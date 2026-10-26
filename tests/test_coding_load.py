"""
The load and soak rig of the coding path (scripts/coding_load.py and its fakes).

* The fake member is held to the real member proxy (engine/coding_member.py): the same request sent to both gets the
  same status, error body, Retry-After and shape of answer, so a load result is about the coordinator and not about a
  fake that is kinder than a real member.
* The analysis is checked on made-up records whose answers are known.
* One small end-to-end run (the `smoke` profile) drives a real coordinator process on loopback with a handful of agents,
  well under 20 s; it needs nothing but this checkout.

The full profiles (scripts/coding_load.py run members | churn | slow | ramp | soak) are not part of the suite.
"""

import asyncio
import importlib.util
import json
import os
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import coding_load  # noqa: E402
import coding_load_fakes as fakes  # noqa: E402
from engine import e2e  # noqa: E402


def free_port() -> int:
    return coding_load.free_port()


class AliveProcess:
    returncode = None
    pid = 4242          # the member records its llama-server's pid (leftover reaping, engine/coding_member.py)

    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


# ======================================================================================
# Small pieces
# ======================================================================================

def test_percentile_and_slope():
    assert fakes.percentile([], 50) == 0.0
    assert fakes.percentile([7], 99) == 7.0
    assert fakes.percentile([1, 2, 3, 4, 5], 50) == 3.0
    assert fakes.percentile([0, 10], 95) == pytest.approx(9.5)
    assert coding_load._slope_per_hour([(0, 10), (3600, 20), (7200, 30)]) == pytest.approx(10.0)
    assert coding_load._slope_per_hour([(0, 5)]) == 0.0


def test_a_slower_world_packs_more_tokens_into_each_event():
    assert fakes.Pace(30.0, 120.0, scale=1.0).tokens_per_event() == 2
    assert fakes.Pace(30.0, 120.0, scale=40.0).tokens_per_event() == 48
    assert fakes.Pace(1.0, 1.0, scale=1.0).tokens_per_event() == 1


def test_the_profiles_are_complete_and_make_a_spec():
    for name in coding_load.PROFILES:
        spec = coding_load.make_spec(name)
        assert spec["name"] == name and spec["agents"]["count"] > 0 and spec["scale"] > 0
        assert sum(spec["agents"]["tiers"].values()) > 0
    assert coding_load.make_spec("smoke", {"agents": {"count": 3}})["agents"]["count"] == 3
    assert coding_load.PROFILES["smoke"]["duration_s"] <= 10, "the CI smoke test must stay short"


def test_the_trust_ids_the_parent_lists_are_the_ones_the_member_has():
    from engine import member_identity
    priv, pub, trust = fakes.identity_for("m00")
    assert trust == member_identity.trust_id(pub) and len(trust) == 32
    assert fakes.identity_for("m00")[2] == trust and fakes.identity_for("m01")[2] != trust


def test_the_prompt_cache_makes_the_next_turn_cheap_and_a_stranger_pays_in_full():
    async def scenario():
        engine = fakes.FakeEngine("t", fakes.Pace(1000.0, 100.0, scale=100.0, jitter=0.0))
        body = {"messages": [{"role": "user", "content": "x" * 40000}], "stream": True,
                "x_load": {"sid": "s1", "otok": 4, "rid": "r"}}

        async def first_content(b):
            plan = engine.plan(b)
            t = time.monotonic()
            async for _ in engine.stream(plan, "t", time.time()):
                break
            return time.monotonic() - t, plan

        cold, plan = await first_content(body)
        assert plan["cached"] == 0
        # finish one so the cache learns the session
        plan = engine.plan(body)
        async for _ in engine.stream(plan, "t", time.time()):
            pass
        warm_body = {**body, "messages": [{"role": "user", "content": "x" * 44000}]}
        warm_plan = engine.plan(warm_body)
        assert warm_plan["cached"] >= 9000
        stranger = engine.plan({**warm_body, "x_load": {"sid": "someone-else", "otok": 4}})
        assert stranger["cached"] == 0
        return cold

    assert asyncio.run(scenario()) >= 0


# ======================================================================================
# The fake member against the real member proxy
# ======================================================================================

