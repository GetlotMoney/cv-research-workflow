from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.service import CoreService


ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class CoreServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime = ROOT / ".runtime"
        runtime.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="", dir=runtime)
        self.addCleanup(self.temporary.cleanup)
        self.system_root = Path(self.temporary.name)
        (self.system_root / "users").mkdir()
        (self.system_root / "config").mkdir()
        self.config = {
            "schema": "cvwf.research-system-config.v2",
            "research_root": str(ROOT / "common" / "research"),
            "environment_file": str(
                ROOT / "config" / "environment.local.json"
            ),
            "directions_file": str(
                ROOT / "config" / "directions" / "catalog.json"
            ),
            "users_root": "users",
            "active_workspace_file": "config/active-workspace.json",
        }

    def _service(self, *, active: bool) -> CoreService:
        if active:
            workspace = self.system_root / "users" / "tester"
            for name in (
                "repositories",
                "deliveries",
                "inbox",
                "materials",
                ".runtime",
            ):
                (workspace / name).mkdir(parents=True, exist_ok=True)
            _write_json(
                workspace / "workspace.json",
                {
                    "schema": "cvwf.personal-workspace.v2",
                    "workspace_id": "test-workspace",
                    "slug": "tester",
                    "display_name": "测试科研实例",
                    "skill_id": "tester-cv-workflow-test",
                    "status": "active",
                    "direction_catalog_version": "DATA-DIRECTIONS-V1.0.0",
                },
            )
            _write_json(
                workspace / "REPOSITORY_INDEX.json",
                {
                    "schema": "cvwf.repository-index.v2",
                    "workspace_id": "test-workspace",
                    "repositories": [],
                },
            )
            _write_json(
                self.system_root / "config" / "active-workspace.json",
                {
                    "schema": "cvwf.active-workspace.v1",
                    "workspace_file": "users/tester/workspace.json",
                },
            )
        return CoreService(self.config, system_root=self.system_root)

    def test_template_mode_starts_without_any_personal_workspace(self) -> None:
        service = self._service(active=False)
        status = service.status()

        self.assertEqual("template_only", status["mode"])
        self.assertIsNone(status["workspace"])
        self.assertEqual([], status["repositories"])
        self.assertEqual(
            [
                "image_classification",
                "object_detection",
                "instance_segmentation",
                "semantic_segmentation",
                "super_resolution",
                "gzsl",
            ],
            [
                item["id"]
                for item in status["direction_catalog"]["directions"]
            ],
        )
        self.assertEqual(
            ["gzsl"],
            [
                item["id"]
                for item in status["direction_catalog"]["directions"]
                if item["status"] == "ready"
            ],
        )
        with self.assertRaisesRegex(ValueError, "尚未创建并激活"):
            service.create_repository("test-gzsl", "gzsl")

    def test_active_workspace_can_create_one_gzsl_repository_and_index_it(
        self,
    ) -> None:
        service = self._service(active=True)
        created = service.create_repository("test-gzsl", "gzsl")
        validation = service.validate_repository("test-gzsl")
        index = json.loads(
            (
                self.system_root
                / "users"
                / "tester"
                / "REPOSITORY_INDEX.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual("test-gzsl", created["repository"]["name"])
        self.assertEqual("gzsl-base", created["framework"]["slug"])
        self.assertEqual("pass", validation["status"])
        self.assertEqual(
            [
                {
                    "name": "test-gzsl",
                    "direction": "gzsl",
                    "path": "repositories/test-gzsl",
                    "status": "active",
                }
            ],
            index["repositories"],
        )

    def test_pending_and_unknown_directions_fail_closed(self) -> None:
        service = self._service(active=True)
        with self.assertRaisesRegex(ValueError, "尚未开放"):
            service.create_repository("test-cls", "image_classification")
        with self.assertRaisesRegex(ValueError, "未知研究方向"):
            service.create_repository("test-other", "other")

    def test_active_workspace_cannot_escape_users_root(self) -> None:
        outside = self.system_root / "outside"
        outside.mkdir()
        _write_json(
            outside / "workspace.json",
            {
                "schema": "cvwf.personal-workspace.v2",
                "workspace_id": "outside",
                "slug": "outside",
                "display_name": "越界",
                "skill_id": "outside-skill",
                "status": "active",
                "direction_catalog_version": "DATA-DIRECTIONS-V1.0.0",
            },
        )
        _write_json(
            self.system_root / "config" / "active-workspace.json",
            {
                "schema": "cvwf.active-workspace.v1",
                "workspace_file": "outside/workspace.json",
            },
        )
        with self.assertRaisesRegex(ValueError, "users"):
            CoreService(self.config, system_root=self.system_root)


if __name__ == "__main__":
    unittest.main()
