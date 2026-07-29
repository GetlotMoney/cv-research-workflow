from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .io import (
    DEFAULT_JSON_LIMIT,
    atomic_create_bytes_clean,
    atomic_create_json,
    atomic_write_bytes,
    atomic_write_json,
    read_bounded_json_object,
    read_bounded_regular_file,
)
from .locking import project_snapshot_lock, project_write_lock


PROJECT_SKILL_SCHEMA = "cv-experiment-workflow.project-skill.v1"
PROJECT_SKILL_ENTRYPOINT = "SKILL.md"
PROJECT_SKILL_LIMIT = 64 * 1024
PROJECT_SKILL_INSTALL_MANIFEST = ".cv-experiment-workflow-project-skill.json"
PROJECT_SKILL_INSTALL_SCHEMA = "cv-experiment-workflow.project-skill-install.v1"
PROJECT_SCHEMAS = {
    "cv-experiment-workflow.project.v1",
    "cv-experiment-workflow.project.v2",
}


def project_skill_identity(
    project_root: Path,
    project: dict[str, Any],
) -> dict[str, str]:
    """根据项目路径和不可变项目 ID 生成稳定的项目 Skill 身份。"""
    project_id = _validated_project_id(project)
    existing = project.get("project_skill")
    if existing is not None:
        return _validated_project_skill_identity(existing)
    candidate = _hyphen_slug(Path(project_root).name)
    if not candidate or candidate == "cv-experiment-workflow":
        candidate = "cv-project"
    suffix = project_id.split("-", 1)[0]
    candidate = candidate[: 64 - len(suffix) - 1].strip("-")
    return {
        "schema": PROJECT_SKILL_SCHEMA,
        "skill_id": f"{candidate}-{suffix}",
        "entrypoint": PROJECT_SKILL_ENTRYPOINT,
    }


def render_project_skill(
    project_root: Path,
    project: dict[str, Any],
) -> bytes:
    """生成只绑定项目身份和边界、不复制通用状态机的薄 Skill。"""
    identity = project_skill_identity(project_root, project)
    project_id = _validated_project_id(project)
    project_name = _validated_project_name(project)
    root_hint = str(Path(project_root).expanduser().resolve(strict=False))
    description = (
        f"Use when 用户提到“{project_name}”、{identity['skill_id']}，"
        "或要求在该方向仓库管理完整代码框架、Idea、复现、调参、消融、"
        "创新、GPU Run、结果确认和论文交付包。"
    )
    content = f"""---
name: {identity["skill_id"]}
description: {json.dumps(description, ensure_ascii=False)}
---

# {project_name} 总协调入口

本 Skill 是这个方向仓库的专属入口；共享规则和命令必须委托给 `cv-experiment-workflow`，不得另建第二套工作流。

## 先确认项目

- 项目路径提示：`{root_hint}`
- 预期项目 ID：`{project_id}`
- 先读取 `AGENTS.md`、`.experiment-workflow/repository.json`、`.experiment-workflow/frameworks/*/framework.json` 和 `.experiment-workflow/workflow.lock.json`。
- 路径移动后，只在用户指定的工作区或原路径父目录内查找 `.experiment-workflow/project.json`；项目 ID 唯一命中才继续，零命中或多命中都停止并询问用户。
- 漂移检查所需源码 Skill：若索引给出 `skill_path`，把它拼到通用仓库路径；否则只接受唯一存在的 `skills/cv-experiment-workflow`，找不到时停止，不得猜测。
- **REQUIRED SUB-SKILL:** 使用 `cv-experiment-workflow` 的“当前唯一主路”选择 Framework、实验类型并调用 `rw.py`。

## 固定协调边界

- 结构化记录只通过工作流命令写入，不手改编号、状态和 Run 结果。
- 默认只写当前仓库；跨仓库写入必须有用户当前授权。
- 运行前检查 Skill 漂移、项目状态、Git 工作树、Adapter、环境、预算和停止条件。
- Runner 只执行冻结事实；Analyst 不把实现失败当成假设失败；Reviewer 保持独立只读。
- 未经明确授权，不 push、不发布、不删除历史，不启动超出已批准预算的高成本训练。

## 人话入口

用户可以直接说“导入一个框架”“做复现”“调参”“做消融”“记录 Idea”“试创新”“查状态”“确认结果”或“生成论文交付包”。先确定基于哪个 Framework；只有创新绑定 Idea，只有创新成功后才能生成稳定子 Framework。
"""
    rendered = content.encode("utf-8")
    if len(rendered) > PROJECT_SKILL_LIMIT:
        raise ValueError("项目专属 Skill 超过大小上限")
    return rendered


