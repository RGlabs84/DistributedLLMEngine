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
    <arch>.full_attention_interval     hybrid models: only every Nth block has attention
    <arch>.ssm.*                       hybrid models: the recurrent (Gated DeltaNet / Mamba)
                                       state each sequence carries instead of a KV cache
    <arch>.nextn_predict_layers        speculative "next-token" blocks, never in the cache

HYBRID MODELS (2.4)
-------------------
Qwen3.5/3.6 (arch ``qwen35``/``qwen35moe``) are 3:1 hybrids: one block in four is full
attention and holds a KV cache, the other three are Gated DeltaNet blocks that keep a
fixed-size recurrent state per sequence however long the context is. Counting every block
as attention (what this module did until 2.4) priced Qwen3.6-35B-A3B at 80 KiB per token
against a real 20 KiB, four times too much, and hid that 32,768 tokens for a user cost
about 0.42 GB of VRAM rather than 1.7 GB. The numbers below were checked against
llama.cpp's own memory breakdown for Qwen3.6-35B-A3B at ``-c 65536 -np 2`` with a q8_0
cache (``context = 805 MiB``): 680 MiB of KV for the ten attention blocks plus 2 x 62.8 MiB
of recurrent state.

The tensor table that follows the key/value table is read as well, because it says what the
header cannot: which blocks really own attention weights, how many bytes sit in routed
expert tensors (the part ``--n-cpu-moe`` moves to system RAM), and how big the token
embedding is (llama.cpp always keeps it in system RAM). Tensor sizes come from consecutive
offsets, since the format stores no size.

