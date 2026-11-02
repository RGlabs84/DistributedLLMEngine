#!/usr/bin/env python3
"""
Download the model file voice (speech to text) needs (2.8.0) into models/voice, verified.

    scripts/fetch_voice_models.py                 into models/voice under the install
    scripts/fetch_voice_models.py --dir DIR       into DIR

The pin is in scripts/voice_models.json: ggml-large-v3-turbo-q8_0.bin by size and SHA-256. The download is the picture
models' (scripts/fetch_image_models.py): a file already in place with the right size and SHA-256 is left alone, a
download goes to <file>.part and carries on where it stopped, and it is moved into place only once its size AND its
SHA-256 match; a file that does not match is deleted and refused.
"""

import argparse
import importlib.util
import json
import os
import sys
from typing import Any, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

PINS = os.path.join(REPO_ROOT, "scripts", "voice_models.json")
DEFAULT_MODEL_SUBDIR = os.path.join("models", "voice")


def _images():
    spec = importlib.util.spec_from_file_location("fetch_image_models", os.path.join(REPO_ROOT, "scripts", "fetch_image_models.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_img = _images()
FetchError = _img.FetchError
sha256_file = _img.sha256_file
verified = _img.verified
fetch_one = _img.fetch_one


def load_pins(pins_path: str = PINS) -> List[Dict[str, Any]]:
    """The pins as ``{role, file, url, sha256, size}``."""
    with open(pins_path, "r", encoding="utf-8") as handle:
        entries = json.load(handle)["models"]
    return [{key: entry[key] for key in ("role", "file", "url", "sha256", "size")} for entry in entries]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", help="where to put the file (default: models/voice under the install)")
    args = parser.parse_args(argv)
    try:
        directory = args.dir
        if not directory:
            from engine.config import get_base_dir
            directory = os.path.join(get_base_dir(), DEFAULT_MODEL_SUBDIR)
        for pin in load_pins():
            fetch_one(pin, directory)
    except (FetchError, OSError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"The voice model is in {directory}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
