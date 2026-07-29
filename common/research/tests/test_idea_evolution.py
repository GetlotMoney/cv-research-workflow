from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from tests._helpers import cli_json, read_json, run_cli


class IdeaRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project = Path(self.temp_dir.name) / "project"
        cli_json("init", "--path", self.project, "--name", "Idea 演化")
        draft = cli_json(
            "new-idea", "--project", self.project,
            "--title", "轻量创新", "--note", "验证 Idea 演化",
        )
        self.idea = cli_json(
            "activate-idea", "--project", self.project, "--idea", draft["id"],
            "--problem", "旧问题", "--mechanism", "旧机制",
            "--hypothesis", "旧假设",
        )
        version = cli_json(
            "register-version", "--project", self.project, "--name", "基线",
            "--repo-url", "https://example.invalid/repo", "--commit", "a" * 40,
        )
        trial = cli_json(
            "new-trial", "--project", self.project, "--idea", self.idea["id"],
            "--base-version", version["id"],
            "--template-family", "feature-adapter",
            "--attachment-point", "feature-output",
        )
        self.attempt = cli_json(
            "new-attempt", "--project", self.project, "--target", trial["id"],
            "--type", "innovation", "--seed", 1,
            "--command", "python train.py", "--config", "{}",
        )

    def _complete_attempt(self) -> None:
        cli_json(
            "record-result", "--project", self.project,
            "--attempt", self.attempt["id"], "--metrics", "{}",
            "--decision", "inconclusive", "--conclusion", "支持 Idea 修订证据",
        )

    def test_revise_v1_idea_lazily_upgrades_and_preserves_previous_revision(self) -> None:
        self._complete_attempt()
        result = cli_json(
            "revise-idea", "--project", self.project, "--idea", self.idea["id"],
            "--reason", "实验暴露问题定义偏窄", "--problem", "新问题",
            "--evidence", json.dumps([self.attempt["id"]]),
        )

        self.assertEqual("cv-experiment-workflow.idea.v2", result["schema"])
        self.assertEqual(2, result["revision"])
        self.assertEqual("新问题", result["problem"])
        self.assertIsNone(result["implementation_mapping"])
        self.assertEqual(
            {
                "revision": 1,
                "problem": "旧问题",
                "mechanism": "旧机制",
                "hypothesis": "旧假设",
                "implementation_mapping": None,
                "revision_reason": "实验暴露问题定义偏窄",
                "evidence_refs": [self.attempt["id"]],
                "revised_at": result["revision_history"][0]["revised_at"],
            },
            result["revision_history"][0],
        )
        self.assertEqual(
            result,
            read_json(
                self.project / ".experiment-workflow" / "ideas"
                / f"{self.idea['id']}.json"
            ),
        )

    def test_set_mapping_lazily_upgrades_v1_and_is_byte_idempotent(self) -> None:
        mapping = {
            "problem": "旧问题",
            "mechanism": "旧机制",
            "attachment_point": "feature-output",
            "target_path": "models/adapter.py",
            "validation": "pytest tests/test_adapter.py",
            "disabled_behavior": "返回原输入",
        }
        args = (
            "set-implementation-mapping", "--project", self.project,
            "--idea", self.idea["id"], "--mapping", json.dumps(mapping),
        )

        first = cli_json(*args)
        path = (
            self.project / ".experiment-workflow" / "ideas"
            / f"{self.idea['id']}.json"
        )
        before = path.read_bytes()
        second = cli_json(*args)

        self.assertEqual("cv-experiment-workflow.idea.v2", first["schema"])
        self.assertEqual(1, first["revision"])
        self.assertEqual(mapping, first["implementation_mapping"])
        self.assertEqual(first, second)
        self.assertEqual(before, path.read_bytes())

    def test_revise_rejects_no_change_or_bad_evidence_without_writes(self) -> None:
        path = (
            self.project / ".experiment-workflow" / "ideas"
            / f"{self.idea['id']}.json"
        )
        before = path.read_bytes()
        cases = (
            ("--reason", "无实际变化", "--problem", "旧问题", "--evidence", json.dumps([self.attempt["id"]])),
            ("--reason", "重复证据", "--problem", "新问题", "--evidence", json.dumps([self.attempt["id"], self.attempt["id"]])),
            ("--reason", "缺失证据", "--problem", "新问题", "--evidence", '["ATTEMPT-9999"]'),
            ("--reason", "无实验证据", "--problem", "新问题", "--evidence", json.dumps([self.idea["id"]])),
            ("--reason", "", "--problem", "新问题", "--evidence", json.dumps([self.attempt["id"]])),
        )
        for extra in cases:
            with self.subTest(extra=extra):
                result = run_cli(
                    "revise-idea", "--project", self.project,
                    "--idea", self.idea["id"], *extra, check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertEqual(before, path.read_bytes())

    def test_mapping_rejects_bad_shape_drift_and_conflict_without_writes(self) -> None:
        path = (
            self.project / ".experiment-workflow" / "ideas"
            / f"{self.idea['id']}.json"
        )
        valid = {
            "problem": "旧问题", "mechanism": "旧机制",
            "attachment_point": "feature-output",
            "target_path": "models/adapter.py", "validation": "pytest -q",
            "disabled_behavior": "identity",
        }
        invalid = []
        missing = dict(valid)
        missing.pop("validation")
        invalid.append(missing)
        invalid.append({**valid, "extra": "x"})
        invalid.append({**valid, "validation": " "})
        invalid.append({**valid, "target_path": "../escape.py"})
        invalid.append({**valid, "problem": "漂移问题"})
        before = path.read_bytes()
        for mapping in invalid:
            with self.subTest(mapping=mapping):
                result = run_cli(
                    "set-implementation-mapping", "--project", self.project,
                    "--idea", self.idea["id"], "--mapping", json.dumps(mapping),
                    check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertEqual(before, path.read_bytes())

        cli_json(
            "set-implementation-mapping", "--project", self.project,
            "--idea", self.idea["id"], "--mapping", json.dumps(valid),
        )
        mapped = path.read_bytes()
        result = run_cli(
            "set-implementation-mapping", "--project", self.project,
            "--idea", self.idea["id"],
            "--mapping", json.dumps({**valid, "validation": "python validate.py"}),
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertEqual(mapped, path.read_bytes())

    def test_problem_or_mechanism_revision_invalidates_mapping_and_archives_it(self) -> None:
        self._complete_attempt()
        mapping = {
            "problem": "旧问题", "mechanism": "旧机制",
            "attachment_point": "feature-output",
            "target_path": "models/adapter.py", "validation": "pytest -q",
            "disabled_behavior": "identity",
        }
        cli_json(
            "set-implementation-mapping", "--project", self.project,
            "--idea", self.idea["id"], "--mapping", json.dumps(mapping),
        )
        revised = cli_json(
            "revise-idea", "--project", self.project, "--idea", self.idea["id"],
            "--reason", "机制需要调整", "--mechanism", "新机制",
            "--evidence", json.dumps([self.attempt["id"]]),
        )
        self.assertIsNone(revised["implementation_mapping"])
        self.assertEqual(mapping, revised["revision_history"][0]["implementation_mapping"])

        path = (
            self.project / ".experiment-workflow" / "ideas"
            / f"{self.idea['id']}.json"
        )
        tampered = read_json(path)
        tampered["revision_history"][0]["implementation_mapping"]["problem"] = "漂移"
        path.write_text(json.dumps(tampered), encoding="utf-8")
        invalid = run_cli("validate", "--project", self.project, check=False)
        self.assertEqual(2, invalid.returncode)

    def test_planned_attempt_cannot_be_revision_evidence_and_leaves_idea_unchanged(self) -> None:
        path = (
            self.project / ".experiment-workflow" / "ideas"
            / f"{self.idea['id']}.json"
        )
        before = path.read_bytes()

        result = run_cli(
            "revise-idea", "--project", self.project, "--idea", self.idea["id"],
            "--reason", "planned 还没有 Result", "--problem", "新问题",
            "--evidence", json.dumps([self.attempt["id"]]), check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("completed", result.stderr)
        self.assertEqual(before, path.read_bytes())

    def test_formal_trial_rejects_mapping_drift_without_residue(self) -> None:
        self._write_project_template()
        mapping = {
            "problem": "旧问题", "mechanism": "旧机制",
            "attachment_point": "feature-output",
            "target_path": "models/adapter.py", "validation": "pytest -q",
            "disabled_behavior": "identity",
        }
        cli_json(
            "set-implementation-mapping", "--project", self.project,
            "--idea", self.idea["id"], "--mapping", json.dumps(mapping),
        )
        control = self.project / ".experiment-workflow"
        idea_path = control / "ideas" / f"{self.idea['id']}.json"
        original = idea_path.read_bytes()
        cases = (
            {**mapping, "problem": "漂移"},
            {**mapping, "mechanism": "漂移"},
            {**mapping, "attachment_point": "other-output"},
            {**mapping, "target_path": "models/other.py"},
            {**mapping, "disabled_behavior": "zero"},
            {**mapping, "validation": " "},
        )
        for drifted in cases:
            with self.subTest(drifted=drifted):
                payload = json.loads(original)
                payload["implementation_mapping"] = drifted
                idea_path.write_text(json.dumps(payload), encoding="utf-8")
                before_trials = sorted(path.name for path in (control / "trials").iterdir())
                before_assets = sorted(path.name for path in (control / "code-assets").iterdir())
                result = run_cli(
                    "new-trial", "--project", self.project, "--idea", self.idea["id"],
                    "--base-version", "VER-0001", "--template", "TPL-0001",
                    "--attachment-point", "feature-output", check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertEqual(before_trials, sorted(path.name for path in (control / "trials").iterdir()))
                self.assertEqual(before_assets, sorted(path.name for path in (control / "code-assets").iterdir()))
                idea_path.write_bytes(original)

    def _write_project_template(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": "cv-experiment-workflow.project-template.v1",
            "template_id": "TPL-0001",
            "name": "映射门禁模板",
            "description": "只验证 Idea mapping 与 attachment 一致性",
            "code_source": {
                "repo_url": "https://example.invalid/repo",
                "commit": "a" * 40,
                "template_path": "templates/adapter",
            },
            "listed_files": [
                {"path": "module.py", "role": "入口", "digest": "sha256:" + "b" * 64}
            ],
            "interfaces": {
                "checkpoint": "path", "config": "mapping", "data": "batch",
                "evaluation": "evaluate", "metrics": "mapping", "model": "model",
                "seed": "integer", "training": "train",
            },
            "attachment_points": [{
                "name": "feature-output", "contract": "tensor -> tensor",
                "target_path": "models/adapter.py", "disabled_behavior": "identity",
            }],
            "provenance_sources": [{
                "label": "测试", "locator": "fixture:adapter", "identity": "测试夹具",
                "commit": None, "license_spdx": "MIT", "files": [], "symbols": [],
                "use_mode": "reimplementation", "note": "仅测试",
            }],
            "framework": {
                "nodes": [{"id": "adapter", "label": "Adapter", "kind": "model"}],
                "edges": [],
            },
            "verification": {
                "git_head": "a" * 40, "listed_file_count": 1, "verified": True,
            },
        }
        path = (
            self.project / ".experiment-workflow" / "templates" / "TPL-0001.json"
        )
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        return payload

    @staticmethod
    def _formal_mapping() -> dict[str, str]:
        return {
            "problem": "旧问题", "mechanism": "旧机制",
            "attachment_point": "feature-output",
            "target_path": "models/adapter.py", "validation": "pytest -q",
            "disabled_behavior": "identity",
        }

    def _create_mapped_formal_trial(self) -> tuple[dict[str, str], dict[str, object]]:
        self._write_project_template()
        mapping = self._formal_mapping()
        cli_json(
            "set-implementation-mapping", "--project", self.project,
            "--idea", self.idea["id"], "--mapping", json.dumps(mapping),
        )
        trial = cli_json(
            "new-trial", "--project", self.project, "--idea", self.idea["id"],
            "--base-version", "VER-0001", "--template", "TPL-0001",
            "--attachment-point", "feature-output",
        )
        return mapping, trial

    def test_formal_trial_requires_matching_implementation_mapping(self) -> None:
        self._write_project_template()
        control = self.project / ".experiment-workflow"
        before_trials = sorted((control / "trials").iterdir())
        before_assets = sorted((control / "code-assets").iterdir())
        missing = run_cli(
            "new-trial", "--project", self.project, "--idea", self.idea["id"],
            "--base-version", "VER-0001", "--template", "TPL-0001",
            "--attachment-point", "feature-output", check=False,
        )
        self.assertNotEqual(0, missing.returncode)
        self.assertIn("implementation mapping", missing.stderr)
        self.assertEqual(before_trials, sorted((control / "trials").iterdir()))
        self.assertEqual(before_assets, sorted((control / "code-assets").iterdir()))

        mapping = {
            "problem": "旧问题", "mechanism": "旧机制",
            "attachment_point": "feature-output",
            "target_path": "models/adapter.py", "validation": "pytest -q",
            "disabled_behavior": "identity",
        }
        cli_json(
            "set-implementation-mapping", "--project", self.project,
            "--idea", self.idea["id"], "--mapping", json.dumps(mapping),
        )
        trial = cli_json(
            "new-trial", "--project", self.project, "--idea", self.idea["id"],
            "--base-version", "VER-0001", "--template", "TPL-0001",
            "--attachment-point", "feature-output",
        )
        self.assertEqual("cv-experiment-workflow.trial.v2", trial["schema"])
        expected_mapping_digest = hashlib.sha256(
            (json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        ).hexdigest()
        self.assertEqual(1, trial.get("idea_revision"))
        self.assertEqual(
            expected_mapping_digest, trial.get("implementation_mapping_sha256"),
        )

    def test_validate_rejects_manual_current_mapping_drift(self) -> None:
        _mapping, _trial = self._create_mapped_formal_trial()
        idea_path = (
            self.project / ".experiment-workflow" / "ideas"
            / f"{self.idea['id']}.json"
        )
        idea = read_json(idea_path)
        idea["implementation_mapping"]["validation"] = "python drift.py"
        idea_path.write_text(json.dumps(idea), encoding="utf-8")

        result = run_cli("validate", "--project", self.project, check=False)

        self.assertEqual(2, result.returncode)
        self.assertIn("mapping", result.stderr)

    def test_validate_resolves_historical_mapping_after_legal_revision(self) -> None:
        _mapping, _trial = self._create_mapped_formal_trial()
        self._complete_attempt()
        revised = cli_json(
            "revise-idea", "--project", self.project, "--idea", self.idea["id"],
            "--reason", "问题边界变化", "--problem", "新问题",
            "--evidence", json.dumps([self.attempt["id"]]),
        )
        self.assertIsNone(revised["implementation_mapping"])
        self.assertEqual(0, run_cli("validate", "--project", self.project).returncode)

        idea_path = (
            self.project / ".experiment-workflow" / "ideas"
            / f"{self.idea['id']}.json"
        )
        revised["revision_history"][0]["implementation_mapping"]["validation"] = "drift"
        idea_path.write_text(json.dumps(revised), encoding="utf-8")
        drift = run_cli("validate", "--project", self.project, check=False)
        self.assertEqual(2, drift.returncode)
        self.assertIn("mapping", drift.stderr)

    def test_formal_trial_rejects_boolean_revision_and_integer_mapping_digest(self) -> None:
        _mapping, trial = self._create_mapped_formal_trial()
        trial_path = (
            self.project / ".experiment-workflow" / "trials"
            / trial["id"] / "trial.json"
        )
        original = trial_path.read_bytes()
        cases = (
            {"idea_revision": True},
            {"implementation_mapping_sha256": 1},
        )
        for update in cases:
            with self.subTest(update=update):
                payload = json.loads(original)
                payload.update(update)
                trial_path.write_text(json.dumps(payload), encoding="utf-8")
                result = run_cli("validate", "--project", self.project, check=False)
                self.assertEqual(2, result.returncode)
                self.assertIn("正式 Trial 记录无效", result.stderr)
                trial_path.write_bytes(original)

    def test_legacy_formal_trial_without_frozen_mapping_fields_remains_valid(self) -> None:
        _mapping, trial = self._create_mapped_formal_trial()
        trial_path = (
            self.project / ".experiment-workflow" / "trials"
            / trial["id"] / "trial.json"
        )
        legacy = read_json(trial_path)
        legacy.pop("idea_revision")
        legacy.pop("implementation_mapping_sha256")
        trial_path.write_text(json.dumps(legacy), encoding="utf-8")

        result = run_cli("validate", "--project", self.project, check=False)

        self.assertEqual(0, result.returncode, result.stderr)

    def test_formal_trial_rejects_partial_frozen_mapping_fields(self) -> None:
        _mapping, trial = self._create_mapped_formal_trial()
        trial_path = (
            self.project / ".experiment-workflow" / "trials"
            / trial["id"] / "trial.json"
        )
        original = trial_path.read_bytes()
        for missing in ("idea_revision", "implementation_mapping_sha256"):
            with self.subTest(missing=missing):
                payload = json.loads(original)
                payload.pop(missing)
                trial_path.write_text(json.dumps(payload), encoding="utf-8")
                result = run_cli("validate", "--project", self.project, check=False)
                self.assertEqual(2, result.returncode)
                self.assertIn("正式 Trial 记录无效", result.stderr)
                trial_path.write_bytes(original)

    def test_coordinated_mapping_and_digest_tamper_still_fails_template_semantics(self) -> None:
        _mapping, trial = self._create_mapped_formal_trial()
        control = self.project / ".experiment-workflow"
        idea_path = control / "ideas" / f"{self.idea['id']}.json"
        trial_path = control / "trials" / trial["id"] / "trial.json"
        idea = read_json(idea_path)
        idea["implementation_mapping"]["target_path"] = "models/other.py"
        idea_path.write_text(json.dumps(idea), encoding="utf-8")
        tampered_trial = read_json(trial_path)
        tampered_trial["implementation_mapping_sha256"] = hashlib.sha256(
            (
                json.dumps(
                    idea["implementation_mapping"], ensure_ascii=False,
                    indent=2, sort_keys=True,
                ) + "\n"
            ).encode("utf-8")
        ).hexdigest()
        trial_path.write_text(json.dumps(tampered_trial), encoding="utf-8")

        result = run_cli("validate", "--project", self.project, check=False)

        self.assertEqual(2, result.returncode)
        self.assertIn("Template", result.stderr)

    def test_validate_rejects_noncanonical_current_and_historical_mapping(self) -> None:
        mapping = self._formal_mapping()
        cli_json(
            "set-implementation-mapping", "--project", self.project,
            "--idea", self.idea["id"], "--mapping", json.dumps(mapping),
        )
        idea_path = (
            self.project / ".experiment-workflow" / "ideas"
            / f"{self.idea['id']}.json"
        )
        canonical = idea_path.read_bytes()
        for field, value in (
            ("target_path", "models\\adapter.py"),
            ("validation", " pytest -q "),
        ):
            with self.subTest(scope="current", field=field):
                payload = json.loads(canonical)
                payload["implementation_mapping"][field] = value
                idea_path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertEqual(
                    2,
                    run_cli("validate", "--project", self.project, check=False).returncode,
                )
                idea_path.write_bytes(canonical)

        self._complete_attempt()
        revised = cli_json(
            "revise-idea", "--project", self.project, "--idea", self.idea["id"],
            "--reason", "归档旧 mapping", "--problem", "新问题",
            "--evidence", json.dumps([self.attempt["id"]]),
        )
        historical = idea_path.read_bytes()
        for field, value in (
            ("target_path", "models\\adapter.py"),
            ("validation", " pytest -q "),
        ):
            with self.subTest(scope="history", field=field):
                payload = json.loads(historical)
                payload["revision_history"][0]["implementation_mapping"][field] = value
                idea_path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertEqual(
                    2,
                    run_cli("validate", "--project", self.project, check=False).returncode,
                )
                idea_path.write_bytes(historical)


class IdeaRelationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project = Path(self.temp_dir.name) / "project"
        cli_json("init", "--path", self.project, "--name", "Idea 关系")

    def _idea(self, title: str) -> dict[str, object]:
        return cli_json(
            "new-idea", "--project", self.project,
            "--title", title, "--note", "关系测试",
        )

    def test_new_related_idea_is_v2_while_plain_idea_stays_v1(self) -> None:
        parent = self._idea("父 Idea")
        related = self._idea("相关 Idea")

        child = cli_json(
            "new-idea", "--project", self.project,
            "--title", "子 Idea", "--note", "组合已有方向",
            "--parent-idea", parent["id"],
            "--related-idea", related["id"],
        )

        self.assertEqual("cv-experiment-workflow.idea.v1", parent["schema"])
        self.assertEqual("cv-experiment-workflow.idea.v2", child["schema"])
        self.assertEqual(parent["id"], child["parent_idea_id"])
        self.assertEqual([related["id"]], child["related_idea_ids"])
        self.assertEqual(1, child["revision"])
        self.assertEqual([], child["revision_history"])
        self.assertIsNone(child["implementation_mapping"])

    def test_global_validate_rejects_unreferenced_parent_cycle(self) -> None:
        first = self._idea("第一条")
        second = cli_json(
            "new-idea", "--project", self.project,
            "--title", "第二条", "--note", "继承第一条",
            "--parent-idea", first["id"],
        )
        first_path = (
            self.project / ".experiment-workflow" / "ideas"
            / f"{first['id']}.json"
        )
        tampered = read_json(first_path)
        tampered.update({
            "schema": "cv-experiment-workflow.idea.v2",
            "revision": 1,
            "revision_history": [],
            "parent_idea_id": second["id"],
            "related_idea_ids": [],
            "implementation_mapping": None,
        })
        first_path.write_text(json.dumps(tampered), encoding="utf-8")

        result = run_cli("validate", "--project", self.project, check=False)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("循环", result.stderr)

    def test_relation_creation_rejects_missing_duplicate_and_parent_overlap(self) -> None:
        parent = self._idea("父 Idea")
        ideas = self.project / ".experiment-workflow" / "ideas"
        before = sorted(path.name for path in ideas.iterdir())
        cases = (
            ("--parent-idea", "IDEA-9999"),
            ("--related-idea", parent["id"], "--related-idea", parent["id"]),
            ("--parent-idea", parent["id"], "--related-idea", parent["id"]),
            ("--related-idea", "IDEA-0002"),
        )
        for extra in cases:
            with self.subTest(extra=extra):
                result = run_cli(
                    "new-idea", "--project", self.project,
                    "--title", "非法关系", "--note", "应零写", *extra,
                    check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertEqual(before, sorted(path.name for path in ideas.iterdir()))

    def test_global_validate_rejects_unreferenced_bad_v2_types_and_missing_relation(self) -> None:
        parent = self._idea("父 Idea")
        child = cli_json(
            "new-idea", "--project", self.project,
            "--title", "子 Idea", "--note", "类型校验",
            "--parent-idea", parent["id"],
        )
        child_path = (
            self.project / ".experiment-workflow" / "ideas"
            / f"{child['id']}.json"
        )
        original = child_path.read_bytes()
        cases = (
            {"revision": True},
            {"parent_idea_id": 1},
            {"parent_idea_id": "IDEA-9999"},
            {"related_idea_ids": [child["id"]]},
        )
        for update in cases:
            with self.subTest(update=update):
                payload = json.loads(original)
                payload.update(update)
                child_path.write_text(json.dumps(payload), encoding="utf-8")
                result = run_cli("validate", "--project", self.project, check=False)
                self.assertEqual(2, result.returncode)
                child_path.write_bytes(original)

        validated = cli_json("validate", "--project", self.project)
        self.assertEqual(2, validated["ideas"])


if __name__ == "__main__":
    unittest.main()
