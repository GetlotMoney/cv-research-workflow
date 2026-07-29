#!/usr/bin/env python3
"""复查清洁发布暂存目录，并只从约定测试目录发现必需测试。"""

from __future__ import annotations

import argparse
import configparser
import ctypes
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

if os.name == "nt":
    from ctypes import wintypes


TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from build_release_staging import (  # noqa: E402
    DOWNLOAD_URLS,
    EXCLUSIONS_NAME,
    MANIFEST_NAME,
    MAX_RELEASE_BYTES,
    content_contains_private_path,
    contains_private_absolute_path,
    has_reparse_flag,
    parse_strict_json_object,
    safe_git_environment,
    stable_read_file,
    validate_release_paths,
    write_new_json_report,
)


_ALLOWED_TOP_LEVEL = {
    "research",
    "paperflow",
    MANIFEST_NAME,
    EXCLUSIONS_NAME,
}
_FORBIDDEN_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "artifacts",
    "cache",
    "caches",
    "checkpoints",
    "datasets",
    "dist",
    "logs",
    "models",
    "node_modules",
    "output",
    "outputs",
    "runs",
    "venv",
    "weights",
}
_FORBIDDEN_SUFFIXES = {
    ".7z",
    ".bin",
    ".ckpt",
    ".db",
    ".dll",
    ".exe",
    ".gif",
    ".gz",
    ".jpeg",
    ".jpg",
    ".log",
    ".npy",
    ".npz",
    ".onnx",
    ".pdf",
    ".pickle",
    ".pkl",
    ".png",
    ".pt",
    ".pth",
    ".pyc",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tiff",
    ".webp",
    ".zip",
}
_COLLECTION_SKIP_MARKER = "CV_RELEASE_REQUIRED_COLLECTION_SKIP::"
MAX_CAPTURED_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_RELEASE_FILES = 100_000
_COLLECTION_WRAPPER = """
import os
import sys
import pytest
from _pytest.skipping import evaluate_skip_marks

sys.path.insert(0, os.getcwd())

MARKER = "CV_RELEASE_REQUIRED_COLLECTION_SKIP::"

class RequiredCollectionPlugin:
    def pytest_collectreport(self, report):
        if report.skipped:
            print(MARKER + str(report.nodeid), flush=True)

    def pytest_collection_modifyitems(self, session, config, items):
        for item in items:
            if not any(
                marker.name in {"skip", "skipif"}
                for marker in item.iter_markers()
            ):
                continue
            decision = evaluate_skip_marks(item)
            if decision is not None:
                print(MARKER + str(item.nodeid), flush=True)

raise SystemExit(pytest.main(
    ["--collect-only", "-q", "-ra", "-p", "no:cacheprovider", sys.argv[1]],
    plugins=[RequiredCollectionPlugin()],
))
""".strip()


if os.name == "nt":
    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]


    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]


    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


class _WindowsJobOwner:
    """用内核 Job handle 绑定进程树，避免 PID 复用误杀。"""

    _KILL_ON_JOB_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows Job Object 只能在 Windows 创建")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
        ]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._ntdll = ctypes.WinDLL("ntdll")
        self._ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self._ntdll.NtResumeProcess.restype = ctypes.c_long
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = self._KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self._handle,
            self._EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    @property
    def kind(self) -> str:
        return "windows_job_object"

    def assign(self, process: subprocess.Popen[str]) -> None:
        raw_handle = getattr(process, "_handle", None)
        if raw_handle is None:
            raise RuntimeError("Popen 未暴露 Windows 进程 handle，拒绝按 PID 降级")
        if not self._kernel32.AssignProcessToJobObject(
            self._handle,
            wintypes.HANDLE(int(raw_handle)),
        ):
            error_code = ctypes.get_last_error()
            raise RuntimeError(
                "无法把子进程加入独立 Windows Job Object，拒绝按 PID 降级；"
                f"WinError={error_code}"
            )

    def resume(self, process: subprocess.Popen[str]) -> None:
        raw_handle = getattr(process, "_handle", None)
        if raw_handle is None:
            raise RuntimeError("Popen 未暴露 Windows 进程 handle，无法恢复挂起进程")
        status = int(
            self._ntdll.NtResumeProcess(
                wintypes.HANDLE(int(raw_handle)),
            )
        )
        if status != 0:
            raise RuntimeError(
                "Windows 子进程已加入 Job，但无法恢复挂起状态；"
                f"NTSTATUS=0x{status & 0xFFFFFFFF:08x}"
            )

    def terminate(
        self,
        process: subprocess.Popen[str],
        identity: dict[str, int],
    ) -> None:
        if process.pid != identity.get("pid"):
            raise RuntimeError("进程身份不一致，拒绝回收")
        if not self._kernel32.TerminateJobObject(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if getattr(self, "_handle", None):
            handle = self._handle
            self._handle = None
            if not self._kernel32.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())


