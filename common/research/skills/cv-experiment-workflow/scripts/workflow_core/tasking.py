from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from .io import atomic_create_json, atomic_write_json, read_bounded_json_object
from .locking import project_write_lock
from .project import PROJECT_SCHEMA_V2, is_link_or_reparse
from .records import (
    _initialized_project,
    _validate_project,
)


TASK_ID = re.compile(r"TASK-[0-9]{4}")
TASK_ROUTES = {"tune", "ablation", "reproduction", "innovation"}
_COMMON_ROUTE_INPUT_FIELDS = {
    "config",
    "seed",
    "code_binding",
    "debug_required",
    "changes_code_behavior",
    "code_verified",
    "execution_authorized",
    "plan_unchanged",
    "primary_metric",
    "baseline",
    "framework_experiment",
}
_ROUTE_INPUT_FIELDS = {
    "tune": {"allowed_changes", "baseline_run_ref"},
    "ablation": {"module_ref", "baseline_run_ref", "disabled_behavior"},
    "reproduction": {
        "source_ref",
        "source_run_ref",
        "tolerance",
        "code_standard",
        "data_standard",
    },
    "innovation": {"source_run_ref"},
}
TASK_FIELDS = {
    "id",
    "owner_request",
    "route",
    "target_refs",
    "route_inputs",
    "budget",
    "stop_condition",
    "run_refs",
    "stage",
    "conclusion",
}
TASK_FORWARD = {
    "queued": {"preparing", "stopped"},
    "preparing": {"ready", "stopped"},
    "ready": {"executing", "stopped"},
    "executing": {"reviewing", "stopped"},
    "reviewing": {"done", "preparing", "ready", "stopped"},
    "stopped": {"preparing"},
    "done": set(),
}
_PREPARING_ISSUES = {"implementation", "prerequisite"}
_READY_ISSUES = {"environment", "temporary_resource"}
CODE_BINDING_FIELDS = {"codebase_id", "branch", "commit", "tag"}


def create_task(
    project: Path,
    *,
    owner_request: str,
    route: str,
    target_refs: list[str],
    route_inputs: dict[str, Any],
    budget: dict[str, Any],
    stop_condition: dict[str, Any],
) -> dict[str, Any]:
    owner_request = _text(owner_request, "owner_request")
    route = _text(route, "route")
    targets = _string_list(target_refs, "target_refs")
    inputs = _json_object(route_inputs, "route_inputs")
    budget_value = _json_object(budget, "budget")
    stop_value = _json_object(stop_condition, "stop_condition")
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _validate_command_root_locked(control)
        task_id = _next_task_id(control / "tasks")
        payload = {
            "id": task_id,
            "owner_request": owner_request,
            "route": route,
            "target_refs": targets,
            "route_inputs": inputs,
            "budget": budget_value,
            "stop_condition": stop_value,
            "run_refs": [],
            "stage": "queued",
            "conclusion": None,
        }
        validate_task(payload, expected_id=task_id)
        from .codebases import validate_codebases_locked

        validate_task_codebase_links_locked(
            {task_id: payload},
            validate_codebases_locked(control),
        )
        if route == "innovation":
            from .validation import load_v2_catalog_locked

            _validate_innovation_catalog_refs(
                payload,
                load_v2_catalog_locked(control),
                known_tasks=validate_tasks_locked(control),
            )
        destination = control / "tasks" / f"{task_id}.json"
        if not atomic_create_json(
            destination, payload, transaction_id=uuid.uuid4().hex
        ):
            raise FileExistsError(f"Task 已存在，拒绝覆盖：{task_id}")
        return deepcopy(payload)


def load_task(project: Path, task_id: str) -> dict[str, Any]:
    _task_id(task_id)
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _validate_command_root_locked(control)
        task = _load_task_locked(control, task_id)
        _validate_task_run_refs_locked(control, task)
        return deepcopy(task)


