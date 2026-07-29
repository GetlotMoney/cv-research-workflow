from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runtime import run_inference


def main() -> int:
    parser = argparse.ArgumentParser(description="PACK-DET 推理入口")
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument(
        "--mode",
        required=True,
        choices=("synthetic_debug", "local_coco"),
    )
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    arguments = parser.parse_args()
    result = run_inference(
        arguments.checkpoint,
        arguments.output_dir,
        mode=arguments.mode,
        data_root=arguments.data_root,
        annotation_file=arguments.annotations,
        device=arguments.device,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
