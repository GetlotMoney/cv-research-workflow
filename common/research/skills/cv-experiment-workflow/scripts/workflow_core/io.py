"""工作流原子 I/O 的 v1 威胁模型。

协作写入者应遵守 project lock；v1 处理静态 symlink/reparse、崩溃、
KeyboardInterrupt、未知目标不覆盖和异常路径不误删。同一账户的非协作进程若在
相邻系统调用之间换绑路径，属于范围外威胁；best-effort 身份检查只用于审计，
不构成安全隔离承诺。
"""

from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, Mapping


FileIdentity = tuple[int, int]
DEFAULT_JSON_LIMIT = 1024 * 1024


def read_bounded_regular_file(
    path: Path,
    limit: int,
    label: str,
    *,
    _expected_before: os.stat_result | None = None,
) -> bytes:
    """在稳定的普通文件身份下读取至多 ``limit`` 字节。"""
    path = Path(path)
    before = _bounded_regular_file_stat(path, limit, label)
    if _expected_before is not None and (
        not os.path.samestat(before, _expected_before)
        or before.st_size != _expected_before.st_size
        or before.st_mtime_ns != _expected_before.st_mtime_ns
        or before.st_ctime_ns != _expected_before.st_ctime_ns
    ):
        raise ValueError(f"{label} 文件身份与初次枚举不一致：{path}")

    try:
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
                raise ValueError(f"{label} 文件身份在读取前发生变化：{path}")
            if opened.st_size > limit:
                raise ValueError(f"{label} 超过 {_format_size_limit(limit)} 上限：{path}")
            content = source.read(limit + 1)
            after_read = os.fstat(source.fileno())
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(f"无法读取普通文件 {label}：{path}") from error

    try:
        current = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} 读取后身份不可确认：{path}") from error
    if len(content) > limit or after_read.st_size > limit:
        raise ValueError(f"{label} 超过 {_format_size_limit(limit)} 上限：{path}")
    if (
        not stat.S_ISREG(after_read.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or _windows_stat_is_reparse(current)
        or not stat.S_ISREG(current.st_mode)
        or not os.path.samestat(opened, after_read)
        or not os.path.samestat(before, current)
    ):
        raise ValueError(f"{label} 文件身份在读取期间发生变化：{path}")
    return content


def validate_bounded_regular_file(path: Path, limit: int, label: str) -> None:
    """只检查普通文件身份与大小，不读取正文。"""
    _bounded_regular_file_stat(Path(path), limit, label)


def _bounded_regular_file_stat(
    path: Path, limit: int, label: str
) -> os.stat_result:
    try:
        before = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} 不是普通文件：{path}") from error
    if (
        stat.S_ISLNK(before.st_mode)
        or _windows_stat_is_reparse(before)
        or not stat.S_ISREG(before.st_mode)
    ):
        raise ValueError(f"{label} 必须是普通文件：{path}")
    if before.st_size > limit:
        raise ValueError(f"{label} 超过 {_format_size_limit(limit)} 上限：{path}")
    return before


def parse_json_object_bytes(content: bytes, label: str) -> dict[str, Any]:
    """严格解析 UTF-8 JSON object，并拒绝 NaN/Infinity。"""
    try:
        payload = json.loads(
            content.decode("utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} 必须是普通 UTF-8 JSON object") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} 必须是 JSON object")
    return payload


def read_bounded_json_object(
    path: Path,
    limit: int = DEFAULT_JSON_LIMIT,
    label: str | None = None,
) -> dict[str, Any]:
    """有界读取并严格解析顶层为 object 的 JSON 文件。"""
    path = Path(path)
    display_label = label or path.name
    content = read_bounded_regular_file(path, limit, display_label)
    return parse_json_object_bytes(content, display_label)


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"JSON 非有限数值不受支持：{constant}")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"重复 JSON 键：{key}")
        payload[key] = value
    return payload


