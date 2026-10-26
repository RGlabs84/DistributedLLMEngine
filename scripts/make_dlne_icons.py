#!/usr/bin/env python3
"""
Make every DLNE icon from the ONE official logo, vscode-extension/media/dlne_logo.png: the
cyan-and-pink brain with "DLNE" under it. Nothing else is a DLNE mark, and nothing is drawn
here; every file written is the logo resized, padded, or (for the Activity Bar) traced.

    ~/.venvs/dlne/bin/python scripts/make_dlne_icons.py

Writes
  engine/static/icon-192.png, icon-512.png     the app icon ("any" purpose)
  engine/static/icon-maskable-512.png          the logo at 80% on its own background colour
  engine/static/apple-touch-icon.png           180 px, opaque (iOS shows transparency as black)
  engine/static/favicon-32.png, favicon-48.png
  engine/static/icons.json                     SHA-256 of the logo and of each icon above
  vscode-extension/media/dlne_activitybar.svg  the brain alone as one-colour silhouette

The phone chat (engine/chat_page.py) serves the PNGs and ships them in the release archive;
tests/test_chat_icons.py checks them against icons.json, so a hand-edited or swapped icon, or a
changed logo whose icons were not regenerated, fails the suite.

Why a silhouette: VS Code draws Activity Bar icons as a mask, one colour taken from the theme,
so a colour image cannot show there. The brain is traced from the logo's own neon lines (the
folds) plus its outline, as vector paths, so it stays crisp at every scaling.

Needs Pillow and numpy, which the engine itself does not.
"""

import collections
import hashlib
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO = os.path.join(REPO_ROOT, "vscode-extension", "media", "dlne_logo.png")
LOGO_RELATIVE = "vscode-extension/media/dlne_logo.png"
STATIC_DIR = os.path.join(REPO_ROOT, "engine", "static")
ACTIVITY_BAR_SVG = os.path.join(REPO_ROOT, "vscode-extension", "media", "dlne_activitybar.svg")

# name -> (pixels, purpose). Mirrors engine/icons.py, which tests/test_chat_icons.py compares.
ICONS = {
    "icon-192.png": (192, "any"),
    "icon-512.png": (512, "any"),
    "icon-maskable-512.png": (512, "maskable"),
    "apple-touch-icon.png": (180, "apple"),
    "favicon-32.png": (32, "favicon"),
    "favicon-48.png": (48, "favicon"),
}

# Android's mask keeps a circle of 80% of the icon's width; the logo goes in at 80% so the brain
# and the text are well inside it.
MASKABLE_SCALE = 0.8


