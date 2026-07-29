from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .io import (
    DEFAULT_JSON_LIMIT,
    atomic_create_json,
    read_bounded_json_object,
    read_bounded_regular_file,
)
from .locking import project_write_lock
from .project import PROJECT_SCHEMA_V2, is_link_or_reparse
from .records import (
    _initialized_project,
    _next_id,
    _preflight_workflow_command_locked,
    _validate_project,
    normalize_safe_relative_path,
)


SOURCE_SCHEMA = "cv-experiment-workflow.source.v2"
LEGACY_IDEA_SCHEMA = "cv-experiment-workflow.idea.v2"
IDEA_SCHEMA = "cv-experiment-workflow.catalog-idea.v1"
MODULE_SCHEMA = "cv-experiment-workflow.module.v2"
SOURCE_ID = re.compile(r"SRC-[0-9]{4}")
IDEA_ID = re.compile(r"IDEA-[0-9]{4}")
MODULE_ID = re.compile(r"MOD-[0-9]{4}")
SOURCE_NAME = re.compile(r"SRC-[0-9]{4}\.json")
IDEA_NAME = re.compile(r"IDEA-[0-9]{4}\.json")
MODULE_NAME = re.compile(r"MOD-[0-9]{4}\.json")
TEMPLATE_ID = re.compile(r"TPL-[0-9]{4}")
COMMIT = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MAX_MANIFEST_SIZE = 256 * 1024
MAX_TEXT = 4096
MAX_REFS = 64
MAX_TESTS = 64
MAX_SOURCE_FILE_SIZE = 512 * 1024 * 1024
MAX_SNAPSHOT_FILES = 64
MAX_SNAPSHOT_FILE_SIZE = 16 * 1024 * 1024
MAX_SNAPSHOT_TOTAL_SIZE = 256 * 1024 * 1024
MAX_COMMIT_OBJECT_SIZE = 4 * 1024 * 1024
MAX_MODULE_FILE_SIZE = 16 * 1024 * 1024
SOURCE_BASE_FIELDS = {
    "schema", "id", "kind", "identity", "locator", "revision", "commit",
    "digest", "license",
}
SOURCE_SNAPSHOT_FIELDS = SOURCE_BASE_FIELDS | {"files"}
SOURCE_MANIFEST_BASE_FIELDS = SOURCE_BASE_FIELDS - {"id"}
SOURCE_MANIFEST_SNAPSHOT_FIELDS = SOURCE_SNAPSHOT_FIELDS - {"id"}
LEGACY_IDEA_FIELDS = {
    "schema", "id", "status", "problem", "mechanism",
    "falsifiable_hypothesis", "source_refs", "revision", "evidence_refs",
}
LEGACY_IDEA_MANIFEST_FIELDS = LEGACY_IDEA_FIELDS - {"id"}
IDEA_FIELDS = {
    "schema", "id", "status", "problem", "mechanism",
    "falsifiable_hypothesis", "source_refs", "revision", "evidence_refs",
    "source_links", "parent_idea_ref", "revision_reason",
}
IDEA_MANIFEST_FIELDS = IDEA_FIELDS - {
    "id", "revision", "parent_idea_ref", "revision_reason",
}
MODULE_FIELDS = {
    "schema", "id", "status", "kind", "intent", "idea_refs", "source_refs",
    "attachment", "entry", "contract", "toggle", "disabled_behavior",
    "code_ref", "tests", "validation",
}
MODULE_MANIFEST_FIELDS = MODULE_FIELDS - {"id", "validation"}
SOURCE_KINDS = {"code", "paper", "code_snapshot"}
IDEA_STATUSES = {"draft", "ready", "rejected"}
MODULE_STATUSES = {"draft", "ready", "disabled"}
MODULE_KINDS = {"research", "infrastructure"}


