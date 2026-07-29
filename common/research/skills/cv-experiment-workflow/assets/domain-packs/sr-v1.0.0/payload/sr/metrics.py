from __future__ import annotations

import math
from typing import Any, Iterable

from .contract import require_runtime


def cumulative_rgb_psnr(
    pairs: Iterable[tuple[Any, Any]],
) -> dict[str, float | int]:
    _, numpy, _ = require_runtime()
    total_sse = 0
    total_values = 0
    pair_count = 0
    for predicted, target in pairs:
        if (
            not isinstance(predicted, numpy.ndarray)
            or not isinstance(target, numpy.ndarray)
            or predicted.dtype != numpy.uint8
            or target.dtype != numpy.uint8
            or predicted.shape != target.shape
            or predicted.ndim != 3
            or predicted.shape[2] != 3
            or predicted.size == 0
        ):
            raise ValueError("PSNR 输入必须是同形非空 RGB uint8 [H,W,3]")
        difference = predicted.astype(numpy.int64) - target.astype(numpy.int64)
        total_sse += int(numpy.sum(difference * difference, dtype=numpy.int64))
        total_values += int(predicted.size)
        pair_count += 1
    if pair_count == 0 or total_values == 0:
        raise ValueError("PSNR 至少需要一对图像")
    mse = float(total_sse / total_values)
    if mse == 0.0:
        raise ValueError("预测与真值完全一致导致 MSE=0；拒绝输出无穷 PSNR")
    psnr = float(10.0 * math.log10((255.0**2) / mse))
    if not math.isfinite(psnr):
        raise ValueError("PSNR 不是有限数字")
    return {
        "sse": total_sse,
        "value_count": total_values,
        "mse": mse,
        "psnr_rgb_x2": psnr,
    }
