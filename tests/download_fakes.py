"""
A local HTTPS file server for the download tests (tests/test_downloader.py and the
auto-swap integration in tests/test_auto_swap.py).

The downloader accepts https:// URLs only, so the tests talk to a real TLS server on
127.0.0.1 with a throwaway self-signed certificate (made with ``cryptography``, which the
project already depends on); the matching client context is ``server.client_context``.
Everything is a real socket, so the failures the downloader has to survive (a connection cut
mid-body, a server that ignores Range, a 503, a stall) are the real ones, not a mock's idea.

:class:`FakeModelServer` serves ``files`` (path -> bytes) with HTTP Range support. Per-path
behaviour is set with :meth:`FakeModelServer.rule`:

    statuses=[503, 503]   answer the first requests for this path with these statuses (None: serve normally)
    retry_after=1         ... with a Retry-After header
    cut_after=N           send N body bytes then drop the connection ...
    cut_times=K           ... for the first K requests (default 1)
    ignore_range=True     answer a Range request with the whole file and 200
    delay=0.02            sleep this long after every 16 KiB sent (a slow server)
    stall=2.0             sleep this long before answering at all
    redirect="https://"   answer 302 to this URL
    body=b"..."           serve these bytes instead of ``files[path]``
    no_length=True        no Content-Length (the body ends when the connection closes)
    content_length=N      claim this Content-Length whatever the body is
    headers={...}         extra response headers
    extra=b"..."          bytes sent after the body

``server.requests`` is the log: a list of ``(path, range header or None)``.
"""

import datetime
import hashlib
import http.server
import ipaddress
import random
import re
import socket
import ssl
import threading
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CHUNK = 16 * 1024


def make_blob(size: int, seed: int = 0) -> bytes:
    """``size`` deterministic pseudo-random bytes."""
    return random.Random(seed).randbytes(size)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_certificate(directory) -> Tuple[str, str]:
    """A self-signed certificate for 127.0.0.1 and localhost; returns (cert path, key path)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                    x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                       critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=False,
                                     content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = str(directory / "server.pem"), str(directory / "server.key")
    with open(cert_path, "wb") as handle:
        handle.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as handle:
        handle.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    return cert_path, key_path


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:   # silence the default stderr line per request
        pass

    def setup(self) -> None:
        self.request.settimeout(10)
        self.request.do_handshake()          # in this thread, so one slow client cannot block the others
        super().setup()

    def _send_plain(self, status: int, headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        owner: "FakeModelServer" = self.server.owner
        path = urllib.parse.urlsplit(self.path).path
        wanted = self.headers.get("Range")
        with owner.lock:
            owner.requests.append((path, wanted))
            seen = sum(1 for p, _ in owner.requests if p == path)
            rule = owner.rules.setdefault(path, {})
            cutting = False
            if rule.get("cut_after") is not None and rule.get("_cuts", 0) < rule.get("cut_times", 1):
                rule["_cuts"] = rule.get("_cuts", 0) + 1
                cutting = True

        if rule.get("stall"):
            time.sleep(rule["stall"])
        if rule.get("redirect"):
            self._send_plain(302, {"Location": rule["redirect"]})
            return
        body = rule.get("body", owner.files.get(path))
        if body is None:
            self._send_plain(404)
            return
        statuses = rule.get("statuses", [])
        if seen <= len(statuses) and statuses[seen - 1] is not None:
            extra = {"Retry-After": str(rule["retry_after"])} if rule.get("retry_after") is not None else {}
            self._send_plain(statuses[seen - 1], extra)
            return

        start, status = 0, 200
        if wanted and not rule.get("ignore_range"):
            match = re.fullmatch(r"bytes=(\d+)-", wanted)
            start = int(match.group(1)) if match else 0
            if start >= len(body):
                self._send_plain(416, {"Content-Range": f"bytes */{len(body)}"})
                return
            status = 206
        payload = body[start:]

        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        if rule.get("no_length"):
            self.send_header("Connection", "close")
            self.close_connection = True
        else:
            self.send_header("Content-Length", str(rule.get("content_length", len(payload))))
        for header, value in rule.get("headers", {}).items():
            self.send_header(header, value)
        self.end_headers()
        try:
            sent = 0
            while sent < len(payload):
                if cutting and sent >= rule["cut_after"]:
                    self.wfile.flush()
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                end = min(len(payload), sent + CHUNK)
                if cutting:
                    end = min(end, rule["cut_after"])
                self.wfile.write(payload[sent:end])
                sent = end
                if rule.get("delay"):
                    self.wfile.flush()
                    time.sleep(rule["delay"])
            if rule.get("extra"):
                self.wfile.write(rule["extra"])
            self.wfile.flush()
        except OSError:
            self.close_connection = True      # the client went away (a cancelled download)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:   # a client hanging up is not news
        pass


class FakeModelServer:
    """See the module docstring. Use as a context manager or call start()/stop()."""

    def __init__(self, directory) -> None:
        self.files: Dict[str, bytes] = {}
        self.rules: Dict[str, dict] = {}
        self.requests: List[Tuple[str, Optional[str]]] = []
        self.lock = threading.Lock()
        cert_path, key_path = _make_certificate(directory)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)
        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._httpd.socket = server_context.wrap_socket(self._httpd.socket, server_side=True,
                                                       do_handshake_on_connect=False)
        self._httpd.owner = self
        self.port = self._httpd.server_address[1]
        self.client_context = ssl.create_default_context(cafile=cert_path)
        self.client_context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> "FakeModelServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "FakeModelServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def url(self, name: str) -> str:
        return f"https://127.0.0.1:{self.port}/{name}"

    def rule(self, name: str, **behaviour) -> None:
        with self.lock:
            self.rules[f"/{name}"] = dict(behaviour)

    def serve(self, name: str, data: bytes) -> dict:
        """Publish ``data`` as ``name``; returns its manifest entry."""
        self.files[f"/{name}"] = data
        return {"file": name, "url": self.url(name), "size": len(data), "sha256": sha256(data)}

    def count(self, name: str) -> int:
        with self.lock:
            return sum(1 for path, _ in self.requests if path == f"/{name}")

    def ranges(self, name: str) -> List[Optional[str]]:
        with self.lock:
            return [wanted for path, wanted in self.requests if path == f"/{name}"]