def register_source(
    project: Path, manifest: Path, source_path: Path,
) -> dict[str, Any]:
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _preflight_workflow_command_locked(control)
        facts = _normalize_source_manifest(_read_manifest(manifest, "Source"))
        project_real = _real_directory(
            Path(root).resolve(strict=True), "project-root",
        )
        candidate = Path(source_path).expanduser().absolute()
        if facts["kind"] == "paper":
            source_file = _real_regular_file(candidate, "paper source-path")
            source_file = source_file.resolve(strict=True)
            if facts["locator"] != str(source_file):
                raise ValueError(
                    "paper Source locator 必须精确等于 source-path 的真实绝对路径"
                )
            content = read_bounded_regular_file(
                source_file,
                MAX_SOURCE_FILE_SIZE,
                "paper Source",
            )
            digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
            if facts["digest"] != digest:
                raise ValueError("paper Source digest 与本地文件真实 SHA256 不一致")
        elif facts["kind"] == "code":
            source_root = _real_directory(
                candidate, "code source-path",
            ).resolve(strict=True)
            git_top = _real_directory(
                Path(_git(source_root, "rev-parse", "--show-toplevel")).resolve(
                    strict=True,
                ),
                "code Source Git 顶层",
            )
            if source_root != git_top:
                raise ValueError("code Source source-path 必须是干净 Git 顶层")
            if facts["locator"] != str(source_root):
                raise ValueError(
                    "code Source locator 必须精确等于 source-path 的真实绝对路径"
                )
            head = _git(source_root, "rev-parse", "HEAD").lower()
            if head != facts["commit"]:
                raise ValueError("code Source HEAD 与 manifest commit 不一致")
            if _code_status(source_root, project_real):
                raise ValueError("code Source Git 工作树必须干净")
            digest = _code_source_digest(source_root, head)
            if digest != facts["digest"]:
                raise ValueError(
                    "code Source digest 与 Git commit 对象真实 SHA256 不一致"
                )
            if _code_status(source_root, project_real):
                raise ValueError("code Source Git 工作树必须干净")
            final_head = _git(source_root, "rev-parse", "HEAD").lower()
            if final_head != head or final_head != facts["commit"]:
                raise ValueError("code Source HEAD 在核验期间发生变化，拒绝登记")
        else:
            source_root = _real_directory(
                candidate, "code_snapshot source-path",
            ).resolve(strict=True)
            if facts["locator"] != str(source_root):
                raise ValueError(
                    "code_snapshot Source locator 必须精确等于 "
                    "source-path 的真实绝对路径"
                )
            _verify_code_snapshot(source_root, facts)
        directory = _real_directory(control / "sources", "sources 目录")
        source_id = _next_id(directory, "SRC", SOURCE_ID)
        payload = {"schema": SOURCE_SCHEMA, "id": source_id, **facts}
        validate_source(payload, expected_id=source_id)
        if facts["kind"] == "code_snapshot":
            _verify_code_snapshot(source_root, facts)
        _atomic_create(directory / f"{source_id}.json", payload, "Source")
        return payload


def save_idea(project: Path, manifest: Path) -> dict[str, Any]:
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _preflight_workflow_command_locked(control)
        sources = validate_sources_locked(control)
        facts = _normalize_idea_manifest(_read_manifest(manifest, "Idea"))
        directory = _real_directory(control / "ideas", "ideas 目录")
        idea_id = _next_id(directory, "IDEA", IDEA_ID)
        payload = {
            "schema": IDEA_SCHEMA,
            "id": idea_id,
            **facts,
            "revision": 1,
            "parent_idea_ref": None,
            "revision_reason": None,
        }
        validate_idea(
            payload,
            expected_id=idea_id,
            sources=sources,
            control=control,
        )
        _atomic_create(directory / f"{idea_id}.json", payload, "Idea")
        return payload


def revise_catalog_idea(
    project: Path,
    idea_id: str,
    manifest: Path,
    reason: str,
) -> dict[str, Any]:
    """以新 ID 保存 Idea 修订，绝不覆盖父 Idea。"""
    if IDEA_ID.fullmatch(idea_id) is None:
        raise ValueError("父 Idea ID 无效")
    revision_reason = _text(reason, "Idea revision_reason")
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _preflight_workflow_command_locked(control)
        sources = validate_sources_locked(control)
        ideas = validate_ideas_locked(control, sources)
        parent = ideas.get(idea_id)
        if parent is None:
            raise ValueError(f"父 Idea 不存在：{idea_id}")
        if parent["revision"] >= 9999:
            raise ValueError("父 Idea revision 已达到 9999，不能继续修订")
        facts = _normalize_idea_manifest(_read_manifest(manifest, "Idea"))
        if (
            parent.get("schema") == IDEA_SCHEMA
            and all(
                parent.get(key) == facts[key]
                for key in IDEA_MANIFEST_FIELDS - {"schema"}
            )
        ):
            raise ValueError("Idea 科学内容没有实际变化，拒绝创建空修订")
        directory = _real_directory(control / "ideas", "ideas 目录")
        new_idea_id = _next_id(directory, "IDEA", IDEA_ID)
        payload = {
            "schema": IDEA_SCHEMA,
            "id": new_idea_id,
            **facts,
            "revision": parent["revision"] + 1,
            "parent_idea_ref": idea_id,
            "revision_reason": revision_reason,
        }
        validate_idea(
            payload,
            expected_id=new_idea_id,
            sources=sources,
            control=control,
            ideas=ideas,
        )
        _atomic_create(
            directory / f"{new_idea_id}.json",
            payload,
            "Idea revision",
        )
        return payload


def register_module(
    project: Path, manifest: Path, code_root: Path,
) -> dict[str, Any]:
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _preflight_workflow_command_locked(control)
        sources = validate_sources_locked(control)
        ideas = validate_ideas_locked(control, sources)
        from .project_templates import load_project_templates_locked

        templates = load_project_templates_locked(control, sources=sources)
        facts = _normalize_module_manifest(_read_manifest(manifest, "Module"))
        if facts["status"] == "ready":
            validation = _verify_ready_module(root, facts, code_root)
        else:
            validation = {
                "verified": False,
                "git_head": None,
                "files": [],
            }
        directory = _real_directory(control / "modules", "modules 目录")
        module_id = _next_id(directory, "MOD", MODULE_ID)
        payload = {
            "schema": MODULE_SCHEMA,
            "id": module_id,
            **facts,
            "validation": validation,
        }
        validate_module(
            payload,
            expected_id=module_id,
            sources=sources,
            ideas=ideas,
            templates=templates,
        )
        _atomic_create(directory / f"{module_id}.json", payload, "Module")
        return payload


