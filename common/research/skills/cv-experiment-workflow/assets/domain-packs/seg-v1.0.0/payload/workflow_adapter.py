from __future__ import annotations

import hashlib as _hashlib
import importlib.util as _importlib_util
import io as _io
import json as _json
import math as _math
import os as _os
import re as _re
import stat as _stat
import subprocess as _subprocess
import sys as _sys
import types as _types
import uuid as _uuid
from pathlib import Path as _Path
from typing import Any as _Any
from urllib.parse import urlsplit as _urlsplit


_RUN_ID = _re.compile(r"RUN-[0-9]{4}")
_SHA256 = _re.compile(r"[0-9a-f]{64}")
_TOKEN = _re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MODES = {"synthetic_smoke", "local_segmentation"}
_FROZEN_FIELDS = {"code", "config", "seed", "data", "environment"}
_IDENTITY_FIELDS = {
    "schema",
    "dataset_id",
    "version",
    "source_uri",
    "manifest_sha256",
    "split",
}
_REPARSE_POINT = 0x400
_BOUND_CODE_SCHEMA = "cv-experiment-workflow.bound-code.v2"
_CENTRAL_DATASET_SPLIT = "train+val"
_FORMAL_METRIC_DEFINITION = {
    "mean_iou": (
        "Mean intersection over union computed from one whole-dataset "
        "confusion matrix; label 255 is ignored and classes with zero union "
        "are omitted; range 0 to 1, higher is better."
    ),
}
_MAX_FILE_SIZE = 16 * 1024 * 1024
_MAX_TOTAL_SIZE = 64 * 1024 * 1024
_MAX_FILES = 4096


