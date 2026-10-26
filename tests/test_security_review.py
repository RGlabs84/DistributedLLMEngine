"""
Findings of the independent security review of the privacy stack (ids are SR-n, see the review report).

One test per finding, showing the bad behaviour.
  * A finding fixed in the same review is a normal test now: it fails again if the fix is undone.
  * A finding still open is ``@pytest.mark.xfail(strict=True, reason="SECURITY: SR-n")``: the suite stays
    green, and the day the fix lands the test XPASSes (strict makes that a failure), so the marker has to
    come off together with the fix.
  * Tests for the coordinator's router (engine/coding_router.py, not merged when this was written) skip
    until that module exists, then run.

The last section holds properties that were attacked and held ("checked and fine"), so they stay that way.
Nothing here uses the network or the real install (every file lives in tmp_path).
"""

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from engine import canary, downloader, e2e, privacy_policy, release, worker
from engine.canary import make_decoy, build_decoy_request

REPO = Path(__file__).resolve().parent.parent
EXTENSION = REPO / "vscode-extension" / "src"


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


# ==========================================================================================
# SR-01, SR-02: a canary must be a request the extension could have sent
# ==========================================================================================
#
# A member can tell a canary from real traffic if the two differ in anything it can see. These tests
# compare a canary body with what vscode-extension/src really builds, from the TypeScript source, so a
# change on either side that makes them differ fails here.

def _extension_system_prompt() -> str:
    source = (EXTENSION / "agent" / "agentLoop.ts").read_text(encoding="utf-8")
    at = source.index("private initSystemPrompt()")
    start = source.index("content: `", at) + len("content: `")
    end = start
    while not (source[end] == "`" and source[end - 1] != "\\"):
        end += 1
    prompt = source[start:end]
    assert "${" not in prompt, "the system prompt gained an interpolation; teach this test to evaluate it"
    return prompt.replace("\\`", "`")


def test_sr01_a_canary_carries_the_system_prompt_the_extension_sends():
    assert canary._AGENT_SYSTEM_PROMPT == _extension_system_prompt()


def _extension_request_fields() -> set:
    source = (EXTENSION / "client" / "dlneClient.ts").read_text(encoding="utf-8")
    start = source.index("async streamChat(")
    end = source.index("let forbiddenRetries", start)
    body = source[start:end]
    literal = re.search(r"const payload: Record<string, any> = \{(.*?)\n    \};", body, re.S).group(1)
    fields = set(re.findall(r"^\s*(\w+)\s*(?::|,|$)", literal, re.M))
    fields |= set(re.findall(r"payload\.(\w+)\s*=", body))
    return fields


def test_sr02_a_canary_has_no_field_the_extension_does_not_send():
    real = _extension_request_fields()
    assert {"messages", "stream", "temperature", "max_tokens", "tools"} <= real, real
    body = build_decoy_request(make_decoy(), random.Random(1))
    assert set(body) <= real, f"only a canary has: {sorted(set(body) - real)}"
    # the coordinator adds penalties to a PLAIN request; a sealed one reaches the member as the editor made it
    assert "frequency_penalty" not in body and "presence_penalty" not in body
    assert body["temperature"] == 0.2 and body["max_tokens"] == 4096 and body["stream"] is True


def test_sr03_a_canary_prompt_holds_the_decoy_link_and_no_decoy_key():
    """The extension's redactor empties every DLNE-format key before a request is sealed, so a live key in a
    prompt proves it came from the coordinator. The decoy is the link alone, and where a file would hold a
    key a canary shows ``[REDACTED]``, as a real request does (the JS test checks the redactor leaves each
    such line exactly as it is)."""
    for seed in range(300):
        decoy = make_decoy()
        body = build_decoy_request(decoy, random.Random(seed))
        text = json.dumps(body)
        assert canary.KEY_PREFIX not in text and "dlne_sk_" not in text and decoy.key not in text
        assert text.count(decoy.url) == 1
        assert canary.HIDDEN in body["messages"][-1]["content"], "the file shows a hidden credential, as a real one does"


def test_the_canary_tool_list_is_the_extensions_tool_list():
    """Checked and fine: the descriptions, in order, are the same text (a difference would be a tell)."""
    source = (EXTENSION / "agent" / "tools.ts").read_text(encoding="utf-8")
    start = source.index("static getToolDefinitions()")
    end = source.index("private static capOutput")
    real = [d.replace("\\'", "'") for d in re.findall(r"description: '((?:[^'\\]|\\.)*)'", source[start:end])]
    ours = []
    for tool in canary._AGENT_TOOLS:
        ours.append(tool["function"]["description"])
        ours += [p["description"] for p in tool["function"]["parameters"]["properties"].values()]
    assert ours == real


