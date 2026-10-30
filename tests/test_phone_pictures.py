"""
Pictures from the phone chat (2.6.2): the model's create_image tool, section 3.1 of the contract.

* Part 1 is AppPool.stream with a scripted member (as tests/test_app_lookups.py): when the tool and its prompt line are
  offered, what the phone is sent when the model calls it, the one picture per answer, and the plain ask (IMAGE_ASK) that
  is required and, when the model talks instead, answered by the server's own call.
* Part 2 is IMAGE_ASK itself: what it matches, what it must not, and that it stays quick on any text.
* Part 3 is the coordinator: a fake engine on a phone lane that calls the tool, a real image service with a stand-in
  picture engine, the real routes. The request's ``features``, the job it queues, its refusals (the same code and words
  POST /v1/app/images gives), and the 2.6.1 pin: a request without the feature sends the engine exactly what it did.
"""

import asyncio
import json
import re
import time
from types import SimpleNamespace

import httpx
import pytest

from engine import app_pool as ap
from engine import coordinator, image_policy, search
from engine.contributor_networks import ContributorBook
from engine.image_pool import ImageService
from engine.landing import PUBLIC_URL
from tests.app_pool_fakes import DONE
from tests.test_app_lookups import (ANSWER, TODAY, TOOLS, WEB_TOOLS, Member_, Result, content, dlne, events, sse, text,
                                    tool_call)
from tests.test_image_endpoints import finished
from tests.test_image_pool import FakeMember, Judge
from tests import test_phone_lane_endpoint as lanes
from tests.test_phone_lane_endpoint import env, nothing_held, servers  # noqa: F401 - fixtures

# --------------------------------------------------------------------------------------------------------------
# What the contract says, written out once more: a change to any of it is a change to the contract
# --------------------------------------------------------------------------------------------------------------

CONTRACT_TOOL = {"type": "function", "function": {
    "name": "create_image",
    "description": "Create a new picture from a description. It is shown to the user below your reply about a minute and "
                   "a half later. Use it whenever the user asks for a picture, image, drawing, painting, illustration, "
                   "photo-like scene, wallpaper, logo, icon, poster or art of something.",
    "parameters": {"type": "object", "properties": {
        "prompt": {"type": "string", "description": "A detailed English description of the picture: subject, setting, "
                                                    "style, colours, lighting and composition. Under 1000 characters. "
                                                    "Never a real, named person."},
        "shape": {"type": "string", "enum": ["square", "portrait", "landscape"],
                  "description": "square, unless the user wants a phone wallpaper or a portrait (portrait), or a banner, "
                                 "desktop wallpaper or wide scene (landscape)."}},
        "required": ["prompt"]}}}
CONTRACT_LINE = (" You can create pictures with the create_image tool: when the user asks for a picture of something, call "
                 "it with a detailed description instead of describing a picture in words. The picture appears below your "
                 "reply about a minute and a half later, so reply in one short sentence. You cannot change or edit a photo "
                 "the user sent; offer to create a new picture from a description instead.")
MADE = ("The picture is being made and will appear below your reply in about {seconds} seconds. Tell the user in one short "
        "sentence. Do not describe it as if you could see it, and add no link or placeholder.")
REFUSED = "The picture was not made: {detail} Tell the user briefly and kindly, in your own words."
ONE_ONLY = "Only one picture per message."


def test_the_tool_is_defined_as_the_contract_defines_it():
    assert ap.IMAGE_TOOL == CONTRACT_TOOL and ap.IMAGE_TOOL_NAME == "create_image"
    assert json.dumps(ap.IMAGE_TOOL, sort_keys=True) == json.dumps(CONTRACT_TOOL, sort_keys=True)


def test_the_prompt_line_and_the_tool_results_are_worded_as_the_contract_words_them():
    assert ap.IMAGE_INSTRUCTIONS == CONTRACT_LINE
    assert ap.PICTURE_MADE_REPLY == MADE and ap.PICTURE_REFUSED_REPLY == REFUSED and ap.ONE_PICTURE_REPLY == ONE_ONLY


def test_the_prompt_says_nothing_new_without_the_tool_and_the_line_goes_last_with_it():
    plain = ap._system_prompt(False, "Monday")
    assert ap._system_prompt(False, "Monday", False, False) == plain and "create_image" not in plain
    with_line = ap._system_prompt(False, "Monday", False, True)
    assert with_line == plain + CONTRACT_LINE and ap.NO_SEARCH_NOTE in with_line and ap.SEARCH_INSTRUCTIONS not in with_line
    looking = ap._system_prompt(True, "Monday", True, True)
    assert looking == ap._system_prompt(True, "Monday", True) + CONTRACT_LINE
    assert ap.LOCATION_INSTRUCTIONS in looking and looking.index(ap.SEARCH_INSTRUCTIONS) < looking.index(CONTRACT_LINE)


@pytest.mark.parametrize("shape, size", [
    ("square", "768x768"), ("portrait", "768x1024"), ("landscape", "1024x768"),
    (None, "768x768"), ("wide", "768x768"), ("Landscape", "768x768"), ("1024x1024", "768x768"), ("", "768x768"),
    (5, "768x768"), (["landscape"], "768x768"), ({"a": 1}, "768x768"), (True, "768x768"),
])
def test_a_shape_is_a_size_and_anything_else_is_square(shape, size):
    assert image_policy.size_for_shape(shape) == size and size in image_policy.SIZES


# ======================================================================================
# Part 1: AppPool.stream
# ======================================================================================

class Maker:
    """The coordinator's side of create_image, as the stream reads it: a job queued, or a refusal."""

    def __init__(self, refusal=None):
        self.calls = []
        self.refusal = refusal

    async def __call__(self, arguments):
        self.calls.append(arguments)
        if self.refusal is not None:
            code, detail = self.refusal
            return ap.Picture({"event": "image_refused", "code": code, "detail": detail}, REFUSED.format(detail=detail))
        return ap.Picture({"event": "image_job", "id": f"job-{len(self.calls)}", "prompt": "a red fox", "size": "768x768",
                           "position": 1, "eta_seconds": 80}, MADE.format(seconds=80))


class Looker:
    """The lookup runner the pictures stand beside."""

    def __init__(self):
        self.calls = []

    async def __call__(self, name, arguments, first_n=1, client=None):
        self.calls.append((name, arguments, first_n))
        return Result(sources=[{"n": first_n, "title": "t", "url": "https://x.example/", "host": "x", "age": ""}],
                      context_text=f"[{first_n}] sunny")


async def run(member, body, tools=None, run_tool=None, local=True):
    """tests/test_app_lookups.py's ``run``, answered by a member on this machine (the engine, or the CPU worker beside
    it) unless ``local`` is False: a volunteer's, whose create_image calls are not trusted (2.6.2 review)."""
    pool = ap.AppPool()
    async with httpx.AsyncClient(transport=httpx.MockTransport(member.handler)) as client:
        members = [ap.Member("worker:m", 16.0, "small", True, 2, "http://member", local=local)]
        chunks = [c async for c in pool.stream(body, members, "small", client, tools=tools, run_tool=run_tool,
                                               today=TODAY)]
    return b"".join(chunks), pool


def pictured(maker=None, lookups=None, looker=None):
    """``(tools, run_tool, maker)`` as the coordinator builds them (:func:`app_pool.with_pictures`)."""
    maker = maker or Maker()
    tools, run_tool = ap.with_pictures(lookups, looker, maker)
    return tools, run_tool, maker


def ask(sentence):
    return {"messages": [{"role": "user", "content": sentence}]}


def shown(out):
    """What the phone is sent, in order: ("dlne", the event's name) and ("text", a piece of the reply)."""
    return [("dlne", e["dlne"]["event"]) if "dlne" in e else ("text", e["choices"][0]["delta"]["content"])
            for e in events(out)]


FOX = ("create_image", {"prompt": "A red fox in fresh snow at dawn, soft light, wide shot", "shape": "landscape"})
TALK = content("Sure! Here is a description of a fox in the snow: a red fox walks across ") + content("a field.") + DONE
SKETCH = "Hey, can I get a picture of a fox in the snow?"          # a plain ask (IMAGE_ASK)
ON_ITS_OWN = "I'm bored, surprise me with something wintry"        # not one: making a picture is the model's to decide