def _format_size_limit(limit: int) -> str:
    if limit == 64 * 1024:
        return "64 KiB"
    if limit == 1024 * 1024:
        return "1 MiB"
    return f"{limit} bytes"


class ParentDirectoryAnchor:
    """在一次原子操作期间固定目标父目录，并提供同目录操作。"""

    def __init__(
        self,
        path: Path,
        *,
        directory_fd: int | None = None,
        windows_handle: int | None = None,
    ) -> None:
        self.path = _absolute_lexical_path(path)
        self.directory_fd = directory_fd
        self.windows_handle = windows_handle

    def open_exclusive(self, name: str) -> BinaryIO:
        _validate_child_name(name)
        if self.directory_fd is None:
            return (self.path / name).open("xb")
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=self.directory_fd,
        )
        return os.fdopen(descriptor, "wb")

    def open_unique(self, prefix: str, suffix: str) -> tuple[BinaryIO, str]:
        if self.directory_fd is None:
            temporary = tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=prefix,
                suffix=suffix,
                dir=self.path,
                delete=False,
            )
            return temporary, Path(temporary.name).name
        for _attempt in range(100):
            name = f"{prefix}{uuid.uuid4().hex}{suffix}"
            try:
                return self.open_exclusive(name), name
            except FileExistsError:
                continue
        raise FileExistsError("无法分配唯一的原子写临时文件")

    def lexists(self, name: str) -> bool:
        _validate_child_name(name)
        if self.directory_fd is None:
            return os.path.lexists(self.path / name)
        try:
            os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def lstat(self, name: str) -> os.stat_result:
        _validate_child_name(name)
        if self.directory_fd is None:
            return (self.path / name).lstat()
        return os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)

    def samefile(self, left: str, right: str) -> bool:
        _validate_child_name(left)
        _validate_child_name(right)
        if self.directory_fd is None:
            return os.path.samefile(self.path / left, self.path / right)
        left_stat = os.stat(left, dir_fd=self.directory_fd, follow_symlinks=False)
        right_stat = os.stat(right, dir_fd=self.directory_fd, follow_symlinks=False)
        return os.path.samestat(left_stat, right_stat)

    def link(self, source: str, destination: str) -> None:
        _validate_child_name(source)
        _validate_child_name(destination)
        if self.directory_fd is None:
            os.link(
                self.path / source,
                self.path / destination,
                follow_symlinks=False,
            )
            return
        os.link(
            source,
            destination,
            src_dir_fd=self.directory_fd,
            dst_dir_fd=self.directory_fd,
            follow_symlinks=False,
        )

    def replace(self, source: str, destination: str) -> None:
        _validate_child_name(source)
        _validate_child_name(destination)
        if self.directory_fd is None:
            os.replace(self.path / source, self.path / destination)
            return
        os.replace(
            source,
            destination,
            src_dir_fd=self.directory_fd,
            dst_dir_fd=self.directory_fd,
        )

    def read_bytes(self, name: str) -> bytes:
        _validate_child_name(name)
        if self.directory_fd is None:
            return (self.path / name).read_bytes()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, dir_fd=self.directory_fd)
        with os.fdopen(descriptor, "rb") as source:
            return source.read()

    def unlink(self, name: str, *, missing_ok: bool = False) -> None:
        _validate_child_name(name)
        try:
            if self.directory_fd is None:
                (self.path / name).unlink()
            else:
                os.unlink(name, dir_fd=self.directory_fd)
        except FileNotFoundError:
            if not missing_ok:
                raise


_ANCHOR_STATE = threading.local()


