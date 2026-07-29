from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from unittest import mock

from tests._helpers import SCRIPTS, cli_json, read_json, run_cli


SUGGESTION_FIELDS = {
    "kind", "target", "action", "reason", "prerequisites", "cost",
    "uncertainty_reduction", "blocks", "requires_authorization",
}


class PlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        cli_json("init", "--path", self.project, "--name", "只读规划")
        self.control = self.project / ".experiment-workflow"

    @staticmethod
    def _snapshot(root: Path) -> dict[str, tuple[str, bytes | None]]:
        snapshot: dict[str, tuple[str, bytes | None]] = {}
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                snapshot[relative] = ("link", str(path.readlink()).encode("utf-8"))
            elif path.is_dir():
                snapshot[relative] = ("directory", None)
            elif path.is_file():
                snapshot[relative] = ("file", path.read_bytes())
        return snapshot

    def _new_active_idea(self, title: str = "idea") -> dict:
        idea = cli_json(
            "new-idea", "--project", self.project,
            "--title", title, "--note", "note",
        )
        return cli_json(
            "activate-idea", "--project", self.project, "--idea", idea["id"],
            "--problem", f"{title} problem", "--mechanism", f"{title} mechanism",
            "--hypothesis", f"{title} hypothesis",
        )

    def _new_version(self, name: str = "baseline", commit: str = "a" * 40) -> dict:
        return cli_json(
            "register-version", "--project", self.project, "--name", name,
            "--repo-url", "https://example.invalid/repo", "--commit", commit,
        )

    @staticmethod
    def _order_key(item: dict) -> tuple[int, str, int, str]:
        rank = {
            "structural_blocker": 0,
            "implementation_confidence": 1,
            "confirmation_gap": 2,
            "optional_extension": 3,
        }[item["kind"]]
        target = item["target"]
        suffix = target["id"].rsplit("-", 1)[-1]
        number = int(suffix) if suffix.isdigit() else 0
        return rank, target["kind"], number, item["action"]

    def _new_trial(self, idea_id: str, version_id: str, attachment: str) -> dict:
        return cli_json(
            "new-trial", "--project", self.project, "--idea", idea_id,
            "--base-version", version_id, "--template-family", "feature-adapter",
            "--attachment-point", attachment,
        )

    def _new_attempt(self, target: str, seed: int) -> dict:
        return cli_json(
            "new-attempt", "--project", self.project, "--target", target,
            "--type", "innovation", "--seed", seed,
            "--command", "python train.py", "--config", "{}",
        )

    def _record_axes(
        self, attempt_id: str, implementation: str, hypothesis: str,
    ) -> dict:
        return cli_json(
            "record-result", "--project", self.project, "--attempt", attempt_id,
            "--metrics", "{}", "--implementation-status", implementation,
            "--hypothesis-status", hypothesis, "--conclusion", "done",
        )

    def _eligible_cohort(self, count: int) -> list[dict]:
        idea = self._new_active_idea("eligible cohort")
        version = self._new_version()
        trial = self._new_trial(idea["id"], version["id"], "feature-output")
        attempts = [self._new_attempt(trial["id"], seed) for seed in range(1, count + 1)]
        for attempt in attempts:
            self._record_axes(attempt["id"], "valid", "supported")
        return attempts

    def _install_formal_template_fixture(self) -> None:
        payload = {
            "schema": "cv-experiment-workflow.project-template.v1",
            "template_id": "TPL-0001",
            "name": "规划测试模板",
            "description": "只用于识别正式创新上下文",
            "code_source": {
                "repo_url": "https://example.invalid/formal.git",
                "commit": "b" * 40,
                "template_path": ".",
            },
            "listed_files": [{
                "path": "models/adapter.py", "role": "落点",
                "digest": "sha256:" + "0" * 64,
            }],
            "interfaces": {
                "data": "batch", "model": "model", "training": "train",
                "evaluation": "evaluate", "config": "mapping", "seed": "integer",
                "metrics": "mapping", "checkpoint": "path",
            },
            "attachment_points": [{
                "name": "feature-output", "contract": "value -> value",
                "target_path": "models/adapter.py", "disabled_behavior": "identity",
            }],
            "provenance_sources": [{
                "label": "测试", "locator": "fixture", "identity": "fixture",
                "commit": None, "license_spdx": "MIT", "files": [], "symbols": [],
                "use_mode": "reimplementation", "note": "测试",
            }],
            "framework": {
                "nodes": [{"id": "model", "label": "模型", "kind": "model"}],
                "edges": [],
            },
            "verification": {
                "git_head": "b" * 40, "listed_file_count": 1, "verified": True,
            },
        }
        (self.control / "templates" / "TPL-0001.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )

    def test_status_is_stable_complete_and_read_only(self) -> None:
        idea = self._new_active_idea()
        version = self._new_version()
        trial = self._new_trial(idea["id"], version["id"], "feature-output")
        completed = self._new_attempt(trial["id"], 1)
        cli_json(
            "record-result", "--project", self.project,
            "--attempt", completed["id"], "--metrics", "{}",
            "--decision", "accept", "--conclusion", "legacy accepted",
        )
        planned_trial = self._new_trial(idea["id"], version["id"], "feature-output")
        planned = self._new_attempt(planned_trial["id"], 2)
        draft = cli_json(
            "promote", "--project", self.project, "--attempt", completed["id"],
        )
        before = self._snapshot(self.project)
        first_raw = run_cli("status", "--project", self.project).stdout
        second_raw = run_cli("status", "--project", self.project).stdout
        after = self._snapshot(self.project)
        status = json.loads(first_raw)

        self.assertEqual(first_raw, second_raw)
        self.assertEqual(before, after)
        self.assertTrue(status["valid"])
        self.assertEqual(1, status["counts"]["ideas"])
        self.assertEqual(2, status["counts"]["trials"])
        self.assertEqual(2, status["counts"]["attempts"])
        self.assertEqual([idea["id"]], status["active_idea_ids"])
        self.assertEqual([version["id"]], status["active_version_ids"])
        self.assertEqual([planned["id"]], status["planned_attempt_ids"])
        self.assertEqual([completed["id"]], status["completed_attempt_ids"])
        self.assertEqual([draft["id"]], status["draft_version_ids"])
        self.assertEqual(
            {
                "implicit_confirmation_policy", "legacy_result",
            },
            {warning["code"] for warning in status["warnings"]},
        )

    def test_empty_project_has_one_minimal_start_without_inbox(self) -> None:
        before = self._snapshot(self.project)
        first_raw = run_cli("plan-next", "--project", self.project).stdout
        second_raw = run_cli("plan-next", "--project", self.project).stdout
        payload = json.loads(first_raw)

        self.assertEqual(first_raw, second_raw)
        self.assertEqual(before, self._snapshot(self.project))
        self.assertEqual(1, len(payload["suggestions"]))
        suggestion = payload["suggestions"][0]
        self.assertEqual(SUGGESTION_FIELDS, set(suggestion))
        self.assertEqual({"kind": "project", "id": "workflow"}, suggestion["target"])
        self.assertEqual("new-idea", suggestion["action"])
        self.assertNotIn("Inbox", first_raw)
        self.assertNotIn("task_id", suggestion)

    def test_plan_next_reports_only_current_structural_blockers_in_stable_order(self) -> None:
        draft = cli_json(
            "new-idea", "--project", self.project,
            "--title", "待澄清", "--note", "note",
        )
        active = self._new_active_idea("正式创新")
        self._install_formal_template_fixture()
        version = self._new_version()
        no_attempt = self._new_trial(active["id"], version["id"], "feature-output")
        planned_trial = self._new_trial(active["id"], version["id"], "feature-output")
        planned = self._new_attempt(planned_trial["id"], 3)
        cli_json(
            "set-implementation-mapping", "--project", self.project,
            "--idea", active["id"], "--mapping", json.dumps({
                "problem": active["problem"], "mechanism": active["mechanism"],
                "attachment_point": "feature-output",
                "target_path": "models/adapter.py", "validation": "python -m pytest -q",
                "disabled_behavior": "identity",
            }),
        )
        formal_version = self._new_version("formal baseline", "b" * 40)
        formal = cli_json(
            "new-trial", "--project", self.project, "--idea", active["id"],
            "--base-version", formal_version["id"], "--template", "TPL-0001",
            "--attachment-point", "feature-output",
        )

        before = self._snapshot(self.project)
        payload = cli_json("plan-next", "--project", self.project)
        self.assertEqual(before, self._snapshot(self.project))

        suggestions = payload["suggestions"]
        self.assertTrue(all(set(item) == SUGGESTION_FIELDS for item in suggestions))
        self.assertEqual(sorted(suggestions, key=self._order_key), suggestions)
        pairs = [(item["target"]["id"], item["action"]) for item in suggestions]
        self.assertIn((draft["id"], "clarify-idea"), pairs)
        self.assertNotIn((active["id"], "set-implementation-mapping"), pairs)
        self.assertIn((no_attempt["id"], "prepare_attempt"), pairs)
        self.assertIn((planned["id"], "run-and-record-result"), pairs)
        self.assertIn((formal["code_asset_id"], "sync-code-asset"), pairs)
        formal_suggestion = next(
            item for item in suggestions
            if item["target"]["id"] == formal["code_asset_id"]
        )
        self.assertEqual("unknown", formal_suggestion["cost"])
        trial_suggestion = next(
            item for item in suggestions if item["target"]["id"] == no_attempt["id"]
        )
        self.assertEqual("trial_without_attempt", trial_suggestion["reason"])
        self.assertEqual(
            ["confirm_seed", "confirm_command", "confirm_config"],
            trial_suggestion["prerequisites"],
        )
        self.assertEqual("unknown", trial_suggestion["cost"])
        self.assertTrue(trial_suggestion["requires_authorization"])
        self.assertTrue(next(
            item for item in suggestions if item["target"]["id"] == planned["id"]
        )["requires_authorization"])

    def test_plan_next_distinguishes_implementation_confirmation_promotion_and_activation(self) -> None:
        idea = self._new_active_idea()
        version = self._new_version()
        invalid_trial = self._new_trial(idea["id"], version["id"], "feature-output")
        invalid = self._new_attempt(invalid_trial["id"], 1)
        self._record_axes(invalid["id"], "uncertain", "not_evaluated")

        cli_json(
            "set-confirmation-policy", "--project", self.project,
            "--minimum-accepted-attempts", 2, "--minimum-distinct-seeds", 2,
        )
        gap_trial = self._new_trial(idea["id"], version["id"], "feature-output")
        gap = self._new_attempt(gap_trial["id"], 2)
        self._record_axes(gap["id"], "valid", "supported")

        payload = cli_json("plan-next", "--project", self.project)
        by_target = {item["target"]["id"]: item for item in payload["suggestions"]}
        self.assertEqual("implementation_confidence", by_target[invalid["id"]]["kind"])
        self.assertEqual("verify-implementation", by_target[invalid["id"]]["action"])
        self.assertEqual("confirmation_gap", by_target[gap["id"]]["kind"])
        self.assertEqual("add-confirmation-attempt", by_target[gap["id"]]["action"])

        cli_json(
            "set-confirmation-policy", "--project", self.project,
            "--minimum-accepted-attempts", 1, "--minimum-distinct-seeds", 1,
        )
        eligible = cli_json("plan-next", "--project", self.project)
        eligible_by_target = {
            item["target"]["id"]: item for item in eligible["suggestions"]
        }
        self.assertEqual("promotion-check", eligible_by_target[gap["id"]]["action"])
        self.assertFalse(eligible_by_target[gap["id"]]["requires_authorization"])

        promoted = cli_json(
            "promote", "--project", self.project, "--attempt", gap["id"],
        )
        activated = cli_json("plan-next", "--project", self.project)
        activated_by_target = {
            item["target"]["id"]: item for item in activated["suggestions"]
        }
        self.assertEqual("activate-version", activated_by_target[promoted["id"]]["action"])
        self.assertNotIn(gap["id"], activated_by_target)

    def test_status_uses_existing_lock_without_create_cleanup_or_write_flags(self) -> None:
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        from workflow_core import locking
        from workflow_core.planning import status

        real_open = locking.os.open
        observed_flags: list[int] = []

        def inspect_open(path, flags, *args, **kwargs):
            observed_flags.append(flags)
            self.assertFalse(flags & os.O_CREAT)
            self.assertFalse(flags & os.O_WRONLY)
            self.assertFalse(flags & os.O_RDWR)
            return real_open(path, flags, *args, **kwargs)

        before = self._snapshot(self.project)
        with (
            mock.patch.object(locking.os, "open", side_effect=inspect_open),
            mock.patch.object(
                locking, "_open_lock_file",
                side_effect=AssertionError("read path must not create a lock"),
            ),
            mock.patch.object(
                locking, "_cleanup_lock_bootstrap_files",
                side_effect=AssertionError("read path must not clean files"),
            ),
        ):
            self.assertTrue(status(self.project)["valid"])
        self.assertTrue(observed_flags)
        self.assertEqual(before, self._snapshot(self.project))

    def test_status_waits_for_writer_and_reads_one_consistent_snapshot(self) -> None:
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        from workflow_core.locking import project_write_lock
        from workflow_core.planning import status

        started = threading.Event()

        def read_status() -> dict:
            started.set()
            return status(self.project)

        before = self._snapshot(self.project)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with project_write_lock(self.project):
                future = executor.submit(read_status)
                self.assertTrue(started.wait(timeout=1))
                with self.assertRaises(TimeoutError):
                    future.result(timeout=0.1)
            self.assertTrue(future.result(timeout=2)["valid"])
        self.assertEqual(before, self._snapshot(self.project))

    def test_plan_promotion_evaluation_stays_inside_snapshot_lock(self) -> None:
        attempts = self._eligible_cohort(1)
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        from workflow_core import locking, planning

        canonical = locking.canonical_project_path(self.project)
        original = planning._promotion_evaluation_locked

        def inspect(control: Path, attempt_id: str) -> dict:
            self.assertTrue(locking._PROCESS_LOCKS[canonical].locked())
            return original(control, attempt_id)

        with mock.patch.object(
            planning, "_promotion_evaluation_locked", side_effect=inspect,
        ):
            payload = planning.plan_next(self.project)
        suggestion = next(
            item for item in payload["suggestions"]
            if item["target"]["id"] == attempts[0]["id"]
        )
        self.assertEqual("promotion-check", suggestion["action"])

    def test_plan_holds_snapshot_while_writer_waits_and_does_not_mix_policy(self) -> None:
        attempts = self._eligible_cohort(1)
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        from workflow_core import planning
        from workflow_core.policy import set_confirmation_policy

        evaluation_started = threading.Event()
        release_evaluation = threading.Event()
        original = planning._promotion_evaluation_locked

        def pause(control: Path, attempt_id: str) -> dict:
            evaluation_started.set()
            self.assertTrue(release_evaluation.wait(timeout=2))
            return original(control, attempt_id)

        with mock.patch.object(
            planning, "_promotion_evaluation_locked", side_effect=pause,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                plan_future = executor.submit(planning.plan_next, self.project)
                self.assertTrue(evaluation_started.wait(timeout=2))
                writer = executor.submit(
                    set_confirmation_policy, self.project, 2, 2,
                )
                try:
                    with self.assertRaises(TimeoutError):
                        writer.result(timeout=0.1)
                finally:
                    release_evaluation.set()
                plan = plan_future.result(timeout=2)
                self.assertEqual(
                    {"minimum_accepted_attempts": 2, "minimum_distinct_seeds": 2},
                    writer.result(timeout=2),
                )
        suggestion = next(
            item for item in plan["suggestions"]
            if item["target"]["id"] == attempts[0]["id"]
        )
        self.assertEqual("promotion-check", suggestion["action"])

    def test_plan_evaluates_one_eight_attempt_cohort_only_once(self) -> None:
        attempts = self._eligible_cohort(8)
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        from workflow_core import planning

        original = planning._promotion_evaluation_locked
        calls: list[str] = []

        def count(control: Path, attempt_id: str) -> dict:
            calls.append(attempt_id)
            return original(control, attempt_id)

        with mock.patch.object(
            planning, "_promotion_evaluation_locked", side_effect=count,
        ):
            payload = planning.plan_next(self.project)
        promotion = [
            item for item in payload["suggestions"]
            if item["action"] == "promotion-check"
        ]
        self.assertEqual([attempts[0]["id"]], calls)
        self.assertEqual(1, len(promotion))

    def test_only_old_formal_trial_is_warned_as_legacy_trial(self) -> None:
        active = self._new_active_idea("formal legacy")
        self._install_formal_template_fixture()
        cli_json(
            "set-implementation-mapping", "--project", self.project,
            "--idea", active["id"], "--mapping", json.dumps({
                "problem": active["problem"], "mechanism": active["mechanism"],
                "attachment_point": "feature-output",
                "target_path": "models/adapter.py", "validation": "pytest -q",
                "disabled_behavior": "identity",
            }),
        )
        version = self._new_version("formal", "b" * 40)
        formal = cli_json(
            "new-trial", "--project", self.project, "--idea", active["id"],
            "--base-version", version["id"], "--template", "TPL-0001",
            "--attachment-point", "feature-output",
        )
        trial_path = self.control / "trials" / formal["id"] / "trial.json"
        legacy = read_json(trial_path)
        legacy.pop("idea_revision")
        legacy.pop("implementation_mapping_sha256")
        trial_path.write_text(json.dumps(legacy), encoding="utf-8")

        status_payload = cli_json("status", "--project", self.project)
        warning = next(
            item for item in status_payload["warnings"]
            if item["code"] == "legacy_formal_trial"
        )
        self.assertEqual(
            [{"kind": "trial", "id": formal["id"]}], warning["targets"],
        )

    def test_invalid_unknown_or_symlink_ledger_fails_without_writes(self) -> None:
        unknown = self.control / "unexpected.json"
        unknown.write_text("{}\n", encoding="utf-8")
        before = self._snapshot(self.project)
        for command in ("status", "plan-next"):
            failed = run_cli(command, "--project", self.project, check=False)
            self.assertNotEqual(0, failed.returncode)
            self.assertEqual(before, self._snapshot(self.project))
        unknown.unlink()

        project_path = self.control / "project.json"
        invalid = read_json(project_path)
        invalid["name"] = ""
        project_path.write_text(json.dumps(invalid), encoding="utf-8")
        before = self._snapshot(self.project)
        for command in ("status", "plan-next"):
            failed = run_cli(command, "--project", self.project, check=False)
            self.assertNotEqual(0, failed.returncode)
            self.assertEqual(before, self._snapshot(self.project))

    def test_control_symlink_is_rejected_without_touching_target(self) -> None:
        external = Path(self.temporary.name) / "external"
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        link = self.control / "ideas" / "IDEA-0001.json"
        try:
            link.symlink_to(sentinel)
        except OSError as error:
            self.skipTest(f"symlink unavailable: {error}")
        before = self._snapshot(self.project)
        for command in ("status", "plan-next"):
            failed = run_cli(command, "--project", self.project, check=False)
            self.assertNotEqual(0, failed.returncode)
            self.assertEqual(before, self._snapshot(self.project))
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
