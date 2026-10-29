"""
Tests for engine/canary.py: the decoy credentials and the requests that carry them, who is sent a
canary and who never is, detection through a decoy key and through a decoy link, flags that survive
a restart, and the coordinator's side (the boring /c/{token} page, the key tripwire, the admin view).

Members are identified by trust id, never by worker id (contract sections 1 and 2): the tests build
members with a verified trust id, a worker id that is only a label, and a real E2E key ring.

Nothing here touches the real install: every book lives in tmp_path, the clock is fake, and the
module-wide book is swapped for the test's own and put back afterwards.
"""

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import secrets
import stat
from dataclasses import dataclass
from typing import Any, Optional

import pytest
from httpx import ASGITransport, AsyncClient

import engine.config as engine_config
from engine import api_keys, canary, coordinator, e2e
from engine.canary import (
    CanaryBook,
    CanaryScheduler,
    Decoy,
    KEY_PREFIX,
    build_decoy_request,
    make_decoy,
)
from engine.no_trace import forward_headers

KEY_RE = re.compile(r"dlne_ck_[0-9a-f]{48}")
URL_RE = re.compile(r"https://dlne\.rossgilsonlabs\.dev/c/([A-Za-z0-9_-]+)")
ADMIN_KEY = "dlne_ak_canary_test_key"
LOOPBACK = ("127.0.0.1", 40000)
REMOTE = ("203.0.113.50", 40000)          # TEST-NET-3: never local
SNOOPER = ("198.51.100.77", 40000)        # TEST-NET-2
T0 = 1_760_000_000.0


def tid(name):
    """A member's trust id, as the router derives one: 32 hex characters of a hash of its identity."""
    return hashlib.sha256(f"identity:{name}".encode()).hexdigest()[:32]


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def book(tmp_path, clock, monkeypatch):
    """A book in tmp_path, installed as the process-wide one; the previous one is put back."""
    monkeypatch.setattr(canary, "get_base_dir", lambda: str(tmp_path))
    fresh = CanaryBook(str(tmp_path / "config" / "canaries.json"), clock=clock)
    previous = canary.set_book(fresh)
    yield fresh
    canary.set_book(previous)


def reopen(book, clock):
    """What a restart does: a new book reading the same file."""
    return CanaryBook(book.path, clock=clock)


def assert_no_decoy_key(body):
    """No decoy key is in any prompt (SR-03): the extension's redactor empties every DLNE-format key, so a live
    one in a request would prove the request came from the coordinator."""
    text = json.dumps(body)
    assert KEY_PREFIX not in text and not KEY_RE.findall(text) and "dlne_sk_" not in text


def body_token(body):
    found = URL_RE.findall(json.dumps(body))
    assert len(found) == 1, found
    return found[0]


def saved(book):
    return json.load(open(book.path, encoding="utf-8"))


def only_record(book):
    return next(iter(saved(book)["canaries"].values()))


# ==========================================================================================
# Fakes: a router with members that really open what they are sent
# ==========================================================================================

@dataclass
class FakeMember:
    """A coding member as the router lists it: worker id (a label), verified trust id, E2E key ring."""
    worker_id: str
    ready: bool = True
    busy: int = 0
    slots: int = 2
    local: bool = False
    keyed: bool = True              # False: no pubkey in the heartbeat
    verified: bool = True           # False: the identity signature never checked out, so no trust id
    active: bool = True             # the router re-stamps last_served_at as "just served" while True
    last_served_at: Optional[float] = None
    trust_id: Optional[str] = None
    ring: Any = None
    pubkey: str = ""

    def __post_init__(self):
        self.ring = e2e.KeyRing()
        self.pubkey = self.ring.public_b64 if self.keyed else ""
        if not self.verified:
            self.trust_id = None
        elif self.trust_id is None:
            self.trust_id = tid(self.worker_id)
        if self.active and self.last_served_at is None:
            self.last_served_at = T0 - 60


def sse(resp_key, chunks=("Sure", ", here", " it is."), sealed_end=True, plain_done=True):
    """What a real member streams back: sealed chunks, the sealed end, the plain [DONE]."""
    sealer = e2e.ResponseSealer(resp_key)
    out = []
    for text in chunks:
        payload = json.dumps({"choices": [{"delta": {"content": text}}]}).encode()
        out.append("data: " + json.dumps({"dlne_e2e": sealer.seal(payload)}, separators=(",", ":")) + "\n\n")
    if sealed_end:
        out.append("data: " + json.dumps({"dlne_e2e": sealer.finish()}, separators=(",", ":")) + "\n\n")
    if plain_done:
        out.append("data: [DONE]\n\n")
    return "".join(out)


def field(member, name):
    return member.get(name) if isinstance(member, dict) else getattr(member, name, None)


class FakeRouter:
    """What the scheduler needs from the router: the members, and a way to forward bytes to one.

    ``forward`` plays the member: it opens the sealed request with the member's key ring (so a
    canary that is not sealed properly fails here) and streams a sealed answer back."""

    def __init__(self, members, accept=True, answer=None, as_dicts=False, clock=None):
        self.members_list = list(members)
        self.sent = []           # (trust id, the opened request body)
        self.wire = []           # (trust id, bytes on the wire, headers)
        self.targets = []        # the member objects forward() was given
        self.accept = accept
        self.answer = answer     # None: a proper answer; or a function (resp_key) -> response
        self.as_dicts = as_dicts
        self.clock = clock

    def members(self):
        for m in self.members_list:
            if self.clock is not None and getattr(m, "active", False):
                m.last_served_at = self.clock() - 30           # it is in use right now
        if self.as_dicts:
            return [{"worker_id": m.worker_id, "trust_id": m.trust_id, "pubkey": m.pubkey, "ready": m.ready,
                     "busy": m.busy, "slots": m.slots, "local": m.local, "last_served_at": m.last_served_at}
                    for m in self.members_list]
        return list(self.members_list)

    async def forward(self, member, wire, headers):
        self.targets.append(member)
        real = next(m for m in self.members_list if getattr(m, "pubkey", None) == field(member, "pubkey"))
        self.wire.append((real.trust_id, wire, dict(headers)))
        body, resp_key = real.ring.open_request(json.loads(wire))
        self.sent.append((real.trust_id, body))
        if not self.accept:
            raise ConnectionError("member busy")
        return self.answer(resp_key) if self.answer else sse(resp_key).encode()


class ForcedRng(random.Random):
    """random() returns a fixed number (0.0: every coin comes up 'send'; 0.99: none does)."""

    def __init__(self, value=0.0):
        super().__init__(7)
        self.value = value

    def random(self):
        return self.value


def scheduler(router, book, clock, *, trusted=(), per_day=4, rng=None, interval=60.0, **kw):
    router.clock = clock
    return CanaryScheduler(router.members, router.forward, book=book, trusted=lambda: set(trusted),
                           per_day=lambda: per_day, interval=interval, clock=clock,
                           rng=rng or ForcedRng(0.0), **kw)


# ==========================================================================================
# Generation
# ==========================================================================================

def test_a_decoy_has_a_dlne_style_key_and_a_link_on_the_public_address():
    d = make_decoy()
    assert re.fullmatch(r"dlne_ck_[0-9a-f]{48}", d.key)
    assert d.key.startswith(KEY_PREFIX)
    assert d.url == f"https://dlne.rossgilsonlabs.dev/c/{d.token}"
    assert re.fullmatch(r"[A-Za-z0-9_-]{24}", d.token)


def test_decoys_are_unique_and_unguessable_in_size():
    decoys = [make_decoy() for _ in range(2000)]
    assert len({d.key for d in decoys}) == 2000
    assert len({d.token for d in decoys}) == 2000
    assert len(secrets.token_urlsafe(18)) == len(decoys[0].token)      # 144 bits of token, 192 of key


def test_the_key_looks_like_a_real_one_apart_from_its_marker(tmp_path):
    real, _meta = api_keys.ApiKeyManager(file_path=str(tmp_path / "api_keys.json")).generate_key("t")
    assert re.fullmatch(r"dlne_sk_[0-9a-f]{48}", real)
    assert len(make_decoy().key) == len(real)


