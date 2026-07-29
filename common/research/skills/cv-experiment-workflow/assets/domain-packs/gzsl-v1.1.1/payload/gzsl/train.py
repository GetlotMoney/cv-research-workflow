from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .contract import canonical_sha256, load_config, positive_float, positive_int
from .runtime import run_local


def run_training(
    config_path: Path,
    data_path: Path,
    output_dir: Path,
    *,
    dataset_id: str,
    version: str,
    source_uri: str,
    manifest_sha256: str,
    seed_override: int | None = None,
    run_id: str | None = None,
    config_sha256: str | None = None,
    device_override: str | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    if set(config) != {
        "schema",
        "seed",
        "device",
        "epochs",
        "learning_rate",
    } or config.get("device") != "cuda":
        raise ValueError("GZSL baseline 配置字段/device 无效")
    positive_int(config, "epochs")
    positive_float(config, "learning_rate")
    seed = config["seed"] if seed_override is None else seed_override
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed 必须是非负整数")
    device = config["device"] if device_override is None else device_override
    if device != "cuda":
        raise ValueError("本 GZSL 工作流固定使用 cuda，不支持 CPU fallback")
    effective_config = dict(config)
    effective_config["device"] = device
    return run_local(
        config=effective_config,
        data_path=data_path,
        output_dir=output_dir,
        seed=seed,
        run_id="RUN-0001" if run_id is None else run_id,
        config_sha256=(
            canonical_sha256(effective_config)
            if config_sha256 is None
            else config_sha256
        ),
        dataset_id=dataset_id,
        version=version,
        source_uri=source_uri,
        manifest_sha256=manifest_sha256,
        device=device,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="训练本地 GZSL 线性基线")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--data-path", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--source-uri", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--config-sha256")
    parser.add_argument("--device", choices=("cuda",))
    arguments = parser.parse_args()
    run_training(
        arguments.config,
        arguments.data_path,
        arguments.output_dir,
        dataset_id=arguments.dataset_id,
        version=arguments.dataset_version,
        source_uri=arguments.source_uri,
        manifest_sha256=arguments.manifest_sha256,
        seed_override=arguments.seed,
        run_id=arguments.run_id,
        config_sha256=arguments.config_sha256,
        device_override=arguments.device,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
