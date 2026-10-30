"""
Images in chat requests (2.6): what the coding endpoint accepts before the engine sees them.

A message's ``content`` is a string, or a list of parts: ``{"type": "text", "text": ...}`` and
``{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}`` (``image/png`` or ``image/jpeg``,
spelled exactly so). Only images sent inline are taken: the coordinator never fetches a URL on a client's
behalf (a request that could make this machine open any address would be a way into the owner's network),
so http(s), file and every other scheme are refused. The type of an image is read from its own first bytes,
never from the mime the client declared.

The engine decodes with llama.cpp's vendored stb_image (PNG, JPEG), which allocates whatever a file's
compressed stream expands to, whatever its header says; a 3 MB PNG of one pixel can ask for 2 GiB. So a PNG
is stream-inflated here with a hard output bound taken from its own header (nothing is kept, and no pixel is
converted), and PNG and JPEG are accepted only in the variants stb_image decodes. WebP is not accepted
(2.6.1): stb_image has no WebP decoder, llama.cpp would hand it to an ffmpeg subprocess, and the engine is
started with that path pointed at an empty directory (engine/inference_manager.py), so it could not be read.

Image bytes are never logged. ``describe`` is the one line a log may carry for an image.

Standard library only: the coordinator imports this on every chat request.
"""

import base64
import binascii
import hashlib
import struct
import zlib
from dataclasses import dataclass
from typing import Any, List, Optional

MAX_IMAGES = 4                       # per request, all messages together
MAX_IMAGE_BYTES = 4 * 1024 * 1024    # decoded, each
MAX_SIDE = 4096                      # pixels
MAX_PIXELS = 4096 * 4096             # 16,777,216: the whole 4096-pixel square
# PNG: a file is walked chunk by chunk and its zlib stream inflated in steps, so these keep the walk short.
MAX_PNG_CHUNKS = 16384
MAX_PNG_IDAT_CHUNKS = 8192
MAX_PNG_SIDE_BYTES = 1024 * 1024     # all chunks that are not IDAT together (text, profiles)
PNG_INFLATE_MARGIN = 1024            # bytes an encoder may add past the rows
_INFLATE_STEP = 1 << 20

IMAGE_FORMATS = ("png", "jpeg")       # what _sniff can return: the formats stb_image decodes and this module admits

TOO_MANY_IMAGES = "too_many_images"
IMAGE_TOO_LARGE = "image_too_large"
UNSUPPORTED_IMAGE_TYPE = "unsupported_image_type"
REMOTE_IMAGE_URL = "remote_image_url"
IMAGE_INPUT_UNAVAILABLE = "image_input_unavailable"
IMAGE_QUOTA = "image_quota"
UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"


class ImageError(ValueError):
    """A request's image is refused. ``code`` is one of the constants above, for the client to act on;
    the message is for a person."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ImageInfo:
    format: str     # "png" or "jpeg"
    width: int
    height: int
    sha256: str     # hex of the decoded bytes


def _parts(content: Any) -> List[dict]:
    return [part for part in content if isinstance(part, dict)] if isinstance(content, list) else []


def content_text(content: Any) -> str:
    """The text of a message's content: the string itself, or the text parts of a list joined by newlines."""
    if isinstance(content, str):
        return content
    return "\n".join(str(part.get("text") or "") for part in _parts(content) if part.get("type") == "text")


def _image_parts(content: Any) -> List[dict]:
    return [part for part in _parts(content) if part.get("type") == "image_url"]


def image_count(content: Any) -> int:
    return len(_image_parts(content))


def has_images(messages: Any) -> bool:
    return any(image_count(m.get("content")) for m in (messages or []) if isinstance(m, dict))


def add_text(content: Any, text: str, front: bool = False) -> Any:
    """``content`` with ``text`` put before (``front``) or after what it says; images stay where they are."""
    if isinstance(content, list):
        part = {"type": "text", "text": text}
        return [part] + content if front else content + [part]
    content = content or ""
    return text + content if front else content + text


def tail_content(content: Any, max_chars: int, marker: str) -> Any:
    """``content`` with its text cut down to the last ``max_chars`` characters, led by ``marker``. A list
    keeps its images and gets one text part, first, in place of its text parts."""
    text = marker + (content_text(content)[-max_chars:] if max_chars > 0 else "")
    if isinstance(content, list):
        return [{"type": "text", "text": text}] + _image_parts(content)
    return text


