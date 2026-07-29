from __future__ import annotations

import hashlib
import importlib
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._helpers import SCRIPTS
from tests.test_research_brief import research_brief_manifest


sys.path.insert(0, str(SCRIPTS))

from workflow_core.codebases import CODEBASE_SCHEMA, register_codebase  # noqa: E402
from workflow_core.engine import execute_task  # noqa: E402
from workflow_core.evidence import record_evidence_transition  # noqa: E402
from workflow_core.output_seal import seal_run_outputs  # noqa: E402
from workflow_core import paper_package as package_core  # noqa: E402
from workflow_core import paper_package_delivery as delivery_module  # noqa: E402
from workflow_core.paper_package import (  # noqa: E402
    export_paper_package,
    save_research_brief,
    seal_paper_package,
)
from workflow_core.project import init_project  # noqa: E402
from workflow_core.runs import load_run  # noqa: E402
from workflow_core.tasking import create_task, load_task  # noqa: E402
from workflow_core.v2_catalog import register_source  # noqa: E402


def _fake_central_cuda_attestation(
    *,
    gate: dict[str, object],
    adapter: dict[str, str],
) -> dict[str, object]:
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


class BoundPaperPackageV15Tests(unittest.TestCase):
    def setUp(self) -> None:
        cuda_attestation = mock.patch(
            "workflow_core.run_identity.create_central_cuda_attestation",
            side_effect=_fake_central_cuda_attestation,
        )
        cuda_attestation.start()
        self.addCleanup(cuda_attestation.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "paper-project"
        init_project(self.project, "bound-paper-package", layout="v2")
        brief_path = self.root / "brief.json"
        brief_path.write_text(
            json.dumps(research_brief_manifest(), ensure_ascii=False),
            encoding="utf-8",
        )
        self.brief = save_research_brief(self.project, brief_path)

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

    def _register_bound_repository(
        self,
        *,
        log_text: str = "score=0.8\n",
        artifact_name: str = "metrics.json",
        artifact_bytes: bytes = b'{"score":0.8}\n',
    ) -> str:
        repository = self.root / "codebase"
        repository.mkdir()
        self._git(repository, "init", "-b", "main")
        self._git(repository, "config", "user.name", "Test User")
        self._git(repository, "config", "user.email", "test@example.com")
        adapter_source = """\
def inspect(project):
    return {"standard": {"debug_required": False}}


def validate(project):
    return []


def prepare_runs(project, task):
    return [{
        "code": {"revision": "paper-package-v15"},
        "config": {
            "device": "cuda",
            "metric_definition": {
                "score": "validation split mean accuracy",
            },
        },
        "seed": 7,
        "data": {
            "kind": "real_imagefolder",
            "evaluation": {
                "schema": "test.evaluation.v1",
                "metric": "score",
                "split": "validation",
            },
            "dataset_identity": {
                "schema": "cv-experiment-workflow.dataset-identity.v1",
                "dataset_id": "paper-package-demo",
                "version": "1",
                "source_uri": "dataset://paper-package/demo",
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
        (output / "run.log").write_bytes(b"score=0.8\\n")
        (output / "metrics.json").write_bytes(b'{"score":0.8}\\n')
        return {"status": "finished", "process_id": 101}
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
"""
        adapter_source = adapter_source.replace(
            'b"score=0.8\\n"',
            repr(log_text.encode("utf-8")),
        )
        adapter_source = adapter_source.replace(
            '(output / "metrics.json").write_bytes(b\'{"score":0.8}\\n\')',
            f"(output / {artifact_name!r}).write_bytes({artifact_bytes!r})",
        ).replace(
            '".cv-workflow-output/metrics.json"',
            f'".cv-workflow-output/{artifact_name}"',
        )
        (repository / "workflow_adapter.py").write_text(
            adapter_source,
            encoding="utf-8",
            newline="\n",
        )
        self._git(repository, "add", ".")
        self._git(repository, "commit", "-m", "bound package fixture")
        commit = self._git(repository, "rev-parse", "HEAD")
        manifest = {
            "schema": CODEBASE_SCHEMA,
            "name": "paper-package-codebase",
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
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        register_codebase(self.project, manifest_path, repository)
        return commit

    def _confirmed_bound_run(
        self,
        *,
        log_text: str = "score=0.8\n",
        artifact_name: str = "metrics.json",
        artifact_bytes: bytes = b'{"score":0.8}\n',
    ) -> tuple[dict[str, object], dict[str, object]]:
        task, run = self._pending_bound_run(
            log_text=log_text,
            artifact_name=artifact_name,
            artifact_bytes=artifact_bytes,
        )
        run_id = str(run["id"])
        seal_run_outputs(self.project, run_id)
        second = execute_task(self.project, task["id"], purpose="evidence")
        self.assertEqual("single_run", second["evidence_level"])
        record_evidence_transition(
            self.project,
            run_id,
            "confirmed",
            evidence_refs=[run_id],
            reason="独立复核通过",
            proposed_by="producer-test",
            checked_by="reviewer-test",
            applied_by="producer-test",
        )
        return load_task(self.project, task["id"]), load_run(self.project, run_id)

    def _pending_bound_run(
        self,
        *,
        log_text: str = "score=0.8\n",
        artifact_name: str = "metrics.json",
        artifact_bytes: bytes = b'{"score":0.8}\n',
    ) -> tuple[dict[str, object], dict[str, object]]:
        commit = self._register_bound_repository(
            log_text=log_text,
            artifact_name=artifact_name,
            artifact_bytes=artifact_bytes,
        )
        task = create_task(
            self.project,
            owner_request="produce one bound paper package",
            route="tune",
            target_refs=["baseline"],
            route_inputs={
                "config": {"metric": "score"},
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
        first = execute_task(self.project, task["id"], purpose="evidence")
        self.assertEqual("artifact_seal_pending", first["status"])
        return load_task(self.project, task["id"]), load_run(
            self.project,
            first["run"]["id"],
        )

    def _selection(
        self,
        task: dict[str, object],
        run: dict[str, object],
    ) -> dict[str, object]:
        brief = research_brief_manifest()
        run_id = str(run["id"])
        return {
            "schema": "cv-experiment-workflow.paper-package-selection.v1",
            "asset_mode": "hybrid",
            "paper_scope": {
                "title_hint": "正式绑定证据演示",
                "research_area": brief["research_area"],
                "goal": brief["objective"],
                "included_claim_ids": ["CLM-0001"],
                "excluded_topics": brief["scope"]["excluded"],
            },
            "claims": [
                {
                    "claim_id": "CLM-0001",
                    "kind": "result",
                    "origin": "project",
                    "statement_zh": "验证集平均准确率为 0.8。",
                    "statement_en": None,
                    "maturity": "confirmed",
                    "run_refs": [run_id],
                    "metric_refs": [
                        {"run_id": run_id, "metric_name": "score"},
                    ],
                    "source_refs": [],
                    "idea_refs": [],
                    "module_refs": [],
                    "innovation_boundary": None,
                    "allowed_sections": ["experiments"],
                },
            ],
            "experiments": [
                {
                    "experiment_id": "EXP-0001",
                    "title": "正式主结果",
                    "objective": "验证绑定证据包",
                    "task_id": task["id"],
                    "run_refs": [run_id],
                    "claim_refs": ["CLM-0001"],
                },
            ],
            "source_uses": [],
            "assets": [
                {
                    "asset_id": "AST-0001",
                    "source_object_ref": run_id,
                    "path": ".cv-workflow-output/run.log",
                    "role": "raw_log",
                    "required_for_writing": True,
                    "copy_allowed": True,
                    "license": "project-owned",
                    "privacy_classification": "internal",
                    "availability": "available",
                    "omission_reason": "",
                },
                {
                    "asset_id": "AST-0002",
                    "source_object_ref": run_id,
                    "path": ".cv-workflow-output/metrics.json",
                    "role": "result_data",
                    "required_for_writing": True,
                    "copy_allowed": True,
                    "license": "project-owned",
                    "privacy_classification": "internal",
                    "availability": "available",
                    "omission_reason": "",
                },
            ],
            "visuals": [],
            "supersedes_package_id": None,
        }

    def _seal_and_export(self) -> tuple[dict[str, object], Path, dict[str, object]]:
        task, run = self._confirmed_bound_run()
        selection_path = self.root / "selection.json"
        selection_path.write_text(
            json.dumps(self._selection(task, run), ensure_ascii=False),
            encoding="utf-8",
        )
        sealed = seal_paper_package(
            self.project,
            str(self.brief["brief_id"]),
            selection_path,
        )
        destination = self.root / "delivery" / str(sealed["package_id"])
        destination.parent.mkdir()
        export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            destination,
        )
        return sealed, destination, run

    def _seal_selection(
        self,
        task: dict[str, object],
        run: dict[str, object],
    ) -> dict[str, object]:
        selection_path = self.root / "selection.json"
        selection_path.write_text(
            json.dumps(self._selection(task, run), ensure_ascii=False),
            encoding="utf-8",
        )
        return seal_paper_package(
            self.project,
            str(self.brief["brief_id"]),
            selection_path,
        )

    def test_bound_custom_adapter_is_rejected_by_paperflow_receiver(
        self,
    ) -> None:
        _sealed, destination, run = self._seal_and_export()
        manifest = json.loads(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {
                "skill_id": "cv-experiment-workflow",
                "release_version": "1.5.0",
                "system_version": "SYS-V2.13.0",
            },
            manifest["producer"],
        )
        self.assertEqual(
            [run["id"]],
            manifest["source_run_refs"],
        )
        experiment = json.loads(
            (destination / "experiments.json").read_text(encoding="utf-8")
        )["items"][0]
        delivered_run = experiment["runs"][0]
        self.assertIn("execution_snapshot_manifest", delivered_run)
        self.assertIn("output_seal", delivered_run)
        self.assertEqual("confirmed", delivered_run["evidence_binding"]["level"])
        self.assertNotIn(
            str(self.root),
            json.dumps(
                {"manifest": manifest, "experiment": experiment},
                ensure_ascii=False,
            ),
        )

        receiver_root = os.environ.get("PAPERFLOW_BOUND_RECEIVER_ROOT")
        if receiver_root is None:
            self.skipTest("PAPERFLOW_BOUND_RECEIVER_ROOT 未设置")
        sys.path.insert(0, receiver_root)
        self.addCleanup(sys.path.remove, receiver_root)
        receiver = importlib.import_module("paperflow_v2.research_package")
        with self.assertRaisesRegex(
            receiver.ResearchPackageError,
            "central CUDA attestation adapter is invalid",
        ):
            receiver.validate_research_package(destination)

        raw_log = delivered_run["output_seal"]["raw_log"]
        included = next(
            item
            for item in manifest["delivery"]["assets"]
            if item["asset_id"] == "AST-0001"
        )
        authoritative = (
            self.project
            / ".cv-workflow-seals"
            / str(run["id"])
            / raw_log["source_path"]
        ).read_bytes()
        self.assertEqual(
            hashlib.sha256(authoritative).hexdigest(),
            raw_log["sha256"].removeprefix("sha256:"),
        )
        self.assertEqual(
            authoritative,
            (destination / included["package_path"]).read_bytes(),
        )

    def test_unsealed_or_unconfirmed_bound_run_cannot_be_packaged(self) -> None:
        task, run = self._pending_bound_run()
        with self.assertRaisesRegex(ValueError, "正式 Run"):
            self._seal_selection(task, run)

        seal_run_outputs(self.project, str(run["id"]))
        completed = execute_task(
            self.project,
            str(task["id"]),
            purpose="evidence",
        )
        with self.assertRaisesRegex(ValueError, "两事件证据链"):
            self._seal_selection(completed["task"], completed["run"])

    def test_authoritative_seal_drift_is_rejected_before_export(self) -> None:
        task, run = self._confirmed_bound_run()
        sealed = self._seal_selection(task, run)
        authoritative = (
            self.project
            / ".cv-workflow-seals"
            / str(run["id"])
            / ".cv-workflow-output"
            / "metrics.json"
        )
        authoritative.write_bytes(b'{"score":0.1}\n')
        destination = self.root / "delivery" / str(sealed["package_id"])
        destination.parent.mkdir()
        with self.assertRaises(ValueError):
            export_paper_package(
                self.project,
                str(sealed["package_id"]),
                "hybrid",
                destination,
            )
        self.assertFalse(destination.exists())

    def test_mutable_snapshot_output_is_not_used_for_delivery(self) -> None:
        task, run = self._confirmed_bound_run()
        sealed = self._seal_selection(task, run)
        snapshot_output = (
            self.project
            / ".experiment-workflow"
            / str(run["execution_snapshot"]["relative_path"])
            / ".cv-workflow-output"
            / "metrics.json"
        )
        snapshot_output.write_bytes(b'{"score":999}\n')
        destination = self.root / "delivery" / str(sealed["package_id"])
        destination.parent.mkdir()
        export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            destination,
        )
        manifest = json.loads(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )
        included = next(
            item
            for item in manifest["delivery"]["assets"]
            if item["asset_id"] == "AST-0002"
        )
        authoritative = (
            self.project
            / ".cv-workflow-seals"
            / str(run["id"])
            / ".cv-workflow-output"
            / "metrics.json"
        ).read_bytes()
        self.assertNotEqual(snapshot_output.read_bytes(), authoritative)
        self.assertEqual(
            authoritative,
            (destination / included["package_path"]).read_bytes(),
        )

    def test_execution_snapshot_drift_is_rejected_before_export(self) -> None:
        task, run = self._confirmed_bound_run()
        sealed = self._seal_selection(task, run)
        manifest_path = (
            self.project
            / ".experiment-workflow"
            / str(run["execution_snapshot"]["manifest_path"])
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["sha256"] = "sha256:" + ("0" * 64)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        destination = self.root / "delivery" / str(sealed["package_id"])
        destination.parent.mkdir()
        with self.assertRaises(ValueError):
            export_paper_package(
                self.project,
                str(sealed["package_id"]),
                "hybrid",
                destination,
            )
        self.assertFalse(destination.exists())

    def test_debug_run_cannot_be_selected_for_a_result_package(self) -> None:
        commit = self._register_bound_repository()
        task = create_task(
            self.project,
            owner_request="debug must not become paper evidence",
            route="tune",
            target_refs=["baseline"],
            route_inputs={
                "config": {"metric": "score"},
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
        completed = execute_task(
            self.project,
            str(task["id"]),
            purpose="debug",
        )
        with self.assertRaises(ValueError):
            self._seal_selection(completed["task"], completed["run"])

    def test_paperflow_rejects_tampered_included_output(self) -> None:
        _sealed, destination, _run = self._seal_and_export()
        receiver_root = os.environ.get("PAPERFLOW_BOUND_RECEIVER_ROOT")
        if receiver_root is None:
            self.skipTest("PAPERFLOW_BOUND_RECEIVER_ROOT 未设置")
        sys.path.insert(0, receiver_root)
        self.addCleanup(sys.path.remove, receiver_root)
        receiver = importlib.import_module("paperflow_v2.research_package")
        manifest = json.loads(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )
        included = next(
            item
            for item in manifest["delivery"]["assets"]
            if item["status"] == "included"
        )
        (destination / included["package_path"]).write_bytes(b"tampered\n")
        with self.assertRaises(receiver.ResearchPackageError):
            receiver.validate_research_package(destination)

    def test_local_paper_source_is_exported_with_portable_locator(self) -> None:
        task, run = self._confirmed_bound_run()
        paper_path = self.root / "prior-art.pdf"
        paper_path.write_bytes(b"%PDF-1.4\nportable source fixture\n%%EOF\n")
        source_manifest = self.root / "paper-source.json"
        source_manifest.write_text(
            json.dumps(
                {
                    "schema": "cv-experiment-workflow.source.v2",
                    "kind": "paper",
                    "identity": "Portable Prior Art 2026",
                    "locator": str(paper_path.resolve()),
                    "revision": "v1",
                    "commit": None,
                    "digest": "sha256:"
                    + hashlib.sha256(paper_path.read_bytes()).hexdigest(),
                    "license": "fair-use",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        source = register_source(
            self.project,
            source_manifest,
            paper_path,
        )
        selection = self._selection(task, run)
        selection["paper_scope"]["included_claim_ids"] = [
            "CLM-0001",
            "CLM-0002",
        ]
        selection["claims"].append(
            {
                "claim_id": "CLM-0002",
                "kind": "background",
                "origin": "external",
                "statement_zh": "已有研究讨论了原型校准。",
                "statement_en": None,
                "maturity": "supported",
                "run_refs": [],
                "metric_refs": [],
                "source_refs": [source["id"]],
                "idea_refs": [],
                "module_refs": [],
                "innovation_boundary": None,
                "allowed_sections": ["introduction", "related_work"],
            }
        )
        selection["source_uses"] = [
            {
                "source_ref": source["id"],
                "role": "background",
                "excerpt": "Prior work discusses prototype calibration.",
                "supports_claim_ids": ["CLM-0002"],
            }
        ]
        selection_path = self.root / "selection-with-source.json"
        selection_path.write_text(
            json.dumps(selection, ensure_ascii=False),
            encoding="utf-8",
        )
        sealed = seal_paper_package(
            self.project,
            str(self.brief["brief_id"]),
            selection_path,
        )
        destination = self.root / "delivery" / str(sealed["package_id"])
        destination.parent.mkdir()
        export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            destination,
        )
        source_row = json.loads(
            (destination / "sources.jsonl").read_text(encoding="utf-8")
        )
        self.assertEqual(
            f"source://{source['id']}",
            source_row["locator"],
        )
        # 内置正式模板与真实 GPU 的接收正向闭环由
        # test_full_flow_smoke.py 的完整流程测试负责。

    def test_text_output_with_local_absolute_path_is_not_exported(self) -> None:
        task, run = self._confirmed_bound_run(
            log_text="source=C:\\private\\dataset\\sample.jpg\n",
        )
        sealed = self._seal_selection(task, run)
        destination = self.root / "delivery" / str(sealed["package_id"])
        destination.parent.mkdir()
        with self.assertRaisesRegex(ValueError, "本机绝对路径"):
            export_paper_package(
                self.project,
                str(sealed["package_id"]),
                "hybrid",
                destination,
            )
        self.assertFalse(destination.exists())

    def _assert_utf8_asset_path_is_rejected(self, artifact_name: str) -> None:
        task, run = self._confirmed_bound_run(
            artifact_name=artifact_name,
            artifact_bytes=b"private=C:\\Users\\Alice\\secret.txt\n",
        )
        selection = self._selection(task, run)
        selection["assets"][1]["path"] = (
            f".cv-workflow-output/{artifact_name}"
        )
        selection_path = self.root / f"{artifact_name}.json"
        selection_path.write_text(
            json.dumps(selection, ensure_ascii=False), encoding="utf-8"
        )
        sealed = seal_paper_package(
            self.project,
            str(self.brief["brief_id"]),
            selection_path,
        )
        destination = self.root / "delivery" / str(sealed["package_id"])
        destination.parent.mkdir()
        with self.assertRaisesRegex(ValueError, "本机绝对路径"):
            export_paper_package(
                self.project,
                str(sealed["package_id"]),
                "hybrid",
                destination,
            )
        self.assertFalse(destination.exists())

    def test_py_utf8_asset_with_local_path_is_not_exported(self) -> None:
        self._assert_utf8_asset_path_is_rejected("leak.py")

    def test_extensionless_utf8_asset_with_local_path_is_not_exported(self) -> None:
        self._assert_utf8_asset_path_is_rejected("leak")

    def test_unknown_extension_utf8_asset_with_local_path_is_not_exported(self) -> None:
        self._assert_utf8_asset_path_is_rejected("leak.unknown")

    def test_invalid_utf8_prefix_cannot_hide_later_ascii_path(self) -> None:
        task, run = self._confirmed_bound_run(
            artifact_name="disguised.bin",
            artifact_bytes=(
                b"\x89\xff\x80binary-prefix\x00"
                b"private=C:\\Users\\Alice\\secret.txt\n"
            ),
        )
        selection = self._selection(task, run)
        selection["assets"][1]["path"] = (
            ".cv-workflow-output/disguised.bin"
        )
        selection_path = self.root / "disguised-selection.json"
        selection_path.write_text(
            json.dumps(selection, ensure_ascii=False),
            encoding="utf-8",
        )
        sealed = seal_paper_package(
            self.project,
            str(self.brief["brief_id"]),
            selection_path,
        )
        destination = self.root / "delivery" / str(sealed["package_id"])
        destination.parent.mkdir()
        with self.assertRaisesRegex(ValueError, "本机绝对路径"):
            export_paper_package(
                self.project,
                str(sealed["package_id"]),
                "hybrid",
                destination,
            )
        self.assertFalse(destination.exists())

    def test_utf16_bom_asset_with_local_path_is_not_exported(self) -> None:
        task, run = self._confirmed_bound_run(
            artifact_name="utf16.dat",
            artifact_bytes=(
                "private=C:\\Users\\Alice\\secret.txt\n".encode("utf-16")
            ),
        )
        selection = self._selection(task, run)
        selection["assets"][1]["path"] = ".cv-workflow-output/utf16.dat"
        selection_path = self.root / "utf16-selection.json"
        selection_path.write_text(
            json.dumps(selection, ensure_ascii=False),
            encoding="utf-8",
        )
        sealed = seal_paper_package(
            self.project,
            str(self.brief["brief_id"]),
            selection_path,
        )
        destination = self.root / "delivery" / str(sealed["package_id"])
        destination.parent.mkdir()
        with self.assertRaisesRegex(ValueError, "本机绝对路径"):
            export_paper_package(
                self.project,
                str(sealed["package_id"]),
                "hybrid",
                destination,
            )
        self.assertFalse(destination.exists())

    def test_utf32_bom_asset_with_local_path_is_not_exported(self) -> None:
        task, run = self._confirmed_bound_run(
            artifact_name="utf32.dat",
            artifact_bytes=(
                "private=C:\\Users\\Alice\\secret.txt\n".encode("utf-32")
            ),
        )
        selection = self._selection(task, run)
        selection["assets"][1]["path"] = ".cv-workflow-output/utf32.dat"
        selection_path = self.root / "utf32-selection.json"
        selection_path.write_text(
            json.dumps(selection, ensure_ascii=False),
            encoding="utf-8",
        )
        sealed = seal_paper_package(
            self.project,
            str(self.brief["brief_id"]),
            selection_path,
        )
        destination = self.root / "delivery" / str(sealed["package_id"])
        destination.parent.mkdir()
        with self.assertRaisesRegex(ValueError, "本机绝对路径"):
            export_paper_package(
                self.project,
                str(sealed["package_id"]),
                "hybrid",
                destination,
            )
        self.assertFalse(destination.exists())

    def test_big_endian_utf16_and_utf32_bom_paths_are_rejected(self) -> None:
        text = "private=C:\\Users\\Alice\\secret.txt\n"
        cases = {
            "utf16-be": b"\xfe\xff" + text.encode("utf-16-be"),
            "utf32-be": b"\x00\x00\xfe\xff" + text.encode("utf-32-be"),
        }
        for name, content in cases.items():
            with self.subTest(encoding=name):
                path = self.root / f"{name}.dat"
                path.write_bytes(content)
                with self.assertRaisesRegex(ValueError, "本机绝对路径"):
                    delivery_module._scan_portable_regular_file(
                        path,
                        name,
                        expected_size=len(content),
                        expected_sha256=(
                            "sha256:" + hashlib.sha256(content).hexdigest()
                        ),
                        root=self.root.resolve(),
                    )

    def test_windows_current_drive_root_path_is_not_exported(self) -> None:
        task, run = self._confirmed_bound_run(
            artifact_name="rooted.txt",
            artifact_bytes=b"private=\\Users\\Alice\\secret.txt\n",
        )
        selection = self._selection(task, run)
        selection["assets"][1]["path"] = ".cv-workflow-output/rooted.txt"
        selection_path = self.root / "rooted-selection.json"
        selection_path.write_text(
            json.dumps(selection, ensure_ascii=False),
            encoding="utf-8",
        )
        sealed = seal_paper_package(
            self.project,
            str(self.brief["brief_id"]),
            selection_path,
        )
        destination = self.root / "delivery" / str(sealed["package_id"])
        destination.parent.mkdir()
        with self.assertRaisesRegex(ValueError, "本机绝对路径"):
            export_paper_package(
                self.project,
                str(sealed["package_id"]),
                "hybrid",
                destination,
            )
        self.assertFalse(destination.exists())

    def test_asset_swap_after_portability_scan_is_rejected(self) -> None:
        task, run = self._confirmed_bound_run()
        sealed = self._seal_selection(task, run)
        destination = self.root / "delivery" / str(sealed["package_id"])
        destination.parent.mkdir()
        real_publish = delivery_module._publish_handoff

        def swap_then_publish(**kwargs: object) -> dict[str, object]:
            copy_items = kwargs["copy_items"]
            self.assertIsInstance(copy_items, list)
            item = next(
                row
                for row in copy_items
                if row["package_path"].endswith("/metrics.json")
            )
            source = Path(item["source"])
            replacement = source.with_name(source.name + ".replacement")
            replacement.write_bytes(source.read_bytes())
            os.replace(replacement, source)
            return real_publish(**kwargs)

        with mock.patch.object(
            delivery_module,
            "_publish_handoff",
            side_effect=swap_then_publish,
        ):
            with self.assertRaisesRegex(ValueError, "扫描后身份变化"):
                export_paper_package(
                    self.project,
                    str(sealed["package_id"]),
                    "hybrid",
                    destination,
                )
        self.assertFalse(destination.exists())

    def test_standalone_verify_scans_scientific_json_and_all_assets(self) -> None:
        _sealed, destination, _run = self._seal_and_export()
        real_scientific_scan = (
            delivery_module._validate_bound_scientific_portability
        )
        real_asset_scan = delivery_module._scan_portable_regular_file
        with (
            mock.patch.object(
                delivery_module,
                "_validate_bound_scientific_portability",
                wraps=real_scientific_scan,
            ) as scientific_scan,
            mock.patch.object(
                delivery_module,
                "_scan_portable_regular_file",
                wraps=real_asset_scan,
            ) as asset_scan,
        ):
            verified = delivery_module.verify_paper_package(destination)
        manifest = json.loads(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )
        included_paths = {
            row["package_path"]
            for row in manifest["delivery"]["assets"]
            if row["status"] == "included"
        }
        scanned_paths = {
            Path(call.args[0]).resolve()
            for call in asset_scan.call_args_list
        }
        self.assertEqual("pass", verified["status"])
        scientific_scan.assert_called_once()
        self.assertEqual(
            {
                (destination / relative).resolve()
                for relative in included_paths
            },
            scanned_paths,
        )

    def test_standalone_verify_rejects_resigned_scientific_local_path(self) -> None:
        _sealed, destination, _run = self._seal_and_export()
        manifest_path = destination / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        scientific = {
            name: (destination / name).read_bytes()
            for name in package_core.SCIENTIFIC_FILES
        }
        experiments = json.loads(scientific["experiments.json"])
        run = experiments["items"][0]["runs"][0]
        run["analysis"]["limitations"] = [
            "debug source: C:\\Users\\Alice\\private\\trace.json"
        ]
        seal = run["output_seal"]
        seal["finished_snapshot_sha256"] = (
            package_core._canonical_object_sha256(
                {
                    "id": run["run_id"],
                    "task_id": experiments["items"][0]["task_id"],
                    "purpose": run["purpose"],
                    "frozen_digest": run["frozen_digest"],
                    "execution": {
                        key: run["execution"].get(key)
                        for key in (
                            "outcome",
                            "started_at",
                            "finished_at",
                            "process_id",
                            "exit_code",
                            "issue_kind",
                        )
                    },
                    "result": run["result"],
                    "quality": run["quality"],
                    "analysis": run["analysis"],
                    "artifacts": run["artifact_paths"],
                    "execution_snapshot": run["execution_snapshot"],
                }
            )
        )
        seal["seal_sha256"] = package_core._canonical_object_sha256(
            {
                key: seal[key]
                for key in seal
                if key != "seal_sha256"
            }
        )
        manifest["reverification"]["runs"][0]["seal_sha256"] = (
            seal["seal_sha256"]
        )
        scientific["experiments.json"] = package_core._canonical_json_line(
            experiments
        )
        (destination / "experiments.json").write_bytes(
            scientific["experiments.json"]
        )
        manifest["content_sha256"] = package_core._content_sha256(scientific)
        for row in manifest["delivery"]["files"]:
            if row["path"] not in scientific:
                continue
            content = scientific[row["path"]]
            row["size_bytes"] = len(content)
            row["sha256"] = (
                f"sha256:{hashlib.sha256(content).hexdigest()}"
            )
        manifest["reverification"]["payload_sha256"] = (
            delivery_module._handoff_payload_sha256(
                destination,
                manifest["delivery"]["files"],
            )
        )
        manifest_bytes = package_core._canonical_json_line(manifest)
        manifest_path.write_bytes(manifest_bytes)
        asset_hashes = {
            row["path"]: row["sha256"]
            for row in manifest["delivery"]["files"]
            if row["path"].startswith("assets/included/")
        }
        (destination / "checksums.sha256").write_bytes(
            delivery_module._handoff_checksum_bytes(
                {"manifest.json": manifest_bytes, **scientific}
                | {path: None for path in asset_hashes},
                asset_hashes=asset_hashes,
            )
        )
        with self.assertRaisesRegex(ValueError, "本机绝对路径"):
            delivery_module.verify_paper_package(destination)

    def test_binary_asset_remains_exportable(self) -> None:
        artifact_name = "result.png"
        # 合法的 1×1 PNG；证明真实图片二进制不会因文本扫描误报。
        artifact_bytes = bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001"
            "0804000000b51c0c020000000b4944415478da6364f80f00"
            "010501012718e3660000000049454e44ae426082"
        )
        task, run = self._confirmed_bound_run(
            artifact_name=artifact_name,
            artifact_bytes=artifact_bytes,
        )
        selection = self._selection(task, run)
        selection["assets"][1]["path"] = (
            f".cv-workflow-output/{artifact_name}"
        )
        selection_path = self.root / "binary-selection.json"
        selection_path.write_text(
            json.dumps(selection, ensure_ascii=False), encoding="utf-8"
        )
        sealed = seal_paper_package(
            self.project,
            str(self.brief["brief_id"]),
            selection_path,
        )
        destination = self.root / "delivery" / str(sealed["package_id"])
        destination.parent.mkdir()
        export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            destination,
        )
        manifest = json.loads(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )
        included = next(
            row
            for row in manifest["delivery"]["assets"]
            if row["asset_id"] == "AST-0002"
        )
        self.assertEqual(
            artifact_bytes,
            (destination / included["package_path"]).read_bytes(),
        )

    def test_high_entropy_binary_samples_do_not_trigger_path_false_positive(
        self,
    ) -> None:
        self.assertFalse(
            delivery_module._contains_binary_local_absolute_path(
                "~\\&\x1d\ufffdrandom-binary"
            )
        )
        self.assertTrue(
            delivery_module._contains_binary_local_absolute_path(
                "~/private/research"
            )
        )
        generator = random.Random(20260727)
        path = self.root / "high-entropy.png"
        png_prefix = bytes.fromhex("89504e470d0a1a0a")
        for index in range(200):
            content = png_prefix + generator.randbytes(4096)
            path.write_bytes(content)
            facts, _identity, _resolved = (
                delivery_module._scan_portable_regular_file(
                    path,
                    f"high-entropy-png-{index}",
                    expected_size=len(content),
                    expected_sha256=(
                        "sha256:" + hashlib.sha256(content).hexdigest()
                    ),
                    root=self.root.resolve(),
                )
            )
            self.assertEqual(len(content), facts["size_bytes"])


if __name__ == "__main__":
    unittest.main()
