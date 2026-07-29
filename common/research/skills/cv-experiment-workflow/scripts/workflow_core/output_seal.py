from __future__ import annotations

import hashlib
import json
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

from .locking import project_snapshot_lock, project_write_lock
from .project import PROJECT_SCHEMA_V2, is_link_or_reparse
from .records import (
    _initialized_project,
    _validate_project,
    normalize_safe_relative_path,
)


OUTPUT_SEAL_SCHEMA = "cv-experiment-workflow.output-seal/v1"
OUTPUT_ROOT_KIND = "run_execution_snapshot_output"
SEALED_ROOT_KIND = "project_sealed_output_copy"
SEALED_ROOT_DIRECTORY = ".cv-workflow-seals"
MAX_OUTPUT_FILE_COUNT = 128
MAX_TOTAL_OUTPUT_BYTES = 64 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
MAX_RELATIVE_PATH_BYTES = 1024
MAX_PATH_SEGMENT_BYTES = 255
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"}
_WINDOWS_RESERVED.update(f"com{index}" for index in range(1, 10))
_WINDOWS_RESERVED.update(f"lpt{index}" for index in range(1, 10))
_WINDOWS_FORBIDDEN = set('<>:"|?*')
_SEAL_FIELDS = {
    "schema",
    "source",
    "root",
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
}
_FILE_RECORD_FIELDS = {"path", "size_bytes", "sha256"}


def seal_run_outputs(project: Path, run_id: str) -> dict[str, Any]:
    """把正式绑定 Run 的完整输出复制到工作流掌管的只读来源区。"""
    from .execution_snapshot import verify_run_execution_snapshot
    from .runs import (
        _load_run_locked,
        _run_id,
        _validate_run_refs_locked,
        _write_run_locked,
    )
    from .tasking import _validate_command_root_locked

    _run_id(run_id)
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _validate_command_root_locked(control)
        run = _load_run_locked(control, run_id)
        _validate_run_refs_locked(control, run)
        _require_sealable_run(run)
        if run["output_seal"] is not None:
            verify_run_execution_snapshot(control, run)
            _verify_output_tree(root, run, run["output_seal"])
            return deepcopy(run)

        execution_root = verify_run_execution_snapshot(control, run)
        container = _prepare_seal_container(root)
        final = container / run_id
        if os.path.lexists(final):
            raise ValueError("本次 Run 的封存目录已存在，拒绝覆盖")
        staging = container / f".staging-{run_id}-{uuid.uuid4().hex}"
        staging.mkdir()
        staging_identity = _directory_identity(staging)
        published = False
        seal: dict[str, Any] | None = None
        try:
            seal = _build_output_seal(execution_root, run, staging)
            validate_output_seal_record(run, seal)
            _verify_records_match_current_tree(
                staging,
                run["execution_snapshot"]["output_relative_path"],
                [seal["raw_log"], *seal["artifacts"]],
            )
            _atomic_publish_directory_no_overwrite(staging, final)
            published = True
            _verify_output_tree(root, run, seal)

            updated = deepcopy(run)
            updated["output_seal"] = seal
            written = _write_run_locked(control, updated)
            _verify_output_tree(root, written, seal)
            return written
        except BaseException:
            current = _load_run_locked(control, run_id)
            if seal is not None and current.get("output_seal") == seal:
                rolled_back = deepcopy(current)
                rolled_back["output_seal"] = None
                _write_run_locked(control, rolled_back)
            cleanup_target = final if published else staging
            _remove_owned_seal_tree(
                cleanup_target,
                expected_parent=container,
                expected_identity=staging_identity,
                run_id=run_id,
            )
            raise


def verify_run_output_seal(project: Path, run_id: str) -> dict[str, Any]:
    """重读工作流封存副本，确认它仍与 Run 记录逐字节一致。"""
    from .execution_snapshot import verify_run_execution_snapshot
    from .runs import _load_run_locked, _run_id, _validate_run_refs_locked
    from .tasking import _validate_command_root_locked

    _run_id(run_id)
    root = _initialized_project(project)
    with project_snapshot_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _validate_command_root_locked(control)
        run = _load_run_locked(control, run_id)
        _validate_run_refs_locked(control, run)
        seal = run.get("output_seal")
        if not isinstance(seal, dict):
            raise ValueError("Run 尚未封存正式输出")
        verify_run_execution_snapshot(control, run)
        _verify_output_tree(root, run, seal)
        return deepcopy(seal)


