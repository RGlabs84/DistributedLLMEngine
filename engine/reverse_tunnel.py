"""
Outbound Reverse RPC Tunnel for NAT / Firewall-Isolated DLNE Worker Nodes.

WHY THIS EXISTS
---------------
In public and home-network deployments, a Worker's outbound HTTP/WebSocket traffic to the
Coordinator (`:11435`) passes freely through stateful host firewalls (`nftables`, `ufw`,
Windows Defender Firewall) and home NAT routers, whereas *inbound* TCP connections initiated
by the Coordinator's `llama-server` to `<worker_ip>:50052` are dropped or rejected with
`ICMP Administratively Prohibited` (`[Errno 113] No route to host`).

This module multiplexes raw `ggml-rpc` TCP streams over a persistent outbound WebSocket
(`/ws/rpc_tunnel/{worker_id}`) on the Coordinator's existing port `11435`:
  1. Each connected worker receives a dedicated local loopback listener port on the
     Coordinator (`127.0.0.1:50100..50199`).
  2. When `InferenceManager` builds `--rpc` arguments for `llama-server`, it probes direct
     TCP `<worker_ip>:50052` first. If reachable, `llama-server` talks to `<worker_ip>:50052`
     directly with zero relay overhead.
  3. If `<worker_ip>:50052` is blocked by NAT or a host firewall, `resolve_rpc_endpoint`
     transparently substitutes `127.0.0.1:<relay_port>`, bridging `llama-server` over the
     worker's outbound WebSocket directly into the worker's local `127.0.0.1:50052`
     (`ggml-rpc-server`).
"""

import asyncio
from collections import deque
import socket
import struct
import threading
import time
from typing import Any, Callable, Deque, Dict, Optional, Tuple

from fastapi import WebSocket, WebSocketDisconnect
from engine.logger import log_coordinator, log_warning, log_error

# Frame header: [frame_type: 1 byte][stream_id: 4 bytes uint32 big-endian]
# frame_type:
#   1 = OPEN_STREAM
#   2 = DATA
#   3 = CLOSE_STREAM
FRAME_OPEN = 1
FRAME_DATA = 2
FRAME_CLOSE = 3
HEADER_STRUCT = struct.Struct("!BI")

# 1 MB stream buffer avoids per-64KB WebSocket frame overhead during multi-GB set_tensor uploads
READ_CHUNK_SIZE = 1048576

RELAY_PORT_MIN = 50100
RELAY_PORT_MAX = 50199
# One address may hold this many tunnels at once. Every tunnel belongs to an authenticated
# worker, but the swarm's network key is public, so registering workers is cheap; without a
# cap one address could still take every relay port. Eight leaves room for a household or
# office of contributors behind one router.
MAX_TUNNELS_PER_SOURCE = 8


def _enable_socket_low_latency(target: Any) -> None:
    """Enables TCP_NODELAY and TCP_QUICKACK on raw sockets or asyncio stream writers/transports."""
    if target is None:
        return
    sock = None
    if hasattr(target, "get_extra_info"):
        sock = target.get_extra_info("socket")
    if sock is None and hasattr(target, "_sock"):
        sock = target._sock
    if sock is None and isinstance(target, socket.socket):
        sock = target
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        if hasattr(socket, "TCP_QUICKACK"):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
            except Exception:
                pass


def _enable_tcp_nodelay(writer: asyncio.StreamWriter) -> None:
    _enable_socket_low_latency(writer)


