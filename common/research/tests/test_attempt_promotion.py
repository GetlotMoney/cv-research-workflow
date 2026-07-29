from __future__ import annotations

import concurrent.futures
import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests._helpers import SCRIPTS, cli_json, read_json, run_cli


class AttemptPromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        self.control, self.version, self.trial = self._ready_project(self.project)

    def _ready_project(self, project: Path):
        cli_json("init", "--path", project, "--name", "attempt test")
        idea = cli_json(
            "new-idea", "--project", project, "--title", "idea", "--note", "note",
        )
        cli_json(
            "activate-idea", "--project", project, "--idea", idea["id"],
            "--problem", "problem", "--mechanism", "mechanism",
            "--hypothesis", "hypothesis",
        )
        version = cli_json(
            "register-version", "--project", project, "--name", "baseline",
            "--repo-url", "https://example.invalid/repo", "--commit", "a" * 40,
        )
        trial = cli_json(
            "new-trial", "--project", project, "--idea", idea["id"],
            "--base-version", version["id"], "--template-family", "feature-adapter",
            "--attachment-point", "feature-output",
        )
        return project / ".experiment-workflow", version, trial

    def _ready_formal_project(self, project: Path):
        repo = project.parent / f"{project.name}-code"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        for key, value in (
            ("user.email", "formal@example.invalid"),
            ("user.name", "Formal Tests"),
            ("core.autocrlf", "false"),
        ):
            subprocess.run(
                ["git", "-C", str(repo), "config", key, value],
                check=True, capture_output=True,
            )
        target = repo / "models" / "adapter.py"
        target.parent.mkdir()
        target.write_text("def apply(value):\n    return value\n", encoding="utf-8")
        (repo / "models" / "other.py").write_text(
            "def apply(value):\n    return value\n", encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "formal fixture"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin",
             "https://example.invalid/formal.git"], check=True,
        )
        commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True, encoding="utf-8",
        ).stdout.strip()
        cli_json("init", "--path", project, "--name", "formal attempt")
        control = project / ".experiment-workflow"
        template = {
            "schema": "cv-experiment-workflow.project-template.v1",
            "template_id": "TPL-0001", "name": "正式测试模板",
            "description": "仅保存组合事实",
            "code_source": {"repo_url": "https://example.invalid/formal.git",
                            "commit": commit, "template_path": "."},
            "listed_files": [{"path": "models/adapter.py", "role": "落点",
                              "digest": "sha256:" + "0" * 64}],
            "interfaces": {
                "checkpoint": "path", "config": "mapping", "data": "batch",
                "evaluation": "evaluate", "metrics": "mapping", "model": "model",
                "seed": "integer", "training": "train",
            },
            "attachment_points": [
                {"name": "feature-output", "contract": "value -> value",
                 "target_path": "models/adapter.py", "disabled_behavior": "identity"},
                {"name": "other-output", "contract": "value -> value",
                 "target_path": "models/other.py", "disabled_behavior": "identity"},
            ],
            "provenance_sources": [
                {"label": "测试", "locator": "local", "identity": "fixture",
                 "commit": None, "license_spdx": "MIT", "files": [],
                 "symbols": [], "use_mode": "reimplementation", "note": "测试"},
            ],
            "framework": {
                "nodes": [{"id": "model", "label": "模型", "kind": "model"}],
                "edges": [],
            },
            "verification": {"git_head": commit, "listed_file_count": 1,
                             "verified": True},
        }
        (control / "templates" / "TPL-0001.json").write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        idea = cli_json(
            "new-idea", "--project", project, "--title", "formal", "--note", "formal",
        )
        cli_json(
            "activate-idea", "--project", project, "--idea", idea["id"],
            "--problem", "problem", "--mechanism", "mechanism",
            "--hypothesis", "hypothesis",
        )
        cli_json(
            "set-implementation-mapping", "--project", project,
            "--idea", idea["id"], "--mapping", json.dumps({
                "problem": "problem", "mechanism": "mechanism",
                "attachment_point": "feature-output",
                "target_path": "models/adapter.py",
                "validation": "python -m pytest -q",
                "disabled_behavior": "identity",
            }),
        )
        version = cli_json(
            "register-version", "--project", project, "--name", "formal base",
            "--repo-url", "https://example.invalid/formal.git", "--commit", commit,
        )
        trial = cli_json(
            "new-trial", "--project", project, "--idea", idea["id"],
            "--base-version", version["id"], "--template", "TPL-0001",
            "--attachment-point", "feature-output",
        )
        return control, version, trial, repo

    def _ready_formal_draft(self, project: Path):
        control, base, trial, repo = self._ready_formal_project(project)
        bound = cli_json(
            "sync-code-asset", "--project", project,
            "--asset", trial["code_asset_id"], "--code-root", repo,
            "--relative-path", "models/adapter.py",
        )
        attempt = cli_json(
            "new-attempt", "--project", project, "--target", trial["id"],
            "--type", "innovation", "--seed", 31,
            "--command", "python train.py",
        )
        cli_json(
            "record-result", "--project", project, "--attempt", attempt["id"],
            "--metrics", "{}", "--decision", "accept", "--conclusion", "accepted",
        )
        draft = cli_json(
            "promote", "--project", project, "--attempt", attempt["id"],
        )
        return control, base, trial, repo, bound, attempt, draft

    def _ready_activated_formal_project(self, project: Path):
        control, base, trial, repo, bound, attempt, draft = (
            self._ready_formal_draft(project)
        )
        active = cli_json(
            "activate-version", "--project", project, "--version", draft["id"],
            "--repo-url", "https://example.invalid/integrated.git",
            "--commit", "d" * 40, "--code-path", "src/model",
        )
        return control, base, trial, repo, bound, attempt, draft, active

    @staticmethod
    def _snapshot_files(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    @staticmethod
    def _snapshot_tree(root: Path) -> dict[str, bytes | None]:
        return {
            path.relative_to(root).as_posix(): (
                None if path.is_dir() else path.read_bytes()
            )
            for path in root.rglob("*")
        }

    def new_attempt(
        self, target: str | None = None, attempt_type: str = "innovation", seed: int = 5,
        config: str | None = None, *, check: bool = True,
    ):
        args: list[object] = [
            "new-attempt", "--project", self.project, "--target", target or self.trial["id"],
            "--type", attempt_type, "--seed", seed, "--command", "python train.py",
        ]
        if config is not None:
            args.extend(("--config", config))
        return run_cli(*args, check=check)

    def record_accept(self, attempt_id: str, *, metrics: str = '{"acc":0.9}'):
        return cli_json(
            "record-result", "--project", self.project, "--attempt", attempt_id,
            "--metrics", metrics, "--decision", "accept",
            "--conclusion", "improved", "--artifacts", '[{"path":"runs/out.json"}]',
            "--limitations", '["small sample"]',
        )

    def _record_result_api(self, project: Path, attempt_id: str):
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        attempts_module = importlib.import_module("workflow_core.attempts")
        return attempts_module.record_result(
            project,
            attempt_id,
            {"acc": 0.9},
            "accept",
            "improved",
            [{"path": "runs/out.json"}],
            ["small sample"],
        )

    def test_formal_attempt_requires_binding_and_freezes_composition_order(self) -> None:
        project = Path(self.temporary.name) / "formal-project"
        control, _version, trial, repo = self._ready_formal_project(project)

        rejected = run_cli(
            "new-attempt", "--project", project, "--target", trial["id"],
            "--type", "innovation", "--seed", 7, "--command", "python train.py",
            check=False,
        )
        self.assertEqual(2, rejected.returncode)
        self.assertEqual([], list((control / "attempts").iterdir()))

        cli_json(
            "sync-code-asset", "--project", project,
            "--asset", trial["code_asset_id"], "--code-root", repo,
            "--relative-path", "models/adapter.py",
        )
        attempt = cli_json(
            "new-attempt", "--project", project, "--target", trial["id"],
            "--type", "innovation", "--seed", 7, "--command", "python train.py",
        )
        self.assertEqual([trial["code_asset_id"]], attempt["ordered_code_asset_ids"])
        cli_json("validate", "--project", project)

    def test_record_result_api_rejects_frozen_module_drift_before_attempt_write(self) -> None:
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        rendering = importlib.import_module("workflow_core.rendering")
        for rerender in (False, True):
            with self.subTest(rerender=rerender):
                project = Path(self.temporary.name) / f"result-drift-{rerender}"
                control, _version, trial = self._ready_project(project)
                attempt = cli_json(
                    "new-attempt", "--project", project, "--target", trial["id"],
                    "--type", "innovation", "--seed", 9,
                    "--command", "python train.py",
                )
                asset_dir = control / "code-assets" / trial["code_asset_id"]
                (asset_dir / "module.py").write_text(
                    "def drifted():\n    return 1\n", encoding="utf-8",
                )
                if rerender:
                    (asset_dir / "framework.html").write_bytes(
                        rendering.render_code_asset(asset_dir)
                    )
                before = self._snapshot_files(control / "attempts")

                with self.assertRaises(ValueError):
                    self._record_result_api(project, attempt["id"])

                self.assertEqual(before, self._snapshot_files(control / "attempts"))

    def test_record_result_cli_rejects_target_fact_drift_before_attempt_write(self) -> None:
        attempt = json.loads(self.new_attempt(target=self.version["id"]).stdout)
        version_path = self.control / "versions" / f"{self.version['id']}.json"
        changed = read_json(version_path)
        changed["name"] = "drifted baseline"
        version_path.write_text(json.dumps(changed), encoding="utf-8")
        before = self._snapshot_files(self.control / "attempts")

        result = run_cli(
            "record-result", "--project", self.project, "--attempt", attempt["id"],
            "--metrics", '{"acc":0.9}', "--decision", "accept",
            "--conclusion", "improved", check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, self._snapshot_files(self.control / "attempts"))

    def test_record_result_cli_rejects_unknown_control_entry_before_attempt_write(self) -> None:
        attempt = json.loads(self.new_attempt().stdout)
        unknown = self.control / "unknown.txt"
        unknown.write_text("unowned", encoding="utf-8")
        before = self._snapshot_files(self.control / "attempts")

        result = run_cli(
            "record-result", "--project", self.project, "--attempt", attempt["id"],
            "--metrics", '{"acc":0.9}', "--decision", "accept",
            "--conclusion", "improved", check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, self._snapshot_files(self.control / "attempts"))

    def test_all_workflow_commands_reject_unknown_control_entries_without_writes(self) -> None:
        commands = (
            "new-idea", "activate-idea", "register-version", "new-trial",
            "sync-code-asset", "new-attempt", "record-result", "promote",
            "update-role-memory", "promotion-check",
        )
        corruptions = ("root-file", "root-directory", "object-extra")
        for command in commands:
            for corruption in corruptions:
                with self.subTest(command=command, corruption=corruption):
                    project = Path(self.temporary.name) / f"matrix-{command}-{corruption}"
                    control, version, trial = self._ready_project(project)
                    draft = cli_json(
                        "new-idea", "--project", project,
                        "--title", "draft", "--note", "draft",
                    )
                    planned = {"id": "ATTEMPT-0000"}
                    if command == "record-result":
                        planned = cli_json(
                            "new-attempt", "--project", project, "--target", trial["id"],
                            "--type", "innovation", "--seed", 11,
                            "--command", "python train.py",
                        )
                    completed = {"id": "ATTEMPT-0000"}
                    if command in {"promote", "promotion-check"}:
                        completed = cli_json(
                            "new-attempt", "--project", project, "--target", trial["id"],
                            "--type", "innovation", "--seed", 12,
                            "--command", "python train.py",
                        )
                        cli_json(
                            "record-result", "--project", project,
                            "--attempt", completed["id"], "--metrics", "{}",
                            "--decision", "accept", "--conclusion", "accepted",
                        )
                    memory = project.parent / f"{project.name}-memory.md"
                    memory.write_text("reviewed memory\n", encoding="utf-8")
                    args: dict[str, tuple[object, ...]] = {
                        "new-idea": (
                            "new-idea", "--project", project,
                            "--title", "next", "--note", "next",
                        ),
                        "activate-idea": (
                            "activate-idea", "--project", project,
                            "--idea", draft["id"], "--problem", "p",
                            "--mechanism", "m", "--hypothesis", "h",
                        ),
                        "register-version": (
                            "register-version", "--project", project,
                            "--name", "next", "--repo-url", "https://example.invalid/next",
                            "--commit", "b" * 40,
                        ),
                        "new-trial": (
                            "new-trial", "--project", project,
                            "--idea", trial["idea_id"], "--base-version", version["id"],
                            "--template-family", "feature-adapter",
                            "--attachment-point", "feature-output",
                        ),
                        "sync-code-asset": (
                            "sync-code-asset", "--project", project,
                            "--asset", trial["code_asset_id"],
                        ),
                        "new-attempt": (
                            "new-attempt", "--project", project, "--target", trial["id"],
                            "--type", "innovation", "--seed", 13,
                            "--command", "python train.py",
                        ),
                        "record-result": (
                            "record-result", "--project", project,
                            "--attempt", planned["id"], "--metrics", "{}",
                            "--decision", "accept", "--conclusion", "done",
                        ),
                        "promote": (
                            "promote", "--project", project,
                            "--attempt", completed["id"],
                        ),
                        "update-role-memory": (
                            "update-role-memory", "--project", project,
                            "--role", "reviewer", "--content-file", memory,
                        ),
                        "promotion-check": (
                            "promotion-check", "--project", project,
                            "--attempt", completed["id"],
                        ),
                    }
                    if corruption == "root-file":
                        unknown = control / "unknown.txt"
                        unknown.write_text("unknown", encoding="utf-8")
                    elif corruption == "root-directory":
                        unknown = control / "unknown-dir"
                        unknown.mkdir()
                    else:
                        unknown = (
                            control / "code-assets" / trial["code_asset_id"] / "extra.txt"
                        )
                        unknown.write_text("extra", encoding="utf-8")
                    before = self._snapshot_tree(control)

                    result = run_cli(*args[command], check=False)

                    self.assertNotEqual(0, result.returncode)
                    self.assertEqual(before, self._snapshot_tree(control))

    def test_new_attempt_freezes_identity_with_default_config(self) -> None:
        payload = json.loads(self.new_attempt().stdout)

        self.assertEqual("ATTEMPT-0001", payload["id"])
        self.assertEqual("planned", payload["status"])
        self.assertEqual("innovation", payload["type"])
        self.assertEqual(5, payload["seed"])
        self.assertIs(type(payload["seed"]), int)
        self.assertEqual("python train.py", payload["command"])
        self.assertEqual({}, payload["config"])
        self.assertEqual(self.version["id"], payload["base_version_id"])
        self.assertEqual(
            {"kind": "trial", "id": self.trial["id"],
             "facts_digest": payload["target"]["facts_digest"]},
            payload["target"],
        )
        self.assertEqual([self.trial["code_asset_id"]], payload["ordered_code_asset_ids"])
        self.assertRegex(payload["target"]["facts_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(payload["frozen_identity_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            payload,
            read_json(self.control / "attempts" / "ATTEMPT-0001.json"),
        )

    def test_digest_is_canonical_and_ignores_record_id_and_timestamps(self) -> None:
        first = json.loads(self.new_attempt(config='{"z":1,"nested":{"b":2,"a":1}}').stdout)
        other = Path(self.temporary.name) / "other"
        _control, _version, other_trial = self._ready_project(other)
        second = cli_json(
            "new-attempt", "--project", other, "--target", other_trial["id"],
            "--type", "innovation", "--seed", "5", "--command", "python train.py",
            "--config", '{"nested":{"a":1,"b":2},"z":1}',
        )

        self.assertNotEqual(first["created_at"], second["created_at"])
        self.assertEqual(first["frozen_identity_digest"], second["frozen_identity_digest"])

    def test_all_types_support_trial_version_and_completed_attempt_targets(self) -> None:
        completed = json.loads(self.new_attempt().stdout)
        self.record_accept(completed["id"])
        targets = (self.trial["id"], self.version["id"], completed["id"])
        for attempt_type, target in zip(
            ("tune", "ablation", "reproduction"), targets,
        ):
            with self.subTest(attempt_type=attempt_type, target=target):
                payload = json.loads(self.new_attempt(target, attempt_type).stdout)
                self.assertEqual(attempt_type, payload["type"])
                self.assertEqual(self.version["id"], payload["base_version_id"])

    def test_bad_types_targets_and_non_object_config_leave_no_attempt(self) -> None:
        planned = json.loads(self.new_attempt().stdout)
        before = sorted(path.name for path in (self.control / "attempts").iterdir())
        cases = (
            ("--target", "bad", "--type", "innovation"),
            ("--target", "TRIAL-9999", "--type", "innovation"),
            ("--target", planned["id"], "--type", "innovation"),
            ("--target", self.trial["id"], "--type", "other"),
            ("--target", self.trial["id"], "--type", "innovation", "--config", "[]"),
        )
        for extra in cases:
            with self.subTest(extra=extra):
                result = run_cli(
                    "new-attempt", "--project", self.project, *extra,
                    "--seed", "5", "--command", "python train.py", check=False,
                )
                self.assertNotEqual(0, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertTrue(result.stderr.strip())
                self.assertEqual(
                    before,
                    sorted(path.name for path in (self.control / "attempts").iterdir()),
                )

    def test_draft_or_blocked_target_leaves_no_attempt(self) -> None:
        trial_path = self.control / "trials" / self.trial["id"] / "trial.json"
        original = read_json(trial_path)
        for status in ("draft", "blocked"):
            with self.subTest(status=status):
                trial_path.write_text(
                    json.dumps({**original, "status": status}), encoding="utf-8",
                )
                result = self.new_attempt(check=False)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual([], list((self.control / "attempts").iterdir()))
        trial_path.write_text(json.dumps(original), encoding="utf-8")

    def test_linked_trial_object_directory_is_rejected_without_external_write(self) -> None:
        trial_directory = self.control / "trials" / self.trial["id"]
        external = Path(self.temporary.name) / "external-trial"
        trial_directory.rename(external)
        before = {path.name: path.read_bytes() for path in external.iterdir()}
        try:
            trial_directory.symlink_to(external, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlink unavailable: {error}")

        result = self.new_attempt(check=False)

        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], list((self.control / "attempts").iterdir()))
        self.assertEqual(before, {path.name: path.read_bytes() for path in external.iterdir()})

    def test_target_or_code_asset_tampering_rejects_new_attempt_and_validate(self) -> None:
        attempt = json.loads(self.new_attempt().stdout)
        self.record_accept(attempt["id"])
        trial_path = self.control / "trials" / self.trial["id"] / "trial.json"
        trial = read_json(trial_path)
        trial["attachment_point"] = "tampered"
        trial_path.write_text(json.dumps(trial), encoding="utf-8")

        create = self.new_attempt(check=False)
        validate = run_cli("validate", "--project", self.project, check=False)
        promotion = run_cli(
            "promotion-check", "--project", self.project, "--attempt", attempt["id"],
            check=False,
        )
        self.assertNotEqual(0, create.returncode)
        self.assertNotEqual(0, validate.returncode)
        self.assertNotEqual(0, promotion.returncode)
        self.assertEqual([f"{attempt['id']}.json"], [p.name for p in (self.control / "attempts").iterdir()])

        trial_path.write_text(json.dumps({**trial, "attachment_point": self.trial["attachment_point"]}), encoding="utf-8")
        asset_file = (
            self.control / "code-assets" / self.trial["code_asset_id"] / "module.py"
        )
        asset_file.write_text("tampered\n", encoding="utf-8")
        self.assertNotEqual(0, self.new_attempt(check=False).returncode)
        self.assertNotEqual(
            0, run_cli("validate", "--project", self.project, check=False).returncode,
        )

    def test_trial_target_revalidates_frozen_base_version_facts(self) -> None:
        for mutation in ("commit", "status", "missing"):
            with self.subTest(mutation=mutation):
                project = Path(self.temporary.name) / f"base-{mutation}"
                control, version, trial = self._ready_project(project)
                attempt = cli_json(
                    "new-attempt", "--project", project, "--target", trial["id"],
                    "--type", "innovation", "--seed", "5",
                    "--command", "python train.py",
                )
                cli_json(
                    "record-result", "--project", project, "--attempt", attempt["id"],
                    "--metrics", "{}", "--decision", "accept", "--conclusion", "ok",
                )
                child = cli_json(
                    "new-attempt", "--project", project, "--target", attempt["id"],
                    "--type", "reproduction", "--seed", "6",
                    "--command", "python reproduce.py",
                )
                cli_json(
                    "record-result", "--project", project, "--attempt", child["id"],
                    "--metrics", "{}", "--decision", "accept", "--conclusion", "ok",
                )
                version_path = control / "versions" / f"{version['id']}.json"
                if mutation == "missing":
                    version_path.unlink()
                else:
                    changed = read_json(version_path)
                    if mutation == "commit":
                        changed["code_sources"][0]["commit"] = "b" * 40
                    else:
                        changed["status"] = "draft"
                    version_path.write_text(json.dumps(changed), encoding="utf-8")
                before = self._snapshot_files(control)

                promotion = run_cli(
                    "promotion-check", "--project", project,
                    "--attempt", child["id"], check=False,
                )
                validation = run_cli("validate", "--project", project, check=False)

                self.assertNotEqual(0, promotion.returncode)
                self.assertNotEqual(0, validation.returncode)
                self.assertEqual(before, self._snapshot_files(control))

    def test_record_result_updates_same_attempt_once_with_strict_payloads(self) -> None:
        attempt = json.loads(self.new_attempt().stdout)
        completed = self.record_accept(attempt["id"])

        self.assertEqual("completed", completed["status"])
        self.assertEqual({"acc": 0.9}, completed["result"]["metrics"])
        self.assertEqual([{"path": "runs/out.json"}], completed["result"]["artifacts"])
        self.assertEqual("accept", completed["result"]["decision"])
        self.assertEqual("improved", completed["result"]["conclusion"])
        self.assertEqual(["small sample"], completed["result"]["limitations"])
        self.assertIn("recorded_at", completed["result"])
        path = self.control / "attempts" / f"{attempt['id']}.json"
        self.assertEqual(completed, read_json(path))
        original = path.read_bytes()
        repeated = run_cli(
            "record-result", "--project", self.project, "--attempt", attempt["id"],
            "--metrics", "{}", "--decision", "reject", "--conclusion", "no",
            check=False,
        )
        self.assertNotEqual(0, repeated.returncode)
        self.assertEqual(original, path.read_bytes())

    def test_artifact_reference_accepts_optional_existing_sha256_style(self) -> None:
        attempt = json.loads(self.new_attempt().stdout)
        digest = "a" * 64
        completed = cli_json(
            "record-result", "--project", self.project, "--attempt", attempt["id"],
            "--metrics", "{}", "--decision", "accept", "--conclusion", "ok",
            "--artifacts", json.dumps([{"path": "runs/model.bin", "digest": digest}]),
        )
        self.assertEqual(
            [{"path": "runs/model.bin", "digest": digest}],
            completed["result"]["artifacts"],
        )

    def test_result_keyboard_interrupt_preserves_original_and_audits_temp(self) -> None:
        attempt = json.loads(self.new_attempt().stdout)
        path = self.control / "attempts" / f"{attempt['id']}.json"
        original = path.read_bytes()
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        workflow_io = importlib.import_module("workflow_core.io")
        attempts = importlib.import_module("workflow_core.attempts")
        real_replace = os.replace

        def interrupt_replace(source, destination):
            if Path(destination) == path:
                raise KeyboardInterrupt("injected")
            return real_replace(source, destination)

        with mock.patch.object(workflow_io.os, "replace", side_effect=interrupt_replace):
            with self.assertRaises(RuntimeError) as captured:
                attempts.record_result(
                    self.project, attempt["id"], {}, "accept", "ok", [], [],
                )

        self.assertEqual(original, path.read_bytes())
        residuals = list(path.parent.glob(f".{path.name}.*.tmp"))
        self.assertEqual(1, len(residuals))
        self.assertIn("injected", str(captured.exception))
        self.assertIn("异常路径未自动清理", str(captured.exception))
        self.assertIn(
            str(residuals[0]), str(captured.exception).split("残留路径：", 1)[1],
        )

        with self.assertRaisesRegex(ValueError, "未知文件或目录"):
            attempts.record_result(
                self.project, attempt["id"], {}, "accept", "ok", [], [],
            )
        self.assertEqual(original, path.read_bytes())
        self.assertTrue(residuals[0].exists())

    def test_result_retry_is_idempotent_after_replace_committed_then_interrupted(self) -> None:
        attempt = json.loads(self.new_attempt().stdout)
        path = self.control / "attempts" / f"{attempt['id']}.json"
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        workflow_io = importlib.import_module("workflow_core.io")
        attempts_module = importlib.import_module("workflow_core.attempts")
        real_replace = os.replace
        interrupted = False

        def replace_then_interrupt(source, destination):
            nonlocal interrupted
            real_replace(source, destination)
            if Path(destination) == path and not interrupted:
                interrupted = True
                raise KeyboardInterrupt("replace committed")

        request = ({"acc": 0.9}, "accept", "improved", [{"path": "runs/a.json"}], ["small"])
        with mock.patch.object(workflow_io.os, "replace", side_effect=replace_then_interrupt):
            first = attempts_module.record_result(
                self.project, attempt["id"], *request,
            )
        committed = read_json(path)
        retried = attempts_module.record_result(
            self.project, attempt["id"], *request,
        )
        before_different = path.read_bytes()
        with self.assertRaises(ValueError):
            attempts_module.record_result(
                self.project, attempt["id"], {"acc": 0.8}, "reject",
                "different", [], [],
            )

        self.assertTrue(interrupted)
        self.assertEqual(committed, first)
        self.assertEqual(committed, retried)
        self.assertEqual(before_different, path.read_bytes())
        self.assertEqual([], list(path.parent.glob(f".{path.name}.*.tmp")))

    def test_result_retry_rejects_boolean_as_integer_equivalent(self) -> None:
        attempt = json.loads(self.new_attempt().stdout)
        path = self.control / "attempts" / f"{attempt['id']}.json"
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        attempts_module = importlib.import_module("workflow_core.attempts")
        attempts_module.record_result(
            self.project, attempt["id"], {"value": True}, "accept", "ok", [], [],
        )
        original = path.read_bytes()

        with self.assertRaises(ValueError):
            attempts_module.record_result(
                self.project, attempt["id"], {"value": 1}, "accept", "ok", [], [],
            )

        self.assertEqual(original, path.read_bytes())

    def test_result_retry_rejects_integer_as_float_equivalent(self) -> None:
        attempt = json.loads(self.new_attempt().stdout)
        path = self.control / "attempts" / f"{attempt['id']}.json"
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        attempts_module = importlib.import_module("workflow_core.attempts")
        attempts_module.record_result(
            self.project, attempt["id"], {"value": 1}, "accept", "ok", [], [],
        )
        original = path.read_bytes()

        with self.assertRaises(ValueError):
            attempts_module.record_result(
                self.project, attempt["id"], {"value": 1.0}, "accept", "ok", [], [],
            )

        self.assertEqual(original, path.read_bytes())

    def test_result_retry_accepts_same_json_with_different_key_order(self) -> None:
        attempt = json.loads(self.new_attempt().stdout)
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        attempts_module = importlib.import_module("workflow_core.attempts")
        completed = attempts_module.record_result(
            self.project,
            attempt["id"],
            {"outer": {"first": 1, "second": 2}, "tail": 3},
            "accept",
            "ok",
            [],
            [],
        )

        retried = attempts_module.record_result(
            self.project,
            attempt["id"],
            {"tail": 3, "outer": {"second": 2, "first": 1}},
            "accept",
            "ok",
            [],
            [],
        )

        self.assertEqual(completed, retried)

    def test_record_result_rejects_invalid_json_numbers_artifacts_and_text(self) -> None:
        invalid = (
            ("--metrics", "[]", "--decision", "accept", "--conclusion", "ok"),
            ("--metrics", '{"loss":NaN}', "--decision", "accept", "--conclusion", "ok"),
            ("--metrics", "{}", "--decision", "accept", "--conclusion", " "),
            ("--metrics", "{}", "--decision", "accept", "--conclusion", "ok",
             "--artifacts", '[{"path":"../outside"}]'),
            ("--metrics", "{}", "--decision", "accept", "--conclusion", "ok",
             "--artifacts", "{}"),
            ("--metrics", "{}", "--decision", "accept", "--conclusion", "ok",
             "--limitations", '[""]'),
        )
        for index, extra in enumerate(invalid):
            attempt = json.loads(self.new_attempt(seed=index).stdout)
            path = self.control / "attempts" / f"{attempt['id']}.json"
            original = path.read_bytes()
            result = run_cli(
                "record-result", "--project", self.project, "--attempt", attempt["id"],
                *extra, check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertEqual(original, path.read_bytes())

    def test_direct_api_rejects_non_json_values_without_modification(self) -> None:
        class CustomValue:
            pass

        invalid_metrics = (
            {"nested": (float("nan"),)},
            {"tuple": (1, 2)},
            {"set": {1}},
            {"custom": CustomValue()},
            {"nested": [float("nan")]},
            {"nested": {"positive": float("inf")}},
            {"negative": float("-inf")},
        )
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        attempts_module = importlib.import_module("workflow_core.attempts")
        for index, metrics in enumerate(invalid_metrics):
            with self.subTest(index=index, metrics=metrics):
                project = Path(self.temporary.name) / f"strict-json-{index}"
                control, _version, trial = self._ready_project(project)
                attempt = cli_json(
                    "new-attempt", "--project", project, "--target", trial["id"],
                    "--type", "innovation", "--seed", "5",
                    "--command", "python train.py",
                )
                path = control / "attempts" / f"{attempt['id']}.json"
                original = path.read_bytes()

                with self.assertRaises(ValueError):
                    attempts_module.record_result(
                        project, attempt["id"], metrics, "accept", "ok", [], [],
                    )

                self.assertEqual(original, path.read_bytes())

    def test_config_and_io_defense_reject_non_json_values(self) -> None:
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        attempts_module = importlib.import_module("workflow_core.attempts")
        workflow_io = importlib.import_module("workflow_core.io")
        before = self._snapshot_files(self.control / "attempts")
        with self.assertRaises(ValueError):
            attempts_module.new_attempt(
                self.project, self.version["id"], "innovation", 5,
                "python train.py", {"tuple": (1, 2)},
            )
        self.assertEqual(before, self._snapshot_files(self.control / "attempts"))

        destination = Path(self.temporary.name) / "strict-io" / "payload.json"
        destination.parent.mkdir()
        with self.assertRaises(ValueError):
            workflow_io.atomic_write_json(destination, {"nested": [float("nan")]})
        self.assertFalse(destination.exists())
        self.assertEqual([], list(destination.parent.iterdir()))

    def test_promotion_check_explains_ineligible_status_and_decisions(self) -> None:
        planned = json.loads(self.new_attempt().stdout)
        planned_result = run_cli(
            "promotion-check", "--project", self.project, "--attempt", planned["id"],
            check=False,
        )
        self.assertNotEqual(0, planned_result.returncode)
        self.assertIn("planned", planned_result.stderr)
        for decision in ("reject", "inconclusive"):
            attempt = json.loads(self.new_attempt().stdout)
            cli_json(
                "record-result", "--project", self.project, "--attempt", attempt["id"],
                "--metrics", "{}", "--decision", decision, "--conclusion", decision,
            )
            check = run_cli(
                "promotion-check", "--project", self.project, "--attempt", attempt["id"],
                check=False,
            )
            self.assertNotEqual(0, check.returncode)
            self.assertIn(decision, check.stderr)

    def test_promote_creates_draft_version_with_ordered_deduplicated_evidence(self) -> None:
        first = json.loads(self.new_attempt().stdout)
        self.record_accept(first["id"])
        second = json.loads(self.new_attempt(first["id"], "ablation").stdout)
        self.record_accept(second["id"])
        original_version = (self.control / "versions" / f"{self.version['id']}.json").read_bytes()
        original_trial = (self.control / "trials" / self.trial["id"] / "trial.json").read_bytes()
        original_attempts = {
            path.name: path.read_bytes() for path in (self.control / "attempts").iterdir()
        }

        promoted = cli_json(
            "promote", "--project", self.project, "--attempt", second["id"],
            "--attempt", first["id"], "--attempt", second["id"],
        )

        self.assertEqual("VER-0002", promoted["id"])
        self.assertEqual("draft", promoted["status"])
        self.assertEqual(self.version["id"], promoted["base_version_id"])
        self.assertEqual([first["id"], second["id"]], promoted["accepted_attempt_ids"])
        self.assertEqual([self.trial["code_asset_id"]], promoted["ordered_code_asset_ids"])
        self.assertEqual(self.version["code_sources"], promoted["code_sources"])
        self.assertEqual(original_version, (self.control / "versions" / f"{self.version['id']}.json").read_bytes())
        self.assertEqual(original_trial, (self.control / "trials" / self.trial["id"] / "trial.json").read_bytes())
        self.assertEqual(original_attempts, {p.name: p.read_bytes() for p in (self.control / "attempts").iterdir()})
        blocked = run_cli(
            "new-trial", "--project", self.project, "--idea", self.trial["idea_id"],
            "--base-version", promoted["id"], "--template-family", "feature-adapter",
            "--attachment-point", "feature-output", check=False,
        )
        self.assertNotEqual(0, blocked.returncode)

    def test_activate_promoted_formal_version_preserves_lineage_and_is_idempotent(
        self,
    ) -> None:
        project = Path(self.temporary.name) / "formal-activation"
        control, base, trial, repo = self._ready_formal_project(project)
        bound = cli_json(
            "sync-code-asset", "--project", project,
            "--asset", trial["code_asset_id"], "--code-root", repo,
            "--relative-path", "models/adapter.py",
        )
        attempt = cli_json(
            "new-attempt", "--project", project, "--target", trial["id"],
            "--type", "innovation", "--seed", 17,
            "--command", "python train.py",
        )
        cli_json(
            "record-result", "--project", project, "--attempt", attempt["id"],
            "--metrics", '{"acc":0.9}', "--decision", "accept",
            "--conclusion", "accepted",
        )
        draft = cli_json(
            "promote", "--project", project, "--attempt", attempt["id"],
        )
        path = control / "versions" / f"{draft['id']}.json"

        activated = cli_json(
            "activate-version", "--project", project, "--version", draft["id"],
            "--repo-url", "https://example.invalid/integrated.git",
            "--commit", "ABCDEF0123456789ABCDEF0123456789ABCDEF01",
            "--code-path", "src/model",
        )

        self.assertEqual(
            {
                "schema": "cv-experiment-workflow.version.v2",
                "id": draft["id"],
                "status": "active",
                "name": draft["name"],
                "base_version_id": base["id"],
                "template_id": "TPL-0001",
                "accepted_attempt_ids": [attempt["id"]],
                "accepted_code_asset_ids": [trial["code_asset_id"]],
                "accepted_code_refs": [
                    {
                        "code_asset_id": trial["code_asset_id"],
                        "external_code_ref": bound["external_code_ref"],
                    }
                ],
                "code_sources": [
                    {
                        "repo_url": "https://example.invalid/integrated.git",
                        "commit": "abcdef0123456789abcdef0123456789abcdef01",
                        "relative_path": "src/model",
                    }
                ],
                "created_at": draft["created_at"],
                "updated_at": activated["updated_at"],
            },
            activated,
        )
        first_bytes = path.read_bytes()
        repeated = cli_json(
            "activate-version", "--project", project, "--version", draft["id"],
            "--repo-url", "https://example.invalid/integrated.git",
            "--commit", "abcdef0123456789abcdef0123456789abcdef01",
            "--code-path", "src/model",
        )
        self.assertEqual(activated, repeated)
        self.assertEqual(first_bytes, path.read_bytes())
        cli_json("validate", "--project", project)

    def test_promote_returns_existing_active_version_for_same_signature(self) -> None:
        project = Path(self.temporary.name) / "active-promotion-retry"
        control, _base, _trial, _repo, _bound, attempt, _draft, active = (
            self._ready_activated_formal_project(project)
        )

        repeated = cli_json(
            "promote", "--project", project, "--attempt", attempt["id"],
        )

        self.assertEqual(active, repeated)
        self.assertEqual(
            ["VER-0001.json", "VER-0002.json"],
            sorted(path.name for path in (control / "versions").glob("VER-*.json")),
        )

    def test_active_v2_freezes_and_replays_accepted_assets_for_next_promotion(
        self,
    ) -> None:
        project = Path(self.temporary.name) / "formal-v2-base"
        control, _base, first_trial, repo = self._ready_formal_project(project)
        cli_json(
            "sync-code-asset", "--project", project,
            "--asset", first_trial["code_asset_id"], "--code-root", repo,
            "--relative-path", "models/adapter.py",
        )
        first_attempt = cli_json(
            "new-attempt", "--project", project, "--target", first_trial["id"],
            "--type", "innovation", "--seed", 21,
            "--command", "python train.py",
        )
        cli_json(
            "record-result", "--project", project, "--attempt", first_attempt["id"],
            "--metrics", "{}", "--decision", "accept", "--conclusion", "accepted",
        )
        draft = cli_json(
            "promote", "--project", project, "--attempt", first_attempt["id"],
        )
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True, encoding="utf-8",
        ).stdout.strip()
        active = cli_json(
            "activate-version", "--project", project, "--version", draft["id"],
            "--repo-url", "https://example.invalid/formal.git",
            "--commit", head, "--code-path", ".",
        )

        version_attempt = cli_json(
            "new-attempt", "--project", project, "--target", active["id"],
            "--type", "reproduction", "--seed", 22,
            "--command", "python reproduce.py",
        )
        self.assertEqual(
            active["accepted_code_asset_ids"],
            version_attempt["ordered_code_asset_ids"],
        )

        next_idea = cli_json(
            "new-idea", "--project", project, "--title", "next", "--note", "next",
        )
        cli_json(
            "activate-idea", "--project", project, "--idea", next_idea["id"],
            "--problem", "problem", "--mechanism", "mechanism",
            "--hypothesis", "hypothesis",
        )
        cli_json(
            "set-implementation-mapping", "--project", project,
            "--idea", next_idea["id"], "--mapping", json.dumps({
                "problem": "problem", "mechanism": "mechanism",
                "attachment_point": "other-output",
                "target_path": "models/other.py",
                "validation": "python -m pytest -q",
                "disabled_behavior": "identity",
            }),
        )
        next_trial = cli_json(
            "new-trial", "--project", project, "--idea", next_idea["id"],
            "--base-version", active["id"], "--template", active["template_id"],
            "--attachment-point", "other-output",
        )
        composition = read_json(
            control / "code-assets" / next_trial["code_asset_id"] / "composition.json"
        )
        self.assertEqual(active["accepted_code_asset_ids"], composition["inherited_code_asset_ids"])
        self.assertEqual(head, composition["base_commit"])
        cli_json(
            "sync-code-asset", "--project", project,
            "--asset", next_trial["code_asset_id"], "--code-root", repo,
            "--relative-path", "models/other.py",
        )
        next_attempt = cli_json(
            "new-attempt", "--project", project, "--target", next_trial["id"],
            "--type", "innovation", "--seed", 23,
            "--command", "python train.py",
        )
        self.assertEqual(
            [first_trial["code_asset_id"], next_trial["code_asset_id"]],
            next_attempt["ordered_code_asset_ids"],
        )
        cli_json(
            "record-result", "--project", project, "--attempt", next_attempt["id"],
            "--metrics", "{}", "--decision", "accept", "--conclusion", "accepted",
        )
        next_draft = cli_json(
            "promote", "--project", project, "--attempt", next_attempt["id"],
        )
        self.assertEqual(active["id"], next_draft["base_version_id"])
        self.assertEqual(
            [first_trial["code_asset_id"], next_trial["code_asset_id"]],
            next_draft["ordered_code_asset_ids"],
        )

        conflicting_idea = cli_json(
            "new-idea", "--project", project, "--title", "conflict", "--note", "conflict",
        )
        cli_json(
            "activate-idea", "--project", project, "--idea", conflicting_idea["id"],
            "--problem", "problem", "--mechanism", "mechanism",
            "--hypothesis", "hypothesis",
        )
        cli_json(
            "set-implementation-mapping", "--project", project,
            "--idea", conflicting_idea["id"], "--mapping", json.dumps({
                "problem": "problem", "mechanism": "mechanism",
                "attachment_point": "feature-output",
                "target_path": "models/adapter.py",
                "validation": "python -m pytest -q",
                "disabled_behavior": "identity",
            }),
        )
        conflict = run_cli(
            "new-trial", "--project", project, "--idea", conflicting_idea["id"],
            "--base-version", active["id"], "--template", active["template_id"],
            "--attachment-point", "feature-output", check=False,
        )
        self.assertEqual(2, conflict.returncode)
        self.assertIn("冲突", conflict.stderr)

    def test_validate_active_v2_rejects_promotion_lineage_and_reference_drift(
        self,
    ) -> None:
        project = Path(self.temporary.name) / "active-v2-drift"
        control, _base, trial, _repo, _bound, _attempt, _draft, active = (
            self._ready_activated_formal_project(project)
        )
        version_path = control / "versions" / f"{active['id']}.json"
        original_version = version_path.read_bytes()
        composition_path = (
            control / "code-assets" / trial["code_asset_id"] / "composition.json"
        )
        original_composition = composition_path.read_bytes()

        def changed_ids(payload: dict[str, object]) -> None:
            payload["accepted_code_asset_ids"] = ["CODE-9999"]
            refs = payload["accepted_code_refs"]
            assert isinstance(refs, list) and isinstance(refs[0], dict)
            refs[0]["code_asset_id"] = "CODE-9999"

        def changed_ref(payload: dict[str, object]) -> None:
            refs = payload["accepted_code_refs"]
            assert isinstance(refs, list) and isinstance(refs[0], dict)
            external = refs[0]["external_code_ref"]
            assert isinstance(external, dict)
            external["commit"] = "e" * 40

        mutations = (
            ("accepted IDs", changed_ids),
            ("external ref", changed_ref),
            ("template lineage", lambda payload: payload.__setitem__("template_id", "TPL-0002")),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                payload = json.loads(original_version.decode("utf-8"))
                mutate(payload)
                version_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                failed = run_cli("validate", "--project", project, check=False)
                self.assertEqual(2, failed.returncode)
                version_path.write_bytes(original_version)

        composition = json.loads(original_composition.decode("utf-8"))
        composition["target_path"] = "models/tampered.py"
        composition_path.write_text(
            json.dumps(composition, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        failed = run_cli("validate", "--project", project, check=False)
        self.assertEqual(2, failed.returncode)
        composition_path.write_bytes(original_composition)
        version_path.write_bytes(original_version)
        cli_json("validate", "--project", project)

    def test_activate_version_rejects_conflicts_and_invalid_drafts_without_writes(
        self,
    ) -> None:
        project = Path(self.temporary.name) / "activation-failures"
        control, base, _trial, _repo, _bound, _attempt, _draft, active = (
            self._ready_activated_formal_project(project)
        )
        active_path = control / "versions" / f"{active['id']}.json"
        active_bytes = active_path.read_bytes()
        conflicting_calls = (
            ("repo", "https://example.invalid/other.git", "d" * 40, "src/model"),
            ("commit", "https://example.invalid/integrated.git", "e" * 40, "src/model"),
            ("path", "https://example.invalid/integrated.git", "d" * 40, "other/path"),
            ("bad commit", "https://example.invalid/integrated.git", "abc", "src/model"),
            ("bad path", "https://example.invalid/integrated.git", "d" * 40, "../escape"),
        )
        for label, repo_url, commit, code_path in conflicting_calls:
            with self.subTest(label=label):
                before = self._snapshot_tree(control)
                failed = run_cli(
                    "activate-version", "--project", project,
                    "--version", active["id"], "--repo-url", repo_url,
                    "--commit", commit, "--code-path", code_path, check=False,
                )
                self.assertEqual(2, failed.returncode)
                self.assertEqual(before, self._snapshot_tree(control))
                self.assertEqual(active_bytes, active_path.read_bytes())

        before_missing = self._snapshot_tree(control)
        rejected_missing = run_cli(
            "activate-version", "--project", project, "--version", "VER-9999",
            "--repo-url", "https://example.invalid/missing.git",
            "--commit", "f" * 40, "--code-path", ".", check=False,
        )
        self.assertEqual(2, rejected_missing.returncode)
        self.assertEqual(before_missing, self._snapshot_tree(control))

        base_path = control / "versions" / f"{base['id']}.json"
        base_bytes = base_path.read_bytes()
        rejected_v1 = run_cli(
            "activate-version", "--project", project, "--version", base["id"],
            "--repo-url", "https://example.invalid/base.git",
            "--commit", "f" * 40, "--code-path", ".", check=False,
        )
        self.assertEqual(2, rejected_v1.returncode)
        self.assertEqual(base_bytes, base_path.read_bytes())

        legacy_attempt = json.loads(self.new_attempt().stdout)
        self.record_accept(legacy_attempt["id"])
        legacy_draft = cli_json(
            "promote", "--project", self.project, "--attempt", legacy_attempt["id"],
        )
        legacy_path = self.control / "versions" / f"{legacy_draft['id']}.json"
        legacy_bytes = legacy_path.read_bytes()
        rejected_legacy = run_cli(
            "activate-version", "--project", self.project,
            "--version", legacy_draft["id"],
            "--repo-url", "https://example.invalid/legacy.git",
            "--commit", "1" * 40, "--code-path", ".", check=False,
        )
        self.assertEqual(2, rejected_legacy.returncode)
        self.assertEqual(legacy_bytes, legacy_path.read_bytes())

        malformed = read_json(legacy_path)
        malformed["accepted_attempt_ids"] = []
        legacy_path.write_text(
            json.dumps(malformed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        malformed_bytes = legacy_path.read_bytes()
        rejected_malformed = run_cli(
            "activate-version", "--project", self.project,
            "--version", legacy_draft["id"],
            "--repo-url", "https://example.invalid/legacy.git",
            "--commit", "1" * 40, "--code-path", ".", check=False,
        )
        self.assertEqual(2, rejected_malformed.returncode)
        self.assertEqual(malformed_bytes, legacy_path.read_bytes())

    def test_concurrent_same_activation_publishes_one_identical_version(self) -> None:
        project = Path(self.temporary.name) / "activation-concurrency"
        control, _base, _trial, _repo, _bound, _attempt, draft = (
            self._ready_formal_draft(project)
        )
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        records = importlib.import_module("workflow_core.records")
        barrier = threading.Barrier(2)

        def activate_once():
            barrier.wait(timeout=5)
            return records.activate_version(
                project, draft["id"], "https://example.invalid/integrated.git",
                "2" * 40, "src/model",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: activate_once(), range(2)))

        self.assertEqual(results[0], results[1])
        path = control / "versions" / f"{draft['id']}.json"
        self.assertEqual(results[0], read_json(path))
        self.assertEqual(
            ["VER-0001.json", "VER-0002.json"],
            sorted(item.name for item in (control / "versions").glob("VER-*.json")),
        )

    def test_promote_requires_same_base_and_eligible_without_residual_version(self) -> None:
        eligible = json.loads(self.new_attempt().stdout)
        self.record_accept(eligible["id"])
        other_project = Path(self.temporary.name) / "other-base-source"
        # A second active version in the same project supplies a distinct base.
        other_version = cli_json(
            "register-version", "--project", self.project, "--name", "other",
            "--repo-url", "https://example.invalid/other", "--commit", "b" * 40,
        )
        other_attempt = json.loads(self.new_attempt(other_version["id"]).stdout)
        self.record_accept(other_attempt["id"])
        before = sorted(path.name for path in (self.control / "versions").iterdir())

        result = run_cli(
            "promote", "--project", self.project, "--attempt", eligible["id"],
            "--attempt", other_attempt["id"], check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, sorted(path.name for path in (self.control / "versions").iterdir()))

    def test_promote_retry_returns_committed_draft_after_cleanup_interrupt(self) -> None:
        attempt = json.loads(self.new_attempt().stdout)
        self.record_accept(attempt["id"])
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        workflow_io = importlib.import_module("workflow_core.io")
        attempts_module = importlib.import_module("workflow_core.attempts")
        original_unlink = workflow_io.ParentDirectoryAnchor.unlink
        interrupted = False

        def interrupt_draft_cleanup(anchor, name: str, *, missing_ok: bool = False):
            nonlocal interrupted
            if (
                not interrupted
                and anchor.path == self.control / "versions"
                and name.startswith(".VER-0002.json.cvexp-")
            ):
                interrupted = True
                raise KeyboardInterrupt("after draft publish")
            return original_unlink(anchor, name, missing_ok=missing_ok)

        with mock.patch.object(
            workflow_io.ParentDirectoryAnchor,
            "unlink",
            new=interrupt_draft_cleanup,
        ):
            first = attempts_module.promote(self.project, [attempt["id"]])

        committed_path = self.control / "versions" / "VER-0002.json"
        committed = read_json(committed_path)
        retried = attempts_module.promote(self.project, [attempt["id"]])

        self.assertTrue(interrupted)
        self.assertEqual(committed, first)
        self.assertEqual(committed, retried)
        self.assertEqual(
            ["VER-0001.json", "VER-0002.json"],
            sorted(path.name for path in (self.control / "versions").glob("VER-*.json")),
        )

    def test_promote_persistent_temp_cleanup_failure_is_auditable_and_blocks_retry(
        self,
    ) -> None:
        attempt = json.loads(self.new_attempt().stdout)
        self.record_accept(attempt["id"])
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        workflow_io = importlib.import_module("workflow_core.io")
        attempts_module = importlib.import_module("workflow_core.attempts")
        original_unlink = workflow_io.ParentDirectoryAnchor.unlink
        residual_names: list[str] = []

        def deny_draft_temp_cleanup(anchor, name: str, *, missing_ok: bool = False):
            if (
                anchor.path == self.control / "versions"
                and name.startswith(".VER-0002.json.cvexp-")
            ):
                residual_names.append(name)
                raise PermissionError("persistent draft cleanup denied")
            return original_unlink(anchor, name, missing_ok=missing_ok)

        with mock.patch.object(
            workflow_io.ParentDirectoryAnchor,
            "unlink",
            new=deny_draft_temp_cleanup,
        ):
            with self.assertRaises(RuntimeError) as captured:
                attempts_module.promote(self.project, [attempt["id"]])

        self.assertTrue(residual_names)
        residual = self.control / "versions" / residual_names[0]
        message = str(captured.exception)
        self.assertIn("persistent draft cleanup denied", message)
        self.assertIn(str(residual), message.split("残留路径：", 1)[1])
        self.assertTrue(residual.exists())

        before_retry = self._snapshot_files(self.control / "versions")
        with self.assertRaisesRegex(ValueError, "未知文件或目录"):
            attempts_module.promote(self.project, [attempt["id"]])
        self.assertEqual(before_retry, self._snapshot_files(self.control / "versions"))
        self.assertEqual(
            ["VER-0001.json", "VER-0002.json"],
            sorted(path.name for path in (self.control / "versions").glob("VER-*.json")),
        )

    def test_validate_counts_attempt_results_and_promotion_then_detects_drift(self) -> None:
        attempt = json.loads(self.new_attempt().stdout)
        self.record_accept(attempt["id"])
        cli_json("promote", "--project", self.project, "--attempt", attempt["id"])

        validation = cli_json("validate", "--project", self.project)
        self.assertTrue(validation["valid"])
        self.assertEqual(1, validation["attempts"])
        self.assertEqual(1, validation["results"])
        self.assertEqual(2, validation["versions"])
        self.assertEqual(1, validation["draft_versions"])

        path = self.control / "attempts" / f"{attempt['id']}.json"
        tampered = read_json(path)
        tampered["command"] = "python changed.py"
        path.write_text(json.dumps(tampered), encoding="utf-8")
        self.assertNotEqual(
            0, run_cli("validate", "--project", self.project, check=False).returncode,
        )

    def test_validate_uses_one_lock_snapshot_across_trials_and_attempts(self) -> None:
        project = Path(self.temporary.name) / "validate-snapshot"
        cli_json("init", "--path", project, "--name", "snapshot")
        version = cli_json(
            "register-version", "--project", project, "--name", "baseline",
            "--repo-url", "https://example.invalid/repo", "--commit", "c" * 40,
        )
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        locking = importlib.import_module("workflow_core.locking")
        templates = importlib.import_module("workflow_core.templates")
        attempts = importlib.import_module("workflow_core.attempts")
        rw_module = importlib.import_module("rw")
        validator_thread = threading.get_ident()
        validation_released = threading.Event()
        writer_finished = threading.Event()
        writer_errors: list[BaseException] = []

        @contextmanager
        def coordinated_lock(project_root: Path):
            with locking.project_write_lock(project_root):
                yield
            if (
                threading.get_ident() == validator_thread
                and not validation_released.is_set()
            ):
                validation_released.set()
                if not writer_finished.wait(10):
                    raise TimeoutError("writer did not run after validation lock release")

        def writer() -> None:
            try:
                if not validation_released.wait(10):
                    raise TimeoutError("validation did not release its lock")
                attempts.new_attempt(
                    project, version["id"], "innovation", 7, "python train.py", {},
                )
            except BaseException as error:
                writer_errors.append(error)
            finally:
                writer_finished.set()

        worker = threading.Thread(target=writer)
        worker.start()
        with (
            mock.patch.object(templates, "project_write_lock", coordinated_lock),
            mock.patch.object(attempts, "project_write_lock", coordinated_lock),
        ):
            validation = rw_module._handle_validate(SimpleNamespace(project=project))
        worker.join(10)

        self.assertFalse(worker.is_alive())
        self.assertEqual([], writer_errors)
        self.assertEqual(
            (0, 0),
            (validation["trials"], validation["attempts"]),
            "writer must run after the complete validation snapshot",
        )
        self.assertEqual(
            1,
            len(list((project / ".experiment-workflow" / "attempts").glob("*.json"))),
        )

    def test_attempt_chain_over_six_hundred_is_validated_iteratively(self) -> None:
        project = Path(self.temporary.name) / "deep-attempt-chain"
        control, version, _trial = self._ready_project(project)
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        attempts_module = importlib.import_module("workflow_core.attempts")
        workflow_io = importlib.import_module("workflow_core.io")
        first = attempts_module.new_attempt(
            project, version["id"], "reproduction", 1, "python reproduce.py", {},
        )
        previous = attempts_module.record_result(
            project, first["id"], {}, "accept", "root", [], [],
        )
        for number in range(2, 651):
            attempt_id = f"ATTEMPT-{number:04d}"
            payload = {
                "schema": attempts_module.ATTEMPT_SCHEMA,
                "id": attempt_id,
                "status": "completed",
                "type": "reproduction",
                "seed": number,
                "command": "python reproduce.py",
                "config": {},
                "base_version_id": version["id"],
                "target": {
                    "kind": "attempt",
                    "id": previous["id"],
                    "facts_digest": attempts_module._canonical_digest(
                        attempts_module._attempt_facts(previous)
                    ),
                },
                "ordered_code_asset_ids": [],
                "frozen_identity_digest": "",
                "created_at": attempts_module._utc_now(),
                "result": {
                    "metrics": {},
                    "artifacts": [],
                    "decision": "accept",
                    "conclusion": "chain",
                    "limitations": [],
                    "recorded_at": attempts_module._utc_now(),
                },
            }
            payload["frozen_identity_digest"] = attempts_module._identity_digest(payload)
            attempts_module.validate_attempt(payload, expected_id=attempt_id)
            workflow_io.atomic_create_json(
                control / "attempts" / f"{attempt_id}.json",
                payload,
                transaction_id=f"{number:032x}",
            )
            previous = payload

        check = attempts_module.promotion_check(project, previous["id"])
        validation = attempts_module.validate_project_workflow(project)
        created = attempts_module.new_attempt(
            project, previous["id"], "reproduction", 651,
            "python reproduce.py", {},
        )

        self.assertTrue(check["eligible"])
        self.assertEqual(650, validation["attempts"])
        self.assertEqual("ATTEMPT-0651", created["id"])

    def test_attempt_cycle_fails_explicitly_without_recursion_error(self) -> None:
        project = Path(self.temporary.name) / "attempt-cycle"
        control, version, _trial = self._ready_project(project)
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        attempts_module = importlib.import_module("workflow_core.attempts")
        workflow_io = importlib.import_module("workflow_core.io")
        timestamp = attempts_module._utc_now()
        for number, target_number in ((1, 2), (2, 1)):
            attempt_id = f"ATTEMPT-{number:04d}"
            payload = {
                "schema": attempts_module.ATTEMPT_SCHEMA,
                "id": attempt_id,
                "status": "completed",
                "type": "reproduction",
                "seed": number,
                "command": "python reproduce.py",
                "config": {},
                "base_version_id": version["id"],
                "target": {
                    "kind": "attempt",
                    "id": f"ATTEMPT-{target_number:04d}",
                    "facts_digest": "sha256:" + "0" * 64,
                },
                "ordered_code_asset_ids": [],
                "frozen_identity_digest": "",
                "created_at": timestamp,
                "result": {
                    "metrics": {}, "artifacts": [], "decision": "accept",
                    "conclusion": "cycle", "limitations": [],
                    "recorded_at": timestamp,
                },
            }
            payload["frozen_identity_digest"] = attempts_module._identity_digest(payload)
            workflow_io.atomic_create_json(
                control / "attempts" / f"{attempt_id}.json",
                payload,
                transaction_id=f"{number:032x}",
            )

        with self.assertRaisesRegex(ValueError, "循环"):
            attempts_module.promotion_check(project, "ATTEMPT-0001")
        result = run_cli("validate", "--project", project, check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("循环", result.stderr)
        self.assertNotIn("RecursionError", result.stderr)

    def test_concurrent_attempt_ids_and_linked_attempt_parent_are_safe(self) -> None:
        def create(seed: int) -> dict:
            return json.loads(self.new_attempt(seed=seed).stdout)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            created = list(pool.map(create, (1, 2)))
        self.assertEqual({"ATTEMPT-0001", "ATTEMPT-0002"}, {item["id"] for item in created})

        attempts = self.control / "attempts"
        attempts.rename(self.control / "attempts-real")
        external = Path(self.temporary.name) / "external-attempts"
        external.mkdir()
        marker = external / "owned.txt"
        marker.write_bytes(b"owned\n")
        try:
            attempts.symlink_to(external, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlink unavailable: {error}")
        result = self.new_attempt(check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(b"owned\n", marker.read_bytes())
        self.assertEqual(["owned.txt"], [path.name for path in external.iterdir()])

    @unittest.skipUnless(os.name == "nt", "Windows directory handle semantics only")
    def test_windows_parent_anchor_blocks_rename_until_release(self) -> None:
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        workflow_io = importlib.import_module("workflow_core.io")
        directory = Path(self.temporary.name) / "anchored-parent"
        renamed = Path(self.temporary.name) / "renamed-parent"
        directory.mkdir()

        with workflow_io.parent_directory_anchor(directory):
            with self.assertRaises(OSError):
                directory.rename(renamed)
        directory.rename(renamed)
        self.assertTrue(renamed.is_dir())

    def test_atomic_json_operations_anchor_parent_swap_without_external_write(self) -> None:
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        workflow_io = importlib.import_module("workflow_core.io")
        attempts_module = importlib.import_module("workflow_core.attempts")

        for operation in ("new-attempt", "record-result", "promote"):
            with self.subTest(operation=operation):
                project = Path(self.temporary.name) / f"parent-swap-{operation}"
                control, version, trial = self._ready_project(project)
                if operation in {"record-result", "promote"}:
                    attempt = cli_json(
                        "new-attempt", "--project", project, "--target", trial["id"],
                        "--type", "innovation", "--seed", "5",
                        "--command", "python train.py",
                    )
                if operation == "promote":
                    cli_json(
                        "record-result", "--project", project,
                        "--attempt", attempt["id"], "--metrics", "{}",
                        "--decision", "accept", "--conclusion", "ok",
                    )
                parent_name = "versions" if operation == "promote" else "attempts"
                parent = control / parent_name
                saved = control / f"{parent_name}-saved"
                external = Path(self.temporary.name) / f"external-{operation}"
                external.mkdir()
                marker = external / "owned.txt"
                marker.write_bytes(b"external-owned\n")
                before = self._snapshot_files(external)
                original_transaction_temp_path = workflow_io.transaction_temp_path
                swapped = False

                def swap_before_temp(path: Path, transaction_id: str) -> Path:
                    nonlocal swapped
                    if not swapped and Path(path).parent == parent:
                        swapped = True
                        parent.rename(saved)
                        parent.symlink_to(external, target_is_directory=True)
                    return original_transaction_temp_path(path, transaction_id)

                def run_operation():
                    if operation == "new-attempt":
                        return attempts_module.new_attempt(
                            project, version["id"], "innovation", 8,
                            "python train.py", {},
                        )
                    if operation == "record-result":
                        return attempts_module.record_result(
                            project, attempt["id"], {}, "accept", "ok", [], [],
                        )
                    return attempts_module.promote(project, [attempt["id"]])

                with mock.patch.object(
                    workflow_io,
                    "transaction_temp_path",
                    side_effect=swap_before_temp,
                ):
                    if os.name == "nt":
                        with self.assertRaises((OSError, RuntimeError, ValueError)):
                            run_operation()
                    else:
                        result = run_operation()

                if os.name != "nt":
                    self.assertTrue(saved.is_dir())
                    if operation == "new-attempt":
                        self.assertEqual(
                            result, read_json(saved / f"{result['id']}.json")
                        )
                    elif operation == "record-result":
                        self.assertEqual("completed", read_json(
                            saved / f"{attempt['id']}.json"
                        )["status"])
                    else:
                        self.assertEqual(result, read_json(saved / f"{result['id']}.json"))

                self.assertTrue(swapped)
                self.assertEqual(before, self._snapshot_files(external))


if __name__ == "__main__":
    unittest.main()
