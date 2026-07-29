from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from tests._helpers import SCRIPTS


sys.path.insert(0, str(SCRIPTS))
import rw  # noqa: E402
from workflow_core import routes  # noqa: E402
from workflow_core.run_identity import CURRENT_COMPARISON_POLICY  # noqa: E402
from workflow_core.runs import validate_task_run_route_contracts_locked  # noqa: E402


def route_task(route: str, **inputs: object) -> dict[str, object]:
    route_inputs: dict[str, object] = {
        "config": {"path": "configs/debug.yaml"},
        "primary_metric": "score",
        "baseline": 0.7,
        "changes_code_behavior": False,
    }
    route_inputs.update(inputs)
    return {
        "route": route,
        "target_refs": ["target"],
        "route_inputs": route_inputs,
        "budget": {"max_runs": 1},
        "stop_condition": {"type": "single_debug", "after_runs": 1},
    }


class ExperimentRouteContractTests(unittest.TestCase):
    def test_cli_parser_accepts_exactly_the_four_v2_experiment_routes(self) -> None:
        parser = rw.build_parser()
        for route in ("tune", "ablation", "reproduction", "innovation"):
            with self.subTest(route=route):
                parsed = parser.parse_args(
                    [
                        "start-task",
                        "--project",
                        "project",
                        "--request",
                        "最小闭环",
                        "--target-ref",
                        "target",
                        "--route",
                        route,
                        "--budget",
                        '{"max_runs":1}',
                        "--stop-condition",
                        '{"type":"single_debug","after_runs":1}',
                    ]
                )
                self.assertEqual(route, parsed.route)
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "start-task",
                    "--project",
                    "project",
                    "--request",
                    "非法路线",
                    "--target-ref",
                    "target",
                    "--route",
                    "other",
                    "--budget",
                    '{"max_runs":1}',
                    "--stop-condition",
                    '{"type":"single_debug","after_runs":1}',
                ]
            )

    def test_all_four_routes_share_the_same_public_route_actions(self) -> None:
        for route in ("tune", "ablation", "reproduction", "innovation"):
            with self.subTest(route=route):
                task = route_task(route)
                if route == "ablation":
                    task["route_inputs"].update(
                        {
                            "module_ref": "MOD-0001",
                            "baseline_run_ref": "RUN-0001",
                            "disabled_behavior": "关闭后严格退化为基线",
                        }
                    )
                elif route == "reproduction":
                    task["route_inputs"].update(
                        {
                            "source_ref": "SRC-0001",
                            "source_run_ref": "RUN-0001",
                            "tolerance": 0.01,
                            "code_standard": {"cli_sha256": "a" * 64},
                            "data_standard": {"split_sha256": "b" * 64},
                        }
                    )
                self.assertEqual([], routes.requires(route, task))
                self.assertEqual(
                    "innovation" if route == "innovation" else "configuration",
                    routes.mutation_class(route, task),
                )

    def test_ablation_requires_one_module_disabled_behavior_and_enabled_run(self) -> None:
        complete = route_task(
            "ablation",
            module_ref="MOD-0001",
            baseline_run_ref="RUN-0001",
            disabled_behavior="关闭后严格退化为 global_only",
        )
        self.assertEqual([], routes.requires("ablation", complete))
        for missing in ("module_ref", "baseline_run_ref", "disabled_behavior"):
            with self.subTest(missing=missing):
                task = route_task(
                    "ablation",
                    module_ref="MOD-0001",
                    baseline_run_ref="RUN-0001",
                    disabled_behavior="关闭后严格退化为 global_only",
                )
                task["route_inputs"].pop(missing)
                self.assertTrue(routes.requires("ablation", task))

        comparison = routes.compare(
            "ablation",
            complete,
            {"result": {"metrics": {"score": 0.8}}},
        )
        self.assertEqual(0.7, comparison["enabled"])
        self.assertEqual(0.8, comparison["disabled"])
        self.assertEqual("side_by_side_only", comparison["comparison_scope"])
        self.assertIsNone(comparison["delta"])
        self.assertIsNone(comparison["module_effect"])
        self.assertTrue(comparison["blockers"])

    def test_reproduction_preserves_target_and_reports_true_tolerance_result(self) -> None:
        task = route_task(
            "reproduction",
            source_ref="SRC-0001",
            source_run_ref="RUN-0001",
            tolerance=0.01,
            code_standard={"cli_sha256": "a" * 64},
            data_standard={"split_sha256": "b" * 64},
        )
        self.assertEqual([], routes.requires("reproduction", task))

        comparison = routes.compare(
            "reproduction",
            task,
            {"result": {"metrics": {"score": 0.712}}},
        )
        self.assertEqual(0.7, comparison["target"])
        self.assertEqual(0.01, comparison["tolerance"])
        self.assertEqual("side_by_side_only", comparison["comparison_scope"])
        self.assertIsNone(comparison["delta"])
        self.assertIsNone(comparison["difference"])
        self.assertIsNone(comparison["within_tolerance"])
        self.assertTrue(comparison["blockers"])
        self.assertEqual(0.7, task["route_inputs"]["baseline"])

        for bad_tolerance in (-0.1, float("inf"), True):
            with self.subTest(tolerance=bad_tolerance):
                invalid = route_task(
                    "reproduction",
                    source_ref="SRC-0001",
                    source_run_ref="RUN-0001",
                    tolerance=bad_tolerance,
                    code_standard={"cli_sha256": "a" * 64},
                    data_standard={"split_sha256": "b" * 64},
                )
                self.assertTrue(routes.requires("reproduction", invalid))

    def test_pre_comparison_debug_only_record_may_omit_comparison_but_current_cannot(
        self,
    ) -> None:
        task = route_task("innovation")
        task.update(
            {
                "id": "TASK-0001",
                "stage": "done",
                "conclusion": {
                    "run_id": "RUN-0001",
                    "scope": "debug_only",
                },
            }
        )
        old_debug = {
            "id": "RUN-0001",
            "task_id": "TASK-0001",
            "purpose": "debug",
            "frozen": {"config": {}},
            "result": {"metrics": {"score": 0.6}, "raw_log": None},
        }
        validate_task_run_route_contracts_locked(
            Path("unused"),
            {"TASK-0001": task},
            {"RUN-0001": old_debug},
        )

        current_debug = deepcopy(old_debug)
        current_debug["frozen"]["environment"] = {
            "workflow_comparison_policy": deepcopy(CURRENT_COMPARISON_POLICY)
        }
        with self.assertRaisesRegex(ValueError, "永久比较结果无效"):
            validate_task_run_route_contracts_locked(
                Path("unused"),
                {"TASK-0001": task},
                {"RUN-0001": current_debug},
            )

        old_evidence = deepcopy(old_debug)
        old_evidence["purpose"] = "evidence"
        with self.assertRaisesRegex(ValueError, "永久比较结果无效"):
            validate_task_run_route_contracts_locked(
                Path("unused"),
                {"TASK-0001": task},
                {"RUN-0001": old_evidence},
            )

    def test_frozen_route_contract_seals_task_and_persisted_comparison(self) -> None:
        task = route_task(
            "reproduction",
            source_ref="SRC-0001",
            source_run_ref="RUN-0001",
            tolerance=0.01,
            code_standard={"tree": "a" * 40},
            data_standard={"split_sha256": "b" * 64},
        )
        task.update(
            {
                "id": "TASK-0001",
                "stage": "done",
            }
        )
        source_run = {
            "id": "RUN-0001",
            "result": {"metrics": {"score": 0.7}, "raw_log": None},
            "frozen": {"config": {}},
        }
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory)
            source_path = control / "runs" / "RUN-0001" / "run.json"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(
                json.dumps(source_run, ensure_ascii=False),
                encoding="utf-8",
            )
            source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
            contract = {
                "task_id": "TASK-0001",
                "route": "reproduction",
                "primary_metric": "score",
                "baseline": 0.7,
                "source_ref": "SRC-0001",
                "source_run_ref": "RUN-0001",
                "source_run_sha256": source_digest,
                "tolerance": 0.01,
                "code_standard": {"tree": "a" * 40},
                "data_standard": {"split_sha256": "b" * 64},
            }
            run = {
                "id": "RUN-0002",
                "task_id": "TASK-0001",
                "frozen": {
                    "config": {"route_contract": contract},
                    "environment": {
                        "workflow_comparison_policy": deepcopy(
                            CURRENT_COMPARISON_POLICY
                        )
                    },
                },
                "result": {"metrics": {"score": 0.705}, "raw_log": None},
            }
            task["conclusion"] = {
                "run_id": "RUN-0002",
                "comparison": routes.compare(
                    "reproduction",
                    task,
                    run,
                    source_run=source_run,
                    seal_verifier=lambda _run_id: {},
                ),
            }
            validate_task_run_route_contracts_locked(
                control,
                {"TASK-0001": task},
                {"RUN-0001": source_run, "RUN-0002": run},
            )

            legacy_parsed_task = deepcopy(task)
            legacy_parsed = {
                "execution": {
                    "outcome": None,
                    "exit_code": None,
                    "issue_kind": None,
                },
                "result": deepcopy(run["result"]),
                "quality": None,
                "analysis": None,
                "artifacts": None,
            }
            legacy_parsed_task["conclusion"]["comparison"] = routes.compare(
                "reproduction",
                legacy_parsed_task,
                legacy_parsed,
            )
            self.assertEqual(
                "side_by_side_only",
                legacy_parsed_task["conclusion"]["comparison"][
                    "comparison_scope"
                ],
            )
            with self.assertRaisesRegex(ValueError, "永久比较结果"):
                validate_task_run_route_contracts_locked(
                    control,
                    {"TASK-0001": legacy_parsed_task},
                    {"RUN-0001": source_run, "RUN-0002": run},
                )

            changed_target = deepcopy(task)
            changed_target["route_inputs"]["baseline"] = 0.8
            with self.assertRaisesRegex(ValueError, "冻结合同字段"):
                validate_task_run_route_contracts_locked(
                    control,
                    {"TASK-0001": changed_target},
                    {"RUN-0001": source_run, "RUN-0002": run},
                )

            changed_comparison = deepcopy(task)
            changed_comparison["conclusion"]["comparison"][
                "within_tolerance"
            ] = False
            with self.assertRaisesRegex(ValueError, "永久比较结果"):
                validate_task_run_route_contracts_locked(
                    control,
                    {"TASK-0001": changed_comparison},
                    {"RUN-0001": source_run, "RUN-0002": run},
                )

            changed_source = deepcopy(source_run)
            changed_source["result"]["metrics"]["score"] = 0.8
            source_path.write_text(
                json.dumps(changed_source, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "来源 Run.*哈希"):
                validate_task_run_route_contracts_locked(
                    control,
                    {"TASK-0001": task},
                    {"RUN-0001": changed_source, "RUN-0002": run},
                )

            changed_contract_run = deepcopy(run)
            changed_contract = changed_contract_run["frozen"]["config"][
                "route_contract"
            ]
            changed_contract["source_run_sha256"] = hashlib.sha256(
                source_path.read_bytes()
            ).hexdigest()
            with self.assertRaisesRegex(ValueError, "来源 Run.*主指标|baseline"):
                validate_task_run_route_contracts_locked(
                    control,
                    {"TASK-0001": task},
                    {
                        "RUN-0001": changed_source,
                        "RUN-0002": changed_contract_run,
                    },
                )

    def test_pre_fair_comparison_shape_remains_valid_but_cannot_be_tampered(
        self,
    ) -> None:
        task = route_task("tune")
        task.update(
            {
                "id": "TASK-0001",
                "stage": "done",
                "conclusion": {
                    "run_id": "RUN-0001",
                    "comparison": {
                        "primary_metric": "score",
                        "baseline": 0.7,
                        "candidate": 0.8,
                        "delta": 0.10000000000000009,
                    },
                },
            }
        )
        run = {
            "id": "RUN-0001",
            "task_id": "TASK-0001",
            "frozen": {"config": {}},
            "result": {"metrics": {"score": 0.8}, "raw_log": None},
        }
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory)
            validate_task_run_route_contracts_locked(
                control,
                {"TASK-0001": task},
                {"RUN-0001": run},
            )
            changed = deepcopy(task)
            changed["conclusion"]["comparison"]["delta"] = 123.0
            with self.assertRaisesRegex(ValueError, "永久比较结果"):
                validate_task_run_route_contracts_locked(
                    control,
                    {"TASK-0001": changed},
                    {"RUN-0001": run},
                )


if __name__ == "__main__":
    unittest.main()
