from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runtime import run_training


def main() -> int:
    parser = argparse.ArgumentParser(description="PACK-INSTSEG 合成调试闭环")
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--device", default="cpu", choices=("cpu",))
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument("--run-id", default="STANDALONE")
    parser.add_argument("--config-sha256")
    arguments = parser.parse_args()
    result = run_training(
        Path("configs/smoke.json"),
        arguments.work_dir,
        mode="synthetic_debug",
        seed_override=arguments.seed,
        run_id=arguments.run_id,
        config_sha256=arguments.config_sha256,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
