from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._helpers import ROOT

sys.path.insert(
    0,
    str(ROOT / "skills" / "cv-experiment-workflow" / "scripts"),
)
from workflow_core.project import init_project  # noqa: E402


def _paperflow_candidate() -> Path | None:
    value = os.environ.get("PAPERFLOW_BOUND_RECEIVER_ROOT")
    return Path(value) if value else None


def _load_tool():
    path = ROOT / "tools" / "check_unified_preflight.py"
    specification = importlib.util.spec_from_file_location(
        "check_unified_preflight",
        path,
    )
    if specification is None or specification.loader is None:
        raise AssertionError("无法加载统一入口预检工具")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class UnifiedPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "项目"
        init_project(self.project, "统一入口预检", layout="v2")
        self.library = self.root / "library"
        self.library.mkdir()

    def test_real_candidate_receiver_accepts_the_exact_current_producer(self) -> None:
        paperflow = _paperflow_candidate()
        if paperflow is None or not paperflow.is_dir():
            self.skipTest("当前主机没有本轮 PaperFlow 候选 worktree")
        report = _load_tool().check_unified_preflight(
            self.project,
            paperflow,
            self.library,
        )
        self.assertEqual("pass", report["status"])
        self.assertEqual("1.5.0", report["producer"]["release_version"])
        self.assertEqual("SYS-V2.13.0", report["producer"]["system_version"])
        self.assertEqual(
            "bound_evidence_v15",
            report["paperflow"]["receiver_profile"],
        )

    def test_plain_directory_fails_without_mutation(self) -> None:
        ordinary = self.root / "ordinary"
        ordinary.mkdir()
        before = tuple(ordinary.iterdir())
        command = [
            sys.executable,
            "-B",
            str(ROOT / "tools" / "check_unified_preflight.py"),
            "--project",
            str(ordinary),
            "--paperflow-root",
            str(self.root / "missing-paperflow"),
            "--library-root",
            str(self.library),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertEqual(before, tuple(ordinary.iterdir()))
        self.assertNotIn(str(ordinary), result.stderr)

    def test_report_is_json_and_does_not_expose_local_roots(self) -> None:
        paperflow = _paperflow_candidate()
        if paperflow is None or not paperflow.is_dir():
            self.skipTest("当前主机没有本轮 PaperFlow 候选 worktree")
        report = _load_tool().check_unified_preflight(
            self.project,
            paperflow,
            self.library,
        )
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(str(self.project), encoded)
        self.assertNotIn(str(paperflow), encoded)
        self.assertNotIn(str(self.library), encoded)


if __name__ == "__main__":
    unittest.main()
