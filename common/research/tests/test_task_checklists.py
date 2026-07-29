from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from tests._helpers import SCRIPTS, cli_json, run_cli


sys.path.insert(0, str(SCRIPTS))
import workflow_core.checklists as checklist_module  # noqa: E402
import workflow_core.evidence as evidence_module  # noqa: E402
from workflow_core.checklists import derive_task_list  # noqa: E402
from workflow_core.project import init_project  # noqa: E402
from workflow_core.tasking import create_task  # noqa: E402


CHECKLIST_IDS = ["objective", "readiness", "debug", "evidence", "review", "close"]
CHECKLIST_STATUSES = {"pending", "current", "done", "blocked"}


def synthetic_task(
    task_id: str,
    *,
    route: str,
    stage: str,
    debug_required: bool = True,
    max_runs: int | None = None,
    run_refs: list[str] | None = None,
) -> dict[str, object]:
    task: dict[str, object] = {
        "id": task_id,
        "owner_request": f"处理 {route}",
        "route": route,
        "route_inputs": {"debug_required": debug_required},
        "stage": stage,
    }
    if max_runs is not None:
        task["budget"] = {"max_runs": max_runs}
        task["run_refs"] = list(run_refs or [])
    return task


def synthetic_run(
    run_id: str,
    task_id: str,
    *,
    purpose: str,
    stage: str = "closed",
    outcome: str = "succeeded",
) -> dict[str, object]:
    return {
        "id": run_id,
        "task_id": task_id,
        "purpose": purpose,
        "execution": {"stage": stage, "outcome": outcome},
    }


def event(run_id: str, level: str) -> dict[str, str]:
    return {"subject_id": run_id, "to": level}


