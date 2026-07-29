from __future__ import annotations

import sys
import unittest
from copy import deepcopy

from tests._helpers import SCRIPTS


sys.path.insert(0, str(SCRIPTS))
from workflow_core import routes  # noqa: E402
from workflow_core.fair_comparison import build_fair_comparison  # noqa: E402
from workflow_core.run_identity import canonical_fingerprint  # noqa: E402
from workflow_core.tasking import validate_task  # noqa: E402


def _frozen(
    *,
    data_name: str = "demo",
    evaluation_split: str = "validation",
) -> dict[str, object]:
    evaluation = {
        "schema": "test.evaluation.v1",
        "metric": "score",
        "split": evaluation_split,
    }
    data = {
        "kind": "real_imagefolder",
        "name": data_name,
        "evaluation": evaluation,
        "dataset_identity": {
            "schema": "cv-experiment-workflow.dataset-identity.v1",
            "dataset_id": data_name,
            "version": "1",
            "source_uri": f"dataset://fair-comparison/{data_name}",
            "manifest_sha256": "sha256:" + ("2" * 64),
            "split": "validation",
        },
        "run_kind": "real_experiment",
        "paper_eligible": True,
    }
    environment = {
        "platform": {
            "system": "Windows",
            "release": "test",
            "machine": "x86_64",
        },
        "python": {
            "implementation": "CPython",
            "version": "3.test",
            "cache_tag": "cpython-test",
            "abi_flags": "none",
            "executable_sha256": "sha256:" + ("3" * 64),
        },
        "packages": {
            "torch": "missing",
            "numpy": "missing",
            "Pillow": "missing",
            "torchvision": "missing",
        },
        "requirements_locks": {},
        "declared_environment": {"backend": "test"},
    }
    pure_data = {
        key: value
        for key, value in data.items()
        if key not in {"evaluation", "run_kind", "paper_eligible"}
    }
    code = {
        "schema": "cv-experiment-workflow.bound-code.v2",
        "codebase_id": "CB-0001",
        "branch": "main",
        "commit": "1" * 40,
        "tag": None,
        "clean_required": True,
        "worktree_clean": True,
        "adapter_path": "workflow_adapter.py",
        "adapter_sha256": "4" * 64,
        "declared_code": {"revision": "test"},
        "fingerprints": {
            "config": canonical_fingerprint({"learning_rate": 0.1}),
            "data": canonical_fingerprint(pure_data),
            "evaluation": canonical_fingerprint(evaluation),
            "environment": canonical_fingerprint(environment),
        },
    }
    return {
        "code": code,
        "config": {"learning_rate": 0.1},
        "seed": 7,
        "data": data,
        "environment": environment,
    }


def _run(
    run_id: str,
    score: float,
    *,
    task_id: str,
    data_name: str = "demo",
    evaluation_split: str = "validation",
) -> dict[str, object]:
    return {
        "id": run_id,
        "task_id": task_id,
        "purpose": "evidence",
        "frozen": _frozen(
            data_name=data_name,
            evaluation_split=evaluation_split,
        ),
        "execution": {
            "stage": "closed",
            "outcome": "succeeded",
        },
        "result": {"metrics": {"score": score}, "raw_log": "metrics.json"},
        "quality": {
            "implementation": "valid",
            "interface": "valid",
            "data": "valid",
            "metrics": "valid",
        },
        "output_seal": {"seal_sha256": "sha256:" + ("5" * 64)},
    }


def _task(route: str) -> dict[str, object]:
    inputs: dict[str, object] = {
        "primary_metric": "score",
        "baseline": 0.7,
    }
    targets = ["RUN-0001"]
    if route == "ablation":
        inputs.update(
            {
                "module_ref": "MOD-0001",
                "baseline_run_ref": "RUN-0001",
                "disabled_behavior": "关闭模块",
            }
        )
        targets.append("MOD-0001")
    elif route == "reproduction":
        inputs.update(
            {
                "source_ref": "SRC-0001",
                "source_run_ref": "RUN-0001",
                "tolerance": 0.02,
            }
        )
        targets.append("SRC-0001")
    elif route == "tune":
        inputs["baseline_run_ref"] = "RUN-0001"
    elif route == "innovation":
        inputs["source_run_ref"] = "RUN-0001"
    return {
        "id": "TASK-0002",
        "route": route,
        "target_refs": targets,
        "route_inputs": inputs,
    }


def _blocker_codes(comparison: dict[str, object]) -> set[str]:
    blockers = comparison["blockers"]
    assert isinstance(blockers, list)
    return {
        str(item).split(":", 1)[0]
        for item in blockers
    }


