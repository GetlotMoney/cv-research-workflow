from __future__ import annotations

import html
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .domain_packs import materialize_domain_repository, resolve_domain_pack
from .codebases import CODEBASE_SCHEMA, register_codebase
from .io import atomic_create_json, atomic_write_json, read_bounded_json_object
from .locking import project_write_lock
from .project import init_project, is_link_or_reparse


REPOSITORY_SCHEMA = "cv-experiment-workflow.framework-repository.v1"
FRAMEWORK_SCHEMA = "cv-experiment-workflow.framework.v1"
EXPERIMENT_SCHEMA = "cv-experiment-workflow.framework-experiment.v1"
ROUTES = ("reproduction", "tuning", "ablation", "innovation")
ROUTE_PREFIXES = {
    "reproduction": "repro",
    "tuning": "tune",
    "ablation": "ablate",
    "innovation": "innov",
}
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
IDEA_ID = re.compile(r"IDEA-[0-9]{4}")
SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
COMMIT = re.compile(r"[0-9a-f]{40}")
MAX_IMPORTED_FRAMEWORK_BYTES = 64 * 1024 * 1024
GZSL_COMPONENTS = (
    "data",
    "model",
    "losses",
    "trainer",
    "evaluator",
    "inferencer",
    "metrics",
    "configs",
)
GZSL_STANDARD_FILES = (
    "workflow_adapter.py",
    "configs/baseline.json",
    "gzsl/data.py",
    "gzsl/model.py",
    "gzsl/train.py",
    "gzsl/evaluate.py",
    "gzsl/infer.py",
    "gzsl/metrics.py",
    "gzsl/runtime.py",
    "gzsl/cuda_probe.py",
    "gzsl/dataset_manifest.py",
    "tests/domain_pack/test_metric_contract.py",
    "tests/domain_pack/test_synthetic_smoke.py",
)


def create_gzsl_repository(
    destination: Path,
    *,
    name: str,
    environment_name: str,
) -> dict[str, Any]:
    """创建一个把代码、Framework、实验和证据放在同一仓库的 GZSL 项目。"""
    selected_name = _text(name, "仓库名称")
    selected_environment = _text(environment_name, "Conda 环境名称")
    if selected_environment != "dvsr_gpu":
        raise ValueError("Core V1 固定使用 Conda 环境 dvsr_gpu")
    target = Path(destination).expanduser().absolute()
    repository = materialize_domain_repository(
        resolve_domain_pack("gzsl@1.1.1"),
        target,
        selected_name,
    )
    try:
        init_project(target, selected_name, layout="v2")
        _git(
            target,
            "add",
            "--",
            "AGENTS.md",
            "SKILL.md",
            "WORKFLOW.md",
        )
        _git(
            target,
            "-c",
            "user.name=CV Workflow",
            "-c",
            "user.email=cv-workflow@localhost",
            "commit",
            "-m",
            "chore: initialize GZSL workflow",
        )
        commit = _head(target)
        tag = "framework/gzsl-base/v1.0.0"
        _create_tag(target, tag, commit, "Stable GZSL base framework")
        codebase = _register_compatibility_codebase(
            target,
            code_root=target,
            name=selected_name,
            commit=commit,
            branch="main",
            tag=tag,
            source="domain_pack",
        )
        with project_write_lock(target):
            control = _control(target)
            framework = _create_framework_locked(
                control,
                slug="gzsl-base",
                title="GZSL Base",
                commit=commit,
                tag=tag,
                version="1.0.0",
                parent_framework=None,
                idea_ref=None,
                source_experiment=None,
            )
            repository_record = {
                "schema": REPOSITORY_SCHEMA,
                "name": selected_name,
                "direction": "gzsl",
                "repo_path": str(target.resolve(strict=True)),
                "execution_policy": "gpu_only",
                "cpu_fallback": False,
                "environment_name": selected_environment,
                "active_framework": "gzsl-base",
                "kernel_codebase_id": codebase["id"],
                "created_at": _utc_now(),
            }
            path = control / "repository.json"
            if not atomic_create_json(
                path,
                repository_record,
                transaction_id=uuid.uuid4().hex,
            ):
                raise FileExistsError(f"仓库记录已存在，拒绝覆盖：{path}")
        _require_clean(target)
        return {
            "repository": {**repository, "workflow_commit": commit},
            "framework": framework,
        }
    except BaseException as error:
        raise RuntimeError(
            "GZSL 仓库已开始创建，失败现场会保留，不自动删除；"
            f"路径：{target}；原因：{error}"
        ) from error


def import_standardized_gzsl_framework(
    repository: Path,
    *,
    source_repository: Path,
    framework_slug: str,
    title: str,
    version: str,
    component_map: dict[str, Any],
    equivalence: dict[str, Any],
    trusted_execution_acknowledged: bool,
) -> dict[str, Any]:
    """把已由用户或 AI 标准化并验证的外来 GZSL 代码登记成顶层 Framework。"""
    root = _repository(repository)
    if trusted_execution_acknowledged is not True:
        raise ValueError(
            "外来代码会在本机 GPU 环境运行；必须先人工审读代码并明确确认信任"
        )
    source = _real_directory(source_repository, "外来 GZSL 仓库")
    if not (source / ".git").exists():
        raise ValueError("外来 GZSL 框架必须先克隆成独立 Git 仓库")
    if source == root or root in source.parents or source in root.parents:
        raise ValueError("外来仓库必须与当前受管仓库相互独立")
    _require_clean(source)
    selected_slug = _slug(framework_slug, "Framework slug")
    selected_title = _text(title, "Framework 标题")
    selected_version = _semver(version)
    normalized_map = _normalize_component_map(source, component_map)
    normalized_equivalence = _normalize_equivalence(source, equivalence)
    _require_standardized_gzsl_files(source)
    verification = _verify_standardized_gzsl_source(
        source,
        trusted_execution_acknowledged=trusted_execution_acknowledged,
    )
    _require_clean(source)

    with project_write_lock(root):
        control = _control(root)
        target_framework = control / "frameworks" / selected_slug
        if os.path.lexists(target_framework):
            raise FileExistsError(f"Framework 已存在：{selected_slug}")
        branch = f"framework/import/{selected_slug}"
        branch_ref = _run_git(
            root,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        )
        if branch_ref.returncode == 0:
            raise FileExistsError(f"Framework 导入分支已存在：{branch}")
        if branch_ref.returncode != 1:
            raise RuntimeError("无法检查 Framework 导入分支")
        import_worktree = root / ".worktrees" / f"framework-import-{selected_slug}"
        if os.path.lexists(import_worktree):
            raise FileExistsError(f"Framework 导入 Worktree 已存在：{import_worktree}")
        import_worktree.parent.mkdir(exist_ok=True)
        _git(root, "worktree", "add", "-b", branch, str(import_worktree), "HEAD")

    try:
        _replace_worktree_with_standardized_source(
            import_worktree,
            source,
        )
        _git(import_worktree, "add", "-A", "--", ".")
        _git(
            import_worktree,
            "-c",
            "user.name=CV Workflow",
            "-c",
            "user.email=cv-workflow@localhost",
            "commit",
            "--allow-empty",
            "-m",
            f"feat: import standardized GZSL framework {selected_slug}",
        )
        commit = _head(import_worktree)
        tag = f"framework/{selected_slug}/v{selected_version}"
        _create_tag(root, tag, commit, f"Stable imported framework {selected_slug}")
        codebase = _register_compatibility_codebase(
            root,
            code_root=import_worktree,
            name=f"{selected_slug}-framework",
            commit=commit,
            branch=branch,
            tag=tag,
            source="external",
        )
        with project_write_lock(root):
            control = _control(root)
            framework = _create_framework_locked(
                control,
                slug=selected_slug,
                title=selected_title,
                commit=commit,
                tag=tag,
                version=selected_version,
                parent_framework=None,
                idea_ref=None,
                source_experiment=None,
            )
            source_record = {
                "schema": "cv-experiment-workflow.framework-source.v1",
                "kind": "external_standardized",
                "source_repository": str(source),
                "source_commit": _head(source),
                "import_branch": branch,
                "import_worktree": str(import_worktree.resolve(strict=True)),
                "kernel_codebase_id": codebase["id"],
                "component_map": normalized_map,
                "equivalence": normalized_equivalence,
                "verification": verification,
                "imported_at": _utc_now(),
            }
            atomic_write_json(
                control / "frameworks" / selected_slug / "source.json",
                source_record,
            )
        _require_clean(import_worktree)
        return {"framework": framework, "source": source_record}
    except BaseException as error:
        raise RuntimeError(
            "外来 GZSL Framework 导入失败；原仓库和失败现场均已保留，"
            f"不会自动删除。原因：{error}"
        ) from error


