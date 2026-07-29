#!/usr/bin/env python3
"""Run the non-submittable research-package to PaperFlow smoke path.

This program deliberately keeps three boundaries visible:

* the research producer must reject a debug Run;
* the research producer creates a temporary 1.5.0 bound-evidence package that
  must pass PaperFlow's current receiver, source matching, and writing gate;
* PaperFlow's separate test fixture package alone drives six-section writing.

It never approves or publishes a writing run.  The formal PaperFlow database is
opened with SQLite ``mode=ro&immutable=1`` plus ``PRAGMA query_only=ON`` and
copied into the caller-provided artifact directory before production retrieval
code is allowed to open it.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import html
import importlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


RESEARCH_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_SCRIPTS = (
    RESEARCH_ROOT / "skills" / "cv-experiment-workflow" / "scripts"
)
PAPER_SECTIONS = (
    "abstract",
    "introduction",
    "related_work",
    "method",
    "experiments",
    "conclusion",
)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_json_once(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(_json_bytes(payload))


def _run_git_text(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"临时 Git 操作失败：git {' '.join(arguments)}；"
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def _commit_test_fixture(repository: Path, message: str) -> str:
    return _run_git_text(
        repository,
        "-c",
        "user.name=CV-Workflow-Test",
        "-c",
        "user.email=cv-workflow-test@example.invalid",
        "commit",
        "-m",
        message,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_roots(
    paperflow_root: Path,
    library_root: Path,
    artifacts_root: Path,
    cuda_python: Path,
) -> tuple[Path, Path, Path, Path]:
    paperflow_root = Path(paperflow_root).expanduser().resolve(strict=True)
    library_root = Path(library_root).expanduser().resolve(strict=True)
    artifacts_root = Path(artifacts_root).expanduser().resolve(strict=False)
    cuda_python_input = Path(cuda_python).expanduser()
    cuda_python_metadata = cuda_python_input.lstat()
    if (
        cuda_python_input.is_symlink()
        or bool(
            getattr(cuda_python_metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    ):
        raise ValueError("CUDA Python 解释器不能是链接或重解析点")
    cuda_python = cuda_python_input.resolve(strict=True)
    if not stat.S_ISREG(cuda_python.stat().st_mode):
        raise ValueError("CUDA Python 解释器必须是普通文件")
    if not (paperflow_root / "paperflow_v2" / "__init__.py").is_file():
        raise ValueError("PaperFlow 候选根目录无效")
    database = library_root / "database" / "knowledge.db"
    if not database.is_file():
        raise ValueError("正式知识库缺少 database/knowledge.db")
    protected = (
        (RESEARCH_ROOT.resolve(), "科研候选源码"),
        (paperflow_root, "PaperFlow 候选源码"),
        (library_root, "正式知识库"),
    )
    for root, label in protected:
        if artifacts_root == root or _is_inside(artifacts_root, root):
            raise ValueError(f"产物目录不能位于{label}内")
    if artifacts_root.exists():
        raise FileExistsError("产物目录已存在，拒绝覆盖")
    return paperflow_root, library_root, artifacts_root, cuda_python


@contextlib.contextmanager
def _prepend_sys_path(*paths: Path):
    values = [str(Path(path).resolve()) for path in paths]
    for value in reversed(values):
        sys.path.insert(0, value)
    try:
        yield
    finally:
        for value in values:
            with contextlib.suppress(ValueError):
                sys.path.remove(value)


def _research_brief() -> dict[str, Any]:
    return {
        "title": "端到端门禁测试",
        "research_area": "计算机视觉",
        "background": "验证科研交付包与论文系统之间的安全边界。",
        "problem": "调试结果不能被误当作论文成绩。",
        "objective": "证明调试结果被拒绝，测试包只能生成候选预览。",
        "research_questions": ["调试结果能否绕过论文交付门禁？"],
        "hypotheses": [
            {
                "hypothesis_id": "H-01",
                "statement": "调试结果应被论文交付门禁拒绝。",
                "falsification_criteria": "任何 debug Run 能成功封存即失败。",
            }
        ],
        "scope": {
            "included": ["本地、低成本、测试专用闭环"],
            "excluded": ["正式实验结论", "投稿终稿"],
        },
        "terminology": [
            {
                "term_en": "debug run",
                "term_zh": "调试运行",
                "definition_zh": "只检查程序链路、不支持论文成绩的运行。",
            }
        ],
        "planned_contributions": [
            {
                "contribution_id": "C-01",
                "statement_zh": "验证交付边界。",
                "provenance": "original",
                "source_refs": [],
            }
        ],
        "user_confirmation": {
            "confirmed": True,
            "confirmed_at": "2026-07-27T00:00:00+08:00",
            "confirmed_by": "full-flow-smoke",
        },
    }


def _debug_selection(
    task: dict[str, Any],
    run: dict[str, Any],
) -> dict[str, Any]:
    run_id = str(run["id"])
    task_id = str(task["id"])
    return {
        "schema": "cv-experiment-workflow.paper-package-selection.v1",
        "asset_mode": "hybrid",
        "paper_scope": {
            "title_hint": "端到端门禁测试",
            "research_area": "计算机视觉",
            "goal": "证明调试结果被拒绝，测试包只能生成候选预览。",
            "included_claim_ids": ["CLM-0001"],
            "excluded_topics": ["正式实验结论", "投稿终稿"],
        },
        "claims": [
            {
                "claim_id": "CLM-0001",
                "kind": "result",
                "origin": "project",
                "statement_zh": "测试专用分数为 0.5。",
                "statement_en": None,
                "maturity": "confirmed",
                "run_refs": [run_id],
                "metric_refs": [
                    {"run_id": run_id, "metric_name": "score"}
                ],
                "source_refs": [],
                "idea_refs": [],
                "module_refs": [],
                "innovation_boundary": None,
                "allowed_sections": ["experiments"],
            }
        ],
        "experiments": [
            {
                "experiment_id": "EXP-0001",
                "title": "调试运行",
                "objective": "验证交付门禁",
                "task_id": task_id,
                "run_refs": [run_id],
                "claim_refs": ["CLM-0001"],
            }
        ],
        "source_uses": [],
        "assets": [
            {
                "asset_id": "AST-0001",
                "source_object_ref": run_id,
                "path": "artifacts/debug.log",
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
                "path": "artifacts/metrics.json",
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
        "supersedes_package_id": None,
    }


def _bound_research_brief() -> dict[str, Any]:
    brief = _research_brief()
    brief.update(
        {
            "title": "生产交付接口门禁测试",
            "background": "只验证科研生产交付包能否被论文系统安全接收。",
            "problem": "完整测试缺少科研端生产包到论文端当前门禁的正向证明。",
            "objective": (
                "用无科研意义的临时结果验证生产封存、导出、接收、"
                "来源匹配和写作门禁。"
            ),
            "research_questions": [
                "科研端 1.5.0 生产包能否通过 PaperFlow 当前接收与写作门禁？"
            ],
            "hypotheses": [
                {
                    "hypothesis_id": "H-01",
                    "statement": "生产交付包应在临时工作区通过当前门禁。",
                    "falsification_criteria": "任一生产导出、接收或门禁步骤失败。",
                }
            ],
            "scope": {
                "included": ["本地临时接口验证"],
                "excluded": ["正式科研结论", "论文成绩", "投稿终稿"],
            },
            "planned_contributions": [
                {
                    "contribution_id": "C-01",
                    "statement_zh": "补齐科研生产包到论文门禁的正向验证。",
                    "provenance": "original",
                    "source_refs": [],
                }
            ],
        }
    )
    return brief


def _bound_selection(
    task: dict[str, Any],
    run: dict[str, Any],
) -> dict[str, Any]:
    run_id = str(run["id"])
    metrics = run["result"]["metrics"]
    metric_name = (
        "top1_accuracy"
        if "top1_accuracy" in metrics
        else sorted(metrics)[0]
    )
    metric_value = metrics[metric_name]
    metric_label = {
        "top1_accuracy": "最高分分类准确率",
        "top5_accuracy": "前五分类准确率",
        "macro_f1": "类别等权平均调和分数",
    }.get(metric_name, "选定指标")
    raw_log = str(run["result"]["raw_log"])
    artifact_paths = [str(item) for item in run["artifacts"]]
    role_by_name = {
        "checkpoint.pt": "model_checkpoint",
        "metrics.json": "result_data",
        "predictions.json": "predictions",
        "run.json": "run_metadata",
    }
    asset_paths = [(raw_log, "raw_log")] + [
        (
            path,
            role_by_name.get(Path(path).name, "result_artifact"),
        )
        for path in artifact_paths
    ]
    assets = [
        {
            "asset_id": f"AST-{number:04d}",
            "source_object_ref": run_id,
            "path": path,
            "role": role,
            "required_for_writing": True,
            "copy_allowed": True,
            "license": "project-owned",
            "privacy_classification": "internal",
            "availability": "available",
            "omission_reason": "",
        }
        for number, (path, role) in enumerate(asset_paths, start=1)
    ]
    return {
        "schema": "cv-experiment-workflow.paper-package-selection.v1",
        "asset_mode": "hybrid",
        "paper_scope": {
            "title_hint": "生产交付接口门禁测试",
            "research_area": "计算机视觉",
            "goal": (
                "用无科研意义的临时结果验证生产封存、导出、接收、"
                "来源匹配和写作门禁。"
            ),
            "included_claim_ids": ["CLM-0001"],
            "excluded_topics": ["正式科研结论", "论文成绩", "投稿终稿"],
        },
        "claims": [
            {
                "claim_id": "CLM-0001",
                "kind": "result",
                "origin": "project",
                "statement_zh": (
                    f"临时接口校验的{metric_label}为 {metric_value}；"
                    "这个数字没有科研意义，"
                    "不得用于论文或投稿。"
                ),
                "statement_en": None,
                "maturity": "confirmed",
                "run_refs": [run_id],
                "metric_refs": [
                    {"run_id": run_id, "metric_name": metric_name}
                ],
                "source_refs": [],
                "idea_refs": [],
                "module_refs": [],
                "innovation_boundary": None,
                "allowed_sections": ["experiments"],
            }
        ],
        "experiments": [
            {
                "experiment_id": "EXP-0001",
                "title": "生产交付接口测试",
                "objective": "验证真实生产 API 生成的绑定证据包。",
                "task_id": str(task["id"]),
                "run_refs": [run_id],
                "claim_refs": ["CLM-0001"],
            }
        ],
        "source_uses": [],
        "assets": assets,
        "visuals": [],
        "supersedes_package_id": None,
    }


def _produce_gpu_bound_package(work_root: Path) -> dict[str, Any]:
    with _prepend_sys_path(RESEARCH_SCRIPTS):
        from PIL import Image
        from workflow_core.domain_packs import (
            create_domain_repository,
            resolve_domain_pack,
        )
        from workflow_core.engine import execute_task
        from workflow_core.evidence import record_evidence_transition
        from workflow_core.output_seal import seal_run_outputs
        from workflow_core.paper_package import (
            export_paper_package,
            save_research_brief,
            seal_paper_package,
        )
        from workflow_core.project import init_project
        from workflow_core.runs import load_run
        from workflow_core.tasking import create_task, load_task

        work_root.mkdir(parents=True, exist_ok=False)
        project = work_root / "r"
        init_project(project, "research-bound-project", layout="v2")
        brief_path = work_root / "b.json"
        _write_json_once(brief_path, _bound_research_brief())
        brief = save_research_brief(project, brief_path)

        created = create_domain_repository(
            project,
            resolve_domain_pack("cls@1.0.0"),
            work_root / "c",
            "full-flow-gpu-cls",
        )
        repository = created["repository"]
        codebase = created["codebase"]
        repository_root = Path(repository["repo_path"]).resolve(strict=True)
        data_relative = (
            Path("fixtures")
            / "full-flow-gpu-interface-check"
        )
        data_root = repository_root / data_relative
        for split, count in (("train", 2), ("val", 1)):
            for class_name, level in (("dark", 20), ("bright", 230)):
                directory = data_root / split / class_name
                directory.mkdir(parents=True)
                for index in range(count):
                    value = level + index if level < 128 else level - index
                    Image.new(
                        "RGB",
                        (8, 8),
                        (value, value, value),
                    ).save(directory / f"{index}.png")
        _run_git_text(
            repository_root,
            "add",
            "--",
            data_relative.as_posix(),
        )
        _commit_test_fixture(
            repository_root,
            "test: add tiny portable GPU interface fixture",
        )
        bound_commit = _run_git_text(
            repository_root,
            "rev-parse",
            "HEAD",
        )

        task = create_task(
            project,
            owner_request="验证生产 1.5.0 GPU 绑定证据包到 PaperFlow 当前门禁",
            route="tune",
            target_refs=["temporary-gpu-interface-check"],
            route_inputs={
                "config": {
                    "mode": "local_imagefolder",
                    "data_root": data_relative.as_posix(),
                    "dataset_id": "full-flow-gpu-interface-check",
                    "version": "1",
                    "source_uri": (
                        "https://example.org/full-flow-gpu-interface-check"
                    ),
                    "device": "cuda",
                },
                "seed": 7,
                "debug_required": True,
                "changes_code_behavior": False,
                "code_verified": True,
                "primary_metric": "top1_accuracy",
                "code_binding": {
                    "codebase_id": codebase["id"],
                    "branch": repository["default_branch"],
                    "commit": bound_commit,
                    "tag": None,
                },
            },
            budget={"max_runs": 2},
            stop_condition={"max_failures": 1},
        )
        debug = execute_task(
            project,
            str(task["id"]),
            purpose="debug",
        )
        if (
            debug.get("evidence_level") != "debug"
            or debug.get("task", {}).get("stage") != "preparing"
        ):
            raise AssertionError(
                f"GPU 生产 Task 没有先完成 debug 闭环：{debug}"
            )
        first = execute_task(project, str(task["id"]), purpose="evidence")
        if first.get("status") != "artifact_seal_pending":
            raise AssertionError(
                f"GPU 生产 Run 没有进入输出封存门禁：{first}"
            )
        run_id = str(first["run"]["id"])
        seal_run_outputs(project, run_id)
        second = execute_task(project, str(task["id"]), purpose="evidence")
        if second.get("evidence_level") != "single_run":
            raise AssertionError("GPU 生产 Run 没有形成 single_run 证据")
        record_evidence_transition(
            project,
            run_id,
            "confirmed",
            evidence_refs=[run_id],
            reason="临时 GPU 生产接口链路已独立复核",
            proposed_by="full-flow-producer",
            checked_by="full-flow-reviewer",
            applied_by="full-flow-producer",
        )
        task = load_task(project, str(task["id"]))
        run = load_run(project, run_id)
        attestation = run["frozen"]["environment"].get(
            "central_cuda_attestation"
        )
        if (
            not isinstance(attestation, dict)
            or attestation.get("probe", {}).get("result") != 8.0
            or attestation.get("device_name") in {None, ""}
        ):
            raise AssertionError("GPU 生产 Run 缺少中央 CUDA 真算子证明")
        selection_path = work_root / "s.json"
        _write_json_once(selection_path, _bound_selection(task, run))
        sealed = seal_paper_package(
            project,
            str(brief["brief_id"]),
            selection_path,
        )
        package = work_root / "d" / str(sealed["package_id"])
        package.parent.mkdir()
        export_paper_package(
            project,
            str(sealed["package_id"]),
            "hybrid",
            package,
        )
        return {
            "package": str(package.resolve()),
            "debug_run_id": str(debug["run"]["id"]),
            "run_id": run_id,
            "cuda_python_sha256": attestation[
                "python_executable_sha256"
            ],
            "device_name": attestation["device_name"],
            "torch_version": attestation["torch_version"],
            "cuda_runtime": attestation["cuda_runtime"],
            "compute_capability": attestation["compute_capability"],
            "central_cuda_probe": attestation["probe"],
        }


def _run_gpu_producer_subprocess(
    cuda_python: Path,
    work_root: Path,
) -> dict[str, Any]:
    result_path = work_root.parent / "gpu-producer-result.json"
    helper = (
        "import runpy,sys\n"
        "from pathlib import Path\n"
        "ns=runpy.run_path(sys.argv[1],run_name='gpu_helper')\n"
        "result=ns['_produce_gpu_bound_package'](Path(sys.argv[2]))\n"
        "ns['_write_json_once'](Path(sys.argv[3]),result)\n"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(RESEARCH_SCRIPTS)
    environment["CUDA_VISIBLE_DEVICES"] = environment.get(
        "CV_WORKFLOW_CUDA_VISIBLE_DEVICES",
        "0",
    )
    process = subprocess.run(
        [
            str(cuda_python),
            "-B",
            "-X",
            "utf8",
            "-c",
            helper,
            str(Path(__file__).resolve()),
            str(work_root),
            str(result_path),
        ],
        cwd=RESEARCH_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=180,
    )
    if process.returncode != 0:
        raise RuntimeError(
            "GPU 科研生产子进程失败："
            + (process.stderr.strip() or process.stdout.strip())
        )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("GPU 科研生产子进程结果无效")
    return result


def _prove_production_bound_package_accepted(
    paperflow_root: Path,
    work_root: Path,
    cuda_python: Path,
) -> dict[str, Any]:
    gpu_producer = _run_gpu_producer_subprocess(
        cuda_python,
        work_root,
    )
    package = Path(str(gpu_producer["package"])).resolve(strict=True)

    with _prepend_sys_path(paperflow_root):
        from paperflow_v2.paper_workspace import create_paper_workspace
        from paperflow_v2.research_gate import (
            require_complete_research_package_locked,
        )
        from paperflow_v2.research_package import (
            import_research_package,
            validate_research_package,
        )
        from paperflow_v2.research_source_matching import (
            match_research_package_sources,
        )

        validated = validate_research_package(package)
        producer = dict(validated.manifest["producer"])
        expected_producer = {
            "skill_id": "cv-experiment-workflow",
            "release_version": "1.5.0",
            "system_version": "SYS-V2.13.0",
        }
        if producer != expected_producer:
            raise AssertionError(f"科研生产者版本不符：{producer}")
        paper = create_paper_workspace(
            work_root / "p",
            paper_id="q",
            title="Production Bound Package Gate Smoke",
            research_area="computer vision",
            goal="validate the production handoff contract",
        )
        imported = import_research_package(
            paper.root,
            package,
            work_root / "u",
        )
        matching = match_research_package_sources(
            paper.root,
            work_root / "v",
        )
        gate = require_complete_research_package_locked(paper)
        if (
            imported["status"] != "imported"
            or matching["status"] != "complete"
            or gate["status"] != "pass"
            or gate["complete_for_paper"] is not True
        ):
            raise AssertionError(
                "科研生产包没有通过 PaperFlow 当前接收、来源匹配和写作门禁"
            )
        return {
            "package_id": validated.package_id,
            "producer": producer,
            "source_run_refs": list(
                validated.manifest["source_run_refs"]
            ),
            "debug_run_ref": gpu_producer["debug_run_id"],
            "debug_closed_before_evidence": True,
            "paperflow_receiver_validation": "pass",
            "paperflow_import": imported["status"],
            "paperflow_source_matching": matching["status"],
            "paperflow_writing_gate": gate["status"],
            "complete_for_paper": gate["complete_for_paper"],
            "package_retention": "temporary_only",
            "not_for_submission": True,
            "declared_device": "cuda",
            "central_cuda_attestation": "pass",
            "cuda_python_sha256": gpu_producer["cuda_python_sha256"],
            "device_name": gpu_producer["device_name"],
            "torch_version": gpu_producer["torch_version"],
            "cuda_runtime": gpu_producer["cuda_runtime"],
            "compute_capability": gpu_producer["compute_capability"],
            "central_cuda_probe": gpu_producer["central_cuda_probe"],
            "approved": False,
            "published": False,
        }


def _prove_debug_export_rejected(work_root: Path) -> dict[str, Any]:
    with _prepend_sys_path(RESEARCH_SCRIPTS):
        from workflow_core.evidence import record_evidence_transition
        from workflow_core.paper_package import (
            save_research_brief,
            seal_paper_package,
        )
        from workflow_core.project import init_project
        from workflow_core.runs import create_run, finish_run, start_run
        from workflow_core.tasking import (
            create_task,
            load_task,
            readiness,
            transition_task,
        )

        project = work_root / "research-debug-project"
        init_project(project, "research-debug-project", layout="v2")
        brief_path = work_root / "research-brief.json"
        _write_json_once(brief_path, _research_brief())
        brief = save_research_brief(project, brief_path)
        task = create_task(
            project,
            owner_request="验证 debug 结果不能交给 PaperFlow",
            route="tune",
            target_refs=["debug-boundary"],
            route_inputs={
                "debug_required": True,
                "primary_metric": "score",
            },
            budget={"max_runs": 1},
            stop_condition={"max_failures": 1},
        )
        transition_task(project, str(task["id"]), "preparing")
        ready = readiness(
            project,
            str(task["id"]),
            target_clear=True,
            template_runnable=True,
            mutation_allowed=True,
            code_verified=True,
        )
        if ready != {"status": "pass"}:
            raise AssertionError(f"测试 Task readiness 失败：{ready}")
        task = load_task(project, str(task["id"]))
        frozen = {
            "code": {"repository": "test-only", "commit": "d" * 40},
            "config": {
                "metric_definition": {
                    "score": "仅供门禁测试的无科研意义分数"
                }
            },
            "seed": 7,
            "data": {
                "dataset_id": "test-fixture-only",
                "version": "1",
                "split": "debug",
            },
            "environment": {"backend": "test", "device": "cpu"},
        }
        run = create_run(project, str(task["id"]), frozen, purpose="debug")
        artifacts = project / "artifacts"
        artifacts.mkdir()
        (artifacts / "debug.log").write_text(
            "score=0.5; test_fixture_only=true\n",
            encoding="utf-8",
        )
        (artifacts / "metrics.json").write_text(
            '{"score":0.5,"test_fixture_only":true}\n',
            encoding="utf-8",
        )
        start_run(project, str(run["id"]), process_id=os.getpid())
        run = finish_run(
            project,
            str(run["id"]),
            outcome="succeeded",
            exit_code=0,
            metrics={"score": 0.5},
            raw_log="artifacts/debug.log",
            implementation="valid",
            interface="valid",
            data="valid",
            metrics_quality="valid",
            hypothesis="not_evaluated",
            limitations=["test_fixture_only"],
            suggestions=[],
            artifacts=["artifacts/metrics.json"],
        )
        record_evidence_transition(
            project,
            str(run["id"]),
            "debug",
            evidence_refs=[str(run["id"])],
            reason="测试专用调试运行",
            proposed_by="full-flow-smoke",
            checked_by="full-flow-smoke",
            applied_by="full-flow-smoke",
        )
        selection_path = work_root / "debug-selection.json"
        _write_json_once(selection_path, _debug_selection(task, run))
        try:
            seal_paper_package(
                project,
                str(brief["brief_id"]),
                selection_path,
            )
        except ValueError as error:
            message = str(error)
        else:
            raise AssertionError("生产封存器错误接受了 debug Run")
        if not any(
            marker in message
            for marker in (
                "confirmed",
                "debug",
                "closed",
                "正式",
                "Evidence",
                "Codebase",
            )
        ):
            raise AssertionError(
                "debug Run 虽被拒绝，但拒绝原因没有命中 1.5 安全门："
                + message
            )

        forged = _debug_selection(task, run)
        forged["test_fixture_only"] = True
        forged_path = work_root / "forged-test-field-selection.json"
        _write_json_once(forged_path, forged)
        try:
            seal_paper_package(
                project,
                str(brief["brief_id"]),
                forged_path,
            )
        except ValueError as error:
            forged_message = str(error)
        else:
            raise AssertionError(
                "生产封存器错误接受了 test_fixture_only 字段"
            )
        return {
            "debug_export": "rejected",
            "debug_run_id": str(run["id"]),
            "rejection": message,
            "test_fixture_field": "rejected",
            "test_fixture_field_rejection": forged_message,
        }


def _readonly_database_snapshot(
    library_root: Path,
    target: Path,
) -> dict[str, Any]:
    database = library_root / "database" / "knowledge.db"
    before = _sha256_file(database)
    source = sqlite3.connect(_readonly_sqlite_uri(database), uri=True)
    try:
        source.row_factory = sqlite3.Row
        source.execute("PRAGMA query_only=ON")
        integrity_rows = [
            str(row[0]) for row in source.execute("PRAGMA integrity_check")
        ]
        foreign_rows = list(source.execute("PRAGMA foreign_key_check"))
        model_rows = [
            dict(row)
            for row in source.execute(
                """
                SELECT model, dimensions, COUNT(*) AS count
                FROM embeddings
                GROUP BY model, dimensions
                ORDER BY model, dimensions
                """
            )
        ]
        paper_count = int(
            source.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        )
        chunk_count = int(
            source.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        )
    finally:
        source.close()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(database, target)
    after = _sha256_file(database)
    if before != after:
        raise AssertionError("正式知识库在只读验证期间发生变化")
    if _sha256_file(target) != before:
        raise AssertionError("正式知识库只读副本与源文件不一致")
    if integrity_rows != ["ok"]:
        raise AssertionError(f"SQLite integrity_check 失败：{integrity_rows}")
    if foreign_rows:
        raise AssertionError("SQLite 存在外键错误")
    bge_rows = [
        row for row in model_rows if row["model"] == "BAAI/bge-m3"
    ]
    if not bge_rows:
        raise AssertionError("正式知识库没有 BAAI/bge-m3 向量")
    return {
        "open_mode": "mode=ro&immutable=1; query_only=ON",
        "integrity_check": "ok",
        "foreign_key_errors": 0,
        "formal_database_sha256": before,
        "formal_database_unchanged": True,
        "papers": paper_count,
        "chunks": chunk_count,
        "embedding_models": model_rows,
    }


def _readonly_sqlite_uri(database: Path) -> str:
    """生成不会创建 journal、WAL 或旁路文件的正式知识库只读 URI。"""
    return Path(database).resolve().as_uri() + "?mode=ro&immutable=1"


def _resolve_device(requested: str) -> str:
    if requested not in {"auto", "cuda", "cpu"}:
        raise ValueError("device 必须是 auto、cuda 或 cpu")
    try:
        import torch
    except ImportError as error:
        if requested == "cuda":
            raise RuntimeError("请求 CUDA，但当前 Python 没有 PyTorch") from error
        return "cpu"
    available = bool(torch.cuda.is_available())
    if requested == "cuda" and not available:
        raise RuntimeError(
            "请求 CUDA，但当前 Python 的 PyTorch 没有可用 CUDA；拒绝假装使用 GPU"
        )
    if requested == "auto":
        return "cuda" if available else "cpu"
    return requested


def _retrieve_real_evidence(
    paperflow_root: Path,
    database_copy: Path,
    artifacts_root: Path,
    *,
    requested_device: str,
) -> dict[str, Any]:
    with _prepend_sys_path(paperflow_root):
        from paperflow_v2.embeddings import SentenceTransformerBackend
        from paperflow_v2.evidence import (
            build_evidence_pack,
            write_evidence_pack,
        )
        from paperflow_v2.retrieval import hybrid_search
        from paperflow_v2.store import EvidenceStoreV2

        device = _resolve_device(requested_device)
        query = "generalized zero-shot learning semantic alignment"
        started = time.monotonic()
        vector = SentenceTransformerBackend(
            "BAAI/bge-m3",
            device=device,
            local_files_only=True,
            batch_size=1,
            max_seq_length=128,
        ).embed_query(query)
        embedding_seconds = round(time.monotonic() - started, 3)
        store = EvidenceStoreV2(database_copy)
        results = hybrid_search(
            store,
            query=query,
            query_vector=vector,
            model="BAAI/bge-m3",
            limit=5,
        )
        if not results:
            raise AssertionError("正式知识库混合检索没有返回结果")
        retrieval_sources = sorted(
            {
                source
                for result in results
                for source in result.retrieval_sources
            }
        )
        if retrieval_sources != ["fts", "vector"]:
            raise AssertionError(
                f"混合检索没有同时走 FTS5 与 BGE-M3：{retrieval_sources}"
            )
        pack = build_evidence_pack(query, results, database_copy)
        evidence_path = write_evidence_pack(
            pack,
            artifacts_root / "formal-knowledge-evidence-pack.json",
        )
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        for item in payload["items"]:
            if (
                not item["paper_id"]
                or not item["section"]
                or not isinstance(item["page_start"], int)
                or not item["text"]
                or len(item["source_sha256"]) != 64
            ):
                raise AssertionError("Evidence Pack 缺少可追溯字段")
        return {
            "embedding_model": "BAAI/bge-m3",
            "embedding_dimensions": len(vector),
            "embedding_device": device,
            "embedding_seconds": embedding_seconds,
            "retrieval_sources": retrieval_sources,
            "rrf": True,
            "result_count": len(results),
            "evidence_pack": str(evidence_path),
            "evidence_pack_id": payload["pack_id"],
        }


def _load_paperflow_test_helpers(paperflow_root: Path):
    """Load fixture builders that are intentionally absent from production APIs."""

    with _prepend_sys_path(paperflow_root):
        module = importlib.import_module(
            "tests_v2.test_research_package_writing_gates"
        )
    return module


def _validate_package_negative_cases(
    paperflow_root: Path,
    package: Path,
    work_root: Path,
) -> dict[str, str]:
    with _prepend_sys_path(paperflow_root):
        from paperflow_v2.research_package import (
            ResearchPackageError,
            import_research_package,
            validate_research_package,
        )
        from paperflow_v2.research_gate import (
            require_complete_research_package_locked,
        )
        fixture_helpers = importlib.import_module(
            "tests_v2.test_research_package_v2"
        )

        validated = validate_research_package(package)
        tampered = work_root / "tampered-fixture" / package.name
        tampered.parent.mkdir()
        shutil.copytree(package, tampered)
        with (tampered / "claims.jsonl").open("ab") as handle:
            handle.write(b" ")
        try:
            validate_research_package(tampered)
        except ResearchPackageError:
            tampered_status = "rejected"
        else:
            raise AssertionError("接收器错误接受了篡改包")

        missing_required = (
            work_root / "missing-required-fixture" / package.name
        )
        missing_required.parent.mkdir()
        shutil.copytree(package, missing_required)
        (missing_required / "assets.jsonl").unlink()
        try:
            validate_research_package(missing_required)
        except ResearchPackageError:
            missing_required_status = "rejected"
        else:
            raise AssertionError("接收器错误接受了缺少必需文件的包")

        incomplete, _files = fixture_helpers._build_package(
            work_root / "incomplete-fixture"
        )
        expected_missing = [
            "required_asset_license_unknown:AST-0002",
            "required_asset_not_copyable:AST-0002",
            "required_asset_privacy_blocked:AST-0002",
            "required_asset_unavailable:AST-0002",
        ]
        fixture_helpers._add_policy_blocked_required_asset(
            incomplete,
            missing_requirements=expected_missing,
        )
        incomplete_paper = fixture_helpers._workspace(
            work_root / "incomplete-paper"
        )
        imported = import_research_package(
            incomplete_paper.root,
            incomplete,
            work_root / "unused-compatibility-root",
        )
        if imported["status"] != "imported":
            raise AssertionError("不完整包没有进入受控本地副本")
        try:
            require_complete_research_package_locked(incomplete_paper)
        except ValueError:
            incomplete_status = "quarantined_from_writing"
        else:
            raise AssertionError("不完整包错误通过了写作门禁")
        return {
            "package_id": validated.package_id,
            "producer_release": str(
                validated.manifest["producer"]["release_version"]
            ),
            "hash_validation": "pass",
            "tampered": tampered_status,
            "missing_required_file": missing_required_status,
            "incomplete": incomplete_status,
        }


def _paperflow_candidate_flow(
    paperflow_root: Path,
    work_root: Path,
    artifacts_root: Path,
) -> dict[str, Any]:
    helpers = _load_paperflow_test_helpers(paperflow_root)
    with _prepend_sys_path(paperflow_root):
        from paperflow_v2.method_combinations import (
            discover_method_cards,
            discover_method_combinations,
        )
        from paperflow_v2.paper_assembly import assemble_paper, audit_paper
        from paperflow_v2.paper_selection import save_paper_method_selection
        from paperflow_v2.paper_workspace import create_paper_workspace
        from paperflow_v2.research_gate import (
            require_complete_research_package_locked,
            require_current_research_evidence_locked,
        )
        from paperflow_v2.writing_models import load_json_object

        cards = discover_method_cards(paperflow_root)
        combinations = discover_method_combinations(paperflow_root)
        if len(cards) != 13 or len(combinations) != 3:
            raise AssertionError(
                f"方法库数量不符：cards={len(cards)}, combinations={len(combinations)}"
            )
        layout, paper, upstream_package, spec, gate = (
            helpers._finalized_v2_six_section_project(work_root)
        )
        package_checks = _validate_package_negative_cases(
            paperflow_root,
            upstream_package,
            work_root,
        )
        current_gate = require_current_research_evidence_locked(paper)
        if (
            current_gate["status"] != "pass"
            or not current_gate["complete_for_paper"]
            or current_gate["check_digest"] != gate["check_digest"]
        ):
            raise AssertionError("导入后的科研交付包门禁未通过")

        chosen = next(
            item
            for item in combinations
            if item["combination_id"] == "COMBO-BALANCED-HYBRID"
        )
        selection = save_paper_method_selection(
            paper,
            workspace=paperflow_root,
            section_cards=chosen["sections"],
            material_components=[
                {
                    "component_id": "test-fixture-research-package",
                    "enabled": True,
                }
            ],
            shared_library_enabled=False,
            combination_path=Path(str(chosen["path"])),
        )
        if selection["selection_id"] != "selection-v001":
            raise AssertionError("六章方法选择没有生成 selection-v001")

        section_statuses: dict[str, str] = {}
        for section in PAPER_SECTIONS:
            audit = load_json_object(
                paper.runs
                / f"WR-V2-ASSEMBLY-{section}"
                / "final"
                / "audit.json"
            )
            section_statuses[section] = str(audit["status"])
        if set(section_statuses.values()) != {"pass"}:
            raise AssertionError(f"逐章检查失败：{section_statuses}")

        output = paper.output / "paper-v2-test-candidate"
        assembled = assemble_paper(
            layout,
            spec_path=spec,
            output_dir=output,
            paper_workspace=paper,
        )
        audited = audit_paper(output)
        if assembled["status"] != "pass" or audited["status"] != "pass":
            raise AssertionError("六章组装或整篇审计失败")
        persisted_output = artifacts_root / "paper-candidate"
        shutil.copytree(output, persisted_output)

        negative_paper = create_paper_workspace(
            work_root / "negative-papers",
            paper_id="no-research-package",
            title="No Research Package",
        )
        try:
            require_complete_research_package_locked(negative_paper)
        except ValueError:
            unverified_result_gate = "rejected"
        else:
            raise AssertionError("没有科研交付包的论文错误通过结果门禁")

        preview = artifacts_root / "paperflow-test-preview.html"
        english_path = output / "paper_english.md"
        chinese_path = output / "paper_chinese.md"
        preview.write_text(
            """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8">
