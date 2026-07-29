from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .contract import (
    load_checkpoint,
    require_plain_directory,
    require_plain_file,
    set_deterministic,
    write_json,
)
from .data import IMAGE_EXTENSIONS, _scan_image_files, load_image_tensor
from .model import build_model


def _input_files(source: Path) -> list[Path]:
    selected = Path(source)
    if selected.is_file():
        require_plain_file(selected, "推理输入")
        if selected.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError(f"推理输入不是支持的图片：{selected}")
        return [selected]
    if selected.is_dir():
        require_plain_directory(selected, "推理输入目录")
        files = _scan_image_files(selected)
        if files:
            return files
    raise ValueError(f"推理输入没有可读图片：{source}")


def run_inference(
    checkpoint_path: Path,
    source: Path,
    output_path: Path,
    *,
    device: str = "cpu",
) -> dict[str, Any]:
    checkpoint = load_checkpoint(checkpoint_path)
    torch = set_deterministic(checkpoint["seed"], device)
    model = build_model(len(checkpoint["class_names"])).to(device=device)
    model.load_state_dict(checkpoint["model_state_dict"], strict=True)
    model.eval()
    files = _input_files(source)
    predictions = []
    for start in range(0, len(files), 32):
        selected_files = files[start : start + 32]
        batch = torch.stack(
            [
                load_image_tensor(path, checkpoint["image_size"])
                for path in selected_files
            ]
        ).to(device=device)
        with torch.no_grad():
            probabilities = torch.softmax(model(batch), dim=1)
        scores, indices = probabilities.max(dim=1)
        for path, class_index, score in zip(
            selected_files,
            indices.detach().to(device="cpu").tolist(),
            scores.detach().to(device="cpu").tolist(),
            strict=True,
        ):
            predictions.append({
                "input": str(path.resolve()),
                "class_index": class_index,
                "class_name": checkpoint["class_names"][class_index],
                "score": float(score),
            })
    payload = {
        "schema": "pack-cls.predictions.v1",
        "predictions": predictions,
    }
    write_json(Path(output_path), payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="运行本地图像分类推理")
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    arguments = parser.parse_args()
    run_inference(
        arguments.checkpoint,
        arguments.input,
        arguments.output,
        device=arguments.device,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