def create_framework_experiment(
    repository: Path,
    *,
    framework_slug: str,
    route: str,
    slug: str,
    title: str,
    summary: str,
    idea_ref: str | None = None,
    route_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = _repository(repository)
    selected_framework = _slug(framework_slug, "Framework slug")
    selected_route = _route(route)
    selected_slug = _slug(slug, "实验 slug")
    selected_title = _text(title, "实验标题")
    selected_summary = _text(summary, "实验说明")
    normalized_contract = _normalize_framework_route_contract(
        selected_route,
        route_contract,
    )
    if selected_route == "innovation":
        if idea_ref is None:
            raise ValueError("创新实验必须绑定一个 active Idea")
    elif idea_ref is not None:
        raise ValueError("只有创新实验可以绑定 Idea")

    with project_write_lock(root):
        control = _control(root)
        framework = _read_framework(control, selected_framework)
        idea_record = (
            _active_idea(control, idea_ref)
            if idea_ref is not None
            else None
        )
        normalized_idea = (
            idea_record["id"] if idea_record is not None else None
        )
        route_directory = _route_directory(
            control,
            selected_framework,
            selected_route,
        )
        experiment_id = _next_experiment_id(
            control,
            selected_route,
            selected_slug,
        )
        branch = (
            f"experiment/{selected_route}/"
            f"{selected_framework}/{experiment_id}"
        )
        _git(root, "branch", branch, framework["commit"])
        experiment_directory = route_directory / experiment_id
        experiment_directory.mkdir()
        record = {
            "schema": EXPERIMENT_SCHEMA,
            "id": experiment_id,
            "route": selected_route,
            "slug": selected_slug,
            "title": selected_title,
            "summary": selected_summary,
            "route_contract": normalized_contract,
            "parent_framework": selected_framework,
            "parent_commit": framework["commit"],
            "idea_ref": normalized_idea,
            "branch": branch,
            "worktree": None,
            "kernel_task_id": None,
            "kernel_codebase_id": None,
            "dataset_refs": [],
            "child_framework": None,
            "status": "planned",
            "created_at": _utc_now(),
        }
        path = experiment_directory / "experiment.json"
        if not atomic_create_json(
            path,
            record,
            transaction_id=uuid.uuid4().hex,
        ):
            raise FileExistsError(f"实验记录已存在，拒绝覆盖：{path}")
        if idea_record is not None:
            updated_idea = {
                **idea_record,
                "experiment_refs": [
                    *idea_record["experiment_refs"],
                    experiment_id,
                ],
                "updated_at": _utc_now(),
            }
            atomic_write_json(
                control
                / "idea-tree"
                / idea_record["id"]
                / "idea.json",
                updated_idea,
            )
        return record


def create_framework_idea(
    repository: Path,
    *,
    title: str,
    problem: str,
    mechanism: str,
    falsifiable_hypothesis: str,
    source_notes: list[dict[str, str]],
    parent_idea_ref: str | None = None,
) -> dict[str, Any]:
    root = _repository(repository)
    selected_title = _text(title, "Idea 标题")
    selected_problem = _text(problem, "Idea 问题")
    selected_mechanism = _text(mechanism, "Idea 机制")
    selected_hypothesis = _text(
        falsifiable_hypothesis,
        "Idea 可证伪假设",
    )
    normalized_sources = _normalize_source_notes(source_notes)
    with project_write_lock(root):
        control = _control(root)
        tree = control / "idea-tree"
        tree.mkdir(exist_ok=True)
        existing: list[int] = []
        for entry in tree.iterdir():
            match = re.fullmatch(r"IDEA-([0-9]{4})", entry.name)
            if match is None or not entry.is_dir() or is_link_or_reparse(entry):
                raise ValueError(f"Idea 树存在未知条目：{entry}")
            existing.append(int(match.group(1)))
        number = max(existing, default=0) + 1
        if number > 9999:
            raise ValueError("Idea 编号已达到 9999")
        idea_id = f"IDEA-{number:04d}"
        if parent_idea_ref is not None:
            parent = _active_idea(control, parent_idea_ref)
            normalized_parent = parent["id"]
        else:
            normalized_parent = None
        directory = tree / idea_id
        directory.mkdir()
        timestamp = _utc_now()
        record = {
            "schema": "cv-experiment-workflow.framework-idea.v1",
            "id": idea_id,
            "status": "active",
            "title": selected_title,
            "problem": selected_problem,
            "mechanism": selected_mechanism,
            "falsifiable_hypothesis": selected_hypothesis,
            "source_notes": normalized_sources,
            "parent_idea_ref": normalized_parent,
            "experiment_refs": [],
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        if not atomic_create_json(
            directory / "idea.json",
            record,
            transaction_id=uuid.uuid4().hex,
        ):
            raise FileExistsError(f"Idea 已存在，拒绝覆盖：{idea_id}")
        return record


def prepare_experiment_worktree(
    repository: Path,
    *,
    experiment_id: str,
) -> Path:
    root = _repository(repository)
    with project_write_lock(root):
        path, record = _find_experiment(_control(root), experiment_id)
        if record["worktree"] is not None:
            current = Path(record["worktree"])
            if current.is_dir():
                return current
            raise ValueError("实验记录中的 worktree 已丢失，拒绝猜测重建")
        worktree = root / ".worktrees" / experiment_id
        if os.path.lexists(worktree):
            raise FileExistsError(f"Worktree 目标已存在，拒绝覆盖：{worktree}")
        worktree.parent.mkdir(exist_ok=True)
        _git(root, "worktree", "add", str(worktree), record["branch"])
        updated = {**record, "worktree": str(worktree.resolve(strict=True))}
        atomic_write_json(path / "experiment.json", updated)
        return worktree


def register_gzsl_dataset(
    repository: Path,
    *,
    experiment_id: str,
    worktree: Path,
    source_path: Path,
    dataset_slug: str,
    dataset_id: str,
    version: str,
    source_uri: str,
    license_name: str,
) -> dict[str, Any]:
    """校验外置 NPZ；Git 只保存身份清单，不保存原始数据。"""
    root = _repository(repository)
    candidate_worktree = _real_directory(worktree, "实验 Worktree")
    _require_clean(candidate_worktree)
    slug = _slug(dataset_slug, "数据集短名")
    selected_id = _text(dataset_id, "数据集 ID")
    selected_version = _text(version, "数据集版本")
    selected_uri = _text(source_uri, "数据来源地址")
    selected_license = _text(license_name, "数据许可证")
    source = Path(source_path).expanduser().absolute()
    if (
        not os.path.lexists(source)
        or is_link_or_reparse(source)
        or not source.is_file()
        or source.suffix.lower() != ".npz"
    ):
        raise ValueError("GZSL 数据必须是普通的 .npz 文件")
    source_info = source.stat()
    if source_info.st_size <= 0:
        raise ValueError("GZSL 数据文件不能为空")

    with project_write_lock(root):
        experiment_directory, experiment = _find_experiment(
            _control(root),
            experiment_id,
        )
        if (
            experiment["worktree"] is None
            or Path(experiment["worktree"]).resolve(strict=True)
            != candidate_worktree
        ):
            raise ValueError("传入 Worktree 与实验记录不一致")
        if _git(candidate_worktree, "branch", "--show-current") != experiment["branch"]:
            raise ValueError("实验 Worktree 当前分支与账本不一致")
        if slug in experiment.get("dataset_refs", []):
            raise ValueError(f"本实验已经登记数据集：{slug}")

    manifest_relative = Path("data") / "manifests" / f"{slug}.json"
    manifest_path = candidate_worktree / manifest_relative
    source_sha256 = _sha256_regular_file(source, source_info)

    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "gzsl.dataset_manifest",
            "--data-path",
            str(source),
        ],
        cwd=candidate_worktree,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            "GZSL 数据结构校验失败："
            f"{completed.stderr[-1000:] or completed.stdout[-1000:]}"
        )
    try:
        content_manifest = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("GZSL 数据校验器没有返回 JSON") from error
    if (
        not isinstance(content_manifest, dict)
        or not isinstance(content_manifest.get("manifest_sha256"), str)
    ):
        raise ValueError("GZSL 数据校验结果缺少 manifest_sha256")
    source_sha256_after = _sha256_regular_file(source, source_info)
    if source_sha256_after != source_sha256:
        raise ValueError("校验过程中源数据发生变化，拒绝登记")

    dataset_record = {
        "schema": "cv-experiment-workflow.gzsl-dataset.v2",
        "slug": slug,
        "dataset_id": selected_id,
        "version": selected_version,
        "source_uri": selected_uri,
        "license": selected_license,
        "storage": "external_local_file",
        "source_sha256": source_sha256,
        "size_bytes": source_info.st_size,
        "content_manifest": content_manifest,
        "created_at": _utc_now(),
    }
    with project_write_lock(root):
        experiment_directory, experiment = _find_experiment(
            _control(root),
            experiment_id,
        )
        if (
            experiment["worktree"] is None
            or Path(experiment["worktree"]).resolve(strict=True)
            != candidate_worktree
            or _git(candidate_worktree, "branch", "--show-current")
            != experiment["branch"]
        ):
            raise ValueError("数据写入前 Worktree 身份已经变化")
        if slug in experiment.get("dataset_refs", []):
            raise ValueError(f"本实验已经登记数据集：{slug}")
        if os.path.lexists(manifest_path):
            raise FileExistsError(f"数据登记目标已经存在：{slug}")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        created = atomic_create_json(
            manifest_path,
            dataset_record,
            transaction_id=uuid.uuid4().hex,
        )
        if not created:
            raise FileExistsError(f"数据清单已经存在：{manifest_path}")
        committed = False
        try:
            _git(
                candidate_worktree,
                "add",
                "-f",
                "--",
                manifest_relative.as_posix(),
            )
            _git(
                candidate_worktree,
                "-c",
                "user.name=CV Workflow",
                "-c",
                "user.email=cv-workflow@localhost",
                "commit",
                "-m",
                f"data: register GZSL dataset {slug}",
            )
            committed = True
            _require_clean(candidate_worktree)
        except BaseException:
            if not committed:
                _run_git(
                    candidate_worktree,
                    "reset",
                    "--",
                    manifest_relative.as_posix(),
                    check=False,
                )
                if manifest_path.is_file() and not is_link_or_reparse(manifest_path):
                    manifest_path.unlink()
            raise
        updated = {
            **experiment,
            "dataset_refs": [*experiment.get("dataset_refs", []), slug],
        }
        atomic_write_json(experiment_directory / "experiment.json", updated)
        commit = _head(candidate_worktree)

    return {
        "dataset": dataset_record,
        "commit": commit,
        "config": {
            "mode": "local_gzsl_npz",
            "device": "cuda",
            "data_path": str(source),
            "dataset_id": selected_id,
            "version": selected_version,
            "source_uri": selected_uri,
            "manifest_sha256": content_manifest["manifest_sha256"],
        },
    }


