from __future__ import annotations

from pathlib import Path
from typing import Any

from .adapters import (
    ProjectAdapter,
    adapter_summary,
    load_project_adapter,
    load_snapshot_adapter,
)
from .codebases import load_codebase_adapter_spec, verify_codebase_gate
from .evidence import current_evidence_level, record_evidence_transition
from .execution_snapshot import (
    temporary_commit_snapshot,
    verify_project_run_execution_snapshot,
    verify_temporary_execution_snapshot,
)
from .review import close_session, open_session, review_parsed_result
from .routes import build_variants, compare, mutation_class, requires
from .runner import collect as runner_collect
from .runner import start as runner_start
from .runner import status as runner_status
from .runner import stop as runner_stop
from .runs import (
    claim_run,
    close_run,
    finish_run,
    load_run,
    record_run_cleanup_state,
    set_run_process_id,
    start_run,
)
from .run_identity import (
    bind_current_comparison_policy,
    build_bound_frozen,
    is_bound_frozen,
)
from .tasking import evaluate_readiness, load_task, readiness, transition_task


def execute_task(
    project: Path,
    task_id: str,
    purpose: str = "evidence",
    *,
    backend: str = "project",
) -> dict[str, Any]:
    if purpose not in {"debug", "evidence"}:
        raise ValueError("purpose 只允许 debug/evidence")
    if backend not in {"local", "project"}:
        raise ValueError("backend 只允许 local/project")
    if backend == "local":
        raise NotImplementedError("local backend 尚无真实项目需求")

    initial_task = load_task(project, task_id)
    _require_bound_task_for_new_evidence(initial_task, purpose)
    binding = initial_task["route_inputs"].get("code_binding")
    bound_gate: dict[str, Any] | None = None
    prepared_frozen: dict[str, Any] | None = None
    resume_identity_review: dict[str, Any] | None = None
    adapter_warnings: list[str] = []
    if binding is None:
        recovered = _recover_satisfied_single_debug(project, task_id, purpose)
        if recovered is not None:
            return recovered
        task = _prepare_task(project, task_id)
        adapter = load_project_adapter(project)
        execution_root = project
        if task["stage"] in {"preparing", "ready"}:
            gate = _task_gate(project, execution_root, task, adapter)
            if gate["status"] == "fail":
                return _gate_failure(task, gate)
            adapter_warnings = list(gate["warnings"])
            variants = build_variants(
                task["route"],
                adapter,
                execution_root,
                task,
            )
            prepared_frozen = bind_current_comparison_policy(variants[0])
            if task["stage"] == "preparing":
                readiness(project, task_id, **gate["checks"])
            task = load_task(project, task_id)
            run = claim_run(
                project,
                task_id,
                prepared_frozen,
                purpose=purpose,
            )
        else:
            run = _resume_run(project, task, purpose)
    elif initial_task["stage"] in {"executing", "reviewing"}:
        task = _prepare_task(project, task_id)
        run = _resume_run(project, task, purpose)
        try:
            execution_root = verify_project_run_execution_snapshot(
                project,
                run["id"],
            )
        except Exception:
            if run["execution"]["stage"] == "running":
                record_run_cleanup_state(
                    project,
                    run["id"],
                    state="cleanup_failure",
                    reason="snapshot_unavailable_for_safe_stop",
                )
                return _cleanup_result(
                    project,
                    task_id,
                    run["id"],
                    status="cleanup_failure",
                    adapter=None,
                )
            raise
        adapter = load_snapshot_adapter(
            execution_root,
            run["frozen"]["code"]["commit"],
        )
        verified_again, verification_error = _capture_snapshot_verification(
            project,
            run["id"],
        )
        if verification_error is not None:
            failure_reason = _identity_failure_reason(
                verification_error,
                "execution_snapshot_drift",
            )
            if run["execution"]["stage"] == "running":
                cleanup = _stop_with_loaded_snapshot_adapter(
                    project,
                    execution_root,
                    task_id,
                    purpose,
                    backend,
                    adapter,
                    run,
                    reason=failure_reason,
                )
                if cleanup is not None:
                    return cleanup
            elif run["execution"]["stage"] != "planned":
                raise verification_error
            resume_identity_review = _failed_review(
                "prerequisite",
                failure_reason,
            )
            verified_again = execution_root
        if verified_again != execution_root:
            failure_reason = "execution_snapshot_path_changed"
            if run["execution"]["stage"] == "running":
                cleanup = _stop_with_loaded_snapshot_adapter(
                    project,
                    execution_root,
                    task_id,
                    purpose,
                    backend,
                    adapter,
                    run,
                    reason=failure_reason,
                )
                if cleanup is not None:
                    return cleanup
            elif run["execution"]["stage"] != "planned":
                raise ValueError(
                    "execution_snapshot path changed for a finished Run"
                )
            resume_identity_review = _failed_review(
                "prerequisite",
                failure_reason,
            )
    else:
        expected_git = {
            "branch": binding["branch"],
            "commit": binding["commit"],
            "tag": binding["tag"],
            "require_clean": purpose == "evidence",
        }
        bound_gate = verify_codebase_gate(
            project,
            binding["codebase_id"],
            expected_git=expected_git,
        )
        repository = Path(bound_gate["repo_path"])
        load_codebase_adapter_spec(repository, binding["commit"])
        with temporary_commit_snapshot(
            repository,
            binding["commit"],
        ) as preclaim_root:
            adapter = load_snapshot_adapter(
                preclaim_root,
                binding["commit"],
            )
            task = initial_task
            verify_temporary_execution_snapshot(preclaim_root)
            gate = _task_gate(project, preclaim_root, task, adapter)
            verify_temporary_execution_snapshot(preclaim_root)
            if gate["status"] == "fail":
                return _gate_failure(task, gate)
            adapter_warnings = list(gate["warnings"])
            variants = build_variants(
                task["route"],
                adapter,
                preclaim_root,
                task,
            )
            verify_temporary_execution_snapshot(preclaim_root)
            final_gate = verify_codebase_gate(
                project,
                binding["codebase_id"],
                expected_git=expected_git,
            )
            for field in (
                "codebase_id",
                "repo_path",
                "branch",
                "commit",
                "tag",
            ):
                if final_gate[field] != bound_gate[field]:
                    raise ValueError(
                        "Codebase Git 身份在 Adapter preclaim 调用期间发生变化"
                    )
            bound_gate = final_gate
            load_codebase_adapter_spec(repository, binding["commit"])
            prepared_frozen = build_bound_frozen(
                variants[0],
                binding=binding,
                gate=bound_gate,
                adapter=adapter_summary(adapter),
                purpose=purpose,
            )
            if (
                purpose == "evidence"
                and prepared_frozen["data"]["run_kind"]
                == "synthetic_debug_only"
            ):
                raise ValueError(
                    "synthetic_debug_only 只允许 debug，不能创建论文 evidence Run"
                )
            task = _prepare_task(project, task_id)
            if task["stage"] == "preparing":
                readiness(project, task_id, **gate["checks"])
            task = load_task(project, task_id)
            assert prepared_frozen is not None
            run = claim_run(
                project,
                task_id,
                prepared_frozen,
                purpose=purpose,
            )
        execution_root = verify_project_run_execution_snapshot(
            project,
            run["id"],
        )
        adapter = load_snapshot_adapter(
            execution_root,
            run["frozen"]["code"]["commit"],
        )
        verified_again = verify_project_run_execution_snapshot(
            project,
            run["id"],
        )
        if verified_again != execution_root:
            raise ValueError("execution_snapshot 路径在 Adapter 加载期间发生变化")

    task = load_task(project, task_id)
    session = open_session(task, run)
    try:
        result = _continue_task(
            project,
            execution_root,
            task_id,
            purpose,
            backend,
            adapter,
            session,
            initial_reviewed=resume_identity_review,
        )
        if adapter_warnings:
            result = dict(result)
            result["warnings"] = adapter_warnings
        return result
    finally:
        close_session(session)