CHAT = {"model": "x", "messages": [{"role": "user", "content": "hello " * 50}], "stream": True,
        "x_load": {"rid": "r1", "sid": "s1", "otok": 6}}


async def _outcome(client: httpx.AsyncClient, base: str, pubkey: str, how: str):
    """What one scenario looks like from outside: status, error object, Retry-After, content type, sealed-stream facts."""
    url = f"{base}/v1/chat/completions"
    if how == "bad_json":
        r = await client.post(url, content=b"{nope", headers={"content-type": "application/json"})
    elif how == "not_object":
        r = await client.post(url, content=b"[1]", headers={"content-type": "application/json"})
    elif how == "bad_envelope":
        r = await client.post(url, json={"dlne_e2e": {"v": 1, "epk": "x", "nonce": "y", "ct": "z"}})
    elif how == "replay":
        envelope, _key = e2e.seal_request(CHAT, pubkey)
        first = await client.post(url, json=envelope)
        assert first.status_code == 200, first.text
        r = await client.post(url, json=envelope)
    elif how == "wrong_key":
        _priv, other_pub = e2e.generate_keypair()
        envelope, _key = e2e.seal_request(CHAT, other_pub)
        r = await client.post(url, json=envelope)
    elif how == "get_chat":
        r = await client.get(url)
    elif how == "sealed_stream":
        envelope, key = e2e.seal_request(CHAT, pubkey)
        opener = e2e.ResponseOpener(key)
        plain_done = False
        texts = []
        async with client.stream("POST", url, json=envelope) as reply:
            assert reply.status_code == 200
            ctype = reply.headers["content-type"].split(";")[0]
            async for line in reply.aiter_lines():
                if not line.startswith("data: "):
                    continue
                if line[6:] == "[DONE]":
                    plain_done = True
                    continue
                raw = opener.open(json.loads(line[6:])["dlne_e2e"])
                if raw is not None:
                    texts.append(json.loads(raw))
        return {"status": 200, "ctype": ctype, "finished": opener.finished, "plain_done": plain_done,
                "chunks": len(texts) > 0, "first_has_role_or_text": True}
    elif how == "plain_stream":
        async with client.stream("POST", url, json=CHAT) as reply:
            body = (await reply.aread()).decode()
        return {"status": reply.status_code, "ctype": reply.headers["content-type"].split(";")[0],
                "done": body.rstrip().endswith("data: [DONE]"), "has_text": '"content"' in body}
    elif how == "sealed_abort":
        envelope, key = e2e.seal_request(CHAT, pubkey)
        opener = e2e.ResponseOpener(key)
        plain_done = False
        try:
            async with client.stream("POST", url, json=envelope) as reply:
                async for line in reply.aiter_lines():
                    if line.startswith("data: ") and line[6:] != "[DONE]":
                        opener.open(json.loads(line[6:])["dlne_e2e"])
                    elif line == "data: [DONE]":
                        plain_done = True
            broke = False
        except (httpx.TransportError, httpx.StreamError):
            broke = True
        return {"broke_or_cut": broke or not opener.finished, "sealed_end": opener.finished, "plain_done": plain_done}
    else:
        raise AssertionError(how)
    error = r.json().get("error") if r.headers.get("content-type", "").startswith("application/json") else None
    return {"status": r.status_code, "error": error, "retry_after": r.headers.get("retry-after"),
            "ctype": r.headers.get("content-type", "").split(";")[0]}


def _real_member(tmp_path, upstream_port: int):
    from engine.coding_member import CodingMember
    member = CodingMember(str(tmp_path), lambda: [], port=free_port(), llama_port=upstream_port, keyring=e2e.KeyRing())
    assert member.start()
    member.process, member.ready = AliveProcess(), True
    member.serving, member.model = "t", "m.gguf"
    return member