def run_framework_experiment(
    repository: Path,
    *,
    experiment_id: str,
    worktree: Path,
    config: dict[str, Any],
    seed: int,
    purpose: str,
) -> dict[str, Any]:
    if purpose not in {"debug", "evidence"}:
        raise ValueError("purpose 只允许 debug 或 evidence")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed 必须是非负整数")
    if not isinstance(config, dict) or config.get("device") != "cuda":
        raise ValueError("GZSL Run 必须显式使用 device=cuda")
    root = _repository(repository)
    candidate_worktree = _real_directory(worktree, "实验 Worktree")
    _require_clean(candidate_worktree)

    with project_write_lock(root):
        control = _control(root)
        experiment_directory, experiment = _find_experiment(
            control,
            experiment_id,
        )
        if (
            experiment["worktree"] is None
            or Path(experiment["worktree"]).resolve(strict=True)
            != candidate_worktree
        ):
            raise ValueError("传入 Worktree 与实验记录不一致")
        if _git(candidate_worktree, "branch", "--show-current") != experiment["branch"]:
            raise ValueError("实验 Worktree 当前分支与账本不一致")
        commit = _head(candidate_worktree)
        codebase_id = experiment.get("kernel_codebase_id")
        existing_task_id = experiment.get("kernel_task_id")
        if existing_task_id is not None:
            if (
                experiment.get("frozen_seed") != seed
                or experiment.get("frozen_config") != config
            ):
                raise ValueError(
                    "同一实验的 debug/evidence 必须使用完全相同的配置和 seed；"
                    "如需改变参数，请新建实验"
                )

    if codebase_id is None:
        codebase = _register_compatibility_codebase(
            root,
            code_root=candidate_worktree,
            name=f"{experiment['id']}-worktree",
            commit=commit,
            branch=experiment["branch"],
            tag=None,
            source="domain_pack",
        )
        codebase_id = codebase["id"]
        with project_write_lock(root):
            experiment_directory, experiment = _find_experiment(
                _control(root),
                experiment_id,
            )
            experiment = {
                **experiment,
                "kernel_codebase_id": codebase_id,
            }
            atomic_write_json(
                experiment_directory / "experiment.json",
                experiment,
            )

    task_id = experiment.get("kernel_task_id")
    if task_id is None:
        from .tasking import create_task

        route_contract = experiment["route_contract"]
        task = create_task(
            root,
            owner_request=(
                f"{experiment['route']}：{experiment['title']}；"
                f"本地实验 {experiment['id']}"
            ),
            route={
                "reproduction": "reproduction",
                "tuning": "tune",
                "ablation": "ablation",
                "innovation": "innovation",
            }[experiment["route"]],
            target_refs=[experiment["id"]],
            route_inputs={
                "config": dict(config),
                "seed": seed,
                "debug_required": True,
                "changes_code_behavior": experiment["route"] == "innovation",
                "code_verified": True,
                "execution_authorized": True,
                "plan_unchanged": True,
                "primary_metric": route_contract.get("primary_metric", "H"),
                "baseline": route_contract.get("baseline", 0.0),
                **_framework_task_route_inputs(
                    experiment["route"],
                    route_contract,
                ),
                "framework_experiment": {
                    "id": experiment["id"],
                    "route": {
                        "reproduction": "reproduction",
                        "tuning": "tune",
                        "ablation": "ablation",
                        "innovation": "innovation",
                    }[experiment["route"]],
                    "framework_slug": experiment["parent_framework"],
                    "idea_ref": experiment["idea_ref"],
                },
                "code_binding": {
                    "codebase_id": codebase_id,
                    "branch": experiment["branch"],
                    "commit": commit,
                    "tag": None,
                },
            },
            budget={"max_runs": 2},
            stop_condition={"max_failures": 1},
        )
        task_id = task["id"]
        with project_write_lock(root):
            experiment_directory, experiment = _find_experiment(
                _control(root),
                experiment_id,
            )
            experiment = {
                **experiment,
                "kernel_task_id": task_id,
                "kernel_codebase_id": codebase_id,
                "frozen_config": dict(config),
                "frozen_seed": seed,
                "status": "running",
            }
            atomic_write_json(
                experiment_directory / "experiment.json",
                experiment,
            )

    from .engine import execute_task
    from .output_seal import seal_run_outputs
    from .runs import load_run

    result = execute_task(root, task_id, purpose=purpose)
    if result.get("status") == "artifact_seal_pending":
        pending_run_id = result["run"]["id"]
        seal_run_outputs(root, pending_run_id)
        result = execute_task(root, task_id, purpose=purpose)
    kernel_run = result.get("run")
    if not isinstance(kernel_run, dict) or not isinstance(
        kernel_run.get("id"),
        str,
    ):
        raise RuntimeError(f"Task 没有返回可登记的 Run：{result}")
    live_run = load_run(root, kernel_run["id"])
    evidence_level = result.get("evidence_level")
    if evidence_level not in {"debug", "single_run"}:
        raise RuntimeError(f"Run 没有形成预期证据等级：{result}")
    metrics = live_run.get("result", {}).get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        raise RuntimeError("Run 缺少结构化 metrics")
    actual_seed = live_run.get("frozen", {}).get("seed")
    if actual_seed != seed:
        raise RuntimeError(
            "中央 Run 的实际 seed 与本次请求不一致，拒绝登记错误证据"
        )

    with project_write_lock(root):
        experiment_directory, experiment = _find_experiment(
            _control(root),
            experiment_id,
        )
        runs_directory = experiment_directory / "runs"
        runs_directory.mkdir(exist_ok=True)
        local_run_id = _next_local_run_id(runs_directory, actual_seed)
        local_directory = runs_directory / local_run_id
        local_directory.mkdir()
        record = {
            "schema": "cv-experiment-workflow.framework-run.v1",
            "id": local_run_id,
            "experiment_id": experiment_id,
            "kernel_task_id": task_id,
            "kernel_run_id": live_run["id"],
            "purpose": purpose,
            "seed": actual_seed,
            "device": "cuda",
            "cpu_fallback": False,
            "code": {
                "branch": experiment["branch"],
                "commit": commit,
                "worktree": str(candidate_worktree),
            },
            "dataset": live_run["frozen"]["data"],
            "metrics": metrics,
            "evidence_level": evidence_level,
            "output_seal": live_run.get("output_seal"),
            "created_at": _utc_now(),
            "confirmation": None,
        }
        if not atomic_create_json(
            local_directory / "run.json",
            record,
            transaction_id=uuid.uuid4().hex,
        ):
            raise FileExistsError(f"本地 Run 已存在：{local_run_id}")
        return record


