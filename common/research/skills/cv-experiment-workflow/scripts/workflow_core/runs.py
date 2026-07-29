from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .io import atomic_create_json, atomic_write_json, read_bounded_json_object
from .locking import project_write_lock
from .project import PROJECT_SCHEMA_V2, is_link_or_reparse
from .records import (
    _initialized_project,
    _validate_project,
    normalize_safe_relative_path,
)
from .tasking import (
    _attach_run_locked,
    _load_task_locked,
    _validate_command_root_locked,
    _validate_task_run_refs_locked,
    validate_task,
)


RUN_ID = re.compile(r"RUN-[0-9]{4}")
RUN_FIELDS = {
    "id",
    "task_id",
    "purpose",
    "repeat_of",
    "frozen",
    "frozen_digest",
    "execution",
    "result",
    "quality",
    "analysis",
    "artifacts",
}
BOUND_RUN_FIELDS = RUN_FIELDS | {"execution_snapshot", "output_seal"}
FROZEN_FIELDS = {"code", "config", "seed", "data", "environment"}
EXECUTION_FIELDS = {
    "stage",
    "outcome",
    "started_at",
    "finished_at",
    "closed_at",
    "process_id",
    "exit_code",
    "issue_kind",
}
RUN_STAGE_FORWARD = {
    "planned": {"running"},
    "running": {"finished"},
    "finished": {"closed"},
    "closed": set(),
}
RUN_OUTCOMES = {"pending", "succeeded", "failed", "stopped"}
ISSUE_KINDS = {
    None,
    "implementation",
    "prerequisite",
    "environment",
    "temporary_resource",
    "user_stop",
}
FINAL_OUTCOME_ISSUES = {
    "succeeded": {None},
    "failed": {"implementation", "prerequisite", "environment", "temporary_resource"},
    "stopped": {"user_stop"},
}
IMPLEMENTATION_STATES = {"valid", "invalid", "uncertain", "not_applicable"}
HYPOTHESIS_STATES = {
    "supported",
    "not_supported",
    "inconclusive",
    "not_evaluated",
}
QUALITY_STATES = IMPLEMENTATION_STATES


def create_run(
    project: Path,
    task_id: str,
    frozen: dict[str, Any],
    *,
    purpose: str,
    repeat_of: str | None = None,
) -> dict[str, Any]:
    if purpose not in {"debug", "evidence"}:
        raise ValueError("Run purpose 必须是 debug 或 evidence")
    frozen_value = _validate_frozen_payload(frozen, purpose=purpose)
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _validate_command_root_locked(control)
        task = _load_task_locked(control, task_id)
        _validate_task_run_refs_locked(control, task)
        _require_bound_for_new_evidence(task, purpose)
        _validate_task_run_code_binding(task, frozen_value)
        _validate_live_bound_run_locked(
            control,
            task,
            frozen_value,
            purpose=purpose,
        )
        _ensure_run_budget_available(task)
        if task["stage"] != "ready":
            raise ValueError(f"只有通过 readiness 且处于 ready 的 Task 能创建 Run：{task_id}")
        return _create_run_file_locked(
            control,
            task_id,
            purpose,
            repeat_of,
            frozen_value,
            lambda run_id: _attach_run_locked(control, task_id, run_id),
        )


def claim_run(
    project: Path,
    task_id: str,
    frozen: dict[str, Any],
    *,
    purpose: str,
) -> dict[str, Any]:
    """原子完成 ready Task 的单次 Run 占用，不在锁内调用外部 Adapter。"""
    if purpose not in {"debug", "evidence"}:
        raise ValueError("Run purpose 必须是 debug 或 evidence")
    frozen_value = _validate_frozen_payload(frozen, purpose=purpose)
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _validate_command_root_locked(control)
        task = _load_task_locked(control, task_id)
        _validate_task_run_refs_locked(control, task)
        _require_bound_for_new_evidence(task, purpose)
        _validate_task_run_code_binding(task, frozen_value)
        _validate_live_bound_run_locked(
            control,
            task,
            frozen_value,
            purpose=purpose,
        )
        _ensure_run_budget_available(task)
        if task["stage"] != "ready":
            raise ValueError(f"Task 已被占用或尚未 ready：{task_id}")
        def claim_task(run_id: str) -> None:
            updated_task = deepcopy(task)
            updated_task["run_refs"].append(run_id)
            updated_task["stage"] = "executing"
            validate_task(updated_task, expected_id=task_id)
            atomic_write_json(
                control / "tasks" / f"{task_id}.json",
                updated_task,
                transaction_id=uuid.uuid4().hex,
            )
        return _create_run_file_locked(
            control, task_id, purpose, None, frozen_value, claim_task
        )


