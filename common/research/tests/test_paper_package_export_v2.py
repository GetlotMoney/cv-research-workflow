from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from tests import test_paper_package_v2 as package_tests
from tests._helpers import SCRIPTS, cli_json, run_cli


sys.path.insert(0, str(SCRIPTS))
from workflow_core import paper_package as paper_package_module  # noqa: E402
from workflow_core import paper_package_delivery as delivery_module  # noqa: E402


class PaperPackageExportV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = package_tests.PaperPackageV2Tests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.project = self.fixture.project
        self.control = self.fixture.control

    def _seal(self, *, mode: str = "hybrid") -> dict[str, object]:
        task, run = self.fixture._confirmed_run()
        return self.fixture._seal(
            self.fixture._selection(task, run, asset_mode=mode)
        )

    @staticmethod
    def _rewrite_handoff_integrity(package: Path) -> None:
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        science = {
            name: (package / name).read_bytes()
            for name in paper_package_module.SCIENTIFIC_FILES
        }
        manifest["content_sha256"] = paper_package_module._content_sha256(
            science
        )
        for row in manifest["delivery"]["files"]:
            path = package / row["path"]
            content = path.read_bytes()
            row["size_bytes"] = len(content)
            row["sha256"] = (
                "sha256:" + hashlib.sha256(content).hexdigest()
            )
        manifest_path.write_bytes(
            paper_package_module._canonical_json_line(manifest)
        )
        checksum_paths = [
            "manifest.json",
            *paper_package_module.SCIENTIFIC_FILES,
            *[
                row["path"]
                for row in manifest["delivery"]["files"]
                if row["path"].startswith("assets/included/")
            ],
        ]
        (package / "checksums.sha256").write_bytes(
            b"".join(
                (
                    f"{hashlib.sha256((package / name).read_bytes()).hexdigest()}"
                    f"  {name}\n"
                ).encode("utf-8")
                for name in sorted(
                    checksum_paths,
                    key=lambda value: value.encode("utf-8"),
                )
            )
        )

    @staticmethod
    def _staging_paths(parent: Path) -> list[Path]:
        return [
            item
            for item in parent.iterdir()
            if delivery_module._is_handoff_staging_name(item.name)
        ]

    def test_hybrid_export_has_fixed_layout_and_verifies_standalone(self) -> None:
        sealed = self._seal(mode="hybrid")
        package_id = str(sealed["package_id"])
        output = self.root / package_id

        result = paper_package_module.export_paper_package(
            self.project,
            package_id,
            "hybrid",
            output,
        )

        self.assertEqual("exported", result["status"])
        self.assertEqual(package_id, result["package_id"])
        self.assertEqual(
            {
                "manifest.json",
                "study.json",
                "claims.jsonl",
                "experiments.json",
                "sources.jsonl",
                "assets.jsonl",
                "visuals.json",
                "checksums.sha256",
                "assets",
            },
            {item.name for item in output.iterdir()},
        )
        manifest = json.loads((output / "manifest.json").read_text("utf-8"))
        internal = json.loads(
            (
                self.control
                / "paper-packages"
                / "packages"
                / package_id
                / "package.json"
            ).read_text("utf-8")
        )
        self.assertEqual("cv-research-handoff/v2", manifest["schema"])
        self.assertEqual(
            {**internal, "schema": "cv-research-handoff/v2"},
            {key: value for key, value in manifest.items() if key != "delivery"},
        )
        self.assertEqual(1, manifest["delivery"]["layout_revision"])
        self.assertEqual(
            list(paper_package_module.SCIENTIFIC_FILES),
            [
                row["path"]
                for row in manifest["delivery"]["files"]
                if not row["path"].startswith("assets/included/")
            ],
        )
        self.assertEqual("pass", paper_package_module.verify_paper_package(output)["status"])

        shutil.rmtree(self.project)
        self.assertEqual("pass", paper_package_module.verify_paper_package(output)["status"])

    def test_full_export_cli_and_mode_and_destination_gates(self) -> None:
        sealed = self._seal(mode="full")
        package_id = str(sealed["package_id"])
        output = self.root / package_id

        result = cli_json(
            "export-paper-package",
            "--project",
            self.project,
            "--package",
            package_id,
            "--mode",
            "full",
            "--out",
            output,
        )
        self.assertEqual("exported", result["status"])
        verified = cli_json(
            "verify-paper-package",
            "--package-dir",
            output,
        )
        self.assertEqual("pass", verified["status"])

        (self.root / "wrong-mode").mkdir()
        wrong_mode = run_cli(
            "export-paper-package",
            "--project",
            self.project,
            "--package",
            package_id,
            "--mode",
            "hybrid",
            "--out",
            self.root / "wrong-mode" / package_id,
            check=False,
        )
        self.assertNotEqual(0, wrong_mode.returncode)
        self.assertIn("asset_mode", wrong_mode.stderr)

        wrong_name = run_cli(
            "export-paper-package",
            "--project",
            self.project,
            "--package",
            package_id,
            "--mode",
            "full",
            "--out",
            self.root / "not-the-package-id",
            check=False,
        )
        self.assertNotEqual(0, wrong_name.returncode)
        self.assertIn("PKG", wrong_name.stderr)

        existing = run_cli(
            "export-paper-package",
            "--project",
            self.project,
            "--package",
            package_id,
            "--mode",
            "full",
            "--out",
            output,
            check=False,
        )
        self.assertNotEqual(0, existing.returncode)
        self.assertIn("存在", existing.stderr)

    def test_verifier_rejects_tamper_and_unknown_file(self) -> None:
        sealed = self._seal()
        package_id = str(sealed["package_id"])
        tampered = self.root / package_id
        paper_package_module.export_paper_package(
            self.project,
            package_id,
            "hybrid",
            tampered,
        )
        (tampered / "study.json").write_bytes(
            (tampered / "study.json").read_bytes() + b" "
        )
        with self.assertRaisesRegex(ValueError, "checksum|sha256|大小"):
            paper_package_module.verify_paper_package(tampered)

        second = self.root / "second" / package_id
        second.parent.mkdir()
        paper_package_module.export_paper_package(
            self.project,
            package_id,
            "hybrid",
            second,
        )
        (second / "unexpected.txt").write_text("surprise", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "未知|布局|文件"):
            paper_package_module.verify_paper_package(second)

    def test_hybrid_and_full_apply_different_optional_asset_policy(self) -> None:
        task, run = self.fixture._confirmed_run()
        hybrid_selection = self.fixture._selection(
            task,
            run,
            asset_mode="hybrid",
        )
        hybrid_selection["assets"][0]["required_for_writing"] = False
        hybrid = self.fixture._seal(hybrid_selection)
        hybrid_out = self.root / str(hybrid["package_id"])
        paper_package_module.export_paper_package(
            self.project,
            str(hybrid["package_id"]),
            "hybrid",
            hybrid_out,
        )
        hybrid_manifest = json.loads(
            (hybrid_out / "manifest.json").read_text("utf-8")
        )
        self.assertEqual(
            ["referenced", "included"],
            [
                row["status"]
                for row in hybrid_manifest["delivery"]["assets"]
            ],
        )

        full_selection = self.fixture._selection(
            task,
            run,
            asset_mode="full",
        )
        full_selection["assets"][0]["required_for_writing"] = False
        full = self.fixture._seal(full_selection)
        full_out = self.root / "full" / str(full["package_id"])
        full_out.parent.mkdir()
        paper_package_module.export_paper_package(
            self.project,
            str(full["package_id"]),
            "full",
            full_out,
        )
        full_manifest = json.loads(
            (full_out / "manifest.json").read_text("utf-8")
        )
        self.assertEqual(
            ["included", "included"],
            [row["status"] for row in full_manifest["delivery"]["assets"]],
        )

    def test_license_privacy_and_incomplete_assets_are_not_copied(self) -> None:
        task, run = self.fixture._confirmed_run()
        selection = self.fixture._selection(task, run)
        selection["assets"][0].update(
            {
                "required_for_writing": False,
                "copy_allowed": False,
                "license": "unknown",
            }
        )
        selection["assets"][1].update(
            {
                "copy_allowed": False,
                "privacy_classification": "sensitive",
            }
        )
        sealed = self.fixture._seal(selection)
        self.assertEqual("incomplete", sealed["readiness"])
        output = self.root / str(sealed["package_id"])

        paper_package_module.export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            output,
        )

        manifest = json.loads((output / "manifest.json").read_text("utf-8"))
        self.assertEqual("incomplete", manifest["readiness"])
        self.assertEqual(
            ["referenced", "omitted"],
            [row["status"] for row in manifest["delivery"]["assets"]],
        )
        self.assertFalse(any((output / "assets" / "included").rglob("*.*")))
        self.assertEqual(
            "pass",
            paper_package_module.verify_paper_package(output)["status"],
        )

    def test_missing_run_artifact_remains_standalone_verifiable(self) -> None:
        task, run = self.fixture._confirmed_run()
        selection = self.fixture._selection(task, run)
        selection["assets"].pop()
        sealed = self.fixture._seal_direct(selection)
        output = self.root / str(sealed["package_id"])

        paper_package_module.export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            output,
        )

        manifest = json.loads((output / "manifest.json").read_text("utf-8"))
        missing_path = str(run["artifacts"][0])
        self.assertIn(
            f"result_file_missing:{run['id']}:{missing_path}",
            manifest["missing_requirements"],
        )
        experiments = json.loads(
            (output / "experiments.json").read_text("utf-8")
        )
        exported_run = experiments["items"][0]["runs"][0]
        self.assertEqual([missing_path], exported_run["artifact_paths"])
        self.assertNotIn(
            missing_path,
            [row["path"] for row in exported_run["files"]],
        )
        self.assertEqual(
            "pass",
            paper_package_module.verify_paper_package(output)["status"],
        )

    def test_standalone_verify_rejects_identity_for_unread_assets(self) -> None:
        task, run = self.fixture._confirmed_run()
        selection = self.fixture._selection(task, run)
        selection["assets"][0].update(
            {
                "availability": "external",
                "omission_reason": "保留在外部实验归档中",
            }
        )
        selection["assets"][1].update(
            {
                "availability": "restricted",
                "omission_reason": "受访问控制限制",
            }
        )
        sealed = self.fixture._seal_direct(selection)
        output = self.root / str(sealed["package_id"])
        paper_package_module.export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            output,
        )
        experiments = json.loads(
            (output / "experiments.json").read_text("utf-8")
        )
        run_files = experiments["items"][0]["runs"][0]["files"]
        self.assertEqual(
            ["AST-0001", "AST-0002"],
            [row["asset_id"] for row in run_files],
        )
        self.assertTrue(
            all(
                row["size_bytes"] is None and row["sha256"] is None
                for row in run_files
            )
        )
        assets_path = output / "assets.jsonl"
        rows = [
            json.loads(line)
            for line in assets_path.read_text("utf-8").splitlines()
        ]
        for row in rows:
            row["size_bytes"] = 4
            row["sha256"] = "sha256:" + ("0" * 64)
        assets_path.write_bytes(
            paper_package_module._canonical_jsonl(rows, "asset_id")
        )
        self._rewrite_handoff_integrity(output)

        with self.assertRaisesRegex(
            ValueError,
            "未读取 Asset 的大小和哈希必须为空",
        ):
            paper_package_module.verify_paper_package(output)

    def test_same_hash_uses_stricter_declaration_and_deduplicates_copy(self) -> None:
        task, run = self.fixture._confirmed_run()
        raw_log = self.project / str(run["result"]["raw_log"])
        metrics = self.project / str(run["artifacts"][0])
        raw_log.write_bytes(metrics.read_bytes())

        blocked_selection = self.fixture._selection(task, run)
        blocked_selection["assets"][0].update(
            {
                "required_for_writing": False,
                "copy_allowed": False,
            }
        )
        blocked = self.fixture._seal(blocked_selection)
        with self.assertRaisesRegex(ValueError, "必需资产|included"):
            paper_package_module.export_paper_package(
                self.project,
                str(blocked["package_id"]),
                "hybrid",
                self.root / str(blocked["package_id"]),
            )

        allowed_selection = self.fixture._selection(
            task,
            run,
            asset_mode="full",
        )
        allowed = self.fixture._seal(allowed_selection)
        output = self.root / "deduplicated" / str(allowed["package_id"])
        output.parent.mkdir()
        paper_package_module.export_paper_package(
            self.project,
            str(allowed["package_id"]),
            "full",
            output,
        )
        manifest = json.loads((output / "manifest.json").read_text("utf-8"))
        asset_delivery = manifest["delivery"]["assets"]
        self.assertEqual(["included", "included"], [
            row["status"] for row in asset_delivery
        ])
        self.assertEqual(
            asset_delivery[0]["package_path"],
            asset_delivery[1]["package_path"],
        )
        included_files = [
            path
            for path in (output / "assets" / "included").rglob("*")
            if path.is_file()
        ]
        self.assertEqual(1, len(included_files))

    def test_coordinated_manifest_tamper_cannot_reference_required_asset(self) -> None:
        sealed = self._seal()
        output = self.root / str(sealed["package_id"])
        paper_package_module.export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            output,
        )
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        asset = manifest["delivery"]["assets"][0]
        included_path = asset["package_path"]
        asset.update(
            {
                "status": "referenced",
                "package_path": None,
                "reason": "coordinated_tamper",
            }
        )
        manifest["delivery"]["files"] = [
            row
            for row in manifest["delivery"]["files"]
            if row["path"] != included_path
        ]
        manifest_path.write_bytes(
            paper_package_module._canonical_json_line(manifest)
        )
        shutil.rmtree((output / included_path).parent)
        self._rewrite_handoff_integrity(output)

        with self.assertRaisesRegex(ValueError, "必需|策略|delivery"):
            paper_package_module.verify_paper_package(output)

    def test_coordinated_scientific_tamper_cannot_downgrade_result_claim(
        self,
    ) -> None:
        sealed = self._seal()
        output = self.root / str(sealed["package_id"])
        paper_package_module.export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            output,
        )
        claims_path = output / "claims.jsonl"
        rows = [
            json.loads(line)
            for line in claims_path.read_text("utf-8").splitlines()
        ]
        rows[0]["maturity"] = "supported"
        claims_path.write_bytes(
            paper_package_module._canonical_jsonl(rows, "claim_id")
        )
        self._rewrite_handoff_integrity(output)

        with self.assertRaisesRegex(ValueError, "result|confirmed|Claim"):
            paper_package_module.verify_paper_package(output)

    def test_coordinated_english_result_tamper_cannot_invent_number(
        self,
    ) -> None:
        sealed = self._seal()
        output = self.root / str(sealed["package_id"])
        paper_package_module.export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            output,
        )
        claims_path = output / "claims.jsonl"
        rows = [
            json.loads(line)
            for line in claims_path.read_text("utf-8").splitlines()
        ]
        rows[0]["statement_en"] = "Accuracy reaches 99%."
        claims_path.write_bytes(
            paper_package_module._canonical_jsonl(rows, "claim_id")
        )
        self._rewrite_handoff_integrity(output)

        with self.assertRaisesRegex(ValueError, "数字|指标|Claim"):
            paper_package_module.verify_paper_package(output)

    def test_standalone_rejects_resealed_result_without_raw_log_asset(
        self,
    ) -> None:
        sealed = self._seal()
        output = self.root / str(sealed["package_id"])
        paper_package_module.export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            output,
        )

        assets_path = output / "assets.jsonl"
        assets_path.write_bytes(
            paper_package_module._canonical_jsonl([], "asset_id")
        )
        experiments_path = output / "experiments.json"
        experiments = json.loads(experiments_path.read_text("utf-8"))
        for experiment in experiments["items"]:
            for run in experiment["runs"]:
                run["files"] = []
        experiments_path.write_bytes(
            paper_package_module._canonical_json_line(experiments)
        )
        visuals_path = output / "visuals.json"
        visuals = json.loads(visuals_path.read_text("utf-8"))
        for visual in visuals["items"]:
            visual["asset_refs"] = []
        visuals_path.write_bytes(
            paper_package_module._canonical_json_line(visuals)
        )

        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["delivery"]["assets"] = []
        manifest["delivery"]["files"] = [
            row
            for row in manifest["delivery"]["files"]
            if not row["path"].startswith("assets/included/")
        ]
        manifest["readiness"] = "paper_ready"
        manifest["missing_requirements"] = []
        manifest_path.write_bytes(
            paper_package_module._canonical_json_line(manifest)
        )
        included = output / "assets" / "included"
        shutil.rmtree(included)
        included.mkdir()
        self._rewrite_handoff_integrity(output)

        with self.assertRaisesRegex(
            ValueError,
            "raw_log|Asset|readiness|missing",
        ):
            paper_package_module.verify_paper_package(output)

    def test_standalone_rejects_resealed_forged_comparison_number(
        self,
    ) -> None:
        sealed = self._seal()
        output = self.root / str(sealed["package_id"])
        paper_package_module.export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            output,
        )
        claims_path = output / "claims.jsonl"
        rows = [
            json.loads(line)
            for line in claims_path.read_text("utf-8").splitlines()
        ]
        rows[0]["comparisons"] = [{"forged_delta": 0.99}]
        rows[0]["statement_en"] = (
            "Accuracy is 0.75 with a 99% forged gain."
        )
        claims_path.write_bytes(
            paper_package_module._canonical_jsonl(rows, "claim_id")
        )
        self._rewrite_handoff_integrity(output)

        with self.assertRaisesRegex(
            ValueError,
            "comparison|Comparison|number|Claim",
        ):
            paper_package_module.verify_paper_package(output)

    def test_standalone_recomputes_done_task_comparison_from_run(
        self,
    ) -> None:
        task, run = self.fixture._confirmed_run()
        current = package_tests.load_task(
            self.project,
            str(task["id"]),
        )
        package_tests.transition_task(
            self.project,
            current["id"],
            "executing",
        )
        package_tests.transition_task(
            self.project,
            current["id"],
            "reviewing",
        )
        valid_comparison = {
            "primary_metric": "score",
            "baseline": None,
            "candidate": 0.75,
            "delta": None,
        }
        package_tests.transition_task(
            self.project,
            current["id"],
            "done",
            conclusion={
                "run_id": run["id"],
                "comparison": valid_comparison,
            },
        )
        sealed = self.fixture._seal_direct(
            self.fixture._selection(task, run)
        )
        output = self.root / str(sealed["package_id"])
        paper_package_module.export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            output,
        )

        claims_path = output / "claims.jsonl"
        claims = [
            json.loads(line)
            for line in claims_path.read_text("utf-8").splitlines()
        ]
        forged_comparison = {
            **valid_comparison,
            "candidate": 0.99,
        }
        claims[0]["comparisons"] = [
            {
                "task_id": task["id"],
                "run_id": run["id"],
                **forged_comparison,
            }
        ]
        claims[0]["statement_en"] = (
            "Accuracy is 0.75 with a forged 99% candidate."
        )
        claims_path.write_bytes(
            paper_package_module._canonical_jsonl(
                claims,
                "claim_id",
            )
        )
        experiments_path = output / "experiments.json"
        experiments = json.loads(experiments_path.read_text("utf-8"))
        experiments["items"][0]["task"]["conclusion"][
            "comparison"
        ] = forged_comparison
        experiments_path.write_bytes(
            paper_package_module._canonical_json_line(experiments)
        )
        self._rewrite_handoff_integrity(output)

        with self.assertRaisesRegex(
            ValueError,
            "comparison|Comparison|数字|Claim",
        ):
            paper_package_module.verify_paper_package(output)

    def test_standalone_rejects_scope_detached_from_brief(self) -> None:
        sealed = self._seal()
        output = self.root / str(sealed["package_id"])
        paper_package_module.export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            output,
        )
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        study_path = output / "study.json"
        study = json.loads(study_path.read_text("utf-8"))
        forged_scope = {
            "research_area": "forged research area",
            "goal": "forged objective",
            "excluded_topics": ["forged exclusion"],
        }
        manifest["paper_scope"].update(forged_scope)
        study["paper_scope"].update(forged_scope)
        manifest_path.write_bytes(
            paper_package_module._canonical_json_line(manifest)
        )
        study_path.write_bytes(
            paper_package_module._canonical_json_line(study)
        )
        self._rewrite_handoff_integrity(output)

        with self.assertRaisesRegex(
            ValueError,
            "Brief|brief|paper_scope",
        ):
            paper_package_module.verify_paper_package(output)

    def test_standalone_rejects_invalid_source_metadata_types(self) -> None:
        paper, _code, idea, _template, module, execution = (
            self.fixture._innovation_chain()
        )
        selection = self.fixture._innovation_selection(
            paper,
            idea,
            module,
            execution,
        )
        sealed = self.fixture._seal_direct(selection)
        output = self.root / str(sealed["package_id"])
        paper_package_module.export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            output,
        )
        sources_path = output / "sources.jsonl"
        original_rows = [
            json.loads(line)
            for line in sources_path.read_text("utf-8").splitlines()
        ]
        mutations = (
            ("kind", "dataset"),
            ("identity", {"forged": True}),
            ("locator", ["forged"]),
            ("revision", {"forged": True}),
            ("license", ["project-owned"]),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                rows = [dict(row) for row in original_rows]
                rows[0][field] = value
                sources_path.write_bytes(
                    paper_package_module._canonical_jsonl(
                        rows,
                        "source_ref",
                    )
                )
                self._rewrite_handoff_integrity(output)
                with self.assertRaisesRegex(
                    ValueError,
                    "Source|source|kind|identity|locator|revision|license",
                ):
                    paper_package_module.verify_paper_package(output)

    def test_standalone_rejects_noncanonical_manifest_scalars(self) -> None:
        sealed = self._seal()
        package_id = str(sealed["package_id"])
        output = self.root / package_id
        paper_package_module.export_paper_package(
            self.project,
            package_id,
            "hybrid",
            output,
        )
        manifest_path = output / "manifest.json"
        study_path = output / "study.json"
        original_manifest = json.loads(manifest_path.read_text("utf-8"))
        original_study = json.loads(study_path.read_text("utf-8"))
        mutations = (
            ("layout_revision_bool", True),
            ("project_id_not_uuid", "not-a-uuid"),
            (
                "project_id_not_canonical",
                "{" + original_manifest["project_id"] + "}",
            ),
            ("supersedes_self", package_id),
            ("supersedes_future", "PKG-0002"),
        )
        for mutation, value in mutations:
            with self.subTest(mutation=mutation):
                manifest = json.loads(json.dumps(original_manifest))
                study = json.loads(json.dumps(original_study))
                if mutation == "layout_revision_bool":
                    manifest["delivery"]["layout_revision"] = value
                elif mutation in {
                    "project_id_not_uuid",
                    "project_id_not_canonical",
                }:
                    manifest["project_id"] = value
                    study["brief"]["project_id"] = value
                else:
                    manifest["supersedes_package_id"] = value
                manifest_path.write_bytes(
                    paper_package_module._canonical_json_line(manifest)
                )
                study_path.write_bytes(
                    paper_package_module._canonical_json_line(study)
                )
                self._rewrite_handoff_integrity(output)
                with self.assertRaisesRegex(
                    ValueError,
                    "layout_revision|project_id|UUID|supersedes|package",
                ):
                    paper_package_module.verify_paper_package(output)

    def test_paths_reject_case_nfkc_controls_and_byte_limits(self) -> None:
        sealed = self._seal()
        output = self.root / str(sealed["package_id"])
        paper_package_module.export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            output,
        )
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["delivery"]["files"].append(
            {
                **manifest["delivery"]["files"][0],
                "path": "STUDY.JSON",
            }
        )
        manifest_path.write_bytes(
            paper_package_module._canonical_json_line(manifest)
        )
        self._rewrite_handoff_integrity(output)
        with self.assertRaisesRegex(ValueError, "冲突"):
            paper_package_module.verify_paper_package(output)
        manifest["delivery"]["files"].pop()
        manifest_path.write_bytes(
            paper_package_module._canonical_json_line(manifest)
        )
        self._rewrite_handoff_integrity(output)

        (output / "ｍanifest.json").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "NFKC"):
            paper_package_module.verify_paper_package(output)
        (output / "ｍanifest.json").unlink()

        unsafe_paths = [
            "assets/included/" + ("界" * 86),
            ("safe/" * 205) + "name.bin",
            "assets/included/visible\u0085name.bin",
            "assets/included/visible\u202ename.bin",
            "../study.json",
            "assets\\included\\file.bin",
            "//server/share/file.bin",
            "C:/absolute/file.bin",
            "assets/included/CON.txt",
            "assets/included/trailing.",
            "assets//included/file.bin",
        ]
        for value in unsafe_paths:
            with self.subTest(path=value[-30:]):
                with self.assertRaises(ValueError):
                    delivery_module._normalize_handoff_relative_path(
                        value,
                        "test.path",
                    )

    def test_preflight_budget_rejects_before_asset_hash(self) -> None:
        sealed = self._seal()
        output = self.root / str(sealed["package_id"])
        paper_package_module.export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            output,
        )
        with (
            mock.patch.object(
                paper_package_module,
                "MAX_HANDOFF_ASSET_TOTAL_SIZE",
                1,
            ),
            mock.patch.object(
                paper_package_module,
                "_hash_live_regular_file",
                side_effect=AssertionError("hash must not run"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "512 MiB|资产合计"):
                paper_package_module.verify_paper_package(output)

    def test_verifier_rejects_hardlink_before_hash(self) -> None:
        sealed = self._seal()
        output = self.root / str(sealed["package_id"])
        paper_package_module.export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            output,
        )
        manifest = json.loads((output / "manifest.json").read_text("utf-8"))
        relative = manifest["delivery"]["assets"][0]["package_path"]
        asset = output / relative
        outside = self.root / "same-content.bin"
        outside.write_bytes(asset.read_bytes())
        asset.unlink()
        try:
            os.link(outside, asset)
        except OSError as error:
            self.skipTest(f"当前文件系统不能建立 hardlink：{error}")
        with self.assertRaisesRegex(ValueError, "hardlink"):
            paper_package_module.verify_paper_package(output)

    def test_source_toctou_and_keyboard_interrupt_leave_no_partial_output(
        self,
    ) -> None:
        sealed = self._seal()
        package_id = str(sealed["package_id"])
        output = self.root / package_id
        original_plan = delivery_module._plan_assets
        manifest = json.loads(
            (
                self.control
                / "paper-packages"
                / "packages"
                / package_id
                / "assets.jsonl"
            ).read_text("utf-8").splitlines()[0]
        )
        mutated_source = self.project / manifest["path"]
        original_source = mutated_source.read_bytes()

        def mutate_after_plan(*args: object, **kwargs: object):
            delivery, copies = original_plan(*args, **kwargs)
            copies[0]["source"].write_bytes(b"changed-after-live-validation")
            return delivery, copies

        with mock.patch.object(
            delivery_module,
            "_plan_assets",
            side_effect=mutate_after_plan,
        ):
            with self.assertRaisesRegex(ValueError, "大小|哈希|变化"):
                paper_package_module.export_paper_package(
                    self.project,
                    package_id,
                    "hybrid",
                    output,
                )
        self.assertFalse(os.path.lexists(output))
        self.assertEqual(
            [],
            self._staging_paths(self.root),
        )
        mutated_source.write_bytes(original_source)

        second_root = self.root / "interrupt"
        second_root.mkdir()
        with mock.patch.object(
            delivery_module,
            "_copy_regular_file_same_handle",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                paper_package_module.export_paper_package(
                    self.project,
                    package_id,
                    "hybrid",
                    second_root / package_id,
                )
        self.assertFalse(os.path.lexists(second_root / package_id))
        self.assertEqual(
            [],
            self._staging_paths(second_root),
        )

    def test_control_read_is_bound_to_initial_inventory_identity(self) -> None:
        sealed = self._seal()
        package_id = str(sealed["package_id"])
        output = self.root / package_id
        paper_package_module.export_paper_package(
            self.project,
            package_id,
            "hybrid",
            output,
        )
        real_read = delivery_module.read_bounded_regular_file
        backup = self.root / "study-original.json"
        swapped = False

        def replace_read_restore(
            path: Path,
            limit: int,
            label: str,
            **kwargs: object,
        ) -> bytes:
            nonlocal swapped
            if label != "study.json" or swapped:
                return real_read(path, limit, label, **kwargs)
            swapped = True
            content = path.read_bytes()
            os.replace(path, backup)
            path.write_bytes(content)
            try:
                return real_read(path, limit, label, **kwargs)
            finally:
                path.unlink()
                os.replace(backup, path)

        with mock.patch.object(
            delivery_module,
            "read_bounded_regular_file",
            side_effect=replace_read_restore,
        ):
            with self.assertRaisesRegex(ValueError, "身份|枚举|变化"):
                paper_package_module.verify_paper_package(output)

    def test_asset_hash_is_bound_to_initial_inventory_identity(self) -> None:
        sealed = self._seal()
        package_id = str(sealed["package_id"])
        output = self.root / package_id
        paper_package_module.export_paper_package(
            self.project,
            package_id,
            "hybrid",
            output,
        )
        manifest = json.loads((output / "manifest.json").read_text("utf-8"))
        relative = manifest["delivery"]["assets"][0]["package_path"]
        target = output / relative
        backup = self.root / "asset-original.bin"
        real_hash = paper_package_module._hash_live_regular_file
        swapped = False

        def replace_hash_restore(
            path: Path,
            label: str,
            limit: int,
            **kwargs: object,
        ) -> dict[str, object]:
            nonlocal swapped
            if Path(path) != target or swapped:
                return real_hash(path, label, limit, **kwargs)
            swapped = True
            content = path.read_bytes()
            os.replace(path, backup)
            path.write_bytes(content)
            try:
                return real_hash(path, label, limit, **kwargs)
            finally:
                path.unlink()
                os.replace(backup, path)

        with mock.patch.object(
            paper_package_module,
            "_hash_live_regular_file",
            side_effect=replace_hash_restore,
        ):
            with self.assertRaisesRegex(ValueError, "身份|枚举|变化"):
                paper_package_module.verify_paper_package(output)

    def test_created_staging_is_cleaned_if_mkdir_raises_interrupt(self) -> None:
        sealed = self._seal()
        package_id = str(sealed["package_id"])
        output = self.root / package_id
        real_mkdir = Path.mkdir
        created_staging: Path | None = None

        def mkdir_then_interrupt(
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal created_staging
            real_mkdir(path, *args, **kwargs)
            candidate = Path(path)
            if (
                created_staging is None
                and candidate.parent == output.parent
                and candidate != output
            ):
                created_staging = candidate
                raise KeyboardInterrupt

        with mock.patch.object(Path, "mkdir", new=mkdir_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                paper_package_module.export_paper_package(
                    self.project,
                    package_id,
                    "hybrid",
                    output,
                )
        self.assertIsNotNone(created_staging)
        self.assertFalse(os.path.lexists(created_staging))
        self.assertFalse(os.path.lexists(output))

    @unittest.skipUnless(os.name == "nt", "Windows 路径边界测试")
    def test_windows_boundary_export_uses_no_longer_staging_or_temp(self) -> None:
        task, run = self.fixture._confirmed_run()
        selection = self.fixture._selection(task, run)
        for row, availability, reason in (
            (selection["assets"][0], "external", "保留在外部归档"),
            (selection["assets"][1], "restricted", "访问受限"),
        ):
            row["availability"] = availability
            row["omission_reason"] = reason
        sealed = self.fixture._seal_direct(selection)
        package_id = str(sealed["package_id"])
        final_tail = str(Path(package_id) / "checksums.sha256")
        component_length = 248 - len(str(self.root)) - len(final_tail) - 2
        self.assertGreater(component_length, 16)
        self.assertLessEqual(component_length, 240)
        boundary_parent = self.root / ("p" * component_length)
        boundary_parent.mkdir()
        output = boundary_parent / package_id
        self.assertEqual(248, len(str(output / "checksums.sha256")))
        real_publish = paper_package_module._publish_directory_no_replace

        def publish_only_if_staging_is_not_longer(
            source: Path,
            destination: Path,
        ) -> None:
            self.assertLessEqual(len(str(source)), len(str(destination)))
            real_publish(source, destination)

        with (
            mock.patch.object(
                paper_package_module,
                "atomic_create_bytes_clean",
                side_effect=AssertionError(
                    "私有 staging 不得再创建更长的 .cvexp 临时文件"
                ),
            ),
            mock.patch.object(
                paper_package_module,
                "_publish_directory_no_replace",
                side_effect=publish_only_if_staging_is_not_longer,
            ),
        ):
            result = paper_package_module.export_paper_package(
                self.project,
                package_id,
                "hybrid",
                output,
            )
        self.assertEqual("exported", result["status"])
        self.assertEqual(
            "pass",
            paper_package_module.verify_paper_package(output)["status"],
        )

    def test_publish_interrupt_after_rename_returns_committed_result(self) -> None:
        sealed = self._seal()
        package_id = str(sealed["package_id"])
        output = self.root / package_id
        real_publish = paper_package_module._publish_directory_no_replace

        def publish_then_interrupt(source: Path, destination: Path) -> None:
            real_publish(source, destination)
            raise KeyboardInterrupt

        with mock.patch.object(
            paper_package_module,
            "_publish_directory_no_replace",
            side_effect=publish_then_interrupt,
        ):
            result = paper_package_module.export_paper_package(
                self.project,
                package_id,
                "hybrid",
                output,
            )
        self.assertEqual("exported", result["status"])
        self.assertEqual(
            "pass",
            paper_package_module.verify_paper_package(output)["status"],
        )

    def test_unknown_staging_identity_is_preserved_and_reported(self) -> None:
        sealed = self._seal()
        package_id = str(sealed["package_id"])
        output = self.root / package_id

        def replace_staging(
            _source: Path,
            destination: Path,
            **_kwargs: object,
        ) -> None:
            staging = next(
                parent
                for parent in destination.parents
                if delivery_module._is_handoff_staging_name(parent.name)
            )
            staging.rename(staging.with_name(staging.name + ".owned"))
            staging.mkdir()
            raise KeyboardInterrupt

        with mock.patch.object(
            delivery_module,
            "_copy_regular_file_same_handle",
            side_effect=replace_staging,
        ):
            with self.assertRaisesRegex(RuntimeError, "身份变化|保留"):
                paper_package_module.export_paper_package(
                    self.project,
                    package_id,
                    "hybrid",
                    output,
                )
        self.assertTrue(
            self._staging_paths(self.root)
        )

    def test_concurrent_export_same_target_has_one_winner(self) -> None:
        sealed = self._seal()
        package_id = str(sealed["package_id"])
        output = self.root / package_id

        def export() -> str:
            try:
                return paper_package_module.export_paper_package(
                    self.project,
                    package_id,
                    "hybrid",
                    output,
                )["status"]
            except FileExistsError:
                return "exists"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _value: export(), range(2)))
        self.assertEqual(["exists", "exported"], sorted(results))
        self.assertEqual(
            "pass",
            paper_package_module.verify_paper_package(output)["status"],
        )

    def test_final_inventory_detects_concurrent_add_and_replacement(self) -> None:
        sealed = self._seal()
        output = self.root / str(sealed["package_id"])
        paper_package_module.export_paper_package(
            self.project,
            str(sealed["package_id"]),
            "hybrid",
            output,
        )
        original_hash = paper_package_module._hash_live_regular_file
        added = False

        def add_after_preflight(*args: object, **kwargs: object):
            nonlocal added
            result = original_hash(*args, **kwargs)
            if not added:
                added = True
                (output / "late-file.txt").write_text("late", encoding="utf-8")
            return result

        with mock.patch.object(
            paper_package_module,
            "_hash_live_regular_file",
            side_effect=add_after_preflight,
        ):
            with self.assertRaisesRegex(ValueError, "变化|未知|布局"):
                paper_package_module.verify_paper_package(output)
        (output / "late-file.txt").unlink()

        original_read = delivery_module.read_bounded_regular_file
        replaced = False

        def replace_after_read(
            path: Path,
            limit: int,
            label: str,
            **kwargs: object,
        ) -> bytes:
            nonlocal replaced
            content = original_read(path, limit, label, **kwargs)
            if label == "manifest.json" and not replaced:
                replaced = True
                changed = bytearray(content)
                changed[0] = ord("[")
                path.write_bytes(bytes(changed))
            return content

        with mock.patch.object(
            delivery_module,
            "read_bounded_regular_file",
            side_effect=replace_after_read,
        ):
            with self.assertRaisesRegex(ValueError, "变化|身份|布局"):
                paper_package_module.verify_paper_package(output)


if __name__ == "__main__":
    unittest.main()
