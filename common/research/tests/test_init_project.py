from __future__ import annotations

import contextlib
import errno
import importlib
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tests._helpers import (
    ROOT,
    SCRIPTS,
    SUBPROCESS_TIMEOUT_SECONDS,
    cli_json,
    read_json,
    run_cli,
)


class InitProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project = Path(self.temp_dir.name) / "尚未创建的项目"

    def test_init_creates_control_plane_without_domain_objects(self) -> None:
        result = cli_json(
            "init", "--path", self.project, "--name", "中文演示项目"
        )

        self.assertTrue(self.project.is_dir())
        control = self.project / ".experiment-workflow"
        self.assertTrue(control.is_dir())
        self.assertFalse((self.project / ".research-workflow").exists())

        for directory in (
            "ideas",
            "trials",
            "code-assets",
            "attempts",
            "versions",
            "agents",
            ".runtime",
            "artifacts",
            "templates",
        ):
            self.assertTrue((control / directory).is_dir(), directory)

        for directory in ("ideas", "trials", "code-assets", "attempts", "versions"):
            self.assertEqual([], list((control / directory).rglob("*.json")), directory)

        project = read_json(control / "project.json")
        self.assertEqual("cv-experiment-workflow.project.v1", project["schema"])
        self.assertEqual("中文演示项目", project["name"])
        self.assertEqual(project, result)
        self.assertEqual(str(uuid.UUID(project["project_id"])), project["project_id"])

        lock = read_json(control / "workflow.lock.json")
        self.assertEqual(
            "cv-experiment-workflow.workflow-lock.v2",
            lock["schema"],
        )
        self.assertEqual("cv-experiment-workflow", lock["skill_id"])
        self.assertEqual("1.5.0", lock["release_version"])
        self.assertEqual("SYS-V2.13.0", lock["system_version"])
        self.assertEqual("sha256-path-bytes-v1", lock["payload"]["algorithm"])
        self.assertRegex(lock["payload"]["digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertGreater(lock["payload"]["file_count"], 0)
        adapter = read_json(control / "adapter.json")
        self.assertEqual("unbound", adapter["status"])
        self.assertEqual([], adapter["code_sources"])
        self.assertEqual({}, adapter["capabilities"])
        self.assertEqual(
            {
                "schema": "cv-experiment-workflow.artifact-index.v1",
                "artifacts": [],
            },
            read_json(control / "artifacts" / "index.json"),
        )

        stdout = run_cli(
            "init",
            "--path",
            self.project / "另一个项目",
            "--name",
            "中文演示项目",
        ).stdout
        self.assertIn("中文演示项目", stdout)

    def test_init_writes_utf8_chinese_guidance_and_gitignore(self) -> None:
        run_cli("init", "--path", self.project, "--name", "演示")

        for filename in ("AGENTS.md", "WORKFLOW.md"):
            raw = (self.project / filename).read_bytes()
            text = raw.decode("utf-8")
            self.assertIn("实验工作流", text)
            self.assertIn("唯一权威", text)
            self.assertIn(".experiment-workflow/project.json", text)
            self.assertRegex(text, r"[\u4e00-\u9fff]")
            self.assertNotIn("code_sources", text)
            self.assertNotIn("capabilities", text)

        ignore = (self.project / ".gitignore").read_text(encoding="utf-8")
        for pattern in (
            ".experiment-workflow.init.lock",
            ".experiment-workflow/.runtime/",
            "data/",
            "runs/",
            "checkpoints/",
            "*.pt",
            "*.pth",
            "*.ckpt",
            "*.pdf",
            ".env",
            "__pycache__/",
        ):
            self.assertIn(pattern, ignore)

    def test_init_refuses_existing_control_plane_without_rewriting(self) -> None:
        run_cli("init", "--path", self.project, "--name", "初始名称")
        project_json = self.project / ".experiment-workflow" / "project.json"
        original = project_json.read_bytes()
        agents = self.project / "AGENTS.md"
        original_agents = agents.read_bytes()

        second = run_cli(
            "init", "--path", self.project, "--name", "不应写入", check=False
        )

        self.assertEqual(2, second.returncode)
        self.assertEqual("", second.stdout)
        self.assertTrue(second.stderr.strip())
        self.assertEqual(original, project_json.read_bytes())
        self.assertEqual(original_agents, agents.read_bytes())

    def test_init_preserves_existing_project_guidance_and_gitignore(self) -> None:
        self.project.mkdir()
        existing = {
            "AGENTS.md": "# 原有协作规则\n\n请保留这条规则。\n",
            "WORKFLOW.md": "# 原有工作流\n\n请保留这段说明。\n",
            ".gitignore": "build/\n",
        }
        for filename, content in existing.items():
            (self.project / filename).write_text(content, encoding="utf-8")

        run_cli("init", "--path", self.project, "--name", "演示")

        for filename, content in existing.items():
            self.assertEqual(
                content,
                (self.project / filename).read_text(encoding="utf-8"),
            )

    def test_unknown_regular_init_lock_is_rejected_without_rewrite(self) -> None:
        self.project.mkdir()
        lock = self.project / ".experiment-workflow.init.lock"
        original = b"user-owned lock file\n"
        lock.write_bytes(original)

        result = run_cli(
            "init", "--path", self.project, "--name", "演示", check=False
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("锁文件", result.stderr)
        self.assertEqual(original, lock.read_bytes())
        self.assertFalse((self.project / ".experiment-workflow").exists())
        self.assertFalse((self.project / "AGENTS.md").exists())

    def test_init_lock_symlink_is_rejected_without_touching_external_file(
        self,
    ) -> None:
        self.project.mkdir()
        external = Path(self.temp_dir.name) / "external-lock"
        original = b"cv-experiment-workflow.init-lock.v1\n"
        external.write_bytes(original)
        timestamp = 1_000_000_000_000_000_000
        os.utime(external, ns=(timestamp, timestamp))
        lock = self.project / ".experiment-workflow.init.lock"
        try:
            lock.symlink_to(external)
        except OSError as error:
            self.skipTest(f"当前环境不支持文件符号链接：{error}")

        result = run_cli(
            "init", "--path", self.project, "--name", "演示", check=False
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("锁文件", result.stderr)
        self.assertTrue(lock.is_symlink())
        self.assertEqual(original, external.read_bytes())
        self.assertEqual(timestamp, external.stat().st_mtime_ns)
        self.assertFalse((self.project / ".experiment-workflow").exists())
        self.assertFalse((self.project / "AGENTS.md").exists())

    def test_lock_bootstrap_hard_exit_is_recoverable(self) -> None:
        script = textwrap.dedent(
            r"""
            import os
            import sys
            from pathlib import Path

            sys.path.insert(0, sys.argv[1])
            import workflow_core.locking as locking
            from workflow_core.project import init_project

            project_root = Path(sys.argv[2])
            mode = sys.argv[3]
            if mode == "before-magic":
                def exit_before_magic(*args, **kwargs):
                    os._exit(94)

                locking.os.fdopen = exit_before_magic
            else:
                real_link = os.link

                def exit_before_publish(
                    source, destination, *, follow_symlinks=True
                ):
                    if Path(destination).name == ".experiment-workflow.init.lock":
                        os._exit(95)
                    return real_link(
                        source,
                        destination,
                        follow_symlinks=follow_symlinks,
                    )

                locking.os.link = exit_before_publish

            init_project(project_root, mode)
            """
        )

        for mode, exit_code in (("before-magic", 94), ("before-publish", 95)):
            with self.subTest(mode=mode):
                project = Path(self.temp_dir.name) / mode
                project.mkdir()
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
                        mode,
                    ],
                    capture_output=True,
                    encoding="utf-8",
                    text=True,
                    check=False,
                    timeout=SUBPROCESS_TIMEOUT_SECONDS,
                )

                self.assertEqual(exit_code, crashed.returncode, crashed.stderr)
                self.assertFalse((project / ".experiment-workflow").exists())
                retry = run_cli(
                    "init", "--path", project, "--name", "重试", check=False
                )
                self.assertEqual(0, retry.returncode, retry.stderr)
                self.assertEqual(
                    b"cv-experiment-workflow.init-lock.v1\n",
                    (project / ".experiment-workflow.init.lock").read_bytes(),
                )
                self.assertEqual(
                    [],
                    list(
                        project.glob(
                            ".experiment-workflow.init.lock.bootstrap-*.tmp"
                        )
                    ),
                )

    def test_lock_bootstrap_symlink_is_preserved_without_touching_external(
        self,
    ) -> None:
        self.project.mkdir()
        external = Path(self.temp_dir.name) / "external-bootstrap"
        original = b"external bootstrap data\n"
        external.write_bytes(original)
        timestamp = 1_000_000_000_000_000_000
        os.utime(external, ns=(timestamp, timestamp))
        bootstrap = self.project / (
            ".experiment-workflow.init.lock.bootstrap-"
            + "a" * 32
            + ".tmp"
        )
        try:
            bootstrap.symlink_to(external)
        except OSError as error:
            self.skipTest(f"当前环境不支持文件符号链接：{error}")

        result = run_cli(
            "init", "--path", self.project, "--name", "演示", check=False
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("bootstrap", result.stderr)
        self.assertTrue(bootstrap.is_symlink())
        self.assertEqual(original, external.read_bytes())
        self.assertEqual(timestamp, external.stat().st_mtime_ns)
        self.assertFalse((self.project / ".experiment-workflow").exists())

    def test_init_success_preserves_existing_hardlinks_and_ctime(self) -> None:
        self.project.mkdir()
        backing_dir = Path(self.temp_dir.name) / "backing"
        backing_dir.mkdir()
        snapshots: dict[str, tuple[Path, bytes, int]] = {}
        for filename in ("AGENTS.md", "WORKFLOW.md", ".gitignore"):
            backing = backing_dir / filename.replace(".", "_")
            content = f"不可变：{filename}\n".encode("utf-8")
            backing.write_bytes(content)
            destination = self.project / filename
            os.link(backing, destination)
            snapshots[filename] = (backing, content, destination.stat().st_ctime_ns)

        run_cli("init", "--path", self.project, "--name", "演示")

        for filename, (backing, content, ctime_ns) in snapshots.items():
            destination = self.project / filename
            self.assertTrue(os.path.samefile(backing, destination), filename)
            self.assertEqual(content, destination.read_bytes(), filename)
            self.assertEqual(ctime_ns, destination.stat().st_ctime_ns, filename)

    def test_hard_exit_during_root_asset_write_never_leaves_partial_or_formal_control(
        self,
    ) -> None:
        self.project.mkdir()
        agents = self.project / "AGENTS.md"
        template = (
            ROOT
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "project"
            / "AGENTS.md"
        ).read_bytes()
        complete_new = template
        script = textwrap.dedent(
            r"""
            import os
            import pathlib
            import sys

            sys.path.insert(0, sys.argv[1])
            from workflow_core import io as workflow_io
            from workflow_core.project import init_project

            project_root = pathlib.Path(sys.argv[2])
            target = project_root / "AGENTS.md"

            class ExitDuringWrite:
                def __init__(self, wrapped):
                    self.wrapped = wrapped

                def __enter__(self):
                    self.wrapped.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.wrapped.__exit__(*args)

                def __getattr__(self, name):
                    return getattr(self.wrapped, name)

                def write(self, content):
                    partial = content[:max(1, len(content) // 2)]
                    self.wrapped.write(partial)
                    self.wrapped.flush()
                    os.fsync(self.wrapped.fileno())
                    os._exit(91)

            real_path_open = pathlib.Path.open

            def crash_direct_write(path, mode="r", *args, **kwargs):
                handle = real_path_open(path, mode, *args, **kwargs)
                file_path = pathlib.Path(path)
                is_target_temp = (
                    file_path.parent == target.parent
                    and file_path.name.startswith(f".{target.name}.cvexp-")
                    and file_path.name.endswith(".tmp")
                )
                if (
                    (file_path == target or is_target_temp)
                    and ("w" in mode or "x" in mode)
                    and "b" in mode
                ):
                    return ExitDuringWrite(handle)
                return handle

            pathlib.Path.open = crash_direct_write
            real_named_temporary_file = workflow_io.tempfile.NamedTemporaryFile

            def crash_temporary_write(*args, **kwargs):
                handle = real_named_temporary_file(*args, **kwargs)
                directory = kwargs.get("dir")
                if (
                    kwargs.get("prefix") == f".{target.name}."
                    and directory is not None
                    and pathlib.Path(directory) == target.parent
                ):
                    return ExitDuringWrite(handle)
                return handle

            workflow_io.tempfile.NamedTemporaryFile = crash_temporary_write
            init_project(project_root, "crash-test")
            """
        )

        crashed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-c",
                script,
                str(SCRIPTS),
                str(self.project),
            ],
            capture_output=True,
            encoding="utf-8",
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )

        self.assertEqual(91, crashed.returncode, crashed.stderr)
        if agents.exists():
            self.assertEqual(complete_new, agents.read_bytes())
        self.assertFalse((self.project / ".experiment-workflow").exists())
        staging = self.project / ".experiment-workflow.staging"
        self.assertTrue(staging.is_dir())

        retry = run_cli(
            "init", "--path", self.project, "--name", "retry", check=False
        )
        self.assertEqual(0, retry.returncode, retry.stderr)
        self.assertEqual(complete_new, agents.read_bytes())
        self.assertTrue((self.project / ".experiment-workflow").is_dir())
        self.assertFalse(staging.exists())
        self.assertFalse(
            (
                self.project
                / (".AGENTS.md.cvexp-" + "1" * 32 + ".tmp")
            ).exists()
        )

    def test_hard_exit_before_staging_marker_is_recoverable(self) -> None:
        self.project.mkdir()
        staging = self.project / ".experiment-workflow.staging"
        script = textwrap.dedent(
            r"""
            import os
            import sys
            from pathlib import Path

            sys.path.insert(0, sys.argv[1])
            import workflow_core.project as project_module

            project_root = Path(sys.argv[2])
            staging = project_root / ".experiment-workflow.staging"
            original_mkdir = Path.mkdir

            def exit_after_staging_creation(
                path, mode=0o777, parents=False, exist_ok=False
            ):
                original_mkdir(
                    path, mode=mode, parents=parents, exist_ok=exist_ok
                )
                if path == staging:
                    os._exit(93)

            Path.mkdir = exit_after_staging_creation
            project_module.init_project(project_root, "pre-marker-crash")
            """
        )

        crashed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-c",
                script,
                str(SCRIPTS),
                str(self.project),
            ],
            capture_output=True,
            encoding="utf-8",
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )

        self.assertEqual(93, crashed.returncode, crashed.stderr)
        self.assertTrue(staging.is_dir())
        self.assertEqual([], list(staging.iterdir()))
        retry = run_cli(
            "init", "--path", self.project, "--name", "retry", check=False
        )
        self.assertEqual(0, retry.returncode, retry.stderr)
        self.assertFalse(staging.exists())
        self.assertTrue((self.project / ".experiment-workflow").is_dir())

    def test_hard_exit_after_final_rename_keeps_completed_control(self) -> None:
        self.project.mkdir()
        script = textwrap.dedent(
            r"""
            import os
            import sys
            from pathlib import Path

            sys.path.insert(0, sys.argv[1])
            import workflow_core.project as project_module

            project_root = Path(sys.argv[2])
            control = project_root / ".experiment-workflow"
            real_replace = os.replace

            def exit_after_commit(source, target):
                source_path = Path(source)
                target_path = Path(target)
                if (
                    source_path.name == ".experiment-workflow.staging"
                    and target_path == control
                ):
                    real_replace(source, target)
                    os._exit(92)
                real_replace(source, target)

            project_module.os.replace = exit_after_commit
            project_module.init_project(project_root, "committed")
            """
        )

        crashed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-c",
                script,
                str(SCRIPTS),
                str(self.project),
            ],
            capture_output=True,
            encoding="utf-8",
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )

        self.assertEqual(92, crashed.returncode, crashed.stderr)
        control = self.project / ".experiment-workflow"
        self.assertTrue((control / "project.json").is_file())
        self.assertTrue(
            (control / ".cv-experiment-workflow-staging").is_file()
        )
        root_temps = [
            self.project / (f".{filename}.cvexp-" + "1" * 32 + ".tmp")
            for filename in ("AGENTS.md", "WORKFLOW.md", ".gitignore")
        ]
        self.assertTrue(all(path.is_file() for path in root_temps))
        retry = run_cli(
            "init", "--path", self.project, "--name", "retry", check=False
        )
        self.assertEqual(2, retry.returncode)
        self.assertIn("已经存在", retry.stderr)
        self.assertTrue((control / "project.json").is_file())
        self.assertTrue(all(not path.exists() for path in root_temps))
        self.assertFalse(
            (control / ".cv-experiment-workflow-staging").exists()
        )

    def test_exception_after_final_rename_treats_marker_as_committed(self) -> None:
        self.project.mkdir()
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        project_module = importlib.import_module("workflow_core.project")
        control = self.project / ".experiment-workflow"
        original_replace = os.replace

        def interrupt_after_commit(source: object, target: object) -> None:
            if (
                Path(source).name == ".experiment-workflow.staging"
                and Path(target) == control
            ):
                original_replace(source, target)
                raise KeyboardInterrupt("interrupt after commit")
            original_replace(source, target)

        with mock.patch.object(
            project_module.os,
            "replace",
            side_effect=interrupt_after_commit,
        ):
            with self.assertRaisesRegex(
                KeyboardInterrupt, "interrupt after commit"
            ):
                project_module.init_project(self.project, "committed")

        self.assertTrue((control / "project.json").is_file())
        self.assertTrue(
            (control / ".cv-experiment-workflow-staging").is_file()
        )
        for filename in ("AGENTS.md", "WORKFLOW.md", ".gitignore"):
            self.assertTrue((self.project / filename).is_file(), filename)

    def test_exception_after_marker_removal_uses_committed_memory_state(self) -> None:
        self.project.mkdir()
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        project_module = importlib.import_module("workflow_core.project")
        control = self.project / ".experiment-workflow"
        marker = control / ".cv-experiment-workflow-staging"
        original_unlink = Path.unlink

        def interrupt_after_marker_removal(
            path: Path, missing_ok: bool = False
        ) -> None:
            original_unlink(path, missing_ok=missing_ok)
            if path == marker:
                raise KeyboardInterrupt("interrupt after marker removal")

        with mock.patch.object(
            Path,
            "unlink",
            new=interrupt_after_marker_removal,
        ):
            with self.assertRaisesRegex(
                KeyboardInterrupt, "interrupt after marker removal"
            ):
                project_module.init_project(self.project, "committed")

        self.assertTrue((control / "project.json").is_file())
        self.assertFalse(marker.exists())
        for filename in ("AGENTS.md", "WORKFLOW.md", ".gitignore"):
            self.assertTrue((self.project / filename).is_file(), filename)

    def test_exception_does_not_follow_control_symlink_marker(self) -> None:
        self.project.mkdir()
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        project_module = importlib.import_module("workflow_core.project")
        control = self.project / ".experiment-workflow"
        staging = self.project / ".experiment-workflow.staging"
        external = Path(self.temp_dir.name) / "external-control"
        external.mkdir()
        probe = self.project / "symlink-probe"
        try:
            probe.symlink_to(external, target_is_directory=True)
            probe.unlink()
        except OSError as error:
            self.skipTest(f"当前环境不支持目录符号链接：{error}")
        original_replace = os.replace
        external_marker_snapshot: list[bytes] = []

        def replace_control_with_symlink_then_fail(
            source: object, target: object
        ) -> None:
            if Path(source) == staging and Path(target) == control:
                marker_content = (
                    staging / ".cv-experiment-workflow-staging"
                ).read_bytes()
                external_marker = (
                    external / ".cv-experiment-workflow-staging"
                )
                external_marker.write_bytes(marker_content)
                external_marker_snapshot.append(marker_content)
                control.symlink_to(external, target_is_directory=True)
                raise OSError("final rename blocked by control symlink")
            original_replace(source, target)

        with mock.patch.object(
            project_module.os,
            "replace",
            side_effect=replace_control_with_symlink_then_fail,
        ):
            with self.assertRaisesRegex(
                OSError, "final rename blocked by control symlink"
            ):
                project_module.init_project(self.project, "演示")

        self.assertTrue(control.is_symlink())
        self.assertEqual(
            external_marker_snapshot[0],
            (external / ".cv-experiment-workflow-staging").read_bytes(),
        )
        for filename in ("AGENTS.md", "WORKFLOW.md", ".gitignore"):
            self.assertFalse((self.project / filename).exists(), filename)
        self.assertFalse(staging.exists())

    def test_init_keeps_unknown_staging_like_directory(self) -> None:
        self.project.mkdir()
        unknown = self.project / (
            ".experiment-workflow.staging-" + "0" * 32
        )
        unknown.mkdir()
        sentinel = unknown / "user-data.txt"
        sentinel.write_text("不得删除", encoding="utf-8")
        unknown_temp = self.project / (
            ".AGENTS.md.cvexp-" + "f" * 32 + ".tmp"
        )
        unknown_temp.write_text("未知临时文件", encoding="utf-8")

        run_cli("init", "--path", self.project, "--name", "演示")

        self.assertEqual("不得删除", sentinel.read_text(encoding="utf-8"))
        self.assertEqual(
            "未知临时文件", unknown_temp.read_text(encoding="utf-8")
        )

    def test_reserved_root_temp_without_owned_marker_is_preserved(self) -> None:
        self.project.mkdir()
        reserved_temp = self.project / (
            ".AGENTS.md.cvexp-" + "1" * 32 + ".tmp"
        )
        original = b"user-owned reserved-looking temp\n"
        reserved_temp.write_bytes(original)
        timestamp = 1_000_000_000_000_000_000
        os.utime(reserved_temp, ns=(timestamp, timestamp))

        result = run_cli(
            "init", "--path", self.project, "--name", "演示", check=False
        )

        self.assertEqual(2, result.returncode)
        self.assertIn(reserved_temp.name, result.stderr)
        self.assertEqual(original, reserved_temp.read_bytes())
        self.assertEqual(timestamp, reserved_temp.stat().st_mtime_ns)
        self.assertFalse((self.project / ".experiment-workflow").exists())
        self.assertFalse((self.project / "AGENTS.md").exists())

    def test_reserved_staging_with_unknown_content_is_rejected_and_listed(
        self,
    ) -> None:
        self.project.mkdir()
        staging = self.project / ".experiment-workflow.staging"
        staging.mkdir()
        sentinel = staging / "user-data.txt"
        sentinel.write_text("不得删除", encoding="utf-8")

        result = run_cli(
            "init", "--path", self.project, "--name", "演示", check=False
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("user-data.txt", result.stderr)
        self.assertEqual("不得删除", sentinel.read_text(encoding="utf-8"))
        self.assertFalse((self.project / ".experiment-workflow").exists())
        self.assertFalse((self.project / "AGENTS.md").exists())

    def test_retry_cleans_predefined_reserved_staging_temp(self) -> None:
        self.project.mkdir()
        staging = self.project / ".experiment-workflow.staging"
        staging.mkdir()
        reserved_temp = staging / (
            ".project.json.cvexp-" + "0" * 32 + ".tmp"
        )
        reserved_temp.write_bytes(b"partial")

        result = run_cli(
            "init", "--path", self.project, "--name", "重试", check=False
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(staging.exists())
        self.assertTrue((self.project / ".experiment-workflow").is_dir())

    def test_concurrent_processes_with_different_temp_directories_share_lock(
        self,
    ) -> None:
        self.project.mkdir()
        coordination = Path(self.temp_dir.name) / "coordination"
        coordination.mkdir()
        process_temp = {
            "holder": Path(self.temp_dir.name) / "holder-temp",
            "contender": Path(self.temp_dir.name) / "contender-temp",
        }
        for directory in process_temp.values():
            directory.mkdir()
        holder_ready = coordination / "holder-ready"
        release_holder = coordination / "release-holder"
        script = textwrap.dedent(
            r"""
            import json
            import sys
            import time
            from pathlib import Path

            sys.path.insert(0, sys.argv[1])
            import workflow_core.project as project_module

            project_root = Path(sys.argv[2])
            coordination = Path(sys.argv[3])
            role = sys.argv[4]
            if role == "holder":
                original_apply = project_module._apply_project_assets

                def hold_during_apply(updates):
                    (coordination / "holder-ready").write_text(
                        "ready", encoding="utf-8"
                    )
                    deadline = time.monotonic() + 12
                    while not (coordination / "release-holder").exists():
                        if time.monotonic() >= deadline:
                            raise TimeoutError("holder release timed out")
                        time.sleep(0.01)
                    return original_apply(updates)

                project_module._apply_project_assets = hold_during_apply

            try:
                payload = project_module.init_project(project_root, role)
            except Exception as error:
                print(str(error), file=sys.stderr)
                raise SystemExit(2)
            print(json.dumps(payload, ensure_ascii=False))
            """
        )

        def start(role: str) -> subprocess.Popen[str]:
            environment = os.environ.copy()
            for variable in ("TEMP", "TMP", "TMPDIR"):
                environment[variable] = str(process_temp[role])
            return subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-X",
                    "utf8",
                    "-c",
                    script,
                    str(SCRIPTS),
                    str(self.project),
                    str(coordination),
                    role,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                text=True,
                env=environment,
            )

        holder = start("holder")
        contender: subprocess.Popen[str] | None = None
        holder_output = ("", "")
        contender_output = ("", "")
        try:
            deadline = time.monotonic() + SUBPROCESS_TIMEOUT_SECONDS
            while not holder_ready.exists():
                if holder.poll() is not None:
                    stdout, stderr = holder.communicate()
                    self.fail(f"holder 提前退出：{stdout}\n{stderr}")
                if time.monotonic() >= deadline:
                    self.fail("holder 未进入受保护的资产阶段")
                time.sleep(0.01)

            contender = start("contender")
            contender_output = contender.communicate(
                timeout=SUBPROCESS_TIMEOUT_SECONDS
            )
            live_staging = self.project / ".experiment-workflow.staging"
            self.assertTrue(live_staging.is_dir())
            self.assertTrue(
                (live_staging / ".cv-experiment-workflow-staging").is_file()
            )
            release_holder.write_text("release", encoding="utf-8")
            holder_output = holder.communicate(timeout=SUBPROCESS_TIMEOUT_SECONDS)
        finally:
            release_holder.touch(exist_ok=True)
            for process in (holder, contender):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.communicate()

        return_codes = [holder.returncode, contender.returncode]
        self.assertEqual([0, 2], sorted(return_codes))
        loser_stderr = (
            holder_output[1] if holder.returncode == 2 else contender_output[1]
        ).lower()
        self.assertTrue(
            "initialization in progress" in loser_stderr
            or "already initialized" in loser_stderr,
            loser_stderr,
        )
        self.assertFalse(
            (self.project / ".experiment-workflow.staging").exists()
        )
        self.assertTrue((self.project / ".experiment-workflow").is_dir())
        self.assertTrue(
            (self.project / ".experiment-workflow.init.lock").is_file()
        )
        for filename in ("AGENTS.md", "WORKFLOW.md"):
            self.assertIn(
                ".experiment-workflow/project.json",
                (self.project / filename).read_text(encoding="utf-8"),
            )
        self.assertIn(
            ".experiment-workflow/.runtime/",
            (self.project / ".gitignore").read_text(encoding="utf-8"),
        )

    def test_simultaneous_lock_bootstrap_with_open_temp_has_one_winner(self) -> None:
        self.project.mkdir()
        coordination = Path(self.temp_dir.name) / "bootstrap-coordination"
        coordination.mkdir()
        script = textwrap.dedent(
            r"""
            import json
            import os
            import sys
            import time
            from pathlib import Path

            sys.path.insert(0, sys.argv[1])
            import workflow_core.locking as locking
            from workflow_core.project import init_project

            project_root = Path(sys.argv[2])
            coordination = Path(sys.argv[3])
            role = sys.argv[4]
            other = "loser" if role == "winner" else "winner"
            real_link = locking.os.link
            real_cleanup = locking._cleanup_lock_bootstrap_files
            held_bootstrap = None

            def wait_for(path):
                deadline = time.monotonic() + 12
                while not path.exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for {path.name}")
                    time.sleep(0.01)

            def coordinated_link(
                source, destination, *, follow_symlinks=True
            ):
                global held_bootstrap
                if Path(destination).name == ".experiment-workflow.init.lock":
                    if role == "loser":
                        held_bootstrap = Path(source).open("rb")
                    (coordination / f"{role}-ready").write_text(
                        "ready", encoding="utf-8"
                    )
                    wait_for(coordination / f"{other}-ready")
                    if role == "loser":
                        wait_for(coordination / "winner-cleaned")
                return real_link(
                    source,
                    destination,
                    follow_symlinks=follow_symlinks,
                )

            locking.os.link = coordinated_link
            if role == "winner":
                def cleanup_then_release(project_path):
                    try:
                        real_cleanup(project_path)
                    finally:
                        (coordination / "winner-cleaned").write_text(
                            "cleaned", encoding="utf-8"
                        )

                locking._cleanup_lock_bootstrap_files = cleanup_then_release

            exit_code = 0
            try:
                payload = init_project(project_root, role)
            except Exception as error:
                print(str(error), file=sys.stderr)
                exit_code = 2
            finally:
                if held_bootstrap is not None:
                    held_bootstrap.close()
            if exit_code:
                raise SystemExit(exit_code)
            print(json.dumps(payload, ensure_ascii=False))
            """
        )

        def start(role: str) -> subprocess.Popen[str]:
            return subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-X",
                    "utf8",
                    "-c",
                    script,
                    str(SCRIPTS),
                    str(self.project),
                    str(coordination),
                    role,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                text=True,
            )

        processes = [start("loser"), start("winner")]
        outputs: list[tuple[str, str]] = []
        try:
            for process in processes:
                outputs.append(
                    process.communicate(timeout=SUBPROCESS_TIMEOUT_SECONDS)
                )
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

        self.assertEqual([0, 2], sorted(process.returncode for process in processes))
        loser_index = next(
            index
            for index, process in enumerate(processes)
            if process.returncode == 2
        )
        loser_stderr = outputs[loser_index][1].lower()
        self.assertTrue(
            "initialization in progress" in loser_stderr
            or "already initialized" in loser_stderr
            or "已经存在" in loser_stderr,
            loser_stderr,
        )
        self.assertTrue(
            (self.project / ".experiment-workflow" / "project.json").is_file()
        )
        for filename in ("AGENTS.md", "WORKFLOW.md", ".gitignore"):
            self.assertTrue((self.project / filename).is_file(), filename)
        retry = run_cli(
            "init", "--path", self.project, "--name", "重试", check=False
        )
        self.assertEqual(2, retry.returncode)
        self.assertIn("已经存在", retry.stderr)
        self.assertEqual(
            [],
            list(
                self.project.glob(
                    ".experiment-workflow.init.lock.bootstrap-*.tmp"
                )
            ),
        )


class WorkflowCoreImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(SCRIPTS))
        cls.addClassCleanup(lambda: sys.path.remove(str(SCRIPTS)))


class ProjectConcurrencyTests(WorkflowCoreImportTests):
    def test_concurrent_threads_have_one_winner_and_one_in_progress_error(self) -> None:
        project_module = importlib.import_module("workflow_core.project")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            ready = threading.Event()
            release = threading.Event()
            call_guard = threading.Lock()
            apply_calls = 0
            successes: list[dict[str, object]] = []
            errors: list[BaseException] = []
            original_apply = project_module._apply_project_assets

            def hold_first_apply(updates: object) -> object:
                nonlocal apply_calls
                with call_guard:
                    apply_calls += 1
                    is_first = apply_calls == 1
                if is_first:
                    ready.set()
                    if not release.wait(timeout=SUBPROCESS_TIMEOUT_SECONDS):
                        raise TimeoutError("thread release timed out")
                return original_apply(updates)

            def initialize(name: str) -> None:
                try:
                    successes.append(project_module.init_project(project_root, name))
                except BaseException as error:
                    errors.append(error)

            with mock.patch.object(
                project_module,
                "_apply_project_assets",
                side_effect=hold_first_apply,
            ):
                first = threading.Thread(target=initialize, args=("first",))
                second = threading.Thread(target=initialize, args=("second",))
                first.start()
                self.assertTrue(
                    ready.wait(timeout=SUBPROCESS_TIMEOUT_SECONDS),
                    "first thread 未进入资产阶段",
                )
                second.start()
                second.join(timeout=SUBPROCESS_TIMEOUT_SECONDS)
                self.assertFalse(second.is_alive(), "second thread 未非阻塞退出")
                release.set()
                first.join(timeout=SUBPROCESS_TIMEOUT_SECONDS)
                self.assertFalse(first.is_alive(), "first thread 未完成")

            self.assertEqual(1, len(successes))
            self.assertEqual(1, len(errors))
            error_message = str(errors[0]).lower()
            self.assertTrue(
                "initialization in progress" in error_message
                or "already initialized" in error_message,
                error_message,
            )
            self.assertTrue(
                (project_root / ".experiment-workflow" / "project.json").is_file()
            )
            self.assertFalse(
                (project_root / ".experiment-workflow.staging").exists()
            )


