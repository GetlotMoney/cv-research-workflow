from __future__ import annotations

import errno
import os
import stat
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()
LOCK_FILENAME = ".experiment-workflow.init.lock"
LOCK_MAGIC = b"cv-experiment-workflow.init-lock.v1\n"
LOCK_BOOTSTRAP_PREFIX = ".experiment-workflow.init.lock.bootstrap-"
LOCK_BOOTSTRAP_SUFFIX = ".tmp"


class ProjectInitializationInProgressError(RuntimeError):
    """同一项目已有初始化事务持有独占锁。"""


class InvalidProjectInitializationLockError(RuntimeError):
    """项目根的初始化锁文件不属于本工具。"""


def canonical_project_path(path: Path) -> str:
    resolved = Path(path).expanduser().resolve(strict=False)
    return os.path.normcase(str(resolved))


@contextmanager
def project_initialization_lock(project_root: Path) -> Iterator[str]:
    canonical = canonical_project_path(project_root)
    with _PROCESS_LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(canonical, threading.Lock())
    if not process_lock.acquire(blocking=False):
        raise ProjectInitializationInProgressError(
            f"项目正在初始化（initialization in progress）：{canonical}"
        )

    handle: BinaryIO | None = None
    bootstrap_path: Path | None = None
    bootstrap_stat: os.stat_result | None = None
    os_lock_acquired = False
    try:
        handle, bootstrap_path, bootstrap_stat = _open_lock_file(
            project_root / LOCK_FILENAME
        )
        try:
            _acquire_os_lock(handle)
        except OSError as error:
            raise ProjectInitializationInProgressError(
                f"项目正在初始化（initialization in progress）：{canonical}"
            ) from error
        os_lock_acquired = True
        _validate_lock_magic(handle, project_root / LOCK_FILENAME)
        _cleanup_lock_bootstrap_files(project_root)
        yield canonical
    finally:
        try:
            if handle is not None:
                if os_lock_acquired:
                    try:
                        _release_os_lock(handle)
                    except OSError:
                        pass
                handle.close()
        finally:
            try:
                if bootstrap_path is not None and bootstrap_stat is not None:
                    _cleanup_own_bootstrap_file(bootstrap_path, bootstrap_stat)
            finally:
                process_lock.release()


@contextmanager
def project_write_lock(project_root: Path) -> Iterator[str]:
    """等待并持有项目级独占写锁。"""
    canonical = canonical_project_path(project_root)
    with _PROCESS_LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(canonical, threading.Lock())
    process_lock.acquire()

    handle: BinaryIO | None = None
    bootstrap_path: Path | None = None
    bootstrap_stat: os.stat_result | None = None
    os_lock_acquired = False
    try:
        handle, bootstrap_path, bootstrap_stat = _open_lock_file(
            project_root / LOCK_FILENAME
        )
        _acquire_os_lock_blocking(handle)
        os_lock_acquired = True
        _validate_lock_magic(handle, project_root / LOCK_FILENAME)
        _cleanup_lock_bootstrap_files(project_root)
        yield canonical
    finally:
        try:
            if handle is not None:
                if os_lock_acquired:
                    try:
                        _release_os_lock(handle)
                    except OSError:
                        pass
                handle.close()
        finally:
            try:
                if bootstrap_path is not None and bootstrap_stat is not None:
                    _cleanup_own_bootstrap_file(bootstrap_path, bootstrap_stat)
            finally:
                process_lock.release()


@contextmanager
def project_snapshot_lock(project_root: Path) -> Iterator[str]:
    """只读打开已有项目锁，并与写操作使用同一独占协议。"""
    canonical = canonical_project_path(project_root)
    with _PROCESS_LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(canonical, threading.Lock())
    process_lock.acquire()

    handle: BinaryIO | None = None
    os_lock_acquired = False
    try:
        binary_flag = getattr(os, "O_BINARY", 0)
        handle = _open_existing_lock_file(
            project_root / LOCK_FILENAME, os.O_RDONLY | binary_flag, mode="rb",
        )
        _acquire_os_lock_blocking(handle)
        os_lock_acquired = True
        _validate_lock_magic(handle, project_root / LOCK_FILENAME)
        yield canonical
    finally:
        try:
            if handle is not None:
                if os_lock_acquired:
                    try:
                        _release_os_lock(handle)
                    except OSError:
                        pass
                handle.close()
        finally:
            process_lock.release()