@contextmanager
def parent_directory_anchor(path: Path) -> Iterator[ParentDirectoryAnchor]:
    """打开并固定一个非 reparse 普通目录，直到原子操作完全结束。"""
    directory = _absolute_lexical_path(path)
    if os.name == "nt":
        handle = _open_windows_directory_anchor(directory)
        anchor = ParentDirectoryAnchor(directory, windows_handle=handle)
        try:
            with _registered_anchor(anchor):
                yield anchor
        finally:
            _close_windows_handle(handle)
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(directory, flags)
    try:
        opened = os.fstat(descriptor)
        current = directory.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or not os.path.samestat(opened, current)
        ):
            raise ValueError(f"原子操作父目录身份无效：{directory}")
        anchor = ParentDirectoryAnchor(directory, directory_fd=descriptor)
        with _registered_anchor(anchor):
            yield anchor
    finally:
        os.close(descriptor)


def atomic_write_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    transaction_id: str | None = None,
) -> None:
    """在目标文件所在目录中原子写入 UTF-8 JSON。"""
    path = _absolute_lexical_path(path)
    content = _json_bytes(payload)
    _validate_json_size(content)
    _atomic_write_bytes(
        path,
        content,
        operation="JSON 原子写入",
        transaction_id=transaction_id,
    )


def atomic_create_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    transaction_id: str,
) -> bool:
    """仅在目标缺失时原子创建 JSON，不覆盖竞争期间出现的目标。"""
    path = _absolute_lexical_path(path)
    content = _json_bytes(payload)
    transaction_temp_path(path, transaction_id)
    with parent_directory_anchor(path.parent) as anchor:
        if _atomic_create_target_exists(anchor, path.name):
            return False
        _validate_json_size(content)
        return atomic_create_bytes_clean(
            path,
            content,
            transaction_id=transaction_id,
            operation="JSON 原子创建",
        )


def atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    transaction_id: str | None = None,
) -> None:
    """在目标文件所在目录中原子写入字节。"""
    path = _absolute_lexical_path(path)
    _atomic_write_bytes(
        path,
        content,
        operation="文件原子写入",
        transaction_id=transaction_id,
    )


def atomic_create_bytes(
    path: Path,
    content: bytes,
    *,
    transaction_id: str,
) -> bool:
    """只在目标缺失时原子发布字节，并在成功后保留事务临时硬链接。"""
    path = _absolute_lexical_path(path)
    active = _active_anchor(path.parent)
    if active is not None:
        return _atomic_create_bytes_anchored(
            active,
            path.name,
            content,
            transaction_id=transaction_id,
        )
    with parent_directory_anchor(path.parent) as anchor:
        return _atomic_create_bytes_anchored(
            anchor,
            path.name,
            content,
            transaction_id=transaction_id,
        )


def atomic_create_bytes_clean(
    path: Path,
    content: bytes,
    *,
    transaction_id: str,
    operation: str = "文件原子创建",
) -> bool:
    """原子创建字节，并在校验所有权后清理成功发布的事务锚点。"""
    path = _absolute_lexical_path(path)
    transaction_temp_path(path, transaction_id)
    active = _active_anchor(path.parent)
    if active is not None:
        return _atomic_create_bytes_clean_anchored(
            active,
            path.name,
            content,
            transaction_id=transaction_id,
            operation=operation,
        )
    with parent_directory_anchor(path.parent) as anchor:
        return _atomic_create_bytes_clean_anchored(
            anchor,
            path.name,
            content,
            transaction_id=transaction_id,
            operation=operation,
        )


