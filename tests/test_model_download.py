"""
Interrupted model downloads.

WHAT WENT WRONG
---------------
A seed download was killed on 2026-08-01 partway through. The failure handler deletes
partial files, but it only runs for exceptions - a killed process runs nothing at all, so
eight .partN files sat in models/ for nine days. Every run afterwards launched an engine
against a phi3-mini.gguf that had never been assembled, which exited 1 immediately, and the
coordinator respawned it every four seconds. Nothing in the logs said the model was
missing, because nothing had checked.

WHAT IS ASSERTED HERE
---------------------
That a complete-but-unstitched download is recovered rather than re-fetched (2.3GB on a
home connection), that anything else is discarded because a partial range cannot be
resumed, and that a file is only called usable when it actually parses as GGUF.
"""

import os
import struct

import pytest

from engine import coordinator, gguf


# --------------------------------------------------------------------------- helpers


def _write_parts(directory, name, sizes):
    """Writes .partN files whose concatenation is a recognisable byte pattern."""
    dest = os.path.join(str(directory), name)
    for index, size in enumerate(sizes):
        with open(f"{dest}.part{index}", "wb") as handle:
            handle.write(bytes([index % 256]) * size)
    return dest


def _minimal_gguf(path, block_count=2):
    """A GGUF file with no tensors and the keys kv_bytes_per_token needs."""
    def kv_u32(key, value):
        key_bytes = key.encode("utf-8")
        return (struct.pack("<Q", len(key_bytes)) + key_bytes
                + struct.pack("<I", 4) + struct.pack("<I", value))

    def kv_str(key, value):
        key_bytes = key.encode("utf-8")
        value_bytes = value.encode("utf-8")
        return (struct.pack("<Q", len(key_bytes)) + key_bytes
                + struct.pack("<I", 8)
                + struct.pack("<Q", len(value_bytes)) + value_bytes)

    pairs = [
        kv_str("general.architecture", "llama"),
        kv_u32("llama.block_count", block_count),
        kv_u32("llama.attention.head_count_kv", 8),
        kv_u32("llama.attention.key_length", 128),
        kv_u32("llama.attention.value_length", 128),
    ]
    with open(path, "wb") as handle:
        handle.write(b"GGUF")
        handle.write(struct.pack("<I", 3))
        handle.write(struct.pack("<Q", 0))            # tensor_count
        handle.write(struct.pack("<Q", len(pairs)))   # kv_count
        for pair in pairs:
            handle.write(pair)
    return path


# ------------------------------------------------------------------------- stitching


def test_parts_are_concatenated_in_index_order(tmp_path):
    dest = _write_parts(tmp_path, "model.gguf", [4, 4, 4])

    coordinator._stitch_parts(dest, 3)

    assert open(dest, "rb").read() == bytes([0]) * 4 + bytes([1]) * 4 + bytes([2]) * 4


def test_stitching_removes_the_parts_it_consumed(tmp_path):
    dest = _write_parts(tmp_path, "model.gguf", [4, 4])

    coordinator._stitch_parts(dest, 2)

    assert not os.path.exists(f"{dest}.part0")
    assert not os.path.exists(f"{dest}.part1")
    assert not os.path.exists(f"{dest}.tmp")


def test_stitching_replaces_an_existing_file(tmp_path):
    """os.replace, not os.rename - renaming onto an existing path fails on Windows."""
    dest = _write_parts(tmp_path, "model.gguf", [4, 4])
    with open(dest, "wb") as handle:
        handle.write(b"stale")

    coordinator._stitch_parts(dest, 2)

    assert len(open(dest, "rb").read()) == 8


# ---------------------------------------------------------------------- usability


def test_a_real_gguf_is_usable(tmp_path):
    path = _minimal_gguf(str(tmp_path / "model.gguf"))

    assert coordinator.model_file_is_usable(path) is True
    assert gguf.read_metadata(path) is not None


def test_a_missing_file_is_not_usable(tmp_path):
    assert coordinator.model_file_is_usable(str(tmp_path / "absent.gguf")) is False


def test_an_empty_file_is_not_usable(tmp_path):
    path = tmp_path / "model.gguf"
    path.write_bytes(b"")

    assert coordinator.model_file_is_usable(str(path)) is False


def test_a_directory_is_not_usable(tmp_path):
    (tmp_path / "model.gguf").mkdir()

    assert coordinator.model_file_is_usable(str(tmp_path / "model.gguf")) is False


def test_an_html_error_page_saved_as_gguf_is_not_usable(tmp_path):
    """What a CDN hands you when the download really failed."""
    path = tmp_path / "model.gguf"
    path.write_bytes(b"<!doctype html><html><body>404 Not Found</body></html>")

    assert coordinator.model_file_is_usable(str(path)) is False


def test_a_truncated_gguf_is_not_usable(tmp_path):
    """The exact shape of an interrupted download: right magic, missing body."""
    full = _minimal_gguf(str(tmp_path / "full.gguf"))
    truncated = tmp_path / "model.gguf"
    truncated.write_bytes(open(full, "rb").read()[:12])

    assert coordinator.model_file_is_usable(str(truncated)) is False


def test_the_unassembled_parts_case_reports_unusable(tmp_path):
    """
    The regression itself. Parts on disk, no assembled file: the seed check must not
    conclude the model is present.
    """
    _write_parts(tmp_path, "phi3-mini.gguf", [64, 64, 64, 64, 64, 64, 64, 64])

    assert coordinator.model_file_is_usable(str(tmp_path / "phi3-mini.gguf")) is False