@pytest.mark.anyio
async def test_the_tool_and_its_line_are_offered_when_pictures_are():
    tools, run_tool, maker = pictured()
    member = Member_(ANSWER)
    out, pool = await run(member, ask("Tell me about foxes."), tools, run_tool)
    sent = member.requests[0]
    assert sent["tools"] == [ap.IMAGE_TOOL] and "tool_choice" not in sent
    system = sent["messages"][0]["content"]
    assert system.endswith(CONTRACT_LINE) and ap.NO_SEARCH_NOTE in system and ap.SEARCH_INSTRUCTIONS not in system
    assert text(out) == "It is sunny." and dlne(out) == [] and maker.calls == [], "the model did not ask: nothing is made"
    assert sent["max_tokens"] == ap.MAX_OUTPUT_TOKENS and pool._inflight == {}


@pytest.mark.anyio
async def test_lookups_and_pictures_are_offered_together():
    tools, run_tool, _ = pictured(lookups=WEB_TOOLS, looker=Looker())
    member = Member_(ANSWER)
    await run(member, ask("Tell me about foxes."), tools, run_tool)
    sent = member.requests[0]
    assert sent["tools"] == WEB_TOOLS + [ap.IMAGE_TOOL]
    system = sent["messages"][0]["content"]
    assert ap.SEARCH_INSTRUCTIONS in system and system.endswith(CONTRACT_LINE)


@pytest.mark.anyio
async def test_without_the_tool_the_member_is_sent_what_2_6_1_sent():
    member = Member_(ANSWER, ANSWER)
    await run(member, ask("Tell me about foxes."))
    assert "tools" not in member.requests[0]
    assert member.requests[0]["messages"][0]["content"] == ap._system_prompt(False, TODAY)
    await run(member, ask("Tell me about foxes."), TOOLS, Looker())
    assert member.requests[1]["tools"] == TOOLS
    assert member.requests[1]["messages"][0]["content"] == ap._system_prompt(True, TODAY)
    assert all("create_image" not in json.dumps(request) for request in member.requests)


@pytest.mark.anyio
async def test_a_model_that_calls_the_tool_makes_a_picture_and_the_phone_is_told_before_the_reply():
    tools, run_tool, maker = pictured()
    member = Member_(tool_call([("c1", *FOX)]), content("On its way!") + DONE)
    out, pool = await run(member, ask(ON_ITS_OWN), tools, run_tool)
    assert maker.calls == [FOX[1]], "the arguments as the model wrote them"
    assert dlne(out) == [{"event": "image_job", "id": "job-1", "prompt": "a red fox", "size": "768x768", "position": 1,
                          "eta_seconds": 80}]
    assert shown(out) == [("dlne", "image_job"), ("text", "On its way!")], "no 'searching' event: it is not a lookup"
    second = member.requests[1]
    assistant, tool = second["messages"][-2:]
    assert assistant["tool_calls"][0]["function"]["name"] == "create_image" and assistant["tool_calls"][0]["id"] == "c1"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == FOX[1]
    assert tool == {"role": "tool", "tool_call_id": "c1", "content": MADE.format(seconds=80)}
    assert pool._inflight == {}


@pytest.mark.anyio
async def test_a_refused_picture_is_an_image_refused_event_with_its_code_and_words():
    words = "I can't make images with nudity or sexual content. Describe something else and I'll gladly draw it."
    tools, run_tool, maker = pictured(Maker(("prompt_refused", words)))
    member = Member_(tool_call([("c1", *FOX)]), content("I couldn't draw that.") + DONE)
    out, _ = await run(member, ask(ON_ITS_OWN), tools, run_tool)
    assert dlne(out) == [{"event": "image_refused", "code": "prompt_refused", "detail": words}]
    assert member.requests[1]["messages"][-1]["content"] == REFUSED.format(detail=words)
    assert text(out) == "I couldn't draw that."


@pytest.mark.anyio
async def test_a_second_call_in_the_same_round_makes_no_job():
    tools, run_tool, maker = pictured()
    member = Member_(tool_call([("a", *FOX), ("b", "create_image", {"prompt": "another fox"})]), ANSWER)
    out, _ = await run(member, ask("draw foxes"), tools, run_tool)
    assert len(maker.calls) == 1 and [e["event"] for e in dlne(out)] == ["image_job"]
    tool_messages = [m for m in member.requests[1]["messages"] if m["role"] == "tool"]
    assert [(m["tool_call_id"], m["content"]) for m in tool_messages] == [("a", MADE.format(seconds=80)), ("b", ONE_ONLY)]


@pytest.mark.anyio
async def test_a_second_call_in_a_later_round_makes_no_job_either():
    tools, run_tool, maker = pictured()
    member = Member_(tool_call([("a", *FOX)]), tool_call([("b", "create_image", {"prompt": "one more"})]), ANSWER)
    out, _ = await run(member, ask(ON_ITS_OWN), tools, run_tool)
    assert len(maker.calls) == 1 and [e["event"] for e in dlne(out)] == ["image_job"]
    assert member.requests[2]["messages"][-1] == {"role": "tool", "tool_call_id": "b", "content": ONE_ONLY}
    assert "tools" not in member.requests[2], "the tools are withdrawn after MAX_TOOL_ROUNDS, so an answer always comes"


@pytest.mark.anyio
async def test_a_refused_first_call_still_counts_as_the_one_picture():
    tools, run_tool, maker = pictured(Maker(("prompt_too_long", "Keep the description under 1000 characters.")))
    member = Member_(tool_call([("a", *FOX)]), tool_call([("b", *FOX)]), ANSWER)
    out, _ = await run(member, ask("draw a fox"), tools, run_tool)
    assert len(maker.calls) == 1 and [e["event"] for e in dlne(out)] == ["image_refused"]
    assert member.requests[2]["messages"][-1]["content"] == ONE_ONLY


@pytest.mark.anyio
async def test_the_tools_stay_on_offer_in_the_round_after_a_picture():
    tools, run_tool, _ = pictured()
    member = Member_(tool_call([("a", *FOX)]), ANSWER)
    await run(member, ask(ON_ITS_OWN), tools, run_tool)
    assert member.requests[1]["tools"] == [ap.IMAGE_TOOL], "the model may still look something up"


@pytest.mark.anyio
async def test_a_lookup_and_a_picture_in_one_round_each_get_their_own_events_and_replies():
    looker = Looker()
    tools, run_tool, maker = pictured(lookups=WEB_TOOLS, looker=looker)
    member = Member_(tool_call([("w", "web_search", {"query": "snow in Oslo"}), ("p", *FOX)]), ANSWER)
    out, _ = await run(member, ask("how is the snow in Oslo, and show me a picture"), tools, run_tool)
    assert looker.calls == [("web_search", {"query": "snow in Oslo"}, 1)] and maker.calls == [FOX[1]]
    assert [e["event"] for e in dlne(out)] == ["searching", "sources", "image_job"]
    replies = [m["content"] for m in member.requests[1]["messages"] if m["role"] == "tool"]
    assert replies == ["[1] sunny", MADE.format(seconds=80)]


@pytest.mark.anyio
async def test_a_made_up_tool_when_only_pictures_are_offered_is_a_failed_lookup_not_a_crash():
    tools, run_tool, maker = pictured()
    member = Member_(tool_call([("x", "web_search", {"query": "foxes"})]), ANSWER)
    out, pool = await run(member, ask("hello"), tools, run_tool)
    assert dlne(out) == [{"event": "searching", "tool": "web_search", "query": "foxes"},
                         {"event": "search_unavailable", "reason": "Lookups are not available right now."}]
    assert maker.calls == [] and text(out) == "It is sunny." and pool._inflight == {}


@pytest.mark.anyio
async def test_a_create_image_call_when_pictures_were_never_offered_is_a_failed_lookup_as_before():
    seen = []

    async def lookups(name, arguments, first_n=1):
        seen.append(name)
        return Result(ok=False, reason="There is no lookup tool called “create_image”.")

    member = Member_(tool_call([("p", *FOX)]), ANSWER)
    out, _ = await run(member, ask("draw me a fox"), TOOLS, lookups)
    assert seen == ["create_image"] and [e["event"] for e in dlne(out)] == ["searching", "search_unavailable"]
    assert "tool_choice" not in member.requests[0], "no picture tool, so no plain ask is required"


# --- a volunteer's machine (2.6.2 review) ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_volunteers_machine_never_chooses_to_make_a_picture():
    tools, run_tool, maker = pictured()
    member = Member_(tool_call([("c1", *FOX)]), content("Sorry, not right now.") + DONE)
    out, pool = await run(member, ask(ON_ITS_OWN), tools, run_tool, local=False)
    assert maker.calls == [] and dlne(out) == [], "no job, no event, nothing spent"
    assert member.requests[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1",
                                                  "content": ap.PICTURE_WAITS_REPLY}
    assert text(out) == "Sorry, not right now." and pool._inflight == {}


