from __future__ import annotations

import hashlib
import html
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .io import (
    atomic_create_bytes_clean,
    atomic_create_json,
    read_bounded_json_object,
    read_bounded_regular_file,
)
from .locking import project_write_lock
from .project import PROJECT_SCHEMA, PROJECT_SCHEMA_V2, is_link_or_reparse
from .records import (
    _initialized_project,
    _next_id,
    _preflight_workflow_command_locked,
    _read_object,
    _validate_project,
    normalize_safe_relative_path,
)
from .templates import COMPATIBLE_CODE_LICENSES


MANIFEST_SCHEMA = "cv-experiment-workflow.template-manifest.v1"
PROJECT_TEMPLATE_SCHEMA = "cv-experiment-workflow.project-template.v1"
MANIFEST_SCHEMA_V2 = "cv-experiment-workflow.template-manifest.v2"
PROJECT_TEMPLATE_SCHEMA_V2 = "cv-experiment-workflow.template.v2"
TEMPLATE_ID = re.compile(r"TPL-[0-9]{4}")
TEMPLATE_NAME = re.compile(r"TPL-[0-9]{4}\.json")
COMMIT = re.compile(r"[0-9a-fA-F]{40}")
DIGEST = re.compile(r"sha256:[0-9a-fA-F]{64}")
MANIFEST_LIMIT = 256 * 1024
LISTED_FILE_LIMIT = 4 * 1024 * 1024
MAX_LISTED_FILES = 64
MAX_PROVENANCE_SOURCES = 64
MAX_ATTACHMENT_POINTS = 64
MAX_FRAMEWORK_NODES = 128
MAX_FRAMEWORK_EDGES = 256
MAX_STRING_ITEMS = 256
INTERFACE_FIELDS = {
    "data", "model", "training", "evaluation", "config", "seed", "metrics", "checkpoint",
}
MANIFEST_FIELDS = {
    "schema", "name", "description", "code_source", "listed_files", "interfaces",
    "attachment_points", "provenance_sources", "framework",
}
PROJECT_TEMPLATE_FIELDS = (MANIFEST_FIELDS - {"schema"}) | {
    "schema", "template_id", "verification",
}
USE_MODES = {"copied", "adapted", "inspiration", "reimplementation"}
PROJECT_TEMPLATE_FIELDS_V2 = {
    "schema", "id", "name", "ownership", "source_refs", "code", "interfaces",
    "attachment_points", "baseline_recipe", "validation",
}
MANIFEST_FIELDS_V2 = PROJECT_TEMPLATE_FIELDS_V2 - {"id", "validation"}
CODE_FIELDS_V2 = {"source_ref", "template_path", "listed_files"}
BASELINE_RECIPE_FIELDS_V2 = {"entry", "config", "seed"}


def register_template(project: Path, manifest: Path, code_root: Path) -> dict[str, Any]:
    project_root = _initialized_project(project)
    schema = _read_object(
        project_root / ".experiment-workflow" / "project.json"
    ).get("schema")
    if schema == PROJECT_SCHEMA:
        return _register_template_v1(project_root, manifest, code_root)
    if schema == PROJECT_SCHEMA_V2:
        return _register_template_v2(project_root, manifest, code_root)
    raise ValueError("项目 schema 无效，无法登记 Template")