def test_the_fake_member_answers_like_the_real_member_proxy(tmp_path):
    async def scenario():
        log = fakes.EventLog()
        upstream = fakes.FakeLocalEngine(free_port(), free_port(), {"slots": 4, "decode_tps": 600.0, "prefill_tps": 50000.0},
                                         log, scale=1.0)
        await upstream.start()
        real = _real_member(tmp_path, upstream.port)
        fake = fakes.FakeMember(5, {"slots": 2, "decode_tps": 600.0, "prefill_tps": 50000.0}, "http://127.0.0.1:1",
                                "k" * 16, log, 1.0, free_port())
        await fake.server.start()
        results = {}
        try:
            async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
                targets = {"real": (f"http://127.0.0.1:{real.port}", real), "fake": (f"http://127.0.0.1:{fake.port}", fake)}
                for how in ("bad_json", "not_object", "bad_envelope", "replay", "wrong_key", "get_chat", "sealed_stream",
                            "plain_stream"):
                    for name, (base, who) in targets.items():
                        results[(how, name)] = await _outcome(client, base, who.pubkey if name == "real" else who.keyring.public_b64, how)
                # not ready
                real.ready = False
                fake.ready = False
                for name, (base, who) in targets.items():
                    results[("not_ready", name)] = await _outcome(client, base, who.pubkey if name == "real" else who.keyring.public_b64,
                                                                  "bad_json")
                    results[("health_not_ready", name)] = (await client.get(f"{base}/health")).status_code
                real.ready = True
                fake.ready = True
                for name, (base, who) in targets.items():
                    body = (await client.get(f"{base}/health")).json()
                    results[("health", name)] = {k: v for k, v in body.items() if k != "trust_id"}
                # busy: three at once on two slots, the third is refused in the clear and told to retry
                slow = {**CHAT, "x_load": {"rid": "slow", "sid": "s2", "otok": 600}}
                for name, (base, who) in targets.items():
                    pub = who.pubkey if name == "real" else who.keyring.public_b64

                    async def hold():
                        envelope, _k = e2e.seal_request(slow, pub)
                        async with client.stream("POST", f"{base}/v1/chat/completions", json=envelope) as reply:
                            async for _ in reply.aiter_lines():
                                pass

                    holds = [asyncio.create_task(hold()) for _ in range(2)]
                    await asyncio.sleep(0.4)
                    envelope, _k = e2e.seal_request(CHAT, pub)
                    third = await client.post(f"{base}/v1/chat/completions", json=envelope)
                    results[("busy_third", name)] = {"status": third.status_code, "retry_after": third.headers.get("retry-after"),
                                                     "error": third.json().get("error")}
                    for task in holds:
                        task.cancel()
                    await asyncio.gather(*holds, return_exceptions=True)
                    await asyncio.sleep(0.3)
                # a stream that breaks: no sealed end, and no plain [DONE] either
                upstream.engine.inject["die_mid_rate"] = 1.0
                fake.engine.inject["die_mid_rate"] = 1.0
                for name, (base, who) in targets.items():
                    results[("abort", name)] = await _outcome(client, base, who.pubkey if name == "real" else who.keyring.public_b64,
                                                              "sealed_abort")
        finally:
            real.stop()
            await fake.server.stop()
            await upstream.stop()
        return results

    results = asyncio.run(scenario())
    for how in sorted({k[0] for k in results}):
        assert results[(how, "real")] == results[(how, "fake")], (how, results[(how, "real")], results[(how, "fake")])
    assert results[("busy_third", "fake")]["status"] == 503 and results[("busy_third", "fake")]["retry_after"] == "2"
    assert results[("not_ready", "fake")]["status"] == 503 and results[("not_ready", "fake")]["retry_after"] == "5"
    assert results[("replay", "fake")]["status"] == 400
    assert results[("abort", "fake")] == {"broke_or_cut": True, "sealed_end": False, "plain_done": False}


# ======================================================================================
# The analysis, on records whose answers are known
# ======================================================================================

def _call(rid, sid, call, tier, t_call, recv, t_end, served_by="local", frames=None, cached=0, **extra):
    row = {"k": "call", "rid": rid, "sid": sid, "agent": 0, "tier": tier, "priv": "open", "task": 1, "call": call,
           "t_call": t_call, "t_first": recv + 0.1, "t_end": t_end, "outcome": "ok", "otok": 100, "attempts": [{"route": False}],
           "frames": frames or [], "polls": [], "route_ms": [], "recv": recv, "served_by": served_by, "cached": cached}
    row.update(extra)
    return row


