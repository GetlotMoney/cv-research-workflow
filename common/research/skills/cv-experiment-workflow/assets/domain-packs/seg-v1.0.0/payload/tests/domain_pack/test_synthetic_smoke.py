from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from seg.smoke import run_synthetic_smoke


class SyntheticSmokeTests(unittest.TestCase):
    def test_real_training_stays_debug_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            run = run_synthetic_smoke(
                output,
                config_path=Path("configs/smoke.json"),
            )
            self.assertGreater(run["optimizer_steps"], 0)
            self.assertEqual("synthetic_debug_only", run["run_kind"])
            self.assertFalse(run["paper_eligible"])
            evaluation = json.loads(
                (output / "evaluation.json").read_text(encoding="utf-8")
            )
            self.assertEqual({"debug_mean_iou"}, set(evaluation["metrics"]))


if __name__ == "__main__":
    unittest.main()