def public_limits(per_hour: int) -> dict:
    """
    The image limits as /v1/cluster/status publishes them (``policy.image_input``), read from the very
    constants the validator enforces so a page and the checks can never disagree. ``per_hour`` is the
    per-network budget (IMAGE_INPUT_PER_HOUR), which is the coordinator's setting, not this module's.
    """
    return {"formats": list(IMAGE_FORMATS), "max_images": MAX_IMAGES, "max_bytes": MAX_IMAGE_BYTES,
            "max_side": MAX_SIDE, "max_pixels": MAX_PIXELS, "per_hour": int(per_hour)}


def describe(info: ImageInfo, noun: str = "image") -> str:
    """The log line for an image: its size and a short fingerprint, nothing of its bytes. ``noun`` is what the
    caller's people call it ("photo", for the phone chat)."""
    return f"[{noun} {info.width}x{info.height} sha256:{info.sha256[:8]}]"


ALLOWED_PART_TYPES = ("text", "image_url")


def check_content_types(messages: Any) -> None:
    """
    Refuses any message content that is not a string (or nothing) or a list of ``text`` and ``image_url``
    parts, whatever the message's role, with or without vision. llama-server also takes ``video_url``,
    ``input_video`` and ``input_audio`` parts and fetches an http(s) URL in them itself, so a part type
    this module does not know is never passed on. Raises :class:`ImageError` (``unsupported_content_type``).
    """
    for message in (messages or []):
        content = message.get("content") if isinstance(message, dict) else None
        if content is None or isinstance(content, str):
            continue
        if not isinstance(content, list):
            raise ImageError(UNSUPPORTED_CONTENT_TYPE, "A message's content must be text or a list of text and image parts.")
        for part in content:
            kind = part.get("type") if isinstance(part, dict) else None
            if kind not in ALLOWED_PART_TYPES:
                raise ImageError(UNSUPPORTED_CONTENT_TYPE, "Only text and inline images can be sent in a message.")


def is_plain_prompt(prompt: Any) -> bool:
    """
    Whether a ``/v1/completions`` prompt is text or tokens: a string, a list of strings and token ids (llama-server's
    mixed form), or a batch of those (a list of such lists, one level deep). Never an object (llama-server reads
    ``{"prompt_string", "multimodal_data"}`` as an image prompt) and never a bool.
    """
    def flat(items: Any) -> bool:
        return isinstance(items, list) and all(
            isinstance(p, str) or (isinstance(p, int) and not isinstance(p, bool)) for p in items)

    if isinstance(prompt, str):
        return True
    return isinstance(prompt, list) and (flat(prompt) or all(flat(inner) for inner in prompt))


def validate_images(messages: Any) -> List[ImageInfo]:
    """
    Checks every image in a request's messages and returns what was found, in order. Raises
    :class:`ImageError` for the first problem: more than ``MAX_IMAGES`` in all, a URL that is not inline
    ``data:image/png|jpeg;base64,`` data, a file that is not a PNG or JPEG stb_image decodes, one over
    ``MAX_IMAGE_BYTES`` / ``MAX_SIDE`` / ``MAX_PIXELS``, or a PNG whose compressed data expands past what
    its own header allows.
    """
    parts = [p for m in (messages or []) if isinstance(m, dict) for p in _image_parts(m.get("content"))]
    if len(parts) > MAX_IMAGES:
        raise ImageError(TOO_MANY_IMAGES, f"At most {MAX_IMAGES} images fit in one request.")
    return [_check_image(part) for part in parts]


# The only spellings of the data URL's header that pass: llama-server matches "data:" case-sensitively and
# then needs an image/ type, so anything looser is an image this module accepts and the engine does not.
_DATA_HEADERS = ("data:image/png;base64", "data:image/jpeg;base64")
_BAD_FORM = "Images must be PNG or JPEG, sent as data:image/png;base64,... or data:image/jpeg;base64,... (lowercase, no spaces)."


