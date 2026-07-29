#!/usr/bin/env python3
"""真实执行科研与 PaperFlow 必需测试，并原子写出完整验证摘要。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from build_release_staging import (  # noqa: E402
    has_reparse_flag,
    safe_git_environment,
    stable_read_file,
    validate_new_json_output,
    write_new_json_report,
)
from run_release_checks import (  # noqa: E402
    MAX_CAPTURED_OUTPUT_BYTES,
    _offline_test_environment,
    _start_owned_process,
    _terminate_owned_process_tree,
)


SUMMARY_SCHEMA = "cv-research-paperflow.release-validation.v1"
SOURCE_SNAPSHOT_SCHEMA = "cv-research-paperflow.source-tree.v1"
DEFAULT_RESEARCH_TIMEOUT_SECONDS = 3600.0
DEFAULT_PAPERFLOW_TIMEOUT_SECONDS = 1800.0
MAX_OUTPUT_TAIL_LINES = 80
SHORT_BASETEMP_POLICY = "short-volume-root"
CANDIDATE_SKILL_MODE = "candidate-skill-sandbox"
MAX_SKILL_SANDBOX_BYTES = 20 * 1024 * 1024
GIT_SNAPSHOT_TIMEOUT_SECONDS = 30.0
WINDOWS_SANDBOX_RETENTION_STATUS = "retained_by_windows_safety_policy"
WINDOWS_UNVERIFIED_RETENTION_STATUS = "retained_unverified_setup_failure"
WINDOWS_RETENTION_VERIFICATION_FAILED = "retention_verification_failed"
CANDIDATE_SKILL_ID = "cv-paper-workflow"
EXPLICIT_RESEARCH_ENVIRONMENT_KEYS = frozenset(
    {
        "PAPERFLOW_BOUND_RECEIVER_ROOT",
        "PAPERFLOW_LIBRARY_ROOT",
        "HF_HOME",
        "CV_WORKFLOW_CUDA_PYTHON",
        "CV_WORKFLOW_CUDA_VISIBLE_DEVICES",
    }
)
INSTALLED_SKILL_IDS = (
    "cv-paper-ingest",
    "cv-paper-retrieve",
    "cv-methodology-distill",
    "cv-compose-methods",
    "cv-write-abstract",
    "cv-write-section",
    "cv-paper-audit",
    "cv-assemble-paper",
)
ISOLATED_PYTEST_WRAPPER = (
    "import os, sys\n"
    "import pytest\n"
    "sys.path.insert(0, os.getcwd())\n"
    "raise SystemExit(pytest.main(sys.argv[1:]))\n"
)


def _validate_environment_overrides(
    overrides: object,
) -> tuple[tuple[str, str], ...]:
    if type(overrides) is not tuple:
        raise ValueError("测试环境覆盖项必须使用不可变 tuple")
    seen: set[str] = set()
    for item in overrides:
        if (
            type(item) is not tuple
            or len(item) != 2
            or not all(isinstance(value, str) for value in item)
        ):
            raise ValueError("每个测试环境覆盖项必须是两个字符串组成的 tuple")
        key, value = item
        if key not in EXPLICIT_RESEARCH_ENVIRONMENT_KEYS:
            raise ValueError(f"测试环境覆盖键不在白名单中：{key}")
        if key in seen:
            raise ValueError(f"测试环境覆盖键不得重复：{key}")
        if not value or "\x00" in value:
            raise ValueError(f"测试环境覆盖值非法：{key}")
        if (
            key == "CV_WORKFLOW_CUDA_VISIBLE_DEVICES"
            and re.fullmatch(r"0|[1-9]\d*", value) is None
        ):
            raise ValueError("GPU 索引必须是非负整数")
        seen.add(key)
    return overrides


@dataclass(frozen=True)
class AllowedSkip:
    node_id: str
    reason: str


@dataclass(frozen=True)
class ValidationGroup:
    group_id: str
    cwd: Path
    command: tuple[str, ...]
    timeout_seconds: float
    seed: int = 1901
    allowed_skips: tuple[AllowedSkip, ...] = ()
    basetemp_policy: str | None = None
    candidate_skill_sandbox: bool = False
    installed_skills_root: Path | None = None
    environment_overrides: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _validate_environment_overrides(self.environment_overrides)


@dataclass
class _ExecutionSandbox:
    root: Path
    anchor: Path
    identity: tuple[int, int]
    owner_token: str
    marker: Path
    basetemp: Path
    home: Path
    environment: dict[str, str]
    command: tuple[str, ...]
    skill_mode: str


@dataclass(frozen=True)
class _SourceTreeSnapshot:
    sha256: str
    entry_count: int
    file_count: int
    directory_count: int
    total_file_bytes: int
    entries: tuple[tuple[object, ...], ...]


class _SandboxPreparationError(RuntimeError):
    def __init__(
        self,
        detail: str,
        *,
        root: Path,
        basetemp: Path | None,
        skill_mode: str,
        retention_status: str,
    ) -> None:
        super().__init__(detail)
        self.root = root
        self.basetemp = basetemp
        self.skill_mode = skill_mode
        self.retention_status = retention_status


RESEARCH_WINDOWS_ALLOWED_SKIPS = (
    AllowedSkip(
        node_id=(
            "tests/test_adapter_snapshot_isolation.py::"
            "AdapterSnapshotIsolationTests::"
            "test_bound_adapter_rejects_dir_fd_escape"
        ),
        reason="当前平台不支持 remove(dir_fd=...)",
    ),
)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _positive_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("超时必须是数字") from error
    if not 0 < parsed <= 24 * 60 * 60:
        raise argparse.ArgumentTypeError("超时必须大于 0 且不超过 24 小时")
    return parsed


def _gpu_index(value: str) -> int:
    if re.fullmatch(r"0|[1-9]\d*", value) is None:
        raise argparse.ArgumentTypeError("GPU 索引必须是非负整数")
    return int(value)


def _require_plain_directory(path: Path, label: str) -> Path:
    lexical = Path(os.path.abspath(path))
    try:
        metadata = os.lstat(lexical)
    except OSError as error:
        raise ValueError(f"{label}不存在：{lexical}") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or has_reparse_flag(metadata)
    ):
        raise ValueError(f"{label}必须是普通目录，不能是 link/reparse")
    return lexical.resolve(strict=True)


def _require_python(path: Path, label: str) -> Path:
    lexical = Path(os.path.abspath(path))
    try:
        metadata = os.lstat(lexical)
    except OSError as error:
        raise ValueError(f"{label}不存在：{lexical}") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or has_reparse_flag(metadata)
    ):
        raise ValueError(f"{label}必须是普通可执行文件")
    return lexical.resolve(strict=True)


def _git_semantic_snapshot(root: Path) -> bytes:
    environment = safe_git_environment()

    def run_git(*arguments: str) -> bytes:
        try:
            completed = subprocess.run(
                ("git", "-C", str(root), *arguments),
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=GIT_SNAPSHOT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError(
                f"Git source snapshot command failed: {error}"
            ) from error
        if completed.returncode != 0:
            detail = completed.stderr.decode(
                "utf-8",
                errors="replace",
            ).strip()[:240]
            raise ValueError(
                "Git source snapshot command returned "
                f"{completed.returncode}: {detail}"
            )
        return completed.stdout

    top_level = run_git("rev-parse", "--show-toplevel").decode(
        "utf-8",
        errors="strict",
    ).strip()
    if Path(top_level).resolve(strict=True) != root.resolve(strict=True):
        raise ValueError("Git top-level does not match source snapshot root")
    head = run_git("rev-parse", "--verify", "HEAD^{commit}").strip()
    tracked = run_git("ls-files", "--stage", "-v", "-z")
    return b"head\0" + head + b"\0tracked\0" + tracked


def _snapshot_source_tree(root: Path) -> _SourceTreeSnapshot:
    source = _require_plain_directory(root, "source snapshot root")
    root_identity = _directory_identity(source)
    entries: list[tuple[object, ...]] = []
    pending: list[tuple[Path, tuple[str, ...], tuple[int, int]]] = [
        (source, (), root_identity)
    ]
    file_count = 0
    directory_count = 0
    total_file_bytes = 0
    root_git_metadata = False

    while pending:
        directory, prefix, expected_identity = pending.pop()
        if _directory_identity(directory) != expected_identity:
            raise ValueError(
                f"source directory identity changed during snapshot: {directory}"
            )
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
        child_directories: list[
            tuple[Path, tuple[str, ...], tuple[int, int]]
        ] = []
        for child in children:
            lexical = Path(os.path.abspath(child.path))
            metadata = os.lstat(lexical)
            relative_parts = (*prefix, child.name)
            relative = "/".join(relative_parts)
            if child.name.casefold() == ".git":
                if prefix:
                    raise ValueError(
                        "source snapshot rejects nested Git metadata: "
                        f"{relative}"
                    )
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or has_reparse_flag(metadata)
                ):
                    raise ValueError(
                        "source snapshot rejects linked Git metadata"
                    )
                root_git_metadata = True
                if stat.S_ISDIR(metadata.st_mode):
                    entries.append(
                        ("git_metadata_directory", relative, 0, "")
                    )
                    directory_count += 1
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    content = stable_read_file(lexical)
                    entries.append(
                        (
                            "git_metadata_file",
                            relative,
                            len(content),
                            hashlib.sha256(content).hexdigest(),
                        )
                    )
                    file_count += 1
                    total_file_bytes += len(content)
                    continue
                raise ValueError(
                    "source snapshot rejects special Git metadata entries"
                )
            if stat.S_ISLNK(metadata.st_mode) or has_reparse_flag(metadata):
                raise ValueError(
                    "source snapshot rejects links and reparse points: "
                    f"{relative}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                identity = (
                    int(metadata.st_dev),
                    int(metadata.st_ino),
                )
                entries.append(("directory", relative, 0, ""))
                directory_count += 1
                child_directories.append(
                    (lexical, relative_parts, identity)
                )
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(
                    f"source snapshot rejects special entries: {relative}"
                )
            content = stable_read_file(lexical)
            size = len(content)
            entries.append(
                (
                    "file",
                    relative,
                    size,
                    hashlib.sha256(content).hexdigest(),
                )
            )
            file_count += 1
            total_file_bytes += size
        pending.extend(reversed(child_directories))

    if root_git_metadata:
        git_payload = _git_semantic_snapshot(source)
        entries.append(
            (
                "git_semantics",
                ".git-semantics",
                len(git_payload),
                hashlib.sha256(git_payload).hexdigest(),
            )
        )
    if _directory_identity(source) != root_identity:
        raise ValueError("source snapshot root identity changed during scan")
    entries.sort(key=lambda item: str(item[1]))
    frozen_entries = tuple(entries)
    encoded = json.dumps(
        frozen_entries,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _SourceTreeSnapshot(
        sha256=hashlib.sha256(encoded).hexdigest(),
        entry_count=len(frozen_entries),
        file_count=file_count,
        directory_count=directory_count,
        total_file_bytes=total_file_bytes,
        entries=frozen_entries,
    )


def _snapshot_metadata(
    snapshot: _SourceTreeSnapshot | None,
) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "sha256": snapshot.sha256,
        "entry_count": snapshot.entry_count,
        "file_count": snapshot.file_count,
        "directory_count": snapshot.directory_count,
        "total_file_bytes": snapshot.total_file_bytes,
    }


def _source_snapshot_report(
    before: _SourceTreeSnapshot | None,
    after: _SourceTreeSnapshot | None,
    *,
    error: str = "",
) -> dict[str, object]:
    changed_paths: list[str] = []
    if before is not None and after is not None:
        before_entries = {
            str(entry[1]): entry
            for entry in before.entries
        }
        after_entries = {
            str(entry[1]): entry
            for entry in after.entries
        }
        changed_paths = sorted(
            path
            for path in set(before_entries) | set(after_entries)
            if before_entries.get(path) != after_entries.get(path)
        )
    unchanged = (
        not error
        and before is not None
        and after is not None
        and before.entries == after.entries
    )
    if error:
        status = "ERROR"
        detail = f"source tree could not be verified: {error}"
    elif unchanged:
        status = "PASS"
        detail = "source tree bytes are unchanged"
    else:
        status = "FAIL"
        detail = (
            f"source tree changed at {len(changed_paths)} path(s)"
        )
    return {
        "schema": SOURCE_SNAPSHOT_SCHEMA,
        "status": status,
        "algorithm": "sha256",
        "scope": (
            "all relative paths, entry kinds, sizes, plain-file content "
            "sha256, root .git pointer bytes, and deterministic Git "
            "HEAD/index semantics; .git directories are not recursively "
            "hashed; links/reparse points are rejected; timestamps excluded"
        ),
        "before": _snapshot_metadata(before),
        "after": _snapshot_metadata(after),
        "before_sha256": before.sha256 if before is not None else None,
        "after_sha256": after.sha256 if after is not None else None,
        "unchanged": unchanged,
        "changed_path_count": len(changed_paths),
        "changed_paths": changed_paths[:80],
        "changed_paths_truncated": len(changed_paths) > 80,
        "detail": detail,
    }


def _attach_source_snapshot(
    result: dict[str, object],
    before: _SourceTreeSnapshot | None,
    after: _SourceTreeSnapshot | None,
    *,
    error: str = "",
) -> dict[str, object]:
    snapshot = _source_snapshot_report(before, after, error=error)
    unchanged = snapshot["status"] == "PASS"
    result["source_snapshot"] = snapshot
    result["source_snapshot_unchanged"] = unchanged
    if not unchanged:
        result["status"] = "FAIL"
        detail = str(result.get("detail", "")).strip()
        source_detail = str(snapshot["detail"])
        result["detail"] = (
            f"{detail}; {source_detail}"
            if detail
            else source_detail
        )
    return result


def _default_paperflow_python() -> Path:
    configured = os.environ.get("CVWF_PAPERFLOW_PYTHON")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return candidate
    if os.name == "nt":
        candidate = (
            Path.home()
            / ".codex"
            / "runtimes"
            / "cvwf-paperflow"
            / "Scripts"
            / "python.exe"
        )
    else:
        candidate = Path.home() / ".local" / "share" / "cvwf" / "bin" / "python"
    return candidate if candidate.is_file() else Path(sys.executable)


def _pytest_command(python: Path, test_root: str) -> tuple[str, ...]:
    return (
        str(python),
        "-I",
        "-B",
        "-X",
        "utf8",
        "-c",
        ISOLATED_PYTEST_WRAPPER,
        "-vv",
        "-ra",
        "--color=no",
        "-o",
        "addopts=",
        "-p",
        "no:cacheprovider",
        test_root,
    )


def build_default_groups(
    *,
    research_root: Path,
    paperflow_root: Path,
    paperflow_library_root: Path,
    hf_home: Path,
    cuda_python: Path,
    research_python: Path,
    paperflow_python: Path,
    gpu_index: int = 0,
    research_timeout: float = DEFAULT_RESEARCH_TIMEOUT_SECONDS,
    paperflow_timeout: float = DEFAULT_PAPERFLOW_TIMEOUT_SECONDS,
    installed_skills_root: Path | None = None,
) -> list[ValidationGroup]:
    if research_timeout <= 0 or paperflow_timeout <= 0:
        raise ValueError("每个测试组的超时必须大于 0")
    if (
        isinstance(gpu_index, bool)
        or not isinstance(gpu_index, int)
        or gpu_index < 0
    ):
        raise ValueError("GPU 索引必须是非负整数")
    research = _require_plain_directory(research_root, "科研源码根")
    paperflow = _require_plain_directory(paperflow_root, "PaperFlow 源码根")
    paperflow_library = _require_plain_directory(
        paperflow_library_root,
        "PaperFlow 知识库根",
    )
    huggingface_cache = _require_plain_directory(
        hf_home,
        "Hugging Face 缓存根",
    )
    cuda_runtime = _require_python(cuda_python, "GPU Python")
    research_runtime = _require_python(research_python, "科研 Python")
    paperflow_runtime = _require_python(paperflow_python, "PaperFlow Python")
    skills_root = _require_plain_directory(
        (
            Path(installed_skills_root)
            if installed_skills_root is not None
            else Path.home() / ".codex" / "skills"
        ),
        "已安装 Skill 根目录",
    )
    return [
        ValidationGroup(
            group_id="research",
            cwd=research,
            command=_pytest_command(research_runtime, "tests"),
            timeout_seconds=float(research_timeout),
            seed=1901,
            allowed_skips=RESEARCH_WINDOWS_ALLOWED_SKIPS,
            basetemp_policy=SHORT_BASETEMP_POLICY,
            environment_overrides=(
                ("PAPERFLOW_BOUND_RECEIVER_ROOT", str(paperflow)),
                ("PAPERFLOW_LIBRARY_ROOT", str(paperflow_library)),
                ("HF_HOME", str(huggingface_cache)),
                ("CV_WORKFLOW_CUDA_PYTHON", str(cuda_runtime)),
                ("CV_WORKFLOW_CUDA_VISIBLE_DEVICES", str(gpu_index)),
            ),
        ),
        ValidationGroup(
            group_id="paperflow",
            cwd=paperflow,
            command=_pytest_command(paperflow_runtime, "tests_v2"),
            timeout_seconds=float(paperflow_timeout),
            seed=1907,
            basetemp_policy=SHORT_BASETEMP_POLICY,
            candidate_skill_sandbox=True,
            installed_skills_root=skills_root,
        ),
    ]


def _directory_identity(path: Path) -> tuple[int, int]:
    metadata = os.lstat(path)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or has_reparse_flag(metadata)
    ):
        raise ValueError(f"执行隔离目录必须是普通目录：{path}")
    return int(metadata.st_dev), int(metadata.st_ino)


def _require_owned_cleanup_root(sandbox: _ExecutionSandbox) -> Path:
    lexical = Path(os.path.abspath(sandbox.root))
    anchor = Path(os.path.abspath(sandbox.anchor))
    try:
        anchor_metadata = os.lstat(anchor)
        root_identity = _directory_identity(lexical)
    except (OSError, ValueError) as error:
        raise RuntimeError(
            "sandbox cleanup root identity cannot be verified"
        ) from error
    if (
        lexical.parent != anchor
        or not lexical.name.startswith("cvv-")
        or not stat.S_ISDIR(anchor_metadata.st_mode)
        or stat.S_ISLNK(anchor_metadata.st_mode)
        or has_reparse_flag(anchor_metadata)
        or root_identity != sandbox.identity
    ):
        raise RuntimeError(
            "sandbox cleanup root identity or lexical ownership changed"
        )
    return lexical


def _require_owned_cleanup_target(
    sandbox: _ExecutionSandbox,
    raw_path: str | os.PathLike[str],
) -> os.stat_result:
    root = _require_owned_cleanup_root(sandbox)
    target = Path(os.path.abspath(raw_path))
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise RuntimeError(
            f"cleanup target is outside the owned sandbox boundary: {target}"
        ) from error

    current = root
    if not relative.parts:
        return os.lstat(root)
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            metadata = os.lstat(current)
        except OSError as error:
            raise RuntimeError(
                f"cleanup target identity cannot be verified: {current}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or has_reparse_flag(metadata):
            raise RuntimeError(
                f"cleanup target must not contain a link/reparse point: {current}"
            )
        is_final = index == len(relative.parts) - 1
        if not is_final and not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(
                f"cleanup target parent must be an ordinary directory: {current}"
            )
        if is_final:
            if not (
                stat.S_ISREG(metadata.st_mode)
                or stat.S_ISDIR(metadata.st_mode)
            ):
                raise RuntimeError(
                    f"cleanup target must be an ordinary file or directory: {current}"
                )
            if (
                stat.S_ISREG(metadata.st_mode)
                and int(getattr(metadata, "st_nlink", 1)) != 1
            ):
                raise RuntimeError(
                    f"cleanup target file must have exactly one hard link: {current}"
                )
            return metadata
    raise RuntimeError(f"cleanup target cannot be verified: {target}")


def _require_cleanup_ownership_marker(sandbox: _ExecutionSandbox) -> Path:
    root = _require_owned_cleanup_root(sandbox)
    marker = Path(os.path.abspath(sandbox.marker))
    if marker != root / ".owner":
        raise RuntimeError("sandbox ownership marker path changed")
    try:
        metadata = _require_owned_cleanup_target(sandbox, marker)
        token = stable_read_file(marker, limit=256)
        expected = sandbox.owner_token.encode("ascii")
    except (OSError, UnicodeError, ValueError, RuntimeError) as error:
        raise RuntimeError("sandbox ownership marker cannot be verified") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or int(getattr(metadata, "st_nlink", 1)) != 1
        or token != expected
    ):
        raise RuntimeError("sandbox ownership marker changed")
    return root


def _require_plain_descendant_directory(
    root: Path,
    relative_parts: Sequence[str],
    label: str,
) -> Path:
    trusted_root = _require_plain_directory(root, f"{label}根目录")
    current = trusted_root
    for part in relative_parts:
        if (
            not isinstance(part, str)
            or not part
            or part in {".", ".."}
            or Path(part).name != part
        ):
            raise ValueError(f"{label}包含非法目录名")
        child = current / part
        try:
            metadata = os.lstat(child)
        except OSError as error:
            raise ValueError(f"{label}不存在：{child}") from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or has_reparse_flag(metadata)
        ):
            raise ValueError(
                f"{label}的每一级都必须位于候选项目内，"
                f"且不能是 link/reparse：{child}"
            )
        resolved = child.resolve(strict=True)
        try:
            resolved.relative_to(trusted_root)
        except ValueError as error:
            raise ValueError(f"{label}越出候选项目：{resolved}") from error
        current = resolved
    return current


def _copy_plain_skill_tree(
    source: Path,
    destination: Path,
    *,
    remaining_bytes: int,
) -> int:
    source_root = _require_plain_directory(source, "Skill 来源目录")
    if os.path.lexists(destination):
        raise ValueError(f"Skill 沙箱目标必须不存在：{destination}")
    destination.mkdir(parents=True, exist_ok=False)
    copied = 0
    for current, directory_names, file_names in os.walk(
        source_root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        _directory_identity(current_path)
        relative_directory = current_path.relative_to(source_root)
        destination_directory = destination / relative_directory
        destination_directory.mkdir(parents=True, exist_ok=True)
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            child = current_path / name
            _directory_identity(child)
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in sorted(file_names):
            source_file = current_path / name
            metadata = os.lstat(source_file)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or has_reparse_flag(metadata)
                or int(getattr(metadata, "st_nlink", 1)) != 1
            ):
                raise ValueError(f"Skill 来源只能含普通单链接文件：{source_file}")
            content = stable_read_file(
                source_file,
                limit=remaining_bytes - copied,
            )
            copied += len(content)
            if copied > remaining_bytes:
                raise ValueError("Skill 沙箱复制体积超过上限")
            target = destination_directory / name
            with target.open("xb") as handle:
                handle.write(content)
    return copied


def _sandbox_environment(home: Path, temp_root: Path) -> dict[str, str]:
    appdata = home / "AppData" / "Roaming"
    local_appdata = home / "AppData" / "Local"
    temp_dir = temp_root / "t"
    for directory in (appdata, local_appdata, temp_dir):
        directory.mkdir(parents=True, exist_ok=False)
    values = {
        "USERPROFILE": str(home),
        "HOME": str(home),
        "APPDATA": str(appdata),
        "LOCALAPPDATA": str(local_appdata),
        "TEMP": str(temp_dir),
        "TMP": str(temp_dir),
        "TMPDIR": str(temp_dir),
        "PYTHONUSERBASE": str(home / ".local"),
    }
    if os.name == "nt":
        values["HOMEDRIVE"] = home.drive
        values["HOMEPATH"] = str(home)[len(home.drive):]
    return values


def _retain_execution_sandbox(sandbox: _ExecutionSandbox) -> str:
    if os.name != "nt":
        raise RuntimeError("本版发布验证器只支持 Windows 沙箱保留策略")
    _require_cleanup_ownership_marker(sandbox)
    return WINDOWS_SANDBOX_RETENTION_STATUS


def _prepare_execution_sandbox(
    group: ValidationGroup,
    cwd: Path,
) -> _ExecutionSandbox | None:
    if group.basetemp_policy is None:
        if group.candidate_skill_sandbox:
            raise ValueError("candidate skill sandbox 必须使用短卷根 basetemp")
        return None
    if group.basetemp_policy != SHORT_BASETEMP_POLICY:
        raise ValueError(f"未知 basetemp 策略：{group.basetemp_policy}")
    if (
        "-I" not in group.command
        or "-c" not in group.command
        or ISOLATED_PYTEST_WRAPPER not in group.command
    ):
        raise ValueError("短 basetemp 只允许用于隔离启动的 pytest 测试组")
    anchor = _require_plain_directory(Path(cwd.anchor), "测试工作目录卷根")
    root = Path(
        tempfile.mkdtemp(
            prefix="cvv-",
            dir=str(anchor),
        )
    )
    try:
        identity = _directory_identity(root)
        owner_token = os.urandom(24).hex()
        marker = root / ".owner"
        marker.write_text(owner_token, encoding="ascii")
    except (OSError, ValueError, RuntimeError) as error:
        raise _SandboxPreparationError(
            str(error),
            root=root,
            basetemp=None,
            skill_mode=(
                CANDIDATE_SKILL_MODE
                if group.candidate_skill_sandbox
                else "none"
            ),
            retention_status=WINDOWS_UNVERIFIED_RETENTION_STATUS,
        ) from error
    basetemp = root / "b"
    home = root / "h"
    sandbox = _ExecutionSandbox(
        root=root,
        anchor=anchor,
        identity=identity,
        owner_token=owner_token,
        marker=marker,
        basetemp=basetemp,
        home=home,
        environment={},
        command=(*group.command, "--basetemp", str(basetemp)),
        skill_mode="none",
    )
    try:
        basetemp.mkdir()
        home.mkdir()
        sandbox.environment = _sandbox_environment(home, root)
        if group.candidate_skill_sandbox:
            installed_root = group.installed_skills_root
            if installed_root is None:
                raise ValueError("candidate skill sandbox 缺少已安装 Skill 根目录")
            installed = _require_plain_directory(
                installed_root,
                "已安装 Skill 根目录",
            )
            candidate_skill = _require_plain_descendant_directory(
                cwd,
                ("docs", "skills", CANDIDATE_SKILL_ID),
                "候选项目 Skill",
            )
            destination_root = home / ".codex" / "skills"
            destination_root.mkdir(parents=True)
            copied = 0
            for skill_id in INSTALLED_SKILL_IDS:
                copied += _copy_plain_skill_tree(
                    installed / skill_id,
                    destination_root / skill_id,
                    remaining_bytes=MAX_SKILL_SANDBOX_BYTES - copied,
                )
            copied += _copy_plain_skill_tree(
                candidate_skill,
                destination_root / CANDIDATE_SKILL_ID,
                remaining_bytes=MAX_SKILL_SANDBOX_BYTES - copied,
            )
            sandbox.skill_mode = CANDIDATE_SKILL_MODE
    except (OSError, ValueError, RuntimeError) as error:
        try:
            disposition = _retain_execution_sandbox(sandbox)
            detail = str(error)
        except (OSError, ValueError, RuntimeError) as retention_error:
            disposition = WINDOWS_RETENTION_VERIFICATION_FAILED
            detail = f"{error}；沙箱保留状态无法核验：{retention_error}"
        raise _SandboxPreparationError(
            detail,
            root=sandbox.root,
            basetemp=sandbox.basetemp,
            skill_mode=(
                CANDIDATE_SKILL_MODE
                if group.candidate_skill_sandbox
                else sandbox.skill_mode
            ),
            retention_status=disposition,
        ) from error
    return sandbox


def _sandbox_report(
    group: ValidationGroup,
    sandbox: _ExecutionSandbox | None,
    *,
    cleanup: str,
) -> dict[str, object]:
    return {
        "basetemp_policy": group.basetemp_policy or "none",
        "skill_mode": (
            sandbox.skill_mode
            if sandbox is not None
            else (
                CANDIDATE_SKILL_MODE
                if group.candidate_skill_sandbox
                else "none"
            )
        ),
        "root": str(sandbox.root) if sandbox is not None else None,
        "basetemp": str(sandbox.basetemp) if sandbox is not None else None,
        "cleanup": cleanup,
    }


def _count_pytest_outcomes(output: str) -> dict[str, int]:
    def largest(pattern: str) -> int:
        return max(
            (int(value) for value in re.findall(pattern, output)),
            default=0,
        )

    return {
        "passed": largest(r"(?<!\d)(\d+)\s+passed\b"),
        "failed": largest(r"(?<!\d)(\d+)\s+failed\b"),
        "errors": largest(r"(?<!\d)(\d+)\s+errors?\b"),
        "skipped": largest(r"(?<!\d)(\d+)\s+skipped\b"),
        "xfailed": largest(r"(?<!\d)(\d+)\s+xfailed\b"),
        "xpassed": largest(r"(?<!\d)(\d+)\s+xpassed\b"),
        "deselected": largest(r"(?<!\d)(\d+)\s+deselected\b"),
    }


def _allowed_skip_was_observed(output: str, rule: AllowedSkip) -> bool:
    normalized = output.replace("\\", "/")
    node_id = rule.node_id.replace("\\", "/")
    test_file = node_id.split("::", 1)[0]
    node_line = re.compile(
        rf"^{re.escape(node_id)}\s+SKIPPED(?:\s|\()",
        re.MULTILINE,
    )
    reason_line = re.compile(
        rf"^SKIPPED\s+\[\d+\]\s+{re.escape(test_file)}:\d+:\s+"
        rf"{re.escape(rule.reason)}\s*$",
        re.MULTILINE,
    )
    return bool(node_line.search(normalized) and reason_line.search(normalized))


def _validate_allowed_skip_rules(group: ValidationGroup) -> None:
    seen: set[str] = set()
    for rule in group.allowed_skips:
        if (
            not isinstance(rule, AllowedSkip)
            or not rule.node_id.strip()
            or not rule.reason.strip()
            or "::" not in rule.node_id
        ):
            raise ValueError(
                f"{group.group_id} 允许跳过项必须提供精确测试节点和原因"
            )
        normalized = rule.node_id.replace("\\", "/")
        if normalized in seen:
            raise ValueError(f"{group.group_id} 允许跳过的测试节点不得重复")
        seen.add(normalized)


def _failure_result(
    group: ValidationGroup,
    *,
    started_at: str,
    started: float,
    detail: str,
) -> dict[str, object]:
    return {
        "id": group.group_id,
        "status": "FAIL",
        "mode": "execute",
        "executed": False,
        "command": list(group.command),
        "cwd": str(Path(group.cwd).resolve(strict=False)),
        "timeout_seconds": group.timeout_seconds,
        "seed": group.seed,
        "started_at": started_at,
        "duration_seconds": round(time.monotonic() - started, 3),
        "return_code": None,
        "timed_out": False,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "deselected": 0,
        "allowed_skipped": 0,
        "unexpected_skipped": 0,
        "allowed_skip_rules": [
            {"node_id": rule.node_id, "reason": rule.reason}
            for rule in group.allowed_skips
        ],
        "process_owner": None,
        "process_tree_cleanup": "NOT_STARTED",
        "output_limit_exceeded": False,
        "captured_output_bytes": 0,
        "output_tail": [],
        "sandbox": _sandbox_report(
            group,
            None,
            cleanup="NOT_STARTED",
        ),
        "detail": detail,
    }


def _execute_validation_group(
    group: ValidationGroup,
    *,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    emit = progress or (lambda _message: None)
    started = time.monotonic()
    started_at = _utc_now()
    if not isinstance(group.group_id, str) or not group.group_id.strip():
        raise ValueError("测试组 id 必须是非空字符串")
    if not group.command or not all(
        isinstance(item, str) and item for item in group.command
    ):
        raise ValueError(f"{group.group_id} 测试命令必须是非空字符串序列")
    if group.timeout_seconds <= 0:
        raise ValueError(f"{group.group_id} 测试超时必须大于 0")
    _validate_allowed_skip_rules(group)
    cwd = _require_plain_directory(Path(group.cwd), f"{group.group_id} 工作目录")
    try:
        sandbox = _prepare_execution_sandbox(group, cwd)
    except _SandboxPreparationError as error:
        result = _failure_result(
            group,
            started_at=started_at,
            started=started,
            detail=f"测试执行隔离准备失败：{error}",
        )
        result["sandbox"] = {
            "basetemp_policy": group.basetemp_policy or "none",
            "skill_mode": error.skill_mode,
            "root": str(error.root),
            "basetemp": (
                str(error.basetemp)
                if error.basetemp is not None
                else None
            ),
            "cleanup": error.retention_status,
        }
        emit(
            f"[WARN] {group.group_id}：{error.retention_status}："
            f"{error.root}"
        )
        emit(f"[FAIL] {group.group_id}：{result['detail']}")
        return result
    except (OSError, ValueError, RuntimeError) as error:
        result = _failure_result(
            group,
            started_at=started_at,
            started=started,
            detail=f"测试执行隔离准备失败：{error}",
        )
        result["sandbox"] = _sandbox_report(
            group,
            None,
            cleanup="SETUP_FAILED",
        )
        emit(f"[FAIL] {group.group_id}：{result['detail']}")
        return result
    emit(
        f"[START] {group.group_id}：真实执行，"
        f"timeout={group.timeout_seconds}s"
    )
    environment = _offline_test_environment(group.seed)
    for key in EXPLICIT_RESEARCH_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment["PYTHONUNBUFFERED"] = "1"
    effective_command = group.command
    if sandbox is not None:
        environment.update(sandbox.environment)
        effective_command = sandbox.command
    environment.update(
        dict(_validate_environment_overrides(group.environment_overrides))
    )
    try:
        process, owner = _start_owned_process(
            list(effective_command),
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        sandbox_cleanup = "NOT_REQUIRED"
        sandbox_error = ""
        if sandbox is not None:
            try:
                sandbox_cleanup = _retain_execution_sandbox(sandbox)
            except (OSError, ValueError, RuntimeError) as cleanup_error:
                sandbox_cleanup = WINDOWS_RETENTION_VERIFICATION_FAILED
                sandbox_error = f"；执行隔离保留状态核验失败：{cleanup_error}"
            emit(
                f"[WARN] {group.group_id}：{sandbox_cleanup}："
                f"{sandbox.root}"
            )
        result = _failure_result(
            group,
            started_at=started_at,
            started=started,
            detail=f"测试进程启动失败：{error}{sandbox_error}",
        )
        result["command"] = list(effective_command)
        result["sandbox"] = _sandbox_report(
            group,
            sandbox,
            cleanup=sandbox_cleanup,
        )
        emit(f"[FAIL] {group.group_id}：{result['detail']}")
        return result

    identity = {
        "pid": process.pid,
        "started_monotonic_ns": time.monotonic_ns(),
    }
    captured_lines: list[str] = []
    captured_output_bytes = 0
    output_limit_exceeded = threading.Event()
    assert process.stdout is not None

    def read_output() -> None:
        nonlocal captured_output_bytes
        for line in iter(process.stdout.readline, ""):
            clean = line.rstrip("\r\n")
            emit(f"[{group.group_id}] {clean}")
            size = len(line.encode("utf-8", errors="replace"))
            remaining = MAX_CAPTURED_OUTPUT_BYTES - captured_output_bytes
            if size <= remaining:
                captured_lines.append(clean)
                captured_output_bytes += size
            else:
                output_limit_exceeded.set()
        process.stdout.close()

    reader = threading.Thread(
        target=read_output,
        name=f"validation-output-{group.group_id}",
        daemon=True,
    )
    reader.start()
    timed_out = False
    cleanup_error = ""
    return_code: int | None = None
    try:
        return_code = process.wait(timeout=group.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
    try:
        _terminate_owned_process_tree(process, identity, owner)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        cleanup_error = str(error)
    if return_code is None:
        return_code = process.returncode
    try:
        owner.close()
    except (OSError, RuntimeError) as error:
        cleanup_error = cleanup_error or str(error)
    reader.join(timeout=5)
    if reader.is_alive():
        cleanup_error = cleanup_error or "输出读取线程未在进程树回收后结束"
    sandbox_cleanup_error = ""
    sandbox_cleanup = "NOT_REQUIRED"
    if sandbox is not None:
        try:
            sandbox_cleanup = _retain_execution_sandbox(sandbox)
        except (OSError, ValueError, RuntimeError) as error:
            sandbox_cleanup_error = str(error)
            sandbox_cleanup = WINDOWS_RETENTION_VERIFICATION_FAILED
        emit(
            f"[WARN] {group.group_id}：{sandbox_cleanup}："
            f"{sandbox.root}"
        )

    output = "\n".join(captured_lines)
    outcomes = _count_pytest_outcomes(output)
    passed = outcomes["passed"]
    skipped = outcomes["skipped"]
    xfailed = outcomes["xfailed"]
    xpassed = outcomes["xpassed"]
    deselected = outcomes["deselected"]
    allowed_skipped = min(
        skipped,
        sum(
            1
            for rule in group.allowed_skips
            if _allowed_skip_was_observed(output, rule)
        ),
    )
    unexpected_skipped = max(0, skipped - allowed_skipped)
    ok = (
        not timed_out
        and not cleanup_error
        and not sandbox_cleanup_error
        and not output_limit_exceeded.is_set()
        and return_code == 0
        and passed > 0
        and unexpected_skipped == 0
        and xfailed == 0
        and xpassed == 0
        and deselected == 0
    )
    detail_parts: list[str] = []
    if timed_out:
        detail_parts.append(f"超过 {group.timeout_seconds} 秒")
    if cleanup_error:
        detail_parts.append(f"进程树回收失败：{cleanup_error}")
    if sandbox_cleanup_error:
        detail_parts.append(f"执行隔离保留状态核验失败：{sandbox_cleanup_error}")
    if output_limit_exceeded.is_set():
        detail_parts.append(
            f"输出超过 {MAX_CAPTURED_OUTPUT_BYTES} 字节上限"
        )
    if return_code != 0:
        detail_parts.append(f"退出码 {return_code}")
    if allowed_skipped:
        detail_parts.append(f"{allowed_skipped} 个已知平台限制测试按规则跳过")
    if unexpected_skipped:
        detail_parts.append(f"{unexpected_skipped} 个必需测试被意外跳过")
    if xfailed:
        detail_parts.append(f"{xfailed} 个必需测试仍是预期失败")
    if xpassed:
        detail_parts.append(f"{xpassed} 个必需测试意外通过但未解除 xfail")
    if deselected:
        detail_parts.append(f"{deselected} 个必需测试未被执行")
    if passed == 0:
        detail_parts.append("没有可证明已通过的必需测试")
    detail = (
        "；".join(detail_parts)
        if detail_parts
        else f"{passed} 个必需测试通过"
    )
    status = "PASS" if ok else "FAIL"
    result = {
        "id": group.group_id,
        "status": status,
        "mode": "execute",
        "executed": True,
        "command": list(effective_command),
        "cwd": str(cwd),
        "timeout_seconds": group.timeout_seconds,
        "seed": group.seed,
        "started_at": started_at,
        "duration_seconds": round(time.monotonic() - started, 3),
        "return_code": return_code,
        "timed_out": timed_out,
        **outcomes,
        "allowed_skipped": allowed_skipped,
        "unexpected_skipped": unexpected_skipped,
        "allowed_skip_rules": [
            {"node_id": rule.node_id, "reason": rule.reason}
            for rule in group.allowed_skips
        ],
        "process_owner": owner.kind,
        "process_tree_cleanup": "FAIL" if cleanup_error else "PASS",
        "output_limit_exceeded": output_limit_exceeded.is_set(),
        "captured_output_bytes": captured_output_bytes,
        "output_tail": captured_lines[-MAX_OUTPUT_TAIL_LINES:],
        "sandbox": _sandbox_report(
            group,
            sandbox,
            cleanup=sandbox_cleanup,
        ),
        "detail": detail,
    }
    emit(f"[{status}] {group.group_id}：{detail}")
    return result


def run_validation_group(
    group: ValidationGroup,
    *,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    emit = progress or (lambda _message: None)
    snapshot_started = time.monotonic()
    snapshot_started_at = _utc_now()
    cwd = _require_plain_directory(
        Path(group.cwd),
        f"{group.group_id} source snapshot root",
    )
    try:
        before = _snapshot_source_tree(cwd)
    except (OSError, ValueError, RuntimeError) as error:
        result = _failure_result(
            group,
            started_at=snapshot_started_at,
            started=snapshot_started,
            detail=f"source tree snapshot failed before execution: {error}",
        )
        _attach_source_snapshot(
            result,
            None,
            None,
            error=str(error),
        )
        emit(f"[FAIL] {group.group_id}: {result['detail']}")
        return result

    result = _execute_validation_group(group, progress=progress)
    try:
        after = _snapshot_source_tree(cwd)
    except (OSError, ValueError, RuntimeError) as error:
        _attach_source_snapshot(
            result,
            before,
            None,
            error=str(error),
        )
        emit(f"[FAIL] {group.group_id}: {result['detail']}")
        return result

    previous_status = result.get("status")
    _attach_source_snapshot(result, before, after)
    if previous_status == "PASS" and result["status"] == "FAIL":
        emit(f"[FAIL] {group.group_id}: {result['detail']}")
    return result


def _canonical_digest(summary: dict[str, object]) -> str:
    unsigned = dict(summary)
    unsigned.pop("integrity", None)
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _with_integrity(summary: dict[str, object]) -> dict[str, object]:
    result = dict(summary)
    result["integrity"] = {
        "algorithm": "sha256",
        "scope": "canonical-json-without-integrity",
        "sha256": _canonical_digest(result),
    }
    return result


def validate_release(
    *,
    groups: Sequence[ValidationGroup],
    json_out: Path,
    forbidden_roots: Iterable[Path],
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    selected = list(groups)
    if not selected:
        raise ValueError("至少需要一个必需测试组")
    identifiers = [group.group_id for group in selected]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("必需测试组 id 不得重复")
    protected = tuple(Path(root) for root in forbidden_roots)
    validate_new_json_output(json_out, forbidden_roots=protected)

    emit = progress or (lambda _message: None)
    roots: dict[str, Path] = {}
    root_groups: dict[str, list[str]] = {}
    group_root_keys: dict[str, str] = {}
    for group in selected:
        root = _require_plain_directory(
            Path(group.cwd),
            f"{group.group_id} release source root",
        )
        root_key = os.path.normcase(str(root))
        roots.setdefault(root_key, root)
        root_groups.setdefault(root_key, []).append(group.group_id)
        group_root_keys[group.group_id] = root_key

    release_before: dict[str, _SourceTreeSnapshot | None] = {}
    release_before_errors: dict[str, str] = {}
    for root_key, root in roots.items():
        try:
            release_before[root_key] = _snapshot_source_tree(root)
        except (OSError, ValueError, RuntimeError) as error:
            release_before[root_key] = None
            release_before_errors[root_key] = str(error)

    latest_snapshots = dict(release_before)
    latest_errors = dict(release_before_errors)
    contamination: dict[str, dict[str, object]] = {}
    for root_key, error in release_before_errors.items():
        contamination[root_key] = {
            "detected_after_group": "before_execution",
            "snapshot": _source_snapshot_report(
                None,
                None,
                error=error,
            ),
        }

    def capture_release_roots() -> tuple[
        dict[str, _SourceTreeSnapshot | None],
        dict[str, str],
    ]:
        snapshots: dict[str, _SourceTreeSnapshot | None] = {}
        errors: dict[str, str] = {}
        for root_key, root in roots.items():
            try:
                snapshots[root_key] = _snapshot_source_tree(root)
            except (OSError, ValueError, RuntimeError) as error:
                snapshots[root_key] = None
                errors[root_key] = str(error)
        return snapshots, errors

    def failed_without_execution(
        group: ValidationGroup,
        detail: str,
    ) -> dict[str, object]:
        started = time.monotonic()
        result = _failure_result(
            group,
            started_at=_utc_now(),
            started=started,
            detail=detail,
        )
        root_key = group_root_keys[group.group_id]
        _attach_source_snapshot(
            result,
            release_before.get(root_key),
            latest_snapshots.get(root_key),
            error=latest_errors.get(root_key, ""),
        )
        emit(f"[FAIL] {group.group_id}: {result['detail']}")
        return result

    results: list[dict[str, object]] = []
    for group in selected:
        if contamination:
            results.append(
                failed_without_execution(
                    group,
                    "not executed because a release source root was "
                    "already contaminated",
                )
            )
            continue
        try:
            result = run_validation_group(group, progress=progress)
        except (OSError, ValueError, RuntimeError) as error:
            result = failed_without_execution(
                group,
                f"validation group could not execute safely: {error}",
            )
        results.append(result)

        checkpoint_snapshots, checkpoint_errors = capture_release_roots()
        latest_snapshots = checkpoint_snapshots
        latest_errors = checkpoint_errors
        newly_contaminated: list[str] = []
        for root_key in roots:
            checkpoint = _source_snapshot_report(
                release_before.get(root_key),
                checkpoint_snapshots.get(root_key),
                error=checkpoint_errors.get(root_key, ""),
            )
            if checkpoint["status"] == "PASS":
                continue
            if root_key not in contamination:
                contamination[root_key] = {
                    "detected_after_group": group.group_id,
                    "snapshot": checkpoint,
                }
                newly_contaminated.append(root_key)
        if newly_contaminated:
            result["status"] = "FAIL"
            contaminated_groups = sorted(
                {
                    group_id
                    for root_key in newly_contaminated
                    for group_id in root_groups[root_key]
                }
            )
            checkpoint_detail = (
                "release-wide source contamination detected after this "
                f"group; affected roots belong to {contaminated_groups}"
            )
            existing = str(result.get("detail", "")).strip()
            result["detail"] = (
                f"{existing}; {checkpoint_detail}"
                if existing
                else checkpoint_detail
            )
            emit(f"[FAIL] {group.group_id}: {checkpoint_detail}")

    final_snapshots, final_errors = capture_release_roots()
    latest_snapshots = final_snapshots
    latest_errors = final_errors
    for root_key in roots:
        final_checkpoint = _source_snapshot_report(
            release_before.get(root_key),
            final_snapshots.get(root_key),
            error=final_errors.get(root_key, ""),
        )
        if (
            final_checkpoint["status"] != "PASS"
            and root_key not in contamination
        ):
            contamination[root_key] = {
                "detected_after_group": "final_verification",
                "snapshot": final_checkpoint,
            }

    results_by_id = {
        str(result["id"]): result
        for result in results
    }
    release_source_roots: list[dict[str, object]] = []
    for root_key, root in roots.items():
        final_snapshot = _source_snapshot_report(
            release_before.get(root_key),
            final_snapshots.get(root_key),
            error=final_errors.get(root_key, ""),
        )
        detected = contamination.get(root_key)
        if detected is not None:
            observed = dict(detected["snapshot"])
            observed["status"] = "FAIL"
            observed["unchanged"] = False
            observed["detected_after_group"] = detected[
                "detected_after_group"
            ]
            observed["final"] = final_snapshot["after"]
            observed["final_sha256"] = final_snapshot["after_sha256"]
            observed["final_matches_before"] = (
                final_snapshot["status"] == "PASS"
            )
            snapshot = observed
        else:
            snapshot = final_snapshot
            snapshot["detected_after_group"] = None
            snapshot["final"] = final_snapshot["after"]
            snapshot["final_sha256"] = final_snapshot["after_sha256"]
            snapshot["final_matches_before"] = True
        release_entry = {
            "group_ids": list(root_groups[root_key]),
            **snapshot,
        }
        release_source_roots.append(release_entry)
        unchanged = release_entry["status"] == "PASS"
        for group_id in root_groups[root_key]:
            result = results_by_id[group_id]
            result["release_source_snapshot"] = release_entry
            result["release_source_snapshot_unchanged"] = unchanged
            if unchanged:
                continue
            result["status"] = "FAIL"
            existing = str(result.get("detail", "")).strip()
            release_detail = (
                "release-wide source tree verification failed: "
                f"{release_entry['detail']}"
            )
            if release_detail not in existing:
                result["detail"] = (
                    f"{existing}; {release_detail}"
                    if existing
                    else release_detail
                )
            emit(f"[FAIL] {group_id}: {release_detail}")

    release_sources_unchanged = all(
        entry["status"] == "PASS"
        for entry in release_source_roots
    )
    ok = (
        release_sources_unchanged
        and all(result["status"] == "PASS" for result in results)
    )
    report = _with_integrity(
        {
            "schema": SUMMARY_SCHEMA,
            "version": "1.2.0",
            "generated_at": _utc_now(),
            "status": "PASS" if ok else "FAIL",
            "ok": ok,
            "power_action": "none",
            "source_roots_unchanged": release_sources_unchanged,
            "source_roots": release_source_roots,
            "groups": results,
        }
    )
    write_new_json_report(
        json_out,
        report,
        forbidden_roots=protected,
    )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按组真实执行科研与 PaperFlow 必需测试并写出验证摘要。"
    )
    parser.add_argument("--paperflow-root", required=True, type=Path)
    parser.add_argument(
        "--paperflow-library-root",
        required=True,
        type=Path,
    )
    parser.add_argument("--hf-home", required=True, type=Path)
    parser.add_argument("--cuda-python", required=True, type=Path)
    parser.add_argument("--gpu-index", type=_gpu_index, default=0)
    parser.add_argument("--json-out", required=True, type=Path)
    parser.add_argument("--research-python", type=Path)
    parser.add_argument("--paperflow-python", type=Path)
    parser.add_argument(
        "--protected-root",
        action="append",
        type=Path,
        default=[],
        help="显式保护的禁止写入根目录；可重复提供。",
    )
    parser.add_argument(
        "--research-timeout",
        type=_positive_timeout,
        default=DEFAULT_RESEARCH_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--paperflow-timeout",
        type=_positive_timeout,
        default=DEFAULT_PAPERFLOW_TIMEOUT_SECONDS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        research = _require_plain_directory(ROOT, "科研源码根")
        paperflow = _require_plain_directory(
            arguments.paperflow_root,
            "PaperFlow 源码根",
        )
        paperflow_library = _require_plain_directory(
            arguments.paperflow_library_root,
            "PaperFlow 知识库根",
        )
        hf_home = _require_plain_directory(
            arguments.hf_home,
            "Hugging Face 缓存根",
        )
        cuda_python = _require_python(
            arguments.cuda_python,
            "GPU Python",
        )
        research_python = (
            arguments.research_python
            if arguments.research_python is not None
            else Path(sys.executable)
        )
        paperflow_python = (
            arguments.paperflow_python
            if arguments.paperflow_python is not None
            else _default_paperflow_python()
        )
        groups = build_default_groups(
            research_root=research,
            paperflow_root=paperflow,
            paperflow_library_root=paperflow_library,
            hf_home=hf_home,
            cuda_python=cuda_python,
            gpu_index=arguments.gpu_index,
            research_python=research_python,
            paperflow_python=paperflow_python,
            research_timeout=arguments.research_timeout,
            paperflow_timeout=arguments.paperflow_timeout,
        )
        report = validate_release(
            groups=groups,
            json_out=arguments.json_out,
            forbidden_roots=(
                research,
                paperflow,
                *arguments.protected_root,
            ),
            progress=lambda message: print(
                message,
                file=sys.stderr,
                flush=True,
            ),
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(
            json.dumps(
                {
                    "schema": SUMMARY_SCHEMA,
                    "ok": False,
                    "status": "FAIL",
                    "error": str(error),
                    "power_action": "none",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