def test_a_decoy_request_is_shaped_like_the_extensions_agent_request():
    body = build_decoy_request(make_decoy(), random.Random(3))
    assert body["stream"] is True
    assert body["temperature"] == 0.2 and body["max_tokens"] == 10240   # the extension's AGENT_MAX_TOKENS (10240 since 1.4.2)
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"].startswith("You are the DLNE coding agent")
    assert body["messages"][-1]["role"] in ("user", "tool")
    names = [t["function"]["name"] for t in body["tools"]]
    assert names == ["read_file", "write_file", "edit_file_diff", "list_directory", "search_codebase",
                     "run_terminal_command", "get_workspace_diagnostics", "remember"]
    json.dumps(body)       # plain JSON all the way down


def _is_context(message):
    return message["role"] == "user" and message["content"].startswith("<dlne_context>")


def _context_bodies(count=600):
    """(seed, body) for the first ``count`` seeds that carry a context message."""
    found = []
    for seed in range(count):
        body = build_decoy_request(make_decoy(), random.Random(seed))
        if _is_context(body["messages"][1]):
            found.append((seed, body))
    return found


def test_the_system_prompt_is_the_agent_memory_contracts_text():
    """docs/agent-memory.md section 2 is the source and canary-parity.test.js compares the two word for word (the
    docs folder is not on the CI copy); this checks what the memory work added."""
    prompt = canary._AGENT_SYSTEM_PROMPT
    assert prompt.startswith("You are the DLNE coding agent, a software engineering assistant")
    assert '- remember(fact, scope, replaces): without being asked, keep what a later session would otherwise have to rediscover' in prompt
    assert "Your memory: a <dlne_context> message, when there is one" in prompt and "<dlne_working_memory>" in prompt
    assert prompt.endswith("Begin with a short <think> plan.")


def test_remember_is_the_eighth_and_last_tool_with_a_scope_of_project_or_user():
    tools = canary._AGENT_TOOLS
    assert len(tools) == 8 and tools[-1]["function"]["name"] == "remember"
    parameters = tools[-1]["function"]["parameters"]
    assert parameters["required"] == ["fact", "scope"]
    assert parameters["properties"]["scope"]["enum"] == ["project", "user"]
    assert set(parameters["properties"]) == {"fact", "scope", "replaces"}   # replaces: update or remove a saved fact


def test_about_two_in_five_canaries_carry_a_context_message_at_index_one():
    carrying = 0
    for seed in range(1000):
        body = build_decoy_request(make_decoy(), random.Random(seed))
        has = [i for i, m in enumerate(body["messages"]) if _is_context(m)]
        assert has in ([], [1]), "a notes message is at index 1 or nowhere, and there is only one"
        carrying += bool(has)
    assert 0.34 <= carrying / 1000 <= 0.46, carrying


HEADINGS = ["## Project instructions (AGENTS.md)", "## Project memory (.dlne/memory.md)",
            "## The user's preferences (~/.dlne/memory.md)"]


def test_a_context_message_has_exactly_the_layout_of_the_extensions_block():
    """docs/agent-memory.md section 7.3: the open tag and intro, then one to three sections in order, one empty line
    between them and none between a heading and its text, the close tag on the line after the last text."""
    for _, body in _context_bodies():
        content = body["messages"][1]["content"]
        assert body["messages"][1]["role"] == "user"
        assert content.startswith("<dlne_context>\nStanding notes for this workspace, loaded by the DLNE extension. "
                                  "They are not a request from the user.\n\n## ")
        assert content.endswith("\n</dlne_context>") and not content.endswith("\n\n</dlne_context>")
        inner = content[len("<dlne_context>\n"):-len("\n</dlne_context>")]
        sections = inner.split("\n\n## ")[1:]
        assert 1 <= len(sections) <= 3
        seen = []
        for section in sections:
            heading, _, text = ("## " + section).partition("\n")
            assert heading in HEADINGS and text and text == text.strip()
            seen.append(HEADINGS.index(heading))
        assert seen == sorted(set(seen)), "each section once, in the contract's order"


def test_no_canary_carries_working_memory():
    for seed in range(400):
        body = build_decoy_request(make_decoy(), random.Random(seed))
        for message in body["messages"][1:]:
            assert "<dlne_working_memory>" not in message["content"]
        assert sum(_is_context(m) for m in body["messages"]) <= 1


def test_context_contents_are_plausible_varied_and_hold_nothing_a_redactor_or_a_reader_would_notice():
    blocks, combos = set(), set()
    for _, body in _context_bodies(1500):
        content = body["messages"][1]["content"]
        blocks.add(content)
        combos.add(tuple(h for h in HEADINGS if h in content))
        assert "{" not in content and "}" not in content, "an unfilled placeholder"
        # Nothing that links, hides or names a credential: the one link of a canary is in its file.
        assert "http" not in content and canary.HIDDEN not in content and not re.search(r"dlne_[a-z]{2}_", content)
        assert not re.search(r"(?i)secret|password|token|api[_ -]?key|private key", content)
        assert len(content) < 3000
    assert len(blocks) > 150, "no fixed text to fingerprint"
    assert len(combos) == 7, "every combination of the three sections comes up"


def test_the_notes_fit_the_canarys_language_and_sometimes_name_its_file():
    named = 0
    for _, body in _context_bodies(800):
        content = body["messages"][1]["content"]
        last_user = [m for m in body["messages"] if m["role"] == "user"][-1]["content"]
        match = re.search(r"`([^`]+)`:|Open (\S+)\.", last_user)
        path = match.group(1) or match.group(2)
        named += path in content
        if path.endswith(".go"):
            assert "npm" not in content and "pytest" not in content, content
        if path.endswith(".py"):
            assert "go test" not in content and "npm ci" not in content, content
    assert named > 20


def test_a_context_message_changes_nothing_else_about_the_request():
    for _, body in _context_bodies(200)[:40]:
        assert set(body) == {"messages", "stream", "temperature", "max_tokens", "tools"}
        assert body["max_tokens"] == 10240 and body["tools"] == canary._AGENT_TOOLS


def test_the_decoy_link_appears_exactly_once_and_no_decoy_key_is_in_the_prompt():
    for seed in range(80):
        d = make_decoy()
        body = build_decoy_request(d, random.Random(seed))
        assert body_token(body) == d.token
        assert_no_decoy_key(body)
        assert d.key not in json.dumps(body)
        # where a file would hold a key, it shows what the extension's redactor leaves there
        assert canary.HIDDEN in body["messages"][-1]["content"]


def test_the_decoy_sits_in_a_file_the_model_is_asked_to_change_never_in_the_system_prompt():
    for seed in range(40):
        d = make_decoy()
        body = build_decoy_request(d, random.Random(seed))
        assert d.url not in body["messages"][0]["content"]
        carrier = body["messages"][-1]["content"]          # the pasted file, or the read_file result
        assert d.url in carrier and d.key not in carrier
        user = [m for m in body["messages"] if m["role"] == "user"][-1]["content"]
        assert len(user) > 40, "a request in plain words as well as the file"


def test_both_shapes_and_every_file_template_come_up():
    shapes, paths = set(), set()
    for seed in range(300):
        body = build_decoy_request(make_decoy(), random.Random(seed))
        shapes.add(body["messages"][-1]["role"])
        last_user = [m for m in body["messages"] if m["role"] == "user"][-1]["content"]
        match = re.search(r"`([^`]+)`:|Open (\S+)\.", last_user)
        paths.add(match.group(1) or match.group(2))
    assert shapes == {"user", "tool"}
    assert len({p.rsplit(".", 1)[-1] for p in paths}) >= 5      # py, ts, sh, yml, json, go
    assert len(paths) >= 20                                      # names vary, not one fixed fingerprint


def test_requests_differ_in_size_and_carry_ordinary_code_around_the_decoy():
    sizes, with_filler = set(), 0
    for seed in range(300):
        body = build_decoy_request(make_decoy(), random.Random(seed))
        sizes.add(len(json.dumps(body)))
        carrier = body["messages"][-1]["content"]
        with_filler += any(marker in carrier for blocks in canary._FILLER.values() for marker in
                           (b.split("\n")[0] for b in blocks))
    assert len(sizes) > 150, "no fixed length to fingerprint"
    assert with_filler > 60


