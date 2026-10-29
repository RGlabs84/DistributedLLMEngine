"""
Canaries while image generation is on (2.6.1): the decoy requests look exactly like the extension's, which then carries a ninth
tool (generate_image) and one more line in its system prompt. With image generation off a decoy is what it always was.

The extension's text is read from its source (vscode-extension/src/agent/imageTool.ts), the way the other canary parity
tests read the extension, so the two cannot drift apart. The file is in this repository once the extension's 2.6.1 branch is
merged; until then DLNE_EXT_SRC (the folder holding imageTool.ts and agentLoop.ts) or the sibling 2.6.1 extension worktree
is used, and with neither the parity tests skip.
"""

import json
import os
import random
import re
from pathlib import Path

import pytest

from engine import canary
from engine.canary import CanaryScheduler, build_decoy_request, make_decoy
from tests.test_canary import FakeMember, FakeRouter, ForcedRng, anyio_backend, book, clock, tid  # noqa: F401 - fixtures

REPO = Path(__file__).resolve().parent.parent
AGENT_DIR = Path("vscode-extension") / "src" / "agent"


def extension_file(name):
    """The text of src/agent/<name> in the first extension source that has imageTool.ts (both files must come from one), or None."""
    folders = [Path(os.environ["DLNE_EXT_SRC"])] if os.environ.get("DLNE_EXT_SRC") else []
    folders += [REPO / AGENT_DIR, REPO.parent / "dlne-261-ext" / AGENT_DIR]
    for folder in folders:
        if (folder / "imageTool.ts").is_file() and (folder / name).is_file():
            return (folder / name).read_text(encoding="utf-8")
    return None


# --- the bodies -----------------------------------------------------------------------------------------------

DECOY = make_decoy()


def seeded(**kwargs):
    return build_decoy_request(DECOY, random.Random(11), **kwargs)


def test_with_image_generation_off_a_decoy_is_exactly_what_it_was():
    body = seeded()
    assert body["tools"] is canary._AGENT_TOOLS and len(body["tools"]) == 8
    assert body["messages"][0] == {"role": "system", "content": canary._AGENT_SYSTEM_PROMPT}
    assert "generate_image" not in json.dumps(body)
    assert seeded(image_generation=False)["tools"] == body["tools"]


def test_with_it_on_the_ninth_tool_is_last_and_the_first_eight_are_untouched():
    body = seeded(image_generation=True)
    assert [t["function"]["name"] for t in body["tools"]][-1] == "generate_image" and len(body["tools"]) == 9
    assert body["tools"][:8] == canary._AGENT_TOOLS
    assert len(canary._AGENT_TOOLS) == 8                             # the shared list is not mutated


def test_with_it_on_one_line_joins_the_prompt_before_run_terminal_command_and_nothing_else_changes():
    plain, with_images = seeded(), seeded(image_generation=True)
    prompt, original = with_images["messages"][0]["content"], canary._AGENT_SYSTEM_PROMPT
    assert prompt.count(canary._IMAGE_PROMPT_LINE) == 1
    before, after = prompt.split(canary._IMAGE_PROMPT_LINE + "\n")
    assert before + after == original
    assert after.startswith("- run_terminal_command(") and before.endswith("\n")
    assert with_images["messages"][1:] == plain["messages"][1:]                    # same seed: the same conversation
    assert {k: v for k, v in with_images.items() if k not in ("messages", "tools")} == \
        {k: v for k, v in plain.items() if k not in ("messages", "tools")}


# --- parity with the extension --------------------------------------------------------------------------------

STRING = r"'((?:[^'\\]|\\.)*)'"


def js_string(source, pattern):
    match = re.search(pattern, source, re.DOTALL)
    assert match, pattern
    return match.group(1).replace("\\'", "'").replace("\\\\", "\\")


@pytest.fixture(scope="module")
def ts():
    source = extension_file("imageTool.ts")
    if source is None:
        pytest.skip("the extension's imageTool.ts is not here (set DLNE_EXT_SRC, or merge the 2.6.1 extension)")
    return source


