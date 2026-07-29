from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests._helpers import SCRIPTS


sys.path.insert(0, str(SCRIPTS))
from workflow_core import evidence as evidence_module  # noqa: E402
from workflow_core import runs as runs_module  # noqa: E402
from workflow_core.evidence import (  # noqa: E402
    EVIDENCE_EVENT_FIELDS,
    current_evidence_level,
    record_evidence_transition,
)
from workflow_core.project import init_project  # noqa: E402
from workflow_core.runs import (  # noqa: E402
    claim_run,
    close_run,
    create_run,
    finish_run,
    load_run,
    start_run,
    validate_frozen,
)
from workflow_core.tasking import (  # noqa: E402
    TASK_FIELDS,
    create_task,
    load_task,
    readiness,
    transition_task,
)
from workflow_core.validation import validate_boundaries_locked  # noqa: E402


_DEFAULT_ISSUE = object()
_create_run_live = create_run
_claim_run_live = claim_run
_record_evidence_transition_live = record_evidence_transition
_transition_validator_live = evidence_module._validate_transition_semantics


# 本文件保存的是旧版 v1 生命周期的回放用例。当前生产入口已经禁止新建
# “未绑定代码快照的论文证据”，所以这里只在测试夹具里关闭新规则，
# 让既有历史状态仍可验证；正式入口的拒绝行为由 formal gate 用例单独覆盖。
def create_run(*args, **kwargs):
    with mock.patch.object(runs_module, "_require_bound_for_new_evidence"):
        return _create_run_live(*args, **kwargs)


def claim_run(*args, **kwargs):
    with mock.patch.object(runs_module, "_require_bound_for_new_evidence"):
        return _claim_run_live(*args, **kwargs)


def record_evidence_transition(*args, **kwargs):
    def replay_legacy_transition(*validator_args, **validator_kwargs):
        validator_kwargs["live_write"] = False
        return _transition_validator_live(
            *validator_args,
            **validator_kwargs,
        )

    with mock.patch.object(
        evidence_module,
        "_validate_transition_semantics",
        side_effect=replay_legacy_transition,
    ):
        return _record_evidence_transition_live(*args, **kwargs)


class WorkflowStateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        init_project(self.project, "state-lines", layout="v2")
        self.control = self.project / ".experiment-workflow"

    def new_task(
        self,
        *,
        debug_required: bool = False,
        target_refs: list[str] | None = None,
        budget: dict[str, object] | None = None,
        stop_condition: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return create_task(
            self.project,
            owner_request="调一个最小候选",
            route="tune",
            target_refs=["VER-0001", "baseline"]
            if target_refs is None
            else target_refs,
            route_inputs={"debug_required": debug_required},
            budget={"max_runs": 1} if budget is None else budget,
            stop_condition={"max_failures": 1}
            if stop_condition is None
            else stop_condition,
        )

    def pass_gate(self, task: dict[str, object]) -> dict[str, object]:
        if task["stage"] == "queued":
            task = transition_task(self.project, task["id"], "preparing")
        self.assertEqual(
            {"status": "pass"},
            readiness(
                self.project,
                task["id"],
                target_clear=True,
                template_runnable=True,
                mutation_allowed=True,
                code_verified=True,
            ),
        )
        return load_task(self.project, task["id"])

    def ready_task(
        self,
        *,
        debug_required: bool = False,
        budget: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self.pass_gate(
            self.new_task(debug_required=debug_required, budget=budget)
        )

    def done_task(self) -> dict[str, object]:
        task = self.ready_task()
        task = transition_task(self.project, task["id"], "executing")
        task = transition_task(self.project, task["id"], "reviewing")
        return transition_task(
            self.project,
            task["id"],
            "done",
            conclusion={"summary": "完成"},
        )

    @staticmethod
    def frozen(seed: int = 7) -> dict[str, object]:
        return {
            "code": {"commit": "a" * 40},
            "config": {"lr": 0.001},
            "seed": seed,
            "data": {"split": "test"},
            "environment": {"python": "3.10"},
        }

    def finish(
        self,
        run_id: str,
        *,
        outcome: str = "succeeded",
        implementation: str = "valid",
        interface: str = "valid",
        data: str = "valid",
        metrics_quality: str = "valid",
        hypothesis: str = "supported",
        issue_kind: object = _DEFAULT_ISSUE,
    ) -> dict[str, object]:
        if issue_kind is _DEFAULT_ISSUE:
            issue_kind = (
                None
                if outcome == "succeeded"
                else "user_stop"
                if outcome == "stopped"
                else "implementation"
            )
        start_run(self.project, run_id, process_id=123)
        return finish_run(
            self.project,
            run_id,
            outcome=outcome,
            exit_code=0 if outcome == "succeeded" else 1,
            metrics={"score": 0.75},
            raw_log="artifacts/run.log",
            implementation=implementation,
            interface=interface,
            data=data,
            metrics_quality=metrics_quality,
            hypothesis=hypothesis,
            limitations=[],
            suggestions=[],
            artifacts=["artifacts/run.log"],
            issue_kind=issue_kind,  # type: ignore[arg-type]
        )


class TaskLifecycleTests(WorkflowStateTestCase):
    def test_readiness_checks_task_prerequisite_refs(self) -> None:
        missing = self.new_task(target_refs=["TASK-9999", "baseline"])
        missing = transition_task(self.project, missing["id"], "preparing")
        result = readiness(
            self.project,
            missing["id"],
            target_clear=True,
            template_runnable=True,
            mutation_allowed=True,
            code_verified=True,
        )
        self.assertEqual("fail", result["status"])
        self.assertTrue(any("不存在" in reason for reason in result["reasons"]))

        incomplete_prerequisite = self.new_task()
        incomplete = self.new_task(
            target_refs=[incomplete_prerequisite["id"]]
        )
        incomplete = transition_task(
            self.project, incomplete["id"], "preparing"
        )
        result = readiness(
            self.project,
            incomplete["id"],
            target_clear=True,
            template_runnable=True,
            mutation_allowed=True,
            code_verified=True,
        )
        self.assertEqual("fail", result["status"])
        self.assertTrue(any("未完成" in reason for reason in result["reasons"]))

        completed_prerequisite = self.done_task()
        dependent = self.new_task(target_refs=[completed_prerequisite["id"]])
        dependent = transition_task(self.project, dependent["id"], "preparing")
        self.assertEqual(
            {"status": "pass"},
            readiness(
                self.project,
                dependent["id"],
                target_clear=True,
                template_runnable=True,
                mutation_allowed=True,
                code_verified=True,
            ),
        )

        self_ref = self.new_task()
        self_path = self.control / "tasks" / f"{self_ref['id']}.json"
        self_payload = json.loads(self_path.read_text(encoding="utf-8"))
        self_payload["target_refs"] = [self_ref["id"]]
        self_path.write_text(json.dumps(self_payload), encoding="utf-8")
        self_ref = transition_task(self.project, self_ref["id"], "preparing")
        result = readiness(
            self.project,
            self_ref["id"],
            target_clear=True,
            template_runnable=True,
            mutation_allowed=True,
            code_verified=True,
        )
        self.assertEqual("fail", result["status"])
        self.assertTrue(any("自己" in reason for reason in result["reasons"]))

    def test_conclusion_exists_only_on_done_task(self) -> None:
        task = self.new_task()
        with self.assertRaises(ValueError):
            transition_task(
                self.project,
                task["id"],
                "preparing",
                conclusion={"too_early": True},
            )
        task = transition_task(self.project, task["id"], "preparing")
        task = self.pass_gate(task)
        task = transition_task(self.project, task["id"], "executing")
        task = transition_task(self.project, task["id"], "reviewing")
        task = transition_task(
            self.project,
            task["id"],
            "done",
            conclusion={"summary": "可信结论"},
        )
        self.assertEqual({"summary": "可信结论"}, task["conclusion"])

        invalid = self.new_task()
        invalid_path = self.control / "tasks" / f"{invalid['id']}.json"
        payload = json.loads(invalid_path.read_text(encoding="utf-8"))
        payload["conclusion"] = {"too_early": True}
        invalid_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            validate_boundaries_locked(self.control)

    def test_inputs_that_json_would_silently_change_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            create_task(
                self.project,
                owner_request="拒绝变形输入",
                route="tune",
                target_refs=["baseline"],
                route_inputs={1: "整数键"},
                budget={"max_runs": 1},
                stop_condition={"max_failures": 1},
            )
        self.assertEqual([], list((self.control / "tasks").iterdir()))

        task = self.ready_task()
        malformed_frozen = self.frozen()
        malformed_frozen["config"] = {1: "整数键"}
        with self.assertRaises(ValueError):
            create_run(
                self.project,
                task["id"],
                malformed_frozen,
                purpose="evidence",
            )
        self.assertEqual([], list((self.control / "runs").iterdir()))
        self.assertEqual([], load_task(self.project, task["id"])["run_refs"])

    def test_all_four_v2_routes_can_be_persisted_and_unknown_route_is_rejected(
        self,
    ) -> None:
        route_inputs = {
            "tune": {"config": {}},
            "innovation": {
                "config": {},
                "baseline": 0.7,
                "primary_metric": "score",
                "changes_code_behavior": True,
            },
            "ablation": {
                "config": {},
                "baseline": 0.7,
                "primary_metric": "score",
                "module_ref": "MOD-0001",
                "baseline_run_ref": "RUN-0001",
                "disabled_behavior": "关闭后严格退化为基线",
            },
            "reproduction": {
                "config": {},
                "baseline": 0.7,
                "primary_metric": "score",
                "source_ref": "SRC-0001",
                "source_run_ref": "RUN-0001",
                "tolerance": 0.01,
                "code_standard": {"cli_sha256": "a" * 64},
                "data_standard": {"split_sha256": "b" * 64},
            },
        }
        for route in ("tune", "ablation", "reproduction"):
            with self.subTest(route=route):
                target_refs = ["baseline"]
                if route == "ablation":
                    target_refs = ["MOD-0001", "RUN-0001"]
                elif route == "reproduction":
                    target_refs = ["SRC-0001", "RUN-0001"]
                task = create_task(
                    self.project,
                    owner_request=f"创建路线：{route}",
                    route=route,
                    target_refs=target_refs,
                    route_inputs=route_inputs[route],
                    budget={"max_runs": 1},
                    stop_condition={"max_failures": 1},
                )
                self.assertEqual(route, task["route"])
        with self.assertRaisesRegex(ValueError, "route"):
            create_task(
                self.project,
                owner_request="拒绝未知路线",
                route="other",
                target_refs=["baseline"],
                route_inputs={"config": {}},
                budget={"max_runs": 1},
                stop_condition={"max_failures": 1},
            )

        task = self.new_task()
        task_path = self.control / "tasks" / f"{task['id']}.json"
        payload = json.loads(task_path.read_text(encoding="utf-8"))
        payload["route"] = "other"
        task_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "route"):
            validate_boundaries_locked(self.control)

    def test_route_contract_refs_match_declared_task_targets_and_reject_unknowns(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "target_refs"):
            create_task(
                self.project,
                owner_request="拒绝写一套、跑一套的调参",
                route="tune",
                target_refs=["RUN-0002"],
                route_inputs={
                    "baseline": 0.7,
                    "primary_metric": "score",
                    "baseline_run_ref": "RUN-0001",
                    "allowed_changes": {"learning_rate": 0.0005},
                },
                budget={"max_runs": 1},
                stop_condition={"max_failures": 1},
            )
        with self.assertRaisesRegex(ValueError, "target_refs"):
            create_task(
                self.project,
                owner_request="拒绝写一套、跑一套的消融",
                route="ablation",
                target_refs=["MOD-0001", "RUN-0002"],
                route_inputs={
                    "baseline": 0.7,
                    "primary_metric": "score",
                    "module_ref": "MOD-0001",
                    "baseline_run_ref": "RUN-0001",
                    "disabled_behavior": "关闭后严格退化为基线",
                },
                budget={"max_runs": 1},
                stop_condition={"max_failures": 1},
            )
        with self.assertRaisesRegex(ValueError, "target_refs"):
            create_task(
                self.project,
                owner_request="拒绝写一套、跑一套的复现",
                route="reproduction",
                target_refs=["SRC-0002", "RUN-0001"],
                route_inputs={
                    "baseline": 0.7,
                    "primary_metric": "score",
                    "source_ref": "SRC-0001",
                    "source_run_ref": "RUN-0001",
                    "tolerance": 0.01,
                    "code_standard": {},
                    "data_standard": {},
                },
                budget={"max_runs": 1},
                stop_condition={"max_failures": 1},
            )
        with self.assertRaisesRegex(ValueError, "未知字段"):
            create_task(
                self.project,
                owner_request="拒绝复现路线中的静默字段",
                route="reproduction",
                target_refs=["SRC-0001", "RUN-0001"],
                route_inputs={
                    "baseline": 0.7,
                    "primary_metric": "score",
                    "source_ref": "SRC-0001",
                    "source_run_ref": "RUN-0001",
                    "tolerance": 0.01,
                    "code_standard": {},
                    "data_standard": {},
                    "target": 0.7,
                },
                budget={"max_runs": 1},
                stop_condition={"max_failures": 1},
            )

    def test_task_stores_exactly_ten_fields_and_moves_forward(self) -> None:
        task = self.new_task()

        self.assertEqual(TASK_FIELDS, set(task))
        self.assertEqual("queued", task["stage"])
        for stage in ("preparing", "ready", "executing", "reviewing", "done"):
            task = (
                self.pass_gate(task)
                if stage == "ready"
                else transition_task(self.project, task["id"], stage)
            )
            self.assertEqual(stage, task["stage"])

        with self.assertRaisesRegex(ValueError, "done"):
            transition_task(self.project, task["id"], "preparing")

    def test_every_unfinished_stage_can_stop_and_only_resume_via_preparing(self) -> None:
        forward = {
            "queued": (),
            "preparing": ("preparing",),
            "ready": ("preparing", "ready"),
            "executing": ("preparing", "ready", "executing"),
            "reviewing": ("preparing", "ready", "executing", "reviewing"),
        }
        for expected_stage, path in forward.items():
            with self.subTest(stage=expected_stage):
                task = self.new_task()
                for stage in path:
                    task = (
                        self.pass_gate(task)
                        if stage == "ready"
                        else transition_task(self.project, task["id"], stage)
                    )
                self.assertEqual(expected_stage, task["stage"])
                stopped = transition_task(self.project, task["id"], "stopped")
                self.assertEqual("stopped", stopped["stage"])
                with self.assertRaises(ValueError):
                    transition_task(self.project, task["id"], "ready")
                resumed = transition_task(self.project, task["id"], "preparing")
                self.assertEqual("preparing", resumed["stage"])

    def test_review_failure_returns_to_only_the_matching_stage(self) -> None:
        implementation_task = self.new_task()
        for stage in ("preparing", "ready", "executing", "reviewing"):
            implementation_task = (
                self.pass_gate(implementation_task)
                if stage == "ready"
                else transition_task(
                    self.project, implementation_task["id"], stage
                )
            )
        returned = transition_task(
            self.project,
            implementation_task["id"],
            "preparing",
            issue_kind="implementation",
        )
        self.assertEqual("preparing", returned["stage"])

        environment_task = self.new_task()
        for stage in ("preparing", "ready", "executing", "reviewing"):
            environment_task = (
                self.pass_gate(environment_task)
                if stage == "ready"
                else transition_task(
                    self.project, environment_task["id"], stage
                )
            )
        returned = transition_task(
            self.project,
            environment_task["id"],
            "ready",
            issue_kind="environment",
        )
        self.assertEqual("ready", returned["stage"])

        mismatched_task = self.new_task()
        for stage in ("preparing", "ready", "executing", "reviewing"):
            mismatched_task = (
                self.pass_gate(mismatched_task)
                if stage == "ready"
                else transition_task(
                    self.project, mismatched_task["id"], stage
                )
            )
        with self.assertRaisesRegex(ValueError, "environment"):
            transition_task(
                self.project,
                mismatched_task["id"],
                "ready",
                issue_kind="implementation",
            )

    def test_readiness_has_only_pass_or_fail_with_five_reason_categories(self) -> None:
        queued = self.new_task()
        with self.assertRaisesRegex(ValueError, "preparing"):
            readiness(
                self.project,
                queued["id"],
                target_clear=True,
                template_runnable=True,
                mutation_allowed=True,
                code_verified=True,
            )
        task = self.new_task(budget={}, stop_condition={})
        task = transition_task(self.project, task["id"], "preparing")
        before = list((self.control / "runs").iterdir())

        failed = readiness(
            self.project,
            task["id"],
            target_clear=False,
            template_runnable=False,
            mutation_allowed=False,
            code_verified=False,
        )

        self.assertEqual({"status", "reasons"}, set(failed))
        self.assertEqual("fail", failed["status"])
        self.assertEqual(5, len(failed["reasons"]))
        self.assertEqual("preparing", load_task(self.project, task["id"])["stage"])
        self.assertEqual(before, list((self.control / "runs").iterdir()))
        with self.assertRaisesRegex(ValueError, "ready"):
            create_run(
                self.project, task["id"], self.frozen(), purpose="evidence"
            )
        self.assertEqual(before, list((self.control / "runs").iterdir()))

        ready_task = self.new_task()
        ready_task = transition_task(
            self.project, ready_task["id"], "preparing"
        )
        self.assertEqual(
            {"status": "pass"},
            readiness(
                self.project,
                ready_task["id"],
                target_clear=True,
                template_runnable=True,
                mutation_allowed=True,
                code_verified=True,
            ),
        )
        self.assertEqual(
            "ready", load_task(self.project, ready_task["id"])["stage"]
        )


    def test_transition_rejects_missing_direct_run_reference(self) -> None:
        task = self.new_task()
        path = self.control / "tasks" / f"{task['id']}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["run_refs"] = ["RUN-9999"]
        path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Run"):
            transition_task(self.project, task["id"], "preparing")

    def test_full_validation_rejects_ready_task_with_unfinished_prerequisite(
        self,
    ) -> None:
        prerequisite = self.new_task()
        task = self.ready_task()
        path = self.control / "tasks" / f"{task['id']}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["target_refs"] = [prerequisite["id"]]
        path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Task"):
            validate_boundaries_locked(self.control)

    def test_preparing_task_may_wait_for_unfinished_prerequisite(self) -> None:
        prerequisite = self.new_task()
        task = self.new_task(target_refs=[prerequisite["id"]])
        transition_task(self.project, task["id"], "preparing")

        validate_boundaries_locked(self.control)
        result = readiness(
            self.project,
            task["id"],
            target_clear=True,
            template_runnable=True,
            mutation_allowed=True,
            code_verified=True,
        )
        self.assertEqual("fail", result["status"])

    def test_full_validation_always_rejects_self_prerequisite(self) -> None:
        task = self.new_task()
        path = self.control / "tasks" / f"{task['id']}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["target_refs"] = [task["id"]]
        path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Task"):
            validate_boundaries_locked(self.control)


class RunLifecycleTests(WorkflowStateTestCase):
    def test_claim_run_rechecks_budget_inside_the_atomic_write_lock(self) -> None:
        task = self.ready_task(budget={"max_runs": 1})
        first = create_run(
            self.project,
            task["id"],
            self.frozen(),
            purpose="debug",
        )
        with self.assertRaisesRegex(ValueError, "预算已用完"):
            claim_run(
                self.project,
                task["id"],
                self.frozen(seed=8),
                purpose="debug",
            )
        self.assertEqual(
            [first["id"]],
            load_task(self.project, task["id"])["run_refs"],
        )
        self.assertEqual(
            [first["id"]],
            [path.name for path in (self.control / "runs").iterdir()],
        )

    def test_outcome_and_issue_kind_accept_only_legal_pairs(self) -> None:
        valid = (
            ("succeeded", None),
            ("failed", "implementation"),
            ("failed", "prerequisite"),
            ("failed", "environment"),
            ("failed", "temporary_resource"),
            ("stopped", "user_stop"),
        )
        for outcome, issue_kind in valid:
            with self.subTest(valid=(outcome, issue_kind)):
                task = self.ready_task()
                run = create_run(
                    self.project, task["id"], self.frozen(), purpose="evidence"
                )
                finished = self.finish(
                    run["id"],
                    outcome=outcome,
                    hypothesis="supported" if outcome == "succeeded" else "inconclusive",
                    issue_kind=issue_kind,
                )
                self.assertEqual(issue_kind, finished["execution"]["issue_kind"])

        invalid = (
            ("succeeded", "implementation"),
            ("succeeded", "user_stop"),
            ("failed", None),
            ("failed", "user_stop"),
            ("stopped", None),
            ("stopped", "environment"),
        )
        for outcome, issue_kind in invalid:
            with self.subTest(invalid=(outcome, issue_kind)):
                task = self.ready_task()
                run = create_run(
                    self.project, task["id"], self.frozen(), purpose="evidence"
                )
                with self.assertRaisesRegex(ValueError, "outcome.*issue_kind"):
                    self.finish(
                        run["id"],
                        outcome=outcome,
                        hypothesis="not_evaluated",
                        issue_kind=issue_kind,
                    )

    def test_full_validation_rejects_tampered_outcome_issue_pair(self) -> None:
        task = self.ready_task()
        run = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        finished = self.finish(run["id"])
        run_path = self.control / "runs" / finished["id"] / "run.json"
        payload = json.loads(run_path.read_text(encoding="utf-8"))
        payload["execution"]["issue_kind"] = "environment"
        run_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outcome.*issue_kind"):
            validate_boundaries_locked(self.control)

    def test_run_persists_and_strictly_validates_issue_kind(self) -> None:
        task = self.ready_task()
        run = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        finished = self.finish(
            run["id"],
            outcome="failed",
            hypothesis="not_evaluated",
            issue_kind="environment",
        )
        self.assertEqual("environment", finished["execution"]["issue_kind"])
        run_path = self.control / "runs" / run["id"] / "run.json"
        payload = json.loads(run_path.read_text(encoding="utf-8"))
        payload["execution"]["issue_kind"] = "mystery"
        run_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "issue_kind"):
            load_run(self.project, run["id"])

    def close_for_repeat(
        self, run: dict[str, object], *, purpose: str
    ) -> dict[str, object]:
        self.finish(
            run["id"],
            hypothesis="not_evaluated" if purpose == "debug" else "supported",
        )
        record_evidence_transition(
            self.project,
            run["id"],
            "debug" if purpose == "debug" else "single_run",
            evidence_refs=[run["id"]],
            reason="repeat_of fixture",
            proposed_by="analyst-1",
            checked_by="reviewer-1",
            applied_by="coordinator-1",
        )
        return close_run(self.project, run["id"])

    def set_repeat_of(self, run_id: str, repeat_of: str | None) -> None:
        path = self.control / "runs" / run_id / "run.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["repeat_of"] = repeat_of
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_repeat_creation_requires_same_closed_task_and_purpose(self) -> None:
        task = self.ready_task(budget={"max_runs": 4})
        unfinished = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        with self.assertRaisesRegex(ValueError, "closed"):
            create_run(
                self.project,
                task["id"],
                self.frozen(seed=8),
                purpose="evidence",
                repeat_of=unfinished["id"],
            )
        prior = self.close_for_repeat(unfinished, purpose="evidence")

        valid = create_run(
            self.project,
            task["id"],
            self.frozen(seed=8),
            purpose="evidence",
            repeat_of=prior["id"],
        )
        self.assertEqual(prior["id"], valid["repeat_of"])

        other_task = self.ready_task()
        with self.assertRaisesRegex(ValueError, "Task"):
            create_run(
                self.project,
                other_task["id"],
                self.frozen(seed=9),
                purpose="evidence",
                repeat_of=prior["id"],
            )

        debug = create_run(
            self.project, task["id"], self.frozen(seed=10), purpose="debug"
        )
        debug = self.close_for_repeat(debug, purpose="debug")
        with self.assertRaisesRegex(ValueError, "purpose"):
            create_run(
                self.project,
                task["id"],
                self.frozen(seed=11),
                purpose="evidence",
                repeat_of=debug["id"],
            )

    def test_start_and_finish_reject_broken_task_backlink(self) -> None:
        task = self.ready_task()
        run = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        task_path = self.control / "tasks" / f"{task['id']}.json"
        payload = json.loads(task_path.read_text(encoding="utf-8"))
        payload["run_refs"] = []
        task_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Task"):
            start_run(self.project, run["id"])

        payload["run_refs"] = [run["id"]]
        task_path.write_text(json.dumps(payload), encoding="utf-8")
        start_run(self.project, run["id"])
        payload["run_refs"] = []
        task_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Task"):
            finish_run(
                self.project,
                run["id"],
                outcome="succeeded",
                exit_code=0,
                metrics={},
                raw_log=None,
                implementation="valid",
                interface="valid",
                data="valid",
                metrics_quality="valid",
                hypothesis="supported",
                limitations=[],
                suggestions=[],
                artifacts=[],
            )

    def test_start_and_finish_revalidate_direct_repeat_reference(self) -> None:
        task = self.ready_task(budget={"max_runs": 4})
        prior = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        prior = self.close_for_repeat(prior, purpose="evidence")

        start_candidate = create_run(
            self.project,
            task["id"],
            self.frozen(seed=8),
            purpose="evidence",
            repeat_of=prior["id"],
        )
        self.set_repeat_of(start_candidate["id"], start_candidate["id"])
        with self.assertRaises(ValueError):
            start_run(self.project, start_candidate["id"])
        self.set_repeat_of(start_candidate["id"], prior["id"])

        finish_candidate = create_run(
            self.project,
            task["id"],
            self.frozen(seed=9),
            purpose="evidence",
            repeat_of=prior["id"],
        )
        start_run(self.project, finish_candidate["id"])
        later = create_run(
            self.project,
            task["id"],
            self.frozen(seed=10),
            purpose="evidence",
        )
        self.set_repeat_of(finish_candidate["id"], later["id"])
        with self.assertRaises(ValueError):
            finish_run(
                self.project,
                finish_candidate["id"],
                outcome="succeeded",
                exit_code=0,
                metrics={},
                raw_log=None,
                implementation="valid",
                interface="valid",
                data="valid",
                metrics_quality="valid",
                hypothesis="supported",
                limitations=[],
                suggestions=[],
                artifacts=[],
            )

    def test_repeat_validation_rejects_bad_links_and_cycles(self) -> None:
        task = self.ready_task(budget={"max_runs": 5})
        prior = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        prior = self.close_for_repeat(prior, purpose="evidence")
        debug = create_run(
            self.project, task["id"], self.frozen(seed=8), purpose="debug"
        )
        debug = self.close_for_repeat(debug, purpose="debug")
        unfinished = create_run(
            self.project, task["id"], self.frozen(seed=9), purpose="evidence"
        )
        current = create_run(
            self.project, task["id"], self.frozen(seed=10), purpose="evidence"
        )
        later = create_run(
            self.project, task["id"], self.frozen(seed=11), purpose="evidence"
        )
        cross_task = self.ready_task()
        cross = create_run(
            self.project,
            cross_task["id"],
            self.frozen(seed=12),
            purpose="evidence",
        )
        cross = self.close_for_repeat(cross, purpose="evidence")

        invalid_refs = {
            "self": current["id"],
            "later": later["id"],
            "cross Task": cross["id"],
            "purpose": debug["id"],
            "not closed": unfinished["id"],
        }
        for label, repeat_of in invalid_refs.items():
            with self.subTest(label=label):
                self.set_repeat_of(current["id"], repeat_of)
                with self.assertRaises(ValueError):
                    validate_boundaries_locked(self.control)
                self.set_repeat_of(current["id"], None)

        self.set_repeat_of(current["id"], prior["id"])
        self.set_repeat_of(prior["id"], current["id"])
        with self.assertRaises(ValueError):
            validate_boundaries_locked(self.control)

    def test_run_creation_requires_ready_for_both_purposes(self) -> None:
        tasks: dict[str, dict[str, object]] = {}
        tasks["queued"] = self.new_task()
        tasks["preparing"] = transition_task(
            self.project, self.new_task()["id"], "preparing"
        )
        reviewing = self.ready_task()
        reviewing = transition_task(self.project, reviewing["id"], "executing")
        tasks["reviewing"] = transition_task(
            self.project, reviewing["id"], "reviewing"
        )
        tasks["stopped"] = transition_task(
            self.project, self.new_task()["id"], "stopped"
        )
        done = self.ready_task()
        done = transition_task(self.project, done["id"], "executing")
        done = transition_task(self.project, done["id"], "reviewing")
        tasks["done"] = transition_task(self.project, done["id"], "done")

        before = list((self.control / "runs").iterdir())
        for stage, task in tasks.items():
            for purpose in ("debug", "evidence"):
                with self.subTest(stage=stage, purpose=purpose):
                    with self.assertRaisesRegex(ValueError, "ready"):
                        create_run(
                            self.project,
                            task["id"],
                            self.frozen(),
                            purpose=purpose,
                        )
                    self.assertEqual(
                        before, list((self.control / "runs").iterdir())
                    )

    def test_run_has_one_main_file_and_frozen_digest_detects_tampering(self) -> None:
        task = self.ready_task()
        run = create_run(
            self.project,
            task["id"],
            self.frozen(),
            purpose="evidence",
        )
        run_dir = self.control / "runs" / run["id"]

        self.assertEqual({"run.json"}, {entry.name for entry in run_dir.iterdir()})
        self.assertTrue(validate_frozen(run))
        run_path = run_dir / "run.json"
        payload = json.loads(run_path.read_text(encoding="utf-8"))
        payload["frozen"]["config"]["lr"] = 0.002
        run_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "frozen"):
            load_run(self.project, run["id"])
        with self.assertRaisesRegex(ValueError, "frozen"):
            validate_boundaries_locked(self.control)

    def test_success_failure_and_stop_close_only_after_evidence(self) -> None:
        for outcome in ("succeeded", "failed", "stopped"):
            with self.subTest(outcome=outcome):
                task = self.ready_task()
                run = create_run(
                    self.project,
                    task["id"],
                    self.frozen(),
                    purpose="evidence",
                )
                finished = self.finish(
                    run["id"],
                    outcome=outcome,
                    hypothesis="supported"
                    if outcome == "succeeded"
                    else "inconclusive",
                )
                self.assertEqual("finished", finished["execution"]["stage"])
                with self.assertRaisesRegex(ValueError, "证据"):
                    close_run(self.project, run["id"])

                level = "single_run" if outcome == "succeeded" else "disqualified"
                record_evidence_transition(
                    self.project,
                    run["id"],
                    level,
                    evidence_refs=[run["id"]],
                    reason=f"记录 {outcome}",
                    proposed_by="analyst-1",
                    checked_by="reviewer-1",
                    applied_by="coordinator-1",
                )
                closed = close_run(self.project, run["id"])
                self.assertEqual("closed", closed["execution"]["stage"])
                self.assertEqual(outcome, closed["execution"]["outcome"])
                with self.assertRaises(ValueError):
                    start_run(self.project, run["id"])

    def test_retry_always_creates_a_new_run(self) -> None:
        task = self.ready_task(budget={"max_runs": 2})
        first = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        self.finish(first["id"])
        record_evidence_transition(
            self.project,
            first["id"],
            "single_run",
            evidence_refs=[first["id"]],
            reason="首次可信运行",
            proposed_by="analyst-1",
            checked_by="reviewer-1",
            applied_by="coordinator-1",
        )
        close_run(self.project, first["id"])

        retry = create_run(
            self.project,
            task["id"],
            self.frozen(seed=8),
            purpose="evidence",
            repeat_of=first["id"],
        )

        self.assertNotEqual(first["id"], retry["id"])
        self.assertEqual(first["id"], retry["repeat_of"])
        with self.assertRaises(ValueError):
            start_run(self.project, first["id"])

    def test_implementation_and_hypothesis_are_independent_but_not_misleading(self) -> None:
        task = self.ready_task()
        invalid = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        start_run(self.project, invalid["id"])
        with self.assertRaisesRegex(ValueError, "not_supported"):
            finish_run(
                self.project,
                invalid["id"],
                outcome="failed",
                exit_code=1,
                metrics={},
                raw_log=None,
                implementation="invalid",
                interface="invalid",
                data="uncertain",
                metrics_quality="uncertain",
                hypothesis="not_supported",
                limitations=[],
                suggestions=[],
                artifacts=[],
                issue_kind="implementation",
            )

        finished = finish_run(
            self.project,
            invalid["id"],
            outcome="failed",
            exit_code=1,
            metrics={},
            raw_log=None,
            implementation="invalid",
            interface="invalid",
            data="uncertain",
            metrics_quality="uncertain",
            hypothesis="not_evaluated",
            limitations=[],
            suggestions=[],
            artifacts=[],
            issue_kind="implementation",
        )
        self.assertEqual("invalid", finished["quality"]["implementation"])
        self.assertEqual("not_evaluated", finished["analysis"]["hypothesis"])