def _open_lock_file(path: Path) -> tuple[BinaryIO, Path, os.stat_result]:
    binary_flag = getattr(os, "O_BINARY", 0)
    flags = os.O_RDWR | binary_flag
    bootstrap_path = path.parent / (
        f"{LOCK_BOOTSTRAP_PREFIX}{uuid.uuid4().hex}{LOCK_BOOTSTRAP_SUFFIX}"
    )
    descriptor = os.open(
        bootstrap_path,
        flags | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        created_handle = os.fdopen(descriptor, "r+b")
    except BaseException:
        os.close(descriptor)
        raise
    with created_handle:
        created_handle.write(LOCK_MAGIC)
        created_handle.flush()
        os.fsync(created_handle.fileno())
        bootstrap_stat = os.fstat(created_handle.fileno())

    try:
        try:
            os.link(bootstrap_path, path, follow_symlinks=False)
        except OSError as error:
            bootstrap_was_cleaned = isinstance(error, FileNotFoundError)
            final_exists = os.path.lexists(path)
            if bootstrap_was_cleaned and final_exists:
                pass
            elif (
                os.name == "nt"
                and final_exists
                and isinstance(error, PermissionError)
            ):
                pass
            elif not (
                isinstance(error, FileExistsError)
                or error.errno == errno.EEXIST
            ):
                raise
        handle = _open_existing_lock_file(path, flags)
        return handle, bootstrap_path, bootstrap_stat
    except BaseException:
        _cleanup_own_bootstrap_file(bootstrap_path, bootstrap_stat)
        raise


def _cleanup_lock_bootstrap_files(project_root: Path) -> None:
    candidates = list(project_root.glob(f"{LOCK_BOOTSTRAP_PREFIX}*"))
    validated: list[tuple[Path, os.stat_result]] = []
    conflicts: list[str] = []
    for candidate in candidates:
        suffix = candidate.name.removeprefix(LOCK_BOOTSTRAP_PREFIX)
        transaction_id = suffix.removesuffix(LOCK_BOOTSTRAP_SUFFIX)
        valid_name = (
            suffix.endswith(LOCK_BOOTSTRAP_SUFFIX)
            and len(transaction_id) == 32
            and all(character in "0123456789abcdef" for character in transaction_id)
        )
        try:
            candidate_stat = candidate.lstat()
        except OSError:
            continue
        if (
            not valid_name
            or not stat.S_ISREG(candidate_stat.st_mode)
            or _is_reparse(candidate_stat)
        ):
            conflicts.append(candidate.name)
        else:
            validated.append((candidate, candidate_stat))
    if conflicts:
        paths = "、".join(sorted(conflicts))
        raise InvalidProjectInitializationLockError(
            f"初始化锁 bootstrap 含未知内容，拒绝清理：{paths}"
        )
    for candidate, expected_stat in validated:
        try:
            current_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(current_stat.st_mode)
            or _is_reparse(current_stat)
            or not _same_file(expected_stat, current_stat)
        ):
            raise InvalidProjectInitializationLockError(
                f"初始化锁 bootstrap 在清理前发生变化：{candidate}"
            )
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 32:
                continue
            raise


def _cleanup_own_bootstrap_file(
    path: Path,
    expected_stat: os.stat_result,
) -> None:
    try:
        current_stat = path.lstat()
    except OSError:
        return
    if (
        stat.S_ISREG(current_stat.st_mode)
        and not _is_reparse(current_stat)
        and _same_file(expected_stat, current_stat)
    ):
        try:
            path.unlink()
        except OSError:
            pass


def _open_existing_lock_file(
    path: Path, flags: int, *, mode: str = "r+b",
) -> BinaryIO:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise InvalidProjectInitializationLockError(
            f"无法校验初始化锁文件：{path}"
        ) from error
    if not stat.S_ISREG(path_stat.st_mode) or _is_reparse(path_stat):
        raise InvalidProjectInitializationLockError(
            f"初始化锁文件不是本工具的普通文件，拒绝访问：{path}"
        )

    open_flags = flags
    if os.name != "nt":
        open_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, open_flags)
    except OSError as error:
        raise InvalidProjectInitializationLockError(
            f"无法安全打开初始化锁文件：{path}"
        ) from error
    try:
        handle = os.fdopen(descriptor, mode)
    except BaseException:
        os.close(descriptor)
        raise

    try:
        opened_stat = os.fstat(handle.fileno())
        current_stat = path.lstat()
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or not stat.S_ISREG(current_stat.st_mode)
            or _is_reparse(current_stat)
            or not _same_file(path_stat, current_stat)
            or not _same_file(current_stat, opened_stat)
        ):
            raise InvalidProjectInitializationLockError(
                f"初始化锁文件在打开期间发生变化，拒绝访问：{path}"
            )
        return handle
    except BaseException:
        handle.close()
        raise


def _is_reparse(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _validate_lock_magic(handle: BinaryIO, path: Path) -> None:
    handle.seek(0)
    if handle.read() != LOCK_MAGIC:
        raise InvalidProjectInitializationLockError(
            f"初始化锁文件不属于本工具，拒绝改写：{path}"
        )
    handle.seek(0)


def _acquire_os_lock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _acquire_os_lock_blocking(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
