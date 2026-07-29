from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests._helpers import ROOT, SCRIPTS, cli_json, read_json, run_cli

sys.path.insert(0, str(SCRIPTS))

from workflow_core.codebases import (  # noqa: E402
    CODEBASE_SCHEMA,
    load_codebase_locked,
    register_codebase,
    validate_codebase,
    validate_codebases_locked,
    verify_codebase_gate,
)
from workflow_core.project import init_project  # noqa: E402


class CodebaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        init_project(self.project, "codebase-tests", layout="v2")
        self.control = self.project / ".experiment-workflow"
        self.repo = self._new_repo("repo")
        self.commit = self._git(self.repo, "rev-parse", "HEAD")

    def _new_repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir()
        self._git(repo, "init", "-b", "main")
        self._git(repo, "config", "user.name", "Test User")
        self._git(repo, "config", "user.email", "test@example.com")
        (repo / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "initial")
        return repo

    def _git(self, repo: Path, *arguments: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if check:
            self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def _manifest(
        self,
        name: str,
        *,
        repo: Path | None = None,
        branch: str = "main",
        commit: str | None = None,
        tag: str | None = None,
        direction: str = "cls",
        source: str = "domain_pack",
    ) -> Path:
        selected_repo = self.repo if repo is None else repo
        payload = {
            "schema": CODEBASE_SCHEMA,
            "name": name,
            "primary_direction": direction,
            "repo_path": str(selected_repo.resolve()),
            "source": source,
            "template_id": "PACK-CLS",
            "template_version": "1.0.0",
            "default_branch": branch,
            "initial_commit": self.commit if commit is None else commit,
            "initial_tag": tag,
        }
        path = self.root / f"{name}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def test_register_persists_minimal_immutable_record_and_gate_rechecks(self) -> None:
        from workflow_core import codebases

        with mock.patch.object(
            codebases,
            "_verify_repository_identity",
            wraps=codebases._verify_repository_identity,
        ) as verify:
            record = register_codebase(
                self.project,
                self._manifest("classification"),
                self.repo,
            )

        self.assertEqual(2, verify.call_count)
        self.assertEqual("CB-0001", record["id"])
        self.assertEqual(
            {
                "schema",
                "id",
                "name",
                "primary_direction",
                "repo_path",
                "source",
                "template_id",
                "template_version",
                "default_branch",
                "initial_commit",
                "initial_tag",
                "created_at",
            },
            set(record),
        )
        self.assertEqual(
            record,
            read_json(self.control / "codebases" / "CB-0001.json"),
        )
        validate_codebase(record, expected_id="CB-0001")
        self.assertEqual(
            record,
            load_codebase_locked(self.control, "CB-0001"),
        )
        self.assertEqual(
            {"CB-0001": record},
            validate_codebases_locked(self.control),
        )
        gate = verify_codebase_gate(self.project, "CB-0001")
        self.assertEqual("pass", gate["status"])
        self.assertEqual("CB-0001", gate["codebase_id"])
        self.assertEqual(self.commit, gate["commit"])
        self.assertEqual("baseline", gate["verification_scope"])
        self.assertFalse(gate["clean_checked"])

    def test_old_v2_without_codebases_stays_valid_and_new_records_are_strict(self) -> None:
        self.assertFalse((self.control / "codebases").exists())
        self.assertEqual(
            {"sources": 0, "ideas": 0, "templates": 0, "modules": 0},
            cli_json("validate", "--project", self.project),
        )
        record = register_codebase(
            self.project,
            self._manifest("strict-record"),
            self.repo,
        )
        self.assertEqual(
            {"sources": 0, "ideas": 0, "templates": 0, "modules": 0},
            cli_json("validate", "--project", self.project),
        )
        path = self.control / "codebases" / f"{record['id']}.json"
        tampered = read_json(path)
        tampered["unexpected"] = True
        path.write_text(json.dumps(tampered), encoding="utf-8")
        result = run_cli("validate", "--project", self.project, check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Codebase", result.stderr)

    def test_wrong_repo_branch_commit_or_tag_fails_without_record(self) -> None:
        other = self._new_repo("other")
        cases = [
            (
                self._manifest("wrong-repo", repo=self.repo),
                other,
                "repo_path",
            ),
            (
                self._manifest("wrong-branch", branch="dev"),
                self.repo,
                "branch",
            ),
            (
                self._manifest("wrong-commit", commit="0" * 40),
                self.repo,
                "commit",
            ),
            (
                self._manifest("wrong-tag", tag="missing-v1"),
                self.repo,
                "tag",
            ),
        ]
        for manifest, repo, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    register_codebase(self.project, manifest, repo)
                directory = self.control / "codebases"
                self.assertFalse(directory.exists() and any(directory.iterdir()))

    def test_detached_or_dirty_repository_fails_closed(self) -> None:
        self._git(self.repo, "checkout", "--detach")
        with self.assertRaisesRegex(ValueError, "detached"):
            register_codebase(
                self.project,
                self._manifest("detached"),
                self.repo,
            )
        self._git(self.repo, "switch", "main")
        (self.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "clean"):
            register_codebase(
                self.project,
                self._manifest("dirty"),
                self.repo,
            )

    def test_link_or_reparse_repository_is_rejected(self) -> None:
        link = self.root / "repo-link"
        try:
            link.symlink_to(self.repo, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"当前主机不能创建目录链接：{error}")
        manifest = self._manifest("linked", repo=link)
        with self.assertRaisesRegex(ValueError, "link|reparse"):
            register_codebase(self.project, manifest, link)

        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(self.root, target_is_directory=True)
        repo_via_linked_parent = linked_parent / self.repo.name
        manifest = self._manifest("linked-parent", repo=repo_via_linked_parent)
        with self.assertRaisesRegex(ValueError, "link|reparse"):
            register_codebase(
                self.project,
                manifest,
                repo_via_linked_parent,
            )

    def test_clean_gate_never_ignores_tracked_control_code_or_external_ledger(
        self,
    ) -> None:
        hidden = self.repo / ".experiment-workflow"
        hidden.mkdir()
        control_code = hidden / "train.py"
        control_code.write_text("VALUE = 1\n", encoding="utf-8")
        self._git(self.repo, "add", ".experiment-workflow/train.py")
        self._git(self.repo, "commit", "-m", "track control state")
        self.commit = self._git(self.repo, "rev-parse", "HEAD")
        control_code.write_text("VALUE = 2\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "clean"):
            register_codebase(
                self.project,
                self._manifest("tracked-control-code"),
                self.repo,
            )

        external = self._new_repo("external-ledger")
        external_commit = self._git(external, "rev-parse", "HEAD")
        (external / ".experiment-workflow").mkdir()
        (external / ".experiment-workflow" / "state.json").write_text(
            "{}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "clean"):
            register_codebase(
                self.project,
                self._manifest(
                    "external-untracked-ledger",
                    repo=external,
                    commit=external_commit,
                ),
                external,
            )

    def test_same_repo_allows_only_untracked_non_executable_ledger_files(
        self,
    ) -> None:
        same_repo = self._new_repo("same-repo")
        init_project(same_repo, "same-repo-project", layout="v2")
        self._git(
            same_repo,
            "add",
            ".gitignore",
            "AGENTS.md",
            "SKILL.md",
            "WORKFLOW.md",
        )
        self._git(same_repo, "commit", "-m", "track project entry files")
        same_commit = self._git(same_repo, "rev-parse", "HEAD")

        record = register_codebase(
            same_repo,
            self._manifest(
                "same-repo-ledger",
                repo=same_repo,
                commit=same_commit,
            ),
            same_repo,
        )

        self.assertEqual("CB-0001", record["id"])

    def test_atomic_target_collision_fails_and_concurrent_ids_do_not_overwrite(self) -> None:
        from workflow_core import codebases

        manifest = self._manifest("collision")
        with mock.patch.object(codebases, "atomic_create_json", return_value=False):
            with self.assertRaisesRegex(FileExistsError, "已存在"):
                register_codebase(self.project, manifest, self.repo)
        self.assertEqual([], list((self.control / "codebases").iterdir()))

        results: list[str] = []
        errors: list[BaseException] = []

        def register(name: str) -> None:
            try:
                payload = register_codebase(
                    self.project,
                    self._manifest(name),
                    self.repo,
                )
                results.append(payload["id"])
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = [
            threading.Thread(target=register, args=(name,))
            for name in ("concurrent-a", "concurrent-b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(["CB-0001", "CB-0002"], sorted(results))
        self.assertEqual(
            ["CB-0001.json", "CB-0002.json"],
            sorted(path.name for path in (self.control / "codebases").iterdir()),
        )

    def test_register_and_verify_cli(self) -> None:
        record = cli_json(
            "register-codebase",
            "--project",
            self.project,
            "--manifest",
            self._manifest("cli"),
            "--repo-root",
            self.repo,
        )
        self.assertEqual("CB-0001", record["id"])
        self._git(self.repo, "switch", "-c", "cli-experiment")
        (self.repo / "README.md").write_text(
            "# cli experiment\n",
            encoding="utf-8",
        )
        self._git(self.repo, "add", "README.md")
        self._git(self.repo, "commit", "-m", "cli experiment")
        experiment_commit = self._git(self.repo, "rev-parse", "HEAD")
        gate = cli_json(
            "verify-codebase",
            "--project",
            self.project,
            "--codebase",
            record["id"],
        )
        self.assertEqual("pass", gate["status"])
        self.assertEqual(record["id"], gate["codebase_id"])
        self.assertEqual(experiment_commit, gate["commit"])
        self.assertEqual("baseline", gate["verification_scope"])
        self.assertFalse(gate["clean_checked"])
        exact_gate = cli_json(
            "verify-codebase",
            "--project",
            self.project,
            "--codebase",
            record["id"],
            "--expected-git",
            json.dumps({
                "branch": "cli-experiment",
                "commit": experiment_commit,
                "tag": None,
                "require_clean": True,
            }),
        )
        self.assertEqual("pass", exact_gate["status"])
        self.assertEqual(experiment_commit, exact_gate["commit"])
        self.assertEqual("current_checkout", exact_gate["verification_scope"])
        self.assertTrue(exact_gate["clean_checked"])

    def test_baseline_gate_survives_new_checkout_and_expected_git_is_exact(
        self,
    ) -> None:
        record = register_codebase(
            self.project,
            self._manifest("evolving-codebase"),
            self.repo,
        )
        self._git(self.repo, "switch", "-c", "experiment")
        (self.repo / "README.md").write_text("# experiment\n", encoding="utf-8")
        self._git(self.repo, "add", "README.md")
        self._git(self.repo, "commit", "-m", "experiment")
        experiment_commit = self._git(self.repo, "rev-parse", "HEAD")
        self._git(self.repo, "tag", "experiment-v1")

        baseline = verify_codebase_gate(self.project, record["id"])
        self.assertEqual(experiment_commit, baseline["commit"])
        self.assertEqual("baseline", baseline["verification_scope"])
        self.assertFalse(baseline["clean_checked"])
        expected = {
            "branch": "experiment",
            "commit": experiment_commit,
            "tag": "experiment-v1",
            "require_clean": True,
        }
        exact = verify_codebase_gate(
            self.project,
            record["id"],
            expected_git=expected,
        )
        self.assertEqual(experiment_commit, exact["commit"])
        self.assertEqual("current_checkout", exact["verification_scope"])
        self.assertTrue(exact["clean_checked"])

        with self.assertRaisesRegex(ValueError, "branch"):
            verify_codebase_gate(
                self.project,
                record["id"],
                expected_git={**expected, "branch": "main"},
            )
        with self.assertRaisesRegex(ValueError, "commit"):
            verify_codebase_gate(
                self.project,
                record["id"],
                expected_git={**expected, "commit": record["initial_commit"]},
            )
        with self.assertRaisesRegex(ValueError, "tag"):
            verify_codebase_gate(
                self.project,
                record["id"],
                expected_git={**expected, "tag": "missing-tag"},
            )

        (self.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        self.assertEqual(
            "pass",
            verify_codebase_gate(self.project, record["id"])["status"],
        )
        dirty_allowed = verify_codebase_gate(
            self.project,
            record["id"],
            expected_git={**expected, "require_clean": False},
        )
        self.assertEqual("pass", dirty_allowed["status"])
        self.assertEqual(
            "current_checkout",
            dirty_allowed["verification_scope"],
        )
        self.assertFalse(dirty_allowed["clean_checked"])
        with self.assertRaisesRegex(ValueError, "clean"):
            verify_codebase_gate(
                self.project,
                record["id"],
                expected_git=expected,
            )

    def test_git_verification_disables_malicious_fsmonitor(self) -> None:
        git_dir = self.repo / ".git"
        marker = git_dir / "fsmonitor-ran"
        hook = git_dir / "malicious-fsmonitor.sh"
        hook.write_text(
            "#!/bin/sh\n"
            'printf invoked > "$(dirname "$0")/fsmonitor-ran"\n'
            'printf "%s\\n" "$2"\n',
            encoding="utf-8",
        )
        hook.chmod(0o755)
        self._git(self.repo, "config", "core.fsmonitor", hook.as_posix())

        record = register_codebase(
            self.project,
            self._manifest("no-fsmonitor"),
            self.repo,
        )
        verify_codebase_gate(self.project, record["id"])

        self.assertFalse(marker.exists(), "Git 核验执行了仓库配置的 fsmonitor")

    def test_git_verification_does_not_refresh_index_metadata(self) -> None:
        record = register_codebase(
            self.project,
            self._manifest("metadata-read-only"),
            self.repo,
        )
        tracked = self.repo / "README.md"
        tracked_stat = tracked.stat()
        os.utime(
            tracked,
            ns=(
                tracked_stat.st_atime_ns,
                tracked_stat.st_mtime_ns + 2_000_000_000,
            ),
        )
        index = self.repo / ".git" / "index"
        before = (index.read_bytes(), index.stat().st_mtime_ns)

        verify_codebase_gate(self.project, record["id"])

        after = (index.read_bytes(), index.stat().st_mtime_ns)
        self.assertEqual(before, after)

    def test_text_fields_reject_ascii_and_unicode_control_categories(
        self,
    ) -> None:
        controls = {
            "C0": "\u0001",
            "DEL": "\u007f",
            "C1": "\u0085",
            "format": "\u200b",
            "private-use": "\ue000",
        }
        for label, character in controls.items():
            with self.subTest(label=label):
                manifest = self._manifest(f"control-{label}")
                payload = read_json(manifest)
                payload["name"] = f"bad{character}name"
                manifest.write_text(
                    json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "控制|规范"):
                    register_codebase(self.project, manifest, self.repo)

    def test_require_clean_rejects_ignored_executable_code(self) -> None:
        from workflow_core import codebases

        external = self._new_repo("ignored-external")
        (external / "data").mkdir()
        (external / "data" / "run.py").write_text(
            "print('unsafe')\n",
            encoding="utf-8",
        )
        (external / ".git" / "info" / "exclude").write_text(
            "data/\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "ignored|可执行|clean"):
            codebases._verify_clean_worktree(external.resolve(), None)

        same_repo = self._new_repo("ignored-same-repo")
        control = same_repo / ".experiment-workflow"
        control.mkdir()
        (control / "train.py").write_text(
            "print('unsafe')\n",
            encoding="utf-8",
        )
        (same_repo / ".git" / "info" / "exclude").write_text(
            ".experiment-workflow/train.py\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "ignored|可执行|clean"):
            codebases._verify_clean_worktree(
                same_repo.resolve(),
                control.resolve(),
            )

    def test_docs_distinguish_baseline_from_formal_run_checkout_gate(self) -> None:
        documents = (
            ROOT / "skills" / "cv-experiment-workflow" / "SKILL.md",
            ROOT
            / "skills"
            / "cv-experiment-workflow"
            / "references"
            / "workflow.md",
        )
        for document in documents:
            with self.subTest(document=document.name):
                text = document.read_text(encoding="utf-8")
                self.assertIn("默认只核对 Codebase 基线身份", text)
                self.assertIn("expected_git", text)
                self.assertIn("require_clean=true", text)


if __name__ == "__main__":
    unittest.main()
