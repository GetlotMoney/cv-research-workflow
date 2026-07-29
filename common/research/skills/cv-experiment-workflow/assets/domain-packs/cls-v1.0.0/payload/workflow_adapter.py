from __future__ import annotations

import hashlib as _hashlib
import json as _json
import math as _math
import os as _os
import re as _re
import stat as _stat
import subprocess as _subprocess
import sys as _sys
import types as _types
import unicodedata as _unicodedata
import uuid as _uuid
import importlib.util as _importlib_util
from pathlib import Path as _Path
from typing import Any as _Any


_RUN_ID = _re.compile(r"RUN-[0-9]{4}")
_SHA256 = _re.compile(r"[0-9a-f]{64}")
_MODES = {"synthetic_smoke", "local_imagefolder"}
_METRICS = {"top1_accuracy", "top5_accuracy", "macro_f1"}
_REPARSE_POINT = 0x400
_DATASET_IDENTITY_SCHEMA = "cv-experiment-workflow.dataset-identity.v1"
_DATASET_MANIFEST_SCHEMA = "pack-cls.dataset-file-manifest.v1"
_MAX_DATASET_FILES = 100_000
_METRIC_DEFINITION = {
    "top1_accuracy": (
        "验证集样本中最高分预测类别与真实标签一致的比例；"
        "取值为 0 到 1，越高越好。"
    ),
    "top5_accuracy": (
        "验证集真实标签落入得分最高的 min(5, 类别数) 个预测中的比例；"
        "取值为 0 到 1，越高越好。"
    ),
    "macro_f1": (
        "先分别计算每个类别的 F1，再对所有类别做等权平均；"
        "取值为 0 到 1，越高越好。"
    ),
}


def _config(run: dict[str, _Any]) -> dict[str, _Any]:
    frozen = run.get("frozen")
    if not isinstance(frozen, dict):
        raise ValueError("Run 缺少 frozen")
    config = frozen.get("config")
    if not isinstance(config, dict):
        raise ValueError("Run 缺少 frozen.config")
    try:
        encoded = _json.dumps(
            config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        normalized = _json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError("Run frozen.config 必须是有限 JSON object") from error
    if normalized != config:
        raise ValueError("Run frozen.config 序列化后会改变")
    return dict(config)


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
        }
    )
    return environment


