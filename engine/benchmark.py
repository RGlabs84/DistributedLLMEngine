"""
Speed measurement for a DLNE cluster, taken at both ends of the link.

WHY THIS EXISTS AT ALL
    Until 1.1.0 nothing in DLNE reported how fast anything was. Diagnosing the live WAN
    cluster meant driving it by hand from a PowerShell prompt, counting SSE chunks as a
    proxy for tokens, and timing the whole thing with a stopwatch — and every one of those
    attempts failed for reasons that had nothing to do with throughput (see the timeouts
    fixed alongside this module). An engine that cannot say how fast it is cannot be
    tuned, and a contributor cannot tell a node that is pulling its weight from one that
    is silently useless.

WHY BOTH ENDS
    "It is slow" has at least three distinct causes and they are not distinguishable from
    one side. The Coordinator can say how long generation took; only the node can say what
    its link to the Coordinator is actually worth. Measuring inference alone would leave
    the operator guessing whether a bad number means a weak GPU, a saturated uplink, or a
    model that should never have been sharded in the first place. So:

    * :func:`run_inference_benchmark` runs on the Coordinator, against ``llama-server`` on
      loopback, and reads llama.cpp's own ``timings`` block rather than inferring anything.
    * :func:`measure_link` runs on a node, against the Coordinator, and reports round-trip
      time and throughput in both directions.

    Both append to the same newline-delimited JSON file. That is deliberate and it is the
    point: an operator cannot be connected remotely and watching the Coordinator's console
    at the same moment, so a measurement taken while remote has to still be there to read
    afterwards.
"""

import ipaddress
import json
import os
import socket
import statistics
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from engine.config import get_base_dir
from engine.logger import log_coordinator, log_error, log_warning
from engine.wireguard import WG_SUBNET_CIDR

# The prompt every benchmark sends unless a caller supplies one. Fixed on purpose: two runs
# are only comparable if they asked for the same work, and an operator comparing a "before"
# against an "after" should not have to remember what they typed the first time. Long enough
# that prompt processing is measurable, ordinary enough that no model refuses it.
DEFAULT_PROMPT = (
    "Explain, in clear technical prose, how a river forms from rainfall to river mouth. "
    "Cover catchment, erosion, tributaries, and deposition."
)

# Enough tokens for the generation rate to settle past the first-token cost, few enough that
# a benchmark on a slow WAN-sharded pipeline still finishes in a reasonable time.
DEFAULT_MAX_TOKENS = 128

# A benchmark that has to load a model first is the ordinary case, not a failure, so this is
# deliberately far longer than the budgets on the serving path.
BENCHMARK_TIMEOUT = httpx.Timeout(1800.0, connect=10.0)

# Serialises appends to the results file. Benchmarks can be triggered from the WebUI, the
# admin API and the console at once, and a torn line in a JSONL file is unreadable forever.
_APPEND_LOCK = threading.Lock()

# Caps the payload a link probe may ask for, in both directions. The probe endpoint is
# reachable by any contributing node, and without a ceiling "measure my downlink" is an
# arbitrary-size allocation request aimed at the Coordinator.
MAX_PROBE_BYTES = 32 * 1024 * 1024
DEFAULT_PROBE_BYTES = 4 * 1024 * 1024


# --------------------------------------------------------------------------------------
# Results file
# --------------------------------------------------------------------------------------

def benchmarks_path() -> str:
    """Absolute path of the newline-delimited JSON file every result is appended to."""
    return os.path.join(get_base_dir(), "logs", "benchmarks.jsonl")


def append_record(record: Dict[str, Any]) -> bool:
    """
    Append one result to ``logs/benchmarks.jsonl``.

    Append-only and one object per line, rather than a JSON document that has to be read,
    parsed, extended and rewritten: results are written from several places and read long
    after the run that produced them, and a whole-file rewrite is the one shape that can
    lose every previous measurement when it fails halfway.

    Never raises. A benchmark whose number was measured correctly but could not be filed is
    still a successful benchmark, and losing it must not turn into an exception on a caller
    that was only asking how fast the cluster is.

    Returns:
        True when the line reached disk.
    """
    try:
        path = benchmarks_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(record, separators=(",", ":"), default=str)
        with _APPEND_LOCK:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return True
    except Exception as exc:
        log_warning(f"Could not record the benchmark result: {exc}")
        return False


def read_records(limit: int = 50) -> List[Dict[str, Any]]:
    """
    The most recent results, newest last, for the WebUI's table and the console.

    Skips lines that do not parse instead of failing: a file appended to by several
    processes over months of running is exactly where one torn line eventually appears, and
    it must not cost the operator every other measurement in the file.
    """
    path = benchmarks_path()
    if not os.path.isfile(path):
        return []

    records: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    records.append(parsed)
    except OSError as exc:
        log_warning(f"Could not read {path}: {exc}")
        return []

    return records[-limit:] if limit > 0 else records