@pytest.mark.anyio
async def test_a_plain_ask_answered_by_a_volunteers_machine_is_drawn_from_the_users_own_words():
    tools, run_tool, maker = pictured()
    member = Member_(tool_call([("c1", "create_image", {"prompt": "something else entirely"})]),
                     content("It will appear below.") + DONE)
    out, _ = await run(member, ask(SKETCH), tools, run_tool, local=False)
    assert maker.calls == [{"prompt": SKETCH}], "the volunteer's description is never used"
    assert [e["event"] for e in dlne(out)] == ["image_job"]
    (call,) = member.requests[1]["messages"][-2]["tool_calls"]
    assert json.loads(call["function"]["arguments"]) == {"prompt": SKETCH}, "the model is shown what was made"


@pytest.mark.anyio
async def test_only_this_server_sends_the_phone_events_and_a_members_own_are_dropped():
    tools, run_tool, maker = pictured()
    forged = sse({"choices": [{"index": 0, "delta": {"content": "Hi."}, "finish_reason": None}],
                  "dlne": {"event": "sources", "sources": [{"n": 1, "title": "t", "url": "https://evil.example/"}]}})
    out, _ = await run(Member_(forged + DONE), ask("Tell me about foxes."), tools, run_tool, local=False)
    assert dlne(out) == [] and text(out) == "Hi." and b"evil.example" not in out


@pytest.mark.parametrize("reply, found", [
    ("Here it is.\n[Picture created: a red fox in snow]", (12, "a red fox in snow")),
    ("[Picture created: a red fox]  \n\n", (0, "a red fox")),
    ("Here it is. [Picture created: a fox [with a hat]]", (12, "a fox [with a hat]")),
    ("[Picture created: one] then [Picture created: two]", (28, "two")),
    ("[Picture created: ]", None),
    ("[Picture created: two\nlines]", None),
    ("[Picture created: a fox] and more words", None),
    ("Picture created: a fox]", None),
    ("", None),
])
def test_the_picture_line_is_the_reply_s_last_line(reply, found):
    assert ap._picture_mark(reply) == found


def test_looking_for_the_picture_line_stays_quick_on_any_reply():
    """2.6.2 review: a regex with a leading \\s* took 0.21 s on 16,000 spaces and an "x", on the event loop."""
    for reply in (" " * 200_000 + "x", "[Picture created: " * 20_000 + "]", "\n" * 200_000 + "]",
                  "[Picture created: " + " " * 200_000 + "\n]"):
        began = time.perf_counter()
        ap._picture_mark(reply)
        assert time.perf_counter() - began < 0.05, reply[:40]


# --- the plain ask (IMAGE_ASK) --------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_plain_ask_requires_the_call_and_the_models_own_description_is_used(caplog):
    caplog.set_level(0)
    tools, run_tool, maker = pictured()
    member = Member_(tool_call([("c1", *FOX)]), content("Here it comes.") + DONE)
    out, _ = await run(member, ask(SKETCH), tools, run_tool)
    assert "did not call create_image" not in caplog.text, "the model did what it was asked: nothing to report"
    first = member.requests[0]
    assert first["tool_choice"] == "required" and first["tools"] == [ap.IMAGE_TOOL]
    assert first["max_tokens"] == ap.IMAGE_HELD_TOKENS, "room for a whole description, never the whole answer's"
    assert maker.calls == [FOX[1]] and text(out) == "Here it comes."
    assert "tool_choice" not in member.requests[1], "only the first pass is required"


@pytest.mark.anyio
async def test_a_plain_ask_whose_model_talks_instead_gets_the_servers_own_call_with_the_users_words(caplog):
    caplog.set_level(0)
    tools, run_tool, maker = pictured()
    member = Member_(TALK, content("It will appear below.") + DONE)
    out, pool = await run(member, ask(SKETCH), tools, run_tool)
    assert "did not call create_image for a plain ask" in caplog.text, "the operator can see how often the model needs it"
    assert SKETCH not in caplog.text and "fox" not in caplog.text, "and never the words"
    assert maker.calls == [{"prompt": SKETCH}], "no shape: square"
    assert "fox in the snow: a red fox walks" not in text(out), "what it said instead is dropped, never shown"
    assert [e["event"] for e in dlne(out)] == ["image_job"] and text(out) == "It will appear below."
    assistant, tool = member.requests[1]["messages"][-2:]
    (call,) = assistant["tool_calls"]
    assert call["id"] == "call_picture" and call["function"]["name"] == "create_image"
    assert json.loads(call["function"]["arguments"]) == {"prompt": SKETCH}
    assert tool == {"role": "tool", "tool_call_id": "call_picture", "content": MADE.format(seconds=80)}
    assert pool._inflight == {}


@pytest.mark.anyio
async def test_a_description_cut_off_by_the_held_passs_cap_becomes_the_servers_own_call():
    cut = (sse({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c", "type": "function", "function": {
        "name": "create_image", "arguments": "{\"prompt\": \"A red fox in"}}]}, "finish_reason": None}]})
        + sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}) + DONE)
    tools, run_tool, maker = pictured()
    member = Member_(cut, ANSWER)
    await run(member, ask(SKETCH), tools, run_tool)
    assert maker.calls == [{"prompt": SKETCH}]
    sent_back = member.requests[1]["messages"][-2]["tool_calls"][0]
    assert sent_back["id"] == "call_picture", "the server's call, not the half of one that the model made"


@pytest.mark.anyio
async def test_the_servers_own_call_takes_the_first_thousand_characters_of_the_users_words():
    tools, run_tool, maker = pictured()
    words = "Can you draw me a fox " + "in the snow " * 200
    member = Member_(TALK, ANSWER)
    await run(member, ask(words), tools, run_tool)
    assert maker.calls == [{"prompt": words[:image_policy.MAX_PROMPT_CHARS]}]


@pytest.mark.anyio
async def test_a_question_that_is_not_a_plain_ask_is_not_forced_and_its_talk_is_streamed_live():
    tools, run_tool, maker = pictured()
    member = Member_(TALK)
    out, _ = await run(member, ask("How do I draw a fox?"), tools, run_tool)
    assert "tool_choice" not in member.requests[0] and member.requests[0]["max_tokens"] == ap.MAX_OUTPUT_TOKENS
    assert text(out).startswith("Sure! Here is a description") and maker.calls == []


@pytest.mark.anyio
async def test_only_the_newest_user_text_is_asked_about():
    tools, run_tool, maker = pictured()
    member = Member_(ANSWER)
    body = {"messages": [{"role": "user", "content": "draw me a fox"}, {"role": "assistant", "content": "Done!"},
                         {"role": "user", "content": "thanks, it is lovely"}]}
    await run(member, body, tools, run_tool)
    assert "tool_choice" not in member.requests[0] and maker.calls == []


@pytest.mark.anyio
async def test_a_plain_ask_without_the_tool_is_not_forced():
    member = Member_(ANSWER)
    await run(member, ask(SKETCH))
    assert "tools" not in member.requests[0] and "tool_choice" not in member.requests[0]


@pytest.mark.anyio
async def test_a_fresh_question_is_still_a_lookup_first_when_pictures_are_on_offer():
    looker = Looker()
    tools, run_tool, maker = pictured(lookups=WEB_TOOLS, looker=looker)
    question = "Who won the most recent Super Bowl?"
    member = Member_(content("The Chiefs, I think.") + DONE, ANSWER)
    out, _ = await run(member, ask(question), tools, run_tool)
    first = member.requests[0]
    assert first["tool_choice"] == "required" and first["max_tokens"] == ap.HELD_PASS_TOKENS
    assert first["tools"] == WEB_TOOLS + [ap.IMAGE_TOOL]
    assert looker.calls == [("web_search", {"query": question}, 1)] and maker.calls == []
    assert "Chiefs" not in text(out)


@pytest.mark.anyio
async def test_with_pictures_alone_a_fresh_question_is_not_forced_to_call_anything():
    tools, run_tool, maker = pictured()
    member = Member_(ANSWER)
    out, _ = await run(member, ask("What's the weather today?"), tools, run_tool)
    assert "tool_choice" not in member.requests[0] and text(out) == "It is sunny." and maker.calls == []
    assert ap.SEARCH_INSTRUCTIONS not in member.requests[0]["messages"][0]["content"]


@pytest.mark.anyio
async def test_a_question_that_is_both_fresh_and_a_plain_ask_makes_the_picture_first():
    looker = Looker()
    tools, run_tool, maker = pictured(lookups=WEB_TOOLS, looker=looker)
    member = Member_(TALK, ANSWER)
    sentence = "Make me a picture of the weather today"
    await run(member, ask(sentence), tools, run_tool)
    assert member.requests[0]["tool_choice"] == "required" and member.requests[0]["max_tokens"] == ap.IMAGE_HELD_TOKENS
    assert maker.calls == [{"prompt": sentence}] and looker.calls == []


