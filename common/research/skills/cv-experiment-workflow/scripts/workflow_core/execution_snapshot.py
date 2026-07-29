from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .codebases import load_codebase_locked
from .git_safe import run_git_bounded
from .io import atomic_create_bytes_clean, read_bounded_json_object
from .locking import project_snapshot_lock
from .project import PROJECT_SCHEMA_V2
from .project import is_link_or_reparse
from .records import _initialized_project, _validate_project


EXECUTION_SNAPSHOT_SCHEMA = "cv-experiment-workflow.execution-snapshot.v1"
EXECUTION_SNAPSHOT_FIELDS = {
    "schema",
    "root",
    "commit",
    "relative_path",
    "manifest_path",
    "snapshot_sha256",
    "output_relative_path",
}
EXECUTION_SNAPSHOT_ROOT_FIELDS = {"kind", "codebase_id"}
EXECUTION_SNAPSHOT_ROOT_KIND = "run_local_git_commit"
SNAPSHOT_MANIFEST_SCHEMA = "cv-experiment-workflow.execution-snapshot-manifest.v1"
SNAPSHOT_MANIFEST_FIELDS = {"schema", "root", "commit", "files"}
SNAPSHOT_FILE_FIELDS = {"path", "git_mode", "size_bytes", "sha256"}
RUN_OUTPUT_RELATIVE_PATH = ".cv-workflow-output"
MAX_SNAPSHOT_FILES = 20_000
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 128 * 1024 * 1024
MAX_SNAPSHOT_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_RELATIVE_PATH_BYTES = 1024
MAX_PATH_SEGMENT_BYTES = 255
MAX_GIT_TREE_OUTPUT_BYTES = 32 * 1024 * 1024
_COMMIT = re.compile(r"[0-9a-f]{40}")
_OBJECT_ID = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_RUN_ID = re.compile(r"RUN-[0-9]{4}")
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)


class ExecutionSnapshotError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def create_run_execution_snapshot_locked(
    control: Path,
    run_id: str,
    frozen: dict[str, Any],
) -> dict[str, Any]:
    _validate_snapshot_run_id(run_id)
    code = frozen["code"]
    codebase = load_codebase_locked(control, code["codebase_id"])
    repository = Path(codebase["repo_path"])
    commit = code["commit"]
    run_dir = control / "runs" / run_id
    if not run_dir.is_dir() or is_link_or_reparse(run_dir):
        raise ExecutionSnapshotError(
            "execution_snapshot_parent_invalid",
            "Run 目录必须先以真实目录创建",
        )
    final_root = run_dir / "execution_snapshot"
    final_manifest = run_dir / "execution_snapshot.manifest.json"
    if os.path.lexists(final_root) or os.path.lexists(final_manifest):
        raise ExecutionSnapshotError(
            "execution_snapshot_exists",
            "执行快照目标已存在，拒绝覆盖",
        )
    staging = run_dir / f".execution-snapshot-{uuid.uuid4().hex}"
    staging.mkdir()
    staging_identity = _file_identity(staging.lstat())
    published = False
    manifest_created = False
    manifest_signature: tuple[int, int, int, int, str] | None = None
    try:
        manifest = bind_manifest_codebase(
            _materialize_commit(repository, commit, staging),
            code["codebase_id"],
        )
        snapshot_sha256 = _canonical_digest(manifest)
        output_dir = staging / RUN_OUTPUT_RELATIVE_PATH
        output_dir.mkdir()
        _seal_snapshot_permissions(staging, manifest)
        _atomic_publish_directory_no_overwrite(staging, final_root)
        published = True
        if not _atomic_create_snapshot_manifest(
            final_manifest,
            manifest,
            transaction_id=uuid.uuid4().hex,
        ):
            raise ExecutionSnapshotError(
                "execution_snapshot_manifest_exists",
                "执行快照 manifest 已存在，拒绝覆盖",
            )
        manifest_created = True
        manifest_signature = _owned_file_signature(final_manifest)
        record = {
            "schema": EXECUTION_SNAPSHOT_SCHEMA,
            "root": {
                "kind": EXECUTION_SNAPSHOT_ROOT_KIND,
                "codebase_id": code["codebase_id"],
            },
            "commit": commit,
            "relative_path": f"runs/{run_id}/execution_snapshot",
            "manifest_path": (
                f"runs/{run_id}/execution_snapshot.manifest.json"
            ),
            "snapshot_sha256": snapshot_sha256,
            "output_relative_path": RUN_OUTPUT_RELATIVE_PATH,
        }
        validate_execution_snapshot_record(
            run_id,
            frozen,
            record,
        )
        return record
    except BaseException:
        if published:
            _remove_owned_tree(
                final_root,
                expected_parent=run_dir,
                expected_identity=staging_identity,
            )
            if (
                manifest_created
                and manifest_signature is not None
                and os.path.lexists(final_manifest)
                and not is_link_or_reparse(final_manifest)
                and _matches_owned_file_signature(
                    final_manifest,
                    manifest_signature,
                )
            ):
                final_manifest.unlink()
        else:
            _remove_owned_tree(
                staging,
                expected_parent=run_dir,
                expected_identity=staging_identity,
            )
        raise


