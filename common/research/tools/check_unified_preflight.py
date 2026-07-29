from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "cv-experiment-workflow" / "scripts"
sys.dont_write_bytecode = True
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from workflow_core.paper_package import BOUND_EVIDENCE_PRODUCER  # noqa: E402
from workflow_core.planning import read_console_snapshot  # noqa: E402
from workflow_core.project import is_link_or_reparse  # noqa: E402


PREFLIGHT_SCHEMA = "cv-unified-preflight.v1"
EXPECTED_PROJECT_SCHEMA = "cv-experiment-workflow.project.v2"


def check_unified_preflight(
    project: Path,
    paperflow_root: Path,
    library_root: Path,
) -> dict[str, Any]:
    """只读核对科研项目、PaperFlow 接收器和版本握手。"""
    project_root = _real_directory(project, "project")
    paperflow = _real_directory(paperflow_root, "paperflow_root")
    library = _real_directory(library_root, "library_root")

    snapshot = read_console_snapshot(project_root)
    if snapshot["project"].get("schema") != EXPECTED_PROJECT_SCHEMA:
        raise ValueError("统一入口只接受 v2 科研项目")

    module_path = paperflow / "paperflow_v2" / "__main__.py"
    receiver_path = paperflow / "paperflow_v2" / "research_package.py"
    if not _real_file(module_path) or not _real_file(receiver_path):
        raise ValueError("PaperFlow 候选源码不完整")

    paperflow_text = str(paperflow)
    if paperflow_text not in sys.path:
        sys.path.insert(0, paperflow_text)
    from paperflow_v2 import research_package as receiver

    loaded_receiver = Path(receiver.__file__).resolve(strict=True)
    if loaded_receiver != receiver_path.resolve(strict=True):
        raise ValueError("PaperFlow 接收器不是来自指定候选根")

    producer = (
        BOUND_EVIDENCE_PRODUCER["skill_id"],
        BOUND_EVIDENCE_PRODUCER["release_version"],
        BOUND_EVIDENCE_PRODUCER["system_version"],
    )
    if producer not in receiver.SUPPORTED_PRODUCERS:
        raise ValueError("科研交付包与 PaperFlow 接收器版本不匹配")

    return {
        "schema": PREFLIGHT_SCHEMA,
        "status": "pass",
        "project": {
            "schema": snapshot["project"]["schema"],
            "project_id": snapshot["project"]["project_id"],
        },
        "producer": {
            "skill_id": producer[0],
            "release_version": producer[1],
            "system_version": producer[2],
        },
        "paperflow": {
            "receiver_profile": receiver.PRODUCER_PROFILES[producer],
            "entry_module": "paperflow_v2",
        },
        "library": {
            "status": "present",
            "database_present": _real_file(
                library / "database" / "knowledge.db"
            ),
        },
    }


def _real_directory(path: Path, label: str) -> Path:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"{label} 不存在或无法读取") from error
    if is_link_or_reparse(candidate) or not resolved.is_dir():
        raise ValueError(f"{label} 必须是本机普通目录")
    return resolved


def _real_file(path: Path) -> bool:
    try:
        return (
            not is_link_or_reparse(path)
            and path.is_file()
            and os.path.samefile(path, path.resolve(strict=True))
        )
    except OSError:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读核对统一入口启动条件")
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--paperflow-root", required=True, type=Path)
    parser.add_argument("--library-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        report = check_unified_preflight(
            arguments.project,
            arguments.paperflow_root,
            arguments.library_root,
        )
    except (ImportError, OSError, ValueError):
        parser.exit(2, "统一入口启动前核对失败；没有创建目录或启动服务。\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
