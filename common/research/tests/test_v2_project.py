from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._helpers import SCRIPTS, cli_json, read_json, run_cli


V1_ROOT_ENTRIES = {
    ".runtime",
    "adapter.json",
    "agents",
    "artifacts",
    "attempts",
    "code-assets",
    "ideas",
    "project.json",
    "templates",
    "trials",
    "versions",
    "workflow.lock.json",
}
V2_ROOT_ENTRIES = {
    ".runtime",
    "adapter.json",
    "artifacts",
    "evidence.jsonl",
    "ideas",
    "modules",
    "project.json",
    "runs",
    "sources",
    "tasks",
    "templates",
    "versions",
    "workflow.lock.json",
}


class V2ProjectLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _init_v2(self, name: str = "v2-project") -> Path:
        project = self.root / name
        run_cli(
            "init",
            "--path",
            project,
            "--name",
            name,
            "--layout",
            "v2",
        )
        return project / ".experiment-workflow"

    def test_v2_init_creates_exact_isolated_control_root(self) -> None:
        control = self._init_v2()

        self.assertEqual(
            V2_ROOT_ENTRIES,
            {entry.name for entry in control.iterdir()},
        )
        self.assertEqual(
            "cv-experiment-workflow.project.v2",
            read_json(control / "project.json")["schema"],
        )
        self.assertEqual(b"", (control / "evidence.jsonl").read_bytes())
        self.assertEqual(
            {
                "schema": "cv-experiment-workflow.artifact-index.v1",
                "artifacts": [],
            },
            read_json(control / "artifacts" / "index.json"),
        )
        self.assertNotIn("agents", V2_ROOT_ENTRIES)
        self.assertNotIn("trials", V2_ROOT_ENTRIES)
        self.assertNotIn("code-assets", V2_ROOT_ENTRIES)
        self.assertNotIn("attempts", V2_ROOT_ENTRIES)

    def test_v2_gitignore_tracks_run_ledger_but_ignores_large_run_artifacts(
        self,
    ) -> None:
        control = self._init_v2()
        project = control.parent
        subprocess.run(
            ["git", "init", str(project)],
            capture_output=True,
            check=True,
        )
        ledger = control / "runs" / "RUN-0001" / "run.json"
        ledger.parent.mkdir()
        ledger.write_text("{}", encoding="utf-8")
        artifact = project / "runs" / "workflow" / "RUN-0001" / "result.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("{}", encoding="utf-8")

        ledger_result = subprocess.run(
            ["git", "-C", str(project), "check-ignore", str(ledger)],
            capture_output=True,
            encoding="utf-8",
            text=True,
            check=False,
        )
        artifact_result = subprocess.run(
            ["git", "-C", str(project), "check-ignore", str(artifact)],
            capture_output=True,
            encoding="utf-8",
            text=True,
            check=False,
        )

        self.assertEqual(1, ledger_result.returncode, ledger_result.stdout)
        self.assertEqual(0, artifact_result.returncode, artifact_result.stderr)


    def test_default_and_explicit_v1_keep_the_original_layout(self) -> None:
        default_project = self.root / "default-v1"
        explicit_project = self.root / "explicit-v1"

        run_cli("init", "--path", default_project, "--name", "default")
        run_cli(
            "init",
            "--path",
            explicit_project,
            "--name",
            "explicit",
            "--layout",
            "v1",
        )

        for project in (default_project, explicit_project):
            control = project / ".experiment-workflow"
            self.assertEqual(
                V1_ROOT_ENTRIES,
                {entry.name for entry in control.iterdir()},
            )
            self.assertEqual(
                "cv-experiment-workflow.project.v1",
                read_json(control / "project.json")["schema"],
            )

    def test_init_project_rejects_unknown_layout(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(sys.path.remove, str(SCRIPTS))
        from workflow_core.project import init_project

        with self.assertRaisesRegex(ValueError, "layout"):
            init_project(self.root / "invalid", "invalid", layout="v3")

    def test_cli_rejects_unknown_layout_choice(self) -> None:
        result = run_cli(
            "init",
            "--path",
            self.root / "invalid-cli",
            "--name",
            "invalid",
            "--layout",
            "v3",
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("invalid choice", result.stderr)

    def test_v2_validation_accepts_empty_ledger(self) -> None:
        control = self._init_v2()

        self.assertEqual(
            {"sources": 0, "ideas": 0, "templates": 0, "modules": 0},
            self._validate(control),
        )

    def test_cli_validate_accepts_v2_empty_ledger(self) -> None:
        control = self._init_v2()

        self.assertEqual(
            {"sources": 0, "ideas": 0, "templates": 0, "modules": 0},
            cli_json("validate", "--project", control.parent),
        )

    def test_cli_validate_rejects_unknown_project_schema(self) -> None:
        control = self._init_v2()
        project_json = control / "project.json"
        payload = read_json(project_json)
        payload["schema"] = "cv-experiment-workflow.project.v999"
        self._write_json(project_json, payload)

        result = self._run_validate(control)

        self.assertEqual(2, result.returncode)
        self.assertIn("项目元数据无效", result.stderr)

    def test_cli_validate_rejects_v2_invalid_project_id(self) -> None:
        control = self._init_v2()
        project_json = control / "project.json"
        payload = read_json(project_json)
        payload["project_id"] = "not-a-uuid"
        self._write_json(project_json, payload)

        self.assertEqual(2, self._run_validate(control).returncode)

    def test_cli_validate_rejects_v2_blank_project_name(self) -> None:
        control = self._init_v2()
        project_json = control / "project.json"
        payload = read_json(project_json)
        payload["name"] = "   "
        self._write_json(project_json, payload)

        self.assertEqual(2, self._run_validate(control).returncode)

    def test_cli_validate_rejects_v2_invalid_workflow_lock(self) -> None:
        control = self._init_v2()
        self._write_json(control / "workflow.lock.json", {})

        self.assertEqual(2, self._run_validate(control).returncode)

    def test_cli_validate_rejects_v2_invalid_adapter(self) -> None:
        control = self._init_v2()
        self._write_json(control / "adapter.json", {})

        self.assertEqual(2, self._run_validate(control).returncode)

    def test_v2_retry_recovers_crash_before_staging_marker_write(self) -> None:
        project = self.root / "pre-marker-crash"
        script = r'''
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import workflow_core.project as project_module

original_write_json = project_module.atomic_write_json

def crash_before_marker(path, payload, *, transaction_id=None):
    if Path(path).name == project_module.STAGING_MARKER:
        os._exit(91)
    return original_write_json(
        path,
        payload,
        transaction_id=transaction_id,
    )

project_module.atomic_write_json = crash_before_marker
project_module.init_project(Path(sys.argv[2]), "crash", layout="v2")
'''
        crashed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-c",
                script,
                str(SCRIPTS),
                str(project),
            ],
            capture_output=True,
            encoding="utf-8",
            text=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(91, crashed.returncode, crashed.stderr)

        run_cli(
            "init",
            "--path",
            project,
            "--name",
            "recovered",
            "--layout",
            "v2",
        )

        control = project / ".experiment-workflow"
        self.assertEqual(
            V2_ROOT_ENTRIES,
            {entry.name for entry in control.iterdir()},
        )
        self.assertFalse((project / ".experiment-workflow.staging").exists())
        self.assertFalse((control / ".cv-experiment-workflow-staging").exists())
        self.assertEqual(
            [],
            [
                path
                for path in project.rglob("*")
                if ".cvexp-" in path.name
                or path.name == ".cv-experiment-workflow-staging"
            ],
        )

    def test_v2_validation_rejects_missing_and_unknown_root_entries(self) -> None:
        missing_control = self._init_v2("missing")
        (missing_control / "tasks").rmdir()

        with self.assertRaises(ValueError):
            self._validate(missing_control)

        unknown_control = self._init_v2("unknown")
        (unknown_control / "unexpected").mkdir()

        with self.assertRaises(ValueError):
            self._validate(unknown_control)

    def test_v2_validation_rejects_evidence_directory(self) -> None:
        control = self._init_v2()
        evidence = control / "evidence.jsonl"
        evidence.unlink()
        evidence.mkdir()

        with self.assertRaisesRegex(ValueError, "evidence.jsonl"):
            self._validate(control)

    def test_cli_validate_rejects_non_empty_v2_evidence(self) -> None:
        control = self._init_v2()
        (control / "evidence.jsonl").write_bytes(b"x")

        self.assertEqual(2, self._run_validate(control).returncode)

    def test_v2_validation_rejects_evidence_link_when_supported(self) -> None:
        control = self._init_v2()
        evidence = control / "evidence.jsonl"
        target = self.root / "external-evidence.jsonl"
        target.write_bytes(b"")
        evidence.unlink()
        try:
            os.symlink(target, evidence)
        except OSError as error:
            self.skipTest(f"当前平台不能安全创建文件符号链接：{error}")

        with self.assertRaisesRegex(ValueError, "evidence.jsonl"):
            self._validate(control)

    def test_v2_validation_rejects_object_directory_as_file(self) -> None:
        control = self._init_v2()
        tasks = control / "tasks"
        tasks.rmdir()
        tasks.write_bytes(b"")

        with self.assertRaisesRegex(ValueError, "tasks"):
            self._validate(control)

    def test_empty_directory_validator_rejects_file_itself(self) -> None:
        candidate = self.root / "not-a-directory"
        candidate.write_bytes(b"")

        self._assert_empty_directory_validator_rejects(candidate)

    def test_empty_directory_validator_rejects_link_itself_when_supported(
        self,
    ) -> None:
        target = self.root / "empty-target"
        target.mkdir()
        candidate = self.root / "linked-directory"
        try:
            os.symlink(target, candidate, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"当前平台不能安全创建目录符号链接：{error}")

        self._assert_empty_directory_validator_rejects(candidate)

    @staticmethod
    def _validate(control: Path) -> dict[str, int]:
        sys.path.insert(0, str(SCRIPTS))
        try:
            from workflow_core.validation import validate_boundaries_locked

            return validate_boundaries_locked(control)
        finally:
            sys.path.remove(str(SCRIPTS))

    def _assert_empty_directory_validator_rejects(self, path: Path) -> None:
        sys.path.insert(0, str(SCRIPTS))
        try:
            from workflow_core.validation import _validate_empty_directory

            try:
                _validate_empty_directory(path, "测试目录")
            except ValueError:
                return
            except Exception as error:
                self.fail(
                    f"应抛出 ValueError，实际为 {type(error).__name__}: {error}"
                )
            self.fail("空目录校验器错误接受了非普通目录")
        finally:
            sys.path.remove(str(SCRIPTS))

    @staticmethod
    def _run_validate(control: Path) -> subprocess.CompletedProcess[str]:
        return run_cli(
            "validate",
            "--project",
            control.parent,
            check=False,
        )

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
