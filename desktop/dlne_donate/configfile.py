"""
``config.txt``: the engine's settings file, which the running worker re-reads. Editing it is how a slider change reaches
a worker that is already running (the same way the VS Code extension does it, contribution.ts ``setConfigValue``).
"""

import os
import re
from typing import Dict, List, Optional


def set_config_value(text: str, key: str, value: str) -> str:
    """
    ``text`` with ``key`` set to ``value``, the way engine.config.set_config_values does it: keys match in any casing,
    comments, blank lines and other keys are copied through, the first match is edited in place, a duplicate further
    down is dropped (the engine's parser is last-write-wins, so a stale duplicate would override the new value) and a
    missing key is appended. The file's line endings and a leading byte-order mark are handled: the mark is removed
    (the engine would read it as part of the first key's name) and the result ends in exactly one line ending.
    """
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = re.split(r"\r?\n", text.lstrip("﻿"))
    if lines and lines[-1] == "":
        lines.pop()
    wanted = key.strip().upper()
    out: List[str] = []
    written = False
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(raw)
            continue
        name = stripped[: stripped.index("=")].strip()
        if name.upper() != wanted:
            out.append(raw)
        elif not written:
            out.append(f"{name}={value}")
            written = True
    if not written:
        out.append(f"{wanted}={value}")
    return eol.join(out) + eol


def read_config(path: str) -> Dict[str, str]:
    """The config as ``UPPERCASE_KEY -> value``, parsed the way engine.config does (last occurrence wins)."""
    parsed: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read().lstrip("﻿")
    except OSError:
        return parsed
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip():
            parsed[key.strip().upper()] = value.strip()
    return parsed


def update_config(path: str, updates: Dict[str, str]) -> bool:
    """
    Apply ``updates`` to the file at ``path``. Written beside the real file and renamed over it, so the worker never
    reads half a config; Windows refuses a rename over a file another program has open, and then the file is written
    in place. Returns whether the file changed.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            current = handle.read()
    except OSError:
        current = ""
    text = current
    for key, value in updates.items():
        text = set_config_value(text, key, str(value))
    if text == current and os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    scratch = f"{path}.{os.getpid()}.tmp"
    try:
        with open(scratch, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if os.name == "posix":
            os.chmod(scratch, 0o600)
        os.replace(scratch, path)
    except OSError:
        try:
            os.remove(scratch)
        except OSError:
            pass
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return True


def format_gb(value: float) -> str:
    """A number of GB as config.txt writes it: ``16`` or ``12.5``, never ``16.0``."""
    return f"{value:g}"