def initialize_project_skill(project_root: Path) -> dict[str, Any]:
    """为已经存在的项目补建专属 Skill，并登记机器身份。"""
    root = Path(project_root).expanduser().absolute()
    _reject_reparse_path(root, "项目根目录")
    with project_write_lock(root):
        project_path = root / ".experiment-workflow" / "project.json"
        project = _read_project(project_path)
        _validate_complete_project(
            root,
            project,
            validate_project_skill=False,
            validate_workflow_lock=False,
        )
        from .releases import workflow_lock_is_upgrade_source

        workflow_lock = read_bounded_json_object(
            root / ".experiment-workflow" / "workflow.lock.json",
            DEFAULT_JSON_LIMIT,
            "workflow.lock.json",
        )
        if not workflow_lock_is_upgrade_source(workflow_lock):
            raise ValueError("workflow lock 不受信任，拒绝初始化项目专属 Skill")
        identity = project_skill_identity(root, project)
        existing_identity = project.get("project_skill")
        if existing_identity is not None and existing_identity != identity:
            raise ValueError("project.json 中的项目 Skill 身份与当前项目不一致")

        expected = render_project_skill(root, project)
        skill_path = root / PROJECT_SKILL_ENTRYPOINT
        created = False
        if os.path.lexists(skill_path):
            actual = read_bounded_regular_file(
                skill_path,
                PROJECT_SKILL_LIMIT,
                "项目 SKILL.md",
            )
            if actual != expected:
                raise ValueError(f"已有 SKILL.md 不属于本项目，拒绝覆盖：{skill_path}")
        else:
            created = atomic_create_bytes_clean(
                skill_path,
                expected,
                transaction_id=uuid.uuid4().hex,
            )
            if not created:
                actual = read_bounded_regular_file(
                    skill_path,
                    PROJECT_SKILL_LIMIT,
                    "项目 SKILL.md",
                )
                if actual != expected:
                    raise ValueError(
                        f"SKILL.md 在创建竞争中被其他内容占用：{skill_path}"
                    )

        if existing_identity is None:
            updated = dict(project)
            updated["project_skill"] = identity
            try:
                atomic_write_json(project_path, updated)
            except BaseException:
                if (
                    created
                    and skill_path.is_file()
                    and skill_path.read_bytes() == expected
                ):
                    skill_path.unlink()
                raise
            project = updated

    return {
        "schema": PROJECT_SKILL_SCHEMA,
        "status": "created" if created else "already_initialized",
        "skill_id": identity["skill_id"],
        "source": str(skill_path),
        "project_id": project["project_id"],
    }


def rebind_project_skill(project_root: Path) -> dict[str, Any]:
    """项目移动后，核验旧生成内容并刷新路径提示。"""
    root = Path(project_root).expanduser().absolute()
    _reject_reparse_path(root, "项目根目录")
    with project_write_lock(root):
        project = _read_project(root / ".experiment-workflow" / "project.json")
        _validate_complete_project(root, project, validate_project_skill=False)
        identity = project_skill_identity(root, project)
        if project.get("project_skill") != identity:
            raise ValueError("项目尚未初始化专属 Skill")
        skill_path = root / identity["entrypoint"]
        actual = read_bounded_regular_file(
            skill_path,
            PROJECT_SKILL_LIMIT,
            "项目 SKILL.md",
        )
        expected = render_project_skill(root, project)
        if actual == expected:
            status = "already_current"
        else:
            old_root = _bound_root_hint(actual)
            if actual != render_project_skill(old_root, project):
                raise ValueError("项目 SKILL.md 含有人工修改或身份不符，拒绝刷新")
            atomic_write_bytes(
                skill_path,
                expected,
                transaction_id=uuid.uuid4().hex,
            )
            status = "rebound"
    return {
        "schema": PROJECT_SKILL_SCHEMA,
        "status": status,
        "skill_id": identity["skill_id"],
        "source": str(skill_path),
        "project_id": project["project_id"],
    }


