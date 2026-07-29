from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LauncherFilesTests(unittest.TestCase):
    def test_portable_system_config_has_no_personal_or_paperflow_path(self) -> None:
        config = json.loads(
            (ROOT / "config" / "system.json").read_text(encoding="utf-8")
        )
        self.assertEqual("cvwf.research-system-config.v2", config["schema"])
        self.assertEqual(
            {
                "schema",
                "research_root",
                "environment_file",
                "directions_file",
                "users_root",
                "active_workspace_file",
            },
            set(config),
        )
        for field in (
            "research_root",
            "environment_file",
            "directions_file",
            "users_root",
            "active_workspace_file",
        ):
            self.assertFalse(Path(config[field]).is_absolute())
        self.assertNotIn("legacy-personal-workflow", json.dumps(config).lower())
        self.assertNotIn("paperflow", json.dumps(config).lower())

    def test_one_click_launcher_starts_research_only(self) -> None:
        start = (ROOT / "tools" / "start-workflow.ps1").read_text(
            encoding="utf-8"
        )
        stop = (ROOT / "tools" / "stop-workflow.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("app.server", start)
        self.assertIn("18082", start)
        self.assertIn("environment.local.json", start)
        self.assertNotIn("paperflow_v2", start)
        self.assertNotIn("18765", start)
        self.assertIn("Stop-Process", stop)
        self.assertNotIn("Stop-Computer", start + stop)

    def test_personal_instances_are_local_only(self) -> None:
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("users/*", ignore)
        self.assertTrue((ROOT / "users" / ".gitkeep").is_file())
        self.assertEqual(
            [],
            [
                path
                for path in (ROOT / "users").iterdir()
                if path.name != ".gitkeep"
            ],
        )


if __name__ == "__main__":
    unittest.main()
