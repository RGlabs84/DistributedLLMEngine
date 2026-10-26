"""
Tests for engine/no_trace.py: the llama-server arguments that keep a coding member from writing
down what it processes, the environment scrub, and the one header filter between the coordinator
and a member (docs/coding-members.md, section 8).
"""

import httpx
import pytest
from starlette.datastructures import Headers

from engine import no_trace
from engine.no_trace import (
    FORBIDDEN_LLAMA_FLAGS,
    LLAMA_NO_TRACE_ARGS,
    forward_headers,
    no_trace_env,
    no_trace_violations,
)


# --- the llama-server arguments ---------------------------------------------------------------

def test_the_arguments_are_a_flat_list_of_strings_to_extend_a_command_line_with():
    assert isinstance(LLAMA_NO_TRACE_ARGS, list)
    assert all(isinstance(a, str) and a for a in LLAMA_NO_TRACE_ARGS)


def test_the_arguments_are_exactly_the_four_checked_against_b11838():
    """Each exists in b11838's --help: --log-disable, --log-verbosity N, --no-slots, --no-webui."""
    assert LLAMA_NO_TRACE_ARGS == ["--log-disable", "--log-verbosity", "0", "--no-slots", "--no-webui"]


def test_logging_is_switched_off_not_just_turned_down():
    """Debug-level lines carry the prompt and every generated token; --log-disable drops all lines."""
    assert "--log-disable" in LLAMA_NO_TRACE_ARGS
    assert LLAMA_NO_TRACE_ARGS[LLAMA_NO_TRACE_ARGS.index("--log-verbosity") + 1] == "0"


def test_the_slots_endpoint_and_the_web_page_are_off():
    """/slots returns each slot's prompt and generated text; the web page is no use on a member."""
    assert "--no-slots" in LLAMA_NO_TRACE_ARGS
    assert "--no-webui" in LLAMA_NO_TRACE_ARGS


def test_nothing_in_the_list_writes_prompt_data_to_disk_or_exposes_it():
    flags = {a for a in LLAMA_NO_TRACE_ARGS if a.startswith("-")}
    assert not flags & FORBIDDEN_LLAMA_FLAGS
    for banned in ("--slot-save-path", "--log-file", "--log-prompts-dir", "--lookup-cache-dynamic", "--verbose"):
        assert banned in FORBIDDEN_LLAMA_FLAGS
        assert banned not in LLAMA_NO_TRACE_ARGS


def test_the_list_on_its_own_passes_its_own_check():
    assert no_trace_violations(LLAMA_NO_TRACE_ARGS) == []


def test_a_realistic_member_command_line_passes():
    cmd = ["-m", "model.gguf", "--host", "127.0.0.1", "--port", "8123", "--fit", "on", "-c", "32768",
           *LLAMA_NO_TRACE_ARGS]
    assert no_trace_violations(cmd) == []


@pytest.mark.parametrize("flag", sorted(FORBIDDEN_LLAMA_FLAGS))
def test_every_forbidden_flag_is_reported(flag):
    problems = no_trace_violations([*LLAMA_NO_TRACE_ARGS, flag])
    assert any(flag in p and "not allowed" in p for p in problems)


def test_a_forbidden_flag_given_as_name_equals_value_is_still_caught():
    problems = no_trace_violations([*LLAMA_NO_TRACE_ARGS, "--log-file=/tmp/x.log", "--slot-save-path=/tmp/s"])
    assert any("--log-file" in p for p in problems)
    assert any("--slot-save-path" in p for p in problems)


@pytest.mark.parametrize("missing", ["--log-disable", "--no-slots", "--no-webui"])
def test_a_missing_required_flag_is_reported(missing):
    args = [a for a in LLAMA_NO_TRACE_ARGS if a != missing]
    assert any(missing in p and "required" in p for p in no_trace_violations(args))


def test_a_verbose_threshold_is_flagged_only_when_the_logger_is_not_paused():
    without_disable = [a for a in LLAMA_NO_TRACE_ARGS if a != "--log-disable"]
    assert any("--log-verbosity 3" in p for p in no_trace_violations([*without_disable, "--log-verbosity", "3"]))
    assert no_trace_violations([*LLAMA_NO_TRACE_ARGS, "--log-verbosity=3"]) == []


def test_a_non_numeric_verbosity_is_reported():
    assert any("needs a number" in p for p in no_trace_violations([*LLAMA_NO_TRACE_ARGS, "-lv", "loud"]))


# --- the environment --------------------------------------------------------------------------

