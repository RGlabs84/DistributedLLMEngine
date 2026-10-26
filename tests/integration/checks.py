"""
Reading what a wire tap recorded, and the byte-exact questions the scenarios ask of it.

Everything works on raw bytes: an HTTP/1.1 request or answer as it crossed the loopback wire (chunked answers are
decoded, nothing else is interpreted). "Plaintext" below means any piece of what the user wrote or what the model
answered, found as bytes.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tests.integration import taps


def dechunk(body: bytes) -> bytes:
    """The payload of a Transfer-Encoding: chunked body (lenient about a body cut off in the middle)."""
    out = bytearray()
    pos = 0
    while pos < len(body):
        end = body.find(b"\r\n", pos)
        if end < 0:
            break
        try:
            size = int(body[pos:end].split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        start = end + 2
        out += body[start:start + size]
        pos = start + size + 2
    return bytes(out)


def split_message(raw: bytes) -> Tuple[str, Dict[str, str], bytes]:
    """(first line, headers with lower-case names, decoded body) of one HTTP message."""
    head, _sep, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name:
            headers[name.strip().lower()] = value.strip()
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = dechunk(body)
    return lines[0], headers, body


def requests_on(conn: taps.Connection) -> List[Tuple[str, Dict[str, str], bytes]]:
    """Every request carried by one connection (keep-alive connections carry several)."""
    data = bytes(conn.c2s)
    found = []
    # a new message starts at a request line
    # (a request that follows another on a keep-alive connection starts right after the previous body, not after a newline)
    starts = [m.start() for m in re.finditer(rb"(?:GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH) /[^\s]* HTTP/1\.[01]\r\n", data)]
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(data)
        line, headers, body = split_message(data[start:end])
        length = headers.get("content-length")
        if length and length.isdigit():
            body = body[:int(length)]
        found.append((line, headers, body))
    return found


def responses_on(conn: taps.Connection) -> List[Tuple[str, Dict[str, str], bytes]]:
    """Every answer carried by one connection, in order (a keep-alive connection carries several)."""
    data = bytes(conn.s2c)
    starts = [m.start() for m in re.finditer(rb"HTTP/1\.[01] \d{3} [A-Za-z ]*\r\n", data)]
    return [split_message(data[start:(starts[i + 1] if i + 1 < len(starts) else len(data))]) for i, start in enumerate(starts)]


def chat_posts(connections: Iterable[taps.Connection]) -> List[Dict[str, Any]]:
    """Each POST to /v1/chat/completions or /v1/completions as {line, headers, body, answer_*, conn}; the k-th answer on
    a connection belongs to its k-th request."""
    posts = []
    for conn in connections:
        answers = responses_on(conn)
        for index, (line, headers, body) in enumerate(requests_on(conn)):
            if line.startswith(("POST /v1/chat/completions", "POST /v1/completions")):
                status, answer_headers, answer_body = answers[index] if index < len(answers) else ("", {}, b"")
                posts.append({"line": line, "headers": headers, "body": body, "conn": conn,
                              "answer_status": status, "answer_headers": answer_headers, "answer_body": answer_body})
    return posts


def envelope(body: bytes) -> Optional[Dict[str, Any]]:
    """The inner object of a ``{"dlne_e2e": {...}}`` body, or None when it is anything else."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    if isinstance(parsed, dict) and list(parsed) == ["dlne_e2e"] and isinstance(parsed["dlne_e2e"], dict):
        return parsed["dlne_e2e"]
    return None


def sse_data_lines(body: bytes) -> List[str]:
    """The payload after ``data:`` on each line of an event stream."""
    lines = []
    for line in body.decode("utf-8", "replace").splitlines():
        if line.startswith("data:"):
            lines.append(line[5:].strip())
    return lines


def windows(text: str, size: int = 12, step: int = 3) -> List[str]:
    """Pieces of ``text`` long enough that finding one in unrelated bytes would be a coincidence: they hold at least
    six letters or digits and are not made only of JSON punctuation."""
    out = []
    for i in range(0, max(0, len(text) - size + 1), step):
        piece = text[i:i + size]
        if sum(ch.isalnum() for ch in piece) >= 6:
            out.append(piece)
    return out


def plaintext_found(blob: bytes, needles: Iterable[str]) -> List[str]:
    """Which of the needles appear in ``blob`` as bytes (UTF-8, and as JSON-escaped text, which is how a string
    would sit inside a request or answer body)."""
    hits = []
    for needle in needles:
        if not needle:
            continue
        variants = {needle.encode("utf-8"), json.dumps(needle, ensure_ascii=False)[1:-1].encode("utf-8"),
                    json.dumps(needle)[1:-1].encode("ascii")}
        if any(v and v in blob for v in variants):
            hits.append(needle)
    return hits