def sha256_file(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def save_png(image, path):
    image.save(path, format="PNG", optimize=True)


# --------------------------------------------------------------------------------------
# The PNG icons
# --------------------------------------------------------------------------------------

def border_colour(logo):
    """The logo's own background: the median of its outermost three rows and columns."""
    pixels = np.asarray(logo)
    ring = np.concatenate([pixels[:3].reshape(-1, 3), pixels[-3:].reshape(-1, 3),
                           pixels[:, :3].reshape(-1, 3), pixels[:, -3:].reshape(-1, 3)])
    return tuple(int(v) for v in np.median(ring, axis=0))


def maskable(logo, size):
    """The logo scaled into the safe zone, on the background colour it already has."""
    inner = round(size * MASKABLE_SCALE)
    canvas = Image.new("RGB", (size, size), border_colour(logo))
    small = logo.resize((inner, inner), Image.LANCZOS)
    # A feather inside the logo's own empty dark margin (the tile's rim starts about 40 px in), so
    # that margin melts into the flat fill instead of leaving a visible square.
    feather = Image.new("L", (inner, inner), 0)
    ImageDraw.Draw(feather).rectangle((10, 10, inner - 11, inner - 11), fill=255)
    feather = feather.filter(ImageFilter.GaussianBlur(5))
    offset = (size - inner) // 2
    canvas.paste(small, (offset, offset), feather)
    return canvas


def make_pngs(logo):
    os.makedirs(STATIC_DIR, exist_ok=True)
    record = {}
    for name, (size, purpose) in ICONS.items():
        icon = maskable(logo, size) if purpose == "maskable" else logo.resize((size, size), Image.LANCZOS)
        path = os.path.join(STATIC_DIR, name)
        save_png(icon, path)
        record[name] = {"size": size, "purpose": purpose, "sha256": sha256_file(path)}
    return record


# --------------------------------------------------------------------------------------
# The Activity Bar silhouette
# --------------------------------------------------------------------------------------

# Where the brain sits in the 512 px logo (above the text, inside the tile's rim).
BRAIN_BOX = (84, 84, 430, 386)
# Loops (outer specks or holes) under this many square units of the 24-unit box are dropped.
MIN_LOOP_AREA = 0.5


def as_image(mask):
    return Image.fromarray((np.asarray(mask) * 255).astype("uint8"))


def components(mask, min_area):
    """The 8-connected regions of ``mask`` that have at least ``min_area`` pixels, as one mask."""
    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    keep = np.zeros_like(mask, dtype=bool)
    for y0 in range(height):
        for x0 in range(width):
            if not mask[y0, x0] or seen[y0, x0]:
                continue
            seen[y0, x0] = True
            queue = collections.deque([(y0, x0)])
            found = []
            while queue:
                y, x = queue.popleft()
                found.append((y, x))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        ny, nx = y + dy, x + dx
                        if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            queue.append((ny, nx))
            if len(found) >= min_area:
                for y, x in found:
                    keep[y, x] = True
    return keep


def fill_holes(mask):
    padded = Image.new("L", (mask.shape[1] + 2, mask.shape[0] + 2), 0)
    padded.paste(as_image(mask), (1, 1))
    ImageDraw.floodfill(padded, (0, 0), 128)
    return np.asarray(padded)[1:-1, 1:-1] != 128


def brain_mask(logo):
    """
    The brain as a solid-with-folds mask, in logo pixels. Three things from the logo:
    the outline (the neon wireframe closed up and filled), the thick neon lines that are
    the folds, and a ring just inside the edge so the outline never breaks.
    """
    left, top, right, bottom = BRAIN_BOX
    brightness = np.asarray(logo).astype(float).max(axis=2)[top:bottom, left:right]
    closed = as_image(brightness > 110).filter(ImageFilter.MaxFilter(15)).filter(ImageFilter.MinFilter(15))
    solid = components(fill_holes(np.asarray(closed) > 0), 20000)   # the brain, not stray rim sparks
    solid = np.asarray(as_image(solid).filter(ImageFilter.MinFilter(11)).filter(ImageFilter.MaxFilter(11))) > 0   # no thin spikes
    blurred = np.asarray(Image.fromarray(brightness.astype("uint8")).filter(ImageFilter.GaussianBlur(2)))
    folds = components(blurred > 140, 600)
    folds = np.asarray(as_image(folds).filter(ImageFilter.MaxFilter(5))) > 0
    ring = solid & ~(np.asarray(as_image(solid).filter(ImageFilter.MinFilter(15))) > 0)
    return (folds | ring) & solid


# Marching squares: the outline of a smooth field as closed loops. Each cell yields up to two
# segments between points on its edges; an edge is named by its two grid corners.
_CASES = {
    1: [("l", "b")], 2: [("b", "r")], 3: [("l", "r")], 4: [("t", "r")], 6: [("t", "b")],
    7: [("l", "t")], 8: [("l", "t")], 9: [("t", "b")], 11: [("t", "r")], 12: [("l", "r")],
    13: [("b", "r")], 14: [("l", "b")],
}


def trace_loops(field, level=127.5):
    """Closed polylines [(x, y), ...] around the regions of ``field`` above ``level``."""
    height, width = field.shape
    padded = np.zeros((height + 2, width + 2))
    padded[1:-1, 1:-1] = field
    height, width = padded.shape

    def edge(kind, y, x):
        return (kind, y, x)

    def point(e):
        kind, y, x = e
        if kind == "h":   # between (y, x) and (y, x + 1)
            a, b = padded[y, x], padded[y, x + 1]
            return (x + (level - a) / (b - a), y)
        a, b = padded[y, x], padded[y + 1, x]   # "v": between (y, x) and (y + 1, x)
        return (x, y + (level - a) / (b - a))

    links = collections.defaultdict(list)
    for y in range(height - 1):
        for x in range(width - 1):
            tl, tr = padded[y, x] >= level, padded[y, x + 1] >= level
            br, bl = padded[y + 1, x + 1] >= level, padded[y + 1, x] >= level
            case = (tl << 3) | (tr << 2) | (br << 1) | bl
            sides = {"t": edge("h", y, x), "b": edge("h", y + 1, x),
                     "l": edge("v", y, x), "r": edge("v", y, x + 1)}
            if case in (5, 10):   # a saddle: the centre decides which corners join
                centre = (padded[y, x] + padded[y, x + 1] + padded[y + 1, x + 1] + padded[y + 1, x]) / 4 >= level
                if case == 5:
                    pairs = [("l", "t"), ("b", "r")] if centre else [("t", "r"), ("l", "b")]
                else:
                    pairs = [("t", "r"), ("l", "b")] if centre else [("l", "t"), ("b", "r")]
            else:
                pairs = _CASES.get(case, [])
            for a, b in pairs:
                links[sides[a]].append(sides[b])
                links[sides[b]].append(sides[a])

    loops, visited = [], set()
    for start in list(links):
        if start in visited:
            continue
        loop, previous, current = [], None, start
        while current not in visited:
            visited.add(current)
            loop.append(point(current))
            options = [n for n in links[current] if n != previous]
            nxt = options[0] if options else links[current][0]
            previous, current = current, nxt
        loops.append(loop)
    return [[(x - 1, y - 1) for x, y in loop] for loop in loops]   # undo the padding


def simplify(points, tolerance):
    """Ramer-Douglas-Peucker on a closed loop, anchored at its first point and the farthest one."""
    def run(chain):
        if len(chain) < 3:
            return chain
        (x0, y0), (x1, y1) = chain[0], chain[-1]
        dx, dy = x1 - x0, y1 - y0
        norm = (dx * dx + dy * dy) ** 0.5 or 1.0
        worst, index = 0.0, 0
        for i in range(1, len(chain) - 1):
            distance = abs(dy * (chain[i][0] - x0) - dx * (chain[i][1] - y0)) / norm
            if distance > worst:
                worst, index = distance, i
        if worst <= tolerance:
            return [chain[0], chain[-1]]
        return run(chain[:index + 1])[:-1] + run(chain[index:])

    far = max(range(len(points)), key=lambda i: (points[i][0] - points[0][0]) ** 2 + (points[i][1] - points[0][1]) ** 2)
    first, second = points[:far + 1], points[far:] + [points[0]]
    return run(first)[:-1] + run(second)[:-1]


def loop_area(points):
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]))) / 2


