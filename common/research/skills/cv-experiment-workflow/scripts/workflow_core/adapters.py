from __future__ import annotations

import hashlib as _hashlib
import importlib as _importlib
import importlib.machinery as _machinery
import json as _json
import os as _os
import re as _re
import shutil as _shutil
import stat as _stat
import subprocess as _subprocess
import sys as _sys
import _thread as _thread_module
import threading as _threading
import unicodedata as _unicodedata
import uuid as _uuid
from contextlib import contextmanager as _contextmanager
from dataclasses import dataclass as _dataclass
from pathlib import Path as _Path
from types import ModuleType as _ModuleType
from typing import Any as _Any, Callable as _Callable, Protocol

from .policy import load_runtime_adapter_spec as _load_runtime_adapter_spec

try:
    import nt as _nt
except ImportError:  # pragma: no cover - 仅非 Windows 平台
    _nt = None


_ACTIONS = {"inspect", "validate", "prepare_runs", "execute", "parse_result"}
_MISSING_MODULE = object()
_MODULE_LOAD_LOCK = _threading.RLock()
# 这是防止合作型 Python Adapter 误写的进程内护栏，不是 native 沙箱。
# ctypes、C 扩展和继承同一 Windows 身份的恶意代码不在其安全承诺内。
_ADAPTER_FS_POLICY = _threading.local()
_WRITE_OPEN_FLAGS = (
    _os.O_WRONLY
    | _os.O_RDWR
    | _os.O_APPEND
    | _os.O_CREAT
    | _os.O_TRUNC
    | _os.O_EXCL
)
_SINGLE_PATH_MUTATIONS = {
    "os.mkdir": (0, 2),
    "os.remove": (0, 1),
    "os.rmdir": (0, 1),
    "os.chmod": (0, 2),
    "os.chown": (0, 3),
    "os.truncate": (0, None),
    "os.utime": (0, 3),
    "shutil.rmtree": (0, 1),
}
_DOUBLE_PATH_MUTATIONS = {
    "os.rename": (0, 1, 2, 3),
    "os.replace": (0, 1, 2, 3),
    "shutil.move": (0, 1, None, None),
}
_COPY_TARGET_MUTATIONS = {
    "shutil.copyfile",
    "shutil.copymode",
    "shutil.copystat",
}
_LINK_MUTATIONS = {"os.link", "os.symlink"}


class ProjectAdapter(Protocol):
    def inspect(self, project: _Path) -> dict[str, _Any]:
        raise NotImplementedError

    def validate(self, project: _Path) -> list[str]:
        raise NotImplementedError

    def prepare_runs(
        self, project: _Path, task: dict[str, _Any]
    ) -> list[dict[str, _Any]]:
        raise NotImplementedError

    def execute(
        self, project: _Path, action: str, run: dict[str, _Any]
    ) -> dict[str, _Any]:
        raise NotImplementedError

    def parse_result(
        self, project: _Path, run: dict[str, _Any]
    ) -> dict[str, _Any]:
        raise NotImplementedError


@_dataclass(frozen=True, slots=True)
class LoadedAdapter:
    _inspect: _Callable[[_Path], object]
    _validate: _Callable[[_Path], object]
    _prepare_runs: _Callable[[_Path, dict[str, _Any]], object]
    _execute: _Callable[[_Path, str, dict[str, _Any]], object]
    _parse_result: _Callable[[_Path, dict[str, _Any]], object]
    _summary: tuple[str, str, str, str]
    _import_root: _Path | None
    _top_level_modules: tuple[str, ...]
    _bound_snapshot: bool

    def inspect(self, project: _Path) -> dict[str, _Any]:
        with self._call_scope(project, allow_output_write=False):
            value = self._inspect(_Path(project))
        return _json_object(value, "Adapter inspect")

    def validate(self, project: _Path) -> list[str]:
        with self._call_scope(project, allow_output_write=False):
            raw = self._validate(_Path(project))
        value = _finite_json(raw, "Adapter validate")
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise ValueError("Adapter validate 必须返回问题字符串数组")
        return list(value)

    def prepare_runs(
        self, project: _Path, task: dict[str, _Any]
    ) -> list[dict[str, _Any]]:
        task_copy = _json_object(task, "Adapter Task 输入")
        with self._call_scope(project, allow_output_write=False):
            raw = self._prepare_runs(_Path(project), task_copy)
        value = _finite_json(raw, "Adapter prepare_runs")
        if not isinstance(value, list) or not value or not all(
            isinstance(item, dict) for item in value
        ):
            raise ValueError("Adapter 没有生成可运行的候选")
        return value

    def execute(
        self, project: _Path, action: str, run: dict[str, _Any]
    ) -> dict[str, _Any]:
        run_copy = _json_object(run, "Adapter Run 输入")
        with self._call_scope(project, allow_output_write=True):
            value = self._execute(_Path(project), action, run_copy)
        return _json_object(value, "Adapter execute")

    def parse_result(
        self, project: _Path, run: dict[str, _Any]
    ) -> dict[str, _Any]:
        run_copy = _json_object(run, "Adapter Run 输入")
        with self._call_scope(project, allow_output_write=False):
            value = self._parse_result(_Path(project), run_copy)
        return _json_object(value, "Adapter parse_result")

    def _import_scope(self):
        return _adapter_import_scope(
            self._import_root,
            self._top_level_modules,
            normalized_names=self._bound_snapshot,
        )

    @_contextmanager
    def _call_scope(
        self,
        project: _Path,
        *,
        allow_output_write: bool,
    ):
        with self._import_scope():
            if not self._bound_snapshot:
                yield
                return
            if self._import_root is None:
                raise ValueError("绑定执行快照缺少导入根目录")
            project_root = _Path(project).resolve(strict=True)
            if project_root != self._import_root:
                raise ValueError(
                    "冻结 Adapter 必须在自己的执行快照根目录运行"
                )
            allowed_root = (
                project_root / ".cv-workflow-output"
                if allow_output_write
                else None
            )
            with _adapter_filesystem_scope(
                allowed_root,
                snapshot_root=project_root,
                allow_snapshot_subprocess=True,
            ):
                yield
                _validate_snapshot_module_origins(
                    project_root,
                    self._top_level_modules,
                )


