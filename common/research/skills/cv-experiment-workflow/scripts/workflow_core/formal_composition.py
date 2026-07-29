from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any

from .io import (
    atomic_write_json,
    parse_json_object_bytes,
    read_bounded_regular_file,
)
from .records import _utc_now, normalize_safe_relative_path
from .rendering import STRUCTURED_FILE_LIMIT, render_framework_html


FORMAL_TRIAL_SCHEMA = "cv-experiment-workflow.trial.v2"
FORMAL_ASSET_SCHEMA = "cv-experiment-workflow.code-asset.v2"
COMPOSITION_SCHEMA = "cv-experiment-workflow.composition.v1"
ASSET_ID = re.compile(r"CODE-[0-9]{4}")


def version_inherited_assets(
    version: dict[str, Any], template_id: str, template_commit: str,
) -> list[str]:
    """读取未来 Version lineage；不创建或激活 Version。"""
    if "template_id" not in version:
        if version["code_sources"][0]["commit"].lower() != template_commit:
            raise ValueError("首次正式 Trial 的 Base Version commit 必须等于 Template commit")
        return []
    inherited = version.get("accepted_code_asset_ids")
    if (
        version.get("template_id") != template_id
        or not isinstance(inherited, list)
        or len(inherited) != len(set(str(item) for item in inherited))
        or not all(isinstance(item, str) and ASSET_ID.fullmatch(item) for item in inherited)
    ):
        raise ValueError("Base Version 的 Template lineage 或继承清单无效")
    return list(inherited)


def load_inherited_compositions(
    control: Path,
    inherited_ids: list[str],
    template_id: str,
) -> list[dict[str, Any]]:
    """按 Version 接受顺序复核继承资产及其 composition。"""
    from . import templates as common

    compositions: list[dict[str, Any]] = []
    target_paths: set[str] = set()
    for asset_id in inherited_ids:
        directory = control / "code-assets" / asset_id
        common._require_real_directory(directory, "继承 CodeAsset 对象目录")
        asset = common._read_object(directory / "asset.json")
        common._validate_asset_shape(asset, asset_id)
        composition_bytes = read_bounded_regular_file(
            directory / "composition.json", STRUCTURED_FILE_LIMIT,
            f"{asset_id} composition.json",
        )
        composition = parse_json_object_bytes(
            composition_bytes, f"{asset_id} composition.json",
        )
        common._validate_composition(composition, asset_id=asset_id)
        external_ref = asset.get("external_code_ref")
        if (
            asset.get("schema") != FORMAL_ASSET_SCHEMA
            or asset.get("template_id") != template_id
            or composition.get("template_id") != template_id
            or asset["files"]["composition.json"] != common._sha256(composition_bytes)
            or external_ref is None
            or external_ref["relative_path"] != composition["target_path"]
        ):
            raise ValueError(f"继承 CodeAsset composition/binding 无效：{asset_id}")
        target_path = composition["target_path"]
        if target_path in target_paths:
            raise ValueError(f"继承 composition target_path 重复冲突：{target_path}")
        target_paths.add(target_path)
        compositions.append(composition)
    return compositions


def create_formal_trial_locked(
    control: Path,
    idea: dict[str, Any],
    idea_id: str,
    base_version_id: str,
    version: dict[str, Any],
    template_id: str,
    attachment_point: str,
    provenance: dict[str, Any],
    recovered: tuple[dict[str, Any], str] | None,
) -> dict[str, Any]:
    from . import templates as common
    from .project_templates import TEMPLATE_ID, _read_template

    if TEMPLATE_ID.fullmatch(template_id) is None:
        raise ValueError(f"Template ID 格式错误：{template_id}")
    template = _read_template(
        control / "templates" / f"{template_id}.json", template_id,
    )
    attachments = {item["name"]: item for item in template["attachment_points"]}
    if attachment_point not in attachments:
        raise ValueError(
            f"Template 不含 attachment point：{template_id}/{attachment_point}"
        )
    selected = attachments[attachment_point]
    mapping = _validate_idea_mapping(idea, idea_id, selected, attachment_point)
    mapping_digest = common._sha256(common._json_bytes(mapping))
    template_commit = template["code_source"]["commit"]
    inherited = version_inherited_assets(version, template_id, template_commit)
    inherited_compositions = load_inherited_compositions(
        control, inherited, template_id,
    )
    target_path = selected["target_path"]
    if target_path in {
        composition["target_path"] for composition in inherited_compositions
    }:
        raise ValueError(f"继承与本次 composition target_path 冲突：{target_path}")
    request_digest = _request_digest(
        idea_id, idea["revision"], mapping_digest, base_version_id,
        template_id, attachment_point, provenance,
    )
    if recovered is not None:
        recovered_trial, recovered_digest = recovered
        if recovered_digest == request_digest:
            return recovered_trial
    trial_id = common._next_directory_id(
        control / "trials", "TRIAL", common.TRIAL_ID,
    )
    asset_id = common._next_directory_id(
        control / "code-assets", "CODE", common.ASSET_ID,
    )
    composition = {
        "schema": COMPOSITION_SCHEMA,
        "template_id": template_id,
        "base_version_id": base_version_id,
        "base_commit": version["code_sources"][0]["commit"].lower(),
        "inherited_code_asset_ids": inherited,
        "new_code_asset_id": asset_id,
        "attachment_point": attachment_point,
        "target_path": target_path,
    }
    return _create_transaction(
        control, trial_id, asset_id, idea_id, base_version_id, template_id,
        attachment_point, idea["revision"], mapping_digest,
        composition, provenance, request_digest,
    )