class EvidenceLifecycleTests(WorkflowStateTestCase):
    def event(self, run_id: str, to_level: str) -> dict[str, object]:
        return record_evidence_transition(
            self.project,
            run_id,
            to_level,
            evidence_refs=[run_id],
            reason=f"转到 {to_level}",
            proposed_by="analyst-1",
            checked_by="reviewer-1",
            applied_by="coordinator-1",
        )

    def raw_event(
        self,
        subject_id: str,
        to_level: str,
        *,
        evidence_refs: list[str],
    ) -> dict[str, object]:
        return {
            "event_id": "EVT-0001",
            "subject_type": "Run",
            "subject_id": subject_id,
            "from": "none",
            "to": to_level,
            "evidence_refs": evidence_refs,
            "reason": "手工反例",
            "proposed_by": "analyst-1",
            "checked_by": "reviewer-1",
            "applied_by": "coordinator-1",
            "time": datetime.now(timezone.utc).isoformat(),
        }

    def write_raw_event(self, event: dict[str, object]) -> None:
        (self.control / "evidence.jsonl").write_text(
            json.dumps(event, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def finished_debug_pair(self) -> tuple[dict[str, object], dict[str, object]]:
        runs: list[dict[str, object]] = []
        for seed in (7, 8):
            task = self.ready_task(debug_required=True)
            run = create_run(
                self.project,
                task["id"],
                self.frozen(seed=seed),
                purpose="debug",
            )
            self.finish(run["id"], hypothesis="not_evaluated")
            runs.append(run)
        return runs[0], runs[1]

    def test_debug_run_can_only_end_at_debug_or_disqualified(self) -> None:
        task = self.ready_task(debug_required=True)
        run = create_run(
            self.project, task["id"], self.frozen(), purpose="debug"
        )
        self.finish(run["id"], hypothesis="not_evaluated")
        event = self.event(run["id"], "debug")

        self.assertEqual(EVIDENCE_EVENT_FIELDS, set(event))
        self.assertEqual("none", event["from"])
        self.assertEqual("debug", event["to"])
        self.assertEqual("debug", current_evidence_level(self.project, run["id"]))
        with self.assertRaisesRegex(ValueError, "debug"):
            self.event(run["id"], "single_run")

    def test_single_run_rejects_pending_failed_stopped_and_untrusted_quality(
        self,
    ) -> None:
        pending_task = self.ready_task()
        pending = create_run(
            self.project,
            pending_task["id"],
            self.frozen(),
            purpose="evidence",
        )
        with self.assertRaises(ValueError):
            self.event(pending["id"], "single_run")

        for outcome in ("failed", "stopped"):
            with self.subTest(outcome=outcome):
                task = self.ready_task()
                run = create_run(
                    self.project,
                    task["id"],
                    self.frozen(),
                    purpose="evidence",
                )
                self.finish(run["id"], outcome=outcome, hypothesis="inconclusive")
                with self.assertRaises(ValueError):
                    self.event(run["id"], "single_run")

        for dimension in ("implementation", "interface", "data", "metrics_quality"):
            for status in ("invalid", "uncertain"):
                with self.subTest(dimension=dimension, status=status):
                    task = self.ready_task()
                    run = create_run(
                        self.project,
                        task["id"],
                        self.frozen(),
                        purpose="evidence",
                    )
                    quality = {
                        "implementation": "valid",
                        "interface": "valid",
                        "data": "valid",
                        "metrics_quality": "valid",
                    }
                    quality[dimension] = status
                    hypothesis = (
                        "not_evaluated"
                        if dimension == "implementation" and status == "invalid"
                        else "inconclusive"
                    )
                    self.finish(
                        run["id"], hypothesis=hypothesis, **quality
                    )
                    with self.assertRaises(ValueError):
                        self.event(run["id"], "single_run")

        self.assertEqual(b"", (self.control / "evidence.jsonl").read_bytes())

    def test_replay_rejects_manual_single_run_for_untrusted_run(self) -> None:
        task = self.ready_task()
        run = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        self.finish(
            run["id"],
            interface="uncertain",
            hypothesis="inconclusive",
        )
        self.write_raw_event(
            self.raw_event(
                run["id"], "single_run", evidence_refs=[run["id"]]
            )
        )

        with self.assertRaises(ValueError):
            validate_boundaries_locked(self.control)

    def test_first_event_api_rejects_subject_plus_unrelated_run(self) -> None:
        subject, unrelated = self.finished_debug_pair()
        with self.assertRaises(ValueError):
            record_evidence_transition(
                self.project,
                subject["id"],
                "debug",
                evidence_refs=[subject["id"], unrelated["id"]],
                reason="不能夹带无关 Run",
                proposed_by="analyst-1",
                checked_by="reviewer-1",
                applied_by="coordinator-1",
            )
        self.assertEqual(b"", (self.control / "evidence.jsonl").read_bytes())

    def test_replay_rejects_first_event_subject_plus_unrelated_run(self) -> None:
        subject, unrelated = self.finished_debug_pair()
        self.write_raw_event(
            self.raw_event(
                subject["id"],
                "debug",
                evidence_refs=[subject["id"], unrelated["id"]],
            )
        )
        with self.assertRaises(ValueError):
            validate_boundaries_locked(self.control)

    def test_evidence_run_requires_debug_closure_unless_project_waives_it(self) -> None:
        required = self.ready_task(
            debug_required=True, budget={"max_runs": 2}
        )
        evidence_run = create_run(
            self.project, required["id"], self.frozen(), purpose="evidence"
        )
        self.finish(evidence_run["id"])
        with self.assertRaisesRegex(ValueError, "debug"):
            self.event(evidence_run["id"], "single_run")

        debug_run = create_run(
            self.project, required["id"], self.frozen(), purpose="debug"
        )
        self.finish(debug_run["id"], hypothesis="not_evaluated")
        self.event(debug_run["id"], "debug")
        close_run(self.project, debug_run["id"])
        self.assertEqual(
            "single_run",
            self.event(evidence_run["id"], "single_run")["to"],
        )

        waived = self.ready_task(debug_required=False)
        waived_run = create_run(
            self.project, waived["id"], self.frozen(), purpose="evidence"
        )
        self.finish(waived_run["id"])
        self.assertEqual("single_run", self.event(waived_run["id"], "single_run")["to"])

    def test_disqualified_debug_no_longer_unlocks_single_run(self) -> None:
        task = self.ready_task(
            debug_required=True, budget={"max_runs": 2}
        )
        debug_run = create_run(
            self.project, task["id"], self.frozen(), purpose="debug"
        )
        self.finish(debug_run["id"], hypothesis="not_evaluated")
        self.event(debug_run["id"], "debug")
        close_run(self.project, debug_run["id"])
        self.event(debug_run["id"], "disqualified")

        evidence_run = create_run(
            self.project,
            task["id"],
            self.frozen(seed=8),
            purpose="evidence",
        )
        self.finish(evidence_run["id"])
        with self.assertRaisesRegex(ValueError, "debug"):
            self.event(evidence_run["id"], "single_run")

    def test_single_run_requires_valid_implementation(self) -> None:
        task = self.ready_task()
        run = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        self.finish(
            run["id"],
            implementation="not_applicable",
            hypothesis="inconclusive",
        )

        with self.assertRaisesRegex(ValueError, "implementation"):
            self.event(run["id"], "single_run")

    def test_single_run_can_confirm_then_revoke_and_never_revive(self) -> None:
        task = self.ready_task()
        run = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        self.finish(run["id"])
        self.event(run["id"], "single_run")
        self.event(run["id"], "confirmed")
        self.event(run["id"], "revoked")

        self.assertEqual("revoked", current_evidence_level(self.project, run["id"]))
        with self.assertRaises(ValueError):
            self.event(run["id"], "single_run")

    def test_disqualified_is_terminal_and_old_events_remain_unchanged(self) -> None:
        task = self.ready_task()
        run = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        self.finish(
            run["id"],
            outcome="failed",
            implementation="invalid",
            hypothesis="not_evaluated",
        )
        self.event(run["id"], "disqualified")
        ledger = self.control / "evidence.jsonl"
        original = ledger.read_bytes()

        with self.assertRaises(ValueError):
            self.event(run["id"], "single_run")

        self.assertEqual(original, ledger.read_bytes())
        self.assertEqual("disqualified", current_evidence_level(self.project, run["id"]))

    def test_evidence_append_rejects_an_invalid_control_plane(self) -> None:
        task = self.ready_task()
        run = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        self.finish(run["id"])
        ledger = self.control / "evidence.jsonl"
        original = ledger.read_bytes()
        (self.control / "unexpected").mkdir()

        with self.assertRaises(ValueError):
            self.event(run["id"], "single_run")

        self.assertEqual(original, ledger.read_bytes())

    def test_validator_rejects_extra_task_fields_run_links_and_bad_ledger(self) -> None:
        task = self.ready_task()
        task_path = self.control / "tasks" / f"{task['id']}.json"
        payload = json.loads(task_path.read_text(encoding="utf-8"))
        payload["runner_plan"] = "不应保存"
        task_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Task"):
            validate_boundaries_locked(self.control)

        payload.pop("runner_plan")
        task_path.write_text(json.dumps(payload), encoding="utf-8")
        run = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        run_path = self.control / "runs" / run["id"] / "run.json"
        target = self.project / "outside-run.json"
        target.write_text(run_path.read_text(encoding="utf-8"), encoding="utf-8")
        run_path.unlink()
        try:
            os.symlink(target, run_path)
        except OSError as error:
            self.skipTest(f"当前平台不能安全创建文件符号链接：{error}")
        with self.assertRaisesRegex(ValueError, "Run"):
            validate_boundaries_locked(self.control)


class StrictJsonTests(WorkflowStateTestCase):
    def test_task_json_rejects_nested_duplicate_keys(self) -> None:
        task = self.new_task()
        path = self.control / "tasks" / f"{task['id']}.json"
        content = json.dumps(task, ensure_ascii=False)
        original = '"route_inputs": {"debug_required": false}'
        duplicate = (
            '"route_inputs": {"debug_required": true, '
            '"debug_required": false}'
        )
        self.assertIn(original, content)
        path.write_text(content.replace(original, duplicate), encoding="utf-8")

        with self.assertRaises(ValueError):
            validate_boundaries_locked(self.control)


    def test_run_json_rejects_nested_duplicate_keys(self) -> None:
        task = self.ready_task()
        run = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        path = self.control / "runs" / run["id"] / "run.json"
        content = json.dumps(run, ensure_ascii=False)
        marker = '"execution": {'
        self.assertIn(marker, content)
        path.write_text(
            content.replace(
                marker,
                '"execution": {"stage": "closed", ',
                1,
            ),
            encoding="utf-8",
        )

        with self.assertRaises(ValueError):
            validate_boundaries_locked(self.control)


class PerformanceStructureTests(WorkflowStateTestCase):
    def test_ordinary_task_and_run_commands_do_not_call_full_validator(self) -> None:
        with mock.patch(
            "workflow_core.validation.validate_boundaries_locked",
            side_effect=AssertionError("普通命令不应全库扫描"),
        ):
            task = self.new_task()
            task = transition_task(self.project, task["id"], "preparing")
            task = self.pass_gate(task)
            run = create_run(
                self.project,
                task["id"],
                self.frozen(),
                purpose="evidence",
            )
            start_run(self.project, run["id"], process_id=123)
            finish_run(
                self.project,
                run["id"],
                outcome="succeeded",
                exit_code=0,
                metrics={"score": 0.75},
                raw_log="artifacts/run.log",
                implementation="valid",
                interface="valid",
                data="valid",
                metrics_quality="valid",
                hypothesis="supported",
                limitations=[],
                suggestions=[],
                artifacts=["artifacts/run.log"],
            )

    def test_evidence_and_close_each_parse_ledger_once(self) -> None:
        import workflow_core.evidence as evidence_module

        task = self.ready_task()
        run = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        self.finish(run["id"])
        ledger = self.control / "evidence.jsonl"
        original_open = Path.open
        ledger_reads = 0

        def counting_open(path: Path, *args: object, **kwargs: object):
            nonlocal ledger_reads
            mode = args[0] if args else kwargs.get("mode", "r")
            if path == ledger and mode == "rb":
                ledger_reads += 1
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", new=counting_open), mock.patch.object(
            evidence_module,
            "validate_evidence_ledger_locked",
            wraps=evidence_module.validate_evidence_ledger_locked,
        ) as ledger_spy:
            record_evidence_transition(
                self.project,
                run["id"],
                "single_run",
                evidence_refs=[run["id"]],
                reason="单次解析",
                proposed_by="analyst-1",
                checked_by="reviewer-1",
                applied_by="coordinator-1",
            )
            self.assertEqual(1, ledger_spy.call_count)
            self.assertEqual(1, ledger_reads)

        ledger_reads = 0
        with mock.patch.object(Path, "open", new=counting_open), mock.patch.object(
            evidence_module,
            "validate_evidence_ledger_locked",
            wraps=evidence_module.validate_evidence_ledger_locked,
        ) as ledger_spy:
            close_run(self.project, run["id"])
            self.assertEqual(1, ledger_spy.call_count)
            self.assertEqual(1, ledger_reads)

class EvidenceStrictJsonTests(WorkflowStateTestCase):
    def test_oversized_evidence_is_rejected_before_body_read(self) -> None:
        ledger = self.control / "evidence.jsonl"
        with ledger.open("wb") as target:
            target.truncate(1024 * 1024 + 1)
        original_open = Path.open
        reads = 0

        def counting_open(path: Path, *args: object, **kwargs: object):
            nonlocal reads
            mode = args[0] if args else kwargs.get("mode", "r")
            if path == ledger and mode == "rb":
                reads += 1
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", new=counting_open):
            with self.assertRaisesRegex(ValueError, "1 MiB"):
                validate_boundaries_locked(self.control)
        self.assertEqual(0, reads)

    def test_evidence_jsonl_rejects_duplicate_keys(self) -> None:
        task = self.ready_task()
        run = create_run(
            self.project, task["id"], self.frozen(), purpose="evidence"
        )
        self.finish(run["id"])
        event = {
            "event_id": "EVT-0001",
            "subject_type": "Run",
            "subject_id": run["id"],
            "from": "none",
            "to": "single_run",
            "evidence_refs": [run["id"]],
            "reason": "重复键反例",
            "proposed_by": "analyst-1",
            "checked_by": "reviewer-1",
            "applied_by": "coordinator-1",
            "time": datetime.now(timezone.utc).isoformat(),
        }
        content = json.dumps(event, ensure_ascii=False)
        original = '"to": "single_run"'
        self.assertIn(original, content)
        (self.control / "evidence.jsonl").write_text(
            content.replace(
                original,
                '"to": "debug", "to": "single_run"',
            )
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(ValueError):
            validate_boundaries_locked(self.control)


if __name__ == "__main__":
    unittest.main()
