"""
The phone's precise location (2.6): used only by the lookup tools, never in a prompt, a log or a result.

The services are the fakes of tests/test_search.py (no network); the town list is replaced by a
dictionary so these tests do not depend on the place data.
"""

import json
import logging
from collections import OrderedDict

import httpx
import pytest

from engine import app_pool, coordinator, search
from tests.test_search import (FORECAST, FORECAST_ROUTE, GEOCODE, GEOCODE_ROUTE, NETWORK, SEARXNG, SEARXNG_ROUTE,
                               anyio_backend, env)  # noqa: F401 - fixtures

HERE = (-37.8136, 144.9631)       # what a phone sends: far more precise than a forecast needs
THERE = (48.8566, 2.3522)
TOWNS = {(-37.814, 144.963): "Melbourne, Victoria, Australia", (48.857, 2.352): "Paris, Île-de-France, France"}


@pytest.fixture(autouse=True)
def towns(monkeypatch):
    monkeypatch.setattr(search.places, "describe", lambda lat, lon: TOWNS.get((round(lat, 3), round(lon, 3))))


@pytest.fixture(autouse=True)
def no_towns_remembered(monkeypatch):
    monkeypatch.setattr(search, "_places", OrderedDict())


async def lookup(env, name, arguments, location=HERE):
    async with httpx.AsyncClient(transport=httpx.MockTransport(env.web)) as client:
        return await search.run_tool(name, arguments, client=client, network_id=NETWORK, location=location)


def sent_queries(env):
    return [r.url.params["q"] for r in env.web.calls(SEARXNG_ROUTE)]


# ======================================================================================
# Reading the location
# ======================================================================================