def validate_sources_locked(control: Path) -> dict[str, dict[str, Any]]:
    directory = _real_directory(Path(control) / "sources", "sources 目录")
    records: dict[str, dict[str, Any]] = {}
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if SOURCE_NAME.fullmatch(entry.name) is None:
            raise ValueError(f"sources 目录存在未知文件或目录：{entry}")
        payload = _read_record(entry, "Source")
        validate_source(payload, expected_id=entry.stem)
        if payload["kind"] == "code_snapshot":
            source_root = _real_directory(
                Path(payload["locator"]).expanduser().absolute(),
                f"code_snapshot Source {entry.stem}",
            ).resolve(strict=True)
            if str(source_root) != payload["locator"]:
                raise ValueError(
                    f"code_snapshot Source locator 不是规范真实绝对路径：{entry.stem}"
                )
            _verify_code_snapshot(source_root, payload)
        records[entry.stem] = payload
    return records


def validate_ideas_locked(
    control: Path,
    sources: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    directory = _real_directory(Path(control) / "ideas", "ideas 目录")
    records: dict[str, dict[str, Any]] = {}
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if IDEA_NAME.fullmatch(entry.name) is None:
            raise ValueError(f"ideas 目录存在未知文件或目录：{entry}")
        payload = _read_record(entry, "Idea")
        validate_idea(
            payload,
            expected_id=entry.stem,
            sources=sources,
            control=control,
            ideas=records,
        )
        records[entry.stem] = payload
    return records


def validate_modules_locked(
    control: Path,
    sources: dict[str, dict[str, Any]],
    ideas: dict[str, dict[str, Any]],
    templates: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    directory = _real_directory(Path(control) / "modules", "modules 目录")
    records: dict[str, dict[str, Any]] = {}
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if MODULE_NAME.fullmatch(entry.name) is None:
            raise ValueError(f"modules 目录存在未知文件或目录：{entry}")
        payload = _read_record(entry, "Module")
        validate_module(
            payload,
            expected_id=entry.stem,
            sources=sources,
            ideas=ideas,
            templates=templates,
        )
        records[entry.stem] = payload
    return records


def validate_source(payload: dict[str, Any], *, expected_id: str) -> None:
    expected_fields = (
        SOURCE_SNAPSHOT_FIELDS
        if isinstance(payload, dict) and payload.get("kind") == "code_snapshot"
        else SOURCE_BASE_FIELDS
    )
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_fields
        or payload.get("schema") != SOURCE_SCHEMA
        or payload.get("id") != expected_id
        or SOURCE_ID.fullmatch(expected_id) is None
    ):
        raise ValueError(f"Source 记录字段、schema 或 ID 无效：{expected_id}")
    manifest_fields = (
        SOURCE_MANIFEST_SNAPSHOT_FIELDS
        if payload["kind"] == "code_snapshot"
        else SOURCE_MANIFEST_BASE_FIELDS
    )
    normalized = _normalize_source_manifest(
        {key: payload[key] for key in manifest_fields}
    )
    expected = {"schema": SOURCE_SCHEMA, "id": expected_id, **normalized}
    if payload != expected:
        raise ValueError(f"Source 规范化事实发生漂移：{expected_id}")


def validate_idea(
    payload: dict[str, Any],
    *,
    expected_id: str,
    sources: dict[str, dict[str, Any]],
    control: Path | None = None,
    ideas: dict[str, dict[str, Any]] | None = None,
) -> None:
    if isinstance(payload, dict) and payload.get("schema") == LEGACY_IDEA_SCHEMA:
        _validate_legacy_idea(payload, expected_id=expected_id, sources=sources)
        return
    if (
        not isinstance(payload, dict)
        or set(payload) != IDEA_FIELDS
        or payload.get("schema") != IDEA_SCHEMA
        or payload.get("id") != expected_id
        or IDEA_ID.fullmatch(expected_id) is None
    ):
        raise ValueError(f"Idea 记录字段、schema 或 ID 无效：{expected_id}")
    normalized = _normalize_idea_manifest({
        key: payload[key] for key in IDEA_MANIFEST_FIELDS
    })
    revision = payload.get("revision")
    if type(revision) is not int or not 1 <= revision <= 9999:
        raise ValueError("Idea revision 必须是 1..9999 的整数")
    parent_ref = payload.get("parent_idea_ref")
    revision_reason = payload.get("revision_reason")
    if revision == 1:
        if parent_ref is not None or revision_reason is not None:
            raise ValueError("根 Idea 的 parent_idea_ref 和 revision_reason 必须为 null")
    else:
        if (
            not isinstance(parent_ref, str)
            or IDEA_ID.fullmatch(parent_ref) is None
            or not isinstance(revision_reason, str)
        ):
            raise ValueError("修订 Idea 必须提供有效父 Idea 和修订原因")
        revision_reason = _text(revision_reason, "Idea revision_reason")
        if ideas is not None:
            parent = ideas.get(parent_ref)
            if parent is None:
                raise ValueError(f"Idea parent_idea_ref 不存在：{parent_ref}")
            if parent["revision"] + 1 != revision:
                raise ValueError("Idea revision 必须精确等于父 Idea revision + 1")
    expected = {
        "schema": IDEA_SCHEMA,
        "id": expected_id,
        **normalized,
        "revision": revision,
        "parent_idea_ref": parent_ref,
        "revision_reason": revision_reason,
    }
    if payload != expected:
        raise ValueError(f"Idea 规范化事实发生漂移：{expected_id}")
    missing = [
        source_id
        for source_id in payload["source_refs"]
        if source_id not in sources
    ]
    if missing:
        raise ValueError(f"Idea source_refs 引用了不存在的 Source：{missing[0]}")
    for link in payload["source_links"]:
        if link["source_ref"] not in sources:
            raise ValueError(
                f"Idea source_links 引用了不存在的 Source：{link['source_ref']}"
            )
        if link["source_ref"] not in payload["source_refs"]:
            raise ValueError("Idea source_links 必须属于 source_refs")
    linked_refs = {link["source_ref"] for link in payload["source_links"]}
    if linked_refs != set(payload["source_refs"]):
        raise ValueError("每个 Idea source_ref 都必须至少有一条 source_link")
    _validate_idea_evidence_refs(
        payload["evidence_refs"],
        sources=sources,
        control=control,
    )


def _validate_legacy_idea(
    payload: dict[str, Any],
    *,
    expected_id: str,
    sources: dict[str, dict[str, Any]],
) -> None:
    if (
        set(payload) != LEGACY_IDEA_FIELDS
        or payload.get("id") != expected_id
        or IDEA_ID.fullmatch(expected_id) is None
    ):
        raise ValueError(f"旧版 Idea 记录字段、schema 或 ID 无效：{expected_id}")
    normalized = _normalize_legacy_idea_manifest({
        key: payload[key] for key in LEGACY_IDEA_MANIFEST_FIELDS
    })
    expected = {
        "schema": LEGACY_IDEA_SCHEMA,
        "id": expected_id,
        **normalized,
    }
    if payload != expected:
        raise ValueError(f"旧版 Idea 规范化事实发生漂移：{expected_id}")
    missing = [
        source_id
        for source_id in payload["source_refs"]
        if source_id not in sources
    ]
    if missing:
        raise ValueError(f"Idea source_refs 引用了不存在的 Source：{missing[0]}")


def validate_module(
    payload: dict[str, Any],
    *,
    expected_id: str,
    sources: dict[str, dict[str, Any]],
    ideas: dict[str, dict[str, Any]],
    templates: dict[str, dict[str, Any]],
) -> None:
    if (
        not isinstance(payload, dict)
        or set(payload) != MODULE_FIELDS
        or payload.get("schema") != MODULE_SCHEMA
        or payload.get("id") != expected_id
        or MODULE_ID.fullmatch(expected_id) is None
    ):
        raise ValueError(f"Module 记录字段、schema 或 ID 无效：{expected_id}")
    normalized = _normalize_module_manifest(
        {key: payload[key] for key in MODULE_MANIFEST_FIELDS}
    )
    validation = _normalize_module_validation(
        payload.get("validation"),
        status=normalized["status"],
        code_ref=normalized["code_ref"],
        entry=normalized["entry"],
        tests=normalized["tests"],
    )
    expected = {
        "schema": MODULE_SCHEMA,
        "id": expected_id,
        **normalized,
        "validation": validation,
    }
    if payload != expected:
        raise ValueError(f"Module 规范化事实发生漂移：{expected_id}")
    for source_id in payload["source_refs"]:
        if source_id not in sources:
            raise ValueError(f"Module source_refs 引用了不存在的 Source：{source_id}")
    for idea_id in payload["idea_refs"]:
        if idea_id not in ideas:
            raise ValueError(f"Module idea_refs 引用了不存在的 Idea：{idea_id}")
    code_commit = payload["code_ref"]["commit"]
    if not any(
        sources[source_id]["kind"] == "code"
        and sources[source_id]["commit"] == code_commit
        for source_id in payload["source_refs"]
    ):
        raise ValueError(
            "Module code_ref.commit 必须匹配 source_refs 中至少一个 code Source"
        )
    if payload["kind"] == "research" and not any(
        ideas[idea_id]["status"] == "ready" for idea_id in payload["idea_refs"]
    ):
        raise ValueError("research Module 至少需要一个 ready Idea")
    template_ref = payload["attachment"]["template_ref"]
    template = templates.get(template_ref)
    if template is None:
        raise ValueError(
            f"Module attachment 引用了不存在的 Template：{template_ref}"
        )
    point = payload["attachment"]["point"]
    if point not in {item["name"] for item in template["attachment_points"]}:
        raise ValueError(
            f"Module attachment 未由 Template 声明：{template_ref}:{point}"
        )


def _normalize_source_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    kind = payload.get("kind") if isinstance(payload, dict) else None
    expected_fields = (
        SOURCE_MANIFEST_SNAPSHOT_FIELDS
        if kind == "code_snapshot"
        else SOURCE_MANIFEST_BASE_FIELDS
    )
    _exact_fields(payload, expected_fields, "Source manifest")
    if payload.get("schema") != SOURCE_SCHEMA:
        raise ValueError("Source manifest schema 无效")
    if kind not in SOURCE_KINDS:
        raise ValueError("Source kind 必须是 code、paper 或 code_snapshot")
    commit_value = payload.get("commit")
    if kind == "code":
        if not isinstance(commit_value, str) or COMMIT.fullmatch(commit_value) is None:
            raise ValueError("code Source commit 必须是精确 40 位小写十六进制")
        commit: str | None = commit_value
    else:
        if commit_value is not None:
            raise ValueError(f"{kind} Source commit 必须为 null")
        commit = None
    digest = payload.get("digest")
    if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
        raise ValueError("Source digest 必须是 sha256: 加 64 位小写十六进制")
    normalized: dict[str, Any] = {
        "kind": kind,
        "identity": _text(payload.get("identity"), "Source identity"),
        "locator": _text(payload.get("locator"), "Source locator"),
        "revision": _text(payload.get("revision"), "Source revision"),
        "commit": commit,
        "digest": digest,
        "license": _text(payload.get("license"), "Source license"),
    }
    if kind == "code_snapshot":
        normalized["files"] = _normalize_snapshot_files(payload.get("files"))
    return normalized


def _normalize_idea_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(payload, IDEA_MANIFEST_FIELDS, "Idea manifest")
    if payload.get("schema") != IDEA_SCHEMA:
        raise ValueError("Idea manifest schema 无效")
    status = payload.get("status")
    if status not in IDEA_STATUSES:
        raise ValueError("Idea status 无效")
    return {
        "status": status,
        "problem": _text(payload.get("problem"), "Idea problem"),
        "mechanism": _text(payload.get("mechanism"), "Idea mechanism"),
        "falsifiable_hypothesis": _text(
            payload.get("falsifiable_hypothesis"),
            "Idea falsifiable_hypothesis",
        ),
        "source_refs": _id_list(
            payload.get("source_refs"), SOURCE_ID, "Idea source_refs", minimum=1,
        ),
        "evidence_refs": _text_list(
            payload.get("evidence_refs"), "Idea evidence_refs",
        ),
        "source_links": _normalize_source_links(payload.get("source_links")),
    }


def _normalize_legacy_idea_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(payload, LEGACY_IDEA_MANIFEST_FIELDS, "旧版 Idea manifest")
    if payload.get("schema") != LEGACY_IDEA_SCHEMA:
        raise ValueError("旧版 Idea manifest schema 无效")
    status = payload.get("status")
    if status not in IDEA_STATUSES:
        raise ValueError("旧版 Idea status 无效")
    revision = payload.get("revision")
    if type(revision) is not int or not 1 <= revision <= 9999:
        raise ValueError("旧版 Idea revision 必须是 1..9999 的整数")
    return {
        "status": status,
        "problem": _text(payload.get("problem"), "Idea problem"),
        "mechanism": _text(payload.get("mechanism"), "Idea mechanism"),
        "falsifiable_hypothesis": _text(
            payload.get("falsifiable_hypothesis"),
            "Idea falsifiable_hypothesis",
        ),
        "source_refs": _id_list(
            payload.get("source_refs"),
            SOURCE_ID,
            "Idea source_refs",
            minimum=1,
        ),
        "revision": revision,
        "evidence_refs": _text_list(
            payload.get("evidence_refs"),
            "Idea evidence_refs",
        ),
    }


def _normalize_source_links(value: object) -> list[dict[str, str]]:
    items = _bounded_list(value, "Idea source_links", minimum=1)
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in items:
        link = _exact_fields(
            item,
            {"source_ref", "locator", "supports_field", "claim"},
            "Idea source_link",
        )
        source_ref = link.get("source_ref")
        if not isinstance(source_ref, str) or SOURCE_ID.fullmatch(source_ref) is None:
            raise ValueError("Idea source_link.source_ref 无效")
        supports_field = link.get("supports_field")
        if supports_field not in {
            "problem", "mechanism", "falsifiable_hypothesis",
        }:
            raise ValueError("Idea source_link.supports_field 无效")
        normalized_link = {
            "source_ref": source_ref,
            "locator": _text(link.get("locator"), "Idea source_link.locator"),
            "supports_field": supports_field,
            "claim": _text(link.get("claim"), "Idea source_link.claim"),
        }
        identity = tuple(normalized_link.values())
        if identity in seen:
            raise ValueError("Idea source_links 不得重复")
        seen.add(identity)
        normalized.append(normalized_link)
    return normalized


def _validate_idea_evidence_refs(
    refs: list[str],
    *,
    sources: dict[str, dict[str, Any]],
    control: Path | None,
) -> None:
    for ref in refs:
        if ref in sources:
            continue
        if re.fullmatch(r"RUN-[0-9]{4}", ref) is None:
            raise ValueError(f"Idea evidence_refs 不是 Source 或 Run：{ref}")
        if control is None:
            raise ValueError(f"Idea evidence_refs 无法核验 Run：{ref}")
        run_path = Path(control) / "runs" / ref / "run.json"
        if is_link_or_reparse(run_path) or not run_path.is_file():
            raise ValueError(f"Idea evidence_refs 引用了不存在的 Run：{ref}")
        from .runs import validate_run

        run = read_bounded_json_object(run_path, label=f"Idea evidence Run {ref}")
        validate_run(run, expected_id=ref)
        if run["execution"]["stage"] != "closed":
            raise ValueError(f"Idea evidence_refs 只能引用已关闭 Run：{ref}")


def _normalize_module_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(payload, MODULE_MANIFEST_FIELDS, "Module manifest")
    if payload.get("schema") != MODULE_SCHEMA:
        raise ValueError("Module manifest schema 无效")
    status = payload.get("status")
    kind = payload.get("kind")
    if status not in MODULE_STATUSES:
        raise ValueError("Module status 无效")
    if kind not in MODULE_KINDS:
        raise ValueError("Module kind 无效")
    idea_refs = _id_list(
        payload.get("idea_refs"),
        IDEA_ID,
        "Module idea_refs",
        minimum=1 if kind == "research" else 0,
    )
    tests = _path_list(payload.get("tests"), "Module tests", minimum=1)
    attachment = _exact_fields(
        payload.get("attachment"),
        {"template_ref", "point"},
        "Module attachment",
    )
    template_ref = attachment.get("template_ref")
    if (
        not isinstance(template_ref, str)
        or TEMPLATE_ID.fullmatch(template_ref) is None
    ):
        raise ValueError("Module attachment.template_ref 无效")
    contract = _exact_fields(
        payload.get("contract"),
        {"input", "output"},
        "Module contract",
    )
    toggle = _exact_fields(
        payload.get("toggle"),
        {"config_key", "enabled_value", "disabled_value"},
        "Module toggle",
    )
    if (
        toggle.get("enabled_value") is not True
        or toggle.get("disabled_value") is not False
    ):
        raise ValueError(
            "Module toggle 本轮只接受 enabled_value=true、disabled_value=false"
        )
    code_ref = _exact_fields(
        payload.get("code_ref"),
        {"commit", "path"},
        "Module code_ref",
    )
    code_commit = code_ref.get("commit")
    if not isinstance(code_commit, str) or COMMIT.fullmatch(code_commit) is None:
        raise ValueError(
            "Module code_ref.commit 必须是精确 40 位小写十六进制"
        )
    return {
        "status": status,
        "kind": kind,
        "intent": _text(payload.get("intent"), "Module intent"),
        "idea_refs": idea_refs,
        "source_refs": _id_list(
            payload.get("source_refs"), SOURCE_ID, "Module source_refs", minimum=1,
        ),
        "attachment": {
            "template_ref": template_ref,
            "point": _text(
                attachment.get("point"), "Module attachment.point",
            ),
        },
        "entry": _bounded_relative_path(payload.get("entry"), "Module entry"),
        "contract": {
            "input": _text(contract.get("input"), "Module contract.input"),
            "output": _text(contract.get("output"), "Module contract.output"),
        },
        "toggle": {
            "config_key": _text(
                toggle.get("config_key"), "Module toggle.config_key",
            ),
            "enabled_value": True,
            "disabled_value": False,
        },
        "disabled_behavior": _text(
            payload.get("disabled_behavior"), "Module disabled_behavior",
        ),
        "code_ref": {
            "commit": code_commit,
            "path": _bounded_relative_path(
                code_ref.get("path"), "Module code_ref.path",
            ),
        },
        "tests": tests,
    }


def _normalize_module_validation(
    value: object,
    *,
    status: str,
    code_ref: dict[str, str],
    entry: str,
    tests: list[str],
) -> dict[str, Any]:
    validation = _exact_fields(
        value, {"verified", "git_head", "files"}, "Module validation",
    )
    if status != "ready":
        if validation != {
            "verified": False,
            "git_head": None,
            "files": [],
        }:
            raise ValueError(
                "非 ready Module validation 必须明确为未核验"
            )
        return {
            "verified": False,
            "git_head": None,
            "files": [],
        }
    if (
        validation.get("verified") is not True
        or validation.get("git_head") != code_ref["commit"]
    ):
        raise ValueError("ready Module validation 必须绑定 code_ref.commit")
    expected_paths = list(dict.fromkeys([entry, *tests]))
    files = validation.get("files")
    if not isinstance(files, list) or len(files) != len(expected_paths):
        raise ValueError("ready Module validation.files 不完整")
    normalized_files: list[dict[str, str]] = []
    for expected_path, item in zip(expected_paths, files):
        row = _exact_fields(
            item, {"path", "digest"}, "Module validation.files item",
        )
        path = _bounded_relative_path(
            row.get("path"), "Module validation.files.path",
        )
        digest = row.get("digest")
        if (
            path != expected_path
            or not isinstance(digest, str)
            or DIGEST.fullmatch(digest) is None
        ):
            raise ValueError("ready Module validation.files 无效")
        normalized_files.append({"path": path, "digest": digest})
    return {
        "verified": True,
        "git_head": code_ref["commit"],
        "files": normalized_files,
    }


def _verify_ready_module(
    project_root: Path,
    facts: dict[str, Any],
    code_root: Path,
) -> dict[str, Any]:
    project_real = _real_directory(
        Path(project_root).resolve(strict=True), "project-root",
    )
    source_root = _real_directory(
        Path(code_root).expanduser().absolute(), "code-root",
    ).resolve(strict=True)
    git_top = _real_directory(
        Path(_git(source_root, "rev-parse", "--show-toplevel")).resolve(
            strict=True,
        ),
        "Module Git 顶层",
    )
    if source_root != git_top or source_root != project_real:
        raise ValueError(
            "ready Module code-root 必须是 project-root 的真实 Git 顶层"
        )
    if facts["entry"] != facts["code_ref"]["path"]:
        raise ValueError("ready Module entry 必须等于 code_ref.path")
    head = _git(source_root, "rev-parse", "HEAD").lower()
    if head != facts["code_ref"]["commit"]:
        raise ValueError("ready Module HEAD 必须等于 code_ref.commit")
    if _code_status(source_root, project_real):
        raise ValueError("ready Module 项目代码工作树必须干净")
    paths = list(dict.fromkeys([facts["entry"], *facts["tests"]]))
    files = [
        {
            "path": path,
            "digest": _module_file_digest(source_root, head, path),
        }
        for path in paths
    ]
    if _code_status(source_root, project_real):
        raise ValueError("ready Module 项目代码工作树必须干净")
    final_head = _git(source_root, "rev-parse", "HEAD").lower()
    if final_head != head or final_head != facts["code_ref"]["commit"]:
        raise ValueError("ready Module HEAD 在核验期间发生变化，拒绝登记")
    return {"verified": True, "git_head": head, "files": files}


def _normalize_snapshot_files(value: object) -> list[dict[str, str]]:
    items = _bounded_list(
        value,
        "code_snapshot Source files",
        minimum=1,
        maximum=MAX_SNAPSHOT_FILES,
    )
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        row = _exact_fields(
            item, {"path", "digest"}, "code_snapshot Source files item",
        )
        path = _bounded_relative_path(
            row.get("path"), "code_snapshot Source files.path",
        )
        if path == ".":
            raise ValueError("code_snapshot Source files.path 必须指向文件")
        if path in seen:
            raise ValueError(f"code_snapshot Source files.path 重复：{path}")
        seen.add(path)
        digest = row.get("digest")
        if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
            raise ValueError(
                f"code_snapshot Source files.digest 无效：{path}"
            )
        normalized.append({"path": path, "digest": digest})
    return sorted(normalized, key=lambda item: item["path"])


def _snapshot_aggregate_digest(files: list[dict[str, str]]) -> str:
    canonical = json.dumps(
        files,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _verify_code_snapshot(root: Path, facts: dict[str, Any]) -> None:
    total_size = 0
    verified: list[dict[str, str]] = []
    for item in facts["files"]:
        relative = item["path"]
        segments = relative.split("/")
        parent = root
        for segment in segments[:-1]:
            parent = _real_directory(
                parent / segment,
                f"code_snapshot files.path 父目录 {relative}",
            )
        path = _real_regular_file(
            parent / segments[-1],
            f"code_snapshot files.path {relative}",
        )
        resolved = path.resolve(strict=True)
        if root not in resolved.parents:
            raise ValueError(
                f"code_snapshot files.path 逃出来源目录：{relative}"
            )
        content = read_bounded_regular_file(
            path,
            MAX_SNAPSHOT_FILE_SIZE,
            f"code_snapshot file {relative}",
        )
        total_size += len(content)
        if total_size > MAX_SNAPSHOT_TOTAL_SIZE:
            raise ValueError("code_snapshot Source 文件总大小超过限制")
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if digest != item["digest"]:
            raise ValueError(
                f"code_snapshot Source 文件 digest 不一致：{relative}"
            )
        verified.append({"path": relative, "digest": digest})
    aggregate = _snapshot_aggregate_digest(verified)
    if aggregate != facts["digest"]:
        raise ValueError("code_snapshot Source 聚合 digest 不一致")


def _read_manifest(path: Path, label: str) -> dict[str, Any]:
    try:
        return read_bounded_json_object(
            Path(path), MAX_MANIFEST_SIZE, f"{label} manifest",
        )
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取严格 {label} manifest：{path}；{error}") from error


def _read_record(path: Path, label: str) -> dict[str, Any]:
    try:
        return read_bounded_json_object(path, DEFAULT_JSON_LIMIT, f"{label} record")
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取 {label} JSON：{path}；{error}") from error


def _atomic_create(path: Path, payload: dict[str, Any], label: str) -> None:
    if not atomic_create_json(path, payload, transaction_id=uuid.uuid4().hex):
        raise FileExistsError(f"{label} 已存在，拒绝覆盖：{path}")


def _real_directory(path: Path, label: str) -> Path:
    if not os.path.lexists(path) or is_link_or_reparse(path) or not path.is_dir():
        raise ValueError(f"{label} 必须是普通目录且不得为链接/reparse：{path}")
    return path


def _real_regular_file(path: Path, label: str) -> Path:
    if (
        not os.path.lexists(path)
        or is_link_or_reparse(path)
        or not path.is_file()
    ):
        raise ValueError(f"{label} 必须是普通文件且不得为链接/reparse：{path}")
    return path


def _git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"无法执行 Git 校验：{root}；{error}") from error
    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"exit {result.returncode}"
        )
        raise ValueError(f"Git 校验失败：{detail}")
    return result.stdout.strip()