def _verifier_for(*runs: dict[str, object]):
    seals = {
        str(run["id"]): deepcopy(run["output_seal"])
        for run in runs
    }

    def verify(run_id: str) -> object:
        return deepcopy(seals[run_id])

    return verify


class FairComparisonGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _run("RUN-0001", 0.7, task_id="TASK-0001")
        self.candidate = _run("RUN-0002", 0.8, task_id="TASK-0002")

    def test_verified_ablation_allows_formal_delta_and_module_effect(self) -> None:
        verified: list[str] = []

        def verify(run_id: str) -> dict[str, str]:
            verified.append(run_id)
            run = self.source if run_id == self.source["id"] else self.candidate
            return deepcopy(run["output_seal"])

        comparison = routes.compare(
            "ablation",
            _task("ablation"),
            self.candidate,
            source_run=self.source,
            seal_verifier=verify,
        )

        self.assertEqual(["RUN-0001", "RUN-0002"], verified)
        self.assertEqual("formal", comparison["comparison_scope"])
        self.assertEqual([], comparison["blockers"])
        self.assertEqual(0.7, comparison["enabled"])
        self.assertEqual(0.8, comparison["disabled"])
        self.assertAlmostEqual(0.1, comparison["delta"])
        self.assertAlmostEqual(-0.1, comparison["module_effect"])

    def test_each_static_gate_blocks_formal_fields_but_keeps_raw_values(self) -> None:
        cases: list[tuple[str, str, object]] = [
            (
                "candidate_not_bound",
                "candidate_run_not_bound",
                lambda source, candidate, task: candidate["frozen"]["code"].update(
                    {"schema": "unbound"}
                ),
            ),
            (
                "source_not_real",
                "source_run_not_real_experiment",
                lambda source, candidate, task: source["frozen"]["data"].update(
                    {"run_kind": "synthetic_debug_only"}
                ),
            ),
            (
                "candidate_not_paper",
                "candidate_run_not_paper_eligible",
                lambda source, candidate, task: candidate["frozen"]["data"].update(
                    {"paper_eligible": False}
                ),
            ),
            (
                "source_failed",
                "source_run_not_succeeded",
                lambda source, candidate, task: source["execution"].update(
                    {"outcome": "failed"}
                ),
            ),
            (
                "candidate_invalid",
                "candidate_run_quality_not_valid",
                lambda source, candidate, task: candidate["quality"].update(
                    {"metrics": "uncertain"}
                ),
            ),
            (
                "source_not_closed",
                "source_run_not_closed",
                lambda source, candidate, task: source["execution"].update(
                    {"stage": "finished"}
                ),
            ),
            (
                "candidate_unsealed",
                "candidate_run_output_not_sealed",
                lambda source, candidate, task: candidate.update(
                    {"output_seal": None}
                ),
            ),
            (
                "candidate_id_invalid",
                "candidate_run_id_invalid",
                lambda source, candidate, task: candidate.update(
                    {"id": "not-a-run"}
                ),
            ),
            (
                "source_ref_mismatch",
                "source_run_ref_mismatch",
                lambda source, candidate, task: task["route_inputs"].update(
                    {"baseline_run_ref": "RUN-0003"}
                ),
            ),
        ]
        for label, expected, mutate in cases:
            with self.subTest(label=label):
                source = deepcopy(self.source)
                candidate = deepcopy(self.candidate)
                task = _task("ablation")
                mutate(source, candidate, task)
                comparison = build_fair_comparison(
                    "ablation",
                    task,
                    candidate,
                    source_run=source,
                    seal_verifier=lambda _run_id: {},
                )
                self.assertIn(expected, _blocker_codes(comparison))
                self.assertEqual("side_by_side_only", comparison["comparison_scope"])
                self.assertEqual(0.7, comparison["baseline"])
                self.assertEqual(0.8, comparison["candidate"])
                self.assertIsNone(comparison["delta"])
                self.assertIsNone(comparison["module_effect"])

    def test_verified_reproduction_allows_difference_and_tolerance_result(
        self,
    ) -> None:
        candidate = _run(
            "RUN-0002",
            0.712,
            task_id="TASK-0002",
        )
        comparison = routes.compare(
            "reproduction",
            _task("reproduction"),
            candidate,
            source_run=self.source,
            seal_verifier=_verifier_for(self.source, candidate),
        )

        self.assertEqual("formal", comparison["comparison_scope"])
        self.assertAlmostEqual(0.012, comparison["delta"])
        self.assertAlmostEqual(0.012, comparison["difference"])
        self.assertIs(comparison["within_tolerance"], True)

    def test_live_reverify_failure_and_fingerprint_drift_fail_closed(self) -> None:
        def reject_source(run_id: str) -> dict[str, str]:
            if run_id == "RUN-0001":
                raise ValueError("sealed output changed")
            return deepcopy(self.candidate["output_seal"])

        failed = build_fair_comparison(
            "reproduction",
            _task("reproduction"),
            self.candidate,
            source_run=self.source,
            seal_verifier=reject_source,
        )
        self.assertIn("source_run_output_reverify_failed", _blocker_codes(failed))
        self.assertIsNone(failed["delta"])
        self.assertIsNone(failed["difference"])
        self.assertIsNone(failed["within_tolerance"])

        for label, source in (
            (
                "data",
                _run(
                    "RUN-0001",
                    0.7,
                    task_id="TASK-0001",
                    data_name="other",
                ),
            ),
            (
                "evaluation",
                _run(
                    "RUN-0001",
                    0.7,
                    task_id="TASK-0001",
                    evaluation_split="test",
                ),
            ),
        ):
            with self.subTest(fingerprint=label):
                blocked = build_fair_comparison(
                    "reproduction",
                    _task("reproduction"),
                    self.candidate,
                    source_run=source,
                    seal_verifier=lambda _run_id: {},
                )
                self.assertIn(
                    f"{label}_fingerprint_mismatch",
                    _blocker_codes(blocked),
                )
                self.assertIsNone(blocked["difference"])
                self.assertIsNone(blocked["within_tolerance"])

    def test_innovation_requires_a_legal_sealed_source_run(self) -> None:
        missing_source = _task("innovation")
        missing_source["route_inputs"].pop("source_run_ref")
        missing_source["target_refs"] = []

        blocked = build_fair_comparison(
            "innovation",
            missing_source,
            self.candidate,
            source_run=None,
            seal_verifier=lambda _run_id: {},
        )
        self.assertIn("source_run_ref_missing", _blocker_codes(blocked))
        self.assertIn("source_run_missing", _blocker_codes(blocked))
        self.assertIsNone(blocked["delta"])

        formal = build_fair_comparison(
            "innovation",
            _task("innovation"),
            self.candidate,
            source_run=self.source,
            seal_verifier=_verifier_for(self.source, self.candidate),
        )
        self.assertEqual("formal", formal["comparison_scope"])
        self.assertAlmostEqual(0.1, formal["delta"])

    def test_missing_verifier_and_legacy_routes_compare_cannot_claim_delta(
        self,
    ) -> None:
        direct = build_fair_comparison(
            "innovation",
            _task("innovation"),
            self.candidate,
            source_run=self.source,
            seal_verifier=None,
        )
        self.assertIn(
            "live_output_seal_verifier_missing",
            _blocker_codes(direct),
        )
        self.assertIsNone(direct["delta"])

        legacy = routes.compare(
            "innovation",
            _task("innovation"),
            self.candidate,
        )
        self.assertEqual("side_by_side_only", legacy["comparison_scope"])
        self.assertIsNone(legacy["delta"])
        self.assertTrue(legacy["blockers"])

    def test_verifier_must_return_the_exact_live_seal_record(self) -> None:
        comparison = build_fair_comparison(
            "innovation",
            _task("innovation"),
            self.candidate,
            source_run=self.source,
            seal_verifier=lambda _run_id: {"different": "record"},
        )

        self.assertIn(
            "source_run_output_reverify_failed",
            _blocker_codes(comparison),
        )
        self.assertIn(
            "candidate_run_output_reverify_failed",
            _blocker_codes(comparison),
        )
        self.assertIsNone(comparison["delta"])

    def test_innovation_task_can_explicitly_declare_its_source_run(self) -> None:
        task = {
            "id": "TASK-0002",
            "owner_request": "验证创新模块是否有效",
            "route": "innovation",
            "target_refs": ["IDEA-0001", "RUN-0001"],
            "route_inputs": {
                "config": {},
                "primary_metric": "score",
                "baseline": 0.7,
                "source_run_ref": "RUN-0001",
            },
            "budget": {"max_runs": 1},
            "stop_condition": {"type": "single_debug", "after_runs": 1},
            "run_refs": [],
            "stage": "queued",
            "conclusion": None,
        }

        validate_task(task, expected_id="TASK-0002")
        task["target_refs"].remove("RUN-0001")
        with self.assertRaisesRegex(ValueError, "source_run_ref"):
            validate_task(task, expected_id="TASK-0002")


if __name__ == "__main__":
    unittest.main()
