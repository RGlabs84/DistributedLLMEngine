"""
Canaries while the coordinator's engine can read pictures (extension 1.6.0, docs/release-2.7-contract.md 4d): the agent then
offers view_image, last in its tool list (after generate_image when both are on), and one more line in its system prompt before
run_terminal_command's. A decoy does exactly that while the coordinator's status says capabilities.vision, and is what it
always was while it does not. The extension's text is read from its source (viewImage.ts, agentLoop.ts), as test_image_canary.py
does, and scripts/cache_probe.py carries the same words.
"""

import json
import random
import re
import sys

import pytest

from engine import canary
from engine.canary import CanaryScheduler, build_decoy_request, make_decoy
from tests.test_canary import FakeMember, FakeRouter, ForcedRng, anyio_backend, book, clock, tid  # noqa: F401 - fixtures
from tests.test_image_canary import REPO, STRING, extension_file, js_string, recording  # noqa: F401 - fixtures

DECOY = make_decoy()


def seeded(**kwargs):
    return build_decoy_request(DECOY, random.Random(11), **kwargs)


def names(body):
    return [t["function"]["name"] for t in body["tools"]]


def test_with_vision_off_a_decoy_is_exactly_what_it_was():
    body = seeded()
    assert body["tools"] is canary._AGENT_TOOLS and len(body["tools"]) == 10
    assert body["messages"][0]["content"] == canary._AGENT_SYSTEM_PROMPT
    assert "view_image" not in json.dumps(body)
    assert seeded(vision=False) == body


def test_with_vision_on_view_image_is_the_eleventh_tool_and_the_ten_are_untouched():
    body = seeded(vision=True)
    assert names(body)[-1] == "view_image" and len(body["tools"]) == 11
    assert body["tools"][:10] == canary._AGENT_TOOLS and body["tools"][-1] == canary._VIEW_IMAGE_TOOL
    assert len(canary._AGENT_TOOLS) == 10                                  # the shared list is not mutated


def test_with_both_on_generate_image_comes_first_and_view_image_last():
    body = seeded(image_generation=True, vision=True)
    assert names(body)[-2:] == ["generate_image", "view_image"] and len(body["tools"]) == 12
    assert body["tools"][:10] == canary._AGENT_TOOLS
    prompt = body["messages"][0]["content"]
    assert f"{canary._IMAGE_PROMPT_LINE}\n{canary._VIEW_IMAGE_PROMPT_LINE}\n- run_terminal_command(" in prompt
    # with only generate_image on, nothing changed from before
    assert seeded(image_generation=True)["messages"][0]["content"] == canary.agent_system_prompt(True)


def test_the_prompt_line_joins_before_run_terminal_command_and_nothing_else_changes():
    plain, with_vision = seeded(), seeded(vision=True)
    prompt = with_vision["messages"][0]["content"]
    assert prompt.count(canary._VIEW_IMAGE_PROMPT_LINE) == 1
    before, after = prompt.split(canary._VIEW_IMAGE_PROMPT_LINE + "\n")
    assert before + after == canary._AGENT_SYSTEM_PROMPT
    assert after.startswith("- run_terminal_command(") and before.endswith("\n")
    assert with_vision["messages"][1:] == plain["messages"][1:]            # same seed: the same conversation
    assert {k: v for k, v in with_vision.items() if k not in ("messages", "tools")} == \
        {k: v for k, v in plain.items() if k not in ("messages", "tools")}


# --- parity with the extension ---------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ts():
    source = extension_file("viewImage.ts")
    if source is None:
        pytest.skip("the extension's viewImage.ts is not here")
    return source


def test_the_extensions_tool_definition_is_the_canarys_byte_for_byte(ts):
    body = ts[ts.index("export function viewImageToolDefinition"):]
    body = body[:body.index("\n}\n")]
    extension = {"type": "function", "function": {
        "name": js_string(ts, r"export const VIEW_IMAGE_TOOL_NAME = " + STRING),
        "description": js_string(body, r"function: \{\s*name: VIEW_IMAGE_TOOL_NAME,\s*description: " + STRING),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": js_string(body, r"path: \{ type: 'string', description: " + STRING)},
        }, "required": re.findall(r"'([a-z_]+)'", re.search(r"required: \[(.*?)\]", body).group(1))}}}
    assert json.dumps(canary._VIEW_IMAGE_TOOL, separators=(",", ":")) == json.dumps(extension, separators=(",", ":"))


