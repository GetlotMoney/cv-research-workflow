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
from PIL import Image

from tests._helpers import SCRIPTS, cli_json, read_json


sys.path.insert(0, str(SCRIPTS))

from workflow_core.domain_packs import (  # noqa: E402
    discover_domain_packs,
    resolve_domain_pack,
    validate_domain_pack,
)
from workflow_core.project import init_project  # noqa: E402


EXPECTED_SOURCE = {
    "name": "BasicSR",
    "url": "https://github.com/XPixelGroup/BasicSR",
    "revision": "651835a1b9d38dbbdaf45750f56906be2364f01a",
    "license": "Apache-2.0",
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
SR_FORMAL_METRIC_DEFINITION = {
    "psnr_rgb_x2": (
        "RGB PSNR in dB for 2x super-resolution, computed from cumulative "
        "squared error over all uint8 RGB values without border shaving; "
        "higher is better and it is not directly comparable to standard "
        "Y-channel border-shaved PSNR."
    ),
}


class SuperResolutionDomainPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _create_repository(self) -> Path:
        project = self.root / "workflow"
        repository = self.root / "sr-repository"
        init_project(project, "sr-tests", layout="v2")
        created = cli_json(
            "create-domain-repo",
            "--project",
            project,
            "--pack",
            "sr",
            "--destination",
            repository,
            "--name",
            "super-resolution",
        )
        self.assertEqual("PACK-SR", created["repository"]["template_id"])
        return repository

    @staticmethod
    def _load_adapter(repository: Path) -> types.ModuleType:
        path = repository / "workflow_adapter.py"
        module = types.ModuleType("_sr_workflow_adapter")
        module.__file__ = str(path)
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        return module

    @staticmethod
    def _import_pack(repository: Path, module_name: str):
        sys.path.insert(0, str(repository))
        try:
            return importlib.import_module(module_name)
        finally:
            sys.path.remove(str(repository))

    @staticmethod
    def _write_dataset(root: Path) -> None:
        for split in ("train", "val"):
            hr_root = root / split / "HR"
            lr_root = root / split / "LR" / "x2"
            hr_root.mkdir(parents=True, exist_ok=True)
            lr_root.mkdir(parents=True, exist_ok=True)
            for index in range(2):
                lr = np.zeros((4 + index, 5, 3), dtype=np.uint8)
                lr[:, :, 0] = 35 + index * 50
                lr[:, 2:, 1] = 150
                hr = np.repeat(np.repeat(lr, 2, axis=0), 2, axis=1)
                hr[:, :, 2] = (hr[:, :, 2] + 5 + index) % 255
                Image.fromarray(lr, mode="RGB").save(
                    lr_root / f"sample-{index}x2.png"
                )
                Image.fromarray(hr, mode="RGB").save(
                    hr_root / f"sample-{index}.png"
                )

    def _manifest_sha256(self, repository: Path, data_root: Path) -> str:
        module = self._import_pack(repository, "sr.data")
        try:
            return module.build_dataset_manifest(data_root)["manifest_sha256"]
        finally:
            for name in tuple(sys.modules):
                if name == "sr" or name.startswith("sr."):
                    sys.modules.pop(name, None)

    def test_formal_prepare_injects_fixed_metric_definitions(self) -> None:
        repository = self._create_repository()
        data_root = self.root / "sr-metric-data"
        self._write_dataset(data_root)
        manifest_sha256 = self._manifest_sha256(repository, data_root)
        adapter = self._load_adapter(repository)
        config = {
            "mode": "local_sr_x2",
            "data_root": str(data_root),
            "dataset_id": "tiny-sr-metric",
            "version": "2026.07",
            "source_uri": "https://example.org/datasets/tiny-sr-metric",
            "manifest_sha256": manifest_sha256,
            "device": "cpu",
        }
        variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": config, "seed": 23}},
        )[0]
        self.assertNotIn("metric_definition", config)
        self.assertIn("metric_definition", variant["config"])
        self.assertEqual(
            SR_FORMAL_METRIC_DEFINITION,
            variant["config"]["metric_definition"],
        )

        user_override = dict(config)
        user_override["metric_definition"] = dict(
            SR_FORMAL_METRIC_DEFINITION
        )
        with self.assertRaisesRegex(ValueError, "config|字段|metric_definition"):
            adapter.prepare_runs(
                repository,
                {"route_inputs": {"config": user_override, "seed": 23}},
            )

        for mutation in ("missing", "changed", "extra"):
            with self.subTest(mutation=mutation):
                tampered = copy.deepcopy(variant)
                definitions = tampered["config"]["metric_definition"]
                if mutation == "missing":
                    definitions.pop("psnr_rgb_x2")
                elif mutation == "changed":
                    definitions["psnr_rgb_x2"] = "user-defined score"
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

    def test_pack_source_contract_and_adapter_are_self_contained(self) -> None:
        listed = {item["id"]: item for item in discover_domain_packs()}
        self.assertIn("sr", listed)
        pack_root = resolve_domain_pack("sr@1.0.0")
        manifest = validate_domain_pack(pack_root)
        self.assertEqual([EXPECTED_SOURCE], manifest["source_references"])
        payload = pack_root / "payload"
        contract = read_json(payload / "domain-pack.json")
        self.assertEqual("PACK-SR", contract["template_id"])
        self.assertEqual(2, contract["dataset"]["scale"])
        downloads = read_json(payload / "DOWNLOADS.json")
        for resource in downloads["optional_resources"]:
            self.assertEqual(
                {
                    "name",
                    "kind",
                    "url",
                    "version",
                    "revision",
                    "license",
                    "license_url",
                    "sha256",
                },
                set(resource),
            )
            self.assertTrue(resource["url"].startswith("https://"))
            for key in ("version", "revision", "license", "license_url"):
                self.assertIsInstance(resource[key], str)
                self.assertTrue(resource[key])
            self.assertTrue(resource["license_url"].startswith("https://"))
            self.assertNotIn(
                resource["revision"].casefold(),
                {"main", "master", "latest", "stable", "current"},
            )
            if resource["kind"] in {
                "dependency_release_page",
                "dataset_release_page",
            }:
                self.assertIsNone(resource["sha256"])
            elif resource["kind"] == "direct_artifact":
                self.assertRegex(resource["sha256"], r"^[0-9a-f]{64}$")
            else:
                self.fail(f"未知下载资源类型：{resource['kind']}")
        div2k = [
            item
            for item in downloads["optional_resources"]
            if item["name"] == "DIV2K"
        ][0]
        self.assertEqual(
            "仅供学术研究，图片版权归原权利人，使用前核验条款",
            div2k["license"],
        )
        self.assertEqual(
            "https://data.vision.ee.ethz.ch/cvl/DIV2K/",
            div2k["license_url"],
        )
        all_python = "\n".join(
            path.read_text(encoding="utf-8")
            for path in payload.rglob("*.py")
        )
        self.assertNotIn("from seg", all_python)
        self.assertNotIn("from shared", all_python)
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
        inspection = adapter.inspect(payload)
        self.assertEqual(
            "custom_full_rgb_no_shave",
            inspection.get("protocol"),
        )
        self.assertIs(False, inspection.get("comparable_to_standard"))
        metrics_contract = read_json(payload / "contracts" / "metrics.v1.json")
        formal = metrics_contract["formal_metrics"][0]
        self.assertEqual("custom_full_rgb_no_shave", formal["protocol"])
        self.assertEqual(
            "not_comparable_to_standard_y_channel_border_shave_without_recalculation",
            formal["comparability"],
        )

    def test_psnr_is_cumulative_rgb_uint8_and_perfect_match_is_rejected(self) -> None:
        repository = self._create_repository()
        metrics = self._import_pack(repository, "sr.metrics")
        try:
            target_small = np.zeros((1, 1, 3), dtype=np.uint8)
            predicted_small = np.full((1, 1, 3), 10, dtype=np.uint8)
            target_large = np.zeros((3, 3, 3), dtype=np.uint8)
            predicted_large = np.full((3, 3, 3), 2, dtype=np.uint8)
            result = metrics.cumulative_rgb_psnr(
                [
                    (predicted_small, target_small),
                    (predicted_large, target_large),
                ]
            )
            expected_sse = 3 * 100 + 27 * 4
            expected_count = 30
            expected_mse = expected_sse / expected_count
            expected_psnr = 10.0 * math.log10((255.0**2) / expected_mse)
            self.assertEqual(expected_sse, result["sse"])
            self.assertEqual(expected_count, result["value_count"])
            self.assertAlmostEqual(expected_mse, result["mse"], places=12)
            self.assertAlmostEqual(expected_psnr, result["psnr_rgb_x2"], places=12)
            with self.assertRaisesRegex(ValueError, "MSE=0|完全一致"):
                metrics.cumulative_rgb_psnr(
                    [(target_small.copy(), target_small.copy())]
                )
        finally:
            for name in tuple(sys.modules):
                if name == "sr" or name.startswith("sr."):
                    sys.modules.pop(name, None)

    def test_paired_dataset_requires_rgb_png_names_and_exact_x2_dimensions(self) -> None:
        repository = self._create_repository()
        data_root = self.root / "sr-data"
        self._write_dataset(data_root)
        data = self._import_pack(repository, "sr.data")
        try:
            manifest = data.build_dataset_manifest(data_root)
            self.assertRegex(manifest["manifest_sha256"], r"^[0-9a-f]{64}$")
            dataset = data.PairedX2Dataset(data_root / "train")
            self.assertEqual(2, len(dataset))
            lr, hr, sample_id = dataset[0]
            self.assertEqual(3, lr.shape[0])
            self.assertEqual(lr.shape[1] * 2, hr.shape[1])
            self.assertEqual(lr.shape[2] * 2, hr.shape[2])
            self.assertEqual("sample-0", sample_id)

            Image.new("RGB", (9, 8)).save(
                data_root / "train" / "HR" / "sample-0.png"
            )
            with self.assertRaisesRegex(ValueError, "x2|尺寸"):
                data.PairedX2Dataset(data_root / "train")
            self._write_dataset(data_root)
            (data_root / "train" / "LR" / "x2" / "sample-1x2.png").unlink()
            with self.assertRaisesRegex(ValueError, "配对"):
                data.PairedX2Dataset(data_root / "train")
        finally:
            for name in tuple(sys.modules):
                if name == "sr" or name.startswith("sr."):
                    sys.modules.pop(name, None)

    def test_snapshot_bytes_are_consumed_and_png_bombs_fail_before_decode(self) -> None:
        repository = self._create_repository()
        data_root = self.root / "sr-snapshot-data"
        self._write_dataset(data_root)
        data = self._import_pack(repository, "sr.data")
        runtime = self._import_pack(repository, "sr.runtime")
        try:
            manifest, snapshot = data.capture_dataset(data_root)
            original_digest = manifest["manifest_sha256"]
            (data_root / "train" / "LR" / "x2" / "sample-0x2.png").write_bytes(
                b"changed-after-manifest"
            )
            dataset = data.PairedX2Dataset(
                data_root / "train",
                snapshot=snapshot,
                snapshot_root=data_root,
            )
            lr, hr, sample_id = dataset[0]
            self.assertEqual((3, 4, 5), tuple(lr.shape))
            self.assertEqual((3, 8, 10), tuple(hr.shape))
            self.assertEqual("sample-0", sample_id)

            self._write_dataset(data_root)
            real_capture = runtime.capture_dataset

            def capture_then_poison(root: Path):
                captured = real_capture(root)
                (Path(root) / "train" / "LR" / "x2" / "sample-0x2.png").unlink()
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
                run = runtime.run_local(
                    config=cpu_config,
                    data_root=data_root,
                    output_dir=self.root / "sr-snapshot-output",
                    seed=7,
                    run_id="RUN-0092",
                    config_sha256="2" * 64,
                    dataset_id="snapshot-sr",
                    version="1",
                    source_uri="https://example.org/snapshot-sr",
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
                    data.inspect_png_budget(png_bomb, label="SR", channels=3)

            self._write_dataset(data_root)
            with mock.patch.object(data, "MAX_DATASET_PIXELS", 100):
                with self.assertRaisesRegex(ValueError, "累计|像素"):
                    data.capture_dataset(data_root)
        finally:
            for name in tuple(sys.modules):
                if name == "sr" or name.startswith("sr."):
                    sys.modules.pop(name, None)

    def test_output_review_recounts_bytes_and_checks_png_budget_before_decode(
        self,
    ) -> None:
        repository = self._create_repository()
        contract = self._import_pack(repository, "sr.contract")
        data = self._import_pack(repository, "sr.data")
        validator = self._import_pack(repository, "sr.validate_output")
        try:
            budget_output = self.root / "sr-output-budget"
            budget_output.mkdir()
            records = []
            for name in ("one.bin", "two.bin"):
                content = b"1234"
                (budget_output / name).write_bytes(content)
                records.append(
                    {
                        "path": name,
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
            (budget_output / "artifacts.json").write_text(
                json.dumps(
                    {
                        "schema": "pack-sr.artifacts.v1",
                        "artifacts": records,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(contract, "MAX_TOTAL_SIZE", 7):
                with self.assertRaisesRegex(ValueError, "总体积|总.*字节|上限"):
                    contract.validate_artifacts(budget_output)

            output = self.root / "sr-output-png"
            images = output / "predictions" / "images"
            images.mkdir(parents=True)
            image_path = images / "0000.png"
            bomb = (
                b"\x89PNG\r\n\x1a\n"
                + struct.pack(">I", 13)
                + b"IHDR"
                + struct.pack(">IIBBBBB", 2048, 2048, 8, 2, 0, 0, 0)
                + b"\x00\x00\x00\x00"
            )
            image_path.write_bytes(bomb)
            (output / "predictions" / "index.json").write_text(
                json.dumps(
                    {
                        "schema": "pack-sr.predictions.v1",
                        "predictions": [
                            {
                                "input": "synthetic://0000",
                                "image": "images/0000.png",
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
                    validator._validate_predictions(output, 1)
            self.assertFalse(
                pillow_called,
                "篡改输出的 PNG 预算必须在 Pillow 解码前被拒绝",
            )

            valid = np.zeros((2, 2, 3), dtype=np.uint8)
            Image.fromarray(valid, mode="RGB").save(image_path)
            content = image_path.read_bytes()
            index = read_json(output / "predictions" / "index.json")
            index["predictions"][0].update(
                {
                    "width": 2,
                    "height": 2,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
            (output / "predictions" / "index.json").write_text(
                json.dumps(index),
                encoding="utf-8",
            )
            with mock.patch.object(data, "MAX_DATASET_PIXELS", 1):
                with self.assertRaisesRegex(ValueError, "累计|像素"):
                    validator._validate_predictions(output, 1)
        finally:
            for name in tuple(sys.modules):
                if name == "sr" or name.startswith("sr."):
                    sys.modules.pop(name, None)

    def test_artifact_manifest_bytes_are_counted_in_total_budget(
        self,
    ) -> None:
        repository = self._create_repository()
        contract = self._import_pack(repository, "sr.contract")
        output = self.root / "sr-output-manifest-budget"
        output.mkdir()
        content = b"x" * 512
        (output / "artifact.bin").write_bytes(content)
        manifest = (
            json.dumps(
                {
                    "schema": "pack-sr.artifacts.v1",
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
            with mock.patch.object(
                contract,
                "MAX_TOTAL_SIZE",
                len(content),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "总体积|总.*字节|上限",
                ):
                    contract.validate_artifacts(output)
            writer_output = self.root / "sr-writer-manifest-budget"
            writer_output.mkdir()
            (writer_output / "artifact.bin").write_bytes(content)
            with mock.patch.object(
                contract,
                "MAX_TOTAL_SIZE",
                len(content),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "总体积|总.*字节|上限",
                ):
                    contract.write_artifacts(writer_output)
        finally:
            for name in tuple(sys.modules):
                if name == "sr" or name.startswith("sr."):
                    sys.modules.pop(name, None)

    def test_contract_rejects_manifest_growth_after_initial_read(self) -> None:
        repository = self._create_repository()
        contract = self._import_pack(repository, "sr.contract")
        output = self.root / "sr-contract-late-manifest-growth"
        output.mkdir()
        content = b"artifact"
        artifact = output / "artifact.bin"
        artifact.write_bytes(content)
        manifest_path = output / "artifacts.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema": "pack-sr.artifacts.v1",
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
                    contract.validate_artifacts(output)
            self.assertTrue(manifest_grew)
        finally:
            for name in tuple(sys.modules):
                if name == "sr" or name.startswith("sr."):
                    sys.modules.pop(name, None)

    def test_writer_rejects_artifact_growth_after_initial_read(self) -> None:
        repository = self._create_repository()
        contract = self._import_pack(repository, "sr.contract")
        output = self.root / "sr-writer-late-artifact-growth"
        output.mkdir()
        artifact = output / "artifact.bin"
        artifact.write_bytes(b"artifact")
        original_read = contract.secure_read
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
                "secure_read",
                side_effect=grow_artifact_after_read,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "变化|修改|替换|漂移|一致|SHA-256|大小|总体积",
                ):
                    contract.write_artifacts(output)
            self.assertTrue(artifact_grew)
        finally:
            for name in tuple(sys.modules):
                if name == "sr" or name.startswith("sr."):
                    sys.modules.pop(name, None)

    def test_adapter_synthetic_run_has_only_debug_metric_and_real_png_outputs(self) -> None:
        repository = self._create_repository()
        adapter = self._load_adapter(repository)
        variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": {"mode": "synthetic_smoke"}, "seed": 19}},
        )[0]
        self.assertEqual(EXPECTED_VARIANT_FIELDS, set(variant))
        self.assertIn("evaluation", variant["data"])
        self.assertNotIn("run_kind", variant["data"])
        run = {"id": "RUN-0001", "purpose": "debug", "frozen": variant}
        adapter.execute(repository, "start", run)
        parsed = adapter.parse_result(repository, run)
        self.assertEqual({"debug_psnr_rgb_x2"}, set(parsed["result"]["metrics"]))
        self.assertEqual(
            "custom_full_rgb_no_shave",
            parsed["result"].get("protocol"),
        )
        self.assertIs(
            False,
            parsed["result"].get("comparable_to_standard"),
        )
        output = repository / ".cv-workflow-output" / "RUN-0001"
        record = read_json(output / "run.json")
        self.assertEqual("synthetic_debug_only", record["run_kind"])
        self.assertFalse(record["paper_eligible"])
        self.assertGreater(record["optimizer_steps"], 0)
        self.assertTrue(record["checkpoint_reloaded_for_evaluation"])
        self.assertTrue(record["checkpoint_reloaded_for_inference"])
        artifacts = read_json(output / "artifacts.json")["artifacts"]
        by_path = {item["path"]: item for item in artifacts}
        for required in (
            "checkpoint.pt",
            "evaluation.json",
            "predictions/index.json",
            "raw.log",
            "run.json",
        ):
            self.assertIn(required, by_path)
        for relative, item in by_path.items():
            content = (output / relative).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])
        images = sorted((output / "predictions" / "images").glob("*.png"))
        self.assertTrue(images)
        with Image.open(images[0]) as image:
            self.assertEqual("RGB", image.mode)
            self.assertGreater(image.width * image.height, 0)

    def test_formal_run_uses_exact_identity_cumulative_psnr_and_fails_closed(self) -> None:
        repository = self._create_repository()
        data_root = self.root / "sr-data"
        self._write_dataset(data_root)
        manifest_sha256 = self._manifest_sha256(repository, data_root)
        adapter = self._load_adapter(repository)
        config = {
            "mode": "local_sr_x2",
            "data_root": str(data_root),
            "dataset_id": "tiny-sr",
            "version": "2026.1",
            "source_uri": "https://example.org/datasets/tiny-sr",
            "manifest_sha256": manifest_sha256,
            "device": "cpu",
        }
        variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": config, "seed": 23}},
        )[0]
        self.assertEqual(
            {"kind", "dataset_identity", "evaluation"},
            set(variant["data"]),
        )
        self.assertEqual("local_dataset", variant["data"]["kind"])
        self.assertEqual(
            DATASET_IDENTITY_FIELDS,
            set(variant["data"]["dataset_identity"]),
        )
        self.assertEqual("train+val", variant["data"]["dataset_identity"]["split"])
        run = {"id": "RUN-0002", "purpose": "evidence", "frozen": variant}
        adapter.execute(repository, "start", run)
        parsed = adapter.parse_result(repository, run)
        self.assertEqual({"psnr_rgb_x2"}, set(parsed["result"]["metrics"]))
        output = repository / ".cv-workflow-output" / "RUN-0002"
        evaluation = read_json(output / "evaluation.json")
        self.assertEqual("full_rgb_uint8", evaluation["definition"]["color_space"])
        self.assertEqual("cumulative_sse_over_all_values", evaluation["definition"]["aggregation"])
        self.assertFalse(evaluation["definition"]["shave_border"])
        self.assertEqual(
            "custom_full_rgb_no_shave",
            evaluation["definition"]["protocol"],
        )
        self.assertEqual(
            "not_comparable_to_standard_y_channel_border_shave_without_recalculation",
            evaluation["definition"]["comparability"],
        )
        self.assertGreater(evaluation["sse"], 0)
        self.assertGreater(evaluation["value_count"], 0)

        (output / "evaluation.json").write_text(
            json.dumps({**evaluation, "sse": 0}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "SHA-256|哈希|sse"):
            adapter.parse_result(repository, run)

    def test_formal_device_is_frozen_and_cuda_unavailable_writes_nothing(
        self,
    ) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "sr-v1.0.0"
            / "payload"
        )
        repository = self.root / "sr-gpu-contract"
        shutil.copytree(source, repository)
        data_root = self.root / "sr-gpu-data"
        self._write_dataset(data_root)
        manifest_sha256 = self._manifest_sha256(repository, data_root)
        adapter = self._load_adapter(repository)
        config = {
            "mode": "local_sr_x2",
            "data_root": str(data_root),
            "dataset_id": "tiny-sr",
            "version": "1",
            "source_uri": "https://example.org/tiny-sr",
            "manifest_sha256": manifest_sha256,
            "device": "cuda",
        }
        variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": config, "seed": 51}},
        )[0]
        self.assertEqual("cuda", variant["config"]["device"])
        self.assertEqual(
            {"backend": "project", "device": "cuda"},
            variant["environment"],
        )
        run = {"id": "RUN-0051", "purpose": "evidence", "frozen": variant}
        import torch

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
            {"route_inputs": {"config": config, "seed": 51}},
        )[0]
        cpu_run = {
            "id": "RUN-0053",
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
                / "RUN-0053"
                / "run.json"
            )["device"],
        )

    def test_formal_entrypoints_use_selected_device_and_safe_checkpoint_loading(
        self,
    ) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "sr-v1.0.0"
            / "payload"
        )
        repository = self.root / "sr-device-entrypoints"
        shutil.copytree(source, repository)
        data_root = self.root / "sr-device-data"
        self._write_dataset(data_root)
        manifest_sha256 = self._manifest_sha256(repository, data_root)

        sys.path.insert(0, str(repository))
        try:
            import torch
            from sr.contract import load_checkpoint
            from sr.evaluate import run_evaluation
            from sr.infer import run_inference
            from sr.train import run_training

            cuda_config = read_json(repository / "configs" / "baseline.json")
            cuda_config["device"] = "cuda"
            cuda_config_path = self.root / "sr-cuda-config.json"
            cuda_config_path.write_text(
                json.dumps(cuda_config, ensure_ascii=False),
                encoding="utf-8",
            )
            cuda_training_output = self.root / "sr-cuda-training"
            with mock.patch.object(torch.cuda, "is_available", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "CUDA|cuda|GPU"):
                    run_training(
                        cuda_config_path,
                        data_root,
                        cuda_training_output,
                        dataset_id="tiny-sr",
                        version="1",
                        source_uri="https://example.org/tiny-sr",
                        manifest_sha256=manifest_sha256,
                        run_id="RUN-0054",
                        config_sha256="8" * 64,
                    )
            self.assertFalse(cuda_training_output.exists())

            incompatible_output = self.root / "sr-incompatible-cuda"
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
                        dataset_id="tiny-sr",
                        version="1",
                        source_uri="https://example.org/tiny-sr",
                        manifest_sha256=manifest_sha256,
                        run_id="RUN-0055",
                        config_sha256="b" * 64,
                    )
            self.assertFalse(incompatible_output.exists())

            training_output = self.root / "sr-device-training"
            run = run_training(
                repository / "configs" / "baseline.json",
                data_root,
                training_output,
                dataset_id="tiny-sr",
                version="1",
                source_uri="https://example.org/tiny-sr",
                manifest_sha256=manifest_sha256,
                run_id="RUN-0052",
                config_sha256="5" * 64,
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

            evaluation_output = self.root / "sr-cuda-evaluation"
            inference_output = self.root / "sr-cuda-inference"
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
                if name == "sr" or name.startswith("sr."):
                    sys.modules.pop(name, None)
            sys.path.remove(str(repository))

    def test_windows_cuda_environment_and_instructions_are_explicit(self) -> None:
        payload = (
            Path(__file__).resolve().parents[1]
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "sr-v1.0.0"
            / "payload"
        )
        adapter = self._load_adapter(payload)
        with mock.patch.dict(
            os.environ,
            {
                "CUDA_VISIBLE_DEVICES": "4",
                "CUBLAS_WORKSPACE_CONFIG": "wrong",
            },
            clear=False,
        ):
            environment = adapter._environment(23)
        self.assertEqual("4", environment["CUDA_VISIBLE_DEVICES"])
        self.assertEqual(":4096:8", environment["CUBLAS_WORKSPACE_CONFIG"])

        readme = (payload / "README.md").read_text(encoding="utf-8")
        self.assertIn("https://pytorch.org/get-started/locally/", readme)
        self.assertIn("python -m sr.train", readme)
        self.assertIn("--device cuda", readme)
        self.assertIn("CUDA_VISIBLE_DEVICES", readme)
        self.assertIn("CUBLAS_WORKSPACE_CONFIG=:4096:8", readme)
        self.assertIn("synthetic", readme)
        cpu_lock = (
            payload / "requirements" / "windows-cpu.lock.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("https://download.pytorch.org/whl/cpu", cpu_lock)

    def test_local_source_uri_manifest_and_identity_conflicts_are_rejected(self) -> None:
        repository = self._create_repository()
        data_root = self.root / "sr-data"
        self._write_dataset(data_root)
        manifest_sha256 = self._manifest_sha256(repository, data_root)
        adapter = self._load_adapter(repository)
        config = {
            "mode": "local_sr_x2",
            "data_root": str(data_root),
            "dataset_id": "tiny-sr",
            "version": "1",
            "source_uri": str(data_root),
            "manifest_sha256": manifest_sha256,
            "device": "cpu",
        }
        with self.assertRaisesRegex(ValueError, "source_uri"):
            adapter.prepare_runs(
                repository,
                {"route_inputs": {"config": config, "seed": 1}},
            )
        config["source_uri"] = "https://example.org/tiny-sr"
        config["manifest_sha256"] = "0" * 64
        variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": config, "seed": 1}},
        )[0]
        with self.assertRaisesRegex(RuntimeError, "manifest|哈希"):
            adapter.execute(
                repository,
                "start",
                {"id": "RUN-0003", "purpose": "evidence", "frozen": variant},
            )
        variant["data"]["dataset_identity"]["version"] = "conflict"
        with self.assertRaisesRegex(ValueError, "冲突|身份"):
            adapter.execute(
                repository,
                "start",
                {"id": "RUN-0004", "purpose": "evidence", "frozen": variant},
            )


if __name__ == "__main__":
    unittest.main()