def _atomic_create_bytes_clean_anchored(
    anchor: ParentDirectoryAnchor,
    destination_name: str,
    content: bytes,
    *,
    transaction_id: str,
    operation: str,
) -> bool:
    if _atomic_create_target_exists(anchor, destination_name):
        return False
    created = atomic_create_bytes(
        anchor.path / destination_name,
        content,
        transaction_id=transaction_id,
    )
    if not created:
        return False

    temporary_name = transaction_temp_path(
        anchor.path / destination_name, transaction_id,
    ).name
    try:
        temporary_identity = _file_identity(anchor.lstat(temporary_name))
        owns_anchor = (
            anchor.lexists(destination_name)
            and anchor.samefile(destination_name, temporary_name)
        )
    except OSError as error:
        raise RuntimeError(
            f"{operation}后的事务锚点无法校验：{anchor.path / temporary_name}"
        ) from error
    if not owns_anchor:
        raise RuntimeError(
            f"{operation}后的事务锚点所有权已变化：{anchor.path / temporary_name}"
        )

    cleanup_failures: list[str] = []
    residual_paths: list[Path] = []
    _best_effort_unlink(
        anchor,
        temporary_name,
        expected_identity=temporary_identity,
        cleanup_failures=cleanup_failures,
        residual_paths=residual_paths,
        attempts=2,
    )
    if cleanup_failures:
        _committed, probe_failure = _probe_committed(
            lambda: (
                anchor.lexists(destination_name)
                and anchor.read_bytes(destination_name) == content
            )
        )
        _raise_if_audit_failed(
            f"{operation}后清理",
            OSError("事务临时文件清理未完成"),
            probe_failure=probe_failure,
            cleanup_failures=cleanup_failures,
            residual_paths=residual_paths,
        )
    return True


def _atomic_create_bytes_anchored(
    anchor: ParentDirectoryAnchor,
    destination_name: str,
    content: bytes,
    *,
    transaction_id: str,
) -> bool:
    temporary_name = transaction_temp_path(
        anchor.path / destination_name, transaction_id
    ).name
    if _atomic_create_target_exists(anchor, destination_name):
        return False
    temporary_created = False
    temporary_identity: FileIdentity | None = None
    own_link_succeeded = False
    link_conflict = False
    write_error: BaseException | None = None
    try:
        with anchor.open_exclusive(temporary_name) as temporary:
            temporary_created = True
            temporary_identity = _file_identity(os.fstat(temporary.fileno()))
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            anchor.link(temporary_name, destination_name)
        except OSError as error:
            if isinstance(error, FileExistsError) or error.errno == errno.EEXIST:
                link_conflict = True
                write_error = error
            else:
                raise
        else:
            own_link_succeeded = True
    except BaseException as error:
        write_error = error

    if own_link_succeeded:
        return True

    probe_failure: BaseException | None = None
    cleanup_failures: list[str] = []
    residual_paths: list[Path] = []
    if temporary_created:
        _owned_temp, probe_failure = _probe_committed(
            lambda: (
                temporary_identity is not None
                and _file_identity(anchor.lstat(temporary_name)) == temporary_identity
            )
        )
        if not link_conflict:
            destination_present, destination_probe_failure = _probe_committed(
                lambda: anchor.lexists(destination_name)
            )
            if probe_failure is None and destination_probe_failure is not None:
                probe_failure = destination_probe_failure
            elif destination_present:
                cleanup_failures.append(
                    f"{destination_name}: publish 异常后目标已出现，所有权未知，已保留"
                )
        audit_probe_failure = _audit_exception_temp(
            anchor,
            temporary_name,
            expected_identity=temporary_identity,
            audit_failures=cleanup_failures,
            residual_paths=residual_paths,
        )
        if probe_failure is None:
            probe_failure = audit_probe_failure
    if write_error is None:
        raise RuntimeError("文件原子创建失败但未记录原始错误")
    if link_conflict:
        _raise_if_audit_failed(
            "文件原子创建竞争清理",
            write_error,
            probe_failure=probe_failure,
            cleanup_failures=cleanup_failures,
            residual_paths=residual_paths,
        )
        return False
    _raise_if_audit_failed(
        "文件原子创建",
        write_error,
        probe_failure=probe_failure,
        cleanup_failures=cleanup_failures,
        residual_paths=residual_paths,
    )
    raise write_error


def transaction_temp_path(path: Path, transaction_id: str) -> Path:
    if len(transaction_id) != 32 or any(
        character not in "0123456789abcdef" for character in transaction_id
    ):
        raise ValueError("事务 ID 必须是 32 位小写十六进制字符串")
    path = Path(path)
    return path.parent / f".{path.name}.cvexp-{transaction_id}.tmp"


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
    ).encode("utf-8")