# ==========================================================================================
# SR-07, SR-08: the member runtime (engine/coding_member.py, engine/worker.py)
# ==========================================================================================

class _RecordingMember:
    def __init__(self):
        self.assigned, self.stopped = [], False

    def status(self):
        return {"model": None, "ready": False}

    def assign(self, tier):
        self.assigned.append(tier)

    def stop(self):
        self.stopped = True


@pytest.fixture
def node(monkeypatch, tmp_path):
    import engine.setup_core as setup_core
    monkeypatch.setattr(setup_core, "get_or_create_worker_id", lambda: "worker-under-test")
    monkeypatch.setattr(worker, "get_hardware_profile",
                        lambda: {"gpu": {"has_gpu": True, "vram_total_gb": 8.0}, "ram_gb": 30.0, "cpu_cores": 8,
                                 "cpu_threads": 16, "is_yielding": False})
    monkeypatch.setattr(worker, "_write_token_to_disk", lambda token: None)
    monkeypatch.setattr(worker, "get_worker_limits", lambda: {"network_key": "a-network-key-for-tests"})
    monkeypatch.setattr(worker, "get_base_dir", lambda: str(tmp_path))
    n = worker.WorkerNode("http://coordinator:11435")
    n.is_running = True
    yield n
    n._stopped = True


def _settle(node, timeout=10.0):
    end = time.monotonic() + timeout
    while node._role_busy and time.monotonic() < end:
        time.sleep(0.02)
    assert not node._role_busy, "the role switch never finished"


def test_sr07_a_node_with_no_usable_gpu_does_not_obey_a_coding_role(node, monkeypatch):
    """A heartbeat answer is the coordinator's word; a machine that chose "CPU only" (its profile then has no
    GPU) is still told ``role: coding`` by a coordinator that is wrong, hacked or malicious, and the worker
    starts a whole coding model for strangers on it."""
    node.hardware_profile = {"gpu": {"has_gpu": False, "vram_total_gb": 0}, "ram_gb": 30.0}
    started = []

    def start_coding_member():
        node.coding_member = _RecordingMember()
        started.append(True)
        return True

    monkeypatch.setattr(node, "start_coding_member", start_coding_member)
    node._follow_assignment({"role": "coding", "coding_tier": "any-tier"})
    _settle(node)
    assert not started and node.coding_member is None


def test_sr08_the_member_proxy_answers_only_requests_meant_for_it(tmp_path):
    """The proxy listens on loopback with no credential and does not look at ``Host`` or ``Origin``. A web page
    in the owner's browser can POST to it (a text/plain body needs no CORS preflight), and after a DNS
    rebinding can read the answer: the owner's GPU runs a stranger's prompts and the page learns the
    member's trust id from /health."""
    from engine.coding_member import CodingMember
    from engine.e2e import KeyRing
    from tests.coding_member_fakes import FakeUpstream, free_port

    up = FakeUpstream("up").start()
    member = CodingMember(str(tmp_path), lambda: [], port=free_port(), llama_port=up.port, keyring=KeyRing())
    try:
        assert member.start()
        member.process, member.ready = _Alive(), True
        member.serving, member.model = "coder-a", "coder-a.gguf"
        answer = httpx.post(f"http://127.0.0.1:{member.port}/v1/chat/completions", timeout=15, trust_env=False,
                            content=json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
                            headers={"Host": "rebind.attacker.example", "Origin": "https://attacker.example",
                                     "Content-Type": "text/plain"})
        assert answer.status_code in (403, 421), answer.status_code
        assert up.received == []
    finally:
        member.stop()
        up.stop()


class _Alive:
    """A llama-server process that is running (all the member asks of it)."""
    returncode = None
    pid = 0

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


# ==========================================================================================
# SR-09, SR-10: the coordinator's router (engine/coding_router.py; skipped until it is merged)
# ==========================================================================================

@pytest.fixture(autouse=True)
def _own_canary_book(tmp_path, monkeypatch):
    """The router asks the canary book who is flagged; give it an empty one in tmp_path."""
    monkeypatch.setattr(canary, "_book", canary.CanaryBook(path=str(tmp_path / "canaries.json")))


