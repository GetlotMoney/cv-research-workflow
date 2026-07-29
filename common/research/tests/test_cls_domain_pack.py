from __future__ import annotations

import json
import hashlib
import importlib.util
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from pathlib import Path

from PIL import Image

from tests._helpers import SCRIPTS, cli_json, read_json, run_cli


sys.path.insert(0, str(SCRIPTS))

from workflow_core.domain_packs import (  # noqa: E402
    discover_domain_packs,
    resolve_domain_pack,
    validate_domain_pack,
)
from workflow_core.project import init_project  # noqa: E402
from workflow_core.review import _validate_parsed  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SOURCE = {
    "name": "torchvision",
    "url": "https://github.com/pytorch/vision",
    "revision": "78839c2b06c83c6cfb5c4da692ffb331bbd4c4cc",
    "license": "BSD-3-Clause",
}
REQUIRED_PAYLOAD_FILES = {
    ".gitattributes",
    ".gitignore",
    "LICENSE",
    "README.md",
    "domain-pack.json",
    "DOWNLOADS.json",
    "requirements/windows-cpu.lock.txt",
    "configs/smoke.json",
    "configs/baseline.json",
    "contracts/metrics.v1.json",
    "cls/__init__.py",
    "cls/data.py",
    "cls/model.py",
    "cls/metrics.py",
    "cls/contract.py",
    "cls/train.py",
    "cls/evaluate.py",
    "cls/infer.py",
    "cls/smoke.py",
    "workflow_adapter.py",
    "tests/domain_pack/test_synthetic_smoke.py",
    "tests/domain_pack/test_metric_contract.py",
}
METRIC_DEFINITION = {
    "top1_accuracy": (
        "验证集样本中最高分预测类别与真实标签一致的比例；"
        "取值为 0 到 1，越高越好。"
    ),
    "top5_accuracy": (
        "验证集真实标签落入得分最高的 min(5, 类别数) 个预测中的比例；"
        "取值为 0 到 1，越高越好。"
    ),
    "macro_f1": (
        "先分别计算每个类别的 F1，再对所有类别做等权平均；"
        "取值为 0 到 1，越高越好。"
    ),
}


class ClassificationDomainPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _run_repo(
        self,
        repository: Path,
        *arguments: str,
        timeout: float = 30.0,
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
        self.assertEqual(
            0,
            result.returncode,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    def _create_repository(self, name: str = "classification") -> Path:
        project = self.root / f"{name}-workflow"
        destination = self.root / f"{name}-repo"
        init_project(project, name, layout="v2")
        result = cli_json(
            "create-domain-repo",
            "--project",
            project,
            "--pack",
            "cls",
            "--destination",
            destination,
            "--name",
            name,
        )
        self.assertEqual("CB-0001", result["codebase"]["id"])
        self.assertEqual(str(destination.resolve()), result["repository"]["repo_path"])
        return destination

    def _load_adapter(self, repository: Path, suffix: str = "") -> types.ModuleType:
        adapter_path = repository / "workflow_adapter.py"
        module = types.ModuleType(f"_classification_pack_adapter_{suffix}")
        module.__file__ = str(adapter_path)
        exec(
            compile(adapter_path.read_bytes(), str(adapter_path), "exec"),
            module.__dict__,
        )
        return module

    @staticmethod
    def _adapter_run(
        run_id: str,
        *,
        mode: str = "synthetic_smoke",
        purpose: str = "debug",
        seed: int = 7,
        config: dict[str, object] | None = None,
    ) -> dict[str, object]:
        frozen_config: dict[str, object] = {"mode": mode}
        if mode == "local_imagefolder":
            frozen_config["device"] = "cpu"
        if config:
            frozen_config.update(config)
        frozen_config["metric_definition"] = dict(METRIC_DEFINITION)
        return {
            "id": run_id,
            "purpose": purpose,
            "frozen": {
                "config": frozen_config,
                "seed": seed,
            },
        }

    def test_builtin_registry_manifest_and_adapter_contract_are_real(self) -> None:
        packs = {pack["id"]: pack for pack in discover_domain_packs()}
        self.assertEqual(
            {"cls", "det", "gzsl", "instseg", "seg", "sr"},
            set(packs),
        )
        self.assertEqual(
            {
                "id": "cls",
                "version": "1.0.0",
                "template_id": "PACK-CLS",
                "primary_direction": "cls",
                "directory": "cls-v1.0.0",
            },
            packs["cls"],
        )
        pack_root = resolve_domain_pack("cls")
        self.assertEqual(pack_root, resolve_domain_pack("cls@1.0.0"))
        manifest = validate_domain_pack(pack_root)
        self.assertEqual([EXPECTED_SOURCE], manifest["source_references"])
        self.assertEqual(
            REQUIRED_PAYLOAD_FILES,
            {entry["path"] for entry in manifest["files"]},
        )
        payload = pack_root / "payload"
        self.assertEqual("* -text\n", (payload / ".gitattributes").read_text("utf-8"))
        contract = read_json(payload / "domain-pack.json")
        self.assertEqual("PACK-CLS", contract["template_id"])
        self.assertEqual(
            ["train", "evaluate", "infer", "synthetic_smoke"],
            contract["commands"],
        )
        self.assertFalse(contract["synthetic_smoke"]["paper_eligible"])

        adapter_path = payload / "workflow_adapter.py"
        module = types.ModuleType("_classification_pack_adapter")
        module.__file__ = str(adapter_path)
        exec(
            compile(
                adapter_path.read_bytes(),
                str(adapter_path),
                "exec",
            ),
            module.__dict__,
        )
        public_callables = {
            name
            for name, value in vars(module).items()
            if not name.startswith("_") and callable(value)
        }
        self.assertEqual(
            {"inspect", "validate", "prepare_runs", "execute", "parse_result"},
            public_callables,
        )

    def test_skill_documents_explain_the_two_domain_pack_commands(self) -> None:
        documents = (
            ROOT / "skills" / "cv-experiment-workflow" / "SKILL.md",
            ROOT
            / "skills"
            / "cv-experiment-workflow"
            / "references"
            / "workflow.md",
        )
        for document in documents:
            with self.subTest(document=document.name):
                text = document.read_text(encoding="utf-8")
                self.assertIn("list-domain-packs", text)
                self.assertIn("create-domain-repo", text)
                self.assertIn("synthetic_debug_only", text)
                self.assertIn("paper_eligible=false", text)

    def test_prepare_runs_builds_real_dataset_identity_from_actual_file_manifest(
        self,
    ) -> None:
        repository = self._create_repository("dataset-identity")
        module = self._load_adapter(repository, "dataset_identity")
        data_root = self.root / "dataset-identity-data"
        train_file = data_root / "train" / "class-a" / "a.bin"
        val_file = data_root / "val" / "class-a" / "b.bin"
        train_file.parent.mkdir(parents=True)
        val_file.parent.mkdir(parents=True)
        train_file.write_bytes(b"train-v1")
        val_file.write_bytes(b"val-v1")
        config = {
            "mode": "local_imagefolder",
            "data_root": str(data_root),
            "dataset_id": "demo-cls",
            "version": "2026-07",
            "source_uri": "local-dataset:demo-cls/2026-07",
        }
        task = {"route_inputs": {"config": config, "seed": 7}}

        prepared = module.prepare_runs(repository, task)[0]
        first = prepared["data"]
        self.assertEqual(
            METRIC_DEFINITION,
            prepared["config"]["metric_definition"],
        )
        with self.assertRaisesRegex(ValueError, "字段|metric_definition"):
            module.prepare_runs(
                repository,
                {
                    "route_inputs": {
                        "config": {
                            **config,
                            "metric_definition": {
                                "top1_accuracy": "用户覆盖"
                            },
                        },
                        "seed": 7,
                    }
                },
            )
        identity = first["dataset_identity"]
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
        self.assertEqual(
            "cv-experiment-workflow.dataset-identity.v1",
            identity["schema"],
        )
        self.assertEqual("train+val", identity["split"])
        self.assertRegex(identity["manifest_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn(str(data_root), identity["source_uri"])
        self.assertEqual(
            {
                "schema": "pack-cls.evaluation.v1",
                "metric": "top1_accuracy",
                "split": "val",
            },
            first["evaluation"],
        )

        train_file.write_bytes(b"train-v2")
        changed = module.prepare_runs(repository, task)[0]["data"]
        self.assertNotEqual(
            identity["manifest_sha256"],
            changed["dataset_identity"]["manifest_sha256"],
        )

        missing = {
            "route_inputs": {
                "config": {
                    key: value
                    for key, value in config.items()
                    if key != "version"
                },
                "seed": 7,
            }
        }
        with self.assertRaisesRegex(ValueError, "配置字段|version"):
            module.prepare_runs(repository, missing)
        local_source = {
            "route_inputs": {
                "config": {
                    **config,
                    "source_uri": r"D:\private\dataset",
                },
                "seed": 7,
            }
        }
        with self.assertRaisesRegex(ValueError, "source_uri|URI"):
            module.prepare_runs(repository, local_source)

        linked_root = self.root / "linked-dataset-root"
        try:
            linked_root.symlink_to(data_root, target_is_directory=True)
        except OSError:
            linked_root = None
        if linked_root is not None:
            linked_task = {
                "route_inputs": {
                    "config": {
                        **config,
                        "data_root": str(linked_root),
                    },
                    "seed": 7,
                }
            }
            with self.assertRaisesRegex(ValueError, "link|reparse"):
                module.prepare_runs(repository, linked_task)

        original_open = Path.open
        replacement = train_file.with_name("replacement.bin")
        replacement.write_bytes(b"replaced-during-read")
        replaced = {"done": False}

        def replacing_open(path: Path, *args: object, **kwargs: object) -> object:
            if Path(path).name == train_file.name and not replaced["done"]:
                replaced["done"] = True
                os.replace(replacement, train_file)
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", replacing_open):
            with self.assertRaisesRegex(ValueError, "替换|变化|open"):
                module.prepare_runs(repository, task)

    def test_cli_lists_creates_registers_and_runs_real_synthetic_smoke(self) -> None:
        listed = cli_json("list-domain-packs")
        self.assertEqual("cv-experiment-workflow.domain-pack-registry.v1", listed["schema"])
        listed_ids = [pack["id"] for pack in listed["packs"]]
        self.assertEqual(sorted(listed_ids), listed_ids)
        self.assertEqual(
            ["cls", "det", "gzsl", "instseg", "seg", "sr"],
            listed_ids,
        )
        rejected = run_cli(
            "list-domain-packs",
            "--assets-root",
            self.root,
            check=False,
        )
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("unrecognized arguments", rejected.stderr)

        repository = self._create_repository()
        output = repository / "runs" / "smoke"
        started = time.perf_counter()
        self._run_repo(
            repository,
            "-m",
            "cls.smoke",
            "--work-dir",
            str(output),
            "--device",
            "cpu",
        )
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 20.0)

        run = read_json(output / "run.json")
        metrics = read_json(output / "metrics.json")
        predictions = read_json(output / "predictions.json")
        self.assertEqual("synthetic_debug_only", run["run_kind"])
        self.assertFalse(run["paper_eligible"])
        self.assertEqual(7, run["seed"])
        self.assertEqual("cpu", run["device"])
        self.assertTrue(run["checkpoint_reloaded"])
        self.assertGreater(run["optimizer_steps"], 0)
        self.assertEqual(
            {"top1_accuracy", "top5_accuracy", "macro_f1"},
            set(metrics["metrics"]),
        )
        for value in metrics["metrics"].values():
            self.assertIsInstance(value, float)
            self.assertTrue(math.isfinite(value))
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)
        self.assertGreater(len(predictions["predictions"]), 0)
        self.assertTrue((output / "checkpoint.pt").is_file())

        import torch

        checkpoint = torch.load(
            output / "checkpoint.pt",
            map_location="cpu",
            weights_only=True,
        )
        self.assertEqual(
            {
                "model_state_dict",
                "class_names",
                "image_size",
                "seed",
                "optimizer_steps",
            },
            set(checkpoint),
        )
        self.assertGreater(checkpoint["optimizer_steps"], 0)
        self.assertTrue(checkpoint["model_state_dict"])
        self.assertTrue(
            all(
                isinstance(value, torch.Tensor)
                for value in checkpoint["model_state_dict"].values()
            )
        )
        git_status = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
        self.assertEqual(
            "",
            git_status,
            "运行产物应被 .gitignore 排除，不能把新仓库弄脏",
        )
        protected = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                output / "checkpoint.pt",
                output / "metrics.json",
                output / "predictions.json",
                output / "run.json",
            )
        }
        repeated = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-m",
                "cls.smoke",
                "--work-dir",
                str(output),
                "--device",
                "cpu",
            ],
            cwd=repository,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertNotEqual(0, repeated.returncode)
        self.assertEqual(
            protected,
            {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (
                    output / "checkpoint.pt",
                    output / "metrics.json",
                    output / "predictions.json",
                    output / "run.json",
                )
            },
            "重复运行必须拒绝，不能覆盖旧 smoke 产物",
        )

    def test_metric_contract_handles_perfect_top5_and_rejects_empty_input(self) -> None:
        repository = self._create_repository("metric-contract")
        script = (
            "import json, torch\n"
            "from cls.metrics import classification_metrics\n"
            "logits=torch.tensor([[9.0,1.0],[0.0,8.0],[7.0,2.0]])\n"
            "targets=torch.tensor([0,1,0])\n"
            "print(json.dumps(classification_metrics(logits, targets)))\n"
            "try:\n"
            " classification_metrics(torch.empty((0,2)), torch.empty((0,),dtype=torch.long))\n"
            "except ValueError as error:\n"
            " print(type(error).__name__ + ':' + str(error))\n"
            "else:\n"
            " raise SystemExit('empty input was accepted')\n"
        )
        result = self._run_repo(repository, "-c", script)
        lines = result.stdout.splitlines()
        metrics = json.loads(lines[0])
        self.assertEqual(
            {
                "top1_accuracy": 1.0,
                "top5_accuracy": 1.0,
                "macro_f1": 1.0,
            },
            metrics,
        )
        self.assertIn("ValueError", lines[1])

    def test_adapter_refuses_to_run_synthetic_data_as_evidence(self) -> None:
        repository = self._create_repository("synthetic-evidence-gate")
        module = self._load_adapter(repository, "evidence_gate")
        run = self._adapter_run(
            "RUN-0001",
            mode="synthetic_smoke",
            purpose="evidence",
        )
        with self.assertRaisesRegex(ValueError, "evidence|论文|合成"):
            module.execute(repository, "start", run)
        with self.assertRaisesRegex(ValueError, "evidence|论文|合成"):
            module.execute(repository, "status", run)
        self.assertFalse(
            (
                repository
                / ".cv-workflow-output"
                / "RUN-0001"
            ).exists()
        )

        debug_run = self._adapter_run("RUN-0002")
        module.execute(repository, "start", debug_run)
        record_path = (
            repository
            / ".cv-workflow-output"
            / "RUN-0002"
            / "run.json"
        )
        record = read_json(record_path)
        record["run_kind"] = "local_dataset_baseline"
        record["paper_eligible"] = True
        record_path.write_text(
            json.dumps(record, ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "篡改"):
            module.parse_result(repository, debug_run)

    def test_adapter_binds_each_run_to_a_unique_identity_and_output(self) -> None:
        repository = self._create_repository("adapter-identities")
        module = self._load_adapter(repository, "identities")
        shared = self._adapter_run(
            "RUN-0001",
            config={"output_dir": "runs/shared"},
        )
        with self.assertRaisesRegex(ValueError, "output_dir|输出"):
            module.execute(repository, "start", shared)
        self.assertFalse((repository / "runs" / "shared").exists())

        first = self._adapter_run("RUN-0001")
        second = self._adapter_run("RUN-0002")
        first_started = module.execute(repository, "start", first)
        second_started = module.execute(repository, "start", second)
        self.assertEqual(
            {"status": "finished", "process_id": None},
            first_started,
        )
        self.assertEqual(
            {"status": "finished", "process_id": None},
            second_started,
        )
        first_output = (
            repository / ".cv-workflow-output" / "RUN-0001"
        )
        second_output = (
            repository / ".cv-workflow-output" / "RUN-0002"
        )
        self.assertTrue((first_output / "run.json").is_file())
        self.assertTrue((second_output / "run.json").is_file())
        first_record = read_json(first_output / "run.json")
        second_record = read_json(second_output / "run.json")
        self.assertEqual("RUN-0001", first_record["run_id"])
        self.assertEqual("RUN-0002", second_record["run_id"])
        self.assertEqual("synthetic_smoke", first_record["mode"])
        self.assertEqual(7, first_record["seed"])
        self.assertRegex(first_record["config_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual("finished", module.execute(repository, "status", first)["status"])

        for invalid in ("RUN-1", "run-0001", "RUN-00001", "../RUN-0003"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "RUN"
            ):
                module.execute(
                    repository,
                    "status",
                    self._adapter_run(invalid),
                )

    def test_adapter_rejects_unknown_or_cross_mode_stale_results(self) -> None:
        repository = self._create_repository("adapter-mode-gate")
        module = self._load_adapter(repository, "mode_gate")
        synthetic = self._adapter_run("RUN-0001")
        module.execute(repository, "start", synthetic)

        stale_local = self._adapter_run(
            "RUN-0001",
            mode="local_imagefolder",
            purpose="evidence",
            config={"data_root": "datasets/example"},
        )
        with self.assertRaisesRegex(ValueError, "mode|模式|运行类型"):
            module.execute(repository, "status", stale_local)
        with self.assertRaisesRegex(ValueError, "mode|模式|运行类型"):
            module.parse_result(repository, stale_local)

        for mode in ("", "mystery", "../synthetic_smoke"):
            unknown = self._adapter_run("RUN-0003", mode=mode)
            for action in ("start", "status"):
                with self.subTest(mode=mode, action=action), self.assertRaisesRegex(
                    ValueError, "mode|模式"
                ):
                    module.execute(repository, action, unknown)
            with self.assertRaisesRegex(ValueError, "mode|模式"):
                module.parse_result(repository, unknown)

    def test_adapter_parse_result_strictly_validates_all_artifacts(self) -> None:
        repository = self._create_repository("adapter-strict-parse")
        module = self._load_adapter(repository, "strict_parse")
        run = self._adapter_run("RUN-0001")
        module.execute(repository, "start", run)
        output = repository / ".cv-workflow-output" / "RUN-0001"
        parsed = module.parse_result(repository, run)
        self.assertEqual(
            "valid",
            parsed["quality"]["metrics"],
        )
        self.assertEqual(
            [
                ".cv-workflow-output/RUN-0001/checkpoint.pt",
                ".cv-workflow-output/RUN-0001/metrics.json",
                ".cv-workflow-output/RUN-0001/predictions.json",
                ".cv-workflow-output/RUN-0001/run.json",
            ],
            parsed["artifacts"],
        )
        self.assertEqual(
            ".cv-workflow-output/RUN-0001/raw.log",
            parsed["result"]["raw_log"],
        )
        self.assertEqual(parsed, _validate_parsed(parsed))

        cases = [
            (
                "metrics.json",
                lambda payload: payload.update({"schema": "wrong"}),
                "schema",
            ),
            (
                "metrics.json",
                lambda payload: payload["metrics"].update({"extra": 0.5}),
                "指标|metrics",
            ),
            (
                "metrics.json",
                lambda payload: payload["metrics"].update(
                    {"top1_accuracy": 1.5}
                ),
                "指标|范围",
            ),
            (
                "run.json",
                lambda payload: payload.update({"seed": 8}),
                "seed|身份",
            ),
            (
                "run.json",
                lambda payload: payload.update({"checkpoint_reloaded": False}),
                "checkpoint|重载",
            ),
            (
                "run.json",
                lambda payload: payload.update({"optimizer_steps": 0}),
                "optimizer_steps|优化",
            ),
            (
                "run.json",
                lambda payload: payload.update({"phase": "train"}),
                "phase|阶段",
            ),
        ]
        for filename, mutate, message in cases:
            with self.subTest(filename=filename, message=message):
                path = output / filename
                original = path.read_bytes()
                payload = read_json(path)
                mutate(payload)
                path.write_text(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    encoding="utf-8",
                )
                try:
                    with self.assertRaisesRegex(ValueError, message):
                        module.parse_result(repository, run)
                finally:
                    path.write_bytes(original)

        metrics_path = output / "metrics.json"
        original_metrics = metrics_path.read_bytes()
        metrics_path.write_text(
            '{"schema":"pack-cls.metrics-output.v1",'
            '"metrics":{"top1_accuracy":NaN,'
            '"top5_accuracy":1.0,"macro_f1":1.0}}',
            encoding="utf-8",
        )
        try:
            with self.assertRaisesRegex(ValueError, "非有限|数字"):
                module.parse_result(repository, run)
        finally:
            metrics_path.write_bytes(original_metrics)

        predictions = output / "predictions.json"
        moved = output / "predictions.saved"
        predictions.rename(moved)
        try:
            with self.assertRaisesRegex((ValueError, FileNotFoundError), "predictions"):
                module.parse_result(repository, run)
        finally:
            moved.rename(predictions)

        raw_log = output / "raw.log"
        original_raw_log = raw_log.read_bytes()
        raw_log.write_text("forged\n", encoding="utf-8")
        try:
            with self.assertRaisesRegex(ValueError, "raw.log|Run 身份"):
                module.parse_result(repository, run)
        finally:
            raw_log.write_bytes(original_raw_log)

    def test_adapter_rejects_prediction_count_and_duplicate_identity_attacks(
        self,
    ) -> None:
        repository = self._create_repository("prediction-attacks")
        module = self._load_adapter(repository, "prediction_attacks")
        run = self._adapter_run("RUN-0001")
        module.execute(repository, "start", run)
        output = repository / ".cv-workflow-output" / "RUN-0001"
        path = output / "predictions.json"
        original = path.read_bytes()
        payload = read_json(path)
        predictions = payload["predictions"]
        attacks = (
            ("short", predictions[:-1]),
            ("long", [*predictions, dict(predictions[-1], input="extra")]),
            (
                "duplicate",
                [
                    *predictions[:-1],
                    dict(predictions[-1], input=predictions[0]["input"]),
                ],
            ),
        )
        for name, attacked in attacks:
            with self.subTest(name=name):
                payload["predictions"] = attacked
                path.write_text(
                    json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8",
                )
                try:
                    with self.assertRaisesRegex(
                        ValueError,
                        "predictions|数量|唯一|重复",
                    ):
                        module.parse_result(repository, run)
                finally:
                    path.write_bytes(original)
                    payload = read_json(path)
                    predictions = payload["predictions"]

        parsed = module.parse_result(repository, run)
        self.assertTrue(parsed["artifacts"])
        self.assertTrue(
            all(
                isinstance(item, str)
                and not Path(item).is_absolute()
                and "\\" not in item
                for item in parsed["artifacts"]
            )
        )
        from workflow_core.review import _validate_parsed

        self.assertEqual(parsed, _validate_parsed(parsed))

    def test_adapter_rejects_malformed_or_nonfinite_checkpoint_state_dict(
        self,
    ) -> None:
        repository = self._create_repository("checkpoint-attacks")
        module = self._load_adapter(repository, "checkpoint_attacks")
        run = self._adapter_run("RUN-0001")
        module.execute(repository, "start", run)
        checkpoint_path = (
            repository
            / ".cv-workflow-output"
            / "RUN-0001"
            / "checkpoint.pt"
        )
        original = checkpoint_path.read_bytes()

        import torch

        def load() -> dict[str, object]:
            return torch.load(
                checkpoint_path,
                map_location="cpu",
                weights_only=True,
            )

        def fake_key(payload: dict[str, object]) -> None:
            payload["model_state_dict"]["attacker.fake"] = torch.zeros(1)

        def non_tensor(payload: dict[str, object]) -> None:
            first = next(iter(payload["model_state_dict"]))
            payload["model_state_dict"][first] = "not-a-tensor"

        def nonfinite(payload: dict[str, object], value: float) -> None:
            first = next(iter(payload["model_state_dict"]))
            tensor = payload["model_state_dict"][first].clone()
            tensor.reshape(-1)[0] = value
            payload["model_state_dict"][first] = tensor

        attacks = (
            ("fake-key", fake_key),
            ("non-tensor", non_tensor),
            ("nan", lambda payload: nonfinite(payload, float("nan"))),
            ("inf", lambda payload: nonfinite(payload, float("inf"))),
        )
        for name, mutate in attacks:
            with self.subTest(name=name):
                payload = load()
                mutate(payload)
                torch.save(payload, checkpoint_path)
                try:
                    with self.assertRaisesRegex(
                        ValueError,
                        "checkpoint|state_dict|Tensor|有限",
                    ):
                        module.parse_result(repository, run)
                finally:
                    checkpoint_path.write_bytes(original)

    def test_checkpoint_validation_uses_only_fixed_repository_model_root(
        self,
    ) -> None:
        repository = self._create_repository("checkpoint-trust-root")
        module = self._load_adapter(repository, "checkpoint_trust_root")
        run = self._adapter_run("RUN-0001")
        module.execute(repository, "start", run)
        marker = self.root / "untrusted-model-executed.txt"
        untrusted = repository / ".cv-workflow-output" / "cls"
        untrusted.mkdir()
        marker_literal = repr(str(marker))
        (untrusted / "contract.py").write_text(
            "from pathlib import Path\n"
            f"Path({marker_literal}).write_text('contract', encoding='utf-8')\n"
            "def require_runtime():\n"
            " import torch, numpy\n"
            " from PIL import Image\n"
            " return torch, numpy, Image\n",
            encoding="utf-8",
        )
        (untrusted / "model.py").write_text(
            "from pathlib import Path\n"
            f"Path({marker_literal}).write_text('model', encoding='utf-8')\n"
            "def build_model(num_classes):\n"
            " raise RuntimeError('untrusted model executed')\n",
            encoding="utf-8",
        )
        parsed = module.parse_result(repository, run)
        self.assertEqual("valid", parsed["quality"]["implementation"])
        self.assertFalse(
            marker.exists(),
            "checkpoint 校验不得搜索或执行 runs 下的伪造 cls/model.py",
        )

    def test_local_adapter_failure_preserves_staging_and_same_run_can_retry(
        self,
    ) -> None:
        repository = self._create_repository("local-staging-retry")
        module = self._load_adapter(repository, "local_staging_retry")
        data_root = self.root / "retry-images"
        run = self._adapter_run(
            "RUN-0001",
            mode="local_imagefolder",
            purpose="evidence",
            config={"data_root": str(data_root)},
        )
        final = repository / ".cv-workflow-output" / "RUN-0001"
        original_run = subprocess.run
        captured: dict[str, Path] = {}

        def forced_failure(*args: object, **kwargs: object) -> object:
            command = list(args[0])
            output = Path(command[command.index("--output-dir") + 1])
            output.mkdir()
            staging = output
            sentinel = staging / "00-staging-sentinel.txt"
            sentinel.write_text("must-remain", encoding="utf-8")
            outside = self.root / "outside-marker.txt"
            outside.write_text("outside-must-remain", encoding="utf-8")
            link = staging / "zz-external-link"
            try:
                link.symlink_to(outside)
            except OSError:
                link = None
            captured.update({
                "staging": staging,
                "sentinel": sentinel,
                "outside": outside,
            })
            if link is not None:
                captured["link"] = link
            return subprocess.CompletedProcess(
                command,
                17,
                stdout="",
                stderr="forced local failure",
            )

        module._subprocess = types.SimpleNamespace(run=forced_failure)
        with self.assertRaisesRegex(RuntimeError, r"staging|\.RUN-0001"):
            module.execute(repository, "start", run)
        self.assertFalse(final.exists())
        failed_staging = captured["staging"]
        self.assertTrue(failed_staging.is_dir())
        self.assertEqual(
            "must-remain",
            captured["sentinel"].read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "outside-must-remain",
            captured["outside"].read_text(encoding="utf-8"),
        )
        if "link" in captured:
            self.assertTrue(captured["link"].is_symlink())

        for split in ("train", "val"):
            for class_name, level in (("dark", 10), ("bright", 240)):
                directory = data_root / split / class_name
                directory.mkdir(parents=True)
                Image.new("RGB", (8, 8), (level, level, level)).save(
                    directory / "0.png"
                )
        calls: list[object] = []

        def recording_run(*args: object, **kwargs: object) -> object:
            calls.append(kwargs.get("timeout", "__missing__"))
            return original_run(*args, **kwargs)

        module._subprocess = types.SimpleNamespace(run=recording_run)
        self.assertEqual(
            {"status": "finished", "process_id": None},
            module.execute(repository, "start", run),
        )
        self.assertEqual([None], calls)
        self.assertTrue((final / "run.json").is_file())
        self.assertTrue(failed_staging.is_dir())
        self.assertTrue(captured["sentinel"].is_file())
        self.assertEqual(
            "valid",
            module.parse_result(repository, run)["quality"]["implementation"],
        )

    def test_local_success_publish_never_removes_path_after_parent_replacement(
        self,
    ) -> None:
        repository = self._create_repository("local-publish-race")
        module = self._load_adapter(repository, "local_publish_race")
        data_root = self.root / "publish-race-images"
        for split in ("train", "val"):
            for class_name, level in (("dark", 10), ("bright", 240)):
                directory = data_root / split / class_name
                directory.mkdir(parents=True)
                Image.new("RGB", (8, 8), (level, level, level)).save(
                    directory / "0.png"
                )
        run = self._adapter_run(
            "RUN-0001",
            mode="local_imagefolder",
            purpose="evidence",
            config={"data_root": str(data_root)},
        )
        original_rename = os.rename
        external = self.root / "external-workflow"
        external.mkdir()
        outside_marker = external / "outside-marker.txt"
        outside_marker.write_text("must-remain", encoding="utf-8")
        state: dict[str, object] = {"replaced": False}

        def replacing_rename(source: object, target: object) -> None:
            original_rename(source, target)
            source_path = Path(source)
            target_path = Path(target)
            workflow = target_path.parent
            moved = workflow.with_name("workflow-published-original")
            external_staging = external / source_path.parent.name
            original_rename(workflow, moved)
            external_staging.mkdir()
            try:
                workflow.symlink_to(external, target_is_directory=True)
            except OSError:
                original_rename(moved, workflow)
                state["external_staging"] = external_staging
                return
            state.update({
                "replaced": True,
                "external_staging": external_staging,
                "moved": moved,
            })

        module._os = types.SimpleNamespace(
            environ=os.environ,
            path=os.path,
            open=os.open,
            fdopen=os.fdopen,
            fsync=os.fsync,
            O_WRONLY=os.O_WRONLY,
            O_CREAT=os.O_CREAT,
            O_EXCL=os.O_EXCL,
            rename=replacing_rename,
            scandir=os.scandir,
        )
        rmdir_calls: list[Path] = []

        def forbidden_rmdir(path: Path) -> None:
            rmdir_calls.append(Path(path))
            raise AssertionError("发布成功后不允许按路径调用 rmdir")

        result: dict[str, object] | None = None
        captured_error: BaseException | None = None
        with mock.patch.object(Path, "rmdir", forbidden_rmdir):
            try:
                result = module.execute(repository, "start", run)
            except BaseException as error:
                captured_error = error

        self.assertEqual(
            [],
            rmdir_calls,
            "无论父目录是否被替换，发布后都不能再调用路径式 rmdir",
        )
        self.assertEqual(
            "must-remain",
            outside_marker.read_text(encoding="utf-8"),
        )
        self.assertTrue(state["external_staging"].is_dir())
        if state["replaced"]:
            self.assertIsNotNone(captured_error)
            self.assertIsNone(result)
            self.assertTrue(state["moved"].is_dir())
        else:
            self.assertIsNone(captured_error)
            self.assertEqual(
                {"status": "finished", "process_id": None},
                result,
            )
            self.assertTrue(
                (
                    repository
                    / ".cv-workflow-output"
                    / "RUN-0001"
                    / "run.json"
                ).is_file()
            )

    def test_workflow_output_is_exclusive_and_incomplete_run_is_not_finished(self) -> None:
        repository = self._create_repository("exclusive-output")
        module = self._load_adapter(repository, "exclusive_output")
        run = self._adapter_run("RUN-0100")
        output = repository / ".cv-workflow-output" / "RUN-0100"
        output.mkdir(parents=True)
        marker = output / "user.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex((ValueError, FileExistsError), "存在|覆盖"):
            module.execute(repository, "start", run)
        self.assertEqual("keep", marker.read_text(encoding="utf-8"))
        self.assertNotEqual(
            "finished",
            module.execute(repository, "status", run)["status"],
        )

    def test_local_imagefolder_baseline_trains_evaluates_and_infers(self) -> None:
        repository = self._create_repository("real-baseline")
        data_root = self.root / "images"
        for split in ("train", "val"):
            for class_index, class_name in enumerate(("dark", "bright")):
                directory = data_root / split / class_name
                directory.mkdir(parents=True)
                sample_count = 3 if split == "train" else 2
                for sample in range(sample_count):
                    level = 20 + sample if class_index == 0 else 230 - sample
                    Image.new("RGB", (10, 10), (level, level, level)).save(
                        directory / f"{sample}.png"
                    )

        output = self.root / "baseline-output"
        self._run_repo(
            repository,
            "-m",
            "cls.train",
            "--config",
            "configs/baseline.json",
            "--data-root",
            str(data_root),
            "--output-dir",
            str(output / "train"),
            "--device",
            "cpu",
        )
        self._run_repo(
            repository,
            "-m",
            "cls.evaluate",
            "--checkpoint",
            str(output / "train" / "checkpoint.pt"),
            "--data-root",
            str(data_root),
            "--output-dir",
            str(output / "evaluate"),
            "--split",
            "val",
        )
        self._run_repo(
            repository,
            "-m",
            "cls.infer",
            "--checkpoint",
            str(output / "train" / "checkpoint.pt"),
            "--input",
            str(data_root / "val" / "bright" / "0.png"),
            "--output",
            str(output / "predictions.json"),
        )
        for metrics_path in (
            output / "train" / "metrics.json",
            output / "evaluate" / "metrics.json",
        ):
            metrics = read_json(metrics_path)["metrics"]
            self.assertEqual(
                {"top1_accuracy", "top5_accuracy", "macro_f1"},
                set(metrics),
            )
            self.assertTrue(all(math.isfinite(value) for value in metrics.values()))
        predictions = read_json(output / "predictions.json")["predictions"]
        self.assertEqual(1, len(predictions))
        self.assertIn(predictions[0]["class_name"], {"dark", "bright"})
        self.assertTrue(math.isfinite(predictions[0]["score"]))
        before = hashlib.sha256(
            (output / "train" / "checkpoint.pt").read_bytes()
        ).hexdigest()
        repeated = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-m",
                "cls.train",
                "--config",
                "configs/baseline.json",
                "--data-root",
                str(data_root),
                "--output-dir",
                str(output / "train"),
                "--device",
                "cpu",
            ],
            cwd=repository,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertNotEqual(0, repeated.returncode)
        self.assertEqual(
            before,
            hashlib.sha256(
                (output / "train" / "checkpoint.pt").read_bytes()
            ).hexdigest(),
        )

        train_record = read_json(output / "train" / "run.json")
        self.assertEqual("val", train_record["evaluation_split"])
        self.assertEqual(6, train_record["training_sample_count"])
        self.assertEqual(4, train_record["sample_count"])
        self.assertTrue(train_record["checkpoint_reloaded"])
        self.assertGreater(train_record["optimizer_steps"], 0)
        self.assertTrue((output / "train" / "predictions.json").is_file())

    def test_formal_device_is_frozen_and_cuda_unavailable_writes_nothing(
        self,
    ) -> None:
        source = (
            ROOT
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "cls-v1.0.0"
            / "payload"
        )
        repository = self.root / "classification-gpu-contract"
        shutil.copytree(source, repository)
        data_root = self.root / "classification-gpu-data"
        for split in ("train", "val"):
            for class_name, level in (("dark", 20), ("bright", 230)):
                directory = data_root / split / class_name
                directory.mkdir(parents=True)
                Image.new("RGB", (8, 8), (level, level, level)).save(
                    directory / "0.png"
                )

        adapter = self._load_adapter(repository, "gpu_contract")
        config = {
            "mode": "local_imagefolder",
            "data_root": str(data_root),
            "dataset_id": "tiny-cls",
            "version": "1",
            "source_uri": "https://example.org/tiny-cls",
            "device": "cuda",
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
        run = {"id": "RUN-0031", "purpose": "evidence", "frozen": variant}
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
            {"route_inputs": {"config": config, "seed": 31}},
        )[0]
        cpu_run = {
            "id": "RUN-0033",
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
                / "RUN-0033"
                / "run.json"
            )["device"],
        )
        output_root = repository / ".cv-workflow-output"
        self.assertEqual(
            ["RUN-0033"],
            sorted(path.name for path in output_root.iterdir()),
        )
        parsed = adapter.parse_result(repository, cpu_run)
        self.assertEqual(
            ".cv-workflow-output/RUN-0033/raw.log",
            parsed["result"]["raw_log"],
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
            / "cls-v1.0.0"
            / "payload"
        )
        repository = self.root / "classification-device-entrypoints"
        shutil.copytree(source, repository)
        data_root = self.root / "classification-device-data"
        for split in ("train", "val"):
            for class_name, level in (("dark", 20), ("bright", 230)):
                directory = data_root / split / class_name
                directory.mkdir(parents=True)
                Image.new("RGB", (8, 8), (level, level, level)).save(
                    directory / "0.png"
                )

        sys.path.insert(0, str(repository))
        try:
            import torch
            from cls.contract import load_checkpoint
            from cls.evaluate import run_evaluation
            from cls.infer import run_inference
            from cls.train import run_training

            cuda_config = read_json(repository / "configs" / "baseline.json")
            cuda_config["device"] = "cuda"
            cuda_config_path = self.root / "classification-cuda-config.json"
            cuda_config_path.write_text(
                json.dumps(cuda_config, ensure_ascii=False),
                encoding="utf-8",
            )
            cuda_training_output = self.root / "classification-cuda-training"
            with mock.patch.object(torch.cuda, "is_available", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "CUDA|cuda|GPU"):
                    run_training(
                        cuda_config_path,
                        data_root,
                        cuda_training_output,
                        run_id="RUN-0034",
                        config_sha256="6" * 64,
                    )
            self.assertFalse(cuda_training_output.exists())

            incompatible_output = self.root / "classification-incompatible-cuda"
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
                        run_id="RUN-0035",
                        config_sha256="9" * 64,
                    )
            self.assertFalse(incompatible_output.exists())

            training_output = self.root / "classification-device-training"
            run = run_training(
                repository / "configs" / "baseline.json",
                data_root,
                training_output,
                run_id="RUN-0032",
                config_sha256="3" * 64,
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

            evaluation_output = self.root / "classification-cuda-evaluation"
            inference_output = self.root / "classification-cuda-inference.json"
            with mock.patch.object(torch.cuda, "is_available", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "CUDA|cuda|GPU"):
                    run_evaluation(
                        checkpoint_path,
                        data_root,
                        evaluation_output,
                        "val",
                        device="cuda",
                    )
                with self.assertRaisesRegex(RuntimeError, "CUDA|cuda|GPU"):
                    run_inference(
                        checkpoint_path,
                        data_root / "val" / "bright" / "0.png",
                        inference_output,
                        device="cuda",
                    )
            self.assertFalse(evaluation_output.exists())
            self.assertFalse(inference_output.exists())
        finally:
            for name in tuple(sys.modules):
                if name == "cls" or name.startswith("cls."):
                    sys.modules.pop(name, None)
            sys.path.remove(str(repository))

    def test_windows_cuda_environment_and_instructions_are_explicit(self) -> None:
        payload = (
            ROOT
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "cls-v1.0.0"
            / "payload"
        )
        adapter = self._load_adapter(payload, "cuda_environment")
        with mock.patch.dict(
            os.environ,
            {
                "CUDA_VISIBLE_DEVICES": "2",
                "CUBLAS_WORKSPACE_CONFIG": "wrong",
            },
            clear=False,
        ):
            environment = adapter._safe_environment(17)
        self.assertEqual("2", environment["CUDA_VISIBLE_DEVICES"])
        self.assertEqual(":4096:8", environment["CUBLAS_WORKSPACE_CONFIG"])

        readme = (payload / "README.md").read_text(encoding="utf-8")
        self.assertIn("https://pytorch.org/get-started/locally/", readme)
        self.assertIn("python -m cls.train", readme)
        self.assertIn("--device cuda", readme)
        self.assertIn("CUDA_VISIBLE_DEVICES", readme)
        self.assertIn("CUBLAS_WORKSPACE_CONFIG=:4096:8", readme)
        self.assertIn("synthetic", readme)
        cpu_lock = (
            payload / "requirements" / "windows-cpu.lock.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("https://download.pytorch.org/whl/cpu", cpu_lock)

    def test_split_names_links_and_large_inference_are_handled_safely(self) -> None:
        repository = self._create_repository("path-safety")
        data_root = self.root / "safe-images"
        for split in ("train", "val"):
            for class_name, level in (("dark", 10), ("bright", 240)):
                directory = data_root / split / class_name
                directory.mkdir(parents=True)
                Image.new("RGB", (8, 8), (level, level, level)).save(
                    directory / "0.png"
                )
        training = self.root / "safe-training"
        self._run_repo(
            repository,
            "-m",
            "cls.train",
            "--config",
            "configs/baseline.json",
            "--data-root",
            str(data_root),
            "--output-dir",
            str(training),
            "--device",
            "cpu",
        )
        checkpoint = training / "checkpoint.pt"
        invalid_splits = (".", "..", "/absolute", "C:drive", "a/b", "a\\b", "a\nb")
        for index, split in enumerate(invalid_splits):
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-X",
                    "utf8",
                    "-m",
                    "cls.evaluate",
                    "--checkpoint",
                    str(checkpoint),
                    "--data-root",
                    str(data_root),
                    "--output-dir",
                    str(self.root / f"unsafe-split-{index}"),
                    "--split",
                    split,
                ],
                cwd=repository,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
            )
            with self.subTest(split=repr(split)):
                self.assertNotEqual(0, result.returncode)
                self.assertFalse((self.root / f"unsafe-split-{index}").exists())

        linked = data_root / "val" / "bright" / "linked.png"
        try:
            linked.symlink_to(data_root / "val" / "bright" / "0.png")
        except OSError:
            linked = None
        if linked is not None:
            rejected = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-X",
                    "utf8",
                    "-m",
                    "cls.evaluate",
                    "--checkpoint",
                    str(checkpoint),
                    "--data-root",
                    str(data_root),
                    "--output-dir",
                    str(self.root / "linked-evaluation"),
                    "--split",
                    "val",
                ],
                cwd=repository,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
            )
            self.assertNotEqual(0, rejected.returncode)
            linked.unlink()

        inference_root = self.root / "many-inputs"
        inference_root.mkdir()
        for index in range(65):
            Image.new("RGB", (8, 8), (index, index, index)).save(
                inference_root / f"{index:03d}.png"
            )
        script = (
            "from pathlib import Path\n"
            "import torch\n"
            "from cls.infer import run_inference\n"
            "_stack=torch.stack\n"
            "def guarded(values,*args,**kwargs):\n"
            " values=list(values)\n"
            " assert len(values)<=32, f'oversized batch: {len(values)}'\n"
            " return _stack(values,*args,**kwargs)\n"
            "torch.stack=guarded\n"
            f"run_inference(Path({str(checkpoint)!r}),"
            f"Path({str(inference_root)!r}),"
            f"Path({str(self.root / 'many-predictions.json')!r}))\n"
        )
        self._run_repo(repository, "-c", script)
        self.assertEqual(
            65,
            len(read_json(self.root / "many-predictions.json")["predictions"]),
        )

    def test_synthetic_outputs_are_byte_deterministic_across_fresh_directories(
        self,
    ) -> None:
        repository = self._create_repository("deterministic-output")
        outputs = (self.root / "deterministic-a", self.root / "deterministic-b")
        for output in outputs:
            self._run_repo(
                repository,
                "-m",
                "cls.smoke",
                "--work-dir",
                str(output),
                "--device",
                "cpu",
            )
        for filename in (
            "checkpoint.pt",
            "metrics.json",
            "predictions.json",
            "run.json",
        ):
            with self.subTest(filename=filename):
                self.assertEqual(
                    hashlib.sha256((outputs[0] / filename).read_bytes()).hexdigest(),
                    hashlib.sha256((outputs[1] / filename).read_bytes()).hexdigest(),
                )

    def test_manifest_rebuilder_uses_each_pack_provenance_and_rejects_cache(
        self,
    ) -> None:
        tool_path = ROOT / "tools" / "rebuild_domain_pack_manifests.py"
        spec = importlib.util.spec_from_file_location(
            "_domain_pack_manifest_tool",
            tool_path,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        source = resolve_domain_pack("cls")
        copied = self.root / "copied-pack"
        shutil.copytree(source, copied)
        registry_entry = next(
            pack for pack in discover_domain_packs() if pack["id"] == "cls"
        )
        manifest = module._build_manifest(copied, registry_entry)
        provenance = read_json(copied / "payload" / "domain-pack.json")
        self.assertEqual(provenance["license"], manifest["license"])
        self.assertEqual(
            provenance["source_references"],
            manifest["source_references"],
        )

        cache = copied / "payload" / "__pycache__"
        cache.mkdir()
        (cache / "unsafe.pyc").write_bytes(b"\x00\x01binary")
        with self.assertRaisesRegex(ValueError, "cache|缓存|pyc|危险"):
            module._build_manifest(copied, registry_entry)

    def test_manifest_rebuilder_rejects_unsafe_registry_and_pack_paths_prewrite(
        self,
    ) -> None:
        tool_path = ROOT / "tools" / "rebuild_domain_pack_manifests.py"
        spec = importlib.util.spec_from_file_location(
            "_domain_pack_manifest_path_tool",
            tool_path,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        source_root = resolve_domain_pack("cls").parent
        copied_root = self.root / "domain-packs"
        shutil.copytree(source_root, copied_root)
        valid = next(
            pack for pack in discover_domain_packs() if pack["id"] == "cls"
        )
        pack_json = copied_root / valid["directory"] / "pack.json"
        original = pack_json.read_bytes()
        attacks = (
            dict(valid, directory="../escape"),
            dict(valid, directory="cls-v9.9.9"),
            dict(valid, template_id="PACK-DET"),
            dict(valid, primary_direction="det"),
            {key: value for key, value in valid.items() if key != "version"},
            dict(valid, unexpected="field"),
        )
        for attacked in attacks:
            with self.subTest(attacked=attacked):
                with self.assertRaisesRegex(
                    ValueError,
                    "registry|directory|目录|字段|身份",
                ):
                    module._rebuild_one(copied_root, attacked)
                self.assertEqual(original, pack_json.read_bytes())

        linked_root = self.root / "linked-domain-packs"
        try:
            linked_root.symlink_to(copied_root, target_is_directory=True)
        except OSError:
            linked_root = None
        if linked_root is not None:
            with self.assertRaisesRegex(ValueError, "link|reparse|目录"):
                module._rebuild_one(linked_root, valid)
            self.assertEqual(original, pack_json.read_bytes())

        hardlink = pack_json.with_name("pack-hardlink.json")
        try:
            os.link(pack_json, hardlink)
        except OSError:
            hardlink = None
        if hardlink is not None:
            replacement = pack_json.with_name("pack-original.json")
            pack_json.rename(replacement)
            hardlink.rename(pack_json)
            try:
                with self.assertRaisesRegex(ValueError, "hardlink|普通文件"):
                    module._rebuild_one(copied_root, valid)
                self.assertEqual(original, pack_json.read_bytes())
            finally:
                pack_json.unlink()
                replacement.rename(pack_json)


if __name__ == "__main__":
    unittest.main()
