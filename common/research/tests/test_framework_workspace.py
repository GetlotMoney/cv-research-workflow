from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from tests._helpers import SCRIPTS, cli_json

import sys

sys.path.insert(0, str(SCRIPTS))

from workflow_core.framework_workspace import (  # noqa: E402
    _normalize_equivalence,
    _verify_standardized_gzsl_source,
    create_framework_experiment,
    create_framework_idea,
    create_gzsl_repository,
    export_framework_paper_package,
    confirm_framework_run,
    import_standardized_gzsl_framework,
    prepare_experiment_worktree,
    promote_innovation_framework,
    register_gzsl_dataset,
    run_framework_experiment,
    validate_framework_workspace,
)
from rw import build_parser  # noqa: E402


class FrameworkWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "demo-gzsl"

    def _git(self, root: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return completed.stdout.strip()

    def _active_idea(self) -> str:
        idea = create_framework_idea(
            self.repository,
            title="DemoAlign",
            problem="基础映射对未见类的语义偏移缺少约束。",
            mechanism="在映射输出后增加残差式语义对齐。",
            falsifiable_hypothesis="相同数据划分下，H 高于父 Framework 基线。",
            source_notes=[
                {
                    "label": "用户提出",
                    "locator": "本项目讨论",
                    "claim": "原创假设，尚待实验验证",
                }
            ],
        )
        return idea["id"]

    @staticmethod
    def _tiny_gzsl_arrays() -> dict[str, np.ndarray]:
        class_ids = np.array([10, 20, 30, 40], dtype=np.int64)
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
                + np.array(
                    [sample * 0.01, -sample * 0.005, 0.0],
                    dtype=np.float32,
                )
                for index in range(4)
                for sample in range(3)
            ]
        ).astype(np.float32)
        return {
            "features": features,
            "labels": labels,
            "attributes": attributes,
            "class_ids": class_ids,
            "seen_class_ids": np.array([10, 20], dtype=np.int64),
            "unseen_class_ids": np.array([30, 40], dtype=np.int64),
            "train_indices": np.array([0, 1, 3, 4], dtype=np.int64),
            "test_seen_indices": np.array([2, 5], dtype=np.int64),
            "test_unseen_indices": np.array(
                [6, 7, 8, 9, 10, 11],
                dtype=np.int64,
            ),
        }

    def _formal_config(
        self,
        worktree: Path,
        experiment_id: str,
    ) -> dict[str, object]:
        source = self.root / "tiny-gzsl.npz"
        np.savez(source, **self._tiny_gzsl_arrays())
        registered = register_gzsl_dataset(
            self.repository,
            experiment_id=experiment_id,
            worktree=worktree,
            source_path=source,
            dataset_slug="tiny-gzsl",
            dataset_id="tiny-gzsl-gpu",
            version="2026.07",
            source_uri="https://example.org/datasets/tiny-gzsl-gpu",
            license_name="test-only",
        )
        self.assertEqual(str(source), registered["config"]["data_path"])
        self.assertFalse(
            (worktree / "data" / "managed" / "tiny-gzsl.npz").exists()
        )
        self.assertEqual(
            "data/manifests/tiny-gzsl.json",
            self._git(worktree, "ls-files", "data"),
        )
        self.assertEqual(
            "external_local_file",
            registered["dataset"]["storage"],
        )
        return registered["config"]

    @staticmethod
    def _route_contract(route: str) -> dict[str, object]:
        if route == "tuning":
            return {"allowed_changes": ["learning_rate"]}
        if route == "reproduction":
            return {
                "source_ref": "https://example.org/paper",
                "source_run_ref": "reported-table-1",
                "tolerance": 1e-6,
                "code_standard": {"commit": "official-release"},
                "data_standard": {"split": "official-split"},
                "baseline": 0.0,
                "primary_metric": "H",
            }
        if route == "ablation":
            return {
                "module_ref": "semantic-projection",
                "baseline_run_ref": "confirmed-enabled-run",
                "disabled_behavior": "使用恒等映射代替该模块",
                "baseline": 0.0,
                "primary_metric": "H",
            }
        return {
            "baseline_run_ref": "confirmed-parent-run",
            "baseline": 0.0,
            "primary_metric": "H",
            "minimum_delta": 0.0,
        }

    def test_create_repository_has_one_gpu_only_ledger_and_base_framework(self) -> None:
        result = create_gzsl_repository(
            self.repository,
            name="demo-gzsl",
            environment_name="dvsr_gpu",
        )

        control = self.repository / ".experiment-workflow"
        framework = (
            control / "frameworks" / "gzsl-base" / "framework.json"
        )
        record = json.loads(framework.read_text(encoding="utf-8"))
        repository = json.loads(
            (control / "repository.json").read_text(encoding="utf-8")
        )

        self.assertEqual("gzsl", repository["direction"])
        self.assertEqual("gpu_only", repository["execution_policy"])
        self.assertEqual("dvsr_gpu", repository["environment_name"])
        self.assertEqual("gzsl-base", repository["active_framework"])
        self.assertEqual("gzsl-base", result["framework"]["slug"])
        self.assertIsNone(record["parent_framework"])
        self.assertIsNone(record["idea_ref"])
        self.assertEqual("stable", record["status"])
        self.assertEqual(
            self._git(self.repository, "rev-parse", "HEAD"),
            record["commit"],
        )
        self.assertEqual(
            record["commit"],
            self._git(
                self.repository,
                "rev-parse",
                "refs/tags/framework/gzsl-base/v1.0.0^{commit}",
            ),
        )
        for route in ("reproduction", "tuning", "ablation", "innovation"):
            self.assertTrue(
                (
                    control
                    / "frameworks"
                    / "gzsl-base"
                    / "experiments"
                    / route
                ).is_dir()
            )
        validation = validate_framework_workspace(self.repository)
        self.assertEqual("pass", validation["status"])
        self.assertEqual(1, validation["counts"]["frameworks"])
        self.assertEqual(0, validation["counts"]["framework_html"])
        self.assertEqual(
            "cuda",
            json.loads(
                (self.repository / "configs" / "smoke.json").read_text(
                    encoding="utf-8"
                )
            )["device"],
        )
        self.assertTrue(
            (
                self.repository
                / "requirements"
                / "windows-gpu.lock.txt"
            ).is_file()
        )
        self.assertFalse(
            (
                self.repository
                / "requirements"
                / "windows-cpu.lock.txt"
            ).exists()
        )
        self.assertEqual(
            "",
            self._git(
                self.repository,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ),
        )

    def test_import_standardized_external_gzsl_as_top_level_framework(
        self,
    ) -> None:
        create_gzsl_repository(
            self.repository,
            name="demo-gzsl",
            environment_name="dvsr_gpu",
        )
        source = self.root / "external-gzsl"
        create_gzsl_repository(
            source,
            name="external-gzsl",
            environment_name="dvsr_gpu",
        )
        evidence_root = source / "standardization" / "evidence"
        evidence_root.mkdir(parents=True)
        generator = evidence_root / "generate_equivalence.py"
        generator.write_text(
            """
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--result", required=True)
parser.add_argument("--artifact", required=True)
parser.add_argument("--code-commit", required=True)
args = parser.parse_args()
artifact = Path(args.artifact)
artifact.parent.mkdir(parents=True, exist_ok=True)
artifact.write_text(f"verified output for {Path(args.result).name}\\n", encoding="utf-8")
command = [
    "python",
    "standardization/evidence/generate_equivalence.py",
    "--result",
    args.result,
    "--artifact",
    args.artifact,
    "--code-commit",
    args.code_commit,
]
payload = {
    "schema": "cv-experiment-workflow.equivalence-result.v1",
    "dataset_id": "same-split",
    "dataset_manifest_sha256": "a" * 64,
    "metrics": {"S": 0.5, "U": 0.4, "H": 0.444444},
    "command": command,
    "code_commit": args.code_commit,
    "output_artifact": args.artifact,
    "output_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
}
result = Path(args.result)
result.parent.mkdir(parents=True, exist_ok=True)
result.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
""".strip()
            + "\n",
            encoding="utf-8",
        )
        self._git(source, "add", "standardization/evidence/generate_equivalence.py")
        self._git(
            source,
            "-c",
            "user.name=Framework Test",
            "-c",
            "user.email=framework@example.com",
            "commit",
            "-m",
            "test: add deterministic equivalence generator",
        )
        code_commit = self._git(source, "rev-parse", "HEAD")
        for name in ("original.json", "standardized.json"):
            artifact_name = name.replace(".json", ".output")
            result_relative = f"standardization/evidence/{name}"
            artifact_relative = f"standardization/evidence/{artifact_name}"
            completed = subprocess.run(
                [
                    sys.executable,
                    "standardization/evidence/generate_equivalence.py",
                    "--result",
                    result_relative,
                    "--artifact",
                    artifact_relative,
                    "--code-commit",
                    code_commit,
                ],
                cwd=source,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
        self._git(source, "add", "standardization/evidence")
        self._git(
            source,
            "-c",
            "user.name=Framework Test",
            "-c",
            "user.email=framework@example.com",
            "commit",
            "-m",
            "test: add equivalence evidence",
        )
        imported = import_standardized_gzsl_framework(
            self.repository,
            source_repository=source,
            framework_slug="external-base",
            title="External GZSL Base",
            version="1.0.0",
            component_map={
                "data": ["gzsl/data.py"],
                "model": ["gzsl/model.py"],
                "losses": ["gzsl/model.py", "gzsl/train.py"],
                "trainer": ["gzsl/train.py"],
                "evaluator": ["gzsl/evaluate.py"],
                "inferencer": ["gzsl/infer.py"],
                "metrics": ["gzsl/metrics.py"],
                "configs": ["configs/baseline.json"],
            },
            equivalence={
                "dataset_id": "same-split",
                "tolerance": 1e-6,
                "original_result_path": (
                    "standardization/evidence/original.json"
                ),
                "standardized_result_path": (
                    "standardization/evidence/standardized.json"
                ),
                "checked_by": ["demo-user", "codex"],
            },
            trusted_execution_acknowledged=True,
        )
        framework = imported["framework"]
        self.assertIsNone(framework["parent_framework"])
        self.assertIsNone(framework["idea_ref"])
        self.assertEqual("pass", imported["source"]["equivalence"]["status"])
        self.assertEqual("pass", imported["source"]["verification"]["status"])
        for command_record in imported["source"]["verification"]["commands"]:
            self.assertNotIn("stdout_tail", command_record)
            self.assertNotIn("stderr_tail", command_record)
        for evidence_record in imported["source"]["equivalence"][
            "evidence"
        ].values():
            self.assertEqual(
                {
                    "status",
                    "returncode",
                    "result_sha256",
                    "output_sha256",
                },
                set(evidence_record["replay"]),
            )
        self.assertEqual(
            framework["commit"],
            self._git(
                self.repository,
                "rev-parse",
                "refs/tags/framework/external-base/v1.0.0^{commit}",
            ),
        )
        self.assertEqual(
            "pass",
            validate_framework_workspace(self.repository)["status"],
        )
        first = create_framework_experiment(
            self.repository,
            framework_slug="gzsl-base",
            route="tuning",
            slug="same-name",
            title="基础框架调参",
            summary="验证跨 Framework 编号不会冲突。",
            route_contract=self._route_contract("tuning"),
        )
        second = create_framework_experiment(
            self.repository,
            framework_slug="external-base",
            route="tuning",
            slug="same-name",
            title="外来框架调参",
            summary="验证跨 Framework 编号不会冲突。",
            route_contract=self._route_contract("tuning"),
        )
        self.assertEqual("tune-001-same-name", first["id"])
        self.assertEqual("tune-002-same-name", second["id"])
        self.assertTrue(
            prepare_experiment_worktree(
                self.repository,
                experiment_id=second["id"],
            ).is_dir()
        )

    def test_external_verifiers_and_equivalence_must_be_replayable(self) -> None:
        source = self.root / "external-verification"
        create_gzsl_repository(
            source,
            name="external-verification",
            environment_name="dvsr_gpu",
        )
        probe = source / "gzsl" / "cuda_probe.py"
        probe.write_text(
            'print(\'{"status":"pass","cuda":true,"device":"FAKE GPU"}\')\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "保持通用模板原文"):
            _verify_standardized_gzsl_source(
                source,
                trusted_execution_acknowledged=True,
            )
        self._git(source, "checkout", "--", "gzsl/cuda_probe.py")

        evidence = source / "standardization" / "evidence"
        evidence.mkdir(parents=True)
        code_commit = self._git(source, "rev-parse", "HEAD")
        import hashlib

        for name in ("original", "standardized"):
            artifact = evidence / f"{name}.output"
            artifact.write_text("hand-written\n", encoding="utf-8")
            (evidence / f"{name}.json").write_text(
                json.dumps(
                    {
                        "schema": "cv-experiment-workflow.equivalence-result.v1",
                        "dataset_id": "same-split",
                        "dataset_manifest_sha256": "a" * 64,
                        "metrics": {"S": 0.5, "U": 0.4, "H": 0.444444},
                        "command": ["python", "definitely-not-a-real-command.py"],
                        "code_commit": code_commit,
                        "output_artifact": (
                            f"standardization/evidence/{name}.output"
                        ),
                        "output_sha256": hashlib.sha256(
                            artifact.read_bytes()
                        ).hexdigest(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        self._git(source, "add", "standardization/evidence")
        self._git(
            source,
            "-c",
            "user.name=Framework Test",
            "-c",
            "user.email=framework@example.com",
            "commit",
            "-m",
            "test: add hand-written fake evidence",
        )
        with self.assertRaisesRegex(RuntimeError, "重放失败"):
            _normalize_equivalence(
                source,
                {
                    "dataset_id": "same-split",
                    "tolerance": 1e-6,
                    "original_result_path": (
                        "standardization/evidence/original.json"
                    ),
                    "standardized_result_path": (
                        "standardization/evidence/standardized.json"
                    ),
                    "checked_by": ["reviewer-a", "reviewer-b"],
                },
            )
    def test_four_routes_use_local_readable_ids_and_only_innovation_needs_idea(
        self,
    ) -> None:
        create_gzsl_repository(
            self.repository,
            name="demo-gzsl",
            environment_name="dvsr_gpu",
        )
        idea_ref = self._active_idea()

        reproduction = create_framework_experiment(
            self.repository,
            framework_slug="gzsl-base",
            route="reproduction",
            slug="cub-baseline",
            title="复现 CUB 基线",
            summary="固定数据划分和 S/U/H 指标。",
            route_contract=self._route_contract("reproduction"),
        )
        tuning = create_framework_experiment(
            self.repository,
            framework_slug="gzsl-base",
            route="tuning",
            slug="learning-rate",
            title="学习率调参",
            summary="只改变学习率，不改变代码行为。",
            route_contract=self._route_contract("tuning"),
        )
        ablation = create_framework_experiment(
            self.repository,
            framework_slug="gzsl-base",
            route="ablation",
            slug="disable-projection",
            title="关闭投影层",
            summary="保持数据和评估不变。",
            route_contract=self._route_contract("ablation"),
        )
        innovation = create_framework_experiment(
            self.repository,
            framework_slug="gzsl-base",
            route="innovation",
            slug="demo-align",
            title="DemoAlign 创新",
            summary="基于 Idea 创建代码子路线。",
            idea_ref=idea_ref,
            route_contract=self._route_contract("innovation"),
        )

        self.assertEqual("repro-001-cub-baseline", reproduction["id"])
        self.assertEqual("tune-001-learning-rate", tuning["id"])
        self.assertEqual("ablate-001-disable-projection", ablation["id"])
        self.assertEqual("innov-001-demo-align", innovation["id"])
        self.assertEqual(idea_ref, innovation["idea_ref"])
        self.assertIsNone(reproduction["idea_ref"])
        self.assertIsNone(tuning["idea_ref"])
        self.assertIsNone(ablation["idea_ref"])
        idea = json.loads(
            (
                self.repository
                / ".experiment-workflow"
                / "idea-tree"
                / idea_ref
                / "idea.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual([innovation["id"]], idea["experiment_refs"])

        second_tuning = create_framework_experiment(
            self.repository,
            framework_slug="gzsl-base",
            route="tuning",
            slug="weight-decay",
            title="权重衰减调参",
            summary="同一 Framework 内按调参类别单独编号。",
            route_contract=self._route_contract("tuning"),
        )
        self.assertEqual("tune-002-weight-decay", second_tuning["id"])

        with self.assertRaisesRegex(ValueError, "Idea"):
            create_framework_experiment(
                self.repository,
                framework_slug="gzsl-base",
                route="innovation",
                slug="missing-idea",
                title="缺少 Idea",
                summary="必须失败。",
                route_contract=self._route_contract("innovation"),
            )
        with self.assertRaisesRegex(ValueError, "Idea"):
            create_framework_experiment(
                self.repository,
                framework_slug="gzsl-base",
                route="tuning",
                slug="wrong-idea",
                title="错误绑定",
                summary="非创新实验不能绑定 Idea。",
                idea_ref=idea_ref,
                route_contract=self._route_contract("tuning"),
            )

    def test_innovation_cannot_promote_before_confirmed_run(self) -> None:
        create_gzsl_repository(
            self.repository,
            name="demo-gzsl",
            environment_name="dvsr_gpu",
        )
        idea_ref = self._active_idea()
        innovation = create_framework_experiment(
            self.repository,
            framework_slug="gzsl-base",
            route="innovation",
            slug="demo-align",
            title="DemoAlign 创新",
            summary="在父 Framework 上修改。",
            idea_ref=idea_ref,
            route_contract=self._route_contract("innovation"),
        )
        worktree = prepare_experiment_worktree(
            self.repository,
            experiment_id=innovation["id"],
        )
        model = worktree / "gzsl" / "model.py"
        model.write_text(
            model.read_text(encoding="utf-8")
            + "\n# DemoAlign innovation marker\n",
            encoding="utf-8",
        )
        self._git(worktree, "add", "gzsl/model.py")
        self._git(
            worktree,
            "-c",
            "user.name=Framework Test",
            "-c",
            "user.email=framework@example.com",
            "commit",
            "-m",
            "feat: add DemoAlign child framework",
        )

        with self.assertRaisesRegex(ValueError, "evidence Run.*人工确认"):
            promote_innovation_framework(
                self.repository,
                experiment_id=innovation["id"],
                worktree=worktree,
                child_slug="demo-align-v1",
                title="DemoAlign V1",
                version="1.0.0",
                framework_view={
                    "nodes": [
                        {"id": "visual", "label": "视觉特征"},
                        {"id": "align", "label": "DemoAlign 对齐"},
                    ],
                    "edges": [{"from": "visual", "to": "align"}],
                },
            )
        record_path = (
            self.repository
            / ".experiment-workflow"
            / "frameworks"
            / "gzsl-base"
            / "experiments"
            / "innovation"
            / innovation["id"]
            / "experiment.json"
        )
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["status"] = "confirmed"
        record["innovation_decision"] = {
            "accepted": False,
            "meets_rule": False,
            "local_run_id": "run-001-seed-31",
        }
        record_path.write_text(
            json.dumps(record, ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "判定成功.*预设门槛"):
            promote_innovation_framework(
                self.repository,
                experiment_id=innovation["id"],
                worktree=worktree,
                child_slug="demo-align-v1",
                title="DemoAlign V1",
                version="1.0.0",
                framework_view={
                    "nodes": [
                        {"id": "visual", "label": "视觉特征"},
                        {"id": "align", "label": "DemoAlign 对齐"},
                    ],
                    "edges": [{"from": "visual", "to": "align"}],
                },
            )

    def test_cli_creates_repository_idea_and_readable_innovation(self) -> None:
        created = cli_json(
            "create-gzsl-repository",
            "--destination",
            self.repository,
            "--name",
            "demo-gzsl",
            "--environment",
            "dvsr_gpu",
        )
        self.assertEqual("gzsl-base", created["framework"]["slug"])

        idea = cli_json(
            "create-framework-idea",
            "--repository",
            self.repository,
            "--title",
            "CLI DemoAlign",
            "--problem",
            "语义偏移缺少约束。",
            "--mechanism",
            "增加残差式语义对齐。",
            "--hypothesis",
            "相同设置下 H 高于父框架。",
            "--source-notes",
            json.dumps(
                [
                    {
                        "label": "用户提出",
                        "locator": "CLI 测试",
                        "claim": "原创假设",
                    }
                ],
                ensure_ascii=False,
            ),
        )
        experiment = cli_json(
            "create-framework-experiment",
            "--repository",
            self.repository,
            "--framework",
            "gzsl-base",
            "--route",
            "innovation",
            "--slug",
            "cli-demo-align",
            "--title",
            "CLI DemoAlign",
            "--summary",
            "CLI 建立创新实验。",
            "--idea",
            idea["id"],
            "--route-contract",
            json.dumps(self._route_contract("innovation"), ensure_ascii=False),
        )

        self.assertEqual("innov-001-cli-demo-align", experiment["id"])
        self.assertEqual(idea["id"], experiment["idea_ref"])

    def test_cli_parser_exposes_gpu_run_and_confirmation(self) -> None:
        parser = build_parser()
        run = parser.parse_args(
            [
                "run-framework-experiment",
                "--repository",
                str(self.repository),
                "--experiment",
                "tune-001-learning-rate",
                "--worktree",
                str(self.repository / ".worktrees" / "tune-001-learning-rate"),
                "--config-json",
                json.dumps({"device": "cuda"}),
                "--seed",
                "31",
                "--purpose",
                "evidence",
            ]
        )
        confirm = parser.parse_args(
            [
                "confirm-framework-run",
                "--repository",
                str(self.repository),
                "--experiment",
                "tune-001-learning-rate",
                "--run",
                "run-001-seed-31",
                "--reason",
                "已复核",
                "--proposed-by",
                "workflow",
                "--checked-by",
                "reviewer",
            ]
        )
        export_package = parser.parse_args(
            [
                "export-framework-paper-package",
                "--repository",
                str(self.repository),
                "--experiment",
                "tune-001-learning-rate",
                "--run",
                "run-001-seed-31",
                "--research-brief-json",
                json.dumps(
                    {
                        "title": "测试",
                        "research_area": "GZSL",
                        "background": "背景",
                        "problem": "问题",
                        "objective": "目标",
                        "research_questions": ["问题"],
                        "hypotheses": [],
                        "scope": {"included": [], "excluded": []},
                        "terminology": [],
                        "planned_contributions": [],
                    }
                ),
                "--claim",
                "已核验结果",
                "--confirmed-by",
                "demo-user",
            ]
        )
        dataset = parser.parse_args(
            [
                "register-gzsl-dataset",
                "--repository",
                str(self.repository),
                "--experiment",
                "tune-001-learning-rate",
                "--worktree",
                str(self.repository / ".worktrees" / "tune-001-learning-rate"),
                "--source",
                str(self.root / "tiny.npz"),
                "--slug",
                "tiny",
                "--dataset-id",
                "tiny-gzsl",
                "--version",
                "1",
                "--source-uri",
                "https://example.org/tiny.npz",
                "--license",
                "test-only",
            ]
        )
        validation = parser.parse_args(
            [
                "validate-framework-workspace",
                "--repository",
                str(self.repository),
            ]
        )

        self.assertEqual("_handle_run_framework_experiment", run.handler.__name__)
        self.assertEqual("_handle_confirm_framework_run", confirm.handler.__name__)
        self.assertEqual(
            "_handle_export_framework_paper_package",
            export_package.handler.__name__,
        )
        self.assertEqual("_handle_register_gzsl_dataset", dataset.handler.__name__)
        self.assertEqual(
            "_handle_validate_framework_workspace",
            validation.handler.__name__,
        )

    def test_reproduction_and_ablation_routes_reach_real_gpu_debug(self) -> None:
        create_gzsl_repository(
            self.repository,
            name="demo-gzsl",
            environment_name="dvsr_gpu",
        )
        for route in ("reproduction", "ablation"):
            experiment = create_framework_experiment(
                self.repository,
                framework_slug="gzsl-base",
                route=route,
                slug=f"{route}-gpu",
                title=f"{route} GPU",
                summary="验证该路线不是只有名字，而是真能执行。",
                route_contract=self._route_contract(route),
            )
            worktree = prepare_experiment_worktree(
                self.repository,
                experiment_id=experiment["id"],
            )
            config = self._formal_config(worktree, experiment["id"])
            run = run_framework_experiment(
                self.repository,
                experiment_id=experiment["id"],
                worktree=worktree,
                config=config,
                seed=31,
                purpose="debug",
            )
            self.assertEqual("debug", run["evidence_level"])
            self.assertEqual("cuda", run["device"])

    def test_concurrent_dataset_registrations_are_serialized(self) -> None:
        create_gzsl_repository(
            self.repository,
            name="demo-gzsl",
            environment_name="dvsr_gpu",
        )
        experiment = create_framework_experiment(
            self.repository,
            framework_slug="gzsl-base",
            route="tuning",
            slug="dataset-concurrency",
            title="数据登记并发",
            summary="两个数据清单不能同时争用 Git index。",
            route_contract=self._route_contract("tuning"),
        )
        worktree = prepare_experiment_worktree(
            self.repository,
            experiment_id=experiment["id"],
        )
        sources: list[Path] = []
        for suffix in ("a", "b"):
            source = self.root / f"tiny-{suffix}.npz"
            np.savez(source, **self._tiny_gzsl_arrays())
            sources.append(source)

        def register(index: int) -> dict[str, object]:
            suffix = ("a", "b")[index]
            return register_gzsl_dataset(
                self.repository,
                experiment_id=experiment["id"],
                worktree=worktree,
                source_path=sources[index],
                dataset_slug=f"tiny-{suffix}",
                dataset_id=f"tiny-{suffix}",
                version="1",
                source_uri=f"https://example.org/tiny-{suffix}.npz",
                license_name="test-only",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(register, range(2)))
        self.assertEqual(2, len(results))
        self.assertEqual(
            ["tiny-a", "tiny-b"],
            sorted(
                json.loads(
                    (
                        self.repository
                        / ".experiment-workflow"
                        / "frameworks"
                        / "gzsl-base"
                        / "experiments"
                        / "tuning"
                        / experiment["id"]
                        / "experiment.json"
                    ).read_text(encoding="utf-8")
                )["dataset_refs"]
            ),
        )
        self.assertEqual("", self._git(worktree, "status", "--porcelain=v1"))

    def test_gpu_train_evaluate_infer_becomes_local_run_and_confirmed_evidence(
        self,
    ) -> None:
        create_gzsl_repository(
            self.repository,
            name="demo-gzsl",
            environment_name="dvsr_gpu",
        )
        baseline_experiment = create_framework_experiment(
            self.repository,
            framework_slug="gzsl-base",
            route="tuning",
            slug="confirmed-baseline",
            title="已确认父框架基线",
            summary="先生成真实基线，创新不能自行填写虚构成绩。",
            route_contract=self._route_contract("tuning"),
        )
        baseline_worktree = prepare_experiment_worktree(
            self.repository,
            experiment_id=baseline_experiment["id"],
        )
        baseline_config = self._formal_config(
            baseline_worktree,
            baseline_experiment["id"],
        )
        run_framework_experiment(
            self.repository,
            experiment_id=baseline_experiment["id"],
            worktree=baseline_worktree,
            config=baseline_config,
            seed=31,
            purpose="debug",
        )
        baseline_run = run_framework_experiment(
            self.repository,
            experiment_id=baseline_experiment["id"],
            worktree=baseline_worktree,
            config=baseline_config,
            seed=31,
            purpose="evidence",
        )
        confirm_framework_run(
            self.repository,
            experiment_id=baseline_experiment["id"],
            local_run_id=baseline_run["id"],
            reason="已复核父 Framework 的真实基线结果。",
            proposed_by="workflow",
            checked_by="baseline-review",
        )
        idea_ref = self._active_idea()
        innovation_contract = self._route_contract("innovation")
        innovation_contract.update(
            {
                "baseline_run_ref": (
                    f"{baseline_experiment['id']}/{baseline_run['id']}"
                ),
                "baseline": baseline_run["metrics"]["H"],
            }
        )
        experiment = create_framework_experiment(
            self.repository,
            framework_slug="gzsl-base",
            route="innovation",
            slug="gpu-confirmed-promotion",
            title="GPU 确认后晋级",
            summary="验证 train、evaluate、infer、确认和创新晋级。",
            idea_ref=idea_ref,
            route_contract=innovation_contract,
        )
        worktree = prepare_experiment_worktree(
            self.repository,
            experiment_id=experiment["id"],
        )
        model = worktree / "gzsl" / "model.py"
        model.write_text(
            model.read_text(encoding="utf-8")
            + "\n# confirmed innovation promotion marker\n",
            encoding="utf-8",
        )
        self._git(worktree, "add", "gzsl/model.py")
        self._git(
            worktree,
            "-c",
            "user.name=Framework Test",
            "-c",
            "user.email=framework@example.com",
            "commit",
            "-m",
            "feat: add confirmed innovation",
        )
        config = self._formal_config(worktree, experiment["id"])

        debug = run_framework_experiment(
            self.repository,
            experiment_id=experiment["id"],
            worktree=worktree,
            config=config,
            seed=31,
            purpose="debug",
        )
        with self.assertRaisesRegex(ValueError, "完全相同的配置和 seed"):
            run_framework_experiment(
                self.repository,
                experiment_id=experiment["id"],
                worktree=worktree,
                config={**config, "batch_size": 999},
                seed=999,
                purpose="evidence",
            )
        evidence = run_framework_experiment(
            self.repository,
            experiment_id=experiment["id"],
            worktree=worktree,
            config=config,
            seed=31,
            purpose="evidence",
        )
        confirmed = confirm_framework_run(
            self.repository,
            experiment_id=experiment["id"],
            local_run_id=evidence["id"],
            reason="GPU 训练、评估、推理和输出哈希均已复核。",
            proposed_by="workflow",
            checked_by="independent-review",
            innovation_accepted=True,
            innovation_conclusion="H 达到预设门槛，接受该创新。",
        )
        self.assertEqual(
            baseline_run["id"],
            confirmed["innovation_decision"]["baseline_evidence"][
                "local_run_id"
            ],
        )
        self.assertEqual(
            baseline_run["metrics"]["H"],
            confirmed["innovation_decision"]["baseline"],
        )
        child = promote_innovation_framework(
            self.repository,
            experiment_id=experiment["id"],
            worktree=worktree,
            child_slug="gpu-confirmed-v1",
            title="GPU Confirmed V1",
            version="1.0.0",
            framework_view={
                "nodes": [
                    {"id": "visual", "label": "视觉特征"},
                    {"id": "innovation", "label": "已确认创新"},
                    {"id": "semantic", "label": "语义预测"},
                ],
                "edges": [
                    {"from": "visual", "to": "innovation"},
                    {"from": "innovation", "to": "semantic"},
                ],
            },
        )
        paper_package = export_framework_paper_package(
            self.repository,
            experiment_id=experiment["id"],
            local_run_id=evidence["id"],
            research_brief={
                "title": "Tiny GZSL GPU 测试",
                "research_area": "广义零样本学习",
                "background": "检验科研证据能否完整交给论文工作流。",
                "problem": "未经封存和复核的实验数字不能用于写论文。",
                "objective": "验证 GPU 实验、证据封存和论文交付接口。",
                "research_questions": ["正式 GPU Run 能否形成可核查交付包？"],
                "hypotheses": [
                    {
                        "hypothesis_id": "H-01",
                        "statement": "完整封存并复核的 Run 可以进入论文交付包。",
                        "falsification_criteria": "任一导出或校验步骤失败。",
                    }
                ],
                "scope": {
                    "included": ["GPU 链路和交付接口"],
                    "excluded": ["正式论文成绩", "投稿终稿"],
                },
                "terminology": [
                    {
                        "term_en": "generalized zero-shot learning",
                        "term_zh": "广义零样本学习",
                        "definition_zh": "同时在已见类和未见类上进行分类。",
                    }
                ],
                "planned_contributions": [
                    {
                        "contribution_id": "C-01",
                        "statement_zh": "建立 GZSL 科研证据到 PaperFlow 的可核查接口。",
                        "provenance": "original",
                        "source_refs": [],
                    }
                ],
            },
            claim_statement_zh=(
                "Tiny GZSL GPU 测试已产出 S、U、H；"
                "该合成小数据只验证系统链路，不是论文成绩。"
            ),
            confirmed_by="workflow-test",
        )

        self.assertEqual("run-001-seed-31", debug["id"])
        self.assertEqual("debug", debug["evidence_level"])
        self.assertEqual("cuda", debug["device"])
        self.assertEqual("run-002-seed-31", evidence["id"])
        self.assertEqual("single_run", evidence["evidence_level"])
        self.assertEqual({"S", "U", "H"}, set(evidence["metrics"]))
        self.assertEqual("confirmed", confirmed["evidence_level"])
        self.assertFalse(confirmed["cpu_fallback"])
        self.assertEqual("gpu-confirmed-v1", child["slug"])
        self.assertEqual("stable", child["status"])
        self.assertEqual(
            child["commit"],
            self._git(
                self.repository,
                "rev-parse",
                "refs/tags/framework/gpu-confirmed-v1/v1.0.0^{commit}",
            ),
        )
        experiment_dir = (
            self.repository
            / ".experiment-workflow"
            / "frameworks"
            / "gzsl-base"
            / "experiments"
            / "innovation"
            / experiment["id"]
        )
        self.assertEqual(
            [experiment_dir / "framework.html"],
            list(
                (
                    self.repository / ".experiment-workflow"
                ).rglob("framework.html")
            ),
        )
        self.assertEqual("paper_ready", paper_package["readiness"])
        self.assertTrue(Path(paper_package["package_path"]).is_dir())
        self.assertTrue(
            (Path(paper_package["package_path"]) / "manifest.json").is_file()
        )
        run_directory = (
            self.repository
            / ".experiment-workflow"
            / "frameworks"
            / "gzsl-base"
            / "experiments"
            / experiment["route"]
            / experiment["id"]
            / "runs"
            / evidence["id"]
        )
        self.assertTrue((run_directory / "run.json").is_file())
        validation = validate_framework_workspace(self.repository)
        self.assertEqual("pass", validation["status"])
        self.assertEqual(2, validation["counts"]["frameworks"])
        self.assertEqual(1, validation["counts"]["framework_html"])
        self.assertEqual(4, validation["counts"]["runs"])
        self.assertEqual(2, validation["counts"]["confirmed_runs"])
        seal = evidence["output_seal"]
        output = (
            self.repository
            / seal["root"]["relative_path"]
            / seal["source"]["relative_path"]
            / evidence["kernel_run_id"]
        )
        produced = json.loads(
            (output / "run.json").read_text(encoding="utf-8")
        )
        self.assertEqual("cuda", produced["device"])
        self.assertTrue(produced["checkpoint_reloaded_for_evaluation"])
        self.assertTrue(produced["checkpoint_reloaded_for_inference"])


if __name__ == "__main__":
    unittest.main()
