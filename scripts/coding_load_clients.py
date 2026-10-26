"""
The fake extension of the load and soak test (scripts/coding_load.py): agents that talk to the coordinator the way
vscode-extension/src/client/dlneClient.ts does (docs/coding-members.md sections 5, 6 and 10), with no editor and no model.

An :class:`Agent` is one developer. It works in tasks: each task is a conversation with its own random
``X-DLNE-Session`` and a growing prompt, made of several model calls (the agent loop: call, run a tool, call again) with a
short gap between calls and a long one between tasks. Each call:

1. asks ``/v1/cluster/status`` once in a while whether the coordinator has coding members (``coding_members.enabled``);
2. when it has, sends ``X-DLNE-Privacy`` and asks ``/v1/coding/route`` before **every** attempt;
3. seals the body to the member's key (engine.e2e.seal_request) and sends ``X-DLNE-Route``; a sealed body is sent once;
4. reads the stream: ``dlne_queue`` frames (place in line) before the answer, sealed chunks through
   ``ResponseOpener``, complete only at the sealed end;
5. handles failure as the extension does: ``503``/``429`` with ``dlne_queue`` is "busy, wait Retry-After and ask again
   holding the ticket"; any other ``503`` is "the model is loading" (one second, then again); ``404/410/421/502/504`` from a
   routed request, or an answer that fails its checks before anything was shown, asks for a new route and seals again, twice
   at most, then plain; an answer cut after part of it was shown is an error; and after two seconds of silence it reads
   ``coding_queue`` from the status every four seconds to estimate the place.

Every call becomes one JSON line (``call``) with the timings the analysis needs; nothing here judges the result.
"""

import asyncio
import json
import math
import random
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

from coding_load_fakes import LagProbe, proc_stats

TIER_PRIORITY = {"key": 0, "gpu": 0, "cpu": 1, "trial": 2}
MEMBER_TRIES = 2                    # members tried before the coordinator's own engine in plain (DlneClient.MEMBER_TRIES)
SILENCE_BEFORE_POLL_S = 2.0         # WaitWatcher: silence before it asks the status
POLL_EVERY_S = 4.0
BUSY_GIVE_UP_S = 15 * 60.0

_WORDS = ("def self return import async await class for in if else None True False lambda yield const let var function "
          "export interface type string number boolean void Promise fn pub struct impl match Some Ok Err mut ref use "
          "mod trait where assert raise try except finally with as is not and or").split()
_RNG = random.Random(12345)
_CORPUS = " ".join(_RNG.choices(_WORDS, k=60000)) * 6      # ~2 MB of code-ish text to slice prompts from