class _PosixProcessGroupOwner:
    @property
    def kind(self) -> str:
        return "posix_process_group"

    def assign(self, process: subprocess.Popen[str]) -> None:
        if os.getpgid(process.pid) != process.pid:
            raise RuntimeError("子进程没有独立进程组，拒绝扩大终止范围")

    def resume(self, process: subprocess.Popen[str]) -> None:
        return None

    def terminate(
        self,
        process: subprocess.Popen[str],
        identity: dict[str, int],
    ) -> None:
        if process.pid != identity.get("pid"):
            raise RuntimeError("进程身份不一致，拒绝回收")
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return

    def close(self) -> None:
        return None


def _check(
    checks: list[dict[str, str]],
    check_id: str,
    ok: bool,
    detail: str,
) -> None:
    checks.append(
        {
            "id": check_id,
            "status": "PASS" if ok else "FAIL",
            "detail": detail,
        }
    )


def _read_json(path: Path, content: bytes | None = None) -> dict[str, object]:
    raw = stable_read_file(path) if content is None else content
    return parse_strict_json_object(raw, path.name)


def _walk_release(
    root: Path,
) -> tuple[list[str], list[str], list[str], list[str]]:
    files: list[str] = []
    directories: list[str] = []
    links: list[str] = []
    hardlinks: list[str] = []
    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        kept: list[str] = []
        for name in directory_names:
            child = current_path / name
            relative = child.relative_to(root).as_posix()
            metadata = os.lstat(child)
            if stat.S_ISLNK(metadata.st_mode) or has_reparse_flag(metadata):
                links.append(relative)
            else:
                kept.append(name)
                directories.append(relative)
        directory_names[:] = kept
        for name in file_names:
            child = current_path / name
            relative = child.relative_to(root).as_posix()
            metadata = os.lstat(child)
            if stat.S_ISLNK(metadata.st_mode) or has_reparse_flag(metadata):
                links.append(relative)
            else:
                files.append(relative)
                if int(getattr(metadata, "st_nlink", 1)) > 1:
                    hardlinks.append(relative)
    return (
        sorted(files),
        sorted(directories),
        sorted(links),
        sorted(hardlinks),
    )


def _expected_directories(files: Iterable[str]) -> list[str]:
    expected: set[str] = set()
    for relative in files:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            expected.add(parent.as_posix())
            parent = parent.parent
    return sorted(expected)


def _release_content_snapshot(
    root: Path,
    files: Iterable[str],
    *,
    total_limit: int,
) -> dict[str, bytes]:
    if (
        isinstance(total_limit, bool)
        or not isinstance(total_limit, int)
        or total_limit < 0
    ):
        raise ValueError("发布快照总量上限必须是非负整数")
    snapshot: dict[str, bytes] = {}
    remaining = total_limit
    for relative in files:
        content = stable_read_file(
            root.joinpath(*PurePosixPath(relative).parts),
            limit=remaining,
        )
        snapshot[relative] = content
        remaining -= len(content)
    return snapshot


def _preflight_release_size(
    root: Path,
    files: Iterable[str],
    *,
    size_limit: int,
) -> tuple[int, bool]:
    """只读元数据并在累计超限时立刻停止，不先加载任何文件内容。"""

    total = 0
    for relative in files:
        path = root.joinpath(*PurePosixPath(relative).parts)
        metadata = os.lstat(path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or has_reparse_flag(metadata)
        ):
            raise ValueError(f"发布快照只接受普通文件：{relative}")
        total += int(metadata.st_size)
        if total > size_limit:
            return total, True
    return total, False


def _early_release_failure(
    *,
    check_id: str,
    detail: str,
    total_bytes: int,
    size_limit: int,
    run_collections: bool,
) -> dict[str, object]:
    checks: list[dict[str, str]] = []
    _check(checks, check_id, False, detail)
    _check(
        checks,
        "required_test_collection",
        run_collections,
        (
            "必需测试收集已启用"
            if run_collections
            else "必需测试收集被禁用；结构诊断不能作为发布 PASS"
        ),
    )
    return {
        "schema": "cv-research-paperflow.release-check-result.v1",
        "ok": False,
        "total_bytes": total_bytes,
        "size_limit": size_limit,
        "checks": checks,
        "test_groups": [],
    }


def _release_content_state(
    snapshot: dict[str, bytes],
) -> dict[str, tuple[int, str]]:
    return {
        relative: (len(content), hashlib.sha256(content).hexdigest())
        for relative, content in snapshot.items()
    }


def _forbidden_payload_paths(paths: Iterable[str]) -> list[str]:
    forbidden: list[str] = []
    for raw in paths:
        pure = PurePosixPath(raw)
        lower_parts = tuple(part.casefold() for part in pure.parts)
        if lower_parts == ("paperflow", "security.md"):
            forbidden.append(raw)
            continue
        if any(part in _FORBIDDEN_DIRECTORY_NAMES for part in lower_parts):
            forbidden.append(raw)
            continue
        if pure.suffix.casefold() in _FORBIDDEN_SUFFIXES:
            forbidden.append(raw)
    return forbidden