def load_project_adapter(project: _Path) -> ProjectAdapter:
    return _load_adapter(_load_runtime_adapter_spec(project))


def load_codebase_adapter(
    repository: _Path,
    commit: str,
) -> ProjectAdapter:
    from .codebases import load_codebase_adapter_spec

    return _load_adapter(
        load_codebase_adapter_spec(_Path(repository), commit)
    )


def load_snapshot_adapter(
    snapshot_root: _Path,
    commit: str,
) -> ProjectAdapter:
    from .execution_snapshot import load_snapshot_adapter_spec

    return _load_adapter(
        load_snapshot_adapter_spec(_Path(snapshot_root), commit),
        bound_snapshot=True,
    )


def _load_adapter(
    spec: dict[str, _Any],
    *,
    bound_snapshot: bool = False,
) -> ProjectAdapter:
    source = spec["source_bytes"]
    digest = _hashlib.sha256(source).hexdigest()
    module_name = f"_cv_workflow_adapter_{digest}"
    module = _ModuleType(module_name)
    module.__file__ = spec["display_path"]
    import_root_value = spec.get("import_root")
    import_root = (
        _Path(import_root_value).resolve(strict=True)
        if isinstance(import_root_value, str)
        else None
    )
    if bound_snapshot and import_root is None:
        raise ValueError("绑定执行快照必须提供 import_root")
    top_level_modules = _discover_top_level_modules(
        import_root,
        reject_normalized_collisions=bound_snapshot,
    )
    with _MODULE_LOAD_LOCK:
        previous = _sys.modules.get(module_name, _MISSING_MODULE)
        _sys.modules[module_name] = module
        try:
            try:
                code = compile(source, spec["display_path"], "exec")
                with _adapter_import_scope(
                    import_root,
                    top_level_modules,
                    normalized_names=bound_snapshot,
                ):
                    with _adapter_filesystem_scope(
                        None,
                        guarded=bound_snapshot,
                        snapshot_root=import_root,
                        allow_snapshot_subprocess=True,
                    ):
                        exec(code, module.__dict__)
                        if bound_snapshot:
                            _validate_snapshot_module_origins(
                                import_root,
                                top_level_modules,
                            )
                if _sys.modules.get(module_name) is not module:
                    raise RuntimeError("项目 Adapter 修改了自身模块登记")
            except Exception as error:
                raise ValueError("项目 Adapter 加载失败") from error
            public_actions = {
                name
                for name, value in vars(module).items()
                if not name.startswith("_") and callable(value)
            }
            if public_actions != _ACTIONS or any(
                not callable(getattr(module, name, None))
                for name in _ACTIONS
            ):
                raise ValueError("项目 Adapter 必须且只能暴露五个接口")
        except BaseException:
            if previous is _MISSING_MODULE:
                _sys.modules.pop(module_name, None)
            else:
                _sys.modules[module_name] = previous
            raise
    return LoadedAdapter(
        module.inspect,
        module.validate,
        module.prepare_runs,
        module.execute,
        module.parse_result,
        (digest, spec["repo_url"], spec["commit"], spec["source"]),
        import_root,
        top_level_modules,
        bound_snapshot,
    )


def _discover_top_level_modules(
    root: _Path | None,
    *,
    reject_normalized_collisions: bool = False,
) -> tuple[str, ...]:
    if root is None:
        return ()
    names: set[str] = set()
    observed: dict[str, str] = {}
    for entry in root.iterdir():
        if entry.name.startswith("."):
            continue
        name: str | None = None
        if (
            entry.is_file()
            and any(
                entry.name.endswith(suffix)
                for suffix in _machinery.all_suffixes()
            )
        ):
            name = entry.stem
        elif entry.is_dir():
            name = entry.name
        if name is None:
            continue
        if reject_normalized_collisions:
            key = _module_key(name)
            previous = observed.get(key)
            if previous is not None:
                raise ValueError(
                    "Snapshot 顶层模块发生 NFKC+casefold 冲突："
                    f"{previous} / {name}"
                )
            observed[key] = name
        names.add(name)
    return tuple(sorted(names))


