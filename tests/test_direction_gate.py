from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    ROOT
    / "common"
    / "research"
    / "skills"
    / "cv-experiment-workflow"
    / "scripts"
)
RW = SCRIPTS / "rw.py"


class DirectionGateTests(unittest.TestCase):
    def test_cli_lists_catalog_status_and_rejects_all_pending_packs(self) -> None:
        listed = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(RW), "list-domain-packs"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )
        self.assertEqual(0, listed.returncode, listed.stderr)
        packs = {
            item["id"]: item["availability"]
            for item in json.loads(listed.stdout)["packs"]
        }
        self.assertEqual("ready", packs.pop("gzsl"))
        self.assertEqual(
            {"cls", "det", "instseg", "seg", "sr"},
            set(packs),
        )
        self.assertEqual({"pending"}, set(packs.values()))

        runtime = ROOT / ".runtime"
        runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime) as raw:
            temporary = Path(raw)
            for pack in packs:
                destination = temporary / f"{pack}-repo"
                blocked = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-X",
                        "utf8",
                        str(RW),
                        "create-domain-repo",
                        "--project",
                        str(temporary / "unused-project"),
                        "--pack",
                        pack,
                        "--destination",
                        str(destination),
                        "--name",
                        f"{pack}-blocked",
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    timeout=30,
                )
                self.assertNotEqual(0, blocked.returncode)
                self.assertIn("尚未开放", blocked.stderr)
                self.assertFalse(destination.exists())

    def test_legacy_ui_write_action_rejects_pending_direction(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        from workflow_core.ui_service import _create_domain_repo

        runtime = ROOT / ".runtime"
        runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime) as raw:
            project = Path(raw)
            with self.assertRaisesRegex(ValueError, "尚未开放"):
                _create_domain_repo(
                    project,
                    {"direction": "cls", "name": "blocked-cls"},
                )
            self.assertFalse((project / "repositories").exists())


if __name__ == "__main__":
    unittest.main()
