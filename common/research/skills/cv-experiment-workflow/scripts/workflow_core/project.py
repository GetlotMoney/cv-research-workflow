from __future__ import annotations

import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any

from .io import (
    atomic_create_bytes,
    atomic_write_bytes,
    atomic_write_json,
    read_bounded_json_object,
    transaction_temp_path,
)
from .locking import project_initialization_lock


PROJECT_SCHEMA = "cv-experiment-workflow.project.v1"
PROJECT_SCHEMA_V2 = "cv-experiment-workflow.project.v2"
PROJECT_SCHEMAS = {
    "v1": PROJECT_SCHEMA,
    "v2": PROJECT_SCHEMA_V2,
}
ADAPTER_DEFAULT = {
    "schema": "cv-experiment-workflow.adapter.v1",
    "status": "unbound",
    "code_sources": [],
    "capabilities": {},
}
ARTIFACT_INDEX_DEFAULT = {
    "schema": "cv-experiment-workflow.artifact-index.v1",
    "artifacts": [],
}
CONTROL_DIRECTORIES = (
    "ideas",
    "trials",
    "code-assets",
    "attempts",
    "versions",
    "agents",
    ".runtime",
    "artifacts",
    "templates",
)
V2_CONTROL_DIRECTORIES = (
    "ideas",
    "versions",
    "modules",
    "runs",
    "sources",
    "tasks",
    ".runtime",
    "artifacts",
    "templates",
)
CONTROL_DIRECTORIES_BY_LAYOUT = {
    "v1": CONTROL_DIRECTORIES,
    "v2": V2_CONTROL_DIRECTORIES,
}
AssetUpdate = tuple[Path, bytes, Path]
STAGING_NAME = ".experiment-workflow.staging"
STAGING_MARKER = ".cv-experiment-workflow-staging"
STAGING_SCHEMA = "cv-experiment-workflow.staging.v1"
STAGING_TRANSACTION_ID = "0" * 32
ROOT_ASSET_TRANSACTION_ID = "1" * 32
STAGING_JSON_FILES = (
    STAGING_MARKER,
    "project.json",
    "workflow.lock.json",
    "adapter.json",
    "artifacts/index.json",
)
V2_STAGING_FILES = STAGING_JSON_FILES + ("evidence.jsonl",)


class ProjectAlreadyInitializedError(ValueError):
    """目标目录已经包含实验工作流控制目录。"""


def init_project(path: Path, name: str, layout: str = "v1") -> dict[str, Any]:
    """初始化项目，并返回持久化的项目身份对象。"""
    project_root = Path(path).expanduser()
    project_name = name.strip()

    if not project_name:
        raise ValueError("项目名称不能为空")
    if layout not in PROJECT_SCHEMAS:
        raise ValueError("layout 必须是 v1 或 v2")
    if project_root.exists() and not project_root.is_dir():
        raise NotADirectoryError(f"项目路径不是目录：{project_root}")
    project_root.mkdir(parents=True, exist_ok=True)
    with project_initialization_lock(project_root) as canonical_root:
        return _init_project_locked(
            project_root,
            project_name,
            canonical_root,
            layout,
        )


