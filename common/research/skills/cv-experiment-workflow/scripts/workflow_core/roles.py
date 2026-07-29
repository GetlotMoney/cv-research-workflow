from __future__ import annotations

from pathlib import Path
from typing import Any

from .attempts import validate_project_workflow_locked
from .io import atomic_write_bytes, read_bounded_regular_file
from .locking import project_write_lock
from .records import _initialized_project, _validate_project
from .validation import ROLE_MEMORY_LIMIT, ROLE_NAMES, validate_role_memories_locked


def update_role_memory(
    project: Path,
    role: str,
    content_file: Path,
) -> dict[str, Any]:
    if role not in ROLE_NAMES:
        raise ValueError(f"未知角色：{role}")
    content = read_bounded_regular_file(
        Path(content_file), ROLE_MEMORY_LIMIT, "角色 memory content-file",
    )
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("角色 memory content-file 必须是 UTF-8") from error

    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root)
        validate_project_workflow_locked(control)
        target = control / "agents" / f"{role}.md"
        atomic_write_bytes(target, content)
        validate_role_memories_locked(control / "agents")
        return {
            "role": role,
            "path": target.relative_to(root).as_posix(),
            "bytes": len(content),
        }
