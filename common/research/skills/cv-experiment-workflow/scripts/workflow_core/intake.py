from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import routes
from .planning import read_intake_discovery
from .tasking import validate_task


INTAKE_RESULT_SCHEMA = "cv-experiment-workflow.intake-result.v1"
INTENTS = {
    "status",
    "continue",
    "tune",
    "ablation",
    "reproduction",
    "innovation",
}

_COMMON_REQUIRED = [
    "domain_task.domain",
    "domain_task.task",
    "dataset.source",
    "dataset.split",
    "research_goal",
    "base_candidate.kind",
    "base_candidate.reference",
    "primary_metric",
    "compute_budget.max_runs",
    "compute_budget.max_hours",
    "compute_budget.max_gpus",
    "stop_condition",
    "route_details",
]
_ROUTE_REQUIRED = {
    "tune": [],
    "ablation": [
        "route_details.baseline",
        "route_details.module_ref",
        "route_details.baseline_run_ref",
        "route_details.disabled_behavior",
    ],
    "reproduction": [
        "route_details.baseline",
        "route_details.source_ref",
        "route_details.source_run_ref",
        "route_details.tolerance",
        "route_details.code_standard",
        "route_details.data_standard",
    ],
    "innovation": [
        "route_details.baseline",
        "route_details.idea_refs",
        "route_details.template_refs",
        "route_details.module_refs",
    ],
}
_ROUTE_ALLOWED = {
    route: {path.rsplit(".", 1)[-1] for path in required}
    for route, required in _ROUTE_REQUIRED.items()
}
_ROUTE_ALLOWED["tune"] = {
    "config",
    "allowed_changes",
    "baseline",
    "baseline_run_ref",
}
_ROUTE_ALLOWED["innovation"].add("source_run_ref")
_EXPERIMENT_FIELDS = {
    "domain_task",
    "dataset",
    "research_goal",
    "base_candidate",
    "primary_metric",
    "compute_budget",
    "stop_condition",
    "route_details",
}
def evaluate_intake(
    *,
    intent: str,
    provided: dict[str, Any],
    discovered: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate one startup request without reading or writing external state."""
    if intent not in INTENTS:
        raise ValueError(f"intent 无效：{intent}")
    if not isinstance(provided, dict):
        raise ValueError("provided 必须是 JSON object")
    if not isinstance(discovered, dict):
        raise ValueError("discovered 必须是 JSON object")

    if intent == "status":
        _reject_unknown_fields(provided, set(), "provided")
        return _result(
            intent=intent,
            route=None,
            required=[],
            provided=provided,
            discovered=discovered,
            missing=[],
            can_continue=True,
            next_step="show_status",
        )
    if intent == "continue":
        return _evaluate_continue(provided, discovered)

    _reject_unknown_fields(provided, _EXPERIMENT_FIELDS, "provided")
    required = [*_COMMON_REQUIRED, *_ROUTE_REQUIRED[intent]]
    missing = _missing_experiment_fields(intent, provided)
    if not missing:
        _validate_shared_task_contract(intent, provided)
    return _result(
        intent=intent,
        route=intent,
        required=required,
        provided=provided,
        discovered=discovered,
        missing=missing,
        can_continue=not missing,
        next_step=(
            "review_read_only_confirmation"
            if not missing
            else "provide_missing_inputs"
        ),
    )


def check_intake(
    project: Path,
    *,
    intent: str,
    provided: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read one validated project snapshot and evaluate its startup request."""
    discovered = read_intake_discovery(Path(project))
    return evaluate_intake(
        intent=intent,
        provided={} if provided is None else provided,
        discovered=discovered,
    )


def _evaluate_continue(
    provided: dict[str, Any],
    discovered: dict[str, Any],
) -> dict[str, Any]:
    _reject_unknown_fields(provided, {"task_ref"}, "provided")
    unfinished = discovered.get("unfinished_tasks", [])
    if not isinstance(unfinished, list):
        raise ValueError("discovered.unfinished_tasks 必须是 array")
    task_ids: list[str] = []
    for item in unfinished:
        if not isinstance(item, dict) or not _id(item.get("id"), "TASK"):
            raise ValueError("discovered.unfinished_tasks 存在无效 Task")
        task_ids.append(item["id"])
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("discovered.unfinished_tasks 存在重复 Task")

    required = ["unfinished_task"]
    selected = provided.get("task_ref")
    if not task_ids:
        missing = ["unfinished_task"]
        can_continue = False
        next_step = "choose_experiment_route"
    elif len(task_ids) == 1 and selected is None:
        missing = []
        can_continue = True
        next_step = "review_read_only_confirmation"
    else:
        required.append("task_ref")
        if selected in task_ids:
            missing = []
            can_continue = True
            next_step = "review_read_only_confirmation"
        else:
            missing = ["task_ref"]
            can_continue = False
            next_step = "choose_task"
    return _result(
        intent="continue",
        route=None,
        required=required,
        provided=provided,
        discovered=discovered,
        missing=missing,
        can_continue=can_continue,
        next_step=next_step,
    )


def _missing_experiment_fields(
    route: str,
    provided: dict[str, Any],
) -> list[str]:
    missing: list[str] = []
    domain_task = _mapping(provided.get("domain_task"))
    if domain_task is not None:
        _reject_unknown_fields(
            domain_task, {"domain", "task"}, "domain_task",
        )
    if domain_task is None or not _text(domain_task.get("domain")):
        missing.append("domain_task.domain")
    if domain_task is None or not _text(domain_task.get("task")):
        missing.append("domain_task.task")

    dataset = _mapping(provided.get("dataset"))
    if dataset is not None:
        _reject_unknown_fields(dataset, {"source", "split"}, "dataset")
    if dataset is None or not _text(dataset.get("source")):
        missing.append("dataset.source")
    if dataset is None or not _text(dataset.get("split")):
        missing.append("dataset.split")

    if not _text(provided.get("research_goal")):
        missing.append("research_goal")

    base = _mapping(provided.get("base_candidate"))
    if base is not None:
        _reject_unknown_fields(
            base, {"kind", "reference"}, "base_candidate",
        )
    if base is None or base.get("kind") not in {"template", "accepted_version"}:
        missing.append("base_candidate.kind")
    if base is None or not _text(base.get("reference")):
        missing.append("base_candidate.reference")

    if not _metric(provided.get("primary_metric")):
        missing.append("primary_metric")

    budget = _mapping(provided.get("compute_budget"))
    if budget is not None:
        _reject_unknown_fields(
            budget,
            {"max_runs", "max_hours", "max_gpus"},
            "compute_budget",
        )
    if budget is None or not _positive_int(budget.get("max_runs")):
        missing.append("compute_budget.max_runs")
    if budget is None or not _positive_number(budget.get("max_hours")):
        missing.append("compute_budget.max_hours")
    if budget is None or not _nonnegative_int(budget.get("max_gpus")):
        missing.append("compute_budget.max_gpus")

    stop_condition = provided.get("stop_condition")
    if not _nonempty_json_object(stop_condition):
        missing.append("stop_condition")

    details = provided.get("route_details")
    if not isinstance(details, dict):
        missing.append("route_details")
        return missing
    _reject_unknown_fields(
        details,
        _ROUTE_ALLOWED[route],
        "route_details",
    )
    missing.extend(_missing_route_fields(route, details))
    return missing


def _missing_route_fields(
    route: str,
    details: dict[str, Any],
) -> list[str]:
    if route == "tune":
        missing: list[str] = []
        if "config" in details and not _json_object(details["config"]):
            missing.append("route_details.config")
        if (
            "allowed_changes" in details
            and not _finite_json(details["allowed_changes"])
        ):
            missing.append("route_details.allowed_changes")
        if (
            "baseline" in details
            and not _finite_number(details["baseline"])
        ):
            missing.append("route_details.baseline")
        baseline_run_ref = details.get("baseline_run_ref")
        if (
            "baseline_run_ref" in details
            and baseline_run_ref is not None
            and not _id(baseline_run_ref, "RUN")
        ):
            missing.append("route_details.baseline_run_ref")
        return missing
    missing: list[str] = []
    if not _finite_number(details.get("baseline")):
        missing.append("route_details.baseline")
    if route == "ablation":
        if not _id(details.get("module_ref"), "MOD"):
            missing.append("route_details.module_ref")
        if not _id(details.get("baseline_run_ref"), "RUN"):
            missing.append("route_details.baseline_run_ref")
        if not _text(details.get("disabled_behavior")):
            missing.append("route_details.disabled_behavior")
    elif route == "reproduction":
        if not _id(details.get("source_ref"), "SRC"):
            missing.append("route_details.source_ref")
        if not _id(details.get("source_run_ref"), "RUN"):
            missing.append("route_details.source_run_ref")
        tolerance = details.get("tolerance")
        if not _finite_number(tolerance) or tolerance < 0:
            missing.append("route_details.tolerance")
        if not _json_object(details.get("code_standard")):
            missing.append("route_details.code_standard")
        if not _json_object(details.get("data_standard")):
            missing.append("route_details.data_standard")
    elif route == "innovation":
        for field, prefix in (
            ("idea_refs", "IDEA"),
            ("template_refs", "TPL"),
            ("module_refs", "MOD"),
        ):
            if not _id_list(details.get(field), prefix):
                missing.append(f"route_details.{field}")
        source_run_ref = details.get("source_run_ref")
        if (
            source_run_ref is not None
            and not _id(source_run_ref, "RUN")
        ):
            missing.append("route_details.source_run_ref")
    return missing


def _validate_shared_task_contract(
    route: str,
    provided: dict[str, Any],
) -> None:
    details = provided["route_details"]
    if route == "ablation":
        targets = [details["module_ref"], details["baseline_run_ref"]]
    elif route == "reproduction":
        targets = [details["source_ref"], details["source_run_ref"]]
    elif route == "innovation":
        targets = [
            *details["idea_refs"],
            *details["template_refs"],
            *details["module_refs"],
        ]
        source_run_ref = details.get("source_run_ref")
        if source_run_ref is not None:
            targets.append(source_run_ref)
    else:
        targets = [provided["base_candidate"]["reference"]]
        baseline_run_ref = details.get("baseline_run_ref")
        if baseline_run_ref is not None:
            targets.append(baseline_run_ref)

    route_inputs = deepcopy(details)
    if route == "innovation":
        for field in ("idea_refs", "template_refs", "module_refs"):
            route_inputs.pop(field)
    route_inputs.setdefault("config", {})
    route_inputs["primary_metric"] = provided["primary_metric"]
    route_inputs["changes_code_behavior"] = route == "innovation"
    task = {
        "id": "TASK-0000",
        "owner_request": provided["research_goal"],
        "route": route,
        "target_refs": targets,
        "route_inputs": route_inputs,
        "budget": deepcopy(provided["compute_budget"]),
        "stop_condition": deepcopy(provided["stop_condition"]),
        "run_refs": [],
        "stage": "queued",
        "conclusion": None,
    }
    validate_task(task, expected_id="TASK-0000")
    issues = routes.requires(route, task)
    if issues:
        raise ValueError("共享路线合同拒绝 intake：" + "；".join(issues))


def _result(
    *,
    intent: str,
    route: str | None,
    required: list[str],
    provided: dict[str, Any],
    discovered: dict[str, Any],
    missing: list[str],
    can_continue: bool,
    next_step: str,
) -> dict[str, Any]:
    return {
        "schema": INTAKE_RESULT_SCHEMA,
        "mode": "preview",
        "read_only": True,
        "intent": intent,
        "route": route,
        "required": list(required),
        "provided": deepcopy(provided),
        "discovered": deepcopy(discovered),
        "missing": list(missing),
        "can_continue": can_continue,
        "next_step": next_step,
    }


def _mapping(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _reject_unknown_fields(
    value: dict[str, Any],
    allowed: set[str],
    label: str,
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            f"{label} 存在未知字段：{', '.join(sorted(map(str, unknown)))}"
        )


def _text(value: object) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and 1 <= len(value) <= 4096
    )


def _metric(value: object) -> bool:
    return _text(value) and len(value) <= 256


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _positive_number(value: object) -> bool:
    return _finite_number(value) and value > 0


def _id(value: object, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(rf"{prefix}-[0-9]{{4}}", value) is not None
    )


def _id_list(value: object, prefix: str) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_id(item, prefix) for item in value)
        and len(value) == len(set(value))
    )


def _json_object(value: object) -> bool:
    return isinstance(value, dict) and _finite_json(value)


def _finite_json(value: object) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def _nonempty_json_object(value: object) -> bool:
    return bool(value) and _json_object(value)
