from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "cv-experiment-workflow" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from workflow_core.domain_packs import (  # noqa: E402
    discover_domain_packs,
    resolve_domain_pack,
    validate_domain_pack,
)
from workflow_core.engine import execute_task  # noqa: E402
from workflow_core.runs import load_run  # noqa: E402
from workflow_core.tasking import create_task  # noqa: E402
from workflow_core.v2_catalog import (  # noqa: E402
    IDEA_SCHEMA,
    SOURCE_SCHEMA,
    register_source,
    revise_catalog_idea,
    save_idea,
)


REPORT_SCHEMA = "cv-experiment-workflow.six-domain-pack-smoke.v1"
LINEAGE_SCHEMA = "cv-experiment-workflow.branch-run-lineage-smoke.v1"
DIRECTIONS = ("cls", "det", "gzsl", "instseg", "seg", "sr")
COMMIT = re.compile(r"[0-9a-f]{40}")
CODEBASE_ID = re.compile(r"CB-[0-9]{4}")
IDEA_ID = re.compile(r"IDEA-[0-9]{4}")
TASK_ID = re.compile(r"TASK-[0-9]{4}")
RUN_ID = re.compile(r"RUN-[0-9]{4}")


def run_six_domain_packs(
    *,
    artifacts_root: Path | None = None,
    device: str | None = None,
) -> dict[str, Any]:
    """静态核验六方向目录和 manifest，不启动训练或 CUDA。"""
    if device is not None and device.lower() != "cuda":
        raise ValueError("GZSL 固定使用 CUDA，明确拒绝 CPU")
    root: Path | None = None
    if artifacts_root is not None:
        root = Path(artifacts_root).resolve()
        root.mkdir(parents=True, exist_ok=False)
    return _run(root, persistent_artifacts=root is not None)


def _run(
    root: Path | None,
    *,
    persistent_artifacts: bool,
) -> dict[str, Any]:
    started = time.monotonic()
    registry = discover_domain_packs()
    if tuple(item["id"] for item in registry) != DIRECTIONS:
        raise ValueError("六方向 registry 必须精确包含并按顺序列出六个方向")
    direction_statuses = _load_direction_statuses()
    results = [
        _inspect_direction(item, direction_statuses[item["id"]])
        for item in registry
    ]
    report = {
        "schema": REPORT_SCHEMA,
        "mode": "static_manifest_validation",
        "persistent_artifacts": persistent_artifacts,
        "directions": results,
        "summary": {
            "status": "pass",
            "ready": sum(item["status"] == "ready" for item in results),
            "pending": sum(item["status"] == "pending" for item in results),
            "duration_seconds": _duration(started),
        },
    }
    if root is not None:
        _write_json(root / "six_domain_packs_report.json", report)
    return report


def _inspect_direction(
    registry_item: dict[str, str],
    availability: str,
) -> dict[str, Any]:
    direction = registry_item["id"]
    version = registry_item["version"]
    pack_root = resolve_domain_pack(f"{direction}@{version}")
    pack_manifest = validate_domain_pack(pack_root)
    runnable_manifest = _read_json(pack_root / "payload" / "domain-pack.json")
    device, cpu_fallback = _require_static_contract(
        runnable_manifest,
        direction=direction,
        version=version,
    )
    return {
        "direction": direction,
        "pack": {
            "id": pack_manifest["id"],
            "version": pack_manifest["version"],
            "template_id": pack_manifest["template_id"],
        },
        "status": availability,
        "device": device,
        "cpu_fallback": cpu_fallback,
        "run_kind": "synthetic_debug_only",
        "paper_eligible": False,
    }


