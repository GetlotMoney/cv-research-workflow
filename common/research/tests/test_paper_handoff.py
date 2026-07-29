from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tests._helpers import SCRIPTS


sys.path.insert(0, str(SCRIPTS))
import rw  # noqa: E402
from workflow_core import evidence as evidence_module  # noqa: E402
from workflow_core import paper_handoff as paper_handoff_module  # noqa: E402
from workflow_core import runs as runs_module  # noqa: E402
from workflow_core.attempts import validate_project_workflow  # noqa: E402
from workflow_core.evidence import record_evidence_transition  # noqa: E402
from workflow_core.paper_handoff import (  # noqa: E402
    HANDOFF_HASH_CHUNK_BYTES,
    MAX_ARTIFACT_COUNT,
    MAX_TOTAL_ARTIFACT_BYTES,
    export_paperflow,
    verify_paperflow_handoff,
)
from workflow_core.project import init_project  # noqa: E402
from workflow_core.releases import upgrade_workflow_lock  # noqa: E402
from workflow_core.runs import (  # noqa: E402
    _canonical_digest,
    create_run,
    finish_run,
    start_run,
)
from workflow_core.tasking import (  # noqa: E402
    create_task,
    readiness,
    transition_task,
)


class PaperHandoffTests(unittest.TestCase):
    HANDOFF_CONTEXT_KEY = "_cv_experiment_workflow_handoff"
    HANDOFF_CONTEXT_SCHEMA = (
        "cv-experiment-workflow.paperflow-v1-context/v1"
    )
    VERIFICATION_REPORT_FIELDS = {
        "schema",
        "status",
        "error_code",
        "project_uuid",
        "run_id",
        "event_id",
        "source_status",
        "current_payload_sha256",
    }
    OLD_1_2_2_LOCK = {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.2.2",
        "system_version": "SYS-V2.10.2",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 57,
            "digest": (
                "sha256:"
                "4125290e8cad4c349d2b59d80132ff25333571d692afa5283acd85eb18871ad0"
            ),
        },
    }
    OLD_1_2_3_LOCK = {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.2.3",
        "system_version": "SYS-V2.10.3",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 57,
            "digest": (
                "sha256:"
                "a610932129c3ee8397f7a1d887b3b485fb2f283a67af8a92d668a9d7e977592a"
            ),
        },
    }
    OLD_1_2_1_LOCK = {
        "schema": "cv-experiment-workflow.workflow-lock.v2",
        "skill_id": "cv-experiment-workflow",
        "release_version": "1.2.1",
        "system_version": "SYS-V2.10.1",
        "payload": {
            "algorithm": "sha256-path-bytes-v1",
            "file_count": 56,
            "digest": (
                "sha256:"
                "b17499136cefeab72995f498a430e0afddf9ddb79d703beac0a2349118c96fdc"
            ),
        },
    }

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        init_project(self.project, "paper-handoff", layout="v2")
        self.control = self.project / ".experiment-workflow"
        self.source_artifacts = self.project / "artifacts"
        self.source_artifacts.mkdir()
        self.raw_log = self.source_artifacts / "run.log"
        self.metrics_file = self.source_artifacts / "metrics.json"
        self.raw_log.write_text("fixture log\n", encoding="utf-8")
        self.metrics_file.write_text('{"score":0.75}\n', encoding="utf-8")
        self.task = create_task(
            self.project,
            owner_request="验证一个可写进论文的结果",
            route="tune",
            target_refs=["VER-0001", "baseline"],
            route_inputs={
                "config": {},
                "seed": 7,
                "debug_required": False,
                "changes_code_behavior": False,
                "code_verified": True,
                "execution_authorized": True,
                "plan_unchanged": True,
                "primary_metric": "score",
                "baseline": 0.70,
                "allowed_changes": ["learning_rate"],
                "baseline_run_ref": None,
            },
            budget={"max_runs": 20},
            stop_condition={"max_failures": 3},
        )
        transition_task(self.project, self.task["id"], "preparing")
        readiness(
            self.project,
            self.task["id"],
            target_clear=True,
            template_runnable=True,
            mutation_allowed=True,
            code_verified=True,
        )

    @staticmethod
    def complete_frozen() -> dict[str, object]:
        return {
            "code": {
                "repository": "local-fixture",
                "commit": "a" * 40,
            },
            "config": {
                "learning_rate": 0.001,
                "metric_definition": {
                    "score": "held-out test accuracy",
                },
            },
            "seed": 7,
            "data": {
                "dataset_id": "fixture-dataset",
                "version": "v1",
                "split": "test",
            },
            "environment": {
                "python": "3.10",
                "backend": "cpu",
            },
        }

    def make_run(
        self,
        level: str | None,
        *,
        purpose: str = "evidence",
        frozen: dict[str, object] | None = None,
    ) -> dict[str, object]:
        # 这些用例验证已存在的旧版交付记录。当前系统禁止新建未绑定
        # evidence；这里只在测试夹具里重放旧写入规则，不能形成生产旁路。
        with mock.patch.object(
            runs_module,
            "_require_bound_for_new_evidence",
        ):
            run = create_run(
                self.project,
                self.task["id"],
                self.complete_frozen() if frozen is None else frozen,
                purpose=purpose,
            )
        start_run(self.project, run["id"], process_id=123)
        finish_run(
            self.project,
            run["id"],
            outcome="succeeded",
            exit_code=0,
            metrics={"score": 0.75},
            raw_log="artifacts/run.log",
            implementation="valid",
            interface="valid",
            data="valid",
            metrics_quality="valid",
            hypothesis="supported",
            limitations=[],
            suggestions=[],
            artifacts=["artifacts/metrics.json"],
            issue_kind=None,
        )
        if level is not None:
            current_validator = evidence_module._validate_transition_semantics

            def replay_legacy_transition(*args, **kwargs):
                kwargs["live_write"] = False
                return current_validator(*args, **kwargs)

            transition_patch = mock.patch.object(
                evidence_module,
                "_validate_transition_semantics",
                side_effect=replay_legacy_transition,
            )
        else:
            transition_patch = mock.patch.object(
                evidence_module,
                "_validate_transition_semantics",
                wraps=evidence_module._validate_transition_semantics,
            )
        with transition_patch:
            if level is None:
                return run
            if level in {"single_run", "confirmed", "revoked"}:
                record_evidence_transition(
                    self.project,
                    run["id"],
                    "single_run",
                    evidence_refs=[run["id"]],
                    reason="单次实验可信",
                    proposed_by="analyst",
                    checked_by="reviewer",
                    applied_by="coordinator",
                )
            if level in {"confirmed", "revoked"}:
                record_evidence_transition(
                    self.project,
                    run["id"],
                    "confirmed",
                    evidence_refs=[run["id"]],
                    reason="复核通过",
                    proposed_by="analyst",
                    checked_by="reviewer",
                    applied_by="coordinator",
                )
            if level == "revoked":
                record_evidence_transition(
                    self.project,
                    run["id"],
                    "revoked",
                    evidence_refs=[run["id"]],
                    reason="撤销",
                    proposed_by="analyst",
                    checked_by="reviewer",
                    applied_by="coordinator",
                )
            elif level in {"debug", "disqualified"}:
                record_evidence_transition(
                    self.project,
                    run["id"],
                    level,
                    evidence_refs=[run["id"]],
                    reason=f"转到 {level}",
                    proposed_by="analyst",
                    checked_by="reviewer",
                    applied_by="coordinator",
                )
        return run

    def test_confirmed_complete_identity_is_stable_result_verified(self) -> None:
        run = self.make_run("confirmed")

        first = export_paperflow(
            self.project, run["id"], self.root / "unused-1.json", dry_run=True
        )
        second = export_paperflow(
            self.project, run["id"], self.root / "unused-2.json", dry_run=True
        )

        self.assertEqual("cv-research-handoff/v1", first["schema"])
        self.assertEqual("confirmed", first["payload"]["source_status"])
        self.assertEqual("result_verified", first["payload"]["qualification"])
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["payload_sha256"], second["payload_sha256"])
        self.assertNotIn("created_at", first["payload"])
        canonical = json.dumps(
            first["payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        self.assertEqual(f"sha256:{digest}", first["payload_sha256"])
        self.assertEqual(f"rsh-{digest}", first["snapshot_id"])

    def test_single_run_is_candidate_and_never_verified(self) -> None:
        run = self.make_run("single_run")

        envelope = export_paperflow(
            self.project, run["id"], self.root / "unused.json", dry_run=True
        )

        self.assertEqual("single_run", envelope["payload"]["source_status"])
        self.assertEqual("candidate", envelope["payload"]["qualification"])
        self.assertNotEqual(
            "result_verified", envelope["payload"]["qualification"]
        )

    def test_debug_disqualified_revoked_and_unknown_are_rejected(self) -> None:
        cases = (
            ("debug", "debug"),
            ("disqualified", "evidence"),
            ("revoked", "evidence"),
            (None, "evidence"),
        )
        for level, purpose in cases:
            with self.subTest(level=level):
                run = self.make_run(level, purpose=purpose)
                with self.assertRaisesRegex(ValueError, "不允许导出|未知"):
                    export_paperflow(
                        self.project,
                        run["id"],
                        self.root / f"{run['id']}.json",
                        dry_run=True,
                    )

    def test_confirmed_missing_each_reproducibility_identity_is_rejected(
        self,
    ) -> None:
        run = self.make_run("confirmed")
        run_path = self.control / "runs" / run["id"] / "run.json"
        original = json.loads(run_path.read_text(encoding="utf-8"))
        mutations = {
            "code_version": lambda value: value["frozen"].update(
                {"code": {"repository": "local-fixture"}}
            ),
            "data_identity": lambda value: value["frozen"].update(
                {"data": {"split": "test"}}
            ),
            "config": lambda value: value["frozen"].update({"config": {}}),
            "seed": lambda value: value["frozen"].update({"seed": True}),
            "environment": lambda value: value["frozen"].update(
                {"environment": {}}
            ),
            "metric_definition": lambda value: value["frozen"]["config"].pop(
                "metric_definition"
            ),
            "metric_value": lambda value: value["result"].update(
                {"metrics": {}}
            ),
            "result_source": lambda value: value["result"].update(
                {"raw_log": None}
            ),
            "artifact_source": lambda value: value.update({"artifacts": []}),
        }
        expected_errors = {
            "code_version": "frozen.code.commit",
            "data_identity": "frozen.data.dataset_id",
            "config": "frozen.config",
            "seed": "frozen.seed",
            "environment": "frozen.environment",
            "metric_definition": "frozen.config.metric_definition",
            "metric_value": "result.metrics",
            "result_source": "result.raw_log",
            "artifact_source": "artifacts",
        }
        for label, mutate in mutations.items():
            with self.subTest(identity=label):
                changed = deepcopy(original)
                mutate(changed)
                changed["frozen_digest"] = _canonical_digest(changed["frozen"])
                run_path.write_text(
                    json.dumps(changed, ensure_ascii=False), encoding="utf-8"
                )
                with self.assertRaisesRegex(
                    ValueError,
                    re.escape(expected_errors[label]),
                ):
                    export_paperflow(
                        self.project,
                        run["id"],
                        self.root / f"missing-{label}.json",
                        dry_run=True,
                    )
        run_path.write_text(
            json.dumps(original, ensure_ascii=False), encoding="utf-8"
        )

    def test_missing_project_uuid_is_rejected(self) -> None:
        run = self.make_run("confirmed")
        project_path = self.control / "project.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project.pop("project_id")
        project_path.write_text(
            json.dumps(project, ensure_ascii=False), encoding="utf-8"
        )

        with self.assertRaises(ValueError):
            export_paperflow(
                self.project, run["id"], self.root / "missing-uuid.json", dry_run=True
            )

    def test_output_is_atomic_no_overwrite_and_dry_run_writes_nothing(self) -> None:
        run = self.make_run("confirmed")
        dry_target = self.root / "dry-run.json"

        def project_snapshot() -> dict[str, tuple[bytes, int]]:
            return {
                path.relative_to(self.project).as_posix(): (
                    path.read_bytes(),
                    path.stat().st_mtime_ns,
                )
                for path in self.project.rglob("*")
                if path.is_file()
            }

        before = project_snapshot()
        with mock.patch(
            "workflow_core.locking._open_lock_file",
            side_effect=AssertionError(
                "dry-run 不得创建写锁 bootstrap 临时文件"
            ),
        ):
            export_paperflow(
                self.project,
                run["id"],
                dry_target,
                dry_run=True,
            )
        self.assertFalse(dry_target.exists())
        self.assertEqual(before, project_snapshot())

        target = self.root / "handoff.json"
        envelope = export_paperflow(self.project, run["id"], target)
        self.assertEqual(
            envelope,
            json.loads(target.read_text(encoding="utf-8")),
        )
        with self.assertRaises(FileExistsError):
            export_paperflow(self.project, run["id"], target)

    def test_output_rejects_control_directory_absolute_relative_and_link_paths(
        self,
    ) -> None:
        run = self.make_run("confirmed")
        previous_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous_cwd)

        def control_snapshot() -> dict[str, bytes]:
            return {
                path.relative_to(self.control).as_posix(): path.read_bytes()
                for path in self.control.rglob("*")
                if path.is_file()
            }

        cases: list[tuple[str, Path, bool]] = [
            (
                "absolute",
                self.control / "forbidden-absolute.json",
                False,
            ),
            (
                "relative-dry-run",
                Path(
                    "project/.experiment-workflow/"
                    "forbidden-relative.json"
                ),
                True,
            ),
            (
                "control-itself",
                self.control,
                True,
            ),
        ]
        link = self.root / "control-link"
        try:
            link.symlink_to(self.control, target_is_directory=True)
        except OSError:
            link = None
        if link is not None:
            cases.extend(
                [
                    ("linked-parent", link / "forbidden-link.json", False),
                    (
                        "linked-parent-dry-run",
                        link / "forbidden-link-dry.json",
                        True,
                    ),
                ]
            )

        for label, target, dry_run in cases:
            with self.subTest(path_kind=label, dry_run=dry_run):
                before = control_snapshot()
                resolved_target = target.resolve(strict=False)
                try:
                    with self.assertRaisesRegex(ValueError, "控制目录"):
                        export_paperflow(
                            self.project,
                            run["id"],
                            target,
                            dry_run=dry_run,
                        )
                    self.assertEqual(before, control_snapshot())
                    self.assertFalse(
                        resolved_target.is_file(),
                        f"拒绝后不应生成文件：{resolved_target}",
                    )
                    validate_project_workflow(self.project)
                finally:
                    if resolved_target.is_file():
                        resolved_target.unlink()

    def test_output_pins_resolved_parent_before_link_ancestor_swap(self) -> None:
        run = self.make_run("confirmed")
        safe_parent = self.root / "safe-output" / "tasks"
        safe_parent.mkdir(parents=True)
        mutable_link = self.root / "mutable-output-link"
        try:
            mutable_link.symlink_to(
                safe_parent.parent,
                target_is_directory=True,
            )
        except OSError as error:
            self.skipTest(f"symlink unavailable: {error}")
        safe_target = safe_parent / "pinned-handoff.json"
        control_target = (
            self.control / "tasks" / "pinned-handoff.json"
        )
        requested_target = (
            mutable_link / "tasks" / "pinned-handoff.json"
        )
        original_build = paper_handoff_module._build_payload
        swapped = False

        def swap_link_after_output_check(
            *args: object,
            **kwargs: object,
        ) -> dict[str, object]:
            nonlocal swapped
            payload = original_build(*args, **kwargs)
            mutable_link.unlink()
            mutable_link.symlink_to(
                self.control,
                target_is_directory=True,
            )
            swapped = True
            return payload

        try:
            with mock.patch(
                "workflow_core.paper_handoff._build_payload",
                side_effect=swap_link_after_output_check,
            ):
                export_paperflow(
                    self.project,
                    run["id"],
                    requested_target,
                )

            self.assertTrue(swapped)
            self.assertTrue(safe_target.is_file())
            self.assertFalse(control_target.exists())
            validate_project_workflow(self.project)
        finally:
            for path in (safe_target, control_target):
                if path.is_file():
                    path.unlink()

    def test_output_anchors_resolved_parent_during_ancestor_replacement(
        self,
    ) -> None:
        run = self.make_run("confirmed")
        safe_root = self.root / "resolved-safe-output"
        safe_parent = safe_root / "tasks"
        safe_parent.mkdir(parents=True)
        moved_root = self.root / "moved-safe-output"
        requested_target = safe_parent / "anchored-handoff.json"
        moved_target = (
            moved_root / "tasks" / "anchored-handoff.json"
        )
        control_target = (
            self.control / "tasks" / "anchored-handoff.json"
        )
        original_build = paper_handoff_module._build_payload
        ancestor_replaced = False
        replacement_blocked = False

        def replace_resolved_ancestor_after_check(
            *args: object,
            **kwargs: object,
        ) -> dict[str, object]:
            nonlocal ancestor_replaced, replacement_blocked
            payload = original_build(*args, **kwargs)
            try:
                safe_root.rename(moved_root)
            except OSError:
                replacement_blocked = True
            else:
                safe_root.symlink_to(
                    self.control,
                    target_is_directory=True,
                )
                ancestor_replaced = True
            return payload

        try:
            with mock.patch(
                "workflow_core.paper_handoff._build_payload",
                side_effect=replace_resolved_ancestor_after_check,
            ):
                export_paperflow(
                    self.project,
                    run["id"],
                    requested_target,
                )

            self.assertTrue(ancestor_replaced or replacement_blocked)
            pinned_target = (
                moved_target if ancestor_replaced else requested_target
            )
            self.assertTrue(pinned_target.is_file())
            self.assertFalse(control_target.exists())
            validate_project_workflow(self.project)
        finally:
            for path in (requested_target, moved_target, control_target):
                if path.is_file():
                    path.unlink()

    def test_source_files_are_hashed_and_producer_identity_is_stable(self) -> None:
        run = self.make_run("confirmed")

        envelope = export_paperflow(
            self.project, run["id"], self.root / "unused.json", dry_run=True
        )

        reproducibility = envelope["payload"]["reproducibility"]
        raw_log = reproducibility["raw_log"]
        artifact = reproducibility["artifacts"][0]
        self.assertEqual("artifacts/run.log", raw_log["path"])
        self.assertEqual(self.raw_log.stat().st_size, raw_log["size_bytes"])
        self.assertEqual(
            f"sha256:{hashlib.sha256(self.raw_log.read_bytes()).hexdigest()}",
            raw_log["sha256"],
        )
        self.assertEqual("artifacts/metrics.json", artifact["path"])
        self.assertEqual(self.metrics_file.stat().st_size, artifact["size_bytes"])
        self.assertEqual(
            f"sha256:{hashlib.sha256(self.metrics_file.read_bytes()).hexdigest()}",
            artifact["sha256"],
        )
        self.assertEqual(
            {
                "skill_id": "cv-experiment-workflow",
                "release_version": "1.5.0",
                "system_version": "SYS-V2.13.0",
            },
            envelope["payload"]["producer"],
        )

    def test_current_v1_shape_is_unchanged_and_context_binds_omitted_facts(
        self,
    ) -> None:
        run = self.make_run("confirmed")
        run_record = json.loads(
            (
                self.control / "runs" / run["id"] / "run.json"
            ).read_text(encoding="utf-8")
        )
        evidence_events = [
            json.loads(line)
            for line in (
                self.control / "evidence.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        current_event = evidence_events[-1]

        envelope = export_paperflow(
            self.project,
            run["id"],
            self.root / "unused.json",
            dry_run=True,
        )
        payload = envelope["payload"]
        context = payload["reproducibility"]["config"][
            self.HANDOFF_CONTEXT_KEY
        ]

        self.assertEqual(
            {
                "schema",
                "snapshot_id",
                "payload_sha256",
                "created_at",
                "payload",
            },
            set(envelope),
        )
        self.assertEqual(
            {
                "producer",
                "project",
                "task",
                "run",
                "source_status",
                "qualification",
                "evidence_event",
                "reproducibility",
            },
            set(payload),
        )
        self.assertEqual(
            {"skill_id", "release_version", "system_version"},
            set(payload["producer"]),
        )
        self.assertEqual({"project_id"}, set(payload["project"]))
        self.assertEqual(
            {"task_id", "route", "version_refs"},
            set(payload["task"]),
        )
        self.assertEqual(
            {"run_id", "purpose", "frozen_digest"},
            set(payload["run"]),
        )
        self.assertEqual(
            {"event_id", "from", "to", "evidence_refs"},
            set(payload["evidence_event"]),
        )
        self.assertEqual(
            {
                "code",
                "config",
                "seed",
                "data",
                "environment",
                "metric_definition",
                "metrics",
                "raw_log",
                "artifacts",
            },
            set(payload["reproducibility"]),
        )
        self.assertEqual(
            {"path", "size_bytes", "sha256"},
            set(payload["reproducibility"]["raw_log"]),
        )
        self.assertTrue(payload["reproducibility"]["artifacts"])
        for artifact in payload["reproducibility"]["artifacts"]:
            self.assertEqual(
                {"path", "size_bytes", "sha256"},
                set(artifact),
            )
        self.assertEqual(self.HANDOFF_CONTEXT_SCHEMA, context["schema"])
        self.assertEqual(
            {"primary_metric": "score"},
            context["route_inputs"],
        )
        self.assertEqual(
            {"analysis": run_record["analysis"]},
            context["run"],
        )
        self.assertEqual(
            {
                field: current_event[field]
                for field in (
                    "reason",
                    "proposed_by",
                    "checked_by",
                    "applied_by",
                    "time",
                )
            },
            context["evidence_event"],
        )

    def test_current_export_rejects_reserved_context_key_collision(self) -> None:
        frozen = self.complete_frozen()
        frozen["config"][self.HANDOFF_CONTEXT_KEY] = {
            "schema": "user-owned",
        }
        run = self.make_run("confirmed", frozen=frozen)

        with self.assertRaisesRegex(ValueError, "保留键"):
            export_paperflow(
                self.project,
                run["id"],
                self.root / "reserved-key.json",
                dry_run=True,
            )

    def test_source_file_change_changes_payload_digest_and_snapshot(self) -> None:
        run = self.make_run("confirmed")
        first = export_paperflow(
            self.project, run["id"], self.root / "unused-1.json", dry_run=True
        )

        self.metrics_file.write_text('{"score":0.76}\n', encoding="utf-8")
        second = export_paperflow(
            self.project, run["id"], self.root / "unused-2.json", dry_run=True
        )

        self.assertNotEqual(first["payload_sha256"], second["payload_sha256"])
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertNotEqual(
            first["payload"]["reproducibility"]["artifacts"][0]["sha256"],
            second["payload"]["reproducibility"]["artifacts"][0]["sha256"],
        )

    def test_missing_escaping_and_non_regular_source_files_are_rejected(
        self,
    ) -> None:
        run = self.make_run("confirmed")
        run_path = self.control / "runs" / run["id"] / "run.json"
        original = json.loads(run_path.read_text(encoding="utf-8"))
        outside = self.root / "outside.log"
        outside.write_text("outside\n", encoding="utf-8")
        mutations = {
            "missing_raw_log": lambda value: value["result"].update(
                {"raw_log": "artifacts/missing.log"}
            ),
            "escaping_raw_log": lambda value: value["result"].update(
                {"raw_log": "../outside.log"}
            ),
            "directory_raw_log": lambda value: value["result"].update(
                {"raw_log": "artifacts"}
            ),
            "missing_artifact": lambda value: value.update(
                {"artifacts": ["artifacts/missing.json"]}
            ),
            "escaping_artifact": lambda value: value.update(
                {"artifacts": ["../outside.log"]}
            ),
            "directory_artifact": lambda value: value.update(
                {"artifacts": ["artifacts"]}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(source=label):
                changed = deepcopy(original)
                mutate(changed)
                run_path.write_text(
                    json.dumps(changed, ensure_ascii=False), encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    export_paperflow(
                        self.project,
                        run["id"],
                        self.root / f"invalid-{label}.json",
                        dry_run=True,
                    )
        run_path.write_text(
            json.dumps(original, ensure_ascii=False), encoding="utf-8"
        )

    def test_metric_definitions_and_primary_metric_are_required(self) -> None:
        run = self.make_run("confirmed")
        run_path = self.control / "runs" / run["id"] / "run.json"
        task_path = self.control / "tasks" / f"{self.task['id']}.json"
        original_run = json.loads(run_path.read_text(encoding="utf-8"))
        original_task = json.loads(task_path.read_text(encoding="utf-8"))
        cases = {
            "null_metric": (
                lambda run_value, task_value: run_value["result"].update(
                    {"metrics": {"score": None}}
                ),
                "指标值必须是有限数字：score",
            ),
            "wrong_metric_name": (
                lambda run_value, task_value: run_value["result"].update(
                    {"metrics": {"accuracy": 0.75}}
                ),
                "指标 accuracy 缺少同名定义",
            ),
            "null_definition": (
                lambda run_value, task_value: run_value["frozen"]["config"].update(
                    {"metric_definition": {"score": None}}
                ),
                "指标定义 score 必须是非空字符串",
            ),
            "missing_primary_metric": (
                lambda run_value, task_value: task_value["route_inputs"].update(
                    {"primary_metric": "accuracy"}
                ),
                "主指标 accuracy 在 result.metrics 中缺失",
            ),
        }
        for label, (mutate, error) in cases.items():
            with self.subTest(metric=label):
                changed_run = deepcopy(original_run)
                changed_task = deepcopy(original_task)
                mutate(changed_run, changed_task)
                changed_run["frozen_digest"] = _canonical_digest(
                    changed_run["frozen"]
                )
                run_path.write_text(
                    json.dumps(changed_run, ensure_ascii=False), encoding="utf-8"
                )
                task_path.write_text(
                    json.dumps(changed_task, ensure_ascii=False), encoding="utf-8"
                )
                with self.assertRaisesRegex(ValueError, re.escape(error)):
                    export_paperflow(
                        self.project,
                        run["id"],
                        self.root / f"metric-{label}.json",
                        dry_run=True,
                    )
        run_path.write_text(
            json.dumps(original_run, ensure_ascii=False), encoding="utf-8"
        )
        task_path.write_text(
            json.dumps(original_task, ensure_ascii=False), encoding="utf-8"
        )

    def test_duplicate_count_and_total_size_limits_fail_before_hashing(self) -> None:
        run = self.make_run("confirmed")
        run_path = self.control / "runs" / run["id"] / "run.json"
        original = json.loads(run_path.read_text(encoding="utf-8"))

        duplicate = deepcopy(original)
        duplicate["artifacts"] = ["artifacts/run.log"]
        run_path.write_text(
            json.dumps(duplicate, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "结果文件路径重复"):
            export_paperflow(
                self.project, run["id"], self.root / "duplicate.json", dry_run=True
            )

        too_many = deepcopy(original)
        too_many["artifacts"] = [
            f"artifacts/item-{index:03d}.json"
            for index in range(MAX_ARTIFACT_COUNT + 1)
        ]
        run_path.write_text(
            json.dumps(too_many, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "artifact 数量超过"):
            export_paperflow(
                self.project, run["id"], self.root / "too-many.json", dry_run=True
            )

        with self.metrics_file.open("wb") as sparse:
            sparse.seek(MAX_TOTAL_ARTIFACT_BYTES)
            sparse.write(b"x")
        run_path.write_text(
            json.dumps(original, ensure_ascii=False), encoding="utf-8"
        )
        with mock.patch(
            "workflow_core.paper_handoff._hash_preflight_file",
            side_effect=AssertionError("总大小超限后不应开始哈希"),
        ):
            with self.assertRaisesRegex(ValueError, "结果文件总大小超过"):
                export_paperflow(
                    self.project,
                    run["id"],
                    self.root / "too-large.json",
                    dry_run=True,
                )

    def test_result_files_are_hashed_in_bounded_chunks(self) -> None:
        run = self.make_run("confirmed")
        self.metrics_file.write_bytes(b"x" * (HANDOFF_HASH_CHUNK_BYTES + 17))
        original_open = Path.open
        read_sizes: list[int] = []

        class TrackingFile:
            def __init__(self, path: Path, *args: object, **kwargs: object) -> None:
                self.source = original_open(path, *args, **kwargs)

            def __enter__(self) -> "TrackingFile":
                return self

            def __exit__(self, *args: object) -> None:
                self.source.close()

            def fileno(self) -> int:
                return self.source.fileno()

            def read(self, size: int = -1) -> bytes:
                read_sizes.append(size)
                return self.source.read(size)

        def tracked_open(
            path: Path, *args: object, **kwargs: object
        ) -> object:
            if path == self.metrics_file and args and args[0] == "rb":
                return TrackingFile(path, *args, **kwargs)
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", autospec=True, side_effect=tracked_open):
            export_paperflow(
                self.project,
                run["id"],
                self.root / "chunked.json",
                dry_run=True,
            )

        self.assertGreaterEqual(read_sizes.count(HANDOFF_HASH_CHUNK_BYTES), 2)
        self.assertNotIn(-1, read_sizes)

    def test_first_hash_reads_only_preflight_size_then_one_byte_probe(
        self,
    ) -> None:
        run = self.make_run("confirmed")
        original_open = Path.open
        expected_size = self.metrics_file.stat().st_size
        probe_reads = 0
        oversized_requests: list[int] = []

        class GrowingFile:
            def __init__(self, path: Path, *args: object, **kwargs: object) -> None:
                self.source = original_open(path, *args, **kwargs)

            def __enter__(self) -> "GrowingFile":
                return self

            def __exit__(self, *args: object) -> None:
                self.source.close()

            def fileno(self) -> int:
                return self.source.fileno()

            def read(self, size: int = -1) -> bytes:
                nonlocal probe_reads
                remaining = expected_size - self.source.tell()
                if remaining > 0:
                    expected_request = min(
                        HANDOFF_HASH_CHUNK_BYTES,
                        remaining,
                    )
                    if size != expected_request:
                        oversized_requests.append(size)
                    return self.source.read(size)
                probe_reads += 1
                return b"x" if probe_reads == 1 else b""

        def growing_open(
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> object:
            if path == self.metrics_file and args and args[0] == "rb":
                return GrowingFile(path, *args, **kwargs)
            return original_open(path, *args, **kwargs)

        with mock.patch.object(
            Path,
            "open",
            autospec=True,
            side_effect=growing_open,
        ):
            with self.assertRaisesRegex(ValueError, "发生变化"):
                export_paperflow(
                    self.project,
                    run["id"],
                    self.root / "bounded-first-hash.json",
                    dry_run=True,
                )
        self.assertEqual(1, probe_reads)
        self.assertEqual([], oversized_requests)

    def test_final_hash_reads_only_preflight_size_then_one_byte_probe(
        self,
    ) -> None:
        run = self.make_run("confirmed")
        original_open = Path.open
        expected_size = self.metrics_file.stat().st_size
        metrics_open_count = 0
        probe_reads = 0
        oversized_requests: list[int] = []

        class GrowingFile:
            def __init__(self, path: Path, *args: object, **kwargs: object) -> None:
                self.source = original_open(path, *args, **kwargs)

            def __enter__(self) -> "GrowingFile":
                return self

            def __exit__(self, *args: object) -> None:
                self.source.close()

            def fileno(self) -> int:
                return self.source.fileno()

            def read(self, size: int = -1) -> bytes:
                nonlocal probe_reads
                remaining = expected_size - self.source.tell()
                if remaining > 0:
                    expected_request = min(
                        HANDOFF_HASH_CHUNK_BYTES,
                        remaining,
                    )
                    if size != expected_request:
                        oversized_requests.append(size)
                    return self.source.read(size)
                probe_reads += 1
                return b"x" if probe_reads == 1 else b""

        def growing_on_final_open(
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> object:
            nonlocal metrics_open_count
            if path == self.metrics_file and args and args[0] == "rb":
                metrics_open_count += 1
                if metrics_open_count == 2:
                    return GrowingFile(path, *args, **kwargs)
            return original_open(path, *args, **kwargs)

        with mock.patch.object(
            Path,
            "open",
            autospec=True,
            side_effect=growing_on_final_open,
        ):
            with self.assertRaisesRegex(ValueError, "发生变化"):
                export_paperflow(
                    self.project,
                    run["id"],
                    self.root / "bounded-final-hash.json",
                    dry_run=True,
                )
        self.assertEqual(2, metrics_open_count)
        self.assertEqual(1, probe_reads)
        self.assertEqual([], oversized_requests)

    def test_append_and_same_size_overwrite_after_read_fail_closed(self) -> None:
        run = self.make_run("confirmed")
        original_open = Path.open

        for mutation in ("append", "same_size_overwrite"):
            with self.subTest(mutation=mutation):
                original = b"stable-result-content\n"
                self.metrics_file.write_bytes(original)
                before = self.metrics_file.stat()
                changed = False

                def mutate_after_eof() -> None:
                    nonlocal changed
                    if changed:
                        return
                    changed = True
                    if mutation == "append":
                        with original_open(self.metrics_file, "ab") as target:
                            target.write(b"appended")
                    else:
                        with original_open(self.metrics_file, "r+b") as target:
                            target.seek(0)
                            target.write(b"x" * len(original))
                    after_write = self.metrics_file.stat()
                    os.utime(
                        self.metrics_file,
                        ns=(
                            after_write.st_atime_ns,
                            max(
                                after_write.st_mtime_ns,
                                before.st_mtime_ns + 2_000_000_000,
                            ),
                        ),
                    )

                class MutatingFile:
                    def __init__(
                        self, path: Path, *args: object, **kwargs: object
                    ) -> None:
                        self.source = original_open(path, *args, **kwargs)

                    def __enter__(self) -> "MutatingFile":
                        return self

                    def __exit__(self, *args: object) -> None:
                        self.source.close()

                    def fileno(self) -> int:
                        return self.source.fileno()

                    def read(self, size: int = -1) -> bytes:
                        chunk = self.source.read(size)
                        if not chunk:
                            mutate_after_eof()
                        return chunk

                def mutating_open(
                    path: Path, *args: object, **kwargs: object
                ) -> object:
                    if path == self.metrics_file and args and args[0] == "rb":
                        return MutatingFile(path, *args, **kwargs)
                    return original_open(path, *args, **kwargs)

                with mock.patch.object(
                    Path,
                    "open",
                    autospec=True,
                    side_effect=mutating_open,
                ):
                    with self.assertRaisesRegex(ValueError, "发生变化") as caught:
                        export_paperflow(
                            self.project,
                            run["id"],
                            self.root / f"mutated-{mutation}.json",
                            dry_run=True,
                        )
                self.assertTrue(changed, str(caught.exception))

    def test_all_files_are_rechecked_after_the_last_hash(self) -> None:
        run = self.make_run("confirmed")
        original_hash = paper_handoff_module._hash_preflight_file
        raw_hashed = False

        def mutate_raw_while_hashing_artifact(
            item: dict[str, object],
        ) -> dict[str, object]:
            nonlocal raw_hashed
            if item["path"] == "artifacts/run.log":
                result = original_hash(item)
                raw_hashed = True
                return result
            if raw_hashed:
                before = self.raw_log.stat()
                self.raw_log.write_text("changed after first hash\n", encoding="utf-8")
                after = self.raw_log.stat()
                os.utime(
                    self.raw_log,
                    ns=(
                        after.st_atime_ns,
                        max(after.st_mtime_ns, before.st_mtime_ns + 2_000_000_000),
                    ),
                )
            return original_hash(item)

        with mock.patch(
            "workflow_core.paper_handoff._hash_preflight_file",
            side_effect=mutate_raw_while_hashing_artifact,
        ):
            with self.assertRaisesRegex(ValueError, "哈希完成后发生变化"):
                export_paperflow(
                    self.project,
                    run["id"],
                    self.root / "cross-file-toctou.json",
                    dry_run=True,
                )

    def test_final_recheck_detects_same_size_raw_log_replacement(self) -> None:
        run = self.make_run("confirmed")
        original_hash = paper_handoff_module._hash_preflight_file
        original_signature = paper_handoff_module._stable_file_signature
        raw_hashed = False

        def metadata_without_change_time(
            value: os.stat_result,
            *,
            include_ctime: bool = True,
        ) -> tuple[int, ...]:
            return original_signature(value, include_ctime=False)

        def replace_raw_after_its_first_hash(
            item: dict[str, object],
        ) -> dict[str, object]:
            nonlocal raw_hashed
            result = original_hash(item)
            if item["path"] == "artifacts/run.log":
                raw_hashed = True
                return result
            if raw_hashed:
                original = self.raw_log.read_bytes()
                before = self.raw_log.stat()
                self.raw_log.write_bytes(b"x" * len(original))
                os.utime(
                    self.raw_log,
                    ns=(before.st_atime_ns, before.st_mtime_ns),
                )
                raw_hashed = False
            return result

        with (
            mock.patch(
                "workflow_core.paper_handoff._stable_file_signature",
                side_effect=metadata_without_change_time,
            ),
            mock.patch(
                "workflow_core.paper_handoff._hash_preflight_file",
                side_effect=replace_raw_after_its_first_hash,
            ),
        ):
            with self.assertRaisesRegex(ValueError, "哈希完成后发生变化"):
                export_paperflow(
                    self.project,
                    run["id"],
                    self.root / "same-size-long-window.json",
                    dry_run=True,
                )

    def test_exact_previous_release_lock_upgrades_but_tampering_is_rejected(
        self,
    ) -> None:
        lock_path = self.control / "workflow.lock.json"
        lock_path.write_text(
            json.dumps(self.OLD_1_2_2_LOCK, ensure_ascii=False), encoding="utf-8"
        )

        report = upgrade_workflow_lock(self.project, SCRIPTS.parent)

        self.assertEqual("upgraded", report["status"])
        self.assertEqual("1.5.0", report["workflow_lock"]["release_version"])
        self.assertEqual("SYS-V2.13.0", report["workflow_lock"]["system_version"])

        tampered = deepcopy(self.OLD_1_2_2_LOCK)
        tampered["payload"]["digest"] = "sha256:" + "0" * 64
        lock_path.write_text(
            json.dumps(tampered, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "不受信任"):
            upgrade_workflow_lock(self.project, SCRIPTS.parent)

    def test_exact_installed_1_2_3_lock_upgrades_but_tampering_is_rejected(
        self,
    ) -> None:
        lock_path = self.control / "workflow.lock.json"
        lock_path.write_text(
            json.dumps(self.OLD_1_2_3_LOCK, ensure_ascii=False),
            encoding="utf-8",
        )

        report = upgrade_workflow_lock(self.project, SCRIPTS.parent)

        self.assertEqual("upgraded", report["status"])
        self.assertEqual(self.OLD_1_2_3_LOCK, report["previous_lock"])

        tampered = deepcopy(self.OLD_1_2_3_LOCK)
        digest = tampered["payload"]["digest"]
        tampered["payload"]["digest"] = digest[:-1] + (
            "0" if digest[-1] != "0" else "1"
        )
        lock_path.write_text(
            json.dumps(tampered, ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "不受信任"):
            upgrade_workflow_lock(self.project, SCRIPTS.parent)

    def test_cli_help_and_dry_run_dispatch(self) -> None:
        help_text = rw.build_parser().format_help()
        self.assertIn("export-paperflow", help_text)
        run = self.make_run("confirmed")
        target = self.root / "cli-dry-run.json"
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = rw.main(
                [
                    "export-paperflow",
                    "--project",
                    str(self.project),
                    "--run",
                    run["id"],
                    "--out",
                    str(target),
                    "--dry-run",
                ]
            )

        self.assertEqual(0, exit_code)
        self.assertFalse(target.exists())
        payload = json.loads(stdout.getvalue())
        self.assertEqual("cv-research-handoff/v1", payload["schema"])

    @staticmethod
    def _resign(envelope: dict[str, object]) -> None:
        payload = envelope["payload"]
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        envelope["payload_sha256"] = f"sha256:{digest}"
        envelope["snapshot_id"] = f"rsh-{digest}"

    def _freeze_exact_1_2_2_handoff(
        self,
        run_id: str,
    ) -> dict[str, object]:
        """按历史提交 4ed6660 的正文规则冻结一份等价真实输出。"""
        project = json.loads(
            (self.control / "project.json").read_text(encoding="utf-8")
        )
        task = json.loads(
            (self.control / "tasks" / f"{self.task['id']}.json").read_text(
                encoding="utf-8"
            )
        )
        run = json.loads(
            (self.control / "runs" / run_id / "run.json").read_text(
                encoding="utf-8"
            )
        )
        events = [
            json.loads(line)
            for line in (self.control / "evidence.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        event = [
            item for item in events if item.get("subject_id") == run_id
        ][-1]

        def historical_file_record(relative: str) -> dict[str, object]:
            content = (self.project / relative).read_bytes()
            return {
                "path": relative,
                "size_bytes": len(content),
                "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
            }

        frozen = run["frozen"]
        result = run["result"]
        payload = {
            "producer": {
                "skill_id": "cv-experiment-workflow",
                "release_version": "1.2.2",
                "system_version": "SYS-V2.10.2",
            },
            "project": {"project_id": project["project_id"]},
            "task": {
                "task_id": task["id"],
                "route": task["route"],
                "version_refs": list(task["target_refs"]),
            },
            "run": {
                "run_id": run["id"],
                "purpose": run["purpose"],
                "frozen_digest": run["frozen_digest"],
            },
            "source_status": "confirmed",
            "qualification": "result_verified",
            "evidence_event": {
                "event_id": event["event_id"],
                "from": event["from"],
                "to": event["to"],
                "evidence_refs": list(event["evidence_refs"]),
            },
            "reproducibility": {
                "code": deepcopy(frozen["code"]),
                "config": deepcopy(frozen["config"]),
                "seed": frozen["seed"],
                "data": deepcopy(frozen["data"]),
                "environment": deepcopy(frozen["environment"]),
                "metric_definition": deepcopy(
                    frozen["config"]["metric_definition"]
                ),
                "metrics": deepcopy(result["metrics"]),
                "raw_log": historical_file_record(result["raw_log"]),
                "artifacts": [
                    historical_file_record(relative)
                    for relative in run["artifacts"]
                ],
            },
        }
        envelope: dict[str, object] = {
            "schema": "cv-research-handoff/v1",
            "snapshot_id": "",
            "payload_sha256": "",
            "created_at": "2026-07-25T04:28:16+00:00",
            "payload": payload,
        }
        self._resign(envelope)
        return envelope

    def test_verify_exact_current_handoff_passes_with_stable_report(self) -> None:
        run = self.make_run("confirmed")
        handoff = self.root / "handoff.json"
        envelope = export_paperflow(self.project, run["id"], handoff)

        report = verify_paperflow_handoff(
            self.project,
            handoff,
            expected_run_id=run["id"],
            expected_payload_sha256=envelope["payload_sha256"],
        )

        self.assertEqual("pass", report["status"])
        self.assertEqual(self.VERIFICATION_REPORT_FIELDS, set(report))
        self.assertIsNone(report["error_code"])
        self.assertEqual(
            envelope["payload_sha256"], report["current_payload_sha256"]
        )
        self.assertEqual(run["id"], report["run_id"])
        self.assertEqual(
            envelope["payload"]["project"]["project_id"],
            report["project_uuid"],
        )
        self.assertEqual("confirmed", report["source_status"])
        self.assertEqual(
            envelope["payload"]["evidence_event"]["event_id"],
            report["event_id"],
        )

    def test_verify_accepts_exact_published_v1_producer_pairs(self) -> None:
        run = self.make_run("confirmed")
        current = export_paperflow(
            self.project,
            run["id"],
            self.root / "unused.json",
            dry_run=True,
        )
        published = (
            ("1.2.2", "SYS-V2.10.2"),
            ("1.2.3", "SYS-V2.10.3"),
            ("1.3.0", "SYS-V2.11.0"),
        )

        for release_version, system_version in published:
            with self.subTest(
                release_version=release_version,
                system_version=system_version,
            ):
                legacy = deepcopy(current)
                legacy["payload"]["producer"] = {
                    "skill_id": "cv-experiment-workflow",
                    "release_version": release_version,
                    "system_version": system_version,
                }
                legacy["payload"]["reproducibility"]["config"].pop(
                    "_cv_experiment_workflow_handoff",
                    None,
                )
                self._resign(legacy)
                handoff = self.root / f"legacy-{release_version}.json"
                handoff.write_text(
                    json.dumps(legacy, ensure_ascii=False),
                    encoding="utf-8",
                )

                report = verify_paperflow_handoff(
                    self.project,
                    handoff,
                    expected_run_id=run["id"],
                    expected_payload_sha256=legacy["payload_sha256"],
                )

                self.assertEqual("pass", report["status"])
                self.assertIsNone(report["error_code"])
                self.assertEqual(
                    legacy["payload_sha256"],
                    report["current_payload_sha256"],
                )

    def test_verify_accepts_real_1_2_2_body_with_non_nfkc_path(self) -> None:
        run = self.make_run("confirmed")
        historical_artifact = self.source_artifacts / "Ａ.json"
        historical_artifact.write_text(
            '{"score":0.75}\n',
            encoding="utf-8",
        )
        run_path = self.control / "runs" / run["id"] / "run.json"
        run_record = json.loads(run_path.read_text(encoding="utf-8"))
        run_record["artifacts"] = ["artifacts/Ａ.json"]
        run_path.write_text(
            json.dumps(run_record, ensure_ascii=False),
            encoding="utf-8",
        )
        historical = self._freeze_exact_1_2_2_handoff(run["id"])
        self.assertEqual(
            {
                "skill_id": "cv-experiment-workflow",
                "release_version": "1.2.2",
                "system_version": "SYS-V2.10.2",
            },
            historical["payload"]["producer"],
        )
        self.assertEqual(
            "artifacts/Ａ.json",
            historical["payload"]["reproducibility"]["artifacts"][0]["path"],
        )
        self.assertNotIn(
            self.HANDOFF_CONTEXT_KEY,
            historical["payload"]["reproducibility"]["config"],
        )
        handoff = self.root / "historical-1.2.2-unicode.json"
        handoff.write_text(
            json.dumps(historical, ensure_ascii=False),
            encoding="utf-8",
        )

        report = verify_paperflow_handoff(
            self.project,
            handoff,
            expected_run_id=run["id"],
            expected_payload_sha256=historical["payload_sha256"],
        )

        self.assertEqual("pass", report["status"])
        self.assertIsNone(report["error_code"])
        self.assertEqual(
            historical["payload_sha256"],
            report["current_payload_sha256"],
        )

    def test_verify_rejects_unknown_or_mismatched_producer_pairs(self) -> None:
        run = self.make_run("confirmed")
        current = export_paperflow(
            self.project,
            run["id"],
            self.root / "unused.json",
            dry_run=True,
        )
        invalid = (
            (
                "cv-experiment-workflow",
                "1.2.2",
                "SYS-V2.10.3",
            ),
            (
                "cv-experiment-workflow",
                "1.3.0",
                "SYS-V2.12.0",
            ),
            (
                "cv-experiment-workflow",
                "9.9.9",
                "SYS-V9.9.9",
            ),
            (
                "different-skill",
                "1.2.3",
                "SYS-V2.10.3",
            ),
        )

        for skill_id, release_version, system_version in invalid:
            with self.subTest(
                skill_id=skill_id,
                release_version=release_version,
                system_version=system_version,
            ):
                forged = deepcopy(current)
                forged["payload"]["producer"] = {
                    "skill_id": skill_id,
                    "release_version": release_version,
                    "system_version": system_version,
                }
                self._resign(forged)
                handoff = self.root / (
                    f"invalid-producer-{release_version}-{system_version}.json"
                )
                handoff.write_text(
                    json.dumps(forged, ensure_ascii=False),
                    encoding="utf-8",
                )

                report = verify_paperflow_handoff(
                    self.project,
                    handoff,
                    expected_run_id=run["id"],
                    expected_payload_sha256=forged["payload_sha256"],
                )

                self.assertEqual("fail", report["status"])
                self.assertEqual("producer_mismatch", report["error_code"])

    def test_verify_binds_primary_metric_analysis_and_evidence_review(
        self,
    ) -> None:
        run = self.make_run("confirmed")
        run_path = self.control / "runs" / run["id"] / "run.json"
        task_path = self.control / "tasks" / f"{self.task['id']}.json"
        evidence_path = self.control / "evidence.jsonl"
        original_run = json.loads(run_path.read_text(encoding="utf-8"))
        original_task = json.loads(task_path.read_text(encoding="utf-8"))
        original_events = [
            json.loads(line)
            for line in evidence_path.read_text(encoding="utf-8").splitlines()
        ]

        original_run["frozen"]["config"]["metric_definition"]["aux"] = (
            "held-out auxiliary score"
        )
        original_run["result"]["metrics"]["aux"] = 0.50
        original_run["frozen_digest"] = _canonical_digest(
            original_run["frozen"]
        )
        run_path.write_text(
            json.dumps(original_run, ensure_ascii=False),
            encoding="utf-8",
        )
        handoff = self.root / "bound-context.json"
        envelope = export_paperflow(self.project, run["id"], handoff)

        changed_task = deepcopy(original_task)
        changed_task["route_inputs"]["primary_metric"] = "aux"
        task_path.write_text(
            json.dumps(changed_task, ensure_ascii=False),
            encoding="utf-8",
        )
        report = verify_paperflow_handoff(
            self.project,
            handoff,
            expected_run_id=run["id"],
            expected_payload_sha256=envelope["payload_sha256"],
        )
        self.assertEqual("handoff_payload_mismatch", report["error_code"])
        task_path.write_text(
            json.dumps(original_task, ensure_ascii=False),
            encoding="utf-8",
        )

        changed_run = deepcopy(original_run)
        changed_run["analysis"]["limitations"] = ["新的适用边界"]
        run_path.write_text(
            json.dumps(changed_run, ensure_ascii=False),
            encoding="utf-8",
        )
        report = verify_paperflow_handoff(
            self.project,
            handoff,
            expected_run_id=run["id"],
            expected_payload_sha256=envelope["payload_sha256"],
        )
        self.assertEqual("handoff_payload_mismatch", report["error_code"])
        run_path.write_text(
            json.dumps(original_run, ensure_ascii=False),
            encoding="utf-8",
        )

        event_mutations = {
            "reason": "新的复核理由",
            "proposed_by": "second-analyst",
            "checked_by": "second-reviewer",
            "applied_by": "second-coordinator",
            "time": "2030-01-01T00:00:00+00:00",
        }
        for field, value in event_mutations.items():
            with self.subTest(evidence_field=field):
                changed_events = deepcopy(original_events)
                changed_events[-1][field] = value
                evidence_path.write_text(
                    "".join(
                        json.dumps(
                            event,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                        for event in changed_events
                    ),
                    encoding="utf-8",
                )
                report = verify_paperflow_handoff(
                    self.project,
                    handoff,
                    expected_run_id=run["id"],
                    expected_payload_sha256=envelope["payload_sha256"],
                )
                self.assertEqual(
                    "handoff_payload_mismatch",
                    report["error_code"],
                )
        evidence_path.write_text(
            "".join(
                json.dumps(
                    event,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
                for event in original_events
            ),
            encoding="utf-8",
        )

    def test_verify_rejects_self_signed_payload_forgery(self) -> None:
        run = self.make_run("confirmed")
        handoff = self.root / "forged.json"
        envelope = export_paperflow(
            self.project, run["id"], self.root / "unused.json", dry_run=True
        )
        envelope["payload"]["reproducibility"]["metrics"]["score"] = 0.99
        self._resign(envelope)
        handoff.write_text(
            json.dumps(envelope, ensure_ascii=False), encoding="utf-8"
        )

        report = verify_paperflow_handoff(
            self.project,
            handoff,
            expected_run_id=run["id"],
            expected_payload_sha256=envelope["payload_sha256"],
        )

        self.assertEqual("fail", report["status"])
        self.assertEqual(self.VERIFICATION_REPORT_FIELDS, set(report))
        self.assertEqual("handoff_payload_mismatch", report["error_code"])

    def test_verify_rejects_forged_producer_before_reading_source(self) -> None:
        run = self.make_run("confirmed")
        handoff = self.root / "forged-producer.json"
        envelope = export_paperflow(
            self.project, run["id"], self.root / "unused.json", dry_run=True
        )
        envelope["payload"]["producer"]["release_version"] = "9.9.9"
        self._resign(envelope)
        handoff.write_text(json.dumps(envelope), encoding="utf-8")

        report = verify_paperflow_handoff(
            self.project,
            handoff,
            expected_run_id=run["id"],
            expected_payload_sha256=envelope["payload_sha256"],
        )

        self.assertEqual("producer_mismatch", report["error_code"])
        self.assertEqual(run["id"], report["run_id"])
        self.assertIsNone(report["source_status"])
        self.assertIsNone(report["project_uuid"])
        self.assertIsNone(report["event_id"])
        self.assertIsNone(report["current_payload_sha256"])

    def test_verify_unknown_producer_wins_over_missing_or_revoked_source(
        self,
    ) -> None:
        run = self.make_run("confirmed")
        envelope = export_paperflow(
            self.project,
            run["id"],
            self.root / "unused.json",
            dry_run=True,
        )
        envelope["payload"]["producer"] = {
            "skill_id": "cv-experiment-workflow",
            "release_version": "9.9.9",
            "system_version": "SYS-V9.9.9",
        }
        self._resign(envelope)
        handoff = self.root / "unknown-producer.json"
        handoff.write_text(
            json.dumps(envelope, ensure_ascii=False),
            encoding="utf-8",
        )

        record_evidence_transition(
            self.project,
            run["id"],
            "revoked",
            evidence_refs=[run["id"]],
            reason="撤销历史结果",
            proposed_by="analyst",
            checked_by="reviewer",
            applied_by="coordinator",
        )
        projects = (
            ("missing", self.root / "missing-project"),
            ("revoked", self.project),
        )
        for label, project in projects:
            with self.subTest(source=label):
                report = verify_paperflow_handoff(
                    project,
                    handoff,
                    expected_run_id=run["id"],
                    expected_payload_sha256=envelope["payload_sha256"],
                )
                self.assertEqual("producer_mismatch", report["error_code"])
                self.assertIsNone(report["project_uuid"])
                self.assertIsNone(report["source_status"])
                self.assertIsNone(report["current_payload_sha256"])

    def test_verify_rejects_current_run_or_artifact_drift_with_same_event(
        self,
    ) -> None:
        run = self.make_run("confirmed")
        handoff = self.root / "handoff.json"
        envelope = export_paperflow(self.project, run["id"], handoff)
        run_path = self.control / "runs" / run["id"] / "run.json"
        original = json.loads(run_path.read_text(encoding="utf-8"))

        mutations = {
            "frozen": lambda value: value["frozen"]["config"].update(
                {"learning_rate": 0.002}
            ),
            "metrics": lambda value: value["result"]["metrics"].update(
                {"score": 0.76}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = deepcopy(original)
                mutate(changed)
                changed["frozen_digest"] = _canonical_digest(changed["frozen"])
                run_path.write_text(
                    json.dumps(changed, ensure_ascii=False), encoding="utf-8"
                )
                report = verify_paperflow_handoff(
                    self.project,
                    handoff,
                    expected_run_id=run["id"],
                    expected_payload_sha256=envelope["payload_sha256"],
                )
                self.assertEqual("fail", report["status"])
                self.assertEqual(
                    "handoff_payload_mismatch", report["error_code"]
                )
                self.assertEqual(
                    envelope["payload"]["evidence_event"]["event_id"],
                    report["event_id"],
                )
        run_path.write_text(json.dumps(original), encoding="utf-8")
        self.metrics_file.write_text('{"score":0.76}\n', encoding="utf-8")
        report = verify_paperflow_handoff(
            self.project,
            handoff,
            expected_run_id=run["id"],
            expected_payload_sha256=envelope["payload_sha256"],
        )
        self.assertEqual("handoff_payload_mismatch", report["error_code"])

    def test_verify_rejects_revoked_bad_package_and_unavailable_source(self) -> None:
        run = self.make_run("confirmed")
        handoff = self.root / "handoff.json"
        envelope = export_paperflow(self.project, run["id"], handoff)
        record_evidence_transition(
            self.project,
            run["id"],
            "revoked",
            evidence_refs=[run["id"]],
            reason="撤销论文证据",
            proposed_by="analyst",
            checked_by="reviewer",
            applied_by="coordinator",
        )

        revoked = verify_paperflow_handoff(
            self.project,
            handoff,
            expected_run_id=run["id"],
            expected_payload_sha256=envelope["payload_sha256"],
        )
        self.assertEqual("fail", revoked["status"])
        self.assertEqual("revoked", revoked["error_code"])
        self.assertEqual("revoked", revoked["source_status"])

        bad = self.root / "bad.json"
        bad.write_text('{"schema":"wrong"}', encoding="utf-8")
        invalid = verify_paperflow_handoff(
            self.project,
            bad,
            expected_run_id=run["id"],
            expected_payload_sha256=envelope["payload_sha256"],
        )
        self.assertEqual("handoff_invalid", invalid["error_code"])

        unavailable = verify_paperflow_handoff(
            self.root / "missing-project",
            handoff,
            expected_run_id=run["id"],
            expected_payload_sha256=envelope["payload_sha256"],
        )
        self.assertEqual("source_unavailable", unavailable["error_code"])

        (self.project / ".experiment-workflow.init.lock").write_text(
            "not-a-workflow-lock\n", encoding="utf-8"
        )
        invalid_lock = verify_paperflow_handoff(
            self.project,
            handoff,
            expected_run_id=run["id"],
            expected_payload_sha256=envelope["payload_sha256"],
        )
        self.assertEqual("source_unavailable", invalid_lock["error_code"])

    def test_verify_cli_always_prints_json_and_uses_nonzero_for_failure(
        self,
    ) -> None:
        run = self.make_run("confirmed")
        handoff = self.root / "handoff.json"
        envelope = export_paperflow(self.project, run["id"], handoff)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            passed = rw.main(
                [
                    "verify-paperflow-handoff",
                    "--project",
                    str(self.project),
                    "--handoff",
                    str(handoff),
                    "--expected-run",
                    run["id"],
                    "--expected-payload-sha256",
                    envelope["payload_sha256"],
                ]
            )
        self.assertEqual(0, passed)
        self.assertEqual("pass", json.loads(stdout.getvalue())["status"])

        payload = json.loads(handoff.read_text(encoding="utf-8"))
        payload["payload"]["reproducibility"]["metrics"]["score"] = 0.99
        self._resign(payload)
        handoff.write_text(json.dumps(payload), encoding="utf-8")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            failed = rw.main(
                [
                    "verify-paperflow-handoff",
                    "--project",
                    str(self.project),
                    "--handoff",
                    str(handoff),
                    "--expected-run",
                    run["id"],
                    "--expected-payload-sha256",
                    envelope["payload_sha256"],
                ]
            )
        self.assertNotEqual(0, failed)
        self.assertEqual("fail", json.loads(stdout.getvalue())["status"])

    def test_verify_rejects_a_different_valid_handoff_than_caller_expected(
        self,
    ) -> None:
        first_run = self.make_run("confirmed")
        first_path = self.root / "first.json"
        first = export_paperflow(
            self.project,
            first_run["id"],
            first_path,
        )
        second_run = self.make_run("confirmed")
        second_path = self.root / "second.json"
        export_paperflow(self.project, second_run["id"], second_path)

        wrong_run = verify_paperflow_handoff(
            self.project,
            second_path,
            expected_run_id=first_run["id"],
            expected_payload_sha256=first["payload_sha256"],
        )
        self.assertEqual("expected_identity_mismatch", wrong_run["error_code"])

        wrong_hash = verify_paperflow_handoff(
            self.project,
            first_path,
            expected_run_id=first_run["id"],
            expected_payload_sha256="sha256:" + "0" * 64,
        )
        self.assertEqual("expected_identity_mismatch", wrong_hash["error_code"])

    def test_verify_compares_canonical_payload_bytes_not_only_hash(self) -> None:
        run = self.make_run("confirmed")
        handoff = self.root / "handoff.json"
        envelope = export_paperflow(self.project, run["id"], handoff)
        self.metrics_file.write_text('{"score":0.76}\n', encoding="utf-8")
        with (
            mock.patch(
                "workflow_core.paper_handoff._parse_handoff",
                return_value=envelope,
            ),
            mock.patch(
                "workflow_core.paper_handoff._sha256_bytes",
                return_value=envelope["payload_sha256"],
            ),
        ):
            report = verify_paperflow_handoff(
                self.project,
                handoff,
                expected_run_id=run["id"],
                expected_payload_sha256=envelope["payload_sha256"],
            )

        self.assertEqual(
            envelope["payload_sha256"], report["current_payload_sha256"]
        )
        self.assertEqual("handoff_payload_mismatch", report["error_code"])

    def test_verify_argument_errors_are_stable_json_without_stderr(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        secret_path = self.root / "secret-project-path"
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = rw.main(
                [
                    "verify-paperflow-handoff",
                    "--project",
                    str(secret_path),
                    "--handoff",
                    str(self.root / "handoff.json"),
                ]
            )

        self.assertNotEqual(0, exit_code)
        self.assertEqual("", stderr.getvalue())
        report = json.loads(stdout.getvalue())
        self.assertEqual(self.VERIFICATION_REPORT_FIELDS, set(report))
        self.assertEqual("cv-research-handoff-verification/v1", report["schema"])
        self.assertEqual("fail", report["status"])
        self.assertEqual("invalid_arguments", report["error_code"])
        self.assertIsNone(report["run_id"])
        self.assertIsNone(report["current_payload_sha256"])
        self.assertNotIn(str(secret_path), json.dumps(report))

    def test_windows_artifact_paths_are_strict_and_case_insensitive_unique(
        self,
    ) -> None:
        run = self.make_run("confirmed")
        run_path = self.control / "runs" / run["id"] / "run.json"
        original = json.loads(run_path.read_text(encoding="utf-8"))
        invalid_paths = (
            "artifacts/metrics.json:stream",
            "artifacts/CON",
            "artifacts/PRN.txt",
            "artifacts/name.",
            "artifacts/name ",
            "artifacts/Ａ.json",
        )
        for index, path in enumerate(invalid_paths):
            with self.subTest(path=path):
                changed = deepcopy(original)
                changed["artifacts"] = [path]
                run_path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(ValueError):
                    export_paperflow(
                        self.project,
                        run["id"],
                        self.root / f"invalid-windows-{index}.json",
                        dry_run=True,
                    )

        changed = deepcopy(original)
        changed["result"]["raw_log"] = "artifacts/run.log"
        changed["artifacts"] = ["ARTIFACTS/RUN.LOG"]
        run_path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "路径重复"):
            export_paperflow(
                self.project,
                run["id"],
                self.root / "case-collision.json",
                dry_run=True,
            )


if __name__ == "__main__":
    unittest.main()
