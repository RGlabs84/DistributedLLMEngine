"""
The phone's precise location through the endpoint (2.6): the model is told only that the tools know
where the user is, and the location is in no body sent to a model, on either phone path.
"""

import contextlib
import json
import logging
import os
from collections import OrderedDict

import httpx
import pytest

from engine import app_pool, coordinator, search
from engine import config as engine_config
from tests.test_app_lookups import tool_call
from tests.test_phone_lane_endpoint import (CHAT_A, REMOTE, DONE, client, env, phone, phone_body,  # noqa: F401
                                            servers, sse, system_of)
from tests.test_search import (FORECAST, FORECAST_ROUTE, SEARXNG, SEARXNG_ROUTE, Clock, FakeWeb)
from engine.priority import UsageWindow


LOCATION = {"lat": -37.8136, "lon": 144.9631}
NEEDLES = ("37.81", "144.96", "-37.8")


def tool_names(sent):
    return {t["function"]["name"]: t["function"] for t in sent.get("tools", [])}


@pytest.mark.anyio
async def test_a_located_phone_on_an_engine_lane_is_told_the_tools_know_and_nothing_else(env, caplog):
    caplog.set_level(0)
    async with client() as c:
        resp = await phone(c, phone_body("pizza near me", location=LOCATION))
    assert resp.status_code == 200
    (sent,) = env.engine.chats
    assert app_pool.LOCATION_INSTRUCTIONS in system_of(sent)
    assert "near_me" in tool_names(sent)["web_search"]["parameters"]["properties"]
    assert tool_names(sent)["weather"]["parameters"]["required"] == []
    wire = json.dumps(sent)
    assert not any(n in wire for n in NEEDLES) and "location" not in sent
    assert not any(n in caplog.text or n.encode() in resp.content for n in NEEDLES)


@pytest.mark.anyio
async def test_a_located_phone_on_the_cpu_pool_is_told_the_same(env):
    env.limits["engine_phone_slots"] = 0
    env.manager.phone_slots = 0
    async with client() as c:
        resp = await phone(c, phone_body("pizza near me", location=LOCATION))
    assert resp.status_code == 200 and env.engine.chats == []
    (sent,) = env.cpu.payloads
    assert app_pool.LOCATION_INSTRUCTIONS in system_of(sent)
    assert not any(n in json.dumps(sent) for n in NEEDLES)


@pytest.mark.anyio
@pytest.mark.parametrize("location", [None, "here", [1, 2], {"lat": 99, "lon": 0}, {"lat": "x", "lon": 1}, {}])
async def test_without_a_usable_location_the_request_is_exactly_as_it_was(env, location):
    async with client() as c:
        await phone(c, phone_body("pizza near me"))
        resp = await phone(c, phone_body("pizza near me", location=location))
    assert resp.status_code == 200
    plain, sent = env.engine.chats
    assert app_pool.LOCATION_INSTRUCTIONS not in system_of(sent)
    assert system_of(sent) == system_of(plain) and sent["tools"] == plain["tools"]
    assert "near_me" not in json.dumps(sent["tools"])


# ======================================================================================
# The pool: a weather call with no place survives the required pass
# ======================================================================================

from engine.app_pool import AppPool, Member  # noqa: E402
from tests.test_app_lookups import ANSWER, TODAY, Member_, Result, dlne, tool_call  # noqa: E402
import httpx  # noqa: E402

WEATHER_AND_WEB = [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
                   for name in ("weather", "web_search")]


