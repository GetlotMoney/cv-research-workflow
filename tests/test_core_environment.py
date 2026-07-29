from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "environment.local.json"
EXAMPLE = ROOT / "config" / "environment.example.json"
WRAPPER = ROOT / "tools" / "core-env.ps1"


class CoreEnvironmentTests(unittest.TestCase):
    def test_local_environment_is_gpu_only_and_not_publishable(self) -> None:
        payload = json.loads(CONFIG.read_text(encoding="utf-8"))
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        expected_python = Path(payload["python_executable"])

        self.assertEqual("cvwf.research-environment.v2", payload["schema"])
        self.assertEqual("windows_only", payload["platform"])
        self.assertEqual("gpu_only", payload["execution_policy"])
        self.assertFalse(payload["cpu_fallback"])
        self.assertEqual("dvsr_gpu", payload["environment_name"])
        self.assertEqual("python.exe", expected_python.name.lower())
        self.assertEqual("2.11.0+cu128", payload["pytorch_version"])
        self.assertEqual("12.8", payload["cuda_runtime"])
        self.assertEqual("verified", payload["status"])
        self.assertTrue(expected_python.is_file())
        self.assertIn("config/environment.local.json", ignore)
        self.assertEqual("unconfigured", example["status"])
        self.assertNotIn("paperflow", json.dumps(payload).lower())

    def test_verify_gpu_runs_real_cuda_tensor_without_cpu_fallback(self) -> None:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(WRAPPER),
                "verify-gpu",
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual("pass", payload["status"])
        self.assertTrue(payload["cuda_available"])
        self.assertEqual("cuda:0", payload["tensor_device"])
        self.assertEqual("NVIDIA GeForce RTX 5070 Ti", payload["device_name"])
        self.assertFalse(payload["cpu_fallback"])


if __name__ == "__main__":
    unittest.main()
