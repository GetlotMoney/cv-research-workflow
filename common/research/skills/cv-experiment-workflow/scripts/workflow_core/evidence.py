from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io import (
    DEFAULT_JSON_LIMIT,
    atomic_write_bytes,
    parse_json_object_bytes,
    read_bounded_regular_file,
)
from .locking import project_write_lock
from .project import PROJECT_SCHEMA_V2
from .records import _initialized_project, _validate_project
from .runs import RUN_ID


EVIDENCE_EVENT_FIELDS = {
    "event_id",
    "subject_type",
    "subject_id",
    "from",
    "to",
    "evidence_refs",
    "reason",
    "proposed_by",
    "checked_by",
    "applied_by",
    "time",
}
EVIDENCE_FORWARD = {
    "none": {"debug", "single_run", "disqualified"},
    "debug": {"disqualified"},
    "single_run": {"confirmed", "disqualified", "revoked"},
    "confirmed": {"revoked"},
    "disqualified": set(),
    "revoked": set(),
}
EVENT_ID = re.compile(r"EVT-[0-9]{4}")


def record_evidence_transition(
    project: Path,
    run_id: str,
    to_level: str,
    *,
    evidence_refs: list[str],
    reason: str,
    proposed_by: str,
    checked_by: str,
    applied_by: str,
) -> dict[str, Any]:
    if to_level not in EVIDENCE_FORWARD:
        raise ValueError(f"Evidence 等级无效：{to_level}")
    refs = _run_refs(evidence_refs)
    reason_value = _text(reason, "reason")
    proposed_value = _text(proposed_by, "proposed_by")
    checked_value = _optional_text(checked_by, "checked_by")
    applied_value = _text(applied_by, "applied_by")
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        from .validation import validate_v2_evidence_gate_locked

        tasks, runs, events, previous = validate_v2_evidence_gate_locked(control)
        run = runs.get(run_id)
        if run is None:
            raise ValueError(f"Run 不存在：{run_id}")
        if run["execution"]["stage"] not in {"finished", "closed"}:
            raise ValueError("Run 必须 finished 后才能记录证据")
        for ref in refs:
            if ref not in runs:
                raise ValueError(f"Evidence 引用 Run 不存在：{ref}")
        levels = _levels_from_events(events)
        from_level = levels.get(run_id, "none")
        if (
            to_level in {"single_run", "confirmed"}
            and _is_bound_run(run)
        ):
            _verify_live_bound_output_seal_locked(control, run)
        _validate_transition_semantics(
            tasks,
            levels,
            runs,
            run,
            from_level,
            to_level,
            refs,
            live_write=True,
        )
        event = {
            "event_id": f"EVT-{len(events) + 1:04d}",
            "subject_type": "Run",
            "subject_id": run_id,
            "from": from_level,
            "to": to_level,
            "evidence_refs": refs,
            "reason": reason_value,
            "proposed_by": proposed_value,
            "checked_by": checked_value,
            "applied_by": applied_value,
            "time": _utc_now(),
        }
        validate_evidence_event(event, expected_number=len(events) + 1)
        ledger = control / "evidence.jsonl"
        line = (
            json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        if len(previous) + len(line) > DEFAULT_JSON_LIMIT:
            raise ValueError("evidence.jsonl 超过 1 MiB 上限")
        atomic_write_bytes(
            ledger, previous + line, transaction_id=uuid.uuid4().hex
        )
        return deepcopy(event)


def current_evidence_level(project: Path, run_id: str) -> str:
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        from .validation import validate_v2_state_locked

        _tasks, runs, events, _content = validate_v2_state_locked(control)
        if run_id not in runs:
            raise ValueError(f"Run 不存在：{run_id}")
        return _level_from_events(events, run_id)


def current_evidence_level_locked(
    run_id: str,
    *,
    events: list[dict[str, Any]],
) -> str:
    return _level_from_events(events, run_id)


def validate_evidence_ledger_locked(
    control: Path,
    known_runs: dict[str, dict[str, Any]] | None = None,
    known_tasks: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], bytes]:
    loaded_objects = known_runs is None or known_tasks is None
    if known_runs is None:
        from .runs import validate_runs_locked

        runs = validate_runs_locked(control)
    else:
        runs = known_runs
    if known_tasks is None:
        from .tasking import validate_tasks_locked

        tasks = validate_tasks_locked(control)
    else:
        tasks = known_tasks
    if loaded_objects:
        from .runs import validate_task_run_links_locked

        validate_task_run_links_locked(tasks, runs)
    content = read_bounded_regular_file(
        control / "evidence.jsonl",
        DEFAULT_JSON_LIMIT,
        "evidence.jsonl",
    )
    events: list[dict[str, Any]] = []
    levels: dict[str, str] = {}
    if content:
        if not content.endswith(b"\n"):
            raise ValueError("evidence.jsonl 每条事件必须以换行结束")
        for number, line in enumerate(content.splitlines(), start=1):
            if not line:
                raise ValueError("evidence.jsonl 不允许空行")
            event = parse_json_object_bytes(line, "evidence event")
            validate_evidence_event(event, expected_number=number)
            subject_id = event["subject_id"]
            if subject_id not in runs:
                raise ValueError(f"Evidence subject Run 不存在：{subject_id}")
            for run_id in event["evidence_refs"]:
                if run_id not in runs:
                    raise ValueError(f"Evidence 引用 Run 不存在：{run_id}")
            from_level = levels.get(subject_id, "none")
            to_level = event["to"]
            if event["from"] != from_level:
                raise ValueError(f"Evidence from 与现场等级不一致：{event['event_id']}")
            _validate_transition_semantics(
                tasks,
                levels,
                runs,
                runs[subject_id],
                from_level,
                to_level,
                event["evidence_refs"],
            )
            levels[subject_id] = to_level
            events.append(event)
    return events, runs, content


