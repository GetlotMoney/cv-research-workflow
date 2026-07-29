from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from tests._helpers import ROOT, SCRIPTS, cli_json, read_json, run_cli


SKILL = ROOT / "skills" / "cv-experiment-workflow"
TEMPLATES = SKILL / "assets" / "templates"


class TrialTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        cli_json("init", "--path", self.project, "--name", "演示")
        self.idea = cli_json(
            "new-idea", "--project", self.project,
            "--title", "适配器", "--note", "结构探索",
        )
        cli_json(
            "activate-idea", "--project", self.project,
            "--idea", self.idea["id"], "--problem", "域偏移",
            "--mechanism", "可关闭结构", "--hypothesis", "等待实验验证",
        )
        self.version = cli_json(
            "register-version", "--project", self.project,
            "--name", "基线", "--repo-url", "https://example.invalid/repo",
            "--commit", "a" * 40,
        )

    def new_trial(self, *extra: object, check: bool = True):
        return run_cli(
            "new-trial", "--project", self.project,
            "--idea", self.idea["id"], "--base-version", self.version["id"],
            "--template-family", "feature-adapter",
            "--attachment-point", "feature-output", *extra, check=check,
        )

    def create_ready_project(self, label: str):
        project = Path(self.temporary.name) / label
        cli_json("init", "--path", project, "--name", label)
        idea = cli_json(
            "new-idea", "--project", project, "--title", "测试", "--note", "测试",
        )
        cli_json(
            "activate-idea", "--project", project, "--idea", idea["id"],
            "--problem", "问题", "--mechanism", "机制", "--hypothesis", "假设",
        )
        version = cli_json(
            "register-version", "--project", project, "--name", "基线",
            "--repo-url", "https://example.invalid/repo", "--commit", "c" * 40,
        )
        return project, idea, version

    @staticmethod
    def snapshot_tree(root: Path) -> dict[str, bytes | None]:
        snapshot: dict[str, bytes | None] = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            snapshot[relative] = None if path.is_dir() else path.read_bytes()
        return snapshot

    def write_template_candidate(
        self,
        module: object,
        skill_root: Path,
        directory_name: str,
        *,
        required_capabilities: list[str] | None = None,
        interfaces: list[str] | None = None,
        dependencies: list[str] | None = None,
        provenance_status: str = "verified",
        marker: str,
    ) -> Path:
        directory = skill_root / "assets" / "templates" / directory_name
        directory.mkdir(parents=True)
        (directory / "module.py").write_text(
            f'SELECTION_MARKER = "{marker}"\n', encoding="utf-8",
        )
        (directory / "test_contract.py").write_text(
            "def test_contract():\n    return True\n", encoding="utf-8",
        )
        manifest = {
            "schema": "cv-experiment-workflow.template.v1",
            "family": "feature-adapter",
            "attachment_points": ["feature-output"],
            "required_capabilities": required_capabilities or [],
            "interfaces": interfaces or [],
            "dependencies": dependencies or [],
            "provenance_status": provenance_status,
            "template_kind": "structural_scaffold",
            "scientific_claim_scope": "none",
            "papers": [],
            "code_sources": [],
            "canonical_sha256": "0" * 64,
        }
        manifest["canonical_sha256"] = module._template_digest(directory, manifest)
        (directory / "template.json").write_text(
            json.dumps(manifest), encoding="utf-8",
        )
        return directory

    def test_exact_selection_records_canonical_digest(self) -> None:
        result = self.new_trial()
        trial = json.loads(result.stdout)
        asset = read_json(
            self.project / ".experiment-workflow" / "code-assets"
            / trial["code_asset_id"] / "asset.json"
        )
        manifest = read_json(TEMPLATES / "feature-adapter" / "template.json")
        self.assertEqual("feature-adapter", trial["template_family"])
        self.assertEqual("feature-output", trial["attachment_point"])
        self.assertIn("family", trial["template_selection_reason"])
        self.assertEqual(trial["template_sha256"], asset["template_sha256"])
        self.assertEqual(manifest["canonical_sha256"], trial["template_sha256"])
        self.assertRegex(trial["template_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn(str(SKILL.resolve()), trial["template_selection_reason"])

    def test_new_trial_requires_exactly_one_template_source(self) -> None:
        common = (
            "new-trial", "--project", self.project, "--idea", self.idea["id"],
            "--base-version", self.version["id"],
            "--attachment-point", "feature-output",
        )
        missing = run_cli(*common, check=False)
        both = run_cli(
            *common, "--template-family", "feature-adapter",
            "--template", "TPL-0001", check=False,
        )
        self.assertEqual(2, missing.returncode)
        self.assertEqual(2, both.returncode)
        control = self.project / ".experiment-workflow"
        self.assertEqual([], list((control / "trials").iterdir()))
        self.assertEqual([], list((control / "code-assets").iterdir()))

    def test_cli_rejects_subcommand_option_abbreviations(self) -> None:
        trial = json.loads(self.new_trial().stdout)
        sync = run_cli(
            "sync-code-asset", "--proj", self.project,
            "--ass", trial["code_asset_id"], check=False,
        )
        abbreviated_trial = run_cli(
            "new-trial", "--proj", self.project, "--ide", self.idea["id"],
            "--base-v", self.version["id"], "--template-f", "feature-adapter",
            "--attachment-p", "feature-output", check=False,
        )
        self.assertEqual(2, sync.returncode)
        self.assertIn("error:", sync.stderr)
        self.assertEqual(2, abbreviated_trial.returncode)
        self.assertIn("error:", abbreviated_trial.stderr)
        control = self.project / ".experiment-workflow"
        self.assertEqual(["TRIAL-0001"], sorted(path.name for path in (control / "trials").iterdir()))
        self.assertEqual(["CODE-0001"], sorted(path.name for path in (control / "code-assets").iterdir()))

    def test_v1_trial_and_asset_reject_integer_identity_and_digest_fields(self) -> None:
        trial = json.loads(self.new_trial().stdout)
        control = self.project / ".experiment-workflow"
        trial_payload = read_json(control / "trials" / trial["id"] / "trial.json")
        asset_payload = read_json(
            control / "code-assets" / trial["code_asset_id"] / "asset.json"
        )
        trial_payload["transaction_id"] = int("5" * 32)
        asset_payload["transaction_id"] = int("5" * 32)
        asset_payload["files"]["module.py"] = int("6" * 64)
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        templates_module = importlib.import_module("workflow_core.templates")

        with self.subTest(kind="trial"):
            with self.assertRaises(ValueError):
                templates_module._validate_trial_shape(trial_payload, trial["id"])
        with self.subTest(kind="asset"):
            with self.assertRaises(ValueError):
                templates_module._validate_asset_shape(
                    asset_payload, trial["code_asset_id"],
                )

    def test_transaction_marker_rejects_integer_transaction_id(self) -> None:
        control = self.project / ".experiment-workflow"
        marker = {
            "schema": "cv-experiment-workflow.new-trial-transaction.v2",
            "transaction_id": int("7" * 32),
            "trial_id": "TRIAL-0001",
            "asset_id": "CODE-0001",
            "request_digest": "8" * 64,
        }
        (control / ".runtime" / "new-trial-transaction.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        templates_module = importlib.import_module("workflow_core.templates")

        with self.assertRaisesRegex(ValueError, "marker"):
            templates_module._recover_transaction(control)

    def test_future_version_lineage_helper_preserves_inherited_order(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        formal = importlib.import_module("workflow_core.formal_composition")
        first = {"code_sources": [{"commit": "a" * 40}]}
        later = {
            "template_id": "TPL-0001",
            "accepted_code_asset_ids": ["CODE-0002", "CODE-0001"],
            "code_sources": [{"commit": "b" * 40}],
        }

        self.assertEqual(
            [], formal.version_inherited_assets(first, "TPL-0001", "a" * 40),
        )
        self.assertEqual(
            ["CODE-0002", "CODE-0001"],
            formal.version_inherited_assets(later, "TPL-0001", "a" * 40),
        )
        with self.assertRaises(ValueError):
            formal.version_inherited_assets(
                {**later, "template_id": "TPL-0002"}, "TPL-0001", "a" * 40,
            )
        with self.assertRaises(ValueError):
            formal.version_inherited_assets(
                {**later, "accepted_code_asset_ids": ["CODE-0001", "CODE-0001"]},
                "TPL-0001", "a" * 40,
            )

    def test_adapter_capability_mismatch_rejects_template(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        skill_root = Path(self.temporary.name) / "capability-skill"
        self.write_template_candidate(
            module, skill_root, "candidate-required",
            required_capabilities=["supports_feature_transform"],
            interfaces=["feature-output"], marker="requires-capability",
        )
        with mock.patch.object(module, "_skill_root", return_value=skill_root):
            with self.assertRaisesRegex(ValueError, "capabilit"):
                module.new_trial(
                    self.project, self.idea["id"], self.version["id"],
                    "feature-adapter", "feature-output",
                )

    def test_two_candidates_follow_declared_stable_ranking(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        skill_root = Path(self.temporary.name) / "ranking-skill"
        self.write_template_candidate(
            module, skill_root, "candidate-exact",
            interfaces=["feature-output"], dependencies=["a", "b"],
            marker="exact-interface",
        )
        self.write_template_candidate(
            module, skill_root, "candidate-fewer-deps",
            interfaces=["other-interface"], dependencies=[], marker="fewer-deps",
        )
        with mock.patch.object(module, "_skill_root", return_value=skill_root):
            trial = module.new_trial(
                self.project, self.idea["id"], self.version["id"],
                "feature-adapter", "feature-output",
            )
        selected = (
            self.project / ".experiment-workflow" / "code-assets"
            / trial["code_asset_id"] / "module.py"
        ).read_text(encoding="utf-8")
        self.assertIn("exact-interface", selected)
        self.assertIn("exact_interface=true", trial["template_selection_reason"])

    def test_template_selection_ignores_iterdir_order(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        skill_root = Path(self.temporary.name) / "order-skill"
        first = self.write_template_candidate(
            module, skill_root, "candidate-a",
            interfaces=["feature-output"], marker="canonical-first",
        )
        second = self.write_template_candidate(
            module, skill_root, "candidate-b",
            interfaces=["feature-output"], marker="canonical-second",
        )
        templates = skill_root / "assets" / "templates"
        original_iterdir = Path.iterdir

        def reversed_templates(path: Path):
            if path == templates:
                return iter((second, first))
            return original_iterdir(path)

        with (
            mock.patch.object(module, "_skill_root", return_value=skill_root),
            mock.patch.object(Path, "iterdir", new=reversed_templates),
        ):
            trial = module.new_trial(
                self.project, self.idea["id"], self.version["id"],
                "feature-adapter", "feature-output",
            )
        selected = (
            self.project / ".experiment-workflow" / "code-assets"
            / trial["code_asset_id"] / "module.py"
        ).read_text(encoding="utf-8")
        self.assertIn("canonical-first", selected)

    def test_validate_uses_historical_template_digest_not_current_winner(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        skill_root = Path(self.temporary.name) / "digest-history-skill"
        self.write_template_candidate(
            module, skill_root, "candidate-original",
            interfaces=["other-interface"], dependencies=["a"], marker="original",
        )
        with mock.patch.object(module, "_skill_root", return_value=skill_root):
            trial = module.new_trial(
                self.project, self.idea["id"], self.version["id"],
                "feature-adapter", "feature-output",
            )
            self.write_template_candidate(
                module, skill_root, "candidate-new-winner",
                interfaces=["feature-output"], dependencies=[], marker="new-winner",
            )
            result = module.validate_project_trials(self.project)
        self.assertTrue(result["valid"])
        self.assertEqual(
            trial["template_sha256"],
            read_json(skill_root / "assets/templates/candidate-original/template.json")["canonical_sha256"],
        )

    def test_validate_rejects_historical_trial_after_adapter_capability_changes(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        skill_root = Path(self.temporary.name) / "historical-skill"
        self.write_template_candidate(
            module, skill_root, "candidate-history",
            required_capabilities=["supports_feature_transform"],
            interfaces=["feature-output"], marker="historical",
        )
        adapter_path = self.project / ".experiment-workflow" / "adapter.json"
        adapter = read_json(adapter_path)
        adapter["capabilities"] = {"supports_feature_transform": True}
        adapter_path.write_text(json.dumps(adapter), encoding="utf-8")
        with mock.patch.object(module, "_skill_root", return_value=skill_root):
            module.new_trial(
                self.project, self.idea["id"], self.version["id"],
                "feature-adapter", "feature-output",
            )
            adapter["capabilities"]["supports_feature_transform"] = False
            adapter_path.write_text(json.dumps(adapter), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "capabilit"):
                module.validate_project_trials(self.project)

    def test_new_trial_strictly_validates_adapter_json(self) -> None:
        adapter_path = self.project / ".experiment-workflow" / "adapter.json"
        adapter = read_json(adapter_path)
        adapter["capabilities"] = {"not-a-boolean": "yes"}
        adapter_path.write_text(json.dumps(adapter), encoding="utf-8")
        result = self.new_trial(check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("adapter", result.stderr.lower())

    def test_draft_idea_and_invalid_version_are_rejected_without_residue(self) -> None:
        draft = cli_json(
            "new-idea", "--project", self.project,
            "--title", "草稿", "--note", "未完成",
        )
        bad_idea = run_cli(
            "new-trial", "--project", self.project, "--idea", draft["id"],
            "--base-version", self.version["id"], "--template-family",
            "feature-adapter", "--attachment-point", "feature-output", check=False,
        )
        bad_version = run_cli(
            "new-trial", "--project", self.project, "--idea", self.idea["id"],
            "--base-version", "VER-9999", "--template-family",
            "feature-adapter", "--attachment-point", "feature-output", check=False,
        )
        self.assertEqual(2, bad_idea.returncode)
        self.assertEqual(2, bad_version.returncode)
        self.assertEqual([], list((self.project / ".experiment-workflow/trials").iterdir()))
        self.assertEqual([], list((self.project / ".experiment-workflow/code-assets").iterdir()))

    def test_version_with_corrupt_schema_is_rejected(self) -> None:
        version_path = (
            self.project / ".experiment-workflow/versions" / f"{self.version['id']}.json"
        )
        version = read_json(version_path)
        version["schema"] = "wrong"
        version_path.write_text(json.dumps(version), encoding="utf-8")
        result = self.new_trial(check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("Version", result.stderr)

    def test_no_match_leaves_no_objects_and_does_not_copy_old_trial(self) -> None:
        first = json.loads(self.new_trial().stdout)
        old_module = (
            self.project / ".experiment-workflow/code-assets"
            / first["code_asset_id"] / "module.py"
        )
        old_module.write_text("USER_OLD_TRIAL_SENTINEL = True\n", encoding="utf-8")
        missing = run_cli(
            "new-trial", "--project", self.project, "--idea", self.idea["id"],
            "--base-version", self.version["id"], "--template-family", "unknown",
            "--attachment-point", "nowhere", check=False,
        )
        self.assertEqual(2, missing.returncode)
        second = json.loads(self.new_trial().stdout)
        new_module = (
            self.project / ".experiment-workflow/code-assets"
            / second["code_asset_id"] / "module.py"
        )
        self.assertNotIn("USER_OLD_TRIAL_SENTINEL", new_module.read_text(encoding="utf-8"))
        self.assertEqual(2, len(list((self.project / ".experiment-workflow/trials").iterdir())))

    def test_paper_derived_requires_verified_primary_before_creation(self) -> None:
        blocked = self.new_trial("--claim-scope", "paper-derived", check=False)
        self.assertEqual(2, blocked.returncode)
        self.assertIn("verified primary paper", blocked.stderr)
        control = self.project / ".experiment-workflow"
        self.assertEqual([], list((control / "trials").iterdir()))
        self.assertEqual([], list((control / "code-assets").iterdir()))
        self.assertEqual([], list((control / ".runtime").iterdir()))

        provenance = Path(self.temporary.name) / "provenance.json"
        provenance.write_text(json.dumps({
            "schema": "cv-experiment-workflow.provenance.v1",
            "scientific_claim_scope": "paper-derived",
            "papers": [{
                "role": "primary_mechanism", "verification_status": "verified",
                "title": "A Paper", "authors": ["A. Author"], "year": 2024,
                "venue": "CVPR", "url": "https://example.invalid/paper",
                "locator": "Section 3", "supported_claim": "定义待验证机制",
            }],
            "code_sources": [], "what_is_copied": [], "what_is_adapted": [],
            "what_is_new": [], "unresolved_questions": [],
        }), encoding="utf-8")
        verified = json.loads(self.new_trial(
            "--claim-scope", "paper-derived", "--provenance-file", provenance,
        ).stdout)
        self.assertEqual("active", verified["status"])
        self.assertEqual(0, run_cli("validate", "--project", self.project).returncode)

    def test_unsafe_paths_are_rejected_across_version_adapter_and_provenance(self) -> None:
        unsafe_paths = ("../../outside", "/etc/passwd", r"\Windows\System32", r"C:\Windows\System32")
        for index, unsafe in enumerate(unsafe_paths):
            with self.subTest(kind="register-version", path=unsafe):
                result = run_cli(
                    "register-version", "--project", self.project, "--name", f"bad-{index}",
                    "--repo-url", "https://example.invalid/repo", "--commit", "d" * 40,
                    "--code-path", unsafe, check=False,
                )
                self.assertEqual(2, result.returncode)

        version_path = self.project / ".experiment-workflow/versions" / f"{self.version['id']}.json"
        version = read_json(version_path)
        version["code_sources"][0]["relative_path"] = r"C:\outside"
        version_path.write_text(json.dumps(version), encoding="utf-8")
        self.assertEqual(2, self.new_trial(check=False).returncode)
        version["code_sources"][0]["relative_path"] = "."
        version_path.write_text(json.dumps(version), encoding="utf-8")

        adapter_path = self.project / ".experiment-workflow/adapter.json"
        adapter = read_json(adapter_path)
        adapter["code_sources"] = [{
            "repo_url": "https://example.invalid/repo", "commit": "e" * 40,
            "relative_path": r"\Windows\System32",
        }]
        adapter_path.write_text(json.dumps(adapter), encoding="utf-8")
        self.assertEqual(2, self.new_trial(check=False).returncode)
        adapter["code_sources"] = []
        adapter_path.write_text(json.dumps(adapter), encoding="utf-8")

        provenance = Path(self.temporary.name) / "unsafe-provenance.json"
        provenance.write_text(json.dumps({
            "schema": "cv-experiment-workflow.provenance.v1",
            "scientific_claim_scope": "none", "papers": [],
            "code_sources": [{
                "repo_url": "https://example.invalid/source", "commit": "f" * 40,
                "source_files": [{"path": "../../outside", "symbols": ["Block"]}],
                "use_mode": "adapted", "license_spdx": "MIT",
                "license_compatibility": "compatible",
            }],
            "what_is_copied": [], "what_is_adapted": ["Block"],
            "what_is_new": [], "unresolved_questions": [],
        }), encoding="utf-8")
        self.assertEqual(
            2, self.new_trial("--provenance-file", provenance, check=False).returncode,
        )

    def test_original_hypothesis_allows_zero_papers_and_supplies_new_work(self) -> None:
        trial = json.loads(self.new_trial("--claim-scope", "original-hypothesis").stdout)
        provenance = read_json(
            self.project / ".experiment-workflow/code-assets"
            / trial["code_asset_id"] / "provenance.json"
        )
        self.assertEqual([], provenance["papers"])
        self.assertEqual([], provenance["code_sources"])
        self.assertEqual(
            ["机制：可关闭结构；假设：等待实验验证"],
            provenance["what_is_new"],
        )
        self.assertNotIn("待实现", provenance["what_is_new"][0])
        self.assertEqual(0, run_cli("validate", "--project", self.project).returncode)

    def test_new_trial_recovers_owned_transaction_then_rejects_unknown_entry(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        original_unlink = Path.unlink

        def interrupt_before_marker(path: Path, *args: object, **kwargs: object) -> None:
            if path.name == module.MARKER_NAME:
                raise KeyboardInterrupt("before marker delete")
            original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=interrupt_before_marker):
            with self.assertRaisesRegex(KeyboardInterrupt, "before marker delete"):
                module.new_trial(
                    self.project, self.idea["id"], self.version["id"],
                    "feature-adapter", "feature-output",
                )
        control = self.project / ".experiment-workflow"
        unknown = control / "unknown.txt"
        unknown.write_text("user-owned", encoding="utf-8")
        before = unknown.read_bytes()

        with self.assertRaises(ValueError):
            module.new_trial(
                self.project, self.idea["id"], self.version["id"],
                "feature-adapter", "feature-output",
            )

        self.assertEqual(before, unknown.read_bytes())
        self.assertEqual([], list((control / ".runtime").iterdir()))

    def test_incompatible_or_incomplete_code_license_is_blocked(self) -> None:
        provenance = Path(self.temporary.name) / "bad-license.json"
        provenance.write_text(json.dumps({
            "schema": "cv-experiment-workflow.provenance.v1",
            "scientific_claim_scope": "none", "papers": [],
            "code_sources": [{
                "repo_url": "https://example.invalid/source", "commit": "b" * 40,
                "source_files": [{"path": "model.py", "symbols": ["Block"]}],
                "use_mode": "adapted", "license_spdx": "UNKNOWN",
                "license_compatibility": "compatible",
            }],
            "what_is_copied": [], "what_is_adapted": ["Block"], "what_is_new": [],
            "unresolved_questions": [],
        }), encoding="utf-8")
        result = self.new_trial("--provenance-file", provenance, check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("license", result.stderr.lower())

    def test_provenance_requires_bidirectional_code_use_binding(self) -> None:
        def source(use_mode: str, license_spdx: str = "MIT") -> dict[str, object]:
            return {
                "repo_url": "https://example.invalid/source", "commit": "b" * 40,
                "source_files": [{"path": "model.py", "symbols": ["Block"]}],
                "use_mode": use_mode, "license_spdx": license_spdx,
                "license_compatibility": "compatible",
            }

        cases = (
            ("copied explanation without source", [], ["复制 Block"], []),
            ("copied source without explanation", [source("copied")], [], []),
            ("adapted explanation without source", [], [], ["改编 Block"]),
            ("adapted source without explanation", [source("adapted")], [], []),
            ("inspiration cannot satisfy copied", [source("inspiration")], ["复制 Block"], []),
        )
        for index, (label, sources, copied, adapted) in enumerate(cases):
            with self.subTest(label=label):
                provenance = Path(self.temporary.name) / f"binding-{index}.json"
                provenance.write_text(json.dumps({
                    "schema": "cv-experiment-workflow.provenance.v1",
                    "scientific_claim_scope": "none", "papers": [],
                    "code_sources": sources, "what_is_copied": copied,
                    "what_is_adapted": adapted, "what_is_new": [],
                    "unresolved_questions": [],
                }), encoding="utf-8")
                result = self.new_trial("--provenance-file", provenance, check=False)
                self.assertEqual(2, result.returncode, label)

    def test_copied_or_adapted_code_accepts_only_compatible_spdx_allowlist(self) -> None:
        for index, license_spdx in enumerate(("GPL-3.0-only", "Friendly-1.0", "UNKNOWN")):
            with self.subTest(license=license_spdx):
                provenance = Path(self.temporary.name) / f"spdx-{index}.json"
                provenance.write_text(json.dumps({
                    "schema": "cv-experiment-workflow.provenance.v1",
                    "scientific_claim_scope": "none", "papers": [],
                    "code_sources": [{
                        "repo_url": "https://example.invalid/source", "commit": "b" * 40,
                        "source_files": [{"path": "model.py", "symbols": ["Block"]}],
                        "use_mode": "copied", "license_spdx": license_spdx,
                        "license_compatibility": "compatible",
                    }],
                    "what_is_copied": ["复制 Block"], "what_is_adapted": [],
                    "what_is_new": [], "unresolved_questions": [],
                }), encoding="utf-8")
                result = self.new_trial("--provenance-file", provenance, check=False)
                self.assertEqual(2, result.returncode, license_spdx)

    def test_inspiration_source_does_not_require_copy_or_adaptation_claim(self) -> None:
        provenance = Path(self.temporary.name) / "inspiration.json"
        provenance.write_text(json.dumps({
            "schema": "cv-experiment-workflow.provenance.v1",
            "scientific_claim_scope": "none", "papers": [],
            "code_sources": [{
                "repo_url": "https://example.invalid/source", "commit": "b" * 40,
                "source_files": [{"path": "model.py", "symbols": ["Block"]}],
                "use_mode": "inspiration", "license_spdx": "NOASSERTION",
                "license_compatibility": "unknown",
            }],
            "what_is_copied": [], "what_is_adapted": [], "what_is_new": [],
            "unresolved_questions": ["仅用于定位思想来源，不复制或改编代码"],
        }), encoding="utf-8")
        result = self.new_trial("--provenance-file", provenance, check=False)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_template_scaffolds_have_no_forbidden_claim_tokens_and_compile(self) -> None:
        forbidden = ("sample_project", "CUB", "xlsa17")
        for directory in sorted(path for path in TEMPLATES.iterdir() if path.is_dir()):
            manifest = read_json(directory / "template.json")
            self.assertEqual("structural_scaffold", manifest["template_kind"])
            self.assertEqual("none", manifest["scientific_claim_scope"])
            self.assertEqual([], manifest["papers"])
            self.assertEqual([], manifest["code_sources"])
            source = (directory / "module.py").read_text(encoding="utf-8")
            self.assertFalse(any(token in source for token in forbidden), directory.name)
            compile(source, str(directory / "module.py"), "exec")
            compile((directory / "test_contract.py").read_text(encoding="utf-8"),
                    str(directory / "test_contract.py"), "exec")
            isolated = Path(self.temporary.name) / f"contract-{directory.name}"
            isolated.mkdir()
            result = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(directory / "test_contract.py")],
                cwd=isolated, capture_output=True, text=True, encoding="utf-8",
                timeout=15,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_concurrent_creation_allocates_unique_strict_ids(self) -> None:
        def create(_: int) -> subprocess.CompletedProcess[str]:
            return self.new_trial()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(create, range(8)))
        ids = sorted(json.loads(result.stdout)["id"] for result in results)
        self.assertEqual([f"TRIAL-{number:04d}" for number in range(1, 9)], ids)
        assets = list((self.project / ".experiment-workflow/code-assets").iterdir())
        self.assertEqual(8, len(assets))

    def test_provenance_symlink_is_rejected(self) -> None:
        source = Path(self.temporary.name) / "source.json"
        source.write_text("{}", encoding="utf-8")
        link = Path(self.temporary.name) / "link.json"
        try:
            link.symlink_to(source)
        except OSError as error:
            self.skipTest(f"当前环境不支持符号链接：{error}")
        result = self.new_trial("--provenance-file", link, check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("普通文件", result.stderr)

    def test_validate_rejects_missing_idea_reference(self) -> None:
        self.new_trial()
        idea_path = (
            self.project / ".experiment-workflow/ideas" / f"{self.idea['id']}.json"
        )
        idea_path.rename(idea_path.with_suffix(".removed"))
        result = run_cli("validate", "--project", self.project, check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn(self.idea["id"], result.stderr)

    def test_validate_rejects_incomplete_trial_metadata(self) -> None:
        trial = json.loads(self.new_trial().stdout)
        trial_path = (
            self.project / ".experiment-workflow/trials" / trial["id"] / "trial.json"
        )
        payload = read_json(trial_path)
        payload["template_selection_reason"] = ""
        trial_path.write_text(json.dumps(payload), encoding="utf-8")
        result = run_cli("validate", "--project", self.project, check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("Trial", result.stderr)

    def test_validate_rejects_all_trial_asset_fact_drift(self) -> None:
        trial = json.loads(self.new_trial().stdout)
        asset_path = (
            self.project / ".experiment-workflow/code-assets"
            / trial["code_asset_id"] / "asset.json"
        )
        original = read_json(asset_path)
        drifts = {
            "status": "blocked_provenance",
            "template_family": "fusion-gate",
            "attachment_point": "other-point",
            "template_selection_reason": "different reason",
            "transaction_id": "b" * 32,
            "created_at": "2020-01-01T00:00:00Z",
            "trial_id": "TRIAL-9999",
            "template_sha256": "b" * 64,
        }
        for field, changed in drifts.items():
            with self.subTest(field=field):
                payload = dict(original)
                payload[field] = changed
                asset_path.write_text(json.dumps(payload), encoding="utf-8")
                result = run_cli("validate", "--project", self.project, check=False)
                self.assertEqual(2, result.returncode, field)
                asset_path.write_text(json.dumps(original), encoding="utf-8")

    def test_validate_rejects_linked_transaction_parent_directories(self) -> None:
        control = self.project / ".experiment-workflow"
        for directory_name in ("trials", "code-assets", ".runtime"):
            with self.subTest(directory=directory_name):
                directory = control / directory_name
                external = Path(self.temporary.name) / f"external-validate-{directory_name.strip('.')}"
                external.mkdir()
                sentinel = external / "sentinel.bin"
                sentinel.write_bytes(b"outside")
                directory.rmdir()
                try:
                    directory.symlink_to(external, target_is_directory=True)
                except OSError as error:
                    self.skipTest(f"当前环境不支持目录符号链接：{error}")
                before = self.snapshot_tree(external)
                result = run_cli("validate", "--project", self.project, check=False)
                self.assertEqual(2, result.returncode, directory_name)
                self.assertEqual(before, self.snapshot_tree(external), directory_name)
                directory.unlink()
                directory.mkdir()

    def test_new_trial_rejects_linked_transaction_parent_directories(self) -> None:
        for index, directory_name in enumerate(("trials", "code-assets", ".runtime")):
            with self.subTest(directory=directory_name):
                project, idea, version = self.create_ready_project(f"linked-new-{index}")
                directory = project / ".experiment-workflow" / directory_name
                external = Path(self.temporary.name) / f"external-new-{index}"
                external.mkdir()
                (external / "sentinel.bin").write_bytes(b"outside")
                directory.rmdir()
                try:
                    directory.symlink_to(external, target_is_directory=True)
                except OSError as error:
                    self.skipTest(f"当前环境不支持目录符号链接：{error}")
                before = self.snapshot_tree(external)
                result = run_cli(
                    "new-trial", "--project", project, "--idea", idea["id"],
                    "--base-version", version["id"], "--template-family",
                    "feature-adapter", "--attachment-point", "feature-output",
                    check=False,
                )
                self.assertEqual(2, result.returncode, directory_name)
                self.assertEqual(before, self.snapshot_tree(external), directory_name)

    def test_recovery_rejects_linked_parents_without_changing_external_tree(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        for index, directory_name in enumerate(("trials", "code-assets", ".runtime")):
            with self.subTest(directory=directory_name):
                project, idea, version = self.create_ready_project(f"linked-recovery-{index}")
                original_replace = module.os.replace

                def interrupt_after_trial(source: object, target: object) -> None:
                    original_replace(source, target)
                    if Path(target).parent.name == "trials":
                        raise KeyboardInterrupt("hard stop")

                with mock.patch.object(module.os, "replace", side_effect=interrupt_after_trial):
                    with self.assertRaisesRegex(KeyboardInterrupt, "hard stop"):
                        module.new_trial(
                            project, idea["id"], version["id"],
                            "feature-adapter", "feature-output",
                        )
                parent = project / ".experiment-workflow" / directory_name
                external = Path(self.temporary.name) / f"external-recovery-{index}"
                parent.rename(external)
                try:
                    parent.symlink_to(external, target_is_directory=True)
                except OSError as error:
                    self.skipTest(f"当前环境不支持目录符号链接：{error}")
                before = self.snapshot_tree(external)
                result = run_cli(
                    "new-trial", "--project", project, "--idea", idea["id"],
                    "--base-version", version["id"], "--template-family",
                    "feature-adapter", "--attachment-point", "feature-output",
                    check=False,
                )
                self.assertEqual(2, result.returncode, directory_name)
                self.assertEqual(before, self.snapshot_tree(external), directory_name)

    def test_catchable_publish_failure_leaves_no_object_and_retry_reuses_ids(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        original_replace = module.os.replace

        def fail_asset_publish(source: object, target: object) -> None:
            target_path = Path(target)
            if target_path.parent.name == "code-assets":
                raise OSError("asset publish failed")
            original_replace(source, target)

        with mock.patch.object(module.os, "replace", side_effect=fail_asset_publish):
            with self.assertRaisesRegex(OSError, "asset publish failed"):
                module.new_trial(
                    self.project, self.idea["id"], self.version["id"],
                    "feature-adapter", "feature-output",
                )
        trial = json.loads(self.new_trial().stdout)
        self.assertEqual("TRIAL-0001", trial["id"])
        self.assertEqual("CODE-0001", trial["code_asset_id"])

    def test_recovery_never_follows_replaced_formal_object_directories(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        for index, target_parent in enumerate(("trials", "code-assets")):
            with self.subTest(parent=target_parent):
                project, idea, version = self.create_ready_project(f"formal-swap-{index}")
                original_replace = module.os.replace

                def interrupt(source: object, target: object) -> None:
                    original_replace(source, target)
                    if Path(target).parent.name == target_parent:
                        raise KeyboardInterrupt("hard stop")

                with mock.patch.object(module.os, "replace", side_effect=interrupt):
                    with self.assertRaisesRegex(KeyboardInterrupt, "hard stop"):
                        module.new_trial(
                            project, idea["id"], version["id"],
                            "feature-adapter", "feature-output",
                        )
                object_id = "TRIAL-0001" if target_parent == "trials" else "CODE-0001"
                formal = project / ".experiment-workflow" / target_parent / object_id
                external = Path(self.temporary.name) / f"external-formal-{index}"
                formal.rename(external)
                try:
                    formal.symlink_to(external, target_is_directory=True)
                except OSError as error:
                    self.skipTest(f"当前环境不支持目录符号链接：{error}")
                before = self.snapshot_tree(external)
                retry = run_cli(
                    "new-trial", "--project", project, "--idea", idea["id"],
                    "--base-version", version["id"], "--template-family",
                    "feature-adapter", "--attachment-point", "feature-output", check=False,
                )
                self.assertEqual(2, retry.returncode)
                self.assertEqual(before, self.snapshot_tree(external))

    def test_cleanup_never_follows_replaced_staging_root(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        original_write = module.atomic_write_json

        def interrupt(path: Path, payload: object) -> None:
            original_write(path, payload)
            if Path(path).name == "asset.json":
                raise KeyboardInterrupt("prepare hard stop")

        with mock.patch.object(module, "atomic_write_json", side_effect=interrupt):
            with self.assertRaisesRegex(KeyboardInterrupt, "prepare hard stop"):
                module.new_trial(
                    self.project, self.idea["id"], self.version["id"],
                    "feature-adapter", "feature-output",
                )
        runtime = self.project / ".experiment-workflow/.runtime"
        staging = next(runtime.glob("new-trial-*"))
        external = Path(self.temporary.name) / "external-staging"
        staging.rename(external)
        try:
            staging.symlink_to(external, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"当前环境不支持目录符号链接：{error}")
        before = self.snapshot_tree(external)
        with self.assertRaises(RuntimeError):
            module._cleanup_unpublished_staging(staging, "TRIAL-0001", "CODE-0001")
        self.assertEqual(before, self.snapshot_tree(external))

    def test_cleanup_rejects_staging_child_swap_after_file_check(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        original_is_file = Path.is_file
        for index, (object_id, filenames) in enumerate((
            ("TRIAL-0001", ("trial.json",)),
            ("CODE-0001", module.ASSET_FILES),
        )):
            with self.subTest(object_id=object_id):
                staging = Path(self.temporary.name) / f"child-swap-{index}"
                child = staging / object_id
                child.mkdir(parents=True)
                for filename in filenames:
                    (child / filename).write_bytes(f"outside-{filename}".encode())
                external = Path(self.temporary.name) / f"external-child-{index}"
                before = self.snapshot_tree(child)
                target = child / filenames[0]
                swapped = False

                def swap_after_check(path: Path) -> bool:
                    nonlocal swapped
                    result = original_is_file(path)
                    if path == target and result and not swapped:
                        swapped = True
                        child.rename(external)
                        child.symlink_to(external, target_is_directory=True)
                    return result

                with mock.patch.object(Path, "is_file", new=swap_after_check):
                    with self.assertRaisesRegex(RuntimeError, object_id):
                        module._cleanup_unpublished_staging(
                            staging, "TRIAL-0001", "CODE-0001",
                        )
                self.assertEqual(before, self.snapshot_tree(external))

    def test_committed_transaction_cleanup_failure_returns_committed_trial(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        original_unlink = Path.unlink
        failed = False

        def fail_once(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal failed
            if path.name == "transaction.json" and not failed:
                failed = True
                raise OSError("staging cleanup failed")
            original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=fail_once):
            trial = module.new_trial(
                self.project, self.idea["id"], self.version["id"],
                "feature-adapter", "feature-output",
            )
        self.assertEqual("TRIAL-0001", trial["id"])
        self.assertEqual(0, run_cli("validate", "--project", self.project).returncode)

    def test_marker_interrupt_before_and_after_delete_has_committed_semantics(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        original_unlink = Path.unlink

        def interrupt_before(path: Path, *args: object, **kwargs: object) -> None:
            if path.name == module.MARKER_NAME:
                raise KeyboardInterrupt("before marker delete")
            original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=interrupt_before):
            with self.assertRaisesRegex(KeyboardInterrupt, "before marker delete"):
                module.new_trial(
                    self.project, self.idea["id"], self.version["id"],
                    "feature-adapter", "feature-output",
                )
        committed = read_json(
            self.project / ".experiment-workflow/trials/TRIAL-0001/trial.json"
        )
        recovered = module.new_trial(
            self.project, self.idea["id"], self.version["id"],
            "feature-adapter", "feature-output",
        )
        self.assertEqual(committed["transaction_id"], recovered["transaction_id"])

        project, idea, version = self.create_ready_project("marker-after")
        raised = False

        def interrupt_after(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal raised
            original_unlink(path, *args, **kwargs)
            if path.name == module.MARKER_NAME and not raised:
                raised = True
                raise KeyboardInterrupt("after marker delete")

        with mock.patch.object(Path, "unlink", new=interrupt_after):
            result = module.new_trial(
                project, idea["id"], version["id"],
                "feature-adapter", "feature-output",
            )
        self.assertEqual("TRIAL-0001", result["id"])

    def test_committed_recovery_is_idempotent_only_for_same_request(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        original_unlink = Path.unlink

        def interrupt_before_marker(path: Path, *args: object, **kwargs: object) -> None:
            if path.name == module.MARKER_NAME:
                raise KeyboardInterrupt("before marker delete")
            original_unlink(path, *args, **kwargs)

        same_project, same_idea, same_version = self.create_ready_project("same-request")
        with mock.patch.object(Path, "unlink", new=interrupt_before_marker):
            with self.assertRaises(KeyboardInterrupt):
                module.new_trial(
                    same_project, same_idea["id"], same_version["id"],
                    "feature-adapter", "feature-output",
                )
        same = module.new_trial(
            same_project, same_idea["id"], same_version["id"],
            "feature-adapter", "feature-output",
        )
        self.assertEqual("TRIAL-0001", same["id"])

        other_project, other_idea, other_version = self.create_ready_project("other-request")
        with mock.patch.object(Path, "unlink", new=interrupt_before_marker):
            with self.assertRaises(KeyboardInterrupt):
                module.new_trial(
                    other_project, other_idea["id"], other_version["id"],
                    "feature-adapter", "feature-output",
                )
        other = module.new_trial(
            other_project, other_idea["id"], other_version["id"],
            "fusion-gate", "fusion-input",
        )
        self.assertEqual("TRIAL-0002", other["id"])
        self.assertEqual("fusion-gate", other["template_family"])
        self.assertTrue(
            (other_project / ".experiment-workflow/trials/TRIAL-0001/trial.json").is_file()
        )

    def test_recovery_rejects_marker_without_request_digest(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        original_unlink = Path.unlink

        def interrupt_before_marker(path: Path, *args: object, **kwargs: object) -> None:
            if path.name == module.MARKER_NAME:
                raise KeyboardInterrupt("before marker delete")
            original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=interrupt_before_marker):
            with self.assertRaises(KeyboardInterrupt):
                module.new_trial(
                    self.project, self.idea["id"], self.version["id"],
                    "feature-adapter", "feature-output",
                )
        marker_path = self.project / ".experiment-workflow/.runtime" / module.MARKER_NAME
        marker = read_json(marker_path)
        marker.pop("request_digest", None)
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        result = self.new_trial(check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("request_digest", result.stderr)

    def test_catchable_prepare_failure_removes_tool_owned_staging(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        original_write = module.atomic_write_json

        def fail_asset_json(path: Path, payload: object) -> None:
            if Path(path).name == "asset.json":
                raise OSError("prepare failed")
            original_write(path, payload)

        with mock.patch.object(module, "atomic_write_json", side_effect=fail_asset_json):
            with self.assertRaisesRegex(OSError, "prepare failed"):
                module.new_trial(
                    self.project, self.idea["id"], self.version["id"],
                    "feature-adapter", "feature-output",
                )
        runtime = self.project / ".experiment-workflow/.runtime"
        self.assertEqual([], list(runtime.glob("new-trial-*")))
        trial = json.loads(self.new_trial().stdout)
        self.assertEqual("TRIAL-0001", trial["id"])

    def test_hard_interrupt_is_recovered_but_unknown_file_is_preserved(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        original_replace = module.os.replace

        def interrupt_after_trial(source: object, target: object) -> None:
            target_path = Path(target)
            original_replace(source, target)
            if target_path.parent.name == "trials":
                raise KeyboardInterrupt("hard stop")

        with mock.patch.object(module.os, "replace", side_effect=interrupt_after_trial):
            with self.assertRaisesRegex(KeyboardInterrupt, "hard stop"):
                module.new_trial(
                    self.project, self.idea["id"], self.version["id"],
                    "feature-adapter", "feature-output",
                )
        partial = self.project / ".experiment-workflow/trials/TRIAL-0001"
        user_file = partial / "user-note.txt"
        user_file.write_text("keep me", encoding="utf-8")
        recovery = self.new_trial(check=False)
        self.assertEqual(2, recovery.returncode)
        self.assertEqual("keep me", user_file.read_text(encoding="utf-8"))
        self.assertIn("unknown/用户文件", recovery.stderr)

    def test_hard_interrupt_during_prepare_is_recovered_on_retry(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        module = importlib.import_module("workflow_core.templates")
        original_write = module.atomic_write_json

        def interrupt_asset_json(path: Path, payload: object) -> None:
            if Path(path).name == "asset.json":
                raise KeyboardInterrupt("prepare hard stop")
            original_write(path, payload)

        with mock.patch.object(module, "atomic_write_json", side_effect=interrupt_asset_json):
            with self.assertRaisesRegex(KeyboardInterrupt, "prepare hard stop"):
                module.new_trial(
                    self.project, self.idea["id"], self.version["id"],
                    "feature-adapter", "feature-output",
                )
        trial = json.loads(self.new_trial().stdout)
        self.assertEqual("TRIAL-0001", trial["id"])
        runtime = self.project / ".experiment-workflow/.runtime"
        self.assertEqual([], list(runtime.glob("new-trial-*")))


class ReferencePackTests(unittest.TestCase):
    def test_gzsl_reference_pack_has_exactly_four_primary_cards(self) -> None:
        pack = read_json(SKILL / "assets/reference-packs/gzsl.json")
        self.assertEqual(4, len(pack["papers"]))
        self.assertEqual(
            {("Chao", 2016), ("Xian", 2017), ("Xian", 2018), ("Schonfeld", 2019)},
            {(paper["authors"][0].split()[-1], paper["year"]) for paper in pack["papers"]},
        )
        for paper in pack["papers"]:
            self.assertEqual("primary_mechanism", paper["role"])
            self.assertEqual(
                {"role", "title", "authors", "year", "venue", "url"}, set(paper)
            )

    def test_gzsl_reference_pack_has_correct_changpinyo_and_cvf_url(self) -> None:
        pack = read_json(SKILL / "assets/reference-packs/gzsl.json")
        chao = next(paper for paper in pack["papers"] if paper["year"] == 2016)
        xian = next(paper for paper in pack["papers"] if paper["year"] == 2017)
        self.assertIn("Soravit Changpinyo", chao["authors"])
        self.assertNotIn("Sorin Changpinyo", chao["authors"])
        self.assertEqual(
            "https://openaccess.thecvf.com/content_cvpr_2017/html/"
            "Xian_Zero-Shot_Learning_-_CVPR_2017_paper.html",
            xian["url"],
        )


if __name__ == "__main__":
    unittest.main()