def test_sticky_rate_counts_only_pairs_where_the_member_stayed():
    calls = [_call("a1", "s", 1, "trial", 0, 0.1, 1, "m00"), _call("a2", "s", 2, "trial", 2, 2.1, 3, "m00", cached=500),
             _call("a3", "s", 3, "trial", 4, 4.1, 5, "m01"),                        # m00 was alive: a miss
             _call("b1", "t", 1, "trial", 0, 0.1, 1, "m02"), _call("b2", "t", 2, "trial", 3, 3.1, 4, "m03")]
    events = [{"k": "member", "t": 2.0, "op": "die", "who": "m02"}]                 # m02 left between b1 and b2
    sticky = coding_load._sticky(calls, events)
    assert sticky["pairs"] == 3 and sticky["hit_rate"] == pytest.approx(1 / 3, abs=0.01)
    assert sticky["pairs_member_stayed"] == 2 and sticky["hit_rate_member_stayed"] == 0.5


def test_a_resent_envelope_or_one_seen_by_two_members_is_found():
    events = [{"k": "req", "t": 1, "who": "m00", "epk": "e1", "sealed": True, "opened": True},
              {"k": "req", "t": 2, "who": "m00", "epk": "e1", "sealed": True, "opened": False, "code": "replay"},
              {"k": "req", "t": 3, "who": "m01", "epk": "e2", "sealed": True, "opened": True},
              {"k": "req", "t": 4, "who": "m02", "epk": "e2", "sealed": True, "opened": False, "code": "auth"}]
    out = coding_load._reroute([], events)
    assert out["envelopes_sent_twice"] == 2 and out["envelopes_seen_by_two_members"] == 1
    assert out["wrong_member_deliveries_auth_failures"] == 1


def test_place_in_line_is_scored_against_who_was_really_ahead():
    # two slots busy; A (key) and B (trial) wait; C (key) arrives later and is served before B
    calls = [
        _call("A", "sa", 1, "key", 0.0, 5.0, 9.0, frames=[[0.1, 1, 2, 2, 2, None, "stream"]]),
        _call("B", "sb", 1, "trial", 0.5, 12.0, 15.0, frames=[[0.6, 2, 2, 2, 2, None, "stream"], [7.0, 2, 3, 2, 2, None, "stream"]]),
        _call("C", "sc", 1, "key", 6.0, 9.0, 12.0, frames=[]),
    ]
    out = coding_load._place_accuracy(calls)
    assert out["scored_in_this_run"] and out["frames_scored"] == 3
    # at 0.1 A is first (truth 1); at 0.6 B has A ahead (truth 2); at 7.0 B has A gone... A served at 5, C waits ahead: truth 2
    assert out["exact"] == 1.0 and out["max_abs_error"] == 0


# ======================================================================================
# One small end-to-end run: a real coordinator process, a few agents
# ======================================================================================

def test_smoke_run_drives_a_real_coordinator(tmp_path):
    started = time.monotonic()
    spec = coding_load.make_spec("smoke")
    result = coding_load.run_profile(spec, str(tmp_path / "run"), repo=REPO_ROOT, python=sys.executable, log=lambda *a: None)
    report = result["report"]
    took = time.monotonic() - started
    print(coding_load.format_report(report))        # shown on a failure, or with -s
    print("notes:", result["notes"], "seconds:", round(took, 1))
    assert result["drained"], "every slot and queue place came back"
    assert result["coordinator_exit"] in (0, -15, None), result["coordinator_exit"]
    assert report["calls"]["total"] >= 6, report["calls"]
    ok = report["calls"]["outcomes"].get("ok", 0)
    cancelled = report["calls"]["outcomes"].get("cancelled", 0)
    assert ok >= 4 and ok + cancelled >= 0.8 * report["calls"]["total"], report["calls"]
    assert report["calls"]["outcomes"].get("exception", 0) == 0
    assert report["generic_clients"]["dlne_queue_frames_seen"] == 0, "a caller without X-DLNE-Session never sees a queue frame"
    assert report["reroute"]["envelopes_sent_twice"] == 0
    assert report["reroute"]["wrong_member_deliveries_auth_failures"] == 0
    assert report["reroute"]["answers_for_another_request"] == 0
    assert report["final"]["gate"]["in_use"] == 0 and report["final"]["gate"]["waiting"] == 0
    if result["report"]["members"]:
        assert report["member_share"] > 0, "with members on, some calls were served by a member"
    assert took < 20.0, f"the smoke run took {took:.1f} s"