def _validate_json_size(content: bytes) -> None:
    if len(content) > DEFAULT_JSON_LIMIT:
        raise ValueError("JSON 超过 1 MiB 上限")


def _atomic_create_target_exists(
    anchor: ParentDirectoryAnchor,
    destination_name: str,
) -> bool:
    try:
        destination_metadata = anchor.lstat(destination_name)
    except FileNotFoundError:
        return False
    if (
        stat.S_ISREG(destination_metadata.st_mode)
        and not _windows_stat_is_reparse(destination_metadata)
    ):
        return True
    raise ValueError(
        f"原子创建目标已存在且不是普通文件：{anchor.path / destination_name}"
    )


def _atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    operation: str,
    transaction_id: str | None = None,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with parent_directory_anchor(path.parent) as anchor:
        temporary_name: str | None = None
        temporary_created = False
        temporary_identity: FileIdentity | None = None
        try:
            if transaction_id is None:
                temporary, temporary_name = anchor.open_unique(
                    prefix=f".{path.name}.", suffix=".tmp"
                )
            else:
                temporary_name = transaction_temp_path(path, transaction_id).name
                temporary = anchor.open_exclusive(temporary_name)
            temporary_created = True
            with temporary:
                temporary_identity = _file_identity(os.fstat(temporary.fileno()))
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())

            anchor.replace(temporary_name, path.name)
        except BaseException as write_error:
            committed, probe_failure = _probe_committed(
                lambda: (
                    temporary_name is not None
                    and not anchor.lexists(temporary_name)
                    and anchor.lexists(path.name)
                    and anchor.read_bytes(path.name) == content
                )
            )
            if committed:
                return
            cleanup_failures: list[str] = []
            residual_paths: list[Path] = []
            if temporary_created and temporary_name is not None:
                audit_probe_failure = _audit_exception_temp(
                    anchor,
                    temporary_name,
                    expected_identity=temporary_identity,
                    audit_failures=cleanup_failures,
                    residual_paths=residual_paths,
                )
                if probe_failure is None:
                    probe_failure = audit_probe_failure
            _raise_if_audit_failed(
                operation,
                write_error,
                probe_failure=probe_failure,
                cleanup_failures=cleanup_failures,
                residual_paths=residual_paths,
            )
            raise


def _probe_committed(probe: Callable[[], bool]) -> tuple[bool, BaseException | None]:
    try:
        return bool(probe()), None
    except BaseException as error:
        return False, error


def _best_effort_unlink(
    anchor: ParentDirectoryAnchor,
    name: str,
    *,
    expected_identity: FileIdentity | None,
    cleanup_failures: list[str],
    residual_paths: list[Path],
    attempts: int = 1,
) -> None:
    # 仅用于 publish 已明确成功后的正常清理。异常路径的数据保守优先，禁止按名删除。
    for attempt in range(attempts):
        try:
            current_identity = _file_identity(anchor.lstat(name))
        except FileNotFoundError:
            return
        except BaseException as error:
            cleanup_failures.append(f"{name}: 删除前身份探测失败：{error}")
            residual_paths.append(anchor.path / name)
            return
        if expected_identity is None or current_identity != expected_identity:
            cleanup_failures.append(f"{name}: 所有权已变化，已保留当前路径")
            residual_paths.append(anchor.path / name)
            return
        try:
            anchor.unlink(name, missing_ok=True)
            return
        except BaseException as error:
            if attempt + 1 < attempts:
                continue
            cleanup_failures.append(f"{name}: {error}")
            residual_paths.append(anchor.path / name)
            return


