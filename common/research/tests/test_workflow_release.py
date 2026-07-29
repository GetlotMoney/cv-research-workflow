from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._helpers import ROOT, SCRIPTS, run_cli


SKILL_ROOT = ROOT / "skills" / "cv-experiment-workflow"


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class WorkflowReleaseTests(unittest.TestCase):
    POST_CLEANUP_PRE_MANIFEST_LOCK = {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.5.0",
        "system_version": "SYS-V2.13.0",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 214,
            "digest": (
                "sha256:"
                "fac9322368559cb858c13f23705f01b0fbcefd7bcf367d373"
                "3f7cc261846584b"
            ),
        },
    }
    CONSOLIDATED_PRE_CLEANUP_LOCK = {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.5.0",
        "system_version": "SYS-V2.13.0",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 214,
            "digest": (
                "sha256:"
                "58e7041985a3deb9f2deaf7b30cc08d7be456407bba701156"
                "c15ce6da16a2154"
            ),
        },
    }
    OLD_1_5_0_PREFINAL_LOCK = {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.5.0",
        "system_version": "SYS-V2.13.0",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 214,
            "digest": (
                "sha256:"
                "d2af72b9220007ea5d84f6dba9a836fbeb058ef21b79d667"
                "3668bdd76d94089e"
            ),
        },
    }
    OLD_1_4_0_LOCK = {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.4.0",
        "system_version": "SYS-V2.12.0",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 211,
            "digest": (
                "sha256:"
                "c54ba663dc3cab4ccb50f304ff8a539cae1f79aabac69bc"
                "7015f4ce715aa5dad"
            ),
        },
    }
    OLD_1_2_3_LOCK = {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.2.3",
        "system_version": "SYS-V2.10.3",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 57,
            "digest": (
                "sha256:"
                "a610932129c3ee8397f7a1d887b3b485fb2f283a67af8a92"
                "d668a9d7e977592a"
            ),
        },
    }

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        run_cli(
            "init",
            "--path",
            self.project,
            "--name",
            "release-check",
            "--layout",
            "v2",
        )

    def _check(self, source_skill: Path) -> dict[str, object]:
        result = run_cli(
            "check-workflow-drift",
            "--project",
            self.project,
            "--source-skill",
            source_skill,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIsInstance(payload, dict)
        return payload

    def _upgrade(self, source_skill: Path, *, check: bool = True):
        return run_cli(
            "upgrade-workflow-lock",
            "--project",
            self.project,
            "--source-skill",
            source_skill,
            check=check,
        )

    def test_payload_fingerprint_is_stable_and_ignores_runtime_caches(self) -> None:
        copied = self.root / "copied-skill"
        shutil.copytree(SKILL_ROOT, copied)
        first = self._check(copied)
        cache = copied / "scripts" / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "ignored.pyc").write_bytes(b"runtime cache")

        second = self._check(copied)

        self.assertEqual(first["source_payload"], second["source_payload"])

    def test_new_project_is_pinned_to_the_runtime_payload(
        self,
    ) -> None:
        before = _tree_bytes(self.project)

        result = self._check(SKILL_ROOT)

        self.assertEqual("in_sync", result["status"])
        self.assertTrue(result["runtime_matches_source"])
        self.assertTrue(result["project_matches_runtime"])
        self.assertGreater(result["runtime_payload"]["file_count"], 0)
        self.assertEqual(
            result["runtime_payload"]["file_count"],
            result["source_payload"]["file_count"],
        )
        self.assertEqual(before, _tree_bytes(self.project))

    def test_legacy_lock_is_readable_and_requires_explicit_upgrade(self) -> None:
        lock_path = self.project / ".experiment-workflow" / "workflow.lock.json"
        lock_path.write_text(
            json.dumps(
                {
                    "schema": "cv-experiment-workflow.workflow-lock.v1",
                    "skill_id": "cv-experiment-workflow",
                    "version": "1.0.0",
                }
            ),
            encoding="utf-8",
        )
        before = _tree_bytes(self.project)

        checked = self._check(SKILL_ROOT)

        self.assertEqual("legacy_unpinned", checked["status"])
        self.assertEqual(before, _tree_bytes(self.project))

        upgraded = json.loads(self._upgrade(SKILL_ROOT).stdout)
        self.assertEqual("upgraded", upgraded["status"])
        self.assertEqual(
            "cv-experiment-workflow.workflow-lock.v2",
            upgraded["workflow_lock"]["schema"],
        )
        self.assertEqual(
            checked["runtime_payload"],
            upgraded["workflow_lock"]["payload"],
        )
        self.assertEqual("in_sync", self._check(SKILL_ROOT)["status"])

    def test_upgrade_rejects_changed_source_without_writing(self) -> None:
        lock_path = self.project / ".experiment-workflow" / "workflow.lock.json"
        lock_path.write_text(
            json.dumps(
                {
                    "schema": "cv-experiment-workflow.workflow-lock.v1",
                    "skill_id": "cv-experiment-workflow",
                    "version": "1.0.0",
                }
            ),
            encoding="utf-8",
        )
        changed = self.root / "changed-for-upgrade"
        shutil.copytree(SKILL_ROOT, changed)
        skill_file = changed / "SKILL.md"
        skill_file.write_bytes(skill_file.read_bytes() + b"\n# changed\n")
        before = _tree_bytes(self.project)

        failed = self._upgrade(changed, check=False)

        self.assertEqual(2, failed.returncode)
        self.assertIn("不一致", failed.stderr)
        self.assertEqual(before, _tree_bytes(self.project))

    def test_validate_rejects_tampered_pinned_payload(self) -> None:
        lock_path = self.project / ".experiment-workflow" / "workflow.lock.json"
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        payload["payload"]["digest"] = "sha256:" + "0" * 64
        lock_path.write_text(json.dumps(payload), encoding="utf-8")

        failed = run_cli("validate", "--project", self.project, check=False)

        self.assertEqual(2, failed.returncode)
        self.assertIn("workflow lock", failed.stderr)

    def test_upgrade_rejects_fake_or_damaged_project_without_writing(self) -> None:
        fake = self.root / "fake"
        fake_control = fake / ".experiment-workflow"
        fake_control.mkdir(parents=True)
        fake_lock = fake_control / "workflow.lock.json"
        fake_lock.write_text(
            json.dumps(
                {
                    "schema": "cv-experiment-workflow.workflow-lock.v1",
                    "skill_id": "cv-experiment-workflow",
                    "version": "1.0.0",
                }
            ),
            encoding="utf-8",
        )
        fake_before = _tree_bytes(fake)

        fake_result = run_cli(
            "upgrade-workflow-lock",
            "--project",
            fake,
            "--source-skill",
            SKILL_ROOT,
            check=False,
        )

        self.assertEqual(2, fake_result.returncode)
        self.assertEqual(fake_before, _tree_bytes(fake))

        lock_path = self.project / ".experiment-workflow" / "workflow.lock.json"
        lock_path.write_text(
            json.dumps(
                {
                    "schema": "cv-experiment-workflow.workflow-lock.v1",
                    "skill_id": "cv-experiment-workflow",
                    "version": "1.0.0",
                }
            ),
            encoding="utf-8",
        )
        damaged = self.project / ".experiment-workflow" / "tasks" / "bad.json"
        damaged.write_text("{}", encoding="utf-8")
        damaged_before = _tree_bytes(self.project)

        damaged_result = self._upgrade(SKILL_ROOT, check=False)

        self.assertEqual(2, damaged_result.returncode)
        self.assertEqual(damaged_before, _tree_bytes(self.project))

    def test_upgrade_accepts_well_formed_older_v2_lock(self) -> None:
        lock_path = self.project / ".experiment-workflow" / "workflow.lock.json"
        old_v2 = {
            "schema": "cv-experiment-workflow.workflow-lock.v2",
            "skill_id": "cv-experiment-workflow",
            "release_version": "1.1.0",
            "system_version": "SYS-V2.9",
            "payload": {
                "algorithm": "sha256-path-bytes-v1",
                "file_count": 55,
                "digest": (
                    "sha256:"
                    "56807d433ae8fc81bc8cc2cce75a7f4614cf4ac80601f5c3"
                    "a1ad695e6c0bbc27"
                ),
            },
        }
        lock_path.write_text(json.dumps(old_v2), encoding="utf-8")

        upgraded = json.loads(self._upgrade(SKILL_ROOT).stdout)

        self.assertEqual("upgraded", upgraded["status"])
        self.assertEqual("1.1.0", upgraded["previous_lock"]["release_version"])
        self.assertEqual("1.5.0", upgraded["workflow_lock"]["release_version"])
        self.assertEqual("in_sync", self._check(SKILL_ROOT)["status"])

    def test_exact_123_lock_is_trusted_but_130_is_not_an_upgrade_source(
        self,
    ) -> None:
        lock_path = self.project / ".experiment-workflow" / "workflow.lock.json"
        lock_path.write_text(
            json.dumps(self.OLD_1_2_3_LOCK),
            encoding="utf-8",
        )

        upgraded = json.loads(self._upgrade(SKILL_ROOT).stdout)

        self.assertEqual("upgraded", upgraded["status"])
        self.assertEqual(
            "1.2.3",
            upgraded["previous_lock"]["release_version"],
        )
        self.assertEqual("1.5.0", upgraded["workflow_lock"]["release_version"])
        self.assertEqual(
            "SYS-V2.13.0",
            upgraded["workflow_lock"]["system_version"],
        )

        import sys

        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(sys.path.remove, str(SCRIPTS))
        from workflow_core.releases import TRUSTED_PREVIOUS_WORKFLOW_LOCKS

        self.assertFalse(
            any(
                lock["release_version"] == "1.3.0"
                for lock in TRUSTED_PREVIOUS_WORKFLOW_LOCKS
            )
        )

    def test_exact_140_lock_remains_an_explicit_upgrade_source(self) -> None:
        lock_path = self.project / ".experiment-workflow" / "workflow.lock.json"
        lock_path.write_text(
            json.dumps(self.OLD_1_4_0_LOCK),
            encoding="utf-8",
        )

        upgraded = json.loads(self._upgrade(SKILL_ROOT).stdout)

        self.assertEqual("upgraded", upgraded["status"])
        self.assertEqual(
            "1.4.0",
            upgraded["previous_lock"]["release_version"],
        )
        self.assertEqual(
            "1.5.0",
            upgraded["workflow_lock"]["release_version"],
        )
        self.assertEqual(
            "SYS-V2.13.0",
            upgraded["workflow_lock"]["system_version"],
        )

    def test_prefinal_150_lock_remains_an_explicit_upgrade_source(self) -> None:
        lock_path = self.project / ".experiment-workflow" / "workflow.lock.json"
        lock_path.write_text(
            json.dumps(self.OLD_1_5_0_PREFINAL_LOCK),
            encoding="utf-8",
        )

        upgraded = json.loads(self._upgrade(SKILL_ROOT).stdout)

        self.assertEqual("upgraded", upgraded["status"])
        self.assertEqual(
            self.OLD_1_5_0_PREFINAL_LOCK,
            upgraded["previous_lock"],
        )
        self.assertNotEqual(
            self.OLD_1_5_0_PREFINAL_LOCK["payload"],
            upgraded["workflow_lock"]["payload"],
        )

    def test_consolidated_pre_cleanup_lock_is_an_upgrade_source(self) -> None:
        lock_path = self.project / ".experiment-workflow" / "workflow.lock.json"
        lock_path.write_text(
            json.dumps(self.CONSOLIDATED_PRE_CLEANUP_LOCK),
            encoding="utf-8",
        )

        upgraded = json.loads(self._upgrade(SKILL_ROOT).stdout)

        self.assertEqual("upgraded", upgraded["status"])
        self.assertEqual(
            self.CONSOLIDATED_PRE_CLEANUP_LOCK,
            upgraded["previous_lock"],
        )
        self.assertNotEqual(
            self.CONSOLIDATED_PRE_CLEANUP_LOCK["payload"],
            upgraded["workflow_lock"]["payload"],
        )

    def test_post_cleanup_pre_manifest_lock_is_an_upgrade_source(self) -> None:
        lock_path = self.project / ".experiment-workflow" / "workflow.lock.json"
        lock_path.write_text(
            json.dumps(self.POST_CLEANUP_PRE_MANIFEST_LOCK),
            encoding="utf-8",
        )

        upgraded = json.loads(self._upgrade(SKILL_ROOT).stdout)

        self.assertEqual("upgraded", upgraded["status"])
        self.assertEqual(
            self.POST_CLEANUP_PRE_MANIFEST_LOCK,
            upgraded["previous_lock"],
        )
        self.assertNotEqual(
            self.POST_CLEANUP_PRE_MANIFEST_LOCK["payload"],
            upgraded["workflow_lock"]["payload"],
        )

    def test_upgrade_rejects_unknown_v2_lock_without_writing(self) -> None:
        lock_path = self.project / ".experiment-workflow" / "workflow.lock.json"
        unknown = json.loads(lock_path.read_text(encoding="utf-8"))
        unknown["payload"]["digest"] = "sha256:" + "9" * 64
        lock_path.write_text(json.dumps(unknown), encoding="utf-8")
        before = _tree_bytes(self.project)

        failed = self._upgrade(SKILL_ROOT, check=False)

        self.assertEqual(2, failed.returncode)
        self.assertIn("不受信任", failed.stderr)
        self.assertEqual(before, _tree_bytes(self.project))

    def test_upgrade_rechecks_source_payload_inside_write_lock(self) -> None:
        copied = self.root / "source-changing-during-upgrade"
        shutil.copytree(SKILL_ROOT, copied)
        before = _tree_bytes(self.project)
        import sys

        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(sys.path.remove, str(SCRIPTS))
        import workflow_core.releases as releases

        real_fingerprint = releases.skill_payload_fingerprint
        copied_calls = 0

        def change_on_second_source_read(path: Path) -> dict[str, object]:
            nonlocal copied_calls
            payload = real_fingerprint(path)
            if Path(path).resolve() == copied.resolve():
                copied_calls += 1
                if copied_calls == 2:
                    return {
                        **payload,
                        "digest": "sha256:" + "7" * 64,
                    }
            return payload

        with mock.patch.object(
            releases,
            "skill_payload_fingerprint",
            side_effect=change_on_second_source_read,
        ):
            with self.assertRaisesRegex(ValueError, "核验期间发生变化"):
                releases.upgrade_workflow_lock(self.project, copied)

        self.assertEqual(2, copied_calls)
        self.assertEqual(before, _tree_bytes(self.project))

    def test_read_only_drift_check_detects_source_runtime_byte_change(self) -> None:
        changed = self.root / "changed-skill"
        shutil.copytree(SKILL_ROOT, changed)
        skill_file = changed / "SKILL.md"
        skill_file.write_bytes(skill_file.read_bytes() + b"\n# changed\n")

        result = self._check(changed)

        self.assertEqual("source_runtime_drift", result["status"])
        self.assertFalse(result["runtime_matches_source"])

    def test_drift_check_rejects_non_skill_directory(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()

        result = run_cli(
            "check-workflow-drift",
            "--project",
            self.project,
            "--source-skill",
            empty,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("Skill", result.stderr)


if __name__ == "__main__":
    unittest.main()
