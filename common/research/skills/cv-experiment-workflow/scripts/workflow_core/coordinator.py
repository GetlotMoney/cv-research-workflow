from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .planning import coordinator_context


Interpreter = Callable[[str, dict[str, Any]], dict[str, Any] | None]
INTENTS = {
    "paper",
    "code",
    "tune",
    "ablation",
    "reproduction",
    "innovation",
    "status",
    "continue",
}
ROUTE_TABLE = (
    {"intent": "paper", "route": "paper", "meaning": "论文整理"},
    {"intent": "code", "route": "code", "meaning": "模板或模块接入"},
    {"intent": "tune", "route": "experiment", "meaning": "调参实验"},
    {"intent": "ablation", "route": "experiment", "meaning": "消融实验"},
    {
        "intent": "reproduction",
        "route": "experiment",
        "meaning": "复现实验",
    },
    {
        "intent": "innovation",
        "route": "experiment",
        "meaning": "创新实验",
    },
    {"intent": "status", "route": "management", "meaning": "只读状态"},
    {"intent": "continue", "route": "management", "meaning": "继续原任务"},
)

# 只在 LLM 不可用时使用；保持集中、短小，重叠即澄清。
FALLBACK_KEYWORDS = {
    "paper": ("论文", "文献", "创新点"),
    "code": ("代码", "模板", "接入模块", "接入这个模块"),
    "tune": ("调参", "学习率", "超参数"),
    "ablation": ("消融",),
    "reproduction": ("复现",),
    "innovation": (
        "创新实验",
        "试这个 idea",
        "试这个idea",
        "验证这个 idea",
        "验证这个idea",
    ),
    "status": (),
    "continue": ("继续", "接着"),
}
FALLBACK_EXACT = {
    "创新": "innovation",
    "试这个创新": "innovation",
    "请试这个创新": "innovation",
    "帮我做创新": "innovation",
    "状态": "status",
    "进度": "status",
    "查状态": "status",
    "查一下状态": "status",
    "看状态": "status",
    "查看状态": "status",
    "查进度": "status",
    "查看进度": "status",
}
_SENTENCE_END = "。！？!?.,，；;：:"

_ROUTES = {item["intent"]: item["route"] for item in ROUTE_TABLE}
_MEANINGS = {item["intent"]: item["meaning"] for item in ROUTE_TABLE}


def interpret_request(
    project: Path,
    request: str,
    *,
    interpreter: Interpreter | None = None,
) -> dict[str, Any]:
    """只读理解一句人话；返回建议，不创建或执行 Task。"""
    normalized = _request(request)
    facts = coordinator_context(project)
    llm_context = {
        "project_standard": facts["project_standard"],
        "current_version": facts["current_version"],
        "unfinished_tasks": facts["unfinished_tasks"],
        "routes": [dict(item) for item in ROUTE_TABLE],
    }
    intent: str | None = None
    source = "fallback"
    if interpreter is not None:
        try:
            llm_result = interpreter(normalized, llm_context)
        except Exception:
            llm_result = None
        if llm_result is not None:
            intent = _validate_llm_result(llm_result)
            if intent is not None:
                source = "llm"
    if intent is None:
        matches = _fallback_matches(normalized)
        if len(matches) != 1:
            return _clarification(normalized, source, matches, facts)
        intent = matches[0]
    return _explanation(intent, normalized, source, facts)


def _validate_llm_result(result: dict[str, Any]) -> str | None:
    if not isinstance(result, dict) or set(result) != {"intent"}:
        raise ValueError("LLM 解释结果必须只包含 intent")
    intent = result["intent"]
    if intent is None:
        return None
    if not isinstance(intent, str):
        raise ValueError("LLM intent 必须是字符串或 null")
    if intent not in INTENTS and intent != "needs_clarification":
        raise ValueError("LLM intent 不在固定路线表内")
    return intent


def _fallback_matches(request: str) -> list[str]:
    lowered = request.casefold().rstrip().rstrip(_SENTENCE_END).rstrip()
    exact = FALLBACK_EXACT.get(lowered)
    if exact is not None:
        return [exact]
    return [
        intent
        for intent, keywords in FALLBACK_KEYWORDS.items()
        if any(keyword.casefold() in lowered for keyword in keywords)
    ]


def _clarification(
    request: str,
    source: str,
    matches: list[str],
    facts: dict[str, Any],
) -> dict[str, Any]:
    choices = "、".join(matches) if matches else "没有明确路线"
    return _payload(
        intent="needs_clarification",
        route=None,
        source=source,
        understood=f"这句话暂时不能安全归到一条路线：{request}",
        based_on=_basis(facts),
        available=False,
        missing=[f"需要在这些方向中确认一个：{choices}"],
        next_step="只问一个问题：这次最想先完成哪一件事？",
        needs_clarification=True,
        needs_confirmation=False,
        structured_action=None,
    )