def test_conversation_depth_varies_and_earlier_turns_look_like_the_agents():
    depths = set()
    for seed in range(300):
        body = build_decoy_request(make_decoy(), random.Random(seed))
        messages = body["messages"]
        final = 1 if messages[-1]["role"] == "user" else 3        # the file, or the read_file exchange
        first = 2 if _is_context(messages[1]) else 1               # the notes message is not a turn
        earlier = messages[first:len(messages) - final]
        depths.add(len(earlier) // 2)
        assert len(earlier) % 2 == 0
        for i, m in enumerate(earlier):
            assert m["role"] == ("user" if i % 2 == 0 else "assistant")
            if m["role"] == "assistant":
                assert m["content"].startswith("<think>") and "</think>" in m["content"]
        assert "tool_calls" not in "".join(m["role"] for m in earlier)
    assert depths == {0, 1, 2, 3}


def test_the_read_file_exchange_is_well_formed():
    for seed in range(200):
        body = build_decoy_request(make_decoy(), random.Random(seed))
        if body["messages"][-1]["role"] != "tool":
            continue
        user, assistant, tool = body["messages"][-3:]
        call = assistant["tool_calls"][0]
        assert user["role"] == "user" and assistant["role"] == "assistant"
        assert assistant["content"].startswith("<think>")
        assert call["function"]["name"] == "read_file" and tool["tool_call_id"] == call["id"]
        assert re.fullmatch(r"[A-Za-z0-9]{32}", call["id"])
        assert json.loads(call["function"]["arguments"])["path"]
        assert call["function"]["arguments"].startswith('{"path":"'), "compact, like JSON.stringify"
        assert tool["content"].startswith("1: ")
        return
    pytest.fail("no read_file shaped request in 200 tries")


def test_the_same_seed_gives_the_same_request_and_other_seeds_differ():
    d = Decoy(token="t" * 24, key=KEY_PREFIX + "a" * 48, url="https://dlne.rossgilsonlabs.dev/c/" + "t" * 24, id="x")
    assert build_decoy_request(d, random.Random(5)) == build_decoy_request(d, random.Random(5))
    assert build_decoy_request(d, random.Random(5)) != build_decoy_request(d, random.Random(6))


def test_issuing_records_digests_only_never_the_key_or_the_link_token(book):
    d = book.issue(tid("a"), "worker-a")
    raw = open(book.path, encoding="utf-8").read()
    assert d.key not in raw and d.token not in raw
    assert tid("a") in raw and "worker-a" in raw
    assert json.loads(raw)["version"] == 2


@pytest.mark.parametrize("bad", ["", None, 5, "worker-a", "x" * 129, "A" * 32, "a" * 31, "a" * 33, "g" * 32,
                                 tid("a").upper(), " " + tid("a")])
def test_a_canary_needs_a_verified_trust_id_a_worker_id_will_not_do(book, bad):
    with pytest.raises(ValueError):
        book.issue(bad, "label")


# ==========================================================================================
# Sending: who gets a canary
# ==========================================================================================

@pytest.mark.anyio
async def test_a_canary_goes_to_each_eligible_member_and_carries_that_members_decoy(book, clock):
    router = FakeRouter([FakeMember("a"), FakeMember("b")])
    sent = await scheduler(router, book, clock).tick()
    assert sorted(sent) == sorted([tid("a"), tid("b")])
    assert [t for t, _ in router.sent] == [tid("a"), tid("b")]
    for trust_id, body in router.sent:
        assert book.fetched(body_token(body), "198.51.100.1") == trust_id      # the link is bound to that member
    tokens = {body_token(b) for _, b in router.sent}
    assert len(tokens) == 2, "each member gets its own decoy"


@pytest.mark.anyio
async def test_the_router_is_handed_the_member_itself_to_forward_to(book, clock):
    a, b = FakeMember("a"), FakeMember("b")
    router = FakeRouter([a, b])
    await scheduler(router, book, clock).tick()
    assert [t is a or t is b for t in router.targets] == [True, True]
    assert {id(t) for t in router.targets} == {id(a), id(b)}


@pytest.mark.anyio
async def test_trusted_members_are_never_sent_one_and_trust_is_by_trust_id(book, clock):
    router = FakeRouter([FakeMember("trusted-1"), FakeMember("stranger")])
    sent = await scheduler(router, book, clock, trusted={tid("trusted-1")}).tick()
    assert sent == [tid("stranger")]
    # a worker id in the trusted list means nothing: anyone can claim a worker id
    clock.advance(canary.MIN_GAP_SECONDS + 1)
    router2 = FakeRouter([FakeMember("trusted-1"), FakeMember("stranger")])
    sent = await scheduler(router2, book, clock, trusted={"trusted-1"}).tick()
    assert sorted(sent) == sorted([tid("trusted-1"), tid("stranger")])


@pytest.mark.anyio
async def test_the_local_engine_is_never_sent_one(book, clock):
    router = FakeRouter([FakeMember("local", local=True), FakeMember("stranger")])
    assert await scheduler(router, book, clock).tick() == [tid("stranger")]
    only_local = FakeRouter([FakeMember("thewall", local=True)])
    assert await scheduler(only_local, book, clock).tick() == []
    assert only_local.sent == []


@pytest.mark.anyio
async def test_dict_shaped_members_work_the_same(book, clock):
    router = FakeRouter([FakeMember("d1"), FakeMember("d2", local=True), FakeMember("d3", busy=2)], as_dicts=True)
    assert all(isinstance(m, dict) for m in router.members())
    assert await scheduler(router, book, clock).tick() == [tid("d1")]
    assert all(isinstance(t, dict) for t in router.targets)


@pytest.mark.anyio
async def test_flagged_members_are_never_sent_one(book, clock):
    d = book.issue(tid("bad"), "bad")
    book.fetched(d.token, "198.51.100.9")
    router = FakeRouter([FakeMember("bad"), FakeMember("good")])
    assert await scheduler(router, book, clock).tick() == [tid("good")]


@pytest.mark.anyio
async def test_members_not_ready_or_with_no_free_slot_are_skipped(book, clock):
    router = FakeRouter([FakeMember("warming", ready=False), FakeMember("full", busy=2, slots=2),
                         FakeMember("one-free", busy=1, slots=2)])
    assert await scheduler(router, book, clock).tick() == [tid("one-free")]


@pytest.mark.anyio
@pytest.mark.parametrize("trust_id", [None, "", "worker-a", "A" * 32, "ab" * 8, 5])
async def test_a_member_without_a_verified_trust_id_never_gets_one(book, clock, trust_id):
    m = FakeMember("a", verified=False)
    m.trust_id = trust_id
    router = FakeRouter([m, FakeMember("ok")])
    assert await scheduler(router, book, clock).tick() == [tid("ok")]
    assert [t for t, _, _ in router.wire] == [tid("ok")]


@pytest.mark.anyio
async def test_members_without_a_usable_entry_are_skipped(book, clock):
    router = FakeRouter([{"nothing": 1}, {"worker_id": 5}, {"trust_id": tid("x")}, FakeMember("ok")])
    assert await scheduler(router, book, clock).tick() == [tid("ok")]


@pytest.mark.anyio
async def test_one_machine_listed_twice_is_sent_one_canary(book, clock):
    """Two worker ids, one identity key: the same machine."""
    first, second = FakeMember("name-one", trust_id=tid("same")), FakeMember("name-two", trust_id=tid("same"))
    router = FakeRouter([first, second])
    assert await scheduler(router, book, clock).tick() == [tid("same")]
    assert len(router.wire) == 1


@pytest.mark.anyio
async def test_renaming_does_not_shake_off_a_flag_and_claiming_a_name_frames_nobody(book, clock):
    d = book.issue(tid("snooper"), "the-snoopers-name")
    book.presented_key(d.key, "198.51.100.9")
    # the same machine under a new worker id: still flagged, still sent nothing
    renamed = FakeMember("innocent-looking-name", trust_id=tid("snooper"))
    # another machine that claims the snooper's old worker id: not flagged
    claimant = FakeMember("the-snoopers-name", trust_id=tid("innocent"))
    assert canary.is_flagged(renamed) and not canary.is_flagged(claimant)
    router = FakeRouter([renamed, claimant])
    assert await scheduler(router, book, clock).tick() == [tid("innocent")]
    # and when a decoy sent to one machine trips, the other machine with the same worker id is untouched
    twin_a, twin_b = FakeMember("same-name", trust_id=tid("a")), FakeMember("same-name", trust_id=tid("b"))
    da = book.issue(twin_a.trust_id, twin_a.worker_id)
    book.fetched(da.token, "198.51.100.9")
    assert book.is_flagged(tid("a")) and not book.is_flagged(tid("b"))


@pytest.mark.anyio
async def test_a_hostile_worker_id_is_cleaned_before_it_is_kept(book, clock):
    router = FakeRouter([FakeMember("x[bold]\r\n\x00evil" + "y" * 200)])
    await scheduler(router, book, clock).tick()
    label = only_record(book)["label"]
    assert len(label) <= 64 and not re.search(r"[\[\]\r\n\x00]", label)


@pytest.mark.anyio
async def test_a_losing_coin_sends_nothing(book, clock):
    router = FakeRouter([FakeMember("a")])
    assert await scheduler(router, book, clock, rng=ForcedRng(0.99)).tick() == []
    assert router.sent == []


@pytest.mark.anyio
async def test_zero_per_day_turns_sending_off(book, clock):
    router = FakeRouter([FakeMember("a")])
    assert await scheduler(router, book, clock, per_day=0).tick() == []
    assert router.sent == []


@pytest.mark.anyio
async def test_a_member_is_not_sent_two_canaries_close_together(book, clock):
    router = FakeRouter([FakeMember("a")])
    s = scheduler(router, book, clock)
    assert await s.tick() == [tid("a")]
    clock.advance(canary.MIN_GAP_SECONDS - 1)
    assert await s.tick() == []
    clock.advance(2)
    assert await s.tick() == [tid("a")]


@pytest.mark.anyio
async def test_the_per_day_number_is_a_ceiling_on_any_24_hours(book, clock):
    router = FakeRouter([FakeMember("a")])
    s = scheduler(router, book, clock, per_day=2)
    sent = 0
    for _ in range(80):                                      # 80 ticks, 1000 s apart: about 22 hours
        sent += len(await s.tick())
        clock.advance(1000)
    assert sent == 2
    clock.advance(86400)
    assert await s.tick() == [tid("a")]


@pytest.mark.anyio
async def test_a_member_that_cannot_be_reached_is_recorded_as_undelivered(book, clock):
    router = FakeRouter([FakeMember("a")], accept=False)
    assert await scheduler(router, book, clock).tick() == []
    assert len(router.sent) == 1
    rec = only_record(book)
    assert rec["delivered"] is False and rec["answered"] is False
    # ... but the decoy still counts if it is ever used: the request may have got through
    assert book.fetched(body_token(router.sent[0][1]), "198.51.100.1") == tid("a")


@pytest.mark.anyio
async def test_a_transport_that_raises_or_hangs_does_not_stop_the_round(book, clock):
    members = [FakeMember("boom"), FakeMember("hang"), FakeMember("fine")]
    calls = []

    async def forward(member, wire, headers):
        calls.append(member.worker_id)
        if member.worker_id == "boom":
            raise RuntimeError("tunnel down")
        if member.worker_id == "hang":
            await asyncio.sleep(60)
        _, resp_key = member.ring.open_request(json.loads(wire))
        return sse(resp_key)

    s = CanaryScheduler(lambda: members, forward, book=book, trusted=lambda: set(), per_day=lambda: 4,
                        clock=clock, rng=ForcedRng(0.0), timeout=0.2)
    assert await s.tick() == [tid("fine")]
    assert sorted(calls) == ["boom", "fine", "hang"]


@pytest.mark.anyio
async def test_a_plain_function_transport_works_too(book, clock):
    member, seen = FakeMember("a"), []

    def forward(m, wire, headers):
        seen.append(m)
        _, resp_key = m.ring.open_request(json.loads(wire))
        return sse(resp_key).encode()

    s = CanaryScheduler(lambda: [member], forward, book=book, trusted=lambda: set(), per_day=lambda: 4,
                        clock=clock, rng=ForcedRng(0.0))
    assert await s.tick() == [tid("a")]
    assert seen == [member]


@pytest.mark.anyio
async def test_the_trusted_list_is_read_from_config_when_none_is_given(book, clock, monkeypatch):
    monkeypatch.setattr(canary, "get_config", lambda: {"TRUSTED_MEMBERS": f" {tid('tw-1')} , {tid('tw-2')},, "})
    assert canary.trusted_member_ids() == {tid("tw-1"), tid("tw-2")}
    router = FakeRouter([FakeMember("tw-1"), FakeMember("tw-2"), FakeMember("stranger")], clock=clock)
    s = CanaryScheduler(router.members, router.forward, book=book, per_day=lambda: 4, clock=clock, rng=ForcedRng(0.0))
    assert await s.tick() == [tid("stranger")]
    monkeypatch.setattr(canary, "get_config", lambda: {})
    assert canary.trusted_member_ids() == set()


@pytest.mark.anyio
async def test_the_per_day_setting_comes_from_config_and_can_change_live(book, clock, monkeypatch):
    limits = {"coding_canary_per_day": 4}
    monkeypatch.setattr(canary, "get_worker_limits", lambda: dict(limits))
    assert canary.per_day_setting() == 4
    router = FakeRouter([FakeMember("a")], clock=clock)
    s = CanaryScheduler(router.members, router.forward, book=book, trusted=lambda: set(), clock=clock, rng=ForcedRng(0.0))
    limits["coding_canary_per_day"] = 0
    assert await s.tick() == []
    limits["coding_canary_per_day"] = 8
    assert await s.tick() == [tid("a")]


@pytest.mark.anyio
async def test_run_ticks_until_stopped(book, clock):
    router = FakeRouter([FakeMember("a")])
    stop = asyncio.Event()
    s = scheduler(router, book, clock, interval=0.01)
    task = asyncio.ensure_future(s.run(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert len(router.sent) == 1          # then the minimum gap held it back


# --- canaries ride on real use ---------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("served,expected", [
    (T0 - 60, True), (T0 - 7200, True), (T0 - 7201, False), (T0 - 86400, False), (T0 + 30, True),
    (T0 + 3600, False), (None, False), ("yesterday", False), (True, False), (float("nan"), False),
])
async def test_only_a_member_that_served_real_traffic_in_the_last_two_hours_is_sent_one(book, clock, served, expected):
    m = FakeMember("a", active=False)
    m.last_served_at = served
    router = FakeRouter([m])
    s = CanaryScheduler(router.members, router.forward, book=book, trusted=lambda: set(), per_day=lambda: 4,
                        clock=clock, rng=ForcedRng(0.0))
    assert (await s.tick() == [tid("a")]) is expected


@pytest.mark.anyio
async def test_a_member_nobody_is_using_is_left_alone_however_long_it_waits(book, clock):
    m = FakeMember("a", active=False)
    m.last_served_at = T0 - 60
    router = FakeRouter([m])
    s = CanaryScheduler(router.members, router.forward, book=book, trusted=lambda: set(), per_day=lambda: 4,
                        clock=clock, rng=ForcedRng(0.0))
    assert await s.tick() == [tid("a")]
    clock.advance(3 * 3600)               # three idle hours: no more
    assert await s.tick() == []
    m.last_served_at = clock() - 10       # in use again
    assert await s.tick() == [tid("a")]


@pytest.mark.anyio
async def test_without_last_served_at_on_any_member_it_says_so_once(book, clock, monkeypatch):
    lines = []
    monkeypatch.setattr(canary, "log_warning", lines.append)
    members = [{"worker_id": "a", "trust_id": tid("a"), "pubkey": FakeMember("a").pubkey}]
    s = CanaryScheduler(lambda: members, lambda *a: None, book=book, trusted=lambda: set(), per_day=lambda: 4,
                        clock=clock, rng=ForcedRng(0.0))
    assert await s.tick() == [] and await s.tick() == []
    assert len(lines) == 1 and "last_served_at" in lines[0]


@pytest.mark.anyio
async def test_a_member_in_use_all_day_gets_a_handful_never_over_the_ceiling(book, clock):
    router = FakeRouter([FakeMember("a")])
    s = scheduler(router, book, clock, per_day=4, rng=random.Random(42))
    times = []
    days = 30
    for _ in range(days * 1440):
        if await s.tick():
            times.append(clock())
        clock.advance(60)
    per_day = len(times) / days
    assert 2.5 <= per_day <= 4.0, per_day
    for i, t in enumerate(times):                              # no 24 hours holds more than the ceiling
        assert sum(1 for u in times[i:] if u < t + 86400) <= 4
    gaps = {round(b - a) for a, b in zip(times, times[1:])}
    assert len(gaps) > 50, "send times must not follow a pattern"
    assert min(b - a for a, b in zip(times, times[1:])) >= canary.MIN_GAP_SECONDS


@pytest.mark.anyio
async def test_a_member_used_one_hour_a_day_gets_canaries_only_around_that_hour_and_few(book, clock):
    member = FakeMember("a", active=False)
    router = FakeRouter([member])

    def members():
        if 36000 <= clock() % 86400 < 39600:          # served real traffic between 10:00 and 11:00 UTC
            member.last_served_at = clock() - 5
        return [member]

    s = CanaryScheduler(members, router.forward, book=book, trusted=lambda: set(), per_day=lambda: 4,
                        interval=60.0, clock=clock, rng=random.Random(9))
    times, days = [], 40
    for _ in range(days * 1440):
        if await s.tick():
            times.append(clock() % 86400)
        clock.advance(60)
    assert times and all(36000 <= t < 36000 + 3600 + 7200 + 60 for t in times), "only while it is in use"
    assert 0.3 <= len(times) / days <= 2.0, len(times) / days


# --- a canary is indistinguishable from real traffic on the wire ---------------------------------

async def one_canary(book, clock, member=None, **router_kw):
    member = member or FakeMember("a")
    router = FakeRouter([member], **router_kw)
    sent = await scheduler(router, book, clock).tick()
    return router, member, sent


@pytest.mark.anyio
async def test_a_canary_goes_out_sealed_and_never_as_plain_json(book, clock):
    router, _, sent = await one_canary(book, clock)
    assert sent == [tid("a")]
    (_, wire, _), = router.wire
    envelope = json.loads(wire)
    assert list(envelope) == ["dlne_e2e"]
    assert list(envelope["dlne_e2e"]) == ["v", "epk", "nonce", "ct"] and envelope["dlne_e2e"]["v"] == 1
    for plain in (b"dlne_ck_", b"rossgilsonlabs", b"messages", b"tools", b"role", b"max_tokens", b"coding agent"):
        assert plain not in wire


@pytest.mark.anyio
async def test_it_is_serialised_the_way_the_extension_serialises_its_envelope(book, clock):
    router, _, _ = await one_canary(book, clock)
    (_, wire, _), = router.wire
    assert b" " not in wire and b"\n" not in wire, "JSON.stringify writes no spaces"
    assert wire.startswith(b'{"dlne_e2e":{"v":1,"epk":"')


@pytest.mark.anyio
async def test_the_headers_are_the_ones_routed_traffic_gets_and_nothing_more(book, clock):
    router, _, _ = await one_canary(book, clock)
    (_, wire, headers), = router.wire
    assert headers == {"content-type": "application/json", "content-length": str(len(wire))}
    assert headers == forward_headers({"Content-Type": "application/json", "Content-Length": str(len(wire)),
                                       "Authorization": "x", "X-Forwarded-For": "y"})


@pytest.mark.anyio
async def test_it_is_sealed_with_the_libraries_defaults_and_nothing_else(book, clock, monkeypatch):
    """No pad_block of its own: whatever padding real clients get by default, canaries get too."""
    calls, real = [], e2e.seal_request

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(canary.e2e, "seal_request", spy)
    member = FakeMember("a")
    await one_canary(book, clock, member)
    (args, kwargs), = calls
    assert len(args) == 2 and args[1] == member.pubkey and kwargs == {}


@pytest.mark.anyio
async def test_its_size_on_the_wire_is_what_a_real_client_produces_for_the_same_body(book, clock):
    router, member, _ = await one_canary(book, clock)
    (_, wire, _), = router.wire
    body = router.sent[0][1]
    real_wire, _ = canary.seal_for_member(body, member.pubkey)
    assert len(real_wire) == len(wire)
    envelope, _ = e2e.seal_request(body, member.pubkey)
    assert len(json.dumps(envelope, separators=(",", ":")).encode()) == len(wire)
    assert e2e.PAD_BLOCK == 256, "the default padding real clients get"


@pytest.mark.anyio
async def test_a_failure_to_seal_never_falls_back_to_plain_json(book, clock, monkeypatch):
    def broken(*_a, **_k):
        raise e2e.E2EError("key", "bad member key")

    monkeypatch.setattr(canary.e2e, "seal_request", broken)
    router, _, sent = await one_canary(book, clock)
    assert sent == [] and router.wire == []


@pytest.mark.anyio
async def test_the_member_opens_it_and_finds_an_ordinary_agent_request_with_its_own_decoy(book, clock):
    router, _, _ = await one_canary(book, clock)
    body = router.sent[0][1]
    assert body["stream"] is True and body["max_tokens"] == 10240 and body["temperature"] == 0.2
    assert book.fetched(body_token(body), "198.51.100.1") == tid("a")


@pytest.mark.anyio
async def test_a_proper_answer_is_read_to_the_end_and_recorded(book, clock):
    await one_canary(book, clock)
    rec = only_record(book)
    assert rec["delivered"] is True and rec["answered"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["garbage", "no-sealed-end", "tampered", "dropped-chunk", "empty", "data-after-end"])
async def test_a_bad_or_truncated_answer_is_noted_and_never_raises(book, clock, how):
    def tamper(key):
        text = sse(key)
        i = text.index('"ct":"') + 16
        return text[:i] + ("A" if text[i] != "A" else "B") + text[i + 1:]

    def drop(key):
        parts = sse(key).split("\n\n")
        del parts[1]
        return "\n\n".join(parts)

    def after_end(key):
        text = sse(key, plain_done=False)
        return text + 'data: {"dlne_e2e":{"n":9,"ct":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}}\n\ndata: [DONE]\n\n'

    answers = {
        "garbage": lambda k: b"<html>502 Bad Gateway</html>",
        "no-sealed-end": lambda k: sse(k, sealed_end=False),          # plain [DONE] alone is a cut stream
        "tampered": tamper,
        "dropped-chunk": drop,
        "empty": lambda k: b"",
        "data-after-end": after_end,
    }
    router, _, sent = await one_canary(book, clock, answer=answers[how])
    assert sent == [tid("a")], "the member did take it"
    rec = only_record(book)
    assert rec["delivered"] is True and rec["answered"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("shape", ["bytes", "text", "list", "chunked-async", "no-plain-done"])
async def test_the_answer_may_come_back_in_any_shape_the_router_has(book, clock, shape):
    def make(key):
        data = sse(key, plain_done=(shape != "no-plain-done"))
        if shape in ("bytes", "no-plain-done"):
            return data.encode()                  # closing at the end of the data counts like the plain [DONE]
        if shape == "text":
            return data
        if shape == "list":
            return [data[i:i + 5].encode() for i in range(0, len(data), 5)]

        async def gen():
            raw = data.encode()
            for i in range(0, len(raw), 7):          # lines split anywhere, even mid-way through a character
                yield raw[i:i + 7]
        return gen()

    await one_canary(book, clock, answer=make)
    assert only_record(book)["answered"] is True


@pytest.mark.anyio
async def test_members_without_a_usable_key_are_not_sent_a_canary(book, clock):
    ok, nokey, bad, short = FakeMember("ok"), FakeMember("nokey", keyed=False), FakeMember("bad"), FakeMember("short")
    bad.pubkey = "not base64 !!!"
    short.pubkey = base64.b64encode(b"x" * 16).decode()
    router = FakeRouter([ok, nokey, bad, short])
    assert await scheduler(router, book, clock).tick() == [tid("ok")]
    assert [t for t, _, _ in router.wire] == [tid("ok")]


# --- reading the answer: ResponseOpener.open per object, close() at the end ---------------------

def key32():
    return secrets.token_bytes(32)


@pytest.mark.anyio
@pytest.mark.parametrize("kwargs,expected", [
    ({}, True),                                          # content, sealed end, plain [DONE]
    ({"plain_done": False}, True),                       # the data just ends after the sealed end
    ({"sealed_end": False}, False),                      # plain [DONE] without the sealed end: cut
    ({"sealed_end": False, "plain_done": False}, False),
    ({"chunks": ()}, False),                             # a sealed end and no content is not an answer
])
async def test_the_answer_counts_only_when_the_sealed_end_arrived(kwargs, expected):
    key = key32()
    assert await canary.read_sealed_answer(sse(key, **kwargs), key) is expected


@pytest.mark.anyio
async def test_the_reader_uses_the_current_opener_interface_only(monkeypatch):
    assert not hasattr(canary, "_open_sealed"), "the old open(n, ct) path is gone"
    key = key32()
    seen = []
    real = e2e.ResponseOpener

    class Spy(real):
        def open(self, sealed):
            seen.append(sorted(sealed))
            return super().open(sealed)

        def close(self):
            seen.append("close")
            return super().close()

    monkeypatch.setattr(canary.e2e, "ResponseOpener", Spy)
    assert await canary.read_sealed_answer(sse(key), key) is True
    assert seen[0] == ["dlne_e2e"] and seen[-1] == "close" and seen.count("close") == 1


@pytest.mark.anyio
async def test_a_runaway_answer_is_cut_off(monkeypatch):
    monkeypatch.setattr(canary, "MAX_ANSWER_BYTES", 100)
    assert await canary.read_sealed_answer("data: " + "x" * 500, b"k" * 32) is False


# ==========================================================================================
# Detection
# ==========================================================================================

def test_using_a_decoy_key_flags_exactly_the_member_it_was_sent_to(book):
    a, b = book.issue(tid("a"), "a"), book.issue(tid("b"), "b")
    assert book.presented_key(a.key, "198.51.100.1") == tid("a")
    assert book.is_flagged(tid("a")) and not book.is_flagged(tid("b"))
    assert book.presented_key(b.key, "198.51.100.1") == tid("b")
    assert book.is_flagged(tid("b"))


def test_opening_a_decoy_link_flags_the_member(book):
    d = book.issue(tid("a"), "a")
    assert book.fetched(d.token, "198.51.100.1") == tid("a")
    assert book.is_flagged(tid("a"))


def test_the_worker_id_is_only_a_label_in_the_flag(book):
    d = book.issue(tid("a"), "claimed-name")
    book.fetched(d.token, "198.51.100.1")
    assert not book.is_flagged("claimed-name")
    entry = book.snapshot()["flagged"][0]
    assert entry["trust_id"] == tid("a") and entry["worker_id"] == "claimed-name"


def test_both_channels_are_remembered(book):
    d = book.issue(tid("a"), "a")
    book.presented_key(d.key, "198.51.100.1")
    book.fetched(d.token, "198.51.100.2")
    entry = book.snapshot()["flagged"][0]
    assert entry["trust_id"] == tid("a")
    assert entry["channels"] == ["key", "url"]
    assert entry["hits"] == 2
    assert entry["ip"] == "198.51.100.1", "the first sighting is kept as evidence"


@pytest.mark.parametrize("junk", [
    KEY_PREFIX + "0" * 48,                       # right shape, never issued
    KEY_PREFIX + "0" * 47, KEY_PREFIX + "0" * 49, KEY_PREFIX + "G" * 48, "dlne_sk_" + "0" * 48,
    "", None, 5, "Bearer x", " ",
])
def test_keys_that_are_not_ours_flag_nobody(book, junk):
    book.issue(tid("a"), "a")
    assert book.presented_key(junk, "198.51.100.1") is None
    assert not book.is_flagged(tid("a"))


def test_a_decoy_key_with_a_changed_character_does_not_match(book):
    d = book.issue(tid("a"), "a")
    other = d.key[:-1] + ("0" if d.key[-1] != "0" else "1")
    assert book.presented_key(other, "198.51.100.1") is None
    assert book.presented_key(d.key.upper(), "198.51.100.1") is None


@pytest.mark.parametrize("junk", ["short", "x" * 100, "has space in it here!!", "a" * 24, "../../etc/passwd", "", None, 7])
def test_links_that_are_not_ours_flag_nobody(book, junk):
    book.issue(tid("a"), "a")
    assert book.fetched(junk, "198.51.100.1") is None
    assert not book.is_flagged(tid("a"))


def test_a_flag_survives_a_restart_and_old_decoys_still_work(book, clock):
    d1, d2 = book.issue(tid("a"), "a"), book.issue(tid("b"), "b")
    clock.advance(500)
    book.fetched(d1.token, "198.51.100.1")
    again = reopen(book, clock)
    assert again.is_flagged(tid("a")) and not again.is_flagged(tid("b"))
    entry = again.snapshot()["flagged"][0]
    assert entry["trust_id"] == tid("a") and entry["worker_id"] == "a"
    assert again.presented_key(d2.key, "198.51.100.5") == tid("b"), "decoys sent before the restart still catch"
    assert again.last_sent(tid("b")) is not None


def test_a_flag_set_by_a_key_is_on_disk_before_the_call_returns(book, clock):
    d = book.issue(tid("a"), "a")
    book.presented_key(d.key, "198.51.100.1")
    assert saved(book)["flagged"][tid("a")]["channels"] == ["key"]


def test_the_send_history_survives_a_restart_so_the_daily_ceiling_does(book, clock):
    for _ in range(3):
        book.issue(tid("a"), "a")
        clock.advance(10)
    again = reopen(book, clock)
    assert again.sent_since(tid("a"), clock() - 3600) == 3
    assert again.last_sent(tid("a")) == pytest.approx(clock() - 10)


def test_an_unreadable_book_is_kept_aside_and_the_coordinator_still_starts(tmp_path, clock, monkeypatch):
    path = tmp_path / "config" / "canaries.json"
    path.parent.mkdir()
    path.write_text("{not json", encoding="utf-8")
    errors = []
    monkeypatch.setattr(canary, "log_error", errors.append)
    fresh = CanaryBook(str(path), clock=clock)
    assert fresh.snapshot()["flagged"] == []
    assert any("could not be read" in e for e in errors)
    assert [p.name for p in path.parent.iterdir() if ".corrupt-" in p.name], "the bad file is kept for the operator"


def test_garbage_entries_in_a_good_file_are_ignored(tmp_path, clock):
    path = tmp_path / "config" / "canaries.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"version": 2, "canaries": {"abc": "nope", "def": {"trust_id": 5},
                                                          "ghi": {"trust_id": "worker-a", "sent": 1, "key": "k"}},
                                "flagged": {tid("w"): {"trust_id": tid("w"), "hits": 1}, "x": {"hits": 1},
                                            tid("y"): 3}, "events": ["a", {"at": 1}]}))
    fresh = CanaryBook(str(path), clock=clock)
    assert fresh.is_flagged(tid("w")) and not fresh.is_flagged("x") and not fresh.is_flagged(tid("y"))
    assert fresh.snapshot()["sent"]["total"] == 0