def test_the_extensions_prompt_line_and_where_it_goes_are_the_canarys(ts):
    assert js_string(ts, r"export const VIEW_IMAGE_PROMPT_LINE = " + STRING) == canary._VIEW_IMAGE_PROMPT_LINE
    assert js_string(ts, r"const marker = " + STRING) == canary._IMAGE_PROMPT_MARKER
    assert "`${VIEW_IMAGE_PROMPT_LINE}\\n${marker}`" in ts


def test_the_extension_adds_the_tool_and_the_line_in_the_canarys_order():
    source = extension_file("agentLoop.ts")
    assert source is not None
    assert "...(this.imageOffered ? [imageToolDefinition()] : []),\n      ...(this.visionOn ? [viewImageToolDefinition()] : []),"  in source
    # the view_image line goes on after the generate_image line, so it lands after it: both before run_terminal_command's
    assert "this.visionOn ? withViewImageLine(withImage) : withImage" in source
    assert "const withImage = this.imageOffered ? withImageLine(AGENT_SYSTEM_PROMPT) : AGENT_SYSTEM_PROMPT;" in source


def test_the_cache_probe_carries_the_same_words():
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import cache_probe as cp
    finally:
        sys.path.pop(0)
    assert json.dumps(cp.VIEW_IMAGE_TOOL) == json.dumps(canary._VIEW_IMAGE_TOOL)
    assert cp.VIEW_IMAGE_PROMPT_LINE == canary._VIEW_IMAGE_PROMPT_LINE
    assert cp.agent_prompt(True) == canary.agent_system_prompt(False, True) and cp.agent_prompt() == canary._AGENT_SYSTEM_PROMPT
    assert cp.agent_tools(True) == [*cp.TOOLS, cp.VIEW_IMAGE_TOOL] and cp.agent_tools() is cp.TOOLS
    assert cp.agent_tools(True) == canary.agent_tools(False, True)


# --- the scheduler -----------------------------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("flag, on", [(None, False), (lambda: False, False), (lambda: True, True)])
async def test_the_scheduler_asks_for_view_image_only_while_the_engine_can_read_pictures(book, clock, recording, flag, on):
    build, asked = recording
    router = FakeRouter([FakeMember("a")], clock=clock)
    s = CanaryScheduler(router.members, router.forward, book=book, trusted=lambda: set(), per_day=lambda: 4,
                        clock=clock, rng=ForcedRng(0.0), build=build, vision=flag)
    assert await s.tick() == [tid("a")]
    assert asked == ([{"vision": True}] if on else [{}])
    ((_, body),) = router.sent
    assert len(body["tools"]) == (11 if on else 10)
    assert ("view_image(path)" in body["messages"][0]["content"]) is on


@pytest.mark.anyio
async def test_both_flags_together_and_a_flag_that_fails_or_is_not_a_plain_yes(book, clock, recording):
    build, asked = recording
    router = FakeRouter([FakeMember("a")], clock=clock)

    async def yes():
        return True

    s = CanaryScheduler(router.members, router.forward, book=book, trusted=lambda: set(), per_day=lambda: 4,
                        clock=clock, rng=ForcedRng(0.0), build=build, image_generation=yes, vision=yes)
    assert await s.tick() == [tid("a")]
    assert asked == [{"image_generation": True, "vision": True}]

    def broken():
        raise RuntimeError("status unavailable")

    for flag in (broken, lambda: "yes", lambda: None):
        assert await CanaryScheduler(lambda: [], None, vision=flag)._vision_offered() is False


def test_the_coordinator_hands_the_scheduler_its_own_view_of_whether_the_engine_can_see():
    source = (REPO / "engine" / "coordinator.py").read_text(encoding="utf-8")
    assert "vision=_vision_offered" in source
    assert "return await _engine_vision(manager.get_active_model_names())" in source
