from __future__ import annotations

import argparse
from pathlib import Path

from .runtime import infer_existing


def run_inference(
    checkpoint_path: Path,
    data_root: Path,
    output_dir: Path,
    split: str,
    *,
    device: str = "cpu",
) -> int:
    return infer_existing(
        checkpoint_path,
        data_root,
        output_dir,
        split,
        device=device,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 PACK-SR 推理")
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--split", default="val")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    arguments = parser.parse_args()
    run_inference(
        arguments.checkpoint,
        arguments.data_root,
        arguments.output_dir,
        arguments.split,
        device=arguments.device,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