def test_a_book_written_with_worker_id_keys_flags_nobody(tmp_path, clock):
    """Records keyed the old way (worker ids) are not honoured: a worker id is not an identity."""
    path = tmp_path / "config" / "canaries.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"version": 1, "canaries": {"d": {"member": "worker-a", "sent": 1, "key": "k"}},
                                "flagged": {"worker-a": {"worker_id": "worker-a", "hits": 1}}, "events": []}))
    fresh = CanaryBook(str(path), clock=clock)
    assert not fresh.is_flagged("worker-a") and fresh.snapshot()["flagged"] == []


def test_the_file_is_written_atomically_and_privately(book):
    d = book.issue(tid("a"), "a")
    book.fetched(d.token, "198.51.100.1")
    names = sorted(p.name for p in os.scandir(os.path.dirname(book.path)))
    assert names == ["canaries.json"], "no staging file left behind"
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(book.path).st_mode) == 0o600


def test_a_failed_write_keeps_the_flag_in_memory_and_the_old_file_intact(book, monkeypatch):
    good = book.issue(tid("a"), "a")
    before = open(book.path, encoding="utf-8").read()
    errors = []
    monkeypatch.setattr(canary, "log_error", errors.append)

    def refuse(*_a, **_k):
        raise OSError("disk full")

    with monkeypatch.context() as patched:
        patched.setattr(canary.os, "replace", refuse)
        book.fetched(good.token, "198.51.100.1")
    assert book.is_flagged(tid("a"))
    assert any("Could not save" in e for e in errors)
    assert open(book.path, encoding="utf-8").read() == before
    assert [p.name for p in os.scandir(os.path.dirname(book.path))] == ["canaries.json"]


