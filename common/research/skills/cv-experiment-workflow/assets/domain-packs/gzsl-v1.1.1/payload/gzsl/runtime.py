from __future__ import annotations

from pathlib import Path
from typing import Any

from .contract import (
    load_checkpoint,
    make_dataset_identity,
    normalize_run,
    prepare_output,
    require_device,
    require_runtime,
    save_checkpoint,
    set_deterministic,
    write_artifacts,
    write_bytes,
    write_json,
)
from .data import capture_dataset, load_gzsl_npz_bytes, synthetic_dataset
from .metrics import gzsl_class_average_metrics
from .model import build_model


def _target_attributes(
    data: dict[str, Any],
    labels: Any,
    *,
    device: str,
) -> Any:
    torch, _ = require_runtime()
    rows = [data["class_to_attribute_row"][int(label)] for label in labels.tolist()]
    return torch.from_numpy(data["attributes"][rows]).to(
        device=require_device(device),
        dtype=torch.float32,
    )


def _train(
    data: dict[str, Any],
    *,
    epochs: int,
    learning_rate: float,
    seed: int,
    device: str,
) -> tuple[Any, int]:
    torch = set_deterministic(seed, device)
    torch_device = require_device(device)
    features = torch.from_numpy(
        data["features"][data["train_indices"]]
    ).to(device=torch_device, dtype=torch.float32)
    labels = data["labels"][data["train_indices"]]
    targets = _target_attributes(data, labels, device=device)
    model = build_model(
        data["features"].shape[1],
        data["attributes"].shape[1],
    ).to(device=torch_device)
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    loss_function = torch.nn.MSELoss()
    optimizer_steps = 0
    model.train()
    for _ in range(epochs):
        optimizer.zero_grad(set_to_none=True)
        loss = loss_function(model(features), targets)
        if not bool(torch.isfinite(loss)):
            raise ValueError("GZSL 训练 loss 变成 NaN/Inf")
        loss.backward()
        optimizer.step()
        optimizer_steps += 1
    if optimizer_steps <= 0:
        raise ValueError("GZSL 训练没有执行 optimizer.step")
    return model, optimizer_steps


def _reload(
    checkpoint_path: Path,
    *,
    device: str,
) -> tuple[dict[str, Any], Any]:
    checkpoint = load_checkpoint(checkpoint_path)
    model = build_model(
        checkpoint["visual_dim"],
        checkpoint["attribute_dim"],
    )
    try:
        model.load_state_dict(checkpoint["model_state_dict"], strict=True)
    except (RuntimeError, TypeError, ValueError) as error:
        raise ValueError("checkpoint 与 LinearVisualToAttribute 架构不一致") from error
    model = model.to(device=require_device(device))
    model.eval()
    return checkpoint, model


def _classify(
    checkpoint_path: Path,
    data: dict[str, Any],
    indices: Any,
    *,
    device: str,
) -> tuple[Any, Any, Any]:
    torch, numpy = require_runtime()
    torch_device = require_device(device)
    checkpoint, model = _reload(checkpoint_path, device=device)
    if (
        checkpoint["visual_dim"] != data["features"].shape[1]
        or checkpoint["attribute_dim"] != data["attributes"].shape[1]
        or not numpy.array_equal(
            checkpoint["class_ids"].cpu().numpy(),
            data["class_ids"],
        )
        or not numpy.array_equal(
            checkpoint["attributes"].cpu().numpy(),
            data["attributes"],
        )
    ):
        raise ValueError("checkpoint 与当前 GZSL 数据架构/类别/属性身份不一致")
    features = torch.from_numpy(data["features"][indices]).to(
        device=torch_device,
        dtype=torch.float32,
    )
    with torch.no_grad():
        mapped = torch.nn.functional.normalize(model(features), dim=1)
        attributes = torch.nn.functional.normalize(
            checkpoint["attributes"].to(
                device=torch_device,
                dtype=torch.float32,
            ),
            dim=1,
        )
        similarities = mapped @ attributes.T
        scores, rows = similarities.max(dim=1)
    predicted_ids = (
        checkpoint["class_ids"].to(device=torch_device)[rows].cpu().numpy().astype(
        numpy.int64,
        copy=True,
        )
    )
    return predicted_ids, scores.cpu().numpy(), data["labels"][indices].copy()


