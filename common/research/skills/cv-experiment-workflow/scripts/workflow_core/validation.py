from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .io import (
    DEFAULT_JSON_LIMIT,
    read_bounded_json_object,
    read_bounded_regular_file,
    validate_bounded_regular_file,
)
from .project import PROJECT_SCHEMA, PROJECT_SCHEMA_V2, is_link_or_reparse
from .records import normalize_safe_relative_path


ARTIFACT_DIGEST = re.compile(r"(?:sha256:)?[0-9a-f]{64}")
ROLE_NAMES = (
    "coordinator",
    "idea-scientist",
    "implementer",
    "runner",
    "analyst",
    "reviewer",
)
ROLE_MEMORY_LIMIT = 64 * 1024
V1_ROOT_FILES = {"project.json", "workflow.lock.json", "adapter.json"}
V1_ROOT_DIRECTORIES = {
    ".runtime", "ideas", "versions", "trials", "code-assets",
    "attempts", "agents", "artifacts", "templates",
}
V2_ROOT_FILES = {
    "project.json", "workflow.lock.json", "adapter.json", "evidence.jsonl",
}
V2_OPTIONAL_ROOT_FILES = {"repository.json"}
V2_ROOT_DIRECTORIES = {
    ".runtime", "ideas", "modules", "runs", "sources", "tasks",
    "versions", "artifacts", "templates",
}
V2_OPTIONAL_ROOT_DIRECTORIES = {
    "codebases",
    "paper-packages",
    "frameworks",
    "idea-tree",
}
V2_EMPTY_OBJECT_DIRECTORIES = {
    "versions",
}
IDEA_NAME = re.compile(r"IDEA-[0-9]{4}\.json")
VERSION_NAME = re.compile(r"VER-[0-9]{4}\.json")
ATTEMPT_NAME = re.compile(r"ATTEMPT-[0-9]{4}\.json")
TRIAL_NAME = re.compile(r"TRIAL-[0-9]{4}")
ASSET_NAME = re.compile(r"CODE-[0-9]{4}")
ASSET_V1_FILES = {
    "asset.json", "module.py", "test_contract.py", "provenance.json",
    "framework.html",
}
ASSET_V2_FILES = ASSET_V1_FILES | {"composition.json"}


def validate_boundaries_locked(control: Path) -> dict[str, int]:
    """在调用者已持有的单一 project_write_lock 快照内检查存储边界。"""
    control = Path(control)
    if is_link_or_reparse(control) or not control.is_dir():
        raise ValueError(f"控制平面不是普通目录：{control}")
    project = read_bounded_json_object(
        control / "project.json",
        DEFAULT_JSON_LIMIT,
        "project.json",
    )
    schema = project.get("schema")
    if schema == PROJECT_SCHEMA:
        _walk_v1_control_plane(control)
        validate_role_memories_locked(control / "agents")
        _validate_artifact_directory(control / "artifacts")
    elif schema == PROJECT_SCHEMA_V2:
        _tasks, _runs, _events, _content, catalog = (
            validate_v2_state_and_catalog_locked(control)
        )
        return {
            name: len(catalog[name])
            for name in ("sources", "ideas", "templates", "modules")
        }
    else:
        raise ValueError(f"项目 schema 无效：{control / 'project.json'}")
    from .project_templates import validate_project_templates_locked
    return validate_project_templates_locked(control)


def preflight_workflow_command_locked(control: Path) -> None:
    """在调用者持有 project lock 时统一拒绝无效控制平面。"""
    validate_boundaries_locked(control)


