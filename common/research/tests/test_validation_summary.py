from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "validate_release.py"


def load_tool():
    if not TOOL.is_file():
        raise AssertionError("真实发布验证工具尚未实现")
    spec = importlib.util.spec_from_file_location("validate_release", TOOL)
    if spec is None or spec.loader is None:
        raise AssertionError("无法加载真实发布验证工具")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def canonical_digest(summary: dict[str, object]) -> str:
    unsigned = dict(summary)
    unsigned.pop("integrity", None)
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ValidationSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name) / "发布 验证"
        self.research = self.base / "科研"
        self.paperflow = self.base / "论文"
        self.output_parent = self.base / "验证产物"
        self.research.mkdir(parents=True)
        self.paperflow.mkdir(parents=True)
        self.output_parent.mkdir(parents=True)
        self.paperflow_library = self.base / "论文知识库"
        self.paperflow_library.mkdir()
        self.hf_home = self.base / "huggingface-cache"
        self.hf_home.mkdir()
        self.cuda_python = self.base / "gpu-python.exe"
        self.cuda_python.write_bytes(b"plain cuda python fixture")
        self.installed_skills = self.base / "installed-skills"
        for skill_id in (
            "cv-paper-ingest",
            "cv-paper-retrieve",
            "cv-methodology-distill",
            "cv-compose-methods",
            "cv-write-abstract",
            "cv-write-section",
            "cv-paper-audit",
            "cv-assemble-paper",
        ):
            skill = self.installed_skills / skill_id
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                f"# {skill_id}\n",
                encoding="utf-8",
            )
        unlisted = self.installed_skills / "superpowers"
        unlisted.mkdir()
        (unlisted / "SHOULD_NOT_COPY.md").write_text(
            "not in the exact whitelist\n",
            encoding="utf-8",
        )
        candidate_skill = (
            self.paperflow / "docs" / "skills" / "cv-paper-workflow"
        )
        candidate_skill.mkdir(parents=True)
        (candidate_skill / "SKILL.md").write_text(
            "# candidate cv-paper-workflow\n",
            encoding="utf-8",
        )

    def _write_test(self, root: Path, test_root: str, source: str) -> None:
        target = root / test_root / "test_required.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")

    def _make_cleanup_sandbox(self, module, name: str):
        anchor = (self.base / "cleanup-anchor").resolve()
        anchor.mkdir(exist_ok=True)
        root = anchor / f"cvv-{name}"
        root.mkdir()
        marker = root / ".owner"
        owner_token = "a" * 48
        marker.write_text(owner_token, encoding="ascii")
        basetemp = root / "b"
        home = root / "h"
        basetemp.mkdir()
        home.mkdir()
        return module._ExecutionSandbox(
            root=root,
            anchor=anchor,
            identity=module._directory_identity(root),
            owner_token=owner_token,
            marker=marker,
            basetemp=basetemp,
            home=home,
            environment={},
            command=(),
            skill_mode="none",
        )

    def test_default_groups_execute_both_required_test_roots(self) -> None:
        module = load_tool()
        self.assertFalse(hasattr(module, "PROTECTED_ROOTS"))

        groups = module.build_default_groups(
            research_root=self.research,
            paperflow_root=self.paperflow,
            paperflow_library_root=self.paperflow_library,
            hf_home=self.hf_home,
            cuda_python=self.cuda_python,
            research_python=Path(sys.executable),
            paperflow_python=Path(sys.executable),
            research_timeout=12.0,
            paperflow_timeout=13.0,
            installed_skills_root=self.installed_skills,
        )

        self.assertEqual([item.group_id for item in groups], ["research", "paperflow"])
        self.assertEqual([item.timeout_seconds for item in groups], [12.0, 13.0])
        self.assertEqual(groups[0].cwd, self.research.resolve())
        self.assertEqual(groups[1].cwd, self.paperflow.resolve())
        self.assertIn("tests", groups[0].command)
        self.assertIn("tests_v2", groups[1].command)
        self.assertEqual(
            groups[0].allowed_skips,
            (
                module.AllowedSkip(
                    node_id=(
                        "tests/test_adapter_snapshot_isolation.py::"
                        "AdapterSnapshotIsolationTests::"
                        "test_bound_adapter_rejects_dir_fd_escape"
                    ),
                    reason="当前平台不支持 remove(dir_fd=...)",
                ),
            ),
        )
        self.assertEqual(groups[1].allowed_skips, ())
        for group in groups:
            self.assertIn("-I", group.command)
            self.assertTrue(
                any("pytest.main" in argument for argument in group.command)
            )
            self.assertNotIn("--collect-only", group.command)
        arguments = module._parser().parse_args(
            [
                "--paperflow-root",
                str(self.paperflow),
                "--paperflow-library-root",
                str(self.paperflow_library),
                "--hf-home",
                str(self.hf_home),
                "--cuda-python",
                str(self.cuda_python),
                "--json-out",
                str(self.output_parent / "summary.json"),
                "--protected-root",
                str(self.base / "旧 sample_project"),
                "--protected-root",
                str(self.base / "旧 sample_project 2"),
            ]
        )
        self.assertEqual(len(arguments.protected_root), 2)
        self.assertEqual(arguments.gpu_index, 0)

    def test_cli_requires_explicit_library_cuda_python_and_hf_home(self) -> None:
        module = load_tool()
        base = [
            "--paperflow-root",
            str(self.paperflow),
            "--json-out",
            str(self.output_parent / "summary.json"),
        ]
        missing_cases = (
            base,
            [
                *base,
                "--paperflow-library-root",
                str(self.paperflow_library),
                "--cuda-python",
                str(self.cuda_python),
            ],
            [
                *base,
                "--cuda-python",
                str(self.cuda_python),
                "--hf-home",
                str(self.hf_home),
            ],
            [
                *base,
                "--paperflow-library-root",
                str(self.paperflow_library),
                "--hf-home",
                str(self.hf_home),
            ],
        )
        for arguments in missing_cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit):
                    module._parser().parse_args(arguments)

        parsed = module._parser().parse_args(
            [
                *base,
                "--paperflow-library-root",
                str(self.paperflow_library),
                "--hf-home",
                str(self.hf_home),
                "--cuda-python",
                str(self.cuda_python),
            ]
        )
        self.assertEqual(parsed.gpu_index, 0)

    def test_environment_overrides_are_immutable_and_strictly_whitelisted(
        self,
    ) -> None:
        module = load_tool()
        group = module.ValidationGroup(
            group_id="contract",
            cwd=self.research,
            command=(sys.executable, "-c", "print('1 passed')"),
            timeout_seconds=10.0,
            environment_overrides=(
                ("CV_WORKFLOW_CUDA_VISIBLE_DEVICES", "0"),
            ),
        )

        self.assertIsInstance(group.environment_overrides, tuple)
        with self.assertRaises(FrozenInstanceError):
            group.environment_overrides = ()

        invalid_cases = (
            [("CV_WORKFLOW_CUDA_VISIBLE_DEVICES", "0")],
            (("CUDA_VISIBLE_DEVICES", "0"),),
            (
                ("CV_WORKFLOW_CUDA_VISIBLE_DEVICES", "0"),
                ("CV_WORKFLOW_CUDA_VISIBLE_DEVICES", "1"),
            ),
            (("CV_WORKFLOW_CUDA_VISIBLE_DEVICES", "-1"),),
            (("PAPERFLOW_LIBRARY_ROOT", ""),),
            (["PAPERFLOW_LIBRARY_ROOT", str(self.paperflow_library)],),
        )
        for overrides in invalid_cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    module.ValidationGroup(
                        group_id="invalid",
                        cwd=self.research,
                        command=(sys.executable, "-c", "print('1 passed')"),
                        timeout_seconds=10.0,
                        environment_overrides=overrides,
                    )

    def test_research_group_sees_only_explicit_paths_and_gpu_child_selector(
        self,
    ) -> None:
        module = load_tool()
        expected = {
            "PAPERFLOW_BOUND_RECEIVER_ROOT": str(self.paperflow.resolve()),
            "PAPERFLOW_LIBRARY_ROOT": str(self.paperflow_library.resolve()),
            "HF_HOME": str(self.hf_home.resolve()),
            "CV_WORKFLOW_CUDA_PYTHON": str(self.cuda_python.resolve()),
            "CV_WORKFLOW_CUDA_VISIBLE_DEVICES": "0",
        }
        source = (
            "import os\n\n"
            f"EXPECTED = {expected!r}\n\n"
            "def test_explicit_environment_contract():\n"
            "    for key, value in EXPECTED.items():\n"
            "        assert os.environ.get(key) == value\n"
            "    assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''\n"
            "    assert os.environ.get('HF_HUB_OFFLINE') == '1'\n"
            "    assert os.environ.get('TRANSFORMERS_OFFLINE') == '1'\n"
        )
        self._write_test(self.research, "tests", source)
        ambient = {
            "PAPERFLOW_BOUND_RECEIVER_ROOT": "ambient-paperflow-attacker",
            "PAPERFLOW_LIBRARY_ROOT": "ambient-library-attacker",
            "HF_HOME": "ambient-huggingface-attacker",
            "CV_WORKFLOW_CUDA_PYTHON": "ambient-python-attacker",
            "CV_WORKFLOW_CUDA_VISIBLE_DEVICES": "7",
            "CUDA_VISIBLE_DEVICES": "9",
        }

        with mock.patch.dict(os.environ, ambient):
            group = module.build_default_groups(
                research_root=self.research,
                paperflow_root=self.paperflow,
                paperflow_library_root=self.paperflow_library,
                hf_home=self.hf_home,
                cuda_python=self.cuda_python,
                gpu_index=0,
                research_python=Path(sys.executable),
                paperflow_python=Path(sys.executable),
                research_timeout=30.0,
                paperflow_timeout=30.0,
                installed_skills_root=self.installed_skills,
            )[0]
            report = module.validate_release(
                groups=[group],
                json_out=self.output_parent / "explicit-environment.json",
                forbidden_roots=(self.research, self.paperflow),
            )

        self.assertTrue(report["ok"], report)
        serialized = json.dumps(report, ensure_ascii=False)
        for value in expected.values():
            if value != "0":
                self.assertNotIn(value, serialized)
        for value in ambient.values():
            if len(value) > 1:
                self.assertNotIn(value, serialized)

    def test_ordinary_group_cannot_inherit_gpu_or_bound_paths(self) -> None:
        module = load_tool()
        script = (
            "import os; "
            "assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''; "
            "assert 'CV_WORKFLOW_CUDA_VISIBLE_DEVICES' not in os.environ; "
            "assert 'CV_WORKFLOW_CUDA_PYTHON' not in os.environ; "
            "assert 'PAPERFLOW_BOUND_RECEIVER_ROOT' not in os.environ; "
            "assert 'PAPERFLOW_LIBRARY_ROOT' not in os.environ; "
            "assert 'HF_HOME' not in os.environ; "
            "print('1 passed')"
        )
        group = module.ValidationGroup(
            group_id="ordinary",
            cwd=self.research,
            command=(sys.executable, "-B", "-c", script),
            timeout_seconds=10.0,
        )
        ambient = {
            "PAPERFLOW_BOUND_RECEIVER_ROOT": "ambient-paperflow-attacker",
            "PAPERFLOW_LIBRARY_ROOT": "ambient-library-attacker",
            "HF_HOME": "ambient-huggingface-attacker",
            "CV_WORKFLOW_CUDA_PYTHON": "ambient-python-attacker",
            "CV_WORKFLOW_CUDA_VISIBLE_DEVICES": "7",
            "CUDA_VISIBLE_DEVICES": "9",
        }

        with mock.patch.dict(os.environ, ambient):
            result = module.run_validation_group(group)

        self.assertEqual(result["status"], "PASS", result)

    def test_passing_command_that_mutates_source_tree_is_rejected(self) -> None:
        module = load_tool()
        tracked = self.research / "tracked.txt"
        tracked.write_text("before\n", encoding="utf-8")
        script = (
            "from pathlib import Path; "
            f"Path({str(tracked)!r}).write_text('after\\n', encoding='utf-8'); "
            "print('1 passed')"
        )
        group = module.ValidationGroup(
            group_id="mutating",
            cwd=self.research,
            command=(sys.executable, "-B", "-c", script),
            timeout_seconds=10.0,
        )

        result = module.run_validation_group(group)

        self.assertEqual(result["return_code"], 0, result)
        self.assertEqual(result["passed"], 1, result)
        self.assertEqual(result["status"], "FAIL", result)
        self.assertFalse(result["source_snapshot_unchanged"])
        snapshot = result["source_snapshot"]
        self.assertFalse(snapshot["unchanged"])
        self.assertNotEqual(
            snapshot["before_sha256"],
            snapshot["after_sha256"],
        )

    def test_source_snapshot_detects_added_deleted_and_empty_entries(
        self,
    ) -> None:
        module = load_tool()
        cases = (
            (
                "added",
                None,
                "Path('added.txt').write_text('new\\n', encoding='utf-8')",
                "added.txt",
            ),
            (
                "deleted",
                ("deleted.txt", "old\n"),
                "Path('deleted.txt').unlink()",
                "deleted.txt",
            ),
            (
                "empty-directory",
                None,
                "Path('new-empty-directory').mkdir()",
                "new-empty-directory",
            ),
        )
        for name, fixture, mutation, changed_path in cases:
            with self.subTest(name=name):
                source = self.base / f"source-{name}"
                source.mkdir()
                if fixture is not None:
                    relative, content = fixture
                    (source / relative).write_text(content, encoding="utf-8")
                group = module.ValidationGroup(
                    group_id=name,
                    cwd=source,
                    command=(
                        sys.executable,
                        "-B",
                        "-c",
                        f"from pathlib import Path; {mutation}; print('1 passed')",
                    ),
                    timeout_seconds=10.0,
                )

                result = module.run_validation_group(group)

                self.assertEqual(result["status"], "FAIL", result)
                self.assertIn(
                    changed_path,
                    result["source_snapshot"]["changed_paths"],
                )

    def test_rewriting_identical_bytes_does_not_fail_source_snapshot(
        self,
    ) -> None:
        module = load_tool()
        tracked = self.research / "tracked.txt"
        tracked.write_text("same\n", encoding="utf-8")
        group = module.ValidationGroup(
            group_id="same-bytes",
            cwd=self.research,
            command=(
                sys.executable,
                "-B",
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('tracked.txt').write_text("
                    "'same\\n', encoding='utf-8'); "
                    "print('1 passed')"
                ),
            ),
            timeout_seconds=10.0,
        )

        result = module.run_validation_group(group)

        self.assertEqual(result["status"], "PASS", result)
        self.assertTrue(result["source_snapshot_unchanged"])
        self.assertEqual(
            result["source_snapshot"]["before_sha256"],
            result["source_snapshot"]["after_sha256"],
        )

    def test_earlier_group_cannot_mutate_a_later_source_root(self) -> None:
        module = load_tool()
        victim = self.paperflow / "victim.txt"
        victim.write_text("before\n", encoding="utf-8")
        first = module.ValidationGroup(
            group_id="first",
            cwd=self.research,
            command=(
                sys.executable,
                "-B",
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(victim)!r}).write_text("
                    "'after\\n', encoding='utf-8'); "
                    "print('1 passed')"
                ),
            ),
            timeout_seconds=10.0,
        )
        second = module.ValidationGroup(
            group_id="second",
            cwd=self.paperflow,
            command=(sys.executable, "-B", "-c", "print('1 passed')"),
            timeout_seconds=10.0,
        )
        output = self.output_parent / "cross-root-mutation.json"

        report = module.validate_release(
            groups=(first, second),
            json_out=output,
            forbidden_roots=(self.research, self.paperflow),
        )

        self.assertFalse(report["ok"], report)
        self.assertFalse(report["source_roots_unchanged"])
        self.assertEqual(report["groups"][0]["status"], "FAIL")
        self.assertTrue(report["groups"][0]["executed"])
        self.assertEqual(report["groups"][1]["status"], "FAIL")
        self.assertFalse(report["groups"][1]["executed"])
        self.assertTrue(output.is_file())
        affected = next(
            item
            for item in report["source_roots"]
            if item["group_ids"] == ["second"]
        )
        self.assertEqual(affected["detected_after_group"], "first")
        self.assertIn("victim.txt", affected["changed_paths"])

    def test_removed_later_root_still_writes_structured_failure(self) -> None:
        module = load_tool()
        moved = self.paperflow.with_name("paperflow-moved")
        first = module.ValidationGroup(
            group_id="first",
            cwd=self.research,
            command=(
                sys.executable,
                "-B",
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(self.paperflow)!r}).rename("
                    f"Path({str(moved)!r})); "
                    "print('1 passed')"
                ),
            ),
            timeout_seconds=10.0,
        )
        second = module.ValidationGroup(
            group_id="second",
            cwd=self.paperflow,
            command=(sys.executable, "-B", "-c", "print('1 passed')"),
            timeout_seconds=10.0,
        )
        output = self.output_parent / "removed-root.json"

        report = module.validate_release(
            groups=(first, second),
            json_out=output,
            forbidden_roots=(self.research, self.paperflow),
        )

        self.assertFalse(report["ok"], report)
        self.assertTrue(output.is_file())
        self.assertTrue(report["groups"][0]["executed"])
        self.assertFalse(report["groups"][1]["executed"])
        self.assertEqual(
            report["groups"][1]["source_snapshot"]["status"],
            "ERROR",
        )

    def test_git_index_only_mutation_fails_source_snapshot(self) -> None:
        module = load_tool()
        repository = self.base / "git-source"
        repository.mkdir()
        tracked = repository / "tracked.txt"
        tracked.write_text("same working bytes\n", encoding="utf-8")
        subprocess.run(
            ("git", "init", "--quiet", str(repository)),
            check=True,
        )
        subprocess.run(
            ("git", "-C", str(repository), "add", "tracked.txt"),
            check=True,
        )
        subprocess.run(
            (
                "git",
                "-C",
                str(repository),
                "-c",
                "user.name=Validation Test",
                "-c",
                "user.email=validation@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "fixture",
            ),
            check=True,
        )
        group = module.ValidationGroup(
            group_id="git-index",
            cwd=repository,
            command=(
                sys.executable,
                "-B",
                "-c",
                (
                    "import subprocess; "
                    "subprocess.run("
                    "['git', 'rm', '--cached', '--quiet', 'tracked.txt'], "
                    "check=True); "
                    "print('1 passed')"
                ),
            ),
            timeout_seconds=10.0,
        )

        result = module.run_validation_group(group)

        self.assertEqual(result["return_code"], 0, result)
        self.assertEqual(result["passed"], 1, result)
        self.assertEqual(result["status"], "FAIL", result)
        self.assertIn(
            ".git-semantics",
            result["source_snapshot"]["changed_paths"],
        )
        self.assertEqual(
            tracked.read_text(encoding="utf-8"),
            "same working bytes\n",
        )

    def test_hostile_ambient_git_variables_cannot_redirect_validation(
        self,
    ) -> None:
        module = load_tool()

        def initialize_repository(root: Path, filename: str) -> None:
            root.mkdir()
            (root / filename).write_text("tracked\n", encoding="utf-8")
            subprocess.run(
                ("git", "init", "--quiet", str(root)),
                check=True,
            )
            subprocess.run(
                ("git", "-C", str(root), "add", filename),
                check=True,
            )
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Validation Test",
                    "-c",
                    "user.email=validation@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ),
                check=True,
            )

        victim = self.base / "git-victim"
        attacker = self.base / "git-attacker"
        initialize_repository(victim, "victim.txt")
        initialize_repository(attacker, "attacker.txt")
        group = module.ValidationGroup(
            group_id="hostile-git-environment",
            cwd=victim,
            command=(
                sys.executable,
                "-B",
                "-c",
                (
                    "import os, subprocess; "
                    "assert 'GIT_DIR' not in os.environ; "
                    "assert 'GIT_WORK_TREE' not in os.environ; "
                    "assert 'GIT_INDEX_FILE' not in os.environ; "
                    "assert 'GIT_CONFIG_COUNT' not in os.environ; "
                    "subprocess.run("
                    "['git', 'rm', '--cached', '--quiet', 'victim.txt'], "
                    "check=True); "
                    "print('1 passed')"
                ),
            ),
            timeout_seconds=10.0,
        )
        hostile = {
            "GIT_DIR": str(attacker / ".git"),
            "GIT_WORK_TREE": str(victim),
            "GIT_INDEX_FILE": str(attacker / ".git" / "index"),
            "GIT_OBJECT_DIRECTORY": str(attacker / ".git" / "objects"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.worktree",
            "GIT_CONFIG_VALUE_0": str(victim),
        }

        with mock.patch.dict(os.environ, hostile, clear=False):
            result = module.run_validation_group(group)

        self.assertEqual(result["return_code"], 0, result)
        self.assertEqual(result["passed"], 1, result)
        self.assertEqual(result["status"], "FAIL", result)
        self.assertIn(
            ".git-semantics",
            result["source_snapshot"]["changed_paths"],
        )
        victim_files = subprocess.run(
            ("git", "-C", str(victim), "ls-files"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        attacker_files = subprocess.run(
            ("git", "-C", str(attacker), "ls-files"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(victim_files, "")
        self.assertEqual(attacker_files, "attacker.txt")

    def test_nested_link_is_rejected_before_command_can_write_outside(
        self,
    ) -> None:
        module = load_tool()
        source = self.base / "linked-source"
        outside = self.base / "outside-link-target"
        source.mkdir()
        outside.mkdir()
        marker = outside / "marker.txt"
        marker.write_text("unchanged\n", encoding="utf-8")
        try:
            (source / "linked").symlink_to(
                outside,
                target_is_directory=True,
            )
        except OSError as error:
            self.skipTest(f"symlink unavailable: {error}")
        group = module.ValidationGroup(
            group_id="linked",
            cwd=source,
            command=(
                sys.executable,
                "-B",
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('linked/marker.txt').write_text("
                    "'changed\\n', encoding='utf-8'); "
                    "print('1 passed')"
                ),
            ),
            timeout_seconds=10.0,
        )

        result = module.run_validation_group(group)

        self.assertEqual(result["status"], "FAIL", result)
        self.assertFalse(result["executed"])
        self.assertEqual(result["source_snapshot"]["status"], "ERROR")
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged\n")

    def test_real_tests_run_and_atomic_summary_contains_verifiable_digest(
        self,
    ) -> None:
        module = load_tool()
        self._write_test(
            self.research,
            "tests",
            "def test_research_required():\n    assert 2 + 2 == 4\n",
        )
        self._write_test(
            self.paperflow,
            "tests_v2",
            "def test_paperflow_required():\n    assert 'paper'.upper() == 'PAPER'\n",
        )
        output = self.output_parent / "validation-summary.json"
        events: list[str] = []
        groups = module.build_default_groups(
            research_root=self.research,
            paperflow_root=self.paperflow,
            paperflow_library_root=self.paperflow_library,
            hf_home=self.hf_home,
            cuda_python=self.cuda_python,
            research_python=Path(sys.executable),
            paperflow_python=Path(sys.executable),
            research_timeout=30.0,
            paperflow_timeout=30.0,
            installed_skills_root=self.installed_skills,
        )

        report = module.validate_release(
            groups=groups,
            json_out=output,
            forbidden_roots=(self.research, self.paperflow),
            progress=events.append,
        )

        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["version"], "1.2.0")
        self.assertEqual(report["power_action"], "none")
        self.assertTrue(report["source_roots_unchanged"])
        self.assertEqual(len(report["source_roots"]), 2)
        for source_root in report["source_roots"]:
            self.assertEqual(source_root["status"], "PASS")
            self.assertEqual(
                source_root["before_sha256"],
                source_root["after_sha256"],
            )
            self.assertTrue(source_root["final_matches_before"])
        self.assertRegex(
            report["generated_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$",
        )
        self.assertEqual(len(report["groups"]), 2)
        for group in report["groups"]:
            self.assertEqual(group["status"], "PASS")
            self.assertEqual(group["mode"], "execute")
            self.assertTrue(group["executed"])
            self.assertEqual(group["return_code"], 0)
            self.assertFalse(group["timed_out"])
            self.assertEqual(group["skipped"], 0)
            self.assertEqual(group["allowed_skipped"], 0)
            self.assertEqual(group["unexpected_skipped"], 0)
            self.assertEqual(group["process_tree_cleanup"], "PASS")
            self.assertTrue(group["source_snapshot_unchanged"])
            self.assertTrue(group["release_source_snapshot_unchanged"])
            self.assertTrue(group["source_snapshot"]["unchanged"])
            self.assertEqual(
                group["source_snapshot"]["before_sha256"],
                group["source_snapshot"]["after_sha256"],
            )
            self.assertGreaterEqual(group["duration_seconds"], 0)
            self.assertIsInstance(group["command"], list)
        self.assertTrue(any(event.startswith("[START] research") for event in events))
        self.assertTrue(any(event.startswith("[PASS] paperflow") for event in events))

        on_disk = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, report)
        self.assertEqual(
            report["integrity"],
            {
                "algorithm": "sha256",
                "scope": "canonical-json-without-integrity",
                "sha256": canonical_digest(report),
            },
        )

        before = output.read_bytes()
        with self.assertRaisesRegex(ValueError, "已存在|覆盖"):
            module.validate_release(
                groups=groups,
                json_out=output,
                forbidden_roots=(self.research, self.paperflow),
            )
        self.assertEqual(output.read_bytes(), before)

    def test_skipped_required_test_makes_release_fail(self) -> None:
        module = load_tool()
        self._write_test(
            self.research,
            "tests",
            (
                "import pytest\n\n"
                "@pytest.mark.skip(reason='required backend missing')\n"
                "def test_required():\n"
                "    assert True\n"
            ),
        )
        group = module.build_default_groups(
            research_root=self.research,
            paperflow_root=self.paperflow,
            paperflow_library_root=self.paperflow_library,
            hf_home=self.hf_home,
            cuda_python=self.cuda_python,
            research_python=Path(sys.executable),
            paperflow_python=Path(sys.executable),
            research_timeout=30.0,
            paperflow_timeout=30.0,
            installed_skills_root=self.installed_skills,
        )[0]

        report = module.validate_release(
            groups=[group],
            json_out=self.output_parent / "skip.json",
            forbidden_roots=(self.research, self.paperflow),
        )

        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["groups"][0]["status"], "FAIL")
        self.assertEqual(report["groups"][0]["skipped"], 1)
        self.assertEqual(report["groups"][0]["allowed_skipped"], 0)
        self.assertEqual(report["groups"][0]["unexpected_skipped"], 1)

    def test_exact_allowed_platform_skip_passes_and_is_recorded_separately(
        self,
    ) -> None:
        module = load_tool()
        self._write_test(
            self.research,
            "tests",
            (
                "import pytest\n\n"
                "@pytest.mark.skip(reason='platform lacks dir_fd')\n"
                "def test_platform_only():\n"
                "    assert True\n"
                "\n"
                "def test_required_passes():\n"
                "    assert True\n"
            ),
        )
        group = module.ValidationGroup(
            group_id="platform",
            cwd=self.research,
            command=module._pytest_command(Path(sys.executable), "tests"),
            timeout_seconds=30.0,
            allowed_skips=(
                module.AllowedSkip(
                    node_id="tests/test_required.py::test_platform_only",
                    reason="platform lacks dir_fd",
                ),
            ),
        )

        report = module.validate_release(
            groups=[group],
            json_out=self.output_parent / "allowed-skip.json",
            forbidden_roots=(self.research, self.paperflow),
        )

        self.assertTrue(report["ok"], report)
        result = report["groups"][0]
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["allowed_skipped"], 1)
        self.assertEqual(result["unexpected_skipped"], 0)

    def test_xfail_xpass_and_deselection_cannot_bypass_required_tests(
        self,
    ) -> None:
        module = load_tool()
        cases = {
            "xfailed": (
                (
                    "import pytest\n\n"
                    "@pytest.mark.xfail(reason='known failure')\n"
                    "def test_not_fixed():\n"
                    "    assert False\n\n"
                    "def test_required_passes():\n"
                    "    assert True\n"
                ),
                (),
            ),
            "xpassed": (
                (
                    "import pytest\n\n"
                    "@pytest.mark.xfail(reason='unexpected pass')\n"
                    "def test_not_fixed():\n"
                    "    assert True\n\n"
                    "def test_required_passes():\n"
                    "    assert True\n"
                ),
                (),
            ),
            "deselected": (
                (
                    "def test_filtered_out():\n"
                    "    assert True\n\n"
                    "def test_required_passes():\n"
                    "    assert True\n"
                ),
                ("-k", "test_required_passes"),
            ),
        }
        for outcome, (source, extra_args) in cases.items():
            with self.subTest(outcome=outcome):
                root = self.base / outcome
                self._write_test(root, "tests", source)
                group = module.ValidationGroup(
                    group_id=outcome,
                    cwd=root,
                    command=(
                        module._pytest_command(Path(sys.executable), "tests")
                        + extra_args
                    ),
                    timeout_seconds=30.0,
                )

                report = module.validate_release(
                    groups=[group],
                    json_out=self.output_parent / f"{outcome}.json",
                    forbidden_roots=(self.research, self.paperflow),
                )

                result = report["groups"][0]
                self.assertFalse(report["ok"], result)
                self.assertEqual(result["status"], "FAIL")
                self.assertEqual(result[outcome], 1)

    def test_pytest_environment_cannot_ignore_required_tests(self) -> None:
        module = load_tool()
        self._write_test(
            self.research,
            "tests",
            "def test_required_failure():\n    assert False\n",
        )
        (self.research / "tests" / "test_control.py").write_text(
            "def test_control_passes():\n    assert True\n",
            encoding="utf-8",
        )
        group = module.build_default_groups(
            research_root=self.research,
            paperflow_root=self.paperflow,
            paperflow_library_root=self.paperflow_library,
            hf_home=self.hf_home,
            cuda_python=self.cuda_python,
            research_python=Path(sys.executable),
            paperflow_python=Path(sys.executable),
            research_timeout=30.0,
            paperflow_timeout=30.0,
            installed_skills_root=self.installed_skills,
        )[0]

        with mock.patch.dict(
            os.environ,
            {
                "PYTEST_ADDOPTS": "--ignore=tests/test_required.py",
                "PYTEST_PLUGINS": "untrusted_plugin",
            },
        ):
            environment = module._offline_test_environment(group.seed)
            report = module.validate_release(
                groups=[group],
                json_out=self.output_parent / "hostile-pytest-env.json",
                forbidden_roots=(self.research, self.paperflow),
            )

        self.assertNotIn("PYTEST_ADDOPTS", environment)
        self.assertNotIn("PYTEST_PLUGINS", environment)
        self.assertFalse(report["ok"], report)
        self.assertEqual(report["groups"][0]["failed"], 1)

    def test_pythonpath_cannot_replace_the_real_pytest_runner(self) -> None:
        module = load_tool()
        self._write_test(
            self.research,
            "tests",
            "def test_required_failure():\n    assert False\n",
        )
        attacker = self.base / "attacker-controlled"
        fake_pytest = attacker / "pytest"
        fake_pytest.mkdir(parents=True)
        (fake_pytest / "__init__.py").write_text("", encoding="utf-8")
        (fake_pytest / "__main__.py").write_text(
            "print('999 passed in 0.01s')\n",
            encoding="utf-8",
        )
        group = module.build_default_groups(
            research_root=self.research,
            paperflow_root=self.paperflow,
            paperflow_library_root=self.paperflow_library,
            hf_home=self.hf_home,
            cuda_python=self.cuda_python,
            research_python=Path(sys.executable),
            paperflow_python=Path(sys.executable),
            research_timeout=30.0,
            paperflow_timeout=30.0,
            installed_skills_root=self.installed_skills,
        )[0]

        with mock.patch.dict(
            os.environ,
            {
                "PYTHONPATH": str(attacker),
                "PYTHONHOME": str(attacker),
            },
        ):
            environment = module._offline_test_environment(group.seed)
            report = module.validate_release(
                groups=[group],
                json_out=self.output_parent / "hostile-python-env.json",
                forbidden_roots=(self.research, self.paperflow),
            )

        self.assertIsNone(environment.get("PYTHONPATH"))
        self.assertIsNone(environment.get("PYTHONHOME"))
        self.assertFalse(report["ok"], report)
        self.assertEqual(report["groups"][0]["failed"], 1)
        self.assertNotIn(
            "999 passed",
            "\n".join(report["groups"][0]["output_tail"]),
        )

    def test_candidate_skill_rejects_reparse_in_docs_ancestor(self) -> None:
        module = load_tool()
        candidate_skill = (
            self.paperflow / "docs" / "skills" / "cv-paper-workflow"
        )
        (candidate_skill / "SKILL.md").unlink()
        candidate_skill.rmdir()
        candidate_skill.parent.rmdir()
        candidate_skill.parent.parent.rmdir()
        outside_docs = self.base / "outside-docs"
        external_skill = outside_docs / "skills" / "cv-paper-workflow"
        external_skill.mkdir(parents=True)
        (external_skill / "SKILL.md").write_text(
            "# external candidate skill\n",
            encoding="utf-8",
        )
        try:
            (self.paperflow / "docs").symlink_to(
                outside_docs,
                target_is_directory=True,
            )
        except OSError as error:
            self.skipTest(f"symlink unavailable: {error}")
        self._write_test(
            self.paperflow,
            "tests_v2",
            "def test_required_passes():\n    assert True\n",
        )
        group = module.build_default_groups(
            research_root=self.research,
            paperflow_root=self.paperflow,
            paperflow_library_root=self.paperflow_library,
            hf_home=self.hf_home,
            cuda_python=self.cuda_python,
            research_python=Path(sys.executable),
            paperflow_python=Path(sys.executable),
            research_timeout=30.0,
            paperflow_timeout=30.0,
            installed_skills_root=self.installed_skills,
        )[1]

        progress: list[str] = []
        with (
            mock.patch("shutil.rmtree") as rmtree,
            mock.patch.object(module.os, "unlink") as unlink,
            mock.patch.object(module.os, "remove") as remove,
            mock.patch.object(module.os, "rmdir") as rmdir,
            mock.patch.object(module.os, "chmod") as chmod,
        ):
            result = module.run_validation_group(
                group,
                progress=progress.append,
            )

        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["executed"])
        self.assertEqual(result["sandbox"]["cleanup"], "NOT_STARTED")
        self.assertIsNone(result["sandbox"]["root"])
        self.assertEqual(result["source_snapshot"]["status"], "ERROR")
        self.assertTrue(any(line.startswith("[FAIL]") for line in progress))
        self.assertRegex(result["detail"], "link|reparse|候选项目")
        for mocked in (rmtree, unlink, remove, rmdir, chmod):
            mocked.assert_not_called()

    def test_early_setup_failure_reports_unverified_retained_root_without_delete(
        self,
    ) -> None:
        module = load_tool()
        group = module.build_default_groups(
            research_root=self.research,
            paperflow_root=self.paperflow,
            paperflow_library_root=self.paperflow_library,
            hf_home=self.hf_home,
            cuda_python=self.cuda_python,
            research_python=Path(sys.executable),
            paperflow_python=Path(sys.executable),
            research_timeout=30.0,
            paperflow_timeout=30.0,
            installed_skills_root=self.installed_skills,
        )[1]
        created: list[Path] = []
        real_mkdtemp = module.tempfile.mkdtemp

        def capture_mkdtemp(*args, **kwargs) -> str:
            root = Path(real_mkdtemp(*args, **kwargs))
            created.append(root)
            return str(root)

        progress: list[str] = []
        with (
            mock.patch.object(
                module.tempfile,
                "mkdtemp",
                side_effect=capture_mkdtemp,
            ),
            mock.patch.object(
                module.Path,
                "write_text",
                side_effect=PermissionError("owner marker denied"),
            ),
            mock.patch("shutil.rmtree") as rmtree,
            mock.patch.object(module.os, "unlink") as unlink,
            mock.patch.object(module.os, "remove") as remove,
            mock.patch.object(module.os, "rmdir") as rmdir,
            mock.patch.object(module.os, "chmod") as chmod,
        ):
            result = module.run_validation_group(
                group,
                progress=progress.append,
            )

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(len(created), 1)
        retained = created[0]
        self.assertEqual(Path(result["sandbox"]["root"]), retained)
        self.assertEqual(
            result["sandbox"]["cleanup"],
            "retained_unverified_setup_failure",
        )
        self.assertTrue(retained.is_dir())
        self.assertFalse((retained / ".owner").exists())
        self.assertTrue(
            any(
                line.startswith("[WARN]")
                and "retained_unverified_setup_failure" in line
                and str(retained) in line
                for line in progress
            ),
            progress,
        )
        for mocked in (rmtree, unlink, remove, rmdir, chmod):
            mocked.assert_not_called()

    def test_process_start_failure_reports_verified_retention_without_delete(
        self,
    ) -> None:
        module = load_tool()
        missing_python = self.base / "missing-python.exe"
        group = module.ValidationGroup(
            group_id="start-failure",
            cwd=self.research,
            command=module._pytest_command(missing_python, "tests"),
            timeout_seconds=30.0,
            basetemp_policy=module.SHORT_BASETEMP_POLICY,
        )
        progress: list[str] = []
        with (
            mock.patch("shutil.rmtree") as rmtree,
            mock.patch.object(module.os, "unlink") as unlink,
            mock.patch.object(module.os, "remove") as remove,
            mock.patch.object(module.os, "rmdir") as rmdir,
            mock.patch.object(module.os, "chmod") as chmod,
        ):
            result = module.run_validation_group(
                group,
                progress=progress.append,
            )

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(
            result["sandbox"]["cleanup"],
            "retained_by_windows_safety_policy",
        )
        retained = Path(result["sandbox"]["root"])
        self.assertTrue(retained.is_dir())
        self.assertTrue((retained / ".owner").is_file())
        self.assertTrue(
            any(
                line.startswith("[WARN]")
                and "retained_by_windows_safety_policy" in line
                and str(retained) in line
                for line in progress
            ),
            progress,
        )
        for mocked in (rmtree, unlink, remove, rmdir, chmod):
            mocked.assert_not_called()

    def test_process_start_retention_check_failure_has_explicit_status_and_warn(
        self,
    ) -> None:
        module = load_tool()
        missing_python = self.base / "missing-python.exe"
        group = module.ValidationGroup(
            group_id="retention-check-failure",
            cwd=self.research,
            command=module._pytest_command(missing_python, "tests"),
            timeout_seconds=30.0,
            basetemp_policy=module.SHORT_BASETEMP_POLICY,
        )
        progress: list[str] = []
        with mock.patch.object(
            module,
            "_retain_execution_sandbox",
            side_effect=RuntimeError("simulated identity change"),
        ):
            result = module.run_validation_group(
                group,
                progress=progress.append,
            )

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(
            result["sandbox"]["cleanup"],
            "retention_verification_failed",
        )
        retained = Path(result["sandbox"]["root"])
        self.assertTrue(retained.is_dir())
        self.assertTrue((retained / ".owner").is_file())
        self.assertTrue(
            any(
                line.startswith("[WARN]")
                and "retention_verification_failed" in line
                and str(retained) in line
                for line in progress
            ),
            progress,
        )
        self.assertIn("simulated identity change", result["detail"])

    def test_paperflow_uses_short_temp_and_candidate_skill_sandbox(
        self,
    ) -> None:
        module = load_tool()
        package = self.paperflow / "paperflow_v2"
        package.mkdir()
        (package / "__init__.py").write_text("VALUE = 17\n", encoding="utf-8")
        self._write_test(
            self.paperflow,
            "tests_v2",
            (
                "from pathlib import Path\n"
                "from paperflow_v2 import VALUE\n\n"
                "def test_candidate_environment(tmp_path):\n"
                "    project = Path(__file__).resolve().parents[1]\n"
                "    installed = Path.home() / '.codex' / 'skills'\n"
                "    assert VALUE == 17\n"
                "    assert (installed / 'cv-paper-workflow' / 'SKILL.md').read_bytes() == (\n"
                "        project / 'docs' / 'skills' / 'cv-paper-workflow' / 'SKILL.md'\n"
                "    ).read_bytes()\n"
                "    assert (installed / 'cv-paper-ingest' / 'SKILL.md').is_file()\n"
                "    assert not (installed / 'superpowers').exists()\n"
                "    assert len(str(tmp_path)) < 120\n"
            ),
        )
        group = module.build_default_groups(
            research_root=self.research,
            paperflow_root=self.paperflow,
            paperflow_library_root=self.paperflow_library,
            hf_home=self.hf_home,
            cuda_python=self.cuda_python,
            research_python=Path(sys.executable),
            paperflow_python=Path(sys.executable),
            research_timeout=30.0,
            paperflow_timeout=30.0,
            installed_skills_root=self.installed_skills,
        )[1]

        progress: list[str] = []
        report = module.validate_release(
            groups=[group],
            json_out=self.output_parent / "paperflow-sandbox.json",
            forbidden_roots=(self.research, self.paperflow),
            progress=progress.append,
        )

        self.assertTrue(report["ok"], report)
        result = report["groups"][0]
        self.assertEqual(result["sandbox"]["basetemp_policy"], "short-volume-root")
        self.assertEqual(result["sandbox"]["skill_mode"], "candidate-skill-sandbox")
        self.assertEqual(
            result["sandbox"]["cleanup"],
            "retained_by_windows_safety_policy",
        )
        retained = Path(result["sandbox"]["root"])
        self.assertTrue(retained.is_dir())
        self.assertTrue((retained / ".owner").is_file())
        self.assertTrue(
            any(
                line.startswith("[WARN]")
                and "retained_by_windows_safety_policy" in line
                and str(retained) in line
                for line in progress
            ),
            progress,
        )
        self.assertIn("--basetemp", result["command"])

    def test_windows_safety_policy_retains_sandbox_without_destructive_calls(
        self,
    ) -> None:
        module = load_tool()
        sandbox = self._make_cleanup_sandbox(module, "retain")
        payload = sandbox.basetemp / "payload.json"
        payload.write_text("{}\n", encoding="utf-8")
        before_marker = sandbox.marker.read_bytes()
        before_payload = payload.read_bytes()
        with (
            mock.patch("shutil.rmtree") as rmtree,
            mock.patch.object(module.os, "unlink") as unlink,
            mock.patch.object(module.os, "remove") as remove,
            mock.patch.object(module.os, "rmdir") as rmdir,
            mock.patch.object(module.os, "chmod") as chmod,
        ):
            disposition = module._retain_execution_sandbox(sandbox)

        self.assertEqual(
            disposition,
            "retained_by_windows_safety_policy",
        )
        rmtree.assert_not_called()
        unlink.assert_not_called()
        remove.assert_not_called()
        rmdir.assert_not_called()
        chmod.assert_not_called()
        self.assertTrue(sandbox.root.is_dir())
        self.assertEqual(sandbox.marker.read_bytes(), before_marker)
        self.assertEqual(payload.read_bytes(), before_payload)

    def test_root_swap_and_partial_delete_mocks_are_never_entered(
        self,
    ) -> None:
        module = load_tool()
        sandbox = self._make_cleanup_sandbox(module, "swap")
        payload = sandbox.basetemp / "payload.json"
        payload.write_text("owned payload\n", encoding="utf-8")

        def swap_root_if_called(*_args, **_kwargs):
            moved = sandbox.root.with_name(f"{sandbox.root.name}-moved")
            sandbox.root.rename(moved)
            victim = sandbox.root
            victim.mkdir()
            (victim / "victim.txt").write_text(
                "must survive\n",
                encoding="utf-8",
            )
            raise AssertionError("destructive root swap path was entered")

        def delete_marker_if_called(*_args, **_kwargs):
            sandbox.marker.unlink()
            raise AssertionError("partial-delete path was entered")

        with (
            mock.patch("shutil.rmtree", side_effect=swap_root_if_called) as rmtree,
            mock.patch.object(
                module.os,
                "unlink",
                side_effect=delete_marker_if_called,
            ) as unlink,
            mock.patch.object(module.os, "remove") as remove,
            mock.patch.object(module.os, "rmdir") as rmdir,
            mock.patch.object(module.os, "chmod") as chmod,
        ):
            disposition = module._retain_execution_sandbox(sandbox)

        self.assertEqual(
            disposition,
            "retained_by_windows_safety_policy",
        )
        rmtree.assert_not_called()
        unlink.assert_not_called()
        remove.assert_not_called()
        rmdir.assert_not_called()
        chmod.assert_not_called()
        self.assertTrue(sandbox.root.is_dir())
        self.assertEqual(
            sandbox.marker.read_text(encoding="ascii"),
            sandbox.owner_token,
        )
        self.assertEqual(payload.read_text(encoding="utf-8"), "owned payload\n")

    def test_retention_rejects_marker_or_root_identity_change_without_delete(
        self,
    ) -> None:
        module = load_tool()
        sandbox = self._make_cleanup_sandbox(module, "ownership")
        sandbox.marker.write_text("b" * 48, encoding="ascii")
        destructive = (
            mock.patch("shutil.rmtree"),
            mock.patch.object(module.os, "unlink"),
            mock.patch.object(module.os, "remove"),
            mock.patch.object(module.os, "rmdir"),
            mock.patch.object(module.os, "chmod"),
        )
        with (
            destructive[0] as rmtree,
            destructive[1] as unlink,
            destructive[2] as remove,
            destructive[3] as rmdir,
            destructive[4] as chmod,
        ):
            with self.assertRaisesRegex(RuntimeError, "所有权|ownership"):
                module._retain_execution_sandbox(sandbox)
        for mocked in (rmtree, unlink, remove, rmdir, chmod):
            mocked.assert_not_called()
        self.assertTrue(sandbox.root.is_dir())

        sandbox.marker.write_text(sandbox.owner_token, encoding="ascii")
        wrong_identity = module._ExecutionSandbox(
            root=sandbox.root,
            anchor=sandbox.anchor,
            identity=(sandbox.identity[0], sandbox.identity[1] + 1),
            owner_token=sandbox.owner_token,
            marker=sandbox.marker,
            basetemp=sandbox.basetemp,
            home=sandbox.home,
            environment=sandbox.environment,
            command=sandbox.command,
            skill_mode=sandbox.skill_mode,
        )
        with mock.patch("shutil.rmtree") as rmtree:
            with self.assertRaisesRegex(RuntimeError, "身份|identity"):
                module._retain_execution_sandbox(wrong_identity)
        rmtree.assert_not_called()
        self.assertTrue(sandbox.root.is_dir())

    def test_timeout_fails_and_owned_process_tree_is_reclaimed(self) -> None:
        module = load_tool()
        group = module.ValidationGroup(
            group_id="slow",
            cwd=self.base,
            command=(
                sys.executable,
                "-B",
                "-c",
                "import time; print('started', flush=True); time.sleep(5)",
            ),
            timeout_seconds=0.2,
        )

        result = module.run_validation_group(group)

        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(result["executed"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["process_tree_cleanup"], "PASS")
        self.assertLess(result["duration_seconds"], 5)

    def test_progress_is_streamed_before_group_finishes(self) -> None:
        module = load_tool()
        observed = threading.Event()
        finished = threading.Event()
        group = module.ValidationGroup(
            group_id="stream",
            cwd=self.base,
            command=(
                sys.executable,
                "-B",
                "-c",
                "import time; print('ready'); time.sleep(1.5)",
            ),
            timeout_seconds=5.0,
        )

        def progress(message: str) -> None:
            if message == "[stream] ready":
                observed.set()

        def run() -> None:
            try:
                module.run_validation_group(group, progress=progress)
            finally:
                finished.set()

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(observed.wait(timeout=0.75))
            self.assertFalse(finished.is_set())
        finally:
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())

    def test_output_inside_source_root_is_rejected_before_command_runs(self) -> None:
        module = load_tool()
        sentinel = self.base / "must-not-run.txt"
        group = module.ValidationGroup(
            group_id="sentinel",
            cwd=self.base,
            command=(
                sys.executable,
                "-B",
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(sentinel)!r}).write_text('ran', encoding='utf-8')"
                ),
            ),
            timeout_seconds=10.0,
        )

        with self.assertRaisesRegex(ValueError, "源码|受保护"):
            module.validate_release(
                groups=[group],
                json_out=self.research / "validation-summary.json",
                forbidden_roots=(self.research, self.paperflow),
            )

        self.assertFalse(sentinel.exists())

    def test_tool_contains_no_power_control_command(self) -> None:
        source = TOOL.read_text(encoding="utf-8").casefold()
        forbidden = (
            "stop-computer",
            "restart-computer",
            "exitwindows",
            "init 0",
            "systemctl poweroff",
        )
        self.assertFalse(any(token in source for token in forbidden))
        self.assertIsNone(re.search(r"\bshutdown(?:\.exe)?\b", source))


if __name__ == "__main__":
    unittest.main()
