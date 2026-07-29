from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "check_runtime_environment.py"
EXPECTED_DIRECTIONS = {"cls", "det", "seg", "instseg", "sr", "gzsl"}


def load_tool():
    if not TOOL.is_file():
        raise AssertionError("环境预检工具尚未实现")
    spec = importlib.util.spec_from_file_location("check_runtime_environment", TOOL)
    if spec is None or spec.loader is None:
        raise AssertionError("无法加载环境预检工具")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def completed(command: list[str], stdout: str = "ok") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def _windows_tool_path(name: str) -> str:
    separator = chr(92)
    return "C:" + separator + separator.join(("Tools", f"{name}.exe"))


class RuntimeEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "科研 工作流"
        self.root.mkdir(parents=True)
        catalog = self.root / "config" / "directions" / "catalog.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text(
            json.dumps(
                {
                    "schema": "cvwf.direction-catalog.v1",
                    "version": "DATA-DIRECTIONS-TEST",
                    "directions": [
                        {
                            "id": direction_id,
                            "slug": slug,
                            "label_zh": label,
                            "status": "ready" if slug == "gzsl" else "pending",
                            "repository_factory": (
                                "create_gzsl_repository"
                                if slug == "gzsl"
                                else None
                            ),
                        }
                        for direction_id, slug, label in (
                            ("image_classification", "cls", "图像分类"),
                            ("object_detection", "det", "目标检测"),
                            ("instance_segmentation", "instseg", "实例分割"),
                            ("semantic_segmentation", "seg", "语义分割"),
                            ("super_resolution", "sr", "超分辨率"),
                            ("gzsl", "gzsl", "广义零样本学习"),
                        )
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _write_pack_registry(self, directions: list[str]) -> None:
        packs_root = (
            self.root
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
        )
        packs_root.mkdir(parents=True)
        records = []
        for direction in sorted(directions):
            version = "1.1.1" if direction == "gzsl" else "1.0.0"
            device = "cuda" if direction == "gzsl" else "cpu"
            directory = f"{direction}-v{version}"
            payload = packs_root / directory / "payload"
            payload.mkdir(parents=True)
            smoke_contract = {
                "run_kind": "synthetic_debug_only",
                "paper_eligible": False,
                "device": device,
            }
            if direction == "gzsl":
                smoke_contract["cpu_fallback"] = False
            (payload / "domain-pack.json").write_text(
                json.dumps(
                    {
                        "schema": "cv-experiment-workflow.runnable-domain-pack.v1",
                        "id": direction,
                        "version": version,
                        "template_id": f"PACK-{direction.upper()}",
                        "primary_direction": direction,
                        "commands": ["train", "evaluate", "infer", "synthetic_smoke"],
                        "entrypoints": {
                            "train": f"python -m {direction}.train",
                            "evaluate": f"python -m {direction}.evaluate",
                            "infer": f"python -m {direction}.infer",
                            "synthetic_smoke": f"python -m {direction}.smoke",
                        },
                        "synthetic_smoke": smoke_contract,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            payload_files = []
            for path in sorted(
                item for item in payload.rglob("*") if item.is_file()
            ):
                content = path.read_bytes()
                payload_files.append(
                    {
                        "path": path.relative_to(payload).as_posix(),
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
            (packs_root / directory / "pack.json").write_text(
                json.dumps(
                    {
                        "schema": "cv-experiment-workflow.domain-pack.v1",
                        "id": direction,
                        "version": version,
                        "template_id": f"PACK-{direction.upper()}",
                        "primary_direction": direction,
                        "license": "MIT",
                        "source_references": [],
                        "files": payload_files,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            records.append(
                {
                    "id": direction,
                    "version": version,
                    "template_id": f"PACK-{direction.upper()}",
                    "primary_direction": direction,
                    "directory": directory,
                }
            )
        (packs_root / "registry.json").write_text(
            json.dumps(
                {
                    "schema": "cv-experiment-workflow.domain-pack-registry.v1",
                    "packs": records,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _healthy_patches(self, module):
        return (
            mock.patch.object(module.platform, "system", return_value="Windows"),
            mock.patch.object(module.sys, "version_info", (3, 11, 9)),
            mock.patch.object(
                module.shutil,
                "which",
                side_effect=_windows_tool_path,
            ),
            mock.patch.object(module, "_module_available", return_value=True),
            mock.patch.object(
                module,
                "_run_command",
                side_effect=lambda command, **_: completed(command),
            ),
        )

    def _write_smoke_module(self, direction: str, source: str) -> Path:
        payload = self.root / "payload"
        package = payload / direction
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "smoke.py").write_text(
            textwrap.dedent(source),
            encoding="utf-8",
        )
        return payload

    def _rewrite_pack_manifest(self, pack_root: Path) -> None:
        payload = pack_root / "payload"
        records = []
        for path in sorted(
            (item for item in payload.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(payload).as_posix(),
        ):
            content = path.read_bytes()
            records.append(
                {
                    "path": path.relative_to(payload).as_posix(),
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        manifest = json.loads(
            (pack_root / "pack.json").read_text(encoding="utf-8")
        )
        manifest["files"] = records
        (pack_root / "pack.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_current_repository_checks_all_manifests_and_only_gzsl_is_ready(
        self,
    ) -> None:
        module = load_tool()
        calls: list[tuple[str, str, int, int]] = []

        def smoke(pack: Path, direction: str, device: str, seed: int, timeout: int):
            calls.append((pack.name, device, seed, timeout))
            return {
                "status": "PASS",
                "detail": "不应执行方向包",
                "duration_seconds": 0.01,
                "guard": {
                    "schema": "cv-experiment-workflow.smoke-guard.v1",
                    "status": "PASS",
                    "violations": [],
                },
            }

        patches = self._healthy_patches(module)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with mock.patch.object(
                module,
                "_module_available",
                side_effect=lambda name: name != "pycocotools",
            ):
                with mock.patch.object(module, "_run_pack_smoke", side_effect=smoke):
                    report = module.check_runtime_environment(ROOT)

        pack_items = {item["id"]: item for item in report["domain_packs"]}
        self.assertEqual(EXPECTED_DIRECTIONS, set(pack_items))
        self.assertEqual("ready", pack_items["gzsl"]["availability"])
        self.assertEqual("PASS", pack_items["gzsl"]["status"])
        self.assertEqual("1.1.1", pack_items["gzsl"]["version"])
        self.assertEqual("cuda", pack_items["gzsl"]["device"])
        self.assertFalse(pack_items["gzsl"]["cpu_fallback"])
        for direction in EXPECTED_DIRECTIONS - {"gzsl"}:
            with self.subTest(direction=direction):
                self.assertEqual("pending", pack_items[direction]["availability"])
                self.assertEqual("PENDING", pack_items[direction]["status"])
                self.assertEqual("1.0.0", pack_items[direction]["version"])
                self.assertEqual("cpu", pack_items[direction]["device"])
        self.assertTrue(report["ok"])
        self.assertEqual([], calls)

    def test_manifest_versions_and_devices_are_read_per_pack_without_execution(
        self,
    ) -> None:
        module = load_tool()
        self._write_pack_registry(sorted(EXPECTED_DIRECTIONS))
        calls: list[tuple[str, str, int, int]] = []

        def smoke(pack: Path, direction: str, device: str, seed: int, timeout: int):
            calls.append((direction, device, seed, timeout))
            return {
                "status": "PASS",
                "detail": "通过",
                "duration_seconds": 0.02,
                "guard": {
                    "schema": "cv-experiment-workflow.smoke-guard.v1",
                    "status": "PASS",
                    "violations": [],
                },
            }

        patches = self._healthy_patches(module)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with mock.patch.object(module, "_run_pack_smoke", side_effect=smoke):
                report = module.check_runtime_environment(self.root)

        pack_items = {item["id"]: item for item in report["domain_packs"]}
        self.assertTrue(report["ok"])
        self.assertEqual([], calls)
        self.assertEqual(
            {
                "cls": ("1.0.0", "cpu", "pending"),
                "det": ("1.0.0", "cpu", "pending"),
                "gzsl": ("1.1.1", "cuda", "ready"),
                "instseg": ("1.0.0", "cpu", "pending"),
                "seg": ("1.0.0", "cpu", "pending"),
                "sr": ("1.0.0", "cpu", "pending"),
            },
            {
                direction: (
                    item["version"],
                    item["device"],
                    item["availability"],
                )
                for direction, item in pack_items.items()
            },
        )

    def test_gzsl_manifest_cpu_is_explicitly_rejected_without_execution(
        self,
    ) -> None:
        module = load_tool()
        self._write_pack_registry(sorted(EXPECTED_DIRECTIONS))
        pack_root = (
            self.root
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "gzsl-v1.1.1"
        )
        manifest_path = pack_root / "payload" / "domain-pack.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["synthetic_smoke"]["device"] = "cpu"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        self._rewrite_pack_manifest(pack_root)

        patches = self._healthy_patches(module)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with mock.patch.object(module, "_run_pack_smoke") as smoke:
                report = module.check_runtime_environment(self.root)

        gzsl = next(
            item for item in report["domain_packs"] if item["id"] == "gzsl"
        )
        self.assertEqual("FAIL", gzsl["status"])
        self.assertRegex(gzsl["detail"], "GZSL.*CPU|CPU.*GZSL")
        smoke.assert_not_called()

    def test_pack_seal_and_exact_entrypoints_are_verified_before_smoke(
        self,
    ) -> None:
        module = load_tool()
        self._write_pack_registry(sorted(EXPECTED_DIRECTIONS))
        cls_manifest = (
            self.root
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "cls-v1.0.0"
            / "payload"
            / "domain-pack.json"
        )
        payload = json.loads(cls_manifest.read_text(encoding="utf-8"))
        payload["entrypoints"]["train"] = "python -m attacker.main"
        cls_manifest.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        patches = self._healthy_patches(module)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with mock.patch.object(
                module,
                "_run_pack_smoke",
                return_value={
                    "status": "PASS",
                    "detail": "不应运行被篡改方向包",
                    "duration_seconds": 0.01,
                    "guard": {
                        "schema": "cv-experiment-workflow.smoke-guard.v1",
                        "status": "PASS",
                        "violations": [],
                    },
                },
            ) as smoke:
                report = module.check_runtime_environment(self.root)

        cls = next(
            item for item in report["domain_packs"] if item["id"] == "cls"
        )
        self.assertEqual("FAIL", cls["status"])
        self.assertRegex(cls["detail"], "SHA-256|清单|entrypoint|入口")
        self.assertNotIn(
            "cls",
            [
                call.args[1]
                for call in smoke.call_args_list
                if len(call.args) > 1
            ],
        )

    def test_required_dependency_or_smoke_failure_fails_closed(self) -> None:
        module = load_tool()
        self._write_pack_registry(sorted(EXPECTED_DIRECTIONS))

        def available(name: str) -> bool:
            return name != "torch"

        patches = self._healthy_patches(module)
        with patches[0], patches[1], patches[2]:
            with mock.patch.object(module, "_module_available", side_effect=available):
                with patches[4]:
                    with mock.patch.object(
                        module,
                        "_run_pack_smoke",
                        return_value={
                            "status": "FAIL",
                            "detail": "超时",
                            "duration_seconds": 30.0,
                            "guard": {
                                "schema": "cv-experiment-workflow.smoke-guard.v1",
                                "status": "PASS",
                                "violations": [],
                            },
                        },
                    ):
                        report = module.check_runtime_environment(self.root)

        required = {item["id"]: item for item in report["required"]}
        self.assertEqual("MISSING_REQUIRED", required["pytorch"]["status"])
        self.assertFalse(report["ok"])
        pack_items = {item["id"]: item for item in report["domain_packs"]}
        self.assertEqual("PASS", pack_items["gzsl"]["status"])
        self.assertTrue(
            all(
                pack_items[direction]["status"] == "PENDING"
                for direction in EXPECTED_DIRECTIONS - {"gzsl"}
            )
        )

    def test_optional_readiness_is_reported_with_download_urls_and_not_installed(
        self,
    ) -> None:
        module = load_tool()
        self._write_pack_registry(sorted(EXPECTED_DIRECTIONS))
        missing_optional = {
            "torchvision",
            "pycocotools",
            "docling",
            "sentence_transformers",
            "rapidocr_onnxruntime",
        }

        def available(name: str) -> bool:
            return name not in missing_optional

        patches = self._healthy_patches(module)
        with patches[0], patches[1], patches[2]:
            with mock.patch.object(module, "_module_available", side_effect=available):
                with patches[4]:
                    with mock.patch.object(
                        module,
                        "_run_pack_smoke",
                        return_value={
                            "status": "PASS",
                            "detail": "通过",
                            "duration_seconds": 0.01,
                            "guard": {
                                "schema": "cv-experiment-workflow.smoke-guard.v1",
                                "status": "PASS",
                                "violations": [],
                            },
                        },
                    ):
                        report = module.check_runtime_environment(self.root)

        optional = {item["id"]: item for item in report["optional_readiness"]}
        for item_id in (
            "torchvision",
            "pycocotools",
            "docling",
            "bge_m3_backend",
            "rapidocr",
            "coco_data",
            "real_cv_data",
        ):
            self.assertEqual(
                "OPTIONAL_NOT_INSTALLED",
                optional[item_id]["status"],
                item_id,
            )
            self.assertTrue(optional[item_id]["download_url"].startswith("https://"))
        self.assertTrue(report["ok"])

    def test_runner_forbids_network_install_commands_and_sets_offline_environment(
        self,
    ) -> None:
        module = load_tool()
        seen: dict[str, object] = {}

        class Process:
            pid = 43123
            returncode = 0
            stdout = io.StringIO("smoke ok\n")
            stderr = io.StringIO("")

            def communicate(self, timeout: int):
                seen["timeout"] = timeout
                return ("smoke ok\n", "")

            def wait(self, timeout: int):
                seen["timeout"] = timeout
                return self.returncode

            def kill(self):
                seen["killed"] = True

            def poll(self):
                return self.returncode

        class Owner:
            kind = "test-owner"

            def terminate(self, process, identity):
                seen["owner_terminated"] = True

            def close(self):
                seen["owner_closed"] = True

        def start(command, **kwargs):
            seen["command"] = command
            seen["env"] = kwargs["env"]
            seen["cwd"] = kwargs["cwd"]
            guard_path = Path(command[7])
            guard_path.write_text(
                json.dumps(
                    {
                        "schema": "cv-experiment-workflow.smoke-guard.v1",
                        "status": "PASS",
                        "violations": [],
                    }
                ),
                encoding="utf-8",
            )
            process = Process()
            seen["process"] = process
            return process, Owner()

        with mock.patch.object(module, "_start_owned_process", side_effect=start):
            result = module._run_pack_smoke(
                self.root,
                "cls",
                "cpu",
                seed=1731,
                timeout=41,
            )

        command_text = " ".join(seen["command"]).lower()
        self.assertEqual(["-I", "-B", "-c"], seen["command"][1:4])
        self.assertNotIn("-m pip", command_text)
        self.assertNotIn("-m conda", command_text)
        self.assertNotIn(" curl ", command_text)
        self.assertNotIn(" wget ", command_text)
        self.assertEqual("1", seen["env"]["HF_HUB_OFFLINE"])
        self.assertEqual("1", seen["env"]["TRANSFORMERS_OFFLINE"])
        self.assertEqual("1", seen["env"]["PIP_NO_INDEX"])
        self.assertEqual("1731", seen["env"]["PYTHONHASHSEED"])
        self.assertEqual("1", seen["env"]["PYTHONDONTWRITEBYTECODE"])
        self.assertNotEqual(self.root, seen["cwd"])
        self.assertTrue(result["source_immutable"])
        self.assertEqual("PASS", result["guard"]["status"])
        self.assertTrue(seen["owner_terminated"])
        self.assertTrue(seen["owner_closed"])
        process = seen["process"]
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        self.assertEqual("PASS", result["status"])

    def test_dependency_discovery_and_import_use_isolated_python(self) -> None:
        module = load_tool()
        commands: list[list[str]] = []

        def run(command: list[str], **_):
            commands.append(command)
            return completed(command, "available")

        with mock.patch.object(module, "_run_command", side_effect=run):
            self.assertTrue(module._module_available("torch"))
            available, _ = module._probe_python_module("torch")

        self.assertTrue(available)
        self.assertEqual(3, len(commands))
        self.assertTrue(all(command[1:3] == ["-I", "-B"] for command in commands))

    def test_smoke_runs_from_temporary_copy_and_original_source_stays_unchanged(
        self,
    ) -> None:
        module = load_tool()
        payload = self._write_smoke_module(
            "cls",
            """
            from pathlib import Path
            Path(__file__).with_name("copy-only-mutation.txt").write_text(
                "temporary", encoding="utf-8"
            )
            """,
        )
        before = {
            path.relative_to(payload).as_posix(): path.read_bytes()
            for path in payload.rglob("*")
            if path.is_file()
        }

        result = module._run_pack_smoke(payload, "cls", "cpu", 1733, 20)

        after = {
            path.relative_to(payload).as_posix(): path.read_bytes()
            for path in payload.rglob("*")
            if path.is_file()
        }
        self.assertEqual("PASS", result["status"], result)
        self.assertEqual(before, after)
        self.assertTrue(result["source_immutable"])
        self.assertEqual([], result["guard"]["violations"])
        self.assertFalse((payload / "cls" / "copy-only-mutation.txt").exists())
        self.assertFalse(any(path.suffix == ".pyc" for path in payload.rglob("*")))

    def test_validated_pack_bytes_are_the_only_bytes_executed_by_smoke(
        self,
    ) -> None:
        module = load_tool()
        self._write_pack_registry(["cls"])
        pack_root = (
            self.root
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "cls-v1.0.0"
        )
        payload = pack_root / "payload"
        package = payload / "cls"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "smoke.py").write_text(
            "print('validated snapshot')\n",
            encoding="utf-8",
        )
        self._rewrite_pack_manifest(pack_root)
        validated = module._validate_pack_contract(pack_root, "cls")

        (package / "smoke.py").write_text(
            "print('changed after validation')\n",
            encoding="utf-8",
        )
        result = module._run_pack_smoke(
            validated,
            "cls",
            "cpu",
            1733,
            20,
        )

        self.assertEqual("FAIL", result["status"], result)
        self.assertFalse(result["source_immutable"])
        self.assertIn("发生变化", result["detail"])

    def test_pack_snapshot_rejects_payload_and_control_hardlinks(self) -> None:
        module = load_tool()
        payload = self._write_smoke_module("cls", "VALUE = 1\n")
        source = payload / "cls" / "smoke.py"
        outside = self.root / "same-content.py"
        outside.write_bytes(source.read_bytes())
        source.unlink()
        try:
            os.link(outside, source)
        except OSError as error:
            self.skipTest(f"当前文件系统不能创建 hardlink：{error}")
        with self.assertRaisesRegex(ValueError, "hardlink"):
            module._snapshot_pack_tree(payload)

        self._write_pack_registry(["det"])
        pack_root = (
            self.root
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / "det-v1.0.0"
        )
        control = pack_root / "pack.json"
        outside_control = self.root / "same-pack.json"
        outside_control.write_bytes(control.read_bytes())
        control.unlink()
        try:
            os.link(outside_control, control)
        except OSError as error:
            self.skipTest(f"当前文件系统不能创建 control hardlink：{error}")
        with self.assertRaisesRegex(ValueError, "hardlink"):
            module._validate_pack_contract(pack_root, "det")

    def test_smoke_guard_report_rejects_duplicate_keys_and_nan(self) -> None:
        module = load_tool()
        guard = self.root / "guard.json"
        attacks = (
            (
                b'{"schema":"cv-experiment-workflow.smoke-guard.v1",'
                b'"status":"FAIL","status":"PASS","violations":[]}'
            ),
            (
                b'{"schema":"cv-experiment-workflow.smoke-guard.v1",'
                b'"status":"PASS","violations":[],"extra":NaN}'
            ),
        )
        for content in attacks:
            with self.subTest(content=content):
                guard.write_bytes(content)
                report = module._read_guard_report(guard)
                self.assertEqual("FAIL", report["status"])
                self.assertRegex(
                    report["violations"][0]["event"],
                    "invalid|无效|重复|JSON",
                )

    def test_smoke_output_is_bounded_and_fails_closed(self) -> None:
        module = load_tool()
        payload = self._write_smoke_module(
            "cls",
            "print('x' * 5000)\n",
        )

        with mock.patch.object(module, "MAX_CAPTURED_OUTPUT_BYTES", 1024):
            result = module._run_pack_smoke(
                payload,
                "cls",
                "cpu",
                1734,
                20,
            )

        self.assertEqual("FAIL", result["status"], result)
        self.assertTrue(result["output_limit_exceeded"])
        self.assertLessEqual(result["captured_output_bytes"], 1024)

    def test_pack_snapshot_tracks_empty_directories(self) -> None:
        module = load_tool()
        payload = self._write_smoke_module("cls", "VALUE = 1\n")
        before = module._snapshot_pack_tree(payload)

        (payload / "new-empty-directory").mkdir()
        after = module._snapshot_pack_tree(payload)

        self.assertNotEqual(before, after)

    def test_smoke_start_and_cleanup_runtime_errors_are_structured_without_pid_fallback(
        self,
    ) -> None:
        module = load_tool()
        payload = self._write_smoke_module("cls", "VALUE = 1\n")
        with mock.patch.object(
            module,
            "_start_owned_process",
            side_effect=RuntimeError("job binding failed"),
        ):
            start_failed = module._run_pack_smoke(
                payload,
                "cls",
                "cpu",
                1739,
                20,
            )
        self.assertEqual("FAIL", start_failed["status"])
        self.assertEqual("NOT_STARTED", start_failed["process_tree_cleanup"])
        self.assertIn("job binding failed", start_failed["detail"])

        class Process:
            pid = 5511
            returncode = 0
            stdout = io.StringIO("smoke ok\n")
            stderr = io.StringIO("")

            def communicate(self, timeout: int):
                return ("smoke ok\n", "")

            def poll(self):
                return self.returncode

            def wait(self, timeout: int):
                return self.returncode

            def kill(self):
                raise AssertionError("运行阶段禁止裸 PID kill 降级")

        class Owner:
            kind = "test-owner"

            def terminate(self, process, identity):
                raise RuntimeError("owned descendant cleanup failed")

            def close(self):
                return None

        with mock.patch.object(
            module,
            "_start_owned_process",
            return_value=(Process(), Owner()),
        ):
            cleanup_failed = module._run_pack_smoke(
                payload,
                "cls",
                "cpu",
                1740,
                20,
            )
        self.assertEqual("FAIL", cleanup_failed["status"])
        self.assertEqual("FAIL", cleanup_failed["process_tree_cleanup"])
        self.assertIn("owned descendant cleanup failed", cleanup_failed["detail"])

    def test_audit_guard_blocks_network_and_new_process_attempts_fail_closed(
        self,
    ) -> None:
        module = load_tool()
        attempts = {
            "network": """
                import socket
                socket.socket().connect(("127.0.0.1", 9))
            """,
            "process": """
                import subprocess, sys
                subprocess.run([sys.executable, "-c", "print('forbidden')"])
            """,
        }
        for name, source in attempts.items():
            with self.subTest(name=name):
                payload = self._write_smoke_module("cls", source)
                result = module._run_pack_smoke(
                    payload,
                    "cls",
                    "cpu",
                    1741,
                    20,
                )
                self.assertEqual("FAIL", result["status"], result)
                self.assertEqual("FAIL", result["guard"]["status"])
                self.assertTrue(result["guard"]["violations"])
                self.assertRegex(
                    json.dumps(result["guard"]["violations"]),
                    "socket|subprocess|process|install",
                )

    def test_network_report_only_claims_common_python_api_attempt_detection(
        self,
    ) -> None:
        module = load_tool()
        patches = self._healthy_patches(module)
        smoke = {
            "status": "PASS",
            "detail": "通过",
            "duration_seconds": 0.01,
            "guard": {
                "schema": "cv-experiment-workflow.smoke-guard.v1",
                "status": "PASS",
                "violations": [],
            },
        }
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with mock.patch.object(module, "_run_pack_smoke", return_value=smoke):
                report = module.check_runtime_environment(self.root)

        self.assertNotIn("network_install_performed", report)
        self.assertNotIn("network_install_guard", report)
        guard = report["python_api_attempt_guard"]
        self.assertFalse(guard["hard_isolation"])
        self.assertIn("常用 Python API", guard["scope"])
        limitations = "\n".join(guard["limitations"])
        for phrase in ("os.startfile", "ctypes", "环境变量", "完全断网"):
            self.assertIn(phrase, limitations)

    def test_cli_writes_json_and_returns_nonzero_when_required_items_are_missing(
        self,
    ) -> None:
        module = load_tool()
        output = Path(self.temporary.name) / "环境结果.json"
        report = {
            "schema": "cv-experiment-workflow.runtime-environment.v1",
            "ok": False,
            "required": [],
            "domain_packs": [],
            "optional_readiness": [],
        }
        with mock.patch.object(
            module,
            "check_runtime_environment",
            return_value=report,
        ):
            code = module.main(
                [
                    "--research-root",
                    str(self.root),
                    "--json-out",
                    str(output),
                ]
            )

        self.assertEqual(1, code)
        self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))

    def test_json_output_never_overwrites_or_writes_protected_and_source_roots(
        self,
    ) -> None:
        module = load_tool()
        report = {
            "schema": "cv-experiment-workflow.runtime-environment.v1",
            "ok": False,
            "required": [],
            "domain_packs": [],
            "optional_readiness": [],
        }
        existing = Path(self.temporary.name) / "existing.json"
        existing.write_text("用户文件", encoding="utf-8")
        protected_root = Path(self.temporary.name) / "旧 sample_project"
        protected = protected_root / "codex-release-environment-never-write.json"
        inside_source = self.root / "environment.json"
        self.assertFalse(hasattr(module, "PROTECTED_ROOTS"))

        with mock.patch.object(
            module,
            "check_runtime_environment",
            return_value=report,
        ):
            for target in (existing, protected, inside_source):
                with self.subTest(target=target):
                    code = module.main(
                        [
                            "--research-root",
                            str(self.root),
                            "--json-out",
                            str(target),
                            "--protected-root",
                            str(protected_root),
                        ]
                    )
                    self.assertEqual(1, code)

        self.assertEqual("用户文件", existing.read_text(encoding="utf-8"))
        self.assertFalse(inside_source.exists())
        self.assertFalse(os.path.lexists(protected))


if __name__ == "__main__":
    unittest.main()
