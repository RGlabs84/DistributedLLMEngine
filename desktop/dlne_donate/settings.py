"""The settings file: one small JSON document in the app-data folder."""

import json
import os
from typing import Any, Dict, Sequence

from . import paths
from .tiers import Hardware, Settings, default_settings, settings_from_json, settings_to_json


def load(hw: Hardware, tiers: Sequence[Dict[str, Any]], path: str = "") -> Settings:
    """The saved settings, clamped to what this machine can honour; the defaults if there is no readable file."""
    path = path or paths.settings_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return default_settings(hw, tiers)
    return settings_from_json(data, hw, tiers)


def save(settings: Settings, path: str = "") -> None:
    """Write beside the real file and rename over it, so a crash never leaves half a file."""
    path = path or paths.settings_path()
    paths.ensure_private_dir(os.path.dirname(path))
    scratch = f"{path}.{os.getpid()}.tmp"
    with open(scratch, "w", encoding="utf-8") as handle:
        json.dump(settings_to_json(settings), handle, indent=2)
        handle.write("\n")
    try:
        os.replace(scratch, path)
    except OSError:
        with open(path, "w", encoding="utf-8") as handle:     # Windows: a rename over an open file is refused
            json.dump(settings_to_json(settings), handle, indent=2)
        try:
            os.remove(scratch)
        except OSError:
            pass
