from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import torch
from PIL import Image

from tests._helpers import SCRIPTS, cli_json, read_json


sys.path.insert(0, str(SCRIPTS))

from workflow_core.domain_packs import (  # noqa: E402
    discover_domain_packs,
    resolve_domain_pack,
    validate_domain_pack,
)
from workflow_core.project import init_project  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SOURCE = {
    "name": "torchvision",
    "url": "https://github.com/pytorch/vision",
    "revision": "78839c2c243098a860dc8de1583c311ef2966c98",
    "license": "BSD-3-Clause",
}
EXPECTED_VARIANT_FIELDS = {"code", "config", "seed", "data", "environment"}
DATASET_IDENTITY_FIELDS = {
    "schema",
    "dataset_id",
    "version",
    "source_uri",
    "manifest_sha256",
    "split",
}
SEG_FORMAL_METRIC_DEFINITION = {
    "mean_iou": (
        "Mean intersection over union computed from one whole-dataset "
        "confusion matrix; label 255 is ignored and classes with zero union "
        "are omitted; range 0 to 1, higher is better."
    ),
}


class SegmentationDomainPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _create_repository(self) -> Path:
        project = self.root / "workflow"
        repository = self.root / "segmentation-repository"
        init_project(project, "segmentation-tests", layout="v2")
        created = cli_json(
            "create-domain-repo",
            "--project",
            project,
            "--pack",
            "seg",
            "--destination",
            repository,
            "--name",
            "segmentation",
        )
        self.assertEqual("PACK-SEG", created["repository"]["template_id"])
        return repository

    @staticmethod
    def _load_adapter(repository: Path) -> types.ModuleType:
        path = repository / "workflow_adapter.py"
        module = types.ModuleType("_seg_workflow_adapter")
        module.__file__ = str(path)
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        return module

    @staticmethod
    def _run_module(
        repository: Path,
        *arguments: str,
        timeout: float = 60.0,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", *arguments],
            cwd=repository,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"exit={result.returncode}\nstdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        return result

    @staticmethod
    def _write_seg_dataset(root: Path) -> None:
        for split in ("train", "val"):
            images = root / split / "images"
            masks = root / split / "masks"
            images.mkdir(parents=True, exist_ok=True)
            masks.mkdir(parents=True, exist_ok=True)
            for index in range(2):
                pixels = np.zeros((8, 8, 3), dtype=np.uint8)
                pixels[:, :, 0] = 35 + index * 90
                pixels[:, 4:, 1] = 170
                Image.fromarray(pixels, mode="RGB").save(
                    images / f"sample-{index}.png"
                )
                labels = np.zeros((8, 8), dtype=np.uint8)
                labels[:, 4:] = 1
                if split == "val" and index == 1:
                    labels[:2, :2] = 255
                Image.fromarray(labels, mode="L").save(
                    masks / f"sample-{index}.png"
                )

    @staticmethod
    def _import_pack(repository: Path, module_name: str):
        sys.path.insert(0, str(repository))
        try:
            return importlib.import_module(module_name)
        finally:
            sys.path.remove(str(repository))

    def _manifest_sha256(self, repository: Path, data_root: Path) -> str:
        module = self._import_pack(repository, "seg.data")
        try:
            return module.build_dataset_manifest(data_root)["manifest_sha256"]
        finally:
            for name in tuple(sys.modules):
                if name == "seg" or name.startswith("seg."):
                    sys.modules.pop(name, None)

    @staticmethod
    def _refresh_artifact_records(output: Path, *relative_paths: str) -> None:
        manifest_path = output / "artifacts.json"
        manifest = read_json(manifest_path)
        records = {
            item["path"]: item
            for item in manifest["artifacts"]
        }
        for relative in relative_paths:
            content = (output / Path(*relative.split("/"))).read_bytes()
            records[relative]["size"] = len(content)
            records[relative]["sha256"] = hashlib.sha256(content).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_formal_prepare_injects_fixed_metric_definitions(self) -> None:
        repository = self._create_repository()
        data_root = self.root / "seg-metric-data"
        self._write_seg_dataset(data_root)
        manifest_sha256 = self._manifest_sha256(repository, data_root)
        adapter = self._load_adapter(repository)
        config = {
            "mode": "local_segmentation",
            "data_root": str(data_root),
            "dataset_id": "tiny-seg-metric",
            "version": "2026.07",
            "source_uri": "https://example.org/datasets/tiny-seg-metric",
            "manifest_sha256": manifest_sha256,
            "device": "cpu",
        }
        variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": config, "seed": 17}},
        )[0]
        self.assertNotIn("metric_definition", config)
        self.assertIn("metric_definition", variant["config"])
        self.assertEqual(
            SEG_FORMAL_METRIC_DEFINITION,
            variant["config"]["metric_definition"],
        )

        user_override = dict(config)
        user_override["metric_definition"] = dict(
            SEG_FORMAL_METRIC_DEFINITION
        )
        with self.assertRaisesRegex(ValueError, "config|字段|metric_definition"):
            adapter.prepare_runs(
                repository,
                {"route_inputs": {"config": user_override, "seed": 17}},
            )

        for mutation in ("missing", "changed", "extra"):
            with self.subTest(mutation=mutation):
                tampered = copy.deepcopy(variant)
                definitions = tampered["config"]["metric_definition"]
                if mutation == "missing":
                    definitions.pop("mean_iou")
                elif mutation == "changed":
                    definitions["mean_iou"] = "user-defined score"
                else:
                    definitions["other"] = "unknown metric"
                with self.assertRaisesRegex(
                    ValueError,
                    "metric_definition|指标|口径|config",
                ):
                    adapter.execute(
                        repository,
                        "status",
                        {
                            "id": "RUN-0088",
                            "purpose": "evidence",
                            "frozen": tampered,
                        },
                    )

    def test_pack_manifest_source_and_public_adapter_are_exact(self) -> None:
        listed = {item["id"]: item for item in discover_domain_packs()}
        self.assertIn("seg", listed)
        self.assertEqual("1.0.0", listed["seg"]["version"])
        pack_root = resolve_domain_pack("seg@1.0.0")
        manifest = validate_domain_pack(pack_root)
        self.assertEqual([EXPECTED_SOURCE], manifest["source_references"])
        payload = pack_root / "payload"
        contract = read_json(payload / "domain-pack.json")
        self.assertEqual("PACK-SEG", contract["template_id"])
        self.assertEqual(
            ["train", "evaluate", "infer", "synthetic_smoke"],
            contract["commands"],
        )
        self.assertEqual("synthetic_debug_only", contract["synthetic_smoke"]["run_kind"])
        self.assertFalse(contract["synthetic_smoke"]["paper_eligible"])
        all_python = "\n".join(
            path.read_text(encoding="utf-8")
            for path in payload.rglob("*.py")
        )
        self.assertNotIn("domain-packs.cls", all_python)
        self.assertNotIn("from shared", all_python)
        self.assertNotIn("from common", all_python)

        adapter = self._load_adapter(payload)
        callables = {
            name
            for name, value in vars(adapter).items()
            if not name.startswith("_") and callable(value)
        }
        self.assertEqual(
            {"inspect", "validate", "prepare_runs", "execute", "parse_result"},
            callables,
        )
        downloads = read_json(payload / "DOWNLOADS.json")
        for resource in downloads["optional_resources"]:
            self.assertTrue(
                {
                    "name",
                    "kind",
                    "url",
                    "version",
                    "revision",
                    "license",
                    "license_url",
                    "sha256",
                }.issubset(resource)
            )
            self.assertTrue(resource["url"].startswith("https://"))
            self.assertEqual("dependency_release_page", resource["kind"])
            for key in ("version", "revision", "license", "license_url"):
                self.assertIsInstance(resource[key], str)
                self.assertTrue(resource[key])
            self.assertTrue(resource["license_url"].startswith("https://"))
            self.assertNotIn(
                resource["revision"].casefold(),
                {"main", "master", "latest", "stable", "current"},
            )
            self.assertIsNone(resource["sha256"])

    def test_segmentation_metric_uses_whole_dataset_confusion_matrix(self) -> None:
        repository = self._create_repository()
        metrics = self._import_pack(repository, "seg.metrics")
        try:
            predicted = torch.tensor(
                [
                    [[0, 0], [1, 2]],
                    [[0, 2], [2, 2]],
                ],
                dtype=torch.long,
            )
            target = torch.tensor(
                [
                    [[0, 1], [1, 255]],
                    [[0, 1], [2, 2]],
                ],
                dtype=torch.long,
            )
            result = metrics.segmentation_metrics(predicted, target, 4)
            self.assertEqual(
                [[2, 0, 0, 0], [1, 1, 1, 0], [0, 0, 2, 0], [0, 0, 0, 0]],
                result["confusion_matrix"],
            )
            self.assertEqual([0, 1, 2], result["included_classes"])
            self.assertIsNone(result["per_class_iou"][3])
            expected = (2 / 3 + 1 / 3 + 2 / 3) / 3
            self.assertAlmostEqual(expected, result["mean_iou"], places=7)
            self.assertTrue(math.isfinite(result["mean_iou"]))
            with self.assertRaisesRegex(ValueError, "ignore"):
                metrics.segmentation_metrics(
                    torch.zeros((1, 2, 2), dtype=torch.long),
                    torch.full((1, 2, 2), 255, dtype=torch.long),
                    2,
                )
        finally:
            for name in tuple(sys.modules):
                if name == "seg" or name.startswith("seg."):
                    sys.modules.pop(name, None)

    def test_pixel_cross_entropy_matches_dense_loss_and_backpropagates(
        self,
    ) -> None:
        repository = self._create_repository()
        runtime = self._import_pack(repository, "seg.runtime")
        try:
            logits = torch.randn(
                2,
                3,
                4,
                5,
                dtype=torch.float32,
                requires_grad=True,
            )
            masks = torch.randint(0, 3, (2, 4, 5), dtype=torch.long)
            masks[0, 0, 0] = 255
            expected = torch.nn.functional.cross_entropy(
                logits,
                masks,
                ignore_index=255,
            )
            actual = runtime._pixel_cross_entropy(logits, masks)
            torch.testing.assert_close(actual, expected)
            actual.backward()
            self.assertIsNotNone(logits.grad)
            self.assertTrue(bool(torch.isfinite(logits.grad).all()))
        finally:
            for name in tuple(sys.modules):
                if name == "seg" or name.startswith("seg."):
                    sys.modules.pop(name, None)

    def test_output_evaluation_is_recomputed_from_confusion_matrix(self) -> None:
        repository = self._create_repository()
        adapter = self._load_adapter(repository)
        definition = {
            "aggregation": "whole_dataset_confusion_matrix",
            "ignore_index": 255,
            "zero_union": "null_and_omitted_from_mean",
            "one_sided_union": "included",
        }
        contradictory = [
            {
                "schema": "pack-seg.evaluation.v1",
                "metrics": {"debug_mean_iou": 0.5},
                "confusion_matrix": [[5, 0], [0, 5]],
                "per_class_iou": [0.5, 0.5],
                "included_classes": [0, 1],
                "definition": definition,
            },
            {
                "schema": "pack-seg.evaluation.v1",
                "metrics": {"debug_mean_iou": 1.0},
                "confusion_matrix": [[5, 0], [0, 5]],
                "per_class_iou": [1.0, None],
                "included_classes": [0],
                "definition": definition,
            },
        ]
        for payload in contradictory:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(
                    ValueError,
                    "confusion|混淆矩阵|重算|IoU|included",
                ):
                    adapter._validate_evaluation(
                        payload,
                        synthetic=True,
                        num_classes=2,
                    )

    def test_output_review_recounts_total_artifact_bytes(self) -> None:
        repository = self._create_repository()
        adapter = self._load_adapter(repository)
        contract = self._import_pack(repository, "seg.contract")
        output = self.root / "seg-output-budget"
        output.mkdir()
        records = []
        for name in ("one.bin", "two.bin"):
            content = b"1234"
            (output / name).write_bytes(content)
            records.append(
                {
                    "path": name,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        (output / "artifacts.json").write_text(
            json.dumps(
                {
                    "schema": "pack-seg.artifacts.v1",
                    "artifacts": records,
                }
            ),
            encoding="utf-8",
        )
        try:
            with mock.patch.object(
                adapter,
                "_MAX_TOTAL_SIZE",
                7,
                create=True,
            ):
                with self.assertRaisesRegex(ValueError, "总体积|总.*字节|上限"):
                    adapter._validate_artifacts(output)
            with mock.patch.object(contract, "MAX_DATASET_SIZE", 7):
                with self.assertRaisesRegex(ValueError, "总体积|总.*字节|上限"):
                    contract.validate_artifact_manifest(output)
        finally:
            for name in tuple(sys.modules):
                if name == "seg" or name.startswith("seg."):
                    sys.modules.pop(name, None)

    def test_artifact_manifest_bytes_are_counted_in_total_budget(
        self,
    ) -> None:
        repository = self._create_repository()
        adapter = self._load_adapter(repository)
        contract = self._import_pack(repository, "seg.contract")
        output = self.root / "seg-output-manifest-budget"
        output.mkdir()
        content = b"x" * 512
        (output / "artifact.bin").write_bytes(content)
        manifest = (
            json.dumps(
                {
                    "schema": "pack-seg.artifacts.v1",
                    "artifacts": [
                        {
                            "path": "artifact.bin",
                            "size": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    ],
                }
            ).encode("utf-8")
            + b" " * 32
        )
        (output / "artifacts.json").write_bytes(manifest)
        self.assertLess(len(manifest), len(content))
        self.assertGreater(len(content) + len(manifest), len(content))
        try:
            validators = (
                (
                    "adapter",
                    mock.patch.object(
                        adapter,
                        "_MAX_TOTAL_SIZE",
                        len(content),
                        create=True,
                    ),
                    adapter._validate_artifacts,
                ),
                (
                    "contract",
                    mock.patch.object(
                        contract,
                        "MAX_DATASET_SIZE",
                        len(content),
                    ),
                    contract.validate_artifact_manifest,
                ),
            )
            for label, limit_patch, validator in validators:
                with self.subTest(validator=label):
                    with limit_patch:
                        with self.assertRaisesRegex(
                            ValueError,
                            "总体积|总.*字节|上限",
                        ):
                            validator(output)
            writer_output = self.root / "seg-writer-manifest-budget"
            writer_output.mkdir()
            (writer_output / "artifact.bin").write_bytes(content)
            with mock.patch.object(
                contract,
                "MAX_DATASET_SIZE",
                len(content),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "总体积|总.*字节|上限",
                ):
                    contract.write_artifact_manifest(writer_output)
        finally:
            for name in tuple(sys.modules):
                if name == "seg" or name.startswith("seg."):
                    sys.modules.pop(name, None)

    def test_contract_rejects_manifest_growth_after_initial_read(self) -> None:
        repository = self._create_repository()
        contract = self._import_pack(repository, "seg.contract")
        output = self.root / "seg-contract-late-manifest-growth"
        output.mkdir()
        content = b"artifact"
        artifact = output / "artifact.bin"
        artifact.write_bytes(content)
        manifest_path = output / "artifacts.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema": "pack-seg.artifacts.v1",
                    "artifacts": [
                        {
                            "path": artifact.name,
                            "size": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        reader_name = (
            "read_json_snapshot"
            if hasattr(contract, "read_json_snapshot")
            else "read_json_with_size"
        )
        original_read = getattr(contract, reader_name)
        manifest_grew = False

        def grow_manifest_after_read(path, label, **kwargs):
            nonlocal manifest_grew
            observed = original_read(path, label, **kwargs)
            if Path(path) == manifest_path and not manifest_grew:
                manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
                manifest_grew = True
            return observed

        try:
            with mock.patch.object(
                contract,
                reader_name,
                side_effect=grow_manifest_after_read,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "变化|修改|替换|漂移|一致|总体积|上限",
                ):
                    contract.validate_artifact_manifest(output)
            self.assertTrue(manifest_grew)
        finally:
            for name in tuple(sys.modules):
                if name == "seg" or name.startswith("seg."):
                    sys.modules.pop(name, None)

    def test_adapter_rejects_manifest_growth_after_initial_read(self) -> None:
        repository = self._create_repository()
        adapter = self._load_adapter(repository)
        output = self.root / "seg-adapter-late-manifest-growth"
        output.mkdir()
        content = b"artifact"
        artifact = output / "artifact.bin"
        artifact.write_bytes(content)
        manifest_path = output / "artifacts.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema": "pack-seg.artifacts.v1",
                    "artifacts": [
                        {
                            "path": artifact.name,
                            "size": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        reader_name = (
            "_read_json_snapshot"
            if hasattr(adapter, "_read_json_snapshot")
            else "_read_json_with_size"
        )
        original_read = getattr(adapter, reader_name)
        manifest_grew = False

        def grow_manifest_after_read(path, label, **kwargs):
            nonlocal manifest_grew
            observed = original_read(path, label, **kwargs)
            if Path(path) == manifest_path and not manifest_grew:
                manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
                manifest_grew = True
            return observed

        with mock.patch.object(
            adapter,
            reader_name,
            side_effect=grow_manifest_after_read,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "变化|修改|替换|漂移|一致|总体积|上限",
            ):
                adapter._validate_artifacts(output)
        self.assertTrue(manifest_grew)

    def test_writer_rejects_artifact_growth_after_initial_read(self) -> None:
        repository = self._create_repository()
        contract = self._import_pack(repository, "seg.contract")
        output = self.root / "seg-writer-late-artifact-growth"
        output.mkdir()
        artifact = output / "artifact.bin"
        artifact.write_bytes(b"artifact")
        original_read = contract.secure_read_bytes
        artifact_grew = False

        def grow_artifact_after_read(path, label, **kwargs):
            nonlocal artifact_grew
            observed = original_read(path, label, **kwargs)
            if Path(path) == artifact and not artifact_grew:
                artifact.write_bytes(observed + b"-late-growth")
                artifact_grew = True
            return observed

        try:
            with mock.patch.object(
                contract,
                "secure_read_bytes",
                side_effect=grow_artifact_after_read,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "变化|修改|替换|漂移|一致|SHA-256|大小|总体积",
                ):
                    contract.write_artifact_manifest(output)
            self.assertTrue(artifact_grew)
        finally:
            for name in tuple(sys.modules):
                if name == "seg" or name.startswith("seg."):
                    sys.modules.pop(name, None)

    def test_output_png_budget_precedes_pillow_decode(self) -> None:
        repository = self._create_repository()
        adapter = self._load_adapter(repository)
        output = self.root / "seg-output-png"
        masks = output / "predictions" / "masks"
        masks.mkdir(parents=True)
        bomb = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">IIBBBBB", 2048, 2048, 8, 0, 0, 0, 0)
            + b"\x00\x00\x00\x00"
        )
        (masks / "0000.png").write_bytes(bomb)
        (output / "predictions" / "index.json").write_text(
            json.dumps(
                {
                    "schema": "pack-seg.predictions.v1",
                    "predictions": [
                        {
                            "input": "synthetic://0000",
                            "mask": "masks/0000.png",
                            "width": 2048,
                            "height": 2048,
                            "sha256": hashlib.sha256(bomb).hexdigest(),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        pillow_called = False

        def reject_decode(*_args, **_kwargs):
            nonlocal pillow_called
            pillow_called = True
            raise OSError("decoder must not run")

        with mock.patch.object(Image, "open", side_effect=reject_decode):
            with self.assertRaisesRegex(ValueError, "压缩比|像素|尺寸|PNG"):
                adapter._validate_predictions(output, 1)
        self.assertFalse(
            pillow_called,
            "篡改输出的 PNG 预算必须在 Pillow 解码前被拒绝",
        )

    def test_dataset_pairing_modes_ids_dimensions_and_manifest_are_strict(self) -> None:
        repository = self._create_repository()
        data_root = self.root / "seg-data"
        self._write_seg_dataset(data_root)
        data = self._import_pack(repository, "seg.data")
        try:
            manifest = data.build_dataset_manifest(data_root)
            self.assertRegex(manifest["manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(len(manifest["files"]), 0)
            dataset = data.SegmentationDataset(
                data_root / "train",
                image_size=8,
                num_classes=2,
            )
            self.assertEqual(2, len(dataset))
            image, mask, sample_id = dataset[0]
            self.assertEqual((3, 8, 8), tuple(image.shape))
            self.assertEqual((8, 8), tuple(mask.shape))
            self.assertEqual("sample-0", sample_id)

            (data_root / "train" / "masks" / "sample-1.png").unlink()
            with self.assertRaisesRegex(ValueError, "配对"):
                data.SegmentationDataset(
                    data_root / "train",
                    image_size=8,
                    num_classes=2,
                )
            self._write_seg_dataset(data_root)
            bad_mask = np.full((7, 8), 4, dtype=np.uint8)
            Image.fromarray(bad_mask, mode="L").save(
                data_root / "train" / "masks" / "sample-0.png"
            )
            with self.assertRaisesRegex(ValueError, "尺寸|类别"):
                data.SegmentationDataset(
                    data_root / "train",
                    image_size=8,
                    num_classes=2,
                )
        finally:
            for name in tuple(sys.modules):
                if name == "seg" or name.startswith("seg."):
                    sys.modules.pop(name, None)

    def test_snapshot_bytes_are_consumed_and_png_bombs_fail_before_decode(self) -> None:
        repository = self._create_repository()
        data_root = self.root / "seg-snapshot-data"
        self._write_seg_dataset(data_root)
        data = self._import_pack(repository, "seg.data")
        runtime = self._import_pack(repository, "seg.runtime")
        try:
            manifest, snapshot = data.capture_dataset(data_root)
            original_digest = manifest["manifest_sha256"]
            (data_root / "train" / "images" / "sample-0.png").write_bytes(
                b"changed-after-manifest"
            )
            dataset = data.SegmentationDataset(
                data_root / "train",
                image_size=8,
                num_classes=2,
                snapshot=snapshot,
                snapshot_root=data_root,
            )
            image, mask, sample_id = dataset[0]
            self.assertEqual((3, 8, 8), tuple(image.shape))
            self.assertEqual((8, 8), tuple(mask.shape))
            self.assertEqual("sample-0", sample_id)

            self._write_seg_dataset(data_root)
            real_capture = runtime.capture_dataset

            def capture_then_poison(root: Path):
                captured = real_capture(root)
                (Path(root) / "train" / "images" / "sample-0.png").unlink()
                return captured

            with mock.patch.object(
                runtime,
                "capture_dataset",
                side_effect=capture_then_poison,
            ):
                cpu_config = read_json(
                    repository / "configs" / "baseline.json"
                )
                cpu_config["device"] = "cpu"
                run = runtime.run_local_training(
                    config=cpu_config,
                    data_root=data_root,
                    output_dir=self.root / "seg-snapshot-output",
                    seed=7,
                    run_id="RUN-0091",
                    config_sha256="1" * 64,
                    dataset_id="snapshot-seg",
                    version="1",
                    source_uri="https://example.org/snapshot-seg",
                    manifest_sha256=original_digest,
                )
            self.assertGreater(run["optimizer_steps"], 0)

            png_bomb = (
                b"\x89PNG\r\n\x1a\n"
                + struct.pack(">I", 13)
                + b"IHDR"
                + struct.pack(">IIBBBBB", 2048, 2048, 8, 2, 0, 0, 0)
                + b"\x00\x00\x00\x00"
            )
            with mock.patch.object(
                Image,
                "open",
                side_effect=AssertionError("PNG 预算检查必须先于 Pillow 解码"),
            ):
                with self.assertRaisesRegex(ValueError, "压缩比|像素|尺寸"):
                    data.inspect_png_budget(png_bomb, label="RGB", channels=3)

            self._write_seg_dataset(data_root)
            with mock.patch.object(data, "MAX_DATASET_PIXELS", 100):
                with self.assertRaisesRegex(ValueError, "累计|像素"):
                    data.capture_dataset(data_root)
        finally:
            for name in tuple(sys.modules):
                if name == "seg" or name.startswith("seg."):
                    sys.modules.pop(name, None)

    def test_adapter_runs_real_synthetic_training_and_hashes_every_artifact(self) -> None:
        repository = self._create_repository()
        adapter = self._load_adapter(repository)
        variants = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": {"mode": "synthetic_smoke"}, "seed": 13}},
        )
        self.assertEqual(1, len(variants))
        self.assertEqual(EXPECTED_VARIANT_FIELDS, set(variants[0]))
        self.assertIn("evaluation", variants[0]["data"])
        self.assertNotIn("run_kind", variants[0]["data"])
        self.assertNotIn("paper_eligible", variants[0]["data"])
        run = {"id": "RUN-0001", "purpose": "debug", "frozen": variants[0]}
        self.assertEqual(
            {"status": "finished", "process_id": None},
            adapter.execute(repository, "start", run),
        )
        parsed = adapter.parse_result(repository, run)
        self.assertEqual("succeeded", parsed["execution"]["outcome"])
        metrics = parsed["result"]["metrics"]
        self.assertEqual({"debug_mean_iou"}, set(metrics))
        output = repository / ".cv-workflow-output" / "RUN-0001"
        record = read_json(output / "run.json")
        self.assertEqual("synthetic_debug_only", record["run_kind"])
        self.assertFalse(record["paper_eligible"])
        self.assertTrue(record["checkpoint_reloaded_for_evaluation"])
        self.assertTrue(record["checkpoint_reloaded_for_inference"])
        artifact_manifest = read_json(output / "artifacts.json")
        listed = {item["path"]: item for item in artifact_manifest["artifacts"]}
        for required in (
            "checkpoint.pt",
            "evaluation.json",
            "predictions/index.json",
            "raw.log",
            "run.json",
        ):
            self.assertIn(required, listed)
        for relative, item in listed.items():
            content = (output / relative).read_bytes()
            self.assertEqual(len(content), item["size"])
            self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])
        mask_files = sorted((output / "predictions" / "masks").glob("*.png"))
        self.assertTrue(mask_files)
        with Image.open(mask_files[0]) as mask:
            self.assertEqual("L", mask.mode)
            self.assertGreater(mask.width * mask.height, 0)

        index_path = output / "predictions" / "index.json"
        index_payload = read_json(index_path)
        mask_relative = index_payload["predictions"][0]["mask"]
        mask_path = output / "predictions" / mask_relative
        original_mask = mask_path.read_bytes()
        original_index = index_path.read_bytes()
        invalid_label = np.full(
            (
                index_payload["predictions"][0]["height"],
                index_payload["predictions"][0]["width"],
            ),
            record["num_classes"],
            dtype=np.uint8,
        )
        Image.fromarray(invalid_label, mode="L").save(mask_path)
        index_payload["predictions"][0]["sha256"] = hashlib.sha256(
            mask_path.read_bytes()
        ).hexdigest()
        index_path.write_text(
            json.dumps(index_payload, ensure_ascii=False),
            encoding="utf-8",
        )
        self._refresh_artifact_records(
            output,
            f"predictions/{mask_relative}",
            "predictions/index.json",
        )
        with self.assertRaisesRegex(ValueError, "类别|像素|mask|范围"):
            adapter.parse_result(repository, run)

        mask_path.write_bytes(original_mask)
        index_path.write_bytes(original_index)
        index_payload = read_json(index_path)
        bomb = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">IIBBBBB", 2048, 2048, 8, 0, 0, 0, 0)
            + b"\x00\x00\x00\x00"
        )
        mask_path.write_bytes(bomb)
        index_payload["predictions"][0]["width"] = 2048
        index_payload["predictions"][0]["height"] = 2048
        index_payload["predictions"][0]["sha256"] = hashlib.sha256(
            bomb
        ).hexdigest()
        index_path.write_text(
            json.dumps(index_payload, ensure_ascii=False),
            encoding="utf-8",
        )
        self._refresh_artifact_records(
            output,
            f"predictions/{mask_relative}",
            "predictions/index.json",
        )
        pillow_called = False

        def reject_decode(*_args, **_kwargs):
            nonlocal pillow_called
            pillow_called = True
            raise OSError("decoder must not run")

        with mock.patch.object(Image, "open", side_effect=reject_decode):
            with self.assertRaisesRegex(ValueError, "压缩比|像素|尺寸|PNG"):
                adapter.parse_result(repository, run)
        self.assertFalse(
            pillow_called,
            "篡改输出的 PNG 预算必须在 Pillow 解码前被拒绝",
        )
        with self.assertRaises(FileExistsError):
            adapter.execute(repository, "start", run)

    def test_formal_local_run_binds_dataset_identity_and_rejects_tampering(self) -> None:
        repository = self._create_repository()
        data_root = self.root / "seg-data"
        self._write_seg_dataset(data_root)
        manifest_sha256 = self._manifest_sha256(repository, data_root)
        adapter = self._load_adapter(repository)
        config = {
            "mode": "local_segmentation",
            "data_root": str(data_root),
            "dataset_id": "tiny-seg",
            "version": "2026.1",
            "source_uri": "https://example.org/datasets/tiny-seg",
            "manifest_sha256": manifest_sha256,
            "device": "cpu",
        }
        variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": config, "seed": 17}},
        )[0]
        self.assertEqual(EXPECTED_VARIANT_FIELDS, set(variant))
        self.assertEqual(
            {"kind", "dataset_identity", "evaluation"},
            set(variant["data"]),
        )
        self.assertEqual("local_dataset", variant["data"]["kind"])
        self.assertEqual(
            DATASET_IDENTITY_FIELDS,
            set(variant["data"]["dataset_identity"]),
        )
        self.assertEqual(
            "cv-experiment-workflow.dataset-identity.v1",
            variant["data"]["dataset_identity"]["schema"],
        )
        self.assertEqual("train+val", variant["data"]["dataset_identity"]["split"])
        run = {"id": "RUN-0002", "purpose": "evidence", "frozen": variant}
        adapter.execute(repository, "start", run)
        parsed = adapter.parse_result(repository, run)
        self.assertEqual({"mean_iou"}, set(parsed["result"]["metrics"]))
        output = repository / ".cv-workflow-output" / "RUN-0002"
        evaluation = read_json(output / "evaluation.json")
        self.assertEqual({"mean_iou"}, set(evaluation["metrics"]))
        self.assertIn("confusion_matrix", evaluation)
        self.assertIn("per_class_iou", evaluation)
        self.assertIn("included_classes", evaluation)

        checkpoint = output / "checkpoint.pt"
        checkpoint.write_bytes(b"not-a-checkpoint")
        with self.assertRaisesRegex(ValueError, "checkpoint|SHA-256|哈希"):
            adapter.parse_result(repository, run)

    def test_formal_device_is_frozen_and_cuda_unavailable_writes_nothing(
        self,
    ) -> None:
        source = (
            ROOT
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "seg-v1.0.0"
            / "payload"
        )
        repository = self.root / "seg-gpu-contract"
        shutil.copytree(source, repository)
        data_root = self.root / "seg-gpu-data"
        self._write_seg_dataset(data_root)
        manifest_sha256 = self._manifest_sha256(repository, data_root)
        adapter = self._load_adapter(repository)
        config = {
            "mode": "local_segmentation",
            "data_root": str(data_root),
            "dataset_id": "tiny-seg",
            "version": "1",
            "source_uri": "https://example.org/tiny-seg",
            "manifest_sha256": manifest_sha256,
            "device": "cuda",
        }
        variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": config, "seed": 41}},
        )[0]
        self.assertEqual("cuda", variant["config"]["device"])
        self.assertEqual(
            {"backend": "project", "device": "cuda"},
            variant["environment"],
        )
        run = {"id": "RUN-0041", "purpose": "evidence", "frozen": variant}
        with mock.patch.object(torch.cuda, "is_available", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "CUDA|cuda|GPU"):
                adapter.execute(repository, "start", run)
        self.assertFalse((repository / ".cv-workflow-output").exists())

        with (
            mock.patch.object(torch.cuda, "is_available", return_value=True),
            mock.patch.object(
                torch,
                "empty",
                side_effect=RuntimeError(
                    "no kernel image is available for execution on the device"
                ),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "CUDA.*小算子探测失败"):
                adapter.execute(repository, "start", run)
        self.assertFalse((repository / ".cv-workflow-output").exists())
        self.assertFalse((repository / ".cv-workflow-staging").exists())

        config["device"] = "cpu"
        cpu_variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": config, "seed": 41}},
        )[0]
        cpu_run = {
            "id": "RUN-0043",
            "purpose": "evidence",
            "frozen": cpu_variant,
        }
        self.assertEqual(
            {"status": "finished", "process_id": None},
            adapter.execute(repository, "start", cpu_run),
        )
        self.assertEqual(
            "cpu",
            read_json(
                repository
                / ".cv-workflow-output"
                / "RUN-0043"
                / "run.json"
            )["device"],
        )

    def test_formal_entrypoints_use_selected_device_and_safe_checkpoint_loading(
        self,
    ) -> None:
        source = (
            ROOT
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "seg-v1.0.0"
            / "payload"
        )
        repository = self.root / "seg-device-entrypoints"
        shutil.copytree(source, repository)
        data_root = self.root / "seg-device-data"
        self._write_seg_dataset(data_root)
        manifest_sha256 = self._manifest_sha256(repository, data_root)

        sys.path.insert(0, str(repository))
        try:
            from seg.contract import load_checkpoint
            from seg.evaluate import run_evaluation
            from seg.infer import run_inference
            from seg.train import run_training

            cuda_config = read_json(repository / "configs" / "baseline.json")
            cuda_config["device"] = "cuda"
            cuda_config_path = self.root / "seg-cuda-config.json"
            cuda_config_path.write_text(
                json.dumps(cuda_config, ensure_ascii=False),
                encoding="utf-8",
            )
            cuda_training_output = self.root / "seg-cuda-training"
            with mock.patch.object(torch.cuda, "is_available", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "CUDA|cuda|GPU"):
                    run_training(
                        cuda_config_path,
                        data_root,
                        cuda_training_output,
                        dataset_id="tiny-seg",
                        version="1",
                        source_uri="https://example.org/tiny-seg",
                        manifest_sha256=manifest_sha256,
                        run_id="RUN-0044",
                        config_sha256="7" * 64,
                    )
            self.assertFalse(cuda_training_output.exists())

            incompatible_output = self.root / "seg-incompatible-cuda"
            with (
                mock.patch.object(torch.cuda, "is_available", return_value=True),
                mock.patch.object(
                    torch,
                    "empty",
                    side_effect=RuntimeError(
                        "no kernel image is available for execution on the device"
                    ),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "CUDA.*小算子探测失败"):
                    run_training(
                        cuda_config_path,
                        data_root,
                        incompatible_output,
                        dataset_id="tiny-seg",
                        version="1",
                        source_uri="https://example.org/tiny-seg",
                        manifest_sha256=manifest_sha256,
                        run_id="RUN-0045",
                        config_sha256="a" * 64,
                    )
            self.assertFalse(incompatible_output.exists())

            training_output = self.root / "seg-device-training"
            run = run_training(
                repository / "configs" / "baseline.json",
                data_root,
                training_output,
                dataset_id="tiny-seg",
                version="1",
                source_uri="https://example.org/tiny-seg",
                manifest_sha256=manifest_sha256,
                run_id="RUN-0042",
                config_sha256="4" * 64,
                device_override="cpu",
            )
            self.assertEqual("cpu", run["device"])
            checkpoint_path = training_output / "checkpoint.pt"
            checkpoint = load_checkpoint(checkpoint_path)
            self.assertTrue(
                all(
                    tensor.device.type == "cpu"
                    for tensor in checkpoint["model_state_dict"].values()
                )
            )

            evaluation_output = self.root / "seg-cuda-evaluation"
            inference_output = self.root / "seg-cuda-inference"
            with mock.patch.object(torch.cuda, "is_available", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "CUDA|cuda|GPU"):
                    run_evaluation(
                        checkpoint_path,
                        data_root,
                        evaluation_output,
                        device="cuda",
                    )
                with self.assertRaisesRegex(RuntimeError, "CUDA|cuda|GPU"):
                    run_inference(
                        checkpoint_path,
                        data_root,
                        inference_output,
                        "val",
                        device="cuda",
                    )
            self.assertFalse(evaluation_output.exists())
            self.assertFalse(inference_output.exists())
        finally:
            for name in tuple(sys.modules):
                if name == "seg" or name.startswith("seg."):
                    sys.modules.pop(name, None)
            sys.path.remove(str(repository))

    def test_windows_cuda_environment_and_instructions_are_explicit(self) -> None:
        payload = (
            ROOT
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "seg-v1.0.0"
            / "payload"
        )
        adapter = self._load_adapter(payload)
        with mock.patch.dict(
            os.environ,
            {
                "CUDA_VISIBLE_DEVICES": "3",
                "CUBLAS_WORKSPACE_CONFIG": "wrong",
            },
            clear=False,
        ):
            environment = adapter._safe_environment(19)
        self.assertEqual("3", environment["CUDA_VISIBLE_DEVICES"])
        self.assertEqual(":4096:8", environment["CUBLAS_WORKSPACE_CONFIG"])

        readme = (payload / "README.md").read_text(encoding="utf-8")
        self.assertIn("https://pytorch.org/get-started/locally/", readme)
        self.assertIn("python -m seg.train", readme)
        self.assertIn("--device cuda", readme)
        self.assertIn("CUDA_VISIBLE_DEVICES", readme)
        self.assertIn("CUBLAS_WORKSPACE_CONFIG=:4096:8", readme)
        self.assertIn("synthetic", readme)
        cpu_lock = (
            payload / "requirements" / "windows-cpu.lock.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("https://download.pytorch.org/whl/cpu", cpu_lock)

    def test_formal_identity_rejects_local_uri_conflicts_and_bad_manifest(self) -> None:
        repository = self._create_repository()
        data_root = self.root / "seg-data"
        self._write_seg_dataset(data_root)
        manifest_sha256 = self._manifest_sha256(repository, data_root)
        adapter = self._load_adapter(repository)
        base = {
            "mode": "local_segmentation",
            "data_root": str(data_root),
            "dataset_id": "tiny-seg",
            "version": "1",
            "source_uri": "file:///tmp/tiny",
            "manifest_sha256": manifest_sha256,
            "device": "cpu",
        }
        with self.assertRaisesRegex(ValueError, "source_uri"):
            adapter.prepare_runs(
                repository,
                {"route_inputs": {"config": base, "seed": 1}},
            )
        base["source_uri"] = "https://example.org/tiny"
        base["manifest_sha256"] = "0" * 64
        variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": base, "seed": 1}},
        )[0]
        run = {"id": "RUN-0003", "purpose": "evidence", "frozen": variant}
        with self.assertRaisesRegex(RuntimeError, "manifest|哈希"):
            adapter.execute(repository, "start", run)

        variant["data"]["dataset_identity"]["dataset_id"] = "conflict"
        with self.assertRaisesRegex(ValueError, "identity|身份|冲突"):
            adapter.execute(
                repository,
                "start",
                {"id": "RUN-0004", "purpose": "evidence", "frozen": variant},
            )


if __name__ == "__main__":
    unittest.main()