def _evaluation_indices(data: dict[str, Any]) -> tuple[Any, list[str]]:
    _, numpy = require_runtime()
    seen = data["test_seen_indices"]
    unseen = data["test_unseen_indices"]
    indices = numpy.concatenate([seen, unseen]).astype(numpy.int64, copy=False)
    split_names = ["seen_test"] * len(seen) + ["unseen_test"] * len(unseen)
    return indices, split_names


def _formal_metrics(data: dict[str, Any], predicted: Any, targets: Any) -> dict[str, float]:
    return gzsl_class_average_metrics(
        predicted,
        targets,
        data["seen_class_ids"],
        data["unseen_class_ids"],
    )


def _write_predictions(
    output: Path,
    indices: Any,
    split_names: list[str],
    predicted: Any,
    targets: Any,
    scores: Any,
) -> None:
    write_json(
        output / "predictions.json",
        {
            "schema": "pack-gzsl.predictions.v1",
            "predictions": [
                {
                    "sample_index": int(sample_index),
                    "split": split,
                    "true_class_id": int(target),
                    "predicted_class_id": int(prediction),
                    "score": float(score),
                }
                for sample_index, split, target, prediction, score in zip(
                    indices.tolist(),
                    split_names,
                    targets.tolist(),
                    predicted.tolist(),
                    scores.tolist(),
                    strict=True,
                )
            ],
        },
    )


def _evaluation_payload(
    metrics: dict[str, float],
    *,
    synthetic: bool,
) -> dict[str, Any]:
    selected = (
        {
            "debug_seen_class_average_accuracy": metrics["S"],
            "debug_unseen_class_average_accuracy": metrics["U"],
            "debug_harmonic_mean": metrics["H"],
        }
        if synthetic
        else metrics
    )
    return {
        "schema": "pack-gzsl.evaluation.v1",
        "metrics": selected,
        "definition": {
            "accuracy_averaging": "class_average",
            "candidate_classes": "seen_union_unseen",
            "similarity": "cosine",
            "calibration": "none",
            "harmonic_mean": "2*S*U/(S+U); 0 when S+U=0",
        },
    }


def _finish(
    *,
    output: Path,
    checkpoint_path: Path,
    data: dict[str, Any],
    run: dict[str, Any],
    synthetic: bool,
    device: str,
) -> dict[str, Any]:
    indices, split_names = _evaluation_indices(data)
    predicted, scores, targets = _classify(
        checkpoint_path,
        data,
        indices,
        device=device,
    )
    metrics = _formal_metrics(data, predicted, targets)
    write_json(
        output / "evaluation.json",
        _evaluation_payload(metrics, synthetic=synthetic),
    )
    inference_predictions, inference_scores, inference_targets = _classify(
        checkpoint_path,
        data,
        indices,
        device=device,
    )
    torch, numpy = require_runtime()
    del torch
    if (
        not numpy.array_equal(predicted, inference_predictions)
        or not numpy.array_equal(targets, inference_targets)
        or not numpy.allclose(scores, inference_scores, rtol=0.0, atol=0.0)
    ):
        raise ValueError("GZSL 评估与推理重载结果不一致")
    _write_predictions(
        output,
        indices,
        split_names,
        inference_predictions,
        inference_targets,
        inference_scores,
    )
    run["checkpoint_reloaded_for_evaluation"] = True
    run["checkpoint_reloaded_for_inference"] = True
    write_bytes(
        output / "raw.log",
        (
            f"phase={run['phase']}\n"
            f"run_id={run['run_id']}\n"
            f"optimizer_steps={run['optimizer_steps']}\n"
            f"evaluation_samples={len(indices)}\n"
        ).encode("utf-8"),
    )
    write_json(output / "run.json", run)
    write_artifacts(output)
    return run


