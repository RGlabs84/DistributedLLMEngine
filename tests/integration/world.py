"""
The world the integration scenarios run in: one real coordinator, one to three real coding members, each with its
own llama-server on the CPU, and the editor's real client, all on loopback.

    editor (node, the compiled extension)
        |  http, through a wire tap                         127.0.0.3
        v
    coordinator  (engine/coordinator.py, its own llama-server, 127.0.0.2)
        |  the reverse tunnel (websocket), then a relay port on 127.0.0.1
        v
    member       (engine/worker.py as a coding member + engine/coding_member.py + llama-server)
                 a wire tap in front of its proxy records what the coordinator relays

Every process runs from its OWN copy of the engine in a folder under the run directory (the engine finds its config,
models and logs from where its files are), with its own fake HOME (the worker writes its heartbeat token under
~/.dlne), and in its own process group so a scenario can kill a member and everything it started. Nothing is
written outside the run directory; nothing listens outside 127.0.0.0/8; llama-server is wrapped to run on the CPU only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from tests.integration import models, taps

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
EXTENSION = REPO / "vscode-extension"

COORDINATOR_HOST = "127.0.0.2"
CLIENT_TAP_HOST = "127.0.0.3"
# The most llama-server threads the whole run may use at once (the laptop's other work comes first).
THREADS_PER_SERVER = 2


# ======================================================================================================
# small helpers
# ======================================================================================================

def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def mem_available_mb() -> int:
    with open("/proc/meminfo", "r", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return -1


def wait_until(predicate: Callable[[], Any], timeout: float, what: str, interval: float = 0.25) -> Any:
    """Poll ``predicate`` until it returns something truthy (returned), or fail with ``what`` after ``timeout`` s."""
    end = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < end:
        try:
            last = predicate()
        except Exception as exc:  # noqa: BLE001 - polled again
            last = exc
        else:
            if last:
                return last
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what} (last: {last!r})")


def write_config(path: Path, values: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={value}" for key, value in values.items()]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)               # an atomic swap: the running coordinator re-reads it when it changes


def read_config(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


class Proc:
    """A child process in its own process group, its output in a log file."""

    def __init__(self, name: str, argv: List[str], cwd: Path, env: Dict[str, str], log: Path) -> None:
        self.name = name
        self.log = log
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self._out = open(log, "wb")
        self.popen = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=self._out,
                                      stderr=subprocess.STDOUT, start_new_session=True)

    @property
    def pid(self) -> int:
        return self.popen.pid

    def alive(self) -> bool:
        return self.popen.poll() is None

    def kill_group(self, sig: int = signal.SIGKILL) -> None:
        """Signal the process and everything it started (a member's llama-server included)."""
        try:
            os.killpg(self.popen.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self, grace: float = 8.0) -> None:
        if self.alive():
            self.kill_group(signal.SIGTERM)
            try:
                self.popen.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        self.kill_group(signal.SIGKILL)         # whatever is left of the group (llama-server children)
        try:
            self.popen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        self._out.close()

    def text(self) -> str:
        try:
            return self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


# ======================================================================================================
# installs
# ======================================================================================================

WRAPPER = """#!/bin/bash
# Integration harness: llama-server on the CPU only. Nothing here reaches a GPU: the devices are hidden from every
# backend, the Vulkan loader is pointed at nothing, and the arguments the engine builds are overridden at the end.
export CUDA_VISIBLE_DEVICES="" GGML_VK_VISIBLE_DEVICES="" HIP_VISIBLE_DEVICES="" ROCR_VISIBLE_DEVICES=""
export VK_ICD_FILENAMES=/nonexistent VK_DRIVER_FILES=/nonexistent
export LD_LIBRARY_PATH="{libs}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"
exec "{real}" "$@" --device none -ngl 0
"""

RPC_STUB = """#!/bin/bash
# Integration harness: the file the worker looks for beside llama-server. It is never run as a server here.
exit 0
"""


def _tier() -> Dict[str, Any]:
    """The one tiny coding tier of this run, in the shape of scripts/coding_models.json."""
    m = models.MODEL
    return {"id": "it-tiny", "name": "Qwen2.5-Coder 0.5B (integration test)", "min_vram_gb": 1, "context": 16384,
            "enabled": True, "file": m["file"], "url": m["url"], "sha256": m["sha256"], "size": m["size"]}


@dataclass
class Resources:
    model: Path
    llama_dir: Path
    llama_version: str


def discover() -> Resources:
    model = models.find_model()
    llama_dir, version = models.find_llama()
    return Resources(model, llama_dir, version)


def make_install(root: Path, name: str, res: Resources, config: Dict[str, Any], launcher: str,
                 with_ladder: bool = False) -> Path:
    """A folder the engine runs from: its own copy of engine/, config, models/, bin/, and the launcher."""
    install = root / name
    if install.exists():
        shutil.rmtree(install)
    install.mkdir(parents=True)
    shutil.copytree(REPO / "engine", install / "engine", ignore=shutil.ignore_patterns("__pycache__", "static"),
                    symlinks=False)
    if (REPO / "engine" / "static").is_dir():
        shutil.copytree(REPO / "engine" / "static", install / "engine" / "static")
    shutil.copy2(HERE / launcher, install / launcher)
    shutil.copy2(HERE / "taps.py", install / "taps.py")
    write_config(install / "config" / "config.txt", config)
    (install / "logs").mkdir()
    (install / "models").mkdir()
    target = install / "models" / res.model.name
    try:
        os.link(res.model, target)                       # same file, no copy (the run directory is on the same disk)
    except OSError:
        shutil.copy2(res.model, target)
    (install / "bin").mkdir()
    wrapper = install / "bin" / "llama-server"
    wrapper.write_text(WRAPPER.format(libs=res.llama_dir, real=res.llama_dir / "llama-server"))
    stub = install / "bin" / "ggml-rpc-server"
    stub.write_text(RPC_STUB)
    for path in (wrapper, stub):
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    if with_ladder:
        (install / "scripts").mkdir()
        (install / "scripts" / "coding_models.json").write_text(json.dumps({"tiers": [_tier()]}, indent=2))
    return install


def child_env(home: Path) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR") or k.startswith("LC_")}
    home.mkdir(parents=True, exist_ok=True)
    env.update({"HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
                "CUDA_VISIBLE_DEVICES": "", "GGML_VK_VISIBLE_DEVICES": "", "NO_PROXY": "*", "no_proxy": "*"})
    return env


# ======================================================================================================
# members
# ======================================================================================================

@dataclass
class Member:
    name: str
    install: Path
    home: Path
    proxy_port: int
    tap_port: int
    trust_id: str
    identity_pub: str
    tamper: Optional[str] = None
    snoop: Optional[str] = None
    proc: Optional[Proc] = None
    worker_id: str = ""

    @property
    def tap_path(self) -> Path:
        return self.install / "member_tap.jsonl"

    def connections(self) -> List[taps.Connection]:
        """What the coordinator relayed to this member, and what the member answered (the far end of the tunnel)."""
        return taps.read_record(str(self.tap_path))

    def completions(self) -> List[Any]:
        """Every chat or completion request the member received (a keep-alive connection can carry several)."""
        from tests.integration import checks
        found = []
        for conn in self.connections():
            for line, headers, body in checks.requests_on(conn):
                if line.startswith("POST /v1/chat/completions") or line.startswith("POST /v1/completions"):
                    found.append((line, headers, body))
        return found

    def snooped(self) -> List[Dict[str, Any]]:
        path = self.install / "snoop.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def log(self) -> str:
        return self.proc.text() if self.proc else ""

    def llama_log(self) -> str:
        path = self.install / "logs" / "coding_server.log"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    def kill(self) -> None:
        """The volunteer's machine goes away: the worker and its llama-server die at once."""
        if self.proc:
            self.proc.kill_group(signal.SIGKILL)

    def llama_pids(self) -> List[int]:
        """The member's llama-server process(es), found in the member's process group."""
        if not self.proc:
            return []
        out = subprocess.run(["pgrep", "-g", str(self.proc.pid), "-f", "llama-server"], capture_output=True, text=True)
        return [int(p) for p in out.stdout.split() if p.isdigit()]

    def kill_engine_only(self) -> None:
        """Only the model server dies (the worker, the proxy and the tunnel stay up)."""
        for pid in self.llama_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# ======================================================================================================
# the client
# ======================================================================================================

class ClientRun:
    """The compiled extension client (client_driver.js) running a list of ops; events are read as they arrive."""

    def __init__(self, world: "World", name: str, ops: List[Dict[str, Any]], *, settings: Optional[Dict[str, Any]] = None,
                 fake_github: Optional[Dict[str, Any]] = None, workspace: Optional[Path] = None,
                 wait_timing: Optional[Dict[str, Any]] = None, busy_give_up_ms: Optional[int] = None,
                 server_url: Optional[str] = None) -> None:
        self.world = world
        self.events: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        run = world.root / "clients" / f"{name}-{secrets.token_hex(3)}"
        run.mkdir(parents=True)
        cfg = {"extensionRoot": str(EXTENSION), "serverUrl": server_url or world.client_url, "home": str(run / "home"),
               "settings": settings or {}, "ops": ops}
        if fake_github:
            cfg["fakeGithub"] = fake_github
        if workspace:
            cfg["workspaceFolder"] = str(workspace)
        if wait_timing:
            cfg["waitTiming"] = wait_timing
        if busy_give_up_ms:
            cfg["busyGiveUpMs"] = busy_give_up_ms
        (run / "home").mkdir()
        (run / "config.json").write_text(json.dumps(cfg))
        env = child_env(run / "home")
        env["PATH"] = os.environ.get("PATH", "")
        self.popen = subprocess.Popen(["node", str(HERE / "client_driver.js"), str(run / "config.json")], cwd=str(run),
                                      env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, start_new_session=True)
        self.stderr_text = ""
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._err_reader = threading.Thread(target=self._read_err, daemon=True)
        self._reader.start()
        self._err_reader.start()

    def _read(self) -> None:
        for line in self.popen.stdout:                      # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                event = {"ev": "garbage", "line": line}
            event["_t"] = time.time()
            with self._lock:
                self.events.append(event)

    def _read_err(self) -> None:
        for line in self.popen.stderr:                      # type: ignore[union-attr]
            self.stderr_text += line

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.events)

    def of(self, ev: str, op: Optional[str] = None) -> List[Dict[str, Any]]:
        return [e for e in self.snapshot() if e.get("ev") == ev and (op is None or e.get("op") == op)]

    def wait_for(self, predicate: Callable[[List[Dict[str, Any]]], Any], timeout: float, what: str) -> Any:
        return wait_until(lambda: predicate(self.snapshot()), timeout, what, interval=0.1)

    def wait(self, timeout: float = 180.0) -> List[Dict[str, Any]]:
        """Until the driver exits; the events."""
        try:
            self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise TimeoutError(f"client did not finish in {timeout:.0f}s; events so far: {self.snapshot()[-6:]}")
        self._reader.join(timeout=5)
        self._err_reader.join(timeout=5)
        return self.snapshot()

    def result(self, op: str) -> Dict[str, Any]:
        """The 'done' or 'error' event of one op."""
        for event in self.snapshot():
            if event.get("op") == op and event.get("ev") in ("done", "error"):
                return event
        raise KeyError(f"no result for op {op!r}: {self.snapshot()[-6:]}")

    def text(self, op: str) -> str:
        return "".join(e.get("text", "") for e in self.of("delta", op))

    def kill(self) -> None:
        try:
            os.killpg(self.popen.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class Holder:
    """A request that keeps a slot taken: a long answer read until ``release()``. ``sealed`` asks for a coding member
    the way the client does (route lookup, then a sealed body with X-DLNE-Route); otherwise it is a plain request.
    These only occupy slots; every assertion about what the client does is made with the real (TypeScript) client."""

    PROMPT = "Count from 1 to 3000, one number per line, and write nothing else."

    def __init__(self, world: "World", *, sealed: bool, privacy: str = "any") -> None:
        self.world = world
        self.sealed = sealed
        self.privacy = privacy
        self.session = secrets.token_hex(8)
        self.started = threading.Event()
        self.stop = threading.Event()
        self.error: Optional[str] = None
        self.bytes_read = 0
        self.status: Optional[int] = None
        self.routed = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "Holder":
        self._thread.start()
        return self

    def _run(self) -> None:
        body = {"messages": [{"role": "user", "content": self.PROMPT}], "stream": True, "max_tokens": 3000,
                "temperature": 0.2, "ignore_eos": True}
        headers = {"X-DLNE-Session": self.session, "X-DLNE-Privacy": self.privacy, "Content-Type": "application/json"}
        content: Any = body
        try:
            if self.sealed:
                from engine import e2e
                offer = self.world.route(self.session, self.privacy)
                if not offer.get("route"):
                    self.error = "no route offered"
                    self.started.set()
                    return
                self.routed = True
                envelope, _key = e2e.seal_request(body, offer["pubkey"])
                headers["X-DLNE-Route"] = offer["route"]
                content = envelope
            with httpx.stream("POST", self.world.coordinator_url + "/v1/chat/completions", json=content, headers=headers,
                              timeout=httpx.Timeout(600, connect=10), trust_env=False) as response:
                self.status = response.status_code
                for chunk in response.iter_bytes():
                    self.bytes_read += len(chunk)
                    self.started.set()
                    if self.stop.is_set():
                        break
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.started.set()

    def release(self) -> None:
        self.stop.set()
        self._thread.join(timeout=15)


# ======================================================================================================
# the world
# ======================================================================================================

class World:
    def __init__(self, root: Path, res: Resources, *, coding_enabled: bool = True, canary_per_day: int = 4,
                 extra_config: Optional[Dict[str, Any]] = None) -> None:
        self.root = root
        self.res = res
        self.root.mkdir(parents=True, exist_ok=True)
        self.network_key = secrets.token_hex(32)
        self.admin_key = "dlne_adm_it_" + secrets.token_urlsafe(24)
        self.coord_port = free_port(COORDINATOR_HOST)
        self.client_tap_port = free_port(CLIENT_TAP_HOST)
        self.coordinator_url = f"http://{COORDINATOR_HOST}:{self.coord_port}"
        self.client_url = f"http://{CLIENT_TAP_HOST}:{self.client_tap_port}"
        self.members: Dict[str, Member] = {}
        self.coord: Optional[Proc] = None
        self.coord_install: Optional[Path] = None
        self.client_tap: Optional[taps.RecordingProxy] = None
        self.clients: List[ClientRun] = []
        self.coding_enabled = coding_enabled
        self.canary_per_day = canary_per_day
        self.extra_config = extra_config or {}
        self._real_dlne_dir = Path.home() / ".dlne"
        self._real_dlne_before = self._fingerprint(self._real_dlne_dir)
        self.mem_start_mb = mem_available_mb()

    # ---------------------------------------------------------------------------------- building
    @staticmethod
    def _fingerprint(path: Path) -> Any:
        """When the real ~/.dlne last changed: no process of this run may touch it."""
        found = []
        if path.exists():
            for item in path.rglob("*"):
                try:
                    found.append((str(item), item.stat().st_mtime_ns, item.stat().st_size))
                except OSError:
                    pass
        return sorted(found)

    def real_home_untouched(self) -> bool:
        return self._fingerprint(self._real_dlne_dir) == self._real_dlne_before

    def add_member(self, name: str, *, tamper: Optional[str] = None, snoop: Optional[str] = None,
                   other_identity: Optional[str] = None) -> Member:
        """Prepare a member (nothing runs until start_member). Its identity key is made now, with the engine's own
        code, so its trust id is known before the coordinator is configured."""
        from engine import member_identity            # the real module, in this process, to make the key file only
        config = {"NETWORK_KEY": self.network_key, "MAX_CPU_THREADS": THREADS_PER_SERVER,
                  "ALLOWED_GPU_INDICES": "none",
                  "CODING_MEMBER_MIN_DECODE_TPS": 5, "CODING_MEMBER_MIN_PREFILL_TPS": 20}
        install = make_install(self.root, f"member-{name}", self.res, config, "member_main.py")
        key_file = install / "config" / "coding_identity.key"
        _priv, pub_b64 = member_identity.load_or_create_identity(str(key_file))
        member = Member(name=name, install=install, home=self.root / "homes" / name, proxy_port=free_port(),
                        tap_port=free_port(), trust_id=member_identity.trust_id(pub_b64), identity_pub=pub_b64,
                        tamper=tamper, snoop=snoop)
        member._other_identity = other_identity           # type: ignore[attr-defined]
        self.members[name] = member
        return member

    def coordinator_config(self, trusted: Optional[List[str]] = None) -> Dict[str, Any]:
        config: Dict[str, Any] = {
            "NETWORK_KEY": self.network_key,
            "DEFAULT_MODEL": models.MODEL["file"],
            "ENGINE_CONTEXT": 16384,
            "MAX_CPU_THREADS": THREADS_PER_SERVER,
            "ALLOWED_GPU_INDICES": "none",
            "AUTO_SWAP": "false",
            "REBALANCE_ON_JOIN": "false",
            "CODING_MEMBERS_ENABLED": "true" if self.coding_enabled else "false",
            "TRUSTED_MEMBERS": ",".join(trusted or []),
            "CODING_CANARY_PER_DAY": self.canary_per_day,
            "CODING_MEMBER_MIN_DECODE_TPS": 5,
            "CODING_MEMBER_MIN_PREFILL_TPS": 20,
            "ADMIN_KEY_HASH": hashlib.sha256(self.admin_key.encode("utf-8")).hexdigest(),
        }
        config.update(self.extra_config)
        return config

    def start_coordinator(self, trusted: Optional[List[str]] = None, *, role_grace: Optional[float] = 3.0) -> None:
        """Build the coordinator's install and start it. ``role_grace`` is how long the coordinator waits before it
        decides a worker nobody has measured is far away and gives it a coding job (ROLE_GRACE_SECONDS, 90 in
        production); the scenarios shorten it so a run does not spend two minutes per member waiting. ``None`` keeps 90."""
        config = self.coordinator_config(trusted)
        self.coord_install = make_install(self.root, "coordinator", self.res, config, "coordinator_main.py",
                                          with_ladder=True)
        self._launch = {"host": COORDINATOR_HOST, "port": self.coord_port,
                        "far_ports": [m.tap_port for m in self.members.values()], "canary_min_gap": 3600.0}
        if role_grace is not None:
            self._launch["role_grace"] = role_grace
        (self.coord_install / "launch.json").write_text(json.dumps(self._launch))
        self.client_tap = taps.RecordingProxy(CLIENT_TAP_HOST, self.client_tap_port, COORDINATOR_HOST,
                                              self.coord_port).start()
        self._spawn_coordinator()

    def _spawn_coordinator(self, suffix: str = "") -> None:
        self.coord = Proc("coordinator", [sys.executable, "coordinator_main.py", "launch.json"], self.coord_install,
                          child_env(self.root / "homes" / "coordinator"), self.root / "logs" / f"coordinator{suffix}.out")
        wait_until(lambda: self.status() is not None, 60, "the coordinator to answer /v1/cluster/status")

    def restart_coordinator(self, *, role_grace: Optional[float] = None) -> None:
        """The coordinator process is killed and started again on the same install, port and config (a deploy).
        Members keep running and find it again through their heartbeats and tunnels."""
        self.coord.kill_group(signal.SIGKILL)
        try:
            self.coord.popen.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        launch = dict(self._launch)
        launch.pop("role_grace", None)
        if role_grace is not None:
            launch["role_grace"] = role_grace
        (self.coord_install / "launch.json").write_text(json.dumps(launch))
        self._restarts = getattr(self, "_restarts", 0) + 1
        self._spawn_coordinator(suffix=f"-restart{self._restarts}")

    def start_member(self, name: str) -> Member:
        member = self.members[name]
        launch = {"name": name, "coordinator_url": self.coordinator_url, "proxy_port": member.proxy_port,
                  "tap_port": member.tap_port, "tier": _tier(), "tamper": member.tamper, "snoop": member.snoop,
                  "other_identity": getattr(member, "_other_identity", None)}
        (member.install / "launch.json").write_text(json.dumps(launch))
        member.proc = Proc(f"member-{name}", [sys.executable, "member_main.py", "launch.json"], member.install,
                           child_env(member.home), self.root / "logs" / f"member-{name}.out")
        return member

    def set_coordinator_config(self, **changes: Any) -> None:
        """Change the running coordinator's config.txt (it re-reads the file when it changes)."""
        path = self.coord_install / "config" / "config.txt"
        values: Dict[str, Any] = dict(read_config(path))
        values.update(changes)
        write_config(path, values)

    def set_trusted(self, *names: str) -> None:
        self.set_coordinator_config(TRUSTED_MEMBERS=",".join(self.members[n].trust_id for n in names))

    # ---------------------------------------------------------------------------------- asking
    def http(self, method: str, path: str, *, admin: bool = False, base: Optional[str] = None, **kw: Any) -> httpx.Response:
        headers = dict(kw.pop("headers", {}))
        if admin:
            headers["Authorization"] = f"Bearer {self.admin_key}"
        return httpx.request(method, (base or self.coordinator_url) + path, headers=headers, timeout=kw.pop("timeout", 15),
                             trust_env=False, **kw)

    def status(self) -> Optional[Dict[str, Any]]:
        try:
            response = self.http("GET", "/v1/cluster/status")
        except httpx.HTTPError:
            return None
        return response.json() if response.status_code == 200 else None

    def coding_status(self) -> Dict[str, Any]:
        return (self.status() or {}).get("coding_members") or {}

    def route(self, session: str, privacy: str = "trusted") -> Dict[str, Any]:
        return self.http("GET", f"/v1/coding/route?session={session}&privacy={privacy}").json()

    def canaries(self) -> Dict[str, Any]:
        return self.http("GET", "/v1/admin/canaries", admin=True).json()

    def seal_for(self, offer: Dict[str, Any], text: str = "Say hello in one short sentence.") -> Dict[str, Any]:
        """A sealed chat body for a route offer, made with the engine's own seal (the same bytes the client makes)."""
        from engine import e2e
        envelope, _key = e2e.seal_request({"messages": [{"role": "user", "content": text}], "stream": True,
                                           "max_tokens": 16, "temperature": 0.2}, offer["pubkey"])
        return envelope

    def post_sealed(self, offer: Dict[str, Any], envelope: Dict[str, Any], session: str, privacy: str = "any") -> httpx.Response:
        """Send an envelope with the route it was sealed for (what the editor does), and return the whole answer."""
        return self.http("POST", "/v1/chat/completions", json=envelope, timeout=120,
                         headers={"X-DLNE-Route": offer["route"], "X-DLNE-Session": session, "X-DLNE-Privacy": privacy})

    def hold(self, count: int, *, sealed: bool, privacy: str = "any") -> List[Holder]:
        """``count`` long answers started now; each keeps a slot until released."""
        holders = [Holder(self, sealed=sealed, privacy=privacy).start() for _ in range(count)]
        for holder in holders:
            holder.started.wait(60)
        return holders

    def client(self, name: str, ops: List[Dict[str, Any]], **kw: Any) -> ClientRun:
        run = ClientRun(self, name, ops, **kw)
        self.clients.append(run)
        return run

    def ask(self, text: str, *, session: Optional[str] = None, timeout: float = 180.0, name: str = "ask",
            **kw: Any) -> Dict[str, Any]:
        """One chat turn through the real client; its done/error event plus all events."""
        session = session or secrets.token_hex(8)
        run = self.client(name, [{"op": "status", "id": "s"},
                                 {"op": "chat", "id": "c", "text": text, "sessionId": session}], **kw)
        run.wait(timeout)
        result = dict(run.result("c"))
        result["run"] = run
        result["session"] = session
        result["text"] = run.text("c")
        return result

    # ---------------------------------------------------------------------------------- readiness
    def wait_members_ready(self, count: int, timeout: float = 240.0) -> None:
        def ready() -> bool:
            status = self.coding_status()
            return status.get("ready", 0) >= count
        try:
            wait_until(ready, timeout, f"{count} coding member(s) ready (/v1/cluster/status coding_members)")
        except TimeoutError:
            raise TimeoutError(self.diagnostics())

    def wait_engine_ready(self, timeout: float = 240.0) -> None:
        """The coordinator's own llama-server is up (the status names it running, with its real context)."""
        def ready() -> bool:
            status = self.status() or {}
            loading = (status.get("model_loading") or {}).get("is_loading")
            return bool(status.get("running_models")) and not loading and bool((status.get("coding_model") or {}).get("context"))
        try:
            wait_until(ready, timeout, "the coordinator's own engine to be running", interval=1.0)
        except TimeoutError:
            raise TimeoutError(self.diagnostics())

    def diagnostics(self) -> str:
        parts = [f"status={json.dumps(self.status())[:1500]}"]
        if self.coord:
            parts.append("--- coordinator log tail ---\n" + self.coord.text()[-2500:])
        for member in self.members.values():
            parts.append(f"--- member {member.name} log tail ---\n" + member.log()[-2000:])
        return "\n".join(parts)

    # ---------------------------------------------------------------------------------- reading what was on the wire
    def logs_text(self) -> str:
        """Everything the coordinator wrote to its stdout and into its logs folder (TheWall-side logs)."""
        chunks = [self.coord.text() if self.coord else ""]
        if self.coord_install:
            for path in sorted((self.coord_install / "logs").rglob("*")):
                if path.is_file():
                    chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(chunks)

    def coordinator_disk_bytes(self) -> bytes:
        """Every byte the coordinator's install folder holds outside the engine's code and the model."""
        blobs = []
        for path in self.coord_install.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.coord_install)
            if relative.parts[0] in ("engine", "models", "bin") or path.name.endswith(".py"):
                continue
            try:
                blobs.append(path.read_bytes())
            except OSError:
                pass
        return b"\n".join(blobs)

    # ---------------------------------------------------------------------------------- end
    def stop(self) -> None:
        for client in self.clients:
            client.kill()
        for member in self.members.values():
            if member.proc:
                member.proc.stop()
        if self.coord:
            self.coord.stop()
        if self.client_tap:
            self.client_tap.close()