class _WorkerTunnelSession:
    """Represents one active outbound WebSocket tunnel from a worker node on the Coordinator."""

    def __init__(self, worker_id: str, client_ip: str, rpc_port: int, relay_port: int, websocket: WebSocket):
        self.worker_id = worker_id
        self.client_ip = client_ip
        self.rpc_port = rpc_port
        self.relay_port = relay_port
        self.websocket = websocket
        # Where the WebSocket came from (for the per-source cap); set by the manager.
        self.source_ip = client_ip
        self.server: Optional[asyncio.AbstractServer] = None
        self._next_stream_id = 1
        self._streams: Dict[int, asyncio.StreamWriter] = {}
        self._send_lock = asyncio.Lock()
        self.closed = False

        # Live network telemetry counters
        self.total_bytes_tx: int = 0
        self.total_bytes_rx: int = 0
        self.load_bytes_tx: int = 0
        self._samples_lock = threading.Lock()
        self._tx_samples: Deque[Tuple[float, int]] = deque(maxlen=4096)

    def reset_load_counters(self) -> None:
        with self._samples_lock:
            self.load_bytes_tx = 0
            self._tx_samples.clear()

    def record_tx(self, num_bytes: int) -> None:
        if num_bytes <= 0:
            return
        now = time.monotonic()
        with self._samples_lock:
            self.total_bytes_tx += num_bytes
            self.load_bytes_tx += num_bytes
            self._tx_samples.append((now, num_bytes))
            cutoff = now - 5.0
            while self._tx_samples and self._tx_samples[0][0] < cutoff:
                self._tx_samples.popleft()

    def get_speed_mb_s(self) -> float:
        now = time.monotonic()
        cutoff = now - 5.0
        with self._samples_lock:
            while self._tx_samples and self._tx_samples[0][0] < cutoff:
                self._tx_samples.popleft()
            if not self._tx_samples:
                return 0.0
            window_bytes = sum(b for _, b in self._tx_samples)
            oldest = self._tx_samples[0][0]
            span = max(0.25, now - oldest)
            return round((window_bytes / (1024.0 * 1024.0)) / span, 2)

    async def start_local_listener(self) -> None:
        self.server = await asyncio.start_server(
            self._handle_local_llama_client,
            host="127.0.0.1",
            port=self.relay_port,
            limit=READ_CHUNK_SIZE * 2,
        )

    async def send_frame(self, frame_type: int, stream_id: int, payload: bytes = b"") -> None:
        if self.closed:
            return
        header = HEADER_STRUCT.pack(frame_type, stream_id)
        async with self._send_lock:
            await self.websocket.send_bytes(header + payload)
        if payload:
            self.record_tx(len(payload))

    async def _handle_local_llama_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        _enable_tcp_nodelay(writer)
        stream_id = self._next_stream_id
        self._next_stream_id += 1
        self._streams[stream_id] = writer

        try:
            await self.send_frame(FRAME_OPEN, stream_id)
            while not self.closed:
                data = await reader.read(READ_CHUNK_SIZE)
                if not data:
                    break
                await self.send_frame(FRAME_DATA, stream_id, data)
        except Exception:
            pass
        finally:
            self._streams.pop(stream_id, None)
            try:
                await self.send_frame(FRAME_CLOSE, stream_id)
            except Exception:
                pass
            try:
                writer.close()
            except Exception:
                pass

    async def run_receive_loop(self) -> None:
        try:
            while not self.closed:
                raw = await self.websocket.receive_bytes()
                if len(raw) < HEADER_STRUCT.size:
                    continue
                frame_type, stream_id = HEADER_STRUCT.unpack_from(raw, 0)
                payload = raw[HEADER_STRUCT.size :]
                if payload:
                    self.total_bytes_rx += len(payload)

                if frame_type == FRAME_DATA:
                    writer = self._streams.get(stream_id)
                    if writer is not None:
                        try:
                            writer.write(payload)
                            await writer.drain()
                        except Exception:
                            self._streams.pop(stream_id, None)
                elif frame_type == FRAME_CLOSE:
                    writer = self._streams.pop(stream_id, None)
                    if writer is not None:
                        try:
                            writer.close()
                        except Exception:
                            pass
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            await self.close()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.server is not None:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
        for writer in list(self._streams.values()):
            try:
                writer.close()
            except Exception:
                pass
        self._streams.clear()