def transition_task(
    project: Path,
    task_id: str,
    to_stage: str,
    *,
    issue_kind: str | None = None,
    conclusion: object | None = None,
) -> dict[str, Any]:
    _task_id(task_id)
    if to_stage not in TASK_FORWARD:
        raise ValueError(f"Task stage 无效：{to_stage}")
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _validate_command_root_locked(control)
        current = _load_task_locked(control, task_id)
        _validate_task_run_refs_locked(control, current)
        from_stage = current["stage"]
        if from_stage == "preparing" and to_stage == "ready":
            raise ValueError("preparing→ready 必须通过 readiness")
        if to_stage not in TASK_FORWARD[from_stage]:
            raise ValueError(
                f"Task 不能从 {from_stage} 转到 {to_stage}：{task_id}"
            )
        _validate_issue_return(from_stage, to_stage, issue_kind)
        if to_stage != "done" and conclusion is not None:
            raise ValueError("只有 done Task 可以保存 conclusion")
        if to_stage == "done" and conclusion is None:
            conclusion = {}
        updated = deepcopy(current)
        updated["stage"] = to_stage
        if conclusion is not None:
            updated["conclusion"] = deepcopy(conclusion)
        validate_task(updated, expected_id=task_id)
        atomic_write_json(
            control / "tasks" / f"{task_id}.json",
            updated,
            transaction_id=uuid.uuid4().hex,
        )
        return deepcopy(updated)


def readiness(
    project: Path,
    task_id: str,
    *,
    target_clear: bool,
    template_runnable: bool,
    mutation_allowed: bool,
    code_verified: bool,
) -> dict[str, Any]:
    _task_id(task_id)
    checks = _readiness_checks(target_clear, template_runnable, mutation_allowed, code_verified)
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _validate_command_root_locked(control)
        task, reasons = _evaluate_readiness_locked(control, task_id, checks, {"preparing"})
        if reasons:
            return {"status": "fail", "reasons": reasons}
        updated = deepcopy(task)
        updated["stage"] = "ready"
        validate_task(updated, expected_id=task_id)
        atomic_write_json(
            control / "tasks" / f"{task_id}.json",
            updated,
            transaction_id=uuid.uuid4().hex,
        )
        return {"status": "pass"}


def evaluate_readiness(
    project: Path,
    task_id: str,
    *,
    target_clear: bool,
    template_runnable: bool,
    mutation_allowed: bool,
    code_verified: bool,
) -> dict[str, Any]:
    _task_id(task_id)
    checks = _readiness_checks(target_clear, template_runnable, mutation_allowed, code_verified)
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _validate_command_root_locked(control)
        _task, reasons = _evaluate_readiness_locked(
            control,
            task_id,
            checks,
            {"queued", "preparing", "ready"},
        )
        return {"status": "fail", "reasons": reasons} if reasons else {"status": "pass"}


def _readiness_checks(
    *values: object,
) -> dict[str, bool]:
    if len(values) != 4 or not all(isinstance(value, bool) for value in values):
        raise ValueError("readiness 检查值必须是 bool")
    names = ("target_clear", "template_runnable", "mutation_allowed", "code_verified")
    return dict(zip(names, values))  # type: ignore[arg-type, return-value]


def _evaluate_readiness_locked(
    control: Path,
    task_id: str,
    checks: dict[str, bool],
    allowed_stages: set[str],
) -> tuple[dict[str, Any], list[str]]:
    task = _load_task_locked(control, task_id)
    _validate_task_run_refs_locked(control, task)
    if task["stage"] not in allowed_stages:
        raise ValueError(f"readiness 只接收 {'/'.join(sorted(allowed_stages))} Task")
    return task, _readiness_reasons(control, task, **checks)


