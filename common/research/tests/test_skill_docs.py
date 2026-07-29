from __future__ import annotations

import re
import unittest
from pathlib import Path


RESEARCH_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_ROOT = RESEARCH_ROOT.parents[1]
SKILL = RESEARCH_ROOT / "skills" / "cv-experiment-workflow" / "SKILL.md"
README = RESEARCH_ROOT / "README.md"
ROOT_README = SYSTEM_ROOT / "README.md"
USAGE = SYSTEM_ROOT / "docs" / "USAGE.md"
TECH_HISTORY = SYSTEM_ROOT / "docs" / "TECH_STACK_HISTORY.md"
VALIDATION = SYSTEM_ROOT / "docs" / "reviews" / "RESEARCH_V2_VALIDATION.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class SkillDocumentationTests(unittest.TestCase):
    def test_required_current_documents_exist(self) -> None:
        for path in (SKILL, README, ROOT_README, USAGE, TECH_HISTORY, VALIDATION):
            self.assertTrue(path.is_file(), path)

    def test_skill_frontmatter_is_valid_and_generic(self) -> None:
        text = read(SKILL)
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: cv-experiment-workflow", text)
        self.assertIn("description:", text)
        self.assertNotRegex(
            text,
            re.compile(r"legacy-personal-workflow|旧个人实验室", re.IGNORECASE),
        )

    def test_skill_documents_the_user_facing_framework_commands(self) -> None:
        text = read(SKILL)
        for command in (
            "create-gzsl-repository",
            "import-standardized-gzsl-framework",
            "create-framework-idea",
            "create-framework-experiment",
            "prepare-framework-worktree",
            "register-gzsl-dataset",
            "run-framework-experiment",
            "confirm-framework-run",
            "export-framework-paper-package",
            "promote-framework",
            "validate-framework-workspace",
        ):
            self.assertIn(command, text)

    def test_template_mode_does_not_create_a_personal_workspace(self) -> None:
        combined = "\n".join((read(SKILL), read(ROOT_README), read(USAGE)))
        self.assertIn("没有激活个人实例时只检查通用模板", combined)
        self.assertIn("不会偷偷创建用户目录", combined)
        self.assertIn("用户决定后再实例化", combined)

    def test_six_directions_share_one_catalog_and_only_gzsl_is_ready(self) -> None:
        combined = "\n".join((read(SKILL), read(ROOT_README), read(USAGE)))
        self.assertIn("config/directions/catalog.json", combined)
        for name in ("图像分类", "目标检测", "实例分割", "语义分割", "超分辨率", "GZSL"):
            self.assertIn(name, combined)
        self.assertIn("当前只有 GZSL 是 `ready`", combined)

    def test_four_routes_and_route_contracts_are_explained(self) -> None:
        combined = "\n".join((read(SKILL), read(USAGE)))
        for route in ("复现", "调参", "消融", "创新"):
            self.assertIn(route, combined)
        for contract in ("允许误差", "允许改变哪些参数", "关闭哪个模块", "最低提升"):
            self.assertIn(contract, combined)
        self.assertIn("--route-contract", combined)

    def test_run_identity_freezes_config_and_seed(self) -> None:
        combined = "\n".join((read(ROOT_README), read(USAGE)))
        self.assertIn("同一份配置与随机种子", combined)
        self.assertIn("系统会拒绝中途改配置", combined)
        self.assertIn("实验编号在整个仓库中唯一", combined)

    def test_dataset_is_external_and_license_bound(self) -> None:
        combined = "\n".join((read(SKILL), read(ROOT_README), read(USAGE)))
        for token in ("原始 NPZ 不进入 Git", "许可证", "SHA-256", "1 GiB"):
            self.assertIn(token, combined)
        self.assertIn("不会写入本机绝对路径", combined)

    def test_external_code_warning_is_explicit(self) -> None:
        combined = "\n".join((read(SKILL), read(ROOT_README), read(USAGE)))
        self.assertIn("不是操作系统沙箱", combined)
        self.assertIn("人工审读", combined)
        self.assertIn("--i-trust-this-code", combined)
        self.assertIn("两名", combined)
        self.assertIn("输出文件哈希", combined)
        self.assertIn("手写两份相同", combined)

    def test_innovation_confirmation_and_promotion_are_separate(self) -> None:
        combined = "\n".join((read(SKILL), read(USAGE)))
        self.assertIn("--innovation-outcome accepted\\|rejected", combined)
        self.assertIn("明确选择“接受”或“拒绝”", combined)
        self.assertIn("才能晋级成稳定子 Framework", combined)

    def test_research_and_paperflow_have_an_explicit_boundary(self) -> None:
        combined = "\n".join((read(SKILL), read(ROOT_README), read(USAGE)))
        self.assertIn("科研不会自动触发论文写作", combined)
        self.assertIn("用户主动生成", combined)
        self.assertIn("PaperFlow 不直接读取正在变化的实验仓库", combined)

    def test_current_versions_are_recorded_by_object(self) -> None:
        history = read(TECH_HISTORY)
        for version in (
            "SYS-RESEARCH-V2.0.2",
            "UI-RESEARCH-V2.0.2",
            "DATA-RESEARCH-V2.0.2",
            "SKILL-RESEARCH-V2.0.2",
            "PACK-GZSL-V1.1.1",
        ):
            self.assertIn(version, history)


if __name__ == "__main__":
    unittest.main()
