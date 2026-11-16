"""
The VRAM reading on /v1/cluster/status (2.9.1): this host's own cards by the job they have, live, with no card's name or
address, and how much the volunteers' GPUs hold, counted apart. The environment is the pool status tests'
(tests/test_pool_status.py).
"""
import json
import os

import pytest

from engine import coordinator, hardware, takeover
from tests.test_coding_endpoint import canary_book, client, env, fakes  # noqa: F401 - the fixtures pools stands on
from tests.test_pool_status import pools, status  # noqa: F401 - pools is a fixture

POOL_A, POOL_B, MEDIA = "0000:03:00.0", "0000:09:00.0", "0000:0c:00.0"
GIB = 1024 ** 3


def card(gpu_id, address, name="AMD Radeon", **extra):
    return {"id": gpu_id, "address": address, "name": name, "vram_total_gb": 8.0, "vk_index": gpu_id, **extra}


@pytest.fixture
def thewall(monkeypatch):
    """TheWall's three cards, with amdgpu's counters saying how much of each is in use."""
    used = {POOL_A: 7 * GIB, POOL_B: 6 * GIB, MEDIA: 4 * GIB}

    def read(path):
        address, name = os.path.basename(os.path.dirname(path)), os.path.basename(path)
        if address not in used:
            return ""
        return str(used[address]) if name == "mem_info_vram_used" else str(8 * GIB)

    monkeypatch.setattr(hardware, "get_all_gpus", lambda: [card(0, POOL_A), card(1, POOL_B), card(2, MEDIA)])
    monkeypatch.setattr(hardware, "_read_sysfs", read)
    return used


def test_each_card_has_the_job_it_has_now_and_a_live_figure(thewall):
    cards = hardware.host_vram_cards(f"{POOL_A},{POOL_B}", MEDIA)
    assert cards == [{"role": "coding", "used_gb": 7.0, "total_gb": 8.0}, {"role": "coding", "used_gb": 6.0, "total_gb": 8.0},
                     {"role": "media", "used_gb": 4.0, "total_gb": 8.0}]


def test_a_lent_card_says_so_and_one_with_no_job_is_other(thewall):
    roles = [c["role"] for c in hardware.host_vram_cards(POOL_B, MEDIA, lent=[POOL_A.upper()])]
    assert roles == ["lent", "coding", "media"]
    assert [c["role"] for c in hardware.host_vram_cards(POOL_B, MEDIA)] == ["other", "coding", "media"]


def test_a_media_card_shared_with_the_engine_is_a_coding_card(thewall):
    assert [c["role"] for c in hardware.host_vram_cards(MEDIA, MEDIA, media_shared=True)] == ["other", "other", "coding"]


def test_no_limit_gives_the_engine_every_card_and_none_gives_it_no_card(thewall):
    assert [c["role"] for c in hardware.host_vram_cards("-1")] == ["coding"] * 3
    assert [c["role"] for c in hardware.host_vram_cards("none")] == ["other"] * 3


def test_a_card_with_no_counters_has_no_live_figure_unless_nvidia_smi_gave_one(monkeypatch):
    monkeypatch.setattr(hardware, "_read_sysfs", lambda path: "")
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: [
        card(0, "0000:01:00.0"),                                                     # a PCI address, and nothing readable there
        {"id": 1, "name": "NVIDIA RTX", "vram_total_gb": 12.0, "vram_free_gb": 9.0},  # nvidia-smi's path: no address, a live figure
        {"id": 2, "name": "NVIDIA RTX", "vram_total_gb": 12.0}])
    assert [(c["used_gb"], c["total_gb"]) for c in hardware.host_vram_cards()] == [(None, 8.0), (3.0, 12.0), (None, 12.0)]


def test_an_integrated_gpu_is_not_listed(thewall, monkeypatch):
    monkeypatch.setattr(hardware, "get_all_gpus", lambda: [card(0, POOL_A), card(1, "0000:11:00.0", integrated=True)])
    assert len(hardware.host_vram_cards()) == 1


def test_the_media_card_still_says_how_much_is_free(thewall):
    media = hardware.media_card(MEDIA)
    assert media["free_gb"] == 4.0 and media["vk_index"] == 2


@pytest.mark.anyio
async def test_the_public_status_has_the_reading_with_no_name_and_no_address(pools, thewall):  # noqa: F811
    pools.limits.update({"allowed_gpu_indices": f"{POOL_A},{POOL_B}", "media_gpu": MEDIA})
    async with client() as c:
        body = await status(c)
    block = body["vram"]
    assert set(block) == {"used_gb", "total_gb", "cards"}
    assert all(set(c) == {"role", "used_gb", "total_gb"} for c in block["cards"])
    assert len(block["cards"]) == 3 and block["total_gb"] == 24.0 and block["used_gb"] == 17.0
    text = json.dumps(block)
    assert POOL_A not in text and MEDIA not in text and "Radeon" not in text


@pytest.mark.anyio
async def test_a_card_without_a_live_figure_leaves_the_total_and_no_sum(pools, monkeypatch):  # noqa: F811
    monkeypatch.setattr(hardware, "host_vram_cards", lambda *a, **k: [{"role": "coding", "used_gb": 3.0, "total_gb": 8.0},
                                                                     {"role": "media", "used_gb": None, "total_gb": 8.0}])
    async with client() as c:
        block = (await status(c))["vram"]
    assert block["used_gb"] is None and block["total_gb"] == 16.0 and len(block["cards"]) == 2


@pytest.mark.anyio
async def test_a_reading_that_fails_never_takes_the_status_down(pools, monkeypatch):  # noqa: F811
    def boom(*a, **k):
        raise RuntimeError("no cards today")

    monkeypatch.setattr(hardware, "host_vram_cards", boom)
    async with client() as c:
        assert (await status(c))["vram"] == {"used_gb": None, "total_gb": 0.0, "cards": []}


@pytest.mark.anyio
async def test_a_lease_marks_the_cards_it_took_as_lent(pools, thewall, monkeypatch):  # noqa: F811
    pools.limits.update({"allowed_gpu_indices": POOL_B, "media_gpu": MEDIA})
    monkeypatch.setattr(takeover, "load_lease", lambda: {"granted": {"gpus": [POOL_A]}, "state": "active", "holder": "h"})
    block = await coordinator._vram_status()
    assert block["cards"][0]["role"] == "lent"


@pytest.mark.anyio
async def test_the_volunteers_vram_is_counted_apart_and_a_block_that_does_not_verify_is_not(pools):  # noqa: F811
    async with client() as c:
        gpu = (await status(c))["gpu_pool"]
    # n1 24 + n2 8 + n3 16 + the two verified coding members 8 + 8. Not n4 (yielding), not the block that does not verify.
    assert gpu["volunteer_vram_gb"] == 64.0
