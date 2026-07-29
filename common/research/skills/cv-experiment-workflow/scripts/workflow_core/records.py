from __future__ import annotations

import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .io import atomic_create_json, atomic_write_json, read_bounded_json_object
from .locking import project_write_lock
from .project import PROJECT_SCHEMA, is_link_or_reparse


IDEA_SCHEMA = "cv-experiment-workflow.idea.v1"
IDEA_SCHEMA_V2 = "cv-experiment-workflow.idea.v2"
VERSION_SCHEMA = "cv-experiment-workflow.version.v1"
VERSION_SCHEMA_V2 = "cv-experiment-workflow.version.v2"
IDEA_ID = re.compile(r"IDEA-[0-9]{4}")
VERSION_ID = re.compile(r"VER-[0-9]{4}")
EVIDENCE_ID = re.compile(r"(?:ATTEMPT|TRIAL|VER|IDEA)-[0-9]{4}")
ATTEMPT_ID = re.compile(r"ATTEMPT-[0-9]{4}")


def new_idea(
    project: Path,
    title: str,
    note: str,
    *,
    source_label: str | None = None,
    source_locator: str | None = None,
    source_note: str | None = None,
    parent_idea_id: str | None = None,
    related_idea_ids: list[str] | None = None,
) -> dict[str, Any]:
    title = _required_text(title, "Idea 标题")
    note = _required_text(note, "Idea 备注")
    project_root = _initialized_project(project)
    with project_write_lock(project_root):
        control = _validate_project(project_root)
        _preflight_workflow_command_locked(control)
        record_id = _next_id(control / "ideas", "IDEA", IDEA_ID)
        related = [] if related_idea_ids is None else list(related_idea_ids)
        _validate_requested_relations_locked(
            control, record_id, parent_idea_id, related,
        )
        timestamp = _utc_now()
        sources: list[dict[str, str]] = []
        if any(value is not None for value in (source_label, source_locator, source_note)):
            sources.append(
                {
                    "label": source_label or "",
                    "locator": source_locator or "",
                    "note": source_note or "",
                }
            )
        payload: dict[str, Any] = {
            "schema": IDEA_SCHEMA,
            "id": record_id,
            "status": "draft",
            "title": title,
            "note": note,
            "source_refs": sources,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        if parent_idea_id is not None or related:
            payload.update({
                "schema": IDEA_SCHEMA_V2,
                "revision": 1,
                "revision_history": [],
                "parent_idea_id": parent_idea_id,
                "related_idea_ids": related,
                "implementation_mapping": None,
            })
        validate_idea(payload, expected_id=record_id)
        _create_record(control / "ideas" / f"{record_id}.json", payload)
        return payload


def activate_idea(
    project: Path,
    idea_id: str,
    problem: str,
    mechanism: str,
    hypothesis: str,
) -> dict[str, Any]:
    if IDEA_ID.fullmatch(idea_id) is None:
        raise ValueError(f"Idea ID 格式错误：{idea_id}")
    problem = _required_text(problem, "problem")
    mechanism = _required_text(mechanism, "mechanism")
    hypothesis = _required_text(hypothesis, "hypothesis")
    project_root = _initialized_project(project)
    with project_write_lock(project_root):
        control = _validate_project(project_root)
        _preflight_workflow_command_locked(control)
        path = control / "ideas" / f"{idea_id}.json"
        payload = _read_object(path)
        validate_idea(payload, expected_id=idea_id)
        if payload.get("status") != "draft":
            raise ValueError(f"只有 draft Idea 可以激活：{idea_id}")
        activated = dict(payload)
        activated.update(
            {
                "status": "active",
                "problem": problem,
                "mechanism": mechanism,
                "hypothesis": hypothesis,
                "updated_at": _utc_now(),
            }
        )
        validate_idea(activated, expected_id=idea_id)
        atomic_write_json(path, activated)
        return activated


def revise_idea(
    project: Path,
    idea_id: str,
    reason: str,
    *,
    problem: str | None = None,
    mechanism: str | None = None,
    hypothesis: str | None = None,
    evidence: list[Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(idea_id, str) or IDEA_ID.fullmatch(idea_id) is None:
        raise ValueError(f"Idea ID 格式错误：{idea_id}")
    reason = _required_text(reason, "revision reason")
    evidence_refs = [] if evidence is None else evidence
    if (
        not isinstance(evidence_refs, list)
        or not evidence_refs
        or not all(
            isinstance(item, str) and EVIDENCE_ID.fullmatch(item)
            for item in evidence_refs
        )
        or len(evidence_refs) != len(set(evidence_refs))
        or not any(ATTEMPT_ID.fullmatch(item) for item in evidence_refs)
    ):
        raise ValueError("evidence 必须是严格去重且至少含一个 ATTEMPT ID 的 JSON array")
    changes = {"problem": problem, "mechanism": mechanism, "hypothesis": hypothesis}
    supplied = {
        key: _required_text(value, key)
        for key, value in changes.items()
        if value is not None
    }
    if not supplied:
        raise ValueError("revise-idea 至少提供一个科学字段")
    project_root = _initialized_project(project)
    with project_write_lock(project_root):
        control = _validate_project(project_root)
        _preflight_workflow_command_locked(control)
        path = control / "ideas" / f"{idea_id}.json"
        current = _read_object(path)
        validate_idea(current, expected_id=idea_id)
        if current.get("status") != "active":
            raise ValueError(f"只有 active 且完整的 Idea 可以修订：{idea_id}")
        _validate_evidence_refs_locked(control, evidence_refs)
        if not any(current[key] != value for key, value in supplied.items()):
            raise ValueError("revise-idea 必须实际改变至少一个科学字段")
        upgraded = _upgrade_idea_v2(current)
        previous_mapping = deepcopy(upgraded["implementation_mapping"])
        history_item = {
            "revision": upgraded["revision"],
            "problem": upgraded["problem"],
            "mechanism": upgraded["mechanism"],
            "hypothesis": upgraded["hypothesis"],
            "implementation_mapping": previous_mapping,
            "revision_reason": reason,
            "evidence_refs": list(evidence_refs),
            "revised_at": _utc_now(),
        }
        revised = deepcopy(upgraded)
        revised.update(supplied)
        revised["revision"] += 1
        revised["revision_history"] = [*upgraded["revision_history"], history_item]
        if "problem" in supplied or "mechanism" in supplied:
            revised["implementation_mapping"] = None
        revised["updated_at"] = history_item["revised_at"]
        validate_idea(revised, expected_id=idea_id)
        atomic_write_json(path, revised, transaction_id=uuid.uuid4().hex)
        return revised


def set_implementation_mapping(
    project: Path, idea_id: str, mapping: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(idea_id, str) or IDEA_ID.fullmatch(idea_id) is None:
        raise ValueError(f"Idea ID 格式错误：{idea_id}")
    normalized = validate_implementation_mapping(mapping)
    project_root = _initialized_project(project)
    with project_write_lock(project_root):
        control = _validate_project(project_root)
        _preflight_workflow_command_locked(control)
        path = control / "ideas" / f"{idea_id}.json"
        current = _read_object(path)
        validate_idea(current, expected_id=idea_id)
        if current.get("status") != "active":
            raise ValueError(f"只有 active Idea 可以设置 implementation mapping：{idea_id}")
        if (
            normalized["problem"] != current["problem"]
            or normalized["mechanism"] != current["mechanism"]
        ):
            raise ValueError("mapping.problem/mechanism 必须等于 Idea 当前科学字段")
        upgraded = _upgrade_idea_v2(current)
        if upgraded["implementation_mapping"] == normalized:
            return upgraded
        if upgraded["implementation_mapping"] is not None:
            raise ValueError(f"Idea 已存在不同 implementation mapping：{idea_id}")
        updated = deepcopy(upgraded)
        updated["implementation_mapping"] = normalized
        updated["updated_at"] = _utc_now()
        validate_idea(updated, expected_id=idea_id)
        atomic_write_json(path, updated, transaction_id=uuid.uuid4().hex)
        return updated


def register_version(
    project: Path,
    name: str,
    repo_url: str,
    commit: str,
    code_path: str = ".",
) -> dict[str, Any]:
    name = _required_text(name, "Version 名称")
    repo_url = _required_text(repo_url, "仓库 URL")
    if re.fullmatch(r"[0-9a-fA-F]{40}", commit) is None:
        raise ValueError("commit 必须是精确 40 位十六进制字符串")
    normalized_path = normalize_safe_relative_path(code_path, "code-path")
    project_root = _initialized_project(project)
    with project_write_lock(project_root):
        control = _validate_project(project_root)
        _preflight_workflow_command_locked(control)
        record_id = _next_id(control / "versions", "VER", VERSION_ID)
        timestamp = _utc_now()
        payload: dict[str, Any] = {
            "schema": VERSION_SCHEMA,
            "id": record_id,
            "status": "active",
            "name": name,
            "code_sources": [
                {
                    "repo_url": repo_url,
                    "commit": commit.lower(),
                    "relative_path": normalized_path,
                }
            ],
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        validate_version(payload, expected_id=record_id)
        _create_record(control / "versions" / f"{record_id}.json", payload)
        return payload


def activate_version(
    project: Path,
    version_id: str,
    repo_url: str,
    commit: str,
    code_path: str = ".",
) -> dict[str, Any]:
    """将严格有效的 promotion draft 原子升级为 active Version v2。"""
    if not isinstance(version_id, str) or VERSION_ID.fullmatch(version_id) is None:
        raise ValueError(f"Version ID 格式错误：{version_id}")
    repo_url = _required_text(repo_url, "仓库 URL")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-fA-F]{40}", commit) is None:
        raise ValueError("commit 必须是精确 40 位十六进制字符串")
    normalized_path = normalize_safe_relative_path(code_path, "code-path")
    requested_source = {
        "repo_url": repo_url,
        "commit": commit.lower(),
        "relative_path": normalized_path,
    }
    project_root = _initialized_project(project)
    with project_write_lock(project_root):
        control = _validate_project(project_root)
        from .attempts import (
            _validated_promotion_assets,
            validate_project_workflow_locked,
        )
        validate_project_workflow_locked(control)
        path = control / "versions" / f"{version_id}.json"
        draft = _read_object(path)
        validate_version(draft, expected_id=version_id)
        if draft.get("schema") == VERSION_SCHEMA_V2:
            if draft["code_sources"] == [requested_source]:
                return draft
            raise ValueError(f"active Version 已登记不同集成来源：{version_id}")
        if draft.get("status") != "draft":
            raise ValueError(f"只有 promotion draft Version 可以激活：{version_id}")
        template_id, accepted_refs = _validated_promotion_assets(
            control, draft["ordered_code_asset_ids"],
        )
        activated = {
            "schema": VERSION_SCHEMA_V2,
            "id": version_id,
            "status": "active",
            "name": draft["name"],
            "base_version_id": draft["base_version_id"],
            "template_id": template_id,
            "accepted_attempt_ids": list(draft["accepted_attempt_ids"]),
            "accepted_code_asset_ids": list(draft["ordered_code_asset_ids"]),
            "accepted_code_refs": deepcopy(accepted_refs),
            "code_sources": [requested_source],
            "created_at": draft["created_at"],
            "updated_at": _utc_now(),
        }
        validate_version(activated, expected_id=version_id)
        atomic_write_json(path, activated, transaction_id=uuid.uuid4().hex)
        return activated


def _initialized_project(project: Path) -> Path:
    root = Path(project).expanduser()
    control = root / ".experiment-workflow"
    if (
        is_link_or_reparse(root)
        or not root.is_dir()
        or is_link_or_reparse(control)
        or not control.is_dir()
    ):
        raise ValueError(f"项目不存在或尚未初始化：{root}")
    return root


def _validate_project(
    project_root: Path,
    *,
    expected_schema: str = PROJECT_SCHEMA,
    validate_workflow_lock: bool = True,
    validate_project_skill: bool = True,
) -> Path:
    control = project_root / ".experiment-workflow"
    if is_link_or_reparse(control) or not control.is_dir():
        raise ValueError(f"项目控制目录无效：{control}")
    project_json = control / "project.json"
    payload = _read_object(project_json)
    project_id = payload.get("project_id")
    name = payload.get("name")
    try:
        valid_project_id = (
            isinstance(project_id, str)
            and str(uuid.UUID(project_id)) == project_id
        )
    except ValueError:
        valid_project_id = False
    if (
        payload.get("schema") != expected_schema
        or not valid_project_id
        or not isinstance(name, str)
        or not name.strip()
    ):
        raise ValueError(f"项目元数据无效：{project_json}")
    if validate_workflow_lock:
        workflow_lock_path = control / "workflow.lock.json"
        workflow_lock = _read_object(workflow_lock_path)
        from .releases import workflow_lock_is_compatible

        if not workflow_lock_is_compatible(workflow_lock):
            raise ValueError(f"workflow lock 元数据无效：{workflow_lock_path}")
    if validate_project_skill:
        from .project_skills import validate_project_skill_binding

        validate_project_skill_binding(project_root, payload)
    for directory_name in ("ideas", "versions"):
        directory = control / directory_name
        if is_link_or_reparse(directory) or not directory.is_dir():
            raise ValueError(f"项目控制目录缺失：{directory}")
    return control


def _preflight_workflow_command_locked(control: Path) -> None:
    # validation 依赖 records 的路径规范化函数，因此必须延迟导入以避免循环加载。
    from .validation import preflight_workflow_command_locked
    preflight_workflow_command_locked(control)


def _read_object(path: Path) -> dict[str, Any]:
    if is_link_or_reparse(path) or not path.is_file():
        raise ValueError(f"JSON 不是项目内普通文件：{path}")
    try:
        payload = read_bounded_json_object(path)
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取有效 JSON：{path}；{error}") from error
    return payload


def _next_id(directory: Path, prefix: str, pattern: re.Pattern[str]) -> str:
    numbers: list[int] = []
    for path in directory.iterdir():
        if pattern.fullmatch(path.stem) is None or path.suffix != ".json":
            continue
        if is_link_or_reparse(path) or not path.is_file():
            raise ValueError(f"对象 ID 路径不是普通文件：{path}")
        numbers.append(int(path.stem.split("-")[1]))
    number = max(numbers, default=0) + 1
    if number > 9999:
        raise ValueError(f"{prefix} ID 已耗尽")
    return f"{prefix}-{number:04d}"


def _create_record(path: Path, payload: dict[str, Any]) -> None:
    if not atomic_create_json(path, payload, transaction_id=uuid.uuid4().hex):
        raise FileExistsError(f"记录已存在，拒绝覆盖：{path}")


def validate_idea(
    payload: dict[str, Any],
    *,
    expected_id: str,
) -> None:
    forbidden = {
        "base_version",
        "trial",
        "code",
        "Inbox",
        "Run",
        "source_type",
        "priority",
    }
    if forbidden.intersection(payload):
        raise ValueError(f"Idea 含禁止字段：{expected_id}")
    schema = payload.get("schema")
    if schema not in {IDEA_SCHEMA, IDEA_SCHEMA_V2}:
        raise ValueError(f"Idea schema 无效：{expected_id}")
    base_fields = {
        "schema", "id", "status", "title", "note", "source_refs",
        "created_at", "updated_at",
    }
    if payload.get("status") == "active":
        base_fields |= {"problem", "mechanism", "hypothesis"}
    v2_fields = base_fields | {
        "revision", "revision_history", "parent_idea_id", "related_idea_ids",
        "implementation_mapping",
    }
    if set(payload) != (v2_fields if schema == IDEA_SCHEMA_V2 else base_fields):
        raise ValueError(f"Idea 字段集合无效：{expected_id}")
    if (
        payload.get("id") != expected_id
        or not isinstance(expected_id, str)
        or IDEA_ID.fullmatch(expected_id) is None
        or payload.get("status") not in {"draft", "active"}
        or not _is_nonempty_text(payload.get("title"))
        or not _is_nonempty_text(payload.get("note"))
        or not _is_utc_iso(payload.get("created_at"))
        or not _is_utc_iso(payload.get("updated_at"))
    ):
        raise ValueError(f"Idea 记录无效：{expected_id}")
    source_refs = payload.get("source_refs")
    if not isinstance(source_refs, list):
        raise ValueError(f"Idea 来源列表无效：{expected_id}")
    for source_ref in source_refs:
        if (
            not isinstance(source_ref, dict)
            or set(source_ref) != {"label", "locator", "note"}
            or not all(isinstance(value, str) for value in source_ref.values())
        ):
            raise ValueError(f"Idea 来源项无效：{expected_id}")
    if payload["status"] == "active" and not all(
        _is_nonempty_text(payload.get(field))
        for field in ("problem", "mechanism", "hypothesis")
    ):
        raise ValueError(f"active Idea 缺少科学字段：{expected_id}")
    if schema == IDEA_SCHEMA_V2:
        _validate_idea_v2_fields(payload, expected_id)


def _validate_idea_v2_fields(payload: dict[str, Any], expected_id: str) -> None:
    revision = payload.get("revision")
    parent = payload.get("parent_idea_id")
    related = payload.get("related_idea_ids")
    history = payload.get("revision_history")
    if (
        type(revision) is not int
        or revision < 1
        or (parent is not None and not _strict_fullmatch(IDEA_ID, parent))
        or parent == expected_id
        or not isinstance(related, list)
        or not all(_strict_fullmatch(IDEA_ID, item) for item in related)
        or len(related) != len(set(related))
        or expected_id in related
        or parent in related
        or not isinstance(history, list)
        or len(history) != revision - 1
    ):
        raise ValueError(f"Idea v2 revision 或关系无效：{expected_id}")
    for index, item in enumerate(history, start=1):
        if (
            not isinstance(item, dict)
            or set(item) != {
                "revision", "problem", "mechanism", "hypothesis",
                "implementation_mapping", "revision_reason", "evidence_refs",
                "revised_at",
            }
            or type(item.get("revision")) is not int
            or item["revision"] != index
            or not all(_is_nonempty_text(item.get(key)) for key in (
                "problem", "mechanism", "hypothesis", "revision_reason",
            ))
            or not _is_utc_iso(item.get("revised_at"))
        ):
            raise ValueError(f"Idea v2 revision_history 无效：{expected_id}")
        refs = item.get("evidence_refs")
        if (
            not isinstance(refs, list)
            or not refs
            or not all(_strict_fullmatch(EVIDENCE_ID, ref) for ref in refs)
            or len(refs) != len(set(refs))
            or not any(_strict_fullmatch(ATTEMPT_ID, ref) for ref in refs)
        ):
            raise ValueError(f"Idea v2 revision_history evidence 无效：{expected_id}")
        if item["implementation_mapping"] is not None:
            historical_mapping = validate_implementation_mapping(
                item["implementation_mapping"],
            )
            if (
                item["implementation_mapping"] != historical_mapping
                or historical_mapping["problem"] != item["problem"]
                or historical_mapping["mechanism"] != item["mechanism"]
            ):
                raise ValueError(
                    f"Idea v2 历史 implementation mapping 漂移：{expected_id}"
                )
    if payload.get("implementation_mapping") is not None:
        mapping = validate_implementation_mapping(payload["implementation_mapping"])
        if (
            payload["implementation_mapping"] != mapping
            or payload.get("status") != "active"
            or mapping["problem"] != payload.get("problem")
            or mapping["mechanism"] != payload.get("mechanism")
        ):
            raise ValueError(f"Idea v2 implementation mapping 漂移：{expected_id}")


def validate_implementation_mapping(value: object) -> dict[str, str]:
    fields = {
        "problem", "mechanism", "attachment_point", "target_path",
        "validation", "disabled_behavior",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("implementation mapping 字段无效")
    normalized: dict[str, str] = {}
    for field in fields:
        normalized[field] = _required_text(value.get(field), f"mapping.{field}")
    normalized["target_path"] = normalize_safe_relative_path(
        normalized["target_path"], "mapping.target_path",
    )
    return normalized


def _upgrade_idea_v2(payload: dict[str, Any]) -> dict[str, Any]:
    if payload["schema"] == IDEA_SCHEMA_V2:
        return deepcopy(payload)
    upgraded = deepcopy(payload)
    upgraded.update({
        "schema": IDEA_SCHEMA_V2,
        "revision": 1,
        "revision_history": [],
        "parent_idea_id": None,
        "related_idea_ids": [],
        "implementation_mapping": None,
    })
    return upgraded


def _validate_evidence_refs_locked(control: Path, refs: list[str]) -> None:
    for ref in refs:
        prefix = ref.split("-", 1)[0]
        if prefix == "IDEA":
            payload = _read_object(control / "ideas" / f"{ref}.json")
            validate_idea(payload, expected_id=ref)
        elif prefix == "VER":
            payload = _read_object(control / "versions" / f"{ref}.json")
            validate_version(payload, expected_id=ref)
        elif prefix == "TRIAL":
            from .templates import _validate_trial_shape
            payload = _read_object(control / "trials" / ref / "trial.json")
            _validate_trial_shape(payload, ref)
        elif prefix == "ATTEMPT":
            from .attempts import validate_attempt
            payload = _read_object(control / "attempts" / f"{ref}.json")
            validate_attempt(payload, expected_id=ref)
            if payload["status"] != "completed":
                raise ValueError(f"revision evidence Attempt 必须 completed：{ref}")
        else:  # regex gate above should make this unreachable
            raise ValueError(f"evidence ID 无效：{ref}")


def validate_project_ideas_locked(control: Path) -> dict[str, int]:
    ideas: dict[str, dict[str, Any]] = {}
    for path in sorted((control / "ideas").glob("IDEA-*.json")):
        payload = _read_object(path)
        validate_idea(payload, expected_id=path.stem)
        ideas[path.stem] = payload
    for idea_id, payload in ideas.items():
        if payload["schema"] != IDEA_SCHEMA_V2:
            continue
        parent = payload["parent_idea_id"]
        if parent is not None and parent not in ideas:
            raise ValueError(f"Idea parent 缺失：{idea_id}/{parent}")
        for related in payload["related_idea_ids"]:
            if related not in ideas:
                raise ValueError(f"Idea related 缺失：{idea_id}/{related}")
        for history in payload["revision_history"]:
            _validate_evidence_refs_locked(control, history["evidence_refs"])
    for idea_id in ideas:
        seen: set[str] = set()
        current: str | None = idea_id
        while current is not None:
            if current in seen:
                raise ValueError(f"Idea parent 链出现循环：{idea_id}")
            seen.add(current)
            node = ideas[current]
            current = (
                node["parent_idea_id"]
                if node["schema"] == IDEA_SCHEMA_V2
                else None
            )
    return {"ideas": len(ideas)}


def _validate_requested_relations_locked(
    control: Path,
    idea_id: str,
    parent_idea_id: str | None,
    related_idea_ids: list[str],
) -> None:
    if parent_idea_id is not None and not _strict_fullmatch(IDEA_ID, parent_idea_id):
        raise ValueError(f"parent Idea ID 格式错误：{parent_idea_id}")
    if (
        not all(_strict_fullmatch(IDEA_ID, item) for item in related_idea_ids)
        or len(related_idea_ids) != len(set(related_idea_ids))
        or idea_id in related_idea_ids
        or parent_idea_id in related_idea_ids
        or parent_idea_id == idea_id
    ):
        raise ValueError("Idea parent/related 关系无效、重复或自引用")
    requested = ([parent_idea_id] if parent_idea_id is not None else []) + related_idea_ids
    for ref in requested:
        payload = _read_object(control / "ideas" / f"{ref}.json")
        validate_idea(payload, expected_id=ref)
    seen = {idea_id}
    current = parent_idea_id
    while current is not None:
        if current in seen:
            raise ValueError("Idea parent 链出现循环")
        seen.add(current)
        payload = _read_object(control / "ideas" / f"{current}.json")
        current = (
            payload["parent_idea_id"]
            if payload["schema"] == IDEA_SCHEMA_V2
            else None
        )


def validate_version(payload: dict[str, Any], *, expected_id: str) -> None:
    if payload.get("schema") == VERSION_SCHEMA_V2:
        _validate_version_v2(payload, expected_id=expected_id)
        return
    sources = payload.get("code_sources")
    active_fields = {
        "schema", "id", "status", "name", "code_sources", "created_at", "updated_at",
    }
    draft_fields = active_fields | {
        "base_version_id", "accepted_attempt_ids", "ordered_code_asset_ids",
    }
    status = payload.get("status")
    expected_fields = active_fields if status == "active" else draft_fields
    if (set(payload) != expected_fields
            or payload.get("schema") != VERSION_SCHEMA
            or not isinstance(expected_id, str)
            or VERSION_ID.fullmatch(expected_id) is None
            or payload.get("id") != expected_id
            or status not in {"active", "draft"}
            or not _is_nonempty_text(payload.get("name"))
            or not isinstance(sources, list) or not sources
            or not _is_utc_iso(payload.get("created_at"))
            or not _is_utc_iso(payload.get("updated_at"))):
        raise ValueError(f"Version 记录无效：{expected_id}")
    for source in sources:
        if (not isinstance(source, dict)
                or set(source) != {"repo_url", "commit", "relative_path"}
                or not _is_nonempty_text(source.get("repo_url"))
                or not isinstance(source.get("commit"), str)
                or re.fullmatch(r"[0-9a-fA-F]{40}", source["commit"]) is None):
            raise ValueError(f"Version code_sources 不完整：{expected_id}")
        normalized = normalize_safe_relative_path(
            source.get("relative_path"), "Version relative_path",
        )
        if source["relative_path"] != normalized:
            raise ValueError(f"Version relative_path 未规范化：{expected_id}")
    if status == "draft":
        attempts = payload.get("accepted_attempt_ids")
        assets = payload.get("ordered_code_asset_ids")
        if (
            not _strict_fullmatch(VERSION_ID, payload.get("base_version_id"))
            or not isinstance(attempts, list) or not attempts
            or not all(
                isinstance(item, str)
                and re.fullmatch(r"ATTEMPT-[0-9]{4}", item)
                for item in attempts
            )
            or len(attempts) != len(set(attempts))
            or not isinstance(assets, list)
            or not all(
                isinstance(item, str) and re.fullmatch(r"CODE-[0-9]{4}", item)
                for item in assets
            )
            or len(assets) != len(set(assets))
        ):
            raise ValueError(f"draft promotion Version 记录无效：{expected_id}")


def _validate_version_v2(payload: dict[str, Any], *, expected_id: str) -> None:
    fields = {
        "schema", "id", "status", "name", "base_version_id", "template_id",
        "accepted_attempt_ids", "accepted_code_asset_ids", "accepted_code_refs",
        "code_sources", "created_at", "updated_at",
    }
    attempts = payload.get("accepted_attempt_ids")
    assets = payload.get("accepted_code_asset_ids")
    refs = payload.get("accepted_code_refs")
    if (
        set(payload) != fields
        or payload.get("schema") != VERSION_SCHEMA_V2
        or payload.get("id") != expected_id
        or not isinstance(expected_id, str)
        or VERSION_ID.fullmatch(expected_id) is None
        or payload.get("status") != "active"
        or not _is_nonempty_text(payload.get("name"))
        or not _strict_fullmatch(VERSION_ID, payload.get("base_version_id"))
        or not _strict_fullmatch(re.compile(r"TPL-[0-9]{4}"), payload.get("template_id"))
        or not _strict_id_list(attempts, re.compile(r"ATTEMPT-[0-9]{4}"))
        or not _strict_id_list(assets, re.compile(r"CODE-[0-9]{4}"))
        or not isinstance(refs, list)
        or len(refs) != len(assets)
        or not _is_utc_iso(payload.get("created_at"))
        or not _is_utc_iso(payload.get("updated_at"))
    ):
        raise ValueError(f"Version v2 记录无效：{expected_id}")
    _validate_code_sources(payload.get("code_sources"), expected_id, require_single=True)
    for asset_id, item in zip(assets, refs):
        if (
            not isinstance(item, dict)
            or set(item) != {"code_asset_id", "external_code_ref"}
            or item.get("code_asset_id") != asset_id
        ):
            raise ValueError(f"Version v2 accepted_code_refs 无效：{expected_id}")
        _validate_version_external_ref(item.get("external_code_ref"), expected_id)


def _validate_code_sources(
    sources: object, expected_id: str, *, require_single: bool = False,
) -> None:
    if (
        not isinstance(sources, list)
        or not sources
        or (require_single and len(sources) != 1)
    ):
        raise ValueError(f"Version code_sources 不完整：{expected_id}")
    for source in sources:
        if (
            not isinstance(source, dict)
            or set(source) != {"repo_url", "commit", "relative_path"}
            or not _is_nonempty_text(source.get("repo_url"))
            or not isinstance(source.get("commit"), str)
            or re.fullmatch(r"[0-9a-f]{40}", source["commit"]) is None
        ):
            raise ValueError(f"Version code_sources 不完整：{expected_id}")
        normalized = normalize_safe_relative_path(
            source.get("relative_path"), "Version relative_path",
        )
        if source["relative_path"] != normalized:
            raise ValueError(f"Version relative_path 未规范化：{expected_id}")


def _validate_version_external_ref(value: object, expected_id: str) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"repo_url", "commit", "relative_path", "digest"}
        or not _is_nonempty_text(value.get("repo_url"))
        or not isinstance(value.get("commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}", value["commit"]) is None
        or not isinstance(value.get("digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value["digest"]) is None
    ):
        raise ValueError(f"Version v2 external_code_ref 无效：{expected_id}")
    normalized = normalize_safe_relative_path(
        value.get("relative_path"), "Version external_code_ref.relative_path",
    )
    if value["relative_path"] != normalized:
        raise ValueError(f"Version v2 external_code_ref 路径未规范化：{expected_id}")


def _strict_id_list(value: object, pattern: re.Pattern[str]) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_strict_fullmatch(pattern, item) for item in value)
        and len(value) == len(set(value))
    )


def _strict_fullmatch(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def normalize_safe_relative_path(value: object, label: str = "relative path") -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} 必须是安全的非空相对路径")
    normalized_separators = value.replace("\\", "/")
    if normalized_separators == ".":
        return "."
    segments = normalized_separators.split("/")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        any(segment in {"", ".", ".."} for segment in segments)
        or posix.is_absolute() or bool(posix.anchor)
        or windows.is_absolute() or bool(windows.drive) or bool(windows.root)
        or bool(windows.anchor)
    ):
        raise ValueError(f"{label} 必须是安全的项目内相对路径")
    return "/".join(segments)


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} 不能为空")
    return stripped


def _is_nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_utc_iso(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