def _require_bound_task_for_new_evidence(
    task: dict[str, Any],
    purpose: str,
) -> None:
    if (
        purpose == "evidence"
        and task["route_inputs"].get("code_binding") is None
    ):
        raise ValueError(
            "未绑定 Codebase 的 Task 不能新建论文 evidence Run；"
            "请先登记并绑定代码仓库"
        )


def _task_gate(
    project: Path,
    execution_root: Path,
    task: dict[str, Any],
    adapter: ProjectAdapter,
) -> dict[str, Any]:
    inspection = adapter.inspect(execution_root)
    adapter_issues = adapter.validate(execution_root)
    route_config = task["route_inputs"].get("config", {})
    route_mode = (
        route_config.get("mode")
        if isinstance(route_config, dict)
        else None
    )
    synthetic_debug = route_mode in {
        "synthetic_smoke",
        "synthetic_debug",
    }
    adapter_warnings = [
        issue
        for issue in adapter_issues
        if (
            synthetic_debug
            and issue.startswith("OPTIONAL_NOT_INSTALLED：")
        )
    ]
    blocking_adapter_issues = [
        issue for issue in adapter_issues if issue not in adapter_warnings
    ]
    route_issues = requires(task["route"], task)
    mutation = mutation_class(task["route"], task)
    standard = inspection.get("standard", {})
    standard_debug = standard.get("debug_required") if isinstance(standard, dict) else None
    task_debug = task["route_inputs"].get("debug_required", True)
    standard_matches = isinstance(standard_debug, bool) and standard_debug == task_debug
    code_verified = task["route_inputs"].get("code_verified", True)
    if not isinstance(code_verified, bool):
        raise ValueError("route_inputs.code_verified 必须是 bool")
    checks = {
        "target_clear": not route_issues,
        "template_runnable": (
            not blocking_adapter_issues and standard_matches
        ),
        "mutation_allowed": (
            mutation == "configuration" or task["route"] == "innovation"
        ),
        "code_verified": code_verified,
    }
    gate = evaluate_readiness(project, task["id"], **checks)
    reasons = []
    if gate["status"] == "fail":
        reasons = list(
            dict.fromkeys(
                [
                    *gate["reasons"],
                    *route_issues,
                    *blocking_adapter_issues,
                    *adapter_warnings,
                    *([] if standard_matches else ["Task 的 debug 规则与项目标准不一致"]),
                ]
            )
        )
    return {
        "status": gate["status"],
        "reasons": reasons,
        "checks": checks,
        "mutation": mutation,
        "warnings": adapter_warnings,
    }


