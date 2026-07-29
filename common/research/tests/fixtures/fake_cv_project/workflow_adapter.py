from __future__ import annotations

from pathlib import Path as _Path
from typing import Any as _Any


def inspect(project: _Path) -> dict[str, _Any]:
    return {
        "project": project.name,
        "standard": {"debug_required": (project / ".fake-debug-required").is_file()},
        "baseline": {"score": 0.70},
    }


def validate(project: _Path) -> list[str]:
    return [] if project.is_dir() else ["项目目录不存在"]


def prepare_runs(
    project: _Path, task: dict[str, _Any]
) -> list[dict[str, _Any]]:
    inputs = task["route_inputs"]
    return [
        {
            "code": {"revision": "fixture-v1"},
            "config": dict(inputs.get("config", {})),
            "seed": inputs.get("seed", 7),
            "data": {"split": "validation"},
            "environment": {"backend": "project", "fixture": True},
        }
    ]


def execute(
    project: _Path, action: str, run: dict[str, _Any]
) -> dict[str, _Any]:
    if action == "start":
        return {"status": "finished", "process_id": 4242}
    if action == "status":
        return {"status": "finished"}
    if action == "stop":
        return {"status": "stopped"}
    raise ValueError(f"不支持的 action：{action}")


def parse_result(project: _Path, run: dict[str, _Any]) -> dict[str, _Any]:
    config = run["frozen"]["config"]
    outcome = config.get("fixture_outcome", "succeeded")
    issue_kind = config.get("fixture_issue_kind")
    if outcome == "failed" and issue_kind is None:
        issue_kind = "implementation"
    if outcome == "stopped" and issue_kind is None:
        issue_kind = "user_stop"
    implementation = (
        "invalid" if issue_kind == "implementation" else "valid"
    )
    interface = config.get("fixture_interface", "valid")
    quality_valid = implementation == "valid" and interface == "valid"
    return {
        "execution": {
            "outcome": outcome,
            "exit_code": 0 if outcome == "succeeded" else 1,
            "issue_kind": issue_kind,
        },
        "result": {"metrics": {"score": 0.70}, "raw_log": None},
        "quality": {
            "implementation": implementation,
            "interface": interface,
            "data": "valid",
            "metrics": "valid",
        },
        "analysis": {
            "hypothesis": (
                "inconclusive"
                if outcome == "succeeded" and quality_valid
                else "not_evaluated"
            ),
            "limitations": ["假项目只验证流程，不证明指标提升"],
            "suggestions": [],
        },
        "artifacts": [],
    }
