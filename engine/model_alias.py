"""
The name a llama-server reports for its model.

Left to itself llama-server puts ``"model": "<the path it loaded>"`` in every answer: the folder the model
sits in, which for a volunteer's machine is their home directory and their user name. That answer is passed
on, to a phone through the pool and to a developer through a coding member (sealed, but opened by the
requester), so the path must never be the name. ``--alias <name>`` makes llama-server report ``<name>`` in
its answers and list it in ``/v1/models`` instead.

The name is the model file's own name, never a directory: ``Qwen3.6-35B-A3B-UD-IQ3_S.gguf``. It is also what
the coordinator calls the model everywhere else (``canonical_model_tag``), so nothing that matches on a model
name sees a difference. Nothing in DLNE reads the ``model`` of an answer (the extension and the phone apps
name the model from the pool's status, and the pool strips the field from phone answers), so the alias is
for whoever else reads it.

Every launch of llama-server takes its arguments from one of the builders that call :func:`alias_args`:
the coordinator's coding engine (engine.inference_manager), the phone pool's members (engine.app_pool, and
engine.app_member through it) and the coding members (engine.coding_member).
"""

import re
from typing import Any, List

ALIAS_FLAG = "--alias"
DEFAULT_NAME = "model.gguf"

_SEPARATORS = re.compile(r"[\\/]")


def model_alias(model_path: Any) -> str:
    """The model file's name from a path of either kind (a Windows path is cut at ``\\`` on any system)."""
    name = _SEPARATORS.split(str(model_path or ""))[-1]
    # --alias takes a comma-separated list, and a name is a name: no list, no control characters.
    name = "".join(ch for ch in name.replace(",", "_") if ch.isprintable()).strip()
    return name if name and name not in (".", "..") else DEFAULT_NAME


def alias_args(model_path: Any) -> List[str]:
    """``["--alias", <the file's name>]`` for a llama-server loading ``model_path``."""
    return [ALIAS_FLAG, model_alias(model_path)]


def alias_of(args: List[str]) -> Any:
    """The alias in a llama-server argument list, or None when it has none (for the checks around a launch)."""
    if ALIAS_FLAG not in args:
        return None
    index = args.index(ALIAS_FLAG) + 1
    return args[index] if index < len(args) else None


def alias_problem(args: List[str]) -> str:
    """What is wrong with the alias of a llama-server argument list; empty when it is a plain file name."""
    alias = alias_of(args)
    if not alias:
        return "no --alias, so its answers would carry the model's path"
    if _SEPARATORS.search(alias) or "," in alias:
        return "its --alias is not a plain file name"
    return ""
