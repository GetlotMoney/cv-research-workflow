from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from tests._helpers import SCRIPTS, cli_json, read_json


sys.path.insert(0, str(SCRIPTS))

from workflow_core.domain_packs import (  # noqa: E402
    discover_domain_packs,
    resolve_domain_pack,
    validate_domain_pack,
)
from workflow_core.project import init_project  # noqa: E402
from workflow_core.review import _validate_parsed  # noqa: E402
from workflow_core.runs import FROZEN_FIELDS  # noqa: E402


TORCHVISION = {
    "name": "torchvision",
    "url": "https://github.com/pytorch/vision",
    "revision": "78839c2b06c83c6cfb5c4da692ffb331bbd4c4cc",
    "license": "BSD-3-Clause",
}
PYCOCOTOOLS = {
    "name": "pycocotools",
    "url": "https://github.com/ppwwyyxx/cocoapi",
    "revision": "ac87f5077ad6b8864c2dc5e93d14cae62d1db05a",
    "license": "BSD-2-Clause",
}
DET_DEBUG_EVALUATION = {
    "schema": "pack-det.evaluation-contract.v1",
    "backend": "debug_bbox_metrics",
    "iou_type": "bbox",
    "metrics": [
        "debug_bbox_mean_iou",
        "debug_precision_at_iou_0_5",
    ],
}
DET_FORMAL_METRIC_DEFINITION = {
    "coco_bbox_ap": (
        "COCO bounding-box AP averaged over IoU thresholds 0.50:0.05:0.95, "
        "area=all and maxDets=100; range 0 to 1, higher is better."
    ),
    "coco_bbox_ap50": (
        "COCO bounding-box AP at IoU=0.50, area=all and maxDets=100; "
        "range 0 to 1, higher is better."
    ),
    "coco_bbox_ap75": (
        "COCO bounding-box AP at IoU=0.75, area=all and maxDets=100; "
        "range 0 to 1, higher is better."
    ),
}


class DetectionDomainPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _create_repository(self, name: str = "detector") -> Path:
        project = self.root / f"{name}-workflow"
        destination = self.root / f"{name}-repo"
        init_project(project, name, layout="v2")
        created = cli_json(
            "create-domain-repo",
            "--project",
            project,
            "--pack",
            "det",
            "--destination",
            destination,
            "--name",
            name,
        )
        self.assertEqual("PACK-DET", created["repository"]["template_id"])
        self.assertEqual("CB-0001", created["codebase"]["id"])
        return destination

    def _run(
        self,
        repository: Path,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", *arguments],
            cwd=repository,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        if check:
            self.assertEqual(
                0,
                result.returncode,
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
        return result

    @staticmethod
    def _load_python(path: Path, name: str) -> types.ModuleType:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise AssertionError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _adapter_run(
        run_id: str,
        *,
        mode: str = "synthetic_debug",
        purpose: str = "debug",
        config: dict[str, object] | None = None,
        data: dict[str, object] | None = None,
    ) -> dict[str, object]:
        frozen_config: dict[str, object] = {"mode": mode}
        if mode == "local_coco":
            frozen_config["metric_definition"] = dict(
                DET_FORMAL_METRIC_DEFINITION
            )
        if config:
            frozen_config.update(config)
        frozen_data = data or {
            "kind": "synthetic_debug_only",
            "evaluation": DET_DEBUG_EVALUATION,
        }
        return {
            "id": run_id,
            "purpose": purpose,
            "frozen": {
                "code": {"template_id": "PACK-DET", "version": "1.0.0"},
                "config": frozen_config,
                "seed": 17,
                "data": frozen_data,
                "environment": {"backend": "project", "device": "cpu"},
            },
        }

    def _write_coco(self, root: Path) -> tuple[Path, Path]:
        images = root / "images"
        images.mkdir(parents=True)
        Image.new("RGB", (16, 16), (30, 40, 50)).save(images / "one.png")
        Image.new("RGB", (16, 16), (200, 100, 20)).save(images / "two.png")
        annotation = root / "instances.json"
        annotation.write_text(
            json.dumps(
                {
                    "images": [
                        {"id": 1, "file_name": "one.png", "width": 16, "height": 16},
                        {"id": 2, "file_name": "two.png", "width": 16, "height": 16},
                    ],
                    "categories": [
                        {"id": 1, "name": "square"},
                        {"id": 2, "name": "circle"},
                    ],
                    "annotations": [
                        {
                            "id": 1,
                            "image_id": 1,
                            "category_id": 1,
                            "bbox": [2.0, 2.0, 8.0, 7.0],
                            "area": 56.0,
                            "iscrowd": 0,
                        },
                        {
                            "id": 2,
                            "image_id": 2,
                            "category_id": 2,
                            "bbox": [3.0, 4.0, 6.0, 5.0],
                            "area": 30.0,
                            "iscrowd": 0,
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return images, annotation

    def test_formal_prepare_injects_fixed_metric_definitions(self) -> None:
        repository = self._create_repository("det-metric-definition")
        adapter = self._load_python(
            repository / "workflow_adapter.py",
            "_det_metric_definition_adapter",
        )
        images, annotation = self._write_coco(self.root / "det-metric-data")
        config = {
            "mode": "local_coco",
            "device": "cpu",
            "data_root": str(images),
            "annotation_file": str(annotation),
            "dataset_id": "tiny-coco-det-metric",
            "version": "2026.07",
            "source_uri": "https://example.org/datasets/tiny-coco-det-metric",
            "split": "val",
        }
        variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": config, "seed": 17}},
        )[0]
        self.assertNotIn("metric_definition", config)
        self.assertIn("metric_definition", variant["config"])
        self.assertEqual(
            DET_FORMAL_METRIC_DEFINITION,
            variant["config"]["metric_definition"],
        )

        user_override = dict(config)
        user_override["metric_definition"] = dict(
            DET_FORMAL_METRIC_DEFINITION
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
                    definitions.pop("coco_bbox_ap")
                elif mutation == "changed":
                    definitions["coco_bbox_ap"] = "user-defined score"
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

    def test_registry_manifest_and_payload_are_self_contained(self) -> None:
        self.assertEqual(
            ["cls", "det", "gzsl", "instseg", "seg", "sr"],
            [item["id"] for item in discover_domain_packs()],
        )
        pack = resolve_domain_pack("det")
        manifest = validate_domain_pack(pack)
        self.assertEqual("PACK-DET", manifest["template_id"])
        self.assertEqual([TORCHVISION, PYCOCOTOOLS], manifest["source_references"])
        files = {item["path"] for item in manifest["files"]}
        required = {
            ".gitignore",
            "README.md",
            "requirements/windows-cpu.lock.txt",
            "configs/smoke.json",
            "configs/baseline.json",
            "src/__init__.py",
            "src/det/__init__.py",
            "src/det/runtime.py",
            "src/det/train.py",
            "src/det/evaluate.py",
            "src/det/infer.py",
            "src/det/smoke.py",
            "workflow_adapter.py",
            "tests/domain_pack/test_synthetic_smoke.py",
        }
        self.assertTrue(required.issubset(files))
        payload = pack / "payload"
        all_python = "\n".join(
            path.read_text(encoding="utf-8")
            for path in payload.rglob("*.py")
        )
        self.assertNotIn("instseg", all_python.casefold())
        self.assertNotIn("_shared", all_python)
        downloads = read_json(payload / "DOWNLOADS.json")
        self.assertFalse(downloads["automatic_download"])
        self.assertEqual([], downloads["bundled_large_files"])
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
            for field in (
                "name",
                "kind",
                "url",
                "version",
                "revision",
                "license",
                "license_url",
            ):
                self.assertIsInstance(resource[field], str)
                self.assertTrue(resource[field])
        coco = downloads["optional_resources"][0]
        self.assertEqual("2017", coco["version"])
        self.assertEqual("trainval2017", coco["revision"])
        self.assertIn("CC-BY-4.0", coco["license"])

    def test_synthetic_train_evaluate_infer_reload_and_hash_real_outputs(self) -> None:
        repository = self._create_repository()
        train = self.root / "det-train"
        evaluation = self.root / "det-eval"
        inference = self.root / "det-infer"
        self._run(
            repository,
            "-m",
            "src.det.train",
            "--config",
            "configs/smoke.json",
            "--mode",
            "synthetic_debug",
            "--output-dir",
            str(train),
        )
        self._run(
            repository,
            "-m",
            "src.det.evaluate",
            "--checkpoint",
            str(train / "checkpoint.pt"),
            "--mode",
            "synthetic_debug",
            "--output-dir",
            str(evaluation),
        )
        self._run(
            repository,
            "-m",
            "src.det.infer",
            "--checkpoint",
            str(train / "checkpoint.pt"),
            "--mode",
            "synthetic_debug",
            "--output-dir",
            str(inference),
        )
        train_record = read_json(train / "run.json")
        self.assertGreater(train_record["optimizer_steps"], 0)
        self.assertTrue(train_record["checkpoint_reloaded"])
        self.assertEqual("synthetic_debug_only", train_record["run_kind"])
        self.assertFalse(train_record["paper_eligible"])
        metrics = read_json(evaluation / "evaluation.json")["metrics"]
        self.assertEqual(
            {"debug_bbox_mean_iou", "debug_precision_at_iou_0_5"},
            set(metrics),
        )
        for name, value in metrics.items():
            self.assertNotIn("average_precision", name.casefold())
            self.assertNotEqual("ap", name.casefold())
            self.assertTrue(math.isfinite(value))
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)
        predictions = read_json(inference / "predictions.json")
        self.assertGreater(len(predictions["predictions"]), 0)
        for item in predictions["predictions"]:
            image = inference / item["input_path"]
            self.assertEqual(
                item["input_sha256"],
                f"sha256:{hashlib.sha256(image.read_bytes()).hexdigest()}",
            )
        import torch

        checkpoint = torch.load(
            train / "checkpoint.pt",
            map_location="cpu",
            weights_only=True,
        )
        self.assertTrue(checkpoint["model_state_dict"])
        self.assertEqual("det", checkpoint["direction"])

        repeated = self._run(
            repository,
            "-m",
            "src.det.train",
            "--config",
            "configs/smoke.json",
            "--mode",
            "synthetic_debug",
            "--output-dir",
            str(train),
            check=False,
        )
        self.assertNotEqual(0, repeated.returncode)

    def test_device_contract_keeps_synthetic_cpu_and_cuda_fails_before_outputs(
        self,
    ) -> None:
        repository = self.root / "det-device-repo"
        shutil.copytree(
            Path(__file__).resolve().parents[1]
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "det-v1.0.0"
            / "payload",
            repository,
        )
        adapter = self._load_python(
            repository / "workflow_adapter.py",
            "_det_device_adapter",
        )
        runtime = self._load_python(
            repository / "src" / "det" / "runtime.py",
            "_det_device_runtime",
        )
        synthetic = adapter.prepare_runs(
            repository,
            {
                "route_inputs": {
                    "config": {"mode": "synthetic_debug"},
                    "seed": 17,
                },
            },
        )[0]
        self.assertEqual(
            {"backend": "project", "device": "cpu"},
            synthetic["environment"],
        )
        with self.assertRaisesRegex(ValueError, "synthetic|CPU|cpu|device"):
            adapter.prepare_runs(
                repository,
                {
                    "route_inputs": {
                        "config": {
                            "mode": "synthetic_debug",
                            "device": "cuda",
                        },
                    },
                },
            )

        images, annotation = self._write_coco(self.root / "det-device-data")
        formal_config = {
            "mode": "local_coco",
            "device": "cuda",
            "data_root": str(images),
            "annotation_file": str(annotation),
            "dataset_id": "tiny-coco-det-device",
            "version": "2026.07",
            "source_uri": "https://example.org/datasets/tiny-coco-det-device",
            "split": "val",
        }
        variant = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": formal_config, "seed": 17}},
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
            direct_output = self.root / "det-cuda-unavailable"
            with self.assertRaisesRegex(RuntimeError, "CUDA|cuda"):
                runtime.run_training(
                    repository / "configs" / "baseline.json",
                    direct_output,
                    mode="local_coco",
                    data_root=images,
                    annotation_file=annotation,
                    device_override="cuda",
                )
            evaluation_output = self.root / "det-cuda-evaluation"
            inference_output = self.root / "det-cuda-inference"
            for operation, output in (
                (runtime.run_evaluation, evaluation_output),
                (runtime.run_inference, inference_output),
            ):
                with self.subTest(operation=operation.__name__):
                    with self.assertRaisesRegex(RuntimeError, "CUDA|cuda"):
                        operation(
                            self.root / "missing-checkpoint.pt",
                            output,
                            mode="local_coco",
                            data_root=images,
                            annotation_file=annotation,
                            device="cuda",
                        )
        self.assertFalse((repository / ".cv-workflow-output").exists())
        self.assertFalse(direct_output.exists())
        self.assertFalse(evaluation_output.exists())
        self.assertFalse(inference_output.exists())

    def test_cuda_probe_failure_stops_before_staging(self) -> None:
        repository = self.root / "det-cuda-probe-repo"
        shutil.copytree(
            Path(__file__).resolve().parents[1]
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "det-v1.0.0"
            / "payload",
            repository,
        )
        adapter = self._load_python(
            repository / "workflow_adapter.py",
            "_det_cuda_probe_adapter",
        )
        images, annotation = self._write_coco(self.root / "det-probe-data")
        variant = adapter.prepare_runs(
            repository,
            {
                "route_inputs": {
                    "config": {
                        "mode": "local_coco",
                        "device": "cuda",
                        "data_root": str(images),
                        "annotation_file": str(annotation),
                        "dataset_id": "tiny-coco-det-probe",
                        "version": "2026.07",
                        "source_uri": "https://example.org/det-probe",
                        "split": "val",
                    },
                    "seed": 17,
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
        probe.assert_called_once_with("cuda", seed=17)
        self.assertFalse((repository / ".cv-workflow-output").exists())

    def test_cuda_probe_uses_real_mm_and_preserves_cuda_environment(self) -> None:
        repository = self.root / "det-cuda-env-repo"
        shutil.copytree(
            Path(__file__).resolve().parents[1]
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "det-v1.0.0"
            / "payload",
            repository,
        )
        adapter = self._load_python(
            repository / "workflow_adapter.py",
            "_det_cuda_env_adapter",
        )
        failed = types.SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="no kernel image is available",
        )
        with (
            mock.patch.dict(
                os.environ,
                {"CUDA_VISIBLE_DEVICES": "2"},
                clear=False,
            ),
            mock.patch.object(
                adapter._subprocess,
                "run",
                return_value=failed,
            ) as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "CUDA|cuda"):
                adapter._probe_cuda("cuda", seed=17)
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertIn("torch.mm", command[-1])
        self.assertIn("torch.cuda.synchronize", command[-1])
        self.assertEqual("2", environment["CUDA_VISIBLE_DEVICES"])
        self.assertEqual(":4096:8", environment["CUBLAS_WORKSPACE_CONFIG"])

    def test_adapter_contract_is_unique_and_synthetic_never_becomes_evidence(self) -> None:
        repository = self._create_repository("det-adapter")
        adapter = self._load_python(
            repository / "workflow_adapter.py",
            "_det_adapter",
        )
        public = {
            name
            for name, value in vars(adapter).items()
            if not name.startswith("_") and callable(value)
        }
        self.assertEqual(
            {"inspect", "validate", "prepare_runs", "execute", "parse_result"},
            public,
        )
        task = {
            "route_inputs": {
                "config": {"mode": "synthetic_debug"},
                "seed": 17,
            }
        }
        prepared = adapter.prepare_runs(repository, task)
        self.assertEqual(1, len(prepared))
        self.assertEqual(FROZEN_FIELDS, set(prepared[0]))
        self.assertIsInstance(prepared[0]["data"]["evaluation"], dict)
        self.assertEqual(
            "synthetic_debug_only",
            prepared[0]["data"]["kind"],
        )
        self.assertEqual(
            {
                "schema": "pack-det.evaluation-contract.v1",
                "backend": "debug_bbox_metrics",
                "iou_type": "bbox",
                "metrics": [
                    "debug_bbox_mean_iou",
                    "debug_precision_at_iou_0_5",
                ],
            },
            prepared[0]["data"]["evaluation"],
        )
        self.assertNotIn("run_kind", prepared[0])
        self.assertNotIn("paper_eligible", prepared[0])
        evidence = self._adapter_run("RUN-0001", purpose="evidence")
        with self.assertRaisesRegex(ValueError, "合成|evidence|论文"):
            adapter.execute(repository, "start", evidence)
        run = self._adapter_run("RUN-0002")
        self.assertEqual(
            {"status": "finished", "process_id": None},
            adapter.execute(repository, "start", run),
        )
        parsed = adapter.parse_result(repository, run)
        self.assertEqual("valid", parsed["quality"]["metrics"])
        self.assertEqual(parsed, _validate_parsed(parsed))
        self.assertTrue(
            all(
                path.startswith(".cv-workflow-output/RUN-0002/")
                for path in parsed["artifacts"]
            )
        )
        with self.assertRaisesRegex(FileExistsError, "存在|覆盖"):
            adapter.execute(repository, "start", run)

    def test_adapter_identity_requires_exact_frozen_code_environment_and_purpose(self) -> None:
        repository = self._create_repository("det-frozen-contract")
        adapter = self._load_python(
            repository / "workflow_adapter.py",
            "_det_frozen_contract_adapter",
        )
        valid = self._adapter_run("RUN-0009")
        self.assertEqual("RUN-0009", adapter._identity(valid)["run_id"])
        variants: list[tuple[str, dict[str, object]]] = []
        missing_code = json.loads(json.dumps(valid))
        missing_code["frozen"].pop("code")
        variants.append(("missing-code", missing_code))
        extra_frozen = json.loads(json.dumps(valid))
        extra_frozen["frozen"]["unexpected"] = True
        variants.append(("extra-frozen", extra_frozen))
        wrong_code = json.loads(json.dumps(valid))
        wrong_code["frozen"]["code"]["version"] = "9.9.9"
        variants.append(("wrong-code", wrong_code))
        wrong_environment = json.loads(json.dumps(valid))
        wrong_environment["frozen"]["environment"]["device"] = "cuda"
        variants.append(("wrong-environment", wrong_environment))
        wrong_purpose = json.loads(json.dumps(valid))
        wrong_purpose["purpose"] = "formal"
        variants.append(("wrong-purpose", wrong_purpose))
        for name, candidate in variants:
            with self.subTest(name=name), self.assertRaisesRegex(
                ValueError,
                "frozen|code|environment|purpose|字段",
            ):
                adapter._identity(candidate)

    def test_local_coco_identity_is_content_bound_and_dependency_is_explicit(self) -> None:
        repository = self._create_repository("det-coco")
        images, annotation = self._write_coco(self.root / "det-coco-data")
        adapter = self._load_python(
            repository / "workflow_adapter.py",
            "_det_coco_adapter",
        )
        config = {
            "mode": "local_coco",
            "device": "cpu",
            "data_root": str(images),
            "annotation_file": str(annotation),
            "dataset_id": "tiny-coco-det",
            "version": "2026.07",
            "source_uri": "https://example.org/datasets/tiny-coco-det",
            "split": "val",
        }
        prepared = adapter.prepare_runs(
            repository,
            {"route_inputs": {"config": config, "seed": 17}},
        )[0]
        self.assertEqual(
            {"kind", "dataset_identity", "evaluation"},
            set(prepared["data"]),
        )
        self.assertEqual("local_dataset", prepared["data"]["kind"])
        identity = prepared["data"]["dataset_identity"]
        self.assertEqual(
            {
                "schema",
                "dataset_id",
                "version",
                "source_uri",
                "manifest_sha256",
                "split",
            },
            set(identity),
        )
        self.assertRegex(identity["manifest_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            {
                "schema": "pack-det.evaluation-contract.v1",
                "backend": "pycocotools==2.0.11",
                "iou_type": "bbox",
                "metrics": ["coco_bbox_ap", "coco_bbox_ap50", "coco_bbox_ap75"],
            },
            prepared["data"]["evaluation"],
        )
        invalid_uri = dict(config, source_uri=str(images))
        with self.assertRaisesRegex(ValueError, "source_uri|URI"):
            adapter.prepare_runs(
                repository,
                {"route_inputs": {"config": invalid_uri}},
            )
        run = self._adapter_run(
            "RUN-0003",
            mode="local_coco",
            purpose="evidence",
            config={key: value for key, value in config.items() if key != "mode"},
            data=prepared["data"],
        )
        try:
            import importlib.metadata as metadata

            installed = metadata.version("pycocotools")
        except metadata.PackageNotFoundError:
            installed = None
        if installed != "2.0.11":
            with self.assertRaisesRegex(
                RuntimeError,
                "OPTIONAL_NOT_INSTALLED|2\\.0\\.11",
            ):
                adapter.execute(repository, "start", run)
            self.assertFalse(
                (repository / ".cv-workflow-output" / "RUN-0003").exists()
            )

        Image.new("RGB", (16, 16), (1, 1, 1)).save(images / "one.png")
        bound_run = {
            "id": "RUN-0007",
            "purpose": "evidence",
            "frozen": {
                "code": {"template_id": "PACK-DET", "version": "1.0.0"},
                "config": config,
                "seed": 17,
                "data": prepared["data"],
                "environment": {"backend": "project", "device": "cpu"},
            },
        }
        with self.assertRaisesRegex(ValueError, "data|manifest|数据|身份"):
            adapter.execute(repository, "start", bound_run)

    def test_coco_validator_rejects_bad_geometry_links_and_read_time_replacement(self) -> None:
        repository = self._create_repository("det-validator")
        runtime = self._load_python(
            repository / "src" / "det" / "runtime.py",
            "_det_runtime",
        )
        images, annotation = self._write_coco(self.root / "det-validation-data")
        valid = runtime.load_coco_dataset(images, annotation)
        self.assertEqual(2, len(valid["images"]))

        original = read_json(annotation)
        strict_integer_cases = (
            ("image_id", True),
            ("image_id", 1.0),
            ("category_id", True),
            ("category_id", 1.0),
            ("iscrowd", False),
            ("iscrowd", 0.0),
        )
        for index, (field, value) in enumerate(strict_integer_cases):
            payload = json.loads(json.dumps(original))
            payload["annotations"][0][field] = value
            candidate = annotation.with_name(f"strict-integer-{index}.json")
            candidate.write_text(json.dumps(payload), encoding="utf-8")
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                ValueError,
                "image|category|iscrowd|整数|关联",
            ):
                runtime.load_coco_dataset(images, candidate)

        with mock.patch.object(runtime, "MAX_IMAGE_DIMENSION", 8, create=True):
            with self.assertRaisesRegex(ValueError, "尺寸|width|height|维度"):
                runtime.load_coco_dataset(images, annotation)
        with mock.patch.object(runtime, "MAX_TOTAL_PIXELS", 100, create=True):
            with self.assertRaisesRegex(ValueError, "像素|pixel|上限"):
                runtime.load_coco_dataset(images, annotation)
        too_many = json.loads(json.dumps(original))
        duplicate = dict(too_many["annotations"][0])
        duplicate["id"] = 3
        too_many["annotations"].append(duplicate)
        too_many_path = annotation.with_name("too-many-per-image.json")
        too_many_path.write_text(json.dumps(too_many), encoding="utf-8")
        with mock.patch.object(runtime, "MAX_INSTANCES_PER_IMAGE", 1, create=True):
            with self.assertRaisesRegex(ValueError, "实例|annotation|数量|上限"):
                runtime.load_coco_dataset(images, too_many_path)

        payload = json.loads(json.dumps(original))
        payload["annotations"][0]["bbox"] = [12.0, 2.0, 8.0, 7.0]
        annotation.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "bbox|边界|越界"):
            runtime.load_coco_dataset(images, annotation)

        images, annotation = self._write_coco(self.root / "det-link-data")
        linked = self.root / "det-linked-images"
        try:
            linked.symlink_to(images, target_is_directory=True)
        except OSError:
            linked = None
        if linked is not None:
            with self.assertRaisesRegex(ValueError, "link|reparse"):
                runtime.load_coco_dataset(linked, annotation)

        victim = images / "one.png"
        replacement = images / "replacement.png"
        Image.new("RGB", (16, 16), (1, 2, 3)).save(replacement)
        replacement_bytes = replacement.read_bytes()
        original_read = runtime._os.read
        switched = False

        def replacing_read(descriptor: int, size: int) -> bytes:
            nonlocal switched
            chunk = original_read(descriptor, size)
            if not switched and chunk:
                switched = True
                victim.write_bytes(replacement_bytes)
            return chunk

        with mock.patch.object(runtime._os, "read", replacing_read):
            with self.assertRaisesRegex(ValueError, "读取期间|替换|变化"):
                runtime.stable_read_bytes(victim, "图片", 2 * 1024 * 1024)

    def test_tampered_checkpoint_and_prediction_are_rejected(self) -> None:
        repository = self._create_repository("det-tamper")
        adapter = self._load_python(
            repository / "workflow_adapter.py",
            "_det_tamper_adapter",
        )
        run = self._adapter_run("RUN-0004")
        adapter.execute(repository, "start", run)
        output = repository / ".cv-workflow-output" / "RUN-0004"
        prediction_path = output / "predictions.json"
        prediction = read_json(prediction_path)
        prediction["predictions"][0]["input_sha256"] = "sha256:" + ("0" * 64)
        prediction_path.write_text(json.dumps(prediction), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash|SHA|篡改"):
            adapter.parse_result(repository, run)

        second = self._adapter_run("RUN-0005")
        adapter.execute(repository, "start", second)
        checkpoint_path = (
            repository / ".cv-workflow-output" / "RUN-0005" / "checkpoint.pt"
        )
        import torch

        checkpoint = torch.load(
            checkpoint_path,
            map_location="cpu",
            weights_only=True,
        )
        first_key = next(iter(checkpoint["model_state_dict"]))
        checkpoint["model_state_dict"][first_key].fill_(float("nan"))
        checkpoint_path.unlink()
        torch.save(checkpoint, checkpoint_path)
        with self.assertRaisesRegex(ValueError, "NaN|Inf|有限|checkpoint"):
            adapter.parse_result(repository, second)

    def test_checkpoint_uses_one_stable_byte_snapshot(self) -> None:
        repository = self._create_repository("det-checkpoint-snapshot")
        adapter = self._load_python(
            repository / "workflow_adapter.py",
            "_det_checkpoint_snapshot_adapter",
        )
        run = self._adapter_run("RUN-0010")
        adapter.execute(repository, "start", run)
        checkpoint_path = (
            repository / ".cv-workflow-output" / "RUN-0010" / "checkpoint.pt"
        )
        runtime = self._load_python(
            repository / "src" / "det" / "runtime.py",
            "_det_checkpoint_snapshot_runtime",
        )
        torch = runtime.require_runtime()[0]
        real_load = torch.load
        observed_sources: list[object] = []

        def checked_load(source: object, *args: object, **kwargs: object) -> object:
            observed_sources.append(source)
            self.assertIsInstance(source, runtime.io.BytesIO)
            return real_load(source, *args, **kwargs)

        with mock.patch.object(
            runtime,
            "stable_read_bytes",
            wraps=runtime.stable_read_bytes,
        ) as stable_read, mock.patch.object(torch, "load", side_effect=checked_load):
            checkpoint, _ = runtime.load_checkpoint(checkpoint_path)
        self.assertEqual("RUN-0010", checkpoint["run_id"])
        stable_read.assert_called_once_with(
            checkpoint_path,
            "checkpoint",
            runtime.MAX_FILE_BYTES,
        )
        self.assertEqual(1, len(observed_sources))

    def test_output_tree_and_json_top_levels_are_exact_and_resource_bounded(self) -> None:
        repository = self._create_repository("det-output-exact")
        adapter = self._load_python(
            repository / "workflow_adapter.py",
            "_det_output_exact_adapter",
        )
        run = self._adapter_run("RUN-0011")
        adapter.execute(repository, "start", run)
        output = repository / ".cv-workflow-output" / "RUN-0011"
        for filename in ("run.json", "evaluation.json", "predictions.json"):
            path = output / filename
            original = path.read_bytes()
            payload = json.loads(original)
            payload["unexpected"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.subTest(filename=filename), self.assertRaisesRegex(
                ValueError,
                "顶层|字段|严格",
            ):
                adapter.parse_result(repository, run)
            path.write_bytes(original)

        extra = output / "unlisted.txt"
        extra.write_text("not in run manifest", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "未登记|输出|清单|未知"):
            adapter.parse_result(repository, run)
        extra.unlink()
        linked = output / "unlisted-link.txt"
        try:
            linked.symlink_to(output / "raw.log")
        except OSError:
            linked = None
        if linked is not None:
            with self.assertRaisesRegex(ValueError, "link|reparse|普通文件"):
                adapter.parse_result(repository, run)
            linked.unlink()

        runtime = self._load_python(
            repository / "src" / "det" / "runtime.py",
            "_det_output_bound_runtime",
        )
        record = read_json(output / "run.json")
        with mock.patch.object(runtime, "MAX_TOTAL_PIXELS", 1, create=True):
            with self.assertRaisesRegex(ValueError, "像素|pixel|上限"):
                runtime.validate_workflow_output(
                    output,
                    mode="synthetic_debug",
                    run_id="RUN-0011",
                    config_sha256=record["config_sha256"],
                    seed=record["seed"],
                )

    def test_output_total_bytes_include_png_tails_and_every_artifact(self) -> None:
        repository = self._create_repository("det-output-total-bytes")
        adapter = self._load_python(
            repository / "workflow_adapter.py",
            "_det_output_total_bytes_adapter",
        )
        run = self._adapter_run("RUN-0012")
        adapter.execute(repository, "start", run)
        adapter.parse_result(repository, run)
        output = repository / ".cv-workflow-output" / "RUN-0012"
        runtime = self._load_python(
            repository / "src" / "det" / "runtime.py",
            "_det_output_total_bytes_runtime",
        )

        padding = b"\x00" * 4096
        predictions_path = output / "predictions.json"
        predictions = read_json(predictions_path)
        for item in predictions["predictions"]:
            input_path = output / item["input_path"]
            input_path.write_bytes(input_path.read_bytes() + padding)
            item["input_sha256"] = (
                "sha256:" + hashlib.sha256(input_path.read_bytes()).hexdigest()
            )
        predictions_path.write_bytes(
            (
                json.dumps(
                    predictions,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            + (b" " * 4096)
        )
        for filename in ("data-manifest.json", "evaluation.json"):
            path = output / filename
            path.write_bytes(path.read_bytes() + (b" " * 4096))
        log_path = output / "raw.log"
        log_path.write_bytes(log_path.read_bytes() + (b"x" * 4096))

        run_path = output / "run.json"
        record = read_json(run_path)
        for relative in record["artifact_sha256"]:
            content = (output / relative).read_bytes()
            record["artifact_sha256"][relative] = (
                "sha256:" + hashlib.sha256(content).hexdigest()
            )
        run_path.write_bytes(
            (
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            + (b" " * 4096)
        )

        total_bytes = sum(
            path.stat().st_size
            for path in output.rglob("*")
            if path.is_file()
        )
        with mock.patch.object(
            runtime,
            "MAX_OUTPUT_BYTES",
            total_bytes - 1,
            create=True,
        ):
            with self.assertRaisesRegex(ValueError, "输出.*总|总.*字节|总量|上限"):
                runtime.validate_workflow_output(
                    output,
                    mode="synthetic_debug",
                    run_id="RUN-0012",
                    config_sha256=record["config_sha256"],
                    seed=record["seed"],
                )
        self.assertEqual(64 * 1024 * 1024, runtime.MAX_OUTPUT_BYTES)

    def test_formal_coco_sidecar_is_self_contained_for_exact_backend(self) -> None:
        repository = self._create_repository("det-formal-sidecar")
        runtime = self._load_python(
            repository / "src" / "det" / "runtime.py",
            "_det_formal_sidecar_runtime",
        )
        images, annotation = self._write_coco(self.root / "det-formal-data")
        dataset = runtime.load_coco_dataset(images, annotation)
        output = self.root / "det-formal-output"
        output.mkdir()
        observed: dict[str, object] = {}

        class FakeCOCO:
            def __init__(self, path: str) -> None:
                payload = json.loads(Path(path).read_text(encoding="utf-8"))
                if "info" not in payload:
                    raise AssertionError("COCO loadRes requires a self-contained info field")
                observed["payload"] = payload

            def loadRes(self, results: list[dict[str, object]]) -> object:
                observed["results"] = results
                return object()

        class FakeEvaluator:
            def __init__(self, truth: object, result: object, iou_type: str) -> None:
                self.stats = [0.1, 0.2, 0.3]
                observed["iou_type"] = iou_type

            def evaluate(self) -> None:
                pass

            def accumulate(self) -> None:
                pass

            def summarize(self) -> None:
                pass

        runtime.require_pycocotools = lambda: (FakeCOCO, FakeEvaluator)
        predictions = [
            {
                "image_id": image["id"],
                "category_id": image["annotations"][0]["category_id"],
                "bbox": image["annotations"][0]["bbox"],
                "score": 0.5,
            }
            for image in dataset["images"]
        ]
        metrics = runtime._formal_metrics(output, dataset, predictions)
        self.assertEqual("bbox", observed["iou_type"])
        self.assertEqual(
            {"coco_bbox_ap": 0.1, "coco_bbox_ap50": 0.2, "coco_bbox_ap75": 0.3},
            metrics,
        )

    def test_output_manifest_rejects_windows_backslash_traversal(self) -> None:
        repository = self._create_repository("det-output-traversal")
        adapter = self._load_python(
            repository / "workflow_adapter.py",
            "_det_output_traversal_adapter",
        )
        run = self._adapter_run("RUN-0006")
        adapter.execute(repository, "start", run)
        outside = repository / "outside.txt"
        outside.write_text("must-not-be-read-as-an-artifact", encoding="utf-8")
        record_path = (
            repository / ".cv-workflow-output" / "RUN-0006" / "run.json"
        )
        record = read_json(record_path)
        record["artifact_sha256"]["..\\..\\..\\outside.txt"] = (
            "sha256:" + hashlib.sha256(outside.read_bytes()).hexdigest()
        )
        record_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "路径|path|artifact|穿越|规范"):
            adapter.parse_result(repository, run)

    def test_prediction_identity_rejects_duplicate_images(self) -> None:
        repository = self._create_repository("det-duplicate-prediction")
        adapter = self._load_python(
            repository / "workflow_adapter.py",
            "_det_duplicate_prediction_adapter",
        )
        run = self._adapter_run("RUN-0008")
        adapter.execute(repository, "start", run)
        output = repository / ".cv-workflow-output" / "RUN-0008"
        prediction_path = output / "predictions.json"
        payload = read_json(prediction_path)
        payload["predictions"][1]["image_id"] = payload["predictions"][0]["image_id"]
        prediction_path.write_text(json.dumps(payload), encoding="utf-8")
        record_path = output / "run.json"
        record = read_json(record_path)
        record["artifact_sha256"]["predictions.json"] = (
            "sha256:" + hashlib.sha256(prediction_path.read_bytes()).hexdigest()
        )
        record_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "image|图片|重复|身份"):
            adapter.parse_result(repository, run)

    def test_prediction_identity_is_bound_to_frozen_manifest_and_categories(self) -> None:
        repository = self._create_repository("det-frozen-prediction")
        adapter = self._load_python(
            repository / "workflow_adapter.py",
            "_det_frozen_prediction_adapter",
        )
        run = self._adapter_run("RUN-0009")
        adapter.execute(repository, "start", run)
        output = repository / ".cv-workflow-output" / "RUN-0009"
        prediction_path = output / "predictions.json"
        record_path = output / "run.json"
        original_predictions = read_json(prediction_path)
        original_record = read_json(record_path)
        cases = (
            ("image_id", 999_999),
            (
                "source_sha256",
                original_predictions["predictions"][1]["source_sha256"],
            ),
            ("category_id", 999_999),
        )
        for field, value in cases:
            payload = copy.deepcopy(original_predictions)
            payload["predictions"][0][field] = value
            prediction_path.write_text(json.dumps(payload), encoding="utf-8")
            record = copy.deepcopy(original_record)
            record["artifact_sha256"]["predictions.json"] = (
                "sha256:" + hashlib.sha256(prediction_path.read_bytes()).hexdigest()
            )
            record_path.write_text(json.dumps(record), encoding="utf-8")
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError,
                "manifest|source|category|image|数据|样本|类别|身份",
            ):
                adapter.parse_result(repository, run)

    def test_huge_config_and_checkpoint_integers_fail_before_allocation(self) -> None:
        repository = self._create_repository("det-bounded-model")
        runtime = self._load_python(
            repository / "src" / "det" / "runtime.py",
            "_det_bounded_model_runtime",
        )
        base_config = read_json(repository / "configs" / "smoke.json")
        for field in (
            "num_classes",
            "num_queries",
            "image_size",
            "epochs",
            "sample_count",
        ):
            config = dict(base_config)
            config[field] = 1_000_000_000
            candidate = self.root / f"det-huge-{field}.json"
            candidate.write_text(json.dumps(config), encoding="utf-8")
            with mock.patch.object(
                runtime,
                "synthetic_dataset",
                side_effect=AssertionError("dataset allocation reached"),
            ), mock.patch.object(
                runtime,
                "build_model",
                side_effect=AssertionError("model allocation reached"),
            ), self.subTest(source="config", field=field), self.assertRaisesRegex(
                ValueError,
                "上限|最多|至多|范围|maximum",
            ):
                runtime.run_training(
                    candidate,
                    self.root / f"det-huge-{field}-output",
                    mode="synthetic_debug",
                )

        valid_output = self.root / "det-small-checkpoint"
        runtime.run_training(
            repository / "configs" / "smoke.json",
            valid_output,
            mode="synthetic_debug",
        )
        torch = runtime.require_runtime()[0]
        original = torch.load(
            valid_output / "checkpoint.pt",
            map_location="cpu",
            weights_only=True,
        )
        for field in ("num_classes", "num_queries", "image_size"):
            payload = copy.deepcopy(original)
            payload["model_config"][field] = 1_000_000_000
            candidate = self.root / f"det-huge-checkpoint-{field}.pt"
            torch.save(payload, candidate)
            with mock.patch.object(
                runtime,
                "build_model",
                side_effect=AssertionError("model allocation reached"),
            ), self.subTest(source="checkpoint", field=field), self.assertRaisesRegex(
                ValueError,
                "上限|最多|至多|范围|maximum",
            ):
                runtime.load_checkpoint(candidate)


if __name__ == "__main__":
    unittest.main()
