from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tests._helpers import CLI, SCRIPTS, cli_json, read_json, run_cli
from tests.test_research_brief import research_brief_manifest


sys.path.insert(0, str(SCRIPTS))
from workflow_core.evidence import record_evidence_transition  # noqa: E402
from workflow_core import paper_package as paper_package_module  # noqa: E402
from workflow_core.paper_package import seal_paper_package  # noqa: E402
from workflow_core.planning import status  # noqa: E402
from workflow_core.project_templates import register_template  # noqa: E402
from workflow_core.project import init_project  # noqa: E402
from workflow_core.runs import create_run, finish_run, start_run  # noqa: E402
from workflow_core.tasking import (  # noqa: E402
    create_task,
    load_task,
    readiness,
    transition_task,
)
from workflow_core.v2_catalog import (  # noqa: E402
    register_module,
    register_source,
    save_idea,
)


PACKAGE_FILES = {
    "package.json",
    "study.json",
    "claims.jsonl",
    "experiments.json",
    "sources.jsonl",
    "assets.jsonl",
    "visuals.json",
    "checksums.sha256",
}


class PaperPackageV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "paper-project"
        init_project(self.project, "paper-project", layout="v2")
        self.control = self.project / ".experiment-workflow"

        brief_path = self.root / "brief.json"
        brief_path.write_text(
            json.dumps(research_brief_manifest(), ensure_ascii=False),
            encoding="utf-8",
        )
        self.brief = cli_json(
            "save-research-brief",
            "--project",
            self.project,
            "--manifest",
            brief_path,
        )

    def _confirmed_run(
        self,
        *,
        final_level: str = "confirmed",
        purpose: str = "evidence",
        outcome: str = "succeeded",
        artifact_tag: str = "",
    ) -> tuple[dict[str, object], dict[str, object]]:
        task = create_task(
            self.project,
            owner_request="确认论文结果",
            route="tune",
            target_refs=["VER-0001", "baseline"],
            route_inputs={
                "debug_required": False,
                "primary_metric": "score",
            },
            budget={"max_runs": 1},
            stop_condition={"max_failures": 1},
        )
        transition_task(self.project, task["id"], "preparing")
        self.assertEqual(
            {"status": "pass"},
            readiness(
                self.project,
                task["id"],
                target_clear=True,
                template_runnable=True,
                mutation_allowed=True,
                code_verified=True,
            ),
        )
        task = load_task(self.project, task["id"])
        # 这组历史包回归需要构造 1.2/1.3 已存在的未绑定 Run；
        # 只在测试进程内绕过“禁止新建未绑定 evidence”入口，不改变生产规则。
        with mock.patch(
            "workflow_core.runs._require_bound_for_new_evidence"
        ):
            run = create_run(
                self.project,
                task["id"],
                {
                    "code": {
                        "repository": "local",
                        "commit": "a" * 40,
                    },
                    "config": {
                        "metric_definition": {
                            "score": "测试集上的平均准确率",
                        },
                    },
                    "seed": 7,
                    "data": {
                        "dataset_id": "demo",
                        "version": "1",
                        "split": "test",
                    },
                    "environment": {"python": "3.10"},
                },
                purpose=purpose,
            )
        (self.project / "artifacts").mkdir(exist_ok=True)
        run_name = f"{artifact_tag}-run.log" if artifact_tag else "run.log"
        metrics_name = (
            f"{artifact_tag}-metrics.json"
            if artifact_tag
            else "metrics.json"
        )
        artifact = self.project / "artifacts" / run_name
        artifact.write_text("score=0.75\n", encoding="utf-8")
        metrics_artifact = self.project / "artifacts" / metrics_name
        metrics_artifact.write_text('{"score":0.75}\n', encoding="utf-8")
        start_run(self.project, run["id"], process_id=123)
        run = finish_run(
            self.project,
            run["id"],
            outcome=outcome,
            exit_code=0 if outcome == "succeeded" else 1,
            metrics={"score": 0.75},
            raw_log=f"artifacts/{run_name}",
            implementation="valid" if outcome == "succeeded" else "invalid",
            interface="valid",
            data="valid",
            metrics_quality="valid",
            hypothesis="supported" if outcome == "succeeded" else "not_evaluated",
            limitations=[],
            suggestions=[],
            artifacts=[f"artifacts/{metrics_name}"],
            issue_kind=None if outcome == "succeeded" else "implementation",
        )
        levels = (
            ["debug"]
            if purpose == "debug"
            else ["disqualified"]
            if outcome != "succeeded"
            else ["single_run"]
            if final_level == "single_run"
            else ["single_run", "confirmed"]
        )
        with mock.patch(
            "workflow_core.evidence._validate_transition_semantics"
        ):
            for level in levels:
                record_evidence_transition(
                    self.project,
                    run["id"],
                    level,
                    evidence_refs=[run["id"]],
                    reason="证据充分",
                    proposed_by="tester",
                    checked_by="reviewer",
                    applied_by="tester",
                )
        return task, run

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.project), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def _write_manifest(
        self,
        name: str,
        payload: dict[str, object],
    ) -> Path:
        path = self.root / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def _innovation_chain(
        self,
    ) -> tuple[
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        (self.project / "templates" / "base").mkdir(parents=True)
        files = {
            "templates/base/entry.py": "def main():\n    return 0\n",
            "templates/base/config.json": '{"enabled":true}\n',
            "templates/base/module.py": (
                "def apply(value):\n    return value + 1\n"
            ),
            "module.py": "def apply(value):\n    return value + 1\n",
            "test_module.py": (
                "from module import apply\n\n"
                "def test_apply():\n    assert apply(1) == 2\n"
            ),
        }
        for relative, content in files.items():
            (self.project / relative).write_text(content, encoding="utf-8")
        (self.project / ".gitignore").write_text(
            ".experiment-workflow/\n"
            ".experiment-workflow.init.lock\n"
            ".experiment-workflow.init.lock.bootstrap-*.tmp\n"
            "artifacts/\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "init", str(self.project)],
            check=True,
            capture_output=True,
        )
        self._git("config", "user.email", "paper-tests@example.invalid")
        self._git("config", "user.name", "Paper Tests")
        self._git("config", "core.autocrlf", "false")
        self._git("add", ".")
        self._git("commit", "-m", "innovation fixture")

        paper_path = self.root / "prior-art.pdf"
        paper_path.write_bytes(
            b"%PDF-1.4\nprior art prototype calibration\n%%EOF\n"
        )
        paper_source = register_source(
            self.project,
            self._write_manifest(
                "paper-source.json",
                {
                    "schema": "cv-experiment-workflow.source.v2",
                    "kind": "paper",
                    "identity": "Prior Art 2025",
                    "locator": str(paper_path.resolve()),
                    "revision": "v1",
                    "commit": None,
                    "digest": (
                        "sha256:"
                        + hashlib.sha256(paper_path.read_bytes()).hexdigest()
                    ),
                    "license": "fair-use",
                },
            ),
            paper_path,
        )
        self._git("add", ".")
        self._git("commit", "--amend", "--no-edit")
        commit = self._git("rev-parse", "HEAD")
        commit_bytes = subprocess.run(
            ["git", "-C", str(self.project), "cat-file", "commit", commit],
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(
            "",
            self._git("status", "--porcelain", "--untracked-files=all"),
        )
        code_source = register_source(
            self.project,
            self._write_manifest(
                "code-source.json",
                {
                    "schema": "cv-experiment-workflow.source.v2",
                    "kind": "code",
                    "identity": "本项目代码",
                    "locator": str(self.project.resolve()),
                    "revision": commit,
                    "commit": commit,
                    "digest": (
                        "sha256:"
                        + hashlib.sha256(commit_bytes).hexdigest()
                    ),
                    "license": "project-owned",
                },
            ),
            self.project,
        )
        idea = save_idea(
            self.project,
            self._write_manifest(
                "idea.json",
                {
                    "schema": "cv-experiment-workflow.catalog-idea.v1",
                    "status": "ready",
                    "problem": "少样本类别原型存在偏差",
                    "mechanism": "按来源置信度校准原型",
                    "falsifiable_hypothesis": "测试准确率应高于 0.70",
                    "source_refs": [paper_source["id"]],
                    "evidence_refs": [paper_source["id"]],
                    "source_links": [
                        {
                            "source_ref": paper_source["id"],
                            "locator": "page:1",
                            "supports_field": "mechanism",
                            "claim": "已有工作使用原型校准",
                        },
                    ],
                },
            ),
        )
        template_files = []
        for relative in ("entry.py", "config.json", "module.py"):
            content = (
                self.project / "templates" / "base" / relative
            ).read_bytes()
            template_files.append(
                {
                    "path": relative,
                    "role": "可执行模板文件",
                    "digest": (
                        "sha256:" + hashlib.sha256(content).hexdigest()
                    ),
                }
            )
        template = register_template(
            self.project,
            self._write_manifest(
                "template.json",
                {
                    "schema": (
                        "cv-experiment-workflow.template-manifest.v2"
                    ),
                    "name": "原型校准模板",
                    "ownership": "project",
                    "source_refs": [code_source["id"]],
                    "code": {
                        "source_ref": code_source["id"],
                        "template_path": "templates/base",
                        "listed_files": template_files,
                    },
                    "interfaces": {
                        "data": "batch",
                        "model": "module",
                        "training": "train",
                        "evaluation": "evaluate",
                        "config": "mapping",
                        "seed": "integer",
                        "metrics": "mapping",
                        "checkpoint": "path",
                    },
                    "attachment_points": [
                        {
                            "name": "feature-output",
                            "contract": "tensor -> tensor",
                            "target_path": "module.py",
                            "disabled_behavior": "identity",
                        },
                    ],
                    "baseline_recipe": {
                        "entry": "entry.py",
                        "config": "config.json",
                        "seed": 7,
                    },
                },
            ),
            self.project,
        )
        module = register_module(
            self.project,
            self._write_manifest(
                "module.json",
                {
                    "schema": "cv-experiment-workflow.module.v2",
                    "status": "ready",
                    "kind": "research",
                    "intent": "实现来源感知原型校准",
                    "idea_refs": [idea["id"]],
                    "source_refs": [code_source["id"]],
                    "attachment": {
                        "template_ref": template["id"],
                        "point": "feature-output",
                    },
                    "entry": "module.py",
                    "contract": {
                        "input": "feature tensor",
                        "output": "calibrated tensor",
                    },
                    "toggle": {
                        "config_key": "model.calibration.enabled",
                        "enabled_value": True,
                        "disabled_value": False,
                    },
                    "disabled_behavior": "identity",
                    "code_ref": {"commit": commit, "path": "module.py"},
                    "tests": ["test_module.py"],
                },
            ),
            self.project,
        )
        task = create_task(
            self.project,
            owner_request="验证来源感知原型校准",
            route="innovation",
            target_refs=[idea["id"], template["id"], module["id"]],
            route_inputs={
                "config": {"model.calibration.enabled": True},
                "seed": 7,
                "debug_required": True,
                "changes_code_behavior": True,
                "code_verified": True,
                "execution_authorized": True,
                "plan_unchanged": True,
                "primary_metric": "score",
                "baseline": 0.70,
            },
            budget={"max_runs": 2},
            stop_condition={"max_failures": 1},
        )
        transition_task(self.project, task["id"], "preparing")
        self.assertEqual(
            {"status": "pass"},
            readiness(
                self.project,
                task["id"],
                target_clear=True,
                template_runnable=True,
                mutation_allowed=True,
                code_verified=True,
            ),
        )
        frozen = {
            "code": {"repository": "local", "commit": commit},
            "config": {
                "metric_definition": {
                    "score": "测试集上的平均准确率",
                },
            },
            "seed": 7,
            "data": {
                "dataset_id": "demo",
                "version": "1",
                "split": "test",
            },
            "environment": {"python": "3.10"},
        }
        debug_run = create_run(
            self.project, task["id"], frozen, purpose="debug"
        )
        start_run(self.project, debug_run["id"], process_id=124)
        finish_run(
            self.project,
            debug_run["id"],
            outcome="succeeded",
            exit_code=0,
            metrics={"score": 0.71},
            raw_log=None,
            implementation="valid",
            interface="valid",
            data="valid",
            metrics_quality="valid",
            hypothesis="not_evaluated",
            limitations=[],
            suggestions=[],
            artifacts=[],
        )
        record_evidence_transition(
            self.project,
            debug_run["id"],
            "debug",
            evidence_refs=[debug_run["id"]],
            reason="调试闭环完成",
            proposed_by="tester",
            checked_by="reviewer",
            applied_by="tester",
        )
        from workflow_core.runs import close_run

        close_run(self.project, debug_run["id"])
        # 同上：只为旧包解析回归构造历史未绑定 evidence Run。
        with mock.patch(
            "workflow_core.runs._require_bound_for_new_evidence"
        ):
            run = create_run(
                self.project, task["id"], frozen, purpose="evidence"
            )
        (self.project / "artifacts").mkdir(exist_ok=True)
        (self.project / "artifacts" / "innovation.log").write_text(
            "score=0.75\n", encoding="utf-8"
        )
        (self.project / "artifacts" / "innovation-metrics.json").write_text(
            '{"score":0.75}\n', encoding="utf-8"
        )
        start_run(self.project, run["id"], process_id=125)
        run = finish_run(
            self.project,
            run["id"],
            outcome="succeeded",
            exit_code=0,
            metrics={"score": 0.75},
            raw_log="artifacts/innovation.log",
            implementation="valid",
            interface="valid",
            data="valid",
            metrics_quality="valid",
            hypothesis="supported",
            limitations=[],
            suggestions=[],
            artifacts=["artifacts/innovation-metrics.json"],
        )
        with mock.patch(
            "workflow_core.evidence._validate_transition_semantics"
        ):
            for level in ("single_run", "confirmed"):
                record_evidence_transition(
                    self.project,
                    run["id"],
                    level,
                    evidence_refs=[run["id"]],
                    reason="创新实验已确认",
                    proposed_by="tester",
                    checked_by="reviewer",
                    applied_by="tester",
                )
        return paper_source, code_source, idea, template, module, {
            "task": task,
            "run": run,
        }

    @staticmethod
    def _selection(
        task: dict[str, object],
        run: dict[str, object],
        *,
        asset_mode: str = "hybrid",
        supersedes_package_id: str | None = None,
    ) -> dict[str, object]:
        run_id = str(run["id"])
        task_id = str(task["id"])
        result = run["result"]
        raw_log = str(result["raw_log"])
        metrics_path = str(run["artifacts"][0])
        brief = research_brief_manifest()
        return {
            "schema": "cv-experiment-workflow.paper-package-selection.v1",
            "asset_mode": asset_mode,
            "paper_scope": {
                "title_hint": "来源感知原型校准",
                "research_area": brief["research_area"],
                "goal": brief["objective"],
                "included_claim_ids": ["CLM-0001"],
                "excluded_topics": brief["scope"]["excluded"],
            },
            "claims": [
                {
                    "claim_id": "CLM-0001",
                    "kind": "result",
                    "origin": "project",
                    "statement_zh": "原型校准的测试准确率为 0.75。",
                    "statement_en": None,
                    "maturity": "confirmed",
                    "run_refs": [run_id],
                    "metric_refs": [
                        {"run_id": run_id, "metric_name": "score"},
                    ],
                    "source_refs": [],
                    "idea_refs": [],
                    "module_refs": [],
                    "innovation_boundary": None,
                    "allowed_sections": ["experiments"],
                },
            ],
            "experiments": [
                {
                    "experiment_id": "EXP-0001",
                    "title": "主结果实验",
                    "objective": "确认原型校准准确率",
                    "task_id": task_id,
                    "run_refs": [run_id],
                    "claim_refs": ["CLM-0001"],
                },
            ],
            "source_uses": [],
            "assets": [
                {
                    "asset_id": "AST-0001",
                    "source_object_ref": run_id,
                    "path": raw_log,
                    "role": "raw_log",
                    "required_for_writing": True,
                    "copy_allowed": True,
                    "license": "project-owned",
                    "privacy_classification": "internal",
                    "availability": "available",
                    "omission_reason": "",
                },
                {
                    "asset_id": "AST-0002",
                    "source_object_ref": run_id,
                    "path": metrics_path,
                    "role": "result_data",
                    "required_for_writing": True,
                    "copy_allowed": True,
                    "license": "project-owned",
                    "privacy_classification": "internal",
                    "availability": "available",
                    "omission_reason": "",
                },
            ],
            "visuals": [],
            "supersedes_package_id": supersedes_package_id,
        }

    def _innovation_selection(
        self,
        paper: dict[str, object],
        idea: dict[str, object],
        module: dict[str, object],
        execution: dict[str, object],
        *,
        statement: str = "提出来源感知的原型校准机制。",
    ) -> dict[str, object]:
        task = execution["task"]
        run = execution["run"]
        selection = self._selection(task, run)
        selection["paper_scope"]["included_claim_ids"] = [
            "CLM-0001",
            "CLM-0002",
        ]
        selection["claims"].append(
            {
                "claim_id": "CLM-0002",
                "kind": "innovation",
                "origin": "project",
                "statement_zh": statement,
                "statement_en": None,
                "maturity": "supported",
                "run_refs": [],
                "metric_refs": [],
                "source_refs": [paper["id"]],
                "idea_refs": [idea["id"]],
                "module_refs": [module["id"]],
                "innovation_boundary": {
                    "existing_or_prior": "已有工作使用一般原型校准。",
                    "new_contribution": "本文按来源置信度调整校准幅度。",
                    "not_claimed": "不声称首次提出所有原型校准方法。",
                },
                "allowed_sections": ["introduction", "method"],
            }
        )
        selection["experiments"][0]["claim_refs"].append("CLM-0002")
        selection["source_uses"] = [
            {
                "source_ref": paper["id"],
                "role": "prior_art",
                "excerpt": "Prior art uses prototype calibration.",
                "supports_claim_ids": ["CLM-0002"],
            }
        ]
        selection["assets"] = [
            {
                **selection["assets"][0],
                "path": "artifacts/innovation.log",
            },
            {
                **selection["assets"][1],
                "path": "artifacts/innovation-metrics.json",
            },
            {
                "asset_id": "AST-0003",
                "source_object_ref": module["id"],
                "path": "module.py",
                "role": "implementation",
                "required_for_writing": True,
                "copy_allowed": True,
                "license": "project-owned",
                "privacy_classification": "internal",
                "availability": "available",
                "omission_reason": "",
            },
        ]
        selection["visuals"] = [
            {
                "visual_id": "VIS-0001",
                "kind": "figure",
                "title": "创新机制与结果",
                "purpose": "展示机制与实验证据的对应关系",
                "allowed_sections": ["method", "experiments"],
                "claim_refs": ["CLM-0001", "CLM-0002"],
                "experiment_refs": ["EXP-0001"],
                "asset_refs": ["AST-0002", "AST-0003"],
            }
        ]
        return selection

    def _seal(
        self,
        selection: dict[str, object],
    ) -> dict[str, object]:
        selection_path = self.root / "selection.json"
        selection_path.write_text(
            json.dumps(selection, ensure_ascii=False),
            encoding="utf-8",
        )
        return cli_json(
            "seal-paper-package",
            "--project",
            self.project,
            "--brief",
            self.brief["brief_id"],
            "--selection",
            selection_path,
        )

    def _seal_direct(
        self,
        selection: dict[str, object],
    ) -> dict[str, object]:
        selection_path = self.root / "selection-direct.json"
        selection_path.write_text(
            json.dumps(selection, ensure_ascii=False),
            encoding="utf-8",
        )
        return seal_paper_package(
            self.project,
            str(self.brief["brief_id"]),
            selection_path,
        )

    @staticmethod
    def _recompute_package_hashes(package: Path) -> None:
        package_path = package / "package.json"
        package_payload = read_json(package_path)
        package_payload["content_sha256"] = paper_package_module._content_sha256(
            {
                name: (package / name).read_bytes()
                for name in paper_package_module.SCIENTIFIC_FILES
            }
        )
        package_path.write_bytes(
            (
                json.dumps(
                    package_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
        checksum_names = sorted(PACKAGE_FILES - {"checksums.sha256"})
        (package / "checksums.sha256").write_bytes(
            "".join(
                f"{hashlib.sha256((package / name).read_bytes()).hexdigest()}"
                f"  {name}\n"
                for name in checksum_names
            ).encode("ascii")
        )

    @staticmethod
    def _write_canonical_json(path: Path, payload: object) -> None:
        path.write_bytes(
            (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )

    def test_seal_confirmed_result_creates_exact_immutable_package_and_status(
        self,
    ) -> None:
        task, run = self._confirmed_run()

        result = self._seal(self._selection(task, run))

        self.assertEqual(
            "cv-experiment-workflow.paper-package-seal-result.v1",
            result["schema"],
        )
        self.assertEqual("PKG-0001", result["package_id"])
        self.assertEqual("sealed", result["status"])
        self.assertEqual("paper_ready", result["readiness"])
        package_directory = (
            self.control / "paper-packages" / "packages" / "PKG-0001"
        )
        self.assertEqual(
            PACKAGE_FILES,
            {entry.name for entry in package_directory.iterdir()},
        )
        package = read_json(package_directory / "package.json")
        self.assertEqual(
            "cv-experiment-workflow.paper-package.v1",
            package["schema"],
        )
        self.assertEqual("hybrid", package["asset_mode"])
        self.assertEqual(1, package["package_revision"])
        self.assertEqual("paper_ready", package["readiness"])
        self.assertRegex(package["content_sha256"], r"^sha256:[0-9a-f]{64}$")
        checksum_lines = (
            package_directory / "checksums.sha256"
        ).read_text(encoding="ascii").splitlines()
        self.assertEqual(
            sorted(PACKAGE_FILES - {"checksums.sha256"}),
            [line.split("  ", 1)[1] for line in checksum_lines],
        )
        self.assertTrue(
            all(
                len(line.split("  ", 1)[0]) == 64
                for line in checksum_lines
            )
        )

        status = cli_json("workflow-status", "--project", self.project)
        self.assertEqual(1, status["research_packages"]["count"])
        self.assertEqual(
            {
                "package_id": "PKG-0001",
                "readiness": "paper_ready",
                "mode": "hybrid",
                "supersedes": None,
                "superseded_by": None,
                "source_revoked": False,
                "missing_requirements": [],
            },
            status["research_packages"]["items"][0],
        )

    def test_result_statement_must_match_selected_live_metric(self) -> None:
        task, run = self._confirmed_run()
        selection = self._selection(task, run)
        selection["claims"][0]["statement_zh"] = "测试准确率为 0.76。"

        with self.assertRaisesRegex(ValueError, "所选指标值"):
            self._seal_direct(selection)

        self.assertFalse(
            (self.control / "paper-packages" / "packages").exists()
        )

    def test_result_number_does_not_treat_bare_75_as_75_percent(self) -> None:
        statements = (
            (
                "statement_zh",
                "测试准确率为0.75，增益75。",
            ),
            (
                "statement_zh",
                "测试准确率为 0.75，增益75。",
            ),
            (
                "statement_zh",
                "测试准确率为 0.75，错误值0.76。",
            ),
            (
                "statement_zh",
                "测试准确率为 0.75，错误值7.5e1。",
            ),
            (
                "statement_zh",
                "测试准确率为 0.75，错误值.76。",
            ),
            (
                "statement_zh",
                "测试准确率x-0.75。",
            ),
            (
                "statement_zh",
                "测试准确率delta-7.5e-1。",
            ),
            (
                "statement_en",
                "Test accuracy is 0.75, not bare 75.",
            ),
        )
        for index, (field, statement) in enumerate(statements):
            with self.subTest(field=field, statement=statement):
                if index:
                    self.setUp()
                task, run = self._confirmed_run()
                selection = self._selection(task, run)
                selection["claims"][0][field] = statement

                with self.assertRaisesRegex(ValueError, "无法由指标|数字|所选指标"):
                    self._seal_direct(selection)

        with self.assertRaisesRegex(ValueError, "无法由指标|数字"):
            paper_package_module._validate_result_statement_numbers(
                "Metric is 1.0, malformed value is 1.e2.",
                selected_metric_values=[1.0],
                allowed_numbers=[1.0],
                claim_id="CLM-TEST",
            )

    def test_result_allows_decimal_and_explicit_percent_equivalent(self) -> None:
        task, run = self._confirmed_run()
        selection = self._selection(task, run)
        selection["claims"][0]["statement_zh"] = (
            "测试准确率为 0.75，等价写法是 75%。"
        )

        result = self._seal_direct(selection)

        self.assertEqual("paper_ready", result["readiness"])

    def test_result_allows_spaced_and_fullwidth_percent_tokens(self) -> None:
        statements = (
            ("statement_zh", "测试准确率75%。"),
            ("statement_zh", "测试准确率75 %。"),
            ("statement_zh", "测试准确率75％。"),
            ("statement_zh", "测试准确率75 ％。"),
            ("statement_zh", "测试准确率.75。"),
            "Test accuracy is 0.75, equivalently 75 %.",
        )
        for index, statement in enumerate(statements):
            with self.subTest(statement=statement):
                if index:
                    self.setUp()
                task, run = self._confirmed_run()
                selection = self._selection(task, run)
                if isinstance(statement, tuple):
                    field, statement = statement
                else:
                    field = "statement_en"
                selection["claims"][0][field] = statement

                result = self._seal_direct(selection)

                self.assertEqual("paper_ready", result["readiness"])

    def test_english_result_statement_cannot_forge_metric(self) -> None:
        task, run = self._confirmed_run()
        selection = self._selection(task, run)
        selection["claims"][0]["statement_en"] = "Test accuracy is 0.99."

        with self.assertRaisesRegex(ValueError, "英文|数字|指标"):
            self._seal_direct(selection)

    def test_done_task_comparison_must_match_selected_run(self) -> None:
        task, run = self._confirmed_run()
        current = load_task(self.project, str(task["id"]))
        transition_task(self.project, current["id"], "executing")
        transition_task(self.project, current["id"], "reviewing")
        transition_task(
            self.project,
            current["id"],
            "done",
            conclusion={
                "run_id": run["id"],
                "comparison": {
                    "primary_metric": "score",
                    "baseline": None,
                    "candidate": 0.76,
                    "delta": None,
                },
            },
        )

        with self.assertRaisesRegex(ValueError, "永久比较结果"):
            self._seal_direct(self._selection(task, run))

    def test_candidate_debug_failed_and_revoked_runs_cannot_bypass_result_gate(
        self,
    ) -> None:
        scenarios = (
            {"final_level": "single_run", "expected": "confirmed"},
            {"purpose": "debug", "expected": "confirmed"},
            {"outcome": "failed", "expected": "confirmed"},
        )
        for index, scenario in enumerate(scenarios):
            with self.subTest(scenario=scenario):
                if index:
                    self.setUp()
                task, run = self._confirmed_run(
                    final_level=scenario.get("final_level", "confirmed"),
                    purpose=scenario.get("purpose", "evidence"),
                    outcome=scenario.get("outcome", "succeeded"),
                )
                with self.assertRaisesRegex(
                    ValueError, str(scenario["expected"])
                ):
                    self._seal_direct(self._selection(task, run))

        self.setUp()
        task, run = self._confirmed_run()
        record_evidence_transition(
            self.project,
            str(run["id"]),
            "revoked",
            evidence_refs=[str(run["id"])],
            reason="封存前撤销",
            proposed_by="tester",
            checked_by="reviewer",
            applied_by="tester",
        )
        with self.assertRaisesRegex(ValueError, "confirmed"):
            self._seal_direct(self._selection(task, run))

    def test_readback_requires_real_historical_confirmed_event(self) -> None:
        task, run = self._confirmed_run(final_level="single_run")
        with mock.patch(
            "workflow_core.evidence.levels_from_events",
            return_value={str(run["id"]): "confirmed"},
        ):
            with self.assertRaisesRegex(ValueError, "历史 confirmed"):
                self._seal_direct(self._selection(task, run))

    def test_assets_must_bind_real_run_paths_and_drive_readiness(self) -> None:
        task, run = self._confirmed_run()
        bad_path = self._selection(task, run)
        bad_path["assets"][0]["path"] = "artifacts/not-owned.log"
        with self.assertRaisesRegex(ValueError, "不属于 Run"):
            self._seal_direct(bad_path)

        incomplete = self._selection(task, run)
        incomplete["assets"][0]["copy_allowed"] = False
        result = self._seal_direct(incomplete)
        self.assertEqual("incomplete", result["readiness"])
        self.assertIn(
            "required_asset_not_copyable:AST-0001",
            result["missing_requirements"],
        )

    def test_omitted_live_run_file_seals_as_incomplete(self) -> None:
        for index, omitted in enumerate(("raw_log", "artifact")):
            with self.subTest(omitted=omitted):
                if index:
                    self.setUp()
                task, run = self._confirmed_run()
                selection = self._selection(task, run)
                if omitted == "raw_log":
                    selection["assets"] = [selection["assets"][1]]
                    selection["assets"][0]["asset_id"] = "AST-0001"
                    omitted_path = "artifacts/run.log"
                else:
                    selection["assets"].pop()
                    omitted_path = "artifacts/metrics.json"

                result = self._seal_direct(selection)

                self.assertEqual("incomplete", result["readiness"])
                self.assertIn(
                    f"result_file_missing:{run['id']}:{omitted_path}",
                    result["missing_requirements"],
                )

    def test_unknown_license_and_false_availability_cannot_bypass(self) -> None:
        task, run = self._confirmed_run()
        unknown = self._selection(task, run)
        unknown["assets"][0]["license"] = "unknown"
        with self.assertRaisesRegex(ValueError, "unknown license"):
            self._seal_direct(unknown)

        missing = self._selection(task, run)
        missing["assets"][0]["availability"] = "missing"
        missing["assets"][0]["omission_reason"] = "用户声称缺失"
        with self.assertRaisesRegex(ValueError, "现场文件不一致"):
            self._seal_direct(missing)

    def test_source_asset_license_must_match_source_ledger(self) -> None:
        paper, _code, idea, _template, module, execution = (
            self._innovation_chain()
        )
        selection = self._innovation_selection(
            paper, idea, module, execution
        )
        selection["assets"].append(
            {
                "asset_id": "AST-0004",
                "source_object_ref": paper["id"],
                "path": paper["locator"],
                "role": "prior_art",
                "required_for_writing": True,
                "copy_allowed": True,
                "license": "project-owned",
                "privacy_classification": "public",
                "availability": "available",
                "omission_reason": "",
            }
        )

        with self.assertRaisesRegex(ValueError, "license.*Source|许可"):
            self._seal_direct(selection)

    def test_selection_enum_wrong_types_fail_as_value_errors(self) -> None:
        task, run = self._confirmed_run()
        base = self._selection(task, run)
        mutations = {
            "asset_mode": lambda row: row.__setitem__("asset_mode", []),
            "claim_kind": lambda row: row["claims"][0].__setitem__("kind", []),
            "claim_origin": lambda row: row["claims"][0].__setitem__(
                "origin", []
            ),
            "claim_maturity": lambda row: row["claims"][0].__setitem__(
                "maturity", []
            ),
            "asset_privacy": lambda row: row["assets"][0].__setitem__(
                "privacy_classification", []
            ),
            "asset_availability": lambda row: row["assets"][0].__setitem__(
                "availability", []
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(field=label):
                selection = json.loads(json.dumps(base))
                mutate(selection)
                with self.assertRaises(ValueError):
                    self._seal_direct(selection)

        paper, _code, idea, _template, module, execution = (
            self._innovation_chain()
        )
        innovation = self._innovation_selection(
            paper, idea, module, execution
        )
        for label, mutate in {
            "source_role": lambda row: row["source_uses"][0].__setitem__(
                "role", []
            ),
            "visual_kind": lambda row: row["visuals"][0].__setitem__(
                "kind", []
            ),
        }.items():
            with self.subTest(field=label):
                selection = json.loads(json.dumps(innovation))
                mutate(selection)
                with self.assertRaises(ValueError):
                    self._seal_direct(selection)

    def test_dual_modes_share_scientific_content_hash(self) -> None:
        task, run = self._confirmed_run()
        hybrid = self._seal_direct(
            self._selection(task, run, asset_mode="hybrid")
        )
        full = self._seal_direct(
            self._selection(task, run, asset_mode="full")
        )

        self.assertEqual("PKG-0001", hybrid["package_id"])
        self.assertEqual("PKG-0002", full["package_id"])
        self.assertEqual(hybrid["content_sha256"], full["content_sha256"])

    def test_concurrent_seals_publish_two_complete_non_overwritten_packages(
        self,
    ) -> None:
        task, run = self._confirmed_run()
        selection_path = self._write_manifest(
            "concurrent-selection.json",
            self._selection(task, run),
        )

        def seal() -> dict[str, object]:
            return seal_paper_package(
                self.project,
                str(self.brief["brief_id"]),
                selection_path,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: seal(), range(2)))

        self.assertEqual(
            ["PKG-0001", "PKG-0002"],
            sorted(str(item["package_id"]) for item in results),
        )
        packages = self.control / "paper-packages" / "packages"
        for package_id in ("PKG-0001", "PKG-0002"):
            self.assertEqual(
                PACKAGE_FILES,
                {path.name for path in (packages / package_id).iterdir()},
            )

    def test_two_cli_processes_share_the_os_project_lock(self) -> None:
        task, run = self._confirmed_run()
        selections = [
            self._write_manifest(
                f"process-selection-{index}.json",
                self._selection(task, run),
            )
            for index in range(2)
        ]
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-X",
                    "utf8",
                    str(CLI),
                    "seal-paper-package",
                    "--project",
                    str(self.project),
                    "--brief",
                    str(self.brief["brief_id"]),
                    "--selection",
                    str(selection),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            for selection in selections
        ]
        completed = [
            (process.returncode, stdout, stderr)
            for process in processes
            for stdout, stderr in [process.communicate(timeout=30)]
        ]
        self.assertTrue(
            all(code == 0 for code, _stdout, _stderr in completed),
            completed,
        )
        package_ids = sorted(
            json.loads(stdout)["package_id"]
            for _code, stdout, _stderr in completed
        )
        self.assertEqual(["PKG-0001", "PKG-0002"], package_ids)
        packages = self.control / "paper-packages" / "packages"
        for package_id in package_ids:
            self.assertEqual(
                PACKAGE_FILES,
                {path.name for path in (packages / package_id).iterdir()},
            )
        self.assertEqual(
            [],
            list(self.project.glob(".experiment-workflow.paper-packages-*")),
        )

    def test_supersedes_is_forward_only_and_revocation_is_derived(self) -> None:
        task, run = self._confirmed_run()
        first = self._seal_direct(self._selection(task, run))
        first_directory = (
            self.control / "paper-packages" / "packages" / "PKG-0001"
        )
        first_bytes = {
            path.name: path.read_bytes() for path in first_directory.iterdir()
        }
        second = self._seal_direct(
            self._selection(
                task,
                run,
                asset_mode="full",
                supersedes_package_id=first["package_id"],
            )
        )
        self.assertEqual("PKG-0002", second["package_id"])
        self.assertEqual(
            first_bytes,
            {
                path.name: path.read_bytes()
                for path in first_directory.iterdir()
            },
        )
        with self.assertRaisesRegex(ValueError, "已被直接 supersede"):
            self._seal_direct(
                self._selection(
                    task,
                    run,
                    supersedes_package_id=first["package_id"],
                )
            )

        record_evidence_transition(
            self.project,
            str(run["id"]),
            "revoked",
            evidence_refs=[str(run["id"])],
            reason="后续发现数据问题",
            proposed_by="tester",
            checked_by="reviewer",
            applied_by="tester",
        )
        items = cli_json(
            "workflow-status", "--project", self.project
        )["research_packages"]["items"]
        self.assertEqual("PKG-0002", items[0]["superseded_by"])
        self.assertEqual("PKG-0001", items[1]["supersedes"])
        self.assertTrue(items[0]["source_revoked"])
        self.assertTrue(items[1]["source_revoked"])

    def test_legal_run_close_does_not_invalidate_sealed_package(self) -> None:
        from workflow_core.runs import close_run

        task, run = self._confirmed_run()
        self._seal_direct(self._selection(task, run))
        close_run(self.project, str(run["id"]))

        status = cli_json("workflow-status", "--project", self.project)
        self.assertEqual(1, status["research_packages"]["count"])
        self.assertEqual(
            "paper_ready",
            status["research_packages"]["items"][0]["readiness"],
        )

    def test_tamper_is_rejected_by_status(self) -> None:
        task, run = self._confirmed_run()
        self._seal_direct(self._selection(task, run))
        claims = (
            self.control
            / "paper-packages"
            / "packages"
            / "PKG-0001"
            / "claims.jsonl"
        )
        claims.write_bytes(claims.read_bytes() + b"{}\n")

        result = run_cli(
            "workflow-status",
            "--project",
            self.project,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("checksums.sha256", result.stderr)

    def test_recomputed_checksums_do_not_hide_noncanonical_control_json(
        self,
    ) -> None:
        task, run = self._confirmed_run()
        self._seal_direct(self._selection(task, run))
        package = (
            self.control / "paper-packages" / "packages" / "PKG-0001"
        )
        package_json = package / "package.json"
        payload = read_json(package_json)
        package_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        checksum_names = sorted(PACKAGE_FILES - {"checksums.sha256"})
        (package / "checksums.sha256").write_bytes(
            "".join(
                f"{hashlib.sha256((package / name).read_bytes()).hexdigest()}"
                f"  {name}\n"
                for name in checksum_names
            ).encode("ascii")
        )

        result = run_cli(
            "workflow-status",
            "--project",
            self.project,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("不是规范 JSON", result.stderr)

    def test_recomputed_hashes_do_not_hide_unknown_scientific_fields(
        self,
    ) -> None:
        task, run = self._confirmed_run()
        self._seal_direct(self._selection(task, run))
        package = (
            self.control / "paper-packages" / "packages" / "PKG-0001"
        )
        claims_path = package / "claims.jsonl"
        claim = json.loads(claims_path.read_text(encoding="utf-8"))
        claim["forged_note"] = "不能进入严格封存格式"
        claims_path.write_bytes(
            (
                json.dumps(
                    claim,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
        self._recompute_package_hashes(package)

        result = run_cli(
            "workflow-status",
            "--project",
            self.project,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("Claim 字段无效", result.stderr)

    def test_recomputed_hashes_do_not_hide_forged_result_number(self) -> None:
        task, run = self._confirmed_run()
        self._seal_direct(self._selection(task, run))
        package = (
            self.control / "paper-packages" / "packages" / "PKG-0001"
        )
        claims_path = package / "claims.jsonl"
        claim = json.loads(claims_path.read_text(encoding="utf-8"))
        claim["statement_zh"] = "原型校准的测试准确率为 0.99。"
        claims_path.write_bytes(
            (
                json.dumps(
                    claim,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
        self._recompute_package_hashes(package)

        result = run_cli(
            "workflow-status",
            "--project",
            self.project,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("所选指标值", result.stderr)

    def test_recomputed_hashes_cannot_detach_scope_from_live_brief(self) -> None:
        task, run = self._confirmed_run()
        self._seal_direct(self._selection(task, run))
        package = (
            self.control / "paper-packages" / "packages" / "PKG-0001"
        )
        study_path = package / "study.json"
        study = read_json(study_path)
        study["paper_scope"]["research_area"] = "伪造研究领域"
        self._write_canonical_json(study_path, study)
        package_path = package / "package.json"
        package_payload = read_json(package_path)
        package_payload["paper_scope"]["research_area"] = "伪造研究领域"
        self._write_canonical_json(package_path, package_payload)
        self._recompute_package_hashes(package)

        result = run_cli(
            "workflow-status",
            "--project",
            self.project,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("Research Brief", result.stderr)

    def test_recomputed_hashes_cannot_null_asset_and_experiment_identity(
        self,
    ) -> None:
        task, run = self._confirmed_run()
        self._seal_direct(self._selection(task, run))
        package = (
            self.control / "paper-packages" / "packages" / "PKG-0001"
        )
        assets_path = package / "assets.jsonl"
        assets = [
            json.loads(line)
            for line in assets_path.read_text(encoding="utf-8").splitlines()
        ]
        assets[0]["size_bytes"] = None
        assets[0]["sha256"] = None
        assets_path.write_bytes(
            b"".join(
                (
                    json.dumps(
                        asset,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
                for asset in assets
            )
        )
        experiments_path = package / "experiments.json"
        experiments = read_json(experiments_path)
        experiment_file = experiments["items"][0]["runs"][0]["files"][0]
        experiment_file["size_bytes"] = None
        experiment_file["sha256"] = None
        self._write_canonical_json(experiments_path, experiments)
        self._recompute_package_hashes(package)

        result = run_cli(
            "workflow-status",
            "--project",
            self.project,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("Asset", result.stderr)

    def test_experiment_files_must_exactly_cover_live_run_files(self) -> None:
        for index, mutation in enumerate(("missing", "duplicate")):
            with self.subTest(mutation=mutation):
                if index:
                    self.setUp()
                task, run = self._confirmed_run()
                self._seal_direct(self._selection(task, run))
                package = (
                    self.control
                    / "paper-packages"
                    / "packages"
                    / "PKG-0001"
                )
                experiments_path = package / "experiments.json"
                experiments = read_json(experiments_path)
                files = experiments["items"][0]["runs"][0]["files"]
                if mutation == "missing":
                    files.pop()
                else:
                    files.append(dict(files[0]))
                self._write_canonical_json(experiments_path, experiments)
                self._recompute_package_hashes(package)

                result = run_cli(
                    "workflow-status",
                    "--project",
                    self.project,
                    check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertIn("Experiment 文件", result.stderr)

    def test_coordinated_asset_and_experiment_omission_cannot_stay_ready(
        self,
    ) -> None:
        task, run = self._confirmed_run()
        self._seal_direct(self._selection(task, run))
        package = (
            self.control / "paper-packages" / "packages" / "PKG-0001"
        )
        assets_path = package / "assets.jsonl"
        assets = assets_path.read_text(encoding="utf-8").splitlines()
        assets_path.write_bytes((assets[0] + "\n").encode("utf-8"))
        experiments_path = package / "experiments.json"
        experiments = read_json(experiments_path)
        experiments["items"][0]["runs"][0]["files"].pop()
        self._write_canonical_json(experiments_path, experiments)
        self._recompute_package_hashes(package)

        result = run_cli(
            "workflow-status",
            "--project",
            self.project,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("missing_requirements", result.stderr)

    def test_live_run_assets_drift_invalidates_sealed_package(self) -> None:
        for index, relative in enumerate(
            ("artifacts/run.log", "artifacts/metrics.json")
        ):
            with self.subTest(path=relative):
                if index:
                    self.setUp()
                task, run = self._confirmed_run()
                self._seal_direct(self._selection(task, run))
                (self.project / relative).write_text(
                    "changed after seal\n",
                    encoding="utf-8",
                )

                result = run_cli(
                    "workflow-status",
                    "--project",
                    self.project,
                    check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertIn("Asset", result.stderr)

    def test_live_module_and_source_drift_invalidate_sealed_package(
        self,
    ) -> None:
        for index, target in enumerate(("module", "source")):
            with self.subTest(target=target):
                if index:
                    self.setUp()
                paper, _code, idea, _template, module, execution = (
                    self._innovation_chain()
                )
                selection = self._innovation_selection(
                    paper, idea, module, execution
                )
                self._seal_direct(selection)
                path = (
                    self.project / "module.py"
                    if target == "module"
                    else Path(str(paper["locator"]))
                )
                path.write_text(
                    "changed after seal\n",
                    encoding="utf-8",
                )

                result = run_cli(
                    "workflow-status",
                    "--project",
                    self.project,
                    check=False,
                )
                self.assertEqual(2, result.returncode)

    def test_recomputed_hashes_with_wrong_claim_type_fail_closed(self) -> None:
        task, run = self._confirmed_run()
        self._seal_direct(self._selection(task, run))
        package = (
            self.control / "paper-packages" / "packages" / "PKG-0001"
        )
        claims_path = package / "claims.jsonl"
        claim = json.loads(claims_path.read_text(encoding="utf-8"))
        claim["kind"] = []
        claims_path.write_bytes(
            (
                json.dumps(
                    claim,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
        self._recompute_package_hashes(package)

        result = run_cli(
            "workflow-status",
            "--project",
            self.project,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("Claim 值无效", result.stderr)
        self.assertNotIn("TypeError", result.stderr)

    def test_first_publish_failure_leaves_no_half_package(self) -> None:
        task, run = self._confirmed_run()
        selection = self._selection(task, run)

        with mock.patch.object(
            paper_package_module,
            "_publish_directory_no_replace",
            side_effect=OSError("模拟发布失败"),
        ):
            with self.assertRaisesRegex(OSError, "模拟发布失败"):
                self._seal_direct(selection)

        self.assertFalse(
            (self.control / "paper-packages" / "packages").exists()
        )
        self.assertEqual(
            [],
            list(self.project.glob(".experiment-workflow.paper-packages-*")),
        )
        self.assertEqual("PKG-0001", self._seal_direct(selection)["package_id"])
        first = (
            self.control / "paper-packages" / "packages" / "PKG-0001"
        )
        first_bytes = {
            path.name: path.read_bytes() for path in first.iterdir()
        }
        with mock.patch.object(
            paper_package_module,
            "_publish_directory_no_replace",
            side_effect=OSError("模拟后续发布失败"),
        ):
            with self.assertRaisesRegex(OSError, "模拟后续发布失败"):
                self._seal_direct(selection)
        self.assertEqual(
            ["PKG-0001"],
            [
                path.name
                for path in (
                    self.control / "paper-packages" / "packages"
                ).iterdir()
            ],
        )
        self.assertEqual(
            first_bytes,
            {path.name: path.read_bytes() for path in first.iterdir()},
        )
        self.assertEqual(
            [],
            list(self.project.glob(".experiment-workflow.paper-packages-*")),
        )

    def test_post_publish_reread_uses_fresh_live_hash_session(self) -> None:
        task, run = self._confirmed_run()
        selection = self._selection(task, run)
        raw_log = self.project / str(run["result"]["raw_log"])
        real_publish = paper_package_module._publish_directory_no_replace

        def publish_then_rewrite_same_identity(
            source: Path,
            destination: Path,
        ) -> None:
            real_publish(source, destination)
            before = raw_log.stat()
            original = raw_log.read_bytes()
            replacement = original.replace(b"0.75", b"0.74")
            self.assertEqual(len(original), len(replacement))
            raw_log.write_bytes(replacement)
            os.utime(
                raw_log,
                ns=(before.st_atime_ns, before.st_mtime_ns),
            )

        with mock.patch.object(
            paper_package_module,
            "_publish_directory_no_replace",
            side_effect=publish_then_rewrite_same_identity,
        ):
            with self.assertRaisesRegex(ValueError, "现场文件|哈希|不一致"):
                self._seal_direct(selection)

    def test_numeric_looking_claim_fragments_fail_closed(self) -> None:
        statements = (
            "准确率为 0.75，同时声称提升⁹⁹％。",
            "准确率为 0.75，同时声称提升⑨⑨%。",
            "准确率为 ０．７５，同时声称提升９９％。",
            "准确率为 0.75，另一个值写成 0.75e+。",
        )
        for index, statement in enumerate(statements):
            with self.subTest(statement=statement):
                if index:
                    self.setUp()
                task, run = self._confirmed_run()
                selection = self._selection(task, run)
                selection["claims"][0]["statement_zh"] = statement
                with self.assertRaisesRegex(
                    ValueError,
                    "数字|指标|科学计数|写法",
                ):
                    self._seal_direct(selection)

    def test_external_and_restricted_assets_never_hash_local_content(
        self,
    ) -> None:
        task, run = self._confirmed_run()
        selection = self._selection(task, run)
        selection["assets"][0].update(
            {
                "availability": "external",
                "omission_reason": "保留在外部实验归档中",
            }
        )
        selection["assets"][1].update(
            {
                "availability": "restricted",
                "omission_reason": "受访问控制限制",
            }
        )

        with mock.patch.object(
            paper_package_module._LiveFileHashSession,
            "hash",
            side_effect=AssertionError("external/restricted 不得读取正文"),
        ):
            result = self._seal_direct(selection)
            self.assertEqual("incomplete", result["readiness"])
            package = (
                self.control
                / "paper-packages"
                / "packages"
                / str(result["package_id"])
            )
            rows = [
                json.loads(line)
                for line in (package / "assets.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                ["external", "restricted"],
                [row["availability"] for row in rows],
            )
            self.assertTrue(
                all(
                    row["size_bytes"] is None and row["sha256"] is None
                    for row in rows
                )
            )
            self.assertEqual(
                1,
                status(self.project)["research_packages"]["count"],
            )

    def test_brief_only_project_reports_empty_research_packages(self) -> None:
        status = cli_json("workflow-status", "--project", self.project)
        self.assertEqual(
            {"count": 0, "items": []},
            status["research_packages"],
        )

    def test_real_innovation_chain_seals_structural_claim_and_prior_art(
        self,
    ) -> None:
        paper, _code, idea, _template, module, execution = (
            self._innovation_chain()
        )
        selection = self._innovation_selection(
            paper, idea, module, execution
        )

        result = self._seal_direct(selection)

        self.assertEqual("paper_ready", result["readiness"])
        package = (
            self.control / "paper-packages" / "packages" / "PKG-0001"
        )
        sources = (package / "sources.jsonl").read_text(encoding="utf-8")
        self.assertIn(str(paper["id"]), sources)
        self.assertIn("excerpt_sha256", sources)
        self.assertIn("source_sha256", sources)

    def test_all_eight_claim_kinds_keep_their_distinct_provenance_rules(
        self,
    ) -> None:
        paper, _code, idea, _template, module, execution = (
            self._innovation_chain()
        )
        selection = self._innovation_selection(
            paper, idea, module, execution
        )
        additions = (
            ("background", "external", [paper["id"]], "introduction"),
            ("research_gap", "external", [paper["id"]], "introduction"),
            ("objective", "project", [], "introduction"),
            ("method", "project", [], "method"),
            ("protocol", "project", [], "experiments"),
            ("limitation", "project", [], "conclusion"),
        )
        for number, (kind, origin, source_refs, section) in enumerate(
            additions,
            start=3,
        ):
            selection["claims"].append(
                {
                    "claim_id": f"CLM-{number:04d}",
                    "kind": kind,
                    "origin": origin,
                    "statement_zh": f"{kind} 的可核验陈述。",
                    "statement_en": None,
                    "maturity": "supported",
                    "run_refs": [],
                    "metric_refs": [],
                    "source_refs": source_refs,
                    "idea_refs": [],
                    "module_refs": [],
                    "innovation_boundary": None,
                    "allowed_sections": [section],
                }
            )
        all_claim_ids = [
            item["claim_id"] for item in selection["claims"]
        ]
        selection["paper_scope"]["included_claim_ids"] = all_claim_ids
        selection["experiments"][0]["claim_refs"] = all_claim_ids
        selection["source_uses"].extend(
            [
                {
                    "source_ref": paper["id"],
                    "role": "background",
                    "excerpt": "Prior art establishes the background.",
                    "supports_claim_ids": ["CLM-0003"],
                },
                {
                    "source_ref": paper["id"],
                    "role": "comparison_context",
                    "excerpt": "Prior art leaves a research gap.",
                    "supports_claim_ids": ["CLM-0004"],
                },
            ]
        )

        result = self._seal_direct(selection)

        self.assertEqual("paper_ready", result["readiness"])
        claims = (
            self.control
            / "paper-packages"
            / "packages"
            / "PKG-0001"
            / "claims.jsonl"
        ).read_text(encoding="utf-8")
        for kind, _origin, _refs, _section in additions:
            self.assertIn(f'"kind":"{kind}"', claims)

    def test_innovation_effect_number_and_external_run_misuse_are_rejected(
        self,
    ) -> None:
        paper, _code, idea, _template, module, execution = (
            self._innovation_chain()
        )
        run = execution["run"]
        selection = self._innovation_selection(
            paper,
            idea,
            module,
            execution,
            statement="提出新机制并提升 5%。",
        )
        with self.assertRaisesRegex(ValueError, "effect|数字|结果"):
            self._seal_direct(selection)

        innovation = selection["claims"][1]
        innovation["statement_zh"] = "提出新机制，准确率提升75％。"
        with self.assertRaisesRegex(ValueError, "effect|数字|结果"):
            self._seal_direct(selection)

        innovation["kind"] = "method"
        innovation["origin"] = "external"
        innovation["statement_zh"] = "外部方法在本项目运行中有效。"
        innovation["run_refs"] = [run["id"]]
        innovation["metric_refs"] = [
            {"run_id": run["id"], "metric_name": "score"}
        ]
        innovation["idea_refs"] = []
        innovation["module_refs"] = []
        innovation["innovation_boundary"] = None
        with self.assertRaisesRegex(ValueError, "不得借用项目"):
            self._seal_direct(selection)

    def test_english_innovation_statement_cannot_hide_effect_number(
        self,
    ) -> None:
        paper, _code, idea, _template, module, execution = (
            self._innovation_chain()
        )
        selection = self._innovation_selection(
            paper, idea, module, execution
        )
        selection["claims"][1]["statement_en"] = (
            "The mechanism improves accuracy by 5%."
        )

        with self.assertRaisesRegex(ValueError, "英文|数字|结果"):
            self._seal_direct(selection)

    def test_external_paper_is_rehashed_at_seal_time(self) -> None:
        paper, _code, idea, _template, module, execution = (
            self._innovation_chain()
        )
        selection = self._innovation_selection(
            paper, idea, module, execution
        )
        Path(str(paper["locator"])).write_bytes(
            b"%PDF-1.4\nchanged after registration\n%%EOF\n"
        )

        with self.assertRaisesRegex(ValueError, "paper Source 文件哈希已变化"):
            self._seal_direct(selection)

    def test_publish_lstat_interrupt_cleans_first_and_later_staging(self) -> None:
        for index, existing_package in enumerate((False, True)):
            with self.subTest(existing_package=existing_package):
                if index:
                    self.setUp()
                task, run = self._confirmed_run()
                selection = self._selection(task, run)
                if existing_package:
                    self._seal_direct(selection)
                original_lstat = Path.lstat

                def interrupt_staging_lstat(path: Path):
                    if (
                        path.parent == self.project
                        and path.name.startswith(
                            paper_package_module.INITIAL_STAGING_PREFIX
                        )
                        and path.name.endswith(
                            paper_package_module.INITIAL_STAGING_SUFFIX
                        )
                    ):
                        raise KeyboardInterrupt("模拟 staging lstat 中断")
                    return original_lstat(path)

                with mock.patch.object(
                    Path,
                    "lstat",
                    new=interrupt_staging_lstat,
                ):
                    with self.assertRaisesRegex(
                        KeyboardInterrupt,
                        "staging lstat",
                    ):
                        self._seal_direct(selection)

                self.assertEqual(
                    [],
                    list(
                        self.project.glob(
                            ".experiment-workflow.paper-packages-*"
                        )
                    ),
                )
                expected = ["PKG-0001"] if existing_package else []
                packages = (
                    self.control / "paper-packages" / "packages"
                )
                actual = (
                    sorted(path.name for path in packages.iterdir())
                    if packages.exists()
                    else []
                )
                self.assertEqual(expected, actual)

    def test_unknown_package_producer_is_not_parse_compatible(self) -> None:
        task, run = self._confirmed_run()
        self._seal_direct(self._selection(task, run))
        package = (
            self.control / "paper-packages" / "packages" / "PKG-0001"
        )
        package_path = package / "package.json"
        payload = read_json(package_path)
        payload["producer"] = {
            "skill_id": "evil",
            "release_version": "999",
            "system_version": "SYS-EVIL",
        }
        self._write_canonical_json(package_path, payload)
        self._recompute_package_hashes(package)

        result = run_cli(
            "workflow-status",
            "--project",
            self.project,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("producer", result.stderr)

    def test_recomputed_hashes_reject_malformed_selection_shaped_text(
        self,
    ) -> None:
        mutations = ("scope", "experiment", "visual")
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                if index:
                    self.setUp()
                task, run = self._confirmed_run()
                selection = self._selection(task, run)
                selection["visuals"] = [
                    {
                        "visual_id": "VIS-0001",
                        "kind": "figure",
                        "title": "结果图",
                        "purpose": "展示确认结果",
                        "allowed_sections": ["experiments"],
                        "claim_refs": ["CLM-0001"],
                        "experiment_refs": ["EXP-0001"],
                        "asset_refs": ["AST-0002"],
                    }
                ]
                self._seal_direct(selection)
                package = (
                    self.control
                    / "paper-packages"
                    / "packages"
                    / "PKG-0001"
                )
                if mutation == "scope":
                    study_path = package / "study.json"
                    study = read_json(study_path)
                    study["paper_scope"]["title_hint"] = []
                    self._write_canonical_json(study_path, study)
                    package_path = package / "package.json"
                    package_payload = read_json(package_path)
                    package_payload["paper_scope"]["title_hint"] = []
                    self._write_canonical_json(package_path, package_payload)
                elif mutation == "experiment":
                    experiments_path = package / "experiments.json"
                    experiments = read_json(experiments_path)
                    experiments["items"][0]["title"] = []
                    experiments["items"][0]["objective"] = {"bad": True}
                    self._write_canonical_json(
                        experiments_path,
                        experiments,
                    )
                else:
                    visuals_path = package / "visuals.json"
                    visuals = read_json(visuals_path)
                    visuals["items"][0]["title"] = []
                    visuals["items"][0]["purpose"] = {"bad": True}
                    self._write_canonical_json(visuals_path, visuals)
                self._recompute_package_hashes(package)

                result = run_cli(
                    "workflow-status",
                    "--project",
                    self.project,
                    check=False,
                )

                self.assertEqual(2, result.returncode)
                self.assertNotIn("TypeError", result.stderr)

    def test_status_hashes_each_live_asset_only_once(self) -> None:
        task, run = self._confirmed_run()
        self._seal_direct(self._selection(task, run))

        with mock.patch.object(
            paper_package_module,
            "_hash_live_regular_file",
            wraps=paper_package_module._hash_live_regular_file,
        ) as hash_file:
            payload = status(self.project)

        self.assertTrue(payload["valid"])
        self.assertEqual(2, hash_file.call_count)

    def test_live_asset_total_hash_budget_is_checked_before_excess_read(
        self,
    ) -> None:
        task, run = self._confirmed_run()
        selection = self._selection(task, run)

        with mock.patch.object(
            paper_package_module,
            "MAX_LIVE_ASSET_TOTAL_SIZE",
            20,
            create=True,
        ):
            with self.assertRaisesRegex(ValueError, "总哈希预算"):
                self._seal_direct(selection)

        self.assertFalse(
            (self.control / "paper-packages" / "packages").exists()
        )

    def test_hash_cache_rejects_same_path_with_new_file_identity(self) -> None:
        target = self.root / "replace-me.bin"
        target.write_bytes(b"AAAA")
        original = target.stat()
        session = paper_package_module._LiveFileHashSession()
        first = session.hash(target, "测试文件", 1024)
        target.unlink()
        target.write_bytes(b"BBBB")
        os.utime(
            target,
            ns=(original.st_atime_ns, original.st_mtime_ns),
        )

        second = session.hash(target, "测试文件", 1024)

        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_total_budget_failure_before_later_package_publish(self) -> None:
        first_task, first_run = self._confirmed_run(artifact_tag="first")
        self._seal_direct(self._selection(first_task, first_run))
        second_task, second_run = self._confirmed_run(artifact_tag="second")
        first_total = sum(
            (self.project / path).stat().st_size
            for path in (
                first_run["result"]["raw_log"],
                *first_run["artifacts"],
            )
        )
        second_total = sum(
            (self.project / path).stat().st_size
            for path in (
                second_run["result"]["raw_log"],
                *second_run["artifacts"],
            )
        )

        with mock.patch.object(
            paper_package_module,
            "MAX_LIVE_ASSET_TOTAL_SIZE",
            max(first_total, second_total),
        ):
            with self.assertRaisesRegex(ValueError, "总哈希预算"):
                self._seal_direct(
                    self._selection(second_task, second_run)
                )

        packages = self.control / "paper-packages" / "packages"
        self.assertEqual(
            ["PKG-0001"],
            sorted(path.name for path in packages.iterdir()),
        )
        self.assertEqual(
            [],
            list(self.project.glob(".experiment-workflow.paper-packages-*")),
        )

    def test_result_number_scanner_rejects_adjacent_and_unsupported_tokens(
        self,
    ) -> None:
        statements = (
            "测试准确率为 0.75，delta-0.99。",
            "测试准确率为 0.75，delta−0.99。",
            "测试准确率为 0.75，delta - 0.99。",
            "测试准确率为 0.75，伪造值 1_000。",
        )
        for index, statement in enumerate(statements):
            with self.subTest(statement=statement):
                if index:
                    self.setUp()
                task, run = self._confirmed_run()
                selection = self._selection(task, run)
                selection["claims"][0]["statement_zh"] = statement

                with self.assertRaisesRegex(
                    ValueError,
                    "无法由指标|数字|不支持",
                ):
                    self._seal_direct(selection)

    def test_innovation_number_scanner_rejects_adjacent_effect_tokens(
        self,
    ) -> None:
        for index, statement in enumerate(
            ("提出新机制 delta-5%。", "提出新机制版本 1_000。")
        ):
            with self.subTest(statement=statement):
                if index:
                    self.setUp()
                paper, _code, idea, _template, module, execution = (
                    self._innovation_chain()
                )
                selection = self._innovation_selection(
                    paper,
                    idea,
                    module,
                    execution,
                    statement=statement,
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "数字|结果|不支持",
                ):
                    self._seal_direct(selection)

    def test_bool_cannot_impersonate_int_in_sealed_package(self) -> None:
        mutations = ("package_revision", "exit_code", "route_input")
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                if index:
                    self.setUp()
                task, run = self._confirmed_run()
                self._seal_direct(self._selection(task, run))
                package = (
                    self.control
                    / "paper-packages"
                    / "packages"
                    / "PKG-0001"
                )
                if mutation == "package_revision":
                    package_path = package / "package.json"
                    payload = read_json(package_path)
                    payload["package_revision"] = True
                    self._write_canonical_json(package_path, payload)
                else:
                    experiments_path = package / "experiments.json"
                    experiments = read_json(experiments_path)
                    if mutation == "exit_code":
                        experiments["items"][0]["runs"][0]["result"][
                            "exit_code"
                        ] = False
                    else:
                        experiments["items"][0]["task"]["route_inputs"][
                            "debug_required"
                        ] = 0
                    self._write_canonical_json(
                        experiments_path,
                        experiments,
                    )
                self._recompute_package_hashes(package)

                result = run_cli(
                    "workflow-status",
                    "--project",
                    self.project,
                    check=False,
                )

                self.assertEqual(2, result.returncode)

    def test_innovation_source_can_supply_multiple_roles_in_any_order(
        self,
    ) -> None:
        for index, reverse in enumerate((False, True)):
            with self.subTest(reverse=reverse):
                if index:
                    self.setUp()
                paper, _code, idea, _template, module, execution = (
                    self._innovation_chain()
                )
                selection = self._innovation_selection(
                    paper,
                    idea,
                    module,
                    execution,
                )
                selection["source_uses"].append(
                    {
                        "source_ref": paper["id"],
                        "role": "terminology",
                        "excerpt": "Prototype calibration is the standard term.",
                        "supports_claim_ids": ["CLM-0002"],
                    }
                )
                if reverse:
                    selection["source_uses"].reverse()

                result = self._seal_direct(selection)

                self.assertEqual("paper_ready", result["readiness"])

    def test_read_rejects_more_than_2048_experiments_or_visuals(self) -> None:
        for index, target in enumerate(("experiments", "visuals")):
            with self.subTest(target=target):
                if index:
                    self.setUp()
                task, run = self._confirmed_run()
                selection = self._selection(task, run)
                selection["visuals"] = [
                    {
                        "visual_id": "VIS-0001",
                        "kind": "figure",
                        "title": "结果图",
                        "purpose": "展示确认结果",
                        "allowed_sections": ["experiments"],
                        "claim_refs": ["CLM-0001"],
                        "experiment_refs": ["EXP-0001"],
                        "asset_refs": ["AST-0002"],
                    }
                ]
                self._seal_direct(selection)
                package = (
                    self.control
                    / "paper-packages"
                    / "packages"
                    / "PKG-0001"
                )
                path = package / f"{target}.json"
                payload = read_json(path)
                template = payload["items"][0]
                prefix = "EXP" if target == "experiments" else "VIS"
                id_field = (
                    "experiment_id"
                    if target == "experiments"
                    else "visual_id"
                )
                payload["items"] = []
                for number in range(1, 2050):
                    row = deepcopy(template)
                    row[id_field] = f"{prefix}-{number:04d}"
                    payload["items"].append(row)
                self._write_canonical_json(path, payload)
                self._recompute_package_hashes(package)

                result = run_cli(
                    "workflow-status",
                    "--project",
                    self.project,
                    check=False,
                )

                self.assertEqual(2, result.returncode)
                self.assertIn("2048", result.stderr)

    def test_package_id_exhaustion_and_draft_claim_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "PKG.*耗尽"):
            paper_package_module._next_package_id(
                {f"PKG-{number:04d}": {} for number in range(1, 10000)}
            )

        task, run = self._confirmed_run()
        selection = self._selection(task, run)
        selection["claims"][0]["maturity"] = "draft"
        with self.assertRaisesRegex(ValueError, "maturity|draft"):
            paper_package_module._normalize_package_selection(selection)


if __name__ == "__main__":
    unittest.main()
