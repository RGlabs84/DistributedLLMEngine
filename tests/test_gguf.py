"""
Tests for ``engine.gguf`` — reading a model's real KV cache cost out of its own header.

WHAT THIS PROTECTS
------------------
The coordinator used to price every model's KV cache at one shipped constant, 64 KB per
token. Phi-3-mini, DLNE's default model, costs 384 KB per token: 32 layers of full
multi-head attention, no grouped-query sharing. Sizing a 125,545-token context against
16 GB of VRAM therefore asked for a 49 GB cache, the driver spilled into host memory, and
the OOM killer took the engine and the RPC server with it.

The headers below are built byte by byte to the GGUF specification rather than mocked, so
these tests fail if the parser drifts from the format. The two named architectures use the
real published shapes:

    phi3       32 blocks, 32 KV heads, 3072 embedding, 32 heads  -> 384 KB/token
    llama (8B) 32 blocks,  8 KV heads, 4096 embedding, 32 heads  -> 128 KB/token
"""

import struct

import pytest

import engine.gguf as gguf


# GGUF type tags used by the builders below.
T_UINT32 = 4
T_STRING = 8
T_ARRAY = 9
T_FLOAT32 = 6


def _string(text):
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _kv(key, tag, payload):
    return _string(key) + struct.pack("<I", tag) + payload


def build_gguf(entries, version=3, tensor_count=0, magic=gguf.GGUF_MAGIC):
    """
    Assembles a GGUF header.

    Args:
        entries: ``(key, tag, payload_bytes)`` triples, already encoded.
    """
    body = b"".join(_kv(key, tag, payload) for key, tag, payload in entries)
    return (magic
            + struct.pack("<I", version)
            + struct.pack("<Q", tensor_count)
            + struct.pack("<Q", len(entries))
            + body)


def architecture_entries(arch, blocks, heads_kv, embedding, heads):
    return [
        ("general.architecture", T_STRING, _string(arch)),
        (f"{arch}.block_count", T_UINT32, struct.pack("<I", blocks)),
        (f"{arch}.attention.head_count_kv", T_UINT32, struct.pack("<I", heads_kv)),
        (f"{arch}.embedding_length", T_UINT32, struct.pack("<I", embedding)),
        (f"{arch}.attention.head_count", T_UINT32, struct.pack("<I", heads)),
    ]


def write_model(tmp_path, payload, name="model.gguf"):
    path = tmp_path / name
    path.write_bytes(payload)
    return str(path)


# --------------------------------------------------------------------------------------
# The real architectures
# --------------------------------------------------------------------------------------

def test_phi3_mini_costs_384kb_per_token(tmp_path):
    """The model that caused the out-of-memory kill, priced from its own header."""
    path = write_model(tmp_path, build_gguf(
        architecture_entries("phi3", blocks=32, heads_kv=32, embedding=3072, heads=32)))

    assert gguf.kv_bytes_per_token_for_model(path) == 393_216

    # The concrete failure: a 125,545-token context needs 49 GB, not the 7.7 GB the old
    # flat constant claimed.
    needed_gb = 125_545 * 393_216 / (1024 ** 3)
    assert needed_gb == pytest.approx(45.98, abs=0.1)


def test_grouped_query_attention_is_eight_times_cheaper(tmp_path):
    """
    Llama-3-8B shares its KV heads 4:1, and pricing it like Phi-3 would cost most of its
    context for no reason. This is the case a single flat constant cannot serve.
    """
    path = write_model(tmp_path, build_gguf(
        architecture_entries("llama", blocks=32, heads_kv=8, embedding=4096, heads=32)))

    assert gguf.kv_bytes_per_token_for_model(path) == 131_072


def test_explicit_key_and_value_lengths_win_over_the_derivation(tmp_path):
    """
    head_dim is only ``embedding / heads`` when every head is that width. Converters that
    state key_length and value_length outright are describing a model where it is not.
    """
    entries = architecture_entries("deepseek2", blocks=30, heads_kv=4, embedding=2048, heads=16)
    entries += [
        ("deepseek2.attention.key_length", T_UINT32, struct.pack("<I", 192)),
        ("deepseek2.attention.value_length", T_UINT32, struct.pack("<I", 128)),
    ]
    path = write_model(tmp_path, build_gguf(entries))

    # (192 + 128) * 4 heads * 30 blocks * 2 bytes — not the 128 that 2048/16 would give.
    assert gguf.kv_bytes_per_token_for_model(path) == (192 + 128) * 4 * 30 * 2


