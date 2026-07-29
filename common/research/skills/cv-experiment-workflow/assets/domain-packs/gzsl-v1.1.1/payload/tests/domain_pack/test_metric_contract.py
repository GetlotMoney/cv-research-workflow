from __future__ import annotations

import unittest

import numpy as np

from gzsl.metrics import gzsl_class_average_metrics


class MetricContractTests(unittest.TestCase):
    def test_class_average_and_zero_h(self) -> None:
        target = np.array([10, 20, 30, 40], dtype=np.int64)
        result = gzsl_class_average_metrics(
            np.array([20, 10, 40, 30], dtype=np.int64),
            target,
            np.array([10, 20], dtype=np.int64),
            np.array([30, 40], dtype=np.int64),
        )
        self.assertEqual({"S": 0.0, "U": 0.0, "H": 0.0}, result)


if __name__ == "__main__":
    unittest.main()