@_contextmanager
def _adapter_import_scope(
    root: _Path | None,
    top_level_modules: tuple[str, ...],
    *,
    normalized_names: bool = False,
):
    if root is None:
        yield
        return
    with _MODULE_LOAD_LOCK:
        key = _module_key if normalized_names else lambda value: value
        prefixes = {key(name) for name in top_level_modules}
        saved = {
            name: module
            for name, module in list(_sys.modules.items())
            if key(name.split(".", 1)[0]) in prefixes
        }
        for name in saved:
            _sys.modules.pop(name, None)
        root_text = str(root)
        saved_sys_path = list(_sys.path)
        _sys.path[:] = [
            root_text,
            *_trusted_runtime_paths(saved_sys_path),
        ]
        finder = _SnapshotRootFinder(
            root,
            prefixes,
            normalized_names=normalized_names,
        )
        _sys.meta_path.insert(0, finder)
        saved_directory = _Path.cwd()
        _os.chdir(root)
        guarded_environment = _snapshot_process_environment(root)
        missing_environment = object()
        saved_environment = {
            name: _os.environ.get(name, missing_environment)
            for name in guarded_environment
        }
        for name, value in guarded_environment.items():
            if value is None:
                _os.environ.pop(name, None)
            else:
                _os.environ[name] = value
        previous_dont_write_bytecode = _sys.dont_write_bytecode
        _sys.dont_write_bytecode = True
        _importlib.invalidate_caches()
        try:
            yield
        finally:
            for name in list(_sys.modules):
                if key(name.split(".", 1)[0]) in prefixes:
                    _sys.modules.pop(name, None)
            _sys.modules.update(saved)
            _sys.path[:] = saved_sys_path
            try:
                _sys.meta_path.remove(finder)
            except ValueError:
                pass
            _os.chdir(saved_directory)
            for name, previous in saved_environment.items():
                if previous is missing_environment:
                    _os.environ.pop(name, None)
                else:
                    _os.environ[name] = previous
            _sys.dont_write_bytecode = previous_dont_write_bytecode
            _importlib.invalidate_caches()


class _SnapshotRootFinder:
    def __init__(
        self,
        root: _Path,
        protected: set[str],
        *,
        normalized_names: bool,
    ) -> None:
        self._root = root
        self._protected = protected
        self._normalized_names = normalized_names

    def find_spec(self, fullname, path=None, target=None):
        top_level = fullname.split(".", 1)[0]
        key = (
            _module_key(top_level)
            if self._normalized_names
            else top_level
        )
        if key not in self._protected:
            return None
        if "." not in fullname:
            search_path = [str(self._root)]
        else:
            search_path = [
                str(candidate)
                for candidate in (path or ())
                if _path_is_within_root(_Path(candidate), self._root)
            ]
        spec = _machinery.PathFinder.find_spec(fullname, search_path)
        if spec is None:
            raise ModuleNotFoundError(
                f"Snapshot-local module is missing: {fullname}"
            )
        if spec.origin not in {None, "namespace"} and not _path_is_within_root(
            _Path(spec.origin),
            self._root,
        ):
            raise ModuleNotFoundError(
                f"Snapshot import escaped its root: {fullname}"
            )
        locations = spec.submodule_search_locations
        if locations is not None and any(
            not _path_is_within_root(_Path(location), self._root)
            for location in locations
        ):
            raise ModuleNotFoundError(
                f"Snapshot package path escaped its root: {fullname}"
            )
        return spec


def _validate_snapshot_module_origins(
    root: _Path | None,
    top_level_modules: tuple[str, ...],
) -> None:
    if root is None:
        return
    protected = {_module_key(name) for name in top_level_modules}
    for name, module in list(_sys.modules.items()):
        if _module_key(name.split(".", 1)[0]) not in protected:
            continue
        origin = getattr(module, "__file__", None)
        locations = getattr(module, "__path__", None)
        if not isinstance(origin, str) and locations is None:
            raise ImportError(
                f"Snapshot module has no verifiable origin: {name}"
            )
        if isinstance(origin, str) and not _path_is_within_root(
            _Path(origin),
            root,
        ):
            raise ImportError(
                f"Snapshot module escaped its root: {name}"
            )
        if locations is not None and any(
            not _path_is_within_root(_Path(location), root)
            for location in locations
        ):
            raise ImportError(
                f"Snapshot package path escaped its root: {name}"
            )


def _module_key(value: str) -> str:
    return _unicodedata.normalize("NFKC", value).casefold()


