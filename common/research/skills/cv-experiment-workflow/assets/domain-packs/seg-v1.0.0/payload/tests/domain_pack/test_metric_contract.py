from __future__ import annotations

import unittest

import torch

from seg.metrics import segmentation_metrics


class MetricContractTests(unittest.TestCase):
    def test_empty_union_is_null_and_all_ignore_is_rejected(self) -> None:
        result = segmentation_metrics(
            torch.tensor([[[0, 1], [1, 1]]]),
            torch.tensor([[[0, 0], [1, 255]]]),
            3,
        )
        self.assertEqual([0, 1], result["included_classes"])
        self.assertIsNone(result["per_class_iou"][2])
        with self.assertRaises(ValueError):
            segmentation_metrics(
                torch.zeros((1, 2, 2), dtype=torch.long),
                torch.full((1, 2, 2), 255, dtype=torch.long),
                2,
            )


if __name__ == "__main__":
    unittest.main()
