from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from .contract import (
    load_checkpoint,
    read_json,
    require_directory,
    require_runtime,
    validate_artifacts,
)
from .metrics import gzsl_class_average_metrics
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
        "visual_dim",
        "attribute_dim",
        "class_count",
        "training_sample_count",
        "evaluation_sample_count",
        "checkpoint",
        "optimizer_steps",
        "dataset_identity",
        "checkpoint_reloaded_for_evaluation",
        "checkpoint_reloaded_for_inference",
    }
    if set(record) != fields or record.get("schema") != "pack-gzsl.run.v1":
        raise ValueError("run.json schema 字段无效")
    if record.get("mode") not in {"synthetic_smoke", "local_gzsl_npz"}:
        raise ValueError("run.json mode 无效")
    synthetic = record["mode"] == "synthetic_smoke"
    if record.get("device") != "cuda":
        raise ValueError("run.json 必须声明 cuda，禁止 CPU fallback")
    expected = {
        "phase": "synthetic_smoke" if synthetic else "train",
        "run_kind": "synthetic_debug_only" if synthetic else "local_dataset_baseline",
        "paper_eligible": False,
        "checkpoint": "checkpoint.pt",
        "checkpoint_reloaded_for_evaluation": True,
        "checkpoint_reloaded_for_inference": True,
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise ValueError(f"run.json {key} 无效")
    for key in (
        "visual_dim",
        "attribute_dim",
        "class_count",
        "training_sample_count",
        "evaluation_sample_count",
        "optimizer_steps",
    ):
        if isinstance(record.get(key), bool) or not isinstance(record.get(key), int) or record[key] <= 0:
            raise ValueError(f"run.json {key} 必须是正整数")
    if isinstance(record.get("seed"), bool) or not isinstance(record.get("seed"), int) or record["seed"] < 0:
        raise ValueError("run.json seed 无效")


def _expected_metric_names(synthetic: bool) -> set[str]:
    return (
        {
            "debug_seen_class_average_accuracy",
            "debug_unseen_class_average_accuracy",
            "debug_harmonic_mean",
        }
        if synthetic
        else {"S", "U", "H"}
    )


def _validate_evaluation(payload: dict[str, Any], synthetic: bool) -> dict[str, float]:
    if set(payload) != {"schema", "metrics", "definition"} or payload.get("schema") != "pack-gzsl.evaluation.v1":
        raise ValueError("evaluation.json schema 无效")
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != _expected_metric_names(synthetic):
        raise ValueError("GZSL 合成/正式指标名混用")
    for name, value in metrics.items():
        if type(value) is not float or not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"GZSL 指标 {name} 必须是有限 [0,1] float")
    if payload.get("definition") != {
        "accuracy_averaging": "class_average",
        "candidate_classes": "seen_union_unseen",
        "similarity": "cosine",
        "calibration": "none",
        "harmonic_mean": "2*S*U/(S+U); 0 when S+U=0",
    }:
        raise ValueError("GZSL S/U/H 定义被篡改")
    if synthetic:
        s_value = metrics["debug_seen_class_average_accuracy"]
        u_value = metrics["debug_unseen_class_average_accuracy"]
        h_value = metrics["debug_harmonic_mean"]
    else:
        s_value, u_value, h_value = metrics["S"], metrics["U"], metrics["H"]
    expected_h = (
        0.0
        if s_value + u_value == 0.0
        else 2.0 * s_value * u_value / (s_value + u_value)
    )
    if not math.isclose(h_value, expected_h, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError("H 与 2*S*U/(S+U) 不一致")
    return dict(metrics)


def _validate_predictions(
    payload: dict[str, Any],
    expected_count: int,
    class_ids: Any,
    seen_class_ids: Any,
    unseen_class_ids: Any,
) -> tuple[Any, Any]:
    _, numpy = require_runtime()
    if (
        set(payload) != {"schema", "predictions"}
        or payload.get("schema") != "pack-gzsl.predictions.v1"
        or not isinstance(payload.get("predictions"), list)
        or len(payload["predictions"]) != expected_count
    ):
        raise ValueError("predictions.json schema/数量无效")
    candidates = set(int(value) for value in class_ids.tolist())
    seen = set(int(value) for value in seen_class_ids.tolist())
    unseen = set(int(value) for value in unseen_class_ids.tolist())
    sample_indices: set[int] = set()
    predictions = []
    targets = []
    for item in payload["predictions"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "sample_index",
                "split",
                "true_class_id",
                "predicted_class_id",
                "score",
            }
            or isinstance(item.get("sample_index"), bool)
            or not isinstance(item.get("sample_index"), int)
            or item["sample_index"] < 0
            or item["sample_index"] in sample_indices
            or item.get("split") not in {"seen_test", "unseen_test"}
            or isinstance(item.get("true_class_id"), bool)
            or not isinstance(item.get("true_class_id"), int)
            or isinstance(item.get("predicted_class_id"), bool)
            or not isinstance(item.get("predicted_class_id"), int)
            or item["predicted_class_id"] not in candidates
            or type(item.get("score")) is not float
            or not math.isfinite(item["score"])
            or not -1.000001 <= item["score"] <= 1.000001
        ):
            raise ValueError("GZSL prediction 条目无效")
        expected_group = seen if item["split"] == "seen_test" else unseen
        if item["true_class_id"] not in expected_group:
            raise ValueError("prediction split 与 true class 身份冲突")
        sample_indices.add(item["sample_index"])
        predictions.append(item["predicted_class_id"])
        targets.append(item["true_class_id"])
    return (
        numpy.asarray(predictions, dtype=numpy.int64),
        numpy.asarray(targets, dtype=numpy.int64),
    )