def remove_run_execution_snapshot_locked(
    control: Path,
    run_id: str,
) -> None:
    """只清理由本次 Run 创建、名称固定的快照对象；不接收任意删除路径。"""
    _validate_snapshot_run_id(run_id)
    run_dir = control / "runs" / run_id
    snapshot = run_dir / "execution_snapshot"
    manifest = run_dir / "execution_snapshot.manifest.json"
    identity = (
        _file_identity(snapshot.lstat())
        if os.path.lexists(snapshot) and not is_link_or_reparse(snapshot)
        else None
    )
    if (
        identity is None
        or not os.path.lexists(manifest)
        or is_link_or_reparse(manifest)
        or not manifest.is_file()
    ):
        raise ExecutionSnapshotError(
            "execution_snapshot_cleanup_refused",
            "Owned snapshot cleanup objects are incomplete or unsafe",
        )
    manifest_signature = _owned_file_signature(manifest)
    _remove_owned_tree(
        snapshot,
        expected_parent=run_dir,
        expected_identity=identity,
    )
    if (
        not os.path.lexists(manifest)
        or is_link_or_reparse(manifest)
        or _owned_file_signature(manifest) != manifest_signature
    ):
        raise ExecutionSnapshotError(
            "execution_snapshot_cleanup_identity_changed",
            "Snapshot manifest identity changed; current object was preserved",
        )
    manifest.unlink()


@contextmanager
def temporary_commit_snapshot(
    repository: Path,
    commit: str,
) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="cv-workflow-snapshot-") as directory:
        root = Path(directory) / "execution_snapshot"
        root.mkdir()
        manifest = _materialize_commit(repository, commit, root)
        (root / RUN_OUTPUT_RELATIVE_PATH).mkdir()
        _seal_snapshot_permissions(root, manifest)
        manifest_path = root.parent / "snapshot.manifest.json"
        if not _atomic_create_snapshot_manifest(
            manifest_path,
            manifest,
            transaction_id=uuid.uuid4().hex,
        ):
            raise ExecutionSnapshotError(
                "execution_snapshot_manifest_exists",
                "临时执行快照 manifest 已存在",
            )
        verify_temporary_execution_snapshot(root)
        completed = False
        try:
            yield root
            completed = True
        finally:
            try:
                if completed:
                    verify_temporary_execution_snapshot(root)
            finally:
                _make_owned_snapshot_writable(root)


def verify_temporary_execution_snapshot(snapshot_root: Path) -> None:
    root = snapshot_root.resolve(strict=True)
    _require_read_only_directory(root, "temporary execution snapshot root")
    _require_real_directory(root, "临时执行快照根目录")
    manifest_path = root.parent / "snapshot.manifest.json"
    manifest = read_bounded_json_object(
        manifest_path,
        limit=MAX_SNAPSHOT_MANIFEST_BYTES,
        label="temporary execution snapshot manifest",
    )
    if (
        not isinstance(manifest, dict)
        or set(manifest) != SNAPSHOT_MANIFEST_FIELDS
        or manifest.get("schema") != SNAPSHOT_MANIFEST_SCHEMA
        or manifest.get("root")
        != {
            "kind": EXECUTION_SNAPSHOT_ROOT_KIND,
            "codebase_id": None,
        }
        or not isinstance(manifest.get("commit"), str)
        or _COMMIT.fullmatch(manifest["commit"]) is None
        or not isinstance(manifest.get("files"), list)
    ):
        raise ExecutionSnapshotError(
            "execution_snapshot_manifest_invalid",
            "临时执行快照 manifest 无效",
        )
    _validate_manifest(
        manifest,
        codebase_id=None,
        commit=manifest["commit"],
    )
    _verify_snapshot_files(root, manifest["files"])


