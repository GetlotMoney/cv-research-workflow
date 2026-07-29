from __future__ import annotations

import stat as _stat
from pathlib import Path as _Path
from typing import Any as _Any

from .adapters import ProjectAdapter as _ProjectAdapter


def start(
    project: _Path,
    adapter: _ProjectAdapter,
    run: dict[str, _Any],
    *,
    purpose: str,
    backend: str,
) -> dict[str, _Any]:
    _validate_request(run, purpose, backend)
    return _execute(project, adapter, "start", run, backend=backend)


def status(
    project: _Path,
    adapter: _ProjectAdapter,
    run: dict[str, _Any],
    *,
    purpose: str,
    backend: str,
) -> dict[str, _Any]:
    _validate_request(run, purpose, backend)
    return _execute(project, adapter, "status", run, backend=backend)


def stop(
    project: _Path,
    adapter: _ProjectAdapter,
    run: dict[str, _Any],
    *,
    purpose: str,
    backend: str,
) -> dict[str, _Any]:
    _validate_request(run, purpose, backend)
    return _execute(project, adapter, "stop", run, backend=backend)


def collect(
    project: _Path,
    adapter: _ProjectAdapter,
    run: dict[str, _Any],
    *,
    purpose: str,
    backend: str,
) -> dict[str, _Any]:
    _validate_request(run, purpose, backend)
    _require_project_backend(backend)
    parsed = adapter.parse_result(project, run)
    if set(parsed) != {"execution", "result", "quality", "analysis", "artifacts"}:
        raise ValueError("Runner collect 返回结构无效")
    if not all(isinstance(parsed[name], dict) for name in (
        "execution", "result", "quality", "analysis"
    )) or not isinstance(parsed["artifacts"], list):
        raise ValueError("Runner collect 返回结构无效")
    _validate_bound_output_paths(project, run, parsed)
    return parsed


def _validate_bound_output_paths(
    project: _Path,
    run: dict[str, _Any],
    parsed: dict[str, _Any],
) -> None:
    from .project import is_link_or_reparse
    from .records import normalize_safe_relative_path
    from .run_identity import is_bound_frozen

    if not is_bound_frozen(run.get("frozen")):
        return
    snapshot = run.get("execution_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("Bound Run is missing execution_snapshot")
    output_root = snapshot.get("output_relative_path")
    if not isinstance(output_root, str) or not output_root:
        raise ValueError("Bound Run output root is invalid")

    raw_log = parsed["result"].get("raw_log")
    values: list[tuple[str, object]] = []
    if raw_log is not None:
        values.append(("raw_log", raw_log))
    values.extend(("artifact", value) for value in parsed["artifacts"])
    prefix = output_root + "/"
    for label, value in values:
        normalized = normalize_safe_relative_path(value, label)
        if not normalized.startswith(prefix):
            raise ValueError(
                f"Bound Run {label} must be inside its unique output root"
            )
        current = _Path(project)
        segments = normalized.split("/")
        for index, segment in enumerate(segments):
            current = current / segment
            try:
                info = current.lstat()
            except OSError as error:
                raise ValueError(
                    f"Bound Run {label} output does not exist"
                ) from error
            if is_link_or_reparse(current):
                raise ValueError(
                    f"Bound Run {label} output contains a link/reparse point"
                )
            if index < len(segments) - 1:
                if not _stat.S_ISDIR(info.st_mode):
                    raise ValueError(
                        f"Bound Run {label} output parent is not a directory"
                    )
            elif (
                not _stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
            ):
                raise ValueError(
                    f"Bound Run {label} must be one owned regular file"
                )


def _execute(
    project: _Path,
    adapter: _ProjectAdapter,
    action: str,
    run: dict[str, _Any],
    *,
    backend: str,
) -> dict[str, _Any]:
    if action not in {"start", "status", "stop"}:
        raise ValueError("Runner action 只允许 start/status/stop")
    _require_project_backend(backend)
    response = adapter.execute(project, action, run)
    return _validate_response(action, response)


def _validate_request(run: dict[str, _Any], purpose: str, backend: str) -> None:
    if purpose not in {"debug", "evidence"}:
        raise ValueError("Runner purpose 只允许 debug/evidence")
    if run.get("purpose") != purpose:
        raise ValueError("Runner purpose 与冻结 Run 不一致")
    if backend not in {"local", "project"}:
        raise ValueError("Runner backend 只允许 local/project")


def _require_project_backend(backend: str) -> None:
    if backend == "local":
        raise NotImplementedError("local backend 尚无真实项目需求")


def _validate_response(action: str, response: dict[str, _Any]) -> dict[str, _Any]:
    if action == "start":
        if set(response) != {"status", "process_id"} or response["status"] not in {
            "running", "finished", "stopped"
        }:
            raise ValueError("Runner start 返回结构无效")
        process_id = response["process_id"]
        if process_id is not None and (
            isinstance(process_id, bool)
            or not isinstance(process_id, int)
            or process_id <= 0
        ):
            raise ValueError("Runner start process_id 无效")
    elif action == "status":
        if set(response) != {"status"} or response["status"] not in {
            "not_started",
            "running",
            "cleanup_pending",
            "finished",
            "failed",
            "stopped",
        }:
            raise ValueError("Runner status 返回结构无效")
    elif action == "stop":
        if set(response) != {"status"} or response["status"] not in {
            "stop_requested",
            "stopped",
        }:
            raise ValueError("Runner stop 返回结构无效")
    return dict(response)
