#!/usr/bin/env python3
"""
Regenerate every DLNE Android icon asset from the ONE official logo,
vscode-extension/media/dlne_logo.png (a cyan-and-pink glowing brain with "DLNE" under it).

This script is the only place icon art comes from. Nothing else in android/ may draw,
trace or redraw a DLNE mark; app/src/test/.../OfficialIconPolicyTest.kt fails the build's
tests if it does.

What it writes (paths relative to android/):

  app/src/main/res/drawable-nodpi/dlne_logo.png
      A byte-for-byte copy of the official logo. It is the in-app mark (empty state, drawer
      header) and, through the <inset> in mipmap-anydpi/ic_launcher.xml, the adaptive icon's
      foreground (66% of the 108 dp layer, the adaptive-icon safe zone).
  app/src/main/res/drawable-nodpi/ic_launcher_monochrome.png
      The Android 13+ themed-icon layer: the brain alone (no "DLNE" lettering, no tile),
      cut out of the official logo, 432 x 432 px (= 108 dp at xxxhdpi). Only its alpha is used.
  play/icon-512.png
      The official logo as a 512 x 512, 32-bit (RGBA) PNG for the Play listing. Its RGB
      is identical to the official file; alpha is 255 everywhere.
  play/feature-graphic.png
      1024 x 500 (no alpha): official logo on the left, "DLNE Chat" and one line on a dark
      background in the logo's palette.
  tools/official-icon-hashes.txt
      SHA-256 of the official logo and of every generated file; the unit test compares them.

Run (Pillow and numpy needed):  ~/.venvs/dlne/bin/python android/tools/generate_dlne_icons.py
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
ANDROID = HERE.parent
REPO = ANDROID.parent
LOGO = REPO / "vscode-extension" / "media" / "dlne_logo.png"
RES = ANDROID / "app" / "src" / "main" / "res"
PLAY = ANDROID / "play"
HASHES = HERE / "official-icon-hashes.txt"

# Adaptive icon geometry (dp). The layer is 108 dp; Android guarantees only the central
# 66 dp circle is visible under every launcher mask.
LAYER_DP = 108
SAFE_CIRCLE_DP = 66
MONO_PX = 432  # 108 dp at xxxhdpi

NOTO_BOLD = Path("/usr/share/fonts/noto/NotoSans-Bold.ttf")
NOTO_MEDIUM = Path("/usr/share/fonts/noto/NotoSans-Medium.ttf")

# The logo's own palette (sampled from dlne_logo.png): the cyan of the lettering and the
# pink of the right hemisphere; the near-black navy of its backdrop.
CYAN = (102, 254, 254)
PINK = (240, 106, 192)
FEATURE_TITLE = "DLNE Chat"
FEATURE_LINE = "Free AI chat. No account."


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backdrop_colour(logo: Image.Image) -> tuple[int, int, int]:
    """Mean colour of the logo's outer 24 px ring: the dark backdrop around the glass tile."""
    a = np.asarray(logo.convert("RGB")).astype(float)
    ring = np.concatenate(
        [a[:24].reshape(-1, 3), a[-24:].reshape(-1, 3), a[:, :24].reshape(-1, 3), a[:, -24:].reshape(-1, 3)]
    )
    r, g, b = ring.mean(0)
    return round(r), round(g), round(b)


def brain_mask(logo: Image.Image) -> np.ndarray:
    """Float alpha (0..1, 512 x 512) of the brain alone: a solid rim and fold lines over a
    translucent body, so the layer reads as a brain at 48 dp as well as at 108 dp."""
    mx = np.asarray(logo.convert("RGB")).astype(int).max(2)
    region = np.zeros(mx.shape, bool)
    region[60:384, 60:452] = True  # above the "DLNE" lettering, inside the tile frame lines
    lit = ((mx > 140) & region).astype("uint8") * 255
    closed = Image.fromarray(lit).filter(ImageFilter.MaxFilter(11)).filter(ImageFilter.MinFilter(11))
    holes = closed.copy()
    ImageDraw.floodfill(holes, (0, 0), 128)
    filled = Image.fromarray(((np.asarray(holes) != 128) * 255).astype("uint8"))
    seed = (250, 200)  # inside the brain
    assert np.asarray(filled)[seed[1], seed[0]] == 255, "brain seed point is not inside the silhouette"
    only_brain = filled.copy()
    ImageDraw.floodfill(only_brain, seed, 128)  # keeps the brain, drops tile-frame specks
    sil = Image.fromarray((np.asarray(only_brain) == 128).astype("uint8") * 255)
    sil = sil.filter(ImageFilter.GaussianBlur(2.0)).point(lambda v: 255 if v > 127 else 0)
    silhouette = np.asarray(sil) / 255.0
    interior = np.asarray(sil.filter(ImageFilter.MinFilter(25))) / 255.0
    rim = silhouette * (1 - interior)
    lines = np.clip((mx - 100) / 90.0, 0, 1) * silhouette
    lines = np.asarray(Image.fromarray((lines * 255).astype("uint8")).filter(ImageFilter.MaxFilter(3))) / 255.0
    return np.maximum(silhouette * 0.4, np.maximum(rim, lines))


def make_monochrome(logo: Image.Image) -> Image.Image:
    alpha = brain_mask(logo)
    ys, xs = np.where(alpha > 0.02)
    cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
    # Scale so the farthest brain pixel sits at 94% of the safe-circle radius.
    far = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2).max()
    safe_radius_px = SAFE_CIRCLE_DP / 2 * MONO_PX / LAYER_DP
    scale = 0.94 * safe_radius_px / far
    size = round(512 * scale)
    scaled = Image.fromarray((alpha * 255).astype("uint8")).resize((size, size), Image.LANCZOS)
    canvas = Image.new("L", (MONO_PX, MONO_PX), 0)
    canvas.paste(scaled, (round(MONO_PX / 2 - cx * scale), round(MONO_PX / 2 - cy * scale)))
    out = Image.new("RGBA", (MONO_PX, MONO_PX), (255, 255, 255, 0))
    out.putalpha(canvas)
    return out


