from __future__ import annotations

from pathlib import Path
from typing import Any

from .attempts import (
    RESULT_SCHEMA_V2,
    _promotion_evaluation_locked,
    _result_is_promotion_eligible,
    validate_project_workflow_locked,
)
from .locking import project_snapshot_lock
from .policy import (
    ADAPTER_SCHEMA_V1,
    audit_runtime_adapter_locked,
    load_adapter,
)
from .project import PROJECT_SCHEMA, PROJECT_SCHEMA_V2
from .records import _initialized_project, _read_object, _validate_project
from .releases import console_workflow_lock_trust
from .templates import FORMAL_ASSET_SCHEMA, FORMAL_TRIAL_SCHEMA
from .tasking import TASK_ROUTES


RANK = {
    "structural_blocker": 0,
    "implementation_confidence": 1,
    "confirmation_gap": 2,
    "optional_extension": 3,
}
INTAKE_PREVIEW_SCOPE = {
    "persistence": False,
    "domain_pack": False,
    "codebase_registry": False,
    "base_verification": False,
    "task_creation": False,
}


def status(project: Path) -> dict[str, Any]:
    """在不修改文件的项目锁快照中返回账本事实。"""
    snapshot = _read_snapshot(project)
    if snapshot["layout"] == "v2":
        return _v2_status_payload(snapshot)
    return _status_payload(snapshot)


def task_list(
    project: Path, task_id: str | None = None
) -> dict[str, Any]:
    """从 v2 项目的同一只读快照派生 Task 清单。"""
    snapshot = _read_snapshot(project)
    if snapshot["layout"] != "v2":
        raise ValueError("task-list 只支持 v2 项目")
    from .checklists import derive_task_list

    tasks = snapshot["tasks"]
    if task_id is not None:
        from .tasking import _task_id

        _task_id(task_id)
        task = tasks.get(task_id)
        if task is None:
            raise ValueError(f"Task 不存在：{task_id}")
        tasks = {task_id: task}
    payload = derive_task_list(
        tasks, snapshot["runs"], snapshot["events"]
    )
    return payload


def plan_next(project: Path) -> dict[str, Any]:
    """从同一只读快照派生最小建议，不创建对象或执行动作。"""
    snapshot = _read_planning_snapshot(project)
    suggestions = snapshot["suggestions"]
    if snapshot["layout"] == "v1":
        suggestions.sort(key=_suggestion_order)
    return {
        "schema": f"cv-experiment-workflow.plan.{snapshot['layout']}",
        "suggestions": suggestions,
        "warnings": snapshot["warnings"],
    }


def coordinator_context(project: Path) -> dict[str, Any]:
    """返回 Coordinator 可见的最小事实，不包含源码、日志或仓库路径。"""
    return coordinator_context_from_snapshot(_read_snapshot(project))