def _register_template_v1(
    project_root: Path, manifest: Path, code_root: Path,
) -> dict[str, Any]:
    with project_write_lock(project_root):
        control = _validate_project(project_root)
        _preflight_workflow_command_locked(control)
        facts = _normalize_manifest(_read_manifest(Path(manifest)))
        source_root = _real_directory(Path(code_root).expanduser().absolute(), "code-root")
        source_root = source_root.resolve(strict=True)
        git_top_level = _real_directory(
            Path(_git(source_root, "rev-parse", "--show-toplevel")).resolve(strict=True),
            "Git worktree 顶层目录",
        )
        if source_root != git_top_level:
            raise ValueError("code-root 必须是当前 Git worktree 的精确顶层目录")
        head = _git(source_root, "rev-parse", "HEAD")
        expected_head = facts["code_source"]["commit"]
        if head.lower() != expected_head:
            raise ValueError(f"code-root HEAD 与 manifest commit 不一致：{head}")
        if _git(source_root, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("code-root Git 工作树必须干净")
        template_root = _real_relative_directory(
            source_root, facts["code_source"]["template_path"], "template_path",
        )
        _verify_listed_files(
            source_root,
            template_root,
            facts["code_source"]["template_path"],
            expected_head,
            facts["listed_files"],
        )
        if _git(source_root, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("code-root Git 工作树必须干净")

        directory = control / "templates"
        if not os.path.lexists(directory):
            directory.mkdir()
        _real_directory(directory, "templates 目录")
        template_id = _next_id(directory, "TPL", TEMPLATE_ID)
        payload = {
            "schema": PROJECT_TEMPLATE_SCHEMA,
            "template_id": template_id,
            **{key: value for key, value in facts.items() if key != "schema"},
            "verification": {
                "git_head": expected_head,
                "listed_file_count": len(facts["listed_files"]),
                "verified": True,
            },
        }
        validate_project_template(payload, expected_id=template_id)
        destination = directory / f"{template_id}.json"
        if not atomic_create_json(destination, payload, transaction_id=uuid.uuid4().hex):
            raise FileExistsError(f"Template 已存在，拒绝覆盖：{destination}")
        return payload


def _register_template_v2(
    project_root: Path, manifest: Path, code_root: Path,
) -> dict[str, Any]:
    with project_write_lock(project_root):
        control = _validate_project(
            project_root, expected_schema=PROJECT_SCHEMA_V2,
        )
        _preflight_workflow_command_locked(control)
        from .v2_catalog import _code_status, validate_sources_locked

        sources = validate_sources_locked(control)
        facts = _normalize_manifest_v2(_read_manifest(Path(manifest)))
        _validate_v2_template_source_refs(facts, sources, "manifest")
        source_root = _real_directory(
            Path(code_root).expanduser().absolute(), "code-root",
        ).resolve(strict=True)
        git_top_level = _real_directory(
            Path(_git(source_root, "rev-parse", "--show-toplevel")).resolve(
                strict=True,
            ),
            "Git worktree 顶层目录",
        )
        if source_root != git_top_level:
            raise ValueError("code-root 必须是当前 Git worktree 的精确顶层目录")
        project_real = _real_directory(
            Path(project_root).resolve(strict=True), "project-root",
        )
        project_git_top = _real_directory(
            Path(_git(project_real, "rev-parse", "--show-toplevel")).resolve(
                strict=True,
            ),
            "项目 Git worktree 顶层目录",
        )
        if project_real != project_git_top or source_root != project_real:
            raise ValueError(
                "v2 Template ownership=project 要求 code-root "
                "就是当前 project-root 的真实 Git 顶层"
            )
        head = _git(source_root, "rev-parse", "HEAD").lower()
        expected_source = sources[facts["code"]["source_ref"]]
        if head != expected_source["commit"]:
            raise ValueError(f"code-root HEAD 与 code Source commit 不一致：{head}")
        if _code_status(source_root, project_real):
            raise ValueError("code-root Git 工作树必须干净")
        template_root = _real_relative_directory(
            source_root, facts["code"]["template_path"], "template_path",
        )
        _verify_listed_files(
            source_root,
            template_root,
            facts["code"]["template_path"],
            head,
            facts["code"]["listed_files"],
        )
        if _code_status(source_root, project_real):
            raise ValueError("code-root Git 工作树必须干净")
        final_head = _git(source_root, "rev-parse", "HEAD").lower()
        if final_head != head or final_head != expected_source["commit"]:
            raise ValueError(
                "code-root HEAD 在 Template 核验期间发生变化，拒绝登记"
            )

        directory = _real_directory(control / "templates", "templates 目录")
        template_id = _next_id(directory, "TPL", TEMPLATE_ID)
        payload = {
            "schema": PROJECT_TEMPLATE_SCHEMA_V2,
            "id": template_id,
            **facts,
            "validation": {
                "git_head": head,
                "listed_file_count": len(facts["code"]["listed_files"]),
                "verified": True,
            },
        }
        validate_project_template_v2(
            payload, expected_id=template_id, sources=sources,
        )
        destination = directory / f"{template_id}.json"
        if not atomic_create_json(
            destination, payload, transaction_id=uuid.uuid4().hex,
        ):
            raise FileExistsError(f"Template 已存在，拒绝覆盖：{destination}")
        return payload


def render_template(
    project: Path, template_id: str, output: Path,
) -> dict[str, Any]:
    if TEMPLATE_ID.fullmatch(template_id) is None:
        raise ValueError(f"Template ID 格式错误：{template_id}")
    project_root = _initialized_project(project)
    destination = Path(output).expanduser().absolute()
    with project_write_lock(project_root):
        schema = _read_object(
            project_root / ".experiment-workflow" / "project.json"
        ).get("schema")
        if schema not in {PROJECT_SCHEMA, PROJECT_SCHEMA_V2}:
            raise ValueError("项目 schema 无效，无法渲染 Template")
        control = _validate_project(project_root, expected_schema=schema)
        _preflight_workflow_command_locked(control)
        _require_output_outside_control(destination, control)
        if schema == PROJECT_SCHEMA_V2:
            raise ValueError("render-template 本轮只支持 v1 Template")
        payload = _read_template_v1(
            control / "templates" / f"{template_id}.json", template_id,
        )
        content = _render_html(payload)
        if not atomic_create_bytes_clean(
            destination, content, transaction_id=uuid.uuid4().hex,
        ):
            raise FileExistsError(f"HTML 已存在，拒绝覆盖：{destination}")
    return {"template_id": template_id, "output": str(destination)}


def validate_project_templates_locked(control: Path) -> dict[str, int]:
    return {"templates": len(load_project_templates_locked(control))}


def load_project_templates_locked(
    control: Path,
    *,
    sources: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    directory = Path(control) / "templates"
    if not os.path.lexists(directory):
        return {}
    _real_directory(directory, "templates 目录")
    schema = _read_object(Path(control) / "project.json").get("schema")
    if schema not in {PROJECT_SCHEMA, PROJECT_SCHEMA_V2}:
        raise ValueError("项目 schema 无效，无法校验 Template")
    if schema == PROJECT_SCHEMA_V2 and sources is None:
        from .v2_catalog import validate_sources_locked

        sources = validate_sources_locked(control)
    records: dict[str, dict[str, Any]] = {}
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if TEMPLATE_NAME.fullmatch(entry.name) is None:
            raise ValueError(f"templates 目录存在未知文件或目录：{entry}")
        if schema == PROJECT_SCHEMA_V2:
            assert sources is not None
            records[entry.stem] = _read_template_v2(
                entry, entry.stem, sources,
            )
        else:
            records[entry.stem] = _read_template_v1(entry, entry.stem)
    return records


def validate_project_template(payload: dict[str, Any], *, expected_id: str) -> None:
    if set(payload) != PROJECT_TEMPLATE_FIELDS or payload.get("schema") != PROJECT_TEMPLATE_SCHEMA:
        raise ValueError(f"Template 记录字段或 schema 无效：{expected_id}")
    if TEMPLATE_ID.fullmatch(expected_id) is None or payload.get("template_id") != expected_id:
        raise ValueError(f"Template ID 无效：{expected_id}")
    manifest = {
        "schema": MANIFEST_SCHEMA,
        **{key: payload[key] for key in MANIFEST_FIELDS if key != "schema"},
    }
    normalized = _normalize_manifest(manifest)
    if manifest != normalized:
        raise ValueError(f"Template 规范化事实发生漂移：{expected_id}")
    verification = payload.get("verification")
    if (
        not isinstance(verification, dict)
        or set(verification) != {"git_head", "listed_file_count", "verified"}
        or verification.get("verified") is not True
        or verification.get("git_head") != normalized["code_source"]["commit"]
        or type(verification.get("listed_file_count")) is not int
        or verification.get("listed_file_count") != len(normalized["listed_files"])
    ):
        raise ValueError(f"Template verification 无效：{expected_id}")


def validate_project_template_v2(
    payload: dict[str, Any],
    *,
    expected_id: str,
    sources: dict[str, dict[str, Any]],
) -> None:
    if (
        not isinstance(payload, dict)
        or set(payload) != PROJECT_TEMPLATE_FIELDS_V2
        or payload.get("schema") != PROJECT_TEMPLATE_SCHEMA_V2
        or payload.get("id") != expected_id
        or TEMPLATE_ID.fullmatch(expected_id) is None
    ):
        raise ValueError(f"v2 Template 记录字段、schema 或 ID 无效：{expected_id}")
    manifest = {
        "schema": MANIFEST_SCHEMA_V2,
        **{
            key: payload[key]
            for key in MANIFEST_FIELDS_V2
            if key != "schema"
        },
    }
    normalized = _normalize_manifest_v2(manifest)
    expected_facts = {
        "schema": PROJECT_TEMPLATE_SCHEMA_V2,
        "id": expected_id,
        **normalized,
    }
    if any(payload.get(key) != value for key, value in expected_facts.items()):
        raise ValueError(f"v2 Template 规范化事实发生漂移：{expected_id}")
    _validate_v2_template_source_refs(payload, sources, expected_id)
    verification = payload.get("validation")
    code_source = sources[payload["code"]["source_ref"]]
    if (
        not isinstance(verification, dict)
        or set(verification) != {
            "git_head", "listed_file_count", "verified",
        }
        or verification.get("verified") is not True
        or verification.get("git_head") != code_source["commit"]
        or type(verification.get("listed_file_count")) is not int
        or verification.get("listed_file_count")
        != len(payload["code"]["listed_files"])
    ):
        raise ValueError(f"v2 Template validation 无效：{expected_id}")


def _normalize_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != MANIFEST_FIELDS or payload.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("template manifest 顶层字段或 schema 无效")
    source = _exact_dict(payload.get("code_source"), {"repo_url", "commit", "template_path"}, "code_source")
    commit = _commit(source.get("commit"), "code_source.commit")
    template_path = normalize_safe_relative_path(source.get("template_path"), "template_path")

    listed = _bounded_list(payload.get("listed_files"), 1, MAX_LISTED_FILES, "listed_files")
    normalized_files: list[dict[str, str]] = []
    file_paths: set[str] = set()
    for item in listed:
        row = _exact_dict(item, {"path", "role", "digest"}, "listed_files item")
        path = normalize_safe_relative_path(row.get("path"), "listed_files.path")
        if path in file_paths:
            raise ValueError(f"listed_files.path 重复：{path}")
        file_paths.add(path)
        digest = row.get("digest")
        if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
            raise ValueError(f"listed_files.digest 无效：{path}")
        normalized_files.append({
            "path": path, "role": _text(row.get("role"), "listed_files.role"),
            "digest": digest.lower(),
        })

    interfaces = _exact_dict(payload.get("interfaces"), INTERFACE_FIELDS, "interfaces")
    normalized_interfaces = {key: _text(interfaces.get(key), f"interfaces.{key}") for key in sorted(INTERFACE_FIELDS)}

    attachments = _bounded_list(
        payload.get("attachment_points"), 1, MAX_ATTACHMENT_POINTS, "attachment_points",
    )
    normalized_attachments: list[dict[str, str]] = []
    attachment_names: set[str] = set()
    for item in attachments:
        row = _exact_dict(
            item, {"name", "contract", "target_path", "disabled_behavior"},
            "attachment_points item",
        )
        name = _text(row.get("name"), "attachment_points.name")
        if name in attachment_names:
            raise ValueError(f"attachment point name 重复：{name}")
        attachment_names.add(name)
        normalized_attachments.append({
            "name": name,
            "contract": _text(row.get("contract"), "attachment_points.contract"),
            "target_path": normalize_safe_relative_path(
                row.get("target_path"), "attachment_points.target_path",
            ),
            "disabled_behavior": _text(
                row.get("disabled_behavior"), "attachment_points.disabled_behavior",
            ),
        })

    provenance = _bounded_list(
        payload.get("provenance_sources"), 1, MAX_PROVENANCE_SOURCES,
        "provenance_sources",
    )
    normalized_provenance = [_normalize_provenance(item) for item in provenance]
    framework = _normalize_framework(payload.get("framework"))
    return {
        "schema": MANIFEST_SCHEMA,
        "name": _text(payload.get("name"), "name"),
        "description": _text(payload.get("description"), "description"),
        "code_source": {
            "repo_url": _text(source.get("repo_url"), "code_source.repo_url"),
            "commit": commit,
            "template_path": template_path,
        },
        "listed_files": normalized_files,
        "interfaces": normalized_interfaces,
        "attachment_points": normalized_attachments,
        "provenance_sources": normalized_provenance,
        "framework": framework,
    }


def _normalize_manifest_v2(payload: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or set(payload) != MANIFEST_FIELDS_V2
        or payload.get("schema") != MANIFEST_SCHEMA_V2
    ):
        raise ValueError("v2 template manifest 顶层字段或 schema 无效")
    ownership = payload.get("ownership")
    if ownership != "project":
        raise ValueError("v2 Template ownership 必须是 project")
    source_refs = _v2_id_list(
        payload.get("source_refs"), re.compile(r"SRC-[0-9]{4}"),
        "source_refs", minimum=1,
    )
    code = _exact_dict(payload.get("code"), CODE_FIELDS_V2, "code")
    code_source_ref = code.get("source_ref")
    if (
        not isinstance(code_source_ref, str)
        or re.fullmatch(r"SRC-[0-9]{4}", code_source_ref) is None
        or code_source_ref not in source_refs
    ):
        raise ValueError("code.source_ref 必须出现在 source_refs 中")
    template_path = _bounded_relative_path_v2(
        code.get("template_path"), "code.template_path",
    )
    listed = _bounded_list(
        code.get("listed_files"), 1, MAX_LISTED_FILES, "code.listed_files",
    )
    normalized_files: list[dict[str, str]] = []
    file_paths: set[str] = set()
    for item in listed:
        row = _exact_dict(
            item, {"path", "role", "digest"}, "code.listed_files item",
        )
        path = _bounded_relative_path_v2(
            row.get("path"), "code.listed_files.path",
        )
        if path in file_paths:
            raise ValueError(f"code.listed_files.path 重复：{path}")
        file_paths.add(path)
        digest = row.get("digest")
        if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
            raise ValueError(f"code.listed_files.digest 无效：{path}")
        normalized_files.append(
            {
                "path": path,
                "role": _text(row.get("role"), "code.listed_files.role"),
                "digest": digest.lower(),
            }
        )
    interfaces = _exact_dict(
        payload.get("interfaces"), INTERFACE_FIELDS, "interfaces",
    )
    normalized_interfaces = {
        key: _text(interfaces.get(key), f"interfaces.{key}")
        for key in sorted(INTERFACE_FIELDS)
    }
    attachments = _bounded_list(
        payload.get("attachment_points"),
        1,
        MAX_ATTACHMENT_POINTS,
        "attachment_points",
    )
    normalized_attachments: list[dict[str, str]] = []
    attachment_names: set[str] = set()
    for item in attachments:
        row = _exact_dict(
            item,
            {"name", "contract", "target_path", "disabled_behavior"},
            "attachment_points item",
        )
        name = _text(row.get("name"), "attachment_points.name")
        if name in attachment_names:
            raise ValueError(f"attachment point name 重复：{name}")
        attachment_names.add(name)
        normalized_attachments.append(
            {
                "name": name,
                "contract": _text(
                    row.get("contract"), "attachment_points.contract",
                ),
                "target_path": _bounded_relative_path_v2(
                    row.get("target_path"), "attachment_points.target_path",
                ),
                "disabled_behavior": _text(
                    row.get("disabled_behavior"),
                    "attachment_points.disabled_behavior",
                ),
            }
        )
    recipe = _exact_dict(
        payload.get("baseline_recipe"),
        BASELINE_RECIPE_FIELDS_V2,
        "baseline_recipe",
    )
    seed = recipe.get("seed")
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("baseline_recipe.seed 必须是 0..2^32-1 的整数")
    baseline_entry = _bounded_relative_path_v2(
        recipe.get("entry"), "baseline_recipe.entry",
    )
    baseline_config = _bounded_relative_path_v2(
        recipe.get("config"), "baseline_recipe.config",
    )
    required_listed_paths = {
        baseline_entry,
        baseline_config,
        *(
            attachment["target_path"]
            for attachment in normalized_attachments
        ),
    }
    missing_listed = sorted(required_listed_paths - file_paths)
    if missing_listed:
        raise ValueError(
            "v2 Template 可执行路径必须出现在 code.listed_files.path："
            f"{missing_listed[0]}"
        )
    return {
        "name": _text(payload.get("name"), "name"),
        "ownership": "project",
        "source_refs": source_refs,
        "code": {
            "source_ref": code_source_ref,
            "template_path": template_path,
            "listed_files": normalized_files,
        },
        "interfaces": normalized_interfaces,
        "attachment_points": normalized_attachments,
        "baseline_recipe": {
            "entry": baseline_entry,
            "config": baseline_config,
            "seed": seed,
        },
    }


def _validate_v2_template_source_refs(
    payload: dict[str, Any],
    sources: dict[str, dict[str, Any]],
    label: str,
) -> None:
    for source_id in payload["source_refs"]:
        source = sources.get(source_id)
        if source is None:
            raise ValueError(
                f"v2 Template source_refs 引用了不存在的 Source：{source_id}"
            )
        if source["kind"] != "code":
            raise ValueError(
                f"v2 Template source_refs 必须都是 code Source：{source_id}"
            )
    code_source_ref = payload["code"]["source_ref"]
    if code_source_ref not in sources:
        raise ValueError(
            f"v2 Template code.source_ref 不存在：{code_source_ref}"
        )


def _v2_id_list(
    value: object,
    pattern: re.Pattern[str],
    label: str,
    *,
    minimum: int,
) -> list[str]:
    items = _bounded_list(value, minimum, MAX_PROVENANCE_SOURCES, label)
    if (
        not all(
            isinstance(item, str) and pattern.fullmatch(item) is not None
            for item in items
        )
        or len(items) != len(set(items))
    ):
        raise ValueError(f"{label} 必须是无重复的严格 ID 列表")
    return list(items)


def _bounded_relative_path_v2(value: object, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise ValueError(f"{label} 必须是 1..4096 字符的安全相对路径")
    return normalize_safe_relative_path(value, label)


def _normalize_provenance(value: object) -> dict[str, Any]:
    fields = {
        "label", "locator", "identity", "commit", "license_spdx", "files", "symbols",
        "use_mode", "note",
    }
    row = _exact_dict(value, fields, "provenance_sources item")
    mode = row.get("use_mode")
    if mode not in USE_MODES:
        raise ValueError("provenance_sources.use_mode 无效")
    commit_value = row.get("commit")
    commit = None if commit_value is None else _commit(commit_value, "provenance_sources.commit")
    files = _string_list(row.get("files"), "provenance_sources.files")
    symbols = _string_list(row.get("symbols"), "provenance_sources.symbols")
    license_spdx = _text(row.get("license_spdx"), "provenance_sources.license_spdx")
    if mode in {"copied", "adapted"} and (
        commit is None or not files or license_spdx not in COMPATIBLE_CODE_LICENSES
    ):
        raise ValueError("copied/adapted 来源必须有 commit、files 和兼容 SPDX 许可证")
    return {
        "label": _text(row.get("label"), "provenance_sources.label"),
        "locator": _text(row.get("locator"), "provenance_sources.locator"),
        "identity": _text(row.get("identity"), "provenance_sources.identity"),
        "commit": commit,
        "license_spdx": license_spdx,
        "files": files,
        "symbols": symbols,
        "use_mode": mode,
        "note": _text(row.get("note"), "provenance_sources.note"),
    }


def _normalize_framework(value: object) -> dict[str, Any]:
    framework = _exact_dict(value, {"nodes", "edges"}, "framework")
    nodes = _bounded_list(framework.get("nodes"), 1, MAX_FRAMEWORK_NODES, "framework.nodes")
    edges = _bounded_list(framework.get("edges"), 0, MAX_FRAMEWORK_EDGES, "framework.edges")
    normalized_nodes: list[dict[str, str]] = []
    node_ids: set[str] = set()
    for item in nodes:
        row = _exact_dict(item, {"id", "label", "kind"}, "framework.nodes item")
        node_id = _text(row.get("id"), "framework.nodes.id")
        if node_id in node_ids:
            raise ValueError(f"framework node id 重复：{node_id}")
        node_ids.add(node_id)
        normalized_nodes.append({
            "id": node_id, "label": _text(row.get("label"), "framework.nodes.label"),
            "kind": _text(row.get("kind"), "framework.nodes.kind"),
        })
    normalized_edges: list[dict[str, str]] = []
    for item in edges:
        row = _exact_dict(item, {"source", "target", "label"}, "framework.edges item")
        source = _text(row.get("source"), "framework.edges.source")
        target = _text(row.get("target"), "framework.edges.target")
        if source not in node_ids or target not in node_ids:
            raise ValueError("framework edge 引用了不存在的节点")
        normalized_edges.append({
            "source": source, "target": target,
            "label": _text(row.get("label"), "framework.edges.label"),
        })
    return {"nodes": normalized_nodes, "edges": normalized_edges}


def _verify_listed_files(
    source_root: Path,
    template_root: Path,
    template_path: str,
    commit: str,
    listed: list[dict[str, str]],
) -> None:
    for item in listed:
        relative = item["path"]
        segments = relative.split("/")
        parent = template_root
        for segment in segments[:-1]:
            parent = _real_directory(parent / segment, f"listed_files.path 父目录 {relative}")
        path = parent / segments[-1]
        read_bounded_regular_file(path, LISTED_FILE_LIMIT, f"listed file {relative}")
        repo_relative = relative if template_path == "." else f"{template_path}/{relative}"
        blob = _git_blob(source_root, commit, repo_relative)
        blob_digest = f"sha256:{hashlib.sha256(blob).hexdigest()}"
        if blob_digest != item["digest"]:
            raise ValueError(f"listed file 与 manifest commit Git blob 不一致：{relative}")


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        return read_bounded_json_object(path, MANIFEST_LIMIT, "template manifest")
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取严格 template manifest：{path}；{error}") from error


def _read_template_v1(path: Path, expected_id: str) -> dict[str, Any]:
    try:
        payload = read_bounded_json_object(path, MANIFEST_LIMIT, "project template")
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取 Template JSON：{path}；{error}") from error
    validate_project_template(payload, expected_id=expected_id)
    return payload


def _read_template(path: Path, expected_id: str) -> dict[str, Any]:
    """保留 v1 内部调用接口，避免旧 Trial/Template 路径发生行为变化。"""
    return _read_template_v1(path, expected_id)


def _read_template_v2(
    path: Path,
    expected_id: str,
    sources: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    try:
        payload = read_bounded_json_object(
            path, MANIFEST_LIMIT, "v2 project template",
        )
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取 v2 Template JSON：{path}；{error}") from error
    validate_project_template_v2(
        payload, expected_id=expected_id, sources=sources,
    )
    return payload


def _git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments], capture_output=True, text=True,
            encoding="utf-8", check=False, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"无法执行 Git 校验：{root}；{error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ValueError(f"Git 校验失败：{detail}")
    return result.stdout.strip()


def _git_blob(root: Path, commit: str, repo_relative: str) -> bytes:
    _require_git_regular_blob(root, commit, repo_relative)
    object_name = f"{commit}:{repo_relative}"
    size_text = _git(root, "cat-file", "-s", object_name)
    try:
        size = int(size_text)
    except ValueError as error:
        raise ValueError(f"Git blob 大小无效：{repo_relative}") from error
    if size < 0 or size > LISTED_FILE_LIMIT:
        raise ValueError(f"Git blob 超过大小限制：{repo_relative}")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", object_name],
            capture_output=True, check=False, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"无法读取 Git blob：{repo_relative}：{error}") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"读取 Git blob 失败：{repo_relative}：{detail or result.returncode}")
    if len(result.stdout) != size:
        raise ValueError(f"Git blob 大小与声明不一致：{repo_relative}")
    return result.stdout


def _require_git_regular_blob(
    root: Path, commit: str, repo_relative: str,
) -> None:
    try:
        result = subprocess.run(
            [
                "git", "-C", str(root), "ls-tree", "-z",
                commit, "--", repo_relative,
            ],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(
            f"无法检查 Git 文件类型：{repo_relative}：{error}"
        ) from error
    records = [record for record in result.stdout.split(b"\0") if record]
    if result.returncode != 0 or len(records) != 1 or b"\t" not in records[0]:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(
            f"Git 路径无法唯一定位：{repo_relative}："
            f"{detail or result.returncode}"
        )
    metadata, raw_path = records[0].split(b"\t", 1)
    parts = metadata.split()
    if (
        len(parts) != 3
        or parts[0] not in {b"100644", b"100755"}
        or parts[1] != b"blob"
        or raw_path != repo_relative.encode("utf-8")
    ):
        mode = parts[0].decode("ascii", errors="replace") if parts else "unknown"
        object_type = (
            parts[1].decode("ascii", errors="replace")
            if len(parts) > 1
            else "unknown"
        )
        raise ValueError(
            "Git 路径必须是 mode 100644/100755 且 type=blob："
            f"{repo_relative}（实际 {mode} {object_type}）"
        )


def _real_directory(path: Path, label: str) -> Path:
    if is_link_or_reparse(path) or not path.is_dir():
        raise ValueError(f"{label} 必须是普通目录且不得为链接/reparse：{path}")
    return path


def _real_relative_directory(root: Path, relative: str, label: str) -> Path:
    current = _real_directory(root, "code-root")
    if relative == ".":
        return current
    for segment in relative.split("/"):
        current = _real_directory(current / segment, label)
    return current


def _require_output_outside_control(output: Path, control: Path) -> None:
    resolved_output = output.resolve(strict=False)
    resolved_control = control.resolve(strict=True)
    if resolved_output == resolved_control or resolved_control in resolved_output.parents:
        raise ValueError("render-template 输出必须位于 .experiment-workflow 控制面之外")


def _render_html(payload: dict[str, Any]) -> bytes:
    escape = lambda value: html.escape(str(value), quote=True)
    node_rows = "\n".join(
        f"<tr><td>{escape(node['id'])}</td><td>{escape(node['label'])}</td><td>{escape(node['kind'])}</td></tr>"
        for node in payload["framework"]["nodes"]
    )
    edge_rows = "\n".join(
        f"<tr><td>{escape(edge['source'])}</td><td>{escape(edge['target'])}</td><td>{escape(edge['label'])}</td></tr>"
        for edge in payload["framework"]["edges"]
    )
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{escape(payload['template_id'])} 框架视图</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#1f2937}}table{{border-collapse:collapse;width:100%;margin-bottom:2rem}}th,td{{border:1px solid #d1d5db;padding:.55rem;text-align:left}}th{{background:#f3f4f6}}code{{background:#f3f4f6;padding:.1rem .25rem}}</style></head>
<body><h1>{escape(payload['name'])}</h1><p>{escape(payload['description'])}</p>
<p>Template：<code>{escape(payload['template_id'])}</code>；commit：<code>{escape(payload['code_source']['commit'])}</code></p>
<h2>节点</h2><table><thead><tr><th>ID</th><th>名称</th><th>类型</th></tr></thead><tbody>{node_rows}</tbody></table>
<h2>连接</h2><table><thead><tr><th>来源</th><th>目标</th><th>说明</th></tr></thead><tbody>{edge_rows}</tbody></table>
</body></html>
"""
    return document.encode("utf-8")


def _exact_dict(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} 字段无效")
    return value


def _bounded_list(value: object, minimum: int, maximum: int, label: str) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{label} 数量必须在 {minimum}..{maximum} 之间")
    return value


def _string_list(value: object, label: str) -> list[str]:
    items = _bounded_list(value, 0, MAX_STRING_ITEMS, label)
    return [_text(item, label) for item in items]


def _commit(value: object, label: str) -> str:
    if not isinstance(value, str) or COMMIT.fullmatch(value) is None:
        raise ValueError(f"{label} 必须是精确 40 位十六进制字符串")
    return value.lower()


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 4096:
        raise ValueError(f"{label} 必须是 1..4096 字符的非空字符串")
    return value.strip()