def _gate_failure(task: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "task_id": task["id"],
        "task_stage": task["stage"],
        "reasons": gate["reasons"],
    }
    if gate["mutation"] == "innovation":
        result.update({"route_redirect": "innovation", "requires_code_review": True})
    return result


def _capture_snapshot_verification(
    project: Path,
    run_id: str,
) -> tuple[Path | None, Exception | None]:
    try:
        return (
            verify_project_run_execution_snapshot(project, run_id),
            None,
        )
    except Exception as error:
        return None, error


def _verify_bound_snapshot(
    project: Path,
    run: dict[str, Any],
) -> None:
    if is_bound_frozen(run.get("frozen")):
        verify_project_run_execution_snapshot(project, run["id"])


def _reverify_bound_variant(
    project: Path,
    execution_root: Path,
    adapter: ProjectAdapter,
    task: dict[str, Any],
    run: dict[str, Any],
) -> None:
    if not is_bound_frozen(run.get("frozen")):
        return
    _verify_bound_snapshot(project, run)
    variants = build_variants(
        task["route"],
        adapter,
        execution_root,
        task,
    )
    _verify_bound_snapshot(project, run)
    from .run_identity import canonical_fingerprint, data_contract_fingerprint

    candidate = variants[0]
    config = candidate.get("config")
    data = candidate.get("data")
    evaluation = data.get("evaluation") if isinstance(data, dict) else None
    if not isinstance(config, dict) or not isinstance(data, dict):
        raise ValueError("data_identity_invalid: Adapter 候选缺少 config/data")
    if not isinstance(evaluation, dict):
        raise ValueError("data_identity_invalid: Adapter 候选缺少 evaluation")
    observed = {
        "config": canonical_fingerprint(config),
        "data": data_contract_fingerprint(data),
        "evaluation": canonical_fingerprint(evaluation),
    }
    expected = run["frozen"]["code"]["fingerprints"]
    if any(observed[field] != expected[field] for field in observed):
        raise ValueError(
            "data_identity_drift: prepare_runs 返回的 config/data/evaluation "
            "与冻结 Run 不一致"
        )


