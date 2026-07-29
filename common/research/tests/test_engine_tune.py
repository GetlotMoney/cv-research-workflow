from __future__ import annotations

import hashlib
import inspect
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from unittest import mock

from tests._helpers import ROOT, SCRIPTS, cli_json, read_json


sys.path.insert(0, str(SCRIPTS))
from workflow_core import (  # noqa: E402
    adapters,
    engine,
    evidence,
    policy,
    review,
    routes,
    runner,
    runs,
)
from workflow_core.evidence import current_evidence_level  # noqa: E402
from workflow_core.attempts import validate_project_workflow  # noqa: E402
from workflow_core.project import init_project  # noqa: E402
from workflow_core.runs import load_run  # noqa: E402
from workflow_core.tasking import (  # noqa: E402
    create_task,
    load_task,
    readiness,
    transition_task,
)


FIXTURE_ADAPTER = (
    ROOT / "tests" / "fixtures" / "fake_cv_project" / "workflow_adapter.py"
)
CORE = SCRIPTS / "workflow_core"


def _process_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait_for_single_object.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return wait_for_single_object(handle, 0) == 0x102
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _force_kill(pid: int) -> None:
    if not _process_exists(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


class TuneEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "fake-project"
        init_project(self.project, "fake-cv", layout="v2")
        shutil.copy2(FIXTURE_ADAPTER, self.project / "workflow_adapter.py")
        self._git("init")
        self._git("config", "user.name", "Test User")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "core.autocrlf", "false")
        self._git("add", ".")
        self._git("commit", "-m", "fixture adapter")
        self.adapter_commit = self._git("rev-parse", "HEAD")
        self.control = self.project / ".experiment-workflow"
        adapter = read_json(self.control / "adapter.json")
        adapter.update(
            {
                "status": "bound",
                "code_sources": [
                    {
                        "repo_url": "fixture:fake-cv-project",
                        "commit": self.adapter_commit,
                        "relative_path": "workflow_adapter.py",
                    }
                ],
                "capabilities": {"workflow_adapter": True},
            }
        )
        (self.control / "adapter.json").write_text(
            json.dumps(adapter, ensure_ascii=False), encoding="utf-8"
        )
        # 本文件回放 v1 的未绑定执行生命周期。当前正式入口要求 evidence
        # 必须绑定 Codebase；这里只在旧测试进程内关闭新门禁，生产 CLI
        # 的拒绝行为由本文件的 CLI 用例和 formal gate 专项验证。
        legacy_validator = evidence._validate_transition_semantics

        def replay_legacy_transition(*args: object, **kwargs: object) -> object:
            kwargs["live_write"] = False
            return legacy_validator(*args, **kwargs)

        patchers = (
            mock.patch.object(
                engine,
                "_require_bound_task_for_new_evidence",
            ),
            mock.patch.object(
                runs,
                "_require_bound_for_new_evidence",
            ),
            mock.patch.object(
                evidence,
                "_validate_transition_semantics",
                side_effect=replay_legacy_transition,
            ),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.project), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def _commit_adapter(self, message: str = "update adapter") -> str:
        self._git("add", "workflow_adapter.py")
        self._git("commit", "-m", message)
        commit = self._git("rev-parse", "HEAD")
        adapter_path = self.control / "adapter.json"
        adapter = read_json(adapter_path)
        adapter["code_sources"][0]["commit"] = commit
        adapter_path.write_text(
            json.dumps(adapter, ensure_ascii=False), encoding="utf-8"
        )
        return commit

    def _install_adapter_source(self, source: str, message: str) -> str:
        (self.project / "workflow_adapter.py").write_text(
            source,
            encoding="utf-8",
        )
        return self._commit_adapter(message)

    def start_task(self, **inputs: object) -> dict[str, object]:
        max_runs = inputs.pop("max_runs", 1)
        stop_condition = inputs.pop(
            "stop_condition", {"max_failures": 1}
        )
        route_inputs: dict[str, object] = {
            "config": {"learning_rate": 0.001},
            "seed": 7,
            "debug_required": False,
            "changes_code_behavior": False,
            "code_verified": True,
        }
        route_inputs.update(inputs)
        return create_task(
            self.project,
            owner_request="调一个学习率候选",
            route="tune",
            target_refs=["baseline"],
            route_inputs=route_inputs,
            budget={"max_runs": max_runs},
            stop_condition=stop_condition,
        )

    def test_adapter_protocol_and_fixture_expose_exactly_five_actions(self) -> None:
        expected = {
            "inspect",
            "validate",
            "prepare_runs",
            "execute",
            "parse_result",
        }
        protocol_actions = {
            name
            for name, value in vars(adapters.ProjectAdapter).items()
            if not name.startswith("_") and callable(value)
        }
        self.assertEqual(expected, protocol_actions)

        loaded = adapters.load_project_adapter(self.project)
        module_actions = {
            name
            for name in dir(loaded)
            if not name.startswith("_") and callable(getattr(loaded, name))
        }
        self.assertEqual(expected, module_actions)
        summary = adapters.adapter_summary(loaded)
        self.assertRegex(summary["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            hashlib.sha256(FIXTURE_ADAPTER.read_bytes()).hexdigest(),
            summary["sha256"],
        )

    def test_bound_adapter_private_dataclass_survives_repeated_real_loads(
        self,
    ) -> None:
        import dataclasses

        source = """from dataclasses import dataclass as _dataclass

@_dataclass(frozen=True)
class _PrivateState:
    value: int

def inspect(project):
    return {'standard': {'debug_required': False}, 'private_value': _PrivateState(7).value}

def validate(project):
    return []

def prepare_runs(project, task):
    return [{}]

def execute(project, action, run):
    return {'status': 'finished'}

def parse_result(project, run):
    return {}
"""
        self._install_adapter_source(source, "private dataclass adapter")
        digest = hashlib.sha256(
            (self.project / "workflow_adapter.py").read_bytes()
        ).hexdigest()
        module_name = f"_cv_workflow_adapter_{digest}"
        previous = sys.modules.pop(module_name, None)

        def restore() -> None:
            sys.modules.pop(module_name, None)
            if previous is not None:
                sys.modules[module_name] = previous

        self.addCleanup(restore)
        first = adapters.load_project_adapter(self.project)
        first_module = sys.modules[module_name]
        private_state = first._inspect.__globals__["_PrivateState"]
        instance = private_state(11)
        self.assertTrue(dataclasses.is_dataclass(private_state))
        self.assertEqual(7, first.inspect(self.project)["private_value"])

        second = adapters.load_project_adapter(self.project)
        second_module = sys.modules[module_name]

        self.assertIsNot(first_module, second_module)
        self.assertIs(second._inspect.__globals__, vars(second_module))
        self.assertTrue(
            dataclasses.is_dataclass(
                second._inspect.__globals__["_PrivateState"]
            )
        )
        self.assertEqual(private_state(11), instance)
        self.assertEqual(7, first.inspect(self.project)["private_value"])
        self.assertEqual(7, second.inspect(self.project)["private_value"])
        self.assertEqual(
            {
                "inspect",
                "validate",
                "prepare_runs",
                "execute",
                "parse_result",
            },
            {
                name
                for name in dir(second)
                if not name.startswith("_")
                and callable(getattr(second, name))
            },
        )

    def test_adapter_module_registration_is_restored_after_load_failures(
        self,
    ) -> None:
        cases = (
            (
                "exec",
                """from dataclasses import dataclass as _dataclass
@_dataclass
class _PrivateState:
    value: int
raise RuntimeError('boom')
""",
                None,
                "加载失败",
            ),
            (
                "surface",
                """import sys as _sys
if vars(_sys.modules[__name__]) is not globals():
    raise RuntimeError('module was not registered before exec')
def inspect(project): return {}
def validate(project): return []
def prepare_runs(project, task): return [{}]
def execute(project, action, run): return {}
def parse_result(project, run): return {}
def extra(): return None
""",
                ModuleType("_preexisting_adapter_sentinel"),
                "五个接口",
            ),
        )
        for name, source, sentinel, expected in cases:
            with self.subTest(name=name):
                self._install_adapter_source(source, f"{name} failure adapter")
                digest = hashlib.sha256(
                    (self.project / "workflow_adapter.py").read_bytes()
                ).hexdigest()
                module_name = f"_cv_workflow_adapter_{digest}"
                original = sys.modules.pop(module_name, None)
                if sentinel is not None:
                    sys.modules[module_name] = sentinel
                try:
                    with self.assertRaisesRegex(ValueError, expected):
                        adapters.load_project_adapter(self.project)
                    if sentinel is None:
                        self.assertNotIn(module_name, sys.modules)
                    else:
                        self.assertIs(sentinel, sys.modules[module_name])
                finally:
                    sys.modules.pop(module_name, None)
                    if original is not None:
                        sys.modules[module_name] = original

        interrupt_source = "raise KeyboardInterrupt('stop loading')\n"
        self._install_adapter_source(
            interrupt_source,
            "interrupted adapter",
        )
        interrupt_digest = hashlib.sha256(
            (self.project / "workflow_adapter.py").read_bytes()
        ).hexdigest()
        interrupt_module = f"_cv_workflow_adapter_{interrupt_digest}"
        original = sys.modules.pop(interrupt_module, None)
        try:
            with self.assertRaises(KeyboardInterrupt):
                adapters.load_project_adapter(self.project)
            self.assertNotIn(interrupt_module, sys.modules)
        finally:
            sys.modules.pop(interrupt_module, None)
            if original is not None:
                sys.modules[interrupt_module] = original

    def test_core_contains_no_project_specific_terms(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in CORE.glob("*.py")
        ).lower()
        banned = [
            "gt" + "pj",
            "/".join(("h", "u", "s", "zs")),
            "dynamic" + "-routing",
        ]
        for term in banned:
            self.assertNotIn(term, combined)

    def test_routes_and_runner_have_only_the_locked_public_actions(self) -> None:
        route_actions = {
            name
            for name, value in vars(routes).items()
            if not name.startswith("_") and inspect.isfunction(value)
        }
        runner_actions = {
            name
            for name, value in vars(runner).items()
            if not name.startswith("_") and inspect.isfunction(value)
        }
        self.assertEqual(
            {"requires", "build_variants", "compare", "mutation_class"},
            route_actions,
        )
        self.assertEqual({"start", "status", "stop", "collect"}, runner_actions)

    def test_innovation_comparison_requires_finite_flat_primary_metric(self) -> None:
        task = {
            "route": "innovation",
            "route_inputs": {
                "primary_metric": "score",
                "baseline": 0.70,
                "changes_code_behavior": False,
            },
        }
        self.assertEqual("innovation", routes.mutation_class("innovation", task))
        parsed = {"result": {"metrics": {"score": 0.75}}}
        comparison = routes.compare("innovation", task, parsed)
        self.assertEqual("score", comparison["primary_metric"])
        self.assertEqual(0.70, comparison["baseline"])
        self.assertEqual(0.75, comparison["candidate"])
        self.assertEqual("side_by_side_only", comparison["comparison_scope"])
        self.assertIsNone(comparison["delta"])
        self.assertTrue(comparison["blockers"])
        for metrics in ({}, {"score": float("inf")}, {"nested": {"score": 0.75}}):
            with self.subTest(metrics=metrics):
                with self.assertRaisesRegex(ValueError, "finite|有限"):
                    routes.compare(
                        "innovation", task, {"result": {"metrics": metrics}}
                    )

    def test_temporary_roles_are_minimal_and_close_after_review(self) -> None:
        normal = self.start_task()
        changed = self.start_task(changes_code_behavior=True)
        self.assertEqual(
            ["Runner", "Analyst"], review.roles_for_task(normal)
        )
        self.assertEqual(
            ["Researcher", "Implementer", "Reviewer", "Runner", "Analyst"],
            review.roles_for_task(changed),
        )
        normal_session = review.open_session(normal, {"id": "RUN-0001"})
        changed_session = review.open_session(changed, {"id": "RUN-0002"})
        self.assertEqual(
            ["Coordinator", "Runner", "Analyst"],
            [item["role"] for item in normal_session],
        )
        self.assertEqual(
            [
                "Coordinator", "Researcher", "Implementer", "Reviewer",
                "Runner", "Analyst",
            ],
            [item["role"] for item in changed_session],
        )
        review.close_session(normal_session)
        review.close_session(changed_session)
        self.assertTrue(all(item["closed"] for item in normal_session))
        self.assertTrue(all(item["closed"] for item in changed_session))
        self.assertEqual("", engine._evidence_actors(normal_session)["checked_by"])
        self.assertIn(
            ":Reviewer:", engine._evidence_actors(changed_session)["checked_by"]
        )

    def test_cli_rejects_unbound_evidence_loop(self) -> None:
        task = cli_json(
            "start-task",
            "--project",
            self.project,
            "--request",
            "调一个学习率候选",
            "--target-ref",
            "baseline",
            "--config",
            '{"learning_rate":0.001}',
            "--budget",
            '{"max_runs":1}',
            "--stop-condition",
            '{"max_failures":1}',
            "--debug-required",
            "false",
        )
        self.assertEqual("tune", task["route"])
        self.assertFalse(task["route_inputs"]["debug_required"])

        with self.assertRaisesRegex(AssertionError, "Codebase|绑定"):
            cli_json(
                "run-task",
                "--project",
                self.project,
                "--task",
                task["id"],
                "--purpose",
                "evidence",
                "--backend",
                "project",
            )
        final_task = load_task(self.project, task["id"])
        self.assertEqual("queued", final_task["stage"])
        self.assertEqual([], final_task["run_refs"])

    def test_engine_uses_the_single_fixed_order(self) -> None:
        task = self.start_task()
        calls: list[str] = []

        def track(name: str, function: object) -> object:
            def wrapped(*args: object, **kwargs: object) -> object:
                calls.append(name)
                return function(*args, **kwargs)  # type: ignore[operator]

            return wrapped

        names = (
            "build_variants",
            "claim_run",
            "open_session",
            "runner_start",
            "runner_collect",
            "review_parsed_result",
            "record_evidence_transition",
            "close_run",
            "_finish_task",
            "close_session",
        )
        patches = [
            mock.patch.object(
                engine, name, side_effect=track(name, getattr(engine, name))
            )
            for name in names
        ]
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            engine.execute_task(self.project, task["id"], purpose="evidence")
        self.assertEqual(list(names), calls)

    def test_readiness_failure_creates_no_run_and_marks_innovation_redirect(self) -> None:
        task = self.start_task(changes_code_behavior=True, code_verified=False)
        outcome = engine.execute_task(self.project, task["id"])
        self.assertEqual("preparing", outcome["task_stage"])
        self.assertEqual("innovation", outcome["route_redirect"])
        self.assertTrue(outcome["requires_code_review"])
        self.assertEqual([], load_task(self.project, task["id"])["run_refs"])
        self.assertEqual([], list((self.control / "runs").iterdir()))

    def test_synthetic_debug_success_exposes_optional_dependency_warning(self) -> None:
        task = self.start_task(config={"mode": "synthetic_debug"})
        loaded = adapters.load_project_adapter(self.project)
        warning = "OPTIONAL_NOT_INSTALLED：fixture-extra"
        warning_adapter = replace(loaded, _validate=lambda project: [warning])

        with mock.patch.object(
            engine,
            "load_project_adapter",
            return_value=warning_adapter,
        ):
            result = engine.execute_task(
                self.project,
                task["id"],
                purpose="debug",
            )

        self.assertEqual([warning], result["warnings"])
        self.assertEqual("succeeded", result["run"]["execution"]["outcome"])

    def test_formal_mode_keeps_optional_dependency_issue_blocking(self) -> None:
        task = self.start_task()
        loaded = adapters.load_project_adapter(self.project)
        issue = "OPTIONAL_NOT_INSTALLED：fixture-extra"
        issue_adapter = replace(loaded, _validate=lambda project: [issue])

        gate = engine._task_gate(
            self.project,
            self.project,
            task,
            issue_adapter,
        )

        self.assertEqual("fail", gate["status"])
        self.assertIn(issue, gate["reasons"])
        self.assertEqual([], gate["warnings"])

    def test_failed_and_stopped_runs_land_on_recoverable_task_stages(self) -> None:
        cases = (
            ("failed", "implementation", "preparing"),
            ("failed", "environment", "ready"),
            ("stopped", "user_stop", "stopped"),
        )
        for outcome, issue_kind, task_stage in cases:
            with self.subTest(outcome=outcome, issue_kind=issue_kind):
                task = self.start_task(
                    config={
                        "fixture_outcome": outcome,
                        "fixture_issue_kind": issue_kind,
                    }
                )
                result = engine.execute_task(self.project, task["id"])
                run = load_run(self.project, result["run"]["id"])
                self.assertEqual(
                    task_stage, load_task(self.project, task["id"])["stage"]
                )
                self.assertEqual("closed", run["execution"]["stage"])
                self.assertEqual(outcome, run["execution"]["outcome"])
                self.assertEqual(issue_kind, run["execution"]["issue_kind"])
                self.assertEqual("not_evaluated", run["analysis"]["hypothesis"])
                self.assertEqual(
                    "disqualified", current_evidence_level(self.project, run["id"])
                )
                self.assertTrue(all(item["closed"] for item in result["session"]))

    def test_success_with_invalid_quality_does_not_finish_task(self) -> None:
        task = self.start_task(
            config={
                "fixture_issue_kind": "prerequisite",
                "fixture_interface": "invalid",
            }
        )
        result = engine.execute_task(self.project, task["id"])
        run = load_run(self.project, result["run"]["id"])
        self.assertEqual("preparing", load_task(self.project, task["id"])["stage"])
        self.assertEqual("not_evaluated", run["analysis"]["hypothesis"])
        self.assertEqual(
            "disqualified", current_evidence_level(self.project, run["id"])
        )

    def test_debug_then_evidence_uses_two_closed_runs_and_new_agent_ids(self) -> None:
        (self.project / ".fake-debug-required").write_text(
            "required", encoding="utf-8"
        )
        self._git("add", ".fake-debug-required")
        self._git("commit", "-m", "require debug")
        task = self.start_task(debug_required=True, max_runs=2)

        debug_result = engine.execute_task(
            self.project, task["id"], purpose="debug"
        )
        after_debug = load_task(self.project, task["id"])
        debug_run = load_run(self.project, debug_result["run"]["id"])
        self.assertEqual("preparing", after_debug["stage"])
        self.assertEqual([debug_run["id"]], after_debug["run_refs"])
        self.assertEqual("closed", debug_run["execution"]["stage"])
        self.assertEqual("debug", current_evidence_level(self.project, debug_run["id"]))
        self.assertEqual("debug_complete", debug_result["evidence"]["reason"])

        evidence_result = engine.execute_task(
            self.project, task["id"], purpose="evidence"
        )
        final_task = load_task(self.project, task["id"])
        evidence_run = load_run(self.project, evidence_result["run"]["id"])
        self.assertEqual("done", final_task["stage"])
        self.assertEqual([debug_run["id"], evidence_run["id"]], final_task["run_refs"])
        self.assertNotEqual(debug_run["id"], evidence_run["id"])
        self.assertEqual("closed", load_run(self.project, debug_run["id"])["execution"]["stage"])
        self.assertEqual("closed", evidence_run["execution"]["stage"])
        self.assertEqual(
            "single_run", current_evidence_level(self.project, evidence_run["id"])
        )

        debug_ids = {item["id"] for item in debug_result["session"]}
        evidence_ids = {item["id"] for item in evidence_result["session"]}
        self.assertTrue(debug_ids.isdisjoint(evidence_ids))
        for result in (debug_result, evidence_result):
            self.assertTrue(all(item["closed"] for item in result["session"]))
            event = result["evidence"]
            self.assertIn(":Analyst:", event["proposed_by"])
            self.assertEqual("", event["checked_by"])
            self.assertIn(":Coordinator:", event["applied_by"])
            self.assertNotIn(event["proposed_by"], review.ROLE_DESCRIPTIONS)
            self.assertNotIn(event["applied_by"], review.ROLE_DESCRIPTIONS)

    def test_exhausted_debug_budget_rejects_second_run_without_mutation(
        self,
    ) -> None:
        (self.project / ".fake-debug-required").write_text(
            "required", encoding="utf-8"
        )
        self._git("add", ".fake-debug-required")
        self._git("commit", "-m", "require one debug")
        task = self.start_task(debug_required=True, max_runs=1)

        first = engine.execute_task(
            self.project, task["id"], purpose="debug"
        )
        before = load_task(self.project, task["id"])

        with self.assertRaisesRegex(ValueError, "运行预算已用完"):
            engine.execute_task(
                self.project, task["id"], purpose="evidence"
            )

        after = load_task(self.project, task["id"])
        self.assertEqual(before, after)
        self.assertEqual([first["run"]["id"]], after["run_refs"])
        self.assertEqual("preparing", after["stage"])

    def test_single_debug_stop_condition_closes_task_with_scoped_conclusion(
        self,
    ) -> None:
        (self.project / ".fake-debug-required").write_text(
            "required", encoding="utf-8"
        )
        self._git("add", ".fake-debug-required")
        self._git("commit", "-m", "require scoped debug")
        task = self.start_task(
            debug_required=True,
            max_runs=1,
            stop_condition={"type": "single_debug", "after_runs": 1},
        )

        result = engine.execute_task(
            self.project, task["id"], purpose="debug"
        )

        closed = load_task(self.project, task["id"])
        self.assertEqual("done", closed["stage"])
        self.assertEqual(
            {
                "scope": "debug_only",
                "reason": "stop_condition_met",
                "run_id": result["run"]["id"],
                "evidence_level": "debug",
                "hypothesis": "inconclusive",
                "comparison": result["comparison"],
            },
            closed["conclusion"],
        )

    def test_single_debug_recovers_old_closed_run_before_budget_rejection(
        self,
    ) -> None:
        (self.project / ".fake-debug-required").write_text(
            "required", encoding="utf-8"
        )
        self._git("add", ".fake-debug-required")
        self._git("commit", "-m", "require recoverable scoped debug")
        task = self.start_task(
            debug_required=True,
            max_runs=1,
            stop_condition={"type": "single_debug", "after_runs": 1},
        )

        def strand_in_preparing(
            project,
            task_id,
            run,
            evidence_level,
            decision,
            comparison,
        ):
            return transition_task(
                project,
                task_id,
                "preparing",
                issue_kind="implementation",
            )

        with mock.patch.object(
            engine, "_finish_task", side_effect=strand_in_preparing
        ):
            first = engine.execute_task(
                self.project, task["id"], purpose="debug"
            )
        self.assertEqual(
            "preparing", load_task(self.project, task["id"])["stage"]
        )

        recovered = engine.execute_task(
            self.project, task["id"], purpose="debug"
        )

        self.assertEqual("recovered_complete", recovered["status"])
        self.assertEqual("done", recovered["task"]["stage"])
        self.assertEqual(
            [first["run"]["id"]], recovered["task"]["run_refs"]
        )
        self.assertEqual(
            "debug_only", recovered["task"]["conclusion"]["scope"]
        )
        validate_project_workflow(self.project)

    def test_agent_session_closes_when_start_failure_is_cleaned(self) -> None:
        task = self.start_task()
        captured: list[list[dict[str, object]]] = []
        original = review.open_session

        def capture(task_value: object, run_value: object) -> object:
            session = original(task_value, run_value)  # type: ignore[arg-type]
            captured.append(session)
            return session

        with (
            mock.patch.object(engine, "open_session", side_effect=capture),
            mock.patch.object(engine, "runner_start", side_effect=RuntimeError("boom")),
            mock.patch.object(
                engine,
                "runner_status",
                side_effect=AssertionError("unsafe status fallback"),
            ) as status,
        ):
            result = engine.execute_task(self.project, task["id"])
        status.assert_not_called()
        self.assertEqual("failed", result["run"]["execution"]["outcome"])
        self.assertEqual(
            "environment",
            result["run"]["execution"]["issue_kind"],
        )
        self.assertEqual(1, len(captured))
        self.assertTrue(all(item["closed"] for item in captured[0]))

    def test_non_writers_do_not_import_or_call_ledger_apis(self) -> None:
        for module in (adapters, routes, runner, review):
            source = inspect.getsource(module)
            for forbidden in (
                "from .tasking",
                "from .evidence",
                "create_task(",
                "create_run(",
                "record_evidence_transition(",
                "transition_task(",
            ):
                self.assertNotIn(forbidden, source, module.__name__)

    def test_invalid_action_purpose_and_backend_are_rejected(self) -> None:
        task = self.start_task()
        loaded = adapters.load_project_adapter(self.project)
        frozen = routes.build_variants("tune", loaded, self.project, task)[0]
        run = {"id": "RUN-0001", "purpose": "evidence", "frozen": frozen}
        with self.assertRaises(ValueError):
            runner._execute(self.project, loaded, "launch", run, backend="project")
        with self.assertRaises(ValueError):
            runner.start(self.project, loaded, run, purpose="formal", backend="project")
        with self.assertRaises(ValueError):
            runner.start(self.project, loaded, run, purpose="evidence", backend="remote")
        with self.assertRaises(NotImplementedError):
            runner.start(self.project, loaded, run, purpose="evidence", backend="local")

    def test_unbound_and_unsafe_adapter_sources_are_rejected(self) -> None:
        adapter_path = self.control / "adapter.json"
        adapter = read_json(adapter_path)
        adapter["status"] = "unbound"
        adapter_path.write_text(json.dumps(adapter), encoding="utf-8")
        with self.assertRaises(ValueError):
            adapters.load_project_adapter(self.project)

    def test_bound_adapter_requires_exact_current_git_ancestry_and_bytes(self) -> None:
        adapter_path = self.control / "adapter.json"
        original = read_json(adapter_path)

        forged = json.loads(json.dumps(original))
        forged["code_sources"][0]["commit"] = "a" * 40
        adapter_path.write_text(json.dumps(forged), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Git|commit"):
            adapters.load_project_adapter(self.project)

        uppercase = json.loads(json.dumps(original))
        uppercase["code_sources"][0]["commit"] = self.adapter_commit.upper()
        adapter_path.write_text(json.dumps(uppercase), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "commit"):
            adapters.load_project_adapter(self.project)

        orphan = self._git(
            "commit-tree", f"{self.adapter_commit}^{{tree}}", "-m", "orphan"
        )
        non_ancestor = json.loads(json.dumps(original))
        non_ancestor["code_sources"][0]["commit"] = orphan
        adapter_path.write_text(json.dumps(non_ancestor), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "祖先"):
            adapters.load_project_adapter(self.project)

        adapter_path.write_text(json.dumps(original), encoding="utf-8")
        (self.project / "workflow_adapter.py").write_text(
            "raise RuntimeError('tampered')\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "一致|工作树"):
            adapters.load_project_adapter(self.project)

    def test_bound_adapter_rejects_git_symlink_blob_and_non_ledger_dirt(self) -> None:
        target = self.project / "adapter-target.txt"
        target.write_text("workflow_adapter.py", encoding="utf-8")
        blob = self._git("hash-object", "-w", "adapter-target.txt")
        self._git(
            "update-index",
            "--add",
            "--cacheinfo",
            f"120000,{blob},workflow_adapter.py",
        )
        self._git("commit", "-m", "symlink adapter blob")
        symlink_commit = self._git("rev-parse", "HEAD")
        adapter_path = self.control / "adapter.json"
        adapter = read_json(adapter_path)
        adapter["code_sources"][0]["commit"] = symlink_commit
        adapter_path.write_text(json.dumps(adapter), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "100644|100755"):
            adapters.load_project_adapter(self.project)

    def test_bound_adapter_allows_only_ledger_worktree_changes(self) -> None:
        # setUp 绑定 adapter.json 后，工作树本来就只有账本变化，应当允许。
        loaded = adapters.load_project_adapter(self.project)
        self.assertEqual(self.adapter_commit, adapters.adapter_summary(loaded)["commit"])

        (self.project / "untracked-code.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "工作树"):
            adapters.load_project_adapter(self.project)

    def test_bound_adapter_rejects_dirty_tracked_code_outside_ledger(self) -> None:
        (self.project / "workflow_adapter.py").write_text(
            FIXTURE_ADAPTER.read_text(encoding="utf-8") + "\n# dirty\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "一致|工作树"):
            adapters.load_project_adapter(self.project)

    def test_git_process_collection_is_bounded_and_reaps_children(self) -> None:
        small = policy._run_bounded_process(
            [sys.executable, "-c", "print('small-output')"],
            label="small",
            timeout_seconds=2,
        )
        self.assertEqual(b"small-output", small.stdout.strip())
        self.assertEqual(b"", small.stderr)

        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                script = (
                    "import sys;"
                    f"sys.{stream}.buffer.write("
                    f"b'x'*({policy.GIT_OUTPUT_LIMIT}+1));"
                    f"sys.{stream}.flush()"
                )
                with self.assertRaisesRegex(ValueError, "超过大小限制"):
                    policy._run_bounded_process(
                        [sys.executable, "-c", script],
                        label=f"large-{stream}",
                        timeout_seconds=2,
                    )

        with self.assertRaisesRegex(ValueError, "超时"):
            policy._run_bounded_process(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                label="timeout",
                timeout_seconds=0.05,
            )
        with self.assertRaisesRegex(ValueError, "exit 7"):
            policy._run_bounded_process(
                [sys.executable, "-c", "raise SystemExit(7)"],
                label="nonzero",
                timeout_seconds=2,
            )

    def test_git_text_rejects_invalid_utf8_after_bounded_collection(self) -> None:
        completed = policy._run_bounded_process(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(bytes([255]))",
            ],
            label="invalid-utf8",
            timeout_seconds=2,
        )
        with mock.patch.object(policy, "_git_process", return_value=completed):
            with self.assertRaisesRegex(ValueError, "严格 UTF-8"):
                policy._git_text(self.project, "status", label="invalid")

    def test_bounded_process_kills_spawned_children_on_every_abnormal_exit(
        self,
    ) -> None:
        for mode in ("timeout", "stdout", "stderr"):
            with self.subTest(mode=mode):
                pid_file = Path(self.temporary.name) / f"{mode}-pids.txt"
                script = (
                    "import os, pathlib, subprocess, sys, time;"
                    "child=subprocess.Popen("
                    "[sys.executable,'-c','import time; time.sleep(30)']);"
                    f"pathlib.Path({str(pid_file)!r}).write_text("
                    "f'{os.getpid()},{child.pid}',encoding='utf-8');"
                )
                if mode == "timeout":
                    script += "time.sleep(30)"
                    timeout = 0.5
                else:
                    script += (
                        f"sys.{mode}.buffer.write("
                        f"b'x'*({policy.GIT_OUTPUT_LIMIT}+65536));"
                        f"sys.{mode}.flush();time.sleep(30)"
                    )
                    timeout = 5
                pids: list[int] = []
                try:
                    expected = "超时" if mode == "timeout" else "超过大小限制"
                    with self.assertRaisesRegex(ValueError, expected):
                        policy._run_bounded_process(
                            [sys.executable, "-c", script],
                            label=f"tree-{mode}",
                            timeout_seconds=timeout,
                        )
                    self.assertTrue(pid_file.is_file())
                    pids = [
                        int(item)
                        for item in pid_file.read_text(encoding="utf-8").split(",")
                    ]
                    deadline = time.monotonic() + 3
                    while any(_process_exists(pid) for pid in pids):
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.05)
                    self.assertEqual(
                        [],
                        [pid for pid in pids if _process_exists(pid)],
                        f"{mode} 后仍有父/子进程存活",
                    )
                finally:
                    for pid in reversed(pids):
                        _force_kill(pid)

    def test_bounded_process_closes_tree_when_parent_exits_with_inherited_pipes(
        self,
    ) -> None:
        pid_file = Path(self.temporary.name) / "parent-exit-pids.txt"
        script = (
            "import os, pathlib, subprocess, sys;"
            "child=subprocess.Popen("
            "[sys.executable,'-c','import time; time.sleep(30)']);"
            f"pathlib.Path({str(pid_file)!r}).write_text("
            "f'{os.getpid()},{child.pid}',encoding='utf-8');"
            "print('parent-done',flush=True)"
        )
        pids: list[int] = []
        started = time.monotonic()
        try:
            completed = policy._run_bounded_process(
                [sys.executable, "-c", script],
                label="parent-exit",
                timeout_seconds=2,
            )
            self.assertEqual(b"parent-done", completed.stdout.strip())
            self.assertTrue(pid_file.is_file())
            pids = [
                int(item)
                for item in pid_file.read_text(encoding="utf-8").split(",")
            ]
            deadline = started + 2
            while any(_process_exists(pid) for pid in pids):
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.assertEqual([], [pid for pid in pids if _process_exists(pid)])
            self.assertLess(
                time.monotonic() - started,
                2.5,
                "父进程退出后，清理子进程和管道不能另加固定等待时间",
            )
        finally:
            for pid in reversed(pids):
                _force_kill(pid)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object 专用测试")
    def test_job_setup_failures_never_release_a_suspended_process(self) -> None:
        class FailingJob:
            def __init__(self, failure: str) -> None:
                self.failure = failure
                self.closed = False
                self.process: subprocess.Popen[bytes] | None = None

            def assign(self, process: subprocess.Popen[bytes]) -> None:
                self.process = process
                if self.failure == "assign":
                    raise OSError("assign failed")

            def resume(self, process: subprocess.Popen[bytes]) -> None:
                self.process = process
                raise OSError("resume failed")

            def close(self, deadline: float | None = None) -> None:
                self.closed = True

        for failure in ("assign", "resume"):
            with self.subTest(failure=failure):
                job = FailingJob(failure)
                with (
                    mock.patch.object(
                        policy, "_create_windows_job", return_value=job
                    ),
                    self.assertRaisesRegex(ValueError, "进程隔离失败"),
                ):
                    policy._start_bounded_process(
                        [
                            sys.executable,
                            "-c",
                            "import time; time.sleep(30)",
                        ],
                        label=f"job-{failure}",
                        deadline=time.monotonic() + 2,
                    )
                self.assertTrue(job.closed)
                self.assertIsNotNone(job.process)
                self.assertIsNotNone(job.process.poll())
                self.assertTrue(job.process.stdout.closed)
                self.assertTrue(job.process.stderr.closed)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object 专用测试")
    def test_job_close_failure_still_kills_and_reaps_parent(self) -> None:
        class FailingCloseJob:
            def close(self, deadline: float | None = None) -> None:
                raise ValueError("job close failed")

        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            with self.assertRaisesRegex(ValueError, "job close failed"):
                policy._terminate_process_tree(
                    process,
                    deadline=time.monotonic() + 2,
                    windows_job=FailingCloseJob(),
                )
            self.assertIsNotNone(
                process.poll(),
                "Job 关闭异常时也必须终止并回收已经启动的父进程",
            )
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def test_bound_adapter_rejects_ignored_executables_but_allows_run_data_cache(
        self,
    ) -> None:
        gitignore = self.project / ".gitignore"
        existing = (
            gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
        )
        ignored_names = (
            "ignored-helper.py",
            "ignored-script.cmd",
            "ignored-policy.PS1",
            "ignored-tool.EXE",
            "ignored-shell.SH",
        )
        gitignore.write_text(
            existing
            + "\n"
            + "".join(f"/{name}\n" for name in ignored_names)
            + "/runs/\n/data/\n",
            encoding="utf-8",
        )
        self._git("add", ".gitignore")
        self._git("commit", "-m", "ignore local outputs")
        (self.project / "runs").mkdir()
        (self.project / "runs" / "metrics.JSON").write_text(
            '{"score": 1}', encoding="utf-8"
        )
        (self.project / "data").mkdir()
        (self.project / "data" / "model.PT").write_bytes(b"model-cache")
        adapters.load_project_adapter(self.project)

        for name in ignored_names:
            with self.subTest(name=name):
                path = self.project / name
                path.write_text("untracked executable code\n", encoding="utf-8")
                try:
                    with self.assertRaisesRegex(ValueError, "ignored|忽略|代码"):
                        adapters.load_project_adapter(self.project)
                finally:
                    path.unlink()

    def test_loaded_adapter_owns_an_immutable_summary(self) -> None:
        loaded = adapters.load_project_adapter(self.project)
        expected = adapters.adapter_summary(loaded)
        adapter_globals = loaded._inspect.__globals__  # type: ignore[attr-defined]
        self.assertNotIn("_workflow_summary", adapter_globals)
        adapter_globals["_workflow_summary"] = {
            "sha256": "0" * 64,
            "repo_url": "forged",
            "commit": "0" * 40,
            "source": "forged.py",
        }
        self.addCleanup(
            adapter_globals.pop, "_workflow_summary", None
        )
        first = adapters.adapter_summary(loaded)
        first["sha256"] = "0" * 64
        self.assertEqual(expected, adapters.adapter_summary(loaded))
        with self.assertRaises((AttributeError, TypeError)):
            loaded._summary = ()  # type: ignore[attr-defined]

    def test_adapter_boundaries_copy_inputs_outputs_and_reject_nan(self) -> None:
        source = self.project / "workflow_adapter.py"
        source.write_text(
            """def inspect(project):
    return {'standard': {'debug_required': False}}
def validate(project):
    return []
def prepare_runs(project, task):
    task['route_inputs']['baseline'] = 999
    shared = {'x': 1}
    return [{'code': shared, 'config': {}, 'seed': 1, 'data': shared, 'environment': {}}]
def execute(project, action, run):
    run['frozen']['config']['changed'] = True
    return {'status': 'finished', 'process_id': 7}
def parse_result(project, run):
    run['frozen']['config']['parsed'] = True
    return {'bad': float('nan')}
""",
            encoding="utf-8",
        )
        self._commit_adapter()
        loaded = adapters.load_project_adapter(self.project)
        task = self.start_task(baseline=0.7)
        variants = loaded.prepare_runs(self.project, task)
        self.assertEqual(0.7, task["route_inputs"]["baseline"])
        variants[0]["code"]["x"] = 2
        self.assertEqual(1, variants[0]["data"]["x"])
        run = {"purpose": "evidence", "frozen": variants[0]}
        response = loaded.execute(self.project, "start", run)
        self.assertNotIn("changed", run["frozen"]["config"])
        response["status"] = "forged"
        self.assertNotIn("parsed", run["frozen"]["config"])
        with self.assertRaisesRegex(ValueError, "有限 JSON"):
            loaded.parse_result(self.project, run)

    def test_ready_task_rechecks_code_and_adapter_standard_before_claim(self) -> None:
        for change in ("code", "standard"):
            with self.subTest(change=change):
                task = self.start_task()
                transition_task(self.project, task["id"], "preparing")
                self.assertEqual(
                    {"status": "pass"},
                    readiness(
                        self.project,
                        task["id"],
                        target_clear=True,
                        template_runnable=True,
                        mutation_allowed=True,
                        code_verified=True,
                    ),
                )
                if change == "code":
                    current = read_json(
                        self.control / "tasks" / f"{task['id']}.json"
                    )
                    current["route_inputs"]["code_verified"] = False
                    (self.control / "tasks" / f"{task['id']}.json").write_text(
                        json.dumps(current, ensure_ascii=False), encoding="utf-8"
                    )
                else:
                    (self.project / ".fake-debug-required").write_text(
                        "required", encoding="utf-8"
                    )
                    self._git("add", ".fake-debug-required")
                    self._git("commit", "-m", "change project standard")
                result = engine.execute_task(self.project, task["id"])
                self.assertEqual("ready", result["task_stage"])
                self.assertEqual([], load_task(self.project, task["id"])["run_refs"])
                if change == "standard":
                    # 临时仓库随测试销毁，不再制造删除后的脏工作树。
                    pass

    def test_concurrent_execute_claims_only_one_run(self) -> None:
        task = self.start_task()
        transition_task(self.project, task["id"], "preparing")
        readiness(
            self.project,
            task["id"],
            target_clear=True,
            template_runnable=True,
            mutation_allowed=True,
            code_verified=True,
        )
        barrier = threading.Barrier(2)
        original = engine.claim_run
        results: list[object] = []

        def synchronized(*args: object, **kwargs: object) -> object:
            barrier.wait(timeout=10)
            return original(*args, **kwargs)  # type: ignore[arg-type]

        def invoke() -> None:
            try:
                results.append(engine.execute_task(self.project, task["id"]))
            except BaseException as error:
                results.append(error)

        with mock.patch.object(engine, "claim_run", side_effect=synchronized):
            threads = [threading.Thread(target=invoke) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(1, sum(isinstance(item, dict) for item in results))
        self.assertEqual(1, sum(isinstance(item, ValueError) for item in results))
        self.assertEqual(1, len(load_task(self.project, task["id"])["run_refs"]))
        self.assertEqual(1, len(list((self.control / "runs").iterdir())))

    @unittest.skipUnless(os.name == "nt", "Junction 竞态只在 Windows 验证")
    def test_adapter_parent_junction_swap_is_rejected_before_execution(self) -> None:
        safe = self.project / "adapter-dir"
        safe.mkdir()
        shutil.move(str(self.project / "workflow_adapter.py"), safe / "adapter.py")
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "adapter.py").write_text(
            "raise RuntimeError('外部源码不应执行')\n", encoding="utf-8"
        )
        adapter_path = self.control / "adapter.json"
        spec = read_json(adapter_path)
        spec["code_sources"][0]["relative_path"] = "adapter-dir/adapter.py"
        adapter_path.write_text(json.dumps(spec), encoding="utf-8")
        original_read = policy._read_bounded_regular_file
        moved = self.project / "adapter-dir-safe"

        def swap(path: Path, limit: int, label: str) -> bytes:
            safe.rename(moved)
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(safe), str(outside)],
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest("当前系统不能创建 Junction")
            return original_read(path, limit, label)

        try:
            with mock.patch.object(
                policy, "_read_bounded_regular_file", side_effect=swap
            ):
                with self.assertRaises(ValueError):
                    adapters.load_project_adapter(self.project)
        finally:
            if safe.exists():
                subprocess.run(
                    ["cmd", "/c", "rmdir", str(safe)],
                    capture_output=True,
                    check=False,
                )
            if moved.exists() and not safe.exists():
                moved.rename(safe)

    def test_start_crash_after_external_start_stops_without_status_guess(self) -> None:
        task = self.start_task()
        sessions: list[list[dict[str, object]]] = []
        original_start = engine.runner_start
        original_open = engine.open_session
        starts = 0

        def start_then_crash(*args: object, **kwargs: object) -> object:
            nonlocal starts
            starts += 1
            original_start(*args, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError("start response lost")

        def capture(*args: object, **kwargs: object) -> object:
            session = original_open(*args, **kwargs)  # type: ignore[arg-type]
            sessions.append(session)
            return session

        with (
            mock.patch.object(engine, "open_session", side_effect=capture),
            mock.patch.object(engine, "runner_start", side_effect=start_then_crash),
            mock.patch.object(
                engine,
                "runner_status",
                side_effect=AssertionError("unsafe status fallback"),
            ) as status,
        ):
            result = engine.execute_task(self.project, task["id"])
        status.assert_not_called()
        run_id = result["run"]["id"]
        current = load_run(self.project, run_id)
        self.assertEqual("closed", current["execution"]["stage"])
        self.assertEqual("failed", current["execution"]["outcome"])
        self.assertEqual("environment", current["execution"]["issue_kind"])
        self.assertEqual(1, starts)
        self.assertTrue(all(item["closed"] for session in sessions for item in session))

    def test_collect_and_review_errors_close_bad_run_before_retry(self) -> None:
        for hook in ("runner_collect", "review_parsed_result"):
            with self.subTest(hook=hook):
                task = self.start_task(max_runs=2)
                with mock.patch.object(
                    engine, hook, side_effect=RuntimeError(f"{hook} boom")
                ):
                    first = engine.execute_task(self.project, task["id"])
                bad_run = load_run(self.project, first["run"]["id"])
                self.assertEqual("closed", bad_run["execution"]["stage"])
                self.assertEqual("failed", bad_run["execution"]["outcome"])
                self.assertEqual(
                    "implementation", bad_run["execution"]["issue_kind"]
                )
                self.assertEqual("not_evaluated", bad_run["analysis"]["hypothesis"])
                self.assertEqual(
                    "disqualified",
                    current_evidence_level(self.project, bad_run["id"]),
                )
                self.assertEqual("preparing", load_task(self.project, task["id"])["stage"])
                second = engine.execute_task(self.project, task["id"])
                self.assertEqual("done", second["task"]["stage"])
                self.assertEqual(
                    2, len(load_task(self.project, task["id"])["run_refs"])
                )

    def test_malformed_collected_values_are_closed_as_bad_run(self) -> None:
        task = self.start_task()
        malformed = {
            "execution": {
                "outcome": "mystery",
                "exit_code": 0,
                "issue_kind": None,
            },
            "result": {"metrics": {}, "raw_log": None},
            "quality": {
                "implementation": "valid",
                "interface": "valid",
                "data": "valid",
                "metrics": "made_up",
            },
            "analysis": {
                "hypothesis": "supported",
                "limitations": [],
                "suggestions": [],
            },
            "artifacts": [],
        }
        with mock.patch.object(engine, "runner_collect", return_value=malformed):
            result = engine.execute_task(self.project, task["id"])
        run = load_run(self.project, result["run"]["id"])
        self.assertEqual("closed", run["execution"]["stage"])
        self.assertEqual("failed", run["execution"]["outcome"])
        self.assertEqual("implementation", run["execution"]["issue_kind"])

    def test_persistence_crashes_resume_without_duplicate_evidence(self) -> None:
        for hook in ("finish_run", "record_evidence_transition", "close_run"):
            with self.subTest(hook=hook):
                task = self.start_task()
                before_events = len(
                    (self.control / "evidence.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
                original = getattr(engine, hook)
                raised = False
                sessions: list[list[dict[str, object]]] = []
                original_open = engine.open_session

                def persist_then_crash(*args: object, **kwargs: object) -> object:
                    nonlocal raised
                    result = original(*args, **kwargs)
                    if not raised:
                        raised = True
                        raise RuntimeError(f"{hook} crash")
                    return result

                def capture(*args: object, **kwargs: object) -> object:
                    session = original_open(*args, **kwargs)  # type: ignore[arg-type]
                    sessions.append(session)
                    return session

                with (
                    mock.patch.object(engine, "open_session", side_effect=capture),
                    mock.patch.object(engine, hook, side_effect=persist_then_crash),
                ):
                    with self.assertRaisesRegex(RuntimeError, f"{hook} crash"):
                        engine.execute_task(self.project, task["id"])
                result = engine.execute_task(self.project, task["id"])
                self.assertEqual("done", result["task"]["stage"])
                ledger = (self.control / "evidence.jsonl").read_text(encoding="utf-8")
                self.assertEqual(before_events + 1, len(ledger.splitlines()))
                self.assertEqual(1, len(load_task(self.project, task["id"])["run_refs"]))
                self.assertTrue(
                    all(item["closed"] for session in sessions for item in session)
                )

    def test_finish_task_crash_before_write_resumes_reviewing(self) -> None:
        task = self.start_task()
        original = engine._finish_task
        calls = 0

        def crash_once(*args: object, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("finish task crash")
            return original(*args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(engine, "_finish_task", side_effect=crash_once):
            with self.assertRaisesRegex(RuntimeError, "finish task crash"):
                engine.execute_task(self.project, task["id"])
        self.assertEqual("reviewing", load_task(self.project, task["id"])["stage"])
        result = engine.execute_task(self.project, task["id"])
        self.assertEqual("done", result["task"]["stage"])
        self.assertEqual(1, len((self.control / "evidence.jsonl").read_text().splitlines()))

    def test_start_not_started_is_closed_as_environment_failure(self) -> None:
        task = self.start_task()
        with (
            mock.patch.object(engine, "runner_start", side_effect=RuntimeError("lost")),
            mock.patch.object(
                engine, "runner_status", return_value={"status": "not_started"}
            ),
        ):
            first = engine.execute_task(self.project, task["id"])
        run = load_run(self.project, first["run"]["id"])
        self.assertEqual("closed", run["execution"]["stage"])
        self.assertEqual("failed", run["execution"]["outcome"])
        self.assertEqual("environment", run["execution"]["issue_kind"])
        self.assertEqual("ready", load_task(self.project, task["id"])["stage"])
        self.assertEqual("disqualified", current_evidence_level(self.project, run["id"]))

    def test_runner_action_responses_are_strict(self) -> None:
        task = self.start_task()
        loaded = adapters.load_project_adapter(self.project)
        frozen = routes.build_variants("tune", loaded, self.project, task)[0]
        run = {"id": "RUN-0001", "purpose": "evidence", "frozen": frozen}
        bad = mock.Mock()
        bad.execute.return_value = {"status": "finished", "extra": True}
        with self.assertRaisesRegex(ValueError, "start 返回结构"):
            runner.start(
                self.project, bad, run, purpose="evidence", backend="project"
            )
        bad.execute.return_value = {"status": "mystery"}
        with self.assertRaisesRegex(ValueError, "status 返回结构"):
            runner.status(
                self.project, bad, run, purpose="evidence", backend="project"
            )
        bad.execute.return_value = {"status": "cleanup_pending"}
        self.assertEqual(
            {"status": "cleanup_pending"},
            runner.status(
                self.project, bad, run, purpose="evidence", backend="project"
            ),
        )
        bad.execute.return_value = {"status": "running"}
        with self.assertRaisesRegex(ValueError, "stop 返回结构"):
            runner.stop(
                self.project, bad, run, purpose="evidence", backend="project"
            )
        bad.execute.return_value = {"status": "stop_requested"}
        self.assertEqual(
            {"status": "stop_requested"},
            runner.stop(
                self.project, bad, run, purpose="evidence", backend="project"
            ),
        )
        bad.parse_result.return_value = {"bad": True}
        with self.assertRaisesRegex(ValueError, "collect 返回结构"):
            runner.collect(
                self.project, bad, run, purpose="evidence", backend="project"
            )

    def test_claim_rolls_back_run_when_task_write_fails(self) -> None:
        task = self.start_task()
        transition_task(self.project, task["id"], "preparing")
        readiness(
            self.project,
            task["id"],
            target_clear=True,
            template_runnable=True,
            mutation_allowed=True,
            code_verified=True,
        )
        loaded = adapters.load_project_adapter(self.project)
        frozen = routes.build_variants("tune", loaded, self.project, task)[0]
        original = runs.atomic_write_json

        def fail_task(path: Path, *args: object, **kwargs: object) -> object:
            if path.parent.name == "tasks":
                raise OSError("task write failed")
            return original(path, *args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(runs, "atomic_write_json", side_effect=fail_task):
            with self.assertRaisesRegex(OSError, "task write failed"):
                runs.claim_run(
                    self.project, task["id"], frozen, purpose="evidence"
                )
        self.assertEqual("ready", load_task(self.project, task["id"])["stage"])
        self.assertEqual([], load_task(self.project, task["id"])["run_refs"])
        self.assertEqual([], list((self.control / "runs").iterdir()))

    def test_malicious_adapter_cannot_change_comparison_or_ledger_objects(self) -> None:
        (self.project / "workflow_adapter.py").write_text(
            """_SHARED = {}
def inspect(project):
    return {'standard': {'debug_required': False}}
def validate(project):
    return []
def prepare_runs(project, task):
    task['route_inputs']['baseline'] = 999
    return [{'code': {}, 'config': {}, 'seed': 3, 'data': {}, 'environment': {}}]
def execute(project, action, run):
    run['frozen']['config']['forged'] = True
    if action == 'start':
        return {'status': 'finished', 'process_id': 9}
    if action == 'status':
        return {'status': 'finished'}
    return {'status': 'stopped'}
def parse_result(project, run):
    run['frozen']['config']['parsed'] = True
    _SHARED.clear()
    _SHARED.update({
        'execution': {'outcome': 'succeeded', 'exit_code': 0, 'issue_kind': None},
        'result': {'metrics': {'score': 0.71}, 'raw_log': None},
        'quality': {'implementation': 'valid', 'interface': 'valid', 'data': 'valid', 'metrics': 'valid'},
        'analysis': {'hypothesis': 'supported', 'limitations': [], 'suggestions': []},
        'artifacts': [],
    })
    return _SHARED
""",
            encoding="utf-8",
        )
        self._commit_adapter("install malicious adapter")
        loaded = adapters.load_project_adapter(self.project)
        task = self.start_task(baseline=0.70, primary_metric="score")
        with mock.patch.object(engine, "load_project_adapter", return_value=loaded):
            result = engine.execute_task(self.project, task["id"])
        loaded._parse_result.__globals__["_SHARED"]["result"]["metrics"]["score"] = 9  # type: ignore[attr-defined]
        final_task = load_task(self.project, task["id"])
        run = load_run(self.project, final_task["run_refs"][0])
        self.assertEqual(0.70, final_task["route_inputs"]["baseline"])
        self.assertNotIn("forged", run["frozen"]["config"])
        self.assertNotIn("parsed", run["frozen"]["config"])
        self.assertEqual(0.71, run["result"]["metrics"]["score"])
        self.assertEqual(
            "side_by_side_only",
            result["comparison"]["comparison_scope"],
        )
        self.assertIsNone(result["comparison"]["delta"])
        self.assertTrue(result["comparison"]["blockers"])

    def test_running_runner_waits_without_collect_then_resumes_once(self) -> None:
        task = self.start_task()
        original_collect = engine.runner_collect
        with (
            mock.patch.object(
                engine,
                "runner_start",
                return_value={"status": "running", "process_id": 321},
            ) as started,
            mock.patch.object(
                engine, "runner_status", return_value={"status": "finished"}
            ) as status,
            mock.patch.object(
                engine, "runner_collect", wraps=original_collect
            ) as collected,
        ):
            first = engine.execute_task(self.project, task["id"])
            self.assertEqual("in_progress", first["status"])
            self.assertEqual("executing", first["task"]["stage"])
            self.assertEqual("running", first["run"]["execution"]["stage"])
            self.assertTrue(all(item["closed"] for item in first["session"]))
            self.assertEqual(0, collected.call_count)
            second = engine.execute_task(self.project, task["id"])
        self.assertEqual("done", second["task"]["stage"])
        self.assertEqual(1, started.call_count)
        self.assertEqual(1, status.call_count)
        self.assertEqual(1, collected.call_count)

    def test_repeated_running_status_never_restarts_or_collects(self) -> None:
        task = self.start_task()
        original_collect = engine.runner_collect
        with (
            mock.patch.object(
                engine,
                "runner_start",
                return_value={"status": "running", "process_id": 654},
            ) as started,
            mock.patch.object(
                engine,
                "runner_status",
                side_effect=[
                    {"status": "cleanup_pending"},
                    {"status": "running"},
                    {"status": "finished"},
                ],
            ) as status,
            mock.patch.object(
                engine, "runner_collect", wraps=original_collect
            ) as collected,
        ):
            results = [engine.execute_task(self.project, task["id"]) for _ in range(4)]
        self.assertEqual(
            ["in_progress", "in_progress", "in_progress"],
            [item["status"] for item in results[:3]],
        )
        self.assertEqual("done", results[3]["task"]["stage"])
        self.assertEqual(1, started.call_count)
        self.assertEqual(3, status.call_count)
        self.assertEqual(1, collected.call_count)

    def test_async_failed_and_stopped_status_use_legal_closures(self) -> None:
        cases = (
            ("failed", "failed", "environment", "ready"),
            ("stopped", "stopped", "user_stop", "stopped"),
        )
        for status_value, outcome, issue_kind, task_stage in cases:
            with self.subTest(status=status_value):
                task = self.start_task()
                with mock.patch.object(
                    engine,
                    "runner_start",
                    return_value={"status": "running", "process_id": 88},
                ):
                    first = engine.execute_task(self.project, task["id"])
                self.assertEqual("in_progress", first["status"])
                with mock.patch.object(
                    engine,
                    "runner_status",
                    return_value={"status": status_value},
                ):
                    final = engine.execute_task(self.project, task["id"])
                self.assertEqual(task_stage, final["task"]["stage"])
                self.assertEqual(outcome, final["run"]["execution"]["outcome"])
                self.assertEqual(
                    issue_kind, final["run"]["execution"]["issue_kind"]
                )


if __name__ == "__main__":
    unittest.main()