def _identity(run: dict[str, _Any]) -> dict[str, _Any]:
    run_id = run.get("id")
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("Run id 必须是 RUN-0001 形式")
    purpose = run.get("purpose")
    if purpose not in {"debug", "evidence"}:
        raise ValueError("Run purpose 只允许 debug/evidence")
    frozen = run.get("frozen")
    if not isinstance(frozen, dict):
        raise ValueError("Run 缺少 frozen")
    seed = frozen.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("Run frozen.seed 必须是非负整数")
    config = _config(run)
    if "output_dir" in config:
        raise ValueError(
            "config.output_dir 不允许由路线指定；工作流会按 Run id 绑定唯一输出目录"
        )
    mode = config.get("mode")
    if mode not in _MODES:
        raise ValueError("config.mode 只允许 synthetic_smoke/local_imagefolder")
    if mode == "local_imagefolder":
        config.setdefault("device", "cuda")
    allowed = (
        {"mode", "metric_definition"}
        if mode == "synthetic_smoke"
        else None
    )
    local_legacy = {
        "mode",
        "data_root",
        "device",
        "metric_definition",
    }
    local_bound = {
        "mode",
        "data_root",
        "dataset_id",
        "version",
        "source_uri",
        "device",
        "metric_definition",
    }
    if (
        mode == "synthetic_smoke"
        and set(config) != allowed
    ) or (
        mode == "local_imagefolder"
        and set(config) not in (local_legacy, local_bound)
    ):
        if allowed is None:
            allowed = local_bound
        raise ValueError(
            f"{mode} 配置字段必须精确为：{sorted(allowed)}"
        )
    if config.get("metric_definition") != _METRIC_DEFINITION:
        raise ValueError("config.metric_definition 与分类模板固定口径不一致")
    if mode == "synthetic_smoke" and purpose != "debug":
        raise ValueError(
            "合成 synthetic smoke 只能用于 debug，不能作为 evidence 或论文证据"
        )
    if mode == "local_imagefolder":
        if config.get("device") not in {"cpu", "cuda"}:
            raise ValueError("local_imagefolder device 只允许 cpu/cuda")
        data_root = config.get("data_root")
        if (
            not isinstance(data_root, str)
            or not data_root
            or data_root != data_root.strip()
            or any(ord(character) < 32 for character in data_root)
        ):
            raise ValueError("local_imagefolder 必须提供规范的 data_root")
    device = "cpu" if mode == "synthetic_smoke" else config["device"]
    expected_environment = {"backend": "project", "device": device}
    environment = frozen.get("environment")
    if environment is not None and not (
        environment == expected_environment
        or (
            isinstance(environment, dict)
            and environment.get("declared_environment")
            == expected_environment
        )
    ):
        raise ValueError("Run frozen.environment 与冻结 device 不一致")
    config_sha256 = _hashlib.sha256(
        _json.dumps(
            config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "run_id": run_id,
        "purpose": purpose,
        "seed": seed,
        "mode": mode,
        "device": device,
        "config": config,
        "config_sha256": config_sha256,
    }


def _output_dir(project: _Path, identity: dict[str, _Any]) -> _Path:
    root = _Path(project).resolve(strict=True)
    target = (
        root
        / ".cv-workflow-output"
        / identity["run_id"]
    )
    target.relative_to(root)
    return target


def _output_root(project: _Path) -> _Path:
    root = _Path(project).resolve(strict=True)
    output_root = root / ".cv-workflow-output"
    if _os.path.lexists(output_root):
        if _is_link_or_reparse(output_root) or not output_root.is_dir():
            raise ValueError("Run 固定输出根必须是普通目录")
    else:
        output_root.mkdir()
    return output_root


def _create_local_staging(
    project: _Path,
    identity: dict[str, _Any],
) -> _Path:
    output_root = _output_root(project)
    staging = output_root / (
        f".{identity['run_id']}.staging-{_uuid.uuid4().hex}"
    )
    if _os.path.lexists(staging):
        raise FileExistsError("随机 Run staging 已存在，拒绝覆盖")
    return staging


def _is_link_or_reparse(path: _Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _regular_file(path: _Path) -> _Path:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError(f"必需产物不存在：{path.name}") from error
    if (
        not _stat.S_ISREG(metadata.st_mode)
        or _is_link_or_reparse(path)
    ):
        raise ValueError(f"必需产物不是普通文件：{path.name}")
    return path


def _raw_log_bytes(identity: dict[str, _Any]) -> bytes:
    return (
        "schema=pack-cls.raw-log.v1\n"
        f"run_id={identity['run_id']}\n"
        f"mode={identity['mode']}\n"
        f"device={identity['device']}\n"
        f"config_sha256={identity['config_sha256']}\n"
        "exit_code=0\n"
    ).encode("utf-8")


def _write_raw_log(
    output: _Path,
    identity: dict[str, _Any],
) -> None:
    target = output / "raw.log"
    try:
        descriptor = _os.open(
            target,
            _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL,
            0o600,
        )
    except FileExistsError as error:
        raise FileExistsError("raw.log 已存在，拒绝覆盖") from error
    with _os.fdopen(descriptor, "wb") as handle:
        handle.write(_raw_log_bytes(identity))
        handle.flush()
        _os.fsync(handle.fileno())


def _validate_raw_log(
    output: _Path,
    identity: dict[str, _Any],
) -> None:
    source = _regular_file(output / "raw.log")
    if source.read_bytes() != _raw_log_bytes(identity):
        raise ValueError("raw.log 与冻结 Run 身份不一致")


def _read_json(path: _Path) -> dict[str, _Any]:
    source = _regular_file(path)
    try:
        payload = _json.loads(
            source.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"非有限数字：{value}")
            ),
        )
    except (OSError, UnicodeError, _json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"无法读取严格 JSON 产物 {source.name}：{error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"输出不是 JSON object：{source.name}")
    return payload


def _validate_record(
    identity: dict[str, _Any],
    record: dict[str, _Any],
) -> None:
    if record.get("mode") != identity["mode"]:
        raise ValueError("run.json mode 与当前 Run 模式不一致")
    if record.get("run_id") != identity["run_id"]:
        raise ValueError("run.json run_id 与当前 Run 身份不一致")
    if record.get("config_sha256") != identity["config_sha256"]:
        raise ValueError("run.json config_sha256 与冻结配置身份不一致")
    if record.get("seed") != identity["seed"]:
        raise ValueError("run.json seed 与冻结身份不一致")
    common = {
        "schema",
        "phase",
        "mode",
        "run_kind",
        "paper_eligible",
        "run_id",
        "config_sha256",
        "seed",
        "device",
        "sample_count",
        "class_names",
        "checkpoint",
        "checkpoint_reloaded",
        "optimizer_steps",
    }
    expected_fields = (
        common
        if identity["mode"] == "synthetic_smoke"
        else common | {"training_sample_count", "evaluation_split"}
    )
    if set(record) != expected_fields:
        raise ValueError("run.json schema 字段被篡改")
    expected = {
        "schema": "pack-cls.run.v1",
        "phase": (
            "synthetic_smoke"
            if identity["mode"] == "synthetic_smoke"
            else "train"
        ),
        "mode": identity["mode"],
        "run_kind": (
            "synthetic_debug_only"
            if identity["mode"] == "synthetic_smoke"
            else "local_dataset_baseline"
        ),
        "paper_eligible": False,
        "run_id": identity["run_id"],
        "config_sha256": identity["config_sha256"],
        "seed": identity["seed"],
        "device": identity["device"],
        "checkpoint": "checkpoint.pt",
        "checkpoint_reloaded": True,
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise ValueError(f"run.json {key} 与冻结身份不一致或被篡改")
    if (
        isinstance(record.get("optimizer_steps"), bool)
        or not isinstance(record.get("optimizer_steps"), int)
        or record["optimizer_steps"] <= 0
    ):
        raise ValueError("run.json optimizer_steps 必须是正整数")
    if (
        isinstance(record.get("sample_count"), bool)
        or not isinstance(record.get("sample_count"), int)
        or record["sample_count"] <= 0
    ):
        raise ValueError("run.json sample_count 必须是正整数")
    class_names = record.get("class_names")
    if (
        not isinstance(class_names, list)
        or not class_names
        or not all(isinstance(item, str) and item for item in class_names)
        or len(set(class_names)) != len(class_names)
    ):
        raise ValueError("run.json class_names 无效")
    if identity["mode"] == "local_imagefolder":
        if (
            isinstance(record.get("training_sample_count"), bool)
            or not isinstance(record.get("training_sample_count"), int)
            or record["training_sample_count"] <= 0
        ):
            raise ValueError("run.json training_sample_count 必须是正整数")
        if record.get("evaluation_split") != "val":
            raise ValueError("run.json evaluation_split 必须与基线配置 val 一致")


def _validate_metrics(payload: dict[str, _Any]) -> dict[str, float]:
    if set(payload) != {"schema", "metrics"}:
        raise ValueError("metrics.json schema 字段无效")
    if payload.get("schema") != "pack-cls.metrics-output.v1":
        raise ValueError("metrics.json schema 无效")
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != _METRICS:
        raise ValueError("metrics.json 指标必须精确包含 top1/top5/macro_f1")
    for name, value in metrics.items():
        if type(value) is not float or not _math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"指标 {name} 必须是有限 [0,1] float")
    return dict(metrics)


def _validate_predictions(
    payload: dict[str, _Any],
    class_names: list[str],
    sample_count: int,
) -> None:
    if (
        set(payload) != {"schema", "predictions"}
        or payload.get("schema") != "pack-cls.predictions.v1"
        or not isinstance(payload.get("predictions"), list)
        or not payload["predictions"]
    ):
        raise ValueError("predictions.json schema 或 predictions 无效")
    if len(payload["predictions"]) != sample_count:
        raise ValueError(
            "predictions.json 数量必须与 run.json sample_count 精确一致"
        )
    inputs: set[str] = set()
    for item in payload["predictions"]:
        if not isinstance(item, dict) or set(item) != {
            "input",
            "class_index",
            "class_name",
            "score",
        }:
            raise ValueError("predictions.json 条目字段无效")
        class_index = item.get("class_index")
        if (
            isinstance(class_index, bool)
            or not isinstance(class_index, int)
            or not 0 <= class_index < len(class_names)
            or item.get("class_name") != class_names[class_index]
            or not isinstance(item.get("input"), str)
            or not item["input"].strip()
            or item["input"] != item["input"].strip()
            or type(item.get("score")) is not float
            or not _math.isfinite(item["score"])
            or not 0.0 <= item["score"] <= 1.0
        ):
            raise ValueError("predictions.json 条目身份或数字无效")
        if item["input"] in inputs:
            raise ValueError("predictions.json input 必须全局唯一，不能重复")
        inputs.add(item["input"])


def _build_checkpoint_model(project: _Path, num_classes: int) -> _Any:
    package_name = f"_pack_cls_checkpoint_{_uuid.uuid4().hex}"
    package = _types.ModuleType(package_name)
    package.__path__ = [str(project / "cls")]
    loaded_names = [package_name]
    _sys.modules[package_name] = package
    try:
        for module_name in ("contract", "model"):
            qualified = f"{package_name}.{module_name}"
            path = project / "cls" / f"{module_name}.py"
            spec = _importlib_util.spec_from_file_location(qualified, path)
            if spec is None or spec.loader is None:
                raise ValueError(f"无法加载 checkpoint 模型模块：{path}")
            module = _importlib_util.module_from_spec(spec)
            _sys.modules[qualified] = module
            loaded_names.append(qualified)
            spec.loader.exec_module(module)
        builder = _sys.modules[f"{package_name}.model"].build_model
        return builder(num_classes)
    except (OSError, ImportError, AttributeError, RuntimeError, ValueError) as error:
        raise ValueError("无法按 PACK-CLS build_model 构建 checkpoint 模型") from error
    finally:
        for name in reversed(loaded_names):
            _sys.modules.pop(name, None)


def _validate_checkpoint(
    path: _Path,
    record: dict[str, _Any],
    project: _Path,
) -> None:
    source = _regular_file(path)
    try:
        import torch as _torch

        checkpoint = _torch.load(
            source,
            map_location="cpu",
            weights_only=True,
        )
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        raise ValueError("checkpoint.pt 无法按 state_dict 安全读取") from error
    if (
        not isinstance(checkpoint, dict)
        or set(checkpoint) != {
            "model_state_dict",
            "class_names",
            "image_size",
            "seed",
            "optimizer_steps",
        }
        or not isinstance(checkpoint["model_state_dict"], dict)
        or not checkpoint["model_state_dict"]
        or checkpoint.get("class_names") != record["class_names"]
        or checkpoint.get("seed") != record["seed"]
        or checkpoint.get("optimizer_steps") != record["optimizer_steps"]
        or isinstance(checkpoint.get("image_size"), bool)
        or not isinstance(checkpoint.get("image_size"), int)
        or checkpoint["image_size"] <= 0
    ):
        raise ValueError("checkpoint.pt 与 run.json 身份不一致")
    state_dict = checkpoint["model_state_dict"]
    if not all(isinstance(value, _torch.Tensor) for value in state_dict.values()):
        raise ValueError("checkpoint state_dict 的每个值都必须是 Tensor")
    if not all(bool(_torch.isfinite(value).all()) for value in state_dict.values()):
        raise ValueError("checkpoint state_dict 含 NaN/Inf 非有限 Tensor")
    root = _Path(project).resolve(strict=True)
    _regular_file(root / "cls" / "model.py")
    _regular_file(root / "cls" / "contract.py")
    model = _build_checkpoint_model(root, len(record["class_names"]))
    try:
        model.load_state_dict(state_dict, strict=True)
    except (RuntimeError, TypeError, ValueError) as error:
        raise ValueError(
            "checkpoint state_dict 无法通过 build_model strict=True 校验"
        ) from error


def _validated_outputs(
    output: _Path,
    identity: dict[str, _Any],
    project: _Path,
) -> tuple[dict[str, _Any], dict[str, float]]:
    record = _read_json(output / "run.json")
    _validate_record(identity, record)
    metrics = _validate_metrics(_read_json(output / "metrics.json"))
    _validate_predictions(
        _read_json(output / "predictions.json"),
        record["class_names"],
        record["sample_count"],
    )
    _validate_checkpoint(output / "checkpoint.pt", record, project)
    _validate_raw_log(output, identity)
    return record, metrics


def _metadata_text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 4096
        or any(
            _unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        raise ValueError(f"{label} 必须是规范非空字符串")
    return value


def _source_uri(value: object) -> str:
    source = _metadata_text(value, "source_uri")
    if (
        "\\" in source
        or any(character.isspace() for character in source)
        or _re.fullmatch(
            r"[A-Za-z][A-Za-z0-9+.-]*:[^\s]+",
            source,
        )
        is None
        or source.split(":", 1)[0].lower() == "file"
        or _re.match(r"^[A-Za-z]:", source) is not None
    ):
        raise ValueError("source_uri 必须是非 file、本机路径无关的合法 URI")
    return source


def _dataset_manifest_sha256(
    project: _Path,
    data_root_value: str,
) -> str:
    data_root = _Path(data_root_value)
    if not data_root.is_absolute():
        data_root = _Path(project) / data_root
    if ".." in data_root.parts:
        raise ValueError("local_imagefolder data_root 不得包含 ..")
    data_root = data_root.absolute()
    for candidate in (data_root, *data_root.parents):
        try:
            candidate.lstat()
        except OSError as error:
            raise ValueError("local_imagefolder data_root 父路径不存在") from error
        if _is_link_or_reparse(candidate):
            raise ValueError(
                "local_imagefolder data_root 及父路径不得包含 link/reparse"
            )
    try:
        data_root = data_root.resolve(strict=True)
    except OSError as error:
        raise ValueError("local_imagefolder data_root 不存在") from error
    if _is_link_or_reparse(data_root) or not data_root.is_dir():
        raise ValueError("local_imagefolder data_root 必须是普通目录")

    files: list[dict[str, _Any]] = []

    def walk(directory: _Path, relative: _Path) -> None:
        try:
            with _os.scandir(directory) as iterator:
                entries = sorted(
                    iterator,
                    key=lambda entry: entry.name,
                )
        except OSError as error:
            raise ValueError("无法遍历 local_imagefolder 数据目录") from error
        for entry in entries:
            path = directory / entry.name
            child_relative = relative / entry.name
            try:
                metadata = path.lstat()
            except OSError as error:
                raise ValueError("数据文件在清单生成期间消失") from error
            if _is_link_or_reparse(path):
                raise ValueError("数据清单拒绝 link/reparse 路径")
            if _stat.S_ISDIR(metadata.st_mode):
                walk(path, child_relative)
                continue
            if not _stat.S_ISREG(metadata.st_mode):
                raise ValueError("数据清单只接受普通文件和普通目录")
            digest = _hashlib.sha256()
            try:
                with path.open("rb") as stream:
                    opened = _os.fstat(stream.fileno())
                    if not _same_file_identity(metadata, opened):
                        raise ValueError("数据文件在 open 前被替换")
                    while True:
                        block = stream.read(1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                    opened_after = _os.fstat(stream.fileno())
            except OSError as error:
                raise ValueError("无法读取数据文件生成清单") from error
            try:
                after = path.lstat()
            except OSError as error:
                raise ValueError("数据文件在清单生成期间消失") from error
            if (
                _is_link_or_reparse(path)
                or not _stat.S_ISREG(after.st_mode)
                or not _same_file_identity(metadata, opened_after)
                or not _same_file_identity(metadata, after)
            ):
                raise ValueError("数据文件在清单生成期间发生变化")
            files.append({
                "path": child_relative.as_posix(),
                "size": metadata.st_size,
                "sha256": "sha256:" + digest.hexdigest(),
            })
            if len(files) > _MAX_DATASET_FILES:
                raise ValueError("数据文件数量超过清单上限")

    for split in ("train", "val"):
        split_root = data_root / split
        if _is_link_or_reparse(split_root) or not split_root.is_dir():
            raise ValueError(f"local_imagefolder 缺少普通 {split} 目录")
        walk(split_root, _Path(split))
    if not files:
        raise ValueError("local_imagefolder 数据清单不能为空")
    manifest = {
        "schema": _DATASET_MANIFEST_SCHEMA,
        "files": files,
    }
    encoded = _json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + _hashlib.sha256(encoded).hexdigest()


def _same_file_identity(left: object, right: object) -> bool:
    required = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(
        getattr(left, field, None) != getattr(right, field, None)
        for field in required
    ):
        return False
    if _os.name == "nt":
        return True
    left_ctime = getattr(left, "st_ctime_ns", None)
    right_ctime = getattr(right, "st_ctime_ns", None)
    return (
        left_ctime is None
        or right_ctime is None
        or left_ctime == right_ctime
    )


def inspect(project: _Path) -> dict[str, _Any]:
    root = _Path(project)
    return {
        "project": root.name,
        "direction": "cls",
        "template_id": "PACK-CLS",
        "template_version": "1.0.0",
        "standard": {"debug_required": True},
        "baseline": {"metric": "top1_accuracy"},
        "synthetic": {
            "run_kind": "synthetic_debug_only",
            "paper_eligible": False,
        },
    }


def validate(project: _Path) -> list[str]:
    root = _Path(project)
    issues = []
    required = (
        "domain-pack.json",
        "configs/smoke.json",
        "configs/baseline.json",
        "contracts/metrics.v1.json",
        "cls/smoke.py",
        "cls/train.py",
    )
    for relative in required:
        if not (root / relative).is_file():
            issues.append(f"缺少分类模板文件：{relative}")
    try:
        import numpy as _numpy  # noqa: F401
        import torch as _torch  # noqa: F401
        from PIL import Image as _Image  # noqa: F401
    except ImportError:
        issues.append(
            "缺少 torch、numpy 或 Pillow；请按 "
            "requirements/windows-cpu.lock.txt 安装"
        )
    return issues


def prepare_runs(
    project: _Path,
    task: dict[str, _Any],
) -> list[dict[str, _Any]]:
    route_inputs = task.get("route_inputs")
    if not isinstance(route_inputs, dict):
        raise ValueError("Task 缺少 route_inputs")
    raw_config = route_inputs.get("config", {})
    if not isinstance(raw_config, dict):
        raise ValueError("Task route_inputs.config 必须是 JSON object")
    config = dict(raw_config)
    config.setdefault("mode", "synthetic_smoke")
    if "output_dir" in config:
        raise ValueError("Task config.output_dir 不能覆盖 Run 唯一输出目录")
    mode = config.get("mode")
    if mode not in _MODES:
        raise ValueError("Task config.mode 无效")
    if mode == "local_imagefolder":
        config.setdefault("device", "cuda")
    allowed = (
        {"mode"}
        if mode == "synthetic_smoke"
        else {
            "mode",
            "data_root",
            "dataset_id",
            "version",
            "source_uri",
            "device",
        }
    )
    if set(config) != allowed:
        raise ValueError(f"Task {mode} 配置字段必须精确为：{sorted(allowed)}")
    if mode == "local_imagefolder" and (
        not isinstance(config.get("data_root"), str)
        or not config["data_root"].strip()
    ):
        raise ValueError("local_imagefolder 需要 data_root")
    if mode == "local_imagefolder" and config.get("device") not in {
        "cpu",
        "cuda",
    }:
        raise ValueError("local_imagefolder device 只允许 cpu/cuda")
    config["metric_definition"] = dict(_METRIC_DEFINITION)
    dataset_identity: dict[str, _Any] | None = None
    if mode == "local_imagefolder":
        dataset_identity = {
            "schema": _DATASET_IDENTITY_SCHEMA,
            "dataset_id": _metadata_text(
                config.get("dataset_id"),
                "dataset_id",
            ),
            "version": _metadata_text(
                config.get("version"),
                "version",
            ),
            "source_uri": _source_uri(config.get("source_uri")),
            "manifest_sha256": _dataset_manifest_sha256(
                _Path(project),
                config["data_root"],
            ),
            "split": "train+val",
        }
    seed = route_inputs.get("seed", 7)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("Task seed 必须是非负整数")
    data = {
        "kind": (
            "synthetic_debug_only"
            if mode == "synthetic_smoke"
            else "local_imagefolder"
        ),
        "evaluation": {
            "schema": "pack-cls.evaluation.v1",
            "metric": "top1_accuracy",
            "split": (
                "synthetic"
                if mode == "synthetic_smoke"
                else "val"
            ),
        },
    }
    if dataset_identity is not None:
        data["dataset_identity"] = dataset_identity
    return [{
        "code": {"template_id": "PACK-CLS", "version": "1.0.0"},
        "config": config,
        "seed": seed,
        "data": data,
        "environment": {
            "backend": "project",
            "device": (
                "cpu"
                if mode == "synthetic_smoke"
                else config["device"]
            ),
        },
    }]


def execute(
    project: _Path,
    action: str,
    run: dict[str, _Any],
) -> dict[str, _Any]:
    identity = _identity(run)
    output = _output_dir(project, identity)
    if action == "status":
        if not _os.path.lexists(output):
            return {"status": "not_started"}
        if _is_link_or_reparse(output) or not output.is_dir():
            raise ValueError("Run 输出路径不是普通目录")
        if not (output / "run.json").is_file():
            return {"status": "failed"}
        record = _read_json(output / "run.json")
        _validate_record(identity, record)
        required = (
            output / "checkpoint.pt",
            output / "metrics.json",
            output / "predictions.json",
            output / "raw.log",
        )
        return {
            "status": (
                "finished"
                if all(path.is_file() and not _is_link_or_reparse(path) for path in required)
                else "failed"
            )
        }
    if action == "stop":
        return {"status": "stopped"}
    if action != "start":
        raise ValueError(f"不支持的 action：{action}")
    if _os.path.lexists(output):
        raise FileExistsError(f"Run 输出目录已存在，拒绝覆盖：{output}")
    _require_device_available(identity["device"])
    _output_root(project)
    config = identity["config"]
    staging: _Path | None = None
    execution_output = output
    timeout: float | None = 30
    if identity["mode"] == "synthetic_smoke":
        command = [
            _sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "cls.smoke",
            "--work-dir",
            str(output),
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
        staging = _create_local_staging(project, identity)
        execution_output = staging
        timeout = None
        data_root = _Path(config["data_root"])
        if not data_root.is_absolute():
            data_root = _Path(project) / data_root
        command = [
            _sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "cls.train",
            "--config",
            "configs/baseline.json",
            "--data-root",
            str(data_root),
            "--output-dir",
            str(execution_output),
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
        result = _subprocess.run(
            command,
            cwd=_Path(project),
            env=_safe_environment(identity["seed"]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "分类运行失败："
                f"exit={result.returncode} stderr={result.stderr[-2000:]}"
            )
        _write_raw_log(execution_output, identity)
        _validated_outputs(
            execution_output,
            identity,
            _Path(project).resolve(strict=True),
        )
        if staging is not None:
            if _os.path.lexists(output):
                raise FileExistsError(
                    f"Run 输出目录在发布前已出现，拒绝覆盖：{output}"
            )
            _os.rename(execution_output, output)
            staging = None
        record = _read_json(output / "run.json")
        _validate_record(identity, record)
        return {"status": "finished", "process_id": None}
    except BaseException as error:
        if staging is not None:
            raise RuntimeError(
                "分类运行失败；为避免递归删除和 TOCTOU 风险，"
                f"本次随机 staging 已原样保留：{staging}；原因：{error}"
            ) from error
        raise


def parse_result(
    project: _Path,
    run: dict[str, _Any],
) -> dict[str, _Any]:
    identity = _identity(run)
    output = _output_dir(project, identity)
    root = _Path(project).resolve(strict=True)
    _, metrics = _validated_outputs(output, identity, root)
    relative_output = output.relative_to(root)
    synthetic = identity["mode"] == "synthetic_smoke"
    limitations = (
        ["合成数据只验证代码链路，不能证明科研方法有效"]
        if synthetic
        else ["本地基线仍需外层工作流冻结代码、数据和环境身份后才能晋级"]
    )
    return {
        "execution": {
            "outcome": "succeeded",
            "exit_code": 0,
            "issue_kind": None,
        },
        "result": {
            "metrics": metrics,
            "raw_log": (relative_output / "raw.log").as_posix(),
        },
        "quality": {
            "implementation": "valid",
            "interface": "valid",
            "data": "valid",
            "metrics": "valid",
        },
        "analysis": {
            "hypothesis": "inconclusive",
            "limitations": limitations,
            "suggestions": [],
        },
        "artifacts": [
            (relative_output / name).as_posix()
            for name in (
                "checkpoint.pt",
                "metrics.json",
                "predictions.json",
                "run.json",
            )
        ],
    }
