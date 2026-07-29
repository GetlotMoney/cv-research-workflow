from __future__ import annotations

import math
from typing import Any

from .contract import require_runtime


def segmentation_metrics(
    predictions: Any,
    targets: Any,
    num_classes: int,
) -> dict[str, Any]:
    torch, _, _ = require_runtime()
    if (
        not isinstance(predictions, torch.Tensor)
        or not isinstance(targets, torch.Tensor)
        or predictions.shape != targets.shape
        or predictions.ndim not in {2, 3}
        or predictions.numel() == 0
        or isinstance(num_classes, bool)
        or not isinstance(num_classes, int)
        or num_classes < 2
    ):
        raise ValueError("predictions/targets 必须是同形非空 2D/3D Tensor")
    predictions = predictions.to(dtype=torch.long, device="cpu")
    targets = targets.to(dtype=torch.long, device="cpu")
    valid = targets != 255
    if not bool(valid.any()):
        raise ValueError("评估 mask 全部是 ignore=255，无法计算 mIoU")
    selected_predictions = predictions[valid]
    selected_targets = targets[valid]
    if bool(
        ((selected_predictions < 0) | (selected_predictions >= num_classes)).any()
    ):
        raise ValueError("预测类别超出 0..C-1")
    if bool(((selected_targets < 0) | (selected_targets >= num_classes)).any()):
        raise ValueError("真实类别超出 0..C-1/ignore=255")
    indices = selected_targets * num_classes + selected_predictions
    matrix = torch.bincount(
        indices,
        minlength=num_classes * num_classes,
    ).reshape(num_classes, num_classes)
    per_class_iou: list[float | None] = []
    included_classes: list[int] = []
    for class_index in range(num_classes):
        intersection = int(matrix[class_index, class_index].item())
        target_count = int(matrix[class_index, :].sum().item())
        prediction_count = int(matrix[:, class_index].sum().item())
        union = target_count + prediction_count - intersection
        if union == 0:
            per_class_iou.append(None)
            continue
        value = float(intersection / union)
        per_class_iou.append(value)
        included_classes.append(class_index)
    if not included_classes:
        raise ValueError("没有 union>0 的类别，无法计算 mIoU")
    mean_iou = float(
        sum(per_class_iou[index] for index in included_classes)  # type: ignore[arg-type]
        / len(included_classes)
    )
    if not math.isfinite(mean_iou) or not 0.0 <= mean_iou <= 1.0:
        raise ValueError("mIoU 不是有限 [0,1] 数字")
    return {
        "mean_iou": mean_iou,
        "confusion_matrix": matrix.tolist(),
        "per_class_iou": per_class_iou,
        "included_classes": included_classes,
    }
