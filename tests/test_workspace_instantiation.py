from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.workspace import instantiate_workspace


ROOT = Path(__file__).resolve().parents[1]
CATALOG_FILE = ROOT / "config" / "directions" / "catalog.json"


class WorkspaceInstantiationTests(unittest.TestCase):
    def _prepare_system_root(self, raw: str) -> Path:
        system_root = Path(raw)
        catalog = (
            json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
            if CATALOG_FILE.is_file()
            else {
                "schema": "cvwf.direction-catalog.v1",
                "version": "DATA-DIRECTIONS-V1.0.0",
                "directions": [],
            }
        )
        catalog["version"] = "DATA-DIRECTIONS-TEST"
        catalog_file = system_root / "config" / "directions" / "catalog.json"
        catalog_file.parent.mkdir(parents=True)
        catalog_file.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return system_root

    def test_common_system_can_instantiate_one_personal_workspace(self) -> None:
        temporary_root = ROOT / ".runtime"
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="",
            dir=temporary_root,
        ) as raw:
            system_root = self._prepare_system_root(raw)

            result = instantiate_workspace(
                system_root,
                display_name="测试用户科研论文工作流",
                slug="tester",
            )

            workspace_root = system_root / "users" / "tester"
            manifest = json.loads(
                (workspace_root / "workspace.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("tester", manifest["slug"])
            self.assertEqual(
                "测试用户科研论文工作流",
                manifest["display_name"],
            )
            self.assertTrue(manifest["skill_id"].startswith("tester-cv-workflow-"))
            self.assertEqual(
                "cvwf.personal-workspace.v2",
                manifest["schema"],
            )
            self.assertEqual(
                "DATA-DIRECTIONS-TEST",
                manifest["direction_catalog_version"],
            )
            self.assertNotIn("available_directions", manifest)
            self.assertEqual(str(workspace_root), result["workspace_root"])
            self.assertEqual(
                {
                    ".runtime",
                    "deliveries",
                    "inbox",
                    "materials",
                    "repositories",
                },
                {
                    path.name
                    for path in workspace_root.iterdir()
                    if path.is_dir()
                },
            )
            self.assertEqual(
                {"REPOSITORY_INDEX.json", "SKILL.md", "workspace.json"},
                {
                    path.name
                    for path in workspace_root.iterdir()
                    if path.is_file()
                },
            )
            repository_index = json.loads(
                (workspace_root / "REPOSITORY_INDEX.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                "cvwf.repository-index.v2",
                repository_index["schema"],
            )
            self.assertEqual([], repository_index["repositories"])

    def test_transient_windows_lock_does_not_abort_workspace_creation(self) -> None:
        temporary_root = ROOT / ".runtime"
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as raw:
            system_root = self._prepare_system_root(raw)
            original_rename = Path.rename
            promotion_attempts = 0

            def flaky_rename(path: Path, target: Path) -> Path:
                nonlocal promotion_attempts
                if (
                    path.name.startswith(".tester-initializing-")
                    and Path(target).name == "tester"
                ):
                    promotion_attempts += 1
                    if promotion_attempts < 3:
                        error = PermissionError(
                            13,
                            "The process cannot access the directory",
                            str(path),
                        )
                        error.winerror = 5
                        raise error
                return original_rename(path, target)

            with mock.patch.object(Path, "rename", new=flaky_rename):
                instantiate_workspace(
                    system_root,
                    display_name="测试用户",
                    slug="tester",
                )

            self.assertEqual(3, promotion_attempts)
            self.assertTrue((system_root / "users" / "tester").is_dir())

    def test_existing_personal_workspace_is_never_overwritten(self) -> None:
        temporary_root = ROOT / ".runtime"
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="",
            dir=temporary_root,
        ) as raw:
            system_root = self._prepare_system_root(raw)
            instantiate_workspace(
                system_root,
                display_name="第一次",
                slug="tester",
            )

            with self.assertRaisesRegex(FileExistsError, "已经存在"):
                instantiate_workspace(
                    system_root,
                    display_name="第二次",
                    slug="tester",
                )

    def test_display_name_cannot_inject_generated_skill(self) -> None:
        temporary_root = ROOT / ".runtime"
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as raw:
            system_root = self._prepare_system_root(raw)
            for display_name in (
                '坏名字"\n---',
                "坏名字\n第二行",
                "x" * 65,
            ):
                with self.subTest(display_name=display_name):
                    with self.assertRaisesRegex(ValueError, "个人工作流名称"):
                        instantiate_workspace(
                            system_root,
                            display_name=display_name,
                            slug="safe-user",
                        )
            self.assertFalse(
                (system_root / "users" / "safe-user").exists()
            )


if __name__ == "__main__":
    unittest.main()
