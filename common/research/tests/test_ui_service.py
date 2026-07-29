from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tests._helpers import ROOT, SCRIPTS, cli_json, run_cli


sys.path.insert(0, str(SCRIPTS))
import rw  # noqa: E402
from workflow_core import planning, policy  # noqa: E402
from workflow_core.intake import check_intake  # noqa: E402
from workflow_core.planning import read_console_snapshot, status, task_list  # noqa: E402
from workflow_core.project import init_project  # noqa: E402
from workflow_core.releases import TRUSTED_PREVIOUS_WORKFLOW_LOCKS  # noqa: E402
from workflow_core.tasking import create_task  # noqa: E402
from workflow_core.ui_service import console_state  # noqa: E402


TOP_LEVEL_FIELDS = {
    "schema",
    "mode",
    "read_only",
    "execution_enabled",
    "snapshot",
    "start",
    "relationships",
    "execution",
}
CONDITION_IDS = {
    "project_snapshot",
    "workflow_lock",
    "intake_complete",
    "adapter_config",
    "adapter_runtime",
    "controlled_actions",
}


def _tree_snapshot(root: Path) -> dict[str, tuple[str, str | None, int, int]]:
    snapshot: dict[str, tuple[str, str | None, int, int]] = {}
    for path in sorted([root, *root.rglob("*")], key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if path.is_symlink():
            snapshot[relative] = (
                "link",
                str(path.readlink()),
                info.st_size,
                info.st_mtime_ns,
            )
        elif path.is_dir():
            snapshot[relative] = (
                "directory",
                None,
                info.st_size,
                info.st_mtime_ns,
            )
        else:
            snapshot[relative] = (
                "file",
                hashlib.sha256(path.read_bytes()).hexdigest(),
                info.st_size,
                info.st_mtime_ns,
            )
    return snapshot


def _trusted_lock(version: str = "1.2.1") -> dict[str, object]:
    return deepcopy(
        next(
            lock
            for lock in TRUSTED_PREVIOUS_WORKFLOW_LOCKS
            if lock["release_version"] == version
        )
    )


def _synthetic_snapshot() -> dict[str, object]:
    return {
        "layout": "v2",
        "project": {
            "schema": "cv-experiment-workflow.project.v2",
            "project_id": "11111111-1111-1111-1111-111111111111",
            "name": "关系测试",
            "project_skill": {
                "schema": "cv-experiment-workflow.project-skill.v1",
                "skill_id": "relationship-test",
                "entrypoint": "SKILL.md",
            },
        },
        "workflow_lock": {
            "schema": "cv-experiment-workflow.workflow-lock.v2",
            "skill_id": "cv-experiment-workflow",
            "release_version": "1.2.1",
            "system_version": "SYS-V2.10.1",
            "payload": {
                "algorithm": "sha256-path-bytes-v1",
                "file_count": 56,
                "digest": (
                    "sha256:"
                    "b17499136cefeab72995f498a430e0afddf9ddb79d703beac"
                    "0a2349118c96fdc"
                ),
            },
        },
        "workflow_lock_trust": "trusted_previous",
        "adapter": {
            "schema": "cv-experiment-workflow.adapter.v1",
            "status": "bound",
            "code_sources": [
                {
                    "repo_url": "local:D:/research/relation-test",
                    "commit": "a" * 40,
                    "relative_path": "workflow_adapter.py",
                }
            ],
            "capabilities": {"workflow_adapter": True},
        },
        "adapter_runtime": {
            "status": "block",
            "reason": "runtime_verification_failed",
        },
        "catalog": {
            "sources": {
                "SRC-0001": {
                    "id": "SRC-0001",
                    "kind": "paper",
                    "identity": "论文来源",
                },
                "SRC-0002": {
                    "id": "SRC-0002",
                    "kind": "code",
                    "identity": "代码来源",
                },
            },
            "ideas": {
                "IDEA-0001": {
                    "id": "IDEA-0001",
                    "status": "ready",
                    "source_refs": ["SRC-0001"],
                }
            },
            "templates": {
                "TPL-0001": {
                    "id": "TPL-0001",
                    "name": "基础模板",
                    "source_refs": ["SRC-0002"],
                }
            },
            "modules": {
                "MOD-0001": {
                    "id": "MOD-0001",
                    "status": "ready",
                    "source_refs": ["SRC-0001", "SRC-0002"],
                    "idea_refs": ["IDEA-0001"],
                    "attachment": {"template_ref": "TPL-0001", "point": "gate"},
                }
            },
        },
        "tasks": {
            "TASK-0001": {
                "id": "TASK-0001",
                "route": "innovation",
                "stage": "done",
                "target_refs": ["IDEA-0001", "TPL-0001", "MOD-0001"],
                "route_inputs": {},
                "budget": {"max_runs": 1},
                "run_refs": ["RUN-0001", "RUN-0002"],
            },
            "TASK-0002": {
                "id": "TASK-0002",
                "route": "tune",
                "stage": "queued",
                "target_refs": ["RUN-0001", "TASK-0001"],
                "route_inputs": {"baseline_run_ref": "RUN-0001"},
                "budget": {"max_runs": 2},
                "run_refs": [],
            },
        },
        "runs": {
            "RUN-0001": {
                "id": "RUN-0001",
                "task_id": "TASK-0001",
                "purpose": "debug",
                "execution": {"stage": "closed", "outcome": "succeeded"},
            },
            "RUN-0002": {
                "id": "RUN-0002",
                "task_id": "TASK-0001",
                "purpose": "debug",
                "execution": {"stage": "closed", "outcome": "succeeded"},
            }
        },
        "events": [
            {
                "event_id": "EVT-0001",
                "subject_type": "Run",
                "subject_id": "RUN-0001",
                "from": "none",
                "to": "debug",
                "evidence_refs": ["RUN-0001"],
            },
            {
                "event_id": "EVT-0002",
                "subject_type": "Run",
                "subject_id": "RUN-0001",
                "from": "debug",
                "to": "disqualified",
                "evidence_refs": ["RUN-0001", "RUN-0002"],
            },
        ],
    }


class ConsoleSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        init_project(self.project, "控制台快照", layout="v2")
        self.control = self.project / ".experiment-workflow"

    def _git(self, project: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(project), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def _bound_git_project(
        self,
        name: str,
        *,
        ignore_executable: bool = False,
    ) -> tuple[Path, Path]:
        project = Path(self.temporary.name) / name
        init_project(project, name, layout="v2")
        adapter_source = project / "workflow_adapter.py"
        adapter_source.write_bytes(
            (
                ROOT
                / "tests"
                / "fixtures"
                / "fake_cv_project"
                / "workflow_adapter.py"
            ).read_bytes()
        )
        if ignore_executable:
            gitignore = project / ".gitignore"
            gitignore.write_text(
                gitignore.read_text(encoding="utf-8") + "ignored.py\n",
                encoding="utf-8",
            )
        self._git(project, "init")
        self._git(project, "config", "user.name", "Console Test")
        self._git(project, "config", "user.email", "console@example.com")
        self._git(project, "add", ".")
        self._git(project, "commit", "-m", "fixture")
        commit = self._git(project, "rev-parse", "HEAD")
        adapter_path = project / ".experiment-workflow" / "adapter.json"
        adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
        adapter.update(
            {
                "status": "bound",
                "code_sources": [
                    {
                        "repo_url": f"local:{project.as_posix()}",
                        "commit": commit,
                        "relative_path": "workflow_adapter.py",
                    }
                ],
                "capabilities": {"workflow_adapter": True},
            }
        )
        adapter_path.write_text(
            json.dumps(adapter, ensure_ascii=False),
            encoding="utf-8",
        )
        return project, adapter_source

    def test_reads_one_locked_and_fully_validated_v2_snapshot_without_writing(
        self,
    ) -> None:
        from workflow_core import locking, validation

        before = _tree_snapshot(self.project)
        with (
            mock.patch.object(
                planning,
                "project_snapshot_lock",
                wraps=planning.project_snapshot_lock,
            ) as snapshot_lock,
            mock.patch.object(
                validation,
                "validate_v2_state_and_catalog_locked",
                wraps=validation.validate_v2_state_and_catalog_locked,
            ) as full_validator,
            mock.patch.object(
                locking,
                "project_write_lock",
                side_effect=AssertionError("控制台快照不得使用写锁"),
            ),
        ):
            snapshot = read_console_snapshot(self.project)

        snapshot_lock.assert_called_once()
        full_validator.assert_called_once()
        self.assertEqual("v2", snapshot["layout"])
        self.assertEqual("current", snapshot["workflow_lock_trust"])
        self.assertEqual("block", snapshot["adapter_runtime"]["status"])
        self.assertEqual(before, _tree_snapshot(self.project))

    def test_console_accepts_exact_trusted_previous_lock_but_existing_commands_do_not(
        self,
    ) -> None:
        lock_path = self.control / "workflow.lock.json"
        lock_path.write_text(
            json.dumps(_trusted_lock(), ensure_ascii=False),
            encoding="utf-8",
        )

        snapshot = read_console_snapshot(self.project)

        self.assertEqual("trusted_previous", snapshot["workflow_lock_trust"])
        with self.assertRaisesRegex(ValueError, "workflow lock"):
            status(self.project)
        with self.assertRaisesRegex(ValueError, "workflow lock"):
            task_list(self.project)

    def test_console_rejects_a_tampered_trusted_lock_digest(self) -> None:
        lock = _trusted_lock()
        lock["payload"]["digest"] = "sha256:" + "0" * 64
        (self.control / "workflow.lock.json").write_text(
            json.dumps(lock, ensure_ascii=False),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "workflow lock"):
            read_console_snapshot(self.project)

    def test_runtime_audit_never_executes_adapter_python_and_turns_git_drift_into_block(
        self,
    ) -> None:
        adapter = {
            "schema": "cv-experiment-workflow.adapter.v1",
            "status": "bound",
            "code_sources": [
                {
                    "repo_url": "local:test",
                    "commit": "a" * 40,
                    "relative_path": "workflow_adapter.py",
                }
            ],
            "capabilities": {"workflow_adapter": True},
        }
        with (
            mock.patch.object(
                policy,
                "_read_adapter_source_spec",
                side_effect=ValueError("工作树不干净"),
            ) as source_check,
            mock.patch("builtins.exec", side_effect=AssertionError("不得 exec")),
        ):
            result = policy.audit_runtime_adapter_locked(self.project, adapter)

        source_check.assert_called_once()
        self.assertEqual("block", result["status"])
        self.assertEqual("runtime_verification_failed", result["reason"])

    def test_read_only_git_never_refreshes_index_after_adapter_mtime_touch(
        self,
    ) -> None:
        project, adapter_source = self._bound_git_project("index-stability")
        touched = adapter_source.stat().st_mtime_ns + 2_000_000_000
        os.utime(adapter_source, ns=(touched, touched))
        index = project / ".git" / "index"
        before = (index.read_bytes(), index.stat().st_mtime_ns)

        result = console_state(project)

        self.assertEqual("pass", result["start"]["adapter"]["runtime"]["status"])
        self.assertEqual(before, (index.read_bytes(), index.stat().st_mtime_ns))

    def test_runtime_audit_reasons_are_stable_and_specific(self) -> None:
        base = {
            "schema": "cv-experiment-workflow.adapter.v1",
            "status": "bound",
            "code_sources": [
                {
                    "repo_url": "local:test",
                    "commit": "a" * 40,
                    "relative_path": "workflow_adapter.py",
                }
            ],
            "capabilities": {"workflow_adapter": True},
        }
        variants = {
            "adapter_unbound": {
                **deepcopy(base),
                "status": "unbound",
                "code_sources": [],
            },
            "capability_missing": {
                **deepcopy(base),
                "capabilities": {"workflow_adapter": False},
            },
            "code_source_count_invalid": {
                **deepcopy(base),
                "code_sources": [],
            },
        }
        for expected, adapter in variants.items():
            with self.subTest(expected=expected):
                result = policy.audit_runtime_adapter_locked(
                    self.project,
                    adapter,
                )
                self.assertEqual("block", result["status"])
                self.assertEqual(expected, result["reason"])

        dirty, _source = self._bound_git_project("dirty-reason")
        (dirty / "notes.txt").write_text("dirty\n", encoding="utf-8")
        self.assertEqual(
            "worktree_dirty",
            console_state(dirty)["start"]["adapter"]["runtime"]["reason"],
        )

        ignored, _source = self._bound_git_project(
            "ignored-reason",
            ignore_executable=True,
        )
        (ignored / "ignored.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.assertEqual(
            "ignored_executable_code",
            console_state(ignored)["start"]["adapter"]["runtime"]["reason"],
        )


class ConsoleStateTests(unittest.TestCase):
    def test_fixed_contract_uses_one_snapshot_and_pure_intake_then_builds_real_edges(
        self,
    ) -> None:
        snapshot = _synthetic_snapshot()
        with (
            mock.patch(
                "workflow_core.ui_service.read_console_snapshot",
                return_value=snapshot,
            ) as read_snapshot,
            mock.patch(
                "workflow_core.ui_service.evaluate_intake",
                wraps=__import__(
                    "workflow_core.intake",
                    fromlist=["evaluate_intake"],
                ).evaluate_intake,
            ) as intake,
        ):
            result = console_state(Path("unused"), intent="continue")

        read_snapshot.assert_called_once()
        intake.assert_called_once()
        self.assertEqual(TOP_LEVEL_FIELDS, set(result))
        self.assertEqual(
            "cv-experiment-workflow.console-state.v1",
            result["schema"],
        )
        self.assertEqual("controlled", result["mode"])
        self.assertIs(result["read_only"], False)
        self.assertIs(result["execution_enabled"], True)
        self.assertEqual("controlled_local_actions", result["execution"]["phase"])
        self.assertEqual(
            CONDITION_IDS,
            {item["id"] for item in result["execution"]["conditions"]},
        )
        self.assertEqual(
            ["adapter_runtime"],
            result["execution"]["blocked_by"],
        )

        nodes = {node["id"]: node for node in result["relationships"]["nodes"]}
        edges = {
            (edge["from"], edge["to"], edge["kind"])
            for edge in result["relationships"]["edges"]
        }
        self.assertEqual(15, len(nodes))
        self.assertEqual(19, len(edges))
        self.assertFalse(
            any(
                node_id.startswith(("CB-", "codebase:"))
                or "GZSL" in str(node)
                or "domain" in node
                for node_id, node in nodes.items()
            )
        )
        self.assertIn(
            ("SRC-0001", "IDEA-0001", "source_supports_idea"),
            edges,
        )
        self.assertIn(
            ("TPL-0001", "MOD-0001", "template_hosts_module"),
            edges,
        )
        self.assertIn(
            ("RUN-0001", "TASK-0002", "prior_run_feeds_task"),
            edges,
        )
        self.assertIn(
            ("TASK-0001", "TASK-0002", "task_prerequisite"),
            edges,
        )
        self.assertIn(
            ("RUN-0001", "EVT-0001", "run_has_evidence"),
            edges,
        )
        self.assertIn(
            ("RUN-0001", "EVT-0002", "run_has_evidence"),
            edges,
        )
        self.assertIn(
            ("RUN-0002", "EVT-0002", "run_supports_evidence"),
            edges,
        )
        self.assertFalse(
            any(node_id.startswith("evidence:") for node_id in nodes)
        )
        self.assertEqual(
            sorted(result["relationships"]["nodes"], key=lambda item: item["id"]),
            result["relationships"]["nodes"],
        )
        self.assertEqual(
            sorted(
                result["relationships"]["edges"],
                key=lambda item: (item["from"], item["to"], item["kind"]),
            ),
            result["relationships"]["edges"],
        )

    def test_invalid_storage_returns_the_same_empty_skeleton_without_partial_graph(
        self,
    ) -> None:
        cases = {
            "adapter": (
                "adapter.json",
                b'{"schema":"bad"}',
            ),
            "task": (
                "tasks/TASK-0001.json",
                b'{"id":"TASK-0001"}',
            ),
            "run": (
                "runs/RUN-0001/run.json",
                b'{"id":"RUN-0001"}',
            ),
            "evidence": (
                "evidence.jsonl",
                b'{"event_id":',
            ),
            "catalog": (
                "sources/SRC-0001.json",
                b'{"id":"SRC-0001"}',
            ),
            "lock": (
                "workflow.lock.json",
                json.dumps(
                    {
                        **_trusted_lock(),
                        "payload": {
                            **_trusted_lock()["payload"],
                            "digest": "sha256:" + "0" * 64,
                        },
                    }
                ).encode("utf-8"),
            ),
        }
        signatures: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, (relative, content) in cases.items():
                with self.subTest(name=name):
                    project = root / name
                    init_project(project, name, layout="v2")
                    target = project / ".experiment-workflow" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)

                    result = console_state(project)

                    self.assertEqual([], result["relationships"]["nodes"])
                    self.assertEqual([], result["relationships"]["edges"])
                    self.assertEqual([], result["relationships"]["omissions"])
                    self.assertIsNone(result["start"]["project"])
                    self.assertIsNone(result["start"]["workflow"])
                    self.assertIsNone(result["start"]["adapter"])
                    self.assertIsNone(result["start"]["intake"])
                    self.assertEqual("invalid", result["snapshot"]["status"])
                    signatures.append(result)
        self.assertTrue(
            all(item == signatures[0] for item in signatures[1:]),
            "不同损坏输入必须得到同一个固定空骨架",
        )

    def test_intake_check_and_console_use_the_same_discovery_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            init_project(project, "统一发现", layout="v2")
            task = create_task(
                project,
                owner_request="调参",
                route="tune",
                target_refs=["baseline"],
                route_inputs={"config": {}, "debug_required": False},
                budget={"max_runs": 2},
                stop_condition={"type": "max_runs", "value": 2},
            )

            intake = check_intake(project, intent="continue")
            console_intake = console_state(
                project,
                intent="continue",
            )["start"]["intake"]

        self.assertEqual(intake, console_intake)
        unfinished = console_intake["discovered"]["unfinished_tasks"]
        self.assertEqual([task["id"]], [item["id"] for item in unfinished])
        self.assertIs(unfinished[0]["route_supported"], True)

    def test_invalid_intent_and_non_object_input_are_rejected_before_project_read(
        self,
    ) -> None:
        with mock.patch(
            "workflow_core.ui_service.read_console_snapshot",
        ) as read_snapshot:
            with self.assertRaisesRegex(ValueError, "intent"):
                console_state(Path("missing"), intent="train")
            with self.assertRaisesRegex(ValueError, "provided"):
                console_state(Path("missing"), provided=[])
        read_snapshot.assert_not_called()

    def test_repository_index_optional_read_only_instance_has_expected_relationship_counts(
        self,
    ) -> None:
        common_dir_text = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        common_dir = Path(common_dir_text)
        if not common_dir.is_absolute():
            common_dir = ROOT / common_dir
        owner_root = common_dir.resolve().parent
        index_path = owner_root / "REPOSITORY_INDEX.json"
        if not index_path.is_file():
            return
        index = json.loads(
            index_path.read_text(encoding="utf-8")
        )
        record = next(
            item
            for item in index["repositories"]
            if item["id"] == "research-demo-lab"
        )
        self.assertEqual("read_only_reference", record["access"])
        self.assertEqual(r"..\Research-Demo-Lab", record["path"])
        instance = (owner_root / record["path"]).resolve()
        if not instance.is_dir():
            # 可移植发布快照只包含通用工作流，不捆绑个人只读实例。
            return

        result = console_state(instance)

        self.assertEqual("valid", result["snapshot"]["status"])
        self.assertEqual(
            {
                "sources": 9,
                "ideas": 1,
                "templates": 1,
                "modules": 1,
                "tasks": 4,
                "runs": 4,
                "evidence": 4,
                "nodes": 28,
                "edges": 33,
            },
            result["snapshot"]["counts"],
        )
        self.assertEqual(
            "trusted_previous",
            result["start"]["workflow"]["trust"],
        )
        self.assertEqual("block", result["start"]["adapter"]["runtime"]["status"])
        self.assertEqual(
            "worktree_dirty",
            result["start"]["adapter"]["runtime"]["reason"],
        )
        self.assertEqual(
            ["adapter_runtime"],
            result["execution"]["blocked_by"],
        )

    def test_console_state_cli_matches_core_and_reuses_strict_json_parser(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            init_project(project, "CLI 控制台", layout="v2")

            core = console_state(project, intent="status")
            command = cli_json(
                "console-state",
                "--project",
                project,
                "--intent",
                "status",
            )
            self.assertEqual(core, command)

            parsed = rw.build_parser().parse_args(
                ["console-state", "--project", str(project), "--intent", "status"]
            )
            self.assertIsNone(parsed.provided)
            for invalid in (
                "[]",
                '{"compute_budget":{"max_hours":NaN}}',
                '{"domain_task":{"domain":"a","domain":"b"}}',
            ):
                with self.subTest(invalid=invalid):
                    result = run_cli(
                        "console-state",
                        "--project",
                        project,
                        "--intent",
                        "tune",
                        "--provided",
                        invalid,
                        check=False,
                    )
                    self.assertEqual(2, result.returncode)


if __name__ == "__main__":
    unittest.main()