def _require_static_contract(
    manifest: dict[str, Any],
    *,
    direction: str,
    version: str,
) -> tuple[str, bool | None]:
    if (
        manifest.get("schema")
        != "cv-experiment-workflow.runnable-domain-pack.v1"
        or manifest.get("id") != direction
        or manifest.get("version") != version
        or manifest.get("template_id") != f"PACK-{direction.upper()}"
        or manifest.get("primary_direction") != direction
    ):
        raise ValueError(f"{direction} runnable manifest 与 registry 身份不一致")
    smoke = manifest.get("synthetic_smoke")
    dataset = manifest.get("dataset")
    expected_device = "cuda" if direction == "gzsl" else "cpu"
    if (
        not isinstance(smoke, dict)
        or smoke.get("device") != expected_device
        or smoke.get("run_kind") != "synthetic_debug_only"
        or smoke.get("paper_eligible") is not False
        or not isinstance(dataset, dict)
        or dataset.get("automatic_download") is not False
    ):
        raise ValueError(f"{direction} 静态运行合同无效")
    cpu_fallback = smoke.get("cpu_fallback")
    if direction == "gzsl" and cpu_fallback is not False:
        raise ValueError("GZSL 必须固定使用 CUDA，且不得提供 CPU fallback")
    return expected_device, cpu_fallback


def _load_direction_statuses() -> dict[str, str]:
    candidates = [
        base / "config" / "directions" / "catalog.json"
        for base in (ROOT, *ROOT.parents)
    ]
    catalog_path = next((path for path in candidates if path.is_file()), None)
    if catalog_path is None:
        raise ValueError("缺少六方向唯一状态文件 config/directions/catalog.json")
    payload = _read_json(catalog_path)
    directions = payload.get("directions")
    if (
        payload.get("schema") != "cvwf.direction-catalog.v1"
        or not isinstance(directions, list)
        or len(directions) != len(DIRECTIONS)
    ):
        raise ValueError("六方向状态文件 schema 或方向数量无效")
    result: dict[str, str] = {}
    for item in directions:
        if not isinstance(item, dict):
            raise ValueError("六方向状态条目必须是对象")
        slug = item.get("slug")
        status = item.get("status")
        if slug not in DIRECTIONS or slug in result or status not in {
            "ready",
            "pending",
        }:
            raise ValueError("六方向 slug、状态或唯一性无效")
        result[str(slug)] = str(status)
    if set(result) != set(DIRECTIONS):
        raise ValueError("六方向状态文件缺失方向")
    return result


