from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._helpers import CLI, cli_json, read_json, run_cli


MANIFEST_SCHEMA = "cv-experiment-workflow.template-manifest.v1"
PROJECT_TEMPLATE_SCHEMA = "cv-experiment-workflow.project-template.v1"


class ProjectTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.repo = self.root / "source"
        run_cli("init", "--path", self.project, "--name", "模板登记测试")
        self._init_repo()
        self.manifest_path = self.root / "template-manifest.json"
        self._write_manifest(self._manifest())

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode != 0:
            self.fail(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result.stdout.strip()

    def _init_repo(self) -> None:
        self.repo.mkdir()
        subprocess.run(["git", "init", str(self.repo)], check=True, capture_output=True)
        self._git("config", "user.email", "template-tests@example.invalid")
        self._git("config", "user.name", "Template Tests")
        self._git("config", "core.autocrlf", "false")
        source = self.repo / "templates" / "adapter"
        source.mkdir(parents=True)
        (source / "module.py").write_text(
            "class Adapter:\n    pass\n", encoding="utf-8",
        )
        (source / "contract.txt").write_text("input -> output\n", encoding="utf-8")
        (source / "unlisted.bin").write_bytes(b"not part of the template facts")
        implementation = self.repo / "models" / "adapter.py"
        implementation.parent.mkdir()
        implementation.write_text("def apply(value):\n    return value\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "test fixture")
        self._git("remote", "add", "origin", "https://example.invalid/formal-code.git")
        self.commit = self._git("rev-parse", "HEAD")

    def _digest(self, relative: str) -> str:
        content = (self.repo / "templates" / "adapter" / relative).read_bytes()
        return f"sha256:{hashlib.sha256(content).hexdigest()}"

    def _manifest(self) -> dict[str, object]:
        return {
            "schema": MANIFEST_SCHEMA,
            "name": "轻量适配器 <TPL>",
            "description": "离线、可复核的模板 & 框架",
            "code_source": {
                "repo_url": "local:test-fixture",
                "commit": self.commit.upper(),
                "template_path": "templates/adapter",
            },
            "listed_files": [
                {"path": "module.py", "role": "模型入口", "digest": self._digest("module.py")},
                {"path": "contract.txt", "role": "接口契约", "digest": self._digest("contract.txt")},
            ],
            "interfaces": {
                "data": "batch",
                "model": "Adapter",
                "training": "train_step",
                "evaluation": "evaluate",
                "config": "mapping",
                "seed": "integer",
                "metrics": "mapping",
                "checkpoint": "path",
            },
            "attachment_points": [
                {
                    "name": "feature-output",
                    "contract": "tensor -> tensor",
                    "target_path": "models/adapter.py",
                    "disabled_behavior": "identity",
                }
            ],
            "provenance_sources": [
                {
                    "label": "本地测试实现",
                    "locator": "fixture:templates/adapter",
                    "identity": "测试仓库中的原创夹具",
                    "commit": None,
                    "license_spdx": "MIT",
                    "files": [],
                    "symbols": [],
                    "use_mode": "reimplementation",
                    "note": "仅用于测试，不声称来自公开仓库",
                }
            ],
            "framework": {
                "nodes": [
                    {"id": "input", "label": "输入 <batch>", "kind": "data"},
                    {"id": "adapter", "label": "适配器 & gate", "kind": "model"},
                ],
                "edges": [
                    {"source": "input", "target": "adapter", "label": "features"}
                ],
            },
        }

    def _write_manifest(self, payload: dict[str, object]) -> None:
        self.manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )

    def _register(self, *, check: bool = True):
        return run_cli(
            "register-template",
            "--project", self.project,
            "--manifest", self.manifest_path,
            "--code-root", self.repo,
            check=check,
        )

    def _register_json(self) -> dict[str, object]:
        result = self._register()
        payload = json.loads(result.stdout)
        self.assertIsInstance(payload, dict)
        return payload

    def _set_mapping(
        self,
        idea_id: object,
        *,
        attachment_point: str = "feature-output",
        target_path: str = "models/adapter.py",
        disabled_behavior: str = "identity",
    ) -> dict[str, object]:
        idea = read_json(
            self.project / ".experiment-workflow" / "ideas" / f"{idea_id}.json"
        )
        return cli_json(
            "set-implementation-mapping", "--project", self.project,
            "--idea", idea_id, "--mapping", json.dumps({
                "problem": idea["problem"],
                "mechanism": idea["mechanism"],
                "attachment_point": attachment_point,
                "target_path": target_path,
                "validation": "python -m pytest -q",
                "disabled_behavior": disabled_behavior,
            }),
        )

    def _create_formal_trial(self) -> tuple[dict[str, object], dict[str, object]]:
        template = self._register_json()
        idea = cli_json(
            "new-idea", "--project", self.project, "--title", "正式测试",
            "--note", "正式组合与绑定",
        )
        cli_json(
            "activate-idea", "--project", self.project, "--idea", idea["id"],
            "--problem", "验证正式路径", "--mechanism", "引用外部实现",
            "--hypothesis", "账本严格冻结",
        )
        attachment = template["attachment_points"][0]
        self._set_mapping(
            idea["id"],
            attachment_point=attachment["name"],
            target_path=attachment["target_path"],
            disabled_behavior=attachment["disabled_behavior"],
        )
        version = cli_json(
            "register-version", "--project", self.project, "--name", "模板基线",
            "--repo-url", "https://example.invalid/formal-code.git",
            "--commit", self.commit,
        )
        trial = cli_json(
            "new-trial", "--project", self.project, "--idea", idea["id"],
            "--base-version", version["id"], "--template", template["template_id"],
            "--attachment-point", "feature-output",
        )
        return template, trial

    def _future_version_validator(self, templates_module, future_id: str):
        original = templates_module.validate_version

        def validate(payload: dict[str, object], *, expected_id: str) -> None:
            if expected_id != future_id:
                original(payload, expected_id=expected_id)
                return
            self.assertEqual(future_id, payload["id"])
            self.assertEqual("active", payload["status"])
            self.assertEqual("TPL-0001", payload["template_id"])
            self.assertIsInstance(payload["accepted_code_asset_ids"], list)

        return validate

    def _new_active_idea(
        self,
        title: str,
        *,
        attachment_point: str = "feature-output",
        target_path: str = "models/adapter.py",
    ) -> dict[str, object]:
        idea = cli_json(
            "new-idea", "--project", self.project, "--title", title,
            "--note", "目标冲突测试",
        )
        cli_json(
            "activate-idea", "--project", self.project, "--idea", idea["id"],
            "--problem", "目标冲突", "--mechanism", "继承组合",
            "--hypothesis", "冲突应被拒绝",
        )
        self._set_mapping(
            idea["id"], attachment_point=attachment_point, target_path=target_path,
        )
        return idea

    def _create_two_bound_formal_assets(self):
        manifest = self._manifest()
        manifest["attachment_points"].append({  # type: ignore[union-attr]
            "name": "other-output",
            "contract": "value -> value",
            "target_path": "models/other.py",
            "disabled_behavior": "identity",
        })
        self._write_manifest(manifest)
        template, first = self._create_formal_trial()
        second_idea = self._new_active_idea("第二个继承资产")
        second = cli_json(
            "new-trial", "--project", self.project, "--idea", second_idea["id"],
            "--base-version", "VER-0001", "--template", template["template_id"],
            "--attachment-point", "feature-output",
        )
        for trial in (first, second):
            cli_json(
                "sync-code-asset", "--project", self.project,
                "--asset", trial["code_asset_id"], "--code-root", self.repo,
                "--relative-path", "models/adapter.py",
            )
        return template, first, second

    def test_formal_new_trial_records_template_composition_without_payload(self) -> None:
        template = self._register_json()
        idea = cli_json(
            "new-idea", "--project", self.project,
            "--title", "正式组合", "--note", "仅引用登记模板",
        )
        cli_json(
            "activate-idea", "--project", self.project, "--idea", idea["id"],
            "--problem", "验证组合", "--mechanism", "禁用默认骨架",
            "--hypothesis", "外部实现绑定后可运行",
        )
        self._set_mapping(idea["id"])
        version = cli_json(
            "register-version", "--project", self.project, "--name", "模板基线",
            "--repo-url", "local:test-fixture", "--commit", self.commit,
        )

        trial = cli_json(
            "new-trial", "--project", self.project, "--idea", idea["id"],
            "--base-version", version["id"], "--template", template["template_id"],
            "--attachment-point", "feature-output",
        )

        control = self.project / ".experiment-workflow"
        asset_dir = control / "code-assets" / trial["code_asset_id"]
        asset = read_json(asset_dir / "asset.json")
        composition = read_json(asset_dir / "composition.json")
        self.assertEqual("cv-experiment-workflow.trial.v2", trial["schema"])
        self.assertEqual("cv-experiment-workflow.code-asset.v2", asset["schema"])
        self.assertEqual(template["template_id"], trial["template_id"])
        self.assertEqual(template["template_id"], asset["template_id"])
        self.assertIsNone(asset["external_code_ref"])
        self.assertEqual(
            {
                "schema": "cv-experiment-workflow.composition.v1",
                "template_id": template["template_id"],
                "base_version_id": version["id"],
                "base_commit": self.commit,
                "inherited_code_asset_ids": [],
                "new_code_asset_id": trial["code_asset_id"],
                "attachment_point": "feature-output",
                "target_path": "models/adapter.py",
            },
            composition,
        )
        self.assertEqual(
            {"asset.json", "module.py", "test_contract.py", "provenance.json",
             "framework.html", "composition.json"},
            {path.name for path in asset_dir.iterdir()},
        )
        self.assertNotIn("class Adapter", (asset_dir / "module.py").read_text(encoding="utf-8"))
        self.assertIn(
            template["template_id"],
            (asset_dir / "framework.html").read_text(encoding="utf-8"),
        )
        self.assertFalse(any(path.name == "payload" for path in control.rglob("*")))
        validation = cli_json("validate", "--project", self.project)
        self.assertEqual(1, validation["trials"])
        self.assertEqual(1, validation["code_assets"])

    def test_future_version_rejects_inherited_target_conflict_with_new_asset(self) -> None:
        _template, first_trial = self._create_formal_trial()
        cli_json(
            "sync-code-asset", "--project", self.project,
            "--asset", first_trial["code_asset_id"], "--code-root", self.repo,
            "--relative-path", "models/adapter.py",
        )
        control = self.project / ".experiment-workflow"
        base = read_json(control / "versions" / "VER-0001.json")
        future = {
            **base,
            "id": "VER-0002",
            "name": "future active lineage",
            "template_id": "TPL-0001",
            "accepted_code_asset_ids": [first_trial["code_asset_id"]],
        }
        (control / "versions" / "VER-0002.json").write_text(
            json.dumps(future, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        idea = self._new_active_idea("继承与本次冲突")
        sys.path.insert(0, str(CLI.parent))
        self.addCleanup(lambda: sys.path.remove(str(CLI.parent)))
        templates_module = importlib.import_module("workflow_core.templates")
        before_trials = sorted(path.name for path in (control / "trials").iterdir())
        before_assets = sorted(path.name for path in (control / "code-assets").iterdir())

        with mock.patch.object(
            templates_module,
            "validate_version",
            side_effect=self._future_version_validator(templates_module, "VER-0002"),
        ):
            with self.assertRaisesRegex(ValueError, "target|目标|冲突"):
                templates_module.new_trial(
                    self.project, idea["id"], "VER-0002", None,
                    "feature-output", template_id="TPL-0001",
                )

        self.assertEqual(
            before_trials, sorted(path.name for path in (control / "trials").iterdir()),
        )
        self.assertEqual(
            before_assets, sorted(path.name for path in (control / "code-assets").iterdir()),
        )
        self.assertEqual([], list((control / ".runtime").iterdir()))

    def test_lineage_loader_rejects_duplicate_inherited_target_paths(self) -> None:
        _template, first, second = self._create_two_bound_formal_assets()
        sys.path.insert(0, str(CLI.parent))
        self.addCleanup(lambda: sys.path.remove(str(CLI.parent)))
        formal = importlib.import_module("workflow_core.formal_composition")
        control = self.project / ".experiment-workflow"

        with self.assertRaisesRegex(ValueError, "target|目标|冲突|重复"):
            formal.load_inherited_compositions(
                control,
                [first["code_asset_id"], second["code_asset_id"]],
                "TPL-0001",
            )

    def test_validate_rejects_duplicate_inherited_target_paths(self) -> None:
        _template, first, second = self._create_two_bound_formal_assets()
        third_idea = self._new_active_idea(
            "非冲突的新落点",
            attachment_point="other-output", target_path="models/other.py",
        )
        third = cli_json(
            "new-trial", "--project", self.project, "--idea", third_idea["id"],
            "--base-version", "VER-0001", "--template", "TPL-0001",
            "--attachment-point", "other-output",
        )
        control = self.project / ".experiment-workflow"
        base = read_json(control / "versions" / "VER-0001.json")
        future = {
            **base,
            "id": "VER-0002",
            "name": "future duplicate lineage",
            "template_id": "TPL-0001",
            "accepted_code_asset_ids": [
                first["code_asset_id"], second["code_asset_id"],
            ],
        }
        (control / "versions" / "VER-0002.json").write_text(
            json.dumps(future, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        trial_path = control / "trials" / third["id"] / "trial.json"
        trial_payload = read_json(trial_path)
        trial_payload["base_version_id"] = "VER-0002"
        trial_path.write_text(
            json.dumps(trial_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        asset_dir = control / "code-assets" / third["code_asset_id"]
        composition_path = asset_dir / "composition.json"
        composition = read_json(composition_path)
        composition["base_version_id"] = "VER-0002"
        composition["inherited_code_asset_ids"] = future["accepted_code_asset_ids"]
        composition_path.write_text(
            json.dumps(composition, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        asset_path = asset_dir / "asset.json"
        asset = read_json(asset_path)
        asset["files"]["composition.json"] = hashlib.sha256(
            composition_path.read_bytes()
        ).hexdigest()
        asset_path.write_text(
            json.dumps(asset, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        sys.path.insert(0, str(CLI.parent))
        self.addCleanup(lambda: sys.path.remove(str(CLI.parent)))
        templates_module = importlib.import_module("workflow_core.templates")
        rendering_module = importlib.import_module("workflow_core.rendering")
        (asset_dir / "framework.html").write_bytes(
            rendering_module.render_code_asset(asset_dir)
        )

        with mock.patch.object(
            templates_module,
            "validate_version",
            side_effect=self._future_version_validator(templates_module, "VER-0002"),
        ):
            with self.assertRaisesRegex(ValueError, "target|目标|冲突|重复"):
                templates_module._validate_project_trials_locked(control)

    def test_formal_sync_binds_external_git_file_idempotently(self) -> None:
        template = self._register_json()
        idea = cli_json(
            "new-idea", "--project", self.project, "--title", "绑定",
            "--note", "外部代码引用",
        )
        cli_json(
            "activate-idea", "--project", self.project, "--idea", idea["id"],
            "--problem", "绑定验证", "--mechanism", "外部实现",
            "--hypothesis", "摘要可冻结",
        )
        self._set_mapping(idea["id"])
        version = cli_json(
            "register-version", "--project", self.project, "--name", "模板基线",
            "--repo-url", "https://example.invalid/formal-code.git",
            "--commit", self.commit,
        )
        trial = cli_json(
            "new-trial", "--project", self.project, "--idea", idea["id"],
            "--base-version", version["id"], "--template", template["template_id"],
            "--attachment-point", "feature-output",
        )
        args = (
            "sync-code-asset", "--project", self.project,
            "--asset", trial["code_asset_id"], "--code-root", self.repo,
            "--relative-path", "models/adapter.py",
        )

        bound = cli_json(*args)
        repeated = cli_json(*args)

        expected_digest = "sha256:" + hashlib.sha256(
            (self.repo / "models" / "adapter.py").read_bytes()
        ).hexdigest()
        self.assertEqual(bound, repeated)
        self.assertEqual(
            {
                "repo_url": "https://example.invalid/formal-code.git",
                "commit": self.commit,
                "relative_path": "models/adapter.py",
                "digest": expected_digest,
            },
            bound["external_code_ref"],
        )
        cli_json("validate", "--project", self.project)

    def test_formal_new_trial_bad_reference_attachment_or_base_leaves_no_objects(self) -> None:
        template = self._register_json()
        idea = cli_json(
            "new-idea", "--project", self.project, "--title", "失败路径",
            "--note", "不得残留",
        )
        cli_json(
            "activate-idea", "--project", self.project, "--idea", idea["id"],
            "--problem", "失败路径", "--mechanism", "严格拒绝",
            "--hypothesis", "控制面不变",
        )
        good_version = cli_json(
            "register-version", "--project", self.project, "--name", "正确基线",
            "--repo-url", "local:test-fixture", "--commit", self.commit,
        )
        bad_version = cli_json(
            "register-version", "--project", self.project, "--name", "错误基线",
            "--repo-url", "local:test-fixture", "--commit", "f" * 40,
        )
        common = (
            "new-trial", "--project", self.project, "--idea", idea["id"],
            "--attachment-point", "feature-output",
        )
        cases = (
            (*common, "--base-version", good_version["id"], "--template", "TPL-9999"),
            (*common, "--base-version", bad_version["id"], "--template", template["template_id"]),
            (
                "new-trial", "--project", self.project, "--idea", idea["id"],
                "--base-version", good_version["id"], "--template", template["template_id"],
                "--attachment-point", "missing-point",
            ),
        )
        for args in cases:
            with self.subTest(args=args[-1]):
                result = run_cli(*args, check=False)
                self.assertEqual(2, result.returncode)
                control = self.project / ".experiment-workflow"
                self.assertEqual([], list((control / "trials").iterdir()))
                self.assertEqual([], list((control / "code-assets").iterdir()))
                self.assertEqual([], list((control / ".runtime").iterdir()))

    def test_formal_new_trial_rejects_demo_provenance_options(self) -> None:
        template = self._register_json()
        idea = cli_json(
            "new-idea", "--project", self.project, "--title", "出处边界",
            "--note", "formal 不接收 demo 声明",
        )
        cli_json(
            "activate-idea", "--project", self.project, "--idea", idea["id"],
            "--problem", "出处边界", "--mechanism", "模板引用",
            "--hypothesis", "严格分流",
        )
        version = cli_json(
            "register-version", "--project", self.project, "--name", "模板基线",
            "--repo-url", "local:test-fixture", "--commit", self.commit,
        )
        result = run_cli(
            "new-trial", "--project", self.project, "--idea", idea["id"],
            "--base-version", version["id"], "--template", template["template_id"],
            "--attachment-point", "feature-output",
            "--claim-scope", "original-hypothesis", check=False,
        )
        self.assertEqual(2, result.returncode)
        control = self.project / ".experiment-workflow"
        self.assertEqual([], list((control / "trials").iterdir()))
        self.assertEqual([], list((control / "code-assets").iterdir()))

    def test_formal_sync_rejects_partial_unsafe_dirty_and_conflicting_bindings(self) -> None:
        _template, trial = self._create_formal_trial()
        asset_path = (
            self.project / ".experiment-workflow" / "code-assets"
            / trial["code_asset_id"] / "asset.json"
        )
        base = (
            "sync-code-asset", "--project", self.project,
            "--asset", trial["code_asset_id"],
        )
        self.assertIsNone(cli_json(*base)["external_code_ref"])
        rejected = (
            (*base, "--code-root", self.repo),
            (*base, "--relative-path", "models/adapter.py"),
            (*base, "--code-root", self.repo, "--relative-path", "../adapter.py"),
            (*base, "--code-root", self.repo, "--relative-path", "templates/adapter/module.py"),
            (*base, "--code-root", self.repo / "models", "--relative-path", "models/adapter.py"),
        )
        for args in rejected:
            with self.subTest(args=args[-2:]):
                before = asset_path.read_bytes()
                result = run_cli(*args, check=False)
                self.assertEqual(2, result.returncode)
                self.assertEqual(before, asset_path.read_bytes())

        dirty = self.repo / "dirty.txt"
        dirty.write_text("dirty", encoding="utf-8")
        dirty_result = run_cli(
            *base, "--code-root", self.repo,
            "--relative-path", "models/adapter.py", check=False,
        )
        self.assertEqual(2, dirty_result.returncode)
        dirty.unlink()

        self._git("remote", "remove", "origin")
        no_origin = run_cli(
            *base, "--code-root", self.repo,
            "--relative-path", "models/adapter.py", check=False,
        )
        self.assertEqual(2, no_origin.returncode)
        self._git("remote", "add", "origin", "https://example.invalid/formal-code.git")

        bound = cli_json(
            *base, "--code-root", self.repo,
            "--relative-path", "models/adapter.py",
        )
        original = asset_path.read_bytes()
        implementation = self.repo / "models" / "adapter.py"
        implementation.write_text("def apply(value):\n    return ('changed', value)\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "conflicting implementation")
        conflict = run_cli(
            *base, "--code-root", self.repo,
            "--relative-path", "models/adapter.py", check=False,
        )
        self.assertEqual(2, conflict.returncode)
        self.assertEqual(original, asset_path.read_bytes())
        self.assertEqual(bound, read_json(asset_path))

    def test_formal_sync_rejects_clean_ignored_file_absent_from_head(self) -> None:
        (self.repo / ".gitignore").write_text(
            "models/ignored.py\n", encoding="utf-8",
        )
        self._git("add", ".gitignore")
        self._git("commit", "-m", "ignore local implementation")
        self.commit = self._git("rev-parse", "HEAD")
        ignored = self.repo / "models" / "ignored.py"
        ignored.write_text("def apply(value):\n    return value\n", encoding="utf-8")
        self.assertEqual("", self._git("status", "--porcelain", "--untracked-files=all"))
        manifest = self._manifest()
        manifest["attachment_points"][0]["target_path"] = "models/ignored.py"  # type: ignore[index]
        self._write_manifest(manifest)
        _template, trial = self._create_formal_trial()
        asset_path = (
            self.project / ".experiment-workflow" / "code-assets"
            / trial["code_asset_id"] / "asset.json"
        )
        before = asset_path.read_bytes()

        result = run_cli(
            "sync-code-asset", "--project", self.project,
            "--asset", trial["code_asset_id"], "--code-root", self.repo,
            "--relative-path", "models/ignored.py", check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertEqual(before, asset_path.read_bytes())
        self.assertIsNone(read_json(asset_path)["external_code_ref"])

    def test_formal_sync_rejects_directory_and_link_target(self) -> None:
        _template, trial = self._create_formal_trial()
        base = (
            "sync-code-asset", "--project", self.project,
            "--asset", trial["code_asset_id"], "--code-root", self.repo,
            "--relative-path", "models/adapter.py",
        )
        target = self.repo / "models" / "adapter.py"
        target.unlink()
        target.mkdir()
        (target / "nested.py").write_text("VALUE = 1\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", "directory target")
        self.assertEqual(2, run_cli(*base, check=False).returncode)

        (target / "nested.py").unlink()
        target.rmdir()
        outside = self.root / "outside.py"
        outside.write_text("VALUE = 2\n", encoding="utf-8")
        try:
            target.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"当前环境不支持文件符号链接：{error}")
        self._git("add", "-A")
        self._git("commit", "-m", "linked target")
        self.assertEqual(2, run_cli(*base, check=False).returncode)

    def test_validate_rejects_external_reference_target_drift_offline(self) -> None:
        _template, trial = self._create_formal_trial()
        cli_json(
            "sync-code-asset", "--project", self.project,
            "--asset", trial["code_asset_id"], "--code-root", self.repo,
            "--relative-path", "models/adapter.py",
        )
        asset_path = (
            self.project / ".experiment-workflow" / "code-assets"
            / trial["code_asset_id"] / "asset.json"
        )
        asset = read_json(asset_path)
        asset["external_code_ref"]["relative_path"] = "models/wrong.py"
        asset_path.write_text(
            json.dumps(asset, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        cli_json(
            "sync-code-asset", "--project", self.project,
            "--asset", trial["code_asset_id"],
        )

        result = run_cli("validate", "--project", self.project, check=False)
        self.assertEqual(2, result.returncode)

    def test_validate_rejects_formal_composition_and_asset_drift(self) -> None:
        _template, trial = self._create_formal_trial()
        asset_dir = (
            self.project / ".experiment-workflow" / "code-assets"
            / trial["code_asset_id"]
        )
        composition_path = asset_dir / "composition.json"
        asset_path = asset_dir / "asset.json"
        composition_bytes = composition_path.read_bytes()
        asset_bytes = asset_path.read_bytes()

        composition = read_json(composition_path)
        composition["target_path"] = "models/wrong.py"
        composition_path.write_text(
            json.dumps(composition, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            2,
            run_cli("validate", "--project", self.project, check=False).returncode,
        )
        composition_path.write_bytes(composition_bytes)

        asset = read_json(asset_path)
        asset["template_id"] = "TPL-9999"
        asset_path.write_text(
            json.dumps(asset, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            2,
            run_cli("validate", "--project", self.project, check=False).returncode,
        )
        asset_path.write_bytes(asset_bytes)
        cli_json("validate", "--project", self.project)

    def test_validate_rejects_integer_formal_identity_fields(self) -> None:
        _template, trial = self._create_formal_trial()
        cli_json(
            "sync-code-asset", "--project", self.project,
            "--asset", trial["code_asset_id"], "--code-root", self.repo,
            "--relative-path", "models/adapter.py",
        )
        control = self.project / ".experiment-workflow"
        transaction_id = int("1" * 32)
        trial_path = control / "trials" / trial["id"] / "trial.json"
        trial_payload = read_json(trial_path)
        trial_payload["transaction_id"] = transaction_id
        trial_path.write_text(
            json.dumps(trial_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        asset_dir = control / "code-assets" / trial["code_asset_id"]
        asset_path = asset_dir / "asset.json"
        asset = read_json(asset_path)
        asset["transaction_id"] = transaction_id
        asset["external_code_ref"]["commit"] = int("2" * 40)
        asset_path.write_text(
            json.dumps(asset, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        sys.path.insert(0, str(CLI.parent))
        self.addCleanup(lambda: sys.path.remove(str(CLI.parent)))
        rendering = importlib.import_module("workflow_core.rendering")
        (asset_dir / "framework.html").write_bytes(rendering.render_code_asset(asset_dir))

        result = run_cli("validate", "--project", self.project, check=False)
        self.assertEqual(2, result.returncode)

    def test_composition_rejects_integer_base_commit(self) -> None:
        _template, trial = self._create_formal_trial()
        composition = read_json(
            self.project / ".experiment-workflow" / "code-assets"
            / trial["code_asset_id"] / "composition.json"
        )
        composition["base_commit"] = int("3" * 40)
        sys.path.insert(0, str(CLI.parent))
        self.addCleanup(lambda: sys.path.remove(str(CLI.parent)))
        templates_module = importlib.import_module("workflow_core.templates")

        with self.assertRaises(ValueError):
            templates_module._validate_composition(
                composition, asset_id=trial["code_asset_id"],
            )

    def test_register_creates_only_one_canonical_json_without_payload(self) -> None:
        result = self._register_json()

        templates = self.project / ".experiment-workflow" / "templates"
        self.assertEqual(["TPL-0001.json"], sorted(path.name for path in templates.iterdir()))
        self.assertFalse((templates / "TPL-0001").exists())
        self.assertFalse(any(path.name == "payload" for path in templates.rglob("*")))
        saved = read_json(templates / "TPL-0001.json")
        self.assertEqual(saved, result)
        self.assertEqual(PROJECT_TEMPLATE_SCHEMA, saved["schema"])
        self.assertEqual("TPL-0001", saved["template_id"])
        self.assertEqual(self.commit.lower(), saved["code_source"]["commit"])
        self.assertEqual(self.commit.lower(), saved["verification"]["git_head"])
        self.assertEqual(2, saved["verification"]["listed_file_count"])
        self.assertIs(saved["verification"]["verified"], True)

    def test_manifest_requires_exact_top_level_and_nested_fields(self) -> None:
        cases: list[dict[str, object]] = []
        missing = self._manifest()
        missing.pop("framework")
        cases.append(missing)
        extra = self._manifest()
        extra["payload"] = "forbidden"
        cases.append(extra)
        nested = self._manifest()
        nested["code_source"] = {**nested["code_source"], "branch": "main"}  # type: ignore[arg-type]
        cases.append(nested)
        interface = self._manifest()
        interface["interfaces"] = {**interface["interfaces"], "optimizer": "adam"}  # type: ignore[arg-type]
        cases.append(interface)
        for index, manifest in enumerate(cases):
            with self.subTest(index=index):
                self._write_manifest(manifest)
                result = self._register(check=False)
                self.assertEqual(2, result.returncode)
                self.assertEqual([], list((self.project / ".experiment-workflow" / "templates").iterdir()))

    def test_manifest_rejects_non_string_bad_or_noncanonical_commit(self) -> None:
        for commit in (123, None, "a" * 39, "g" * 40):
            with self.subTest(commit=commit):
                manifest = self._manifest()
                manifest["code_source"]["commit"] = commit  # type: ignore[index]
                self._write_manifest(manifest)
                result = self._register(check=False)
                self.assertEqual(2, result.returncode)

    def test_listed_files_require_one_to_sixty_four_unique_exact_items(self) -> None:
        empty = self._manifest()
        empty["listed_files"] = []
        self._write_manifest(empty)
        self.assertEqual(2, self._register(check=False).returncode)

        duplicate = self._manifest()
        duplicate["listed_files"] = [duplicate["listed_files"][0]] * 2  # type: ignore[index]
        self._write_manifest(duplicate)
        self.assertEqual(2, self._register(check=False).returncode)

        oversized = self._manifest()
        oversized["listed_files"] = [
            {"path": f"file-{index}.py", "role": "test", "digest": f"sha256:{index:064x}"}
            for index in range(65)
        ]
        self._write_manifest(oversized)
        self.assertEqual(2, self._register(check=False).returncode)

        extra = self._manifest()
        extra["listed_files"][0]["size"] = 1  # type: ignore[index]
        self._write_manifest(extra)
        self.assertEqual(2, self._register(check=False).returncode)

    def test_register_rejects_dirty_or_wrong_git_head(self) -> None:
        (self.repo / "dirty.txt").write_text("dirty", encoding="utf-8")
        dirty = self._register(check=False)
        self.assertEqual(2, dirty.returncode)
        self.assertIn("干净", dirty.stderr)
        (self.repo / "dirty.txt").unlink()

        manifest = self._manifest()
        manifest["code_source"]["commit"] = "0" * 40  # type: ignore[index]
        self._write_manifest(manifest)
        wrong = self._register(check=False)
        self.assertEqual(2, wrong.returncode)
        self.assertIn("HEAD", wrong.stderr)

    def test_register_rejects_repository_subdirectory_as_code_root(self) -> None:
        manifest = self._manifest()
        manifest["code_source"]["template_path"] = "adapter"  # type: ignore[index]
        self._write_manifest(manifest)

        result = run_cli(
            "register-template",
            "--project", self.project,
            "--manifest", self.manifest_path,
            "--code-root", self.repo / "templates",
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertEqual(
            [], list((self.project / ".experiment-workflow" / "templates").iterdir()),
        )

    def test_register_rejects_ignored_listed_file_absent_from_commit(self) -> None:
        ignored = self.repo / "templates" / "adapter" / "ignored.py"
        (self.repo / ".gitignore").write_text(
            "templates/adapter/ignored.py\n", encoding="utf-8",
        )
        self._git("add", ".gitignore")
        self._git("commit", "-m", "ignore local template file")
        self.commit = self._git("rev-parse", "HEAD")
        ignored.write_text("LOCAL_ONLY = True\n", encoding="utf-8")
        manifest = self._manifest()
        manifest["listed_files"] = [
            {
                "path": "ignored.py",
                "role": "ignored local file",
                "digest": self._digest("ignored.py"),
            }
        ]
        self._write_manifest(manifest)

        result = self._register(check=False)

        self.assertEqual(2, result.returncode)
        self.assertEqual(
            [], list((self.project / ".experiment-workflow" / "templates").iterdir()),
        )

    def test_register_accepts_clean_autocrlf_checkout_using_blob_digest(self) -> None:
        module = self.repo / "templates" / "adapter" / "module.py"
        blob_content = b"class Adapter:\n    pass\n"
        module.write_bytes(blob_content)
        self._git("add", "templates/adapter/module.py")
        self._git("commit", "-m", "store LF template blob")

        checkout = self.root / "autocrlf-checkout"
        result = subprocess.run(
            [
                "git", "-c", "core.autocrlf=true", "clone", "--quiet",
                str(self.repo), str(checkout),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.repo = checkout
        self._git("config", "core.autocrlf", "true")
        self.commit = self._git("rev-parse", "HEAD")
        checked_out = self.repo / "templates" / "adapter" / "module.py"
        self.assertEqual(blob_content.replace(b"\n", b"\r\n"), checked_out.read_bytes())
        self.assertEqual("", self._git("status", "--porcelain", "--untracked-files=all"))

        manifest = self._manifest()
        manifest["listed_files"] = [{
            "path": "module.py",
            "role": "模型入口",
            "digest": f"sha256:{hashlib.sha256(blob_content).hexdigest()}",
        }]
        self._write_manifest(manifest)

        saved = self._register_json()

        self.assertEqual("TPL-0001", saved["template_id"])

    def test_register_rejects_unsafe_paths_types_links_and_digest_drift(self) -> None:
        cases: list[dict[str, object]] = []
        traversal = self._manifest()
        traversal["listed_files"][0]["path"] = "../module.py"  # type: ignore[index]
        cases.append(traversal)
        directory = self._manifest()
        directory["listed_files"][0]["path"] = "."  # type: ignore[index]
        cases.append(directory)
        drift = self._manifest()
        drift["listed_files"][0]["digest"] = "sha256:" + "0" * 64  # type: ignore[index]
        cases.append(drift)
        bad_template = self._manifest()
        bad_template["code_source"]["template_path"] = "templates/adapter/module.py"  # type: ignore[index]
        cases.append(bad_template)
        for index, manifest in enumerate(cases):
            with self.subTest(index=index):
                self._write_manifest(manifest)
                self.assertEqual(2, self._register(check=False).returncode)

    def test_register_rejects_linked_code_root_template_path_and_listed_file(self) -> None:
        links = self.root / "links"
        links.mkdir()
        code_root_link = links / "repo"
        template_link = self.repo / "linked-template"
        file_link = self.repo / "templates" / "adapter" / "linked.py"
        try:
            os.symlink(self.repo, code_root_link, target_is_directory=True)
            os.symlink(self.repo / "templates" / "adapter", template_link, target_is_directory=True)
            os.symlink(self.repo / "templates" / "adapter" / "module.py", file_link)
        except (OSError, NotImplementedError):
            self.skipTest("当前平台不允许创建符号链接")

        result = run_cli(
            "register-template", "--project", self.project,
            "--manifest", self.manifest_path, "--code-root", code_root_link,
            check=False,
        )
        self.assertEqual(2, result.returncode)

        manifest = self._manifest()
        manifest["code_source"]["template_path"] = "linked-template"  # type: ignore[index]
        self._write_manifest(manifest)
        self.assertEqual(2, self._register(check=False).returncode)

        self._git("add", "templates/adapter/linked.py")
        self._git("commit", "-m", "add symlink")
        self.commit = self._git("rev-parse", "HEAD")
        manifest = self._manifest()
        manifest["listed_files"] = [
            {"path": "linked.py", "role": "link", "digest": self._digest("module.py")}
        ]
        self._write_manifest(manifest)
        self.assertEqual(2, self._register(check=False).returncode)

    def test_provenance_license_and_mode_gates(self) -> None:
        source = self._manifest()["provenance_sources"][0]  # type: ignore[index]
        for mode in ("copied", "adapted"):
            with self.subTest(mode=mode):
                manifest = self._manifest()
                item = dict(source)
                item.update({
                    "use_mode": mode,
                    "commit": self.commit,
                    "files": ["module.py"],
                    "symbols": ["Adapter"],
                    "license_spdx": "GPL-3.0-only",
                })
                manifest["provenance_sources"] = [item]
                self._write_manifest(manifest)
                self.assertEqual(2, self._register(check=False).returncode)

        valid = self._manifest()
        item = dict(source)
        item.update({
            "use_mode": "adapted", "commit": self.commit.upper(),
            "files": ["module.py"], "symbols": ["Adapter"], "license_spdx": "MIT",
        })
        valid["provenance_sources"] = [item]
        self._write_manifest(valid)
        saved = self._register_json()
        self.assertEqual(self.commit, saved["provenance_sources"][0]["commit"])

    def test_concurrent_registration_allocates_distinct_ids_without_overwrite(self) -> None:
        command = [
            sys.executable, "-B", "-X", "utf8", str(CLI), "register-template",
            "--project", str(self.project), "--manifest", str(self.manifest_path),
            "--code-root", str(self.repo),
        ]
        processes = [
            subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            for _ in range(2)
        ]
        outputs = [process.communicate(timeout=15) for process in processes]
        self.assertEqual([0, 0], [process.returncode for process in processes], outputs)
        ids = {json.loads(stdout)["template_id"] for stdout, _stderr in outputs}
        self.assertEqual({"TPL-0001", "TPL-0002"}, ids)
        self.assertEqual(
            ["TPL-0001.json", "TPL-0002.json"],
            sorted(path.name for path in (self.project / ".experiment-workflow" / "templates").iterdir()),
        )

    def test_registration_does_not_rewrite_current_lock(self) -> None:
        control = self.project / ".experiment-workflow"
        lock_path = control / "workflow.lock.json"
        before = lock_path.read_bytes()

        saved = self._register_json()

        self.assertEqual("TPL-0001", saved["template_id"])
        self.assertEqual(before, lock_path.read_bytes())
        self.assertEqual(0, run_cli("validate", "--project", self.project).returncode)

    def test_v100_lock_requires_templates(self) -> None:
        control = self.project / ".experiment-workflow"
        templates = control / "templates"
        templates.rmdir()
        failed = run_cli("validate", "--project", self.project, check=False)
        self.assertEqual(2, failed.returncode)
        self.assertIn("templates", failed.stderr)

    def test_unlisted_large_file_is_not_read_or_copied(self) -> None:
        large = self.repo / "templates" / "adapter" / "unlisted-large.bin"
        large.write_bytes(b"x" * (5 * 1024 * 1024))
        self._git("add", ".")
        self._git("commit", "-m", "large unlisted fixture")
        self.commit = self._git("rev-parse", "HEAD")
        self._write_manifest(self._manifest())

        self._register_json()

        templates = self.project / ".experiment-workflow" / "templates"
        self.assertEqual(["TPL-0001.json"], [path.name for path in templates.iterdir()])
        self.assertNotIn("unlisted-large", (templates / "TPL-0001.json").read_text(encoding="utf-8"))

    def test_render_is_deterministic_escaped_offline_and_refuses_overwrite(self) -> None:
        self._register_json()
        first = self.root / "views" / "one.html"
        second = self.root / "views" / "two.html"
        first.parent.mkdir()
        cli_json("render-template", "--project", self.project, "--template", "TPL-0001", "--output", first)
        cli_json("render-template", "--project", self.project, "--template", "TPL-0001", "--output", second)

        self.assertEqual(first.read_bytes(), second.read_bytes())
        html = first.read_text(encoding="utf-8")
        self.assertIn("&lt;TPL&gt;", html)
        self.assertIn("&amp;", html)
        self.assertNotIn("<TPL>", html)
        self.assertNotIn("http://", html.lower())
        self.assertNotIn("https://", html.lower())
        self.assertNotIn("<script", html.lower())
        before = first.read_bytes()
        again = run_cli(
            "render-template", "--project", self.project,
            "--template", "TPL-0001", "--output", first, check=False,
        )
        self.assertEqual(2, again.returncode)
        self.assertEqual(before, first.read_bytes())

    def test_render_success_leaves_only_target_html_in_output_directory(self) -> None:
        self._register_json()
        output_directory = self.root / "views"
        output_directory.mkdir()
        output = output_directory / "framework.html"

        cli_json(
            "render-template", "--project", self.project,
            "--template", "TPL-0001", "--output", output,
        )

        self.assertEqual(["framework.html"], sorted(
            path.name for path in output_directory.iterdir()
        ))

    def test_render_rejects_control_plane_output(self) -> None:
        self._register_json()
        output = self.project / ".experiment-workflow" / "framework.html"
        result = run_cli(
            "render-template", "--project", self.project,
            "--template", "TPL-0001", "--output", output, check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertFalse(output.exists())

    def test_validate_counts_templates_and_rejects_drift_or_unknown_entries(self) -> None:
        self._register_json()
        validation = cli_json("validate", "--project", self.project)
        self.assertEqual(1, validation["templates"])
        template = self.project / ".experiment-workflow" / "templates" / "TPL-0001.json"
        original = template.read_bytes()
        payload = read_json(template)
        payload["verification"]["listed_file_count"] = 99
        template.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        self.assertEqual(2, run_cli("validate", "--project", self.project, check=False).returncode)

        template.write_bytes(original)
        unknown = template.parent / "README.md"
        unknown.write_text("forbidden", encoding="utf-8")
        self.assertEqual(2, run_cli("validate", "--project", self.project, check=False).returncode)

    def test_validate_rejects_boolean_listed_file_count(self) -> None:
        manifest = self._manifest()
        manifest["listed_files"] = manifest["listed_files"][:1]  # type: ignore[index]
        self._write_manifest(manifest)
        self._register_json()
        template = self.project / ".experiment-workflow" / "templates" / "TPL-0001.json"
        payload = read_json(template)
        payload["verification"]["listed_file_count"] = True
        template.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        self.assertEqual(2, run_cli("validate", "--project", self.project, check=False).returncode)

    def test_validate_rejects_float_listed_file_count(self) -> None:
        manifest = self._manifest()
        manifest["listed_files"] = manifest["listed_files"][:1]  # type: ignore[index]
        self._write_manifest(manifest)
        self._register_json()
        template = self.project / ".experiment-workflow" / "templates" / "TPL-0001.json"
        payload = read_json(template)
        payload["verification"]["listed_file_count"] = 1.0
        template.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        self.assertEqual(2, run_cli("validate", "--project", self.project, check=False).returncode)


if __name__ == "__main__":
    unittest.main()
