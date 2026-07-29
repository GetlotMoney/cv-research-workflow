from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = (
    ROOT
    / "common"
    / "research"
    / "skills"
    / "cv-experiment-workflow"
)


class DocumentationLayersTests(unittest.TestCase):
    def test_current_skill_exposes_only_framework_route(self) -> None:
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        for required in (
            "Framework",
            "复现",
            "调参",
            "消融",
            "创新",
            "GPU Run",
            "人工确认",
        ):
            self.assertIn(required, text)
        forbidden = re.compile(
            r"\bv1(?:\.\d+)*\b|\bv2(?:\.\d+)*\b|"
            r"Codebase|Module|Trial|Attempt|UI-CONSOLE|"
            r"start-task|run-task|create-domain-repo|"
            r"PaperFlow|export-paperflow|seal-paper-package",
            re.IGNORECASE,
        )
        self.assertIsNone(forbidden.search(text))

    def test_public_entry_uses_one_v1_release_name(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("科研工作流 V1.0", readme)
        self.assertNotRegex(readme, r"SYS-|UI-|DATA-|SKILL-|PACK-")
        self.assertNotRegex(readme, r"\bV2(?:\.\d+)*\b")
        architecture = (
            ROOT / "docs" / "architecture" / "RESEARCH_WORKFLOW_FINAL.html"
        ).read_text(encoding="utf-8")
        self.assertIn("科研工作流 V1.0", architecture)
        self.assertNotRegex(architecture, r"SYS-RESEARCH-|\bV2(?:\.\d+)*\b")

    def test_pending_direction_readmes_do_not_claim_runtime_support(self) -> None:
        packs = SKILL_ROOT / "assets" / "domain-packs"
        for name in (
            "cls-v1.0.0",
            "det-v1.0.0",
            "instseg-v1.0.0",
            "seg-v1.0.0",
            "sr-v1.0.0",
        ):
            with self.subTest(pack=name):
                text = (packs / name / "payload" / "README.md").read_text(
                    encoding="utf-8"
                )
                self.assertIn("尚未开放", text)
                self.assertIn("不能创建仓库或运行实验", text)
                self.assertNotIn("python ", text.lower())

    def test_history_has_one_entry_and_clear_warning(self) -> None:
        history_index = ROOT / "docs" / "HISTORY.md"
        self.assertTrue(history_index.is_file())
        self.assertIn(
            "不能作为当前操作说明",
            history_index.read_text(encoding="utf-8"),
        )
        for path in (
            ROOT / "docs" / "TECH_STACK_HISTORY.md",
            ROOT / "common" / "research" / "CHANGELOG.md",
            ROOT
            / "common"
            / "research"
            / "docs"
            / "TECH_STACK_HISTORY.md",
        ):
            with self.subTest(path=path):
                self.assertIn(
                    "不能作为当前操作说明",
                    path.read_text(encoding="utf-8"),
                )

    def test_skill_scenario_outputs_are_not_markdown_documents(self) -> None:
        scenario_root = ROOT / "common" / "research" / "tests" / "skill_scenarios"
        self.assertEqual([], sorted(scenario_root.glob("*.md")))
        self.assertGreaterEqual(len(list(scenario_root.glob("*.txt"))), 7)

    def test_current_validation_has_version_neutral_filename(self) -> None:
        validation = ROOT / "docs" / "reviews" / "CURRENT_VALIDATION.md"
        self.assertTrue(validation.is_file())
        self.assertFalse(
            (ROOT / "docs" / "reviews" / "RESEARCH_V2_VALIDATION.md").exists()
        )


if __name__ == "__main__":
    unittest.main()