def text_chars(n: int, salt: int) -> str:
    """``n`` characters of code-ish text (n up to ~1 MB), different for each ``salt``."""
    n = max(1, min(int(n), len(_CORPUS) // 2))
    start = (salt * 7919) % (len(_CORPUS) - n - 1)
    return _CORPUS[start:start + n]


class QueueFrame(Exception):
    pass


class MemberAnswerError(Exception):
    def __init__(self, message: str, partial: bool = False) -> None:
        super().__init__(message)
        self.partial = partial


def lognormal(rng: random.Random, median: float, sigma: float = 0.6) -> float:
    return median * math.exp(rng.gauss(0.0, sigma))


class LoadClient:
    """One process worth of agents, sharing HTTP clients, the status poll and the output file."""

    def __init__(self, base_url: str, spec: Dict[str, Any], out_path: str, t0: float, shard: int, seed: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.spec = spec
        self.agents_spec = spec["agents"]
        self.scale = float(spec.get("scale", 1.0))
        self.t0 = t0
        self.deadline = t0 + float(spec["duration_s"])
        self.hard_deadline = self.deadline + float(spec.get("grace_s", 60.0))
        self.shard = shard
        self.rng = random.Random(seed)
        self.out = open(out_path, "a", buffering=1 << 16, encoding="utf-8")
        self.members_enabled = False
        self.coding_queue: Dict[str, Any] = {}
        self.status_at = 0.0
        self.route_unsupported_until = 0.0
        self.http: Optional[httpx.AsyncClient] = None        # status, route (kept alive)
        self.chat_http: Optional[httpx.AsyncClient] = None   # one fresh connection per chat request, as the extension
        self.lag = LagProbe()
        self.counters: Dict[str, int] = {}
        self.done_agents = 0

    def count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def emit(self, row: Dict[str, Any]) -> None:
        self.out.write(json.dumps(row, separators=(",", ":")) + "\n")

    # ------------------------------------------------------------------ status
    async def poll_status(self) -> None:
        while True:
            try:
                reply = await self.http.get(f"{self.base_url}/v1/cluster/status", headers={"X-Forwarded-For": "198.51.100.250"})
                if reply.status_code == 200:
                    data = reply.json()
                    self.members_enabled = bool((data.get("coding_members") or {}).get("enabled"))
                    self.coding_queue = data.get("coding_queue") or {}
                    self.status_at = time.time()
            except Exception:  # noqa: BLE001 - the status is a convenience
                self.count("status_errors")
            await asyncio.sleep(5.0)

    async def status_now(self) -> Dict[str, Any]:
        try:
            reply = await self.http.get(f"{self.base_url}/v1/cluster/status", headers={"X-Forwarded-For": "198.51.100.250"})
            return reply.json() if reply.status_code == 200 else {}
        except Exception:  # noqa: BLE001
            return {}

    # ------------------------------------------------------------------ the run
    async def run(self) -> None:
        limits = httpx.Limits(max_connections=None, max_keepalive_connections=64, keepalive_expiry=30.0)
        self.http = httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(30.0))
        self.chat_http = httpx.AsyncClient(limits=httpx.Limits(max_connections=None, max_keepalive_connections=0),
                                           timeout=httpx.Timeout(None, connect=30.0))
        self.lag.start()
        status_task = asyncio.create_task(self.poll_status())
        sampler = asyncio.create_task(self._sample())
        try:
            await self.status_once()
            agents = [Agent(self, i) for i in self.assigned_agents()]
            await asyncio.gather(*(a.run() for a in agents))
        finally:
            status_task.cancel()
            sampler.cancel()
            self.emit({"k": "client_end", "shard": self.shard, "t": time.time(), "counters": self.counters,
                       "lag": self.lag.take(), "proc": proc_stats()})
            self.out.close()
            await self.http.aclose()
            await self.chat_http.aclose()

    async def status_once(self) -> None:
        data = await self.status_now()
        self.members_enabled = bool((data.get("coding_members") or {}).get("enabled"))
        self.coding_queue = data.get("coding_queue") or {}

    async def _sample(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            self.emit({"k": "client_sample", "shard": self.shard, "t": time.time(), "lag": self.lag.take(),
                       "proc": proc_stats()})

    def assigned_agents(self) -> List[int]:
        count, shards = int(self.agents_spec["count"]), int(self.spec.get("_shards", 1))
        return [i for i in range(count) if i % shards == self.shard]


class Agent:
    def __init__(self, client: LoadClient, index: int) -> None:
        self.c = client
        spec = client.agents_spec
        self.index = index
        self.rng = random.Random(client.rng.random() + index * 1009)
        self.tier = self._pick(spec["tiers"])
        self.privacy = self._pick(spec["privacy"])
        # A generic OpenAI-style client (a script, another editor): no X-DLNE-Session, no privacy header, no route lookup, no
        # sealing, and it treats any chunk without ``choices`` as a broken answer.
        self.generic = self.rng.random() < float(spec.get("generic_fraction", 0.0))
        self.ip = f"198.51.{100 + index // 250}.{1 + index % 250}"
        self.token = self._token(spec)
        self.sid = ""
        self.messages: List[Dict[str, str]] = []
        self.call_no = 0
        self.task_no = 0

    def _pick(self, weights: Dict[str, float]) -> str:
        names = list(weights)
        return self.rng.choices(names, weights=[weights[n] for n in names])[0]

    def _token(self, spec: Dict[str, Any]) -> Optional[str]:
        from coding_load_fakes import token_for
        if self.tier == "key":
            keys = spec.get("api_keys") or []
            return keys[self.index % len(keys)] if keys else None
        if self.tier == "cpu":
            workers = int(spec.get("cpu_contributors", 0))
            return token_for(f"fake-cpu-{self.index % max(1, workers):03d}") if workers else None
        if self.tier == "gpu":
            members = int(spec.get("gpu_members", 0))
            return token_for(f"fake-member-{self.index % max(1, members):03d}") if members else None
        return None

    def headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        out = {"X-Forwarded-For": self.ip, "Content-Type": "application/json"}
        if self.token:
            out["Authorization"] = f"Bearer {self.token}"
        if extra:
            out.update(extra)
        return out

    # ------------------------------------------------------------------ the work
    async def run(self) -> None:
        c, spec = self.c, self.c.agents_spec
        await asyncio.sleep(self.rng.random() * float(spec.get("ramp_s", 10.0)))
        while time.time() < c.deadline:
            self.task_no += 1
            self.sid = str(uuid.uuid4())
            self.call_no = 0
            self.messages = [{"role": "system", "content": text_chars(8000, self.index * 31 + self.task_no)},
                             {"role": "user", "content": text_chars(int(spec.get("ptok0", 3000)) * 4,
                                                                      self.index * 17 + self.task_no)}]
            calls = max(1, round(self.rng.expovariate(1.0 / float(spec.get("calls_per_task", 8)))))
            for _ in range(calls):
                if time.time() >= c.deadline:
                    break
                await self.call()
                await asyncio.sleep(lognormal(self.rng, float(spec.get("tool_gap_s", 2.0)) / c.scale))
            await asyncio.sleep(lognormal(self.rng, float(spec.get("user_gap_s", 40.0)) / c.scale))
        c.done_agents += 1

    async def call(self) -> None:
        c, spec = self.c, self.c.agents_spec
        self.call_no += 1
        step = int(spec.get("ptok_step", 1200))
        otok = int(self.rng.uniform(*spec.get("otok", [60, 600])))
        if sum(len(m["content"]) for m in self.messages) // 4 > int(spec.get("ptok_max", 40000)):
            # The agent compacts a conversation that has outgrown the context: the system prompt, a summary, the latest turn.
            self.messages = self.messages[:1] + [{"role": "user", "content": text_chars(int(spec.get("ptok0", 3000)) * 4,
                                                                                         self.index * 13 + self.call_no)}]
            self.c.count("compactions")
        if self.call_no > 1:
            self.messages.append({"role": "assistant", "content": text_chars(otok * 4, self.index + self.call_no)})
            self.messages.append({"role": "user",
                                  "content": text_chars(int(step * self.rng.uniform(0.5, 1.5)) * 4, self.index * 3 + self.call_no)})
        rid = f"a{self.index:03d}-t{self.task_no}-c{self.call_no}"
        record: Dict[str, Any] = {"k": "call", "rid": rid, "agent": self.index, "sid": self.sid, "tier": self.tier,
                                  "generic": self.generic, "priv": self.privacy, "task": self.task_no, "call": self.call_no, "t_call": time.time(),
                                  "otok": otok, "attempts": [], "frames": [], "polls": [], "route_ms": []}
        record["ptok"] = sum(len(m["content"]) // 4 + 4 for m in self.messages)
        cancel_after = None
        if self.rng.random() < float(spec.get("cancel_prob", 0.0)):
            cancel_after = self.rng.uniform(*spec.get("cancel_after_s", [1.0, 20.0])) / c.scale
        task = asyncio.create_task(self._call_body(rid, otok, record))
        timer = None
        if cancel_after is not None:
            timer = asyncio.get_running_loop().call_later(cancel_after, self._cancel, task, record)
        try:
            await task
        except asyncio.CancelledError:
            record["outcome"] = "cancelled"
            c.count("cancelled")
        except Exception as exc:  # noqa: BLE001 - a call that raised is data, not a crash of the test
            record["outcome"] = "exception"
            record["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            c.count("exceptions")
        finally:
            if timer is not None:
                timer.cancel()
        record["t_end"] = time.time()
        record.setdefault("outcome", "unknown")
        # an assistant turn keeps the conversation's prefix stable for the next call
        c.emit(record)

    @staticmethod
    def _cancel(task: "asyncio.Task", record: Dict[str, Any]) -> None:
        if not task.done():
            record["t_cancel"] = time.time()
            task.cancel()

    # ------------------------------------------------------------------ one call, with the extension's retry logic
    async def _call_body(self, rid: str, otok: int, record: Dict[str, Any]) -> None:
        c = self.c
        member_failures = 0
        network_retries = 0
        ticket: Optional[str] = None
        busy_since: Optional[float] = None
        for attempt_no in range(180):
            membership = c.members_enabled and not self.generic
            force_plain = member_failures >= MEMBER_TRIES
            route = pubkey = None
            if membership and not force_plain and time.time() >= c.route_unsupported_until:
                route, pubkey = await self.lookup_route(record)
            body: Dict[str, Any] = {
                "model": self.c.spec.get("client_model", "Qwen3.6-35B-A3B-UD-IQ3_S.gguf"), "messages": self.messages, "stream": True, "max_tokens": otok, "temperature": 0.2,
                "x_load": {"rid": rid, "sid": self.sid, "turn": self.call_no, "tier": self.tier,
                           "sent": time.time(), "otok": otok, "attempt": attempt_no}}
            headers = self.headers({"Accept": "text/event-stream"})
            if not self.generic:
                headers["X-DLNE-Session"] = self.sid
            if membership:
                headers["X-DLNE-Privacy"] = self.privacy
            if ticket:
                headers["X-DLNE-Ticket"] = ticket
            resp_key = None
            payload: Any = body
            if route:
                from engine import e2e
                payload, resp_key = e2e.seal_request(body, pubkey)
                headers["X-DLNE-Route"] = route
            info: Dict[str, Any] = {"n": attempt_no, "route": bool(route), "t0": time.time()}
            record["attempts"].append(info)
            try:
                result = await self.attempt(headers, payload, resp_key, record, info)
            except (httpx.TransportError, OSError) as exc:
                info.update(kind="socket", error=type(exc).__name__, t1=time.time())
                if network_retries < 4:
                    network_retries += 1
                    await asyncio.sleep(1.0 * network_retries)
                    continue
                record["outcome"] = "failed"
                return
            except MemberAnswerError as exc:
                info.update(kind="member_answer_error", partial=exc.partial, t1=time.time())
                if route and member_failures < MEMBER_TRIES and not exc.partial:
                    member_failures += 1
                    self.c.count("reroutes")
                    continue
                record["outcome"] = "cut" if exc.partial else "failed"
                return
            info["t1"] = time.time()
            kind = result["kind"]
            info["kind"] = kind
            info["status"] = result.get("status")
            if kind == "ok":
                record["outcome"] = result["outcome"]
                return
            if kind == "busy":
                busy_since = busy_since or time.time()
                if time.time() - busy_since > BUSY_GIVE_UP_S:
                    record["outcome"] = "gave_up"
                    return
                ticket = result.get("ticket") or ticket
                record["frames"].append([time.time(), result.get("position"), result.get("waiting"), result.get("slots"),
                                         result.get("in_use"), result.get("eta"), "response"])
                await asyncio.sleep(min(15.0, max(1.0, result.get("retry_after") or 3.0)))
                continue
            if kind == "loading":
                self.c.count("loading_503")
                await asyncio.sleep(1.0)
                await c.status_once()
                continue
            if kind == "member_failure":
                if route and member_failures < MEMBER_TRIES:
                    member_failures += 1
                    self.c.count("reroutes")
                    continue
                record["outcome"] = "failed"
                record["error"] = f"member failure {result.get('status')}"
                return
            record["outcome"] = "refused"
            record["status"] = result.get("status")
            return
        record["outcome"] = "gave_up"

    async def lookup_route(self, record: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        c = self.c
        t = time.time()
        try:
            reply = await c.http.get(f"{c.base_url}/v1/coding/route", params={"session": self.sid, "privacy": self.privacy},
                                     headers=self.headers(), timeout=1.5 + 3.0)
        except Exception:  # noqa: BLE001 - slow or broken: the plain path
            record["route_ms"].append(round((time.time() - t) * 1000, 1))
            c.count("route_errors")
            return None, None
        record["route_ms"].append(round((time.time() - t) * 1000, 1))
        if reply.status_code in (404, 405, 501):
            c.route_unsupported_until = time.time() + 600.0
            return None, None
        if reply.status_code != 200:
            c.count("route_errors")
            return None, None
        try:
            data = reply.json()
        except ValueError:
            return None, None
        if not data.get("route"):
            c.count("route_null")
            return None, None
        c.count("route_offered")
        return data["route"], data.get("pubkey")

    async def attempt(self, headers: Dict[str, str], payload: Any, resp_key: Optional[bytes], record: Dict[str, Any],
                      info: Dict[str, Any]) -> Dict[str, Any]:
        c = self.c
        opener = None
        if resp_key is not None:
            from engine import e2e
            opener = e2e.ResponseOpener(resp_key)
        started = False
        done_seen = False
        sealed_frames = plain_frames = 0
        tokens = 0
        silence = asyncio.create_task(self._watch_silence(record))
        try:
            async with c.chat_http.stream("POST", f"{c.base_url}/v1/chat/completions", headers=headers,
                                          content=json.dumps(payload).encode()) as reply:
                info["status"] = reply.status_code
                if reply.status_code != 200:
                    text = (await reply.aread()).decode("utf-8", "replace")
                    return self._refusal(reply, text, bool(opener is not None or headers.get("X-DLNE-Route")))
                async for line in reply.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue
                    if line == "data: [DONE]":
                        done_seen = True
                        continue
                    if not line.startswith("data: "):
                        continue
                    try:
                        parsed = json.loads(line[6:])
                    except ValueError:
                        continue
                    if isinstance(parsed, dict) and "dlne_queue" in parsed:
                        if self.generic:
                            record["generic_frames"] = record.get("generic_frames", 0) + 1     # it would read this as a broken answer
                            continue
                        q = parsed["dlne_queue"] if isinstance(parsed["dlne_queue"], dict) else {}
                        if not started:
                            record["frames"].append([time.time(), q.get("position"), q.get("waiting"), q.get("slots"),
                                                     q.get("in_use"), q.get("eta_seconds"), "stream"])
                        silence.cancel()
                        continue
                    if opener is not None:
                        sealed = parsed.get("dlne_e2e") if isinstance(parsed, dict) else None
                        if sealed is None:
                            if isinstance(parsed, dict) and "choices" in parsed:
                                plain_frames += 1
                            continue
                        try:
                            raw = opener.open(sealed)
                        except Exception as exc:  # noqa: BLE001
                            raise MemberAnswerError("integrity: " + type(exc).__name__, partial=started)
                        sealed_frames += 1
                        if raw is None:
                            continue                       # the sealed end
                        try:
                            parsed = json.loads(raw)
                        except ValueError:
                            continue
                    choices = parsed.get("choices") if isinstance(parsed, dict) else None
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content") or ""
                    if not started:
                        started = True
                        silence.cancel()
                    if content:
                        if "t_first" not in record:
                            record["t_first"] = time.time()
                        tokens += 1
                    xl = parsed.get("x_load")
                    if isinstance(xl, dict) and "served_by" not in record:
                        record["served_by"] = xl.get("member")
                        record["recv"] = xl.get("recv")
                        record["start"] = xl.get("start")
                        record["cached"] = xl.get("cached")
                        record["served_rid"] = xl.get("rid")
        finally:
            silence.cancel()
        record["events"] = tokens
        if opener is not None:
            if sealed_frames == 0 and plain_frames > 0:
                raise MemberAnswerError("answered in the clear", partial=False)
            if not opener.finished:
                raise MemberAnswerError("cut before the sealed end", partial=started)
            outcome = "ok"
        else:
            outcome = "ok" if done_seen else "truncated_plain"
        if "served_rid" in record and record["served_rid"] != record["rid"]:
            record["wrong_rid"] = True
        return {"kind": "ok", "outcome": outcome}

    @staticmethod
    def _refusal(reply: httpx.Response, text: str, routed: bool) -> Dict[str, Any]:
        status = reply.status_code
        queue = None
        if status in (503, 429):
            try:
                body = json.loads(text)
                if isinstance(body, dict) and isinstance(body.get("dlne_queue"), dict):
                    queue = body["dlne_queue"]
            except ValueError:
                queue = None
        if queue is not None:
            try:
                retry_after = float(reply.headers.get("retry-after", ""))
            except ValueError:
                retry_after = None
            return {"kind": "busy", "status": status, "retry_after": retry_after, "ticket": queue.get("ticket"),
                    "position": queue.get("position"), "waiting": queue.get("waiting"), "slots": queue.get("slots"),
                    "in_use": queue.get("in_use"), "eta": queue.get("eta_seconds")}
        if status == 503:
            return {"kind": "loading", "status": status}
        if routed and status in (404, 410, 421, 502, 504):
            return {"kind": "member_failure", "status": status}
        return {"kind": "refused", "status": status}

    async def _watch_silence(self, record: Dict[str, Any]) -> None:
        """The extension's WaitWatcher: after two seconds of silence read ``coding_queue`` every four seconds."""
        c = self.c
        try:
            await asyncio.sleep(SILENCE_BEFORE_POLL_S)
            while True:
                data = await c.status_now()
                queue = data.get("coding_queue") or {}
                by = queue.get("waiting_by_priority") or {}
                mine = TIER_PRIORITY.get(self.tier, 2)
                if queue.get("in_use", 0) >= queue.get("slots", 1) and queue.get("waiting", 0) > 0:
                    place = sum(v for k, v in by.items() if int(k) < mine) + by.get(str(mine), 0)
                    record["polls"].append([time.time(), place, queue.get("waiting"), queue.get("slots"), queue.get("in_use")])
                await asyncio.sleep(POLL_EVERY_S)
        except asyncio.CancelledError:
            pass


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return rows
