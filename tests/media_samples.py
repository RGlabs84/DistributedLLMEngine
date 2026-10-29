"""
Image files for the media tests, built byte by byte with the standard library (no image library).

``png`` is a real, valid PNG of any size: every pixel is zero, one bit deep, so the file stays a few kilobytes and
its data inflates to exactly what the header says. ``jpeg`` is a header and a frame header only (the validator reads
a JPEG's frame header; nothing is decoded). ``png_with_stream`` is the way to build a PNG whose data is not what its
header says, for the decompression-bomb tests.
"""

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def png_with_stream(width, height, stream, color=0, depth=1, interlace=0, before=b"", after=b"", idat_split=None):
    """A PNG whose IDAT holds ``stream`` (already zlib data), split into pieces of ``idat_split`` bytes if given."""
    pieces = [stream] if not idat_split else [stream[i:i + idat_split] for i in range(0, len(stream), idat_split)]
    return (PNG_SIGNATURE + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, interlace))
            + before + b"".join(png_chunk(b"IDAT", p) for p in pieces) + after + png_chunk(b"IEND", b""))


def raw_rows(width, height, channels=1, depth=1):
    """The inflated data of an all-zero image: each row a filter byte (0) and its zeroed pixels."""
    return (b"\x00" + bytes(((channels * width * depth) + 7) // 8)) * height


def png(width, height, pad=0, **kwargs):
    """A valid all-zero gray PNG. ``pad`` extra bytes follow the zlib data inside the IDAT (decoders ignore them)."""
    stream = zlib.compress(raw_rows(width, height), 9) + bytes(pad)
    return png_with_stream(width, height, stream, **kwargs)


def jpeg(width, height, marker=0xC0, before=b"", precision=8, components=3, sampling=(0x22, 0x11, 0x11)):
    comp = b"".join(bytes([i + 1, sampling[i % len(sampling)], 0]) for i in range(components))
    frame = bytes([precision]) + struct.pack(">HH", height, width) + bytes([components]) + comp
    segments = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + bytes(9)         # an APP0 to step over
    return b"\xff\xd8" + segments + before + bytes([0xFF, marker]) + struct.pack(">H", len(frame) + 2) + frame