def activity_bar_svg(logo):
    """One <path> in a 24-unit box, fitted to the brain's own bounding box and centred."""
    mask = brain_mask(logo)
    field = np.asarray(as_image(mask).filter(ImageFilter.GaussianBlur(1.5))).astype(float)
    ys, xs = np.nonzero(mask)
    x_min, x_max, y_min, y_max = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    scale = 22.0 / (x_max - x_min)
    x_off = 1.0 - x_min * scale
    y_off = (24.0 - (y_max - y_min) * scale) / 2 - y_min * scale
    parts = []
    for loop in trace_loops(field):
        loop = [(x * scale + x_off, y * scale + y_off) for x, y in loop]
        if len(loop) < 8 or loop_area(loop) < MIN_LOOP_AREA:
            continue   # a gap or speck smaller than a pixel at 24 px only muddies the picture
        loop = simplify(loop, 0.012)
        if len(loop) >= 3:
            parts.append("M" + " ".join(f"{x:.2f} {y:.2f}" for x, y in loop) + "Z")
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">\n'
            '<!-- The DLNE brain from media/dlne_logo.png as a one-colour silhouette for the Activity Bar\n'
            '     (VS Code uses its alpha as a mask). Traced by scripts/make_dlne_icons.py; not hand-drawn. -->\n'
            '<path fill="#c5c5c5" fill-rule="evenodd" d="' + "".join(parts) + '"/>\n</svg>\n')


# --------------------------------------------------------------------------------------

def main():
    logo = Image.open(LOGO).convert("RGB")
    if logo.size != (512, 512):
        sys.exit(f"{LOGO_RELATIVE} is {logo.size[0]}x{logo.size[1]}; the icons expect the official 512x512 logo.")
    record = {"generated_by": "scripts/make_dlne_icons.py", "source": LOGO_RELATIVE,
              "source_sha256": sha256_file(LOGO), "icons": make_pngs(logo)}
    with open(os.path.join(STATIC_DIR, "icons.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
    with open(ACTIVITY_BAR_SVG, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(activity_bar_svg(logo))
    for name, info in record["icons"].items():
        print(f"{name:24} {info['size']:>3}px  {info['sha256']}")
    print(f"{os.path.relpath(ACTIVITY_BAR_SVG, REPO_ROOT)} written")


if __name__ == "__main__":
    main()
