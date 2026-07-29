from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tests._helpers import ROOT, SCRIPTS


PACK_ROOT = (
    ROOT
    / "skills"
    / "cv-experiment-workflow"
    / "assets"
    / "domain-packs"
)


def _load_smoke_tool():
    path = ROOT / "tools" / "smoke_six_domain_packs.py"
    specification = importlib.util.spec_from_file_location(
        "smoke_six_domain_packs",
        path,
    )
    if specification is None or specification.loader is None:
        raise AssertionError("无法加载六方向冒烟工具")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _load_adapter(direction: str):
    matches = sorted(PACK_ROOT.glob(f"{direction}-v*/payload/workflow_adapter.py"))
    if len(matches) != 1:
        raise AssertionError(f"{direction} Adapter 数量不是 1：{matches}")
    module = types.ModuleType(f"_gpu_default_{direction}")
    module.__file__ = str(matches[0])
    exec(
        compile(matches[0].read_bytes(), str(matches[0]), "exec"),
        module.__dict__,
    )
    return module


def _fake_central_cuda_attestation(
    *,
    gate: dict[str, object],
    adapter: dict[str, str],
) -> dict[str, object]:
    from workflow_core import run_identity

    environment = run_identity.live_environment_snapshot()
    template_id = str(gate.get("template_id") or "PACK-CLS")
    template_version = str(gate.get("template_version") or "1.0.0")
    return {
        "schema": run_identity.CUDA_ATTESTATION_SCHEMA,
        "verified_by": run_identity.CUDA_ATTESTATION_VERIFIER,
        "template_id": template_id,
        "template_version": template_version,
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


class SixDomainPacksSmokeTests(unittest.TestCase):
    def test_lineage_fixture_commit_uses_only_command_scoped_identity(
        self,
    ) -> None:
        tool = _load_smoke_tool()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "empty-home"
            repository = root / "repository"
            home.mkdir()
            repository.mkdir()
            isolated_environment = {
                key: value
                for key, value in os.environ.items()
                if key.upper()
                in {
                    "COMSPEC",
                    "PATH",
                    "PATHEXT",
                    "SYSTEMDRIVE",
                    "SYSTEMROOT",
                    "WINDIR",
                }
            }
            isolated_environment.update(
                {
                    "HOME": str(home),
                    "USERPROFILE": str(home),
                    "HOMEDRIVE": home.drive,
                    "HOMEPATH": str(home)[len(home.drive) :],
                    "APPDATA": str(home / "AppData" / "Roaming"),
                    "LOCALAPPDATA": str(home / "AppData" / "Local"),
                    "GIT_CONFIG_NOSYSTEM": "1",
                }
            )

            with mock.patch.dict(
                os.environ,
                isolated_environment,
                clear=True,
            ):
                tool._git_text(repository, "init")
                global_before = subprocess.run(
                    ["git", "config", "--global", "--list"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                fixture = repository / "fixture.json"
                fixture.write_text("{}\n", encoding="utf-8")
                tool._git_text(repository, "add", "--", fixture.name)

                tool._commit_test_fixture(
                    repository,
                    "test: isolated lineage fixture",
                )

                identity = tool._git_text(
                    repository,
                    "show",
                    "-s",
                    "--format=%an%n%ae%n%cn%n%ce",
                    "HEAD",
                ).splitlines()
                self.assertEqual(
                    [
                        "CV-Workflow-Test",
                        "cv-workflow-test@example.invalid",
                        "CV-Workflow-Test",
                        "cv-workflow-test@example.invalid",
                    ],
                    identity,
                )
                local_config = tool._git_text(
                    repository,
                    "config",
                    "--local",
                    "--list",
                ).splitlines()
                self.assertFalse(
                    any(
                        entry.lower().startswith(("user.name=", "user.email="))
                        for entry in local_config
                    )
                )
                global_after = subprocess.run(
                    ["git", "config", "--global", "--list"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                self.assertEqual(
                    (
                        global_before.returncode,
                        global_before.stdout,
                        global_before.stderr,
                    ),
                    (
                        global_after.returncode,
                        global_after.stdout,
                        global_after.stderr,
                    ),
                )

                fixture.write_text('{"changed": true}\n', encoding="utf-8")
                tool._git_text(repository, "add", "--", fixture.name)
                with self.assertRaisesRegex(
                    ValueError,
                    "Author identity unknown|unable to auto-detect",
                ):
                    tool._git_text(
                        repository,
                        "commit",
                        "-m",
                        "ordinary commit must stay unconfigured",
                    )

    def test_custom_adapter_cannot_self_attest_cuda_for_formal_evidence(
        self,
    ) -> None:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        from workflow_core import run_identity

        binding = {
            "codebase_id": "CB-0001",
            "branch": "experiment/custom-cuda-claim",
            "commit": "a" * 40,
            "tag": None,
        }
        gate = {
            **binding,
            "repo_path": str(ROOT),
            "worktree_clean": True,
            "source": "manual",
            "template_id": None,
            "template_version": None,
        }
        variant = {
            "code": {"template_id": "CUSTOM", "version": "1"},
            "config": {"device": "cuda"},
            "seed": 7,
            "data": {
                "kind": "real_fixture",
                "dataset_identity": {
                    "schema": (
                        "cv-experiment-workflow.dataset-identity.v1"
                    ),
                    "dataset_id": "custom-cuda-claim",
                    "version": "1",
                    "source_uri": "https://example.org/custom-cuda-claim",
                    "manifest_sha256": "sha256:" + "c" * 64,
                    "split": "validation",
                },
                "evaluation": {"primary_metric": "fixture_score"},
            },
            "environment": {"backend": "custom", "device": "cuda"},
        }
        with mock.patch.object(
            run_identity,
            "committed_requirements_locks",
            return_value={},
        ):
            with self.assertRaisesRegex(
                ValueError,
                "受信任|trusted|CUDA.*验证",
            ):
                run_identity.build_bound_frozen(
                    variant,
                    binding=binding,
                    gate=gate,
                    adapter={"sha256": "b" * 64},
                    purpose="evidence",
                )

    def test_central_cuda_attestation_rejects_untrusted_adapter_and_no_gpu(
        self,
    ) -> None:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        from workflow_core import run_identity
        from workflow_core.domain_packs import (
            resolve_domain_pack,
            validate_domain_pack,
        )

        manifest = validate_domain_pack(resolve_domain_pack("cls@1.0.0"))
        adapter_sha256 = next(
            item["sha256"]
            for item in manifest["files"]
            if item["path"] == "workflow_adapter.py"
        )
        gate = {
            "source": "domain_pack",
            "template_id": "PACK-CLS",
            "template_version": "1.0.0",
        }
        with self.assertRaisesRegex(ValueError, "Adapter.*不匹配"):
            run_identity.create_central_cuda_attestation(
                gate=gate,
                adapter={"sha256": "b" * 64},
            )

        fake_torch = types.SimpleNamespace(
            cuda=types.SimpleNamespace(
                is_available=lambda: False,
                device_count=lambda: 0,
            )
        )
        with (
            mock.patch.dict(sys.modules, {"torch": fake_torch}),
            self.assertRaisesRegex(ValueError, "未发现可用 GPU"),
        ):
            run_identity.create_central_cuda_attestation(
                gate=gate,
                adapter={"sha256": adapter_sha256},
            )

    def test_all_six_real_data_packs_reject_cpu_as_formal_evidence(
        self,
    ) -> None:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        from workflow_core import run_identity

        binding = {
            "codebase_id": "CB-0001",
            "branch": "experiment/gpu-evidence",
            "commit": "a" * 40,
            "tag": None,
        }
        gate = {
            **binding,
            "repo_path": str(ROOT),
            "worktree_clean": True,
        }
        adapter = {"sha256": "b" * 64}
        dataset_identity = {
            "schema": "cv-experiment-workflow.dataset-identity.v1",
            "dataset_id": "tiny-real-fixture",
            "version": "1",
            "source_uri": "https://example.org/tiny-real-fixture",
            "manifest_sha256": "sha256:" + "c" * 64,
            "split": "train",
        }

        with (
            mock.patch.object(
                run_identity,
                "committed_requirements_locks",
                return_value={},
            ),
            mock.patch.object(
                run_identity,
                "create_central_cuda_attestation",
                side_effect=_fake_central_cuda_attestation,
            ),
        ):
            for pack_id in (
                "PACK-CLS",
                "PACK-DET",
                "PACK-GZSL",
                "PACK-INSTSEG",
                "PACK-SEG",
                "PACK-SR",
            ):
                gate["source"] = "domain_pack"
                gate["template_id"] = pack_id
                pack_version = "1.1.1" if pack_id == "PACK-GZSL" else "1.0.0"
                gate["template_version"] = pack_version
                variant = {
                    "code": {"template_id": pack_id, "version": pack_version},
                    "config": {"mode": "local_fixture", "device": "cpu"},
                    "seed": 7,
                    "data": {
                        "kind": "real_fixture",
                        "dataset_identity": dataset_identity,
                        "evaluation": {"primary_metric": "fixture_score"},
                    },
                    "environment": {"backend": "project", "device": "cpu"},
                }
                with self.subTest(pack_id=pack_id, purpose="evidence"):
                    with self.assertRaisesRegex(
                        ValueError,
                        "CUDA|GPU|cpu.*debug",
                    ):
                        run_identity.build_bound_frozen(
                            variant,
                            binding=binding,
                            gate=gate,
                            adapter=adapter,
                            purpose="evidence",
                        )
                with self.subTest(pack_id=pack_id, purpose="debug"):
                    debug = run_identity.build_bound_frozen(
                        variant,
                        binding=binding,
                        gate=gate,
                        adapter=adapter,
                        purpose="debug",
                    )
                    self.assertEqual(
                        "real_experiment",
                        debug["data"]["run_kind"],
                    )
                    self.assertFalse(debug["data"]["paper_eligible"])
                with self.subTest(pack_id=pack_id, purpose="persisted-gate"):
                    forged_cpu_evidence = deepcopy(debug)
                    forged_cpu_evidence["code"]["clean_required"] = True
                    forged_cpu_evidence["data"]["paper_eligible"] = True
                    with self.assertRaisesRegex(
                        ValueError,
                        "CUDA|GPU|cpu.*debug",
                    ):
                        run_identity.validate_bound_frozen(
                            forged_cpu_evidence,
                            purpose="evidence",
                        )
                with self.subTest(
                    pack_id=pack_id,
                    purpose="template-id-spoof-gate",
                ):
                    template_spoof = deepcopy(forged_cpu_evidence)
                    template_spoof["code"]["declared_code"][
                        "template_id"
                    ] = "CUSTOM"
                    with self.assertRaisesRegex(
                        ValueError,
                        "CUDA|GPU|cpu.*debug",
                    ):
                        run_identity.validate_bound_frozen(
                            template_spoof,
                            purpose="evidence",
                        )
                with self.subTest(
                    pack_id=pack_id,
                    purpose="missing-device-spoof-gate",
                ):
                    missing_device_spoof = deepcopy(template_spoof)
                    missing_device_spoof["config"].pop("device")
                    missing_device_spoof["environment"][
                        "declared_environment"
                    ].pop("device")
                    fingerprints = missing_device_spoof["code"][
                        "fingerprints"
                    ]
                    fingerprints["config"] = (
                        run_identity.canonical_fingerprint(
                            missing_device_spoof["config"]
                        )
                    )
                    fingerprints["environment"] = (
                        run_identity.canonical_fingerprint(
                            missing_device_spoof["environment"]
                        )
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        "CUDA|GPU|cpu.*debug",
                    ):
                        run_identity.validate_bound_frozen(
                            missing_device_spoof,
                            purpose="evidence",
                        )
                with self.subTest(pack_id=pack_id, purpose="cuda-evidence"):
                    cuda_variant = deepcopy(variant)
                    cuda_variant["config"]["device"] = "cuda"
                    cuda_variant["environment"]["device"] = "cuda"
                    evidence = run_identity.build_bound_frozen(
                        cuda_variant,
                        binding=binding,
                        gate=gate,
                        adapter=adapter,
                        purpose="evidence",
                    )
                    self.assertEqual(
                        "real_experiment",
                        evidence["data"]["run_kind"],
                    )
                    self.assertTrue(evidence["data"]["paper_eligible"])
                with self.subTest(
                    pack_id=pack_id,
                    purpose="cuda-attestation-tamper",
                ):
                    tampered = deepcopy(evidence)
                    tampered["environment"][
                        "central_cuda_attestation"
                    ]["probe"]["result"] = 7.0
                    tampered["code"]["fingerprints"]["environment"] = (
                        run_identity.canonical_fingerprint(
                            tampered["environment"]
                        )
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        "CUDA.*真算子",
                    ):
                        run_identity.validate_bound_frozen(
                            tampered,
                            purpose="evidence",
                        )

    def test_branch_run_lineage_contract_keeps_code_and_execution_separate(
        self,
    ) -> None:
        tool = _load_smoke_tool()
        base_commit = "a" * 40
        proof = {
            "schema": "cv-experiment-workflow.branch-run-lineage-smoke.v1",
            "repository": "repositories/smoke-cls",
            "codebase_id": "CB-0001",
            "base_tag": "domain-pack/cls/v1.0.0",
            "base_commit": base_commit,
            "idea_tree": {
                "root_idea_id": "IDEA-0001",
                "branch_idea_ids": ["IDEA-0002", "IDEA-0003"],
                "shared_parent_verified": True,
            },
            "branches": [
                {
                    "branch": "experiment/idea-a",
                    "commit": "b" * 40,
                    "parent_commit": base_commit,
                    "source_tag": "domain-pack/cls/v1.0.0",
                    "idea_id": "IDEA-0002",
                    "git_worktree_clean": True,
                    "runs": [
                        {
                            "id": "RUN-0001",
                            "task_id": "TASK-0001",
                            "purpose": "debug",
                            "branch": "experiment/idea-a",
                            "commit": "b" * 40,
                            "execution_stage": "closed",
                            "outcome": "succeeded",
                            "evidence_level": "debug",
                            "run_kind": "synthetic_debug_only",
                            "paper_eligible": False,
                        },
                        {
                            "id": "RUN-0002",
                            "task_id": "TASK-0002",
                            "purpose": "debug",
                            "branch": "experiment/idea-a",
                            "commit": "b" * 40,
                            "execution_stage": "closed",
                            "outcome": "succeeded",
                            "evidence_level": "debug",
                            "run_kind": "synthetic_debug_only",
                            "paper_eligible": False,
                        },
                    ],
                },
                {
                    "branch": "experiment/idea-b",
                    "commit": "c" * 40,
                    "parent_commit": base_commit,
                    "source_tag": "domain-pack/cls/v1.0.0",
                    "idea_id": "IDEA-0003",
                    "git_worktree_clean": True,
                    "runs": [
                        {
                            "id": "RUN-0003",
                            "task_id": "TASK-0003",
                            "purpose": "debug",
                            "branch": "experiment/idea-b",
                            "commit": "c" * 40,
                            "execution_stage": "closed",
                            "outcome": "succeeded",
                            "evidence_level": "debug",
                            "run_kind": "synthetic_debug_only",
                            "paper_eligible": False,
                        },
                        {
                            "id": "RUN-0004",
                            "task_id": "TASK-0004",
                            "purpose": "debug",
                            "branch": "experiment/idea-b",
                            "commit": "c" * 40,
                            "execution_stage": "closed",
                            "outcome": "succeeded",
                            "evidence_level": "debug",
                            "run_kind": "synthetic_debug_only",
                            "paper_eligible": False,
                        },
                    ],
                },
            ],
        }

        self.assertTrue(tool._validate_lineage_proof(proof))

        conflated = deepcopy(proof)
        conflated["branches"][1]["runs"][0]["id"] = "RUN-0001"
        with self.assertRaisesRegex(ValueError, "Run"):
            tool._validate_lineage_proof(conflated)

    def test_gzsl_is_cuda_only_and_pending_packs_are_not_published(
        self,
    ) -> None:
        for direction in ("cls", "det", "instseg", "seg", "sr", "gzsl"):
            payload = next(PACK_ROOT.glob(f"{direction}-v*/payload"))
            with self.subTest(direction=direction, config="baseline"):
                baseline = json.loads(
                    (payload / "configs" / "baseline.json").read_text(
                        encoding="utf-8"
                    )
                )
                smoke = json.loads(
                    (payload / "configs" / "smoke.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual("cuda", baseline["device"])
                readme = (payload / "README.md").read_text(encoding="utf-8")
                if direction == "gzsl":
                    self.assertEqual("cuda", smoke["device"])
                    self.assertIn("固定使用 `dvsr_gpu` 和 CUDA", readme)
                    self.assertIn("没有 CPU 退路", readme)
                else:
                    self.assertIn("尚未开放", readme)
                    self.assertIn("不能创建仓库或运行实验", readme)
                    self.assertNotIn("python ", readme.lower())

        cls = _load_adapter("cls")
        cls_config = {
            "mode": "local_imagefolder",
            "data_root": r"D:\tiny-cls",
            "dataset_id": "tiny-cls",
            "version": "1",
            "source_uri": "https://example.org/tiny-cls",
        }
        with mock.patch.object(
            cls,
            "_dataset_manifest_sha256",
            return_value="sha256:" + "1" * 64,
        ):
            cls_default = cls.prepare_runs(
                Path("."),
                {"route_inputs": {"config": cls_config}},
            )[0]
            cls_cpu = cls.prepare_runs(
                Path("."),
                {
                    "route_inputs": {
                        "config": {**cls_config, "device": "cpu"},
                    }
                },
            )[0]
        self.assertEqual("cuda", cls_default["config"]["device"])
        self.assertEqual(
            {"backend": "project", "device": "cuda"},
            cls_default["environment"],
        )
        self.assertEqual("cpu", cls_cpu["config"]["device"])

        coco_config = {
            "mode": "local_coco",
            "data_root": r"D:\tiny-coco\images",
            "annotation_file": r"D:\tiny-coco\instances.json",
            "dataset_id": "tiny-coco",
            "version": "1",
            "source_uri": "https://example.org/tiny-coco",
            "split": "val",
        }
        for direction, seed in (("det", 17), ("instseg", 23)):
            adapter = _load_adapter(direction)
            with self.subTest(direction=direction, config="adapter-default"):
                default, mode = adapter._validated_config(
                    coco_config,
                    seed,
                    require_metric_definition=False,
                )
                explicit_cpu, _ = adapter._validated_config(
                    {**coco_config, "device": "cpu"},
                    seed,
                    require_metric_definition=False,
                )
                self.assertEqual("cuda", default["device"])
                self.assertEqual(
                    "cuda",
                    adapter._device_for(default, mode),
                )
                self.assertEqual("cpu", explicit_cpu["device"])

        formal_configs = {
            "seg": {
                "mode": "local_segmentation",
                "data_root": r"D:\tiny-seg",
                "dataset_id": "tiny-seg",
                "version": "1",
                "source_uri": "https://example.org/tiny-seg",
                "manifest_sha256": "2" * 64,
            },
            "sr": {
                "mode": "local_sr_x2",
                "data_root": r"D:\tiny-sr",
                "dataset_id": "tiny-sr",
                "version": "1",
                "source_uri": "https://example.org/tiny-sr",
                "manifest_sha256": "3" * 64,
            },
            "gzsl": {
                "mode": "local_gzsl_npz",
                "data_path": r"D:\tiny-gzsl\data.npz",
                "dataset_id": "tiny-gzsl",
                "version": "1",
                "source_uri": "https://example.org/tiny-gzsl",
                "manifest_sha256": "4" * 64,
            },
        }
        normalizers = {
            "seg": "_normalize_config",
            "sr": "_config",
            "gzsl": "_config",
        }
        for direction, raw_config in formal_configs.items():
            adapter = _load_adapter(direction)
            normalize = getattr(adapter, normalizers[direction])
            with self.subTest(direction=direction, config="adapter-default"):
                default = normalize(
                    raw_config,
                    require_metric_definition=False,
                )
                self.assertEqual("cuda", default["device"])
                if direction == "gzsl":
                    with self.assertRaisesRegex(
                        ValueError,
                        "固定使用 cuda|不支持 CPU fallback",
                    ):
                        normalize(
                            {**raw_config, "device": "cpu"},
                            require_metric_definition=False,
                        )
                else:
                    explicit_cpu = normalize(
                        {**raw_config, "device": "cpu"},
                        require_metric_definition=False,
                    )
                    self.assertEqual("cpu", explicit_cpu["device"])

    def test_all_manifests_are_checked_without_running_cuda_and_only_gzsl_is_ready(
        self,
    ) -> None:
        tool = _load_smoke_tool()
        with tempfile.TemporaryDirectory() as temporary:
            artifacts_root = Path(temporary) / "artifacts"
            report = tool.run_six_domain_packs(artifacts_root=artifacts_root)

            self.assertEqual(
                "cv-experiment-workflow.six-domain-pack-smoke.v1",
                report["schema"],
            )
            self.assertEqual(
                ["cls", "det", "gzsl", "instseg", "seg", "sr"],
                [item["direction"] for item in report["directions"]],
            )
            self.assertEqual("static_manifest_validation", report["mode"])
            self.assertEqual("pass", report["summary"]["status"])
            self.assertEqual(1, report["summary"]["ready"])
            self.assertEqual(5, report["summary"]["pending"])
            self.assertTrue((artifacts_root / "six_domain_packs_report.json").is_file())
            self.assertEqual(
                report,
                json.loads(
                    (artifacts_root / "six_domain_packs_report.json").read_text(
                        encoding="utf-8"
                    )
                ),
            )
            for item in report["directions"]:
                with self.subTest(direction=item["direction"]):
                    expected = "ready" if item["direction"] == "gzsl" else "pending"
                    device = "cuda" if item["direction"] == "gzsl" else "cpu"
                    self.assertEqual(expected, item["status"])
                    self.assertEqual(device, item["device"])
                    self.assertFalse(item["paper_eligible"])
                    self.assertNotIn("commands", item)
            gzsl = next(
                item for item in report["directions"] if item["direction"] == "gzsl"
            )
            self.assertEqual("1.1.1", gzsl["pack"]["version"])
            self.assertFalse(gzsl["cpu_fallback"])
            self.assertFalse(hasattr(tool, "_run_command"))

    def test_gzsl_cpu_is_rejected_before_any_command_starts(self) -> None:
        tool = _load_smoke_tool()
        with self.assertRaisesRegex(ValueError, "GZSL.*CPU|CPU.*GZSL"):
            tool.run_six_domain_packs(device="cpu")
        self.assertFalse(hasattr(tool, "_run_command"))


if __name__ == "__main__":
    unittest.main()