@pytest.mark.anyio
async def test_a_picture_the_model_makes_on_its_own_may_follow_talk_the_phone_already_saw():
    tools, run_tool, maker = pictured()
    member = Member_(content("Sure, one moment. ") + tool_call([("c1", *FOX)]), content("Done.") + DONE)
    out, _ = await run(member, ask("I'm bored, surprise me with something wintry"), tools, run_tool)
    assert shown(out) == [("text", "Sure, one moment. "), ("dlne", "image_job"), ("text", "Done.")]


def test_with_pictures_adds_the_tool_and_dispatches_by_name():
    looker, maker = Looker(), Maker()
    tools, run_tool = ap.with_pictures(TOOLS, looker, maker)
    assert tools == TOOLS + [ap.IMAGE_TOOL] and len(TOOLS) == 1, "a new list: the lookup list itself is not changed"

    async def calls():
        picture = await run_tool("create_image", {"prompt": "x"}, first_n=3, client="http")
        lookup = await run_tool("weather", {"location": "Oslo"}, first_n=3, client="http")
        return picture, lookup

    picture, lookup = asyncio.run(calls())
    assert isinstance(picture, ap.Picture) and maker.calls == [{"prompt": "x"}]
    assert looker.calls == [("weather", {"location": "Oslo"}, 3)] and lookup.context_text == "[3] sunny"


def test_with_pictures_without_lookups_gives_a_failed_lookup_for_any_other_name():
    tools, run_tool = ap.with_pictures(None, None, Maker())
    assert tools == [ap.IMAGE_TOOL]
    result = asyncio.run(run_tool("web_search", {"query": "x"}))
    assert result.ok is False and result.reason


# ======================================================================================
# Part 2: IMAGE_ASK
# ======================================================================================

PLAIN_ASKS = [
    "Hey, can I get a picture of a fox in the snow?", "can I get a picture of a fox in the snow",
    "Draw me a cat wearing a hat", "paint me a sunset over the ocean", "sketch a dragon", "Create an image of a castle",
    "generate a wallpaper of mountains", "Make me a logo for my coffee shop", "can I have a poster of space",
    "I'd like a drawing of my dog", "I’d like a drawing of my dog", "id like a poster of a fox",
    "Show me a picture of a red panda", "send me a pic of a puppy", "Give me some art of a forest",
    "create a phone wallpaper with neon cities", "make me a cool logo", "Can you draw a cat?",
    "could you please make me a poster about recycling", "Please generate an image of a mountain", "Make a picture",
    "I want a picture of a sunset", "I need a logo for my shop", "Show me pictures of foxes", "generate images of dogs",
    "that's cool. now draw me a fox", "OK, make a really cool fantasy poster of a dragon",
    "May I have an image of a robot?", "Could I see a picture of a fox in the snow",
    "Hey DLNE, can I get a picture of a fox", "so can you draw me a fox",
    "Can you generate me a photo-realistic cyberpunk-style picture of a city?", "Create a picture, please.",
    "I'd love you to paint me a landscape", "draw a picture of a cat", "make me an icon for my app", "Give me an image",
    "can u draw me a cat", "gimme a picture of a frog", "pls draw a fox",
    "I'm bored.\nCan you make me a poster of a whale?", "DRAW ME A FOX", "Whip up a logo for my bakery",
    "design a poster for a jazz night", "Get me a picture of the moon", "paint me a picture of a summer meadow",
    "Illustrate a dragon", "sketch a quick cat", "Draw an owl", "can you paint a sunset", "draw one cat",
]
NOT_ASKS = [
    "How do I draw a cat?", "Who painted the Mona Lisa?", "What does the Mona Lisa painting look like?",
    "I'd like to know about modern art", "Is there an app that can draw images?", "Can you explain how to make a logo?",
    "What is the best way to make a logo for a shop?", "Create a picture book story about a fox",
    "make an art project plan for my class", "give me an idea for a logo", "Describe this image",
    "make this image brighter", "Can you make the picture bluer?", "show me the picture", "Write a poem about art",
    "draw a cat in ASCII", "Make a logo in SVG", "make an icon component in react",
    "I need a refund for the picture I bought", "Can I get a refund for this picture?", "I like to draw a lot",
    "Do you know how to paint a fence?", "Tell me about the painting Starry Night", "I want to learn to draw",
    "Please explain what an icon is", "hello", "What's the weather today?", "Can you draw conclusions from this?",
    "make me a sandwich", "make a plan for the poster", "start a new art project",
    "can you make an icon that works for dark mode in my CSS?", "Can I get a picture book recommendation?",
    "I need an image optimization tip", "Paint me a picture in words of a rainy Rome",
    "Paint a picture with words of what summer feels like", "Create an image of my disk with dd",
    "Make a picture-perfect plan for my wedding", "Give me a logo idea for my bakery", "Tell me a joke",
    "Can you draw?", "What is an icon?", "How many pixels are in a 4K image?", "I'd like a refund",
    "Draw me like one of your French girls",
    # figures of speech
    "Draw a comparison between iPhone and Android", "draw me a conclusion", "Draw a line from A to B",
    "Sketch a plan for my week", "Illustrate a concept with an example", "paint me a picture of what summer feels like",
    "Draw a card", "Sketch an outline of the essay", "Can you illustrate a point about trade?",
    "Draw an inference from this data", "paint a picture of how the economy works", "sketch a schedule for my trip",
    "illustrate an example of recursion", "Draw a distinction between the two",
    # images that are not pictures
    "Create an image of my server", "Make an image of this VM", "Create an image of my disk with dd",
]


@pytest.mark.parametrize("sentence", PLAIN_ASKS)
def test_a_plain_ask_for_a_picture_is_one(sentence):
    assert ap.IMAGE_ASK.search(sentence), sentence


@pytest.mark.parametrize("sentence", NOT_ASKS)
def test_a_question_about_pictures_or_a_request_in_code_is_not(sentence):
    assert not ap.IMAGE_ASK.search(sentence), sentence


@pytest.mark.parametrize("sentence", [
    "a" * 17000, "make me " * 2000, "Draw me a " * 1700, "hey, " * 3400, "Can you please " * 1100, "hey. " * 3400,
    "hey\n" * 4000, "\n" * 17000, ". " * 8000, "!" * 17000, "a." + " " * 17000, "\n  " * 6000, "hey" + " " * 17000,
    "hey-" * 4000, "I want a " * 1900, "I'd like you to " * 1000, "make me a " + "big " * 4000 + "picture",
    ("a. " * 3 + "draw me ") * 1500, "make a picture " + "of " * 5000,
    ". ".join(["I'd like a very very very very very very cool thing"] * 300),
])
def test_it_stays_quick_on_any_text(sentence):
    """Text goes through it on the event loop, up to the whole of a long message: it must be linear, never quadratic."""
    began = time.perf_counter()
    ap.IMAGE_ASK.search(sentence)
    assert time.perf_counter() - began < 0.5


def test_it_is_a_pattern_the_stream_can_search():
    assert isinstance(ap.IMAGE_ASK, re.Pattern) and ap.IMAGE_ASK.flags & re.IGNORECASE


# ======================================================================================
# Part 3: the coordinator
# ======================================================================================

NET, OTHER_NET = lanes.REMOTE[0], lanes.OTHER[0]
SNOW_PROMPT = "A red fox in fresh snow at dawn, soft light, wide shot"
WORDS_OF_NO = "I can't make images with nudity or sexual content. Describe something else and I'll gladly draw it."
NEW_WORDING = ("Your free trial is used up. Pictures stay free on any network where a computer contributes to DLNE "
               "(now or in the last day).")
IMAGE_JOBS = ["image_jobs"]