def confirm_framework_run(
    repository: Path,
    *,
    experiment_id: str,
    local_run_id: str,
    reason: str,
    proposed_by: str,
    checked_by: str,
    innovation_accepted: bool | None = None,
    innovation_conclusion: str | None = None,
) -> dict[str, Any]:
    root = _repository(repository)
    selected_reason = _text(reason, "确认原因")
    selected_proposer = _text(proposed_by, "提出者")
    selected_checker = _text(checked_by, "复核者")
    with project_write_lock(root):
        experiment_directory, experiment = _find_experiment(
            _control(root),
            experiment_id,
        )
        run_path = experiment_directory / "runs" / local_run_id / "run.json"
        record = read_bounded_json_object(run_path, label="Framework Run")
        if (
            record.get("experiment_id") != experiment_id
            or record.get("evidence_level") != "single_run"
        ):
            raise ValueError("只有 single_run 可以确认")
        innovation_decision: dict[str, Any] | None = None
        if experiment["route"] == "innovation":
            if not isinstance(innovation_accepted, bool):
                raise ValueError("创新实验必须明确选择“成功”或“未成功”")
            conclusion = _text(
                innovation_conclusion,
                "创新结论",
            )
            contract = experiment["route_contract"]
            primary_metric = contract["primary_metric"]
            baseline_evidence = _confirmed_baseline_run(
                _control(root),
                str(contract["baseline_run_ref"]),
                current_experiment_id=experiment_id,
                parent_framework=experiment["parent_framework"],
                primary_metric=primary_metric,
            )
            baseline_observed = baseline_evidence["metric_value"]
            if not math.isclose(
                float(contract["baseline"]),
                baseline_observed,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ValueError(
                    "创新合同填写的 baseline 与已确认基线 Run 的真实指标不一致"
                )
            observed = record.get("metrics", {}).get(primary_metric)
            observed_value = _finite_float(
                observed,
                f"创新结果 {primary_metric}",
            )
            required_value = (
                baseline_observed
                + float(contract["minimum_delta"])
            )
            meets_rule = observed_value >= required_value
            if innovation_accepted and not meets_rule:
                raise ValueError(
                    f"创新结果未达到成功门槛：{primary_metric}={observed_value}，"
                    f"至少需要 {required_value}"
                )
            innovation_decision = {
                "accepted": innovation_accepted,
                "conclusion": conclusion,
                "baseline_run_ref": contract["baseline_run_ref"],
                "baseline_evidence": baseline_evidence,
                "primary_metric": primary_metric,
                "baseline": baseline_observed,
                "minimum_delta": contract["minimum_delta"],
                "observed": observed_value,
                "required": required_value,
                "meets_rule": meets_rule,
                "local_run_id": local_run_id,
                "checked_by": selected_checker,
            }
        elif (
            innovation_accepted is not None
            or innovation_conclusion is not None
        ):
            raise ValueError("只有创新实验可以填写创新成功判定")
        kernel_run_id = record["kernel_run_id"]

    from .evidence import record_evidence_transition

    event = record_evidence_transition(
        root,
        kernel_run_id,
        "confirmed",
        evidence_refs=[kernel_run_id],
        reason=selected_reason,
        proposed_by=selected_proposer,
        checked_by=selected_checker,
        applied_by=selected_proposer,
    )
    with project_write_lock(root):
        experiment_directory, _experiment = _find_experiment(
            _control(root),
            experiment_id,
        )
        run_path = experiment_directory / "runs" / local_run_id / "run.json"
        current = read_bounded_json_object(run_path, label="Framework Run")
        updated = {
            **current,
            "evidence_level": "confirmed",
            "confirmation": {
                "event_id": event["event_id"],
                "reason": selected_reason,
                "proposed_by": selected_proposer,
                "checked_by": selected_checker,
                "confirmed_at": event["time"],
            },
            "innovation_decision": innovation_decision,
        }
        atomic_write_json(run_path, updated)
        atomic_write_json(
            experiment_directory / "experiment.json",
            {
                **_experiment,
                "status": "confirmed",
                "innovation_decision": innovation_decision,
            },
        )
        return updated


def export_framework_paper_package(
    repository: Path,
    *,
    experiment_id: str,
    local_run_id: str,
    research_brief: dict[str, Any],
    claim_statement_zh: str,
    confirmed_by: str,
    destination_root: Path | None = None,
) -> dict[str, Any]:
    """在用户主动交付时，把一条已确认 Run 导出成 PaperFlow 科研包。"""
    root = _repository(repository)
    statement = _text(claim_statement_zh, "结果事实")
    confirmer = _text(confirmed_by, "交付确认人")
    brief_fields = {
        "title",
        "research_area",
        "background",
        "problem",
        "objective",
        "research_questions",
        "hypotheses",
        "scope",
        "terminology",
        "planned_contributions",
    }
    if not isinstance(research_brief, dict) or set(research_brief) != brief_fields:
        raise ValueError(
            "Research Brief 必须完整填写标题、方向、背景、问题、目标、"
            "研究问题、假设、范围、术语和计划贡献"
        )

    with project_write_lock(root):
        experiment_directory, experiment = _find_experiment(
            _control(root),
            experiment_id,
        )
        local_path = (
            experiment_directory / "runs" / local_run_id / "run.json"
        )
        local_run = read_bounded_json_object(
            local_path,
            label="Framework Run",
        )
        if (
            local_run.get("experiment_id") != experiment_id
            or local_run.get("evidence_level") != "confirmed"
            or not isinstance(local_run.get("confirmation"), dict)
        ):
            raise ValueError("只有人工复核为 confirmed 的 Run 才能交给 PaperFlow")
        kernel_run_id = local_run.get("kernel_run_id")
        task_id = local_run.get("kernel_task_id")
        if not isinstance(kernel_run_id, str) or not isinstance(task_id, str):
            raise ValueError("Framework Run 缺少底层 Task/Run 引用")

    from .paper_package import (
        export_paper_package,
        save_research_brief,
        seal_paper_package,
        verify_paper_package,
    )
    from .runs import load_run
    from .tasking import load_task

    task = load_task(root, task_id)
    kernel_run = load_run(root, kernel_run_id)
    metrics = kernel_run.get("result", {}).get("metrics")
    raw_log = kernel_run.get("result", {}).get("raw_log")
    artifacts = kernel_run.get("artifacts")
    if (
        not isinstance(metrics, dict)
        or not metrics
        or not isinstance(raw_log, str)
        or not isinstance(artifacts, list)
        or not all(isinstance(item, str) for item in artifacts)
    ):
        raise ValueError("已确认 Run 缺少可交付的指标、日志或产物")

    brief_payload = {
        **research_brief,
        "user_confirmation": {
            "confirmed": True,
            "confirmed_at": _utc_now(),
            "confirmed_by": confirmer,
        },
    }
    metric_refs = [
        {"run_id": kernel_run_id, "metric_name": name}
        for name in sorted(metrics)
    ]
    metric_text = "，".join(
        f"{name}={repr(float(metrics[name]))}" for name in sorted(metrics)
    )
    result_statement = f"{statement} 已确认指标：{metric_text}。"
    asset_paths = [(raw_log, "raw_log")] + [
        (path, _paper_asset_role(path)) for path in artifacts
    ]
    assets = [
        {
            "asset_id": f"AST-{number:04d}",
            "source_object_ref": kernel_run_id,
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
    selection = {
        "schema": "cv-experiment-workflow.paper-package-selection.v1",
        "asset_mode": "hybrid",
        "paper_scope": {
            "title_hint": research_brief["title"],
            "research_area": research_brief["research_area"],
            "goal": research_brief["objective"],
            "included_claim_ids": ["CLM-0001"],
            "excluded_topics": list(research_brief["scope"]["excluded"]),
        },
        "claims": [
            {
                "claim_id": "CLM-0001",
                "kind": "result",
                "origin": "project",
                "statement_zh": result_statement,
                "statement_en": None,
                "maturity": "confirmed",
                "run_refs": [kernel_run_id],
                "metric_refs": metric_refs,
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
                "title": experiment["title"],
                "objective": experiment["summary"],
                "task_id": task["id"],
                "run_refs": [kernel_run_id],
                "claim_refs": ["CLM-0001"],
            }
        ],
        "source_uses": [],
        "assets": assets,
        "visuals": [],
        "supersedes_package_id": None,
    }

    brief_path = _temporary_json(root, "research-brief-", brief_payload)
    selection_path = _temporary_json(root, "paper-selection-", selection)
    try:
        saved_brief = save_research_brief(root, brief_path)
        sealed = seal_paper_package(
            root,
            str(saved_brief["brief_id"]),
            selection_path,
        )
    finally:
        brief_path.unlink(missing_ok=True)
        selection_path.unlink(missing_ok=True)

    delivery_root = (
        root / "deliveries" / "paperflow"
        if destination_root is None
        else Path(destination_root).expanduser().absolute()
    )
    delivery_root.mkdir(parents=True, exist_ok=True)
    package_path = delivery_root / str(sealed["package_id"])
    export_paper_package(
        root,
        str(sealed["package_id"]),
        "hybrid",
        package_path,
    )
    verification = verify_paper_package(package_path)
    if verification.get("status") != "pass":
        raise RuntimeError(f"PaperFlow 科研包校验失败：{verification}")
    return {
        "package_id": sealed["package_id"],
        "package_path": str(package_path.resolve(strict=True)),
        "readiness": sealed["readiness"],
        "verification": verification,
    }


def promote_innovation_framework(
    repository: Path,
    *,
    experiment_id: str,
    worktree: Path,
    child_slug: str,
    title: str,
    version: str,
    framework_view: dict[str, Any],
) -> dict[str, Any]:
    root = _repository(repository)
    selected_child = _slug(child_slug, "子 Framework slug")
    selected_title = _text(title, "子 Framework 标题")
    selected_version = _semver(version)
    view = _normalize_framework_view(framework_view)
    candidate_worktree = _real_directory(worktree, "创新 Worktree")

    with project_write_lock(root):
        control = _control(root)
        experiment_directory, experiment = _find_experiment(
            control,
            experiment_id,
        )
        if experiment["route"] != "innovation" or experiment["idea_ref"] is None:
            raise ValueError("只有绑定 Idea 的创新实验可以产生子 Framework")
        if experiment["status"] != "confirmed":
            raise ValueError("创新实验必须先完成 evidence Run 并人工确认，才能晋级")
        decision = experiment.get("innovation_decision")
        if (
            not isinstance(decision, dict)
            or decision.get("accepted") is not True
            or decision.get("meets_rule") is not True
            or not isinstance(decision.get("local_run_id"), str)
        ):
            raise ValueError("创新实验必须明确判定成功并达到预设门槛，才能晋级")
        if (
            experiment["worktree"] is None
            or Path(experiment["worktree"]).resolve(strict=True)
            != candidate_worktree
        ):
            raise ValueError("传入 Worktree 与实验记录不一致")
        if _git(candidate_worktree, "branch", "--show-current") != experiment["branch"]:
            raise ValueError("创新 Worktree 当前分支与实验记录不一致")
        _require_clean(candidate_worktree)
        commit = _head(candidate_worktree)
        confirmed_run_matches_commit = False
        runs_root = experiment_directory / "runs"
        if runs_root.is_dir():
            for run_path in sorted(runs_root.glob("*/run.json")):
                run = read_bounded_json_object(
                    run_path,
                    label="Framework Run",
                )
                code = run.get("code")
                if (
                    run.get("experiment_id") == experiment_id
                    and run.get("id") == decision["local_run_id"]
                    and run.get("evidence_level") == "confirmed"
                    and run.get("innovation_decision") == decision
                    and isinstance(code, dict)
                    and code.get("commit") == commit
                ):
                    confirmed_run_matches_commit = True
                    break
        if not confirmed_run_matches_commit:
            raise ValueError("当前提交没有对应的已确认 evidence Run，不能晋级")
        ancestor = _run_git(
            root,
            "merge-base",
            "--is-ancestor",
            experiment["parent_commit"],
            commit,
            check=False,
        )
        if ancestor.returncode != 0 or commit == experiment["parent_commit"]:
            raise ValueError("子 Framework 必须是父 Framework 之后的新提交")
        tag = f"framework/{selected_child}/v{selected_version}"
        _create_tag(root, tag, commit, f"Stable framework {selected_child}")
        child = _create_framework_locked(
            control,
            slug=selected_child,
            title=selected_title,
            commit=commit,
            tag=tag,
            version=selected_version,
            parent_framework=experiment["parent_framework"],
            idea_ref=experiment["idea_ref"],
            source_experiment=experiment["id"],
        )
        _write_framework_html(
            experiment_directory / "framework.html",
            parent_framework=experiment["parent_framework"],
            child_framework=child,
            idea_ref=experiment["idea_ref"],
            view=view,
        )
        updated = {
            **experiment,
            "child_framework": selected_child,
            "status": "promoted",
        }
        atomic_write_json(
            experiment_directory / "experiment.json",
            updated,
        )
        return child


def validate_framework_workspace(repository: Path) -> dict[str, Any]:
    """检查 GZSL 仓库、Framework、Idea、实验、Run 与 Git 引用是否一致。"""
    root = _repository(repository)
    control = _control(root)
    repository_record = read_bounded_json_object(
        control / "repository.json",
        label="Repository",
    )
    issues: list[str] = []
    counts = {
        "frameworks": 0,
        "ideas": 0,
        "experiments": 0,
        "runs": 0,
        "confirmed_runs": 0,
        "framework_html": 0,
    }

    ideas: dict[str, dict[str, Any]] = {}
    for idea_path in sorted((control / "idea-tree").glob("IDEA-*/idea.json")):
        try:
            idea = read_bounded_json_object(idea_path, label="Idea")
            idea_id = idea_path.parent.name
            if (
                idea.get("schema")
                != "cv-experiment-workflow.framework-idea.v1"
                or idea.get("id") != idea_id
                or IDEA_ID.fullmatch(idea_id) is None
            ):
                raise ValueError("身份字段无效")
            parent = idea.get("parent_idea_ref")
            if parent is not None and (
                not isinstance(parent, str)
                or IDEA_ID.fullmatch(parent) is None
            ):
                raise ValueError("父 Idea 无效")
            ideas[idea_id] = idea
            counts["ideas"] += 1
        except (OSError, ValueError) as error:
            issues.append(f"{idea_path}: {error}")
    for idea_id, idea in ideas.items():
        parent = idea.get("parent_idea_ref")
        if parent is not None and parent not in ideas:
            issues.append(f"{idea_id}: 父 Idea {parent} 不存在")

    framework_paths = sorted(
        (control / "frameworks").glob("*/framework.json")
    )
    framework_slugs = {path.parent.name for path in framework_paths}
    for framework_path in framework_paths:
        slug = framework_path.parent.name
        try:
            framework = _read_framework(control, slug)
            counts["frameworks"] += 1
            parent_framework = framework.get("parent_framework")
            if (
                parent_framework is not None
                and parent_framework not in framework_slugs
            ):
                issues.append(
                    f"{slug}: 父 Framework {parent_framework} 不存在"
                )
            idea_ref = framework.get("idea_ref")
            if idea_ref is not None and idea_ref not in ideas:
                issues.append(f"{slug}: Idea {idea_ref} 不存在")
            tag_commit = _run_git(
                root,
                "rev-list",
                "-n",
                "1",
                framework["tag"],
                check=False,
            )
            if (
                tag_commit.returncode != 0
                or tag_commit.stdout.strip() != framework["commit"]
            ):
                issues.append(f"{slug}: 稳定 Tag 与 Framework commit 不一致")

            experiments_root = framework_path.parent / "experiments"
            expected_routes = set(ROUTES)
            observed_routes = {
                path.name
                for path in experiments_root.iterdir()
                if path.is_dir() and not is_link_or_reparse(path)
            }
            if observed_routes != expected_routes:
                issues.append(
                    f"{slug}: 四类实验目录不完整，当前为 {sorted(observed_routes)}"
                )
            for route in ROUTES:
                route_root = experiments_root / route
                if not route_root.is_dir() or is_link_or_reparse(route_root):
                    continue
                prefix = ROUTE_PREFIXES[route]
                pattern = re.compile(
                    rf"{re.escape(prefix)}-[0-9]{{3}}-[a-z0-9-]+"
                )
                for experiment_path in sorted(
                    route_root.glob("*/experiment.json")
                ):
                    counts["experiments"] += 1
                    experiment_id = experiment_path.parent.name
                    try:
                        experiment = read_bounded_json_object(
                            experiment_path,
                            label="Framework experiment",
                        )
                        if (
                            experiment.get("schema") != EXPERIMENT_SCHEMA
                            or experiment.get("id") != experiment_id
                            or pattern.fullmatch(experiment_id) is None
                            or experiment.get("route") != route
                            or experiment.get("parent_framework") != slug
                            or experiment.get("parent_commit")
                            != framework["commit"]
                        ):
                            raise ValueError("身份、类型或父 Framework 无效")
                        idea_ref = experiment.get("idea_ref")
                        if route == "innovation":
                            if idea_ref not in ideas:
                                raise ValueError("创新实验缺少有效 Idea")
                        elif idea_ref is not None:
                            raise ValueError("非创新实验不能绑定 Idea")
                        branch = experiment.get("branch")
                        branch_ref = _run_git(
                            root,
                            "show-ref",
                            "--verify",
                            "--quiet",
                            f"refs/heads/{branch}",
                            check=False,
                        )
                        if branch_ref.returncode != 0:
                            issues.append(
                                f"{experiment_id}: Git 分支不存在"
                            )
                        html_path = experiment_path.parent / "framework.html"
                        should_have_html = (
                            route == "innovation"
                            and experiment.get("status") == "promoted"
                            and experiment.get("child_framework") is not None
                        )
                        if html_path.is_file():
                            counts["framework_html"] += 1
                        if html_path.is_file() != should_have_html:
                            issues.append(
                                f"{experiment_id}: framework.html 与创新晋级状态不一致"
                            )
                        runs_root = experiment_path.parent / "runs"
                        if runs_root.exists():
                            for run_path in sorted(
                                runs_root.glob("run-*/run.json")
                            ):
                                run = read_bounded_json_object(
                                    run_path,
                                    label="Framework Run",
                                )
                                counts["runs"] += 1
                                if (
                                    run.get("schema")
                                    != "cv-experiment-workflow.framework-run.v1"
                                    or run.get("experiment_id")
                                    != experiment_id
                                    or run.get("device") != "cuda"
                                    or run.get("cpu_fallback") is not False
                                ):
                                    issues.append(
                                        f"{run_path.parent.name}: Run 身份或 GPU 规则无效"
                                    )
                                if run.get("evidence_level") == "confirmed":
                                    counts["confirmed_runs"] += 1
                                    if not isinstance(
                                        run.get("confirmation"),
                                        dict,
                                    ):
                                        issues.append(
                                            f"{run_path.parent.name}: 已确认 Run 缺少确认记录"
                                        )
                    except (OSError, ValueError) as error:
                        issues.append(f"{experiment_id}: {error}")
        except (OSError, ValueError) as error:
            issues.append(f"{slug}: {error}")

    if repository_record.get("active_framework") not in framework_slugs:
        issues.append("repository.json 的 active_framework 不存在")
    if issues:
        raise ValueError("GZSL 仓库完整性检查失败：" + "；".join(issues))
    return {
        "schema": "cv-experiment-workflow.framework-validation.v1",
        "status": "pass",
        "repository": repository_record["name"],
        "execution_policy": "gpu_only",
        "counts": counts,
        "checked_at": _utc_now(),
    }


def _create_framework_locked(
    control: Path,
    *,
    slug: str,
    title: str,
    commit: str,
    tag: str,
    version: str,
    parent_framework: str | None,
    idea_ref: str | None,
    source_experiment: str | None,
) -> dict[str, Any]:
    directory = control / "frameworks" / slug
    if os.path.lexists(directory):
        raise FileExistsError(f"Framework 已存在，拒绝覆盖：{slug}")
    directory.mkdir(parents=True)
    experiments = directory / "experiments"
    experiments.mkdir()
    for route in ROUTES:
        (experiments / route).mkdir()
    record = {
        "schema": FRAMEWORK_SCHEMA,
        "slug": slug,
        "title": title,
        "direction": "gzsl",
        "version": version,
        "commit": commit,
        "tag": tag,
        "status": "stable",
        "parent_framework": parent_framework,
        "idea_ref": idea_ref,
        "source_experiment": source_experiment,
        "created_at": _utc_now(),
    }
    if not atomic_create_json(
        directory / "framework.json",
        record,
        transaction_id=uuid.uuid4().hex,
    ):
        raise FileExistsError(f"Framework 记录已存在，拒绝覆盖：{slug}")
    return record


def _write_framework_html(
    path: Path,
    *,
    parent_framework: str,
    child_framework: dict[str, Any],
    idea_ref: str,
    view: dict[str, list[dict[str, str]]],
) -> None:
    if os.path.lexists(path):
        raise FileExistsError(f"框架图已存在，拒绝覆盖：{path}")
    data = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    title = html.escape(child_framework["title"])
    parent = html.escape(parent_framework)
    child = html.escape(child_framework["slug"])
    idea = html.escape(idea_ref)
    content = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 创新框架</title>
<style>
:root {{ color-scheme: dark; font-family: "Microsoft YaHei", sans-serif; }}
body {{ margin:0; background:#08111f; color:#edf4ff; }}
main {{ max-width:1180px; margin:auto; padding:52px 28px 72px; }}
.eyebrow {{ color:#82d9c8; letter-spacing:.14em; font-size:13px; }}
h1 {{ font-size:clamp(30px,5vw,54px); margin:12px 0 10px; }}
.meta {{ color:#a7b8cf; margin-bottom:38px; }}
#canvas {{ position:relative; min-height:360px; border:1px solid #233751;
  border-radius:24px; background:linear-gradient(145deg,#0d1b2d,#091522);
  overflow:hidden; box-shadow:0 24px 70px #0007; }}
svg {{ position:absolute; inset:0; width:100%; height:100%; }}
.node {{ position:absolute; width:190px; min-height:74px; display:grid;
  place-items:center; padding:12px; border-radius:18px; text-align:center;
  background:#142940; border:1px solid #3f6d88; box-shadow:0 12px 28px #0008; }}
.node.innovation {{ border-color:#7ce3bd; background:#123b3c; }}
.legend {{ display:flex; gap:18px; color:#9fb0c7; margin-top:20px; font-size:14px; }}
</style>
</head>
<body>
<main>
  <div class="eyebrow">IDEA-DRIVEN FRAMEWORK</div>
  <h1>{title}</h1>
  <div class="meta">父框架：{parent}　→　子框架：{child}　·　Idea：{idea}</div>
  <section id="canvas" aria-label="创新后的代码框架"></section>
  <div class="legend">本页只描述这次创新后的完整框架；复现、调参和消融不会生成新图。</div>
</main>
<script>
const graph={data};
const canvas=document.querySelector('#canvas');
const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
svg.innerHTML='<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#6fa6c6"/></marker></defs>';
canvas.appendChild(svg);
const positions=new Map();
graph.nodes.forEach((node,index)=>{{
  const x=8+index*(84/Math.max(1,graph.nodes.length-1));
  const y=index%2===0?32:58;
  positions.set(node.id,{{x,y}});
  const card=document.createElement('div');
  card.className='node'+(node.id.toLowerCase().includes('align')?' innovation':'');
  card.textContent=node.label;
  card.style.left=`calc(${{x}}% - 95px)`;
  card.style.top=`calc(${{y}}% - 49px)`;
  canvas.appendChild(card);
}});
graph.edges.forEach(edge=>{{
  const a=positions.get(edge.from),b=positions.get(edge.to);
  const line=document.createElementNS('http://www.w3.org/2000/svg','line');
  line.setAttribute('x1',a.x+'%'); line.setAttribute('y1',a.y+'%');
  line.setAttribute('x2',b.x+'%'); line.setAttribute('y2',b.y+'%');
  line.setAttribute('stroke','#6fa6c6'); line.setAttribute('stroke-width','2');
  line.setAttribute('marker-end','url(#arrow)'); svg.appendChild(line);
}});
</script>
</body>
</html>
"""
    path.write_text(content, encoding="utf-8")


def _normalize_framework_view(
    value: dict[str, Any],
) -> dict[str, list[dict[str, str]]]:
    if not isinstance(value, dict) or set(value) != {"nodes", "edges"}:
        raise ValueError("framework_view 必须只包含 nodes 和 edges")
    nodes = value["nodes"]
    edges = value["edges"]
    if (
        not isinstance(nodes, list)
        or not 2 <= len(nodes) <= 30
        or not isinstance(edges, list)
        or not 1 <= len(edges) <= 60
    ):
        raise ValueError("framework_view 节点或边数量无效")
    normalized_nodes: list[dict[str, str]] = []
    node_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"id", "label"}:
            raise ValueError("framework_view node 字段无效")
        node_id = _slug(node["id"], "节点 id")
        if node_id in node_ids:
            raise ValueError("framework_view node id 重复")
        node_ids.add(node_id)
        normalized_nodes.append(
            {"id": node_id, "label": _text(node["label"], "节点名称")}
        )
    normalized_edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) != {"from", "to"}:
            raise ValueError("framework_view edge 字段无效")
        source = _slug(edge["from"], "边起点")
        target = _slug(edge["to"], "边终点")
        identity = (source, target)
        if source not in node_ids or target not in node_ids or source == target:
            raise ValueError("framework_view edge 引用了无效节点")
        if identity in seen_edges:
            raise ValueError("framework_view edge 重复")
        seen_edges.add(identity)
        normalized_edges.append({"from": source, "to": target})
    return {"nodes": normalized_nodes, "edges": normalized_edges}


def _normalize_component_map(
    source: Path,
    value: dict[str, Any],
) -> dict[str, list[str]]:
    if not isinstance(value, dict) or set(value) != set(GZSL_COMPONENTS):
        raise ValueError(
            "component_map 必须完整说明 data、model、losses、trainer、"
            "evaluator、inferencer、metrics 和 configs"
        )
    result: dict[str, list[str]] = {}
    for component in GZSL_COMPONENTS:
        paths = value[component]
        if (
            not isinstance(paths, list)
            or not paths
            or len(paths) > 64
            or not all(isinstance(item, str) for item in paths)
        ):
            raise ValueError(f"component_map.{component} 必须是非空文件列表")
        normalized: list[str] = []
        for item in paths:
            relative = Path(item)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative.as_posix() != item.replace("\\", "/")
            ):
                raise ValueError(f"component_map 路径无效：{item}")
            candidate = source / relative
            if (
                not candidate.is_file()
                or is_link_or_reparse(candidate)
                or source not in candidate.resolve(strict=True).parents
            ):
                raise ValueError(f"component_map 文件不存在或不安全：{item}")
            normalized.append(relative.as_posix())
        result[component] = sorted(set(normalized))
    return result


def _external_execution_environment() -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in (
            "SystemRoot",
            "WINDIR",
            "PATH",
            "PATHEXT",
            "TEMP",
            "TMP",
            "USERNAME",
            "USERPROFILE",
            "LOCALAPPDATA",
            "APPDATA",
            "CUDA_PATH",
            "CUDA_VISIBLE_DEVICES",
        )
        if key in os.environ
    }
    environment.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "WANDB_MODE": "offline",
        }
    )
    return environment


def _replay_equivalence_command(
    source: Path,
    *,
    result_relative: Path,
    artifact_relative: Path,
    command: list[str],
    expected_payload: dict[str, Any],
) -> dict[str, Any]:
    executable = Path(command[0])
    if (
        executable.name.lower() not in {"python", "python.exe"}
        and executable.resolve() != Path(sys.executable).resolve()
    ):
        raise ValueError("等价验证命令必须使用当前受管 Python")
    for argument in command[1:]:
        if "\x00" in argument or (
            not argument.startswith("-") and Path(argument).is_absolute()
        ):
            raise ValueError("等价验证命令除 Python 外只能使用仓库内相对参数")

    with tempfile.TemporaryDirectory(prefix="cvwf-equivalence-replay-") as temporary:
        replay = Path(temporary) / "repository"
        clone = subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-hardlinks",
                "--no-local",
                str(source),
                str(replay),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
        if clone.returncode != 0:
            raise RuntimeError(
                "无法建立等价验证重放快照："
                f"{clone.stderr[-1000:] or clone.stdout[-1000:]}"
            )
        replay = replay.resolve(strict=True)
        result_path = replay / result_relative
        artifact_path = replay / artifact_relative
        for target in (result_path, artifact_path):
            if target.exists():
                if (
                    not target.is_file()
                    or is_link_or_reparse(target)
                    or replay not in target.resolve(strict=True).parents
                ):
                    raise ValueError("等价验证重放目标不是安全的仓库内普通文件")
                target.unlink()
        completed = subprocess.run(
            [sys.executable, *command[1:]],
            cwd=replay,
            env=_external_execution_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "等价验证命令重放失败："
                f"{completed.stderr[-1000:] or completed.stdout[-1000:]}"
            )
        if (
            not result_path.is_file()
            or not artifact_path.is_file()
            or is_link_or_reparse(result_path)
            or is_link_or_reparse(artifact_path)
        ):
            raise ValueError("等价验证命令没有重新生成结果 JSON 和输出产物")
        replay_payload = read_bounded_json_object(
            result_path,
            label="replayed equivalence result",
        )
        if replay_payload != expected_payload:
            raise ValueError("等价验证命令重放结果与提交的结果 JSON 不一致")
        artifact_info = artifact_path.stat()
        replay_sha256 = _sha256_regular_file(artifact_path, artifact_info)
        if replay_sha256 != expected_payload["output_sha256"]:
            raise ValueError("等价验证命令重放产物哈希不一致")
        return {
            "status": "pass",
            "returncode": completed.returncode,
            "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
            "output_sha256": replay_sha256,
        }


def _normalize_equivalence(
    source: Path,
    value: dict[str, Any],
) -> dict[str, Any]:
    source = source.resolve(strict=True)
    expected = {
        "dataset_id",
        "tolerance",
        "original_result_path",
        "standardized_result_path",
        "checked_by",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("equivalence 字段不完整")
    dataset_id = _text(value["dataset_id"], "等价验证数据集")
    tolerance = value["tolerance"]
    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not 0 <= float(tolerance) <= 0.01
    ):
        raise ValueError("equivalence.tolerance 必须在 0 到 0.01 之间")
    checked_by = value["checked_by"]
    if (
        not isinstance(checked_by, list)
        or len(checked_by) < 2
        or len(checked_by) > 8
    ):
        raise ValueError("equivalence.checked_by 至少需要两名不同复核者")
    reviewers = [_text(item, "等价验证复核者") for item in checked_by]
    if len(set(reviewers)) != len(reviewers):
        raise ValueError("equivalence.checked_by 复核者不能重复")
    evidence: dict[str, dict[str, Any]] = {}
    metrics: dict[str, dict[str, float]] = {}
    dataset_manifest_sha256: str | None = None
    for label, metrics_name in (
        ("original_result_path", "original_metrics"),
        ("standardized_result_path", "standardized_metrics"),
    ):
        relative = Path(value[label])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"equivalence.{label} 必须是仓库内相对路径")
        result_path = source / relative
        if (
            not result_path.is_file()
            or is_link_or_reparse(result_path)
            or source not in result_path.resolve(strict=True).parents
            or result_path.stat().st_size > 1024 * 1024
        ):
            raise ValueError(f"equivalence.{label} 文件不存在、不安全或超过 1 MiB")
        payload = read_bounded_json_object(
            result_path,
            label=f"equivalence {label}",
        )
        result_fields = {
            "schema",
            "dataset_id",
            "dataset_manifest_sha256",
            "metrics",
            "command",
            "code_commit",
            "output_artifact",
            "output_sha256",
        }
        if (
            set(payload) != result_fields
            or payload.get("schema")
            != "cv-experiment-workflow.equivalence-result.v1"
            or payload.get("dataset_id") != dataset_id
            or COMMIT.fullmatch(str(payload.get("code_commit"))) is None
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(payload.get("dataset_manifest_sha256")),
            )
            is None
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(payload.get("output_sha256")),
            )
            is None
        ):
            raise ValueError(f"equivalence.{label} 缺少可信运行身份")
        command = payload["command"]
        if (
            not isinstance(command, list)
            or not command
            or len(command) > 64
        ):
            raise ValueError(f"equivalence.{label}.command 无效")
        normalized_command = [
            _text(item, f"equivalence.{label}.command") for item in command
        ]
        observed = payload["metrics"]
        if not isinstance(observed, dict) or set(observed) != {"S", "U", "H"}:
            raise ValueError(f"equivalence.{label}.metrics 必须包含 S/U/H")
        converted: dict[str, float] = {}
        for metric, score in observed.items():
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not 0 <= float(score) <= 1
            ):
                raise ValueError(f"equivalence.{label}.metrics.{metric} 无效")
            converted[metric] = float(score)
        metrics[metrics_name] = converted
        current_manifest = str(payload["dataset_manifest_sha256"])
        if dataset_manifest_sha256 is None:
            dataset_manifest_sha256 = current_manifest
        elif dataset_manifest_sha256 != current_manifest:
            raise ValueError("标准化前后必须使用同一份数据清单")
        artifact_relative = Path(payload["output_artifact"])
        if artifact_relative.is_absolute() or ".." in artifact_relative.parts:
            raise ValueError(f"equivalence.{label}.output_artifact 路径无效")
        artifact = source / artifact_relative
        if (
            not artifact.is_file()
            or is_link_or_reparse(artifact)
            or source not in artifact.resolve(strict=True).parents
        ):
            raise ValueError(f"equivalence.{label} 输出产物不存在或不安全")
        artifact_info = artifact.stat()
        actual_output_sha256 = _sha256_regular_file(
            artifact,
            artifact_info,
        )
        if actual_output_sha256 != payload["output_sha256"]:
            raise ValueError(f"equivalence.{label} 输出产物哈希不匹配")
        if label == "standardized_result_path":
            ancestor = _run_git(
                source,
                "merge-base",
                "--is-ancestor",
                payload["code_commit"],
                "HEAD",
                check=False,
            )
            if ancestor.returncode != 0:
                raise ValueError("标准化结果必须绑定当前草稿历史中的代码 commit")
        replay = _replay_equivalence_command(
            source,
            result_relative=relative,
            artifact_relative=artifact_relative,
            command=normalized_command,
            expected_payload=payload,
        )
        evidence[label] = {
            "path": relative.as_posix(),
            "sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
            "command": normalized_command,
            "code_commit": payload["code_commit"],
            "dataset_manifest_sha256": current_manifest,
            "output_artifact": artifact_relative.as_posix(),
            "output_sha256": actual_output_sha256,
            "replay": replay,
        }
    for metric in ("S", "U", "H"):
        difference = abs(
            metrics["original_metrics"][metric]
            - metrics["standardized_metrics"][metric]
        )
        if difference > float(tolerance):
            raise ValueError(
                f"标准化前后 {metric} 相差 {difference:.8f}，超过允许误差"
            )
    return {
        "status": "pass",
        "dataset_id": dataset_id,
        "dataset_manifest_sha256": dataset_manifest_sha256,
        "tolerance": float(tolerance),
        **metrics,
        "evidence": evidence,
        "checked_by": reviewers,
    }


def _require_standardized_gzsl_files(source: Path) -> None:
    missing = [
        relative
        for relative in GZSL_STANDARD_FILES
        if not (source / Path(relative)).is_file()
        or is_link_or_reparse(source / Path(relative))
    ]
    if missing:
        raise ValueError(
            "外来代码尚未完成 GZSL 标准接口拆分，缺少："
            + "、".join(missing)
        )


def _require_canonical_standard_verifiers(source: Path) -> Path:
    payload = resolve_domain_pack("gzsl@1.1.1") / "payload"
    for relative in (
        "gzsl/cuda_probe.py",
        "tests/domain_pack/test_metric_contract.py",
        "tests/domain_pack/test_synthetic_smoke.py",
    ):
        candidate = source / relative
        canonical = payload / relative
        if candidate.read_bytes() != canonical.read_bytes():
            raise ValueError(
                "外来 Framework 的标准测试和 CUDA 探针必须保持通用模板原文："
                f"{relative}"
            )
    return payload


def _verify_standardized_gzsl_source(
    source: Path,
    *,
    trusted_execution_acknowledged: bool,
) -> dict[str, Any]:
    if trusted_execution_acknowledged is not True:
        raise ValueError("没有人工确认信任时，禁止执行外来代码")
    canonical_payload = _require_canonical_standard_verifiers(source)
    commands = (
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests/domain_pack",
            "-p",
            "test_*.py",
        ],
        [
            sys.executable,
            "-I",
            "-B",
            "-X",
            "utf8",
            str(canonical_payload / "gzsl" / "cuda_probe.py"),
        ],
    )
    results: list[dict[str, Any]] = []
    raw_outputs: list[tuple[str, str]] = []
    environment = _external_execution_environment()
    for index, command in enumerate(commands):
        completed = subprocess.run(
            command,
            cwd=source if index == 0 else canonical_payload,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            check=False,
        )
        result = {
            "command": (
                command[1:]
                if index == 0
                else ["trusted-pack", "gzsl/cuda_probe.py"]
            ),
            "returncode": completed.returncode,
        }
        raw_outputs.append((completed.stdout, completed.stderr))
        results.append(result)
        if completed.returncode != 0:
            raise RuntimeError(
                "外来 GZSL 标准化验证失败："
                f"{result}；"
                f"{completed.stderr[-1000:] or completed.stdout[-1000:]}"
            )
    test_output = raw_outputs[0][0] + raw_outputs[0][1]
    match = re.search(r"Ran ([0-9]+) tests?", test_output)
    if match is None or int(match.group(1)) < 2 or "OK" not in test_output:
        raise RuntimeError("外来 Framework 至少需要 2 个真实通过的标准测试")
    try:
        cuda_report = json.loads(raw_outputs[1][0].strip())
    except json.JSONDecodeError as error:
        raise RuntimeError("CUDA 探针必须输出 JSON 设备报告") from error
    if (
        not isinstance(cuda_report, dict)
        or cuda_report.get("status") != "pass"
        or cuda_report.get("cuda") is not True
        or not isinstance(cuda_report.get("device"), str)
        or not cuda_report["device"].strip()
    ):
        raise RuntimeError("CUDA 探针没有证明真实 GPU 可用")
    return {
        "status": "pass",
        "trusted_execution_acknowledged": True,
        "test_count": int(match.group(1)),
        "cuda_report": cuda_report,
        "commands": results,
    }


def _replace_worktree_with_standardized_source(
    worktree: Path,
    source: Path,
) -> None:
    _require_clean(worktree)
    management_files = ("AGENTS.md", "SKILL.md", "WORKFLOW.md", ".gitignore")
    preserved = {
        name: (worktree / name).read_bytes()
        for name in management_files
        if (worktree / name).is_file()
        and not is_link_or_reparse(worktree / name)
    }
    _git(worktree, "rm", "-r", "--ignore-unmatch", "--", ".")
    excluded = {
        ".git",
        ".experiment-workflow",
        ".worktrees",
        "deliveries",
        "__pycache__",
    }
    files: list[tuple[Path, Path]] = []
    total = 0
    for candidate in source.rglob("*"):
        relative = candidate.relative_to(source)
        if any(part in excluded for part in relative.parts):
            continue
        if relative.as_posix() in preserved:
            continue
        if is_link_or_reparse(candidate):
            raise ValueError(f"外来仓库含链接或重解析点：{relative}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ValueError(f"外来仓库含未知文件类型：{relative}")
        size = candidate.stat().st_size
        total += size
        if total > MAX_IMPORTED_FRAMEWORK_BYTES:
            raise ValueError(
                "外来框架代码超过 64 MiB；大型权重和数据只能登记下载地址"
            )
        files.append((candidate, relative))
    if not files:
        raise ValueError("外来仓库没有可导入文件")
    for candidate, relative in files:
        target = worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, target)
    for name, content in preserved.items():
        (worktree / name).write_bytes(content)


def _active_idea(control: Path, idea_ref: str) -> dict[str, Any]:
    if not isinstance(idea_ref, str) or IDEA_ID.fullmatch(idea_ref) is None:
        raise ValueError("Idea ID 无效")
    path = control / "idea-tree" / idea_ref / "idea.json"
    payload = read_bounded_json_object(path, label="Idea")
    if (
        payload.get("schema")
        != "cv-experiment-workflow.framework-idea.v1"
        or payload.get("id") != idea_ref
        or payload.get("status") != "active"
        or not isinstance(payload.get("experiment_refs"), list)
    ):
        raise ValueError("创新实验只能绑定 active/ready Idea")
    return payload


def _normalize_source_notes(
    value: object,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value or len(value) > 32:
        raise ValueError("Idea source_notes 必须包含 1..32 条来源说明")
    result: list[dict[str, str]] = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"label", "locator", "claim"}
        ):
            raise ValueError("Idea source_note 字段无效")
        result.append(
            {
                "label": _text(item["label"], "Idea 来源标签"),
                "locator": _text(item["locator"], "Idea 来源位置"),
                "claim": _text(item["claim"], "Idea 来源说明"),
            }
        )
    return result


def _next_experiment_id(
    control: Path,
    route: str,
    slug: str,
) -> str:
    prefix = ROUTE_PREFIXES[route]
    pattern = re.compile(rf"{re.escape(prefix)}-([0-9]{{3}})-[a-z0-9-]+")
    numbers: list[int] = []
    route_directories = sorted(
        (control / "frameworks").glob(f"*/experiments/{route}")
    )
    for route_directory in route_directories:
        if not route_directory.is_dir() or is_link_or_reparse(route_directory):
            raise ValueError(f"实验路线目录不安全：{route_directory}")
        for entry in route_directory.iterdir():
            match = pattern.fullmatch(entry.name)
            if match is None or not entry.is_dir() or is_link_or_reparse(entry):
                raise ValueError(f"实验目录存在未知条目：{entry}")
            numbers.append(int(match.group(1)))
    number = max(numbers, default=0) + 1
    if number > 999:
        raise ValueError("当前仓库该类实验编号已达到 999")
    return f"{prefix}-{number:03d}-{slug}"


def _next_local_run_id(runs_directory: Path, seed: int) -> str:
    numbers: list[int] = []
    for entry in runs_directory.iterdir():
        match = re.fullmatch(r"run-([0-9]{3})-seed-[0-9]+", entry.name)
        if match is None or not entry.is_dir() or is_link_or_reparse(entry):
            raise ValueError(f"Run 目录存在未知条目：{entry}")
        numbers.append(int(match.group(1)))
    number = max(numbers, default=0) + 1
    if number > 999:
        raise ValueError("当前实验的本地 Run 已达到 999")
    return f"run-{number:03d}-seed-{seed}"


def _register_compatibility_codebase(
    project: Path,
    *,
    code_root: Path,
    name: str,
    commit: str,
    branch: str,
    tag: str | None,
    source: str,
) -> dict[str, Any]:
    manifest = {
        "schema": CODEBASE_SCHEMA,
        "name": name,
        "primary_direction": "gzsl",
        "repo_path": str(Path(code_root).resolve(strict=True)),
        "source": source,
        "template_id": "PACK-GZSL",
        "template_version": "1.1.1",
        "default_branch": branch,
        "initial_commit": commit,
        "initial_tag": tag,
    }
    manifest_parent = Path(project).resolve(strict=True).parent
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        prefix="codebase-",
        dir=manifest_parent,
        delete=False,
    )
    path = Path(handle.name)
    try:
        with handle:
            json.dump(manifest, handle, ensure_ascii=False)
        return register_codebase(project, path, code_root)
    finally:
        path.unlink(missing_ok=True)


def _temporary_json(
    repository: Path,
    prefix: str,
    payload: dict[str, Any],
) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        prefix=prefix,
        dir=repository.parent,
        delete=False,
    )
    path = Path(handle.name)
    with handle:
        json.dump(payload, handle, ensure_ascii=False)
    return path


def _paper_asset_role(path: str) -> str:
    return {
        "checkpoint.pt": "model_checkpoint",
        "evaluation.json": "result_data",
        "predictions.json": "predictions",
        "run.json": "run_metadata",
        "dataset_manifest.json": "dataset_manifest",
        "artifacts.json": "artifact_manifest",
    }.get(Path(path).name, "result_artifact")


def _find_experiment(
    control: Path,
    experiment_id: str,
) -> tuple[Path, dict[str, Any]]:
    if not isinstance(experiment_id, str):
        raise ValueError("实验 ID 无效")
    matches = list(
        (control / "frameworks").glob(
            f"*/experiments/*/{experiment_id}/experiment.json"
        )
    )
    if len(matches) != 1:
        raise ValueError(f"实验 ID 不存在或不唯一：{experiment_id}")
    payload = read_bounded_json_object(matches[0], label="Framework experiment")
    if payload.get("schema") != EXPERIMENT_SCHEMA or payload.get("id") != experiment_id:
        raise ValueError("实验记录身份无效")
    return matches[0].parent, payload


def _confirmed_baseline_run(
    control: Path,
    reference: str,
    *,
    current_experiment_id: str,
    parent_framework: str,
    primary_metric: str,
) -> dict[str, Any]:
    parts = reference.split("/", 1)
    if len(parts) != 2 or not all(part.strip() for part in parts):
        raise ValueError(
            "创新 baseline_run_ref 必须使用“实验编号/Run 编号”格式"
        )
    baseline_experiment_id, local_run_id = parts
    if baseline_experiment_id == current_experiment_id:
        raise ValueError("创新基线必须来自当前创新实验之外的已确认 Run")
    experiment_directory, experiment = _find_experiment(
        control,
        baseline_experiment_id,
    )
    if experiment.get("parent_framework") != parent_framework:
        raise ValueError("创新基线 Run 必须基于同一个父 Framework")
    run_path = experiment_directory / "runs" / local_run_id / "run.json"
    if not run_path.is_file() or is_link_or_reparse(run_path):
        raise ValueError("创新 baseline_run_ref 指向的 Run 不存在")
    run = read_bounded_json_object(run_path, label="innovation baseline Run")
    if (
        run.get("experiment_id") != baseline_experiment_id
        or run.get("id") != local_run_id
        or run.get("evidence_level") != "confirmed"
    ):
        raise ValueError("创新基线必须是另一个实验中已人工确认的 evidence Run")
    metric_value = _finite_float(
        run.get("metrics", {}).get(primary_metric),
        f"创新基线指标 {primary_metric}",
    )
    return {
        "experiment_id": baseline_experiment_id,
        "local_run_id": local_run_id,
        "kernel_run_id": run.get("kernel_run_id"),
        "primary_metric": primary_metric,
        "metric_value": metric_value,
        "code_commit": run.get("code", {}).get("commit"),
    }


def _read_framework(control: Path, slug: str) -> dict[str, Any]:
    payload = read_bounded_json_object(
        control / "frameworks" / slug / "framework.json",
        label="Framework",
    )
    if (
        payload.get("schema") != FRAMEWORK_SCHEMA
        or payload.get("slug") != slug
        or payload.get("status") != "stable"
        or COMMIT.fullmatch(str(payload.get("commit"))) is None
    ):
        raise ValueError(f"Framework 记录无效：{slug}")
    return payload


def _route_directory(control: Path, framework: str, route: str) -> Path:
    path = control / "frameworks" / framework / "experiments" / route
    return _real_directory(path, f"{route} 实验目录")


def _control(repository: Path) -> Path:
    return _real_directory(
        Path(repository) / ".experiment-workflow",
        "实验工作流控制目录",
    )


def _repository(path: Path) -> Path:
    root = _real_directory(path, "GZSL 仓库")
    if not (root / ".git").exists() or not (root / "gzsl").is_dir():
        raise ValueError("目标不是已初始化的 GZSL Git 仓库")
    repository = read_bounded_json_object(
        _control(root) / "repository.json",
        label="Repository",
    )
    if (
        repository.get("schema") != REPOSITORY_SCHEMA
        or repository.get("direction") != "gzsl"
        or repository.get("execution_policy") != "gpu_only"
        or repository.get("environment_name") != "dvsr_gpu"
    ):
        raise ValueError("GZSL 仓库记录无效")
    return root


def _create_tag(
    root: Path,
    tag: str,
    commit: str,
    message: str,
) -> None:
    existing = _run_git(
        root,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/tags/{tag}",
        check=False,
    )
    if existing.returncode == 0:
        raise FileExistsError(f"Framework Tag 已存在：{tag}")
    if existing.returncode not in {0, 1}:
        raise RuntimeError("无法检查 Framework Tag")
    _git(root, "tag", "-a", tag, commit, "-m", message)


def _require_clean(root: Path) -> None:
    status = _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if status:
        raise ValueError("Git 工作树必须 clean 才能固定 Framework")


def _head(root: Path) -> str:
    commit = _git(root, "rev-parse", "HEAD")
    if COMMIT.fullmatch(commit) is None:
        raise ValueError("Git HEAD 不是 40 位 commit")
    return commit


def _git(root: Path, *arguments: str) -> str:
    completed = _run_git(root, *arguments)
    return completed.stdout.strip()


def _run_git(
    root: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=NUL",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "tag.gpgSign=false",
            "-C",
            str(root),
            *arguments,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"Git 命令失败：{' '.join(arguments)}；{completed.stderr.strip()}"
        )
    return completed


def _real_directory(path: Path, label: str) -> Path:
    candidate = Path(path).expanduser().absolute()
    if (
        not candidate.is_dir()
        or is_link_or_reparse(candidate)
    ):
        raise ValueError(f"{label} 必须是非链接普通目录：{candidate}")
    return candidate.resolve(strict=True)


def _route(value: str) -> str:
    if value not in ROUTES:
        raise ValueError("route 必须是 reproduction/tuning/ablation/innovation")
    return value


def _normalize_framework_route_contract(
    route: str,
    value: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{route} 实验必须填写该路线的比较规则")
    fields = {
        "reproduction": {
            "source_ref",
            "source_run_ref",
            "tolerance",
            "code_standard",
            "data_standard",
            "baseline",
            "primary_metric",
        },
        "tuning": {"allowed_changes"},
        "ablation": {
            "module_ref",
            "baseline_run_ref",
            "disabled_behavior",
            "baseline",
            "primary_metric",
        },
        "innovation": {
            "baseline_run_ref",
            "baseline",
            "primary_metric",
            "minimum_delta",
        },
    }[route]
    if set(value) != fields:
        raise ValueError(
            f"{route} 比较规则字段不完整；需要："
            + "、".join(sorted(fields))
        )
    if route == "tuning":
        allowed = value["allowed_changes"]
        if (
            not isinstance(allowed, list)
            or not allowed
            or len(allowed) > 64
        ):
            raise ValueError("调参实验至少要写一个允许修改的参数")
        return {
            "allowed_changes": [
                _text(item, "允许修改的参数") for item in allowed
            ]
        }
    normalized = {
        "baseline": _finite_float(value["baseline"], "基线指标"),
        "primary_metric": _text(value["primary_metric"], "主指标"),
    }
    if route == "reproduction":
        tolerance = _finite_float(value["tolerance"], "复现误差")
        if tolerance < 0:
            raise ValueError("复现误差不能小于 0")
        for field in ("code_standard", "data_standard"):
            candidate = value[field]
            if not isinstance(candidate, dict) or not candidate:
                raise ValueError(f"{field} 必须是非空对象")
            try:
                json.dumps(candidate, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{field} 必须是有限 JSON 对象") from error
        return {
            **normalized,
            "source_ref": _text(value["source_ref"], "论文或代码来源"),
            "source_run_ref": _text(value["source_run_ref"], "来源结果"),
            "tolerance": tolerance,
            "code_standard": dict(value["code_standard"]),
            "data_standard": dict(value["data_standard"]),
        }
    if route == "ablation":
        return {
            **normalized,
            "module_ref": _text(value["module_ref"], "被关闭模块"),
            "baseline_run_ref": _text(
                value["baseline_run_ref"],
                "启用侧基线 Run",
            ),
            "disabled_behavior": _text(
                value["disabled_behavior"],
                "模块关闭行为",
            ),
        }
    minimum_delta = _finite_float(value["minimum_delta"], "创新最小提升")
    if minimum_delta < 0:
        raise ValueError("创新最小提升不能小于 0")
    return {
        **normalized,
        "baseline_run_ref": _text(
            value["baseline_run_ref"],
            "创新对照 Run",
        ),
        "minimum_delta": minimum_delta,
    }


def _framework_task_route_inputs(
    route: str,
    contract: dict[str, Any],
) -> dict[str, Any]:
    if route == "tuning":
        return {"allowed_changes": list(contract["allowed_changes"])}
    if route == "reproduction":
        return {
            field: contract[field]
            for field in (
                "source_ref",
                "source_run_ref",
                "tolerance",
                "code_standard",
                "data_standard",
            )
        }
    if route == "ablation":
        return {
            field: contract[field]
            for field in (
                "module_ref",
                "baseline_run_ref",
                "disabled_behavior",
            )
        }
    return {"source_run_ref": contract["baseline_run_ref"]}


def _finite_float(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{label} 必须是有限数字")
    return float(value)


def _sha256_regular_file(path: Path, expected: os.stat_result) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(4 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
    observed = path.stat()
    before = (
        expected.st_dev,
        expected.st_ino,
        expected.st_size,
        expected.st_mtime_ns,
    )
    after = (
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
    )
    if before != after or is_link_or_reparse(path):
        raise ValueError("读取过程中源数据发生变化，拒绝登记")
    return digest.hexdigest()


def _slug(value: object, label: str) -> str:
    if not isinstance(value, str) or SLUG.fullmatch(value) is None:
        raise ValueError(f"{label} 必须是小写英文、数字和短横线")
    return value


def _semver(value: object) -> str:
    if not isinstance(value, str) or SEMVER.fullmatch(value) is None:
        raise ValueError("Framework version 必须是 x.y.z")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是文本")
    selected = value.strip()
    if not selected or len(selected) > 512 or any(
        ord(character) < 32 for character in selected
    ):
        raise ValueError(f"{label} 无效")
    return selected


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