<title>PaperFlow test fixture preview</title>
<style>
body{max-width:920px;margin:40px auto;padding:0 24px;font:16px/1.7 system-ui}
.warning{padding:16px;border:2px solid #b42318;background:#fff1f0;color:#7a271a}
pre{white-space:pre-wrap;background:#f6f8fa;padding:16px}
</style>
<h1>PaperFlow 测试候选预览</h1>
<p class="warning"><strong>not_for_submission</strong>：
这是 test_fixture_only 流程验证，不是科研结果、投稿稿件或可发布论文。</p>
<h2>English candidate</h2><pre>"""
            + html.escape(english_path.read_text(encoding="utf-8"))
            + """</pre><h2>中文候选</h2><pre>"""
            + html.escape(chinese_path.read_text(encoding="utf-8"))
            + "</pre></html>\n",
            encoding="utf-8",
            newline="\n",
        )
        if list(paper.root.rglob("approval.json")):
            raise AssertionError("测试流程不应生成 approval.json")
        if list(paper.root.rglob("*publish*")):
            raise AssertionError("测试流程不应生成发布记录")

        fixture_boundary = {
            "schema": "full-flow-smoke-fixture-boundary/v1",
            "test_fixture_only": True,
            "not_for_submission": True,
            "approved": False,
            "published": False,
            "source_package_id": package_checks["package_id"],
            "source_package_producer_release": package_checks[
                "producer_release"
            ],
            "note": (
                "正向包由 PaperFlow tests_v2 内部 builder 创建；"
                "科研生产 API 没有这个 builder。"
            ),
        }
        fixture_boundary_path = artifacts_root / "fixture-boundary.json"
        _write_json_once(fixture_boundary_path, fixture_boundary)
        return {
            "method_cards": len(cards),
            "combinations": len(combinations),
            "selection": selection["selection_id"],
            "source_matching": current_gate["source_matching"]["status"],
            "research_gate": current_gate["status"],
            "background_evidence": "available",
            "unverified_result_gate": unverified_result_gate,
            "section_audit": "pass",
            "section_statuses": section_statuses,
            "assembly": assembled["status"],
            "paper_audit": audited["status"],
            "package_checks": package_checks,
            "output": str(persisted_output),
            "preview": str(preview),
            "fixture_boundary": str(fixture_boundary_path),
        }


def run_full_flow(
    *,
    paperflow_root: Path,
    library_root: Path,
    artifacts_root: Path,
    cuda_python: Path,
    device: str = "auto",
) -> dict[str, Any]:
    (
        paperflow_root,
        library_root,
        artifacts_root,
        cuda_python,
    ) = _validate_roots(
        paperflow_root,
        library_root,
        artifacts_root,
        cuda_python,
    )
    started = time.monotonic()
    artifacts_root.mkdir(parents=True, exist_ok=False)
    database_copy = artifacts_root / "knowledge-readonly-copy.db"
    knowledge = _readonly_database_snapshot(library_root, database_copy)
    knowledge.update(
        _retrieve_real_evidence(
            paperflow_root,
            database_copy,
            artifacts_root,
            requested_device=device,
        )
    )
    volume_root = Path(artifacts_root.anchor)
    with tempfile.TemporaryDirectory(
        prefix="pfsm-",
        dir=volume_root,
    ) as temporary:
        work_root = Path(temporary)
        research = _prove_debug_export_rejected(work_root)
        research["production_bound_package"] = (
            _prove_production_bound_package_accepted(
                paperflow_root,
                work_root / "b",
                cuda_python,
            )
        )
        paperflow = _paperflow_candidate_flow(
            paperflow_root,
            work_root / "p",
            artifacts_root,
        )
    report = {
        "schema": "cv-experiment-workflow.full-flow-smoke/v1",
        "status": "pass",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "safety": {
            "test_fixture_only": True,
            "not_for_submission": True,
            "approved": False,
            "published": False,
            "formal_database_read_only": True,
            "shutdown_requested": False,
        },
        "research": research,
        "knowledge_base": knowledge,
        "paperflow": paperflow,
        "positive_proofs": {
            "production_1_5_to_current_paperflow_gate": {
                "status": "pass",
                "purpose": (
                    "科研端生产 seal/export 到 PaperFlow 当前接收、"
                    "来源匹配和写作门禁"
                ),
                "package_id": research["production_bound_package"][
                    "package_id"
                ],
                "producer": research["production_bound_package"]["producer"],
                "central_cuda_attestation": research[
                    "production_bound_package"
                ]["central_cuda_attestation"],
                "device_name": research["production_bound_package"][
                    "device_name"
                ],
                "drives_six_section_writing": False,
                "package_retention": "temporary_only",
            },
            "paperflow_fixture_to_six_section_candidate": {
                "status": "pass",
                "purpose": "PaperFlow 测试包驱动六章候选写作与整篇审计",
                "package_id": paperflow["package_checks"]["package_id"],
                "producer_release": paperflow["package_checks"][
                    "producer_release"
                ],
                "test_fixture_only": True,
                "drives_six_section_writing": True,
            },
        },
        "artifacts": {
            "root": str(artifacts_root),
            "preview": paperflow["preview"],
            "evidence_pack": knowledge["evidence_pack"],
            "fixture_boundary": paperflow["fixture_boundary"],
        },
    }
    report_path = artifacts_root / "full-flow-report.json"
    report["artifacts"]["report"] = str(report_path)
    _write_json_once(report_path, report)
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "运行 test_fixture_only、not_for_submission 的科研到论文闭环。"
        )
    )
    parser.add_argument("--paperflow-root", type=Path, required=True)
    parser.add_argument(
        "--library-root",
        type=Path,
        required=True,
        help="正式 PaperFlow runtime_v2；只读打开",
    )
    parser.add_argument("--artifacts-root", type=Path, required=True)
    parser.add_argument(
        "--cuda-python",
        type=Path,
        required=True,
        help="装有可用 CUDA PyTorch 的 Python 解释器",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cuda", "cpu"),
        default="auto",
        help="仅用于 BGE-M3 查询向量；auto 优先 CUDA",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    args = _build_parser().parse_args(argv)
    try:
        report = run_full_flow(
            paperflow_root=args.paperflow_root,
            library_root=args.library_root,
            artifacts_root=args.artifacts_root,
            cuda_python=args.cuda_python,
            device=args.device,
        )
    except Exception as error:
        payload = {
            "schema": "cv-experiment-workflow.full-flow-smoke/v1",
            "status": "fail",
            "error": str(error),
            "error_type": type(error).__name__,
            "shutdown_requested": False,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
