from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.framework_intake import (
    DraftPreparationError,
    FrameworkIntakeError,
    ScanLimitExceeded,
    prepare_standardization_draft,
    scan_gzsl_repository,
)


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {
    "data",
    "model",
    "losses",
    "trainer",
    "evaluator",
    "inferencer",
    "metrics",
    "configs",
}


def _git(*arguments: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def _make_repository(parent: Path) -> Path:
    repository = parent / "source"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "tests@example.invalid", cwd=repository)
    _git("config", "user.name", "Framework Intake Tests", cwd=repository)
    files = {
        "README.md": "# Tiny GZSL example\n",
        "train.py": "def main():\n    return 'train'\n",
        "evaluate.py": "def evaluate():\n    return {}\n",
        "datasets/loader.py": "class DatasetLoader:\n    pass\n",
        "models/gzsl_model.py": "class GZSLModel:\n    pass\n",
        "losses.py": "def classification_loss():\n    return 0\n",
        "metrics.py": "def harmonic_mean():\n    return 0\n",
        "configs/default.yaml": "epochs: 10\n",
    }
    for relative, content in files.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git("add", ".", cwd=repository)
    _git("commit", "-m", "initial fixture", cwd=repository)
    return repository


def _tree_digest(repository: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (
            item
            for item in repository.rglob("*")
            if item.is_file() and ".git" not in item.parts
        ),
        key=lambda item: item.as_posix(),
    ):
        digest.update(path.relative_to(repository).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


class FrameworkRepositoryScanTests(unittest.TestCase):
    def test_scan_reports_git_state_entrypoints_and_all_component_groups(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as raw:
            repository = _make_repository(Path(raw))

            result = scan_gzsl_repository(repository)

            self.assertTrue(result["repository_clean"])
            self.assertEqual(_git("rev-parse", "HEAD", cwd=repository), result["commit"])
            self.assertEqual(COMPONENTS, set(result["components"]))
            self.assertIn(
                "train.py",
                [item["path"] for item in result["candidate_entrypoints"]],
            )
            self.assertIn(
                "datasets/loader.py",
                result["components"]["data"]["candidates"],
            )
            for component in result["components"].values():
                self.assertTrue(component["explanation"])
            self.assertFalse(result["semantic_validation"]["passed"])
            self.assertIn("候选", result["semantic_validation"]["message"])

    def test_scan_reports_dirty_repository_without_reading_git_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as raw:
            repository = _make_repository(Path(raw))
            (repository / "train.py").write_text("changed\n", encoding="utf-8")

            result = scan_gzsl_repository(repository)

            self.assertFalse(result["repository_clean"])
            self.assertTrue(
                all(not path.startswith(".git") for path in result["scanned_paths"])
            )

    def test_scan_fails_closed_when_file_or_byte_limit_is_exceeded(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as raw:
            repository = _make_repository(Path(raw))

            with self.assertRaises(ScanLimitExceeded):
                scan_gzsl_repository(repository, max_files=2)
            with self.assertRaises(ScanLimitExceeded):
                scan_gzsl_repository(repository, max_read_bytes=8)
            with self.assertRaises(ScanLimitExceeded):
                scan_gzsl_repository(repository, max_total_bytes=8)

    def test_scan_skips_secret_binary_weight_data_and_large_files(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as raw:
            repository = _make_repository(Path(raw))
            additions = {
                ".env": b"TOKEN=do-not-read\n",
                "private.pem": b"secret-key\n",
                "checkpoint.pt": b"weights",
                "samples.csv": b"label,value\n1,2\n",
                "binary.bin": b"\x00\x01\x02",
                "huge.txt": b"x" * 128,
            }
            for name, content in additions.items():
                (repository / name).write_bytes(content)

            result = scan_gzsl_repository(repository, max_file_bytes=64)

            excluded = {item["path"]: item["reason"] for item in result["excluded"]}
            self.assertIn(".env", excluded)
            self.assertIn("private.pem", excluded)
            self.assertIn("checkpoint.pt", excluded)
            self.assertIn("samples.csv", excluded)
            self.assertIn("binary.bin", excluded)
            self.assertIn("huge.txt", excluded)
            self.assertNotIn("TOKEN=do-not-read", json.dumps(result))

    def test_scan_rejects_non_repository_and_repository_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as raw:
            parent = Path(raw)
            repository = _make_repository(parent)

            with self.assertRaises(FrameworkIntakeError):
                scan_gzsl_repository(parent)
            with self.assertRaises(FrameworkIntakeError):
                scan_gzsl_repository(repository / "models")

    def test_scan_does_not_run_repository_configured_fsmonitor(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as raw:
            parent = Path(raw)
            repository = _make_repository(parent)
            marker = parent / "fsmonitor-was-run"
            monitor = repository / "malicious_monitor.py"
            monitor.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
                encoding="utf-8",
            )
            _git("add", "malicious_monitor.py", cwd=repository)
            _git("commit", "-m", "add monitor fixture", cwd=repository)
            _git(
                "config",
                "core.fsmonitor",
                f'python "{monitor}"',
                cwd=repository,
            )

            scan_gzsl_repository(repository)

            self.assertFalse(marker.exists())

    def test_scan_and_draft_reject_link_or_reparse_point(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as raw:
            parent = Path(raw)
            repository = _make_repository(parent)
            inbox = parent / "inbox"
            inbox.mkdir()
            outside = parent / "outside.py"
            outside.write_text("outside = True\n", encoding="utf-8")
            link = repository / "linked.py"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"当前系统不允许创建测试符号链接：{exc}")
            _git("add", "linked.py", cwd=repository)
            _git("commit", "-m", "add link fixture", cwd=repository)

            with self.assertRaisesRegex(FrameworkIntakeError, "链接|重解析点"):
                scan_gzsl_repository(repository)
            with self.assertRaisesRegex(FrameworkIntakeError, "链接|重解析点"):
                prepare_standardization_draft(repository, inbox, "linked")
            self.assertFalse((inbox / "linked").exists())


class StandardizationDraftTests(unittest.TestCase):
    def test_prepare_clones_into_inbox_and_keeps_source_unchanged(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as raw:
            parent = Path(raw)
            repository = _make_repository(parent)
            inbox = parent / "inbox"
            inbox.mkdir()
            before_digest = _tree_digest(repository)
            before_status = _git("status", "--porcelain", cwd=repository)

            result = prepare_standardization_draft(
                repository,
                inbox,
                "tiny-gzsl",
                component_map={
                    "model": [
                        "models/gzsl_model.py",
                        "models/gzsl_model.py",
                    ],
                    "data": ["datasets/loader.py"],
                },
            )

            draft = inbox / "tiny-gzsl"
            self.assertEqual(str(draft.resolve()), result["draft_root"])
            self.assertTrue((draft / ".git").exists())
            self.assertEqual(before_digest, _tree_digest(repository))
            self.assertEqual(before_status, _git("status", "--porcelain", cwd=repository))
            self.assertEqual(
                _git("rev-parse", "HEAD", cwd=repository),
                _git("rev-parse", "HEAD", cwd=draft),
            )
            standardization = draft / "standardization"
            for name in (
                "scan.json",
                "component-map.json",
                "README.md",
                "workflow_adapter.py",
            ):
                self.assertTrue((standardization / name).is_file(), name)
            component_payload = json.loads(
                (standardization / "component-map.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                ["models/gzsl_model.py"],
                component_payload["components"]["model"]["selected"],
            )
            self.assertEqual(
                ["datasets/loader.py"],
                component_payload["components"]["data"]["selected"],
            )
            adapter = (standardization / "workflow_adapter.py").read_text(
                encoding="utf-8"
            )
            self.assertIn("STANDARDIZATION_COMPLETE = False", adapter)
            self.assertIn("raise StandardizationIncompleteError", adapter)
            readme = (standardization / "README.md").read_text(encoding="utf-8")
            self.assertIn("未完成", readme)
            self.assertIn("不能", readme)

    def test_prepare_applies_configurable_clone_file_and_byte_limits(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as raw:
            parent = Path(raw)
            repository = _make_repository(parent)
            inbox = parent / "inbox"
            inbox.mkdir()

            with self.assertRaises(ScanLimitExceeded):
                prepare_standardization_draft(
                    repository,
                    inbox,
                    "too-many-files",
                    max_clone_files=2,
                )
            with self.assertRaises(ScanLimitExceeded):
                prepare_standardization_draft(
                    repository,
                    inbox,
                    "too-many-bytes",
                    max_clone_total_bytes=8,
                )

            self.assertFalse((inbox / "too-many-files").exists())
            self.assertFalse((inbox / "too-many-bytes").exists())

    def test_clone_failure_preserves_partial_target_with_explicit_status(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as raw:
            parent = Path(raw)
            repository = _make_repository(parent)
            inbox = parent / "inbox"
            inbox.mkdir()
            target = inbox / "failed-clone"
            original_run = subprocess.run

            def fail_clone(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if "clone" not in arguments:
                    return original_run(arguments, **kwargs)
                target.mkdir()
                (target / "clone.stderr").write_text(
                    "partial clone retained\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(
                    arguments,
                    returncode=1,
                    stdout="",
                    stderr="simulated clone failure",
                )

            with mock.patch(
                "app.framework_intake.subprocess.run",
                side_effect=fail_clone,
            ):
                with self.assertRaises(DraftPreparationError) as raised:
                    prepare_standardization_draft(
                        repository,
                        inbox,
                        "failed-clone",
                    )

            self.assertEqual("draft_failed_preserved", raised.exception.status)
            self.assertEqual(str(target), raised.exception.draft_root)
            self.assertIn("失败现场已保留", str(raised.exception))
            self.assertTrue((target / "clone.stderr").is_file())

    def test_post_clone_limit_failure_preserves_target_and_marks_status(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as raw:
            parent = Path(raw)
            repository = _make_repository(parent)
            inbox = parent / "inbox"
            inbox.mkdir()
            target = inbox / "grew-during-clone"
            original_run = subprocess.run

            def grow_after_clone(
                arguments: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                completed = original_run(arguments, **kwargs)
                if "clone" in arguments and completed.returncode == 0:
                    (target / "unexpected.bin").write_bytes(b"x" * 4096)
                return completed

            with mock.patch(
                "app.framework_intake.subprocess.run",
                side_effect=grow_after_clone,
            ):
                with self.assertRaises(DraftPreparationError) as raised:
                    prepare_standardization_draft(
                        repository,
                        inbox,
                        "grew-during-clone",
                        max_clone_total_bytes=2048,
                    )

            self.assertEqual("draft_failed_preserved", raised.exception.status)
            self.assertTrue((target / "unexpected.bin").is_file())
            self.assertIn("体积", str(raised.exception))

    def test_prepare_rejects_missing_inbox_existing_target_and_path_escape(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as raw:
            parent = Path(raw)
            repository = _make_repository(parent)
            inbox = parent / "inbox"
            inbox.mkdir()
            (inbox / "existing").mkdir()

            with self.assertRaises(FileNotFoundError):
                prepare_standardization_draft(
                    repository, parent / "missing", "draft"
                )
            with self.assertRaises(FileExistsError):
                prepare_standardization_draft(repository, inbox, "existing")
            with self.assertRaises(FrameworkIntakeError):
                prepare_standardization_draft(repository, inbox, "../outside")
            with self.assertRaises(FrameworkIntakeError):
                prepare_standardization_draft(
                    repository,
                    inbox,
                    "draft",
                    component_map={"model": "../outside.py"},
                )

    def test_prepare_rejects_dirty_repository_and_secret_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as raw:
            parent = Path(raw)
            repository = _make_repository(parent)
            inbox = parent / "inbox"
            inbox.mkdir()
            (repository / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

            with self.assertRaisesRegex(FrameworkIntakeError, "干净"):
                prepare_standardization_draft(repository, inbox, "dirty")

            (repository / "uncommitted.txt").unlink()
            (repository / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            _git("add", ".env", cwd=repository)
            _git("commit", "-m", "tracked secret fixture", cwd=repository)
            with self.assertRaisesRegex(FrameworkIntakeError, "秘密"):
                prepare_standardization_draft(repository, inbox, "secret")
            self.assertFalse((inbox / "secret").exists())


if __name__ == "__main__":
    unittest.main()