def validate_task(payload: dict[str, Any], *, expected_id: str) -> None:
    if not isinstance(payload, dict) or set(payload) != TASK_FIELDS:
        raise ValueError(f"Task 字段无效：{expected_id}")
    _task_id(expected_id)
    if payload.get("id") != expected_id:
        raise ValueError(f"Task id 与文件名不一致：{expected_id}")
    _text(payload.get("owner_request"), "owner_request")
    route = _text(payload.get("route"), "route")
    if route not in TASK_ROUTES:
        raise ValueError(
            f"Task route 无效：{route}；当前只支持四类实验路线"
        )
    target_refs = _string_list(payload.get("target_refs"), "target_refs")
    inputs = _json_object(payload.get("route_inputs"), "route_inputs")
    _validate_route_inputs(route, inputs, target_refs)
    _json_object(payload.get("budget"), "budget")
    _json_object(payload.get("stop_condition"), "stop_condition")
    run_refs = _string_list(payload.get("run_refs"), "run_refs")
    if len(run_refs) != len(set(run_refs)) or any(
        re.fullmatch(r"RUN-[0-9]{4}", item) is None for item in run_refs
    ):
        raise ValueError(f"Task run_refs 无效：{expected_id}")
    if payload.get("stage") not in TASK_FORWARD:
        raise ValueError(f"Task stage 无效：{expected_id}")
    conclusion = payload.get("conclusion")
    if payload["stage"] == "done":
        if conclusion is None:
            raise ValueError(f"done Task 必须保存 conclusion：{expected_id}")
        _canonical_json(conclusion, "conclusion")
    elif conclusion is not None:
        raise ValueError(f"非 done Task 的 conclusion 必须是 null：{expected_id}")


def validate_tasks_locked(control: Path) -> dict[str, dict[str, Any]]:
    directory = control / "tasks"
    if is_link_or_reparse(directory) or not directory.is_dir():
        raise ValueError(f"Task 目录无效：{directory}")
    tasks: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if (
            path.suffix != ".json"
            or TASK_ID.fullmatch(path.stem) is None
            or is_link_or_reparse(path)
            or not path.is_file()
        ):
            raise ValueError(f"Task 目录存在未知对象：{path}")
        payload = read_bounded_json_object(path, label="Task")
        validate_task(payload, expected_id=path.stem)
        tasks[path.stem] = payload
    return tasks


def validate_task_prerequisite_links_locked(
    tasks: dict[str, dict[str, Any]],
) -> None:
    settled_stages = {"ready", "executing", "reviewing", "done"}
    for task_id, task in tasks.items():
        for reference in task["target_refs"]:
            if TASK_ID.fullmatch(reference) is None:
                continue
            if reference == task_id:
                raise ValueError(f"Task 前置引用不能指向自己：{task_id}")
            if task["stage"] not in settled_stages:
                continue
            prerequisite = tasks.get(reference)
            if prerequisite is None or prerequisite["stage"] != "done":
                raise ValueError(f"Task 前置引用尚未完成：{task_id}/{reference}")


def validate_task_catalog_links_locked(
    tasks: dict[str, dict[str, Any]],
    catalog: dict[str, dict[str, dict[str, Any]]],
) -> None:
    for task in tasks.values():
        if task["route"] == "innovation":
            _validate_innovation_catalog_refs(
                task, catalog, known_tasks=tasks
            )


def validate_task_codebase_links_locked(
    tasks: dict[str, dict[str, Any]],
    codebases: dict[str, dict[str, Any]],
) -> None:
    for task_id, task in tasks.items():
        binding = task["route_inputs"].get("code_binding")
        if binding is None:
            continue
        codebase_id = binding["codebase_id"]
        if codebase_id not in codebases:
            raise ValueError(
                f"Task 绑定的 Codebase 不存在：{task_id}/{codebase_id}"
            )


