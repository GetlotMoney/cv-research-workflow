from __future__ import annotations

import hashlib as _hashlib
import json as _json
import os as _os
import re as _re
import stat as _stat
import subprocess as _subprocess
import sys as _sys
import uuid as _uuid
from pathlib import Path as _Path
from typing import Any as _Any
from urllib.parse import urlsplit as _urlsplit


_RUN_ID = _re.compile(r"RUN-[0-9]{4}")
_SHA256 = _re.compile(r"[0-9a-f]{64}")
_TOKEN = _re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
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
    "psnr_rgb_x2": (
        "RGB PSNR in dB for 2x super-resolution, computed from cumulative "
        "squared error over all uint8 RGB values without border shaving; "
        "higher is better and it is not directly comparable to standard "
        "Y-channel border-shaved PSNR."
    ),
}
_MAX_FILE_SIZE = 16 * 1024 * 1024


def _is_link_or_reparse(path: _Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _directory(path: _Path, label: str) -> _Path:
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


def _stable(metadata: _os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _safe_read(path: _Path, label: str, *, root: _Path) -> bytes:
    safe_root = _directory(root, f"{label} 根")
    candidate = _Path(_os.path.abspath(_os.fspath(path)))
    try:
        candidate.relative_to(safe_root)
    except ValueError as error:
        raise ValueError(f"{label} 逃逸根目录") from error
    for current in (candidate, *candidate.parents):
        if _os.path.lexists(current) and _is_link_or_reparse(current):
            raise ValueError(f"{label} 及父路径不能含 link/junction/reparse")
    try:
        before = candidate.lstat()
    except OSError as error:
        raise ValueError(f"{label} 不存在") from error
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
    descriptor = _os.open(candidate, flags)
    try:
        opened_before = _os.fstat(descriptor)
        if _stable(opened_before) != _stable(before):
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
        _stable(opened_before) != _stable(before)
        or _stable(opened_after) != _stable(before)
        or _stable(after) != _stable(before)
        or opened_before.st_ctime_ns != opened_after.st_ctime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or _is_link_or_reparse(candidate)
    ):
        raise ValueError(f"{label} 在读取过程中被替换或修改")
    return b"".join(chunks)


def _read_json(path: _Path, label: str, *, root: _Path) -> dict[str, _Any]:
    try:
        payload = _json.loads(
            _safe_read(path, label, root=root).decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"非有限值：{value}")
            ),
        )
    except (UnicodeError, _json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} 不是严格 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} 必须是 object")
    return payload


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