def _ensure_run_budget_available(task: dict[str, Any]) -> None:
    max_runs = task["budget"].get("max_runs")
    if (
        type(max_runs) is int
        and max_runs > 0
        and len(task["run_refs"]) >= max_runs
    ):
        raise ValueError(
            f"Task 运行预算已用完；如需继续请新建 Task：{task['id']}"
        )


def _require_bound_for_new_evidence(
    task: dict[str, Any],
    purpose: str,
) -> None:
    if (
        purpose == "evidence"
        and task["route_inputs"].get("code_binding") is None
    ):
        raise ValueError(
            "未绑定 Codebase 的 Task 不能新建论文 evidence Run"
        )


def _create_run_file_locked(
    control: Path,
    task_id: str,
    purpose: str,
    repeat_of: str | None,
    frozen: dict[str, Any],
    after_create: Callable[[str], None],
) -> dict[str, Any]:
    run_id = _next_run_id(control / "runs")
    if repeat_of is not None:
        previous = _load_run_locked(control, _run_id(repeat_of))
        _validate_repeat_reference(run_id, task_id, purpose, repeat_of, previous)
    run_dir = control / "runs" / run_id
    run_dir.mkdir()
    created = False
    snapshot_created = False
    try:
        from .run_identity import is_bound_frozen

        execution_snapshot = None
        if is_bound_frozen(frozen):
            from .execution_snapshot import (
                create_run_execution_snapshot_locked,
            )

            execution_snapshot = create_run_execution_snapshot_locked(
                control,
                run_id,
                frozen,
            )
            snapshot_created = True
        payload = _new_run_payload(
            run_id,
            task_id,
            purpose,
            repeat_of,
            frozen,
            execution_snapshot=execution_snapshot,
        )
        created = atomic_create_json(
            run_dir / "run.json", payload, transaction_id=uuid.uuid4().hex
        )
        if not created:
            raise FileExistsError(f"Run 已存在，拒绝覆盖：{run_id}")
        after_create(run_id)
    except BaseException:
        if created:
            (run_dir / "run.json").unlink(missing_ok=True)
        if snapshot_created:
            from .execution_snapshot import (
                remove_run_execution_snapshot_locked,
            )

            remove_run_execution_snapshot_locked(control, run_id)
        try:
            run_dir.rmdir()
        except OSError:
            pass
        raise
    return deepcopy(payload)


def _new_run_payload(
    run_id: str,
    task_id: str,
    purpose: str,
    repeat_of: str | None,
    frozen: dict[str, Any],
    *,
    execution_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "id": run_id,
        "task_id": task_id,
        "purpose": purpose,
        "repeat_of": repeat_of,
        "frozen": frozen,
        "frozen_digest": _canonical_digest(frozen),
        "execution": {
            "stage": "planned",
            "outcome": "pending",
            "started_at": None,
            "finished_at": None,
            "closed_at": None,
            "process_id": None,
            "exit_code": None,
            "issue_kind": None,
        },
        "result": {"metrics": {}, "raw_log": None},
        "quality": {
            "implementation": "not_applicable",
            "interface": "not_applicable",
            "data": "not_applicable",
            "metrics": "not_applicable",
        },
        "analysis": {
            "hypothesis": "not_evaluated",
            "limitations": [],
            "suggestions": [],
        },
        "artifacts": [],
    }
    from .run_identity import is_bound_frozen

    if is_bound_frozen(frozen):
        if execution_snapshot is None:
            raise ValueError("绑定 Run 必须先创建 execution_snapshot")
        payload["execution_snapshot"] = execution_snapshot
        payload["output_seal"] = None
    elif execution_snapshot is not None:
        raise ValueError("未绑定 Run 不能携带 execution_snapshot")
    validate_run(payload, expected_id=run_id)
    return payload


def load_run(project: Path, run_id: str) -> dict[str, Any]:
    _run_id(run_id)
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _validate_command_root_locked(control)
        run = _load_run_locked(control, run_id)
        _validate_run_refs_locked(control, run)
        return deepcopy(run)


def start_run(
    project: Path, run_id: str, *, process_id: int | None = None
) -> dict[str, Any]:
    if process_id is not None and (
        isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0
    ):
        raise ValueError("process_id 必须是正整数或 null")
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _validate_command_root_locked(control)
        current = _load_run_locked(control, run_id)
        _validate_run_refs_locked(control, current)
        _require_stage_transition(current, "running")
        updated = deepcopy(current)
        updated["execution"].update(
            {
                "stage": "running",
                "started_at": _utc_now(),
                "process_id": process_id,
            }
        )
        return _write_run_locked(control, updated)


