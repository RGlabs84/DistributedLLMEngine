"""
The CPU pool (2.3.0): CPU contributors answer the phone app's requests.

Unlike the GPU pipeline, nothing is sharded here. Each member runs llama-server with a whole
small model (generating a token reads every weight, so on a CPU a model is only as fast as
its memory bandwidth allows, and network hops per token would make that worse). The
coordinator sends each phone request, whole, to the least busy member and streams the answer
back.

TIERS. The signed release lists the pool's models smallest first (engine/release.app_tiers),
each with the RAM a member must lend to host it. The coordinator's own member always serves
the biggest tier its allocation holds, so the phone app starts on the best model the host can
run. Above that, like the GPU pool's 14B -> 32B -> 70B, a bigger tier unlocks once enough
volunteers can host it (``app_tier_min_members``, default 2, so one machine leaving cannot
flip the pool back and forth). Every member is told, in its heartbeat answer, to serve the
biggest tier it can that is not above the unlocked one; members that cannot reach the
unlocked tier keep serving a smaller one as overflow, so nobody's capacity sits idle.

THE COORDINATOR'S OWN MEMBER. On the coordinator host (TheWall) the pool gets the cores and
RAM the owner allocated (``APP_LOCAL_*``): a llama-server on the CPU only, its threads pinned
one per logical CPU so they never migrate. It makes the phone app work before any volunteer
joins.

THROTTLE. Phone users are unlimited but throttled per network address (the free trial's
identity): one answer at a time, and at most ``app_rate_per_minute`` requests a minute.

SEARCH. Open models' knowledge is frozen at their training date, so the phone model is told
today's date and offered lookup tools (engine/search.py: weather, wikipedia, news,
web_search, all free) for anything that may have changed since. When it calls them the
coordinator (never a member) runs the lookups, tells the phone ("dlne" events in the stream:
searching, sources, search_note, search_unavailable) and asks the model again with the
results. After MAX_TOOL_ROUNDS the tools are withdrawn, so an answer always comes.

PRIVACY. A volunteer's machine sees the question and answer text of the requests it serves,
and any search results for it; the phone app says so. Searches go from the coordinator to the
search provider, never from members.
"""

import asyncio
import hashlib
import json
import math
import os
import re
import subprocess
import threading
import time
from collections import deque
from typing import Any, AsyncIterator, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

from engine.logger import log_coordinator, log_error, log_warning
from engine.priority import UsageWindow
from engine.throughput import POOL_PHONE, throughput

# A member silent this long (before its first byte, or between chunks) is given up on.
FIRST_BYTE_TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 3
# What a single phone answer may cost a member, whatever the client asks for.
MAX_OUTPUT_TOKENS = 1024
# How much of a conversation is sent with each question (characters, newest kept).
HISTORY_CHARS = 8000

# The phone model's standing instructions ({today} is filled in per request).
SYSTEM_PROMPT = ("You are DLNE Chat, a helpful assistant people use on their phones. Today is {today}. "
                 "Answer briefly and clearly: phone screens are small. Use short paragraphs and lists, no tables.")
SEARCH_INSTRUCTIONS = (" Your own knowledge has a cutoff date and may be out of date. Use your tools whenever the "
                       "question is about anything that could have changed since then (news, who holds a job or "
                       "title, prices, product versions, sports, weather, schedules, laws) or when you are not sure "
                       "of the answer. Do not look up timeless facts, math, writing, translation or explanations. "
                       "When you use what the tools return, cite it like [1] and never invent a source; when "
                       "nothing was looked up, write no citation brackets at all.")
NO_SEARCH_NOTE = (" Your own knowledge has a cutoff date; when a question may need newer information, answer what "
                  "you know and say it may be out of date.")
# Lookup rounds per answer, and lookups per round: each round costs the CPU model another read.
MAX_TOOL_ROUNDS = 2
MAX_CALLS_PER_ROUND = 2
# Questions plainly about now get a lookup whatever the model would choose: left to itself it
# answered "who won the most recent Super Bowl" from memory, wrongly (2.3.1 smoke test). The
# model still picks which tool; a false positive only costs a lookup.
FRESH_FACTS = re.compile(
    r"\b(?:latest|newest|most recent|recent(?:ly)?|current(?:ly)?|right now|nowadays|today|tonight|"
    r"tomorrow|yesterday|this (?:week|weekend|month|year|season)|last (?:night|week|weekend|month|year|season)|"
    r"who (?:won|is winning|leads)|winners?|scores?|standings|prices?|how much (?:is|are|does|do)|"
    r"weather|forecast|news|headlines?|elections?|stocks?|20[2-9]\d|upcoming|release date|"
    r"com(?:e|es|ing) out|how old is|net worth|still alive|"
    r"next (?:game|match|race|fight|episode|season|election|launch|update|release|event)|"
    r"who(?:'s| is| are) (?:the |our )?(?:president|prime minister|premier|ceo|chancellor|governor|mayor|"
    r"king|queen|pope|leader|coach|head coach|owner|champions?|chair(?:man)?|secretary))\b", re.IGNORECASE)