def test_the_extensions_tool_definition_is_the_canarys_byte_for_byte(ts):
    body = ts[ts.index("export function imageToolDefinition"):]
    body = body[:body.index("\n}\n")]
    sizes = re.findall(r"'(\d+x\d+)'", re.search(r"export const IMAGE_SIZES = \[(.*?)\];", ts, re.DOTALL).group(1))
    extension = {"type": "function", "function": {
        "name": js_string(ts, r"export const IMAGE_TOOL_NAME = " + STRING),
        "description": js_string(body, r"function: \{\s*name: IMAGE_TOOL_NAME,\s*description: " + STRING),
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": js_string(body, r"prompt: \{ type: 'string', description: " + STRING)},
            "path": {"type": "string", "description": js_string(body, r"path: \{ type: 'string', description: " + STRING)},
            "size": {"type": "string", "enum": sizes,
                     "description": js_string(body, r"size: \{ type: 'string', enum: IMAGE_SIZES, description: " + STRING)},
        }, "required": re.findall(r"'([a-z_]+)'", re.search(r"required: \[(.*?)\]", body).group(1))}}}
    assert len(sizes) == 4
    # Compared as the bytes the extension's JSON.stringify and the canary's json.dumps put on the wire.
    assert json.dumps(canary._IMAGE_TOOL, separators=(",", ":")) == json.dumps(extension, separators=(",", ":"))


def test_the_extensions_prompt_line_and_where_it_goes_are_the_canarys(ts):
    assert js_string(ts, r"export const IMAGE_PROMPT_LINE = " + STRING) == canary._IMAGE_PROMPT_LINE
    assert js_string(ts, r"const marker = " + STRING) == canary._IMAGE_PROMPT_MARKER
    assert "`${IMAGE_PROMPT_LINE}\\n${marker}`" in ts                    # the line, a newline, then the marker's own line


def test_the_extension_adds_the_tool_last_to_the_agents_list(ts):
    source = extension_file("agentLoop.ts")
    assert source is not None and "[...ToolRegistry.getToolDefinitions(), imageToolDefinition()]" in source


# --- the scheduler ----------------------------------------------------------------------------------------------

@pytest.fixture
def recording():
    """build_decoy_request, remembering what it was asked for."""
    asked = []

    def build(decoy, rng, **kwargs):
        asked.append(kwargs)
        return build_decoy_request(decoy, rng, **kwargs)

    return build, asked


@pytest.mark.anyio
@pytest.mark.parametrize("flag, nine", [(None, False), (lambda: False, False), (lambda: True, True)])
async def test_the_scheduler_asks_for_the_image_tool_only_while_images_are_offered(book, clock, recording, flag, nine):
    build, asked = recording
    router = FakeRouter([FakeMember("a")], clock=clock)
    s = CanaryScheduler(router.members, router.forward, book=book, trusted=lambda: set(), per_day=lambda: 4,
                        clock=clock, rng=ForcedRng(0.0), build=build, image_generation=flag)
    assert await s.tick() == [tid("a")]
    assert asked == ([{"image_generation": True}] if nine else [{}])
    ((_, body),) = router.sent
    assert len(body["tools"]) == (9 if nine else 8)
    assert ("generate_image(prompt, path, size)" in body["messages"][0]["content"]) is nine


@pytest.mark.anyio
async def test_the_flag_may_be_async_and_is_read_every_time():
    state = {"on": True}

    async def offered():
        return state["on"]

    s = CanaryScheduler(lambda: [], None, image_generation=offered)
    assert await s._images_offered() is True
    state["on"] = False
    assert await s._images_offered() is False


@pytest.mark.anyio
async def test_a_flag_that_fails_or_is_not_a_plain_yes_leaves_the_decoy_as_it_was(book, clock, recording):
    def broken():
        raise RuntimeError("status unavailable")

    for flag in (broken, lambda: "yes", lambda: None):
        s = CanaryScheduler(lambda: [], None, image_generation=flag)
        assert await s._images_offered() is False


def test_the_coordinator_hands_the_scheduler_its_own_view_of_whether_images_are_offered():
    source = (REPO / "engine" / "coordinator.py").read_text(encoding="utf-8")
    assert "image_generation=_image_offered" in source
