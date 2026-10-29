"""
What the worker prints for a front end to read (engine/worker.py, engine/companion.py): the job the swarm gave this
machine, whether the CPU also serves the phone app, the coordinator's refusal, and the clock notice. The same
parsing as vscode-extension/src/worker/contribution.ts, line for line.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

ROLE_MARKER = "DLNE_ROLE: "
ALSO_MARKER = "DLNE_ALSO: "
REFUSAL_MARKER = "DLNE_REFUSAL: "
# Printed by engine/downloader.py when a Windows engine exits 0xC0000135 for want of the Visual C++ Runtime.
VCREDIST_MARKER = "DLNE_NEEDS: vcredist"
VCREDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
VCREDIST_TEXT = ("The Visual C++ Runtime is missing from this PC, so the DLNE engine cannot start. Install "
                 f"\"Microsoft Visual C++ Redistributable (x64)\" from {VCREDIST_URL} and start DLNE Donate again.")
CLOCK_MARKER = "DLNE_CLOCK: "
# 2.5.0: whether the machine is docked (wired + mains) and whether it is stepping aside for its owner's own load.
DOCK_MARKER = "DLNE_DOCK: "
YIELD_MARKER = "DLNE_YIELD: "

# The worker's exit codes (engine/worker.py).
VERSION_MISMATCH_EXIT_CODE = 75
CPU_BELOW_MINIMUM_EXIT_CODE = 78

ROLE_LABEL = {"rpc": "GPU pool", "coding": "Coding member", "app": "Phone app member"}

_ROLE_RE = re.compile(r"^(rpc|coding|app)\b\s*(\{.*\})?\s*$")
_TRUST_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class Role:
    role: str
    model: str = ""
    tier: str = ""
    slots: int = 0
    trust_id: str = ""
    context: int = 0            # tokens each request gets on this member (2.5.0)
    decode_tps: float = 0.0     # the member's own join-test speeds (2.5.0)
    prefill_tps: float = 0.0


@dataclass(frozen=True)
class Dock:
    """What the worker read about this machine being docked (2.5.0): "" / None when it could not say."""
    link: str = ""              # wired, wifi or cellular
    power: str = ""             # ac or battery
    docked: Optional[bool] = None


@dataclass(frozen=True)
class Yield:
    """Whether the machine is stepping aside for its owner's own load (2.5.0), and why."""
    yielding: bool = False
    why: str = ""               # fullscreen, gpu or cpu


def take_complete_lines(pending: str, chunk: str) -> Tuple[str, str]:
    """
    Split worker output into the lines that are complete and the unfinished rest. A pipe may cut a line anywhere, and
    a Windows line ends in CRLF, which stays with its line (the parsers strip it). The rest is capped so a worker that
    never prints a newline cannot grow it without bound.
    """
    text = pending + chunk
    end = text.rfind("\n")
    if end == -1:
        return "", text[-65536:]
    return text[: end + 1], text[end + 1:][-65536:]


def parse_refusal(output: str) -> Optional[str]:
    """The coordinator's reason for refusing this machine, one bounded line, or None."""
    for line in output.splitlines():
        at = line.find(REFUSAL_MARKER)
        if at != -1:
            reason = line[at + len(REFUSAL_MARKER):].strip()
            return reason[:400] if reason else None
    return None


def parse_role(output: str) -> Optional[Role]:
    """The last job the worker announced in ``output``. Anything malformed is ignored."""
    found: Optional[Role] = None
    for line in output.splitlines():
        at = line.find(ROLE_MARKER)
        if at == -1:
            continue
        match = _ROLE_RE.match(line[at + len(ROLE_MARKER):].strip())
        if not match:
            continue
        role = Role(role=match.group(1))
        if match.group(2):
            try:
                detail = json.loads(match.group(2))
            except ValueError:
                detail = None
            if isinstance(detail, dict):
                if isinstance(detail.get("model"), str) and len(detail["model"]) <= 128:
                    role.model = detail["model"]
                if isinstance(detail.get("tier"), str) and len(detail["tier"]) <= 128:
                    role.tier = detail["tier"]
                slots = detail.get("slots")
                if isinstance(slots, int) and not isinstance(slots, bool) and 1 <= slots <= 64:
                    role.slots = slots
                trust = detail.get("trust_id")
                if isinstance(trust, str) and _TRUST_ID_RE.match(trust):
                    role.trust_id = trust
                context = detail.get("context")
                if isinstance(context, int) and not isinstance(context, bool) and 1024 <= context <= 2 ** 21:
                    role.context = context
                for key in ("decode_tps", "prefill_tps"):
                    value = detail.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 100000:
                        setattr(role, key, round(float(value), 1))
        found = role
    return found