def test_repeat_hits_are_one_event_and_do_not_rewrite_the_file_every_time(book, clock):
    d = book.issue(tid("a"), "a")
    for _ in range(10):
        book.fetched(d.token, "198.51.100.1")
        clock.advance(1)
    assert len(book.snapshot()["events"]) == 1 and book.snapshot()["events"][0]["count"] == 10
    assert saved(book)["flagged"][tid("a")]["hits"] == 1        # held back
    book.flush()
    assert saved(book)["flagged"][tid("a")]["hits"] == 10
    clock.advance(canary.EVENT_COALESCE_SECONDS + 1)
    book.fetched(d.token, "198.51.100.1")
    assert len(book.snapshot()["events"]) == 2
    assert saved(book)["flagged"][tid("a")]["hits"] == 11        # past the interval: written straight away


def test_the_event_list_is_bounded(book, clock):
    d = book.issue(tid("a"), "a")
    for i in range(canary.MAX_EVENTS + 50):
        book.fetched(d.token, f"198.51.100.{i % 250}")
        clock.advance(1)
    assert len(book.snapshot(events=10_000)["events"]) == canary.MAX_EVENTS
    assert len(saved(book)["events"]) <= canary.MAX_EVENTS


def test_a_hostile_address_is_cleaned_before_it_is_stored_or_logged(book):
    d = book.issue(tid("a"), "a")
    book.fetched(d.token, "1.2.3.4\r\n[bold]EVIL[/bold] \x00" + "9" * 200)
    ip = book.snapshot()["flagged"][0]["ip"]
    assert len(ip) <= 64 and not re.search(r"[\s\[\]\x00]", ip)


