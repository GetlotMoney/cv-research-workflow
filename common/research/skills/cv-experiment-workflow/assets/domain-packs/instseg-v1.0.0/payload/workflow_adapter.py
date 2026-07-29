from __future__ import annotations

import hashlib as _hashlib
import importlib.metadata as _metadata
import importlib.util as _importlib_util
import json as _json
import os as _os
import re as _re
import stat as _stat
import subprocess as _subprocess
import sys as _sys
import types as _types
import uuid as _uuid
from pathlib import Path as _Path
from typing import Any as _Any


_RUN_ID = _re.compile(r"RUN-[0-9]{4}")
_SHA256 = _re.compile(r"sha256:[0-9a-f]{64}")
_REPARSE_POINT = 0x400
_BOUND_CODE_SCHEMA = "cv-experiment-workflow.bound-code.v2"
_DECLARED_ENVIRONMENT = {"backend": "project", "device": "cpu"}
_SYNTHETIC_EVALUATION = {
    "schema": "pack-instseg.evaluation-contract.v1",
    "backend": "debug_mask_metrics",
    "iou_type": "segm",
    "metrics": [
        "debug_mask_mean_iou",
        "debug_precision_at_mask_iou_0_5",
    ],
}
_FORMAL_EVALUATION = {
    "schema": "pack-instseg.evaluation-contract.v1",
    "backend": "pycocotools==2.0.11",
    "iou_type": "segm",
    "metrics": ["coco_segm_ap", "coco_segm_ap50", "coco_segm_ap75"],
}
_FORMAL_METRIC_DEFINITION = {
    "coco_segm_ap": (
        "COCO instance-segmentation AP averaged over IoU thresholds "
        "0.50:0.05:0.95, area=all and maxDets=100; range 0 to 1, "
        "higher is better."
    ),
    "coco_segm_ap50": (
        "COCO instance-segmentation AP at IoU=0.50, area=all and "
        "maxDets=100; range 0 to 1, higher is better."
    ),
    "coco_segm_ap75": (
        "COCO instance-segmentation AP at IoU=0.75, area=all and "
        "maxDets=100; range 0 to 1, higher is better."
    ),
}
_CUDA_PROBE_CODE = (
    "import torch\n"
    "if not torch.cuda.is_available():\n"
    "    raise RuntimeError('CUDA unavailable')\n"
    "left = torch.ones((2, 2), device='cuda')\n"
    "right = torch.ones((2, 2), device='cuda')\n"
    "result = torch.mm(left, right)\n"
    "torch.cuda.synchronize()\n"
    "if tuple(result.shape) != (2, 2):\n"
    "    raise RuntimeError('CUDA mm returned an invalid shape')\n"
)


