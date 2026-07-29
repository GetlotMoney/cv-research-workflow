from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "build_release_staging.py"


def _windows_private_path(*parts: str, slash: str = "\\") -> str:
    return "C:" + slash + slash.join(parts)


def _posix_private_path(*parts: str) -> str:
    return "/" + "/".join(parts)


def _unc_private_path(*parts: str) -> str:
    separator = chr(92)
    return separator * 2 + separator.join(parts)


def load_tool():
    if not TOOL.is_file():
        raise AssertionError("发布暂存构建工具尚未实现")
    spec = importlib.util.spec_from_file_location("build_release_staging", TOOL)
    if spec is None or spec.loader is None:
        raise AssertionError("无法加载发布暂存构建工具")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ReleaseStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name) / "带 空格和中文"
        self.base.mkdir(parents=True)
        self.research = self.base / "科研源码"
        self.paperflow = self.base / "论文源码"
        self._init_repository(
            self.research,
            {
                "README.md": "# 科研工作流\n",
                "pyproject.toml": (
                    '[project]\nname = "fixture"\nversion = "1.0.0"\n'
                    '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
                ),
                "skills/cv-experiment-workflow/SKILL.md": "# Skill\n",
                "tools/helper.py": "print('research')\n",
                "tests/test_ok.py": "def test_ok():\n    assert True\n",
                "docs/guide.md": "# 使用说明\n",
            },
        )
        self._init_repository(
            self.paperflow,
            {
                "README.md": "# PaperFlow\n",
                "SECURITY.md": "用户专属安全说明，不得进入通用候选包\n",
                "pytest.ini": "[pytest]\ntestpaths = tests_v2\n",
                "paperflow_v2/app.py": "APP_NAME = 'paperflow'\n",
                "tools/paperflow.ps1": "Write-Output 'paperflow'\n",
                "tests_v2/test_ok.py": "def test_ok():\n    assert True\n",
                "docs/guide.md": "# 写作说明\n",
                "data/model_configs/models.json": '{"models": []}\n',
                "data/project_facts/private.json": '{"secret": "excluded"}\n',
                "output/playwright/screenshot.png": "excluded output\n",
            },
        )

    def _git(self, root: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def _init_repository(self, root: Path, files: dict[str, str]) -> None:
        root.mkdir(parents=True)
        for relative, content in files.items():
            target = root.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        result = subprocess.run(
            ["git", "init", "-b", "main", str(root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self._git(root, "config", "user.email", "fixture@example.invalid")
        self._git(root, "config", "user.name", "Fixture")
        self._git(root, "add", "--all")
        self._git(root, "commit", "-m", "fixture")

    def _snapshot_repository(self, source: Path, target: Path) -> None:
        target.mkdir(parents=True)
        result = subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))
        paths = [
            item
            for item in result.stdout.decode("utf-8").split("\0")
            if item
        ]
        for relative in paths:
            source_file = source.joinpath(*relative.split("/"))
            if not source_file.is_file():
                continue
            destination = target.joinpath(*relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination)
        result = subprocess.run(
            ["git", "init", "-b", "main", str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self._git(target, "config", "user.email", "fixture@example.invalid")
        self._git(target, "config", "user.name", "Fixture")
        self._git(target, "add", "--all")
        self._git(target, "commit", "-m", "clean candidate snapshot")

    def test_builds_new_clean_staging_from_two_tracked_whitelists(self) -> None:
        module = load_tool()
        output = self.base / "发布 暂存"

        report = module.build_release_staging(
            self.research,
            self.paperflow,
            output,
        )

        self.assertTrue(report["ok"])
        self.assertTrue((output / "research" / "tools" / "helper.py").is_file())
        self.assertTrue(
            (output / "paperflow" / "paperflow_v2" / "app.py").is_file()
        )
        self.assertTrue(
            (output / "paperflow" / "data" / "model_configs" / "models.json").is_file()
        )
        self.assertFalse((output / ".git").exists())
        self.assertFalse((output / "research" / ".git").exists())
        self.assertFalse(
            (output / "paperflow" / "data" / "project_facts" / "private.json").exists()
        )
        self.assertFalse(
            (output / "paperflow" / "output" / "playwright" / "screenshot.png").exists()
        )
        self.assertFalse((output / "paperflow" / "SECURITY.md").exists())
        manifest = json.loads(
            (output / "release-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            "cv-research-paperflow.release-staging.v1",
            manifest["schema"],
        )
        self.assertLessEqual(manifest["payload_bytes"], module.MAX_RELEASE_BYTES)
        self.assertTrue(manifest["files"])
        self.assertEqual(
            ["release-exclusions.json"],
            [item["path"] for item in manifest["control_files"]],
        )
        exclusions_bytes = (output / "release-exclusions.json").read_bytes()
        self.assertEqual(len(exclusions_bytes), manifest["control_files"][0]["size"])
        self.assertEqual(
            hashlib.sha256(exclusions_bytes).hexdigest(),
            manifest["control_files"][0]["sha256"],
        )
        self.assertTrue(
            all(not Path(item["path"]).is_absolute() for item in manifest["files"])
        )
        exclusions = json.loads(
            (output / "release-exclusions.json").read_text(encoding="utf-8")
        )
        excluded = {item["path"] for item in exclusions["excluded"]}
        self.assertIn("paperflow/SECURITY.md", excluded)
        self.assertIn("paperflow/data/project_facts/private.json", excluded)
        self.assertIn("paperflow/output/playwright/screenshot.png", excluded)
        self.assertTrue(exclusions["download_urls"])

    def test_hostile_ambient_git_redirection_cannot_change_release_inputs(
        self,
    ) -> None:
        module = load_tool()
        attacker = self.base / "attacker-repository"
        self._init_repository(
            attacker,
            {"attacker-only.md": "must never enter the release\n"},
        )
        output = self.base / "ambient-git-safe-release"
        hostile = {
            "GIT_DIR": str(attacker / ".git"),
            "GIT_WORK_TREE": str(self.research),
            "GIT_INDEX_FILE": str(attacker / ".git" / "index"),
            "GIT_OBJECT_DIRECTORY": str(attacker / ".git" / "objects"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.worktree",
            "GIT_CONFIG_VALUE_0": str(self.research),
        }

        with mock.patch.dict(os.environ, hostile, clear=False):
            report = module.build_release_staging(
                self.research,
                self.paperflow,
                output,
            )

        self.assertTrue(report["ok"], report)
        self.assertTrue((output / "research" / "README.md").is_file())
        self.assertTrue(
            (output / "paperflow" / "paperflow_v2" / "app.py").is_file()
        )
        self.assertFalse(
            (output / "research" / "attacker-only.md").exists()
        )
        safe = module.safe_git_environment()
        for key in hostile:
            if key.startswith("GIT_"):
                self.assertNotEqual(safe.get(key), hostile[key])

    def test_existing_destination_is_never_overwritten(self) -> None:
        module = load_tool()
        output = self.base / "already-exists"
        output.mkdir()
        marker = output / "keep.txt"
        marker.write_text("用户文件", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "已存在|覆盖"):
            module.build_release_staging(self.research, self.paperflow, output)

        self.assertEqual("用户文件", marker.read_text(encoding="utf-8"))

    def test_protected_roots_are_explicit_and_rejected_without_writing(self) -> None:
        module = load_tool()
        protected = self.base / "旧 sample_project"
        target = protected / "release-staging"

        self.assertFalse(hasattr(module, "PROTECTED_ROOTS"))
        self.assertEqual(
            target.resolve(strict=False),
            module.validate_output_destination(
                target,
                self.research,
                self.paperflow,
            ),
        )
        with self.assertRaisesRegex(ValueError, "受保护"):
            module.validate_output_destination(
                target,
                self.research,
                self.paperflow,
                protected_roots=(protected,),
            )
        arguments = module._parser().parse_args(
            [
                "--research-root",
                str(self.research),
                "--paperflow-root",
                str(self.paperflow),
                "--out",
                str(self.base / "release"),
                "--protected-root",
                str(protected),
                "--protected-root",
                str(self.base / "旧 sample_project 2"),
            ]
        )
        self.assertEqual(len(arguments.protected_root), 2)

    def test_unknown_untracked_file_inside_included_area_fails_closed(self) -> None:
        module = load_tool()
        unknown = self.research / "tools" / "surprise.xyz"
        unknown.write_text("unknown", encoding="utf-8")
        output = self.base / "unknown-output"

        with self.assertRaisesRegex(ValueError, "未知|未跟踪"):
            module.build_release_staging(self.research, self.paperflow, output)

        self.assertFalse(output.exists())

    def test_absolute_source_path_leak_in_production_file_is_rejected(self) -> None:
        module = load_tool()
        leak = self.research / "tools" / "leak.py"
        leak.write_text(
            f"SOURCE_MACHINE_PATH = {_windows_private_path('Users', 'alice', 'private', 'source.py')!r}\n",
            encoding="utf-8",
        )
        self._git(self.research, "add", "tools/leak.py")
        self._git(self.research, "commit", "-m", "add leak")
        output = self.base / "leak-output"

        with self.assertRaisesRegex(ValueError, "绝对路径|泄漏"):
            module.build_release_staging(self.research, self.paperflow, output)

        self.assertFalse(output.exists())

    def test_synthetic_private_paths_in_tests_are_portable_fixtures(self) -> None:
        module = load_tool()
        fixture = self.research / "tests" / "test_private_paths.py"
        fixture.write_text(
            (
                "WINDOWS = 'C:\\\\Users\\\\alice\\\\private\\\\source.py'\n"
                "UNC = '\\\\\\\\server\\\\share\\\\fixture.txt'\n"
                "POSIX = '/root/private/fixture.txt'\n"
                "def test_fixture():\n"
                "    assert WINDOWS and UNC and POSIX\n"
            ),
            encoding="utf-8",
        )
        self._git(self.research, "add", "tests/test_private_paths.py")
        self._git(self.research, "commit", "-m", "add portable path fixtures")
        output = self.base / "portable-test-fixtures"

        report = module.build_release_staging(
            self.research,
            self.paperflow,
            output,
        )

        self.assertTrue(report["ok"])
        self.assertTrue(
            (output / "research" / "tests" / "test_private_paths.py").is_file()
        )

    def test_real_workspace_path_in_test_fixture_is_rejected(self) -> None:
        module = load_tool()
        fixture = self.paperflow / "tests_v2" / "fixtures" / "README.md"
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(
            f'SOURCE = "{self.base / "private-review-archive"}"\n',
            encoding="utf-8",
        )
        self._git(self.paperflow, "add", "tests_v2/fixtures/README.md")
        self._git(self.paperflow, "commit", "-m", "add accidental workspace leak")

        with self.assertRaisesRegex(ValueError, "机器绝对路径泄漏"):
            module.build_release_staging(
                self.research,
                self.paperflow,
                self.base / "must-not-publish-workspace-leak",
            )

    def test_portable_parser_literals_do_not_waive_other_private_paths(self) -> None:
        module = load_tool()
        relative = "paperflow_v2/parsers.py"
        escaped_separator = chr(92) * 2
        parser_runtime = (
            "C:"
            + escaped_separator
            + escaped_separator.join(
                (
                    "Users",
                    "Administrator",
                    ".obsolete-runtime",
                    "venv",
                    "Scripts",
                    "python.exe",
                )
            )
        )
        known = 'FONT = Path("C:/Windows/Fonts/msyh.ttc")\n'
        self.assertFalse(
            module.content_contains_private_path(
                known,
                component="paperflow",
                relative=relative,
                private_roots=(),
            )
        )
        self.assertTrue(
            module.content_contains_private_path(
                f'"use {parser_runtime}"\n',
                component="paperflow",
                relative=relative,
                private_roots=(),
            )
        )
        self.assertTrue(
            module.content_contains_private_path(
                known + "LEAK = 'D:\\\\secret\\\\private.bin'\n",
                component="paperflow",
                relative=relative,
                private_roots=(),
            )
        )
        for suffix in (
            ".bak",
            ":private",
            ",private",
            ";private",
            ")private",
            escaped_separator
            + escaped_separator.join(("..", "..", "private.txt")),
        ):
            with self.subTest(suffix=suffix):
                self.assertTrue(
                    module.content_contains_private_path(
                        f'"use {parser_runtime}{suffix}"\n',
                        component="paperflow",
                        relative=relative,
                        private_roots=(),
                    )
                )
        for extended_font in (
            "C:/Windows/Fonts/msyh.ttc.bak",
            "C:/Windows/Fonts/arial.ttf/child",
        ):
            with self.subTest(extended_font=extended_font):
                self.assertTrue(
                    module.content_contains_private_path(
                        f'FONT = Path("{extended_font}")\n',
                        component="paperflow",
                        relative=relative,
                        private_roots=(),
                    )
                )
        for device_marker in ("?", "."):
            source_device_prefix = (
                escaped_separator * 2
                + device_marker
                + escaped_separator
            )
            with self.subTest(device_marker=device_marker):
                self.assertTrue(
                    module.content_contains_private_path(
                        f'P = "{source_device_prefix}{parser_runtime}"\n',
                        component="paperflow",
                        relative=relative,
                        private_roots=(),
                    )
                )

    def test_paperflow_integration_path_examples_are_narrowly_portable(
        self,
    ) -> None:
        module = load_tool()
        relative = "docs/integration/RESEARCH_PACKAGE_V2_IMPORT.md"
        examples = "\n".join(
            (
                "`//server/share` and `//?/C:/...` are rejected examples.",
                r"`C:\Users\...`, `D:\...`, and `\\server\share`.",
                "`q:/an` is a documented benign short fragment.",
                r'--paper-workspace "D:\papers\my-paper"',
                r'--package "D:\deliveries\PKG-0001"',
                r'--root "D:\paperflow-runtime"',
            )
        )
        self.assertFalse(
            module.content_contains_private_path(
                examples,
                component="paperflow",
                relative=relative,
                private_roots=(),
            )
        )
        self.assertTrue(
            module.content_contains_private_path(
                examples + "\nLEAK=D:\\other-machine\\private.txt\n",
                component="paperflow",
                relative=relative,
                private_roots=(),
            )
        )

    def test_portable_document_examples_do_not_waive_other_private_paths(
        self,
    ) -> None:
        module = load_tool()
        placeholder = "run --root D:\\\\path\\\\to\\\\your-research-project\n"
        self.assertFalse(
            module.content_contains_private_path(
                placeholder,
                component="research",
                relative="README.md",
                private_roots=(),
            )
        )
        self.assertTrue(
            module.content_contains_private_path(
                placeholder + "LEAK = 'D:\\\\secret\\\\private.bin'\n",
                component="research",
                relative="README.md",
                private_roots=(),
            )
        )

    def test_exact_source_home_and_explicit_private_roots_still_fail_in_tests(
        self,
    ) -> None:
        module = load_tool()
        protected = self.base / "private legacy"
        markers = {
            "source": str(self.research),
            "home": str(Path.home()),
            "protected": str(protected),
        }
        for name, marker in markers.items():
            escaped_marker = marker.replace("\\", "\\\\")
            self.assertTrue(
                module.content_contains_private_path(
                    f"# ESCAPED_LEAK={escaped_marker}\n",
                    component="research",
                    relative=f"tests/test_{name}_escaped.py",
                    private_roots=(
                        self.research,
                        protected,
                    ),
                ),
                f"双反斜杠形式也必须拦截：{name}",
            )
            with self.subTest(name=name):
                fixture = self.research / "tests" / f"test_{name}_marker.py"
                fixture.write_text(
                    f"# LEAK={marker}\ndef test_marker(): assert True\n",
                    encoding="utf-8",
                )
                self._git(
                    self.research,
                    "add",
                    f"tests/{fixture.name}",
                )
                self._git(self.research, "commit", "-m", f"add {name} marker")
                output = self.base / f"reject-{name}"
                with self.assertRaisesRegex(ValueError, "绝对路径|泄漏"):
                    module.build_release_staging(
                        self.research,
                        self.paperflow,
                        output,
                        protected_roots=(protected,),
                    )
                self.assertFalse(output.exists())
                fixture.unlink()
                self._git(self.research, "add", "-u")
                self._git(self.research, "commit", "-m", f"remove {name} marker")

    def test_historical_machine_and_local_only_materials_are_recorded_exclusions(
        self,
    ) -> None:
        module = load_tool()
        historical = {
            "docs/reviews/old-review.md": "D:\\\\audit\\\\result\n",
            "docs/superpowers/plans/old-plan.md": "D:\\\\plan\\\\draft\n",
            "docs/workflow_audit/render.json": '{"root": "D:\\\\\\\\render"}\n',
            "docs/diagrams/audit/browser.json": '{"root": "D:\\\\\\\\browser"}\n',
            "docs/deployment/local-machine.md": "H:\\\\runtime\\\\model\n",
            "docs/TECH_STACK_HISTORY.md": "C:\\\\Users\\\\local\\\\history\n",
            "tools/start_unlimited_ocr_model_download.ps1": (
                "Write-Output 'H:\\\\models'\n"
            ),
            "tools/build_bilingual_paper_pdfs.py": (
                "SOURCE = 'C:\\\\Users\\\\local\\\\paper.pdf'\n"
            ),
            "tests/test_legacy.py": (
                "LEGACY = 'D:\\\\legacy\\\\paperflow.py'\n"
            ),
        }
        for relative, content in historical.items():
            target = self.paperflow.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self._git(self.paperflow, "add", "--all")
        self._git(self.paperflow, "commit", "-m", "add excluded history")
        output = self.base / "excluded-history"

        module.build_release_staging(self.research, self.paperflow, output)

        exclusions = json.loads(
            (output / "release-exclusions.json").read_text(encoding="utf-8")
        )
        records = {item["path"]: item["reason"] for item in exclusions["excluded"]}
        for relative in historical:
            release_path = f"paperflow/{relative}"
            self.assertFalse(output.joinpath(*release_path.split("/")).exists())
            self.assertIn(release_path, records)
            self.assertRegex(records[release_path], "历史|旧版|本机|部署|审核|审计")

    def test_absolute_path_scanner_covers_windows_unc_and_private_posix(self) -> None:
        module = load_tool()
        separator = chr(92)
        rejected = (
            _windows_private_path("Users", "alice", "source.py"),
            _windows_private_path("Users", "alice", "source.py", slash="/"),
            _unc_private_path("server", "share", "source.py"),
            "/" * 2 + "/".join(("server", "share", "source.py")),
            separator * 2
            + "?"
            + separator
            + _windows_private_path("Users", "alice", "source.py"),
            separator * 2 + "." + separator + separator.join(("PIPE", "private")),
            _posix_private_path("home", "alice", "source.py"),
            _posix_private_path("Users", "alice", "source.py"),
            _posix_private_path("root", "source.py"),
            _posix_private_path("var", "lib", "private", "source.py"),
            _posix_private_path("opt", "private", "source.py"),
            _posix_private_path("mnt", "c", "Users", "alice", "source.py"),
        )
        for value in rejected:
            with self.subTest(value=value):
                self.assertTrue(module.contains_private_absolute_path(value))
        for value in (
            "https://example.org/home/alice",
            "https://server/share/source.py",
            "relative/path/source.py",
        ):
            with self.subTest(value=value):
                self.assertFalse(module.contains_private_absolute_path(value))

    def test_windows_reserved_names_case_collisions_and_parent_escape_are_rejected(
        self,
    ) -> None:
        module = load_tool()
        bad_sets = [
            ["tools/CON.py"],
            ["tests/Case.py", "tests/case.py"],
            ["../escape.py"],
            ["tools/name. /x.py"],
            [_windows_private_path("private", "file.py", slash="/")],
        ]
        for paths in bad_sets:
            with self.subTest(paths=paths):
                with self.assertRaises(ValueError):
                    module.validate_release_paths(paths)

    def test_clean_snapshot_of_real_repository_builds_without_mock_waivers(
        self,
    ) -> None:
        module = load_tool()
        research_snapshot = self.base / "真实科研干净快照"
        self._snapshot_repository(ROOT, research_snapshot)
        output = self.base / "真实科研仓库发布暂存"
        report = module.build_release_staging(
            research_snapshot,
            self.paperflow,
            output,
        )

        self.assertTrue(report["ok"], report)
        self.assertTrue((output / "research" / "tests" / "test_release_staging.py").is_file())
        self.assertTrue(
            (output / "research" / "tools" / "run_full_flow_smoke.py").is_file()
        )
        self.assertFalse((output / "research" / "docs" / "reviews").exists())
        self.assertFalse(
            (output / "research" / "docs" / "superpowers" / "plans").exists()
        )
        audit_only = ("research/tools/build_audit_package.py",)
        exclusions = json.loads(
            (output / "release-exclusions.json").read_text(encoding="utf-8")
        )
        excluded_paths = {item["path"] for item in exclusions["excluded"]}
        for relative in audit_only:
            self.assertFalse(output.joinpath(*relative.split("/")).exists())
            self.assertIn(relative, excluded_paths)

    def test_reparse_flag_is_detected_even_if_real_symlink_cannot_be_created(
        self,
    ) -> None:
        module = load_tool()
        fake = SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
        )
        self.assertTrue(module.has_reparse_flag(fake))

        target = self.base / "link-target"
        target.mkdir()
        link = self.base / "source-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            # Windows 未启用开发者模式时创建符号链接会失败；上面的真实
            # FILE_ATTRIBUTE_REPARSE_POINT 分支仍必须被执行并断言，不能跳过。
            self.assertTrue(module.has_reparse_flag(fake))
        else:
            self.assertTrue(module.is_link_or_reparse(link))

    def test_size_limit_is_exactly_fifty_mebibytes_and_fails_before_publish(
        self,
    ) -> None:
        module = load_tool()
        self.assertEqual(50 * 1024 * 1024, module.MAX_RELEASE_BYTES)
        with self.assertRaisesRegex(ValueError, "50|体积"):
            module.enforce_size_limit(
                module.MAX_RELEASE_BYTES + 1,
                module.MAX_RELEASE_BYTES,
            )
        output = self.base / "oversize-output"
        with self.assertRaisesRegex(ValueError, "上限|体积"):
            module.build_release_staging(
                self.research,
                self.paperflow,
                output,
                size_limit=16,
            )
        self.assertFalse(output.exists())

    def test_failed_build_retains_staging_without_any_recursive_cleanup(
        self,
    ) -> None:
        module = load_tool()
        output = self.base / "failed-build-output"

        with (
            mock.patch.object(
                module,
                "_ordinary_tree_state",
                side_effect=ValueError("模拟发布前目录身份变化"),
            ),
            mock.patch(
                "shutil.rmtree",
                side_effect=AssertionError("失败暂存不得递归删除"),
            ) as rmtree,
            mock.patch.object(
                module.os,
                "unlink",
                side_effect=AssertionError("失败暂存不得 unlink"),
            ) as unlink,
            mock.patch.object(
                module.os,
                "remove",
                side_effect=AssertionError("失败暂存不得 remove"),
            ) as remove,
            mock.patch.object(
                module.os,
                "rmdir",
                side_effect=AssertionError("失败暂存不得 rmdir"),
            ) as rmdir,
            mock.patch.object(
                module.os,
                "chmod",
                side_effect=AssertionError("失败暂存不得 chmod"),
            ) as chmod,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "模拟发布前目录身份变化",
            ) as caught:
                module.build_release_staging(
                    self.research,
                    self.paperflow,
                    output,
                )

        self.assertFalse(output.exists())
        retained = list(self.base.glob(".release-staging-*"))
        self.assertEqual(1, len(retained), retained)
        self.assertTrue(retained[0].is_dir())
        self.assertTrue(
            os.path.samefile(
                retained[0],
                getattr(caught.exception, "retained_staging_path", ""),
            )
        )
        self.assertEqual(
            "retained_failed_release_staging",
            getattr(caught.exception, "retention_status", None),
        )
        self.assertIn(
            "失败暂存已保留",
            getattr(caught.exception, "retention_message", ""),
        )
        for destructive in (rmtree, unlink, remove, rmdir, chmod):
            destructive.assert_not_called()

    def test_failed_build_retention_does_not_resolve_or_follow_swapped_root(
        self,
    ) -> None:
        module = load_tool()
        output = self.base / "swapped-root-output"
        original_resolve = module.Path.resolve
        temporary_resolve_calls = 0

        def guarded_resolve(path: Path, *args, **kwargs):
            nonlocal temporary_resolve_calls
            if path.name.startswith(".release-staging-"):
                temporary_resolve_calls += 1
                raise AssertionError("失败保留不得解析或跟随临时根")
            return original_resolve(path, *args, **kwargs)

        with (
            mock.patch.object(
                module,
                "_ordinary_tree_state",
                side_effect=ValueError("模拟根目录被调包"),
            ),
            mock.patch.object(
                module.Path,
                "resolve",
                new=guarded_resolve,
            ),
            mock.patch(
                "shutil.rmtree",
                side_effect=AssertionError("被调包根不得递归删除"),
            ) as rmtree,
        ):
            with self.assertRaisesRegex(ValueError, "模拟根目录被调包") as caught:
                module.build_release_staging(
                    self.research,
                    self.paperflow,
                    output,
                )

        self.assertEqual(0, temporary_resolve_calls)
        rmtree.assert_not_called()
        self.assertEqual(
            "retained_failed_release_staging",
            getattr(caught.exception, "retention_status", None),
        )
        self.assertTrue(
            Path(caught.exception.retained_staging_path).is_dir()
        )

    def test_atomic_publish_never_replaces_target_created_at_last_moment(
        self,
    ) -> None:
        module = load_tool()
        output = self.base / "last-moment-target"
        marker = output / "keep.txt"
        original_rename = module.os.rename

        def create_target_then_rename(source: Path, destination: Path):
            Path(destination).mkdir()
            marker.write_text("用户目录", encoding="utf-8")
            return original_rename(source, destination)

        with (
            mock.patch.object(
                module.os,
                "rename",
                side_effect=create_target_then_rename,
            ) as rename,
            mock.patch.object(
                module.os,
                "replace",
                side_effect=AssertionError("目录发布不得使用可覆盖的 os.replace"),
            ) as replace,
        ):
            with self.assertRaisesRegex(
                FileExistsError,
                "原子发布|拒绝覆盖|已出现",
            ) as caught:
                module.build_release_staging(
                    self.research,
                    self.paperflow,
                    output,
                )

        rename.assert_called_once()
        replace.assert_not_called()
        self.assertEqual("用户目录", marker.read_text(encoding="utf-8"))
        retained = list(self.base.glob(".release-staging-*"))
        self.assertEqual(1, len(retained), retained)
        self.assertTrue(
            os.path.samefile(
                retained[0],
                caught.exception.retained_staging_path,
            )
        )
        self.assertEqual(
            "retained_failed_release_staging",
            caught.exception.retention_status,
        )

    def test_stable_read_rejects_mutation_during_read(self) -> None:
        module = load_tool()
        target = self.research / "README.md"
        original_fstat = module.os.fstat
        calls = 0

        def racing_fstat(descriptor: int):
            nonlocal calls
            metadata = original_fstat(descriptor)
            calls += 1
            if calls == 1:
                target.write_text("# 已被并发替换\n", encoding="utf-8")
            return metadata

        self.assertTrue(
            hasattr(module, "stable_read_file"),
            "稳定读取 helper 尚未实现",
        )
        with mock.patch.object(module.os, "fstat", side_effect=racing_fstat):
            with self.assertRaisesRegex(ValueError, "读取期间|身份|变化"):
                module.stable_read_file(target)

    def test_stable_read_rejects_oversize_before_loading_content(self) -> None:
        module = load_tool()
        target = self.research / "README.md"

        with mock.patch.object(
            module.os,
            "read",
            side_effect=AssertionError("超限文件不应进入读取循环"),
        ):
            with self.assertRaisesRegex(ValueError, "上限|体积"):
                module.stable_read_file(target, limit=4)

    def test_path_manifest_never_contains_absolute_roots(self) -> None:
        module = load_tool()
        output = self.base / "manifest-output"
        module.build_release_staging(self.research, self.paperflow, output)
        manifest_text = (output / "release-manifest.json").read_text(
            encoding="utf-8"
        )
        exclusions_text = (output / "release-exclusions.json").read_text(
            encoding="utf-8"
        )
        for root in (self.research, self.paperflow, output):
            for marker in {str(root), str(root).replace("\\", "/")}:
                self.assertNotIn(marker, manifest_text)
                self.assertNotIn(marker, exclusions_text)


if __name__ == "__main__":
    unittest.main()
