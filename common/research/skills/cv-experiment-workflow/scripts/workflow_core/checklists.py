from __future__ import annotations

from typing import Any

from .evidence import levels_from_events


TASK_LIST_SCHEMA = "cv-experiment-workflow.task-list.v1"
CHECKLIST_IDS = (
    "objective",
    "readiness",
    "debug",
    "evidence",
    "review",
    "close",
)
ROUTE_LABELS = {
    "tune": (
        "明确调参目标、候选范围与停止条件",
        "通过调参开跑检查",
        "完成最小调试运行",
        "完成正式调参证据运行",
        "分析指标并审核调参结论",
        "收尾并记录本次配置结果",
    ),
    "ablation": (
        "明确消融模块、关闭行为与启用侧对照",
        "通过消融实验开跑检查",
        "完成关闭侧最小调试运行",
        "完成正式消融证据运行",
        "核对启用/关闭差异并审核结论",
        "收尾并记录启用/关闭差异",
    ),
    "reproduction": (
        "锁定来源、目标、容差、代码和数据标准",
        "通过复现实验开跑检查",
        "完成最小复现调试运行",
        "完成正式复现证据运行",
        "记录真实差异并审核是否落入容差",
        "收尾并记录复现结果",
    ),
    "innovation": (
        "明确创新假设、对照条件与停止条件",
        "通过创新实验开跑检查",
        "完成最小调试运行",
        "完成正式创新证据运行",
        "分析结果并审核创新结论",
        "收尾并记录创新结论",
    ),
}


def derive_task_list(
    tasks: dict[str, dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    evidence_levels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """从已经验证的 v2 快照派生只读 Task 清单。"""
    levels = (
        levels_from_events(events)
        if evidence_levels is None
        else evidence_levels
    )
    runs_by_task: dict[str, list[dict[str, Any]]] = {}
    for run in runs.values():
        task_id = run.get("task_id")
        if task_id in tasks:
            runs_by_task.setdefault(task_id, []).append(run)
    task_items = [
        _task_item(task, runs_by_task.get(task["id"], []), levels)
        for _task_id, task in sorted(tasks.items())
    ]
    return {
        "schema": TASK_LIST_SCHEMA,
        "summary": {
            "total": len(tasks),
            "unfinished": sum(task["stage"] != "done" for task in tasks.values()),
            "done": sum(task["stage"] == "done" for task in tasks.values()),
            "stopped": sum(task["stage"] == "stopped" for task in tasks.values()),
        },
        "tasks": task_items,
    }


def _task_item(
    task: dict[str, Any],
    task_runs: list[dict[str, Any]],
    levels: dict[str, str],
) -> dict[str, Any]:
    stage = task["stage"]
    statuses = ["pending"] * len(CHECKLIST_IDS)
    budget_exhausted = _run_budget_exhausted(task)
    conclusion = task.get("conclusion")
    debug_scope_done = (
        stage == "done"
        and isinstance(conclusion, dict)
        and conclusion.get("scope") == "debug_only"
        and conclusion.get("reason") == "stop_condition_met"
    )

    if stage in {"preparing", "ready", "executing", "reviewing", "done"} or task_runs:
        statuses[0] = "done"
    if stage in {"ready", "executing", "reviewing", "done"} or task_runs:
        statuses[1] = "done"
    if task["route_inputs"].get("debug_required") is False or _has_completed_run(
        task_runs, levels, purpose="debug", evidence_levels={"debug"}
    ):
        statuses[2] = "done"
    if _has_completed_run(
        task_runs,
        levels,
        purpose="evidence",
        evidence_levels={"single_run", "confirmed"},
    ):
        statuses[3] = "done"
    if stage == "done":
        statuses[4] = "done"
        statuses[5] = "done"
    elif stage == "reviewing":
        statuses[4] = "current"

    if stage in {"reviewing", "done"}:
        _mark_first_required_fact_blocked(statuses)
    elif stage == "stopped":
        _mark_first(statuses, "pending", "blocked")
    elif budget_exhausted:
        _mark_first(statuses, "pending", "blocked")
    elif stage not in {"reviewing", "done"}:
        _mark_first(statuses, "pending", "current")

    try:
        labels = list(ROUTE_LABELS[task["route"]])
    except KeyError as error:
        raise ValueError(f"Task route 无效：{task['route']}") from error
    if debug_scope_done:
        statuses[3] = "done"
        labels[3] = "按 single_debug 停止条件结束；正式证据需要新建 Task"
    checklist = [
        {"id": step_id, "label": label, "status": status}
        for step_id, label, status in zip(CHECKLIST_IDS, labels, statuses)
    ]
    active = next(
        (
            step
            for step in checklist
            if step["status"] in {"blocked", "current"}
        ),
        None,
    )
    if active is None:
        active = next(
            (step for step in checklist if step["status"] == "pending"),
            None,
        )
    next_step = active["label"] if active is not None else "已完成全部清单步骤"
    if (
        stage == "done"
        and active is not None
        and active["status"] == "blocked"
    ):
        next_step = f"任务已标记完成，但记录不完整：{active['label']}"
    elif budget_exhausted:
        next_step = "本任务运行预算已用完；如需正式证据，请新建 Task"
    elif debug_scope_done:
        next_step = "已完成本 Task 的 debug 范围"
    return {
        "task_id": task["id"],
        "owner_request": task["owner_request"],
        "route": task["route"],
        "stage": stage,
        "progress": {
            "done": sum(status == "done" for status in statuses),
            "total": len(statuses),
        },
        "next_step": next_step,
        "checklist": checklist,
    }


def _run_budget_exhausted(task: dict[str, Any]) -> bool:
    budget = task.get("budget")
    max_runs = budget.get("max_runs") if isinstance(budget, dict) else None
    run_refs = task.get("run_refs")
    return (
        type(max_runs) is int
        and max_runs > 0
        and isinstance(run_refs, list)
        and len(run_refs) >= max_runs
        and task.get("stage") in {"queued", "preparing", "ready", "stopped"}
    )


def _has_completed_run(
    runs: list[dict[str, Any]],
    levels: dict[str, str],
    *,
    purpose: str,
    evidence_levels: set[str],
) -> bool:
    return any(
        run["purpose"] == purpose
        and run["execution"]["stage"] == "closed"
        and run["execution"]["outcome"] == "succeeded"
        and levels.get(run["id"], "none") in evidence_levels
        for run in runs
    )


def _mark_first(statuses: list[str], old: str, new: str) -> None:
    for index, status in enumerate(statuses):
        if status == old:
            statuses[index] = new
            return


def _mark_first_required_fact_blocked(statuses: list[str]) -> None:
    for index in (2, 3):
        if statuses[index] == "pending":
            statuses[index] = "blocked"
            return
