from __future__ import annotations

from typing import Any

from .contract import require_runtime


def build_model(num_classes: int) -> Any:
    torch, _, _ = require_runtime()
    if isinstance(num_classes, bool) or not isinstance(num_classes, int) or num_classes < 2:
        raise ValueError("num_classes 必须是至少 2 的整数")

    class TinyClassifier(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.features = torch.nn.Sequential(
                torch.nn.Conv2d(3, 8, kernel_size=3, padding=1),
                torch.nn.ReLU(),
                torch.nn.Conv2d(8, 8, kernel_size=3, padding=1),
                torch.nn.ReLU(),
                torch.nn.AdaptiveAvgPool2d((1, 1)),
            )
            self.classifier = torch.nn.Linear(8, num_classes)

        def forward(self, images: Any) -> Any:
            features = self.features(images).flatten(1)
            return self.classifier(features)

    return TinyClassifier()
