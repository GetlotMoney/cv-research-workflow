from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .contract import (
    load_checkpoint,
    prepare_output_directory,
    require_runtime,
    safe_split_name,
    set_deterministic,
    write_json,
)
from .data import ImageFolderDataset
from .metrics import classification_metrics
from .model import build_model


def run_evaluation(
    checkpoint_path: Path,
    data_root: Path,
    output_dir: Path,
    split: str,
    *,
    device: str = "cpu",
) -> dict[str, Any]:
    selected_split = safe_split_name(split)
    checkpoint = load_checkpoint(checkpoint_path)
    torch = set_deterministic(checkpoint["seed"], device)
    output = prepare_output_directory(output_dir)
    dataset = ImageFolderDataset(
        Path(data_root) / selected_split,
        checkpoint["image_size"],
        checkpoint["class_names"],
    )
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=min(32, len(dataset)),
        shuffle=False,
        num_workers=0,
    )
    model = build_model(len(checkpoint["class_names"])).to(device=device)
    model.load_state_dict(checkpoint["model_state_dict"], strict=True)
    model.eval()
    all_logits = []
    all_targets = []
    with torch.no_grad():
        for images, targets, _ in loader:
            images = images.to(device=device)
            targets = targets.to(device=device)
            all_logits.append(model(images))
            all_targets.append(targets)
    metrics = classification_metrics(
        torch.cat(all_logits),
        torch.cat(all_targets),
    )
    write_json(
        output / "metrics.json",
        {"schema": "pack-cls.metrics-output.v1", "metrics": metrics},
    )
    run = {
        "schema": "pack-cls.run.v1",
        "phase": "evaluate",
        "run_kind": "local_dataset_baseline",
        "paper_eligible": False,
        "seed": checkpoint["seed"],
        "device": device,
        "split": selected_split,
        "sample_count": len(dataset),
        "checkpoint_reloaded": True,
    }
    write_json(output / "run.json", run)
    return run


def main() -> int:
    parser = argparse.ArgumentParser(description="评估本地图像分类 checkpoint")
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--split", default="val")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    arguments = parser.parse_args()
    run_evaluation(
        arguments.checkpoint,
        arguments.data_root,
        arguments.output_dir,
        arguments.split,
        device=arguments.device,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