def set_run_process_id(
    project: Path, run_id: str, process_id: int | None
) -> dict[str, Any]:
    if process_id is not None and (
        isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0
    ):
        raise ValueError("process_id 必须是正整数或 null")
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _validate_command_root_locked(control)
        current = _load_run_locked(control, run_id)
        _validate_run_refs_locked(control, current)
        if current["execution"]["stage"] != "running":
            raise ValueError("只有 running Run 能记录 process_id")
        existing = current["execution"]["process_id"]
        if existing not in {None, process_id}:
            raise ValueError("Run process_id 已存在且不一致")
        if existing == process_id:
            return deepcopy(current)
        updated = deepcopy(current)
        updated["execution"]["process_id"] = process_id
        return _write_run_locked(control, updated)


def record_run_cleanup_state(
    project: Path,
    run_id: str,
    *,
    state: str,
    reason: str,
) -> dict[str, Any]:
    if state not in {"cleanup_pending", "cleanup_failure"}:
        raise ValueError("cleanup state must be cleanup_pending/cleanup_failure")
    if (
        not isinstance(reason, str)
        or not reason
        or re.fullmatch(r"[a-z0-9_:-]{1,256}", reason) is None
    ):
        raise ValueError("cleanup reason must be a fixed machine-readable code")
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _validate_command_root_locked(control)
        current = _load_run_locked(control, run_id)
        _validate_run_refs_locked(control, current)
        if current["execution"]["stage"] != "running":
            raise ValueError("cleanup state can only be recorded on a running Run")
        updated = deepcopy(current)
        retained = [
            value
            for value in updated["analysis"]["limitations"]
            if not value.startswith(("cleanup_pending:", "cleanup_failure:"))
        ]
        updated["analysis"]["limitations"] = [
            *retained,
            f"{state}:{reason}",
        ]
        return _write_run_locked(control, updated)


def finish_run(
    project: Path,
    run_id: str,
    *,
    outcome: str,
    exit_code: int | None,
    metrics: dict[str, Any],
    raw_log: str | None,
    implementation: str,
    interface: str,
    data: str,
    metrics_quality: str,
    hypothesis: str,
    limitations: list[str],
    suggestions: list[str],
    artifacts: list[str],
    issue_kind: str | None = None,
) -> dict[str, Any]:
    validate_outcome_issue_kind(outcome, issue_kind)
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int)
    ):
        raise ValueError("exit_code 必须是整数或 null")
    quality = {
        "implementation": implementation,
        "interface": interface,
        "data": data,
        "metrics": metrics_quality,
    }
    _validate_quality_and_analysis(quality, hypothesis)
    metrics_value = _canonical_json_object(metrics, "metrics")
    raw_log_value = (
        None
        if raw_log is None
        else normalize_safe_relative_path(raw_log, "raw_log")
    )
    limitation_values = _strings(limitations, "limitations")
    suggestion_values = _strings(suggestions, "suggestions")
    artifact_values = [
        normalize_safe_relative_path(item, "artifact")
        for item in _strings(artifacts, "artifacts")
    ]
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _validate_command_root_locked(control)
        current = _load_run_locked(control, run_id)
        _validate_run_refs_locked(control, current)
        _require_stage_transition(current, "finished")
        updated = deepcopy(current)
        updated["execution"].update(
            {
                "stage": "finished",
                "outcome": outcome,
                "finished_at": _utc_now(),
                "exit_code": exit_code,
                "issue_kind": issue_kind,
            }
        )
        updated["result"] = {"metrics": metrics_value, "raw_log": raw_log_value}
        updated["quality"] = quality
        updated["analysis"] = {
            "hypothesis": hypothesis,
            "limitations": limitation_values,
            "suggestions": suggestion_values,
        }
        updated["artifacts"] = artifact_values
        return _write_run_locked(control, updated)


def close_run(project: Path, run_id: str) -> dict[str, Any]:
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        from .validation import validate_v2_evidence_gate_locked

        _tasks, runs, events, _content = validate_v2_evidence_gate_locked(control)
        current = runs.get(run_id)
        if current is None:
            raise ValueError(f"Run 不存在：{run_id}")
        _require_stage_transition(current, "closed")
        from .evidence import current_evidence_level_locked

        if current_evidence_level_locked(
            run_id, events=events
        ) == "none":
            raise ValueError(f"Run 记录证据前不能 closed：{run_id}")
        updated = deepcopy(current)
        updated["execution"]["stage"] = "closed"
        updated["execution"]["closed_at"] = _utc_now()
        return _write_run_locked(control, updated)


def validate_frozen(run: dict[str, Any]) -> bool:
    if not isinstance(run, dict) or "frozen" not in run or "frozen_digest" not in run:
        raise ValueError("Run frozen 结构缺失")
    _validate_frozen_payload(
        run["frozen"],
        purpose=run.get("purpose"),
    )
    if run["frozen_digest"] != _canonical_digest(run["frozen"]):
        raise ValueError("Run frozen 摘要不匹配")
    return True


