from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tests._helpers import SCRIPTS

import sys

sys.path.insert(0, str(SCRIPTS))
from workflow_core.project import init_project  # noqa: E402
from workflow_core.ui_service import _WRITE_ACTIONS, console_write_action  # noqa: E402


def _tree_bytes(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes() if path.is_file() else None
        )
        for path in sorted(root.rglob("*"))
    }


class UnifiedEntryTests(unittest.TestCase):
    def test_plain_directory_rejects_every_write_action_without_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            ordinary = Path(temporary) / "ordinary"
            ordinary.mkdir()
            (ordinary / "keep.txt").write_text("keep", encoding="utf-8")
            before = _tree_bytes(ordinary)
            for action in sorted(_WRITE_ACTIONS):
                with self.subTest(action=action):
                    with self.assertRaises((OSError, ValueError)):
                        console_write_action(ordinary, action, {})
                    self.assertEqual(before, _tree_bytes(ordinary))

    def test_freeze_action_calls_production_seal_export_and_verify(self) -> None:
        with TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            init_project(project, "统一入口", layout="v2")
            delivery_root = project / "deliverables" / "paperflow"
            delivery_root.mkdir(parents=True)
            data = {
                "brief_id": "BRIEF-0001",
                "mode": "hybrid",
                "selection": {"asset_mode": "hybrid"},
            }
            sealed = {
                "package_id": "PKG-0001",
                "readiness": "paper_ready",
                "asset_mode": "hybrid",
                "content_sha256": "sha256:" + "a" * 64,
            }
            verified = {
                "status": "pass",
                "package_id": "PKG-0001",
                "included_assets": 2,
                "referenced_assets": 0,
                "omitted_assets": 0,
            }
            with (
                mock.patch(
                    "workflow_core.ui_service.seal_paper_package",
                    return_value=sealed,
                ) as seal,
                mock.patch(
                    "workflow_core.ui_service.export_paper_package",
                    return_value={"package_id": "PKG-0001"},
                ) as export,
                mock.patch(
                    "workflow_core.ui_service.verify_paper_package",
                    return_value=verified,
                ) as verify,
            ):
                result = console_write_action(
                    project,
                    "freeze_research_package",
                    data,
                    paperflow_entrypoint=(
                        "http://127.0.0.1:8766/"
                        "#token=abcdefghijklmnop"
                    ),
                )

        self.assertEqual("exported", result["status"])
        self.assertEqual("PKG-0001", result["result"]["package_id"])
        self.assertEqual(
            "deliverables/paperflow/PKG-0001",
            result["result"]["handoff_path"],
        )
        self.assertEqual("ready", result["paperflow"]["status"])
        self.assertEqual("open_paperflow", result["next_action"])
        seal.assert_called_once()
        export.assert_called_once()
        verify.assert_called_once()

    def test_seal_and_human_confirmation_are_explicit_production_actions(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            init_project(project, "统一入口", layout="v2")
            sealed_run = {
                "id": "RUN-0001",
                "execution": {"stage": "finished", "outcome": "succeeded"},
                "output_seal": {
                    "total_files": 2,
                    "total_bytes": 32,
                    "seal_sha256": "sha256:" + "b" * 64,
                },
            }
            event = {
                "event_id": "EVT-0002",
                "subject_id": "RUN-0001",
                "checked_by": "reviewer",
            }
            with mock.patch(
                "workflow_core.ui_service.seal_run_outputs",
                return_value=sealed_run,
            ) as seal:
                sealed = console_write_action(
                    project,
                    "seal_run_outputs",
                    {"run_id": "RUN-0001"},
                )
            with (
                mock.patch(
                    "workflow_core.ui_service.current_evidence_level",
                    return_value="single_run",
                ),
                mock.patch(
                    "workflow_core.ui_service.record_evidence_transition",
                    return_value=event,
                ) as confirm,
            ):
                confirmed = console_write_action(
                    project,
                    "confirm_run_evidence",
                    {
                        "run_id": "RUN-0001",
                        "reason": "已核对",
                        "confirmed_by": "reviewer",
                        "acknowledge": True,
                    },
                )

        self.assertEqual("sealed", sealed["status"])
        self.assertEqual("confirmed", confirmed["status"])
        seal.assert_called_once_with(project.resolve(), "RUN-0001")
        confirm.assert_called_once()

    def test_human_confirmation_refuses_implicit_acknowledgement(self) -> None:
        with TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            init_project(project, "统一入口", layout="v2")
            with self.assertRaisesRegex(ValueError, "acknowledge"):
                console_write_action(
                    project,
                    "confirm_run_evidence",
                    {
                        "run_id": "RUN-0001",
                        "reason": "未明确确认",
                        "confirmed_by": "reviewer",
                        "acknowledge": False,
                    },
                )

    def test_run_actions_follow_the_real_execution_state(self) -> None:
        cases = [
            (
                "run_task_evidence",
                {
                    "status": "in_progress",
                    "run": {"id": "RUN-0001"},
                },
                "run_task_evidence",
            ),
            (
                "run_task_evidence",
                {
                    "status": "artifact_seal_pending",
                    "run": {"id": "RUN-0001"},
                    "evidence_level": "none",
                },
                "seal_run_outputs",
            ),
            (
                "run_task_evidence",
                {
                    "run": {"id": "RUN-0001"},
                    "evidence_level": "single_run",
                },
                "confirm_run_evidence",
            ),
            (
                "run_task_debug",
                {
                    "run": {
                        "id": "RUN-0001",
                        "frozen": {
                            "data": {
                                "run_kind": "synthetic_debug_only",
                            }
                        },
                    },
                    "evidence_level": "debug",
                },
                "create_task",
            ),
            (
                "run_task_debug",
                {
                    "run": {
                        "id": "RUN-0001",
                        "frozen": {
                            "data": {
                                "run_kind": "formal_dataset",
                            }
                        },
                    },
                    "evidence_level": "debug",
                },
                "run_task_evidence",
            ),
        ]
        for action, execution, expected_next in cases:
            with self.subTest(action=action, expected_next=expected_next):
                with TemporaryDirectory() as temporary:
                    project = Path(temporary) / "project"
                    init_project(project, "运行状态", layout="v2")
                    with mock.patch(
                        "workflow_core.ui_service.execute_task",
                        return_value=execution,
                    ):
                        result = console_write_action(
                            project,
                            action,
                            {"task_id": "TASK-0001"},
                        )
                self.assertEqual(expected_next, result["next_action"])


if __name__ == "__main__":
    unittest.main()
