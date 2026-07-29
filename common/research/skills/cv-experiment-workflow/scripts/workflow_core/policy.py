from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .io import (
    DEFAULT_JSON_LIMIT,
    atomic_write_json,
    read_bounded_json_object,
    read_bounded_regular_file as _read_bounded_regular_file,
)
from .locking import project_write_lock
from .project import PROJECT_SCHEMA_V2, is_link_or_reparse
from .records import _initialized_project, _validate_project, normalize_safe_relative_path


ADAPTER_SCHEMA_V1 = "cv-experiment-workflow.adapter.v1"
ADAPTER_SCHEMA_V2 = "cv-experiment-workflow.adapter.v2"
ADAPTER_STATUSES = {"unbound", "bound"}
DEFAULT_CONFIRMATION_POLICY = {
    "minimum_accepted_attempts": 1,
    "minimum_distinct_seeds": 1,
}
GIT_TIMEOUT_SECONDS = 15
GIT_OUTPUT_LIMIT = DEFAULT_JSON_LIMIT


class _RuntimeAdapterBlock(ValueError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def load_adapter(control: Path) -> dict[str, Any]:
    path = control / "adapter.json"
    if is_link_or_reparse(path) or not path.is_file():
        raise ValueError(f"adapter.json 不是项目内普通文件：{path}")
    adapter = read_bounded_json_object(path)
    schema = adapter.get("schema")
    expected = {"schema", "status", "code_sources", "capabilities"}
    if schema == ADAPTER_SCHEMA_V2:
        expected.add("confirmation_policy")
    sources = adapter.get("code_sources")
    capabilities = adapter.get("capabilities")
    if (
        schema not in {ADAPTER_SCHEMA_V1, ADAPTER_SCHEMA_V2}
        or set(adapter) != expected
        or adapter.get("status") not in ADAPTER_STATUSES
        or not isinstance(sources, list)
        or not isinstance(capabilities, dict)
        or not all(
            isinstance(name, str) and bool(name.strip()) and name == name.strip()
            and isinstance(enabled, bool)
            for name, enabled in capabilities.items()
        )
    ):
        raise ValueError("adapter.json schema/status/code_sources/capabilities 无效")
    for source in sources:
        if (
            not isinstance(source, dict)
            or set(source) != {"repo_url", "commit", "relative_path"}
            or not isinstance(source.get("repo_url"), str)
            or not source["repo_url"].strip()
            or source["repo_url"] != source["repo_url"].strip()
            or not isinstance(source.get("commit"), str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", source["commit"]) is None
        ):
            raise ValueError("adapter.json code_sources 无效")
        normalized = normalize_safe_relative_path(
            source.get("relative_path"), "adapter relative_path",
        )
        if source["relative_path"] != normalized:
            raise ValueError("adapter.json relative_path 未规范化")
    if schema == ADAPTER_SCHEMA_V2:
        validate_confirmation_policy(adapter.get("confirmation_policy"))
    return adapter


def load_runtime_adapter_spec(project: Path) -> dict[str, Any]:
    """在同一项目锁内校验并读取唯一 Adapter，返回不可变源码快照。"""
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        adapter = load_adapter(control)
        return _runtime_adapter_spec_locked(root, adapter)


def audit_runtime_adapter_locked(
    root: Path,
    adapter: dict[str, Any],
) -> dict[str, Any]:
    """调用方持锁时只读核验 Adapter；失败只形成控制台 blocker。"""
    try:
        spec = _runtime_adapter_spec_locked(Path(root), adapter)
    except _RuntimeAdapterBlock as error:
        return {
            "status": "block",
            "reason": error.reason,
        }
    except (OSError, ValueError):
        return {
            "status": "block",
            "reason": "runtime_verification_failed",
        }
    return {
        "status": "pass",
        "reason": None,
        "source": spec["source"],
        "repo_url": spec["repo_url"],
        "commit": spec["commit"],
    }


def _runtime_adapter_spec_locked(
    root: Path,
    adapter: dict[str, Any],
) -> dict[str, Any]:
    if adapter["status"] != "bound":
        raise _RuntimeAdapterBlock(
            "adapter_unbound",
            "项目 Adapter 尚未绑定",
        )
    if adapter["capabilities"].get("workflow_adapter") is not True:
        raise _RuntimeAdapterBlock(
            "capability_missing",
            "项目未声明 workflow_adapter 能力",
        )
    sources = adapter["code_sources"]
    if len(sources) != 1:
        raise _RuntimeAdapterBlock(
            "code_source_count_invalid",
            "运行 Adapter 必须声明且只声明一个 code_source",
        )
    source = sources[0]
    return _read_adapter_source_spec(
        root,
        source["relative_path"],
        source["repo_url"],
        source["commit"],
    )


def _read_adapter_source_spec(
    root: Path,
    relative_path: object,
    repo_url: object,
    commit: object,
) -> dict[str, Any]:
    relative = normalize_safe_relative_path(relative_path, "adapter relative_path")
    if not relative.endswith(".py"):
        raise ValueError("运行 Adapter 必须是 Python 源文件")
    if not isinstance(repo_url, str) or not repo_url.strip() or repo_url != repo_url.strip():
        raise ValueError("Adapter repo_url 无效")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("Adapter commit 无效")
    candidate = root / relative
    resolved_root = root.resolve(strict=True)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise ValueError("运行 Adapter 必须位于项目目录内") from error
    _validate_adapter_ancestors(root, relative)
    source_bytes = _read_bounded_regular_file(
        resolved, DEFAULT_JSON_LIMIT, "workflow adapter"
    )
    _verify_adapter_git_binding(root, relative, commit, source_bytes)
    try:
        resolved_after = candidate.resolve(strict=True)
        resolved_after.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise ValueError("运行 Adapter 读取后不再位于项目目录内") from error
    _validate_adapter_ancestors(root, relative)
    if resolved_after != resolved:
        raise ValueError("运行 Adapter 读取期间路径发生变化")
    source_bytes_after = _read_bounded_regular_file(
        resolved_after, DEFAULT_JSON_LIMIT, "workflow adapter"
    )
    if source_bytes_after != source_bytes:
        raise ValueError("运行 Adapter 读取期间内容发生变化")
    _verify_adapter_git_binding(root, relative, commit, source_bytes_after)
    return {
        "source_bytes": bytes(source_bytes),
        "source": relative,
        "display_path": str(resolved),
        "repo_url": repo_url,
        "commit": commit,
    }


def _verify_adapter_git_binding(
    root: Path,
    relative: str,
    commit: str,
    source_bytes: bytes,
) -> None:
    top_level = Path(
        _git_text(root, "rev-parse", "--show-toplevel", label="Git 顶层目录")
    )
    try:
        if top_level.resolve(strict=True) != root.resolve(strict=True):
            raise ValueError("v2 项目必须正好是运行 Adapter 所属 Git 顶层目录")
    except OSError as error:
        raise ValueError("无法解析运行 Adapter 的 Git 顶层目录") from error

    head = _git_text(root, "rev-parse", "HEAD", label="Git HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise ValueError("运行 Adapter Git HEAD 不是规范 40 位小写 commit")
    resolved_commit = _git_text(
        root,
        "rev-parse",
        "--verify",
        f"{commit}^{{commit}}",
        label="Adapter commit",
    )
    if resolved_commit != commit:
        raise ValueError("Adapter commit 不能精确解析为声明的提交")
    ancestor = _git_process(
        root,
        "merge-base",
        "--is-ancestor",
        commit,
        head,
        label="Adapter commit 祖先关系",
        accepted_returncodes={0, 1},
    )
    if ancestor.returncode != 0:
        raise ValueError("Adapter commit 不是当前 HEAD 的祖先")

    tree = _git_bytes(
        root,
        "ls-tree",
        "-z",
        commit,
        "--",
        relative,
        label="Adapter Git 文件类型",
    )
    records = [record for record in tree.split(b"\0") if record]
    if len(records) != 1 or b"\t" not in records[0]:
        raise ValueError("Adapter relative_path 无法在声明 commit 中唯一定位")
    metadata, raw_path = records[0].split(b"\t", 1)
    parts = metadata.split()
    if (
        len(parts) != 3
        or parts[0] not in {b"100644", b"100755"}
        or parts[1] != b"blob"
        or raw_path != relative.encode("utf-8")
    ):
        raise ValueError(
            "Adapter Git 路径必须是 mode 100644/100755 且 type=blob"
        )

    _require_only_ledger_dirty(root)
    object_name = f"{commit}:{relative}"
    size_text = _git_text(
        root, "cat-file", "-s", object_name, label="Adapter Git blob 大小"
    )
    try:
        size = int(size_text)
    except ValueError as error:
        raise ValueError("Adapter Git blob 大小无效") from error
    if not 0 <= size <= DEFAULT_JSON_LIMIT:
        raise ValueError("Adapter Git blob 超过大小限制")
    committed_bytes = _git_bytes(
        root, "cat-file", "blob", object_name, label="Adapter Git blob"
    )
    if len(committed_bytes) != size:
        raise ValueError("Adapter Git blob 读取长度不一致")
    if committed_bytes != source_bytes:
        raise ValueError("当前 Adapter 文件与声明 commit 中的 bytes 不一致")
    if _git_text(root, "rev-parse", "HEAD", label="Git HEAD 复核") != head:
        raise ValueError("Adapter 核验期间 Git HEAD 发生变化")
    _require_only_ledger_dirty(root)
    _require_no_ignored_code(root)


def _require_only_ledger_dirty(root: Path) -> None:
    status = _git_bytes(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        ".",
        ":(exclude).experiment-workflow/**",
        label="Adapter 项目工作树",
    )
    if status:
        raise _RuntimeAdapterBlock(
            "worktree_dirty",
            "运行 Adapter 项目工作树除 .experiment-workflow 账本外必须干净",
        )


def _require_no_ignored_code(root: Path) -> None:
    risky_extensions = (
        "py",
        "pyc",
        "pyo",
        "pyd",
        "so",
        "dll",
        "exe",
        "com",
        "bat",
        "cmd",
        "ps1",
        "psm1",
        "scr",
        "msi",
        "sh",
        "bash",
        "zsh",
        "fish",
        "js",
        "mjs",
        "cjs",
        "jar",
        "class",
        "rb",
        "pl",
        "php",
        "lua",
    )
    pathspecs = [
        pattern
        for extension in risky_extensions
        for pattern in (
            f":(glob,icase)*.{extension}",
            f":(glob,icase)**/*.{extension}",
        )
    ]
    ignored = _git_bytes(
        root,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        "--",
        *pathspecs,
        ":(exclude).experiment-workflow/**",
        label="Adapter ignored code",
    )
    if not ignored:
        return
    try:
        paths = [
            item.decode("utf-8", errors="strict")
            for item in ignored.split(b"\0")
            if item
        ]
    except UnicodeDecodeError as error:
        raise ValueError("Adapter ignored code 路径不是严格 UTF-8") from error
    sample = "、".join(paths[:3])
    raise _RuntimeAdapterBlock(
        "ignored_executable_code",
        f"运行 Adapter 项目存在被忽略的可执行代码：{sample}",
    )


def _git_text(root: Path, *arguments: str, label: str) -> str:
    result = _git_process(root, *arguments, label=label)
    try:
        return result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} 输出不是严格 UTF-8") from error


def _git_bytes(root: Path, *arguments: str, label: str) -> bytes:
    return bytes(_git_process(root, *arguments, label=label).stdout)


def _git_process(
    root: Path,
    *arguments: str,
    label: str,
    accepted_returncodes: set[int] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return _run_bounded_process(
        ["git", "-C", str(root), *arguments],
        label=label,
        timeout_seconds=GIT_TIMEOUT_SECONDS,
        accepted_returncodes=accepted_returncodes,
        environment=environment,
    )


class _WindowsJob:
    __slots__ = (
        "_assign_process",
        "_close_handle",
        "_closed",
        "_handle",
        "_query_information",
        "_accounting_type",
        "_resume_process",
        "_terminate_job",
    )

    def __init__(
        self,
        handle: Any,
        *,
        assign_process: Any,
        close_handle: Any,
        query_information: Any,
        accounting_type: Any,
        resume_process: Any,
        terminate_job: Any,
    ) -> None:
        self._handle = handle
        self._assign_process = assign_process
        self._close_handle = close_handle
        self._query_information = query_information
        self._accounting_type = accounting_type
        self._resume_process = resume_process
        self._terminate_job = terminate_job
        self._closed = False

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        import ctypes

        if not self._assign_process(self._handle, process._handle):
            error = ctypes.get_last_error()
            raise OSError(error, "AssignProcessToJobObject 失败")

    def resume(self, process: subprocess.Popen[bytes]) -> None:
        status = int(self._resume_process(process._handle))
        if status != 0:
            raise OSError(
                f"NtResumeProcess 失败，NTSTATUS=0x{status & 0xFFFFFFFF:08x}"
            )

    def close(self, deadline: float | None = None) -> None:
        import ctypes

        if self._closed:
            return
        failure: BaseException | None = None
        try:
            if deadline is not None:
                if not self._terminate_job(self._handle, 1):
                    error = ctypes.get_last_error()
                    failure = OSError(error, "TerminateJobObject 失败")
                else:
                    while True:
                        accounting = self._accounting_type()
                        if not self._query_information(
                            self._handle,
                            1,
                            ctypes.byref(accounting),
                            ctypes.sizeof(accounting),
                            None,
                        ):
                            error = ctypes.get_last_error()
                            failure = OSError(
                                error,
                                "QueryInformationJobObject 失败",
                            )
                            break
                        if int(accounting.ActiveProcesses) == 0:
                            break
                        remaining = _remaining_time(deadline)
                        if remaining <= 0:
                            failure = ValueError(
                                "Job Object 未在超时预算内清空"
                            )
                            break
                        time.sleep(min(0.005, remaining))
        finally:
            if not self._close_handle(self._handle):
                error = ctypes.get_last_error()
                failure = OSError(error, "CloseHandle(Job Object) 失败")
            else:
                self._closed = True
        if failure is not None:
            raise failure


def _create_windows_job() -> _WindowsJob:
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
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

    class BasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    create_job = kernel32.CreateJobObjectW
    create_job.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    create_job.restype = wintypes.HANDLE
    set_information = kernel32.SetInformationJobObject
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    set_information.restype = wintypes.BOOL
    assign_process = kernel32.AssignProcessToJobObject
    assign_process.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    assign_process.restype = wintypes.BOOL
    query_information = kernel32.QueryInformationJobObject
    query_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query_information.restype = wintypes.BOOL
    terminate_job = kernel32.TerminateJobObject
    terminate_job.argtypes = [wintypes.HANDLE, wintypes.UINT]
    terminate_job.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    resume_process = ntdll.NtResumeProcess
    resume_process.argtypes = [wintypes.HANDLE]
    resume_process.restype = wintypes.LONG

    handle = create_job(None, None)
    if not handle:
        error = ctypes.get_last_error()
        raise OSError(error, "CreateJobObjectW 失败")
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00002000
    if not set_information(
        handle,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        close_handle(handle)
        raise OSError(error, "设置 KILL_ON_JOB_CLOSE 失败")
    return _WindowsJob(
        handle,
        assign_process=assign_process,
        close_handle=close_handle,
        query_information=query_information,
        accounting_type=BasicAccountingInformation,
        resume_process=resume_process,
        terminate_job=terminate_job,
    )


def _run_bounded_process(
    command: list[str],
    *,
    label: str,
    timeout_seconds: float,
    accepted_returncodes: set[int] | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    deadline = time.monotonic() + timeout_seconds
    cleanup_reserve = min(0.25, max(0.02, timeout_seconds * 0.1))
    execution_deadline = max(time.monotonic(), deadline - cleanup_reserve)
    process: subprocess.Popen[bytes] | None = None
    windows_job: _WindowsJob | None = None
    tree_closed = False
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()
    reader_failed = threading.Event()
    reader_errors: list[BaseException] = []

    def collect(stream: Any, destination: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                remaining = GIT_OUTPUT_LIMIT + 1 - len(destination)
                if remaining > 0:
                    destination.extend(chunk[:remaining])
                if len(destination) > GIT_OUTPUT_LIMIT or len(chunk) > remaining:
                    overflow.set()
                    return
        except BaseException as error:
            reader_errors.append(error)
            reader_failed.set()

    readers: list[threading.Thread] = []
    try:
        process, windows_job = _start_bounded_process(
            command,
            label=label,
            deadline=deadline,
            environment=environment,
        )
        if process.stdout is None or process.stderr is None:
            raise ValueError(f"{label} Git 输出管道创建失败")
        readers = [
            threading.Thread(
                target=collect,
                args=(process.stdout, stdout),
                name=f"{label}-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=collect,
                args=(process.stderr, stderr),
                name=f"{label}-stderr",
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        failure: str | None = None
        while process.poll() is None:
            if overflow.is_set():
                failure = "输出超过大小限制"
                break
            if reader_failed.is_set():
                failure = "输出读取失败"
                break
            remaining = execution_deadline - time.monotonic()
            if remaining <= 0:
                failure = "校验超时"
                break
            overflow.wait(min(0.005, remaining))

        _terminate_process_tree(
            process,
            deadline=deadline,
            windows_job=windows_job,
        )
        tree_closed = True
        returncode = process.returncode
        if returncode is None:
            raise ValueError(f"{label} Git 进程未在超时预算内退出")
        for reader in readers:
            reader.join(timeout=_remaining_time(deadline))
        if any(reader.is_alive() for reader in readers):
            raise ValueError(f"{label} Git 输出读取线程未在超时预算内退出")
        if reader_errors:
            raise ValueError(f"{label} Git 输出读取失败：{reader_errors[0]}")
        if failure is not None:
            raise ValueError(f"{label} Git {failure}")
        if overflow.is_set():
            raise ValueError(f"{label} Git 输出超过大小限制")
        result = subprocess.CompletedProcess(
            command,
            returncode,
            stdout=bytes(stdout),
            stderr=bytes(stderr),
        )
        accepted = {0} if accepted_returncodes is None else accepted_returncodes
        if result.returncode not in accepted:
            try:
                detail = (result.stderr or result.stdout).decode(
                    "utf-8", errors="strict"
                ).strip()
            except UnicodeDecodeError as error:
                raise ValueError(
                    f"{label} Git 错误输出不是严格 UTF-8"
                ) from error
            raise ValueError(
                f"{label} Git 校验失败："
                f"{detail or f'exit {result.returncode}'}"
            )
        return result
    finally:
        if process is not None and not tree_closed:
            _terminate_process_tree(
                process,
                deadline=deadline,
                windows_job=windows_job,
            )
        for reader in readers:
            reader.join(timeout=_remaining_time(deadline))
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def _start_bounded_process(
    command: list[str],
    *,
    label: str,
    deadline: float,
    environment: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[bytes], _WindowsJob | None]:
    windows_job: _WindowsJob | None = None
    popen_options: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if environment is not None:
        popen_options["env"] = environment
    if os.name == "nt":
        try:
            windows_job = _create_windows_job()
        except OSError as error:
            raise ValueError(
                f"{label} Git Windows 进程隔离创建失败：{error}"
            ) from error
        popen_options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | 0x00000004  # CREATE_SUSPENDED
        )
    else:
        popen_options["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **popen_options)
    except OSError as error:
        if windows_job is not None:
            windows_job.close()
        raise ValueError(f"{label} Git 校验失败：{error}") from error
    if windows_job is None:
        return process, None
    try:
        windows_job.assign(process)
        windows_job.resume(process)
    except OSError as error:
        cleanup_error: BaseException | None = None
        try:
            windows_job.close(deadline)
        except BaseException as caught:
            cleanup_error = caught
        try:
            if process.poll() is None:
                process.kill()
            _wait_process_until(process, deadline)
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        if cleanup_error is not None:
            raise ValueError(
                f"{label} Git Windows 进程隔离清理失败：{cleanup_error}"
            ) from cleanup_error
        raise ValueError(
            f"{label} Git Windows 进程隔离失败：{error}"
        ) from error
    return process, windows_job


def _remaining_time(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _wait_process_until(
    process: subprocess.Popen[bytes],
    deadline: float,
) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        process.wait(timeout=_remaining_time(deadline))
        return
    except subprocess.TimeoutExpired:
        process.kill()
    try:
        process.wait(timeout=_remaining_time(deadline))
    except subprocess.TimeoutExpired as error:
        raise ValueError("Git 进程未在超时预算内退出") from error


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    *,
    deadline: float,
    windows_job: _WindowsJob | None,
) -> None:
    if os.name == "nt":
        if windows_job is None:
            if process.poll() is None:
                process.kill()
        else:
            try:
                windows_job.close(deadline)
            except BaseException:
                if process.poll() is None:
                    process.kill()
                _wait_process_until(process, deadline)
                raise
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _wait_process_until(process, deadline)


def _validate_adapter_ancestors(root: Path, relative_path: str) -> None:
    current = root
    for part in Path(relative_path).parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError as error:
            raise ValueError("运行 Adapter 路径不存在") from error
        if is_link_or_reparse(current) or (
            current != root / relative_path and not stat.S_ISDIR(info.st_mode)
        ):
            raise ValueError("运行 Adapter 路径不能包含链接或重解析点")


def get_confirmation_policy(control: Path) -> dict[str, int]:
    adapter = load_adapter(control)
    if adapter["schema"] == ADAPTER_SCHEMA_V1:
        return dict(DEFAULT_CONFIRMATION_POLICY)
    return dict(adapter["confirmation_policy"])


def validate_confirmation_policy(value: object) -> None:
    if not isinstance(value, dict) or set(value) != set(DEFAULT_CONFIRMATION_POLICY):
        raise ValueError("adapter confirmation_policy 结构无效")
    for name, number in value.items():
        if type(number) is not int or not 1 <= number <= 100:
            raise ValueError(f"adapter confirmation_policy {name} 必须是 1..100 的整数")


def set_confirmation_policy(
    project: Path,
    minimum_accepted_attempts: int,
    minimum_distinct_seeds: int,
) -> dict[str, int]:
    requested = {
        "minimum_accepted_attempts": minimum_accepted_attempts,
        "minimum_distinct_seeds": minimum_distinct_seeds,
    }
    validate_confirmation_policy(requested)
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root)
        from .attempts import validate_project_workflow_locked

        validate_project_workflow_locked(control)
        adapter = load_adapter(control)
        if (
            adapter["schema"] == ADAPTER_SCHEMA_V2
            and adapter["confirmation_policy"] == requested
        ):
            return dict(requested)
        upgraded = json.loads(json.dumps(adapter))
        upgraded["schema"] = ADAPTER_SCHEMA_V2
        upgraded["confirmation_policy"] = requested
        load_candidate = control / "adapter.json"
        # 先在内存中按与读取器相同的严格规则验证，随后单文件原子替换。
        _validate_adapter_payload(upgraded)
        atomic_write_json(load_candidate, upgraded, transaction_id=uuid.uuid4().hex)
        return dict(requested)


def _validate_adapter_payload(adapter: dict[str, Any]) -> None:
    if adapter.get("schema") != ADAPTER_SCHEMA_V2:
        raise ValueError("adapter upgrade schema 无效")
    expected = {"schema", "status", "code_sources", "capabilities", "confirmation_policy"}
    if set(adapter) != expected:
        raise ValueError("adapter upgrade 字段无效")
    validate_confirmation_policy(adapter["confirmation_policy"])
    # 复用 JSON 编码可验证不存在非 JSON 值；其余旧字段已经由 load_adapter 校验。
    json.dumps(adapter, ensure_ascii=False, allow_nan=False)
