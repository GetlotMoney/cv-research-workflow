from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

from .contract import (
    canonical_sha256,
    dataset_identity,
    load_checkpoint,
    normalize_run_identity,
    prepare_output_directory,
    require_runtime,
    save_checkpoint,
    set_deterministic,
    write_artifact_manifest,
    write_bytes,
    write_json,
)
from .data import SegmentationDataset, capture_dataset, synthetic_tensors
from .metrics import segmentation_metrics
from .model import build_model


class _SyntheticDataset:
    def __init__(self, images: Any, masks: Any, names: list[str]) -> None:
        self.images = images
        self.masks = masks
        self.names = names

    def __len__(self) -> int:
        return len(self.names)

    def __getitem__(self, index: int) -> tuple[Any, Any, str]:
        return self.images[index], self.masks[index], self.names[index]


def _pixel_cross_entropy(logits: Any, masks: Any) -> Any:
    torch, _, _ = require_runtime()
    if (
        logits.ndim != 4
        or masks.ndim != 3
        or logits.shape[0] != masks.shape[0]
        or tuple(logits.shape[2:]) != tuple(masks.shape[1:])
    ):
        raise ValueError("分割 logits 与 mask 形状不匹配")
    classes = int(logits.shape[1])
    flattened_logits = logits.permute(0, 2, 3, 1).reshape(-1, classes)
    flattened_masks = masks.reshape(-1)
    return torch.nn.functional.cross_entropy(
        flattened_logits,
        flattened_masks,
        ignore_index=255,
    )


def _train_model(
    dataset: Any,
    *,
    num_classes: int,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    seed: int,
    device: str,
) -> tuple[Any, int]:
    torch = set_deterministic(seed, device)
    generator = torch.Generator(device="cpu").manual_seed(seed)
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=True,
        generator=generator,
        num_workers=0,
    )
    model = build_model(num_classes).to(device=device)
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    optimizer_steps = 0
    model.train()
    for _ in range(epochs):
        for images, masks, _ in loader:
            images = images.to(device=device)
            masks = masks.to(device=device)
            optimizer.zero_grad(set_to_none=True)
            loss = _pixel_cross_entropy(model(images), masks)
            if not bool(torch.isfinite(loss)):
                raise ValueError("训练 loss 变成 NaN/Inf")
            loss.backward()
            optimizer.step()
            optimizer_steps += 1
    if optimizer_steps <= 0:
        raise ValueError("训练没有执行 optimizer.step")
    return model, optimizer_steps


def _reload_model(
    checkpoint_path: Path,
    device: str,
) -> tuple[dict[str, Any], Any]:
    checkpoint = load_checkpoint(checkpoint_path)
    set_deterministic(checkpoint["seed"], device)
    model = build_model(checkpoint["num_classes"]).to(device=device)
    try:
        model.load_state_dict(checkpoint["model_state_dict"], strict=True)
    except (RuntimeError, TypeError, ValueError) as error:
        raise ValueError("checkpoint state_dict 与 TinySegmenter 架构不一致") from error
    model.eval()
    return checkpoint, model


def _predict_reloaded(
    checkpoint_path: Path,
    dataset: Any,
    device: str,
) -> tuple[dict[str, Any], Any, Any, list[str]]:
    torch, _, _ = require_runtime()
    checkpoint, model = _reload_model(checkpoint_path, device)
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=min(16, len(dataset)),
        shuffle=False,
        num_workers=0,
    )
    predictions = []
    targets = []
    names: list[str] = []
    with torch.no_grad():
        for images, masks, batch_names in loader:
            images = images.to(device=device)
            masks = masks.to(device=device)
            predictions.append(model(images).argmax(dim=1).to(dtype=torch.long))
            targets.append(masks.to(dtype=torch.long))
            names.extend(str(name) for name in batch_names)
    return checkpoint, torch.cat(predictions), torch.cat(targets), names


