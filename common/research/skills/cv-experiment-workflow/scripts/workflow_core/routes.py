from __future__ import annotations

import math as _math
from pathlib import Path as _Path
from typing import Any as _Any

from .adapters import ProjectAdapter as _ProjectAdapter
from .fair_comparison import build_fair_comparison as _build_fair_comparison


_SUPPORTED_ROUTES = {"tune", "ablation", "reproduction", "innovation"}


def requires(route: str, task: dict[str, _Any]) -> list[str]:
    _require_supported(route, task)
    missing: list[str] = []
    if not task.get("target_refs"):
        missing.append("实验需要明确目标和比较对象")
    inputs = task.get("route_inputs")
    if not isinstance(inputs, dict) or not isinstance(inputs.get("config"), dict):
        missing.append("调参配置没有写明")
    budget = task.get("budget")
    if not isinstance(budget, dict) or not _positive_int(budget.get("max_runs")):
        missing.append("max_runs 预算没有写明")
    if not task.get("stop_condition"):
        missing.append("停止条件没有写明")
    inputs = task.get("route_inputs", {})
    framework_experiment = inputs.get("framework_experiment")
    if isinstance(framework_experiment, dict):
        missing.extend(_framework_route_missing(route, inputs))
        return missing
    if route in {"ablation", "reproduction", "innovation"}:
        if not _number(inputs.get("baseline")):
            missing.append(f"{route} 实验需要有限 baseline")
        primary = inputs.get("primary_metric")
        if not isinstance(primary, str) or not primary.strip():
            missing.append(f"{route} 实验需要 primary_metric")
    if route == "ablation":
        if not _id(inputs.get("module_ref"), "MOD"):
            missing.append("消融实验需要明确一个 MOD")
        if not _id(inputs.get("baseline_run_ref"), "RUN"):
            missing.append("消融实验需要一个已完成的启用侧 Run")
        if not _text(inputs.get("disabled_behavior")):
            missing.append("消融实验需要写明 disabled_behavior")
    if route == "reproduction":
        if not _id(inputs.get("source_ref"), "SRC"):
            missing.append("复现实验需要锁定 Source")
        if not _id(inputs.get("source_run_ref"), "RUN"):
            missing.append("复现实验需要锁定来源 Run")
        if not _nonnegative_number(inputs.get("tolerance")):
            missing.append("复现实验需要有限且非负的 tolerance")
        if not isinstance(inputs.get("code_standard"), dict):
            missing.append("复现实验需要锁定代码标准")
        if not isinstance(inputs.get("data_standard"), dict):
            missing.append("复现实验需要锁定数据标准")
    return missing


def _framework_route_missing(
    route: str,
    inputs: dict[str, _Any],
) -> list[str]:
    """Framework 实验使用可读来源，不伪造中央 SRC/MOD/RUN 编号。"""

    missing: list[str] = []
    if route in {"ablation", "reproduction", "innovation"}:
        if not _number(inputs.get("baseline")):
            missing.append(f"{route} 实验需要有限 baseline")
        if not _text(inputs.get("primary_metric")):
            missing.append(f"{route} 实验需要 primary_metric")
    if route == "reproduction":
        for field, message in (
            ("source_ref", "复现实验需要锁定论文或代码来源"),
            ("source_run_ref", "复现实验需要写明来源结果"),
        ):
            if not _text(inputs.get(field)):
                missing.append(message)
        if not _nonnegative_number(inputs.get("tolerance")):
            missing.append("复现实验需要有限且非负的 tolerance")
        for field, message in (
            ("code_standard", "复现实验需要锁定代码标准"),
            ("data_standard", "复现实验需要锁定数据标准"),
        ):
            if not isinstance(inputs.get(field), dict) or not inputs[field]:
                missing.append(message)
    elif route == "ablation":
        for field, message in (
            ("module_ref", "消融实验需要写明被关闭的模块"),
            ("baseline_run_ref", "消融实验需要写明启用侧基线 Run"),
            ("disabled_behavior", "消融实验需要写明关闭后的行为"),
        ):
            if not _text(inputs.get(field)):
                missing.append(message)
    elif route == "tune":
        allowed = inputs.get("allowed_changes")
        if (
            not isinstance(allowed, list)
            or not allowed
            or not all(_text(item) for item in allowed)
        ):
            missing.append("调参实验需要写明允许修改的参数")
    return missing


def build_variants(
    route: str,
    adapter: _ProjectAdapter,
    project: _Path,
    task: dict[str, _Any],
) -> list[dict[str, _Any]]:
    _require_supported(route, task)
    variants = adapter.prepare_runs(project, task)
    if (
        not isinstance(variants, list)
        or not variants
        or not all(isinstance(item, dict) for item in variants)
    ):
        raise ValueError("Adapter 没有生成可运行的实验候选")
    return variants


def compare(
    route: str,
    task: dict[str, _Any],
    parsed: dict[str, _Any],
    *,
    source_run: dict[str, _Any] | None = None,
    seal_verifier: _Any = None,
) -> dict[str, _Any]:
    _require_supported(route, task)
    return _build_fair_comparison(
        route,
        task,
        parsed,
        source_run=source_run,
        seal_verifier=seal_verifier,
    )


def mutation_class(route: str, task: dict[str, _Any]) -> str:
    _require_supported(route, task)
    if route == "innovation":
        return "innovation"
    changed = task.get("route_inputs", {}).get("changes_code_behavior", False)
    if not isinstance(changed, bool):
        raise ValueError("changes_code_behavior 必须是 bool")
    return "innovation" if changed else "configuration"


def _require_supported(route: str, task: dict[str, _Any]) -> None:
    if route not in _SUPPORTED_ROUTES or task.get("route") != route:
        raise ValueError(
            "共享执行循环只支持 tune/ablation/reproduction/innovation 路线"
        )


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and _math.isfinite(value)
    )


def _nonnegative_number(value: object) -> bool:
    return _number(value) and value >= 0


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _id(value: object, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 8
        and value.startswith(f"{prefix}-")
        and value[4:].isdigit()
    )
