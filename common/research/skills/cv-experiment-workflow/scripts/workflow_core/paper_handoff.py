from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import unicodedata
import uuid
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .evidence import levels_from_events
from .io import (
    _json_bytes,
    _validate_json_size,
    atomic_create_bytes_clean,
    parent_directory_anchor,
    read_bounded_json_object,
)
from .locking import project_snapshot_lock, project_write_lock
from .project import PROJECT_SCHEMA_V2, is_link_or_reparse
from .records import (
    _initialized_project,
    _validate_project,
    normalize_safe_relative_path,
)
from .releases import WORKFLOW_RELEASE_IDENTITY
from .validation import validate_v2_state_locked


HANDOFF_SCHEMA = "cv-research-handoff/v1"
HANDOFF_VERIFICATION_SCHEMA = "cv-research-handoff-verification/v1"
HANDOFF_HASH_CHUNK_BYTES = 1024 * 1024
MAX_ARTIFACT_COUNT = 128
MAX_TOTAL_ARTIFACT_BYTES = 512 * 1024 * 1024
HANDOFF_CONTEXT_KEY = "_cv_experiment_workflow_handoff"
HANDOFF_CONTEXT_SCHEMA = "cv-experiment-workflow.paperflow-v1-context/v1"
_VERSION_REF = re.compile(r"VER-[0-9]{4}")
_RUN_ID = re.compile(r"RUN-[0-9]{4}")
_SHA256_IDENTITY = re.compile(r"sha256:[0-9a-f]{64}")
_LEGACY_V1_PRODUCERS = (
    {
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.2.2",
        "system_version": "SYS-V2.10.2",
    },
    {
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.2.3",
        "system_version": "SYS-V2.10.3",
    },
    {
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.3.0",
        "system_version": "SYS-V2.11.0",
    },
)