def _identity_failure_reason(
    error: Exception,
    fallback: str,
) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.startswith("execution_snapshot_"):
        return code
    message = str(error)
    for prefix in ("data_identity_drift", "data_identity_invalid"):
        if message.startswith(prefix):
            return prefix
    return fallback


def _cleanup_result(
    project: Path,
    task_id: str,
    run_id: str,
    *,
    status: str,
    adapter: ProjectAdapter | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "task": load_task(project, task_id),
        "run": load_run(project, run_id),
        "evidence": None,
        "evidence_level": "none",
        "promotion_blockers": [status],
    }
    if adapter is not None:
        result["adapter"] = adapter_summary(adapter)
    return result


def _stop_with_loaded_snapshot_adapter(
    project: Path,
    execution_root: Path,
    task_id: str,
    purpose: str,
    backend: str,
    adapter: ProjectAdapter,
    run: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any] | None:
    try:
        response = runner_stop(
            execution_root,
            adapter,
            run,
            purpose=purpose,
            backend=backend,
        )
    except Exception:
        record_run_cleanup_state(
            project,
            run["id"],
            state="cleanup_failure",
            reason=f"{reason}:stop_failed",
        )
        return _cleanup_result(
            project,
            task_id,
            run["id"],
            status="cleanup_failure",
            adapter=adapter,
        )
    if response["status"] != "stopped":
        record_run_cleanup_state(
            project,
            run["id"],
            state="cleanup_pending",
            reason=f"{reason}:stop_requested",
        )
        return _cleanup_result(
            project,
            task_id,
            run["id"],
            status="cleanup_pending",
            adapter=adapter,
        )
    return None