def _validate_innovation_catalog_refs(
    task: dict[str, Any],
    catalog: dict[str, dict[str, dict[str, Any]]],
    *,
    known_tasks: dict[str, dict[str, Any]],
) -> None:
    from .v2_catalog import IDEA_ID, MODULE_ID, SOURCE_ID, TEMPLATE_ID

    if "framework_experiment" in task["route_inputs"]:
        _validate_framework_experiment_binding(
            task["route_inputs"]["framework_experiment"],
            "innovation",
            task["target_refs"],
        )
        inputs = task["route_inputs"]
        if not _finite_number(inputs.get("baseline")):
            raise ValueError("innovation route_inputs.baseline 必须是有限数字")
        primary = inputs.get("primary_metric")
        if (
            not isinstance(primary, str)
            or primary != primary.strip()
            or not 1 <= len(primary) <= 256
        ):
            raise ValueError("innovation route_inputs.primary_metric 无效")
        if inputs.get("changes_code_behavior") is not True:
            raise ValueError("innovation 必须设置 changes_code_behavior=true")
        return

    idea_ids: list[str] = []
    template_ids: list[str] = []
    module_ids: list[str] = []
    if len(task["target_refs"]) != len(set(task["target_refs"])):
        raise ValueError("innovation target_refs 不得重复")
    for reference in task["target_refs"]:
        if TASK_ID.fullmatch(reference):
            if reference not in known_tasks:
                raise ValueError(
                    f"innovation target_refs 的前置 Task 不存在：{reference}"
                )
            continue
        if IDEA_ID.fullmatch(reference):
            if reference not in catalog["ideas"]:
                raise ValueError(f"innovation target_refs 的 Idea 不存在：{reference}")
            idea_ids.append(reference)
        elif TEMPLATE_ID.fullmatch(reference):
            if reference not in catalog["templates"]:
                raise ValueError(
                    f"innovation target_refs 的 Template 不存在：{reference}"
                )
            template_ids.append(reference)
        elif MODULE_ID.fullmatch(reference):
            if reference not in catalog["modules"]:
                raise ValueError(
                    f"innovation target_refs 的 Module 不存在：{reference}"
                )
            module_ids.append(reference)
        elif SOURCE_ID.fullmatch(reference):
            raise ValueError(
                "innovation target_refs 不能用 Source 或 code_snapshot 冒充 "
                f"Idea/Template/Module：{reference}"
            )
        else:
            raise ValueError(
                f"innovation target_refs 存在未知或悬空对象：{reference}"
            )
    if not idea_ids or not template_ids or not module_ids:
        raise ValueError(
            "innovation target_refs 至少需要一个 ready Idea、"
            "一个 v2 Template 和一个 ready research Module"
        )
    for idea_id in idea_ids:
        if catalog["ideas"][idea_id]["status"] != "ready":
            raise ValueError(f"innovation 目标 Idea 尚未 ready：{idea_id}")
    for module_id in module_ids:
        module = catalog["modules"][module_id]
        if module["status"] != "ready" or module["kind"] != "research":
            raise ValueError(
                f"innovation 目标 Module 必须是 ready research：{module_id}"
            )
        if not set(module["idea_refs"]).intersection(idea_ids):
            raise ValueError(
                f"innovation 目标 Module 未对应目标 Idea：{module_id}"
            )
        if module["attachment"]["template_ref"] not in template_ids:
            raise ValueError(
                f"innovation 目标 Module 未对应目标 Template：{module_id}"
            )
    for idea_id in idea_ids:
        if not any(
            idea_id in catalog["modules"][module_id]["idea_refs"]
            for module_id in module_ids
        ):
            raise ValueError(f"目标 Idea 没有对应的目标 Module：{idea_id}")
    for template_id in template_ids:
        if not any(
            catalog["modules"][module_id]["attachment"]["template_ref"]
            == template_id
            for module_id in module_ids
        ):
            raise ValueError(
                f"目标 Template 没有对应的目标 Module：{template_id}"
            )
    inputs = task["route_inputs"]
    baseline = inputs.get("baseline")
    if not _finite_number(baseline):
        raise ValueError("innovation route_inputs.baseline 必须是有限数字")
    primary = inputs.get("primary_metric")
    if (
        not isinstance(primary, str)
        or primary != primary.strip()
        or not 1 <= len(primary) <= 256
    ):
        raise ValueError("innovation route_inputs.primary_metric 无效")
    if inputs.get("changes_code_behavior") is not True:
        raise ValueError("innovation 必须设置 changes_code_behavior=true")


def _load_task_locked(control: Path, task_id: str) -> dict[str, Any]:
    path = control / "tasks" / f"{task_id}.json"
    if is_link_or_reparse(path) or not path.is_file():
        raise ValueError(f"Task 不存在或不是普通文件：{task_id}")
    payload = read_bounded_json_object(path, label="Task")
    validate_task(payload, expected_id=task_id)
    return payload