def validate_role_memories_locked(directory: Path) -> None:
    if is_link_or_reparse(directory) or not directory.is_dir():
        raise ValueError(f"agents 目录不是普通目录：{directory}")
    allowed = {f"{role}.md" for role in ROLE_NAMES}
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if entry.name not in allowed:
            raise ValueError(f"agents 目录存在未知文件或目录：{entry}")
        if is_link_or_reparse(entry):
            raise ValueError(f"角色 memory 拒绝链接或 reparse 路径：{entry}")
        if not entry.is_file():
            raise ValueError(f"角色 memory 不是普通文件：{entry}")
        content = read_bounded_regular_file(
            entry, ROLE_MEMORY_LIMIT, f"角色 memory {entry.name}",
        )
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"角色 memory 必须是 UTF-8：{entry}") from error


def _validate_artifact_directory(directory: Path) -> None:
    if is_link_or_reparse(directory) or not directory.is_dir():
        raise ValueError(f"artifact 目录不是普通目录：{directory}")
    entries = sorted(directory.iterdir(), key=lambda item: item.name)
    for entry in entries:
        if entry.name != "index.json":
            raise ValueError(f"artifact 目录只允许严格 index.json，发现：{entry}")
        if is_link_or_reparse(entry) or not entry.is_file():
            raise ValueError(f"artifact index 不是普通文件：{entry}")
    index = directory / "index.json"
    if not index.is_file() or is_link_or_reparse(index):
        raise ValueError(f"artifact index 缺失或不是普通文件：{index}")
    try:
        payload = read_bounded_json_object(index, DEFAULT_JSON_LIMIT, "artifact index")
    except (OSError, ValueError) as error:
        raise ValueError(f"artifact index 不是有效 UTF-8 JSON：{index}；{error}") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "artifacts"}
        or payload.get("schema") != "cv-experiment-workflow.artifact-index.v1"
        or not isinstance(payload.get("artifacts"), list)
    ):
        raise ValueError(f"artifact index 结构无效：{index}")
    for item in payload["artifacts"]:
        if not isinstance(item, dict) or set(item) not in ({"path"}, {"path", "digest"}):
            raise ValueError(f"artifact index 引用结构无效：{index}")
        normalized = normalize_safe_relative_path(item.get("path"), "artifact index path")
        if item["path"] != normalized:
            raise ValueError(f"artifact index path 未规范化：{index}")
        if "digest" in item and (
            not isinstance(item["digest"], str)
            or ARTIFACT_DIGEST.fullmatch(item["digest"]) is None
        ):
            raise ValueError(f"artifact index digest 无效：{index}")


def _walk_v1_control_plane(control: Path) -> None:
    _validate_fixed_directory(
        control, files=V1_ROOT_FILES, directories=V1_ROOT_DIRECTORIES,
        label="控制平面根目录",
    )
    for name in V1_ROOT_FILES:
        _validate_json_file(control / name)
    _validate_empty_runtime(control / ".runtime")
    _validate_id_json_directory(control / "ideas", IDEA_NAME, "Idea")
    _validate_id_json_directory(control / "versions", VERSION_NAME, "Version")
    _validate_id_json_directory(control / "attempts", ATTEMPT_NAME, "Attempt")
    _validate_object_directories(
        control / "trials", TRIAL_NAME, {"trial.json"}, "Trial",
    )
    _validate_code_asset_directories(control / "code-assets")


def validate_v2_root_locked(control: Path) -> None:
    """只检查 v2 固定根、根文件和常量大小目录，不遍历对象账本。"""
    optional_directories = {
        name
        for name in V2_OPTIONAL_ROOT_DIRECTORIES
        if (control / name).exists() or is_link_or_reparse(control / name)
    }
    optional_files = {
        name
        for name in V2_OPTIONAL_ROOT_FILES
        if (control / name).exists() or is_link_or_reparse(control / name)
    }
    _validate_fixed_directory(
        control,
        files=V2_ROOT_FILES | optional_files,
        directories=V2_ROOT_DIRECTORIES | optional_directories,
        label="v2 控制平面根目录",
    )
    for name in V2_ROOT_FILES - {"evidence.jsonl"}:
        _validate_json_file(control / name)
    for name in optional_files:
        _validate_json_file(control / name)
    validate_bounded_regular_file(
        control / "evidence.jsonl",
        DEFAULT_JSON_LIMIT,
        "evidence.jsonl",
    )
    _validate_empty_runtime(control / ".runtime")
    _validate_artifact_directory(control / "artifacts")
    from .policy import load_adapter

    load_adapter(control)


