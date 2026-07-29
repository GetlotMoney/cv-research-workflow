from __future__ import annotations

import argparse
from pathlib import Path

from .runtime import infer_existing


def run_inference(
    checkpoint_path: Path,
    data_path: Path,
    output_dir: Path,
    *,
    device: str = "cuda",
) -> int:
    return infer_existing(
        checkpoint_path,
        data_path,
        output_dir,
        device=device,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 PACK-GZSL 推理")
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--data-path", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--device", choices=("cuda",), default="cuda")
    arguments = parser.parse_args()
    run_inference(
        arguments.checkpoint,
        arguments.data_path,
        arguments.output_dir,
        device=arguments.device,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
