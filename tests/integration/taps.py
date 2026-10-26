"""
A wire tap: a TCP proxy that passes bytes through untouched and keeps a copy of every one.

It is how the integration run proves what a listener on the path could see. One tap sits between the editor and
the coordinator (everything TheWall's HTTP layer receives and sends, after TLS would have been removed), another
sits in front of each member on the far side of the reverse tunnel (the bytes the coordinator relays to the
member and the member's answer). Everything is recorded per connection and per direction, in arrival order.

A tap in another process (the member's) appends each chunk to a JSON-lines file; ``read_record`` loads such a
file back. Nothing here interprets the bytes.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

CHUNK = 65536


@dataclass
class Connection:
    """Everything one proxied connection carried."""

    number: int
    opened: float
    closed: Optional[float] = None
    c2s: bytearray = field(default_factory=bytearray)     # toward the server (the request)
    s2c: bytearray = field(default_factory=bytearray)     # toward the client (the answer)
    chunks: List[Tuple[float, str, bytes]] = field(default_factory=list)

    def request_line(self) -> str:
        return bytes(self.c2s).split(b"\r\n", 1)[0].decode("latin-1")

    @property
    def request_body(self) -> bytes:
        """The body of the (first) HTTP request on this connection, as it crossed the wire."""
        data = bytes(self.c2s)
        head, sep, body = data.partition(b"\r\n\r\n")
        return body if sep else b""

    def request_headers(self) -> Dict[str, str]:
        head = bytes(self.c2s).partition(b"\r\n\r\n")[0].decode("latin-1")
        headers: Dict[str, str] = {}
        for line in head.split("\r\n")[1:]:
            name, _, value = line.partition(":")
            if name:
                headers[name.strip().lower()] = value.strip()
        return headers

    @property
    def response_body(self) -> bytes:
        data = bytes(self.s2c)
        head, sep, body = data.partition(b"\r\n\r\n")
        return body if sep else b""


class RecordingProxy:
    """Listen on (listen_host, listen_port), forward to (target_host, target_port), record both directions."""

    def __init__(self, listen_host: str, listen_port: int, target_host: str, target_port: int,
                 record_path: Optional[str] = None) -> None:
        self.listen = (listen_host, listen_port)
        self.target = (target_host, target_port)
        self.record_path = record_path
        self.connections: List[Connection] = []
        self._lock = threading.Lock()
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._open: List[socket.socket] = []

    # ---------------------------------------------------------------------------------- lifecycle
    def start(self) -> "RecordingProxy":
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(self.listen)
        server.listen(64)
        server.settimeout(0.3)
        self._server = server
        self._thread = threading.Thread(target=self._accept_loop, name=f"tap-{self.listen[1]}", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        with self._lock:
            sockets, self._open = list(self._open), []
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass

    def sever(self) -> None:
        """Drop every connection now (the far side sees a reset), and keep listening."""
        with self._lock:
            sockets, self._open = list(self._open), []
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    # ---------------------------------------------------------------------------------- the relay
    def _accept_loop(self) -> None:
        count = 0
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()          # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                return
            count += 1
            conn = Connection(number=count, opened=time.time())
            with self._lock:
                self.connections.append(conn)
            threading.Thread(target=self._serve, args=(client, conn), daemon=True).start()

    def _serve(self, client: socket.socket, conn: Connection) -> None:
        try:
            upstream = socket.create_connection(self.target, timeout=10)
            upstream.settimeout(None)
        except OSError:
            try:
                client.close()
            except OSError:
                pass
            conn.closed = time.time()
            return
        with self._lock:
            self._open += [client, upstream]
        done = threading.Event()

        def pump(src: socket.socket, dst: socket.socket, direction: str, sink: bytearray) -> None:
            try:
                while True:
                    data = src.recv(CHUNK)
                    if not data:
                        break
                    self._note(conn, direction, data, sink)
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                done.set()
                for sock in (src, dst):
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        a = threading.Thread(target=pump, args=(client, upstream, "c2s", conn.c2s), daemon=True)
        b = threading.Thread(target=pump, args=(upstream, client, "s2c", conn.s2c), daemon=True)
        a.start()
        b.start()
        a.join()
        b.join()
        conn.closed = time.time()
        for sock in (client, upstream):
            try:
                sock.close()
            except OSError:
                pass

    def _note(self, conn: Connection, direction: str, data: bytes, sink: bytearray) -> None:
        now = time.time()
        with self._lock:
            sink += data
            conn.chunks.append((now, direction, data))
            if self.record_path:
                line = json.dumps({"t": now, "conn": conn.number, "dir": direction,
                                   "b64": base64.b64encode(data).decode("ascii")})
                with open(self.record_path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")

    # ---------------------------------------------------------------------------------- reading
    def snapshot(self) -> List[Connection]:
        with self._lock:
            return list(self.connections)

    def all_bytes(self) -> bytes:
        """Every byte seen, both directions, connection by connection."""
        return b"".join(bytes(c.c2s) + bytes(c.s2c) for c in self.snapshot())


def read_record(path: str) -> List[Connection]:
    """The connections a tap in another process recorded to ``path``."""
    found: Dict[int, Connection] = {}
    try:
        handle = open(path, "r", encoding="utf-8")
    except OSError:
        return []
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue                    # a line still being written
            number = int(item["conn"])
            conn = found.setdefault(number, Connection(number=number, opened=float(item["t"])))
            data = base64.b64decode(item["b64"])
            (conn.c2s if item["dir"] == "c2s" else conn.s2c).extend(data)
            conn.chunks.append((float(item["t"]), item["dir"], data))
            conn.closed = float(item["t"])
    return [found[k] for k in sorted(found)]
