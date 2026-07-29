from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

from .contract import (
    load_checkpoint,
    make_dataset_identity,
    normalize_run,
    prepare_output,
    require_runtime,
    save_checkpoint,
    set_deterministic,
    write_artifacts,
    write_bytes,
    write_json,
)
from .data import PairedX2Dataset, capture_dataset, synthetic_pairs
from .metrics import cumulative_rgb_psnr
from .model import build_model


class _SyntheticPairs:
    def __init__(self, low: list[Any], high: list[Any], names: list[str]) -> None:
        self.low = low
        self.high = high
        self.names = names

    def __len__(self) -> int:
        return len(self.names)

    def __getitem__(self, index: int) -> tuple[Any, Any, str]:
        return self.low[index], self.high[index], self.names[index]


def _train(
    dataset: Any,
    *,
    epochs: int,
    learning_rate: float,
    seed: int,
    device: str,
) -> tuple[Any, int]:
    torch = set_deterministic(seed, device)
    model = build_model().to(device=device)
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    loss_function = torch.nn.L1Loss().to(device=device)
    optimizer_steps = 0
    model.train()
    generator = torch.Generator(device="cpu").manual_seed(seed)
    order = torch.randperm(len(dataset), generator=generator).tolist()
    for _ in range(epochs):
        for index in order:
            lr, hr, _ = dataset[index]
            lr = lr.to(device=device)
            hr = hr.to(device=device)
            optimizer.zero_grad(set_to_none=True)
            predicted = model(lr.unsqueeze(0))
            loss = loss_function(predicted, hr.unsqueeze(0))
            if not bool(torch.isfinite(loss)):
                raise ValueError("SR 训练 loss 变成 NaN/Inf")
            loss.backward()
            optimizer.step()
            optimizer_steps += 1
    if optimizer_steps <= 0:
        raise ValueError("SR 训练没有执行 optimizer.step")
    return model, optimizer_steps


def _reload(
    checkpoint_path: Path,
    device: str,
) -> tuple[dict[str, Any], Any]:
    checkpoint = load_checkpoint(checkpoint_path)
    set_deterministic(checkpoint["seed"], device)
    model = build_model().to(device=device)
    try:
        model.load_state_dict(checkpoint["model_state_dict"], strict=True)
    except (RuntimeError, TypeError, ValueError) as error:
        raise ValueError("checkpoint 与 TinyPixelShuffle 架构不一致") from error
    model.eval()
    return checkpoint, model


def _uint8_rgb(tensor: Any) -> Any:
    _, numpy, _ = require_runtime()
    array = (
        tensor.detach()
        .to(device="cpu")
        .clamp(0.0, 1.0)
        .mul(255.0)
        .round()
        .to(dtype=require_runtime()[0].uint8)
        .permute(1, 2, 0)
        .contiguous()
        .numpy()
    )
    return numpy.asarray(array, dtype=numpy.uint8).copy()


def _predict(
    checkpoint_path: Path,
    dataset: Any,
    device: str,
) -> tuple[dict[str, Any], list[Any], list[Any], list[str]]:
    torch, _, _ = require_runtime()
    checkpoint, model = _reload(checkpoint_path, device)
    predicted_images = []
    target_images = []
    names = []
    with torch.no_grad():
        for index in range(len(dataset)):
            lr, hr, name = dataset[index]
            predicted = model(lr.unsqueeze(0).to(device=device))[0]
            predicted_images.append(_uint8_rgb(predicted))
            target_images.append(_uint8_rgb(hr))
            names.append(str(name))
    return checkpoint, predicted_images, target_images, names


