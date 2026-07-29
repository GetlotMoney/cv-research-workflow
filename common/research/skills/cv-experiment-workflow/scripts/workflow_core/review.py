from __future__ import annotations

from copy import deepcopy as _deepcopy
from typing import Any as _Any

from .routes import compare as _compare
from .routes import mutation_class as _mutation_class
from .runs import validate_outcome_issue_kind as _validate_outcome_issue_kind


ROLE_DESCRIPTIONS = {
    "Coordinator": "理解任务并且唯一写入工作流账本",
    "Researcher": "只读分析论文、来源、想法、代码和实验方案",
    "Implementer": "作为同一代码区域唯一写入者完成修改",
    "Runner": "启动、查看、停止并收集一次运行",
    "Analyst": "核对日志、指标、实现和科学结论",
    "Reviewer": "独立进行只读审核",
}
_QUALITY = {"valid", "invalid", "uncertain", "not_applicable"}
_HYPOTHESES = {
    "supported", "not_supported", "inconclusive", "not_evaluated",
}


def roles_for_task(task: dict[str, _Any]) -> list[str]:
    if _mutation_class(task.get("route", ""), task) == "innovation":
        return ["Researcher", "Implementer", "Reviewer", "Runner", "Analyst"]
    return ["Runner", "Analyst"]


def open_session(
    task: dict[str, _Any], run: dict[str, _Any]
) -> list[dict[str, _Any]]:
    run_id = run.get("id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("Agent session 缺少 Run ID")
    return [
        {"id": f"{run_id}:{role}:01", "role": role, "closed": False}
        for role in ["Coordinator", *roles_for_task(task)]
    ]


def close_session(session: list[dict[str, _Any]]) -> None:
    for instance in session:
        if set(instance) != {"id", "role", "closed"}:
            raise ValueError("Agent session 结构无效")
        instance["closed"] = True


def review_parsed_result(
    project: object,
    task: dict[str, _Any],
    run: dict[str, _Any],
    parsed: dict[str, _Any],
) -> dict[str, _Any]:
    reviewed = _validate_parsed(parsed)
    reviewed["comparison"] = _compare(task["route"], task, parsed)
    reviewed["decision"] = _closure_decision(run, reviewed)
    return reviewed


def _validate_parsed(parsed: dict[str, _Any]) -> dict[str, _Any]:
    if not isinstance(parsed, dict) or set(parsed) != {
        "execution",
        "result",
        "quality",
        "analysis",
        "artifacts",
    }:
        raise ValueError("Adapter 结果必须分成 execution/result/quality/analysis/artifacts")
    execution = parsed["execution"]
    result = parsed["result"]
    quality = parsed["quality"]
    analysis = parsed["analysis"]
    if not isinstance(execution, dict) or set(execution) != {
        "outcome", "exit_code", "issue_kind",
    }:
        raise ValueError("Adapter execution 结果无效")
    issue_kind = execution["issue_kind"]
    _validate_outcome_issue_kind(execution["outcome"], issue_kind)
    exit_code = execution["exit_code"]
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int)
    ):
        raise ValueError("Adapter exit_code 无效")
    if not isinstance(result, dict) or set(result) != {"metrics", "raw_log"}:
        raise ValueError("Adapter result 结果无效")
    if not isinstance(result["metrics"], dict) or not (
        result["raw_log"] is None or isinstance(result["raw_log"], str)
    ):
        raise ValueError("Adapter metrics 必须是 JSON object")
    if not isinstance(quality, dict) or set(quality) != {
        "implementation",
        "interface",
        "data",
        "metrics",
    }:
        raise ValueError("Adapter quality 结果无效")
    if not isinstance(analysis, dict) or set(analysis) != {
        "hypothesis",
        "limitations",
        "suggestions",
    }:
        raise ValueError("Adapter analysis 结果无效")
    if any(value not in _QUALITY for value in quality.values()):
        raise ValueError("Adapter quality 判断无效")
    if analysis["hypothesis"] not in _HYPOTHESES:
        raise ValueError("Adapter hypothesis 判断无效")
    if not all(
        isinstance(values, list)
        and all(isinstance(item, str) and item.strip() for item in values)
        for values in (analysis["limitations"], analysis["suggestions"])
    ):
        raise ValueError("Adapter analysis 数组无效")
    if quality["implementation"] == "invalid" and analysis["hypothesis"] != "not_evaluated":
        raise ValueError("实现 invalid 时不能解释科学假设")
    if not isinstance(parsed["artifacts"], list) or not all(
        isinstance(item, str) and item.strip() for item in parsed["artifacts"]
    ):
        raise ValueError("Adapter artifacts 必须是数组")
    return _deepcopy(parsed)


def _closure_decision(
    run: dict[str, _Any], reviewed: dict[str, _Any]
) -> dict[str, _Any]:
    execution = reviewed["execution"]
    quality = reviewed["quality"]
    outcome = execution["outcome"]
    issue_kind = execution["issue_kind"]
    if outcome == "stopped" and issue_kind == "user_stop":
        return {"closure": "stopped", "issue_kind": issue_kind, "reason": "user_stop"}
    if outcome != "succeeded" or any(value != "valid" for value in quality.values()):
        if issue_kind not in {
            "implementation", "prerequisite", "environment", "temporary_resource",
        }:
            issue_kind = "implementation"
        closure = (
            "preparing"
            if issue_kind in {"implementation", "prerequisite"}
            else "ready"
        )
        return {
            "closure": closure,
            "issue_kind": issue_kind,
            "reason": f"{issue_kind}_failed",
        }
    if run["purpose"] == "debug":
        return {
            "closure": "preparing",
            "issue_kind": "prerequisite",
            "reason": "debug_complete",
        }
    return {"closure": "done", "issue_kind": None, "reason": "run_complete"}