def _check_image(part: dict) -> ImageInfo:
    image = part.get("image_url")
    url = image.get("url") if isinstance(image, dict) else None
    if not isinstance(url, str) or not url.strip():
        raise ImageError(UNSUPPORTED_IMAGE_TYPE, "An image has no data.")
    if not url.lstrip()[:5].lower() == "data:":
        raise ImageError(REMOTE_IMAGE_URL, "Images must be sent inline as data: URLs; links are not fetched.")
    header, _, payload = url.partition(",")
    if header not in _DATA_HEADERS:
        raise ImageError(UNSUPPORTED_IMAGE_TYPE, _BAD_FORM)
    # Refused on the encoded length first, so a huge payload is never decoded just to be turned away.
    if len(payload) * 3 // 4 > MAX_IMAGE_BYTES + 3:
        raise ImageError(IMAGE_TOO_LARGE, f"An image is over {MAX_IMAGE_BYTES // (1024 * 1024)} MiB.")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ImageError(UNSUPPORTED_IMAGE_TYPE, "An image is not valid base64.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError(IMAGE_TOO_LARGE, f"An image is over {MAX_IMAGE_BYTES // (1024 * 1024)} MiB.")

    kind, size, png = _sniff(data)
    if size is None:
        raise ImageError(UNSUPPORTED_IMAGE_TYPE, "Images must be PNG or JPEG files.")
    width, height = size
    if width < 1 or height < 1:
        raise ImageError(UNSUPPORTED_IMAGE_TYPE, "An image has no size.")
    if width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
        raise ImageError(IMAGE_TOO_LARGE, f"An image is over {MAX_SIDE} pixels a side.")
    if png is not None:
        _check_png_stream(data, png)
    return ImageInfo(kind, width, height, hashlib.sha256(data).hexdigest())


def _sniff(data: bytes):
    """``(format, (width, height), png)`` read from the file's own header, or ``("", None, None)`` when it
    is not a PNG or JPEG of a kind stb_image decodes. ``png`` is the :class:`_Png` plan for a PNG."""
    try:
        if data.startswith(_PNG_SIGNATURE):
            png = _png_plan(data)
            return ("png", (png.width, png.height), png) if png else ("", None, None)
        if data.startswith(b"\xff\xd8\xff"):
            return "jpeg", _jpeg_size(data), None
    except (struct.error, IndexError):      # a header cut short
        pass
    return "", None, None


# ---------------------------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------------------------

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# colour type -> (channels stored per pixel, the bit depths a PNG may use with it); a palette is one channel
_PNG_COLOR = {0: (1, (1, 2, 4, 8, 16)), 2: (3, (8, 16)), 3: (1, (1, 2, 4, 8)), 4: (2, (8, 16)), 6: (4, (8, 16))}
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))   # xorig, yorig, xspc, yspc


class _Png:
    """What a PNG's own chunks say: its size, how its rows are stored, and where its compressed data is."""

    def __init__(self, width: int, height: int, color: int, depth: int, interlace: int, idat: List[bytes]):
        self.width, self.height, self.color, self.depth, self.interlace, self.idat = width, height, color, depth, interlace, idat

    def raw_size(self) -> int:
        """Bytes the inflated stream holds: every row, with its filter byte, of every pass."""
        channels = _PNG_COLOR[self.color][0]
        passes = _ADAM7 if self.interlace else ((0, 0, 1, 1),)
        total = 0
        for x0, y0, dx, dy in passes:
            across = (self.width - x0 + dx - 1) // dx
            down = (self.height - y0 + dy - 1) // dy
            if across > 0 and down > 0:
                total += (((channels * across * self.depth + 7) >> 3) + 1) * down
        return total


def _png_fail(message: str):
    raise ImageError(UNSUPPORTED_IMAGE_TYPE, message)


