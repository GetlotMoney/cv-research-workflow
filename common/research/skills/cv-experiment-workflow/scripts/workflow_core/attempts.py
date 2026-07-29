from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from pathlib import Path
from typing import Any

from .io import atomic_write_json, parse_json_object_bytes, read_bounded_regular_file
from .locking import project_write_lock
from .project import PROJECT_SCHEMA, PROJECT_SCHEMA_V2, is_link_or_reparse
from .rendering import STRUCTURED_FILE_LIMIT, validated_render_inputs
from .records import (
    VERSION_ID,
    VERSION_SCHEMA,
    VERSION_SCHEMA_V2,
    _create_record,
    _initialized_project,
    _is_utc_iso,
    _next_id,
    _read_object,
    _required_text,
    _utc_now,
    _validate_project,
    normalize_safe_relative_path,
    validate_version,
)
from .templates import (
    ASSET_ID,
    FORMAL_ASSET_SCHEMA,
    FORMAL_TRIAL_SCHEMA,
    TRIAL_ID,
    _sha256,
    _validate_asset_shape,
    _validate_composition,
    _validate_project_trials_locked,
    _validate_provenance,
    _validate_trial_shape,
)


ATTEMPT_SCHEMA = "cv-experiment-workflow.attempt.v1"
RESULT_SCHEMA_V2 = "cv-experiment-workflow.result.v2"
ATTEMPT_ID = re.compile(r"ATTEMPT-[0-9]{4}")
ATTEMPT_TYPES = {"innovation", "tune", "ablation", "reproduction"}
DECISIONS = {"accept", "reject", "inconclusive"}
IMPLEMENTATION_STATUSES = {"valid", "invalid", "uncertain"}
HYPOTHESIS_STATUSES = {
    "supported", "not_supported", "inconclusive", "not_evaluated",
}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
ARTIFACT_DIGEST = re.compile(r"(?:sha256:)?[0-9a-f]{64}")


def new_attempt(
    project: Path,
    target_id: str,
    attempt_type: str,
    seed: int,
    command: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    if attempt_type not in ATTEMPT_TYPES:
        raise ValueError(f"Attempt type 无效：{attempt_type}")
    if type(seed) is not int:
        raise ValueError("seed 必须是一个整数")
    command = _required_text(command, "command")
    _validate_json_object(config, "config")
    root = _initialized_project(project)
    _require_real_directory(root / ".experiment-workflow" / "attempts", "attempts 父目录")
    with project_write_lock(root):
        control = _validate_project(root)
        _validate_attempt_directories(control)
        _validate_project_attempt_ledger_locked(control)
        target = _resolve_target(control, target_id, visited=set())
        from .validation import validate_boundaries_locked
        validate_boundaries_locked(control)
        attempt_id = _next_id(control / "attempts", "ATTEMPT", ATTEMPT_ID)
        timestamp = _utc_now()
        payload: dict[str, Any] = {
            "schema": ATTEMPT_SCHEMA,
            "id": attempt_id,
            "status": "planned",
            "type": attempt_type,
            "seed": seed,
            "command": command,
            "config": config,
            "base_version_id": target["base_version_id"],
            "target": {
                "kind": target["kind"],
                "id": target_id,
                "facts_digest": target["facts_digest"],
            },
            "ordered_code_asset_ids": target["ordered_code_asset_ids"],
            "frozen_identity_digest": "",
            "created_at": timestamp,
        }
        payload["frozen_identity_digest"] = _identity_digest(payload)
        validate_attempt(payload, expected_id=attempt_id)
        _create_record(control / "attempts" / f"{attempt_id}.json", payload)
        return payload


def record_result(
    project: Path,
    attempt_id: str,
    metrics: dict[str, Any],
    decision: str | None,
    conclusion: str,
    artifacts: list[Any],
    limitations: list[Any],
    *,
    implementation_status: str | None = None,
    hypothesis_status: str | None = None,
) -> dict[str, Any]:
    _require_attempt_id(attempt_id)
    _validate_json_object(metrics, "metrics")
    has_axes = implementation_status is not None or hypothesis_status is not None
    if decision is not None and has_axes:
        raise ValueError("legacy decision 与 Result 双轴不得混用")
    if decision is None and not has_axes:
        raise ValueError("必须提供 legacy decision 或完整 Result 双轴")
    if has_axes:
        if implementation_status not in IMPLEMENTATION_STATUSES:
            raise ValueError(f"implementation_status 无效：{implementation_status}")
        if hypothesis_status not in HYPOTHESIS_STATUSES:
            raise ValueError(f"hypothesis_status 无效：{hypothesis_status}")
        derived_decision = _derived_decision(implementation_status, hypothesis_status)
    else:
        if decision not in DECISIONS:
            raise ValueError(f"decision 无效：{decision}")
        derived_decision = decision
    conclusion = _required_text(conclusion, "conclusion")
    normalized_artifacts = _normalize_artifacts(artifacts)
    normalized_limitations = _normalize_limitations(limitations)
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root)
        validate_project_workflow_locked(control)
        path = control / "attempts" / f"{attempt_id}.json"
        payload = _read_object(path)
        validate_attempt(payload, expected_id=attempt_id)
        requested_result = {
            "metrics": metrics,
            "artifacts": normalized_artifacts,
            "decision": derived_decision,
            "conclusion": conclusion,
            "limitations": normalized_limitations,
        }
        if has_axes:
            requested_result = {
                "schema": RESULT_SCHEMA_V2,
                **requested_result,
                "implementation_status": implementation_status,
                "hypothesis_status": hypothesis_status,
            }
        if payload["status"] == "completed":
            existing_result = {
                key: value
                for key, value in payload["result"].items()
                if key != "recorded_at"
            }
            if _canonical_digest(existing_result) == _canonical_digest(requested_result):
                return payload
            raise ValueError(f"completed Attempt 不得以不同 Result 覆盖：{attempt_id}")
        completed = dict(payload)
        completed["status"] = "completed"
        completed["result"] = {
            **requested_result,
            "recorded_at": _utc_now(),
        }
        validate_attempt(completed, expected_id=attempt_id)
        _require_regular_file(path, "Attempt 对象")
        atomic_write_json(path, completed, transaction_id=uuid.uuid4().hex)
        return completed


