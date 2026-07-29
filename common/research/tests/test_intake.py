from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tests._helpers import SCRIPTS, cli_json, run_cli


sys.path.insert(0, str(SCRIPTS))
import rw  # noqa: E402
from workflow_core.intake import check_intake, evaluate_intake  # noqa: E402
from workflow_core.project import init_project  # noqa: E402
from workflow_core.tasking import create_task  # noqa: E402


OUTPUT_FIELDS = {
    "schema",
    "mode",
    "read_only",
    "intent",
    "route",
    "required",
    "provided",
    "discovered",
    "missing",
    "can_continue",
    "next_step",
}
COMMON_REQUIRED = [
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


def complete_input(route: str) -> dict[str, object]:
    details: dict[str, object]
    if route == "tune":
        details = {}
    elif route == "ablation":
        details = {
            "baseline": 0.7,
            "module_ref": "MOD-0001",
            "baseline_run_ref": "RUN-0001",
            "disabled_behavior": "关闭后恢复基础模板行为",
        }
    elif route == "reproduction":
        details = {
            "baseline": 0.7,
            "source_ref": "SRC-0001",
            "source_run_ref": "RUN-0001",
            "tolerance": 0.01,
            "code_standard": {"commit": "a" * 40},
            "data_standard": {"split": "official-test"},
        }
    elif route == "innovation":
        details = {
            "baseline": 0.7,
            "idea_refs": ["IDEA-0001"],
            "template_refs": ["TPL-0001"],
            "module_refs": ["MOD-0001"],
        }
    else:
        raise AssertionError(route)
    return {
        "domain_task": {"domain": "classification", "task": "image-classification"},
        "dataset": {"source": "datasets/cifar10", "split": "train/val/test-v1"},
        "research_goal": "验证候选方法是否提升 Top-1",
        "base_candidate": {"kind": "template", "reference": "TPL-0001"},
        "primary_metric": "top1",
        "compute_budget": {"max_runs": 3, "max_hours": 6.0, "max_gpus": 1},
        "stop_condition": {"type": "max_runs", "value": 3},
        "route_details": details,
    }


class EvaluateIntakeTests(unittest.TestCase):
    def test_status_is_a_read_only_request_that_can_open_the_status_page(self) -> None:
        result = evaluate_intake(intent="status", provided={}, discovered={})

        self.assertEqual(OUTPUT_FIELDS, set(result))
        self.assertEqual("cv-experiment-workflow.intake-result.v1", result["schema"])
        self.assertEqual("preview", result["mode"])
        self.assertIs(result["read_only"], True)
        self.assertEqual("status", result["intent"])
        self.assertIsNone(result["route"])
        self.assertEqual([], result["required"])
        self.assertEqual([], result["missing"])
        self.assertIs(result["can_continue"], True)
        self.assertEqual("show_status", result["next_step"])
        self.assertNotIn("execution_enabled", result)

    def test_incomplete_experiment_reports_stable_leaf_paths(self) -> None:
        provided = {
            "domain_task": {"domain": "classification"},
            "dataset": {"source": "datasets/cifar10", "split": ""},
            "research_goal": " ",
            "base_candidate": {"kind": "other", "reference": ""},
            "primary_metric": "",
            "compute_budget": {
                "max_runs": True,
                "max_hours": math.inf,
                "max_gpus": -1,
            },
            "stop_condition": {},
        }
        result = evaluate_intake(
            intent="tune",
            provided=provided,
            discovered={"preview_scope": {"task_creation": False}},
        )

        self.assertEqual(COMMON_REQUIRED, result["required"])
        self.assertEqual(
            [
                "domain_task.task",
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
            ],
            result["missing"],
        )
        self.assertIs(result["can_continue"], False)
        self.assertEqual("provide_missing_inputs", result["next_step"])
        self.assertIs(result["read_only"], True)

    def test_all_four_routes_accept_complete_inputs_without_mutating_them(self) -> None:
        extras = {
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
        for route in ("tune", "ablation", "reproduction", "innovation"):
            with self.subTest(route=route):
                provided = complete_input(route)
                discovered = {"marker": [route]}
                original_provided = deepcopy(provided)
                original_discovered = deepcopy(discovered)

                result = evaluate_intake(
                    intent=route,
                    provided=provided,
                    discovered=discovered,
                )

                self.assertEqual(COMMON_REQUIRED + extras[route], result["required"])
                self.assertEqual([], result["missing"])
                self.assertIs(result["can_continue"], True)
                self.assertEqual(
                    "review_read_only_confirmation",
                    result["next_step"],
                )
                self.assertEqual(route, result["route"])
                self.assertEqual(original_provided, provided)
                self.assertEqual(original_discovered, discovered)
                self.assertIsNot(result["provided"], provided)
                self.assertIsNot(result["discovered"], discovered)

    def test_tune_accepts_known_candidate_details_without_dropping_them(self) -> None:
        provided = complete_input("tune")
        provided["route_details"] = {
            "config": {"learning_rate": [0.001, 0.0005]},
            "allowed_changes": ["learning_rate"],
            "baseline": 0.7,
            "baseline_run_ref": "RUN-0001",
        }

        result = evaluate_intake(
            intent="tune",
            provided=provided,
            discovered={},
        )

        self.assertEqual([], result["missing"])
        self.assertIs(result["can_continue"], True)
        self.assertEqual(
            provided["route_details"],
            result["provided"]["route_details"],
        )

    def test_tune_allowed_changes_accepts_any_finite_json_value(self) -> None:
        for value in ("learning_rate", 0, [], {}):
            with self.subTest(value=value):
                provided = complete_input("tune")
                provided["route_details"] = {"allowed_changes": value}

                result = evaluate_intake(
                    intent="tune",
                    provided=provided,
                    discovered={},
                )

                self.assertEqual([], result["missing"])
                self.assertIs(result["can_continue"], True)

    def test_tune_invalid_optional_details_return_all_missing_paths(self) -> None:
        provided = complete_input("tune")
        provided["route_details"] = {
            "config": [],
            "allowed_changes": math.nan,
            "baseline": True,
            "baseline_run_ref": "RUN-x",
        }

        result = evaluate_intake(
            intent="tune",
            provided=provided,
            discovered={},
        )

        self.assertEqual(
            [
                "route_details.config",
                "route_details.allowed_changes",
                "route_details.baseline",
                "route_details.baseline_run_ref",
            ],
            result["missing"],
        )
        self.assertIs(result["can_continue"], False)
        self.assertEqual("provide_missing_inputs", result["next_step"])

    def test_tune_infinite_allowed_changes_is_reported_missing(self) -> None:
        provided = complete_input("tune")
        provided["route_details"] = {"allowed_changes": math.inf}

        result = evaluate_intake(
            intent="tune",
            provided=provided,
            discovered={},
        )

        self.assertEqual(["route_details.allowed_changes"], result["missing"])
        self.assertIs(result["can_continue"], False)

    def test_route_specific_invalid_values_are_reported_as_missing_fields(self) -> None:
        cases = {
            "ablation": {
                "baseline": True,
                "module_ref": "SRC-0001",
                "baseline_run_ref": "RUN-x",
                "disabled_behavior": "",
            },
            "reproduction": {
                "baseline": math.nan,
                "source_ref": "IDEA-0001",
                "source_run_ref": "RUN-x",
                "tolerance": -0.01,
                "code_standard": [],
                "data_standard": None,
            },
            "innovation": {
                "baseline": math.inf,
                "idea_refs": [],
                "template_refs": ["TPL-x"],
                "module_refs": ["MOD-0001", "MOD-0001"],
            },
        }
        for route, bad_details in cases.items():
            with self.subTest(route=route):
                provided = complete_input(route)
                provided["route_details"] = bad_details
                result = evaluate_intake(
                    intent=route,
                    provided=provided,
                    discovered={},
                )
                expected = [
                    item
                    for item in result["required"]
                    if item.startswith("route_details.")
                ]
                self.assertEqual(expected, result["missing"])
                self.assertIs(result["can_continue"], False)

    def test_unknown_intent_and_unknown_provided_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "intent"):
            evaluate_intake(intent="train", provided={}, discovered={})
        with self.assertRaisesRegex(ValueError, "未知字段"):
            evaluate_intake(
                intent="status",
                provided={"execution_enabled": True},
                discovered={},
            )

    def test_innovation_object_in_reference_list_is_reported_not_crashed(self) -> None:
        provided = complete_input("innovation")
        provided["route_details"]["module_refs"] = [{"id": "MOD-0001"}]

        result = evaluate_intake(
            intent="innovation",
            provided=provided,
            discovered={},
        )

        self.assertEqual(["route_details.module_refs"], result["missing"])
        self.assertIs(result["can_continue"], False)

    def test_innovation_can_optionally_declare_a_source_run_for_comparison(
        self,
    ) -> None:
        provided = complete_input("innovation")
        provided["route_details"]["source_run_ref"] = "RUN-0001"

        result = evaluate_intake(
            intent="innovation",
            provided=provided,
            discovered={},
        )

        self.assertEqual([], result["missing"])
        self.assertIs(result["can_continue"], True)

    def test_continue_selects_zero_one_or_one_of_many_unfinished_tasks(self) -> None:
        no_task = evaluate_intake(
            intent="continue",
            provided={},
            discovered={"unfinished_tasks": []},
        )
        self.assertEqual(["unfinished_task"], no_task["required"])
        self.assertEqual(["unfinished_task"], no_task["missing"])
        self.assertIs(no_task["can_continue"], False)
        self.assertEqual("choose_experiment_route", no_task["next_step"])

        one_task = evaluate_intake(
            intent="continue",
            provided={},
            discovered={
                "unfinished_tasks": [
                    {"id": "TASK-0001", "route": "tune", "stage": "queued"}
                ]
            },
        )
        self.assertEqual(["unfinished_task"], one_task["required"])
        self.assertEqual([], one_task["missing"])
        self.assertIs(one_task["can_continue"], True)
        self.assertEqual(
            "review_read_only_confirmation",
            one_task["next_step"],
        )

        many = {
            "unfinished_tasks": [
                {"id": "TASK-0001", "route": "tune", "stage": "queued"},
                {"id": "TASK-0002", "route": "innovation", "stage": "ready"},
            ]
        }
        ambiguous = evaluate_intake(
            intent="continue",
            provided={},
            discovered=many,
        )
        self.assertEqual(["unfinished_task", "task_ref"], ambiguous["required"])
        self.assertEqual(["task_ref"], ambiguous["missing"])
        self.assertEqual("choose_task", ambiguous["next_step"])

        selected = evaluate_intake(
            intent="continue",
            provided={"task_ref": "TASK-0002"},
            discovered=many,
        )
        self.assertEqual([], selected["missing"])
        self.assertIs(selected["can_continue"], True)
        self.assertEqual(
            "review_read_only_confirmation",
            selected["next_step"],
        )

        unknown = evaluate_intake(
            intent="continue",
            provided={"task_ref": "TASK-9999"},
            discovered=many,
        )
        self.assertEqual(["task_ref"], unknown["missing"])
        self.assertIs(unknown["can_continue"], False)


class CheckIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        init_project(self.project, "启动检查", layout="v2")

    @staticmethod
    def _snapshot(
        root: Path,
    ) -> dict[str, tuple[str, str | None, int, int]]:
        snapshot: dict[str, tuple[str, str | None, int, int]] = {}
        paths = [root, *root.rglob("*")]
        for path in sorted(paths, key=lambda item: item.as_posix()):
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if path.is_symlink():
                snapshot[relative] = (
                    "link",
                    str(path.readlink()),
                    info.st_size,
                    info.st_mtime_ns,
                )
            elif path.is_dir():
                snapshot[relative] = (
                    "directory",
                    None,
                    info.st_size,
                    info.st_mtime_ns,
                )
            elif path.is_file():
                snapshot[relative] = (
                    "file",
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    info.st_size,
                    info.st_mtime_ns,
                )
        return snapshot

    def test_check_intake_discovers_tasks_and_never_writes(self) -> None:
        task = create_task(
            self.project,
            owner_request="调一个参数",
            route="tune",
            target_refs=["baseline"],
            route_inputs={"config": {}, "debug_required": False},
            budget={"max_runs": 1},
            stop_condition={"type": "single_debug"},
        )
        before = self._snapshot(self.project)

        result = check_intake(self.project, intent="continue")

        self.assertEqual(before, self._snapshot(self.project))
        self.assertIs(result["can_continue"], True)
        unfinished = result["discovered"]["unfinished_tasks"]
        self.assertEqual([task["id"]], [item["id"] for item in unfinished])
        self.assertEqual(
            {
                "persistence": False,
                "domain_pack": False,
                "codebase_registry": False,
                "base_verification": False,
                "task_creation": False,
            },
            result["discovered"]["preview_scope"],
        )

    def test_check_intake_uses_one_snapshot_lock_and_no_write_path(self) -> None:
        from workflow_core import locking, planning

        before = self._snapshot(self.project)
        self.assertIn(".", before)
        real_open = locking.os.open
        observed_flags: list[int] = []

        def read_only_open(path, flags, *args, **kwargs):
            observed_flags.append(flags)
            self.assertFalse(flags & os.O_CREAT)
            self.assertFalse(flags & os.O_WRONLY)
            self.assertFalse(flags & os.O_RDWR)
            return real_open(path, flags, *args, **kwargs)

        with (
            mock.patch.object(
                planning,
                "project_snapshot_lock",
                wraps=planning.project_snapshot_lock,
            ) as snapshot_lock,
            mock.patch.object(
                locking,
                "project_write_lock",
                side_effect=AssertionError("intake 不得使用项目写锁"),
            ),
            mock.patch.object(locking.os, "open", side_effect=read_only_open),
            mock.patch.object(
                locking,
                "_open_lock_file",
                side_effect=AssertionError("intake 不得创建锁文件"),
            ),
            mock.patch.object(
                locking,
                "_cleanup_lock_bootstrap_files",
                side_effect=AssertionError("intake 不得清理或改写锁文件"),
            ),
        ):
            result = check_intake(self.project, intent="status")

        snapshot_lock.assert_called_once()
        self.assertTrue(observed_flags)
        self.assertIs(result["read_only"], True)
        self.assertEqual(before, self._snapshot(self.project))

    def test_cli_matches_core_and_is_read_only(self) -> None:
        provided = complete_input("tune")
        before = self._snapshot(self.project)

        cli_result = cli_json(
            "intake-check",
            "--project",
            self.project,
            "--intent",
            "tune",
            "--provided",
            json.dumps(provided, ensure_ascii=False),
        )
        core_result = check_intake(
            self.project,
            intent="tune",
            provided=provided,
        )

        self.assertEqual(core_result, cli_result)
        self.assertEqual(before, self._snapshot(self.project))

    def test_cli_omitted_provided_uses_none_parser_default(self) -> None:
        parsed = rw.build_parser().parse_args(
            [
                "intake-check",
                "--project",
                str(self.project),
                "--intent",
                "status",
            ]
        )
        self.assertIsNone(parsed.provided)

        result = cli_json(
            "intake-check",
            "--project",
            self.project,
            "--intent",
            "status",
        )
        self.assertIs(result["can_continue"], True)
        self.assertEqual({}, result["provided"])

    def test_cli_rejects_non_object_nonfinite_and_duplicate_json(self) -> None:
        invalid_values = (
            "[]",
            '{"compute_budget":{"max_hours":NaN}}',
            '{"domain_task":{"domain":"a","domain":"b"}}',
        )
        for value in invalid_values:
            with self.subTest(value=value):
                result = run_cli(
                    "intake-check",
                    "--project",
                    self.project,
                    "--intent",
                    "tune",
                    "--provided",
                    value,
                    check=False,
                )
                self.assertEqual(2, result.returncode)


if __name__ == "__main__":
    unittest.main()
