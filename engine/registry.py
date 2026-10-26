from typing import Any, Callable, Dict, List, Optional, Tuple
import re
import threading
import time
from engine.logger import log_coordinator, log_warning, log_error

# The longest worker-supplied value that may be interpolated into a log line. A real GPU
# name is around 40 characters; anything much past this is padding sent to flood an
# operator's console, and truncating it costs nothing legitimate.
MAX_LOGGED_VALUE_CHARS = 96

# Square brackets are what rich markup is made of, so neither may survive in a value that is
# about to be interpolated into a markup string. They are mapped to parentheses, which read
# almost identically in a log line and carry no meaning to the parser at all.
#
# WHY NOT ``rich.markup.escape``. Its job is to escape a *complete* string, and it only acts
# on text that already looks like a tag — its pattern requires the closing ``]``. A value of
# ``"[/"`` therefore passes through it untouched, and the surrounding template then supplies
# the ``]``: the composed message contains ``[/ | V.8.0GB | ... ]``, a closing tag matching
# nothing, and rich raises ``MarkupError``. That is the auditor's reproduction verbatim. An
# escaper cannot fix this, because the hazard is created by the *concatenation*; only
# removing the bracket from the interpolated fragment can.
_MARKUP_NEUTRALISATION = str.maketrans({"[": "(", "]": ")"})


def safe_log_text(value: Any, max_chars: int = MAX_LOGGED_VALUE_CHARS) -> str:
    """
    Render an untrusted value so it is safe to interpolate into a log message.

    WHY THIS EXISTS (finding R3-1)
        ``engine.logger`` builds its ``RichHandler`` with ``markup=True``, so every message
        it is handed is parsed as rich console markup. Worker hardware profiles arrive over
        the network from other machines and pass straight into those messages. Two things
        then go wrong with a hostile (or merely broken) value:

        * ``rich`` raises ``MarkupError`` for a closing tag that matches nothing — e.g. a
          GPU ``name`` of ``"[/dim]"`` — and that error escapes ``RichHandler.emit`` rather
          than being absorbed by ``logging``'s own error handling, so it propagates out of
          whatever called the logger. On the heartbeat path that is a remote HTTP 500.
        * Even when it does not raise, unescaped markup lets a remote node colour and
          restyle the operator's console, or forge extra log lines with an embedded newline.

    WHAT IT DOES
        1. Coerces to ``str`` without ever raising (a value whose ``__str__`` explodes
           becomes a placeholder rather than an exception).
        2. Replaces every non-printable character — which includes ``\\r`` and ``\\n``, the
           log-forging characters — with a space.
        3. Neutralises rich markup by mapping ``[`` and ``]`` to parentheses. See the note on
           ``_MARKUP_NEUTRALISATION`` for why escaping is not enough here.

    Truncation happens FIRST, before the per-character scan, because the input length is
    attacker-chosen: a heartbeat carrying a one-megabyte GPU name would otherwise pay for a
    million-iteration generator expression on the request path. Neither of the later steps
    changes the length (both are one-to-one character mappings), so cutting early gives
    exactly the same answer as cutting late.

    Use it for EVERY interpolated value that came from another machine. It is exported
    because ``engine.coordinator`` logs the same worker-supplied fields on the heartbeat
    path and must not grow a second, subtly different copy of this logic.
    """
    try:
        text = str(value)
    except Exception:
        # A value whose __str__ raises is pathological, but a log call must never be the
        # thing that fails a request.
        return "<unprintable>"

    ellipsis = ""
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 1)]
        ellipsis = "…"

    # str.isprintable() is False for every C0/C1 control character (including newline and
    # carriage return) and for unassigned code points, which is exactly the set that can
    # forge a log line or scramble a terminal.
    text = "".join(ch if ch.isprintable() else " " for ch in text)

    return text.translate(_MARKUP_NEUTRALISATION) + ellipsis