Everything here fails soft. An unreadable, truncated, or unfamiliar file returns None and
the caller falls back to its constant — a wrong context length is a bad day, but refusing to
start a model because its header was unusual is worse.
"""

import functools
import os
import re
import struct
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
    ".full_attention_interval",
    ".nextn_predict_layers",
    ".ssm.state_size",
    ".ssm.inner_size",
    ".ssm.conv_kernel",
    ".ssm.group_count",
)
_WANTED_KEYS = ("general.architecture", "general.alignment")

# The element width of an f16 cache, which is what llama.cpp uses when nothing else is
# asked for and what :func:`kv_bytes_per_token` returns by default. Callers that start the
# engine with a quantised cache price it with ``cache_type`` below.
KV_ELEMENT_BYTES = 2

# Bytes per element of the cache types DLNE starts the engine with, as (bytes, elements per
# block): q8_0 packs 32 values into 34 bytes, q4_0 into 18. These are exact; the factors
# of 2 and 4 the sizing code used before understated both caches by 6% and 12%.
KV_CACHE_TYPES: Dict[str, Tuple[int, int]] = {
    "f16": (2, 1),
    "q8_0": (34, 32),
    "q4_0": (18, 32),
}

# The recurrent state of a hybrid model is kept in f32 whatever the KV cache type is.
RECURRENT_STATE_ELEMENT_BYTES = 4

# GGUF's default tensor alignment, used when the header does not state one.
_DEFAULT_ALIGNMENT = 32

# More tensors than this and the file is not what it claims; bail rather than loop.
_MAX_TENSORS = 200_000

# Split models: Model-00001-of-00003.gguf.
_SHARD_RE = re.compile(r"^(?P<stem>.+)-(?P<index>\d{5})-of-(?P<total>\d{5})\.gguf$", re.IGNORECASE)
_ATTENTION_TENSOR_RE = re.compile(r"^blk\.(\d+)\.attn_k\.weight$")
_EXPERT_TENSOR_RE = re.compile(r"^blk\.(\d+)\.ffn_(?:gate|up|down|gate_up)_exps\.")
# Tensors llama.cpp keeps in system RAM whatever -ngl says (input layers are never offloaded).
_HOST_ONLY_TENSORS = ("per_layer_token_embd.weight",)

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


def _read(path: str, want_tensors: bool) -> Optional[Tuple[Dict[str, Any], Optional[Dict[str, int]]]]:
    """
    Parses one GGUF file: the metadata keys sizing needs and, when asked, each tensor's size.

    Returns ``(metadata, tensor_sizes)`` where ``tensor_sizes`` maps tensor name to bytes, or
    None when the tensor table was not requested, is empty, or could not be read (the
    metadata is still returned: a damaged tensor table must not cost the key/value
    sizing). Returns None itself when the file is missing, is not GGUF, or is malformed.
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

            tensor_count = struct.unpack("<Q", reader.raw(8))[0]
            kv_count = struct.unpack("<Q", reader.raw(8))[0]
            if kv_count > 1_000_000:
                return None

            metadata: Dict[str, Any] = {}
            for _ in range(kv_count):
                key = reader.string()
                tag = struct.unpack("<I", reader.raw(4))[0]
                value = reader.value(tag)
                if key in _WANTED_KEYS or key.endswith(_WANTED_SUFFIXES):
                    metadata[key] = value

            if not want_tensors or not 0 < tensor_count <= _MAX_TENSORS:
                return metadata, None

            try:
                entries: List[Tuple[str, int]] = []
                for _ in range(tensor_count):
                    name = reader.string()
                    dimensions = struct.unpack("<I", reader.raw(4))[0]
                    if dimensions > 8:
                        raise ValueError("implausible tensor rank")
                    reader.raw(8 * dimensions)   # the dimensions themselves
                    reader.raw(4)                # the ggml type
                    entries.append((name, struct.unpack("<Q", reader.raw(8))[0]))
                table_end = handle.tell()
            except (ValueError, struct.error, MemoryError):
                return metadata, None

            alignment = _as_int(metadata.get("general.alignment")) or _DEFAULT_ALIGNMENT
            data_start = -(-table_end // alignment) * alignment
            file_size = os.path.getsize(path)

        # The format stores offsets, not sizes: a tensor runs to the start of the next one
        # and the last runs to the end of the file. Alignment padding (under 32 bytes a
        # tensor) is counted with the tensor before it, which is far below anything sizing
        # cares about.
        ordered = sorted(entries, key=lambda entry: entry[1])
        sizes: Dict[str, int] = {}
        for (name, offset), (_next_name, next_offset) in zip(ordered, ordered[1:]):
            sizes[name] = max(0, next_offset - offset)
        last_name, last_offset = ordered[-1]
        sizes[last_name] = max(0, file_size - data_start - last_offset)
        return metadata, sizes
    except (OSError, ValueError, struct.error, MemoryError):
        return None


def read_metadata(path: str) -> Optional[Dict[str, Any]]:
    """
    Reads a GGUF file's metadata table, keeping only the architecture keys sizing needs.

    Args:
        path: absolute path to a ``.gguf`` file.

    Returns:
        ``{"general.architecture": str, ...}`` or None if the file is missing, is not
        GGUF, or is malformed. Never raises.
    """
    parsed = _read(path, want_tensors=False)
    return parsed[0] if parsed else None


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


def scale_kv_bytes(f16_bytes: int, cache_type: str = "f16") -> int:
    """
    An f16 cache cost re-priced for another cache type (``q8_0``, ``q4_0``), rounded up.

    An unknown type is priced as f16, the most expensive, so a typo reserves too much
    rather than too little.
    """
    numerator, denominator = KV_CACHE_TYPES.get(cache_type, KV_CACHE_TYPES["f16"])
    elements = -(-f16_bytes // KV_ELEMENT_BYTES)
    return -(-elements * numerator // denominator)


def _cached_block_count(metadata: Dict[str, Any], arch: str) -> Optional[int]:
    """Blocks that take part in the cache: ``block_count`` less the next-token blocks."""
    blocks = _as_int(metadata.get(f"{arch}.block_count"))
    if not blocks:
        return None
    next_token_blocks = _as_int(metadata.get(f"{arch}.nextn_predict_layers")) or 0
    return blocks - next_token_blocks if blocks > next_token_blocks else blocks


def kv_layer_count(metadata: Optional[Dict[str, Any]],
                   attention_layers: Optional[Iterable[int]] = None) -> Optional[int]:
    """
    How many blocks hold a KV cache. Every block does in a classic transformer; in a hybrid
    only the attention blocks do, and the rest keep a fixed recurrent state instead.

    Where the header says which blocks are attention, that is believed:

    * ``<arch>.full_attention_interval = N`` (Qwen3.5/3.6/Next): block ``i`` is attention
      when ``(i + 1) % N == 0``, which is llama.cpp's own rule;
    * a per-block ``head_count_kv`` array: blocks with 0 KV heads hold no cache.

    ``attention_layers`` (block indexes whose tensors include ``attn_k``) can only RAISE
    that count: it verifies the rule against the tensor table, and a model whose tensors
    show more attention blocks than the rule says is priced by the tensors. It never lowers
    the count, because a naming quirk in some other family must not make the cache look
    cheaper than it is. With no rule in the header every block counts, as before.
    """
    if not metadata:
        return None
    arch = metadata.get("general.architecture")
    if not isinstance(arch, str) or not arch:
        return None

    cached = _cached_block_count(metadata, arch)
    if not cached:
        return None

    stated: List[int] = []
    interval = _as_int(metadata.get(f"{arch}.full_attention_interval"))
    if interval:
        stated.append(sum(1 for block in range(cached) if (block + 1) % interval == 0))

    per_block = metadata.get(f"{arch}.attention.head_count_kv")
    if isinstance(per_block, (list, tuple)) and len(per_block) >= cached:
        stated.append(sum(1 for count in per_block[:cached]
                          if isinstance(count, int) and not isinstance(count, bool) and count > 0))

    stated = [count for count in stated if count > 0]
    if not stated:
        return cached

    layers = max(stated)
    if attention_layers is not None:
        layers = max(layers, len({block for block in attention_layers if 0 <= block < cached}))
    return layers


def recurrent_state_bytes(metadata: Optional[Dict[str, Any]], kv_layers: Optional[int] = None) -> int:
    """
    Bytes of recurrent state ONE sequence (one slot) carries in a hybrid model, or 0.

    Gated DeltaNet and Mamba-style blocks keep a state that does not grow with the context,
    in f32, next to the conv window::

        per block = ssm.state_size * ssm.inner_size
                    + (ssm.conv_kernel - 1) * (ssm.inner_size + 2 * ssm.group_count * ssm.state_size)

    which is llama.cpp's ``n_embd_s() + n_embd_r()``. Each slot needs its own copy, so this
    is the price of a slot as the KV cache is the price of its tokens.
    """
    if not metadata:
        return 0
    arch = metadata.get("general.architecture")
    if not isinstance(arch, str) or not arch:
        return 0

    state = _as_int(metadata.get(f"{arch}.ssm.state_size"))
    inner = _as_int(metadata.get(f"{arch}.ssm.inner_size"))
    kernel = _as_int(metadata.get(f"{arch}.ssm.conv_kernel"))
    if not state or not inner or not kernel:
        return 0
    groups = _as_int(metadata.get(f"{arch}.ssm.group_count")) or 1

    cached = _cached_block_count(metadata, arch)
    if kv_layers is None:
        kv_layers = kv_layer_count(metadata)
    if not cached or kv_layers is None or cached <= kv_layers:
        return 0

    per_block = state * inner + (kernel - 1) * (inner + 2 * groups * state)
    return (cached - kv_layers) * per_block * RECURRENT_STATE_ELEMENT_BYTES


def kv_bytes_per_token(metadata: Optional[Dict[str, Any]],
                       attention_layers: Optional[Iterable[int]] = None,
                       cache_type: str = "f16") -> Optional[int]:
    """
    Exact KV cache cost of one token, in bytes, or None when the header does not say.

    The cache holds a key and a value for every KV head, in every block that has attention
    (see :func:`kv_layer_count`)::

        (key_length + value_length) * head_count_kv * attention_blocks * element_bytes

    Args:
        metadata: the dict from :func:`read_metadata`.
        attention_layers: optional block indexes owning ``attn_k`` tensors, to verify the
            header's hybrid rule against the tensor table.
        cache_type: ``f16`` (default), ``q8_0`` or ``q4_0``: the ``-ctk``/``-ctv`` the
            engine will be started with.

    Returns:
        Positive byte count, or None if any required field is missing or implausible.
    """
    if not metadata:
        return None

    arch = metadata.get("general.architecture")
    if not isinstance(arch, str) or not arch:
        return None

    layers = kv_layer_count(metadata, attention_layers)
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
    return scale_kv_bytes(total, cache_type)


# --------------------------------------------------------------------------------------
# What a whole model costs, from its header and tensor table
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelShape:
    """
    Everything memory planning needs to know about one model file, read from the file.

    Byte counts cover all shards of a split model. ``has_tensor_table`` is False when the
    tensor table could not be read, in which case the GPU/CPU split is unknown and every
    byte is treated as GPU-resident (the pessimistic answer).
    """

    arch: str
    file_bytes: int
    kv_layers: int
    kv_f16_bytes_per_token: int
    state_bytes_per_sequence: int = 0
    embedding_bytes: int = 0
    embedding_tied: bool = False
    host_only_bytes: int = 0
    expert_bytes_by_layer: Tuple[Tuple[int, int], ...] = ()
    has_tensor_table: bool = False

    def kv_bytes_per_token(self, cache_type: str = "f16") -> int:
        """KV cost of one token for the cache type the engine will run with."""
        return scale_kv_bytes(self.kv_f16_bytes_per_token, cache_type)

    def experts_on_cpu_bytes(self, cpu_moe_layers: int = 0) -> int:
        """Routed-expert bytes of the first ``cpu_moe_layers`` blocks (``--n-cpu-moe N``)."""
        if cpu_moe_layers <= 0:
            return 0
        return sum(size for layer, size in self.expert_bytes_by_layer if layer < cpu_moe_layers)

    def cpu_bytes(self, cpu_moe_layers: int = 0) -> int:
        """Bytes that live in system RAM: the token embedding plus any experts moved there."""
        return self.embedding_bytes + self.host_only_bytes + self.experts_on_cpu_bytes(cpu_moe_layers)

    def gpu_bytes(self, cpu_moe_layers: int = 0) -> int:
        """
        Bytes that must be resident on the GPUs with ``-ngl 99``.

        The token embedding stays in RAM, except when the output projection is tied to it:
        then the GPU holds the copy the output layer reads and the file's single copy is
        the GPU's.
        """
        host = self.host_only_bytes + (0 if self.embedding_tied else self.embedding_bytes)
        return max(0, self.file_bytes - host - self.experts_on_cpu_bytes(cpu_moe_layers))


def shard_paths(path: str) -> List[str]:
    """
    Every file of a split model (``Name-00001-of-00003.gguf``) when all are present, else
    just ``path``. llama.cpp is given the first shard and finds the rest next to it.
    """
    match = _SHARD_RE.match(os.path.basename(path or ""))
    if not match:
        return [path]
    total = int(match.group("total"))
    if not 1 < total <= 1000:
        return [path]
    directory = os.path.dirname(path)
    shards = [
        os.path.join(directory, f"{match.group('stem')}-{index:05d}-of-{total:05d}.gguf")
        for index in range(1, total + 1)
    ]
    return shards if all(os.path.isfile(shard) for shard in shards) else [path]


def _build_shape(paths: Tuple[str, ...]) -> Optional[ModelShape]:
    parsed = [_read(shard, want_tensors=True) for shard in paths]
    if not parsed or any(entry is None for entry in parsed):
        return None

    metadata = parsed[0][0]
    tensors: Dict[str, int] = {}
    tables = [entry[1] for entry in parsed]
    has_table = all(table for table in tables)
    if has_table:
        for table in tables:
            tensors.update(table)

    attention_layers = None
    if has_table:
        attention_layers = [int(match.group(1)) for name in tensors
                            for match in [_ATTENTION_TENSOR_RE.match(name)] if match]

    kv_per_token = kv_bytes_per_token(metadata, attention_layers)
    kv_layers = kv_layer_count(metadata, attention_layers)
    if not kv_per_token or not kv_layers:
        return None

    try:
        file_bytes = sum(os.path.getsize(shard) for shard in paths)
    except OSError:
        return None

    experts: Dict[int, int] = {}
    if has_table:
        for name, size in tensors.items():
            match = _EXPERT_TENSOR_RE.match(name)
            if match:
                experts[int(match.group(1))] = experts.get(int(match.group(1)), 0) + size

    return ModelShape(
        arch=str(metadata.get("general.architecture")),
        file_bytes=file_bytes,
        kv_layers=kv_layers,
        kv_f16_bytes_per_token=kv_per_token,
        state_bytes_per_sequence=recurrent_state_bytes(metadata, kv_layers),
        embedding_bytes=tensors.get("token_embd.weight", 0),
        embedding_tied=has_table and "output.weight" not in tensors,
        host_only_bytes=sum(tensors.get(name, 0) for name in _HOST_ONLY_TENSORS),
        expert_bytes_by_layer=tuple(sorted(experts.items())),
        has_tensor_table=has_table,
    )


@functools.lru_cache(maxsize=32)
def _model_shape_cached(paths: Tuple[str, ...], stamp: Tuple[Tuple[int, int], ...]) -> Optional[ModelShape]:
    return _build_shape(paths)


def model_shape(path: str) -> Optional[ModelShape]:
    """
    The :class:`ModelShape` of a model file, or None when the header cannot be sized.

    The answer is cached per file (path, size, modification time): admission asks on every
    chat request and on the coordinator's two-second monitor tick, and the tensor table of
    a 35B model is some 700 entries.
    """
    if not path:
        return None
    paths = tuple(shard_paths(path))
    try:
        stamp = tuple((stat.st_size, stat.st_mtime_ns) for stat in map(os.stat, paths))
    except OSError:
        return None
    return _model_shape_cached(paths, stamp)


def kv_bytes_per_token_for_model(path: str, cache_type: str = "f16") -> int:
    """
    KV cost per token for a model file, falling back to a deliberately expensive default.

    This is the function callers want: it always returns a usable number, and the number it
    invents when the file cannot be read errs toward reserving too much rather than too
    little.
    """
    shape = model_shape(path)
    if shape:
        return shape.kv_bytes_per_token(cache_type)
    return scale_kv_bytes(FALLBACK_KV_BYTES_PER_TOKEN, cache_type)