def promotion_check(project: Path, attempt_id: str) -> dict[str, Any]:
    _require_attempt_id(attempt_id)
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root)
        from .validation import preflight_workflow_command_locked
        preflight_workflow_command_locked(control)
        return _promotion_evaluation_locked(control, attempt_id)


def promote(project: Path, attempt_ids: list[str]) -> dict[str, Any]:
    if not attempt_ids:
        raise ValueError("promote 至少需要一个 --attempt")
    unique_ids = _stable_unique(attempt_ids)
    for attempt_id in unique_ids:
        _require_attempt_id(attempt_id)
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root)
        from .validation import preflight_workflow_command_locked
        preflight_workflow_command_locked(control)
        evaluations = [
            _promotion_evaluation_locked(control, attempt_id) for attempt_id in unique_ids
        ]
        shortfalls = [item for item in evaluations if not item["eligible"]]
        if shortfalls:
            details = "；".join(
                f"{item['attempt_id']}: {'，'.join(item['reasons'])}"
                for item in shortfalls
            )
            raise ValueError(f"Confirmation policy 尚未满足：{details}")
        evidence_ids = sorted({
            evidence_id
            for item in evaluations
            for evidence_id in item["qualified_attempt_ids"]
        })
        attempts = [
            _intrinsic_promotion_attempt_locked(control, attempt_id)
            for attempt_id in evidence_ids
        ]
        base_ids = {attempt["base_version_id"] for attempt in attempts}
        if len(base_ids) != 1:
            raise ValueError("所有 promotion Attempt 必须具有同一 base_version_id")
        base_id = next(iter(base_ids))
        base = _read_object(control / "versions" / f"{base_id}.json")
        validate_version(base, expected_id=base_id)
        if base["status"] != "active":
            raise ValueError(f"promotion 基础 Version 必须为 active：{base_id}")
        ordered_assets = _stable_unique(
            [asset_id for attempt in attempts for asset_id in attempt["ordered_code_asset_ids"]]
        )
        existing = _matching_promotion_version(
            control,
            base_id=base_id,
            attempt_ids=evidence_ids,
            asset_ids=ordered_assets,
        )
        if existing is not None:
            return existing
        version_id = _next_id(control / "versions", "VER", VERSION_ID)
        timestamp = _utc_now()
        payload: dict[str, Any] = {
            "schema": VERSION_SCHEMA,
            "id": version_id,
            "status": "draft",
            "name": f"Promotion from {base_id}",
            "base_version_id": base_id,
            "accepted_attempt_ids": evidence_ids,
            "ordered_code_asset_ids": ordered_assets,
            "code_sources": list(base["code_sources"]),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        validate_version(payload, expected_id=version_id)
        _create_record(control / "versions" / f"{version_id}.json", payload)
        return payload


def validate_project_attempts(project: Path) -> dict[str, int]:
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root)
        return _validate_project_attempts_locked(control)


def validate_project_workflow(project: Path) -> dict[str, Any]:
    root = _initialized_project(project)
    with project_write_lock(root):
        control = root / ".experiment-workflow"
        schema = _read_object(control / "project.json").get("schema")
        if schema == PROJECT_SCHEMA:
            return validate_project_workflow_locked(
                _validate_project(root, expected_schema=PROJECT_SCHEMA)
            )
        if schema == PROJECT_SCHEMA_V2:
            control = _validate_project(
                root,
                expected_schema=PROJECT_SCHEMA_V2,
            )
            from .policy import load_adapter
            from .validation import validate_boundaries_locked
            load_adapter(control)
            return validate_boundaries_locked(control)
        raise ValueError(f"项目元数据无效：{control / 'project.json'}")


