from __future__ import annotations

import tempfile
import unittest
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

from tests._helpers import SCRIPTS, cli_json, read_json, run_cli

sys.path.insert(0, str(SCRIPTS))
from workflow_core.project_skills import install_project_skill
from workflow_core import project as project_module


class ProjectSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.project = self.root / "Research-Demo-Lab"

    def test_init_generates_project_specific_skill_and_identity(self) -> None:
        project = cli_json(
            "init",
            "--path",
            self.project,
            "--name",
            "通用 CV 实验项目",
            "--layout",
            "v2",
        )

        self.assertEqual(
            "cv-experiment-workflow.project-skill.v1",
            project["project_skill"]["schema"],
        )
        self.assertEqual("SKILL.md", project["project_skill"]["entrypoint"])
        self.assertRegex(
            project["project_skill"]["skill_id"],
            r"^research-demo-lab-[0-9a-f]{8}$",
        )
        self.assertEqual(
            project,
            read_json(self.project / ".experiment-workflow" / "project.json"),
        )

        skill_path = self.project / "SKILL.md"
        skill = skill_path.read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\n"))
        self.assertIn(
            f"name: {project['project_skill']['skill_id']}",
            skill,
        )
        self.assertIn("Use when", skill)
        self.assertIn(project["project_id"], skill)
        self.assertIn(str(self.project.resolve()), skill)
        self.assertIn("cv-experiment-workflow", skill)
        self.assertIn(".experiment-workflow/repository.json", skill)
        self.assertIn(".experiment-workflow/frameworks/*/framework.json", skill)
        self.assertFalse((self.project / "REPOSITORY_INDEX.json").exists())
        self.assertIn("结构化记录只通过工作流命令写入", skill)
        self.assertIn("只有创新绑定 Idea", skill)

    def test_init_refuses_foreign_root_skill_without_partial_project(self) -> None:
        self.project.mkdir()
        foreign = b"---\nname: user-owned\ndescription: Use when user asks.\n---\n"
        (self.project / "SKILL.md").write_bytes(foreign)

        result = run_cli(
            "init",
            "--path",
            self.project,
            "--name",
            "通用 CV 实验项目",
            "--layout",
            "v2",
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("SKILL.md", result.stderr)
        self.assertEqual(foreign, (self.project / "SKILL.md").read_bytes())
        self.assertFalse((self.project / ".experiment-workflow").exists())

    def test_existing_project_can_initialize_its_missing_project_skill(
        self,
    ) -> None:
        project = cli_json(
            "init",
            "--path",
            self.project,
            "--name",
            "通用 CV 实验项目",
            "--layout",
            "v2",
        )
        skill_path = self.project / "SKILL.md"
        skill_path.unlink()
        project.pop("project_skill")
        project_path = self.project / ".experiment-workflow" / "project.json"
        project_path.write_text(
            json.dumps(project, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = cli_json(
            "init-project-skill",
            "--project",
            self.project,
        )

        self.assertEqual("created", result["status"])
        self.assertRegex(
            result["skill_id"],
            r"^research-demo-lab-[0-9a-f]{8}$",
        )
        self.assertTrue(skill_path.is_file())
        updated = read_json(project_path)
        self.assertEqual(result["skill_id"], updated["project_skill"]["skill_id"])

    def test_validate_requires_the_bound_project_skill(self) -> None:
        cli_json(
            "init",
            "--path",
            self.project,
            "--name",
            "通用 CV 实验项目",
            "--layout",
            "v2",
        )
        (self.project / "SKILL.md").unlink()

        result = run_cli("validate", "--project", self.project, check=False)

        self.assertEqual(2, result.returncode)
        self.assertIn("项目专属 Skill", result.stderr)

    def test_init_project_skill_refuses_incomplete_fake_project(self) -> None:
        control = self.project / ".experiment-workflow"
        control.mkdir(parents=True)
        project_path = control / "project.json"
        fake = {
            "schema": "cv-experiment-workflow.project.v2",
            "project_id": str(uuid.uuid4()),
            "name": "伪项目",
        }
        project_path.write_text(
            json.dumps(fake, ensure_ascii=False),
            encoding="utf-8",
        )

        result = run_cli(
            "init-project-skill",
            "--project",
            self.project,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertFalse((self.project / "SKILL.md").exists())
        self.assertEqual(fake, read_json(project_path))

    def test_project_name_cannot_inject_skill_markdown(self) -> None:
        result = run_cli(
            "init",
            "--path",
            self.project,
            "--name",
            "合法标题\n## 外来指令",
            "--layout",
            "v2",
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertFalse((self.project / "SKILL.md").exists())
        self.assertFalse((self.project / ".experiment-workflow").exists())

    def test_project_name_cannot_create_an_unreadable_oversized_skill(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "项目名称无效"):
            project_module.init_project(
                self.project,
                "a" * 40_000,
                layout="v2",
            )

        self.assertFalse((self.project / "SKILL.md").exists())
        self.assertFalse((self.project / ".experiment-workflow").exists())

    def test_init_rolls_back_if_skill_is_occupied_after_preflight(self) -> None:
        original_create = project_module.atomic_create_bytes

        def occupy_skill(path: Path, content: bytes, **kwargs: object) -> bool:
            if path.name == "SKILL.md":
                path.write_bytes(b"foreign")
                return False
            return original_create(path, content, **kwargs)

        with patch(
            "workflow_core.project.atomic_create_bytes",
            side_effect=occupy_skill,
        ):
            with self.assertRaisesRegex(FileExistsError, "被其他内容占用"):
                project_module.init_project(
                    self.project,
                    "通用 CV 实验项目",
                    layout="v2",
                )

        self.assertEqual(b"foreign", (self.project / "SKILL.md").read_bytes())
        self.assertFalse((self.project / ".experiment-workflow").exists())

    def test_trusted_legacy_v2_can_upgrade_then_initialize_skill(self) -> None:
        project = cli_json(
            "init",
            "--path",
            self.project,
            "--name",
            "通用 CV 实验项目",
            "--layout",
            "v2",
        )
        (self.project / "SKILL.md").unlink()
        project.pop("project_skill")
        project_path = self.project / ".experiment-workflow" / "project.json"
        project_path.write_text(
            json.dumps(project, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        old_lock = {
            "schema": "cv-experiment-workflow.workflow-lock.v2",
            "skill_id": "cv-experiment-workflow",
            "release_version": "1.1.2",
            "system_version": "SYS-V2.9.2",
            "payload": {
                "algorithm": "sha256-path-bytes-v1",
                "file_count": 55,
                "digest": (
                    "sha256:"
                    "200971d3078c27e82228a8bfcc62730105134a79a38264cc"
                    "ee5be616cf48140a"
                ),
            },
        }
        (
            self.project / ".experiment-workflow" / "workflow.lock.json"
        ).write_text(
            json.dumps(old_lock, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        upgraded = cli_json(
            "upgrade-workflow-lock",
            "--project",
            self.project,
            "--source-skill",
            SCRIPTS.parent,
        )
        initialized = cli_json(
            "init-project-skill",
            "--project",
            self.project,
        )

        self.assertEqual("upgraded", upgraded["status"])
        self.assertEqual("created", initialized["status"])
        cli_json("validate", "--project", self.project)

    def test_same_directory_name_uses_project_id_to_avoid_collisions(self) -> None:
        first = self.root / "first" / "same-name"
        second = self.root / "second" / "same-name"
        first_project = cli_json(
            "init", "--path", first, "--name", "项目一", "--layout", "v2"
        )
        second_project = cli_json(
            "init", "--path", second, "--name", "项目二", "--layout", "v2"
        )

        first_id = first_project["project_skill"]["skill_id"]
        second_id = second_project["project_skill"]["skill_id"]
        self.assertNotEqual(first_id, second_id)
        self.assertTrue(first_id.startswith("same-name-"))
        self.assertTrue(second_id.startswith("same-name-"))

    def test_moved_project_can_rebind_and_safely_update_installed_copy(
        self,
    ) -> None:
        project = cli_json(
            "init",
            "--path",
            self.project,
            "--name",
            "通用 CV 实验项目",
            "--layout",
            "v2",
        )
        skill_id = project["project_skill"]["skill_id"]
        skills_root = self.root / "installed-skills"
        cli_json(
            "install-project-skill",
            "--project",
            self.project,
            "--skills-root",
            skills_root,
        )
        moved = self.root / "moved" / "Research-Demo-Lab"
        moved.parent.mkdir()
        self.project.rename(moved)

        invalid = run_cli("validate", "--project", moved, check=False)
        self.assertEqual(2, invalid.returncode)
        rebound = cli_json("rebind-project-skill", "--project", moved)
        self.assertEqual("rebound", rebound["status"])
        self.assertIn(str(moved.resolve()), (moved / "SKILL.md").read_text("utf-8"))

        synced = cli_json(
            "install-project-skill",
            "--project",
            moved,
            "--skills-root",
            skills_root,
        )
        self.assertEqual("updated", synced["status"])
        self.assertEqual(
            (moved / "SKILL.md").read_bytes(),
            (skills_root / skill_id / "SKILL.md").read_bytes(),
        )
        cli_json("validate", "--project", moved)

    def test_install_project_skill_is_idempotent_and_refuses_conflicts(
        self,
    ) -> None:
        project = cli_json(
            "init",
            "--path",
            self.project,
            "--name",
            "通用 CV 实验项目",
            "--layout",
            "v2",
        )
        skills_root = self.root / "installed-skills"

        first = cli_json(
            "install-project-skill",
            "--project",
            self.project,
            "--skills-root",
            skills_root,
        )
        self.assertEqual("installed", first["status"])
        self.assertEqual(project["project_skill"]["skill_id"], first["skill_id"])
        skill_id = project["project_skill"]["skill_id"]
        installed = skills_root / skill_id / "SKILL.md"
        self.assertEqual(
            (self.project / "SKILL.md").read_bytes(),
            installed.read_bytes(),
        )

        second = cli_json(
            "install-project-skill",
            "--project",
            self.project,
            "--skills-root",
            skills_root,
        )
        self.assertEqual("already_installed", second["status"])
        manifest = installed.parent / ".cv-experiment-workflow-project-skill.json"
        self.assertTrue(manifest.is_file())

        installed.write_text("user-owned\n", encoding="utf-8")
        conflict = run_cli(
            "install-project-skill",
            "--project",
            self.project,
            "--skills-root",
            skills_root,
            check=False,
        )
        self.assertEqual(2, conflict.returncode)
        self.assertIn("拒绝覆盖", conflict.stderr)
        self.assertEqual("user-owned\n", installed.read_text(encoding="utf-8"))

    def test_install_rechecks_atomic_create_race(self) -> None:
        project = cli_json(
            "init",
            "--path",
            self.project,
            "--name",
            "通用 CV 实验项目",
            "--layout",
            "v2",
        )
        skills_root = self.root / "installed-skills"

        def occupy(path: Path, _content: bytes, **_kwargs: object) -> bool:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"foreign")
            return False

        with patch(
            "workflow_core.project_skills.atomic_create_bytes_clean",
            side_effect=occupy,
        ):
            with self.assertRaisesRegex(ValueError, "创建竞争"):
                install_project_skill(self.project, skills_root)

        destination = (
            skills_root / project["project_skill"]["skill_id"] / "SKILL.md"
        )
        self.assertEqual(b"foreign", destination.read_bytes())

    def test_install_refuses_symlinked_skills_root(self) -> None:
        cli_json(
            "init",
            "--path",
            self.project,
            "--name",
            "通用 CV 实验项目",
            "--layout",
            "v2",
        )
        real = self.root / "real-skills"
        real.mkdir()
        link = self.root / "skills-link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"当前系统不能创建目录链接：{error}")

        result = run_cli(
            "install-project-skill",
            "--project",
            self.project,
            "--skills-root",
            link,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("链接或 reparse", result.stderr)
        self.assertEqual([], list(real.iterdir()))


if __name__ == "__main__":
    unittest.main()
