from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests._helpers import SCRIPTS, cli_json, read_json, run_cli

sys.path.insert(0, str(SCRIPTS))

from workflow_core.codebases import (  # noqa: E402
    CODEBASE_SCHEMA,
    register_codebase,
    verify_codebase_gate,
)
from workflow_core.domain_packs import (  # noqa: E402
    create_domain_repository,
    resolve_domain_pack,
)
from workflow_core.adapters import load_snapshot_adapter  # noqa: E402
from workflow_core.engine import execute_task  # noqa: E402
from workflow_core import engine as engine_module  # noqa: E402
from workflow_core import execution_snapshot as snapshot_module  # noqa: E402
from workflow_core.evidence import (  # noqa: E402
    current_evidence_level,
    record_evidence_transition,
)
from workflow_core.project import init_project  # noqa: E402
from workflow_core import paper_handoff as paper_handoff_module  # noqa: E402
from workflow_core.run_identity import is_bound_frozen  # noqa: E402
from workflow_core.runner import collect as runner_collect  # noqa: E402
from workflow_core.runs import (  # noqa: E402
    claim_run,
    create_run,
    finish_run,
    load_run,
    start_run,
    validate_run,
)
from workflow_core.tasking import (  # noqa: E402
    create_task,
    load_task,
    readiness,
    transition_task,
)


def _fake_central_cuda_attestation(
    *,
    gate: dict[str, object],
    adapter: dict[str, str],
) -> dict[str, object]:
    from workflow_core import run_identity

    environment = run_identity.live_environment_snapshot()
    return {
        "schema": run_identity.CUDA_ATTESTATION_SCHEMA,
        "verified_by": run_identity.CUDA_ATTESTATION_VERIFIER,
        "template_id": "PACK-CLS",
        "template_version": "1.0.0",
        "adapter_sha256": adapter["sha256"],
        "python_executable_sha256": environment["python"][
            "executable_sha256"
        ],
        "torch_version": "test-cuda",
        "cuda_runtime": "test-cuda",
        "device_index": 0,
        "device_count": 1,
        "device_name": "Test CUDA Device",
        "compute_capability": [9, 0],
        "probe": {
            "operation": "2x2_ones_matmul_sum",
            "result": 8.0,
            "tensor_device": "cuda:0",
        },
    }


