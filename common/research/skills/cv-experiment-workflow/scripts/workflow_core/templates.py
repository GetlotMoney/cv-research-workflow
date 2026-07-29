from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .io import (
    _atomic_write_bytes,
    atomic_write_json,
    parse_json_object_bytes,
    read_bounded_json_object,
    read_bounded_regular_file,
)
from .locking import project_write_lock
from .project import is_link_or_reparse
from .rendering import (
    PYTHON_FILE_LIMIT,
    STRUCTURED_FILE_LIMIT,
    render_framework_html,
    validated_render_inputs,
)
from .records import (
    IDEA_ID,
    IDEA_SCHEMA_V2,
    VERSION_ID,
    _initialized_project,
    _is_utc_iso,
    _read_object,
    _utc_now,
    _validate_project,
    normalize_safe_relative_path,
    validate_idea,
    validate_version,
)


TRIAL_SCHEMA = "cv-experiment-workflow.trial.v1"
ASSET_SCHEMA = "cv-experiment-workflow.code-asset.v1"
FORMAL_TRIAL_SCHEMA = "cv-experiment-workflow.trial.v2"
FORMAL_ASSET_SCHEMA = "cv-experiment-workflow.code-asset.v2"
COMPOSITION_SCHEMA = "cv-experiment-workflow.composition.v1"
PROVENANCE_SCHEMA = "cv-experiment-workflow.provenance.v1"
TRANSACTION_SCHEMA = "cv-experiment-workflow.new-trial-transaction.v2"
TEMPLATE_SCHEMA = "cv-experiment-workflow.template.v1"
TRIAL_ID = re.compile(r"TRIAL-[0-9]{4}")
ASSET_ID = re.compile(r"CODE-[0-9]{4}")
SCOPES = {"none", "paper-derived", "original-hypothesis"}
PROVENANCE_STATUS_PRIORITY = {"verified": 0, "declared": 1, "unknown": 2}
COMPATIBLE_CODE_LICENSES = {
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC",
}
TEMPLATE_FILES = ("template.json", "module.py", "test_contract.py")
ASSET_DIGEST_FILES = ("module.py", "test_contract.py", "provenance.json")
V1_ASSET_FILES = ("asset.json", *ASSET_DIGEST_FILES, "framework.html")
FORMAL_ASSET_DIGEST_FILES = (*ASSET_DIGEST_FILES, "composition.json")
FORMAL_ASSET_FILES = ("asset.json", *FORMAL_ASSET_DIGEST_FILES, "framework.html")
ASSET_FILES = FORMAL_ASSET_FILES
MARKER_NAME = "new-trial-transaction.json"


def new_trial(
    project: Path,
    idea_id: str,
    base_version_id: str,
    template_family: str | None,
    attachment_point: str,
    *,
    template_id: str | None = None,
    claim_scope: str = "none",
    provenance_file: Path | None = None,
) -> dict[str, Any]:
    if (template_family is None) == (template_id is None):
        raise ValueError("必须且只能选择 template-family 或 template")
    if template_id is not None and (
        claim_scope != "none" or provenance_file is not None
    ):
        raise ValueError("claim-scope 与 provenance-file 仅用于 demo template-family 路径")
    if claim_scope not in SCOPES:
        raise ValueError(f"scientific claim scope 无效：{claim_scope}")
    root = _initialized_project(project)
    _validate_transaction_directories(root)
    with project_write_lock(root):
        control = _validate_project(root)
        _validate_transaction_directories(root)
        recovered = _recover_transaction(control)
        from .validation import preflight_workflow_command_locked
        preflight_workflow_command_locked(control)
        idea = _read_object(control / "ideas" / f"{idea_id}.json")
        validate_idea(idea, expected_id=idea_id)
        if idea.get("status") != "active":
            raise ValueError(f"仅 active 且完整的 Idea 可创建 Trial：{idea_id}")
        version = _read_object(control / "versions" / f"{base_version_id}.json")
        validate_version(version, expected_id=base_version_id)
        if version.get("status") != "active":
            raise ValueError(f"仅 active Version 可创建 Trial：{base_version_id}")
        provenance = _load_provenance(provenance_file, claim_scope, idea=idea)
        if template_id is not None:
            return _new_formal_trial_locked(
                control, idea, idea_id, base_version_id, version, template_id,
                attachment_point, provenance, recovered,
            )
        request_digest = _request_digest(
            idea_id, base_version_id, template_family, attachment_point,
            claim_scope, provenance,
        )
        if recovered is not None:
            recovered_trial, recovered_digest = recovered
            if recovered_digest == request_digest:
                return recovered_trial
        adapter = _load_adapter(control)
        template = _select_template(
            template_family, attachment_point, adapter["capabilities"],
        )
        trial_id = _next_directory_id(control / "trials", "TRIAL", TRIAL_ID)
        asset_id = _next_directory_id(control / "code-assets", "CODE", ASSET_ID)
        return _create_transaction(
            control, trial_id, asset_id, idea_id, base_version_id,
            attachment_point, template, provenance, request_digest,
        )


