from __future__ import annotations

import unittest

import torch

from cls.metrics import classification_metrics


class MetricContractTests(unittest.TestCase):
    def test_two_class_top5_uses_real_class_count(self) -> None:
        metrics = classification_metrics(
            torch.tensor([[8.0, 1.0], [1.0, 8.0]]),
            torch.tensor([0, 1]),
        )
        self.assertEqual(1.0, metrics["top1_accuracy"])
        self.assertEqual(1.0, metrics["top5_accuracy"])
        self.assertEqual(1.0, metrics["macro_f1"])


if __name__ == "__main__":
    unittest.main()