def validate_outcome_issue_kind(outcome: object, issue_kind: object) -> None:
    allowed = FINAL_OUTCOME_ISSUES.get(outcome) if isinstance(outcome, str) else None
    if allowed is None or issue_kind not in allowed:
        raise ValueError("Run outcome 与 issue_kind 组合无效")


def validate_run(payload: dict[str, Any], *, expected_id: str) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f"Run 字段无效：{expected_id}")
    from .run_identity import is_bound_frozen

    bound = is_bound_frozen(payload.get("frozen"))
    snapshot_record_present = "execution_snapshot" in payload
    expected_fields = (
        BOUND_RUN_FIELDS
        if bound or snapshot_record_present
        else RUN_FIELDS
    )
    if set(payload) != expected_fields:
        raise ValueError(f"Run 字段无效：{expected_id}")
    _run_id(expected_id)
    if payload.get("id") != expected_id:
        raise ValueError(f"Run id 与目录名不一致：{expected_id}")
    if re.fullmatch(r"TASK-[0-9]{4}", str(payload.get("task_id"))) is None:
        raise ValueError(f"Run task_id 无效：{expected_id}")
    if payload.get("purpose") not in {"debug", "evidence"}:
        raise ValueError(f"Run purpose 无效：{expected_id}")
    if snapshot_record_present and not bound:
        from .run_identity import validate_bound_frozen

        validate_bound_frozen(
            payload.get("frozen"),
            purpose=payload.get("purpose"),
        )
    repeat_of = payload.get("repeat_of")
    if repeat_of is not None:
        _run_id(repeat_of)
        if repeat_of == expected_id:
            raise ValueError("Run repeat_of 不能指向自己")
    validate_frozen(payload)
    _validate_execution(payload.get("execution"), expected_id)
    result = payload.get("result")
    if (
        not isinstance(result, dict)
        or set(result) != {"metrics", "raw_log"}
        or not isinstance(result["metrics"], dict)
    ):
        raise ValueError(f"Run result 无效：{expected_id}")
    if result["raw_log"] is not None:
        normalize_safe_relative_path(result["raw_log"], "raw_log")
    quality = payload.get("quality")
    analysis = payload.get("analysis")
    if not isinstance(analysis, dict) or set(analysis) != {
        "hypothesis",
        "limitations",
        "suggestions",
    }:
        raise ValueError(f"Run analysis 无效：{expected_id}")
    _validate_quality_and_analysis(quality, analysis["hypothesis"])
    _strings(analysis["limitations"], "limitations")
    _strings(analysis["suggestions"], "suggestions")
    for artifact in _strings(payload.get("artifacts"), "artifacts"):
        normalize_safe_relative_path(artifact, "artifact")
    if snapshot_record_present:
        from .execution_snapshot import validate_execution_snapshot_record
        from .output_seal import validate_output_seal_record

        validate_execution_snapshot_record(
            expected_id,
            payload["frozen"],
            payload["execution_snapshot"],
        )
        validate_output_seal_record(payload, payload["output_seal"])


def validate_runs_locked(control: Path) -> dict[str, dict[str, Any]]:
    directory = control / "runs"
    if is_link_or_reparse(directory) or not directory.is_dir():
        raise ValueError(f"Run 目录无效：{directory}")
    runs: dict[str, dict[str, Any]] = {}
    for run_dir in sorted(directory.iterdir(), key=lambda item: item.name):
        if (
            RUN_ID.fullmatch(run_dir.name) is None
            or is_link_or_reparse(run_dir)
            or not run_dir.is_dir()
        ):
            raise ValueError(f"Run 目录存在未知对象：{run_dir}")
        entries = {entry.name: entry for entry in run_dir.iterdir()}
        allowed = {
            "run.json",
            "review",
            "execution_snapshot",
            "execution_snapshot.manifest.json",
        }
        if "run.json" not in entries or not set(entries).issubset(allowed):
            raise ValueError(f"Run 目录包含未知对象：{run_dir}")
        run_path = entries["run.json"]
        if is_link_or_reparse(run_path) or not run_path.is_file():
            raise ValueError(f"Run 主文件不是普通文件：{run_path}")
        if "review" in entries:
            review = entries["review"]
            if is_link_or_reparse(review) or not review.is_dir() or any(review.iterdir()):
                raise ValueError(f"Run review 目录无效：{review}")
        payload = read_bounded_json_object(run_path, label="Run")
        validate_run(payload, expected_id=run_dir.name)
        from .run_identity import is_bound_frozen

        snapshot_entries = {
            "execution_snapshot",
            "execution_snapshot.manifest.json",
        }
        present_snapshot_entries = set(entries).intersection(snapshot_entries)
        if is_bound_frozen(payload["frozen"]):
            if present_snapshot_entries != snapshot_entries:
                raise ValueError(f"绑定 Run 缺少执行快照对象：{run_dir}")
        elif present_snapshot_entries:
            raise ValueError(f"未绑定 Run 不能携带执行快照对象：{run_dir}")
        runs[run_dir.name] = payload
    return runs