def validate_execution_snapshot_record(
    run_id: str,
    frozen: dict[str, Any],
    value: object,
) -> None:
    code = frozen.get("code") if isinstance(frozen, dict) else None
    if (
        not isinstance(code, dict)
        or not isinstance(value, dict)
        or set(value) != EXECUTION_SNAPSHOT_FIELDS
        or value.get("schema") != EXECUTION_SNAPSHOT_SCHEMA
    ):
        raise ValueError("Run execution_snapshot 字段无效")
    root = value["root"]
    if (
        not isinstance(root, dict)
        or set(root) != EXECUTION_SNAPSHOT_ROOT_FIELDS
        or root.get("kind") != EXECUTION_SNAPSHOT_ROOT_KIND
        or root.get("codebase_id") != code.get("codebase_id")
        or value.get("commit") != code.get("commit")
        or value.get("relative_path")
        != f"runs/{run_id}/execution_snapshot"
        or value.get("manifest_path")
        != f"runs/{run_id}/execution_snapshot.manifest.json"
        or value.get("output_relative_path") != RUN_OUTPUT_RELATIVE_PATH
        or not isinstance(value.get("snapshot_sha256"), str)
        or _SHA256.fullmatch(value["snapshot_sha256"]) is None
    ):
        raise ValueError("Run execution_snapshot 身份无效")


def verify_run_execution_snapshot(
    control: Path,
    run: dict[str, Any],
) -> Path:
    record = run.get("execution_snapshot")
    validate_execution_snapshot_record(
        run["id"],
        run["frozen"],
        record,
    )
    assert isinstance(record, dict)
    root = _resolve_control_relative(
        control,
        record["relative_path"],
        directory=True,
    )
    _require_read_only_directory(root, "execution snapshot root")
    manifest_path = _resolve_control_relative(
        control,
        record["manifest_path"],
        directory=False,
    )
    manifest = read_bounded_json_object(
        manifest_path,
        limit=MAX_SNAPSHOT_MANIFEST_BYTES,
        label="execution snapshot manifest",
    )
    _validate_manifest(
        manifest,
        codebase_id=record["root"]["codebase_id"],
        commit=record["commit"],
    )
    if _canonical_digest(manifest) != record["snapshot_sha256"]:
        raise ExecutionSnapshotError(
            "execution_snapshot_digest_mismatch",
            "执行快照 manifest 摘要与 Run 不一致",
        )
    expected = {
        item["path"]: item
        for item in manifest["files"]
    }
    expected_directories = _expected_source_directories(expected)
    observed_paths: list[str] = []
    output_root = root / RUN_OUTPUT_RELATIVE_PATH
    _require_writable_output_directory(output_root)
    _require_real_directory(output_root, "执行输出目录")
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as error:
            raise ExecutionSnapshotError(
                "execution_snapshot_escape",
                "执行快照对象逃逸出根目录",
            ) from error
        if (
            relative == RUN_OUTPUT_RELATIVE_PATH
            or relative.startswith(RUN_OUTPUT_RELATIVE_PATH + "/")
        ):
            continue
        if is_link_or_reparse(path):
            raise ExecutionSnapshotError(
                "execution_snapshot_link",
                f"执行快照包含 link/reparse：{relative}",
            )
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            if relative not in expected_directories:
                raise ExecutionSnapshotError(
                    "execution_snapshot_extra_directory",
                    f"Execution snapshot contains an extra source directory: {relative}",
                )
            _require_read_only_directory(
                path,
                "execution snapshot source directory",
            )
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            raise ExecutionSnapshotError(
                "execution_snapshot_special_file",
                f"执行快照包含特殊文件或硬链接：{relative}",
            )
        observed_paths.append(relative)
        expected_item = expected.get(relative)
        if expected_item is None:
            raise ExecutionSnapshotError(
                "execution_snapshot_extra_file",
                f"执行快照出现额外源码文件：{relative}",
            )
        actual = _stable_file_record(
            path,
            relative,
            expected_item["git_mode"],
        )
        if actual != expected_item:
            raise ExecutionSnapshotError(
                "execution_snapshot_digest_mismatch",
                f"执行快照文件发生变化：{relative}",
            )
    if observed_paths != sorted(expected):
        missing = sorted(set(expected) - set(observed_paths))
        raise ExecutionSnapshotError(
            "execution_snapshot_missing_file",
            "执行快照缺少源码文件：" + ", ".join(missing[:8]),
        )
    return root