def _runtime(project: _Path) -> _types.ModuleType:
    root = _Path(project).resolve(strict=True)
    source = root / "src" / "instseg" / "runtime.py"
    _regular_file(source, "PACK-INSTSEG runtime")
    name = f"_pack_instseg_runtime_{_uuid.uuid4().hex}"
    spec = _importlib_util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise ValueError("无法加载 PACK-INSTSEG runtime")
    module = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_link_or_reparse(path: _Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _regular_file(path: _Path, label: str) -> _Path:
    candidate = _Path(_os.path.abspath(_os.fspath(path)))
    for current in (candidate, *candidate.parents):
        if _os.path.lexists(current) and _is_link_or_reparse(current):
            raise ValueError(f"{label} 及父路径不能包含 link/reparse")
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise ValueError(f"{label} 不存在") from error
    if not _stat.S_ISREG(metadata.st_mode) or _is_link_or_reparse(candidate):
        raise ValueError(f"{label} 必须是普通文件")
    return candidate


def _canonical_sha(payload: object) -> str:
    encoded = _json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{_hashlib.sha256(encoded).hexdigest()}"


def _validated_config(
    config: object,
    seed: object,
    *,
    require_metric_definition: bool = True,
) -> tuple[dict[str, _Any], str]:
    if not isinstance(config, dict):
        raise ValueError("Run frozen.config 必须是 object")
    normalized = dict(config)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("Run seed 必须是非负整数")
    mode = normalized.get("mode")
    if mode == "synthetic_debug":
        if set(normalized) != {"mode"}:
            raise ValueError("synthetic_debug config 只能包含 mode")
    elif mode == "local_coco":
        normalized.setdefault("device", "cuda")
        required = {
            "mode",
            "data_root",
            "annotation_file",
            "dataset_id",
            "version",
            "source_uri",
            "split",
            "device",
        }
        if require_metric_definition:
            required.add("metric_definition")
        if set(normalized) != required:
            raise ValueError(
                "local_coco config 必须精确包含正式数据字段，"
                "device 缺省时默认使用 cuda"
            )
        if normalized.get("device") not in {"cpu", "cuda"}:
            raise ValueError("local_coco device 只允许 cpu 或 cuda")
        if (
            require_metric_definition
            and normalized.get("metric_definition")
            != _FORMAL_METRIC_DEFINITION
        ):
            raise ValueError(
                "config.metric_definition 与实例分割模板固定指标口径不一致"
            )
    else:
        raise ValueError("Run mode 无效")
    return normalized, mode


def _bound_contract(
    frozen: dict[str, _Any],
    purpose: str,
    expected_code: dict[str, str],
) -> bool:
    code = frozen.get("code")
    environment = frozen.get("environment")
    if code == expected_code:
        if not isinstance(environment, dict):
            raise ValueError("Run frozen.environment 必须是 object")
        return False
    if (
        not isinstance(code, dict)
        or code.get("schema") != _BOUND_CODE_SCHEMA
        or code.get("declared_code") != expected_code
    ):
        raise ValueError("Run frozen.code 中央声明与 PACK-INSTSEG V1 不一致")
    if not isinstance(environment, dict):
        raise ValueError("Run frozen.environment 中央声明无效")
    clean_required = code.get("clean_required")
    worktree_clean = code.get("worktree_clean")
    if (
        type(clean_required) is not bool
        or type(worktree_clean) is not bool
        or clean_required != (purpose == "evidence")
        or (clean_required and not worktree_clean)
    ):
        raise ValueError("Run frozen.code 中央 clean 状态与 purpose 不一致")
    return True


def _device_for(config: dict[str, _Any], mode: str) -> str:
    device = "cpu" if mode == "synthetic_debug" else config.get("device", "cuda")
    if device not in {"cpu", "cuda"}:
        raise ValueError("device 只允许 cpu 或 cuda")
    return str(device)


def _validate_environment(
    frozen: dict[str, _Any],
    *,
    bound: bool,
    device: str,
) -> None:
    expected = {"backend": "project", "device": device}
    environment = frozen.get("environment")
    observed = (
        environment.get("declared_environment")
        if bound and isinstance(environment, dict)
        else environment
    )
    if observed != expected:
        raise ValueError("Run frozen.environment 与实际 device 不一致")


def _declared_data(
    frozen: dict[str, _Any],
    *,
    bound: bool,
    purpose: str,
    real_experiment: bool,
) -> dict[str, _Any]:
    value = frozen.get("data")
    if not isinstance(value, dict):
        raise ValueError("Run frozen.data 必须是 object")
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
        raise ValueError("Run ID 必须是 RUN-0001")
    frozen = run.get("frozen")
    if not isinstance(frozen, dict):
        raise ValueError("Run 缺少 frozen")
    expected_frozen = {"code", "config", "seed", "data", "environment"}
    if set(frozen) != expected_frozen:
        raise ValueError(f"Run frozen 字段必须精确为 {sorted(expected_frozen)}")
    purpose = run.get("purpose")
    if purpose not in {"debug", "evidence"}:
        raise ValueError("Run purpose 只允许 debug/evidence")
    expected_code = {"template_id": "PACK-INSTSEG", "version": "1.0.0"}
    bound = _bound_contract(frozen, purpose, expected_code)
    seed = frozen["seed"]
    config, mode = _validated_config(frozen["config"], seed)
    device = _device_for(config, mode)
    _validate_environment(frozen, bound=bound, device=device)
    frozen_data = _declared_data(
        frozen,
        bound=bound,
        purpose=purpose,
        real_experiment=mode != "synthetic_debug",
    )
    expected_manifest_sha256 = None
    if mode == "synthetic_debug":
        if (
            set(frozen_data) != {"kind", "evaluation"}
            or frozen_data.get("kind") != "synthetic_debug_only"
            or frozen_data.get("evaluation") != _SYNTHETIC_EVALUATION
        ):
            raise ValueError("合成 Run frozen.data 身份无效")
    else:
        if (
            set(frozen_data) != {"kind", "dataset_identity", "evaluation"}
            or frozen_data.get("kind")
            not in (
                {"local_dataset"}
                if bound
                else {"local_dataset", "local_coco"}
            )
            or not isinstance(frozen_data.get("dataset_identity"), dict)
            or frozen_data.get("evaluation") != _FORMAL_EVALUATION
        ):
            raise ValueError("local_coco Run frozen.data 身份无效")
        dataset_identity = frozen_data["dataset_identity"]
        if (
            set(dataset_identity)
            != {
                "schema",
                "dataset_id",
                "version",
                "source_uri",
                "manifest_sha256",
                "split",
            }
            or dataset_identity.get("schema")
            != "cv-experiment-workflow.dataset-identity.v1"
            or _SHA256.fullmatch(
                str(dataset_identity.get("manifest_sha256", ""))
            )
            is None
        ):
            raise ValueError("Run frozen.data dataset_identity 无效")
        if bound and any(
            dataset_identity.get(field) != config[field]
            for field in ("dataset_id", "version", "source_uri", "split")
        ):
            raise ValueError(
                "bound Run frozen.data dataset_identity 与 config 身份冲突"
            )
        expected_manifest_sha256 = dataset_identity["manifest_sha256"]
    return {
        "run_id": run_id,
        "purpose": purpose,
        "mode": mode,
        "config": dict(config),
        "config_sha256": _canonical_sha(config),
        "seed": seed,
        "device": device,
        "frozen_data": frozen_data,
        "expected_manifest_sha256": expected_manifest_sha256,
    }


def _output(project: _Path, identity: dict[str, _Any]) -> _Path:
    root = _Path(project).resolve(strict=True)
    target = root / ".cv-workflow-output" / identity["run_id"]
    target.relative_to(root)
    return target


def _plain_directory(path: _Path, label: str) -> None:
    metadata = path.lstat()
    if not _stat.S_ISDIR(metadata.st_mode) or _is_link_or_reparse(path):
        raise ValueError(f"{label} 必须是普通目录")


def _staging(project: _Path, identity: dict[str, _Any]) -> _Path:
    root = _Path(project).resolve(strict=True)
    workflow = root / ".cv-workflow-output"
    if _os.path.lexists(workflow):
        _plain_directory(workflow, "Run 输出父目录")
    else:
        workflow.mkdir()
        _plain_directory(workflow, "Run 输出父目录")
    for _ in range(8):
        candidate = workflow / (
            f".{identity['run_id']}.staging-{_uuid.uuid4().hex}"
        )
        if not _os.path.lexists(candidate):
            return candidate
    raise RuntimeError("无法分配唯一 Run staging")


def _safe_environment(seed: int = 0) -> dict[str, str]:
    environment = {
        "PYTHONHASHSEED": str(seed),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
    }
    for key in (
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERNAME",
        "USERDOMAIN",
    ):
        value = _os.environ.get(key)
        if value:
            environment[key] = value
    if "CUDA_VISIBLE_DEVICES" in _os.environ:
        environment["CUDA_VISIBLE_DEVICES"] = _os.environ[
            "CUDA_VISIBLE_DEVICES"
        ]
    return environment


def _probe_cuda(device: str, *, seed: int) -> None:
    if device != "cuda":
        return
    completed = _subprocess.run(
        [
            _sys.executable,
            "-B",
            "-X",
            "utf8",
            "-c",
            _CUDA_PROBE_CODE,
        ],
        env=_safe_environment(seed),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30.0,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "CUDA probe 失败：当前 PyTorch/CUDA 无法执行矩阵乘法；"
            f"exit={completed.returncode} stderr={completed.stderr[-1000:]}"
        )


def _resolve_local_path(project: _Path, value: object, label: str) -> _Path:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} 必须是规范路径文本")
    candidate = _Path(value)
    if not candidate.is_absolute():
        candidate = _Path(project) / candidate
    return candidate


