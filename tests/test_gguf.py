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

import os
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


# --------------------------------------------------------------------------------------
# Hybrid models: only the attention blocks hold a KV cache (2.4)
#
# Qwen3.6-35B-A3B is 40 blocks, one in four full attention (2 KV heads of 256), three in four
# Gated DeltaNet. Pricing all 40 as attention made a user's 32K cost 1.7 GB of VRAM when it
# is 0.42 GB. The numbers below are the model's real header values, and the one hard
# reference is llama.cpp's own memory breakdown for it at ``-c 65536 -np 2`` with a q8_0
# cache: ``context = 805 MiB`` (laptop run, bake-off results q36-iq3s-probe).
# --------------------------------------------------------------------------------------

MIB = 1024 * 1024


def u32_entry(key, value):
    return (key, T_UINT32, struct.pack("<I", value))


def qwen36_entries(**overrides):
    """The header values of Qwen3.6-35B-A3B (arch qwen35moe), as read from the real file."""
    arch = "qwen35moe"
    values = {
        "block_count": 40, "attention.head_count": 16, "attention.head_count_kv": 2,
        "attention.key_length": 256, "attention.value_length": 256, "embedding_length": 2048,
        "full_attention_interval": 4, "ssm.state_size": 128, "ssm.inner_size": 4096,
        "ssm.conv_kernel": 4, "ssm.group_count": 16,
    }
    values.update(overrides)
    entries = [("general.architecture", T_STRING, _string(arch))]
    entries += [u32_entry(f"{arch}.{key}", value) for key, value in values.items() if value is not None]
    return entries


def build_gguf_with_tensors(entries, tensors, alignment=32):
    """
    A complete GGUF file: header, key/values, a tensor table and zero-filled tensor data.

    ``tensors`` is ``[(name, nbytes)]`` with ``nbytes`` a multiple of ``alignment`` so that
    the sizes the reader derives from consecutive offsets are exact.
    """
    infos, offset = b"", 0
    for name, nbytes in tensors:
        assert nbytes % alignment == 0
        # one dimension, ggml type 0; only the offset matters to the reader
        infos += (_string(name) + struct.pack("<I", 1) + struct.pack("<Q", nbytes)
                  + struct.pack("<I", 0) + struct.pack("<Q", offset))
        offset += nbytes
    header = build_gguf(entries, tensor_count=len(tensors)) + infos
    return header + b"\0" * (-len(header) % alignment) + b"\0" * offset


def qwen36_tensors(extra_attention_blocks=(), tied=False):
    """Qwen3.6's tensor layout at toy sizes: expert tensors 320 B each, attention in 3,7,...,39."""
    tensors = [("token_embd.weight", 3200)]
    if not tied:
        tensors.append(("output.weight", 3200))
    for block in range(40):
        for kind in ("up", "down", "gate"):
            tensors.append((f"blk.{block}.ffn_{kind}_exps.weight", 320))
        tensors.append((f"blk.{block}.ffn_up_shexp.weight", 64))
        if block % 4 == 3 or block in extra_attention_blocks:
            tensors.append((f"blk.{block}.attn_k.weight", 128))
        else:
            tensors.append((f"blk.{block}.attn_qkv.weight", 128))
    return tensors


def test_a_hybrid_prices_only_its_attention_blocks(tmp_path):
    path = write_model(tmp_path, build_gguf(qwen36_entries()))

    # 10 attention blocks x 2 KV heads x (256 + 256) x 2 bytes: 20 KiB, not the 80 KiB
    # that counting all 40 blocks gave.
    assert gguf.kv_bytes_per_token_for_model(path) == 10 * 2 * 512 * 2 == 20_480
    assert gguf.kv_layer_count(gguf.read_metadata(path)) == 10


