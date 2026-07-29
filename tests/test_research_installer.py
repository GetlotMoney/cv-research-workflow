from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "tools" / "install-research-workflow.ps1"


class ResearchInstallerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = INSTALLER.read_text(encoding="utf-8")

    def test_exposes_windows_gpu_environment_parameters(self) -> None:
        self.assertIn('[string]$PythonExecutable = ""', self.script)
        self.assertIn("Resolve-ResearchPython", self.script)
        self.assertIn("Get-Command conda", self.script)
        self.assertIn("env list --json", self.script)
        self.assertIn('"dvsr_gpu"', self.script)
        self.assertIn('[string]$SkillsRoot', self.script)
        self.assertIn('$env:USERPROFILE', self.script)
        self.assertIn('[switch]$Check', self.script)
        self.assertIn('Test-Path -LiteralPath $PythonExecutable -PathType Leaf', self.script)

    def test_probes_torch_and_rejects_cpu_fallback(self) -> None:
        for expected in (
            "torch.__version__",
            "torch.cuda.is_available()",
            "torch.version.cuda",
            "torch.cuda.get_device_name(0)",
        ):
            self.assertIn(expected, self.script)
        self.assertIn("$probeCode | & $Python -B -u -", self.script)
        self.assertIn("import tomli", self.script)
        self.assertRegex(
            self.script,
            r"if\s*\(\s*-not\s+\$probe\.cuda_available\s*\)",
        )
        self.assertIn("CPU fallback is forbidden", self.script)

    def test_writes_research_only_local_environment_config(self) -> None:
        self.assertIn("config\\environment.local.json", self.script)
        self.assertIn("ConvertTo-Json", self.script)
        self.assertIn("[IO.File]::WriteAllText", self.script)
        self.assertIn("[Text.UTF8Encoding]::new($false)", self.script)
        self.assertIn('execution_policy = "gpu_only"', self.script)
        self.assertIn("cpu_fallback = $false", self.script)
        self.assertNotIn("paperflow", self.script.lower())
        self.assertIn("--editable", self.script)
        self.assertIn("$researchPackagePath", self.script)

    def test_installs_only_the_shared_skill_as_a_junction(self) -> None:
        self.assertIn(
            "common\\research\\skills\\cv-experiment-workflow",
            self.script,
        )
        self.assertIn(
            'Join-Path $SkillsRoot "cv-experiment-workflow"',
            self.script,
        )
        self.assertRegex(
            self.script,
            r"New-Item\s+-ItemType\s+Junction",
        )
        self.assertIn("Refusing to overwrite", self.script)
        self.assertIn("already points to the shared source", self.script)
        self.assertNotIn("legacy-personal-workflow", self.script.lower())

    def test_check_mode_uses_a_read_only_function(self) -> None:
        match = re.search(
            r"function Test-InstalledState\s*\{(?P<body>.*?)^\}",
            self.script,
            flags=re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(match, "installer must define Test-InstalledState")
        body = match.group("body")
        for mutating_command in ("New-Item", "Set-Content", "Remove-Item"):
            self.assertNotIn(mutating_command, body)
        self.assertRegex(
            self.script,
            r"if\s*\(\s*\$Check\s*\)\s*\{\s*Test-InstalledState",
        )

    def test_check_mode_validates_the_generated_environment_contract(self) -> None:
        for expected in (
            '$config.schema -ne "cvwf.research-environment.v2"',
            '$config.manager -ne "conda_named_environment"',
            "$config.python_version",
            '$config.status -ne "verified"',
        ):
            self.assertIn(expected, self.script)


if __name__ == "__main__":
    unittest.main()