@_contextmanager
def _adapter_filesystem_scope(
    allowed_root: _Path | None,
    *,
    guarded: bool = True,
    snapshot_root: _Path | None = None,
    allow_snapshot_subprocess: bool = False,
):
    if not guarded:
        yield
        return
    if snapshot_root is None:
        raise ValueError("冻结 Adapter 缺少执行快照根目录")
    resolved_snapshot = _Path(snapshot_root).resolve(strict=True)
    if not resolved_snapshot.is_dir():
        raise ValueError("冻结 Adapter 的执行快照根目录无效")
    resolved_allowed: _Path | None = None
    if allowed_root is not None:
        resolved_allowed = _Path(allowed_root).resolve(strict=True)
        if (
            not resolved_allowed.is_dir()
            or resolved_allowed.parent != resolved_snapshot
            or resolved_allowed.name != ".cv-workflow-output"
        ):
            raise ValueError("冻结 Adapter 的固定输出根目录无效")
    missing = object()
    previous = getattr(_ADAPTER_FS_POLICY, "value", missing)
    _ADAPTER_FS_POLICY.value = {
        "allowed_root": resolved_allowed,
        "snapshot_root": resolved_snapshot,
        "allow_snapshot_subprocess": allow_snapshot_subprocess,
        "spawn_permit": 0,
        "thread_permit": 0,
        "allowed_write_fds": {},
        "protected_child_temp": _protected_child_temp_from_environment(
            resolved_allowed
        ),
    }
    previous_run = _subprocess.run
    previous_thread_start = _threading.Thread.start
    previous_start_new_thread = _thread_module.start_new_thread
    previous_os_open = _os.open
    previous_os_dup = getattr(_os, "dup", None)
    previous_os_dup2 = getattr(_os, "dup2", None)
    previous_nt_dup = getattr(_nt, "dup", None) if _nt is not None else None
    previous_nt_dup2 = getattr(_nt, "dup2", None) if _nt is not None else None

    def guarded_run(*args, **kwargs):
        policy = getattr(_ADAPTER_FS_POLICY, "value", None)
        if not isinstance(policy, dict):
            return previous_run(*args, **kwargs)
        if not policy.get("allow_snapshot_subprocess"):
            raise PermissionError("冻结 Adapter 子进程不允许再启动嵌套子进程")
        original, rewritten, environment = _controlled_snapshot_command(
            args,
            kwargs,
            policy,
        )
        call_args = list(args)
        call_kwargs = dict(kwargs)
        if call_args:
            call_args[0] = rewritten
        else:
            call_kwargs["args"] = rewritten
        child_temp = _prepare_controlled_child_temp(policy, environment)
        call_kwargs["env"] = environment
        policy["spawn_permit"] = int(policy.get("spawn_permit", 0)) + 1
        policy["thread_permit"] = int(policy.get("thread_permit", 0)) + 1
        try:
            try:
                completed = previous_run(*call_args, **call_kwargs)
            except _subprocess.TimeoutExpired as error:
                error.cmd = original
                error.args = (original, error.timeout)
                raise
            except _subprocess.CalledProcessError as error:
                error.cmd = original
                error.args = (error.returncode, original)
                raise
        finally:
            policy["spawn_permit"] = int(policy["spawn_permit"]) - 1
            policy["thread_permit"] = int(policy["thread_permit"]) - 1
            if child_temp is not None:
                _remove_controlled_child_temp(*child_temp)
        completed.args = original
        return completed

    def guarded_thread_start(thread, *args, **kwargs):
        policy = getattr(_ADAPTER_FS_POLICY, "value", None)
        if (
            isinstance(policy, dict)
            and int(policy.get("thread_permit", 0)) <= 0
        ):
            raise PermissionError("冻结 Adapter 不允许启动新 thread/线程")
        return previous_thread_start(thread, *args, **kwargs)

    def guarded_start_new_thread(function, args, kwargs=None):
        policy = getattr(_ADAPTER_FS_POLICY, "value", None)
        if (
            isinstance(policy, dict)
            and int(policy.get("thread_permit", 0)) <= 0
        ):
            raise PermissionError("冻结 Adapter 不允许启动新 thread/线程")
        if kwargs is None:
            return previous_start_new_thread(function, args)
        return previous_start_new_thread(function, args, kwargs)

    def guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
        policy = getattr(_ADAPTER_FS_POLICY, "value", None)
        if isinstance(policy, dict) and dir_fd is not None:
            raise PermissionError(
                "冻结 Adapter 不允许使用 dir_fd/目录描述符打开路径"
            )
        if dir_fd is None:
            descriptor = previous_os_open(path, flags, mode)
            try:
                if (
                    isinstance(policy, dict)
                    and type(flags) is int
                    and bool(flags & _WRITE_OPEN_FLAGS)
                ):
                    allowed = policy.get("allowed_write_fds")
                    if isinstance(allowed, dict):
                        allowed[descriptor] = (
                            _write_file_descriptor_identity(descriptor)
                        )
                return descriptor
            except BaseException:
                _os.close(descriptor)
                raise
        return previous_os_open(path, flags, mode, dir_fd=dir_fd)

    def guarded_os_dup(*args, **kwargs):
        if isinstance(getattr(_ADAPTER_FS_POLICY, "value", None), dict):
            raise PermissionError(
                "冻结 Adapter 不允许复制或换绑文件描述符"
            )
        if previous_os_dup is None:
            raise AttributeError("os.dup 不可用")
        return previous_os_dup(*args, **kwargs)

    def guarded_os_dup2(*args, **kwargs):
        if isinstance(getattr(_ADAPTER_FS_POLICY, "value", None), dict):
            raise PermissionError(
                "冻结 Adapter 不允许复制或换绑文件描述符"
            )
        if previous_os_dup2 is None:
            raise AttributeError("os.dup2 不可用")
        return previous_os_dup2(*args, **kwargs)

    _subprocess.run = guarded_run
    _threading.Thread.start = guarded_thread_start
    _thread_module.start_new_thread = guarded_start_new_thread
    _os.open = guarded_os_open
    if previous_os_dup is not None:
        _os.dup = guarded_os_dup
    if previous_os_dup2 is not None:
        _os.dup2 = guarded_os_dup2
    if _nt is not None and previous_nt_dup is not None:
        _nt.dup = guarded_os_dup
    if _nt is not None and previous_nt_dup2 is not None:
        _nt.dup2 = guarded_os_dup2
    try:
        yield
    finally:
        if _nt is not None and previous_nt_dup2 is not None:
            _nt.dup2 = previous_nt_dup2
        if _nt is not None and previous_nt_dup is not None:
            _nt.dup = previous_nt_dup
        if previous_os_dup2 is not None:
            _os.dup2 = previous_os_dup2
        if previous_os_dup is not None:
            _os.dup = previous_os_dup
        _os.open = previous_os_open
        _thread_module.start_new_thread = previous_start_new_thread
        _threading.Thread.start = previous_thread_start
        _subprocess.run = previous_run
        if previous is missing:
            try:
                del _ADAPTER_FS_POLICY.value
            except AttributeError:
                pass
        else:
            _ADAPTER_FS_POLICY.value = previous