def validate_project_workflow_locked(control: Path) -> dict[str, Any]:
    """在调用者持有 project lock 时完成一次完整工作流快照校验。"""
    from .policy import load_adapter
    from .records import validate_project_ideas_locked
    load_adapter(control)
    payload = validate_project_ideas_locked(control)
    payload.update(_validate_project_trials_locked(control))
    payload.update(_validate_project_attempts_locked(control))
    from .validation import validate_boundaries_locked
    payload.update(validate_boundaries_locked(control))
    return payload


def _validate_project_attempts_locked(control: Path) -> dict[str, int]:
    counts, _attempts = _validate_project_attempt_ledger_locked(control)
    return counts


def _validate_project_attempt_ledger_locked(
    control: Path,
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    _validate_attempt_directories(control)
    versions: dict[str, dict[str, Any]] = {}
    for path in _json_records(control / "versions", VERSION_ID):
        payload = _read_object(path)
        validate_version(payload, expected_id=path.stem)
        versions[path.stem] = payload
    attempts: dict[str, dict[str, Any]] = {}
    for path in _json_records(control / "attempts", ATTEMPT_ID):
        payload = _read_object(path)
        validate_attempt(payload, expected_id=path.stem)
        attempts[path.stem] = payload
    resolution_cache: dict[str, dict[str, Any]] = {}
    for attempt_id, attempt in attempts.items():
        _verify_attempt_frozen(
            control,
            attempt,
            visited={attempt_id},
            cache=resolution_cache,
        )
    for version_id, version in versions.items():
        if version["status"] == "draft":
            _validate_promotion_version(control, version_id, version)
        elif version["schema"] == VERSION_SCHEMA_V2:
            _validate_active_promotion_version(control, version_id, version)
    counts = {
        "attempts": len(attempts),
        "results": sum(item["status"] == "completed" for item in attempts.values()),
        "legacy_results": sum(
            item["status"] == "completed" and "schema" not in item["result"]
            for item in attempts.values()
        ),
        "versions": len(versions),
        "draft_versions": sum(item["status"] == "draft" for item in versions.values()),
    }
    return counts, attempts


def validate_attempt(payload: dict[str, Any], *, expected_id: str) -> None:
    planned_fields = {
        "schema", "id", "status", "type", "seed", "command", "config",
        "base_version_id", "target", "ordered_code_asset_ids",
        "frozen_identity_digest", "created_at",
    }
    status = payload.get("status")
    expected_fields = planned_fields if status == "planned" else planned_fields | {"result"}
    target = payload.get("target")
    assets = payload.get("ordered_code_asset_ids")
    if (
        set(payload) != expected_fields
        or payload.get("schema") != ATTEMPT_SCHEMA
        or payload.get("id") != expected_id
        or ATTEMPT_ID.fullmatch(expected_id) is None
        or status not in {"planned", "completed"}
        or payload.get("type") not in ATTEMPT_TYPES
        or type(payload.get("seed")) is not int
        or not isinstance(payload.get("command"), str)
        or not payload["command"].strip()
        or payload["command"] != payload["command"].strip()
        or VERSION_ID.fullmatch(str(payload.get("base_version_id"))) is None
        or not isinstance(target, dict)
        or set(target) != {"kind", "id", "facts_digest"}
        or target.get("kind") not in {"trial", "version", "attempt"}
        or not _target_id_matches(target.get("kind"), target.get("id"))
        or DIGEST.fullmatch(str(target.get("facts_digest"))) is None
        or not isinstance(assets, list)
        or len(assets) != len(set(str(item) for item in assets))
        or not all(isinstance(item, str) and ASSET_ID.fullmatch(item) for item in assets)
        or DIGEST.fullmatch(str(payload.get("frozen_identity_digest"))) is None
        or not _is_utc_iso(payload.get("created_at"))
    ):
        raise ValueError(f"Attempt 记录无效：{expected_id}")
    _validate_json_object(payload.get("config"), "config")
    if payload["frozen_identity_digest"] != _identity_digest(payload):
        raise ValueError(f"Attempt frozen identity digest 不匹配：{expected_id}")
    if status == "completed":
        _validate_result(payload.get("result"), expected_id)


def _promotion_check_locked(control: Path, attempt_id: str) -> dict[str, Any]:
    return _intrinsic_promotion_attempt_locked(control, attempt_id)


def _intrinsic_promotion_attempt_locked(
    control: Path, attempt_id: str,
) -> dict[str, Any]:
    path = control / "attempts" / f"{attempt_id}.json"
    attempt = _read_object(path)
    validate_attempt(attempt, expected_id=attempt_id)
    if attempt["status"] != "completed":
        raise ValueError(f"Attempt {attempt_id} status={attempt['status']}，尚未 completed")
    if not _result_is_promotion_eligible(attempt["result"]):
        decision = attempt["result"]["decision"]
        raise ValueError(f"Attempt {attempt_id} decision={decision}，不可 promotion")
    _verify_attempt_frozen(control, attempt, visited={attempt_id})
    return attempt


def _promotion_evaluation_locked(control: Path, attempt_id: str) -> dict[str, Any]:
    attempt = _intrinsic_promotion_attempt_locked(control, attempt_id)
    from .policy import get_confirmation_policy

    policy = get_confirmation_policy(control)
    root_cache: dict[str, tuple[str, str]] = {}
    resolution_cache: dict[str, dict[str, Any]] = {}
    root_identity = _root_target_identity(control, attempt, cache=root_cache)
    eligible_attempts: list[dict[str, Any]] = []
    for candidate_path in _json_records(control / "attempts", ATTEMPT_ID):
        candidate = _read_object(candidate_path)
        validate_attempt(candidate, expected_id=candidate_path.stem)
        if (
            candidate["status"] != "completed"
            or candidate["base_version_id"] != attempt["base_version_id"]
            or not _result_is_promotion_eligible(candidate["result"])
            or _root_target_identity(control, candidate, cache=root_cache) != root_identity
        ):
            continue
        _verify_attempt_frozen(
            control, candidate, visited={candidate["id"]}, cache=resolution_cache,
        )
        eligible_attempts.append(candidate)
    distinct_seeds = {item["seed"] for item in eligible_attempts}
    accepted_count = len(eligible_attempts)
    seed_count = len(distinct_seeds)
    reasons: list[str] = []
    if accepted_count < policy["minimum_accepted_attempts"]:
        reasons.append(
            f"accepted attempts {accepted_count}/{policy['minimum_accepted_attempts']}"
        )
    if seed_count < policy["minimum_distinct_seeds"]:
        reasons.append(
            f"distinct seeds {seed_count}/{policy['minimum_distinct_seeds']}"
        )
    return {
        "eligible": not reasons,
        "attempt_id": attempt_id,
        "base_version_id": attempt["base_version_id"],
        "ordered_code_asset_ids": list(attempt["ordered_code_asset_ids"]),
        "reason": (
            "completed Attempt 满足结果门禁、冻结事实与 Confirmation policy"
            if not reasons else "Confirmation policy 尚未满足"
        ),
        "reasons": reasons,
        "counts": {
            "accepted_attempts": accepted_count,
            "distinct_seeds": seed_count,
            "required_accepted_attempts": policy["minimum_accepted_attempts"],
            "required_distinct_seeds": policy["minimum_distinct_seeds"],
        },
        "legacy_evidence": any(
            "schema" not in item["result"] for item in eligible_attempts
        ),
        "qualified_attempt_ids": sorted(item["id"] for item in eligible_attempts),
    }


def _root_target_identity(
    control: Path,
    attempt: dict[str, Any],
    *,
    cache: dict[str, tuple[str, str]] | None = None,
) -> tuple[str, str]:
    if cache is not None and attempt["id"] in cache:
        return cache[attempt["id"]]
    target = attempt["target"]
    seen = {attempt["id"]}
    chain = [attempt["id"]]
    while target["kind"] == "attempt":
        target_id = target["id"]
        if target_id in seen:
            raise ValueError(f"Attempt target 出现循环引用：{target_id}")
        if cache is not None and target_id in cache:
            identity = cache[target_id]
            break
        seen.add(target_id)
        parent = _read_object(control / "attempts" / f"{target_id}.json")
        validate_attempt(parent, expected_id=target_id)
        if parent["status"] != "completed":
            raise ValueError(f"Attempt target 必须为 completed：{target_id}")
        chain.append(target_id)
        target = parent["target"]
    else:
        identity = (str(target["kind"]), str(target["id"]))
    if cache is not None:
        for attempt_id in chain:
            cache[attempt_id] = identity
    return identity


def _result_is_promotion_eligible(result: dict[str, Any]) -> bool:
    if result.get("schema") == RESULT_SCHEMA_V2:
        return (
            result["implementation_status"] == "valid"
            and result["decision"] == "accept"
        )
    return result["decision"] == "accept"


def _verify_attempt_frozen(
    control: Path,
    attempt: dict[str, Any],
    *,
    visited: set[str],
    cache: dict[str, dict[str, Any]] | None = None,
) -> None:
    target = attempt["target"]
    resolved = _resolve_target(
        control,
        target["id"],
        visited=visited,
        cache=cache,
    )
    _assert_attempt_target_matches(attempt, resolved)
    if cache is not None:
        cache[attempt["id"]] = _attempt_target_resolution(attempt)


def _assert_attempt_target_matches(
    attempt: dict[str, Any], resolved: dict[str, Any],
) -> None:
    target = attempt["target"]
    if (
        target["kind"] != resolved["kind"]
        or attempt["base_version_id"] != resolved["base_version_id"]
        or attempt["ordered_code_asset_ids"] != resolved["ordered_code_asset_ids"]
        or target["facts_digest"] != resolved["facts_digest"]
    ):
        raise ValueError(f"Attempt 冻结 target 事实漂移：{attempt['id']}")


def _resolve_target(
    control: Path,
    target_id: str,
    *,
    visited: set[str],
    cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    chain: list[dict[str, Any]] = []
    seen = set(visited)
    current_id = target_id
    while ATTEMPT_ID.fullmatch(current_id):
        if current_id in seen:
            raise ValueError(f"Attempt target 出现循环引用：{current_id}")
        seen.add(current_id)
        if cache is not None and current_id in cache:
            resolved = cache[current_id]
            break
        attempt = _read_object(control / "attempts" / f"{current_id}.json")
        validate_attempt(attempt, expected_id=current_id)
        if attempt["status"] != "completed":
            raise ValueError(f"Attempt target 必须为 completed：{current_id}")
        chain.append(attempt)
        current_id = attempt["target"]["id"]
    else:
        resolved = _resolve_terminal_target(control, current_id)

    for attempt in reversed(chain):
        _assert_attempt_target_matches(attempt, resolved)
        resolved = _attempt_target_resolution(attempt)
        if cache is not None:
            cache[attempt["id"]] = resolved
    return resolved


def _resolve_terminal_target(control: Path, target_id: str) -> dict[str, Any]:
    if TRIAL_ID.fullmatch(target_id):
        trial_directory = control / "trials" / target_id
        _require_real_directory(trial_directory, "Trial 对象目录")
        trial_path = trial_directory / "trial.json"
        trial = _read_object(trial_path)
        if trial.get("status") != "active":
            raise ValueError(f"Trial target 必须为 active：{target_id}")
        _validate_trial_shape(trial, target_id)
        asset_id = trial["code_asset_id"]
        asset = _load_verified_asset(control, asset_id)
        if asset["trial_id"] != target_id:
            raise ValueError(f"Trial 与 CodeAsset 引用不一致：{target_id}")
        shared_fields = (
            ("status", "template_id", "attachment_point", "transaction_id", "created_at")
            if trial["schema"] == FORMAL_TRIAL_SCHEMA
            else (
                "status", "template_family", "attachment_point",
                "template_selection_reason", "template_sha256", "transaction_id",
                "created_at",
            )
        )
        for field in shared_fields:
            if trial.get(field) != asset.get(field):
                raise ValueError(f"Trial 与 CodeAsset 共享事实漂移：{target_id}/{field}")
        base_version_id = trial["base_version_id"]
        base_version = _read_object(
            control / "versions" / f"{base_version_id}.json"
        )
        validate_version(base_version, expected_id=base_version_id)
        if base_version["status"] != "active":
            raise ValueError(f"Trial target 的基础 Version 必须为 active：{base_version_id}")
        ordered_assets = [asset_id]
        composition = None
        inherited_assets: list[dict[str, Any]] = []
        if trial["schema"] == FORMAL_TRIAL_SCHEMA:
            if asset.get("schema") != FORMAL_ASSET_SCHEMA:
                raise ValueError(f"正式 Trial 必须引用 v2 CodeAsset：{target_id}")
            composition = _load_verified_composition(control, asset_id)
            if (
                composition["template_id"] != trial["template_id"]
                or composition["base_version_id"] != base_version_id
                or composition["attachment_point"] != trial["attachment_point"]
                or composition["new_code_asset_id"] != asset_id
            ):
                raise ValueError(f"正式 Trial composition 事实漂移：{target_id}")
            ordered_assets = [*composition["inherited_code_asset_ids"], asset_id]
            for inherited_id in composition["inherited_code_asset_ids"]:
                inherited = _load_verified_asset(control, inherited_id)
                if (
                    inherited.get("schema") != FORMAL_ASSET_SCHEMA
                    or inherited.get("template_id") != trial["template_id"]
                ):
                    raise ValueError(f"继承 CodeAsset lineage 不一致：{inherited_id}")
                inherited_assets.append(inherited)
            for candidate_id, candidate in [
                *zip(composition["inherited_code_asset_ids"], inherited_assets),
                (asset_id, asset),
            ]:
                if candidate.get("external_code_ref") is None:
                    raise ValueError(f"正式 CodeAsset 尚未绑定 external_code_ref：{candidate_id}")
        facts = {
            "trial": _without_volatile(trial, {"id", "created_at", "transaction_id"}),
            "asset": _without_volatile(
                asset, {"id", "trial_id", "created_at", "transaction_id"},
            ),
            "base_version": _without_volatile(
                base_version, {"id", "created_at", "updated_at"},
            ),
        }
        if composition is not None:
            facts["composition"] = composition
            facts["inherited_assets"] = [
                _without_volatile(item, {"id", "trial_id", "created_at", "transaction_id"})
                for item in inherited_assets
            ]
        return {
            "kind": "trial",
            "base_version_id": base_version_id,
            "ordered_code_asset_ids": ordered_assets,
            "facts_digest": _canonical_digest(facts),
        }
    if VERSION_ID.fullmatch(target_id):
        version = _read_object(control / "versions" / f"{target_id}.json")
        validate_version(version, expected_id=target_id)
        if version["status"] != "active":
            raise ValueError(f"Version target 必须为 active：{target_id}")
        facts = _without_volatile(version, {"id", "created_at", "updated_at"})
        return {
            "kind": "version",
            "base_version_id": target_id,
            "ordered_code_asset_ids": list(
                version["accepted_code_asset_ids"]
                if version["schema"] == VERSION_SCHEMA_V2 else []
            ),
            "facts_digest": _canonical_digest(facts),
        }
    raise ValueError(f"target ID 格式错误：{target_id}")


def _attempt_target_resolution(attempt: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "attempt",
        "base_version_id": attempt["base_version_id"],
        "ordered_code_asset_ids": list(attempt["ordered_code_asset_ids"]),
        "facts_digest": _canonical_digest(_attempt_facts(attempt)),
    }


def _load_verified_asset(control: Path, asset_id: str) -> dict[str, Any]:
    directory = control / "code-assets" / asset_id
    _require_real_directory(directory, "CodeAsset 对象目录")
    asset = _read_object(directory / "asset.json")
    _validate_asset_shape(asset, asset_id)
    module, contract, provenance_bytes = validated_render_inputs(directory)
    provenance = parse_json_object_bytes(provenance_bytes, "provenance.json")
    _validate_provenance(provenance)
    contents = {
        "module.py": module,
        "test_contract.py": contract,
        "provenance.json": provenance_bytes,
    }
    if asset["schema"] == FORMAL_ASSET_SCHEMA:
        composition_bytes = read_bounded_regular_file(
            control / "code-assets" / asset_id / "composition.json",
            STRUCTURED_FILE_LIMIT,
            "composition.json",
        )
        composition = parse_json_object_bytes(composition_bytes, "composition.json")
        _validate_composition(composition, asset_id=asset_id)
        contents["composition.json"] = composition_bytes
    for filename, content in contents.items():
        if asset["files"][filename] != _sha256(content):
            raise ValueError(f"CodeAsset 文件 digest 不匹配：{asset_id}/{filename}")
    return asset


def _load_verified_composition(control: Path, asset_id: str) -> dict[str, Any]:
    directory = control / "code-assets" / asset_id
    asset = _load_verified_asset(control, asset_id)
    if asset["schema"] != FORMAL_ASSET_SCHEMA:
        raise ValueError(f"CodeAsset 不是正式 v2：{asset_id}")
    composition_bytes = read_bounded_regular_file(
        directory / "composition.json", STRUCTURED_FILE_LIMIT, "composition.json",
    )
    composition = parse_json_object_bytes(composition_bytes, "composition.json")
    _validate_composition(composition, asset_id=asset_id)
    return composition


def _validate_promotion_version(
    control: Path, version_id: str, version: dict[str, Any],
) -> None:
    base_id = version["base_version_id"]
    base = _read_object(control / "versions" / f"{base_id}.json")
    validate_version(base, expected_id=base_id)
    if base["status"] != "active":
        raise ValueError(f"draft Version 的 base 必须为 active：{version_id}")
    attempts = [
        _promotion_check_locked(control, attempt_id)
        for attempt_id in version["accepted_attempt_ids"]
    ]
    if any(attempt["base_version_id"] != base_id for attempt in attempts):
        raise ValueError(f"draft Version promotion base 漂移：{version_id}")
    expected_assets = _stable_unique(
        [asset_id for attempt in attempts for asset_id in attempt["ordered_code_asset_ids"]]
    )
    if version["ordered_code_asset_ids"] != expected_assets:
        raise ValueError(f"draft Version ordered_code_asset_ids 漂移：{version_id}")
    if version["code_sources"] != base["code_sources"]:
        raise ValueError(f"draft Version code_sources 未继承基础 Version：{version_id}")


def _validate_active_promotion_version(
    control: Path, version_id: str, version: dict[str, Any],
) -> None:
    base_id = version["base_version_id"]
    base = _read_object(control / "versions" / f"{base_id}.json")
    validate_version(base, expected_id=base_id)
    if base["status"] != "active":
        raise ValueError(f"active Version v2 的 base 必须为 active：{version_id}")
    attempts = [
        _promotion_check_locked(control, attempt_id)
        for attempt_id in version["accepted_attempt_ids"]
    ]
    if any(attempt["base_version_id"] != base_id for attempt in attempts):
        raise ValueError(f"active Version v2 promotion base 漂移：{version_id}")
    expected_assets = _stable_unique(
        [asset_id for attempt in attempts for asset_id in attempt["ordered_code_asset_ids"]]
    )
    if version["accepted_code_asset_ids"] != expected_assets:
        raise ValueError(f"active Version v2 accepted_code_asset_ids 漂移：{version_id}")
    template_id, refs = _validated_promotion_assets(control, expected_assets)
    if version["template_id"] != template_id:
        raise ValueError(f"active Version v2 template lineage 漂移：{version_id}")
    if version["accepted_code_refs"] != refs:
        raise ValueError(f"active Version v2 external refs 漂移：{version_id}")
    if base["schema"] == VERSION_SCHEMA_V2 and base["template_id"] != template_id:
        raise ValueError(f"active Version v2 与 base template lineage 冲突：{version_id}")


def _validated_promotion_assets(
    control: Path, asset_ids: list[str],
) -> tuple[str, list[dict[str, Any]]]:
    if not asset_ids:
        raise ValueError("promotion draft 必须包含非空 ordered_code_asset_ids")
    template_id: str | None = None
    target_paths: set[str] = set()
    refs: list[dict[str, Any]] = []
    for asset_id in asset_ids:
        asset = _load_verified_asset(control, asset_id)
        composition = _load_verified_composition(control, asset_id)
        external_ref = asset.get("external_code_ref")
        current_template = asset.get("template_id")
        if (
            asset.get("schema") != FORMAL_ASSET_SCHEMA
            or external_ref is None
            or composition["template_id"] != current_template
            or external_ref["relative_path"] != composition["target_path"]
        ):
            raise ValueError(f"promotion CodeAsset composition/binding 无效：{asset_id}")
        if template_id is None:
            template_id = current_template
        elif current_template != template_id:
            raise ValueError("promotion CodeAsset template lineage 冲突")
        target_path = composition["target_path"]
        if target_path in target_paths:
            raise ValueError(f"promotion composition target_path 重复冲突：{target_path}")
        target_paths.add(target_path)
        refs.append({
            "code_asset_id": asset_id,
            "external_code_ref": json.loads(json.dumps(external_ref)),
        })
    if template_id is None:
        raise ValueError("promotion draft 缺少有效 Template lineage")
    return template_id, refs


def _matching_promotion_version(
    control: Path,
    *,
    base_id: str,
    attempt_ids: list[str],
    asset_ids: list[str],
) -> dict[str, Any] | None:
    exact_matches: list[dict[str, Any]] = []
    historical_matches: list[dict[str, Any]] = []
    for path in _json_records(control / "versions", VERSION_ID):
        version = _read_object(path)
        validate_version(version, expected_id=path.stem)
        if version["status"] == "draft":
            candidate_assets = version["ordered_code_asset_ids"]
            validator = _validate_promotion_version
        elif version["schema"] == VERSION_SCHEMA_V2:
            candidate_assets = version["accepted_code_asset_ids"]
            validator = _validate_active_promotion_version
        else:
            continue
        if (
            version["base_version_id"] != base_id
            or set(version["accepted_attempt_ids"]) != set(attempt_ids)
        ):
            continue
        validator(control, path.stem, version)
        if (
            version["accepted_attempt_ids"] == attempt_ids
            and candidate_assets == asset_ids
        ):
            exact_matches.append(version)
        else:
            historical_matches.append(version)
    if len(exact_matches) > 1:
        raise ValueError("同一 promotion signature 对应多个 Version")
    if exact_matches:
        return exact_matches[0]
    if len(historical_matches) > 1:
        raise ValueError("同一 promotion evidence set 对应多个历史 Version")
    return historical_matches[0] if historical_matches else None


def _validate_result(result: object, attempt_id: str) -> None:
    legacy_fields = {
        "metrics", "artifacts", "decision", "conclusion", "limitations", "recorded_at",
    }
    v2_fields = legacy_fields | {
        "schema", "implementation_status", "hypothesis_status",
    }
    if not isinstance(result, dict) or set(result) not in {frozenset(legacy_fields), frozenset(v2_fields)}:
        raise ValueError(f"Attempt Result 结构无效：{attempt_id}")
    is_v2 = "schema" in result
    if is_v2 and (
        result.get("schema") != RESULT_SCHEMA_V2
        or result.get("implementation_status") not in IMPLEMENTATION_STATUSES
        or result.get("hypothesis_status") not in HYPOTHESIS_STATUSES
        or result.get("decision") != _derived_decision(
            result.get("implementation_status"), result.get("hypothesis_status"),
        )
    ):
        raise ValueError(f"Attempt Result 双轴或派生 decision 无效：{attempt_id}")
    _validate_json_object(result["metrics"], "metrics")
    if result["decision"] not in DECISIONS:
        raise ValueError(f"Attempt Result decision 无效：{attempt_id}")
    if not isinstance(result["conclusion"], str) or not result["conclusion"].strip():
        raise ValueError(f"Attempt Result conclusion 为空：{attempt_id}")
    normalized_artifacts = _normalize_artifacts(result["artifacts"])
    normalized_limitations = _normalize_limitations(result["limitations"])
    if result["artifacts"] != normalized_artifacts or result["limitations"] != normalized_limitations:
        raise ValueError(f"Attempt Result 引用未规范化：{attempt_id}")
    if not _is_utc_iso(result["recorded_at"]):
        raise ValueError(f"Attempt Result recorded_at 无效：{attempt_id}")


def _derived_decision(implementation: str, hypothesis: str) -> str:
    if implementation == "valid" and hypothesis == "supported":
        return "accept"
    if implementation == "valid" and hypothesis == "not_supported":
        return "reject"
    return "inconclusive"


def _normalize_artifacts(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("artifacts 必须是 JSON array")
    normalized: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) not in ({"path"}, {"path", "digest"}):
            raise ValueError("artifact 必须仅包含 path 与可选 digest")
        path = normalize_safe_relative_path(item.get("path"), "artifact path")
        result = {"path": path}
        if "digest" in item:
            digest = item["digest"]
            if not isinstance(digest, str) or ARTIFACT_DIGEST.fullmatch(digest) is None:
                raise ValueError("artifact digest 必须为 64hex 或 sha256:64hex")
            result["digest"] = digest
        normalized.append(result)
    return normalized


def _normalize_limitations(value: object) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and bool(item.strip()) and item == item.strip() for item in value
    ):
        raise ValueError("limitations 必须是非空文本组成的 JSON array")
    return list(value)


def _validate_json_object(value: object, label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是 JSON object")
    _validate_json_value(value, label)


def _validate_json_value(value: object, label: str) -> None:
    if value is None or type(value) in {bool, str, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} 中的数字必须有限")
        return
    if type(value) is dict:
        if not all(type(key) is str for key in value):
            raise ValueError(f"{label} 的 key 必须是字符串")
        for nested in value.values():
            _validate_json_value(nested, label)
        return
    if type(value) is list:
        for nested in value:
            _validate_json_value(nested, label)
        return
    raise ValueError(f"{label} 包含非 JSON 值：{type(value).__name__}")


def _identity_digest(payload: dict[str, Any]) -> str:
    identity = {
        "type": payload.get("type"),
        "seed": payload.get("seed"),
        "command": payload.get("command"),
        "config": payload.get("config"),
        "base_version_id": payload.get("base_version_id"),
        "target": payload.get("target"),
        "ordered_code_asset_ids": payload.get("ordered_code_asset_ids"),
    }
    return _canonical_digest(identity)


def _attempt_facts(attempt: dict[str, Any]) -> dict[str, Any]:
    facts = _without_volatile(attempt, {"id", "created_at"})
    result = facts.get("result")
    if isinstance(result, dict):
        facts["result"] = _without_volatile(result, {"recorded_at"})
    return facts


def _canonical_digest(value: object) -> str:
    content = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _without_volatile(payload: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in fields}


def _stable_unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _target_id_matches(kind: object, target_id: object) -> bool:
    if not isinstance(target_id, str):
        return False
    patterns = {"trial": TRIAL_ID, "version": VERSION_ID, "attempt": ATTEMPT_ID}
    pattern = patterns.get(str(kind))
    return pattern is not None and pattern.fullmatch(target_id) is not None


def _require_attempt_id(attempt_id: str) -> None:
    if ATTEMPT_ID.fullmatch(attempt_id) is None:
        raise ValueError(f"Attempt ID 格式错误：{attempt_id}")


def _json_records(directory: Path, pattern: re.Pattern[str]) -> list[Path]:
    _require_real_directory(directory, "JSON 记录父目录")
    result: list[Path] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.suffix != ".json" or pattern.fullmatch(path.stem) is None:
            continue
        _require_regular_file(path, "JSON 对象")
        result.append(path)
    return result


def _validate_attempt_directories(control: Path) -> None:
    _require_real_directory(control, "项目控制目录")
    for name in ("attempts", "versions", "trials", "code-assets"):
        _require_real_directory(control / name, f"{name} 父目录")


def _require_real_directory(path: Path, label: str) -> None:
    if is_link_or_reparse(path) or not path.is_dir():
        raise ValueError(f"{label}不是普通目录，拒绝访问：{path}")


def _require_regular_file(path: Path, label: str) -> None:
    if is_link_or_reparse(path) or not path.is_file():
        raise ValueError(f"{label}不是普通文件，拒绝访问：{path}")