# "Who is <Name>?" is looked up too: from memory the model made people up ("Who is Wubarrk?" got
# a place in Australia, launch night). Case-sensitive on the name, so "who is he" is left alone.
NAMED_PERSON = re.compile(r"\b(?i:who)(?:'s|\s+(?i:is|was|are|were))\s+(?!(?i:the|a|an|this|that|it|he|she|"
                          r"they|you|i|we|my|your|our|their|his|her)\b)[A-Z]")
# A required lookup's first pass is held back and, when the model talks instead of calling a
# tool, thrown away: capped so that costs a moment, not the 28 s of a whole answer (ops watch).
HELD_PASS_TOKENS = 128
# An answer from memory that says it can't know is a lookup the model should have made: on the
# owner's phone it took "search the web", typed after three such answers, to get one. When a
# first answer hedges like this the phone is told to take it back (a "retract" event) and the
# question is searched. Only ever asked of an answer no lookup went into.
# "I'm not sure" / "I don't know" count only once the next word has arrived and isn't one that
# makes it a question back ("I'm not sure what you mean"): text is judged as it streams in.
_UNSURE_OF = r"(?= (?!(?:what|which|whether|if|how|who|why|where|I)\b)\w+\W|[.,;:!?])"
HEDGES = re.compile(
    r"\bmy (?:last |latest )?(?:knowledge|training)|as of my (?:last |latest |most recent )?update|"
    r"\bmy (?:information|data) (?:may|might|could|only)|"
    r"\bI (?:don't|do not|can't|cannot) (?:have )?(?:access to )?(?:real-?time|live|current|up[- ]to[- ]date|"
    r"the latest|recent)\b|"
    r"\bI(?:'m| am)? (?:can't|cannot|unable to|not able to) (?:browse|search|look (?:it|that|this) up|access|"
    r"check|verify|confirm)\b|"
    r"\bI (?:don't|do not) have (?:any |specific |enough |reliable )?(?:information|info|details|data)\b|"
    r"\bI(?:'m| am) not (?:sure|certain|aware|familiar)" + _UNSURE_OF + "|"
    r"\bI (?:don't|do not) know" + _UNSURE_OF + "|"
    r"\b(?:may|might|could) (?:be|have) (?:out of date|outdated|changed since)|"
    r"\bno (?:widely |well[- ])?(?:known|recogni[sz]ed|notable) |\bnot (?:widely|well) known\b|"
    r"\b(?:appears|seems) to be (?:a |an )?(?:fictional|obscure|misspell|typo)|\bcould(?:n't| not) find\b|"
    r"\b(?:did you mean|perhaps you mean|may be a misspelling)\b", re.IGNORECASE)
# Recommended for Qwen3.6 without thinking, and fine for the smaller tiers; clients cannot change them.
SAMPLING = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5,
            "chat_template_kwargs": {"enable_thinking": False}}


def _as_number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _member_threads(app: Dict[str, Any], hardware: Dict[str, Any]) -> int:
    """The CPU threads a pool member runs its model on: what it reports (``app.threads``), or for a worker
    that predates the field, what it would have chosen for itself (engine/app_member.default_threads)."""
    threads = app.get("threads")
    if isinstance(threads, int) and not isinstance(threads, bool) and 1 <= threads <= 256:
        return threads
    from engine.app_member import default_threads      # imported here: app_member imports this module
    return default_threads(int(_as_number(hardware.get("cpu_cores"))))


class Member:
    """One machine able to answer phone requests."""

    def __init__(self, key: str, ram_gb: float, serving: Optional[str], ready: bool,
                 slots: int, base_url: Optional[str], local: bool = False, threads: int = 0) -> None:
        self.key = key
        self.ram_gb = ram_gb
        self.serving = serving
        self.ready = ready
        self.slots = max(1, slots)
        self.base_url = base_url
        self.local = local
        self.threads = max(0, int(threads))     # CPU threads it runs the phone model on (0: not known)