def _router_member(cr, **fields):
    values = dict(worker_id="w1", model="coder.gguf", tier="coder", ready=True, slots=2, busy=0, decode_tps=30.0,
                  prefill_tps=200.0, pubkey=b64(X25519PrivateKey.generate().public_key().public_bytes_raw()),
                  no_trace=True, base_url="http://127.0.0.1:9", floors_met=True, trust_id="a" * 32)
    values.update(fields)
    return cr.CodingMember(**values)


def test_sr09_route_lookups_nobody_uses_cannot_take_a_members_slots():
    """GET /v1/coding/route is open to anyone with an unspent free trial (the gate only *looks* at the quota
    for /v1/coding/), and each answer held a slot on the member for 30 s. Two lookups from a stranger who
    never sends a request took both slots of a two-slot member: everybody else got ``{"route": null}``, or a
    ``busy`` for a route they were already given. Four requests a minute kept one member out of service.
    Fixed: the coordinator issues a free-trial caller's routes with ``hold=False`` (and limits its lookups per
    client; tests/test_coding_endpoint.py checks both at the endpoint)."""
    cr = pytest.importorskip("engine.coding_router")
    router = cr.CodingRouter()
    member = _router_member(cr)
    for i in range(member.slots):
        chosen = router.pick([member], f"stranger-{i}", "open", need_pubkey=True)
        assert chosen is not None
        router.issue_route(chosen, f"stranger-{i}", "open", hold=False)
    assert router.pick([member], "somebody-real", "open", need_pubkey=True) is member


class _Endless(httpx.AsyncByteStream):
    def __init__(self, megabytes):
        self.megabytes = megabytes

    async def __aiter__(self):
        for _ in range(self.megabytes):
            yield b"x" * (1 << 20)


def test_sr10_a_member_cannot_make_the_coordinator_hold_an_unbounded_answer():
    """A non-streamed 200 is read with ``response.aread()`` and no limit (only error bodies are capped at 64 KiB)
    and the read timeout is per read, not for the whole answer: a member that sends gigabytes, or drips for
    hours, grows the coordinator's memory until it is killed. TheWall is that process."""
    cr = pytest.importorskip("engine.coding_router")
    router = cr.CodingRouter()
    member = _router_member(cr)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "application/json"}, stream=_Endless(48)))

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await router._open(client, member, "/v1/completions", b"{}")

    opened = asyncio.run(go())
    assert (not opened.ok) or len(opened.body or b"") <= 32 * (1 << 20), f"held {len(opened.body or b'') >> 20} MiB"


def test_the_router_never_widens_a_privacy_mode():
    """Checked and fine: a missing, odd or tightened mode is ``trusted``; only a member whose trust id the
    operator listed gets private work; a route issued under a strict mode cannot be relaxed by a header."""
    cr = pytest.importorskip("engine.coding_router")
    router = cr.CodingRouter()
    router.trusted_ids = {"b" * 32}
    stranger, friend = _router_member(cr, worker_id="stranger", trust_id="a" * 32), _router_member(cr, worker_id="friend", trust_id="b" * 32)
    for mode in (None, "", "Open", " open", "open,any", "ANY", 7, "trusted"):
        assert router.pick([stranger], "s", mode, need_pubkey=True) is None, mode
        assert router.pick([stranger, friend], "s", mode, need_pubkey=True) is friend, mode
    for mode in ("open", "any"):
        assert router.pick([stranger], "s", mode, need_pubkey=True) is stranger
    # a member that calls itself by a trusted worker id but has another identity is not trusted
    imposter = _router_member(cr, worker_id="friend", trust_id="c" * 32)
    assert router.pick([imposter], "s", "trusted", need_pubkey=True) is None
    route = cr._Route("worker:w1", "trusted", time.monotonic() + 60, False, None)
    assert router._effective_mode(route, "open") == "trusted" and router._effective_mode(route, "any") == "trusted"


# ==========================================================================================
# Checked and fine: the end-to-end layer, trust, the downloader
# ==========================================================================================

LOW_ORDER = [bytes(32), b"\x01" + bytes(31),
             bytes.fromhex("e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800"),
             bytes.fromhex("5f9c95bca3508c24b1d0b1559c83ef5b04445cc4581c8e86d8224eddd09f1157"),
             b"\xec" + b"\xff" * 30 + b"\x7f", b"\xed" + b"\xff" * 30 + b"\x7f", b"\xee" + b"\xff" * 30 + b"\x7f"]


