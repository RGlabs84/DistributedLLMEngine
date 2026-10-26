"""
Stand-ins for the load and soak test of the coding path (scripts/coding_load.py): no real model anywhere.

* :class:`FakeEngine`  - a llama-server's behaviour: an OpenAI-style SSE answer whose prefill and decode speeds
  are set, whose prompt cache remembers the last prompt of each session (so sticky routing pays off exactly as
  it does on a real member), and which can break off mid-answer, stall or answer slowly.
* :class:`FakeMember`  - a coding member as the coordinator sees it (docs/coding-members.md sections 2, 4 and 6):
  a member proxy that answers the way engine/coding_member.py does (same statuses, same error bodies, same
  Retry-After, sealed answers through engine.e2e's ResponseSealer with the sealed end, a stream that breaks
  aborts the connection with no sealed end), a heartbeat whose ``coding`` block is signed with
  engine.member_identity.sign_block, and the real worker side of the reverse tunnel
  (engine.reverse_tunnel._run_worker_tunnel_once), so requests travel the same path as in production.
* :class:`FakeLocalEngine` - TheWall's own coding engine (the llama-server the coordinator spawns), with the
  stop/start the coordinator's "viral traffic" code asks of the engine manager.
* :class:`FakeContributor` - a worker that only heartbeats, so a client can present a contributor's token
  (a CPU contributor is second in the queue).

Everything runs on one asyncio loop in one process and speaks plain HTTP over raw asyncio streams (cheaper than a
web framework per member). Times are real; ``scale`` makes the fake machines that many times faster (and
the clients think that many times less) so a short run covers a long one.

Every request a fake receives, and how it ended, is written to an event log (JSON lines) that the analysis
joins with the clients' own records: that is how queue waits, sticky hits, re-sent envelopes and wrong-member
deliveries are measured without trusting either side.
"""

import asyncio
import base64
import collections
import contextvars
import hashlib
import json
import math
import os
import random
import secrets
import threading
import time
from typing import Any, AsyncIterator, Callable, Deque, Dict, List, Optional, Tuple

import httpx