class AppPool:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tiers: List[Dict[str, Any]] = []
        self._inflight: Dict[str, int] = {}
        self._speeds: Deque[Tuple[float, float]] = deque(maxlen=50)
        self.throttle_window = UsageWindow()
        self._active_by_network: Dict[str, int] = {}
        self.local: Optional["LocalAppServer"] = None

    # ------------------------------------------------------------------ tiers
    def load_tiers(self, tiers: List[Dict[str, Any]]) -> None:
        self.tiers = [t for t in tiers if isinstance(t, dict) and t.get("id")]

    def _tier_index(self, tier_id: Optional[str]) -> int:
        for i, tier in enumerate(self.tiers):
            if tier["id"] == tier_id:
                return i
        return -1

    def capable_index(self, ram_gb: float) -> int:
        """The biggest tier a member lending ``ram_gb`` can host, or -1."""
        best = -1
        for i, tier in enumerate(self.tiers):
            if ram_gb >= _as_number(tier.get("min_ram_gb")):
                best = i
        return best

    def active_index(self, members: List[Member], min_members: int) -> int:
        """The biggest tier the coordinator's own member or at least ``min_members`` members
        can host (the smallest tier when none qualifies, so the pool always has a model)."""
        if not self.tiers:
            return -1
        active = 0
        for i in range(len(self.tiers)):
            able = [m for m in members if self.capable_index(m.ram_gb) >= i]
            if len(able) >= max(1, min_members) or any(m.local for m in able):
                active = i
        return active

    def assignment(self, ram_gb: float, members: List[Member], min_members: int) -> Optional[str]:
        """Which tier a member lending ``ram_gb`` should serve now (None: it can host none)."""
        capable = self.capable_index(ram_gb)
        if capable < 0:
            return None
        index = min(capable, self.active_index(members, min_members))
        return self.tiers[index]["id"] if index >= 0 else None

    # ------------------------------------------------------------------ members
    def members(self, registry, relay_lookup) -> List[Member]:
        found: List[Member] = []
        for record in registry.app_members():
            hw = record.get("hardware", {}) if isinstance(record.get("hardware"), dict) else {}
            app = hw.get("app") if isinstance(hw.get("app"), dict) else {}
            relay = relay_lookup(str(record.get("ip")), int(record.get("port", 0) or 0))
            found.append(Member(
                key=f"worker:{record.get('worker_id')}",
                ram_gb=_as_number(hw.get("ram_gb")),
                serving=app.get("model") if isinstance(app.get("model"), str) else None,
                ready=app.get("ready") is True and relay is not None,
                slots=int(_as_number(app.get("slots")) or 1),
                base_url=f"http://127.0.0.1:{relay}" if relay else None,
                threads=_member_threads(app, hw),
            ))
        if self.local is not None:
            found.append(self.local.as_member())
        return found

    def pick(self, members: List[Member], active_id: Optional[str], exclude: List[str]) -> Optional[Member]:
        """The least busy ready member, preferring the unlocked tier, then the biggest below it."""
        ready = [m for m in members if m.ready and m.base_url and m.key not in exclude]
        if not ready:
            return None
        order = {t["id"]: i for i, t in enumerate(self.tiers)}
        active = order.get(active_id, -1)
        with self._lock:
            def rank(m: Member):
                tier = order.get(m.serving, -1)
                load = self._inflight.get(m.key, 0) / m.slots
                full = load >= 1.0
                # Free capacity first; within that the unlocked tier, then bigger tiers; then load.
                return (full, 0 if tier == active else 1, -tier, load)
            return min(ready, key=rank)

    # ------------------------------------------------------------------ throttle
    def admit(self, network: str, per_minute: int) -> Tuple[bool, str]:
        """One answer at a time per network, and ``per_minute`` requests a minute."""
        with self._lock:
            if self._active_by_network.get(network, 0) >= 1:
                return False, "One answer at a time: wait for the current one to finish."
        allowed, _remaining, reset_at = self.throttle_window.check_and_record(network, per_minute, 60.0)
        if not allowed:
            wait = max(1, int(reset_at - time.time()))
            return False, f"Slow down a little: try again in {wait} seconds."
        with self._lock:
            self._active_by_network[network] = self._active_by_network.get(network, 0) + 1
        return True, ""

    def retry_after(self, network: str, per_minute: int) -> int:
        """Seconds a refused network should wait before asking again (the Retry-After header)."""
        with self._lock:
            if self._active_by_network.get(network, 0) >= 1:
                return 3
        _remaining, reset_at = self.throttle_window.peek(network, per_minute, 60.0)
        return max(1, math.ceil(reset_at - time.time())) if reset_at else 1

    def finished(self, network: str) -> None:
        with self._lock:
            left = self._active_by_network.get(network, 0) - 1
            if left > 0:
                self._active_by_network[network] = left
            else:
                self._active_by_network.pop(network, None)

    # ------------------------------------------------------------------ forwarding
    async def stream(self, body: Dict[str, Any], members: List[Member], active_id: Optional[str],
                     client, tools: Optional[List[Dict[str, Any]]] = None,
                     run_tool: Optional[Callable[..., Awaitable[Any]]] = None,
                     today: Optional[str] = None) -> AsyncIterator[bytes]:
        """
        Answer one phone request: yield SSE bytes for the phone. The member is sent a request
        built here, never the client's body (only its messages and max_tokens are read), so
        nothing a client sends can lift the output cap or change the instructions.

        With ``tools`` and ``run_tool`` the model may look things up: each call runs here as
        ``await run_tool(name, arguments, first_n=k)``, which returns a result with ok, reason,
        note, sources (n, title, url, host, age; numbered from k so sources stay unique across
        lookups) and context_text (engine/search.py). Every "sources" event carries the whole
        list so far.
        """
        began = time.monotonic()
        lookups = bool(tools) and run_tool is not None
        convo = [{"role": "system", "content": _system_prompt(lookups, today)}] + _client_messages(body)
        request = {**SAMPLING, "stream": True, "max_tokens": _output_cap(body.get("max_tokens"))}
        sources: List[Dict[str, Any]] = []
        rounds = 0
        question = _last_user_text(convo)
        fresh = lookups and bool(FRESH_FACTS.search(question) or NAMED_PERSON.search(question))
        while True:
            payload = {**request, "messages": convo}
            required = lookups and rounds == 0 and fresh
            if lookups and rounds < MAX_TOOL_ROUNDS:
                payload["tools"] = tools
                if required:
                    payload["tool_choice"] = "required"
                    if _has_tool(tools, "web_search"):
                        payload["max_tokens"] = min(payload["max_tokens"], HELD_PASS_TOKENS)
            calls: List[Dict[str, Any]] = []
            # A required lookup can come after the model has talked to itself (and made up an
            # answer, in the 2.3.2 smoke test): that text is held back and dropped if the pass
            # ends in a lookup, so the phone only ever sees the answer that follows it.
            watch = lookups and rounds == 0 and not required and _has_tool(tools, "web_search")
            one_pass = self._one_pass(payload, members, active_id, client, citations=len(sources), hold=required,
                                      stop_if=_hedges if watch else None)
            held_answer: Optional[List[bytes]] = None
            hedged = False
            try:
                async for kind, value in one_pass:
                    if kind == "bytes":
                        yield value
                    elif kind == "tool_calls":
                        calls = value[:MAX_CALLS_PER_ROUND]
                        if required:   # a call the held pass's cap cut off has no arguments
                            calls = [call for call in calls if call["arguments"]]
                    elif kind == "held":
                        held_answer = value
                    elif kind == "hedged":
                        hedged = True
                    else:  # "failed": the notice says why
                        yield _sse_notice(value)
                        return
            finally:
                await one_pass.aclose()
            if required and not calls:
                if _has_tool(tools, "web_search"):
                    # Told a lookup was required, the model answered from memory anyway (2.3.3:
                    # a made-up Super Bowl winner and a fake ESPN link; llama-server does not
                    # enforce tool_choice for every chat format). Its text is dropped and the
                    # question itself is searched; the model then answers from the results.
                    calls = [_question_search(convo, "call_required")]
                elif held_answer is not None:
                    for chunk_bytes in held_answer:
                        yield chunk_bytes
            if hedged:
                # The model said it can't know (HEDGES): the phone drops what it was shown so far
                # and the question is searched, as if the user had asked for that.
                yield _sse_event({"event": "retract"})
                calls = [_question_search(convo, "call_unsure")]
            if not calls:
                self._note_speed(began)
                yield b"data: [DONE]\n\n"
                return
            rounds += 1
            replies = []
            for call in calls:
                yield _sse_event({"event": "searching", "tool": call["name"], "query": _call_label(call)})
                result = await run_tool(call["name"], call["arguments"], first_n=len(sources) + 1)
                if getattr(result, "ok", False):
                    sources += [_source_fields(s) for s in result.sources]
                    yield _sse_event({"event": "sources", "sources": sources})
                    if getattr(result, "note", None):
                        yield _sse_event({"event": "search_note", "note": result.note})
                    replies.append(result.context_text)
                else:
                    reason = getattr(result, "reason", None) or "Search is unavailable right now."
                    yield _sse_event({"event": "search_unavailable", "reason": reason})
                    replies.append(f"Lookup unavailable ({reason}). Answer from your own knowledge and say "
                                   "briefly that the answer may be out of date.")
            convo = convo + [{"role": "assistant", "content": "", "tool_calls": [
                {"id": call["id"], "type": "function",
                 "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}} for call in calls]}]
            convo += [{"role": "tool", "tool_call_id": call["id"], "content": reply} for call, reply in zip(calls, replies)]

    async def _one_pass(self, payload: Dict[str, Any], members: List[Member], active_id: Optional[str],
                        client, citations: int = 0, hold: bool = False,
                        stop_if: Optional[Callable[[str], bool]] = None) -> AsyncIterator[Tuple[str, Any]]:
        """
        One request to a member, moving to another if it fails before the phone has been sent
        any text (up to MAX_ATTEMPTS). Yields ("bytes", sse) to pass on as they arrive, then
        ("tool_calls", [{"id", "name", "arguments"}]) when the model asked for lookups, or
        ("failed", notice).
        Only complete SSE events are passed on, so a member dying mid-event cannot leave half
        a line for the notice to be glued onto. With no sources (``citations`` 0), citation
        markers the model writes out of habit are taken out of the text (_PhantomCitations).
        With ``hold`` nothing is passed on: text before a lookup is dropped, and an answer
        that ends without one comes back as ("held", [sse...]) for the caller to judge.
        With ``stop_if``, the text passed on so far is shown to it after each piece; once it
        says yes the member is let go mid-answer (closing the request stops the model) and the
        pass ends with ("hedged", text).
        """
        tried: List[str] = []
        for _attempt in range(MAX_ATTEMPTS):
            member = self.pick(members, active_id, tried)
            if member is None:
                break
            tried.append(member.key)
            with self._lock:
                self._inflight[member.key] = self._inflight.get(member.key, 0) + 1
            started = False
            calls: Dict[int, Dict[str, str]] = {}
            phantom = _PhantomCitations() if citations == 0 else None
            held: List[bytes] = []
            shown = ""
            try:
                async with client.stream("POST", f"{member.base_url}/v1/chat/completions", json=payload,
                                         timeout=_timeout(client)) as resp:
                    if 400 <= resp.status_code < 500:
                        # The request itself is wrong (most often: longer than the model's context);
                        # another member would refuse it the same way.
                        log_warning(f"CPU pool member {member.key} refused a request: HTTP {resp.status_code}")
                        yield "failed", "[This conversation is too long for the phone model. Please start a new chat.]"
                        return
                    if resp.status_code != 200:
                        raise RuntimeError(f"member answered HTTP {resp.status_code}")
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            choice = chunk["choices"][0]
                        except (ValueError, KeyError, IndexError, TypeError):
                            continue
                        if "model" in chunk or "timings" in chunk:
                            # The member's model file path (its home directory) and its timings
                            # are not the phone's business.
                            chunk.pop("model", None)
                            chunk.pop("timings", None)
                            data = json.dumps(chunk)
                        delta = choice.get("delta") or {}
                        if delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"):
                            # One frame of generated text is one token, counted as it arrives (engine/throughput.py)
                            # whether or not the phone is ever shown it (a held or retracted pass still cost the pool).
                            throughput.record(POOL_PHONE, 1)
                        if delta.get("tool_calls"):
                            for part in delta["tool_calls"]:
                                slot = calls.setdefault(int(part.get("index", 0)), {"id": "", "name": "", "arguments": ""})
                                slot["id"] = part.get("id") or slot["id"]
                                function = part.get("function") or {}
                                slot["name"] += function.get("name") or ""
                                slot["arguments"] += function.get("arguments") or ""
                            continue
                        if choice.get("finish_reason") == "tool_calls":
                            continue
                        if phantom is not None:
                            if isinstance(delta.get("content"), str):
                                kept = phantom.feed(delta["content"])
                                if kept != delta["content"]:      # untouched chunks pass as they came
                                    delta["content"] = kept
                                    data = json.dumps(chunk)
                            if choice.get("finish_reason") and phantom.held:
                                if hold:
                                    held.append(_content_chunk(phantom.flush()))
                                else:
                                    yield "bytes", _content_chunk(phantom.flush())
                        if hold:
                            held.append(f"data: {data}\n\n".encode())
                            continue
                        started = started or bool(delta.get("content"))
                        yield "bytes", f"data: {data}\n\n".encode()
                        if stop_if is not None and isinstance(delta.get("content"), str):
                            shown += delta["content"]
                            if stop_if(shown):
                                yield "hedged", shown
                                return
                if phantom is not None and phantom.held:
                    if hold:
                        held.append(_content_chunk(phantom.flush()))
                    else:
                        yield "bytes", _content_chunk(phantom.flush())
                if hold and not calls:
                    yield "held", held
                if calls:
                    yield "tool_calls", [{"id": c["id"] or f"call_{i}", "name": c["name"],
                                          "arguments": _arguments_from(c["arguments"])}
                                         for i, c in (sorted(calls.items()))]
                return
            except Exception as exc:  # noqa: BLE001 - any member failure is handled the same way
                if started:
                    log_warning(f"CPU pool member {member.key} dropped mid-answer: {type(exc).__name__}")
                    yield "failed", "[The machine answering this dropped out. Please send your message again.]"
                    return
                log_warning(f"CPU pool member {member.key} failed before answering ({type(exc).__name__}); trying another.")
            finally:
                with self._lock:
                    left = self._inflight.get(member.key, 1) - 1
                    if left > 0:
                        self._inflight[member.key] = left
                    else:
                        self._inflight.pop(member.key, None)
        yield "failed", "[No machine in the CPU pool is free right now. Please try again in a moment.]"

    def _note_speed(self, began: float) -> None:
        with self._lock:
            self._speeds.append((time.time(), time.monotonic() - began))

    # ------------------------------------------------------------------ status
    def status(self, members: List[Member], min_members: int) -> Dict[str, Any]:
        active = self.active_index(members, min_members)
        with self._lock:
            busy = sum(self._inflight.values())
            recent = [s for t, s in self._speeds if t > time.time() - 900]
        tiers = []
        for i, tier in enumerate(self.tiers):
            capable = sum(1 for m in members if self.capable_index(m.ram_gb) >= i)
            tiers.append({"id": tier["id"], "name": tier.get("name"), "min_ram_gb": tier.get("min_ram_gb"),
                          "members_able": capable, "unlocked": i <= active})
        return {
            "members": len(members),
            "members_ready": sum(1 for m in members if m.ready),
            "active_tier": self.tiers[active]["id"] if active >= 0 else None,
            "active_model": self.tiers[active].get("name") if active >= 0 else None,
            "tiers": tiers,
            "requests_in_flight": busy,
            "median_answer_seconds": round(sorted(recent)[len(recent) // 2], 1) if recent else None,
        }


def _timeout(client):
    # read: the longest a member may stay silent, before its first byte (reading the prompt
    # on a CPU) or between chunks, before it is given up on.
    import httpx
    return httpx.Timeout(600.0, connect=5.0, read=FIRST_BYTE_TIMEOUT_SECONDS)


def _sse_notice(text: str) -> bytes:
    chunk = {"choices": [{"index": 0, "delta": {"content": f"\n\n{text}"}, "finish_reason": "stop"}]}
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()


def _content_chunk(text: str) -> bytes:
    chunk = {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
    return f"data: {json.dumps(chunk)}\n\n".encode()


class _PhantomCitations:
    """
    Takes "[1]"-style markers out of an answer that has no sources: models cite out of habit,
    and a marker pointing at nothing reads like a broken link. Streamed text is filtered as it
    passes; a possibly unfinished marker at the end of a piece ("[", "[1") is held back until
    the next one. Code is left alone (inside ``` fences, or right after a name or bracket, as in
    list[1]), and so are bracketed numbers of 3+ digits, like years.
    """

    # " [1]" goes with the space before it; "[1]" alone only where it cannot be an index.
    MARKER = re.compile(r"(?:\s+|(?<![\w\])`]))\[\d{1,2}(?:\s*,\s*\d{1,2})*\]")
    # Held back: an unclosed " [1" (the marker may finish in the next piece). Qwen's tokenizer
    # sends " [" as one token, so the space before a marker travels with it.
    UNFINISHED = re.compile(r"\s*\[[\d,\s]{0,8}$")

    def __init__(self) -> None:
        self.held = ""
        self.in_code = False

    def feed(self, text: str) -> str:
        text, self.held = self.held + text, ""
        tail = self.UNFINISHED.search(text)
        if tail:
            text, self.held = text[:tail.start()], text[tail.start():]
        out = []
        for i, part in enumerate(text.split("```")):
            if i:
                self.in_code = not self.in_code
                out.append("```")
            out.append(part if self.in_code else self.MARKER.sub("", part))
        return "".join(out)

    def flush(self) -> str:
        held, self.held = self.held, ""
        return held


def _sse_event(event: Dict[str, Any]) -> bytes:
    """An in-band event for the phone app (no "choices", so OpenAI clients skip it)."""
    return f"data: {json.dumps({'dlne': event})}\n\n".encode()


def _system_prompt(lookups: bool, today: Optional[str]) -> str:
    today = today or time.strftime("%A, %d %B %Y")
    return SYSTEM_PROMPT.format(today=today) + (SEARCH_INSTRUCTIONS if lookups else NO_SEARCH_NOTE)


def _client_messages(body: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    The conversation as the phone sent it, reduced to what a phone may send: user and
    assistant turns with text content, the newest HISTORY_CHARS of them (a CPU reads every
    token before its first word, and an over-long chat would not fit the model's context).
    """
    kept: List[Dict[str, str]] = []
    budget = HISTORY_CHARS
    for message in reversed(body.get("messages") or []):
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if kept and len(content) > budget:
            break
        content = content[-budget:] if not kept else content
        kept.append({"role": message["role"], "content": content})
        budget -= len(content)
        if budget <= 0:
            break
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return kept


def _has_tool(tools: Optional[List[Dict[str, Any]]], name: str) -> bool:
    return any(isinstance(t, dict) and (t.get("function") or {}).get("name") == name for t in tools or ())


def _last_user_text(convo: List[Dict[str, Any]]) -> str:
    for message in reversed(convo):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _question_search(convo: List[Dict[str, Any]], call_id: str) -> Dict[str, Any]:
    """A web search for the user's own words, made when the model should have made one."""
    return {"id": call_id, "name": "web_search", "arguments": {"query": _last_user_text(convo)[:200]}}


def _hedges(text: str) -> bool:
    """True when answer text says it can't know (HEDGES), outside code blocks."""
    prose = "".join(part for i, part in enumerate(text.split("```")) if i % 2 == 0)
    return bool(HEDGES.search(prose.replace("’", "'")))


def _output_cap(asked: Any) -> int:
    """max_tokens as the phone asked, within 1..MAX_OUTPUT_TOKENS; anything odd is the cap."""
    if isinstance(asked, bool):
        return MAX_OUTPUT_TOKENS
    try:
        value = float(asked)
    except (TypeError, ValueError):
        return MAX_OUTPUT_TOKENS
    if not math.isfinite(value) or value < 1:
        return MAX_OUTPUT_TOKENS
    return min(int(value), MAX_OUTPUT_TOKENS)


def _arguments_from(raw: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _call_label(call: Dict[str, Any]) -> str:
    """What the phone is told is being looked up: the call's query or location, shortened."""
    args = call.get("arguments") or {}
    for key in ("query", "location"):
        if isinstance(args.get(key), str) and args[key].strip():
            return args[key].strip()[:120]
    return ""


def _source_fields(source: Any) -> Dict[str, Any]:
    get = source.get if isinstance(source, dict) else (lambda key: getattr(source, key, None))
    return {key: get(key) for key in ("n", "title", "url", "host", "age")}


# ======================================================================================
# The coordinator host's own member
# ======================================================================================

def sha256_ok(path: str, expected: str, size: int) -> bool:
    """The file has exactly ``size`` bytes and ``expected`` SHA-256. A marker beside it
    remembers a good check (keyed by size and mtime), so an 18 GB model is hashed once."""
    try:
        stat = os.stat(path)
    except OSError:
        return False
    if stat.st_size != size:
        return False
    marker = f"{path}.dlne-sha256"
    stamp = f"{expected} {stat.st_size} {int(stat.st_mtime)}"
    try:
        with open(marker, "r", encoding="utf-8") as handle:
            if handle.read().strip() == stamp:
                return True
    except OSError:
        pass
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(8 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        return False
    try:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(stamp)
    except OSError:
        pass
    return True


def app_server_args(binary: str, model_path: str, port: int, threads: int, ctx: int = 16384,
                    slots: int = 2, cpu_mask: Optional[str] = None) -> List[str]:
    """llama-server for a CPU pool member: CPU only (never a GPU), loopback only, no web page,
    and never more than MAX_OUTPUT_TOKENS per answer whatever a request asks for."""
    args = [binary, "-m", model_path, "--host", "127.0.0.1", "--port", str(port),
            "-c", str(ctx), "-np", str(slots), "-t", str(threads), "-tb", str(threads),
            "-n", str(MAX_OUTPUT_TOKENS), "--device", "none", "-ngl", "0", "--jinja", "--no-webui"]
    if cpu_mask:
        # One thread pinned per logical CPU in the mask, for generation and prompt reading.
        args += ["--cpu-mask", cpu_mask, "--cpu-strict", "1",
                 "--cpu-mask-batch", cpu_mask, "--cpu-strict-batch", "1"]
    return args


class LocalAppServer:
    """The pool member on the coordinator host, inside the cores and RAM the owner allocated."""

    def __init__(self, models_dir: str, binary_resolver, log_dir: str, port: int, threads: int,
                 ram_gb: float, cpu_mask: Optional[str]) -> None:
        self.models_dir = models_dir
        self.binary_resolver = binary_resolver
        self.log_dir = log_dir
        self.port = port
        self.threads = threads
        self.ram_gb = ram_gb
        self.cpu_mask = cpu_mask
        self.process: Optional[subprocess.Popen] = None
        self.serving: Optional[str] = None
        self.ready = False
        self.problem: Optional[str] = None
        self._log = None
        # (path, size, mtime) of a model file that failed verification: not hashed again
        # (gigabytes, every 20 s) until the file changes.
        self._refused: Optional[Tuple[str, int, int]] = None

    def as_member(self) -> Member:
        alive = self.process is not None and self.process.poll() is None
        return Member("local", self.ram_gb, self.serving, self.ready and alive, 2,
                      f"http://127.0.0.1:{self.port}", local=True, threads=self.threads)

    def stop(self) -> None:
        proc, self.process, self.ready = self.process, None, False
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        log, self._log = self._log, None
        if log is not None:
            log.close()

    def ensure(self, tier: Optional[Dict[str, Any]]) -> None:
        """Serve ``tier`` (blocking: verification and start-up); restart if the process died."""
        if tier is None:
            if self.process is not None:
                self.stop()
            self.serving = None
            return
        alive = self.process is not None and self.process.poll() is None
        if alive and self.serving == tier["id"]:
            return
        # Everything the new tier needs is checked before the running server is stopped: a
        # missing or bad file must not take the phone app dark when the old model still works.
        path = os.path.join(self.models_dir, tier["file"])
        if not self._verified(path, tier):
            self.problem = f"{tier['file']} is missing from {self.models_dir} or does not match the signed release"
            log_error(f"CPU pool (local): {self.problem}.")
            return
        binary = self.binary_resolver()
        if not binary:
            self.problem = "no llama-server binary"
            log_error("CPU pool (local): no llama-server binary.")
            return
        self.stop()
        if self._log is None:
            os.makedirs(self.log_dir, exist_ok=True)
            self._log = open(os.path.join(self.log_dir, "app_server.log"), "a")
        args = app_server_args(binary, path, self.port, self.threads, cpu_mask=self.cpu_mask)
        env = dict(os.environ, GGML_VK_VISIBLE_DEVICES="")
        self.process = subprocess.Popen(args, stdout=self._log, stderr=subprocess.STDOUT, env=env)
        self.serving = tier["id"]
        self.problem = None
        log_coordinator(f"CPU pool (local): serving {tier.get('name')} on 127.0.0.1:{self.port} "
                        f"({self.threads} threads{', pinned to ' + self.cpu_mask if self.cpu_mask else ''}).")

    def _verified(self, path: str, tier: Dict[str, Any]) -> bool:
        try:
            stat = os.stat(path)
        except OSError:
            return False
        fingerprint = (path, stat.st_size, int(stat.st_mtime))
        if fingerprint == self._refused:
            return False
        if sha256_ok(path, tier["sha256"], int(tier["size"])):
            return True
        self._refused = fingerprint
        return False

    async def wait_ready(self, client, timeout: float = 300.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.process is None or self.process.poll() is not None:
                return False
            try:
                resp = await client.get(f"http://127.0.0.1:{self.port}/health", timeout=2.0)
                if resp.status_code == 200:
                    self.ready = True
                    return True
            except Exception:  # noqa: BLE001 - not up yet
                pass
            await asyncio.sleep(1.0)
        return False


app_pool = AppPool()
