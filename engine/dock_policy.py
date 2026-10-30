"""
Whether a GPU may carry a shard of the shared model, from how it is connected and powered (2.6.4).

THE RULE (owner, 2026-10-29)
    mains + wired      docked, as since 2.5.0;
    mains + Wi-Fi      docked only while the link passes its speed test (below);
    battery            never: a laptop on battery can sleep or be unplugged at any moment, and a
                       shard that vanishes breaks the engine for everyone;
    a phone link       never.
    A machine that could not read a fact is judged as before 2.5.0 would have: by the ``docked``
    it reports, and one that says nothing is not held back.

    Before 2.6.4 "docked" meant wired and on mains, so a GPU on Wi-Fi never shared the model
    however good its link was: one with 8 GB or more ran a whole model as a coding member, and a
    smaller one had no job at all.

WHY THE TEST IS ABOUT JITTER MORE THAN SPEED
    A shard exchanges a few KB of activations per token, and it does so on every token, so what
    hurts a pipeline is the round trip and above all its spikes: Wi-Fi's occasional 50-100 ms
    stall holds up every token waiting on that GPU. The test therefore times WIFI_PROBE_SAMPLES
    round trips along the path the engine uses and judges their median (the near limit,
    AUTO_SWAP_MAX_RTT_MS), their spread (the 90th percentile above the median,
    WIFI_MAX_JITTER_MS) and any lost sample. Download speed matters once, for the weights the
    coordinator sends when the shard loads (a few GB, cached on the node afterwards), so it is
    the node's own figure, from /v1/net/probe, at least WIFI_MIN_DOWNLOAD_MBPS.

WHY A NODE'S OWN SPEED FIGURE IS GOOD ENOUGH HERE
    Elsewhere a node's self-report never makes it near (engine/link_paths.py). The docked facts
    can only hold a GPU back, never put it forward: whether it is near enough to share the model
    is still this coordinator's own round-trip measurement, and a dishonest worker could already
    claim to be wired. A false download figure buys nothing the false claim did not.
"""

import math
from typing import Any, Dict, List, Optional, Sequence

# The defaults behind WIFI_SHARD, WIFI_MAX_JITTER_MS, WIFI_MIN_DOWNLOAD_MBPS and WIFI_PROBE_SAMPLES
# (engine/config.py reads them from config.txt).
WIFI_DEFAULTS: Dict[str, Any] = {
    "wifi_shard": True,
    "wifi_max_jitter_ms": 20.0,
    "wifi_min_download_mbps": 100.0,
    "wifi_probe_samples": 20,
}

# A sample lost is a stall the engine would have waited out, so none may be.
WIFI_MAX_LOST = 0

# How long a node's download figure stands. The coordinator asks for a new one when it is older (the
# heartbeat answer's ``speed_test``), and a node measures at most every WIFI_SPEED_TEST_MIN_GAP_SECONDS.
WIFI_DOWNLOAD_MAX_AGE_SECONDS = 2 * 3600.0
WIFI_SPEED_TEST_MIN_GAP_SECONDS = 600.0

# What a node downloads for its speed test: big enough to get past TCP's slow start on a 100 Mbit/s
# link (about 1.3 s there), small enough to cost nothing worth asking about.
WIFI_SPEED_TEST_BYTES = 16 * 1024 * 1024


# The verdict's ``why`` before a test has run, and while the round trips passed but the node's download figure
# has not arrived: neither is settled, and a GPU is given no new job for a short while (coordinator, the role
# answer) rather than sent off to load a whole model it would leave again a minute later.
NOT_TESTED = "the link test has not run yet"
WAITING_FOR_DOWNLOAD = "waiting for this machine's download test"


def verdict_settled(check: Any) -> bool:
    """Whether a Wi-Fi verdict is final for now: it passed, or it failed for a reason the link itself gave."""
    return isinstance(check, dict) and check.get("ok") is not None and check.get("why") != WAITING_FOR_DOWNLOAD


def on_wifi_and_mains(hardware: Any) -> bool:
    """A machine that says it is on Wi-Fi and on mains power: the one case the speed test decides."""
    return isinstance(hardware, dict) and hardware.get("link") == "wifi" and hardware.get("power") == "ac"