async def run_pool(member, located):
    seen = []

    async def run_tool(name, arguments, first_n=1):
        seen.append((name, arguments))
        return Result(context_text="[1] 18 C, sunny")

    pool = AppPool()
    body = {"messages": [{"role": "user", "content": "What's the weather like?"}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(member.handler)) as client:
        [c async for c in pool.stream(body, [Member("w:m", 16.0, "small", True, 2, "http://member")], "small", client,
                                      tools=WEATHER_AND_WEB, run_tool=run_tool, today=TODAY, located=located)]
    return seen, member.requests


@pytest.mark.anyio
async def test_a_located_weather_call_with_no_place_is_run_not_dropped_by_the_required_pass():
    seen, requests = await run_pool(Member_(tool_call([("c1", "weather", {})]), ANSWER), located=True)
    assert seen == [("weather", {})]
    assert app_pool.LOCATION_INSTRUCTIONS in requests[0]["messages"][0]["content"]


@pytest.mark.anyio
async def test_an_unlocated_empty_call_is_dropped_as_before_and_the_question_is_searched():
    seen, requests = await run_pool(Member_(tool_call([("c1", "weather", {})]), ANSWER), located=False)
    assert [name for name, _ in seen] == ["web_search"]
    assert app_pool.LOCATION_INSTRUCTIONS not in requests[0]["messages"][0]["content"]


# ======================================================================================
# 2.6.1: the whole path through the endpoint with a real lookup, and what a report keeps
# ======================================================================================

TOWNS = {(-37.814, 144.963): "Melbourne, Victoria, Australia"}


class RoutedWeb(httpx.AsyncBaseTransport):
    """The search services are the fake web; everything else (the stand-in engine, the CPU member) is really sent."""

    def __init__(self, web):
        self.web = web
        self.real = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        if request.url.port == 8888 or request.url.host != "127.0.0.1":
            return await self.web(request)
        return await self.real.handle_async_request(request)


@pytest.fixture
def web(env, monkeypatch, dlne_root):
    """The lookups really run (engine/search.py), against a fake web; nothing else of search is replaced."""
    settings = engine_config.get_worker_limits()
    clock, fake = Clock(), FakeWeb()
    monkeypatch.setattr(search, "get_worker_limits", lambda: dict(settings))
    monkeypatch.setattr(search, "get_base_dir", lambda: str(dlne_root))
    monkeypatch.setattr(search, "_clock", clock)
    monkeypatch.setattr(search, "_today", lambda: clock.day)
    monkeypatch.setattr(search, "_cache", search._Cache())
    monkeypatch.setattr(search, "_usage", search._Usage())
    monkeypatch.setattr(search, "_per_network", UsageWindow())
    monkeypatch.setattr(search, "_logged", {})
    monkeypatch.setattr(search, "_places", OrderedDict())
    monkeypatch.setattr(search, "RETRY_PAUSE_SECONDS", 0.0)
    upstreams = search._make_upstreams()
    upstreams["brave"].min_gap = upstreams["gdelt"].min_gap = 0.0
    monkeypatch.setattr(search, "_upstreams", upstreams)
    monkeypatch.setattr(search.places, "describe", lambda lat, lon: TOWNS.get((round(lat, 3), round(lon, 3))))

    @contextlib.asynccontextmanager
    async def routed():
        async with httpx.AsyncClient(transport=RoutedWeb(fake)) as http:
            yield http

    monkeypatch.setattr(coordinator, "engine_client", routed)
    monkeypatch.setattr(coordinator, "get_base_dir", lambda: str(dlne_root))
    monkeypatch.setattr(coordinator, "app_report_window", UsageWindow())
    return fake


def wire_of(env):
    return json.dumps(env.engine.chats)


@pytest.mark.anyio
async def test_a_local_search_runs_for_real_and_the_coordinates_reach_no_engine_log_cache_or_report(env, web, caplog):
    caplog.set_level(0)
    web.on(SEARXNG_ROUTE, SEARXNG)
    env.engine.script = [[tool_call([("c1", "web_search", {"query": "pizza", "near_me": True})])],
                         [sse("Try the place on Collins Street in Melbourne."), DONE]]
    async with client() as c:
        resp = await phone(c, phone_body("where can I eat", location=LOCATION))
        report = await c.post("/v1/app/report", json={"reason": "wrong", "question": "where can I eat",
                                                      "answer": "Try the place on Collins Street in Melbourne, Victoria.",
                                                      "note": "melbourne is wrong"})

    assert resp.status_code == 200 and b"Collins Street" in resp.content
    searched = [r for r in web.requests if r.url.path == "/search"]
    assert [r.url.params["q"] for r in searched] == ["pizza near Melbourne, Victoria"], "the lookup ran, with a town only"
    assert len(env.engine.chats) == 2, "the model was asked again with the results"
    for needle in NEEDLES:
        assert needle not in wire_of(env), "no request to the engine, either round, carries the coordinates"
        assert not any(needle in str(r.url) or needle.encode() in r.content for r in web.requests)
        assert needle not in caplog.text and needle.encode() not in resp.content
        assert needle not in json.dumps(vars(search._cache), default=str)
    assert len(search._cache) == 0, "a located lookup is neither read from nor written to the cache"
    # the second round carries the evidence the lookup returned, and still no location
    assert env.engine.chats[1]["messages"][-1]["role"] == "tool"
    assert "location" not in env.engine.chats[1]

    assert report.status_code == 200
    stored = open(os.path.join(coordinator.get_base_dir(), "config", coordinator.APP_REPORTS_FILE)).read()
    assert "Melbourne" not in stored and "melbourne" not in stored.lower(), "a report never keeps the derived town"
    assert "[place]" in stored and "Collins Street" in stored
    for needle in NEEDLES:
        assert needle not in stored


@pytest.mark.anyio
async def test_local_weather_runs_for_real_and_only_the_forecast_service_gets_the_rounded_coordinates(env, web, caplog):
    caplog.set_level(0)
    web.on(FORECAST_ROUTE, FORECAST)
    env.engine.script = [[tool_call([("c1", "weather", {})])], [sse("Mild and dry."), DONE]]
    async with client() as c:
        resp = await phone(c, phone_body("how is the weather", location=LOCATION))

    assert resp.status_code == 200 and b"Mild and dry." in resp.content
    (forecast,) = [r for r in web.requests if r.url.path == "/v1/forecast"]
    assert (forecast.url.params["latitude"], forecast.url.params["longitude"]) == ("-37.8140", "144.9630")
    for needle in ("37.8136", "144.9631"):
        assert needle not in wire_of(env) and needle not in caplog.text and needle.encode() not in resp.content
    assert "Melbourne" in json.dumps(env.engine.chats[1]), "the model reads the results, which name the town"
    assert [r.url.host for r in web.requests] == ["api.open-meteo.com"], "no other service heard of the phone"


@pytest.mark.anyio
async def test_a_phone_in_the_sea_is_told_its_place_is_unknown_and_no_search_is_made(env, web):
    web.on(SEARXNG_ROUTE, SEARXNG)
    env.engine.script = [[tool_call([("c1", "web_search", {"query": "petrol station near me"})])],
                         [sse("I can't tell where you are."), DONE]]
    async with client() as c:
        resp = await phone(c, phone_body("petrol station near me", location={"lat": 0.1, "lon": 0.1}))

    assert resp.status_code == 200 and web.requests == []
    tool_message = env.engine.chats[1]["messages"][-1]
    assert tool_message["role"] == "tool" and "place is not known" in tool_message["content"]
    assert b"search_unavailable" in resp.content


@pytest.mark.anyio
async def test_a_report_from_a_network_that_never_did_a_located_lookup_is_kept_as_sent(env, web):
    async with client() as c:
        await c.post("/v1/app/report", json={"reason": "wrong", "answer": "Melbourne is nice", "question": "q"})
    stored = open(os.path.join(coordinator.get_base_dir(), "config", coordinator.APP_REPORTS_FILE)).read()
    assert "Melbourne is nice" in stored


@pytest.mark.anyio
async def test_a_location_with_enormous_numbers_is_ignored_on_both_paths_not_a_500(env, web):
    raw = '{"messages": [{"role": "user", "content": "pizza near me"}], "location": {"lat": %s, "lon": 0}}' % ("9" * 400)
    for lanes in (True, False):
        env.limits["engine_phone_slots"] = PHONE if lanes else 0
        async with client() as c:
            resp = await c.post("/v1/app/chat/completions", content=raw, headers={"content-type": "application/json"})
        assert resp.status_code == 200
    sent = env.engine.chats[0]
    assert app_pool.LOCATION_INSTRUCTIONS not in system_of(sent), "treated as no location at all"


PHONE = 2


@pytest.mark.anyio
@pytest.mark.parametrize("lanes", [True, False])
async def test_a_phone_with_no_location_asking_near_me_is_told_to_name_a_town_and_nothing_is_searched(env, web, lanes):
    env.limits["engine_phone_slots"] = PHONE if lanes else 0
    server = env.engine if lanes else env.cpu
    web.on(SEARXNG_ROUTE, SEARXNG)
    script = [[tool_call([("c1", "web_search", {"query": "pizza near me"})])], [sse("Which town are you in?"), DONE]]
    if lanes:
        env.engine.script = script
    else:
        env.cpu.script = script
    async with client() as c:
        resp = await phone(c, phone_body("pizza near me"))

    assert resp.status_code == 200 and b"search_unavailable" in resp.content
    assert web.requests == [], "the engines never see a bare 'near me'"
    sent = (env.engine.chats if lanes else env.cpu.payloads)[1]
    assert sent["messages"][-1]["role"] == "tool" and "turn on location sharing" in sent["messages"][-1]["content"]
    assert len(search._cache) == 0