def _init_project_locked(
    project_root: Path,
    project_name: str,
    canonical_root: str,
    layout: str = "v1",
) -> dict[str, Any]:
    control = project_root / ".experiment-workflow"
    if os.path.lexists(control):
        _recover_completed_control(project_root, control, canonical_root)
        raise ProjectAlreadyInitializedError(
            f"实验工作流已经存在，拒绝覆盖：{control}"
        )
    project = {
        "schema": PROJECT_SCHEMAS[layout],
        "project_id": str(uuid.uuid4()),
        "name": project_name,
    }
    from .project_skills import project_skill_identity

    project["project_skill"] = project_skill_identity(project_root, project)
    transaction_id = uuid.uuid4().hex
    staging: Path | None = None
    staging_created = False
    committed = False
    applied_assets: list[AssetUpdate] = []

    try:
        _remove_owned_stale_staging(project_root, canonical_root)
        if os.path.lexists(control):
            raise ProjectAlreadyInitializedError(
                f"实验工作流已经存在，拒绝覆盖：{control}"
            )
        asset_updates = _prepare_project_assets(project_root, project)
        staging = project_root / STAGING_NAME
        staging.mkdir()
        staging_created = True
        marker = {
            "schema": STAGING_SCHEMA,
            "transaction_id": transaction_id,
            "layout": layout,
            "project_root": canonical_root,
            "root_temp_files": [
                temporary_path.name
                for _destination, _content, temporary_path in asset_updates
            ],
        }
        atomic_write_json(
            staging / STAGING_MARKER,
            marker,
            transaction_id=STAGING_TRANSACTION_ID,
        )
        for directory in CONTROL_DIRECTORIES_BY_LAYOUT[layout]:
            (staging / directory).mkdir()

        atomic_write_json(
            staging / "project.json",
            project,
            transaction_id=STAGING_TRANSACTION_ID,
        )
        # 延迟导入避免 project 与 releases 的路径安全工具形成加载环。
        from .releases import current_workflow_lock

        atomic_write_json(
            staging / "workflow.lock.json",
            current_workflow_lock(),
            transaction_id=STAGING_TRANSACTION_ID,
        )
        atomic_write_json(
            staging / "adapter.json",
            ADAPTER_DEFAULT,
            transaction_id=STAGING_TRANSACTION_ID,
        )
        atomic_write_json(
            staging / "artifacts" / "index.json",
            ARTIFACT_INDEX_DEFAULT,
            transaction_id=STAGING_TRANSACTION_ID,
        )
        if layout == "v2":
            atomic_write_bytes(
                staging / "evidence.jsonl",
                b"",
                transaction_id=STAGING_TRANSACTION_ID,
            )
        applied_assets = _apply_project_assets(asset_updates)
        if os.path.lexists(control):
            raise ProjectAlreadyInitializedError(
                f"实验工作流已经存在，拒绝覆盖：{control}"
            )
        os.replace(staging, control)
        committed = True
        staging_created = False
        _release_project_asset_anchors(applied_assets)
        try:
            (control / STAGING_MARKER).unlink()
        except OSError:
            pass
    except BaseException as initialization_error:
        if committed or _read_owned_marker(control, transaction_id, canonical_root) is not None:
            raise
        rollback_failure: BaseException | None = None
        if applied_assets:
            try:
                _restore_project_assets(applied_assets)
            except BaseException as error:
                rollback_failure = error
        cleanup_failures, residual_paths = _cleanup_failed_initialization(
            staging,
            staging_created=staging_created,
            layout=layout,
        )
        if rollback_failure is not None or cleanup_failures:
            failure_parts: list[str] = []
            if rollback_failure is not None:
                failure_parts.append(f"回滚失败：{rollback_failure}")
            if cleanup_failures:
                failures = "；".join(
                    f"{failed_path}: {error}"
                    for failed_path, error in cleanup_failures
                )
                failure_parts.append(f"清理失败：{failures}")
            residuals = "、".join(str(path) for path in residual_paths) or "无"
            raise RuntimeError(
                f"项目初始化失败：{initialization_error}；"
                f"{'；'.join(failure_parts)}；残留路径：{residuals}"
            ) from initialization_error
        raise

    return project


def _cleanup_failed_initialization(
    staging: Path | None,
    *,
    staging_created: bool,
    layout: str = "v1",
) -> tuple[list[tuple[Path, BaseException]], list[Path]]:
    failures: list[tuple[Path, BaseException]] = []
    residual_paths: list[Path] = []

    if staging_created and staging is not None:
        try:
            unknown = _unknown_reserved_staging_entries(staging, layout=layout)
            if unknown:
                paths = "、".join(unknown)
                raise RuntimeError(
                    f"保留 staging 含未知内容，拒绝清理：{paths}"
                )
            shutil.rmtree(staging)
        except BaseException as error:
            failures.append((staging, error))
        if os.path.lexists(staging):
            residual_paths.append(staging)
            if not any(path == staging for path, _error in failures):
                failures.append((staging, OSError("删除后仍然存在")))

    return failures, residual_paths