def _safe_token(value: object, label: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise ValueError(f"{label} 必须是安全稳定标识")
    return value


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
        raise ValueError("source_uri 不能是 file/local 路径")
    return value


def _data_identity(config: dict[str, _Any]) -> dict[str, _Any]:
    return {
        "schema": "cv-experiment-workflow.dataset-identity.v1",
        "dataset_id": config["dataset_id"],
        "version": config["version"],
        "source_uri": config["source_uri"],
        "manifest_sha256": config["manifest_sha256"],
        "split": {"train": "train", "evaluation": "val"},
    }


def _validate_identity(value: object) -> dict[str, _Any]:
    if not isinstance(value, dict) or set(value) != _IDENTITY_FIELDS:
        raise ValueError("data.identity 字段无效")
    if value.get("schema") != "cv-experiment-workflow.dataset-identity.v1":
        raise ValueError("data.identity schema 无效")
    _safe_token(value.get("dataset_id"), "dataset_id")
    _safe_token(value.get("version"), "version")
    _https_uri(value.get("source_uri"))
    if (
        not isinstance(value.get("manifest_sha256"), str)
        or _SHA256.fullmatch(value["manifest_sha256"]) is None
    ):
        raise ValueError("manifest_sha256 格式无效")
    if value.get("split") != {"train": "train", "evaluation": "val"}:
        raise ValueError("SR split 必须固定为 train/val")
    return dict(value)


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
    if not isinstance(value, dict) or value != _central_data_identity(config):
        raise ValueError("SR 中央 dataset_identity 身份与冻结配置冲突")
    return _data_identity(config)


def _config(
    value: object,
    *,
    require_metric_definition: bool = True,
) -> dict[str, _Any]:
    if not isinstance(value, dict):
        raise ValueError("frozen.config 必须是 object")
    config = dict(value)
    mode = config.get("mode")
    if mode == "local_sr_x2":
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
        if mode == "local_sr_x2"
        else set()
    )
    if mode == "local_sr_x2" and require_metric_definition:
        expected.add("metric_definition")
    if not expected or set(config) != expected:
        raise ValueError("config.mode/字段必须精确匹配 synthetic_smoke 或 local_sr_x2")
    _canonical_sha256(config)
    if mode == "local_sr_x2":
        if config.get("device") not in {"cpu", "cuda"}:
            raise ValueError("local_sr_x2 device 只允许 cpu/cuda")
        data_root = config.get("data_root")
        if (
            not isinstance(data_root, str)
            or not data_root
            or data_root != data_root.strip()
            or any(ord(character) < 32 for character in data_root)
        ):
            raise ValueError("local_sr_x2 必须明确 data_root")
        _safe_token(config.get("dataset_id"), "dataset_id")
        _safe_token(config.get("version"), "version")
        _https_uri(config.get("source_uri"))
        if (
            not isinstance(config.get("manifest_sha256"), str)
            or _SHA256.fullmatch(config["manifest_sha256"]) is None
        ):
            raise ValueError("manifest_sha256 格式无效")
        if (
            require_metric_definition
            and config.get("metric_definition")
            != _FORMAL_METRIC_DEFINITION
        ):
            raise ValueError(
                "config.metric_definition 与超分模板固定指标口径不一致"
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
        raise ValueError("frozen.code 中央声明与 PACK-SR-V1.0.0 不一致")
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


def _run_identity(run: dict[str, _Any]) -> dict[str, _Any]:
    run_id = run.get("id")
    purpose = run.get("purpose")
    frozen = run.get("frozen")
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("Run id 必须是 RUN-0001")
    if purpose not in {"debug", "evidence"}:
        raise ValueError("Run purpose 只允许 debug/evidence")
    if not isinstance(frozen, dict) or set(frozen) != _FROZEN_FIELDS:
        raise ValueError("frozen 必须精确包含 code/config/seed/data/environment")
    expected_code = {"template_id": "PACK-SR", "version": "1.0.0"}
    seed = frozen.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("frozen.seed 必须是非负整数")
    config = _config(frozen.get("config"))
    device = "cpu" if config["mode"] == "synthetic_smoke" else config["device"]
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
        if data != {
            "kind": "synthetic_debug_only",
            "evaluation": {
                "metric": "debug_psnr_rgb_x2",
                "scale": 2,
                "color_space": "full_rgb_uint8",
                "aggregation": "cumulative_sse_over_all_values",
                "protocol": "custom_full_rgb_no_shave",
                "comparability": (
                    "not_comparable_to_standard_y_channel_border_shave_"
                    "without_recalculation"
                ),
            },
        }:
            raise ValueError("合成 data/evaluation 被篡改")
        if purpose != "debug":
            raise ValueError("合成 Run 只能是 debug")
        identity = {"kind": "synthetic_debug_only"}
    else:
        if not bound and set(data) == {"identity", "evaluation"}:
            identity = _validate_identity(data.get("identity"))
            if identity != _data_identity(config):
                raise ValueError("config 与 data.identity 身份冲突")
        elif (
            set(data) == {"kind", "dataset_identity", "evaluation"}
            and data.get("kind") == "local_dataset"
        ):
            identity = _validated_central_data_identity(
                data.get("dataset_identity"),
                config,
            )
        else:
            raise ValueError(
                "正式 data 必须使用 local_dataset + dataset_identity"
            )
        if data.get("evaluation") != {
            "metric": "psnr_rgb_x2",
            "scale": 2,
            "color_space": "full_rgb_uint8",
            "aggregation": "cumulative_sse_over_all_values",
            "shave_border": False,
            "perfect_match": "error_no_infinity",
            "protocol": "custom_full_rgb_no_shave",
            "comparability": (
                "not_comparable_to_standard_y_channel_border_shave_"
                "without_recalculation"
            ),
        }:
            raise ValueError("正式 SR evaluation 口径被篡改")
    return {
        "run_id": run_id,
        "purpose": purpose,
        "seed": seed,
        "mode": config["mode"],
        "device": device,
        "config": config,
        "config_sha256": _canonical_sha256(config),
        "data_identity": identity,
    }


def _output(project: _Path, run_id: str) -> _Path:
    root = _directory(project, "项目根")
    target = root / ".cv-workflow-output" / run_id
    target.relative_to(root)
    return target


def _staging(project: _Path, run_id: str) -> _Path:
    root = _directory(project, "项目根")
    workflow = root / ".cv-workflow-output"
    if _os.path.lexists(workflow):
        _directory(workflow, "Run 输出父目录")
    else:
        workflow.mkdir()
        _directory(workflow, "新建 Run 输出父目录")
    staging = workflow / f".{run_id}.staging-{_uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    return _directory(staging, "Run staging")


def _environment(seed: int) -> dict[str, str]:
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
    result = {key: _os.environ[key] for key in allowed if key in _os.environ}
    result.update(
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
    return result


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


def _validate_output(
    project: _Path,
    output: _Path,
    identity: dict[str, _Any],
) -> tuple[dict[str, _Any], dict[str, float], list[str]]:
    command = [
        _sys.executable,
        "-B",
        "-X",
        "utf8",
        "-m",
        "sr.validate_output",
        "--output-dir",
        str(output),
    ]
    completed = _subprocess.run(
        command,
        cwd=project,
        env=_environment(identity["seed"]),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30.0,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            "SR 输出校验失败："
            f"exit={completed.returncode} stderr={completed.stderr[-2000:]}"
        )
    try:
        summary = _json.loads(completed.stdout)
    except _json.JSONDecodeError as error:
        raise ValueError("SR 输出校验器没有返回严格 JSON") from error
    if (
        not isinstance(summary, dict)
        or set(summary) != {"metrics", "artifacts"}
        or not isinstance(summary["metrics"], dict)
        or not isinstance(summary["artifacts"], list)
    ):
        raise ValueError("SR 输出校验摘要字段无效")
    record = _read_json(output / "run.json", "run.json", root=output)
    expected = {
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
        "dataset_identity": identity["data_identity"],
        "scale": 2,
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise ValueError(f"run.json {key} 与冻结身份冲突")
    return record, dict(summary["metrics"]), list(summary["artifacts"])


def inspect(project: _Path) -> dict[str, _Any]:
    return {
        "project": _Path(project).name,
        "direction": "sr",
        "template_id": "PACK-SR",
        "template_version": "1.0.0",
        "standard": {"debug_required": True},
        "baseline": {"metric": "psnr_rgb_x2"},
        "protocol": "custom_full_rgb_no_shave",
        "comparable_to_standard": False,
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
        "sr/contract.py",
        "sr/data.py",
        "sr/model.py",
        "sr/train.py",
        "sr/evaluate.py",
        "sr/infer.py",
        "sr/smoke.py",
        "sr/validate_output.py",
    ):
        if not (root / relative).is_file():
            issues.append(f"缺少超分模板文件：{relative}")
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
        raise ValueError("route_inputs 只允许 config/seed")
    config = _config(
        route_inputs.get("config", {"mode": "synthetic_smoke"}),
        require_metric_definition=False,
    )
    if config["mode"] == "local_sr_x2":
        config["metric_definition"] = dict(_FORMAL_METRIC_DEFINITION)
    seed = route_inputs.get("seed", 7)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed 必须是非负整数")
    if config["mode"] == "synthetic_smoke":
        data = {
            "kind": "synthetic_debug_only",
            "evaluation": {
                "metric": "debug_psnr_rgb_x2",
                "scale": 2,
                "color_space": "full_rgb_uint8",
                "aggregation": "cumulative_sse_over_all_values",
                "protocol": "custom_full_rgb_no_shave",
                "comparability": (
                    "not_comparable_to_standard_y_channel_border_shave_"
                    "without_recalculation"
                ),
            },
        }
    else:
        data = {
            "kind": "local_dataset",
            "dataset_identity": _central_data_identity(config),
            "evaluation": {
                "metric": "psnr_rgb_x2",
                "scale": 2,
                "color_space": "full_rgb_uint8",
                "aggregation": "cumulative_sse_over_all_values",
                "shave_border": False,
                "perfect_match": "error_no_infinity",
                "protocol": "custom_full_rgb_no_shave",
                "comparability": (
                    "not_comparable_to_standard_y_channel_border_shave_"
                    "without_recalculation"
                ),
            },
        }
    return [
        {
            "code": {"template_id": "PACK-SR", "version": "1.0.0"},
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
    identity = _run_identity(run)
    root = _directory(project, "项目根")
    output = _output(root, identity["run_id"])
    if action == "status":
        if not _os.path.lexists(output):
            return {"status": "not_started"}
        try:
            _validate_output(root, output, identity)
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
    staging = _staging(root, identity["run_id"])
    result_dir = staging / "result"
    config = identity["config"]
    if identity["mode"] == "synthetic_smoke":
        command = [
            _sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "sr.smoke",
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
            "sr.train",
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
            env=_environment(identity["seed"]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90.0,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "PACK-SR 子进程失败："
                f"exit={completed.returncode} stderr={completed.stderr[-2000:]}"
            )
        _validate_output(root, result_dir, identity)
        if _os.path.lexists(output):
            raise FileExistsError("发布前 Run 输出被占用，拒绝覆盖")
        _os.rename(result_dir, output)
        _validate_output(root, output, identity)
        return {"status": "finished", "process_id": None}
    except BaseException as error:
        raise RuntimeError(
            "PACK-SR 运行失败；未递归清理目录，"
            f"staging 保留在 {staging}；原因：{error}"
        ) from error


def parse_result(
    project: _Path,
    run: dict[str, _Any],
) -> dict[str, _Any]:
    identity = _run_identity(run)
    root = _directory(project, "项目根")
    output = _output(root, identity["run_id"])
    _, metrics, listed = _validate_output(root, output, identity)
    relative = output.relative_to(root)
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
            "protocol": "custom_full_rgb_no_shave",
            "comparable_to_standard": False,
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
                ["合成超分数据只证明链路可运行，不能作为论文成绩"]
                if synthetic
                else ["仍需外层工作流冻结 Git、环境和人工复核后才能晋级"]
            ),
            "suggestions": [],
        },
        "artifacts": [
            (relative / name).as_posix()
            for name in [*listed, "artifacts.json"]
        ],
    }