def validate_task_run_links_locked(
    tasks: dict[str, dict[str, Any]], runs: dict[str, dict[str, Any]]
) -> None:
    for task_id, task in tasks.items():
        for run_id in task["run_refs"]:
            run = runs.get(run_id)
            if run is None or run["task_id"] != task_id:
                raise ValueError(f"Task/Run 引用不一致：{task_id}/{run_id}")
            _validate_task_run_code_binding(task, run["frozen"])
    for run_id, run in runs.items():
        task = tasks.get(run["task_id"])
        if task is None or run_id not in task["run_refs"]:
            raise ValueError(f"Run/Task 引用不一致：{run_id}")
        repeat_of = run["repeat_of"]
        if repeat_of is not None:
            previous = runs.get(repeat_of)
            if previous is None:
                raise ValueError(f"Run repeat_of 不存在：{run_id}/{repeat_of}")
            _validate_repeat_reference(
                run_id,
                run["task_id"],
                run["purpose"],
                repeat_of,
                previous,
            )


def validate_task_run_route_contracts_locked(
    control: Path,
    tasks: dict[str, dict[str, Any]],
    runs: dict[str, dict[str, Any]],
) -> None:
    """核对 Adapter 冻结合同、Task 声明和最终比较没有漂移。"""
    from . import routes

    route_fields = {
        "tune": {
            "primary_metric": "primary_metric",
            "baseline": "baseline",
            "baseline_run_ref": "source_run_ref",
            "allowed_changes": "allowed_changes",
        },
        "ablation": {
            "primary_metric": "primary_metric",
            "baseline": "baseline",
            "baseline_run_ref": "source_run_ref",
            "module_ref": "module_ref",
            "disabled_behavior": "disabled_behavior",
        },
        "reproduction": {
            "primary_metric": "primary_metric",
            "baseline": "baseline",
            "source_ref": "source_ref",
            "source_run_ref": "source_run_ref",
            "tolerance": "tolerance",
            "code_standard": "code_standard",
            "data_standard": "data_standard",
        },
        "innovation": {
            "primary_metric": "primary_metric",
            "baseline": "baseline",
        },
    }
    for run_id, run in runs.items():
        run_task_id = run.get("task_id")
        task = tasks.get(run_task_id) if isinstance(run_task_id, str) else None
        config = run["frozen"].get("config")
        contract = config.get("route_contract") if isinstance(config, dict) else None
        if contract is not None:
            if task is None:
                raise ValueError(f"Run 路线冻结合同找不到 Task：{run_id}")
            if not isinstance(contract, dict):
                raise ValueError(f"Run 路线冻结合同无效：{run_id}")
            if (
                contract.get("task_id") != task["id"]
                or contract.get("route") != task["route"]
            ):
                raise ValueError(
                    f"Task/Run 路线冻结合同不一致：{task['id']}/{run_id}"
                )
            inputs = task["route_inputs"]
            for task_field, contract_field in route_fields[task["route"]].items():
                if inputs.get(task_field) != contract.get(contract_field):
                    raise ValueError(
                        "Task/Run 路线冻结合同字段不一致："
                        f"{task['id']}/{run_id}/{task_field}"
                    )
            if task["route"] != "innovation":
                source_run_ref = contract.get("source_run_ref")
                source_run = runs.get(source_run_ref)
                if source_run is None:
                    raise ValueError(
                        f"路线冻结合同的来源 Run 不存在：{task['id']}/{run_id}"
                    )
                source_path = (
                    control / "runs" / source_run_ref / "run.json"
                )
                if is_link_or_reparse(source_path) or not source_path.is_file():
                    raise ValueError(
                        f"路线冻结合同的来源 Run 文件无效：{task['id']}/{run_id}"
                    )
                actual_source_sha256 = hashlib.sha256(
                    source_path.read_bytes()
                ).hexdigest()
                if contract.get("source_run_sha256") != actual_source_sha256:
                    raise ValueError(
                        "路线冻结合同的来源 Run 哈希发生变化："
                        f"{task['id']}/{run_id}"
                    )
                source_metrics = source_run.get("result", {}).get("metrics")
                primary_metric = contract.get("primary_metric")
                if (
                    not isinstance(source_metrics, dict)
                    or not isinstance(primary_metric, str)
                    or source_metrics.get(primary_metric)
                    != contract.get("baseline")
                ):
                    raise ValueError(
                        "路线冻结合同的 baseline 与来源 Run 主指标不一致："
                        f"{task['id']}/{run_id}"
                    )

        # 比较结论属于 Task 的永久科学记录，不能因为 Adapter 没有旧版
        # route_contract 就跳过。route_contract 只约束上面的旧路线字段。
        if task is None:
            continue
        conclusion = task.get("conclusion")
        if (
            task["stage"] != "done"
            or not isinstance(conclusion, dict)
            or conclusion.get("run_id") != run_id
        ):
            continue
        from .run_identity import comparison_policy_for_frozen

        policy = comparison_policy_for_frozen(run.get("frozen"))
        persisted = conclusion.get("comparison")
        if not isinstance(persisted, dict):
            # 最早期的 debug-only 记录在永久比较字段上线前已经关闭。
            # 这里只允许“旧、无策略标记、debug Run、debug_only 结论”缺省；
            # 当前策略 Run 即使删除 comparison 也不能伪装成历史记录。
            if (
                policy is None
                and run.get("purpose") == "debug"
                and conclusion.get("scope") == "debug_only"
            ):
                continue
            raise ValueError(
                f"Task 永久比较结果无效：{task['id']}/{run_id}"
            )
        if policy is None:
            # 未标记的历史 Run 只接受最早的 pre-fair 形状。候选阶段曾短暂
            # 出现、但从未发布的 fair-without-live 形状不作为兼容格式，
            # 否则同一条记录可以靠整体换皮绕过正式比较门禁。
            expected = _legacy_route_comparison(task, run)
        else:
            inputs = task.get("route_inputs")
            source_ref: object = None
            if isinstance(inputs, dict):
                source_ref = inputs.get(
                    "source_run_ref"
                    if task["route"] in {"reproduction", "innovation"}
                    else "baseline_run_ref"
                )
            source_run = (
                runs.get(source_ref)
                if isinstance(source_ref, str)
                else None
            )

            def stored_seal(run_ref: str) -> object:
                sealed_run = runs.get(run_ref)
                if not isinstance(sealed_run, dict):
                    raise ValueError("比较引用的 Run 不存在")
                seal = sealed_run.get("output_seal")
                if not isinstance(seal, dict):
                    raise ValueError("比较引用的 Run 没有封存记录")
                return seal

            expected = routes.compare(
                task["route"],
                task,
                run,
                source_run=source_run,
                seal_verifier=stored_seal,
            )
        if persisted != expected:
            raise ValueError(
                f"Task 永久比较结果与冻结合同不一致：{task['id']}/{run_id}"
            )
