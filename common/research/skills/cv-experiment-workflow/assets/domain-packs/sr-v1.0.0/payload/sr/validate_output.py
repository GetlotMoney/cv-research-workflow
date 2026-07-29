from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Any

from . import data as output_data
from .contract import (
    SHA256,
    load_checkpoint,
    read_json,
    require_directory,
    require_runtime,
    secure_read,
    validate_artifacts,
)
from .model import build_model


def _validate_run(record: dict[str, Any]) -> None:
    fields = {
        "schema",
        "phase",
        "mode",
        "run_kind",
        "paper_eligible",
        "run_id",
        "config_sha256",
        "seed",
        "device",
        "scale",
        "training_sample_count",
        "evaluation_sample_count",
        "checkpoint",
        "optimizer_steps",
        "dataset_identity",
        "checkpoint_reloaded_for_evaluation",
        "checkpoint_reloaded_for_inference",
    }
    if set(record) != fields or record.get("schema") != "pack-sr.run.v1":
        raise ValueError("run.json schema 字段无效")
    if record.get("mode") not in {"synthetic_smoke", "local_sr_x2"}:
        raise ValueError("run.json mode 无效")
    synthetic = record["mode"] == "synthetic_smoke"
    device = record.get("device")
    if device not in {"cpu", "cuda"} or (synthetic and device != "cpu"):
        raise ValueError("run.json device 无效；synthetic_smoke 只能使用 cpu")
    expected = {
        "phase": "synthetic_smoke" if synthetic else "train",
        "run_kind": "synthetic_debug_only" if synthetic else "local_dataset_baseline",
        "paper_eligible": False,
        "device": device,
        "scale": 2,
        "checkpoint": "checkpoint.pt",
        "checkpoint_reloaded_for_evaluation": True,
        "checkpoint_reloaded_for_inference": True,
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise ValueError(f"run.json {key} 无效")
    for key in (
        "training_sample_count",
        "evaluation_sample_count",
        "optimizer_steps",
    ):
        if isinstance(record.get(key), bool) or not isinstance(record.get(key), int) or record[key] <= 0:
            raise ValueError(f"run.json {key} 必须是正整数")
    if isinstance(record.get("seed"), bool) or not isinstance(record.get("seed"), int) or record["seed"] < 0:
        raise ValueError("run.json seed 无效")


def _validate_evaluation(
    payload: dict[str, Any],
    *,
    synthetic: bool,
) -> dict[str, float]:
    if set(payload) != {
        "schema",
        "metrics",
        "sse",
        "value_count",
        "mse",
        "definition",
    } or payload.get("schema") != "pack-sr.evaluation.v1":
        raise ValueError("evaluation.json schema 无效")
    metric_name = "debug_psnr_rgb_x2" if synthetic else "psnr_rgb_x2"
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != {metric_name}:
        raise ValueError("合成/正式 PSNR 指标名混用")
    psnr = metrics[metric_name]
    if type(psnr) is not float or not math.isfinite(psnr):
        raise ValueError("PSNR 必须是有限 float")
    sse = payload.get("sse")
    count = payload.get("value_count")
    mse = payload.get("mse")
    if (
        isinstance(sse, bool)
        or not isinstance(sse, int)
        or sse <= 0
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count <= 0
        or type(mse) is not float
        or not math.isfinite(mse)
        or mse <= 0.0
    ):
        raise ValueError("累计 SSE/value_count/MSE 必须为正且有限")
    expected_mse = sse / count
    expected_psnr = 10.0 * math.log10((255.0**2) / expected_mse)
    if not math.isclose(mse, expected_mse, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError("MSE 不是 cumulative SSE/value_count")
    if not math.isclose(psnr, expected_psnr, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError("PSNR 与累计 RGB uint8 MSE 不一致")
    if payload.get("definition") != {
        "protocol": "custom_full_rgb_no_shave",
        "comparability": (
            "not_comparable_to_standard_y_channel_border_shave_"
            "without_recalculation"
        ),
        "scale": 2,
        "color_space": "full_rgb_uint8",
        "aggregation": "cumulative_sse_over_all_values",
        "shave_border": False,
        "perfect_match": "error_no_infinity",
    }:
        raise ValueError("PSNR 口径定义被篡改")
    return {metric_name: psnr}


def _validate_predictions(output: Path, expected_count: int) -> None:
    payload = read_json(
        output / "predictions" / "index.json",
        "predictions/index.json",
        root=output,
    )
    if (
        set(payload) != {"schema", "predictions"}
        or payload.get("schema") != "pack-sr.predictions.v1"
        or not isinstance(payload.get("predictions"), list)
        or len(payload["predictions"]) != expected_count
    ):
        raise ValueError("预测索引 schema/数量无效")
    inputs: set[str] = set()
    total_pixels = 0
    for index, item in enumerate(payload["predictions"]):
        if (
            not isinstance(item, dict)
            or set(item) != {"input", "image", "width", "height", "sha256"}
            or not isinstance(item.get("input"), str)
            or not item["input"]
            or item["input"] in inputs
            or item.get("image") != f"images/{index:04d}.png"
            or isinstance(item.get("width"), bool)
            or not isinstance(item.get("width"), int)
            or item["width"] <= 0
            or isinstance(item.get("height"), bool)
            or not isinstance(item.get("height"), int)
            or item["height"] <= 0
            or not isinstance(item.get("sha256"), str)
            or SHA256.fullmatch(item["sha256"]) is None
        ):
            raise ValueError("预测索引条目身份/尺寸无效")
        inputs.add(item["input"])
        content = secure_read(
            output / "predictions" / "images" / f"{index:04d}.png",
            "SR 预测 PNG",
            root=output,
        )
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError("SR 预测 PNG SHA-256 不一致")
        width, height, pixels = output_data.inspect_png_budget(
            content,
            label="SR 预测 PNG",
            channels=3,
        )
        total_pixels += pixels
        if total_pixels > output_data.MAX_DATASET_PIXELS:
            raise ValueError("SR 预测 PNG 累计像素超过上限")
        if (width, height) != (item["width"], item["height"]):
            raise ValueError("SR 预测 PNG IHDR 尺寸与索引不一致")
        try:
            _, _, Image = require_runtime()
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                if (
                    image.format != "PNG"
                    or image.mode != "RGB"
                    or image.size != (item["width"], item["height"])
                ):
                    raise ValueError("SR 推理产物必须是真实 RGB PNG")
        except (OSError, SyntaxError) as error:
            raise ValueError("SR 推理 PNG 无法解码") from error


def validate_output(output_dir: Path) -> dict[str, Any]:
    output = require_directory(output_dir, "SR Run 输出")
    listed = validate_artifacts(output)
    required = {
        "checkpoint.pt",
        "evaluation.json",
        "predictions/index.json",
        "raw.log",
        "run.json",
    }
    if not required.issubset(listed):
        raise ValueError("SR Run 缺少必需产物")
    run = read_json(output / "run.json", "run.json", root=output)
    _validate_run(run)
    synthetic = run["mode"] == "synthetic_smoke"
    evaluation = _validate_evaluation(
        read_json(output / "evaluation.json", "evaluation.json", root=output),
        synthetic=synthetic,
    )
    _validate_predictions(output, run["evaluation_sample_count"])
    checkpoint = load_checkpoint(output / "checkpoint.pt")
    if (
        checkpoint["seed"] != run["seed"]
        or checkpoint["optimizer_steps"] != run["optimizer_steps"]
        or checkpoint["run_id"] != run["run_id"]
        or checkpoint["config_sha256"] != run["config_sha256"]
        or checkpoint["dataset_identity"] != run["dataset_identity"]
        or checkpoint["run_kind"] != run["run_kind"]
    ):
        raise ValueError("checkpoint 与架构/配置/数据/Run 身份不一致")
    model = build_model()
    try:
        model.load_state_dict(checkpoint["model_state_dict"], strict=True)
    except (RuntimeError, TypeError, ValueError) as error:
        raise ValueError("checkpoint state_dict 无法 strict=True 加载") from error
    if not synthetic:
        manifest = read_json(
            output / "dataset_manifest.json",
            "dataset_manifest.json",
            root=output,
        )
        if manifest.get("manifest_sha256") != run["dataset_identity"]["manifest_sha256"]:
            raise ValueError("dataset_manifest 与 Run 数据身份不一致")
    return {"metrics": evaluation, "artifacts": listed, "run": run}


def main() -> int:
    parser = argparse.ArgumentParser(description="严格校验 PACK-SR Run 输出")
    parser.add_argument("--output-dir", required=True, type=Path)
    arguments = parser.parse_args()
    result = validate_output(arguments.output_dir)
    print(
        json.dumps(
            {"metrics": result["metrics"], "artifacts": result["artifacts"]},
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