def _code_status(source_root: Path, project_root: Path) -> str:
    arguments = ["status", "--porcelain", "--untracked-files=all"]
    if source_root == project_root:
        arguments.extend(
            ["--", ".", ":(exclude).experiment-workflow/**"]
        )
    return _git(source_root, *arguments)


def _code_source_digest(root: Path, commit: str) -> str:
    size_text = _git(root, "cat-file", "-s", commit)
    try:
        size = int(size_text)
    except ValueError as error:
        raise ValueError("Git commit 对象大小无效") from error
    if not 0 <= size <= MAX_COMMIT_OBJECT_SIZE:
        raise ValueError("Git commit 对象超过大小限制")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "cat-file", "commit", commit],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"无法读取 Git commit 对象：{error}") from error
    if result.returncode != 0 or len(result.stdout) != size:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(
            f"读取 Git commit 对象失败：{detail or result.returncode}"
        )
    return f"sha256:{hashlib.sha256(result.stdout).hexdigest()}"


def _module_file_digest(root: Path, commit: str, relative: str) -> str:
    segments = relative.split("/")
    parent = root
    for segment in segments[:-1]:
        parent = _real_directory(
            parent / segment,
            f"Module 文件父目录 {relative}",
        )
    read_bounded_regular_file(
        _real_regular_file(
            parent / segments[-1], f"Module 工作区文件 {relative}",
        ),
        MAX_MODULE_FILE_SIZE,
        f"Module 工作区文件 {relative}",
    )
    _require_git_regular_blob(root, commit, relative, "Module")
    object_name = f"{commit}:{relative}"
    size_text = _git(root, "cat-file", "-s", object_name)
    try:
        size = int(size_text)
    except ValueError as error:
        raise ValueError(f"Module Git blob 大小无效：{relative}") from error
    if not 0 <= size <= MAX_MODULE_FILE_SIZE:
        raise ValueError(f"Module Git blob 超过大小限制：{relative}")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", object_name],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"无法读取 Module Git blob：{relative}；{error}") from error
    if result.returncode != 0 or len(result.stdout) != size:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(
            f"读取 Module Git blob 失败：{relative}；"
            f"{detail or result.returncode}"
        )
    return f"sha256:{hashlib.sha256(result.stdout).hexdigest()}"


