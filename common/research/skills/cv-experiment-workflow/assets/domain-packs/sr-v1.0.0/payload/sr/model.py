from __future__ import annotations

from typing import Any

from .contract import require_runtime


def build_model() -> Any:
    torch, _, _ = require_runtime()

    class TinyPixelShuffle(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.features = torch.nn.Sequential(
                torch.nn.Conv2d(3, 16, kernel_size=3, padding=1),
                torch.nn.ReLU(),
                torch.nn.Conv2d(16, 12, kernel_size=3, padding=1),
                torch.nn.PixelShuffle(2),
            )

        def forward(self, images: Any) -> Any:
            return self.features(images)

    return TinyPixelShuffle()