def _bind_current_dataset(
    project: _Path,
    identity: dict[str, _Any],
    runtime: _types.ModuleType,
) -> dict[str, str]:
    config = identity["config"]
    observed = runtime.dataset_identity(
        data_root=_resolve_local_path(project, config["data_root"], "data_root"),
        annotation_file=_resolve_local_path(
            project,
            config["annotation_file"],
            "annotation_file",
        ),
        dataset_id=config["dataset_id"],
        version=config["version"],
        source_uri=config["source_uri"],
        split=config["split"],
    )
    frozen_data = identity.get("frozen_data")
    if (
        isinstance(frozen_data, dict)
        and isinstance(frozen_data.get("dataset_identity"), dict)
        and observed != frozen_data["dataset_identity"]
    ):
        raise ValueError("当前 data manifest 与冻结数据身份不一致")
    identity["expected_manifest_sha256"] = observed["manifest_sha256"]
    return observed


def inspect(project: _Path) -> dict[str, _Any]:
    return {
        "project": _Path(project).name,
        "direction": "instseg",
        "template_id": "PACK-INSTSEG",
        "template_version": "1.0.0",
        "standard": {"debug_required": True},
        "model": "tiny_fixed_query_instance_segmenter",
        "synthetic": {
            "run_kind": "synthetic_debug_only",
            "paper_eligible": False,
        },
        "formal_evaluator": "pycocotools==2.0.11",
        "segmentation_contract": "polygon_only_no_crowd",
    }