def _new_formal_trial_locked(
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
    from .formal_composition import create_formal_trial_locked

    return create_formal_trial_locked(
        control, idea, idea_id, base_version_id, version, template_id,
        attachment_point, provenance, recovered,
    )


def validate_project_trials(project: Path) -> dict[str, Any]:
    root = _initialized_project(project)
    _validate_transaction_directories(root)
    with project_write_lock(root):
        control = _validate_project(root)
        return _validate_project_trials_locked(control)


def sync_code_asset(
    project: Path,
    asset_id: str,
    *,
    code_root: Path | None = None,
    relative_path: str | None = None,
) -> dict[str, Any]:
    """同步实现者编辑后的事实摘要与派生 HTML；从不导入或执行用户代码。"""
    if (code_root is None) != (relative_path is None):
        raise ValueError("code-root 与 relative-path 必须同时提供或同时省略")
    if ASSET_ID.fullmatch(asset_id) is None:
        raise ValueError(f"CodeAsset ID 格式错误：{asset_id}")
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root)
        _validate_control_directories(control)
        from .attempts import _validate_project_attempt_ledger_locked
        try:
            _counts, attempts = _validate_project_attempt_ledger_locked(control)
        except ValueError as error:
            raise ValueError(f"Attempt frozen target/ledger invalid：{error}") from error
        for attempt_id, attempt in attempts.items():
            if asset_id in attempt["ordered_code_asset_ids"]:
                raise ValueError(
                    f"Attempt frozen target/ledger invalid：CodeAsset {asset_id} "
                    f"已被 {attempt_id} 冻结"
                )
        directory = control / "code-assets" / asset_id
        _require_real_directory(directory, "CodeAsset 对象目录")
        asset_path = directory / "asset.json"
        asset = _read_object(asset_path)
        _validate_asset_shape(asset, asset_id)

        external_ref = None
        if code_root is not None and relative_path is not None:
            if asset["schema"] != FORMAL_ASSET_SCHEMA:
                raise ValueError("外部代码绑定只允许正式 v2 CodeAsset")
            external_ref = _external_code_reference(
                directory, asset_id, Path(code_root), relative_path,
            )
            if (
                asset["external_code_ref"] is not None
                and asset["external_code_ref"] != external_ref
            ):
                raise ValueError(f"CodeAsset 已绑定不同 external_code_ref：{asset_id}")

        module, contract, provenance_bytes = validated_render_inputs(directory)
        provenance = parse_json_object_bytes(provenance_bytes, "provenance.json")
        _validate_provenance(provenance)
        from .validation import validate_boundaries_locked
        validate_boundaries_locked(control)

        updated = dict(asset)
        files = {
            "module.py": _sha256(module),
            "test_contract.py": _sha256(contract),
            "provenance.json": _sha256(provenance_bytes),
        }
        if asset["schema"] == FORMAL_ASSET_SCHEMA:
            composition_bytes = read_bounded_regular_file(
                directory / "composition.json", STRUCTURED_FILE_LIMIT,
                "composition.json",
            )
            composition = parse_json_object_bytes(
                composition_bytes, "composition.json",
            )
            _validate_composition(composition, asset_id=asset_id)
            files["composition.json"] = _sha256(composition_bytes)
            if external_ref is not None:
                updated["external_code_ref"] = external_ref
        updated["files"] = files
        _validate_asset_shape(updated, asset_id)
        framework = render_framework_html(
            updated, module, contract, provenance_bytes,
        )
        framework_path = directory / "framework.html"
        if os.path.lexists(framework_path) and (
            is_link_or_reparse(framework_path) or not framework_path.is_file()
        ):
            raise ValueError(f"framework.html 不是普通文件，拒绝覆盖：{framework_path}")

        atomic_write_json(asset_path, updated, transaction_id=uuid.uuid4().hex)
        _atomic_write_bytes(
            framework_path,
            framework,
            operation="framework.html 原子写入",
            transaction_id=uuid.uuid4().hex,
        )
        return updated


def _external_code_reference(
    asset_directory: Path,
    asset_id: str,
    code_root: Path,
    relative_path: str,
) -> dict[str, str]:
    from .formal_composition import external_code_reference

    return external_code_reference(
        asset_directory, asset_id, code_root, relative_path,
    )


def _validate_project_trials_locked(control: Path) -> dict[str, Any]:
    _validate_control_directories(control)
    _recover_transaction(control)
    adapter = _load_adapter(control)
    issues: list[str] = []
    trials: dict[str, dict[str, Any]] = {}
    assets: dict[str, dict[str, Any]] = {}
    compositions: dict[str, dict[str, Any]] = {}
    frameworks = 0
    for path in _object_directories(control / "trials", TRIAL_ID, issues):
        trial_path = path / "trial.json"
        try:
            trial = _read_object(trial_path)
            _validate_trial_shape(trial, path.name)
            trials[path.name] = trial
        except ValueError as error:
            issues.append(str(error))
    for path in _object_directories(control / "code-assets", ASSET_ID, issues):
        try:
            asset = _read_object(path / "asset.json")
            _validate_asset_shape(asset, path.name)
            assets[path.name] = asset
            module, contract, provenance_bytes = validated_render_inputs(path)
            provenance = parse_json_object_bytes(provenance_bytes, "provenance.json")
            _validate_provenance(provenance)
            contents = {
                "module.py": module,
                "test_contract.py": contract,
                "provenance.json": provenance_bytes,
            }
            if asset["schema"] == FORMAL_ASSET_SCHEMA:
                composition_bytes = read_bounded_regular_file(
                    path / "composition.json", STRUCTURED_FILE_LIMIT, "composition.json",
                )
                composition = parse_json_object_bytes(
                    composition_bytes, "composition.json",
                )
                _validate_composition(composition, asset_id=path.name)
                compositions[path.name] = composition
                contents["composition.json"] = composition_bytes
            for filename, content in contents.items():
                actual = _sha256(content)
                if asset["files"].get(filename) != actual:
                    issues.append(f"{path.name} 文件 digest 不匹配：{filename}")
            expected_framework = render_framework_html(
                asset, module, contract, provenance_bytes,
            )
            framework_path = path / "framework.html"
            try:
                actual_framework = read_bounded_regular_file(
                    framework_path,
                    STRUCTURED_FILE_LIMIT,
                    "framework.html",
                )
            except ValueError:
                actual_framework = None
            if actual_framework != expected_framework:
                issues.append(f"{path.name}: stale framework.html")
            else:
                frameworks += 1
        except ValueError as error:
            issues.append(str(error))
    referenced_assets: list[str] = []
    for trial_id, trial in trials.items():
        asset_id = trial["code_asset_id"]
        referenced_assets.append(asset_id)
        idea: dict[str, Any] | None = None
        try:
            idea = _read_object(control / "ideas" / f"{trial['idea_id']}.json")
            validate_idea(idea, expected_id=trial["idea_id"])
            if idea.get("status") != "active":
                raise ValueError(f"Trial 引用的 Idea 非 active：{trial['idea_id']}")
        except ValueError as error:
            issues.append(f"{trial_id} 的 Idea 引用无效：{error}")
        try:
            version = _read_object(
                control / "versions" / f"{trial['base_version_id']}.json"
            )
            validate_version(version, expected_id=trial["base_version_id"])
            if version.get("status") != "active":
                raise ValueError(
                    f"Trial 引用的 Version 非 active：{trial['base_version_id']}"
                )
        except ValueError as error:
            issues.append(f"{trial_id} 的 Version 引用无效：{error}")
        asset = assets.get(asset_id)
        if asset is None or asset.get("trial_id") != trial_id:
            issues.append(f"{trial_id} 与 CodeAsset 引用不一致")
        if asset is not None and trial["schema"] == TRIAL_SCHEMA:
            if asset.get("schema") != ASSET_SCHEMA:
                issues.append(f"{trial_id} 与 {asset_id} schema lineage 不一致")
            shared_facts = (
                "status", "template_family", "attachment_point",
                "template_selection_reason", "transaction_id", "created_at",
                "template_sha256",
            )
            for field in shared_facts:
                if asset.get(field) != trial.get(field):
                    issues.append(f"{trial_id} 与 {asset_id} 共有事实漂移：{field}")
        if trial["schema"] == FORMAL_TRIAL_SCHEMA and asset is not None:
            if asset.get("schema") != FORMAL_ASSET_SCHEMA:
                issues.append(f"{trial_id} 与 {asset_id} schema lineage 不一致")
            for field in (
                "status", "template_id", "attachment_point", "transaction_id",
                "created_at",
            ):
                if asset.get(field) != trial.get(field):
                    issues.append(f"{trial_id} 与 {asset_id} 共有事实漂移：{field}")
            composition = compositions.get(asset_id)
            if composition is None:
                issues.append(f"{asset_id} 缺少有效 composition")
            else:
                _validate_formal_links(
                    control, trial_id, trial, idea, asset_id, asset, composition, assets,
                    issues,
                )
        if trial["schema"] == TRIAL_SCHEMA:
            try:
                template = _select_template(
                    trial["template_family"], trial["attachment_point"],
                    adapter["capabilities"],
                    template_sha256=trial["template_sha256"],
                )
                if template["manifest"]["canonical_sha256"] != trial["template_sha256"]:
                    issues.append(f"{trial_id} 模板 canonical digest 不匹配")
            except ValueError as error:
                issues.append(str(error))
    for asset_id in assets:
        if referenced_assets.count(asset_id) != 1:
            issues.append(f"{asset_id} 必须恰好被一个 Trial 引用")
    if issues:
        raise ValueError("；".join(issues))
    return {"schema": "cv-experiment-workflow.validation.v1", "valid": True,
            "trials": len(trials), "code_assets": len(assets),
            "frameworks": frameworks}