def _validate_idea_mapping(
    idea: dict[str, Any],
    idea_id: str,
    attachment: dict[str, str],
    attachment_point: str,
) -> dict[str, str]:
    from .records import IDEA_SCHEMA_V2, validate_implementation_mapping

    if idea.get("schema") != IDEA_SCHEMA_V2 or idea.get("implementation_mapping") is None:
        raise ValueError(f"formal Trial 要求 Idea v2 且存在 implementation mapping：{idea_id}")
    mapping = validate_implementation_mapping(idea["implementation_mapping"])
    if (
        mapping["problem"] != idea.get("problem")
        or mapping["mechanism"] != idea.get("mechanism")
        or mapping["attachment_point"] != attachment_point
        or mapping["target_path"] != attachment["target_path"]
        or mapping["disabled_behavior"] != attachment["disabled_behavior"]
        or not mapping["validation"].strip()
    ):
        raise ValueError(f"Idea implementation mapping 与 formal Trial/Template 不一致：{idea_id}")
    return mapping


def external_code_reference(
    asset_directory: Path,
    asset_id: str,
    code_root: Path,
    relative_path: str,
) -> dict[str, str]:
    from . import templates as common
    from .project_templates import LISTED_FILE_LIMIT, _git, _git_blob, _real_directory

    normalized = normalize_safe_relative_path(relative_path, "relative-path")
    if normalized != relative_path:
        raise ValueError("relative-path 必须使用规范 POSIX 相对路径")
    composition = common._read_regular_json(asset_directory / "composition.json")
    common._validate_composition(composition, asset_id=asset_id)
    if normalized != composition["target_path"]:
        raise ValueError("relative-path 必须等于 composition.target_path")

    source_root = _real_directory(
        code_root.expanduser().absolute(), "code-root",
    ).resolve(strict=True)
    git_top = _real_directory(
        Path(_git(source_root, "rev-parse", "--show-toplevel")).resolve(strict=True),
        "Git worktree 顶层目录",
    )
    if source_root != git_top:
        raise ValueError("code-root 必须是当前 Git worktree 的精确顶层目录")
    if _git(source_root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("code-root Git 工作树必须干净")
    commit = _git(source_root, "rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("code-root HEAD 必须是精确 40 位小写 commit")
    repo_url = _git(source_root, "remote", "get-url", "origin")
    if not repo_url.strip():
        raise ValueError("code-root origin URL 不能为空")
    current = source_root
    segments = normalized.split("/")
    for segment in segments[:-1]:
        current = _real_directory(current / segment, "relative-path 父目录")
    read_bounded_regular_file(
        current / segments[-1], LISTED_FILE_LIMIT, "external code file",
    )
    if _git(source_root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("code-root Git 工作树读取期间发生变化或不干净")
    blob = _git_blob(source_root, commit, normalized)
    return {
        "repo_url": repo_url,
        "commit": commit,
        "relative_path": normalized,
        "digest": f"sha256:{common._sha256(blob)}",
    }


def _request_digest(
    idea_id: str,
    idea_revision: int,
    mapping_digest: str,
    base_version_id: str,
    template_id: str,
    attachment_point: str,
    provenance: dict[str, Any],
) -> str:
    from . import templates as common

    request = {
        "idea_id": idea_id,
        "idea_revision": idea_revision,
        "implementation_mapping_sha256": mapping_digest,
        "base_version_id": base_version_id,
        "template_id": template_id,
        "attachment_point": attachment_point,
        "provenance_sha256": common._sha256(common._json_bytes(provenance)),
    }
    return common._sha256(common._json_bytes(request))


def _create_transaction(
    control: Path,
    trial_id: str,
    asset_id: str,
    idea_id: str,
    version_id: str,
    template_id: str,
    attachment_point: str,
    idea_revision: int,
    mapping_digest: str,
    composition: dict[str, Any],
    provenance: dict[str, Any],
    request_digest: str,
) -> dict[str, Any]:
    from . import templates as common

    common._validate_control_directories(control)
    tx = uuid.uuid4().hex
    runtime = control / ".runtime"
    staging = runtime / f"new-trial-{tx}"
    marker_path = runtime / common.MARKER_NAME
    trial_stage = staging / trial_id
    asset_stage = staging / asset_id
    trial_final = control / "trials" / trial_id
    asset_final = control / "code-assets" / asset_id
    timestamp = _utc_now()
    module_bytes = (
        '"""正式 Trial 的通用骨架；默认不改变输入。"""\n\n'
        "def apply(value, *, enabled=False):\n"
        "    if not enabled:\n"
        "        return value\n"
        '    raise NotImplementedError("请在外部代码仓库实现并绑定此 attachment")\n'
    ).encode("utf-8")
    contract_bytes = (
        "from module import apply\n\n"
        "def test_disabled_by_default():\n"
        "    marker = object()\n"
        "    assert apply(marker) is marker\n"
    ).encode("utf-8")
    provenance_bytes = common._json_bytes(provenance)
    composition_bytes = common._json_bytes(composition)
    trial = {
        "schema": FORMAL_TRIAL_SCHEMA, "id": trial_id, "status": "active",
        "idea_id": idea_id, "base_version_id": version_id,
        "code_asset_id": asset_id, "template_id": template_id,
        "attachment_point": attachment_point, "transaction_id": tx,
        "idea_revision": idea_revision,
        "implementation_mapping_sha256": mapping_digest,
        "created_at": timestamp,
    }
    asset = {
        "schema": FORMAL_ASSET_SCHEMA, "id": asset_id, "status": "active",
        "trial_id": trial_id, "template_id": template_id,
        "attachment_point": attachment_point, "transaction_id": tx,
        "created_at": timestamp,
        "files": {
            "module.py": common._sha256(module_bytes),
            "test_contract.py": common._sha256(contract_bytes),
            "provenance.json": common._sha256(provenance_bytes),
            "composition.json": common._sha256(composition_bytes),
        },
        "external_code_ref": None,
    }
    framework_bytes = render_framework_html(
        asset, module_bytes, contract_bytes, provenance_bytes,
    )
    marker = {
        "schema": common.TRANSACTION_SCHEMA, "transaction_id": tx,
        "trial_id": trial_id, "asset_id": asset_id,
        "request_digest": request_digest,
    }
    try:
        atomic_write_json(marker_path, marker)
        staging.mkdir()
        trial_stage.mkdir()
        asset_stage.mkdir()
        atomic_write_json(trial_stage / "trial.json", trial)
        atomic_write_json(asset_stage / "asset.json", asset)
        (asset_stage / "module.py").write_bytes(module_bytes)
        (asset_stage / "test_contract.py").write_bytes(contract_bytes)
        atomic_write_json(asset_stage / "provenance.json", provenance)
        atomic_write_json(asset_stage / "composition.json", composition)
        (asset_stage / "framework.html").write_bytes(framework_bytes)
        atomic_write_json(staging / "transaction.json", marker)
        if os.path.lexists(trial_final) or os.path.lexists(asset_final):
            raise FileExistsError("Trial 或 CodeAsset ID 已存在，拒绝覆盖")
        common._validate_control_directories(control)
        os.replace(trial_stage, trial_final)
        common._validate_control_directories(control)
        os.replace(asset_stage, asset_final)
        (staging / "transaction.json").unlink()
        staging.rmdir()
        try:
            marker_path.unlink()
        except BaseException:
            if not os.path.lexists(marker_path):
                return trial
            raise
        return trial
    except Exception:
        if os.path.lexists(marker_path):
            recovered = common._recover_transaction(control)
            if recovered is not None:
                return recovered[0]
        else:
            common._cleanup_unpublished_staging(staging, trial_id, asset_id)
        raise
