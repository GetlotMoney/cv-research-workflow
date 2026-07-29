from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gzsl.smoke import run_synthetic_smoke


class SyntheticSmokeTests(unittest.TestCase):
    def test_smoke_never_uses_formal_metric_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            run = run_synthetic_smoke(
                output,
                config_path=Path("configs/smoke.json"),
            )
            self.assertGreater(run["optimizer_steps"], 0)
            evaluation = json.loads(
                (output / "evaluation.json").read_text(encoding="utf-8")
            )
            self.assertTrue(all(name.startswith("debug_") for name in evaluation["metrics"]))


if __name__ == "__main__":
    unittest.main()