class ReverseRpcBridgeManager:
    """Manages active outbound reverse RPC tunnels on the Coordinator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_worker_id: Dict[str, _WorkerTunnelSession] = {}
        self._by_ip_port: Dict[Tuple[str, int], int] = {}

    def _allocate_relay_port_locked(self) -> int:
        used = {sess.relay_port for sess in self._by_worker_id.values()}
        for port in range(RELAY_PORT_MIN, RELAY_PORT_MAX + 1):
            if port in used:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        return 0

    async def handle_websocket(
        self, websocket: WebSocket, worker_id: str, client_ip: str, rpc_port: int = 50052,
        source_ip: Optional[str] = None,
    ) -> None:
        """
        ``client_ip``/``rpc_port`` are the worker's registered endpoint (what the engine is
        told to dial); ``source_ip`` is where the WebSocket actually came from, used for the
        per-source cap. Callers authenticate the worker first (coordinator.rpc_tunnel_endpoint).
        """
        source = source_ip or client_ip
        with self._lock:
            from_source = sum(1 for wid, sess in self._by_worker_id.items()
                              if sess.source_ip == source and wid != worker_id)
        if from_source >= MAX_TUNNELS_PER_SOURCE:
            log_coordinator(
                f"Refused a reverse tunnel for {worker_id}: {source} already holds "
                f"{from_source} (the most one address may hold)."
            )
            await websocket.close(code=1008)
            return

        await websocket.accept()

        # Optimize underlying TCP socket for sub-millisecond RPC message delivery
        try:
            transport = websocket.scope.get("transport")
            if transport is not None:
                _enable_socket_low_latency(transport)
        except Exception:
            pass

        old_session: Optional[_WorkerTunnelSession] = None
        with self._lock:
            old_session = self._by_worker_id.pop(worker_id, None)
            if old_session is not None:
                self._by_ip_port.pop((old_session.client_ip, old_session.rpc_port), None)

        if old_session is not None:
            await old_session.close()

        with self._lock:
            relay_port = self._allocate_relay_port_locked()
            if relay_port == 0:
                await websocket.close()
                return
            session = _WorkerTunnelSession(worker_id, client_ip, rpc_port, relay_port, websocket)
            session.source_ip = source
            self._by_worker_id[worker_id] = session
            self._by_ip_port[(client_ip, rpc_port)] = relay_port

        try:
            await session.start_local_listener()
            log_coordinator(
                f"Reverse RPC Tunnel active for {client_ip}:{rpc_port} -> 127.0.0.1:{relay_port}"
            )
            await session.run_receive_loop()
        finally:
            with self._lock:
                if self._by_worker_id.get(worker_id) is session:
                    self._by_worker_id.pop(worker_id, None)
                if self._by_ip_port.get((client_ip, rpc_port)) == relay_port:
                    self._by_ip_port.pop((client_ip, rpc_port), None)
            await session.close()

    def get_relay_port(self, ip: str, port: int) -> Optional[int]:
        with self._lock:
            return self._by_ip_port.get((ip, port))

    def reset_load_counters(self) -> None:
        with self._lock:
            sessions = list(self._by_worker_id.values())
        for sess in sessions:
            sess.reset_load_counters()

    def get_transfer_telemetry(self) -> Dict[str, Any]:
        with self._lock:
            sessions = list(self._by_worker_id.values())
        load_tx_bytes = sum(s.load_bytes_tx for s in sessions)
        total_tx_bytes = sum(s.total_bytes_tx for s in sessions)
        speed_mb_s = round(sum(s.get_speed_mb_s() for s in sessions), 2)
        return {
            "active_tunnels": len(sessions),
            "load_tx_bytes": load_tx_bytes,
            "load_tx_gb": round(load_tx_bytes / (1024.0**3), 2),
            "total_tx_bytes": total_tx_bytes,
            "speed_mb_s": speed_mb_s,
        }

    def resolve_endpoint(self, endpoint: str, probe_timeout: float = 0.5) -> Optional[str]:
        """
        Given a remote `ip:port` endpoint:
          1. If direct TCP `ip:port` is reachable within `probe_timeout`, return `ip:port`.
          2. Otherwise, if an active outbound reverse tunnel exists for `(ip, port)`, return
             `127.0.0.1:<relay_port>`.
          3. If neither is reachable (e.g. inside unit tests with synthetic IPs and no live
             listeners), return `endpoint` when no live sockets exist, or `None` when a real
             worker's RPC port is unreachable.
        """
        try:
            host, port_s = endpoint.rsplit(":", 1)
            port = int(port_s)
        except (ValueError, TypeError):
            return endpoint

        # 1. Fast direct TCP probe
        try:
            with socket.create_connection((host, port), timeout=probe_timeout):
                return endpoint
        except OSError:
            pass

        # 2. Check if this worker has an active outbound reverse RPC tunnel
        relay_port = self.get_relay_port(host, port)
        if relay_port is not None:
            log_coordinator(
                f"Direct TCP {endpoint} blocked by firewall/NAT; routing RPC over outbound "
                f"reverse tunnel 127.0.0.1:{relay_port}."
            )
            return f"127.0.0.1:{relay_port}"

        return endpoint


reverse_bridge = ReverseRpcBridgeManager()


# ======================================================================================
# Worker-side Outbound Reverse RPC Client
# ======================================================================================

def _auth_header_kwargs(websockets_version: str, token: Optional[str]) -> Dict[str, Any]:
    """The connect() keyword carrying ``Authorization`` for this websockets release: the
    client rewritten in 14.0 takes ``additional_headers``, the legacy one ``extra_headers``."""
    if not token:
        return {}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        major = int(str(websockets_version).split(".")[0])
    except ValueError:
        major = 14
    return {"additional_headers": headers} if major >= 14 else {"extra_headers": headers}


async def _run_worker_tunnel_once(ws_url: str, local_rpc_port: int, stop_event: threading.Event,
                                  token: Optional[str] = None) -> None:
    try:
        import websockets
    except ImportError:
        return

    streams: Dict[int, asyncio.StreamWriter] = {}
    send_lock = asyncio.Lock()

    async with websockets.connect(
        ws_url,
        max_size=None,
        ping_interval=15,
        ping_timeout=15,
        **_auth_header_kwargs(getattr(websockets, "__version__", "14"), token),
    ) as ws:
        try:
            transport = getattr(ws, "transport", None)
            if transport is not None:
                _enable_socket_low_latency(transport)
        except Exception:
            pass

        async def send_frame(frame_type: int, stream_id: int, payload: bytes = b"") -> None:
            header = HEADER_STRUCT.pack(frame_type, stream_id)
            async with send_lock:
                await ws.send(header + payload)

        async def pipe_local_to_ws(stream_id: int, reader: asyncio.StreamReader) -> None:
            try:
                while not stop_event.is_set():
                    data = await reader.read(READ_CHUNK_SIZE)
                    if not data:
                        break
                    await send_frame(FRAME_DATA, stream_id, data)
            except Exception:
                pass
            finally:
                streams.pop(stream_id, None)
                try:
                    await send_frame(FRAME_CLOSE, stream_id)
                except Exception:
                    pass

        try:
            async for raw in ws:
                if stop_event.is_set():
                    break
                if not isinstance(raw, bytes) or len(raw) < HEADER_STRUCT.size:
                    continue
                frame_type, stream_id = HEADER_STRUCT.unpack_from(raw, 0)
                payload = raw[HEADER_STRUCT.size :]

                if frame_type == FRAME_OPEN:
                    try:
                        reader, writer = await asyncio.open_connection(
                            "127.0.0.1", local_rpc_port, limit=READ_CHUNK_SIZE * 2
                        )
                        _enable_tcp_nodelay(writer)
                        streams[stream_id] = writer
                        asyncio.create_task(pipe_local_to_ws(stream_id, reader))
                    except Exception:
                        await send_frame(FRAME_CLOSE, stream_id)
                elif frame_type == FRAME_DATA:
                    writer = streams.get(stream_id)
                    if writer is not None:
                        try:
                            writer.write(payload)
                            await writer.drain()
                        except Exception:
                            streams.pop(stream_id, None)
                elif frame_type == FRAME_CLOSE:
                    writer = streams.pop(stream_id, None)
                    if writer is not None:
                        try:
                            writer.close()
                        except Exception:
                            pass
        finally:
            for w in list(streams.values()):
                try:
                    w.close()
                except Exception:
                    pass
            streams.clear()


def start_worker_reverse_tunnel(
    coordinator_url: str,
    worker_id: str,
    local_rpc_port: int,
    stop_event: threading.Event,
    token_provider: Optional[Callable[[], Optional[str]]] = None,
) -> Optional[threading.Thread]:
    """
    Spawns a background daemon thread on a Worker node that maintains a persistent outbound
    reverse RPC WebSocket tunnel to the Coordinator (`/ws/rpc_tunnel/{worker_id}`).

    ``token_provider`` returns the worker's CURRENT heartbeat token, read again on every
    (re)connect because the token rotates every minute; the Coordinator admits a tunnel only
    for the worker that token is registered to. Until the first heartbeat is accepted the
    tunnel is refused, and the loop below simply tries again three seconds later.
    """
    base = coordinator_url.strip().rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://") :]
    else:
        ws_base = "ws://" + base

    ws_url = f"{ws_base}/ws/rpc_tunnel/{worker_id}?port={local_rpc_port}"

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while not stop_event.is_set():
            try:
                token = token_provider() if token_provider else None
                loop.run_until_complete(_run_worker_tunnel_once(ws_url, local_rpc_port, stop_event, token))
            except Exception:
                pass
            if stop_event.wait(3.0):
                break
        try:
            loop.close()
        except Exception:
            pass

    t = threading.Thread(target=_runner, name="dlne-reverse-rpc-tunnel", daemon=True)
    t.start()
    return t
