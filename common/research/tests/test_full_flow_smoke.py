from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "run_full_flow_smoke.py"
PAPERFLOW_ROOT = (
    Path(os.environ["PAPERFLOW_BOUND_RECEIVER_ROOT"])
    if os.environ.get("PAPERFLOW_BOUND_RECEIVER_ROOT")
    else None
)
FORMAL_RUNTIME = (
    Path(os.environ["PAPERFLOW_LIBRARY_ROOT"])
    if os.environ.get("PAPERFLOW_LIBRARY_ROOT")
    else None
)
CUDA_PYTHON = (
    Path(os.environ["CV_WORKFLOW_CUDA_PYTHON"])
    if os.environ.get("CV_WORKFLOW_CUDA_PYTHON")
    else None
)


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "run_full_flow_smoke",
        TOOL,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load full-flow smoke tool")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FullFlowSmokeTests(unittest.TestCase):
    def test_test_fixture_commit_uses_only_command_scoped_identity(
        self,
    ) -> None:
        module = _load_tool()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "empty-home"
            repository = root / "repository"
            home.mkdir()
            repository.mkdir()
            isolated_environment = {
                key: value
                for key, value in os.environ.items()
                if key.upper()
                in {
                    "COMSPEC",
                    "PATH",
                    "PATHEXT",
                    "SYSTEMDRIVE",
                    "SYSTEMROOT",
                    "WINDIR",
                }
            }
            isolated_environment.update(
                {
                    "HOME": str(home),
                    "USERPROFILE": str(home),
                    "HOMEDRIVE": home.drive,
                    "HOMEPATH": str(home)[len(home.drive) :],
                    "APPDATA": str(home / "AppData" / "Roaming"),
                    "LOCALAPPDATA": str(home / "AppData" / "Local"),
                    "GIT_CONFIG_NOSYSTEM": "1",
                }
            )
            for key in (
                "GIT_AUTHOR_NAME",
                "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL",
            ):
                self.assertNotIn(key, isolated_environment)

            with mock.patch.dict(
                os.environ,
                isolated_environment,
                clear=True,
            ):
                module._run_git_text(repository, "init")
                fixture = repository / "fixture.txt"
                fixture.write_text("fixture\n", encoding="utf-8")
                module._run_git_text(repository, "add", "--", fixture.name)
                module._commit_test_fixture(
                    repository,
                    "test: isolated fixture",
                )
                identity = module._run_git_text(
                    repository,
                    "show",
                    "-s",
                    "--format=%an%n%ae%n%cn%n%ce",
                    "HEAD",
                ).splitlines()
                self.assertEqual(
                    [
                        "CV-Workflow-Test",
                        "cv-workflow-test@example.invalid",
                        "CV-Workflow-Test",
                        "cv-workflow-test@example.invalid",
                    ],
                    identity,
                )

                fixture.write_text("fixture two\n", encoding="utf-8")
                module._run_git_text(repository, "add", "--", fixture.name)
                with self.assertRaisesRegex(
                    RuntimeError,
                    "Author identity unknown|unable to auto-detect",
                ):
                    module._run_git_text(
                        repository,
                        "commit",
                        "-m",
                        "ordinary commit must stay unconfigured",
                    )

    def test_formal_database_uri_is_read_only_and_immutable(self) -> None:
        module = _load_tool()
        database = Path("formal-library") / "database" / "knowledge.db"

        uri = module._readonly_sqlite_uri(database)

        self.assertTrue(uri.startswith("file:"))
        self.assertTrue(uri.endswith("?mode=ro&immutable=1"))

    def test_cli_requires_explicit_library_root(self) -> None:
        module = _load_tool()
        self.assertFalse(
            hasattr(module, "DEFAULT_LIBRARY_ROOT"),
            "通用闭环工具不能内置某台电脑的 PaperFlow 知识库路径",
        )
        with self.assertRaises(SystemExit):
            module._build_parser().parse_args(
                [
                    "--paperflow-root",
                    "paperflow-candidate",
                    "--artifacts-root",
                    "artifacts",
                ]
            )

    def test_full_flow_proves_production_handoff_and_never_approves(self) -> None:
        if (
            PAPERFLOW_ROOT is None
            or FORMAL_RUNTIME is None
            or CUDA_PYTHON is None
            or not PAPERFLOW_ROOT.is_dir()
            or not FORMAL_RUNTIME.is_dir()
            or not CUDA_PYTHON.is_file()
        ):
            self.skipTest(
                "本机 PaperFlow 候选、正式只读知识库或 CUDA Python 不存在"
            )
        module = _load_tool()
        with tempfile.TemporaryDirectory() as temporary:
            report = module.run_full_flow(
                paperflow_root=PAPERFLOW_ROOT,
                library_root=FORMAL_RUNTIME,
                artifacts_root=Path(temporary) / "artifacts",
                cuda_python=CUDA_PYTHON,
                device="cpu",
            )

            self.assertEqual("pass", report["status"])
            self.assertTrue(report["safety"]["test_fixture_only"])
            self.assertTrue(report["safety"]["not_for_submission"])
            self.assertFalse(report["safety"]["approved"])
            self.assertFalse(report["safety"]["published"])
            self.assertEqual(
                "rejected",
                report["research"]["debug_export"],
            )
            bound_package = report["research"]["production_bound_package"]
            self.assertEqual(
                {
                    "skill_id": "cv-experiment-workflow",
                    "release_version": "1.5.0",
                    "system_version": "SYS-V2.13.0",
                },
                bound_package["producer"],
            )
            self.assertEqual(
                "pass",
                bound_package["paperflow_receiver_validation"],
            )
            self.assertEqual(
                "imported",
                bound_package["paperflow_import"],
            )
            self.assertEqual(
                "complete",
                bound_package["paperflow_source_matching"],
            )
            self.assertEqual(
                "pass",
                bound_package["paperflow_writing_gate"],
            )
            self.assertTrue(bound_package["complete_for_paper"])
            self.assertEqual(
                "temporary_only",
                bound_package["package_retention"],
            )
            self.assertTrue(bound_package["not_for_submission"])
            self.assertTrue(
                bound_package["debug_closed_before_evidence"]
            )
            self.assertNotIn(
                bound_package["debug_run_ref"],
                bound_package["source_run_refs"],
            )
            self.assertEqual(
                "pass",
                bound_package["central_cuda_attestation"],
            )
            self.assertTrue(bound_package["device_name"])
            self.assertTrue(bound_package["torch_version"])
            self.assertTrue(bound_package["cuda_runtime"])
            self.assertEqual(
                8.0,
                bound_package["central_cuda_probe"]["result"],
            )
            self.assertTrue(
                bound_package["central_cuda_probe"][
                    "tensor_device"
                ].startswith("cuda:")
            )
            self.assertEqual(13, report["paperflow"]["method_cards"])
            self.assertEqual(3, report["paperflow"]["combinations"])
            self.assertEqual("pass", report["paperflow"]["section_audit"])
            self.assertEqual("pass", report["paperflow"]["paper_audit"])
            package_checks = report["paperflow"]["package_checks"]
            self.assertNotEqual(
                "1.5.0",
                package_checks["producer_release"],
                "六章候选写作仍应由独立 PaperFlow 测试包负责",
            )
            self.assertEqual("rejected", package_checks["tampered"])
            self.assertEqual(
                "rejected",
                package_checks["missing_required_file"],
            )
            self.assertEqual(
                "quarantined_from_writing",
                package_checks["incomplete"],
            )
            self.assertEqual(
                "ok",
                report["knowledge_base"]["integrity_check"],
            )
            self.assertEqual(
                "mode=ro&immutable=1; query_only=ON",
                report["knowledge_base"]["open_mode"],
            )
            self.assertEqual(
                0,
                report["knowledge_base"]["foreign_key_errors"],
            )
            self.assertIn(
                "fts",
                report["knowledge_base"]["retrieval_sources"],
            )
            self.assertIn(
                "vector",
                report["knowledge_base"]["retrieval_sources"],
            )
            preview = Path(report["artifacts"]["preview"])
            self.assertTrue(preview.is_file())
            saved_report = Path(report["artifacts"]["report"])
            self.assertTrue(saved_report.is_file())
            self.assertEqual(
                str(saved_report),
                json.loads(saved_report.read_text(encoding="utf-8"))[
                    "artifacts"
                ]["report"],
            )
            self.assertIn(
                "not_for_submission",
                preview.read_text(encoding="utf-8"),
            )
            proofs = report["positive_proofs"]
            self.assertEqual(
                "pass",
                proofs["production_1_5_to_current_paperflow_gate"]["status"],
            )
            self.assertEqual(
                "pass",
                proofs["paperflow_fixture_to_six_section_candidate"]["status"],
            )
            self.assertFalse(
                proofs["production_1_5_to_current_paperflow_gate"][
                    "drives_six_section_writing"
                ]
            )
            self.assertTrue(
                proofs["paperflow_fixture_to_six_section_candidate"][
                    "drives_six_section_writing"
                ]
            )
            self.assertNotEqual(
                proofs["production_1_5_to_current_paperflow_gate"]["purpose"],
                proofs["paperflow_fixture_to_six_section_candidate"][
                    "purpose"
                ],
            )
            self.assertEqual(
                [],
                list(Path(temporary).rglob("approval.json")),
            )

    def test_cli_rejects_artifacts_inside_formal_library(self) -> None:
        if (
            PAPERFLOW_ROOT is None
            or FORMAL_RUNTIME is None
            or CUDA_PYTHON is None
            or not PAPERFLOW_ROOT.is_dir()
            or not FORMAL_RUNTIME.is_dir()
            or not CUDA_PYTHON.is_file()
        ):
            self.skipTest(
                "本机 PaperFlow 候选、正式只读知识库或 CUDA Python 不存在"
            )
        result = subprocess.run(
            [
                sys.executable,
                str(TOOL),
                "--paperflow-root",
                str(PAPERFLOW_ROOT),
                "--library-root",
                str(FORMAL_RUNTIME),
                "--artifacts-root",
                str(FORMAL_RUNTIME / "forbidden-smoke-output"),
                "--cuda-python",
                str(CUDA_PYTHON),
                "--device",
                "cpu",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )

        self.assertNotEqual(0, result.returncode)
        payload = json.loads(result.stdout)
        self.assertEqual("fail", payload["status"])
        self.assertIn("正式知识库", payload["error"])
        self.assertFalse(
            (FORMAL_RUNTIME / "forbidden-smoke-output").exists()
        )


if __name__ == "__main__":
    unittest.main()