def validate_v2_state_locked(
    control: Path,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    bytes,
]:
    """完整读取并交叉校验一次 v2 Task、Run 和 Evidence 快照。"""
    tasks, runs, events, evidence_content, _catalog = (
        validate_v2_state_and_catalog_locked(control)
    )
    return tasks, runs, events, evidence_content


def validate_v2_state_and_catalog_locked(
    control: Path,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    bytes,
    dict[str, dict[str, dict[str, Any]]],
]:
    """在同一锁快照中读取 v2 catalog、Task、Run 和 Evidence。"""
    snapshot = validate_v2_full_snapshot_locked(control)
    return snapshot[:5]


def validate_v2_full_snapshot_locked(
    control: Path,
    *,
    hash_session: Any | None = None,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    bytes,
    dict[str, dict[str, dict[str, Any]]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    """一次读取完整 v2 快照，供需要 Brief/Package 的内部调用方复用。"""
    validate_v2_root_locked(control)
    for name in sorted(V2_EMPTY_OBJECT_DIRECTORIES):
        _validate_empty_directory(control / name, name)
    from .codebases import validate_codebases_locked

    codebases = validate_codebases_locked(control)
    catalog = load_v2_catalog_locked(control)
    from .paper_package import (
        validate_research_briefs_locked,
        validate_research_packages_locked,
    )

    briefs = validate_research_briefs_locked(
        control,
        sources=catalog["sources"],
    )
    from .tasking import (
        validate_task_catalog_links_locked,
        validate_task_codebase_links_locked,
        validate_task_prerequisite_links_locked,
        validate_tasks_locked,
    )
    from .runs import (
        validate_runs_locked,
        validate_task_run_links_locked,
        validate_task_run_route_contracts_locked,
    )
    from .evidence import validate_evidence_ledger_locked

    tasks = validate_tasks_locked(control)
    validate_task_codebase_links_locked(tasks, codebases)
    validate_task_prerequisite_links_locked(tasks)
    validate_task_catalog_links_locked(tasks, catalog)
    runs = validate_runs_locked(control)
    validate_task_run_links_locked(tasks, runs)
    validate_task_run_route_contracts_locked(control, tasks, runs)
    events, _runs, evidence_content = validate_evidence_ledger_locked(
        control,
        known_runs=runs,
        known_tasks=tasks,
    )
    packages = validate_research_packages_locked(
        control,
        tasks=tasks,
        runs=runs,
        events=events,
        catalog=catalog,
        briefs=briefs,
        hash_session=hash_session,
    )
    return (
        tasks,
        runs,
        events,
        evidence_content,
        catalog,
        briefs,
        packages,
    )


def load_v2_catalog_locked(
    control: Path,
) -> dict[str, dict[str, dict[str, Any]]]:
    from .v2_catalog import (
        validate_ideas_locked,
        validate_modules_locked,
        validate_sources_locked,
    )
    from .project_templates import load_project_templates_locked

    sources = validate_sources_locked(control)
    ideas = validate_ideas_locked(control, sources)
    templates = load_project_templates_locked(control, sources=sources)
    modules = validate_modules_locked(control, sources, ideas, templates)
    return {
        "sources": sources,
        "ideas": ideas,
        "templates": templates,
        "modules": modules,
    }


def validate_v2_evidence_gate_locked(
    control: Path,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    bytes,
]:
    """证据写入/关闭前做一次完整 v2 快照与 Template 校验。"""
    snapshot = validate_v2_state_locked(control)
    from .project_templates import validate_project_templates_locked

    validate_project_templates_locked(control)
    return snapshot


def _validate_fixed_directory(
    directory: Path,
    *,
    files: set[str],
    directories: set[str],
    label: str,
) -> None:
    if is_link_or_reparse(directory) or not directory.is_dir():
        raise ValueError(f"{label} 不是普通目录：{directory}")
    entries = {entry.name: entry for entry in directory.iterdir()}
    expected = files | directories
    unknown = sorted(set(entries) - expected)
    missing = sorted(expected - set(entries))
    if unknown:
        raise ValueError(f"{label} 存在未知文件或目录：{entries[unknown[0]]}")
    if missing:
        raise ValueError(f"{label} 缺少必需条目：{directory / missing[0]}")
    for name in sorted(files):
        entry = entries[name]
        if is_link_or_reparse(entry) or not entry.is_file():
            raise ValueError(f"{label} 文件不是普通文件：{entry}")
    for name in sorted(directories):
        entry = entries[name]
        if is_link_or_reparse(entry) or not entry.is_dir():
            raise ValueError(f"{label} 子目录不是普通目录：{entry}")


def _validate_empty_directory(directory: Path, label: str) -> None:
    if is_link_or_reparse(directory) or not directory.is_dir():
        raise ValueError(f"{label} 不是普通目录：{directory}")
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        raise ValueError(f"{label} 目录存在未知文件或目录：{entry}")


def _validate_empty_runtime(directory: Path) -> None:
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if _is_transaction_residual(entry.name.lower()):
            raise ValueError(f"控制平面存在事务残留 temp/marker：{entry}")
        raise ValueError(f".runtime 存在未知文件或目录：{entry}")


def _validate_id_json_directory(
    directory: Path,
    pattern: re.Pattern[str],
    label: str,
) -> None:
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if pattern.fullmatch(entry.name) is None:
            raise ValueError(f"{label} 目录存在未知文件或目录：{entry}")
        _validate_json_file(entry)


def _validate_object_directories(
    directory: Path,
    pattern: re.Pattern[str],
    files: set[str],
    label: str,
) -> None:
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if (
            pattern.fullmatch(entry.name) is None
            or is_link_or_reparse(entry)
            or not entry.is_dir()
        ):
            raise ValueError(f"{label} 目录存在未知对象：{entry}")
        _validate_fixed_directory(
            entry, files=files, directories=set(), label=f"{label} 对象目录",
        )
        for name in files:
            if name.endswith(".json"):
                _validate_json_file(entry / name)


def _validate_code_asset_directories(directory: Path) -> None:
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if (
            ASSET_NAME.fullmatch(entry.name) is None
            or is_link_or_reparse(entry)
            or not entry.is_dir()
        ):
            raise ValueError(f"CodeAsset 目录存在未知对象：{entry}")
        asset_path = entry / "asset.json"
        asset = read_bounded_json_object(asset_path, DEFAULT_JSON_LIMIT, "asset.json")
        schema = asset.get("schema")
        if schema == "cv-experiment-workflow.code-asset.v1":
            files = ASSET_V1_FILES
        elif schema == "cv-experiment-workflow.code-asset.v2":
            files = ASSET_V2_FILES
        else:
            raise ValueError(f"CodeAsset schema 无效：{entry.name}")
        _validate_fixed_directory(
            entry, files=files, directories=set(), label="CodeAsset 对象目录",
        )
        for name in files:
            if name.endswith(".json"):
                _validate_json_file(entry / name)


def _validate_json_file(path: Path) -> None:
    try:
        read_bounded_json_object(path, DEFAULT_JSON_LIMIT)
    except (OSError, ValueError) as error:
        raise ValueError(f"控制面 JSON 无效：{path}；{error}") from error


def _is_transaction_residual(name: str) -> bool:
    return (
        name.endswith(".tmp")
        or ".cvexp-" in name
        or name == "new-trial-transaction.json"
        or name.startswith("new-trial-")
    )