def test_cache_types_are_priced_exactly(tmp_path):
    """q8_0 packs 32 values in 34 bytes and q4_0 in 18, not exactly half and a quarter."""
    path = write_model(tmp_path, build_gguf(qwen36_entries()))

    assert gguf.kv_bytes_per_token_for_model(path, "f16") == 20_480
    assert gguf.kv_bytes_per_token_for_model(path, "q8_0") == 10_880
    assert gguf.kv_bytes_per_token_for_model(path, "q4_0") == 5_760
    # A typo in the type is priced as the most expensive one, never the cheapest.
    assert gguf.kv_bytes_per_token_for_model(path, "q9_9") == 20_480
    assert gguf.scale_kv_bytes(gguf.FALLBACK_KV_BYTES_PER_TOKEN, "q8_0") == 208_896


def test_the_recurrent_state_a_slot_carries_is_priced(tmp_path):
    """The other three blocks in four keep a fixed f32 state per sequence, whatever the context."""
    metadata = gguf.read_metadata(write_model(tmp_path, build_gguf(qwen36_entries())))

    # 30 blocks x [128 x 4096 (ssm) + 3 x (4096 + 2 x 16 x 128) (conv)] floats x 4 bytes
    per_block = 128 * 4096 + 3 * (4096 + 2 * 16 * 128)
    assert gguf.recurrent_state_bytes(metadata) == 30 * per_block * 4 == 65_863_680
    # A plain transformer has none.
    plain = gguf.read_metadata(write_model(tmp_path, build_gguf(
        architecture_entries("llama", blocks=32, heads_kv=8, embedding=4096, heads=32)), "plain.gguf"))
    assert gguf.recurrent_state_bytes(plain) == 0


def test_the_arithmetic_reproduces_llama_cpps_own_memory_breakdown(tmp_path):
    """
    Measured, not derived: llama-server b11838 on Qwen3.6-35B-A3B at -c 65536 -np 2 with a
    q8_0 cache reports ``context = 805 MiB`` (680 MiB of KV for the ten attention blocks
    plus 2 x 62.8 MiB of recurrent state). Printed as an integer number of MiB.
    """
    metadata = gguf.read_metadata(write_model(tmp_path, build_gguf(qwen36_entries())))
    kv = 65536 * gguf.kv_bytes_per_token(metadata, cache_type="q8_0")
    state = 2 * gguf.recurrent_state_bytes(metadata)

    assert int((kv + state) / MIB) == 805
    assert kv / MIB == pytest.approx(680.0)


def test_next_token_blocks_are_not_in_the_cache(tmp_path):
    """The MTP build of the model carries a 41st block that the main cache never holds."""
    entries = qwen36_entries(block_count=41, nextn_predict_layers=1)
    metadata = gguf.read_metadata(write_model(tmp_path, build_gguf(entries)))

    assert gguf.kv_layer_count(metadata) == 10


def test_a_per_block_kv_head_array_with_zeros_marks_the_blocks_without_attention(tmp_path):
    counts = [0, 0, 0, 2] * 10
    entries = [e for e in qwen36_entries(full_attention_interval=None)
               if not e[0].endswith("head_count_kv")]
    entries.append(("qwen35moe.attention.head_count_kv", T_ARRAY,
                    struct.pack("<I", T_UINT32) + struct.pack("<Q", len(counts))
                    + struct.pack(f"<{len(counts)}I", *counts)))
    metadata = gguf.read_metadata(write_model(tmp_path, build_gguf(entries)))

    assert gguf.kv_layer_count(metadata) == 10
    assert gguf.kv_bytes_per_token(metadata) == 20_480


def test_tensors_can_raise_the_attention_count_but_never_lower_it(tmp_path):
    """
    The tensor table verifies the header's rule. A model whose tensors show more attention
    blocks than the rule says is priced by the tensors; a family whose tensors merely use
    other names can never make the cache look cheaper than the header's own count.
    """
    metadata = gguf.read_metadata(write_model(tmp_path, build_gguf(qwen36_entries())))

    assert gguf.kv_layer_count(metadata, attention_layers=range(3, 40, 4)) == 10
    assert gguf.kv_layer_count(metadata, attention_layers=list(range(3, 40, 4)) + [1, 2]) == 12
    assert gguf.kv_layer_count(metadata, attention_layers=[3]) == 10

    no_rule = gguf.read_metadata(write_model(tmp_path, build_gguf(
        qwen36_entries(full_attention_interval=None)), "norule.gguf"))
    assert gguf.kv_layer_count(no_rule, attention_layers=range(3, 40, 4)) == 40