def _contains_absolute_path(text: str) -> bool:
    return contains_private_absolute_path(text)


def _path_leaks(
    snapshot: dict[str, bytes],
    payload_paths: Iterable[str],
    *,
    protected_roots: Iterable[Path] = (),
) -> list[str]:
    leaks: list[str] = []
    protected = tuple(Path(item) for item in protected_roots)
    for relative in payload_paths:
        try:
            text = snapshot[relative].decode("utf-8", errors="strict")
        except (KeyError, UnicodeError):
            leaks.append(f"{relative}（不是严格 UTF-8 文本）")
            continue
        pure = PurePosixPath(relative)
        component = pure.parts[0] if pure.parts else "control"
        component_relative = (
            PurePosixPath(*pure.parts[1:]).as_posix()
            if len(pure.parts) > 1
            else relative
        )
        if content_contains_private_path(
            text,
            component=component,
            relative=component_relative,
            private_roots=protected,
        ):
            leaks.append(relative)
    return sorted(leaks)


def _validate_manifest(
    manifest: dict[str, object],
    actual_payload: list[str],
    snapshot: dict[str, bytes],
) -> list[str]:
    errors: list[str] = []
    allowed_keys = {
        "schema",
        "version",
        "payload_bytes",
        "file_count",
        "files",
        "control_files",
    }
    keys = set(manifest)
    if keys != allowed_keys:
        errors.append("release-manifest.json 字段不严格或含未知字段")
        return errors
    if manifest.get("schema") != "cv-research-paperflow.release-staging.v1":
        errors.append("release-manifest.json schema 无效")
    if manifest.get("version") != "1.0.0":
        errors.append("release-manifest.json version 无效")
    payload_bytes = manifest.get("payload_bytes")
    file_count = manifest.get("file_count")
    if (
        isinstance(payload_bytes, bool)
        or not isinstance(payload_bytes, int)
        or payload_bytes < 0
    ):
        errors.append("manifest payload_bytes 必须是非负整数")
    if (
        isinstance(file_count, bool)
        or not isinstance(file_count, int)
        or file_count < 0
    ):
        errors.append("manifest file_count 必须是非负整数")
    records = manifest.get("files")
    if not isinstance(records, list):
        errors.append("release-manifest.json files 必须是数组")
        return errors
    declared_paths: list[str] = []
    declared_total = 0
    normalized_records: list[tuple[str, int, str]] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            errors.append("release-manifest.json 文件条目字段无效")
            continue
        relative = record.get("path")
        size = record.get("size")
        digest = record.get("sha256")
        if (
            not isinstance(relative, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            errors.append("release-manifest.json 文件条目值无效")
            continue
        declared_paths.append(relative)
        declared_total += size
        normalized_records.append((relative, size, digest))
    try:
        validate_release_paths(declared_paths)
    except ValueError as error:
        # 必须先验证路径再拼接 root；否则恶意 ../ 条目可能让复查器读取暂存
        # 目录之外的文件。
        errors.append(f"manifest Windows 路径无效：{error}")
        return errors

    actual_set = set(actual_payload)
    for relative, size, digest in normalized_records:
        if relative not in actual_set:
            errors.append(f"manifest 声明文件缺失或不是普通 payload：{relative}")
            continue
        try:
            content = snapshot[relative]
        except KeyError:
            errors.append(f"manifest 声明文件在复查期间消失：{relative}")
            continue
        if len(content) != size:
            errors.append(f"文件体积与 manifest 不一致：{relative}")
        if hashlib.sha256(content).hexdigest() != digest:
            errors.append(f"文件 SHA-256 与 manifest 不一致：{relative}")
    if sorted(declared_paths) != sorted(actual_payload):
        errors.append("manifest 文件集合与实际 payload 不一致")
    if type(payload_bytes) is not int or payload_bytes != declared_total:
        errors.append("manifest payload_bytes 与逐文件体积不一致")
    if type(file_count) is not int or file_count != len(records):
        errors.append("manifest file_count 与文件条目数不一致")

    controls = manifest.get("control_files")
    if not isinstance(controls, list) or len(controls) != 1:
        errors.append("manifest control_files 必须精确声明一个排除清单")
    else:
        control = controls[0]
        if (
            not isinstance(control, dict)
            or set(control) != {"path", "size", "sha256"}
            or control.get("path") != EXCLUSIONS_NAME
            or isinstance(control.get("size"), bool)
            or not isinstance(control.get("size"), int)
            or int(control["size"]) < 0
            or not isinstance(control.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(control["sha256"]))
        ):
            errors.append("manifest control_files 条目无效")
        else:
            control_content = snapshot.get(EXCLUSIONS_NAME)
            if control_content is None:
                errors.append("manifest 声明的排除清单缺失")
            elif (
                len(control_content) != control["size"]
                or hashlib.sha256(control_content).hexdigest()
                != control["sha256"]
            ):
                errors.append("排除清单 control 体积或 SHA-256 不一致")
    return errors


def _download_policy_errors(downloads: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(downloads, list) or not downloads:
        return ["release-exclusions.json 缺少下载地址清单"]
    identifiers: list[str] = []
    required = {
        "id",
        "purpose",
        "url",
        "kind",
        "version",
        "revision",
        "sha256",
    }
    for item in downloads:
        if not isinstance(item, dict) or set(item) != required:
            errors.append("下载地址条目字段不严格")
            continue
        identifier = item.get("id")
        purpose = item.get("purpose")
        url = item.get("url")
        kind = item.get("kind")
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(purpose, str)
            or not purpose
            or not isinstance(url, str)
            or not url.startswith("https://")
            or kind not in {"landing_page", "artifact"}
        ):
            errors.append("下载地址条目值无效，必须使用固定 HTTPS 地址")
            continue
        identifiers.append(identifier)
        if kind == "landing_page":
            if any(item.get(name) is not None for name in ("version", "revision", "sha256")):
                errors.append("landing_page 不得伪造版本、revision 或 SHA-256")
        elif (
            not isinstance(item.get("version"), str)
            or not item["version"]
            or not isinstance(item.get("revision"), str)
            or not item["revision"]
            or not isinstance(item.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        ):
            errors.append("直接下载 artifact 必须固定版本、revision 和 SHA-256")
    if len(identifiers) != len(set(identifiers)):
        errors.append("下载地址 id 不得重复")
    return errors


def _validate_exclusions(exclusions: dict[str, object]) -> list[str]:
    errors: list[str] = []
    expected_fields = {
        "schema",
        "excluded",
        "excluded_patterns",
        "download_urls",
    }
    if set(exclusions) != expected_fields:
        errors.append("release-exclusions.json 字段不严格或缺少必需字段")
    if exclusions.get("schema") != "cv-research-paperflow.release-exclusions.v1":
        errors.append("release-exclusions.json schema 无效")
    downloads = exclusions.get("download_urls")
    errors.extend(_download_policy_errors(downloads))
    if downloads != DOWNLOAD_URLS:
        errors.append("下载地址清单与固定 allowlist 不一致（缺失、增加、重复或被改写）")
    excluded = exclusions.get("excluded")
    if not isinstance(excluded, list):
        errors.append("release-exclusions.json excluded 必须是数组")
    else:
        excluded_paths: list[str] = []
        for item in excluded:
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "reason", "recovery"}
                or not isinstance(item.get("path"), str)
                or not isinstance(item.get("reason"), str)
                or not isinstance(item.get("recovery"), str)
            ):
                errors.append("排除项条目字段无效")
                break
            excluded_paths.append(item["path"])
        try:
            validate_release_paths(excluded_paths)
        except ValueError as error:
            errors.append(f"排除项 Windows 路径无效：{error}")
    patterns = exclusions.get("excluded_patterns")
    if (
        not isinstance(patterns, list)
        or not all(isinstance(item, str) and item for item in patterns)
    ):
        errors.append("excluded_patterns 必须是非空字符串数组")
    return errors


def _test_root_configuration(
    snapshot: dict[str, bytes],
    actual_files: set[str],
) -> list[str]:
    errors: list[str] = []
    try:
        if "research/pyproject.toml" not in actual_files:
            raise OSError("科研 pyproject.toml 不是普通发布文件")
        research = tomllib.loads(
            snapshot["research/pyproject.toml"].decode("utf-8", errors="strict")
        )
        research_paths = (
            research.get("tool", {})
            .get("pytest", {})
            .get("ini_options", {})
            .get("testpaths")
        )
        if research_paths != ["tests"]:
            errors.append("科研 pytest testpaths 必须精确等于 tests/")
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, AttributeError):
        errors.append("无法核验科研 pyproject.toml 的 pytest testpaths")

    parser = configparser.ConfigParser()
    try:
        if "paperflow/pytest.ini" not in actual_files:
            raise OSError("PaperFlow pytest.ini 不是普通发布文件")
        parser.read_string(
            snapshot["paperflow/pytest.ini"].decode("utf-8", errors="strict")
        )
        paper_paths = parser.get("pytest", "testpaths").split()
        if paper_paths != ["tests_v2"]:
            errors.append("PaperFlow pytest testpaths 必须精确等于 tests_v2/")
    except (OSError, UnicodeError, configparser.Error, KeyError):
        errors.append("无法核验 PaperFlow pytest.ini 的 testpaths")
    return errors