def _legacy_route_comparison(
    task: dict[str, Any],
    run: dict[str, Any],
) -> dict[str, Any]:
    """只用于核对 SYS-FAIR-COMPARISON 之前已经落盘的旧比较形状。"""
    inputs = task["route_inputs"]
    metrics = run.get("result", {}).get("metrics", {})
    primary = inputs.get("primary_metric", "score")
    baseline = inputs.get("baseline")
    candidate = metrics.get(primary) if isinstance(metrics, dict) else None
    delta = (
        candidate - baseline
        if _finite_number(candidate) and _finite_number(baseline)
        else None
    )
    comparison: dict[str, Any] = {
        "primary_metric": primary,
        "baseline": baseline,
        "candidate": candidate,
        "delta": delta,
    }
    if task["route"] == "ablation":
        comparison.update(
            {
                "module_ref": inputs["module_ref"],
                "enabled_run_ref": inputs["baseline_run_ref"],
                "enabled": baseline,
                "disabled": candidate,
                "module_effect": (
                    baseline - candidate
                    if _finite_number(candidate)
                    and _finite_number(baseline)
                    else None
                ),
            }
        )
    elif task["route"] == "reproduction":
        tolerance = inputs["tolerance"]
        comparison.update(
            {
                "source_ref": inputs["source_ref"],
                "source_run_ref": inputs["source_run_ref"],
                "target": baseline,
                "tolerance": tolerance,
                "difference": delta,
                "within_tolerance": (
                    abs(delta) <= tolerance
                    if _finite_number(delta)
                    and _finite_number(tolerance)
                    and tolerance >= 0
                    else False
                ),
            }
        )
    return comparison


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _load_run_locked(control: Path, run_id: str) -> dict[str, Any]:
    _run_id(run_id)
    path = control / "runs" / run_id / "run.json"
    if is_link_or_reparse(path) or not path.is_file():
        raise ValueError(f"Run 不存在或主文件不是普通文件：{run_id}")
    payload = read_bounded_json_object(path, label="Run")
    validate_run(payload, expected_id=run_id)
    return payload