def _continue_task(
    project: Path,
    execution_root: Path,
    task_id: str,
    purpose: str,
    backend: str,
    adapter: ProjectAdapter,
    session: list[dict[str, Any]],
    *,
    initial_reviewed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task = load_task(project, task_id)
    run = _resume_run(project, task, purpose)
    reviewed: dict[str, Any] | None = initial_reviewed
    runner_state: str | None = None

    if any(
        value.startswith("cleanup_failure:")
        for value in run["analysis"]["limitations"]
    ):
        return _cleanup_result(
            project,
            task_id,
            run["id"],
            status="cleanup_failure",
            adapter=adapter,
        )

    if initial_reviewed is not None:
        if run["execution"]["stage"] == "planned":
            run = start_run(project, run["id"])
        elif run["execution"]["stage"] != "running":
            raise ValueError(
                "initial identity failure only supports planned/running Run"
            )
    elif run["execution"]["stage"] == "planned":
        try:
            _reverify_bound_variant(
                project,
                execution_root,
                adapter,
                task,
                run,
            )
        except Exception as error:
            run = start_run(project, run["id"])
            reviewed = _failed_review(
                "prerequisite",
                _identity_failure_reason(error, "data_identity_recheck_failed"),
            )
        else:
            run = start_run(project, run["id"])
            try:
                response = runner_start(
                    execution_root,
                    adapter,
                    run,
                    purpose=purpose,
                    backend=backend,
                )
            except Exception:
                cleanup = _stop_with_loaded_snapshot_adapter(
                    project,
                    execution_root,
                    task_id,
                    purpose,
                    backend,
                    adapter,
                    run,
                    reason="runner_start_failed",
                )
                if cleanup is not None:
                    return cleanup
                reviewed = _failed_review(
                    "environment",
                    "runner_start_failed",
                )
            else:
                run = set_run_process_id(
                    project,
                    run["id"],
                    response["process_id"],
                )
                if response["status"] == "stopped":
                    reviewed = _failed_review(
                        "user_stop", "runner_stopped", outcome="stopped"
                    )
                runner_state = response["status"]
            try:
                _verify_bound_snapshot(project, run)
                _reverify_bound_variant(
                    project,
                    execution_root,
                    adapter,
                    load_task(project, task_id),
                    load_run(project, run["id"]),
                )
            except Exception as error:
                failure_reason = _identity_failure_reason(
                    error,
                    "data_identity_recheck_failed",
                )
                if runner_state in {"running", "cleanup_pending"}:
                    cleanup = _stop_with_loaded_snapshot_adapter(
                        project,
                        execution_root,
                        task_id,
                        purpose,
                        backend,
                        adapter,
                        run,
                        reason=failure_reason,
                    )
                    if cleanup is not None:
                        return cleanup
                reviewed = _failed_review(
                    "prerequisite",
                    failure_reason,
                )
                runner_state = None
    elif run["execution"]["stage"] == "running":
        try:
            _verify_bound_snapshot(project, run)
        except Exception as error:
            failure_reason = _identity_failure_reason(
                error,
                "execution_snapshot_drift",
            )
            cleanup = _stop_with_loaded_snapshot_adapter(
                project,
                execution_root,
                task_id,
                purpose,
                backend,
                adapter,
                run,
                reason=failure_reason,
            )
            if cleanup is not None:
                return cleanup
            reviewed = _failed_review(
                "prerequisite",
                failure_reason,
            )
        else:
            try:
                response = runner_status(
                    execution_root,
                    adapter,
                    run,
                    purpose=purpose,
                    backend=backend,
                )
            except Exception:
                cleanup = _stop_with_loaded_snapshot_adapter(
                    project,
                    execution_root,
                    task_id,
                    purpose,
                    backend,
                    adapter,
                    run,
                    reason="runner_status_failed",
                )
                if cleanup is not None:
                    return cleanup
                reviewed = _failed_review(
                    "environment",
                    "runner_status_failed",
                )
                response = {"status": "failed"}
            if response["status"] in {"not_started", "failed"}:
                if reviewed is None:
                    reviewed = _failed_review(
                        "environment", "runner_not_available"
                    )
            elif response["status"] == "stopped":
                reviewed = _failed_review(
                    "user_stop", "runner_stopped", outcome="stopped"
                )
            runner_state = response["status"]
            try:
                _verify_bound_snapshot(project, run)
            except Exception as error:
                failure_reason = _identity_failure_reason(
                    error,
                    "execution_snapshot_drift",
                )
                if runner_state in {"running", "cleanup_pending"}:
                    cleanup = _stop_with_loaded_snapshot_adapter(
                        project,
                        execution_root,
                        task_id,
                        purpose,
                        backend,
                        adapter,
                        run,
                        reason=failure_reason,
                    )
                    if cleanup is not None:
                        return cleanup
                reviewed = _failed_review(
                    "prerequisite",
                    failure_reason,
                )
                runner_state = None

    run = load_run(project, run["id"])
    if runner_state in {"running", "cleanup_pending"}:
        return {
            "status": "in_progress",
            "task": load_task(project, task_id),
            "run": run,
            "session": session,
            "adapter": adapter_summary(adapter),
        }
    if run["execution"]["stage"] == "running":
        if reviewed is None:
            try:
                _verify_bound_snapshot(project, run)
                parsed = runner_collect(
                    execution_root,
                    adapter,
                    run,
                    purpose=purpose,
                    backend=backend,
                )
                _verify_bound_snapshot(project, run)
                _reverify_bound_variant(
                    project,
                    execution_root,
                    adapter,
                    load_task(project, task_id),
                    load_run(project, run["id"]),
                )
            except Exception as error:
                reason = _identity_failure_reason(error, "")
                reviewed = _failed_review(
                    "prerequisite" if reason else "implementation",
                    reason or "collect_failed",
                )
            else:
                task_snapshot = load_task(project, task_id)
                run_snapshot = load_run(project, run["id"])
                try:
                    reviewed = review_parsed_result(
                        project, task_snapshot, run_snapshot, parsed
                    )
                except Exception:
                    reviewed = _failed_review("implementation", "review_failed")
        run = _finish_from_review(project, run["id"], reviewed)
    elif run["execution"]["stage"] in {"finished", "closed"}:
        reviewed = _review_from_run(project, task_id, run)

    task = load_task(project, task_id)
    if task["stage"] == "executing":
        task = transition_task(project, task_id, "reviewing")
    elif task["stage"] != "reviewing":
        raise ValueError(f"恢复时 Task stage 无效：{task['stage']}")

    run = load_run(project, run["id"])
    evidence_level = _evidence_level(project, run)
    current_level = current_evidence_level(project, run["id"])
    if evidence_level == "none" and current_level == "none":
        return {
            "status": "artifact_seal_pending",
            "task": task,
            "run": run,
            "evidence": None,
            "evidence_level": "none",
            "promotion_blockers": ["artifact_seal_pending"],
            "comparison": reviewed["comparison"],
            "session": session,
            "adapter": adapter_summary(adapter),
        }
    actors = _evidence_actors(session)
    event: dict[str, Any] | None = None
    if current_level == "none":
        event = record_evidence_transition(
            project,
            run["id"],
            evidence_level,
            evidence_refs=[run["id"]],
            reason=reviewed["decision"]["reason"],
            proposed_by=actors["proposed_by"],
            checked_by=actors["checked_by"],
            applied_by=actors["applied_by"],
        )
    else:
        evidence_level = current_level
    run = load_run(project, run["id"])
    if run["execution"]["stage"] == "finished":
        run = close_run(project, run["id"])
    reviewed["comparison"] = _live_comparison(
        project,
        load_task(project, task_id),
        run,
    )
    if any(
        blocker.startswith(
            (
                "source_run_output_reverify_failed:",
                "candidate_run_output_reverify_failed:",
            )
        )
        for blocker in reviewed["comparison"].get("blockers", [])
        if isinstance(blocker, str)
    ):
        return {
            "status": "comparison_reverify_failed",
            "task": load_task(project, task_id),
            "run": run,
            "evidence": event,
            "evidence_level": evidence_level,
            "comparison": reviewed["comparison"],
            "session": session,
            "adapter": adapter_summary(adapter),
        }
    task = load_task(project, task_id)
    if task["stage"] == "reviewing":
        task = _finish_task(
            project,
            task_id,
            run,
            evidence_level,
            reviewed["decision"],
            reviewed["comparison"],
        )
    return {
        "task": task,
        "run": run,
        "evidence": event,
        "evidence_level": evidence_level,
        "comparison": reviewed["comparison"],
        "session": session,
        "adapter": adapter_summary(adapter),
    }


def _finish_from_review(
    project: Path, run_id: str, reviewed: dict[str, Any]
) -> dict[str, Any]:
    return finish_run(
        project,
        run_id,
        outcome=reviewed["execution"]["outcome"],
        exit_code=reviewed["execution"]["exit_code"],
        metrics=reviewed["result"]["metrics"],
        raw_log=reviewed["result"]["raw_log"],
        implementation=reviewed["quality"]["implementation"],
        interface=reviewed["quality"]["interface"],
        data=reviewed["quality"]["data"],
        metrics_quality=reviewed["quality"]["metrics"],
        hypothesis=reviewed["analysis"]["hypothesis"],
        limitations=reviewed["analysis"]["limitations"],
        suggestions=reviewed["analysis"]["suggestions"],
        artifacts=reviewed["artifacts"],
        issue_kind=reviewed["execution"]["issue_kind"],
    )


def _review_from_run(
    project: Path, task_id: str, run: dict[str, Any]
) -> dict[str, Any]:
    parsed = {
        "execution": {
            "outcome": run["execution"]["outcome"],
            "exit_code": run["execution"]["exit_code"],
            "issue_kind": run["execution"]["issue_kind"],
        },
        "result": run["result"],
        "quality": run["quality"],
        "analysis": run["analysis"],
        "artifacts": run["artifacts"],
    }
    return review_parsed_result(
        project, load_task(project, task_id), load_run(project, run["id"]), parsed
    )


def _failed_review(
    issue_kind: str, reason: str, *, outcome: str = "failed"
) -> dict[str, Any]:
    quality_state = "invalid" if issue_kind == "implementation" else "uncertain"
    closure = (
        "stopped"
        if issue_kind == "user_stop"
        else "preparing"
        if issue_kind in {"implementation", "prerequisite"}
        else "ready"
    )
    return {
        "execution": {"outcome": outcome, "exit_code": None, "issue_kind": issue_kind},
        "result": {"metrics": {}, "raw_log": None},
        "quality": {
            "implementation": quality_state,
            "interface": "uncertain",
            "data": "uncertain",
            "metrics": "uncertain",
        },
        "analysis": {
            "hypothesis": "not_evaluated",
            "limitations": [reason],
            "suggestions": [],
        },
        "artifacts": [],
        "comparison": {
            "primary_metric": None,
            "baseline": None,
            "candidate": None,
            "delta": None,
        },
        "decision": {"closure": closure, "issue_kind": issue_kind, "reason": reason},
    }


def _prepare_task(project: Path, task_id: str) -> dict[str, Any]:
    task = load_task(project, task_id)
    if task["stage"] in {"queued", "preparing", "ready"}:
        max_runs = task["budget"].get("max_runs")
        if (
            type(max_runs) is int
            and max_runs > 0
            and len(task["run_refs"]) >= max_runs
        ):
            raise ValueError(
                f"Task 运行预算已用完；如需继续请新建 Task：{task_id}"
            )
    if task["stage"] == "queued":
        task = transition_task(project, task_id, "preparing")
    if task["stage"] not in {"preparing", "ready", "executing", "reviewing"}:
        raise ValueError(
            "execute_task 只接收 queued、preparing、ready、executing 或 reviewing Task"
        )
    return task


def _recover_satisfied_single_debug(
    project: Path, task_id: str, purpose: str
) -> dict[str, Any] | None:
    task = load_task(project, task_id)
    stop_condition = task["stop_condition"]
    after_runs = stop_condition.get("after_runs")
    if (
        purpose != "debug"
        or task["stage"] not in {"preparing", "ready"}
        or stop_condition.get("type") != "single_debug"
        or type(after_runs) is not int
        or after_runs <= 0
        or len(task["run_refs"]) < after_runs
    ):
        return None
    run = load_run(project, task["run_refs"][-1])
    evidence_level = current_evidence_level(project, run["id"])
    credible_closed_debug = (
        run["purpose"] == "debug"
        and run["execution"]["stage"] == "closed"
        and run["execution"]["outcome"] == "succeeded"
        and all(value == "valid" for value in run["quality"].values())
        and evidence_level == "debug"
    )
    if not credible_closed_debug:
        return None

    if task["stage"] == "preparing":
        readiness(
            project,
            task_id,
            target_clear=True,
            template_runnable=True,
            mutation_allowed=True,
            code_verified=True,
        )
        task = load_task(project, task_id)
    if task["stage"] == "ready":
        task = transition_task(project, task_id, "executing")
    task = transition_task(project, task_id, "reviewing")
    from .run_identity import comparison_policy_for_frozen

    if comparison_policy_for_frozen(run.get("frozen")) is None:
        from .runs import _legacy_route_comparison

        recovery_comparison = _legacy_route_comparison(task, run)
    else:
        recovery_comparison = _live_comparison(project, task, run)
    task = _finish_task(
        project,
        task_id,
        run,
        evidence_level,
        {},
        recovery_comparison,
    )
    return {
        "status": "recovered_complete",
        "task": task,
        "run": run,
        "evidence": None,
        "evidence_level": evidence_level,
    }


def _resume_run(
    project: Path, task: dict[str, Any], purpose: str
) -> dict[str, Any]:
    if not task["run_refs"]:
        raise ValueError(f"{task['stage']} Task 没有可恢复 Run")
    run = load_run(project, task["run_refs"][-1])
    if run["purpose"] != purpose:
        raise ValueError("恢复 purpose 与现有 Run 不一致")
    if run["execution"]["stage"] not in {"planned", "running", "finished", "closed"}:
        raise ValueError("现有 Run stage 无法恢复")
    return run


def _evidence_level(project: Path, run: dict[str, Any]) -> str:
    credible = (
        run["execution"]["outcome"] == "succeeded"
        and all(value == "valid" for value in run["quality"].values())
    )
    if not credible:
        return "disqualified"
    if run["purpose"] == "debug":
        return "debug"
    if is_bound_frozen(run.get("frozen")):
        if run.get("output_seal") is None:
            return "none"
        from .output_seal import verify_run_output_seal

        verify_run_output_seal(project, run["id"])
    return "single_run"


def _live_comparison(
    project: Path,
    task: dict[str, Any],
    candidate_run: dict[str, Any],
) -> dict[str, Any]:
    inputs = task.get("route_inputs")
    source_ref: object = None
    if isinstance(inputs, dict):
        source_ref = inputs.get(
            "source_run_ref"
            if task.get("route") in {"reproduction", "innovation"}
            else "baseline_run_ref"
        )
    source_run: dict[str, Any] | None = None
    if isinstance(source_ref, str):
        try:
            source_run = load_run(project, source_ref)
        except ValueError:
            source_run = None

    from .output_seal import verify_run_output_seal

    return compare(
        task["route"],
        task,
        candidate_run,
        source_run=source_run,
        seal_verifier=lambda run_id: verify_run_output_seal(
            project,
            run_id,
        ),
    )


def _evidence_actors(session: list[dict[str, Any]]) -> dict[str, str]:
    role_ids = {item["role"]: item["id"] for item in session}
    return {
        "proposed_by": role_ids["Analyst"],
        "checked_by": role_ids.get("Reviewer", ""),
        "applied_by": role_ids["Coordinator"],
    }


def _finish_task(
    project: Path,
    task_id: str,
    run: dict[str, Any],
    evidence_level: str,
    decision: dict[str, Any],
    comparison: dict[str, Any],
) -> dict[str, Any]:
    task = load_task(project, task_id)
    stop_condition = task["stop_condition"]
    after_runs = stop_condition.get("after_runs")
    if (
        run["purpose"] == "debug"
        and evidence_level == "debug"
        and stop_condition.get("type") == "single_debug"
        and type(after_runs) is int
        and after_runs > 0
        and len(task["run_refs"]) >= after_runs
    ):
        return transition_task(
            project,
            task_id,
            "done",
            conclusion={
                "scope": "debug_only",
                "reason": "stop_condition_met",
                "run_id": run["id"],
                "evidence_level": evidence_level,
                "hypothesis": run["analysis"]["hypothesis"],
                "comparison": comparison,
            },
        )
    closure = decision["closure"]
    if closure != "done":
        if closure == "stopped":
            return transition_task(project, task_id, "stopped")
        return transition_task(
            project, task_id, closure, issue_kind=decision["issue_kind"]
        )
    return transition_task(
        project,
        task_id,
        "done",
        conclusion={
            "run_id": run["id"],
            "outcome": run["execution"]["outcome"],
            "hypothesis": run["analysis"]["hypothesis"],
            "evidence": evidence_level,
            "comparison": comparison,
        },
    )
