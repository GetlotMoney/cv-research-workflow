from __future__ import annotations

import math
from typing import Any

from .contract import require_runtime


def _class_average(predictions: Any, targets: Any, classes: Any) -> float:
    _, numpy = require_runtime()
    accuracies = []
    for class_id in classes.tolist():
        selected = targets == class_id
        if not bool(selected.any()):
            raise ValueError(f"评估 split 缺 declared class：{class_id}")
        accuracies.append(float(numpy.mean(predictions[selected] == targets[selected])))
    return float(sum(accuracies) / len(accuracies))


def gzsl_class_average_metrics(
    predictions: Any,
    targets: Any,
    seen_class_ids: Any,
    unseen_class_ids: Any,
) -> dict[str, float]:
    _, numpy = require_runtime()
    if (
        not isinstance(predictions, numpy.ndarray)
        or not isinstance(targets, numpy.ndarray)
        or predictions.dtype != numpy.int64
        or targets.dtype != numpy.int64
        or predictions.ndim != 1
        or targets.ndim != 1
        or predictions.shape != targets.shape
        or predictions.size == 0
        or not isinstance(seen_class_ids, numpy.ndarray)
        or not isinstance(unseen_class_ids, numpy.ndarray)
        or seen_class_ids.dtype != numpy.int64
        or unseen_class_ids.dtype != numpy.int64
        or seen_class_ids.ndim != 1
        or unseen_class_ids.ndim != 1
        or seen_class_ids.size == 0
        or unseen_class_ids.size == 0
    ):
        raise ValueError("GZSL 指标输入必须是非空 int64 一维数组")
    candidate = set(int(value) for value in numpy.concatenate(
        [seen_class_ids, unseen_class_ids]
    ).tolist())
    if not set(int(value) for value in predictions.tolist()).issubset(candidate):
        raise ValueError("预测类别不在 seen∪unseen 候选集合")
    seen_mask = numpy.isin(targets, seen_class_ids)
    unseen_mask = numpy.isin(targets, unseen_class_ids)
    if not bool(seen_mask.any()) or not bool(unseen_mask.any()) or bool((~(seen_mask | unseen_mask)).any()):
        raise ValueError("targets 必须同时含 declared seen 与 unseen 类")
    seen = _class_average(predictions[seen_mask], targets[seen_mask], seen_class_ids)
    unseen = _class_average(
        predictions[unseen_mask],
        targets[unseen_mask],
        unseen_class_ids,
    )
    denominator = seen + unseen
    harmonic = 0.0 if denominator == 0.0 else float(2.0 * seen * unseen / denominator)
    result = {"S": float(seen), "U": float(unseen), "H": harmonic}
    if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in result.values()):
        raise ValueError("S/U/H 必须是有限 [0,1] 数字")
    return result