def parse_dock(output: str) -> Optional[Dock]:
    """The last docked facts the worker printed (DLNE_DOCK), or None. Anything malformed is ignored."""
    found: Optional[Dock] = None
    for line in output.splitlines():
        at = line.find(DOCK_MARKER)
        if at == -1:
            continue
        try:
            facts = json.loads(line[at + len(DOCK_MARKER):].strip())
        except ValueError:
            continue
        if not isinstance(facts, dict):
            continue
        link = facts.get("link") if facts.get("link") in ("wired", "wifi", "cellular") else ""
        power = facts.get("power") if facts.get("power") in ("ac", "battery") else ""
        docked = facts.get("docked") if isinstance(facts.get("docked"), bool) else None
        found = Dock(link=link, power=power, docked=docked)
    return found


def parse_yield(output: str) -> Optional[Yield]:
    """The last yield state the worker printed (DLNE_YIELD), or None. Anything malformed is ignored."""
    found: Optional[Yield] = None
    for line in output.splitlines():
        at = line.find(YIELD_MARKER)
        if at == -1:
            continue
        try:
            facts = json.loads(line[at + len(YIELD_MARKER):].strip())
        except ValueError:
            continue
        if not isinstance(facts, dict) or not isinstance(facts.get("yielding"), bool):
            continue
        why = facts.get("why") if facts["yielding"] and facts.get("why") in ("fullscreen", "gpu", "cpu") else ""
        found = Yield(yielding=facts["yielding"], why=why)
    return found


def dock_line(dock: Optional[Dock]) -> str:
    """The Settings window's docked line (the extension's dockLine, word for word), "" when unknown."""
    if dock is None or dock.docked is None:
        return ""
    if dock.docked:
        return "Docked (wired and plugged in): your GPU may share the swarm's big model while it stays docked."
    on = " and ".join(part for part in ("Wi-Fi" if dock.link == "wifi" else "a phone link" if dock.link == "cellular" else "",
                                         "battery" if dock.power == "battery" else "") if part)
    return f"Not docked{f' (on {on})' if on else ''}: your GPU runs a whole model by itself until it is docked again."


def yield_line(state: Optional[Yield]) -> str:
    """The stepping-aside line (the extension's yieldLine), "" while the machine is not yielding."""
    if state is None or not state.yielding:
        return ""
    what = {"fullscreen": "a full-screen app", "gpu": "your own GPU use"}.get(state.why, "your own CPU use")
    return f"Stepping aside for {what}: the swarm gets the GPU back once you are done."


def parse_also(output: str) -> Optional[bool]:
    """Whether the CPU serves the phone pool next to the GPU's own job, from the last DLNE_ALSO line; None if none."""
    found: Optional[bool] = None
    for line in output.splitlines():
        at = line.find(ALSO_MARKER)
        if at == -1:
            continue
        word = line[at + len(ALSO_MARKER):].strip()
        if word == "app":
            found = True
        elif word == "none":
            found = False
    return found


def parse_clock(output: str) -> Optional[int]:
    """How many seconds this computer's clock is off (positive: behind), from the last DLNE_CLOCK line; None if none."""
    found: Optional[int] = None
    for line in output.splitlines():
        at = line.find(CLOCK_MARKER)
        if at == -1:
            continue
        try:
            detail = json.loads(line[at + len(CLOCK_MARKER):].strip())
        except ValueError:
            continue
        offset = detail.get("offset_seconds") if isinstance(detail, dict) else None
        if isinstance(offset, (int, float)) and not isinstance(offset, bool) and abs(offset) < 10 * 365 * 86400:
            found = int(round(offset))
    return found


@dataclass(frozen=True)
class Activity:
    """What the model download or server is doing, from the worker's log lines (not markers: a worker that words them
    differently simply shows no activity). ``fraction`` is set while a download runs."""
    text: str
    fraction: Optional[float] = None


_LOG_PREFIX = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?(?:INFO|WARNING|ERROR|DEBUG)?\s*")
_DOWNLOADING = re.compile(r"Downloading (?P<name>.+?) for the (?:coding|CPU) pool \((?P<size>[\d.]+) GB, once\)")
_PERCENT = re.compile(r"^(?P<name>[^:]{3,80}): (?P<pct>\d{1,3})%\s*$")
_VERIFIED = re.compile(r"^(?P<name>.+?) downloaded and verified\.")
_SERVING = re.compile(r"^Serving (?P<name>.+?) (?:to the coding pool|for the phone app)\.")


