from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from tests._helpers import SCRIPTS, cli_json, run_cli


sys.path.insert(0, str(SCRIPTS))
from workflow_core.evidence import record_evidence_transition  # noqa: E402
from workflow_core.project import init_project, is_link_or_reparse  # noqa: E402
from workflow_core.runs import close_run, finish_run, start_run  # noqa: E402
from workflow_core.tasking import (  # noqa: E402
    create_task,
    readiness,
    transition_task,
)


INTERPRETATION_FIELDS = {
    "schema",
    "intent",
    "route",
    "source",
    "understood",
    "based_on",
    "available",
    "missing",
    "next_step",
    "needs_clarification",
    "needs_confirmation",
    "structured_action",
}


class HumanEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        init_project(self.project, "人话入口", layout="v2")
        self.control = self.project / ".experiment-workflow"

    @staticmethod
    def _snapshot(root: Path) -> dict[str, tuple[str, str | None, int, int]]:
        snapshot: dict[str, tuple[str, str | None, int, int]] = {}
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if is_link_or_reparse(path):
                kind = "link"
                digest = hashlib.sha256(os.readlink(path).encode("utf-8")).hexdigest()
            elif stat.S_ISDIR(info.st_mode):
                kind = "directory"
                digest = None
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                kind = "other"
                digest = None
            snapshot[relative] = (kind, digest, info.st_size, info.st_mtime_ns)
        return snapshot

    def _new_task(
        self,
        *,
        request: str = "调一个学习率候选",
        authorization: bool = False,
        route: str = "tune",
        max_runs: int = 1,
        project: Path | None = None,
    ) -> dict[str, object]:
        selected_project = self.project if project is None else project
        return create_task(
            selected_project,
            owner_request=request,
            route=route,
            target_refs=["baseline"],
            route_inputs={
                "config": {"learning_rate": 0.001},
                "seed": 7,
                "debug_required": False,
                "execution_authorized": authorization,
                "plan_unchanged": authorization,
            },
            budget={"max_runs": max_runs},
            stop_condition={"max_failures": 1},
        )

    def _ready_task(self, *, authorization: bool = False) -> dict[str, object]:
        task = self._new_task(authorization=authorization)
        transition_task(self.project, task["id"], "preparing")
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
        return cli_json("workflow-status", "--project", self.project)

    def test_fallback_distinguishes_all_eight_requests(self) -> None:
        from workflow_core.coordinator import interpret_request

        cases = {
            "请读这篇论文并整理创新点": "paper",
            "把这套代码整理成模板": "code",
            "帮我调参": "tune",
            "做一个消融实验": "ablation",
            "复现这个结果": "reproduction",
            "试这个 Idea 做创新实验": "innovation",
            "查一下状态": "status",
            "继续上次任务": "continue",
        }
        before = self._snapshot(self.control)
        for request, expected in cases.items():
            with self.subTest(request=request):
                result = interpret_request(self.project, request)
                self.assertEqual(INTERPRETATION_FIELDS, set(result))
                self.assertEqual(expected, result["intent"])
                self.assertEqual("fallback", result["source"])
        self.assertEqual(before, self._snapshot(self.control))

    def test_innovation_short_requests_are_exact_without_stealing_paper_idea(self) -> None:
        from workflow_core.coordinator import interpret_request

        self.assertEqual(
            "innovation", interpret_request(self.project, "创新")["intent"]
        )
        self.assertEqual(
            "innovation", interpret_request(self.project, "试这个创新")["intent"]
        )
        self.assertEqual(
            "paper", interpret_request(self.project, "整理创新点")["intent"]
        )

    def test_fallback_strips_sentence_punctuation_without_broad_status_matches(self) -> None:
        from workflow_core.coordinator import interpret_request

        self.assertEqual(
            "innovation", interpret_request(self.project, "帮我做创新。")["intent"]
        )
        self.assertEqual(
            "innovation", interpret_request(self.project, "请试这个创新")["intent"]
        )
        self.assertEqual(
            "paper", interpret_request(self.project, "整理创新点")["intent"]
        )
        for request in ("状态机呢", "进度条"):
            with self.subTest(request=request):
                self.assertEqual(
                    "needs_clarification",
                    interpret_request(self.project, request)["intent"],
                )

    def test_llm_has_priority_and_receives_only_minimal_context(self) -> None:
        from workflow_core.coordinator import interpret_request

        sentinel = self.project / "secret-source.py"
        sentinel.write_text("NEVER_SEND_THIS_SOURCE", encoding="utf-8")
        captured: dict[str, object] = {}

        def interpreter(request: str, context: dict[str, object]) -> dict[str, str]:
            captured["request"] = request
            captured["context"] = context
            return {"intent": "paper"}

        result = interpret_request(self.project, "帮我调参", interpreter=interpreter)

        self.assertEqual("paper", result["intent"])
        self.assertEqual("llm", result["source"])
        self.assertEqual("帮我调参", captured["request"])
        context = captured["context"]
        self.assertEqual(
            {"project_standard", "current_version", "unfinished_tasks", "routes"},
            set(context),
        )
        serialized = json.dumps(context, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("NEVER_SEND_THIS_SOURCE", serialized)
        self.assertNotIn(str(self.project), serialized)
        self.assertNotIn("secret-source.py", serialized)

    def test_llm_unavailable_falls_back_but_invalid_llm_output_is_rejected(self) -> None:
        from workflow_core.coordinator import interpret_request

        self.assertEqual(
            "tune",
            interpret_request(self.project, "调参", interpreter=lambda *_: None)[
                "intent"
            ],
        )

        def unavailable(*_args: object) -> dict[str, str]:
            raise RuntimeError("offline")

        self.assertEqual(
            "status",
            interpret_request(self.project, "查状态", interpreter=unavailable)[
                "intent"
            ],
        )
        with self.assertRaisesRegex(ValueError, "LLM"):
            interpret_request(
                self.project,
                "调参",
                interpreter=lambda *_: {"intent": "invented"},
            )
        for invalid in ([], {}):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "LLM intent"):
                    interpret_request(
                        self.project,
                        "调参",
                        interpreter=lambda *_, value=invalid: {"intent": value},
                    )

    def test_ambiguous_fallback_does_not_guess(self) -> None:
        from workflow_core.coordinator import interpret_request

        result = interpret_request(self.project, "读论文并接入代码")
        self.assertEqual("needs_clarification", result["intent"])
        self.assertTrue(result["needs_clarification"])
        self.assertIsNone(result["structured_action"])
        self.assertFalse(result["available"])

    def test_status_and_plan_are_stable_complete_and_byte_read_only(self) -> None:
        from workflow_core.runs import create_run

        task = self._new_task()
        transition_task(self.project, task["id"], "preparing")
        readiness(
            self.project,
            task["id"],
            target_clear=True,
            template_runnable=True,
            mutation_allowed=True,
            code_verified=True,
        )
        run = create_run(
            self.project,
            task["id"],
            {
                "code": {"commit": "a" * 40},
                "config": {"learning_rate": 0.001},
                "seed": 7,
                "data": {"split": "validation"},
                "environment": {"python": "3.10"},
            },
            purpose="debug",
        )
        transition_task(self.project, task["id"], "executing")
        start_run(self.project, run["id"], process_id=123)
        finish_run(
            self.project,
            run["id"],
            outcome="succeeded",
            exit_code=0,
            metrics={"score": 0.75},
            raw_log=None,
            implementation="valid",
            interface="valid",
            data="valid",
            metrics_quality="valid",
            hypothesis="not_evaluated",
            limitations=[],
            suggestions=[],
            artifacts=[],
        )
        transition_task(self.project, task["id"], "reviewing")
        record_evidence_transition(
            self.project,
            run["id"],
            "debug",
            evidence_refs=[run["id"]],
            reason="链路通过",
            proposed_by="agent:analyst",
            checked_by="",
            applied_by="agent:coordinator",
        )
        close_run(self.project, run["id"])
        stopped = self._new_task(request="暂停的调参")
        transition_task(self.project, stopped["id"], "stopped")

        before = self._snapshot(self.control)
        status_first = run_cli("workflow-status", "--project", self.project).stdout
        status_second = run_cli("workflow-status", "--project", self.project).stdout
        plan_first = run_cli("plan-next", "--project", self.project).stdout
        plan_second = run_cli("plan-next", "--project", self.project).stdout
        after = self._snapshot(self.control)

        self.assertEqual(before, after)
        self.assertEqual(status_first, status_second)
        self.assertEqual(plan_first, plan_second)
        status_payload = json.loads(status_first)
        self.assertEqual("cv-experiment-workflow.status.v2", status_payload["schema"])
        self.assertEqual(1, status_payload["task_stage_counts"]["reviewing"])
        self.assertEqual(1, status_payload["task_stage_counts"]["stopped"])
        self.assertEqual(1, status_payload["run_stage_counts"]["closed"])
        self.assertEqual(1, status_payload["run_outcome_counts"]["succeeded"])
        self.assertEqual("debug", status_payload["run_evidence"][run["id"]])
        self.assertEqual(
            sorted([task["id"], stopped["id"]]),
            status_payload["unfinished_task_ids"],
        )
        self.assertIn(stopped["id"], status_payload["continuable_task_ids"])
        self.assertEqual(
            [stopped["id"]],
            [item["task_id"] for item in status_payload["blocked"]],
        )
        plan_payload = json.loads(plan_first)
        self.assertEqual("cv-experiment-workflow.plan.v2", plan_payload["schema"])
        by_task = {
            item["task_id"]: item for item in plan_payload["suggestions"]
        }
        self.assertEqual("finish-review", by_task[task["id"]]["action"])
        self.assertEqual("continue", by_task[stopped["id"]]["action"])

    def test_status_folds_evidence_once_and_defaults_missing_runs_to_none(self) -> None:
        from workflow_core.planning import _v2_status_payload

        class CountingEvents(list):
            iterations = 0

            def __iter__(self):
                self.iterations += 1
                return super().__iter__()

        runs = {
            f"RUN-{number:04d}": {
                "execution": {"stage": "closed", "outcome": "succeeded"}
            }
            for number in range(1, 13)
        }
        events = CountingEvents(
            [
                {"subject_id": "RUN-0001", "to": "debug"},
                {"subject_id": "RUN-0002", "to": "single_run"},
            ]
        )
        payload = _v2_status_payload(
            {
                "tasks": {},
                "runs": runs,
                "events": events,
                "catalog": {
                    "sources": {},
                    "ideas": {},
                    "templates": {},
                    "modules": {},
                },
            }
        )

        self.assertEqual(1, events.iterations)
        self.assertEqual("debug", payload["run_evidence"]["RUN-0001"])
        self.assertEqual("single_run", payload["run_evidence"]["RUN-0002"])
        self.assertEqual("none", payload["run_evidence"]["RUN-0012"])
        self.assertEqual(10, payload["evidence_level_counts"]["none"])

    def test_continue_never_runs_and_requires_saved_authorization(self) -> None:
        from workflow_core.coordinator import interpret_request

        task = self._new_task(authorization=False)
        transition_task(self.project, task["id"], "preparing")
        readiness(
            self.project,
            task["id"],
            target_clear=True,
            template_runnable=True,
            mutation_allowed=True,
            code_verified=True,
        )
        before = self._snapshot(self.control)
        result = interpret_request(self.project, "继续")
        self.assertEqual(before, self._snapshot(self.control))
        self.assertTrue(result["needs_confirmation"])
        self.assertIsNone(result["structured_action"])
        self.assertEqual([], list((self.control / "runs").iterdir()))

        task_path = self.control / "tasks" / f"{task['id']}.json"
        payload = json.loads(task_path.read_text(encoding="utf-8"))
        payload["route_inputs"].update(
            {"execution_authorized": True, "plan_unchanged": True}
        )
        task_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        before = self._snapshot(self.control)
        authorized = interpret_request(self.project, "继续")
        self.assertEqual(before, self._snapshot(self.control))
        self.assertFalse(authorized["needs_confirmation"])
        self.assertEqual("run-task", authorized["structured_action"]["command"])
        self.assertTrue(authorized["structured_action"]["executable"])
        self.assertEqual([], list((self.control / "runs").iterdir()))

    def test_continue_rejects_exhausted_budget(self) -> None:
        from workflow_core.coordinator import interpret_request
        from workflow_core.planning import coordinator_context
        from workflow_core.runs import create_run

        exhausted_project = Path(self.temporary.name) / "exhausted"
        init_project(exhausted_project, "预算用完", layout="v2")
        exhausted = self._new_task(
            authorization=True,
            max_runs=1,
            project=exhausted_project,
        )
        transition_task(exhausted_project, exhausted["id"], "preparing")
        readiness(
            exhausted_project,
            exhausted["id"],
            target_clear=True,
            template_runnable=True,
            mutation_allowed=True,
            code_verified=True,
        )
        create_run(
            exhausted_project,
            exhausted["id"],
            {
                "code": {"commit": "a" * 40},
                "config": {"learning_rate": 0.001},
                "seed": 7,
                "data": {"split": "validation"},
                "environment": {"python": "3.10"},
            },
            purpose="debug",
        )
        exhausted_context = coordinator_context(exhausted_project)[
            "unfinished_tasks"
        ][0]
        self.assertTrue(exhausted_context["route_supported"])
        self.assertEqual(0, exhausted_context["remaining_runs"])
        exhausted_result = interpret_request(exhausted_project, "继续")
        self.assertTrue(exhausted_result["needs_confirmation"])
        self.assertIsNone(exhausted_result["structured_action"])
        self.assertIn("预算用完", exhausted_result["missing"][0])

    def test_cli_interpret_request_is_explicit_fallback_json(self) -> None:
        result = cli_json(
            "interpret-request",
            "--project",
            self.project,
            "--request",
            "查状态",
        )
        self.assertEqual(INTERPRETATION_FIELDS, set(result))
        self.assertEqual("status", result["intent"])
        self.assertEqual("fallback", result["source"])
        self.assertEqual("workflow-status", result["structured_action"]["command"])

    def test_bad_v2_ledger_is_rejected_without_repair_or_write(self) -> None:
        task = self._new_task()
        task_path = self.control / "tasks" / f"{task['id']}.json"
        task_path.write_text("{bad json", encoding="utf-8")
        before = self._snapshot(self.control)
        for command in ("workflow-status", "plan-next", "interpret-request"):
            args: list[object] = [command, "--project", self.project]
            if command == "interpret-request":
                args.extend(["--request", "查状态"])
            failed = run_cli(*args, check=False)
            self.assertNotEqual(0, failed.returncode)
            self.assertEqual(before, self._snapshot(self.control))


if __name__ == "__main__":
    unittest.main()