def _offline_test_environment(seed: int) -> dict[str, str]:
    environment = safe_git_environment()
    for variable in tuple(environment):
        if variable.startswith("PYTHON") or variable in {
            "PYTEST_ADDOPTS",
            "PYTEST_PLUGINS",
        }:
            environment.pop(variable, None)
    environment.update(
        {
            "PYTHONHASHSEED": str(seed),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PIP_NO_INDEX": "1",
            "NO_PROXY": "*",
            "no_proxy": "*",
            "CUDA_VISIBLE_DEVICES": "",
        }
    )
    return environment


def process_is_alive(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _popen_process_group_options() -> dict[str, object]:
    if os.name == "nt":
        return {
            "creationflags": getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0x00000200,
            )
            | getattr(
                subprocess,
                "CREATE_SUSPENDED",
                0x00000004,
            )
        }
    return {"start_new_session": True}


def _start_owned_process(
    command: list[str],
    **kwargs: object,
) -> tuple[subprocess.Popen[str], object]:
    """先挂起 Windows 子进程，绑定内核所有权后才允许执行用户代码。"""

    owner: object
    if os.name == "nt":
        owner = _WindowsJobOwner()
    else:
        owner = _PosixProcessGroupOwner()
    process: subprocess.Popen[str] | None = None
    assigned = False
    cleanup_errors: list[str] = []
    try:
        process = subprocess.Popen(
            command,
            **kwargs,
            **_popen_process_group_options(),
        )
        owner.assign(process)
        assigned = True
        owner.resume(process)
        return process, owner
    except BaseException as error:
        if process is not None:
            try:
                if assigned:
                    identity = {
                        "pid": process.pid,
                        "started_monotonic_ns": time.monotonic_ns(),
                    }
                    owner.terminate(process, identity)
                elif process.poll() is None:
                    # Windows 此时仍处于 CREATE_SUSPENDED，尚未执行用户代码；
                    # 这里只回收这个未绑定父进程，不是一般运行阶段的 PID 降级。
                    process.kill()
                if process.poll() is None:
                    process.wait(timeout=5)
            except (OSError, RuntimeError, subprocess.SubprocessError) as cleanup:
                cleanup_errors.append(str(cleanup))
        try:
            owner.close()
        except (OSError, RuntimeError) as cleanup:
            cleanup_errors.append(str(cleanup))
        if cleanup_errors:
            raise RuntimeError(
                f"{error}；启动失败后的回收也失败：{'；'.join(cleanup_errors)}"
            ) from error
        raise