def _audit_adapter_filesystem(event: str, args: tuple[object, ...]) -> None:
    policy = getattr(_ADAPTER_FS_POLICY, "value", None)
    if not isinstance(policy, dict):
        return
    if event == "open":
        if len(args) < 3:
            raise PermissionError("冻结 Adapter 的文件打开请求结构无效")
        mode = args[1]
        flags = args[2]
        write_requested = (
            isinstance(mode, str)
            and any(character in mode for character in "wax+")
        ) or (
            type(flags) is int
            and bool(flags & _WRITE_OPEN_FLAGS)
        )
        if write_requested:
            if isinstance(args[0], int):
                allowed = policy.get("allowed_write_fds")
                token = (
                    allowed.pop(args[0], None)
                    if isinstance(allowed, dict)
                    else None
                )
                if token is not None:
                    try:
                        current = _write_file_descriptor_identity(args[0])
                    except (OSError, PermissionError) as error:
                        raise PermissionError(
                            "冻结 Adapter 文件描述符已经关闭或失效"
                        ) from error
                    if current != token:
                        raise PermissionError(
                            "冻结 Adapter 文件描述符身份已经变化"
                        )
                    return
            _require_adapter_write_path(args[0], policy)
        return
    if event in _SINGLE_PATH_MUTATIONS:
        path_index, dir_fd_index = _SINGLE_PATH_MUTATIONS[event]
        if len(args) <= path_index:
            raise PermissionError("冻结 Adapter 的文件修改请求结构无效")
        _reject_nondefault_dir_fd(args, dir_fd_index)
        _reject_protected_child_temp_root(args[path_index], policy)
        _require_adapter_write_path(args[path_index], policy)
        return
    if event in _DOUBLE_PATH_MUTATIONS:
        source_index, target_index, source_fd_index, target_fd_index = (
            _DOUBLE_PATH_MUTATIONS[event]
        )
        if len(args) <= max(source_index, target_index):
            raise PermissionError("冻结 Adapter 的文件移动请求结构无效")
        _reject_nondefault_dir_fd(args, source_fd_index)
        _reject_nondefault_dir_fd(args, target_fd_index)
        _reject_protected_child_temp_root(args[source_index], policy)
        _reject_protected_child_temp_root(args[target_index], policy)
        _reject_protected_child_temp_escape(
            args[source_index],
            args[target_index],
            policy,
        )
        _require_adapter_write_path(args[source_index], policy)
        _require_adapter_write_path(args[target_index], policy)
        return
    if event in _COPY_TARGET_MUTATIONS:
        if len(args) < 2:
            raise PermissionError("冻结 Adapter 的文件复制请求结构无效")
        _reject_protected_child_temp_root(args[1], policy)
        _reject_protected_child_temp_escape(args[0], args[1], policy)
        _require_adapter_write_path(args[1], policy)
        return
    if event in _LINK_MUTATIONS:
        raise PermissionError("冻结 Adapter 不允许创建 link/链接")
    if event == "subprocess.Popen":
        if int(policy.get("spawn_permit", 0)) <= 0:
            raise PermissionError("冻结 Adapter 不允许任意 subprocess/子进程")
        return
    if (
        event in {"os.system", "os.startfile"}
        or event.startswith("os.exec")
        or event.startswith("os.spawn")
        or event.startswith("os.posix_spawn")
    ):
        raise PermissionError("冻结 Adapter 不允许任意 subprocess/子进程")
    if event == "os.chdir":
        raise PermissionError("冻结 Adapter 不允许修改进程工作目录")