@pytest.fixture
async def pictures(env, monkeypatch, tmp_path):
    """The phone-lane environment (a fake engine on a phone lane, a CPU member, a registry) with pictures on top: a real
    image service over a stand-in picture engine and model, a trial of 5 chats, 3 pictures an hour, nobody a contributor."""
    env.limits.update({"image_gen_enabled": True, "image_per_hour": 3, "image_queue_max": 2, "image_result_ttl_seconds": 600,
                       "starter_quota_inferences": 5, "enforce_contrib_clause": True})
    member, judge = FakeMember(), Judge()
    service = ImageService(member, judge, limits=lambda: coordinator.get_worker_limits(), housekeeping=0.05)
    monkeypatch.setattr(coordinator, "image_service", service)

    async def vision(_names):
        return True

    monkeypatch.setattr(coordinator, "_engine_vision", vision)
    monkeypatch.setattr(coordinator, "_image_reason_seen", None)
    quota = coordinator.StarterQuotaManager(str(tmp_path / "quota-pictures.json"), default_quota=5)
    monkeypatch.setattr(coordinator, "starter_quota_mgr", quota)
    contributors = ContributorBook()
    monkeypatch.setattr(coordinator, "contributor_networks", contributors)
    yield SimpleNamespace(env=env, member=member, judge=judge, service=service, quota=quota, limits=env.limits,
                          contributors=contributors, engine=env.engine, cpu=env.cpu)
    await service.shutdown()


def phone_asks(sentence, features=IMAGE_JOBS, **extra):
    body = lanes.phone_body(sentence, **extra)
    if features is not None:
        body["features"] = features
    return body


async def chat(sentence, features=IMAGE_JOBS, ip=lanes.REMOTE, **extra):
    async with lanes.client(ip) as c:
        resp = await c.post("/v1/app/chat/completions", json=phone_asks(sentence, features, **extra))
    assert resp.status_code == 200, resp.text
    return resp.content


def calls_create(arguments, call_id="c1"):
    return tool_call([(call_id, "create_image", arguments)])


def reply(words):
    return content(words) + DONE


def tools_of(request):
    return [tool["function"]["name"] for tool in request.get("tools") or ()]


def picture_event(out):
    jobs = [e for e in dlne(out) if e["event"] == "image_job"]
    assert len(jobs) == 1, dlne(out)
    return jobs[0]


def picture_free(out):
    """No picture event of any kind in the stream."""
    return not [e for e in dlne(out) if e["event"] in ("image_job", "image_refused")]


# --- the feature and what it needs --------------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("features, offered", [
    (["image_jobs"], True), (["image_jobs", "future_thing"], True), (["future_thing", "image_jobs"], True),
    (["image_jobs", 5, None, ["image_jobs"], {"a": 1}], True),
    (["future_thing"], False), ([], False), (None, False), ("image_jobs", False), ({"image_jobs": True}, False),
    ([["image_jobs"]], False), ([1, None, True], False), (["IMAGE_JOBS"], False), (["image_jobs "], False), (5, False),
])
async def test_the_tool_is_offered_only_to_a_client_that_says_it_shows_pictures(pictures, features, offered):
    out = await chat("Tell me about foxes.", features)
    (sent,) = pictures.engine.chats
    assert ("create_image" in tools_of(sent)) is offered
    assert (CONTRACT_LINE in lanes.system_of(sent)) is offered
    assert text(out), "an unknown value or a type that is not a list is ignored, never an error"
    assert pictures.service.status_block()["queue"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("why", ["off", "not installed", "no vision", "swapping"])
async def test_the_tool_is_offered_only_while_pictures_can_be_made(pictures, monkeypatch, why):
    if why == "off":
        pictures.limits["image_gen_enabled"] = False
    elif why == "not installed":
        pictures.member.reason = "the sd build is not installed"
    elif why == "no vision":
        async def no_vision(_names):
            return False

        monkeypatch.setattr(coordinator, "_engine_vision", no_vision)
    else:
        monkeypatch.setattr(coordinator, "is_swapping_models", True)        # the CPU pool answers while the engine swaps
    out = await chat("Tell me about foxes.")
    sent = pictures.cpu.payloads[0] if why == "swapping" else pictures.engine.chats[0]
    assert "create_image" not in tools_of(sent) and CONTRACT_LINE not in lanes.system_of(sent) and text(out)


@pytest.mark.anyio
async def test_lookups_and_the_picture_tool_are_offered_together_and_the_lookups_are_untouched(pictures):
    await chat("Tell me about foxes.")
    (sent,) = pictures.engine.chats
    assert sent["tools"] == search.TOOLS + [ap.IMAGE_TOOL]
    system = lanes.system_of(sent)
    assert ap.SEARCH_INSTRUCTIONS in system and CONTRACT_LINE in system


@pytest.mark.anyio
async def test_with_search_off_the_picture_tool_stands_alone_and_there_is_no_search_prompt(pictures, monkeypatch):
    monkeypatch.setattr(coordinator, "_app_lookup_tools", lambda network, location=None: (None, None))
    await chat("What's the weather today?")
    (sent,) = pictures.engine.chats
    assert sent["tools"] == [ap.IMAGE_TOOL] and "tool_choice" not in sent
    assert ap.SEARCH_INSTRUCTIONS not in lanes.system_of(sent) and ap.NO_SEARCH_NOTE in lanes.system_of(sent)


# --- the 2.6.1 pin ------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("features", [None, [], ["future_thing"], "image_jobs", {"image_jobs": True}, [None, 5]])
async def test_a_request_without_the_feature_sends_the_engine_exactly_what_2_6_1_sent(pictures, monkeypatch, features):
    asked = []

    async def never(*args, **kwargs):
        asked.append(1)
        return True, None

    monkeypatch.setattr(coordinator, "_image_available", never)
    await chat("Tell me about foxes.", features)
    (sent,) = pictures.engine.chats
    assert asked == [], "pictures are not even asked about"
    assert set(sent) == {"temperature", "top_p", "top_k", "presence_penalty", "chat_template_kwargs", "stream", "max_tokens",
                         "messages", "tools", "id_slot", "n_cache_reuse"}, "the keys of a 2.6.1 lane request, no more"
    assert sent["tools"] == search.TOOLS, "the lookups, as they were"
    assert sent["max_tokens"] == ap.MAX_OUTPUT_TOKENS and sent["stream"] is True and sent["n_cache_reuse"] == 0
    assert sent["id_slot"] in (2, 3)
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    assert sent["messages"][1]["content"] == "Tell me about foxes."
    system = lanes.system_of(sent)
    assert system.startswith(ap._system_prompt(True, None)) and system.endswith(lanes.chat_privacy.salt_line(lanes.CHAT_A))
    assert system == ap._system_prompt(True, None) + " " + lanes.chat_privacy.salt_line(lanes.CHAT_A)
    assert "picture" not in system.lower() and "create_image" not in json.dumps(sent)


@pytest.mark.anyio
async def test_a_question_about_now_without_the_feature_is_required_and_held_as_it_was_in_2_6_1(pictures, monkeypatch):
    looked = []

    async def lookup(name, arguments, *, client, network_id, first_n=1, location=None):
        looked.append((name, arguments, network_id))
        return Result(sources=[{"n": first_n, "title": "t", "url": "https://x.example/", "host": "x", "age": ""}],
                      context_text="[1] sunny")

    monkeypatch.setattr(search, "run_tool", lookup)                    # the lookups themselves are not what is tested
    question = "What's the weather in Philadelphia?"
    pictures.engine.script = [[content("It is sunny, I think.") + DONE], [reply("It is sunny [1].")]]
    out = await chat(question, None)
    first, second = pictures.engine.chats
    assert first["tools"] == search.TOOLS and first["tool_choice"] == "required"
    assert first["max_tokens"] == ap.HELD_PASS_TOKENS
    assert looked == [("web_search", {"query": question}, NET)] and "sunny, I think" not in text(out)
    assert "tool_choice" not in second and second["tools"] == search.TOOLS
    assert "create_image" not in json.dumps([first, second]) and picture_free(out)


@pytest.mark.anyio
async def test_a_request_without_the_feature_is_the_same_whatever_else_it_says_about_features(pictures):
    await chat("Tell me about foxes.", None)
    await chat("Tell me about foxes.", ["future_thing"])
    await chat("Tell me about foxes.", [])
    first, *others = pictures.engine.chats
    for other in others:
        assert {k: v for k, v in other.items() if k != "id_slot"} == {k: v for k, v in first.items() if k != "id_slot"}


@pytest.mark.anyio
async def test_the_pin_also_holds_on_the_cpu_pool(pictures):
    pictures.limits["engine_phone_slots"] = 0
    pictures.env.manager.phone_slots = 0
    await chat("Tell me about foxes.", None)
    (sent,) = pictures.cpu.payloads
    assert sent["tools"] == search.TOOLS and "create_image" not in json.dumps(sent)
    assert sent["messages"][0]["content"] == ap._system_prompt(True, None)


# --- a chat that makes a picture ----------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_chat_that_calls_the_tool_queues_a_job_owned_by_its_network(pictures):
    pictures.engine.script = [[calls_create({"prompt": SNOW_PROMPT + " <lora:x:1>", "shape": "landscape"})],
                              [reply("It will show up below in a minute or so.")]]
    out = await chat(ON_ITS_OWN)
    event = picture_event(out)
    assert set(event) == {"event", "id", "prompt", "size", "position", "eta_seconds"}
    assert event["prompt"] == SNOW_PROMPT, "what the job runs: cleaned, the tag block gone"
    assert event["size"] == "1024x768" and event["position"] == 1 and event["eta_seconds"] == 111
    assert shown(out) == [("dlne", "image_job"), ("text", "It will show up below in a minute or so.")]

    job = pictures.service.get(NET, "app", event["id"])
    assert job is not None and job.owner == NET and job.kind == "app" and job.size == "1024x768"
    assert pictures.service.get(OTHER_NET, "app", event["id"]) is None, "another network cannot see it"
    assert pictures.service.get(NET, "ide", event["id"]) is None, "and it is not a coding-side job"
    assert pictures.quota.summary(5)["chats_used"] == 1, "a stranger's picture spends a free-trial chat, as in 2.6.1"

    second = pictures.engine.chats[1]
    assistant, tool = second["messages"][-2:]
    assert assistant["tool_calls"][0]["function"]["name"] == "create_image"
    assert tool["role"] == "tool" and tool["content"] == MADE.format(seconds=111)
    assert nothing_held(pictures.env), "the lane, the gate and the network's slot are all free"


@pytest.mark.anyio
async def test_the_phone_polls_the_job_like_one_made_from_the_dialog_and_gets_the_picture(pictures):
    pictures.engine.script = [[calls_create({"prompt": SNOW_PROMPT})], [reply("Coming.")]]
    out = await chat(ON_ITS_OWN)
    job_id = picture_event(out)["id"]
    async with lanes.client(lanes.REMOTE) as c, lanes.client(lanes.OTHER) as other:
        done = await finished(c, "/v1/app/images", job_id)
        assert (await other.get(f"/v1/app/images/{job_id}")).status_code == 404
    assert done["status"] == "completed" and done["image"]["width"] == 768
    assert pictures.member.calls[0]["prompt"] == SNOW_PROMPT and pictures.member.calls[0]["width"] == 768
    assert pictures.judge.calls[0][0] == "app", "its checks take a phone lane, as the dialog's do"


@pytest.mark.anyio
@pytest.mark.parametrize("shape, size", [("square", "768x768"), ("portrait", "768x1024"), ("landscape", "1024x768"),
                                         (None, "768x768"), ("wide", "768x768"), (5, "768x768"), ("1024x1024", "768x768")])
async def test_the_shape_decides_the_size_and_a_wrong_word_is_square(pictures, shape, size):
    arguments = {"prompt": SNOW_PROMPT} if shape is None else {"prompt": SNOW_PROMPT, "shape": shape}
    pictures.engine.script = [[calls_create(arguments)], [reply("ok")]]
    event = picture_event(await chat(ON_ITS_OWN))
    assert event["size"] == size
    async with lanes.client(lanes.REMOTE) as c:
        await finished(c, "/v1/app/images", event["id"])
    width, height = (int(n) for n in size.split("x"))
    assert (pictures.member.calls[0]["width"], pictures.member.calls[0]["height"]) == (width, height)


@pytest.mark.anyio
async def test_the_dialog_and_the_tool_run_the_one_shared_function(pictures, monkeypatch):
    seen, real = [], coordinator._create_image_job

    def spy(request, kind, raw, size):
        seen.append((kind, raw, size))
        return real(request, kind, raw, size)

    monkeypatch.setattr(coordinator, "_create_image_job", spy)
    async with lanes.client(lanes.REMOTE) as c:
        assert (await c.post("/v1/app/images", json={"prompt": "from the dialog", "size": "768x1024"})).status_code == 202
        assert (await c.post("/v1/images", json={"prompt": "from the IDE"})).status_code == 202
    pictures.engine.script = [[calls_create({"prompt": "from the chat", "shape": "portrait"})], [reply("ok")]]
    await chat(ON_ITS_OWN)
    assert seen == [("app", "from the dialog", "768x1024"), ("ide", "from the IDE", None),
                    ("app", "from the chat", "768x1024")]


@pytest.mark.anyio
async def test_the_description_never_reaches_a_log(pictures, caplog):
    caplog.set_level(0)
    secret = "quokka riding a unicycle through a snowstorm"
    pictures.engine.script = [[calls_create({"prompt": f"A {secret}"})], [reply("ok")]]
    out = await chat(ON_ITS_OWN)
    async with lanes.client(lanes.REMOTE) as c:
        await finished(c, "/v1/app/images", picture_event(out)["id"])
    assert secret not in caplog.text and "quokka" not in caplog.text and ON_ITS_OWN not in caplog.text


# --- one picture per answer ---------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_second_call_in_the_same_answer_makes_no_job(pictures):
    pictures.engine.script = [[tool_call([("a", "create_image", {"prompt": "one fox"}),
                                          ("b", "create_image", {"prompt": "two foxes"})])], [reply("ok")]]
    out = await chat(ON_ITS_OWN)
    picture_event(out)
    assert len(pictures.service._jobs) == 1 and pictures.quota.summary(5)["chats_used"] == 1
    tool_messages = [m for m in pictures.engine.chats[1]["messages"] if m["role"] == "tool"]
    assert [m["content"] for m in tool_messages] == [MADE.format(seconds=80), ONE_ONLY]


@pytest.mark.anyio
async def test_a_second_call_in_a_second_round_makes_no_job_either(pictures):
    pictures.engine.script = [[calls_create({"prompt": "one fox"}, "a")], [calls_create({"prompt": "two foxes"}, "b")],
                              [reply("ok")]]
    out = await chat(ON_ITS_OWN)
    picture_event(out)
    assert len(pictures.service._jobs) == 1 and pictures.quota.summary(5)["chats_used"] == 1
    assert pictures.engine.chats[2]["messages"][-1]["content"] == ONE_ONLY


# --- refusals: the same code and words the endpoint gives ----------------------------------------------------------------

def cut_off_call():
    """A create_image call whose arguments never became JSON (the model's turn was cut short)."""
    return (sse({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {
        "name": "create_image", "arguments": "{\"prompt\": \"A red fo"}}]}, "finish_reason": None}]})
        + sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}) + DONE)


