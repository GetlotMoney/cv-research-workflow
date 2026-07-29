from __future__ import annotations

import json
import hashlib
import re
import subprocess
import unittest
from pathlib import Path


EVIDENCE = Path(__file__).parent / "skill_scenarios" / "baseline.json"
EXECUTION = Path(__file__).parent / "skill_scenarios" / "execution.json"
REPOSITORY = Path(__file__).resolve().parents[1]
INTENTIONALLY_REMOVED_PATHS = {
    "skills/cv-experiment-workflow/scripts/workflow_core/legacy.py",
    "skills/cv-experiment-workflow/scripts/workflow_core/migration.py",
    "tests/test_sample_project_legacy_view.py",
    "tests/test_sample_project_tune_pilot.py",
    "tests/test_migration.py",
}


class ScenarioEvidenceTests(unittest.TestCase):
    def load_evidence(self) -> dict:
        self.assertTrue(EVIDENCE.is_file(), "RED baseline evidence is missing")
        return json.loads(EVIDENCE.read_text(encoding="utf-8"))

    def scenario_file(self, name: str) -> Path:
        self.assertEqual(name, Path(name).name, "scenario evidence path must be a plain filename")
        path = EVIDENCE.parent / name
        self.assertTrue(path.is_file(), f"scenario evidence file is missing: {name}")
        return path

    def assert_sha256(self, path: Path, expected: str) -> None:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(expected, actual, f"stale evidence digest: {path.name}")

    def test_three_baselines_capture_real_failures(self) -> None:
        payload = self.load_evidence()
        self.assertEqual("skill-scenario.v1", payload["schema_version"])
        self.assertEqual(3, len(payload["cases"]))
        self.assertEqual(3, len({case["id"] for case in payload["cases"]}))
        self.assertEqual(3, len({case["baseline_agent_id"] for case in payload["cases"]}))
        self.assertEqual(3, len({case["baseline_output_file"] for case in payload["cases"]}))
        for case in payload["cases"]:
            self.assertTrue(case["prompt"].strip())
            output_path = self.scenario_file(case["baseline_output_file"])
            output = output_path.read_text(encoding="utf-8")
            self.assertTrue(output.strip())
            self.assertGreaterEqual(len(case["failures"]), 1)
            for failure in case["failures"]:
                self.assertIsInstance(failure, dict)
                self.assertTrue(failure["summary"].strip())
                self.assertGreaterEqual(len(failure["evidence_snippets"]), 1)
                for snippet in failure["evidence_snippets"]:
                    self.assertTrue(snippet.strip())
                    self.assertIn(snippet, output)

    def test_final_forward_results_are_sanitized_and_pass(self) -> None:
        payload = self.load_evidence()
        for case in payload["cases"]:
            self.assertTrue(case["forward_pass"])
            self.assertTrue(case["forward_agent_id"].startswith("/root/forward_"))
            self.assertRegex(case["forward_skill_commit"], r"^[0-9a-f]{40}$")
            self.assertIn(case["forward_round"], (1, 2))
            output_path = self.scenario_file(case["forward_output_file"])
            self.assert_sha256(output_path, case["forward_output_sha256"])
            output = output_path.read_text(encoding="utf-8")
            self.assertTrue(output.strip())
            self.assertIn("sanitized transcript（脱敏转录）", output)
            self.assertIn("原平台任务记录仍是最终来源", output)
            self.assertGreaterEqual(len(case["forward_pass_evidence"]), 1)
            for snippet in case["forward_pass_evidence"]:
                self.assertIn(snippet, output)

    def test_red_green_order_and_agent_isolation_are_auditable(self) -> None:
        execution = json.loads(EXECUTION.read_text(encoding="utf-8"))
        self.assertIn("sanitized transcript（脱敏转录）", execution["attestation"]["scope"])
        self.assertIn("最终来源", execution["attestation"]["scope"])
        red = execution["red_test"]
        green = execution["green_test"]
        self.assertEqual(1, red["exit_code"])
        self.assertEqual(0, green["exit_code"])
        self.assertTrue(red["observed_before_baseline_evidence"])
        red_path = self.scenario_file(red["output_file"])
        green_path = self.scenario_file(green["output_file"])
        red_output = red_path.read_text(encoding="utf-8")
        green_output = green_path.read_text(encoding="utf-8")
        self.assertIn("Exit code: 1", red_output)
        self.assertGreaterEqual(red_output.count("RED baseline evidence is missing"), 2)
        self.assertIn("Ran 2 tests", red_output)
        self.assertIn("FAILED (failures=2)", red_output)
        self.assertIn("Exit code: 0", green_output)
        self.assertIn("Ran 3 tests", green_output)
        self.assertTrue(green_output.rstrip().endswith("OK"))
        self.assert_sha256(red_path, red["output_sha256"])
        self.assert_sha256(green_path, green["output_sha256"])
        self.assert_sha256(self.scenario_file(red["test_source_file"]), red["test_source_sha256"])
        self.assert_sha256(
            self.scenario_file(green["test_source_file"]),
            green["test_source_sha256"],
        )

        agents = execution["agents"]
        self.assertEqual(3, len(agents))
        agent_ids = {agent["agent_id"] for agent in agents}
        self.assertEqual(3, len(agent_ids))
        baseline_by_id = {case["id"]: case for case in self.load_evidence()["cases"]}
        self.assertEqual(set(baseline_by_id), {agent["case_id"] for agent in agents})
        for agent in agents:
            case = baseline_by_id[agent["case_id"]]
            self.assertEqual("none", agent["fork_turns"])
            self.assertFalse(agent["skill_loaded"])
            self.assertIn("不要读取或使用任何名为 cv-experiment-workflow 的 Skill", agent["spawn_prompt"])
            self.assertIn(case["prompt"], agent["spawn_prompt"])
            self.assertEqual(case["baseline_agent_id"], agent["agent_id"])
            self.assertEqual(case["baseline_output_file"], agent["output_file"])
            self.assert_sha256(self.scenario_file(agent["output_file"]), agent["output_sha256"])

    def test_forward_execution_and_directory_inspection_are_auditable(self) -> None:
        execution = json.loads(EXECUTION.read_text(encoding="utf-8"))
        forward = execution["forward_tests"]
        self.assertFalse(forward["attestation"]["cryptographic_platform_receipt"])
        cases = forward["cases"]
        self.assertEqual(3, len(cases))
        self.assertEqual(3, len({case["agent_id"] for case in cases}))
        baseline_by_id = {case["id"]: case for case in self.load_evidence()["cases"]}
        self.assertEqual(set(baseline_by_id), {case["case_id"] for case in cases})
        for observed in cases:
            expected = baseline_by_id[observed["case_id"]]
            self.assertTrue(observed["skill_loaded"])
            self.assertTrue(observed["pass"])
            self.assertEqual(expected["forward_agent_id"], observed["agent_id"])
            self.assertEqual(expected["forward_skill_commit"], observed["skill_commit"])
            self.assertEqual(expected["forward_output_file"], observed["output_file"])
            self.assertEqual(expected["forward_output_sha256"], observed["output_sha256"])
            self.assert_sha256(
                self.scenario_file(observed["output_file"]), observed["output_sha256"]
            )
            audit = observed["project_audit"]
            self.assertTrue(audit["exists_at_inspection"])
            self.assertEqual(8, audit["regular_file_count"])
            self.assertEqual(0, audit["role_memory_file_count"])
            self.assertEqual([], audit["unexpected_markdown_files"])
            self.assertTrue(audit["validation"]["valid"])
            for name in (
                "trials", "code_assets", "attempts", "results", "versions",
            ):
                self.assertEqual(0, audit["validation"][name])

    def test_tracked_release_text_has_no_personal_local_roots(self) -> None:
        username = "Admin" + "istrator"
        backup_root = "Back" + "up"
        documents = "Docu" + "ments"
        owner = "My" + "self"
        patterns = (
            re.compile(
                rf"[A-Za-z]:[\\/]+Users[\\/]+{username}(?:[\\/]|$)",
                re.IGNORECASE,
            ),
            re.compile(
                rf"[A-Za-z]:[\\/]+{backup_root}[\\/]+{documents}"
                rf"[\\/]+{owner}(?:[\\/]|$)",
                re.IGNORECASE,
            ),
            re.compile(r"\." + "worktrees" + r"[\\/]+v1(?:[\\/]|$)"),
        )
        listed = subprocess.run(
            [
                "git", "ls-files", "-z", "--cached", "--others",
                "--exclude-standard",
            ], cwd=REPOSITORY,
            capture_output=True, check=True,
        ).stdout.split(b"\0")
        findings: list[str] = []
        for encoded_path in listed:
            if not encoded_path:
                continue
            relative = encoded_path.decode("utf-8")
            try:
                text = (REPOSITORY / relative).read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except UnicodeDecodeError:
                continue
            if any(pattern.search(text) for pattern in patterns):
                findings.append(relative)
        self.assertEqual([], findings, "tracked text contains personal local roots")


if __name__ == "__main__":
    unittest.main()