MODEL_PATH = "/home/fake/models/Qwen3.6-35B-A3B-UD-IQ3_S.gguf"
MEMBER_MODEL = "Qwen3.6-35B-A3B-UD-IQ3_S.gguf"
MEMBER_TIER = "qwen36-a3b"
MAX_BODY_BYTES = 16 << 20
MAX_EVENTS_PER_SECOND = 25.0       # SSE events one stream may emit per second; more tokens share an event


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def percentile(values: List[float], q: float) -> float:
    """The ``q`` (0..100) percentile by linear interpolation; 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * q / 100.0
    low = int(math.floor(rank))
    high = min(low + 1, len(ordered) - 1)
    return float(ordered[low] + (ordered[high] - ordered[low]) * (rank - low))


def proc_stats(pid: Optional[int] = None) -> Dict[str, Any]:
    """CPU ticks, resident memory, open files and threads of a process, from /proc (Linux); {} elsewhere."""
    root = f"/proc/{pid if pid is not None else 'self'}"
    try:
        with open(f"{root}/stat", "r", encoding="ascii") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        out: Dict[str, Any] = {"cpu_s": (int(fields[11]) + int(fields[12])) / ticks, "threads": int(fields[17]),
                               "rss_kb": int(fields[21]) * (os.sysconf("SC_PAGE_SIZE") // 1024)}
        out["fds"] = len(os.listdir(f"{root}/fd"))
        return out
    except (OSError, IndexError, ValueError):
        return {}


class LagProbe:
    """Event-loop lag: how late a 50 ms sleep wakes. A loop that is blocked shows it here, to the millisecond."""

    def __init__(self, interval: float = 0.05) -> None:
        self.interval = interval
        self._lags: List[float] = []
        self.total_samples = 0
        self.worst = 0.0
        self._task: Optional[asyncio.Task] = None

    async def _run(self) -> None:
        while True:
            before = time.perf_counter()
            await asyncio.sleep(self.interval)
            lag = max(0.0, time.perf_counter() - before - self.interval)
            self._lags.append(lag)
            self.total_samples += 1
            self.worst = max(self.worst, lag)
            if len(self._lags) > 200000:
                del self._lags[:100000]

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    def take(self) -> Dict[str, float]:
        """Summary of the lag since the last call (milliseconds), then forget it."""
        lags, self._lags = self._lags, []
        ms = [v * 1000.0 for v in lags]
        return {"n": len(ms), "p50": round(percentile(ms, 50), 2), "p95": round(percentile(ms, 95), 2),
                "p99": round(percentile(ms, 99), 2), "max": round(max(ms), 2) if ms else 0.0,
                "over100": sum(1 for v in ms if v > 100.0), "over1000": sum(1 for v in ms if v > 1000.0)}


def token_for(worker_id: str) -> str:
    """The access token a fake worker heartbeats with (a client presents it to be treated as that contributor)."""
    return hashlib.sha256(f"token:{worker_id}".encode()).hexdigest()[:48]


def loopback(index: int) -> str:
    """A distinct loopback address for the ``index``-th fake machine (127.0.0.0/8 is all this host's)."""
    return f"127.0.{10 + index // 250}.{1 + index % 250}"


# ======================================================================================
# Event log
# ======================================================================================

class EventLog:
    """JSON lines, one per event, each with the wall-clock ``t`` (the same host's clock as the clients')."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path
        self.rows: List[Dict[str, Any]] = []
        self._file = open(path, "a", buffering=1 << 16, encoding="utf-8") if path else None

    def add(self, kind: str, **fields: Any) -> None:
        row = {"t": time.time(), "k": kind, **fields}
        if self._file is not None:
            self._file.write(json.dumps(row, separators=(",", ":")) + "\n")
        else:
            self.rows.append(row)

    def flush(self) -> None:
        if self._file is not None:
            self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


# ======================================================================================
# A tiny HTTP/1.1 server on raw asyncio streams
# ======================================================================================

class Req:
    def __init__(self, method: str, path: str, headers: Dict[str, str], body: bytes) -> None:
        self.method, self.path, self.headers, self.body = method, path, headers, body


async def read_request(reader: asyncio.StreamReader) -> Optional[Req]:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, target, _version = lines[0].split(" ", 2)
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    if headers.get("transfer-encoding", "").lower() == "chunked":
        parts: List[bytes] = []
        while True:
            size = int((await reader.readline()).strip().split(b";")[0] or b"0", 16)
            if size == 0:
                await reader.readline()
                break
            parts.append(await reader.readexactly(size))
            await reader.readexactly(2)
        body = b"".join(parts)
    else:
        length = int(headers.get("content-length", "0") or 0)
        body = await reader.readexactly(length) if length else b""
    return Req(method.upper(), target.split("?", 1)[0], headers, body)


_REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large", 499: "Client Closed",
            500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable"}


async def send_response(writer: asyncio.StreamWriter, status: int, body: bytes, content_type: str = "application/json",
                        headers: Optional[Dict[str, str]] = None) -> None:
    head = [f"HTTP/1.1 {status} {_REASONS.get(status, 'Status')}", f"content-type: {content_type}",
            f"content-length: {len(body)}"]
    for key, value in (headers or {}).items():
        head.append(f"{key}: {value}")
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
    await writer.drain()


async def send_json(writer: asyncio.StreamWriter, status: int, obj: Any, headers: Optional[Dict[str, str]] = None) -> None:
    await send_response(writer, status, json.dumps(obj, separators=(",", ":")).encode(), headers=headers)


async def begin_sse(writer: asyncio.StreamWriter) -> None:
    writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncache-control: no-cache\r\n"
                 b"transfer-encoding: chunked\r\n\r\n")
    await writer.drain()


async def send_chunk(writer: asyncio.StreamWriter, data: bytes) -> None:
    if writer.transport.is_closing():
        raise ConnectionResetError("peer gone")
    writer.write(b"%x\r\n" % len(data) + data + b"\r\n")
    await writer.drain()


async def end_sse(writer: asyncio.StreamWriter) -> None:
    writer.write(b"0\r\n\r\n")
    await writer.drain()


class HttpServer:
    """``handler(req, writer) -> keep_alive`` per request; a connection serves requests one after another."""

    def __init__(self, host: str, port: int, handler: Callable[[Req, asyncio.StreamWriter], Any]) -> None:
        self.host, self.port, self.handler = host, port, handler
        self._server: Optional[asyncio.AbstractServer] = None
        self.writers: set = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_connection, self.host, self.port,
                                                  limit=1 << 22, backlog=2048, reuse_address=True)

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.add(writer)
        try:
            while True:
                try:
                    req = await asyncio.wait_for(read_request(reader), 120.0)
                except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError, asyncio.LimitOverrunError,
                        ValueError, OSError):
                    break
                keep = await self.handler(req, writer)
                if not keep:
                    break
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            self.writers.discard(writer)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def stop(self, abort: bool = True) -> None:
        """Stop listening; ``abort`` also resets every open connection (a machine that died)."""
        if self._server is not None:
            self._server.close()
            self._server = None
        for writer in list(self.writers):
            try:
                if abort:
                    writer.transport.abort()
                else:
                    writer.close()
            except Exception:  # noqa: BLE001
                pass
        self.writers.clear()


# ======================================================================================
# The model server
# ======================================================================================

_CORPUS_WORDS = ("def self return import async await class for in if else None True False lambda yield "
                 "const let var function export interface type string number boolean void Promise "
                 "fn pub struct impl match Some Ok Err mut ref use mod trait where").split()


def estimate_tokens(body: Dict[str, Any]) -> int:
    """About what a tokenizer would count: four characters a token, a few for each message's framing."""
    messages = body.get("messages")
    if isinstance(messages, list):
        total = 0
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            total += (len(content) if isinstance(content, str) else 0) // 4 + 4
        return max(1, total)
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        return max(1, len(prompt) // 4)
    return 1


class EngineError(Exception):
    """The model server answers with an HTTP error instead of a stream."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class StreamAbort(Exception):
    """The model server broke off mid-answer. ``hard``: the connection is reset; else it is closed without the end."""

    def __init__(self, hard: bool) -> None:
        super().__init__("abort")
        self.hard = hard


class Pace:
    def __init__(self, decode_tps: float = 30.0, prefill_tps: float = 120.0, scale: float = 1.0, jitter: float = 0.08) -> None:
        self.decode_tps, self.prefill_tps, self.scale, self.jitter = decode_tps, prefill_tps, max(0.01, scale), jitter

    def tokens_per_event(self) -> int:
        return max(1, math.ceil(self.decode_tps * self.scale / MAX_EVENTS_PER_SECOND))

    def noise(self) -> float:
        return max(0.5, random.gauss(1.0, self.jitter))


class FakeEngine:
    """
    One llama-server's behaviour. ``inject`` (changeable while running) holds the faults to apply to a request:
    ``die_mid_rate`` / ``die_before_rate`` (the answer breaks off after some tokens / before the first),
    ``stall_rate`` with ``stall_s`` (it goes quiet part-way for that long; None = for good), ``http500_rate``
    and ``slow_factor`` (all times multiplied).
    """

    def __init__(self, name: str, pace: Pace, cache_sessions: int = 8) -> None:
        self.name, self.pace = name, pace
        self.cache_sessions = cache_sessions
        self.cache: "collections.OrderedDict[str, int]" = collections.OrderedDict()
        self.inject: Dict[str, Any] = {}
        self.served = 0
        self.cache_hits = 0

    def _cached_prefix(self, sid: Optional[str], ptok: int) -> int:
        if not sid or sid not in self.cache:
            return 0
        self.cache.move_to_end(sid)
        return max(0, min(self.cache[sid], ptok - 1))

    def _remember(self, sid: Optional[str], tokens: int) -> None:
        if not sid:
            return
        self.cache[sid] = tokens
        self.cache.move_to_end(sid)
        while len(self.cache) > self.cache_sessions:
            self.cache.popitem(last=False)

    def _roll(self, rate_key: str) -> bool:
        rate = float(self.inject.get(rate_key) or 0.0)
        return rate > 0 and random.random() < rate

    def plan(self, body: Dict[str, Any]) -> Dict[str, Any]:
        xl = body.get("x_load") if isinstance(body.get("x_load"), dict) else {}
        ptok = estimate_tokens(body)
        otok = xl.get("otok") or body.get("max_tokens") or 128
        otok = max(1, min(int(otok), 8000)) if isinstance(otok, (int, float)) else 128
        sid = xl.get("sid") if isinstance(xl.get("sid"), str) else None
        cached = self._cached_prefix(sid, ptok)
        if cached:
            self.cache_hits += 1
        slow = float(self.inject.get("slow_factor") or 1.0)
        fault = None
        if self._roll("http500_rate"):
            fault = "http500"
        elif self._roll("die_before_rate"):
            fault = "die_before"
        elif self._roll("die_mid_rate"):
            fault = "die_mid"
        elif self._roll("stall_rate"):
            fault = "stall"
        return {"ptok": ptok, "otok": otok, "sid": sid, "cached": cached, "slow": slow, "fault": fault,
                "rid": xl.get("rid"), "xl": xl}

    async def stream(self, plan: Dict[str, Any], who: str, t_recv: float) -> AsyncIterator[bytes]:
        """The chunks of a streamed chat answer, as llama-server would send them (JSON payloads, no framing)."""
        pace, fault = self.pace, plan["fault"]
        if fault == "http500":
            raise EngineError(500)
        if fault == "die_before":
            await asyncio.sleep(0.05)
            raise StreamAbort(hard=random.random() < 0.5)
        eff = max(1, plan["ptok"] - plan["cached"])
        prefill_s = eff / (pace.prefill_tps * pace.scale) * pace.noise() * plan["slow"]
        await asyncio.sleep(prefill_s)
        t_start = time.time()
        chunk_id = "chatcmpl-" + (plan["rid"] or secrets.token_hex(6))

        def chunk(content: Optional[str], finish: Optional[str] = None, **extra: Any) -> bytes:
            obj: Dict[str, Any] = {"id": chunk_id, "object": "chat.completion.chunk", "created": int(t_start),
                                   "model": MODEL_PATH,
                                   "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}]}
            obj.update(extra)
            return json.dumps(obj, separators=(",", ":")).encode()

        k = pace.tokens_per_event()
        events = max(1, math.ceil(plan["otok"] / k))
        if fault in ("die_mid", "stall"):
            events = max(events, 3)              # something has to come before the break
        interval = k / (pace.decode_tps * pace.scale) * plan["slow"]
        die_at = random.randint(1, max(1, events - 1)) if fault == "die_mid" else None
        stall_at = random.randint(1, max(1, events - 1)) if fault == "stall" else None
        # llama-server's first chunk carries the role and no text.
        yield json.dumps({"id": chunk_id, "object": "chat.completion.chunk", "created": int(t_start), "model": MODEL_PATH,
                          "choices": [{"index": 0, "delta": {"role": "assistant", "content": None},
                                       "finish_reason": None}]}, separators=(",", ":")).encode()
        text = f"[{who}] " + " ".join(random.choices(_CORPUS_WORDS, k=k)) + " "
        for i in range(events):
            if die_at is not None and i == die_at:
                raise StreamAbort(hard=random.random() < 0.5)
            if stall_at is not None and i == stall_at:
                stall = self.inject.get("stall_s", 20.0)
                if stall is None:
                    await asyncio.sleep(3600)
                else:
                    await asyncio.sleep(float(stall))
            if i == 0:
                yield chunk(text, x_load={"member": who, "recv": t_recv, "start": t_start, "cached": plan["cached"],
                                          "ptok": plan["ptok"], "rid": plan["rid"]})
            else:
                yield chunk(" ".join(random.choices(_CORPUS_WORDS, k=k)) + " ")
            await asyncio.sleep(interval * pace.noise())
        decode_s = max(1e-6, time.time() - t_start)
        self._remember(plan["sid"], plan["ptok"] + plan["otok"])
        self.served += 1
        timings = {"prompt_n": eff, "prompt_per_second": round(eff / max(1e-6, t_start - t_recv), 1),
                   "predicted_n": plan["otok"], "predicted_per_second": round(plan["otok"] / decode_s, 1)}
        yield chunk("", "stop", timings=timings,
                    usage={"prompt_tokens": plan["ptok"], "completion_tokens": plan["otok"],
                           "total_tokens": plan["ptok"] + plan["otok"]})

    async def complete(self, plan: Dict[str, Any], who: str, t_recv: float) -> Dict[str, Any]:
        """A non-streamed (autocomplete) answer."""
        if plan["fault"] == "http500":
            raise EngineError(500)
        pace = self.pace
        eff = max(1, plan["ptok"] - plan["cached"])
        await asyncio.sleep((eff / (pace.prefill_tps * pace.scale) + min(plan["otok"], 24) / (pace.decode_tps * pace.scale))
                            * plan["slow"])
        self._remember(plan["sid"], plan["ptok"])
        self.served += 1
        return {"id": "cmpl-" + secrets.token_hex(6), "object": "text_completion", "model": MODEL_PATH,
                "choices": [{"index": 0, "text": " return None", "finish_reason": "stop"}],
                "x_load": {"member": who, "recv": t_recv}}


# ======================================================================================
# Identity, heartbeat
# ======================================================================================

def identity_for(seed: str) -> Tuple[bytes, str, str]:
    """A member's identity key made from a name, so the parent knows its trust id before the member starts:
    ``(raw private key, base64 public key, trust id)``."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from engine import member_identity
    raw = hashlib.sha256(("dlne-load-identity:" + seed).encode()).digest()
    pub = Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return raw, b64(pub), member_identity.trust_id_of_pub(pub)


def coding_hardware(coding_block: Dict[str, Any]) -> Dict[str, Any]:
    return {"ram_gb": 32.0, "cpu_cores": 8, "cpu_threads": 16, "cpu_arch": "x86_64", "cpu_features": ["avx2", "fma"],
            "gpu": {"has_gpu": True, "vram_total_gb": 16.0, "name": "Fake GPU"}, "coding_ok": True,
            "pool": "coding", "coding": coding_block}


def cpu_contributor_hardware() -> Dict[str, Any]:
    return {"ram_gb": 16.0, "cpu_cores": 8, "cpu_threads": 16, "cpu_arch": "x86_64", "cpu_features": ["avx2", "fma"],
            "mem_bandwidth_gbs": 60.0, "gpu": None, "pool": "app", "app": {"model": "fake-app.gguf", "ready": True, "slots": 2}}


class Heartbeater:
    """A worker's signed heartbeat loop (engine/worker.py's, minus everything else)."""

    def __init__(self, base_url: str, network_key: str, worker_id: str, token: str, port: int, src_ip: Optional[str],
                 hardware: Callable[[], Dict[str, Any]], interval: float = 5.0) -> None:
        self.base_url, self.network_key, self.worker_id, self.token, self.port = base_url, network_key, worker_id, token, port
        self.src_ip, self.hardware, self.interval = src_ip, hardware, interval
        self.answers: "collections.Counter[str]" = collections.Counter()
        self.statuses: "collections.Counter[int]" = collections.Counter()
        self.last_answer: Dict[str, Any] = {}
        self._task: Optional[asyncio.Task] = None

    async def beat(self, client: httpx.AsyncClient) -> None:
        from engine import authn
        from engine.version import VERSION
        hardware = self.hardware()
        stamp, nonce = time.time(), secrets.token_hex(16)
        signature = authn.sign_heartbeat(self.network_key, self.worker_id, self.port, self.token, stamp, nonce,
                                         VERSION, hardware, "worker", "")
        payload = {"worker_id": self.worker_id, "version": VERSION, "port": self.port, "token": self.token,
                   "hardware": hardware, "timestamp": stamp, "nonce": nonce, "signature": signature,
                   "role": "worker", "staged_version": ""}
        try:
            reply = await client.post(f"{self.base_url}/worker/heartbeat", json=payload)
            self.statuses[reply.status_code] += 1
            if reply.status_code == 200:
                self.last_answer = reply.json()
                self.answers[json.dumps({k: v for k, v in self.last_answer.items() if k != "coding_tier"}, sort_keys=True)] += 1
        except Exception as exc:  # noqa: BLE001 - a heartbeat that fails is simply missed, as on a real network
            self.statuses[0] += 1
            self.answers["error:" + type(exc).__name__] += 1

    async def _run(self) -> None:
        transport = httpx.AsyncHTTPTransport(local_address=self.src_ip) if self.src_ip else None
        async with httpx.AsyncClient(transport=transport, timeout=10.0) as client:
            await asyncio.sleep(random.random() * min(self.interval, 2.0))
            while True:
                await self.beat(client)
                await asyncio.sleep(self.interval * random.uniform(0.9, 1.1))

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


# ======================================================================================
# The worker side of the reverse tunnel, bound to one loopback source address
# ======================================================================================

_SRC = contextvars.ContextVar("dlne_load_src_ip", default=None)
_patched = False


def _patch_websockets() -> None:
    """engine.reverse_tunnel's worker client opens its WebSocket from wherever the OS picks; one source address
    may hold only eight tunnels, so each fake machine connects from its own loopback address."""
    global _patched
    if _patched:
        return
    import websockets
    original = websockets.connect

    def connect(*args: Any, **kwargs: Any):
        src = _SRC.get()
        if src:
            kwargs.setdefault("local_addr", (src, 0))
        return original(*args, **kwargs)

    websockets.connect = connect
    _patched = True


class Tunnel:
    def __init__(self, base_url: str, worker_id: str, local_port: int, token: str, src_ip: str) -> None:
        self.ws_url = base_url.replace("http://", "ws://", 1) + f"/ws/rpc_tunnel/{worker_id}?port={local_port}"
        self.local_port, self.token, self.src_ip = local_port, token, src_ip
        self.stop_event = threading.Event()
        self._task: Optional[asyncio.Task] = None
        self.connects = 0

    async def _run(self) -> None:
        from engine.reverse_tunnel import _run_worker_tunnel_once
        _SRC.set(self.src_ip)
        while not self.stop_event.is_set():
            try:
                self.connects += 1
                await _run_worker_tunnel_once(self.ws_url, self.local_port, self.stop_event, self.token)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - refused until the first heartbeat is accepted; try again
                pass
            await asyncio.sleep(1.0)

    def start(self) -> None:
        _patch_websockets()
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        self.stop_event.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None


# ======================================================================================
# A coding member
# ======================================================================================

def _proxy_error(status: int, kind: str, message: str, retry_after: Optional[int] = None) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
    """The proxy-made error engine/coding_member.py sends: fixed words, never anything from the request."""
    headers = {"Retry-After": str(retry_after)} if retry_after else {}
    return status, {"error": {"message": message, "type": kind, "code": status}}, headers


class FakeMember:
    """A coding member: proxy on its own port, signed heartbeat, tunnel, and the model server behind it."""

    def __init__(self, index: int, spec: Dict[str, Any], coordinator_url: str, network_key: str, log: EventLog,
                 scale: float, port: int) -> None:
        from engine.e2e import KeyRing
        self.index = index
        self.name = f"m{index:02d}"
        self.log, self.coordinator_url, self.network_key = log, coordinator_url, network_key
        self.slots = int(spec.get("slots", 2))
        self.port = port
        self.src_ip = loopback(index)
        self.worker_id = f"fake-member-{index:03d}"
        self.token = token_for(self.worker_id)
        self.identity_priv, self.identity_pub, self.trust_id = identity_for(self.name)
        self.keyring = KeyRing(rotate_every=float(spec["rotate_every_s"])) if spec.get("rotate_every_s") else KeyRing()
        self.decode_tps = float(spec.get("decode_tps", 30.0))
        self.prefill_tps = float(spec.get("prefill_tps", 120.0))
        self.engine = FakeEngine(self.name, Pace(self.decode_tps, self.prefill_tps, scale),
                                 cache_sessions=int(spec.get("cache_sessions", 8)))
        self.engine.inject.update(spec.get("inject") or {})
        self.ready = True
        self.aside = False            # the owner is gaming: ready false, no slots, nothing is answered
        self.inflight = 0
        self.peak = 0
        self.up = False
        self.counts: "collections.Counter[str]" = collections.Counter()
        self.server = HttpServer("127.0.0.1", port, self._handle)
        self.heartbeat = Heartbeater(coordinator_url, network_key, self.worker_id, self.token, port, self.src_ip,
                                     self._hardware, interval=float(spec.get("heartbeat_s", 5.0)))
        self.tunnel = Tunnel(coordinator_url, self.worker_id, port, self.token, self.src_ip)

    # ---- heartbeat
    @property
    def serving(self) -> bool:
        return self.ready and not self.aside

    def block(self) -> Dict[str, Any]:
        """The ``coding`` block, signed as engine/coding_member.py's status() signs it. A member stepping aside
        says ready false with 0 slots (the router stops sending before anything else happens)."""
        from engine import member_identity
        self.keyring.maybe_rotate()
        slots = self.slots if not self.aside else 0
        block = {"model": MEMBER_MODEL, "tier": MEMBER_TIER, "ready": self.serving, "slots": slots,
                 "busy": min(self.inflight, slots), "decode_tps": round(self.decode_tps, 1),
                 "prefill_tps": round(self.prefill_tps, 1), "pubkey": self.keyring.public_b64, "no_trace": True}
        return member_identity.sign_block(block, self.identity_priv)

    def _hardware(self) -> Dict[str, Any]:
        return coding_hardware(self.block())

    # ---- lifecycle
    async def start(self) -> None:
        await self.server.start()
        self.up = True
        self.heartbeat.start()
        self.tunnel.start()
        self.log.add("member", op="start", who=self.name, trust=self.trust_id)

    async def stop(self, hard: bool = False) -> None:
        """Leave. Graceful: stop heartbeating and close the tunnel and the port. Hard: the same at once and every
        stream reset (a machine that lost power); the coordinator learns only from the tunnel and missing heartbeats."""
        if not self.up:
            return
        self.up = False
        self.heartbeat.stop()
        self.tunnel.stop()
        await self.server.stop(abort=True)
        self.log.add("member", op="die" if hard else "leave", who=self.name)

    # ---- the proxy (engine/coding_member.py's _proxy_request, status for status)
    async def _handle(self, req: Req, writer: asyncio.StreamWriter) -> bool:
        if req.method == "GET" and req.path == "/health":
            ok = self.serving
            await send_json(writer, 200 if ok else 503, {"status": "ok" if ok else "loading", "ready": ok,
                                                         "slots": self.slots, "busy": min(self.inflight, self.slots),
                                                         "trust_id": self.trust_id})
            return True
        if req.method == "POST" and req.path in ("/v1/chat/completions", "/v1/completions"):
            return await self._proxy(req, writer)
        if req.path in ("/v1/chat/completions", "/v1/completions", "/health"):
            await send_json(writer, 405, {"detail": "Method Not Allowed"})
        else:
            await send_json(writer, 404, {"detail": "Not Found"})
        return True

    async def _reject(self, writer: asyncio.StreamWriter, why: str, rid: Optional[str], *error: Any) -> bool:
        status, body, headers = error
        self.counts["rej_" + why] += 1
        self.log.add("rej", who=self.name, why=why, rid=rid)
        await send_json(writer, status, body, headers)
        return True

    async def _proxy(self, req: Req, writer: asyncio.StreamWriter) -> bool:
        t_recv = time.time()
        if not self.serving:
            return await self._reject(writer, "not_ready", None, *_proxy_error(
                503, "not_ready", "this member's model is not ready yet", retry_after=5))
        if len(req.body) > MAX_BODY_BYTES:
            return await self._reject(writer, "too_large", None, *_proxy_error(
                413, "invalid_request", "the request body is too large"))
        try:
            body = json.loads(req.body)
        except (ValueError, RecursionError):
            return await self._reject(writer, "bad_json", None, *_proxy_error(
                400, "invalid_request", "the request body is not valid JSON"))
        if not isinstance(body, dict):
            return await self._reject(writer, "bad_json", None, *_proxy_error(
                400, "invalid_request", "the request body must be a JSON object"))
        resp_key: Optional[bytes] = None
        epk = None
        if "dlne_e2e" in body:
            envelope = body["dlne_e2e"]
            epk = hashlib.sha256(str(envelope.get("epk") if isinstance(envelope, dict) else "").encode()).hexdigest()[:16]
            try:
                opened, resp_key = self.keyring.open_request(body)
                if not isinstance(opened, dict):
                    raise ValueError
            except Exception as exc:  # noqa: BLE001 - the same answer whatever the reason
                self.counts["envelope_unopened"] += 1
                self.log.add("req", who=self.name, rid=None, epk=epk, sealed=True, opened=False,
                             code=str(getattr(exc, "code", type(exc).__name__)), size=len(req.body))
                return await self._reject(writer, "invalid_envelope", None, *_proxy_error(
                    400, "invalid_envelope", "the encrypted request could not be opened"))
            body = opened
        xl = body.get("x_load") if isinstance(body.get("x_load"), dict) else {}
        rid = xl.get("rid")
        self.log.add("req", who=self.name, rid=rid, sid=xl.get("sid"), turn=xl.get("turn"), tier=xl.get("tier"),
                     attempt=xl.get("attempt"), sent=xl.get("sent"), epk=epk, sealed=resp_key is not None,
                     opened=True, size=len(req.body), busy=self.inflight)
        if self.inflight >= self.slots:
            return await self._reject(writer, "busy", rid, *_proxy_error(
                503, "busy", "all of this member's slots are busy", retry_after=2))
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        outcome, sent_events = "ok", 0
        try:
            plan = self.engine.plan(body)
            streaming = bool(body.get("stream")) and req.path == "/v1/chat/completions"
            sealer = None
            if resp_key is not None:
                from engine.e2e import ResponseSealer
                sealer = ResponseSealer(resp_key)
            if not streaming:
                try:
                    result = await self.engine.complete(plan, self.name, t_recv)
                except EngineError:
                    outcome = "engine_error"
                    if sealer is not None:
                        await send_json(writer, 500, {"error": {"message": "this member's model server could not answer the request",
                                                                "type": "engine_error", "code": 500}})
                    else:
                        await send_json(writer, 500, {"error": {"message": "engine error", "type": "engine_error"}})
                    return True
                if sealer is not None:
                    await send_json(writer, 200, {"dlne_e2e": sealer.seal(json.dumps(result).encode())})
                else:
                    await send_json(writer, 200, result)
                return True
            generator = self.engine.stream(plan, self.name, t_recv)
            began = False
            try:
                async for payload in generator:
                    if not began:
                        await begin_sse(writer)
                        began = True
                    if sealer is not None:
                        wire = b"data: " + json.dumps({"dlne_e2e": sealer.seal(payload)}, separators=(",", ":")).encode() + b"\n\n"
                    else:
                        wire = b"data: " + payload + b"\n\n"
                    await send_chunk(writer, wire)
                    sent_events += 1
                if sealer is not None:
                    await send_chunk(writer, b"data: " + json.dumps({"dlne_e2e": sealer.finish()}, separators=(",", ":")).encode()
                                     + b"\n\n")
                await send_chunk(writer, b"data: [DONE]\n\n")
                await end_sse(writer)
            except EngineError:
                outcome = "engine_error"
                if not began:
                    await send_json(writer, 502 if sealer is None else 500, {"error": {
                        "message": "this member's model server could not answer the request", "type": "engine_error"}})
                    return True
                writer.transport.abort()
                return False
            except StreamAbort as abort:
                # A stream that breaks mid-answer aborts the connection: no sealed end, no [DONE].
                outcome = "abort"
                self.counts["aborted"] += 1
                if not began:
                    status, err, headers = _proxy_error(502, "engine_unavailable", "this member's model server did not answer")
                    await send_json(writer, status, err, headers)
                    return True
                if abort.hard:
                    writer.transport.abort()
                else:
                    writer.close()
                return False
            return True
        except (ConnectionError, OSError):
            outcome = "peer_gone"
            self.counts["peer_gone"] += 1
            self.log.add("gone", who=self.name, rid=rid, events=sent_events)
            return False
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            self.inflight -= 1
            self.counts["finished_" + outcome] += 1
            self.log.add("fin", who=self.name, rid=rid, out=outcome, events=sent_events, dur=round(time.time() - t_recv, 3))

    def stats(self) -> Dict[str, Any]:
        return {"name": self.name, "up": self.up, "inflight": self.inflight, "peak": self.peak,
                "served": self.engine.served, "cache_hits": self.engine.cache_hits, "counts": dict(self.counts),
                "heartbeats": dict(self.heartbeat.statuses), "tunnel_connects": self.tunnel.connects,
                "answers": dict(self.heartbeat.answers)}


# ======================================================================================
# TheWall's own engine
# ======================================================================================

class FakeLocalEngine:
    """
    The llama-server the coordinator spawns for the local coding path. It queues requests beyond its slots
    (as llama.cpp does) and answers plain JSON. A second, control port takes the stop/start the engine manager
    would do (the coordinator's "viral traffic" code restarts the engine with more lanes): stop resets every
    open stream and refuses connections; start takes ``boot_s`` and comes back with ``slots`` lanes.
    """

    def __init__(self, port: int, control_port: int, spec: Dict[str, Any], log: EventLog, scale: float) -> None:
        self.port, self.control_port, self.log, self.scale = port, control_port, log, scale
        self.slots = int(spec.get("slots", 2))
        self.pace = Pace(float(spec.get("decode_tps", 40.0)), float(spec.get("prefill_tps", 1500.0)), scale)
        self.engine = FakeEngine("local", self.pace, cache_sessions=int(spec.get("cache_sessions", 8)))
        self.engine.inject.update(spec.get("inject") or {})
        self.boot_s = float(spec.get("boot_s", 15.0))
        self.running = False
        self.inflight = 0
        self.peak = 0
        self.restarts = 0
        self.counts: "collections.Counter[str]" = collections.Counter()
        self._sem = asyncio.Semaphore(self.slots)
        self.server = HttpServer("127.0.0.1", port, self._handle)
        self.control = HttpServer("127.0.0.1", control_port, self._control)

    async def start(self, slots: Optional[int] = None) -> None:
        if slots:
            self.slots = int(slots)
        self._sem = asyncio.Semaphore(self.slots)
        self.server = HttpServer("127.0.0.1", self.port, self._handle)
        await self.server.start()
        self.running = True
        self.log.add("engine", op="start", slots=self.slots)

    async def stop(self) -> None:
        self.running = False
        await self.server.stop(abort=True)
        self.restarts += 1
        self.log.add("engine", op="stop", inflight=self.inflight)

    async def start_control(self) -> None:
        await self.control.start()

    async def _control(self, req: Req, writer: asyncio.StreamWriter) -> bool:
        try:
            command = json.loads(req.body or b"{}")
        except ValueError:
            command = {}
        op = command.get("cmd")
        if op == "stop":
            await self.stop()
        elif op == "start":
            if not self.running:
                await asyncio.sleep(float(command.get("boot_s", self.boot_s)))
                await self.start(command.get("slots"))
        await send_json(writer, 200, {"running": self.running, "slots": self.slots, "restarts": self.restarts})
        return True

    async def _handle(self, req: Req, writer: asyncio.StreamWriter) -> bool:
        if req.method == "GET" and req.path in ("/health", "/v1/models", "/props"):
            await send_json(writer, 200, {"status": "ok", "data": [{"id": MODEL_PATH}]})
            return True
        if req.method == "POST" and req.path in ("/v1/chat/completions", "/v1/completions"):
            return await self._serve(req, writer)
        await send_json(writer, 404, {"error": {"message": "not found"}})
        return True

    async def _serve(self, req: Req, writer: asyncio.StreamWriter) -> bool:
        t_recv = time.time()
        try:
            body = json.loads(req.body)
        except ValueError:
            await send_json(writer, 400, {"error": {"message": "bad json"}})
            return True
        xl = body.get("x_load") if isinstance(body.get("x_load"), dict) else {}
        rid = xl.get("rid")
        self.log.add("req", who="local", rid=rid, sid=xl.get("sid"), turn=xl.get("turn"), tier=xl.get("tier"),
                     attempt=xl.get("attempt"), sent=xl.get("sent"), sealed=False, size=len(req.body), busy=self.inflight)
        outcome, sent_events = "ok", 0
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            async with self._sem:
                t_start = time.time()
                plan = self.engine.plan(body)
                if not (bool(body.get("stream")) and req.path == "/v1/chat/completions"):
                    await send_json(writer, 200, await self.engine.complete(plan, "local", t_start))
                    return True
                began = False
                try:
                    async for payload in self.engine.stream(plan, "local", t_start):
                        if not began:
                            await begin_sse(writer)
                            began = True
                        await send_chunk(writer, b"data: " + payload + b"\n\n")
                        sent_events += 1
                    await send_chunk(writer, b"data: [DONE]\n\n")
                    await end_sse(writer)
                except EngineError:
                    outcome = "engine_error"
                    await send_json(writer, 500, {"error": {"message": "engine error"}})
                except StreamAbort:
                    outcome = "abort"
                    writer.transport.abort()
                    return False
                return True
        except (ConnectionError, OSError):
            outcome = "peer_gone"
            self.log.add("gone", who="local", rid=rid, events=sent_events)
            return False
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            self.inflight -= 1
            self.counts["finished_" + outcome] += 1
            self.log.add("fin", who="local", rid=rid, out=outcome, events=sent_events, dur=round(time.time() - t_recv, 3))

    def stats(self) -> Dict[str, Any]:
        return {"running": self.running, "slots": self.slots, "inflight": self.inflight, "peak": self.peak,
                "restarts": self.restarts, "served": self.engine.served, "cache_hits": self.engine.cache_hits,
                "counts": dict(self.counts)}


# ======================================================================================
# A worker that only heartbeats
# ======================================================================================

class FakeContributor:
    """A contributor the clients can present the token of: a CPU contributor (second in the queue)."""

    def __init__(self, index: int, coordinator_url: str, network_key: str) -> None:
        self.worker_id = f"fake-cpu-{index:03d}"
        self.token = token_for(self.worker_id)
        self.heartbeat = Heartbeater(coordinator_url, network_key, self.worker_id, self.token, 49000 + index,
                                     loopback(200 + index), cpu_contributor_hardware, interval=5.0)

    def start(self) -> None:
        self.heartbeat.start()