def install_project_skill(
    project_root: Path,
    skills_root: Path,
) -> dict[str, Any]:
    """把项目内正式 Skill 副本显式安装到给定 Codex Skills 目录。"""
    from .project import is_link_or_reparse

    root = Path(project_root).expanduser().absolute()
    _reject_reparse_path(root, "项目根目录")
    with project_snapshot_lock(root):
        project = _read_project(root / ".experiment-workflow" / "project.json")
        _validate_complete_project(root, project, validate_project_skill=True)
        identity = project_skill_identity(root, project)
        if project.get("project_skill") != identity:
            raise ValueError("项目尚未初始化专属 Skill，请先运行 init-project-skill")

        source = root / identity["entrypoint"]
        source_bytes = read_bounded_regular_file(
            source,
            PROJECT_SKILL_LIMIT,
            "项目 SKILL.md",
        )
        if source_bytes != render_project_skill(root, project):
            raise ValueError(f"项目 SKILL.md 与机器身份不一致：{source}")

    destination_root = Path(skills_root).expanduser().absolute()
    _reject_reparse_path(destination_root, "Skills 根目录")
    if os.path.lexists(destination_root):
        if is_link_or_reparse(destination_root) or not destination_root.is_dir():
            raise ValueError(f"Skills 根目录不是普通目录：{destination_root}")
    else:
        destination_root.mkdir(parents=True)

    skill_directory = destination_root / identity["skill_id"]
    created_directory = False
    if os.path.lexists(skill_directory):
        if is_link_or_reparse(skill_directory) or not skill_directory.is_dir():
            raise ValueError(f"项目 Skill 安装目录不是普通目录：{skill_directory}")
    else:
        skill_directory.mkdir()
        created_directory = True

    destination = skill_directory / "SKILL.md"
    manifest_path = skill_directory / PROJECT_SKILL_INSTALL_MANIFEST
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    manifest = {
        "schema": PROJECT_SKILL_INSTALL_SCHEMA,
        "project_id": project["project_id"],
        "skill_id": identity["skill_id"],
        "source_sha256": source_digest,
    }
    try:
        if os.path.lexists(destination):
            installed = read_bounded_regular_file(
                destination,
                PROJECT_SKILL_LIMIT,
                "已安装项目 SKILL.md",
            )
            if installed != source_bytes:
                installed_manifest = _read_install_manifest(manifest_path)
                if (
                    installed_manifest.get("project_id") != project["project_id"]
                    or installed_manifest.get("skill_id") != identity["skill_id"]
                    or installed_manifest.get("source_sha256")
                    != hashlib.sha256(installed).hexdigest()
                ):
                    raise ValueError(
                        f"已安装 Skill 内容不同且不属于可安全更新副本，拒绝覆盖：{destination}"
                    )
                atomic_write_bytes(
                    destination,
                    source_bytes,
                    transaction_id=uuid.uuid4().hex,
                )
                try:
                    atomic_write_json(
                        manifest_path,
                        manifest,
                        transaction_id=uuid.uuid4().hex,
                    )
                except BaseException:
                    atomic_write_bytes(
                        destination,
                        installed,
                        transaction_id=uuid.uuid4().hex,
                    )
                    raise
                status = "updated"
            else:
                _ensure_install_manifest(manifest_path, manifest)
                status = "already_installed"
        else:
            created = atomic_create_bytes_clean(
                destination,
                source_bytes,
                transaction_id=uuid.uuid4().hex,
            )
            if not created:
                installed = read_bounded_regular_file(
                    destination,
                    PROJECT_SKILL_LIMIT,
                    "已安装项目 SKILL.md",
                )
                if installed != source_bytes:
                    raise ValueError(
                        f"安装目标在创建竞争中被其他内容占用：{destination}"
                    )
                status = "already_installed"
            else:
                status = "installed"
            try:
                _ensure_install_manifest(manifest_path, manifest)
            except BaseException:
                if created and destination.is_file() and destination.read_bytes() == source_bytes:
                    destination.unlink()
                raise
    except BaseException:
        if created_directory:
            try:
                skill_directory.rmdir()
            except OSError:
                pass
        raise

    return {
        "schema": PROJECT_SKILL_INSTALL_SCHEMA,
        "status": status,
        "skill_id": identity["skill_id"],
        "source": str(source),
        "destination": str(destination),
    }