def _validate_formal_links(
    control: Path,
    trial_id: str,
    trial: dict[str, Any],
    idea: dict[str, Any] | None,
    asset_id: str,
    asset: dict[str, Any],
    composition: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    issues: list[str],
) -> None:
    from .project_templates import _read_template

    for field in ("template_id", "base_version_id", "attachment_point"):
        if composition.get(field) != trial.get(field):
            issues.append(f"{trial_id} 与 composition 共有事实漂移：{field}")
    if composition.get("new_code_asset_id") != asset_id:
        issues.append(f"{trial_id} 与 composition new CodeAsset 引用不一致")
    try:
        template = _read_template(
            control / "templates" / f"{trial['template_id']}.json",
            trial["template_id"],
        )
        attachments = {
            item["name"]: item for item in template["attachment_points"]
        }
        selected = attachments.get(trial["attachment_point"])
        if selected is None:
            raise ValueError("正式 Trial attachment point 不在 Template 中")
        if "idea_revision" in trial:
            mapping, problem, mechanism = _idea_mapping_for_formal_trial(
                idea, trial,
            )
            if _sha256(_json_bytes(mapping)) != trial["implementation_mapping_sha256"]:
                raise ValueError("formal Trial implementation mapping digest 不匹配")
            if (
                mapping["problem"] != problem
                or mapping["mechanism"] != mechanism
                or mapping["attachment_point"] != trial["attachment_point"]
                or mapping["target_path"] != selected["target_path"]
                or mapping["disabled_behavior"] != selected["disabled_behavior"]
            ):
                raise ValueError("formal Trial mapping 与 Idea/Template 语义不一致")
        if composition["target_path"] != selected["target_path"]:
            raise ValueError("composition.target_path 与 Template attachment 不一致")
        external_ref = asset.get("external_code_ref")
        if (
            external_ref is not None
            and external_ref["relative_path"] != composition["target_path"]
        ):
            raise ValueError("external_code_ref.relative_path 与 composition.target_path 不一致")
        version = _read_object(
            control / "versions" / f"{trial['base_version_id']}.json"
        )
        if version["code_sources"][0]["commit"].lower() != composition["base_commit"]:
            raise ValueError("composition.base_commit 与 Base Version 主 commit 不一致")
        inherited = composition["inherited_code_asset_ids"]
        if "template_id" in version:
            if version.get("template_id") != trial["template_id"]:
                raise ValueError("Base Version 与 Trial Template lineage 不一致")
            expected = version.get("accepted_code_asset_ids")
            if inherited != expected:
                raise ValueError("composition 继承顺序与 Base Version 不一致")
        elif inherited:
            raise ValueError("首次正式 Trial 不得继承 CodeAsset")
        elif composition["base_commit"] != template["code_source"]["commit"]:
            raise ValueError("首次 composition.base_commit 与 Template commit 不一致")
        from .formal_composition import load_inherited_compositions
        inherited_compositions = load_inherited_compositions(
            control, inherited, trial["template_id"],
        )
        if composition["target_path"] in {
            item["target_path"] for item in inherited_compositions
        }:
            raise ValueError(
                "继承与本次 composition target_path 冲突："
                f"{composition['target_path']}"
            )
        for inherited_id in inherited:
            inherited_asset = assets.get(inherited_id)
            if inherited_asset is None:
                raise ValueError(f"继承 CodeAsset 缺失：{inherited_id}")
            if (
                inherited_asset.get("schema") != FORMAL_ASSET_SCHEMA
                or inherited_asset.get("template_id") != trial["template_id"]
                or inherited_asset.get("external_code_ref") is None
            ):
                raise ValueError(f"继承 CodeAsset 未绑定或 lineage 不一致：{inherited_id}")
    except (KeyError, TypeError, ValueError) as error:
        issues.append(f"{trial_id} 正式 composition 无效：{error}")