def _terminate_owned_process_tree(
    process: subprocess.Popen[str],
    identity: dict[str, int],
    owner: object,
) -> None:
    """只使用已绑定的内核所有权回收树，绝不按裸 PID 批量终止。"""

    if process.pid != identity.get("pid"):
        raise RuntimeError("进程身份不一致，拒绝回收")
    owner.terminate(process, identity)
    if process.poll() is None:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("已请求回收，但父进程未在 10 秒内退出") from error


_COLLECTION_LIMITATIONS = [
    "pytest collect-only 会导入测试模块并执行模块级代码，不是零执行操作",
    "只核对测试收集范围，没有执行测试函数；动态 runtime skip "
    "由后续 validate_release 执行阶段判定",
    "前后文件与目录快照只能发现发布树内可见污染，不能证明系统其他位置零副作用",
]


def _collection_start_failure(
    *,
    group_id: str,
    test_root: str,
    timeout_seconds: float,
    seed: int,
    started: float,
    error: BaseException,
) -> dict[str, object]:
    return {
        "id": group_id,
        "status": "COLLECTION_FAIL",
        "mode": "collect_only",
        "executed": False,
        "limitations": list(_COLLECTION_LIMITATIONS),
        "detail": f"受控测试收集进程启动失败：{error}",
        "timeout_seconds": timeout_seconds,
        "seed": seed,
        "timed_out": False,
        "skipped": 0,
        "skip_nodes": [],
        "return_code": None,
        "collected": [],
        "process_identity": None,
        "process_owner": None,
        "process_tree_cleanup": "NOT_STARTED",
        "output_limit_exceeded": False,
        "captured_output_bytes": 0,
        "duration_seconds": round(time.monotonic() - started, 3),
        "test_root": test_root,
    }