class FormalRunGitGateTests(unittest.TestCase):
    def setUp(self) -> None:
        cuda_attestation = mock.patch(
            "workflow_core.run_identity.create_central_cuda_attestation",
            side_effect=_fake_central_cuda_attestation,
        )
        cuda_attestation.start()
        self.addCleanup(cuda_attestation.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "ledger-project"
        init_project(self.project, "formal-run-git-gate", layout="v2")
        self.control = self.project / ".experiment-workflow"
        self.repo = self._new_codebase_repo()
        self.commit = self._git(self.repo, "rev-parse", "HEAD")
        self.codebase = self._register_codebase()

    def _git(
        self,
        repository: Path,
        *arguments: str,
        check: bool = True,
    ) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=15,
        )
        if check:
            self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def _adapter_source(
        self,
        *,
        data_kind: str | None = "real_imagefolder",
        start_status: str = "finished",
        mutate_tracked_in_prepare: bool = False,
        include_dataset_identity: bool = True,
    ) -> str:
        mutation = (
            '    (project / "CODEBASE_MARKER").write_text('
            '"mutated by adapter\\n", encoding="utf-8")\n'
            if mutate_tracked_in_prepare
            else ""
        )
        dataset_identity = (
            '            "dataset_identity": {\n'
            '                "schema": '
            '"cv-experiment-workflow.dataset-identity.v1",\n'
            '                "dataset_id": "formal-gate-demo",\n'
            '                "version": "1",\n'
            '                "source_uri": "dataset://formal-gate/demo",\n'
            '                "manifest_sha256": "sha256:'
            + ("2" * 64)
            + '",\n'
            '                "split": "validation",\n'
            '            },\n'
            if include_dataset_identity
            else ""
        )
        return f"""\
from pathlib import Path as _Path


def inspect(project):
    if not (project / "CODEBASE_MARKER").is_file():
        raise ValueError("inspect did not receive codebase root")
    return {{"standard": {{"debug_required": False}}}}


def validate(project):
    if not (project / "CODEBASE_MARKER").is_file():
        return ["validate did not receive codebase root"]
    return []


def prepare_runs(project, task):
    if not (project / "CODEBASE_MARKER").is_file():
        raise ValueError("prepare_runs did not receive codebase root")
{mutation}\
    return [{{
        "code": {{
            "revision": "adapter-declared",
            "codebase_id": "CB-9999",
            "fingerprints": {{"config": "forged"}},
        }},
        "config": {{
            **dict(task["route_inputs"]["config"]),
            "device": "cuda",
        }},
        "seed": task["route_inputs"]["seed"],
        "data": {{
            "kind": {data_kind!r},
            "evaluation": {{
                "schema": "test.evaluation.v1",
                "metric": "score",
                "split": "validation",
            }},
{dataset_identity}\
            "run_kind": "forged",
            "paper_eligible": True,
        }},
        "environment": {{
            "backend": "adapter-declared",
            "device": "cuda",
            "platform": "forged",
        }},
    }}]


def execute(project, action, run):
    if not (project / "CODEBASE_MARKER").is_file():
        raise ValueError("execute did not receive codebase root")
    if action == "start":
        return {{"status": {start_status!r}, "process_id": None}}
    if action == "status":
        return {{"status": "finished"}}
    if action == "stop":
        return {{"status": "stopped"}}
    raise ValueError(action)


def parse_result(project, run):
    if not (project / "CODEBASE_MARKER").is_file():
        raise ValueError("parse_result did not receive codebase root")
    return {{
        "execution": {{
            "outcome": "succeeded",
            "exit_code": 0,
            "issue_kind": None,
        }},
        "result": {{"metrics": {{"score": 0.8}}, "raw_log": None}},
        "quality": {{
            "implementation": "valid",
            "interface": "valid",
            "data": "valid",
            "metrics": "valid",
        }},
        "analysis": {{
            "hypothesis": "inconclusive",
            "limitations": [],
            "suggestions": [],
        }},
        "artifacts": [],
    }}
"""

    def _new_codebase_repo(
        self,
        *,
        data_kind: str | None = "real_imagefolder",
        start_status: str = "finished",
    ) -> Path:
        repo = self.root / "codebase"
        repo.mkdir()
        self._git(repo, "init", "-b", "main")
        self._git(repo, "config", "user.name", "Test User")
        self._git(repo, "config", "user.email", "test@example.com")
        (repo / "CODEBASE_MARKER").write_text("bound-root\n", encoding="utf-8")
        (repo / "workflow_adapter.py").write_text(
            self._adapter_source(
                data_kind=data_kind,
                start_status=start_status,
            ),
            encoding="utf-8",
            newline="\n",
        )
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-m", "bound adapter")
        return repo

    def _register_codebase(self) -> dict[str, object]:
        manifest = {
            "schema": CODEBASE_SCHEMA,
            "name": "classification-codebase",
            "primary_direction": "cls",
            "repo_path": str(self.repo.resolve()),
            "source": "domain_pack",
            "template_id": "PACK-CLS",
            "template_version": "1.0.0",
            "default_branch": "main",
            "initial_commit": self.commit,
            "initial_tag": None,
        }
        path = self.root / "codebase-manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return register_codebase(self.project, path, self.repo)

    def _binding(self, **changes: object) -> dict[str, object]:
        binding: dict[str, object] = {
            "codebase_id": "CB-0001",
            "branch": "main",
            "commit": self.commit,
            "tag": None,
        }
        binding.update(changes)
        return binding

    def _create_task(
        self,
        *,
        binding: dict[str, object] | None | object = ...,
        max_runs: int = 2,
        config: dict[str, object] | None = None,
        debug_required: bool = False,
    ) -> dict[str, object]:
        inputs: dict[str, object] = {
            "config": (
                {"learning_rate": 0.001}
                if config is None
                else dict(config)
            ),
            "seed": 7,
            "debug_required": debug_required,
            "changes_code_behavior": False,
            "code_verified": True,
        }
        if binding is ...:
            inputs["code_binding"] = self._binding()
        elif binding is not None:
            inputs["code_binding"] = binding
        return create_task(
            self.project,
            owner_request="run the bound codebase",
            route="tune",
            target_refs=["baseline"],
            route_inputs=inputs,
            budget={"max_runs": max_runs},
            stop_condition={"max_failures": 1},
        )

    def _install_data_probe_adapter(
        self,
        *,
        mutate_on_start: bool,
    ) -> Path:
        probe = self.root / "dataset.manifest"
        probe.write_text("original\n", encoding="utf-8")
        mutation = (
            '        _Path(run["frozen"]["config"]["probe_path"]).write_text('
            '"changed-after-start\\n", encoding="utf-8")\n'
            if mutate_on_start
            else ""
        )
        source = f"""\
from pathlib import Path as _Path
import hashlib as _hashlib


def inspect(project):
    return {{"standard": {{"debug_required": False}}}}


def validate(project):
    return []


def prepare_runs(project, task):
    probe = _Path(task["route_inputs"]["config"]["probe_path"])
    digest = _hashlib.sha256(probe.read_bytes()).hexdigest()
    return [{{
        "code": {{"revision": "data-probe"}},
        "config": {{
            **dict(task["route_inputs"]["config"]),
            "device": "cuda",
        }},
        "seed": task["route_inputs"]["seed"],
        "data": {{
            "kind": "real_imagefolder",
            "evaluation": {{
                "schema": "test.evaluation.v1",
                "metric": "score",
                "split": "validation",
            }},
            "dataset_identity": {{
                "schema": "cv-experiment-workflow.dataset-identity.v1",
                "dataset_id": "data-probe",
                "version": "1",
                "source_uri": "dataset://formal-gate/data-probe",
                "manifest_sha256": "sha256:" + digest,
                "split": "validation",
            }},
        }},
        "environment": {{"backend": "test", "device": "cuda"}},
    }}]


def execute(project, action, run):
    if action == "start":
{mutation}\
        return {{"status": "finished", "process_id": None}}
    if action == "status":
        return {{"status": "finished"}}
    if action == "stop":
        return {{"status": "stopped"}}
    raise ValueError(action)


def parse_result(project, run):
    return {{
        "execution": {{
            "outcome": "succeeded",
            "exit_code": 0,
            "issue_kind": None,
        }},
        "result": {{"metrics": {{"score": 0.8}}, "raw_log": None}},
        "quality": {{
            "implementation": "valid",
            "interface": "valid",
            "data": "valid",
            "metrics": "valid",
        }},
        "analysis": {{
            "hypothesis": "supported",
            "limitations": [],
            "suggestions": [],
        }},
        "artifacts": [],
    }}
"""
        (self.repo / "workflow_adapter.py").write_text(
            source,
            encoding="utf-8",
            newline="\n",
        )
        self._git(self.repo, "add", "workflow_adapter.py")
        self._git(self.repo, "commit", "-m", "data probe adapter")
        self.commit = self._git(self.repo, "rev-parse", "HEAD")
        return probe

    def _ready_task(self, task: dict[str, object]) -> dict[str, object]:
        transition_task(self.project, str(task["id"]), "preparing")
        self.assertEqual(
            {"status": "pass"},
            readiness(
                self.project,
                str(task["id"]),
                target_clear=True,
                template_runnable=True,
                mutation_allowed=True,
                code_verified=True,
            ),
        )
        return load_task(self.project, str(task["id"]))

    @staticmethod
    def _legacy_frozen() -> dict[str, object]:
        return {
            "code": {"commit": "a" * 40},
            "config": {"learning_rate": 0.001},
            "seed": 7,
            "data": {"kind": "synthetic_debug_only"},
            "environment": {"python": "test"},
        }

    def _bound_frozen(self, *, purpose: str = "debug") -> dict[str, object]:
        from workflow_core.run_identity import build_bound_frozen

        adapter_path = self.repo / "workflow_adapter.py"
        return build_bound_frozen(
            {
                "code": {"revision": "direct-api-test"},
                "config": {
                    "learning_rate": 0.001,
                    "device": "cuda",
                },
                "seed": 7,
                "data": {
                    "kind": "real_imagefolder",
                    "evaluation": {
                        "schema": "test.evaluation.v1",
                        "metric": "score",
                        "split": "validation",
                    },
                    "dataset_identity": {
                        "schema": (
                            "cv-experiment-workflow.dataset-identity.v1"
                        ),
                        "dataset_id": "formal-gate-demo",
                        "version": "1",
                        "source_uri": "dataset://formal-gate/demo",
                        "manifest_sha256": "sha256:" + ("2" * 64),
                        "split": "validation",
                    },
                },
                "environment": {
                    "backend": "direct-api-test",
                    "device": "cuda",
                },
            },
            binding=self._binding(),
            gate={
                "codebase_id": "CB-0001",
                "repo_path": str(self.repo.resolve()),
                "branch": "main",
                "commit": self.commit,
                "tag": None,
                "worktree_clean": True,
            },
            adapter={
                "sha256": hashlib.sha256(adapter_path.read_bytes()).hexdigest(),
                "repo_url": str(self.repo.resolve()),
                "commit": self.commit,
                "source": "workflow_adapter.py",
            },
            purpose=purpose,
        )

    def _assert_direct_run_rejected_without_mutation(
        self,
        api: object,
        task: dict[str, object],
        frozen: dict[str, object],
        *,
        purpose: str = "debug",
        message: str = "bound|code_binding|Codebase|Git|Adapter|purpose",
    ) -> None:
        task_path = self.control / "tasks" / f"{task['id']}.json"
        before_task = task_path.read_bytes()
        before_runs = sorted(
            path.relative_to(self.control)
            for path in (self.control / "runs").rglob("*")
        )
        with self.assertRaisesRegex(ValueError, message):
            api(  # type: ignore[operator]
                self.project,
                str(task["id"]),
                copy.deepcopy(frozen),
                purpose=purpose,
            )
        self.assertEqual(before_task, task_path.read_bytes())
        self.assertEqual(
            before_runs,
            sorted(
                path.relative_to(self.control)
                for path in (self.control / "runs").rglob("*")
            ),
        )

    def _replace_adapter_and_commit(
        self,
        *,
        data_kind: str | None = "real_imagefolder",
        start_status: str = "finished",
        mutate_tracked_in_prepare: bool = False,
        include_dataset_identity: bool = True,
    ) -> None:
        (self.repo / "workflow_adapter.py").write_text(
            self._adapter_source(
                data_kind=data_kind,
                start_status=start_status,
                mutate_tracked_in_prepare=mutate_tracked_in_prepare,
                include_dataset_identity=include_dataset_identity,
            ),
            encoding="utf-8",
            newline="\n",
        )
        self._git(self.repo, "add", "workflow_adapter.py")
        self._git(self.repo, "commit", "-m", "replace adapter")
        self.commit = self._git(self.repo, "rev-parse", "HEAD")

    def _commit_index_entry(
        self,
        *,
        mode: str,
        relative: str,
        content: str,
    ) -> None:
        source = self.root / "git-object-source"
        source.write_text(content, encoding="utf-8")
        object_id = self._git(self.repo, "hash-object", "-w", str(source))
        self._git(
            self.repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"{mode},{object_id},{relative}",
        )
        self._git(self.repo, "commit", "-m", f"add {relative} as {mode}")
        self.commit = self._git(self.repo, "rev-parse", "HEAD")

    def test_binding_is_strict_cross_linked_and_old_tasks_stay_valid(self) -> None:
        task = self._create_task()
        self.assertEqual(self._binding(), task["route_inputs"]["code_binding"])

        old_task = self._create_task(binding=None)
        self.assertNotIn("code_binding", old_task["route_inputs"])
        self.assertEqual(old_task, load_task(self.project, old_task["id"]))
        self.assertEqual(
            {"sources": 0, "ideas": 0, "templates": 0, "modules": 0},
            cli_json("validate", "--project", self.project),
        )

        invalid_bindings = (
            {**self._binding(), "extra": True},
            {key: value for key, value in self._binding().items() if key != "tag"},
            self._binding(codebase_id="CB-1"),
            self._binding(commit="A" * 40),
            self._binding(branch=" main"),
            self._binding(branch="ma\x00in"),
            self._binding(branch="ma\nin"),
            self._binding(tag=""),
            self._binding(tag="v1\x00bad"),
            self._binding(tag="v1\nbad"),
        )
        for binding in invalid_bindings:
            with self.subTest(binding=binding):
                with self.assertRaises(ValueError):
                    self._create_task(binding=binding)

        before = sorted(path.name for path in (self.control / "tasks").iterdir())
        with self.assertRaisesRegex(ValueError, "Codebase|CB-9999"):
            self._create_task(binding=self._binding(codebase_id="CB-9999"))
        self.assertEqual(
            before,
            sorted(path.name for path in (self.control / "tasks").iterdir()),
        )

        record = self.control / "codebases" / "CB-0001.json"
        saved = record.read_bytes()
        record.unlink()
        try:
            invalid = run_cli(
                "validate",
                "--project",
                self.project,
                check=False,
            )
            self.assertNotEqual(0, invalid.returncode)
            self.assertRegex(invalid.stderr, "Codebase|CB-0001")
        finally:
            record.write_bytes(saved)

    def test_direct_run_apis_enforce_task_binding_without_mutation(self) -> None:
        bound_task = self._ready_task(self._create_task())
        unbound_task = self._ready_task(self._create_task(binding=None))
        exact_bound = self._bound_frozen()

        for api in (create_run, claim_run):
            with self.subTest(api=api.__name__, direction="bound_needs_bound"):
                self._assert_direct_run_rejected_without_mutation(
                    api,
                    bound_task,
                    self._legacy_frozen(),
                    message="bound Run frozen",
                )
            with self.subTest(api=api.__name__, direction="unbound_rejects_bound"):
                self._assert_direct_run_rejected_without_mutation(
                    api,
                    unbound_task,
                    exact_bound,
                    message="bound Run frozen",
                )

        for field, wrong in (
            ("codebase_id", "CB-0002"),
            ("branch", "other"),
            ("commit", "0" * 40),
            ("tag", "v2"),
        ):
            attacked = copy.deepcopy(exact_bound)
            attacked["code"][field] = wrong  # type: ignore[index]
            with self.subTest(field=field):
                self._assert_direct_run_rejected_without_mutation(
                    create_run,
                    bound_task,
                    attacked,
                    message="code_binding",
                )

    def test_direct_bound_run_apis_rebuild_live_code_identity(self) -> None:
        for api in (create_run, claim_run):
            with self.subTest(api=api.__name__, case="exact_identity"):
                task = self._ready_task(self._create_task())
                run = api(
                    self.project,
                    str(task["id"]),
                    self._bound_frozen(),
                    purpose="debug",
                )
                self.assertIn(
                    run["id"],
                    load_task(self.project, str(task["id"]))["run_refs"],
                )

        task = self._ready_task(self._create_task())
        exact = self._bound_frozen()
        frozen_attacks = (
            ("adapter_sha256", "0" * 64),
            ("worktree_clean", False),
        )
        for api in (create_run, claim_run):
            for field, wrong in frozen_attacks:
                attacked = copy.deepcopy(exact)
                attacked["code"][field] = wrong  # type: ignore[index]
                with self.subTest(api=api.__name__, field=field):
                    self._assert_direct_run_rejected_without_mutation(
                        api,
                        task,
                        attacked,
                        message=f"live Codebase.*{field}",
                    )

        evidence_identity = self._bound_frozen(purpose="evidence")
        for api in (create_run, claim_run):
            with self.subTest(api=api.__name__, field="clean_required"):
                self._assert_direct_run_rejected_without_mutation(
                    api,
                    task,
                    evidence_identity,
                    purpose="debug",
                    message="clean_required.*purpose",
                )

        for field, wrong in (
            ("branch", "other"),
            ("commit", "0" * 40),
            ("tag", "missing-tag"),
        ):
            binding = self._binding(**{field: wrong})
            wrong_task = self._ready_task(self._create_task(binding=binding))
            wrong_frozen = self._bound_frozen()
            wrong_frozen["code"][field] = wrong  # type: ignore[index]
            for api in (create_run, claim_run):
                with self.subTest(api=api.__name__, live_git_field=field):
                    self._assert_direct_run_rejected_without_mutation(
                        api,
                        wrong_task,
                        wrong_frozen,
                        message=f"{field}|Git",
                    )

        note = self.repo / "live-dirty.txt"
        dirty_task = self._ready_task(self._create_task())
        clean_claim = self._bound_frozen()
        note.write_text("dirty\n", encoding="utf-8")
        try:
            for api in (create_run, claim_run):
                with self.subTest(api=api.__name__, case="live_dirty"):
                    self._assert_direct_run_rejected_without_mutation(
                        api,
                        dirty_task,
                        clean_claim,
                        message="worktree_clean",
                    )
        finally:
            note.unlink(missing_ok=True)

        adapter_path = self.repo / "workflow_adapter.py"
        original_adapter = adapter_path.read_bytes()
        adapter_path.write_bytes(original_adapter + b"\n# forged bytes\n")
        try:
            forged_adapter = self._bound_frozen()
            forged_adapter["code"]["worktree_clean"] = False  # type: ignore[index]
            for api in (create_run, claim_run):
                forged_task = self._ready_task(self._create_task())
                with self.subTest(api=api.__name__, case="adapter_bytes"):
                    self._assert_direct_run_rejected_without_mutation(
                        api,
                        forged_task,
                        forged_adapter,
                        message="bytes|blob|Adapter",
                    )
        finally:
            adapter_path.write_bytes(original_adapter)

    def test_validate_rejects_task_run_binding_tamper(self) -> None:
        task = self._create_task()
        result = execute_task(self.project, str(task["id"]), purpose="debug")
        run = load_run(self.project, result["run"]["id"])
        run_path = self.control / "runs" / run["id"] / "run.json"
        task_path = self.control / "tasks" / f"{task['id']}.json"
        original_run = run_path.read_bytes()
        original_task = task_path.read_bytes()

        attacked_run = read_json(run_path)
        attacked_run["frozen"]["code"]["branch"] = "other"
        canonical = json.dumps(
            attacked_run["frozen"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        attacked_run["frozen_digest"] = (
            "sha256:" + hashlib.sha256(canonical).hexdigest()
        )
        run_path.write_text(json.dumps(attacked_run), encoding="utf-8")
        invalid = run_cli("validate", "--project", self.project, check=False)
        self.assertNotEqual(0, invalid.returncode)
        self.assertRegex(invalid.stderr, "code_binding")

        run_path.write_bytes(original_run)
        attacked_task = read_json(task_path)
        attacked_task["route_inputs"].pop("code_binding")
        task_path.write_text(json.dumps(attacked_task), encoding="utf-8")
        invalid = run_cli("validate", "--project", self.project, check=False)
        self.assertNotEqual(0, invalid.returncode)
        self.assertRegex(invalid.stderr, "bound Run frozen")

        task_path.write_bytes(original_task)
        self.assertEqual(
            {"sources": 0, "ideas": 0, "templates": 0, "modules": 0},
            cli_json("validate", "--project", self.project),
        )

    def test_gate_reports_cleanliness_even_when_dirty_is_allowed(self) -> None:
        expected = {
            "branch": "main",
            "commit": self.commit,
            "tag": None,
            "require_clean": False,
        }
        clean = verify_codebase_gate(
            self.project,
            "CB-0001",
            expected_git=expected,
        )
        self.assertTrue(clean["worktree_clean"])

        dirty_cases = {
            "tracked": lambda: (self.repo / "CODEBASE_MARKER").write_text(
                "dirty\n", encoding="utf-8"
            ),
            "untracked": lambda: (self.repo / "notes.txt").write_text(
                "dirty\n", encoding="utf-8"
            ),
            "ignored_executable": self._add_ignored_executable,
        }
        for name, make_dirty in dirty_cases.items():
            with self.subTest(name=name):
                self._git(self.repo, "reset", "--hard", self.commit)
                self._git(self.repo, "clean", "-fdx")
                make_dirty()
                allowed = verify_codebase_gate(
                    self.project,
                    "CB-0001",
                    expected_git=expected,
                )
                self.assertFalse(allowed["worktree_clean"])
                with self.assertRaisesRegex(ValueError, "clean"):
                    verify_codebase_gate(
                        self.project,
                        "CB-0001",
                        expected_git={**expected, "require_clean": True},
                    )

    def _add_ignored_executable(self) -> None:
        (self.repo / ".git" / "info" / "exclude").write_text(
            "ignored.py\n",
            encoding="utf-8",
        )
        (self.repo / "ignored.py").write_text("VALUE = 1\n", encoding="utf-8")

    def test_exact_git_failures_happen_before_any_task_or_run_mutation(self) -> None:
        cases = (
            ("branch", self._binding(branch="other")),
            ("commit", self._binding(commit="0" * 40)),
            ("tag", self._binding(tag="missing-tag")),
        )
        for label, binding in cases:
            with self.subTest(label=label):
                task = self._create_task(binding=binding)
                before = (
                    read_json(self.control / "tasks" / f"{task['id']}.json"),
                    sorted((self.control / "runs").iterdir()),
                )
                with self.assertRaisesRegex(ValueError, label):
                    execute_task(self.project, task["id"], purpose="debug")
                self.assertEqual(before[0], load_task(self.project, task["id"]))
                self.assertEqual(before[1], sorted((self.control / "runs").iterdir()))

        task = self._create_task()
        self._git(self.repo, "checkout", "--detach")
        with self.assertRaisesRegex(ValueError, "branch|detached"):
            execute_task(self.project, task["id"], purpose="debug")
        self.assertEqual("queued", load_task(self.project, task["id"])["stage"])
        self.assertEqual([], load_task(self.project, task["id"])["run_refs"])

    def test_debug_allows_dirty_but_freezes_non_paper_identity(self) -> None:
        task = self._create_task()
        (self.repo / "notes.txt").write_text("debug-only\n", encoding="utf-8")
        result = execute_task(self.project, task["id"], purpose="debug")
        run = load_run(self.project, result["run"]["id"])

        self.assertFalse(run["frozen"]["code"]["clean_required"])
        self.assertFalse(run["frozen"]["code"]["worktree_clean"])
        self.assertEqual("real_experiment", run["frozen"]["data"]["run_kind"])
        self.assertFalse(run["frozen"]["data"]["paper_eligible"])
        self.assertEqual("debug", result["evidence_level"])
        self.assertEqual("debug", current_evidence_level(self.project, run["id"]))
        self.assertEqual("closed", run["execution"]["stage"])
        self.assertEqual("preparing", load_task(self.project, task["id"])["stage"])

    def test_evidence_dirty_and_synthetic_reject_before_claim(self) -> None:
        task = self._create_task()
        (self.repo / "notes.txt").write_text("dirty\n", encoding="utf-8")
        before = copy.deepcopy(load_task(self.project, task["id"]))
        with self.assertRaisesRegex(ValueError, "clean"):
            execute_task(self.project, task["id"], purpose="evidence")
        self.assertEqual(before, load_task(self.project, task["id"]))
        self.assertEqual([], list((self.control / "runs").iterdir()))

        self._git(self.repo, "clean", "-fd")
        self._replace_adapter_and_commit(data_kind="synthetic_debug_only")
        synthetic_task = self._create_task(binding=self._binding())
        before = copy.deepcopy(load_task(self.project, synthetic_task["id"]))
        with self.assertRaisesRegex(ValueError, "synthetic|合成|paper"):
            execute_task(
                self.project,
                synthetic_task["id"],
                purpose="evidence",
            )
        self.assertEqual(before, load_task(self.project, synthetic_task["id"]))
        self.assertEqual([], list((self.control / "runs").iterdir()))

        self._replace_adapter_and_commit(data_kind=None)
        missing_kind_task = self._create_task(binding=self._binding())
        before = copy.deepcopy(load_task(self.project, missing_kind_task["id"]))
        with self.assertRaisesRegex(ValueError, "data.kind|kind"):
            execute_task(
                self.project,
                missing_kind_task["id"],
                purpose="evidence",
            )
        self.assertEqual(before, load_task(self.project, missing_kind_task["id"]))
        self.assertEqual([], list((self.control / "runs").iterdir()))

        self._replace_adapter_and_commit(
            data_kind="real_imagefolder",
            include_dataset_identity=False,
        )
        missing_identity_task = self._create_task(binding=self._binding())
        before = copy.deepcopy(load_task(self.project, missing_identity_task["id"]))
        with self.assertRaisesRegex(ValueError, "dataset_identity"):
            execute_task(
                self.project,
                missing_identity_task["id"],
                purpose="evidence",
            )
        self.assertEqual(before, load_task(self.project, missing_identity_task["id"]))
        self.assertEqual([], list((self.control / "runs").iterdir()))

    def test_adapter_cannot_dirty_tracked_code_between_gate_and_claim(self) -> None:
        self._replace_adapter_and_commit(mutate_tracked_in_prepare=True)
        task = self._create_task(binding=self._binding())
        before = copy.deepcopy(load_task(self.project, task["id"]))

        with self.assertRaises((ValueError, PermissionError)):
            execute_task(self.project, task["id"], purpose="evidence")

        self.assertEqual(before, load_task(self.project, task["id"]))
        self.assertEqual([], list((self.control / "runs").iterdir()))

    def test_credible_bound_synthetic_debug_closes_without_paper_seal(
        self,
    ) -> None:
        self._replace_adapter_and_commit(data_kind="synthetic_debug_only")
        task = self._create_task(binding=self._binding())
        result = execute_task(self.project, task["id"], purpose="debug")
        run = load_run(self.project, result["run"]["id"])

        self.assertEqual("debug", result["evidence_level"])
        self.assertEqual("synthetic_debug_only", run["frozen"]["data"]["run_kind"])
        self.assertFalse(run["frozen"]["data"]["paper_eligible"])
        self.assertEqual("debug", current_evidence_level(self.project, run["id"]))
        self.assertEqual("closed", run["execution"]["stage"])
        self.assertEqual("preparing", load_task(self.project, task["id"])["stage"])

    def test_bound_adapter_bytes_and_every_runtime_call_use_codebase_root(self) -> None:
        self._replace_adapter_and_commit(start_status="running")
        task = self._create_task(binding=self._binding())
        first = execute_task(self.project, task["id"], purpose="evidence")
        self.assertEqual("in_progress", first["status"])
        second = execute_task(self.project, task["id"], purpose="evidence")
        run = load_run(self.project, second["run"]["id"])
        self.assertEqual("succeeded", run["execution"]["outcome"])
        self.assertEqual("artifact_seal_pending", second["status"])
        self.assertEqual("none", second["evidence_level"])
        self.assertNotIn("repository", run["frozen"]["code"])
        snapshot_root = self.control / run["execution_snapshot"]["relative_path"]
        self.assertTrue((snapshot_root / "workflow_adapter.py").is_file())

        tamper_task = self._create_task(binding=self._binding())
        adapter_path = self.repo / "workflow_adapter.py"
        adapter_path.write_text(
            adapter_path.read_text(encoding="utf-8") + "\n# byte drift\n",
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(ValueError, "bytes|blob|Adapter"):
            execute_task(self.project, tamper_task["id"], purpose="debug")
        self.assertEqual("queued", load_task(self.project, tamper_task["id"])["stage"])
        self.assertEqual([], load_task(self.project, tamper_task["id"])["run_refs"])

    def test_central_identity_overwrites_spoofing_and_fingerprints_detect_tamper(
        self,
    ) -> None:
        task = self._create_task()
        first = execute_task(self.project, task["id"], purpose="debug")
        first_run = load_run(self.project, first["run"]["id"])
        code = first_run["frozen"]["code"]
        self.assertEqual(
            {
                "schema",
                "codebase_id",
                "branch",
                "commit",
                "tag",
                "clean_required",
                "worktree_clean",
                "adapter_path",
                "adapter_sha256",
                "declared_code",
                "fingerprints",
            },
            set(code),
        )
        self.assertEqual("CB-0001", code["codebase_id"])
        self.assertEqual("adapter-declared", code["declared_code"]["revision"])
        self.assertEqual(
            {"config", "data", "evaluation", "environment"},
            set(code["fingerprints"]),
        )
        self.assertTrue(
            all(
                isinstance(value, str)
                and value.startswith("sha256:")
                and len(value) == 71
                for value in code["fingerprints"].values()
            )
        )
        self.assertEqual(
            "adapter-declared",
            first_run["frozen"]["environment"]["declared_environment"]["backend"],
        )
        self.assertNotEqual(
            "forged",
            first_run["frozen"]["environment"]["platform"],
        )

        second_task = self._create_task()
        second = execute_task(
            self.project,
            second_task["id"],
            purpose="debug",
        )
        second_run = load_run(self.project, second["run"]["id"])
        self.assertEqual(
            code["fingerprints"],
            second_run["frozen"]["code"]["fingerprints"],
        )

        run_path = (
            self.control
            / "runs"
            / first_run["id"]
            / "run.json"
        )
        tampered = read_json(run_path)
        tampered["frozen"]["data"]["kind"] = "tampered"
        canonical = json.dumps(
            tampered["frozen"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        tampered["frozen_digest"] = (
            "sha256:" + hashlib.sha256(canonical).hexdigest()
        )
        run_path.write_text(json.dumps(tampered), encoding="utf-8")
        invalid = run_cli(
            "validate",
            "--project",
            self.project,
            check=False,
        )
        self.assertNotEqual(0, invalid.returncode)
        self.assertRegex(invalid.stderr, "fingerprint|指纹")

    def test_bound_environment_freezes_runtime_and_committed_lock_identity(
        self,
    ) -> None:
        requirements = b"numpy==test-version\n"
        (self.repo / "requirements.txt").write_bytes(requirements)
        self._git(self.repo, "add", "requirements.txt")
        self._git(self.repo, "commit", "-m", "add committed requirements")
        self.commit = self._git(self.repo, "rev-parse", "HEAD")

        task = self._create_task()
        result = execute_task(self.project, task["id"], purpose="debug")
        run = load_run(self.project, result["run"]["id"])
        environment = run["frozen"]["environment"]

        self.assertEqual(
            {
                "platform",
                "python",
                "packages",
                "requirements_locks",
                "declared_environment",
                "workflow_comparison_policy",
            },
            set(environment),
        )
        self.assertEqual(
            {
                "schema": "cv-experiment-workflow.comparison-policy/v1",
                "algorithm": "fair-live-sealed-runs-v1",
                "require_live_output_seal": True,
            },
            environment["workflow_comparison_policy"],
        )
        self.assertEqual(
            {
                "implementation",
                "version",
                "cache_tag",
                "abi_flags",
                "executable_sha256",
            },
            set(environment["python"]),
        )
        self.assertRegex(
            environment["python"]["executable_sha256"],
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertNotIn("executable", environment["python"])
        self.assertEqual(
            {"torch", "numpy", "Pillow", "torchvision"},
            set(environment["packages"]),
        )
        self.assertTrue(
            all(
                isinstance(value, str) and value
                for value in environment["packages"].values()
            )
        )
        self.assertEqual(
            {
                "requirements.txt": (
                    "sha256:" + hashlib.sha256(requirements).hexdigest()
                )
            },
            environment["requirements_locks"],
        )

        attacked = copy.deepcopy(run)
        attacked["frozen"]["environment"]["python"][
            "executable_sha256"
        ] = "sha256:" + ("0" * 64)
        canonical = json.dumps(
            attacked["frozen"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        attacked["frozen_digest"] = (
            "sha256:" + hashlib.sha256(canonical).hexdigest()
        )
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            validate_run(attacked, expected_id=attacked["id"])

        unknown_policy = copy.deepcopy(run)
        unknown_policy["frozen"]["environment"][
            "workflow_comparison_policy"
        ]["algorithm"] = "attacker-selected-rule"
        canonical = json.dumps(
            unknown_policy["frozen"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        unknown_policy["frozen_digest"] = (
            "sha256:" + hashlib.sha256(canonical).hexdigest()
        )
        with self.assertRaisesRegex(ValueError, "comparison policy"):
            validate_run(
                unknown_policy,
                expected_id=unknown_policy["id"],
            )

    def test_bound_code_schema_is_fixed_and_exact(self) -> None:
        task = self._create_task()
        result = execute_task(self.project, task["id"], purpose="debug")
        run = load_run(self.project, result["run"]["id"])
        self.assertEqual(
            "cv-experiment-workflow.bound-code.v2",
            run["frozen"]["code"]["schema"],
        )

        cases = []
        missing = copy.deepcopy(run)
        del missing["frozen"]["code"]["schema"]
        cases.append(missing)
        unknown = copy.deepcopy(run)
        unknown["frozen"]["code"]["schema"] = "unknown"
        cases.append(unknown)
        extra = copy.deepcopy(run)
        extra["frozen"]["code"]["unexpected"] = True
        cases.append(extra)
        bad_branch = copy.deepcopy(run)
        bad_branch["frozen"]["code"]["branch"] = "ma\x00in"
        cases.append(bad_branch)
        bad_tag = copy.deepcopy(run)
        bad_tag["frozen"]["code"]["tag"] = "v1\nbad"
        cases.append(bad_tag)
        for attacked in cases:
            canonical = json.dumps(
                attacked["frozen"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            attacked["frozen_digest"] = (
                "sha256:" + hashlib.sha256(canonical).hexdigest()
            )
            with self.subTest(code=attacked["frozen"]["code"]):
                with self.assertRaisesRegex(
                    ValueError,
                    "schema|中央字段|branch|tag|repository",
                ):
                    validate_run(attacked, expected_id=attacked["id"])

    def test_data_and_evaluation_fingerprints_are_independent(self) -> None:
        from workflow_core.run_identity import (
            build_bound_frozen,
            data_contract_fingerprint,
        )

        binding = self._binding()
        gate = {
            "codebase_id": "CB-0001",
            "repo_path": str(self.repo.resolve()),
            "branch": "main",
            "commit": self.commit,
            "tag": None,
            "worktree_clean": True,
        }
        adapter = {
            "sha256": "1" * 64,
            "repo_url": str(self.repo.resolve()),
            "commit": self.commit,
            "source": "workflow_adapter.py",
        }
        variant = {
            "code": {"revision": "declared"},
            "config": {
                "learning_rate": 0.001,
                "device": "cuda",
            },
            "seed": 7,
            "data": {
                "kind": "real_imagefolder",
                "dataset": "demo",
                "dataset_identity": {
                    "schema": "cv-experiment-workflow.dataset-identity.v1",
                    "dataset_id": "demo",
                    "version": "1",
                    "source_uri": "dataset://formal-gate/demo",
                    "manifest_sha256": "sha256:" + ("3" * 64),
                    "split": "validation",
                },
                "evaluation": {
                    "schema": "test.evaluation.v1",
                    "metric": "score",
                    "split": "validation",
                },
            },
            "environment": {
                "backend": "declared",
                "device": "cuda",
            },
        }
        environment = {
            "platform": {"system": "Windows", "release": "test", "machine": "x64"},
            "python": {
                "implementation": "CPython",
                "version": "3.test",
                "cache_tag": "cpython-test",
                "abi_flags": "none",
                "executable_sha256": "sha256:" + ("4" * 64),
            },
            "packages": {
                "torch": "missing",
                "numpy": "missing",
                "Pillow": "missing",
                "torchvision": "missing",
            },
        }
        with mock.patch(
            "workflow_core.run_identity.live_environment_snapshot",
            return_value=environment,
        ):
            debug = build_bound_frozen(
                variant,
                binding=binding,
                gate=gate,
                adapter=adapter,
                purpose="debug",
            )
            evidence = build_bound_frozen(
                variant,
                binding=binding,
                gate=gate,
                adapter=adapter,
                purpose="evidence",
            )
            changed_evaluation = copy.deepcopy(variant)
            changed_evaluation["data"]["evaluation"]["split"] = "test"
            changed = build_bound_frozen(
                changed_evaluation,
                binding=binding,
                gate=gate,
                adapter=adapter,
                purpose="evidence",
            )

        self.assertNotEqual(
            debug["data"]["paper_eligible"],
            evidence["data"]["paper_eligible"],
        )
        self.assertEqual(
            debug["code"]["fingerprints"]["data"],
            evidence["code"]["fingerprints"]["data"],
        )
        self.assertEqual(
            evidence["code"]["fingerprints"]["data"],
            changed["code"]["fingerprints"]["data"],
        )
        self.assertNotEqual(
            evidence["code"]["fingerprints"]["evaluation"],
            changed["code"]["fingerprints"]["evaluation"],
        )
        central_only_changed = copy.deepcopy(evidence["data"])
        central_only_changed["run_kind"] = "synthetic_debug_only"
        central_only_changed["paper_eligible"] = False
        self.assertEqual(
            data_contract_fingerprint(evidence["data"]),
            data_contract_fingerprint(central_only_changed),
        )

        identity_attacks = []
        missing = copy.deepcopy(variant)
        del missing["data"]["dataset_identity"]["version"]
        identity_attacks.append(missing)
        extra = copy.deepcopy(variant)
        extra["data"]["dataset_identity"]["extra"] = True
        identity_attacks.append(extra)
        bad_digest = copy.deepcopy(variant)
        bad_digest["data"]["dataset_identity"]["manifest_sha256"] = "0" * 64
        identity_attacks.append(bad_digest)
        local_path = copy.deepcopy(variant)
        local_path["data"]["dataset_identity"]["source_uri"] = (
            r"D:\private\dataset"
        )
        identity_attacks.append(local_path)
        for attacked in identity_attacks:
            with self.subTest(identity=attacked["data"]["dataset_identity"]):
                with self.assertRaisesRegex(
                    ValueError,
                    "dataset_identity|source_uri",
                ):
                    build_bound_frozen(
                        attacked,
                        binding=binding,
                        gate=gate,
                        adapter=adapter,
                        purpose="evidence",
                    )

    def test_bound_real_evidence_is_blocked_until_artifact_seal_exists(self) -> None:
        task = self._create_task()
        result = execute_task(self.project, task["id"], purpose="evidence")
        run = load_run(self.project, result["run"]["id"])
        self.assertEqual("succeeded", run["execution"]["outcome"])
        self.assertTrue(run["frozen"]["data"]["paper_eligible"])
        self.assertEqual("artifact_seal_pending", result["status"])
        self.assertEqual("none", result["evidence_level"])
        self.assertEqual("none", current_evidence_level(self.project, run["id"]))
        self.assertEqual("finished", run["execution"]["stage"])
        self.assertEqual("reviewing", load_task(self.project, task["id"])["stage"])
        self.assertIsNone(result["task"]["conclusion"])
        self.assertNotIn(
            "artifact_seal_pending",
            run["analysis"]["limitations"],
        )
        self.assertEqual(
            {"sources": 0, "ideas": 0, "templates": 0, "modules": 0},
            cli_json("validate", "--project", self.project),
        )

        with self.assertRaises(ValueError):
            record_evidence_transition(
                self.project,
                run["id"],
                "single_run",
                evidence_refs=[run["id"]],
                reason="manual promotion must stay blocked",
                proposed_by="Agent:Analyst:test",
                checked_by="",
                applied_by="Agent:Coordinator:test",
            )

    def test_new_unbound_work_cannot_create_or_promote_paper_evidence_but_old_event_replays(
        self,
    ) -> None:
        queued = self._create_task(binding=None)
        before = copy.deepcopy(load_task(self.project, queued["id"]))
        with self.assertRaisesRegex(ValueError, "Codebase|绑定|unbound"):
            execute_task(self.project, queued["id"], purpose="evidence")
        self.assertEqual(before, load_task(self.project, queued["id"]))
        self.assertEqual([], list((self.control / "runs").iterdir()))

        ready = self._ready_task(queued)
        for api in (create_run, claim_run):
            with self.subTest(api=api.__name__):
                with self.assertRaisesRegex(ValueError, "Codebase|绑定|unbound"):
                    api(
                        self.project,
                        ready["id"],
                        self._legacy_frozen(),
                        purpose="evidence",
                    )
        self.assertEqual([], list((self.control / "runs").iterdir()))

        legacy = create_run(
            self.project,
            ready["id"],
            self._legacy_frozen(),
            purpose="debug",
        )
        start_run(self.project, legacy["id"])
        legacy = finish_run(
            self.project,
            legacy["id"],
            outcome="succeeded",
            exit_code=0,
            metrics={"score": 0.8},
            raw_log=None,
            implementation="valid",
            interface="valid",
            data="valid",
            metrics_quality="valid",
            hypothesis="supported",
            limitations=[],
            suggestions=[],
            artifacts=[],
            issue_kind=None,
        )
        run_path = self.control / "runs" / legacy["id"] / "run.json"
        old_bytes = read_json(run_path)
        old_bytes["purpose"] = "evidence"
        run_path.write_text(json.dumps(old_bytes), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Codebase|绑定|unbound"):
            record_evidence_transition(
                self.project,
                legacy["id"],
                "single_run",
                evidence_refs=[legacy["id"]],
                reason="new writes must fail",
                proposed_by="analyst",
                checked_by="reviewer",
                applied_by="coordinator",
            )
        event = {
            "event_id": "EVT-0001",
            "subject_type": "Run",
            "subject_id": legacy["id"],
            "from": "none",
            "to": "single_run",
            "evidence_refs": [legacy["id"]],
            "reason": "historical event",
            "proposed_by": "historical-analyst",
            "checked_by": "historical-reviewer",
            "applied_by": "historical-coordinator",
            "time": datetime.now(timezone.utc).isoformat(),
        }
        (self.control / "evidence.jsonl").write_text(
            json.dumps(event, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            {"sources": 0, "ideas": 0, "templates": 0, "modules": 0},
            cli_json("validate", "--project", self.project),
        )

    def test_bound_detection_requires_the_explicit_schema(self) -> None:
        legacy_like = self._legacy_frozen()
        legacy_like["code"]["codebase_id"] = "CB-0001"
        self.assertFalse(is_bound_frozen(legacy_like))

        bound_task = self._ready_task(self._create_task())
        with self.assertRaisesRegex(ValueError, "bound Run|绑定"):
            create_run(
                self.project,
                bound_task["id"],
                legacy_like,
                purpose="debug",
            )

        unbound_task = self._ready_task(self._create_task(binding=None))
        run = create_run(
            self.project,
            unbound_task["id"],
            legacy_like,
            purpose="debug",
        )
        self.assertEqual(legacy_like, run["frozen"])

    def test_bound_frozen_is_portable_and_bound_paperflow_export_is_blocked(
        self,
    ) -> None:
        task = self._create_task()
        result = execute_task(self.project, task["id"], purpose="evidence")
        run = load_run(self.project, result["run"]["id"])
        self.assertNotIn("repository", run["frozen"]["code"])
        self.assertIn("execution_snapshot", run)
        self.assertNotIn(
            str(self.repo.resolve()),
            json.dumps(run["frozen"], ensure_ascii=False),
        )
        event = {
            "event_id": "EVT-0001",
            "subject_type": "Run",
            "subject_id": run["id"],
            "from": "none",
            "to": "single_run",
            "evidence_refs": [run["id"]],
            "reason": "manual historical attack",
            "proposed_by": "attacker",
            "checked_by": "attacker",
            "applied_by": "attacker",
            "time": datetime.now(timezone.utc).isoformat(),
        }
        with self.assertRaisesRegex(ValueError, "bound|绑定|PaperFlow"):
            paper_handoff_module._build_payload(
                self.project,
                {"project_id": "formal-run-git-gate"},
                load_task(self.project, task["id"]),
                run,
                [event],
            )

    def test_bound_resume_runs_only_from_the_commit_snapshot(self) -> None:
        self._replace_adapter_and_commit(start_status="running")
        package = self.repo / "cls"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        score_file = package / "score.py"
        score_file.write_text("SCORE = 0.8\n", encoding="utf-8")
        adapter_path = self.repo / "workflow_adapter.py"
        source = adapter_path.read_text(encoding="utf-8")
        source = source.replace(
            "from pathlib import Path as _Path\n",
            "from pathlib import Path as _Path\nfrom cls.score import SCORE\n",
        ).replace(
            '{"score": 0.8}',
            '{"score": SCORE}',
        )
        adapter_path.write_text(source, encoding="utf-8", newline="\n")
        self._git(self.repo, "add", ".")
        self._git(self.repo, "commit", "-m", "add imported score")
        self.commit = self._git(self.repo, "rev-parse", "HEAD")

        task = self._create_task(binding=self._binding())
        first = execute_task(self.project, task["id"], purpose="debug")
        self.assertEqual("in_progress", first["status"])
        run = load_run(self.project, first["run"]["id"])
        snapshot = run["execution_snapshot"]
        snapshot_root = self.control / snapshot["relative_path"]
        self.assertEqual(
            "SCORE = 0.8\n",
            (snapshot_root / "cls" / "score.py").read_text(encoding="utf-8"),
        )

        score_file.write_text("SCORE = 0.1\n", encoding="utf-8")
        second = execute_task(self.project, task["id"], purpose="debug")
        self.assertEqual("debug", second["evidence_level"])
        self.assertEqual(
            0.8,
            load_run(self.project, run["id"])["result"]["metrics"]["score"],
        )

    def test_cls_pack_runs_end_to_end_inside_the_bound_snapshot(self) -> None:
        created = create_domain_repository(
            self.project,
            resolve_domain_pack("cls"),
            self.root / "bound-cls-repository",
            "bound-cls",
        )
        repository = created["repository"]
        codebase = created["codebase"]
        binding = {
            "codebase_id": codebase["id"],
            "branch": repository["default_branch"],
            "commit": repository["initial_commit"],
            "tag": repository["initial_tag"],
        }
        task = self._create_task(
            binding=binding,
            config={"mode": "synthetic_smoke"},
            debug_required=True,
        )

        result = execute_task(
            self.project,
            task["id"],
            purpose="debug",
        )

        self.assertEqual("debug", result["evidence_level"])
        run = load_run(self.project, result["run"]["id"])
        self.assertEqual("succeeded", run["execution"]["outcome"])
        self.assertTrue(run["artifacts"])
        self.assertTrue(
            all(
                path.startswith(".cv-workflow-output/")
                for path in run["artifacts"]
            )
        )
        snapshot_root = (
            self.control / run["execution_snapshot"]["relative_path"]
        )
        self.assertTrue(
            (
                snapshot_root
                / ".cv-workflow-output"
                / run["id"]
                / "metrics.json"
            ).is_file()
        )
        self.assertFalse(
            (
                Path(repository["repo_path"])
                / ".cv-workflow-output"
            ).exists()
        )

    def test_det_pack_runs_end_to_end_inside_the_bound_snapshot(self) -> None:
        created = create_domain_repository(
            self.project,
            resolve_domain_pack("det"),
            self.root / "bound-det-repository",
            "bound-det",
        )
        repository = created["repository"]
        codebase = created["codebase"]
        binding = {
            "codebase_id": codebase["id"],
            "branch": repository["default_branch"],
            "commit": repository["initial_commit"],
            "tag": repository["initial_tag"],
        }
        task = self._create_task(
            binding=binding,
            config={"mode": "synthetic_debug"},
            debug_required=True,
        )

        result = execute_task(
            self.project,
            task["id"],
            purpose="debug",
        )

        self.assertEqual("debug", result["evidence_level"])
        run = load_run(self.project, result["run"]["id"])
        self.assertEqual("succeeded", run["execution"]["outcome"])
        self.assertEqual(
            "synthetic_debug_only",
            run["frozen"]["data"]["run_kind"],
        )
        self.assertFalse(run["frozen"]["data"]["paper_eligible"])
        self.assertTrue(run["artifacts"])
        self.assertTrue(
            all(
                path.startswith(".cv-workflow-output/")
                for path in run["artifacts"]
            )
        )
        snapshot_root = (
            self.control / run["execution_snapshot"]["relative_path"]
        )
        self.assertTrue(
            all((snapshot_root / path).is_file() for path in run["artifacts"])
        )
        self.assertFalse(
            (
                Path(repository["repo_path"])
                / ".cv-workflow-output"
            ).exists()
        )

    def test_execution_snapshot_tamper_fails_closed(self) -> None:
        self._replace_adapter_and_commit(start_status="running")
        task = self._create_task(binding=self._binding())
        first = execute_task(self.project, task["id"], purpose="debug")
        run = load_run(self.project, first["run"]["id"])
        snapshot_root = self.control / run["execution_snapshot"]["relative_path"]
        adapter_path = snapshot_root / "workflow_adapter.py"
        adapter_path.chmod(0o600)
        adapter_path.write_text(
            "# tampered snapshot\n",
            encoding="utf-8",
        )
        result = execute_task(self.project, task["id"], purpose="debug")
        self.assertEqual("cleanup_failure", result["status"])
        current = load_run(self.project, run["id"])
        self.assertEqual("running", current["execution"]["stage"])
        self.assertEqual("pending", current["execution"]["outcome"])
        self.assertIn(
            "cleanup_failure:snapshot_unavailable_for_safe_stop",
            current["analysis"]["limitations"],
        )
        self.assertNotIn(
            current_evidence_level(self.project, run["id"]),
            {"single_run", "confirmed"},
        )

    def test_execution_snapshot_rejects_git_symlink_before_run_creation(
        self,
    ) -> None:
        self._commit_index_entry(
            mode="120000",
            relative="unsafe-link.py",
            content="workflow_adapter.py",
        )
        task = self._create_task(binding=self._binding())
        before = copy.deepcopy(load_task(self.project, task["id"]))
        with self.assertRaisesRegex(
            ValueError,
            "symlink|submodule|special|unsupported_git_object",
        ):
            execute_task(self.project, task["id"], purpose="debug")
        self.assertEqual(before, load_task(self.project, task["id"]))
        self.assertEqual([], list((self.control / "runs").iterdir()))

    def test_execution_snapshot_rejects_gitlink_submodule_before_run_creation(
        self,
    ) -> None:
        self._git(
            self.repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{self.commit},vendor/model",
        )
        self._git(self.repo, "commit", "-m", "add gitlink")
        self.commit = self._git(self.repo, "rev-parse", "HEAD")
        task = self._create_task(binding=self._binding())
        with self.assertRaisesRegex(
            ValueError,
            "symlink|submodule|special|unsupported_git_object",
        ):
            execute_task(self.project, task["id"], purpose="debug")
        self.assertEqual([], list((self.control / "runs").iterdir()))

    def test_execution_snapshot_rejects_windows_reserved_names(
        self,
    ) -> None:
        from workflow_core.execution_snapshot import (
            _normalize_git_path,
            _validate_windows_tree_paths,
        )

        with self.assertRaisesRegex(ValueError, "Windows|path_invalid"):
            _normalize_git_path("AUX.py")
        with self.assertRaisesRegex(ValueError, "output_collision|输出"):
            _normalize_git_path(".CV-WORKFLOW-OUTPUT/result.json")
        for paths in (
            ["Package/one.py", "package/two.py"],
            ["model", "model/config.json"],
        ):
            with self.subTest(paths=paths):
                with self.assertRaisesRegex(
                    ValueError,
                    "Windows|collision",
                ):
                    _validate_windows_tree_paths(paths)

    def test_execution_snapshot_budget_fails_before_run_creation(self) -> None:
        task = self._create_task(binding=self._binding())
        before = copy.deepcopy(load_task(self.project, task["id"]))
        with mock.patch(
            "workflow_core.execution_snapshot.MAX_SNAPSHOT_FILES",
            1,
        ):
            with self.assertRaisesRegex(ValueError, "budget|预算"):
                execute_task(self.project, task["id"], purpose="debug")
        self.assertEqual(before, load_task(self.project, task["id"]))
        self.assertEqual([], list((self.control / "runs").iterdir()))

    def test_git_tree_listing_is_bounded_before_run_creation(self) -> None:
        task = self._create_task(binding=self._binding())
        before = copy.deepcopy(load_task(self.project, task["id"]))
        with mock.patch.object(
            snapshot_module,
            "MAX_GIT_TREE_OUTPUT_BYTES",
            1,
        ):
            with self.assertRaisesRegex(ValueError, "bounded|limit|output"):
                execute_task(self.project, task["id"], purpose="debug")
        self.assertEqual(before, load_task(self.project, task["id"]))
        self.assertEqual([], list((self.control / "runs").iterdir()))

    def test_start_exception_stops_with_the_loaded_snapshot_adapter_first(
        self,
    ) -> None:
        task = self._create_task()
        with (
            mock.patch.object(
                engine_module,
                "runner_start",
                side_effect=RuntimeError("start failed after spawning"),
            ),
            mock.patch.object(
                engine_module,
                "runner_status",
                side_effect=AssertionError(
                    "status must not run before a new identity check"
                ),
            ),
            mock.patch.object(
                engine_module,
                "runner_stop",
                return_value={"status": "stopped"},
                create=True,
            ) as stop,
        ):
            result = execute_task(
                self.project,
                task["id"],
                purpose="debug",
            )

        stop.assert_called_once()
        run = load_run(self.project, result["run"]["id"])
        self.assertEqual("failed", run["execution"]["outcome"])
        self.assertEqual("environment", run["execution"]["issue_kind"])
        self.assertNotIn(
            "cleanup_failure",
            " ".join(run["analysis"]["limitations"]),
        )

    def test_start_exception_stop_failure_remains_auditable_and_unfinished(
        self,
    ) -> None:
        task = self._create_task()
        with (
            mock.patch.object(
                engine_module,
                "runner_start",
                side_effect=RuntimeError("start failed after spawning"),
            ),
            mock.patch.object(
                engine_module,
                "runner_status",
                side_effect=AssertionError("unsafe fallback status"),
            ),
            mock.patch.object(
                engine_module,
                "runner_stop",
                side_effect=RuntimeError("stop failed"),
                create=True,
            ),
        ):
            result = execute_task(
                self.project,
                task["id"],
                purpose="debug",
            )

        self.assertEqual("cleanup_failure", result["status"])
        run = load_run(self.project, result["run"]["id"])
        self.assertEqual("running", run["execution"]["stage"])
        self.assertEqual("pending", run["execution"]["outcome"])
        self.assertIn(
            "cleanup_failure",
            " ".join(run["analysis"]["limitations"]),
        )
        self.assertEqual("none", current_evidence_level(self.project, run["id"]))
        with (
            mock.patch.object(
                engine_module,
                "runner_start",
                side_effect=AssertionError("must not restart"),
            ),
            mock.patch.object(
                engine_module,
                "runner_status",
                side_effect=AssertionError("manual cleanup is still required"),
            ),
            mock.patch.object(
                engine_module,
                "runner_collect",
                side_effect=AssertionError("must not collect"),
            ),
        ):
            resumed = execute_task(
                self.project,
                task["id"],
                purpose="debug",
            )
        self.assertEqual("cleanup_failure", resumed["status"])
        self.assertEqual(
            run["analysis"]["limitations"],
            resumed["run"]["analysis"]["limitations"],
        )

    def test_identity_drift_after_start_stops_before_finishing_run(self) -> None:
        task = self._create_task()
        with (
            mock.patch.object(
                engine_module,
                "_reverify_bound_variant",
                side_effect=[
                    None,
                    ValueError("data_identity_drift: changed"),
                ],
            ),
            mock.patch.object(
                engine_module,
                "runner_start",
                return_value={"status": "running", "process_id": 123},
            ),
            mock.patch.object(
                engine_module,
                "runner_stop",
                return_value={"status": "stopped"},
            ) as stop,
            mock.patch.object(
                engine_module,
                "runner_status",
                side_effect=AssertionError("status must not race with cleanup"),
            ),
        ):
            result = execute_task(
                self.project,
                task["id"],
                purpose="debug",
            )

        stop.assert_called_once()
        run = load_run(self.project, result["run"]["id"])
        self.assertIn(run["execution"]["stage"], {"finished", "closed"})
        self.assertEqual("failed", run["execution"]["outcome"])
        self.assertEqual("prerequisite", run["execution"]["issue_kind"])
        self.assertIn(
            "data_identity_drift",
            run["analysis"]["limitations"],
        )
        self.assertNotIn(
            current_evidence_level(self.project, run["id"]),
            {"single_run", "confirmed"},
        )

    def test_bound_outputs_are_confined_to_the_unique_run_output_root(
        self,
    ) -> None:
        task = self._ready_task(self._create_task())
        run = claim_run(
            self.project,
            task["id"],
            self._bound_frozen(),
            purpose="debug",
        )
        snapshot_root = (
            self.control / run["execution_snapshot"]["relative_path"]
        )
        output_root = snapshot_root / ".cv-workflow-output"
        (output_root / "inside.log").write_text("ok\n", encoding="utf-8")
        (output_root / "inside.json").write_text("{}\n", encoding="utf-8")

        class ParsedAdapter:
            @staticmethod
            def parse_result(project, current_run):
                return {
                    "execution": {
                        "outcome": "succeeded",
                        "exit_code": 0,
                        "issue_kind": None,
                    },
                    "result": {
                        "metrics": {"score": 0.8},
                        "raw_log": "outside.log",
                    },
                    "quality": {
                        "implementation": "valid",
                        "interface": "valid",
                        "data": "valid",
                        "metrics": "valid",
                    },
                    "analysis": {
                        "hypothesis": "supported",
                        "limitations": [],
                        "suggestions": [],
                    },
                    "artifacts": [
                        ".cv-workflow-output/inside.json",
                        "outside.json",
                    ],
                }

        with self.assertRaisesRegex(
            ValueError,
            "output|raw_log|artifact",
        ):
            runner_collect(
                snapshot_root,
                ParsedAdapter(),
                run,
                purpose="debug",
                backend="project",
            )

        outside = self.root / "outside.log"
        outside.write_text("outside\n", encoding="utf-8")
        os.link(outside, output_root / "linked.log")

        class HardlinkAdapter(ParsedAdapter):
            @staticmethod
            def parse_result(project, current_run):
                parsed = ParsedAdapter.parse_result(project, current_run)
                parsed["result"]["raw_log"] = (
                    ".cv-workflow-output/linked.log"
                )
                parsed["artifacts"] = []
                return parsed

        with self.assertRaisesRegex(
            ValueError,
            "owned regular|hardlink|output",
        ):
            runner_collect(
                snapshot_root,
                HardlinkAdapter(),
                run,
                purpose="debug",
                backend="project",
            )

        class ValidAdapter(ParsedAdapter):
            @staticmethod
            def parse_result(project, current_run):
                parsed = ParsedAdapter.parse_result(project, current_run)
                parsed["result"]["raw_log"] = (
                    ".cv-workflow-output/inside.log"
                )
                parsed["artifacts"] = [
                    ".cv-workflow-output/inside.json",
                ]
                return parsed

        parsed = runner_collect(
            snapshot_root,
            ValidAdapter(),
            run,
            purpose="debug",
            backend="project",
        )
        self.assertEqual(
            ".cv-workflow-output/inside.log",
            parsed["result"]["raw_log"],
        )

    def test_snapshot_sources_are_read_only_but_output_root_is_writable(
        self,
    ) -> None:
        task = self._ready_task(self._create_task())
        run = claim_run(
            self.project,
            task["id"],
            self._bound_frozen(),
            purpose="debug",
        )
        snapshot_root = (
            self.control / run["execution_snapshot"]["relative_path"]
        )
        adapter_path = snapshot_root / "workflow_adapter.py"
        write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        self.assertEqual(0, adapter_path.stat().st_mode & write_bits)

        output = snapshot_root / ".cv-workflow-output" / "probe.txt"
        output.write_text("ok\n", encoding="utf-8")
        self.assertEqual("ok\n", output.read_text(encoding="utf-8"))

    def test_snapshot_detects_an_extra_empty_source_directory(self) -> None:
        task = self._ready_task(self._create_task())
        run = claim_run(
            self.project,
            task["id"],
            self._bound_frozen(),
            purpose="debug",
        )
        snapshot_root = (
            self.control / run["execution_snapshot"]["relative_path"]
        )
        (snapshot_root / "extra-empty-directory").mkdir()

        with self.assertRaisesRegex(ValueError, "extra|directory|snapshot"):
            engine_module.verify_project_run_execution_snapshot(
                self.project,
                run["id"],
            )

    def test_git_replace_refs_cannot_change_the_bound_commit_tree(self) -> None:
        original_commit = self.commit
        original_adapter = (self.repo / "workflow_adapter.py").read_bytes()
        (self.repo / "workflow_adapter.py").write_text(
            self._adapter_source().replace(
                '{"score": 0.8}',
                '{"score": 0.01}',
            ),
            encoding="utf-8",
            newline="\n",
        )
        self._git(self.repo, "add", "workflow_adapter.py")
        self._git(self.repo, "commit", "-m", "replacement tree")
        replacement_commit = self._git(self.repo, "rev-parse", "HEAD")
        self._git(self.repo, "reset", "--hard", original_commit)
        self._git(self.repo, "replace", original_commit, replacement_commit)
        self._git(self.repo, "reset", "--hard", original_commit)
        self.commit = original_commit

        with engine_module.temporary_commit_snapshot(
            self.repo,
            original_commit,
        ) as snapshot_root:
            self.assertEqual(
                original_adapter,
                (snapshot_root / "workflow_adapter.py").read_bytes(),
            )

    def test_namespace_package_never_falls_back_to_live_python_paths(
        self,
    ) -> None:
        namespace = self.repo / "namespace_pkg"
        namespace.mkdir()
        (namespace / "placeholder.txt").write_text(
            "committed namespace marker\n",
            encoding="utf-8",
        )
        (self.repo / "workflow_adapter.py").write_text(
            "from namespace_pkg import secret as _secret\n"
            + self._adapter_source(),
            encoding="utf-8",
            newline="\n",
        )
        self._git(self.repo, "add", "namespace_pkg", "workflow_adapter.py")
        self._git(self.repo, "commit", "-m", "namespace adapter")
        self.commit = self._git(self.repo, "rev-parse", "HEAD")
        (namespace / "secret.py").write_text(
            "VALUE = 'live-only'\n",
            encoding="utf-8",
        )

        task = self._create_task()
        before = copy.deepcopy(load_task(self.project, task["id"]))
        sys.path.insert(0, str(self.repo))
        try:
            with self.assertRaisesRegex(ValueError, "Adapter|snapshot|import"):
                execute_task(self.project, task["id"], purpose="debug")
        finally:
            sys.path.remove(str(self.repo))
        self.assertEqual(before, load_task(self.project, task["id"]))
        self.assertEqual([], list((self.control / "runs").iterdir()))

    def test_snapshot_adapter_git_cannot_discover_an_ancestor_repository(
        self,
    ) -> None:
        snapshot_root = self.repo / "nested-execution-snapshot"
        snapshot_root.mkdir()
        source = """\
import subprocess as _subprocess


def inspect(project):
    probe = _subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {"returncode": probe.returncode}


def validate(project):
    return []


def prepare_runs(project, task):
    return [{"code": {}, "config": {}, "seed": 1, "data": {}, "environment": {}}]


def execute(project, action, run):
    if action == "start":
        return {"status": "finished", "process_id": None}
    if action == "status":
        return {"status": "finished"}
    return {"status": "stopped"}


def parse_result(project, run):
    return {
        "execution": {"outcome": "succeeded", "exit_code": 0, "issue_kind": None},
        "result": {"metrics": {}, "raw_log": None},
        "quality": {
            "implementation": "valid",
            "interface": "valid",
            "data": "valid",
            "metrics": "valid",
        },
        "analysis": {
            "hypothesis": "supported",
            "limitations": [],
            "suggestions": [],
        },
        "artifacts": [],
    }
"""
        adapter_path = snapshot_root / "workflow_adapter.py"
        adapter_path.write_text(source, encoding="utf-8", newline="\n")
        adapter_path.chmod(0o444)
        try:
            adapter = load_snapshot_adapter(snapshot_root, self.commit)
            with self.assertRaisesRegex(
                PermissionError,
                "只允许受控的 python",
            ):
                adapter.inspect(snapshot_root)
        finally:
            adapter_path.chmod(0o600)

    def test_snapshot_cleanup_rejects_invalid_run_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "Run ID|run_id"):
            snapshot_module.remove_run_execution_snapshot_locked(
                self.control,
                "../RUN-0001",
            )

    def test_snapshot_cleanup_preserves_a_replaced_manifest(self) -> None:
        task = self._ready_task(self._create_task())
        run = claim_run(
            self.project,
            task["id"],
            self._bound_frozen(),
            purpose="debug",
        )
        manifest = (
            self.control / run["execution_snapshot"]["manifest_path"]
        )
        competitor = b'{"competitor": true}\n'
        original_remove = snapshot_module._remove_owned_tree

        def replace_manifest_after_snapshot_cleanup(*args, **kwargs):
            original_remove(*args, **kwargs)
            manifest.write_bytes(competitor)

        with mock.patch.object(
            snapshot_module,
            "_remove_owned_tree",
            side_effect=replace_manifest_after_snapshot_cleanup,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "identity changed|identity_changed",
            ):
                snapshot_module.remove_run_execution_snapshot_locked(
                    self.control,
                    run["id"],
                )
        self.assertEqual(competitor, manifest.read_bytes())

    def test_data_change_between_prepare_and_start_is_disqualified_with_fixed_reason(
        self,
    ) -> None:
        probe = self._install_data_probe_adapter(mutate_on_start=False)
        task = self._create_task(
            binding=self._binding(),
            config={
                "learning_rate": 0.001,
                "probe_path": str(probe),
            },
        )
        original_claim = engine_module.claim_run

        def mutate_after_claim(*args: object, **kwargs: object) -> dict[str, object]:
            run = original_claim(*args, **kwargs)
            probe.write_text("changed-before-start\n", encoding="utf-8")
            return run

        with mock.patch(
            "workflow_core.engine.claim_run",
            side_effect=mutate_after_claim,
        ):
            result = execute_task(
                self.project,
                task["id"],
                purpose="evidence",
            )
        run = load_run(self.project, result["run"]["id"])
        self.assertEqual("failed", run["execution"]["outcome"])
        self.assertEqual("prerequisite", run["execution"]["issue_kind"])
        self.assertEqual(
            ["data_identity_drift"],
            run["analysis"]["limitations"],
        )
        self.assertEqual("disqualified", result["evidence_level"])
        self.assertNotIn(
            result["evidence_level"],
            {"single_run", "confirmed"},
        )

    def test_adapter_cannot_change_external_data_during_start(self) -> None:
        probe = self._install_data_probe_adapter(mutate_on_start=True)
        task = self._create_task(
            binding=self._binding(),
            config={
                "learning_rate": 0.001,
                "probe_path": str(probe),
            },
        )
        result = execute_task(
            self.project,
            task["id"],
            purpose="evidence",
        )
        run = load_run(self.project, result["run"]["id"])
        self.assertEqual("original\n", probe.read_text(encoding="utf-8"))
        self.assertEqual("failed", run["execution"]["outcome"])
        self.assertEqual(
            ["runner_start_failed"],
            run["analysis"]["limitations"],
        )
        self.assertEqual("disqualified", result["evidence_level"])


if __name__ == "__main__":
    unittest.main()
