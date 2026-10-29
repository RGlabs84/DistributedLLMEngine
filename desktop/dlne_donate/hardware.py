"""
Looking at this computer the way the worker will (engine/hardware.py): same GPU probe, same rule for which GPUs count,
same physical-core count. Used only to draw the sliders; the worker measures for itself.
"""

from typing import Any, Dict, List

from . import bundled
from .tiers import Hardware, installed_ram_gb


def detect() -> Hardware:
    """Never raises: a probe that fails reports a machine with no GPU and what psutil can still say."""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return Hardware(total_ram_gb=8, physical_cores=2, logical_threads=2, avx2=False)

    total_bytes = float(psutil.virtual_memory().total)
    logical = psutil.cpu_count(logical=True) or 1
    physical = psutil.cpu_count(logical=False) or logical
    gpu_name, vram, count, integrated, avx2 = "", 0.0, 0, "", True

    try:
        hw = bundled.import_snapshot("hardware")
        try:
            physical = int(hw._detect_physical_cores()) or physical      # the uncapped count (get_cpu_cores applies the config's cap)
        except Exception:  # noqa: BLE001
            pass
        features = [str(f).lower() for f in (hw.get_cpu_features() or [])]
        avx2 = "avx2" in features if features else _avx2_guess()
        cards: List[Dict[str, Any]] = list(hw.get_all_gpus() or [])
        counted = [c for c in cards if hw.counts_as_gpu(str(c.get("name") or ""), c.get("integrated"))]
        if counted:
            counted.sort(key=lambda c: float(c.get("vram_total_gb") or 0), reverse=True)
            gpu_name = str(counted[0].get("name") or "GPU")
            vram = round(sum(float(c.get("vram_total_gb") or 0) for c in counted), 1)
            count = len(counted)
        elif cards:
            integrated = str(cards[0].get("name") or "")
    except Exception:  # noqa: BLE001 - no probe: the CPU is still offered
        avx2 = _avx2_guess()

    return Hardware(gpu_name=gpu_name, vram_gb=vram, gpu_count=count, total_ram_gb=float(installed_ram_gb(total_bytes)),
                    physical_cores=max(1, int(physical)), logical_threads=max(1, int(logical)), avx2=avx2,
                    integrated_gpu=integrated)


def _avx2_guess() -> bool:
    """When the feature probe is unavailable: x86-64 computers from the last decade have AVX2; the worker checks for itself."""
    import platform
    return platform.machine().lower() in ("amd64", "x86_64")
