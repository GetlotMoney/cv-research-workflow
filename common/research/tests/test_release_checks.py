from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "run_release_checks.py"


def _windows_private_path(*parts: str) -> str:
    separator = chr(92)
    return "C:" + separator + separator.join(parts)


def load_tool():
    if not TOOL.is_file():
        raise AssertionError("发布复查工具尚未实现")
    spec = importlib.util.spec_from_file_location("run_release_checks", TOOL)
    if spec is None or spec.loader is None:
        raise AssertionError("无法加载发布复查工具")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ReleaseChecksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "发布 检查"
        self._make_valid_release()

    def _write(self, relative: str, content: str) -> Path:
        target = self.root.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def _make_valid_release(self) -> None:
        self._write(
            "research/pyproject.toml",
            (
                '[project]\nname = "research-fixture"\nversion = "1.0.0"\n'
                '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
            ),
        )
        self._write(
            "research/tests/test_ok.py",
            "def test_research_ok():\n    assert True\n",
        )
        self._write(
            "research/docs/test_snapshot.py",
            "raise RuntimeError('docs 快照不应被收集')\n",
        )
        self._write("research/tools/helper.py", "VALUE = 1\n")
        self._write(
            "paperflow/pytest.ini",
            "[pytest]\ntestpaths = tests_v2\n",
        )
        self._write(
            "paperflow/tests_v2/test_ok.py",
            "def test_paperflow_ok():\n    assert True\n",
        )
        self._write(
            "paperflow/docs/test_snapshot.py",
            "raise RuntimeError('docs 快照不应被收集')\n",
        )
        self._write("paperflow/paperflow_v2/app.py", "VALUE = 1\n")
        self._rewrite_manifests()

    def _rewrite_manifests(self) -> None:
        module = load_tool()
        payload = []
        for path in sorted(
            (
                item
                for item in self.root.rglob("*")
                if item.is_file()
                and item.name
                not in {"release-manifest.json", "release-exclusions.json"}
            ),
            key=lambda item: item.relative_to(self.root).as_posix(),
        ):
            content = path.read_bytes()
            payload.append(
                {
                    "path": path.relative_to(self.root).as_posix(),
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        exclusions = {
            "schema": "cv-research-paperflow.release-exclusions.v1",
            "excluded": [],
            "excluded_patterns": ["fixture: none"],
            "download_urls": module.DOWNLOAD_URLS,
        }
        exclusions_path = self._write(
            "release-exclusions.json",
            json.dumps(
                exclusions,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        exclusions_bytes = exclusions_path.read_bytes()
        manifest = {
            "schema": "cv-research-paperflow.release-staging.v1",
            "version": "1.0.0",
            "payload_bytes": sum(item["size"] for item in payload),
            "file_count": len(payload),
            "files": payload,
            "control_files": [
                {
                    "path": "release-exclusions.json",
                    "size": len(exclusions_bytes),
                    "sha256": hashlib.sha256(exclusions_bytes).hexdigest(),
                }
            ],
        }
        self._write(
            "release-manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )

    def _tree_state(self) -> dict[str, tuple[int, str]]:
        state: dict[str, tuple[int, str]] = {}
        for path in sorted(
            (item for item in self.root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(self.root).as_posix(),
        ):
            content = path.read_bytes()
            state[path.relative_to(self.root).as_posix()] = (
                len(content),
                hashlib.sha256(content).hexdigest(),
            )
        return state

    def test_valid_release_passes_structure_hash_size_and_collection_checks(
        self,
    ) -> None:
        module = load_tool()

        report = module.run_release_checks(self.root)

        self.assertTrue(report["ok"], report)
        groups = {item["id"]: item for item in report["test_groups"]}
        self.assertEqual({"research", "paperflow"}, set(groups))
        self.assertTrue(
            all(item["status"] == "COLLECTION_PASS" for item in groups.values())
        )
        self.assertTrue(all(item["mode"] == "collect_only" for item in groups.values()))
        self.assertTrue(all(item["executed"] is False for item in groups.values()))
        self.assertTrue(
            all(
                any("没有执行测试函数" in value for value in item["limitations"])
                for item in groups.values()
            )
        )
        self.assertTrue(
            all(
                "/docs/" not in node.replace("\\", "/")
                for item in groups.values()
                for node in item["collected"]
            )
        )
        self.assertTrue(
            all(
                item["timeout_seconds"] > 0 and item["seed"] > 0
                for item in groups.values()
            )
        )

    def test_collection_imports_packages_from_each_candidate_root(self) -> None:
        module = load_tool()
        self._write("research/candidate_pkg/__init__.py", "VALUE = 7\n")
        self._write(
            "research/tests/test_local_import.py",
            (
                "from candidate_pkg import VALUE\n\n"
                "def test_local_import():\n"
                "    assert VALUE == 7\n"
            ),
        )
        self._write("paperflow/paperflow_v2/__init__.py", "VALUE = 9\n")
        self._write(
            "paperflow/tests_v2/test_local_import.py",
            (
                "from paperflow_v2 import VALUE\n\n"
                "def test_local_import():\n"
                "    assert VALUE == 9\n"
            ),
        )
        self._rewrite_manifests()

        report = module.run_release_checks(self.root)

        self.assertTrue(report["ok"], report)
        self.assertTrue(
            all(
                item["status"] == "COLLECTION_PASS"
                for item in report["test_groups"]
            )
        )

    def test_core_api_never_reports_pass_when_required_collection_is_disabled(
        self,
    ) -> None:
        module = load_tool()

        report = module.run_release_checks(
            self.root,
            run_collections=False,
        )

        self.assertFalse(report["ok"], report)
        self.assertEqual([], report["test_groups"])
        collection = next(
            item
            for item in report["checks"]
            if item["id"] == "required_test_collection"
        )
        self.assertEqual("FAIL", collection["status"])

    def test_required_collection_timeout_and_skip_are_failures(self) -> None:
        module = load_tool()
        timeout_group = module.run_test_group(
            group_id="timeout",
            cwd=self.root / "research",
            test_root="tests",
            timeout_seconds=0.05,
            seed=1801,
            command=[
                sys.executable,
                "-c",
                "import time; time.sleep(1)",
            ],
        )
        self.assertEqual("COLLECTION_FAIL", timeout_group["status"])
        self.assertTrue(timeout_group["timed_out"])

        self._write(
            "research/tests/test_skip.py",
            "import pytest\npytest.skip('required missing', allow_module_level=True)\n",
        )
        skip_group = module.run_test_group(
            group_id="skip",
            cwd=self.root / "research",
            test_root="tests",
            timeout_seconds=20,
            seed=1802,
        )
        self.assertEqual("COLLECTION_FAIL", skip_group["status"])
        self.assertGreater(skip_group["skipped"], 0)

    def test_collection_output_is_bounded_and_fails_closed(self) -> None:
        module = load_tool()
        with mock.patch.object(module, "MAX_CAPTURED_OUTPUT_BYTES", 1024):
            group = module.run_test_group(
                group_id="output-overflow",
                cwd=self.root / "research",
                test_root="tests",
                timeout_seconds=20,
                seed=1803,
                command=[
                    sys.executable,
                    "-c",
                    "print('x' * 5000)",
                ],
            )

        self.assertEqual("COLLECTION_FAIL", group["status"])
        self.assertTrue(group["output_limit_exceeded"])
        self.assertLessEqual(group["captured_output_bytes"], 1024)

    def test_function_level_skip_and_true_skipif_fail_collection(self) -> None:
        module = load_tool()
        skipped = self._write(
            "research/tests/test_function_skip.py",
            (
                "import pytest\n"
                "@pytest.mark.skip(reason='required test disabled')\n"
                "def test_skipped():\n"
                "    assert True\n"
                "@pytest.mark.skipif(True, reason='required backend missing')\n"
                "def test_skipif_true():\n"
                "    assert True\n"
                "@pytest.mark.skipif(False, reason='available')\n"
                "def test_skipif_false():\n"
                "    assert True\n"
            ),
        )

        group = module.run_test_group(
            group_id="function-skip",
            cwd=self.root / "research",
            test_root="tests",
            timeout_seconds=20,
            seed=1803,
        )

        self.assertEqual("COLLECTION_FAIL", group["status"])
        self.assertEqual(2, group["skipped"])
        self.assertIn("test_skipped", "\n".join(group["skip_nodes"]))
        self.assertIn("test_skipif_true", "\n".join(group["skip_nodes"]))
        self.assertNotIn("test_skipif_false", "\n".join(group["skip_nodes"]))
        skipped.unlink()

    def test_collection_does_not_mutate_the_release_tree(self) -> None:
        module = load_tool()
        before = self._tree_state()

        report = module.run_release_checks(self.root)

        self.assertTrue(report["ok"], report)
        self.assertEqual(before, self._tree_state())
        self.assertFalse(
            any(
                path.name in {"__pycache__", ".pytest_cache"}
                or path.suffix == ".pyc"
                for path in self.root.rglob("*")
            )
        )

    def test_collection_detects_empty_directory_pollution_and_reports_import_risk(
        self,
    ) -> None:
        module = load_tool()
        polluter = self._write(
            "research/tests/test_directory_polluter.py",
            (
                "from pathlib import Path\n"
                "Path(__file__).resolve().parents[2].joinpath("
                "'empty-collection-pollution').mkdir(exist_ok=True)\n"
                "def test_polluter():\n"
                "    assert True\n"
            ),
        )
        self._rewrite_manifests()

        report = module.run_release_checks(self.root)

        self.assertFalse(report["ok"], report)
        immutable = next(
            item
            for item in report["checks"]
            if item["id"] == "post_collection_immutable"
        )
        self.assertEqual("FAIL", immutable["status"])
        research = next(
            item for item in report["test_groups"] if item["id"] == "research"
        )
        limitations = "\n".join(research["limitations"])
        self.assertIn("导入", limitations)
        self.assertIn("不能证明", limitations)
        polluter.unlink()

    def test_forbidden_database_unknown_top_level_and_hash_drift_fail_closed(
        self,
    ) -> None:
        module = load_tool()
        database = self._write("research/cache/knowledge.db", "forbidden")
        self._rewrite_manifests()
        report = module.run_release_checks(self.root, run_collections=False)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("数据库" in item["detail"] or ".db" in item["detail"] for item in report["checks"])
        )

        database.unlink()
        self._write("mystery.txt", "unknown top-level")
        self._rewrite_manifests()
        report = module.run_release_checks(self.root, run_collections=False)
        self.assertFalse(report["ok"])
        self.assertTrue(any("顶层" in item["detail"] for item in report["checks"]))

        (self.root / "mystery.txt").unlink()
        self._rewrite_manifests()
        self._write("research/tools/helper.py", "VALUE = 999\n")
        report = module.run_release_checks(self.root, run_collections=False)
        self.assertFalse(report["ok"])
        self.assertTrue(any("SHA-256" in item["detail"] for item in report["checks"]))

    def test_paperflow_security_policy_file_is_forbidden(self) -> None:
        module = load_tool()
        self._write(
            "paperflow/SECURITY.md",
            "用户专属安全说明，不得进入通用候选包\n",
        )
        self._rewrite_manifests()

        report = module.run_release_checks(self.root, run_collections=False)

        self.assertFalse(report["ok"])
        forbidden = next(
            item for item in report["checks"] if item["id"] == "forbidden_content"
        )
        self.assertEqual("FAIL", forbidden["status"])
        self.assertIn("paperflow/SECURITY.md", forbidden["detail"])

    def test_test_fixture_cannot_leak_release_parent_path(self) -> None:
        module = load_tool()
        leaked = self.root.parent / "private-review-archive"
        self._write(
            "paperflow/tests_v2/fixtures/README.md",
            f'SOURCE = "{leaked}"\n',
        )
        self._rewrite_manifests()

        report = module.run_release_checks(self.root, run_collections=False)

        self.assertFalse(report["ok"])
        absolute = next(
            item for item in report["checks"] if item["id"] == "absolute_paths"
        )
        self.assertEqual("FAIL", absolute["status"])
        self.assertIn("paperflow/tests_v2/fixtures/README.md", absolute["detail"])

    def test_absolute_path_leak_in_production_source_is_rejected(self) -> None:
        module = load_tool()
        self._write(
            "research/tools/leak.py",
            f"PRIVATE = {_windows_private_path('Users', 'secret', 'workspace', 'source.py')!r}\n",
        )
        self._rewrite_manifests()

        report = module.run_release_checks(self.root, run_collections=False)

        self.assertFalse(report["ok"])
        self.assertTrue(any("绝对路径" in item["detail"] for item in report["checks"]))

    def test_all_release_text_including_docs_and_pack_configs_rejects_paths(
        self,
    ) -> None:
        module = load_tool()
        self._write(
            "research/docs/windows-example.md",
            "示例命令：python tool.py --root "
            + _windows_private_path("example", "data").replace("C:", "D:", 1)
            + "\n",
        )
        self._rewrite_manifests()
        report = module.run_release_checks(self.root, run_collections=False)
        self.assertFalse(report["ok"])
        self.assertTrue(any("绝对路径" in item["detail"] for item in report["checks"]))

        (self.root / "research" / "docs" / "windows-example.md").unlink()
        self._write(
            "research/skills/cv-experiment-workflow/assets/pack.json",
            json.dumps(
                {
                    "source": _windows_private_path("private", "pack").replace(
                        "C:",
                        "D:",
                        1,
                    )
                }
            )
            + "\n",
        )
        self._rewrite_manifests()
        report = module.run_release_checks(self.root, run_collections=False)
        self.assertFalse(report["ok"])
        self.assertTrue(any("绝对路径" in item["detail"] for item in report["checks"]))

    def test_test_fixtures_allow_synthetic_paths_but_not_sensitive_roots(
        self,
    ) -> None:
        module = load_tool()
        fixture = self._write(
            "research/tests/test_private_fixture.py",
            (
                "ALICE = 'C:\\\\Users\\\\alice\\\\private\\\\fixture.py'\n"
                "ROOT_FIXTURE = '/root/private/fixture.py'\n"
                "def test_fixture(): assert ALICE and ROOT_FIXTURE\n"
            ),
        )
        self._rewrite_manifests()

        report = module.run_release_checks(
            self.root,
            run_collections=False,
        )
        absolute = next(
            item for item in report["checks"] if item["id"] == "absolute_paths"
        )
        self.assertEqual("PASS", absolute["status"])

        protected = self.root.parent / "private legacy"
        escaped_protected = str(protected).replace("\\", "\\\\")
        fixture.write_text(
            f"# LEAK={escaped_protected}\ndef test_fixture(): assert True\n",
            encoding="utf-8",
        )
        self._rewrite_manifests()
        report = module.run_release_checks(
            self.root,
            run_collections=False,
            protected_roots=(protected,),
        )
        absolute = next(
            item for item in report["checks"] if item["id"] == "absolute_paths"
        )
        self.assertEqual("FAIL", absolute["status"])

    def test_size_limit_and_reparse_links_are_rejected(self) -> None:
        module = load_tool()
        with mock.patch.object(
            module,
            "stable_read_file",
            wraps=module.stable_read_file,
        ) as stable_read:
            report = module.run_release_checks(
                self.root,
                run_collections=False,
                size_limit=32,
            )
        self.assertFalse(report["ok"])
        self.assertTrue(any("体积" in item["detail"] for item in report["checks"]))
        self.assertEqual(
            [],
            stable_read.call_args_list,
            "总量预检必须在把发布文件读入内存前拒绝超限目录",
        )

        fake = SimpleNamespace(
            st_mode=stat.S_IFREG,
            st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
        )
        self.assertTrue(module.has_reparse_flag(fake))

        target = self.root / "research" / "tools" / "helper.py"
        link = self.root / "research" / "tools" / "helper-link.py"
        try:
            link.symlink_to(target)
        except OSError:
            # 没有创建链接权限时仍实际覆盖 Windows reparse 位的判断分支。
            self.assertTrue(module.has_reparse_flag(fake))
        else:
            self._rewrite_manifests()
            report = module.run_release_checks(self.root, run_collections=False)
            self.assertFalse(report["ok"])
            self.assertTrue(any("link/reparse" in item["detail"] for item in report["checks"]))

    def test_unmanifested_empty_directories_and_hardlinks_fail_closed(self) -> None:
        module = load_tool()
        empty = self.root / "research" / "docs" / "unlisted-empty"
        empty.mkdir(parents=True)

        report = module.run_release_checks(self.root, run_collections=False)

        self.assertFalse(report["ok"], report)
        self.assertTrue(
            any(
                item["id"] == "directories" and item["status"] == "FAIL"
                for item in report["checks"]
            )
        )
        empty.rmdir()

        source = self.root / "research" / "tools" / "helper.py"
        linked = self.root / "research" / "tools" / "helper-hardlink.py"
        try:
            os.link(source, linked)
        except OSError as error:
            self.skipTest(f"当前文件系统不能创建 hardlink：{error}")
        self._rewrite_manifests()

        report = module.run_release_checks(self.root, run_collections=False)

        self.assertFalse(report["ok"], report)
        self.assertTrue(
            any(
                item["id"] == "hardlinks" and item["status"] == "FAIL"
                for item in report["checks"]
            )
        )

    def test_manifest_and_exclusions_require_exact_fields(self) -> None:
        module = load_tool()
        manifest_path = self.root / "release-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("file_count")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )

        report = module.run_release_checks(self.root, run_collections=False)

        self.assertFalse(report["ok"], report)
        self.assertTrue(
            any(
                item["id"] == "manifest" and item["status"] == "FAIL"
                for item in report["checks"]
            )
        )

        self._rewrite_manifests()
        exclusions_path = self.root / "release-exclusions.json"
        exclusions = json.loads(exclusions_path.read_text(encoding="utf-8"))
        exclusions.pop("excluded_patterns")
        exclusions_path.write_text(
            json.dumps(exclusions, ensure_ascii=False),
            encoding="utf-8",
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        content = exclusions_path.read_bytes()
        manifest["control_files"][0]["size"] = len(content)
        manifest["control_files"][0]["sha256"] = hashlib.sha256(content).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )

        report = module.run_release_checks(self.root, run_collections=False)

        self.assertFalse(report["ok"], report)
        self.assertTrue(
            any(
                item["id"] == "exclusions" and item["status"] == "FAIL"
                for item in report["checks"]
            )
        )

    def test_manifest_counts_and_exclusion_patterns_require_exact_types(
        self,
    ) -> None:
        module = load_tool()
        manifest_path = self.root / "release-manifest.json"
        exclusions_path = self.root / "release-exclusions.json"

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["payload_bytes"] = float(manifest["payload_bytes"])
        manifest["file_count"] = float(manifest["file_count"])
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        report = module.run_release_checks(
            self.root,
            run_collections=False,
        )
        manifest_check = next(
            item for item in report["checks"] if item["id"] == "manifest"
        )
        self.assertEqual("FAIL", manifest_check["status"], report)

        self._rewrite_manifests()
        exclusions = json.loads(exclusions_path.read_text(encoding="utf-8"))
        exclusions["excluded_patterns"] = None
        exclusions_path.write_text(
            json.dumps(exclusions, ensure_ascii=False),
            encoding="utf-8",
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        content = exclusions_path.read_bytes()
        manifest["control_files"][0]["size"] = len(content)
        manifest["control_files"][0]["sha256"] = hashlib.sha256(content).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        report = module.run_release_checks(
            self.root,
            run_collections=False,
        )
        exclusions_check = next(
            item for item in report["checks"] if item["id"] == "exclusions"
        )
        self.assertEqual("FAIL", exclusions_check["status"], report)

    def test_control_json_rejects_duplicate_keys_and_nan(self) -> None:
        module = load_tool()
        target = self.root.parent / "bad-control.json"
        for content in (
            b'{"schema": "first", "schema": "second"}',
            b'{"schema": NaN}',
        ):
            with self.subTest(content=content):
                target.write_bytes(content)
                with self.assertRaisesRegex(ValueError, "严格|重复|JSON"):
                    module._read_json(target)

    def test_windows_unsafe_names_and_case_collisions_are_rejected_in_manifest(
        self,
    ) -> None:
        module = load_tool()
        manifest = json.loads(
            (self.root / "release-manifest.json").read_text(encoding="utf-8")
        )
        manifest["files"].append(
            {
                "path": "research/tools/CON.py",
                "size": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
            }
        )
        (self.root / "release-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )

        report = module.run_release_checks(self.root, run_collections=False)

        self.assertFalse(report["ok"])
        self.assertTrue(any("Windows" in item["detail"] for item in report["checks"]))

    def test_download_allowlist_and_exclusions_control_hash_are_exact(self) -> None:
        module = load_tool()
        manifest = json.loads(
            (self.root / "release-manifest.json").read_text(encoding="utf-8")
        )
        self.assertIn("control_files", manifest)
        self.assertEqual(
            ["release-exclusions.json"],
            [item["path"] for item in manifest["control_files"]],
        )

        exclusions_path = self.root / "release-exclusions.json"
        exclusions = json.loads(exclusions_path.read_text(encoding="utf-8"))
        exclusions["download_urls"] = [
            {
                "id": "untrusted-installer",
                "purpose": "替换下载地址",
                "url": "https://attacker.invalid/payload",
                "kind": "landing_page",
                "version": None,
                "revision": None,
                "sha256": None,
            }
        ]
        exclusions_path.write_text(
            json.dumps(exclusions, ensure_ascii=False),
            encoding="utf-8",
        )

        report = module.run_release_checks(self.root, run_collections=False)

        self.assertFalse(report["ok"])
        details = "\n".join(item["detail"] for item in report["checks"])
        self.assertRegex(details, "下载|allowlist|control|SHA-256")

    def test_cli_writes_json_and_returns_nonzero_on_failure(self) -> None:
        module = load_tool()
        output = self.root.parent / "检查结果.json"
        self._write("research/cache/bad.db", "bad")
        self._rewrite_manifests()

        code = module.main(
            [
                "--root",
                str(self.root),
                "--json-out",
                str(output),
                "--no-collect",
            ]
        )

        self.assertEqual(1, code)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(report["ok"])

    def test_cli_json_output_never_overwrites_or_mutates_staging_or_sample_project(
        self,
    ) -> None:
        module = load_tool()
        existing = self.root.parent / "existing-check.json"
        existing.write_text("用户文件", encoding="utf-8")
        inside_staging = self.root / "check-result.json"
        protected_root = self.root.parent / "旧 sample_project"
        protected = protected_root / "codex-release-check-never-write.json"

        for target in (existing, inside_staging, protected):
            with self.subTest(target=target):
                code = module.main(
                    [
                        "--root",
                        str(self.root),
                        "--json-out",
                        str(target),
                        "--no-collect",
                        "--protected-root",
                        str(protected_root),
                    ]
                )
                self.assertEqual(1, code)

        self.assertEqual("用户文件", existing.read_text(encoding="utf-8"))
        self.assertFalse(inside_staging.exists())
        self.assertFalse(os.path.lexists(protected))

    def test_timeout_kills_only_owned_process_tree_and_keeps_neighbor_alive(
        self,
    ) -> None:
        module = load_tool()
        neighbor = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"]
        )
        child_pid_file = self.root.parent / "owned-child.pid"
        script = (
            "import pathlib, subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', "
            "\"import time; time.sleep(20)\"]); "
            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid)); "
            "time.sleep(20)"
        )
        try:
            group = module.run_test_group(
                group_id="owned-tree-timeout",
                cwd=self.root / "research",
                test_root="tests",
                timeout_seconds=1,
                seed=1811,
                command=[sys.executable, "-c", script],
            )
            self.assertEqual("COLLECTION_FAIL", group["status"])
            self.assertTrue(group["timed_out"])
            self.assertGreater(group["process_identity"]["pid"], 0)
            self.assertGreater(
                group["process_identity"]["started_monotonic_ns"],
                0,
            )
            self.assertIsNone(neighbor.poll(), "旁边的用户 Python 不得被误杀")
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            for _ in range(20):
                if not module.process_is_alive(child_pid):
                    break
                time.sleep(0.05)
            self.assertFalse(
                module.process_is_alive(child_pid),
                "测试进程创建的孙进程必须被回收",
            )
        finally:
            if neighbor.poll() is None:
                neighbor.terminate()
                try:
                    neighbor.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    neighbor.kill()
                    neighbor.wait(timeout=5)

    def test_process_tree_cleanup_uses_owned_handle_not_taskkill_pid(self) -> None:
        module = load_tool()

        class Process:
            pid = 4242
            returncode = None

            def poll(self):
                return None

            def wait(self, timeout: int):
                self.returncode = 1
                return 1

        class Owner:
            def __init__(self) -> None:
                self.calls: list[tuple[object, dict[str, int]]] = []

            def terminate(
                self,
                process: object,
                identity: dict[str, int],
            ) -> None:
                self.calls.append((process, identity))

        process = Process()
        owner = Owner()
        identity = {"pid": process.pid, "started_monotonic_ns": 1}
        with mock.patch.object(
            module.subprocess,
            "run",
            side_effect=AssertionError("不得调用 taskkill 或其他 PID 终止命令"),
        ):
            module._terminate_owned_process_tree(process, identity, owner)

        self.assertEqual([(process, identity)], owner.calls)

    def test_start_failure_and_cleanup_runtime_error_are_structured_failures(
        self,
    ) -> None:
        module = load_tool()
        with mock.patch.object(
            module,
            "_start_owned_process",
            side_effect=RuntimeError("job assign failed"),
        ):
            start_failed = module.run_test_group(
                group_id="start-failed",
                cwd=self.root / "research",
                test_root="tests",
                timeout_seconds=1,
                seed=1823,
                command=[sys.executable, "-c", "print('never')"],
            )
        self.assertEqual("COLLECTION_FAIL", start_failed["status"])
        self.assertEqual("NOT_STARTED", start_failed["process_tree_cleanup"])
        self.assertIn("job assign failed", start_failed["detail"])

        class Process:
            pid = 4343
            returncode = 0
            stdout = io.StringIO("")

            def poll(self):
                return self.returncode

            def wait(self, timeout: float):
                return self.returncode

            def kill(self):
                raise AssertionError("运行阶段禁止裸 PID kill 降级")

        class Owner:
            kind = "test-owner"

            def terminate(self, process, identity):
                raise RuntimeError("owned cleanup failed")

            def close(self):
                return None

        with mock.patch.object(
            module,
            "_start_owned_process",
            return_value=(Process(), Owner()),
        ):
            cleanup_failed = module.run_test_group(
                group_id="cleanup-failed",
                cwd=self.root / "research",
                test_root="tests",
                timeout_seconds=1,
                seed=1829,
                command=[sys.executable, "-c", "print('done')"],
            )
        self.assertEqual("COLLECTION_FAIL", cleanup_failed["status"])
        self.assertEqual("FAIL", cleanup_failed["process_tree_cleanup"])
        self.assertIn("owned cleanup failed", cleanup_failed["detail"])

    def test_windows_start_assigns_suspended_process_before_resume(self) -> None:
        module = load_tool()
        calls: list[str] = []
        seen_kwargs: dict[str, object] = {}

        class Process:
            pid = 4401
            returncode = None

            def poll(self):
                return self.returncode

        class Owner:
            kind = "windows_job_object"

            def assign(self, process):
                calls.append("assign")

            def resume(self, process):
                calls.append("resume")

            def close(self):
                calls.append("close")

        def popen(command, **kwargs):
            seen_kwargs.update(kwargs)
            return Process()

        with mock.patch.object(module.os, "name", "nt"), mock.patch.object(
            module,
            "_WindowsJobOwner",
            return_value=Owner(),
        ), mock.patch.object(module.subprocess, "Popen", side_effect=popen):
            process, owner = module._start_owned_process(
                [sys.executable, "-c", "print('user code')"]
            )

        self.assertEqual(4401, process.pid)
        self.assertEqual("windows_job_object", owner.kind)
        self.assertEqual(["assign", "resume"], calls)
        creationflags = int(seen_kwargs["creationflags"])
        self.assertTrue(
            creationflags
            & getattr(module.subprocess, "CREATE_SUSPENDED", 0x00000004)
        )

    @unittest.skipUnless(os.name == "nt", "Windows Job Object 原子启动测试")
    def test_windows_user_code_starts_only_after_job_assignment(self) -> None:
        module = load_tool()
        marker = self.root.parent / "user-code-started.txt"
        real_owner_type = module._WindowsJobOwner

        class RecordingOwner(real_owner_type):
            def assign(self, process):
                self.assignment_observed_before_user_code = not marker.exists()
                super().assign(process)

        owner = RecordingOwner()
        with mock.patch.object(module, "_WindowsJobOwner", return_value=owner):
            process, returned_owner = module._start_owned_process(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    (
                        "from pathlib import Path; "
                        f"Path({str(marker)!r}).write_text('started', encoding='utf-8')"
                    ),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        try:
            process.communicate(timeout=10)
            identity = {
                "pid": process.pid,
                "started_monotonic_ns": time.monotonic_ns(),
            }
            module._terminate_owned_process_tree(process, identity, returned_owner)
        finally:
            returned_owner.close()

        self.assertTrue(owner.assignment_observed_before_user_code)
        self.assertTrue(marker.is_file())

    def test_normal_completion_reaps_owned_descendants(self) -> None:
        module = load_tool()
        child_pid_file = self.root.parent / "normal-owned-child.pid"
        script = (
            "import pathlib, subprocess, sys; "
            "child = subprocess.Popen([sys.executable, '-c', "
            "\"import time; time.sleep(20)\"]); "
            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid))"
        )

        group = module.run_test_group(
            group_id="normal-owned-tree",
            cwd=self.root / "research",
            test_root="tests",
            timeout_seconds=10,
            seed=1831,
            command=[sys.executable, "-c", script],
        )

        self.assertEqual("COLLECTION_PASS", group["status"], group)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        for _ in range(40):
            if not module.process_is_alive(child_pid):
                break
            time.sleep(0.05)
        self.assertFalse(
            module.process_is_alive(child_pid),
            "父进程正常退出后，仍存活的 owned descendant 也必须回收",
        )


if __name__ == "__main__":
    unittest.main()