def _png_bytes(mask: Any) -> bytes:
    _, numpy, Image = require_runtime()
    array = mask.to(dtype=require_runtime()[0].uint8, device="cpu").numpy()
    image = Image.fromarray(numpy.asarray(array, dtype=numpy.uint8), mode="L")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def _write_predictions(output: Path, predictions: Any, names: list[str]) -> None:
    prediction_root = output / "predictions"
    masks_root = prediction_root / "masks"
    prediction_root.mkdir()
    masks_root.mkdir()
    records = []
    for index, (name, mask) in enumerate(
        zip(names, predictions, strict=True)
    ):
        relative = f"masks/{index:04d}.png"
        content = _png_bytes(mask)
        write_bytes(prediction_root / Path(*relative.split("/")), content)
        records.append(
            {
                "input": name,
                "mask": relative,
                "width": int(mask.shape[1]),
                "height": int(mask.shape[0]),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    write_json(
        prediction_root / "index.json",
        {
            "schema": "pack-seg.predictions.v1",
            "predictions": records,
        },
    )


def _evaluation_payload(
    result: dict[str, Any],
    *,
    synthetic: bool,
) -> dict[str, Any]:
    metric_name = "debug_mean_iou" if synthetic else "mean_iou"
    return {
        "schema": "pack-seg.evaluation.v1",
        "metrics": {metric_name: result["mean_iou"]},
        "confusion_matrix": result["confusion_matrix"],
        "per_class_iou": result["per_class_iou"],
        "included_classes": result["included_classes"],
        "definition": {
            "aggregation": "whole_dataset_confusion_matrix",
            "ignore_index": 255,
            "zero_union": "null_and_omitted_from_mean",
            "one_sided_union": "included",
        },
    }


def _finish_output(
    *,
    output: Path,
    checkpoint_path: Path,
    evaluation_dataset: Any,
    synthetic: bool,
    run: dict[str, Any],
    device: str,
) -> dict[str, Any]:
    _, evaluation_predictions, targets, names = _predict_reloaded(
        checkpoint_path,
        evaluation_dataset,
        device,
    )
    result = segmentation_metrics(
        evaluation_predictions,
        targets,
        run["num_classes"],
    )
    write_json(
        output / "evaluation.json",
        _evaluation_payload(result, synthetic=synthetic),
    )
    _, inference_predictions, _, inference_names = _predict_reloaded(
        checkpoint_path,
        evaluation_dataset,
        device,
    )
    if names != inference_names:
        raise ValueError("评估与推理读取到的样本身份不一致")
    _write_predictions(output, inference_predictions, inference_names)
    run["checkpoint_reloaded_for_evaluation"] = True
    run["checkpoint_reloaded_for_inference"] = True
    write_bytes(
        output / "raw.log",
        (
            f"phase={run['phase']}\n"
            f"run_id={run['run_id']}\n"
            f"optimizer_steps={run['optimizer_steps']}\n"
            f"evaluation_samples={len(evaluation_dataset)}\n"
        ).encode("utf-8"),
    )
    write_json(output / "run.json", run)
    write_artifact_manifest(output)
    return run


def run_synthetic(
    *,
    config: dict[str, Any],
    output_dir: Path,
    seed: int,
    run_id: str,
    config_sha256: str,
) -> dict[str, Any]:
    normalized_run_id, normalized_config_sha256 = normalize_run_identity(
        run_id,
        config_sha256,
    )
    set_deterministic(seed, "cpu")
    output = prepare_output_directory(output_dir)
    image_size = int(config["image_size"])
    num_classes = int(config["num_classes"])
    sample_count = int(config["sample_count"])
    images, masks, names = synthetic_tensors(
        sample_count=sample_count,
        image_size=image_size,
        num_classes=num_classes,
        seed=seed,
    )
    dataset = _SyntheticDataset(images, masks, names)
    model, optimizer_steps = _train_model(
        dataset,
        num_classes=num_classes,
        epochs=int(config["epochs"]),
        batch_size=int(config["batch_size"]),
        learning_rate=float(config["learning_rate"]),
        seed=seed,
        device="cpu",
    )
    checkpoint_path = output / "checkpoint.pt"
    save_checkpoint(
        checkpoint_path,
        {
            "schema": "pack-seg.checkpoint.v1",
            "architecture": "TinySegmenter-v1",
            "model_state_dict": model.state_dict(),
            "num_classes": num_classes,
            "image_size": image_size,
            "seed": seed,
            "optimizer_steps": optimizer_steps,
            "run_id": normalized_run_id,
            "config_sha256": normalized_config_sha256,
            "dataset_identity": {"kind": "synthetic_debug_only"},
            "run_kind": "synthetic_debug_only",
        },
    )
    run = {
        "schema": "pack-seg.run.v1",
        "phase": "synthetic_smoke",
        "mode": "synthetic_smoke",
        "run_kind": "synthetic_debug_only",
        "paper_eligible": False,
        "run_id": normalized_run_id,
        "config_sha256": normalized_config_sha256,
        "seed": seed,
        "device": "cpu",
        "num_classes": num_classes,
        "image_size": image_size,
        "training_sample_count": len(dataset),
        "evaluation_sample_count": len(dataset),
        "checkpoint": "checkpoint.pt",
        "optimizer_steps": optimizer_steps,
        "dataset_identity": {"kind": "synthetic_debug_only"},
    }
    return _finish_output(
        output=output,
        checkpoint_path=checkpoint_path,
        evaluation_dataset=dataset,
        synthetic=True,
        run=run,
        device="cpu",
    )


def run_local_training(
    *,
    config: dict[str, Any],
    data_root: Path,
    output_dir: Path,
    seed: int,
    run_id: str,
    config_sha256: str,
    dataset_id: str,
    version: str,
    source_uri: str,
    manifest_sha256: str,
) -> dict[str, Any]:
    normalized_run_id, normalized_config_sha256 = normalize_run_identity(
        run_id,
        config_sha256,
    )
    device = config["device"]
    set_deterministic(seed, device)
    manifest, snapshot = capture_dataset(data_root)
    if manifest["manifest_sha256"] != manifest_sha256:
        raise ValueError("真实数据 manifest SHA-256 与冻结身份不一致")
    identity = dataset_identity(
        dataset_id=dataset_id,
        version=version,
        source_uri=source_uri,
        manifest_sha256=manifest_sha256,
        split={
            "train": config["train_split"],
            "evaluation": config["evaluation_split"],
        },
    )
    image_size = int(config["image_size"])
    num_classes = int(config["num_classes"])
    train_dataset = SegmentationDataset(
        Path(data_root) / config["train_split"],
        image_size,
        num_classes,
        snapshot=snapshot,
        snapshot_root=Path(data_root),
    )
    evaluation_dataset = SegmentationDataset(
        Path(data_root) / config["evaluation_split"],
        image_size,
        num_classes,
        snapshot=snapshot,
        snapshot_root=Path(data_root),
    )
    output = prepare_output_directory(output_dir)
    model, optimizer_steps = _train_model(
        train_dataset,
        num_classes=num_classes,
        epochs=int(config["epochs"]),
        batch_size=int(config["batch_size"]),
        learning_rate=float(config["learning_rate"]),
        seed=seed,
        device=device,
    )
    checkpoint_path = output / "checkpoint.pt"
    save_checkpoint(
        checkpoint_path,
        {
            "schema": "pack-seg.checkpoint.v1",
            "architecture": "TinySegmenter-v1",
            "model_state_dict": model.state_dict(),
            "num_classes": num_classes,
            "image_size": image_size,
            "seed": seed,
            "optimizer_steps": optimizer_steps,
            "run_id": normalized_run_id,
            "config_sha256": normalized_config_sha256,
            "dataset_identity": identity,
            "run_kind": "local_dataset_baseline",
        },
    )
    write_json(output / "dataset_manifest.json", manifest)
    run = {
        "schema": "pack-seg.run.v1",
        "phase": "train",
        "mode": "local_segmentation",
        "run_kind": "local_dataset_baseline",
        "paper_eligible": False,
        "run_id": normalized_run_id,
        "config_sha256": normalized_config_sha256,
        "seed": seed,
        "device": device,
        "num_classes": num_classes,
        "image_size": image_size,
        "training_sample_count": len(train_dataset),
        "evaluation_sample_count": len(evaluation_dataset),
        "checkpoint": "checkpoint.pt",
        "optimizer_steps": optimizer_steps,
        "dataset_identity": identity,
    }
    return _finish_output(
        output=output,
        checkpoint_path=checkpoint_path,
        evaluation_dataset=evaluation_dataset,
        synthetic=False,
        run=run,
        device=device,
    )


def evaluate_existing(
    checkpoint_path: Path,
    data_root: Path,
    output_dir: Path,
    *,
    device: str = "cpu",
) -> dict[str, Any]:
    checkpoint, _ = _reload_model(checkpoint_path, device)
    identity = checkpoint["dataset_identity"]
    if checkpoint["run_kind"] != "local_dataset_baseline":
        raise ValueError("独立 evaluate 只接受正式本地数据 checkpoint")
    manifest, snapshot = capture_dataset(data_root)
    if manifest["manifest_sha256"] != identity["manifest_sha256"]:
        raise ValueError("evaluate 数据 manifest 与 checkpoint 不一致")
    dataset = SegmentationDataset(
        Path(data_root) / identity["split"]["evaluation"],
        checkpoint["image_size"],
        checkpoint["num_classes"],
        snapshot=snapshot,
        snapshot_root=Path(data_root),
    )
    output = prepare_output_directory(output_dir)
    _, predictions, targets, names = _predict_reloaded(
        checkpoint_path,
        dataset,
        device,
    )
    result = segmentation_metrics(predictions, targets, checkpoint["num_classes"])
    write_json(output / "evaluation.json", _evaluation_payload(result, synthetic=False))
    _write_predictions(output, predictions, names)
    write_bytes(output / "raw.log", b"phase=evaluate\ncheckpoint_reloaded=true\n")
    write_json(
        output / "run.json",
        {
            "schema": "pack-seg.run.v1",
            "phase": "evaluate",
            "run_kind": "local_dataset_baseline",
            "paper_eligible": False,
            "device": device,
            "checkpoint_reloaded": True,
            "sample_count": len(dataset),
            "dataset_identity": identity,
        },
    )
    write_artifact_manifest(output)
    return result


def infer_existing(
    checkpoint_path: Path,
    data_root: Path,
    output_dir: Path,
    split: str,
    *,
    device: str = "cpu",
) -> int:
    checkpoint, _ = _reload_model(checkpoint_path, device)
    identity = checkpoint["dataset_identity"]
    if checkpoint["run_kind"] != "local_dataset_baseline":
        raise ValueError("独立 infer 只接受正式本地数据 checkpoint")
    manifest, snapshot = capture_dataset(data_root)
    if manifest["manifest_sha256"] != identity["manifest_sha256"]:
        raise ValueError("infer 数据 manifest 与 checkpoint 不一致")
    if split not in set(identity["split"].values()):
        raise ValueError("infer split 不在 checkpoint 冻结的数据划分中")
    dataset = SegmentationDataset(
        Path(data_root) / split,
        checkpoint["image_size"],
        checkpoint["num_classes"],
        snapshot=snapshot,
        snapshot_root=Path(data_root),
    )
    output = prepare_output_directory(output_dir)
    _, predictions, _, names = _predict_reloaded(
        checkpoint_path,
        dataset,
        device,
    )
    _write_predictions(output, predictions, names)
    write_bytes(output / "raw.log", b"phase=infer\ncheckpoint_reloaded=true\n")
    write_json(
        output / "run.json",
        {
            "schema": "pack-seg.run.v1",
            "phase": "infer",
            "run_kind": "local_dataset_baseline",
            "paper_eligible": False,
            "device": device,
            "checkpoint_reloaded": True,
            "sample_count": len(dataset),
            "dataset_identity": identity,
        },
    )
    write_artifact_manifest(output)
    return len(dataset)
