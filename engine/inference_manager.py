"""
DLNE inference process manager.

Owns the lifecycle of the local ``llama-server`` child processes: which models are
running, which TCP port each one listens on, which RPC worker endpoints each one was
sharded across, and how much of the cluster's memory pool each one has reserved.

Four properties of this module are security- or correctness-critical and are called out
where they are implemented:

* ``resolve_model_path`` is the only thing standing between a caller-supplied ``model``
  string (which arrives straight off the public ``/v1/chat/completions`` body) and a
  ``llama-server -m <path>`` subprocess argument. It is a containment check, not a
  convenience helper.
* Port allocation must never hand out a port another process already holds, because
  ``llama-server`` would exit seconds later and the coordinator would proxy requests to
  whatever else is listening there.
* Admission control and KV-cache sizing must agree on a single memory number, otherwise a
  second model gets admitted into memory the first one has already reserved.
* **Every one of the above has to hold under concurrency.** ``astart_model`` dispatches
  ``start_model`` onto a worker thread and ``coordinator.py`` calls it once per chat
  request, so two starts overlapping is the normal case and not an exotic one. The whole
  check-decide-reserve sequence and the whole *bookkeeping* half of teardown therefore run
  under ``_state_lock``; see the lock-ordering note in :meth:`InferenceManager.__init__`.

Two rules govern how the rest of DLNE talks to this class, and both exist because breaking
them produced a real, measured bug:

* **Nothing that takes ``_state_lock`` may be called straight from the event loop.** Every
  such method has an ``a``-prefixed async wrapper (``astart_model``, ``astop_all_models``,
  ``astop_model``, ``areap_model``, ``acan_admit``, ``asnapshot_model``) that hops onto a
  worker thread via ``asyncio.to_thread``. The synchronous originals stay because ``atexit``
  cannot await anything.
* **Callers match on the result SENTINELS below, never on a copied string literal.** The
  message half of ``start_model``'s return value is a control signal, not decoration.
"""

import asyncio
import atexit
import glob
import json
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from engine import gguf
from engine.config import SPEC_DEFAULTS, engine_args_without_placement, get_base_dir, get_worker_limits
from engine.downloader import (download_and_extract_engine, coordinator_engine_flavour, engine_dir,
                               set_coordinator_engine_flavour)
from engine.logger import log_coordinator, log_error, log_warning
from engine.model_alias import alias_args


# --------------------------------------------------------------------------------------
# Memory model
#
# Admission ("may this model start at all?") and context sizing ("how many tokens of KV
# cache may it have?") used to be computed from two different numbers: admission counted
# model weights only, while sizing then handed the model every byte that was not weights.
# The second model was therefore admitted into memory the first one's KV cache had already
# taken. Every constant below feeds BOTH calculations so that can no longer happen.
# --------------------------------------------------------------------------------------

# A GGUF's on-disk size is not its resident footprint: llama.cpp also allocates compute
# buffers, the graph, and per-backend scratch space. 1.2x is the factor DLNE has always
# used for this and it is kept so capacity numbers shown in the UI do not move.
VRAM_OVERHEAD_FACTOR = 1.2

# Cost of the KV cache for one token, in GB, when the model file cannot be read.
#
# There used to be a single shipped constant here for every model: 0.5 GB per 8192 tokens,
# or 64 KB per token, described as a conservative average. It was not conservative for
# anything. Phi-3-mini — DLNE's own default model — costs 384 KB per token, because it runs
# 32 layers of full multi-head attention with no grouped-query sharing. The coordinator
# sized a 125,545-token context against 16 GB of VRAM, the real cache wanted 49 GB, and the
# OOM killer took both the engine and the RPC server.
#
# The real number is a property of the model, so it is read from the model: see
# engine/gguf.py. This constant is only what happens when that fails, and it is set to the
# expensive shape rather than the average one.
KV_GB_PER_TOKEN_FALLBACK = gguf.FALLBACK_KV_BYTES_PER_TOKEN / (1024 ** 3)

# Never commit the last 10% of the pool. Workers report totals, not free memory, and the
# OS, the coordinator process itself and the RPC servers all need headroom.
POOL_SAFETY_FRACTION = 0.90

# A single model may claim at most half of whatever is still free for its KV cache. This
# is what leaves room for a second model to be admitted afterwards instead of the first
# arrival swallowing the entire pool.
KV_CLAIM_FRACTION = 0.50

# Floor on the KV claim, so a model is never started with an unusable sliver of cache.
MIN_KV_GB = 0.5

# Absolute minimum context. Below this most instruct models cannot even fit their system
# prompt plus a reply, so starting them would be pointless.
MIN_CONTEXT_TOKENS = 2048

# Used when a model's real size is unknown (not downloaded yet). Deliberately pessimistic:
# an underestimate would let the coordinator promise capacity the cluster does not have.
UNKNOWN_MODEL_ESTIMATE_GB = 8.0

# --------------------------------------------------------------------------------------
# Real memory, slots and context (2.4)
#
# Until 2.4 a model was admitted as 1.2x its file and its KV cache was priced by counting
# every layer, which is four times too much for a hybrid like Qwen3.6 and sized the coding
# engine at the same two slots whatever the cards could hold. A model that is read (see
# engine/gguf.py) is now admitted from the bytes that really sit on the GPUs plus the
# runtime's own buffers, and its slots and context come from what is left.
# --------------------------------------------------------------------------------------

# What llama.cpp allocates on the cards besides weights and cache: compute buffers (about
# 220-300 MiB per card at -b 2048 -ub 512 with flash attention, measured on an RTX 4070
# Laptop and on TheWall's two BC-160s) and the driver's context. 0.75 GiB covers two cards
# with room to spare; the file-times-1.2 fallback below already carries its own slack.
RUNTIME_OVERHEAD_GB = 0.75

# The least context a slot gets when the engine picks its own numbers (ENGINE_CONTEXT and
# ENGINE_SLOTS at 0). Slots are added first, as long as each can still have this much.
SLOT_CONTEXT_FLOOR = 32768

# Most slots the engine adds on its own. Decode batches across slots, but every extra user
# also splits the same memory bandwidth and prompt processing, so the automatic choice
# stops here; ENGINE_SLOTS goes higher.
AUTO_SLOTS_MAX = 8

# Context is given in steps of this many tokens.
CONTEXT_STEP = 1024

# Past this per-slot context the cache switches from q8_0 to q4_0 (as it always has, but
# per slot now, not on the total: 4 users at 32K each are not a 128K context). The engine
# never grows a slot into q4_0 on its own: automatic context stops one step short. An
# operator who sets a bigger ENGINE_CONTEXT per slot gets q4_0 as before.
Q4_KV_SLOT_CONTEXT = 131072
AUTO_SLOT_CONTEXT_CAP = Q4_KV_SLOT_CONTEXT - CONTEXT_STEP

# Of the memory left after weights, the automatic choice claims this much for slots and
# context. It is the fraction the context sizing has always claimed; the rest is the margin
# for a card that is shared with a desktop, since workers report totals, not free memory.
# An operator who knows the machine sets ENGINE_SLOTS and spends the margin deliberately.
AUTO_KV_CLAIM_FRACTION = 0.70

_GIB = 1024 ** 3

# llama-server pads a unified context up to a multiple of this many tokens.
CONTEXT_PAD = 256