def _attach_run_locked(control: Path, task_id: str, run_id: str) -> None:
    current = _load_task_locked(control, task_id)
    if run_id in current["run_refs"]:
        raise ValueError(f"Task 已引用 Run：{task_id}/{run_id}")
    updated = deepcopy(current)
    updated["run_refs"].append(run_id)
    validate_task(updated, expected_id=task_id)
    atomic_write_json(
        control / "tasks" / f"{task_id}.json",
        updated,
        transaction_id=uuid.uuid4().hex,
    )


def _validate_task_run_refs_locked(
    control: Path, task: dict[str, Any]
) -> None:
    from .runs import _load_run_locked, _validate_task_run_code_binding

    for run_id in task["run_refs"]:
        run = _load_run_locked(control, run_id)
        if run["task_id"] != task["id"]:
            raise ValueError(f"Task/Run 引用不一致：{task['id']}/{run_id}")
        _validate_task_run_code_binding(task, run["frozen"])


def _next_task_id(directory: Path) -> str:
    numbers: list[int] = []
    for path in directory.iterdir():
        if (
            path.suffix != ".json"
            or TASK_ID.fullmatch(path.stem) is None
            or is_link_or_reparse(path)
            or not path.is_file()
        ):
            raise ValueError(f"Task ID 路径无效：{path}")
        numbers.append(int(path.stem.split("-")[1]))
    number = max(numbers, default=0) + 1
    if number > 9999:
        raise ValueError("Task ID 已耗尽")
    return f"TASK-{number:04d}"


def _validate_command_root_locked(control: Path) -> None:
    from .validation import validate_v2_root_locked

    validate_v2_root_locked(control)


def _validate_issue_return(
    from_stage: str, to_stage: str, issue_kind: str | None
) -> None:
    if from_stage == "reviewing" and to_stage == "preparing":
        if issue_kind not in _PREPARING_ISSUES:
            raise ValueError("回 preparing 只用于 implementation/prerequisite 问题")
        return
    if from_stage == "reviewing" and to_stage == "ready":
        if issue_kind not in _READY_ISSUES:
            raise ValueError("回 ready 只用于 environment/temporary_resource 问题")
        return
    if issue_kind is not None:
        raise ValueError("issue_kind 只用于 reviewing 的失败回退")


def _readiness_reasons(
    control: Path,
    task: dict[str, Any],
    *,
    target_clear: bool,
    template_runnable: bool,
    mutation_allowed: bool,
    code_verified: bool,
) -> list[str]:
    reasons: list[str] = []
    if not target_clear or not task["target_refs"]:
        reasons.append("目标和比较对象不清楚")
    if not template_runnable:
        reasons.append("模板还不能运行")
    if not mutation_allowed:
        reasons.append("本次改动超出允许范围")
    if not task["budget"] or not task["stop_condition"]:
        reasons.append("预算或停止条件没有写明")
    if not code_verified:
        reasons.append("代码变化缺少应有的测试或审核")
    for reference in dict.fromkeys(task["target_refs"]):
        if TASK_ID.fullmatch(reference) is None:
            continue
        if reference == task["id"]:
            reasons.append(f"前置 Task 不能引用自己：{reference}")
            continue
        path = control / "tasks" / f"{reference}.json"
        if not os.path.lexists(path):
            reasons.append(f"前置 Task 不存在：{reference}")
            continue
        prerequisite = _load_task_locked(control, reference)
        if prerequisite["stage"] != "done":
            reasons.append(f"前置 Task 未完成：{reference}")
    return reasons


