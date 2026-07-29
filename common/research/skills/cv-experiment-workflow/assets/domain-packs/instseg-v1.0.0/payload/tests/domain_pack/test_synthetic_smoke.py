from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.instseg.runtime import run_training


class SyntheticSmokeTest(unittest.TestCase):
    def test_cpu_training_reloads_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_training(
                Path("configs/smoke.json"),
                Path(temporary) / "run",
                mode="synthetic_debug",
            )
        self.assertGreater(result["optimizer_steps"], 0)
        self.assertTrue(result["checkpoint_reloaded"])
        self.assertFalse(result["paper_eligible"])


if __name__ == "__main__":
    unittest.main()
