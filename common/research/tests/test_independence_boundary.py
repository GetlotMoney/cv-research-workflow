from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "cv-experiment-workflow"
SCRIPTS = SKILL / "scripts"


class IndependenceBoundaryTests(unittest.TestCase):
    def test_v2_cli_has_no_legacy_migration_entry(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "rw.py"), "--help"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertNotIn("migrate", completed.stdout)
        self.assertFalse((SCRIPTS / "workflow_core" / "migration.py").exists())
        self.assertFalse((SCRIPTS / "workflow_core" / "legacy.py").exists())

    def test_runtime_and_active_tests_do_not_depend_on_old_project(self) -> None:
        forbidden = ("sample_project", "gptj", "GPTG", "gptg")
        runtime_files = sorted(SCRIPTS.rglob("*.py"))

        violations: list[str] = []
        for path in runtime_files:
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text:
                    violations.append(f"{path.relative_to(ROOT)}: {token}")

        self.assertEqual([], violations)
        self.assertFalse((ROOT / "tests" / "test_sample_project_legacy_view.py").exists())
        self.assertFalse((ROOT / "tests" / "test_sample_project_tune_pilot.py").exists())
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            if path == Path(__file__):
                continue
            self.assertNotIn(
                "sample_project_WORKTREE",
                path.read_text(encoding="utf-8"),
                path.name,
            )

    def test_runtime_exposes_no_old_project_bridge_api(self) -> None:
        forbidden = (
            "load_explicit_adapter",
            "load_explicit_adapter_with_sha256",
            "load_explicit_adapter_spec",
            "validate_legacy_parsed_result",
            "allow_missing_execution",
        )
        violations: list[str] = []

        for path in sorted((SCRIPTS / "workflow_core").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text:
                    violations.append(f"{path.relative_to(ROOT)}: {token}")

        self.assertEqual([], violations)

    def test_current_docs_define_independence_without_cutover(self) -> None:
        design = (
            ROOT
            / "docs"
            / "superpowers"
            / "specs"
            / "2026-07-17-cv-research-workflow-design.md"
        ).read_text(encoding="utf-8")
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        workflow = (SKILL / "references" / "workflow.md").read_text(
            encoding="utf-8"
        )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for text in (design, skill, workflow, readme):
            self.assertIn("独立", text)
        for name, text in {
            "design": design,
            "skill": skill,
            "workflow": workflow,
            "readme": readme,
        }.items():
            for forbidden in (
                "首个真实项目",
                "停止旧入口写入",
                "原子切换唯一写入口",
                "删除旧重复入口",
                "migrate --",
                "--cutover",
                "--prepare",
            ):
                self.assertNotIn(forbidden, text, name)

    def test_readme_explicitly_initializes_v2(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "rw.py"),
                    "init",
                    "--path",
                    str(project),
                    "--name",
                    "明确 v2",
                    "--layout",
                    "v2",
                ],
                check=True,
                capture_output=True,
            )
            project_json = (
                project / ".experiment-workflow" / "project.json"
            ).read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn('"schema": "cv-experiment-workflow.project.v2"', project_json)
        self.assertIn("--layout v2", readme)
        self.assertIn("当前 v2 尚未发布到公开 `main`", readme)
        self.assertNotIn("git clone", readme)

    def test_pytest_only_collects_the_formal_test_directory(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn("[tool.pytest.ini_options]", pyproject)
        self.assertIn('testpaths = ["tests"]', pyproject)


if __name__ == "__main__":
    unittest.main()