def _png_bytes(array: Any) -> bytes:
    _, numpy, Image = require_runtime()
    image = Image.fromarray(numpy.asarray(array, dtype=numpy.uint8), mode="RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def _write_predictions(output: Path, predicted: list[Any], names: list[str]) -> None:
    root = output / "predictions"
    images = root / "images"
    root.mkdir()
    images.mkdir()
    records = []
    for index, (array, name) in enumerate(zip(predicted, names, strict=True)):
        relative = f"images/{index:04d}.png"
        content = _png_bytes(array)
        write_bytes(root / Path(*relative.split("/")), content)
        records.append(
            {
                "input": name,
                "image": relative,
                "width": int(array.shape[1]),
                "height": int(array.shape[0]),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    write_json(
        root / "index.json",
        {"schema": "pack-sr.predictions.v1", "predictions": records},
    )


def _evaluation_payload(
    result: dict[str, float | int],
    *,
    synthetic: bool,
) -> dict[str, Any]:
    metric_name = "debug_psnr_rgb_x2" if synthetic else "psnr_rgb_x2"
    return {
        "schema": "pack-sr.evaluation.v1",
        "metrics": {metric_name: result["psnr_rgb_x2"]},
        "sse": result["sse"],
        "value_count": result["value_count"],
        "mse": result["mse"],
        "definition": {
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
        },
    }


def _finish(
    *,
    output: Path,
    checkpoint_path: Path,
    evaluation_dataset: Any,
    run: dict[str, Any],
    synthetic: bool,
    device: str,
) -> dict[str, Any]:
    _, predicted, target, names = _predict(
        checkpoint_path,
        evaluation_dataset,
        device,
    )
    result = cumulative_rgb_psnr(zip(predicted, target, strict=True))
    write_json(
        output / "evaluation.json",
        _evaluation_payload(result, synthetic=synthetic),
    )
    _, inference_images, _, inference_names = _predict(
        checkpoint_path,
        evaluation_dataset,
        device,
    )
    if names != inference_names:
        raise ValueError("SR 评估与推理样本身份不一致")
    _write_predictions(output, inference_images, inference_names)
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
    write_artifacts(output)
    return run


def run_synthetic(
    *,
    config: dict[str, Any],
    output_dir: Path,
    seed: int,
    run_id: str,
    config_sha256: str,
) -> dict[str, Any]:
    selected_run_id, selected_config_sha256 = normalize_run(
        run_id,
        config_sha256,
    )
    low, high, names = synthetic_pairs(
        sample_count=int(config["sample_count"]),
        lr_height=int(config["lr_height"]),
        lr_width=int(config["lr_width"]),
        seed=seed,
    )
    dataset = _SyntheticPairs(low, high, names)
    set_deterministic(seed, "cpu")
    output = prepare_output(output_dir)
    model, optimizer_steps = _train(
        dataset,
        epochs=int(config["epochs"]),
        learning_rate=float(config["learning_rate"]),
        seed=seed,
        device="cpu",
    )
    checkpoint_path = output / "checkpoint.pt"
    save_checkpoint(
        checkpoint_path,
        {
            "schema": "pack-sr.checkpoint.v1",
            "architecture": "TinyPixelShuffle-v1",
            "model_state_dict": model.state_dict(),
            "scale": 2,
            "seed": seed,
            "optimizer_steps": optimizer_steps,
            "run_id": selected_run_id,
            "config_sha256": selected_config_sha256,
            "dataset_identity": {"kind": "synthetic_debug_only"},
            "run_kind": "synthetic_debug_only",
        },
    )
    run = {
        "schema": "pack-sr.run.v1",
        "phase": "synthetic_smoke",
        "mode": "synthetic_smoke",
        "run_kind": "synthetic_debug_only",
        "paper_eligible": False,
        "run_id": selected_run_id,
        "config_sha256": selected_config_sha256,
        "seed": seed,
        "device": "cpu",
        "scale": 2,
        "training_sample_count": len(dataset),
        "evaluation_sample_count": len(dataset),
        "checkpoint": "checkpoint.pt",
        "optimizer_steps": optimizer_steps,
        "dataset_identity": {"kind": "synthetic_debug_only"},
    }
    return _finish(
        output=output,
        checkpoint_path=checkpoint_path,
        evaluation_dataset=dataset,
        run=run,
        synthetic=True,
        device="cpu",
    )


def run_local(
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
    selected_run_id, selected_config_sha256 = normalize_run(
        run_id,
        config_sha256,
    )
    device = config["device"]
    set_deterministic(seed, device)
    manifest, snapshot = capture_dataset(data_root)
    if manifest["manifest_sha256"] != manifest_sha256:
        raise ValueError("真实 SR 数据 manifest SHA-256 与冻结身份不一致")
    identity = make_dataset_identity(
        dataset_id=dataset_id,
        version=version,
        source_uri=source_uri,
        manifest_sha256=manifest_sha256,
    )
    train_dataset = PairedX2Dataset(
        Path(data_root) / "train",
        snapshot=snapshot,
        snapshot_root=Path(data_root),
    )
    evaluation_dataset = PairedX2Dataset(
        Path(data_root) / "val",
        snapshot=snapshot,
        snapshot_root=Path(data_root),
    )
    output = prepare_output(output_dir)
    model, optimizer_steps = _train(
        train_dataset,
        epochs=int(config["epochs"]),
        learning_rate=float(config["learning_rate"]),
        seed=seed,
        device=device,
    )
    checkpoint_path = output / "checkpoint.pt"
    save_checkpoint(
        checkpoint_path,
        {
            "schema": "pack-sr.checkpoint.v1",
            "architecture": "TinyPixelShuffle-v1",
            "model_state_dict": model.state_dict(),
            "scale": 2,
            "seed": seed,
            "optimizer_steps": optimizer_steps,
            "run_id": selected_run_id,
            "config_sha256": selected_config_sha256,
            "dataset_identity": identity,
            "run_kind": "local_dataset_baseline",
        },
    )
    write_json(output / "dataset_manifest.json", manifest)
    run = {
        "schema": "pack-sr.run.v1",
        "phase": "train",
        "mode": "local_sr_x2",
        "run_kind": "local_dataset_baseline",
        "paper_eligible": False,
        "run_id": selected_run_id,
        "config_sha256": selected_config_sha256,
        "seed": seed,
        "device": device,
        "scale": 2,
        "training_sample_count": len(train_dataset),
        "evaluation_sample_count": len(evaluation_dataset),
        "checkpoint": "checkpoint.pt",
        "optimizer_steps": optimizer_steps,
        "dataset_identity": identity,
    }
    return _finish(
        output=output,
        checkpoint_path=checkpoint_path,
        evaluation_dataset=evaluation_dataset,
        run=run,
        synthetic=False,
        device=device,
    )


def evaluate_existing(
    checkpoint_path: Path,
    data_root: Path,
    output_dir: Path,
    *,
    device: str = "cpu",
) -> dict[str, Any]:
    checkpoint, _ = _reload(checkpoint_path, device)
    if checkpoint["run_kind"] != "local_dataset_baseline":
        raise ValueError("独立 evaluate 只接受正式本地 checkpoint")
    manifest, snapshot = capture_dataset(data_root)
    if manifest["manifest_sha256"] != checkpoint["dataset_identity"]["manifest_sha256"]:
        raise ValueError("evaluate 数据 manifest 与 checkpoint 不一致")
    dataset = PairedX2Dataset(
        Path(data_root) / "val",
        snapshot=snapshot,
        snapshot_root=Path(data_root),
    )
    output = prepare_output(output_dir)
    _, predicted, target, names = _predict(
        checkpoint_path,
        dataset,
        device,
    )
    result = cumulative_rgb_psnr(zip(predicted, target, strict=True))
    write_json(output / "evaluation.json", _evaluation_payload(result, synthetic=False))
    _write_predictions(output, predicted, names)
    write_bytes(output / "raw.log", b"phase=evaluate\ncheckpoint_reloaded=true\n")
    write_json(
        output / "run.json",
        {
            "schema": "pack-sr.run.v1",
            "phase": "evaluate",
            "run_kind": "local_dataset_baseline",
            "paper_eligible": False,
            "device": device,
            "checkpoint_reloaded": True,
            "sample_count": len(dataset),
            "dataset_identity": checkpoint["dataset_identity"],
        },
    )
    write_artifacts(output)
    return result


def infer_existing(
    checkpoint_path: Path,
    data_root: Path,
    output_dir: Path,
    split: str,
    *,
    device: str = "cpu",
) -> int:
    checkpoint, _ = _reload(checkpoint_path, device)
    if checkpoint["run_kind"] != "local_dataset_baseline":
        raise ValueError("独立 infer 只接受正式本地 checkpoint")
    manifest, snapshot = capture_dataset(data_root)
    if manifest["manifest_sha256"] != checkpoint["dataset_identity"]["manifest_sha256"]:
        raise ValueError("infer 数据 manifest 与 checkpoint 不一致")
    if split not in {"train", "val"}:
        raise ValueError("infer split 只允许 train/val")
    dataset = PairedX2Dataset(
        Path(data_root) / split,
        snapshot=snapshot,
        snapshot_root=Path(data_root),
    )
    output = prepare_output(output_dir)
    _, predicted, _, names = _predict(
        checkpoint_path,
        dataset,
        device,
    )
    _write_predictions(output, predicted, names)
    write_bytes(output / "raw.log", b"phase=infer\ncheckpoint_reloaded=true\n")
    write_json(
        output / "run.json",
        {
            "schema": "pack-sr.run.v1",
            "phase": "infer",
            "run_kind": "local_dataset_baseline",
            "paper_eligible": False,
            "device": device,
            "checkpoint_reloaded": True,
            "sample_count": len(dataset),
            "dataset_identity": checkpoint["dataset_identity"],
        },
    )
    write_artifacts(output)
    return len(dataset)
