from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import unicodedata
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io import (
    atomic_create_bytes_clean,
    atomic_create_json,
    parse_json_object_bytes,
    read_bounded_regular_file,
    read_bounded_json_object,
)
from .locking import project_snapshot_lock, project_write_lock
from .project import PROJECT_SCHEMA_V2, is_link_or_reparse
from .records import (
    _initialized_project,
    _next_id,
    _preflight_workflow_command_locked,
    _read_object,
    _validate_project,
    normalize_safe_relative_path,
)
from .releases import WORKFLOW_RELEASE_IDENTITY
from .v2_catalog import (
    IDEA_ID,
    MODULE_ID,
    SOURCE_ID,
    validate_sources_locked,
)


RESEARCH_BRIEF_SCHEMA = "cv-experiment-workflow.research-brief.v1"
PAPER_PACKAGE_SELECTION_SCHEMA = (
    "cv-experiment-workflow.paper-package-selection.v1"
)
PAPER_PACKAGE_SCHEMA = "cv-experiment-workflow.paper-package.v1"
PAPER_PACKAGE_SEAL_RESULT_SCHEMA = (
    "cv-experiment-workflow.paper-package-seal-result.v1"
)
PAPER_PACKAGE_HANDOFF_SCHEMA = "cv-research-handoff/v2"
PAPER_PACKAGE_EXPORT_RESULT_SCHEMA = (
    "cv-experiment-workflow.paper-package-export-result.v1"
)
PAPER_PACKAGE_VERIFY_RESULT_SCHEMA = (
    "cv-experiment-workflow.paper-package-verification.v1"
)
BOUND_EVIDENCE_PRODUCER = {
    "skill_id": "cv-experiment-workflow",
    "release_version": "1.5.0",
    "system_version": "SYS-V2.13.0",
}
LEGACY_UNBOUND_PRODUCER = {
    "skill_id": "cv-experiment-workflow",
    "release_version": "1.4.0",
    "system_version": "SYS-V2.12.0",
}
LEGACY_UNBOUND_PRODUCER_TUPLE = (
    LEGACY_UNBOUND_PRODUCER["skill_id"],
    LEGACY_UNBOUND_PRODUCER["release_version"],
    LEGACY_UNBOUND_PRODUCER["system_version"],
)
BOUND_EVIDENCE_PRODUCER_TUPLE = (
    BOUND_EVIDENCE_PRODUCER["skill_id"],
    BOUND_EVIDENCE_PRODUCER["release_version"],
    BOUND_EVIDENCE_PRODUCER["system_version"],
)
BOUND_EVIDENCE_REVERIFICATION_SCHEMA = (
    "cv-experiment-workflow.paper-package-reverification.v1"
)
BOUND_EVIDENCE_BINDING_SCHEMA = (
    "cv-experiment-workflow.paper-package-evidence-binding.v1"
)
BOUND_OUTPUT_SEAL_SCHEMA = (
    "cv-experiment-workflow.paper-package-output-seal.v1"
)
BRIEF_ID = re.compile(r"BRIEF-[0-9]{4}")
BRIEF_NAME = re.compile(r"BRIEF-[0-9]{4}\.json")
PACKAGE_ID = re.compile(r"PKG-[0-9]{4}")
CLAIM_ID = re.compile(r"CLM-[0-9]{4}")
EXPERIMENT_ID = re.compile(r"EXP-[0-9]{4}")
ASSET_ID = re.compile(r"AST-[0-9]{4}")
VISUAL_ID = re.compile(r"VIS-[0-9]{4}")
RUN_ID = re.compile(r"RUN-[0-9]{4}")
TASK_ID = re.compile(r"TASK-[0-9]{4}")
RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
INITIAL_STAGING_PREFIX = ".experiment-workflow.paper-packages-"
INITIAL_STAGING_SUFFIX = ".staging"
MAX_MANIFEST_SIZE = 256 * 1024
MAX_RECORD_SIZE = 1024 * 1024
MAX_TEXT = 4096
MAX_ITEMS = 64
MAX_PACKAGE_FILE_SIZE = 8 * 1024 * 1024
MAX_JSONL_LINE_SIZE = 256 * 1024
MAX_JSONL_ROWS = 2048
MAX_PACKAGE_TOTAL_SIZE = 32 * 1024 * 1024
MAX_LIVE_ASSET_SIZE = 512 * 1024 * 1024
MAX_LIVE_ASSET_TOTAL_SIZE = 512 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
MAX_HANDOFF_CONTROL_FILE_SIZE = 8 * 1024 * 1024
MAX_HANDOFF_CONTROL_TOTAL_SIZE = 32 * 1024 * 1024
MAX_HANDOFF_SINGLE_ASSET_SIZE = 512 * 1024 * 1024
MAX_HANDOFF_ASSET_TOTAL_SIZE = 512 * 1024 * 1024
MAX_HANDOFF_PACKAGE_TOTAL_SIZE = 544 * 1024 * 1024
MAX_HANDOFF_FILE_COUNT = 2056
MAX_HANDOFF_ENTRY_COUNT = 4112
MAX_HANDOFF_ASSET_COUNT = 2048
HANDOFF_STAGING_SUFFIX = ".paper-package.staging"
PACKAGE_FILES = (
    "package.json",
    "study.json",
    "claims.jsonl",
    "experiments.json",
    "sources.jsonl",
    "assets.jsonl",
    "visuals.json",
    "checksums.sha256",
)
CHECKSUM_FILES = tuple(sorted(PACKAGE_FILES[:-1]))
SCIENTIFIC_FILES = (
    "study.json",
    "claims.jsonl",
    "experiments.json",
    "sources.jsonl",
    "assets.jsonl",
    "visuals.json",
)
HANDOFF_ROOT_FILES = (
    "manifest.json",
    *SCIENTIFIC_FILES,
    "checksums.sha256",
)
SECTION_IDS = {
    "abstract",
    "introduction",
    "related_work",
    "method",
    "experiments",
    "conclusion",
}
CLAIM_KINDS = {
    "background",
    "research_gap",
    "objective",
    "method",
    "innovation",
    "protocol",
    "result",
    "limitation",
}
SOURCE_USE_ROLES = {
    "background",
    "prior_art",
    "terminology",
    "method_context",
    "comparison_context",
}
# 这里只冻结当前解析器明确理解的历史正文结构。producer 没有签名，
# 不能单独证明包由哪个程序生成，最终仍要通过全部现场事实核验。
PAPER_PACKAGE_PARSE_COMPATIBLE_PRODUCERS = (
    (
        "cv-experiment-workflow",
        "1.2.3",
        "SYS-V2.10.3",
    ),
    (
        "cv-experiment-workflow",
        "1.3.0",
        "SYS-V2.11.0",
    ),
    LEGACY_UNBOUND_PRODUCER_TUPLE,
    (
        WORKFLOW_RELEASE_IDENTITY["skill_id"],
        WORKFLOW_RELEASE_IDENTITY["release_version"],
        WORKFLOW_RELEASE_IDENTITY["system_version"],
    ),
    BOUND_EVIDENCE_PRODUCER_TUPLE,
)
INPUT_FIELDS = {
    "title",
    "research_area",
    "background",
    "problem",
    "objective",
    "research_questions",
    "hypotheses",
    "scope",
    "terminology",
    "planned_contributions",
    "user_confirmation",
}
OUTPUT_FIELDS = INPUT_FIELDS | {
    "schema",
    "project_id",
    "brief_id",
    "revision",
    "created_at",
}


def save_research_brief(project: Path, manifest: Path) -> dict[str, Any]:
    """保存一份不可覆盖的 v2 Research Brief。"""
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _preflight_workflow_command_locked(control)
        sources = validate_sources_locked(control)
        facts = _normalize_manifest(
            read_bounded_json_object(
                Path(manifest),
                MAX_MANIFEST_SIZE,
                "Research Brief manifest",
            )
        )
        records = validate_research_briefs_locked(control, sources=sources)
        package_directory = control / "paper-packages"
        if os.path.lexists(package_directory):
            directory = package_directory / "briefs"
            brief_id = _next_id(directory, "BRIEF", BRIEF_ID)
        else:
            directory = None
            brief_id = "BRIEF-0001"
        revision = len(records) + 1
        project_payload = _read_object(control / "project.json")
        payload = {
            "schema": RESEARCH_BRIEF_SCHEMA,
            "project_id": project_payload["project_id"],
            "brief_id": brief_id,
            "revision": revision,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **facts,
        }
        validate_research_brief(
            payload,
            expected_id=brief_id,
            expected_project_id=project_payload["project_id"],
            sources=sources,
            expected_revision=revision,
        )
        if directory is None:
            _publish_initial_brief_directory(control, payload, sources)
        else:
            path = directory / f"{brief_id}.json"
            if not atomic_create_json(
                path,
                payload,
                transaction_id=uuid.uuid4().hex,
            ):
                raise FileExistsError(f"Research Brief 已存在，拒绝覆盖：{path}")
        return payload


