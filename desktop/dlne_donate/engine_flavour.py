"""
Which engine flavour the worker runs, and the CUDA question (docs/release-2.7-contract.md sections 1 and 7). The Python twin of
the extension's vscode-extension/src/worker/engineFlavour.ts. Everything here is pure (no window, no files, no clock of its
own), so it is tested without any of them.

    engine setting   auto (Vulkan until the owner agrees to CUDA, then CUDA where it can run) | vulkan | cuda
    cuda_consent     yes | never | later (ask again after 3 days) | unasked
    the worker says  DLNE_CUDA (may this computer run CUDA?) and DLNE_ENGINE (what runs now)

Nothing is downloaded before the owner has said "Use CUDA"; the worker prints DLNE_CUDA before any engine download, so the
first start of a machine is always on Vulkan and the question is asked while that worker runs.
"""

from typing import Any, Dict, Optional, Tuple

from . import markers

ENGINE_SETTINGS = ("auto", "vulkan", "cuda")
CONSENTS = ("yes", "never", "later", "unasked")
LATER_S = 3 * 24 * 3600                     # "Not now" asks again after three days

# The question, word for word as the extension asks it, and its three answers.
QUESTION_TITLE = "Faster with CUDA?"
QUESTION_TEXT = ("Your NVIDIA GPU can run DLNE about 30-50% faster with the CUDA engine. It is a one-time download of about "
                 "600 MB. Use CUDA?")
ANSWER_USE, ANSWER_LATER, ANSWER_NEVER = "use", "later", "never"
ANSWER_LABELS = {ANSWER_USE: "Use CUDA", ANSWER_LATER: "Not now", ANSWER_NEVER: "Keep Vulkan"}

NOT_CHECKED_YET = "DLNE checks your GPU the first time you donate"

PAUSED_TEXT = "Paused: DLNE in VS Code is using this computer"
WORKER_STILL_TEXT = "Paused: DLNE's worker from VS Code is still running"


def paused_text(reason: str) -> str:
    """What the window and the tooltip say while the worker is held back (``reason``: "editor" or "worker", see vscode_first.py)."""
    return WORKER_STILL_TEXT if reason == "worker" else PAUSED_TEXT


def clean_engine(value: Any) -> str:
    """The saved engine setting, ``auto`` for anything else."""
    return value if value in ENGINE_SETTINGS else "auto"


def consent_from_json(value: Any) -> Tuple[str, int]:
    """``(consent, later_ms)`` from the settings file: ``"yes"``, ``"never"`` or ``{"later": <epoch ms>}``; anything else is unasked."""
    if value in ("yes", "never"):
        return value, 0
    if isinstance(value, dict):
        later = value.get("later")
        if isinstance(later, (int, float)) and not isinstance(later, bool) and later == later and 0 < later < 2 ** 53:
            return "later", int(later)
    return "unasked", 0


def consent_to_json(consent: str, later_ms: int) -> Any:
    """The settings file's form of the consent (the same meanings as the extension's globalState ``dlne.cudaConsent``)."""
    if consent in ("yes", "never"):
        return consent
    if consent == "later" and later_ms > 0:
        return {"later": int(later_ms)}
    return "unasked"


def effective_consent(consent: str, later_ms: int, now_s: float) -> str:
    """The consent as it stands now: a "Not now" whose three days have passed is unasked again."""
    if consent == "later":
        return "later" if later_ms > 0 and now_s * 1000 < later_ms else "unasked"
    return consent if consent in CONSENTS else "unasked"


def question_due(engine: str, consent: str, later_ms: int, cuda: Optional[markers.Cuda], now_s: float) -> bool:
    """
    Whether the CUDA question is asked now: the worker said this computer can run CUDA, the setting is auto, and the owner
    has not answered yet (or said "Not now" and the three days are over).
    """
    return (engine == "auto" and cuda is not None and cuda.capable
            and effective_consent(consent, later_ms, now_s) == "unasked")


def answer_changes(answer: str, now_s: float, choosing_cuda: bool = False, engine: str = "auto") -> Dict[str, Any]:
    """
    The settings fields an answer changes. ``choosing_cuda`` is the owner picking CUDA in the Settings window: Use CUDA then
    also sets the engine to cuda, while Not now and Keep Vulkan leave the engine setting (``engine``) as it was.
    """
    if answer == ANSWER_USE:
        return {"cuda_consent": "yes", "cuda_later_ms": 0, "engine": "cuda" if choosing_cuda else engine}
    if answer == ANSWER_LATER:
        return {"cuda_consent": "later", "cuda_later_ms": int((now_s + LATER_S) * 1000)}
    if answer == ANSWER_NEVER:
        return {"cuda_consent": "never", "cuda_later_ms": 0}
    return {}


def resolve_flavour(engine: str, consent: str, cuda: Optional[markers.Cuda], cuda_failed: bool = False, lends_gpu: bool = True) -> str:
    """
    The flavour to start the worker with (``DLNE_ENGINE_FLAVOR``), resolved exactly as the extension does (engineFlavour.ts
    resolveFlavour): CUDA needs a setting that allows it (auto or cuda), a computer the worker has said can run it, the owner's
    "Use CUDA", no failed CUDA start since the setting or the engine version last changed, and a GPU that is lent at all. Anything
    else is Vulkan. So the very first start of a machine is Vulkan: the worker says what the computer can do before any engine is
    downloaded, and the app restarts it on CUDA when everything agrees.
    """
    if not lends_gpu or cuda_failed or engine == "vulkan":
        return "vulkan"
    return "cuda" if cuda is not None and cuda.capable and consent == "yes" else "vulkan"


def cuda_choice(cuda: Optional[markers.Cuda]) -> Tuple[bool, str]:
    """``(may the CUDA choice be used, the reason when not)`` for the Settings window."""
    if cuda is None:
        return False, NOT_CHECKED_YET
    if not cuda.capable:
        return False, cuda.reason or "this computer cannot run the CUDA engine"
    return True, ""


def flavour_label(version: str, flavour: str) -> str:
    """``2.7.0+cuda``: the version with its flavour as build metadata (display only, never compared)."""
    return f"{version}+{flavour}" if version and flavour in markers.ENGINE_FLAVOURS else version


def engine_line(engine: Optional[markers.EngineRun]) -> str:
    """The window's "running on" line, with the reason when CUDA could not start; "" until the worker has said."""
    if engine is None:
        return ""
    if engine.flavour == "vulkan" and engine.fallback:
        return f"CUDA could not start: {engine.fallback}; running on Vulkan"
    return "Running on CUDA" if engine.flavour == "cuda" else "Running on Vulkan"
