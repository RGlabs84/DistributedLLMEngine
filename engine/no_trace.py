"""
What a coding member must not keep, and what it may be told about a request.

A coding member (docs/coding-members.md, section 8) runs a whole coding model on a volunteer's
machine, so for the length of a request it holds a stranger's code. This module is the first of
two halves of the promise made about that:

* **No trace** (this file): the member's llama-server is started so that nothing about a request
  is written down, and the member proxy only ever receives what it needs to answer.
* **Canaries** (engine/canary.py): a volunteer who reads their own machine's memory or traffic
  anyway, and acts on what they find, is caught.

It is honest about its reach. These settings stop the *software* from keeping prompts: no log
line, no file, no monitoring endpoint, no web page. They cannot stop the person who owns the
machine from attaching a debugger to the process. That is exactly the gap the canaries cover.

The flags below were checked against llama.cpp b11838, the build DLNE ships (``llama-server
--help`` and the source of ``common/arg.cpp``, ``common/log.cpp``, ``tools/server``):

``--log-disable``
    Pauses llama.cpp's logger (``common_log_pause``): every message is dropped whatever its
    level. This is the one that matters. At the debug level the server logs the whole slot
    (prompt and generated text) when it launches a request and the text of every generated token,
    and even at error level a failed chat-format parse quotes a piece of the model's output.
``--log-verbosity 0``
    The lowest threshold (generic output only). Redundant next to ``--log-disable``, kept so that
    a build that changed what ``--log-disable`` means still logs almost nothing.
``--no-slots``
    Turns off ``GET /slots``. Its JSON carries each slot's full prompt and generated text, readable
    by any process on the machine that can reach the loopback port.
``--no-webui``
    No web page on the port (default is on). Nothing there to show a conversation, and nothing for
    a browser tab to keep. (``--no-ui`` is the same switch under its newer name; b11838 accepts both.)

What is deliberately *not* in the list, and why:

* ``--no-cache-prompt`` / ``--cache-ram 0``: they would keep less in memory between requests, but
  sticky routing depends on the prompt cache; without it every agent turn re-reads the whole
  conversation (at the 80 to 120 tokens a second a laptop GPU manages, minutes per turn). The
  cache lives in memory only, never on disk.
* ``--slot-save-path``, ``--log-file``, ``--log-prompts-dir``, ``--lookup-cache-dynamic``: the
  options that *would* write prompt-derived data to disk are off by default. The point is to never
  pass them, which :func:`no_trace_violations` checks.
* ``--props``, ``--metrics``, ``--tools``, ``--agent``, ``--media-path``, ``--connect``: off by
  default, and listed in :data:`FORBIDDEN_LLAMA_FLAGS` so nobody turns them on.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Tuple

# The arguments every coding member's llama-server is started with, in addition to its model,
# context, host and port. A list of separate argv items, ready to extend a command line with.
LLAMA_NO_TRACE_ARGS: List[str] = [
    "--log-disable",
    "--log-verbosity", "0",
    "--no-slots",
    "--no-webui",
]

# Flags that make llama-server write down, expose or relay request content. None of them is ever
# wanted on a coding member. Both spellings llama.cpp accepts are listed where it has two.
FORBIDDEN_LLAMA_FLAGS = frozenset({
    "--log-file",            # a log on disk
    "--log-prompts-dir",     # prompts, one file each, on disk
    "--slot-save-path",      # KV cache snapshots (the conversation) on disk
    "--lookup-cache-dynamic", "-lcd",   # a cache of the tokens seen, on disk
    "--verbose", "-v", "--log-verbose",  # debug level: prompt and output text in the log
    "--slots",               # /slots shows prompts
    "--props",               # lets a client rewrite server properties
    "--metrics",
    "--webui", "--ui",
    "--tools", "--agent", "-ag",
    "--ui-mcp-proxy", "--webui-mcp-proxy",
    "--media-path",
    "--connect",             # opens a peer-to-peer tunnel to the server
})

# Environment variables llama-server reads as settings. A volunteer's shell may have any of them
# set (LLAMA_ARG_LOG_FILE would put every line in a file the arguments never mention), so a
# member starts llama-server with these stripped (see :func:`no_trace_env`).
_LLAMA_ENV_PREFIXES = ("LLAMA_ARG_", "LLAMA_LOG", "LLAMA_API_KEY", "LLAMA_CACHE")


def no_trace_env(env: Mapping[str, str]) -> Dict[str, str]:
    """
    ``env`` without any variable that configures llama-server, for the member's child process.

    The command line wins over the environment for an option given on both, but the environment
    still adds every option the command line leaves out, so a stray ``LLAMA_ARG_LOG_FILE`` or
    ``LLAMA_ARG_SLOT_SAVE_PATH`` would switch tracing back on without a single visible argument.
    """
    return {k: v for k, v in env.items() if not str(k).upper().startswith(_LLAMA_ENV_PREFIXES)}


def _flag_name(arg: str) -> str:
    """``--log-file=x`` and ``--log-file`` are the same flag."""
    return arg.split("=", 1)[0]


def no_trace_violations(args: Iterable[str]) -> List[str]:
    """
    What is wrong with a llama-server argument list from the no-trace point of view; empty when it
    is fine. Reports every forbidden flag present and every required one missing, as short
    sentences, so a member can refuse to start (or refuse to claim ``no_trace`` in its heartbeat)
    and say why.
    """
    items = [str(a) for a in args]
    problems: List[str] = []
    present = {_flag_name(a) for a in items}
    for flag in sorted(present & FORBIDDEN_LLAMA_FLAGS):
        problems.append(f"{flag} is not allowed on a coding member")
    for flag in ("--log-disable", "--no-slots", "--no-webui"):
        if flag not in present:
            problems.append(f"{flag} is required on a coding member")
    # A verbosity above 0 is only harmless while the logger is paused; flag it so it is noticed.
    for i, arg in enumerate(items):
        name = _flag_name(arg)
        if name in ("-lv", "--verbosity", "--log-verbosity"):
            value = arg.split("=", 1)[1] if "=" in arg else (items[i + 1] if i + 1 < len(items) else "")
            try:
                if int(value) > 0 and "--log-disable" not in present:
                    problems.append(f"{name} {value} logs more than errors and --log-disable is missing")
            except ValueError:
                problems.append(f"{name} needs a number")
    return problems


# --------------------------------------------------------------------------------------------
# What the member proxy is allowed to receive
# --------------------------------------------------------------------------------------------

# Only these survive, and only if their values look like what they should be. Everything else a
# request carries (Authorization, X-Forwarded-For, User-Agent, X-DLNE-*, cookies, Host, the
# client's address) says who asked, so the member never sees any of it.
_SAFE_CONTENT_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]{1,64}/[A-Za-z0-9!#$&^_.+-]{1,64}"
                                r"(?:\s*;\s*[A-Za-z0-9_.-]{1,32}=[A-Za-z0-9._\"-]{1,64})*$")
_SAFE_CONTENT_LENGTH = re.compile(r"^[0-9]{1,12}$")


def _pairs(headers: Any) -> Iterable[Tuple[str, str]]:
    """The (name, value) pairs of a dict, a Starlette/httpx ``Headers``, or a list of pairs."""
    if headers is None:
        return []
    if hasattr(headers, "raw"):   # Starlette / httpx Headers: raw is a list of byte pairs
        try:
            return [(k.decode("latin-1"), v.decode("latin-1")) for k, v in headers.raw]
        except Exception:
            pass
    if hasattr(headers, "items"):
        return list(headers.items())
    return list(headers)


def forward_headers(headers: Any) -> Dict[str, str]:
    """
    The headers a coding member may receive for a request: ``content-type`` and ``content-length``,
    and nothing else. Names come back lower-case. A value that does not look like a content type
    or a plain number is dropped too (so a crafted header cannot smuggle a line break or a second
    header through), and the first of a repeated header wins.
    """
    kept: Dict[str, str] = {}
    for name, value in _pairs(headers):
        try:
            key = str(name).strip().lower()
            text = str(value).strip()
        except Exception:
            continue
        if key in kept:
            continue
        if key == "content-type" and _SAFE_CONTENT_TYPE.match(text):
            kept[key] = text
        elif key == "content-length" and _SAFE_CONTENT_LENGTH.match(text):
            kept[key] = text
    return kept