class AtomicWriteJsonTests(WorkflowCoreImportTests):
    def test_atomic_entry_points_freeze_relative_path_before_cwd_changes(self) -> None:
        io = importlib.import_module("workflow_core.io")
        operations = (
            ("write-json", lambda path, txid: io.atomic_write_json(
                path, {"value": 1}, transaction_id=txid,
            )),
            ("write-bytes", lambda path, txid: io.atomic_write_bytes(
                path, b"write bytes\n", transaction_id=txid,
            )),
            ("create-json", lambda path, txid: io.atomic_create_json(
                path, {"value": 1}, transaction_id=txid,
            )),
            ("create-bytes", lambda path, txid: io.atomic_create_bytes(
                path, b"create bytes\n", transaction_id=txid,
            )),
        )
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_cwd = root / "source"
            changed_cwd = root / "changed"
            source_cwd.mkdir()
            changed_cwd.mkdir()
            for index, (label, operation) in enumerate(operations, start=1):
                with self.subTest(operation=label):
                    relative_parent = Path(f"same-name-{index}")
                    (source_cwd / relative_parent).mkdir()
                    (changed_cwd / relative_parent).mkdir()
                    relative_path = relative_parent / "state.json"
                    expected = source_cwd / relative_path
                    escaped = changed_cwd / relative_path
                    transaction_id = f"{index:032x}"
                    callback_paths: list[Path] = []
                    switched = False
                    original_temp_path = io.transaction_temp_path

                    def switch_cwd(path: Path, txid: str) -> Path:
                        nonlocal switched
                        callback_paths.append(Path(path))
                        result = original_temp_path(path, txid)
                        if not switched:
                            switched = True
                            os.chdir(changed_cwd)
                        return result

                    try:
                        os.chdir(source_cwd)
                        with mock.patch.object(
                            io, "transaction_temp_path", side_effect=switch_cwd,
                        ):
                            operation(relative_path, transaction_id)
                    finally:
                        os.chdir(original_cwd)

                    self.assertTrue(switched)
                    self.assertTrue(callback_paths[0].is_absolute())
                    self.assertTrue(expected.is_file())
                    self.assertFalse(os.path.lexists(escaped))

    def test_atomic_create_json_reuses_absolute_anchor_after_cwd_change(self) -> None:
        io = importlib.import_module("workflow_core.io")
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_cwd = root / "source"
            changed_cwd = root / "changed"
            relative_parent = Path("same-name")
            (source_cwd / relative_parent).mkdir(parents=True)
            (changed_cwd / relative_parent).mkdir(parents=True)
            relative_path = relative_parent / "state.json"
            expected = source_cwd / relative_path
            escaped = changed_cwd / relative_path
            original_create_bytes = io.atomic_create_bytes
            nested_paths: list[Path] = []

            def switch_before_nested_lookup(
                path: Path, content: bytes, *, transaction_id: str,
            ) -> bool:
                nested_paths.append(Path(path))
                os.chdir(changed_cwd)
                return original_create_bytes(
                    path, content, transaction_id=transaction_id,
                )

            try:
                os.chdir(source_cwd)
                with io.parent_directory_anchor(relative_parent) as anchor:
                    self.assertTrue(anchor.path.is_absolute())
                with mock.patch.object(
                    io, "atomic_create_bytes", side_effect=switch_before_nested_lookup,
                ):
                    io.atomic_create_json(
                        relative_path, {"value": 1}, transaction_id="d" * 32,
                    )
            finally:
                os.chdir(original_cwd)

            self.assertTrue(nested_paths[0].is_absolute())
            self.assertTrue(expected.is_file())
            self.assertFalse(os.path.lexists(escaped))

    def test_atomic_write_uses_same_directory_fsync_and_replace(self) -> None:
        io = importlib.import_module("workflow_core.io")
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "state.json"
            original_fsync = os.fsync
            with mock.patch.object(io.os, "fsync", wraps=original_fsync) as fsync:
                io.atomic_write_json(destination, {"名称": "实验"})

            self.assertTrue(fsync.called)
            self.assertEqual({"名称": "实验"}, read_json(destination))
            self.assertIn("实验", destination.read_text(encoding="utf-8"))
            self.assertEqual([destination], list(destination.parent.iterdir()))

    def test_atomic_create_existing_regular_file_returns_false_without_temp(
        self,
    ) -> None:
        io = importlib.import_module("workflow_core.io")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cases = (
                ("bytes", b"existing bytes\n", b"different bytes\n"),
                ("json", b'{"existing": true}\n', {"different": True}),
            )
            for index, (kind, existing, replacement) in enumerate(cases, start=1):
                with self.subTest(kind=kind):
                    destination = root / f"existing-{kind}.bin"
                    transaction_id = f"{index:032x}"
                    temporary = io.transaction_temp_path(destination, transaction_id)
                    destination.write_bytes(existing)
                    original_open = io.ParentDirectoryAnchor.open_exclusive
                    open_calls: list[str] = []

                    def track_open(anchor, name: str):
                        open_calls.append(name)
                        return original_open(anchor, name)

                    with mock.patch.object(
                        io.ParentDirectoryAnchor, "open_exclusive", new=track_open,
                    ):
                        if kind == "bytes":
                            created = io.atomic_create_bytes(
                                destination,
                                replacement,
                                transaction_id=transaction_id,
                            )
                        else:
                            created = io.atomic_create_json(
                                destination,
                                replacement,
                                transaction_id=transaction_id,
                            )

                    self.assertFalse(created)
                    self.assertEqual(existing, destination.read_bytes())
                    self.assertEqual([], open_calls)
                    self.assertFalse(os.path.lexists(temporary))

    def test_atomic_create_rejects_existing_directory_before_creating_temp(
        self,
    ) -> None:
        io = importlib.import_module("workflow_core.io")
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "existing-directory"
            destination.mkdir()
            transaction_id = "3" * 32
            temporary = io.transaction_temp_path(destination, transaction_id)
            original_open = io.ParentDirectoryAnchor.open_exclusive
            open_calls: list[str] = []

            def track_open(anchor, name: str):
                open_calls.append(name)
                return original_open(anchor, name)

            with mock.patch.object(
                io.ParentDirectoryAnchor, "open_exclusive", new=track_open,
            ):
                with self.assertRaisesRegex(ValueError, "不是普通文件"):
                    io.atomic_create_bytes(
                        destination,
                        b"must not publish\n",
                        transaction_id=transaction_id,
                    )

            self.assertTrue(destination.is_dir())
            self.assertEqual([], open_calls)
            self.assertFalse(os.path.lexists(temporary))

    def test_atomic_create_rejects_existing_symlink_before_creating_temp(
        self,
    ) -> None:
        io = importlib.import_module("workflow_core.io")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            external = root / "external.json"
            external_content = b'{"external": true}\n'
            external.write_bytes(external_content)
            destination = root / "existing-link.json"
            try:
                destination.symlink_to(external)
            except OSError as error:
                self.skipTest(f"当前环境不支持文件符号链接：{error}")
            transaction_id = "4" * 32
            temporary = io.transaction_temp_path(destination, transaction_id)
            original_open = io.ParentDirectoryAnchor.open_exclusive
            open_calls: list[str] = []

            def track_open(anchor, name: str):
                open_calls.append(name)
                return original_open(anchor, name)

            with mock.patch.object(
                io.ParentDirectoryAnchor, "open_exclusive", new=track_open,
            ):
                with self.assertRaisesRegex(ValueError, "不是普通文件"):
                    io.atomic_create_json(
                        destination,
                        {"replacement": True},
                        transaction_id=transaction_id,
                    )

            self.assertTrue(destination.is_symlink())
            self.assertEqual(external_content, external.read_bytes())
            self.assertEqual([], open_calls)
            self.assertFalse(os.path.lexists(temporary))

    def test_atomic_write_preserves_auditable_temp_and_allows_new_retry(self) -> None:
        io = importlib.import_module("workflow_core.io")
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "state.json"

            def fail_replace(source: object, target: object) -> None:
                self.assertEqual(destination.parent, Path(source).parent)
                self.assertEqual(destination, Path(target))
                raise OSError("replace failed")

            with mock.patch.object(io.os, "replace", side_effect=fail_replace):
                with self.assertRaises(RuntimeError) as captured:
                    io.atomic_write_json(destination, {"value": 1})

            residuals = list(destination.parent.glob(f".{destination.name}.*.tmp"))
            self.assertEqual(1, len(residuals))
            self.assertIn("replace failed", str(captured.exception))
            self.assertIn("异常路径未自动清理", str(captured.exception))
            self.assertIn(
                str(residuals[0]), str(captured.exception).split("残留路径：", 1)[1],
            )
            self.assertFalse(destination.exists())

            io.atomic_write_json(destination, {"value": 2})
            self.assertEqual({"value": 2}, read_json(destination))
            self.assertTrue(residuals[0].exists())

    def test_atomic_write_preserves_temp_when_lexists_commit_probe_fails(self) -> None:
        io = importlib.import_module("workflow_core.io")
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "state.json"
            transaction_id = "a" * 32
            temporary = io.transaction_temp_path(destination, transaction_id)
            original_lexists = io.ParentDirectoryAnchor.lexists
            probe_failed = False

            def fail_probe_once(anchor, name: str) -> bool:
                nonlocal probe_failed
                if name == temporary.name and not probe_failed:
                    probe_failed = True
                    raise PermissionError("lexists probe denied")
                return original_lexists(anchor, name)

            with mock.patch.object(
                io.ParentDirectoryAnchor, "replace",
                side_effect=OSError("replace precommit failed"),
            ), mock.patch.object(
                io.ParentDirectoryAnchor, "lexists", new=fail_probe_once,
            ):
                with self.assertRaises(RuntimeError) as captured:
                    io.atomic_write_json(
                        destination, {"value": 1}, transaction_id=transaction_id,
                    )

            message = str(captured.exception)
            self.assertIn("replace precommit failed", message)
            self.assertIn("lexists probe denied", message)
            self.assertIn("异常路径未自动清理", message)
            self.assertIn(str(temporary), message.split("残留路径：", 1)[1])
            self.assertTrue(os.path.lexists(temporary))
            self.assertFalse(destination.exists())

    def test_atomic_write_preserves_temp_when_read_commit_probe_fails(self) -> None:
        io = importlib.import_module("workflow_core.io")
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "state.json"
            transaction_id = "b" * 32
            temporary = io.transaction_temp_path(destination, transaction_id)
            original_lexists = io.ParentDirectoryAnchor.lexists
            probe_lexists_calls = 0

            def reach_read_probe(anchor, name: str) -> bool:
                nonlocal probe_lexists_calls
                if probe_lexists_calls < 2:
                    probe_lexists_calls += 1
                    return name == destination.name
                return original_lexists(anchor, name)

            with mock.patch.object(
                io.ParentDirectoryAnchor, "replace",
                side_effect=OSError("replace precommit failed"),
            ), mock.patch.object(
                io.ParentDirectoryAnchor, "lexists", new=reach_read_probe,
            ), mock.patch.object(
                io.ParentDirectoryAnchor, "read_bytes",
                side_effect=PermissionError("read probe denied"),
            ):
                with self.assertRaises(RuntimeError) as captured:
                    io.atomic_write_json(
                        destination, {"value": 1}, transaction_id=transaction_id,
                    )

            message = str(captured.exception)
            self.assertIn("replace precommit failed", message)
            self.assertIn("read probe denied", message)
            self.assertIn("异常路径未自动清理", message)
            self.assertIn(str(temporary), message.split("残留路径：", 1)[1])
            self.assertTrue(os.path.lexists(temporary))
            self.assertFalse(destination.exists())

    def test_atomic_create_reports_probe_and_conservative_residual_after_link_failure(
        self,
    ) -> None:
        io = importlib.import_module("workflow_core.io")
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "state.json"
            transaction_id = "c" * 32
            temporary = io.transaction_temp_path(destination, transaction_id)
            original_lstat = io.ParentDirectoryAnchor.lstat
            original_unlink = io.ParentDirectoryAnchor.unlink
            probe_failed = False
            cleanup_calls = 0

            def fail_probe_once(anchor, name: str) -> os.stat_result:
                nonlocal probe_failed
                if name == temporary.name and not probe_failed:
                    probe_failed = True
                    raise PermissionError("create probe denied")
                return original_lstat(anchor, name)

            def deny_temp_cleanup(anchor, name: str, *, missing_ok: bool = False) -> None:
                nonlocal cleanup_calls
                if name == temporary.name:
                    cleanup_calls += 1
                    raise PermissionError("create cleanup denied")
                original_unlink(anchor, name, missing_ok=missing_ok)

            with mock.patch.object(
                io.ParentDirectoryAnchor, "link",
                side_effect=OSError("link precommit failed"),
            ), mock.patch.object(
                io.ParentDirectoryAnchor, "lstat", new=fail_probe_once,
            ), mock.patch.object(
                io.ParentDirectoryAnchor, "unlink", new=deny_temp_cleanup,
            ):
                with self.assertRaises(RuntimeError) as captured:
                    io.atomic_create_json(
                        destination, {"value": 1}, transaction_id=transaction_id,
                    )

            message = str(captured.exception)
            self.assertIn("link precommit failed", message)
            self.assertIn("create probe denied", message)
            self.assertIn("异常路径未自动清理", message)
            self.assertEqual(0, cleanup_calls)
            self.assertIn(str(temporary), message.split("残留路径：", 1)[1])
            self.assertTrue(temporary.exists())
            self.assertFalse(destination.exists())

    def test_atomic_failures_preserve_rebound_user_temp_and_tool_backup(self) -> None:
        io = importlib.import_module("workflow_core.io")
        operations = ("write", "create")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index, operation in enumerate(operations, start=1):
                with self.subTest(operation=operation):
                    destination = root / f"{operation}.bin"
                    transaction_id = f"{index + 10:032x}"
                    temporary = io.transaction_temp_path(destination, transaction_id)
                    backup = temporary.with_name(temporary.name + ".tool-backup")
                    user_content = f"user-owned-{operation}\n".encode()

                    def swap_temp(anchor, source: str, target: str) -> None:
                        self.assertEqual(temporary.name, source)
                        (anchor.path / source).rename(backup)
                        (anchor.path / source).write_bytes(user_content)
                        raise OSError(f"{operation} publish failed")

                    method = "replace" if operation == "write" else "link"
                    with mock.patch.object(
                        io.ParentDirectoryAnchor, method, new=swap_temp,
                    ):
                        with self.assertRaises(BaseException) as captured:
                            if operation == "write":
                                io.atomic_write_bytes(
                                    destination,
                                    b"tool-write\n",
                                    transaction_id=transaction_id,
                                )
                            else:
                                io.atomic_create_bytes(
                                    destination,
                                    b"tool-create\n",
                                    transaction_id=transaction_id,
                                )

                    self.assertIsInstance(captured.exception, RuntimeError)
                    message = str(captured.exception)
                    self.assertIn("所有权已变化", message)
                    self.assertIn(str(temporary), message.split("残留路径：", 1)[1])
                    self.assertEqual(user_content, temporary.read_bytes())
                    self.assertTrue(backup.is_file())
                    self.assertFalse(destination.exists())

    def test_atomic_exception_path_never_unlinks_after_identity_check(self) -> None:
        io = importlib.import_module("workflow_core.io")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index, operation in enumerate(("write", "create"), start=1):
                with self.subTest(operation=operation):
                    destination = root / f"checked-{operation}.bin"
                    transaction_id = f"{index + 20:032x}"
                    temporary = io.transaction_temp_path(destination, transaction_id)
                    backup = temporary.with_name(temporary.name + ".tool-backup")
                    user_content = f"user-after-check-{operation}\n".encode()
                    original_lstat = io.ParentDirectoryAnchor.lstat
                    original_unlink = io.ParentDirectoryAnchor.unlink
                    lstat_calls = 0
                    unlink_calls = 0
                    swap_on_call = 1 if operation == "write" else 2

                    def swap_after_identity_check(anchor, name: str):
                        nonlocal lstat_calls
                        metadata = original_lstat(anchor, name)
                        if name == temporary.name:
                            lstat_calls += 1
                            if lstat_calls == swap_on_call:
                                (anchor.path / name).rename(backup)
                                (anchor.path / name).write_bytes(user_content)
                        return metadata

                    def track_unlink(
                        anchor, name: str, *, missing_ok: bool = False,
                    ) -> None:
                        nonlocal unlink_calls
                        if name == temporary.name:
                            unlink_calls += 1
                        original_unlink(anchor, name, missing_ok=missing_ok)

                    def fail_publish(anchor, source: str, target: str) -> None:
                        raise OSError(f"{operation} precommit publish failed")

                    method = "replace" if operation == "write" else "link"
                    with mock.patch.object(
                        io.ParentDirectoryAnchor, method, new=fail_publish,
                    ), mock.patch.object(
                        io.ParentDirectoryAnchor, "lstat", new=swap_after_identity_check,
                    ), mock.patch.object(
                        io.ParentDirectoryAnchor, "unlink", new=track_unlink,
                    ):
                        with self.assertRaises(BaseException) as captured:
                            if operation == "write":
                                io.atomic_write_bytes(
                                    destination,
                                    b"tool-write\n",
                                    transaction_id=transaction_id,
                                )
                            else:
                                io.atomic_create_bytes(
                                    destination,
                                    b"tool-create\n",
                                    transaction_id=transaction_id,
                                )

                    self.assertIsInstance(captured.exception, RuntimeError)
                    message = str(captured.exception)
                    self.assertIn(f"{operation} precommit publish failed", message)
                    self.assertIn("异常路径未自动清理", message)
                    self.assertIn(str(temporary), message.split("残留路径：", 1)[1])
                    self.assertEqual(0, unlink_calls)
                    self.assertEqual(user_content, temporary.read_bytes())
                    self.assertTrue(backup.is_file())
                    self.assertFalse(destination.exists())

    def test_atomic_create_eexist_hardlink_race_never_deletes_destination(self) -> None:
        io = importlib.import_module("workflow_core.io")
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "state.bin"
            transaction_id = "e" * 32
            temporary = io.transaction_temp_path(destination, transaction_id)
            content = b"transaction content\n"
            original_link = io.ParentDirectoryAnchor.link
            original_unlink = io.ParentDirectoryAnchor.unlink
            cleanup_calls = 0

            def race_link(anchor, source: str, target: str) -> None:
                original_link(anchor, source, target)
                raise FileExistsError(errno.EEXIST, "competitor published hardlink")

            def fail_first_temp_cleanup(
                anchor, name: str, *, missing_ok: bool = False,
            ) -> None:
                nonlocal cleanup_calls
                if name == temporary.name:
                    cleanup_calls += 1
                    raise PermissionError("first temp cleanup denied")
                original_unlink(anchor, name, missing_ok=missing_ok)

            with mock.patch.object(
                io.ParentDirectoryAnchor, "link", new=race_link,
            ), mock.patch.object(
                io.ParentDirectoryAnchor, "unlink", new=fail_first_temp_cleanup,
            ):
                with self.assertRaises(RuntimeError) as captured:
                    io.atomic_create_bytes(
                        destination, content, transaction_id=transaction_id,
                    )

            self.assertEqual(0, cleanup_calls)
            self.assertIn("异常路径未自动清理", str(captured.exception))
            self.assertEqual(content, destination.read_bytes())
            self.assertTrue(temporary.exists())
            self.assertIn(
                str(temporary), str(captured.exception).split("残留路径：", 1)[1],
            )

    def test_atomic_write_exception_never_attempts_named_temp_cleanup(self) -> None:
        io_module = importlib.import_module("workflow_core.io")
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "state.json"
            residual_temps: list[Path] = []
            original_unlink = Path.unlink

            def deny_temp_cleanup(path: Path, missing_ok: bool = False) -> None:
                if path.name.startswith(f".{destination.name}."):
                    residual_temps.append(path)
                    raise PermissionError("temp cleanup denied")
                original_unlink(path, missing_ok=missing_ok)

            with mock.patch.object(
                io_module.os,
                "replace",
                side_effect=OSError("replace failed"),
            ), mock.patch.object(Path, "unlink", new=deny_temp_cleanup):
                with self.assertRaises(Exception) as captured:
                    io_module.atomic_write_json(destination, {"value": 1})

            self.assertIsInstance(captured.exception, RuntimeError)
            message = str(captured.exception)
            self.assertIn("replace failed", message)
            self.assertIn("异常路径未自动清理", message)
            self.assertIn("残留路径", message)
            self.assertEqual(0, len(residual_temps))
            temporary, = destination.parent.glob(f".{destination.name}.*.tmp")
            self.assertIn(str(temporary), message.split("残留路径：", 1)[1])
            self.assertTrue(temporary.exists())
            self.assertFalse(destination.exists())