def export_paperflow(
    project: Path,
    run_id: str,
    output: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """把一条已核验科研结果导出为不可覆盖的 PaperFlow 交接快照。"""
    root = _initialized_project(project)
    target = Path(output).expanduser()
    target = _reject_control_output(root, target)
    output_anchor = (
        nullcontext()
        if dry_run
        else parent_directory_anchor(target.parent)
    )
    with output_anchor:
        pinned_target = _reject_control_output(root, target)
        if pinned_target != target:
            raise ValueError("PaperFlow 输出路径在固定父目录时发生变化")
        project_lock = (
            project_snapshot_lock if dry_run else project_write_lock
        )
        with project_lock(root):
            control = _validate_project(
                root,
                expected_schema=PROJECT_SCHEMA_V2,
            )
            tasks, runs, events, _evidence_content = (
                validate_v2_state_locked(control)
            )
            run = runs.get(run_id)
            if run is None:
                raise ValueError(f"Run 不存在：{run_id}")
            task = tasks.get(run["task_id"])
            if task is None:
                raise ValueError(f"Run 所属 Task 不存在：{run_id}")
            project_record = read_bounded_json_object(
                control / "project.json",
                label="project.json",
            )
            payload = _build_payload(
                root,
                project_record,
                task,
                run,
                events,
            )

        canonical = _canonical_json(payload)
        digest = hashlib.sha256(canonical).hexdigest()
        envelope = {
            "schema": HANDOFF_SCHEMA,
            "snapshot_id": f"rsh-{digest}",
            "payload_sha256": f"sha256:{digest}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        if dry_run:
            return envelope
        content = _json_bytes(envelope)
        _validate_json_size(content)
        if not atomic_create_bytes_clean(
            target,
            content,
            transaction_id=uuid.uuid4().hex,
            operation="JSON 原子创建",
        ):
            raise FileExistsError(f"目标文件已存在，拒绝覆盖：{target}")
        return envelope


def _reject_control_output(project_root: Path, target: Path) -> Path:
    try:
        control = (project_root / ".experiment-workflow").resolve(strict=True)
        resolved_target = target.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError("无法安全解析 PaperFlow 输出路径") from error
    try:
        resolved_target.relative_to(control)
    except ValueError:
        return resolved_target
    raise ValueError(
        "PaperFlow 输出目标不能位于项目控制目录或其后代："
        f"{target}"
    )


def verify_paperflow_handoff(
    project: Path,
    handoff: Path,
    *,
    expected_run_id: str,
    expected_payload_sha256: str,
) -> dict[str, Any]:
    """只读核验交接包是否仍与科研项目当前一致快照完全相同。"""
    report = verification_failure_report()
    if (
        not isinstance(expected_run_id, str)
        or _RUN_ID.fullmatch(expected_run_id) is None
        or not isinstance(expected_payload_sha256, str)
        or _SHA256_IDENTITY.fullmatch(expected_payload_sha256) is None
    ):
        return _verification_failure(report, "expected_identity_invalid")
    try:
        envelope = _parse_handoff(handoff)
    except Exception:
        return _verification_failure(report, "handoff_invalid")

    payload = envelope["payload"]
    report["run_id"] = payload["run"]["run_id"]
    if (
        report["run_id"] != expected_run_id
        or envelope["payload_sha256"] != expected_payload_sha256
    ):
        return _verification_failure(report, "expected_identity_mismatch")
    package_payload_bytes = _canonical_json(payload)
    package_project_id = payload["project"]["project_id"]
    producer_profile = _producer_payload_profile(payload["producer"])
    if producer_profile is None:
        return _verification_failure(report, "producer_mismatch")

    try:
        root = _initialized_project(project)
        with project_snapshot_lock(root):
            control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
            tasks, runs, events, _evidence_content = validate_v2_state_locked(
                control
            )
            project_record = read_bounded_json_object(
                control / "project.json",
                label="project.json",
            )
            project_id = _nonempty_text(
                project_record.get("project_id"), "project_id"
            )
            report["project_uuid"] = project_id
            if project_id != package_project_id:
                return _verification_failure(report, "project_mismatch")

            run = runs.get(report["run_id"])
            if run is None:
                return _verification_failure(report, "source_unavailable")
            status = levels_from_events(events).get(run["id"], "none")
            report["source_status"] = status
            matching_events = [
                event
                for event in events
                if event.get("subject_id") == run["id"]
            ]
            if matching_events:
                report["event_id"] = matching_events[-1].get("event_id")
            if status == "revoked":
                return _verification_failure(report, "revoked")

            task = tasks.get(run["task_id"])
            if task is None:
                return _verification_failure(report, "source_unavailable")
            current_payload = _build_payload(
                root,
                project_record,
                task,
                run,
                events,
                producer=payload["producer"],
                legacy_v1=producer_profile != "current",
                legacy_1_2_2_paths=(
                    producer_profile == "legacy_1_2_2"
                ),
            )
    except Exception:
        return _verification_failure(report, "source_unavailable")

    current_payload_bytes = _canonical_json(current_payload)
    report["current_payload_sha256"] = _sha256_bytes(current_payload_bytes)
    if (
        report["current_payload_sha256"] != expected_payload_sha256
        or current_payload_bytes != package_payload_bytes
    ):
        return _verification_failure(report, "handoff_payload_mismatch")
    report["status"] = "pass"
    report["error_code"] = None
    return report


def verification_failure_report(
    error_code: str | None = None,
) -> dict[str, Any]:
    report = {
        "schema": HANDOFF_VERIFICATION_SCHEMA,
        "status": "fail",
        "error_code": error_code,
        "project_uuid": None,
        "run_id": None,
        "event_id": None,
        "source_status": None,
        "current_payload_sha256": None,
    }
    return report


def _verification_failure(
    report: dict[str, Any],
    error_code: str,
) -> dict[str, Any]:
    report["status"] = "fail"
    report["error_code"] = error_code
    return report


def _parse_handoff(path: Path) -> dict[str, Any]:
    envelope = read_bounded_json_object(Path(path), label="PaperFlow 交接包")
    if set(envelope) != {
        "schema",
        "snapshot_id",
        "payload_sha256",
        "created_at",
        "payload",
    }:
        raise ValueError("PaperFlow 交接包字段集合无效")
    if envelope.get("schema") != HANDOFF_SCHEMA:
        raise ValueError("PaperFlow 交接包 schema 无效")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("PaperFlow 交接包 payload 无效")
    payload_sha256 = _sha256_bytes(_canonical_json(payload))
    digest = payload_sha256.removeprefix("sha256:")
    if envelope.get("payload_sha256") != payload_sha256:
        raise ValueError("PaperFlow 交接包 payload 摘要无效")
    if envelope.get("snapshot_id") != f"rsh-{digest}":
        raise ValueError("PaperFlow 交接包 snapshot_id 无效")
    _nonempty_text(envelope.get("created_at"), "created_at")
    producer = payload.get("producer")
    project = payload.get("project")
    run = payload.get("run")
    if not isinstance(producer, dict):
        raise ValueError("PaperFlow 交接包 producer 无效")
    if not isinstance(project, dict) or set(project) != {"project_id"}:
        raise ValueError("PaperFlow 交接包 project 无效")
    if not isinstance(run, dict) or "run_id" not in run:
        raise ValueError("PaperFlow 交接包 run 无效")
    _nonempty_text(project.get("project_id"), "payload.project.project_id")
    run_id = _nonempty_text(run.get("run_id"), "payload.run.run_id")
    if _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("payload.run.run_id 格式无效")
    return envelope


def _producer_identity() -> dict[str, str]:
    return {
        "skill_id": WORKFLOW_RELEASE_IDENTITY["skill_id"],
        "release_version": WORKFLOW_RELEASE_IDENTITY["release_version"],
        "system_version": WORKFLOW_RELEASE_IDENTITY["system_version"],
    }


def _producer_payload_profile(value: object) -> str | None:
    if value == _producer_identity():
        return "current"
    if value == _LEGACY_V1_PRODUCERS[0]:
        return "legacy_1_2_2"
    if any(value == producer for producer in _LEGACY_V1_PRODUCERS[1:]):
        return "legacy"
    return None


def _build_payload(
    project_root: Path,
    project: dict[str, Any],
    task: dict[str, Any],
    run: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    producer: dict[str, str] | None = None,
    legacy_v1: bool = False,
    legacy_1_2_2_paths: bool = False,
) -> dict[str, Any]:
    project_id = _nonempty_text(project.get("project_id"), "project_id")
    status = levels_from_events(events).get(run["id"], "none")
    if status not in {"single_run", "confirmed"}:
        raise ValueError(f"当前 Evidence 状态不允许导出或未知：{status}")
    if run.get("purpose") != "evidence":
        raise ValueError("只有 evidence Run 允许导出")
    from .run_identity import is_bound_frozen

    if is_bound_frozen(run.get("frozen")):
        raise ValueError(
            "绑定 Codebase 的 Run 在可搬移证据包协议完成前禁止导出 PaperFlow"
        )

    execution = run.get("execution")
    if (
        not isinstance(execution, dict)
        or execution.get("stage") not in {"finished", "closed"}
        or execution.get("outcome") != "succeeded"
    ):
        raise ValueError("只有已成功完成的 Run 允许导出")

    frozen = run.get("frozen")
    if not isinstance(frozen, dict):
        raise ValueError("Run 缺少 frozen 复现身份")
    code = _required_object(frozen.get("code"), "frozen.code")
    _nonempty_text(code.get("repository"), "frozen.code.repository")
    commit = _nonempty_text(code.get("commit"), "frozen.code.commit")
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", commit) is None:
        raise ValueError("frozen.code.commit 必须是 7..64 位十六进制版本号")

    config = _required_object(frozen.get("config"), "frozen.config")
    if not legacy_v1 and HANDOFF_CONTEXT_KEY in config:
        raise ValueError(
            f"frozen.config 已占用 PaperFlow 保留键：{HANDOFF_CONTEXT_KEY}"
        )
    metric_definition = _required_object(
        config.get("metric_definition"),
        "frozen.config.metric_definition",
    )
    seed = frozen.get("seed")
    if type(seed) is not int:
        raise ValueError("frozen.seed 必须是整数，不能是 bool 或缺失值")

    data = _required_object(frozen.get("data"), "frozen.data")
    for field in ("dataset_id", "version", "split"):
        _nonempty_text(data.get(field), f"frozen.data.{field}")
    environment = _required_object(
        frozen.get("environment"),
        "frozen.environment",
    )

    result = run.get("result")
    if not isinstance(result, dict):
        raise ValueError("Run 缺少 result")
    metrics = _required_object(result.get("metrics"), "result.metrics")
    _validate_finite_metrics(metrics)
    primary_metric = _validate_metric_contract(
        task,
        metric_definition,
        metrics,
    )
    artifacts = run.get("artifacts")
    if (
        not isinstance(artifacts, list)
        or not artifacts
        or not all(isinstance(item, str) and item.strip() for item in artifacts)
    ):
        raise ValueError("Run 必须包含非空 artifacts 结果产物")
    if len(artifacts) > MAX_ARTIFACT_COUNT:
        raise ValueError(
            f"artifact 数量超过 {MAX_ARTIFACT_COUNT} 个上限"
        )
    raw_log_relative = _normalize_result_relative_path(
        result.get("raw_log"),
        "result.raw_log",
        legacy_1_2_2=legacy_1_2_2_paths,
    )
    artifact_relatives = [
        _normalize_result_relative_path(
            path,
            f"artifact[{index}]",
            legacy_1_2_2=legacy_1_2_2_paths,
        )
        for index, path in enumerate(artifacts)
    ]
    all_relatives = [raw_log_relative, *artifact_relatives]
    unique_relatives = (
        set(all_relatives)
        if legacy_1_2_2_paths
        else {_windows_path_key(path) for path in all_relatives}
    )
    if len(all_relatives) != len(unique_relatives):
        raise ValueError("结果文件路径重复，raw_log 与 artifacts 必须各自唯一")
    preflight_files = [
        _preflight_source_file(project_root, relative)
        for relative in all_relatives
    ]
    total_bytes = sum(item["size_bytes"] for item in preflight_files)
    if total_bytes > MAX_TOTAL_ARTIFACT_BYTES:
        raise ValueError(
            "结果文件总大小超过 "
            f"{MAX_TOTAL_ARTIFACT_BYTES} bytes 上限"
        )
    raw_log = _hash_preflight_file(preflight_files[0])

    version_refs = task.get("target_refs")
    if (
        not isinstance(version_refs, list)
        or not any(
            isinstance(item, str) and _VERSION_REF.fullmatch(item)
            for item in version_refs
        )
    ):
        raise ValueError("Task target_refs 缺少稳定 Version ID")

    matching_events = [
        event for event in events if event.get("subject_id") == run["id"]
    ]
    if not matching_events:
        raise ValueError("Run 缺少 Evidence 事件版本")
    current_event = matching_events[-1]
    if current_event.get("to") != status:
        raise ValueError("Evidence 当前事件与状态不一致")

    artifact_records = [
        _hash_preflight_file(item) for item in preflight_files[1:]
    ]
    _verify_all_preflight_files_unchanged(preflight_files)
    qualification = "result_verified" if status == "confirmed" else "candidate"
    reproducibility_config = deepcopy(config)
    if not legacy_v1:
        reproducibility_config[HANDOFF_CONTEXT_KEY] = {
            "schema": HANDOFF_CONTEXT_SCHEMA,
            "route_inputs": {
                "primary_metric": primary_metric,
            },
            "run": {
                "analysis": deepcopy(run["analysis"]),
            },
            "evidence_event": {
                field: deepcopy(current_event[field])
                for field in (
                    "reason",
                    "proposed_by",
                    "checked_by",
                    "applied_by",
                    "time",
                )
            },
        }
    return {
        "producer": deepcopy(
            _producer_identity() if producer is None else producer
        ),
        "project": {
            "project_id": project_id,
        },
        "task": {
            "task_id": task["id"],
            "route": task["route"],
            "version_refs": list(version_refs),
        },
        "run": {
            "run_id": run["id"],
            "purpose": run["purpose"],
            "frozen_digest": run["frozen_digest"],
        },
        "source_status": status,
        "qualification": qualification,
        "evidence_event": {
            "event_id": current_event["event_id"],
            "from": current_event["from"],
            "to": current_event["to"],
            "evidence_refs": list(current_event["evidence_refs"]),
        },
        "reproducibility": {
            "code": deepcopy(code),
            "config": reproducibility_config,
            "seed": seed,
            "data": deepcopy(data),
            "environment": deepcopy(environment),
            "metric_definition": deepcopy(metric_definition),
            "metrics": deepcopy(metrics),
            "raw_log": raw_log,
            "artifacts": artifact_records,
        },
    }


def _required_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{label} 必须是非空 JSON object")
    return value


def _normalize_result_relative_path(
    value: object,
    label: str,
    *,
    legacy_1_2_2: bool = False,
) -> str:
    normalized = normalize_safe_relative_path(value, label)
    if legacy_1_2_2:
        return normalized
    if unicodedata.normalize("NFKC", normalized) != normalized:
        raise ValueError(f"{label} 必须使用 NFKC 规范路径")
    for segment in normalized.split("/"):
        if ":" in segment:
            raise ValueError(f"{label} 禁止 Windows ADS 冒号")
        if segment.endswith((".", " ")):
            raise ValueError(f"{label} 禁止尾随点或空格")
        basename = segment.split(".", 1)[0].casefold()
        reserved = {"con", "prn", "aux", "nul"}
        reserved.update(f"com{index}" for index in range(1, 10))
        reserved.update(f"lpt{index}" for index in range(1, 10))
        if basename in reserved:
            raise ValueError(f"{label} 禁止 Windows 设备名")
    return normalized


def _windows_path_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).replace("\\", "/").casefold()


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串")
    return value.strip()


def _validate_finite_metrics(metrics: dict[str, Any]) -> None:
    for name, value in metrics.items():
        _nonempty_text(name, "metric name")
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError(f"指标值必须是有限数字：{name}")


def _preflight_source_file(
    project_root: Path,
    relative: str,
) -> dict[str, Any]:
    root = project_root.resolve(strict=True)
    target = project_root / relative
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise ValueError(f"结果文件必须指向项目内真实文件：{relative}") from error
    try:
        identity = target.lstat()
    except OSError as error:
        raise ValueError(f"结果文件不存在：{relative}") from error
    if is_link_or_reparse(target) or not stat.S_ISREG(identity.st_mode):
        raise ValueError(f"结果文件必须是普通文件：{relative}")
    return {
        "path": relative,
        "target": target,
        "identity": identity,
        "path_signature": _stable_file_signature(identity),
        "basic_signature": _stable_file_signature(
            identity,
            include_ctime=False,
        ),
        "size_bytes": identity.st_size,
    }


def _hash_preflight_file(item: dict[str, Any]) -> dict[str, Any]:
    target = item["target"]
    identity = item["identity"]
    path_signature = item["path_signature"]
    basic_signature = item["basic_signature"]
    digest = hashlib.sha256()
    bytes_read = 0
    try:
        with target.open("rb") as source:
            opened = os.fstat(source.fileno())
            opened_basic_signature = _stable_file_signature(
                opened,
                include_ctime=False,
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or not os.path.samestat(identity, opened)
                or opened.st_size != item["size_bytes"]
                or opened_basic_signature != basic_signature
            ):
                raise ValueError(
                    f"结果文件在哈希前发生变化：{item['path']}"
                )
            opened_signature = _stable_file_signature(opened)
            remaining = item["size_bytes"]
            while remaining:
                chunk = source.read(
                    min(HANDOFF_HASH_CHUNK_BYTES, remaining)
                )
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise ValueError(
                        f"结果文件在哈希期间发生变化：{item['path']}"
                    )
                bytes_read += len(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            if source.read(1):
                raise ValueError(
                    f"结果文件在哈希期间发生变化：{item['path']}"
                )
            after_read = os.fstat(source.fileno())
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(f"无法读取结果文件：{item['path']}") from error
    try:
        current = target.lstat()
    except OSError as error:
        raise ValueError(f"结果文件哈希后无法确认：{item['path']}") from error
    if (
        is_link_or_reparse(target)
        or not stat.S_ISREG(current.st_mode)
        or not os.path.samestat(opened, after_read)
        or not os.path.samestat(identity, current)
        or after_read.st_size != item["size_bytes"]
        or current.st_size != item["size_bytes"]
        or _stable_file_signature(after_read) != opened_signature
        or _stable_file_signature(current) != path_signature
        or bytes_read != item["size_bytes"]
    ):
        raise ValueError(f"结果文件在哈希期间发生变化：{item['path']}")
    item["hashed_handle_basic_signature"] = _stable_file_signature(
        after_read,
        include_ctime=False,
    )
    item["hashed_path_signature"] = _stable_file_signature(current)
    hashed_sha256 = f"sha256:{digest.hexdigest()}"
    item["hashed_sha256"] = hashed_sha256
    return {
        "path": item["path"],
        "size_bytes": item["size_bytes"],
        "sha256": hashed_sha256,
    }


def _verify_all_preflight_files_unchanged(
    items: list[dict[str, Any]],
) -> None:
    for item in items:
        target = item["target"]
        try:
            current = target.lstat()
        except OSError as error:
            raise ValueError(
                f"结果文件在全部哈希完成后无法确认：{item['path']}"
            ) from error
        current_signature = _stable_file_signature(current)
        current_basic_signature = _stable_file_signature(
            current,
            include_ctime=False,
        )
        if (
            is_link_or_reparse(target)
            or not stat.S_ISREG(current.st_mode)
            or not os.path.samestat(item["identity"], current)
            or current_signature != item["path_signature"]
            or current_basic_signature
            != item.get("hashed_handle_basic_signature")
            or current_signature != item.get("hashed_path_signature")
        ):
            raise ValueError(
                f"结果文件在全部哈希完成后发生变化：{item['path']}"
            )
        if _rehash_preflight_file(item) != item.get("hashed_sha256"):
            raise ValueError(
                f"结果文件在全部哈希完成后发生变化：{item['path']}"
            )


def _rehash_preflight_file(item: dict[str, Any]) -> str:
    target = item["target"]
    digest = hashlib.sha256()
    bytes_read = 0
    try:
        with target.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or not os.path.samestat(item["identity"], opened)
                or opened.st_size != item["size_bytes"]
            ):
                raise ValueError(
                    "结果文件在全部哈希完成后发生变化："
                    f"{item['path']}"
                )
            remaining = item["size_bytes"]
            while remaining:
                chunk = source.read(
                    min(HANDOFF_HASH_CHUNK_BYTES, remaining)
                )
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise ValueError(
                        "结果文件在全部哈希完成后发生变化："
                        f"{item['path']}"
                    )
                bytes_read += len(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            if source.read(1):
                raise ValueError(
                    "结果文件在全部哈希完成后发生变化："
                    f"{item['path']}"
                )
            after_read = os.fstat(source.fileno())
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(
            f"结果文件在全部哈希完成后无法读取：{item['path']}"
        ) from error
    try:
        current = target.lstat()
    except OSError as error:
        raise ValueError(
            f"结果文件在全部哈希完成后无法确认：{item['path']}"
        ) from error
    if (
        is_link_or_reparse(target)
        or not stat.S_ISREG(current.st_mode)
        or not os.path.samestat(opened, after_read)
        or not os.path.samestat(item["identity"], current)
        or after_read.st_size != item["size_bytes"]
        or current.st_size != item["size_bytes"]
        or _stable_file_signature(current) != item["path_signature"]
        or bytes_read != item["size_bytes"]
    ):
        raise ValueError(
            f"结果文件在全部哈希完成后发生变化：{item['path']}"
        )
    return f"sha256:{digest.hexdigest()}"


def _stable_file_signature(
    value: os.stat_result,
    *,
    include_ctime: bool = True,
) -> tuple[int, ...]:
    required: list[int] = []
    for field in ("st_dev", "st_ino", "st_size"):
        current = getattr(value, field, None)
        if type(current) is not int:
            raise ValueError(f"平台缺少稳定文件身份字段：{field}")
        required.append(current)
    mtime_ns = _timestamp_ns(value, "st_mtime_ns", "st_mtime")
    if mtime_ns is None:
        raise ValueError("平台缺少稳定文件身份字段：st_mtime_ns")
    required.append(mtime_ns)
    if include_ctime:
        ctime_ns = _timestamp_ns(value, "st_ctime_ns", "st_ctime")
        if ctime_ns is not None:
            required.append(ctime_ns)
    return tuple(required)


def _timestamp_ns(
    value: os.stat_result,
    nanosecond_field: str,
    second_field: str,
) -> int | None:
    nanoseconds = getattr(value, nanosecond_field, None)
    if type(nanoseconds) is int:
        return nanoseconds
    seconds = getattr(value, second_field, None)
    if type(seconds) in {int, float} and math.isfinite(seconds):
        return int(seconds * 1_000_000_000)
    return None


def _validate_metric_contract(
    task: dict[str, Any],
    definitions: dict[str, Any],
    metrics: dict[str, Any],
) -> str:
    for name in metrics:
        if name not in definitions:
            raise ValueError(f"指标 {name} 缺少同名定义")
        definition = definitions[name]
        if not isinstance(definition, str) or not definition.strip():
            raise ValueError(f"指标定义 {name} 必须是非空字符串")
    primary_metric = _nonempty_text(
        task["route_inputs"].get("primary_metric"),
        "route_inputs.primary_metric",
    )
    if primary_metric not in metrics:
        raise ValueError(
            f"主指标 {primary_metric} 在 result.metrics 中缺失"
        )
    if primary_metric not in definitions:
        raise ValueError(
            f"主指标 {primary_metric} 在 metric_definition 中缺失"
        )
    return primary_metric


def _canonical_json(value: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("交接 payload 必须是有限 JSON") from error


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"