def _explanation(
    intent: str,
    request: str,
    source: str,
    facts: dict[str, Any],
) -> dict[str, Any]:
    if intent == "needs_clarification":
        return _clarification(request, source, [], facts)
    if intent == "continue":
        return _continue_explanation(request, source, facts)

    common = {
        "intent": intent,
        "route": _ROUTES[intent],
        "source": source,
        "understood": _MEANINGS[intent],
        "based_on": _basis(facts),
        "needs_clarification": False,
        "needs_confirmation": False,
    }
    if intent == "status":
        return _payload(
            **common,
            available=True,
            missing=[],
            next_step="只读运行 workflow-status，返回当前 Task、Run 和证据。",
            structured_action=_action("workflow-status", {}, True),
        )
    if intent in {"tune", "ablation", "reproduction"}:
        missing_by_route = {
            "tune": ["比较目标", "候选配置", "预算", "停止条件"],
            "ablation": [
                "ready Module",
                "disabled_behavior",
                "启用侧 Run",
                "关闭侧配置、预算和停止条件",
            ],
            "reproduction": [
                "Source 和来源 Run",
                "目标指标与容差",
                "代码标准和数据标准",
                "预算和停止条件",
            ],
        }
        return _payload(
            **common,
            available=False,
            missing=missing_by_route[intent],
            next_step=(
                f"补齐输入后，可用 start-task --route {intent} 创建 Task；"
                "不会自动开跑。"
            ),
            structured_action=_action(
                "start-task", {"request": request, "route": intent}, False
            ),
        )
    if intent == "innovation":
        return _payload(
            **common,
            available=False,
            missing=[
                "ready Idea",
                "v2 Template",
                "ready research Module",
                "比较基线、主指标、预算和停止条件",
            ],
            next_step=(
                "补齐并对齐 Idea、Template、Module 和比较条件后，"
                "可用 start-task --route innovation 创建 Task；不会自动开跑。"
            ),
            structured_action=_action(
                "start-task", {"request": request, "route": "innovation"}, False
            ),
        )
    missing = {
        "paper": "论文整理写入口尚未实现",
        "code": "模板或模块接入写入口尚未实现",
    }[intent]
    return _payload(
        **common,
        available=False,
        missing=[missing],
        next_step=f"先准备{_MEANINGS[intent]}所需输入，等对应写入口完成后再创建 Task。",
        structured_action=None,
    )


def _continue_explanation(
    request: str, source: str, facts: dict[str, Any]
) -> dict[str, Any]:
    unfinished = facts["unfinished_tasks"]
    common = {
        "intent": "continue",
        "route": "management",
        "source": source,
        "understood": "继续原来的未完成 Task",
        "based_on": _basis(facts),
    }
    if not unfinished:
        return _payload(
            **common,
            available=False,
            missing=["当前没有未完成 Task"],
            next_step="请选择论文、代码、调参、消融、复现或创新中的一条路线。",
            needs_clarification=False,
            needs_confirmation=False,
            structured_action=None,
        )
    if len(unfinished) > 1:
        ids = [task["id"] for task in unfinished]
        return _payload(
            **common,
            available=False,
            missing=[f"有多个未完成 Task：{'、'.join(ids)}"],
            next_step="只问一个问题：这次继续哪个 Task？",
            needs_clarification=True,
            needs_confirmation=False,
            structured_action=None,
        )
    task = unfinished[0]
    task_id = task["id"]
    common["based_on"] = f"{task_id}，当前处于 {task['stage']}"
    can_run = (
        task["stage"] == "ready"
        and task["route_supported"]
        and task["remaining_runs"] is not None
        and task["remaining_runs"] > 0
        and task["execution_authorized"]
        and task["plan_unchanged"]
    )
    if can_run:
        return _payload(
            **common,
            available=True,
            missing=[],
            next_step="可以建议 run-task，但本次解释不会自动执行。",
            needs_clarification=False,
            needs_confirmation=False,
            structured_action=_action(
                "run-task",
                {"task": task_id, "purpose": "evidence", "backend": "project"},
                True,
            ),
        )
    if not task["route_supported"]:
        next_step = (
            "当前只支持继续四类实验路线，"
            f"不支持 {task['route']}。"
        )
    elif task["remaining_runs"] is None:
        next_step = "max_runs 预算必须是正整数。"
    elif task["remaining_runs"] <= 0:
        next_step = "本 Task 的 max_runs 预算用完，不能继续运行。"
    else:
        next_step = {
            "queued": "先进入 preparing 并检查前置条件。",
            "preparing": "先补齐前置条件，再进入 ready。",
            "ready": "先确认本次执行授权和计划没有变化。",
            "executing": "先只读查状态，不启动第二次运行。",
            "reviewing": "继续收齐结果和审核，完成收尾。",
            "stopped": "恢复到 preparing，再重新检查前置条件。",
        }[task["stage"]]
    return _payload(
        **common,
        available=False,
        missing=[next_step],
        next_step=next_step,
        needs_clarification=False,
        needs_confirmation=True,
        structured_action=None,
    )


def _basis(facts: dict[str, Any]) -> str:
    version = facts["current_version"] or "尚无当前 Version"
    task_count = len(facts["unfinished_tasks"])
    return f"项目标准、{version}、{task_count} 个未完成 Task"


def _action(
    command: str, arguments: dict[str, Any], executable: bool
) -> dict[str, Any]:
    return {
        "command": command,
        "arguments": arguments,
        "executable": executable,
    }


def _payload(**values: Any) -> dict[str, Any]:
    return {
        "schema": "cv-experiment-workflow.interpretation.v1",
        "intent": values["intent"],
        "route": values["route"],
        "source": values["source"],
        "understood": values["understood"],
        "based_on": values["based_on"],
        "available": values["available"],
        "missing": values["missing"],
        "next_step": values["next_step"],
        "needs_clarification": values["needs_clarification"],
        "needs_confirmation": values["needs_confirmation"],
        "structured_action": values["structured_action"],
    }


def _request(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 4096:
        raise ValueError("request 必须是 1..4096 字符的非空字符串")
    return value.strip()
