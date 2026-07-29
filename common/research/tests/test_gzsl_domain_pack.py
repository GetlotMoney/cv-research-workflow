from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from tests._helpers import SCRIPTS, cli_json, read_json


sys.path.insert(0, str(SCRIPTS))

from workflow_core.domain_packs import (  # noqa: E402
    discover_domain_packs,
    resolve_domain_pack,
    validate_domain_pack,
)
from workflow_core.project import init_project  # noqa: E402


EXPECTED_SOURCE = {
    "name": "CADA-VAE-PyTorch",
    "url": "https://github.com/edgarschnfld/CADA-VAE-PyTorch",
    "revision": "26f0085fe5e5911dc06fe767f90965c47885dee1",
    "license": "MIT",
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
NPZ_KEYS = {
    "features",
    "labels",
    "attributes",
    "class_ids",
    "seen_class_ids",
    "unseen_class_ids",
    "train_indices",
    "test_seen_indices",
    "test_unseen_indices",
}
GZSL_FORMAL_METRIC_DEFINITION = {
    "S": (
        "Class-average top-1 accuracy on seen test classes, with predictions "
        "chosen from the union of seen and unseen classes; range 0 to 1, "
        "higher is better."
    ),
    "U": (
        "Class-average top-1 accuracy on unseen test classes, with predictions "
        "chosen from the union of seen and unseen classes; range 0 to 1, "
        "higher is better."
    ),
    "H": (
        "Harmonic mean 2*S*U/(S+U), defined as 0 when S+U=0; range 0 to 1, "
        "higher is better."
    ),
}


class GzslDomainPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _create_repository(self) -> Path:
        project = self.root / "workflow"
        repository = self.root / "gzsl-repository"
        init_project(project, "gzsl-tests", layout="v2")
        created = cli_json(
            "create-domain-repo",
            "--project",
            project,
            "--pack",
            "gzsl",
            "--destination",
            repository,
            "--name",
            "generalized-zero-shot",
        )
        self.assertEqual("PACK-GZSL", created["repository"]["template_id"])
        return repository

    @staticmethod
    def _load_adapter(repository: Path) -> types.ModuleType:
        path = repository / "workflow_adapter.py"
        module = types.ModuleType("_gzsl_workflow_adapter")
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
    def _arrays() -> dict[str, np.ndarray]:
        class_ids = np.array([10, 20, 30, 40], dtype=np.int64)
        seen = np.array([10, 20], dtype=np.int64)
        unseen = np.array([30, 40], dtype=np.int64)
        attributes = np.array(
            [
                [1.0, 0.0, 0.1],
                [0.0, 1.0, 0.1],
                [0.7, 0.7, 0.2],
                [-0.7, 0.7, 0.2],
            ],
            dtype=np.float32,
        )
        labels = np.repeat(class_ids, 3)
        features = np.vstack(
            [
                attributes[index]
                + np.array([sample * 0.01, -sample * 0.005, 0.0], dtype=np.float32)
                for index in range(4)
                for sample in range(3)
            ]
        ).astype(np.float32)
        return {
            "features": features,
            "labels": labels.astype(np.int64),
            "attributes": attributes,
            "class_ids": class_ids,
            "seen_class_ids": seen,
            "unseen_class_ids": unseen,
            "train_indices": np.array([0, 1, 3, 4], dtype=np.int64),
            "test_seen_indices": np.array([2, 5], dtype=np.int64),
            "test_unseen_indices": np.array(
                [6, 7, 8, 9, 10, 11],
                dtype=np.int64,
            ),
        }

    def _write_npz(
        self,
        path: Path,
        arrays: dict[str, np.ndarray] | None = None,
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, **(self._arrays() if arrays is None else arrays))
        return path

    def _manifest_sha256(self, repository: Path, data_path: Path) -> str:
        module = self._import_pack(repository, "gzsl.data")
        try:
            return module.build_dataset_manifest(data_path)["manifest_sha256"]
        finally:
            for name in tuple(sys.modules):
                if name == "gzsl" or name.startswith("gzsl."):
                    sys.modules.pop(name, None)

    def test_formal_prepare_injects_fixed_metric_definitions(self) -> None:
        repository = self._create_repository()
        data_path = self._write_npz(self.root / "metric-definition.npz")
        manifest_sha256 = self._manifest_sha256(repository, data_path)
        adapter = self._load_adapter(repository)
        config = {
            "mode": "local_gzsl_npz",
            "device": "cuda",
            "data_path": str(data_path),
            "dataset_id": "tiny-gzsl-metric",
            "version": "2026.07",
            "source_uri": "https://example.org/datasets/tiny-gzsl-metric",
            "manifest_sha256": manifest_sha256,
        }
        variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": config, "seed": 31}},
        )[0]
        self.assertNotIn("metric_definition", config)
        self.assertIn("metric_definition", variant["config"])
        self.assertEqual(
            GZSL_FORMAL_METRIC_DEFINITION,
            variant["config"]["metric_definition"],
        )

        user_override = dict(config)
        user_override["metric_definition"] = dict(
            GZSL_FORMAL_METRIC_DEFINITION
        )
        with self.assertRaisesRegex(ValueError, "config|字段|metric_definition"):
            adapter.prepare_runs(
                repository,
                {"route_inputs": {"config": user_override, "seed": 31}},
            )

        for mutation in ("missing", "changed", "extra"):
            with self.subTest(mutation=mutation):
                tampered = copy.deepcopy(variant)
                definitions = tampered["config"]["metric_definition"]
                if mutation == "missing":
                    definitions.pop("S")
                elif mutation == "changed":
                    definitions["S"] = "user-defined score"
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

    def test_pack_source_download_note_and_adapter_are_exact(self) -> None:
        listed = {item["id"]: item for item in discover_domain_packs()}
        self.assertIn("gzsl", listed)
        pack_root = resolve_domain_pack("gzsl@1.1.1")
        manifest = validate_domain_pack(pack_root)
        self.assertEqual([EXPECTED_SOURCE], manifest["source_references"])
        payload = pack_root / "payload"
        contract = read_json(payload / "domain-pack.json")
        self.assertEqual("PACK-GZSL", contract["template_id"])
        self.assertEqual(sorted(NPZ_KEYS), sorted(contract["dataset"]["npz_keys"]))
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
        awa2 = [
            item
            for item in downloads["optional_resources"]
            if item["name"] == "AwA2"
        ][0]
        self.assertIsInstance(awa2["license"], str)
        self.assertTrue(awa2["license"])
        self.assertEqual("https://cvml.ista.ac.at/AwA2/", awa2["license_url"])
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in payload.rglob("*.py")
        )
        self.assertNotIn("from sr", source)
        self.assertNotIn("from shared", source)
        self.assertNotIn("calibrated_stacking", source)
        adapter = self._load_adapter(payload)
        public = {
            name
            for name, value in vars(adapter).items()
            if not name.startswith("_") and callable(value)
        }
        self.assertEqual(
            {"inspect", "validate", "prepare_runs", "execute", "parse_result"},
            public,
        )

    def test_npz_schema_dtypes_splits_and_attribute_row_mapping_are_strict(self) -> None:
        repository = self._create_repository()
        data = self._import_pack(repository, "gzsl.data")
        try:
            path = self._write_npz(self.root / "valid.npz")
            dataset = data.load_gzsl_npz(path)
            self.assertEqual((12, 3), dataset["features"].shape)
            self.assertEqual(
                {10: 0, 20: 1, 30: 2, 40: 3},
                dataset["class_to_attribute_row"],
            )
            self.assertEqual(
                "class_ids[index] maps to attributes[index]",
                dataset["attribute_row_mapping"],
            )

            arrays = self._arrays()
            arrays["features"] = arrays["features"].copy()
            arrays["features"][0, 0] = np.nan
            with self.assertRaisesRegex(ValueError, "有限|NaN"):
                data.load_gzsl_npz(self._write_npz(self.root / "nan.npz", arrays))

            arrays = self._arrays()
            arrays["class_ids"] = np.array([10, 20, 20, 40], dtype=np.int64)
            with self.assertRaisesRegex(ValueError, "重复|唯一"):
                data.load_gzsl_npz(
                    self._write_npz(self.root / "duplicate.npz", arrays)
                )

            arrays = self._arrays()
            arrays["train_indices"] = np.array([0, 6], dtype=np.int64)
            with self.assertRaisesRegex(ValueError, "train.*seen|训练"):
                data.load_gzsl_npz(
                    self._write_npz(self.root / "unseen-train.npz", arrays)
                )

            arrays = self._arrays()
            arrays["features"] = np.array([object()], dtype=object)
            with self.assertRaisesRegex(ValueError, "object|pickle|dtype"):
                data.load_gzsl_npz(
                    self._write_npz(self.root / "object.npz", arrays)
                )
        finally:
            for name in tuple(sys.modules):
                if name == "gzsl" or name.startswith("gzsl."):
                    sys.modules.pop(name, None)

    def test_snapshot_is_consumed_and_npz_bombs_fail_before_numpy_load(self) -> None:
        repository = self._create_repository()
        data = self._import_pack(repository, "gzsl.data")
        runtime = self._import_pack(repository, "gzsl.runtime")
        try:
            path = self._write_npz(self.root / "snapshot.npz")
            manifest, snapshot = data.capture_dataset(path)
            original_digest = manifest["manifest_sha256"]
            path.write_bytes(b"changed-after-manifest")
            loaded = data.load_gzsl_npz_bytes(snapshot)
            self.assertEqual((12, 3), loaded["features"].shape)

            self._write_npz(path)
            real_capture = runtime.capture_dataset

            def capture_then_poison(source: Path):
                captured = real_capture(source)
                Path(source).write_bytes(b"poisoned-after-capture")
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
                    data_path=path,
                    output_dir=self.root / "gzsl-snapshot-output",
                    seed=7,
                    run_id="RUN-0093",
                    config_sha256="3" * 64,
                    dataset_id="snapshot-gzsl",
                    version="1",
                    source_uri="https://example.org/snapshot-gzsl",
                    manifest_sha256=original_digest,
                )
            self.assertGreater(run["optimizer_steps"], 0)

            bomb_arrays = self._arrays()
            bomb_arrays["features"] = np.zeros((200_000, 16), dtype=np.float32)
            bomb = self.root / "compressed-bomb.npz"
            np.savez_compressed(bomb, **bomb_arrays)
            with mock.patch.object(
                np,
                "load",
                side_effect=AssertionError("ZIP/NPY 预算检查必须先于 numpy.load"),
            ):
                with self.assertRaisesRegex(ValueError, "压缩比|展开|NPY"):
                    data.load_gzsl_npz(bomb)

            regular = self._write_npz(self.root / "expanded-limit.npz")
            with mock.patch.object(data, "MAX_NPZ_EXPANDED_BYTES", 64):
                with mock.patch.object(
                    np,
                    "load",
                    side_effect=AssertionError("展开体积检查必须先于 numpy.load"),
                ):
                    with self.assertRaisesRegex(ValueError, "展开|体积"):
                        data.load_gzsl_npz(regular)
        finally:
            for name in tuple(sys.modules):
                if name == "gzsl" or name.startswith("gzsl."):
                    sys.modules.pop(name, None)

    def test_run_local_defaults_to_cuda(self) -> None:
        repository = self.root / "gzsl-legacy-call-repo"
        shutil.copytree(
            Path(__file__).resolve().parents[1]
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "gzsl-v1.1.1"
            / "payload",
            repository,
        )
        data_path = self._write_npz(self.root / "legacy-call.npz")
        manifest_sha256 = self._manifest_sha256(repository, data_path)
        runtime = self._import_pack(repository, "gzsl.runtime")
        try:
            cuda_config = read_json(repository / "configs" / "baseline.json")
            cuda_config["device"] = "cuda"
            run = runtime.run_local(
                config=cuda_config,
                data_path=data_path,
                output_dir=self.root / "gzsl-legacy-call-output",
                seed=7,
                run_id="RUN-0094",
                config_sha256="4" * 64,
                dataset_id="legacy-gzsl",
                version="1",
                source_uri="https://example.org/legacy-gzsl",
                manifest_sha256=manifest_sha256,
            )
            self.assertEqual("cuda", run["device"])
            self.assertGreater(run["optimizer_steps"], 0)
        finally:
            for name in tuple(sys.modules):
                if name == "gzsl" or name.startswith("gzsl."):
                    sys.modules.pop(name, None)

    def test_output_review_recounts_total_artifact_bytes(self) -> None:
        repository = self._create_repository()
        contract = self._import_pack(repository, "gzsl.contract")
        output = self.root / "gzsl-output-budget"
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
                    "schema": "pack-gzsl.artifacts.v1",
                    "artifacts": records,
                }
            ),
            encoding="utf-8",
        )
        try:
            with mock.patch.object(contract, "MAX_TOTAL_SIZE", 7):
                with self.assertRaisesRegex(ValueError, "总体积|总.*字节|上限"):
                    contract.validate_artifacts(output)
        finally:
            for name in tuple(sys.modules):
                if name == "gzsl" or name.startswith("gzsl."):
                    sys.modules.pop(name, None)

    def test_artifact_manifest_bytes_are_counted_in_total_budget(
        self,
    ) -> None:
        repository = self._create_repository()
        contract = self._import_pack(repository, "gzsl.contract")
        output = self.root / "gzsl-output-manifest-budget"
        output.mkdir()
        content = b"x" * 512
        (output / "artifact.bin").write_bytes(content)
        manifest = (
            json.dumps(
                {
                    "schema": "pack-gzsl.artifacts.v1",
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
            writer_output = self.root / "gzsl-writer-manifest-budget"
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
                if name == "gzsl" or name.startswith("gzsl."):
                    sys.modules.pop(name, None)

    def test_contract_rejects_manifest_growth_after_initial_read(self) -> None:
        repository = self._create_repository()
        contract = self._import_pack(repository, "gzsl.contract")
        output = self.root / "gzsl-contract-late-manifest-growth"
        output.mkdir()
        content = b"artifact"
        artifact = output / "artifact.bin"
        artifact.write_bytes(content)
        manifest_path = output / "artifacts.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema": "pack-gzsl.artifacts.v1",
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
                if name == "gzsl" or name.startswith("gzsl."):
                    sys.modules.pop(name, None)

    def test_writer_rejects_artifact_growth_after_initial_read(self) -> None:
        repository = self._create_repository()
        contract = self._import_pack(repository, "gzsl.contract")
        output = self.root / "gzsl-writer-late-artifact-growth"
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
                if name == "gzsl" or name.startswith("gzsl."):
                    sys.modules.pop(name, None)

    def test_class_average_s_u_h_and_zero_denominator_are_explicit(self) -> None:
        repository = self._create_repository()
        metrics = self._import_pack(repository, "gzsl.metrics")
        try:
            targets = np.array([10, 10, 20, 20, 30, 30, 40, 40], dtype=np.int64)
            predictions = np.array([10, 20, 20, 20, 30, 10, 40, 10], dtype=np.int64)
            result = metrics.gzsl_class_average_metrics(
                predictions,
                targets,
                np.array([10, 20], dtype=np.int64),
                np.array([30, 40], dtype=np.int64),
            )
            expected_s = (0.5 + 1.0) / 2
            expected_u = (0.5 + 0.5) / 2
            expected_h = 2 * expected_s * expected_u / (expected_s + expected_u)
            self.assertEqual({"S", "U", "H"}, set(result))
            self.assertAlmostEqual(expected_s, result["S"])
            self.assertAlmostEqual(expected_u, result["U"])
            self.assertAlmostEqual(expected_h, result["H"])

            all_wrong = np.array([20, 20, 10, 10, 40, 40, 30, 30], dtype=np.int64)
            zero = metrics.gzsl_class_average_metrics(
                all_wrong,
                targets,
                np.array([10, 20], dtype=np.int64),
                np.array([30, 40], dtype=np.int64),
            )
            self.assertEqual({"S": 0.0, "U": 0.0, "H": 0.0}, zero)
        finally:
            for name in tuple(sys.modules):
                if name == "gzsl" or name.startswith("gzsl."):
                    sys.modules.pop(name, None)

    def test_adapter_synthetic_run_uses_only_debug_metric_names(self) -> None:
        repository = self._create_repository()
        adapter = self._load_adapter(repository)
        variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": {"mode": "synthetic_smoke"}, "seed": 29}},
        )[0]
        self.assertEqual(EXPECTED_VARIANT_FIELDS, set(variant))
        self.assertIn("evaluation", variant["data"])
        self.assertNotIn("run_kind", variant["data"])
        run = {"id": "RUN-0001", "purpose": "debug", "frozen": variant}
        adapter.execute(repository, "start", run)
        parsed = adapter.parse_result(repository, run)
        self.assertEqual(
            {
                "debug_seen_class_average_accuracy",
                "debug_unseen_class_average_accuracy",
                "debug_harmonic_mean",
            },
            set(parsed["result"]["metrics"]),
        )
        output = repository / ".cv-workflow-output" / "RUN-0001"
        record = read_json(output / "run.json")
        self.assertEqual("synthetic_debug_only", record["run_kind"])
        self.assertFalse(record["paper_eligible"])
        self.assertGreater(record["optimizer_steps"], 0)
        self.assertTrue(record["checkpoint_reloaded_for_evaluation"])
        self.assertTrue(record["checkpoint_reloaded_for_inference"])
        artifacts = read_json(output / "artifacts.json")["artifacts"]
        for item in artifacts:
            content = (output / item["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])

    def test_device_contract_is_gpu_only_and_cuda_fails_before_outputs(
        self,
    ) -> None:
        repository = self.root / "gzsl-device-repo"
        shutil.copytree(
            Path(__file__).resolve().parents[1]
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "gzsl-v1.1.1"
            / "payload",
            repository,
        )
        data_path = self._write_npz(self.root / "device-formal.npz")
        manifest_sha256 = self._manifest_sha256(repository, data_path)
        adapter = self._load_adapter(repository)
        synthetic = adapter.prepare_runs(
            repository,
            {
                "route_inputs": {
                    "config": {"mode": "synthetic_smoke"},
                    "seed": 29,
                },
            },
        )[0]
        self.assertEqual(
            {"backend": "project", "device": "cuda"},
            synthetic["environment"],
        )
        with self.assertRaisesRegex(ValueError, "config|字段|device"):
            adapter.prepare_runs(
                repository,
                {
                    "route_inputs": {
                        "config": {
                            "mode": "synthetic_smoke",
                            "device": "cpu",
                        },
                    },
                },
            )

        config = {
            "mode": "local_gzsl_npz",
            "device": "cuda",
            "data_path": str(data_path),
            "dataset_id": "tiny-gzsl-device",
            "version": "2026.1",
            "source_uri": "https://example.org/datasets/tiny-gzsl-device",
            "manifest_sha256": manifest_sha256,
        }
        variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": config, "seed": 31}},
        )[0]
        self.assertEqual("cuda", variant["config"]["device"])
        self.assertEqual(
            {"backend": "project", "device": "cuda"},
            variant["environment"],
        )
        import torch

        with mock.patch.object(torch.cuda, "is_available", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "CUDA|cuda"):
                adapter.execute(
                    repository,
                    "start",
                    {
                        "id": "RUN-0090",
                        "purpose": "evidence",
                        "frozen": variant,
                    },
                )
        self.assertFalse((repository / ".cv-workflow-output").exists())
        for name in tuple(sys.modules):
            if name == "gzsl" or name.startswith("gzsl."):
                sys.modules.pop(name, None)

    def test_cuda_probe_failure_stops_before_staging(self) -> None:
        repository = self.root / "gzsl-cuda-probe-repo"
        shutil.copytree(
            Path(__file__).resolve().parents[1]
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "gzsl-v1.1.1"
            / "payload",
            repository,
        )
        data_path = self._write_npz(self.root / "gzsl-probe-data.npz")
        manifest_sha256 = self._manifest_sha256(repository, data_path)
        adapter = self._load_adapter(repository)
        variant = adapter.prepare_runs(
            repository,
            {
                "route_inputs": {
                    "config": {
                        "mode": "local_gzsl_npz",
                        "device": "cuda",
                        "data_path": str(data_path),
                        "dataset_id": "tiny-gzsl-probe",
                        "version": "2026.1",
                        "source_uri": "https://example.org/gzsl-probe",
                        "manifest_sha256": manifest_sha256,
                    },
                    "seed": 31,
                },
            },
        )[0]
        import torch

        with (
            mock.patch.object(torch.cuda, "is_available", return_value=True),
            mock.patch.object(
                adapter,
                "_probe_cuda",
                create=True,
                side_effect=RuntimeError("CUDA probe no kernel image"),
            ) as probe,
        ):
            with self.assertRaisesRegex(RuntimeError, "CUDA probe"):
                adapter.execute(
                    repository,
                    "start",
                    {
                        "id": "RUN-0092",
                        "purpose": "evidence",
                        "frozen": variant,
                    },
                )
        probe.assert_called_once_with(repository, "cuda", seed=31)
        self.assertFalse((repository / ".cv-workflow-output").exists())

    def test_cuda_probe_uses_real_mm_and_preserves_cuda_environment(self) -> None:
        repository = self.root / "gzsl-cuda-env-repo"
        shutil.copytree(
            Path(__file__).resolve().parents[1]
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "gzsl-v1.1.1"
            / "payload",
            repository,
        )
        adapter = self._load_adapter(repository)
        failed = types.SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="no kernel image is available",
        )
        with (
            mock.patch.dict(
                os.environ,
                {"CUDA_VISIBLE_DEVICES": "1"},
                clear=False,
            ),
            mock.patch.object(
                adapter._subprocess,
                "run",
                return_value=failed,
            ) as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "CUDA|cuda"):
                adapter._probe_cuda(repository, "cuda", seed=31)
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(["-m", "gzsl.cuda_probe"], command[-2:])
        self.assertEqual(repository, run.call_args.kwargs["cwd"])
        self.assertEqual("1", environment["CUDA_VISIBLE_DEVICES"])
        self.assertEqual(":4096:8", environment["CUBLAS_WORKSPACE_CONFIG"])

    def test_formal_npz_run_reloads_linear_model_and_reports_s_u_h(self) -> None:
        repository = self._create_repository()
        data_path = self._write_npz(self.root / "formal.npz")
        manifest_sha256 = self._manifest_sha256(repository, data_path)
        adapter = self._load_adapter(repository)
        config = {
            "mode": "local_gzsl_npz",
            "data_path": str(data_path),
            "dataset_id": "tiny-gzsl",
            "version": "2026.1",
            "source_uri": "https://example.org/datasets/tiny-gzsl",
            "manifest_sha256": manifest_sha256,
            "device": "cuda",
        }
        variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": config, "seed": 31}},
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
        self.assertEqual(
            "train_indices+test_seen_indices+test_unseen_indices",
            variant["data"]["dataset_identity"]["split"],
        )
        run = {"id": "RUN-0002", "purpose": "evidence", "frozen": variant}
        adapter.execute(repository, "start", run)
        parsed = adapter.parse_result(repository, run)
        self.assertEqual({"S", "U", "H"}, set(parsed["result"]["metrics"]))
        self.assertTrue(
            all(
                math.isfinite(value) and 0.0 <= value <= 1.0
                for value in parsed["result"]["metrics"].values()
            )
        )
        output = repository / ".cv-workflow-output" / "RUN-0002"
        evaluation = read_json(output / "evaluation.json")
        self.assertEqual("class_average", evaluation["definition"]["accuracy_averaging"])
        self.assertEqual("2*S*U/(S+U); 0 when S+U=0", evaluation["definition"]["harmonic_mean"])
        predictions = read_json(output / "predictions.json")["predictions"]
        self.assertEqual(8, len(predictions))
        self.assertTrue(
            all(item["predicted_class_id"] in {10, 20, 30, 40} for item in predictions)
        )

        checkpoint = output / "checkpoint.pt"
        checkpoint.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "checkpoint|SHA-256|哈希"):
            adapter.parse_result(repository, run)

    def test_bad_manifest_local_uri_and_identity_conflict_fail_closed(self) -> None:
        repository = self._create_repository()
        data_path = self._write_npz(self.root / "formal.npz")
        manifest_sha256 = self._manifest_sha256(repository, data_path)
        adapter = self._load_adapter(repository)
        config = {
            "mode": "local_gzsl_npz",
            "data_path": str(data_path),
            "dataset_id": "tiny-gzsl",
            "version": "1",
            "source_uri": str(data_path),
            "manifest_sha256": manifest_sha256,
            "device": "cuda",
        }
        with self.assertRaisesRegex(ValueError, "source_uri"):
            adapter.prepare_runs(
                repository,
                {"route_inputs": {"config": config, "seed": 1}},
            )
        config["source_uri"] = "https://example.org/tiny-gzsl"
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
        variant["data"]["dataset_identity"]["dataset_id"] = "conflict"
        with self.assertRaisesRegex(ValueError, "冲突|身份"):
            adapter.execute(
                repository,
                "start",
                {"id": "RUN-0004", "purpose": "evidence", "frozen": variant},
            )


if __name__ == "__main__":
    unittest.main()
