from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

from .run_identity import is_bound_frozen


SealVerifier = Callable[[str], object]
_ROUTES = {"tune", "ablation", "reproduction", "innovation"}
_RUN_ID = re.compile(r"RUN-[0-9]{4}")


def build_fair_comparison(
    route: str,
    task: dict[str, Any],
    candidate_run: dict[str, Any],
    *,
    source_run: dict[str, Any] | None,
    seal_verifier: SealVerifier | None,
) -> dict[str, Any]:
    """生成原始并排结果；只有中央门禁通过后才填写正式差值字段。"""
    if route not in _ROUTES or task.get("route") != route:
        raise ValueError("公平比较只支持 tune/ablation/reproduction/innovation")

    inputs = task.get("route_inputs")
    if not isinstance(inputs, dict):
        raise ValueError("Task route_inputs 无效")
    primary = inputs.get("primary_metric", "score")
    if not isinstance(primary, str) or not primary.strip():
        raise ValueError("primary_metric 必须是非空字符串")
    declared_baseline = inputs.get("baseline")
    candidate = _metric(candidate_run, primary)
    if route in {"ablation", "reproduction", "innovation"} and not _number(
        candidate
    ):
        raise ValueError(f"{route} 的 candidate primary metric 必须是有限数字")
    if candidate is not None and not _number(candidate):
        raise ValueError("candidate primary metric 必须是有限数字")
    if declared_baseline is not None and not _number(declared_baseline):
        raise ValueError("baseline 必须是有限数字")

    source_value = _metric(source_run, primary)
    raw_source = (
        source_value
        if _number(source_value)
        else declared_baseline
    )
    comparison: dict[str, Any] = {
        "primary_metric": primary,
        "baseline": declared_baseline,
        "source": raw_source,
        "candidate": candidate,
        "delta": None,
        "comparison_scope": "side_by_side_only",
        "blockers": [],
    }
    if route == "ablation":
        comparison.update(
            {
                "module_ref": inputs.get("module_ref"),
                "enabled_run_ref": inputs.get("baseline_run_ref"),
                "enabled": raw_source,
                "disabled": candidate,
                "module_effect": None,
            }
        )
    elif route == "reproduction":
        comparison.update(
            {
                "source_ref": inputs.get("source_ref"),
                "source_run_ref": inputs.get("source_run_ref"),
                "target": raw_source,
                "tolerance": inputs.get("tolerance"),
                "difference": None,
                "within_tolerance": None,
            }
        )

    blockers: list[str] = []
    expected_source_id = _expected_source_id(route, inputs)
    if expected_source_id is None:
        blockers.append(
            "source_run_ref_missing: 没有声明来源 Run，只能展示原始并排结果"
        )
    elif _RUN_ID.fullmatch(expected_source_id) is None:
        blockers.append(
            "source_run_ref_invalid: 来源 Run 编号无效，只能展示原始并排结果"
        )
    targets = task.get("target_refs")
    if (
        expected_source_id is not None
        and (
            not isinstance(targets, list)
            or expected_source_id not in targets
        )
    ):
        blockers.append(
            "source_run_ref_not_declared_target: 来源 Run 没有列入 Task 目标"
        )
    if source_run is None:
        blockers.append(
            "source_run_missing: 找不到来源 Run，只能展示原始并排结果"
        )
    else:
        source_id = source_run.get("id")
        if source_id != expected_source_id:
            blockers.append(
                "source_run_ref_mismatch: 现场来源 Run 与 Task 声明不一致"
            )
        if source_id == candidate_run.get("id"):
            blockers.append(
                "source_run_is_candidate: 来源 Run 与候选 Run 不能是同一次运行"
            )
    candidate_task_id = candidate_run.get("task_id")
    task_id = task.get("id")
    if task_id is not None and candidate_task_id != task_id:
        blockers.append(
            "candidate_task_mismatch: 候选 Run 不属于当前 Task"
        )

    blockers.extend(_run_blockers("source", source_run))
    blockers.extend(_run_blockers("candidate", candidate_run))
    if not _number(source_value):
        blockers.append(
            "source_primary_metric_missing: 来源 Run 缺少有限的主指标"
        )
    if not _number(declared_baseline):
        blockers.append(
            "declared_baseline_invalid: Task 没有声明有限 baseline"
        )
    elif _number(source_value) and declared_baseline != source_value:
        blockers.append(
            "declared_baseline_mismatch: Task baseline 与来源 Run 主指标不一致"
        )

    source_fingerprints = _fingerprints(source_run)
    candidate_fingerprints = _fingerprints(candidate_run)
    for name in ("data", "evaluation"):
        source_fingerprint = source_fingerprints.get(name)
        candidate_fingerprint = candidate_fingerprints.get(name)
        if not _fingerprint(source_fingerprint) or not _fingerprint(
            candidate_fingerprint
        ):
            blockers.append(
                f"{name}_fingerprint_missing: 两个 Run 都必须有{name}指纹"
            )
        elif source_fingerprint != candidate_fingerprint:
            blockers.append(
                f"{name}_fingerprint_mismatch: 两个 Run 的{name}指纹不一致"
            )

    if seal_verifier is None:
        blockers.append(
            "live_output_seal_verifier_missing: 没有中央现场封存复核器"
        )
    elif not _has_run_gate_blocker(blockers):
        assert source_run is not None
        for label, run in (("source", source_run), ("candidate", candidate_run)):
            run_id = run.get("id")
            assert isinstance(run_id, str)
            try:
                verified = seal_verifier(run_id)
            except Exception:
                blockers.append(
                    f"{label}_run_output_reverify_failed: "
                    f"{'来源' if label == 'source' else '候选'} Run 的封存输出现场复核失败"
                )
            else:
                if (
                    not isinstance(verified, dict)
                    or not verified
                    or verified != run.get("output_seal")
                ):
                    blockers.append(
                        f"{label}_run_output_reverify_failed: "
                        f"{'来源' if label == 'source' else '候选'} Run 的现场封存记录与比较输入不一致"
                    )

    if route == "reproduction":
        tolerance = inputs.get("tolerance")
        if not _nonnegative_number(tolerance):
            blockers.append(
                "tolerance_invalid: reproduction tolerance 必须是有限非负数"
            )

    comparison["blockers"] = blockers
    if blockers:
        return comparison

    assert _number(source_value)
    assert _number(candidate)
    delta = candidate - source_value
    comparison["delta"] = delta
    comparison["comparison_scope"] = "formal"
    if route == "ablation":
        comparison["module_effect"] = source_value - candidate
    elif route == "reproduction":
        tolerance = inputs["tolerance"]
        comparison["difference"] = delta
        comparison["within_tolerance"] = abs(delta) <= tolerance
    return comparison


