from __future__ import annotations

import unittest

import numpy as np

from sr.metrics import cumulative_rgb_psnr


class MetricContractTests(unittest.TestCase):
    def test_cumulative_uint8_and_zero_mse(self) -> None:
        target = np.zeros((2, 2, 3), dtype=np.uint8)
        predicted = np.ones((2, 2, 3), dtype=np.uint8)
        result = cumulative_rgb_psnr([(predicted, target)])
        self.assertEqual(12, result["sse"])
        self.assertEqual(12, result["value_count"])
        with self.assertRaises(ValueError):
            cumulative_rgb_psnr([(target, target)])


if __name__ == "__main__":
    unittest.main()
