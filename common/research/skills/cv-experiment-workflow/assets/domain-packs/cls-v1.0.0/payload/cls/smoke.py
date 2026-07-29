from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .contract import (
    load_config,
    load_checkpoint,
    positive_float,
    positive_int,
    prepare_output_directory,
    normalize_run_identity,
    save_checkpoint,
    set_deterministic,
    write_json,
)
from .data import synthetic_tensors
from .metrics import classification_metrics
from .model import build_model


def run_synthetic_smoke(
    work_dir: Path,
    *,
    config_path: Path,
    device: str,
    seed_override: int | None = None,
    run_id: str | None = None,
    config_sha256: str | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    if device != "cpu":
        raise ValueError("分类 synthetic_smoke 严格只允许 device=cpu")
    if config.get("device") != device:
        raise ValueError("命令 device 必须与 smoke 配置一致")
    seed = config.get("seed") if seed_override is None else seed_override
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("配置 seed 必须是非负整数")
    normalized_run_id, normalized_config_sha256 = normalize_run_identity(
        run_id,
        config_sha256,
    )
    image_size = positive_int(config, "image_size")
    num_classes = positive_int(config, "num_classes")
    samples_per_class = positive_int(config, "samples_per_class")
    epochs = positive_int(config, "epochs")
    batch_size = positive_int(config, "batch_size")
    learning_rate = positive_float(config, "learning_rate")
    torch = set_deterministic(seed, device)
    output = prepare_output_directory(work_dir)
    images, targets, class_names = synthetic_tensors(
        num_classes=num_classes,
        samples_per_class=samples_per_class,
        image_size=image_size,
        seed=seed,
    )
    dataset = torch.utils.data.TensorDataset(images, targets)
    generator = torch.Generator(device="cpu").manual_seed(seed)
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=True,
        generator=generator,
        num_workers=0,
    )
    model = build_model(num_classes)
    optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate)
    loss_function = torch.nn.CrossEntropyLoss()
    model.train()
    optimizer_steps = 0
    for _ in range(epochs):
        for batch_images, batch_targets in loader:
            optimizer.zero_grad(set_to_none=True)
            loss = loss_function(model(batch_images), batch_targets)
            if not torch.isfinite(loss):
                raise ValueError("合成 smoke 的 loss 变成了非有限数字")
            loss.backward()
            optimizer.step()
            optimizer_steps += 1
    save_checkpoint(
        output / "checkpoint.pt",
        model,
        class_names,
        image_size,
        seed,
        optimizer_steps,
    )
    checkpoint = load_checkpoint(output / "checkpoint.pt")
    reloaded_model = build_model(len(checkpoint["class_names"]))
    reloaded_model.load_state_dict(
        checkpoint["model_state_dict"],
        strict=True,
    )
    reloaded_model.eval()
    with torch.no_grad():
        logits = reloaded_model(images)
        probabilities = torch.softmax(logits, dim=1)
    metrics = classification_metrics(logits, targets)
    write_json(
        output / "metrics.json",
        {"schema": "pack-cls.metrics-output.v1", "metrics": metrics},
    )
    scores, indices = probabilities.max(dim=1)
    write_json(
        output / "predictions.json",
        {
            "schema": "pack-cls.predictions.v1",
            "predictions": [
                {
                    "input": f"synthetic-{index:04d}",
                    "class_index": int(class_index),
                    "class_name": class_names[class_index],
                    "score": float(score),
                }
                for index, (class_index, score) in enumerate(
                    zip(indices.tolist(), scores.tolist(), strict=True)
                )
            ],
        },
    )
    run = {
        "schema": "pack-cls.run.v1",
        "phase": "synthetic_smoke",
        "mode": "synthetic_smoke",
        "run_kind": "synthetic_debug_only",
        "paper_eligible": False,
        "run_id": normalized_run_id,
        "config_sha256": normalized_config_sha256,
        "seed": seed,
        "device": "cpu",
        "sample_count": len(dataset),
        "class_names": class_names,
        "checkpoint": "checkpoint.pt",
        "checkpoint_reloaded": True,
        "optimizer_steps": optimizer_steps,
    }
    write_json(output / "run.json", run)
    return run


def main() -> int:
    parser = argparse.ArgumentParser(description="运行分类模板的合成 CPU smoke")
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--config-sha256")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/smoke.json"),
    )
    arguments = parser.parse_args()
    run_synthetic_smoke(
        arguments.work_dir,
        config_path=arguments.config,
        device=arguments.device,
        seed_override=arguments.seed,
        run_id=arguments.run_id,
        config_sha256=arguments.config_sha256,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