def validate_evidence_event(
    event: dict[str, Any], *, expected_number: int
) -> None:
    if not isinstance(event, dict) or set(event) != EVIDENCE_EVENT_FIELDS:
        raise ValueError("Evidence event 字段无效")
    expected_id = f"EVT-{expected_number:04d}"
    if event.get("event_id") != expected_id or EVENT_ID.fullmatch(expected_id) is None:
        raise ValueError(f"Evidence event_id 无效：{event.get('event_id')}")
    if event.get("subject_type") != "Run":
        raise ValueError("Evidence subject_type 首版只能是 Run")
    if not isinstance(event.get("subject_id"), str) or RUN_ID.fullmatch(
        event["subject_id"]
    ) is None:
        raise ValueError("Evidence subject_id 无效")
    from_level = event.get("from")
    to_level = event.get("to")
    if from_level not in EVIDENCE_FORWARD or to_level not in EVIDENCE_FORWARD:
        raise ValueError("Evidence from/to 无效")
    _run_refs(event.get("evidence_refs"))
    _text(event.get("reason"), "reason")
    _text(event.get("proposed_by"), "proposed_by")
    _optional_text(event.get("checked_by"), "checked_by")
    _text(event.get("applied_by"), "applied_by")
    _timestamp(event.get("time"))


def _validate_transition_semantics(
    tasks: dict[str, dict[str, Any]],
    levels: dict[str, str],
    runs: dict[str, dict[str, Any]],
    run: dict[str, Any],
    from_level: str,
    to_level: str,
    evidence_refs: list[str],
    *,
    live_write: bool = False,
) -> None:
    if to_level not in EVIDENCE_FORWARD[from_level]:
        raise ValueError(
            f"Evidence 不能从 {from_level} 转到 {to_level}：{run['id']}"
        )
    purpose = run["purpose"]
    if purpose == "debug" and to_level not in {"debug", "disqualified"}:
        raise ValueError("debug Run 只能进入 debug 或 disqualified，不能升级")
    if purpose == "evidence" and to_level == "debug":
        raise ValueError("evidence Run 不能登记为 debug")
    if from_level == "none" and evidence_refs != [run["id"]]:
        raise ValueError("首次 Evidence 事件只能引用当前 subject Run")
    if from_level == "none" and to_level == "single_run":
        if purpose != "evidence":
            raise ValueError("none→single_run 只允许 evidence Run")
        _require_credible_single_run(run, live_write=live_write)
        task = tasks.get(run["task_id"])
        if task is None:
            raise ValueError(f"Evidence 所属 Task 不存在：{run['task_id']}")
        debug_required = task["route_inputs"].get("debug_required", True)
        if not isinstance(debug_required, bool):
            raise ValueError("route_inputs.debug_required 必须是 bool")
        if debug_required and not _has_closed_debug(
            levels, runs, run["task_id"]
        ):
            raise ValueError("none→single_run 前必须已有 debug 闭环")
    if live_write and to_level in {"single_run", "confirmed"}:
        from .run_identity import is_bound_frozen

        if not is_bound_frozen(run.get("frozen")):
            raise ValueError(
                "未绑定 Codebase 的 Run 不能新产生 single_run/confirmed 论文证据"
            )