def _audit_exception_temp(
    anchor: ParentDirectoryAnchor,
    name: str,
    *,
    expected_identity: FileIdentity | None,
    audit_failures: list[str],
    residual_paths: list[Path],
) -> BaseException | None:
    # 外部进程可在任意检查后换绑同名路径；异常路径因此只读探测并保守保留。
    path = anchor.path / name
    residual_paths.append(path)
    try:
        current_identity = _file_identity(anchor.lstat(name))
    except FileNotFoundError:
        audit_failures.append(f"{name}: 异常路径未自动清理，当前名称缺失且所有权未知")
        return None
    except BaseException as error:
        audit_failures.append(f"{name}: 异常路径未自动清理，所有权探测失败：{error}")
        return error
    if expected_identity is None or current_identity != expected_identity:
        audit_failures.append(f"{name}: 异常路径未自动清理，所有权已变化或未知")
    else:
        audit_failures.append(f"{name}: 异常路径未自动清理，数据保守优先")
    return None


def _file_identity(metadata: os.stat_result) -> FileIdentity:
    return metadata.st_dev, metadata.st_ino


def _raise_if_audit_failed(
    operation: str,
    original_error: BaseException,
    *,
    probe_failure: BaseException | None,
    cleanup_failures: list[str],
    residual_paths: list[Path],
) -> None:
    if probe_failure is None and not cleanup_failures:
        return
    details: list[str] = []
    if probe_failure is not None:
        details.append(f"提交状态探测失败：{probe_failure}")
    if cleanup_failures:
        details.append(f"清理失败：{'；'.join(cleanup_failures)}")
    residuals = "、".join(str(residual) for residual in residual_paths) or "无"
    raise RuntimeError(
        f"{operation}失败：{original_error}；{'；'.join(details)}；残留路径：{residuals}"
    ) from original_error


def _validate_child_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
    ):
        raise ValueError(f"父目录锚点只允许单层文件名：{name!r}")


@contextmanager
def _registered_anchor(anchor: ParentDirectoryAnchor) -> Iterator[None]:
    anchors = getattr(_ANCHOR_STATE, "anchors", None)
    if anchors is None:
        anchors = []
        _ANCHOR_STATE.anchors = anchors
    anchors.append(anchor)
    try:
        yield
    finally:
        popped = anchors.pop()
        if popped is not anchor:
            raise RuntimeError("父目录锚点栈顺序损坏")


def _active_anchor(path: Path) -> ParentDirectoryAnchor | None:
    expected = os.path.normcase(os.fspath(_absolute_lexical_path(path)))
    for anchor in reversed(getattr(_ANCHOR_STATE, "anchors", ())):
        if os.path.normcase(os.fspath(anchor.path)) == expected:
            return anchor
    return None


def _absolute_lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _open_windows_directory_anchor(path: Path) -> int:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL
    before = path.lstat()
    if not stat.S_ISDIR(before.st_mode) or _windows_stat_is_reparse(before):
        raise ValueError(f"原子操作父目录不是普通目录：{path}")
    handle = create_file(
        str(path),
        0x0080 | 0x00010000,  # FILE_READ_ATTRIBUTES | DELETE
        0x0001 | 0x0002,  # FILE_SHARE_READ | FILE_SHARE_WRITE; intentionally no DELETE
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = ByHandleFileInformation()
        if not get_information(handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        current = path.lstat()
        file_index = (
            int(information.file_index_high) << 32
        ) | int(information.file_index_low)
        if (
            information.file_attributes & 0x0400  # FILE_ATTRIBUTE_REPARSE_POINT
            or not stat.S_ISDIR(current.st_mode)
            or _windows_stat_is_reparse(current)
            or not os.path.samestat(before, current)
            or current.st_ino != file_index
        ):
            raise ValueError(f"原子操作父目录身份在打开期间发生变化：{path}")
        return int(handle)
    except BaseException:
        _close_windows_handle(int(handle))
        raise


def _windows_stat_is_reparse(value: os.stat_result) -> bool:
    attributes = getattr(value, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))


def _close_windows_handle(handle: int) -> None:
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    if not close_handle(handle):
        raise ctypes.WinError(ctypes.get_last_error())
