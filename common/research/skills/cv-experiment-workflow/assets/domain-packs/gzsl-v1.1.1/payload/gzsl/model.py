from __future__ import annotations

from typing import Any

from .contract import require_runtime


def build_model(visual_dim: int, attribute_dim: int) -> Any:
    torch, _ = require_runtime()
    if (
        isinstance(visual_dim, bool)
        or not isinstance(visual_dim, int)
        or visual_dim <= 0
        or isinstance(attribute_dim, bool)
        or not isinstance(attribute_dim, int)
        or attribute_dim <= 0
    ):
        raise ValueError("visual_dim/attribute_dim 必须是正整数")
    return torch.nn.Linear(visual_dim, attribute_dim)