def _task_id(value: object) -> str:
    if not isinstance(value, str) or TASK_ID.fullmatch(value) is None:
        raise ValueError(f"Task ID 无效：{value}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 4096:
        raise ValueError(f"{label} 必须是 1..4096 字符的非空字符串")
    return value.strip()


def _string_list(value: object, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > 4096
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ValueError(f"{label} 必须是字符串数组")
    return [item.strip() for item in value]


def _json_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是 JSON object")
    normalized = _canonical_json(value, label)
    if not isinstance(normalized, dict):
        raise ValueError(f"{label} 必须是 JSON object")
    return normalized


def _canonical_json(value: object, label: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        normalized = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} 必须是有限 JSON 值") from error
    if normalized != value:
        raise ValueError(f"{label} 序列化后会改变，拒绝写入")
    return normalized


def _finite_number(value: object) -> bool:
    import math

    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _validate_route_inputs(
    route: str,
    inputs: dict[str, Any],
    target_refs: list[str],
) -> None:
    unknown = set(inputs) - _COMMON_ROUTE_INPUT_FIELDS - _ROUTE_INPUT_FIELDS[route]
    if unknown:
        raise ValueError(
            f"{route} route_inputs 存在未知字段：{', '.join(sorted(unknown))}"
        )
    if "code_binding" in inputs:
        _validate_code_binding(inputs["code_binding"])
    framework_experiment = inputs.get("framework_experiment")
    if framework_experiment is not None:
        _validate_framework_experiment_binding(
            framework_experiment,
            route,
            target_refs,
        )
        _route_metric_contract(inputs, route)
        _validate_framework_route_contract(route, inputs)
        return
    declared_targets = set(target_refs)
    if route == "tune":
        baseline_run_ref = inputs.get("baseline_run_ref")
        if (
            baseline_run_ref is not None
            and baseline_run_ref not in set(target_refs)
        ):
            raise ValueError(
                "tune target_refs 必须包含 route_inputs.baseline_run_ref"
            )
    elif route == "ablation":
        module_ref = inputs.get("module_ref")
        baseline_run_ref = inputs.get("baseline_run_ref")
        _route_id(module_ref, "MOD", "ablation module_ref")
        _route_id(baseline_run_ref, "RUN", "ablation baseline_run_ref")
        if not {module_ref, baseline_run_ref}.issubset(declared_targets):
            raise ValueError(
                "ablation target_refs 必须包含 module_ref 与 baseline_run_ref"
            )
        _route_text(
            inputs.get("disabled_behavior"),
            "ablation disabled_behavior",
        )
        _route_metric_contract(inputs, "ablation")
    elif route == "reproduction":
        source_ref = inputs.get("source_ref")
        source_run_ref = inputs.get("source_run_ref")
        _route_id(source_ref, "SRC", "reproduction source_ref")
        _route_id(source_run_ref, "RUN", "reproduction source_run_ref")
        if not {source_ref, source_run_ref}.issubset(declared_targets):
            raise ValueError(
                "reproduction target_refs 必须包含 source_ref 与 source_run_ref"
            )
        _route_metric_contract(inputs, "reproduction")
        tolerance = inputs.get("tolerance")
        if not _finite_number(tolerance) or tolerance < 0:
            raise ValueError(
                "reproduction route_inputs.tolerance 必须是有限非负数"
            )
        for field in ("code_standard", "data_standard"):
            if not isinstance(inputs.get(field), dict):
                raise ValueError(
                    f"reproduction route_inputs.{field} 必须是 JSON object"
                )
    elif route == "innovation":
        source_run_ref = inputs.get("source_run_ref")
        if source_run_ref is not None:
            _route_id(
                source_run_ref,
                "RUN",
                "innovation source_run_ref",
            )
            if source_run_ref not in declared_targets:
                raise ValueError(
                    "innovation target_refs 必须包含 route_inputs.source_run_ref"
                )


def _validate_code_binding(value: object) -> None:
    if not isinstance(value, dict) or set(value) != CODE_BINDING_FIELDS:
        raise ValueError(
            "route_inputs.code_binding 必须严格包含 "
            "codebase_id/branch/commit/tag"
        )
    codebase_id = value["codebase_id"]
    if (
        not isinstance(codebase_id, str)
        or re.fullmatch(r"CB-[0-9]{4}", codebase_id) is None
    ):
        raise ValueError("code_binding.codebase_id 必须是 CB-0001 形式")
    branch = value["branch"]
    if (
        not isinstance(branch, str)
        or branch != branch.strip()
        or not branch
        or len(branch) > 4096
        or any(
            unicodedata.category(character).startswith("C")
            for character in branch
        )
    ):
        raise ValueError("code_binding.branch 必须是规范非空字符串")
    commit = value["commit"]
    if (
        not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
    ):
        raise ValueError("code_binding.commit 必须是 40 位小写 hex")
    tag = value["tag"]
    if tag is not None and (
        not isinstance(tag, str)
        or tag != tag.strip()
        or not tag
        or len(tag) > 4096
        or any(
            unicodedata.category(character).startswith("C")
            for character in tag
        )
    ):
        raise ValueError("code_binding.tag 必须是 null 或规范非空字符串")


def _validate_framework_experiment_binding(
    value: object,
    route: str,
    target_refs: list[str],
) -> None:
    fields = {"id", "route", "framework_slug", "idea_ref"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(
            "framework_experiment 必须严格包含 "
            "id/route/framework_slug/idea_ref"
        )
    experiment_id = value["id"]
    prefixes = {
        "tune": "tune",
        "ablation": "ablate",
        "reproduction": "repro",
        "innovation": "innov",
    }
    prefix = prefixes[route]
    if (
        not isinstance(experiment_id, str)
        or re.fullmatch(
            rf"{prefix}-[0-9]{{3}}-[a-z0-9][a-z0-9-]{{0,62}}",
            experiment_id,
        )
        is None
        or experiment_id not in target_refs
    ):
        raise ValueError("framework_experiment.id 与 route/target_refs 不一致")
    if value["route"] != route:
        raise ValueError("framework_experiment.route 与 Task route 不一致")
    framework_slug = value["framework_slug"]
    if (
        not isinstance(framework_slug, str)
        or re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,62}",
            framework_slug,
        )
        is None
    ):
        raise ValueError("framework_experiment.framework_slug 无效")
    idea_ref = value["idea_ref"]
    if route == "innovation":
        if (
            not isinstance(idea_ref, str)
            or re.fullmatch(r"IDEA-[0-9]{4}", idea_ref) is None
        ):
            raise ValueError("创新 framework_experiment 必须绑定 Idea")
    elif idea_ref is not None:
        raise ValueError("非创新 framework_experiment 不得绑定 Idea")


def _validate_framework_route_contract(
    route: str,
    inputs: dict[str, Any],
) -> None:
    """校验 Framework 门面的四类实验语义，不冒充中央目录里的 SRC/MOD/RUN。"""

    if route == "reproduction":
        for field in ("source_ref", "source_run_ref"):
            _route_text(inputs.get(field), f"reproduction {field}")
        tolerance = inputs.get("tolerance")
        if not _finite_number(tolerance) or tolerance < 0:
            raise ValueError(
                "reproduction route_inputs.tolerance 必须是有限非负数"
            )
        for field in ("code_standard", "data_standard"):
            value = inputs.get(field)
            if not isinstance(value, dict) or not value:
                raise ValueError(
                    f"reproduction route_inputs.{field} 必须是非空 JSON object"
                )
    elif route == "ablation":
        for field in (
            "module_ref",
            "baseline_run_ref",
            "disabled_behavior",
        ):
            _route_text(inputs.get(field), f"ablation {field}")
    elif route == "tune":
        allowed = inputs.get("allowed_changes")
        if (
            not isinstance(allowed, list)
            or not allowed
            or not all(isinstance(item, str) and item.strip() for item in allowed)
        ):
            raise ValueError("tune allowed_changes 必须是非空字符串列表")
    elif route == "innovation":
        source_run_ref = inputs.get("source_run_ref")
        if source_run_ref is not None:
            _route_text(source_run_ref, "innovation source_run_ref")


def _route_metric_contract(inputs: dict[str, Any], route: str) -> None:
    if not _finite_number(inputs.get("baseline")):
        raise ValueError(f"{route} route_inputs.baseline 必须是有限数字")
    primary = inputs.get("primary_metric")
    if (
        not isinstance(primary, str)
        or primary != primary.strip()
        or not 1 <= len(primary) <= 256
    ):
        raise ValueError(f"{route} route_inputs.primary_metric 无效")


def _route_id(value: object, prefix: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(rf"{prefix}-[0-9]{{4}}", value) is None
    ):
        raise ValueError(f"{label} 无效")


def _route_text(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 4096
    ):
        raise ValueError(f"{label} 无效")
