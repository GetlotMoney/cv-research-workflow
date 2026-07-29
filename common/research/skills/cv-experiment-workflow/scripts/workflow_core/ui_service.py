from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from typing import Any
from urllib.parse import urlsplit

from .codebases import verify_codebase_gate
from .domain_packs import (
    create_domain_repository,
    direction_availability,
    resolve_domain_pack,
)
from .engine import execute_task
from .evidence import current_evidence_level, record_evidence_transition
from .intake import INTENTS, evaluate_intake
from .io import atomic_write_json
from .output_seal import seal_run_outputs
from .paper_package import (
    export_paper_package,
    save_research_brief,
    seal_paper_package,
    verify_paper_package,
)
from .project import init_project, is_link_or_reparse
from .tasking import create_task
from .v2_catalog import (
    IDEA_SCHEMA,
    SOURCE_SCHEMA,
    register_source,
    revise_catalog_idea,
    save_idea,
)
from .planning import (
    intake_discovery_from_snapshot,
    read_console_snapshot,
)


CONSOLE_STATE_SCHEMA = "cv-experiment-workflow.console-state.v1"
CONSOLE_WRITE_SCHEMA = "cv-experiment-workflow.console-write.v1"
_WRITE_ACTIONS = {
    "save_research_brief", "create_domain_repo", "register_source",
    "save_idea", "revise_idea", "select_codebase", "create_task",
    "run_task_debug", "run_task_evidence", "seal_run_outputs",
    "confirm_run_evidence", "freeze_research_package",
}
PROJECT_ACTIONS = {"create_project", "open_project"}
_REPOSITORY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_TASK_FIELDS = {"owner_request", "route", "target_refs", "route_inputs", "budget", "stop_condition"}
_IDEA_FIELDS = {"status", "problem", "mechanism", "falsifiable_hypothesis", "source_refs", "evidence_refs", "source_links"}
_SOURCE_BASE_FIELDS = {
    "source_path", "kind", "identity", "locator", "revision", "commit",
    "digest", "license",
}
_CONDITION_ORDER = (
    "project_snapshot",
    "workflow_lock",
    "intake_complete",
    "adapter_config",
    "adapter_runtime",
    "controlled_actions",
)