def _verify_snapshot_files(
    root: Path,
    files: list[dict[str, Any]],
) -> None:
    expected = {item["path"]: item for item in files}
    expected_directories = _expected_source_directories(expected)
    observed_paths: list[str] = []
    output_root = root / RUN_OUTPUT_RELATIVE_PATH
    _require_writable_output_directory(output_root)
    _require_real_directory(output_root, "执行输出目录")
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if (
            relative == RUN_OUTPUT_RELATIVE_PATH
            or relative.startswith(RUN_OUTPUT_RELATIVE_PATH + "/")
        ):
            continue
        if is_link_or_reparse(path):
            raise ExecutionSnapshotError(
                "execution_snapshot_link",
                f"执行快照包含 link/reparse：{relative}",
            )
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            if relative not in expected_directories:
                raise ExecutionSnapshotError(
                    "execution_snapshot_extra_directory",
                    f"Execution snapshot contains an extra source directory: {relative}",
                )
            _require_read_only_directory(
                path,
                "execution snapshot source directory",
            )
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ExecutionSnapshotError(
                "execution_snapshot_special_file",
                f"执行快照包含特殊文件或硬链接：{relative}",
            )
        observed_paths.append(relative)
        expected_item = expected.get(relative)
        if expected_item is None:
            raise ExecutionSnapshotError(
                "execution_snapshot_extra_file",
                f"执行快照出现额外源码文件：{relative}",
            )
        if _stable_file_record(
            path,
            relative,
            expected_item["git_mode"],
        ) != expected_item:
            raise ExecutionSnapshotError(
                "execution_snapshot_digest_mismatch",
                f"执行快照文件发生变化：{relative}",
            )
    if observed_paths != sorted(expected):
        raise ExecutionSnapshotError(
            "execution_snapshot_missing_file",
            "执行快照缺少源码文件",
        )


def verify_project_run_execution_snapshot(
    project: Path,
    run_id: str,
) -> Path:
    from .runs import _load_run_locked, _run_id

    _run_id(run_id)
    root = _initialized_project(project)
    with project_snapshot_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        run = _load_run_locked(control, run_id)
        return verify_run_execution_snapshot(control, run)


def load_snapshot_adapter_spec(
    snapshot_root: Path,
    commit: str,
) -> dict[str, Any]:
    if _COMMIT.fullmatch(commit) is None:
        raise ValueError("执行快照 commit 无效")
    root = snapshot_root.resolve(strict=True)
    _require_real_directory(root, "执行快照根目录")
    candidate = root / "workflow_adapter.py"
    record, source_bytes = _stable_file_read(
        candidate,
        "workflow_adapter.py",
        "100644",
    )
    return {
        "source_bytes": source_bytes,
        "source": "workflow_adapter.py",
        "display_path": str(candidate),
        "repo_url": str(root),
        "commit": commit,
        "import_root": str(root),
        "sha256": record["sha256"].removeprefix("sha256:"),
    }