def _reject_nondefault_dir_fd(
    args: tuple[object, ...],
    index: int | None,
) -> None:
    if index is None or len(args) <= index:
        return
    if args[index] not in {None, -1}:
        raise PermissionError(
            "冻结 Adapter 不允许使用 dir_fd/目录描述符修改路径"
        )


def _require_adapter_write_path(
    value: object,
    policy: dict[str, object],
) -> None:
    allowed_root = policy.get("allowed_root")
    if not isinstance(allowed_root, _Path):
        raise PermissionError(
            "冻结 Adapter 当前阶段为只读，禁止写文件"
        )
    if isinstance(value, int):
        raise PermissionError(
            "冻结 Adapter 不允许通过已有文件描述符绕过输出目录"
        )
    try:
        candidate = _Path(_os.fsdecode(value)).resolve(strict=False)
        relative = candidate.relative_to(allowed_root)
        if not relative.parts:
            raise ValueError("不能修改固定输出根本身")
    except (OSError, TypeError, ValueError) as error:
        raise PermissionError(
            "冻结 Adapter 只能写入 .cv-workflow-output 固定输出目录"
        ) from error


def _reject_protected_child_temp_root(
    value: object,
    policy: dict[str, object],
) -> None:
    protected = policy.get("protected_child_temp")
    if not isinstance(protected, _Path) or isinstance(value, int):
        return
    candidate = _adapter_policy_path(value)
    if candidate is None:
        return
    if candidate == protected:
        raise PermissionError(
            "受控 Adapter child 临时目录根不能移动、替换或删除"
        )


def _reject_protected_child_temp_escape(
    source: object,
    target: object,
    policy: dict[str, object],
) -> None:
    protected = policy.get("protected_child_temp")
    if not isinstance(protected, _Path):
        return
    source_path = _adapter_policy_path(source)
    target_path = _adapter_policy_path(target)
    if source_path is None or target_path is None:
        return
    if (
        _path_is_same_or_below(source_path, protected)
        and not _path_is_same_or_below(target_path, protected)
    ):
        raise PermissionError(
            "受控 Adapter child 临时目录内容不能移出或复制出临时根"
        )


def _adapter_policy_path(value: object) -> _Path | None:
    if isinstance(value, int):
        return None
    try:
        return _Path(_os.path.abspath(_os.fsdecode(value)))
    except (OSError, TypeError, ValueError):
        return None


