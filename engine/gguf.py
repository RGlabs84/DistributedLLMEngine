"""
Minimal GGUF metadata reader — just enough to size a KV cache correctly.

WHY THIS EXISTS
---------------
The coordinator has to decide how long a context it can afford before it starts an engine,
and it used to answer with one shipped constant: 0.5 GB per 8192 tokens, or 64 KB per
token. That number is not conservative for anything in current use. Phi-3-mini, which DLNE
ships as its default model, costs 384 KB per token — six times as much — because it has 32
layers of full multi-head attention with no grouped-query sharing. The coordinator sized a
125,545-token context against 16 GB of VRAM, the real cache wanted 49 GB, the driver spilled
into host memory, and the OOM killer took both the engine and the RPC server. The operator
saw "engine subprocess crashed with error code -9".

The cost is not a property of the machine or of the release. It is a property of the model
file, and the model file states it. So read it from there.

WHAT IS READ
------------
GGUF begins with a magic, a version, a tensor count and a metadata table of typed key/value
pairs; the tensor data follows. Only the metadata table is parsed here, and only until the
handful of keys below have been seen, so the cost is a few kilobytes off the front of the
file regardless of how large the model is.

    <arch>.block_count                 layers, each of which holds its own K and V
    <arch>.attention.head_count_kv     KV heads — the GQA divisor, and the whole reason
                                       two models of identical size can differ 6x
    <arch>.attention.key_length        head dimension, when stated outright
    <arch>.attention.value_length
    <arch>.embedding_length            used with head_count to derive head dimension
    <arch>.attention.head_count        when key_length/value_length are absent

Everything here fails soft. An unreadable, truncated, or unfamiliar file returns None and
the caller falls back to its constant — a wrong context length is a bad day, but refusing to
start a model because its header was unusual is worse.
"""

import os
import struct
from typing import Any, Dict, Optional

GGUF_MAGIC = b"GGUF"

# Value type tags, from the GGUF specification.
_UINT8, _INT8, _UINT16, _INT16, _UINT32, _INT32, _FLOAT32, _BOOL, _STRING, _ARRAY, \
    _UINT64, _INT64, _FLOAT64 = range(13)

# Fixed-width types: tag -> (struct format, byte width).
_SCALARS = {
    _UINT8: ("<B", 1), _INT8: ("<b", 1),
    _UINT16: ("<H", 2), _INT16: ("<h", 2),
    _UINT32: ("<I", 4), _INT32: ("<i", 4),
    _FLOAT32: ("<f", 4), _BOOL: ("<?", 1),
    _UINT64: ("<Q", 8), _INT64: ("<q", 8),
    _FLOAT64: ("<d", 8),
}

# Only the keys below are retained; a 70B model's header carries a 128k-entry tokenizer
# vocabulary as an array, and holding that to read six integers would be absurd.
_WANTED_SUFFIXES = (
    ".block_count",
    ".attention.head_count_kv",
    ".attention.head_count",
    ".attention.key_length",
    ".attention.value_length",
    ".embedding_length",
)

# llama.cpp defaults both cache halves to f16 and DLNE never passes --cache-type-k/v, so
# this is the element width in force. Stated as a constant because the day DLNE starts
# offering quantised caches, this is the line that has to change with it.
KV_ELEMENT_BYTES = 2

# Bytes of KV cache per token, used when the header cannot be read. Chosen to match a
# full multi-head 32-layer model (the expensive shape, e.g. Phi-3-mini) rather than a
# grouped-query one, because underestimating is what causes the out-of-memory kill.
FALLBACK_KV_BYTES_PER_TOKEN = 393_216

# A header this size means the file is not what it claims; bail rather than allocate.
_MAX_HEADER_BYTES = 64 * 1024 * 1024


class _Reader:
    """Sequential reader over an open GGUF file, with a hard ceiling on how far it walks."""

    def __init__(self, handle):
        self._handle = handle

    def raw(self, count: int) -> bytes:
        data = self._handle.read(count)
        if len(data) != count:
            raise ValueError("unexpected end of file")
        return data

    def scalar(self, tag: int) -> Any:
        fmt, width = _SCALARS[tag]
        return struct.unpack(fmt, self.raw(width))[0]

    def string(self) -> str:
        length = struct.unpack("<Q", self.raw(8))[0]
        if length > _MAX_HEADER_BYTES:
            raise ValueError("implausible string length")
        return self.raw(length).decode("utf-8", errors="replace")

    def value(self, tag: int) -> Any:
        """Reads one value of the given type, consuming exactly its bytes."""
        if tag in _SCALARS:
            return self.scalar(tag)
        if tag == _STRING:
            return self.string()
        if tag == _ARRAY:
            element_tag = struct.unpack("<I", self.raw(4))[0]
            count = struct.unpack("<Q", self.raw(8))[0]
            if element_tag in _SCALARS:
                _fmt, width = _SCALARS[element_tag]
                # Read the block whole rather than element by element: a tokenizer's
                # scores array runs to six figures of entries.
                total = count * width
                if total > _MAX_HEADER_BYTES:
                    raise ValueError("implausible array length")
                block = self.raw(total)
                return list(struct.unpack(f"<{count}{_SCALARS[element_tag][0][1:]}", block))
            if element_tag == _STRING:
                # Strings are variable-width, so they must be walked. The values are
                # discarded; only the file position matters.
                for _ in range(count):
                    self.string()
                return []
            raise ValueError(f"unsupported array element type {element_tag}")
        raise ValueError(f"unsupported value type {tag}")