def _materialize_commit(
    repository: Path,
    commit: str,
    target: Path,
) -> dict[str, Any]:
    if _COMMIT.fullmatch(commit) is None:
        raise ExecutionSnapshotError(
            "execution_snapshot_commit_invalid",
            "commit 必须是 40 位小写 Git 对象 ID",
        )
    _require_real_directory(repository, "Codebase 仓库")
    _require_real_directory(target, "执行快照 staging")
    entries = _git_tree_entries(repository, commit)
    if not entries or not any(
        item["path"] == "workflow_adapter.py" for item in entries
    ):
        raise ExecutionSnapshotError(
            "execution_snapshot_adapter_missing",
            "绑定 commit 根目录缺少 workflow_adapter.py",
        )
    files: list[dict[str, Any]] = []
    for entry in entries:
        relative = entry["path"]
        destination = target.joinpath(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        blob = _git_bytes(
            repository,
            "cat-file",
            "blob",
            entry["object_id"],
            max_output_bytes=entry["size_bytes"],
        )
        if len(blob) != entry["size_bytes"]:
            raise ExecutionSnapshotError(
                "execution_snapshot_blob_size_mismatch",
                f"Git blob 大小不一致：{relative}",
            )
        with destination.open("xb") as output:
            output.write(blob)
            output.flush()
            os.fsync(output.fileno())
        if os.name != "nt":
            os.chmod(
                destination,
                0o755 if entry["git_mode"] == "100755" else 0o644,
            )
        files.append(
            {
                "path": relative,
                "git_mode": entry["git_mode"],
                "size_bytes": len(blob),
                "sha256": "sha256:" + hashlib.sha256(blob).hexdigest(),
            }
        )
    manifest = {
        "schema": SNAPSHOT_MANIFEST_SCHEMA,
        "root": {
            "kind": EXECUTION_SNAPSHOT_ROOT_KIND,
            "codebase_id": None,
        },
        "commit": commit,
        "files": files,
    }
    return manifest


def bind_manifest_codebase(
    manifest: dict[str, Any],
    codebase_id: str,
) -> dict[str, Any]:
    manifest["root"]["codebase_id"] = codebase_id
    return manifest


def _git_tree_entries(
    repository: Path,
    commit: str,
) -> list[dict[str, Any]]:
    raw = _git_bytes(
        repository,
        "ls-tree",
        "-r",
        "-l",
        "-z",
        "--full-tree",
        commit,
        max_output_bytes=MAX_GIT_TREE_OUTPUT_BYTES,
    )
    entries: list[dict[str, Any]] = []
    windows_paths: dict[str, tuple[str, str]] = {}
    total = 0
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            parts = metadata.decode("ascii").split()
            path = raw_path.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as error:
            raise ExecutionSnapshotError(
                "execution_snapshot_tree_invalid",
                "Git tree 记录无法安全解析",
            ) from error
        if len(parts) != 4:
            raise ExecutionSnapshotError(
                "execution_snapshot_tree_invalid",
                "Git tree 元数据字段数量无效",
            )
        mode, object_type, object_id, size_text = parts
        if mode not in {"100644", "100755"} or object_type != "blob":
            raise ExecutionSnapshotError(
                "execution_snapshot_unsupported_git_object",
                f"拒绝 symlink/submodule/special Git 对象：{path}",
            )
        if _OBJECT_ID.fullmatch(object_id) is None:
            raise ExecutionSnapshotError(
                "execution_snapshot_object_invalid",
                f"Git blob ID 无效：{path}",
            )
        try:
            size = int(size_text)
        except ValueError as error:
            raise ExecutionSnapshotError(
                "execution_snapshot_size_invalid",
                f"Git blob 大小无效：{path}",
            ) from error
        if size < 0 or size > MAX_SINGLE_FILE_BYTES:
            raise ExecutionSnapshotError(
                "execution_snapshot_file_too_large",
                f"Git blob 超过单文件上限：{path}",
            )
        normalized = _normalize_git_path(path)
        _register_windows_tree_path(normalized, windows_paths)
        total += size
        if (
            len(entries) + 1 > MAX_SNAPSHOT_FILES
            or total > MAX_SNAPSHOT_BYTES
        ):
            raise ExecutionSnapshotError(
                "execution_snapshot_budget_exceeded",
                "Git 执行快照超过文件数或总大小上限",
            )
        entries.append(
            {
                "path": normalized,
                "git_mode": mode,
                "object_id": object_id,
                "size_bytes": size,
            }
        )
    return sorted(entries, key=lambda item: item["path"])


def _validate_manifest(
    manifest: object,
    *,
    codebase_id: str | None,
    commit: str,
) -> None:
    if (
        not isinstance(manifest, dict)
        or set(manifest) != SNAPSHOT_MANIFEST_FIELDS
        or manifest.get("schema") != SNAPSHOT_MANIFEST_SCHEMA
        or manifest.get("commit") != commit
        or manifest.get("root")
        != {
            "kind": EXECUTION_SNAPSHOT_ROOT_KIND,
            "codebase_id": codebase_id,
        }
        or not isinstance(manifest.get("files"), list)
        or not manifest["files"]
    ):
        raise ExecutionSnapshotError(
            "execution_snapshot_manifest_invalid",
            "执行快照 manifest 结构无效",
        )
    paths: list[str] = []
    total = 0
    for item in manifest["files"]:
        if (
            not isinstance(item, dict)
            or set(item) != SNAPSHOT_FILE_FIELDS
            or item.get("path") != _normalize_git_path(item.get("path"))
            or item.get("git_mode") not in {"100644", "100755"}
            or type(item.get("size_bytes")) is not int
            or item["size_bytes"] < 0
            or not isinstance(item.get("sha256"), str)
            or _SHA256.fullmatch(item["sha256"]) is None
        ):
            raise ExecutionSnapshotError(
                "execution_snapshot_manifest_invalid",
                "执行快照文件记录无效",
            )
        paths.append(item["path"])
        total += item["size_bytes"]
    if (
        paths != sorted(paths)
        or len(paths) > MAX_SNAPSHOT_FILES
        or total > MAX_SNAPSHOT_BYTES
    ):
        raise ExecutionSnapshotError(
            "execution_snapshot_manifest_invalid",
            "执行快照 manifest 顺序、去重或预算无效",
        )
    _validate_windows_tree_paths(paths)


def _expected_source_directories(
    files: dict[str, dict[str, Any]],
) -> set[str]:
    expected: set[str] = set()
    for relative in files:
        parent = Path(relative).parent
        while parent != Path("."):
            expected.add(parent.as_posix())
            parent = parent.parent
    return expected


def _seal_snapshot_permissions(
    root: Path,
    manifest: dict[str, Any],
) -> None:
    for item in manifest["files"]:
        path = root.joinpath(*item["path"].split("/"))
        mode = 0o555 if item["git_mode"] == "100755" else 0o444
        os.chmod(path, mode)
    source_directories = sorted(
        _expected_source_directories(
            {item["path"]: item for item in manifest["files"]}
        ),
        key=lambda value: value.count("/"),
        reverse=True,
    )
    for relative in source_directories:
        os.chmod(root.joinpath(*relative.split("/")), 0o555)
    os.chmod(root, 0o555)
    os.chmod(root / RUN_OUTPUT_RELATIVE_PATH, 0o755)


def _make_owned_snapshot_writable(root: Path) -> None:
    if not os.path.lexists(root) or is_link_or_reparse(root):
        return
    for path in sorted(
        root.rglob("*"),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if is_link_or_reparse(path):
            continue
        try:
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        except OSError:
            pass
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass


def _stable_file_record(
    path: Path,
    relative: str,
    git_mode: str,
) -> dict[str, Any]:
    record, _content = _stable_file_read(
        path,
        relative,
        git_mode,
        capture_content=False,
    )
    return record


def _stable_file_read(
    path: Path,
    relative: str,
    git_mode: str,
    *,
    capture_content: bool = True,
) -> tuple[dict[str, Any], bytes]:
    try:
        before = path.lstat()
    except OSError as error:
        raise ExecutionSnapshotError(
            "execution_snapshot_missing_file",
            f"执行快照文件不存在：{relative}",
        ) from error
    if (
        is_link_or_reparse(path)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise ExecutionSnapshotError(
            "execution_snapshot_special_file",
            f"执行快照文件不是独立普通文件：{relative}",
        )
    write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    if before.st_mode & write_bits:
        raise ExecutionSnapshotError(
            "execution_snapshot_source_writable",
            f"Execution snapshot source is writable: {relative}",
        )
    if os.name != "nt":
        execute_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        expected_execute = execute_bits if git_mode == "100755" else 0
        if before.st_mode & execute_bits != expected_execute:
            raise ExecutionSnapshotError(
                "execution_snapshot_mode_mismatch",
                f"Execution snapshot mode changed: {relative}",
            )
    digest = hashlib.sha256()
    size = 0
    content = bytearray() if capture_content else None
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if not os.path.samestat(before, opened):
            raise ExecutionSnapshotError(
                "execution_snapshot_race",
                f"执行快照文件在打开前被替换：{relative}",
            )
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
            if content is not None:
                content.extend(chunk)
        after_handle = os.fstat(source.fileno())
    after_path = path.lstat()
    for observed in (after_handle, after_path):
        if observed.st_mode & write_bits:
            raise ExecutionSnapshotError(
                "execution_snapshot_source_writable",
                f"Execution snapshot source became writable: {relative}",
            )
        if os.name != "nt":
            execute_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            expected_execute = execute_bits if git_mode == "100755" else 0
            if observed.st_mode & execute_bits != expected_execute:
                raise ExecutionSnapshotError(
                    "execution_snapshot_mode_mismatch",
                    f"Execution snapshot mode changed: {relative}",
                )
    if (
        not os.path.samestat(opened, after_handle)
        or not os.path.samestat(before, after_path)
        or before.st_size != size
        or after_handle.st_size != size
        or after_path.st_size != size
        or before.st_mtime_ns != after_handle.st_mtime_ns
        or before.st_mtime_ns != after_path.st_mtime_ns
    ):
        raise ExecutionSnapshotError(
            "execution_snapshot_race",
            f"执行快照文件在读取期间发生变化：{relative}",
        )
    return (
        {
            "path": relative,
            "git_mode": git_mode,
            "size_bytes": size,
            "sha256": "sha256:" + digest.hexdigest(),
        },
        bytes(content or b""),
    )


def _normalize_git_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value) is not None
    ):
        raise ExecutionSnapshotError(
            "execution_snapshot_path_invalid",
            "Git tree 路径不是规范相对路径",
        )
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ExecutionSnapshotError(
            "execution_snapshot_path_invalid",
            "Git tree 路径包含空段、. 或 ..",
        )
    if len(value.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES:
        raise ExecutionSnapshotError(
            "execution_snapshot_path_too_long",
            "Git tree path exceeds the UTF-8 length budget",
        )
    for segment in segments:
        if len(segment.encode("utf-8")) > MAX_PATH_SEGMENT_BYTES:
            raise ExecutionSnapshotError(
                "execution_snapshot_path_too_long",
                f"Git tree path segment exceeds the UTF-8 length budget: {value}",
            )
        if (
            any(unicodedata.category(char).startswith("C") for char in segment)
            or any(char in _WINDOWS_FORBIDDEN for char in segment)
            or segment.endswith((".", " "))
        ):
            raise ExecutionSnapshotError(
                "execution_snapshot_path_invalid",
                f"Git tree 路径不兼容 Windows：{value}",
            )
        basename = unicodedata.normalize("NFKC", segment).split(".", 1)[0].casefold()
        if basename in _WINDOWS_RESERVED:
            raise ExecutionSnapshotError(
                "execution_snapshot_path_invalid",
                f"Git tree 使用 Windows 保留名：{value}",
            )
    path_key = _windows_key(value)
    output_key = _windows_key(RUN_OUTPUT_RELATIVE_PATH)
    if path_key == output_key or path_key.startswith(output_key + "/"):
        raise ExecutionSnapshotError(
            "execution_snapshot_output_collision",
            "Git tree 占用了固定运行输出目录",
        )
    return value


def _windows_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _validate_windows_tree_paths(paths: list[str]) -> None:
    observed: dict[str, tuple[str, str]] = {}
    for path in paths:
        _register_windows_tree_path(path, observed)


def _register_windows_tree_path(
    path: str,
    observed: dict[str, tuple[str, str]],
) -> None:
    segments = path.split("/")
    for index in range(1, len(segments) + 1):
        relative = "/".join(segments[:index])
        kind = "file" if index == len(segments) else "directory"
        key = _windows_key(relative)
        previous = observed.get(key)
        if previous is None:
            observed[key] = (kind, relative)
            continue
        if previous == (kind, relative) and kind == "directory":
            continue
        raise ExecutionSnapshotError(
            "execution_snapshot_windows_collision",
            "Git tree 在 Windows 上发生文件、目录或大小写冲突："
            f"{previous[1]} / {relative}",
        )


def _resolve_control_relative(
    control: Path,
    relative: str,
    *,
    directory: bool,
) -> Path:
    normalized = _normalize_control_relative(relative)
    current = control.resolve(strict=True)
    segments = normalized.split("/")
    for index, segment in enumerate(segments):
        current = current / segment
        try:
            info = current.lstat()
        except OSError as error:
            raise ExecutionSnapshotError(
                "execution_snapshot_path_missing",
                f"执行快照本机路径不存在：{relative}",
            ) from error
        if is_link_or_reparse(current):
            raise ExecutionSnapshotError(
                "execution_snapshot_path_link",
                f"执行快照本机路径包含 link/reparse：{relative}",
            )
        if index < len(segments) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ExecutionSnapshotError(
                "execution_snapshot_path_invalid",
                f"执行快照父路径不是目录：{relative}",
            )
    info = current.lstat()
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected:
        raise ExecutionSnapshotError(
            "execution_snapshot_path_invalid",
            f"执行快照本机路径类型无效：{relative}",
        )
    return current


def _normalize_control_relative(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("execution snapshot 本机相对路径无效")
    return value


def _require_writable_output_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise ExecutionSnapshotError(
            "execution_snapshot_output_invalid",
            "Execution snapshot output directory is missing",
        ) from error
    if (
        is_link_or_reparse(path)
        or not stat.S_ISDIR(info.st_mode)
        or not info.st_mode & stat.S_IWUSR
    ):
        raise ExecutionSnapshotError(
            "execution_snapshot_output_not_writable",
            "Execution snapshot output directory must be writable",
        )


def _require_read_only_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise ExecutionSnapshotError(
            "execution_snapshot_directory_invalid",
            f"{label} is missing",
        ) from error
    write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    if (
        is_link_or_reparse(path)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_mode & write_bits
    ):
        raise ExecutionSnapshotError(
            "execution_snapshot_source_directory_writable",
            f"{label} must be a read-only real directory",
        )


def _require_real_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise ExecutionSnapshotError(
            "execution_snapshot_directory_invalid",
            f"{label}不存在",
        ) from error
    if is_link_or_reparse(path) or not stat.S_ISDIR(info.st_mode):
        raise ExecutionSnapshotError(
            "execution_snapshot_directory_invalid",
            f"{label}必须是真实目录",
        )


def _git_bytes(
    repository: Path,
    *arguments: str,
    max_output_bytes: int,
) -> bytes:
    try:
        result = run_git_bounded(
            repository,
            *arguments,
            stdout_limit=max_output_bytes,
            timeout=60,
        )
    except ValueError as error:
        raise ExecutionSnapshotError(
            "execution_snapshot_git_output_invalid",
            str(error),
        ) from error
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise ExecutionSnapshotError(
            "execution_snapshot_git_failed",
            message or f"Git {arguments[0]} 失败",
        )
    return bytes(result.stdout)


def _canonical_digest(value: object) -> str:
    content = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _atomic_create_snapshot_manifest(
    path: Path,
    manifest: dict[str, Any],
    *,
    transaction_id: str,
) -> bool:
    content = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(content) > MAX_SNAPSHOT_MANIFEST_BYTES:
        raise ExecutionSnapshotError(
            "execution_snapshot_manifest_too_large",
            "Execution snapshot manifest exceeds its bounded size limit",
        )
    return atomic_create_bytes_clean(
        path,
        content,
        transaction_id=transaction_id,
        operation="execution snapshot manifest create",
    )


def _validate_snapshot_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ExecutionSnapshotError(
            "execution_snapshot_run_id_invalid",
            "Execution snapshot Run ID is invalid",
        )
    return run_id


def _file_identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _owned_file_signature(
    path: Path,
) -> tuple[int, int, int, int, str]:
    try:
        before = path.lstat()
    except OSError as error:
        raise ExecutionSnapshotError(
            "execution_snapshot_cleanup_identity_changed",
            "Snapshot manifest disappeared before cleanup completed",
        ) from error
    if (
        is_link_or_reparse(path)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > MAX_SNAPSHOT_MANIFEST_BYTES
    ):
        raise ExecutionSnapshotError(
            "execution_snapshot_cleanup_refused",
            "Snapshot manifest is not a bounded owned regular file",
        )
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if not os.path.samestat(before, opened):
            raise ExecutionSnapshotError(
                "execution_snapshot_cleanup_identity_changed",
                "Snapshot manifest identity changed while opening",
            )
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_SNAPSHOT_MANIFEST_BYTES:
                raise ExecutionSnapshotError(
                    "execution_snapshot_cleanup_refused",
                    "Snapshot manifest exceeded its cleanup size limit",
                )
            digest.update(chunk)
        after_handle = os.fstat(source.fileno())
    after_path = path.lstat()
    if (
        not os.path.samestat(opened, after_handle)
        or not os.path.samestat(before, after_path)
        or size != before.st_size
        or size != after_handle.st_size
        or size != after_path.st_size
        or before.st_mtime_ns != after_handle.st_mtime_ns
        or before.st_mtime_ns != after_path.st_mtime_ns
    ):
        raise ExecutionSnapshotError(
            "execution_snapshot_cleanup_identity_changed",
            "Snapshot manifest changed while its ownership was checked",
        )
    return (
        before.st_dev,
        before.st_ino,
        size,
        before.st_mtime_ns,
        digest.hexdigest(),
    )


def _matches_owned_file_signature(
    path: Path,
    expected: tuple[int, int, int, int, str],
) -> bool:
    try:
        return _owned_file_signature(path) == expected
    except (OSError, ExecutionSnapshotError):
        return False


def _remove_owned_tree(
    path: Path,
    *,
    expected_parent: Path,
    expected_identity: tuple[int, int] | None,
) -> None:
    if not os.path.lexists(path):
        return
    parent = expected_parent.resolve(strict=True)
    if (
        path.parent.resolve(strict=True) != parent
        or (
            path.name != "execution_snapshot"
            and not path.name.startswith(".execution-snapshot-")
        )
        or is_link_or_reparse(path)
        or not path.is_dir()
    ):
        raise ExecutionSnapshotError(
            "execution_snapshot_cleanup_refused",
            "Cleanup target is not an owned snapshot directory",
        )
    if (
        expected_identity is None
        or _file_identity(path.lstat()) != expected_identity
    ):
        raise ExecutionSnapshotError(
            "execution_snapshot_cleanup_identity_changed",
            "Cleanup target identity changed; current object was preserved",
        )
    _make_owned_snapshot_writable(path)
    shutil.rmtree(path)


def _atomic_publish_directory_no_overwrite(
    source: Path,
    destination: Path,
) -> None:
    """Atomically publish a staging directory without replacing a target."""
    try:
        if os.name == "nt":
            # Windows rename is atomic within a volume and refuses an existing
            # destination when no replace flag is requested.
            os.rename(source, destination)
            return
        if sys.platform.startswith("linux"):
            import ctypes

            renameat2 = getattr(
                ctypes.CDLL(None, use_errno=True),
                "renameat2",
                None,
            )
            if renameat2 is None:
                raise ExecutionSnapshotError(
                    "execution_snapshot_atomic_publish_unsupported",
                    "Linux libc does not expose renameat2 no-replace",
                )
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                -100,
                os.fsencode(source),
                -100,
                os.fsencode(destination),
                1,
            )
            if result == 0:
                return
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise FileExistsError(destination)
            raise OSError(
                error_number,
                os.strerror(error_number),
                destination,
            )
        raise ExecutionSnapshotError(
            "execution_snapshot_atomic_publish_unsupported",
            "This platform has no supported atomic no-replace directory publish",
        )
    except FileExistsError as error:
        raise ExecutionSnapshotError(
            "execution_snapshot_exists",
            "Execution snapshot target already exists; overwrite refused",
        ) from error