def _is_link_or_reparse(path: _Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _require_directory(path: _Path, label: str) -> _Path:
    candidate = _Path(_os.path.abspath(_os.fspath(path)))
    for current in (candidate, *candidate.parents):
        if _os.path.lexists(current) and _is_link_or_reparse(current):
            raise ValueError(f"{label} 及父路径不能含 link/junction/reparse")
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise ValueError(f"{label} 不存在：{candidate}") from error
    if not _stat.S_ISDIR(metadata.st_mode) or _is_link_or_reparse(candidate):
        raise ValueError(f"{label} 必须是普通目录")
    return candidate


def _file_identity(metadata: _os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _opened_path_identity_matches(
    path_before: _os.stat_result,
    opened_before: _os.stat_result,
    opened_after: _os.stat_result,
    path_after: _os.stat_result,
) -> bool:
    def stable(metadata: _os.stat_result) -> tuple[int, int, int, int]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )

    expected = stable(path_before)
    return (
        stable(opened_before) == expected
        and stable(opened_after) == expected
        and stable(path_after) == expected
        and path_before.st_ctime_ns == path_after.st_ctime_ns
        and opened_before.st_ctime_ns == opened_after.st_ctime_ns
    )


def _safe_read(
    path: _Path,
    label: str,
    *,
    root: _Path | None = None,
) -> bytes:
    candidate = _Path(_os.path.abspath(_os.fspath(path)))
    for current in (candidate, *candidate.parents):
        if _os.path.lexists(current) and _is_link_or_reparse(current):
            raise ValueError(f"{label} 及父路径不能含 link/junction/reparse")
    if root is not None:
        safe_root = _require_directory(root, f"{label} 根")
        try:
            candidate.relative_to(safe_root)
        except ValueError as error:
            raise ValueError(f"{label} 逃逸运行根目录") from error
    try:
        before = candidate.lstat()
    except OSError as error:
        raise ValueError(f"{label} 不存在：{candidate}") from error
    if (
        not _stat.S_ISREG(before.st_mode)
        or _is_link_or_reparse(candidate)
        or getattr(before, "st_nlink", 1) != 1
        or before.st_size > _MAX_FILE_SIZE
    ):
        raise ValueError(f"{label} 必须是大小受限的非链接普通文件")
    flags = _os.O_RDONLY | getattr(_os, "O_BINARY", 0)
    if hasattr(_os, "O_NOFOLLOW"):
        flags |= _os.O_NOFOLLOW
    try:
        descriptor = _os.open(candidate, flags)
    except OSError as error:
        raise ValueError(f"{label} 无法安全打开") from error
    try:
        opened_before = _os.fstat(descriptor)
        if (
            opened_before.st_dev,
            opened_before.st_ino,
            opened_before.st_size,
            opened_before.st_mtime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise ValueError(f"{label} 在 lstat→open 之间被替换")
        chunks = []
        size = 0
        while True:
            chunk = _os.read(descriptor, min(1024 * 1024, _MAX_FILE_SIZE + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_FILE_SIZE:
                raise ValueError(f"{label} 超过大小上限")
            chunks.append(chunk)
        opened_after = _os.fstat(descriptor)
    finally:
        _os.close(descriptor)
    after = candidate.lstat()
    if (
        not _opened_path_identity_matches(
            before,
            opened_before,
            opened_after,
            after,
        )
        or _is_link_or_reparse(candidate)
    ):
        raise ValueError(f"{label} 在读取过程中被替换")
    return b"".join(chunks)


def _read_json_snapshot(
    path: _Path,
    label: str,
    *,
    root: _Path | None = None,
) -> tuple[dict[str, _Any], bytes, tuple[int, int, int, int, int]]:
    try:
        source = _Path(_os.path.abspath(_os.fspath(path)))
        before = source.lstat()
        content = _safe_read(source, label, root=root)
        after = source.lstat()
        if (
            _file_identity(before) != _file_identity(after)
            or after.st_size != len(content)
            or _is_link_or_reparse(source)
        ):
            raise ValueError(f"{label} 在读取后被修改或替换")
        payload = _json.loads(
            content.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"非有限值：{value}")
            ),
        )
    except (OSError, UnicodeError, _json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} 不是严格 UTF-8 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} 必须是 JSON object")
    return payload, content, _file_identity(after)


def _read_json_with_size(
    path: _Path,
    label: str,
    *,
    root: _Path | None = None,
) -> tuple[dict[str, _Any], int]:
    payload, content, _ = _read_json_snapshot(path, label, root=root)
    return payload, len(content)


def _read_json(
    path: _Path,
    label: str,
    *,
    root: _Path | None = None,
) -> dict[str, _Any]:
    return _read_json_with_size(path, label, root=root)[0]


def _canonical_sha256(payload: object) -> str:
    try:
        content = _json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("冻结配置必须是有限 JSON") from error
    return _hashlib.sha256(content).hexdigest()


def _https_uri(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or "\\" in value:
        raise ValueError("source_uri 必须是规范 HTTPS URL")
    parsed = _urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("source_uri 不能是 file/local 路径，只允许 HTTPS")
    return value


def _safe_text(value: object, label: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise ValueError(f"{label} 必须是安全稳定标识")
    return value


def _validate_data_identity(value: object) -> dict[str, _Any]:
    if not isinstance(value, dict) or set(value) != _IDENTITY_FIELDS:
        raise ValueError("data.identity 字段不符合 dataset-identity.v1")
    if value.get("schema") != "cv-experiment-workflow.dataset-identity.v1":
        raise ValueError("data.identity schema 无效")
    _safe_text(value.get("dataset_id"), "dataset_id")
    _safe_text(value.get("version"), "version")
    _https_uri(value.get("source_uri"))
    if (
        not isinstance(value.get("manifest_sha256"), str)
        or _SHA256.fullmatch(value["manifest_sha256"]) is None
    ):
        raise ValueError("manifest_sha256 必须是 64 位小写 SHA-256")
    if value.get("split") != {"train": "train", "evaluation": "val"}:
        raise ValueError("SEG 数据 split 必须冻结为 train/val")
    return dict(value)


def _domain_data_identity(config: dict[str, _Any]) -> dict[str, _Any]:
    return _validate_data_identity(
        {
            "schema": "cv-experiment-workflow.dataset-identity.v1",
            "dataset_id": config["dataset_id"],
            "version": config["version"],
            "source_uri": config["source_uri"],
            "manifest_sha256": config["manifest_sha256"],
            "split": {"train": "train", "evaluation": "val"},
        }
    )


def _central_data_identity(config: dict[str, _Any]) -> dict[str, _Any]:
    return {
        "schema": "cv-experiment-workflow.dataset-identity.v1",
        "dataset_id": config["dataset_id"],
        "version": config["version"],
        "source_uri": config["source_uri"],
        "manifest_sha256": f"sha256:{config['manifest_sha256']}",
        "split": _CENTRAL_DATASET_SPLIT,
    }


def _validated_central_data_identity(
    value: object,
    config: dict[str, _Any],
) -> dict[str, _Any]:
    expected = _central_data_identity(config)
    if not isinstance(value, dict) or value != expected:
        raise ValueError("SEG 中央 dataset_identity 与冻结配置不一致")
    return _domain_data_identity(config)


def _normalize_config(
    value: object,
    *,
    require_metric_definition: bool = True,
) -> dict[str, _Any]:
    if not isinstance(value, dict):
        raise ValueError("frozen.config 必须是 JSON object")
    config = dict(value)
    mode = config.get("mode")
    if mode not in _MODES:
        raise ValueError("config.mode 只允许 synthetic_smoke/local_segmentation")
    if mode == "local_segmentation":
        config.setdefault("device", "cuda")
    expected = (
        {"mode"}
        if mode == "synthetic_smoke"
        else {
            "mode",
            "data_root",
            "dataset_id",
            "version",
            "source_uri",
            "manifest_sha256",
            "device",
        }
    )
    if mode == "local_segmentation" and require_metric_definition:
        expected.add("metric_definition")
    if set(config) != expected:
        raise ValueError(f"{mode} config 字段必须精确为 {sorted(expected)}")
    _canonical_sha256(config)
    if mode == "local_segmentation":
        if config.get("device") not in {"cpu", "cuda"}:
            raise ValueError("local_segmentation device 只允许 cpu/cuda")
        data_root = config.get("data_root")
        if (
            not isinstance(data_root, str)
            or not data_root
            or data_root != data_root.strip()
            or any(ord(character) < 32 for character in data_root)
        ):
            raise ValueError("local_segmentation 必须明确 data_root")
        _safe_text(config.get("dataset_id"), "dataset_id")
        _safe_text(config.get("version"), "version")
        _https_uri(config.get("source_uri"))
        if (
            not isinstance(config.get("manifest_sha256"), str)
            or _SHA256.fullmatch(config["manifest_sha256"]) is None
        ):
            raise ValueError("manifest_sha256 必须是 64 位小写 SHA-256")
        if (
            require_metric_definition
            and config.get("metric_definition")
            != _FORMAL_METRIC_DEFINITION
        ):
            raise ValueError(
                "config.metric_definition 与语义分割模板固定指标口径不一致"
            )
    return config


def _bound_contract(
    frozen: dict[str, _Any],
    purpose: str,
    expected_code: dict[str, str],
    expected_environment: dict[str, str],
) -> bool:
    code = frozen.get("code")
    environment = frozen.get("environment")
    if code == expected_code and environment == expected_environment:
        return False
    if (
        not isinstance(code, dict)
        or code.get("schema") != _BOUND_CODE_SCHEMA
        or code.get("declared_code") != expected_code
    ):
        raise ValueError("frozen.code 中央声明与 PACK-SEG-V1.0.0 不一致")
    if (
        not isinstance(environment, dict)
        or environment.get("declared_environment") != expected_environment
    ):
        raise ValueError("frozen.environment 中央声明与冻结 device 不一致")
    clean_required = code.get("clean_required")
    worktree_clean = code.get("worktree_clean")
    if (
        type(clean_required) is not bool
        or type(worktree_clean) is not bool
        or clean_required != (purpose == "evidence")
        or (clean_required and not worktree_clean)
    ):
        raise ValueError("frozen.code 中央 clean 状态与 purpose 不一致")
    return True


def _declared_data(
    frozen: dict[str, _Any],
    *,
    bound: bool,
    purpose: str,
    real_experiment: bool,
) -> dict[str, _Any]:
    value = frozen.get("data")
    if not isinstance(value, dict):
        raise ValueError("frozen.data 必须是 object")
    data = dict(value)
    central_fields = {"run_kind", "paper_eligible"}
    if not bound:
        if central_fields & set(data):
            raise ValueError("直接 frozen.data 不能伪造中央 Run 字段")
        return data
    if not central_fields.issubset(data):
        raise ValueError("bound frozen.data 缺少中央 run_kind/paper_eligible")
    expected_kind = (
        "real_experiment" if real_experiment else "synthetic_debug_only"
    )
    if data.get("run_kind") != expected_kind:
        raise ValueError("bound frozen.data run_kind 与数据模式不一致")
    code = frozen["code"]
    expected_eligibility = bool(
        purpose == "evidence"
        and real_experiment
        and code["clean_required"]
        and code["worktree_clean"]
    )
    if (
        type(data.get("paper_eligible")) is not bool
        or data["paper_eligible"] != expected_eligibility
    ):
        raise ValueError("bound frozen.data paper_eligible 与中央规则不一致")
    data.pop("run_kind")
    data.pop("paper_eligible")
    return data


def _identity(run: dict[str, _Any]) -> dict[str, _Any]:
    if not isinstance(run, dict):
        raise ValueError("Run 必须是 object")
    run_id = run.get("id")
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("Run id 必须是 RUN-0001")
    purpose = run.get("purpose")
    if purpose not in {"debug", "evidence"}:
        raise ValueError("Run purpose 只允许 debug/evidence")
    frozen = run.get("frozen")
    if not isinstance(frozen, dict) or set(frozen) != _FROZEN_FIELDS:
        raise ValueError("Run frozen 必须精确包含 code/config/seed/data/environment")
    expected_code = {"template_id": "PACK-SEG", "version": "1.0.0"}
    seed = frozen.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("frozen.seed 必须是非负整数")
    config = _normalize_config(frozen.get("config"))
    device = (
        "cpu"
        if config["mode"] == "synthetic_smoke"
        else config["device"]
    )
    bound = _bound_contract(
        frozen,
        purpose,
        expected_code,
        {"backend": "project", "device": device},
    )
    data = _declared_data(
        frozen,
        bound=bound,
        purpose=purpose,
        real_experiment=config["mode"] != "synthetic_smoke",
    )
    if config["mode"] == "synthetic_smoke":
        expected_data = {
            "kind": "synthetic_debug_only",
            "evaluation": {
                "metric": "debug_mean_iou",
                "aggregation": "whole_dataset_confusion_matrix",
                "ignore_index": 255,
            },
        }
        if data != expected_data:
            raise ValueError("synthetic frozen.data/evaluation 被篡改")
        if purpose != "debug":
            raise ValueError("合成数据只能用于 debug，不能作为 evidence")
        data_identity: dict[str, _Any] = {"kind": "synthetic_debug_only"}
    else:
        if not bound and set(data) == {"identity", "evaluation"}:
            data_identity = _validate_data_identity(data.get("identity"))
        elif (
            set(data) == {"kind", "dataset_identity", "evaluation"}
            and data.get("kind") == "local_dataset"
        ):
            data_identity = _validated_central_data_identity(
                data.get("dataset_identity"),
                config,
            )
        else:
            raise ValueError(
                "正式 frozen.data 必须使用 local_dataset + dataset_identity"
            )
        expected_evaluation = {
            "metric": "mean_iou",
            "aggregation": "whole_dataset_confusion_matrix",
            "ignore_index": 255,
            "zero_union": "null_and_omitted_from_mean",
        }
        if data.get("evaluation") != expected_evaluation:
            raise ValueError("SEG 正式 evaluation 口径被篡改")
        for key in ("dataset_id", "version", "source_uri", "manifest_sha256"):
            if data_identity[key] != config[key]:
                raise ValueError(f"config 与 data identity {key} 身份冲突")
    return {
        "run_id": run_id,
        "purpose": purpose,
        "seed": seed,
        "mode": config["mode"],
        "device": device,
        "config": config,
        "config_sha256": _canonical_sha256(config),
        "data_identity": data_identity,
    }


def _output_dir(project: _Path, run_id: str) -> _Path:
    root = _require_directory(project, "项目根")
    target = root / ".cv-workflow-output" / run_id
    target.relative_to(root)
    return target


def _create_staging(project: _Path, run_id: str) -> _Path:
    root = _require_directory(project, "项目根")
    workflow = root / ".cv-workflow-output"
    if _os.path.lexists(workflow):
        _require_directory(workflow, "Run 输出父目录")
    else:
        workflow.mkdir()
        _require_directory(workflow, "新建 Run 输出父目录")
    staging = workflow / f".{run_id}.staging-{_uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    return _require_directory(staging, "Run staging")


def _safe_environment(seed: int) -> dict[str, str]:
    allowed = (
        "SystemRoot",
        "WINDIR",
        "PATH",
        "PATHEXT",
        "TEMP",
        "TMP",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMDATA",
        "USERPROFILE",
        "USERNAME",
        "CUDA_VISIBLE_DEVICES",
    )
    environment = {
        key: _os.environ[key]
        for key in allowed
        if key in _os.environ
    }
    environment.update(
        {
            "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
            "PYTHONHASHSEED": str(seed),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "NO_PROXY": "*",
            "no_proxy": "*",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    return environment


def _require_device_available(device: str) -> None:
    if device != "cuda":
        return
    _os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
    try:
        import torch as _torch
    except ImportError as error:
        raise RuntimeError("请求了 CUDA/GPU，但当前环境缺少 PyTorch") from error
    if not _torch.cuda.is_available():
        raise RuntimeError("请求了 CUDA/GPU，但当前 PyTorch 环境检测不到可用 CUDA")
    try:
        probe = _torch.empty(1, device="cuda")
        probe.add_(1.0)
        _torch.cuda.synchronize()
    except Exception as error:
        raise RuntimeError(
            "CUDA/GPU 小算子探测失败；驱动、PyTorch CUDA 构建或显卡架构不兼容"
        ) from error


def _load_checkpoint_model(project: _Path, num_classes: int) -> _Any:
    package_name = f"_pack_seg_validate_{_uuid.uuid4().hex}"
    package = _types.ModuleType(package_name)
    package.__path__ = [str(project / "seg")]
    loaded = [package_name]
    _sys.modules[package_name] = package
    try:
        for name in ("contract", "model"):
            qualified = f"{package_name}.{name}"
            spec = _importlib_util.spec_from_file_location(
                qualified,
                project / "seg" / f"{name}.py",
            )
            if spec is None or spec.loader is None:
                raise ValueError("无法加载 PACK-SEG 模型模块")
            module = _importlib_util.module_from_spec(spec)
            _sys.modules[qualified] = module
            loaded.append(qualified)
            spec.loader.exec_module(module)
        return _sys.modules[f"{package_name}.model"].build_model(num_classes)
    finally:
        for name in reversed(loaded):
            _sys.modules.pop(name, None)


def _load_png_budget(project: _Path) -> tuple[_Any, int]:
    package_name = f"_pack_seg_png_budget_{_uuid.uuid4().hex}"
    package = _types.ModuleType(package_name)
    package.__path__ = [str(project / "seg")]
    loaded = [package_name]
    _sys.modules[package_name] = package
    try:
        for name in ("contract", "data"):
            qualified = f"{package_name}.{name}"
            spec = _importlib_util.spec_from_file_location(
                qualified,
                project / "seg" / f"{name}.py",
            )
            if spec is None or spec.loader is None:
                raise ValueError("无法加载 PACK-SEG PNG 预算模块")
            module = _importlib_util.module_from_spec(spec)
            _sys.modules[qualified] = module
            loaded.append(qualified)
            spec.loader.exec_module(module)
        data = _sys.modules[f"{package_name}.data"]
        return data.inspect_png_budget, data.MAX_DATASET_PIXELS
    finally:
        for name in reversed(loaded):
            _sys.modules.pop(name, None)


def _validate_artifact_tree(
    output: _Path,
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
            or ".." in _Path(item["path"]).parts
            or item["path"] in listed
            or isinstance(item.get("size"), bool)
            or not isinstance(item.get("size"), int)
            or item["size"] < 0
            or not isinstance(item.get("sha256"), str)
            or _SHA256.fullmatch(item["sha256"]) is None
        ):
            raise ValueError("artifacts.json 条目无效")
        listed.add(item["path"])
        content = _safe_read(
            output / _Path(*item["path"].split("/")),
            f"产物 {item['path']}",
            root=output,
        )
        listed_total += len(content)
        if listed_total > _MAX_TOTAL_SIZE:
            raise ValueError("运行产物总体积超过上限")
        if (
            len(content) != item["size"]
            or _hashlib.sha256(content).hexdigest() != item["sha256"]
        ):
            raise ValueError(f"产物 SHA-256/大小不一致：{item['path']}")
    actual: set[str] = set()
    actual_total = manifest_size
    for path in output.rglob("*"):
        if path == output / "artifacts.json":
            continue
        if path.is_dir() and not path.is_symlink():
            _require_directory(path, "产物子目录")
            continue
        content = _safe_read(path, "实际产物", root=output)
        actual_total += len(content)
        if actual_total > _MAX_TOTAL_SIZE:
            raise ValueError("运行产物总体积超过上限")
        actual.add(path.relative_to(output).as_posix())
    if actual != listed:
        raise ValueError("运行目录含未登记、缺失或替换的产物")
    return listed


def _validate_artifacts(output: _Path) -> list[str]:
    manifest_path = output / "artifacts.json"
    payload, manifest_content, manifest_identity = _read_json_snapshot(
        manifest_path,
        "artifacts.json",
        root=output,
    )
    if set(payload) != {"schema", "artifacts"} or payload.get("schema") != "pack-seg.artifacts.v1":
        raise ValueError("artifacts.json schema 无效")
    records = payload.get("artifacts")
    if not isinstance(records, list) or not records or len(records) > _MAX_FILES:
        raise ValueError("artifacts.json 条目数量无效")
    _validate_artifact_tree(output, records, len(manifest_content))
    final_payload, final_content, final_identity = _read_json_snapshot(
        manifest_path,
        "artifacts.json",
        root=output,
    )
    if (
        final_payload != payload
        or final_content != manifest_content
        or final_identity != manifest_identity
    ):
        raise ValueError("artifacts.json 在验收过程中发生变化")
    listed = _validate_artifact_tree(output, records, len(final_content))
    last_payload, last_content, last_identity = _read_json_snapshot(
        manifest_path,
        "artifacts.json",
        root=output,
    )
    if (
        last_payload != payload
        or last_content != manifest_content
        or last_identity != manifest_identity
    ):
        raise ValueError("artifacts.json 在最终目录重扫过程中发生变化")
    return sorted(listed)


def _validate_run(
    record: dict[str, _Any],
    identity: dict[str, _Any],
) -> None:
    fields = {
        "schema",
        "phase",
        "mode",
        "run_kind",
        "paper_eligible",
        "run_id",
        "config_sha256",
        "seed",
        "device",
        "num_classes",
        "image_size",
        "training_sample_count",
        "evaluation_sample_count",
        "checkpoint",
        "optimizer_steps",
        "dataset_identity",
        "checkpoint_reloaded_for_evaluation",
        "checkpoint_reloaded_for_inference",
    }
    if set(record) != fields or record.get("schema") != "pack-seg.run.v1":
        raise ValueError("run.json schema 字段无效")
    synthetic = identity["mode"] == "synthetic_smoke"
    expected = {
        "phase": "synthetic_smoke" if synthetic else "train",
        "mode": identity["mode"],
        "run_kind": "synthetic_debug_only" if synthetic else "local_dataset_baseline",
        "paper_eligible": False,
        "run_id": identity["run_id"],
        "config_sha256": identity["config_sha256"],
        "seed": identity["seed"],
        "device": identity["device"],
        "checkpoint": "checkpoint.pt",
        "dataset_identity": identity["data_identity"],
        "checkpoint_reloaded_for_evaluation": True,
        "checkpoint_reloaded_for_inference": True,
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise ValueError(f"run.json {key} 与冻结身份冲突")
    for key in (
        "num_classes",
        "image_size",
        "training_sample_count",
        "evaluation_sample_count",
        "optimizer_steps",
    ):
        if isinstance(record.get(key), bool) or not isinstance(record.get(key), int) or record[key] <= 0:
            raise ValueError(f"run.json {key} 必须是正整数")


def _validate_evaluation(
    payload: dict[str, _Any],
    *,
    synthetic: bool,
    num_classes: int,
) -> dict[str, float]:
    if set(payload) != {
        "schema",
        "metrics",
        "confusion_matrix",
        "per_class_iou",
        "included_classes",
        "definition",
    } or payload.get("schema") != "pack-seg.evaluation.v1":
        raise ValueError("evaluation.json schema 无效")
    metric_name = "debug_mean_iou" if synthetic else "mean_iou"
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != {metric_name}:
        raise ValueError("evaluation.json 合成/正式指标名混用")
    value = metrics[metric_name]
    if type(value) is not float or not _math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("mIoU 必须是有限 [0,1] float")
    matrix = payload.get("confusion_matrix")
    per_class = payload.get("per_class_iou")
    included = payload.get("included_classes")
    if (
        not isinstance(matrix, list)
        or len(matrix) != num_classes
        or any(
            not isinstance(row, list)
            or len(row) != num_classes
            or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in row)
            for row in matrix
        )
        or not isinstance(per_class, list)
        or len(per_class) != num_classes
        or not isinstance(included, list)
        or not included
        or any(isinstance(item, bool) or not isinstance(item, int) or not 0 <= item < num_classes for item in included)
    ):
        raise ValueError("混淆矩阵/per-class IoU/included classes 无效")
    for index, item in enumerate(per_class):
        if index in included:
            if type(item) is not float or not _math.isfinite(item) or not 0.0 <= item <= 1.0:
                raise ValueError("included class IoU 无效")
        elif item is not None:
            raise ValueError("union=0 类必须为 null 并从 mean 中省略")
    expected_definition = {
        "aggregation": "whole_dataset_confusion_matrix",
        "ignore_index": 255,
        "zero_union": "null_and_omitted_from_mean",
        "one_sided_union": "included",
    }
    if payload.get("definition") != expected_definition:
        raise ValueError("mIoU 定义被篡改")
    expected_per_class: list[float | None] = []
    expected_included: list[int] = []
    for class_index in range(num_classes):
        intersection = matrix[class_index][class_index]
        target_count = sum(matrix[class_index])
        prediction_count = sum(row[class_index] for row in matrix)
        union = target_count + prediction_count - intersection
        if union == 0:
            expected_per_class.append(None)
            continue
        expected_per_class.append(float(intersection / union))
        expected_included.append(class_index)
    if included != expected_included:
        raise ValueError("included_classes 与 confusion_matrix 重算结果不一致")
    for observed, expected in zip(per_class, expected_per_class, strict=True):
        if expected is None:
            if observed is not None:
                raise ValueError("per_class_iou 与 confusion_matrix 重算结果不一致")
        elif not _math.isclose(
            observed,
            expected,
            rel_tol=1e-7,
            abs_tol=1e-7,
        ):
            raise ValueError("per_class_iou 与 confusion_matrix 重算结果不一致")
    expected_mean = sum(
        expected_per_class[index]
        for index in expected_included
    ) / len(expected_included)
    if not _math.isclose(expected_mean, value, rel_tol=1e-7, abs_tol=1e-7):
        raise ValueError("mean_iou 与 confusion_matrix 重算结果不一致")
    return {metric_name: value}


def _validate_predictions(
    output: _Path,
    sample_count: int,
    num_classes: int | None = None,
) -> None:
    if num_classes is not None and (
        isinstance(num_classes, bool)
        or not isinstance(num_classes, int)
        or num_classes < 2
    ):
        raise ValueError("预测 mask num_classes 无效")
    inspect_png_budget, max_total_pixels = _load_png_budget(
        _Path(__file__).resolve().parent
    )
    payload = _read_json(
        output / "predictions" / "index.json",
        "predictions/index.json",
        root=output,
    )
    if (
        set(payload) != {"schema", "predictions"}
        or payload.get("schema") != "pack-seg.predictions.v1"
        or not isinstance(payload.get("predictions"), list)
        or len(payload["predictions"]) != sample_count
    ):
        raise ValueError("predictions/index.json 数量或 schema 无效")
    inputs: set[str] = set()
    masks: set[str] = set()
    total_pixels = 0
    for index, item in enumerate(payload["predictions"]):
        if (
            not isinstance(item, dict)
            or set(item) != {"input", "mask", "width", "height", "sha256"}
            or not isinstance(item.get("input"), str)
            or not item["input"]
            or item["input"] in inputs
            or item.get("mask") != f"masks/{index:04d}.png"
            or item["mask"] in masks
            or isinstance(item.get("width"), bool)
            or not isinstance(item.get("width"), int)
            or item["width"] <= 0
            or isinstance(item.get("height"), bool)
            or not isinstance(item.get("height"), int)
            or item["height"] <= 0
            or not isinstance(item.get("sha256"), str)
            or _SHA256.fullmatch(item["sha256"]) is None
        ):
            raise ValueError("预测索引条目身份/尺寸无效")
        inputs.add(item["input"])
        masks.add(item["mask"])
        content = _safe_read(
            output / "predictions" / "masks" / f"{index:04d}.png",
            "预测 mask",
            root=output,
        )
        if _hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError("预测 mask SHA-256 不一致")
        width, height, pixels = inspect_png_budget(
            content,
            label="预测 mask",
            channels=1,
        )
        total_pixels += pixels
        if total_pixels > max_total_pixels:
            raise ValueError("预测 mask 累计像素超过上限")
        if (width, height) != (item["width"], item["height"]):
            raise ValueError("预测 mask IHDR 尺寸与索引不一致")
        try:
            from PIL import Image as _Image

            with _Image.open(_io.BytesIO(content)) as image:
                image.load()
                if (
                    image.format != "PNG"
                    or image.mode != "L"
                    or image.size != (item["width"], item["height"])
                ):
                    raise ValueError("预测 mask 必须是真实 L 模式 PNG")
                if num_classes is not None:
                    minimum, maximum = image.getextrema()
                    if minimum < 0 or maximum >= num_classes:
                        raise ValueError(
                            "预测 mask 每像素类别必须位于 0..num_classes-1"
                        )
        except (ImportError, OSError, SyntaxError) as error:
            raise ValueError("预测 mask 无法解码") from error


def _validate_checkpoint(
    output: _Path,
    record: dict[str, _Any],
    identity: dict[str, _Any],
    project: _Path,
) -> None:
    source = output / "checkpoint.pt"
    try:
        import torch as _torch

        content = _safe_read(source, "checkpoint.pt", root=output)
        checkpoint = _torch.load(
            _io.BytesIO(content),
            map_location="cpu",
            weights_only=True,
        )
    except (ImportError, OSError, RuntimeError, ValueError, TypeError) as error:
        raise ValueError("checkpoint.pt 无法按 state_dict 安全读取") from error
    fields = {
        "schema",
        "architecture",
        "model_state_dict",
        "num_classes",
        "image_size",
        "seed",
        "optimizer_steps",
        "run_id",
        "config_sha256",
        "dataset_identity",
        "run_kind",
    }
    if (
        not isinstance(checkpoint, dict)
        or set(checkpoint) != fields
        or checkpoint.get("schema") != "pack-seg.checkpoint.v1"
        or checkpoint.get("architecture") != "TinySegmenter-v1"
        or checkpoint.get("num_classes") != record["num_classes"]
        or checkpoint.get("image_size") != record["image_size"]
        or checkpoint.get("seed") != identity["seed"]
        or checkpoint.get("optimizer_steps") != record["optimizer_steps"]
        or checkpoint.get("run_id") != identity["run_id"]
        or checkpoint.get("config_sha256") != identity["config_sha256"]
        or checkpoint.get("dataset_identity") != identity["data_identity"]
        or checkpoint.get("run_kind") != record["run_kind"]
        or not isinstance(checkpoint.get("model_state_dict"), dict)
        or not checkpoint["model_state_dict"]
        or any(not isinstance(value, _torch.Tensor) for value in checkpoint["model_state_dict"].values())
        or any(not bool(_torch.isfinite(value).all()) for value in checkpoint["model_state_dict"].values())
    ):
        raise ValueError("checkpoint 与架构/配置/数据/Run 身份冲突")
    model = _load_checkpoint_model(project, record["num_classes"])
    try:
        model.load_state_dict(checkpoint["model_state_dict"], strict=True)
    except (RuntimeError, TypeError, ValueError) as error:
        raise ValueError("checkpoint state_dict 无法 strict=True 加载") from error


def _validated_outputs(
    output: _Path,
    identity: dict[str, _Any],
    project: _Path,
) -> tuple[dict[str, _Any], dict[str, float], list[str]]:
    root = _require_directory(output, "Run 输出")
    listed = _validate_artifacts(root)
    required = {
        "checkpoint.pt",
        "evaluation.json",
        "predictions/index.json",
        "raw.log",
        "run.json",
    }
    if not required.issubset(listed):
        raise ValueError("Run 缺少 checkpoint/evaluation/predictions/raw log/run 产物")
    record = _read_json(root / "run.json", "run.json", root=root)
    _validate_run(record, identity)
    metrics = _validate_evaluation(
        _read_json(root / "evaluation.json", "evaluation.json", root=root),
        synthetic=identity["mode"] == "synthetic_smoke",
        num_classes=record["num_classes"],
    )
    _validate_predictions(
        root,
        record["evaluation_sample_count"],
        record["num_classes"],
    )
    if identity["mode"] == "local_segmentation":
        manifest = _read_json(
            root / "dataset_manifest.json",
            "dataset_manifest.json",
            root=root,
        )
        if (
            manifest.get("manifest_sha256")
            != identity["data_identity"]["manifest_sha256"]
        ):
            raise ValueError("dataset_manifest 与冻结 data identity 不一致")
    _validate_checkpoint(root, record, identity, project)
    return record, metrics, listed


def inspect(project: _Path) -> dict[str, _Any]:
    return {
        "project": _Path(project).name,
        "direction": "seg",
        "template_id": "PACK-SEG",
        "template_version": "1.0.0",
        "standard": {"debug_required": True},
        "baseline": {"metric": "mean_iou"},
        "synthetic": {
            "run_kind": "synthetic_debug_only",
            "paper_eligible": False,
        },
    }


def validate(project: _Path) -> list[str]:
    root = _Path(project)
    issues = []
    for relative in (
        "domain-pack.json",
        "configs/smoke.json",
        "configs/baseline.json",
        "contracts/metrics.v1.json",
        "seg/contract.py",
        "seg/data.py",
        "seg/model.py",
        "seg/train.py",
        "seg/evaluate.py",
        "seg/infer.py",
        "seg/smoke.py",
    ):
        if not (root / relative).is_file():
            issues.append(f"缺少语义分割模板文件：{relative}")
    try:
        import numpy as _numpy  # noqa: F401
        import torch as _torch  # noqa: F401
        from PIL import Image as _Image  # noqa: F401
    except ImportError:
        issues.append("缺少 torch、numpy 或 Pillow")
    return issues


def prepare_runs(
    project: _Path,
    task: dict[str, _Any],
) -> list[dict[str, _Any]]:
    route_inputs = task.get("route_inputs")
    if not isinstance(route_inputs, dict) or not set(route_inputs).issubset({"config", "seed"}):
        raise ValueError("Task route_inputs 只允许 config/seed")
    config = _normalize_config(
        route_inputs.get("config", {"mode": "synthetic_smoke"}),
        require_metric_definition=False,
    )
    if config["mode"] == "local_segmentation":
        config["metric_definition"] = dict(_FORMAL_METRIC_DEFINITION)
    seed = route_inputs.get("seed", 7)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("Task seed 必须是非负整数")
    if config["mode"] == "synthetic_smoke":
        data = {
            "kind": "synthetic_debug_only",
            "evaluation": {
                "metric": "debug_mean_iou",
                "aggregation": "whole_dataset_confusion_matrix",
                "ignore_index": 255,
            },
        }
    else:
        data = {
            "kind": "local_dataset",
            "dataset_identity": _central_data_identity(config),
            "evaluation": {
                "metric": "mean_iou",
                "aggregation": "whole_dataset_confusion_matrix",
                "ignore_index": 255,
                "zero_union": "null_and_omitted_from_mean",
            },
        }
    return [
        {
            "code": {"template_id": "PACK-SEG", "version": "1.0.0"},
            "config": config,
            "seed": seed,
            "data": data,
            "environment": {
                "backend": "project",
                "device": (
                    "cpu"
                    if config["mode"] == "synthetic_smoke"
                    else config["device"]
                ),
            },
        }
    ]


def execute(
    project: _Path,
    action: str,
    run: dict[str, _Any],
) -> dict[str, _Any]:
    identity = _identity(run)
    root = _require_directory(project, "项目根")
    output = _output_dir(root, identity["run_id"])
    if action == "status":
        if not _os.path.lexists(output):
            return {"status": "not_started"}
        try:
            _validated_outputs(output, identity, root)
        except ValueError:
            return {"status": "failed"}
        return {"status": "finished"}
    if action == "stop":
        return {"status": "stopped"}
    if action != "start":
        raise ValueError(f"不支持的 action：{action}")
    if _os.path.lexists(output):
        raise FileExistsError(f"Run 输出已存在，拒绝覆盖：{output}")
    _require_device_available(identity["device"])
    staging = _create_staging(root, identity["run_id"])
    result_dir = staging / "result"
    config = identity["config"]
    if identity["mode"] == "synthetic_smoke":
        command = [
            _sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "seg.smoke",
            "--work-dir",
            str(result_dir),
            "--config",
            "configs/smoke.json",
            "--device",
            "cpu",
            "--seed",
            str(identity["seed"]),
            "--run-id",
            identity["run_id"],
            "--config-sha256",
            identity["config_sha256"],
        ]
    else:
        data_root = _Path(config["data_root"])
        if not data_root.is_absolute():
            data_root = root / data_root
        command = [
            _sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "seg.train",
            "--config",
            "configs/baseline.json",
            "--data-root",
            str(data_root),
            "--output-dir",
            str(result_dir),
            "--dataset-id",
            config["dataset_id"],
            "--dataset-version",
            config["version"],
            "--source-uri",
            config["source_uri"],
            "--manifest-sha256",
            config["manifest_sha256"],
            "--seed",
            str(identity["seed"]),
            "--run-id",
            identity["run_id"],
            "--config-sha256",
            identity["config_sha256"],
            "--device",
            identity["device"],
        ]
    try:
        completed = _subprocess.run(
            command,
            cwd=root,
            env=_safe_environment(identity["seed"]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90.0,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "PACK-SEG 子进程失败："
                f"exit={completed.returncode} stderr={completed.stderr[-2000:]}"
            )
        _validated_outputs(result_dir, identity, root)
        if _os.path.lexists(output):
            raise FileExistsError("发布前 Run 输出路径被占用，拒绝覆盖")
        _os.rename(result_dir, output)
        _validated_outputs(output, identity, root)
        return {"status": "finished", "process_id": None}
    except BaseException as error:
        raise RuntimeError(
            "PACK-SEG 运行失败；未递归清理任何目录，"
            f"staging 保留在 {staging}；原因：{error}"
        ) from error


def parse_result(
    project: _Path,
    run: dict[str, _Any],
) -> dict[str, _Any]:
    identity = _identity(run)
    root = _require_directory(project, "项目根")
    output = _output_dir(root, identity["run_id"])
    _, metrics, listed = _validated_outputs(output, identity, root)
    relative = output.relative_to(root)
    artifacts = [
        (relative / path).as_posix()
        for path in [*listed, "artifacts.json"]
    ]
    synthetic = identity["mode"] == "synthetic_smoke"
    return {
        "execution": {
            "outcome": "succeeded",
            "exit_code": 0,
            "issue_kind": None,
        },
        "result": {
            "metrics": metrics,
            "raw_log": (relative / "raw.log").as_posix(),
        },
        "quality": {
            "implementation": "valid",
            "interface": "valid",
            "data": "valid",
            "metrics": "valid",
        },
        "analysis": {
            "hypothesis": "inconclusive",
            "limitations": (
                ["合成数据只证明链路可运行，永远不能作为论文成绩"]
                if synthetic
                else ["方向包结果仍需外层工作流冻结 Git 与环境后才能晋级"]
            ),
            "suggestions": [],
        },
        "artifacts": artifacts,
    }