def _idea_mapping_for_formal_trial(
    idea: dict[str, Any] | None, trial: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    if idea is None or idea.get("schema") != IDEA_SCHEMA_V2:
        raise ValueError("formal Trial 引用的 Idea 必须是 v2")
    frozen_revision = trial["idea_revision"]
    current_revision = idea["revision"]
    if frozen_revision == current_revision:
        mapping = idea["implementation_mapping"]
        problem = idea["problem"]
        mechanism = idea["mechanism"]
    elif frozen_revision < current_revision:
        history = next(
            (
                item for item in idea["revision_history"]
                if item["revision"] == frozen_revision
            ),
            None,
        )
        mapping = None if history is None else history["implementation_mapping"]
        problem = None if history is None else history["problem"]
        mechanism = None if history is None else history["mechanism"]
    else:
        mapping = None
        problem = None
        mechanism = None
    if (
        not isinstance(mapping, dict)
        or not isinstance(problem, str)
        or not isinstance(mechanism, str)
    ):
        raise ValueError("formal Trial 对应的 Idea revision mapping 缺失")
    return mapping, problem, mechanism


def _skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _select_template(
    family: str,
    attachment_point: str,
    capabilities: dict[str, bool],
    *,
    template_sha256: str | None = None,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    capability_mismatch = False
    templates = _skill_root() / "assets" / "templates"
    if is_link_or_reparse(templates) or not templates.is_dir():
        raise ValueError(f"模板目录无效：{templates}")
    for directory in templates.iterdir():
        if is_link_or_reparse(directory) or not directory.is_dir():
            continue
        manifest = _read_regular_json(directory / "template.json")
        _validate_manifest_metadata(directory, manifest)
        if manifest["family"] != family or attachment_point not in manifest["attachment_points"]:
            continue
        if template_sha256 is not None and manifest["canonical_sha256"] != template_sha256:
            continue
        if not all(capabilities.get(name) is True for name in manifest["required_capabilities"]):
            capability_mismatch = True
            continue
        test_contract = directory / "test_contract.py"
        has_regular_test = not is_link_or_reparse(test_contract) and test_contract.is_file()
        canonical_path = os.path.normcase(str(directory.resolve(strict=True)))
        template_id = directory.relative_to(templates).as_posix()
        exact_interface = attachment_point in manifest["interfaces"]
        rank = (
            0 if exact_interface else 1,
            len(manifest["dependencies"]),
            0 if has_regular_test else 1,
            PROVENANCE_STATUS_PRIORITY[manifest["provenance_status"]],
            canonical_path,
        )
        matches.append({
            "directory": directory,
            "manifest": manifest,
            "rank": rank,
            "exact_interface": exact_interface,
            "has_regular_test": has_regular_test,
            "canonical_path": canonical_path,
            "template_id": template_id,
        })
    if not matches:
        if capability_mismatch:
            raise ValueError(
                f"adapter capabilities 不满足模板要求：family={family}、"
                f"attachment point={attachment_point}"
            )
        raise ValueError(f"没有匹配 family={family}、attachment point={attachment_point} 的模板")
    selected = min(matches, key=lambda candidate: candidate["rank"])
    directory = selected["directory"]
    manifest = selected["manifest"]
    _validate_manifest(directory, manifest)
    actual_digest = _template_digest(directory, manifest)
    if manifest["canonical_sha256"] != actual_digest:
        raise ValueError(f"模板 canonical sha256 不匹配：{directory.name}")
    selected["selection_reason"] = (
        f"硬过滤 family={family}、attachment point={attachment_point}、"
        f"required_capabilities={manifest['required_capabilities']}；"
        f"排序 exact_interface={'true' if selected['exact_interface'] else 'false'}、"
        f"dependency_count={len(manifest['dependencies'])}、"
        f"test_contract={'regular' if selected['has_regular_test'] else 'missing'}、"
        f"provenance_status={manifest['provenance_status']}、"
        f"template_id={selected['template_id']}"
    )
    return selected


def _validate_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    _validate_manifest_metadata(directory, manifest)
    for filename in TEMPLATE_FILES:
        path = directory / filename
        if is_link_or_reparse(path) or not path.is_file():
            raise ValueError(f"模板文件不是普通文件：{path}")


def _validate_manifest_metadata(directory: Path, manifest: dict[str, Any]) -> None:
    expected = {
        "schema", "family", "attachment_points", "template_kind",
        "scientific_claim_scope", "papers", "code_sources", "canonical_sha256",
        "required_capabilities", "interfaces", "dependencies", "provenance_status",
    }
    list_fields = (
        "attachment_points", "required_capabilities", "interfaces", "dependencies",
    )
    if (
        set(manifest) != expected
        or manifest.get("schema") != TEMPLATE_SCHEMA
        or not _nonempty(manifest.get("family"))
        or manifest.get("template_kind") != "structural_scaffold"
        or manifest.get("scientific_claim_scope") != "none"
        or manifest.get("papers") != []
        or manifest.get("code_sources") != []
        or manifest.get("provenance_status") not in PROVENANCE_STATUS_PRIORITY
        or not all(isinstance(manifest.get(field), list) for field in list_fields)
        or not manifest["attachment_points"]
        or not all(
            all(_nonempty(item) for item in manifest[field])
            and len(manifest[field]) == len(set(manifest[field]))
            for field in list_fields
        )
        or not _strict_fullmatch(r"[0-9a-f]{64}", manifest.get("canonical_sha256"))
    ):
        raise ValueError(f"模板 manifest 无效：{directory.name}")


def _load_adapter(control: Path) -> dict[str, Any]:
    from .policy import load_adapter

    return load_adapter(control)


def _template_digest(directory: Path, manifest: dict[str, Any]) -> str:
    canonical_manifest = dict(manifest)
    canonical_manifest.pop("canonical_sha256", None)
    parts = {
        "template.json": (json.dumps(canonical_manifest, ensure_ascii=False,
                                      sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        "module.py": read_bounded_regular_file(
            directory / "module.py", PYTHON_FILE_LIMIT, "template module.py",
        ),
        "test_contract.py": read_bounded_regular_file(
            directory / "test_contract.py", PYTHON_FILE_LIMIT, "template test_contract.py",
        ),
    }
    digest = hashlib.sha256()
    for name in sorted(parts):
        content = parts[name]
        digest.update(name.encode("utf-8") + b"\0" + str(len(content)).encode("ascii") + b"\0")
        digest.update(content)
    return digest.hexdigest()


def _load_provenance(
    path: Path | None,
    claim_scope: str,
    *,
    idea: dict[str, Any],
) -> dict[str, Any]:
    if path is None:
        payload: dict[str, Any] = {
            "schema": PROVENANCE_SCHEMA, "scientific_claim_scope": claim_scope,
            "papers": [], "code_sources": [], "what_is_copied": [],
            "what_is_adapted": [], "what_is_new": [], "unresolved_questions": [],
        }
        if claim_scope == "original-hypothesis":
            payload["what_is_new"] = [
                f"机制：{idea['mechanism']}；假设：{idea['hypothesis']}"
            ]
    else:
        payload = _read_regular_json(Path(path))
        if payload.get("scientific_claim_scope") != claim_scope:
            raise ValueError("provenance 的 scientific_claim_scope 与 CLI 不一致")
    _validate_provenance(payload)
    return payload


def _validate_provenance(payload: dict[str, Any]) -> None:
    required = {
        "schema", "scientific_claim_scope", "papers", "code_sources",
        "what_is_copied", "what_is_adapted", "what_is_new", "unresolved_questions",
    }
    scope = payload.get("scientific_claim_scope")
    if set(payload) != required or payload.get("schema") != PROVENANCE_SCHEMA or scope not in SCOPES:
        raise ValueError("provenance schema 或字段无效")
    for field in ("papers", "code_sources", "what_is_copied", "what_is_adapted",
                  "what_is_new", "unresolved_questions"):
        if not isinstance(payload[field], list):
            raise ValueError(f"provenance {field} 必须是列表")
    for field in ("what_is_copied", "what_is_adapted", "what_is_new", "unresolved_questions"):
        if not all(_nonempty(value) for value in payload[field]):
            raise ValueError(f"provenance {field} 包含空项")
    verified_primary = False
    for paper in payload["papers"]:
        fields = {"role", "verification_status", "title", "authors", "year", "venue",
                  "url", "locator", "supported_claim"}
        if (
            not isinstance(paper, dict) or set(paper) != fields
            or paper.get("role") not in {"primary_mechanism", "supporting"}
            or paper.get("verification_status") not in {"verified", "unverified"}
            or not all(_nonempty(paper.get(field)) for field in
                       ("title", "venue", "url", "locator", "supported_claim"))
            or not isinstance(paper.get("authors"), list) or not paper["authors"]
            or not all(_nonempty(author) for author in paper["authors"])
            or not isinstance(paper.get("year"), int)
        ):
            raise ValueError("provenance paper 字段不完整")
        verified_primary |= (paper["role"] == "primary_mechanism"
                             and paper["verification_status"] == "verified")
    use_modes: list[str] = []
    for source in payload["code_sources"]:
        fields = {"repo_url", "commit", "source_files", "use_mode", "license_spdx",
                  "license_compatibility"}
        if not isinstance(source, dict) or set(source) != fields:
            raise ValueError("provenance code source 字段不完整")
        source_files = source.get("source_files")
        license_spdx = source.get("license_spdx")
        use_mode = source.get("use_mode")
        if (
            not _nonempty(source.get("repo_url"))
            or not _strict_fullmatch(r"[0-9a-fA-F]{40}", source.get("commit"))
            or use_mode not in {"copied", "adapted", "inspiration"}
            or not _nonempty(license_spdx)
            or not isinstance(source_files, list) or not source_files
        ):
            raise ValueError("code source license/commit/use_mode 不完整或不兼容")
        if use_mode in {"copied", "adapted"} and (
            license_spdx not in COMPATIBLE_CODE_LICENSES
            or source.get("license_compatibility") != "compatible"
        ):
            raise ValueError("code source license SPDX 未在兼容 allowlist 或不兼容")
        for item in source_files:
            if (not isinstance(item, dict) or set(item) != {"path", "symbols"}
                    or not isinstance(item.get("symbols"), list) or not item["symbols"]
                    or not all(_nonempty(symbol) for symbol in item["symbols"])):
                raise ValueError("code source source_files 不完整")
            normalized = normalize_safe_relative_path(
                item.get("path"), "provenance source_files.path",
            )
            if item["path"] != normalized:
                raise ValueError("provenance source_files.path 未规范化")
        use_modes.append(use_mode)
    copied_bound = "copied" in use_modes
    adapted_bound = "adapted" in use_modes
    if bool(payload["what_is_copied"]) != copied_bound:
        raise ValueError("what_is_copied 必须与 copied code source 双向绑定")
    if bool(payload["what_is_adapted"]) != adapted_bound:
        raise ValueError("what_is_adapted 必须与 adapted code source 双向绑定")
    if scope == "original-hypothesis" and not payload["what_is_new"]:
        raise ValueError("original-hypothesis 必须说明 what_is_new")
    if scope == "paper-derived" and not verified_primary:
        raise ValueError("paper-derived 必须包含 verified primary paper")


def _request_digest(
    idea_id: str,
    base_version_id: str,
    template_family: str,
    attachment_point: str,
    claim_scope: str,
    provenance: dict[str, Any],
) -> str:
    request = {
        "idea_id": idea_id,
        "base_version_id": base_version_id,
        "template_family": template_family,
        "attachment_point": attachment_point,
        "claim_scope": claim_scope,
        "provenance_sha256": _sha256(_json_bytes(provenance)),
    }
    return _sha256(_json_bytes(request))


def _create_transaction(
    control: Path, trial_id: str, asset_id: str, idea_id: str,
    version_id: str, attachment_point: str, template: dict[str, Any],
    provenance: dict[str, Any], request_digest: str,
) -> dict[str, Any]:
    _validate_control_directories(control)
    tx = uuid.uuid4().hex
    runtime = control / ".runtime"
    staging = runtime / f"new-trial-{tx}"
    marker_path = runtime / MARKER_NAME
    trial_stage = staging / trial_id
    asset_stage = staging / asset_id
    trial_final = control / "trials" / trial_id
    asset_final = control / "code-assets" / asset_id
    manifest = template["manifest"]
    _validate_provenance(provenance)
    status = "active"
    timestamp = _utc_now()
    reason = template["selection_reason"]
    module_bytes = read_bounded_regular_file(
        template["directory"] / "module.py", PYTHON_FILE_LIMIT, "template module.py",
    )
    contract_bytes = read_bounded_regular_file(
        template["directory"] / "test_contract.py",
        PYTHON_FILE_LIMIT,
        "template test_contract.py",
    )
    provenance_bytes = _json_bytes(provenance)
    trial = {
        "schema": TRIAL_SCHEMA, "id": trial_id, "status": status,
        "idea_id": idea_id, "base_version_id": version_id,
        "code_asset_id": asset_id, "template_family": manifest["family"],
        "attachment_point": attachment_point,
        "template_selection_reason": reason,
        "template_sha256": manifest["canonical_sha256"],
        "transaction_id": tx, "created_at": timestamp,
    }
    asset = {
        "schema": ASSET_SCHEMA, "id": asset_id, "status": status,
        "trial_id": trial_id, "template_family": manifest["family"],
        "attachment_point": attachment_point,
        "template_selection_reason": reason,
        "template_sha256": manifest["canonical_sha256"],
        "transaction_id": tx, "created_at": timestamp,
        "files": {"module.py": _sha256(module_bytes),
                  "test_contract.py": _sha256(contract_bytes),
                  "provenance.json": _sha256(provenance_bytes)},
    }
    framework_bytes = render_framework_html(
        asset, module_bytes, contract_bytes, provenance_bytes,
    )
    marker = {"schema": TRANSACTION_SCHEMA, "transaction_id": tx,
              "trial_id": trial_id, "asset_id": asset_id,
              "request_digest": request_digest}
    try:
        # 先发布项目锁保护下的所有权 marker，使任意后续硬中断都可恢复。
        atomic_write_json(marker_path, marker)
        staging.mkdir()
        trial_stage.mkdir()
        asset_stage.mkdir()
        atomic_write_json(trial_stage / "trial.json", trial)
        atomic_write_json(asset_stage / "asset.json", asset)
        (asset_stage / "module.py").write_bytes(module_bytes)
        (asset_stage / "test_contract.py").write_bytes(contract_bytes)
        atomic_write_json(asset_stage / "provenance.json", provenance)
        (asset_stage / "framework.html").write_bytes(framework_bytes)
        atomic_write_json(staging / "transaction.json", marker)
        if os.path.lexists(trial_final) or os.path.lexists(asset_final):
            raise FileExistsError("Trial 或 CodeAsset ID 已存在，拒绝覆盖")
        _validate_control_directories(control)
        os.replace(trial_stage, trial_final)
        _validate_control_directories(control)
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
            recovered = _recover_transaction(control)
            if recovered is not None:
                return recovered[0]
        else:
            _cleanup_unpublished_staging(staging, trial_id, asset_id)
        raise


def _recover_transaction(control: Path) -> tuple[dict[str, Any], str] | None:
    _validate_control_directories(control)
    marker_path = control / ".runtime" / MARKER_NAME
    if not os.path.lexists(marker_path):
        return None
    marker = _read_regular_json(marker_path)
    if not _strict_fullmatch(r"[0-9a-f]{64}", marker.get("request_digest")):
        raise ValueError(
            "new-trial 事务 marker 缺少有效 request_digest，拒绝猜测，需人工处理"
        )
    if (set(marker) != {"schema", "transaction_id", "trial_id", "asset_id",
                       "request_digest"}
            or marker.get("schema") != TRANSACTION_SCHEMA
            or not _strict_fullmatch(r"[0-9a-f]{32}", marker.get("transaction_id"))
            or not _strict_fullmatch(TRIAL_ID, marker.get("trial_id"))
            or not _strict_fullmatch(ASSET_ID, marker.get("asset_id"))):
        raise ValueError("new-trial 事务 marker 无效，拒绝自动清理")
    tx = marker["transaction_id"]
    residuals: list[str] = []
    trial_path = control / "trials" / marker["trial_id"]
    asset_path = control / "code-assets" / marker["asset_id"]
    trial_payload = _owned_complete_object(
        trial_path, "trial.json", tx, ("trial.json",),
    )
    asset_payload = _owned_complete_asset(asset_path, tx)
    committed = trial_payload is not None and asset_payload is not None
    if not committed:
        _remove_owned_object(trial_path, "trial.json", tx, ("trial.json",), residuals)
        _remove_owned_object(asset_path, "asset.json", tx, ASSET_FILES, residuals)
    staging = control / ".runtime" / f"new-trial-{tx}"
    if os.path.lexists(staging):
        transaction = staging / "transaction.json"
        if is_link_or_reparse(staging) or not staging.is_dir():
            residuals.append(str(staging))
        elif not os.path.lexists(transaction):
            _cleanup_unpublished_staging(
                staging, marker["trial_id"], marker["asset_id"]
            )
        elif is_link_or_reparse(transaction) or not transaction.is_file():
            residuals.append(str(transaction))
        else:
            staged_marker = _read_regular_json(transaction)
            if staged_marker != marker:
                residuals.append(str(staging))
            else:
                _remove_owned_object(staging / marker["trial_id"], "trial.json", tx,
                                     ("trial.json",), residuals)
                _remove_owned_object(staging / marker["asset_id"], "asset.json", tx,
                                     ASSET_FILES, residuals)
                transaction.unlink()
                try:
                    staging.rmdir()
                except OSError:
                    residuals.append(str(staging))
    if residuals:
        raise RuntimeError("事务恢复保留了 unknown/用户文件，需人工处理：" + "、".join(residuals))
    marker_path.unlink()
    if committed:
        return trial_payload, marker["request_digest"]
    return None


def _cleanup_unpublished_staging(staging: Path, trial_id: str, asset_id: str) -> None:
    """清理尚未发布 marker 的工具 staging，同时保留任何未知文件。"""
    if not os.path.lexists(staging):
        return
    staging_identity = _real_directory_identity(staging)
    if staging_identity is None:
        raise RuntimeError(f"准备阶段 staging 不是 real 普通目录，拒绝清理：{staging}")
    residuals: list[str] = []
    for directory, known_files in (
        (staging / trial_id, ("trial.json",)),
        (staging / asset_id, ASSET_FILES),
    ):
        if not os.path.lexists(directory):
            continue
        _remove_staging_child(
            staging, staging_identity, directory, known_files, residuals,
        )
    transaction = staging / "transaction.json"
    if os.path.lexists(transaction):
        if is_link_or_reparse(transaction) or not transaction.is_file():
            residuals.append(str(transaction))
        else:
            if not _directory_identity_matches(staging, staging_identity):
                raise RuntimeError(f"准备阶段 staging 清理前发生替换：{staging}")
            transaction.unlink()
    if not _directory_identity_matches(staging, staging_identity):
        raise RuntimeError(f"准备阶段 staging 删除前发生替换：{staging}")
    try:
        staging.rmdir()
    except OSError:
        residuals.append(str(staging))
    if residuals:
        raise RuntimeError(
            "准备阶段清理保留了 unknown/用户文件，需人工处理：" + "、".join(residuals)
        )


def _remove_staging_child(
    staging: Path,
    staging_identity: tuple[int, int],
    directory: Path,
    known_files: tuple[str, ...],
    residuals: list[str],
) -> None:
    child_identity = _real_directory_identity(directory)
    if child_identity is None:
        residuals.append(str(directory))
        return
    for filename in known_files:
        candidate = directory / filename
        if not os.path.lexists(candidate):
            continue
        if is_link_or_reparse(candidate) or not candidate.is_file():
            residuals.append(str(candidate))
            continue
        if (
            not _directory_identity_matches(staging, staging_identity)
            or not _directory_identity_matches(directory, child_identity)
        ):
            residuals.append(str(directory))
            return
        # Windows 的 pathlib.unlink 没有 dir_fd；双 identity 复核封死现有检查窗口，
        # 但复核与 unlink 系统调用之间仍存在无法完全消除的极小竞态。
        candidate.unlink()
    if (
        not _directory_identity_matches(staging, staging_identity)
        or not _directory_identity_matches(directory, child_identity)
    ):
        residuals.append(str(directory))
        return
    try:
        directory.rmdir()
    except OSError:
        residuals.append(str(directory))


def _remove_owned_object(path: Path, anchor: str, tx: str,
                         known_files: tuple[str, ...], residuals: list[str]) -> None:
    if not os.path.lexists(path):
        return
    identity = _real_directory_identity(path)
    if identity is None:
        residuals.append(str(path))
        return
    anchor_path = path / anchor
    try:
        payload = _read_regular_json(anchor_path)
    except ValueError:
        residuals.append(str(path))
        return
    if payload.get("transaction_id") != tx:
        residuals.append(str(path))
        return
    for filename in known_files:
        candidate = path / filename
        if os.path.lexists(candidate):
            if is_link_or_reparse(candidate) or not candidate.is_file():
                residuals.append(str(candidate))
            else:
                if not _directory_identity_matches(path, identity):
                    residuals.append(str(path))
                    return
                candidate.unlink()
    if not _directory_identity_matches(path, identity):
        residuals.append(str(path))
        return
    try:
        path.rmdir()
    except OSError:
        residuals.append(str(path))


def _owned_complete_object(
    path: Path, anchor: str, tx: str, known_files: tuple[str, ...],
) -> dict[str, Any] | None:
    if not os.path.lexists(path) or _real_directory_identity(path) is None:
        return None
    try:
        payload = _read_regular_json(path / anchor)
    except ValueError:
        return None
    if payload.get("transaction_id") != tx:
        return None
    for filename in known_files:
        candidate = path / filename
        if is_link_or_reparse(candidate) or not candidate.is_file():
            return None
    return payload


def _owned_complete_asset(path: Path, tx: str) -> dict[str, Any] | None:
    if not os.path.lexists(path) or _real_directory_identity(path) is None:
        return None
    try:
        payload = _read_regular_json(path / "asset.json")
    except ValueError:
        return None
    files = (
        FORMAL_ASSET_FILES
        if payload.get("schema") == FORMAL_ASSET_SCHEMA
        else V1_ASSET_FILES
    )
    if payload.get("transaction_id") != tx:
        return None
    for filename in files:
        candidate = path / filename
        if is_link_or_reparse(candidate) or not candidate.is_file():
            return None
    return payload


def _real_directory_identity(path: Path) -> tuple[int, int] | None:
    if is_link_or_reparse(path) or not path.is_dir():
        return None
    try:
        metadata = path.lstat()
    except OSError:
        return None
    return metadata.st_dev, metadata.st_ino


def _directory_identity_matches(path: Path, expected: tuple[int, int]) -> bool:
    return _real_directory_identity(path) == expected


def _validate_trial_shape(payload: dict[str, Any], expected_id: str) -> None:
    if payload.get("schema") == FORMAL_TRIAL_SCHEMA:
        _validate_formal_trial_shape(payload, expected_id)
        return
    fields = {"schema", "id", "status", "idea_id", "base_version_id",
              "code_asset_id", "template_family", "attachment_point",
              "template_selection_reason", "template_sha256", "transaction_id",
              "created_at"}
    if (set(payload) != fields
            or payload.get("schema") != TRIAL_SCHEMA or payload.get("id") != expected_id
            or payload.get("status") != "active"
            or not _strict_fullmatch(IDEA_ID, payload.get("idea_id"))
            or not _strict_fullmatch(VERSION_ID, payload.get("base_version_id"))
            or not _strict_fullmatch(ASSET_ID, payload.get("code_asset_id"))
            or not all(_nonempty(payload.get(field)) for field in
                       ("template_family", "attachment_point", "template_selection_reason"))
            or not _strict_fullmatch(r"[0-9a-f]{64}", payload.get("template_sha256"))
            or not _strict_fullmatch(r"[0-9a-f]{32}", payload.get("transaction_id"))
            or not _is_utc_iso(payload.get("created_at"))):
        raise ValueError(f"Trial 记录无效：{expected_id}")


def _validate_asset_shape(payload: dict[str, Any], expected_id: str) -> None:
    if payload.get("schema") == FORMAL_ASSET_SCHEMA:
        _validate_formal_asset_shape(payload, expected_id)
        return
    fields = {"schema", "id", "status", "trial_id", "template_family",
              "attachment_point", "template_selection_reason", "template_sha256",
              "transaction_id", "created_at", "files"}
    if (set(payload) != fields
            or payload.get("schema") != ASSET_SCHEMA or payload.get("id") != expected_id
            or payload.get("status") != "active"
            or not _strict_fullmatch(TRIAL_ID, payload.get("trial_id"))
            or not all(_nonempty(payload.get(field)) for field in
                       ("template_family", "attachment_point", "template_selection_reason"))
            or not _strict_fullmatch(r"[0-9a-f]{64}", payload.get("template_sha256"))
            or not _strict_fullmatch(r"[0-9a-f]{32}", payload.get("transaction_id"))
            or not _is_utc_iso(payload.get("created_at"))
            or not isinstance(payload.get("files"), dict)
            or set(payload["files"]) != {"module.py", "test_contract.py", "provenance.json"}
            or not all(_strict_fullmatch(r"[0-9a-f]{64}", value)
                       for value in payload["files"].values())):
        raise ValueError(f"CodeAsset 记录无效：{expected_id}")


def _validate_formal_trial_shape(payload: dict[str, Any], expected_id: str) -> None:
    from .project_templates import TEMPLATE_ID

    legacy_fields = {
        "schema", "id", "status", "idea_id", "base_version_id",
        "code_asset_id", "template_id", "attachment_point", "transaction_id",
        "created_at",
    }
    current_fields = legacy_fields | {
        "idea_revision", "implementation_mapping_sha256",
    }
    has_frozen_mapping = set(payload) == current_fields
    if (
        set(payload) not in (legacy_fields, current_fields)
        or payload.get("id") != expected_id
        or payload.get("status") != "active"
        or not _strict_fullmatch(IDEA_ID, payload.get("idea_id"))
        or not _strict_fullmatch(VERSION_ID, payload.get("base_version_id"))
        or not _strict_fullmatch(ASSET_ID, payload.get("code_asset_id"))
        or not _strict_fullmatch(TEMPLATE_ID, payload.get("template_id"))
        or not _nonempty(payload.get("attachment_point"))
        or (
            has_frozen_mapping
            and (
                type(payload.get("idea_revision")) is not int
                or payload["idea_revision"] < 1
                or not _strict_fullmatch(
                    r"[0-9a-f]{64}",
                    payload.get("implementation_mapping_sha256"),
                )
            )
        )
        or not _strict_fullmatch(r"[0-9a-f]{32}", payload.get("transaction_id"))
        or not _is_utc_iso(payload.get("created_at"))
    ):
        raise ValueError(f"正式 Trial 记录无效：{expected_id}")


def _validate_formal_asset_shape(payload: dict[str, Any], expected_id: str) -> None:
    from .project_templates import TEMPLATE_ID

    fields = {
        "schema", "id", "status", "trial_id", "template_id",
        "attachment_point", "transaction_id", "created_at", "files",
        "external_code_ref",
    }
    files = payload.get("files")
    if (
        set(payload) != fields
        or payload.get("id") != expected_id
        or payload.get("status") != "active"
        or not _strict_fullmatch(TRIAL_ID, payload.get("trial_id"))
        or not _strict_fullmatch(TEMPLATE_ID, payload.get("template_id"))
        or not _nonempty(payload.get("attachment_point"))
        or not _strict_fullmatch(r"[0-9a-f]{32}", payload.get("transaction_id"))
        or not _is_utc_iso(payload.get("created_at"))
        or not isinstance(files, dict)
        or set(files) != set(FORMAL_ASSET_DIGEST_FILES)
        or not all(_strict_fullmatch(r"[0-9a-f]{64}", value) for value in files.values())
    ):
        raise ValueError(f"正式 CodeAsset 记录无效：{expected_id}")
    _validate_external_code_ref(payload.get("external_code_ref"))


def _validate_external_code_ref(value: object) -> None:
    if value is None:
        return
    if (
        not isinstance(value, dict)
        or set(value) != {"repo_url", "commit", "relative_path", "digest"}
        or not _nonempty(value.get("repo_url"))
        or not _strict_fullmatch(r"[0-9a-f]{40}", value.get("commit"))
        or not _strict_fullmatch(r"sha256:[0-9a-f]{64}", value.get("digest"))
    ):
        raise ValueError("external_code_ref 结构无效")
    normalized = normalize_safe_relative_path(
        value.get("relative_path"), "external_code_ref.relative_path",
    )
    if value["relative_path"] != normalized:
        raise ValueError("external_code_ref.relative_path 未规范化")


def _validate_composition(payload: dict[str, Any], *, asset_id: str) -> None:
    from .project_templates import TEMPLATE_ID

    fields = {
        "schema", "template_id", "base_version_id", "base_commit",
        "inherited_code_asset_ids", "new_code_asset_id", "attachment_point",
        "target_path",
    }
    inherited = payload.get("inherited_code_asset_ids")
    if (
        set(payload) != fields
        or payload.get("schema") != COMPOSITION_SCHEMA
        or not _strict_fullmatch(TEMPLATE_ID, payload.get("template_id"))
        or not _strict_fullmatch(VERSION_ID, payload.get("base_version_id"))
        or not _strict_fullmatch(r"[0-9a-f]{40}", payload.get("base_commit"))
        or not isinstance(inherited, list)
        or len(inherited) != len(set(str(item) for item in inherited))
        or not all(isinstance(item, str) and ASSET_ID.fullmatch(item) for item in inherited)
        or payload.get("new_code_asset_id") != asset_id
        or not _nonempty(payload.get("attachment_point"))
    ):
        raise ValueError(f"composition 记录无效：{asset_id}")
    target = normalize_safe_relative_path(
        payload.get("target_path"), "composition.target_path",
    )
    if payload["target_path"] != target:
        raise ValueError(f"composition.target_path 未规范化：{asset_id}")


def _object_directories(directory: Path, pattern: re.Pattern[str], issues: list[str]) -> list[Path]:
    _require_real_directory(directory, "对象父目录")
    result: list[Path] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if pattern.fullmatch(path.name) is None:
            continue
        if is_link_or_reparse(path) or not path.is_dir():
            issues.append(f"对象路径不是普通目录：{path}")
        else:
            result.append(path)
    return result


def _next_directory_id(directory: Path, prefix: str, pattern: re.Pattern[str]) -> str:
    _require_real_directory(directory, "对象 ID 父目录")
    numbers: list[int] = []
    for path in directory.iterdir():
        if pattern.fullmatch(path.name) is None:
            continue
        if is_link_or_reparse(path) or not path.is_dir():
            raise ValueError(f"对象 ID 路径不是普通目录：{path}")
        numbers.append(int(path.name.split("-")[1]))
    number = max(numbers, default=0) + 1
    if number > 9999:
        raise ValueError(f"{prefix} ID 已耗尽")
    return f"{prefix}-{number:04d}"


def _read_regular_json(path: Path) -> dict[str, Any]:
    if is_link_or_reparse(path) or not path.is_file():
        raise ValueError(f"JSON 来源不是普通文件：{path}")
    try:
        payload = read_bounded_json_object(path)
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取有效 JSON：{path}；{error}") from error
    return payload


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _strict_fullmatch(
    pattern: str | re.Pattern[str], value: object,
) -> bool:
    if not isinstance(value, str):
        return False
    if isinstance(pattern, str):
        return re.fullmatch(pattern, value) is not None
    return pattern.fullmatch(value) is not None


def _validate_transaction_directories(project_root: Path) -> None:
    control = project_root / ".experiment-workflow"
    _validate_control_directories(control)


def _validate_control_directories(control: Path) -> None:
    _require_real_directory(control, "项目控制目录")
    for name in ("trials", "code-assets", ".runtime"):
        _require_real_directory(control / name, "事务父目录")


def _require_real_directory(path: Path, label: str) -> None:
    if is_link_or_reparse(path) or not path.is_dir():
        raise ValueError(f"{label}不是 real 普通目录，拒绝访问：{path}")