def _path_is_same_or_below(candidate: _Path, root: _Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _write_file_descriptor_identity(
    descriptor: int,
) -> tuple[int, int, int]:
    try:
        info = _os.fstat(descriptor)
    except OSError:
        raise
    identity = (
        info.st_dev,
        info.st_ino,
        _stat.S_IFMT(info.st_mode),
    )
    if any(type(value) is not int for value in identity):
        raise PermissionError(
            "平台缺少稳定的 Adapter 文件描述符身份字段"
        )
    if not _stat.S_ISREG(info.st_mode):
        raise PermissionError(
            "冻结 Adapter 只允许把普通输出文件交给 fdopen"
        )
    return identity


def _controlled_snapshot_command(
    args: tuple[object, ...],
    kwargs: dict[str, object],
    policy: dict[str, object],
) -> tuple[list[str], list[str], dict[str, str]]:
    if kwargs.get("shell") not in {None, False}:
        raise PermissionError("冻结 Adapter 子进程不允许 shell=True")
    if kwargs.get("executable") is not None:
        raise PermissionError("冻结 Adapter 子进程不允许替换解释器")
    if kwargs.get("preexec_fn") is not None:
        raise PermissionError("冻结 Adapter 子进程不允许 preexec_fn")
    command = args[0] if args else kwargs.get("args")
    if not isinstance(command, (list, tuple)):
        raise PermissionError("冻结 Adapter 子进程命令必须是参数数组")
    try:
        original = [
            _os.fsdecode(value)
            for value in command
        ]
    except (TypeError, UnicodeError) as error:
        raise PermissionError("冻结 Adapter 子进程参数无效") from error
    if (
        len(original) < 6
        or original[1:5] != ["-B", "-X", "utf8", "-m"]
    ):
        raise PermissionError(
            "冻结 Adapter 只允许受控的 python -B -X utf8 -m 子进程"
        )
    try:
        executable = _Path(original[0]).resolve(strict=True)
        current_python = _Path(_sys.executable).resolve(strict=True)
    except OSError as error:
        raise PermissionError("冻结 Adapter 子进程解释器无效") from error
    if executable != current_python:
        raise PermissionError("冻结 Adapter 子进程必须使用当前 Python")
    snapshot_root = policy.get("snapshot_root")
    if not isinstance(snapshot_root, _Path):
        raise PermissionError("冻结 Adapter 子进程缺少快照根")
    cwd_value = kwargs.get("cwd")
    try:
        cwd = _Path(_os.fsdecode(cwd_value)).resolve(strict=True)
    except (OSError, TypeError, ValueError) as error:
        raise PermissionError("冻结 Adapter 子进程 cwd 无效") from error
    if cwd != snapshot_root:
        raise PermissionError("冻结 Adapter 子进程 cwd 必须是快照根")
    module = original[5]
    _validate_snapshot_module_target(snapshot_root, module)
    environment_value = kwargs.get("env")
    if environment_value is None:
        environment = dict(_os.environ)
    elif isinstance(environment_value, dict):
        try:
            environment = {
                _os.fsdecode(key): _os.fsdecode(value)
                for key, value in environment_value.items()
            }
        except (TypeError, UnicodeError) as error:
            raise PermissionError("冻结 Adapter 子进程环境无效") from error
    else:
        raise PermissionError("冻结 Adapter 子进程环境无效")
    for name in list(environment):
        if name.upper() in {
            "PYTHONHOME",
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "PYTHONINSPECT",
        }:
            environment.pop(name, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    allowed_root = policy.get("allowed_root")
    mode = "write" if isinstance(allowed_root, _Path) else "readonly"
    bootstrap = _Path(__file__).with_name("_adapter_child.py")
    rewritten = [
        str(current_python),
        "-I",
        "-B",
        "-X",
        "utf8",
        str(bootstrap),
        str(snapshot_root),
        mode,
        module,
        *original[6:],
    ]
    return original, rewritten, environment


def _protected_child_temp_from_environment(
    allowed_root: _Path | None,
) -> _Path | None:
    if not isinstance(allowed_root, _Path):
        return None
    names = (
        "TEMP",
        "TMP",
        "TMPDIR",
        "TORCHINDUCTOR_CACHE_DIR",
        "TRITON_CACHE_DIR",
        "XDG_CACHE_HOME",
        "MPLCONFIGDIR",
    )
    values = [_os.environ.get(name) for name in names]
    if any(not isinstance(value, str) or not value for value in values):
        return None
    try:
        candidates = {
            _Path(_os.path.abspath(_os.fsdecode(value)))
            for value in values
        }
    except (OSError, TypeError, ValueError):
        return None
    if len(candidates) != 1:
        return None
    candidate = candidates.pop()
    if (
        candidate.parent != allowed_root
        or not candidate.name.startswith(".adapter-child-")
    ):
        return None
    return candidate


def _prepare_controlled_child_temp(
    policy: dict[str, object],
    environment: dict[str, str],
) -> tuple[_Path, tuple[int, int]] | None:
    allowed_root = policy.get("allowed_root")
    if not isinstance(allowed_root, _Path):
        return None
    target = allowed_root / f".adapter-child-{_uuid.uuid4().hex}"
    target.mkdir(mode=0o700)
    info = target.lstat()
    if (
        target.is_symlink()
        or bool(getattr(info, "st_file_attributes", 0) & 0x400)
        or not target.is_dir()
    ):
        raise PermissionError("受控 Adapter child 临时目录无效")
    identity = (info.st_dev, info.st_ino)
    if any(type(value) is not int for value in identity):
        raise PermissionError("平台缺少 child 临时目录身份字段")
    for name in (
        "TEMP",
        "TMP",
        "TMPDIR",
        "TORCHINDUCTOR_CACHE_DIR",
        "TRITON_CACHE_DIR",
        "XDG_CACHE_HOME",
        "MPLCONFIGDIR",
    ):
        environment[name] = str(target)
    return target, identity


def _remove_controlled_child_temp(
    target: _Path,
    identity: tuple[int, int],
) -> None:
    if not _os.path.lexists(target):
        raise PermissionError(
            "受控 Adapter child 临时目录消失，拒绝把子进程视为成功"
        )
    info = target.lstat()
    if (
        target.is_symlink()
        or bool(getattr(info, "st_file_attributes", 0) & 0x400)
        or not target.is_dir()
        or (info.st_dev, info.st_ino) != identity
        or not target.name.startswith(".adapter-child-")
    ):
        raise PermissionError(
            "受控 Adapter child 临时目录身份变化，已保留现场对象"
        )
    _shutil.rmtree(target)


def _validate_snapshot_module_target(root: _Path, module: str) -> None:
    parts = module.split(".")
    if not parts or any(not part or not part.isidentifier() for part in parts):
        raise PermissionError("冻结 Adapter 子进程模块名无效")
    protected = {
        _module_key(name)
        for name in _discover_top_level_modules(
            root,
            reject_normalized_collisions=True,
        )
    }
    if _module_key(parts[0]) not in protected:
        raise PermissionError("冻结 Adapter 子进程模块不在执行快照中")
    search_path = [str(root)]
    fullname = ""
    for part in parts:
        fullname = part if not fullname else f"{fullname}.{part}"
        spec = _machinery.PathFinder.find_spec(fullname, search_path)
        if spec is None:
            raise PermissionError(
                f"冻结 Adapter 子进程模块不存在：{module}"
            )
        if spec.origin not in {None, "namespace"} and not _path_is_within_root(
            _Path(spec.origin),
            root,
        ):
            raise PermissionError("冻结 Adapter 子进程模块逃出执行快照")
        locations = spec.submodule_search_locations
        if locations is not None:
            if any(
                not _path_is_within_root(_Path(location), root)
                for location in locations
            ):
                raise PermissionError("冻结 Adapter 子进程包路径逃出执行快照")
            search_path = list(locations)
        elif part != parts[-1]:
            raise PermissionError(
                f"冻结 Adapter 子进程模块层级无效：{module}"
            )


def _run_snapshot_child(
    snapshot_root: str,
    mode: str,
    module: str,
    module_args: list[str],
) -> None:
    import runpy

    root = _Path(snapshot_root).resolve(strict=True)
    if mode not in {"readonly", "write"}:
        raise ValueError("Child Adapter 写入模式无效")
    allowed_root = (
        root / ".cv-workflow-output"
        if mode == "write"
        else None
    )
    top_level_modules = _discover_top_level_modules(
        root,
        reject_normalized_collisions=True,
    )
    _validate_snapshot_module_target(root, module)
    saved_argv = list(_sys.argv)
    try:
        with _adapter_import_scope(
            root,
            top_level_modules,
            normalized_names=True,
        ):
            with _adapter_filesystem_scope(
                allowed_root,
                snapshot_root=root,
                allow_snapshot_subprocess=False,
            ):
                _sys.argv[:] = [module, *module_args]
                runpy.run_module(module, run_name="__main__", alter_sys=True)
                _validate_snapshot_module_origins(
                    root,
                    top_level_modules,
                )
    finally:
        _sys.argv[:] = saved_argv


def _path_is_within_root(path: _Path, root: _Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _trusted_runtime_paths(paths: list[str]) -> list[str]:
    prefixes = []
    for value in {
        _sys.base_prefix,
        _sys.prefix,
        _sys.exec_prefix,
    }:
        try:
            prefixes.append(_Path(value).resolve(strict=True))
        except OSError:
            continue
    trusted: list[str] = []
    for value in paths:
        if not value:
            continue
        candidate = _Path(value).resolve(strict=False)
        if any(_path_is_within_root(candidate, prefix) for prefix in prefixes):
            trusted.append(value)
    return trusted


def _snapshot_process_environment(
    root: _Path,
) -> dict[str, str | None]:
    values: dict[str, str | None] = {
        name: None
        for name in _os.environ
        if name.startswith("GIT_")
    }
    values.update({
        "PYTHONPATH": None,
        # Snapshot 中没有 .git。显式指向一个不存在且受只读根目录保护的
        # GIT_DIR，避免项目目录本身就是源码仓库时向上发现现场 .git。
        "GIT_DIR": str(root / ".cv-workflow-no-git"),
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_CONFIG_GLOBAL": _os.devnull,
        "GIT_CONFIG_SYSTEM": _os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CEILING_DIRECTORIES": str(root),
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "0",
    })
    return values


def adapter_summary(adapter: ProjectAdapter) -> dict[str, str]:
    values = getattr(adapter, "_summary", ())
    summary = (
        dict(zip(("sha256", "repo_url", "commit", "source"), values))
        if isinstance(values, tuple) and len(values) == 4
        else {}
    )
    if (
        set(summary) != {"sha256", "repo_url", "commit", "source"}
        or _re.fullmatch(r"[0-9a-f]{64}", summary["sha256"]) is None
        or _re.fullmatch(r"[0-9a-fA-F]{40}", summary["commit"]) is None
        or not summary["repo_url"]
        or not summary["source"]
    ):
        raise ValueError("Adapter 摘要无效")
    return summary


def _json_object(value: object, label: str) -> dict[str, _Any]:
    normalized = _finite_json(value, label)
    if not isinstance(normalized, dict):
        raise ValueError(f"{label} 必须返回 JSON object")
    return normalized


def _finite_json(value: object, label: str) -> _Any:
    try:
        encoded = _json.dumps(value, ensure_ascii=False, allow_nan=False)
        normalized = _json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} 必须是有限 JSON 值") from error
    if normalized != value:
        raise ValueError(f"{label} 序列化后会改变，拒绝接收")
    return normalized


_sys.addaudithook(_audit_adapter_filesystem)
