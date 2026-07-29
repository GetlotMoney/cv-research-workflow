from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG_FILE = ROOT / "config" / "directions" / "catalog.json"


class DirectionCatalogTests(unittest.TestCase):
    def test_catalog_is_the_fixed_six_direction_status_source(self) -> None:
        self.assertTrue(CATALOG_FILE.is_file(), "六方向 catalog 尚未建立")
        catalog = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))

        self.assertEqual("cvwf.direction-catalog.v1", catalog["schema"])
        self.assertEqual("DATA-DIRECTIONS-V1.0.0", catalog["version"])
        self.assertEqual(
            [
                {
                    "id": "image_classification",
                    "slug": "cls",
                    "label_zh": "图像分类",
                    "status": "pending",
                    "repository_factory": None,
                },
                {
                    "id": "object_detection",
                    "slug": "det",
                    "label_zh": "目标检测",
                    "status": "pending",
                    "repository_factory": None,
                },
                {
                    "id": "instance_segmentation",
                    "slug": "instseg",
                    "label_zh": "实例分割",
                    "status": "pending",
                    "repository_factory": None,
                },
                {
                    "id": "semantic_segmentation",
                    "slug": "seg",
                    "label_zh": "语义分割",
                    "status": "pending",
                    "repository_factory": None,
                },
                {
                    "id": "super_resolution",
                    "slug": "sr",
                    "label_zh": "超分辨率",
                    "status": "pending",
                    "repository_factory": None,
                },
                {
                    "id": "gzsl",
                    "slug": "gzsl",
                    "label_zh": "广义零样本学习",
                    "status": "ready",
                    "repository_factory": "create_gzsl_repository",
                },
            ],
            catalog["directions"],
        )
        self.assertTrue(
            all(
                set(direction)
                == {
                    "id",
                    "slug",
                    "label_zh",
                    "status",
                    "repository_factory",
                }
                for direction in catalog["directions"]
            )
        )


if __name__ == "__main__":
    unittest.main()