# --------------------------------------------------------------------------------------
# The tensor table: what sits on the GPUs and what sits in system RAM
# --------------------------------------------------------------------------------------

def _shape(tmp_path, tensors=None, name="m.gguf", entries=None):
    payload = build_gguf_with_tensors(entries or qwen36_entries(),
                                      tensors if tensors is not None else qwen36_tensors())
    return gguf.model_shape(write_model(tmp_path, payload, name))


def test_the_tensor_table_is_read_and_sized_from_consecutive_offsets(tmp_path):
    shape = _shape(tmp_path)

    assert shape.has_tensor_table
    assert shape.kv_layers == 10
    assert shape.kv_f16_bytes_per_token == 20_480
    assert shape.state_bytes_per_sequence == 65_863_680
    assert shape.embedding_bytes == 3200
    assert shape.embedding_tied is False
    # three expert tensors of 320 bytes in each of the 40 blocks
    assert shape.expert_bytes_by_layer[0] == (0, 960) and len(shape.expert_bytes_by_layer) == 40
    assert shape.file_bytes == (tmp_path / "m.gguf").stat().st_size


def test_gpu_and_cpu_bytes_follow_n_cpu_moe(tmp_path):
    """--n-cpu-moe N keeps the routed experts of the first N blocks in system RAM."""
    shape = _shape(tmp_path)
    all_experts = 40 * 960

    # the token embedding is always host memory
    assert shape.gpu_bytes(0) == shape.file_bytes - 3200
    assert shape.cpu_bytes(0) == 3200
    assert shape.gpu_bytes(5) == shape.file_bytes - 3200 - 5 * 960
    assert shape.cpu_bytes(5) == 3200 + 5 * 960
    # more blocks than the model has, and a nonsense count, behave
    assert shape.experts_on_cpu_bytes(999) == all_experts
    assert shape.experts_on_cpu_bytes(-3) == 0
    # bytes are conserved: nothing is lost or counted twice by moving experts
    assert shape.gpu_bytes(7) + shape.cpu_bytes(7) == shape.file_bytes


def test_a_tied_embedding_is_on_the_gpu_too(tmp_path):
    """No output.weight: the output layer reads a duplicate of the embedding on the GPU."""
    shape = _shape(tmp_path, tensors=qwen36_tensors(tied=True))

    assert shape.embedding_tied is True
    assert shape.gpu_bytes(0) == shape.file_bytes
    assert shape.cpu_bytes(0) == 3200   # the host keeps the original


def test_extra_attention_tensors_raise_the_price_of_the_cache(tmp_path):
    """The verification works end to end: a block with attn_k that the rule missed is priced."""
    shape = _shape(tmp_path, tensors=qwen36_tensors(extra_attention_blocks=(0, 1)))

    assert shape.kv_layers == 12
    assert shape.kv_f16_bytes_per_token == 12 * 2 * 512 * 2


def test_a_damaged_tensor_table_keeps_the_header_sizing(tmp_path):
    """Truncated mid-table: the key/value sizing must survive; the GPU/CPU split is unknown."""
    full = build_gguf_with_tensors(qwen36_entries(), qwen36_tensors())
    path = write_model(tmp_path, full[:len(build_gguf(qwen36_entries())) + 40], "cut.gguf")

    shape = gguf.model_shape(path)
    assert shape is not None and shape.has_tensor_table is False
    assert shape.kv_f16_bytes_per_token == 20_480
    # With no table everything is treated as GPU-resident: the pessimistic answer.
    assert shape.gpu_bytes(5) == shape.file_bytes


def test_a_file_without_a_readable_shape_has_none(tmp_path):
    path = write_model(tmp_path, build_gguf([("general.architecture", T_STRING, _string("mystery"))]))

    assert gguf.model_shape(path) is None
    assert gguf.model_shape("") is None
    assert gguf.model_shape(str(tmp_path / "absent.gguf")) is None


