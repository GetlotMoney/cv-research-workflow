from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runtime import run_training


def main() -> int:
    parser = argparse.ArgumentParser(description="PACK-INSTSEG 最小训练入口")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--mode",
        required=True,
        choices=("synthetic_debug", "local_coco"),
    )
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--run-id", default="STANDALONE")
    parser.add_argument("--config-sha256")
    parser.add_argument("--device", choices=("cpu", "cuda"))
    arguments = parser.parse_args()
    result = run_training(
        arguments.config,
        arguments.output_dir,
        mode=arguments.mode,
        data_root=arguments.data_root,
        annotation_file=arguments.annotations,
        seed_override=arguments.seed,
        run_id=arguments.run_id,
        config_sha256=arguments.config_sha256,
        device_override=arguments.device,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