def _validate_run_refs_locked(control: Path, run: dict[str, Any]) -> None:
    task = _load_task_locked(control, run["task_id"])
    if run["id"] not in task["run_refs"]:
        raise ValueError(f"Run/Task 引用不一致：{run['id']}")
    _validate_task_run_code_binding(task, run["frozen"])
    repeat_of = run["repeat_of"]
    if repeat_of is not None:
        previous = _load_run_locked(control, repeat_of)
        _validate_repeat_reference(
            run["id"],
            run["task_id"],
            run["purpose"],
            repeat_of,
            previous,
        )


def _write_run_locked(control: Path, payload: dict[str, Any]) -> dict[str, Any]:
    run_id = payload["id"]
    validate_run(payload, expected_id=run_id)
    atomic_write_json(
        control / "runs" / run_id / "run.json",
        payload,
        transaction_id=uuid.uuid4().hex,
    )
    return deepcopy(payload)


def _validate_frozen_payload(
    value: object,
    *,
    purpose: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != FROZEN_FIELDS:
        raise ValueError("Run frozen 必须包含 code/config/seed/data/environment")
    for key in ("code", "config", "data", "environment"):
        if not isinstance(value[key], dict):
            raise ValueError(f"Run frozen.{key} 必须是 JSON object")
    if isinstance(value["seed"], bool) or not isinstance(value["seed"], int):
        raise ValueError("Run frozen.seed 必须是整数")
    normalized = _canonical_json_object(value, "Run frozen")
    from .run_identity import (
        comparison_policy_for_frozen,
        is_bound_frozen,
        validate_bound_frozen,
    )

    comparison_policy_for_frozen(normalized)
    if is_bound_frozen(normalized):
        validate_bound_frozen(normalized, purpose=purpose)
    return normalized


def _validate_task_run_code_binding(
    task: dict[str, Any],
    frozen: dict[str, Any],
) -> None:
    """强制一张 Task 与它的每个 Run 使用同一种精确代码来源。"""
    from .run_identity import is_bound_frozen

    binding = task["route_inputs"].get("code_binding")
    bound = is_bound_frozen(frozen)
    if binding is None:
        if bound:
            raise ValueError(
                f"未绑定 Codebase 的 Task 不能使用 bound Run frozen：{task['id']}"
            )
        return
    if not bound:
        raise ValueError(
            f"绑定 Codebase 的 Task 必须使用 bound Run frozen：{task['id']}"
        )
    code = frozen["code"]
    for field in ("codebase_id", "branch", "commit", "tag"):
        if code.get(field) != binding[field]:
            raise ValueError(
                "Task/Run code_binding 不一致："
                f"{task['id']}/{field}"
            )


def _validate_live_bound_run_locked(
    control: Path,
    task: dict[str, Any],
    frozen: dict[str, Any],
    *,
    purpose: str,
) -> None:
    """写 Run 前用登记仓库现场重建不可由调用者伪造的代码身份。"""
    binding = task["route_inputs"].get("code_binding")
    if binding is None:
        return
    from .codebases import (
        _verify_codebase_gate_locked,
        load_codebase_adapter_spec,
    )

    expected_git = {
        "branch": binding["branch"],
        "commit": binding["commit"],
        "tag": binding["tag"],
        "require_clean": purpose == "evidence",
    }
    first_gate = _verify_codebase_gate_locked(
        control,
        binding["codebase_id"],
        expected_git,
    )
    adapter = load_codebase_adapter_spec(
        Path(first_gate["repo_path"]),
        binding["commit"],
    )
    second_gate = _verify_codebase_gate_locked(
        control,
        binding["codebase_id"],
        expected_git,
    )
    if second_gate != first_gate:
        raise ValueError("Codebase Git 身份在 Run live gate 期间发生变化")

    code = frozen["code"]
    expected_code = {
        "codebase_id": binding["codebase_id"],
        "branch": second_gate["branch"],
        "commit": second_gate["commit"],
        "tag": second_gate["tag"],
        "clean_required": purpose == "evidence",
        "worktree_clean": second_gate["worktree_clean"],
        "adapter_path": "workflow_adapter.py",
        "adapter_sha256": hashlib.sha256(
            adapter["source_bytes"]
        ).hexdigest(),
    }
    for field, expected in expected_code.items():
        if code.get(field) != expected:
            raise ValueError(
                "Run frozen.code 与 live Codebase 身份不一致："
                f"{task['id']}/{field}"
            )


def _validate_execution(value: object, run_id: str) -> None:
    if not isinstance(value, dict) or set(value) != EXECUTION_FIELDS:
        raise ValueError(f"Run execution 无效：{run_id}")
    stage = value["stage"]
    outcome = value["outcome"]
    if stage not in RUN_STAGE_FORWARD or outcome not in RUN_OUTCOMES:
        raise ValueError(f"Run stage/outcome 无效：{run_id}")
    if stage in {"planned", "running"} and outcome != "pending":
        raise ValueError(f"未完成 Run 的 outcome 必须是 pending：{run_id}")
    if stage in {"finished", "closed"} and outcome == "pending":
        raise ValueError(f"已完成 Run 的 outcome 不能是 pending：{run_id}")
    started = value["started_at"]
    finished = value["finished_at"]
    closed = value["closed_at"]
    if stage == "planned" and any(item is not None for item in (started, finished, closed)):
        raise ValueError(f"planned Run 不应有执行时间：{run_id}")
    if stage == "running" and (started is None or finished is not None or closed is not None):
        raise ValueError(f"running Run 时间无效：{run_id}")
    if stage == "finished" and (started is None or finished is None or closed is not None):
        raise ValueError(f"finished Run 时间无效：{run_id}")
    if stage == "closed" and any(item is None for item in (started, finished, closed)):
        raise ValueError(f"closed Run 时间无效：{run_id}")
    for label, timestamp in (
        ("started_at", started),
        ("finished_at", finished),
        ("closed_at", closed),
    ):
        if timestamp is not None:
            _timestamp(timestamp, label)
    process_id = value["process_id"]
    if process_id is not None and (
        isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0
    ):
        raise ValueError(f"Run process_id 无效：{run_id}")
    exit_code = value["exit_code"]
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int)
    ):
        raise ValueError(f"Run exit_code 无效：{run_id}")
    issue_kind = value["issue_kind"]
    if issue_kind not in ISSUE_KINDS:
        raise ValueError(f"Run issue_kind 无效：{run_id}")
    if stage in {"planned", "running"} and issue_kind is not None:
        raise ValueError(f"未完成 Run 的 issue_kind 必须是 null：{run_id}")
    if stage in {"finished", "closed"}:
        validate_outcome_issue_kind(outcome, issue_kind)


