"""
DLNE configuration — the single source of truth for the flat ``KEY=VALUE`` config file.

Every module that needs a setting reads it through this file so that there is exactly one
parser, one cache and one writer in the process. The on-disk format is intentionally kept
as flat ``KEY=VALUE`` lines (one per line, ``#`` for comments): invite files, the setup
wizard and the release build scripts all generate and consume that shape, so INI sections
or JSON must never be introduced here.

Public surface (relied on by other modules — do not rename):
    CONFIG_FILE        absolute path of ``config/config.txt``
    get_base_dir()     absolute path of the installation root (writable state)
    get_bundle_dir()   absolute path of the read-only assets shipped inside the build
    get_config()       raw ``UPPERCASE_KEY -> str`` snapshot, cached on the file's stat
    get_worker_limits()typed view of the subset the engine treats as operational limits
    set_config_values()atomic read-modify-write that preserves comments and unknown keys
"""

import os
import sys
import tempfile
import threading
import time
from typing import List


# --------------------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------------------

def get_base_dir() -> str:
    """
    Absolute path to the DLNE installation root.

    When frozen by PyInstaller the payload is unpacked next to the executable, so the
    executable's directory *is* the root. Running from source this file lives at
    ``<root>/engine/config.py``, so the root is two directories up.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_bundle_dir() -> str:
    """
    Absolute path to the read-only assets shipped *inside* the build (``webui/`` and
    anything else passed to PyInstaller's ``--add-data``).

    This is deliberately not :func:`get_base_dir`. A onefile build unpacks its payload
    into a temporary directory and records it in ``sys._MEIPASS``; the executable's own
    directory holds only the installation's writable state — ``config/``, ``models/``,
    ``logs/``. The two are the same directory when running from source and different in
    every shipped build, so reading bundled assets from the installation root finds
    nothing once frozen. That is exactly how the graphical setup wizard came to report
    "the wizard's web assets are missing from this build" on every release.
    """
    bundled = getattr(sys, "_MEIPASS", None)
    return bundled if bundled else get_base_dir()


# Exported so nothing else has to rebuild these paths by hand (setup_cli.py imports
# CONFIG_FILE directly). os.path.join keeps this correct on Windows, Linux and macOS.
CONFIG_DIR: str = os.path.join(get_base_dir(), "config")
CONFIG_FILE: str = os.path.join(CONFIG_DIR, "config.txt")

# Naming base for the staging file that is written first and then os.replace()d onto
# CONFIG_FILE, so a crash mid-write can never leave a truncated config behind. The real
# staging path is a *unique* name derived from this one (see _make_staging_file): a fixed
# name meant two concurrent set_config_values() calls wrote the same bytes-in-progress and
# could os.replace() a half-merged file onto the live config.
_CONFIG_TMP_FILE: str = CONFIG_FILE + ".tmp"

# os.replace() fails with WinError 5 (access denied) on Windows while any other handle to
# the destination is open — an antivirus scanner, a text editor, a backup agent, or simply
# another thread of ours mid-read. Those handles are all short-lived, so a few short
# retries turn a silent "config unchanged, returned False" into a successful write.
_REPLACE_ATTEMPTS: int = 5
_REPLACE_BACKOFF_SECONDS: float = 0.05


# --------------------------------------------------------------------------------------
# Raw config access (cached)
# --------------------------------------------------------------------------------------

# get_worker_limits() is called by the coordinator's per-request middleware, so an
# uncached implementation means a file read on every single HTTP request. The cache is
# keyed on the file's (mtime_ns, size) — cheap to check, and any external edit (the setup
# wizard, an operator with a text editor) changes at least one of the two.
_CACHE_LOCK = threading.RLock()
_cached_config: dict[str, str] = {}
_cached_stamp: tuple[int, int] | None = None

# Serialises the whole read-modify-write in set_config_values. Two threads that both read
# the file, each apply their own update to that snapshot and then both write would lose one
# of the two updates entirely — the setup wizard, the WireGuard peer allocator and the
# coordinator all write config concurrently, so this is a real interleaving, not a
# theoretical one. Deliberately a separate lock from _CACHE_LOCK, and always taken *before*
# it, so the ordering can never invert into a deadlock.
_WRITE_LOCK = threading.Lock()

# Sentinel stamp used when the config file does not exist yet. It must differ from any
# real stat result so that creating the file later is detected as a change.
_MISSING_STAMP: tuple[int, int] = (-1, -1)


def _log_config_error(message: str) -> None:
    """
    Surface a configuration failure loudly.

    engine.logger imports this module, so importing the logger at module scope would be a
    circular import. The deferred import keeps config.py dependency-free at import time,
    and the print fallback guarantees the operator still sees the failure if the logger
    itself is unavailable (e.g. during very early startup).
    """
    try:
        from engine.logger import log_error
        log_error(message)
    except Exception:
        print(f"[CONFIG] {message}")


def _stat_stamp() -> tuple[int, int]:
    """Returns the cache key (mtime_ns, size) for the config file, or the missing sentinel."""
    try:
        stat_result = os.stat(CONFIG_FILE)
        return (stat_result.st_mtime_ns, stat_result.st_size)
    except OSError:
        return _MISSING_STAMP


def _parse_config_lines(lines) -> dict[str, str]:
    """
    Parses flat ``KEY=VALUE`` lines into a dict with UPPERCASE keys.

    Blank lines, ``#`` comment lines and lines without a ``=`` are skipped. Keys and
    values are stripped so that a hand-edited ``KEY = value`` still resolves — the old
    parser only stripped the whole line and silently produced a key with a trailing space.
    Later occurrences of a key win, which matches the previous last-write-wins behaviour.
    """
    parsed: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().upper()
        if not key:
            continue
        parsed[key] = value.strip()
    return parsed


def get_config(force_reload: bool = False) -> dict[str, str]:
    """
    Returns the raw configuration as ``UPPERCASE_KEY -> str``.

    The result is a copy: callers routinely keep the dict around and a mutation must not
    be able to poison the shared cache. Pass ``force_reload=True`` right after something
    outside this process (the setup wizard) has rewritten the file.
    """
    global _cached_config, _cached_stamp

    stamp = _stat_stamp()
    with _CACHE_LOCK:
        if force_reload or _cached_stamp is None or stamp != _cached_stamp:
            if stamp == _MISSING_STAMP:
                # No config yet (first run, or the file was deleted): everything falls
                # back to defaults rather than raising at import/request time.
                _cached_config = {}
            else:
                try:
                    # Slurped and parsed *after* the handle closes rather than iterated
                    # through the open file. The config is a few hundred bytes, and on
                    # Windows an open read handle is exactly what makes a concurrent
                    # os.replace() of this path fail with "access denied" — so the handle
                    # is held for the shortest possible time.
                    with open(CONFIG_FILE, "r", encoding="utf-8", errors="replace") as handle:
                        raw_text = handle.read()
                    _cached_config = _parse_config_lines(raw_text.splitlines())
                except OSError as exc:
                    # A transient read failure (file briefly locked by another writer on
                    # Windows) must not explode inside a request handler. Serve the last
                    # good snapshot and leave the stamp untouched so the next call retries.
                    _log_config_error(f"Could not read {CONFIG_FILE}: {exc}")
                    return dict(_cached_config)
            _cached_stamp = stamp
        return dict(_cached_config)


# --------------------------------------------------------------------------------------
# Typed view used by the engine
# --------------------------------------------------------------------------------------

def _as_bool(raw: str) -> bool:
    """
    Parses a boolean exactly the way DLNE always has: only the literal string ``true``
    (any casing) is true. Anything else — including typos — is false, so the security
    flags that use this helper (ALLOW_UNSIGNED_WORKERS, TRUST_PROXY_HEADERS) fail closed.
    """
    return raw.strip().lower() == "true"


# The --spec-type values llama.cpp b11838 accepts.
SPEC_TYPES = frozenset({
    "none", "draft-simple", "draft-eagle3", "draft-mtp", "draft-dflash", "draft-dspark",
    "ngram-simple", "ngram-map-k", "ngram-map-k4v", "ngram-mod", "ngram-cache",
})

# Off unless the operator turns it on: on the production host (two 8 GB Navi12 cards, 14B
# coder, 0.5B draft) draft-simple with these settings measured 36.3 tok/s against 39.2 tok/s
# without it, despite 80-90% acceptance. Whether drafting pays depends on the hardware, so
# it is measured per host and set with SPEC_* in config.txt. The draft settings below are
# llama.cpp's long-standing server defaults, used once a draft type is switched on.
SPEC_DEFAULTS = {
    "spec_type": "none",
    "spec_draft_n_max": 16,
    "spec_draft_n_min": 0,
    "spec_draft_p_min": 0.75,
}

# The coding engine's per-host tuning for models the automatic sizing cannot place (a
# mixture-of-experts model whose experts partly live in RAM, 2.3.3). ENGINE_CONTEXT fixes the
# total -c (0 = automatic). ENGINE_EXTRA_ARGS adds llama-server flags, only from this list and
# only while the engine runs on this host's own GPUs: a split like -ts names local cards.
ENGINE_DEFAULTS = {
    "engine_context": 0,
    "engine_extra_args": (),
}
ENGINE_ARG_FLAGS_WITH_VALUE = frozenset({
    "--n-cpu-moe", "-ncmoe", "-ts", "--tensor-split", "-ot", "--override-tensor", "-sm", "--split-mode",
    "-mg", "--main-gpu", "-t", "--threads", "-tb", "--threads-batch", "-C", "--cpu-mask", "--cpu-strict",
    "-Cb", "--cpu-mask-batch", "--cpu-strict-batch", "--chat-template-kwargs", "--reasoning-budget",
    "--reasoning-format", "--ctx-checkpoints", "--checkpoint-min-step",
})
ENGINE_ARG_FLAGS = frozenset({"--swa-full", "--no-mmap"})
# Where the model's weights go on this host's own cards. Written for those cards, they are wrong
# once volunteers' GPUs join over RPC, so only these are left out then; the rest (threads, CPU
# pinning, and above all the chat settings such as thinking off) apply wherever the model runs.
ENGINE_PLACEMENT_FLAGS = frozenset({
    "--n-cpu-moe", "-ncmoe", "-ts", "--tensor-split", "-ot", "--override-tensor", "-sm", "--split-mode",
    "-mg", "--main-gpu",
})


def engine_args_without_placement(args) -> List[str]:
    """ENGINE_EXTRA_ARGS (already parsed) minus the placement flags and their values."""
    out: List[str] = []
    i = 0
    args = list(args)
    while i < len(args):
        if args[i] in ENGINE_PLACEMENT_FLAGS:
            i += 2
        elif args[i] in ENGINE_ARG_FLAGS_WITH_VALUE:
            out += args[i:i + 2]
            i += 2
        else:
            out.append(args[i])
            i += 1
    return out


def parse_engine_args(raw: str) -> List[str]:
    """ENGINE_EXTRA_ARGS as llama-server arguments, or [] if any token is not allowed (a typo
    keeps the engine on its defaults rather than failing to start)."""
    import shlex

    try:
        tokens = shlex.split(raw)
    except ValueError:
        return []
    out: List[str] = []
    i = 0
    while i < len(tokens):
        flag = tokens[i]
        if flag in ENGINE_ARG_FLAGS:
            out.append(flag)
            i += 1
        elif flag in ENGINE_ARG_FLAGS_WITH_VALUE and i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
            out += [flag, tokens[i + 1]]
            i += 2
        else:
            return []
    return out


CPU_NODE_DEFAULTS = {
    "cpu_min_ram_gb": 8.0,
    "cpu_min_cores": 4,             # physical: hyperthreads add little to a bandwidth-bound job
    "cpu_min_bandwidth_gbs": 30.0,  # good dual-channel DDR4 measures ~30-35, DDR5 ~45-70
}


def get_worker_limits() -> dict:
    """
    Returns the typed operational limits, with defaults for every key.

    Key names and types are part of the internal API (hardware.py, coordinator.py,
    worker.py, inference_manager.py and downloader.py all index this dict), so they are
    never renamed. Keys that previously had no default — ``network_key``, ``admin_hash``,
    ``dependency_mirror_url`` — now always exist, which removes the KeyError-shaped
    landmines that callers were papering over with ``.get(..., "")``.
    """
    limits = {
        # Comma-separated when splitting tensors across multiple GPUs, hence a string.
        "max_vram_gb": "-1",
        "max_ram_gb": -1.0,
        "max_cpu_threads": -1,
        "allowed_gpu_indices": "-1",
        "enforce_model_whitelist": False,
        "enforce_contrib_clause": True,
        "starter_quota_inferences": 100,
        "redundancy_priority_index": 1,
        "custom_gguf_dir": os.path.join(get_base_dir(), "models"),
        # Previously undefined unless present in the file.
        "admin_hash": "",
        # SHA-256 (hex) of the operator key BarrkBOT presents as `Authorization: Bearer <key>`
        # on /v1/admin/*. Only the hash lives here; `dlne_server.py adminkey create` mints
        # the key and prints it exactly once. Empty disables key-based admin access.
        "admin_key_hash": "",
        # Admin-key requests are accepted only from this machine unless this is on. BarrkBOT
        # shares the coordinator's host, so the default needs no exposure to the network.
        "admin_allow_remote": False,
        "network_key": "",
        "dependency_mirror_url": "",
        # Cluster authentication (see the worker heartbeat signing path).
        "allow_unsigned_workers": False,
        "trust_proxy_headers": False,
        # How many reverse proxies sit in front of the Coordinator. Only consulted when
        # trust_proxy_headers is on, where it selects which X-Forwarded-For entry is the
        # one our outermost trusted proxy wrote (see coordinator.get_client_ip). 1 is the
        # overwhelmingly common deployment — a single nginx or Caddy — and it must have a
        # default here rather than only in the reader, because a missing key sends that
        # reader back to the raw config file on a path that is otherwise served entirely
        # from this cached, typed view.
        "trusted_proxy_hops": 1,
        # WireGuard hub details, read by the tunnel enrollment endpoint when it tells a
        # joining node where to dial. WG_WAN_IP is written by both setup wizards; empty
        # means "answer with the address the request arrived from", which is right for a
        # LAN cluster and wrong only for a hub behind NAT that never ran the WAN setup.
        "wg_wan_ip": "",
        "wg_listen_port": 51820,
        # Tunnel MTU, written into every WireGuard [Interface] stanza DLNE generates.
        #
        # 1380 rather than WireGuard's usual 1420. 1420 assumes a 1500-byte path; PPPoE
        # (1492) and any carrier-side encapsulation put the real ceiling below that, and
        # when the oversized packet is dropped the ICMP "fragmentation needed" reply is
        # routinely filtered on residential links. The tunnel then passes small packets
        # and silently black-holes large ones — handshakes, heartbeats and /health work
        # while inference returns a blank response. Costing a few extra packets is the
        # cheap side of that trade.
        "wg_mtu": 1380,
        # How long the worker set must stop changing before the Coordinator rebuilds the
        # engine around it. A rebuild re-uploads every remote shard's weights, so reacting
        # instantly to a flapping node is expensive; see the monitor loop for why only
        # growth is debounced and shrinkage is not.
        "topology_settle_seconds": 30,
        # Whether new worker connections trigger auto-rebalance/restart of running models.
        # False by default (zero-rework policy): existing running inference continues undisturbed.
        "rebalance_on_join": False,
        # "auto"   - shard only when the model does not fit the cheapest devices alone.
        # "always" - hand every registered endpoint to llama-server, the pre-1.1.0
        #            behaviour, kept for demos and for exercising the sharding path.
        "shard_policy": "auto",
        # Whether a speculative-decoding draft model may be spread across RPC backends.
        # llama.cpp distributes it by default, which is almost never what anyone wants:
        # the draft exists to save round trips, so putting it on the far end of one
        # cancels the whole benefit. Off means DLNE pins it with --spec-draft-device.
        "draft_on_rpc": False,
        # Which local device the pinned draft model runs on (a llama.cpp device name such as
        # "Vulkan0" or "CUDA0"). Empty means "pick for this machine": CUDA0 on NVIDIA, else
        # Vulkan0. Resolved at the point of use because it depends on the detected GPU.
        "draft_device": "",
        # Speculative decoding, passed to llama-server as --spec-type and --spec-draft-*.
        # llama.cpp defaults --spec-type to "none", so a draft model given without it is
        # loaded into VRAM and never consulted - which is how DLNE ran until 2.1.1. Draft
        # types (draft-*) only apply when the model has a "_dflash" companion; the ngram
        # types need no second model. See SPEC_DEFAULTS for why it ships switched off.
        **SPEC_DEFAULTS,
        **ENGINE_DEFAULTS,
        # What a node with no usable GPU must bring to be admitted. Generating a token reads
        # every weight a node holds, so a CPU node's speed is its memory bandwidth, and one
        # slow node holds up every token of the pipeline it sits in. These are floors: an
        # operator can raise them, and a node below any one of them is refused with the
        # reason (see coordinator.cpu_node_shortfalls).
        **CPU_NODE_DEFAULTS,
        # A CPU contributor's use of the GPU coding agent: this many chats in any window of
        # this many hours (engine/priority.UsageWindow). GPU contributors are not limited.
        "cpu_coding_limit": 300,
        "cpu_coding_window_hours": 5.0,
        # The CPU pool (engine/app_pool.py). A bigger model tier unlocks once this many members
        # can host it; phone users get one answer at a time and this many requests a minute.
        "app_tier_min_members": 2,
        "app_rate_per_minute": 6,
        # The coordinator host's own pool member: off unless the operator gives it cores. The
        # mask is llama.cpp's --cpu-mask (hex, one thread pinned per logical CPU).
        "app_local_enabled": False,
        "app_local_cpu_mask": "",
        "app_local_threads": 4,
        "app_local_ram_gb": 8.0,
        "app_local_port": 8099,
        # A volunteer's pool member: llama-server threads, 0 = half the physical cores, 2 to 8
        # (engine/app_member.default_threads), so the owner keeps the rest of their machine.
        "app_member_threads": 0,
        # The phone chat's lookup tools (engine/search.py: weather, wikipedia, news, web_search),
        # always run by the coordinator, never by a pool member. Every tool works with no key:
        # web_search asks the local SearXNG first, then Brave (only with a key, and only
        # search_daily_limit times a day, which keeps it inside Brave's free monthly credit),
        # then Wikipedia. The Brave key is a secret: it goes to Brave and nowhere else.
        "search_enabled": True,
        "searxng_url": "http://127.0.0.1:8888",
        "brave_search_api_key": "",
        "search_daily_limit": 33,
        # How long a lookup's answer is reused for the same question (weather and news keep
        # theirs shorter); 0 turns caching off.
        "search_cache_minutes": 30,
        # How much evidence one lookup hands the model: a CPU model reads every token before
        # its first word, so this is deliberately tight.
        "search_context_tokens": 1000,
        # Lookups per network address in any hour, across all four tools (cache hits are free).
        "search_per_network_per_hour": 30,
    }

    config = get_config()

    # Each key is only overridden when actually present, so a missing entry keeps the
    # default above and a malformed numeric entry keeps it too (unchanged behaviour).
    if "MAX_VRAM_GB" in config:
        limits["max_vram_gb"] = config["MAX_VRAM_GB"]

    if "MAX_RAM_GB" in config:
        try:
            limits["max_ram_gb"] = float(config["MAX_RAM_GB"])
        except ValueError:
            pass

    if "MAX_CPU_THREADS" in config:
        try:
            limits["max_cpu_threads"] = int(config["MAX_CPU_THREADS"])
        except ValueError:
            pass

    if "ALLOWED_GPU_INDICES" in config:
        limits["allowed_gpu_indices"] = config["ALLOWED_GPU_INDICES"]

    if "ENFORCE_MODEL_WHITELIST" in config:
        limits["enforce_model_whitelist"] = _as_bool(config["ENFORCE_MODEL_WHITELIST"])

    if "ENFORCE_CONTRIB_CLAUSE" in config:
        limits["enforce_contrib_clause"] = _as_bool(config["ENFORCE_CONTRIB_CLAUSE"])

    if "STARTER_QUOTA_INFERENCES" in config:
        try:
            limits["starter_quota_inferences"] = int(config["STARTER_QUOTA_INFERENCES"])
        except ValueError:
            pass

    if "REDUNDANCY_PRIORITY_INDEX" in config:
        try:
            limits["redundancy_priority_index"] = int(config["REDUNDANCY_PRIORITY_INDEX"])
        except ValueError:
            pass

    if "CUSTOM_GGUF_DIR" in config:
        limits["custom_gguf_dir"] = config["CUSTOM_GGUF_DIR"]

    if "ADMIN_HASH" in config:
        limits["admin_hash"] = config["ADMIN_HASH"]

    if "ADMIN_KEY_HASH" in config:
        limits["admin_key_hash"] = config["ADMIN_KEY_HASH"].strip().lower()

    if "ADMIN_ALLOW_REMOTE" in config:
        limits["admin_allow_remote"] = _as_bool(config["ADMIN_ALLOW_REMOTE"])

    if "NETWORK_KEY" in config:
        limits["network_key"] = config["NETWORK_KEY"]

    if "DEPENDENCY_MIRROR_URL" in config:
        limits["dependency_mirror_url"] = config["DEPENDENCY_MIRROR_URL"]

    if "ALLOW_UNSIGNED_WORKERS" in config:
        limits["allow_unsigned_workers"] = _as_bool(config["ALLOW_UNSIGNED_WORKERS"])

    if "WG_WAN_IP" in config:
        limits["wg_wan_ip"] = config["WG_WAN_IP"]

    if "WG_LISTEN_PORT" in config:
        try:
            limits["wg_listen_port"] = int(config["WG_LISTEN_PORT"])
        except ValueError:
            pass

    if "WG_MTU" in config:
        try:
            mtu = int(config["WG_MTU"])
        except ValueError:
            mtu = 0
        # Below 1280 is not a legal IPv6 link MTU and is far below anything a real path
        # imposes; above 1500 cannot survive an ordinary Ethernet hop. Either is a typo
        # rather than a deployment, and honouring it would break the tunnel it is meant
        # to fix, so the default stands.
        if 1280 <= mtu <= 1500:
            limits["wg_mtu"] = mtu

    if "TOPOLOGY_SETTLE_SECONDS" in config:
        try:
            settle = int(config["TOPOLOGY_SETTLE_SECONDS"])
        except ValueError:
            settle = -1
        # Zero is meaningful — it restores the pre-1.1.0 "rebuild immediately" behaviour.
        if settle >= 0:
            limits["topology_settle_seconds"] = settle

    if "SHARD_POLICY" in config:
        policy = config["SHARD_POLICY"].strip().lower()
        if policy in ("auto", "always"):
            limits["shard_policy"] = policy

    if "DRAFT_ON_RPC" in config:
        limits["draft_on_rpc"] = _as_bool(config["DRAFT_ON_RPC"])

    if "DRAFT_DEVICE" in config:
        limits["draft_device"] = config["DRAFT_DEVICE"].strip()

    # The CPU-node floors may only be raised: lowering them is how one slow machine drags
    # the whole swarm down, so a value under the default (or a typo) keeps the default.
    for key, cast in (("CPU_MIN_RAM_GB", float), ("CPU_MIN_CORES", int), ("CPU_MIN_BANDWIDTH_GBS", float)):
        if key in config:
            try:
                value = cast(config[key])
            except ValueError:
                continue
            if value >= CPU_NODE_DEFAULTS[key.lower()]:
                limits[key.lower()] = value

    for key, cast, low, high in (("APP_TIER_MIN_MEMBERS", int, 1, 1000), ("APP_RATE_PER_MINUTE", int, 1, 600),
                                 ("APP_LOCAL_THREADS", int, 1, 256), ("APP_LOCAL_RAM_GB", float, 1, 4096),
                                 ("APP_LOCAL_PORT", int, 1024, 65535), ("APP_MEMBER_THREADS", int, 0, 256)):
        if key in config:
            try:
                value = cast(config[key])
            except ValueError:
                continue
            if low <= value <= high:
                limits[key.lower()] = value

    if "APP_LOCAL_ENABLED" in config:
        limits["app_local_enabled"] = _as_bool(config["APP_LOCAL_ENABLED"])

    if "APP_LOCAL_CPU_MASK" in config:
        mask = config["APP_LOCAL_CPU_MASK"].strip().lower()
        # llama.cpp takes the mask as hex; anything else would fail its argument parsing.
        if mask == "" or (mask.startswith("0x") and len(mask) > 2 and all(c in "0123456789abcdef" for c in mask[2:])):
            limits["app_local_cpu_mask"] = mask

    if "SEARCH_ENABLED" in config:
        limits["search_enabled"] = _as_bool(config["SEARCH_ENABLED"])

    if "SEARXNG_URL" in config:
        url = config["SEARXNG_URL"].strip()
        # The coordinator fetches this address itself, so only an http(s) base is accepted;
        # empty skips SearXNG and web_search starts at Brave (or Wikipedia).
        if url == "" or url.lower().startswith(("http://", "https://")):
            limits["searxng_url"] = url.rstrip("/")

    if "BRAVE_SEARCH_API_KEY" in config:
        limits["brave_search_api_key"] = config["BRAVE_SEARCH_API_KEY"].strip()

    for key, low, high in (("SEARCH_DAILY_LIMIT", 0, 100000), ("SEARCH_CACHE_MINUTES", 0, 1440),
                           ("SEARCH_CONTEXT_TOKENS", 300, 8000), ("SEARCH_PER_NETWORK_PER_HOUR", 1, 1000)):
        if key in config:
            try:
                value = int(config[key])
            except ValueError:
                continue
            if low <= value <= high:
                limits[key.lower()] = value

    if "CPU_CODING_LIMIT" in config:
        try:
            value = int(config["CPU_CODING_LIMIT"])
        except ValueError:
            value = 0
        if value >= 1:
            limits["cpu_coding_limit"] = value

    if "CPU_CODING_WINDOW_HOURS" in config:
        try:
            hours = float(config["CPU_CODING_WINDOW_HOURS"])
        except ValueError:
            hours = 0.0
        if 0.25 <= hours <= 168:
            limits["cpu_coding_window_hours"] = hours

    if "SPEC_TYPE" in config:
        types = [t.strip().lower() for t in config["SPEC_TYPE"].split(",") if t.strip()]
        # Everything or nothing: llama-server refuses to start on an unknown type, so a typo
        # keeps the default rather than taking the engine down.
        if types and all(t in SPEC_TYPES for t in types):
            limits["spec_type"] = "none" if "none" in types else ",".join(types)

    if "SPEC_DRAFT_N_MAX" in config:
        try:
            n_max = int(config["SPEC_DRAFT_N_MAX"])
        except ValueError:
            n_max = 0
        if 1 <= n_max <= 64:
            limits["spec_draft_n_max"] = n_max

    if "SPEC_DRAFT_N_MIN" in config:
        try:
            n_min = int(config["SPEC_DRAFT_N_MIN"])
        except ValueError:
            n_min = -1
        if 0 <= n_min <= limits["spec_draft_n_max"]:
            limits["spec_draft_n_min"] = n_min

    if "SPEC_DRAFT_P_MIN" in config:
        try:
            p_min = float(config["SPEC_DRAFT_P_MIN"])
        except ValueError:
            p_min = -1.0
        if 0.0 <= p_min <= 1.0:
            limits["spec_draft_p_min"] = p_min

    if "ENGINE_CONTEXT" in config:
        try:
            context = int(config["ENGINE_CONTEXT"])
        except ValueError:
            context = -1
        if context == 0 or 2048 <= context <= 1048576:
            limits["engine_context"] = context

    if "ENGINE_EXTRA_ARGS" in config:
        limits["engine_extra_args"] = parse_engine_args(config["ENGINE_EXTRA_ARGS"])

    if "REBALANCE_ON_JOIN" in config:
        limits["rebalance_on_join"] = _as_bool(config["REBALANCE_ON_JOIN"])

    if "TRUST_PROXY_HEADERS" in config:
        limits["trust_proxy_headers"] = _as_bool(config["TRUST_PROXY_HEADERS"])

    if "TRUSTED_PROXY_HOPS" in config:
        try:
            hops = int(config["TRUSTED_PROXY_HOPS"])
        except ValueError:
            hops = 0
        # A non-positive hop count would index from the wrong end of the forwarded chain
        # entirely, which is the exact bug the right-most read exists to prevent, so a
        # typo keeps the safe default instead of silently mis-identifying every client.
        if hops >= 1:
            limits["trusted_proxy_hops"] = hops

    return limits


# --------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------

def _make_staging_file() -> tuple[int, str]:
    """
    Creates a uniquely named, empty staging file next to ``CONFIG_FILE``.

    Returns the open file descriptor and its path. Two properties matter here:

    * **Unique name.** A fixed ``config.txt.tmp`` is shared state: two concurrent writers
      interleave their bytes in it and whichever one renames second publishes a corrupted
      merge of both. ``mkstemp`` hands each caller its own private path.
    * **Same directory.** ``os.replace`` is only atomic within a single filesystem, so the
      staging file must not live in ``%TEMP%``. The directory is derived from
      ``_CONFIG_TMP_FILE`` so that a test (or an embedder) which re-points the config tree
      moves the staging file with it.

    Note that ``mkstemp`` creates the file 0600 on POSIX, and the rename carries that
    through to ``config.txt``. That is a deliberate tightening rather than an accident:
    the file holds NETWORK_KEY and ADMIN_HASH, which no other local account should be
    able to read.
    """
    staging_dir = os.path.dirname(_CONFIG_TMP_FILE) or CONFIG_DIR
    os.makedirs(staging_dir, exist_ok=True)
    # Prefix/suffix are cosmetic but make an orphaned file (killed process mid-write)
    # obviously identifiable as DLNE's rather than mysterious litter.
    return tempfile.mkstemp(
        prefix=os.path.basename(_CONFIG_TMP_FILE) + ".",
        suffix=".partial",
        dir=staging_dir,
    )


def _discard_staging_file(path: str) -> None:
    """
    Removes an abandoned staging file, ignoring the case where it never existed.

    Leaving it behind would be harmless to correctness — the unique name means it can
    never be mistaken for the live config — but it would accumulate one file per failed
    write in the operator's ``config/`` directory.
    """
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        # Already gone, or the same permission problem that made the write fail. Either
        # way there is nothing useful left to do about it.
        pass


def _replace_with_retry(source_path: str, destination_path: str) -> bool:
    """
    ``os.replace(source, destination)`` with a short bounded retry, returning success.

    On Windows the rename fails with ``PermissionError`` / WinError 5 while *any* other
    handle to the destination is open — an on-access antivirus scan, an editor, a backup
    agent, or one of our own reader threads between ``open()`` and ``close()``. Those
    handles are held for milliseconds, so retrying briefly converts a silent "the config
    did not change and nobody noticed" into a completed write. A genuinely permanent
    failure (read-only volume, ACL) still returns False after the last attempt, and the
    caller logs it.
    """
    last_error: OSError | None = None

    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source_path, destination_path)
            return True
        except OSError as exc:
            last_error = exc
            # No point sleeping after the final attempt.
            if attempt < _REPLACE_ATTEMPTS - 1:
                time.sleep(_REPLACE_BACKOFF_SECONDS)

    _log_config_error(
        f"Could not replace {destination_path} after {_REPLACE_ATTEMPTS} attempts "
        f"({_REPLACE_ATTEMPTS * _REPLACE_BACKOFF_SECONDS:.2f}s): {last_error}. "
        "The configuration on disk is UNCHANGED — close any program holding that file "
        "open (editor, antivirus scan, backup agent) and try again."
    )
    return False


def set_config_values(updates: dict[str, str]) -> bool:
    """
    Applies ``{KEY: value}`` to ``config/config.txt`` atomically and returns success.

    The file is rewritten line by line so comments, blank lines, key order and any key
    this module has never heard of survive untouched. Keys already present are edited in
    place (keeping their original spelling); new keys are appended UPPERCASE. The result
    is written to a unique staging file in the same directory and then ``os.replace``d
    onto the real path, which is atomic on every supported platform — a crash mid-write
    can never truncate the config that the whole cluster's identity lives in.

    The entire read-modify-write runs under ``_WRITE_LOCK``, so two threads updating
    different keys at the same moment can no longer lose one of the two updates.
    """
    global _cached_stamp

    if not updates:
        return True

    # Normalise once: keys are matched case-insensitively, values are always written as
    # text (callers pass ints and bools freely).
    normalised: dict[str, str] = {}
    for key, value in updates.items():
        clean_key = str(key).strip().upper()
        if not clean_key:
            continue
        normalised[clean_key] = str(value)
    if not normalised:
        return True

    # Everything from here on is one indivisible read-modify-write.
    with _WRITE_LOCK:
        existing_lines: list[str] = []
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8", errors="replace") as handle:
                existing_lines = handle.read().splitlines()
        except FileNotFoundError:
            # First run: we are creating the config from scratch.
            existing_lines = []
        except OSError as exc:
            _log_config_error(f"Could not read {CONFIG_FILE} before updating it: {exc}")
            return False

        written_keys: set[str] = set()
        output_lines: list[str] = []

        for raw_line in existing_lines:
            stripped = raw_line.strip()

            # Comments, blank lines and anything that is not a KEY=VALUE pair are copied
            # through verbatim — operators annotate this file by hand.
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                output_lines.append(raw_line)
                continue

            key_text, _, _old_value = stripped.partition("=")
            key = key_text.strip().upper()

            if key not in normalised:
                output_lines.append(raw_line)
                continue

            if key in written_keys:
                # A duplicate of a key we just updated. Dropping it is required, not
                # cosmetic: the parser is last-write-wins, so a stale duplicate further
                # down the file would silently override the value we were asked to set.
                continue

            output_lines.append(f"{key_text.strip()}={normalised[key]}")
            written_keys.add(key)

        for key, value in normalised.items():
            if key not in written_keys:
                output_lines.append(f"{key}={value}")

        staging_path = ""
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            descriptor, staging_path = _make_staging_file()
            # os.fdopen takes ownership of the descriptor, so closing the file object is
            # what closes it — mkstemp's descriptor is never leaked on any path below.
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(output_lines) + "\n")
                # Flush all the way to disk before the rename, otherwise a power loss can
                # leave an atomically-renamed but empty file.
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            _log_config_error(f"Could not write {CONFIG_FILE}: {exc}")
            _discard_staging_file(staging_path)
            return False

        if not _replace_with_retry(staging_path, CONFIG_FILE):
            # _replace_with_retry has already logged the reason in operator-facing terms.
            _discard_staging_file(staging_path)
            return False

        # Invalidate explicitly instead of trusting the stat comparison: a rewrite that
        # happens inside the same filesystem timestamp tick and produces the same byte
        # count would otherwise be served from the stale cache.
        with _CACHE_LOCK:
            _cached_stamp = None

    return True
