from __future__ import annotations

import hashlib
import io
import json
import math
import os
import random
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


CONFIG_SCHEMA = "pack-gzsl.config.v1"
DATASET_IDENTITY_SCHEMA = "cv-experiment-workflow.dataset-identity.v1"
RUN_ID = re.compile(r"RUN-[0-9]{4}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
REPARSE_POINT = 0x400
MAX_FILE_SIZE = 64 * 1024 * 1024
MAX_TOTAL_SIZE = 128 * 1024 * 1024
MAX_FILES = 4096


def require_runtime() -> tuple[Any, Any]:
    try:
        import numpy
        import torch
    except ImportError as error:
        raise RuntimeError(
            "GZSL 模板缺少 torch 或 numpy；"
            "请使用项目固定的 Conda 环境 dvsr_gpu。"
        ) from error
    return torch, numpy


def require_device(device: object, *, synthetic: bool = False) -> Any:
    if device != "cuda":
        raise ValueError("本 GZSL 工作流固定使用 cuda，不支持 CPU fallback")
    torch, _ = require_runtime()
    if not torch.cuda.is_available():
        raise RuntimeError("请求了 CUDA，但当前 PyTorch/CUDA 运行环境不可用")
    return torch.device("cuda")


def set_deterministic(
    seed: int,
    device: str = "cuda",
    *,
    synthetic: bool = False,
) -> Any:
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed 必须是非负整数")
    torch, numpy = require_runtime()
    require_device(device, synthetic=synthetic)
    random.seed(seed)
    numpy.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    if hasattr(torch.backends, "cudnn"):
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
    torch.use_deterministic_algorithms(True)
    torch.set_num_threads(1)
    return torch


def strict_json_bytes(payload: object) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("JSON 只能包含有限可序列化值") from error


def canonical_sha256(payload: object) -> str:
    return hashlib.sha256(strict_json_bytes(payload)).hexdigest()


def validate_source_uri(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or "\\" in value:
        raise ValueError("source_uri 必须是规范 HTTPS URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("source_uri 不能是 file/local 路径")
    return value


def make_dataset_identity(
    *,
    dataset_id: object,
    version: object,
    source_uri: object,
    manifest_sha256: object,
) -> dict[str, Any]:
    if not isinstance(dataset_id, str) or SAFE_TOKEN.fullmatch(dataset_id) is None:
        raise ValueError("dataset_id 无效")
    if not isinstance(version, str) or SAFE_TOKEN.fullmatch(version) is None:
        raise ValueError("version 无效")
    if not isinstance(manifest_sha256, str) or SHA256.fullmatch(manifest_sha256) is None:
        raise ValueError("manifest_sha256 格式无效")
    return {
        "schema": DATASET_IDENTITY_SCHEMA,
        "dataset_id": dataset_id,
        "version": version,
        "source_uri": validate_source_uri(source_uri),
        "manifest_sha256": manifest_sha256,
        "split": {
            "train": "train_indices",
            "seen_test": "test_seen_indices",
            "unseen_test": "test_unseen_indices",
        },
    }


def is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = Path(path).lstat()
    except OSError:
        return True
    return Path(path).is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT
    )


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _check_chain(path: Path, label: str) -> None:
    candidate = _absolute(path)
    for current in (candidate, *candidate.parents):
        if os.path.lexists(current) and is_link_or_reparse(current):
            raise ValueError(f"{label} 及父路径不能含 link/junction/reparse")


def require_directory(path: Path, label: str) -> Path:
    candidate = _absolute(path)
    _check_chain(candidate, label)
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise ValueError(f"{label} 不存在") from error
    if not stat.S_ISDIR(metadata.st_mode) or is_link_or_reparse(candidate):
        raise ValueError(f"{label} 必须是普通目录")
    return candidate


def require_file(path: Path, label: str, *, root: Path | None = None) -> Path:
    candidate = _absolute(path)
    _check_chain(candidate, label)
    if root is not None:
        safe_root = require_directory(root, f"{label} 根")
        try:
            candidate.relative_to(safe_root)
        except ValueError as error:
            raise ValueError(f"{label} 逃逸根目录") from error
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise ValueError(f"{label} 不存在") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or is_link_or_reparse(candidate)
        or getattr(metadata, "st_nlink", 1) != 1
    ):
        raise ValueError(f"{label} 必须是非链接普通文件")
    return candidate


def _stable(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def secure_read(
    path: Path,
    label: str,
    *,
    root: Path | None = None,
    max_size: int = MAX_FILE_SIZE,
) -> bytes:
    source = require_file(path, label, root=root)
    before = source.lstat()
    if before.st_size > max_size:
        raise ValueError(f"{label} 超过大小上限")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(source, flags)
    try:
        opened_before = os.fstat(descriptor)
        if _stable(opened_before) != _stable(before):
            raise ValueError(f"{label} 在 lstat→open 之间被替换")
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_size + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > max_size:
                raise ValueError(f"{label} 读取时超过大小上限")
            chunks.append(chunk)
        opened_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = source.lstat()
    if (
        _stable(opened_before) != _stable(before)
        or _stable(opened_after) != _stable(before)
        or _stable(after) != _stable(before)
        or opened_before.st_ctime_ns != opened_after.st_ctime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or is_link_or_reparse(source)
    ):
        raise ValueError(f"{label} 在读取过程中被替换或修改")
    return b"".join(chunks)


def read_json_snapshot(
    path: Path,
    label: str,
    *,
    root: Path | None = None,
) -> tuple[dict[str, Any], bytes, tuple[int, int, int, int, int]]:
    try:
        source = require_file(path, label, root=root)
        before = source.lstat()
        content = secure_read(source, label, root=root)
        after = source.lstat()
        if (
            _file_identity(before) != _file_identity(after)
            or after.st_size != len(content)
            or is_link_or_reparse(source)
        ):
            raise ValueError(f"{label} 在读取后被修改或替换")
        payload = json.loads(
            content.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"非有限值：{value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} 不是严格 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} 必须是 object")
    return payload, content, _file_identity(after)


def read_json_with_size(
    path: Path,
    label: str,
    *,
    root: Path | None = None,
) -> tuple[dict[str, Any], int]:
    payload, content, _ = read_json_snapshot(path, label, root=root)
    return payload, len(content)


def read_json(path: Path, label: str, *, root: Path | None = None) -> dict[str, Any]:
    return read_json_with_size(path, label, root=root)[0]


def load_config(path: Path) -> dict[str, Any]:
    payload = read_json(path, "配置")
    if payload.get("schema") != CONFIG_SCHEMA:
        raise ValueError(f"配置 schema 必须是 {CONFIG_SCHEMA}")
    return payload


def positive_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{key} 必须是正整数")
    return value


def positive_float(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise ValueError(f"{key} 必须是有限正数")
    return float(value)


def prepare_output(path: Path) -> Path:
    target = _absolute(path)
    _check_chain(target.parent, "输出父路径")
    if os.path.lexists(target):
        raise FileExistsError(f"输出目录已存在，拒绝覆盖：{target}")
    target.mkdir(parents=True, exist_ok=False)
    return require_directory(target, "新建输出目录")


def write_bytes(path: Path, content: bytes) -> None:
    target = _absolute(path)
    require_directory(target.parent, "输出父目录")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path: Path, payload: object) -> None:
    write_bytes(path, strict_json_bytes(payload))


def normalize_run(run_id: object, config_sha256: object) -> tuple[str, str]:
    if (
        not isinstance(run_id, str)
        or RUN_ID.fullmatch(run_id) is None
        or not isinstance(config_sha256, str)
        or SHA256.fullmatch(config_sha256) is None
    ):
        raise ValueError("run_id/config_sha256 格式无效")
    return run_id, config_sha256


def save_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    torch, _ = require_runtime()
    target = _absolute(path)
    require_directory(target.parent, "checkpoint 父目录")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(target, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        torch.save(payload, handle)
        handle.flush()
        os.fsync(handle.fileno())


def load_checkpoint(path: Path) -> dict[str, Any]:
    torch, _ = require_runtime()
    content = secure_read(path, "checkpoint")
    try:
        payload = torch.load(io.BytesIO(content), map_location="cpu", weights_only=True)
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        raise ValueError("checkpoint 无法按 state_dict 安全读取") from error
    fields = {
        "schema",
        "architecture",
        "model_state_dict",
        "visual_dim",
        "attribute_dim",
        "class_ids",
        "seen_class_ids",
        "unseen_class_ids",
        "attributes",
        "seed",
        "optimizer_steps",
        "run_id",
        "config_sha256",
        "dataset_identity",
        "run_kind",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != fields
        or payload.get("schema") != "pack-gzsl.checkpoint.v1"
        or payload.get("architecture") != "LinearVisualToAttribute-v1"
        or not isinstance(payload.get("model_state_dict"), dict)
        or not payload["model_state_dict"]
        or any(not isinstance(value, torch.Tensor) for value in payload["model_state_dict"].values())
        or any(not bool(torch.isfinite(value).all()) for value in payload["model_state_dict"].values())
        or any(
            not isinstance(payload.get(key), torch.Tensor)
            for key in ("class_ids", "seen_class_ids", "unseen_class_ids", "attributes")
        )
        or any(
            not bool(torch.isfinite(payload[key]).all())
            for key in ("attributes",)
        )
    ):
        raise ValueError("checkpoint 字段、架构或 tensor 无效")
    for key in ("visual_dim", "attribute_dim", "optimizer_steps"):
        if isinstance(payload.get(key), bool) or not isinstance(payload.get(key), int) or payload[key] <= 0:
            raise ValueError(f"checkpoint {key} 无效")
    if isinstance(payload.get("seed"), bool) or not isinstance(payload.get("seed"), int) or payload["seed"] < 0:
        raise ValueError("checkpoint seed 无效")
    normalize_run(payload.get("run_id"), payload.get("config_sha256"))
    if payload["attributes"].shape != (
        payload["class_ids"].numel(),
        payload["attribute_dim"],
    ):
        raise ValueError("checkpoint attributes/class_ids 形状无效")
    if payload.get("run_kind") == "synthetic_debug_only":
        if payload.get("dataset_identity") != {"kind": "synthetic_debug_only"}:
            raise ValueError("合成 checkpoint 身份无效")
    elif payload.get("run_kind") == "local_dataset_baseline":
        identity = payload.get("dataset_identity")
        if (
            not isinstance(identity, dict)
            or identity
            != make_dataset_identity(
                dataset_id=identity.get("dataset_id"),
                version=identity.get("version"),
                source_uri=identity.get("source_uri"),
                manifest_sha256=identity.get("manifest_sha256"),
            )
        ):
            raise ValueError("正式 checkpoint 身份无效")
    else:
        raise ValueError("checkpoint run_kind 无效")
    return payload


def write_artifacts(output: Path) -> None:
    root = require_directory(output, "运行输出")
    records = []
    total = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path == root / "artifacts.json":
            continue
        if path.is_dir() and not path.is_symlink():
            require_directory(path, "产物子目录")
            continue
        content = secure_read(path, "产物", root=root)
        total += len(content)
        if total > MAX_TOTAL_SIZE:
            raise ValueError("运行产物总体积超过上限")
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    payload = {"schema": "pack-gzsl.artifacts.v1", "artifacts": records}
    manifest_content = strict_json_bytes(payload)
    if total + len(manifest_content) > MAX_TOTAL_SIZE:
        raise ValueError("运行产物总体积超过上限")
    write_bytes(root / "artifacts.json", manifest_content)
    validate_artifacts(root)


def _validate_artifact_tree(
    root: Path,
    records: list[object],
    manifest_size: int,
) -> set[str]:
    listed: set[str] = set()
    listed_total = manifest_size
    for item in records:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "size", "sha256"}
            or not isinstance(item.get("path"), str)
            or "\\" in item["path"]
            or item["path"].startswith("/")
            or ".." in Path(item["path"]).parts
            or item["path"] in listed
            or isinstance(item.get("size"), bool)
            or not isinstance(item.get("size"), int)
            or item["size"] < 0
            or not isinstance(item.get("sha256"), str)
            or SHA256.fullmatch(item["sha256"]) is None
        ):
            raise ValueError("artifacts.json 条目无效")
        listed.add(item["path"])
        content = secure_read(root / Path(*item["path"].split("/")), "登记产物", root=root)
        listed_total += len(content)
        if listed_total > MAX_TOTAL_SIZE:
            raise ValueError("运行产物总体积超过上限")
        if len(content) != item["size"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError(f"产物 SHA-256/大小不一致：{item['path']}")
    actual = set()
    actual_total = manifest_size
    for path in root.rglob("*"):
        if path == root / "artifacts.json":
            continue
        if path.is_dir() and not path.is_symlink():
            require_directory(path, "产物子目录")
            continue
        content = secure_read(path, "产物", root=root)
        actual_total += len(content)
        if actual_total > MAX_TOTAL_SIZE:
            raise ValueError("运行产物总体积超过上限")
        actual.add(path.relative_to(root).as_posix())
    if actual != listed:
        raise ValueError("运行目录含未登记、缺失或替换的产物")
    return listed


def validate_artifacts(output: Path) -> list[str]:
    root = require_directory(output, "运行输出")
    manifest_path = root / "artifacts.json"
    payload, manifest_content, manifest_identity = read_json_snapshot(
        manifest_path,
        "artifacts.json",
        root=root,
    )
    if set(payload) != {"schema", "artifacts"} or payload.get("schema") != "pack-gzsl.artifacts.v1":
        raise ValueError("artifacts.json schema 无效")
    records = payload.get("artifacts")
    if not isinstance(records, list) or not records or len(records) > MAX_FILES:
        raise ValueError("artifacts.json 数量无效")
    _validate_artifact_tree(root, records, len(manifest_content))
    final_payload, final_content, final_identity = read_json_snapshot(
        manifest_path,
        "artifacts.json",
        root=root,
    )
    if (
        final_payload != payload
        or final_content != manifest_content
        or final_identity != manifest_identity
    ):
        raise ValueError("artifacts.json 在验收过程中发生变化")
    listed = _validate_artifact_tree(root, records, len(final_content))
    last_payload, last_content, last_identity = read_json_snapshot(
        manifest_path,
        "artifacts.json",
        root=root,
    )
    if (
        last_payload != payload
        or last_content != manifest_content
        or last_identity != manifest_identity
    ):
        raise ValueError("artifacts.json 在最终目录重扫过程中发生变化")
    return sorted(listed)