def validate_output_seal_record(
    run: dict[str, Any],
    value: object,
) -> None:
    """只检查 JSON 结构和内部摘要；现场文件由 verify_run_output_seal 检查。"""
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != _SEAL_FIELDS:
        raise ValueError("Run output_seal 字段集合无效")
    if value.get("schema") != OUTPUT_SEAL_SCHEMA:
        raise ValueError("Run output_seal schema 无效")
    snapshot = run.get("execution_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("output_seal 只能属于带执行快照的绑定 Run")
    expected_source = {
        "kind": OUTPUT_ROOT_KIND,
        "relative_path": snapshot.get("output_relative_path"),
    }
    expected_root = {
        "kind": SEALED_ROOT_KIND,
        "relative_path": f"{SEALED_ROOT_DIRECTORY}/{run.get('id')}",
    }
    if value.get("source") != expected_source:
        raise ValueError("Run output_seal 现场来源身份无效")
    if value.get("root") != expected_root:
        raise ValueError("Run output_seal 封存副本身份无效")
    if (
        value.get("execution_snapshot_sha256")
        != snapshot.get("snapshot_sha256")
        or value.get("frozen_digest") != run.get("frozen_digest")
        or value.get("finished_snapshot_sha256")
        != _finished_snapshot_sha256(run)
    ):
        raise ValueError("Run output_seal 与冻结运行身份不一致")

    raw_log = _validate_file_record(value.get("raw_log"), "output_seal.raw_log")
    artifacts_value = value.get("artifacts")
    if (
        not isinstance(artifacts_value, list)
        or not artifacts_value
        or len(artifacts_value) + 1 > MAX_OUTPUT_FILE_COUNT
    ):
        raise ValueError("Run output_seal artifacts 数量无效")
    artifacts = [
        _validate_file_record(item, f"output_seal.artifacts[{index}]")
        for index, item in enumerate(artifacts_value)
    ]
    records = [raw_log, *artifacts]
    normalized_paths = [
        _normalize_output_path(record["path"], "output_seal file path")
        for record in records
    ]
    if len({_windows_key(path) for path in normalized_paths}) != len(records):
        raise ValueError("Run output_seal 文件路径在 Windows 下重复")
    output_prefix = expected_source["relative_path"]
    if not isinstance(output_prefix, str):
        raise ValueError("Run output_seal 输出根路径无效")
    for path in normalized_paths:
        _require_inside_output(path, output_prefix)

    total_bytes = sum(record["size_bytes"] for record in records)
    if value.get("total_files") != len(records):
        raise ValueError("Run output_seal total_files 无效")
    if value.get("total_bytes") != total_bytes:
        raise ValueError("Run output_seal total_bytes 无效")
    if total_bytes > MAX_TOTAL_OUTPUT_BYTES:
        raise ValueError("Run output_seal 文件总大小超过上限")

    fingerprints = run.get("frozen", {}).get("code", {}).get("fingerprints", {})
    expected_comparability = {
        "data_fingerprint": fingerprints.get("data"),
        "evaluation_fingerprint": fingerprints.get("evaluation"),
    }
    if value.get("comparability") != expected_comparability or not all(
        isinstance(item, str) and _SHA256.fullmatch(item)
        for item in expected_comparability.values()
    ):
        raise ValueError("Run output_seal 可比性指纹无效")
    _timestamp(value.get("sealed_at"), "output_seal.sealed_at")
    expected_seal_sha256 = _canonical_sha256(
        {key: value[key] for key in sorted(_SEAL_FIELDS - {"seal_sha256"})}
    )
    if value.get("seal_sha256") != expected_seal_sha256:
        raise ValueError("Run output_seal 自身摘要无效")


def _require_sealable_run(run: dict[str, Any]) -> None:
    from .run_identity import is_bound_frozen

    execution = run.get("execution")
    data = run.get("frozen", {}).get("data", {})
    if not is_bound_frozen(run.get("frozen")):
        raise ValueError("只有绑定代码仓库的正式 Run 才能封存")
    if run.get("purpose") != "evidence":
        raise ValueError("只有论文 evidence 正式 Run 才能封存")
    if (
        not isinstance(execution, dict)
        or execution.get("stage") not in {"finished", "closed"}
        or execution.get("outcome") != "succeeded"
        or not all(value == "valid" for value in run.get("quality", {}).values())
    ):
        raise ValueError("只有成功完成且全部检查有效的正式 Run 才能封存")
    if (
        not isinstance(data, dict)
        or data.get("run_kind") != "real_experiment"
        or data.get("paper_eligible") is not True
    ):
        raise ValueError("调试或合成运行不能封存为论文正式证据")
    if run.get("result", {}).get("raw_log") is None:
        raise ValueError("正式输出封存必须包含 raw_log")
    artifacts = run.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("正式输出封存必须至少包含一个 artifact")


def _build_output_seal(
    execution_root: Path,
    run: dict[str, Any],
    sealed_root: Path,
) -> dict[str, Any]:
    output_relative = run["execution_snapshot"]["output_relative_path"]
    declared = [
        run["result"]["raw_log"],
        *run["artifacts"],
    ]
    normalized = [
        _normalize_output_path(path, f"Run output[{index}]")
        for index, path in enumerate(declared)
    ]
    if len({_windows_key(path) for path in normalized}) != len(normalized):
        raise ValueError("raw_log 与 artifacts 必须是不同的输出文件")
    if len(normalized) > MAX_OUTPUT_FILE_COUNT:
        raise ValueError("正式输出文件数量超过上限")
    for path in normalized:
        _require_inside_output(path, output_relative)

    observed = _scan_output_tree(execution_root, output_relative)
    declared_by_key = {_windows_key(path): path for path in normalized}
    observed_by_key = {
        _windows_key(item["path"]): item["path"] for item in observed["files"]
    }
    if declared_by_key != observed_by_key:
        raise ValueError("输出目录包含未声明、多余或缺失文件，不能封存")

    preflight_by_path = {
        item["path"]: item for item in observed["files"]
    }
    total_bytes = sum(item["size_bytes"] for item in preflight_by_path.values())
    if total_bytes > MAX_TOTAL_OUTPUT_BYTES:
        raise ValueError(
            f"正式输出文件总大小超过 {MAX_TOTAL_OUTPUT_BYTES} bytes 上限"
        )
    output_copy = sealed_root / output_relative
    output_copy.mkdir()
    records = [
        _copy_output_file(
            preflight_by_path[path],
            sealed_root,
        )
        for path in normalized
    ]
    _verify_preflight_files_unchanged(list(preflight_by_path.values()))
    _verify_records_match_current_tree(
        sealed_root,
        output_relative,
        records,
    )

    fingerprints = run["frozen"]["code"]["fingerprints"]
    seal: dict[str, Any] = {
        "schema": OUTPUT_SEAL_SCHEMA,
        "source": {
            "kind": OUTPUT_ROOT_KIND,
            "relative_path": output_relative,
        },
        "root": {
            "kind": SEALED_ROOT_KIND,
            "relative_path": f"{SEALED_ROOT_DIRECTORY}/{run['id']}",
        },
        "execution_snapshot_sha256": run["execution_snapshot"][
            "snapshot_sha256"
        ],
        "frozen_digest": run["frozen_digest"],
        "finished_snapshot_sha256": _finished_snapshot_sha256(run),
        "raw_log": records[0],
        "artifacts": records[1:],
        "total_files": len(records),
        "total_bytes": total_bytes,
        "comparability": {
            "data_fingerprint": fingerprints["data"],
            "evaluation_fingerprint": fingerprints["evaluation"],
        },
        "sealed_at": _utc_now(),
    }
    seal["seal_sha256"] = _canonical_sha256(seal)
    return seal


def _verify_output_tree(
    project_root: Path,
    run: dict[str, Any],
    seal: dict[str, Any],
) -> None:
    validate_output_seal_record(run, seal)
    sealed_root = _resolve_sealed_root(project_root, run, seal)
    records = [seal["raw_log"], *seal["artifacts"]]
    _verify_records_match_current_tree(
        sealed_root,
        seal["source"]["relative_path"],
        records,
    )


def _verify_records_match_current_tree(
    execution_root: Path,
    output_relative: str,
    records: list[dict[str, Any]],
) -> None:
    observed = _scan_output_tree(execution_root, output_relative)
    expected_by_key = {_windows_key(item["path"]): item for item in records}
    observed_by_key = {
        _windows_key(item["path"]): item for item in observed["files"]
    }
    if set(expected_by_key) != set(observed_by_key):
        raise ValueError("现场输出树存在未声明、多余或缺失文件")
    if len(records) != len(expected_by_key):
        raise ValueError("封存输出路径重复")
    if sum(item["size_bytes"] for item in observed["files"]) > MAX_TOTAL_OUTPUT_BYTES:
        raise ValueError("现场输出文件总大小超过上限")
    current_records = [
        _hash_output_file(observed_by_key[_windows_key(item["path"])])
        for item in records
    ]
    if current_records != records:
        raise ValueError("现场输出文件已变化，与封存记录不一致")
    _verify_preflight_files_unchanged(list(observed_by_key.values()))
    final = _scan_output_tree(execution_root, output_relative)
    final_by_key = {
        _windows_key(item["path"]): item for item in final["files"]
    }
    if set(final_by_key) != set(expected_by_key):
        raise ValueError("现场输出树在复验期间发生变化")
    final_records = [
        _hash_output_file(final_by_key[_windows_key(item["path"])])
        for item in records
    ]
    if final_records != records:
        raise ValueError("现场输出文件在复验期间发生变化")
    _verify_preflight_files_unchanged(list(final_by_key.values()))


def _scan_output_tree(
    execution_root: Path,
    output_relative: str,
) -> dict[str, Any]:
    output_path = execution_root / output_relative
    try:
        root_info = output_path.lstat()
    except OSError as error:
        raise ValueError("正式输出目录不存在") from error
    if is_link_or_reparse(output_path) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("正式输出目录必须是普通目录，不能是链接或 reparse")

    files: list[dict[str, Any]] = []
    directories: set[str] = set()
    windows_paths: dict[str, tuple[str, str]] = {}

    def visit(directory: Path) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(
                    iterator,
                    key=lambda item: (
                        unicodedata.normalize("NFKC", item.name).casefold()
                    ),
                )
        except OSError as error:
            raise ValueError("无法遍历正式输出目录") from error
        for entry in entries:
            path = Path(entry.path)
            try:
                info = path.lstat()
                relative = path.relative_to(execution_root).as_posix()
            except (OSError, ValueError) as error:
                raise ValueError("正式输出对象逃逸或无法读取") from error
            normalized = _normalize_output_path(relative, "现场输出路径")
            _require_inside_output(normalized, output_relative)
            if is_link_or_reparse(path):
                raise ValueError(f"正式输出包含链接或 reparse：{normalized}")
            if stat.S_ISDIR(info.st_mode):
                _register_windows_path(normalized, "directory", windows_paths)
                directories.add(normalized)
                visit(path)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError(f"正式输出必须是独占普通文件，拒绝硬链接：{normalized}")
            _register_windows_path(normalized, "file", windows_paths)
            if len(files) >= MAX_OUTPUT_FILE_COUNT:
                raise ValueError("正式输出文件数量超过上限")
            files.append(_preflight_output_file(path, normalized, info))

    visit(output_path)
    expected_directories = _expected_output_directories(
        [item["path"] for item in files],
        output_relative,
    )
    if directories != expected_directories:
        raise ValueError("输出目录包含未声明或空的多余目录")
    files.sort(key=lambda item: _windows_key(item["path"]))
    return {"files": files, "directories": sorted(directories)}


def _preflight_output_file(
    path: Path,
    relative: str,
    identity: os.stat_result,
) -> dict[str, Any]:
    if identity.st_size < 0 or identity.st_size > MAX_TOTAL_OUTPUT_BYTES:
        raise ValueError(f"正式输出文件大小超过上限：{relative}")
    return {
        "path": relative,
        "target": path,
        "identity": identity,
        "signature": _stable_file_signature(identity),
        "size_bytes": identity.st_size,
    }


def _copy_output_file(
    item: dict[str, Any],
    sealed_root: Path,
) -> dict[str, Any]:
    """稳定读取一次现场文件，并把这次读到的字节作为权威封存副本。"""
    source_path = item["target"]
    expected = item["identity"]
    destination = sealed_root / item["path"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    bytes_read = 0
    try:
        with source_path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or not os.path.samestat(expected, opened)
                or opened.st_size != item["size_bytes"]
            ):
                raise ValueError(f"输出文件在复制前发生变化：{item['path']}")
            with destination.open("xb") as target:
                remaining = item["size_bytes"]
                while remaining:
                    chunk = source.read(min(HASH_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    target.write(chunk)
                    digest.update(chunk)
                    bytes_read += len(chunk)
                    remaining -= len(chunk)
                if source.read(1):
                    raise ValueError(f"输出文件在复制期间变大：{item['path']}")
                target.flush()
                os.fsync(target.fileno())
            after_read = os.fstat(source.fileno())
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(f"无法复制正式输出文件：{item['path']}") from error
    try:
        current = source_path.lstat()
        copied = destination.lstat()
    except OSError as error:
        raise ValueError(f"正式输出文件复制后消失：{item['path']}") from error
    if (
        is_link_or_reparse(source_path)
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or not os.path.samestat(opened, after_read)
        or not os.path.samestat(expected, current)
        or after_read.st_size != item["size_bytes"]
        or current.st_size != item["size_bytes"]
        or bytes_read != item["size_bytes"]
        or _stable_file_signature(current) != item["signature"]
    ):
        raise ValueError(f"输出文件在复制期间发生变化：{item['path']}")
    if (
        is_link_or_reparse(destination)
        or not stat.S_ISREG(copied.st_mode)
        or copied.st_nlink != 1
        or copied.st_size != item["size_bytes"]
    ):
        raise ValueError(f"封存副本不是独占普通文件：{item['path']}")
    item["hashed_signature"] = _stable_file_signature(current)
    item["hashed_sha256"] = f"sha256:{digest.hexdigest()}"
    return {
        "path": item["path"],
        "size_bytes": item["size_bytes"],
        "sha256": item["hashed_sha256"],
    }


def _hash_output_file(item: dict[str, Any]) -> dict[str, Any]:
    target = item["target"]
    expected = item["identity"]
    digest = hashlib.sha256()
    bytes_read = 0
    try:
        with target.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or not os.path.samestat(expected, opened)
                or opened.st_size != item["size_bytes"]
            ):
                raise ValueError(f"输出文件在读取前发生变化：{item['path']}")
            remaining = item["size_bytes"]
            while remaining:
                chunk = source.read(min(HASH_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                bytes_read += len(chunk)
                remaining -= len(chunk)
            if source.read(1):
                raise ValueError(f"输出文件在读取期间变大：{item['path']}")
            after_read = os.fstat(source.fileno())
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(f"无法读取正式输出文件：{item['path']}") from error
    try:
        current = target.lstat()
    except OSError as error:
        raise ValueError(f"输出文件读取后消失：{item['path']}") from error
    if (
        is_link_or_reparse(target)
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or not os.path.samestat(opened, after_read)
        or not os.path.samestat(expected, current)
        or after_read.st_size != item["size_bytes"]
        or current.st_size != item["size_bytes"]
        or bytes_read != item["size_bytes"]
        or _stable_file_signature(current) != item["signature"]
    ):
        raise ValueError(f"输出文件在读取期间发生变化：{item['path']}")
    item["hashed_signature"] = _stable_file_signature(current)
    item["hashed_sha256"] = f"sha256:{digest.hexdigest()}"
    return {
        "path": item["path"],
        "size_bytes": item["size_bytes"],
        "sha256": item["hashed_sha256"],
    }


def _verify_preflight_files_unchanged(items: list[dict[str, Any]]) -> None:
    for item in items:
        target = item["target"]
        try:
            current = target.lstat()
        except OSError as error:
            raise ValueError(f"输出文件复查时消失：{item['path']}") from error
        if (
            is_link_or_reparse(target)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or not os.path.samestat(item["identity"], current)
            or _stable_file_signature(current) != item["signature"]
            or item.get("hashed_signature") != item["signature"]
        ):
            raise ValueError(f"输出文件在全部读取后发生变化：{item['path']}")


def _prepare_seal_container(project_root: Path) -> Path:
    container = project_root / SEALED_ROOT_DIRECTORY
    if not os.path.lexists(container):
        try:
            container.mkdir()
        except FileExistsError:
            pass
    try:
        info = container.lstat()
    except OSError as error:
        raise ValueError("无法创建正式输出封存根目录") from error
    if is_link_or_reparse(container) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("正式输出封存根必须是真实目录")
    return container


def _resolve_sealed_root(
    project_root: Path,
    run: dict[str, Any],
    seal: dict[str, Any],
) -> Path:
    expected = f"{SEALED_ROOT_DIRECTORY}/{run['id']}"
    if seal.get("root") != {
        "kind": SEALED_ROOT_KIND,
        "relative_path": expected,
    }:
        raise ValueError("Run output_seal 封存副本身份无效")
    container = project_root / SEALED_ROOT_DIRECTORY
    target = container / run["id"]
    for path, label in (
        (container, "正式输出封存根"),
        (target, "本次 Run 封存副本"),
    ):
        try:
            info = path.lstat()
        except OSError as error:
            raise ValueError(f"{label}不存在") from error
        if is_link_or_reparse(path) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"{label}必须是真实目录")
    return target


def _directory_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if is_link_or_reparse(path) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("封存暂存对象必须是真实目录")
    values = (getattr(info, "st_dev", None), getattr(info, "st_ino", None))
    if any(type(value) is not int for value in values):
        raise ValueError("平台缺少目录身份字段")
    return values  # type: ignore[return-value]


def _atomic_publish_directory_no_overwrite(
    source: Path,
    destination: Path,
) -> None:
    if os.name != "nt":
        raise ValueError("当前正式输出封存发布只支持 Windows")
    if os.path.lexists(destination):
        raise FileExistsError(destination)
    try:
        os.rename(source, destination)
    except OSError as error:
        raise ValueError("无法原子发布正式输出封存副本") from error


def _remove_owned_seal_tree(
    path: Path,
    *,
    expected_parent: Path,
    expected_identity: tuple[int, int],
    run_id: str,
) -> None:
    if not os.path.lexists(path):
        return
    if (
        path.parent != expected_parent
        or path.name not in {run_id}
        and not path.name.startswith(f".staging-{run_id}-")
        or is_link_or_reparse(path)
        or not path.is_dir()
        or _directory_identity(path) != expected_identity
    ):
        raise ValueError("封存回滚目标身份变化，已保留现场对象")
    shutil.rmtree(path)


def _validate_file_record(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _FILE_RECORD_FIELDS:
        raise ValueError(f"{label} 结构无效")
    path = _normalize_output_path(value.get("path"), f"{label}.path")
    size = value.get("size_bytes")
    digest = value.get("sha256")
    if type(size) is not int or size < 0 or size > MAX_TOTAL_OUTPUT_BYTES:
        raise ValueError(f"{label}.size_bytes 无效")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValueError(f"{label}.sha256 无效")
    return {"path": path, "size_bytes": size, "sha256": digest}


def _normalize_output_path(value: object, label: str) -> str:
    normalized = normalize_safe_relative_path(value, label)
    if unicodedata.normalize("NFKC", normalized) != normalized:
        raise ValueError(f"{label} 必须使用 NFKC 规范形式")
    if len(normalized.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES:
        raise ValueError(f"{label} 过长")
    for segment in normalized.split("/"):
        if (
            len(segment.encode("utf-8")) > MAX_PATH_SEGMENT_BYTES
            or any(unicodedata.category(char).startswith("C") for char in segment)
            or any(char in _WINDOWS_FORBIDDEN for char in segment)
            or segment.endswith((".", " "))
        ):
            raise ValueError(f"{label} 不兼容 Windows：{normalized}")
        basename = segment.split(".", 1)[0].casefold()
        if basename in _WINDOWS_RESERVED:
            raise ValueError(f"{label} 使用 Windows 保留名称：{normalized}")
    return normalized


def _require_inside_output(path: str, output_relative: str) -> None:
    normalized_root = _normalize_output_path(output_relative, "output root")
    if not path.startswith(normalized_root + "/"):
        raise ValueError("封存文件必须位于本次 Run 的固定输出目录")


def _register_windows_path(
    path: str,
    kind: str,
    observed: dict[str, tuple[str, str]],
) -> None:
    parts = path.split("/")
    for index in range(1, len(parts) + 1):
        relative = "/".join(parts[:index])
        current_kind = kind if index == len(parts) else "directory"
        key = _windows_key(relative)
        previous = observed.get(key)
        if previous is None:
            observed[key] = (current_kind, relative)
            continue
        if previous == (current_kind, relative) and current_kind == "directory":
            continue
        raise ValueError(
            f"正式输出在 Windows 下发生重名冲突：{previous[1]} / {relative}"
        )


def _expected_output_directories(
    paths: list[str],
    output_relative: str,
) -> set[str]:
    expected: set[str] = set()
    root_parts = output_relative.split("/")
    for path in paths:
        parts = path.split("/")
        for index in range(len(root_parts) + 1, len(parts)):
            expected.add("/".join(parts[:index]))
    return expected


def _windows_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).replace("\\", "/").casefold()


def _stable_file_signature(value: os.stat_result) -> tuple[int, ...]:
    fields = []
    for name in ("st_dev", "st_ino", "st_size", "st_nlink"):
        current = getattr(value, name, None)
        if type(current) is not int:
            raise ValueError(f"平台缺少稳定文件身份字段：{name}")
        fields.append(current)
    for nanosecond, seconds in (
        ("st_mtime_ns", "st_mtime"),
        ("st_ctime_ns", "st_ctime"),
    ):
        value_ns = getattr(value, nanosecond, None)
        if type(value_ns) is not int:
            value_seconds = getattr(value, seconds, None)
            if type(value_seconds) not in {int, float} or not math.isfinite(
                value_seconds
            ):
                raise ValueError(f"平台缺少稳定文件时间字段：{nanosecond}")
            value_ns = int(value_seconds * 1_000_000_000)
        fields.append(value_ns)
    attributes = getattr(value, "st_file_attributes", None)
    if type(attributes) is int:
        fields.append(attributes)
    return tuple(fields)


def _finished_snapshot_sha256(run: dict[str, Any]) -> str:
    execution = run.get("execution", {})
    projection = {
        "id": run.get("id"),
        "task_id": run.get("task_id"),
        "purpose": run.get("purpose"),
        "frozen_digest": run.get("frozen_digest"),
        "execution": {
            key: execution.get(key)
            for key in (
                "outcome",
                "started_at",
                "finished_at",
                "process_id",
                "exit_code",
                "issue_kind",
            )
        },
        "result": run.get("result"),
        "quality": run.get("quality"),
        "analysis": run.get("analysis"),
        "artifacts": run.get("artifacts"),
        "execution_snapshot": run.get("execution_snapshot"),
    }
    return _canonical_sha256(projection)


def _canonical_sha256(value: object) -> str:
    content = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _timestamp(value: object, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} 无效")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} 无效") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} 必须包含时区")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
