from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._helpers import ROOT, SCRIPTS, cli_json, read_json, run_cli


SOURCE_SCHEMA = "cv-experiment-workflow.source.v2"
IDEA_SCHEMA = "cv-experiment-workflow.catalog-idea.v1"
LEGACY_IDEA_SCHEMA = "cv-experiment-workflow.idea.v2"
TEMPLATE_MANIFEST_SCHEMA = "cv-experiment-workflow.template-manifest.v2"
TEMPLATE_SCHEMA = "cv-experiment-workflow.template.v2"
MODULE_SCHEMA = "cv-experiment-workflow.module.v2"
FIXTURE_ADAPTER = (
    ROOT / "tests" / "fixtures" / "fake_cv_project" / "workflow_adapter.py"
)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class V2CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        run_cli(
            "init", "--path", self.project, "--name", "catalog", "--layout", "v2",
        )
        self.control = self.project / ".experiment-workflow"
        self.code_root = self.project
        (self.code_root / "template").mkdir()
        (self.code_root / "template" / "model.py").write_text(
            "VALUE = 1\n", encoding="utf-8",
        )
        (self.code_root / "template" / "configs").mkdir()
        (self.code_root / "template" / "configs" / "baseline.json").write_text(
            '{"seed": 7}\n', encoding="utf-8",
        )
        (self.code_root / "template" / "unlisted.py").write_text(
            "VALUE = 2\n", encoding="utf-8",
        )
        (self.code_root / "modules").mkdir()
        (self.code_root / "modules" / "consistency_gate.py").write_text(
            "def fuse(global_logits, local_logits):\n"
            "    return global_logits\n",
            encoding="utf-8",
        )
        (self.code_root / "tests").mkdir()
        (self.code_root / "tests" / "test_consistency_gate.py").write_text(
            "def test_placeholder():\n    assert True\n", encoding="utf-8",
        )
        (self.code_root / "workflow_adapter.py").write_bytes(
            FIXTURE_ADAPTER.read_bytes()
        )
        self._git("init")
        self._git("config", "user.name", "Test User")
        self._git("config", "user.email", "test@example.com")
        self._git("add", ".")
        self._git("commit", "-m", "fixture")
        self.commit = self._git("rev-parse", "HEAD")
        commit_object = subprocess.run(
            ["git", "-C", str(self.code_root), "cat-file", "commit", self.commit],
            capture_output=True,
            check=True,
        ).stdout
        self.commit_digest = (
            "sha256:" + hashlib.sha256(commit_object).hexdigest()
        )
        blob = subprocess.run(
            [
                "git", "-C", str(self.code_root), "show",
                f"{self.commit}:template/model.py",
            ],
            capture_output=True,
            check=True,
        ).stdout
        self.model_digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        config_blob = subprocess.run(
            [
                "git", "-C", str(self.code_root), "show",
                f"{self.commit}:template/configs/baseline.json",
            ],
            capture_output=True,
            check=True,
        ).stdout
        self.config_digest = "sha256:" + hashlib.sha256(config_blob).hexdigest()
        module_blob = subprocess.run(
            [
                "git", "-C", str(self.code_root), "show",
                f"{self.commit}:modules/consistency_gate.py",
            ],
            capture_output=True,
            check=True,
        ).stdout
        self.module_digest = "sha256:" + hashlib.sha256(module_blob).hexdigest()
        test_blob = subprocess.run(
            [
                "git", "-C", str(self.code_root), "show",
                f"{self.commit}:tests/test_consistency_gate.py",
            ],
            capture_output=True,
            check=True,
        ).stdout
        self.module_test_digest = (
            "sha256:" + hashlib.sha256(test_blob).hexdigest()
        )

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.code_root), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def _manifest(self, name: str, payload: dict[str, object]) -> Path:
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _source_manifest(
        self,
        name: str,
        *,
        kind: str = "code",
        commit: str | None = None,
        digest: str | None = None,
    ) -> Path:
        if commit is None and kind == "code":
            commit = self.commit
        if kind == "paper":
            source_path = self.root / f"{name}.pdf"
            source_path.write_bytes(f"paper:{name}".encode("utf-8"))
            locator = str(source_path.resolve())
            default_digest = (
                "sha256:" + hashlib.sha256(source_path.read_bytes()).hexdigest()
            )
        else:
            locator = str(self.project.resolve())
            default_digest = self.commit_digest
        return self._manifest(
            name,
            {
                "schema": SOURCE_SCHEMA,
                "kind": kind,
                "identity": name,
                "locator": locator,
                "revision": "v1",
                "commit": commit,
                "digest": digest or default_digest,
                "license": "NOASSERTION",
            },
        )

    def _snapshot_manifest(
        self,
        name: str,
        source_root: Path,
        paths: list[str],
        *,
        digest: str | None = None,
        file_digests: dict[str, str] | None = None,
    ) -> Path:
        files = []
        for relative in paths:
            content = (source_root / Path(relative)).read_bytes()
            files.append({
                "path": relative,
                "digest": (
                    file_digests or {}
                ).get(
                    relative,
                    "sha256:" + hashlib.sha256(content).hexdigest(),
                ),
            })
        normalized_files = sorted(files, key=lambda item: item["path"])
        canonical = json.dumps(
            normalized_files,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return self._manifest(
            name,
            {
                "schema": SOURCE_SCHEMA,
                "kind": "code_snapshot",
                "identity": name,
                "locator": str(source_root.resolve()),
                "revision": "local-snapshot-v1",
                "commit": None,
                "digest": digest or (
                    "sha256:" + hashlib.sha256(canonical).hexdigest()
                ),
                "license": "NOASSERTION",
                "files": files,
            },
        )

    def _register_source(self, name: str, **changes: object) -> dict[str, object]:
        kind = str(changes.get("kind", "code"))
        commit = changes.get("commit", self.commit if kind == "code" else None)
        digest = changes.get("digest")
        manifest = read_json(
            self._source_manifest(
                name,
                kind=kind,
                commit=commit if isinstance(commit, str) else None,
                digest=digest if isinstance(digest, str) else None,
            )
        )
        manifest.update(changes)
        path = self._manifest(f"{name}-source", manifest)
        source_path = (
            self.project
            if manifest["kind"] == "code"
            else Path(str(manifest["locator"]))
        )
        return cli_json(
            "register-source",
            "--project",
            self.project,
            "--manifest",
            path,
            "--source-path",
            source_path,
        )

    def _idea_manifest(
        self, source_refs: list[str], *, status: str = "ready",
    ) -> Path:
        return self._manifest(
            "idea",
            {
                "schema": IDEA_SCHEMA,
                "status": status,
                "problem": "全局分支不确定时，局部分支的可信度没有被利用。",
                "mechanism": "使用全局不确定度、局部可靠性和两分支一致性控制融合。",
                "falsifiable_hypothesis": "相同预算下，H 不低于基线且局部分支失真时权重下降。",
                "source_refs": source_refs,
                "evidence_refs": [],
                "source_links": [
                    {
                        "source_ref": source_ref,
                        "locator": f"{source_ref}:mechanism",
                        "supports_field": "mechanism",
                        "claim": "该来源为候选机制或实现位置提供可核查依据。",
                    }
                    for source_ref in source_refs
                ],
            },
        )

    def _template_manifest(self, source_refs: list[str]) -> Path:
        return self._manifest(
            "template-v2",
            {
                "schema": TEMPLATE_MANIFEST_SCHEMA,
                "name": "CUB GZSL 最小基线",
                "ownership": "project",
                "source_refs": source_refs,
                "code": {
                    "source_ref": source_refs[0],
                    "template_path": "template",
                    "listed_files": [
                    {
                        "path": "model.py",
                        "role": "baseline",
                        "digest": self.model_digest,
                    },
                    {
                        "path": "configs/baseline.json",
                        "role": "config",
                        "digest": self.config_digest,
                    },
                    ],
                },
                "interfaces": {
                    "data": "CUB split",
                    "model": "attribute projection",
                    "training": "seen only",
                    "evaluation": "macro S/U/H",
                    "config": "JSON",
                    "seed": "integer",
                    "metrics": "S U H",
                    "checkpoint": "state dict",
                },
                "attachment_points": [
                    {
                        "name": "fusion.gate",
                        "contract": "global_logits, local_logits -> logits",
                        "target_path": "model.py",
                        "disabled_behavior": "返回 global_logits",
                    },
                ],
                "baseline_recipe": {
                    "entry": "model.py",
                    "config": "configs/baseline.json",
                    "seed": 7,
                },
            },
        )

    def _module_manifest(
        self,
        source_refs: list[str],
        idea_refs: list[str],
        *,
        template_ref: str = "TPL-0001",
        attachment: str = "fusion.gate",
    ) -> Path:
        return self._manifest(
            "module",
            {
                "schema": MODULE_SCHEMA,
                "status": "ready",
                "kind": "research",
                "intent": "一致性门控局部融合",
                "idea_refs": idea_refs,
                "source_refs": source_refs,
                "attachment": {
                    "template_ref": template_ref,
                    "point": attachment,
                },
                "entry": "modules/consistency_gate.py",
                "contract": {
                    "input": "global_logits, local_logits",
                    "output": "fused_logits",
                },
                "toggle": {
                    "config_key": "model.consistency_gate.enabled",
                    "enabled_value": True,
                    "disabled_value": False,
                },
                "disabled_behavior": "只返回 global_logits",
                "code_ref": {
                    "commit": self.commit,
                    "path": "modules/consistency_gate.py",
                },
                "tests": ["tests/test_consistency_gate.py"],
            },
        )

    def _install_sources_idea_template(
        self,
    ) -> tuple[list[dict[str, object]], dict[str, object], dict[str, object]]:
        sources = [
            self._register_source("dvsr"),
            self._register_source("genzsl"),
            self._register_source("lazsl"),
            self._register_source("svip"),
        ]
        source_ids = [str(item["id"]) for item in sources]
        idea = cli_json(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._idea_manifest(source_ids),
        )
        template = cli_json(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._template_manifest(source_ids),
            "--code-root",
            self.code_root,
        )
        return sources, idea, template

    def _install_innovation_catalog(
        self,
    ) -> tuple[
        list[dict[str, object]],
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        sources, idea, template = self._install_sources_idea_template()
        source_ids = [str(item["id"]) for item in sources]
        module = cli_json(
            "register-module",
            "--project",
            self.project,
            "--manifest",
            self._module_manifest(source_ids, [str(idea["id"])]),
            "--code-root",
            self.code_root,
        )
        return sources, idea, template, module

    def _bind_fixture_adapter(self) -> None:
        adapter_path = self.control / "adapter.json"
        adapter = read_json(adapter_path)
        adapter.update(
            {
                "status": "bound",
                "code_sources": [
                    {
                        "repo_url": "fixture:fake-cv-project",
                        "commit": self.commit,
                        "relative_path": "workflow_adapter.py",
                    }
                ],
                "capabilities": {"workflow_adapter": True},
            }
        )
        adapter_path.write_text(
            json.dumps(adapter, ensure_ascii=False), encoding="utf-8"
        )

    def _start_innovation(
        self,
        idea: dict[str, object],
        template: dict[str, object],
        module: dict[str, object],
        *extra_targets: str,
    ) -> dict[str, object]:
        arguments: list[object] = [
            "start-task",
            "--project",
            self.project,
            "--request",
            "验证一致性门控",
            "--route",
            "innovation",
            "--target-ref",
            idea["id"],
            "--target-ref",
            template["id"],
            "--target-ref",
            module["id"],
        ]
        for target in extra_targets:
            arguments.extend(["--target-ref", target])
        arguments.extend(
            [
                "--config",
                '{"learning_rate":0.001}',
                "--primary-metric",
                "score",
                "--baseline",
                "0.70",
                "--budget",
                '{"max_runs":1}',
                "--stop-condition",
                '{"max_failures":1}',
                "--debug-required",
                "false",
            ]
        )
        return cli_json(*arguments)

    def test_v2_rejects_v1_project_template_schema(self) -> None:
        payload = {
            "schema": "cv-experiment-workflow.project-template.v1",
            "template_id": "TPL-0001",
            "name": "旧版但结构完整的模板",
            "description": "这个 payload 在 v1 中完全有效，不能混入 v2。",
            "code_source": {
                "repo_url": "local:test",
                "commit": self.commit,
                "template_path": "template",
            },
            "listed_files": [
                {
                    "path": "model.py",
                    "role": "baseline",
                    "digest": self.model_digest,
                },
            ],
            "interfaces": {
                "data": "batch",
                "model": "model",
                "training": "train",
                "evaluation": "evaluate",
                "config": "mapping",
                "seed": "integer",
                "metrics": "mapping",
                "checkpoint": "path",
            },
            "attachment_points": [
                {
                    "name": "fusion.gate",
                    "contract": "tensor -> tensor",
                    "target_path": "model.py",
                    "disabled_behavior": "identity",
                },
            ],
            "provenance_sources": [
                {
                    "label": "本地测试",
                    "locator": "fixture:model.py",
                    "identity": "测试身份",
                    "commit": None,
                    "license_spdx": "MIT",
                    "files": [],
                    "symbols": [],
                    "use_mode": "reimplementation",
                    "note": "仅用于 schema 隔离回归",
                },
            ],
            "framework": {
                "nodes": [
                    {"id": "model", "label": "模型", "kind": "model"},
                ],
                "edges": [],
            },
            "verification": {
                "git_head": self.commit,
                "listed_file_count": 1,
                "verified": True,
            },
        }
        (self.control / "templates" / "TPL-0001.json").write_text(
            json.dumps(payload), encoding="utf-8",
        )

        result = run_cli("validate", "--project", self.project, check=False)

        self.assertEqual(2, result.returncode)
        self.assertIn("v2", result.stderr)

    def test_registers_four_sources_one_idea_one_template_one_module(self) -> None:
        sources, idea, template = self._install_sources_idea_template()
        module = cli_json(
            "register-module",
            "--project",
            self.project,
            "--manifest",
            self._module_manifest(
                [str(item["id"]) for item in sources],
                [str(idea["id"])],
            ),
            "--code-root",
            self.code_root,
        )

        self.assertEqual(
            ["SRC-0001", "SRC-0002", "SRC-0003", "SRC-0004"],
            [item["id"] for item in sources],
        )
        self.assertEqual("IDEA-0001", idea["id"])
        self.assertEqual("TPL-0001", template["id"])
        self.assertEqual(TEMPLATE_SCHEMA, template["schema"])
        self.assertEqual("MOD-0001", module["id"])
        self.assertEqual(
            {
                "schema": MODULE_SCHEMA,
                "id": "MOD-0001",
                "status": "ready",
                "kind": "research",
                "intent": "一致性门控局部融合",
                "idea_refs": ["IDEA-0001"],
                "source_refs": [
                    "SRC-0001", "SRC-0002", "SRC-0003", "SRC-0004",
                ],
                "attachment": {
                    "template_ref": "TPL-0001",
                    "point": "fusion.gate",
                },
                "entry": "modules/consistency_gate.py",
                "contract": {
                    "input": "global_logits, local_logits",
                    "output": "fused_logits",
                },
                "toggle": {
                    "config_key": "model.consistency_gate.enabled",
                    "enabled_value": True,
                    "disabled_value": False,
                },
                "disabled_behavior": "只返回 global_logits",
                "code_ref": {
                    "commit": self.commit,
                    "path": "modules/consistency_gate.py",
                },
                "tests": ["tests/test_consistency_gate.py"],
                "validation": {
                    "verified": True,
                    "git_head": self.commit,
                    "files": [
                        {
                            "path": "modules/consistency_gate.py",
                            "digest": self.module_digest,
                        },
                        {
                            "path": "tests/test_consistency_gate.py",
                            "digest": self.module_test_digest,
                        },
                    ],
                },
            },
            read_json(self.control / "modules" / "MOD-0001.json"),
        )
        self.assertEqual(
            {"sources": 4, "ideas": 1, "templates": 1, "modules": 1},
            cli_json("validate", "--project", self.project),
        )

    def test_catalog_idea_revision_creates_new_record_and_preserves_parent(
        self,
    ) -> None:
        source = self._register_source("idea-revision-source")
        first = cli_json(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._idea_manifest([str(source["id"])]),
        )
        first_path = self.control / "ideas" / "IDEA-0001.json"
        first_bytes = first_path.read_bytes()
        revision_manifest = read_json(
            self._idea_manifest([str(source["id"])])
        )
        revision_manifest["falsifiable_hypothesis"] = (
            "相同预算和三次不同 seed 下，H 至少提高 0.5。"
        )
        revision_path = self._manifest("idea-revision", revision_manifest)

        revised = cli_json(
            "revise-catalog-idea",
            "--project",
            self.project,
            "--idea",
            str(first["id"]),
            "--manifest",
            revision_path,
            "--reason",
            "把模糊的“不低于”改成可量化门槛",
        )

        self.assertEqual(first_bytes, first_path.read_bytes())
        self.assertEqual("IDEA-0002", revised["id"])
        self.assertEqual("IDEA-0001", revised["parent_idea_ref"])
        self.assertEqual(2, revised["revision"])
        self.assertEqual(
            "把模糊的“不低于”改成可量化门槛",
            revised["revision_reason"],
        )
        self.assertEqual(
            revised,
            read_json(self.control / "ideas" / "IDEA-0002.json"),
        )

    def test_new_catalog_idea_forces_root_revision_and_checks_evidence(self) -> None:
        source = self._register_source("idea-evidence-source")
        manifest = read_json(self._idea_manifest([str(source["id"])]))
        manifest["revision"] = 99
        bad_revision = run_cli(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._manifest("bad-root-revision", manifest),
            check=False,
        )
        self.assertEqual(2, bad_revision.returncode)
        self.assertIn("字段", bad_revision.stderr)

        manifest.pop("revision")
        manifest["evidence_refs"] = ["RUN-9999"]
        missing_evidence = run_cli(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._manifest("missing-evidence", manifest),
            check=False,
        )
        self.assertEqual(2, missing_evidence.returncode)
        self.assertIn("evidence_refs", missing_evidence.stderr)
        self.assertEqual([], list((self.control / "ideas").iterdir()))

        manifest["evidence_refs"] = [str(source["id"])]
        saved = cli_json(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._manifest("source-evidence", manifest),
        )
        self.assertEqual(1, saved["revision"])
        self.assertIsNone(saved["parent_idea_ref"])
        self.assertIsNone(saved["revision_reason"])

    def test_catalog_idea_revision_accepts_only_closed_run_evidence(self) -> None:
        sources, idea, template, module = self._install_innovation_catalog()
        self._bind_fixture_adapter()
        task = self._start_innovation(idea, template, module)
        outcome = cli_json(
            "run-task",
            "--project",
            self.project,
            "--task",
            task["id"],
            "--purpose",
            "debug",
            "--backend",
            "project",
        )
        self.assertEqual("closed", outcome["run"]["execution"]["stage"])
        manifest = read_json(
            self._idea_manifest([str(item["id"]) for item in sources])
        )
        manifest["evidence_refs"] = [str(outcome["run"]["id"])]

        revised = cli_json(
            "revise-catalog-idea",
            "--project",
            self.project,
            "--idea",
            str(idea["id"]),
            "--manifest",
            self._manifest("closed-run-evidence", manifest),
            "--reason",
            "加入已关闭调试运行作为机制证据",
        )

        self.assertEqual([outcome["run"]["id"]], revised["evidence_refs"])

        run_path = (
            self.control / "runs" / str(outcome["run"]["id"]) / "run.json"
        )
        unclosed = read_json(run_path)
        unclosed["execution"]["stage"] = "finished"
        unclosed["execution"]["closed_at"] = None
        run_path.write_text(
            json.dumps(unclosed, ensure_ascii=False),
            encoding="utf-8",
        )
        import sys

        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(sys.path.remove, str(SCRIPTS))
        from workflow_core.v2_catalog import _validate_idea_evidence_refs

        with self.assertRaisesRegex(ValueError, "只能引用已关闭 Run"):
            _validate_idea_evidence_refs(
                [str(outcome["run"]["id"])],
                sources={str(item["id"]): item for item in sources},
                control=self.control,
            )

    def test_catalog_idea_requires_complete_valid_source_links(self) -> None:
        code = self._register_source("linked-code")
        paper = self._register_source("linked-paper", kind="paper")
        manifest = read_json(
            self._idea_manifest([str(code["id"]), str(paper["id"])])
        )
        manifest["source_links"] = [
            {
                "source_ref": str(code["id"]),
                "locator": "models/gate.py::ConsistencyGate.forward",
                "supports_field": "mechanism",
                "claim": "该函数展示一致性门控的计算位置。",
            },
            {
                "source_ref": str(paper["id"]),
                "locator": "第 5 页，3.2 节，公式 4",
                "supports_field": "falsifiable_hypothesis",
                "claim": "该公式给出可比较的门控量定义。",
            },
        ]
        saved = cli_json(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._manifest("complete-source-links", manifest),
        )
        self.assertEqual(manifest["source_links"], saved["source_links"])

        invalid_cases: list[tuple[str, dict[str, object]]] = []
        partial = json.loads(json.dumps(manifest))
        partial["source_links"] = partial["source_links"][:1]
        invalid_cases.append(("partial", partial))

        outside = json.loads(json.dumps(manifest))
        outside["source_refs"] = [str(code["id"])]
        invalid_cases.append(("outside", outside))

        bad_field = json.loads(json.dumps(manifest))
        bad_field["source_links"][0]["supports_field"] = "conclusion"
        invalid_cases.append(("bad-field", bad_field))

        duplicate = json.loads(json.dumps(manifest))
        duplicate["source_links"].append(
            json.loads(json.dumps(duplicate["source_links"][0]))
        )
        invalid_cases.append(("duplicate", duplicate))

        for label, candidate in invalid_cases:
            with self.subTest(label=label):
                before = list((self.control / "ideas").iterdir())
                failed = run_cli(
                    "save-idea",
                    "--project",
                    self.project,
                    "--manifest",
                    self._manifest(f"invalid-links-{label}", candidate),
                    check=False,
                )
                self.assertEqual(2, failed.returncode)
                self.assertEqual(
                    before,
                    list((self.control / "ideas").iterdir()),
                )

    def test_catalog_idea_rejects_fake_revision_and_non_evidence_refs(self) -> None:
        source = self._register_source("revision-integrity-source")
        first_manifest = self._idea_manifest([str(source["id"])])
        first = cli_json(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            first_manifest,
        )
        before = _tree_bytes(self.control / "ideas")

        same = run_cli(
            "revise-catalog-idea",
            "--project",
            self.project,
            "--idea",
            str(first["id"]),
            "--manifest",
            first_manifest,
            "--reason",
            "只有理由文字变化，科学内容完全没变",
            check=False,
        )
        self.assertEqual(2, same.returncode)
        self.assertIn("没有实际变化", same.stderr)
        self.assertEqual(before, _tree_bytes(self.control / "ideas"))

        manifest = read_json(first_manifest)
        for bad_ref in ("EVT-0001", "TASK-0001"):
            with self.subTest(ref=bad_ref):
                manifest["evidence_refs"] = [bad_ref]
                failed = run_cli(
                    "save-idea",
                    "--project",
                    self.project,
                    "--manifest",
                    self._manifest(f"bad-evidence-{bad_ref}", manifest),
                    check=False,
                )
                self.assertEqual(2, failed.returncode)
                self.assertIn("Source 或 Run", failed.stderr)

    def test_legacy_idea_can_create_new_format_revision_without_rewrite(
        self,
    ) -> None:
        source = self._register_source("legacy-revision-source")
        legacy = {
            "schema": LEGACY_IDEA_SCHEMA,
            "id": "IDEA-0001",
            "status": "ready",
            "problem": "旧问题",
            "mechanism": "旧机制",
            "falsifiable_hypothesis": "旧假设",
            "source_refs": [str(source["id"])],
            "revision": 1,
            "evidence_refs": [],
        }
        legacy_path = self.control / "ideas" / "IDEA-0001.json"
        legacy_path.write_text(
            json.dumps(legacy, ensure_ascii=False),
            encoding="utf-8",
        )
        old_bytes = legacy_path.read_bytes()
        manifest = read_json(self._idea_manifest([str(source["id"])]))
        manifest["problem"] = "新格式明确后的问题"

        revised = cli_json(
            "revise-catalog-idea",
            "--project",
            self.project,
            "--idea",
            "IDEA-0001",
            "--manifest",
            self._manifest("legacy-to-new-revision", manifest),
            "--reason",
            "以后续新格式继续记录，不改写历史对象",
        )

        self.assertEqual(old_bytes, legacy_path.read_bytes())
        self.assertEqual(LEGACY_IDEA_SCHEMA, legacy["schema"])
        self.assertEqual(IDEA_SCHEMA, revised["schema"])
        self.assertEqual("IDEA-0001", revised["parent_idea_ref"])
        self.assertEqual(2, revised["revision"])
        self.assertEqual(
            {"sources": 1, "ideas": 2, "templates": 0, "modules": 0},
            cli_json("validate", "--project", self.project),
        )

    def test_legacy_catalog_idea_is_read_only_compatible(self) -> None:
        source = self._register_source("legacy-idea-source")
        legacy = {
            "schema": LEGACY_IDEA_SCHEMA,
            "id": "IDEA-0001",
            "status": "ready",
            "problem": "旧问题",
            "mechanism": "旧机制",
            "falsifiable_hypothesis": "旧的可证伪假设",
            "source_refs": [str(source["id"])],
            "revision": 1,
            "evidence_refs": [],
        }
        legacy_path = self.control / "ideas" / "IDEA-0001.json"
        legacy_path.write_text(
            json.dumps(legacy, ensure_ascii=False),
            encoding="utf-8",
        )
        before = legacy_path.read_bytes()

        result = cli_json("validate", "--project", self.project)

        self.assertEqual(1, result["ideas"])
        self.assertEqual(before, legacy_path.read_bytes())

    def test_cross_reference_and_kind_failures_are_atomic(self) -> None:
        code = self._register_source("code")
        before_ideas = list((self.control / "ideas").iterdir())
        bad_idea = run_cli(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._idea_manifest(["SRC-9999"]),
            check=False,
        )
        self.assertEqual(2, bad_idea.returncode)
        self.assertEqual(before_ideas, list((self.control / "ideas").iterdir()))

        paper_manifest = self._source_manifest(
            "paper", kind="paper", commit=None,
        )
        paper = cli_json(
            "register-source",
            "--project",
            self.project,
            "--manifest",
            paper_manifest,
            "--source-path",
            self.root / "paper.pdf",
        )
        bad_template = run_cli(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._template_manifest([str(paper["id"])]),
            "--code-root",
            self.code_root,
            check=False,
        )
        self.assertEqual(2, bad_template.returncode)
        self.assertIn("code Source", bad_template.stderr)
        self.assertEqual([], list((self.control / "templates").iterdir()))

        idea = cli_json(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._idea_manifest([str(code["id"])]),
        )
        cli_json(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._template_manifest([str(code["id"])]),
            "--code-root",
            self.code_root,
        )
        before_modules = list((self.control / "modules").iterdir())
        bad_module = run_cli(
            "register-module",
            "--project",
            self.project,
            "--manifest",
            self._module_manifest(
                [str(code["id"])], [str(idea["id"])],
                attachment="unknown.point",
            ),
            "--code-root",
            self.code_root,
            check=False,
        )
        self.assertEqual(2, bad_module.returncode)
        self.assertIn("attachment", bad_module.stderr)
        self.assertEqual(before_modules, list((self.control / "modules").iterdir()))

        missing_template = run_cli(
            "register-module",
            "--project",
            self.project,
            "--manifest",
            self._module_manifest(
                [str(code["id"])],
                [str(idea["id"])],
                template_ref="TPL-9999",
            ),
            "--code-root",
            self.code_root,
            check=False,
        )
        self.assertEqual(2, missing_template.returncode)
        self.assertIn("Template", missing_template.stderr)
        self.assertEqual(before_modules, list((self.control / "modules").iterdir()))

    def test_research_module_requires_a_ready_idea(self) -> None:
        source = self._register_source("code")
        idea = cli_json(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._idea_manifest([str(source["id"])], status="draft"),
        )
        cli_json(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._template_manifest([str(source["id"])]),
            "--code-root",
            self.code_root,
        )

        result = run_cli(
            "register-module",
            "--project",
            self.project,
            "--manifest",
            self._module_manifest([str(source["id"])], [str(idea["id"])]),
            "--code-root",
            self.code_root,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("ready Idea", result.stderr)
        self.assertEqual([], list((self.control / "modules").iterdir()))

    def test_manifest_reader_rejects_link_and_registration_never_overwrites(self) -> None:
        source_manifest = self._source_manifest("source")
        linked = self.root / "linked-source.json"
        try:
            os.symlink(source_manifest, linked)
        except OSError as error:
            self.skipTest(f"当前平台不能安全创建文件符号链接：{error}")

        linked_result = run_cli(
            "register-source",
            "--project",
            self.project,
            "--manifest",
            linked,
            "--source-path",
            self.project,
            check=False,
        )
        self.assertEqual(2, linked_result.returncode)
        self.assertEqual([], list((self.control / "sources").iterdir()))

        first = self._register_source("first")
        self.assertEqual("SRC-0001", first["id"])
        occupied = self.control / "sources" / "SRC-0002.json"
        occupied.write_text("{}", encoding="utf-8")
        invalid_state = run_cli(
            "register-source",
            "--project",
            self.project,
            "--manifest",
            self._source_manifest("second"),
            "--source-path",
            self.project,
            check=False,
        )
        self.assertEqual(2, invalid_state.returncode)
        self.assertEqual("{}", occupied.read_text(encoding="utf-8"))

    def test_strict_fields_commit_and_safe_paths(self) -> None:
        bad_commit = run_cli(
            "register-source",
            "--project",
            self.project,
            "--manifest",
            self._source_manifest("bad", commit="abc"),
            "--source-path",
            self.project,
            check=False,
        )
        self.assertEqual(2, bad_commit.returncode)

        paper = cli_json(
            "register-source",
            "--project",
            self.project,
            "--manifest",
            self._source_manifest("paper", kind="paper", commit=None),
            "--source-path",
            self.root / "paper.pdf",
        )
        self.assertIsNone(paper["commit"])

        extra = read_json(self._source_manifest("extra"))
        extra["unexpected"] = True
        extra_result = run_cli(
            "register-source",
            "--project",
            self.project,
            "--manifest",
            self._manifest("extra-fields", extra),
            "--source-path",
            self.project,
            check=False,
        )
        self.assertEqual(2, extra_result.returncode)

        code = self._register_source("good")
        template = read_json(self._template_manifest([str(code["id"])]))
        template["code"]["template_path"] = "../outside"  # type: ignore[index]
        unsafe = run_cli(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._manifest("unsafe-template", template),
            "--code-root",
            self.code_root,
            check=False,
        )
        self.assertEqual(2, unsafe.returncode)

    def test_v2_template_rejects_external_clean_repository(self) -> None:
        source = self._register_source("project-code")
        external = self.root / "external"
        subprocess.run(
            ["git", "clone", str(self.project), str(external)],
            check=True,
            capture_output=True,
        )

        result = run_cli(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._template_manifest([str(source["id"])]),
            "--code-root",
            external,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("project", result.stderr)
        self.assertEqual([], list((self.control / "templates").iterdir()))

    def test_v2_template_rechecks_head_after_verification(self) -> None:
        source = self._register_source("project-code")
        manifest = self._template_manifest([str(source["id"])])
        import sys

        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(sys.path.remove, str(SCRIPTS))
        import workflow_core.project_templates as templates

        def switch_to_another_clean_commit(*_args: object) -> None:
            self._git("commit", "--allow-empty", "-m", "head moved")

        with mock.patch.object(
            templates,
            "_verify_listed_files",
            side_effect=switch_to_another_clean_commit,
        ):
            with self.assertRaisesRegex(ValueError, "HEAD"):
                templates.register_template(
                    self.project, manifest, self.code_root,
                )

        self.assertEqual([], list((self.control / "templates").iterdir()))

    def test_v2_relative_paths_are_bounded_before_normalization(self) -> None:
        source = self._register_source("project-code")
        template = read_json(self._template_manifest([str(source["id"])]))
        template["code"]["template_path"] = "x" * 4097  # type: ignore[index]

        bad_template = run_cli(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._manifest("too-long-template-path", template),
            "--code-root",
            self.code_root,
            check=False,
        )

        self.assertEqual(2, bad_template.returncode)
        self.assertIn("4096", bad_template.stderr)
        self.assertEqual([], list((self.control / "templates").iterdir()))

        idea = cli_json(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._idea_manifest([str(source["id"])]),
        )
        cli_json(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._template_manifest([str(source["id"])]),
            "--code-root",
            self.code_root,
        )
        module = read_json(
            self._module_manifest([str(source["id"])], [str(idea["id"])])
        )
        module["code_ref"]["path"] = "x" * 4097  # type: ignore[index]

        bad_module = run_cli(
            "register-module",
            "--project",
            self.project,
            "--manifest",
            self._manifest("too-long-module-path", module),
            "--code-root",
            self.code_root,
            check=False,
        )

        self.assertEqual(2, bad_module.returncode)
        self.assertIn("4096", bad_module.stderr)
        self.assertEqual([], list((self.control / "modules").iterdir()))

    def test_module_requires_structured_contract_toggle_and_matching_code_ref(
        self,
    ) -> None:
        source = self._register_source("project-code")
        idea = cli_json(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._idea_manifest([str(source["id"])]),
        )
        cli_json(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._template_manifest([str(source["id"])]),
            "--code-root",
            self.code_root,
        )
        base = read_json(
            self._module_manifest([str(source["id"])], [str(idea["id"])])
        )
        mutations = (
            ("contract", "input -> output"),
            ("toggle", {"config_key": "x", "enabled_value": 1, "disabled_value": 0}),
            (
                "code_ref",
                {"commit": "f" * 40, "path": "modules/consistency_gate.py"},
            ),
        )
        for index, (field, value) in enumerate(mutations):
            with self.subTest(field=field):
                candidate = json.loads(json.dumps(base))
                candidate[field] = value
                result = run_cli(
                    "register-module",
                    "--project",
                    self.project,
                    "--manifest",
                    self._manifest(f"bad-module-{index}", candidate),
                    "--code-root",
                    self.code_root,
                    check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertEqual(
                    [], list((self.control / "modules").iterdir()),
                )
        saved = cli_json(
            "register-module",
            "--project",
            self.project,
            "--manifest",
            self._manifest("valid-structured-module", base),
            "--code-root",
            self.code_root,
        )
        self.assertEqual(base["contract"], saved["contract"])
        self.assertEqual(base["toggle"], saved["toggle"])
        self.assertEqual(base["code_ref"], saved["code_ref"])

    def test_register_source_verifies_local_paper_and_code_identity(self) -> None:
        code = self._register_source("project-code")
        paper = self._register_source("paper", kind="paper", commit=None)

        self.assertEqual(self.commit, code["commit"])
        self.assertEqual(self.commit_digest, code["digest"])
        self.assertEqual(str(self.project.resolve()), code["locator"])
        paper_path = self.root / "paper.pdf"
        self.assertEqual(str(paper_path.resolve()), paper["locator"])
        self.assertEqual(
            "sha256:" + hashlib.sha256(paper_path.read_bytes()).hexdigest(),
            paper["digest"],
        )

        bad = read_json(self._source_manifest("bad-digest"))
        bad["digest"] = f"sha256:{'0' * 64}"
        result = run_cli(
            "register-source",
            "--project",
            self.project,
            "--manifest",
            self._manifest("bad-source-digest", bad),
            "--source-path",
            self.project,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertFalse((self.control / "sources" / "SRC-0003.json").exists())

    def test_code_snapshot_registers_and_full_validate_rechecks_files(self) -> None:
        snapshot_root = self.root / "reference-code"
        (snapshot_root / "models").mkdir(parents=True)
        (snapshot_root / "main.py").write_text(
            "print('reference')\n", encoding="utf-8",
        )
        (snapshot_root / "models" / "gate.py").write_text(
            "VALUE = 1\n", encoding="utf-8",
        )
        manifest = self._snapshot_manifest(
            "reference-snapshot",
            snapshot_root,
            ["models/gate.py", "main.py"],
        )

        saved = cli_json(
            "register-source",
            "--project",
            self.project,
            "--manifest",
            manifest,
            "--source-path",
            snapshot_root,
        )

        self.assertEqual("code_snapshot", saved["kind"])
        self.assertIsNone(saved["commit"])
        self.assertEqual(
            ["main.py", "models/gate.py"],
            [item["path"] for item in saved["files"]],
        )
        self.assertEqual(
            {"sources": 1, "ideas": 0, "templates": 0, "modules": 0},
            cli_json("validate", "--project", self.project),
        )

        (snapshot_root / "main.py").write_text(
            "print('tampered')\n", encoding="utf-8",
        )
        changed = run_cli(
            "validate", "--project", self.project, check=False,
        )
        self.assertEqual(2, changed.returncode)
        self.assertIn("digest", changed.stderr)

    def test_code_snapshot_rejects_unsafe_duplicate_and_mismatched_files(
        self,
    ) -> None:
        snapshot_root = self.root / "reference-code"
        snapshot_root.mkdir()
        (snapshot_root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
        outside = self.root / "outside.py"
        outside.write_text("OUTSIDE = True\n", encoding="utf-8")

        base = read_json(self._snapshot_manifest(
            "snapshot-base", snapshot_root, ["main.py"],
        ))
        candidates: list[tuple[str, dict[str, object]]] = []

        traversal = json.loads(json.dumps(base))
        traversal["files"][0]["path"] = "../outside.py"
        candidates.append(("traversal", traversal))

        duplicate = json.loads(json.dumps(base))
        duplicate["files"].append(dict(duplicate["files"][0]))
        candidates.append(("duplicate", duplicate))

        wrong_file_hash = json.loads(json.dumps(base))
        wrong_file_hash["files"][0]["digest"] = "sha256:" + "0" * 64
        candidates.append(("file-hash", wrong_file_hash))

        wrong_aggregate = json.loads(json.dumps(base))
        wrong_aggregate["digest"] = "sha256:" + "0" * 64
        candidates.append(("aggregate-hash", wrong_aggregate))

        for name, payload in candidates:
            with self.subTest(name=name):
                result = run_cli(
                    "register-source",
                    "--project",
                    self.project,
                    "--manifest",
                    self._manifest(f"snapshot-{name}", payload),
                    "--source-path",
                    snapshot_root,
                    check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertEqual(
                    [], list((self.control / "sources").iterdir()),
                )

    def test_code_snapshot_rejects_symlinked_file_or_parent(self) -> None:
        snapshot_root = self.root / "reference-code"
        real_parent = self.root / "real-parent"
        snapshot_root.mkdir()
        real_parent.mkdir()
        (real_parent / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
        linked_parent = snapshot_root / "linked"
        linked_file = snapshot_root / "linked.py"
        try:
            os.symlink(real_parent, linked_parent, target_is_directory=True)
            os.symlink(real_parent / "main.py", linked_file)
        except OSError as error:
            self.skipTest(f"当前平台不能安全创建符号链接：{error}")

        for name, relative in (
            ("linked-parent", "linked/main.py"),
            ("linked-file", "linked.py"),
        ):
            with self.subTest(name=name):
                result = run_cli(
                    "register-source",
                    "--project",
                    self.project,
                    "--manifest",
                    self._snapshot_manifest(
                        name, snapshot_root, [relative],
                    ),
                    "--source-path",
                    snapshot_root,
                    check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertIn("链接/reparse", result.stderr)
                self.assertEqual(
                    [], list((self.control / "sources").iterdir()),
                )

    def test_code_snapshot_enforces_file_count_and_size_limits(self) -> None:
        snapshot_root = self.root / "reference-code"
        snapshot_root.mkdir()
        paths = []
        for index in range(65):
            relative = f"file-{index:02d}.py"
            (snapshot_root / relative).write_text(
                f"VALUE = {index}\n", encoding="utf-8",
            )
            paths.append(relative)

        too_many = run_cli(
            "register-source",
            "--project",
            self.project,
            "--manifest",
            self._snapshot_manifest("too-many", snapshot_root, paths),
            "--source-path",
            snapshot_root,
            check=False,
        )
        self.assertEqual(2, too_many.returncode)
        self.assertIn("1..64", too_many.stderr)

        oversized = snapshot_root / "oversized.bin"
        with oversized.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        too_large = run_cli(
            "register-source",
            "--project",
            self.project,
            "--manifest",
            self._snapshot_manifest(
                "too-large", snapshot_root, ["oversized.bin"],
            ),
            "--source-path",
            snapshot_root,
            check=False,
        )
        self.assertEqual(2, too_large.returncode)
        self.assertIn("16777216 bytes", too_large.stderr)
        self.assertEqual([], list((self.control / "sources").iterdir()))

    def test_snapshot_is_inspiration_only_not_template_or_module_code(self) -> None:
        snapshot_root = self.root / "reference-code"
        snapshot_root.mkdir()
        (snapshot_root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
        snapshot = cli_json(
            "register-source",
            "--project",
            self.project,
            "--manifest",
            self._snapshot_manifest("snapshot", snapshot_root, ["main.py"]),
            "--source-path",
            snapshot_root,
        )

        bad_template = run_cli(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._template_manifest([str(snapshot["id"])]),
            "--code-root",
            self.code_root,
            check=False,
        )
        self.assertEqual(2, bad_template.returncode)
        self.assertIn("code Source", bad_template.stderr)

        code = self._register_source("project-code")
        idea = cli_json(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._idea_manifest([str(snapshot["id"])]),
        )
        cli_json(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._template_manifest([str(code["id"])]),
            "--code-root",
            self.code_root,
        )
        bad_module = run_cli(
            "register-module",
            "--project",
            self.project,
            "--manifest",
            self._module_manifest(
                [str(snapshot["id"])], [str(idea["id"])],
            ),
            "--code-root",
            self.code_root,
            check=False,
        )
        self.assertEqual(2, bad_module.returncode)
        self.assertIn("code Source", bad_module.stderr)

    def test_template_and_module_reject_git_symlink_blob_modes(self) -> None:
        template_link = self.code_root / "template" / "link.py"
        module_link = self.code_root / "modules" / "link.py"
        template_link.write_text("model.py", encoding="utf-8")
        module_link.write_text("consistency_gate.py", encoding="utf-8")
        template_blob = self._git("hash-object", "-w", "template/link.py")
        module_blob = self._git("hash-object", "-w", "modules/link.py")
        self._git(
            "update-index", "--add", "--cacheinfo",
            f"120000,{template_blob},template/link.py",
        )
        self._git(
            "update-index", "--add", "--cacheinfo",
            f"120000,{module_blob},modules/link.py",
        )
        self._git("commit", "-m", "add git symlink blobs")
        self.commit = self._git("rev-parse", "HEAD")
        commit_object = subprocess.run(
            ["git", "-C", str(self.code_root), "cat-file", "commit", self.commit],
            capture_output=True,
            check=True,
        ).stdout
        self.commit_digest = (
            "sha256:" + hashlib.sha256(commit_object).hexdigest()
        )
        self.assertEqual("", self._git("status", "--porcelain"))
        source = self._register_source("project-code")

        template = read_json(self._template_manifest([str(source["id"])]))
        link_digest = "sha256:" + hashlib.sha256(b"model.py").hexdigest()
        template["code"]["listed_files"][0] = {
            "path": "link.py",
            "role": "baseline",
            "digest": link_digest,
        }
        template["baseline_recipe"]["entry"] = "link.py"
        template["attachment_points"][0]["target_path"] = "link.py"
        bad_template = run_cli(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._manifest("git-link-template", template),
            "--code-root",
            self.code_root,
            check=False,
        )
        self.assertEqual(2, bad_template.returncode)
        self.assertIn("100644/100755", bad_template.stderr)

        idea = cli_json(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._idea_manifest([str(source["id"])]),
        )
        cli_json(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._template_manifest([str(source["id"])]),
            "--code-root",
            self.code_root,
        )
        module = read_json(self._module_manifest(
            [str(source["id"])], [str(idea["id"])],
        ))
        module["entry"] = "modules/link.py"
        module["code_ref"]["path"] = "modules/link.py"
        bad_module = run_cli(
            "register-module",
            "--project",
            self.project,
            "--manifest",
            self._manifest("git-link-module", module),
            "--code-root",
            self.code_root,
            check=False,
        )
        self.assertEqual(2, bad_module.returncode)
        self.assertIn("100644/100755", bad_module.stderr)

    def test_code_source_rechecks_head_after_digest_verification(self) -> None:
        manifest = self._source_manifest("project-code")
        import sys

        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(sys.path.remove, str(SCRIPTS))
        import workflow_core.v2_catalog as catalog

        real_digest = getattr(catalog, "_code_source_digest", None)

        def digest_then_move_head(root: Path, commit: str) -> str:
            if real_digest is None:
                return self.commit_digest
            digest = real_digest(root, commit)
            self._git("commit", "--allow-empty", "-m", "source head moved")
            return digest

        with mock.patch.object(
            catalog,
            "_code_source_digest",
            side_effect=digest_then_move_head,
            create=True,
        ):
            with self.assertRaisesRegex(ValueError, "HEAD"):
                catalog.register_source(
                    self.project, manifest, self.project,
                )

        self.assertEqual([], list((self.control / "sources").iterdir()))

    def test_template_requires_every_executable_path_in_listed_files(
        self,
    ) -> None:
        source = self._register_source("project-code")
        base = read_json(self._template_manifest([str(source["id"])]))
        candidates = []
        missing_config = json.loads(json.dumps(base))
        missing_config["code"]["listed_files"] = [  # type: ignore[index]
            missing_config["code"]["listed_files"][0]  # type: ignore[index]
        ]
        candidates.append(missing_config)
        missing_attachment = json.loads(json.dumps(base))
        missing_attachment["attachment_points"][0]["target_path"] = (  # type: ignore[index]
            "unlisted.py"
        )
        candidates.append(missing_attachment)

        for index, candidate in enumerate(candidates):
            with self.subTest(index=index):
                result = run_cli(
                    "register-template",
                    "--project",
                    self.project,
                    "--manifest",
                    self._manifest(f"unlisted-executable-{index}", candidate),
                    "--code-root",
                    self.code_root,
                    check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertIn("listed_files", result.stderr)
                self.assertEqual(
                    [], list((self.control / "templates").iterdir()),
                )

    def test_ready_module_verifies_git_files_and_records_validation(self) -> None:
        source = self._register_source("project-code")
        idea = cli_json(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._idea_manifest([str(source["id"])]),
        )
        cli_json(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._template_manifest([str(source["id"])]),
            "--code-root",
            self.code_root,
        )

        saved = cli_json(
            "register-module",
            "--project",
            self.project,
            "--manifest",
            self._module_manifest([str(source["id"])], [str(idea["id"])]),
            "--code-root",
            self.code_root,
        )

        self.assertEqual(
            {
                "verified": True,
                "git_head": self.commit,
                "files": [
                    {
                        "path": "modules/consistency_gate.py",
                        "digest": self.module_digest,
                    },
                    {
                        "path": "tests/test_consistency_gate.py",
                        "digest": self.module_test_digest,
                    },
                ],
            },
            saved["validation"],
        )
        self.assertEqual(
            saved,
            read_json(self.control / "modules" / "MOD-0001.json"),
        )

    def test_ready_module_rechecks_head_after_file_verification(self) -> None:
        source = self._register_source("project-code")
        idea = cli_json(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._idea_manifest([str(source["id"])]),
        )
        cli_json(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._template_manifest([str(source["id"])]),
            "--code-root",
            self.code_root,
        )
        manifest = self._module_manifest(
            [str(source["id"])], [str(idea["id"])],
        )
        import sys

        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(sys.path.remove, str(SCRIPTS))
        import workflow_core.v2_catalog as catalog

        real_digest = getattr(catalog, "_module_file_digest", None)
        calls = 0

        def digest_then_move_head(
            root: Path, commit: str, relative: str,
        ) -> str:
            nonlocal calls
            calls += 1
            if real_digest is None:
                digest = self.module_digest
            else:
                digest = real_digest(root, commit, relative)
            if calls == 1:
                self._git("commit", "--allow-empty", "-m", "module head moved")
            return digest

        with mock.patch.object(
            catalog,
            "_module_file_digest",
            side_effect=digest_then_move_head,
            create=True,
        ):
            with self.assertRaisesRegex(ValueError, "HEAD"):
                catalog.register_module(
                    self.project, manifest, self.code_root,
                )

        self.assertEqual([], list((self.control / "modules").iterdir()))

    def test_validate_and_workflow_status_report_catalog_counts(self) -> None:
        source = self._register_source("project-code")
        idea = cli_json(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._idea_manifest([str(source["id"])]),
        )
        cli_json(
            "register-template",
            "--project",
            self.project,
            "--manifest",
            self._template_manifest([str(source["id"])]),
            "--code-root",
            self.code_root,
        )
        module = cli_json(
            "register-module",
            "--project",
            self.project,
            "--manifest",
            self._module_manifest([str(source["id"])], [str(idea["id"])]),
            "--code-root",
            self.code_root,
        )

        expected_counts = {
            "sources": 1,
            "ideas": 1,
            "templates": 1,
            "modules": 1,
        }
        self.assertEqual(
            expected_counts,
            cli_json("validate", "--project", self.project),
        )
        status = cli_json("workflow-status", "--project", self.project)
        self.assertEqual(expected_counts, status["catalog_counts"])
        self.assertEqual([idea["id"]], status["ready_idea_ids"])
        self.assertEqual([module["id"]], status["ready_module_ids"])

    def test_innovation_task_requires_matching_ready_catalog_targets(self) -> None:
        sources, idea, template, module = self._install_innovation_catalog()
        task = self._start_innovation(idea, template, module)
        self.assertEqual("innovation", task["route"])
        self.assertTrue(task["route_inputs"]["changes_code_behavior"])
        self.assertEqual(0.70, task["route_inputs"]["baseline"])

        second_idea = cli_json(
            "save-idea",
            "--project",
            self.project,
            "--manifest",
            self._idea_manifest([str(item["id"]) for item in sources]),
        )
        mismatch = run_cli(
            "start-task",
            "--project",
            self.project,
            "--request",
            "错配创新",
            "--route",
            "innovation",
            "--target-ref",
            second_idea["id"],
            "--target-ref",
            template["id"],
            "--target-ref",
            module["id"],
            "--baseline",
            "0.7",
            "--budget",
            '{"max_runs":1}',
            "--stop-condition",
            '{"max_failures":1}',
            check=False,
        )
        self.assertNotEqual(0, mismatch.returncode)
        self.assertIn("Module", mismatch.stderr)

        dangling = run_cli(
            "start-task",
            "--project",
            self.project,
            "--request",
            "悬空创新",
            "--route",
            "innovation",
            "--target-ref",
            "IDEA-9999",
            "--target-ref",
            template["id"],
            "--target-ref",
            module["id"],
            "--baseline",
            "0.7",
            "--budget",
            '{"max_runs":1}',
            "--stop-condition",
            '{"max_failures":1}',
            check=False,
        )
        self.assertNotEqual(0, dangling.returncode)
        self.assertIn("不存在", dangling.stderr)

        source_target = run_cli(
            "start-task",
            "--project",
            self.project,
            "--request",
            "来源冒充目标",
            "--route",
            "innovation",
            "--target-ref",
            idea["id"],
            "--target-ref",
            template["id"],
            "--target-ref",
            module["id"],
            "--target-ref",
            sources[0]["id"],
            "--baseline",
            "0.7",
            "--budget",
            '{"max_runs":1}',
            "--stop-condition",
            '{"max_failures":1}',
            check=False,
        )
        self.assertNotEqual(0, source_target.returncode)
        self.assertIn("target_refs", source_target.stderr)

        dangling_task = run_cli(
            "start-task",
            "--project",
            self.project,
            "--request",
            "悬空前置任务",
            "--route",
            "innovation",
            "--target-ref",
            idea["id"],
            "--target-ref",
            template["id"],
            "--target-ref",
            module["id"],
            "--target-ref",
            "TASK-9999",
            "--baseline",
            "0.7",
            "--budget",
            '{"max_runs":1}',
            "--stop-condition",
            '{"max_failures":1}',
            check=False,
        )
        self.assertNotEqual(0, dangling_task.returncode)
        self.assertIn("前置 Task 不存在", dangling_task.stderr)

    def test_full_validate_rejects_dangling_or_mismatched_innovation_refs(
        self,
    ) -> None:
        sources, idea, template, module = self._install_innovation_catalog()
        task = self._start_innovation(idea, template, module)
        task_path = self.control / "tasks" / f"{task['id']}.json"
        payload = read_json(task_path)
        payload["target_refs"][0] = "IDEA-9999"
        task_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        result = run_cli("validate", "--project", self.project, check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("不存在", result.stderr)

    def test_innovation_debug_runs_shared_task_run_evidence_loop(self) -> None:
        _sources, idea, template, module = self._install_innovation_catalog()
        self._bind_fixture_adapter()
        task = self._start_innovation(idea, template, module)
        outcome = cli_json(
            "run-task",
            "--project",
            self.project,
            "--task",
            task["id"],
            "--purpose",
            "debug",
            "--backend",
            "project",
        )
        self.assertEqual("preparing", outcome["task"]["stage"])
        self.assertEqual("closed", outcome["run"]["execution"]["stage"])
        self.assertEqual("succeeded", outcome["run"]["execution"]["outcome"])
        self.assertEqual("debug", outcome["evidence_level"])
        self.assertEqual("debug_complete", outcome["evidence"]["reason"])
        self.assertEqual("score", outcome["comparison"]["primary_metric"])
        self.assertEqual(0.70, outcome["comparison"]["baseline"])
        self.assertEqual(0.70, outcome["comparison"]["candidate"])
        self.assertEqual(
            "side_by_side_only",
            outcome["comparison"]["comparison_scope"],
        )
        self.assertIsNone(outcome["comparison"]["delta"])
        self.assertTrue(outcome["comparison"]["blockers"])
        self.assertEqual(
            [
                "Coordinator",
                "Researcher",
                "Implementer",
                "Reviewer",
                "Runner",
                "Analyst",
            ],
            [item["role"] for item in outcome["session"]],
        )
        continued = cli_json(
            "interpret-request", "--project", self.project, "--request", "继续"
        )
        self.assertFalse(
            any("不支持" in item for item in continued["missing"])
        )

    def test_cli_rejects_missing_or_nonfinite_innovation_baseline(self) -> None:
        _sources, idea, template, module = self._install_innovation_catalog()
        for baseline in (None, "nan", "inf"):
            with self.subTest(baseline=baseline):
                arguments: list[object] = [
                    "start-task",
                    "--project",
                    self.project,
                    "--request",
                    "创新",
                    "--route",
                    "innovation",
                    "--target-ref",
                    idea["id"],
                    "--target-ref",
                    template["id"],
                    "--target-ref",
                    module["id"],
                    "--budget",
                    '{"max_runs":1}',
                    "--stop-condition",
                    '{"max_failures":1}',
                ]
                if baseline is not None:
                    arguments.extend(["--baseline", baseline])
                result = run_cli(*arguments, check=False)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("baseline", result.stderr)


if __name__ == "__main__":
    unittest.main()