def refuse_by(why, p):
    """Sets the stage for a refusal. Returns what the model will call the tool with, and the status, code and detail
    that POST /v1/app/images gives for it."""
    if why == "the term list":
        return {"prompt": "a nude woman"}, 400, "prompt_refused", image_policy.MESSAGES["sexual"]
    if why == "no description":
        return {"shape": "square"}, 400, "prompt_empty", "Describe the picture you'd like."
    if why == "a description that is not text":
        return {"prompt": 5}, 400, "prompt_empty", "Describe the picture you'd like."
    if why == "only tags":
        return {"prompt": "<lora:only:1>"}, 400, "prompt_empty", "Describe the picture you'd like."
    if why == "too long":
        return {"prompt": "x" * 1001}, 400, "prompt_too_long", "Keep the description under 1000 characters."
    if why == "far too long":
        return {"prompt": "x" * 5000}, 400, "prompt_too_long", "Keep the description under 1000 characters."
    if why == "the trial is used up":
        p.limits["starter_quota_inferences"] = 1
        p.quota.check_and_consume(NET, limit=1)
        return {"prompt": SNOW_PROMPT}, 429, "image_quota", NEW_WORDING
    if why == "the hour is used up":
        p.limits["image_per_hour"] = 1
        p.service._hourly.spend(NET, 1, p.service._clock())
        return ({"prompt": SNOW_PROMPT}, 429, "image_quota",
                "You've reached the limit of 1 pictures an hour. Please try again a little later.")
    if why == "the queue is full":
        p.limits["image_queue_max"] = 0
        return ({"prompt": SNOW_PROMPT}, 429, "image_busy",
                "Lots of people are making pictures right now. Please try again in a moment.")
    raise AssertionError(why)


REFUSALS = ["the term list", "no description", "a description that is not text", "only tags", "too long", "far too long",
            "the trial is used up", "the hour is used up", "the queue is full"]


