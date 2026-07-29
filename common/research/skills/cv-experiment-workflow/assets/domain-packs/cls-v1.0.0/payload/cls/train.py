from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .contract import (
    load_checkpoint,
    load_config,
    normalize_run_identity,
    positive_float,
    positive_int,
    prepare_output_directory,
    require_runtime,
    safe_split_name,
    save_checkpoint,
    set_deterministic,
    write_json,
)
from .data import ImageFolderDataset
from .metrics import classification_metrics
from .model import build_model


def _collect_logits(
    model: Any,
    loader: Any,
    device: str,
) -> tuple[Any, Any, list[str]]:
    torch, _, _ = require_runtime()
    model.eval()
    logits = []
    targets = []
    inputs: list[str] = []
    with torch.no_grad():
        for images, labels, paths in loader:
            images = images.to(device=device)
            labels = labels.to(device=device)
            logits.append(model(images))
            targets.append(labels)
            inputs.extend(str(path) for path in paths)
    return torch.cat(logits), torch.cat(targets), inputs


def run_training(
    config_path: Path,
    data_root: Path,
    output_dir: Path,
    *,
    seed_override: int | None = None,
    device_override: str | None = None,
    run_id: str | None = None,
    config_sha256: str | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    seed = config.get("seed") if seed_override is None else seed_override
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("配置 seed 必须是非负整数")
    normalized_run_id, normalized_config_sha256 = normalize_run_identity(
        run_id,
        config_sha256,
    )
    device = (
        config.get("device")
        if device_override is None
        else device_override
    )
    image_size = positive_int(config, "image_size")
    batch_size = positive_int(config, "batch_size")
    epochs = positive_int(config, "epochs")
    learning_rate = positive_float(config, "learning_rate")
    train_split = safe_split_name(config.get("train_split"))
    evaluation_split = safe_split_name(config.get("evaluation_split"))
    torch = set_deterministic(seed, device)
    output = prepare_output_directory(output_dir)
    train_dataset = ImageFolderDataset(
        Path(data_root) / train_split,
        image_size,
    )
    evaluation_dataset = ImageFolderDataset(
        Path(data_root) / evaluation_split,
        image_size,
        train_dataset.class_names,
    )
    generator = torch.Generator(device="cpu").manual_seed(seed)
    loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        generator=generator,
        num_workers=0,
    )
    evaluation_loader = torch.utils.data.DataLoader(
        evaluation_dataset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=0,
    )
    model = build_model(len(train_dataset.class_names)).to(device=device)
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    loss_function = torch.nn.CrossEntropyLoss().to(device=device)
    model.train()
    optimizer_steps = 0
    for _ in range(epochs):
        for images, labels, _ in loader:
            images = images.to(device=device)
            labels = labels.to(device=device)
            optimizer.zero_grad(set_to_none=True)
            loss = loss_function(model(images), labels)
            if not torch.isfinite(loss):
                raise ValueError("训练 loss 变成了非有限数字")
            loss.backward()
            optimizer.step()
            optimizer_steps += 1
    checkpoint_path = output / "checkpoint.pt"
    save_checkpoint(
        checkpoint_path,
        model,
        train_dataset.class_names,
        image_size,
        seed,
        optimizer_steps,
    )
    checkpoint = load_checkpoint(checkpoint_path)
    reloaded_model = build_model(len(checkpoint["class_names"])).to(
        device=device
    )
    reloaded_model.load_state_dict(
        checkpoint["model_state_dict"],
        strict=True,
    )
    reloaded_model.eval()
    logits, targets, inputs = _collect_logits(
        reloaded_model,
        evaluation_loader,
        device,
    )
    metrics = classification_metrics(logits, targets)
    probabilities = torch.softmax(logits, dim=1)
    scores, indices = probabilities.max(dim=1)
    write_json(
        output / "metrics.json",
        {"schema": "pack-cls.metrics-output.v1", "metrics": metrics},
    )
    write_json(
        output / "predictions.json",
        {
            "schema": "pack-cls.predictions.v1",
            "predictions": [
                {
                    "input": input_name,
                    "class_index": int(class_index),
                    "class_name": train_dataset.class_names[class_index],
                    "score": float(score),
                }
                for input_name, class_index, score in zip(
                    inputs,
                    indices.detach().to(device="cpu").tolist(),
                    scores.detach().to(device="cpu").tolist(),
                    strict=True,
                )
            ],
        },
    )
    run = {
        "schema": "pack-cls.run.v1",
        "phase": "train",
        "mode": "local_imagefolder",
        "run_kind": "local_dataset_baseline",
        "paper_eligible": False,
        "run_id": normalized_run_id,
        "config_sha256": normalized_config_sha256,
        "seed": seed,
        "device": device,
        "class_names": train_dataset.class_names,
        "training_sample_count": len(train_dataset),
        "evaluation_split": evaluation_split,
        "sample_count": len(evaluation_dataset),
        "checkpoint": checkpoint_path.name,
        "checkpoint_reloaded": True,
        "optimizer_steps": optimizer_steps,
    }
    write_json(output / "run.json", run)
    return run


def main() -> int:
    parser = argparse.ArgumentParser(description="训练本地图像分类基线")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--device", choices=("cpu", "cuda"))
    parser.add_argument("--run-id")
    parser.add_argument("--config-sha256")
    arguments = parser.parse_args()
    run_training(
        arguments.config,
        arguments.data_root,
        arguments.output_dir,
        seed_override=arguments.seed,
        device_override=arguments.device,
        run_id=arguments.run_id,
        config_sha256=arguments.config_sha256,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