def read_metadata(path: str) -> Optional[Dict[str, Any]]:
    """
    Reads a GGUF file's metadata table, keeping only the architecture keys sizing needs.

    Args:
        path: absolute path to a ``.gguf`` file.

    Returns:
        ``{"general.architecture": str, ...}`` or None if the file is missing, is not
        GGUF, or is malformed. Never raises.
    """
    try:
        if not path or not os.path.isfile(path):
            return None

        with open(path, "rb") as handle:
            reader = _Reader(handle)
            if reader.raw(4) != GGUF_MAGIC:
                return None

            version = struct.unpack("<I", reader.raw(4))[0]
            if version < 2:
                # v1 sized its counts and string lengths as 32-bit. It predates every model
                # DLNE can run, and guessing at it would be worse than falling back.
                return None

            struct.unpack("<Q", reader.raw(8))[0]  # tensor_count, not needed
            kv_count = struct.unpack("<Q", reader.raw(8))[0]
            if kv_count > 1_000_000:
                return None

            metadata: Dict[str, Any] = {}
            for _ in range(kv_count):
                key = reader.string()
                tag = struct.unpack("<I", reader.raw(4))[0]
                value = reader.value(tag)
                if key == "general.architecture" or key.endswith(_WANTED_SUFFIXES):
                    metadata[key] = value

            return metadata
    except (OSError, ValueError, struct.error, MemoryError):
        return None


def _as_int(value: Any) -> Optional[int]:
    """
    Coerces a metadata value to a positive int.

    Per-layer arrays are legal for ``head_count_kv`` — a model may vary attention shape by
    block — and the largest entry is the one that bounds the cache.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, (list, tuple)) and value:
        numbers = [v for v in value if isinstance(v, int) and not isinstance(v, bool) and v > 0]
        return max(numbers) if numbers else None
    return None


def kv_bytes_per_token(metadata: Optional[Dict[str, Any]]) -> Optional[int]:
    """
    Exact KV cache cost of one token, in bytes, or None when the header does not say.

    The cache holds a key and a value for every KV head, in every block::

        (key_length + value_length) * head_count_kv * block_count * element_bytes

    Args:
        metadata: the dict from :func:`read_metadata`.

    Returns:
        Positive byte count, or None if any required field is missing or implausible.
    """
    if not metadata:
        return None

    arch = metadata.get("general.architecture")
    if not isinstance(arch, str) or not arch:
        return None

    layers = _as_int(metadata.get(f"{arch}.block_count"))
    heads_kv = _as_int(metadata.get(f"{arch}.attention.head_count_kv"))
    if not layers or not heads_kv:
        return None

    key_length = _as_int(metadata.get(f"{arch}.attention.key_length"))
    value_length = _as_int(metadata.get(f"{arch}.attention.value_length"))

    if not key_length or not value_length:
        # Not every converter writes them. The classic derivation applies whenever every
        # head is the same width, which is every architecture that omits the explicit keys.
        embedding = _as_int(metadata.get(f"{arch}.embedding_length"))
        heads = _as_int(metadata.get(f"{arch}.attention.head_count"))
        if not embedding or not heads or embedding % heads:
            return None
        head_dim = embedding // heads
        key_length = key_length or head_dim
        value_length = value_length or head_dim

    total = (key_length + value_length) * heads_kv * layers * KV_ELEMENT_BYTES
    # A model costing under 1 KB or over 8 MB per token is not a model DLNE can reason
    # about; treating the header as unreadable is the safe response.
    if not (1024 <= total <= 8 * 1024 * 1024):
        return None
    return total


def kv_bytes_per_token_for_model(path: str) -> int:
    """
    KV cost per token for a model file, falling back to a deliberately expensive default.

    This is the function callers want: it always returns a usable number, and the number it
    invents when the file cannot be read errs toward reserving too much rather than too
    little.
    """
    measured = kv_bytes_per_token(read_metadata(path))
    return measured if measured else FALLBACK_KV_BYTES_PER_TOKEN