def console_state(
    project: Path,
    intent: str = "status",
    provided: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回本地控制台的状态数据合同。"""
    if intent not in INTENTS:
        raise ValueError(f"intent 无效：{intent}")
    if provided is not None and not isinstance(provided, dict):
        raise ValueError("provided 必须是 JSON object")
    try:
        snapshot = read_console_snapshot(Path(project))
    except (OSError, ValueError):
        return _invalid_state(intent)

    discovered = intake_discovery_from_snapshot(snapshot)
    intake = evaluate_intake(
        intent=intent,
        provided={} if provided is None else provided,
        discovered=discovered,
    )
    relationships = _relationships(snapshot)
    counts = {
        "sources": len(snapshot["catalog"]["sources"]),
        "ideas": len(snapshot["catalog"]["ideas"]),
        "templates": len(snapshot["catalog"]["templates"]),
        "modules": len(snapshot["catalog"]["modules"]),
        "tasks": len(snapshot["tasks"]),
        "runs": len(snapshot["runs"]),
        "evidence": sum(
            node["kind"] == "evidence"
            for node in relationships["nodes"]
        ),
        "nodes": len(relationships["nodes"]),
        "edges": len(relationships["edges"]),
    }
    conditions = _valid_conditions(snapshot, intake)
    blocked_by = [
        item["id"] for item in conditions if item["status"] == "block"
    ]
    return {
        "schema": CONSOLE_STATE_SCHEMA,
        "mode": "controlled",
        "read_only": False,
        "execution_enabled": True,
        "snapshot": {
            "status": "valid",
            "layout": snapshot["layout"],
            "counts": counts,
        },
        "start": {
            "project": _project_start(snapshot),
            "workflow": _workflow_start(snapshot),
            "adapter": _adapter_start(snapshot),
            "intake": intake,
        },
        "relationships": relationships,
        "execution": {
            "phase": "controlled_local_actions",
            "intent": intent,
            "preview": intake,
            "conditions": conditions,
            "blocked_by": blocked_by,
            "next_step": _next_step(conditions, intake),
        },
    }


def console_write_action(
    project: Path,
    action: str,
    data: dict[str, Any],
    *,
    paperflow_entrypoint: str | None = None,
) -> dict[str, Any]:
    """统一入口的固定动作合同，只调用已有生产函数。"""
    if action not in _WRITE_ACTIONS or not isinstance(data, dict):
        raise ValueError("无效的控制台写入动作")
    validated_paperflow_entrypoint = (
        None
        if paperflow_entrypoint is None
        else _validated_paperflow_entrypoint(paperflow_entrypoint)
    )
    root = Path(project).resolve(strict=True)
    # 写动作开始前先读取并验证完整项目快照，普通目录或损坏项目不得产生任何文件。
    read_console_snapshot(root)
    if action == "save_research_brief":
        with _temporary_json_manifest(data) as path:
            brief = save_research_brief(root, path)
        return {
            "schema": CONSOLE_WRITE_SCHEMA,
            "action": action,
            "status": "saved",
            "summary": (
                f"已保存 {brief['brief_id']}（第 {brief['revision']} 版）；"
                "下一步可保存 Idea 或创建方向仓库。"
            ),
            "next_action": "save_idea",
            "result": {
                "brief_id": brief["brief_id"],
                "revision": brief["revision"],
                "created_at": brief["created_at"],
                "title": brief["title"],
            },
            "paperflow": {
                "status": "unavailable",
                "entrypoint": None,
                "reason": "research_package_not_frozen",
            },
            "state": console_state(project),
        }
    if action == "create_domain_repo":
        result = _create_domain_repo(root, data)
        codebase = result["codebase"]
        return _action_result(
            project,
            action,
            "created",
            result,
            summary=(
                f"已创建并登记 {codebase['id']}；"
                "下一步选择这个 Codebase，核对 branch/commit/tag。"
            ),
            next_action="select_codebase",
        )
    if action == "register_source":
        result = _register_source(root, data)
        public_result = {
            key: result[key]
            for key in (
                "id",
                "kind",
                "identity",
                "revision",
                "digest",
            )
        }
        return _action_result(
            project,
            action,
            "registered",
            public_result,
            summary=(
                f"已登记 {result['id']}（{result['kind']}）；"
                "下一步保存 Idea，并说明这个来源支持哪一项判断。"
            ),
            next_action="save_idea",
        )
    if action == "save_idea":
        manifest = _idea_manifest(data)
        with _temporary_json_manifest(manifest) as path:
            result = save_idea(root, path)
        return _action_result(
            project,
            action,
            "saved",
            result,
            summary=f"已保存 {result['id']}；后续 Task 可以引用这个 Idea。",
            next_action="create_task",
        )
    if action == "revise_idea":
        if set(data) != {"idea_id", "revision_reason", "idea"} or not isinstance(data["idea"], dict):
            raise ValueError("Idea 修订必须包含 idea_id/revision_reason/idea")
        manifest = _idea_manifest(data["idea"])
        with _temporary_json_manifest(manifest) as path:
            result = revise_catalog_idea(root, data["idea_id"], path, data["revision_reason"])
        return _action_result(
            project,
            action,
            "saved",
            result,
            summary=f"已保存 {result['id']} 的新修订。",
            next_action="create_task",
        )
    if action == "select_codebase":
        if set(data) != {"codebase_id"}:
            raise ValueError("选择 Codebase 只能包含 codebase_id")
        result = verify_codebase_gate(root, data["codebase_id"])
        short_commit = str(result["commit"])[:12]
        return _action_result(
            project,
            action,
            "selected",
            result,
            summary=(
                f"已核对 {result['codebase_id']}：branch={result['branch']}，"
                f"commit={short_commit}；下一步创建 Task。"
            ),
            next_action="create_task",
        )
    if action == "create_task":
        if set(data) != _TASK_FIELDS or data.get("route") not in {"tune", "ablation", "reproduction", "innovation"}:
            raise ValueError("Task 必须使用固定四路线和完整结构化字段")
        result = create_task(root, **data)
        return _action_result(
            project,
            action,
            "created",
            result,
            summary=f"已创建 {result['id']}；先运行 debug Run。",
            next_action="run_task_debug",
        )
    if action in {"run_task_debug", "run_task_evidence"}:
        if set(data) != {"task_id"}:
            raise ValueError("运行 Task 只能包含 task_id")
        purpose = "debug" if action == "run_task_debug" else "evidence"
        result = execute_task(root, data["task_id"], purpose=purpose, backend="project")
        run = result.get("run") if isinstance(result, dict) else None
        run_id = run.get("id") if isinstance(run, dict) else None
        outcome_status = result.get("status")
        evidence_level = result.get("evidence_level")
        if outcome_status == "in_progress":
            next_action = action
            guidance = "实验仍在运行；稍后重复当前动作读取进度。"
        elif outcome_status == "artifact_seal_pending":
            next_action = "seal_run_outputs"
            guidance = "输出已完成但尚未封存；下一步封存这个 Run。"
        elif evidence_level == "single_run":
            next_action = "confirm_run_evidence"
            guidance = "Run 已达到 single_run；下一步由用户核对并明确确认。"
        elif action == "run_task_debug" and evidence_level == "debug":
            if _synthetic_run(result):
                next_action = "create_task"
                guidance = (
                    "合成 debug 已完成且不能用于论文；"
                    "请用真实数据配置创建正式 Task。"
                )
            else:
                next_action = "run_task_evidence"
                guidance = "debug 已通过；下一步运行正式 evidence Run。"
        else:
            next_action = "create_task"
            guidance = "本次 Run 未进入可确认状态；请先核对结果和缺项。"
        return _action_result(
            project,
            action,
            "completed",
            result,
            summary=(
                f"{run_id or 'Run'} 返回 "
                f"{outcome_status or evidence_level or 'completed'}；"
                f"{guidance}"
            ),
            next_action=next_action,
        )
    if action == "seal_run_outputs":
        if set(data) != {"run_id"}:
            raise ValueError("封存 Run 只能包含 run_id")
        run = seal_run_outputs(root, data["run_id"])
        seal = run["output_seal"]
        result = {
            "run_id": run["id"],
            "execution_stage": run["execution"]["stage"],
            "outcome": run["execution"]["outcome"],
            "total_files": seal["total_files"],
            "total_bytes": seal["total_bytes"],
            "seal_sha256": seal["seal_sha256"],
        }
        return _action_result(
            project,
            action,
            "sealed",
            result,
            summary=(
                f"已封存 {run['id']} 的 {seal['total_files']} 个输出文件；"
                "下一步再次运行同一 Task 的 evidence 动作，让它完成 single_run。"
            ),
            next_action="run_task_evidence",
        )
    if action == "confirm_run_evidence":
        required = {"run_id", "reason", "confirmed_by", "acknowledge"}
        if set(data) != required or data.get("acknowledge") is not True:
            raise ValueError("人工确认必须完整填写并把 acknowledge 精确设为 true")
        run_id = data["run_id"]
        if current_evidence_level(root, run_id) != "single_run":
            raise ValueError("Run 必须先达到 single_run，才能由用户确认")
        event = record_evidence_transition(
            root,
            run_id,
            "confirmed",
            evidence_refs=[run_id],
            reason=data["reason"],
            proposed_by=data["confirmed_by"],
            checked_by=data["confirmed_by"],
            applied_by=data["confirmed_by"],
        )
        return _action_result(
            project,
            action,
            "confirmed",
            event,
            summary=(
                f"已由 {event['checked_by']} 把 {run_id} 确认为 confirmed；"
                "下一步冻结并导出 Research Package。"
            ),
            next_action="freeze_research_package",
        )
    if set(data) != {"brief_id", "selection", "mode"}:
        raise ValueError("冻结交付包必须包含 brief_id/selection/mode")
    selection = data["selection"]
    mode = data["mode"]
    if (
        not isinstance(selection, dict)
        or mode not in {"hybrid", "full"}
        or selection.get("asset_mode") != mode
    ):
        raise ValueError("selection 必须是对象，且 asset_mode 与 mode 完全一致")
    with _temporary_json_manifest(selection) as path:
        sealed = seal_paper_package(root, data["brief_id"], path)
    output_root = _paperflow_delivery_root(root)
    output = output_root / sealed["package_id"]
    exported = export_paper_package(
        root,
        sealed["package_id"],
        mode,
        output,
    )
    verified = verify_paper_package(output)
    if (
        exported.get("package_id") != sealed["package_id"]
        or verified.get("package_id") != sealed["package_id"]
        or verified.get("status") != "pass"
    ):
        raise RuntimeError("交付包导出后的独立核验没有通过")
    result = {
        "package_id": sealed["package_id"],
        "readiness": sealed["readiness"],
        "asset_mode": sealed["asset_mode"],
        "content_sha256": sealed["content_sha256"],
        "handoff_path": output.relative_to(root).as_posix(),
        "verification": verified["status"],
        "included_assets": verified.get("included_assets", 0),
        "referenced_assets": verified.get("referenced_assets", 0),
        "omitted_assets": verified.get("omitted_assets", 0),
    }
    return {
        "schema": CONSOLE_WRITE_SCHEMA,
        "action": action,
        "status": "exported",
        "summary": (
            f"已冻结并导出 {sealed['package_id']}；"
            "PaperFlow 不会自动导入，请点击页面链接后手动选择这个包。"
        ),
        "next_action": "open_paperflow",
        "result": result,
        "paperflow": _paperflow_status(validated_paperflow_entrypoint),
        "state": console_state(project),
    }


def _action_result(
    project: Path,
    action: str,
    status: str,
    result: dict[str, Any],
    *,
    summary: str,
    next_action: str,
) -> dict[str, Any]:
    return {
        "schema": CONSOLE_WRITE_SCHEMA,
        "action": action,
        "status": status,
        "summary": summary,
        "next_action": next_action,
        "result": result,
        "paperflow": {"status": "unavailable", "entrypoint": None, "reason": "research_package_not_frozen"},
        "state": console_state(project),
    }


def _create_domain_repo(root: Path, data: dict[str, Any]) -> dict[str, Any]:
    if set(data) != {"direction", "name"}:
        raise ValueError("创建方向仓库只能包含 direction/name")
    direction = data["direction"]
    name = data["name"]
    if direction not in {"det", "cls", "seg", "instseg", "sr", "gzsl"}:
        raise ValueError("direction 必须是六方向之一")
    if direction_availability().get(direction) != "ready":
        raise ValueError(f"研究方向尚未开放：{direction}")
    if not isinstance(name, str) or _REPOSITORY_NAME.fullmatch(name) is None:
        raise ValueError("仓库名称只能使用 1 到 64 位字母、数字、-、_")
    repositories = root / "repositories"
    repositories.mkdir(exist_ok=True)
    if is_link_or_reparse(repositories) or not repositories.is_dir():
        raise ValueError("repositories 不能是链接、重解析点或普通文件")
    destination = (repositories / name).resolve(strict=False)
    if destination.parent != repositories.resolve(strict=True):
        raise ValueError("仓库输出必须固定在 project/repositories/<安全名称>")
    return create_domain_repository(root, resolve_domain_pack(direction), destination, name)


def _register_source(root: Path, data: dict[str, Any]) -> dict[str, Any]:
    kind = data.get("kind") if isinstance(data, dict) else None
    expected = (
        _SOURCE_BASE_FIELDS | {"files"}
        if kind == "code_snapshot"
        else _SOURCE_BASE_FIELDS
    )
    if set(data) != expected:
        raise ValueError("Source 必须使用固定结构化字段")
    source_path = data["source_path"]
    if not isinstance(source_path, str) or not source_path.strip():
        raise ValueError("source_path 必须是明确的本机路径")
    manifest = {
        "schema": SOURCE_SCHEMA,
        **{key: value for key, value in data.items() if key != "source_path"},
    }
    with _temporary_json_manifest(manifest) as path:
        return register_source(root, path, Path(source_path))


def _idea_manifest(data: dict[str, Any]) -> dict[str, Any]:
    if set(data) != _IDEA_FIELDS:
        raise ValueError("Idea 必须使用固定结构化字段")
    return {"schema": IDEA_SCHEMA, **data}


def _synthetic_run(result: dict[str, Any]) -> bool:
    run = result.get("run")
    if not isinstance(run, dict):
        return False
    frozen = run.get("frozen")
    if not isinstance(frozen, dict):
        return False
    data = frozen.get("data")
    return isinstance(data, dict) and (
        data.get("run_kind") == "synthetic_debug_only"
        or data.get("kind") == "synthetic_debug_only"
    )


def console_project_action(
    instances_root: Path,
    action: str,
    data: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """在固定实例目录内新建或打开一个 v2 项目。"""
    if action not in PROJECT_ACTIONS or not isinstance(data, dict):
        raise ValueError("无效的项目动作")
    parent = Path(instances_root).resolve(strict=True)
    if is_link_or_reparse(parent) or not parent.is_dir():
        raise ValueError("科研实例根必须是本机普通目录")
    if action == "create_project":
        if set(data) != {"directory_name", "display_name"}:
            raise ValueError("新建项目必须包含 directory_name/display_name")
        directory_name = _safe_project_directory_name(data["directory_name"])
        display_name = data["display_name"]
        if (
            not isinstance(display_name, str)
            or display_name != display_name.strip()
            or not 1 <= len(display_name) <= 128
            or any(
                ord(character) < 32
                for character in display_name
            )
        ):
            raise ValueError("display_name 必须是 1 到 128 个规范字符")
        destination = parent / directory_name
        if os.path.lexists(destination):
            raise FileExistsError("项目目录已存在，拒绝覆盖")
        init_project(destination, display_name, layout="v2")
        status = "created"
        summary = f"已新建并打开科研项目 {display_name}。"
    else:
        if set(data) != {"directory_name"}:
            raise ValueError("打开项目只能包含 directory_name")
        directory_name = _safe_project_directory_name(data["directory_name"])
        destination = parent / directory_name
        status = "opened"
        summary = f"已打开科研项目 {directory_name}。"
    target = destination.resolve(strict=True)
    if target.parent != parent or is_link_or_reparse(target) or not target.is_dir():
        raise ValueError("项目必须是实例根下的直接普通子目录")
    snapshot = read_console_snapshot(target)
    state = console_state(target)
    return target, {
        "schema": CONSOLE_WRITE_SCHEMA,
        "action": action,
        "status": status,
        "summary": summary,
        "next_action": "save_research_brief",
        "result": {
            "directory_name": directory_name,
            "project_id": snapshot["project"]["project_id"],
            "name": snapshot["project"]["name"],
        },
        "paperflow": {
            "status": "unavailable",
            "entrypoint": None,
            "reason": "research_package_not_frozen",
        },
        "state": state,
    }


def _safe_project_directory_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 64
        or value in {".", ".."}
        or value.endswith((".", " "))
        or unicodedata.normalize("NFC", value) != value
        or any(
            character in '<>:"/\\|?*'
            or unicodedata.category(character).startswith("C")
            for character in value
        )
        or value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError(
            "directory_name 必须是 1 到 64 个安全文件名字符，"
            "不能含路径分隔符、控制字符或 Windows 保留名"
        )
    return value


class _temporary_json_manifest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.directory: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> Path:
        self.directory = tempfile.TemporaryDirectory(prefix="cv-console-manifest-")
        path = Path(self.directory.name) / "manifest.json"
        atomic_write_json(path, self.payload)
        return path

    def __exit__(self, *unused: object) -> None:
        if self.directory is not None:
            self.directory.cleanup()


def _paperflow_delivery_root(root: Path) -> Path:
    deliverables = _require_local_directory(root, "deliverables")
    return _require_local_directory(deliverables, "paperflow")


def _require_local_directory(parent: Path, name: str) -> Path:
    target = parent / name
    if os.path.lexists(target):
        if is_link_or_reparse(target) or not target.is_dir():
            raise ValueError(f"{name} 必须是项目内普通目录")
    else:
        target.mkdir()
    if is_link_or_reparse(target) or not target.is_dir():
        raise ValueError(f"{name} 必须是项目内普通目录")
    resolved_parent = parent.resolve(strict=True)
    resolved = target.resolve(strict=True)
    if resolved.parent != resolved_parent:
        raise ValueError(f"{name} 必须直接位于受控项目目录内")
    return resolved


def _paperflow_status(value: str | None) -> dict[str, str | None]:
    if value is None:
        return {
            "status": "unavailable",
            "entrypoint": None,
            "reason": "paperflow_not_started",
        }
    entrypoint = _validated_paperflow_entrypoint(value)
    return {"status": "ready", "entrypoint": entrypoint, "reason": None}


def _validated_paperflow_entrypoint(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("PaperFlow 入口必须是本机 URL")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("PaperFlow 入口端口无效") from error
    prefix = "token="
    token = (
        parsed.fragment[len(prefix):]
        if parsed.fragment.startswith(prefix)
        else ""
    )
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65535
        or parsed.path != "/"
        or parsed.query
        or not 16 <= len(token) <= 256
        or not all(
            character.isascii()
            and (character.isalnum() or character in "_-")
            for character in token
        )
    ):
        raise ValueError("PaperFlow 入口必须是带会话令牌的本机入口")
    return f"http://127.0.0.1:{port}/#token={token}"


def _invalid_state(intent: str) -> dict[str, Any]:
    conditions = [
        _condition(
            name,
            (
                "block"
                if name in {"project_snapshot", "controlled_actions"}
                else "not_checked"
            ),
            (
                "invalid_project_snapshot"
                if name == "project_snapshot"
                else (
                    "project_snapshot_unavailable"
                    if name == "controlled_actions"
                    else "project_snapshot_unavailable"
                )
            ),
        )
        for name in _CONDITION_ORDER
    ]
    return {
        "schema": CONSOLE_STATE_SCHEMA,
        "mode": "controlled",
        "read_only": False,
        "execution_enabled": False,
        "snapshot": {
            "status": "invalid",
            "layout": None,
            "counts": {
                "sources": 0,
                "ideas": 0,
                "templates": 0,
                "modules": 0,
                "tasks": 0,
                "runs": 0,
                "evidence": 0,
                "nodes": 0,
                "edges": 0,
            },
        },
        "start": {
            "project": None,
            "workflow": None,
            "adapter": None,
            "intake": None,
        },
        "relationships": {
            "nodes": [],
            "edges": [],
            "omissions": [],
        },
        "execution": {
            "phase": "controlled_local_actions",
            "intent": intent,
            "preview": None,
            "conditions": conditions,
            "blocked_by": ["project_snapshot", "controlled_actions"],
            "next_step": "repair_project_snapshot",
        },
    }


def _project_start(snapshot: dict[str, Any]) -> dict[str, Any]:
    project = snapshot["project"]
    project_skill = project["project_skill"]
    return {
        "project_id": project["project_id"],
        "name": project["name"],
        "skill_id": project_skill["skill_id"],
        "entrypoint": project_skill["entrypoint"],
    }


def _workflow_start(snapshot: dict[str, Any]) -> dict[str, Any]:
    lock = snapshot["workflow_lock"]
    return {
        "skill_id": lock["skill_id"],
        "release_version": lock.get("release_version"),
        "system_version": lock.get("system_version"),
        "trust": snapshot["workflow_lock_trust"],
    }


def _adapter_start(snapshot: dict[str, Any]) -> dict[str, Any]:
    adapter = snapshot["adapter"]
    return {
        "status": adapter["status"],
        "capabilities": deepcopy(adapter["capabilities"]),
        "code_sources": deepcopy(adapter["code_sources"]),
        "runtime": _effective_adapter_runtime(snapshot),
    }


def _effective_adapter_runtime(
    snapshot: dict[str, Any],
) -> dict[str, str | None]:
    runtime = deepcopy(snapshot["adapter_runtime"])
    codebases = snapshot.get("codebases", {})
    if (
        runtime.get("status") == "block"
        and runtime.get("reason") == "adapter_unbound"
        and isinstance(codebases, dict)
        and codebases
    ):
        return {
            "status": "pass",
            "reason": "registered_codebase_available",
        }
    return runtime


def _valid_conditions(
    snapshot: dict[str, Any],
    intake: dict[str, Any],
) -> list[dict[str, str]]:
    runtime = _effective_adapter_runtime(snapshot)
    return [
        _condition("project_snapshot", "pass", "validated_v2_snapshot"),
        _condition(
            "workflow_lock",
            "pass",
            snapshot["workflow_lock_trust"],
        ),
        _condition(
            "intake_complete",
            "pass" if intake["can_continue"] else "block",
            (
                "complete"
                if intake["can_continue"]
                else "missing_required_inputs"
            ),
        ),
        _condition("adapter_config", "pass", "validated_adapter_json"),
        _condition(
            "adapter_runtime",
            runtime["status"],
            runtime["reason"] or "verified_runtime_binding",
        ),
        _condition("controlled_actions", "pass", "controlled_actions_available"),
    ]


def _condition(identifier: str, status: str, reason: str) -> dict[str, str]:
    return {"id": identifier, "status": status, "reason": reason}


def _next_step(
    conditions: list[dict[str, str]],
    intake: dict[str, Any],
) -> str:
    blocked = {item["id"] for item in conditions if item["status"] == "block"}
    if "intake_complete" in blocked:
        return intake["next_step"]
    if "adapter_runtime" in blocked:
        return "repair_adapter_runtime"
    return "use_controlled_actions"


def _relationships(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: set[tuple[str, str, str]] = set()
    omissions: list[dict[str, str]] = []

    project = snapshot["project"]
    project_skill_id = f"project-skill:{project['project_skill']['skill_id']}"
    workflow_id = f"workflow:{snapshot['workflow_lock']['skill_id']}"
    ledger_id = f"ledger:{project['project_id']}"
    _add_node(
        nodes,
        project_skill_id,
        "project_skill",
        project["project_skill"]["skill_id"],
        "bound",
    )
    _add_node(
        nodes,
        workflow_id,
        "workflow",
        snapshot["workflow_lock"]["skill_id"],
        snapshot["workflow_lock_trust"],
    )
    _add_node(nodes, ledger_id, "ledger", project["name"], "validated")
    edges.add((project_skill_id, workflow_id, "project_uses_workflow"))
    edges.add((workflow_id, ledger_id, "workflow_manages_ledger"))

    code_sources = sorted(
        snapshot["adapter"]["code_sources"],
        key=lambda item: (
            item["repo_url"],
            item["commit"],
            item["relative_path"],
        ),
    )
    for index, source in enumerate(code_sources, start=1):
        repo_id = f"adapter-repo:{index:04d}"
        _add_node(
            nodes,
            repo_id,
            "adapter_repo",
            source["repo_url"],
            snapshot["adapter_runtime"]["status"],
            {
                "commit": source["commit"],
                "relative_path": source["relative_path"],
            },
        )
        edges.add((repo_id, project_skill_id, "adapter_repo_supplies_project"))
    if not code_sources:
        omissions.append(
            {
                "kind": "adapter_repo",
                "ref": "adapter",
                "reason": "no_code_source",
            }
        )

    for codebase_id, codebase in sorted(
        snapshot.get("codebases", {}).items()
    ):
        node_id = f"codebase:{codebase_id}"
        _add_node(
            nodes,
            node_id,
            "adapter_repo",
            str(codebase["name"]),
            "registered",
            {
                "codebase_id": codebase_id,
                "primary_direction": codebase["primary_direction"],
                "template_id": codebase.get("template_id"),
                "template_version": codebase.get("template_version"),
                "commit": codebase["initial_commit"],
            },
        )
        edges.add(
            (node_id, project_skill_id, "adapter_repo_supplies_project")
        )

    catalog = snapshot["catalog"]
    for source_id, source in sorted(catalog["sources"].items()):
        _add_node(
            nodes,
            source_id,
            "source",
            str(source.get("identity", source_id)),
            str(source.get("kind", "registered")),
        )
    for idea_id, idea in sorted(catalog["ideas"].items()):
        _add_node(
            nodes,
            idea_id,
            "idea",
            idea_id,
            str(idea.get("status", "registered")),
        )
        for source_id in idea["source_refs"]:
            edges.add((source_id, idea_id, "source_supports_idea"))
    for template_id, template in sorted(catalog["templates"].items()):
        _add_node(
            nodes,
            template_id,
            "template",
            str(template.get("name", template_id)),
            str(template.get("status", "registered")),
        )
        for source_id in template["source_refs"]:
            edges.add((source_id, template_id, "source_supports_template"))
    for module_id, module in sorted(catalog["modules"].items()):
        _add_node(
            nodes,
            module_id,
            "module",
            module_id,
            str(module.get("status", "registered")),
        )
        for source_id in module["source_refs"]:
            edges.add((source_id, module_id, "source_supports_module"))
        for idea_id in module["idea_refs"]:
            edges.add((idea_id, module_id, "idea_defines_module"))
        template_ref = module["attachment"]["template_ref"]
        edges.add((template_ref, module_id, "template_hosts_module"))

    for task_id, task in sorted(snapshot["tasks"].items()):
        _add_node(
            nodes,
            task_id,
            "task",
            task_id,
            task["stage"],
            {"route": task["route"]},
        )
    for run_id, run in sorted(snapshot["runs"].items()):
        _add_node(
            nodes,
            run_id,
            "run",
            run_id,
            run["execution"]["stage"],
            {"purpose": run["purpose"]},
        )

    known_catalog = {
        *catalog["sources"],
        *catalog["ideas"],
        *catalog["templates"],
        *catalog["modules"],
    }
    for task_id, task in sorted(snapshot["tasks"].items()):
        inputs = task["route_inputs"]
        references = set(task["target_refs"])
        for name in ("baseline_run_ref", "source_run_ref"):
            value = inputs.get(name)
            if isinstance(value, str):
                references.add(value)
        for reference in sorted(references):
            if reference in known_catalog:
                edges.add((reference, task_id, "catalog_ref_feeds_task"))
            elif reference in snapshot["tasks"]:
                edges.add((reference, task_id, "task_prerequisite"))
            elif reference in snapshot["runs"]:
                edges.add((reference, task_id, "prior_run_feeds_task"))
        for run_id in task["run_refs"]:
            edges.add((task_id, run_id, "task_produces_run"))

    evidence_runs: set[str] = set()
    for event in snapshot["events"]:
        run_id = event["subject_id"]
        evidence_id = event["event_id"]
        evidence_runs.add(run_id)
        _add_node(
            nodes,
            evidence_id,
            "evidence",
            evidence_id,
            event["to"],
            {
                "from": event["from"],
                "to": event["to"],
                "reason": event.get("reason"),
                "time": event.get("time"),
            },
        )
        edges.add((run_id, evidence_id, "run_has_evidence"))
        for reference in event["evidence_refs"]:
            if reference != run_id:
                edges.add(
                    (reference, evidence_id, "run_supports_evidence")
                )
    for run_id in sorted(set(snapshot["runs"]) - evidence_runs):
        omissions.append(
            {
                "kind": "evidence",
                "ref": run_id,
                "reason": "no_evidence_event",
            }
        )

    return {
        "nodes": [nodes[node_id] for node_id in sorted(nodes)],
        "edges": [
            {"from": source, "to": target, "kind": kind}
            for source, target, kind in sorted(edges)
        ],
        "omissions": sorted(
            omissions,
            key=lambda item: (item["kind"], item["ref"], item["reason"]),
        ),
    }


def _add_node(
    nodes: dict[str, dict[str, Any]],
    identifier: str,
    kind: str,
    label: str,
    status: str,
    details: dict[str, Any] | None = None,
) -> None:
    nodes[identifier] = {
        "id": identifier,
        "kind": kind,
        "label": label,
        "status": status,
        "details": {} if details is None else deepcopy(details),
    }