class ProjectRollbackTests(WorkflowCoreImportTests):
    def test_init_rolls_back_created_files_when_json_write_fails(self) -> None:
        project_module = importlib.import_module("workflow_core.project")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"

            with mock.patch.object(
                project_module,
                "atomic_write_json",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    project_module.init_project(project_root, "演示")

            self.assertFalse((project_root / ".experiment-workflow").exists())
            self.assertFalse((project_root / "AGENTS.md").exists())
            self.assertFalse((project_root / "WORKFLOW.md").exists())
            self.assertFalse((project_root / ".gitignore").exists())

            project_module.init_project(project_root, "演示")
            self.assertTrue(
                (project_root / ".experiment-workflow" / "project.json").is_file()
            )

    def test_json_failure_does_not_rewrite_existing_root_files(self) -> None:
        project_module = importlib.import_module("workflow_core.project")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            timestamp = 1_000_000_000_000_000_000
            root_files = []
            for filename in ("AGENTS.md", "WORKFLOW.md", ".gitignore"):
                path = project_root / filename
                path.write_text(f"原有内容：{filename}\n", encoding="utf-8")
                os.utime(path, ns=(timestamp, timestamp))
                root_files.append(path)

            with mock.patch.object(
                project_module,
                "atomic_write_json",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    project_module.init_project(project_root, "演示")

            self.assertFalse((project_root / ".experiment-workflow").exists())
            for path in root_files:
                self.assertEqual(timestamp, path.stat().st_mtime_ns, path.name)

    def test_asset_write_failure_removes_created_assets_and_preserves_existing(
        self,
    ) -> None:
        project_module = importlib.import_module("workflow_core.project")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            timestamp = 1_000_000_000_000_000_000
            agents = project_root / "AGENTS.md"
            original_agents = b"original agents\n"
            agents.write_bytes(original_agents)
            os.utime(agents, ns=(timestamp, timestamp))

            original_atomic_create = project_module.atomic_create_bytes

            def fail_gitignore_write(
                path: Path,
                content: bytes,
                *,
                transaction_id: str | None = None,
            ) -> bool:
                if path.name == ".gitignore":
                    raise OSError("asset write failed")
                return original_atomic_create(
                    path,
                    content,
                    transaction_id=transaction_id,
                )

            with mock.patch.object(
                project_module,
                "atomic_create_bytes",
                side_effect=fail_gitignore_write,
            ):
                with self.assertRaisesRegex(OSError, "asset write failed"):
                    project_module.init_project(project_root, "演示")

            self.assertFalse((project_root / ".experiment-workflow").exists())
            self.assertEqual(original_agents, agents.read_bytes())
            self.assertEqual(timestamp, agents.stat().st_mtime_ns)
            self.assertFalse((project_root / "WORKFLOW.md").exists())
            self.assertFalse((project_root / ".gitignore").exists())

    def test_asset_restore_failure_is_reported_and_control_plane_is_removed(self) -> None:
        project_module = importlib.import_module("workflow_core.project")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()

            original_atomic_create = project_module.atomic_create_bytes
            original_unlink = Path.unlink

            def fail_apply_and_restore(
                path: Path,
                content: bytes,
                *,
                transaction_id: str | None = None,
            ) -> bool:
                if path.name == ".gitignore":
                    raise OSError("asset apply failed")
                return original_atomic_create(
                    path,
                    content,
                    transaction_id=transaction_id,
                )

            def fail_workflow_restore(
                path: Path, missing_ok: bool = False
            ) -> None:
                if path.name == "WORKFLOW.md":
                    raise RuntimeError("asset restore failed")
                original_unlink(path, missing_ok=missing_ok)

            with mock.patch.object(
                project_module,
                "atomic_create_bytes",
                side_effect=fail_apply_and_restore,
            ), mock.patch.object(Path, "unlink", new=fail_workflow_restore):
                with self.assertRaisesRegex(
                    RuntimeError, "asset apply failed.*asset restore failed"
                ):
                    project_module.init_project(project_root, "演示")

            self.assertFalse((project_root / ".experiment-workflow").exists())
            self.assertFalse((project_root / "AGENTS.md").exists())
            self.assertTrue((project_root / "WORKFLOW.md").exists())

    def test_regular_file_created_after_prepare_is_preserved(self) -> None:
        project_module = importlib.import_module("workflow_core.project")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            agents = project_root / "AGENTS.md"
            root_temp = project_root / (".AGENTS.md.cvexp-" + "1" * 32 + ".tmp")
            user_content = b"user-created after prepare\n"
            original_apply = project_module._apply_project_assets

            def claim_then_apply(updates: object) -> object:
                agents.write_bytes(user_content)
                return original_apply(updates)

            with mock.patch.object(
                project_module,
                "_apply_project_assets",
                side_effect=claim_then_apply,
            ):
                project_module.init_project(project_root, "演示")

            self.assertEqual(user_content, agents.read_bytes())
            self.assertTrue(
                (project_root / ".experiment-workflow" / "project.json").is_file()
            )
            self.assertFalse(root_temp.exists())

    def test_directory_created_after_prepare_is_preserved(self) -> None:
        project_module = importlib.import_module("workflow_core.project")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            agents = project_root / "AGENTS.md"
            root_temp = project_root / (".AGENTS.md.cvexp-" + "1" * 32 + ".tmp")
            original_apply = project_module._apply_project_assets

            def claim_then_apply(updates: object) -> object:
                agents.mkdir()
                return original_apply(updates)

            with mock.patch.object(
                project_module,
                "_apply_project_assets",
                side_effect=claim_then_apply,
            ):
                with self.assertRaisesRegex(ValueError, "不是普通文件"):
                    project_module.init_project(project_root, "演示")

            self.assertTrue(agents.is_dir())
            self.assertFalse(root_temp.exists())
            self.assertFalse((project_root / ".experiment-workflow").exists())

    def test_symlink_created_after_prepare_is_preserved(self) -> None:
        project_module = importlib.import_module("workflow_core.project")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            agents = project_root / "AGENTS.md"
            root_temp = project_root / (".AGENTS.md.cvexp-" + "1" * 32 + ".tmp")
            external = Path(temp_dir) / "external.txt"
            external_content = "外部文件不得改写\n"
            external.write_text(external_content, encoding="utf-8")
            original_apply = project_module._apply_project_assets

            def swap_then_apply(updates: object) -> object:
                try:
                    agents.symlink_to(external)
                except OSError as error:
                    self.skipTest(f"当前环境不支持文件符号链接：{error}")
                return original_apply(updates)

            with mock.patch.object(
                project_module,
                "_apply_project_assets",
                side_effect=swap_then_apply,
            ):
                with self.assertRaisesRegex(ValueError, "不是普通文件"):
                    project_module.init_project(project_root, "演示")

            self.assertEqual(
                external_content, external.read_text(encoding="utf-8")
            )
            self.assertTrue(agents.is_symlink())
            self.assertFalse(root_temp.exists())
            self.assertFalse((project_root / ".experiment-workflow").exists())

    def test_rollback_preserves_replaced_asset_and_reports_ownership_change(
        self,
    ) -> None:
        project_module = importlib.import_module("workflow_core.project")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            control = project_root / ".experiment-workflow"
            staging = project_root / ".experiment-workflow.staging"
            agents = project_root / "AGENTS.md"
            user_content = b"user replacement during rollback\n"
            original_replace = os.replace

            def replace_user_asset_then_fail(
                source: object, target: object
            ) -> None:
                if Path(source) == staging and Path(target) == control:
                    agents.unlink()
                    agents.write_bytes(user_content)
                    raise OSError("final rename failed after user replacement")
                original_replace(source, target)

            with mock.patch.object(
                project_module.os,
                "replace",
                side_effect=replace_user_asset_then_fail,
            ):
                with self.assertRaises(Exception) as captured:
                    project_module.init_project(project_root, "演示")

            self.assertIsInstance(captured.exception, RuntimeError)
            self.assertRegex(
                str(captured.exception), "所有权.*AGENTS.md|AGENTS.md.*所有权"
            )
            self.assertEqual(user_content, agents.read_bytes())
            self.assertFalse(control.exists())
            self.assertFalse(staging.exists())
            self.assertEqual(
                [], list(project_root.glob(".*.cvexp-" + "1" * 32 + ".tmp"))
            )

    def test_interrupt_after_asset_link_preserves_ambiguous_target_and_reports(
        self,
    ) -> None:
        project_module = importlib.import_module("workflow_core.project")
        workflow_io = importlib.import_module("workflow_core.io")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            agents = project_root / "AGENTS.md"
            staging = project_root / ".experiment-workflow.staging"
            root_temp = project_root / (
                ".AGENTS.md.cvexp-" + "1" * 32 + ".tmp"
            )
            original_link = os.link

            def interrupt_after_link(
                source: object,
                destination: object,
                *,
                follow_symlinks: bool = True,
            ) -> None:
                original_link(
                    source,
                    destination,
                    follow_symlinks=follow_symlinks,
                )
                if Path(destination) == agents:
                    raise KeyboardInterrupt("interrupt after asset link")

            with mock.patch.object(
                workflow_io.os,
                "link",
                side_effect=interrupt_after_link,
            ):
                with self.assertRaises(RuntimeError) as captured:
                    project_module.init_project(project_root, "演示")

            message = str(captured.exception)
            self.assertIn("interrupt after asset link", message)
            self.assertIn("所有权未知", message)
            self.assertIn("异常路径未自动清理", message)
            self.assertIn(str(root_temp), message)
            self.assertTrue(agents.is_file())
            self.assertTrue(root_temp.exists())
            self.assertFalse(staging.exists())
            self.assertFalse((project_root / ".experiment-workflow").exists())

    def test_interrupt_after_asset_link_preserves_user_swap_and_reports(
        self,
    ) -> None:
        project_module = importlib.import_module("workflow_core.project")
        workflow_io = importlib.import_module("workflow_core.io")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            agents = project_root / "AGENTS.md"
            user_content = b"user swap after asset link\n"
            root_temp = project_root / (
                ".AGENTS.md.cvexp-" + "1" * 32 + ".tmp"
            )
            original_link = os.link

            def swap_after_link(
                source: object,
                destination: object,
                *,
                follow_symlinks: bool = True,
            ) -> None:
                original_link(
                    source,
                    destination,
                    follow_symlinks=follow_symlinks,
                )
                if Path(destination) == agents:
                    agents.unlink()
                    agents.write_bytes(user_content)
                    raise KeyboardInterrupt("interrupt after user swap")

            with mock.patch.object(
                workflow_io.os,
                "link",
                side_effect=swap_after_link,
            ):
                with self.assertRaises(BaseException) as captured:
                    project_module.init_project(project_root, "演示")

            self.assertIsInstance(captured.exception, RuntimeError)
            message = str(captured.exception)
            self.assertIn("所有权", message)
            self.assertIn("异常路径未自动清理", message)
            self.assertIn(str(root_temp), message)
            self.assertEqual(user_content, agents.read_bytes())
            self.assertTrue(root_temp.exists())
            self.assertFalse(
                (project_root / ".experiment-workflow.staging").exists()
            )
            self.assertFalse((project_root / ".experiment-workflow").exists())

    def test_staging_cleanup_failure_reports_original_error_and_residual_path(
        self,
    ) -> None:
        project_module = importlib.import_module("workflow_core.project")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            control = project_root / ".experiment-workflow"
            staging_paths: list[Path] = []

            def deny_staging_cleanup(
                path: object, ignore_errors: bool = False
            ) -> None:
                staging = Path(path)
                self.assertTrue(
                    staging.name == ".experiment-workflow.staging"
                )
                staging_paths.append(staging)
                if ignore_errors:
                    return
                raise PermissionError("staging cleanup denied")

            with mock.patch.object(
                project_module,
                "atomic_write_json",
                side_effect=OSError("disk full"),
            ), mock.patch.object(
                project_module.shutil,
                "rmtree",
                side_effect=deny_staging_cleanup,
            ):
                with self.assertRaises(Exception) as captured:
                    project_module.init_project(project_root, "演示")

            self.assertIsInstance(captured.exception, RuntimeError)
            message = str(captured.exception)
            self.assertIn("disk full", message)
            self.assertIn("staging cleanup denied", message)
            self.assertIn("清理失败", message)
            self.assertIn("残留路径", message)
            self.assertEqual(1, len(staging_paths))
            staging = staging_paths[0]
            self.assertIn(str(staging), message.split("残留路径：", 1)[1])
            self.assertTrue(staging.exists())
            self.assertFalse(control.exists())

    def test_final_rename_failure_restores_assets_and_allows_retry(self) -> None:
        project_module = importlib.import_module("workflow_core.project")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            control = project_root / ".experiment-workflow"
            timestamp = 1_000_000_000_000_000_000
            agents = project_root / "AGENTS.md"
            original_agents = b"original agents\n"
            agents.write_bytes(original_agents)
            os.utime(agents, ns=(timestamp, timestamp))

            original_replace = os.replace

            def fail_final_replace(source: object, target: object) -> None:
                source_path = Path(source)
                if (
                    source_path.name == ".experiment-workflow.staging"
                    and Path(target) == control
                ):
                    raise OSError("final rename failed")
                original_replace(source, target)

            with mock.patch.object(
                project_module.os,
                "replace",
                side_effect=fail_final_replace,
            ):
                with self.assertRaisesRegex(OSError, "final rename failed"):
                    project_module.init_project(project_root, "演示")

            self.assertFalse(control.exists())
            self.assertFalse(
                (project_root / ".experiment-workflow.staging").exists()
            )
            self.assertEqual(original_agents, agents.read_bytes())
            self.assertEqual(timestamp, agents.stat().st_mtime_ns)
            self.assertFalse((project_root / "WORKFLOW.md").exists())
            self.assertFalse((project_root / ".gitignore").exists())

            project_module.init_project(project_root, "重试")
            self.assertTrue(control.is_dir())

    def test_final_rename_failure_preserves_existing_hardlinks_and_ctime(self) -> None:
        project_module = importlib.import_module("workflow_core.project")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            backing_dir = Path(temp_dir) / "backing"
            backing_dir.mkdir()
            control = project_root / ".experiment-workflow"
            snapshots: dict[str, tuple[Path, bytes, int]] = {}
            for filename in ("AGENTS.md", "WORKFLOW.md", ".gitignore"):
                backing = backing_dir / filename.replace(".", "_")
                content = f"不可变：{filename}\n".encode("utf-8")
                backing.write_bytes(content)
                destination = project_root / filename
                os.link(backing, destination)
                snapshots[filename] = (
                    backing,
                    content,
                    destination.stat().st_ctime_ns,
                )

            original_replace = os.replace

            def fail_final_replace(source: object, target: object) -> None:
                if (
                    Path(source).name == ".experiment-workflow.staging"
                    and Path(target) == control
                ):
                    raise OSError("final rename failed")
                original_replace(source, target)

            with mock.patch.object(
                project_module.os,
                "replace",
                side_effect=fail_final_replace,
            ):
                with self.assertRaisesRegex(OSError, "final rename failed"):
                    project_module.init_project(project_root, "演示")

            for filename, (backing, content, ctime_ns) in snapshots.items():
                destination = project_root / filename
                self.assertTrue(os.path.samefile(backing, destination), filename)
                self.assertEqual(content, destination.read_bytes(), filename)
                self.assertEqual(ctime_ns, destination.stat().st_ctime_ns, filename)

    def test_failed_init_in_new_root_leaves_only_persistent_lock(self) -> None:
        project_module = importlib.import_module("workflow_core.project")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            control = project_root / ".experiment-workflow"

            with mock.patch.object(
                project_module,
                "atomic_write_json",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    project_module.init_project(project_root, "演示")

            self.assertFalse(control.exists())
            self.assertTrue(project_root.exists())
            lock = project_root / ".experiment-workflow.init.lock"
            self.assertEqual(
                b"cv-experiment-workflow.init-lock.v1\n",
                lock.read_bytes(),
            )
            self.assertEqual([lock], list(project_root.iterdir()))

    def test_root_creation_failure_is_not_misreported_as_cleanup_failure(self) -> None:
        project_module = importlib.import_module("workflow_core.project")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            original_mkdir = Path.mkdir

            def deny_root_creation(
                path: Path,
                mode: int = 0o777,
                parents: bool = False,
                exist_ok: bool = False,
            ) -> None:
                if path == project_root:
                    raise PermissionError("root creation denied")
                original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

            with mock.patch.object(Path, "mkdir", new=deny_root_creation):
                with self.assertRaisesRegex(
                    PermissionError, "root creation denied"
                ):
                    project_module.init_project(project_root, "演示")

            self.assertFalse(project_root.exists())

    def test_partial_root_creation_failure_does_not_delete_created_ancestors(
        self,
    ) -> None:
        project_module = importlib.import_module("workflow_core.project")
        with tempfile.TemporaryDirectory() as temp_dir:
            existing_parent = Path(temp_dir) / "existing"
            existing_parent.mkdir()
            new_outer = existing_parent / "new-outer"
            new_inner = new_outer / "new-inner"
            project_root = new_inner / "project"
            original_mkdir = Path.mkdir

            def partially_create_then_fail(
                path: Path,
                mode: int = 0o777,
                parents: bool = False,
                exist_ok: bool = False,
            ) -> None:
                if path == project_root:
                    original_mkdir(new_outer)
                    original_mkdir(new_inner)
                    raise PermissionError("root mkdir denied")
                original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

            with mock.patch.object(Path, "mkdir", new=partially_create_then_fail):
                with self.assertRaisesRegex(PermissionError, "root mkdir denied"):
                    project_module.init_project(project_root, "演示")

            self.assertTrue(existing_parent.is_dir())
            self.assertTrue(new_outer.is_dir())
            self.assertTrue(new_inner.is_dir())
            self.assertFalse(project_root.exists())


class CliErrorBoundaryTests(WorkflowCoreImportTests):
    def test_cli_serialization_error_uses_stderr_and_exit_two(self) -> None:
        rw = importlib.import_module("rw")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with mock.patch.object(rw, "init_project", return_value={"bad": {1}}):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = rw.main(["init", "--path", "unused", "--name", "演示"])

        self.assertEqual(2, exit_code)
        self.assertEqual("", stdout.getvalue())
        self.assertTrue(stderr.getvalue().strip())


if __name__ == "__main__":
    unittest.main()
