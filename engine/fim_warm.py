"""
Ghost-text warm-up for the hybrid coding model, shared by the coordinator's own engine and coding members.

Qwen3.6 has recurrent layers that cannot be rolled back to the middle of a prompt. llama-server keeps its
checkpoints near the END of a prompt, which for ``<|fim_prefix|>P<|fim_suffix|>S<|fim_middle|>`` is inside S,
past the point where the next keystroke's prompt starts to differ, so every completion re-read about 500 tokens
(measured on TheWall: 1.57 s median per completion). Sending the text before the cursor as a request of its own
first (no tokens generated) parks the engine's state exactly there, and the real request then reads only S
(1.0 s, same quality on the bake-off's FIM suite). The warm-up is best effort and changes nothing the real
request returns. scripts/cache_probe.py has the measurement.
"""
from typing import Any, Dict, Optional

FIM_PREFIX_TOKEN = "<|fim_prefix|>"
FIM_SUFFIX_TOKEN = "<|fim_suffix|>"


def fim_warm_body(body: Any) -> Optional[Dict[str, Any]]:
    """The prefix-only request that goes ahead of a fill-in-the-middle completion, or None if `body` is not one."""
    prompt = body.get("prompt") if isinstance(body, dict) else None
    if not isinstance(prompt, str) or not prompt.startswith(FIM_PREFIX_TOKEN) or FIM_SUFFIX_TOKEN not in prompt:
        return None
    return {"prompt": prompt.split(FIM_SUFFIX_TOKEN, 1)[0], "max_tokens": 0, "temperature": 0.0, "stream": False}
