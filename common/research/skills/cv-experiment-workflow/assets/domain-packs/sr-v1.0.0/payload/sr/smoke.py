from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .contract import canonical_sha256, load_config, positive_float, positive_int
from .runtime import run_synthetic


def run_synthetic_smoke(
    output_dir: Path,
    *,
    config_path: Path,
    seed_override: int | None = None,
    run_id: str | None = None,
    config_sha256: str | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    if set(config) != {
        "schema",
        "seed",
        "device",
        "scale",
        "sample_count",
        "lr_height",
        "lr_width",
        "epochs",
        "learning_rate",
    } or config.get("device") != "cpu" or config.get("scale") != 2:
        raise ValueError("SR smoke 配置字段/device/scale 无效")
    for key in ("sample_count", "lr_height", "lr_width", "epochs"):
        positive_int(config, key)
    positive_float(config, "learning_rate")
    seed = config["seed"] if seed_override is None else seed_override
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed 必须是非负整数")
    return run_synthetic(
        config=config,
        output_dir=output_dir,
        seed=seed,
        run_id="RUN-0001" if run_id is None else run_id,
        config_sha256=(
            canonical_sha256(config)
            if config_sha256 is None
            else config_sha256
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 PACK-SR 合成 CPU smoke")
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=Path("configs/smoke.json"))
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--config-sha256")
    arguments = parser.parse_args()
    if arguments.device != "cpu":
        raise SystemExit("PACK-SR 只支持 CPU")
    run_synthetic_smoke(
        arguments.work_dir,
        config_path=arguments.config,
        seed_override=arguments.seed,
        run_id=arguments.run_id,
        config_sha256=arguments.config_sha256,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
