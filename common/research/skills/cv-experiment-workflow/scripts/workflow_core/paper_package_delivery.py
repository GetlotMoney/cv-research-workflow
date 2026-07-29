from __future__ import annotations

import base64
import codecs
import hashlib
import math
import os
import re
import shutil
import stat
import unicodedata
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paper_package as core
from .io import (
    parse_json_object_bytes,
    read_bounded_json_object,
    read_bounded_regular_file,
)
from .locking import project_snapshot_lock
from .project import PROJECT_SCHEMA_V2, is_link_or_reparse
from .records import _initialized_project, _read_object, _validate_project


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_RAW_SHA256 = re.compile(r"[0-9a-f]{64}")
_HANDOFF_STAGING_NAME = re.compile(r"\.[A-Za-z0-9_-]{7}")
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"}
_WINDOWS_RESERVED.update(f"com{number}" for number in range(1, 10))
_WINDOWS_RESERVED.update(f"lpt{number}" for number in range(1, 10))
_WINDOWS_ABSOLUTE_IN_TEXT = re.compile(
    r"(?:(?<![A-Za-z0-9+.-])[A-Za-z]:[\\/]|\\\\)",
)
_WINDOWS_CURRENT_DRIVE_ROOT_IN_TEXT = re.compile(
    r"(?<![\\/A-Za-z0-9])\\Users(?:[\\/]|$)",
    re.IGNORECASE,
)
_FORWARD_SLASH_UNC_IN_TEXT = re.compile(r"(?<!:)//")
_POSIX_ABSOLUTE_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9/])/(?!/)(?=[^\s\"'])",
)
_BINARY_POSIX_ABSOLUTE_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9/])/(?!/)(?:"
    r"(?:home|Users|tmp|var|opt|etc|mnt|media|srv|root|private)"
    r"(?:/[^\x00-\x20\x7f\s/\\\"'\ufffd]{2,})?"
    r"|"
    r"(?:[A-Za-z0-9._~()+,=@%-]{2,}/){2,}"
    r"[A-Za-z0-9._~()+,=@%-]{2,}"
    r")",
)
_BINARY_WINDOWS_DRIVE_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9+.-])[A-Za-z]:[\\/](?:"
    r"(?:Users|Windows|ProgramData|Temp)[\\/]"
    r"[^\x00-\x20\x7f\s/\\:\"'\ufffd]{2,}"
    r"|"
    r"[A-Za-z0-9._~()+,=@%-]{2,}[\\/]"
    r"[A-Za-z0-9._~()+,=@%-]{2,}"
    r")",
)
_BINARY_WINDOWS_ROOTED_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9\\])\\(?!\\)(?:"
    r"(?:Users|Windows|ProgramData|Temp)[\\/]"
    r"[^\x00-\x20\x7f\s/\\\"'\ufffd]{2,}"
    r"|"
    r"(?:[A-Za-z0-9._~()+,=@%-]{3,}[\\/]){2,}"
    r"[A-Za-z0-9._~()+,=@%-]{2,}"
    r")",
)
_BINARY_WINDOWS_UNC_IN_TEXT = re.compile(
    r"\\\\[A-Za-z0-9._~-]{3,}[\\/]"
    r"[A-Za-z0-9._~$-]{3,}",
)
_BINARY_FORWARD_SLASH_UNC_IN_TEXT = re.compile(
    r"(?<!:)//[A-Za-z0-9._~-]{3,}/"
    r"[A-Za-z0-9._~$-]{3,}",
)
_BINARY_HOME_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9._~-])~[\\/]"
    r"(?:[A-Za-z0-9._~()+,=@%-]{2,}[\\/])"
    r"[A-Za-z0-9._~()+,=@%-]{2,}",
)
_TEXT_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_PACKAGE_FIELDS = {
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
_MANIFEST_FIELDS = _PACKAGE_FIELDS | {"delivery"}
_BOUND_MANIFEST_FIELDS = _MANIFEST_FIELDS | {"reverification"}
_DELIVERY_FIELDS = {"layout_revision", "files", "assets"}
_DELIVERY_FILE_FIELDS = {"path", "size_bytes", "sha256"}
_DELIVERY_ASSET_FIELDS = {"asset_id", "status", "package_path", "reason"}


def export_paper_package(
    project: Path,
    package_id: str,
    mode: str,
    out: Path,
) -> dict[str, Any]:
    """把内部 PKG 导出成无需科研项目也能核验的交付目录。"""
    if not isinstance(package_id, str) or core.PACKAGE_ID.fullmatch(package_id) is None:
        raise ValueError("package 必须是 PKG-0001 格式")
    if mode not in {"hybrid", "full"}:
        raise ValueError("mode 只能是 hybrid 或 full")
    output = Path(out)
    if output.name != package_id:
        raise ValueError("out 必须直接指向与 PKG ID 同名的最终目录")
    parent = output.parent
    core._require_regular_directory(parent, "Paper Package 导出父目录")
    if os.path.lexists(output):
        raise FileExistsError(f"Paper Package 导出目标已存在，拒绝覆盖：{output}")

    root = _initialized_project(Path(project))
    with project_snapshot_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        from .validation import validate_v2_full_snapshot_locked

        hash_session = core._LiveFileHashSession()
        (
            tasks,
            runs,
            events,
            _evidence_content,
            catalog,
            _briefs,
            packages,
        ) = validate_v2_full_snapshot_locked(
            control,
            hash_session=hash_session,
        )
        package = packages.get(package_id)
        if package is None:
            raise ValueError(f"Paper Package 不存在：{package_id}")
        if package["asset_mode"] != mode:
            raise ValueError(
                "导出 mode 必须与内部 Paper Package 的 asset_mode 完全一致"
            )
        internal = _read_internal_package(control, package_id)
        if not core._strict_json_equal(
            parse_json_object_bytes(
                internal["package.json"],
                f"{package_id}/package.json",
            ),
            package,
        ):
            raise ValueError(f"Paper Package 清单与已验证快照不一致：{package_id}")
        asset_rows = core._parse_jsonl(
            internal["assets.jsonl"],
            f"{package_id}/assets.jsonl",
            "asset_id",
        )
        bound_profile = (
            core._producer_tuple(package["producer"])
            == core.BOUND_EVIDENCE_PRODUCER_TUPLE
        )
        evidence_bindings: dict[str, dict[str, Any]] = {}
        scientific = {
            name: internal[name]
            for name in core.SCIENTIFIC_FILES
        }
        if bound_profile:
            evidence_bindings = core._reverify_bound_runs_locked(
                control,
                source_run_refs=package["source_run_refs"],
                tasks=tasks,
                runs=runs,
                events=events,
            )
            scientific = _bound_scientific_files(
                control=control,
                internal=internal,
                tasks=tasks,
                runs=runs,
                evidence_bindings=evidence_bindings,
                source_run_refs=package["source_run_refs"],
            )
        delivery_assets, copy_items = _plan_assets(
            root=root,
            package=package,
            mode=mode,
            asset_rows=asset_rows,
            runs=runs,
            catalog=catalog,
        )
        delivery_files = _scientific_delivery_files(scientific)
        delivery_files.extend(
            {
                "path": item["package_path"],
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
            }
            for item in sorted(
                copy_items,
                key=lambda value: value["package_path"].encode("utf-8"),
            )
        )
        manifest: dict[str, Any] = {
            **deepcopy(package),
            "schema": core.PAPER_PACKAGE_HANDOFF_SCHEMA,
            "content_sha256": core._content_sha256(scientific),
            "delivery": {
                "layout_revision": 1,
                "files": delivery_files,
                "assets": delivery_assets,
            },
        }
        if bound_profile:
            payload_sha256 = _package_payload_sha256(
                scientific,
                copy_items,
            )
            manifest["reverification"] = {
                "schema": core.BOUND_EVIDENCE_REVERIFICATION_SCHEMA,
                "status": "pass",
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "producer": deepcopy(core.BOUND_EVIDENCE_PRODUCER),
                "payload_sha256": payload_sha256,
                "runs": [
                    {
                        "run_id": run_id,
                        "frozen_digest": runs[run_id]["frozen_digest"],
                        "snapshot_sha256": runs[run_id][
                            "execution_snapshot"
                        ]["snapshot_sha256"],
                        "seal_sha256": _portable_output_seal(
                            runs[run_id]
                        )["seal_sha256"],
                        "evidence_sha256": evidence_bindings[run_id][
                            "evidence_sha256"
                        ],
                    }
                    for run_id in package["source_run_refs"]
                ],
            }
            _validate_bound_portability(
                manifest,
                scientific,
                copy_items,
            )
        manifest_bytes = core._canonical_json_line(manifest)
        control_files = {
            "manifest.json": manifest_bytes,
            **scientific,
        }
        checksum_bytes = _handoff_checksum_bytes(
            {
                **control_files,
                **{
                    item["package_path"]: None
                    for item in copy_items
                },
            },
            asset_hashes={
                item["package_path"]: item["sha256"]
                for item in copy_items
            },
        )
        control_files["checksums.sha256"] = checksum_bytes
        _preflight_export_budget(control_files, copy_items)
        result = _publish_handoff(
            output=output,
            package_id=package_id,
            control_files=control_files,
            copy_items=copy_items,
        )
        result.update(_asset_status_counts(delivery_assets))
        return result


def verify_paper_package(
    package_dir: Path,
    *,
    _expected_package_id: str | None = None,
) -> dict[str, Any]:
    """只读取交付目录本身，不访问原科研项目。"""
    root = Path(package_dir)
    files, directories = _preflight_handoff_tree(root)
    required_roots = set(core.HANDOFF_ROOT_FILES)
    if not required_roots <= set(files):
        missing = sorted(required_roots - set(files))
        raise ValueError(f"Paper Package 交付布局缺少文件：{missing}")
    manifest_bytes = read_bounded_regular_file(
        files["manifest.json"]["path"],
        core.MAX_HANDOFF_CONTROL_FILE_SIZE,
        "manifest.json",
        _expected_before=files["manifest.json"]["stat"],
    )
    manifest = parse_json_object_bytes(manifest_bytes, "manifest.json")
    if manifest_bytes != core._canonical_json_line(manifest):
        raise ValueError("manifest.json 必须是规范 JSON")
    delivery_files, delivery_assets = _validate_manifest(
        manifest,
        root.name if _expected_package_id is None else _expected_package_id,
    )
    included_paths = {
        row["package_path"]
        for row in delivery_assets
        if row["status"] == "included"
    }
    expected_files = required_roots | included_paths
    if set(files) != expected_files:
        unknown = sorted(set(files) - expected_files)
        missing = sorted(expected_files - set(files))
        raise ValueError(
            f"Paper Package 交付布局文件不一致；未知={unknown}，缺少={missing}"
        )
    expected_directories = {"assets", "assets/included"}
    for relative in included_paths:
        parts = relative.split("/")
        expected_directories.update(
            "/".join(parts[:index])
            for index in range(1, len(parts))
        )
    if directories != expected_directories:
        raise ValueError("Paper Package 交付目录布局包含未知或缺失目录")

    scientific: dict[str, bytes] = {}
    actual_facts: dict[str, dict[str, Any]] = {}
    for name in ("manifest.json", *core.SCIENTIFIC_FILES):
        content = (
            manifest_bytes
            if name == "manifest.json"
            else read_bounded_regular_file(
                files[name]["path"],
                core.MAX_HANDOFF_CONTROL_FILE_SIZE,
                name,
                _expected_before=files[name]["stat"],
            )
        )
        if name in core.SCIENTIFIC_FILES:
            scientific[name] = content
        actual_facts[name] = {
            "size_bytes": len(content),
            "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
        }
    bound_profile = (
        core._producer_tuple(manifest["producer"])
        == core.BOUND_EVIDENCE_PRODUCER_TUPLE
    )
    delivery_file_map = {
        row["path"]: row
        for row in delivery_files
    }
    for relative in sorted(included_paths, key=lambda value: value.encode("utf-8")):
        if bound_profile:
            expected = delivery_file_map[relative]
            facts, _identity, _resolved = _scan_portable_regular_file(
                files[relative]["path"],
                f"交付资产 {relative}",
                expected_size=expected["size_bytes"],
                expected_sha256=expected["sha256"],
                root=root.resolve(strict=True),
                expected_before=files[relative]["stat"],
            )
        else:
            facts = core._hash_live_regular_file(
                files[relative]["path"],
                f"交付资产 {relative}",
                core.MAX_HANDOFF_SINGLE_ASSET_SIZE,
                _expected_before=files[relative]["stat"],
            )
        actual_facts[relative] = facts

    expected_delivery_paths = [
        *core.SCIENTIFIC_FILES,
        *sorted(included_paths, key=lambda value: value.encode("utf-8")),
    ]
    if [row["path"] for row in delivery_files] != expected_delivery_paths:
        raise ValueError("manifest delivery.files 顺序或文件集合无效")
    for row in delivery_files:
        facts = actual_facts.get(row["path"])
        if facts is None or (
            row["size_bytes"] != facts["size_bytes"]
            or row["sha256"] != facts["sha256"]
        ):
            raise ValueError(f"交付文件大小或 sha256 不一致：{row['path']}")

    checksum_content = read_bounded_regular_file(
        files["checksums.sha256"]["path"],
        core.MAX_HANDOFF_CONTROL_FILE_SIZE,
        "checksums.sha256",
        _expected_before=files["checksums.sha256"]["stat"],
    )
    expected_checksums = _handoff_checksum_bytes(
        {
            name: (
                manifest_bytes
                if name == "manifest.json"
                else scientific[name]
            )
            for name in ("manifest.json", *core.SCIENTIFIC_FILES)
        }
        | {relative: None for relative in included_paths},
        asset_hashes={
            relative: actual_facts[relative]["sha256"]
            for relative in included_paths
        },
    )
    if checksum_content != expected_checksums:
        raise ValueError("Paper Package checksums.sha256 不一致")
    if manifest["content_sha256"] != core._content_sha256(scientific):
        raise ValueError("Paper Package content_sha256 不一致")
    asset_rows = _validate_scientific_intrinsic(
        scientific,
        manifest,
    )
    _validate_delivery_assets(
        delivery_assets,
        asset_rows,
        actual_facts,
        asset_mode=manifest["asset_mode"],
        readiness=manifest["readiness"],
    )
    if bound_profile:
        _validate_bound_scientific_portability(
            manifest,
            scientific,
        )
        _validate_reverification_bindings_from_handoff(
            root,
            manifest,
            scientific["experiments.json"],
        )
    _validate_actual_budget(actual_facts, checksum_content)
    _verify_final_inventory(root, files, directories)
    counts = _asset_status_counts(delivery_assets)
    return {
        "schema": core.PAPER_PACKAGE_VERIFY_RESULT_SCHEMA,
        "status": "pass",
        "package_id": manifest["package_id"],
        "asset_mode": manifest["asset_mode"],
        "readiness": manifest["readiness"],
        "content_sha256": manifest["content_sha256"],
        **counts,
        "integrity_scope": (
            "内容完整性校验；不提供作者身份或来源真实性签名"
        ),
    }


def _read_internal_package(
    control: Path,
    package_id: str,
) -> dict[str, bytes]:
    directory = control / "paper-packages" / "packages" / package_id
    core._require_regular_directory(directory, f"Paper Package {package_id}")
    entries = {item.name: item for item in directory.iterdir()}
    if set(entries) != set(core.PACKAGE_FILES):
        raise ValueError(f"内部 Paper Package 文件集合无效：{package_id}")
    contents: dict[str, bytes] = {}
    total = 0
    for name in core.PACKAGE_FILES:
        content = read_bounded_regular_file(
            entries[name],
            core.MAX_PACKAGE_FILE_SIZE,
            f"{package_id}/{name}",
        )
        total += len(content)
        if total > core.MAX_PACKAGE_TOTAL_SIZE:
            raise ValueError(f"内部 Paper Package 总大小超过限制：{package_id}")
        contents[name] = content
    core._validate_checksum_bytes(contents)
    return contents


def _bound_scientific_files(
    *,
    control: Path,
    internal: dict[str, bytes],
    tasks: dict[str, dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    evidence_bindings: dict[str, dict[str, Any]],
    source_run_refs: list[str],
) -> dict[str, bytes]:
    """把内部科学内容投影成 1.5 接收端所需的可携带证据快照。"""
    experiments = parse_json_object_bytes(
        internal["experiments.json"],
        "experiments.json",
    )
    delivered_ids: list[str] = []
    for experiment in experiments["items"]:
        task_id = experiment["task_id"]
        if task_id not in tasks:
            raise ValueError(f"1.5 Experiment Task 不存在：{task_id}")
        conclusion = experiment["task"].get("conclusion")
        if isinstance(conclusion, dict):
            experiment["task"]["conclusion"] = {
                "run_id": conclusion["run_id"],
                "comparison": deepcopy(conclusion["comparison"]),
            }
        augmented: list[dict[str, Any]] = []
        for sealed_run in experiment["runs"]:
            run_id = sealed_run["run_id"]
            live = runs.get(run_id)
            if live is None or live["task_id"] != task_id:
                raise ValueError(f"1.5 Experiment Run 不存在：{run_id}")
            snapshot = live["execution_snapshot"]
            manifest_path = control / snapshot["manifest_path"]
            snapshot_manifest = read_bounded_json_object(
                manifest_path,
                32 * 1024 * 1024,
                f"{run_id} execution snapshot manifest",
            )
            augmented.append(
                {
                    **deepcopy(sealed_run),
                    "execution_snapshot": deepcopy(snapshot),
                    "execution_snapshot_manifest": snapshot_manifest,
                    "output_seal": _portable_output_seal(live),
                    "evidence_binding": deepcopy(
                        evidence_bindings[run_id]
                    ),
                }
            )
            delivered_ids.append(run_id)
        experiment["runs"] = augmented
    if sorted(set(delivered_ids)) != source_run_refs:
        raise ValueError(
            "1.5 experiments.json 必须精确覆盖 source_run_refs"
        )
    scientific = {
        name: (
            core._canonical_json_line(experiments)
            if name == "experiments.json"
            else internal[name]
        )
        for name in core.SCIENTIFIC_FILES
    }
    source_rows = core._parse_jsonl(
        scientific["sources.jsonl"],
        "sources.jsonl",
        "source_ref",
        allow_same_primary=True,
    )
    for row in source_rows:
        if _contains_local_absolute_path(row["locator"]):
            row["locator"] = f"source://{row['source_ref']}"
    scientific["sources.jsonl"] = core._canonical_jsonl(
        source_rows,
        "source_ref",
    )
    asset_rows = core._parse_jsonl(
        scientific["assets.jsonl"],
        "assets.jsonl",
        "asset_id",
    )
    for row in asset_rows:
        source_ref = row["source_object_ref"]
        if (
            core.SOURCE_ID.fullmatch(source_ref)
            and _contains_local_absolute_path(row["path"])
        ):
            row["path"] = f"sources/{source_ref}/content"
    scientific["assets.jsonl"] = core._canonical_jsonl(
        asset_rows,
        "asset_id",
    )
    return scientific


def _portable_output_seal(run: dict[str, Any]) -> dict[str, Any]:
    seal = run["output_seal"]
    root = seal["root"]["relative_path"]
    sealed_at = datetime.fromisoformat(
        seal["sealed_at"].replace("Z", "+00:00")
    )
    finished_at = datetime.fromisoformat(
        run["execution"]["finished_at"].replace("Z", "+00:00")
    )
    # 旧封存记录按秒写时间，而 Run 完成时间带微秒。可携带投影把
    # 封存时间下界提升到完成时刻，避免精度截断造成虚假的时间倒序。
    portable_sealed_at = max(sealed_at, finished_at).isoformat()

    def portable(record: dict[str, Any]) -> dict[str, Any]:
        output_prefix = ".cv-workflow-output/"
        if not record["path"].startswith(output_prefix):
            raise ValueError("Run output seal 文件不在固定输出目录")
        return {
            "source_path": record["path"],
            "sealed_path": (
                f"{root}/{record['path'].removeprefix(output_prefix)}"
            ),
            "size_bytes": record["size_bytes"],
            "sha256": record["sha256"],
        }

    payload: dict[str, Any] = {
        "schema": core.BOUND_OUTPUT_SEAL_SCHEMA,
        "source_root": deepcopy(seal["source"]),
        "authoritative_root": {
            "kind": "project_run_output_seal",
            "relative_path": root,
        },
        "execution_snapshot_sha256": seal[
            "execution_snapshot_sha256"
        ],
        "frozen_digest": seal["frozen_digest"],
        "finished_snapshot_sha256": seal[
            "finished_snapshot_sha256"
        ],
        "raw_log": portable(seal["raw_log"]),
        "artifacts": [
            portable(record)
            for record in seal["artifacts"]
        ],
        "total_files": seal["total_files"],
        "total_bytes": seal["total_bytes"],
        "comparability": deepcopy(seal["comparability"]),
        "sealed_at": portable_sealed_at,
    }
    payload["seal_sha256"] = core._canonical_object_sha256(payload)
    return payload


def _package_payload_sha256(
    scientific: dict[str, bytes],
    copy_items: list[dict[str, Any]],
) -> str:
    digest = hashlib.sha256()
    digest.update(b"cv-research-handoff/package-payload/v1\0")
    for name in core.SCIENTIFIC_FILES:
        content = scientific[name]
        _update_payload_header(
            digest,
            name,
            len(content),
        )
        digest.update(content)
    for item in sorted(
        copy_items,
        key=lambda value: value["package_path"].encode("utf-8"),
    ):
        _update_payload_from_file(digest, item)
    return f"sha256:{digest.hexdigest()}"


def _update_payload_header(
    digest: Any,
    relative: str,
    size: int,
) -> None:
    path_bytes = relative.encode("utf-8")
    digest.update(len(path_bytes).to_bytes(8, "big"))
    digest.update(path_bytes)
    digest.update(size.to_bytes(8, "big"))


def _update_payload_from_file(
    digest: Any,
    item: dict[str, Any],
) -> None:
    path, before, resolved = core._inspect_live_regular_file(
        item["source"],
        f"payload asset {item['package_path']}",
        core.MAX_HANDOFF_SINGLE_ASSET_SIZE,
        root=item["source_root"],
    )
    if before.st_size != item["size_bytes"]:
        raise ValueError("payload Asset 大小与封存身份不一致")
    _update_payload_header(
        digest,
        item["package_path"],
        item["size_bytes"],
    )
    file_digest = hashlib.sha256()
    observed = 0
    try:
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if not os.path.samestat(before, opened):
                raise ValueError("payload Asset 在读取前身份变化")
            while True:
                chunk = source.read(core.HASH_CHUNK_BYTES)
                if not chunk:
                    break
                observed += len(chunk)
                if observed > item["size_bytes"]:
                    raise ValueError("payload Asset 在读取期间变大")
                file_digest.update(chunk)
                digest.update(chunk)
            after = os.fstat(source.fileno())
        current = path.lstat()
    except ValueError:
        raise
    except OSError as error:
        raise ValueError("payload Asset 无法读取") from error
    if (
        observed != item["size_bytes"]
        or f"sha256:{file_digest.hexdigest()}" != item["sha256"]
        or not os.path.samestat(opened, after)
        or not os.path.samestat(before, current)
        or current.st_size != before.st_size
        or current.st_mtime_ns != before.st_mtime_ns
        or path.resolve(strict=True) != resolved
    ):
        raise ValueError("payload Asset 在读取期间发生变化")


def _validate_bound_portability(
    manifest: dict[str, Any],
    scientific: dict[str, bytes],
    copy_items: list[dict[str, Any]],
) -> None:
    _validate_bound_scientific_portability(manifest, scientific)
    for item in copy_items:
        facts, identity, resolved = _scan_portable_regular_file(
            item["source"],
            f"portable asset {item['package_path']}",
            expected_size=item["size_bytes"],
            expected_sha256=item["sha256"],
            root=item["source_root"],
        )
        item["_portability_scan"] = {
            "stat": identity,
            "resolved": resolved,
            "size_bytes": facts["size_bytes"],
            "sha256": facts["sha256"],
        }


def _validate_bound_scientific_portability(
    manifest: dict[str, Any],
    scientific: dict[str, bytes],
) -> None:
    values: dict[str, Any] = {
        "manifest": manifest,
        "study": parse_json_object_bytes(
            scientific["study.json"],
            "study.json",
        ),
        "experiments": parse_json_object_bytes(
            scientific["experiments.json"],
            "experiments.json",
        ),
        "visuals": parse_json_object_bytes(
            scientific["visuals.json"],
            "visuals.json",
        ),
        "claims": core._parse_jsonl(
            scientific["claims.jsonl"],
            "claims.jsonl",
            "claim_id",
        ),
        "sources": core._parse_jsonl(
            scientific["sources.jsonl"],
            "sources.jsonl",
            "source_ref",
            allow_same_primary=True,
        ),
        "assets": core._parse_jsonl(
            scientific["assets.jsonl"],
            "assets.jsonl",
            "asset_id",
        ),
    }
    _reject_local_absolute_paths(values)


def _reject_local_absolute_paths(
    value: object,
    label: str = "package",
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_local_absolute_paths(key, f"{label}.<key>")
            _reject_local_absolute_paths(item, f"{label}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_local_absolute_paths(item, f"{label}[{index}]")
        return
    if not isinstance(value, str):
        return
    if _contains_local_absolute_path(value):
        raise ValueError(f"1.5 包含本机绝对路径：{label}")


def _contains_local_absolute_path(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value)
    slash_normalized = normalized.replace("\\/", "/")
    lowered = normalized.casefold()
    return bool(
        "file://" in lowered
        or lowered.startswith(("file:", "~/", "~\\"))
        or _WINDOWS_ABSOLUTE_IN_TEXT.search(normalized)
        or _WINDOWS_CURRENT_DRIVE_ROOT_IN_TEXT.search(normalized)
        or _FORWARD_SLASH_UNC_IN_TEXT.search(slash_normalized)
        or _POSIX_ABSOLUTE_IN_TEXT.search(slash_normalized)
    )


def _contains_binary_local_absolute_path(value: str) -> bool:
    """对已证实的非 UTF 字节只识别高置信、连续可打印路径形状。"""
    normalized = unicodedata.normalize("NFKC", value)
    slash_normalized = normalized.replace("\\/", "/")
    lowered = normalized.casefold()
    return bool(
        "file://" in lowered
        or lowered.startswith("file:")
        or _BINARY_HOME_IN_TEXT.search(normalized)
        or _BINARY_WINDOWS_DRIVE_IN_TEXT.search(normalized)
        or _BINARY_WINDOWS_ROOTED_IN_TEXT.search(normalized)
        or _BINARY_WINDOWS_UNC_IN_TEXT.search(normalized)
        or _BINARY_FORWARD_SLASH_UNC_IN_TEXT.search(slash_normalized)
        or _BINARY_POSIX_ABSOLUTE_IN_TEXT.search(slash_normalized)
    )


class _PortablePathScanner:
    """流式识别 UTF 文本路径，并对普通二进制只检查高置信路径标记。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self._prefix = b""
        self._decoder: Any | None = None
        self._validator: Any | None = None
        self._explicit_text = False
        self._valid_utf8 = True
        self._broad_path_seen = False
        self._binary_path_seen = False
        self._text_tail = ""

    def feed(self, chunk: bytes) -> None:
        if self._decoder is None:
            self._prefix += chunk
            if len(self._prefix) < 4:
                return
            self._initialize_decoder()
            chunk = self._prefix
            self._prefix = b""
        self._decode(chunk, final=False)

    def finish(self) -> None:
        if self._decoder is None:
            self._initialize_decoder()
            chunk = self._prefix
            self._prefix = b""
            self._decode(chunk, final=False)
        self._decode(b"", final=True)
        if (
            (
                self._broad_path_seen
                and (self._explicit_text or self._valid_utf8)
            )
            or (
                not self._explicit_text
                and not self._valid_utf8
                and self._binary_path_seen
            )
        ):
            raise ValueError(
                f"1.5 包含本机绝对路径：{self.label}"
            )

    def _initialize_decoder(self) -> None:
        encoding = "utf-8"
        for bom, candidate in _TEXT_BOMS:
            if self._prefix.startswith(bom):
                encoding = candidate
                self._explicit_text = True
                break
        self._decoder = codecs.getincrementaldecoder(encoding)(
            errors="replace"
        )
        if not self._explicit_text:
            self._validator = codecs.getincrementaldecoder("utf-8")(
                errors="strict"
            )

    def _decode(self, chunk: bytes, *, final: bool) -> None:
        if self._decoder is None:
            raise AssertionError("portable scanner decoder is not initialized")
        decoded = self._decoder.decode(chunk, final=final)
        if self._validator is not None and self._valid_utf8:
            try:
                self._validator.decode(chunk, final=final)
            except UnicodeDecodeError:
                self._valid_utf8 = False
        window = self._text_tail + decoded
        if _contains_local_absolute_path(window):
            self._broad_path_seen = True
        if _contains_binary_local_absolute_path(window):
            self._binary_path_seen = True
        self._text_tail = window[-256:]


def _scan_portable_regular_file(
    source_path: Path,
    label: str,
    *,
    expected_size: int,
    expected_sha256: str,
    root: Path | None,
    expected_before: os.stat_result | None = None,
    expected_resolved: Path | None = None,
) -> tuple[dict[str, Any], os.stat_result, Path]:
    """一次稳定读取同时完成便携性、大小和 SHA-256 核验。"""
    path, before, resolved = core._inspect_live_regular_file(
        source_path,
        label,
        core.MAX_HANDOFF_SINGLE_ASSET_SIZE,
        root=root,
    )
    if (
        expected_before is not None
        and (
            not os.path.samestat(before, expected_before)
            or before.st_size != expected_before.st_size
            or before.st_mtime_ns != expected_before.st_mtime_ns
            or before.st_ctime_ns != expected_before.st_ctime_ns
            or (
                expected_resolved is not None
                and resolved != expected_resolved
            )
        )
    ):
        raise ValueError(f"{label} 在便携性扫描前身份变化")
    if before.st_size != expected_size:
        raise ValueError(f"{label} 大小与 manifest/Asset 记录不一致")
    scanner = _PortablePathScanner(label)
    digest = hashlib.sha256()
    observed = 0
    try:
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or not os.path.samestat(before, opened)
            ):
                raise ValueError(f"{label} 在便携性扫描前身份变化")
            while True:
                chunk = source.read(core.HASH_CHUNK_BYTES)
                if not chunk:
                    break
                observed += len(chunk)
                if observed > expected_size:
                    raise ValueError(f"{label} 在便携性扫描期间变大")
                digest.update(chunk)
                scanner.feed(chunk)
            scanner.finish()
            after = os.fstat(source.fileno())
        current = path.lstat()
        current_resolved = path.resolve(strict=True)
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(f"{label} 无法读取") from error
    facts = {
        "size_bytes": observed,
        "sha256": f"sha256:{digest.hexdigest()}",
    }
    if (
        facts["size_bytes"] != expected_size
        or facts["sha256"] != expected_sha256
        or not os.path.samestat(before, after)
        or not os.path.samestat(before, current)
        or current.st_size != before.st_size
        or current.st_mtime_ns != before.st_mtime_ns
        or current.st_ctime_ns != before.st_ctime_ns
        or current_resolved != resolved
    ):
        raise ValueError(
            f"{label} 在便携性扫描期间变化或与 manifest/Asset 记录不一致"
        )
    return facts, before, resolved


def _plan_assets(
    *,
    root: Path,
    package: dict[str, Any],
    mode: str,
    asset_rows: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    catalog: dict[str, dict[str, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    delivery_assets = _derive_delivery_assets(asset_rows, mode)
    _require_ready_assets_included(
        package["readiness"],
        asset_rows,
        delivery_assets,
    )
    copy_items: list[dict[str, Any]] = []
    copied_paths: set[str] = set()
    for row, delivery in zip(asset_rows, delivery_assets, strict=True):
        if delivery["status"] == "included":
            digest = row["sha256"]
            package_path = delivery["package_path"]
            if package_path not in copied_paths:
                target, _expected_digest, _reference_uri = core._resolve_asset_target(
                    root,
                    row,
                    runs=runs,
                    sources=catalog["sources"],
                    modules=catalog["modules"],
                )
                copy_items.append({
                    "source": target,
                    "package_path": package_path,
                    "size_bytes": row["size_bytes"],
                    "sha256": digest,
                    "asset_ids": [row["asset_id"]],
                    "source_root": (
                        root.resolve(strict=True)
                        if core.RUN_ID.fullmatch(row["source_object_ref"])
                        or core.MODULE_ID.fullmatch(row["source_object_ref"])
                        else None
                    ),
                })
                copied_paths.add(package_path)
            else:
                copy_item = next(
                    item
                    for item in copy_items
                    if item["package_path"] == package_path
                )
                copy_item["asset_ids"].append(row["asset_id"])
    return delivery_assets, copy_items


def _derive_delivery_assets(
    asset_rows: list[dict[str, Any]],
    mode: str,
) -> list[dict[str, Any]]:
    """只依据封存声明推导交付决策，导出与独立验证共用。"""
    if mode not in {"hybrid", "full"}:
        raise ValueError("asset_mode 只能是 hybrid 或 full")
    if len(asset_rows) > core.MAX_HANDOFF_ASSET_COUNT:
        raise ValueError("Paper Package Asset 数量超过 2048")
    planned: list[dict[str, Any]] = []
    digest_groups: dict[str, list[dict[str, Any]]] = {}
    for row in asset_rows:
        status, reason = _base_asset_decision(row, mode)
        item = {"row": row, "status": status, "reason": reason}
        planned.append(item)
        digest = row.get("sha256")
        if isinstance(digest, str) and _SHA256.fullmatch(digest):
            digest_groups.setdefault(digest, []).append(item)

    rank = {"included": 0, "referenced": 1, "omitted": 2}
    for group in digest_groups.values():
        strictest = max(group, key=lambda item: rank[item["status"]])
        for item in group:
            if rank[item["status"]] < rank[strictest["status"]]:
                item["status"] = strictest["status"]
                item["reason"] = (
                    "shared_content_restriction:"
                    f"{strictest['reason'] or strictest['status']}"
                )

    package_paths: dict[str, str] = {}
    delivery: list[dict[str, Any]] = []
    for item in planned:
        row = item["row"]
        package_path: str | None = None
        if item["status"] == "included":
            digest = row["sha256"]
            package_path = package_paths.get(digest)
            if package_path is None:
                basename = Path(row["path"]).name
                package_path = _normalize_handoff_relative_path(
                    "assets/included/"
                    f"{digest.removeprefix('sha256:')}/{basename}",
                    f"{row['asset_id']}.package_path",
                )
                package_paths[digest] = package_path
        delivery.append(
            {
                "asset_id": row["asset_id"],
                "status": item["status"],
                "package_path": package_path,
                "reason": (
                    None if item["status"] == "included" else item["reason"]
                ),
            }
        )
    return delivery


def _require_ready_assets_included(
    readiness: str,
    asset_rows: list[dict[str, Any]],
    delivery_rows: list[dict[str, Any]],
) -> None:
    if readiness != "paper_ready":
        return
    blocked = [
        asset["asset_id"]
        for asset, delivery in zip(asset_rows, delivery_rows, strict=True)
        if asset["required_for_writing"]
        and delivery["status"] != "included"
    ]
    if blocked:
        raise ValueError(
            "paper_ready 包的必需资产必须全部实际 included："
            + ",".join(blocked)
        )


def _base_asset_decision(
    row: dict[str, Any],
    mode: str,
) -> tuple[str, str | None]:
    availability = row["availability"]
    if availability in {"missing", "restricted"}:
        detail = row["omission_reason"] or availability
        return "omitted", f"availability_{availability}:{detail}"
    if availability == "external":
        return "referenced", (
            f"availability_external:{row['omission_reason'] or 'external reference'}"
        )
    if row["privacy_classification"] in {"sensitive", "restricted"}:
        return "omitted", (
            f"privacy_blocked:{row['privacy_classification']}"
        )
    if not row["copy_allowed"]:
        return "referenced", "copy_not_allowed"
    if row["license"].casefold() == "unknown":
        return "referenced", "license_unknown"
    if mode == "hybrid" and not row["required_for_writing"]:
        return "referenced", "hybrid_optional_reference"
    return "included", None


def _scientific_delivery_files(
    internal: dict[str, bytes],
) -> list[dict[str, Any]]:
    return [
        {
            "path": name,
            "size_bytes": len(internal[name]),
            "sha256": (
                f"sha256:{hashlib.sha256(internal[name]).hexdigest()}"
            ),
        }
        for name in core.SCIENTIFIC_FILES
    ]


def _preflight_export_budget(
    control_files: dict[str, bytes],
    copy_items: list[dict[str, Any]],
) -> None:
    if any(
        len(content) > core.MAX_HANDOFF_CONTROL_FILE_SIZE
        for content in control_files.values()
    ):
        raise ValueError("Paper Package 交付控制文件超过 8 MiB")
    control_total = sum(map(len, control_files.values()))
    if control_total > core.MAX_HANDOFF_CONTROL_TOTAL_SIZE:
        raise ValueError("Paper Package 交付控制文件合计超过 32 MiB")
    asset_total = 0
    for item in copy_items:
        size = item["size_bytes"]
        if type(size) is not int or not 0 <= size <= core.MAX_HANDOFF_SINGLE_ASSET_SIZE:
            raise ValueError("Paper Package 单个交付资产超过 512 MiB")
        asset_total += size
    if asset_total > core.MAX_HANDOFF_ASSET_TOTAL_SIZE:
        raise ValueError("Paper Package 交付资产合计超过 512 MiB")
    if control_total + asset_total > core.MAX_HANDOFF_PACKAGE_TOTAL_SIZE:
        raise ValueError("Paper Package 交付目录总大小超过 544 MiB")
    if len(control_files) + len(copy_items) > core.MAX_HANDOFF_FILE_COUNT:
        raise ValueError("Paper Package 交付文件数量超过限制")


def _publish_handoff(
    *,
    output: Path,
    package_id: str,
    control_files: dict[str, bytes],
    copy_items: list[dict[str, Any]],
) -> dict[str, Any]:
    staging = _new_handoff_staging_path(output.parent)
    mkdir_attempted = False
    identity: os.stat_result | None = None
    published = False
    try:
        mkdir_attempted = True
        staging.mkdir()
        identity = staging.lstat()
        (staging / "assets" / "included").mkdir(parents=True)
        for name, content in control_files.items():
            _write_private_staging_file(staging / name, content)
        for item in copy_items:
            destination = staging / item["package_path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            portability_scan = item.get("_portability_scan")
            _copy_regular_file_same_handle(
                item["source"],
                destination,
                expected_size=item["size_bytes"],
                expected_sha256=item["sha256"],
                source_root=item["source_root"],
                expected_source_identity=(
                    portability_scan["stat"]
                    if isinstance(portability_scan, dict)
                    else None
                ),
                expected_source_resolved=(
                    portability_scan["resolved"]
                    if isinstance(portability_scan, dict)
                    else None
                ),
            )
        verified = verify_paper_package(
            staging,
            _expected_package_id=package_id,
        )
        if verified["package_id"] != package_id:
            raise ValueError("导出 staging 的 package_id 不一致")
        if os.path.lexists(output):
            raise FileExistsError(
                f"Paper Package 导出目标已存在，拒绝覆盖：{output}"
            )
        try:
            core._publish_directory_no_replace(staging, output)
            published = True
        except BaseException:
            recovered = _recover_committed_handoff(
                staging=staging,
                output=output,
                expected_identity=identity,
                package_id=package_id,
            )
            if recovered is None:
                raise
            verified = recovered
            published = True
        return _export_result(output, package_id, verified)
    except BaseException as error:
        if not published:
            if identity is None:
                if mkdir_attempted and not isinstance(error, FileExistsError):
                    _cleanup_unidentified_empty_staging(staging)
            else:
                _cleanup_owned_staging(staging, identity)
        raise


def _new_handoff_staging_path(parent: Path) -> Path:
    """分配与 `PKG-xxxx` 等长的私有暂存目录，避免撑爆 Windows 最终路径。"""
    for _attempt in range(100):
        token = base64.urlsafe_b64encode(uuid.uuid4().bytes).decode("ascii")[:7]
        candidate = parent / f".{token}"
        if not os.path.lexists(candidate):
            return candidate
    raise FileExistsError("无法分配唯一的 Paper Package staging 目录")


def _is_handoff_staging_name(name: str) -> bool:
    return _HANDOFF_STAGING_NAME.fullmatch(name) is not None


def _write_private_staging_file(path: Path, content: bytes) -> None:
    """私有目录会整体原子发布，内部文件无需再创建更长的事务临时路径。"""
    try:
        with path.open("xb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
    except OSError as error:
        raise ValueError(f"Paper Package staging 文件写入失败：{path.name}") from error


def _export_result(
    output: Path,
    package_id: str,
    verified: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": core.PAPER_PACKAGE_EXPORT_RESULT_SCHEMA,
        "status": "exported",
        "package_id": package_id,
        "package_dir": str(output),
        "asset_mode": verified["asset_mode"],
        "readiness": verified["readiness"],
        "content_sha256": verified["content_sha256"],
        "integrity_scope": verified["integrity_scope"],
    }


def _recover_committed_handoff(
    *,
    staging: Path,
    output: Path,
    expected_identity: os.stat_result,
    package_id: str,
) -> dict[str, Any] | None:
    """rename 抛异常后，只把同一目录身份且完整通过核验的目标视为已提交。"""
    if os.path.lexists(staging) or not os.path.lexists(output):
        return None
    try:
        current = output.lstat()
    except OSError:
        return None
    if (
        stat.S_ISLNK(current.st_mode)
        or is_link_or_reparse(output)
        or not stat.S_ISDIR(current.st_mode)
        or not os.path.samestat(expected_identity, current)
    ):
        return None
    try:
        return verify_paper_package(
            output,
            _expected_package_id=package_id,
        )
    except (OSError, ValueError) as error:
        raise RuntimeError(
            "Paper Package rename 已提交但最终目录核验失败，已保留现场"
        ) from error


def _cleanup_unidentified_empty_staging(staging: Path) -> None:
    """未取到身份时只删除空的普通随机目录，绝不递归处理未知内容。"""
    if not os.path.lexists(staging):
        return
    current = staging.lstat()
    if (
        not _is_handoff_staging_name(staging.name)
        or stat.S_ISLNK(current.st_mode)
        or is_link_or_reparse(staging)
        or not stat.S_ISDIR(current.st_mode)
    ):
        raise RuntimeError(
            f"Paper Package staging 身份未取得，已保留未知现场：{staging}"
        )
    try:
        staging.rmdir()
    except OSError as error:
        raise RuntimeError(
            f"Paper Package staging 身份未取得且目录不为空，已保留现场：{staging}"
        ) from error


def _copy_regular_file_same_handle(
    source_path: Path,
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    source_root: Path | None,
    expected_source_identity: os.stat_result | None = None,
    expected_source_resolved: Path | None = None,
) -> None:
    source_path, before, resolved = core._inspect_live_regular_file(
        source_path,
        "交付资产源文件",
        core.MAX_HANDOFF_SINGLE_ASSET_SIZE,
        root=source_root,
    )
    if expected_source_identity is not None and (
        not os.path.samestat(before, expected_source_identity)
        or before.st_size != expected_source_identity.st_size
        or before.st_mtime_ns != expected_source_identity.st_mtime_ns
        or before.st_ctime_ns != expected_source_identity.st_ctime_ns
        or (
            expected_source_resolved is not None
            and resolved != expected_source_resolved
        )
    ):
        raise ValueError("交付资产源文件在便携性扫描后身份变化")
    if before.st_size != expected_size:
        raise ValueError("交付资产源文件大小与内部包不一致")
    digest = hashlib.sha256()
    copied = 0
    try:
        with source_path.open("rb") as source, destination.open("xb") as target:
            opened = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or not os.path.samestat(before, opened)
                or opened.st_size != expected_size
            ):
                raise ValueError("交付资产源文件在复制前身份变化")
            while True:
                chunk = source.read(core.HASH_CHUNK_BYTES)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > expected_size:
                    raise ValueError("交付资产源文件在复制期间变大")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
            after = os.fstat(source.fileno())
        current = source_path.lstat()
        current_resolved = source_path.resolve(strict=True)
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(f"交付资产复制失败：{source_path}") from error
    actual_sha256 = f"sha256:{digest.hexdigest()}"
    if (
        copied != expected_size
        or actual_sha256 != expected_sha256
        or not os.path.samestat(opened, after)
        or not os.path.samestat(before, current)
        or current.st_size != before.st_size
        or current.st_mtime_ns != before.st_mtime_ns
        or current_resolved != resolved
    ):
        raise ValueError("交付资产源文件在复制期间发生变化或哈希不一致")


def _cleanup_owned_staging(
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
            f"Paper Package staging 身份变化，已保留现场并拒绝清理：{staging}"
        )
    shutil.rmtree(staging)


def _preflight_handoff_tree(
    root: Path,
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    core._require_regular_directory(root, "Paper Package 交付目录")
    files: dict[str, dict[str, Any]] = {}
    directories: set[str] = set()
    collision_keys: dict[str, str] = {}
    entry_count = 0

    def visit(directory: Path, prefix: str) -> None:
        nonlocal entry_count
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            raise ValueError(f"无法枚举 Paper Package 交付目录：{directory}") from error
        for entry in entries:
            entry_count += 1
            if entry_count > core.MAX_HANDOFF_ENTRY_COUNT:
                raise ValueError("Paper Package 交付目录条目数量超过限制")
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            relative = _normalize_handoff_relative_path(
                relative,
                "交付目录相对路径",
            )
            key = _windows_path_key(relative)
            previous = collision_keys.get(key)
            if previous is not None and previous != relative:
                raise ValueError(
                    f"交付路径发生大小写或 Unicode 规范冲突：{previous}/{relative}"
                )
            collision_keys[key] = relative
            path = Path(entry.path)
            try:
                metadata = path.lstat()
            except OSError as error:
                raise ValueError(f"无法读取交付条目身份：{relative}") from error
            if (
                entry.is_symlink()
                or stat.S_ISLNK(metadata.st_mode)
                or is_link_or_reparse(path)
            ):
                raise ValueError(f"交付目录禁止 symlink/reparse：{relative}")
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(relative)
                visit(path, relative)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"交付目录只允许普通文件和目录：{relative}")
            # Windows may report 0 for a just-created file on some volumes.
            # Values above 1 are a positive hardlink signal and are rejected.
            if getattr(metadata, "st_nlink", 1) > 1:
                raise ValueError(
                    "交付目录禁止 hardlink："
                    f"{relative} (st_nlink={getattr(metadata, 'st_nlink', None)})"
                )
            files[relative] = {"path": path, "stat": metadata}
            if len(files) > core.MAX_HANDOFF_FILE_COUNT:
                raise ValueError("Paper Package 交付文件数量超过限制")

    visit(root, "")
    control_total = 0
    asset_total = 0
    for relative, item in files.items():
        size = item["stat"].st_size
        if relative.startswith("assets/included/"):
            if size > core.MAX_HANDOFF_SINGLE_ASSET_SIZE:
                raise ValueError("Paper Package 单个交付资产超过 512 MiB")
            asset_total += size
        else:
            if size > core.MAX_HANDOFF_CONTROL_FILE_SIZE:
                raise ValueError("Paper Package 交付控制文件超过 8 MiB")
            control_total += size
    if control_total > core.MAX_HANDOFF_CONTROL_TOTAL_SIZE:
        raise ValueError("Paper Package 交付控制文件合计超过 32 MiB")
    if asset_total > core.MAX_HANDOFF_ASSET_TOTAL_SIZE:
        raise ValueError("Paper Package 交付资产合计超过 512 MiB")
    if control_total + asset_total > core.MAX_HANDOFF_PACKAGE_TOTAL_SIZE:
        raise ValueError("Paper Package 交付目录总大小超过 544 MiB")
    return files, directories


def _validate_manifest(
    manifest: dict[str, Any],
    directory_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    producer_candidate = manifest.get("producer")
    producer_identity = (
        core._producer_tuple(producer_candidate)
        if isinstance(producer_candidate, dict)
        else (None, None, None)
    )
    bound_profile = (
        producer_identity == core.BOUND_EVIDENCE_PRODUCER_TUPLE
    )
    expected_fields = (
        _BOUND_MANIFEST_FIELDS if bound_profile else _MANIFEST_FIELDS
    )
    if set(manifest) != expected_fields:
        raise ValueError("Paper Package manifest 顶层字段无效")
    package_id = manifest.get("package_id")
    project_id = manifest.get("project_id")
    try:
        canonical_project_id = (
            str(uuid.UUID(project_id))
            if isinstance(project_id, str)
            else None
        )
    except (ValueError, AttributeError):
        canonical_project_id = None
    if (
        not isinstance(project_id, str)
        or canonical_project_id != project_id
    ):
        raise ValueError(
            "Paper Package manifest.project_id 必须是规范小写 UUID"
        )
    if (
        manifest.get("schema") != core.PAPER_PACKAGE_HANDOFF_SCHEMA
        or not isinstance(package_id, str)
        or core.PACKAGE_ID.fullmatch(package_id) is None
        or package_id != directory_name
        or int(package_id[4:]) < 1
        or type(manifest.get("package_revision")) is not int
        or manifest.get("package_revision") != 1
        or not isinstance(manifest.get("project_name"), str)
        or not manifest["project_name"]
        or not isinstance(manifest.get("brief_id"), str)
        or core.BRIEF_ID.fullmatch(manifest["brief_id"]) is None
        or not core._is_rfc3339(manifest.get("created_at"))
        or manifest.get("asset_mode") not in {"hybrid", "full"}
        or manifest.get("readiness") not in {"paper_ready", "incomplete"}
        or not isinstance(manifest.get("content_sha256"), str)
        or _SHA256.fullmatch(manifest["content_sha256"]) is None
    ):
        raise ValueError("Paper Package manifest 身份字段无效")
    producer = core._exact_object(
        manifest.get("producer"),
        {"skill_id", "release_version", "system_version"},
        "manifest.producer",
    )
    producer_tuple = (
        core._text(producer.get("skill_id"), "producer.skill_id"),
        core._text(producer.get("release_version"), "producer.release_version"),
        core._text(producer.get("system_version"), "producer.system_version"),
    )
    if producer_tuple not in core.PAPER_PACKAGE_PARSE_COMPATIBLE_PRODUCERS:
        raise ValueError("Paper Package producer 不在解析兼容白名单")
    missing = manifest.get("missing_requirements")
    if (
        not isinstance(missing, list)
        or not all(isinstance(item, str) and item for item in missing)
        or missing != sorted(set(missing))
        or (manifest["readiness"] == "paper_ready") != (not missing)
    ):
        raise ValueError("Paper Package readiness 与缺失项不一致")
    if (
        core._reference_list(
            manifest.get("source_run_refs"),
            core.RUN_ID,
            "manifest.source_run_refs",
            minimum=0,
        )
        != manifest["source_run_refs"]
    ):
        raise ValueError("Paper Package source_run_refs 无效")
    if bound_profile:
        if manifest["readiness"] != "paper_ready" or missing:
            raise ValueError("1.5 正式证据包必须为 paper_ready")
        _validate_reverification_shape(
            manifest["reverification"],
            producer=producer,
            source_run_refs=manifest["source_run_refs"],
        )
    supersedes = manifest.get("supersedes_package_id")
    if supersedes is not None and (
        not isinstance(supersedes, str)
        or core.PACKAGE_ID.fullmatch(supersedes) is None
        or int(supersedes[4:]) < 1
        or int(supersedes[4:]) >= int(package_id[4:])
    ):
        raise ValueError("Paper Package supersedes_package_id 无效")
    delivery = core._exact_object(
        manifest.get("delivery"),
        _DELIVERY_FIELDS,
        "manifest.delivery",
    )
    if (
        type(delivery.get("layout_revision")) is not int
        or delivery["layout_revision"] != 1
    ):
        raise ValueError("Paper Package delivery.layout_revision 无效")
    file_rows = core._selection_list(
        delivery.get("files"),
        "manifest.delivery.files",
        minimum=len(core.SCIENTIFIC_FILES),
    )
    if len(file_rows) > core.MAX_HANDOFF_FILE_COUNT - 2:
        raise ValueError("Paper Package delivery.files 数量超过限制")
    normalized_files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for row in file_rows:
        item = core._exact_object(row, _DELIVERY_FILE_FIELDS, "delivery.file")
        relative = _normalize_handoff_relative_path(
            item.get("path"),
            "delivery.file.path",
        )
        key = _windows_path_key(relative)
        if key in seen_paths:
            raise ValueError("delivery.files 包含重复或冲突路径")
        seen_paths.add(key)
        size = item.get("size_bytes")
        digest = item.get("sha256")
        if (
            type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise ValueError(f"delivery.files 身份无效：{relative}")
        normalized_files.append(item)
    asset_rows = core._selection_list(
        delivery.get("assets"),
        "manifest.delivery.assets",
        minimum=0,
    )
    if len(asset_rows) > core.MAX_HANDOFF_ASSET_COUNT:
        raise ValueError("Paper Package delivery.assets 数量超过限制")
    normalized_assets: list[dict[str, Any]] = []
    for row in asset_rows:
        item = core._exact_object(row, _DELIVERY_ASSET_FIELDS, "delivery.asset")
        asset_id = item.get("asset_id")
        status = item.get("status")
        package_path = item.get("package_path")
        reason = item.get("reason")
        if (
            not isinstance(asset_id, str)
            or core.ASSET_ID.fullmatch(asset_id) is None
            or status not in {"included", "referenced", "omitted"}
        ):
            raise ValueError("delivery.assets 身份字段无效")
        if status == "included":
            normalized = _normalize_handoff_relative_path(
                package_path,
                f"{asset_id}.package_path",
            )
            if (
                not normalized.startswith("assets/included/")
                or reason is not None
            ):
                raise ValueError(f"included Asset 交付字段无效：{asset_id}")
        elif package_path is not None or not isinstance(reason, str) or not reason:
            raise ValueError(f"非 included Asset 必须给出 reason：{asset_id}")
        normalized_assets.append(item)
    return normalized_files, normalized_assets


def _validate_reverification_shape(
    value: object,
    *,
    producer: dict[str, Any],
    source_run_refs: list[str],
) -> None:
    record = core._exact_object(
        value,
        {
            "schema",
            "status",
            "verified_at",
            "producer",
            "payload_sha256",
            "runs",
        },
        "manifest.reverification",
    )
    if (
        record["schema"] != core.BOUND_EVIDENCE_REVERIFICATION_SCHEMA
        or record["status"] != "pass"
        or not core._is_rfc3339(record["verified_at"])
        or not core._strict_json_equal(record["producer"], producer)
        or not isinstance(record["payload_sha256"], str)
        or _SHA256.fullmatch(record["payload_sha256"]) is None
    ):
        raise ValueError("manifest.reverification 身份无效")
    rows = core._selection_list(
        record["runs"],
        "manifest.reverification.runs",
        minimum=1,
    )
    ids: list[str] = []
    for row in rows:
        item = core._exact_object(
            row,
            {
                "run_id",
                "frozen_digest",
                "snapshot_sha256",
                "seal_sha256",
                "evidence_sha256",
            },
            "manifest.reverification.run",
        )
        run_id = item["run_id"]
        if not isinstance(run_id, str) or core.RUN_ID.fullmatch(run_id) is None:
            raise ValueError("manifest.reverification Run ID 无效")
        ids.append(run_id)
        for field in (
            "frozen_digest",
            "snapshot_sha256",
            "seal_sha256",
            "evidence_sha256",
        ):
            if (
                not isinstance(item[field], str)
                or _SHA256.fullmatch(item[field]) is None
            ):
                raise ValueError(
                    f"manifest.reverification.{field} 无效"
                )
    if ids != source_run_refs:
        raise ValueError(
            "manifest.reverification 必须精确覆盖 source_run_refs"
        )


def _validate_reverification_bindings_from_handoff(
    root: Path,
    manifest: dict[str, Any],
    experiments_bytes: bytes,
) -> None:
    record = manifest["reverification"]
    observed_payload = _handoff_payload_sha256(
        root,
        manifest["delivery"]["files"],
    )
    if record["payload_sha256"] != observed_payload:
        raise ValueError("manifest.reverification payload_sha256 不一致")
    experiments = parse_json_object_bytes(
        experiments_bytes,
        "experiments.json",
    )
    runs: dict[str, dict[str, Any]] = {}
    for experiment in experiments["items"]:
        for run in experiment["runs"]:
            previous = runs.get(run["run_id"])
            if previous is not None and not core._strict_json_equal(
                previous,
                run,
            ):
                raise ValueError("同一 Run 的 1.5 快照不一致")
            runs[run["run_id"]] = run
    if sorted(runs) != manifest["source_run_refs"]:
        raise ValueError("1.5 Run 集合与 source_run_refs 不一致")
    expected = []
    latest: datetime | None = None
    for run_id in manifest["source_run_refs"]:
        run = runs[run_id]
        expected.append(
            {
                "run_id": run_id,
                "frozen_digest": run["frozen_digest"],
                "snapshot_sha256": run["execution_snapshot"][
                    "snapshot_sha256"
                ],
                "seal_sha256": run["output_seal"]["seal_sha256"],
                "evidence_sha256": run["evidence_binding"][
                    "evidence_sha256"
                ],
            }
        )
        for timestamp in (
            run["output_seal"]["sealed_at"],
            run["evidence_binding"]["events"][-1]["time"],
            run["execution"]["closed_at"],
        ):
            current = datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            )
            if latest is None or current > latest:
                latest = current
    if not core._strict_json_equal(record["runs"], expected):
        raise ValueError("manifest.reverification Run 摘要绑定不一致")
    verified_at = datetime.fromisoformat(
        record["verified_at"].replace("Z", "+00:00")
    )
    if latest is not None and verified_at < latest:
        raise ValueError("manifest.reverification 早于 Run 最终状态")


def _handoff_payload_sha256(
    root: Path,
    delivery_files: list[dict[str, Any]],
) -> str:
    digest = hashlib.sha256()
    digest.update(b"cv-research-handoff/package-payload/v1\0")
    resolved_root = root.resolve(strict=True)
    for entry in delivery_files:
        relative = _normalize_handoff_relative_path(
            entry["path"],
            "payload path",
        )
        path, before, resolved = core._inspect_live_regular_file(
            root / relative,
            f"payload:{relative}",
            core.MAX_HANDOFF_SINGLE_ASSET_SIZE,
            root=resolved_root,
        )
        if before.st_size != entry["size_bytes"]:
            raise ValueError(f"payload 文件大小变化：{relative}")
        _update_payload_header(digest, relative, entry["size_bytes"])
        file_digest = hashlib.sha256()
        observed = 0
        try:
            with path.open("rb") as source:
                opened = os.fstat(source.fileno())
                if not os.path.samestat(before, opened):
                    raise ValueError(f"payload 文件身份变化：{relative}")
                while True:
                    chunk = source.read(core.HASH_CHUNK_BYTES)
                    if not chunk:
                        break
                    observed += len(chunk)
                    if observed > entry["size_bytes"]:
                        raise ValueError(f"payload 文件变大：{relative}")
                    file_digest.update(chunk)
                    digest.update(chunk)
                after = os.fstat(source.fileno())
            current = path.lstat()
        except ValueError:
            raise
        except OSError as error:
            raise ValueError(f"payload 文件无法读取：{relative}") from error
        if (
            observed != entry["size_bytes"]
            or f"sha256:{file_digest.hexdigest()}" != entry["sha256"]
            or not os.path.samestat(opened, after)
            or not os.path.samestat(before, current)
            or current.st_size != before.st_size
            or current.st_mtime_ns != before.st_mtime_ns
            or path.resolve(strict=True) != resolved
        ):
            raise ValueError(f"payload 文件在读取期间变化：{relative}")
    return f"sha256:{digest.hexdigest()}"


def _validate_scientific_intrinsic(
    contents: dict[str, bytes],
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    package_id = manifest["package_id"]
    study = parse_json_object_bytes(contents["study.json"], "study.json")
    if (
        set(study) != {"schema", "brief", "paper_scope"}
        or study.get("schema")
        != "cv-experiment-workflow.paper-package-study.v1"
        or contents["study.json"] != core._canonical_json_line(study)
    ):
        raise ValueError("Paper Package study.json 内在结构无效")
    if not core._strict_json_equal(study["paper_scope"], manifest["paper_scope"]):
        raise ValueError("Paper Package paper_scope 不一致")
    brief = core._exact_object(
        study.get("brief"),
        core.OUTPUT_FIELDS,
        "study.brief",
    )
    if (
        brief.get("schema") != core.RESEARCH_BRIEF_SCHEMA
        or brief.get("brief_id") != manifest["brief_id"]
        or brief.get("project_id") != manifest["project_id"]
        or type(brief.get("revision")) is not int
        or brief["revision"] < 1
        or not core._is_rfc3339(brief.get("created_at"))
    ):
        raise ValueError("Paper Package Research Brief 身份无效")
    normalized_brief = core._normalize_manifest(
        {field: brief[field] for field in core.INPUT_FIELDS}
    )
    if any(
        not core._strict_json_equal(brief[field], normalized_brief[field])
        for field in core.INPUT_FIELDS
    ):
        raise ValueError("Paper Package Research Brief 正文字段未规范化")
    if (
        study["paper_scope"].get("research_area")
        != normalized_brief["research_area"]
        or study["paper_scope"].get("goal")
        != normalized_brief["objective"]
        or study["paper_scope"].get("excluded_topics")
        != normalized_brief["scope"]["excluded"]
    ):
        raise ValueError(
            "Paper Package paper_scope 必须与 Research Brief 的研究领域、目标和排除项一致"
        )

    experiments = parse_json_object_bytes(
        contents["experiments.json"],
        "experiments.json",
    )
    visuals = parse_json_object_bytes(contents["visuals.json"], "visuals.json")
    if (
        set(experiments) != {"schema", "items"}
        or experiments.get("schema")
        != "cv-experiment-workflow.paper-package-experiments.v1"
        or contents["experiments.json"] != core._canonical_json_line(experiments)
    ):
        raise ValueError("Paper Package experiments.json 内在结构无效")
    if (
        set(visuals) != {"schema", "items"}
        or visuals.get("schema")
        != "cv-experiment-workflow.paper-package-visuals.v1"
        or contents["visuals.json"] != core._canonical_json_line(visuals)
    ):
        raise ValueError("Paper Package visuals.json 内在结构无效")
    claim_rows = core._parse_jsonl(
        contents["claims.jsonl"],
        "claims.jsonl",
        "claim_id",
    )
    source_rows = core._parse_jsonl(
        contents["sources.jsonl"],
        "sources.jsonl",
        "source_ref",
        allow_same_primary=True,
    )
    asset_rows = core._parse_jsonl(
        contents["assets.jsonl"],
        "assets.jsonl",
        "asset_id",
    )
    if contents["claims.jsonl"] != core._canonical_jsonl(claim_rows, "claim_id"):
        raise ValueError("Paper Package claims.jsonl 不是规范 JSONL")
    if contents["sources.jsonl"] != core._canonical_jsonl(source_rows, "source_ref"):
        raise ValueError("Paper Package sources.jsonl 不是规范 JSONL")
    if contents["assets.jsonl"] != core._canonical_jsonl(asset_rows, "asset_id"):
        raise ValueError("Paper Package assets.jsonl 不是规范 JSONL")
    _validate_intrinsic_claims(claim_rows, study["paper_scope"], manifest)
    source_support = _validate_intrinsic_sources(source_rows, claim_rows)
    _validate_intrinsic_claim_source_support(claim_rows, source_support)
    _validate_intrinsic_assets(asset_rows)
    run_rows = _validate_intrinsic_experiments(
        experiments.get("items"),
        claim_rows,
        asset_rows,
        bound_profile=(
            core._producer_tuple(manifest["producer"])
            == core.BOUND_EVIDENCE_PRODUCER_TUPLE
        ),
    )
    _validate_intrinsic_readiness(
        manifest,
        claim_rows,
        asset_rows,
        run_rows,
    )
    _validate_intrinsic_visuals(
        visuals.get("items"),
        claim_rows,
        experiments.get("items"),
        asset_rows,
    )
    return asset_rows


def _validate_intrinsic_claims(
    rows: list[dict[str, Any]],
    scope_value: object,
    manifest: dict[str, Any],
) -> None:
    fields = {
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
    claim_ids = [row.get("claim_id") for row in rows]
    if claim_ids != [
        f"CLM-{number:04d}" for number in range(1, len(rows) + 1)
    ]:
        raise ValueError("Paper Package Claim ID 不连续")
    scope = core._exact_object(
        scope_value,
        {
            "title_hint",
            "research_area",
            "goal",
            "included_claim_ids",
            "excluded_topics",
        },
        "paper_scope",
    )
    if (
        core._normalize_paper_scope(scope, claim_ids=claim_ids) != scope
        or scope["included_claim_ids"] != claim_ids
        or not core._strict_json_equal(scope, manifest["paper_scope"])
    ):
        raise ValueError("Paper Package paper_scope Claim 引用无效")
    projected: list[dict[str, Any]] = []
    source_runs: set[str] = set()
    confirmed_results = 0
    for row in rows:
        core._exact_object(row, fields, "Claim")
        if (
            row["kind"] not in core.CLAIM_KINDS
            or row["origin"] not in {"project", "external"}
            or row["maturity"] not in {"supported", "confirmed"}
        ):
            raise ValueError(f"Paper Package Claim 值无效：{row['claim_id']}")
        metric_refs = row["metric_refs"]
        projected_metrics = [
            {
                "run_id": item.get("run_id"),
                "metric_name": item.get("metric_name"),
            }
            if isinstance(item, dict)
            else item
            for item in metric_refs
        ] if isinstance(metric_refs, list) else metric_refs
        projected.append(
            {
                key: deepcopy(row[key])
                for key in fields - {"comparisons", "metric_refs"}
            }
            | {"metric_refs": projected_metrics}
        )
        source_runs.update(row["run_refs"])
        if row["kind"] == "result":
            metric_rows = core._selection_list(
                row["metric_refs"],
                f"{row['claim_id']}.metric_refs",
                minimum=1,
            )
            metric_runs: set[str] = set()
            for metric in metric_rows:
                item = core._exact_object(
                    metric,
                    {"run_id", "metric_name", "value", "definition"},
                    f"{row['claim_id']}.metric_ref",
                )
                value = item["value"]
                if (
                    not isinstance(item["run_id"], str)
                    or core.RUN_ID.fullmatch(item["run_id"]) is None
                    or not isinstance(item["metric_name"], str)
                    or not item["metric_name"].strip()
                    or type(value) not in {int, float}
                    or not math.isfinite(value)
                    or not isinstance(item["definition"], str)
                    or not item["definition"].strip()
                ):
                    raise ValueError(
                        f"result Claim 指标快照无效：{row['claim_id']}"
                    )
                metric_runs.add(item["run_id"])
            core._selection_list(
                row["comparisons"],
                f"{row['claim_id']}.comparisons",
                minimum=0,
            )
            if (
                row["origin"] != "project"
                or row["maturity"] != "confirmed"
                or not row["run_refs"]
                or metric_runs != set(row["run_refs"])
                or row["source_refs"]
                or row["idea_refs"]
                or row["module_refs"]
                or row["innovation_boundary"] is not None
            ):
                raise ValueError(
                    f"result Claim 必须是 confirmed 项目 Run 事实："
                    f"{row['claim_id']}"
                )
            confirmed_results += 1
        elif row["metric_refs"] or row["comparisons"]:
            raise ValueError(
                f"非 result Claim 不得包含指标或 comparison：{row['claim_id']}"
            )
        elif row["origin"] == "external":
            if (
                row["kind"] not in {"background", "research_gap", "method"}
                or row["maturity"] != "supported"
                or not row["source_refs"]
                or row["run_refs"]
                or row["idea_refs"]
                or row["module_refs"]
                or row["innovation_boundary"] is not None
            ):
                raise ValueError(
                    f"external Claim 语义无效：{row['claim_id']}"
                )
        elif row["kind"] == "innovation":
            if (
                row["maturity"] != "supported"
                or row["run_refs"]
                or not row["source_refs"]
                or not row["idea_refs"]
                or not row["module_refs"]
                or row["innovation_boundary"] is None
            ):
                raise ValueError(
                    f"innovation Claim 证据边界无效：{row['claim_id']}"
                )
        elif (
            row["origin"] != "project"
            or row["kind"] not in {"objective", "method", "protocol", "limitation"}
            or row["maturity"] != "supported"
            or row["run_refs"]
            or row["idea_refs"]
            or row["module_refs"]
            or row["innovation_boundary"] is not None
        ):
            raise ValueError(
                f"project Claim 语义无效：{row['claim_id']}"
            )
    if not core._strict_json_equal(core._normalize_claims(projected), projected):
        raise ValueError("Paper Package Claim 字段未规范化")
    if confirmed_results < 1:
        raise ValueError("Paper Package 至少需要一个 confirmed result Claim")
    if sorted(source_runs) != manifest["source_run_refs"]:
        raise ValueError("Paper Package source_run_refs 与 Claim 不一致")


def _validate_intrinsic_sources(
    rows: list[dict[str, Any]],
    claims: list[dict[str, Any]],
) -> dict[tuple[str, str], set[str]]:
    fields = {
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
    claim_ids = {row["claim_id"] for row in claims}
    projected = []
    support: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        core._exact_object(row, fields, "Source")
        if row["kind"] != "paper":
            raise ValueError("Paper Package Source kind 必须是 paper")
        for field in ("identity", "locator", "revision", "license"):
            normalized = core._text(
                row.get(field),
                f"Source.{field}",
            )
            if row[field] != normalized:
                raise ValueError(
                    f"Paper Package Source {field} 必须是规范化非空字符串"
                )
        if (
            row["role"] not in core.SOURCE_USE_ROLES
            or not isinstance(row["source_sha256"], str)
            or _SHA256.fullmatch(row["source_sha256"]) is None
            or row["excerpt_sha256"] != core._sha256_text(row["excerpt"])
            or not set(row["supports_claim_ids"]) <= claim_ids
        ):
            raise ValueError("Paper Package Source 内在字段无效")
        projected.append(
            {
                key: deepcopy(row[key])
                for key in (
                    "source_ref",
                    "role",
                    "excerpt",
                    "supports_claim_ids",
                )
            }
        )
        for claim_id in row["supports_claim_ids"]:
            support.setdefault(
                (row["source_ref"], claim_id),
                set(),
            ).add(row["role"])
    if not core._strict_json_equal(core._normalize_source_uses(projected), projected):
        raise ValueError("Paper Package Source 字段未规范化")
    return support


def _validate_intrinsic_claim_source_support(
    claims: list[dict[str, Any]],
    support: dict[tuple[str, str], set[str]],
) -> None:
    for claim in claims:
        claim_id = claim["claim_id"]
        if any(
            (source_ref, claim_id) not in support
            for source_ref in claim["source_refs"]
        ):
            raise ValueError(
                f"Claim 缺少对应 Source 支撑：{claim_id}"
            )
        if claim["kind"] == "innovation" and not any(
            "prior_art" in support.get((source_ref, claim_id), set())
            for source_ref in claim["source_refs"]
        ):
            raise ValueError(
                f"innovation Claim 缺少 prior_art 支撑：{claim_id}"
            )


def _validate_intrinsic_assets(rows: list[dict[str, Any]]) -> None:
    fields = {
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
    ids = [row.get("asset_id") for row in rows]
    if ids != [f"AST-{number:04d}" for number in range(1, len(rows) + 1)]:
        raise ValueError("Paper Package Asset ID 不连续")
    input_fields = (
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
    projected = [
        {key: deepcopy(row.get(key)) for key in input_fields}
        for row in rows
    ]
    if not core._strict_json_equal(core._normalize_assets(projected), projected):
        raise ValueError("Paper Package Asset 字段未规范化")
    for row in rows:
        core._exact_object(row, fields, "Asset")
        if not isinstance(row["reference_uri"], str) or not row["reference_uri"]:
            raise ValueError("Paper Package Asset reference_uri 无效")
        size = row["size_bytes"]
        digest = row["sha256"]
        if row["availability"] == "available":
            if (
                type(size) is not int
                or size < 0
                or not isinstance(digest, str)
                or _SHA256.fullmatch(digest) is None
            ):
                raise ValueError("Paper Package available Asset 身份无效")
        elif size is not None or digest is not None:
            raise ValueError("Paper Package 未读取 Asset 的大小和哈希必须为空")


def _validate_intrinsic_experiments(
    value: object,
    claims: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    *,
    bound_profile: bool = False,
) -> dict[str, dict[str, Any]]:
    rows = core._selection_list(value, "experiments.items", minimum=1)
    ids = [
        row.get("experiment_id") if isinstance(row, dict) else None
        for row in rows
    ]
    if ids != [f"EXP-{number:04d}" for number in range(1, len(rows) + 1)]:
        raise ValueError("Paper Package Experiment ID 不连续")
    fields = {
        "experiment_id",
        "title",
        "objective",
        "task_id",
        "run_refs",
        "claim_refs",
        "task",
        "runs",
    }
    input_fields = (
        "experiment_id",
        "title",
        "objective",
        "task_id",
        "run_refs",
        "claim_refs",
    )
    projected = []
    claim_ids = {row["claim_id"] for row in claims}
    asset_map = {row["asset_id"]: row for row in assets}
    task_map: dict[str, dict[str, Any]] = {}
    run_map: dict[str, dict[str, Any]] = {}
    run_owner: dict[str, str] = {}
    covered_result_claims: set[str] = set()
    result_claim_ids = {
        row["claim_id"] for row in claims if row["kind"] == "result"
    }
    from .tasking import TASK_FORWARD, TASK_ROUTES

    for row in rows:
        core._exact_object(row, fields, "Experiment")
        projected.append({key: deepcopy(row[key]) for key in input_fields})
        if not set(row["claim_refs"]) <= claim_ids:
            raise ValueError("Experiment 引用了未知 Claim")
        task = core._exact_object(
            row["task"],
            {
                "task_id",
                "route",
                "target_refs",
                "route_inputs",
                "stage",
                "conclusion",
            },
            "Experiment.task",
        )
        if (
            task["task_id"] != row["task_id"]
            or not isinstance(task["task_id"], str)
            or core.TASK_ID.fullmatch(task["task_id"]) is None
            or task["route"] not in TASK_ROUTES
            or not isinstance(task["target_refs"], list)
            or not isinstance(task["route_inputs"], dict)
            or task["stage"] not in TASK_FORWARD
            or (task["stage"] == "done" and task["conclusion"] is None)
            or (task["stage"] != "done" and task["conclusion"] is not None)
        ):
            raise ValueError("Experiment Task 快照无效")
        previous_task = task_map.get(task["task_id"])
        if previous_task is not None and not core._strict_json_equal(
            previous_task,
            task,
        ):
            raise ValueError(
                f"同一 Task 快照不一致：{task['task_id']}"
            )
        task_map[task["task_id"]] = task
        run_rows = core._selection_list(
            row["runs"],
            f"{row['experiment_id']}.runs",
            minimum=1,
        )
        run_ids: list[str] = []
        for run in run_rows:
            run_fields = {
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
            }
            if bound_profile:
                run_fields.update(
                    {
                        "execution_snapshot",
                        "execution_snapshot_manifest",
                        "output_seal",
                        "evidence_binding",
                    }
                )
            core._exact_object(
                run,
                run_fields,
                "Experiment.run",
            )
            run_id = run["run_id"]
            if not isinstance(run_id, str) or core.RUN_ID.fullmatch(run_id) is None:
                raise ValueError("Experiment Run ID 无效")
            run_ids.append(run_id)
            previous_owner = run_owner.get(run_id)
            if previous_owner is not None and previous_owner != task["task_id"]:
                raise ValueError(
                    f"同一 Run 不得归属于不同 Task：{run_id}"
                )
            run_owner[run_id] = task["task_id"]
            previous = run_map.get(run_id)
            if previous is not None and not core._strict_json_equal(previous, run):
                raise ValueError(f"同一 Run 快照不一致：{run_id}")
            run_map[run_id] = run
            execution = core._exact_object(
                run["execution"],
                {
                    "stage",
                    "process_id",
                    "started_at",
                    "finished_at",
                    "closed_at",
                    "outcome",
                    "exit_code",
                    "issue_kind",
                },
                f"{run_id}.execution",
            )
            quality = core._exact_object(
                run["quality"],
                {"implementation", "interface", "data", "metrics"},
                f"{run_id}.quality",
            )
            result = core._exact_object(
                run["result"],
                {"metrics", "raw_log"},
                f"{run_id}.result",
            )
            metrics = core._exact_object(
                result["metrics"],
                set(result["metrics"]) if isinstance(result["metrics"], dict) else set(),
                f"{run_id}.metrics",
            )
            artifact_paths = core._selection_text_list(
                run["artifact_paths"],
                f"{run_id}.artifact_paths",
                minimum=0,
            )
            if bound_profile:
                _validate_bound_run_shape(
                    run,
                    task_id=task["task_id"],
                )
            if (
                not isinstance(run["purpose"], str)
                or not run["purpose"]
                or not isinstance(run["frozen_digest"], str)
                or _SHA256.fullmatch(run["frozen_digest"]) is None
                or not isinstance(run["frozen"], dict)
                or execution["stage"] not in {"finished", "closed"}
                or execution["outcome"] not in {"succeeded", "failed"}
                or type(execution["exit_code"]) is not int
                or any(
                    value not in {"valid", "invalid", "uncertain"}
                    for value in quality.values()
                )
                or not isinstance(result["raw_log"], str)
                or not result["raw_log"]
                or artifact_paths != run["artifact_paths"]
                or result["raw_log"] in artifact_paths
                or not metrics
                or any(
                    type(value) not in {int, float}
                    or not math.isfinite(value)
                    for value in metrics.values()
                )
                or not isinstance(run["analysis"], dict)
            ):
                raise ValueError(f"Experiment Run 质量快照无效：{run_id}")
            seen_files: set[tuple[str, str]] = set()
            for file_row in core._selection_list(
                run["files"],
                f"{run_id}.files",
                minimum=0,
            ):
                item = core._exact_object(
                    file_row,
                    {"path", "asset_id", "size_bytes", "sha256"},
                    "Experiment.run.file",
                )
                asset = asset_map.get(item["asset_id"])
                if asset is None or any(
                    not core._strict_json_equal(asset[field], item[field])
                    for field in ("path", "size_bytes", "sha256")
                ) or asset["source_object_ref"] != run_id:
                    raise ValueError("Experiment Run 文件与 Asset 不一致")
                key = (item["path"], item["asset_id"])
                if key in seen_files:
                    raise ValueError("Experiment Run 文件重复")
                seen_files.add(key)
        if run_ids != row["run_refs"]:
            raise ValueError("Experiment run_refs 与快照不一致")
        for claim_id in row["claim_refs"]:
            claim = next(item for item in claims if item["claim_id"] == claim_id)
            if claim["run_refs"] and not set(claim["run_refs"]) <= set(run_ids):
                raise ValueError("Experiment 未覆盖 Claim 的全部 Run")
            if claim_id in result_claim_ids:
                covered_result_claims.add(claim_id)
    if not core._strict_json_equal(core._normalize_experiments(projected), projected):
        raise ValueError("Paper Package Experiment 字段未规范化")
    if covered_result_claims != result_claim_ids:
        raise ValueError("Paper Package result Claim 未被 Experiment 覆盖")
    for claim in claims:
        if claim["kind"] != "result":
            continue
        unresolved = deepcopy(claim)
        unresolved["metric_refs"] = [
            {
                "run_id": metric["run_id"],
                "metric_name": metric["metric_name"],
            }
            for metric in claim["metric_refs"]
        ]
        augmented_runs = {
            run_id: {
                **deepcopy(run),
                "task_id": run_owner[run_id],
            }
            for run_id, run in run_map.items()
        }
        augmented_tasks = {
            task_id: {
                **deepcopy(task),
                "id": task_id,
            }
            for task_id, task in task_map.items()
        }
        expected = core._resolve_result_claim(
            unresolved,
            tasks=augmented_tasks,
            runs=augmented_runs,
            levels={
                run_id: "confirmed"
                for run_id in claim["run_refs"]
            },
        )
        if not core._strict_json_equal(expected, claim):
            raise ValueError(
                f"result Claim 的指标或 comparison 与 Task/Run 快照不一致："
                f"{claim['claim_id']}"
            )
    return run_map


def _validate_bound_run_shape(
    run: dict[str, Any],
    *,
    task_id: str,
) -> None:
    run_id = run["run_id"]
    frozen = core._exact_object(
        run["frozen"],
        {"code", "config", "seed", "data", "environment"},
        f"{run_id}.frozen",
    )
    code = core._exact_object(
        frozen["code"],
        {
            "schema",
            "codebase_id",
            "branch",
            "commit",
            "tag",
            "clean_required",
            "worktree_clean",
            "adapter_path",
            "adapter_sha256",
            "declared_code",
            "fingerprints",
        },
        f"{run_id}.frozen.code",
    )
    data = core._exact_object(
        frozen["data"],
        set(frozen["data"]) if isinstance(frozen["data"], dict) else set(),
        f"{run_id}.frozen.data",
    )
    if (
        run["purpose"] != "evidence"
        or code["schema"] != "cv-experiment-workflow.bound-code.v2"
        or code["clean_required"] is not True
        or code["worktree_clean"] is not True
        or data.get("run_kind") != "real_experiment"
        or data.get("paper_eligible") is not True
        or run["execution"]["stage"] != "closed"
        or run["execution"]["outcome"] != "succeeded"
        or run["execution"]["exit_code"] != 0
        or run["execution"]["issue_kind"] is not None
        or set(run["quality"].values()) != {"valid"}
    ):
        raise ValueError(f"1.5 Run 不是可写论文的正式证据：{run_id}")
    snapshot = core._exact_object(
        run["execution_snapshot"],
        {
            "schema",
            "root",
            "commit",
            "relative_path",
            "manifest_path",
            "snapshot_sha256",
            "output_relative_path",
        },
        f"{run_id}.execution_snapshot",
    )
    snapshot_manifest = core._exact_object(
        run["execution_snapshot_manifest"],
        {"schema", "root", "commit", "files"},
        f"{run_id}.execution_snapshot_manifest",
    )
    if (
        snapshot["schema"]
        != "cv-experiment-workflow.execution-snapshot.v1"
        or snapshot_manifest["schema"]
        != "cv-experiment-workflow.execution-snapshot-manifest.v1"
        or snapshot["snapshot_sha256"]
        != core._canonical_object_sha256(snapshot_manifest)
        or snapshot["commit"] != code["commit"]
        or snapshot_manifest["commit"] != code["commit"]
    ):
        raise ValueError(f"1.5 execution snapshot 身份无效：{run_id}")
    evidence = core._exact_object(
        run["evidence_binding"],
        {"schema", "level", "events", "evidence_sha256"},
        f"{run_id}.evidence_binding",
    )
    if (
        evidence["schema"] != core.BOUND_EVIDENCE_BINDING_SCHEMA
        or evidence["level"] != "confirmed"
        or evidence["evidence_sha256"]
        != core._canonical_object_sha256(
            {
                key: evidence[key]
                for key in evidence
                if key != "evidence_sha256"
            }
        )
        or [
            (event.get("from"), event.get("to"))
            for event in evidence["events"]
        ]
        != [("none", "single_run"), ("single_run", "confirmed")]
    ):
        raise ValueError(f"1.5 Evidence binding 无效：{run_id}")
    seal = core._exact_object(
        run["output_seal"],
        {
            "schema",
            "source_root",
            "authoritative_root",
            "execution_snapshot_sha256",
            "frozen_digest",
            "finished_snapshot_sha256",
            "raw_log",
            "artifacts",
            "total_files",
            "total_bytes",
            "comparability",
            "sealed_at",
            "seal_sha256",
        },
        f"{run_id}.output_seal",
    )
    expected_finished = core._canonical_object_sha256(
        {
            "id": run_id,
            "task_id": task_id,
            "purpose": run["purpose"],
            "frozen_digest": run["frozen_digest"],
            "execution": {
                key: run["execution"].get(key)
                for key in (
                    "outcome",
                    "started_at",
                    "finished_at",
                    "process_id",
                    "exit_code",
                    "issue_kind",
                )
            },
            "result": run["result"],
            "quality": run["quality"],
            "analysis": run["analysis"],
            "artifacts": run["artifact_paths"],
            "execution_snapshot": run["execution_snapshot"],
        }
    )
    if (
        seal["schema"] != core.BOUND_OUTPUT_SEAL_SCHEMA
        or seal["execution_snapshot_sha256"]
        != snapshot["snapshot_sha256"]
        or seal["frozen_digest"] != run["frozen_digest"]
        or seal["finished_snapshot_sha256"] != expected_finished
        or seal["seal_sha256"]
        != core._canonical_object_sha256(
            {
                key: seal[key]
                for key in seal
                if key != "seal_sha256"
            }
        )
    ):
        raise ValueError(f"1.5 portable output seal 无效：{run_id}")


def _validate_intrinsic_readiness(
    manifest: dict[str, Any],
    claims: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
) -> None:
    """从科学文件重算可确定的缺失项，并核实其余缺失声明有现场依据。"""
    mandatory: set[str] = set()
    asset_by_owner_path: dict[tuple[str, str], dict[str, Any]] = {}
    for asset in assets:
        key = (asset["source_object_ref"], asset["path"])
        if key in asset_by_owner_path:
            raise ValueError(
                "Paper Package Asset 的来源对象与路径组合不得重复"
            )
        asset_by_owner_path[key] = asset
        if not asset["required_for_writing"]:
            continue
        if asset["availability"] != "available":
            mandatory.add(
                f"required_asset_unavailable:{asset['asset_id']}"
            )
        if not asset["copy_allowed"]:
            mandatory.add(
                f"required_asset_not_copyable:{asset['asset_id']}"
            )
        if asset["license"].casefold() == "unknown":
            mandatory.add(
                f"required_asset_license_unknown:{asset['asset_id']}"
            )
        if asset["privacy_classification"] in {
            "sensitive",
            "restricted",
        }:
            mandatory.add(
                f"required_asset_privacy_blocked:{asset['asset_id']}"
            )

    result_run_ids = {
        run_id
        for claim in claims
        if claim["kind"] == "result"
        for run_id in claim["run_refs"]
    }
    for run_id, run in runs.items():
        files = core._selection_list(
            run["files"],
            f"{run_id}.files",
            minimum=0,
        )
        raw_log = run["result"]["raw_log"]
        expected_paths = [
            raw_log,
            *run["artifact_paths"],
        ]
        expected_file_keys: set[tuple[str, str]] = set()
        for path in expected_paths:
            asset = asset_by_owner_path.get((run_id, path))
            if asset is None:
                mandatory.add(f"result_file_missing:{run_id}:{path}")
                continue
            expected_file_keys.add((path, asset["asset_id"]))
            if asset["sha256"] is None:
                mandatory.add(
                    f"result_file_unavailable:{run_id}:{path}"
                )
            if (
                run_id in result_run_ids
                and path == raw_log
                and asset["role"] != "raw_log"
            ):
                raise ValueError(
                    f"confirmed result Run 的 raw_log Asset role 无效："
                    f"{run_id}"
                )
        actual_file_keys = {
            (file_row["path"], file_row["asset_id"])
            for file_row in files
        }
        if actual_file_keys != expected_file_keys:
            raise ValueError(
                f"Experiment Run.files 必须精确锚定所有已声明的预期文件："
                f"{run_id}"
            )
    expected_missing = sorted(mandatory)
    if manifest["missing_requirements"] != expected_missing:
        raise ValueError(
            "Paper Package readiness/missing_requirements "
            "与科学内容重算结果不一致"
        )
    expected_readiness = (
        "paper_ready" if not expected_missing else "incomplete"
    )
    if manifest["readiness"] != expected_readiness:
        raise ValueError(
            "Paper Package readiness 与科学内容重算结果不一致"
        )


def _validate_intrinsic_visuals(
    value: object,
    claims: list[dict[str, Any]],
    experiments: object,
    assets: list[dict[str, Any]],
) -> None:
    rows = core._selection_list(value, "visuals.items", minimum=0)
    if not core._strict_json_equal(core._normalize_visuals(rows), rows):
        raise ValueError("Paper Package Visual 字段未规范化")
    experiment_rows = core._selection_list(
        experiments,
        "experiments.items",
        minimum=1,
    )
    claim_ids = {row["claim_id"] for row in claims}
    experiment_ids = {row["experiment_id"] for row in experiment_rows}
    asset_ids = {row["asset_id"] for row in assets}
    for row in rows:
        if (
            not set(row["claim_refs"]) <= claim_ids
            or not set(row["experiment_refs"]) <= experiment_ids
            or not set(row["asset_refs"]) <= asset_ids
        ):
            raise ValueError("Paper Package Visual 引用无效")


def _validate_delivery_assets(
    delivery_rows: list[dict[str, Any]],
    asset_rows: list[dict[str, Any]],
    actual_facts: dict[str, dict[str, Any]],
    *,
    asset_mode: str,
    readiness: str,
) -> None:
    if [row["asset_id"] for row in delivery_rows] != [
        row["asset_id"] for row in asset_rows
    ]:
        raise ValueError("delivery.assets 必须逐项覆盖 assets.jsonl")
    expected_delivery = _derive_delivery_assets(asset_rows, asset_mode)
    _require_ready_assets_included(
        readiness,
        asset_rows,
        expected_delivery,
    )
    if not core._strict_json_equal(delivery_rows, expected_delivery):
        raise ValueError(
            "delivery.assets 与 asset_mode、许可、隐私或同哈希策略不一致"
        )
    asset_map = {row["asset_id"]: row for row in asset_rows}
    for delivery in delivery_rows:
        asset = asset_map[delivery["asset_id"]]
        status = delivery["status"]
        if status == "included":
            package_path = delivery["package_path"]
            facts = actual_facts.get(package_path)
            if facts is None or (
                facts["size_bytes"] != asset["size_bytes"]
                or facts["sha256"] != asset["sha256"]
            ):
                raise ValueError(
                    f"included Asset 与原始身份不一致：{delivery['asset_id']}"
                )
            if (
                asset["availability"] != "available"
                or not asset["copy_allowed"]
                or asset["license"].casefold() == "unknown"
                or asset["privacy_classification"] in {"sensitive", "restricted"}
            ):
                raise ValueError(
                    f"Asset 不具备实际包含条件：{delivery['asset_id']}"
                )


def _validate_actual_budget(
    facts: dict[str, dict[str, Any]],
    checksum_content: bytes,
) -> None:
    control_total = len(checksum_content)
    asset_total = 0
    for path, item in facts.items():
        if path.startswith("assets/included/"):
            asset_total += item["size_bytes"]
        else:
            control_total += item["size_bytes"]
    if (
        control_total > core.MAX_HANDOFF_CONTROL_TOTAL_SIZE
        or asset_total > core.MAX_HANDOFF_ASSET_TOTAL_SIZE
        or control_total + asset_total > core.MAX_HANDOFF_PACKAGE_TOTAL_SIZE
    ):
        raise ValueError("Paper Package 交付目录实际大小超过预算")


def _verify_final_inventory(
    root: Path,
    initial_files: dict[str, dict[str, Any]],
    initial_directories: set[str],
) -> None:
    final_files, final_directories = _preflight_handoff_tree(root)
    if (
        set(final_files) != set(initial_files)
        or final_directories != initial_directories
    ):
        raise ValueError("Paper Package 交付目录在核验期间发生布局变化")
    for relative, initial in initial_files.items():
        final = final_files[relative]
        before = initial["stat"]
        after = final["stat"]
        if (
            not os.path.samestat(before, after)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise ValueError(
                f"Paper Package 文件在核验期间身份发生变化：{relative}"
            )


def _handoff_checksum_bytes(
    contents: dict[str, bytes | None],
    *,
    asset_hashes: dict[str, str],
) -> bytes:
    lines: list[bytes] = []
    for name in sorted(contents, key=lambda value: value.encode("utf-8")):
        content = contents[name]
        if content is None:
            digest = asset_hashes[name].removeprefix("sha256:")
        else:
            digest = hashlib.sha256(content).hexdigest()
        if _RAW_SHA256.fullmatch(digest) is None:
            raise ValueError(f"交付 checksum 哈希无效：{name}")
        lines.append(f"{digest}  {name}\n".encode("utf-8"))
    return b"".join(lines)


def _normalize_handoff_relative_path(
    value: object,
    label: str,
) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} 必须是非空相对路径")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise ValueError(f"{label} 必须是有效 UTF-8 路径") from error
    if (
        len(encoded) > 1024
        or value.startswith(("/", "\\"))
        or "\\" in value
        or re.match(r"^[A-Za-z]:", value)
        or any(
            unicodedata.category(character).startswith("C")
            for character in value
        )
        or unicodedata.normalize("NFKC", value) != value
    ):
        raise ValueError(f"{label} 不是安全的 NFKC 相对路径")
    segments = value.split("/")
    if any(
        not segment
        or len(segment.encode("utf-8")) > 255
        or segment in {".", ".."}
        or ":" in segment
        or segment.endswith((".", " "))
        or segment.split(".", 1)[0].casefold() in _WINDOWS_RESERVED
        for segment in segments
    ):
        raise ValueError(f"{label} 含有禁止的路径片段")
    return value


def _windows_path_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _asset_status_counts(
    rows: list[dict[str, Any]],
) -> dict[str, int]:
    return {
        "included_asset_count": sum(row["status"] == "included" for row in rows),
        "referenced_asset_count": sum(row["status"] == "referenced" for row in rows),
        "omitted_asset_count": sum(row["status"] == "omitted" for row in rows),
    }