def _remove_owned_stale_staging(project_root: Path, canonical_root: str) -> None:
    candidate = project_root / STAGING_NAME
    unknown: list[str] = []
    owned_root_temp_files: set[str] = set()
    if os.path.lexists(candidate):
        staging_unknown = _unknown_reserved_staging_entries(candidate)
        unknown.extend(staging_unknown)
        if not staging_unknown:
            marker_root_temp_files = _read_owned_marker(
                candidate,
                None,
                canonical_root,
            )
            if marker_root_temp_files is not None:
                owned_root_temp_files.update(marker_root_temp_files)
    root_temporary_paths = _root_asset_temporary_paths(project_root)
    for temporary_path in root_temporary_paths:
        if not os.path.lexists(temporary_path):
            continue
        if (
            temporary_path.name not in owned_root_temp_files
            or is_link_or_reparse(temporary_path)
            or not temporary_path.is_file()
        ):
            unknown.append(temporary_path.name)
    if unknown:
        paths = "、".join(sorted(set(unknown)))
        raise RuntimeError(f"保留事务含未知内容，拒绝清理：{paths}")
    if os.path.lexists(candidate):
        shutil.rmtree(candidate)
    for temporary_path in root_temporary_paths:
        if temporary_path.name in owned_root_temp_files:
            temporary_path.unlink(missing_ok=True)


def _root_asset_temporary_paths(project_root: Path) -> list[Path]:
    return [
        transaction_temp_path(project_root / filename, ROOT_ASSET_TRANSACTION_ID)
        for filename in ("AGENTS.md", "WORKFLOW.md", ".gitignore", "SKILL.md")
    ]


def _unknown_reserved_staging_entries(
    staging: Path,
    *,
    layout: str | None = None,
) -> list[str]:
    if is_link_or_reparse(staging) or not staging.is_dir():
        return [staging.name]

    selected_layout = layout or _read_staging_layout(staging)
    allowed_directories = {
        Path(name).as_posix()
        for name in CONTROL_DIRECTORIES_BY_LAYOUT[selected_layout]
    }
    allowed_files: set[str] = set()
    staging_files = (
        V2_STAGING_FILES if selected_layout == "v2" else STAGING_JSON_FILES
    )
    for relative_name in staging_files:
        target = staging / relative_name
        allowed_files.add(Path(relative_name).as_posix())
        allowed_files.add(
            transaction_temp_path(target, STAGING_TRANSACTION_ID)
            .relative_to(staging)
            .as_posix()
        )

    unknown: list[str] = []
    for entry in staging.rglob("*"):
        relative = entry.relative_to(staging).as_posix()
        if is_link_or_reparse(entry):
            unknown.append(relative)
        elif entry.is_dir():
            if relative not in allowed_directories:
                unknown.append(relative)
        elif entry.is_file():
            if relative not in allowed_files:
                unknown.append(relative)
        else:
            unknown.append(relative)
    return sorted(set(unknown))


def _read_staging_layout(staging: Path) -> str:
    marker = staging / STAGING_MARKER
    if is_link_or_reparse(marker) or not marker.is_file():
        return "v1"
    try:
        payload = read_bounded_json_object(marker)
    except (OSError, ValueError):
        return "v1"
    layout = payload.get("layout", "v1")
    return layout if layout in PROJECT_SCHEMAS else "v1"


def _read_owned_marker(
    directory: Path,
    transaction_id: str | None,
    canonical_root: str,
) -> list[str] | None:
    if is_link_or_reparse(directory) or not directory.is_dir():
        return None
    marker = directory / STAGING_MARKER
    if is_link_or_reparse(marker) or not marker.is_file():
        return None
    try:
        marker_payload = read_bounded_json_object(marker)
    except (OSError, ValueError):
        return None
    root_temp_files = marker_payload.get("root_temp_files")
    marker_transaction_id = marker_payload.get("transaction_id")
    marker_layout = marker_payload.get("layout", "v1")
    if (
        marker_payload.get("schema") != STAGING_SCHEMA
        or marker_layout not in PROJECT_SCHEMAS
        or not isinstance(marker_transaction_id, str)
        or len(marker_transaction_id) != 32
        or any(
            character not in "0123456789abcdef"
            for character in marker_transaction_id
        )
        or (
            transaction_id is not None
            and marker_transaction_id != transaction_id
        )
        or marker_payload.get("project_root") != canonical_root
        or not isinstance(root_temp_files, list)
        or not all(isinstance(name, str) for name in root_temp_files)
    ):
        return None
    project_root = directory.parent
    allowed_temp_files = {
        transaction_temp_path(
            project_root / filename, ROOT_ASSET_TRANSACTION_ID
        ).name
        for filename in ("AGENTS.md", "WORKFLOW.md", ".gitignore", "SKILL.md")
    }
    if len(set(root_temp_files)) != len(root_temp_files) or not set(
        root_temp_files
    ).issubset(allowed_temp_files):
        return None
    return root_temp_files