def _require_credible_single_run(
    run: dict[str, Any],
    *,
    live_write: bool = False,
) -> None:
    from .run_identity import is_bound_frozen

    if is_bound_frozen(run.get("frozen")):
        if not isinstance(run.get("output_seal"), dict):
            raise ValueError(
                "绑定 Codebase 的 Run 在正式输出封存完成前不能进入 single_run"
            )
    elif live_write:
        raise ValueError(
            "未绑定 Codebase 的 Run 不能新产生 single_run 论文证据"
        )
    execution = run["execution"]
    if execution["stage"] not in {"finished", "closed"}:
        raise ValueError("single_run 必须来自已完成 Run")
    if execution["outcome"] != "succeeded":
        raise ValueError("single_run 必须来自 succeeded Run")
    if run["quality"]["implementation"] != "valid":
        raise ValueError("single_run 的 implementation 必须是 valid")
    if any(
        status in {"invalid", "uncertain"}
        for status in run["quality"].values()
    ):
        raise ValueError("single_run 的实现与质量检查必须可信")


def _verify_live_bound_output_seal_locked(
    control: Path,
    run: dict[str, Any],
) -> None:
    from .execution_snapshot import verify_run_execution_snapshot
    from .output_seal import _verify_output_tree
    from .run_identity import is_bound_frozen

    if not is_bound_frozen(run.get("frozen")):
        raise ValueError(
            "未绑定 Codebase 的 Run 不能新产生论文证据"
        )
    seal = run.get("output_seal")
    if not isinstance(seal, dict):
        raise ValueError("正式输出尚未封存，不能登记论文证据")
    verify_run_execution_snapshot(control, run)
    _verify_output_tree(control.parent, run, seal)


def _is_bound_run(run: dict[str, Any]) -> bool:
    from .run_identity import is_bound_frozen

    return is_bound_frozen(run.get("frozen"))


def _has_closed_debug(
    levels: dict[str, str],
    runs: dict[str, dict[str, Any]],
    task_id: str,
) -> bool:
    for run_id, level in levels.items():
        if level != "debug":
            continue
        debug_run = runs[run_id]
        if (
            debug_run["task_id"] == task_id
            and debug_run["purpose"] == "debug"
            and debug_run["execution"]["stage"] == "closed"
        ):
            return True
    return False


def levels_from_events(events: list[dict[str, Any]]) -> dict[str, str]:
    levels: dict[str, str] = {}
    for event in events:
        levels[event["subject_id"]] = event["to"]
    return levels


_levels_from_events = levels_from_events


def _level_from_events(events: list[dict[str, Any]], run_id: str) -> str:
    level = "none"
    for event in events:
        if event["subject_id"] == run_id:
            level = event["to"]
    return level


def _run_refs(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) != len(set(value))
        or not all(isinstance(item, str) and RUN_ID.fullmatch(item) for item in value)
    ):
        raise ValueError("evidence_refs 必须是非空、去重的 Run ID 数组")
    return list(value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 4096:
        raise ValueError(f"{label} 必须是 1..4096 字符的非空字符串")
    return value.strip()


def _optional_text(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value.strip()) > 4096:
        raise ValueError(f"{label} 必须是字符串")
    return value.strip()


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Evidence time 必须是 ISO 时间字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Evidence time 必须是 ISO 时间字符串") from error
    if parsed.tzinfo is None:
        raise ValueError("Evidence time 必须带时区")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