def test_e2e_every_low_order_point_is_refused_as_member_key_and_as_client_key():
    priv, pub = e2e.generate_keypair()
    good, _ = e2e.seal_request({"a": 1}, pub)
    for point in LOW_ORDER:
        with pytest.raises(e2e.E2EError) as sealing:
            e2e.seal_request({"a": 1}, b64(point))
        assert sealing.value.code == "key"
        forged = json.loads(json.dumps(good))
        forged["dlne_e2e"]["epk"] = b64(point)
        with pytest.raises(e2e.E2EError) as opening:
            e2e.open_request(forged, priv, replay_guard=None)
        assert opening.value.code == "key"


def test_e2e_a_request_opens_once_and_a_forged_copy_cannot_burn_it():
    priv, pub = e2e.generate_keypair()
    guard = e2e.ReplayGuard()
    envelope, _ = e2e.seal_request({"a": 1}, pub)
    forged = json.loads(json.dumps(envelope))
    ct = base64.b64decode(forged["dlne_e2e"]["ct"])
    forged["dlne_e2e"]["ct"] = b64(ct[:-1] + bytes([ct[-1] ^ 1]))
    with pytest.raises(e2e.E2EError) as bad:
        e2e.open_request(forged, priv, replay_guard=guard)       # same epk, damaged ciphertext
    assert bad.value.code == "auth"
    body, _ = e2e.open_request(envelope, priv, replay_guard=guard)
    assert body == {"a": 1}
    with pytest.raises(e2e.E2EError) as again:
        e2e.open_request(envelope, priv, replay_guard=guard)
    assert again.value.code == "replay"


def test_e2e_request_and_response_keys_and_aads_cannot_stand_in_for_each_other():
    priv, pub = e2e.generate_keypair()
    envelope, resp_key = e2e.seal_request({"a": 1}, pub)
    inner = envelope["dlne_e2e"]
    # the request ciphertext does not open as a response chunk under either key, nor with the other AAD
    for key in (resp_key, e2e.derive_stream_key(resp_key, bytes(16))):
        with pytest.raises(e2e.E2EError):
            e2e.open_payload(key, 0, inner["ct"])
    sealer = e2e.ResponseSealer(resp_key)
    chunk = sealer.seal(b'{"x":1}')
    assert e2e.ResponseOpener(resp_key).open(chunk) is not None
    # a response chunk replayed into the next request's response (same request key, other salt) fails
    other = e2e.ResponseSealer(resp_key).seal(b'{"x":1}')
    opener = e2e.ResponseOpener(resp_key)
    opener.open(chunk)
    with pytest.raises(e2e.E2EError):
        opener.open({"n": 1, "ct": other["ct"]})


def test_e2e_a_stream_is_complete_only_after_its_sealed_end():
    key = bytes(range(32))
    sealer = e2e.ResponseSealer(key)
    frames = [sealer.seal(b'{"n":1}'), sealer.seal(b'{"n":2}'), sealer.finish()]
    full = e2e.ResponseOpener(key)
    for frame in frames:
        full.open(frame)
    assert full.finished
    cut = e2e.ResponseOpener(key)
    for frame in frames[:2]:
        cut.open(frame)
    with pytest.raises(e2e.E2EError) as truncated:
        cut.close()
    assert truncated.value.code == "truncated"
    for bad in ([frames[0], frames[2]], [frames[1], frames[0]], [frames[0], frames[0]]):
        opener = e2e.ResponseOpener(key)
        with pytest.raises(e2e.E2EError):
            for frame in bad:
                opener.open(frame)


def test_e2e_base64_has_one_spelling():
    priv, pub = e2e.generate_keypair()
    envelope, _ = e2e.seal_request({"a": 1}, pub)
    raw = base64.b64decode(envelope["dlne_e2e"]["nonce"])
    spellings = [envelope["dlne_e2e"]["nonce"].rstrip("="), envelope["dlne_e2e"]["nonce"].replace("=", "") + "\n",
                 base64.urlsafe_b64encode(raw).decode()]
    for spelling in spellings:
        if spelling == envelope["dlne_e2e"]["nonce"]:
            continue
        twisted = json.loads(json.dumps(envelope))
        twisted["dlne_e2e"]["nonce"] = spelling
        with pytest.raises(e2e.E2EError):
            e2e.open_request(twisted, priv, replay_guard=None)