# What an endpoint costs to use, cheapest first. The shard planner spends capacity in this
# order, so a model only ever reaches for the network once the free capacity is exhausted.
_CLASS_RANK = {"local": 0, "lan": 1, "wan": 2, "unknown": 3}

# Stands in for a link nobody has measured. Deliberately larger than any real LAN figure and
# comparable to a mediocre WAN one: an unmeasured node must not be *preferred* over one that
# actually reported a good number, and optimism here would place shards on the slowest link
# in the cluster and call it a tie-break.
_UNMEASURED_RTT_MS = 250.0


def _endpoint_class(ip: str) -> str:
    """Where a worker sits relative to this Coordinator: local, lan, wan or unknown."""
    # Imported lazily: engine.benchmark pulls in the WireGuard module for the tunnel subnet,
    # and the registry is imported early enough by enough entry points that widening its
    # import graph at module scope is not worth the risk.
    from engine.benchmark import classify_endpoint
    return classify_endpoint(ip)


def _as_float(value: Any, default: float = 0.0) -> float:
    """
    Coerce a worker-reported hardware value to float.

    Hardware profiles arrive over the network from other machines, so a single
    malformed field (a string, a None, a JSON null) must never be able to raise
    out of a totals call and take down capacity planning for the whole cluster.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    """
    Coerce a worker-reported hardware value to int.

    Same rationale as :func:`_as_float`, and it additionally guarantees the
    CPU total getters keep returning ``int`` even if a worker reports a float.
    """
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_profile(hardware: Any) -> Dict[str, Any]:
    """
    Return a hardware profile as a dictionary, or an empty one.

    A profile that arrived as a list, a string or ``None`` is not merely useless — every
    getter in this class does ``hardware.get(...)`` on it, so storing one would make the
    *next* totals call raise ``AttributeError`` from a completely different code path. It is
    normalised once, here, on the way in.
    """
    return hardware if isinstance(hardware, dict) else {}


def is_app_member(hardware: Any) -> bool:
    """A CPU pool member (it reports ``"pool": "app"`` in its signed hardware profile)."""
    return _as_profile(hardware).get("pool") == "app"


def _gpu_section(hardware: Any) -> Dict[str, Any]:
    """
    Return a worker's ``gpu`` sub-dictionary, or an empty dict.

    ``hardware.get("gpu", {})`` looks like it does this but does NOT: a default only
    applies when the key is *absent*. A CPU-only node legitimately sends
    ``{"gpu": null}``, in which case ``.get`` returns the stored ``None`` and the very
    next ``.get`` on it raises ``AttributeError``. Since the profile arrives from another
    machine over the network, that turned one malformed (or merely CPU-only) heartbeat
    into an unhandled exception inside the totals - which every capacity decision depends
    on. The same guard covers a ``gpu`` that arrives as a string or a list.
    """
    if not isinstance(hardware, dict):
        return {}
    gpu = hardware.get("gpu")
    return gpu if isinstance(gpu, dict) else {}


class WorkerRegistry:
    """
    Tracks every worker node that is currently heartbeating into this Coordinator.

    All state lives in :attr:`workers` and is guarded by a re-entrant lock: the
    registry is written by FastAPI request handlers and read by background
    telemetry/model-management tasks, which run on different threads.
    """

    def __init__(self):
        # Maps worker_id -> a dict of worker info (ip, port, hardware, token, timings).
        self.workers: Dict[str, Dict[str, Any]] = {}
        # Timeout for missing heartbeats (seconds). Workers beat every 5s, so 20s
        # tolerates three consecutive missed beats before we declare a node dead;
        # the previous 15s allowed only two and evicted healthy nodes on a hiccup.
        self.timeout = 20
        # RLock (not Lock): the public getters take the lock and then call
        # prune_dead_workers(), which takes it again on the same thread.
        self._lock = threading.RLock()
        # Maps worker_id -> the last link measurement that node reported about itself.
        #
        # Held apart from `workers` deliberately: update_worker *replaces* a worker's dict
        # wholesale on every heartbeat, so a measurement stored inside that dict would be
        # erased five seconds after it arrived. A node measures its link rarely and beats
        # constantly, which is exactly the pairing that needs two lifetimes.
        self.link_measurements: Dict[str, Dict[str, Any]] = {}
        # The llama.cpp RPC protocol major this Coordinator's engine speaks (from its signed
        # release), or None when unknown. llama-server ABORTS at startup if any --rpc backend
        # speaks another major, so a mismatched worker must never be routed to: it is left
        # out of every endpoint list and of the memory pool until it has upgraded.
        self.required_rpc_major: Optional[int] = None

    def rpc_compatible(self, hardware: Any) -> bool:
        """
        Whether a worker can serve this Coordinator's 14B engine over RPC: its rpc-server
        protocol matches, and it is not a CPU pool member. A CPU pool member (2.3.0) runs a
        whole small model for phone requests and has no rpc-server at all; counting it here
        would put its RAM in the 14B memory budget and its address in --rpc. Every endpoint
        list and memory total below goes through this one check.
        """
        if is_app_member(hardware):
            return False
        if self.required_rpc_major is None:
            return True
        proto = _as_profile(hardware).get("rpc_proto")
        if not isinstance(proto, str) or not re.match(r"^\d+\.\d+\.\d+$", proto):
            return False  # unknown is not compatible: guessing wrong kills the engine
        return int(proto.split(".")[0]) == self.required_rpc_major

    def update_worker(self, worker_id: str, ip: str, port: int, token: str,
                      hardware_profile: Dict[str, Any], role: str = "worker",
                      staged_version: str = ""):
        """
        Called whenever a heartbeat is received from a worker.

        EVERY ARGUMENT CAME OFF THE NETWORK. ``worker_id``, ``ip``, ``port`` and the whole
        hardware profile are values another machine chose, and under
        ``ALLOW_UNSIGNED_WORKERS`` that machine is not even authenticated. Two rules follow,
        and both are load-bearing:

        1. **Nothing worker-supplied is formatted or interpolated raw.** Numbers go through
           :func:`_as_float`/:func:`_as_int` before they meet a format spec, and text goes
           through :func:`safe_log_text` before it meets the markup-enabled logger. Finding
           R3-1: a heartbeat carrying ``"load_percent": "x"`` used to raise ``ValueError:
           Unknown format code 'f' for object of type 'str'`` out of the ``:.1f`` below, and
           a GPU ``name`` of ``"[/dim]"`` used to raise ``rich.errors.MarkupError`` — both of
           them an unhandled exception out of this method, i.e. a remote HTTP 500.

        2. **The registration happens before any logging.** The old order put the ACK log
           line above ``self.workers[worker_id] = {...}``, so when that line raised, the
           worker was never recorded at all. ``last_log_time`` therefore stayed 0, the
           ten-second throttle never engaged, and *every* subsequent heartbeat from that node
           took the same failing branch: a permanent, self-sustaining 500 loop. Log messages
           are now built into ``pending_logs`` under the lock, the record is stored, and the
           messages are emitted afterwards where nothing they do can lose the registration.
        """
        if not worker_id:
            return

        current_time = time.time()

        # Normalise the profile once. A non-dict here (list, string, JSON null) would make
        # every later `hardware.get(...)` in the total getters raise, from a call site that
        # has nothing to do with this heartbeat.
        profile = _as_profile(hardware_profile)

        # Safely extract hardware profile defaults. _gpu_section, not .get('gpu', {}):
        # a CPU-only node sends an explicit JSON null and the dict default never applies.
        gpu_info = _gpu_section(profile)
        vram_gb = _as_float(gpu_info.get('vram_total_gb', 0.0))
        ram_gb = _as_float(profile.get('ram_gb', 0.0))
        # Kept raw, deliberately: this value is both stored and compared against the
        # previously stored one, and coercing it here would change what "the yielding flag
        # changed" means. It is only ever *rendered* through safe_log_text.
        is_yielding = profile.get('is_yielding', False)

        # worker_id is attacker-controlled, and slicing it does not make it safe: a node
        # calling itself "[/d]-x" produces the short id "[/D]", which rich parses as a
        # closing tag that matches nothing.
        short_id = safe_log_text(worker_id.split('-')[0][:4].upper(), max_chars=8)
        safe_ip = safe_log_text(ip, max_chars=64)
        safe_port = _as_int(port)

        # (emitter, message) pairs, built under the lock and emitted after it is released.
        pending_logs: List[Tuple[Callable[[str], None], str]] = []

        with self._lock:
            existing = self.workers.get(worker_id)

            # If it's a new worker, log it
            if existing is None:
                pending_logs.append((
                    log_coordinator,
                    f"New worker connected from {safe_ip}:{safe_port} [Worker-{short_id}] "
                    f"(VRAM: {vram_gb}GB, RAM: {ram_gb}GB)"
                ))
            else:
                # If yielding status changed, log it
                previous_yielding = _as_profile(existing.get('hardware')).get('is_yielding', False)
                if previous_yielding != is_yielding:
                    if is_yielding:
                        pending_logs.append((
                            log_warning,
                            f"[Worker-{short_id}] ({safe_ip}) is now YIELDING due to external "
                            "heavy load (Gaming Mode)."
                        ))
                    else:
                        pending_logs.append((
                            log_coordinator,
                            f"[Worker-{short_id}] ({safe_ip}) has finished yielding and is active again."
                        ))

            # Lightweight Keep-Alive Logging (throttled to 10s per worker)
            last_log = _as_float(existing.get('last_log_time', 0)) if existing else 0.0

            if current_time - last_log >= 10:
                gpu_name = safe_log_text(gpu_info.get('name', 'Unknown'))
                load_pct = _as_float(gpu_info.get('load_percent', 0.0))
                cpu_cores = _as_int(profile.get('cpu_cores', 1), default=1)
                # NOTE THE BACKSLASH before the hardware group's opening bracket. Sanitising
                # the *values* is not sufficient here, because the bracket is ours: rich's
                # tag pattern is `\[[a-z#/@][^[]*?]`, so a GPU name beginning with a
                # lower-case letter turns our own `[name | V.8.0GB | ... ]` group into what
                # rich reads as a style tag. It does not raise — it silently swallows the
                # whole group, and the operator's console shows an ACK line with the GPU,
                # VRAM, RAM, cores, load and yielding state simply missing. A node can
                # therefore hide its own hardware report by lower-casing its GPU name.
                # Escaping the opener makes it a literal bracket again; the closing `]` needs
                # nothing, because a lone `]` has no meaning to the parser.
                pending_logs.append((
                    log_coordinator,
                    f"[dim]Heartbeat ACK: [Worker-{short_id}] {safe_ip}:{safe_port} "
                    f"\\[{gpu_name} | V.{vram_gb}GB | S.{ram_gb}GB | C.{cpu_cores} | "
                    f"Load: {load_pct:.1f}% | Yielding: {safe_log_text(is_yielding, max_chars=16)}][/dim]"
                ))
                last_log = current_time

            # Token rotation grace window: the worker rotates its ephemeral token every 60 s,
            # and a client may still be holding (or mid-request with) the previous one.
            # Keep the prior token valid for a short grace period so rotation never 403s a
            # legitimate contributor.
            prev_record = self.workers.get(worker_id) or {}
            prev_token = prev_record.get("token", "")
            if prev_token and prev_token != token:
                grace_token = prev_token
                grace_expiry = current_time + 90.0
            else:
                grace_token = prev_record.get("grace_token", "")
                grace_expiry = prev_record.get("grace_expiry", 0.0)

            self.workers[worker_id] = {
                "worker_id": worker_id,
                "ip": ip,
                "port": port,
                "hardware": profile,
                "last_heartbeat": current_time,
                "last_log_time": last_log,
                "token": token,
                "grace_token": grace_token,
                "grace_expiry": grace_expiry,
                # Wire-v3 rollout fields. Both were inside the heartbeat MAC (the
                # coordinator verified them before this call), so storing them verbatim
                # is safe — but they are still only ever *rendered* via safe_log_text.
                "role": role if role in ("worker", "redundancy", "hybrid") else "worker",
                "staged_version": str(staged_version),
            }

        for emit, message in pending_logs:
            try:
                emit(message)
            except Exception:
                # Everything interpolated above is already coerced and markup-escaped, so
                # this should be unreachable. It is caught anyway because the alternative —
                # a logging failure propagating out of a heartbeat handler — is precisely
                # the remote 500 that R3-1 (and item 1.1 before it) was raised to eliminate,
                # and a console message is never worth failing a registration over. There is
                # deliberately no fallback log call here: the logger is the thing that just
                # failed, so calling it again is how a handler ends up in a loop.
                pass

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """
        Returns a point-in-time shallow copy of the worker table, taken under the lock.

        Callers outside this class must NEVER iterate :attr:`workers` directly. The
        registry is pruned from background threads (the telemetry loop and every
        ``get_total_*`` call), so an unguarded ``for w in registry.workers.values()``
        in a request handler raises ``RuntimeError: dictionary changed size during
        iteration`` the moment a node times out mid-request. Copying the mapping under
        the lock gives the caller a stable view to walk.

        The copy is shallow on purpose: :meth:`update_worker` *replaces* a worker's dict
        wholesale rather than mutating it in place, so the referenced dicts are
        effectively immutable once published and deep-copying them would only add cost.
        """
        with self._lock:
            return dict(self.workers)

    def count(self) -> int:
        """Number of currently registered workers, read under the lock."""
        with self._lock:
            return len(self.workers)

    def find_by_token(self, token: str) -> Any:
        """
        Returns the worker registered with ``token``, or None.

        Used by the Non-Contrib gate. Kept here (rather than as a snapshot walk at the
        call site) so the scan happens under the lock and short-circuits.
        """
        if not token:
            return None
        now = time.time()
        with self._lock:
            for data in self.workers.values():
                if data.get("token") == token:
                    return data
            for data in self.workers.values():
                if data.get("grace_token") == token and now < data.get("grace_expiry", 0.0):
                    return data
        return None

    def record_link_measurement(self, worker_id: str, measurement: Dict[str, Any]) -> None:
        """
        Store what a node reported about its own link to this Coordinator.

        Only the fields the shard planner and the WebUI actually consume are kept, and each
        is coerced here rather than trusted: this arrives over the network from a machine
        that chose every number in it, and an ``rtt_min_ms`` of ``"fast"`` reaching a format
        spec or an arithmetic comparison is the same class of remote-500 that
        :meth:`update_worker` documents at length.
        """
        if not worker_id or not isinstance(measurement, dict):
            return

        kept: Dict[str, Any] = {"reported_at": time.time()}
        for key in ("rtt_min_ms", "rtt_median_ms", "rtt_max_ms",
                    "upload_mbps", "download_mbps", "token_ceiling_per_second"):
            if key in measurement:
                value = _as_float(measurement.get(key), -1.0)
                if value >= 0:
                    kept[key] = value

        with self._lock:
            # Only for a node that is actually registered: an unknown id here would grow
            # the dict without bound from anything that could reach the endpoint.
            if worker_id in self.workers:
                self.link_measurements[worker_id] = kept

    def get_link_measurement(self, worker_id: str) -> Dict[str, Any]:
        """The last link measurement for a node, or ``{}`` when it has never reported one."""
        with self._lock:
            return dict(self.link_measurements.get(worker_id, {}))

    def find_by_ip(self, ip: str) -> Any:
        """Returns the first worker heartbeating from ``ip``, or None. Locked."""
        if not ip:
            return None
        with self._lock:
            for data in self.workers.values():
                if data.get("ip") == ip:
                    return data
        return None

    def prune_dead_workers(self):
        """Removes workers that haven't sent a heartbeat recently."""
        current_time = time.time()

        with self._lock:
            dead_ids = []

            for w_id, data in self.workers.items():
                if current_time - data["last_heartbeat"] > self.timeout:
                    dead_ids.append(w_id)

            for w_id in dead_ids:
                # The address is whatever get_client_ip resolved for that node, so it is
                # still worker-influenced text going into a markup-enabled logger. Escape it
                # for the same reason update_worker does, and delete the entry *before*
                # logging so a logging failure can never leave a dead node in the pool.
                ip = safe_log_text(self.workers[w_id].get('ip', 'unknown'), max_chars=64)
                del self.workers[w_id]
                # A measurement describes a link to a node that is no longer here. Keeping
                # it would let a stale figure go on influencing shard placement for a
                # machine that has left the cluster.
                self.link_measurements.pop(w_id, None)
                try:
                    log_error(f"Worker {ip} timed out (Sudden Disconnect). Removing from active pool.")
                except Exception:
                    pass

    def get_active_rpc_endpoints(self) -> List[str]:
        """Returns a list of 'IP:PORT' strings for all alive workers. Excludes yielding GPU workers."""
        with self._lock:
            self.prune_dead_workers()
            endpoints = []
            for w_id, data in self.workers.items():
                hardware = data.get("hardware", {})
                has_gpu = _gpu_section(hardware).get("has_gpu", False)
                is_yielding = hardware.get("is_yielding", False)

                # Exclude a yielding GPU worker, and one whose RPC protocol cannot serve.
                if not (has_gpu and is_yielding) and self.rpc_compatible(hardware):
                    endpoints.append(f"{data['ip']}:{data['port']}")

            # Sorted, not just deduped: callers compare this list against the
            # endpoint set a model was launched with. Dict iteration order shifts
            # whenever a worker reconnects, and an unstable order looked like a
            # topology change and triggered a needless full model restart. It also
            # made llama.cpp's layer sharding non-deterministic between runs.
            return sorted(set(endpoints))

    def plan_endpoints(self, required_gb: float, policy: str = "auto") -> List[str]:
        """
        The endpoints a model of this size should actually be spread across.

        WHY THIS IS NOT SIMPLY "ALL OF THEM"
            Until 1.1.0 every registered endpoint went into ``llama-server --rpc``
            unconditionally, and that is only the right answer when the model genuinely
            does not fit without them. Adding a remote backend to a model that already fits
            costs a multi-gigabyte weight transfer before the first token and a synchronous
            network round trip per token afterwards, and buys nothing whatsoever — the
            model was never short of memory. On the live cluster this was the normal case:
            every test ran a 2.4 GB model that fitted the Coordinator's VRAM several times
            over, across a tunnel, and concluded that distributed inference was slow.

            The project's actual purpose is running a model no single machine can hold. So
            capacity is bought in order of what it costs to use: everything local first
            (free), then the LAN, then the WAN, and only as much of each as the model needs.

        Local endpoints are always included regardless of size. They cost nothing to use,
        and dropping them would idle the Coordinator's own GPU while shipping work to
        somebody else's — and on a ``coordinator_only`` deployment with no local worker
        there would be nothing left to run on at all.

        Args:
            required_gb: the model's estimated footprint.
            policy: ``"auto"`` for the above, ``"always"`` for the pre-1.1.0 behaviour of
                using everything. ``"always"`` exists so the sharding path can be exercised
                deliberately on hardware where it would not otherwise engage.

        Returns:
            A deterministically ordered subset of :meth:`get_active_rpc_endpoints`.
        """
        with self._lock:
            self.prune_dead_workers()

            candidates = []
            for w_id, data in self.workers.items():
                hardware = data.get("hardware", {})
                gpu_info = _gpu_section(hardware)
                has_gpu = gpu_info.get("has_gpu", False)
                if has_gpu and hardware.get("is_yielding", False):
                    continue  # Handed back to its owner. Same rule as the endpoint getter.
                if not self.rpc_compatible(hardware):
                    continue  # Would abort llama-server at startup.

                endpoint = f"{data['ip']}:{data['port']}"
                vram = _as_float(gpu_info.get("vram_total_gb", 0.0)) if has_gpu else 0.0
                link = self.link_measurements.get(w_id, {})
                candidates.append({
                    "endpoint": endpoint,
                    "vram": vram,
                    "class": _endpoint_class(data.get("ip", "")),
                    # A node that has never reported a measurement sorts as if it were on
                    # the slow end of its own class rather than the fast end: assuming an
                    # unmeasured link is quick is how a WAN node ends up preferred over a
                    # LAN one that simply answered the question.
                    "rtt": _as_float(link.get("rtt_min_ms"), _UNMEASURED_RTT_MS),
                })

        if not candidates:
            return []

        # A total order, so two ticks with the same cluster always produce the same list —
        # an unstable ordering reads as a topology change and triggers a needless restart.
        # Larger VRAM first within a class: fewer backends for the same capacity means
        # fewer boundaries for the forward pass to cross.
        candidates.sort(key=lambda c: (_CLASS_RANK.get(c["class"], 9), c["rtt"],
                                       -c["vram"], c["endpoint"]))

        if policy == "always":
            return [c["endpoint"] for c in candidates]

        selected: List[str] = []
        budget = 0.0
        for candidate in candidates:
            is_local = candidate["class"] == "local"
            if is_local or budget < required_gb:
                selected.append(candidate["endpoint"])
                budget += candidate["vram"]

        # Nothing local and nothing needed still has to run somewhere.
        if not selected:
            selected.append(candidates[0]["endpoint"])

        return selected

    def get_total_vram_gb(self) -> float:
        """Calculates total available dedicated VRAM across all active workers."""
        with self._lock:
            # Prune first so a node that died seconds ago cannot inflate the pool
            # that model admission and KV-cache sizing are computed from.
            self.prune_dead_workers()
            total = 0.0
            for w_id, data in self.workers.items():
                hardware = data.get("hardware", {})
                if not hardware.get("is_yielding", False) and self.rpc_compatible(hardware):
                    gpu_info = _gpu_section(hardware)
                    if gpu_info.get("has_gpu", False):
                        total += _as_float(gpu_info.get("vram_total_gb", 0.0))
            return total

    def get_total_sysram_gb(self) -> float:
        """Calculates total available System RAM (Spillover) across all active workers."""
        with self._lock:
            self.prune_dead_workers()
            total = 0.0
            for w_id, data in self.workers.items():
                hardware = data.get("hardware", {})
                # A yielding node has handed its machine back to its owner (Gaming
                # Mode); counting its RAM would let us size a KV cache against
                # memory that is not ours to spend.
                if hardware.get("is_yielding", False) or not self.rpc_compatible(hardware):
                    continue
                total += _as_float(hardware.get("ram_gb", 0))
            return total

    def get_total_cpu_cores(self) -> int:
        """Calculates total physical CPU cores across all active workers."""
        with self._lock:
            self.prune_dead_workers()
            total = 0
            for w_id, data in self.workers.items():
                hardware = data.get("hardware", {})
                if hardware.get("is_yielding", False) or is_app_member(hardware):
                    continue
                total += _as_int(hardware.get("cpu_cores", 0))
            return total

    def get_total_cpu_threads(self) -> int:
        """Calculates total logical CPU threads across all active workers."""
        with self._lock:
            self.prune_dead_workers()
            total = 0
            for w_id, data in self.workers.items():
                hardware = data.get("hardware", {})
                if hardware.get("is_yielding", False) or is_app_member(hardware):
                    continue
                total += _as_int(hardware.get("cpu_threads", 0))
            return total

    def app_members(self) -> List[Dict[str, Any]]:
        """Live CPU pool members (worker records), for engine/app_pool.py."""
        with self._lock:
            self.prune_dead_workers()
            return [dict(data) for data in self.workers.values()
                    if is_app_member(data.get("hardware", {}))
                    and not _as_profile(data.get("hardware", {})).get("is_yielding", False)]