def _validate_quality_and_analysis(quality: object, hypothesis: object) -> None:
    if not isinstance(quality, dict) or set(quality) != {
        "implementation",
        "interface",
        "data",
        "metrics",
    }:
        raise ValueError("Run quality 字段无效")
    if any(value not in QUALITY_STATES for value in quality.values()):
        raise ValueError("Run quality 判断无效")
    if hypothesis not in HYPOTHESIS_STATES:
        raise ValueError("Run hypothesis 判断无效")
    if quality["implementation"] == "invalid" and hypothesis != "not_evaluated":
        raise ValueError("实现 invalid 时 hypothesis 必须是 not_evaluated，不能写 not_supported")


def _require_stage_transition(run: dict[str, Any], to_stage: str) -> None:
    from_stage = run["execution"]["stage"]
    if to_stage not in RUN_STAGE_FORWARD[from_stage]:
        raise ValueError(f"Run 不能从 {from_stage} 转到 {to_stage}：{run['id']}")


def _next_run_id(directory: Path) -> str:
    numbers: list[int] = []
    for path in directory.iterdir():
        if (
            RUN_ID.fullmatch(path.name) is None
            or is_link_or_reparse(path)
            or not path.is_dir()
        ):
            raise ValueError(f"Run ID 路径无效：{path}")
        numbers.append(int(path.name.split("-")[1]))
    number = max(numbers, default=0) + 1
    if number > 9999:
        raise ValueError("Run ID 已耗尽")
    return f"RUN-{number:04d}"


def _validate_repeat_reference(
    run_id: str,
    task_id: str,
    purpose: str,
    repeat_of: str,
    previous: dict[str, Any],
) -> None:
    if repeat_of == run_id:
        raise ValueError("Run repeat_of 不能指向自己")
    if int(repeat_of.split("-")[1]) >= int(run_id.split("-")[1]):
        raise ValueError("repeat_of 必须引用更早创建的 Run")
    if previous["task_id"] != task_id:
        raise ValueError("repeat_of 必须属于同一 Task")
    if previous["purpose"] != purpose:
        raise ValueError("repeat_of 必须保持相同 purpose")
    if previous["execution"]["stage"] != "closed":
        raise ValueError("repeat_of 必须引用已 closed 的旧 Run")


def _run_id(value: object) -> str:
    if not isinstance(value, str) or RUN_ID.fullmatch(value) is None:
        raise ValueError(f"Run ID 无效：{value}")
    return value


def _strings(value: object, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > 4096
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ValueError(f"{label} 必须是字符串数组")
    return [item.strip() for item in value]


def _canonical_digest(value: object) -> str:
    content = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _canonical_json_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是 JSON object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        normalized = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} 必须是有限 JSON object") from error
    if normalized != value:
        raise ValueError(f"{label} 序列化后会改变，拒绝写入")
    return normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是 ISO 时间字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{label} 必须是 ISO 时间字符串") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} 必须带时区")
    return value