def validate(project: _Path) -> list[str]:
    root = _Path(project)
    issues: list[str] = []
    for relative in (
        "domain-pack.json",
        "configs/smoke.json",
        "configs/baseline.json",
        "src/instseg/runtime.py",
        "src/instseg/train.py",
        "src/instseg/evaluate.py",
        "src/instseg/infer.py",
    ):
        if not (root / relative).is_file():
            issues.append(f"缺少实例分割模板文件：{relative}")
    try:
        import numpy as _numpy  # noqa: F401
        import torch as _torch  # noqa: F401
        from PIL import Image as _Image  # noqa: F401
    except ImportError:
        issues.append(
            "缺少 torch、numpy 或 Pillow；请按 "
            "requirements/windows-cpu.lock.txt 安装"
        )
    try:
        version = _metadata.version("pycocotools")
    except _metadata.PackageNotFoundError:
        version = None
    if version != "2.0.11":
        issues.append(
            "OPTIONAL_NOT_INSTALLED：正式 COCO segm 评估需要 "
            "pycocotools==2.0.11；合成调试仍可运行"
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
        raise ValueError("Task route_inputs.config 必须是 object")
    config = dict(raw_config)
    config.setdefault("mode", "synthetic_debug")
    if "output_dir" in config:
        raise ValueError("Task 不能覆盖 Run 唯一输出目录")
    seed = route_inputs.get("seed", 23)
    config, mode = _validated_config(
        config,
        seed,
        require_metric_definition=False,
    )
    if mode == "local_coco":
        config["metric_definition"] = dict(_FORMAL_METRIC_DEFINITION)
    if mode == "synthetic_debug":
        data: dict[str, _Any] = {
            "kind": "synthetic_debug_only",
            "evaluation": dict(_SYNTHETIC_EVALUATION),
        }
    else:
        runtime = _runtime(project)
        bound_identity = runtime.dataset_identity(
            data_root=_resolve_local_path(
                project,
                config["data_root"],
                "data_root",
            ),
            annotation_file=_resolve_local_path(
                project,
                config["annotation_file"],
                "annotation_file",
            ),
            dataset_id=config["dataset_id"],
            version=config["version"],
            source_uri=config["source_uri"],
            split=config["split"],
        )
        data = {
            "kind": "local_dataset",
            "dataset_identity": bound_identity,
            "evaluation": dict(_FORMAL_EVALUATION),
        }
    return [{
        "code": {"template_id": "PACK-INSTSEG", "version": "1.0.0"},
        "config": config,
        "seed": seed,
        "data": data,
        "environment": {
            "backend": "project",
            "device": _device_for(config, mode),
        },
    }]


def execute(
    project: _Path,
    action: str,
    run: dict[str, _Any],
) -> dict[str, _Any]:
    identity = _identity(run)
    final = _output(project, identity)
    runtime = _runtime(project)
    if action == "status":
        if not _os.path.lexists(final):
            return {"status": "not_started"}
        _plain_directory(final, "Run 输出")
        runtime.validate_workflow_output(
            final,
            mode=identity["mode"],
            run_id=identity["run_id"],
            config_sha256=identity["config_sha256"],
            seed=identity["seed"],
            expected_data_manifest_sha256=identity["expected_manifest_sha256"],
            expected_device=identity["device"],
        )
        return {"status": "finished"}
    if action == "stop":
        return {"status": "stopped"}
    if action != "start":
        raise ValueError(f"不支持的 action：{action}")
    if identity["mode"] == "synthetic_debug" and identity["purpose"] == "evidence":
        raise ValueError("合成调试不能作为 evidence 或论文成绩")
    runtime.require_device(
        identity["device"],
        synthetic=identity["mode"] == "synthetic_debug",
    )
    _probe_cuda(identity["device"], seed=identity["seed"])
    if _os.path.lexists(final):
        raise FileExistsError(f"Run 输出已存在，拒绝覆盖：{final}")
    if identity["mode"] == "local_coco":
        _bind_current_dataset(project, identity, runtime)
        runtime.require_pycocotools()
    staging = _staging(project, identity)
    command = [
        _sys.executable,
        "-B",
        "-X",
        "utf8",
        "-m",
        "src.instseg.train",
        "--config",
        (
            "configs/smoke.json"
            if identity["mode"] == "synthetic_debug"
            else "configs/baseline.json"
        ),
        "--mode",
        identity["mode"],
        "--output-dir",
        str(staging),
        "--seed",
        str(identity["seed"]),
        "--run-id",
        identity["run_id"],
        "--config-sha256",
        identity["config_sha256"],
        "--device",
        identity["device"],
    ]
    if identity["mode"] == "local_coco":
        command.extend([
            "--data-root",
            str(
                _resolve_local_path(
                    project,
                    identity["config"]["data_root"],
                    "data_root",
                )
            ),
            "--annotations",
            str(
                _resolve_local_path(
                    project,
                    identity["config"]["annotation_file"],
                    "annotation_file",
                )
            ),
        ])
    try:
        result = _subprocess.run(
            command,
            cwd=_Path(project),
            env=_safe_environment(identity["seed"]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60 if identity["mode"] == "synthetic_debug" else 300,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "PACK-INSTSEG 运行失败："
                f"exit={result.returncode} stderr={result.stderr[-2000:]}"
            )
        runtime.validate_workflow_output(
            staging,
            mode=identity["mode"],
            run_id=identity["run_id"],
            config_sha256=identity["config_sha256"],
            seed=identity["seed"],
            expected_data_manifest_sha256=identity["expected_manifest_sha256"],
            expected_device=identity["device"],
        )
        if _os.path.lexists(final):
            raise FileExistsError("Run 最终输出在发布前出现，拒绝覆盖")
        _os.rename(staging, final)
        runtime.validate_workflow_output(
            final,
            mode=identity["mode"],
            run_id=identity["run_id"],
            config_sha256=identity["config_sha256"],
            seed=identity["seed"],
            expected_data_manifest_sha256=identity["expected_manifest_sha256"],
            expected_device=identity["device"],
        )
    except BaseException as error:
        if _os.path.lexists(staging):
            raise RuntimeError(
                "PACK-INSTSEG 运行失败；随机 staging 不会递归删除，"
                f"已保留以便核查：{staging.name}；原因：{error}"
            ) from error
        raise
    return {"status": "finished", "process_id": None}


def parse_result(
    project: _Path,
    run: dict[str, _Any],
) -> dict[str, _Any]:
    identity = _identity(run)
    root = _Path(project).resolve(strict=True)
    output = _output(root, identity)
    runtime = _runtime(root)
    _, metrics, artifacts = runtime.validate_workflow_output(
        output,
        mode=identity["mode"],
        run_id=identity["run_id"],
        config_sha256=identity["config_sha256"],
        seed=identity["seed"],
        expected_data_manifest_sha256=identity["expected_manifest_sha256"],
        expected_device=identity["device"],
    )
    relative = output.relative_to(root)
    synthetic = identity["mode"] == "synthetic_debug"
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
                ["合成数据只证明代码链路能跑，不能证明实例分割方法有效"]
                if synthetic
                else ["正式结果仍需外层工作流冻结代码、数据、环境和输出"]
            ),
            "suggestions": [],
        },
        "artifacts": [
            (relative / artifact).as_posix()
            for artifact in artifacts
        ],
    }