def validate_project_skill_binding(
    project_root: Path,
    project: dict[str, Any],
) -> None:
    """校验项目身份、根 Skill 和当前路径绑定完全一致。"""
    identity = project_skill_identity(project_root, project)
    if project.get("project_skill") != identity:
        raise ValueError("项目专属 Skill 身份缺失或无效")
    skill_path = project_root / identity["entrypoint"]
    actual = read_bounded_regular_file(
        skill_path,
        PROJECT_SKILL_LIMIT,
        "项目专属 Skill",
    )
    if actual != render_project_skill(project_root, project):
        raise ValueError(f"项目专属 Skill 与当前项目身份或路径不一致：{skill_path}")


def _read_project(path: Path) -> dict[str, Any]:
    project = read_bounded_json_object(path, DEFAULT_JSON_LIMIT, "project.json")
    if project.get("schema") not in PROJECT_SCHEMAS:
        raise ValueError(f"项目 schema 无效：{path}")
    _validated_project_id(project)
    _validated_project_name(project)
    return project


def _validated_project_id(project: dict[str, Any]) -> str:
    project_id = project.get("project_id")
    try:
        if (
            not isinstance(project_id, str)
            or str(uuid.UUID(project_id)) != project_id
        ):
            raise ValueError
    except ValueError as error:
        raise ValueError("项目 ID 无效") from error
    return project_id


def _validated_project_name(project: dict[str, Any]) -> str:
    name = project.get("name")
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > 200
        or name != name.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise ValueError("项目名称无效")
    return name


def _validated_project_skill_identity(value: Any) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "skill_id", "entrypoint"}
        or value.get("schema") != PROJECT_SKILL_SCHEMA
        or value.get("entrypoint") != PROJECT_SKILL_ENTRYPOINT
        or not isinstance(value.get("skill_id"), str)
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", value["skill_id"])
        is None
    ):
        raise ValueError("项目专属 Skill 身份无效")
    return dict(value)


def _validate_complete_project(
    root: Path,
    project: dict[str, Any],
    *,
    validate_project_skill: bool,
    validate_workflow_lock: bool = True,
) -> None:
    from .records import _validate_project

    schema = project["schema"]
    _validate_project(
        root,
        expected_schema=schema,
        validate_workflow_lock=validate_workflow_lock,
        validate_project_skill=validate_project_skill,
    )
    control = root / ".experiment-workflow"
    if schema == "cv-experiment-workflow.project.v1":
        from .attempts import validate_project_workflow_locked

        validate_project_workflow_locked(control)
    else:
        from .validation import validate_boundaries_locked

        validate_boundaries_locked(control)


def _bound_root_hint(content: bytes) -> Path:
    text = content.decode("utf-8")
    matches = re.findall(r"^- 项目路径提示：`([^`\r\n]+)`$", text, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError("项目 SKILL.md 缺少唯一旧路径提示，拒绝刷新")
    return Path(matches[0]).expanduser().absolute()


def _reject_reparse_path(path: Path, label: str) -> None:
    from .project import is_link_or_reparse

    existing: list[Path] = []
    current = path
    while True:
        if os.path.lexists(current):
            existing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for entry in reversed(existing):
        if is_link_or_reparse(entry):
            raise ValueError(f"{label} 路径包含链接或 reparse 目录：{entry}")


def _read_install_manifest(path: Path) -> dict[str, Any]:
    manifest = read_bounded_json_object(path, DEFAULT_JSON_LIMIT, "项目 Skill 安装清单")
    if (
        set(manifest) != {"schema", "project_id", "skill_id", "source_sha256"}
        or manifest.get("schema") != PROJECT_SKILL_INSTALL_SCHEMA
        or not isinstance(manifest.get("source_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", manifest["source_sha256"]) is None
    ):
        raise ValueError(f"项目 Skill 安装清单无效：{path}")
    return manifest


def _ensure_install_manifest(path: Path, manifest: dict[str, Any]) -> None:
    if os.path.lexists(path):
        if _read_install_manifest(path) != manifest:
            raise ValueError(f"项目 Skill 安装清单冲突：{path}")
        return
    created = atomic_create_json(
        path,
        manifest,
        transaction_id=uuid.uuid4().hex,
    )
    if not created and _read_install_manifest(path) != manifest:
        raise ValueError(f"项目 Skill 安装清单在创建竞争中被占用：{path}")


def _hyphen_slug(value: str) -> str:
    parts = re.findall(r"[a-z0-9]+", value.lower())
    return "-".join(parts)