def _prove_branch_run_lineage(
    root: Path,
    project: Path,
    repository: Path,
    *,
    codebase_id: str,
    base_tag: str,
    base_commit: str,
) -> dict[str, Any]:
    """用真实 Idea、Git 和 Run 记录证明代码分支与执行记录是两层事实。"""
    repository = repository.resolve(strict=True)
    if _git_text(repository, "rev-parse", f"{base_tag}^{{commit}}") != base_commit:
        raise ValueError("基础 Tag 没有指向登记的初始 commit")
    if _git_text(repository, "rev-parse", "HEAD") != base_commit:
        raise ValueError("建立分支前，分类仓库没有停在基础 Tag 的 commit")
    if not _git_clean(repository):
        raise ValueError("建立分支前，分类仓库不是干净工作树")

    manifests = root / "lineage-manifests"
    manifests.mkdir()
    source_manifest = manifests / "source.json"
    commit_object = _git_bytes(repository, "cat-file", "commit", base_commit)
    _write_json(
        source_manifest,
        {
            "schema": SOURCE_SCHEMA,
            "kind": "code",
            "identity": "classification-domain-pack-base-tag",
            "locator": str(repository),
            "revision": base_tag,
            "commit": base_commit,
            "digest": f"sha256:{hashlib.sha256(commit_object).hexdigest()}",
            "license": "NOASSERTION",
        },
    )
    source = register_source(project, source_manifest, repository)

    root_manifest = manifests / "idea-root.json"
    root_idea_payload = _idea_manifest(
        source["id"],
        mechanism=(
            "从同一个分类模板 Tag 派生两条代码分支，并把每次执行分别记为 Run。"
        ),
        hypothesis=(
            "若代码版本和执行记录是两层事实，则每条分支都能关联至少两个不同 Run，"
            "而所有 Run 仍精确绑定各自分支的同一个 commit。"
        ),
    )
    _write_json(root_manifest, root_idea_payload)
    root_idea = save_idea(project, root_manifest)

    branch_specs = (
        (
            "experiment/idea-a",
            "idea-a",
            "把分类头宽度作为第一条轻量尝试；这里只验证代码与运行记录的关系。",
        ),
        (
            "experiment/idea-b",
            "idea-b",
            "把特征归一化作为第二条轻量尝试；这里只验证代码与运行记录的关系。",
        ),
    )
    branch_ideas: list[dict[str, Any]] = []
    for _branch, slug, mechanism in branch_specs:
        manifest_path = manifests / f"{slug}.json"
        _write_json(
            manifest_path,
            _idea_manifest(
                source["id"],
                mechanism=mechanism,
                hypothesis=(
                    f"{slug} 分支的两次合成调试应生成两个不同 Run，"
                    "且二者绑定同一分支和同一 commit。"
                ),
            ),
        )
        branch_ideas.append(
            revise_catalog_idea(
                project,
                root_idea["id"],
                manifest_path,
                f"从共同 Idea 派生 {slug} 实验路线",
            )
        )

    branch_results: list[dict[str, Any]] = []
    for index, ((branch, slug, _mechanism), idea) in enumerate(
        zip(branch_specs, branch_ideas)
    ):
        _git_text(repository, "switch", "--create", branch, base_tag)
        branch_file = repository / "experiments" / f"{slug}.json"
        branch_file.parent.mkdir()
        _write_json(
            branch_file,
            {
                "schema": "cv-experiment-workflow.lineage-fixture.v1",
                "idea_id": idea["id"],
                "purpose": "验证 Branch 记录代码演化、Run 记录实际执行",
                "paper_eligible": False,
            },
        )
        relative_branch_file = branch_file.relative_to(repository).as_posix()
        _git_text(repository, "add", "--", relative_branch_file)
        _commit_test_fixture(
            repository,
            f"test: add {slug} lineage fixture",
        )
        branch_commit = _git_text(repository, "rev-parse", "HEAD")
        parent_commit = _git_text(repository, "rev-parse", "HEAD^")
        if parent_commit != base_commit:
            raise ValueError(f"{branch} 不是直接从共同基础 Tag 派生")

        run_records: list[dict[str, Any]] = []
        for run_index in range(2):
            binding = {
                "codebase_id": codebase_id,
                "branch": branch,
                "commit": branch_commit,
                "tag": None,
            }
            task = create_task(
                project,
                owner_request=(
                    f"验证 {branch} 的第 {run_index + 1} 次独立合成调试记录"
                ),
                route="tune",
                target_refs=[idea["id"]],
                route_inputs={
                    "config": {"mode": "synthetic_smoke"},
                    "seed": 101 + index * 10 + run_index,
                    "code_binding": binding,
                    "debug_required": True,
                    "changes_code_behavior": False,
                    "code_verified": True,
                },
                budget={"max_runs": 1},
                stop_condition={"type": "single_debug", "after_runs": 1},
            )
            outcome = execute_task(project, task["id"], purpose="debug")
            run = load_run(project, outcome["run"]["id"])
            code = run["frozen"]["code"]
            data = run["frozen"]["data"]
            run_records.append(
                {
                    "id": run["id"],
                    "task_id": task["id"],
                    "purpose": run["purpose"],
                    "branch": code["branch"],
                    "commit": code["commit"],
                    "execution_stage": run["execution"]["stage"],
                    "outcome": run["execution"]["outcome"],
                    "evidence_level": outcome["evidence_level"],
                    "run_kind": data["run_kind"],
                    "paper_eligible": data["paper_eligible"],
                }
            )
        branch_results.append(
            {
                "branch": branch,
                "commit": branch_commit,
                "parent_commit": parent_commit,
                "source_tag": base_tag,
                "idea_id": idea["id"],
                "git_worktree_clean": _git_clean(repository),
                "runs": run_records,
            }
        )

    proof = {
        "schema": LINEAGE_SCHEMA,
        "repository": "repositories/smoke-cls",
        "codebase_id": codebase_id,
        "base_tag": base_tag,
        "base_commit": base_commit,
        "idea_tree": {
            "root_idea_id": root_idea["id"],
            "branch_idea_ids": [idea["id"] for idea in branch_ideas],
            "shared_parent_verified": all(
                idea["parent_idea_ref"] == root_idea["id"]
                for idea in branch_ideas
            ),
        },
        "branches": branch_results,
    }
    _validate_lineage_proof(proof)
    return proof