def test_a_per_layer_head_count_is_bounded_by_its_largest_entry(tmp_path):
    """Some models vary attention shape by block; the cache must fit the widest one."""
    counts = [8, 8, 32, 8]
    entries = architecture_entries("gemma", blocks=4, heads_kv=1, embedding=2048, heads=16)
    entries = [e for e in entries if not e[0].endswith("head_count_kv")]
    entries.append(("gemma.attention.head_count_kv", T_ARRAY,
                    struct.pack("<I", T_UINT32) + struct.pack("<Q", len(counts))
                    + struct.pack(f"<{len(counts)}I", *counts)))
    path = write_model(tmp_path, build_gguf(entries))

    assert gguf.kv_bytes_per_token_for_model(path) == (128 + 128) * 32 * 4 * 2


# --------------------------------------------------------------------------------------
# Parsing the container
# --------------------------------------------------------------------------------------

def test_a_huge_tokenizer_array_is_walked_without_being_kept(tmp_path):
    """
    A real header carries a six-figure token vocabulary ahead of the keys that matter.
    Skipping past it correctly is the difference between reading the file and misparsing
    everything after it.
    """
    vocabulary = [f"token{i}" for i in range(5000)]
    tokens = struct.pack("<I", T_STRING) + struct.pack("<Q", len(vocabulary))
    tokens += b"".join(_string(word) for word in vocabulary)
    scores = struct.pack("<I", T_FLOAT32) + struct.pack("<Q", len(vocabulary))
    scores += struct.pack(f"<{len(vocabulary)}f", *([0.0] * len(vocabulary)))

    entries = [
        ("tokenizer.ggml.tokens", T_ARRAY, tokens),
        ("tokenizer.ggml.scores", T_ARRAY, scores),
    ] + architecture_entries("phi3", blocks=32, heads_kv=32, embedding=3072, heads=32)

    path = write_model(tmp_path, build_gguf(entries))

    metadata = gguf.read_metadata(path)
    assert gguf.kv_bytes_per_token(metadata) == 393_216
    # The vocabulary is walked past, never retained.
    assert not any(key.startswith("tokenizer.") for key in metadata)


@pytest.mark.parametrize("description,payload", [
    ("not a gguf file", b"GGML" + b"\x00" * 64),
    ("truncated mid-header", gguf.GGUF_MAGIC + struct.pack("<I", 3) + b"\x00" * 4),
    ("version 1", b"GGUF" + struct.pack("<I", 1) + b"\x00" * 32),
    ("empty", b""),
])
def test_unreadable_files_return_none_rather_than_raising(tmp_path, description, payload):
    """A model that cannot be parsed must not be a model that cannot be started."""
    path = write_model(tmp_path, payload)

    assert gguf.read_metadata(path) is None
    assert gguf.kv_bytes_per_token_for_model(path) == gguf.FALLBACK_KV_BYTES_PER_TOKEN


def test_a_missing_file_falls_back_instead_of_raising(tmp_path):
    """The model may not be downloaded yet when the coordinator is doing its arithmetic."""
    assert gguf.read_metadata(str(tmp_path / "absent.gguf")) is None
    assert gguf.kv_bytes_per_token_for_model("") == gguf.FALLBACK_KV_BYTES_PER_TOKEN


def test_a_header_missing_the_shape_keys_falls_back(tmp_path):
    """Present and parseable is not the same as complete."""
    path = write_model(tmp_path, build_gguf([
        ("general.architecture", T_STRING, _string("mystery")),
    ]))

    assert gguf.read_metadata(path) == {"general.architecture": "mystery"}
    assert gguf.kv_bytes_per_token_for_model(path) == gguf.FALLBACK_KV_BYTES_PER_TOKEN


def test_an_absurd_result_is_rejected_rather_than_trusted(tmp_path):
    """
    A header can be well-formed and still describe something impossible. Reserving against
    a nonsense number is worse than reserving against the fallback.
    """
    path = write_model(tmp_path, build_gguf(
        architecture_entries("llama", blocks=9999, heads_kv=9999, embedding=4096, heads=32)))

    assert gguf.kv_bytes_per_token(gguf.read_metadata(path)) is None
    assert gguf.kv_bytes_per_token_for_model(path) == gguf.FALLBACK_KV_BYTES_PER_TOKEN


def test_the_fallback_is_expensive_not_average(tmp_path):
    """
    The whole defect was a fallback chosen as an average. It must sit at the costly end:
    reserving too much loses throughput, reserving too little loses the process.
    """
    assert gguf.FALLBACK_KV_BYTES_PER_TOKEN >= 393_216