def _require_git_regular_blob(
    root: Path, commit: str, relative: str, label: str,
) -> None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-tree", "-z", commit, "--", relative],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(
            f"无法检查 {label} Git 文件类型：{relative}；{error}"
        ) from error
    records = [record for record in result.stdout.split(b"\0") if record]
    if result.returncode != 0 or len(records) != 1 or b"\t" not in records[0]:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(
            f"{label} Git 路径无法唯一定位：{relative}；"
            f"{detail or result.returncode}"
        )
    metadata, raw_path = records[0].split(b"\t", 1)
    parts = metadata.split()
    try:
        expected_path = relative.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} Git 路径无法编码：{relative}") from error
    if (
        len(parts) != 3
        or parts[0] not in {b"100644", b"100755"}
        or parts[1] != b"blob"
        or raw_path != expected_path
    ):
        mode = parts[0].decode("ascii", errors="replace") if parts else "unknown"
        object_type = (
            parts[1].decode("ascii", errors="replace")
            if len(parts) > 1
            else "unknown"
        )
        raise ValueError(
            f"{label} Git 路径必须是 mode 100644/100755 且 type=blob："
            f"{relative}（实际 {mode} {object_type}）"
        )


def _exact_fields(
    payload: object, fields: set[str], label: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError(f"{label} 字段无效")
    return payload


def _text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > MAX_TEXT
    ):
        raise ValueError(f"{label} 必须是 1..{MAX_TEXT} 字符的规范非空字符串")
    return value


def _bounded_list(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_REFS,
) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{label} 数量必须在 {minimum}..{maximum} 之间")
    return value


def _id_list(
    value: object,
    pattern: re.Pattern[str],
    label: str,
    *,
    minimum: int,
) -> list[str]:
    items = _bounded_list(value, label, minimum=minimum)
    if (
        not all(isinstance(item, str) and pattern.fullmatch(item) for item in items)
        or len(items) != len(set(items))
    ):
        raise ValueError(f"{label} 必须是无重复的严格 ID 列表")
    return list(items)


def _text_list(value: object, label: str) -> list[str]:
    items = _bounded_list(value, label)
    normalized = [_text(item, label) for item in items]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} 不得重复")
    return normalized


def _path_list(
    value: object, label: str, *, minimum: int,
) -> list[str]:
    items = _bounded_list(value, label, minimum=minimum, maximum=MAX_TESTS)
    normalized = [_bounded_relative_path(item, label) for item in items]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} 不得重复")
    return normalized


def _bounded_relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise ValueError(f"{label} 必须是 1..4096 字符的安全相对路径")
    return normalize_safe_relative_path(value, label)