# --------------------------------------------------------------------------------------
# Endpoint classification
# --------------------------------------------------------------------------------------

def classify_endpoint(endpoint: str) -> str:
    """
    Where an RPC endpoint sits relative to the Coordinator: local, lan, wan or unknown.

    This is what lets a benchmark result explain itself. "42 tokens/sec" means nothing on
    its own; "42 tokens/sec across one local and one wan backend" is a diagnosis. The same
    classification drives how the shard planner spends its budget, so both agree on what a
    given address costs by construction rather than by coincidence.

    A tunnel address is ``wan`` even though it is numerically private: the whole purpose of
    the tunnel subnet is to carry traffic between machines that are not on one network, and
    calling it ``lan`` because it starts with 10 would defeat the point of asking.

    Args:
        endpoint: ``"host:port"``, or a bare host.

    Returns:
        One of ``"local"``, ``"lan"``, ``"wan"``, ``"unknown"``.
    """
    host = (endpoint or "").strip()
    if not host:
        return "unknown"

    # An IPv6 literal is full of colons, so "everything before the last colon" is only the
    # host when the address is bracketed or when there is exactly one colon to begin with.
    # Splitting unconditionally turned "::1" into "::", which is not an address at all.
    if host.startswith("["):
        host = host[1:].partition("]")[0]
    elif host.count(":") == 1:
        candidate, _, tail = host.partition(":")
        if tail.isdigit() and candidate:
            host = candidate

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "unknown"

    if address.is_loopback:
        return "local"

    try:
        if address in ipaddress.ip_network(WG_SUBNET_CIDR):
            return "wan"
    except ValueError:
        pass

    if address.is_private or address.is_link_local:
        return "lan"

    return "wan"


def summarise_endpoints(endpoints: Optional[List[str]]) -> Dict[str, Any]:
    """
    A compact description of the pipeline a measurement was taken on.

    Recorded beside every inference result because the endpoint list is the single most
    important thing to know when comparing two numbers — a run that shows half the
    throughput of another is uninteresting until you know one of them crossed the internet.
    """
    listed = list(endpoints or [])
    classes = [classify_endpoint(item) for item in listed]
    return {
        "endpoints": listed,
        "classes": classes,
        "local": classes.count("local"),
        "lan": classes.count("lan"),
        "wan": classes.count("wan"),
        "sharded": len(listed) > 1,
        "crosses_wan": "wan" in classes,
    }


# --------------------------------------------------------------------------------------
# Coordinator side: how fast does this cluster actually generate?
# --------------------------------------------------------------------------------------

