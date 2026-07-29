from __future__ import annotations

import importlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._helpers import SCRIPTS, cli_json, read_json, run_cli


class RenderValidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        cli_json("init", "--path", self.project, "--name", "render test")
        idea = cli_json(
            "new-idea", "--project", self.project,
            "--title", "adapter", "--note", "render",
        )
        cli_json(
            "activate-idea", "--project", self.project, "--idea", idea["id"],
            "--problem", "domain shift", "--mechanism", "adapter",
            "--hypothesis", "testable",
        )
        self.version = cli_json(
            "register-version", "--project", self.project, "--name", "baseline",
            "--repo-url", "https://example.invalid/repo", "--commit", "a" * 40,
        )
        self.trial = cli_json(
            "new-trial", "--project", self.project, "--idea", idea["id"],
            "--base-version", self.version["id"],
            "--template-family", "feature-adapter",
            "--attachment-point", "feature-output",
        )
        self.control = self.project / ".experiment-workflow"
        self.asset_id = self.trial["code_asset_id"]
        self.asset_dir = self.control / "code-assets" / self.asset_id

    def sync(self, *, check: bool = True):
        return run_cli(
            "sync-code-asset", "--project", self.project,
            "--asset", self.asset_id, check=check,
        )

    def _workflow_modules(self):
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        return importlib.import_module("workflow_core.attempts")

    def _workflow_attempts_and_templates(self):
        attempts = self._workflow_modules()
        return attempts, importlib.import_module("workflow_core.templates")

    def _assert_size_precedes_unbounded_read(
        self, path: Path, expected: str, operation,
    ) -> None:
        original = Path.read_bytes

        def reject_target_read(candidate: Path) -> bytes:
            if candidate == path:
                raise AssertionError(f"触发无界 Path.read_bytes：{candidate}")
            return original(candidate)

        with mock.patch.object(Path, "read_bytes", reject_target_read):
            with self.assertRaisesRegex(ValueError, expected):
                operation()

    def _assert_json_size_precedes_unbounded_read(
        self, path: Path, operation,
    ) -> None:
        original_bytes = Path.read_bytes
        original_text = Path.read_text

        def reject_bytes(candidate: Path) -> bytes:
            if candidate == path:
                raise AssertionError(f"触发无界 Path.read_bytes：{candidate}")
            return original_bytes(candidate)

        def reject_text(candidate: Path, *args, **kwargs) -> str:
            if candidate == path:
                raise AssertionError(f"触发无界 Path.read_text：{candidate}")
            return original_text(candidate, *args, **kwargs)

        with (
            mock.patch.object(Path, "read_bytes", reject_bytes),
            mock.patch.object(Path, "read_text", reject_text),
        ):
            with self.assertRaisesRegex(ValueError, "1 MiB"):
                operation()

    @staticmethod
    def _write_sparse_oversize(path: Path, limit: int) -> None:
        with path.open("wb") as output:
            output.seek(limit)
            output.write(b"x")

    @staticmethod
    def _snapshot_tree(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def _assert_commands_reject_control_json_without_writes(self) -> None:
        attempts, templates = self._workflow_attempts_and_templates()
        before = self._snapshot_tree(self.control)
        operations = (
            ("validate", lambda: attempts.validate_project_workflow(self.project)),
            ("sync", lambda: templates.sync_code_asset(self.project, self.asset_id)),
            (
                "new-attempt",
                lambda: attempts.new_attempt(
                    self.project,
                    self.version["id"],
                    "innovation",
                    7,
                    "python train.py",
                    {},
                ),
            ),
        )
        for name, operation in operations:
            with self.subTest(operation=name), (
                mock.patch.object(
                    templates,
                    "atomic_write_json",
                    side_effect=AssertionError("invalid JSON 后不得写 asset.json"),
                )
            ), mock.patch.object(
                templates,
                "_atomic_write_bytes",
                side_effect=AssertionError("invalid JSON 后不得写 framework.html"),
            ), mock.patch.object(
                attempts,
                "_create_record",
                side_effect=AssertionError("invalid JSON 后不得创建 Attempt"),
            ):
                with self.assertRaises(ValueError):
                    operation()
            self.assertEqual(before, self._snapshot_tree(self.control))

    def test_new_trial_generates_deterministic_offline_framework_from_real_ast(self) -> None:
        framework = self.asset_dir / "framework.html"
        self.assertTrue(framework.is_file())
        first = framework.read_bytes()
        text = first.decode("utf-8")
        self.assertIn("<svg", text)
        self.assertRegex(text, r"render-input-digest: sha256:[0-9a-f]{64}")
        for visible in (
            self.asset_id, "CodeAsset", "template", "attachment",
            "provenance", "FeatureAdapter", "__init__", "__call__", "图例",
        ):
            self.assertIn(visible, text)
        self.assertNotIn("<script", text.lower())
        self.assertNotRegex(text.lower(), r"(?:src|href)\s*=\s*['\"]https?://")
        self.assertNotIn(str(self.project), text)
        self.assertNotIn(self.trial["created_at"], text)
        self.assertNotIn("sample_project", text)
        self.assertNotIn("framework.html", read_json(self.asset_dir / "asset.json")["files"])

        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        rendering = importlib.import_module("workflow_core.rendering")
        second = rendering.render_code_asset(self.asset_dir)
        self.assertEqual(first, second)

    def test_renderer_escapes_every_metadata_text_and_has_explicit_non_data_edges(self) -> None:
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        rendering = importlib.import_module("workflow_core.rendering")
        asset = read_json(self.asset_dir / "asset.json")
        asset["template_family"] = '<img src=x onerror="boom">'
        rendered = rendering.render_framework_html(
            asset,
            (self.asset_dir / "module.py").read_bytes(),
            (self.asset_dir / "test_contract.py").read_bytes(),
            (self.asset_dir / "provenance.json").read_bytes(),
        ).decode("utf-8")
        self.assertNotIn("<img", rendered)
        self.assertIn("&lt;img src=x onerror=&quot;boom&quot;&gt;", rendered)
        self.assertIn('data-edge-kind="containment"', rendered)
        self.assertIn('data-edge-kind="verification"', rendered)
        self.assertIn('data-edge-kind="boundary"', rendered)
        self.assertNotIn("execution data flow", rendered.lower())

    def test_dense_module_is_summarized_and_responsive_without_faking_flow(self) -> None:
        dense = "\n".join(f"def symbol_{index}():\n    return {index}\n" for index in range(20))
        (self.asset_dir / "module.py").write_text(dense, encoding="utf-8")
        self.sync()
        text = (self.asset_dir / "framework.html").read_text(encoding="utf-8")
        symbol_nodes = text.count("data-symbol-node")
        self.assertLessEqual(symbol_nodes, 9)
        self.assertGreaterEqual(
            text.count('data-edge-kind="containment"'), symbol_nodes,
            "每个可见或汇总 AST 节点都必须有明确 containment 拓扑",
        )
        self.assertIn("其余 12 个符号", text)
        self.assertIn("@media (max-width: 840px)", text)
        self.assertRegex(text, r"max-width\s*:\s*100%")
        before = (self.asset_dir / "framework.html").read_bytes()
        self.sync()
        self.assertEqual(before, (self.asset_dir / "framework.html").read_bytes())

    def test_validate_detects_missing_manual_edit_and_input_staleness(self) -> None:
        framework = self.asset_dir / "framework.html"
        original = framework.read_bytes()
        framework.unlink()
        missing = run_cli("validate", "--project", self.project, check=False)
        self.assertIn("stale framework.html", missing.stdout + missing.stderr)
        framework.write_bytes(original + b"\nmanual")
        edited = run_cli("validate", "--project", self.project, check=False)
        self.assertIn("stale framework.html", edited.stdout + edited.stderr)
        framework.write_bytes(original)

        for filename, content in (
            ("module.py", b"changed = True\n"),
            ("test_contract.py", b"def test_changed():\n    return True\n"),
            ("provenance.json", (self.asset_dir / "provenance.json").read_bytes() + b" "),
        ):
            path = self.asset_dir / filename
            prior = path.read_bytes()
            path.write_bytes(content)
            stale = run_cli("validate", "--project", self.project, check=False)
            self.assertIn("stale framework.html", stale.stdout + stale.stderr, filename)
            path.write_bytes(prior)

        asset_path = self.asset_dir / "asset.json"
        asset = read_json(asset_path)
        asset["template_selection_reason"] += " changed"
        asset_path.write_text(json.dumps(asset), encoding="utf-8")
        stale = run_cli("validate", "--project", self.project, check=False)
        self.assertIn("stale framework.html", stale.stdout + stale.stderr)

    def test_sync_updates_only_derived_facts_and_refuses_frozen_asset(self) -> None:
        module = self.asset_dir / "module.py"
        contract = self.asset_dir / "test_contract.py"
        provenance = self.asset_dir / "provenance.json"
        module.write_text("def innovation():\n    return 1\n", encoding="utf-8")
        contract.write_text("def test_contract():\n    return True\n", encoding="utf-8")
        provenance_payload = read_json(provenance)
        provenance_payload["what_is_new"] = ["local innovation"]
        provenance.write_text(json.dumps(provenance_payload), encoding="utf-8")
        source_snapshot = {p.name: p.read_bytes() for p in (module, contract, provenance)}

        synced = json.loads(self.sync().stdout)
        self.assertEqual(self.asset_id, synced["id"])
        self.assertEqual(source_snapshot, {p.name: p.read_bytes() for p in (module, contract, provenance)})
        self.assertEqual(0, run_cli("validate", "--project", self.project).returncode)

        cli_json(
            "new-attempt", "--project", self.project, "--target", self.trial["id"],
            "--type", "innovation", "--seed", 1, "--command", "python train.py",
        )
        module.write_text("def frozen_change():\n    return 2\n", encoding="utf-8")
        rejected = self.sync(check=False)
        self.assertIn("Attempt", rejected.stdout + rejected.stderr)
        self.assertEqual("def frozen_change():\n    return 2\n", module.read_text(encoding="utf-8"))

    def test_sync_rejects_shape_valid_but_semantically_tampered_attempt_before_writes(self) -> None:
        attempts, templates = self._workflow_attempts_and_templates()
        attempt = cli_json(
            "new-attempt", "--project", self.project, "--target", self.trial["id"],
            "--type", "innovation", "--seed", 7, "--command", "python train.py",
        )
        attempt_path = self.control / "attempts" / f"{attempt['id']}.json"
        tampered = read_json(attempt_path)
        tampered["ordered_code_asset_ids"] = []
        tampered["frozen_identity_digest"] = attempts._identity_digest(tampered)
        attempt_path.write_text(json.dumps(tampered), encoding="utf-8")

        module = self.asset_dir / "module.py"
        module.write_text("def user_edit():\n    return 9\n", encoding="utf-8")
        asset_before = (self.asset_dir / "asset.json").read_bytes()
        framework_before = (self.asset_dir / "framework.html").read_bytes()

        with self.assertRaisesRegex(ValueError, "frozen target|ledger invalid"):
            templates.sync_code_asset(self.project, self.asset_id)

        self.assertEqual("def user_edit():\n    return 9\n", module.read_text(encoding="utf-8"))
        self.assertEqual(asset_before, (self.asset_dir / "asset.json").read_bytes())
        self.assertEqual(framework_before, (self.asset_dir / "framework.html").read_bytes())

    def test_sync_and_validate_enforce_python_utf8_size_and_line_boundaries(self) -> None:
        module = self.asset_dir / "module.py"
        cases = (
            (b"def broken(:\n", "Python"),
            (b"\xff", "UTF-8"),
            (b"x = 1\n" * 501, "500"),
            (b"#" + b"x" * (64 * 1024), "64 KiB"),
        )
        for content, message in cases:
            module.write_bytes(content)
            failed = self.sync(check=False)
            self.assertIn(message, failed.stdout + failed.stderr)
            self.assertEqual(content, module.read_bytes())

    def test_validate_and_new_attempt_reject_sparse_module_before_unbounded_read(self) -> None:
        attempts = self._workflow_modules()
        module = self.asset_dir / "module.py"
        with module.open("wb") as output:
            output.seek(64 * 1024)
            output.write(b"x")

        self._assert_size_precedes_unbounded_read(
            module, "64 KiB", lambda: attempts.validate_project_workflow(self.project),
        )
        self._assert_size_precedes_unbounded_read(
            module,
            "64 KiB",
            lambda: attempts.new_attempt(
                self.project, self.trial["id"], "innovation", 3,
                "python train.py", {},
            ),
        )
        self.assertEqual([], list((self.control / "attempts").glob("*.json")))

    def test_validate_rejects_oversized_contract_before_unbounded_read(self) -> None:
        attempts = self._workflow_modules()
        contract = self.asset_dir / "test_contract.py"
        with contract.open("wb") as output:
            output.write(b"#" * (64 * 1024 + 1))
        self._assert_size_precedes_unbounded_read(
            contract, "64 KiB", lambda: attempts.validate_project_workflow(self.project),
        )

    def test_commands_reject_oversized_asset_json_before_unbounded_read(self) -> None:
        attempts, templates = self._workflow_attempts_and_templates()
        path = self.asset_dir / "asset.json"
        self._write_sparse_oversize(path, 1024 * 1024)
        operations = (
            lambda: attempts.validate_project_workflow(self.project),
            lambda: templates.sync_code_asset(self.project, self.asset_id),
            lambda: attempts.new_attempt(
                self.project, self.trial["id"], "innovation", 1,
                "python train.py", {},
            ),
        )
        for operation in operations:
            self._assert_json_size_precedes_unbounded_read(path, operation)

    def test_commands_reject_oversized_attempt_json_before_unbounded_read(self) -> None:
        attempts, templates = self._workflow_attempts_and_templates()
        attempt = cli_json(
            "new-attempt", "--project", self.project, "--target", self.trial["id"],
            "--type", "innovation", "--seed", 1, "--command", "python train.py",
        )
        path = self.control / "attempts" / f"{attempt['id']}.json"
        self._write_sparse_oversize(path, 1024 * 1024)
        operations = (
            lambda: attempts.validate_project_workflow(self.project),
            lambda: templates.sync_code_asset(self.project, self.asset_id),
            lambda: attempts.new_attempt(
                self.project, self.trial["id"], "innovation", 2,
                "python train.py", {},
            ),
        )
        for operation in operations:
            self._assert_json_size_precedes_unbounded_read(path, operation)

    def test_commands_reject_oversized_artifact_index_before_unbounded_read(self) -> None:
        attempts, templates = self._workflow_attempts_and_templates()
        path = self.control / "artifacts" / "index.json"
        self._write_sparse_oversize(path, 1024 * 1024)
        operations = (
            lambda: attempts.validate_project_workflow(self.project),
            lambda: templates.sync_code_asset(self.project, self.asset_id),
            lambda: attempts.new_attempt(
                self.project, self.trial["id"], "innovation", 1,
                "python train.py", {},
            ),
        )
        for operation in operations:
            self._assert_json_size_precedes_unbounded_read(path, operation)

    def test_all_control_json_is_strictly_read_before_command_writes(self) -> None:
        cases = (
            ("invalid-syntax", "{"),
            ("array", "[]"),
            ("nan", '{"value": NaN}'),
        )
        for relative in ("workflow.lock.json", "agents/bad.json"):
            path = self.control / relative
            for label, content in cases:
                with self.subTest(path=relative, case=label):
                    path.write_text(content, encoding="utf-8")
                    self._assert_commands_reject_control_json_without_writes()

    def test_workflow_lock_semantic_drift_is_rejected_before_command_writes(self) -> None:
        lock = self.control / "workflow.lock.json"
        lock.write_text(
            json.dumps(
                {
                    "schema": "cv-experiment-workflow.workflow-lock.v1",
                    "skill_id": "cv-experiment-workflow",
                    "version": "9.9.9",
                }
            ),
            encoding="utf-8",
        )
        self._assert_commands_reject_control_json_without_writes()

    def test_json_writers_reject_oversized_payload_without_target_or_temp(self) -> None:
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        workflow_io = importlib.import_module("workflow_core.io")
        root = Path(self.temporary.name) / "json-size"
        root.mkdir()
        payload = {"value": "x" * workflow_io.DEFAULT_JSON_LIMIT}
        for operation in ("write", "create"):
            destination = root / f"{operation}.json"
            transaction_id = ("a" if operation == "write" else "b") * 32
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "1 MiB"):
                    if operation == "write":
                        workflow_io.atomic_write_json(
                            destination, payload, transaction_id=transaction_id,
                        )
                    else:
                        workflow_io.atomic_create_json(
                            destination, payload, transaction_id=transaction_id,
                        )
                self.assertFalse(destination.exists())
                self.assertFalse(
                    workflow_io.transaction_temp_path(destination, transaction_id).exists()
                )
        self.assertEqual([], list(root.iterdir()))

    def test_atomic_create_existing_target_keeps_false_for_oversized_payload(self) -> None:
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        workflow_io = importlib.import_module("workflow_core.io")
        root = Path(self.temporary.name) / "json-existing"
        root.mkdir()
        destination = root / "state.json"
        original = b'{"existing": true}\n'
        destination.write_bytes(original)
        transaction_id = "c" * 32
        created = workflow_io.atomic_create_json(
            destination,
            {"value": "x" * workflow_io.DEFAULT_JSON_LIMIT},
            transaction_id=transaction_id,
        )
        self.assertFalse(created)
        self.assertEqual(original, destination.read_bytes())
        self.assertFalse(
            workflow_io.transaction_temp_path(destination, transaction_id).exists()
        )

    def test_new_attempt_rejects_oversized_config_without_file_or_temp(self) -> None:
        attempts = self._workflow_modules()
        before = self._snapshot_tree(self.control / "attempts")
        with self.assertRaisesRegex(ValueError, "1 MiB"):
            attempts.new_attempt(
                self.project,
                self.version["id"],
                "innovation",
                8,
                "python train.py",
                {"value": "x" * (1024 * 1024)},
            )
        self.assertEqual(before, self._snapshot_tree(self.control / "attempts"))

    def test_validate_rejects_artifact_payload_links_deny_extensions_and_residuals(self) -> None:
        artifacts = self.control / "artifacts"
        forbidden = artifacts / "model.ckpt"
        forbidden.write_bytes(b"reference payload must stay outside")
        result = run_cli("validate", "--project", self.project, check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(str(forbidden), result.stdout + result.stderr)
        forbidden.unlink()

        denied = self.control / "agents" / "weights.onnx"
        denied.write_bytes(b"x")
        result = run_cli("validate", "--project", self.project, check=False)
        self.assertIn("weights.onnx", result.stdout + result.stderr)
        denied.unlink()

        residual = self.control / ".runtime" / ".asset.cvexp-deadbeef.tmp"
        residual.write_bytes(b"x")
        result = run_cli("validate", "--project", self.project, check=False)
        self.assertIn(str(residual), result.stdout + result.stderr)
        residual.unlink()

        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(b"outside")
        link = artifacts / "linked.json"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("当前平台不允许创建符号链接")
        result = run_cli("validate", "--project", self.project, check=False)
        self.assertIn(str(link), result.stdout + result.stderr)
        self.assertEqual(b"outside", outside.read_bytes())

    def test_validate_keeps_counts_and_adds_frameworks(self) -> None:
        result = cli_json("validate", "--project", self.project)
        self.assertTrue(result["valid"])
        self.assertEqual(1, result["trials"])
        self.assertEqual(1, result["code_assets"])
        self.assertEqual(1, result["frameworks"])
        self.assertEqual(0, result["templates"])
        self.assertEqual(0, result["attempts"])
        self.assertEqual(1, result["versions"])

    def test_control_plane_rejects_unknown_entries_and_object_extras(self) -> None:
        cases: list[tuple[Path, bool, bytes | None]] = [
            (self.control / "extensionless", False, None),
            (self.control / "unknown.txt", False, b"unknown"),
            (self.control / "unknown.json", False, b"{}"),
            (self.control / "unknown-dir", True, None),
            (self.asset_dir / "extra.txt", False, b"extra"),
            (self.control / "ideas" / "IDEA-9999" / "nested.json", False, b"{}"),
            (self.control / "attempts" / "UNKNOWN-0001.json", False, b"{}"),
        ]
        for path, directory, content in cases:
            with self.subTest(path=path.relative_to(self.control)):
                path.parent.mkdir(parents=True, exist_ok=True)
                if directory:
                    path.mkdir()
                elif path.name == "extensionless":
                    self._write_sparse_oversize(path, 2 * 1024 * 1024)
                else:
                    path.write_bytes(content or b"")
                try:
                    result = run_cli("validate", "--project", self.project, check=False)

                    self.assertNotEqual(0, result.returncode)
                    reported = path.parent if path.parent.name == "IDEA-9999" else path
                    self.assertIn(str(reported), result.stdout + result.stderr)
                finally:
                    if directory:
                        path.rmdir()
                    else:
                        path.unlink()
                    if path.parent.name == "IDEA-9999":
                        path.parent.rmdir()

        self.assertEqual(0, run_cli("validate", "--project", self.project).returncode)

    def test_result_artifact_is_only_a_normalized_reference(self) -> None:
        attempt = cli_json(
            "new-attempt", "--project", self.project, "--target", self.trial["id"],
            "--type", "innovation", "--seed", 1, "--command", "python train.py",
        )
        completed = cli_json(
            "record-result", "--project", self.project, "--attempt", attempt["id"],
            "--metrics", '{"score": 1}', "--decision", "accept",
            "--conclusion", "ok", "--artifacts", '[{"path":"runs/model.ckpt"}]',
        )
        self.assertEqual([{"path": "runs/model.ckpt"}], completed["result"]["artifacts"])
        self.assertFalse((self.control / "artifacts" / "model.ckpt").exists())


if __name__ == "__main__":
    unittest.main()
