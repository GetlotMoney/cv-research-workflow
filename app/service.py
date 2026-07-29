from __future__ import annotations

import importlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


_REPOSITORY_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_CONFIG_FIELDS = {
    "schema",
    "research_root",
    "environment_file",
    "directions_file",
    "users_root",
    "active_workspace_file",
}
_DIRECTION_IDENTITIES = {
    "image_classification": "cls",
    "object_detection": "det",
    "instance_segmentation": "instseg",
    "semantic_segmentation": "seg",
    "super_resolution": "sr",
    "gzsl": "gzsl",
}


class CoreService:
    """通用科研服务。

    服务可以在没有个人实例时启动。只有用户以后主动创建并激活个人实例，
    仓库、实验和 Run 写操作才会开放。
    """

    def __init__(
        self,
        config: dict[str, Any],
        *,
        system_root: Path | None = None,
    ) -> None:
        if (
            not isinstance(config, dict)
            or set(config) != _CONFIG_FIELDS
            or config.get("schema") != "cvwf.research-system-config.v2"
        ):
            raise ValueError("通用科研配置字段或 schema 无效")
        self.system_root = (
            Path(system_root).expanduser().resolve(strict=True)
            if system_root is not None
            else Path.cwd().resolve(strict=True)
        )
        self.research_root = _directory(
            self._config_path(config["research_root"]),
            "科研源码",
        )
        self.environment_file = _file(
            self._config_path(config["environment_file"]),
            "GPU 环境配置",
        )
        self.directions_file = _file(
            self._config_path(config["directions_file"]),
            "研究方向清单",
        )
        self.users_root = _managed_directory(
            self._config_path(config["users_root"]),
            "个人实例根目录",
        )
        if self.users_root.parent != self.system_root:
            raise ValueError("个人实例根目录必须直接位于系统总目录内")
        self.active_workspace_file = self._config_path(
            config["active_workspace_file"]
        )
        if self.active_workspace_file.parent != self.system_root / "config":
            raise ValueError("当前个人实例指针必须位于 config 目录")

        self.environment = _read_json(self.environment_file)
        if (
            self.environment.get("schema") != "cvwf.research-environment.v2"
            or self.environment.get("platform") != "windows_only"
            or self.environment.get("execution_policy") != "gpu_only"
            or self.environment.get("cpu_fallback") is not False
            or self.environment.get("status") != "verified"
            or not isinstance(self.environment.get("python_executable"), str)
        ):
            raise ValueError("科研 GPU 环境尚未通过固定配置验证")
        self.direction_catalog = _direction_catalog(
            _read_json(self.directions_file)
        )
        self.direction_by_id = {
            item["id"]: item for item in self.direction_catalog["directions"]
        }

        self.workspace: dict[str, Any] | None = None
        self.workspace_root: Path | None = None
        self.repositories_root: Path | None = None
        self.deliveries_root: Path | None = None
        self.inbox_root: Path | None = None
        if self.active_workspace_file.is_file():
            self._load_active_workspace()
        elif os.path.lexists(self.active_workspace_file):
            raise ValueError("当前个人实例指针不是普通文件")

        research_scripts = (
            self.research_root
            / "skills"
            / "cv-experiment-workflow"
            / "scripts"
        ).resolve(strict=True)
        text = str(research_scripts)
        if text not in sys.path:
            sys.path.insert(0, text)

    @classmethod
    def from_file(cls, path: Path) -> "CoreService":
        config_path = Path(path).expanduser().resolve(strict=True)
        return cls(
            _read_json(config_path),
            system_root=config_path.parent.parent,
        )

    def status(self) -> dict[str, Any]:
        return {
            "schema": "cvwf.research-status.v3",
            "system_version": "SYS-RESEARCH-V2.0.2",
            "ui_version": "UI-RESEARCH-V2.0.2",
            "mode": "workspace_active" if self.workspace else "template_only",
            "workspace": dict(self.workspace) if self.workspace else None,
            "environment": dict(self.environment),
            "direction_catalog": self.direction_catalog,
            "repositories": self.list_repositories(),
        }

    def scan_framework(self, source_repository: Path) -> dict[str, Any]:
        module = importlib.import_module("app.framework_intake")
        return module.scan_gzsl_repository(source_repository)

    def prepare_framework_draft(
        self,
        *,
        source_repository: Path,
        slug: str,
        component_map: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_workspace()
        assert self.inbox_root is not None
        draft_root = _managed_directory(
            self.inbox_root / "frameworks",
            "外来框架收件目录",
        )
        module = importlib.import_module("app.framework_intake")
        return module.prepare_standardization_draft(
            source_repository,
            draft_root,
            slug,
            component_map=component_map,
        )

    def create_repository(self, name: str, direction: str) -> dict[str, Any]:
        self._require_workspace()
        selected = self.direction_by_id.get(direction)
        if selected is None:
            raise ValueError(f"未知研究方向：{direction}")
        if selected["status"] != "ready":
            raise ValueError(f"研究方向尚未开放：{selected['label_zh']}")
        if (
            selected["slug"] != "gzsl"
            or selected["repository_factory"] != "create_gzsl_repository"
        ):
            raise ValueError("当前版本只实现 GZSL 仓库创建")
        safe_name = _repository_name(name)
        assert self.repositories_root is not None
        module = self._framework_module()
        created = module.create_gzsl_repository(
            self.repositories_root / safe_name,
            name=safe_name,
            environment_name=str(self.environment["environment_name"]),
        )
        repository_record = _read_json(
            self.repositories_root
            / safe_name
            / ".experiment-workflow"
            / "repository.json"
        )
        self._record_repository(repository_record)
        return {**created, "repository": repository_record}

    def list_repositories(self) -> list[dict[str, Any]]:
        if self.repositories_root is None:
            return []
        records: list[dict[str, Any]] = []
        for entry in sorted(self.repositories_root.iterdir()):
            if entry.name.startswith("."):
                continue
            record_path = entry / ".experiment-workflow" / "repository.json"
            if not record_path.is_file():
                continue
            record = _read_json(record_path)
            records.append(
                {
                    "name": record["name"],
                    "direction": record["direction"],
                    "active_framework": record["active_framework"],
                    "execution_policy": record["execution_policy"],
                }
            )
        return records

    def repository_details(self, name: str) -> dict[str, Any]:
        root = self._repository(name)
        control = root / ".experiment-workflow"
        repository = _read_json(control / "repository.json")
        frameworks: list[dict[str, Any]] = []
        experiments: list[dict[str, Any]] = []
        framework_root = control / "frameworks"
        if framework_root.is_dir():
            for framework_file in sorted(
                framework_root.glob("*/framework.json")
            ):
                framework = _read_json(framework_file)
                frameworks.append(framework)
                experiment_root = framework_file.parent / "experiments"
                for experiment_file in sorted(
                    experiment_root.glob("*/*/experiment.json")
                ):
                    experiment = _read_json(experiment_file)
                    run_root = experiment_file.parent / "runs"
                    runs = (
                        [
                            _read_json(path)
                            for path in sorted(run_root.glob("*/run.json"))
                        ]
                        if run_root.is_dir()
                        else []
                    )
                    experiments.append({**experiment, "runs": runs})
        idea_root = control / "idea-tree"
        ideas = (
            [
                _read_json(path)
                for path in sorted(idea_root.glob("*/idea.json"))
            ]
            if idea_root.is_dir()
            else []
        )
        return {
            "repository": repository,
            "frameworks": frameworks,
            "experiments": experiments,
            "ideas": ideas,
        }

    def validate_repository(self, name: str) -> dict[str, Any]:
        return self._framework_module().validate_framework_workspace(
            self._repository(name)
        )

    def import_framework(
        self,
        name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_workspace()
        assert self.inbox_root is not None
        draft_root = _managed_directory(
            self.inbox_root / "frameworks",
            "外来框架收件目录",
        ).resolve(strict=True)
        source = Path(payload["source_repository"]).expanduser().resolve(
            strict=True
        )
        if draft_root not in source.parents:
            raise ValueError(
                "只能导入先经过页面扫描并复制到 inbox 的标准化草稿"
            )
        if payload.get("trusted_execution_acknowledged") is not True:
            raise ValueError(
                "请先人工审读草稿代码，并明确确认它可以在本机 GPU 环境运行"
            )
        return self._framework_module().import_standardized_gzsl_framework(
            self._repository(name),
            source_repository=source,
            framework_slug=payload["framework_slug"],
            title=payload["title"],
            version=payload["version"],
            component_map=payload["component_map"],
            equivalence=payload["equivalence"],
            trusted_execution_acknowledged=True,
        )

    def create_idea(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._framework_module().create_framework_idea(
            self._repository(name),
            title=payload["title"],
            problem=payload["problem"],
            mechanism=payload["mechanism"],
            falsifiable_hypothesis=payload["falsifiable_hypothesis"],
            source_notes=payload["source_notes"],
            parent_idea_ref=payload.get("parent_idea_ref"),
        )

    def create_experiment(
        self,
        name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._framework_module().create_framework_experiment(
            self._repository(name),
            framework_slug=payload["framework_slug"],
            route=payload["route"],
            slug=payload["slug"],
            title=payload["title"],
            summary=payload["summary"],
            idea_ref=payload.get("idea_ref"),
            route_contract=payload.get("route_contract"),
        )

    def prepare_worktree(self, name: str, experiment_id: str) -> dict[str, str]:
        worktree = self._framework_module().prepare_experiment_worktree(
            self._repository(name),
            experiment_id=experiment_id,
        )
        return {"worktree": str(worktree)}

    def register_dataset(
        self,
        name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._framework_module().register_gzsl_dataset(
            self._repository(name),
            experiment_id=payload["experiment_id"],
            worktree=Path(payload["worktree"]),
            source_path=Path(payload["source_path"]),
            dataset_slug=payload["dataset_slug"],
            dataset_id=payload["dataset_id"],
            version=payload["version"],
            source_uri=payload["source_uri"],
            license_name=payload["license"],
        )

    def run_experiment(
        self,
        name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._framework_module().run_framework_experiment(
            self._repository(name),
            experiment_id=payload["experiment_id"],
            worktree=Path(payload["worktree"]),
            config=payload["config"],
            seed=payload["seed"],
            purpose=payload["purpose"],
        )

    def confirm_run(
        self,
        name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._framework_module().confirm_framework_run(
            self._repository(name),
            experiment_id=payload["experiment_id"],
            local_run_id=payload["local_run_id"],
            reason=payload["reason"],
            proposed_by=payload["proposed_by"],
            checked_by=payload["checked_by"],
            innovation_accepted=payload.get("innovation_accepted"),
            innovation_conclusion=payload.get("innovation_conclusion"),
        )

    def export_research_package(
        self,
        name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_workspace()
        assert self.deliveries_root is not None
        return self._framework_module().export_framework_paper_package(
            self._repository(name),
            experiment_id=payload["experiment_id"],
            local_run_id=payload["local_run_id"],
            research_brief=payload["research_brief"],
            claim_statement_zh=payload["claim_statement_zh"],
            confirmed_by=payload["confirmed_by"],
            destination_root=self.deliveries_root / "research-packages",
        )

    def promote_framework(
        self,
        name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._framework_module().promote_innovation_framework(
            self._repository(name),
            experiment_id=payload["experiment_id"],
            worktree=Path(payload["worktree"]),
            child_slug=payload["child_slug"],
            title=payload["title"],
            version=payload["version"],
            framework_view=payload["framework_view"],
        )

    def _config_path(self, value: object) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("配置路径必须是非空字符串")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.system_root / candidate
        return candidate.absolute()

    def _load_active_workspace(self) -> None:
        activation = _read_json(self.active_workspace_file)
        if set(activation) != {"schema", "workspace_file"} or activation.get(
            "schema"
        ) != "cvwf.active-workspace.v1":
            raise ValueError("当前个人实例指针无效")
        workspace_file = self._config_path(activation["workspace_file"])
        workspace_root = workspace_file.parent.resolve(strict=True)
        if workspace_root.parent != self.users_root:
            raise ValueError("个人实例必须是 users 下的直接子目录")
        workspace = _read_json(workspace_file)
        if (
            workspace.get("schema") != "cvwf.personal-workspace.v2"
            or workspace.get("status") != "active"
            or workspace.get("direction_catalog_version")
            != self.direction_catalog["version"]
            or workspace.get("slug") != workspace_root.name
            or not isinstance(workspace.get("workspace_id"), str)
            or not isinstance(workspace.get("display_name"), str)
            or not isinstance(workspace.get("skill_id"), str)
        ):
            raise ValueError("个人实例配置无效或方向清单版本不一致")
        self.workspace = workspace
        self.workspace_root = workspace_root
        self.repositories_root = _directory(
            workspace_root / "repositories",
            "科研仓库根目录",
        )
        self.deliveries_root = _directory(
            workspace_root / "deliveries",
            "科研交付目录",
        )
        self.inbox_root = _directory(
            workspace_root / "inbox",
            "外来材料收件目录",
        )

    def _require_workspace(self) -> None:
        if self.workspace is None:
            raise ValueError("尚未创建并激活个人实例；通用模板不会替你个性化")

    def _repository(self, name: str) -> Path:
        self._require_workspace()
        safe_name = _repository_name(name)
        assert self.repositories_root is not None
        candidate = self.repositories_root / safe_name
        if not candidate.is_dir():
            raise ValueError(f"科研仓库不存在：{safe_name}")
        if candidate.resolve(strict=True).parent != self.repositories_root:
            raise ValueError("科研仓库逃逸管理根目录")
        return candidate

    def _record_repository(self, repository: dict[str, Any]) -> None:
        assert self.workspace_root is not None
        index_path = self.workspace_root / "REPOSITORY_INDEX.json"
        index = _read_json(index_path)
        if (
            index.get("schema") != "cvwf.repository-index.v2"
            or index.get("workspace_id") != self.workspace["workspace_id"]
            or not isinstance(index.get("repositories"), list)
        ):
            raise ValueError("个人仓库索引无效")
        if any(
            item.get("name") == repository["name"]
            for item in index["repositories"]
            if isinstance(item, dict)
        ):
            raise ValueError("个人仓库索引已经存在同名记录")
        index["repositories"].append(
            {
                "name": repository["name"],
                "direction": repository["direction"],
                "path": f"repositories/{repository['name']}",
                "status": "active",
            }
        )
        temporary = index_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(index, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, index_path)

    @staticmethod
    def _framework_module():
        return importlib.import_module("workflow_core.framework_workspace")


def _direction_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    if (
        set(payload) != {"schema", "version", "directions"}
        or payload.get("schema") != "cvwf.direction-catalog.v1"
        or not isinstance(payload.get("version"), str)
        or not isinstance(payload.get("directions"), list)
    ):
        raise ValueError("研究方向清单格式无效")
    directions = payload["directions"]
    if len(directions) != 6:
        raise ValueError("研究方向清单必须恰好包含六个方向")
    normalized: list[dict[str, Any]] = []
    for item in directions:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "slug",
            "label_zh",
            "status",
            "repository_factory",
        }:
            raise ValueError("研究方向条目字段无效")
        direction_id = item["id"]
        if _DIRECTION_IDENTITIES.get(direction_id) != item["slug"]:
            raise ValueError("研究方向 id 与 slug 对应关系无效")
        if item["status"] not in {"ready", "pending"}:
            raise ValueError("研究方向状态只允许 ready 或 pending")
        normalized.append(dict(item))
    if {item["id"] for item in normalized} != set(_DIRECTION_IDENTITIES):
        raise ValueError("研究方向清单缺失或重复")
    ready = [item for item in normalized if item["status"] == "ready"]
    if len(ready) != 1 or ready[0]["id"] != "gzsl":
        raise ValueError("当前版本必须且只能开放 GZSL")
    if ready[0]["repository_factory"] != "create_gzsl_repository":
        raise ValueError("GZSL 仓库创建器无效")
    if any(
        item["repository_factory"] is not None
        for item in normalized
        if item["status"] == "pending"
    ):
        raise ValueError("待做方向不能绑定仓库创建器")
    return {
        "schema": payload["schema"],
        "version": payload["version"],
        "directions": normalized,
    }


def _repository_name(value: object) -> str:
    if not isinstance(value, str) or _REPOSITORY_NAME.fullmatch(value) is None:
        raise ValueError("仓库名只允许小写字母、数字和短横线")
    return value


def _directory(value: object, label: str) -> Path:
    path = Path(str(value)).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"{label}不是目录：{path}")
    return path


def _file(value: object, label: str) -> Path:
    path = Path(str(value)).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"{label}不是文件：{path}")
    return path


def _managed_directory(value: object, label: str) -> Path:
    path = Path(str(value)).expanduser().absolute()
    if os.path.lexists(path) and not path.is_dir():
        raise ValueError(f"{label}不是目录：{path}")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve(strict=True)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path}")
    return payload