def _expected_source_id(route: str, inputs: dict[str, Any]) -> str | None:
    field = (
        "source_run_ref"
        if route in {"reproduction", "innovation"}
        else "baseline_run_ref"
    )
    value = inputs.get(field)
    return value if isinstance(value, str) else None


def _run_blockers(
    label: str,
    run: dict[str, Any] | None,
) -> list[str]:
    if run is None:
        return []
    human = "来源" if label == "source" else "候选"
    blockers: list[str] = []
    run_id = run.get("id")
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        blockers.append(
            f"{label}_run_id_invalid: {human} Run 编号无效"
        )
    frozen = run.get("frozen")
    if not is_bound_frozen(frozen):
        blockers.append(
            f"{label}_run_not_bound: {human} Run 没有绑定受核对的代码身份"
        )
    data = frozen.get("data") if isinstance(frozen, dict) else None
    if not isinstance(data, dict) or data.get("run_kind") != "real_experiment":
        blockers.append(
            f"{label}_run_not_real_experiment: {human} Run 不是真实实验"
        )
    if not isinstance(data, dict) or data.get("paper_eligible") is not True:
        blockers.append(
            f"{label}_run_not_paper_eligible: {human} Run 不能用于论文正式结论"
        )
    if run.get("purpose") != "evidence":
        blockers.append(
            f"{label}_run_not_evidence: {human} Run 不是正式 evidence Run"
        )
    execution = run.get("execution")
    if not isinstance(execution, dict) or execution.get("outcome") != "succeeded":
        blockers.append(
            f"{label}_run_not_succeeded: {human} Run 没有成功结束"
        )
    if not isinstance(execution, dict) or execution.get("stage") != "closed":
        blockers.append(
            f"{label}_run_not_closed: {human} Run 尚未关闭"
        )
    quality = run.get("quality")
    if (
        not isinstance(quality, dict)
        or not quality
        or any(value != "valid" for value in quality.values())
    ):
        blockers.append(
            f"{label}_run_quality_not_valid: {human} Run 的质量检查没有全部通过"
        )
    output_seal = run.get("output_seal")
    if not isinstance(output_seal, dict) or not output_seal:
        blockers.append(
            f"{label}_run_output_not_sealed: {human} Run 没有非空输出封存记录"
        )
    return blockers


def _has_run_gate_blocker(blockers: list[str]) -> bool:
    prefixes = (
        "source_run_",
        "candidate_run_",
        "source_primary_metric_",
        "declared_baseline_",
        "data_fingerprint_",
        "evaluation_fingerprint_",
    )
    return any(item.startswith(prefixes) for item in blockers)


def _metric(run: object, primary: str) -> object:
    if not isinstance(run, dict):
        return None
    result = run.get("result")
    metrics = result.get("metrics") if isinstance(result, dict) else None
    return metrics.get(primary) if isinstance(metrics, dict) else None


def _fingerprints(run: object) -> dict[str, object]:
    if not isinstance(run, dict):
        return {}
    frozen = run.get("frozen")
    code = frozen.get("code") if isinstance(frozen, dict) else None
    fingerprints = code.get("fingerprints") if isinstance(code, dict) else None
    return fingerprints if isinstance(fingerprints, dict) else {}


def _fingerprint(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None
    )


def _number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _nonnegative_number(value: object) -> bool:
    return _number(value) and value >= 0