def _checkpoint_payload(
    *,
    model: Any,
    data: dict[str, Any],
    seed: int,
    optimizer_steps: int,
    run_id: str,
    config_sha256: str,
    dataset_identity: dict[str, Any],
    run_kind: str,
) -> dict[str, Any]:
    torch, _ = require_runtime()
    return {
        "schema": "pack-gzsl.checkpoint.v1",
        "architecture": "LinearVisualToAttribute-v1",
        "model_state_dict": model.state_dict(),
        "visual_dim": int(data["features"].shape[1]),
        "attribute_dim": int(data["attributes"].shape[1]),
        "class_ids": torch.from_numpy(data["class_ids"].copy()),
        "seen_class_ids": torch.from_numpy(data["seen_class_ids"].copy()),
        "unseen_class_ids": torch.from_numpy(data["unseen_class_ids"].copy()),
        "attributes": torch.from_numpy(data["attributes"].copy()),
        "seed": seed,
        "optimizer_steps": optimizer_steps,
        "run_id": run_id,
        "config_sha256": config_sha256,
        "dataset_identity": dataset_identity,
        "run_kind": run_kind,
    }


def run_synthetic(
    *,
    config: dict[str, Any],
    output_dir: Path,
    seed: int,
    run_id: str,
    config_sha256: str,
) -> dict[str, Any]:
    device = config.get("device")
    require_device(device, synthetic=True)
    selected_run_id, selected_config_sha256 = normalize_run(
        run_id,
        config_sha256,
    )
    data = synthetic_dataset(seed)
    output = prepare_output(output_dir)
    model, optimizer_steps = _train(
        data,
        epochs=int(config["epochs"]),
        learning_rate=float(config["learning_rate"]),
        seed=seed,
        device=device,
    )
    identity = {"kind": "synthetic_debug_only"}
    checkpoint_path = output / "checkpoint.pt"
    save_checkpoint(
        checkpoint_path,
        _checkpoint_payload(
            model=model,
            data=data,
            seed=seed,
            optimizer_steps=optimizer_steps,
            run_id=selected_run_id,
            config_sha256=selected_config_sha256,
            dataset_identity=identity,
            run_kind="synthetic_debug_only",
        ),
    )
    run = {
        "schema": "pack-gzsl.run.v1",
        "phase": "synthetic_smoke",
        "mode": "synthetic_smoke",
        "run_kind": "synthetic_debug_only",
        "paper_eligible": False,
        "run_id": selected_run_id,
        "config_sha256": selected_config_sha256,
        "seed": seed,
        "device": device,
        "visual_dim": int(data["features"].shape[1]),
        "attribute_dim": int(data["attributes"].shape[1]),
        "class_count": int(data["class_ids"].size),
        "training_sample_count": int(data["train_indices"].size),
        "evaluation_sample_count": int(
            data["test_seen_indices"].size + data["test_unseen_indices"].size
        ),
        "checkpoint": "checkpoint.pt",
        "optimizer_steps": optimizer_steps,
        "dataset_identity": identity,
    }
    return _finish(
        output=output,
        checkpoint_path=checkpoint_path,
        data=data,
        run=run,
        synthetic=True,
        device=device,
    )


def run_local(
    *,
    config: dict[str, Any],
    data_path: Path,
    output_dir: Path,
    seed: int,
    run_id: str,
    config_sha256: str,
    dataset_id: str,
    version: str,
    source_uri: str,
    manifest_sha256: str,
    device: str = "cuda",
) -> dict[str, Any]:
    require_device(device)
    selected_run_id, selected_config_sha256 = normalize_run(
        run_id,
        config_sha256,
    )
    manifest, snapshot = capture_dataset(data_path)
    if manifest["manifest_sha256"] != manifest_sha256:
        raise ValueError("真实 GZSL manifest SHA-256 与冻结身份不一致")
    data = load_gzsl_npz_bytes(snapshot)
    identity = make_dataset_identity(
        dataset_id=dataset_id,
        version=version,
        source_uri=source_uri,
        manifest_sha256=manifest_sha256,
    )
    output = prepare_output(output_dir)
    model, optimizer_steps = _train(
        data,
        epochs=int(config["epochs"]),
        learning_rate=float(config["learning_rate"]),
        seed=seed,
        device=device,
    )
    checkpoint_path = output / "checkpoint.pt"
    save_checkpoint(
        checkpoint_path,
        _checkpoint_payload(
            model=model,
            data=data,
            seed=seed,
            optimizer_steps=optimizer_steps,
            run_id=selected_run_id,
            config_sha256=selected_config_sha256,
            dataset_identity=identity,
            run_kind="local_dataset_baseline",
        ),
    )
    write_json(output / "dataset_manifest.json", manifest)
    run = {
        "schema": "pack-gzsl.run.v1",
        "phase": "train",
        "mode": "local_gzsl_npz",
        "run_kind": "local_dataset_baseline",
        "paper_eligible": False,
        "run_id": selected_run_id,
        "config_sha256": selected_config_sha256,
        "seed": seed,
        "device": device,
        "visual_dim": int(data["features"].shape[1]),
        "attribute_dim": int(data["attributes"].shape[1]),
        "class_count": int(data["class_ids"].size),
        "training_sample_count": int(data["train_indices"].size),
        "evaluation_sample_count": int(
            data["test_seen_indices"].size + data["test_unseen_indices"].size
        ),
        "checkpoint": "checkpoint.pt",
        "optimizer_steps": optimizer_steps,
        "dataset_identity": identity,
    }
    return _finish(
        output=output,
        checkpoint_path=checkpoint_path,
        data=data,
        run=run,
        synthetic=False,
        device=device,
    )