@pytest.mark.parametrize("raw", [None, "1,2", [1, 2], {}, {"lat": 1}, {"lat": "1", "lon": "2"}, {"lat": True, "lon": 2},
                                 {"lat": float("nan"), "lon": 1}, {"lat": float("inf"), "lon": 1},
                                 {"lat": 91, "lon": 0}, {"lat": 0, "lon": 181}, {"lat": -90.1, "lon": 0},
                                 {"lat": 10**400, "lon": 0}, {"lat": 0, "lon": -10**400}, {"lat": 1e400, "lon": 0},
                                 {"lat": 10**4300 // 7, "lon": 10**30}])
def test_a_location_that_is_not_two_numbers_in_range_is_ignored(raw):
    assert search.clean_location(raw) is None


def test_a_good_location_is_read_and_the_extremes_are_in_range():
    assert search.clean_location({"lat": -37.8136, "lon": 144.9631}) == (-37.8136, 144.9631)
    assert search.clean_location({"lat": 90, "lon": -180, "accuracy": 5}) == (90.0, -180.0)


def test_the_located_tools_add_near_me_and_a_placeless_weather_and_the_plain_tools_stay_as_they_were():
    plain = {t["function"]["name"]: t["function"] for t in search.TOOLS}
    located = {t["function"]["name"]: t["function"] for t in search.TOOLS_LOCATED}
    assert all("near_me" not in f["parameters"]["properties"] for f in plain.values())
    assert plain["weather"]["parameters"]["required"] == ["location"]
    for name in ("news", "web_search"):
        assert located[name]["parameters"]["properties"]["near_me"]["type"] == "boolean"
        assert located[name]["parameters"]["required"] == ["query"]
    assert located["weather"]["parameters"]["required"] == []
    assert "near_me" not in located["weather"]["parameters"]["properties"] and "near_me" not in located["wikipedia"]["parameters"]["properties"]


# ======================================================================================
# Weather
# ======================================================================================

@pytest.mark.anyio
@pytest.mark.parametrize("arguments", [{}, {"location": ""}, {"location": "here"}, {"location": "My Location"},
                                       {"location": "near me"}, {"location": "Here."}])
async def test_weather_with_no_place_or_here_asks_the_forecast_at_the_rounded_coordinates(env, arguments):
    env.web.on(FORECAST_ROUTE, FORECAST)
    result = await lookup(env, "weather", arguments)
    assert result.ok
    params = env.web.calls(FORECAST_ROUTE)[0].url.params
    assert (params["latitude"], params["longitude"]) == ("-37.8140", "144.9630")      # ~100 m
    assert env.web.calls(GEOCODE_ROUTE) == []
    assert result.sources[0].title == "Open-Meteo forecast: Melbourne, Victoria, Australia"
    assert "37.81" not in result.context_text and "144.96" not in result.context_text


@pytest.mark.anyio
async def test_weather_in_a_place_nobody_listed_is_labelled_your_location(env):
    env.web.on(FORECAST_ROUTE, FORECAST)
    result = await lookup(env, "weather", {}, location=(0.1234, 0.5678))
    assert result.sources[0].title == "Open-Meteo forecast: your location"


@pytest.mark.anyio
async def test_a_named_place_still_goes_through_geocoding_even_with_a_location(env):
    env.web.on(GEOCODE_ROUTE, GEOCODE)
    env.web.on(FORECAST_ROUTE, FORECAST)
    result = await lookup(env, "weather", {"location": "Melbourne"}, location=THERE)
    assert result.ok and len(env.web.calls(GEOCODE_ROUTE)) == 1
    assert env.web.calls(FORECAST_ROUTE)[0].url.params["latitude"] == "-37.8140"


@pytest.mark.anyio
async def test_without_a_location_weather_is_exactly_as_before(env):
    result = await lookup(env, "weather", {}, location=None)
    assert not result.ok and "location" in result.reason and env.web.requests == []
    env.web.on(GEOCODE_ROUTE, {"generationtime_ms": 0.1})
    result = await lookup(env, "weather", {"location": "Atlantis"}, location=None)
    assert not result.ok and result.reason.startswith("Couldn't find a place called")
    assert len(env.web.calls(GEOCODE_ROUTE)) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("place", ["here", "Here.", "near me", "my location", "my city", "around here"])
async def test_here_with_no_location_is_not_geocoded_as_a_place_called_here(env, place):
    result = await lookup(env, "weather", {"location": place}, location=None)
    assert not result.ok and result.reason == search.NO_LOCATION
    assert env.web.requests == [], "no geocoding, no forecast, and no allowance spent"


@pytest.mark.anyio
async def test_located_weather_bypasses_the_cache_both_ways_and_leaves_nothing_behind(env):
    env.web.on(FORECAST_ROUTE, FORECAST)
    for _ in range(2):
        result = await lookup(env, "weather", {})
        assert result.ok and not result.cached
    assert len(env.web.calls(FORECAST_ROUTE)) == 2
    assert len(search._cache) == 0 and search._cache._entries == {}


@pytest.mark.anyio
async def test_a_cached_plain_answer_is_not_served_to_a_located_lookup_and_plain_lookups_still_cache(env):
    env.web.on(GEOCODE_ROUTE, GEOCODE)
    env.web.on(FORECAST_ROUTE, FORECAST)
    await lookup(env, "weather", {"location": "Melbourne"}, location=None)    # a placeless phone: geocoded
    located = await lookup(env, "weather", {"location": "here"})
    assert not located.cached and located.sources[0].title.endswith("Melbourne, Victoria, Australia")
    assert (await lookup(env, "weather", {"location": "Melbourne"}, location=None)).cached
    assert len(search._cache) == 2                                              # geocode + forecast of the plain lookup only


# ======================================================================================
# Web and news search
# ======================================================================================

@pytest.mark.anyio
@pytest.mark.parametrize("query, expected", [
    ("pizza near me", "pizza near Melbourne, Victoria"),
    ("Best coffee NEAR ME open now", "Best coffee open now near Melbourne, Victoria"),
    ("chemist nearby", "chemist near Melbourne, Victoria"),
    ("nearby restaurants", "restaurants near Melbourne, Victoria"),
    ("is there a bakery around here?", "is there a bakery near Melbourne, Victoria"),
    ("tyre shop close to me", "tyre shop near Melbourne, Victoria"),
    ("gyms around me", "gyms near Melbourne, Victoria"),
    ("parking near here", "parking near Melbourne, Victoria"),
])
async def test_a_phrase_for_near_me_becomes_the_town(env, query, expected):
    env.web.on(SEARXNG_ROUTE, SEARXNG)
    assert (await lookup(env, "web_search", {"query": query})).ok
    assert sent_queries(env) == [expected]


@pytest.mark.anyio
@pytest.mark.parametrize("flag", [True, "true", "True"])
async def test_the_models_near_me_flag_appends_the_town(env, flag):
    env.web.on(SEARXNG_ROUTE, SEARXNG)
    await lookup(env, "web_search", {"query": "vegan pizza", "near_me": flag})
    assert sent_queries(env) == ["vegan pizza near Melbourne, Victoria"]


@pytest.mark.anyio
@pytest.mark.parametrize("arguments", [{"query": "vegan pizza"}, {"query": "vegan pizza", "near_me": False},
                                       {"query": "vegan pizza", "near_me": "no"}, {"query": "a local variable in python"},
                                       {"query": "the nearest star"},
                                       {"query": "nearly there"}])
async def test_a_query_that_is_not_local_is_sent_as_it_is(env, arguments):
    env.web.on(SEARXNG_ROUTE, SEARXNG)
    await lookup(env, "web_search", arguments)
    assert sent_queries(env) == [arguments["query"]]


@pytest.mark.anyio
async def test_news_is_localized_too_and_the_fallback_search_gets_the_same_query(env):
    env.web.on("api.gdeltproject.org/api/v2/doc/doc", {"articles": []})
    env.web.on(SEARXNG_ROUTE, SEARXNG)
    await lookup(env, "news", {"query": "road closures", "near_me": True})
    gdelt = env.web.calls("api.gdeltproject.org/api/v2/doc/doc")[0].url.params["query"]
    assert "road closures near Melbourne Victoria" in gdelt
    assert all("near Melbourne, Victoria" in q for q in sent_queries(env))


@pytest.mark.anyio
@pytest.mark.parametrize("arguments", [{"query": "tacos near me"}, {"query": "chemist nearby"},
                                       {"query": "vegan pizza", "near_me": True}, {"query": "parking near here"}])
@pytest.mark.parametrize("tool", ["web_search", "news"])
async def test_a_near_me_search_from_a_phone_with_no_location_is_not_made(env, arguments, tool):
    """Sent as it is, the engines would answer for this server's own address and the model would call it local."""
    env.web.on(SEARXNG_ROUTE, SEARXNG)
    before = search._per_network.peek(NETWORK, 30, 3600.0)
    result = await lookup(env, tool, arguments, location=None)

    assert not result.ok and result.reason == search.NO_LOCATION
    assert "turn on location sharing" in result.reason and "name a town" in result.reason
    assert env.web.requests == [], "no search ran"
    assert len(search._cache) == 0, "nothing cached"
    assert search._per_network.peek(NETWORK, 30, 3600.0) == before, "nothing counted against the allowance"
    assert search._usage.snapshot()["tools"] == {}, "and no lookup recorded"


@pytest.mark.anyio
@pytest.mark.parametrize("arguments, sent", [
    ({"query": "restaurants in Rome near me"}, "restaurants in Rome near me"),
    ({"query": "pizza in Paris", "near_me": True}, "pizza in Paris"),
    ({"query": "how do nearby stars look"}, "how do nearby stars look"),
    ({"query": "python release"}, "python release"),
])
async def test_a_search_with_no_location_that_names_a_place_or_is_not_local_is_unaffected(env, arguments, sent):
    env.web.on(SEARXNG_ROUTE, SEARXNG)
    assert (await lookup(env, "web_search", arguments, location=None)).ok
    assert sent_queries(env) == [sent]


@pytest.mark.anyio
@pytest.mark.parametrize("arguments", [{"query": "pizza near me"}, {"query": "pizza", "near_me": True},
                                       {"query": "chemist nearby"}])
@pytest.mark.parametrize("tool", ["web_search", "news"])
async def test_no_town_near_the_phone_means_no_search_and_the_model_is_told_the_place_is_unknown(env, arguments, tool):
    env.web.on(SEARXNG_ROUTE, SEARXNG)
    result = await lookup(env, tool, arguments, location=(0.1, 0.1))                     # the sea: no town
    assert not result.ok and result.reason == search.PLACE_UNKNOWN
    assert "not known" in result.reason and "ask which town" in result.reason
    assert env.web.requests == [], "the engines never see a bare 'near me', and nothing is cached"
    assert len(search._cache) == 0


@pytest.mark.anyio
async def test_a_town_with_one_name_and_a_repeated_name_read_well(env, monkeypatch):
    monkeypatch.setattr(search.places, "describe", lambda lat, lon: "Singapore, Singapore, Singapore")
    env.web.on(SEARXNG_ROUTE, SEARXNG)
    await lookup(env, "web_search", {"query": "pizza near me"})
    assert sent_queries(env) == ["pizza near Singapore"]


@pytest.mark.anyio
async def test_a_search_rewritten_with_the_town_bypasses_the_cache_and_leaves_nothing_behind(env):
    env.web.on(SEARXNG_ROUTE, SEARXNG)
    for location in (HERE, HERE, THERE):
        result = await lookup(env, "web_search", {"query": "pizza near me"}, location=location)
        assert result.ok and not result.cached
    assert sent_queries(env) == ["pizza near Melbourne, Victoria"] * 2 + ["pizza near Paris, Île-de-France"]
    assert len(search._cache) == 0 and search._cache._entries == {}


@pytest.mark.anyio
async def test_searches_that_were_not_rewritten_cache_as_today(env):
    env.web.on(SEARXNG_ROUTE, SEARXNG)
    await lookup(env, "web_search", {"query": "python release"})                 # located phone, not a local question
    assert (await lookup(env, "web_search", {"query": "python release"}, location=THERE)).cached
    await lookup(env, "web_search", {"query": "pizza in Paris near me"}, location=None)   # names a place: cached as today
    assert (await lookup(env, "web_search", {"query": "pizza in Paris near me"}, location=None)).cached
    assert len(env.web.calls(SEARXNG_ROUTE)) == 2


@pytest.mark.anyio
async def test_no_coordinates_reach_a_search_engine_a_log_or_a_result(env, caplog):
    env.web.on(SEARXNG_ROUTE, SEARXNG)
    env.web.on(FORECAST_ROUTE, {"nonsense": True})                  # the weather service fails: a warning is logged
    caplog.set_level(logging.DEBUG)
    results = [await lookup(env, "web_search", {"query": "pizza near me", "near_me": True}),
               await lookup(env, "weather", {})]
    seen = [str(r.url) for r in env.web.calls(SEARXNG_ROUTE)]
    for needle in ("37.81", "144.96", "-37.8"):
        assert not any(needle in url for url in seen)
        assert not any(needle in w for w in env.warnings)
        assert not any(needle in rec.getMessage() for rec in caplog.records)
        assert not any(needle in json.dumps(vars(r), default=str) for r in results)
    assert env.warnings                                              # the failure was logged, without the place


# ======================================================================================
# The phone endpoint: the prompt and the tools
# ======================================================================================

def test_the_prompt_line_is_added_only_for_a_located_phone_with_lookups():
    plain = app_pool._system_prompt(True, "Monday")
    located = app_pool._system_prompt(True, "Monday", True)
    assert located == plain + app_pool.LOCATION_INSTRUCTIONS
    assert app_pool._system_prompt(False, "Monday", True) == app_pool._system_prompt(False, "Monday")
    assert app_pool.LOCATION_INSTRUCTIONS == (" The lookup tools know where the user is and you do not: for a "
                                             "question about places near the user, set near_me on the search; "
                                             "for the weather where they are, call weather with no place.")


def test_the_endpoint_reads_the_location_and_hands_it_to_the_tools_only():
    body = {"messages": [{"role": "user", "content": "hi"}], "location": {"lat": -37.8136, "lon": 144.9631}}
    location = coordinator._app_location(body)
    assert location == (-37.8136, 144.9631)
    tools, run = coordinator._app_lookup_tools("n", location)
    assert tools is search.TOOLS_LOCATED and run.keywords["location"] == location
    tools, run = coordinator._app_lookup_tools("n", coordinator._app_location({"location": {"lat": 99, "lon": 0}}))
    assert tools is search.TOOLS and "location" not in run.keywords


# ======================================================================================
# 2.6.1: malformed calls, places a query already names, a town that is never cut, and reports
# ======================================================================================

@pytest.mark.anyio
@pytest.mark.parametrize("arguments", ['{"location": "Pa', "[1]", "null", "not json", None, 5,
                                       {"location": 5}, {"location": ["Paris"]}, {"location": {"x": 1}},
                                       {"location": True}])
async def test_a_weather_call_that_is_malformed_is_never_the_users_own_place(env, arguments):
    result = await lookup(env, "weather", arguments)
    assert not result.ok and "non-empty" in result.reason
    assert env.web.requests == [], "no forecast was asked for, here or anywhere"


@pytest.mark.anyio
@pytest.mark.parametrize("arguments", [{}, {"location": ""}, {"location": None}, {"location": "  "}])
async def test_a_located_weather_call_that_deliberately_has_no_place_is_the_users_own(env, arguments):
    env.web.on(FORECAST_ROUTE, FORECAST)
    assert (await lookup(env, "weather", arguments)).ok


@pytest.mark.anyio
@pytest.mark.parametrize("arguments", [{"query": 5}, {"query": ["pizza near me"]}, '{"query": "pizza near', ""])
@pytest.mark.parametrize("tool", ["web_search", "news", "wikipedia"])
async def test_a_search_call_that_is_malformed_makes_no_request(env, arguments, tool):
    result = await lookup(env, tool, arguments)
    assert not result.ok and env.web.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize("arguments, expected", [
    ({"query": "how do nearby stars look"}, "how do nearby stars look"),
    ({"query": "wifi nearby channels explained today"}, "wifi nearby channels explained today"),
    ({"query": "restaurants in Rome", "near_me": True}, "restaurants in Rome"),
    ({"query": "pizza in Paris near me"}, "pizza in Paris"),
    ({"query": "Pizza at Luigi's nearby"}, "Pizza at Luigi's"),
    ({"query": "chemist near me"}, "chemist near Melbourne, Victoria"),
])
async def test_the_town_is_not_added_to_a_question_that_is_not_local_or_already_names_a_place(env, arguments, expected):
    env.web.on(SEARXNG_ROUTE, SEARXNG)
    assert (await lookup(env, "web_search", arguments)).ok
    assert sent_queries(env) == [expected]


@pytest.mark.anyio
async def test_a_query_that_names_its_own_place_is_cached_as_any_other_and_a_local_one_is_not(env):
    env.web.on(SEARXNG_ROUTE, SEARXNG)
    await lookup(env, "web_search", {"query": "pizza in Paris near me"})
    assert (await lookup(env, "web_search", {"query": "pizza in Paris"}, location=THERE)).cached
    assert len(env.web.calls(SEARXNG_ROUTE)) == 1


@pytest.mark.anyio
async def test_a_long_local_query_is_cut_before_the_town_not_after_it(env):
    env.web.on(SEARXNG_ROUTE, SEARXNG)
    long = " ".join(["gluten free vegan pizza"] * 8)[:188] + " near me"        # 196 characters: the town does not fit after it
    assert len(long) < search.QUERY_MAX_CHARS
    await lookup(env, "web_search", {"query": long})
    (sent,) = sent_queries(env)
    assert sent.endswith(" near Melbourne, Victoria") and len(sent) <= search.QUERY_MAX_CHARS
    assert "near me" not in sent and not sent.startswith("near")


@pytest.mark.anyio
async def test_a_query_that_is_only_the_phrase_is_not_searched(env):
    result = await lookup(env, "web_search", {"query": "near me"})
    assert not result.ok and "non-empty" in result.reason and env.web.requests == []


def test_places_a_networks_located_lookups_used_are_taken_out_of_its_reports(env):
    search._remember_place(NETWORK, "Melbourne, Victoria, Australia")
    text = "In Melbourne, Victoria it is 12 C. melbourne is cool; Melbourne, Victoria, Australia. Melbournesque stays."

    cleaned = search.redact_places(NETWORK, text)

    assert cleaned == "In [place] it is 12 C. [place] is cool; [place]. Melbournesque stays."
    assert search.redact_places("someone else", text) == text, "only the network whose lookup used the town"
    env.clock.advance(300)
    assert search.redact_places(NETWORK, text) == cleaned, "still remembered within the hours"
    env.clock.advance(61)
    assert search.redact_places(NETWORK, text) == text and NETWORK not in search._places


def test_the_memory_of_towns_is_bounded_and_ignores_nothing_names(env, monkeypatch):
    monkeypatch.setattr(search, "PLACE_MEMORY_MAX", 3)
    for i in range(5):
        search._remember_place(f"net{i}", f"Town{i}, Region")
    assert list(search._places) == ["net2", "net3", "net4"]
    search._remember_place("empty", "")
    search._remember_place("tiny", "Å")
    assert list(search._places) == ["net2", "net3", "net4"], "no name worth taking out, nothing kept"


@pytest.mark.anyio
async def test_a_located_lookup_notes_the_town_it_used_and_an_unlocated_one_notes_nothing(env):
    env.web.on(SEARXNG_ROUTE, SEARXNG)
    env.web.on(FORECAST_ROUTE, FORECAST)
    await lookup(env, "web_search", {"query": "python release"})                       # not local
    assert search._places == {}
    await lookup(env, "weather", {})
    assert search.redact_places(NETWORK, "Melbourne") == "[place]"
    search._places.clear()
    await lookup(env, "web_search", {"query": "pizza near me"})
    assert search.redact_places(NETWORK, "pizza in Melbourne, Victoria") == "pizza in [place]"