def test_wrong_links_are_logged_at_most_once_an_hour_per_address_and_never_skip_the_lookup(book, monkeypatch, clock):
    lines = []
    monkeypatch.setattr(canary, "log_system", lines.append)
    real = book.issue(tid("a"), "a")
    for _ in range(5000):
        assert book.fetched(secrets.token_urlsafe(18), "198.51.100.200") is None
    assert len([l for l in lines if "unknown token" in l]) == 1
    # A snooper who floods the endpoint first to get "rate limited" gains nothing:
    assert book.fetched(real.token, "198.51.100.200") == tid("a")
    clock.advance(canary.MISS_LOG_INTERVAL + 1)
    book.fetched(secrets.token_urlsafe(18), "198.51.100.200")
    assert len([l for l in lines if "unknown token" in l]) == 2


def test_the_table_of_noisy_addresses_is_bounded(book, monkeypatch):
    monkeypatch.setattr(canary, "log_system", lambda *_: None)
    for i in range(canary.MISS_LOG_KEYS + 500):
        book.fetched(secrets.token_urlsafe(18), f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}")
    assert len(book._miss_logged) <= canary.MISS_LOG_KEYS


def test_old_decoys_are_forgotten_after_the_retention_period(book, clock):
    old = book.issue(tid("a"), "a")
    clock.advance(canary.RETENTION_SECONDS + 1)
    new = book.issue(tid("b"), "b")
    assert book.presented_key(old.key, "198.51.100.1") is None
    assert book.presented_key(new.key, "198.51.100.1") == tid("b")
    assert book.sent_since(tid("a"), 0) == 0