def _png_plan(data: bytes) -> Optional[_Png]:
    """
    Walks a PNG's chunks the way stb_image does, refusing what it refuses (an unknown critical chunk, a
    bad IHDR or palette, no IDAT or IEND). Nothing is inflated. Returns None for a PNG whose first chunk
    is not IHDR.
    """
    pos, end = len(_PNG_SIGNATURE), len(data)
    header = None
    palette = 0
    idat: List[bytes] = []
    chunks = side_bytes = 0
    while True:
        if pos + 8 > end:
            _png_fail("This PNG is cut short.")
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = pos + 8
        if length > end or body + length + 4 > end:     # the data and its CRC must be there
            _png_fail("This PNG is cut short.")
        chunks += 1
        if chunks > MAX_PNG_CHUNKS:
            _png_fail("This PNG has too many chunks.")
        if kind != b"IDAT":
            side_bytes += length
            if side_bytes > MAX_PNG_SIDE_BYTES:
                _png_fail("This PNG carries too much besides its picture.")
        if header is None:
            if kind != b"IHDR" or length != 13:
                return None
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data[body:body + 13])
            if color not in _PNG_COLOR or depth not in _PNG_COLOR[color][1]:
                _png_fail("This PNG's colour type or depth is not supported.")
            if compression or filtering or interlace > 1:
                _png_fail("This PNG is corrupt.")
            header = (width, height, color, depth, interlace)
            if width < 1 or height < 1 or width > MAX_SIDE or height > MAX_SIDE:
                # the size limits are applied by the caller, once, with the right message
                return _Png(width, height, color, depth, interlace, [])
        elif kind == b"IHDR":
            _png_fail("This PNG is corrupt.")
        elif kind == b"PLTE":
            if length > 256 * 3 or length % 3 or idat:
                _png_fail("This PNG's palette is corrupt.")
            palette = length // 3
        elif kind == b"tRNS":
            color = header[2]
            if idat:
                _png_fail("This PNG is corrupt.")
            if color == 3:
                if not palette or length > palette:
                    _png_fail("This PNG's transparency is corrupt.")
            elif color not in (0, 2) or length != _PNG_COLOR[color][0] * 2:
                _png_fail("This PNG's transparency is corrupt.")
        elif kind == b"IDAT":
            if header[2] == 3 and not palette:
                _png_fail("This PNG has no palette.")
            if len(idat) >= MAX_PNG_IDAT_CHUNKS:
                _png_fail("This PNG has too many data chunks.")
            idat.append(data[body:body + length])
        elif kind == b"IEND":
            if not idat:
                _png_fail("This PNG has no picture.")
            return _Png(*header, idat)
        elif not kind[0] & 0x20:        # a critical chunk this decoder does not know (CgBI, ...)
            _png_fail("This kind of PNG is not supported.")
        pos = body + length + 4


def _check_png_stream(data: bytes, png: _Png) -> None:
    """
    Inflates the PNG's data in steps, keeping nothing, and refuses when it grows past the size its header
    allows (plus a small margin), stops short, or is not valid zlib. This is the decompression-bomb guard:
    the same stream makes stb_image allocate every byte it expands to.
    """
    expected = png.raw_size()
    limit = expected + PNG_INFLATE_MARGIN
    inflater = zlib.decompressobj()
    total = 0
    pending = b"".join(png.idat)
    try:
        while True:
            produced = inflater.decompress(pending, _INFLATE_STEP)
            total += len(produced)
            if total > limit:
                raise ImageError(IMAGE_TOO_LARGE, "This PNG expands to far more than its size says.")
            pending = inflater.unconsumed_tail
            if inflater.eof or (not produced and not pending):
                break
    except zlib.error:
        _png_fail("This PNG's data is corrupt.")
    if not inflater.eof or total < expected:
        _png_fail("This PNG's data is cut short.")


# ---------------------------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------------------------

def _jpeg_size(data: bytes):
    """
    ``(width, height)`` from the frame header of a JPEG stb_image decodes: baseline or progressive Huffman
    (SOF0, SOF1, SOF2), 8-bit, 1, 3 or 4 components. Arithmetic-coded, lossless, 12-bit and hierarchical
    files, and any segment stb_image does not know before the frame, give None (stb_image refuses them
    and llama.cpp would pass them to ffmpeg).
    """
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:          # fill byte
            pos += 1
            continue
        (length,) = struct.unpack(">H", data[pos + 2:pos + 4])
        if marker in (0xC0, 0xC1, 0xC2):
            return _jpeg_frame_size(data, pos + 4, length)
        if marker == 0xDD and length != 4:
            return None
        if marker in (0xDD, 0xDB, 0xC4, 0xFE) or 0xE0 <= marker <= 0xEF:    # DRI, DQT, DHT, COM, APPn
            if length < 2:
                return None
            pos += 2 + length
            continue
        return None
    return None


