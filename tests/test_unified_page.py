from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class UnifiedPageTests(unittest.TestCase):
    def test_page_is_generic_research_only_and_explains_full_gzsl_flow(
        self,
    ) -> None:
        content = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        for text in (
            "通用 CV 科研工作流",
            "先把科研跑通，再由你个性化",
            "六个方向结构齐全",
            "辅助接入外来 GZSL 框架",
            "只读扫描",
            "四类实验",
            "分支与 Worktree",
            "登记数据",
            "GPU Run",
            "人工确认",
            "创新晋级稳定 Framework",
            "科研交付包",
            "高级设置",
        ):
            self.assertIn(text, content)
        self.assertNotIn("旧个人实验室", content)
        self.assertNotIn("论文工作流", content)
        self.assertNotIn("导入 PaperFlow", content)
        self.assertNotIn("CPU 模式", content)

    def test_page_renders_directions_from_status_instead_of_hardcoding(self) -> None:
        content = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("s.direction_catalog.directions.map", content)
        self.assertIn('s.mode==="workspace_active"', content)
        self.assertIn("data-needs-workspace", content)
        self.assertIn("history.replaceState", content)


if __name__ == "__main__":
    unittest.main()
