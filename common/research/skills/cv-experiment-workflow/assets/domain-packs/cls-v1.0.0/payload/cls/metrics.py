from __future__ import annotations

import math
from typing import Any

from .contract import METRIC_NAMES, require_runtime


def classification_metrics(logits: Any, targets: Any) -> dict[str, float]:
    torch, _, _ = require_runtime()
    if (
        not isinstance(logits, torch.Tensor)
        or not isinstance(targets, torch.Tensor)
        or logits.ndim != 2
        or targets.ndim != 1
        or logits.shape[0] != targets.shape[0]
        or logits.shape[0] == 0
        or logits.shape[1] < 2
    ):
        raise ValueError("logits 必须是非空 [样本数, 类别数]，targets 必须等长")
    if not torch.isfinite(logits).all():
        raise ValueError("logits 含非有限数字")
    targets = targets.to(dtype=torch.long, device=logits.device)
    num_classes = logits.shape[1]
    if bool(((targets < 0) | (targets >= num_classes)).any()):
        raise ValueError("targets 超出类别范围")
    predicted = logits.argmax(dim=1)
    top1 = float((predicted == targets).float().mean().item())
    top_k = min(5, num_classes)
    top_indices = logits.topk(top_k, dim=1).indices
    top5 = float(
        (top_indices == targets.unsqueeze(1)).any(dim=1).float().mean().item()
    )
    class_f1 = []
    for class_index in range(num_classes):
        true_positive = int(
            ((predicted == class_index) & (targets == class_index)).sum().item()
        )
        false_positive = int(
            ((predicted == class_index) & (targets != class_index)).sum().item()
        )
        false_negative = int(
            ((predicted != class_index) & (targets == class_index)).sum().item()
        )
        denominator = 2 * true_positive + false_positive + false_negative
        class_f1.append(
            0.0 if denominator == 0 else (2.0 * true_positive) / denominator
        )
    metrics = {
        "top1_accuracy": top1,
        "top5_accuracy": top5,
        "macro_f1": float(sum(class_f1) / len(class_f1)),
    }
    if set(metrics) != set(METRIC_NAMES) or any(
        not math.isfinite(value) or not 0.0 <= value <= 1.0
        for value in metrics.values()
    ):
        raise ValueError("分类指标不满足有限 [0,1] 合同")
    return metrics