def validate_output(output_dir: Path) -> dict[str, Any]:
    output = require_directory(output_dir, "GZSL Run 输出")
    listed = validate_artifacts(output)
    required = {
        "checkpoint.pt",
        "evaluation.json",
        "predictions.json",
        "raw.log",
        "run.json",
    }
    if not required.issubset(listed):
        raise ValueError("GZSL Run 缺少必需产物")
    run = read_json(output / "run.json", "run.json", root=output)
    _validate_run(run)
    synthetic = run["mode"] == "synthetic_smoke"
    evaluation = _validate_evaluation(
        read_json(output / "evaluation.json", "evaluation.json", root=output),
        synthetic,
    )
    checkpoint = load_checkpoint(output / "checkpoint.pt")
    if (
        checkpoint["visual_dim"] != run["visual_dim"]
        or checkpoint["attribute_dim"] != run["attribute_dim"]
        or checkpoint["class_ids"].numel() != run["class_count"]
        or checkpoint["seed"] != run["seed"]
        or checkpoint["optimizer_steps"] != run["optimizer_steps"]
        or checkpoint["run_id"] != run["run_id"]
        or checkpoint["config_sha256"] != run["config_sha256"]
        or checkpoint["dataset_identity"] != run["dataset_identity"]
        or checkpoint["run_kind"] != run["run_kind"]
    ):
        raise ValueError("checkpoint 与架构/配置/数据/Run 身份不一致")
    model = build_model(run["visual_dim"], run["attribute_dim"])
    try:
        model.load_state_dict(checkpoint["model_state_dict"], strict=True)
    except (RuntimeError, TypeError, ValueError) as error:
        raise ValueError("checkpoint state_dict 无法 strict=True 加载") from error
    predictions, targets = _validate_predictions(
        read_json(output / "predictions.json", "predictions.json", root=output),
        run["evaluation_sample_count"],
        checkpoint["class_ids"].cpu().numpy(),
        checkpoint["seen_class_ids"].cpu().numpy(),
        checkpoint["unseen_class_ids"].cpu().numpy(),
    )
    recomputed = gzsl_class_average_metrics(
        predictions,
        targets,
        checkpoint["seen_class_ids"].cpu().numpy(),
        checkpoint["unseen_class_ids"].cpu().numpy(),
    )
    selected = (
        {
            "debug_seen_class_average_accuracy": recomputed["S"],
            "debug_unseen_class_average_accuracy": recomputed["U"],
            "debug_harmonic_mean": recomputed["H"],
        }
        if synthetic
        else recomputed
    )
    if selected != evaluation:
        raise ValueError("evaluation S/U/H 与 predictions 逐类重算结果不一致")
    if not synthetic:
        manifest = read_json(
            output / "dataset_manifest.json",
            "dataset_manifest.json",
            root=output,
        )
        if manifest.get("manifest_sha256") != run["dataset_identity"]["manifest_sha256"]:
            raise ValueError("dataset_manifest 与 Run 身份不一致")
    return {"metrics": evaluation, "artifacts": listed, "run": run}


def main() -> int:
    parser = argparse.ArgumentParser(description="严格校验 PACK-GZSL Run 输出")
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
