from __future__ import annotations

import concurrent.futures
import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from tests._helpers import SCRIPTS, cli_json, read_json, run_cli


class IdeaVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project = Path(self.temp_dir.name) / "项目"
        run_cli("init", "--path", self.project, "--name", "视觉实验")
        self.control = self.project / ".experiment-workflow"

    def assert_utc_time(self, value: str) -> None:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(0, parsed.utcoffset().total_seconds())

    def make_directory_link(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"当前环境不支持目录符号链接：{error}")

    def make_file_link(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target)
        except OSError as error:
            self.skipTest(f"当前环境不支持文件符号链接：{error}")

    def snapshot_tree(self, root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def import_workflow_modules(self):
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        return (
            importlib.import_module("workflow_core.io"),
            importlib.import_module("workflow_core.records"),
        )

    def test_workflow_lock_validation_requires_current_version(self) -> None:
        _workflow_io, records = self.import_workflow_modules()
        lock_path = self.control / "workflow.lock.json"
        lock = read_json(lock_path)
        lock["version"] = "9.9.9"
        lock_path.write_text(
            json.dumps(lock, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "workflow lock 元数据无效"):
            records.new_idea(self.project, "只接受当前版本", "不得写入")
        self.assertEqual([], list((self.control / "ideas").glob("*.json")))

    def test_version_schema_dispatch_rejects_non_string_ids_and_commits(self) -> None:
        _workflow_io, records = self.import_workflow_modules()
        timestamp = "2026-07-15T00:00:00Z"
        external_ref = {
            "repo_url": "https://example.invalid/source.git",
            "commit": "a" * 40,
            "relative_path": "models/adapter.py",
            "digest": "sha256:" + "b" * 64,
        }
        valid_v2 = {
            "schema": "cv-experiment-workflow.version.v2",
            "id": "VER-0002",
            "status": "active",
            "name": "activated",
            "base_version_id": "VER-0001",
            "template_id": "TPL-0001",
            "accepted_attempt_ids": ["ATTEMPT-0001"],
            "accepted_code_asset_ids": ["CODE-0001"],
            "accepted_code_refs": [
                {"code_asset_id": "CODE-0001", "external_code_ref": external_ref}
            ],
            "code_sources": [
                {
                    "repo_url": "https://example.invalid/integrated.git",
                    "commit": "c" * 40,
                    "relative_path": "src",
                }
            ],
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        records.validate_version(valid_v2, expected_id="VER-0002")

        malformed_v2 = []
        integer_commit = json.loads(json.dumps(valid_v2))
        integer_commit["code_sources"][0]["commit"] = int("1" * 40)
        malformed_v2.append(integer_commit)
        boolean_attempt = json.loads(json.dumps(valid_v2))
        boolean_attempt["accepted_attempt_ids"] = [True]
        malformed_v2.append(boolean_attempt)
        object_attempt = json.loads(json.dumps(valid_v2))
        object_attempt["accepted_attempt_ids"] = [{"id": "ATTEMPT-0001"}]
        malformed_v2.append(object_attempt)
        for payload in malformed_v2:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    records.validate_version(payload, expected_id="VER-0002")

        legacy = {
            "schema": "cv-experiment-workflow.version.v1",
            "id": "VER-0001",
            "status": "active",
            "name": "legacy",
            "code_sources": [
                {
                    "repo_url": "https://example.invalid/legacy.git",
                    "commit": int("2" * 40),
                    "relative_path": ".",
                }
            ],
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        with self.assertRaises(ValueError):
            records.validate_version(legacy, expected_id="VER-0001")

    def test_new_idea_accepts_current_lock_without_rewriting(self) -> None:
        project = Path(self.temp_dir.name) / "当前版本"
        run_cli("init", "--path", project, "--name", "当前版本")
        control = project / ".experiment-workflow"
        lock_path = control / "workflow.lock.json"
        lock_bytes = lock_path.read_bytes()

        idea = cli_json(
            "new-idea", "--project", project,
            "--title", "当前版本", "--note", "真实写命令",
        )

        self.assertEqual("IDEA-0001", idea["id"])
        self.assertEqual(
            idea, read_json(control / "ideas" / "IDEA-0001.json"),
        )
        self.assertEqual(lock_bytes, lock_path.read_bytes())

    def test_new_idea_rejects_malformed_locks_without_writing(self) -> None:
        lock_path = self.control / "workflow.lock.json"
        valid = read_json(lock_path)
        invalid_locks = (
            (
                "错误 schema",
                {**valid, "schema": "cv-experiment-workflow.workflow-lock.v0"},
            ),
            ("错误 skill_id", {**valid, "skill_id": "other-skill"}),
            ("额外字段", {**valid, "unexpected": True}),
            ("非字符串版本", {**valid, "version": 101}),
            ("过旧版本", {**valid, "version": "0.9.0"}),
            ("未知新版", {**valid, "version": "2.0.0"}),
        )
        for label, lock in invalid_locks:
            with self.subTest(label=label):
                lock_path.write_text(
                    json.dumps(lock, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                before = self.snapshot_tree(self.control)

                failed = run_cli(
                    "new-idea", "--project", self.project,
                    "--title", "不得创建", "--note", label, check=False,
                )

                self.assertEqual(2, failed.returncode)
                self.assertEqual("", failed.stdout)
                self.assertIn("workflow lock 元数据无效", failed.stderr)
                self.assertEqual(before, self.snapshot_tree(self.control))
                self.assertEqual([], list((self.control / "ideas").glob("*.json")))

    def test_new_idea_creates_draft_without_version_or_forbidden_fields(self) -> None:
        payload = cli_json(
            "new-idea",
            "--project",
            self.project,
            "--title",
            "多尺度特征融合",
            "--note",
            "先验证小目标",
        )

        self.assertEqual("IDEA-0001", payload["id"])
        self.assertEqual("cv-experiment-workflow.idea.v1", payload["schema"])
        self.assertEqual("draft", payload["status"])
        self.assertEqual("多尺度特征融合", payload["title"])
        self.assertEqual("先验证小目标", payload["note"])
        self.assertEqual([], payload["source_refs"])
        self.assertEqual(payload, read_json(self.control / "ideas" / "IDEA-0001.json"))
        self.assertEqual([], list((self.control / "versions").glob("*.json")))
        forbidden = {"base_version", "trial", "code", "Inbox", "Run", "source_type", "priority"}
        self.assertTrue(forbidden.isdisjoint(payload))
        self.assert_utc_time(payload["created_at"])
        self.assert_utc_time(payload["updated_at"])

    def test_source_reference_keeps_free_text_without_classification(self) -> None:
        payload = cli_json(
            "new-idea",
            "--project",
            self.project,
            "--title",
            "来源测试",
            "--note",
            "自由文本",
            "--source-label",
            "同事随口建议",
            "--source-locator",
            "周会白板左上角",
            "--source-note",
            "无需分类",
        )

        self.assertEqual(
            [{"label": "同事随口建议", "locator": "周会白板左上角", "note": "无需分类"}],
            payload["source_refs"],
        )
        self.assertNotIn("source_type", json.dumps(payload, ensure_ascii=False))

    def test_activate_requires_complete_fields_and_preserves_existing_bytes_on_failure(self) -> None:
        draft = cli_json(
            "new-idea", "--project", self.project, "--title", "门禁", "--note", "草稿"
        )
        path = self.control / "ideas" / f"{draft['id']}.json"
        original = path.read_bytes()

        failed = run_cli(
            "activate-idea",
            "--project",
            self.project,
            "--idea",
            draft["id"],
            "--problem",
            " ",
            "--mechanism",
            "注意力",
            "--hypothesis",
            "提升召回率",
            check=False,
        )

        self.assertEqual(2, failed.returncode)
        self.assertEqual("", failed.stdout)
        self.assertTrue(failed.stderr.strip())
        self.assertEqual(original, path.read_bytes())

    def test_activate_draft_once_and_preserves_source(self) -> None:
        draft = cli_json(
            "new-idea",
            "--project",
            self.project,
            "--title",
            "激活",
            "--note",
            "候选",
            "--source-label",
            "论文",
            "--source-locator",
            "第 3 页",
            "--source-note",
            "图 2",
        )
        activated = cli_json(
            "activate-idea",
            "--project",
            self.project,
            "--idea",
            draft["id"],
            "--problem",
            "遮挡导致漏检",
            "--mechanism",
            "引入上下文聚合",
            "--hypothesis",
            "遮挡目标召回率提升",
        )

        self.assertEqual("active", activated["status"])
        self.assertEqual(draft["created_at"], activated["created_at"])
        self.assertEqual(draft["source_refs"], activated["source_refs"])
        self.assertEqual("遮挡导致漏检", activated["problem"])
        original = (self.control / "ideas" / f"{draft['id']}.json").read_bytes()
        repeated = run_cli(
            "activate-idea",
            "--project",
            self.project,
            "--idea",
            draft["id"],
            "--problem",
            "另一个问题",
            "--mechanism",
            "另一个机制",
            "--hypothesis",
            "另一个假设",
            check=False,
        )
        self.assertEqual(2, repeated.returncode)
        self.assertEqual(original, (self.control / "ideas" / f"{draft['id']}.json").read_bytes())

    def test_register_version_rejects_bad_commit_and_code_paths(self) -> None:
        for extra in (
            ("--commit", "abc"),
            ("--commit", "a" * 39),
            ("--commit", "g" * 40),
            ("--commit", "A" * 40, "--code-path", "../outside"),
            ("--commit", "A" * 40, "--code-path", str(Path(self.temp_dir.name).resolve())),
        ):
            with self.subTest(extra=extra):
                result = run_cli(
                    "register-version",
                    "--project",
                    self.project,
                    "--name",
                    "基线",
                    "--repo-url",
                    "https://example.invalid/repo.git",
                    *extra,
                    check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertTrue(result.stderr.strip())
        self.assertEqual([], list((self.control / "versions").glob("*.json")))

    def test_register_version_creates_one_normalized_code_source(self) -> None:
        payload = cli_json(
            "register-version",
            "--project",
            self.project,
            "--name",
            "检测基线",
            "--repo-url",
            "https://example.invalid/repo.git",
            "--commit",
            "ABCDEF0123456789ABCDEF0123456789ABCDEF01",
        )

        self.assertEqual("VER-0001", payload["id"])
        self.assertEqual("cv-experiment-workflow.version.v1", payload["schema"])
        self.assertEqual("active", payload["status"])
        self.assertEqual("检测基线", payload["name"])
        self.assertEqual(
            [{
                "repo_url": "https://example.invalid/repo.git",
                "commit": "abcdef0123456789abcdef0123456789abcdef01",
                "relative_path": ".",
            }],
            payload["code_sources"],
        )
        self.assertEqual(payload, read_json(self.control / "versions" / "VER-0001.json"))
        self.assert_utc_time(payload["created_at"])
        self.assert_utc_time(payload["updated_at"])

    def test_two_processes_allocate_distinct_idea_ids_without_overwrite(self) -> None:
        def create(title: str) -> dict:
            return cli_json(
                "new-idea", "--project", self.project, "--title", title, "--note", "并发"
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ("并发甲", "并发乙")))

        self.assertEqual({"IDEA-0001", "IDEA-0002"}, {item["id"] for item in results})
        files = sorted((self.control / "ideas").glob("IDEA-*.json"))
        self.assertEqual(["IDEA-0001.json", "IDEA-0002.json"], [path.name for path in files])
        self.assertEqual({"并发甲", "并发乙"}, {read_json(path)["title"] for path in files})

    def test_strict_id_validation_bad_json_and_uninitialized_project_fail_cleanly(self) -> None:
        unknown = self.control / "ideas" / "IDEA-9999-extra.json"
        unknown.write_text("{}", encoding="utf-8")
        rejected = run_cli(
            "new-idea", "--project", self.project,
            "--title", "严格扫描", "--note", "测试", check=False,
        )
        self.assertEqual(2, rejected.returncode)
        self.assertEqual(b"{}", unknown.read_bytes())
        self.assertFalse((self.control / "ideas" / "IDEA-0001.json").exists())
        unknown.unlink()
        created = cli_json(
            "new-idea", "--project", self.project, "--title", "严格扫描", "--note", "测试"
        )
        self.assertEqual("IDEA-0001", created["id"])

        bad_id = run_cli(
            "activate-idea", "--project", self.project, "--idea", "idea-0001",
            "--problem", "p", "--mechanism", "m", "--hypothesis", "h", check=False,
        )
        self.assertEqual(2, bad_id.returncode)
        bad_file = self.control / "ideas" / "IDEA-0002.json"
        bad_file.write_text("{broken", encoding="utf-8")
        bad_json = run_cli(
            "activate-idea", "--project", self.project, "--idea", "IDEA-0002",
            "--problem", "p", "--mechanism", "m", "--hypothesis", "h", check=False,
        )
        self.assertEqual(2, bad_json.returncode)
        missing = run_cli(
            "new-idea", "--project", Path(self.temp_dir.name) / "missing",
            "--title", "x", "--note", "y", check=False,
        )
        self.assertEqual(2, missing.returncode)
        self.assertTrue(all(result.stderr.strip() for result in (bad_id, bad_json, missing)))
        self.assertTrue(all(result.stdout == "" for result in (bad_id, bad_json, missing)))

    def test_new_idea_rejects_linked_ideas_directory_without_external_write(self) -> None:
        ideas = self.control / "ideas"
        ideas.rename(self.control / "ideas-real")
        external = Path(self.temp_dir.name) / "external-ideas"
        external.mkdir()
        marker = external / "owned.txt"
        marker.write_bytes(b"external idea bytes\n")
        self.make_directory_link(ideas, external)
        before = self.snapshot_tree(external)

        result = run_cli(
            "new-idea", "--project", self.project, "--title", "越界", "--note", "拒绝",
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertTrue(result.stderr.strip())
        self.assertEqual(before, self.snapshot_tree(external))

    def test_register_version_rejects_linked_versions_directory_without_external_write(self) -> None:
        versions = self.control / "versions"
        versions.rename(self.control / "versions-real")
        external = Path(self.temp_dir.name) / "external-versions"
        external.mkdir()
        marker = external / "owned.txt"
        marker.write_bytes(b"external version bytes\n")
        self.make_directory_link(versions, external)
        before = self.snapshot_tree(external)

        result = run_cli(
            "register-version", "--project", self.project, "--name", "越界",
            "--repo-url", "https://example.invalid/repo.git", "--commit", "a" * 40,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertTrue(result.stderr.strip())
        self.assertEqual(before, self.snapshot_tree(external))

    def test_new_idea_rejects_matching_link_during_id_scan(self) -> None:
        external = Path(self.temp_dir.name) / "external-scan-idea.json"
        external.write_bytes(b"external idea scan bytes\n")
        linked = self.control / "ideas" / "IDEA-0001.json"
        self.make_file_link(linked, external)
        before = external.read_bytes()

        result = run_cli(
            "new-idea", "--project", self.project, "--title", "扫描", "--note", "拒绝",
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertTrue(result.stderr.strip())
        self.assertEqual(before, external.read_bytes())
        self.assertFalse((self.control / "ideas" / "IDEA-0002.json").exists())

    def test_register_version_rejects_matching_link_during_id_scan(self) -> None:
        external = Path(self.temp_dir.name) / "external-scan-version.json"
        external.write_bytes(b"external version scan bytes\n")
        linked = self.control / "versions" / "VER-0001.json"
        self.make_file_link(linked, external)
        before = external.read_bytes()

        result = run_cli(
            "register-version", "--project", self.project, "--name", "扫描",
            "--repo-url", "https://example.invalid/repo.git", "--commit", "a" * 40,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertTrue(result.stderr.strip())
        self.assertEqual(before, external.read_bytes())
        self.assertFalse((self.control / "versions" / "VER-0002.json").exists())

    def test_new_idea_does_not_overwrite_file_created_during_publish(self) -> None:
        workflow_io, records = self.import_workflow_modules()
        target = self.control / "ideas" / "IDEA-0001.json"
        user_bytes = b"user file won the race\n"
        original_link = os.link

        def inject_file(source, destination, *, follow_symlinks=True):
            destination_path = Path(destination)
            if destination_path == target and not os.path.lexists(target):
                target.write_bytes(user_bytes)
            return original_link(
                source, destination, follow_symlinks=follow_symlinks
            )

        with mock.patch.object(workflow_io.os, "link", side_effect=inject_file):
            with self.assertRaises(RuntimeError) as captured:
                records.new_idea(self.project, "竞争", "拒绝覆盖")

        self.assertEqual(user_bytes, target.read_bytes())
        residuals = list(target.parent.glob(".IDEA-0001.json.*.tmp"))
        self.assertEqual(1, len(residuals))
        self.assertIn("异常路径未自动清理", str(captured.exception))
        self.assertIn(
            str(residuals[0]), str(captured.exception).split("残留路径：", 1)[1],
        )

    def test_register_version_does_not_replace_symlink_created_during_publish(self) -> None:
        workflow_io, records = self.import_workflow_modules()
        target = self.control / "versions" / "VER-0001.json"
        external = Path(self.temp_dir.name) / "publish-race-version.json"
        external_bytes = b"external version race bytes\n"
        external.write_bytes(external_bytes)
        original_link = os.link

        def inject_symlink(source, destination, *, follow_symlinks=True):
            destination_path = Path(destination)
            if destination_path == target and not os.path.lexists(target):
                self.make_file_link(target, external)
            return original_link(
                source, destination, follow_symlinks=follow_symlinks
            )

        with mock.patch.object(workflow_io.os, "link", side_effect=inject_symlink):
            with self.assertRaises(RuntimeError) as captured:
                records.register_version(
                    self.project,
                    "竞争",
                    "https://example.invalid/repo.git",
                    "a" * 40,
                )

        self.assertTrue(target.is_symlink())
        self.assertEqual(external_bytes, external.read_bytes())
        residuals = list(target.parent.glob(".VER-0001.json.*.tmp"))
        self.assertEqual(1, len(residuals))
        self.assertIn("异常路径未自动清理", str(captured.exception))
        self.assertIn(
            str(residuals[0]), str(captured.exception).split("残留路径：", 1)[1],
        )

    def test_atomic_create_json_preserves_swapped_transaction_anchor(self) -> None:
        workflow_io, _records = self.import_workflow_modules()
        target = Path(self.temp_dir.name) / "atomic-create.json"
        transaction_id = "a" * 32
        anchor = workflow_io.transaction_temp_path(target, transaction_id)
        user_bytes = b"user replaced transaction anchor\n"
        original_create = workflow_io.atomic_create_bytes

        def swap_anchor(path, content, *, transaction_id):
            created = original_create(
                path, content, transaction_id=transaction_id
            )
            anchor.unlink()
            anchor.write_bytes(user_bytes)
            return created

        with mock.patch.object(
            workflow_io, "atomic_create_bytes", side_effect=swap_anchor
        ):
            with self.assertRaises(RuntimeError):
                workflow_io.atomic_create_json(
                    target, {"safe": True}, transaction_id=transaction_id
                )

        self.assertEqual(user_bytes, anchor.read_bytes())
        self.assertEqual({"safe": True}, read_json(target))

    def test_activate_rejects_malformed_idea_without_rewriting_bytes(self) -> None:
        malformed_payloads = []
        draft = cli_json(
            "new-idea", "--project", self.project, "--title", "校验", "--note", "草稿"
        )
        malformed_payloads.extend(
            (
                {key: value for key, value in draft.items() if key != "title"},
                {**draft, "note": 123},
                {**draft, "source_refs": {}},
                {**draft, "source_refs": [{"label": "x", "locator": "y"}]},
                {**draft, "source_refs": [{"label": "x", "locator": "y", "note": "z", "source_type": "paper"}]},
                {**draft, "created_at": "not-a-time"},
                {**draft, "updated_at": "2026-07-13T08:00:00+08:00"},
                {**draft, "id": "IDEA-9999"},
                {**draft, "status": "unknown"},
                {**draft, "priority": 1},
                {**draft, "base_version": "VER-0001"},
            )
        )
        path = self.control / "ideas" / f"{draft['id']}.json"
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                path.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
                before = path.read_bytes()
                result = run_cli(
                    "activate-idea", "--project", self.project, "--idea", draft["id"],
                    "--problem", "p", "--mechanism", "m", "--hypothesis", "h", check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertTrue(result.stderr.strip())
                self.assertEqual(before, path.read_bytes())

    def test_idea_validator_requires_scientific_fields_for_active_status(self) -> None:
        _workflow_io, records = self.import_workflow_modules()
        draft = cli_json(
            "new-idea", "--project", self.project, "--title", "科学字段", "--note", "草稿"
        )
        incomplete = {**draft, "status": "active"}
        validator = getattr(records, "validate_idea", None)

        self.assertTrue(callable(validator), "缺少共享 Idea validator")
        with self.assertRaises(ValueError):
            validator(incomplete, expected_id=draft["id"])

    def test_commands_reject_linked_project_root_without_external_changes(self) -> None:
        commands = ("new", "register", "activate")
        for command in commands:
            with self.subTest(command=command):
                external = Path(self.temp_dir.name) / f"external-root-{command}"
                run_cli("init", "--path", external, "--name", "外部项目")
                idea_id = None
                if command == "activate":
                    idea_id = cli_json(
                        "new-idea", "--project", external, "--title", "外部", "--note", "草稿"
                    )["id"]
                linked_root = Path(self.temp_dir.name) / f"linked-root-{command}"
                self.make_directory_link(linked_root, external)
                before = self.snapshot_tree(external)
                if command == "new":
                    result = run_cli(
                        "new-idea", "--project", linked_root, "--title", "越界", "--note", "拒绝",
                        check=False,
                    )
                elif command == "register":
                    result = run_cli(
                        "register-version", "--project", linked_root, "--name", "越界",
                        "--repo-url", "https://example.invalid/repo.git", "--commit", "a" * 40,
                        check=False,
                    )
                else:
                    result = run_cli(
                        "activate-idea", "--project", linked_root, "--idea", idea_id,
                        "--problem", "p", "--mechanism", "m", "--hypothesis", "h", check=False,
                    )
                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertTrue(result.stderr.strip())
                self.assertEqual(before, self.snapshot_tree(external))

    def test_activate_rejects_linked_ideas_directory_without_external_write(self) -> None:
        draft = cli_json(
            "new-idea", "--project", self.project, "--title", "外部草稿", "--note", "拒绝"
        )
        ideas = self.control / "ideas"
        ideas.rename(self.control / "ideas-real")
        external = Path(self.temp_dir.name) / "external-activate"
        external.mkdir()
        external_idea = external / f"{draft['id']}.json"
        external_idea.write_bytes(
            (self.control / "ideas-real" / f"{draft['id']}.json").read_bytes()
        )
        self.make_directory_link(ideas, external)
        before = self.snapshot_tree(external)

        result = run_cli(
            "activate-idea", "--project", self.project, "--idea", draft["id"],
            "--problem", "p", "--mechanism", "m", "--hypothesis", "h", check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertTrue(result.stderr.strip())
        self.assertEqual(before, self.snapshot_tree(external))

    def test_activate_rejects_linked_idea_json_without_external_write(self) -> None:
        draft = cli_json(
            "new-idea", "--project", self.project, "--title", "链接对象", "--note", "拒绝"
        )
        idea = self.control / "ideas" / f"{draft['id']}.json"
        external = Path(self.temp_dir.name) / "external-idea.json"
        external.write_bytes(idea.read_bytes())
        idea.unlink()
        self.make_file_link(idea, external)
        before = external.read_bytes()

        result = run_cli(
            "activate-idea", "--project", self.project, "--idea", draft["id"],
            "--problem", "p", "--mechanism", "m", "--hypothesis", "h", check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertTrue(result.stderr.strip())
        self.assertEqual(before, external.read_bytes())

    def test_rejects_linked_control_root_without_external_write(self) -> None:
        external_project = Path(self.temp_dir.name) / "external-project"
        run_cli("init", "--path", external_project, "--name", "外部项目")
        external_control = external_project / ".experiment-workflow"
        before = self.snapshot_tree(external_control)
        victim = Path(self.temp_dir.name) / "victim"
        victim.mkdir()
        self.make_directory_link(victim / ".experiment-workflow", external_control)

        result = run_cli(
            "new-idea", "--project", victim, "--title", "越界", "--note", "拒绝",
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertTrue(result.stderr.strip())
        self.assertEqual(before, self.snapshot_tree(external_control))
        self.assertEqual([victim / ".experiment-workflow"], list(victim.iterdir()))

    def test_rejects_incomplete_or_invalid_project_metadata(self) -> None:
        project_json = self.control / "project.json"
        valid = read_json(project_json)
        invalid_payloads = (
            {"schema": valid["schema"], "name": valid["name"]},
            {"schema": valid["schema"], "project_id": valid["project_id"]},
            {**valid, "schema": "cv-experiment-workflow.project.v0"},
            {**valid, "project_id": "not-a-uuid"},
            {**valid, "project_id": 123},
            {**valid, "name": "   "},
            {**valid, "name": ["wrong-type"]},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                before = self.snapshot_tree(self.control / "ideas")
                project_json.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
                result = run_cli(
                    "new-idea", "--project", self.project, "--title", "元数据", "--note", "拒绝",
                    check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertTrue(result.stderr.strip())
                self.assertEqual(before, self.snapshot_tree(self.control / "ideas"))


if __name__ == "__main__":
    unittest.main()