def validate_research_briefs_locked(
    control: Path,
    *,
    sources: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """在调用者持有项目锁时，严格读取可选的 Research Brief 账本。"""
    control = Path(control)
    package_directory = control / "paper-packages"
    if not os.path.lexists(package_directory):
        return {}
    _require_regular_directory(package_directory, "paper-packages 目录")
    package_entries = {
        entry.name: entry
        for entry in package_directory.iterdir()
    }
    allowed_entries = {"briefs", "packages"}
    if "briefs" not in package_entries or not set(package_entries) <= allowed_entries:
        unexpected = sorted(set(package_entries) - allowed_entries)
        if unexpected:
            raise ValueError(
                "paper-packages 目录存在未知条目："
                f"{package_entries[unexpected[0]]}"
            )
        raise ValueError(
            f"paper-packages 目录缺少 briefs：{package_directory / 'briefs'}"
        )
    directory = package_entries["briefs"]
    _require_regular_directory(directory, "Research Brief 目录")
    if not any(directory.iterdir()):
        raise ValueError(f"Research Brief 目录不得为空：{directory}")

    source_records = (
        validate_sources_locked(control) if sources is None else sources
    )
    project_id = _read_object(control / "project.json").get("project_id")
    records: dict[str, dict[str, Any]] = {}
    for revision, entry in enumerate(
        sorted(directory.iterdir(), key=lambda item: item.name),
        start=1,
    ):
        if BRIEF_NAME.fullmatch(entry.name) is None:
            raise ValueError(f"Research Brief 目录存在未知条目：{entry}")
        if is_link_or_reparse(entry) or not entry.is_file():
            raise ValueError(f"Research Brief 不是普通 JSON 文件：{entry}")
        expected_id = f"BRIEF-{revision:04d}"
        if entry.stem != expected_id:
            raise ValueError(
                f"Research Brief ID 必须连续，期望 {expected_id}：{entry}"
            )
        payload = read_bounded_json_object(
            entry,
            MAX_RECORD_SIZE,
            f"Research Brief {expected_id}",
        )
        validate_research_brief(
            payload,
            expected_id=expected_id,
            expected_project_id=project_id,
            sources=source_records,
            expected_revision=revision,
        )
        records[expected_id] = payload
    return records


def validate_research_brief(
    payload: dict[str, Any],
    *,
    expected_id: str,
    expected_project_id: object,
    sources: dict[str, dict[str, Any]],
    expected_revision: int,
) -> None:
    if set(payload) != OUTPUT_FIELDS:
        raise ValueError(f"Research Brief 字段不完整或含未知字段：{expected_id}")
    if (
        payload.get("schema") != RESEARCH_BRIEF_SCHEMA
        or payload.get("brief_id") != expected_id
        or BRIEF_ID.fullmatch(expected_id) is None
        or payload.get("project_id") != expected_project_id
        or type(payload.get("revision")) is not int
        or payload.get("revision") != expected_revision
        or not _is_rfc3339(payload.get("created_at"))
    ):
        raise ValueError(f"Research Brief 身份字段无效：{expected_id}")
    facts = _normalize_manifest(
        {field: payload[field] for field in INPUT_FIELDS}
    )
    if any(payload[field] != facts[field] for field in INPUT_FIELDS):
        raise ValueError(f"Research Brief 正文字段未规范化：{expected_id}")
    _validate_contribution_sources(
        facts["planned_contributions"],
        sources,
    )


def seal_paper_package(
    project: Path,
    brief_id: str,
    selection: Path,
) -> dict[str, Any]:
    """把已确认的 v2 账本事实封成一个不可覆盖的内部论文包。"""
    if not isinstance(brief_id, str) or BRIEF_ID.fullmatch(brief_id) is None:
        raise ValueError("Research Brief ID 无效")
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        from .validation import validate_v2_full_snapshot_locked

        hash_session = _LiveFileHashSession()
        (
            tasks,
            runs,
            events,
            _evidence_content,
            catalog,
            briefs,
            existing,
        ) = validate_v2_full_snapshot_locked(
            control,
            hash_session=hash_session,
        )
        brief = briefs.get(brief_id)
        if brief is None:
            raise ValueError(f"Research Brief 不存在：{brief_id}")
        facts = _normalize_package_selection(
            read_bounded_json_object(
                Path(selection),
                MAX_MANIFEST_SIZE,
                "Paper Package selection",
            )
        )
        package_id = _next_package_id(existing)
        supersedes = facts["supersedes_package_id"]
        _validate_supersedes(existing, supersedes)
        project_payload = _read_object(control / "project.json")
        source_run_refs = sorted(
            {
                run_id
                for claim in facts["claims"]
                for run_id in claim["run_refs"]
            }
        )
        producer = _package_producer_identity(
            runs,
            source_run_refs,
        )
        if _producer_tuple(producer) == BOUND_EVIDENCE_PRODUCER_TUPLE:
            _reverify_bound_runs_locked(
                control,
                source_run_refs=source_run_refs,
                tasks=tasks,
                runs=runs,
                events=events,
            )
        science, missing_requirements = _build_scientific_files(
            root=root,
            brief=brief,
            facts=facts,
            tasks=tasks,
            runs=runs,
            events=events,
            catalog=catalog,
            hash_session=hash_session,
        )
        content_sha256 = _content_sha256(science)
        readiness = (
            "paper_ready" if not missing_requirements else "incomplete"
        )
        package_payload = {
            "schema": PAPER_PACKAGE_SCHEMA,
            "package_id": package_id,
            "package_revision": 1,
            "project_id": project_payload["project_id"],
            "project_name": project_payload["name"],
            "brief_id": brief_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "producer": producer,
            "asset_mode": facts["asset_mode"],
            "readiness": readiness,
            "missing_requirements": missing_requirements,
            "paper_scope": facts["paper_scope"],
            "content_sha256": content_sha256,
            "supersedes_package_id": supersedes,
            "source_run_refs": source_run_refs,
        }
        if (
            _producer_tuple(producer) == BOUND_EVIDENCE_PRODUCER_TUPLE
            and (readiness != "paper_ready" or missing_requirements)
        ):
            raise ValueError(
                "1.5 正式证据包必须包含全部论文必需资产并达到 paper_ready"
            )
        files = {
            "package.json": _canonical_json_line(package_payload),
            **science,
        }
        files["checksums.sha256"] = _checksum_file(files)
        _validate_package_file_limits(files)
        _publish_package(
            control,
            package_id,
            files,
            first=not existing,
            project=project_payload,
            briefs=briefs,
            tasks=tasks,
            runs=runs,
            events=events,
            catalog=catalog,
            hash_session=hash_session,
        )
        persisted = validate_research_packages_locked(
            control,
            tasks=tasks,
            runs=runs,
            events=events,
            catalog=catalog,
            # 发布后的复读必须重新读取现场文件。复用封存阶段的缓存会让
            # 同 inode、同 size、同 mtime 的原地改写逃过最终校验。
            hash_session=_LiveFileHashSession(),
            briefs=briefs,
        )
        if package_id not in persisted:
            raise RuntimeError(f"论文包发布后无法重新读取：{package_id}")
        return {
            "schema": PAPER_PACKAGE_SEAL_RESULT_SCHEMA,
            "status": "sealed",
            "package_id": package_id,
            "readiness": readiness,
            "missing_requirements": missing_requirements,
            "asset_mode": facts["asset_mode"],
            "content_sha256": content_sha256,
            "supersedes_package_id": supersedes,
        }


def export_paper_package(
    project: Path,
    package_id: str,
    mode: str,
    out: Path,
) -> dict[str, Any]:
    from .paper_package_delivery import export_paper_package as export

    return export(project, package_id, mode, out)


def verify_paper_package(package_dir: Path) -> dict[str, Any]:
    from .paper_package_delivery import verify_paper_package as verify

    return verify(package_dir)


def _producer_tuple(value: dict[str, Any]) -> tuple[object, object, object]:
    return (
        value.get("skill_id"),
        value.get("release_version"),
        value.get("system_version"),
    )


def _package_producer_identity(
    runs: dict[str, dict[str, Any]],
    source_run_refs: list[str],
) -> dict[str, str]:
    """新绑定证据使用 1.5；旧未绑定账本只保留历史包维护能力。"""
    from .run_identity import is_bound_frozen

    selected = [runs.get(run_id) for run_id in source_run_refs]
    if selected and all(
        isinstance(run, dict) and is_bound_frozen(run.get("frozen"))
        for run in selected
    ):
        return deepcopy(BOUND_EVIDENCE_PRODUCER)
    if any(
        isinstance(run, dict) and is_bound_frozen(run.get("frozen"))
        for run in selected
    ):
        raise ValueError("同一 Paper Package 不能混合绑定与历史未绑定 Run")
    return deepcopy(LEGACY_UNBOUND_PRODUCER)


def _reverify_bound_runs_locked(
    control: Path,
    *,
    source_run_refs: list[str],
    tasks: dict[str, dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """在调用方已持有的项目锁中重验论文级正式 Run。"""
    from .execution_snapshot import verify_run_execution_snapshot
    from .output_seal import _verify_output_tree
    from .run_identity import is_bound_frozen

    if not source_run_refs:
        raise ValueError("1.5 正式证据包至少需要一条 source Run")
    bindings: dict[str, dict[str, Any]] = {}
    for run_id in source_run_refs:
        run = runs.get(run_id)
        if run is None or not is_bound_frozen(run.get("frozen")):
            raise ValueError(f"1.5 包只接受绑定 Codebase 的 Run：{run_id}")
        task = tasks.get(run["task_id"])
        frozen = run["frozen"]
        code = frozen["code"]
        data = frozen["data"]
        execution = run["execution"]
        quality = run["quality"]
        seal = run.get("output_seal")
        if (
            task is None
            or task.get("stage") != "done"
            or run.get("purpose") != "evidence"
            or data.get("run_kind") != "real_experiment"
            or data.get("paper_eligible") is not True
            or code.get("clean_required") is not True
            or code.get("worktree_clean") is not True
            or execution.get("stage") != "closed"
            or execution.get("outcome") != "succeeded"
            or execution.get("exit_code") != 0
            or execution.get("issue_kind") is not None
            or set(quality.values()) != {"valid"}
            or not isinstance(seal, dict)
        ):
            raise ValueError(
                f"1.5 包只接受 clean、成功关闭且质量有效的正式 Run：{run_id}"
            )
        verify_run_execution_snapshot(control, run)
        _verify_output_tree(control.parent, run, seal)
        run_events = [
            deepcopy(event)
            for event in events
            if event.get("subject_id") == run_id
        ]
        if (
            len(run_events) != 2
            or [
                (event.get("from"), event.get("to"))
                for event in run_events
            ]
            != [("none", "single_run"), ("single_run", "confirmed")]
            or any(
                event.get("subject_type") != "Run"
                or event.get("evidence_refs") != [run_id]
                or not isinstance(event.get("checked_by"), str)
                for event in run_events
            )
            or not run_events[1]["checked_by"].strip()
        ):
            raise ValueError(
                f"1.5 包要求精确的 single_run→confirmed 两事件证据链：{run_id}"
            )
        if not run_events[0]["checked_by"].strip():
            run_events[0]["checked_by"] = (
                "not-applicable:single-run-automatic-quality-gate"
            )
        evidence_binding = {
            "schema": BOUND_EVIDENCE_BINDING_SCHEMA,
            "level": "confirmed",
            "events": run_events,
        }
        evidence_binding["evidence_sha256"] = _canonical_object_sha256(
            evidence_binding
        )
        bindings[run_id] = evidence_binding
    return bindings


def _canonical_object_sha256(value: object) -> str:
    content = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _next_package_id(existing: dict[str, dict[str, Any]]) -> str:
    number = len(existing) + 1
    if number > 9999:
        raise ValueError("Paper Package PKG ID 已耗尽")
    return f"PKG-{number:04d}"


def _normalize_package_selection(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "schema",
        "asset_mode",
        "paper_scope",
        "claims",
        "experiments",
        "source_uses",
        "assets",
        "visuals",
        "supersedes_package_id",
    }
    _exact_object(payload, fields, "Paper Package selection")
    if payload.get("schema") != PAPER_PACKAGE_SELECTION_SCHEMA:
        raise ValueError("Paper Package selection schema 无效")
    asset_mode = payload.get("asset_mode")
    if (
        not isinstance(asset_mode, str)
        or asset_mode not in {"hybrid", "full"}
    ):
        raise ValueError("asset_mode 必须是 hybrid 或 full")
    claims = _normalize_claims(payload.get("claims"))
    claim_ids = [item["claim_id"] for item in claims]
    paper_scope = _normalize_paper_scope(
        payload.get("paper_scope"),
        claim_ids=claim_ids,
    )
    experiments = _normalize_experiments(payload.get("experiments"))
    source_uses = _normalize_source_uses(payload.get("source_uses"))
    assets = _normalize_assets(payload.get("assets"))
    visuals = _normalize_visuals(payload.get("visuals"))
    supersedes = payload.get("supersedes_package_id")
    if supersedes is not None and (
        not isinstance(supersedes, str)
        or PACKAGE_ID.fullmatch(supersedes) is None
    ):
        raise ValueError("supersedes_package_id 必须是 PKG-xxxx 或 null")
    return {
        "asset_mode": asset_mode,
        "paper_scope": paper_scope,
        "claims": claims,
        "experiments": experiments,
        "source_uses": source_uses,
        "assets": assets,
        "visuals": visuals,
        "supersedes_package_id": supersedes,
    }


def _normalize_paper_scope(
    value: object,
    *,
    claim_ids: list[str],
) -> dict[str, Any]:
    row = _exact_object(
        value,
        {
            "title_hint",
            "research_area",
            "goal",
            "included_claim_ids",
            "excluded_topics",
        },
        "paper_scope",
    )
    included = _reference_list(
        row.get("included_claim_ids"),
        CLAIM_ID,
        "paper_scope.included_claim_ids",
        minimum=1,
    )
    if included != claim_ids:
        raise ValueError(
            "paper_scope.included_claim_ids 必须与 claims ID 顺序完全一致"
        )
    return {
        "title_hint": _text(row.get("title_hint"), "paper_scope.title_hint"),
        "research_area": _text(
            row.get("research_area"), "paper_scope.research_area"
        ),
        "goal": _text(row.get("goal"), "paper_scope.goal"),
        "included_claim_ids": included,
        "excluded_topics": _selection_text_list(
            row.get("excluded_topics"),
            "paper_scope.excluded_topics",
            minimum=0,
        ),
    }


def _normalize_claims(value: object) -> list[dict[str, Any]]:
    items = _selection_list(value, "claims", minimum=1)
    normalized: list[dict[str, Any]] = []
    for number, item in enumerate(items, start=1):
        row = _exact_object(
            item,
            {
                "claim_id",
                "kind",
                "origin",
                "statement_zh",
                "statement_en",
                "maturity",
                "run_refs",
                "metric_refs",
                "source_refs",
                "idea_refs",
                "module_refs",
                "innovation_boundary",
                "allowed_sections",
            },
            "claim",
        )
        claim_id = _sequential_id(
            row.get("claim_id"), CLAIM_ID, "CLM", number, "claim_id"
        )
        kind = row.get("kind")
        origin = row.get("origin")
        maturity = row.get("maturity")
        if not isinstance(kind, str) or kind not in CLAIM_KINDS:
            raise ValueError(f"Claim kind 无效：{claim_id}")
        if not isinstance(origin, str) or origin not in {"project", "external"}:
            raise ValueError(f"Claim origin 无效：{claim_id}")
        if (
            not isinstance(maturity, str)
            or maturity not in {"supported", "confirmed"}
        ):
            raise ValueError(f"Claim maturity 无效：{claim_id}")
        statement_en = row.get("statement_en")
        if statement_en is not None:
            statement_en = _text(statement_en, f"{claim_id}.statement_en")
        allowed_sections = _selection_text_list(
            row.get("allowed_sections"),
            f"{claim_id}.allowed_sections",
            minimum=1,
        )
        if not set(allowed_sections) <= SECTION_IDS:
            raise ValueError(f"Claim allowed_sections 含未知章节：{claim_id}")
        metric_refs = _normalize_metric_refs(
            row.get("metric_refs"),
            claim_id,
        )
        boundary = _normalize_innovation_boundary(
            row.get("innovation_boundary"),
            claim_id,
        )
        normalized.append(
            {
                "claim_id": claim_id,
                "kind": kind,
                "origin": origin,
                "statement_zh": _text(
                    row.get("statement_zh"), f"{claim_id}.statement_zh"
                ),
                "statement_en": statement_en,
                "maturity": maturity,
                "run_refs": _reference_list(
                    row.get("run_refs"),
                    RUN_ID,
                    f"{claim_id}.run_refs",
                    minimum=0,
                ),
                "metric_refs": metric_refs,
                "source_refs": _reference_list(
                    row.get("source_refs"),
                    SOURCE_ID,
                    f"{claim_id}.source_refs",
                    minimum=0,
                ),
                "idea_refs": _reference_list(
                    row.get("idea_refs"),
                    IDEA_ID,
                    f"{claim_id}.idea_refs",
                    minimum=0,
                ),
                "module_refs": _reference_list(
                    row.get("module_refs"),
                    MODULE_ID,
                    f"{claim_id}.module_refs",
                    minimum=0,
                ),
                "innovation_boundary": boundary,
                "allowed_sections": allowed_sections,
            }
        )
    return normalized


def _normalize_metric_refs(
    value: object,
    claim_id: str,
) -> list[dict[str, str]]:
    items = _selection_list(
        value, f"{claim_id}.metric_refs", minimum=0
    )
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        row = _exact_object(
            item,
            {"run_id", "metric_name"},
            f"{claim_id}.metric_ref",
        )
        run_id = row.get("run_id")
        if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
            raise ValueError(f"Claim metric_ref.run_id 无效：{claim_id}")
        metric_name = _text(
            row.get("metric_name"), f"{claim_id}.metric_name"
        )
        key = (run_id, metric_name)
        if key in seen:
            raise ValueError(f"Claim metric_refs 重复：{claim_id}")
        seen.add(key)
        normalized.append({"run_id": run_id, "metric_name": metric_name})
    return normalized


def _normalize_innovation_boundary(
    value: object,
    claim_id: str,
) -> dict[str, str] | None:
    if value is None:
        return None
    row = _exact_object(
        value,
        {"existing_or_prior", "new_contribution", "not_claimed"},
        f"{claim_id}.innovation_boundary",
    )
    return {
        key: _text(row.get(key), f"{claim_id}.innovation_boundary.{key}")
        for key in (
            "existing_or_prior",
            "new_contribution",
            "not_claimed",
        )
    }


def _normalize_experiments(value: object) -> list[dict[str, Any]]:
    items = _selection_list(value, "experiments", minimum=1)
    normalized: list[dict[str, Any]] = []
    for number, item in enumerate(items, start=1):
        row = _exact_object(
            item,
            {
                "experiment_id",
                "title",
                "objective",
                "task_id",
                "run_refs",
                "claim_refs",
            },
            "experiment",
        )
        experiment_id = _sequential_id(
            row.get("experiment_id"),
            EXPERIMENT_ID,
            "EXP",
            number,
            "experiment_id",
        )
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or TASK_ID.fullmatch(task_id) is None:
            raise ValueError(f"Experiment task_id 无效：{experiment_id}")
        normalized.append(
            {
                "experiment_id": experiment_id,
                "title": _text(
                    row.get("title"), f"{experiment_id}.title"
                ),
                "objective": _text(
                    row.get("objective"), f"{experiment_id}.objective"
                ),
                "task_id": task_id,
                "run_refs": _reference_list(
                    row.get("run_refs"),
                    RUN_ID,
                    f"{experiment_id}.run_refs",
                    minimum=1,
                ),
                "claim_refs": _reference_list(
                    row.get("claim_refs"),
                    CLAIM_ID,
                    f"{experiment_id}.claim_refs",
                    minimum=1,
                ),
            }
        )
    return normalized


def _normalize_source_uses(value: object) -> list[dict[str, Any]]:
    items = _selection_list(value, "source_uses", minimum=0)
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        row = _exact_object(
            item,
            {"source_ref", "role", "excerpt", "supports_claim_ids"},
            "source_use",
        )
        source_ref = row.get("source_ref")
        if (
            not isinstance(source_ref, str)
            or SOURCE_ID.fullmatch(source_ref) is None
        ):
            raise ValueError("source_use.source_ref 无效")
        role = row.get("role")
        if not isinstance(role, str) or role not in SOURCE_USE_ROLES:
            raise ValueError(f"source_use.role 无效：{source_ref}")
        excerpt = _text(row.get("excerpt"), f"{source_ref}.excerpt")
        claim_ids = _reference_list(
            row.get("supports_claim_ids"),
            CLAIM_ID,
            f"{source_ref}.supports_claim_ids",
            minimum=0 if role == "terminology" else 1,
        )
        identity = (source_ref, role, excerpt)
        if identity in seen:
            raise ValueError(f"source_use 重复：{source_ref}")
        seen.add(identity)
        normalized.append(
            {
                "source_ref": source_ref,
                "role": role,
                "excerpt": excerpt,
                "supports_claim_ids": claim_ids,
            }
        )
    return sorted(
        normalized,
        key=lambda item: (
            item["source_ref"],
            item["role"],
            item["excerpt"],
        ),
    )


def _normalize_assets(value: object) -> list[dict[str, Any]]:
    items = _selection_list(value, "assets", minimum=0)
    normalized: list[dict[str, Any]] = []
    for number, item in enumerate(items, start=1):
        row = _exact_object(
            item,
            {
                "asset_id",
                "source_object_ref",
                "path",
                "role",
                "required_for_writing",
                "copy_allowed",
                "license",
                "privacy_classification",
                "availability",
                "omission_reason",
            },
            "asset",
        )
        asset_id = _sequential_id(
            row.get("asset_id"),
            ASSET_ID,
            "AST",
            number,
            "asset_id",
        )
        source_object_ref = row.get("source_object_ref")
        if not isinstance(source_object_ref, str) or not any(
            pattern.fullmatch(source_object_ref)
            for pattern in (RUN_ID, SOURCE_ID, MODULE_ID)
        ):
            raise ValueError(
                f"Asset source_object_ref 无效：{asset_id}"
            )
        required = row.get("required_for_writing")
        copy_allowed = row.get("copy_allowed")
        if type(required) is not bool or type(copy_allowed) is not bool:
            raise ValueError(f"Asset bool 字段无效：{asset_id}")
        privacy = row.get("privacy_classification")
        if (
            not isinstance(privacy, str)
            or privacy not in {"public", "internal", "sensitive", "restricted"}
        ):
            raise ValueError(f"Asset privacy_classification 无效：{asset_id}")
        availability = row.get("availability")
        if (
            not isinstance(availability, str)
            or availability
            not in {"available", "missing", "restricted", "external"}
        ):
            raise ValueError(f"Asset availability 无效：{asset_id}")
        license_value = _text(row.get("license"), f"{asset_id}.license")
        if copy_allowed and license_value.casefold() == "unknown":
            raise ValueError(f"unknown license 不允许复制：{asset_id}")
        if copy_allowed and privacy in {"sensitive", "restricted"}:
            raise ValueError(f"敏感或受限 Asset 不允许复制：{asset_id}")
        omission_reason = row.get("omission_reason")
        if not isinstance(omission_reason, str):
            raise ValueError(f"Asset omission_reason 必须是字符串：{asset_id}")
        omission_reason = omission_reason.strip()
        if availability != "available" and not omission_reason:
            raise ValueError(
                f"不可用 Asset 必须说明 omission_reason：{asset_id}"
            )
        normalized.append(
            {
                "asset_id": asset_id,
                "source_object_ref": source_object_ref,
                "path": _text(row.get("path"), f"{asset_id}.path"),
                "role": _text(row.get("role"), f"{asset_id}.role"),
                "required_for_writing": required,
                "copy_allowed": copy_allowed,
                "license": license_value,
                "privacy_classification": privacy,
                "availability": availability,
                "omission_reason": omission_reason,
            }
        )
    return normalized


def _normalize_visuals(value: object) -> list[dict[str, Any]]:
    items = _selection_list(value, "visuals", minimum=0)
    normalized: list[dict[str, Any]] = []
    for number, item in enumerate(items, start=1):
        row = _exact_object(
            item,
            {
                "visual_id",
                "kind",
                "title",
                "purpose",
                "allowed_sections",
                "claim_refs",
                "experiment_refs",
                "asset_refs",
            },
            "visual",
        )
        visual_id = _sequential_id(
            row.get("visual_id"),
            VISUAL_ID,
            "VIS",
            number,
            "visual_id",
        )
        kind = row.get("kind")
        if not isinstance(kind, str) or kind not in {"figure", "table"}:
            raise ValueError(f"Visual kind 无效：{visual_id}")
        sections = _selection_text_list(
            row.get("allowed_sections"),
            f"{visual_id}.allowed_sections",
            minimum=1,
        )
        if not set(sections) <= SECTION_IDS:
            raise ValueError(f"Visual allowed_sections 含未知章节：{visual_id}")
        normalized.append(
            {
                "visual_id": visual_id,
                "kind": kind,
                "title": _text(row.get("title"), f"{visual_id}.title"),
                "purpose": _text(
                    row.get("purpose"), f"{visual_id}.purpose"
                ),
                "allowed_sections": sections,
                "claim_refs": _reference_list(
                    row.get("claim_refs"),
                    CLAIM_ID,
                    f"{visual_id}.claim_refs",
                    minimum=1,
                ),
                "experiment_refs": _reference_list(
                    row.get("experiment_refs"),
                    EXPERIMENT_ID,
                    f"{visual_id}.experiment_refs",
                    minimum=0,
                ),
                "asset_refs": _reference_list(
                    row.get("asset_refs"),
                    ASSET_ID,
                    f"{visual_id}.asset_refs",
                    minimum=0,
                ),
            }
        )
    return normalized


def _build_scientific_files(
    *,
    root: Path,
    brief: dict[str, Any],
    facts: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    catalog: dict[str, dict[str, dict[str, Any]]],
    hash_session: _LiveFileHashSession | None = None,
) -> tuple[dict[str, bytes], list[str]]:
    hash_session = hash_session or _LiveFileHashSession()
    scope = facts["paper_scope"]
    expected_scope = {
        "research_area": brief["research_area"],
        "goal": brief["objective"],
        "excluded_topics": brief["scope"]["excluded"],
    }
    for field, expected in expected_scope.items():
        if scope[field] != expected:
            raise ValueError(
                f"paper_scope.{field} 与 Research Brief 不一致"
            )

    claim_ids = {item["claim_id"] for item in facts["claims"]}
    source_rows = _resolve_source_uses(
        facts["source_uses"],
        sources=catalog["sources"],
        claim_ids=claim_ids,
        hash_session=hash_session,
    )
    asset_rows = _resolve_assets(
        root,
        facts["assets"],
        runs=runs,
        sources=catalog["sources"],
        modules=catalog["modules"],
        hash_session=hash_session,
    )
    claim_rows = _resolve_claims(
        facts["claims"],
        tasks=tasks,
        runs=runs,
        events=events,
        sources=catalog["sources"],
        ideas=catalog["ideas"],
        modules=catalog["modules"],
        source_rows=source_rows,
    )
    experiment_rows = _resolve_experiments(
        facts["experiments"],
        claims=claim_rows,
        tasks=tasks,
        runs=runs,
        assets=asset_rows,
    )
    visual_rows = _resolve_visuals(
        facts["visuals"],
        claims=claim_rows,
        experiments=experiment_rows,
        assets=asset_rows,
    )
    missing_requirements = _derive_missing_requirements(
        claims=claim_rows,
        assets=asset_rows,
        experiments=experiment_rows,
        runs=runs,
    )
    study = {
        "schema": "cv-experiment-workflow.paper-package-study.v1",
        "brief": deepcopy(brief),
        "paper_scope": deepcopy(scope),
    }
    experiments_payload = {
        "schema": "cv-experiment-workflow.paper-package-experiments.v1",
        "items": experiment_rows,
    }
    visuals_payload = {
        "schema": "cv-experiment-workflow.paper-package-visuals.v1",
        "items": visual_rows,
    }
    return {
        "study.json": _canonical_json_line(study),
        "claims.jsonl": _canonical_jsonl(claim_rows, "claim_id"),
        "experiments.json": _canonical_json_line(experiments_payload),
        "sources.jsonl": _canonical_jsonl(source_rows, "source_ref"),
        "assets.jsonl": _canonical_jsonl(asset_rows, "asset_id"),
        "visuals.json": _canonical_json_line(visuals_payload),
    }, missing_requirements


def _resolve_source_uses(
    uses: list[dict[str, Any]],
    *,
    sources: dict[str, dict[str, Any]],
    claim_ids: set[str],
    hash_session: _LiveFileHashSession,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for use in uses:
        source_ref = use["source_ref"]
        source = sources.get(source_ref)
        if source is None:
            raise ValueError(f"source_use Source 不存在：{source_ref}")
        if source["kind"] != "paper":
            raise ValueError(
                f"论文 source_use 目前只接受 paper Source：{source_ref}"
            )
        unknown = set(use["supports_claim_ids"]) - claim_ids
        if unknown:
            raise ValueError(
                f"source_use 引用了不存在的 Claim：{sorted(unknown)[0]}"
            )
        file_facts = hash_session.hash(
            Path(source["locator"]),
            f"paper Source {source_ref}",
            MAX_LIVE_ASSET_SIZE,
        )
        if file_facts["sha256"] != source["digest"]:
            raise ValueError(f"paper Source 文件哈希已变化：{source_ref}")
        rows.append(
            {
                **deepcopy(use),
                "kind": source["kind"],
                "identity": source["identity"],
                "locator": source["locator"],
                "revision": source["revision"],
                "license": source["license"],
                "source_sha256": file_facts["sha256"],
                "excerpt_sha256": _sha256_text(use["excerpt"]),
            }
        )
    return rows


def _resolve_assets(
    root: Path,
    assets: list[dict[str, Any]],
    *,
    runs: dict[str, dict[str, Any]],
    sources: dict[str, dict[str, Any]],
    modules: dict[str, dict[str, Any]],
    hash_session: _LiveFileHashSession,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_object_paths: set[tuple[str, str]] = set()
    for asset in assets:
        asset_id = asset["asset_id"]
        ref = asset["source_object_ref"]
        if SOURCE_ID.fullmatch(ref):
            source = sources.get(ref)
            if source is None:
                raise ValueError(f"Asset Source 不存在：{ref}")
            if asset["license"] != source["license"]:
                raise ValueError(
                    f"Asset license 必须与 Source 账本一致：{asset_id}/{ref}"
                )
        key = (ref, asset["path"])
        if key in seen_object_paths:
            raise ValueError(f"Asset 重复绑定同一对象路径：{asset_id}")
        seen_object_paths.add(key)
        target, expected_digest, reference_uri = _resolve_asset_target(
            root,
            asset,
            runs=runs,
            sources=sources,
            modules=modules,
        )
        availability = asset["availability"]
        if availability in {"external", "restricted"}:
            rows.append(
                {
                    **deepcopy(asset),
                    "size_bytes": None,
                    "sha256": None,
                    "reference_uri": reference_uri,
                }
            )
            continue
        exists = os.path.lexists(target)
        derived_availability = (
            "available"
            if exists
            else "missing"
        )
        if availability != derived_availability:
            raise ValueError(
                f"Asset availability 与现场文件不一致：{asset_id}"
            )
        if exists:
            file_facts = hash_session.hash(
                target,
                f"Asset {asset_id}",
                MAX_LIVE_ASSET_SIZE,
                root=(
                    root.resolve(strict=True)
                    if RUN_ID.fullmatch(ref) or MODULE_ID.fullmatch(ref)
                    else None
                ),
            )
            if (
                expected_digest is not None
                and file_facts["sha256"] != expected_digest
            ):
                raise ValueError(f"Asset 哈希与来源对象不一致：{asset_id}")
            size_bytes: int | None = file_facts["size_bytes"]
            sha256: str | None = file_facts["sha256"]
        else:
            size_bytes = None
            sha256 = None
        rows.append(
            {
                **deepcopy(asset),
                "availability": derived_availability,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "reference_uri": reference_uri,
            }
        )
    return rows


def _resolve_asset_target(
    root: Path,
    asset: dict[str, Any],
    *,
    runs: dict[str, dict[str, Any]],
    sources: dict[str, dict[str, Any]],
    modules: dict[str, dict[str, Any]],
) -> tuple[Path, str | None, str]:
    ref = asset["source_object_ref"]
    supplied_path = asset["path"]
    if RUN_ID.fullmatch(ref):
        run = runs.get(ref)
        if run is None:
            raise ValueError(f"Asset Run 不存在：{ref}")
        allowed = [
            run.get("result", {}).get("raw_log"),
            *run.get("artifacts", []),
        ]
        allowed = [item for item in allowed if isinstance(item, str)]
        relative = normalize_safe_relative_path(
            supplied_path, f"Asset {asset['asset_id']}.path"
        )
        if relative not in allowed:
            raise ValueError(
                f"Asset path 不属于 Run raw_log/artifacts：{asset['asset_id']}"
            )
        seal = run.get("output_seal")
        if isinstance(seal, dict):
            records = [
                seal.get("raw_log"),
                *seal.get("artifacts", []),
            ]
            record = next(
                (
                    item
                    for item in records
                    if isinstance(item, dict)
                    and item.get("path") == relative
                ),
                None,
            )
            sealed_root = seal.get("root")
            if (
                record is None
                or not isinstance(sealed_root, dict)
                or sealed_root.get("kind")
                != "project_sealed_output_copy"
                or sealed_root.get("relative_path")
                != f".cv-workflow-seals/{ref}"
            ):
                raise ValueError(
                    f"Asset 与 Run 权威输出封存不一致：{asset['asset_id']}"
                )
            sealed_relative = normalize_safe_relative_path(
                f"{sealed_root['relative_path']}/{relative}",
                f"Asset {asset['asset_id']} sealed path",
            )
            return (
                root / sealed_relative,
                record["sha256"],
                f"run-output-seal://{ref}/{relative}",
            )
        return root / relative, None, f"project://{relative}"
    if MODULE_ID.fullmatch(ref):
        module = modules.get(ref)
        if module is None:
            raise ValueError(f"Asset Module 不存在：{ref}")
        if module["status"] != "ready":
            raise ValueError(f"Asset 只能绑定 ready Module：{ref}")
        file_map = {
            item["path"]: item["digest"]
            for item in module["validation"]["files"]
        }
        relative = normalize_safe_relative_path(
            supplied_path, f"Asset {asset['asset_id']}.path"
        )
        expected = file_map.get(relative)
        if expected is None:
            raise ValueError(
                f"Asset path 不属于 Module validation.files：{asset['asset_id']}"
            )
        return root / relative, expected, f"module://{ref}/{relative}"
    source = sources.get(ref)
    if source is None:
        raise ValueError(f"Asset Source 不存在：{ref}")
    if source["kind"] == "paper":
        if supplied_path != source["locator"]:
            raise ValueError(
                f"paper Asset path 必须精确等于 Source locator：{asset['asset_id']}"
            )
        return Path(source["locator"]), source["digest"], f"source://{ref}"
    if source["kind"] == "code_snapshot":
        file_map = {
            item["path"]: item["digest"] for item in source["files"]
        }
        relative = normalize_safe_relative_path(
            supplied_path, f"Asset {asset['asset_id']}.path"
        )
        expected = file_map.get(relative)
        if expected is None:
            raise ValueError(
                "code_snapshot Asset path 不属于 Source files："
                f"{asset['asset_id']}"
            )
        return (
            Path(source["locator"]) / relative,
            expected,
            f"source://{ref}/{relative}",
        )
    raise ValueError(
        f"code Source 是提交身份，不是可直接复制的单文件 Asset：{ref}"
    )


def _resolve_claims(
    claims: list[dict[str, Any]],
    *,
    tasks: dict[str, dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]],
    ideas: dict[str, dict[str, Any]],
    modules: dict[str, dict[str, Any]],
    source_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    from .evidence import levels_from_events

    levels = levels_from_events(events)
    source_support: dict[tuple[str, str], set[str]] = {}
    for row in source_rows:
        for claim_id in row["supports_claim_ids"]:
            source_support.setdefault(
                (row["source_ref"], claim_id),
                set(),
            ).add(row["role"])
    rows: list[dict[str, Any]] = []
    for claim in claims:
        claim_id = claim["claim_id"]
        for source_ref in claim["source_refs"]:
            if source_ref not in sources:
                raise ValueError(
                    f"Claim 引用了不存在的 Source：{claim_id}/{source_ref}"
                )
            if (source_ref, claim_id) not in source_support:
                raise ValueError(
                    f"Claim Source 缺少对应 source_use：{claim_id}/{source_ref}"
                )
        for idea_ref in claim["idea_refs"]:
            if idea_ref not in ideas:
                raise ValueError(
                    f"Claim 引用了不存在的 Idea：{claim_id}/{idea_ref}"
                )
        for module_ref in claim["module_refs"]:
            if module_ref not in modules:
                raise ValueError(
                    f"Claim 引用了不存在的 Module：{claim_id}/{module_ref}"
                )

        if claim["origin"] == "external":
            if claim["kind"] not in {"background", "research_gap", "method"}:
                raise ValueError(
                    f"external Claim 只能说明背景、研究空白或已有方法：{claim_id}"
                )
            if (
                not claim["source_refs"]
                or claim["run_refs"]
                or claim["metric_refs"]
                or claim["idea_refs"]
                or claim["module_refs"]
                or claim["innovation_boundary"] is not None
            ):
                raise ValueError(
                    f"external Claim 不得借用项目 Run/Idea/Module：{claim_id}"
                )
            _require_expected_maturity(claim, "supported")
            rows.append({**deepcopy(claim), "metric_refs": [], "comparisons": []})
            continue

        if claim["kind"] == "result":
            rows.append(
                _resolve_result_claim(
                    claim,
                    tasks=tasks,
                    runs=runs,
                    levels=levels,
                )
            )
            continue
        if claim["kind"] == "innovation":
            _validate_innovation_claim(
                claim,
                sources=sources,
                ideas=ideas,
                modules=modules,
                source_support=source_support,
            )
            _require_expected_maturity(claim, "supported")
            rows.append({**deepcopy(claim), "metric_refs": [], "comparisons": []})
            continue
        if claim["kind"] not in {
            "objective",
            "method",
            "protocol",
            "limitation",
        }:
            raise ValueError(
                f"project Claim kind 缺少可核验的项目来源：{claim_id}"
            )
        if (
            claim["run_refs"]
            or claim["metric_refs"]
            or claim["idea_refs"]
            or claim["module_refs"]
            or claim["innovation_boundary"] is not None
        ):
            raise ValueError(
                f"非结果/创新 Claim 不得伪装 Run 或创新证据：{claim_id}"
            )
        _require_expected_maturity(claim, "supported")
        rows.append({**deepcopy(claim), "metric_refs": [], "comparisons": []})
    if not any(
        row["kind"] == "result" and row["maturity"] == "confirmed"
        for row in rows
    ):
        raise ValueError("论文包至少需要一个 confirmed result Claim")
    return rows


def _resolve_result_claim(
    claim: dict[str, Any],
    *,
    tasks: dict[str, dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    levels: dict[str, str],
) -> dict[str, Any]:
    claim_id = claim["claim_id"]
    if (
        claim["origin"] != "project"
        or claim["maturity"] != "confirmed"
        or not claim["run_refs"]
        or not claim["metric_refs"]
        or claim["source_refs"]
        or claim["idea_refs"]
        or claim["module_refs"]
        or claim["innovation_boundary"] is not None
    ):
        raise ValueError(
            f"result Claim 必须是项目 confirmed Run 事实：{claim_id}"
        )
    resolved_metrics: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    allowed_numbers: list[float] = []
    selected_metric_values: list[float] = []
    for run_id in claim["run_refs"]:
        run = runs.get(run_id)
        if run is None:
            raise ValueError(f"result Claim Run 不存在：{claim_id}/{run_id}")
        if levels.get(run_id, "none") != "confirmed":
            raise ValueError(
                f"result Claim 只接受当前 Evidence=confirmed：{claim_id}/{run_id}"
            )
        execution = run["execution"]
        if (
            run["purpose"] != "evidence"
            or execution["stage"] not in {"finished", "closed"}
            or execution["outcome"] != "succeeded"
            or run["quality"]["implementation"] != "valid"
            or any(
                value in {"invalid", "uncertain"}
                for value in run["quality"].values()
            )
        ):
            raise ValueError(
                f"result Claim Run 未通过成功与质量门禁：{claim_id}/{run_id}"
            )
        task = tasks.get(run["task_id"])
        if task is None:
            raise ValueError(f"result Claim Task 不存在：{claim_id}/{run_id}")
        conclusion = task.get("conclusion")
        if task["stage"] == "done":
            from . import routes
            from .run_identity import comparison_policy_for_frozen

            comparison_kwargs: dict[str, Any] = {}
            policy = comparison_policy_for_frozen(run.get("frozen"))
            if policy is None:
                from .runs import _legacy_route_comparison

                expected_comparison = _legacy_route_comparison(task, run)
            else:
                inputs = task.get("route_inputs")
                source_ref: object = None
                if isinstance(inputs, dict):
                    source_ref = inputs.get(
                        (
                            "source_run_ref"
                            if task["route"]
                            in {"reproduction", "innovation"}
                            else "baseline_run_ref"
                        )
                    )
                source_run = (
                    runs.get(source_ref)
                    if isinstance(source_ref, str)
                    else None
                )
                comparison_run = run
                if (
                    not isinstance(run.get("id"), str)
                    and isinstance(run.get("run_id"), str)
                ):
                    comparison_run = {**run, "id": run["run_id"]}
                if (
                    isinstance(source_run, dict)
                    and not isinstance(source_run.get("id"), str)
                    and isinstance(source_run.get("run_id"), str)
                ):
                    source_run = {
                        **source_run,
                        "id": source_run["run_id"],
                    }

                def stored_seal(run_ref: str) -> object:
                    sealed_run = runs.get(run_ref)
                    if not isinstance(sealed_run, dict):
                        raise ValueError(
                            "比较引用的 Run 不存在"
                        )
                    seal = sealed_run.get("output_seal")
                    if not isinstance(seal, dict):
                        raise ValueError(
                            "比较引用的 Run 没有封存记录"
                        )
                    return seal

                comparison_kwargs = {
                    "source_run": source_run,
                    "seal_verifier": stored_seal,
                }
                expected_comparison = routes.compare(
                    task["route"],
                    task,
                    comparison_run,
                    **comparison_kwargs,
                )
            if (
                not isinstance(conclusion, dict)
                or conclusion.get("run_id") != run_id
                or conclusion.get("comparison") != expected_comparison
            ):
                raise ValueError(
                    f"done Task 的永久 comparison 与 result Claim 不一致：{task['id']}"
                )
            comparison = {
                "task_id": task["id"],
                "run_id": run_id,
                **deepcopy(expected_comparison),
            }
            comparisons.append(comparison)
            allowed_numbers.extend(_finite_numbers(expected_comparison))
    metric_runs = {item["run_id"] for item in claim["metric_refs"]}
    if metric_runs != set(claim["run_refs"]):
        raise ValueError(
            f"result Claim 每个 Run 都必须有选中指标且不得跨 Run：{claim_id}"
        )
    for metric_ref in claim["metric_refs"]:
        run = runs[metric_ref["run_id"]]
        metric_name = metric_ref["metric_name"]
        metrics = run.get("result", {}).get("metrics")
        definitions = run.get("frozen", {}).get("config", {}).get(
            "metric_definition"
        )
        if (
            not isinstance(metrics, dict)
            or metric_name not in metrics
            or not isinstance(definitions, dict)
            or metric_name not in definitions
        ):
            raise ValueError(
                f"result Claim 指标值或定义不存在：{claim_id}/{metric_name}"
            )
        value = metrics[metric_name]
        definition = definitions[metric_name]
        if (
            type(value) not in {int, float}
            or not math.isfinite(value)
            or not isinstance(definition, str)
            or not definition.strip()
        ):
            raise ValueError(
                f"result Claim 指标值或定义无效：{claim_id}/{metric_name}"
            )
        numeric_value = float(value)
        selected_metric_values.append(numeric_value)
        allowed_numbers.append(numeric_value)
        resolved_metrics.append(
            {
                "run_id": metric_ref["run_id"],
                "metric_name": metric_name,
                "value": value,
                "definition": definition,
            }
        )
    _validate_result_statement_numbers(
        claim["statement_zh"],
        selected_metric_values=selected_metric_values,
        allowed_numbers=allowed_numbers,
        claim_id=claim_id,
    )
    if claim["statement_en"] is not None:
        _validate_result_statement_numbers(
            claim["statement_en"],
            selected_metric_values=selected_metric_values,
            allowed_numbers=allowed_numbers,
            claim_id=claim_id,
        )
    return {
        **deepcopy(claim),
        "metric_refs": resolved_metrics,
        "comparisons": comparisons,
    }


def _validate_innovation_claim(
    claim: dict[str, Any],
    *,
    sources: dict[str, dict[str, Any]],
    ideas: dict[str, dict[str, Any]],
    modules: dict[str, dict[str, Any]],
    source_support: dict[tuple[str, str], set[str]],
) -> None:
    claim_id = claim["claim_id"]
    if any(
        statement is not None
        and _statement_number_tokens(statement)
        for statement in (claim["statement_zh"], claim["statement_en"])
    ):
        raise ValueError(
            f"innovation Claim 不得夹带效果数字，效果必须单列 result：{claim_id}"
        )
    if (
        claim["origin"] != "project"
        or claim["innovation_boundary"] is None
        or claim["run_refs"]
        or claim["metric_refs"]
        or not claim["source_refs"]
        or not claim["idea_refs"]
        or not claim["module_refs"]
    ):
        raise ValueError(
            f"innovation Claim 缺少边界、Idea、Module 或 prior art：{claim_id}"
        )
    ready_ideas = {
        idea_ref
        for idea_ref in claim["idea_refs"]
        if ideas[idea_ref]["status"] == "ready"
    }
    if ready_ideas != set(claim["idea_refs"]):
        raise ValueError(f"innovation Claim 只能绑定 ready Idea：{claim_id}")
    for module_ref in claim["module_refs"]:
        module = modules[module_ref]
        if (
            module["status"] != "ready"
            or module["kind"] != "research"
            or not ready_ideas.intersection(module["idea_refs"])
        ):
            raise ValueError(
                f"innovation Claim Module 未链接 ready Idea：{claim_id}/{module_ref}"
            )
    prior_art = [
        source_ref
        for source_ref in claim["source_refs"]
        if sources[source_ref]["kind"] == "paper"
        and "prior_art" in source_support.get((source_ref, claim_id), set())
        and all(
            source_ref in ideas[idea_ref]["source_refs"]
            for idea_ref in claim["idea_refs"]
        )
    ]
    if not prior_art:
        raise ValueError(
            f"innovation Claim 缺少与 Idea 相连的 prior_art paper Source：{claim_id}"
        )


def _resolve_experiments(
    experiments: list[dict[str, Any]],
    *,
    claims: list[dict[str, Any]],
    tasks: dict[str, dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    claim_map = {item["claim_id"]: item for item in claims}
    asset_map = {
        (item["source_object_ref"], item["path"]): item
        for item in assets
    }
    rows: list[dict[str, Any]] = []
    covered_result_claims: set[str] = set()
    for experiment in experiments:
        experiment_id = experiment["experiment_id"]
        task = tasks.get(experiment["task_id"])
        if task is None:
            raise ValueError(
                f"Experiment Task 不存在：{experiment_id}/{experiment['task_id']}"
            )
        unknown_claims = set(experiment["claim_refs"]) - set(claim_map)
        if unknown_claims:
            raise ValueError(
                f"Experiment Claim 不存在：{experiment_id}/{sorted(unknown_claims)[0]}"
            )
        run_rows: list[dict[str, Any]] = []
        for run_id in experiment["run_refs"]:
            run = runs.get(run_id)
            if run is None or run["task_id"] != task["id"]:
                raise ValueError(
                    f"Experiment Run 不属于所选 Task：{experiment_id}/{run_id}"
                )
            result = run.get("result")
            if not isinstance(result, dict):
                raise ValueError(
                    f"Experiment Run 缺少结果：{experiment_id}/{run_id}"
                )
            expected_paths = [
                result.get("raw_log"),
                *run.get("artifacts", []),
            ]
            if (
                not expected_paths
                or any(not isinstance(path, str) for path in expected_paths)
                or len(expected_paths) != len(set(expected_paths))
            ):
                raise ValueError(
                    f"Experiment Run 文件清单缺失或重复：{experiment_id}/{run_id}"
                )
            file_rows = []
            for path in expected_paths:
                asset = asset_map.get((run_id, path))
                if asset is None:
                    continue
                file_rows.append(
                    {
                        "path": path,
                        "asset_id": asset["asset_id"],
                        "size_bytes": asset["size_bytes"],
                        "sha256": asset["sha256"],
                    }
                )
            run_rows.append(
                {
                    "run_id": run_id,
                    "purpose": run["purpose"],
                    "frozen_digest": run["frozen_digest"],
                    "frozen": _portable_frozen_for_delivery(run["frozen"]),
                    "execution": deepcopy(run["execution"]),
                    "result": deepcopy(result),
                    "quality": deepcopy(run["quality"]),
                    "analysis": deepcopy(run["analysis"]),
                    "artifact_paths": deepcopy(run["artifacts"]),
                    "files": file_rows,
                }
            )
        for claim_id in experiment["claim_refs"]:
            claim = claim_map[claim_id]
            if claim["run_refs"] and not set(claim["run_refs"]) <= set(
                experiment["run_refs"]
            ):
                raise ValueError(
                    f"Experiment 未覆盖 Claim 的全部 Run：{experiment_id}/{claim_id}"
                )
            if claim["kind"] == "result":
                covered_result_claims.add(claim_id)
        rows.append(
            {
                **deepcopy(experiment),
                "task": {
                    "task_id": task["id"],
                    "route": task["route"],
                    "target_refs": deepcopy(task["target_refs"]),
                    "route_inputs": _portable_route_inputs_for_delivery(
                        task["route_inputs"]
                    ),
                    "stage": task["stage"],
                    "conclusion": deepcopy(task["conclusion"]),
                },
                "runs": run_rows,
            }
        )
    expected_results = {
        item["claim_id"] for item in claims if item["kind"] == "result"
    }
    if covered_result_claims != expected_results:
        missing = sorted(expected_results - covered_result_claims)
        raise ValueError(
            f"每个 result Claim 都必须由 Experiment 覆盖：{missing[0]}"
        )
    return rows


def _portable_frozen_for_delivery(value: dict[str, Any]) -> dict[str, Any]:
    """交付包保留数据身份与原 frozen_digest，但不泄露本机数据绝对路径。"""

    frozen = deepcopy(value)
    if isinstance(frozen.get("config"), dict):
        frozen["config"] = _portable_config_for_delivery(frozen["config"])
    return frozen


def _portable_route_inputs_for_delivery(
    value: dict[str, Any],
) -> dict[str, Any]:
    inputs = deepcopy(value)
    if isinstance(inputs.get("config"), dict):
        inputs["config"] = _portable_config_for_delivery(inputs["config"])
    return inputs


def _portable_config_for_delivery(value: dict[str, Any]) -> dict[str, Any]:
    config = deepcopy(value)
    data_path = config.get("data_path")
    manifest_sha256 = config.get("manifest_sha256")
    if (
        isinstance(data_path, str)
        and Path(data_path).is_absolute()
        and isinstance(manifest_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is not None
    ):
        config["data_path"] = f"external-dataset://sha256/{manifest_sha256}"
    return config


def _resolve_visuals(
    visuals: list[dict[str, Any]],
    *,
    claims: list[dict[str, Any]],
    experiments: list[dict[str, Any]],
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    claim_ids = {item["claim_id"] for item in claims}
    experiment_ids = {item["experiment_id"] for item in experiments}
    asset_ids = {item["asset_id"] for item in assets}
    for visual in visuals:
        checks = (
            ("Claim", visual["claim_refs"], claim_ids),
            ("Experiment", visual["experiment_refs"], experiment_ids),
            ("Asset", visual["asset_refs"], asset_ids),
        )
        for label, refs, known in checks:
            unknown = set(refs) - known
            if unknown:
                raise ValueError(
                    f"Visual 引用了不存在的 {label}：{visual['visual_id']}/"
                    f"{sorted(unknown)[0]}"
                )
    return deepcopy(visuals)


def _derive_missing_requirements(
    *,
    claims: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    experiments: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
) -> list[str]:
    missing: list[str] = []
    for asset in assets:
        if not asset["required_for_writing"]:
            continue
        if asset["availability"] != "available":
            missing.append(f"required_asset_unavailable:{asset['asset_id']}")
        if not asset["copy_allowed"]:
            missing.append(f"required_asset_not_copyable:{asset['asset_id']}")
        if asset["license"].casefold() == "unknown":
            missing.append(f"required_asset_license_unknown:{asset['asset_id']}")
        if asset["privacy_classification"] in {"sensitive", "restricted"}:
            missing.append(f"required_asset_privacy_blocked:{asset['asset_id']}")
    asset_map = {
        (item["source_object_ref"], item["path"]): item
        for item in assets
    }
    for experiment in experiments:
        for run_row in experiment["runs"]:
            run_id = run_row["run_id"]
            run = runs[run_id]
            expected_paths = [
                run["result"]["raw_log"],
                *run["artifacts"],
            ]
            for path in expected_paths:
                asset = asset_map.get((run_id, path))
                if asset is None:
                    missing.append(f"result_file_missing:{run_id}:{path}")
                elif asset["sha256"] is None:
                    missing.append(f"result_file_unavailable:{run_id}:{path}")
    return sorted(set(missing))


def _require_expected_maturity(
    claim: dict[str, Any],
    expected: str,
) -> None:
    if claim["maturity"] != expected:
        raise ValueError(
            f"Claim maturity 与账本可证明程度不一致：{claim['claim_id']}"
        )


def _validate_result_statement_numbers(
    statement: str,
    *,
    selected_metric_values: list[float],
    allowed_numbers: list[float],
    claim_id: str,
) -> None:
    tokens = _statement_number_tokens(statement)
    parsed: list[float] = []
    for token in tokens:
        normalized = token.strip().replace("−", "-")
        percent = normalized.endswith(("%", "％"))
        normalized = normalized[:-1] if percent else normalized
        number = float(re.sub(r"\s+", "", normalized))
        parsed.append(number / 100.0 if percent else number)
    if not parsed or not any(
        _numbers_equal(number, metric)
        for number in parsed
        for metric in selected_metric_values
    ):
        raise ValueError(
            f"result Claim 陈述必须包含至少一个所选指标值：{claim_id}"
        )
    for number in parsed:
        if not any(
            _numbers_equal(number, allowed)
            for allowed in allowed_numbers
        ):
            raise ValueError(
                f"result Claim 陈述含无法由指标或 comparison 证明的数字：{claim_id}"
            )


def _statement_number_tokens(statement: str) -> list[str]:
    for character in statement:
        if unicodedata.category(character) in {"Nl", "No"}:
            compatibility = unicodedata.normalize("NFKC", character)
            if not compatibility or not all(
                item.isdecimal() for item in compatibility
            ):
                raise ValueError("Claim 陈述含不支持的兼容数字写法")
    normalized = unicodedata.normalize("NFKC", statement).replace("−", "-")
    canonical = "".join(
        str(unicodedata.decimal(character))
        if character.isdecimal()
        else character
        for character in normalized
    )
    if re.search(r"(?<=\d)_+(?=\d)", canonical):
        raise ValueError("Claim 陈述含不支持的下划线数字写法")
    pattern = re.compile(
        r"(?<![A-Za-z0-9_.+\-])"
        r"[+\-−]?\s*"
        r"(?:\d+(?:\.\d*)?|\.\d+)"
        r"(?:[eE][+\-]?\s*\d+)?(?:\s*%)?"
        r"(?![A-Za-z0-9_]|(?:\.\d))"
    )
    matches = list(pattern.finditer(canonical))
    covered = [False] * len(canonical)
    for match in matches:
        for index in range(match.start(), match.end()):
            covered[index] = True
    if any(
        character.isdigit() and not covered[index]
        for index, character in enumerate(canonical)
    ):
        raise ValueError(
            "Claim 陈述含残缺科学计数或不支持的数字写法"
        )
    return [match.group(0) for match in matches]


def _finite_numbers(value: object) -> list[float]:
    numbers: list[float] = []
    if isinstance(value, dict):
        for item in value.values():
            numbers.extend(_finite_numbers(item))
    elif isinstance(value, list):
        for item in value:
            numbers.extend(_finite_numbers(item))
    elif type(value) in {int, float} and math.isfinite(value):
        numbers.append(float(value))
    return numbers


def _numbers_equal(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-12)


def _strict_json_equal(left: object, right: object) -> bool:
    """按 JSON 类型递归比较，避免 bool 与 0/1 被 Python 当成相等。"""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return (
            left.keys() == right.keys()
            and all(
                _strict_json_equal(left[key], right[key])
                for key in left
            )
        )
    if isinstance(left, list):
        return (
            len(left) == len(right)
            and all(
                _strict_json_equal(left_item, right_item)
                for left_item, right_item in zip(left, right, strict=True)
            )
        )
    return left == right


def validate_research_packages_locked(
    control: Path,
    *,
    tasks: dict[str, dict[str, Any]] | None = None,
    runs: dict[str, dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    catalog: dict[str, dict[str, dict[str, Any]]] | None = None,
    hash_session: _LiveFileHashSession | None = None,
    briefs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """严格验证已封存 PKG；调用者必须已经持有项目锁。"""
    control = Path(control)
    paper_packages = control / "paper-packages"
    if not os.path.lexists(paper_packages):
        return {}
    _require_regular_directory(paper_packages, "paper-packages 目录")
    packages = paper_packages / "packages"
    if not os.path.lexists(packages):
        return {}
    _require_regular_directory(packages, "packages 目录")
    entries = sorted(packages.iterdir(), key=lambda item: item.name)
    if not entries:
        raise ValueError(f"packages 目录不得为空：{packages}")
    if tasks is None or runs is None or events is None or catalog is None:
        raise ValueError("严格校验 Paper Package 需要当前项目账本快照")
    hash_session = hash_session or _LiveFileHashSession()
    project = _read_object(control / "project.json")
    if briefs is None:
        briefs = validate_research_briefs_locked(
            control,
            sources=catalog["sources"],
        )
    records: dict[str, dict[str, Any]] = {}
    direct_targets: set[str] = set()
    if len(entries) > 9999:
        raise ValueError("Paper Package PKG ID 已耗尽")
    for number, entry in enumerate(entries, start=1):
        expected_id = f"PKG-{number:04d}"
        if entry.name != expected_id:
            raise ValueError(
                f"Paper Package ID 必须连续，期望 {expected_id}：{entry}"
            )
        _require_regular_directory(entry, f"Paper Package {expected_id}")
        payload = _validate_package_directory(
            entry,
            root=control.parent,
            expected_id=expected_id,
            project=project,
            briefs=briefs,
            tasks=tasks,
            runs=runs,
            events=events,
            catalog=catalog,
            hash_session=hash_session,
        )
        supersedes = payload["supersedes_package_id"]
        if supersedes is not None:
            if supersedes not in records:
                raise ValueError(
                    f"Paper Package 只能 supersede 同项目更早的包：{expected_id}"
                )
            if supersedes in direct_targets:
                raise ValueError(
                    f"Paper Package 已被其他包直接 supersede：{supersedes}"
                )
            direct_targets.add(supersedes)
        records[expected_id] = payload
    return records


def _validate_package_directory(
    directory: Path,
    *,
    root: Path,
    expected_id: str,
    project: dict[str, Any],
    briefs: dict[str, dict[str, Any]],
    tasks: dict[str, dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    catalog: dict[str, dict[str, dict[str, Any]]],
    hash_session: _LiveFileHashSession,
) -> dict[str, Any]:
    entries = {entry.name: entry for entry in directory.iterdir()}
    if set(entries) != set(PACKAGE_FILES):
        raise ValueError(
            f"Paper Package 必须精确包含 8 个控制文件：{expected_id}"
        )
    contents: dict[str, bytes] = {}
    total = 0
    for name in PACKAGE_FILES:
        path = entries[name]
        if is_link_or_reparse(path) or not path.is_file():
            raise ValueError(
                f"Paper Package 控制文件必须是普通文件：{expected_id}/{name}"
            )
        content = read_bounded_regular_file(
            path,
            MAX_PACKAGE_FILE_SIZE,
            f"Paper Package {expected_id}/{name}",
        )
        size = len(content)
        total += size
        if total > MAX_PACKAGE_TOTAL_SIZE:
            raise ValueError(f"Paper Package 总大小超过 32 MiB：{expected_id}")
        contents[name] = content
    _validate_checksum_bytes(contents)
    package = parse_json_object_bytes(
        contents["package.json"], f"{expected_id}/package.json"
    )
    if contents["package.json"] != _canonical_json_line(package):
        raise ValueError(f"Paper Package package.json 不是规范 JSON：{expected_id}")
    package_fields = {
        "schema",
        "package_id",
        "package_revision",
        "project_id",
        "project_name",
        "brief_id",
        "created_at",
        "producer",
        "asset_mode",
        "readiness",
        "missing_requirements",
        "paper_scope",
        "content_sha256",
        "supersedes_package_id",
        "source_run_refs",
    }
    if set(package) != package_fields:
        raise ValueError(f"Paper Package package.json 字段无效：{expected_id}")
    if (
        package.get("schema") != PAPER_PACKAGE_SCHEMA
        or package.get("package_id") != expected_id
        or type(package.get("package_revision")) is not int
        or package.get("package_revision") != 1
        or package.get("project_id") != project["project_id"]
        or package.get("project_name") != project["name"]
        or not isinstance(package.get("brief_id"), str)
        or BRIEF_ID.fullmatch(package["brief_id"]) is None
        or not isinstance(package.get("asset_mode"), str)
        or package.get("asset_mode") not in {"hybrid", "full"}
        or not isinstance(package.get("readiness"), str)
        or package.get("readiness") not in {"paper_ready", "incomplete"}
        or not _is_rfc3339(package.get("created_at"))
    ):
        raise ValueError(f"Paper Package 身份字段无效：{expected_id}")
    producer = _exact_object(
        package.get("producer"),
        {"skill_id", "release_version", "system_version"},
        f"{expected_id}.producer",
    )
    producer_tuple = (
        _text(producer.get("skill_id"), f"{expected_id}.producer.skill_id"),
        _text(
            producer.get("release_version"),
            f"{expected_id}.producer.release_version",
        ),
        _text(
            producer.get("system_version"),
            f"{expected_id}.producer.system_version",
        ),
    )
    if producer_tuple not in PAPER_PACKAGE_PARSE_COMPATIBLE_PRODUCERS:
        raise ValueError(
            f"Paper Package producer 不在明确解析兼容白名单：{expected_id}"
        )
    if package["content_sha256"] != _content_sha256(
        {name: contents[name] for name in SCIENTIFIC_FILES}
    ):
        raise ValueError(f"Paper Package content_sha256 不一致：{expected_id}")
    missing = package.get("missing_requirements")
    if (
        not isinstance(missing, list)
        or not all(isinstance(item, str) and item for item in missing)
        or missing != sorted(set(missing))
        or (package["readiness"] == "paper_ready") != (not missing)
    ):
        raise ValueError(f"Paper Package readiness 字段不一致：{expected_id}")
    _reference_list(
        package.get("source_run_refs"),
        RUN_ID,
        f"{expected_id}.source_run_refs",
        minimum=0,
    )
    supersedes = package.get("supersedes_package_id")
    if supersedes is not None and (
        not isinstance(supersedes, str)
        or PACKAGE_ID.fullmatch(supersedes) is None
    ):
        raise ValueError(f"Paper Package supersedes 无效：{expected_id}")
    _validate_scientific_payloads(
        contents,
        expected_id,
        root=root,
        package=package,
        briefs=briefs,
        tasks=tasks,
        runs=runs,
        events=events,
        catalog=catalog,
        hash_session=hash_session,
    )
    return package


def _validate_scientific_payloads(
    contents: dict[str, bytes],
    package_id: str,
    *,
    root: Path,
    package: dict[str, Any],
    briefs: dict[str, dict[str, Any]],
    tasks: dict[str, dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    catalog: dict[str, dict[str, dict[str, Any]]],
    hash_session: _LiveFileHashSession,
) -> None:
    study = parse_json_object_bytes(
        contents["study.json"], f"{package_id}/study.json"
    )
    if set(study) != {"schema", "brief", "paper_scope"} or study.get(
        "schema"
    ) != "cv-experiment-workflow.paper-package-study.v1":
        raise ValueError(f"Paper Package study.json 无效：{package_id}")
    if contents["study.json"] != _canonical_json_line(study):
        raise ValueError(f"Paper Package study.json 不是规范 JSON：{package_id}")
    experiments = parse_json_object_bytes(
        contents["experiments.json"], f"{package_id}/experiments.json"
    )
    if set(experiments) != {"schema", "items"} or experiments.get(
        "schema"
    ) != "cv-experiment-workflow.paper-package-experiments.v1":
        raise ValueError(f"Paper Package experiments.json 无效：{package_id}")
    if contents["experiments.json"] != _canonical_json_line(experiments):
        raise ValueError(
            f"Paper Package experiments.json 不是规范 JSON：{package_id}"
        )
    visuals = parse_json_object_bytes(
        contents["visuals.json"], f"{package_id}/visuals.json"
    )
    if set(visuals) != {"schema", "items"} or visuals.get(
        "schema"
    ) != "cv-experiment-workflow.paper-package-visuals.v1":
        raise ValueError(f"Paper Package visuals.json 无效：{package_id}")
    if contents["visuals.json"] != _canonical_json_line(visuals):
        raise ValueError(f"Paper Package visuals.json 不是规范 JSON：{package_id}")
    experiment_rows = _selection_list(
        experiments.get("items"),
        f"{package_id}.experiments",
        minimum=1,
    )
    visual_rows = _selection_list(
        visuals.get("items"),
        f"{package_id}.visuals",
        minimum=0,
    )
    claim_rows = _parse_jsonl(
        contents["claims.jsonl"], f"{package_id}/claims.jsonl", "claim_id"
    )
    source_rows = _parse_jsonl(
        contents["sources.jsonl"],
        f"{package_id}/sources.jsonl",
        "source_ref",
        allow_same_primary=True,
    )
    asset_rows = _parse_jsonl(
        contents["assets.jsonl"], f"{package_id}/assets.jsonl", "asset_id"
    )
    if contents["claims.jsonl"] != _canonical_jsonl(claim_rows, "claim_id"):
        raise ValueError(f"Paper Package claims.jsonl 不是规范 JSONL：{package_id}")
    if contents["sources.jsonl"] != _canonical_jsonl(source_rows, "source_ref"):
        raise ValueError(f"Paper Package sources.jsonl 不是规范 JSONL：{package_id}")
    if contents["assets.jsonl"] != _canonical_jsonl(asset_rows, "asset_id"):
        raise ValueError(f"Paper Package assets.jsonl 不是规范 JSONL：{package_id}")

    brief = briefs.get(package["brief_id"])
    if brief is None or not _strict_json_equal(study["brief"], brief):
        raise ValueError(f"Paper Package Research Brief 与账本不一致：{package_id}")
    scope = _exact_object(
        study["paper_scope"],
        {
            "title_hint",
            "research_area",
            "goal",
            "included_claim_ids",
            "excluded_topics",
        },
        f"{package_id}.paper_scope",
    )
    if not _strict_json_equal(scope, package["paper_scope"]):
        raise ValueError(f"Paper Package paper_scope 不一致：{package_id}")
    if (
        scope["research_area"] != brief["research_area"]
        or scope["goal"] != brief["objective"]
        or scope["excluded_topics"] != brief["scope"]["excluded"]
    ):
        raise ValueError(
            f"Paper Package paper_scope 与 Research Brief 不一致：{package_id}"
        )

    claim_fields = {
        "claim_id",
        "kind",
        "origin",
        "statement_zh",
        "statement_en",
        "maturity",
        "run_refs",
        "metric_refs",
        "source_refs",
        "idea_refs",
        "module_refs",
        "innovation_boundary",
        "allowed_sections",
        "comparisons",
    }
    claim_ids = [row.get("claim_id") for row in claim_rows]
    if claim_ids != [f"CLM-{index:04d}" for index in range(1, len(claim_ids) + 1)]:
        raise ValueError(f"Paper Package Claim ID 不连续：{package_id}")
    normalized_scope = _normalize_paper_scope(
        scope,
        claim_ids=claim_ids,
    )
    if (
        not _strict_json_equal(normalized_scope, scope)
        or not _strict_json_equal(scope["included_claim_ids"], claim_ids)
    ):
        raise ValueError(f"Paper Package paper_scope Claim 不一致：{package_id}")
    claim_selection_rows: list[dict[str, Any]] = []
    for row in claim_rows:
        if set(row) != claim_fields:
            raise ValueError(
                f"Paper Package Claim 字段无效：{row.get('claim_id')}"
            )
        if (
            not isinstance(row.get("kind"), str)
            or row["kind"] not in CLAIM_KINDS
            or not isinstance(row.get("origin"), str)
            or row["origin"] not in {"project", "external"}
            or not isinstance(row.get("maturity"), str)
            or row["maturity"] not in {"supported", "confirmed"}
        ):
            raise ValueError(
                f"Paper Package Claim 值无效：{row.get('claim_id')}"
            )
        _exact_object(row, claim_fields, "Claim")
        metric_values = row.get("metric_refs")
        projected_metrics: object = metric_values
        if isinstance(metric_values, list):
            projected_metrics = [
                (
                    {
                        "run_id": metric.get("run_id"),
                        "metric_name": metric.get("metric_name"),
                    }
                    if isinstance(metric, dict)
                    else metric
                )
                for metric in metric_values
            ]
        claim_selection_rows.append(
            {
                key: deepcopy(row[key])
                for key in claim_fields - {"comparisons", "metric_refs"}
            }
            | {"metric_refs": projected_metrics}
        )
    normalized_claims = _normalize_claims(claim_selection_rows)
    if not _strict_json_equal(normalized_claims, claim_selection_rows):
        raise ValueError(
            f"Paper Package Claim 输入字段未规范化：{package_id}"
        )

    sealed_tasks: dict[str, dict[str, Any]] = {}
    for experiment in experiment_rows:
        if not isinstance(experiment, dict):
            raise ValueError(f"Paper Package Experiment 无效：{package_id}")
        task_id = experiment.get("task_id")
        task_snapshot = experiment.get("task")
        if not isinstance(task_id, str) or not isinstance(task_snapshot, dict):
            raise ValueError(f"Paper Package Experiment Task 无效：{package_id}")
        candidate = {"id": task_id, **task_snapshot}
        previous = sealed_tasks.get(task_id)
        if (
            previous is not None
            and not _strict_json_equal(previous, candidate)
        ):
            raise ValueError(
                f"Paper Package 同一 Task 快照不一致：{package_id}/{task_id}"
            )
        sealed_tasks[task_id] = candidate

    source_fields = {
        "source_ref",
        "role",
        "excerpt",
        "supports_claim_ids",
        "kind",
        "identity",
        "locator",
        "revision",
        "license",
        "source_sha256",
        "excerpt_sha256",
    }
    source_input_fields = (
        "source_ref",
        "role",
        "excerpt",
        "supports_claim_ids",
    )
    source_selection_rows = [
        {
            key: deepcopy(row.get(key))
            for key in source_input_fields
        }
        for row in source_rows
    ]
    normalized_sources = _normalize_source_uses(source_selection_rows)
    if not _strict_json_equal(normalized_sources, source_selection_rows):
        raise ValueError(
            f"Paper Package Source 输入字段未规范化：{package_id}"
        )
    source_support: dict[tuple[str, str], set[str]] = {}
    for row in source_rows:
        _exact_object(row, source_fields, "Source")
        source_ref = row["source_ref"]
        excerpt = _text(row["excerpt"], f"{source_ref}.excerpt")
        source = catalog["sources"].get(source_ref)
        if (
            source is None
            or source["kind"] != "paper"
            or not isinstance(row["role"], str)
            or row["role"] not in SOURCE_USE_ROLES
            or not _strict_json_equal(row["kind"], source["kind"])
            or not _strict_json_equal(row["identity"], source["identity"])
            or not _strict_json_equal(row["locator"], source["locator"])
            or not _strict_json_equal(row["revision"], source["revision"])
            or not _strict_json_equal(row["license"], source["license"])
            or not _strict_json_equal(row["source_sha256"], source["digest"])
            or row["excerpt_sha256"] != _sha256_text(excerpt)
        ):
            raise ValueError(
                f"Paper Package Source 与项目账本不一致：{package_id}/{source_ref}"
            )
        supported = _reference_list(
            row["supports_claim_ids"],
            CLAIM_ID,
            f"{package_id}/{source_ref}.supports_claim_ids",
            minimum=0 if row["role"] == "terminology" else 1,
        )
        if not set(supported) <= set(claim_ids):
            raise ValueError(
                f"Paper Package Source 引用了未知 Claim：{package_id}/{source_ref}"
            )
        for claim_id in supported:
            source_support.setdefault((source_ref, claim_id), set()).add(
                row["role"]
            )
    expected_source_rows = _resolve_source_uses(
        source_selection_rows,
        sources=catalog["sources"],
        claim_ids=set(claim_ids),
        hash_session=hash_session,
    )
    if not _strict_json_equal(expected_source_rows, source_rows):
        raise ValueError(f"Paper Package Source 现场文件已漂移：{package_id}")

    result_claim_ids: set[str] = set()
    historically_confirmed = {
        event["subject_id"]
        for event in events
        if event["to"] == "confirmed"
    }
    for row in claim_rows:
        claim_id = row["claim_id"]
        if set(row) != claim_fields:
            raise ValueError(f"Paper Package Claim 字段无效：{claim_id}")
        if (
            not isinstance(row["kind"], str)
            or row["kind"] not in CLAIM_KINDS
            or not isinstance(row["origin"], str)
            or row["origin"] not in {"project", "external"}
            or not isinstance(row["maturity"], str)
            or row["maturity"] not in {"supported", "confirmed"}
        ):
            raise ValueError(f"Paper Package Claim 值无效：{claim_id}")
        _selection_list(
            row["comparisons"],
            f"{claim_id}.comparisons",
            minimum=0,
        )
        _text(row["statement_zh"], f"{claim_id}.statement_zh")
        if row["statement_en"] is not None:
            _text(row["statement_en"], f"{claim_id}.statement_en")
        for field, pattern in (
            ("run_refs", RUN_ID),
            ("source_refs", SOURCE_ID),
            ("idea_refs", IDEA_ID),
            ("module_refs", MODULE_ID),
        ):
            if _reference_list(
                row[field],
                pattern,
                f"{claim_id}.{field}",
                minimum=0,
            ) != row[field]:
                raise ValueError(f"Paper Package Claim 引用未规范化：{claim_id}")
        sections = _selection_text_list(
            row["allowed_sections"],
            f"{claim_id}.allowed_sections",
            minimum=1,
        )
        if sections != row["allowed_sections"] or not set(sections) <= SECTION_IDS:
            raise ValueError(f"Paper Package Claim 章节无效：{claim_id}")
        boundary = row["innovation_boundary"]
        if boundary is not None:
            boundary = _exact_object(
                boundary,
                {"existing_or_prior", "new_contribution", "not_claimed"},
                f"{claim_id}.innovation_boundary",
            )
            for field, value in boundary.items():
                _text(value, f"{claim_id}.innovation_boundary.{field}")
        if any(
            (source_ref, claim_id) not in source_support
            for source_ref in row["source_refs"]
        ):
            raise ValueError(f"Paper Package Claim 缺少 Source 支撑：{claim_id}")

        if row["kind"] == "result":
            if not set(row["run_refs"]) <= historically_confirmed:
                raise ValueError(
                    f"Paper Package result Claim 缺少历史 confirmed Evidence："
                    f"{claim_id}"
                )
            metric_refs = []
            for metric in _selection_list(
                row["metric_refs"],
                f"{claim_id}.metric_refs",
                minimum=1,
            ):
                metric_row = _exact_object(
                    metric,
                    {"run_id", "metric_name", "value", "definition"},
                    f"{claim_id}.metric_ref",
                )
                metric_refs.append(
                    {
                        "run_id": metric_row["run_id"],
                        "metric_name": metric_row["metric_name"],
                    }
                )
            input_claim = {
                key: deepcopy(value)
                for key, value in row.items()
                if key != "comparisons"
            }
            input_claim["metric_refs"] = metric_refs
            expected = _resolve_result_claim(
                input_claim,
                tasks=sealed_tasks,
                runs=runs,
                levels={
                    run_id: "confirmed"
                    for run_id in historically_confirmed
                },
            )
            if not _strict_json_equal(expected, row):
                raise ValueError(
                    f"Paper Package result Claim 与 Run 账本不一致：{claim_id}"
                )
            result_claim_ids.add(claim_id)
        elif row["metric_refs"] or row["comparisons"]:
            raise ValueError(f"Paper Package 非结果 Claim 含结果字段：{claim_id}")
        elif row["origin"] == "external":
            if (
                row["kind"] not in {"background", "research_gap", "method"}
                or not row["source_refs"]
                or row["run_refs"]
                or row["idea_refs"]
                or row["module_refs"]
                or row["innovation_boundary"] is not None
            ):
                raise ValueError(
                    f"Paper Package external Claim 语义无效：{claim_id}"
                )
            _require_expected_maturity(row, "supported")
        elif row["kind"] == "innovation":
            _validate_innovation_claim(
                row,
                sources=catalog["sources"],
                ideas=catalog["ideas"],
                modules=catalog["modules"],
                source_support=source_support,
            )
            _require_expected_maturity(row, "supported")
        elif row["kind"] in {"objective", "method", "protocol", "limitation"}:
            if (
                row["run_refs"]
                or row["idea_refs"]
                or row["module_refs"]
                or row["innovation_boundary"] is not None
            ):
                raise ValueError(
                    f"Paper Package 普通 project Claim 语义无效：{claim_id}"
                )
            _require_expected_maturity(row, "supported")
        else:
            raise ValueError(
                f"Paper Package project Claim 缺少可核验来源：{claim_id}"
            )
    if not result_claim_ids:
        raise ValueError(f"Paper Package 缺少 confirmed result：{package_id}")

    expected_source_runs = sorted(
        {
            run_id
            for row in claim_rows
            for run_id in row["run_refs"]
        }
    )
    if package["source_run_refs"] != expected_source_runs:
        raise ValueError(f"Paper Package source_run_refs 不一致：{package_id}")

    asset_fields = {
        "asset_id",
        "source_object_ref",
        "path",
        "role",
        "required_for_writing",
        "copy_allowed",
        "license",
        "privacy_classification",
        "availability",
        "omission_reason",
        "size_bytes",
        "sha256",
        "reference_uri",
    }
    asset_ids = [row.get("asset_id") for row in asset_rows]
    if asset_ids != [f"AST-{index:04d}" for index in range(1, len(asset_ids) + 1)]:
        raise ValueError(f"Paper Package Asset ID 不连续：{package_id}")
    asset_input_fields = (
        "asset_id",
        "source_object_ref",
        "path",
        "role",
        "required_for_writing",
        "copy_allowed",
        "license",
        "privacy_classification",
        "availability",
        "omission_reason",
    )
    asset_selection_rows = [
        {
            key: deepcopy(row.get(key))
            for key in asset_input_fields
        }
        for row in asset_rows
    ]
    normalized_assets = _normalize_assets(asset_selection_rows)
    if not _strict_json_equal(normalized_assets, asset_selection_rows):
        raise ValueError(
            f"Paper Package Asset 输入字段未规范化：{package_id}"
        )
    for row in asset_rows:
        _exact_object(row, asset_fields, "Asset")
        ref = row["source_object_ref"]
        if not isinstance(ref, str):
            raise ValueError(
                f"Paper Package Asset 来源无效：{row['asset_id']}"
            )
        if SOURCE_ID.fullmatch(ref):
            source = catalog["sources"].get(ref)
            if (
                source is None
                or not _strict_json_equal(
                    row["license"],
                    source["license"],
                )
            ):
                raise ValueError(
                    f"Paper Package Asset license 与 Source 不一致：{row['asset_id']}"
                )
        elif RUN_ID.fullmatch(ref):
            if ref not in runs:
                raise ValueError(
                    f"Paper Package Asset Run 不存在：{row['asset_id']}"
                )
        elif MODULE_ID.fullmatch(ref):
            if ref not in catalog["modules"]:
                raise ValueError(
                    f"Paper Package Asset Module 不存在：{row['asset_id']}"
                )
        else:
            raise ValueError(
                f"Paper Package Asset 来源无效：{row['asset_id']}"
            )
        if (
            type(row["required_for_writing"]) is not bool
            or type(row["copy_allowed"]) is not bool
            or not isinstance(row["privacy_classification"], str)
            or row["privacy_classification"]
            not in {"public", "internal", "sensitive", "restricted"}
            or not isinstance(row["availability"], str)
            or row["availability"]
            not in {"available", "missing", "restricted", "external"}
            or not isinstance(row["license"], str)
            or not isinstance(row["path"], str)
            or not isinstance(row["role"], str)
            or not isinstance(row["omission_reason"], str)
            or not isinstance(row["reference_uri"], str)
            or (
                row["size_bytes"] is not None
                and (
                    type(row["size_bytes"]) is not int
                    or row["size_bytes"] < 0
                )
            )
            or (
                row["sha256"] is not None
                and (
                    not isinstance(row["sha256"], str)
                    or re.fullmatch(
                        r"sha256:[0-9a-f]{64}",
                        row["sha256"],
                    )
                    is None
                )
            )
        ):
            raise ValueError(f"Paper Package Asset 值无效：{row['asset_id']}")
        if row["availability"] == "available" and (
            type(row["size_bytes"]) is not int
            or row["size_bytes"] < 0
            or not isinstance(row["sha256"], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", row["sha256"]) is None
        ):
            raise ValueError(
                f"Paper Package available Asset 身份无效：{row['asset_id']}"
            )
        if row["availability"] != "available" and (
            row["size_bytes"] is not None or row["sha256"] is not None
        ):
            raise ValueError(
                "Paper Package 未读取 Asset 的大小和哈希必须为空："
                f"{row['asset_id']}"
            )

    expected_asset_rows = _resolve_assets(
        root,
        asset_selection_rows,
        runs=runs,
        sources=catalog["sources"],
        modules=catalog["modules"],
        hash_session=hash_session,
    )
    if not _strict_json_equal(expected_asset_rows, asset_rows):
        raise ValueError(f"Paper Package Asset 与现场文件不一致：{package_id}")

    experiment_ids = [
        row.get("experiment_id") if isinstance(row, dict) else None
        for row in experiment_rows
    ]
    if experiment_ids != [
        f"EXP-{index:04d}" for index in range(1, len(experiment_ids) + 1)
    ]:
        raise ValueError(f"Paper Package Experiment ID 不连续：{package_id}")
    asset_map = {row["asset_id"]: row for row in asset_rows}
    asset_by_owner_path = {
        (row["source_object_ref"], row["path"]): row
        for row in asset_rows
    }
    covered_results: set[str] = set()
    experiment_fields = {
        "experiment_id",
        "title",
        "objective",
        "task_id",
        "run_refs",
        "claim_refs",
        "task",
        "runs",
    }
    experiment_input_fields = (
        "experiment_id",
        "title",
        "objective",
        "task_id",
        "run_refs",
        "claim_refs",
    )
    experiment_selection_rows = []
    for row in experiment_rows:
        _exact_object(row, experiment_fields, "Experiment")
        experiment_selection_rows.append(
            {
                key: deepcopy(row[key])
                for key in experiment_input_fields
            }
        )
    normalized_experiments = _normalize_experiments(
        experiment_selection_rows
    )
    if not _strict_json_equal(
        normalized_experiments,
        experiment_selection_rows,
    ):
        raise ValueError(
            f"Paper Package Experiment 输入字段未规范化：{package_id}"
        )
    from .tasking import TASK_FORWARD

    for row in experiment_rows:
        experiment_id = row["experiment_id"]
        _exact_object(row, experiment_fields, "Experiment")
        task = tasks.get(row["task_id"])
        if task is None:
            raise ValueError(f"Paper Package Experiment Task 不存在：{experiment_id}")
        task_snapshot = _exact_object(
            row["task"],
            {
                "task_id",
                "route",
                "target_refs",
                "route_inputs",
                "stage",
                "conclusion",
            },
            f"{experiment_id}.task",
        )
        snapshot_stage = task_snapshot["stage"]
        snapshot_conclusion = task_snapshot["conclusion"]
        if (
            not isinstance(snapshot_stage, str)
            or snapshot_stage not in TASK_FORWARD
            or (
                snapshot_stage == "done"
                and snapshot_conclusion is None
            )
            or (
                snapshot_stage != "done"
                and snapshot_conclusion is not None
            )
        ):
            raise ValueError(
                f"Paper Package Experiment Task 状态无效：{experiment_id}"
            )
        if (
            not _strict_json_equal(task_snapshot["task_id"], task["id"])
            or not _strict_json_equal(task_snapshot["route"], task["route"])
            or not _strict_json_equal(
                task_snapshot["target_refs"],
                task["target_refs"],
            )
            or not _strict_json_equal(
                task_snapshot["route_inputs"],
                _portable_route_inputs_for_delivery(task["route_inputs"]),
            )
        ):
            raise ValueError(
                f"Paper Package Experiment Task 与账本不一致：{experiment_id}"
            )
        if (
            _reference_list(
                row["run_refs"],
                RUN_ID,
                f"{experiment_id}.run_refs",
                minimum=1,
            )
            != row["run_refs"]
            or _reference_list(
                row["claim_refs"],
                CLAIM_ID,
                f"{experiment_id}.claim_refs",
                minimum=1,
            )
            != row["claim_refs"]
            or not set(row["claim_refs"]) <= set(claim_ids)
        ):
            raise ValueError(
                f"Paper Package Experiment 引用无效：{experiment_id}"
            )
        run_row_ids = []
        for run_row in _selection_list(
            row["runs"],
            f"{experiment_id}.runs",
            minimum=1,
        ):
            _exact_object(
                run_row,
                {
                    "run_id",
                    "purpose",
                    "frozen_digest",
                    "frozen",
                    "execution",
                    "result",
                    "quality",
                    "analysis",
                    "artifact_paths",
                    "files",
                },
                f"{experiment_id}.run",
            )
            run_id = run_row["run_id"]
            if (
                not isinstance(run_id, str)
                or RUN_ID.fullmatch(run_id) is None
            ):
                raise ValueError(
                    f"Paper Package Experiment Run ID 无效：{experiment_id}"
                )
            live = runs.get(run_id)
            if live is None or live["task_id"] != task["id"]:
                raise ValueError(
                    f"Paper Package Experiment Run 不存在：{experiment_id}/{run_id}"
                )
            if not _sealed_run_matches_live(run_row, live):
                raise ValueError(
                    f"Paper Package Experiment Run 与账本不一致：{experiment_id}/{run_id}"
                )
            result = live["result"]
            expected_paths = [
                result.get("raw_log"),
                *live.get("artifacts", []),
            ]
            if not _strict_json_equal(
                run_row["artifact_paths"],
                live.get("artifacts", []),
            ):
                raise ValueError(
                    f"Paper Package Experiment artifact_paths 与账本不一致："
                    f"{experiment_id}/{run_id}"
                )
            if (
                not expected_paths
                or any(not isinstance(path, str) for path in expected_paths)
                or len(expected_paths) != len(set(expected_paths))
            ):
                raise ValueError(
                    f"Paper Package live Run 文件清单无效："
                    f"{experiment_id}/{run_id}"
                )
            expected_files: list[dict[str, Any]] = []
            for path in expected_paths:
                asset = asset_by_owner_path.get((run_id, path))
                if asset is None:
                    continue
                expected_files.append(
                    {
                        "path": path,
                        "asset_id": asset["asset_id"],
                        "size_bytes": asset["size_bytes"],
                        "sha256": asset["sha256"],
                    }
                )
            if not _strict_json_equal(run_row["files"], expected_files):
                raise ValueError(
                    f"Paper Package Experiment 文件清单不完整或重复："
                    f"{experiment_id}/{run_id}"
                )
            for file_row in _selection_list(
                run_row["files"],
                f"{experiment_id}/{run_id}.files",
                minimum=0,
            ):
                _exact_object(
                    file_row,
                    {"path", "asset_id", "size_bytes", "sha256"},
                    f"{experiment_id}/{run_id}.file",
                )
                file_asset_id = file_row["asset_id"]
                if (
                    not isinstance(file_asset_id, str)
                    or ASSET_ID.fullmatch(file_asset_id) is None
                ):
                    raise ValueError(
                        f"Paper Package Experiment Asset ID 无效："
                        f"{experiment_id}/{run_id}"
                    )
                asset = asset_map.get(file_asset_id)
                if (
                    asset is None
                    or not _strict_json_equal(
                        asset["source_object_ref"],
                        run_id,
                    )
                    or not _strict_json_equal(
                        asset["path"],
                        file_row["path"],
                    )
                    or not _strict_json_equal(
                        asset["size_bytes"],
                        file_row["size_bytes"],
                    )
                    or not _strict_json_equal(
                        asset["sha256"],
                        file_row["sha256"],
                    )
                ):
                    raise ValueError(
                        f"Paper Package Experiment 文件与 Asset 不一致："
                        f"{experiment_id}/{run_id}"
                    )
            run_row_ids.append(run_id)
        if not _strict_json_equal(run_row_ids, row["run_refs"]):
            raise ValueError(
                f"Paper Package Experiment Run 引用不一致：{experiment_id}"
            )
        covered_results.update(set(row["claim_refs"]) & result_claim_ids)
    if covered_results != result_claim_ids:
        raise ValueError(f"Paper Package result Claim 未被 Experiment 覆盖：{package_id}")

    expected_missing = _derive_missing_requirements(
        claims=claim_rows,
        assets=asset_rows,
        experiments=experiment_rows,
        runs=runs,
    )
    if not _strict_json_equal(
        package["missing_requirements"],
        expected_missing,
    ):
        raise ValueError(f"Paper Package missing_requirements 不一致：{package_id}")

    visual_fields = {
        "visual_id",
        "kind",
        "title",
        "purpose",
        "allowed_sections",
        "claim_refs",
        "experiment_refs",
        "asset_refs",
    }
    visual_ids = [
        row.get("visual_id") if isinstance(row, dict) else None
        for row in visual_rows
    ]
    if visual_ids != [
        f"VIS-{index:04d}" for index in range(1, len(visual_ids) + 1)
    ]:
        raise ValueError(f"Paper Package Visual ID 不连续：{package_id}")
    normalized_visuals = _normalize_visuals(visual_rows)
    if not _strict_json_equal(normalized_visuals, visual_rows):
        raise ValueError(
            f"Paper Package Visual 输入字段未规范化：{package_id}"
        )
    for row in visual_rows:
        _exact_object(row, visual_fields, "Visual")
        sections = _selection_text_list(
            row["allowed_sections"],
            f"{row['visual_id']}.allowed_sections",
            minimum=1,
        )
        claim_refs = _reference_list(
            row["claim_refs"],
            CLAIM_ID,
            f"{row['visual_id']}.claim_refs",
            minimum=1,
        )
        experiment_refs = _reference_list(
            row["experiment_refs"],
            EXPERIMENT_ID,
            f"{row['visual_id']}.experiment_refs",
            minimum=0,
        )
        asset_refs = _reference_list(
            row["asset_refs"],
            ASSET_ID,
            f"{row['visual_id']}.asset_refs",
            minimum=0,
        )
        if (
            not isinstance(row["kind"], str)
            or row["kind"] not in {"figure", "table"}
            or not set(claim_refs) <= set(claim_ids)
            or not set(experiment_refs) <= set(experiment_ids)
            or not set(asset_refs) <= set(asset_ids)
            or not set(sections) <= SECTION_IDS
        ):
            raise ValueError(f"Paper Package Visual 引用无效：{row['visual_id']}")


def _sealed_run_matches_live(
    sealed: dict[str, Any],
    live: dict[str, Any],
) -> bool:
    for field in (
        "purpose",
        "frozen_digest",
        "result",
        "quality",
        "analysis",
    ):
        if not _strict_json_equal(sealed[field], live[field]):
            return False
    if not _strict_json_equal(
        sealed["frozen"],
        _portable_frozen_for_delivery(live["frozen"]),
    ):
        return False
    sealed_execution = sealed["execution"]
    live_execution = live["execution"]
    if _strict_json_equal(sealed_execution, live_execution):
        return True
    if (
        not isinstance(sealed_execution, dict)
        or not isinstance(live_execution, dict)
        or set(sealed_execution) != set(live_execution)
        or sealed_execution.get("stage") != "finished"
        or live_execution.get("stage") != "closed"
        or sealed_execution.get("closed_at") is not None
        or not isinstance(live_execution.get("closed_at"), str)
    ):
        return False
    return all(
        _strict_json_equal(
            sealed_execution.get(field),
            live_execution.get(field),
        )
        for field in set(sealed_execution) - {"stage", "closed_at"}
    )


def _validate_supersedes(
    existing: dict[str, dict[str, Any]],
    supersedes: str | None,
) -> None:
    if supersedes is None:
        return
    if supersedes not in existing:
        raise ValueError(f"supersedes_package_id 不存在：{supersedes}")
    if any(
        item["supersedes_package_id"] == supersedes
        for item in existing.values()
    ):
        raise ValueError(f"目标 Paper Package 已被直接 supersede：{supersedes}")


def _canonical_json_line(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("Paper Package 只能冻结有限 JSON") from error


def _canonical_jsonl(
    rows: list[dict[str, Any]],
    key: str,
) -> bytes:
    ordered = sorted(rows, key=lambda item: item[key])
    lines = [_canonical_json_line(item) for item in ordered]
    if any(len(line) > MAX_JSONL_LINE_SIZE for line in lines):
        raise ValueError("Paper Package JSONL 单行超过 256 KiB")
    if len(lines) > MAX_JSONL_ROWS:
        raise ValueError("Paper Package JSONL 超过 2048 行")
    return b"".join(lines)


def _parse_jsonl(
    content: bytes,
    label: str,
    primary_key: str,
    *,
    allow_same_primary: bool = False,
) -> list[dict[str, Any]]:
    if not content:
        return []
    if not content.endswith(b"\n"):
        raise ValueError(f"{label} 必须以换行结尾")
    lines = content.splitlines()
    if len(lines) > MAX_JSONL_ROWS:
        raise ValueError(f"{label} 超过 2048 行")
    rows: list[dict[str, Any]] = []
    previous: str | None = None
    for line in lines:
        if not line or len(line) + 1 > MAX_JSONL_LINE_SIZE:
            raise ValueError(f"{label} 含空行或超长行")
        row = parse_json_object_bytes(line, label)
        current = row.get(primary_key)
        if not isinstance(current, str):
            raise ValueError(f"{label} 缺少 {primary_key}")
        if previous is not None and (
            current < previous
            or (current == previous and not allow_same_primary)
        ):
            raise ValueError(f"{label} 未按稳定 ID 排序")
        previous = current
        rows.append(row)
    return rows


def _content_sha256(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in SCIENTIFIC_FILES:
        content = files[name]
        path = name.encode("ascii")
        digest.update(len(path).to_bytes(8, "big"))
        digest.update(path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _checksum_file(files: dict[str, bytes]) -> bytes:
    return b"".join(
        f"{hashlib.sha256(files[name]).hexdigest()}  {name}\n".encode("ascii")
        for name in CHECKSUM_FILES
    )


def _validate_checksum_bytes(contents: dict[str, bytes]) -> None:
    expected = _checksum_file(contents)
    if contents["checksums.sha256"] != expected:
        raise ValueError("Paper Package checksums.sha256 不一致")


def _validate_package_file_limits(files: dict[str, bytes]) -> None:
    if set(files) != set(PACKAGE_FILES):
        raise ValueError("Paper Package 文件集合不完整")
    if any(len(content) > MAX_PACKAGE_FILE_SIZE for content in files.values()):
        raise ValueError("Paper Package 单文件超过 8 MiB")
    if sum(map(len, files.values())) > MAX_PACKAGE_TOTAL_SIZE:
        raise ValueError("Paper Package 8 文件合计超过 32 MiB")


def _publish_package(
    control: Path,
    package_id: str,
    files: dict[str, bytes],
    *,
    first: bool,
    project: dict[str, Any],
    briefs: dict[str, dict[str, Any]],
    tasks: dict[str, dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    catalog: dict[str, dict[str, dict[str, Any]]],
    hash_session: _LiveFileHashSession,
) -> None:
    project_root = control.parent
    staging = project_root / (
        f"{INITIAL_STAGING_PREFIX}{uuid.uuid4().hex}{INITIAL_STAGING_SUFFIX}"
    )
    staging_created = False
    identity: os.stat_result | None = None
    published = False
    try:
        staging.mkdir()
        staging_created = True
        identity = staging.lstat()
        destination: Path
        if first:
            package_directory = staging / package_id
            destination = control / "paper-packages" / "packages"
        else:
            package_directory = staging
            destination = (
                control / "paper-packages" / "packages" / package_id
            )
        package_directory.mkdir(exist_ok=not first)
        for name in PACKAGE_FILES:
            if not atomic_create_bytes_clean(
                package_directory / name,
                files[name],
                transaction_id=uuid.uuid4().hex,
            ):
                raise FileExistsError(
                    f"Paper Package staging 文件已存在：{name}"
                )
        _validate_package_directory(
            package_directory,
            root=control.parent,
            expected_id=package_id,
            project=project,
            briefs=briefs,
            tasks=tasks,
            runs=runs,
            events=events,
            catalog=catalog,
            hash_session=hash_session,
        )
        if os.path.lexists(destination):
            raise FileExistsError(
                f"Paper Package 目标已存在，拒绝覆盖：{destination}"
            )
        _publish_directory_no_replace(staging, destination)
        published = True
    except BaseException:
        if not published and staging_created:
            if identity is None:
                staging.rmdir()
            else:
                _cleanup_initial_staging(staging, identity)
        raise


class _LiveFileHashSession:
    """在一次锁内快照中去重现场文件哈希，并限制累计读取量。"""

    def __init__(self, max_total_size: int | None = None) -> None:
        self.max_total_size = (
            MAX_LIVE_ASSET_TOTAL_SIZE
            if max_total_size is None
            else max_total_size
        )
        self.total_size = 0
        self._records: list[
            tuple[str, os.stat_result, dict[str, Any]]
        ] = []

    def hash(
        self,
        path: Path,
        label: str,
        limit: int,
        *,
        root: Path | None = None,
    ) -> dict[str, Any]:
        path, before, resolved = _inspect_live_regular_file(
            path,
            label,
            limit,
            root=root,
        )
        canonical_path = os.path.normcase(os.path.normpath(str(resolved)))
        for known_path, known_identity, facts in self._records:
            same_file = False
            try:
                same_file = os.path.samestat(before, known_identity)
            except OSError:
                same_file = False
            if (
                canonical_path == known_path
                and same_file
                and before.st_size == known_identity.st_size
                and before.st_mtime_ns == known_identity.st_mtime_ns
            ):
                return deepcopy(facts)
        if self.total_size + before.st_size > self.max_total_size:
            raise ValueError(
                "Paper Package 现场文件总哈希预算超过 "
                f"{self.max_total_size} bytes；大数据、checkpoint 或完整仓库"
                "应标为 external/referenced"
            )
        self.total_size += before.st_size
        facts = _hash_live_regular_file(
            path,
            label,
            limit,
            root=root,
            _expected_before=before,
            _expected_resolved=resolved,
        )
        self._records.append((canonical_path, before, deepcopy(facts)))
        return facts


def _inspect_live_regular_file(
    path: Path,
    label: str,
    limit: int,
    *,
    root: Path | None = None,
) -> tuple[Path, os.stat_result, Path]:
    path = Path(path)
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"{label} 不存在或无法定位：{path}") from error
    if (
        stat.S_ISLNK(before.st_mode)
        or is_link_or_reparse(path)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size > limit
    ):
        raise ValueError(f"{label} 必须是大小受限的普通文件：{path}")
    if root is not None:
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise ValueError(f"{label} 逃出项目目录：{path}") from error
    return path, before, resolved


def _hash_live_regular_file(
    path: Path,
    label: str,
    limit: int,
    *,
    root: Path | None = None,
    _expected_before: os.stat_result | None = None,
    _expected_resolved: Path | None = None,
) -> dict[str, Any]:
    path, before, resolved = _inspect_live_regular_file(
        path,
        label,
        limit,
        root=root,
    )
    if _expected_before is not None and (
        not os.path.samestat(before, _expected_before)
        or before.st_size != _expected_before.st_size
        or before.st_mtime_ns != _expected_before.st_mtime_ns
        or (
            _expected_resolved is not None
            and resolved != _expected_resolved
        )
    ):
        raise ValueError(f"{label} 在哈希前身份变化：{path}")
    digest = hashlib.sha256()
    bytes_read = 0
    try:
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or not os.path.samestat(before, opened)
            ):
                raise ValueError(f"{label} 在哈希前身份变化：{path}")
            while True:
                chunk = source.read(HASH_CHUNK_BYTES)
                if not chunk:
                    break
                bytes_read += len(chunk)
                if bytes_read > limit:
                    raise ValueError(
                        f"{label} 读取期间超过大小上限：{path}"
                    )
                if bytes_read > before.st_size:
                    raise ValueError(
                        f"{label} 在哈希期间发生变化：{path}"
                    )
                digest.update(chunk)
            after = os.fstat(source.fileno())
        current = path.lstat()
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(f"{label} 无法读取：{path}") from error
    if (
        bytes_read != before.st_size
        or not os.path.samestat(opened, after)
        or not os.path.samestat(before, current)
        or current.st_size != before.st_size
        or current.st_mtime_ns != before.st_mtime_ns
    ):
        raise ValueError(f"{label} 在哈希期间发生变化：{path}")
    return {
        "size_bytes": bytes_read,
        "sha256": f"sha256:{digest.hexdigest()}",
    }


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _selection_list(
    value: object,
    label: str,
    *,
    minimum: int,
) -> list[Any]:
    if (
        not isinstance(value, list)
        or len(value) < minimum
        or len(value) > MAX_JSONL_ROWS
    ):
        raise ValueError(
            f"{label} 必须是包含 {minimum}..{MAX_JSONL_ROWS} 项的列表"
        )
    return value


def _selection_text_list(
    value: object,
    label: str,
    *,
    minimum: int,
) -> list[str]:
    items = _selection_list(value, label, minimum=minimum)
    normalized = [_text(item, label) for item in items]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} 不得重复")
    return normalized


def _reference_list(
    value: object,
    pattern: re.Pattern[str],
    label: str,
    *,
    minimum: int,
) -> list[str]:
    items = _selection_list(value, label, minimum=minimum)
    if not all(
        isinstance(item, str) and pattern.fullmatch(item) is not None
        for item in items
    ):
        raise ValueError(f"{label} 含无效 ID")
    if len(items) != len(set(items)):
        raise ValueError(f"{label} 不得重复")
    return list(items)


def _sequential_id(
    value: object,
    pattern: re.Pattern[str],
    prefix: str,
    number: int,
    label: str,
) -> str:
    expected = f"{prefix}-{number:04d}"
    if (
        not isinstance(value, str)
        or pattern.fullmatch(value) is None
        or value != expected
    ):
        raise ValueError(f"{label} 必须连续，期望 {expected}")
    return value


def _normalize_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != INPUT_FIELDS:
        raise ValueError("Research Brief manifest 字段不完整或含未知字段")
    questions = _text_list(
        payload.get("research_questions"),
        "research_questions",
        minimum=1,
    )
    hypotheses = _normalize_hypotheses(payload.get("hypotheses"))
    scope = _normalize_scope(payload.get("scope"))
    terminology = _normalize_terminology(payload.get("terminology"))
    contributions = _normalize_contributions(
        payload.get("planned_contributions")
    )
    confirmation = _normalize_confirmation(payload.get("user_confirmation"))
    return {
        "title": _text(payload.get("title"), "title"),
        "research_area": _text(payload.get("research_area"), "research_area"),
        "background": _text(payload.get("background"), "background"),
        "problem": _text(payload.get("problem"), "problem"),
        "objective": _text(payload.get("objective"), "objective"),
        "research_questions": questions,
        "hypotheses": hypotheses,
        "scope": scope,
        "terminology": terminology,
        "planned_contributions": contributions,
        "user_confirmation": confirmation,
    }


def _normalize_hypotheses(value: object) -> list[dict[str, str]]:
    items = _bounded_list(value, "hypotheses", minimum=1)
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        row = _exact_object(
            item,
            {"hypothesis_id", "statement", "falsification_criteria"},
            "hypotheses item",
        )
        hypothesis_id = _text(row.get("hypothesis_id"), "hypothesis_id")
        if hypothesis_id in seen:
            raise ValueError(f"hypothesis_id 重复：{hypothesis_id}")
        seen.add(hypothesis_id)
        normalized.append(
            {
                "hypothesis_id": hypothesis_id,
                "statement": _text(row.get("statement"), "statement"),
                "falsification_criteria": _text(
                    row.get("falsification_criteria"),
                    "falsification_criteria",
                ),
            }
        )
    return normalized


def _normalize_scope(value: object) -> dict[str, list[str]]:
    row = _exact_object(value, {"included", "excluded"}, "scope")
    return {
        "included": _text_list(row.get("included"), "scope.included", minimum=1),
        "excluded": _text_list(row.get("excluded"), "scope.excluded", minimum=0),
    }


def _normalize_terminology(value: object) -> list[dict[str, str]]:
    items = _bounded_list(value, "terminology", minimum=1)
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        row = _exact_object(
            item,
            {"term_en", "term_zh", "definition_zh"},
            "terminology item",
        )
        term_en = _text(row.get("term_en"), "term_en")
        term_zh = _text(row.get("term_zh"), "term_zh")
        key = (term_en, term_zh)
        if key in seen:
            raise ValueError(f"terminology 术语重复：{term_en} / {term_zh}")
        seen.add(key)
        normalized.append(
            {
                "term_en": term_en,
                "term_zh": term_zh,
                "definition_zh": _text(
                    row.get("definition_zh"),
                    "definition_zh",
                ),
            }
        )
    return normalized


def _normalize_contributions(value: object) -> list[dict[str, Any]]:
    items = _bounded_list(value, "planned_contributions", minimum=1)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        row = _exact_object(
            item,
            {
                "contribution_id",
                "statement_zh",
                "provenance",
                "source_refs",
            },
            "planned_contributions item",
        )
        contribution_id = _text(
            row.get("contribution_id"),
            "contribution_id",
        )
        if contribution_id in seen:
            raise ValueError(f"contribution_id 重复：{contribution_id}")
        seen.add(contribution_id)
        provenance = row.get("provenance")
        if provenance not in {"original", "paper_inspired", "adapted"}:
            raise ValueError(
                "provenance 必须是 original、paper_inspired 或 adapted"
            )
        refs = _source_ref_list(row.get("source_refs"))
        if provenance != "original" and not refs:
            raise ValueError(
                f"{provenance} planned contribution 至少需要一个 source_ref"
            )
        normalized.append(
            {
                "contribution_id": contribution_id,
                "statement_zh": _text(
                    row.get("statement_zh"),
                    "statement_zh",
                ),
                "provenance": provenance,
                "source_refs": refs,
            }
        )
    return normalized


def _normalize_confirmation(value: object) -> dict[str, Any]:
    row = _exact_object(
        value,
        {"confirmed", "confirmed_at", "confirmed_by"},
        "user_confirmation",
    )
    if row.get("confirmed") is not True:
        raise ValueError("user_confirmation.confirmed 必须精确为 true")
    confirmed_at = row.get("confirmed_at")
    if not _is_rfc3339(confirmed_at):
        raise ValueError("user_confirmation.confirmed_at 必须是 RFC3339 时间")
    return {
        "confirmed": True,
        "confirmed_at": confirmed_at,
        "confirmed_by": _text(row.get("confirmed_by"), "confirmed_by"),
    }


def _validate_contribution_sources(
    contributions: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]],
) -> None:
    for contribution in contributions:
        for source_ref in contribution["source_refs"]:
            if source_ref not in sources:
                raise ValueError(f"planned contribution Source 不存在：{source_ref}")


def _source_ref_list(value: object) -> list[str]:
    items = _bounded_list(value, "source_refs", minimum=0)
    refs: list[str] = []
    for item in items:
        if not isinstance(item, str) or SOURCE_ID.fullmatch(item) is None:
            raise ValueError(f"source_ref 格式无效：{item}")
        refs.append(item)
    if len(refs) != len(set(refs)):
        raise ValueError("source_refs 不得重复")
    return refs


def _text_list(value: object, label: str, *, minimum: int) -> list[str]:
    items = _bounded_list(value, label, minimum=minimum)
    normalized = [_text(item, label) for item in items]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} 不得重复")
    return normalized


def _bounded_list(value: object, label: str, *, minimum: int) -> list[Any]:
    if (
        not isinstance(value, list)
        or len(value) < minimum
        or len(value) > MAX_ITEMS
    ):
        raise ValueError(
            f"{label} 必须是包含 {minimum} 到 {MAX_ITEMS} 项的列表"
        )
    return value


def _exact_object(
    value: object,
    fields: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} 字段不完整或含未知字段")
    return value


def _text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_TEXT
    ):
        raise ValueError(f"{label} 必须是规范的非空文本")
    return value


def _is_rfc3339(value: object) -> bool:
    if not isinstance(value, str) or RFC3339.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _publish_initial_brief_directory(
    control: Path,
    payload: dict[str, Any],
    sources: dict[str, dict[str, Any]],
) -> None:
    project_root = control.parent
    staging = project_root / (
        f"{INITIAL_STAGING_PREFIX}{uuid.uuid4().hex}{INITIAL_STAGING_SUFFIX}"
    )
    staging_created = False
    staging_identity: os.stat_result | None = None
    published = False
    try:
        staging.mkdir()
        staging_created = True
        staging_identity = staging.lstat()
        briefs = staging / "briefs"
        briefs.mkdir()
        path = briefs / "BRIEF-0001.json"
        if not atomic_create_json(
            path,
            payload,
            transaction_id=uuid.uuid4().hex,
        ):
            raise FileExistsError(
                f"Research Brief staging 目标已存在，拒绝覆盖：{path}"
            )
        validate_research_brief(
            read_bounded_json_object(
                path,
                MAX_RECORD_SIZE,
                "Research Brief BRIEF-0001",
            ),
            expected_id="BRIEF-0001",
            expected_project_id=payload["project_id"],
            sources=sources,
            expected_revision=1,
        )
        destination = control / "paper-packages"
        if os.path.lexists(destination):
            raise FileExistsError(
                f"paper-packages 已存在，拒绝覆盖：{destination}"
            )
        _publish_directory_no_replace(staging, destination)
        published = True
    except BaseException:
        if not published and staging_created:
            if staging_identity is None:
                staging.rmdir()
            else:
                _cleanup_initial_staging(staging, staging_identity)
        raise


def _publish_directory_no_replace(source: Path, destination: Path) -> None:
    """原子发布目录；竞争目标出现时绝不替换。"""
    if os.name == "nt":
        os.rename(source, destination)
        return

    if sys.platform.startswith("linux"):
        at_current_working_directory = -100
        flag = 1  # RENAME_NOREPLACE
    elif sys.platform == "darwin":
        at_current_working_directory = -2
        flag = 4  # RENAME_EXCL
    else:
        raise OSError(
            getattr(os, "ENOTSUP", 95),
            "当前平台不支持目录原子无覆盖发布",
            str(destination),
        )

    import ctypes

    library = ctypes.CDLL(None, use_errno=True)
    symbol = (
        "renameat2" if sys.platform.startswith("linux") else "renameatx_np"
    )
    rename_no_replace = getattr(library, symbol, None)
    if rename_no_replace is None:
        raise OSError(
            getattr(os, "ENOTSUP", 95),
            "当前平台不支持目录原子无覆盖发布",
            str(destination),
        )
    rename_no_replace.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename_no_replace.restype = ctypes.c_int
    result = rename_no_replace(
        at_current_working_directory,
        os.fsencode(source),
        at_current_working_directory,
        os.fsencode(destination),
        flag,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            str(destination),
        )
def _cleanup_initial_staging(
    staging: Path,
    expected_identity: os.stat_result,
) -> None:
    if not os.path.lexists(staging):
        return
    current = staging.lstat()
    if (
        stat.S_ISLNK(current.st_mode)
        or is_link_or_reparse(staging)
        or not stat.S_ISDIR(current.st_mode)
        or not os.path.samestat(expected_identity, current)
    ):
        raise RuntimeError(
            f"Research Brief staging 身份变化，拒绝自动清理：{staging}"
        )
    shutil.rmtree(staging)


def _require_regular_directory(path: Path, label: str) -> None:
    if (
        not os.path.lexists(path)
        or is_link_or_reparse(path)
        or not path.is_dir()
    ):
        raise ValueError(f"{label} 必须是普通目录且不得为链接/reparse：{path}")