def make_feature_graphic(logo: Image.Image) -> Image.Image:
    w, h = 1024, 500
    bg = backdrop_colour(logo)
    yy, xx = np.mgrid[0:h, 0:w].astype(float)
    base = np.zeros((h, w, 3), float)
    base[:] = bg
    # Slightly lifted navy towards the right, so the left (where the logo sits) matches the
    # logo's own backdrop and the right has some depth. The bottom edge of the logo's
    # backdrop is a little darker than its top edge, so darken the left-bottom to follow.
    lift = (xx / w) ** 1.5
    base -= (np.clip(1 - xx / 500.0, 0, 1) * np.clip(yy / h, 0, 1))[..., None] * np.array([2.0, 3.0, 5.0])
    base += lift[..., None] * np.array([6.0, 12.0, 22.0])
    # Very soft glows in the logo's two colours.
    def glow(cx, cy, rad, colour, strength):
        d2 = ((xx - cx) ** 2 + (yy - cy) ** 2) / (rad**2)
        return np.exp(-d2)[..., None] * np.array(colour, float) * strength
    base += glow(900, 470, 380, (255, 60, 190), 0.07)
    bgimg = Image.fromarray(np.clip(base, 0, 255).astype("uint8"), "RGB").convert("RGBA")

    # Official logo, left, vertically centred; its outer edge is feathered into the
    # background so the dark backdrop of the square does not show as a box.
    size = 400
    mark = logo.convert("RGBA").resize((size, size), Image.LANCZOS)
    feather = 36  # the tile's own outer edge starts 40 px in (52 of 512 px): keep it untouched
    edge = np.ones((size, size))
    ramp = np.linspace(0, 1, feather) ** 1.2
    edge[:feather, :] *= ramp[:, None]
    edge[-feather:, :] *= ramp[::-1][:, None]
    edge[:, :feather] *= ramp[None, :]
    edge[:, -feather:] *= ramp[::-1][None, :]
    mark.putalpha(Image.fromarray((edge * 255).astype("uint8")))
    bgimg.alpha_composite(mark, (48, (h - size) // 2))

    # Title in a cyan-to-pink gradient, with one short line under it.
    draw_img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    title_font = ImageFont.truetype(str(NOTO_BOLD), 90)
    line_font = ImageFont.truetype(str(NOTO_MEDIUM), 38)
    x0 = 476
    tb = title_font.getbbox(FEATURE_TITLE)
    title_w, title_h = tb[2] - tb[0], tb[3] - tb[1]
    lb = line_font.getbbox(FEATURE_LINE)
    line_w = lb[2] - lb[0]
    assert x0 + max(title_w, line_w) <= w - 56, (title_w, line_w)
    block_h = title_h + 30 + (lb[3] - lb[1])
    ty = (h - block_h) // 2 - tb[1]
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((x0 - tb[0], ty), FEATURE_TITLE, font=title_font, fill=255)
    grad = np.zeros((h, w, 3), np.uint8)
    t = np.clip((np.arange(w) - x0) / max(title_w, 1), 0, 1)[None, :, None]
    grad[:] = (np.array(CYAN) * (1 - t) + np.array(PINK) * t).astype(np.uint8)
    draw_img.paste(Image.fromarray(grad, "RGB"), (0, 0), mask)
    ly = ty + tb[1] + title_h + 30 - lb[1]
    ImageDraw.Draw(draw_img).text((x0 - lb[0], ly), FEATURE_LINE, font=line_font, fill=(196, 214, 232, 255))
    bgimg.alpha_composite(draw_img)
    return bgimg.convert("RGB")  # the feature graphic must not carry alpha


def main() -> int:
    if not LOGO.is_file():
        print(f"official logo missing: {LOGO}", file=sys.stderr)
        return 1
    logo = Image.open(LOGO)
    if logo.size != (512, 512):
        print(f"official logo must be 512 x 512, is {logo.size}", file=sys.stderr)
        return 1

    (RES / "drawable-nodpi").mkdir(parents=True, exist_ok=True)
    in_app = RES / "drawable-nodpi" / "dlne_logo.png"
    shutil.copyfile(LOGO, in_app)

    mono = RES / "drawable-nodpi" / "ic_launcher_monochrome.png"
    make_monochrome(logo).save(mono, optimize=True)

    icon512 = PLAY / "icon-512.png"
    rgba = logo.convert("RGBA")
    rgba.save(icon512, optimize=True)

    feature = PLAY / "feature-graphic.png"
    make_feature_graphic(logo).save(feature, optimize=True)

    lines = ["# SHA-256 of the official logo and of every file generated from it.",
             "# Written by android/tools/generate_dlne_icons.py; checked by OfficialIconPolicyTest.",
             f"{sha256(LOGO)}  vscode-extension/media/dlne_logo.png"]
    for p in (in_app, mono, icon512, feature):
        lines.append(f"{sha256(p)}  android/{p.relative_to(ANDROID).as_posix()}")
    HASHES.write_text("\n".join(lines) + "\n", encoding="utf-8")
    bg = backdrop_colour(logo)
    print("backdrop colour: #%02X%02X%02X" % bg)
    for p in (in_app, mono, icon512, feature):
        print(f"{p.relative_to(ANDROID)}  {p.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