def test_the_number_of_records_is_capped_oldest_first(book, clock, monkeypatch):
    monkeypatch.setattr(canary, "MAX_CANARIES", 3)
    decoys = []
    for i in range(5):
        decoys.append(book.issue(tid(f"w{i}"), f"w{i}"))
        clock.advance(1)
    assert book.snapshot()["sent"]["total"] == 3
    assert book.fetched(decoys[0].token, "198.51.100.1") is None
    assert book.fetched(decoys[4].token, "198.51.100.1") == tid("w4")


def test_clearing_a_member_lets_it_back_and_retires_the_decoy_that_tripped(book, clock):
    d, other = book.issue(tid("a"), "a"), book.issue(tid("a"), "a")
    book.fetched(d.token, "198.51.100.1")
    assert book.unflag(tid("a")) is True
    assert not book.is_flagged(tid("a"))
    assert book.fetched(d.token, "198.51.100.1") is None, "a link scanner re-fetching it does not flag again"
    assert book.presented_key(d.key, "198.51.100.1") is None
    assert book.presented_key(other.key, "198.51.100.1") == tid("a"), "a decoy that never tripped still works"
    assert book.unflag(tid("nobody")) is False
    assert book.unflag("a") is False, "clearing is by trust id, not by the worker id label"


def test_a_clear_is_permanent_across_a_restart(book, clock):
    d = book.issue(tid("a"), "a")
    book.fetched(d.token, "198.51.100.1")
    book.unflag(tid("a"))
    assert not reopen(book, clock).is_flagged(tid("a"))


def test_the_operators_snapshot_has_no_secrets_in_it(book):
    d = book.issue(tid("a"), "a")
    book.presented_key(d.key, "198.51.100.1")
    text = json.dumps(book.snapshot(per_day=4))
    assert d.key not in text and d.token not in text
    snap = book.snapshot(per_day=4)
    assert snap["enabled"] is True and snap["per_day"] == 4
    assert snap["sent"] == {"total": 1, "last_24h": 1, "members": 1}
    assert snap["flagged"][0]["canary"] == d.id
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", snap["flagged"][0]["first_at"])


def test_a_trip_is_logged_without_the_decoy(book, monkeypatch):
    lines = []
    monkeypatch.setattr(canary, "log_warning", lines.append)
    d = book.issue(tid("w9"), "worker-9")
    book.presented_key(d.key, "198.51.100.1")
    book.presented_key(d.key, "198.51.100.1")
    assert len(lines) == 1, "once per member and channel, not once per hit"
    assert tid("w9") in lines[0] and "worker-9" in lines[0] and "CANARY TRIPPED" in lines[0]
    assert d.key not in lines[0] and d.token not in lines[0]


# --- the module-level hooks the coordinator and router call -------------------------------------

def test_is_flagged_is_what_the_router_asks_and_it_reads_trust_ids_only(book):
    d = book.issue(tid("a"), "worker-a")
    assert canary.is_flagged(tid("a")) is False
    book.fetched(d.token, "198.51.100.1")
    assert canary.is_flagged(tid("a")) is True
    assert canary.is_flagged(tid("b")) is False
    # the router can pass its member object, or a mapping: the trust id is read from it
    assert canary.is_flagged(FakeMember("anything", trust_id=tid("a"))) is True
    assert canary.is_flagged({"trust_id": tid("a"), "worker_id": "someone-else"}) is True
    assert canary.is_flagged(FakeMember("worker-a", trust_id=tid("b"))) is False, "the label is not an identity"
    assert canary.is_flagged("worker-a") is False
    assert canary.is_flagged(FakeMember("worker-a", verified=False)) is False
    assert canary.is_flagged(None) is False


def test_a_broken_book_never_takes_routing_down(book, monkeypatch):
    class Broken:
        def is_flagged(self, _):
            raise RuntimeError("boom")

    monkeypatch.setattr(canary, "log_error", lambda *_: None)
    canary.set_book(Broken())
    assert canary.is_flagged(tid("a")) is False


def test_the_default_book_lives_under_the_install_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "get_base_dir", lambda: str(tmp_path))
    previous = canary.set_book(None)
    try:
        assert canary.book().path == str(tmp_path / "config" / "canaries.json")
        assert canary.book() is canary.book()
    finally:
        canary.set_book(previous)


@pytest.mark.parametrize("headers,query", [
    ({"Authorization": "Bearer {key}"}, {}),
    ({"authorization": "bearer {key}"}, {}),
    ({"Authorization": "{key}"}, {}),
    ({"x-api-key": "{key}"}, {}),
    ({"api-key": " {key} "}, {}),
    ({}, {"token": "{key}"}),
])
def test_a_decoy_key_is_caught_wherever_a_client_would_put_it(book, headers, query):
    d = book.issue(tid("a"), "a")
    canary.check_request({k: v.format(key=d.key) for k, v in headers.items()},
                         {k: v.format(key=d.key) for k, v in query.items()}, "198.51.100.1")
    assert book.is_flagged(tid("a"))


def test_ordinary_credentials_and_odd_input_are_ignored_quietly(book, monkeypatch):
    errors = []
    monkeypatch.setattr(canary, "log_error", errors.append)
    book.issue(tid("a"), "a")
    canary.check_request({"Authorization": "Bearer dlne_sk_" + "0" * 48}, {"token": "abc"}, "198.51.100.1")
    canary.check_request({"Authorization": "Basic Zm9vOmJhcg=="}, None, "198.51.100.1")
    canary.check_request({}, {}, "")
    canary.check_request(None, None, "")        # not even headers: must not raise
    assert not book.is_flagged(tid("a"))


def test_failing_to_work_out_the_address_does_not_cost_the_detection(book):
    d = book.issue(tid("a"), "a")

    def broken():
        raise RuntimeError("no address")

    canary.check_request({"Authorization": f"Bearer {d.key}"}, {}, broken)
    assert book.is_flagged(tid("a"))
    assert book.snapshot()["flagged"][0]["ip"] == ""


# ==========================================================================================
# Config
# ==========================================================================================