async def run_inference_benchmark(manager, model_path: str,
                                  prompt: str = DEFAULT_PROMPT,
                                  max_tokens: int = DEFAULT_MAX_TOKENS,
                                  source: str = "api") -> Dict[str, Any]:
    """
    Time one generation against the running engine and record what llama.cpp reports.

    Goes straight to ``llama-server`` on loopback rather than through the Coordinator's own
    chat endpoint. That is the whole reason this is trustworthy: it measures the engine,
    not the proxy, the queue, the context truncation or the portal, and it cannot be
    defeated by the timeouts on the serving path.

    llama.cpp returns a ``timings`` object on every completed non-streamed response —
    prompt tokens and milliseconds, predicted tokens and milliseconds, and the derived
    rates — plus ``draft_n``/``draft_n_accepted`` when speculative decoding is active. Those
    are read rather than inferred; counting streamed chunks and dividing by a stopwatch is
    an approximation, and an approximation is not worth building a tuning decision on.

    Args:
        manager: the :class:`InferenceManager` that owns the running engine.
        model_path: the model to measure, as the catalogue names it.
        prompt: overridden only when a caller wants to measure a specific workload.
        max_tokens: generation length.
        source: free text recorded with the result, so a run triggered from the WebUI can
            be told apart from one a script fired.

    Returns:
        ``{"ok": bool, "detail": str, ...}``. Never raises: a benchmark that fails must
        report why, because "the measurement itself broke" is a different fact from "the
        cluster is slow" and the caller has to be able to tell them apart.
    """
    snapshot = await manager.asnapshot_model(model_path)
    if snapshot is None:
        return {
            "ok": False,
            "detail": (
                f"'{os.path.basename(model_path)}' is not running. Select it and send one "
                "message first, then benchmark it — this measures a loaded engine and "
                "deliberately does not start one, so that the load time is not silently "
                "folded into the generation rate."
            ),
        }

    port = snapshot.get("port")
    if not port:
        return {"ok": False, "detail": "The running engine did not report a port."}

    payload = {
        "model": os.path.basename(model_path),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(max_tokens),
        "stream": False,
        # Sampling pinned so two runs measure the same work. Left at a model's defaults, a
        # benchmark's token count varies with whatever the last caller happened to set.
        "temperature": 0.0,
    }

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=BENCHMARK_TIMEOUT) as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/v1/chat/completions", json=payload
            )
    except Exception as exc:
        return {
            "ok": False,
            "detail": f"The engine did not complete the benchmark request: {exc}",
        }
    elapsed = time.perf_counter() - started

    if response.status_code != 200:
        # 503 here is the ordinary "still loading" answer and deserves saying so plainly
        # rather than being reported as a benchmark failure.
        if response.status_code == 503:
            return {
                "ok": False,
                "detail": ("The engine is still loading this model. Benchmark it once it "
                           "has answered a message normally."),
            }
        return {
            "ok": False,
            "detail": f"The engine answered HTTP {response.status_code}.",
        }

    try:
        body = response.json()
    except ValueError:
        return {"ok": False, "detail": "The engine's response was not JSON."}

    timings = body.get("timings") or {}
    usage = body.get("usage") or {}

    record: Dict[str, Any] = {
        "kind": "inference",
        "source": source,
        "recorded_at": time.time(),
        "model": os.path.basename(model_path),
        "wall_seconds": round(elapsed, 3),
        "max_tokens": int(max_tokens),
        # llama.cpp's own numbers, carried through unchanged so a reader can trust them.
        "prompt_tokens": timings.get("prompt_n", usage.get("prompt_tokens")),
        "prompt_ms": timings.get("prompt_ms"),
        "prompt_tokens_per_second": timings.get("prompt_per_second"),
        "predicted_tokens": timings.get("predicted_n", usage.get("completion_tokens")),
        "predicted_ms": timings.get("predicted_ms"),
        "tokens_per_second": timings.get("predicted_per_second"),
        "cached_tokens": timings.get("cache_n"),
        # Present only when a draft model is attached. Their ratio is the acceptance rate,
        # which is the number that says whether speculative decoding is earning its place.
        "draft_tokens": timings.get("draft_n"),
        "draft_tokens_accepted": timings.get("draft_n_accepted"),
    }
    record.update(summarise_endpoints(snapshot.get("endpoints")))

    if record["draft_tokens"]:
        try:
            record["draft_acceptance"] = round(
                float(record["draft_tokens_accepted"]) / float(record["draft_tokens"]), 3
            )
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # Prompt processing is the first-token cost, and on a sharded pipeline it is usually
    # where a WAN hop actually shows up. Naming it explicitly saves every reader the
    # arithmetic.
    if isinstance(record.get("prompt_ms"), (int, float)):
        record["time_to_first_token_ms"] = round(float(record["prompt_ms"]), 1)

    append_record(record)
    log_coordinator(
        f"Benchmark: {record['model']} generated "
        f"{record.get('predicted_tokens')} tokens at "
        f"{record.get('tokens_per_second')} tok/s across {len(record['endpoints'])} "
        f"backend(s) ({record['local']} local, {record['lan']} lan, {record['wan']} wan)."
    )

    record["ok"] = True
    record["detail"] = "Benchmark complete."
    return record


# --------------------------------------------------------------------------------------
# Node side: what is this node's link to the Coordinator actually worth?
# --------------------------------------------------------------------------------------

def measure_rtt_ms(host: str, port: int, samples: int = 7,
                   timeout: float = 5.0) -> Dict[str, Any]:
    """
    Round-trip time to the Coordinator, measured by timing TCP connects.

    A connect is one round trip by definition — SYN out, SYN-ACK back — with no HTTP
    parsing, no server-side work and no framing to confuse the number. That makes it the
    cleanest RTT available without raw sockets or an ICMP privilege DLNE does not have.

    The *minimum* is the headline figure rather than the mean: RTT has a hard floor set by
    physics and routing, and every sample above it is queueing. The minimum is the property
    of the path; the median alongside it says how consistent that path is.

    Returns a dict with ``ok`` false rather than raising, because a node that cannot reach
    the Coordinator right now is a normal state during a network change, not an error.
    """
    timings: List[float] = []
    last_error = ""
    for _ in range(max(1, samples)):
        started = time.perf_counter()
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                timings.append((time.perf_counter() - started) * 1000.0)
        except Exception as exc:
            last_error = str(exc)
        # No sleep between samples: back-to-back connects are what expose a path that only
        # looks fast when it is idle.

    if not timings:
        return {"ok": False, "detail": f"No connection to {host}:{port} ({last_error})."}

    return {
        "ok": True,
        "rtt_min_ms": round(min(timings), 2),
        "rtt_median_ms": round(statistics.median(timings), 2),
        "rtt_max_ms": round(max(timings), 2),
        "rtt_samples": len(timings),
        "rtt_lost": max(0, samples - len(timings)),
    }


