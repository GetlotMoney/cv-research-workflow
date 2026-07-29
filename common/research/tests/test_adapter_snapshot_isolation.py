from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

from tests._helpers import SCRIPTS


sys.path.insert(0, str(SCRIPTS))

from workflow_core import adapters as adapters_module  # noqa: E402


def _adapter_source(body: str = "return {'status': 'finished'}") -> bytes:
    return f"""\
def inspect(project):
    return {{}}


def validate(project):
    return []


def prepare_runs(project, task):
    return [{{}}]


def execute(project, action, run):
    {body}


def parse_result(project, run):
    return {{}}
""".encode("utf-8")


def _adapter_with_execute(imports: str, body: str) -> bytes:
    indented = "\n".join(f"    {line}" for line in body.splitlines())
    return f"""\
{imports}


def inspect(project):
    return {{}}


def validate(project):
    return []


def prepare_runs(project, task):
    return [{{}}]


def execute(project, action, run):
{indented}


def parse_result(project, run):
    return {{}}
""".encode("utf-8")


class AdapterSnapshotIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        (self.snapshot / ".cv-workflow-output").mkdir()

    def _load(self, source: bytes, *, bound_snapshot: bool = True):
        adapter_path = self.snapshot / "workflow_adapter.py"
        adapter_path.write_bytes(source)
        return adapters_module._load_adapter(
            {
                "source_bytes": source,
                "source": "workflow_adapter.py",
                "display_path": str(adapter_path),
                "repo_url": str(self.snapshot),
                "commit": "a" * 40,
                "import_root": str(self.snapshot),
                "sha256": "unused-by-loader",
            },
            bound_snapshot=bound_snapshot,
        )

    def test_windows_casefolded_live_module_cannot_replace_snapshot_module(
        self,
    ) -> None:
        (self.snapshot / "Foo.py").write_text(
            "VALUE = 'SNAPSHOT_MODULE'\n",
            encoding="utf-8",
        )
        source = b"""\
import foo


def inspect(project):
    return {"value": foo.VALUE}


def validate(project):
    return []


def prepare_runs(project, task):
    return [{}]


def execute(project, action, run):
    return {"status": "finished"}


def parse_result(project, run):
    return {}
"""
        live = types.ModuleType("foo")
        live.VALUE = "LIVE_SYS_MODULE"
        previous = sys.modules.get("foo")
        sys.modules["foo"] = live
        try:
            with self.assertRaisesRegex(ValueError, "Adapter|加载"):
                self._load(source)
            self.assertIs(live, sys.modules["foo"])
        finally:
            if previous is None:
                sys.modules.pop("foo", None)
            else:
                sys.modules["foo"] = previous

    def test_bound_adapter_direct_write_outside_output_root_is_blocked(
        self,
    ) -> None:
        escaped = self.snapshot.parent / "ESCAPED.txt"
        source = _adapter_source(
            "(project.parent / 'ESCAPED.txt').write_text("
            "'escaped\\n', encoding='utf-8'); "
            "return {'status': 'finished'}"
        )
        adapter = self._load(source)

        with self.assertRaisesRegex(
            (PermissionError, ValueError),
            "output|输出|write|写",
        ):
            adapter.execute(self.snapshot, "start", {})
        self.assertFalse(escaped.exists())

    def test_bound_adapter_can_write_inside_its_fixed_output_root(self) -> None:
        source = _adapter_source(
            "(project / '.cv-workflow-output' / 'ok.txt').write_text("
            "'ok\\n', encoding='utf-8'); "
            "return {'status': 'finished'}"
        )
        adapter = self._load(source)

        self.assertEqual(
            {"status": "finished"},
            adapter.execute(self.snapshot, "start", {}),
        )
        self.assertEqual(
            "ok\n",
            (
                self.snapshot
                / ".cv-workflow-output"
                / "ok.txt"
            ).read_text(encoding="utf-8"),
        )

    def test_unbound_adapter_keeps_existing_write_behavior(self) -> None:
        target = self.snapshot / "unbound.txt"
        source = _adapter_source(
            "(project / 'unbound.txt').write_text("
            "'ok\\n', encoding='utf-8'); "
            "return {'status': 'finished'}"
        )
        adapter = self._load(source, bound_snapshot=False)

        self.assertEqual(
            {"status": "finished"},
            adapter.execute(self.snapshot, "start", {}),
        )
        self.assertEqual("ok\n", target.read_text(encoding="utf-8"))

    def test_nfkc_casefolded_top_level_collision_is_rejected(self) -> None:
        (self.snapshot / "K.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.snapshot / "\N{KELVIN SIGN}.py").write_text(
            "VALUE = 2\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "NFKC|casefold|冲突"):
            self._load(_adapter_source())

    def test_unbound_adapter_does_not_enable_snapshot_collision_policy(
        self,
    ) -> None:
        (self.snapshot / "K.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.snapshot / "\N{KELVIN SIGN}.py").write_text(
            "VALUE = 2\n",
            encoding="utf-8",
        )

        adapter = self._load(_adapter_source(), bound_snapshot=False)
        self.assertEqual(
            {"status": "finished"},
            adapter.execute(self.snapshot, "start", {}),
        )

    def test_snapshot_module_without_an_origin_is_rejected(self) -> None:
        (self.snapshot / "probe.py").write_text(
            "VALUE = 'snapshot'\n",
            encoding="utf-8",
        )
        source = _adapter_with_execute(
            "import sys as _sys\nimport types as _types",
            "return {'status': 'finished'}",
        )
        source += b"\n_sys.modules['probe'] = _types.ModuleType('probe')\n"

        with self.assertRaisesRegex(ValueError, "origin|来源|加载失败"):
            self._load(source)

    def test_runtime_snapshot_module_origin_is_rechecked(self) -> None:
        (self.snapshot / "probe.py").write_text(
            "VALUE = 'snapshot'\n",
            encoding="utf-8",
        )
        source = _adapter_with_execute(
            "import sys as _sys\nimport types as _types",
            "\n".join([
                "_sys.modules['probe'] = _types.ModuleType('probe')",
                "return {'status': 'finished'}",
            ]),
        )
        adapter = self._load(source)

        with self.assertRaisesRegex(ImportError, "origin|来源"):
            adapter.execute(self.snapshot, "start", {})

    def test_bound_adapter_cannot_start_a_new_thread(self) -> None:
        escaped = self.snapshot.parent / "THREAD-ESCAPED.txt"
        source = _adapter_with_execute(
            "import threading",
            "\n".join([
                "worker = threading.Thread(",
                "    target=lambda: (project.parent / 'THREAD-ESCAPED.txt').write_text('escaped')",
                ")",
                "worker.start()",
                "worker.join()",
                "return {'status': 'finished'}",
            ]),
        )
        adapter = self._load(source)

        with self.assertRaisesRegex(PermissionError, "thread|线程"):
            adapter.execute(self.snapshot, "start", {})
        self.assertFalse(escaped.exists())

    def test_bound_adapter_copy_only_checks_write_target(self) -> None:
        source_file = self.snapshot.parent / "input.txt"
        source_file.write_text("input\n", encoding="utf-8")
        destination = self.snapshot / ".cv-workflow-output" / "copied.txt"
        source = _adapter_with_execute(
            "import shutil",
            "\n".join([
                "shutil.copyfile(",
                "    project.parent / 'input.txt',",
                "    project / '.cv-workflow-output' / 'copied.txt',",
                ")",
                "return {'status': 'finished'}",
            ]),
        )
        adapter = self._load(source)

        self.assertEqual(
            {"status": "finished"},
            adapter.execute(self.snapshot, "start", {}),
        )
        self.assertEqual("input\n", destination.read_text(encoding="utf-8"))

    def test_bound_adapter_cannot_remove_output_root_itself(self) -> None:
        source = _adapter_with_execute(
            "import shutil",
            "\n".join([
                "shutil.rmtree(project / '.cv-workflow-output')",
                "return {'status': 'finished'}",
            ]),
        )
        adapter = self._load(source)

        with self.assertRaisesRegex(PermissionError, "output|输出"):
            adapter.execute(self.snapshot, "start", {})
        self.assertTrue((self.snapshot / ".cv-workflow-output").is_dir())

    def test_bound_adapter_cannot_create_links_even_inside_output(self) -> None:
        source_file = self.snapshot / ".cv-workflow-output" / "source.txt"
        source_file.write_text("source\n", encoding="utf-8")
        source = _adapter_with_execute(
            "import os",
            "\n".join([
                "os.symlink(",
                "    project / '.cv-workflow-output' / 'source.txt',",
                "    project / '.cv-workflow-output' / 'linked.txt',",
                ")",
                "return {'status': 'finished'}",
            ]),
        )
        adapter = self._load(source)

        with self.assertRaisesRegex(PermissionError, "link|链接"):
            adapter.execute(self.snapshot, "start", {})
        self.assertFalse(
            os.path.lexists(
                self.snapshot / ".cv-workflow-output" / "linked.txt"
            )
        )

    @unittest.skipUnless(
        os.remove in os.supports_dir_fd,
        "当前平台不支持 remove(dir_fd=...)",
    )
    def test_bound_adapter_rejects_dir_fd_escape(self) -> None:
        escaped = self.snapshot.parent / "DIR-FD-ESCAPED.txt"
        escaped.write_text("sentinel\n", encoding="utf-8")
        source = _adapter_with_execute(
            "import os",
            "\n".join([
                "descriptor = os.open(project.parent, os.O_RDONLY)",
                "try:",
                "    os.remove('DIR-FD-ESCAPED.txt', dir_fd=descriptor)",
                "finally:",
                "    os.close(descriptor)",
                "return {'status': 'finished'}",
            ]),
        )
        adapter = self._load(source)

        with self.assertRaisesRegex(PermissionError, "dir_fd|目录描述符"):
            adapter.execute(self.snapshot, "start", {})
        self.assertEqual("sentinel\n", escaped.read_text(encoding="utf-8"))

    def test_bound_adapter_rejects_os_open_with_dir_fd(self) -> None:
        source = _adapter_with_execute(
            "import os",
            "\n".join([
                "os.open(",
                "    'escaped.txt',",
                "    os.O_WRONLY | os.O_CREAT,",
                "    dir_fd=3,",
                ")",
                "return {'status': 'finished'}",
            ]),
        )
        adapter = self._load(source)

        with self.assertRaisesRegex(PermissionError, "dir_fd|目录描述符"):
            adapter.execute(self.snapshot, "start", {})
        self.assertFalse((self.snapshot / "escaped.txt").exists())

    def test_fdopen_rejects_descriptor_replaced_with_external_file(
        self,
    ) -> None:
        escaped = self.snapshot.parent / "FD-REPLACED.txt"
        escaped.write_text("sentinel\n", encoding="utf-8")
        with escaped.open("r+b", buffering=0) as external:
            source = _adapter_with_execute(
                "import os",
                "\n".join([
                    "target = project / '.cv-workflow-output' / 'owned.txt'",
                    "descriptor = os.open(",
                    "    target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600",
                    ")",
                    "try:",
                    f"    os.dup2({external.fileno()}, descriptor)",
                    "    os.fdopen(descriptor, 'w', closefd=False)",
                    "finally:",
                    "    os.close(descriptor)",
                    "return {'blocked': False}",
                ]),
            )
            adapter = self._load(source)

            with self.assertRaisesRegex(
                PermissionError,
                "描述符|descriptor",
            ):
                adapter.execute(self.snapshot, "start", {})
        self.assertEqual("sentinel\n", escaped.read_text(encoding="utf-8"))

    def test_fdopen_rejects_closed_descriptor_number_reuse(self) -> None:
        escaped = self.snapshot.parent / "FD-REUSED.txt"
        escaped.write_text("sentinel\n", encoding="utf-8")
        with escaped.open("r+b", buffering=0) as external:
            source = _adapter_with_execute(
                "import os",
                "\n".join([
                    "target = project / '.cv-workflow-output' / 'owned.txt'",
                    "descriptor = os.open(",
                    "    target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600",
                    ")",
                    "os.close(descriptor)",
                    f"os.dup2({external.fileno()}, descriptor)",
                    "try:",
                    "    os.fdopen(descriptor, 'w', closefd=False)",
                    "except PermissionError:",
                    "    os.close(descriptor)",
                    "    return {'blocked': True}",
                    "os.close(descriptor)",
                    "return {'blocked': False}",
                ]),
            )
            adapter = self._load(source)

            with self.assertRaisesRegex(
                PermissionError,
                "描述符|descriptor",
            ):
                adapter.execute(self.snapshot, "start", {})
        self.assertEqual("sentinel\n", escaped.read_text(encoding="utf-8"))

    def test_fdopen_rejects_descriptor_rebind_after_file_object_created(
        self,
    ) -> None:
        escaped = self.snapshot.parent / "FD-AFTER-FDOPEN.txt"
        escaped.write_text("sentinel\n", encoding="utf-8")
        with escaped.open("r+b", buffering=0) as external:
            source = _adapter_with_execute(
                "import os",
                "\n".join([
                    "target = project / '.cv-workflow-output' / 'owned.txt'",
                    "descriptor = os.open(",
                    "    target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600",
                    ")",
                    "handle = os.fdopen(descriptor, 'w', closefd=False)",
                    "try:",
                    f"    os.dup2({external.fileno()}, descriptor)",
                    "    handle.write('ESCAPED\\n')",
                    "    handle.flush()",
                    "finally:",
                    "    handle.close()",
                    "    os.close(descriptor)",
                    "return {'blocked': False}",
                ]),
            )
            adapter = self._load(source)

            with self.assertRaisesRegex(
                PermissionError,
                "描述符|descriptor",
            ):
                adapter.execute(self.snapshot, "start", {})
        self.assertEqual("sentinel\n", escaped.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "nt.dup2 仅在 Windows 存在")
    def test_fdopen_rejects_nt_descriptor_rebind_after_file_object_created(
        self,
    ) -> None:
        escaped = self.snapshot.parent / "NT-FD-AFTER-FDOPEN.txt"
        escaped.write_text("sentinel\n", encoding="utf-8")
        with escaped.open("r+b", buffering=0) as external:
            source = _adapter_with_execute(
                "import nt\nimport os",
                "\n".join([
                    "target = project / '.cv-workflow-output' / 'owned-nt.txt'",
                    "descriptor = os.open(",
                    "    target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600",
                    ")",
                    "handle = os.fdopen(descriptor, 'w', closefd=False)",
                    "try:",
                    f"    nt.dup2({external.fileno()}, descriptor)",
                    "    handle.write('ESCAPED\\n')",
                    "    handle.flush()",
                    "finally:",
                    "    handle.close()",
                    "    os.close(descriptor)",
                    "return {'blocked': False}",
                ]),
            )
            adapter = self._load(source)

            with self.assertRaisesRegex(
                PermissionError,
                "描述符|descriptor",
            ):
                adapter.execute(self.snapshot, "start", {})
        self.assertEqual("sentinel\n", escaped.read_text(encoding="utf-8"))

    def test_bound_adapter_rejects_arbitrary_python_subprocess(self) -> None:
        escaped = self.snapshot.parent / "SUBPROCESS-ESCAPED.txt"
        source = _adapter_with_execute(
            "import subprocess\nimport sys",
            "\n".join([
                "subprocess.run([",
                "    sys.executable,",
                "    '-c',",
                "    \"import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('escaped')\",",
                "    str(project.parent / 'SUBPROCESS-ESCAPED.txt'),",
                "], check=True)",
                "return {'status': 'finished'}",
            ]),
        )
        adapter = self._load(source)

        with self.assertRaisesRegex(PermissionError, "subprocess|子进程"):
            adapter.execute(self.snapshot, "start", {})
        self.assertFalse(escaped.exists())

    def test_bound_adapter_rejects_direct_popen_even_for_snapshot_module(
        self,
    ) -> None:
        package = self.snapshot / "probe"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "main.py").write_text("", encoding="utf-8")
        source = _adapter_with_execute(
            "import subprocess\nimport sys",
            "\n".join([
                "subprocess.Popen(",
                "    [sys.executable, '-B', '-X', 'utf8', '-m', 'probe.main'],",
                "    cwd=project,",
                ")",
                "return {'status': 'finished'}",
            ]),
        )
        adapter = self._load(source)

        with self.assertRaisesRegex(PermissionError, "subprocess|子进程"):
            adapter.execute(self.snapshot, "start", {})

    def test_controlled_snapshot_subprocess_preserves_completed_result(self) -> None:
        package = self.snapshot / "probe"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "report.py").write_text(
            "\n".join([
                "import sys",
                "print('child-stdout')",
                "print('child-stderr', file=sys.stderr)",
                "raise SystemExit(7)",
                "",
            ]),
            encoding="utf-8",
        )
        source = _adapter_with_execute(
            "import subprocess\nimport sys",
            "\n".join([
                "completed = subprocess.run(",
                "    [sys.executable, '-B', '-X', 'utf8', '-m', 'probe.report'],",
                "    cwd=project,",
                "    capture_output=True,",
                "    text=True,",
                "    encoding='utf-8',",
                "    timeout=10.0,",
                "    check=False,",
                ")",
                "return {",
                "    'returncode': completed.returncode,",
                "    'stdout': completed.stdout.strip(),",
                "    'stderr': completed.stderr.strip(),",
                "}",
            ]),
        )
        adapter = self._load(source)

        self.assertEqual(
            {
                "returncode": 7,
                "stdout": "child-stdout",
                "stderr": "child-stderr",
            },
            adapter.execute(self.snapshot, "start", {}),
        )

    def test_controlled_snapshot_subprocess_preserves_timeout(self) -> None:
        package = self.snapshot / "probe"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "slow.py").write_text(
            "import time\ntime.sleep(5)\n",
            encoding="utf-8",
        )
        source = _adapter_with_execute(
            "import subprocess\nimport sys",
            "\n".join([
                "command = [",
                "    sys.executable, '-B', '-X', 'utf8', '-m', 'probe.slow'",
                "]",
                "try:",
                "    subprocess.run(",
                "        command,",
                "        cwd=project,",
                "        capture_output=True,",
                "        timeout=0.05,",
                "        check=False,",
                "    )",
                "except subprocess.TimeoutExpired as error:",
                "    return {",
                "        'timed_out': True,",
                "        'cmd_original': error.cmd == command,",
                "        'args_original': error.args[0] == command,",
                "    }",
                "return {",
                "    'timed_out': False,",
                "    'cmd_original': False,",
                "    'args_original': False,",
                "}",
            ]),
        )
        adapter = self._load(source)

        self.assertEqual(
            {
                "timed_out": True,
                "cmd_original": True,
                "args_original": True,
            },
            adapter.execute(self.snapshot, "start", {}),
        )
        self.assertEqual(
            [],
            list(
                (self.snapshot / ".cv-workflow-output").glob(
                    ".adapter-child-*"
                )
            ),
        )

    def test_controlled_snapshot_subprocess_preserves_checked_error(
        self,
    ) -> None:
        package = self.snapshot / "probe"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "fail.py").write_text(
            "raise SystemExit(7)\n",
            encoding="utf-8",
        )
        source = _adapter_with_execute(
            "import subprocess\nimport sys",
            "\n".join([
                "command = [",
                "    sys.executable, '-B', '-X', 'utf8', '-m', 'probe.fail'",
                "]",
                "try:",
                "    subprocess.run(",
                "        command,",
                "        cwd=project,",
                "        capture_output=True,",
                "        timeout=10.0,",
                "        check=True,",
                "    )",
                "except subprocess.CalledProcessError as error:",
                "    return {",
                "        'returncode': error.returncode,",
                "        'cmd_original': error.cmd == command,",
                "        'args_original': error.args[1] == command,",
                "    }",
                "return {",
                "    'returncode': 0,",
                "    'cmd_original': False,",
                "    'args_original': False,",
                "}",
            ]),
        )
        adapter = self._load(source)

        self.assertEqual(
            {
                "returncode": 7,
                "cmd_original": True,
                "args_original": True,
            },
            adapter.execute(self.snapshot, "start", {}),
        )
        self.assertEqual(
            [],
            list(
                (self.snapshot / ".cv-workflow-output").glob(
                    ".adapter-child-*"
                )
            ),
        )

    def test_controlled_snapshot_subprocess_uses_owned_ephemeral_temp(
        self,
    ) -> None:
        package = self.snapshot / "probe"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "temp_probe.py").write_text(
            "\n".join([
                "import pathlib",
                "import tempfile",
                "root = pathlib.Path(tempfile.gettempdir())",
                "(root / 'cache.txt').write_text('cache\\n')",
                "print(root)",
                "",
            ]),
            encoding="utf-8",
        )
        source = _adapter_with_execute(
            "import subprocess\nimport sys",
            "\n".join([
                "completed = subprocess.run(",
                "    [sys.executable, '-B', '-X', 'utf8', '-m', 'probe.temp_probe'],",
                "    cwd=project,",
                "    capture_output=True,",
                "    text=True,",
                "    encoding='utf-8',",
                "    timeout=10.0,",
                "    check=True,",
                ")",
                "return {'temp': completed.stdout.strip()}",
            ]),
        )
        adapter = self._load(source)

        reported = Path(
            adapter.execute(self.snapshot, "start", {})["temp"]
        )
        self.assertEqual(
            (self.snapshot / ".cv-workflow-output").resolve(),
            reported.parent.resolve(),
        )
        self.assertFalse(reported.exists())
        self.assertEqual(
            [],
            list(
                (self.snapshot / ".cv-workflow-output").glob(
                    ".adapter-child-*"
                )
            ),
        )

    def test_controlled_snapshot_subprocess_cannot_move_owned_temp_root(
        self,
    ) -> None:
        package = self.snapshot / "probe"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "move_temp.py").write_text(
            "\n".join([
                "import pathlib",
                "import tempfile",
                "root = pathlib.Path(tempfile.gettempdir())",
                "root.rename(root.parent / 'retained-child-temp')",
                "",
            ]),
            encoding="utf-8",
        )
        source = _adapter_with_execute(
            "import subprocess\nimport sys",
            "\n".join([
                "subprocess.run(",
                "    [sys.executable, '-B', '-X', 'utf8', '-m', 'probe.move_temp'],",
                "    cwd=project,",
                "    capture_output=True,",
                "    text=True,",
                "    encoding='utf-8',",
                "    timeout=10.0,",
                "    check=True,",
                ")",
                "return {'status': 'finished'}",
            ]),
        )
        adapter = self._load(source)

        with self.assertRaises(Exception):
            adapter.execute(self.snapshot, "start", {})
        output = self.snapshot / ".cv-workflow-output"
        self.assertFalse((output / "retained-child-temp").exists())
        self.assertEqual([], list(output.glob(".adapter-child-*")))

    def test_controlled_snapshot_subprocess_cannot_move_temp_contents_out(
        self,
    ) -> None:
        package = self.snapshot / "probe"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "move_temp_content.py").write_text(
            "\n".join([
                "import pathlib",
                "import tempfile",
                "root = pathlib.Path(tempfile.gettempdir())",
                "cache = root / 'cache'",
                "cache.mkdir()",
                "(cache / 'escaped.txt').write_text('escaped\\n')",
                "cache.rename(root.parent / 'retained-child-cache')",
                "",
            ]),
            encoding="utf-8",
        )
        source = _adapter_with_execute(
            "import subprocess\nimport sys",
            "\n".join([
                "subprocess.run(",
                "    [",
                "        sys.executable, '-B', '-X', 'utf8',",
                "        '-m', 'probe.move_temp_content',",
                "    ],",
                "    cwd=project,",
                "    capture_output=True,",
                "    text=True,",
                "    encoding='utf-8',",
                "    timeout=10.0,",
                "    check=True,",
                ")",
                "return {'status': 'finished'}",
            ]),
        )
        adapter = self._load(source)

        with self.assertRaises(Exception):
            adapter.execute(self.snapshot, "start", {})
        output = self.snapshot / ".cv-workflow-output"
        self.assertFalse((output / "retained-child-cache").exists())
        self.assertEqual([], list(output.glob(".adapter-child-*")))

    def test_controlled_snapshot_subprocess_can_write_output(self) -> None:
        package = self.snapshot / "probe"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "write_output.py").write_text(
            "\n".join([
                "import pathlib",
                "import sys",
                "pathlib.Path(sys.argv[1]).write_text('child-output\\n')",
                "",
            ]),
            encoding="utf-8",
        )
        output = self.snapshot / ".cv-workflow-output" / "child.txt"
        source = _adapter_with_execute(
            "import subprocess\nimport sys",
            "\n".join([
                "subprocess.run(",
                "    [",
                "        sys.executable, '-B', '-X', 'utf8',",
                "        '-m', 'probe.write_output',",
                "        str(project / '.cv-workflow-output' / 'child.txt'),",
                "    ],",
                "    cwd=project,",
                "    capture_output=True,",
                "    text=True,",
                "    encoding='utf-8',",
                "    timeout=10.0,",
                "    check=True,",
                ")",
                "return {'status': 'finished'}",
            ]),
        )
        adapter = self._load(source)

        self.assertEqual(
            {"status": "finished"},
            adapter.execute(self.snapshot, "start", {}),
        )
        self.assertEqual("child-output\n", output.read_text(encoding="utf-8"))

    def test_controlled_snapshot_subprocess_cannot_write_parent(self) -> None:
        package = self.snapshot / "probe"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "escape.py").write_text(
            "\n".join([
                "import pathlib",
                "import sys",
                "pathlib.Path(sys.argv[1]).write_text('escaped\\n')",
                "",
            ]),
            encoding="utf-8",
        )
        escaped = self.snapshot.parent / "CHILD-ESCAPED.txt"
        source = _adapter_with_execute(
            "import subprocess\nimport sys",
            "\n".join([
                "subprocess.run(",
                "    [",
                "        sys.executable, '-B', '-X', 'utf8',",
                "        '-m', 'probe.escape',",
                "        str(project.parent / 'CHILD-ESCAPED.txt'),",
                "    ],",
                "    cwd=project,",
                "    capture_output=True,",
                "    text=True,",
                "    encoding='utf-8',",
                "    timeout=10.0,",
                "    check=True,",
                ")",
                "return {'status': 'finished'}",
            ]),
        )
        adapter = self._load(source)

        with self.assertRaises(Exception):
            adapter.execute(self.snapshot, "start", {})
        self.assertFalse(escaped.exists())

    def test_controlled_snapshot_subprocess_rejects_nested_process(self) -> None:
        package = self.snapshot / "probe"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "nested.py").write_text(
            "\n".join([
                "import subprocess",
                "import sys",
                "subprocess.run([sys.executable, '-c', \"print('nested')\"], check=True)",
                "",
            ]),
            encoding="utf-8",
        )
        source = _adapter_with_execute(
            "import subprocess\nimport sys",
            "\n".join([
                "completed = subprocess.run(",
                "    [sys.executable, '-B', '-X', 'utf8', '-m', 'probe.nested'],",
                "    cwd=project,",
                "    capture_output=True,",
                "    text=True,",
                "    encoding='utf-8',",
                "    timeout=10.0,",
                "    check=False,",
                ")",
                "return {'returncode': completed.returncode}",
            ]),
        )
        adapter = self._load(source)

        result = adapter.execute(self.snapshot, "start", {})
        self.assertNotEqual(0, result["returncode"])


if __name__ == "__main__":
    unittest.main()
