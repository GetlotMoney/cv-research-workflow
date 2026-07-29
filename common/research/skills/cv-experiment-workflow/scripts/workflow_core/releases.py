from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

from .io import (
    DEFAULT_JSON_LIMIT,
    atomic_write_json,
    read_bounded_json_object,
)
from .locking import project_snapshot_lock, project_write_lock
from .project import PROJECT_SCHEMA, PROJECT_SCHEMA_V2, is_link_or_reparse


LEGACY_WORKFLOW_LOCK = {
    "schema": "cv-experiment-workflow.workflow-lock.v1",
    "skill_id": "cv-experiment-workflow",
    "version": "1.0.0",
}
WORKFLOW_RELEASE_IDENTITY = {
    "schema": "cv-experiment-workflow.workflow-lock.v2",
    "skill_id": "cv-experiment-workflow",
    "release_version": "1.5.0",
    "system_version": "SYS-V2.13.0",
}
TRUSTED_PREVIOUS_WORKFLOW_LOCKS = (
    {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.5.0",
        "system_version": "SYS-V2.13.0",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 214,
            "digest": (
                "sha256:"
                "e3961ac577526cc8c7286108695e5678ace8006d6018428a"
                "1d0d031dcc1db20e"
            ),
        },
    },
    {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.5.0",
        "system_version": "SYS-V2.13.0",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 214,
            "digest": (
                "sha256:"
                "706b7648bae86212507bd433f0018ad89f50f3131b88d501"
                "0eafe59cb2605481"
            ),
        },
    },
    {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.5.0",
        "system_version": "SYS-V2.13.0",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 214,
            "digest": (
                "sha256:"
                "fac9322368559cb858c13f23705f01b0fbcefd7bcf367d373"
                "3f7cc261846584b"
            ),
        },
    },
    {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.5.0",
        "system_version": "SYS-V2.13.0",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 214,
            "digest": (
                "sha256:"
                "58e7041985a3deb9f2deaf7b30cc08d7be456407bba701156"
                "c15ce6da16a2154"
            ),
        },
    },
    {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.5.0",
        "system_version": "SYS-V2.13.0",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 214,
            "digest": (
                "sha256:"
                "d2af72b9220007ea5d84f6dba9a836fbeb058ef21b79d667"
                "3668bdd76d94089e"
            ),
        },
    },
    {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.4.0",
        "system_version": "SYS-V2.12.0",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 211,
            "digest": (
                "sha256:"
                "c54ba663dc3cab4ccb50f304ff8a539cae1f79aabac69bc"
                "7015f4ce715aa5dad"
            ),
        },
    },
    {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.2.3",
        "system_version": "SYS-V2.10.3",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 57,
            "digest": (
                "sha256:"
                "a610932129c3ee8397f7a1d887b3b485fb2f283a67af8a92"
                "d668a9d7e977592a"
            ),
        },
    },
    {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.2.2",
        "system_version": "SYS-V2.10.2",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 57,
            "digest": (
                "sha256:"
                "4125290e8cad4c349d2b59d80132ff25333571d692afa528"
                "3acd85eb18871ad0"
            ),
        },
    },
    {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.2.1",
        "system_version": "SYS-V2.10.1",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 56,
            "digest": (
                "sha256:"
                "b17499136cefeab72995f498a430e0afddf9ddb79d703beac"
                "0a2349118c96fdc"
            ),
        },
    },
    {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.1.0",
        "system_version": "SYS-V2.9",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 55,
            "digest": (
                "sha256:"
                "56807d433ae8fc81bc8cc2cce75a7f4614cf4ac80601f5c3"
                "a1ad695e6c0bbc27"
            ),
        },
    },
    {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.1.1",
        "system_version": "SYS-V2.9.1",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 55,
            "digest": (
                "sha256:"
                "3a4a2989a0df7ed180d96924684d6eed49f1de2405f081c0"
                "c7d70e5918862185"
            ),
        },
    },
    {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.1.2",
        "system_version": "SYS-V2.9.2",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 55,
            "digest": (
                "sha256:"
                "200971d3078c27e82228a8bfcc62730105134a79a38264cc"
                "ee5be616cf48140a"
            ),
        },
    },
    {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.2.0",
        "system_version": "SYS-V2.10",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 56,
            "digest": (
                "sha256:"
                "eac79ff88bec8d079e20b4d28c4088f18c9e8bf85a2bb10a"
                "c553fc116211cafa"
            ),
        },
    },
)
_REQUIRED_SKILL_FILES = {
    "SKILL.md",
    "scripts/rw.py",
    "references/workflow.md",
}
_IGNORED_DIRECTORY_NAMES = {"__pycache__"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


def skill_payload_fingerprint(skill_root: Path) -> dict[str, Any]:
    """按相对路径和原始字节计算可复制 Skill 的稳定身份。"""
    root = Path(skill_root).expanduser().absolute()
    if is_link_or_reparse(root) or not root.is_dir():
        raise ValueError(f"Skill 根目录无效：{root}")
    files = _payload_files(root)
    relative_paths = [path.relative_to(root).as_posix() for path in files]
    missing = sorted(_REQUIRED_SKILL_FILES - set(relative_paths))
    if missing:
        raise ValueError(f"Skill 根目录缺少正式文件：{missing[0]}")

    aggregate = hashlib.sha256()
    for path, relative in zip(files, relative_paths, strict=True):
        relative_bytes = relative.encode("utf-8")
        content = path.read_bytes()
        aggregate.update(len(relative_bytes).to_bytes(8, "big"))
        aggregate.update(relative_bytes)
        aggregate.update(len(content).to_bytes(8, "big"))
        aggregate.update(content)
    return {
        "algorithm": "sha256-path-bytes-v1",
        "file_count": len(files),
        "digest": f"sha256:{aggregate.hexdigest()}",
    }


def check_workflow_drift(
    project: Path,
    source_skill: Path,
) -> dict[str, Any]:
    """只读比较当前运行 Skill、源码 Skill 与实例的旧版本锁。"""
    project_root = Path(project).expanduser().absolute()
    control = project_root / ".experiment-workflow"
    if (
        is_link_or_reparse(project_root)
        or not project_root.is_dir()
        or is_link_or_reparse(control)
        or not control.is_dir()
    ):
        raise ValueError(f"项目控制目录无效：{control}")
    with project_snapshot_lock(project_root):
        project_payload = _read_json(control / "project.json", "project.json")
        lock_payload = _read_json(
            control / "workflow.lock.json",
            "workflow.lock.json",
        )

    runtime_root = Path(__file__).resolve().parents[2]
    runtime_payload = skill_payload_fingerprint(runtime_root)
    source_payload = skill_payload_fingerprint(source_skill)
    runtime_matches_source = runtime_payload == source_payload

    current_lock = current_workflow_lock()
    project_matches_runtime = lock_payload == current_lock
    if not runtime_matches_source:
        status = "source_runtime_drift"
    elif lock_payload == LEGACY_WORKFLOW_LOCK:
        status = "legacy_unpinned"
    elif project_matches_runtime:
        status = "in_sync"
    elif lock_payload.get("schema") == WORKFLOW_RELEASE_IDENTITY["schema"]:
        status = "project_runtime_drift"
    else:
        status = "incompatible_lock"

    return {
        "schema": "cv-experiment-workflow.drift-report.v1",
        "status": status,
        "read_only": True,
        "project_schema": project_payload.get("schema"),
        "project_lock": lock_payload,
        "runtime_lock": current_lock,
        "runtime_payload": runtime_payload,
        "source_payload": source_payload,
        "runtime_matches_source": runtime_matches_source,
        "project_matches_runtime": project_matches_runtime,
    }


def current_workflow_lock() -> dict[str, Any]:
    """返回当前运行 Skill 的完整、可校验工作流锁。"""
    runtime_root = Path(__file__).resolve().parents[2]
    return {
        **WORKFLOW_RELEASE_IDENTITY,
        "payload": skill_payload_fingerprint(runtime_root),
    }


def workflow_lock_is_compatible(payload: dict[str, Any]) -> bool:
    """旧锁保持可读；新版锁必须与当前运行 Skill 的字节身份完全一致。"""
    return payload == LEGACY_WORKFLOW_LOCK or payload == current_workflow_lock()


def console_workflow_lock_trust(payload: dict[str, Any]) -> str:
    """只给只读控制台识别精确可信锁，不扩大普通命令的兼容范围。"""
    if payload == current_workflow_lock():
        return "current"
    if payload == LEGACY_WORKFLOW_LOCK:
        return "legacy"
    if payload in TRUSTED_PREVIOUS_WORKFLOW_LOCKS:
        return "trusted_previous"
    raise ValueError("workflow lock 不属于只读控制台的精确可信集合")


def workflow_lock_is_upgrade_source(payload: dict[str, Any]) -> bool:
    """项目专属入口补建与锁升级可接受的精确旧锁集合。"""
    return payload in (
        LEGACY_WORKFLOW_LOCK,
        *TRUSTED_PREVIOUS_WORKFLOW_LOCKS,
        current_workflow_lock(),
    )


def upgrade_workflow_lock(
    project: Path,
    source_skill: Path,
) -> dict[str, Any]:
    """显式把旧锁升级为当前运行 Skill 的指纹锁，不做静默迁移。"""
    project_root = Path(project).expanduser().absolute()
    control = project_root / ".experiment-workflow"
    if (
        is_link_or_reparse(project_root)
        or not project_root.is_dir()
        or is_link_or_reparse(control)
        or not control.is_dir()
    ):
        raise ValueError(f"项目控制目录无效：{control}")

    runtime_payload = skill_payload_fingerprint(Path(__file__).resolve().parents[2])
    source_payload = skill_payload_fingerprint(source_skill)
    if runtime_payload != source_payload:
        raise ValueError("源码 Skill 与当前运行 Skill 不一致，拒绝升级实例版本锁")
    preflight_project = _read_json(control / "project.json", "project.json")
    preflight_schema = preflight_project.get("schema")
    if preflight_schema not in {PROJECT_SCHEMA, PROJECT_SCHEMA_V2}:
        raise ValueError("项目 schema 无效，拒绝升级实例版本锁")

    with project_write_lock(project_root):
        project_payload = _read_json(control / "project.json", "project.json")
        project_schema = project_payload.get("schema")
        if project_schema != preflight_schema:
            raise ValueError("项目 schema 在升级核验期间发生变化，拒绝写入")
        if project_schema not in {PROJECT_SCHEMA, PROJECT_SCHEMA_V2}:
            raise ValueError("项目 schema 无效，拒绝升级实例版本锁")
        from .records import _validate_project

        _validate_project(
            project_root,
            expected_schema=project_schema,
            validate_workflow_lock=False,
            validate_project_skill=False,
        )
        if project_schema == PROJECT_SCHEMA:
            from .attempts import validate_project_workflow_locked

            validate_project_workflow_locked(control)
        else:
            from .validation import validate_boundaries_locked

            validate_boundaries_locked(control)
        lock_path = control / "workflow.lock.json"
        previous_lock = _read_json(lock_path, "workflow.lock.json")
        workflow_lock = current_workflow_lock()
        final_source_payload = skill_payload_fingerprint(source_skill)
        if (
            final_source_payload != source_payload
            or final_source_payload != workflow_lock["payload"]
        ):
            raise ValueError("源码 Skill 在升级核验期间发生变化，拒绝写入")
        supported_previous = (
            LEGACY_WORKFLOW_LOCK,
            *TRUSTED_PREVIOUS_WORKFLOW_LOCKS,
            workflow_lock,
        )
        if previous_lock not in supported_previous:
            raise ValueError("实例 v2 版本锁不受信任，拒绝升级或修复")
        if previous_lock == workflow_lock:
            status = "already_current"
        else:
            atomic_write_json(
                lock_path,
                workflow_lock,
                transaction_id=uuid.uuid4().hex,
            )
            status = "upgraded"
    return {
        "schema": "cv-experiment-workflow.lock-upgrade-report.v1",
        "status": status,
        "previous_lock": previous_lock,
        "workflow_lock": workflow_lock,
    }


def _payload_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if any(part in _IGNORED_DIRECTORY_NAMES for part in relative.parts):
            continue
        if path.suffix.lower() in _IGNORED_SUFFIXES:
            continue
        if is_link_or_reparse(path):
            raise ValueError(f"Skill 正式内容拒绝链接或 reparse 路径：{path}")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise ValueError(f"Skill 正式内容存在未知路径类型：{path}")
    return files


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if is_link_or_reparse(path) or not path.is_file():
        raise ValueError(f"{label} 不是普通文件：{path}")
    return read_bounded_json_object(path, DEFAULT_JSON_LIMIT, label)
