from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._helpers import ROOT


class UnifiedLauncherContractTests(unittest.TestCase):
    @property
    def script_path(self) -> Path:
        return ROOT / "tools" / "start_unified_workflow.ps1"

    def test_launcher_only_starts_candidate_servers_and_tracks_its_own_pids(self) -> None:
        script = self.script_path.read_text(encoding="utf-8-sig")
        self.assertIn("console_server.py", script)
        self.assertIn("check_unified_preflight.py", script)
        self.assertIn("cv-unified-preflight.v1", script)
        self.assertIn("SYS-V2.13.0", script)
        self.assertIn("bound_evidence_v15", script)
        self.assertIn("paperflow_v2", script)
        self.assertIn('"start-workflow"', script)
        self.assertIn('"--no-browser"', script)
        self.assertIn("/api/bootstrap", script)
        self.assertIn("X-PaperFlow-Token", script)
        self.assertIn("--paperflow-entrypoint", script)
        self.assertIn("#token=", script)
        self.assertIn("Start-Process", script)
        self.assertIn("owned-pids", script)
        self.assertIn("127.0.0.1", script)
        self.assertIn("Resolve-Path -LiteralPath $Project", script)
        self.assertIn("Resolve-Path -LiteralPath $PaperFlowRoot", script)
        self.assertIn("ConvertTo-ProcessArgument", script)
        self.assertIn("Get-Process -Id $child.Id", script)
        self.assertIn("StartTimeUtc", script)
        self.assertIn("$process.StartTime.ToUniversalTime().Ticks -eq $child.StartTimeUtc", script)
        self.assertIn("Stop-Process -Id $child.Id", script)
        self.assertIn("if ($paperFlow.HasExited)", script)
        self.assertIn("if ($console.HasExited)", script)
        self.assertIn("Get-Content -LiteralPath $paperFlowStdout -Raw -Encoding UTF8", script)
        self.assertIn("config\\environment.json", script)
        self.assertIn("paperflow.python_executable", script)
        self.assertIn("$paperFlowPythonCommand", script)
        self.assertIn(
            "Start-Process -FilePath $paperFlowPythonCommand",
            script,
        )
        self.assertIn('-Contract "console-state"', script)
        self.assertIn('$payload.snapshot.status -eq "valid"', script)
        self.assertIn("$HealthCheckOnly", script)
        self.assertIn("$NoBrowser", script)
        self.assertNotIn("Invoke-Expression", script)
        self.assertNotIn("pip install", script.lower())
        self.assertNotIn("git push", script.lower())
        self.assertNotIn("Stop-Computer", script)
        self.assertNotIn("Restart-Computer", script)
        self.assertNotIn("shutdown", script.lower())

    @unittest.skipUnless(os.name == "nt", "本测试验证 Windows PowerShell 5.1")
    def test_launcher_is_utf8_bom_and_real_windows_powershell_can_parse_it(
        self,
    ) -> None:
        self.assertTrue(
            self.script_path.read_bytes().startswith(b"\xef\xbb\xbf"),
            "Windows PowerShell 5.1 需要 UTF-8 BOM 才能稳定解析中文脚本",
        )
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        escaped = str(self.script_path).replace("'", "''")
        command = (
            "$tokens=$null;$errors=$null;"
            f"[System.Management.Automation.Language.Parser]::ParseFile('{escaped}',"
            "[ref]$tokens,[ref]$errors)|Out-Null;"
            "if($errors.Count){$errors|ForEach-Object{$_.ToString()};exit 1}"
        )
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_empty_paperflow_stdout_is_treated_as_not_ready(self) -> None:
        script = self.script_path.read_text(encoding="utf-8-sig")

        guard = 'if ($null -ne $text)'
        match = '$match = $pattern.Match([string]$text)'
        self.assertIn(guard, script)
        self.assertIn(match, script)
        self.assertLess(script.index(guard), script.index(match))

    @unittest.skipUnless(os.name == "nt", "本测试验证 Windows PowerShell 5.1")
    def test_utf8_paperflow_entrypoint_survives_a_chinese_windows_code_page(
        self,
    ) -> None:
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "paperflow.stdout"
            output.write_text(
                "敏感：请只在本机打开一次此地址："
                "http://127.0.0.1:8766/#token=abcdefghijklmnop\n",
                encoding="utf-8",
            )
            escaped = str(output).replace("'", "''")
            command = (
                f"$text=Get-Content -LiteralPath '{escaped}' -Raw -Encoding UTF8;"
                "$pattern=[regex]'http://127\\.0\\.0\\.1:"
                "(?<port>[0-9]{1,5})/#token=(?<token>[A-Za-z0-9_-]{16,256})';"
                "if(-not $pattern.Match([string]$text).Success){exit 1}"
            )
            result = subprocess.run(
                [
                    str(powershell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    @unittest.skipUnless(os.name == "nt", "启动器只面向当前 Windows 候选")
    def test_plain_directory_fails_before_creating_paperflow_workspace(self) -> None:
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ordinary = root / "ordinary"
            ordinary.mkdir()
            paperflow = root / "paperflow"
            (paperflow / "paperflow_v2").mkdir(parents=True)
            (paperflow / "paperflow_v2" / "__main__.py").write_text(
                "",
                encoding="utf-8",
            )
            library = root / "library"
            library.mkdir()
            result = subprocess.run(
                [
                    str(powershell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(self.script_path),
                    "-Project",
                    str(ordinary),
                    "-PaperFlowRoot",
                    str(paperflow),
                    "-LibraryRoot",
                    str(library),
                    "-ConsolePort",
                    "18761",
                    "-PaperFlowPort",
                    "18762",
                    "-NoBrowser",
                    "-HealthCheckOnly",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertEqual([], list(ordinary.iterdir()))
            self.assertFalse((ordinary / "paperflow-papers").exists())


if __name__ == "__main__":
    unittest.main()