def effective_docked(hardware: Any, wifi_passed: bool) -> Optional[bool]:
    """
    Whether this machine counts as docked for sharding: True, False, or None when it could not say
    (judged as docked, as before 2.5.0). ``wifi_passed`` is this coordinator's verdict on its Wi-Fi
    link (registry.wifi_passed), used only for a machine on Wi-Fi and mains.
    """
    if not isinstance(hardware, dict):
        return None
    if hardware.get("power") == "battery":
        return False
    if on_wifi_and_mains(hardware):
        return bool(wifi_passed)
    docked = hardware.get("docked")
    return docked if isinstance(docked, bool) else None


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """The nearest-rank percentile of an already sorted, non-empty list."""
    rank = max(1, math.ceil(fraction * len(sorted_values)))
    return sorted_values[rank - 1]


def fresh_download(measurement: Any, now: float) -> Optional[float]:
    """The node's download figure (Mbit/s, registry.get_link_measurement) when it is fresh enough to judge by, else None."""
    if not isinstance(measurement, dict):
        return None
    value, at = measurement.get("download_mbps"), measurement.get("reported_at")
    if not isinstance(value, (int, float)) or not isinstance(at, (int, float)) or isinstance(value, bool):
        return None
    if not math.isfinite(value) or now - at > WIFI_DOWNLOAD_MAX_AGE_SECONDS:
        return None
    return float(value)


def judge_wifi(samples: Sequence[Optional[float]], measurement: Any, *, max_rtt_ms: float,
               limits: Dict[str, Any], now: float) -> Dict[str, Any]:
    """
    The verdict on one Wi-Fi link: ``{"ok", "why", "rtt_median_ms", "jitter_ms", "lost",
    "download_mbps", "checked_at", "samples_ms"}``. ``samples`` are this coordinator's round trips
    along the engine's path (None for one that went unanswered); ``measurement`` is the node's own
    last link report (registry.get_link_measurement). ``why`` says, in a few words a contributor can
    act on, what passed or which limit was missed (the first one, in the order the engine would feel
    it). ``samples_ms`` are kept so a download figure that arrives later is judged at once, against
    the same round trips (coordinator.worker_link_report).
    """
    answered: List[float] = sorted(float(s) for s in samples
                                   if isinstance(s, (int, float)) and not isinstance(s, bool)
                                   and math.isfinite(s) and s >= 0)
    lost = len(samples) - len(answered)
    download = fresh_download(measurement, now)
    verdict: Dict[str, Any] = {"ok": False, "lost": lost, "download_mbps": download, "checked_at": now,
                               "rtt_median_ms": None, "jitter_ms": None, "samples_ms": list(samples)}
    if not answered:
        verdict["why"] = "no round trip answered"
        return verdict
    median = _percentile(answered, 0.5)
    jitter = _percentile(answered, 0.9) - median
    verdict.update(rtt_median_ms=round(median, 1), jitter_ms=round(jitter, 1))
    max_jitter = float(limits.get("wifi_max_jitter_ms", WIFI_DEFAULTS["wifi_max_jitter_ms"]))
    min_download = float(limits.get("wifi_min_download_mbps", WIFI_DEFAULTS["wifi_min_download_mbps"]))
    if lost > WIFI_MAX_LOST:
        verdict["why"] = f"{lost} of {len(samples)} round trips lost"
    elif median > max_rtt_ms:
        verdict["why"] = f"round trip {median:.0f} ms, over {max_rtt_ms:.0f}"
    elif jitter > max_jitter:
        verdict["why"] = f"jitter {jitter:.0f} ms, over {max_jitter:.0f}"
    elif download is None:
        verdict["why"] = WAITING_FOR_DOWNLOAD
    elif download < min_download:
        verdict["why"] = f"download {download:.0f} Mbit/s, under {min_download:.0f}"
    else:
        verdict.update(ok=True, why=f"round trip {median:.0f} ms, jitter {jitter:.0f} ms, download {download:.0f} Mbit/s")
    return verdict
