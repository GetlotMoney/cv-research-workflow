from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tests._helpers import SCRIPTS, cli_json


sys.path.insert(0, str(SCRIPTS))

from workflow_core.codebases import CODEBASE_SCHEMA, register_codebase  # noqa: E402
from workflow_core.engine import execute_task  # noqa: E402
from workflow_core import output_seal as output_seal_module  # noqa: E402
from workflow_core.attempts import validate_project_workflow  # noqa: E402
from workflow_core.output_seal import (  # noqa: E402
    OUTPUT_SEAL_SCHEMA,
    seal_run_outputs,
    verify_run_output_seal,
)
from workflow_core.project import init_project  # noqa: E402
from workflow_core.runs import RUN_FIELDS, create_run, load_run  # noqa: E402
from workflow_core.tasking import (  # noqa: E402
    create_task,
    load_task,
    readiness,
    transition_task,
)


def _fake_central_cuda_attestation(
    *,
    gate: dict[str, object],
    adapter: dict[str, str],
) -> dict[str, object]:
    """Keep output-seal tests GPU-independent without weakening production."""
    from workflow_core import run_identity

    environment = run_identity.live_environment_snapshot()
    return {
        "schema": run_identity.CUDA_ATTESTATION_SCHEMA,
        "verified_by": run_identity.CUDA_ATTESTATION_VERIFIER,
        "template_id": "PACK-CLS",
        "template_version": "1.0.0",
        "adapter_sha256": adapter["sha256"],
        "python_executable_sha256": environment["python"][
            "executable_sha256"
        ],
        "torch_version": "test-cuda",
        "cuda_runtime": "test-cuda",
        "device_index": 0,
        "device_count": 1,
        "device_name": "Test CUDA Device",
        "compute_capability": [9, 0],
        "probe": {
            "operation": "2x2_ones_matmul_sum",
            "result": 8.0,
            "tensor_device": "cuda:0",
        },
    }


class OutputSealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "ledger"
        init_project(self.project, "output-seal", layout="v2")
        self.control = self.project / ".experiment-workflow"

    def _git(self, repository: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=15,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def _execute_evidence(
        self,
        task_id: str,
    ) -> dict[str, object]:
        # These tests exercise output sealing, not CUDA discovery. The frozen
        # record remains structurally valid, while a separate test below
        # proves that the custom Adapter is rejected without this test-only
        # attestation.
        with mock.patch(
            "workflow_core.run_identity.create_central_cuda_attestation",
            side_effect=_fake_central_cuda_attestation,
        ):
            return execute_task(
                self.project,
                task_id,
                purpose="evidence",
            )

    def _bound_repo(self) -> tuple[Path, str]:
        repository = self.root / "codebase"
        repository.mkdir()
        self._git(repository, "init", "-b", "main")
        self._git(repository, "config", "user.name", "Test User")
        self._git(repository, "config", "user.email", "test@example.com")
        (repository / "MARKER").write_text(
            "bound\n",
            encoding="utf-8",
            newline="\n",
        )
        (repository / "workflow_adapter.py").write_text(
            """\
def inspect(project):
    return {"standard": {"debug_required": False}}


def validate(project):
    return []


def prepare_runs(project, task):
    return [{
        "code": {"revision": "declared"},
        "config": {
            **dict(task["route_inputs"]["config"]),
            "device": "cuda",
        },
        "seed": task["route_inputs"]["seed"],
        "data": {
            "kind": "real_imagefolder",
            "evaluation": {
                "schema": "test.evaluation.v1",
                "metric": "score",
                "split": "validation",
            },
            "dataset_identity": {
                "schema": "cv-experiment-workflow.dataset-identity.v1",
                "dataset_id": "seal-demo",
                "version": "1",
                "source_uri": "dataset://seal/demo",
                "manifest_sha256": "sha256:" + ("2" * 64),
                "split": "validation",
            },
        },
        "environment": {"backend": "test", "device": "cuda"},
    }]


def execute(project, action, run):
    if action == "start":
        output = project / ".cv-workflow-output"
        output.mkdir(exist_ok=True)
        (output / "run.log").write_bytes(b"finished\\n")
        (output / "metrics.json").write_bytes(b'{"score":0.8}\\n')
        return {"status": "finished", "process_id": None}
    if action == "status":
        return {"status": "finished"}
    if action == "stop":
        return {"status": "stopped"}
    raise ValueError(action)


def parse_result(project, run):
    return {
        "execution": {
            "outcome": "succeeded",
            "exit_code": 0,
            "issue_kind": None,
        },
        "result": {
            "metrics": {"score": 0.8},
            "raw_log": ".cv-workflow-output/run.log",
        },
        "quality": {
            "implementation": "valid",
            "interface": "valid",
            "data": "valid",
            "metrics": "valid",
        },
        "analysis": {
            "hypothesis": "supported",
            "limitations": [],
            "suggestions": [],
        },
        "artifacts": [".cv-workflow-output/metrics.json"],
    }
""",
            encoding="utf-8",
            newline="\n",
        )
        self._git(repository, "add", ".")
        self._git(repository, "commit", "-m", "bound fixture")
        commit = self._git(repository, "rev-parse", "HEAD")
        manifest = {
            "schema": CODEBASE_SCHEMA,
            "name": "seal-codebase",
            "primary_direction": "cls",
            "repo_path": str(repository.resolve()),
            "source": "domain_pack",
            "template_id": "PACK-CLS",
            "template_version": "1.0.0",
            "default_branch": "main",
            "initial_commit": commit,
            "initial_tag": None,
        }
        manifest_path = self.root / "codebase.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        register_codebase(self.project, manifest_path, repository)
        return repository, commit

    def _bound_task(self) -> tuple[dict[str, object], Path]:
        repository, commit = self._bound_repo()
        task = create_task(
            self.project,
            owner_request="seal one real run",
            route="tune",
            target_refs=["baseline"],
            route_inputs={
                "config": {"learning_rate": 0.001},
                "seed": 7,
                "debug_required": False,
                "changes_code_behavior": False,
                "code_verified": True,
                "primary_metric": "score",
                "code_binding": {
                    "codebase_id": "CB-0001",
                    "branch": "main",
                    "commit": commit,
                    "tag": None,
                },
            },
            budget={"max_runs": 1},
            stop_condition={"max_failures": 1},
        )
        return task, repository

    def _legacy_run(self) -> dict[str, object]:
        task = create_task(
            self.project,
            owner_request="legacy exact bytes",
            route="tune",
            target_refs=["baseline"],
            route_inputs={
                "config": {"learning_rate": 0.001},
                "seed": 7,
                "debug_required": False,
                "changes_code_behavior": False,
                "code_verified": True,
                "primary_metric": "score",
            },
            budget={"max_runs": 1},
            stop_condition={"max_failures": 1},
        )
        transition_task(self.project, task["id"], "preparing")
        readiness(
            self.project,
            task["id"],
            target_clear=True,
            template_runnable=True,
            mutation_allowed=True,
            code_verified=True,
        )
        return create_run(
            self.project,
            task["id"],
            {
                "code": {"commit": "a" * 40},
                "config": {"learning_rate": 0.001},
                "seed": 7,
                "data": {"kind": "synthetic_debug_only"},
                "environment": {"python": "test"},
            },
            purpose="debug",
        )

    def _finish_formal(self) -> tuple[dict[str, object], Path]:
        task, _repository = self._bound_task()
        first = self._execute_evidence(str(task["id"]))
        self.assertEqual("artifact_seal_pending", first["status"])
        run = load_run(self.project, first["run"]["id"])
        output = (
            self.control
            / "runs"
            / run["id"]
            / "execution_snapshot"
            / ".cv-workflow-output"
        )
        return run, output

    def _sealed_output(self, run_id: str) -> Path:
        return (
            self.project
            / ".cv-workflow-seals"
            / run_id
            / ".cv-workflow-output"
        )

    def _comparison_task(
        self,
        source_run: dict[str, object],
    ) -> dict[str, object]:
        source_task = load_task(self.project, str(source_run["task_id"]))
        binding = source_task["route_inputs"]["code_binding"]
        return create_task(
            self.project,
            owner_request="compare one sealed real run",
            route="tune",
            target_refs=[str(source_run["id"])],
            route_inputs={
                "config": {"learning_rate": 0.001},
                "seed": 7,
                "debug_required": False,
                "changes_code_behavior": False,
                "code_verified": True,
                "primary_metric": "score",
                "baseline": 0.8,
                "baseline_run_ref": source_run["id"],
                "code_binding": binding,
            },
            budget={"max_runs": 1},
            stop_condition={"max_failures": 1},
        )

    def test_legacy_unbound_run_keeps_the_exact_old_shape(self) -> None:
        run = self._legacy_run()
        self.assertEqual(RUN_FIELDS, set(run))
        self.assertNotIn("output_seal", run)
        self.assertEqual(run, load_run(self.project, run["id"]))

    def test_bound_run_starts_unsealed_and_seal_binds_exact_output_tree(
        self,
    ) -> None:
        run, _output = self._finish_formal()
        self.assertIn("output_seal", run)
        self.assertIsNone(run["output_seal"])

        sealed = seal_run_outputs(self.project, run["id"])
        seal = sealed["output_seal"]
        self.assertEqual(OUTPUT_SEAL_SCHEMA, seal["schema"])
        self.assertEqual(
            {
                "kind": "project_sealed_output_copy",
                "relative_path": f".cv-workflow-seals/{run['id']}",
            },
            seal["root"],
        )
        self.assertEqual(
            {
                "kind": "run_execution_snapshot_output",
                "relative_path": ".cv-workflow-output",
            },
            seal["source"],
        )
        self.assertEqual(
            run["execution_snapshot"]["snapshot_sha256"],
            seal["execution_snapshot_sha256"],
        )
        self.assertEqual(run["frozen_digest"], seal["frozen_digest"])
        self.assertEqual(
            {
                "path": ".cv-workflow-output/run.log",
                "size_bytes": len("finished\n".encode()),
                "sha256": "sha256:"
                + hashlib.sha256(b"finished\n").hexdigest(),
            },
            seal["raw_log"],
        )
        self.assertEqual(
            [".cv-workflow-output/metrics.json"],
            [item["path"] for item in seal["artifacts"]],
        )
        self.assertEqual(2, seal["total_files"])
        self.assertRegex(seal["finished_snapshot_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(seal["seal_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(sealed, load_run(self.project, run["id"]))
        self.assertEqual(seal, verify_run_output_seal(self.project, run["id"]))

    def test_second_execute_registers_evidence_and_closes_after_seal(self) -> None:
        run, _output = self._finish_formal()
        seal_run_outputs(self.project, run["id"])

        completed = self._execute_evidence(str(run["task_id"]))

        self.assertEqual("closed", completed["run"]["execution"]["stage"])
        self.assertEqual("single_run", completed["evidence_level"])
        self.assertEqual("done", completed["task"]["stage"])
        self.assertEqual("done", load_task(self.project, run["task_id"])["stage"])
        self.assertEqual(
            completed["run"]["output_seal"],
            seal_run_outputs(self.project, run["id"])["output_seal"],
        )

    def test_two_live_sealed_runs_produce_a_formal_comparison(self) -> None:
        source, _output = self._finish_formal()
        seal_run_outputs(self.project, source["id"])
        source_completed = self._execute_evidence(str(source["task_id"]))
        source = source_completed["run"]
        comparison_task = self._comparison_task(source)

        first = self._execute_evidence(str(comparison_task["id"]))
        self.assertEqual("artifact_seal_pending", first["status"])
        seal_run_outputs(self.project, first["run"]["id"])
        completed = self._execute_evidence(str(comparison_task["id"]))

        self.assertEqual("formal", completed["comparison"]["comparison_scope"])
        self.assertEqual([], completed["comparison"]["blockers"])
        self.assertEqual(0.0, completed["comparison"]["delta"])
        self.assertEqual(
            completed["comparison"],
            load_task(self.project, comparison_task["id"])["conclusion"][
                "comparison"
            ],
        )

    def test_formal_comparison_tampering_is_rejected_without_route_contract(
        self,
    ) -> None:
        source, _output = self._finish_formal()
        seal_run_outputs(self.project, source["id"])
        source = self._execute_evidence(str(source["task_id"]))["run"]
        comparison_task = self._comparison_task(source)
        first = self._execute_evidence(str(comparison_task["id"]))
        seal_run_outputs(self.project, first["run"]["id"])
        completed = self._execute_evidence(str(comparison_task["id"]))
        self.assertEqual("formal", completed["comparison"]["comparison_scope"])
        self.assertNotIn(
            "route_contract",
            completed["run"]["frozen"]["config"],
        )
        validate_project_workflow(self.project)

        task_path = (
            self.control / "tasks" / f"{comparison_task['id']}.json"
        )
        original_bytes = task_path.read_bytes()
        original = json.loads(original_bytes.decode("utf-8"))
        mutations = {
            "delta": lambda item: item["conclusion"]["comparison"].__setitem__(
                "delta", 123.0
            ),
            "scope": lambda item: item["conclusion"]["comparison"].__setitem__(
                "comparison_scope", "side_by_side_only"
            ),
            "blockers": lambda item: item["conclusion"]["comparison"][
                "blockers"
            ].append("tampered: 人工改写"),
        }
        for label, mutate in mutations.items():
            with self.subTest(field=label):
                tampered = deepcopy(original)
                mutate(tampered)
                task_path.write_text(
                    json.dumps(tampered, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "永久比较结果"):
                    validate_project_workflow(self.project)
                task_path.write_bytes(original_bytes)
                validate_project_workflow(self.project)

        downgrade_cases = {
            "pre_fair_whole_shape": {
                "primary_metric": "score",
                "baseline": 0.8,
                "candidate": 0.8,
                "delta": 0.0,
            },
            "fair_without_live_whole_shape": {
                **deepcopy(original["conclusion"]["comparison"]),
                "delta": None,
                "comparison_scope": "side_by_side_only",
                "blockers": [
                    "live_output_seal_verifier_missing: legacy candidate"
                ],
            },
        }
        for label, comparison in downgrade_cases.items():
            with self.subTest(downgrade=label):
                tampered = deepcopy(original)
                tampered["conclusion"]["comparison"] = comparison
                task_path.write_text(
                    json.dumps(tampered, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "永久比较结果"):
                    validate_project_workflow(self.project)
                task_path.write_bytes(original_bytes)
                validate_project_workflow(self.project)

        combined = deepcopy(original)
        combined["route_inputs"]["baseline"] = 0.1
        combined["conclusion"]["comparison"] = {
            "primary_metric": "score",
            "baseline": 0.1,
            "candidate": 0.8,
            "delta": 0.7000000000000001,
        }
        task_path.write_text(
            json.dumps(combined, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "永久比较结果"):
            validate_project_workflow(self.project)
        task_path.write_bytes(original_bytes)
        validate_project_workflow(self.project)

    def test_source_seal_drift_blocks_final_comparison_irreversibly(self) -> None:
        source, _output = self._finish_formal()
        seal_run_outputs(self.project, source["id"])
        source = self._execute_evidence(str(source["task_id"]))["run"]
        comparison_task = self._comparison_task(source)
        first = self._execute_evidence(str(comparison_task["id"]))
        seal_run_outputs(self.project, first["run"]["id"])

        source_metrics = self._sealed_output(source["id"]) / "metrics.json"
        source_metrics.write_text('{"score":0.9}\n', encoding="utf-8")
        blocked = self._execute_evidence(str(comparison_task["id"]))

        self.assertEqual("comparison_reverify_failed", blocked["status"])
        self.assertEqual("reviewing", blocked["task"]["stage"])
        self.assertIsNone(blocked["comparison"]["delta"])
        self.assertTrue(
            any(
                item.startswith("source_run_output_reverify_failed:")
                for item in blocked["comparison"]["blockers"]
            )
        )

        # 封存同时绑定了文件内容与身份/时间元数据。即使把字节写回原值，
        # 也不能把一次已经发生的篡改伪装成“从未改过”。
        source_metrics.write_text('{"score":0.8}\n', encoding="utf-8")
        still_blocked = self._execute_evidence(str(comparison_task["id"]))
        self.assertEqual(
            "comparison_reverify_failed",
            still_blocked["status"],
        )
        self.assertEqual("reviewing", still_blocked["task"]["stage"])
        self.assertIsNone(still_blocked["comparison"]["delta"])
        self.assertTrue(
            any(
                item.startswith("source_run_output_reverify_failed:")
                for item in still_blocked["comparison"]["blockers"]
            )
        )

    def test_candidate_seal_drift_never_persists_a_formal_comparison(self) -> None:
        source, _output = self._finish_formal()
        seal_run_outputs(self.project, source["id"])
        source = self._execute_evidence(str(source["task_id"]))["run"]
        comparison_task = self._comparison_task(source)
        first = self._execute_evidence(str(comparison_task["id"]))
        seal_run_outputs(self.project, first["run"]["id"])
        candidate = load_run(self.project, first["run"]["id"])
        candidate_metrics = self._sealed_output(candidate["id"]) / "metrics.json"
        candidate_metrics.write_text('{"score":0.9}\n', encoding="utf-8")

        with self.assertRaises(ValueError):
            self._execute_evidence(str(comparison_task["id"]))

        task = load_task(self.project, comparison_task["id"])
        self.assertEqual("reviewing", task["stage"])
        self.assertIsNone(task["conclusion"])

    def test_custom_adapter_cannot_create_real_evidence_without_test_attestation(
        self,
    ) -> None:
        task, _repository = self._bound_task()

        with self.assertRaisesRegex(
            ValueError,
            "受信任|Adapter|CUDA",
        ):
            execute_task(
                self.project,
                str(task["id"]),
                purpose="evidence",
            )

        self.assertEqual([], load_task(self.project, str(task["id"]))["run_refs"])

    def test_debug_run_finishes_normally_but_cannot_be_sealed(self) -> None:
        task, _repository = self._bound_task()
        completed = execute_task(self.project, task["id"], purpose="debug")

        self.assertEqual("closed", completed["run"]["execution"]["stage"])
        self.assertEqual("debug", completed["evidence_level"])
        with self.assertRaisesRegex(ValueError, "evidence|论文|正式"):
            seal_run_outputs(self.project, completed["run"]["id"])

    def test_extra_file_and_budget_overflow_are_rejected_without_mutation(
        self,
    ) -> None:
        run, output = self._finish_formal()
        (output / "undeclared.txt").write_text("extra\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "未声明|exact|多余"):
            seal_run_outputs(self.project, run["id"])
        self.assertIsNone(load_run(self.project, run["id"])["output_seal"])

        (output / "undeclared.txt").unlink()
        with mock.patch.object(output_seal_module, "MAX_TOTAL_OUTPUT_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "大小|bytes|上限"):
                seal_run_outputs(self.project, run["id"])
        self.assertIsNone(load_run(self.project, run["id"])["output_seal"])

    def test_live_source_drift_does_not_replace_authoritative_sealed_copy(
        self,
    ) -> None:
        run, output = self._finish_formal()
        sealed = seal_run_outputs(self.project, run["id"])["output_seal"]
        sealed_output = self._sealed_output(run["id"])
        self.assertEqual(
            (output / "metrics.json").read_bytes(),
            (sealed_output / "metrics.json").read_bytes(),
        )

        (output / "metrics.json").write_text('{"score":0.9}\n', encoding="utf-8")
        (output / "extra.txt").write_text("live extra\n", encoding="utf-8")
        self.assertEqual(
            sealed,
            verify_run_output_seal(self.project, run["id"]),
        )

        (sealed_output / "metrics.json").write_text(
            '{"score":0.9}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "变化|不一致|drift"):
            verify_run_output_seal(self.project, run["id"])

        (sealed_output / "metrics.json").write_text(
            '{"score":0.8}\n',
            encoding="utf-8",
        )
        (sealed_output / "extra.txt").write_text("sealed extra\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "未声明|exact|多余"):
            verify_run_output_seal(self.project, run["id"])

    def test_hardlink_duplicate_and_windows_reserved_paths_are_rejected(
        self,
    ) -> None:
        run, output = self._finish_formal()
        metrics = output / "metrics.json"
        metrics.unlink()
        os.link(output / "run.log", metrics)
        with self.assertRaisesRegex(ValueError, "硬链接|普通文件"):
            seal_run_outputs(self.project, run["id"])
        self.assertIsNone(load_run(self.project, run["id"])["output_seal"])

        with self.assertRaisesRegex(ValueError, "Windows"):
            output_seal_module._normalize_output_path(
                ".cv-workflow-output/con.txt",
                "test path",
            )

    def test_growth_or_new_file_during_hashing_is_rejected(self) -> None:
        run, output = self._finish_formal()
        original_copy = output_seal_module._copy_output_file
        calls = 0

        def grow_after_first(
            item: dict[str, object],
            sealed_root: Path,
        ) -> dict[str, object]:
            nonlocal calls
            record = original_copy(item, sealed_root)
            calls += 1
            if calls == 1:
                with (output / "metrics.json").open("ab") as stream:
                    stream.write(b"x")
            return record

        with mock.patch.object(
            output_seal_module,
            "_copy_output_file",
            side_effect=grow_after_first,
        ):
            with self.assertRaisesRegex(ValueError, "变化|变大"):
                seal_run_outputs(self.project, run["id"])
        self.assertIsNone(load_run(self.project, run["id"])["output_seal"])

    def test_post_write_race_rolls_back_the_seal(self) -> None:
        run, _output = self._finish_formal()
        sealed_output = self._sealed_output(run["id"])
        original_verify = output_seal_module._verify_output_tree
        calls = 0

        def mutate_before_second_verify(
            project_root: Path,
            current_run: dict[str, object],
            seal: dict[str, object],
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                with (sealed_output / "metrics.json").open("ab") as stream:
                    stream.write(b"x")
            original_verify(project_root, current_run, seal)

        with mock.patch.object(
            output_seal_module,
            "_verify_output_tree",
            side_effect=mutate_before_second_verify,
        ):
            with self.assertRaisesRegex(ValueError, "变化|不一致"):
                seal_run_outputs(self.project, run["id"])
        self.assertIsNone(load_run(self.project, run["id"])["output_seal"])
        self.assertFalse(
            (self.project / ".cv-workflow-seals" / run["id"]).exists()
        )

    def test_public_cli_only_needs_project_and_run(self) -> None:
        run, _output = self._finish_formal()
        payload = cli_json(
            "seal-run-outputs",
            "--project",
            self.project,
            "--run",
            run["id"],
        )
        self.assertEqual("sealed", payload["status"])
        self.assertEqual(run["id"], payload["run"]["id"])
        self.assertRegex(
            payload["run"]["output_seal"]["seal_sha256"],
            r"^sha256:[0-9a-f]{64}$",
        )


if __name__ == "__main__":
    unittest.main()