def measure_throughput(base_url: str, token: str = "",
                       payload_bytes: int = DEFAULT_PROBE_BYTES,
                       timeout: float = 120.0) -> Dict[str, Any]:
    """
    Upstream and downstream throughput between this node and the Coordinator.

    Uses the Coordinator's ``/v1/net/probe`` endpoint, which exists for exactly this and
    does nothing else: POST a payload to measure the direction that matters most for
    sharding (the node's *downlink* is what a weight transfer has to fill), GET one back to
    measure the other.

    Both directions are reported because they are routinely asymmetric on the connections
    contributors actually have, and a node that can receive a model quickly but not return
    activations is a different problem from one that can do neither.
    """
    payload_bytes = max(1, min(int(payload_bytes), MAX_PROBE_BYTES))
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    result: Dict[str, Any] = {"ok": False, "probe_bytes": payload_bytes}

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            blob = b"\0" * payload_bytes

            started = time.perf_counter()
            up = client.post(f"{base_url}/v1/net/probe", content=blob, headers=headers)
            up_elapsed = time.perf_counter() - started
            if up.status_code != 200:
                result["detail"] = f"Upstream probe answered HTTP {up.status_code}."
                return result

            started = time.perf_counter()
            down = client.get(
                f"{base_url}/v1/net/probe", params={"bytes": payload_bytes}, headers=headers
            )
            down_elapsed = time.perf_counter() - started
            if down.status_code != 200:
                result["detail"] = f"Downstream probe answered HTTP {down.status_code}."
                return result
            received = len(down.content)
    except Exception as exc:
        result["detail"] = f"Throughput probe failed: {exc}"
        return result

    # Mbit/s, because that is the unit every contributor's connection is sold in and the
    # one they can sanity-check against what they pay for.
    result.update({
        "ok": True,
        "upload_mbps": round((payload_bytes * 8) / up_elapsed / 1_000_000, 2) if up_elapsed else None,
        "download_mbps": round((received * 8) / down_elapsed / 1_000_000, 2) if down_elapsed else None,
        "upload_seconds": round(up_elapsed, 3),
        "download_seconds": round(down_elapsed, 3),
        "detail": "Throughput measured.",
    })
    return result


def measure_link(host: str, port: int, token: str = "",
                 payload_bytes: int = DEFAULT_PROBE_BYTES,
                 source: str = "node") -> Dict[str, Any]:
    """
    The node's half of the measurement: RTT plus throughput, recorded locally.

    Called from the node, about the Coordinator, and filed on the node — so a contributor
    can answer "is my connection the problem?" without any access to the Coordinator at
    all, and so the answer survives them changing networks afterwards.

    RTT is reported even when the throughput probe fails. They fail for different reasons
    (an old Coordinator with no probe endpoint still answers TCP perfectly well), and the
    latency number is the one that governs how fast a sharded pipeline can possibly run.
    """
    record: Dict[str, Any] = {
        "kind": "link",
        "source": source,
        "recorded_at": time.time(),
        "coordinator": f"{host}:{port}",
        "coordinator_class": classify_endpoint(f"{host}:{port}"),
    }

    rtt = measure_rtt_ms(host, port)
    record.update({key: value for key, value in rtt.items() if key != "detail"})
    if not rtt.get("ok"):
        record["detail"] = rtt.get("detail", "")
        append_record(record)
        return record

    throughput = measure_throughput(
        f"http://{host}:{port}", token=token, payload_bytes=payload_bytes
    )
    record["throughput_ok"] = throughput.get("ok", False)
    for key in ("upload_mbps", "download_mbps", "upload_seconds",
                "download_seconds", "probe_bytes"):
        if key in throughput:
            record[key] = throughput[key]
    if not throughput.get("ok"):
        record["throughput_detail"] = throughput.get("detail", "")

    # The ceiling a synchronous per-token round trip imposes, stated outright. This is the
    # number that decides whether sharding across this node can ever feel responsive, and
    # leaving the reader to derive it from an RTT is how it gets ignored.
    if record.get("rtt_min_ms"):
        record["token_ceiling_per_second"] = round(1000.0 / float(record["rtt_min_ms"]), 1)

    record["ok"] = True
    append_record(record)
    return record