def run_test_group(
    *,
    group_id: str,
    cwd: Path,
    test_root: str,
    timeout_seconds: float,
    seed: int,
    command: list[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """收集固定测试根；导入副作用、超时、skip 与回收失败都拒绝放行。"""

    emit = progress or (lambda _message: None)
    selected_command = command or [
        sys.executable,
        "-I",
        "-B",
        "-c",
        _COLLECTION_WRAPPER,
        test_root,
    ]
    require_collection = command is None
    started = time.monotonic()
    emit(
        f"[START] {group_id}：timeout={timeout_seconds}s，seed={seed}，"
        f"root={test_root}"
    )
    process_started = time.monotonic_ns()
    try:
        process, owner = _start_owned_process(
            selected_command,
            cwd=cwd,
            env=_offline_test_environment(seed),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        result = _collection_start_failure(
            group_id=group_id,
            test_root=test_root,
            timeout_seconds=timeout_seconds,
            seed=seed,
            started=started,
            error=error,
        )
        emit(f"[COLLECTION_FAIL] {group_id}：{result['detail']}")
        return result
    process_identity = {
        "pid": process.pid,
        "started_monotonic_ns": process_started,
    }
    captured_chunks: list[str] = []
    captured_output_bytes = 0
    output_limit_exceeded = threading.Event()

    def read_output() -> None:
        nonlocal captured_output_bytes
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            encoded_size = len(chunk.encode("utf-8", errors="replace"))
            remaining = MAX_CAPTURED_OUTPUT_BYTES - captured_output_bytes
            if encoded_size <= remaining:
                captured_chunks.append(chunk)
                captured_output_bytes += encoded_size
            else:
                output_limit_exceeded.set()

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    timed_out = False
    cleanup_error = ""
    return_code: int | None = None
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
    try:
        _terminate_owned_process_tree(process, process_identity, owner)
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
        cleanup_error = cleanup_error or "输出读取线程未在 owned 进程树回收后结束"
    output = "".join(captured_chunks)
    lines = output.splitlines()
    for line in lines:
        emit(f"[{group_id}] {line}")
    skipped_matches = re.findall(r"(\d+)\s+skipped\b", output)
    skip_nodes = sorted(
        {
            line[len(_COLLECTION_SKIP_MARKER):].strip()
            for line in lines
            if line.startswith(_COLLECTION_SKIP_MARKER)
        }
    )
    marker_count = len(skip_nodes)
    skipped = max(
        [marker_count, *(int(value) for value in skipped_matches)],
        default=0,
    )
    collected = sorted(
        {
            line.strip().replace("\\", "/")
            for line in lines
            if "::" in line
            and not line.startswith(_COLLECTION_SKIP_MARKER)
            and not line.lstrip().startswith(("E ", "ERROR"))
        }
    )
    wrong_root = [
        node
        for node in collected
        if not node.startswith(test_root.rstrip("/") + "/")
    ]
    ok = (
        not timed_out
        and not cleanup_error
        and not output_limit_exceeded.is_set()
        and return_code == 0
        and skipped == 0
        and not wrong_root
        and (bool(collected) if require_collection else True)
    )
    detail_parts = []
    if timed_out:
        detail_parts.append(f"超过 {timeout_seconds} 秒")
    if cleanup_error:
        detail_parts.append(f"进程树回收失败：{cleanup_error}")
    if output_limit_exceeded.is_set():
        detail_parts.append(
            f"输出超过 {MAX_CAPTURED_OUTPUT_BYTES} 字节上限"
        )
    if return_code != 0:
        detail_parts.append(f"退出码 {return_code}")
    if skipped:
        detail_parts.append(f"{skipped} 个必需测试被跳过")
    if wrong_root:
        detail_parts.append("发现约定测试根之外的测试")
    if require_collection and not collected:
        detail_parts.append("没有发现必需测试")
    detail = "；".join(detail_parts) if detail_parts else f"发现 {len(collected)} 个测试"
    status = "COLLECTION_PASS" if ok else "COLLECTION_FAIL"
    emit(f"[{status}] {group_id}：{detail}")
    return {
        "id": group_id,
        "status": status,
        "mode": "collect_only",
        "executed": False,
        "limitations": list(_COLLECTION_LIMITATIONS),
        "detail": detail,
        "timeout_seconds": timeout_seconds,
        "seed": seed,
        "timed_out": timed_out,
        "skipped": skipped,
        "skip_nodes": skip_nodes,
        "return_code": return_code,
        "collected": collected,
        "process_identity": process_identity,
        "process_owner": owner.kind,
        "process_tree_cleanup": "FAIL" if cleanup_error else "PASS",
        "output_limit_exceeded": output_limit_exceeded.is_set(),
        "captured_output_bytes": captured_output_bytes,
        "duration_seconds": round(time.monotonic() - started, 3),
        "test_root": test_root,
    }


def run_release_checks(
    root: Path,
    *,
    run_collections: bool = True,
    size_limit: int = MAX_RELEASE_BYTES,
    progress: Callable[[str], None] | None = None,
    protected_roots: Iterable[Path] = (),
) -> dict[str, object]:
    lexical_root = Path(os.path.abspath(root))
    if not os.path.lexists(lexical_root):
        raise ValueError("发布复查根不存在")
    root_metadata = os.lstat(lexical_root)
    if stat.S_ISLNK(root_metadata.st_mode) or has_reparse_flag(root_metadata):
        raise ValueError("发布复查根不得是 link/reparse")
    release_root = lexical_root.resolve(strict=True)
    checks: list[dict[str, str]] = []
    test_groups: list[dict[str, object]] = []
    if not release_root.is_dir():
        raise ValueError("发布复查根必须是目录")

    actual_files, actual_directories, links, hardlinks = _walk_release(
        release_root
    )
    if len(actual_files) > MAX_RELEASE_FILES:
        return _early_release_failure(
            check_id="file_count",
            detail=(
                f"发布文件数 {len(actual_files)} 超过上限 "
                f"{MAX_RELEASE_FILES}"
            ),
            total_bytes=0,
            size_limit=size_limit,
            run_collections=run_collections,
        )
    try:
        preflight_total, preflight_exceeded = _preflight_release_size(
            release_root,
            actual_files,
            size_limit=size_limit,
        )
    except (OSError, ValueError) as error:
        return _early_release_failure(
            check_id="snapshot",
            detail=f"发布快照元数据预检失败：{error}",
            total_bytes=0,
            size_limit=size_limit,
            run_collections=run_collections,
        )
    if preflight_exceeded:
        return _early_release_failure(
            check_id="size",
            detail=(
                f"发布体积至少 {preflight_total} 字节，"
                f"超过上限 {size_limit}；未读取文件内容"
            ),
            total_bytes=preflight_total,
            size_limit=size_limit,
            run_collections=run_collections,
        )
    try:
        initial_snapshot = _release_content_snapshot(
            release_root,
            actual_files,
            total_limit=size_limit,
        )
    except (OSError, ValueError) as error:
        return _early_release_failure(
            check_id="snapshot",
            detail=f"发布快照稳定读取失败：{error}",
            total_bytes=preflight_total,
            size_limit=size_limit,
            run_collections=run_collections,
        )
    initial_content_state = _release_content_state(initial_snapshot)
    actual_file_set = set(actual_files)
    _check(
        checks,
        "links",
        not links,
        "未发现 link/reparse" if not links else f"发现 link/reparse：{links[:8]}",
    )
    _check(
        checks,
        "hardlinks",
        not hardlinks,
        (
            "未发现 hardlink"
            if not hardlinks
            else f"发现 hardlink：{hardlinks[:8]}"
        ),
    )
    try:
        validate_release_paths(actual_files + actual_directories + links)
    except ValueError as error:
        _check(checks, "windows_paths", False, f"Windows 路径无效：{error}")
    else:
        _check(checks, "windows_paths", True, "所有相对路径符合 Windows 规则")

    expected_directories = _expected_directories(actual_files)
    unexpected_directories = sorted(
        set(actual_directories) - set(expected_directories)
    )
    missing_directories = sorted(
        set(expected_directories) - set(actual_directories)
    )
    _check(
        checks,
        "directories",
        not unexpected_directories and not missing_directories,
        (
            "目录集合完全由已声明文件路径确定"
            if not unexpected_directories and not missing_directories
            else (
                f"出现未声明空目录：{unexpected_directories[:8]}；"
                f"缺少父目录：{missing_directories[:8]}"
            )
        ),
    )

    top_entries = {item.name for item in release_root.iterdir()}
    unknown_top = sorted(top_entries - _ALLOWED_TOP_LEVEL)
    missing_top = sorted(_ALLOWED_TOP_LEVEL - top_entries)
    _check(
        checks,
        "structure",
        not unknown_top and not missing_top,
        (
            "顶层结构完整"
            if not unknown_top and not missing_top
            else f"未知顶层：{unknown_top}；缺少顶层：{missing_top}"
        ),
    )

    payload_paths = [
        path
        for path in actual_files
        if path not in {MANIFEST_NAME, EXCLUSIONS_NAME}
    ]
    forbidden = _forbidden_payload_paths(payload_paths)
    _check(
        checks,
        "forbidden_content",
        not forbidden,
        (
            "未发现数据库、模型、缓存、数据集或运行/测试产物"
            if not forbidden
            else f"发现禁止的目录或数据库/模型/产物：{forbidden[:8]}"
        ),
    )

    total_bytes = sum(len(initial_snapshot[path]) for path in actual_files)
    _check(
        checks,
        "size",
        total_bytes <= size_limit,
        (
            f"发布体积 {total_bytes} 字节，不超过 {size_limit}"
            if total_bytes <= size_limit
            else f"发布体积 {total_bytes} 字节超过上限 {size_limit}"
        ),
    )

    manifest: dict[str, object] | None = None
    exclusions: dict[str, object] | None = None
    if MANIFEST_NAME not in actual_file_set:
        manifest_errors = ["release-manifest.json 必须是普通文件，不能是 link/reparse"]
    else:
        try:
            manifest = _read_json(
                release_root / MANIFEST_NAME,
                initial_snapshot[MANIFEST_NAME],
            )
            manifest_errors = _validate_manifest(
                manifest,
                payload_paths,
                initial_snapshot,
            )
        except ValueError as error:
            manifest_errors = [str(error)]
    _check(
        checks,
        "manifest",
        not manifest_errors,
        "逐文件体积和 SHA-256 均一致"
        if not manifest_errors
        else "；".join(manifest_errors[:8]),
    )

    if EXCLUSIONS_NAME not in actual_file_set:
        exclusion_errors = [
            "release-exclusions.json 必须是普通文件，不能是 link/reparse"
        ]
    else:
        try:
            exclusions = _read_json(
                release_root / EXCLUSIONS_NAME,
                initial_snapshot[EXCLUSIONS_NAME],
            )
            exclusion_errors = _validate_exclusions(exclusions)
        except ValueError as error:
            exclusion_errors = [str(error)]
    _check(
        checks,
        "exclusions",
        not exclusion_errors,
        "排除项和下载地址清单可用"
        if not exclusion_errors
        else "；".join(exclusion_errors[:8]),
    )

    leaks = _path_leaks(
        initial_snapshot,
        actual_files,
        protected_roots=(
            lexical_root.parent,
            release_root.parent,
            *protected_roots,
        ),
    )
    _check(
        checks,
        "absolute_paths",
        not leaks,
        "未发现机器绝对路径泄漏"
        if not leaks
        else f"发现绝对路径泄漏：{leaks[:8]}",
    )

    configuration_errors = _test_root_configuration(
        initial_snapshot,
        actual_file_set,
    )
    _check(
        checks,
        "test_roots",
        not configuration_errors,
        "科研只收集 tests/，PaperFlow 只收集 tests_v2/"
        if not configuration_errors
        else "；".join(configuration_errors),
    )
    _check(
        checks,
        "required_test_collection",
        run_collections,
        (
            "必需测试收集已启用"
            if run_collections
            else "必需测试收集被禁用；结构诊断不能作为发布 PASS"
        ),
    )

    structural_ok = all(item["status"] == "PASS" for item in checks)
    if run_collections and structural_ok:
        emit = progress or (lambda _message: None)
        test_groups = [
            run_test_group(
                group_id="research",
                cwd=release_root / "research",
                test_root="tests",
                timeout_seconds=60,
                seed=1901,
                progress=emit,
            ),
            run_test_group(
                group_id="paperflow",
                cwd=release_root / "paperflow",
                test_root="tests_v2",
                timeout_seconds=60,
                seed=1907,
                progress=emit,
            ),
        ]
    try:
        (
            final_files,
            final_directories,
            final_links,
            final_hardlinks,
        ) = _walk_release(release_root)
        final_snapshot = _release_content_snapshot(
            release_root,
            final_files,
            total_limit=size_limit,
        )
        final_content_state = _release_content_state(final_snapshot)
        unchanged = (
            final_files == actual_files
            and final_directories == actual_directories
            and final_links == links
            and final_hardlinks == hardlinks
            and final_content_state == initial_content_state
        )
        immutable_detail = (
            "复查过程未改变发布目录的文件/目录集合、体积或 SHA-256"
            if unchanged
            else "复查过程改变了发布目录的文件或目录，拒绝使用已污染的暂存结果"
        )
    except (OSError, ValueError) as error:
        unchanged = False
        immutable_detail = f"结束复扫失败，发布目录可能已变化：{error}"
    _check(
        checks,
        (
            "post_collection_immutable"
            if run_collections and structural_ok
            else "post_check_immutable"
        ),
        unchanged,
        immutable_detail,
    )
    collections_ok = all(
        item["status"] == "COLLECTION_PASS" for item in test_groups
    ) if run_collections and structural_ok else False
    ok = all(item["status"] == "PASS" for item in checks) and collections_ok
    return {
        "schema": "cv-research-paperflow.release-check-result.v1",
        "ok": ok,
        "total_bytes": total_bytes,
        "size_limit": size_limit,
        "checks": checks,
        "test_groups": test_groups,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="复查清洁发布暂存目录。")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument(
        "--protected-root",
        action="append",
        type=Path,
        default=[],
        help="显式保护的禁止写入和隐私根目录；可重复提供。",
    )
    parser.add_argument(
        "--no-collect",
        action="store_true",
        help="只做结构检查；正式放行不得使用此参数。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_release_checks(
            args.root,
            run_collections=not args.no_collect,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
            protected_roots=args.protected_root,
        )
        if args.no_collect and report.get("ok") is True:
            report["ok"] = False
            report["checks"].append(
                {
                    "id": "required_collection",
                    "status": "FAIL",
                    "detail": "--no-collect 只用于诊断，不能作为正式放行结果",
                }
            )
    except (OSError, ValueError) as error:
        report = {
            "schema": "cv-research-paperflow.release-check-result.v1",
            "ok": False,
            "error": str(error),
            "checks": [],
            "test_groups": [],
        }
    if args.json_out:
        try:
            write_new_json_report(
                args.json_out,
                report,
                forbidden_roots=(args.root, *args.protected_root),
            )
        except (OSError, ValueError) as error:
            print(
                json.dumps(
                    {
                        "schema": "cv-research-paperflow.release-check-result.v1",
                        "ok": False,
                        "error": f"复查报告未写入：{error}",
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
