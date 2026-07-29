from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from tests._helpers import SCRIPTS, cli_json, read_json, run_cli


class ResultAxesPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        cli_json("init", "--path", self.project, "--name", "result policy")
        self.control = self.project / ".experiment-workflow"
        self.idea = cli_json(
            "new-idea", "--project", self.project,
            "--title", "idea", "--note", "note",
        )
        cli_json(
            "activate-idea", "--project", self.project, "--idea", self.idea["id"],
            "--problem", "problem", "--mechanism", "mechanism",
            "--hypothesis", "hypothesis",
        )
        self.version = cli_json(
            "register-version", "--project", self.project, "--name", "baseline",
            "--repo-url", "https://example.invalid/repo", "--commit", "a" * 40,
        )
        self.trial = self._new_trial("feature-output")

    def _new_trial(self, attachment: str) -> dict:
        return cli_json(
            "new-trial", "--project", self.project, "--idea", self.idea["id"],
            "--base-version", self.version["id"],
            "--template-family", "feature-adapter",
            "--attachment-point", attachment,
        )

    def _new_attempt(self, *, target: str | None = None, seed: int = 1) -> dict:
        return cli_json(
            "new-attempt", "--project", self.project,
            "--target", target or self.trial["id"], "--type", "innovation",
            "--seed", seed, "--command", "python train.py",
        )

    def _record_axes(
        self,
        attempt_id: str,
        implementation: str = "valid",
        hypothesis: str = "supported",
    ) -> dict:
        return cli_json(
            "record-result", "--project", self.project, "--attempt", attempt_id,
            "--metrics", '{"score":0.7}',
            "--implementation-status", implementation,
            "--hypothesis-status", hypothesis, "--conclusion", "done",
        )

    def _set_policy(self, accepted: object = 1, seeds: object = 1):
        return cli_json(
            "set-confirmation-policy", "--project", self.project,
            "--minimum-accepted-attempts", accepted,
            "--minimum-distinct-seeds", seeds,
        )

    @staticmethod
    def _snapshot(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()
        }

    def test_result_v2_derives_decision_for_all_axis_combinations(self) -> None:
        expected = {
            ("valid", "supported"): "accept",
            ("valid", "not_supported"): "reject",
            ("valid", "inconclusive"): "inconclusive",
            ("valid", "not_evaluated"): "inconclusive",
            ("invalid", "supported"): "inconclusive",
            ("uncertain", "not_supported"): "inconclusive",
        }
        for number, ((implementation, hypothesis), decision) in enumerate(
            expected.items(), start=1,
        ):
            with self.subTest(implementation=implementation, hypothesis=hypothesis):
                attempt = self._new_attempt(seed=number)
                result = self._record_axes(attempt["id"], implementation, hypothesis)["result"]
                self.assertEqual("cv-experiment-workflow.result.v2", result["schema"])
                self.assertEqual(decision, result["decision"])

    def test_cli_legacy_and_dual_axes_are_exclusive_and_complete(self) -> None:
        attempts = [self._new_attempt(seed=index) for index in range(1, 4)]
        mixed = run_cli(
            "record-result", "--project", self.project, "--attempt", attempts[0]["id"],
            "--metrics", "{}", "--decision", "accept",
            "--implementation-status", "valid", "--hypothesis-status", "supported",
            "--conclusion", "bad", check=False,
        )
        half = run_cli(
            "record-result", "--project", self.project, "--attempt", attempts[1]["id"],
            "--metrics", "{}", "--implementation-status", "valid",
            "--conclusion", "bad", check=False,
        )
        none = run_cli(
            "record-result", "--project", self.project, "--attempt", attempts[2]["id"],
            "--metrics", "{}", "--conclusion", "bad", check=False,
        )
        self.assertTrue(all(item.returncode != 0 for item in (mixed, half, none)))
        self.assertEqual([], list((self.control / "attempts").glob("*.tmp")))
        self.assertTrue(all(read_json(
            self.control / "attempts" / f"{item['id']}.json"
        )["status"] == "planned" for item in attempts))

    def test_result_v2_strict_shape_and_nonfinite_metrics_are_rejected(self) -> None:
        attempt = self._new_attempt()
        before = (self.control / "attempts" / f"{attempt['id']}.json").read_bytes()
        failed = run_cli(
            "record-result", "--project", self.project, "--attempt", attempt["id"],
            "--metrics", '{"score":NaN}', "--implementation-status", "valid",
            "--hypothesis-status", "supported", "--conclusion", "bad", check=False,
        )
        self.assertNotEqual(0, failed.returncode)
        self.assertEqual(before, (self.control / "attempts" / f"{attempt['id']}.json").read_bytes())

        self._record_axes(attempt["id"])
        path = self.control / "attempts" / f"{attempt['id']}.json"
        tampered = read_json(path)
        tampered["result"]["decision"] = "reject"
        path.write_text(json.dumps(tampered), encoding="utf-8")
        self.assertNotEqual(0, run_cli("validate", "--project", self.project, check=False).returncode)

    def test_result_v2_is_idempotent_and_conflicting_retry_is_zero_write(self) -> None:
        attempt = self._new_attempt()
        first = self._record_axes(attempt["id"])
        before = (self.control / "attempts" / f"{attempt['id']}.json").read_bytes()
        second = self._record_axes(attempt["id"])
        conflict = run_cli(
            "record-result", "--project", self.project, "--attempt", attempt["id"],
            "--metrics", '{"score":0.7}', "--implementation-status", "valid",
            "--hypothesis-status", "not_supported", "--conclusion", "done", check=False,
        )
        self.assertEqual(first, second)
        self.assertNotEqual(0, conflict.returncode)
        self.assertEqual(before, (self.control / "attempts" / f"{attempt['id']}.json").read_bytes())

    def test_legacy_result_remains_valid_and_eligible(self) -> None:
        attempt = self._new_attempt()
        cli_json(
            "record-result", "--project", self.project, "--attempt", attempt["id"],
            "--metrics", "{}", "--decision", "accept", "--conclusion", "legacy",
        )
        check = cli_json("promotion-check", "--project", self.project, "--attempt", attempt["id"])
        self.assertTrue(check["eligible"])
        self.assertTrue(check["legacy_evidence"])
        self.assertTrue(cli_json("validate", "--project", self.project)["valid"])

    def test_policy_requires_strict_int_range_and_failure_is_zero_write(self) -> None:
        before = (self.control / "adapter.json").read_bytes()
        for value in (0, 101, "true"):
            with self.subTest(value=value):
                failed = run_cli(
                    "set-confirmation-policy", "--project", self.project,
                    "--minimum-accepted-attempts", value,
                    "--minimum-distinct-seeds", 1, check=False,
                )
                self.assertNotEqual(0, failed.returncode)
                self.assertEqual(before, (self.control / "adapter.json").read_bytes())

    def test_set_policy_lazily_upgrades_adapter_preserving_existing_fields(self) -> None:
        path = self.control / "adapter.json"
        adapter = read_json(path)
        adapter.update({
            "status": "bound",
            "code_sources": [{
                "repo_url": "https://example.invalid/code",
                "commit": "b" * 40,
                "relative_path": ".",
            }],
            "capabilities": {"feature-adapter": True},
        })
        path.write_text(json.dumps(adapter), encoding="utf-8")
        updated = self._set_policy(2, 3)
        persisted = read_json(path)
        self.assertEqual("cv-experiment-workflow.adapter.v2", persisted["schema"])
        self.assertEqual("bound", persisted["status"])
        self.assertEqual(adapter["code_sources"], persisted["code_sources"])
        self.assertEqual(adapter["capabilities"], persisted["capabilities"])
        self.assertEqual(updated, persisted["confirmation_policy"])

        before = path.read_bytes()
        self.assertEqual(updated, self._set_policy(2, 3))
        self.assertEqual(before, path.read_bytes())

    def test_policy_api_rejects_bool_and_v2_adapter_remains_usable_by_trial(self) -> None:
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        from workflow_core.policy import set_confirmation_policy

        before = (self.control / "adapter.json").read_bytes()
        with self.assertRaises(ValueError):
            set_confirmation_policy(self.project, True, 1)
        self.assertEqual(before, (self.control / "adapter.json").read_bytes())
        self._set_policy(2, 2)
        created = self._new_trial("feature-output")
        self.assertTrue(created["id"].startswith("TRIAL-"))

    def test_confirmation_counts_same_root_trial_base_completed_eligible_and_seeds(self) -> None:
        self._set_policy(2, 2)
        first = self._new_attempt(seed=11)
        self._record_axes(first["id"])
        # Attempt 链的 target 虽是另一个 Attempt，但根身份仍是同一 Trial。
        second = self._new_attempt(target=first["id"], seed=12)
        self._record_axes(second["id"])
        # 同一 base 的另一个 Trial、planned、reject 均不得计数。
        other_trial = self._new_trial("feature-output")
        other = self._new_attempt(target=other_trial["id"], seed=13)
        self._record_axes(other["id"])
        self._new_attempt(seed=14)
        rejected = self._new_attempt(seed=15)
        self._record_axes(rejected["id"], "valid", "not_supported")

        check = cli_json("promotion-check", "--project", self.project, "--attempt", second["id"])
        self.assertTrue(check["eligible"])
        self.assertEqual(2, check["counts"]["accepted_attempts"])
        self.assertEqual(2, check["counts"]["distinct_seeds"])
        self.assertEqual([], check["reasons"])

    def test_confirmation_shortfall_is_structured_and_promote_uses_same_gate(self) -> None:
        self._set_policy(2, 2)
        attempt = self._new_attempt(seed=7)
        self._record_axes(attempt["id"])
        check = cli_json("promotion-check", "--project", self.project, "--attempt", attempt["id"])
        self.assertFalse(check["eligible"])
        self.assertTrue(check["reasons"])
        self.assertEqual(1, check["counts"]["accepted_attempts"])
        before = self._snapshot(self.control / "versions")
        failed = run_cli(
            "promote", "--project", self.project, "--attempt", attempt["id"], check=False,
        )
        self.assertNotEqual(0, failed.returncode)
        self.assertEqual(before, self._snapshot(self.control / "versions"))

    def test_default_policy_preserves_single_accept_behavior(self) -> None:
        attempt = self._new_attempt()
        self._record_axes(attempt["id"])
        check = cli_json("promotion-check", "--project", self.project, "--attempt", attempt["id"])
        self.assertTrue(check["eligible"])
        self.assertEqual(1, check["counts"]["required_accepted_attempts"])
        self.assertEqual(1, check["counts"]["required_distinct_seeds"])
        self.assertEqual("draft", cli_json(
            "promote", "--project", self.project, "--attempt", attempt["id"],
        )["status"])

    def test_check_and_promote_freeze_same_cohort_and_policy_raise_keeps_draft_valid(self) -> None:
        self._set_policy(2, 2)
        first = self._new_attempt(seed=21)
        self._record_axes(first["id"])
        second = self._new_attempt(target=first["id"], seed=22)
        self._record_axes(second["id"])

        check = cli_json("promotion-check", "--project", self.project, "--attempt", second["id"])
        draft = cli_json("promote", "--project", self.project, "--attempt", second["id"])
        self.assertEqual(check["qualified_attempt_ids"], draft["accepted_attempt_ids"])
        self.assertEqual([first["id"], second["id"]], draft["accepted_attempt_ids"])

        self._set_policy(3, 3)
        validation = cli_json("validate", "--project", self.project)
        self.assertTrue(validation["valid"])
        self.assertEqual(1, validation["draft_versions"])

    def test_mixed_cohort_reports_legacy_evidence_for_every_anchor(self) -> None:
        self._set_policy(2, 2)
        legacy = self._new_attempt(seed=51)
        cli_json(
            "record-result", "--project", self.project, "--attempt", legacy["id"],
            "--metrics", "{}", "--decision", "accept", "--conclusion", "legacy",
        )
        dual = self._new_attempt(seed=52)
        self._record_axes(dual["id"])

        expected_ids = [legacy["id"], dual["id"]]
        for anchor in (legacy, dual):
            with self.subTest(anchor=anchor["id"]):
                check = cli_json(
                    "promotion-check", "--project", self.project,
                    "--attempt", anchor["id"],
                )
                self.assertTrue(check["eligible"])
                self.assertEqual(expected_ids, check["qualified_attempt_ids"])
                self.assertTrue(check["legacy_evidence"])

        promoted = cli_json(
            "promote", "--project", self.project, "--attempt", dual["id"],
        )
        self.assertEqual(expected_ids, promoted["accepted_attempt_ids"])

    def test_promote_reuses_valid_legacy_draft_with_unsorted_evidence_ids(self) -> None:
        first = self._new_attempt(seed=61)
        cli_json(
            "record-result", "--project", self.project, "--attempt", first["id"],
            "--metrics", "{}", "--decision", "accept", "--conclusion", "first",
        )
        second = self._new_attempt(target=first["id"], seed=62)
        cli_json(
            "record-result", "--project", self.project, "--attempt", second["id"],
            "--metrics", "{}", "--decision", "accept", "--conclusion", "second",
        )
        draft = cli_json(
            "promote", "--project", self.project, "--attempt", second["id"],
        )
        draft_path = self.control / "versions" / f"{draft['id']}.json"
        legacy = read_json(draft_path)
        legacy["accepted_attempt_ids"] = [second["id"], first["id"]]
        draft_path.write_text(json.dumps(legacy), encoding="utf-8")
        self.assertTrue(cli_json("validate", "--project", self.project)["valid"])

        before = self._snapshot(self.control / "versions")
        retried = cli_json(
            "promote", "--project", self.project, "--attempt", first["id"],
        )
        self.assertEqual(legacy, retried)
        self.assertEqual(before, self._snapshot(self.control / "versions"))

    def test_promote_reuses_multi_trial_draft_with_historical_asset_order(self) -> None:
        other_trial = self._new_trial("feature-output")
        attempts: list[dict] = []
        for seed, trial in ((71, self.trial), (72, other_trial)):
            attempt = self._new_attempt(target=trial["id"], seed=seed)
            cli_json(
                "record-result", "--project", self.project,
                "--attempt", attempt["id"], "--metrics", "{}",
                "--decision", "accept", "--conclusion", "legacy",
            )
            attempts.append(attempt)
        draft = cli_json(
            "promote", "--project", self.project,
            "--attempt", attempts[0]["id"], "--attempt", attempts[1]["id"],
        )
        path = self.control / "versions" / f"{draft['id']}.json"
        legacy = read_json(path)
        legacy["accepted_attempt_ids"].reverse()
        legacy["ordered_code_asset_ids"].reverse()
        path.write_text(json.dumps(legacy), encoding="utf-8")
        self.assertTrue(cli_json("validate", "--project", self.project)["valid"])

        before = self._snapshot(self.control / "versions")
        retried = cli_json(
            "promote", "--project", self.project,
            "--attempt", attempts[1]["id"], "--attempt", attempts[0]["id"],
        )
        self.assertEqual(legacy, retried)
        self.assertEqual(before, self._snapshot(self.control / "versions"))

    def test_promote_rejects_ambiguous_legacy_evidence_set_without_writing(self) -> None:
        trials = [self.trial, self._new_trial("feature-output"), self._new_trial("feature-output")]
        attempts: list[dict] = []
        for seed, trial in zip((81, 82, 83), trials):
            attempt = self._new_attempt(target=trial["id"], seed=seed)
            cli_json(
                "record-result", "--project", self.project,
                "--attempt", attempt["id"], "--metrics", "{}",
                "--decision", "accept", "--conclusion", "legacy",
            )
            attempts.append(attempt)
        canonical = cli_json(
            "promote", "--project", self.project,
            *(part for attempt in attempts for part in ("--attempt", attempt["id"])),
        )
        first_path = self.control / "versions" / f"{canonical['id']}.json"
        first = read_json(first_path)
        attempt_ids = [item["id"] for item in attempts]
        asset_by_attempt = {
            item["id"]: read_json(
                self.control / "attempts" / f"{item['id']}.json"
            )["ordered_code_asset_ids"][0]
            for item in attempts
        }
        first_order = [attempt_ids[1], attempt_ids[0], attempt_ids[2]]
        first["accepted_attempt_ids"] = first_order
        first["ordered_code_asset_ids"] = [asset_by_attempt[item] for item in first_order]
        first_path.write_text(json.dumps(first), encoding="utf-8")

        second = dict(first)
        second["id"] = "VER-0003"
        second_order = [attempt_ids[2], attempt_ids[0], attempt_ids[1]]
        second["accepted_attempt_ids"] = second_order
        second["ordered_code_asset_ids"] = [asset_by_attempt[item] for item in second_order]
        (self.control / "versions" / "VER-0003.json").write_text(
            json.dumps(second), encoding="utf-8",
        )
        self.assertTrue(cli_json("validate", "--project", self.project)["valid"])

        before = self._snapshot(self.control / "versions")
        failed = run_cli(
            "promote", "--project", self.project,
            *(part for attempt in attempts for part in ("--attempt", attempt["id"])),
            check=False,
        )
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("多个", failed.stderr)
        self.assertEqual(before, self._snapshot(self.control / "versions"))

    def test_multi_trial_promotion_requires_each_cohort_then_freezes_union(self) -> None:
        self._set_policy(2, 2)
        other_trial = self._new_trial("feature-output")
        first_a = self._new_attempt(seed=31)
        self._record_axes(first_a["id"])
        second_a = self._new_attempt(target=first_a["id"], seed=32)
        self._record_axes(second_a["id"])
        first_b = self._new_attempt(target=other_trial["id"], seed=41)
        self._record_axes(first_b["id"])

        failed = run_cli(
            "promote", "--project", self.project,
            "--attempt", second_a["id"], "--attempt", first_b["id"], check=False,
        )
        self.assertNotEqual(0, failed.returncode)
        self.assertEqual(["VER-0001.json"], sorted(
            path.name for path in (self.control / "versions").glob("*.json")
        ))

        second_b = self._new_attempt(target=first_b["id"], seed=42)
        self._record_axes(second_b["id"])
        promoted = cli_json(
            "promote", "--project", self.project,
            "--attempt", second_a["id"], "--attempt", second_b["id"],
        )
        self.assertEqual(
            sorted([first_a["id"], second_a["id"], first_b["id"], second_b["id"]]),
            promoted["accepted_attempt_ids"],
        )

    def test_invalid_policy_tampering_fails_global_validation(self) -> None:
        self._set_policy(2, 2)
        path = self.control / "adapter.json"
        tampered = read_json(path)
        tampered["confirmation_policy"]["minimum_distinct_seeds"] = True
        path.write_text(json.dumps(tampered), encoding="utf-8")
        failed = run_cli("validate", "--project", self.project, check=False)
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("confirmation", failed.stderr)


if __name__ == "__main__":
    unittest.main()
