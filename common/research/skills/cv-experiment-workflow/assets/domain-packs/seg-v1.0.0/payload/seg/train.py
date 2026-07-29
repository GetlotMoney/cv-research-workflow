from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .contract import (
    canonical_sha256,
    load_config,
    positive_float,
    positive_int,
    safe_split_name,
)
from .runtime import run_local_training


def run_training(
    config_path: Path,
    data_root: Path,
    output_dir: Path,
    *,
    dataset_id: str,
    version: str,
    source_uri: str,
    manifest_sha256: str,
    seed_override: int | None = None,
    device_override: str | None = None,
    run_id: str | None = None,
    config_sha256: str | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    expected = {
        "schema",
        "seed",
        "device",
        "image_size",
        "num_classes",
        "train_split",
        "evaluation_split",
        "epochs",
        "batch_size",
        "learning_rate",
    }
    if set(config) != expected or config.get("device") not in {"cpu", "cuda"}:
        raise ValueError("baseline 配置字段无效，且 device 只允许 cpu/cuda")
    if device_override is not None:
        if device_override not in {"cpu", "cuda"}:
            raise ValueError("device override 只允许 cpu/cuda")
        config = {**config, "device": device_override}
    for key in ("image_size", "num_classes", "epochs", "batch_size"):
        positive_int(config, key)
    positive_float(config, "learning_rate")
    safe_split_name(config["train_split"])
    safe_split_name(config["evaluation_split"])
    seed = config["seed"] if seed_override is None else seed_override
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed 必须是非负整数")
    return run_local_training(
        config=config,
        data_root=data_root,
        output_dir=output_dir,
        seed=seed,
        run_id="RUN-0001" if run_id is None else run_id,
        config_sha256=(
            canonical_sha256(config)
            if config_sha256 is None
            else config_sha256
        ),
        dataset_id=dataset_id,
        version=version,
        source_uri=source_uri,
        manifest_sha256=manifest_sha256,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="训练本地语义分割基线")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--source-uri", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--device", choices=("cpu", "cuda"))
    parser.add_argument("--run-id")
    parser.add_argument("--config-sha256")
    arguments = parser.parse_args()
    run_training(
        arguments.config,
        arguments.data_root,
        arguments.output_dir,
        dataset_id=arguments.dataset_id,
        version=arguments.dataset_version,
        source_uri=arguments.source_uri,
        manifest_sha256=arguments.manifest_sha256,
        seed_override=arguments.seed,
        device_override=arguments.device,
        run_id=arguments.run_id,
        config_sha256=arguments.config_sha256,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