def _idea_manifest(
    source_id: str,
    *,
    mechanism: str,
    hypothesis: str,
) -> dict[str, Any]:
    return {
        "schema": IDEA_SCHEMA,
        "status": "ready",
        "problem": (
            "科研工作中容易把一次代码改动和一次实际执行混成同一条记录，"
            "导致后续无法准确回答某个结果来自哪条代码线。"
        ),
        "mechanism": mechanism,
        "falsifiable_hypothesis": hypothesis,
        "source_refs": [source_id],
        "evidence_refs": [],
        "source_links": [
            {
                "source_ref": source_id,
                "locator": "workflow_adapter.py 与基础 Tag",
                "supports_field": "mechanism",
                "claim": "该代码来源提供共同模板和可运行入口。",
            }
        ],
    }


def _validate_lineage_proof(proof: dict[str, Any]) -> bool:
    """严格核对同一 Tag、两条分支和每条分支多个 Run 的证明。"""
    if not isinstance(proof, dict) or proof.get("schema") != LINEAGE_SCHEMA:
        raise ValueError("Branch/Run lineage schema 无效")
    repository = proof.get("repository")
    if (
        not isinstance(repository, str)
        or not repository.startswith("repositories/")
        or Path(repository).is_absolute()
    ):
        raise ValueError("lineage repository 必须是交付目录内的相对路径")
    codebase_id = proof.get("codebase_id")
    if not isinstance(codebase_id, str) or CODEBASE_ID.fullmatch(codebase_id) is None:
        raise ValueError("lineage codebase_id 无效")
    base_tag = proof.get("base_tag")
    base_commit = proof.get("base_commit")
    if not isinstance(base_tag, str) or not base_tag:
        raise ValueError("lineage base_tag 无效")
    if not isinstance(base_commit, str) or COMMIT.fullmatch(base_commit) is None:
        raise ValueError("lineage base_commit 无效")

    idea_tree = proof.get("idea_tree")
    if not isinstance(idea_tree, dict):
        raise ValueError("lineage Idea 树缺失")
    root_idea_id = idea_tree.get("root_idea_id")
    branch_idea_ids = idea_tree.get("branch_idea_ids")
    if (
        not isinstance(root_idea_id, str)
        or IDEA_ID.fullmatch(root_idea_id) is None
        or not isinstance(branch_idea_ids, list)
        or len(branch_idea_ids) != 2
        or len(set(branch_idea_ids)) != 2
        or any(
            not isinstance(value, str) or IDEA_ID.fullmatch(value) is None
            for value in branch_idea_ids
        )
        or idea_tree.get("shared_parent_verified") is not True
    ):
        raise ValueError("lineage Idea 树没有形成一个父节点和两个子节点")

    branches = proof.get("branches")
    if not isinstance(branches, list) or len(branches) != 2:
        raise ValueError("lineage 必须精确包含两条实验 Branch")
    branch_names: set[str] = set()
    branch_commits: set[str] = set()
    observed_ideas: list[str] = []
    run_ids: set[str] = set()
    task_ids: set[str] = set()
    for branch_record in branches:
        if not isinstance(branch_record, dict):
            raise ValueError("Branch 记录必须是 JSON object")
        branch = branch_record.get("branch")
        commit = branch_record.get("commit")
        idea_id = branch_record.get("idea_id")
        if not isinstance(branch, str) or not branch.startswith("experiment/"):
            raise ValueError("实验 Branch 名称无效")
        if branch in branch_names:
            raise ValueError("实验 Branch 名称重复")
        branch_names.add(branch)
        if (
            not isinstance(commit, str)
            or COMMIT.fullmatch(commit) is None
            or commit == base_commit
            or commit in branch_commits
        ):
            raise ValueError("实验 Branch commit 无效或没有形成独立代码线")
        branch_commits.add(commit)
        if (
            branch_record.get("parent_commit") != base_commit
            or branch_record.get("source_tag") != base_tag
        ):
            raise ValueError("两条 Branch 必须直接来自同一个基础 Tag")
        if (
            not isinstance(idea_id, str)
            or IDEA_ID.fullmatch(idea_id) is None
            or branch_record.get("git_worktree_clean") is not True
        ):
            raise ValueError("Branch 的 Idea 绑定或 Git 干净状态无效")
        observed_ideas.append(idea_id)
        runs = branch_record.get("runs")
        if not isinstance(runs, list) or len(runs) < 2:
            raise ValueError("每条 Branch 必须至少产生两个 Run")
        for run in runs:
            if not isinstance(run, dict):
                raise ValueError("Run 记录必须是 JSON object")
            run_id = run.get("id")
            task_id = run.get("task_id")
            if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
                raise ValueError("Run ID 无效")
            if run_id in run_ids:
                raise ValueError("Run ID 重复，代码分支和执行记录被错误合并")
            run_ids.add(run_id)
            if not isinstance(task_id, str) or TASK_ID.fullmatch(task_id) is None:
                raise ValueError("Run 对应的 Task ID 无效")
            if task_id in task_ids:
                raise ValueError("每次独立 Run 必须来自独立 Task")
            task_ids.add(task_id)
            if (
                run.get("purpose") != "debug"
                or run.get("branch") != branch
                or run.get("commit") != commit
                or run.get("execution_stage") != "closed"
                or run.get("outcome") != "succeeded"
                or run.get("evidence_level") != "debug"
                or run.get("run_kind") != "synthetic_debug_only"
                or run.get("paper_eligible") is not False
            ):
                raise ValueError("Run 没有精确绑定 Branch，或被错误标成论文成绩")
    if observed_ideas != branch_idea_ids:
        raise ValueError("Branch 与共同 Idea 树的两个子节点不一致")
    return True


def _git_clean(repository: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repository), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip()


def _git_text(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ValueError(f"Git {' '.join(arguments)} 失败：{detail}")
    return result.stdout.strip()


def _commit_test_fixture(repository: Path, message: str) -> str:
    return _git_text(
        repository,
        "-c",
        "user.name=CV-Workflow-Test",
        "-c",
        "user.email=cv-workflow-test@example.invalid",
        "commit",
        "-m",
        message,
    )


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Git {' '.join(arguments)} 失败：{detail}")
    return bytes(result.stdout)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object expected: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _duration(started: float) -> float:
    return round(time.monotonic() - started, 3)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="静态核验六方向目录、manifest、当前可用状态和设备合同"
    )
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        help="可选：保存 JSON 报告、临时仓库和合成产物的空目录",
    )
    parser.add_argument(
        "--device",
        choices=("cuda",),
        help="可选：确认当前唯一 ready 的 GZSL 设备合同为 cuda；只做静态检查",
    )
    arguments = parser.parse_args(argv)
    report = run_six_domain_packs(
        artifacts_root=arguments.artifacts_root,
        device=arguments.device,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["summary"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