def pad_context(tokens: int) -> int:
    return -(-int(tokens) // CONTEXT_PAD) * CONTEXT_PAD


@dataclass(frozen=True)
class SlotPlan:
    """How many request slots an engine gets and how much context each has."""

    slots: int                  # the coding slots; the phone slots are counted apart
    context_per_slot: int
    total_context: int          # the coding share of the pool: slots x context_per_slot
    cache_type: str             # -ctk / -ctv
    kv_gb: float                # KV cache plus recurrent state of the whole pool, in GiB
    mode: str                   # "fixed", "context-fixed", "slots-fixed" or "auto"
    phone_slots: int = 0        # lanes reserved for the phone app (ENGINE_PHONE_SLOTS)
    phone_context: int = 0      # tokens each phone lane holds, padded
    pool_context: int = 0       # the engine's -c; equals total_context while there are no phone lanes


def plan_slots_and_context(
    *,
    kv_f16_bytes_per_token: int,
    state_bytes_per_slot: int,
    budget_gb: float,
    native_context: int,
    requested_slots: int,
    fixed_slots: int = 0,
    fixed_context: int = 0,
    phone_slots: int = 0,
    phone_context: int = 0,
    kv_type: str = "auto",
) -> SlotPlan:
    """
    Chooses -np and -c. Pure arithmetic: no files, no locks, so the same call answers the
    admission query and the start, and the tests can feed it real models' numbers.

    The rule is the owner's: MORE SLOTS FIRST, then more context per slot, and no slot
    below SLOT_CONTEXT_FLOOR tokens when the engine chooses.

    * ``fixed_context`` (ENGINE_CONTEXT, the total ``-c``) and ``fixed_slots`` (ENGINE_SLOTS,
      ``-np``) are the operator's and win outright. With only the context fixed the slots
      are that context divided into floors (65,536 gives the 2 x 32,768 TheWall has always
      run); with only the slots fixed the context is what the budget holds.
    * Otherwise slots = how many floor-sized slots the budget holds (at most
      AUTO_SLOTS_MAX), then every slot's context grows into what is left, up to the model's
      own limit.
    * ``requested_slots`` (the caller's queue_channels) is a floor on the slot count,
      never a cap: the coordinator asks for 2 normally and for 6 after a traffic spike. If
      the budget cannot give that many slots the floor of context gives way, not the
      slots, as it always did.

    Args:
        kv_f16_bytes_per_token: the model's KV cost per token at f16 (see gguf).
        state_bytes_per_slot: recurrent state each slot carries (0 for plain attention).
        budget_gb: memory the cache may take, in GiB.
        native_context: the context one slot may use at most (the model's own limit).
        requested_slots: the caller's wish for slots, at least 1.
        fixed_slots / fixed_context: the operator's ENGINE_SLOTS / ENGINE_CONTEXT, 0 = auto.
        phone_slots / phone_context: lanes kept for the phone app (ENGINE_PHONE_SLOTS /
            ENGINE_PHONE_CONTEXT). They are paid for first, out of the budget, so the coding slots
            are sized from what is left; with ENGINE_CONTEXT fixed they come on top of it. The
            coding numbers (``slots``, ``context_per_slot``, ``total_context``) never include them.
    """
    native = max(MIN_CONTEXT_TOKENS, int(native_context))
    floor = min(SLOT_CONTEXT_FLOOR, native)
    ceiling = max(floor, min(native, AUTO_SLOT_CONTEXT_CAP))
    requested = max(1, int(requested_slots))
    budget_bytes = max(0.0, float(budget_gb)) * _GIB

    def token_bytes(cache_type: str) -> int:
        return max(1, gguf.scale_kv_bytes(int(kv_f16_bytes_per_token), cache_type))

    phone_slots = max(0, int(phone_slots))
    phone_context = pad_context(phone_context) if phone_slots else 0
    # Priced at q8_0, the type the coding slots get unless they grow past Q4_KV_SLOT_CONTEXT.
    budget_bytes = max(0.0, budget_bytes - phone_slots * (state_bytes_per_slot + phone_context * token_bytes("q8_0")))

    def fit_per_slot(slots: int) -> int:
        tokens = (budget_bytes / slots - state_bytes_per_slot) / token_bytes("q8_0")
        return max(0, int(tokens) // CONTEXT_STEP * CONTEXT_STEP)

    if fixed_context:
        total = max(MIN_CONTEXT_TOKENS, int(fixed_context))
        if fixed_slots:
            slots, mode = int(fixed_slots), "fixed"
        else:
            slots = max(1, min(AUTO_SLOTS_MAX, total // floor), requested)
            mode = "context-fixed"
        per_slot = total // slots
    else:
        if fixed_slots:
            slots, mode = int(fixed_slots), "slots-fixed"
        else:
            floor_slot_bytes = state_bytes_per_slot + floor * token_bytes("q8_0")
            fits = int(budget_bytes // floor_slot_bytes) if floor_slot_bytes > 0 else AUTO_SLOTS_MAX
            slots, mode = max(1, min(fits, AUTO_SLOTS_MAX), requested), "auto"
        per_slot = min(ceiling, max(MIN_CONTEXT_TOKENS, fit_per_slot(slots)))
        total = per_slot * slots

    # --kv-unified-per-slot caps every lane, the phone's too, at the coding slot's padded size, so a phone lane
    # never holds more than that: the number recorded (and the coordinator's bound) is the one that holds.
    phone_context = min(phone_context, pad_context(per_slot))
    # ENGINE_KV_TYPE: auto is the rule above; q8_0, q4_0 or f16 is the operator's and wins, whatever the slot size.
    if kv_type in ("q8_0", "q4_0", "f16"):
        cache_type = kv_type
    else:
        cache_type = "q4_0" if per_slot >= Q4_KV_SLOT_CONTEXT else "q8_0"
    # llama-server pads every slot of a unified pool to 256, so the pool is built from padded slots
    # and each coding slot keeps the context the coordinator advertises. Without phone lanes -c is
    # the plain total, as it always was.
    pool = total if not phone_slots else slots * pad_context(per_slot) + phone_slots * phone_context
    kv_gb = ((slots + phone_slots) * state_bytes_per_slot + pool * token_bytes(cache_type)) / _GIB
    return SlotPlan(slots, per_slot, total, cache_type, kv_gb, mode, phone_slots, phone_context, pool)


def phone_lane_args(phone_slots: int, slot_context: int) -> List[str]:
    """
    The flags that give the phone lanes their own slots in one shared cache (and close /slots): nothing without
    lanes, so the engine starts exactly as before. ``--kv-unified-per-slot`` caps EVERY slot, phone lanes included,
    at the coding slot's size, so the pool is only safe because the coordinator keeps each phone prompt
    and answer inside ENGINE_PHONE_CONTEXT and the lanes' gates bound how many run at once.
    """
    if phone_slots <= 0:
        return []
    # --no-slots closes llama-server's GET /slots, whose JSON carries every slot's prompt and generated text, and
    # the lanes now hold phone conversations. Nothing in DLNE calls /slots (checked 2.6.1: engine, scripts, tests).
    return ["--kv-unified", "--kv-unified-per-slot", str(pad_context(slot_context)), "--no-slots"]


def cpu_moe_layers(extra_args) -> int:
    """
    The N of ``--n-cpu-moe N`` (or ``-ncmoe N``) in ENGINE_EXTRA_ARGS, or 0.

    Those blocks' routed experts live in system RAM instead of on the cards, which is
    1.4 GB of Qwen3.6 on TheWall. ``-ot`` patterns are not interpreted: a model placed with
    them is priced as if all of it were on the cards, the safe direction.
    """
    tokens = [str(token) for token in (extra_args or ())]
    for index, token in enumerate(tokens[:-1]):
        if token in ("--n-cpu-moe", "-ncmoe"):
            try:
                return max(0, int(tokens[index + 1]))
            except ValueError:
                return 0
    return 0


# --------------------------------------------------------------------------------------
# Port allocation
# --------------------------------------------------------------------------------------

# How many consecutive ports above the base to consider before declaring exhaustion. Large
# enough that a busy machine still finds a slot, small enough that a pathological loop
# terminates quickly.
PORT_SCAN_RANGE = 256


# --------------------------------------------------------------------------------------
# Teardown timings
#
# These are the two ``process.wait()`` budgets an engine teardown can spend, and together
# they are the reason teardown must not happen under ``_state_lock``: a wedged engine costs
# TERMINATE_GRACE_SECONDS + KILL_GRACE_SECONDS all by itself, and a full cluster stop pays
# that for every engine in turn.
# --------------------------------------------------------------------------------------

# How long a well-behaved llama-server is given to exit after SIGTERM before it is killed.
TERMINATE_GRACE_SECONDS = 2.0

# How long the kill itself is given to be reaped before the process is declared unkillable
# and quarantined in ``_zombies``.
KILL_GRACE_SECONDS = 2.0


# --------------------------------------------------------------------------------------
# Concurrent starts
# --------------------------------------------------------------------------------------

# When two threads ask for the SAME model at the same time, the loser does not start a
# second engine: it waits for the winner and then re-reads the result. This is the ceiling
# on that wait. It has to be generous because the winner may be downloading a llama.cpp
# release over a slow link, but it must exist so a wedged start cannot pin a request
# thread forever.
START_HANDOFF_TIMEOUT_SECONDS = 300.0

# How long a waiting thread blocks on the winner's completion event before looking at the
# world again. Short enough that interpreter shutdown is never held up noticeably.
START_WAIT_POLL_SECONDS = 0.25


# --------------------------------------------------------------------------------------
# start_model result sentinels — the contract with coordinator.py
#
# start_model returns ``(success, port, message)``. The message is a control signal, not
# decoration: the coordinator branches on it to decide between serving an answer, showing a
# nudge, retrying, and returning HTTP 500. Every value it can take is therefore a named
# constant here, and callers must match on the SYMBOL rather than on a copied literal.
#
# The values fall into three classes and callers are expected to treat them differently:
#
#   SUCCESS   STARTED_MESSAGE, ALREADY_RUNNING_MESSAGE
#             The engine is up on the returned port. Proceed.
#
#   RETRYABLE Anything in RETRYABLE_START_MESSAGES (test it with
#             is_retryable_start_failure()). Nothing is wrong with the model, the request or
#             the cluster: another thread's teardown or another thread's start won a race.
#             The correct response is to retry once, or to answer 503 with a "the cluster is
#             reconfiguring, try again" nudge. Answering HTTP 500 turns a routine topology
#             restart into a user-visible failure, which is precisely what finding R3-11
#             reported against the chat handler.
#
#   TERMINAL  NETWORK_MAXED_OUT_MESSAGE, NO_FREE_PORT_MESSAGE, the model-not-on-disk message
#             (test it with is_model_not_on_disk()), or any other string, which is an
#             unexpected exception text. The request cannot succeed as asked. Report it;
#             retrying immediately will only produce the same answer.
# --------------------------------------------------------------------------------------

# The reason :meth:`InferenceManager.can_admit` returns alongside ``True``. It is a named
# constant for the same reason every other message here is: coordinator.py's auto-deploy gate
# tests the reason it got back, and a copied "ok" literal there would silently stop matching
# the day this wording changed.
ADMISSION_OK_REASON = "ok"

# A brand new engine process was spawned for this request.
STARTED_MESSAGE = "Started"

# An engine for this model, sharded over this exact set of RPC endpoints, was already up.
ALREADY_RUNNING_MESSAGE = "Already running"

# The pooled memory budget cannot fit this model. Frozen string: coordinator.py matches it to
# emit the graceful "switch to a model that is already loaded" nudge instead of a 500.
NETWORK_MAXED_OUT_MESSAGE = "Network Maxed Out"

# Every port in the scan range is held by something. Terminal, and it means the host is in a
# state an operator has to look at.
NO_FREE_PORT_MESSAGE = "No free inference port available"

# Returned when a start was aborted mid-flight because stop_all_models()/stop_model() ran
# while the engine was still spawning. It is a distinct string from "Network Maxed Out"
# (which coordinator.py matches on exactly) so neither can be mistaken for the other.
START_CANCELLED_MESSAGE = "Start cancelled during shutdown"

# Returned when this thread waited out START_HANDOFF_TIMEOUT_SECONDS for another thread's
# start of the same model and that start never finished.
START_HANDOFF_TIMEOUT_MESSAGE = "Timed out waiting for a concurrent start of the same model"

# The weights are not on disk (or not inside a trusted root) yet. The wording is preserved
# verbatim from the original implementation because operators grep their logs for it.
MODEL_NOT_ON_DISK_TEMPLATE = "Could not find absolute blob path for {model}"

# The distinguishing substring of MODEL_NOT_ON_DISK_TEMPLATE, so a caller can recognise the
# message without reconstructing the model name.
_MODEL_NOT_ON_DISK_MARKER = "absolute blob path"

# Failures that say "ask again in a moment", not "this cannot work".
RETRYABLE_START_MESSAGES = frozenset({
    START_CANCELLED_MESSAGE,
    START_HANDOFF_TIMEOUT_MESSAGE,
})


def is_retryable_start_failure(message: str) -> bool:
    """
    True when a ``start_model`` failure message means "a race was lost, try again".

    Callers use this instead of comparing against a copied string so the set of retryable
    outcomes can grow here without every call site having to be found again.
    """
    return isinstance(message, str) and message in RETRYABLE_START_MESSAGES


def model_not_on_disk_message(model_path: str) -> str:
    """The canonical "the weights are not here" message for ``model_path``."""
    return MODEL_NOT_ON_DISK_TEMPLATE.format(model=model_path)


def is_model_not_on_disk(message: str) -> bool:
    """
    True when a failure message means "the weights have not been downloaded yet".

    The coordinator's auto-deploy loop uses this to render "Paused (Awaiting Model
    Download)" rather than shouting an error every two seconds about a download that is
    simply still in progress.
    """
    return isinstance(message, str) and _MODEL_NOT_ON_DISK_MARKER in message


# --------------------------------------------------------------------------------------
# The single start decision
#
# ``can_admit`` and ``start_model`` used to be two functions that were *supposed* to reach
# the same verdict, and they drifted apart in three separate ways (finding R3-7): can_admit
# never resolved the model path, never consulted the port allocator, and reported any
# running model as admissible even when the caller's RPC topology would force a restart. The
# coordinator's auto-deploy gate hit the first divergence on every 2-second tick.
#
# There is now exactly ONE function that decides — ``_plan_start_locked`` — and it produces
# this record. ``can_admit`` runs it in dry-run mode and reports the verdict; ``start_model``
# runs it in commit mode and executes the plan. They cannot disagree because there is
# nothing left for them to disagree about: same order of checks, same arithmetic, same
# inputs, same code.
# --------------------------------------------------------------------------------------

# The model is up on ``port`` with the requested topology. Nothing to do.
PLAN_ALREADY_RUNNING = "already_running"

# Another thread is starting this exact model right now. The caller should wait for it.
PLAN_IN_FLIGHT = "in_flight"

# The model may start. In commit mode the port is allocated and the reservation published.
PLAN_START = "start"

# The model may not start; ``message`` says why.
PLAN_REJECT = "reject"


@dataclass
class _StartPlan:
    """
    The verdict of one admission decision, plus everything acting on it needs.

    Produced only by :meth:`InferenceManager._plan_start_locked`, which is called with
    ``_state_lock`` held. The numeric fields are the *same locals* the admission test was
    decided from, so sizing can never be computed from a different budget than admission
    was — the property this class was built to guarantee.
    """

    # One of the PLAN_* constants above.
    outcome: str

    # For PLAN_ALREADY_RUNNING / PLAN_REJECT: the message to return to the caller.
    # For PLAN_START: unused (the caller returns STARTED_MESSAGE once the engine is up).
    message: str = ""

    # PLAN_ALREADY_RUNNING: the live engine's port. PLAN_START: the freshly allocated port
    # in commit mode, or a port that was merely observed to be free in dry-run mode.
    port: int = 0

    # PLAN_IN_FLIGHT only: the reservation record of the thread that owns the start, so the
    # caller can park on its completion event.
    in_flight: Optional[Dict] = None

    # PLAN_START only — the spawn parameters.
    resolved_path: str = ""
    safe_context: int = 0
    reserved_gb: float = 0.0

    # PLAN_START only: -np, the per-slot context, the -ctk/-ctv type and how the numbers were
    # chosen ("auto", "fixed", ...; see plan_slots_and_context).
    slots: int = 0
    slot_context: int = 0
    cache_type: str = "q8_0"
    sizing_mode: str = ""

    # PLAN_START only: the phone lanes and the engine's whole -c (see SlotPlan). ``slots`` and
    # ``safe_context`` stay the coding share.
    phone_slots: int = 0
    phone_context: int = 0
    pool_context: int = 0

    # PLAN_START only: set when the operator's fixed slots/context need more than the pool
    # has left by our count. Logged by start_model (not here: the dry run repeats every two
    # seconds); the operator's number still wins.
    sizing_warning: str = ""

    # PLAN_START only, purely for the log line: the pool state the decision was made against.
    committed_gb: float = 0.0
    budget_gb: float = 0.0

    # PLAN_START, commit mode only: the reservation published into ``_starting``.
    reservation: Optional[Dict] = None

    # PLAN_START, commit mode only: a stale engine (dead, or sharded over a topology the
    # caller no longer wants) that has been detached from ``running_models`` and must be
    # stopped — with the lock released — before the replacement is spawned.
    stale_handle: Optional[Dict] = None


# The suffix a speculative-decoding draft model carries, and the single place that spelling
# is written down. start_model derives a companion's path from its base model by this exact
# substitution, so the catalogue must exclude by the same rule or the two disagree.
DRAFT_COMPANION_SUFFIX = "_dflash.gguf"


def _is_draft_companion(filename: str) -> bool:
    """
    True for a ``*_dflash.gguf`` file, which is a draft companion and not a model.

    These were listed by ``/v1/models`` as ordinary selectable models — the live cluster
    offered ``llama3.1-8b`` and ``llama3.1-8b_dflash`` side by side — so a user could load a
    draft as their primary model. A draft is a deliberately small, deliberately inaccurate
    model whose only job is to propose tokens for a real one to check; served on its own it
    is simply a bad model, presented as a choice.
    """
    return filename.lower().endswith(DRAFT_COMPANION_SUFFIX)


def speculative_args(limits: dict, draft_path: Optional[str]) -> List[str]:
    """
    The llama-server arguments for speculative decoding, from the operator's ``spec_*`` limits.

    ``draft_path`` is the model's draft companion, or None when it has none. Draft types
    (``draft-*``) are dropped without a companion; ngram types need no second model. The
    companion is only passed to llama-server when a draft type survives - loading it under
    ``--spec-type none`` costs VRAM and does nothing, which is how every release before
    2.1.1 ran. Device pinning for RPC pipelines is the caller's business.
    """
    spec = str(limits.get("spec_type", SPEC_DEFAULTS["spec_type"]))
    types = [t for t in spec.split(",") if t and t != "none"]
    if not draft_path:
        types = [t for t in types if not t.startswith("draft-")]
    if not types:
        return []

    args = []
    if any(t.startswith("draft-") for t in types):
        args += [
            "--model-draft", draft_path,
            "-ngld", "99",
            "--spec-draft-n-max", str(limits.get("spec_draft_n_max", SPEC_DEFAULTS["spec_draft_n_max"])),
            "--spec-draft-n-min", str(limits.get("spec_draft_n_min", SPEC_DEFAULTS["spec_draft_n_min"])),
            "--spec-draft-p-min", str(limits.get("spec_draft_p_min", SPEC_DEFAULTS["spec_draft_p_min"])),
        ]
    return args + ["--spec-type", ",".join(types)]


def select_coordinator_engine() -> Optional[str]:
    """
    The llama-server program the coordinator's own engine runs. ENGINE_FLAVOR (2.7) decides between the Vulkan build
    and the CUDA one (see downloader.resolve_engine_flavour). When CUDA was chosen but cannot be had (the release
    signs none for this platform, the download failed, the program will not start) the engine runs on Vulkan, as the
    log says, and the status reports the flavour that really runs. With the Vulkan flavour this is exactly the call
    it always was.
    """
    if coordinator_engine_flavour() == "cuda":
        path = download_and_extract_engine("coordinator", flavour="cuda")
        if path:
            return path
        log_warning("The CUDA engine could not be used on this host; running the engine on Vulkan instead.")
        set_coordinator_engine_flavour("vulkan")
    return download_and_extract_engine("coordinator")


def _endpoint_is_remote(endpoint: str) -> bool:
    """
    True when an RPC endpoint lives on another machine.

    Used to decide whether a pipeline crosses a network boundary at all, which is the only
    thing that makes pinning the draft model worth doing. Lazily imported for the same
    reason the registry does it: engine.benchmark pulls in the WireGuard module, and this
    module is on the import path of nearly every entry point.
    """
    try:
        host, port_s = str(endpoint).rsplit(":", 1)
        if host in ("127.0.0.1", "localhost") and 50100 <= int(port_s) <= 50199:
            return True
    except (ValueError, TypeError):
        pass
    from engine.benchmark import classify_endpoint
    return classify_endpoint(endpoint) not in ("local", "unknown")


# What the vision projector takes on a card besides its file: the buffer it encodes an image in. Measured on
# TheWall's RX 580 (2026-10-28, 2.6.0): 0.96 GiB in all for Qwen3.6's mmproj-F16.gguf (0.84 GiB), fixed when the
# projector loads and no growth while images were encoding. That is 0.12 GiB over the file; 0.15 rounds it up.
PROJECTOR_WORKING_GB = 0.15


# The GPU backends llama.cpp builds ship as libraries beside the binary (libggml-vulkan.so, ggml-cuda.dll, ...).
_OTHER_GPU_BACKENDS = ("ggml-cuda", "ggml-hip", "ggml-sycl", "ggml-opencl", "ggml-metal", "ggml-musa", "ggml-cann")
_media_gpu_warned = False


def engine_build_is_vulkan() -> bool:
    """
    False only when the engine's folder (bin/ of the install, where download_and_extract_engine looks first) holds
    another GPU backend and no Vulkan one, as a CUDA build does. DLNE's own builds are Vulkan, and a folder that says
    nothing either way (a monolithic build, none yet) is taken as Vulkan.
    """
    try:
        # The folder of the flavour this engine runs: bin/, or bin-cuda/ when ENGINE_FLAVOR chose CUDA (2.7).
        names = [n.lower() for n in os.listdir(engine_dir(get_base_dir(), coordinator_engine_flavour()))]
    except OSError:
        return True
    if any("ggml-vulkan" in n for n in names):
        return True
    return not any(backend in n for n in names for backend in _OTHER_GPU_BACKENDS)


def media_gpu_spec(limits: dict) -> str:
    """
    MEDIA_GPU as it applies to this engine: "" when it names no card, or when the engine is not a Vulkan build, whose
    device names (Vulkan0, ...) are the only ones DLNE writes for the card. Said once, in the log.
    """
    global _media_gpu_warned
    spec = str(limits.get("media_gpu") or "").strip()
    if spec and not engine_build_is_vulkan():
        if not _media_gpu_warned:
            _media_gpu_warned = True
            log_warning("MEDIA_GPU is ignored: this engine is not a Vulkan build, and DLNE names the media card as a "
                        "Vulkan device. The vision projector, if any, goes on the first engine card.")
        return ""
    return spec


def media_gpu_shared(limits: dict) -> bool:
    """
    Whether the media card also holds model layers (MEDIA_GPU_SHARED): only when a media card applies to this engine
    at all (``media_gpu_spec``). A shared card is one of the engine's cards, in its --device list and in the pool,
    and the projector still runs on it; picture making, which needs it to itself, does not start.
    """
    return bool(limits.get("media_gpu_shared")) and bool(media_gpu_spec(limits))


# How long the projector stays out after an engine died encoding an image (a load failure stays until a restart).
PROJECTOR_RETRY_SECONDS = 30 * 60
_LOG_CHUNK = 256 * 1024
_LOG_CARRY = 64          # bytes kept between chunks: longer than any line marker below


def _projector_log_signs(path: str, offset: int) -> Optional[Dict]:
    """
    What an engine's log, from ``offset`` (where this engine's output began) to its end, says about the vision
    projector. Read in chunks of 256 KiB, so a log that has grown for weeks costs the same as a short one.
    ``encoding`` and ``encoded`` are the file offsets of the last "encoding image slice" and "image slice
    encoded" lines (-1 for none). None when the log cannot be read.
    """
    signs = {"load_failed": False, "clip": False, "loaded": False, "listening": False, "encoding": -1, "encoded": -1}
    markers = ((b"failed to load multimodal model", "load_failed"), (b"CLIP using", "clip"),
               (b"loaded multimodal model", "loaded"), (b"listening on", "listening"))
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            carry, base = b"", offset
            while True:
                block = handle.read(_LOG_CHUNK)
                if not block:
                    break
                text = carry + block
                start = base - len(carry)
                for marker, key in markers:
                    if marker in text:
                        signs[key] = True
                for key, marker in (("encoding", b"encoding image slice"), ("encoded", b"image slice encoded")):
                    found = text.rfind(marker)
                    if found >= 0:
                        signs[key] = start + found
                base += len(block)
                carry = text[-_LOG_CARRY:]
    except OSError:
        return None
    return signs


def _private_empty_dir(name: str) -> str:
    """
    ``<install>/<name>``, made if need be (owner only) and returned, or "" when it cannot be made. Used for the
    directory the engine is told to look for ffmpeg in: with nothing in it there is nothing to run.
    """
    path = os.path.join(get_base_dir(), name)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        if os.listdir(path):
            return ""       # something put a file there: not a directory to point an engine at
    except OSError:
        return ""
    return path


def _engine_devices(limits: dict, remote: bool) -> Optional[Tuple[str, str, str]]:
    """
    ``(cuda_ids, vulkan_devices, media_device)`` for the engine's environment, or None when the plain
    ALLOWED_GPU_INDICES mapping in start_model applies (no media card and no PCI address to resolve).

    ``media_device`` is the llama.cpp name of the media card (MEDIA_GPU), "" when none is in use: not
    named, not found, the only card ALLOWED_GPU_INDICES leaves, or an engine sharded over volunteers'
    GPUs, which drops the card because ``--device`` would hide the RPC devices from the model.

    A shared media card (MEDIA_GPU_SHARED) is listed the same way, last, so the tuple is the same with the flag on
    or off; ``_media_args`` decides whether the model may use it.
    """
    allowed = str(limits.get("allowed_gpu_indices", "-1")).lower()
    media_spec = media_gpu_spec(limits)
    if allowed == "none" or not (media_spec or ":" in allowed):
        return None
    from engine.hardware import engine_gpu_devices
    cuda, vulkan, media_device = engine_gpu_devices(allowed, media_spec, shared=media_gpu_shared(limits))
    if media_device == "Vulkan0":
        # Nothing else to run the model on: the "media" card is the engine's after all.
        cuda, vulkan, media_device = engine_gpu_devices(allowed, "")
    elif media_device and remote and not media_gpu_shared(limits):
        # A media card of its own is dropped while volunteers' GPUs are in the pipeline: --device would hide the
        # RPC devices from the model. A SHARED card stays: this node's pool counts it, so the coordinator plans with
        # it (24 GB here plus a docked 8 GB laptop is the 32 GB that unlocks Q4_K_M), and the engine must really
        # have it. _media_args then passes no --device at all, so the model sees every local card and every RPC one.
        vulkan, media_device = vulkan.rpartition(",")[0], ""
    if allowed == "-1" and not (cuda or vulkan):
        return None
    return cuda, vulkan, media_device


def _get_drm_vram_used_bytes() -> int:
    """Returns total VRAM allocated across local DRM cards in bytes on Linux."""
    import glob
    total = 0
    for p in glob.glob("/sys/class/drm/card*/device/mem_info_vram_used"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                total += int(f.read().strip())
        except Exception:
            pass
    return total


def _inspect_llama_log_stage(log_path: Optional[str]) -> Tuple[float, str]:
    """Returns (stage_progress_pct, stage_phase_text) by reading recent llama-server logs."""
    if not log_path or not os.path.exists(log_path):
        return 0.0, ""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-30:]
        text = "".join(lines)
        if "model loaded" in text or "listening on" in text:
            return 100.0, "Model loaded and listening on port"
        if "initializing, n_slots" in text or "kv_unified" in text:
            return 95.0, "Allocating KV caches in GPU VRAM"
        if "loading draft model" in text or "common_speculative_init_result" in text:
            return 85.0, "Offloading draft model to Vulkan0"
        if "llama threadpool init" in text:
            return 50.0, "Distributing model layers to GPUs"
        if "loading model" in text:
            return 15.0, "Parsing GGUF metadata & tensors"
    except Exception:
        pass
    return 0.0, ""


def canonical_model_tag(tag: Optional[str]) -> str:
    """
    Normalizes any model identifier (absolute path, Ollama colon-tag, or bare filename)
    to its canonical .gguf filename.
    e.g.:
      '/home/user/models/qwen2.5-coder-14b.gguf' -> 'qwen2.5-coder-14b.gguf'
      'qwen2.5-coder-14b'                       -> 'qwen2.5-coder-14b.gguf'
      'qwen2.5-coder:14b'                       -> 'qwen2.5-coder-14b.gguf'
    """
    if not tag or not isinstance(tag, str):
        return ""
    base = os.path.basename(tag.strip()).replace(":", "-")
    if not base or ".." in base or "\x00" in base:
        return ""
    return base if base.lower().endswith(".gguf") else f"{base}.gguf"


class InferenceManager:
    """Starts, tracks and tears down the local llama-server processes."""

    def __init__(self):
        # ------------------------------------------------------------------------------
        # LOCK ORDERING — the only rule that keeps this class deadlock-free
        #
        #       _state_lock   (outer)   ->   _port_lock   (inner, leaf)
        #
        # * ``_state_lock`` guards ``running_models``, ``_starting``, ``_terminating`` and
        #   ``_zombies`` — the four tables that between them account for every byte of the
        #   memory pool and every port DLNE has taken. It is
        #   held across the ENTIRE check-decide-reserve step of ``start_model`` and the
        #   ENTIRE teardown step of ``stop_all_models``/``stop_model``, so admission, port
        #   assignment and publication are one atomic transaction. Two threads can no longer
        #   both miss the "is it already running?" test, both Popen, and have the second
        #   assignment silently destroy the first process handle.
        # * ``_port_lock`` is a LEAF lock: no code path underneath it ever tries to take
        #   ``_state_lock``. There is therefore no cycle to close and the ordering above
        #   cannot be violated by accident.
        # * Both are ``RLock``s. The teardown helpers call one another
        #   (``stop_all_models`` -> ``_settle_termination_locked`` -> ``_release_port``), and
        #   ``atexit`` re-enters ``stop_all_models`` on a thread that may already own the
        #   lock; a plain ``Lock`` would self-deadlock there.
        #
        # WHAT ``_state_lock`` IS NEVER HELD ACROSS — the invariant, stated so it is true
        #
        #   1. Fetching the llama.cpp release, ``Popen``, and the engine's weight load.
        #   2. ``process.terminate()`` / ``process.kill()`` and the ``process.wait()`` that
        #      follows them. This one used to be false. ``_terminate_entry_locked`` ran two
        #      ``wait(timeout=2.0)`` calls with the lock held, and ``stop_all_models`` did it
        #      for every engine in a single unbroken critical section: three wedged engines
        #      measured a 12.03 s hold with an unrelated ``can_admit`` blocked 11.76 s behind
        #      it, on the coordinator's event loop. Teardown is now split in three —
        #      :meth:`_detach_entry_locked` (fast, locked), :meth:`_stop_process_unlocked`
        #      (slow, UNLOCKED) and :meth:`_settle_termination_locked` (fast, locked).
        #   3. Any wait for another thread. A thread parked on a sibling start's completion
        #      event has always released the lock first, and its wait is bounded by
        #      ``START_HANDOFF_TIMEOUT_SECONDS``. This is also why ``atexit`` cannot hang.
        #
        # WHAT IT IS HELD ACROSS, so nobody has to guess: a handful of ``os.stat``/
        # ``realpath`` calls (``resolve_model_path``), one small JSON read
        # (``_lookup_json_max_context``) and up to ``PORT_SCAN_RANGE`` loopback ``bind()``
        # probes. All local, all bounded; measured below the 15.6 ms Windows clock tick on
        # the reference machine with 255 real bind probes plus the stat and the JSON read —
        # i.e. too short to time. A pathologically slow network mount holding the models
        # directory is the only way to stretch it, and that is why the ``a``-prefixed
        # wrappers exist for every entry point that takes it.
        #
        # The one caveat, stated rather than glossed over: the EXCEPTIONAL paths still log
        # while holding the lock — quarantining an engine that survived SIGKILL, reaping one
        # that finally died, and reporting port exhaustion. Those are rare by construction,
        # and the routine paths (every teardown, every start) collect their messages and emit
        # them after releasing, because Python's rotating file handler can perform an
        # ``os.rename`` in the middle of a log call.
        #
        # WHAT MAKES DROPPING THE LOCK SAFE, in both the start and the teardown direction:
        # the resources stay accounted for the whole time they are unowned. A start
        # publishes its memory reservation and its port into ``_starting`` BEFORE the lock is
        # dropped; a teardown moves the engine into ``_terminating`` BEFORE the lock is
        # dropped. Both tables are counted by :meth:`_committed_gb_locked` and both are
        # respected by :meth:`_reconcile_allocated_ports`, so no other thread can be sized
        # against a pool that looks emptier than it is, and no port can be reissued while a
        # live process still owns it.
        # ------------------------------------------------------------------------------
        self._state_lock = threading.RLock()
        self._port_lock = threading.RLock()

        # model_path -> {
        #     "process":       subprocess.Popen,
        #     "port":          int,
        #     "log_file":      open file object kept alive for the child's stdout/stderr,
        #     "safe_context":  int   tokens the engine was started with,
        #     "dynamic_slots": int   parallel request slots (-np),
        #     "endpoints":     List[str] sorted RPC endpoints this model was sharded over,
        #     "reserved_gb":   float weights + KV actually committed from the pool,
        # }
        # Only ever mutated under _state_lock.
        self.running_models: Dict[str, Dict] = {}

        # In-flight starts: model_path -> {
        #     "reserved_gb": float             memory booked against the pool right now,
        #     "port":        int               port already allocated for this start,
        #     "done":        threading.Event   set when the start finishes, either way,
        #     "cancelled":   bool              set by a teardown that ran mid-start,
        #     "waiters":     int               threads parked on "done" (diagnostics only),
        # }
        #
        # This table is what closes the admission race: the reservation is visible to every
        # other thread's budget arithmetic from the instant the decision is made, not from
        # the instant Popen returns tens of seconds later.
        self._starting: Dict[str, Dict] = {}

        # The projector file (ENGINE_MMPROJ) an engine died on; later starts leave it out, so a card or a
        # file that cannot carry it costs the vision and not the coding engine. Cleared by a restart.
        self._mmproj_failed = ""
        self._mmproj_retry_at = 0.0         # monotonic time the projector is tried again; 0 = not before a restart
        # (projector, model) pairs whose headers were compared, with the answer; and the pairs already warned about.
        self._projector_fit_cache: Dict[Tuple, Tuple] = {}
        self._projector_fit_warned: set = set()

        # Engines that have been detached from running_models and are being stopped RIGHT
        # NOW by some thread that is deliberately not holding _state_lock while it waits for
        # them. Each entry: {"model_path", "data", "port", "reserved_gb"}.
        #
        # This table is the teardown mirror image of _starting, and it is what lets the slow
        # terminate/wait escalation happen with the lock released: for the up to ~4 seconds
        # an engine takes to die, it is in neither running_models nor _terminating-free
        # limbo — it is here, still counted by _committed_gb_locked() and still holding its
        # port off the allocator, exactly as it was before it was detached.
        self._terminating: List[Dict] = []

        # Engines that ignored SIGTERM *and* SIGKILL. They are gone from running_models but
        # they are still holding their VRAM and their TCP port, so they keep counting
        # against the budget and their port stays off the allocator until they really die.
        # Each entry: {"model_path", "process", "port", "reserved_gb"}.
        self._zombies: List[Dict] = []

        self.base_llama_port = 8081

        # Ports we believe are ours. Guarded by _port_lock because start_model is executed
        # on worker threads via astart_model() and two starts can therefore overlap.
        self._allocated_ports: set[int] = set()

        # Ports handed out by _allocate_port but not yet recorded in running_models. They
        # have to be tracked separately so the reconciliation pass below cannot reclaim a
        # port out from under an in-flight start_model.
        self._pending_ports: set[int] = set()

        # port -> the Popen we spawned on it. A spawned-but-not-yet-bound engine is
        # invisible to the bind probe for the tens of seconds it spends loading weights, so
        # process liveness, not bindability, is the authority on whether a port is free.
        self._port_processes: Dict[int, object] = {}

        atexit.register(self.stop_all_models)

    # ----------------------------------------------------------------------------------
    # Endpoint aggregate
    # ----------------------------------------------------------------------------------

    @property
    def current_endpoints(self) -> List[str]:
        """
        Sorted union of the RPC endpoints of every running model.

        This used to be a single shared attribute overwritten by whichever model started
        last, which meant starting model B made the coordinator believe model A's topology
        had changed and restart it (bug #6). Endpoints are now stored per model and this
        property is only the read-only aggregate.

        It must keep returning a plain ``list``: coordinator.py serialises it into the
        telemetry JSON and compares ``set(manager.current_endpoints)`` against the live
        registry.
        """
        # list() first: start_model/stop_all_models mutate running_models from worker
        # threads (they are driven through asyncio.to_thread), and the coordinator reads
        # this property from the event loop while serving telemetry. Iterating the live
        # view raised "dictionary changed size during iteration". list(dict.values()) is
        # a single C-level copy, so it cannot observe a partial mutation.
        merged: set[str] = set()
        for data in list(self.running_models.values()):
            merged.update(data.get("endpoints", []))
        return sorted(merged)

    # ----------------------------------------------------------------------------------
    # Path containment
    # ----------------------------------------------------------------------------------

    def _model_search_roots(self) -> List[str]:
        """
        Absolute directories a model file is allowed to live in, most specific first.

        The configured drop folder may legitimately sit outside the install (an operator
        pointing DLNE at a dedicated model drive), so it is a root in its own right. A
        relative value is anchored to the install root rather than the process working
        directory — DLNE is launched from a desktop shortcut, a service manager and a
        console, and those three have three different working directories.
        """
        base_dir = get_base_dir()

        custom_dir = str(get_worker_limits().get("custom_gguf_dir") or "").strip()
        if not custom_dir:
            custom_dir = os.path.join(base_dir, "models")
        elif not os.path.isabs(custom_dir):
            custom_dir = os.path.join(base_dir, custom_dir)

        roots = [custom_dir, os.path.join(base_dir, "models"), base_dir]

        # Deduplicate while preserving priority order; realpath so the containment test
        # below compares like with like (symlinks, 8.3 short names, trailing separators).
        unique_roots: List[str] = []
        for root in roots:
            real_root = os.path.realpath(root)
            if real_root not in unique_roots:
                unique_roots.append(real_root)
        return unique_roots

    @staticmethod
    def _is_within(candidate: str, root: str) -> bool:
        """
        True when ``candidate`` resolves to something inside ``root``.

        ``commonpath`` is used rather than ``startswith`` because a prefix comparison
        happily accepts ``/models-evil`` as being inside ``/models``. It raises ValueError
        for paths on different Windows drives, which is simply "not contained".
        """
        try:
            return os.path.commonpath([candidate, root]) == root
        except ValueError:
            return False

    def resolve_model_path(self, tag: str) -> str:
        """
        Resolves a model tag (``phi3:mini``, ``phi3-mini``, ``phi3-mini.gguf``) to an
        absolute ``.gguf`` path inside a trusted root, or ``""`` if there is no such file.

        SECURITY: the ``model`` field of an OpenAI chat request reaches this function and
        the result is passed to ``llama-server -m``. The previous implementation returned
        any caller-supplied string that ended in ``.gguf`` or happened to exist, which let
        a remote caller name an arbitrary file on the host. A tag is now treated as a bare
        file name and nothing else: anything containing a path separator, a parent-directory
        hop, a drive letter, a home-directory shortcut or a NUL byte is refused outright,
        and the resolved file must still land inside one of ``_model_search_roots()`` after
        symlinks have been followed.
        """
        if not tag or not isinstance(tag, str):
            return ""

        filename = canonical_model_tag(tag)
        if not filename:
            return ""

        for root in self._model_search_roots():
            candidate = os.path.realpath(os.path.join(root, filename))
            # Belt and braces: filename cannot contain a separator by now, but the root
            # itself may be a symlink chain, so the containment test is still performed.
            if not self._is_within(candidate, root):
                continue
            if os.path.isfile(candidate):
                return candidate

        return ""

    # ----------------------------------------------------------------------------------
    # Model discovery
    # ----------------------------------------------------------------------------------

    @staticmethod
    def _classify_model(required_mem: float, total_vram_gb: float, total_sysram_gb: float) -> str:
        """Maps a model's estimated footprint onto the lock status shown in the web UI."""
        if required_mem <= total_vram_gb:
            return "Unlocked"
        if required_mem <= total_vram_gb + total_sysram_gb:
            return "Unlocked (Spillover Warning)"
        return "Locked"

    def get_available_models(self, total_vram_gb: float, total_sysram_gb: float):
        """Formats discovered models into an OpenAI compatible /v1/models response, adding DLNE lock status."""
        model_data = []
        seen_ids: set[str] = set()

        # 1. The operator's drop folder (or the default <install>/models).
        try:
            custom_dir = self._model_search_roots()[0]
            if os.path.isdir(custom_dir):
                for file in sorted(os.listdir(custom_dir)):
                    if not file.lower().endswith(".gguf"):
                        continue
                    if _is_draft_companion(file):
                        continue

                    filepath = os.path.join(custom_dir, file)
                    size_bytes = os.path.getsize(filepath)
                    required_mem = (size_bytes / (1024 ** 3)) * VRAM_OVERHEAD_FACTOR
                    tag = file[: -len(".gguf")]

                    model_data.append({
                        "id": tag,
                        "object": "model",
                        "created": int(os.path.getctime(filepath)),
                        "owned_by": "dlne-custom",
                        "dlne_status": self._classify_model(required_mem, total_vram_gb, total_sysram_gb),
                        "required_vram": round(required_mem, 1),
                    })
                    seen_ids.add(tag.lower())
        except Exception as e:
            log_error(f"Failed to fetch custom models: {e}")

        # 2. Loose .gguf files dropped next to the executable. The glob is anchored to the
        #    install root: it used to be plain "*.gguf", which is relative to the process
        #    working directory, so the same install listed different models depending on
        #    how it was launched (bug #25).
        for gguf_path in sorted(glob.glob(os.path.join(get_base_dir(), "*.gguf"))):
            gguf = os.path.basename(gguf_path)
            if _is_draft_companion(gguf):
                continue
            # Compare on the extension-less stem so the same file discovered through both
            # passes is not listed twice under two spellings.
            if gguf[: -len(".gguf")].lower() in seen_ids or gguf.lower() in seen_ids:
                continue
            try:
                size_bytes = os.path.getsize(gguf_path)
                required_mem = (size_bytes / (1024 ** 3)) * VRAM_OVERHEAD_FACTOR

                model_data.append({
                    "id": gguf,
                    "object": "model",
                    "created": int(os.path.getctime(gguf_path)),
                    "owned_by": "dlne",
                    "dlne_status": self._classify_model(required_mem, total_vram_gb, total_sysram_gb),
                    "required_vram": round(required_mem, 1),
                })
                seen_ids.add(gguf.lower())
            except OSError:
                # File vanished or is unreadable between the glob and the stat; it simply
                # does not appear in the listing.
                continue

        return {"object": "list", "data": model_data}

    def get_active_model_names(self) -> List[str]:
        """
        Basenames of every model currently loaded, for the 'switch to one of these' nudge.

        ``list()`` first for the same reason as :attr:`current_endpoints`: worker threads
        mutate the dict while the event loop reads it, and iterating the live view raises
        "dictionary changed size during iteration".
        """
        return [os.path.basename(m) for m in list(self.running_models.keys())]

    def estimate_model_vram_gb(self, model_path: str) -> float:
        """
        Estimates what a model needs on the GPUs before any context, in GiB: the bytes that
        really sit there plus the runtime's buffers.

        When the model's header and tensor table can be read (engine/gguf.py) that is the
        file minus what llama.cpp keeps in system RAM (the token embedding, and the routed
        experts of the first N blocks when the operator passes ``--n-cpu-moe N``) plus
        RUNTIME_OVERHEAD_GB. Qwen3.6-35B-A3B at IQ3_S with 5 expert blocks in RAM is 11.0
        GiB of weights, 11.8 with the overhead, where the old 1.2 x file said 15.3.
        Otherwise it is the old 1.2 x the file's size, which already carries its own slack.

        The KV cache is NOT in this number: it is sized from what is left (see
        :meth:`_plan_start_locked`).

        Everything goes through ``resolve_model_path`` so an unresolvable or out-of-bounds
        tag can never be stat'ed. When the file is not present yet (a model that is still
        downloading) the pessimistic default is returned rather than 0, so the caller does
        not conclude the cluster has room for a model of unknown size.
        """
        resolved = self.resolve_model_path(model_path)
        if not resolved:
            return UNKNOWN_MODEL_ESTIMATE_GB
        try:
            shape = gguf.model_shape(resolved)
            if shape is not None and shape.has_tensor_table:
                layers = cpu_moe_layers(get_worker_limits().get("engine_extra_args"))
                return shape.gpu_bytes(layers) / _GIB + RUNTIME_OVERHEAD_GB
            file_bytes = sum(os.path.getsize(shard) for shard in gguf.shard_paths(resolved))
            return (file_bytes / _GIB) * VRAM_OVERHEAD_FACTOR
        except OSError:
            return UNKNOWN_MODEL_ESTIMATE_GB

    def _host_memory_gb(self, model_path: str, remote: bool) -> Tuple[float, float]:
        """
        ``(host_gb, returned_gb)`` for a model whose tensor table can be read, else (0, 0).

        ``host_gb`` is what lives in system RAM on this machine: the token embedding plus
        the experts ``--n-cpu-moe`` keeps there. ``returned_gb`` is how much of that
        placement is NOT applied when volunteers' GPUs join (engine_args_without_placement
        drops it): those experts go back to the cards, so :meth:`estimate_model_vram_gb`
        has under-counted the GPU side by that much.
        """
        resolved = self.resolve_model_path(model_path)
        shape = gguf.model_shape(resolved) if resolved else None
        if shape is None or not shape.has_tensor_table:
            return 0.0, 0.0
        layers = cpu_moe_layers(get_worker_limits().get("engine_extra_args"))
        moved = shape.experts_on_cpu_bytes(layers) / _GIB
        if remote:
            return (shape.cpu_bytes(0)) / _GIB, moved
        return shape.cpu_bytes(layers) / _GIB, 0.0

    def _lookup_json_max_context(self, model_path: str) -> int:
        """
        Reads the model's declared ``max_context`` from ``models/models.json``.

        Matches by normalized ID, filename substring, or Hugging Face URL.
        Defaults to 32,768 (or 262,144 for Qwen3, 1,048,576 for 1M models).
        """
        model_name = os.path.basename(model_path).lower().replace(":", "-")

        # Specific ultra-long context overrides (capped to 262,144 / 256K for Vulkan command buffer stability)
        if "1m" in model_name or "1000k" in model_name:
            return 262144   # 256K tokens for 1M models on current Vulkan driver
        if "qwen3" in model_name:
            return 262144   # 256K native for Qwen3
        if "llama-3.3-70b" in model_name or "llama-3.1" in model_name:
            return 131072   # 128K native context for Llama 3.3/3.1

        default_context = 32768
        library_path = os.path.join(get_base_dir(), "models", "models.json")

        try:
            if not os.path.exists(library_path):
                return default_context
            with open(library_path, "r", encoding="utf-8") as handle:
                library = json.load(handle)
            for entry in library:
                if not isinstance(entry, dict):
                    continue
                entry_id = entry.get("id", "").lower().replace(":", "-")
                hf_url = entry.get("hf_url", "").lower()
                hf_file = os.path.basename(hf_url)
                
                # Flexible matching: exact ID, prefix, substring, or GGUF filename
                if (entry_id and (entry_id == model_name or entry_id in model_name or model_name.startswith(entry_id))) or \
                   (hf_file and (hf_file == model_name or hf_file in model_name)):
                    max_ctx = entry.get("max_context")
                    if isinstance(max_ctx, int) and max_ctx > 0:
                        return max_ctx
        except Exception as e:
            log_warning(f"Could not read max_context from models.json: {e}")

        return default_context

    # ----------------------------------------------------------------------------------
    # Port allocation
    # ----------------------------------------------------------------------------------

    @staticmethod
    def _is_port_bindable(port: int) -> bool:
        """
        True when nothing else currently holds ``port`` on the loopback interface.

        Tracking our own allocations is not sufficient: a previous DLNE run that was killed
        without cleanup, or any unrelated program on the machine, can be sitting on the
        port. llama-server binds 127.0.0.1 (see the --host argument below) so the probe
        binds exactly the same address. SO_REUSEADDR is deliberately NOT set — on Windows
        it would let this probe succeed on a port that is already in use, which is the
        precise failure the probe exists to catch.
        """
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    @staticmethod
    def _process_is_alive(process) -> bool:
        """
        True when ``process`` exists and has not exited yet.

        Centralised because "is this engine still running?" is asked from four places and a
        ``poll()`` that raises (a handle closed underneath us, a mock that misbehaves) must
        never be allowed to make a live process look dead — that is exactly how a port gets
        reissued to a second engine.
        """
        if process is None:
            return False
        try:
            return process.poll() is None
        except Exception:
            return False

    def _reconcile_allocated_ports(self) -> None:
        """
        Drops bookkeeping for ports whose model entry has disappeared behind our back.

        ``coordinator.py`` unloads a single model by deleting the entry straight out of
        ``running_models``. Without this pass those port numbers would stay marked as taken
        for the lifetime of the process and the allocator would drift steadily upwards.

        A port is kept when ANY of the following holds:

        * a running model records it, or an in-flight ``_starting`` reservation holds it, or
          a ``_pending_ports`` hand-out has not been confirmed yet — so a concurrent
          ``start_model`` can never lose its port;
        * an engine that is being torn down right now still owns it — a ``_terminating``
          entry is a live process for as long as it takes to die, and its port must not be
          reissued during the (unlocked) window its owner is waiting for it;
        * an unkillable ``_zombies`` entry still owns it;
        * **the process we spawned on it is still alive.** This last rule is the important
          one and it overrides the dictionaries entirely. ``_is_port_bindable`` cannot see
          an engine that has been spawned but has not ``bind()``ed yet, which is the normal
          state for the tens of seconds a multi-GB model spends loading its weights; if the
          dict entry vanished during that window the old code would happily hand the port to
          a second engine that then died on startup.

        Caller must hold ``_state_lock`` (it reads the state dictionaries) and ``_port_lock``.
        """
        keep: set[int] = set(self._pending_ports)

        for data in list(self.running_models.values()):
            port = data.get("port")
            if isinstance(port, int):
                keep.add(port)

        for reservation in list(self._starting.values()):
            port = reservation.get("port")
            if isinstance(port, int):
                keep.add(port)

        for handle in list(self._terminating):
            port = handle.get("port")
            if isinstance(port, int):
                keep.add(port)

        for zombie in list(self._zombies):
            port = zombie.get("port")
            if isinstance(port, int):
                keep.add(port)

        # Liveness beats bookkeeping, and dead processes are forgotten here so the side
        # table cannot grow for the lifetime of the process.
        live_ports: set[int] = set()
        for port, process in list(self._port_processes.items()):
            if self._process_is_alive(process):
                live_ports.add(port)
            else:
                self._port_processes.pop(port, None)

        keep |= live_ports
        self._allocated_ports = (self._allocated_ports & keep) | live_ports

    def _scan_for_free_port(self, claim: bool) -> int:
        """
        Returns the lowest usable port at or above ``base_llama_port``, or 0 if none is.

        "Usable" means both not already allocated by us and not currently bound by anyone
        on the machine. The old ``base_llama_port + len(running_models)`` arithmetic gave
        the same number to two different models as soon as one in the middle was unloaded
        (bug #5): with A on 8081 and B on 8082, stopping A made the next start compute
        8082 again and collide with the live B.

        Args:
            claim: True to take ownership of the port that is found (what ``start_model``
                does); False to only report whether one exists (what ``can_admit`` does).
                The scan itself is identical either way, which is the point: the admission
                query and the start it predicts consult the same allocator and therefore
                cannot disagree about port exhaustion — divergence 2 of finding R3-7.

        Callers hold ``_state_lock``; see the ordering note in ``__init__``.
        """
        with self._port_lock:
            self._reconcile_allocated_ports()
            for port in range(self.base_llama_port, self.base_llama_port + PORT_SCAN_RANGE):
                if port in self._allocated_ports:
                    continue
                if not self._is_port_bindable(port):
                    continue
                if claim:
                    self._allocated_ports.add(port)
                    self._pending_ports.add(port)
                return port

        if claim:
            # Only the claiming path logs. A dry-run query runs on the coordinator's
            # 2-second monitor tick, and logging there would turn one exhausted host into an
            # endless error stream saying the same thing.
            log_error(
                f"No free inference port in range {self.base_llama_port}-"
                f"{self.base_llama_port + PORT_SCAN_RANGE - 1}."
            )
        return 0

    def _allocate_port(self) -> int:
        """Takes ownership of the lowest usable port, or returns 0 if there is none."""
        return self._scan_for_free_port(claim=True)

    def _confirm_port(self, port: int) -> None:
        """Marks an allocated port as recorded in ``running_models``; it is no longer pending."""
        with self._port_lock:
            self._pending_ports.discard(port)

    def _register_port_process(self, port: int, process) -> None:
        """
        Records which process we spawned on ``port``.

        Done the instant ``Popen`` returns, before the entry is published, because from that
        instant onwards the port belongs to a real live process and must not be reissued no
        matter what happens to the bookkeeping afterwards.
        """
        with self._port_lock:
            self._port_processes[port] = process

    def _release_port(self, port: int) -> None:
        """
        Returns a port to the free pool. Safe to call for a port that was never allocated.

        Refuses when the engine we spawned on that port is still alive. Handing that number
        out again would spawn a second llama-server that dies on ``bind()`` seconds later,
        or — worse — leave the coordinator proxying one model's traffic into another one's
        engine. Callers that legitimately keep a live process (the SIGKILL-survivor path in
        :meth:`_settle_termination_locked`) rely on this as their second line of defence.
        """
        with self._port_lock:
            process = self._port_processes.get(port)
            if process is not None:
                if self._process_is_alive(process):
                    log_warning(
                        f"Refusing to free inference port {port}: the engine process that "
                        "owns it is still alive."
                    )
                    return
                self._port_processes.pop(port, None)
            self._allocated_ports.discard(port)
            self._pending_ports.discard(port)

    # ----------------------------------------------------------------------------------
    # Process lifecycle
    # ----------------------------------------------------------------------------------

    def _detach_entry_locked(self, model_path: str, data: Dict) -> Dict:
        """
        Step 1 of 3 of a teardown: hand an engine over to ``_terminating``. FAST.

        Caller must hold ``_state_lock``. Nothing here blocks — no signal is sent, no wait
        is performed, no file is closed. All this does is move the engine from "running" to
        "being stopped", which is a dictionary pop and a list append.

        That matters because of what the two tables mean to everybody else. From the moment
        this returns, the engine is invisible to ``running_models`` (so a caller asking for
        the model gets a fresh start rather than a handle to something that is dying) while
        its memory is still counted by :meth:`_committed_gb_locked` and its port is still
        kept off the allocator by :meth:`_reconcile_allocated_ports`. Nothing about the
        cluster's accounting changes across the hand-over, which is what makes it safe to
        drop the lock immediately afterwards and take as long as the engine needs to die.

        ``data`` does not have to be present in ``running_models``: ``stop_all_models`` has
        already popped it, and the cancelled-start path passes a freshly built entry that
        was never published. The removal below is therefore by IDENTITY, not by key — a
        blind ``pop(model_path)`` could delete somebody else's live entry.

        Returns:
            The handle to pass to :meth:`_stop_process_unlocked` and then to
            :meth:`_settle_termination_locked`.
        """
        port = data.get("port")
        handle = {
            "model_path": model_path,
            "data": data,
            "port": port if isinstance(port, int) else None,
            "reserved_gb": float(data.get("reserved_gb", 0.0) or 0.0),
        }

        if self.running_models.get(model_path) is data:
            self.running_models.pop(model_path, None)

        self._terminating.append(handle)
        return handle

    def _stop_process_unlocked(self, handle: Dict) -> bool:
        """
        Step 2 of 3 of a teardown: actually stop the process. SLOW — up to
        ``TERMINATE_GRACE_SECONDS + KILL_GRACE_SECONDS``.

        The caller MUST NOT hold ``_state_lock``. This method is the entire reason the
        three-step split exists: it used to run inside the critical section, and
        ``stop_all_models`` ran it for every engine back to back without ever releasing,
        which measured a 12.03 s lock hold and blocked an unrelated ``can_admit`` for
        11.76 s (finding R3-10).

        Order matters. A ``terminate()`` that times out MUST be escalated to ``kill()``: the
        original teardown only logged the timeout and then cleared the dictionary, leaving a
        live llama-server holding both its VRAM and its TCP port with no handle left to stop
        it (bug #31).

        Returns:
            True when the process survived ``kill()`` as well and has to be quarantined.
        """
        model_path = handle["model_path"]
        process = handle["data"].get("process")
        survived_kill = False

        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=TERMINATE_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        log_warning(
                            f"Engine for {os.path.basename(model_path)} ignored terminate; killing it."
                        )
                        process.kill()
                        # Second wait reaps the zombie. If even the kill times out the
                        # process is unkillable (uninterruptible driver call) and there is
                        # nothing further we can do but keep accounting for it.
                        try:
                            process.wait(timeout=KILL_GRACE_SECONDS)
                        except subprocess.TimeoutExpired:
                            survived_kill = True
            except Exception as e:
                log_coordinator(f"Engine process already exited or error during termination: {e}")

        # Re-check liveness rather than trusting the timeout alone: a process that ignored
        # terminate may well have died between the kill() and this line, in which case it is
        # an ordinary clean teardown and nothing needs to be quarantined.
        if survived_kill and not self._process_is_alive(process):
            survived_kill = False

        return survived_kill

    def _settle_termination_locked(self, handle: Dict, survived_kill: bool) -> None:
        """
        Step 3 of 3 of a teardown: book the outcome. FAST.

        Caller must hold ``_state_lock``. Closes our copy of the log descriptor, gives the
        port back (or quarantines the process if it would not die), and retires the handle
        from ``_terminating`` — which is the moment the engine's memory finally stops
        counting against the pool.

        A process that survives ``kill()`` is NOT simply forgotten. The original code logged
        "its port and memory stay reserved" and then released the port and dropped the entry
        anyway, so ``committed`` under-reported memory a live process still owned and the
        allocator could reissue its port. Such a process moves to :attr:`_zombies` instead,
        where it keeps counting against the budget and keeps its port off the allocator until
        it actually dies and :meth:`_reap_zombies_locked` collects it.
        """
        data = handle["data"]
        model_path = handle["model_path"]
        port = handle["port"]
        reserved_gb = handle["reserved_gb"]

        # Our copy of the descriptor is always closed. Popen gave the child its own
        # duplicate, so this cannot disturb a survivor's logging, whereas leaving it open
        # leaks a file handle for the lifetime of the coordinator.
        log_file = data.get("log_file")
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass

        if survived_kill:
            self._zombies.append({
                "model_path": model_path,
                "process": data.get("process"),
                "port": port if isinstance(port, int) else 0,
                "reserved_gb": reserved_gb,
            })
            log_error(
                f"Engine for {os.path.basename(model_path)} survived SIGKILL; its port "
                f"({port}) and {reserved_gb:.1f}GB of the memory pool stay reserved until "
                "the OS reaps it."
            )
        elif isinstance(port, int):
            self._release_port(port)

        # Retire by identity: two teardowns of two different engines for the same model can
        # legitimately be in flight at once (stop the old topology, start the new one).
        for index, candidate in enumerate(self._terminating):
            if candidate is handle:
                del self._terminating[index]
                break

    def _stop_and_settle(self, handle: Dict) -> None:
        """
        Runs steps 2 and 3 of a teardown: the slow stop, then the fast bookkeeping.

        The caller MUST NOT hold ``_state_lock`` when calling this — that is the whole point
        of the split. ``atexit`` re-entry is the one exception the design tolerates: because
        both locks are ``RLock``s, a thread that already owns ``_state_lock`` and calls
        ``stop_all_models`` simply keeps holding it throughout. That is a self-inflicted
        stall on a thread that has explicitly serialised itself, not a stall the class
        imposes on the cluster, and it cannot deadlock.
        """
        survived_kill = self._stop_process_unlocked(handle)
        with self._state_lock:
            self._settle_termination_locked(handle, survived_kill)

    def _reap_zombies_locked(self) -> None:
        """
        Releases the port and the memory reservation of any quarantined engine that has
        finally exited.

        Caller must hold ``_state_lock``. Called at the top of every admission decision, so
        an unkillable engine that the OS eventually cleaned up does not permanently shrink
        the cluster's usable pool.
        """
        if not self._zombies:
            return

        still_alive: List[Dict] = []
        for zombie in self._zombies:
            if self._process_is_alive(zombie.get("process")):
                still_alive.append(zombie)
                continue

            log_coordinator(
                f"Unkillable engine for {os.path.basename(str(zombie.get('model_path', '?')))} "
                f"has finally exited; returning port {zombie.get('port')} and "
                f"{float(zombie.get('reserved_gb', 0.0)):.1f}GB to the pool."
            )
            port = zombie.get("port")
            if isinstance(port, int) and port:
                self._release_port(port)

        self._zombies = still_alive

    # ----------------------------------------------------------------------------------
    # Vision (2.6): the media card and the projector
    # ----------------------------------------------------------------------------------

    def _projector_path(self, limits: dict, warn: bool = False, model_path: str = "") -> str:
        """
        ENGINE_MMPROJ as a path, or "" while vision is off, the file is missing, an engine died on it, or (with
        ``model_path``, the model about to start) it is a projector for another model.
        """
        path = os.path.expanduser(str(limits.get("engine_mmproj") or "").strip())
        if path and path == self._mmproj_failed:
            if not self._mmproj_retry_at or time.monotonic() < self._mmproj_retry_at:
                return ""
            self._mmproj_failed, self._mmproj_retry_at = "", 0.0       # the pause is over: try it again
            log_warning(f"Vision is tried again ({os.path.basename(path)}).")
        if not path:
            return ""
        if not os.path.isfile(path):
            if warn:
                log_warning(f"ENGINE_MMPROJ names no file ({path}); the engine starts without vision.")
            return ""
        if model_path and not self._projector_fits(path, model_path, warn):
            return ""
        return path

    def _projector_fits(self, projector: str, model_path: str, warn: bool = False) -> bool:
        """
        Whether ``projector`` is for ``model_path``: the width its output is projected to
        (``clip.vision.projection_dim``, which the converter writes as the text model's hidden size) must be the
        model's ``<arch>.embedding_length``. llama-server refuses a projector of another width at load and exits,
        which would take the coding engine down with it. A header that cannot be read on either side gives no
        answer, and then the projector is tried (the failure fallback is still there).
        """
        try:
            key = (projector, os.stat(projector).st_mtime_ns, model_path, os.stat(model_path).st_mtime_ns)
        except OSError:
            return True
        answer = self._projector_fit_cache.get(key)
        if answer is None:
            wanted, have = gguf.projection_dim(projector), gguf.embedding_length(model_path)
            answer = self._projector_fit_cache[key] = (wanted is None or have is None or wanted == have, wanted, have)
        fits, wanted, have = answer
        if not fits and warn and key not in self._projector_fit_warned:
            self._projector_fit_warned.add(key)
            log_warning(
                f"ENGINE_MMPROJ ({os.path.basename(projector)}, for a model of width {wanted}) is not for "
                f"{os.path.basename(model_path)} (width {have}); that model starts without vision.")
        return fits

    def vision_configured(self) -> bool:
        """Whether an engine started now would load a projector (ENGINE_MMPROJ names a file no engine died on).
        Not whether it fits the model: that is only known once the model is chosen."""
        return bool(self._projector_path(get_worker_limits()))

    def _projector_charge_gb(self, remote: bool, model_path: str = "") -> float:
        """
        What the projector takes from the pool's cards, in GiB: nothing while vision is off, or while it runs
        on a media card that is no part of the pool; its file and working buffer otherwise (a shared media card is
        part of the pool, so the projector on it takes from the pool like anywhere else).
        """
        limits = get_worker_limits()
        path = self._projector_path(limits, model_path=model_path)
        if not path:
            return 0.0
        devices = _engine_devices(limits, remote)
        if devices and devices[2] and not media_gpu_shared(limits):
            return 0.0
        try:
            return os.path.getsize(path) / _GIB + PROJECTOR_WORKING_GB
        except OSError:
            return 0.0

    def _warm_media_hardware(self) -> None:
        """
        Looks at the cards before ``_state_lock`` is taken. The first look can run vulkaninfo, which must not
        happen inside the lock; the sizer's later look (``_projector_charge_gb``) then reads the cached answer.
        """
        limits = get_worker_limits()
        if limits.get("media_gpu") and limits.get("engine_mmproj"):
            _engine_devices(limits, False)

    def _media_args(self, limits: dict, devices: Optional[Tuple[str, str, str]],
                    model_path: str = "", remote: bool = False) -> Tuple[List[str], str]:
        """
        ``(arguments, projector_path)``: ``--device`` for the engine's own cards when a media card is in use
        (so the model gets none of it), and the projector's arguments when vision is on, on the media card if
        there is one. ``projector_path`` is "" without vision, and for a model the projector is not for.
        A shared media card (MEDIA_GPU_SHARED) is in ``--device`` too, last, so ``-ts`` gives it the last share.
        """
        args: List[str] = []
        media_device = devices[2] if devices else ""
        if media_device and not (remote and media_gpu_shared(limits)):
            engine_cards = len(devices[1].split(","))
            if not media_gpu_shared(limits):
                engine_cards -= 1            # the media card is the last one listed, and the model gets none of it
            args += ["--device", ",".join(f"Vulkan{i}" for i in range(engine_cards))]
        # A shared card with volunteers' GPUs in the pipeline: no --device, which would hide the RPC devices. Every
        # local card is visible already (GGML_VK_VISIBLE_DEVICES), the media card among them, and --mmproj-device
        # below still puts the projector on it.
        projector = self._projector_path(limits, warn=True, model_path=model_path)
        if projector:
            no_ffmpeg = _private_empty_dir("no-ffmpeg")
            if not no_ffmpeg:
                log_error("Vision is off: a private empty directory for the engine's ffmpeg path could not be made.")
                return args, ""
            args += ["--mmproj", projector, "--image-max-tokens", str(limits.get("engine_image_max_tokens", 1024))]
            # An image stb_image cannot read (every WebP) would otherwise be piped to an ffmpeg found on PATH;
            # pointed at an empty directory that path cannot start anything, and the image fails as a plain 400.
            args += ["--video-ffmpeg-dir", no_ffmpeg]
            if media_device:
                args += ["--mmproj-device", media_device]
            # llama.cpp turns both off for the whole engine once a projector is loaded.
            log_coordinator(
                f"Vision is on ({os.path.basename(projector)}, on {media_device or 'the first engine card'}): "
                "the engine's prompt-prefix reuse (--cache-reuse) and context shift are off for every request."
            )
        return args, projector

    def _check_projector_failure(self, entry: Dict) -> None:
        """
        Called with an engine that has died. If it ran with the projector and its log shows the projector was
        the trouble, later starts leave the projector out: coding goes on, without vision. That is when it
        failed to load, started loading (llama.cpp prints "CLIP using <backend>" only after the model's own
        weights are in) and never finished (both stay off until DLNE restarts), or was encoding an image when
        the engine ended (off for PROJECTOR_RETRY_SECONDS: that log line cannot tell the projector's card from
        another crash that came after it, so it is tried again). A death before any of that (weights that do
        not fit, a port taken) says nothing about the projector and costs no vision.
        The request that met the crash has had its error; no restart is tried for it.
        """
        projector = entry.get("mmproj_path")
        if not projector or self._process_is_alive(entry.get("process")):
            return
        signs = _projector_log_signs(entry.get("log_path") or "", int(entry.get("log_offset") or 0))
        if signs is None:
            return
        died_loading = signs["load_failed"] or (signs["clip"] and not signs["loaded"] and not signs["listening"])
        died_encoding = signs["encoding"] > signs["encoded"]
        if died_loading or died_encoding:
            self._mmproj_failed = projector
            self._mmproj_retry_at = 0.0 if died_loading else time.monotonic() + PROJECTOR_RETRY_SECONDS
            again = ("Check the media card (MEDIA_GPU) and restart DLNE to try it again." if died_loading
                     else f"It is tried again in {PROJECTOR_RETRY_SECONDS // 60} minutes.")
            log_error(
                f"The inference engine died {'while loading' if died_loading else 'while encoding an image with'} "
                f"the vision projector ({projector}). Vision is OFF from the next engine start; coding goes on. {again}"
            )

    def reap_model(self, model_path: str) -> bool:
        """
        Drops a model whose engine process has already died, releasing everything it held.

        The coordinator's startup watchdog notices a crashed llama-server and previously
        cleaned up by reaching into ``running_models`` itself: closing the log file,
        ``del``-ing the entry and calling the private ``_release_port``. That duplicated
        this class's teardown rules in another module, and the bare ``del`` raised
        ``KeyError`` whenever a concurrent ``stop_all_models()`` had already removed the
        entry - losing the port release. Exposing the operation here keeps process,
        log-file and port lifetimes owned by exactly one class.

        CONCURRENCY: the entry is detached under the lock and the process is then stopped
        with the lock released, so a reap can never stall an unrelated admission decision.
        The coordinator calls this from a request handler; use :meth:`areap_model` there so
        the (short, but non-zero) wait does not run on the event loop.

        Returns:
            True if an entry was found and released, False if it was already gone.
        """
        with self._state_lock:
            model_path = canonical_model_tag(model_path)
            data = self.running_models.get(model_path)
            if data is None:
                return False
            handle = self._detach_entry_locked(model_path, data)

        self._check_projector_failure(data)

        # The caller only invokes this after observing a dead process, so the stop below is
        # normally a no-op poll; it is still performed in full because "the process looked
        # dead a moment ago" and "the process is dead" are not the same statement.
        self._stop_and_settle(handle)
        return True

    # ----------------------------------------------------------------------------------
    # Admission control
    #
    # One implementation, two callers: start_model (which then acts on it) and can_admit
    # (which only reports it, for the coordinator's auto-deploy gate). They cannot drift
    # apart into two different memory formulas because there is only one formula.
    # ----------------------------------------------------------------------------------

    def _committed_gb_locked(self) -> float:
        """
        Total memory currently spoken for, in GB. Caller must hold ``_state_lock``.

        Four populations count, and leaving any of them out over-commits the pool:

        * running models — what they actually took, weights plus the KV cache they were
          started with;
        * in-flight starts — booked at the moment the decision was made, which is what
          stops two concurrent admissions from both being sized against an empty pool;
        * engines being torn down — they have left ``running_models`` but the OS has not
          reclaimed their memory yet, and for the seconds that takes they are still real;
        * zombies — engines that survived SIGKILL and are still holding their allocation.

        A model is NEVER counted twice. The three tables are disjoint by construction: an
        entry is moved from ``running_models`` into ``_terminating`` inside a single critical
        section, and a start's ``_starting`` reservation is retired inside the very same
        critical section that publishes its ``running_models`` entry. That last property is
        finding R3-8: the reservation used to be retired in a *second* acquisition of the
        lock, so for the length of one disk write both records existed and a concurrent
        admission double-counted the model — measured at 41.00 GB committed against a
        28.80 GB budget when the true figure was 20.50 GB, which rejected a model that fitted
        with 8.30 GB to spare.
        """
        total = 0.0
        for data in list(self.running_models.values()):
            total += float(data.get("reserved_gb", 0.0) or 0.0)
        for reservation in list(self._starting.values()):
            total += float(reservation.get("reserved_gb", 0.0) or 0.0)
        for handle in list(self._terminating):
            total += float(handle.get("reserved_gb", 0.0) or 0.0)
        for zombie in list(self._zombies):
            total += float(zombie.get("reserved_gb", 0.0) or 0.0)
        return total

    def _evaluate_admission_locked(
        self,
        model_path: str,
        total_vram_gb: float,
        total_sysram_gb: float,
        excluded_gb: float = 0.0,
        remote: bool = False,
    ) -> Tuple[bool, str, float, float, float, float]:
        """
        The single budget calculation.

        Caller must hold ``_state_lock``.

        Args:
            model_path: the model being considered.
            total_vram_gb: pooled VRAM reported by the registry.
            total_sysram_gb: pooled system RAM reported by the registry.
            excluded_gb: memory that is currently counted as committed but is about to be
                given back as part of this very decision — the reservation of an incumbent
                engine that has to be rebuilt because it is dead or because its RPC topology
                changed. Without this term a restart would be tested against a pool that
                still contains the model it is replacing, and would refuse every model large
                enough to matter.
            remote: True when the engine will run across volunteers' GPUs, where the
                operator's card placement (``--n-cpu-moe``) is dropped and those experts go
                back onto the cards.

        Returns ``(admitted, reason, weights_gb, free_gb, committed_gb, budget_gb)``.
        ``weights_gb`` is what the model charges the budget it is admitted against: the
        bytes on the GPUs plus the runtime's buffers, and, only when it has to spill into
        system RAM, the part of it that lives there too. The extra numbers are what
        ``start_model`` needs to size the cache, so admission and sizing are literally
        computed from the same locals.
        """
        # VRAM is the primary compute pool; SysRAM is strictly an emergency overflow spillover
        vram_budget = total_vram_gb * POOL_SAFETY_FRACTION
        total_budget = (total_vram_gb + total_sysram_gb) * POOL_SAFETY_FRACTION
        committed = max(0.0, self._committed_gb_locked() - excluded_gb)
        weights = self.estimate_model_vram_gb(model_path)
        host_gb, returned_gb = self._host_memory_gb(model_path, remote)
        weights += returned_gb
        weights += self._projector_charge_gb(remote, self.resolve_model_path(model_path) or "")

        # The part that lives in this machine's RAM has to fit there whatever the cards
        # hold. Skipped when the pool reports no RAM at all, as admission always has.
        if host_gb > 0 and total_sysram_gb > 0 and host_gb > total_sysram_gb * POOL_SAFETY_FRACTION:
            return (False, NETWORK_MAXED_OUT_MESSAGE, weights,
                    vram_budget - committed - weights, committed, vram_budget)

        # If it fits in VRAM, budget is VRAM only so SysRAM is never touched
        if (committed + weights + MIN_KV_GB) <= vram_budget:
            budget = vram_budget
            free = budget - committed - weights
        else:
            # Emergency spillover into SysRAM only when model strictly exceeds VRAM; the
            # whole model is then charged, host part included.
            weights += host_gb
            budget = total_budget
            free = budget - committed - weights

        if free < MIN_KV_GB:
            return False, NETWORK_MAXED_OUT_MESSAGE, weights, free, committed, budget

        return True, ADMISSION_OK_REASON, weights, free, committed, budget

    def _plan_start_locked(
        self,
        model_path: str,
        requested_endpoints: Optional[List[str]],
        total_vram_gb: float,
        total_sysram_gb: float,
        commit: bool,
        queue_channels: int = 2,
    ) -> _StartPlan:
        """
        Decides what starting ``model_path`` right now would do. THE single source of truth.

        Caller must hold ``_state_lock``.

        Args:
            model_path: the model tag as the caller supplied it.
            requested_endpoints: the sorted, deduplicated RPC topology the caller wants the
                model sharded over, or ``None`` for "whatever it is already using". ``None``
                is what an admission query passes when it has no opinion; it means a live
                engine is accepted as-is instead of being judged against a topology the
                caller never named.
            total_vram_gb / total_sysram_gb: the pool, as reported by the registry.
            commit: False to only decide (``can_admit``), True to also act on the decision
                (``start_model``) — allocate the port, publish the reservation and detach the
                engine that is being replaced.

        THE ONE-PATH GUARANTEE. ``can_admit`` and ``start_model`` used to be separate
        implementations that reached different verdicts in three measured ways (R3-7):

        1. ``can_admit`` never called ``resolve_model_path``, so a model that had not
           downloaded yet was reported admissible and then failed to start with "Could not
           find absolute blob path". The coordinator's auto-deploy loop hit this every two
           seconds and the failure was hidden by suppressing the log line. Step 3 below is
           shared, so a missing model is now refused by both.
        2. ``can_admit`` ignored port exhaustion entirely. Step 6 below runs the same
           allocator scan for both, differing only in whether the port found is claimed.
        3. ``can_admit`` reported any running model as admissible regardless of the caller's
           RPC topology, while ``start_model`` tore the incumbent down and re-ran admission,
           which could then fail — leaving the caller with no model at all after being told
           yes. Step 2 below decides "rebuild needed?" once, and step 4 evaluates the rebuild
           with the incumbent's own reservation excluded, so both answer the same question.
           A rebuild that is refused now leaves the incumbent running rather than destroying
           it first and discovering the replacement does not fit.

        The residual, and it is structural rather than arithmetic: a dry run assumes the
        incumbent's teardown will succeed. If that engine turns out to survive SIGKILL it
        keeps its memory as a zombie and the start that was predicted to fit will be refused.
        That cannot be predicted without actually killing the process, and it is the only
        difference between the two answers.
        """
        # Reclaim anything an unkillable engine was holding before measuring the pool, in
        # both modes. This only ever gives resources back, and only for processes the OS has
        # genuinely finished with, so it is not a mutation a dry run needs to avoid — and
        # skipping it would make the dry run answer against a stale, smaller pool.
        self._reap_zombies_locked()
        model_path = canonical_model_tag(model_path)

        # --- 1. Is another thread already starting this exact model? ---------------------
        in_flight = self._starting.get(model_path)
        if in_flight is not None:
            return _StartPlan(
                outcome=PLAN_IN_FLIGHT,
                port=int(in_flight.get("port") or 0),
                in_flight=in_flight,
            )

        # --- 2. Is it already running, and if so is it still the engine we want? ---------
        stale_entry = None
        incumbent_gb = 0.0
        data = self.running_models.get(model_path)
        if data is not None:
            # Compare against THIS model's own endpoints. The original code compared a single
            # process-wide attribute, so any other model starting made this one look stale.
            topology_unchanged = (
                requested_endpoints is None
                or set(data.get("endpoints", [])) == set(requested_endpoints)
            )
            if self._process_is_alive(data.get("process")) and topology_unchanged:
                return _StartPlan(
                    outcome=PLAN_ALREADY_RUNNING,
                    message=ALREADY_RUNNING_MESSAGE,
                    port=int(data.get("port") or 0),
                )
            # The process died, or this model's own topology genuinely changed. It has to be
            # rebuilt — but it is NOT torn down yet, because the replacement has not been
            # admitted yet. Its reservation is what admission gets to spend below.
            stale_entry = data
            incumbent_gb = float(data.get("reserved_gb", 0.0) or 0.0)

        # --- 3. Resolve the logical tag into an absolute blob path -----------------------
        # Done before anything is reserved so an unresolvable or out-of-bounds tag can never
        # book memory or burn a port.
        resolved_path = self.resolve_model_path(model_path)
        if not resolved_path:
            return _StartPlan(
                outcome=PLAN_REJECT,
                message=model_not_on_disk_message(model_path),
            )

        # --- 4. Admission ----------------------------------------------------------------
        remote = any(_endpoint_is_remote(endpoint) for endpoint in (requested_endpoints or []))
        admitted, reason, weights, free, committed, budget = self._evaluate_admission_locked(
            model_path, total_vram_gb, total_sysram_gb, excluded_gb=incumbent_gb, remote=remote
        )
        if not admitted:
            return _StartPlan(outcome=PLAN_REJECT, message=reason)

        # --- 5. Slots and context, from the same locals admission was decided from --------
        # What the cache may take: prioritise free VRAM and claim only part of it (the rest
        # is the margin for a card shared with a desktop); never steal SysRAM unless in
        # emergency.
        free_vram = max(0.0, (total_vram_gb * POOL_SAFETY_FRACTION) - committed - weights)
        if free_vram >= MIN_KV_GB:
            kv_budget_gb = free_vram * AUTO_KV_CLAIM_FRACTION  # Stays strictly inside VRAM
        else:
            kv_budget_gb = max(MIN_KV_GB, free * 0.30)  # Emergency minimum spillover only

        # What a token of cache costs, and what each slot costs besides, are properties of
        # THIS model, read from its file: only the attention blocks of a hybrid hold a cache
        # (Qwen3.6: 10 of 40 blocks, 20 KiB a token at f16, not 80), and the others keep a
        # fixed recurrent state per slot. The type is q8_0 below 131,072 tokens per slot,
        # q4_0 from there.
        shape = gguf.model_shape(resolved_path)
        limits = get_worker_limits()
        sized = plan_slots_and_context(
            kv_f16_bytes_per_token=gguf.kv_bytes_per_token_for_model(resolved_path),
            state_bytes_per_slot=shape.state_bytes_per_sequence if shape else 0,
            budget_gb=kv_budget_gb,
            native_context=self._lookup_json_max_context(model_path),
            requested_slots=queue_channels,
            # The operator's ENGINE_SLOTS and ENGINE_CONTEXT win outright; 0 is automatic.
            fixed_slots=int(limits.get("engine_slots") or 0),
            fixed_context=int(limits.get("engine_context") or 0),
            phone_slots=int(limits.get("engine_phone_slots") or 0),
            phone_context=int(limits.get("engine_phone_context") or 0),
            kv_type=str(limits.get("engine_kv_type") or "auto"),
        )
        safe_context = sized.total_context
        reserved_gb = weights + sized.kv_gb

        sizing_warning = ""
        if sized.mode != "auto" and sized.kv_gb > free_vram:
            sizing_warning = (
                f"ENGINE_SLOTS/ENGINE_CONTEXT ask for {sized.slots} slots x {sized.context_per_slot} "
                f"tokens, about {sized.kv_gb:.1f}GB of cache, and the pool has {free_vram:.1f}GB left "
                f"after the weights by our count. The setting is used as written."
            )

        # --- 6. Port ----------------------------------------------------------------------
        # Deliberately BEFORE the incumbent is detached, in both modes: a detached engine
        # keeps its port until it dies, so scanning first is what makes the dry run and the
        # commit see an identical allocator state.
        assigned_port = self._scan_for_free_port(claim=commit)
        if not assigned_port:
            return _StartPlan(outcome=PLAN_REJECT, message=NO_FREE_PORT_MESSAGE)

        plan = _StartPlan(
            outcome=PLAN_START,
            port=assigned_port,
            resolved_path=resolved_path,
            safe_context=safe_context,
            reserved_gb=reserved_gb,
            slots=sized.slots,
            slot_context=sized.context_per_slot,
            cache_type=sized.cache_type,
            sizing_mode=sized.mode,
            phone_slots=sized.phone_slots,
            phone_context=sized.phone_context,
            pool_context=sized.pool_context,
            sizing_warning=sizing_warning,
            committed_gb=committed,
            budget_gb=budget,
        )
        if not commit:
            return plan

        # --- 7. Commit: book the memory, then hand the incumbent over --------------------
        # Publishing the reservation is what makes the lock-free spawn phase safe for
        # everybody else: from here on the memory and the port are visible to every other
        # thread's arithmetic, tens of seconds before Popen returns.
        plan.reservation = {
            "reserved_gb": reserved_gb,
            "port": assigned_port,
            "done": threading.Event(),
            "cancelled": False,
            "retired": False,
            "waiters": 0,
            "handoffs": 0,
        }
        self._starting[model_path] = plan.reservation

        # The reservation is published FIRST and the incumbent detached SECOND, so the total
        # committed figure never dips below the truth for even an instant.
        if stale_entry is not None:
            plan.stale_handle = self._detach_entry_locked(model_path, stale_entry)

        return plan

    def can_admit(
        self,
        model_path: str,
        total_vram_gb: float,
        total_sysram_gb: float,
        rpc_endpoints: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        """
        Answers "would :meth:`start_model` admit this model right now?" without starting it.

        Args:
            model_path: the model tag, exactly as it would be passed to ``start_model``.
            total_vram_gb: pooled VRAM reported by the registry.
            total_sysram_gb: pooled system RAM reported by the registry.
            rpc_endpoints: optional — the topology the caller intends to pass to
                ``start_model``. Supply it when you know it (the coordinator's auto-deploy
                gate does) and the answer will account for the restart a changed topology
                forces. Omit it and a running engine is accepted as it stands.

        Returns:
            ``(True, ADMISSION_OK_REASON)`` or ``(False, "<reason>")``, where the reason is one of the
            module's message sentinels: ``NETWORK_MAXED_OUT_MESSAGE``,
            ``NO_FREE_PORT_MESSAGE``, or the model-not-on-disk message (recognise it with
            :func:`is_model_not_on_disk` rather than by substring).

        This is the authoritative gate. The coordinator's auto-deploy path used to run its
        own weaker one — weights only, against the raw uncorrected pool, with no safety
        fraction, no already-committed term and no KV term — and this replaced it. It now
        goes further: it runs literally the same function ``start_model`` runs, in dry-run
        mode, so the two cannot drift apart again. See :meth:`_plan_start_locked`.

        A model that is already running with the requested topology, or whose start is
        already in flight, is admissible: its reservation is already booked, so
        ``start_model`` would return "Already running" rather than take any new memory.

        BLOCKING: takes ``_state_lock`` and performs a handful of stats, one small JSON read
        and at least one loopback bind probe. Call :meth:`acan_admit` from async code.
        """
        self._warm_media_hardware()
        with self._state_lock:
            plan = self._plan_start_locked(
                model_path,
                sorted(set(rpc_endpoints)) if rpc_endpoints is not None else None,
                total_vram_gb,
                total_sysram_gb,
                commit=False,
            )

        if plan.outcome == PLAN_REJECT:
            return False, plan.message
        return True, ADMISSION_OK_REASON

    def start_model(
        self,
        model_path: str,
        rpc_endpoints: List[str],
        total_vram_gb: float,
        total_sysram_gb: float,
        queue_channels: int = 2,
    ) -> Tuple[bool, int, str]:
        """
        Attempts to start the model.

        Returns: (Success: bool, Port: int, Error/Status Message: str)

        CONCURRENCY. This is called on worker threads (via :meth:`astart_model`) once per
        chat request, so several invocations overlap routinely. The function is split into
        two phases:

        1. **Decide, under ``_state_lock``.** One call to :meth:`_plan_start_locked` — the
           same function ``can_admit`` calls — deduplicates against a running or in-flight
           instance, resolves the path, runs admission, sizes the KV cache, allocates the
           port, publishes a reservation into ``_starting`` and detaches any engine that has
           to be replaced. Nothing here blocks for longer than a few stats, a small JSON read
           and a bind probe.
        2. **Spawn, with the lock released.** Stop the engine being replaced (if any), fetch
           the llama.cpp binary, ``Popen``. The reservation published in phase 1 already
           accounts for the memory and the port, so a concurrent admission cannot be sized
           against a pool that looks emptier than it is, and a concurrent start of the same
           model waits for this one instead of spawning a duplicate that would orphan our
           process handle.

        Phase 2 re-takes the lock to publish the entry, and honours a cancellation flag set
        by a teardown that ran in between, so ``stop_all_models`` can never leave a live
        engine behind that nobody has a handle to.
        """
        model_path = canonical_model_tag(model_path)
        # Deterministic, deduplicated topology. Sorting here (and in the registry) means an
        # unchanged set of workers never looks like a change just because the registry
        # iterated its dict in a different order.
        requested_endpoints = sorted(set(rpc_endpoints or []))

        handoff_deadline = time.monotonic() + START_HANDOFF_TIMEOUT_SECONDS
        self._warm_media_hardware()

        # ------------------------------------------------------------------------------
        # Phase 1: decide and reserve.
        #
        # The loop exists for the "another thread is already starting this exact model"
        # case: we park on its completion event (WITHOUT holding the lock, which is what
        # keeps atexit and every other model moving) and then re-evaluate from scratch,
        # because by then the model may be running, may have failed, or may need a restart
        # for a different topology.
        # ------------------------------------------------------------------------------
        while True:
            if time.monotonic() > handoff_deadline:
                log_error(
                    f"Gave up after {START_HANDOFF_TIMEOUT_SECONDS:.0f}s waiting for a "
                    f"concurrent start of '{model_path}' to finish."
                )
                return False, 0, START_HANDOFF_TIMEOUT_MESSAGE

            with self._state_lock:
                plan = self._plan_start_locked(
                    model_path,
                    requested_endpoints,
                    total_vram_gb,
                    total_sysram_gb,
                    commit=True,
                    queue_channels=queue_channels,
                )

                if plan.outcome == PLAN_IN_FLIGHT:
                    # Someone else owns the start for this exact model. "waiters" is how many
                    # threads are parked right now; "handoffs" is the cumulative count and is
                    # what the completion log line reports, since a thread parks repeatedly
                    # while it polls.
                    in_flight = plan.in_flight
                    in_flight["waiters"] += 1
                    in_flight["handoffs"] += 1
                    waiter = in_flight["done"]

            if plan.outcome == PLAN_ALREADY_RUNNING:
                return True, plan.port, plan.message
            if plan.outcome == PLAN_REJECT:
                return False, 0, plan.message
            if plan.outcome == PLAN_START:
                break

            try:
                waiter.wait(timeout=START_WAIT_POLL_SECONDS)
            finally:
                with self._state_lock:
                    # Only decrement if this is still the same reservation; the owner may
                    # have finished and a brand new start may already be in flight.
                    if self._starting.get(model_path) is in_flight:
                        in_flight["waiters"] -= 1

        # ------------------------------------------------------------------------------
        # Phase 2: spawn. The state lock is NOT held here.
        # ------------------------------------------------------------------------------
        reservation = plan.reservation
        resolved_path = plan.resolved_path
        assigned_port = plan.port
        safe_context = plan.safe_context
        reserved_gb = plan.reserved_gb

        # Stays True until the port becomes owned by something else (a published entry, or
        # a teardown that already dealt with it). The finally block below is the single
        # place that retires the reservation, on every exit path including exceptions.
        release_port = True
        log_file = None
        # Set when the engine we spawned has to be destroyed again because a teardown won the
        # race; the destruction itself happens outside the critical section.
        cancelled_handle = None
        try:
            # An engine being replaced (dead, or sharded over a topology the caller no longer
            # wants) is stopped BEFORE the replacement is spawned, so the two never hold the
            # cluster's memory at the same time. This is up to four seconds of waiting and it
            # happens here, with the lock released, rather than inside phase 1.
            if plan.stale_handle is not None:
                self._check_projector_failure(plan.stale_handle["data"])
                self._stop_and_settle(plan.stale_handle)

            binary_path = select_coordinator_engine()
            if not binary_path:
                return False, 0, "Binary Missing"

            # The slot count the plan settled on: the operator's ENGINE_SLOTS, or as many as
            # the free memory holds at 32,768 tokens each, never fewer than the caller's
            # queue_channels (2 normally, 6 after a traffic spike).
            dynamic_slots = plan.slots
            cache_type = plan.cache_type

            # Apply the explicit user-defined CPU limits
            cpu_limit = get_worker_limits().get('max_cpu_threads', -1)
            if cpu_limit <= 0:
                from engine.hardware import get_cpu_cores
                cpu_limit = get_cpu_cores()

            log_coordinator(
                f"Dynamic Context Scaling ({plan.sizing_mode}): Reserved {safe_context} tokens Global KV Pool across "
                f"{dynamic_slots} parallel request slots ({plan.slot_context} tokens each, "
                f"{cache_type} cache) for {model_path} "
                f"({reserved_gb:.1f}GB of a {plan.budget_gb:.1f}GB pool, "
                f"{plan.committed_gb:.1f}GB already committed)."
            )
            if plan.sizing_warning:
                log_warning(plan.sizing_warning)
            if plan.phone_slots:
                log_coordinator(
                    f"Phone lanes: {plan.phone_slots} extra slots of {plan.phone_context} tokens, "
                    f"{plan.pool_context} tokens in the whole pool."
                )

            remote_rpc_endpoints = []
            for e in (requested_endpoints or []):
                if _endpoint_is_remote(e):
                    try:
                        from engine.reverse_tunnel import reverse_bridge
                        resolved_ep = reverse_bridge.resolve_endpoint(e)
                        if resolved_ep:
                            remote_rpc_endpoints.append(resolved_ep)
                    except Exception:
                        remote_rpc_endpoints.append(e)

            # Distributed micro-batch tuning: smaller ubatch prevents socket stalls over RPC
            batch_size = "1024" if remote_rpc_endpoints else "2048"
            ubatch_size = "256" if remote_rpc_endpoints else "512"

            cmd = [
                binary_path,
                "-m", resolved_path,
                # Answers name the model by its file's name, not by the path llama-server loaded
                # it from (engine/model_alias.py).
                *alias_args(resolved_path),
                "--host", "127.0.0.1",
                "--port", str(assigned_port),
                "-c", str(plan.pool_context or safe_context),
                "-np", str(dynamic_slots + plan.phone_slots), # Multiplex parallel slots dynamically based on active users
                "--threads", str(cpu_limit), # Apply explicit physical core limits for generation
                "--threads-batch", str(cpu_limit), # Apply explicit physical core limits for batching
                "-ngl", "99", # Fully offload all layers to GPU VRAM / RPC backends
                "-b", batch_size, # Optimized batch size
                "-ub", ubatch_size, # Micro-batching for pipelined GPU execution
                "-fa", "on", # Flash Attention
                "-ctk", cache_type, # Quantized Key cache (q8_0; q4_0 from 131,072 tokens a slot)
                "-ctv", cache_type, # Quantized Value cache
                "--cache-reuse", "256", # Reuse KV prompt prefixes across multi-turn agent loops
                # Render prompts with the model's OWN chat template out of the GGUF, rather
                # than llama.cpp's legacy matcher, which recognises a fixed set of templates
                # and silently falls back to ChatML for anything it does not know. Serving a
                # model through another family's delimiters is what leaves tool-trained
                # models emitting bare function-call objects as chat text - see
                # coordinator.summary_is_usable for the damage that did downstream.
                #
                # Safe on a model whose embedded template minja cannot parse: llama-server
                # logs the failure and falls back to ChatML, which is exactly where that
                # model would have landed without this flag.
                "--jinja",
            ]

            cmd.extend(phone_lane_args(plan.phone_slots, plan.slot_context))

            if remote_rpc_endpoints:
                rpc_str = ",".join(remote_rpc_endpoints)
                log_coordinator(f"Distributing model '{os.path.basename(model_path)}' to remote RPC nodes: {rpc_str}")
                cmd.extend(["--rpc", rpc_str])
            elif requested_endpoints:
                log_coordinator(
                    f"Running '{os.path.basename(model_path)}' on local native Vulkan GPUs "
                    f"(bypassing loopback TCP RPC for {requested_endpoints})."
                )

            limits = get_worker_limits()
            # The operator's extra flags (ENGINE_EXTRA_ARGS, allow-listed in config.py), placed
            # after the defaults so they win. A card split or an expert placement written for
            # this host's own GPUs is wrong once volunteers' GPUs join over RPC, so those are left
            # out then; the rest stay (dropping them all turned the coding model's thinking back
            # on whenever a volunteer joined).
            extra = list(limits.get("engine_extra_args") or ())
            if remote_rpc_endpoints:
                kept = engine_args_without_placement(extra)
                if len(kept) < len(extra):
                    log_warning("ENGINE_EXTRA_ARGS card placement left out: the engine is running across volunteers' GPUs.")
                extra = kept
            if extra:
                cmd.extend(extra)
                log_coordinator(f"Engine extra arguments: {' '.join(extra)}")

            # Speculative decoding, with the model's draft companion when it has one.
            dflash_path = resolved_path.replace(".gguf", "_dflash.gguf")
            spec = speculative_args(limits, dflash_path if os.path.exists(dflash_path) else None)
            if spec:
                cmd.extend(spec)
                log_coordinator(f"Speculative decoding for {os.path.basename(model_path)}: {' '.join(spec[spec.index('--spec-type'):])}.")

            if "--model-draft" in spec:
                # Pin draft model strictly to the local GPU (e.g. Vulkan0 or CUDA0) so it NEVER shards
                # across network RPC, but offloads 100% to local GPU hardware for instant proposals.
                remote = [e for e in (requested_endpoints or [])
                          if _endpoint_is_remote(e)]
                if remote and not limits.get("draft_on_rpc", False):
                    from engine.hardware import get_gpu_type
                    gpu_type = get_gpu_type().lower()
                    default_dev = "CUDA0" if gpu_type == "cuda" else "Vulkan0"
                    draft_dev = limits.get("draft_device") or default_dev
                    cmd.extend(["-devd", draft_dev])
                    log_coordinator(
                        f"Draft model pinned to local GPU ({draft_dev}) "
                        f"({len(remote)} remote backend(s) in this pipeline)."
                    )

            # The media card and the vision projector; with MEDIA_GPU and ENGINE_MMPROJ unset, nothing is added.
            devices = _engine_devices(limits, bool(remote_rpc_endpoints))
            media_args, projector = self._media_args(limits, devices, resolved_path, remote=bool(remote_rpc_endpoints))
            cmd.extend(media_args)
            if media_gpu_spec(limits) and not (devices and devices[2]):
                log_warning(
                    "MEDIA_GPU is not used: "
                    + ("the engine is running across volunteers' GPUs." if remote_rpc_endpoints
                       else "it names no card here, or is the only card ALLOWED_GPU_INDICES leaves.")
                )

            # Cheap pre-check: if a shutdown already ran while we were fetching the binary
            # there is no point spawning a process only to kill it a millisecond later.
            with self._state_lock:
                if reservation["cancelled"]:
                    log_warning(
                        f"Start of {os.path.basename(model_path)} was cancelled before the "
                        "engine was spawned."
                    )
                    return False, 0, START_CANCELLED_MESSAGE

            log_coordinator(f"Spawning concurrent engine for '{os.path.basename(model_path)}' on port {assigned_port}...")

            env = os.environ.copy()
            # Prevent AMD RADV Vulkan driver from spilling into slow PCIe GTT; keep strictly in fast HBM
            env.setdefault('RADV_PERFTEST', 'nogttspill')
            allowed_gpu = get_worker_limits().get('allowed_gpu_indices', '-1').lower()
            if devices is not None:
                # A media card, or cards named by PCI address: hardware.engine_gpu_devices has resolved
                # every entry, the media card last so the engine's cards keep the names -ts is written for.
                env['CUDA_VISIBLE_DEVICES'], env['GGML_VK_VISIBLE_DEVICES'] = devices[0], devices[1]
                if not (devices[0] or devices[1]):
                    log_warning("ALLOWED_GPU_INDICES names no card here; the engine runs without a GPU.")
            elif allowed_gpu != '-1' and allowed_gpu != 'none':
                env['CUDA_VISIBLE_DEVICES'] = str(allowed_gpu)
                vk_val = str(allowed_gpu)
                try:
                    from engine.hardware import get_all_gpus
                    all_g = get_all_gpus()
                    req_ids = [int(x.strip()) for x in allowed_gpu.split(',') if x.strip().isdigit()]
                    mapped_vk = []
                    for rid in req_ids:
                        match = next((g for g in all_g if g.get("id") == rid), None)
                        if match and "vk_index" in match:
                            mapped_vk.append(str(match["vk_index"]))
                        else:
                            mapped_vk.append(str(rid))
                    if mapped_vk:
                        vk_val = ",".join(mapped_vk)
                except Exception:
                    pass
                env['GGML_VK_VISIBLE_DEVICES'] = vk_val
            elif allowed_gpu == 'none':
                # Force hide all GPUs
                env['CUDA_VISIBLE_DEVICES'] = ""
                env['GGML_VK_VISIBLE_DEVICES'] = ""

            # Redirect engine process output to a log file instead of discarding it
            log_dir = os.path.join(get_base_dir(), "logs")
            os.makedirs(log_dir, exist_ok=True)
            llama_log_path = os.path.join(log_dir, f"llama_server_{assigned_port}.log")

            try:
                from engine.reverse_tunnel import reverse_bridge
                reverse_bridge.reset_load_counters()
            except Exception:
                pass

            # Using open without with to keep the file descriptor alive for the spawned Popen
            log_offset = os.path.getsize(llama_log_path) if os.path.exists(llama_log_path) else 0
            log_file = open(llama_log_path, "a", encoding="utf-8")
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=log_file,
                env=env
            )

            try:
                model_size_bytes = os.path.getsize(resolved_path) if os.path.exists(resolved_path) else 0
            except OSError:
                model_size_bytes = 0

            entry = {
                "process": process,
                "port": assigned_port,
                "log_file": log_file,
                "safe_context": safe_context,
                "dynamic_slots": dynamic_slots,
                "phone_slots": plan.phone_slots,
                "phone_context": plan.phone_context,
                "endpoints": requested_endpoints,
                "remote_endpoints": list(remote_rpc_endpoints),
                "reserved_gb": reserved_gb,
                "started_at": time.monotonic(),
                "resolved_path": resolved_path,
                "model_size_bytes": model_size_bytes,
                "mmproj_path": projector,
                "log_offset": log_offset,
                "last_io_bytes": 0,
                "last_io_time": time.monotonic(),
                "io_speed_mb_s": 0.0,
                "is_ready_cached": False,
                "log_path": llama_log_path,
                "baseline_vram_bytes": _get_drm_vram_used_bytes(),
            }

            handoff_note = None
            with self._state_lock:
                # From this instant a real process owns the port, whatever happens next.
                self._register_port_process(assigned_port, process)

                if reservation["cancelled"]:
                    # A teardown ran while we were spawning. Publishing now would resurrect a
                    # model the operator just unloaded and, worse, leave a live engine that
                    # stop_all_models has already finished walking past. Hand it straight to
                    # the teardown pipeline instead; the slow half runs below, outside the
                    # lock, and _settle_termination_locked releases the port (or quarantines
                    # the process if it survives SIGKILL).
                    log_warning(
                        f"Start of {os.path.basename(model_path)} was cancelled while the "
                        "engine was spawning; terminating the new process immediately."
                    )
                    cancelled_handle = self._detach_entry_locked(model_path, entry)
                    release_port = False
                    # The handle now carries this model's memory, so the reservation must
                    # stop carrying it too or the pool is charged twice for one engine while
                    # it dies. The reservation itself stays alive until the `finally` below,
                    # which keeps sibling threads parked until the process is really gone.
                    reservation["reserved_gb"] = 0.0
                else:
                    self.running_models[model_path] = entry
                    # The entry now owns the port, so it is no longer "pending" and
                    # reconciliation can see it.
                    self._confirm_port(assigned_port)
                    release_port = False

                    # FINDING R3-8: retire the reservation in the SAME critical section that
                    # publishes the entry. It used to be retired in a second, later
                    # acquisition of the lock, so between the two the model was counted once
                    # as a running entry and once as an in-flight reservation. Any thread
                    # that ran _committed_gb_locked() in that window — the coordinator's
                    # 2-second admission tick, for one — double-counted this model and could
                    # refuse a model that genuinely fitted. The window even contained a disk
                    # write (the "ready for inference" log line below), so it was not
                    # theoretical: it was measured at 41.00 GB committed against a 28.80 GB
                    # budget when the true figure was 20.50 GB.
                    handoff_note = self._retire_reservation_locked(
                        model_path, reservation, release_port=False
                    )

            # Logging is a disk write, so it happens after the lock is dropped, never inside.
            if handoff_note:
                log_coordinator(handoff_note)

            if cancelled_handle is not None:
                self._stop_and_settle(cancelled_handle)
                return False, 0, START_CANCELLED_MESSAGE

            log_coordinator("Engine is running and ready for inference.")
            return True, assigned_port, STARTED_MESSAGE
        except Exception as e:
            # Nothing is running, so the finally block gives the port straight back —
            # otherwise a run of failed spawns would burn through the whole range.
            if log_file is not None:
                try:
                    log_file.close()
                except Exception:
                    pass
            log_error(f"Failed to start llama-server: {e}")
            return False, 0, str(e)
        finally:
            # Retire the reservation on EVERY path. Until this runs the model is still
            # counted as committed and other threads asking for it are still parked.
            self._finish_reservation(model_path, reservation, release_port)

    def _retire_reservation_locked(
        self,
        model_path: str,
        reservation: Dict,
        release_port: bool,
    ) -> Optional[str]:
        """
        Retires an in-flight start reservation and wakes everybody waiting on it.

        Caller must hold ``_state_lock``. Idempotent: the success path calls this from
        inside the publication critical section (finding R3-8) and ``start_model``'s
        ``finally`` then calls it again on the way out, which must be a no-op rather than a
        second port release or a second log line.

        Args:
            model_path: the model whose start has finished, successfully or not.
            reservation: the record published into ``_starting`` by ``start_model``.
            release_port: True when no live process ended up owning the allocated port.

        Returns:
            A message the CALLER must log once the lock is released, or None. Logging is a
            disk write and this method runs inside a critical section, so it does not log
            anything itself.

        The event is set last and unconditionally: a waiter that is never woken would sit
        out the full ``START_HANDOFF_TIMEOUT_SECONDS`` before discovering the truth.
        """
        if reservation.get("retired"):
            return None
        reservation["retired"] = True

        # Identity check, not a key check: a later start for the same model may already
        # have replaced this record.
        if self._starting.get(model_path) is reservation:
            del self._starting[model_path]

        if release_port:
            port = reservation.get("port")
            if isinstance(port, int) and port:
                self._release_port(port)

        note = None
        if reservation.get("handoffs"):
            # Worth saying out loud: it means several chat requests arrived for a model
            # that was not loaded yet and were coalesced onto this single engine start
            # instead of each spawning their own.
            note = (
                f"Coalesced {reservation['handoffs']} concurrent request(s) onto the "
                f"single start of '{os.path.basename(model_path)}'."
            )

        reservation["done"].set()
        return note

    def _finish_reservation(self, model_path: str, reservation: Dict, release_port: bool) -> None:
        """
        Locking wrapper around :meth:`_retire_reservation_locked`.

        This is ``start_model``'s belt-and-braces exit path: it runs from the ``finally``
        block on every route out of the function, including exceptions, and does nothing at
        all when the success path has already retired the reservation in line.
        """
        with self._state_lock:
            note = self._retire_reservation_locked(model_path, reservation, release_port)
        if note:
            log_coordinator(note)

    def stop_all_models(self) -> None:
        """
        Kills all active llama-server processes and releases their ports and log files.

        Two things make this safe against a concurrent start (the old version snapshotted
        the dict, terminated the snapshot and then unconditionally ``.clear()``ed, so any
        model published in between was dropped without terminate, without kill and without
        closing its log file — a live engine holding VRAM with no handle left to stop it):

        * every in-flight start is marked cancelled first, so the thread that owns it kills
          its own process instead of publishing it;
        * the dictionary is drained with ``popitem()`` in a loop under the lock rather than
          cleared, so an entry can only leave the dict by being handed to the teardown
          pipeline.

        LOCK BEHAVIOUR (finding R3-10). The drain above is one short critical section; the
        signals and the waits are then paid for every engine with the lock RELEASED. This
        used to be a single unbroken critical section covering every ``terminate`` and every
        ``wait``, which measured a 12.03 s hold for three wedged engines and blocked an
        unrelated ``can_admit`` for 11.76 s. The drained engines are in ``_terminating`` for
        the whole of the slow phase, so their memory keeps counting and their ports stay off
        the allocator exactly as before.

        This is deliberately a SINGLE drain pass, not a loop-until-empty. A model published
        while the slow phase is running keeps running — which is precisely what happened
        before as well, since a start blocked on the lock simply published a microsecond
        after this method returned. ``stop_all_models`` has never been a barrier against
        future starts and does not pretend to be one now.
        """
        handles: List[Dict] = []
        # (log function, message) pairs, emitted once the lock is released. Logging is a
        # disk write and the rotating handler can even rename the file mid-call, so it has
        # no business inside a critical section this method takes on every shutdown.
        notices: List[Tuple[object, str]] = []

        with self._state_lock:
            # Cancel first: anything spawning right now must not survive this call.
            for pending_model, reservation in list(self._starting.items()):
                reservation["cancelled"] = True
                notices.append((
                    log_warning,
                    f"Shutdown requested while '{os.path.basename(pending_model)}' was still "
                    "starting; its engine will be terminated as soon as it spawns.",
                ))

            # Loop-and-pop, never snapshot-and-clear. _detach_entry_locked's own removal is
            # an identity check, so it is a harmless no-op for the entry popitem() just took.
            while self.running_models:
                model, data = self.running_models.popitem()
                notices.append((
                    log_coordinator,
                    f"Stopping Engine process for {os.path.basename(model)}...",
                ))
                handles.append(self._detach_entry_locked(model, data))

        for emit, message in notices:
            emit(message)

        # The slow part, with the lock released so the rest of the cluster keeps moving.
        for handle in handles:
            self._stop_and_settle(handle)

        with self._state_lock:
            # An engine that survived SIGKILL keeps its port and its reservation; anything
            # that has since died is collected here so the pool is not permanently shrunk.
            self._reap_zombies_locked()

        with self._port_lock:
            self._reconcile_allocated_ports()

    def stop_model(self, model_path: str) -> bool:
        """
        Stops one model. Returns True if it was running or was in the middle of starting.

        Provided so callers have a teardown path that releases the port and closes the log
        file; deleting straight out of ``running_models`` leaks both. A start that is still
        in flight is cancelled, so unloading a model the instant after requesting it cannot
        leave an engine running that nothing is tracking.

        As with ``stop_all_models``, the engine is detached under the lock and stopped with
        the lock released.
        """
        model_path = canonical_model_tag(model_path)
        handle = None
        notices: List[Tuple[object, str]] = []

        with self._state_lock:
            reservation = self._starting.get(model_path)
            if reservation is not None:
                reservation["cancelled"] = True
                notices.append((
                    log_warning,
                    f"Unload requested while '{os.path.basename(model_path)}' was still "
                    "starting; its engine will be terminated as soon as it spawns.",
                ))

            data = self.running_models.get(model_path)
            if data is not None:
                notices.append((
                    log_coordinator,
                    f"Stopping Engine process for {os.path.basename(model_path)}...",
                ))
                handle = self._detach_entry_locked(model_path, data)

        # Same reason as stop_all_models: log outside the critical section, never inside.
        for emit, message in notices:
            emit(message)

        if handle is not None:
            self._stop_and_settle(handle)
            return True

        return reservation is not None

    # ----------------------------------------------------------------------------------
    # Consistent reads
    # ----------------------------------------------------------------------------------

    def snapshot_model(self, model_path: str) -> Optional[Dict]:
        """
        A consistent copy of one model's entry, or None if it is not running.

        ``coordinator.py`` reads ``manager.running_models.get(model_path, {})`` straight off
        the event loop and then makes several decisions from the result — is the process
        alive, what context was it started with, how many parallel slots does it have. Read
        piecemeal like that, a teardown running in another thread turns "the entry exists"
        into "the entry is gone" halfway through the block, and the coordinator ends up
        proxying to a port with nothing behind it or sizing a prompt against default values
        rather than the engine's real ones.

        This returns a shallow copy taken under ``_state_lock``, so the caller gets one
        coherent view of the entry as it existed at a single instant. The ``process`` handle
        is shared rather than copied — it is the live ``Popen`` and polling it is the whole
        point — but the dictionary itself is the caller's, so mutating it cannot corrupt the
        manager's state.

        Returns:
            A dict with the same keys as a ``running_models`` entry (``process``, ``port``,
            ``log_file``, ``safe_context``, ``dynamic_slots``, ``endpoints``,
            ``reserved_gb``), or None.
        """
        with self._state_lock:
            model_path = canonical_model_tag(model_path)
            data = self.running_models.get(model_path)
            if data is None:
                return None
            snapshot = dict(data)

        # Defensive copy of the one mutable member that is not a scalar, so a caller holding
        # a snapshot cannot rewrite the live model's topology by accident.
        snapshot["endpoints"] = list(snapshot.get("endpoints", []))
        return snapshot

    def get_loading_telemetry(self, model_path: Optional[str]) -> Dict:
        """
        Returns live model-loading and distributed shard-sync telemetry for `model_path`.

        Combines:
          1. `llama-server` `/health` endpoint status (`200 OK` vs `503 Loading model`)
          2. Linux DRM sysfs live VRAM allocation (`mem_info_vram_used`)
          3. `llama-server` execution stages parsed directly from its active log
          4. Outbound Reverse RPC Tunnel byte & bandwidth counters (`reverse_bridge`)
        """
        if not model_path:
            return {"is_loading": False}

        model_path = canonical_model_tag(model_path)
        model_name = os.path.basename(model_path)
        with self._state_lock:
            entry = self.running_models.get(model_path)
            if entry is None:
                for k, v in self.running_models.items():
                    if os.path.basename(k) == model_name:
                        entry = v
                        break
            starting = self._starting.get(model_path)
            if starting is None:
                for k, v in self._starting.items():
                    if os.path.basename(k) == model_name:
                        starting = v
                        break
        if entry is None:
            if starting is not None:
                return {
                    "is_loading": True,
                    "model": model_name,
                    "phase": "Allocating distributed GPU compute pool...",
                    "loaded_gb": 0.0,
                    "total_gb": round(float(starting.get("reserved_gb", 0.0) or 0.0), 2),
                    "progress_pct": 1.0,
                    "network_tx_gb": 0.0,
                    "speed_mb_s": 0.0,
                    "eta_seconds": 0,
                    "elapsed_seconds": 1,
                }
            return {"is_loading": False, "model": model_name, "progress_pct": 100.0}

        process = entry.get("process")
        if not self._process_is_alive(process):
            return {"is_loading": False, "model": model_name, "progress_pct": 0.0}

        if entry.get("is_ready_cached"):
            return {"is_loading": False, "model": model_name, "progress_pct": 100.0}

        port = entry.get("port")
        if isinstance(port, int) and port > 0:
            import urllib.request
            import urllib.error
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/health", method="GET")
                with urllib.request.urlopen(req, timeout=0.2) as resp:
                    if resp.status == 200:
                        entry["is_ready_cached"] = True
                        return {"is_loading": False, "model": model_name, "progress_pct": 100.0}
            except urllib.error.HTTPError as he:
                if he.code != 503:
                    pass
            except Exception:
                pass

        # 0. Check engine log stage
        log_path = entry.get("log_path")
        if not log_path and isinstance(port, int):
            log_path = os.path.join(get_base_dir(), "logs", f"llama_server_{port}.log")
        stage_pct, stage_text = _inspect_llama_log_stage(log_path)
        if stage_pct >= 100.0:
            entry["is_ready_cached"] = True
            return {"is_loading": False, "model": model_name, "progress_pct": 100.0}

        now = time.monotonic()
        started_at = float(entry.get("started_at") or now)
        elapsed = max(0.5, now - started_at)
        total_bytes = int(entry.get("model_size_bytes") or 0)
        if total_bytes <= 0:
            resolved = entry.get("resolved_path") or self.resolve_model_path(model_path)
            try:
                total_bytes = os.path.getsize(resolved) if os.path.exists(resolved) else 0
                entry["model_size_bytes"] = total_bytes
            except OSError:
                total_bytes = int(float(entry.get("reserved_gb", 10.0) or 10.0) * (1024**3))

        # 1. Local GPU VRAM delta (via Linux DRM sysfs)
        baseline_vram = int(entry.get("baseline_vram_bytes") or 0)
        current_drm_vram = _get_drm_vram_used_bytes()
        local_vram_bytes = max(0, current_drm_vram - baseline_vram) if baseline_vram > 0 else 0

        # 2. Sample /proc/<pid>/io on Linux
        proc_io_bytes = 0
        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and pid > 0:
            try:
                with open(f"/proc/{pid}/io", "r", encoding="utf-8") as f:
                    rchar_val = 0
                    read_bytes_val = 0
                    for line in f:
                        if line.startswith("rchar:"):
                            rchar_val = int(line.split(":", 1)[1].strip())
                        elif line.startswith("read_bytes:"):
                            read_bytes_val = int(line.split(":", 1)[1].strip())
                    proc_io_bytes = max(read_bytes_val, rchar_val)
            except Exception:
                pass

        # 3. Sample Reverse RPC Tunnel network telemetry
        try:
            from engine.reverse_tunnel import reverse_bridge
            tunnel_stats = reverse_bridge.get_transfer_telemetry()
        except Exception:
            tunnel_stats = {"load_tx_bytes": 0, "load_tx_gb": 0.0, "speed_mb_s": 0.0}

        load_tx_bytes = int(tunnel_stats.get("load_tx_bytes") or 0)
        tunnel_speed_mb_s = float(tunnel_stats.get("speed_mb_s") or 0.0)

        # Combined loaded bytes: local VRAM allocation + remote RPC sync
        hardware_loaded_bytes = local_vram_bytes + load_tx_bytes
        min_stage_bytes = int(total_bytes * (stage_pct / 100.0)) if stage_pct > 0 and total_bytes > 0 else 0
        current_bytes = max(hardware_loaded_bytes, min_stage_bytes, proc_io_bytes)

        if total_bytes > 0:
            current_bytes = min(current_bytes, int(total_bytes * 0.99))

        last_bytes = int(entry.get("last_io_bytes") or 0)
        last_time = float(entry.get("last_io_time") or started_at)
        dt = now - last_time
        io_speed_mb_s = float(entry.get("io_speed_mb_s") or 0.0)
        if dt >= 0.5 and current_bytes >= last_bytes:
            inst_speed = ((current_bytes - last_bytes) / (1024.0 * 1024.0)) / dt
            io_speed_mb_s = round(0.6 * inst_speed + 0.4 * io_speed_mb_s, 2) if io_speed_mb_s > 0 else round(inst_speed, 2)
            entry["last_io_bytes"] = current_bytes
            entry["last_io_time"] = now
            entry["io_speed_mb_s"] = io_speed_mb_s

        avg_speed_mb_s = round((current_bytes / (1024.0 * 1024.0)) / elapsed, 2) if current_bytes > 0 else 0.0
        speed_mb_s = max(tunnel_speed_mb_s, io_speed_mb_s, avg_speed_mb_s)
        if speed_mb_s < 10.0 and stage_pct > 0 and elapsed > 0:
            speed_mb_s = max(speed_mb_s, round((min_stage_bytes / (1024.0 * 1024.0)) / elapsed, 1))

        progress_pct = max(stage_pct, round((current_bytes / total_bytes) * 100.0, 1) if total_bytes > 0 else 0.0)
        remaining_bytes = max(0, total_bytes - current_bytes)
        if speed_mb_s > 0.1 and remaining_bytes > 0:
            eta_seconds = int(remaining_bytes / (speed_mb_s * 1024.0 * 1024.0))
        else:
            eta_seconds = 0

        remote_eps = entry.get("remote_endpoints") or []
        phase = stage_text or (
            f"Syncing distributed shards over RPC ({len(remote_eps)} remote GPU node(s))"
            if remote_eps else "Loading weights into local Vulkan HBM"
        )

        return {
            "is_loading": True,
            "model": model_name,
            "phase": phase,
            "loaded_gb": round(current_bytes / (1024.0**3), 2),
            "total_gb": round(total_bytes / (1024.0**3), 2),
            "progress_pct": progress_pct,
            "network_tx_gb": round(load_tx_bytes / (1024.0**3), 2),
            "speed_mb_s": round(speed_mb_s, 2),
            "eta_seconds": eta_seconds,
            "elapsed_seconds": int(elapsed),
        }

    # ----------------------------------------------------------------------------------
    # Async wrappers
    #
    # EVERY method on this class that takes _state_lock has an 'a'-prefixed wrapper here,
    # and coordinator.py must use the wrapper from any coroutine. The lock is short-held by
    # design (see the note in __init__) but "short" is not "zero": a start that is deciding,
    # a teardown that is settling, or a port scan on an exhausted host all hold it, and an
    # event loop that blocks on it stops serving every other request, WebSocket broadcast
    # and worker heartbeat in the process.
    #
    # The three methods below that do NOT take the lock are wrapped anyway, because they do
    # unbounded filesystem work — realpath, getsize and directory listings against a models
    # directory that may be a network mount. estimate_model_vram_gb in particular is called
    # by the coordinator's monitor loop every two seconds.
    #
    # The synchronous originals all stay exactly as they are, because atexit cannot await
    # anything.
    # ----------------------------------------------------------------------------------

    async def astart_model(self, *args, **kwargs) -> Tuple[bool, int, str]:
        """
        Async wrapper around :meth:`start_model`.

        Signature is intentionally ``*args, **kwargs`` so it forwards whatever start_model
        accepts, and the return value is the identical ``(success, port, message)`` tuple.
        Remember that the message is a control signal: see the sentinel block at the top of
        this module, and use :func:`is_retryable_start_failure` rather than returning HTTP
        500 for a failure that only means another thread won a race.
        """
        return await asyncio.to_thread(self.start_model, *args, **kwargs)

    async def astop_all_models(self) -> None:
        """Async wrapper around :meth:`stop_all_models`; never blocks the event loop."""
        await asyncio.to_thread(self.stop_all_models)

    async def astop_model(self, model_path: str) -> bool:
        """Async wrapper around :meth:`stop_model`."""
        return await asyncio.to_thread(self.stop_model, model_path)

    async def areap_model(self, model_path: str) -> bool:
        """
        Async wrapper around :meth:`reap_model`.

        The chat handler's crash-detection path calls this when it finds a dead engine. It
        was calling the synchronous version straight from the coroutine, which inherited the
        full teardown wait — measured at 11.81 s behind one ``stop_all_models`` before the
        lock split, and still not something an event loop should ever sit through.
        """
        return await asyncio.to_thread(self.reap_model, model_path)

    async def acan_admit(
        self,
        model_path: str,
        total_vram_gb: float,
        total_sysram_gb: float,
        rpc_endpoints: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        """Async wrapper around :meth:`can_admit`."""
        return await asyncio.to_thread(
            self.can_admit, model_path, total_vram_gb, total_sysram_gb, rpc_endpoints
        )

    async def asnapshot_model(self, model_path: str) -> Optional[Dict]:
        """Async wrapper around :meth:`snapshot_model`."""
        return await asyncio.to_thread(self.snapshot_model, model_path)

    async def aestimate_model_vram_gb(self, model_path: str) -> float:
        """
        Async wrapper around :meth:`estimate_model_vram_gb`.

        Takes no lock, but reaches ``resolve_model_path`` -> ``get_worker_limits`` ->
        ``get_config`` (which stats the config file) plus a ``realpath`` per search root and
        a ``getsize``. On a network-mounted models directory any one of those can stall for
        seconds, and the coordinator's monitor loop calls it every two seconds forever.
        """
        return await asyncio.to_thread(self.estimate_model_vram_gb, model_path)

    async def aresolve_model_path(self, tag: str) -> str:
        """Async wrapper around :meth:`resolve_model_path` (realpath + isfile per root)."""
        return await asyncio.to_thread(self.resolve_model_path, tag)

    async def aget_available_models(self, total_vram_gb: float, total_sysram_gb: float):
        """Async wrapper around :meth:`get_available_models` (lists and stats a directory)."""
        return await asyncio.to_thread(
            self.get_available_models, total_vram_gb, total_sysram_gb
        )

    async def aget_loading_telemetry(self, model_path: Optional[str]) -> Dict:
        """Async wrapper around :meth:`get_loading_telemetry`."""
        return await asyncio.to_thread(self.get_loading_telemetry, model_path)