def coordinator_context_from_snapshot(
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """从已经验证的快照纯派生 Coordinator 上下文。"""
    adapter = snapshot["adapter"]
    capabilities = sorted(
        name
        for name, enabled in adapter["capabilities"].items()
        if enabled
    )
    if snapshot["layout"] == "v2":
        unfinished = [
            {
                "id": task_id,
                "route": task["route"],
                "stage": task["stage"],
                "target_refs": list(task["target_refs"]),
                "route_supported": task["route"] in TASK_ROUTES,
                "remaining_runs": _remaining_runs(task),
                "execution_authorized": (
                    task["route_inputs"].get("execution_authorized") is True
                ),
                "plan_unchanged": (
                    task["route_inputs"].get("plan_unchanged") is True
                ),
            }
            for task_id, task in sorted(snapshot["tasks"].items())
            if task["stage"] != "done"
        ]
        current_version = None
    else:
        unfinished = []
        active_versions = _ids_with_status(snapshot["versions"], "active")
        current_version = active_versions[0] if len(active_versions) == 1 else None
    return {
        "layout": snapshot["layout"],
        "project_standard": {
            "project_name": snapshot["project"]["name"],
            "adapter_status": adapter["status"],
            "capabilities": capabilities,
        },
        "current_version": current_version,
        "unfinished_tasks": unfinished,
    }


def intake_discovery_from_snapshot(
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """从同一快照纯派生 intake 与控制台共用的发现事实。"""
    return {
        **coordinator_context_from_snapshot(snapshot),
        "preview_scope": dict(INTAKE_PREVIEW_SCOPE),
    }


def read_intake_discovery(project: Path) -> dict[str, Any]:
    """只读一次项目快照并派生启动检查事实。"""
    return intake_discovery_from_snapshot(_read_snapshot(project))


def read_console_snapshot(project: Path) -> dict[str, Any]:
    """在一把只读锁内取得控制台所需的完整 v2 事实快照。"""
    root = _initialized_project(project)
    with project_snapshot_lock(root):
        control = _validate_project(
            root,
            expected_schema=PROJECT_SCHEMA_V2,
            validate_workflow_lock=False,
        )
        project_payload = _read_object(control / "project.json")
        workflow_lock = _read_object(control / "workflow.lock.json")
        lock_trust = console_workflow_lock_trust(workflow_lock)

        from .validation import validate_v2_state_and_catalog_locked

        tasks, runs, events, _evidence_content, catalog = (
            validate_v2_state_and_catalog_locked(control)
        )
        from .codebases import validate_codebases_locked

        codebases = validate_codebases_locked(control)
        adapter = load_adapter(control)
        adapter_runtime = audit_runtime_adapter_locked(root, adapter)
        return {
            "layout": "v2",
            "project": project_payload,
            "workflow_lock": workflow_lock,
            "workflow_lock_trust": lock_trust,
            "adapter": adapter,
            "adapter_runtime": adapter_runtime,
            "codebases": codebases,
            "catalog": catalog,
            "tasks": tasks,
            "runs": runs,
            "events": events,
        }


def _remaining_runs(task: dict[str, Any]) -> int | None:
    max_runs = task["budget"].get("max_runs")
    if type(max_runs) is not int or max_runs <= 0:
        return None
    return max(0, max_runs - len(task["run_refs"]))


def _run_budget_exhausted(task: dict[str, Any]) -> bool:
    return (
        task["stage"] in {"queued", "preparing", "ready", "stopped"}
        and _remaining_runs(task) == 0
    )


def _read_snapshot(project: Path) -> dict[str, Any]:
    root = _initialized_project(project)
    with project_snapshot_lock(root):
        return _read_snapshot_locked(root)


def _read_planning_snapshot(project: Path) -> dict[str, Any]:
    root = _initialized_project(project)
    with project_snapshot_lock(root):
        snapshot = _read_snapshot_locked(root)
        if snapshot["layout"] == "v2":
            suggestions = _derive_v2_suggestions(snapshot["tasks"])
        else:
            suggestions = _derive_suggestions(snapshot)
        return {
            "layout": snapshot["layout"],
            "suggestions": suggestions,
            "warnings": snapshot["warnings"],
        }


def _read_snapshot_locked(root: Path) -> dict[str, Any]:
    project = _read_object(root / ".experiment-workflow" / "project.json")
    schema = project.get("schema")
    if schema == PROJECT_SCHEMA_V2:
        return _read_v2_snapshot_locked(root, project)
    if schema != PROJECT_SCHEMA:
        raise ValueError("项目 schema 不是受支持的 v1/v2")
    control = _validate_project(root)
    validation = validate_project_workflow_locked(control)
    ideas = _json_records(control / "ideas")
    attempts = _json_records(control / "attempts")
    versions = _json_records(control / "versions")
    trials = {
        path.name: _read_object(path / "trial.json")
        for path in sorted(
            (control / "trials").iterdir(), key=lambda item: item.name,
        )
    }
    assets = {
        path.name: _read_object(path / "asset.json")
        for path in sorted(
            (control / "code-assets").iterdir(), key=lambda item: item.name,
        )
    }
    adapter = load_adapter(control)
    return {
        "layout": "v1",
        "control": control,
        "project": project,
        "adapter": adapter,
        "validation": validation,
        "ideas": ideas,
        "trials": trials,
        "assets": assets,
        "attempts": attempts,
        "versions": versions,
        "warnings": _warnings(
            adapter=adapter,
            trials=trials,
            attempts=attempts,
        ),
    }


def _read_v2_snapshot_locked(
    root: Path, project: dict[str, Any]
) -> dict[str, Any]:
    control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
    from .validation import validate_v2_full_snapshot_locked

    (
        tasks,
        runs,
        events,
        _evidence_content,
        catalog,
        research_briefs,
        research_packages,
    ) = validate_v2_full_snapshot_locked(control)
    return {
        "layout": "v2",
        "control": control,
        "project": project,
        "adapter": load_adapter(control),
        "tasks": tasks,
        "runs": runs,
        "events": events,
        "catalog": catalog,
        "research_briefs": research_briefs,
        "research_packages": research_packages,
        "warnings": [],
    }


def _json_records(directory: Path) -> dict[str, dict[str, Any]]:
    return {
        path.stem: _read_object(path)
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name)
    }


def _status_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    validation = snapshot["validation"]
    counts = {
        key: value
        for key, value in sorted(validation.items())
        if key not in {"schema", "valid"}
    }
    return {
        "schema": "cv-experiment-workflow.status.v1",
        "valid": True,
        "counts": counts,
        "active_idea_ids": _ids_with_status(snapshot["ideas"], "active"),
        "active_version_ids": _ids_with_status(snapshot["versions"], "active"),
        "planned_attempt_ids": _ids_with_status(snapshot["attempts"], "planned"),
        "completed_attempt_ids": _ids_with_status(snapshot["attempts"], "completed"),
        "draft_version_ids": _ids_with_status(snapshot["versions"], "draft"),
        "warnings": snapshot["warnings"],
    }


def _v2_status_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    from .checklists import derive_task_list
    from .evidence import EVIDENCE_FORWARD, levels_from_events
    from .runs import RUN_OUTCOMES, RUN_STAGE_FORWARD
    from .tasking import TASK_FORWARD

    tasks = snapshot["tasks"]
    runs = snapshot["runs"]
    events = snapshot["events"]
    unfinished = sorted(
        task_id for task_id, task in tasks.items() if task["stage"] != "done"
    )
    blocked = [
        {
            "task_id": task_id,
            "stage": task["stage"],
            "reason": (
                "run_budget_exhausted"
                if _run_budget_exhausted(task)
                else (
                    "task_stopped"
                    if task["stage"] == "stopped"
                    else "prerequisites_pending"
                )
            ),
        }
        for task_id, task in sorted(tasks.items())
        if task["stage"] in {"preparing", "stopped"}
        or _run_budget_exhausted(task)
    ]
    levels = levels_from_events(events)
    catalog = snapshot["catalog"]
    evidence_by_run = {
        run_id: levels.get(run_id, "none") for run_id in sorted(runs)
    }
    research_briefs = snapshot.get("research_briefs", {})
    research_packages = snapshot.get("research_packages", {})
    latest_brief = (
        research_briefs[sorted(research_briefs)[-1]]
        if research_briefs
        else None
    )
    return {
        "schema": "cv-experiment-workflow.status.v2",
        "valid": True,
        "catalog_counts": {
            name: len(catalog[name])
            for name in ("sources", "ideas", "templates", "modules")
        },
        "research_briefs": {
            "count": len(research_briefs),
            "latest": (
                {
                    "brief_id": latest_brief["brief_id"],
                    "revision": latest_brief["revision"],
                    "title": latest_brief["title"],
                    "created_at": latest_brief["created_at"],
                }
                if latest_brief is not None
                else None
            ),
        },
        "research_packages": _research_package_status(
            research_packages,
            levels,
        ),
        "ready_idea_ids": sorted(
            idea_id
            for idea_id, idea in catalog["ideas"].items()
            if idea["status"] == "ready"
        ),
        "ready_module_ids": sorted(
            module_id
            for module_id, module in catalog["modules"].items()
            if module["status"] == "ready"
        ),
        "task_stage_counts": _counts(tasks.values(), "stage", TASK_FORWARD),
        "unfinished_task_ids": unfinished,
        "run_stage_counts": _counts(
            (run["execution"] for run in runs.values()),
            "stage",
            RUN_STAGE_FORWARD,
        ),
        "run_outcome_counts": _counts(
            (run["execution"] for run in runs.values()),
            "outcome",
            RUN_OUTCOMES,
        ),
        "run_evidence": evidence_by_run,
        "evidence_level_counts": {
            level: sum(value == level for value in evidence_by_run.values())
            for level in sorted(EVIDENCE_FORWARD)
        },
        "continuable_task_ids": [
            task_id
            for task_id in unfinished
            if not _run_budget_exhausted(tasks[task_id])
        ],
        "blocked": blocked,
        "task_list": derive_task_list(
            tasks, runs, events, evidence_levels=levels
        ),
    }


def _research_package_status(
    packages: dict[str, dict[str, Any]],
    evidence_levels: dict[str, str],
) -> dict[str, Any]:
    reverse = {
        package["supersedes_package_id"]: package_id
        for package_id, package in packages.items()
        if package["supersedes_package_id"] is not None
    }
    return {
        "count": len(packages),
        "items": [
            {
                "package_id": package_id,
                "readiness": package["readiness"],
                "mode": package["asset_mode"],
                "supersedes": package["supersedes_package_id"],
                "superseded_by": reverse.get(package_id),
                "source_revoked": any(
                    evidence_levels.get(run_id) == "revoked"
                    for run_id in package["source_run_refs"]
                ),
                "missing_requirements": list(
                    package["missing_requirements"]
                ),
            }
            for package_id, package in packages.items()
        ],
    }


def _counts(
    records: Any, field: str, allowed: Any
) -> dict[str, int]:
    values = list(records)
    return {
        name: sum(item[field] == name for item in values)
        for name in sorted(allowed)
    }


def _derive_v2_suggestions(
    tasks: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    actions = {
        "queued": ("prepare-task", "task_not_prepared", "preparing", False),
        "preparing": (
            "complete-prerequisites", "prerequisites_pending", "ready", False,
        ),
        "ready": ("run-task", "task_ready", "executing", True),
        "executing": ("workflow-status", "run_in_progress", "executing", False),
        "reviewing": ("finish-review", "review_pending", "done", False),
        "stopped": ("continue", "task_stopped", "preparing", True),
    }
    suggestions = []
    for task_id, task in sorted(tasks.items()):
        if task["stage"] == "done":
            continue
        if _run_budget_exhausted(task):
            suggestions.append(
                {
                    "task_id": task_id,
                    "stage": task["stage"],
                    "action": "new-task",
                    "reason": "run_budget_exhausted",
                    "next_stage": None,
                    "requires_authorization": True,
                }
            )
            continue
        action, reason, next_stage, authorization = actions[task["stage"]]
        suggestions.append(
            {
                "task_id": task_id,
                "stage": task["stage"],
                "action": action,
                "reason": reason,
                "next_stage": next_stage,
                "requires_authorization": authorization,
            }
        )
    if not suggestions:
        suggestions.append(
            {
                "task_id": None,
                "stage": None,
                "action": "choose-route",
                "reason": "no_unfinished_task",
                "next_stage": None,
                "requires_authorization": False,
            }
        )
    return suggestions


def _ids_with_status(records: dict[str, dict[str, Any]], status: str) -> list[str]:
    return sorted(
        record_id
        for record_id, payload in records.items()
        if payload.get("status") == status
    )


def _warnings(
    *,
    adapter: dict[str, Any],
    trials: dict[str, dict[str, Any]],
    attempts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if adapter["schema"] == ADAPTER_SCHEMA_V1:
        warnings.append(_warning(
            "implicit_confirmation_policy",
            [{"kind": "adapter", "id": "adapter"}],
        ))
    legacy_formal = [
        trial_id for trial_id, trial in trials.items()
        if trial.get("schema") == FORMAL_TRIAL_SCHEMA and "idea_revision" not in trial
    ]
    if legacy_formal:
        warnings.append(_warning(
            "legacy_formal_trial",
            [{"kind": "trial", "id": trial_id} for trial_id in legacy_formal],
        ))
    legacy_results = [
        attempt_id for attempt_id, attempt in attempts.items()
        if attempt.get("status") == "completed"
        and "schema" not in attempt.get("result", {})
    ]
    if legacy_results:
        warnings.append(_warning(
            "legacy_result",
            [{"kind": "attempt", "id": attempt_id} for attempt_id in legacy_results],
        ))
    return sorted(
        warnings,
        key=lambda item: (
            item["code"],
            tuple((target["kind"], target["id"]) for target in item["targets"]),
        ),
    )


def _warning(code: str, targets: list[dict[str, str]]) -> dict[str, Any]:
    return {"code": code, "targets": targets}


def _derive_suggestions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    ideas = snapshot["ideas"]
    attempts = snapshot["attempts"]
    versions = snapshot["versions"]
    suggestions: list[dict[str, Any]] = []

    if not ideas:
        suggestions.append(_suggestion(
            "optional_extension", "project", "workflow", "new-idea",
            "project_has_no_idea", [], "low", "research_direction",
            ["experiment_chain"], False,
        ))

    for idea_id, idea in ideas.items():
        if idea["status"] == "draft":
            suggestions.append(_suggestion(
                "structural_blocker", "idea", idea_id, "clarify-idea",
                "idea_is_draft", [], "low", "idea_definition",
                ["trial_creation"], False,
            ))

    for asset_id, asset in snapshot["assets"].items():
        if (
            asset.get("schema") == FORMAL_ASSET_SCHEMA
            and asset.get("external_code_ref") is None
        ):
            suggestions.append(_suggestion(
                "structural_blocker", "code_asset", asset_id, "sync-code-asset",
                "formal_code_asset_unbound", ["implemented_code_commit"],
                "unknown", "implementation_identity", ["attempt_creation"], True,
            ))

    rooted_trial_ids = _rooted_trial_ids(attempts)
    for trial_id, trial in snapshot["trials"].items():
        if trial.get("status") == "active" and trial_id not in rooted_trial_ids:
            suggestions.append(_suggestion(
                "structural_blocker", "trial", trial_id, "prepare_attempt",
                "trial_without_attempt",
                ["confirm_seed", "confirm_command", "confirm_config"],
                "unknown", "experimental_outcome", ["result", "promotion"], True,
            ))

    for attempt_id, attempt in attempts.items():
        if attempt["status"] == "planned":
            suggestions.append(_suggestion(
                "structural_blocker", "attempt", attempt_id,
                "run-and-record-result", "attempt_is_planned",
                ["confirm_command_config_resources"], "unknown",
                "experimental_outcome", ["result", "promotion"], True,
            ))

    promoted_attempts = {
        attempt_id
        for version in versions.values()
        for attempt_id in version.get("accepted_attempt_ids", [])
    }
    covered_attempt_ids: set[str] = set()
    for attempt_id, attempt in attempts.items():
        if attempt["status"] != "completed":
            continue
        result = attempt["result"]
        if result.get("schema") == RESULT_SCHEMA_V2 and result["implementation_status"] in {
            "invalid", "uncertain",
        }:
            suggestions.append(_suggestion(
                "implementation_confidence", "attempt", attempt_id,
                "verify-implementation",
                f"implementation_{result['implementation_status']}",
                ["inspect_code_tests_and_runtime_evidence"], "unknown",
                "implementation_correctness", ["scientific_interpretation"], True,
            ))
        if (
            attempt_id in promoted_attempts
            or attempt_id in covered_attempt_ids
            or not _result_is_promotion_eligible(result)
        ):
            continue
        evaluation = _promotion_evaluation_locked(snapshot["control"], attempt_id)
        qualified = tuple(evaluation["qualified_attempt_ids"])
        covered_attempt_ids.update(qualified)
        target_id = min(qualified)
        if evaluation["eligible"]:
            suggestions.append(_suggestion(
                "optional_extension", "attempt", target_id, "promotion-check",
                "promotion_evidence_ready", ["review_claim_scope"], "low",
                "promotion_readiness", [], False,
            ))
        else:
            suggestions.append(_suggestion(
                "confirmation_gap", "attempt", target_id,
                "add-confirmation-attempt", "confirmation_policy_not_met",
                list(evaluation["reasons"]), "unknown", "result_repeatability",
                ["promotion"], True,
            ))

    for version_id, version in versions.items():
        if version["status"] == "draft":
            suggestions.append(_suggestion(
                "structural_blocker", "version", version_id, "activate-version",
                "promotion_version_is_draft", ["integrated_code_commit"],
                "unknown", "integrated_version_identity", ["version_use"], True,
            ))
    return suggestions


def _rooted_trial_ids(attempts: dict[str, dict[str, Any]]) -> set[str]:
    rooted: set[str] = set()
    for attempt in attempts.values():
        target = attempt["target"]
        while target["kind"] == "attempt":
            target = attempts[target["id"]]["target"]
        if target["kind"] == "trial":
            rooted.add(target["id"])
    return rooted


def _suggestion(
    kind: str,
    target_kind: str,
    target_id: str,
    action: str,
    reason: str,
    prerequisites: list[str],
    cost: str,
    uncertainty_reduction: str,
    blocks: list[str],
    requires_authorization: bool,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "target": {"kind": target_kind, "id": target_id},
        "action": action,
        "reason": reason,
        "prerequisites": prerequisites,
        "cost": cost,
        "uncertainty_reduction": uncertainty_reduction,
        "blocks": blocks,
        "requires_authorization": requires_authorization,
    }


def _suggestion_order(item: dict[str, Any]) -> tuple[int, str, int, str]:
    target = item["target"]
    suffix = target["id"].rsplit("-", 1)[-1]
    number = int(suffix) if suffix.isdigit() else 0
    return RANK[item["kind"]], target["kind"], number, item["action"]