def evaluate_existing(
    checkpoint_path: Path,
    data_path: Path,
    output_dir: Path,
    *,
    device: str = "cuda",
) -> dict[str, float]:
    require_device(device)
    checkpoint, _ = _reload(checkpoint_path, device=device)
    if checkpoint["run_kind"] != "local_dataset_baseline":
        raise ValueError("独立 evaluate 只接受正式本地 checkpoint")
    manifest, snapshot = capture_dataset(data_path)
    if manifest["manifest_sha256"] != checkpoint["dataset_identity"]["manifest_sha256"]:
        raise ValueError("evaluate 数据 manifest 与 checkpoint 不一致")
    data = load_gzsl_npz_bytes(snapshot)
    indices, split_names = _evaluation_indices(data)
    predicted, scores, targets = _classify(
        checkpoint_path,
        data,
        indices,
        device=device,
    )
    metrics = _formal_metrics(data, predicted, targets)
    output = prepare_output(output_dir)
    write_json(output / "evaluation.json", _evaluation_payload(metrics, synthetic=False))
    _write_predictions(output, indices, split_names, predicted, targets, scores)
    write_bytes(output / "raw.log", b"phase=evaluate\ncheckpoint_reloaded=true\n")
    write_json(
        output / "run.json",
        {
            "schema": "pack-gzsl.run.v1",
            "phase": "evaluate",
            "run_kind": "local_dataset_baseline",
            "paper_eligible": False,
            "device": device,
            "checkpoint_reloaded": True,
            "sample_count": len(indices),
            "dataset_identity": checkpoint["dataset_identity"],
        },
    )
    write_artifacts(output)
    return metrics


def infer_existing(
    checkpoint_path: Path,
    data_path: Path,
    output_dir: Path,
    *,
    device: str = "cuda",
) -> int:
    require_device(device)
    checkpoint, _ = _reload(checkpoint_path, device=device)
    if checkpoint["run_kind"] != "local_dataset_baseline":
        raise ValueError("独立 infer 只接受正式本地 checkpoint")
    manifest, snapshot = capture_dataset(data_path)
    if manifest["manifest_sha256"] != checkpoint["dataset_identity"]["manifest_sha256"]:
        raise ValueError("infer 数据 manifest 与 checkpoint 不一致")
    data = load_gzsl_npz_bytes(snapshot)
    indices, split_names = _evaluation_indices(data)
    predicted, scores, targets = _classify(
        checkpoint_path,
        data,
        indices,
        device=device,
    )
    output = prepare_output(output_dir)
    _write_predictions(output, indices, split_names, predicted, targets, scores)
    write_bytes(output / "raw.log", b"phase=infer\ncheckpoint_reloaded=true\n")
    write_json(
        output / "run.json",
        {
            "schema": "pack-gzsl.run.v1",
            "phase": "infer",
            "run_kind": "local_dataset_baseline",
            "paper_eligible": False,
            "device": device,
            "checkpoint_reloaded": True,
            "sample_count": len(indices),
            "dataset_identity": checkpoint["dataset_identity"],
        },
    )
    write_artifacts(output)
    return len(indices)
