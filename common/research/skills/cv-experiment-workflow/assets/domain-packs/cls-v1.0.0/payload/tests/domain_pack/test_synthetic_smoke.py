from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cls.smoke import run_synthetic_smoke


class SyntheticSmokeTests(unittest.TestCase):
    def test_smoke_marks_output_as_non_paper_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "smoke"
            run = run_synthetic_smoke(
                output,
                config_path=Path("configs/smoke.json"),
                device="cpu",
            )
            self.assertEqual("synthetic_debug_only", run["run_kind"])
            self.assertFalse(run["paper_eligible"])
            self.assertTrue((output / "checkpoint.pt").is_file())


if __name__ == "__main__":
    unittest.main()
