from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


SCENARIOS = Path(__file__).parent / "skill_scenarios"


class ForwardEvidenceTests(unittest.TestCase):
    def test_attempt_round_one_failure_is_sanitized_and_auditable(self) -> None:
        evaluation = json.loads(
            (SCENARIOS / "forward-attempt-round1.evaluation.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("skill-forward-evaluation.v1", evaluation["schema_version"])
        self.assertEqual("attempt-confirm-promotion", evaluation["case_id"])
        self.assertEqual(1, evaluation["round"])
        self.assertFalse(evaluation["pass"])
        output = SCENARIOS / evaluation["output_file"]
        self.assertEqual(output.name, evaluation["output_file"])
        self.assertEqual(
            evaluation["output_sha256"], hashlib.sha256(output.read_bytes()).hexdigest()
        )
        text = output.read_text(encoding="utf-8")
        self.assertIn("sanitized transcript（脱敏转录）", text)
        self.assertIn("原平台任务记录仍是最终来源", text)
        for failure in evaluation["failures"]:
            self.assertTrue(failure["summary"].strip())
            for snippet in failure["evidence_snippets"]:
                self.assertIn(snippet, text)
        self.assertRegex(evaluation["fix_commit"], r"^[0-9a-f]{40}$")
        final = evaluation["final_retest"]
        self.assertEqual(2, final["round"])
        self.assertTrue(final["pass"])
        final_output = SCENARIOS / final["output_file"]
        self.assertEqual(
            final["output_sha256"],
            hashlib.sha256(final_output.read_bytes()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
