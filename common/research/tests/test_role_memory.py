from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests._helpers import ROOT, cli_json, run_cli


class RoleMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        cli_json("init", "--path", self.project, "--name", "角色记忆")
        self.control = self.project / ".experiment-workflow"
        self.agents = self.control / "agents"

    def content_file(self, content: bytes, name: str = "memory.md") -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path

    def update(self, role: str, source: Path, *, check: bool = True):
        return run_cli(
            "update-role-memory", "--project", self.project,
            "--role", role, "--content-file", source, check=check,
        )

    def make_file_link(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target)
        except OSError as error:
            self.skipTest(f"当前环境不支持文件符号链接：{error}")

    def clear_agents(self) -> None:
        for path in list(self.agents.iterdir()):
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()

    def test_legal_memory_write_is_exact_atomic_and_valid(self) -> None:
        content = "# Coordinator\n\n- 已审核：训练入口固定。\n".encode("utf-8")
        result = self.update("coordinator", self.content_file(content))
        target = self.agents / "coordinator.md"
        self.assertEqual(content, target.read_bytes())
        self.assertIn('"role": "coordinator"', result.stdout)
        self.assertEqual(0, run_cli("validate", "--project", self.project).returncode)
        self.assertEqual(["coordinator.md"], sorted(path.name for path in self.agents.iterdir()))
        replacement = "# Coordinator\n\n- 已审核：替换旧记忆。\n".encode("utf-8")
        self.update("coordinator", self.content_file(replacement))
        self.assertEqual(replacement, target.read_bytes())
        self.assertEqual(["coordinator.md"], sorted(path.name for path in self.agents.iterdir()))

    def test_all_six_fixed_roles_can_be_written_and_validated(self) -> None:
        roles = (
            "coordinator", "idea-scientist", "implementer",
            "runner", "analyst", "reviewer",
        )
        for role in roles:
            self.update(role, self.content_file(f"# {role}\n".encode("utf-8")))
        self.assertEqual(
            [f"{role}.md" for role in sorted(roles)],
            sorted(path.name for path in self.agents.iterdir()),
        )
        self.assertEqual(0, run_cli("validate", "--project", self.project).returncode)

    def test_six_roles_cover_new_capabilities_without_new_identities(self) -> None:
        expected = {
            "coordinator": ("Promotion Gatekeeper（晋级门控）",),
            "idea-scientist": ("Idea Tree（创意树）", "revision", "来源"),
            "implementer": (
                "Template Architect（模板架构）", "implementation mapping", "干净模板",
            ),
            "runner": ("资源前置检查", "冻结 Attempt"),
            "analyst": (
                "implementation_status", "hypothesis_status", "实现失败", "假设不支持",
            ),
            "reviewer": ("Source Curator（来源整理/出处审核）", "promotion", "只读"),
        }
        role_assets = (
            ROOT / "skills" / "cv-experiment-workflow" / "assets" / "roles"
        )
        self.assertEqual(
            set(expected), {path.stem for path in role_assets.glob("*.md")}
        )
        for role, markers in expected.items():
            text = (role_assets / f"{role}.md").read_text(encoding="utf-8")
            for marker in markers:
                self.assertIn(marker, text, f"{role}: {marker}")

    def test_promotion_confirmation_and_reproduction_roles_are_unambiguous(self) -> None:
        role_assets = (
            ROOT / "skills" / "cv-experiment-workflow" / "assets" / "roles"
        )
        coordinator = (role_assets / "coordinator.md").read_text(encoding="utf-8")
        reviewer = (role_assets / "reviewer.md").read_text(encoding="utf-8")
        analyst = (role_assets / "analyst.md").read_text(encoding="utf-8")
        agents = (
            ROOT / "skills" / "cv-experiment-workflow" / "references" / "agents.md"
        ).read_text(encoding="utf-8")

        for marker in ("机器门槛满足", "Reviewer 审核通过", "用户授权", "缺一不可"):
            self.assertIn(marker, coordinator)
        self.assertIn("promotion 独立只读审核结论", reviewer)
        self.assertNotIn("promotion 只读门禁决定", reviewer)
        for marker in (
            "Confirmation policy（确认策略）", "cohort（同组证据）",
            "独立重复/seed 证据", "不自行发明阈值",
        ):
            self.assertIn(marker, analyst)
        self.assertIn(
            "| 复现/确认 | Coordinator + Runner + Analyst + Reviewer；"
            "需要代码适配时加 Implementer |",
            agents,
        )

    def test_oversized_content_is_zero_write_and_validate_rejects_large_memory(self) -> None:
        oversized = self.content_file(b"x" * (2 * 1024 * 1024), "oversized.md")
        result = self.update("coordinator", oversized, check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("64 KiB", result.stderr)
        self.assertEqual([], list(self.agents.iterdir()))

        (self.agents / "coordinator.md").write_bytes(b"x" * (2 * 1024 * 1024))
        validation = run_cli("validate", "--project", self.project, check=False)
        self.assertEqual(2, validation.returncode)
        self.assertIn("64 KiB", validation.stderr)

    def test_bad_utf8_or_directory_content_file_is_zero_write(self) -> None:
        bad_utf8 = self.update(
            "runner", self.content_file(b"\xff", "bad-utf8.md"), check=False,
        )
        self.assertEqual(2, bad_utf8.returncode)
        self.assertIn("UTF-8", bad_utf8.stderr)
        directory = self.root / "content-directory"
        directory.mkdir()
        bad_directory = self.update("runner", directory, check=False)
        self.assertEqual(2, bad_directory.returncode)
        self.assertIn("普通文件", bad_directory.stderr)
        self.assertEqual([], list(self.agents.iterdir()))

    def test_unknown_role_is_argparse_error_without_write(self) -> None:
        result = self.update("invented-role", self.content_file(b"ok"), check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("argument --role", result.stderr)
        self.assertIn("invalid choice", result.stderr)
        self.assertEqual([], list(self.agents.iterdir()))

    def test_target_link_and_linked_content_are_rejected_without_external_write(self) -> None:
        outside = self.root / "outside.md"
        outside.write_bytes(b"outside")
        target = self.agents / "coordinator.md"
        self.make_file_link(target, outside)
        result = self.update("coordinator", self.content_file(b"new"), check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("链接", result.stderr)
        self.assertEqual(b"outside", outside.read_bytes())
        self.assertTrue(target.is_symlink())

        target.unlink()
        linked_source = self.root / "linked-source.md"
        self.make_file_link(linked_source, outside)
        source_result = self.update("coordinator", linked_source, check=False)
        self.assertEqual(2, source_result.returncode)
        self.assertIn("普通文件", source_result.stderr)
        self.assertEqual([], list(self.agents.iterdir()))

    def test_write_requires_valid_project_snapshot(self) -> None:
        (self.control / "artifacts" / "model.ckpt").write_bytes(b"x")
        result = self.update("analyst", self.content_file("已审核".encode("utf-8")), check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("model.ckpt", result.stderr)
        self.assertFalse((self.agents / "analyst.md").exists())

    def test_validate_rejects_bad_utf8_unknown_file_directory_and_link(self) -> None:
        cases = (
            ("UTF-8", lambda: (self.agents / "runner.md").write_bytes(b"\xff")),
            ("未知", lambda: (self.agents / "notes.txt").write_text("x", encoding="utf-8")),
            ("未知", lambda: (self.agents / "nested").mkdir()),
        )
        for expected, create in cases:
            with self.subTest(expected=expected):
                self.clear_agents()
                create()
                result = run_cli("validate", "--project", self.project, check=False)
                self.assertEqual(2, result.returncode)
                self.assertIn(expected, result.stderr)

        self.clear_agents()
        outside = self.root / "outside-link.md"
        outside.write_text("x", encoding="utf-8")
        self.make_file_link(self.agents / "reviewer.md", outside)
        linked = run_cli("validate", "--project", self.project, check=False)
        self.assertEqual(2, linked.returncode)
        self.assertIn("链接", linked.stderr)


if __name__ == "__main__":
    unittest.main()
