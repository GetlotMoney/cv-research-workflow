from __future__ import annotations

import json
import math
import os
import random
import re
import stat
from pathlib import Path
from typing import Any


CONFIG_SCHEMA = "pack-cls.config.v1"
METRIC_NAMES = ("top1_accuracy", "top5_accuracy", "macro_f1")
RUN_ID = re.compile(r"RUN-[0-9]{4}")
SHA256 = re.compile(r"[0-9a-f]{64}")
REPARSE_POINT = 0x400


def require_runtime() -> tuple[Any, Any, Any]:
    try:
        import numpy
        import torch
        from PIL import Image
    except ImportError as error:
        raise RuntimeError(
            "分类模板缺少运行依赖。请按 "
            "requirements/windows-cpu.lock.txt 安装 torch、numpy 和 Pillow。"
        ) from error
    return torch, numpy, Image


def _require_cuda_ready(torch: Any) -> None:
    os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
    if not torch.cuda.is_available():
        raise RuntimeError("请求了 CUDA/GPU，但当前 PyTorch 环境检测不到可用 CUDA")
    try:
        probe = torch.empty(1, device="cuda")
        probe.add_(1.0)
        torch.cuda.synchronize()
    except Exception as error:
        raise RuntimeError(
            "CUDA/GPU 小算子探测失败；驱动、PyTorch CUDA 构建或显卡架构不兼容"
        ) from error


def set_deterministic(seed: int, device: str) -> Any:
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed 必须是非负整数")
    torch, numpy, _ = require_runtime()
    if device not in {"cpu", "cuda"}:
        raise ValueError("device 只允许 cpu/cuda")
    if device == "cuda":
        _require_cuda_ready(torch)
    random.seed(seed)
    numpy.random.seed(seed)
    torch.manual_seed(seed)
    if device == "cuda":
        torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
    torch.use_deterministic_algorithms(True)
    torch.set_num_threads(1)
    return torch


def load_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            Path(path).read_text(encoding="utf-8"),
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"无法读取严格 JSON 配置：{path}；{error}") from error
    if not isinstance(payload, dict) or payload.get("schema") != CONFIG_SCHEMA:
        raise ValueError(f"配置 schema 必须是 {CONFIG_SCHEMA}")
    return payload


def positive_int(config: dict[str, Any], key: str) -> int:
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"配置 {key} 必须是正整数")
    return value


def positive_float(config: dict[str, Any], key: str) -> float:
    value = config.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise ValueError(f"配置 {key} 必须是有限正数")
    return float(value)


def write_json(path: Path, payload: object) -> None:
    try:
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        )
    except (TypeError, ValueError) as error:
        raise ValueError("输出必须是只含有限数字的 JSON") from error
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as error:
        raise FileExistsError(f"输出文件已存在，拒绝覆盖：{target}") from error
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def prepare_output_directory(path: Path) -> Path:
    target = Path(os.path.abspath(os.fspath(path)))
    for parent in target.parents:
        if os.path.lexists(parent) and is_link_or_reparse(parent):
            raise ValueError(
                f"输出目录父路径不能包含 link/reparse：{parent}"
            )
    if os.path.lexists(target):
        raise FileExistsError(f"输出目录已存在，拒绝覆盖：{target}")
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise FileExistsError(f"输出目录已存在，拒绝覆盖：{target}") from error
    return target


def safe_split_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value in {".", ".."}
        or Path(value).is_absolute()
        or Path(value).drive
        or "/" in value
        or "\\" in value
        or ":" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("split 必须是单一、安全、规范的目录名")
    return value


def is_link_or_reparse(path: Path) -> bool:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError:
        return True
    return candidate.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT
    )


def require_plain_file(path: Path, label: str) -> Path:
    candidate = _plain_lexical_path(Path(path), label)
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise ValueError(f"{label} 不存在：{candidate}") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or is_link_or_reparse(candidate)
    ):
        raise ValueError(f"{label} 必须是普通文件，不能是 link/reparse：{candidate}")
    return candidate


def require_plain_directory(path: Path, label: str) -> Path:
    candidate = _plain_lexical_path(Path(path), label)
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise ValueError(f"{label} 不存在：{candidate}") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or is_link_or_reparse(candidate)
    ):
        raise ValueError(f"{label} 必须是普通目录，不能是 link/reparse：{candidate}")
    return candidate


def _plain_lexical_path(path: Path, label: str) -> Path:
    candidate = Path(os.path.abspath(os.fspath(path)))
    for current in (candidate, *candidate.parents):
        if os.path.lexists(current) and is_link_or_reparse(current):
            raise ValueError(
                f"{label} 及其父路径不能包含 link/reparse：{current}"
            )
    return candidate


def normalize_run_identity(
    run_id: object,
    config_sha256: object,
) -> tuple[str | None, str | None]:
    if run_id is None and config_sha256 is None:
        return None, None
    if (
        not isinstance(run_id, str)
        or RUN_ID.fullmatch(run_id) is None
        or not isinstance(config_sha256, str)
        or SHA256.fullmatch(config_sha256) is None
    ):
        raise ValueError(
            "run_id 和 config_sha256 必须同时提供，且分别是 RUN-0001 与 64 位 SHA-256"
        )
    return run_id, config_sha256


def save_checkpoint(
    path: Path,
    model: Any,
    class_names: list[str],
    image_size: int,
    seed: int,
    optimizer_steps: int,
) -> None:
    torch, _, _ = require_runtime()
    if (
        isinstance(optimizer_steps, bool)
        or not isinstance(optimizer_steps, int)
        or optimizer_steps <= 0
    ):
        raise ValueError("optimizer_steps 必须是正整数")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"checkpoint 已存在，拒绝覆盖：{target}")
    torch.save(
        {
            "model_state_dict": model.state_dict(),
            "class_names": list(class_names),
            "image_size": image_size,
            "seed": seed,
            "optimizer_steps": optimizer_steps,
        },
        target,
    )


def load_checkpoint(path: Path) -> dict[str, Any]:
    torch, _, _ = require_runtime()
    source = require_plain_file(Path(path), "checkpoint")
    try:
        payload = torch.load(
            source,
            map_location="cpu",
            weights_only=True,
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError(f"无法读取 state_dict checkpoint：{path}") from error
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "model_state_dict",
            "class_names",
            "image_size",
            "seed",
            "optimizer_steps",
        }
        or not isinstance(payload["model_state_dict"], dict)
        or not payload["model_state_dict"]
        or not isinstance(payload["class_names"], list)
        or not payload["class_names"]
        or not all(
            isinstance(item, str) and item
            for item in payload["class_names"]
        )
        or isinstance(payload["image_size"], bool)
        or not isinstance(payload["image_size"], int)
        or payload["image_size"] <= 0
        or isinstance(payload["seed"], bool)
        or not isinstance(payload["seed"], int)
        or isinstance(payload["optimizer_steps"], bool)
        or not isinstance(payload["optimizer_steps"], int)
        or payload["optimizer_steps"] <= 0
    ):
        raise ValueError("checkpoint 字段无效")
    return payload


def _reject_constant(value: str) -> None:
    raise ValueError(f"配置含非有限数字：{value}")