@pytest.mark.parametrize("line,expected", [
    ("", 4), ("CODING_CANARY_PER_DAY=0", 0), ("CODING_CANARY_PER_DAY=12", 12), ("CODING_CANARY_PER_DAY=48", 48),
    ("CODING_CANARY_PER_DAY=49", 4), ("CODING_CANARY_PER_DAY=-1", 4), ("CODING_CANARY_PER_DAY=lots", 4),
    ("CODING_CANARY_PER_DAY=2.5", 4),
])
def test_the_per_day_setting_is_validated(isolated_config, monkeypatch, line, expected):
    isolated_config.write_text(line + "\n", encoding="utf-8")
    monkeypatch.setattr(engine_config, "_cached_stamp", None)
    assert engine_config.get_worker_limits()["coding_canary_per_day"] == expected


# ==========================================================================================
# The coordinator's side
# ==========================================================================================

@pytest.fixture
def web(book, monkeypatch):
    real_limits = coordinator.get_worker_limits
    state = {"admin_key_hash": api_keys.hash_admin_key(ADMIN_KEY), "admin_allow_remote": False,
             "starter_quota_inferences": 0}          # 0: the free trial is off, a chat from a stranger is a plain 403
    monkeypatch.setattr(coordinator, "get_worker_limits", lambda: {**real_limits(), **state})
    monkeypatch.setattr(canary, "get_worker_limits", lambda: {**real_limits(), **state})
    return type("Web", (), {"book": book, "state": state})


def client(ip=REMOTE, key=None):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return AsyncClient(transport=ASGITransport(app=coordinator.app, client=ip), base_url="http://test", headers=headers)


@pytest.mark.anyio
async def test_the_canary_page_looks_exactly_like_a_page_that_does_not_exist(web):
    d = web.book.issue(tid("a"), "a")
    async with client(SNOOPER) as c:
        real = await c.get(f"/c/{d.token}")
        wrong = await c.get(f"/c/{secrets.token_urlsafe(18)}")
        nothing = await c.get("/no/such/page")
    assert real.status_code == wrong.status_code == nothing.status_code == 404
    assert real.content == wrong.content == nothing.content
    assert real.headers["content-type"] == wrong.headers["content-type"] == nothing.headers["content-type"]
    assert real.headers.get("content-length") == nothing.headers.get("content-length")
    assert d.token not in real.text
    assert web.book.is_flagged(tid("a"))


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["get", "head", "post", "put", "patch", "delete", "options"])
async def test_every_method_gets_the_same_answer_and_every_method_counts(web, method):
    """A 405 for POST would tell a visitor the page exists; so no method may answer differently."""
    d = web.book.issue(tid("a"), "a")
    async with client(SNOOPER) as c:
        real = await getattr(c, method)(f"/c/{d.token}")
        nothing = await getattr(c, method)("/no/such/page")
    assert real.status_code == nothing.status_code
    assert real.content == nothing.content
    assert web.book.is_flagged(tid("a"))


@pytest.mark.anyio
async def test_a_wrong_token_flags_nobody_and_answers_the_same(web):
    web.book.issue(tid("a"), "a")
    async with client(SNOOPER) as c:
        for token in ("x", "x" * 300, "%20", secrets.token_urlsafe(18), "..%2f..%2fv1%2fadmin%2fstats", "%00"):
            assert (await c.get(f"/c/{token}")).status_code == 404
    assert not web.book.is_flagged(tid("a"))


@pytest.mark.anyio
async def test_a_visit_records_where_it_came_from(web):
    d = web.book.issue(tid("a"), "a")
    async with client(SNOOPER) as c:
        await c.get(f"/c/{d.token}")
    assert web.book.snapshot()["flagged"][0]["ip"] == "198.51.100.77"


@pytest.mark.anyio
async def test_a_decoy_key_on_a_chat_request_flags_the_member_and_is_answered_like_any_invalid_key(web):
    d = web.book.issue(tid("a"), "a")
    made_up = KEY_PREFIX + "ab" * 24
    other_junk = "dlne_xx_" + "ab" * 24
    answers = []
    async with client(SNOOPER) as c:
        for key in (d.key, made_up, other_junk):
            resp = await c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]},
                                headers={"Authorization": f"Bearer {key}"})
            answers.append((resp.status_code, resp.json()))
    assert answers[0] == answers[1] == answers[2]
    assert answers[0][0] == 403
    assert web.book.is_flagged(tid("a"))


@pytest.mark.anyio
@pytest.mark.parametrize("path,kind", [("/", "bearer"), ("/v1/admin/stats", "bearer"),
                                      ("/", "x-api-key"), ("/?token={key}", "query"), ("/nowhere", "bearer")])
async def test_a_decoy_key_is_noticed_on_any_path_and_changes_nothing_about_the_answer(web, path, kind):
    d = web.book.issue(tid("a"), "a")
    junk_key = KEY_PREFIX + "cd" * 24
    async with client(SNOOPER) as c:
        def headers(key):
            if kind == "bearer":
                return {"Authorization": f"Bearer {key}"}
            if kind == "x-api-key":
                return {"x-api-key": key}
            return {}
        junk = await c.get(path.format(key=junk_key), headers=headers(junk_key))
        real = await c.get(path.format(key=d.key), headers=headers(d.key))
    assert (real.status_code, real.content) == (junk.status_code, junk.content)
    assert web.book.is_flagged(tid("a"))


@pytest.mark.anyio
async def test_ordinary_requests_do_not_touch_the_book(web, monkeypatch):
    calls = []
    monkeypatch.setattr(web.book, "presented_key", lambda *a, **k: calls.append(a))
    async with client(SNOOPER) as c:
        await c.get("/")
        await c.get("/v1/admin/stats", headers={"Authorization": "Bearer dlne_sk_" + "0" * 48})
    assert calls == []


# --- the admin view ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_admin_list_needs_the_operator_key(web):
    web.book.issue(tid("a"), "a")
    async with client(LOOPBACK) as c:
        assert (await c.get("/v1/admin/canaries")).status_code == 403
    async with client(LOOPBACK, key="dlne_ak_wrong") as c:
        assert (await c.get("/v1/admin/canaries")).status_code == 403
    async with client(REMOTE, key=ADMIN_KEY) as c:
        resp = await c.get("/v1/admin/canaries")
        assert resp.status_code == 403 and "not accepted from this address" in resp.json()["detail"]
    async with client(LOOPBACK, key=ADMIN_KEY) as c:
        assert (await c.get("/v1/admin/canaries")).status_code == 200


@pytest.mark.anyio
async def test_the_admin_list_shows_flagged_members_by_trust_id_with_the_worker_id_as_a_label(web):
    quiet, loud = web.book.issue(tid("quiet"), "quiet"), web.book.issue(tid("loud"), "the-name-it-claims")
    web.book.fetched(loud.token, "198.51.100.77")
    async with client(LOOPBACK, key=ADMIN_KEY) as c:
        data = (await c.get("/v1/admin/canaries")).json()
    assert data["enabled"] is True and data["per_day"] == 4
    assert [f["trust_id"] for f in data["flagged"]] == [tid("loud")]
    assert data["flagged"][0]["worker_id"] == "the-name-it-claims"
    assert data["flagged"][0]["channels"] == ["url"] and data["flagged"][0]["ip"] == "198.51.100.77"
    assert data["events"][0]["trust_id"] == tid("loud")
    assert data["sent"]["total"] == 2
    assert quiet.key not in json.dumps(data) and loud.token not in json.dumps(data)


@pytest.mark.anyio
async def test_the_admin_list_is_empty_but_well_formed_with_nothing_flagged(web):
    async with client(LOOPBACK, key=ADMIN_KEY) as c:
        data = (await c.get("/v1/admin/canaries")).json()
    assert data["flagged"] == [] and data["events"] == [] and data["sent"]["total"] == 0


@pytest.mark.anyio
async def test_the_operator_clears_a_flag_by_trust_id_and_only_the_operator(web):
    d = web.book.issue(tid("a"), "a-label")
    web.book.fetched(d.token, "198.51.100.77")
    async with client(LOOPBACK) as c:
        assert (await c.delete(f"/v1/admin/canaries/{tid('a')}")).status_code == 403
    assert web.book.is_flagged(tid("a"))
    async with client(LOOPBACK, key=ADMIN_KEY) as c:
        assert (await c.delete("/v1/admin/canaries/a-label")).status_code == 404, "the label is not the key"
        assert web.book.is_flagged(tid("a"))
        assert (await c.delete(f"/v1/admin/canaries/{tid('a')}")).json() == {"status": "cleared", "trust_id": tid("a")}
        assert (await c.delete(f"/v1/admin/canaries/{tid('a')}")).status_code == 404
    assert not web.book.is_flagged(tid("a"))
