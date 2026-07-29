from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.service import CoreService


ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class GzslResearchE2ETests(unittest.TestCase):
    def test_gpu_debug_evidence_confirmation_and_research_package(self) -> None:
        runtime_root = ROOT / ".runtime"
        runtime_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="e", dir=runtime_root) as raw:
            system = Path(raw)
            workspace = system / "users" / "e2e"
            for name in (
                "repositories",
                "deliveries",
                "inbox",
                "materials",
                ".runtime",
            ):
                (workspace / name).mkdir(parents=True, exist_ok=True)
            _write_json(
                workspace / "workspace.json",
                {
                    "schema": "cvwf.personal-workspace.v2",
                    "workspace_id": "e2e-workspace",
                    "slug": "e2e",
                    "display_name": "端到端临时科研实例",
                    "skill_id": "e2e-workflow",
                    "status": "active",
                    "direction_catalog_version": "DATA-DIRECTIONS-V1.0.0",
                },
            )
            _write_json(
                workspace / "REPOSITORY_INDEX.json",
                {
                    "schema": "cvwf.repository-index.v2",
                    "workspace_id": "e2e-workspace",
                    "repositories": [],
                },
            )
            _write_json(
                system / "config" / "active-workspace.json",
                {
                    "schema": "cvwf.active-workspace.v1",
                    "workspace_file": "users/e2e/workspace.json",
                },
            )
            (system / "users").mkdir(exist_ok=True)
            service = CoreService(
                {
                    "schema": "cvwf.research-system-config.v2",
                    "research_root": str(ROOT / "common" / "research"),
                    "environment_file": str(
                        ROOT / "config" / "environment.local.json"
                    ),
                    "directions_file": str(
                        ROOT / "config" / "directions" / "catalog.json"
                    ),
                    "users_root": "users",
                    "active_workspace_file": "config/active-workspace.json",
                },
                system_root=system,
            )
            service.create_repository("e2e-gzsl", "gzsl")
            experiment = service.create_experiment(
                "e2e-gzsl",
                {
                    "framework_slug": "gzsl-base",
                    "route": "tuning",
                    "slug": "e2e",
                    "title": "端到端调参",
                    "summary": "验证通用科研 GPU 证据闭环。",
                    "idea_ref": None,
                    "route_contract": {
                        "allowed_changes": ["learning_rate"],
                    },
                },
            )
            worktree = service.prepare_worktree(
                "e2e-gzsl", experiment["id"]
            )["worktree"]
            source = system / "tiny.npz"
            np.savez(source, **_tiny_gzsl_arrays())
            registered = service.register_dataset(
                "e2e-gzsl",
                {
                    "experiment_id": experiment["id"],
                    "worktree": worktree,
                    "source_path": str(source),
                    "dataset_slug": "tiny",
                    "dataset_id": "tiny-gzsl-e2e",
                    "version": "1",
                    "source_uri": "https://example.org/tiny-gzsl-e2e.npz",
                    "license": "test-only",
                },
            )
            common = {
                "experiment_id": experiment["id"],
                "worktree": worktree,
                "config": registered["config"],
                "seed": 31,
            }
            debug = service.run_experiment(
                "e2e-gzsl", {**common, "purpose": "debug"}
            )
            evidence = service.run_experiment(
                "e2e-gzsl", {**common, "purpose": "evidence"}
            )
            confirmed = service.confirm_run(
                "e2e-gzsl",
                {
                    "experiment_id": experiment["id"],
                    "local_run_id": evidence["id"],
                    "reason": "已复核 GPU 输出、S/U/H 和哈希。",
                    "proposed_by": "e2e",
                    "checked_by": "e2e-review",
                },
            )
            package = service.export_research_package(
                "e2e-gzsl",
                {
                    "experiment_id": experiment["id"],
                    "local_run_id": evidence["id"],
                    "research_brief": _brief(),
                    "claim_statement_zh": (
                        "合成小数据只验证系统闭环，不是论文成绩。"
                    ),
                    "confirmed_by": "e2e-review",
                },
            )

            self.assertEqual("debug", debug["evidence_level"])
            self.assertEqual("cuda", debug["device"])
            self.assertEqual("confirmed", confirmed["evidence_level"])
            self.assertEqual("paper_ready", package["readiness"])
            self.assertTrue(Path(package["package_path"]).is_dir())
            self.assertTrue(
                (Path(package["package_path"]) / "manifest.json").is_file()
            )


def _brief() -> dict[str, object]:
    return {
        "title": "通用 GZSL 科研闭环测试",
        "research_area": "广义零样本学习",
        "background": "科研结果必须经过固定流程和人工确认。",
        "problem": "未经核验的结果不能成为正式证据。",
        "objective": "验证 GPU Run、封存、确认和科研交付。",
        "research_questions": ["已确认 Run 能否形成可核查科研交付包？"],
        "hypotheses": [
            {
                "hypothesis_id": "H-01",
                "statement": "完整封存的已确认 Run 可以导出。",
                "falsification_criteria": "任一检查失败。",
            }
        ],
        "scope": {
            "included": ["通用 GZSL 科研闭环"],
            "excluded": ["论文写作", "正式论文成绩"],
        },
        "terminology": [
            {
                "term_en": "generalized zero-shot learning",
                "term_zh": "广义零样本学习",
                "definition_zh": "同时在已见类和未见类上进行分类。",
            }
        ],
        "planned_contributions": [
            {
                "contribution_id": "C-01",
                "statement_zh": "验证已确认 Run 可形成可核查科研交付包。",
                "provenance": "original",
                "source_refs": [],
            }
        ],
    }


def _tiny_gzsl_arrays() -> dict[str, np.ndarray]:
    class_ids = np.array([10, 20, 30, 40], dtype=np.int64)
    attributes = np.array(
        [
            [1.0, 0.0, 0.1],
            [0.0, 1.0, 0.1],
            [0.7, 0.7, 0.2],
            [-0.7, 0.7, 0.2],
        ],
        dtype=np.float32,
    )
    labels = np.repeat(class_ids, 3)
    features = np.vstack(
        [
            attributes[index]
            + np.array(
                [sample * 0.01, -sample * 0.005, 0.0],
                dtype=np.float32,
            )
            for index in range(4)
            for sample in range(3)
        ]
    ).astype(np.float32)
    return {
        "features": features,
        "labels": labels,
        "attributes": attributes,
        "class_ids": class_ids,
        "seen_class_ids": np.array([10, 20], dtype=np.int64),
        "unseen_class_ids": np.array([30, 40], dtype=np.int64),
        "train_indices": np.array([0, 1, 3, 4], dtype=np.int64),
        "test_seen_indices": np.array([2, 5], dtype=np.int64),
        "test_unseen_indices": np.array(
            [6, 7, 8, 9, 10, 11],
            dtype=np.int64,
        ),
    }


if __name__ == "__main__":
    unittest.main()
