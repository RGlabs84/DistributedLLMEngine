#!/usr/bin/env python3
"""
Download the three model files image generation needs (2.6.1) into IMAGE_MODEL_DIR, verified.

    scripts/fetch_image_models.py                 into IMAGE_MODEL_DIR from config/config.txt (default models/image)
    scripts/fetch_image_models.py --dir DIR       into DIR

The pins are in scripts/image_models.json (the text encoder is the CPU pool's qwen3-4b from scripts/app_models.json).
A file already in place with the right size and SHA-256 is left alone. A download goes to <file>.part and carries on from
where it stopped if the script is run again; it is only moved into place once its size AND its SHA-256 match. A file
that does not match is deleted and refused: the coordinator never sees a half-checked model.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

PINS = os.path.join(REPO_ROOT, "scripts", "image_models.json")
APP_PINS = os.path.join(REPO_ROOT, "scripts", "app_models.json")
CHUNK = 1024 * 1024


class FetchError(Exception):
    """Something the operator needs to fix. The message is the whole story."""


def load_pins(pins_path: str = PINS, app_path: str = APP_PINS) -> List[Dict[str, Any]]:
    """The three pins as ``{role, file, url, sha256, size}``, the text encoder resolved from the CPU pool's list."""
    with open(pins_path, "r", encoding="utf-8") as handle:
        entries = json.load(handle)["models"]
    pins = []
    for entry in entries:
        if "from_app_models" in entry:
            with open(app_path, "r", encoding="utf-8") as handle:
                tiers = {tier["id"]: tier for tier in json.load(handle)["tiers"]}
            tier = tiers.get(entry["from_app_models"])
            if tier is None:
                raise FetchError(f"{entry['from_app_models']} is not in {app_path}.")
            entry = {**entry, **{key: tier[key] for key in ("file", "url", "sha256", "size")}}
        pins.append({key: entry[key] for key in ("role", "file", "url", "sha256", "size")})
    return pins


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified(path: str, pin: Dict[str, Any]) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) == pin["size"] and sha256_file(path) == pin["sha256"]


def fetch_one(pin: Dict[str, Any], directory: str, opener: Optional[Callable[..., Any]] = None,
              log: Callable[[str], None] = print) -> str:
    """Make ``directory/<file>`` exist and match its pin; return its path. Raises :class:`FetchError` otherwise."""
    opener = opener or urllib.request.urlopen
    os.makedirs(directory, exist_ok=True)
    final, part = os.path.join(directory, pin["file"]), os.path.join(directory, pin["file"] + ".part")
    if verified(final, pin):
        log(f"{pin['file']}: already in place.")
        return final
    if os.path.exists(final):
        os.remove(final)               # there, but not the pinned file
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if have > pin["size"]:
        os.remove(part)
        have = 0
    if have < pin["size"]:
        request = urllib.request.Request(pin["url"], headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with opener(request, timeout=60) as response:
                status = getattr(response, "status", 200)
                if have and status != 206:       # the server sent it all again: start over rather than append to it
                    have = 0
                log(f"{pin['file']}: downloading {pin['size'] - have:,} bytes" + (f" (continuing at {have:,})" if have else "") + ".")
                with open(part, "ab" if have else "wb") as out:
                    for chunk in iter(lambda: response.read(CHUNK), b""):
                        out.write(chunk)
        except OSError as exc:
            raise FetchError(f"{pin['file']}: the download stopped ({type(exc).__name__}); run the script again to continue.") from exc
    size = os.path.getsize(part)
    if size != pin["size"]:
        if size > pin["size"]:
            os.remove(part)              # more than the pin says is not a file to resume
        raise FetchError(f"{pin['file']}: the download is {size:,} bytes, expected {pin['size']:,}; "
                         + ("it was deleted." if size > pin["size"] else "run the script again to continue."))
    if sha256_file(part) != pin["sha256"]:
        os.remove(part)
        raise FetchError(f"{pin['file']}: the download does not match its SHA-256; it was deleted and not used.")
    os.replace(part, final)
    log(f"{pin['file']}: verified.")
    return final


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", help="where to put the files (default: IMAGE_MODEL_DIR, else models/image under the install)")
    args = parser.parse_args(argv)
    try:
        directory = args.dir
        if not directory:
            from engine.config import get_base_dir, get_worker_limits
            from engine.image_member import model_dir
            directory = model_dir(get_worker_limits(), get_base_dir())
        for pin in load_pins():
            fetch_one(pin, directory)
    except (FetchError, OSError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"The image models are in {directory}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