@pytest.mark.anyio
@pytest.mark.parametrize("why", REFUSALS)
async def test_a_picture_that_is_not_made_is_an_image_refused_event_with_the_endpoints_code_and_words(pictures, why):
    arguments, status, code, detail = refuse_by(why, pictures)
    async with lanes.client(lanes.REMOTE) as c:
        endpoint = await c.post("/v1/app/images", json={"prompt": arguments.get("prompt")})
    assert (endpoint.status_code, endpoint.json()["code"], endpoint.json()["detail"]) == (status, code, detail)
    used, jobs = pictures.quota.summary(5)["chats_used"], len(pictures.service._jobs)

    pictures.engine.script = [[calls_create(arguments)], [reply("I could not make that one.")]]
    out = await chat(ON_ITS_OWN)
    assert dlne(out) == [{"event": "image_refused", "code": code, "detail": detail}]
    assert pictures.engine.chats[1]["messages"][-1]["content"] == REFUSED.format(detail=detail)
    assert text(out) == "I could not make that one."
    assert len(pictures.service._jobs) == jobs, "nothing made"
    assert pictures.quota.summary(5)["chats_used"] == used, "nothing spent"
    assert nothing_held(pictures.env)


@pytest.mark.anyio
async def test_a_call_whose_arguments_did_not_parse_is_the_endpoints_empty_description(pictures):
    pictures.engine.script = [[cut_off_call()], [reply("ok")]]
    out = await chat(ON_ITS_OWN)
    assert dlne(out) == [{"event": "image_refused", "code": "prompt_empty", "detail": "Describe the picture you'd like."}]


@pytest.mark.anyio
async def test_pictures_that_went_away_between_the_offer_and_the_call_are_refused_as_unavailable(pictures, monkeypatch):
    pictures.engine.script = [[calls_create({"prompt": SNOW_PROMPT})], [reply("ok")]]
    real, calls = coordinator._image_available, []

    async def flips(vision=None):
        calls.append(1)
        if len(calls) > 1:
            pictures.member.reason = "the sd build is not installed"
        return await real(vision)

    monkeypatch.setattr(coordinator, "_image_available", flips)
    out = await chat(ON_ITS_OWN)
    assert dlne(out) == [{"event": "image_refused", "code": "image_generation_unavailable",
                          "detail": coordinator.IMAGE_UNAVAILABLE}]
    assert calls[:2] == [1, 1] and pictures.quota.summary(5)["chats_used"] == 0 and len(pictures.service._jobs) == 0


@pytest.mark.anyio
async def test_an_engine_restart_between_the_offer_and_the_call_says_so(pictures, monkeypatch):
    pictures.engine.script = [[calls_create({"prompt": SNOW_PROMPT})], [reply("ok")]]
    real, calls = coordinator._image_available, []

    async def flips(vision=None):
        calls.append(1)
        if len(calls) > 1:
            monkeypatch.setattr(coordinator, "is_swapping_models", True)
        return await real(vision)

    monkeypatch.setattr(coordinator, "_image_available", flips)
    out = await chat(ON_ITS_OWN)
    assert dlne(out) == [{"event": "image_refused", "code": "image_generation_unavailable",
                          "detail": "DLNE is restarting, try again in a moment."}]


@pytest.mark.anyio
async def test_a_bug_while_queueing_is_a_refusal_not_a_broken_answer(pictures, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(coordinator, "_create_image_job", boom)
    pictures.engine.script = [[calls_create({"prompt": SNOW_PROMPT})], [reply("Sorry about that.")]]
    out = await chat(ON_ITS_OWN)
    assert dlne(out) == [{"event": "image_refused", "code": "generation_failed",
                          "detail": "Something went wrong making the picture. Please try again."}]
    assert text(out) == "Sorry about that." and out.endswith(b"data: [DONE]\n\n") and nothing_held(pictures.env)


# --- the plain ask ------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_plain_ask_whose_model_talks_instead_still_gets_its_picture(pictures):
    pictures.engine.script = [[TALK], [reply("It will appear below.")]]
    out = await chat(SKETCH)
    first, second = pictures.engine.chats
    assert first["tool_choice"] == "required" and first["max_tokens"] == ap.IMAGE_HELD_TOKENS
    assert "create_image" in tools_of(first)
    event = picture_event(out)
    assert event["prompt"] == SKETCH and event["size"] == "768x768"
    job = pictures.service.get(NET, "app", event["id"])
    assert job is not None and job.owner == NET and job.kind == "app"
    assert "fox in the snow: a red fox walks" not in text(out) and text(out) == "It will appear below."
    assert [e["dlne"]["event"] for e in events(out) if "dlne" in e] == ["image_job"], "the event comes before the reply"
    assert events(out)[0]["dlne"]["event"] == "image_job"
    assert second["messages"][-2]["tool_calls"][0]["id"] == "call_picture"
    assert second["messages"][-1]["content"] == MADE.format(seconds=80) and "tool_choice" not in second


@pytest.mark.anyio
async def test_a_plain_ask_whose_model_calls_the_tool_uses_the_models_description(pictures):
    pictures.engine.script = [[calls_create({"prompt": SNOW_PROMPT, "shape": "portrait"})], [reply("There you go.")]]
    out = await chat(SKETCH)
    event = picture_event(out)
    assert event["prompt"] == SNOW_PROMPT and event["size"] == "768x1024"
    assert len(pictures.engine.chats) == 2 and text(out) == "There you go."


@pytest.mark.anyio
async def test_a_plain_ask_that_is_refused_by_the_term_list_says_so_and_costs_nothing(pictures):
    pictures.engine.script = [[TALK], [reply("I can't draw that one.")]]
    out = await chat("Hey, can I get a picture of a nude woman?")
    assert dlne(out) == [{"event": "image_refused", "code": "prompt_refused", "detail": image_policy.MESSAGES["sexual"]}]
    assert pictures.quota.summary(5)["chats_used"] == 0 and len(pictures.service._jobs) == 0


@pytest.mark.anyio
async def test_a_question_about_pictures_is_not_forced(pictures):
    pictures.engine.script = [[reply("Use a pencil and start with a circle.")]]
    out = await chat("How do I draw a fox?")
    (sent,) = pictures.engine.chats
    assert "tool_choice" not in sent and "create_image" in tools_of(sent)
    assert dlne(out) == [] and len(pictures.service._jobs) == 0


# --- paused (IMAGE_GEN_ENABLED=paused, 2026-10-30) ----------------------------------------------------------------------

PAUSED_DETAIL = ("Pictures are paused for now: DLNE's graphics card is busy running the smarter coding model. They will "
                 "be back. More: " + PUBLIC_URL + "/pictures")


@pytest.fixture
def paused(pictures):
    pictures.limits.update(image_gen_enabled=False, image_gen_paused=True)
    return pictures


def nothing_made(p):
    return len(p.service._jobs) == 0 and p.member.calls == [] and p.quota.summary(5)["chats_used"] == 0


@pytest.mark.anyio
async def test_while_paused_the_tool_and_its_line_are_still_offered(paused):
    await chat("Tell me about foxes.")
    (sent,) = paused.engine.chats
    assert sent["tools"] == search.TOOLS + [ap.IMAGE_TOOL] and CONTRACT_LINE in lanes.system_of(sent)


@pytest.mark.anyio
@pytest.mark.parametrize("features", [None, ["future_thing"]])
async def test_while_paused_a_request_without_the_feature_is_still_answered_as_in_2_6_1(paused, features):
    await chat("Tell me about foxes.", features)
    (sent,) = paused.engine.chats
    assert "create_image" not in tools_of(sent) and CONTRACT_LINE not in lanes.system_of(sent)


@pytest.mark.anyio
async def test_while_paused_a_call_is_told_the_pause_with_the_page_and_nothing_is_made(paused):
    paused.engine.script = [[calls_create({"prompt": SNOW_PROMPT})], [reply("Pictures are paused right now, sorry!")]]
    out = await chat(ON_ITS_OWN)
    assert dlne(out) == [{"event": "image_refused", "code": "image_generation_paused", "detail": PAUSED_DETAIL}]
    assert paused.engine.chats[1]["messages"][-1]["content"] == REFUSED.format(detail=PAUSED_DETAIL)
    assert text(out) == "Pictures are paused right now, sorry!"
    assert nothing_made(paused) and nothing_held(paused.env)


@pytest.mark.anyio
async def test_while_paused_a_plain_ask_whose_model_talks_instead_is_told_the_pause_too(paused):
    paused.engine.script = [[TALK], [reply("Pictures are paused for now.")]]
    out = await chat(SKETCH)
    assert paused.engine.chats[0]["tool_choice"] == "required"
    assert dlne(out) == [{"event": "image_refused", "code": "image_generation_paused", "detail": PAUSED_DETAIL}]
    assert paused.engine.chats[1]["messages"][-1]["content"] == REFUSED.format(detail=PAUSED_DETAIL)
    assert nothing_made(paused)


@pytest.mark.anyio
async def test_while_paused_the_cpu_pool_is_offered_the_tool_and_told_the_same(paused):
    paused.limits["engine_phone_slots"] = 0
    paused.env.manager.phone_slots = 0
    paused.cpu.script = [[calls_create({"prompt": SNOW_PROMPT})], [reply("They're paused.")]]
    out = await chat(SKETCH)
    assert "create_image" in tools_of(paused.cpu.payloads[0])
    assert dlne(out) == [{"event": "image_refused", "code": "image_generation_paused", "detail": PAUSED_DETAIL}]
    assert nothing_made(paused)


# --- who pays ------------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_contributors_network_makes_pictures_from_the_chat_for_free_even_with_the_trial_used_up(pictures):
    pictures.limits["starter_quota_inferences"] = 1
    pictures.quota.check_and_consume(NET, limit=1)
    pictures.contributors.note(NET)
    pictures.engine.script = [[calls_create({"prompt": SNOW_PROMPT})], [reply("ok")]]
    out = await chat(ON_ITS_OWN)
    picture_event(out)
    assert pictures.quota.peek(NET, limit=1) == (False, 1, 0), "nothing spent, nothing given back"


@pytest.mark.anyio
async def test_a_network_that_is_nobodys_contributor_is_charged_one_chat_and_told_the_new_words(pictures):
    pictures.limits["starter_quota_inferences"] = 1
    pictures.engine.script = [[calls_create({"prompt": SNOW_PROMPT})], [reply("ok")], [calls_create({"prompt": SNOW_PROMPT})],
                              [reply("ok")]]
    picture_event(await chat(ON_ITS_OWN))
    out = await chat(ON_ITS_OWN)
    assert dlne(out) == [{"event": "image_refused", "code": "image_quota", "detail": NEW_WORDING}]
    assert pictures.engine.chats[3]["messages"][-1]["content"] == REFUSED.format(detail=NEW_WORDING)


@pytest.mark.anyio
async def test_a_local_client_is_never_charged_for_a_picture_from_the_chat(pictures):
    pictures.engine.script = [[calls_create({"prompt": SNOW_PROMPT})], [reply("ok")]]
    out = await chat(ON_ITS_OWN, ip=("127.0.0.1", 5000))
    picture_event(out)
    assert pictures.quota.summary(5)["chats_used"] == 0 and len(pictures.contributors) == 0


# --- the CPU pool -------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_volunteers_cpu_member_gets_a_plain_ask_made_of_the_users_words_and_the_job_is_the_requests_networks(
        pictures):
    """The CPU pool's members can be volunteers' machines (2.6.2 review): one never chooses what a picture shows."""
    pictures.limits["engine_phone_slots"] = 0
    pictures.env.manager.phone_slots = 0
    pictures.cpu.script = [[calls_create({"prompt": SNOW_PROMPT, "shape": "square"})], [reply("It is on its way.")]]
    out = await chat(SKETCH)
    sent = pictures.cpu.payloads[0]
    assert "create_image" in tools_of(sent) and lanes.system_of(sent).endswith(CONTRACT_LINE) and "id_slot" not in sent
    event = picture_event(out)
    job = pictures.service.get(NET, "app", event["id"])
    assert job is not None and job.owner == NET and job.kind == "app"
    (call,) = pictures.cpu.payloads[1]["messages"][-2]["tool_calls"]
    assert json.loads(call["function"]["arguments"]) == {"prompt": SKETCH}, "the user's words, never the member's"
    assert pictures.engine.chats == [] and text(out) == "It is on its way."


