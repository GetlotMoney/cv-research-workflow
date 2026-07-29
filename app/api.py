from __future__ import annotations

from typing import Any

from .service import CoreService


class CoreApi:
    def __init__(self, service: CoreService) -> None:
        self.service = service

    def dispatch(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | list[dict[str, Any]]:
        if method == "GET" and path == "/api/status":
            return self.service.status()
        if method != "POST":
            raise KeyError(f"接口不存在：{method} {path}")

        if path == "/api/repositories/create":
            return self.service.create_repository(
                payload["name"],
                payload["direction"],
            )
        if path == "/api/frameworks/scan":
            return self.service.scan_framework(
                payload["source_repository"],
            )
        if path == "/api/frameworks/prepare":
            return self.service.prepare_framework_draft(
                source_repository=payload["source_repository"],
                slug=payload["slug"],
                component_map=payload.get("component_map"),
            )
        if path == "/api/repositories/details":
            return self.service.repository_details(payload["repository"])
        if path == "/api/repositories/validate":
            return self.service.validate_repository(payload["repository"])
        if path == "/api/frameworks/import":
            return self.service.import_framework(
                payload["repository"],
                payload,
            )
        if path == "/api/ideas/create":
            return self.service.create_idea(
                payload["repository"],
                payload,
            )
        if path == "/api/experiments/create":
            return self.service.create_experiment(
                payload["repository"],
                payload,
            )
        if path == "/api/worktrees/prepare":
            return self.service.prepare_worktree(
                payload["repository"],
                payload["experiment_id"],
            )
        if path == "/api/datasets/register":
            return self.service.register_dataset(
                payload["repository"],
                payload,
            )
        if path == "/api/runs/execute":
            return self.service.run_experiment(
                payload["repository"],
                payload,
            )
        if path == "/api/runs/confirm":
            return self.service.confirm_run(
                payload["repository"],
                payload,
            )
        if path == "/api/frameworks/promote":
            return self.service.promote_framework(
                payload["repository"],
                payload,
            )
        if path == "/api/packages/export":
            return self.service.export_research_package(
                payload["repository"],
                payload,
            )
        raise KeyError(f"接口不存在：{method} {path}")