def _recover_completed_control(
    project_root: Path,
    control: Path,
    canonical_root: str,
) -> None:
    if is_link_or_reparse(control) or not control.is_dir():
        return
    root_temp_files = _read_owned_marker(control, None, canonical_root)
    if root_temp_files is None:
        return

    temporary_paths = [project_root / name for name in root_temp_files]
    conflicts = [
        path.name
        for path in temporary_paths
        if os.path.lexists(path)
        and (is_link_or_reparse(path) or not path.is_file())
    ]
    if conflicts:
        paths = "、".join(conflicts)
        raise RuntimeError(f"已提交事务的保留 temp 类型异常：{paths}")
    for temporary_path in temporary_paths:
        temporary_path.unlink(missing_ok=True)
    (control / STAGING_MARKER).unlink(missing_ok=True)


def is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _prepare_project_assets(
    project_root: Path,
    project: dict[str, Any],
) -> list[AssetUpdate]:
    asset_root = Path(__file__).resolve().parents[2] / "assets" / "project"
    updates: list[AssetUpdate] = []
    for source_name, destination_name in (
        ("AGENTS.md", "AGENTS.md"),
        ("WORKFLOW.md", "WORKFLOW.md"),
        ("gitignore.txt", ".gitignore"),
    ):
        destination = project_root / destination_name
        if os.path.lexists(destination):
            continue
        content = (asset_root / source_name).read_bytes()
        temporary_path = transaction_temp_path(
            destination, ROOT_ASSET_TRANSACTION_ID
        )
        updates.append((destination, content, temporary_path))
    skill_destination = project_root / "SKILL.md"
    if os.path.lexists(skill_destination):
        raise ValueError(
            f"项目根目录已有 SKILL.md，无法安全生成项目专属 Skill："
            f"{skill_destination}"
        )
    from .project_skills import render_project_skill

    updates.append(
        (
            skill_destination,
            render_project_skill(project_root, project),
            transaction_temp_path(
                skill_destination,
                ROOT_ASSET_TRANSACTION_ID,
            ),
        )
    )
    return updates


def _apply_project_assets(updates: list[AssetUpdate]) -> list[AssetUpdate]:
    applied: list[AssetUpdate] = []
    try:
        for update in updates:
            destination, content, _temporary_path = update
            created = atomic_create_bytes(
                destination,
                content,
                transaction_id=ROOT_ASSET_TRANSACTION_ID,
            )
            if created:
                applied.append(update)
            elif destination.name == "SKILL.md":
                raise FileExistsError(
                    f"项目专属 SKILL.md 在初始化提交时被其他内容占用：{destination}"
                )
    except BaseException as apply_error:
        try:
            _restore_project_assets(applied)
        except Exception as restore_error:
            raise RuntimeError(
                f"项目说明写入失败：{apply_error}；回滚失败：{restore_error}"
            ) from apply_error
        raise
    return applied


def _restore_project_assets(updates: list[AssetUpdate]) -> None:
    failures: list[str] = []
    for destination, _content, temporary_path in reversed(updates):
        try:
            owns_destination = (
                os.path.lexists(destination)
                and os.path.lexists(temporary_path)
                and os.path.samefile(destination, temporary_path)
            )
            if owns_destination:
                destination.unlink()
            else:
                failures.append(
                    f"{destination.name}: 所有权已变化，已保留当前目标"
                )
        except BaseException as error:
            failures.append(f"{destination.name}: {error}")
        try:
            temporary_path.unlink(missing_ok=True)
        except BaseException as error:
            failures.append(f"{temporary_path.name}: {error}")
    if failures:
        raise OSError("；".join(failures))


def _release_project_asset_anchors(updates: list[AssetUpdate]) -> None:
    failures: list[str] = []
    for destination, _content, temporary_path in reversed(updates):
        try:
            temporary_path.unlink(missing_ok=True)
        except BaseException as error:
            failures.append(f"{destination.name}: {error}")
    if failures:
        raise OSError("；".join(failures))
