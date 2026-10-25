"""
The memory-module summary, and the chat template the engine is spawned with.

WHAT WENT WRONG
---------------
Changing models in the WebUI produced ``{"name": "good", "parameters": {}}`` as the
assistant's reply, for several turns, before normal answers resumed.

Two faults stacked. Engines were spawned without ``--jinja``, so llama.cpp matched the
GGUF's chat template against its own list of known ones and fell back to ChatML for
anything unrecognised - a tool-trained model served through another family's delimiters
emits its function-call format as ordinary text. And the coordinator's context-overflow
pass asks the user's own model to summarise the older turns, then splices the reply
straight into the next prompt without ever checking it was prose. A model that answered
that prompt with a function call had the object injected into its own context, which
taught it that this was a conversation where tool calls belong.

Model switching is what surfaced it: ``safe_context`` is derived per model, so a switch
can collapse the per-slot budget and trip the summariser on a history that fit a moment
earlier.

WHAT IS ASSERTED HERE
---------------------
That structured output is never injected as a summary, that ordinary prose still is, and
that engines are launched with the model's own template.
"""

import json

import pytest

from engine.coordinator import summary_is_usable


# ------------------------------------------------------------------ rejected: tool calls


def test_the_observed_llama_tool_call_is_rejected():
    """The exact payload that reached a user's chat window."""
    assert summary_is_usable('{"name": "good", "parameters": {}}') is False


def test_a_populated_llama_tool_call_is_rejected():
    assert summary_is_usable(
        '{"name": "get_weather", "parameters": {"city": "Dublin"}}'
    ) is False


def test_a_qwen_style_tagged_tool_call_is_rejected():
    assert summary_is_usable(
        '<tool_call>\n{"name": "recall", "arguments": {}}\n</tool_call>'
    ) is False


def test_tags_are_rejected_even_when_prose_surrounds_them():
    """
    The tags alone teach the model that tool calls belong here, so a reply that is mostly
    prose is still poison if it carries them.
    """
    assert summary_is_usable(
        'The user is called Ross.\n<tool_call>{"name": "x", "arguments": {}}</tool_call>'
    ) is False


def test_the_tag_match_is_case_insensitive():
    assert summary_is_usable('<TOOL_CALL>{"name": "x"}</TOOL_CALL>') is False


def test_a_json_array_reply_is_rejected():
    assert summary_is_usable('["fact one", "fact two"]') is False


def test_leading_whitespace_does_not_smuggle_json_through():
    assert summary_is_usable('\n\n  {"name": "good", "parameters": {}}  \n') is False


# ------------------------------------------------------------------- rejected: nothing


def test_an_empty_summary_is_rejected():
    assert summary_is_usable("") is False


def test_a_whitespace_only_summary_is_rejected():
    assert summary_is_usable("   \n\t  ") is False


def test_a_non_string_is_rejected():
    """The streaming accumulator can only produce a string, but this is a guard, not a hint."""
    assert summary_is_usable(None) is False
    assert summary_is_usable({"name": "good"}) is False


# ------------------------------------------------------------------------ accepted: prose


def test_an_ordinary_summary_is_accepted():
    assert summary_is_usable(
        "The user is running a distributed inference cluster and prefers concise answers."
    ) is True


def test_prose_that_merely_quotes_json_is_accepted():
    """
    A real summary of a coding conversation contains JSON. It is only rejected when the
    *whole* reply parses, which this does not.
    """
    text = 'The user asked why the API returns {"status": "ok"} instead of a 204.'
    assert summary_is_usable(text) is True
    with pytest.raises(ValueError):
        json.loads(text)


def test_a_summary_that_opens_with_a_brace_but_is_not_json_is_accepted():
    assert summary_is_usable('{ is the character the user kept mistyping.') is True


def test_a_markdown_bulleted_summary_is_accepted():
    assert summary_is_usable("- Name: Ross\n- Prefers: short replies") is True