def test_trust_is_read_from_the_trust_id_alone():
    listed = "a" * 32
    for member in ({"worker_id": listed}, {"worker_id": listed, "trust_id": None}, {"worker_id": listed, "trust_id": ""},
                   SimpleNamespace(worker_id=listed), {"trust_id": "a" * 31}, {"trust_id": "A" * 32}):
        assert not privacy_policy.allowed(member, "trusted", listed), member
    assert privacy_policy.allowed({"trust_id": listed}, "trusted", listed)
    assert privacy_policy.allowed({"trust_id": listed}, "trusted", f"x, {listed}")
    assert not privacy_policy.allowed({"trust_id": "a" * 8}, "trusted", listed)          # never a substring
    for mode in (None, "", "Open", " open", "open,any", 3, "TRUSTED", "anyone"):
        assert not privacy_policy.allowed({"trust_id": "d" * 32}, mode, listed), mode


def test_downloader_refuses_what_a_manifest_must_not_say(tmp_path):
    good = {"file": "model-00001-of-00002.gguf", "url": "https://example.org/m.gguf", "size": 10, "sha256": "ab" * 32}
    assert downloader.validate_files([good])[0]["file"] == good["file"]
    bad_names = ["../x.gguf", "a/b.gguf", "a\\b.gguf", "x.gguf.part", "x.gguf.lock", "x.gguf.dlne-sha256", "x.tmp", "con.gguf",
                 "NUL", "x.gguf.", ".hidden", "x y.gguf", "x\x00.gguf", "é.gguf", "a" * 201, "", None, 7, "x..gguf", "C:x.gguf"]
    for name in bad_names:
        with pytest.raises(downloader.DownloadError):
            downloader.validate_files([{**good, "file": name}])
    bad_urls = ["http://example.org/m", "file:///etc/passwd", "ftp://example.org/m", "https:///m", "https://user:pw@example.org/m",
                "https://user@example.org/m", "https://example.org/m\n", "https://exa mple.org/m", "//example.org/m", "", None]
    for url in bad_urls:
        with pytest.raises(downloader.DownloadError):
            downloader.validate_files([{**good, "url": url}])
    for field, value in (("size", 0), ("size", -1), ("size", True), ("size", 1.5), ("sha256", "ab" * 31), ("sha256", "zz" * 32), ("sha256", None)):
        with pytest.raises(downloader.DownloadError):
            downloader.validate_files([{**good, field: value}])
    with pytest.raises(downloader.DownloadError):                   # one bad part refuses the whole call
        downloader.validate_files([good, {**good, "file": "../x"}])
    with pytest.raises(downloader.DownloadError):                   # the same part twice, even in another case
        downloader.validate_files([good, {**good, "file": good["file"].upper()}])


def test_downloader_will_not_write_through_a_planted_symlink(tmp_path):
    dest = tmp_path / "models"
    dest.mkdir()
    elsewhere = tmp_path / "elsewhere.bin"
    elsewhere.write_bytes(b"precious")
    (dest / "m.gguf").symlink_to(elsewhere)
    result = downloader.fetch_verified([{"file": "m.gguf", "url": "https://example.invalid/m.gguf", "size": 8,
                                         "sha256": hashlib.sha256(b"precious").hexdigest()}], str(dest), retries=0)
    assert not result.ok and "outside" in result.reason and result.lasting
    assert elsewhere.read_bytes() == b"precious"


def test_downloader_a_wrong_download_never_lands_under_the_real_name(tmp_path, monkeypatch):
    dest = tmp_path / "models"
    payload = b"not the signed bytes"

    class Reply:
        status = 200
        headers = {"Content-Length": str(len(payload))}

        def __init__(self):
            self._left = payload

        def read1(self, n):
            chunk, self._left = self._left[:n], self._left[n:]
            return chunk

        read = read1

        def close(self):
            pass

    monkeypatch.setattr(downloader._Job, "_open", lambda self, entry, offset: Reply())
    entry = {"file": "m.gguf", "url": "https://example.org/m.gguf", "size": len(payload), "sha256": hashlib.sha256(b"signed").hexdigest()}
    result = downloader.fetch_verified([entry], str(dest), retries=0)
    assert not result.ok and "SHA-256" in result.reason
    assert sorted(os.listdir(dest)) == [], "neither a real file nor a .part is left"


def test_an_engine_zip_may_only_hold_flat_files_under_bin():
    names = ["bin/ggml-rpc-server", "bin/libggml.so", "bin/../../evil", "/etc/passwd", "bin/sub/x", "bin\\x", "../bin/x", "x"]
    assert release.rpc_members(names) == ["bin/../../evil", "/etc/passwd", "bin/sub/x", "bin\\x", "../bin/x", "x"]
