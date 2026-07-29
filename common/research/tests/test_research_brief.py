from __future__ import annotations

import json
import hashlib
import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests._helpers import SCRIPTS, cli_json, read_json, run_cli


sys.path.insert(0, str(SCRIPTS))
from workflow_core import paper_package as paper_package_module  # noqa: E402
from workflow_core.locking import project_snapshot_lock  # noqa: E402
from workflow_core.paper_package import save_research_brief  # noqa: E402
from workflow_core.validation import (  # noqa: E402
    validate_boundaries_locked,
    validate_v2_state_and_catalog_locked,
)


def research_brief_manifest() -> dict[str, object]:
    return {
        "title": "少样本开放词汇识别",
        "research_area": "计算机视觉",
        "background": "现有方法依赖固定类别集合。",
        "problem": "新增类别样本很少时，模型泛化不稳定。",
        "objective": "验证来源感知的原型校准能否提高新类识别率。",
        "research_questions": [
            "来源感知校准是否稳定提高新类准确率？",
        ],
        "hypotheses": [
            {
                "hypothesis_id": "H-01",
                "statement": "来源感知校准能提高新类准确率。",
                "falsification_criteria": "三个随机种子的平均提升不超过 0.5 个百分点。",
            },
        ],
        "scope": {
            "included": ["小规模公开数据集上的离线实验"],
            "excluded": ["在线持续学习"],
        },
        "terminology": [
            {
                "term_en": "prototype calibration",
                "term_zh": "原型校准",
                "definition_zh": "调整类别原型以减少少样本偏差。",
            },
        ],
        "planned_contributions": [
            {
                "contribution_id": "C-01",
                "statement_zh": "提出一种来源感知的原型校准方法。",
                "provenance": "original",
                "source_refs": [],
            },
        ],
        "user_confirmation": {
            "confirmed": True,
            "confirmed_at": "2026-07-25T09:30:00+08:00",
            "confirmed_by": "demo-user",
        },
    }


class ResearchBriefTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "research-project"
        run_cli(
            "init",
            "--path",
            self.project,
            "--name",
            "research-project",
            "--layout",
            "v2",
        )
        self.control = self.project / ".experiment-workflow"
        self.manifest = self.root / "research-brief.json"
        self.manifest.write_text(
            json.dumps(research_brief_manifest(), ensure_ascii=False),
            encoding="utf-8",
        )

    def test_save_research_brief_creates_immutable_record_and_status_summary(
        self,
    ) -> None:
        saved = cli_json(
            "save-research-brief",
            "--project",
            self.project,
            "--manifest",
            self.manifest,
        )

        self.assertEqual("cv-experiment-workflow.research-brief.v1", saved["schema"])
        self.assertEqual("BRIEF-0001", saved["brief_id"])
        self.assertEqual(1, saved["revision"])
        self.assertEqual(
            read_json(self.control / "project.json")["project_id"],
            saved["project_id"],
        )
        brief_path = (
            self.control
            / "paper-packages"
            / "briefs"
            / "BRIEF-0001.json"
        )
        self.assertEqual(saved, read_json(brief_path))

        status = cli_json("workflow-status", "--project", self.project)
        self.assertEqual(
            {
                "count": 1,
                "latest": {
                    "brief_id": "BRIEF-0001",
                    "revision": 1,
                    "title": "少样本开放词汇识别",
                    "created_at": saved["created_at"],
                },
            },
            status["research_briefs"],
        )

    def test_legacy_v2_project_without_paper_packages_remains_valid(self) -> None:
        self.assertFalse((self.control / "paper-packages").exists())

        validation = cli_json("validate", "--project", self.project)
        status = cli_json("workflow-status", "--project", self.project)

        self.assertEqual(
            {"sources": 0, "ideas": 0, "templates": 0, "modules": 0},
            validation,
        )
        self.assertEqual(
            {"count": 0, "latest": None},
            status["research_briefs"],
        )
        self.assertFalse((self.control / "paper-packages").exists())

    def test_successive_saves_increment_id_and_revision_without_overwrite(
        self,
    ) -> None:
        first = cli_json(
            "save-research-brief",
            "--project",
            self.project,
            "--manifest",
            self.manifest,
        )
        first_path = (
            self.control / "paper-packages" / "briefs" / "BRIEF-0001.json"
        )
        first_bytes = first_path.read_bytes()
        revised = research_brief_manifest()
        revised["title"] = "少样本开放词汇识别：来源感知校准"
        self.manifest.write_text(
            json.dumps(revised, ensure_ascii=False),
            encoding="utf-8",
        )

        second = cli_json(
            "save-research-brief",
            "--project",
            self.project,
            "--manifest",
            self.manifest,
        )

        self.assertEqual(("BRIEF-0001", 1), (first["brief_id"], first["revision"]))
        self.assertEqual(("BRIEF-0002", 2), (second["brief_id"], second["revision"]))
        self.assertEqual(first_bytes, first_path.read_bytes())
        status = cli_json("workflow-status", "--project", self.project)
        self.assertEqual(2, status["research_briefs"]["count"])
        self.assertEqual(
            "BRIEF-0002",
            status["research_briefs"]["latest"]["brief_id"],
        )

    def test_brief_does_not_change_existing_validation_return_shapes(
        self,
    ) -> None:
        cli_json(
            "save-research-brief",
            "--project",
            self.project,
            "--manifest",
            self.manifest,
        )

        with project_snapshot_lock(self.project):
            snapshot = validate_v2_state_and_catalog_locked(self.control)
            counts = validate_boundaries_locked(self.control)

        self.assertEqual(5, len(snapshot))
        self.assertEqual(
            {"sources": 0, "ideas": 0, "templates": 0, "modules": 0},
            counts,
        )

    def test_non_original_contribution_requires_an_existing_source(self) -> None:
        manifest = research_brief_manifest()
        contribution = manifest["planned_contributions"][0]
        contribution["provenance"] = "paper_inspired"
        contribution["source_refs"] = []
        self.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )

        missing_ref = run_cli(
            "save-research-brief",
            "--project",
            self.project,
            "--manifest",
            self.manifest,
            check=False,
        )
        self.assertEqual(2, missing_ref.returncode)
        self.assertIn("至少需要一个 source_ref", missing_ref.stderr)

        contribution["source_refs"] = ["SRC-0001"]
        self.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        unknown_ref = run_cli(
            "save-research-brief",
            "--project",
            self.project,
            "--manifest",
            self.manifest,
            check=False,
        )
        self.assertEqual(2, unknown_ref.returncode)
        self.assertIn("Source 不存在", unknown_ref.stderr)

        paper = self.root / "source-paper.pdf"
        paper.write_bytes(b"paper fixture")
        source_manifest = self.root / "source.json"
        source_manifest.write_text(
            json.dumps(
                {
                    "schema": "cv-experiment-workflow.source.v2",
                    "kind": "paper",
                    "identity": "paper-fixture",
                    "locator": str(paper.resolve()),
                    "revision": "v1",
                    "commit": None,
                    "digest": (
                        "sha256:" + hashlib.sha256(paper.read_bytes()).hexdigest()
                    ),
                    "license": "test-only",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        source = cli_json(
            "register-source",
            "--project",
            self.project,
            "--manifest",
            source_manifest,
            "--source-path",
            paper,
        )
        self.assertEqual("SRC-0001", source["id"])

        saved = cli_json(
            "save-research-brief",
            "--project",
            self.project,
            "--manifest",
            self.manifest,
        )
        self.assertEqual(
            ["SRC-0001"],
            saved["planned_contributions"][0]["source_refs"],
        )

    def test_strict_validation_rejects_unknown_package_entry(self) -> None:
        cli_json(
            "save-research-brief",
            "--project",
            self.project,
            "--manifest",
            self.manifest,
        )
        unknown = self.control / "paper-packages" / "unexpected.json"
        unknown.write_text("{}", encoding="utf-8")

        result = run_cli(
            "validate",
            "--project",
            self.project,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("paper-packages 目录存在未知条目", result.stderr)

    def test_strict_validation_rejects_an_empty_brief_ledger(self) -> None:
        briefs = self.control / "paper-packages" / "briefs"
        briefs.mkdir(parents=True)

        result = run_cli(
            "validate",
            "--project",
            self.project,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("Research Brief 目录不得为空", result.stderr)

    def test_manifest_confirmation_and_fields_are_strict(self) -> None:
        cases = (
            ("not confirmed", ("user_confirmation", "confirmed"), False),
            ("bad RFC3339", ("user_confirmation", "confirmed_at"), "2026-07-25"),
            (
                "bad RFC3339 offset minutes",
                ("user_confirmation", "confirmed_at"),
                "2026-07-25T09:30:00+00:60",
            ),
            ("empty terminology", ("terminology",), []),
            ("unknown field", ("extra",), "no"),
        )
        for label, path, value in cases:
            with self.subTest(label=label):
                manifest = research_brief_manifest()
                if len(path) == 1:
                    manifest[path[0]] = value
                else:
                    manifest[path[0]][path[1]] = value
                self.manifest.write_text(
                    json.dumps(manifest, ensure_ascii=False),
                    encoding="utf-8",
                )
                result = run_cli(
                    "save-research-brief",
                    "--project",
                    self.project,
                    "--manifest",
                    self.manifest,
                    check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertFalse((self.control / "paper-packages").exists())

    def test_first_save_interrupt_cleans_staging_and_keeps_project_valid(
        self,
    ) -> None:
        with mock.patch(
            "workflow_core.paper_package.atomic_create_json",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                save_research_brief(self.project, self.manifest)

        self.assertFalse((self.control / "paper-packages").exists())
        self.assertEqual(
            [],
            list(
                self.project.glob(
                    ".experiment-workflow.paper-packages-*.staging"
                )
            ),
        )
        self.assertEqual(
            {"sources": 0, "ideas": 0, "templates": 0, "modules": 0},
            cli_json("validate", "--project", self.project),
        )

    def test_initial_staging_identity_interrupt_cleans_empty_directory(
        self,
    ) -> None:
        original_lstat = Path.lstat

        def interrupt_staging_identity(path: Path) -> object:
            if (
                path.parent == self.project
                and path.name.startswith(
                    paper_package_module.INITIAL_STAGING_PREFIX
                )
                and path.name.endswith(
                    paper_package_module.INITIAL_STAGING_SUFFIX
                )
            ):
                raise KeyboardInterrupt
            return original_lstat(path)

        with mock.patch.object(
            Path,
            "lstat",
            autospec=True,
            side_effect=interrupt_staging_identity,
        ):
            with self.assertRaises(KeyboardInterrupt):
                save_research_brief(self.project, self.manifest)

        self.assertEqual(
            [],
            list(
                self.project.glob(
                    ".experiment-workflow.paper-packages-*.staging"
                )
            ),
        )
        self.assertFalse((self.control / "paper-packages").exists())

    def test_initial_publish_competition_rejects_without_overwrite(self) -> None:
        original_publish = paper_package_module._publish_directory_no_replace
        competitor = self.control / "paper-packages"

        def create_competitor_then_publish(
            source: object,
            destination: object,
        ) -> None:
            competitor.mkdir()
            original_publish(source, destination)

        with mock.patch(
            "workflow_core.paper_package._publish_directory_no_replace",
            side_effect=create_competitor_then_publish,
        ):
            with self.assertRaises(OSError):
                save_research_brief(self.project, self.manifest)

        self.assertTrue(competitor.is_dir())
        self.assertEqual([], list(competitor.iterdir()))
        self.assertEqual(
            [],
            list(
                self.project.glob(
                    ".experiment-workflow.paper-packages-*.staging"
                )
            ),
        )

    def test_directory_publish_primitive_never_replaces_empty_target(
        self,
    ) -> None:
        source = self.root / "staged-paper-packages"
        destination = self.root / "existing-paper-packages"
        source.mkdir()
        (source / "briefs").mkdir()
        destination.mkdir()

        with self.assertRaises(OSError):
            paper_package_module._publish_directory_no_replace(
                source,
                destination,
            )

        self.assertTrue(source.is_dir())
        self.assertTrue((source / "briefs").is_dir())
        self.assertTrue(destination.is_dir())
        self.assertEqual([], list(destination.iterdir()))

    def test_unsupported_posix_publish_fails_closed(self) -> None:
        source = self.root / "staged-posix-package"
        destination = self.root / "published-posix-package"
        source.mkdir()

        with (
            mock.patch.object(paper_package_module.os, "name", "posix"),
            mock.patch.object(paper_package_module.sys, "platform", "other"),
        ):
            with self.assertRaisesRegex(OSError, "不支持"):
                paper_package_module._publish_directory_no_replace(
                    source,
                    destination,
                )

        self.assertTrue(source.is_dir())
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
