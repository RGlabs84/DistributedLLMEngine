"""
The DLNE icons a coordinator serves: the ONE official logo (the cyan-and-pink brain with "DLNE"
under it, vscode-extension/media/dlne_logo.png), resized.

Nothing is drawn here or anywhere else on the server. scripts/make_dlne_icons.py cut the files in
``engine/static/`` from the logo once, and ``engine/static/icons.json`` records the SHA-256 of
the logo and of each icon so tests/test_chat_icons.py can prove they still match. The release
archive ships ``engine/static/*.png`` (engine/release.py), so every coordinator has them.

The phone chat serves them at ``/chat/<name>`` (engine/chat_page.py) and the landing page links
to the same URLs, each with ``?v=<ICON_VERSION>``. The version is what lets an icon that changes
reach a phone that cached the old one: the page is never cached, so its new URLs are fetched
afresh, while a URL carrying the right version can be kept for a year.
"""

import hashlib
import os
from typing import Dict, Tuple

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
URL_PREFIX = "/chat/"

# name -> (pixels, purpose). "maskable" is the logo at 80% on its own background so Android's
# mask never crops the brain or the text; "apple" is opaque because iOS shows transparency as black.
ICONS: Dict[str, Tuple[int, str]] = {
    "icon-192.png": (192, "any"),
    "icon-512.png": (512, "any"),
    "icon-maskable-512.png": (512, "maskable"),
    "apple-touch-icon.png": (180, "apple"),
    "favicon-32.png": (32, "favicon"),
    "favicon-48.png": (48, "favicon"),
}


def _load() -> Dict[str, bytes]:
    loaded: Dict[str, bytes] = {}
    for name in ICONS:
        path = os.path.join(STATIC_DIR, name)
        try:
            with open(path, "rb") as handle:
                loaded[name] = handle.read()
        except OSError as exc:
            # Loud on purpose: a release that lost its icons must fail its post-restart health
            # check and roll back (scripts/release_apply.py), not run with a broken logo.
            raise RuntimeError(f"{path} is missing or unreadable ({exc.strerror}). "
                               "The release archive must ship engine/static/*.png (engine/release.py).") from exc
    return loaded


_BYTES = _load()
ICON_VERSION = hashlib.sha256(b"".join(name.encode() + _BYTES[name] for name in sorted(_BYTES))).hexdigest()[:10]


def icon_png(name: str) -> bytes:
    """One of ``ICONS``, as the PNG file that ships."""
    return _BYTES[name]


def icon_url(name: str) -> str:
    """Where a page links the icon: its route plus the version that busts any cached older copy."""
    if name not in ICONS:
        raise KeyError(name)
    return f"{URL_PREFIX}{name}?v={ICON_VERSION}"