@pytest.mark.anyio
async def test_a_volunteers_cpu_member_cannot_make_a_picture_nobody_plainly_asked_for(pictures):
    pictures.limits["engine_phone_slots"] = 0
    pictures.env.manager.phone_slots = 0
    pictures.cpu.script = [[calls_create({"prompt": SNOW_PROMPT})], [reply("Ask me for one directly.")]]
    out = await chat(ON_ITS_OWN)
    assert dlne(out) == [] and text(out) == "Ask me for one directly."
    assert pictures.cpu.payloads[1]["messages"][-1]["content"] == ap.PICTURE_WAITS_REPLY
    assert len(pictures.service._jobs) == 0 and pictures.quota.summary(5)["chats_used"] == 0, "nothing made or spent"


@pytest.mark.anyio
async def test_when_the_engine_cannot_answer_the_cpu_pool_is_offered_the_tool_and_its_line_too(pictures):
    pictures.engine.fails = True
    pictures.cpu.script = [[calls_create({"prompt": SNOW_PROMPT})], [reply("It is on its way.")]]
    out = await chat(SKETCH)
    sent = pictures.cpu.payloads[0]
    assert "create_image" in tools_of(sent) and lanes.system_of(sent).endswith(CONTRACT_LINE)
    assert "internal reference" not in lanes.system_of(sent), "and still without the chat's salt line"
    picture_event(out)
    assert nothing_held(pictures.env)


# --- a picture in the history (added at the 2.6.2 merge) ---------------------------------------------------------------
# Tried on the real phone model (Qwen3.6 on TheWall, 2026-10-28): shown "[Picture created: …]" at the end of its own
# earlier reply, it typed the marker out when asked "make it more blue, like twilight" and made no picture; shown its
# earlier create_image call instead, it called the tool again with the description updated.

FOX_DESCRIPTION = "A red fox standing in fresh snow at dawn, soft golden light, wide shot"


def test_a_reply_that_made_a_picture_is_told_as_the_call_it_was():
    history = [{"role": "user", "content": "Hey, can I get a picture of a fox in the snow?"},
               {"role": "assistant", "content": "Here comes your fox!\n\n[Picture created: " + FOX_DESCRIPTION + "]"},
               {"role": "user", "content": "make it more blue"}]
    told = ap._pictures_as_calls(history)
    assert [m["role"] for m in told] == ["user", "assistant", "tool", "assistant", "user"]
    call = told[1]["tool_calls"][0]
    assert told[1]["content"] == "" and call["id"] == "picture_1" and call["function"]["name"] == "create_image"
    assert json.loads(call["function"]["arguments"]) == {"prompt": FOX_DESCRIPTION}
    assert told[2] == {"role": "tool", "tool_call_id": "picture_1", "content": ap.PICTURE_HISTORY_RESULT}
    assert told[3] == {"role": "assistant", "content": "Here comes your fox!"}


def test_a_picture_line_alone_two_pictures_and_lookalikes():
    alone = ap._pictures_as_calls([{"role": "user", "content": "draw a fox"},
                                   {"role": "assistant", "content": "[Picture created: a fox]"},
                                   {"role": "user", "content": "and a cat"},
                                   {"role": "assistant", "content": "Sure.\n\n[Picture created: a cat]"}])
    assert [m["role"] for m in alone] == ["user", "assistant", "tool", "user", "assistant", "tool", "assistant"]
    assert [m["tool_calls"][0]["id"] for m in alone if m.get("tool_calls")] == ["picture_1", "picture_2"]
    untouched = [{"role": "user", "content": "[Picture created: typed by the user]"},
                 {"role": "assistant", "content": "The marker [Picture created: x] mid-sentence is just words."}]
    assert ap._pictures_as_calls(untouched) == untouched


@pytest.mark.anyio
async def test_with_the_tool_on_offer_the_member_sees_its_earlier_call_not_the_marker():
    tools, run_tool, maker = pictured()
    member = Member_(content("Here is a bluer one.") + DONE)
    body = {"messages": [{"role": "user", "content": "Can I get a picture of a fox in the snow?"},
                         {"role": "assistant", "content": "Here comes your fox!\n\n[Picture created: " + FOX_DESCRIPTION
                          + "]"},
                         {"role": "user", "content": "make it more blue"}]}
    await run(member, body, tools, run_tool)
    sent = member.requests[0]["messages"]
    assert not any("[Picture created:" in str(m.get("content")) for m in sent)
    (call_turn,) = [m for m in sent if m.get("tool_calls")]
    assert json.loads(call_turn["tool_calls"][0]["function"]["arguments"]) == {"prompt": FOX_DESCRIPTION}


@pytest.mark.anyio
async def test_without_the_tool_the_marker_stays_as_the_app_sent_it():
    member = Member_(content("A fox, yes.") + DONE)
    reply_text = "Here comes your fox!\n\n[Picture created: " + FOX_DESCRIPTION + "]"
    body = {"messages": [{"role": "user", "content": "Can I get a picture of a fox in the snow?"},
                         {"role": "assistant", "content": reply_text},
                         {"role": "user", "content": "thanks"}]}
    await run(member, body)
    sent = member.requests[0]["messages"]
    assert {"role": "assistant", "content": reply_text} in sent and not any(m.get("tool_calls") for m in sent)
