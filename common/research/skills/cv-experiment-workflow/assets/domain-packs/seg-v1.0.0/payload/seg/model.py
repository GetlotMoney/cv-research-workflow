from __future__ import annotations

from typing import Any

from .contract import require_runtime


def build_model(num_classes: int) -> Any:
    torch, _, _ = require_runtime()
    if isinstance(num_classes, bool) or not isinstance(num_classes, int) or num_classes < 2:
        raise ValueError("num_classes 必须是不小于 2 的整数")

    class TinySegmenter(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.network = torch.nn.Sequential(
                torch.nn.Conv2d(3, 8, kernel_size=3, padding=1),
                torch.nn.ReLU(),
                torch.nn.Conv2d(8, 8, kernel_size=3, padding=1),
                torch.nn.ReLU(),
                torch.nn.Conv2d(8, num_classes, kernel_size=1),
            )

        def forward(self, images: Any) -> Any:
            return self.network(images)

    return TinySegmenter()