def test_split_models_are_sized_across_every_shard(tmp_path):
    """llama.cpp is given the first shard and finds the rest; the bytes are all of them."""
    first = build_gguf_with_tensors(
        qwen36_entries(),
        [("token_embd.weight", 3200), ("output.weight", 3200)]
        + [(f"blk.{b}.ffn_up_exps.weight", 320) for b in range(20)])
    second = build_gguf_with_tensors(
        qwen36_entries(), [(f"blk.{b}.ffn_up_exps.weight", 320) for b in range(20, 40)])
    path = write_model(tmp_path, first, "Big-00001-of-00002.gguf")
    # Only the first shard on disk: sized as one file, as a download in progress would be.
    assert gguf.shard_paths(path) == [path]

    write_model(tmp_path, second, "Big-00002-of-00002.gguf")
    assert gguf.shard_paths(path) == [path, str(tmp_path / "Big-00002-of-00002.gguf")]

    shape = gguf.model_shape(path)
    assert shape.file_bytes == len(first) + len(second)
    assert [layer for layer, _ in shape.expert_bytes_by_layer] == list(range(40))
    assert shape.gpu_bytes(0) == shape.file_bytes - 3200


def test_the_shape_is_cached_until_the_file_changes(tmp_path):
    path = write_model(tmp_path, build_gguf_with_tensors(qwen36_entries(), qwen36_tensors()))
    first = gguf.model_shape(path)

    assert gguf.model_shape(path) is first

    with open(path, "ab") as handle:
        handle.write(b"\0" * 64)
    assert gguf.model_shape(path) is not first


def test_read_metadata_keeps_only_the_keys_sizing_needs(tmp_path):
    entries = qwen36_entries() + [("general.name", T_STRING, _string("Qwen3.6")),
                                  u32_entry("general.alignment", 64)]
    metadata = gguf.read_metadata(write_model(tmp_path, build_gguf(entries)))

    assert metadata["qwen35moe.full_attention_interval"] == 4
    assert metadata["qwen35moe.ssm.state_size"] == 128
    assert metadata["general.alignment"] == 64
    assert "general.name" not in metadata


def test_a_non_default_alignment_is_honoured(tmp_path):
    entries = qwen36_entries() + [u32_entry("general.alignment", 64)]
    tensors = [("token_embd.weight", 3200), ("output.weight", 3200), ("blk.0.ffn_up_exps.weight", 640)]
    path = write_model(tmp_path, build_gguf_with_tensors(entries, tensors, alignment=64))

    shape = gguf.model_shape(path)
    assert shape.embedding_bytes == 3200
    assert shape.expert_bytes_by_layer == ((0, 640),)


# --------------------------------------------------------------------------------------
# The real file, when this machine has it (the pinned bake-off download). Skipped on the CI
# box, which has no models; run by hand on a machine with ~/.cache/dlne-test.
# --------------------------------------------------------------------------------------

REAL_QWEN36 = os.path.join(os.environ.get("DLNE_TEST_DIR", os.path.expanduser("~/.cache/dlne-test")),
                           "Qwen3.6-35B-A3B-UD-IQ3_S.gguf")


@pytest.mark.skipif(not os.path.isfile(REAL_QWEN36), reason="the pinned Qwen3.6 IQ3_S file is not on this machine")
def test_the_real_qwen36_header_matches_the_hand_numbers():
    shape = gguf.model_shape(REAL_QWEN36)

    assert shape.arch == "qwen35moe"
    assert shape.file_bytes == 13_676_723_168
    # Ten attention blocks (3, 7, ..., 39), found two independent ways: the header's
    # full_attention_interval and the blocks that own an attn_k tensor.
    assert shape.kv_layers == 10
    assert shape.kv_f16_bytes_per_token == 20_480
    assert shape.kv_bytes_per_token("q8_0") == 10_880
    assert shape.state_bytes_per_sequence == 65_863_680
    assert shape.embedding_bytes == 417_177_600
    assert sum(size for _layer, size in shape.expert_bytes_by_layer) == 11_574_181_888
    # TheWall's placement (--n-cpu-moe 5): 11.0 GiB on the cards, 1.7 GiB in RAM
    assert shape.gpu_bytes(5) == 11_822_996_448
    assert shape.cpu_bytes(5) == 1_853_726_720