def test_the_environment_scrub_removes_every_llama_setting_and_keeps_the_rest():
    env = {
        "PATH": "/usr/bin", "HOME": "/home/x", "LD_LIBRARY_PATH": "/opt/llama",
        "LLAMA_ARG_LOG_FILE": "/tmp/llama.log", "LLAMA_ARG_SLOT_SAVE_PATH": "/tmp/slots",
        "LLAMA_ARG_ENDPOINT_SLOTS": "1", "LLAMA_LOG_FILE": "x", "llama_arg_ui": "1",
        "LLAMA_API_KEY": "k", "LLAMA_CACHE": "/tmp/c",
    }
    cleaned = no_trace_env(env)
    assert cleaned == {"PATH": "/usr/bin", "HOME": "/home/x", "LD_LIBRARY_PATH": "/opt/llama"}
    assert "LLAMA_ARG_LOG_FILE" in env, "the caller's mapping is left alone"


# --- forward_headers --------------------------------------------------------------------------

REAL_REQUEST_HEADERS = {
    "Host": "dlne.duckdns.org",
    "Authorization": "Bearer dlne_sk_0123456789abcdef",
    "X-Forwarded-For": "203.0.113.9",
    "X-Real-IP": "203.0.113.9",
    "X-DLNE-Session": "5f3a",
    "X-DLNE-Route": "r_abc",
    "X-DLNE-Privacy": "trusted",
    "User-Agent": "dlne-vscode/1.2.4",
    "Cookie": "a=b",
    "Accept": "text/event-stream",
    "Content-Type": "application/json",
    "Content-Length": "1234",
}


def test_only_content_type_and_content_length_survive():
    assert forward_headers(REAL_REQUEST_HEADERS) == {"content-type": "application/json", "content-length": "1234"}


def test_nothing_that_says_who_asked_gets_through():
    kept = forward_headers(REAL_REQUEST_HEADERS)
    text = " ".join(f"{k}={v}" for k, v in kept.items()).lower()
    for leak in ("203.0.113.9", "dlne_sk_", "bearer", "dlne-vscode", "x-dlne", "cookie", "dlne.duckdns.org"):
        assert leak not in text


def test_names_match_in_any_case_and_come_back_lower_case():
    assert forward_headers({"CONTENT-TYPE": "application/json", "content-LENGTH": "7"}) == \
        {"content-type": "application/json", "content-length": "7"}


def test_a_content_type_with_a_charset_is_kept():
    assert forward_headers({"Content-Type": "application/json; charset=utf-8"}) == \
        {"content-type": "application/json; charset=utf-8"}


def test_it_reads_starlette_httpx_and_pair_list_headers():
    wire = [(b"content-type", b"application/json"), (b"authorization", b"Bearer x"), (b"content-length", b"5")]
    expected = {"content-type": "application/json", "content-length": "5"}
    assert forward_headers(Headers(raw=wire)) == expected
    assert forward_headers(httpx.Headers(wire)) == expected
    assert forward_headers([("Content-Type", "application/json"), ("Cookie", "a=b"), ("Content-Length", "5")]) == expected


def test_nothing_or_no_matching_headers_gives_an_empty_dict():
    assert forward_headers(None) == {}
    assert forward_headers({}) == {}
    assert forward_headers({"Authorization": "Bearer x"}) == {}


@pytest.mark.parametrize("bad", [
    "application/json\r\nX-Injected: 1", "application/json\nSet-Cookie: a=b", "json", "application/",
    "/json", "application/json; x=y\r\nX-Injected: 1", "a" * 200 + "/json", "application/json;" + "x" * 300,
])
def test_a_malformed_or_hostile_content_type_is_dropped(bad):
    assert "content-type" not in forward_headers({"Content-Type": bad, "Content-Length": "3"})


@pytest.mark.parametrize("bad", ["12abc", "-1", "1.5", "", "1" * 20, "1\r\nX: y", " 0x10"])
def test_a_content_length_that_is_not_a_plain_number_is_dropped(bad):
    assert "content-length" not in forward_headers({"Content-Length": bad, "Content-Type": "application/json"})


def test_a_repeated_header_keeps_the_first_value():
    pairs = [("Content-Type", "application/json"), ("content-type", "text/plain"), ("Content-Length", "4"), ("content-length", "9")]
    assert forward_headers(pairs) == {"content-type": "application/json", "content-length": "4"}


def test_the_module_exports_what_the_contract_names():
    assert callable(no_trace.forward_headers)
    assert isinstance(no_trace.LLAMA_NO_TRACE_ARGS, list)