def parse_activity(output: str) -> Optional[Activity]:
    """The last model activity the worker logged in ``output``, or None."""
    found: Optional[Activity] = None
    for line in output.splitlines():
        body = _LOG_PREFIX.sub("", line, count=1).strip()
        match = _DOWNLOADING.search(body)
        if match:
            found = Activity(f"Downloading {match['name']} ({match['size']} GB, once)", 0.0)
            continue
        match = _PERCENT.match(body)
        if match and int(match["pct"]) <= 100:
            found = Activity(f"Downloading {match['name'].strip()}: {match['pct']}%", int(match["pct"]) / 100)
            continue
        match = _VERIFIED.match(body)
        if match:
            found = Activity(f"{match['name']} downloaded and checked. Starting it...", None)
            continue
        match = _SERVING.match(body)
        if match:
            found = Activity(f"Serving {match['name']}", None)
    return found


def role_title(role: Role) -> str:
    """'Coding member . Qwen3.6 35B-A3B' for the tooltip and the Settings window."""
    label = ROLE_LABEL.get(role.role, role.role)
    model = role.model.rsplit(".gguf", 1)[0] if role.model.lower().endswith(".gguf") else role.model
    return f"{label} · {model}" if model else label


def role_meaning(role: Role) -> str:
    """One sentence on what the job is, as the extension words it."""
    if role.role == "rpc":
        return "Your GPU holds a share of one big model that the swarm runs together on many volunteers' GPUs."
    if role.role == "coding":
        at = f", about {role.slots} at a time" if role.slots else ""
        parts = [p for p in (f"{role.context:,} tokens each" if role.context else "",
                             f"{role.decode_tps:g} tok/s" if role.decode_tps else "") if p]
        facts = f" ({', '.join(parts)})" if parts else ""
        return f"Your GPU runs a whole coding model by itself and answers other people's coding requests{at}{facts}."
    return "Your computer runs a small model that answers questions in the DLNE phone app."


def clock_notice(offset_seconds: int) -> str:
    """What to tell the owner about a wrong clock (the worker already corrects for it)."""
    amount = abs(offset_seconds)
    if amount >= 3600:
        text = f"{amount / 3600:.1f} hours"
    elif amount >= 90:
        text = f"{round(amount / 60)} minutes"
    else:
        text = f"{amount} seconds"
    way = "behind" if offset_seconds > 0 else "ahead"
    return (f"This computer's clock is about {text} {way}. DLNE corrects for it, "
            "but you may want to turn on automatic time in your system settings.")


@dataclass
class OutputReader:
    """Feeds worker output in, keeps what it says. One per worker process."""
    role: Optional[Role] = None
    also_cpu: bool = False
    refusal: Optional[str] = None
    clock_offset: Optional[int] = None
    dock: Optional[Dock] = None
    yielding: Optional[Yield] = None
    activity: Optional[Activity] = None
    heartbeat_ok: bool = False
    needs_vcredist: bool = False
    _tail: Dict[str, str] = field(default_factory=lambda: {"stdout": "", "stderr": ""})

    def feed(self, chunk: str, stream: str = "stdout") -> Dict[str, Any]:
        """Returns what changed, as ``{"role": Role}`` / ``{"also": bool}`` / ``{"refusal": str}`` / ``{"clock": int}`` /
        ``{"dock": Dock}`` / ``{"yield": Yield}``."""
        complete, rest = take_complete_lines(self._tail.get(stream, ""), chunk)
        self._tail[stream] = rest
        changed: Dict[str, Any] = {}
        if not complete:
            return changed
        if VCREDIST_MARKER in complete and not self.needs_vcredist:
            self.needs_vcredist = True
            changed["vcredist"] = True
        refusal = parse_refusal(complete)
        if refusal and refusal != self.refusal:
            self.refusal = refusal
            changed["refusal"] = refusal
        role = parse_role(complete)
        if role and role != self.role:
            self.role = role
            changed["role"] = role
        also = parse_also(complete)
        if also is not None and also != self.also_cpu:
            self.also_cpu = also
            changed["also"] = also
        clock = parse_clock(complete)
        if clock is not None and clock != self.clock_offset:
            self.clock_offset = clock
            changed["clock"] = clock
        dock = parse_dock(complete)
        if dock is not None and dock != self.dock:
            self.dock = dock
            changed["dock"] = dock
        yielding = parse_yield(complete)
        if yielding is not None and yielding != self.yielding:
            self.yielding = yielding
            changed["yield"] = yielding
        activity = parse_activity(complete)
        if activity and activity != self.activity:
            self.activity = activity
            changed["activity"] = activity
        # The coordinator accepted a heartbeat (engine/worker.py heartbeat_loop logs "Heartbeat OK | Status: ..." after
        # raise_for_status). Neither the DLNE_ROLE line (printed before the first heartbeat is even sent) nor "RPC Server
        # started" (the local engine is up) says the swarm has taken this computer, and "Heartbeat ACK" is the
        # coordinator's own log line, which a worker never prints.
        if "Heartbeat OK" in complete and not self.heartbeat_ok:
            self.heartbeat_ok = True
            changed["heartbeat"] = True
        return changed