class DerivedTaskChecklistTests(unittest.TestCase):
    def test_exhausted_single_debug_task_does_not_offer_impossible_evidence_run(
        self,
    ) -> None:
        task = synthetic_task(
            "TASK-0001",
            route="innovation",
            stage="preparing",
            max_runs=1,
            run_refs=["RUN-0001"],
        )
        run = synthetic_run(
            "RUN-0001", "TASK-0001", purpose="debug"
        )
        tasks = {"TASK-0001": task}
        runs = {"RUN-0001": run}
        events = [event("RUN-0001", "debug")]

        item = derive_task_list(tasks, runs, events)["tasks"][0]

        self.assertEqual("done", item["checklist"][2]["status"])
        self.assertEqual("blocked", item["checklist"][3]["status"])
        self.assertEqual(
            "本任务运行预算已用完；如需正式证据，请新建 Task",
            item["next_step"],
        )

        from workflow_core.planning import (
            _derive_v2_suggestions,
            _v2_status_payload,
        )

        snapshot = {
            "tasks": tasks,
            "runs": runs,
            "events": events,
            "catalog": {
                "sources": {},
                "ideas": {},
                "templates": {},
                "modules": {},
            },
        }
        status = _v2_status_payload(snapshot)
        self.assertEqual([], status["continuable_task_ids"])
        self.assertEqual(
            "run_budget_exhausted", status["blocked"][0]["reason"]
        )
        suggestion = _derive_v2_suggestions(tasks)[0]
        self.assertEqual("new-task", suggestion["action"])
        self.assertEqual("run_budget_exhausted", suggestion["reason"])
        self.assertIsNone(suggestion["next_stage"])

    def test_completed_single_debug_scope_has_an_honest_six_step_summary(
        self,
    ) -> None:
        task = synthetic_task(
            "TASK-0001",
            route="innovation",
            stage="done",
            max_runs=1,
            run_refs=["RUN-0001"],
        )
        task["stop_condition"] = {
            "type": "single_debug",
            "after_runs": 1,
        }
        task["conclusion"] = {
            "scope": "debug_only",
            "reason": "stop_condition_met",
            "run_id": "RUN-0001",
            "evidence_level": "debug",
            "hypothesis": "inconclusive",
        }
        item = derive_task_list(
            {"TASK-0001": task},
            {
                "RUN-0001": synthetic_run(
                    "RUN-0001", "TASK-0001", purpose="debug"
                )
            },
            [event("RUN-0001", "debug")],
        )["tasks"][0]

        self.assertEqual(
            "按 single_debug 停止条件结束；正式证据需要新建 Task",
            item["checklist"][3]["label"],
        )
        self.assertTrue(
            all(step["status"] == "done" for step in item["checklist"])
        )
        self.assertEqual("已完成本 Task 的 debug 范围", item["next_step"])

    def test_done_and_reviewing_without_runs_block_the_first_missing_fact(self) -> None:
        tasks = {
            "TASK-0001": synthetic_task(
                "TASK-0001", route="tune", stage="done"
            ),
            "TASK-0002": synthetic_task(
                "TASK-0002",
                route="innovation",
                stage="reviewing",
                debug_required=False,
            ),
        }

        by_id = {
            item["task_id"]: item
            for item in derive_task_list(tasks, {}, [])["tasks"]
        }

        done = by_id["TASK-0001"]
        self.assertEqual(
            ["done", "done", "blocked", "pending", "done", "done"],
            [step["status"] for step in done["checklist"]],
        )
        self.assertEqual("done", done["checklist"][5]["status"])
        self.assertEqual(
            "任务已标记完成，但记录不完整：完成最小调试运行",
            done["next_step"],
        )

        reviewing = by_id["TASK-0002"]
        self.assertEqual(
            ["done", "done", "done", "blocked", "current", "pending"],
            [step["status"] for step in reviewing["checklist"]],
        )
        self.assertEqual(
            reviewing["checklist"][3]["label"], reviewing["next_step"]
        )

    def test_runs_are_grouped_once_instead_of_rescanned_for_every_task(self) -> None:
        class CountingRuns(dict[str, dict[str, object]]):
            values_calls = 0

            def values(self):
                self.values_calls += 1
                return super().values()

        tasks = {
            f"TASK-{number:04d}": synthetic_task(
                f"TASK-{number:04d}", route="tune", stage="queued"
            )
            for number in range(1, 9)
        }
        runs = CountingRuns()

        payload = derive_task_list(tasks, runs, [])

        self.assertEqual(8, len(payload["tasks"]))
        self.assertEqual(1, runs.values_calls)

    def test_evidence_level_folding_is_public_and_shared(self) -> None:
        self.assertTrue(
            hasattr(evidence_module, "levels_from_events"),
            "Evidence 模块应提供公共 levels_from_events",
        )
        public_helper = getattr(evidence_module, "levels_from_events")
        self.assertIs(public_helper, evidence_module._levels_from_events)
        self.assertTrue(hasattr(checklist_module, "levels_from_events"))
        self.assertIs(public_helper, checklist_module.levels_from_events)
        self.assertEqual(
            {"RUN-0001": "confirmed"},
            public_helper(
                [
                    event("RUN-0001", "single_run"),
                    event("RUN-0001", "confirmed"),
                ]
            ),
        )

    def test_routes_have_six_readable_steps_and_fact_derived_progress(self) -> None:
        tasks = {
            "TASK-0002": synthetic_task(
                "TASK-0002",
                route="innovation",
                stage="reviewing",
                debug_required=False,
            ),
            "TASK-0001": synthetic_task(
                "TASK-0001", route="tune", stage="stopped"
            ),
        }
        runs = {
            "RUN-0001": synthetic_run(
                "RUN-0001", "TASK-0001", purpose="debug"
            ),
            "RUN-0002": synthetic_run(
                "RUN-0002", "TASK-0001", purpose="evidence"
            ),
            "RUN-0003": synthetic_run(
                "RUN-0003", "TASK-0002", purpose="evidence"
            ),
        }
        events = [
            event("RUN-0001", "debug"),
            event("RUN-0002", "single_run"),
            event("RUN-0003", "confirmed"),
        ]
        original = copy.deepcopy((tasks, runs, events))

        payload = derive_task_list(tasks, runs, events)

        self.assertEqual(original, (tasks, runs, events))
        self.assertEqual(
            {"total": 2, "unfinished": 2, "done": 0, "stopped": 1},
            payload["summary"],
        )
        self.assertEqual(
            ["TASK-0001", "TASK-0002"],
            [item["task_id"] for item in payload["tasks"]],
        )
        by_id = {item["task_id"]: item for item in payload["tasks"]}
        expected_route_words = {
            "TASK-0001": "调参",
            "TASK-0002": "创新",
        }
        for task_id, item in by_id.items():
            checklist = item["checklist"]
            self.assertEqual(CHECKLIST_IDS, [step["id"] for step in checklist])
            self.assertEqual(6, len(checklist))
            self.assertTrue(
                all(step["status"] in CHECKLIST_STATUSES for step in checklist)
            )
            self.assertIn(
                expected_route_words[task_id],
                "".join(step["label"] for step in checklist),
            )
            self.assertEqual(
                {"done": sum(step["status"] == "done" for step in checklist), "total": 6},
                item["progress"],
            )

        self.assertEqual(
            ["done", "done", "done", "done", "blocked", "pending"],
            [step["status"] for step in by_id["TASK-0001"]["checklist"]],
        )
        self.assertEqual(
            by_id["TASK-0001"]["checklist"][4]["label"],
            by_id["TASK-0001"]["next_step"],
        )
        self.assertEqual(
            ["done", "done", "done", "done", "current", "pending"],
            [step["status"] for step in by_id["TASK-0002"]["checklist"]],
        )

    def test_run_steps_require_success_closed_and_current_evidence_level(self) -> None:
        tasks = {
            "TASK-0001": synthetic_task(
                "TASK-0001", route="tune", stage="ready"
            )
        }
        runs = {
            "RUN-0001": synthetic_run(
                "RUN-0001",
                "TASK-0001",
                purpose="debug",
                outcome="failed",
            ),
            "RUN-0002": synthetic_run(
                "RUN-0002",
                "TASK-0001",
                purpose="debug",
                stage="finished",
            ),
            "RUN-0003": synthetic_run(
                "RUN-0003", "TASK-0001", purpose="evidence"
            ),
            "RUN-0004": synthetic_run(
                "RUN-0004",
                "TASK-0001",
                purpose="evidence",
                outcome="failed",
            ),
        }
        events = [
            event("RUN-0001", "debug"),
            event("RUN-0002", "debug"),
            event("RUN-0003", "revoked"),
            event("RUN-0004", "single_run"),
        ]

        derived = derive_task_list(tasks, runs, events)["tasks"]
        self.assertEqual(1, len(derived), "非空 Task 必须派生一个清单项")
        checklist = derived[0]["checklist"]
        self.assertEqual("current", checklist[2]["status"])
        self.assertEqual("pending", checklist[3]["status"])

        runs["RUN-0005"] = synthetic_run(
            "RUN-0005", "TASK-0001", purpose="debug"
        )
        runs["RUN-0006"] = synthetic_run(
            "RUN-0006", "TASK-0001", purpose="evidence"
        )
        events.extend(
            [event("RUN-0005", "debug"), event("RUN-0006", "confirmed")]
        )
        derived = derive_task_list(tasks, runs, events)["tasks"]
        self.assertEqual(1, len(derived), "非空 Task 必须派生一个清单项")
        checklist = derived[0]["checklist"]
        self.assertEqual("done", checklist[2]["status"])
        self.assertEqual("done", checklist[3]["status"])
        self.assertEqual("current", checklist[4]["status"])

    def test_debug_is_done_without_a_run_only_when_explicitly_disabled(self) -> None:
        tasks = {
            "TASK-0001": synthetic_task(
                "TASK-0001",
                route="tune",
                stage="ready",
                debug_required=False,
            ),
            "TASK-0002": synthetic_task(
                "TASK-0002", route="tune", stage="ready"
            ),
        }

        by_id = {
            item["task_id"]: item
            for item in derive_task_list(tasks, {}, [])["tasks"]
        }
        self.assertEqual(set(tasks), set(by_id), "每个 Task 都必须有派生清单")
        self.assertEqual("done", by_id["TASK-0001"]["checklist"][2]["status"])
        self.assertEqual(
            "current", by_id["TASK-0001"]["checklist"][3]["status"]
        )
        self.assertEqual(
            "current", by_id["TASK-0002"]["checklist"][2]["status"]
        )


class TaskChecklistCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        cli_json(
            "init",
            "--path",
            self.project,
            "--name",
            "清单测试",
            "--layout",
            "v2",
        )

    def _new_task(
        self, *, route: str = "tune", request: str = "跑一个候选"
    ) -> dict[str, object]:
        return create_task(
            self.project,
            owner_request=request,
            route=route,
            target_refs=["baseline"],
            route_inputs={"debug_required": False},
            budget={"max_runs": 1},
            stop_condition={"max_failures": 1},
        )

    @staticmethod
    def _byte_digest(root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(relative)
            digest.update(b"\0")
            if path.is_file():
                digest.update(path.read_bytes())
            else:
                digest.update(b"<directory>")
            digest.update(b"\0")
        return digest.hexdigest()

    def test_empty_v2_project_has_one_stable_task_list_payload(self) -> None:
        payload = cli_json("task-list", "--project", self.project)

        self.assertEqual("cv-experiment-workflow.task-list.v1", payload["schema"])
        self.assertEqual(
            {"total": 0, "unfinished": 0, "done": 0, "stopped": 0},
            payload["summary"],
        )
        self.assertEqual([], payload["tasks"])

    def test_optional_task_filter_and_errors_are_explicit(self) -> None:
        first = self._new_task(route="tune", request="第一个")
        second = self._new_task(route="tune", request="第二个")

        filtered = cli_json(
            "task-list", "--project", self.project, "--task", second["id"]
        )
        self.assertEqual(
            {"total": 1, "unfinished": 1, "done": 0, "stopped": 0},
            filtered["summary"],
        )
        self.assertEqual([second["id"]], [item["task_id"] for item in filtered["tasks"]])
        self.assertEqual("第二个", filtered["tasks"][0]["owner_request"])
        self.assertNotIn("checklist", json.dumps(first, ensure_ascii=False))

        invalid = run_cli(
            "task-list",
            "--project",
            self.project,
            "--task",
            "TASK-bad",
            check=False,
        )
        self.assertNotEqual(0, invalid.returncode)
        self.assertIn("Task ID 无效", invalid.stderr)
        unknown = run_cli(
            "task-list",
            "--project",
            self.project,
            "--task",
            "TASK-9999",
            check=False,
        )
        self.assertNotEqual(0, unknown.returncode)
        self.assertIn("Task 不存在", unknown.stderr)

    def test_v1_is_rejected_explicitly(self) -> None:
        v1_project = Path(self.temporary.name) / "v1"
        init_project(v1_project, "旧项目", layout="v1")

        failed = run_cli("task-list", "--project", v1_project, check=False)

        self.assertNotEqual(0, failed.returncode)
        self.assertIn("只支持 v2", failed.stderr)

    def test_task_list_and_workflow_status_are_stable_and_byte_read_only(self) -> None:
        self._new_task()
        control = self.project / ".experiment-workflow"
        before = self._byte_digest(control)

        task_list_first = run_cli(
            "task-list", "--project", self.project
        ).stdout
        task_list_second = run_cli(
            "task-list", "--project", self.project
        ).stdout
        status_first = run_cli(
            "workflow-status", "--project", self.project
        ).stdout
        status_second = run_cli(
            "workflow-status", "--project", self.project
        ).stdout

        self.assertEqual(before, self._byte_digest(control))
        self.assertEqual(task_list_first, task_list_second)
        self.assertEqual(status_first, status_second)
        self.assertIn("task_list", json.loads(status_first))
        self.assertEqual(
            json.loads(task_list_first), json.loads(status_first)["task_list"]
        )


if __name__ == "__main__":
    unittest.main()