def _jpeg_frame_size(data: bytes, pos: int, length: int):
    # precision(1) height(2) width(2) components(1), then id(1) sampling(1) quantisation table(1) each
    precision, height, width, count = struct.unpack(">BHHB", data[pos:pos + 6])
    if precision != 8 or count not in (1, 3, 4) or length != 8 + 3 * count or not height or not width:
        return None
    sampling = [data[pos + 7 + 3 * i] for i in range(count)]
    horizontal = [s >> 4 for s in sampling]
    vertical = [s & 15 for s in sampling]
    if not all(1 <= h <= 4 for h in horizontal) or not all(1 <= v <= 4 for v in vertical):
        return None
    if any(max(horizontal) % h or max(vertical) % v for h, v in zip(horizontal, vertical)):
        return None                 # stb_image's resampler cannot do fractional ratios
    if any(data[pos + 8 + 3 * i] > 3 for i in range(count)):
        return None
    return width, height


# --------------------------------------------------------------------------------------
# Images going out (2.6.1): a generated PNG is cleaned before it leaves the coordinator.
# --------------------------------------------------------------------------------------

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# What a clean PNG keeps: the pixels and what is needed to show them as they were drawn. Everything else is
# dropped by name, so a chunk type nobody has met yet is gone too. stable-diffusion.cpp writes the whole
# prompt and its settings into a tEXt chunk called "parameters"; EXIF, ICC profiles, time stamps and text of
# any kind go the same way.
PNG_KEPT_CHUNKS = frozenset({b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND", b"sRGB", b"gAMA", b"cHRM", b"pHYs"})
MAX_PNG_BYTES = 32 * 1024 * 1024         # what a generated image may take; far above the 768x512 sd writes (well under 1 MiB)


def strip_png_metadata(data: bytes) -> bytes:
    """
    ``data`` (a PNG) with every chunk but :data:`PNG_KEPT_CHUNKS` removed, kept chunks byte for byte and in order,
    anything after IEND dropped. The pixels are never decoded. Raises :class:`ImageError` for a file that is not a
    well-formed PNG (bad signature, a chunk that runs past the end or fails its CRC, no IHDR first, no IDAT, no IEND):
    an image that cannot be cleaned is not sent.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_PNG_BYTES or not data.startswith(PNG_SIGNATURE):
        raise ImageError(UNSUPPORTED_IMAGE_TYPE, "Not a PNG image.")
    out = bytearray(PNG_SIGNATURE)
    pos, seen_idat, seen_iend = len(PNG_SIGNATURE), False, False
    while pos < len(data) and not seen_iend:
        if pos + 8 > len(data):
            raise ImageError(UNSUPPORTED_IMAGE_TYPE, "The PNG ends inside a chunk header.")
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = bytes(data[pos + 4:pos + 8])
        end = pos + 8 + length + 4
        if length > 0x7FFFFFFF or end > len(data):
            raise ImageError(UNSUPPORTED_IMAGE_TYPE, "The PNG has a chunk that runs past its end.")
        if pos == len(PNG_SIGNATURE) and kind != b"IHDR":
            raise ImageError(UNSUPPORTED_IMAGE_TYPE, "The PNG does not start with its header.")
        if kind in PNG_KEPT_CHUNKS:
            (crc,) = struct.unpack(">I", data[end - 4:end])
            if zlib.crc32(data[pos + 4:end - 4]) & 0xFFFFFFFF != crc:
                raise ImageError(UNSUPPORTED_IMAGE_TYPE, "The PNG has a chunk that fails its checksum.")
            out += data[pos:end]
            seen_idat = seen_idat or kind == b"IDAT"
            seen_iend = kind == b"IEND"
        pos = end
    if not (seen_idat and seen_iend):
        raise ImageError(UNSUPPORTED_IMAGE_TYPE, "The PNG has no image data or no end.")
    return bytes(out)


def png_size(data: bytes):
    """``(width, height)`` of a PNG from its IHDR, or None when it is not one. (Only the header is read: the
    validator's _sniff walks the whole file, and an outgoing image has already been cleaned.)"""
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height) if width and height else None
