from __future__ import annotations

import copy
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKS = {
    "det": ("det-v1.0.0", "synthetic_debug"),
    "seg": ("seg-v1.0.0", "synthetic_smoke"),
    "instseg": ("instseg-v1.0.0", "synthetic_debug"),
    "sr": ("sr-v1.0.0", "synthetic_smoke"),
    "gzsl": ("gzsl-v1.1.1", "synthetic_smoke"),
}
BOUND_CODE_SCHEMA = "cv-experiment-workflow.bound-code.v2"


class DomainPackBoundCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _payload(pack_directory: str) -> Path:
        return (
            ROOT
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "domain-packs"
            / pack_directory
            / "payload"
        )

    @classmethod
    def _load_adapter(
        cls,
        pack_id: str,
        pack_directory: str,
    ) -> tuple[types.ModuleType, Path]:
        payload = cls._payload(pack_directory)
        source = payload / "workflow_adapter.py"
        module = types.ModuleType(f"_bound_compat_{pack_id}")
        module.__file__ = str(source)
        exec(compile(source.read_bytes(), str(source), "exec"), module.__dict__)
        return module, payload

    @staticmethod
    def _bound_variant(
        variant: dict[str, object],
        *,
        purpose: str = "debug",
    ) -> dict[str, object]:
        frozen = copy.deepcopy(variant)
        clean_required = purpose == "evidence"
        frozen["code"] = {
            "schema": BOUND_CODE_SCHEMA,
            "clean_required": clean_required,
            "worktree_clean": True,
            "declared_code": copy.deepcopy(variant["code"]),
        }
        frozen["environment"] = {
            "declared_environment": copy.deepcopy(variant["environment"]),
        }
        data = frozen["data"]
        assert isinstance(data, dict)
        data["run_kind"] = (
            "synthetic_debug_only"
            if data["kind"] == "synthetic_debug_only"
            else "real_experiment"
        )
        data["paper_eligible"] = bool(
            clean_required and data["run_kind"] == "real_experiment"
        )
        return frozen

    def _synthetic(
        self,
        pack_id: str,
    ) -> tuple[types.ModuleType, Path, dict[str, object]]:
        pack_directory, mode = PACKS[pack_id]
        adapter, payload = self._load_adapter(pack_id, pack_directory)
        variant = adapter.prepare_runs(
            payload,
            {
                "route_inputs": {
                    "config": {"mode": mode},
                    "seed": 17,
                },
            },
        )[0]
        return adapter, payload, variant

    def _formal(
        self,
        pack_id: str,
    ) -> tuple[
        types.ModuleType,
        dict[str, object],
        dict[str, object],
    ]:
        pack_directory, _mode = PACKS[pack_id]
        adapter, payload = self._load_adapter(pack_id, pack_directory)
        raw_digest = "0" * 64
        if pack_id in {"det", "instseg"}:
            template_id = "PACK-DET" if pack_id == "det" else "PACK-INSTSEG"
            config = {
                "mode": "local_coco",
                "device": "cpu",
                "data_root": "dataset/images",
                "annotation_file": "dataset/instances.json",
                "dataset_id": f"tiny-{pack_id}",
                "version": "1",
                "source_uri": f"https://example.org/{pack_id}",
                "split": "val",
                "metric_definition": copy.deepcopy(
                    adapter._FORMAL_METRIC_DEFINITION
                ),
            }
            identity = {
                "schema": "cv-experiment-workflow.dataset-identity.v1",
                "dataset_id": f"tiny-{pack_id}",
                "version": "1",
                "source_uri": f"https://example.org/{pack_id}",
                "manifest_sha256": f"sha256:{raw_digest}",
                "split": "val",
            }
            central = {
                "code": {"template_id": template_id, "version": "1.0.0"},
                "config": config,
                "seed": 17,
                "data": {
                    "kind": "local_dataset",
                    "dataset_identity": identity,
                    "evaluation": copy.deepcopy(adapter._FORMAL_EVALUATION),
                },
                "environment": {"backend": "project", "device": "cpu"},
            }
            legacy = copy.deepcopy(central)
            legacy["data"]["kind"] = "local_coco"
            return adapter, central, legacy

        configs = {
            "seg": {
                "mode": "local_segmentation",
                "data_root": "dataset",
                "dataset_id": "tiny-seg",
                "version": "1",
                "source_uri": "https://example.org/seg",
                "manifest_sha256": raw_digest,
            },
            "sr": {
                "mode": "local_sr_x2",
                "data_root": "dataset",
                "dataset_id": "tiny-sr",
                "version": "1",
                "source_uri": "https://example.org/sr",
                "manifest_sha256": raw_digest,
            },
            "gzsl": {
                "mode": "local_gzsl_npz",
                "data_path": "dataset/formal.npz",
                "dataset_id": "tiny-gzsl",
                "version": "1",
                "source_uri": "https://example.org/gzsl",
                "manifest_sha256": raw_digest,
            },
        }
        central = adapter.prepare_runs(
            payload,
            {
                "route_inputs": {
                    "config": configs[pack_id],
                    "seed": 17,
                },
            },
        )[0]
        legacy = copy.deepcopy(central)
        identity_factory = getattr(adapter, "_domain_data_identity", None)
        if identity_factory is None:
            identity_factory = adapter._data_identity
        legacy["data"] = {
            "identity": identity_factory(central["config"]),
            "evaluation": copy.deepcopy(central["data"]["evaluation"]),
        }
        return adapter, central, legacy

    @staticmethod
    def _identity(
        adapter: types.ModuleType,
        run: dict[str, object],
    ) -> dict[str, object]:
        validator = getattr(adapter, "_identity", None)
        if validator is None:
            validator = adapter._run_identity
        return validator(run)

    def test_synthetic_direct_and_bound_frozen_are_both_accepted(self) -> None:
        for pack_id in PACKS:
            with self.subTest(pack_id=pack_id):
                adapter, _payload, variant = self._synthetic(pack_id)
                direct = {
                    "id": "RUN-0001",
                    "purpose": "debug",
                    "frozen": copy.deepcopy(variant),
                }
                bound = {
                    "id": "RUN-0002",
                    "purpose": "debug",
                    "frozen": self._bound_variant(variant),
                }
                self.assertEqual("RUN-0001", self._identity(adapter, direct)["run_id"])
                self.assertEqual("RUN-0002", self._identity(adapter, bound)["run_id"])

    def test_bound_declared_contract_and_central_flags_fail_closed(self) -> None:
        for pack_id in PACKS:
            adapter, _payload, variant = self._synthetic(pack_id)
            baseline = self._bound_variant(variant)
            attacks: list[tuple[str, dict[str, object]]] = []

            wrong_code = copy.deepcopy(baseline)
            wrong_code["code"]["declared_code"]["version"] = "9.9.9"
            attacks.append(("declared_code", wrong_code))

            wrong_environment = copy.deepcopy(baseline)
            wrong_environment["environment"]["declared_environment"]["device"] = (
                "cpu"
                if pack_id == "gzsl"
                else "cuda"
            )
            attacks.append(("declared_environment", wrong_environment))

            wrong_kind = copy.deepcopy(baseline)
            wrong_kind["data"]["run_kind"] = "real_experiment"
            attacks.append(("run_kind", wrong_kind))

            wrong_eligibility = copy.deepcopy(baseline)
            wrong_eligibility["data"]["paper_eligible"] = True
            attacks.append(("paper_eligible", wrong_eligibility))

            missing_flag = copy.deepcopy(baseline)
            del missing_flag["data"]["paper_eligible"]
            attacks.append(("missing_paper_eligible", missing_flag))

            for attack, frozen in attacks:
                with self.subTest(pack_id=pack_id, attack=attack):
                    with self.assertRaisesRegex(
                        ValueError,
                        "code|environment|run_kind|paper_eligible|中央|声明|身份",
                    ):
                        self._identity(
                            adapter,
                            {
                                "id": "RUN-0003",
                                "purpose": "debug",
                                "frozen": frozen,
                            },
                        )

    def test_formal_bound_and_legacy_direct_contracts_are_both_accepted(
        self,
    ) -> None:
        for pack_id in PACKS:
            with self.subTest(pack_id=pack_id):
                adapter, central, legacy = self._formal(pack_id)
                central_run = {
                    "id": "RUN-0004",
                    "purpose": "evidence",
                    "frozen": central,
                }
                legacy_run = {
                    "id": "RUN-0005",
                    "purpose": "evidence",
                    "frozen": legacy,
                }
                bound_run = {
                    "id": "RUN-0006",
                    "purpose": "evidence",
                    "frozen": self._bound_variant(
                        central,
                        purpose="evidence",
                    ),
                }
                self.assertEqual(
                    "RUN-0004",
                    self._identity(adapter, central_run)["run_id"],
                )
                self.assertEqual(
                    "RUN-0005",
                    self._identity(adapter, legacy_run)["run_id"],
                )
                self.assertEqual(
                    "RUN-0006",
                    self._identity(adapter, bound_run)["run_id"],
                )
                attacked = copy.deepcopy(bound_run)
                attacked["frozen"]["data"]["paper_eligible"] = False
                with self.assertRaisesRegex(ValueError, "paper_eligible|中央"):
                    self._identity(adapter, attacked)
                if pack_id in {"det", "instseg"}:
                    for field, replacement in (
                        ("dataset_id", "other-dataset"),
                        ("version", "other-version"),
                        ("source_uri", "https://example.org/other"),
                        ("split", "train"),
                    ):
                        with self.subTest(
                            pack_id=pack_id,
                            mismatch=field,
                        ):
                            mismatch = copy.deepcopy(bound_run)
                            mismatch["frozen"]["data"]["dataset_identity"][
                                field
                            ] = replacement
                            with self.assertRaisesRegex(
                                ValueError,
                                "dataset_identity|config|身份|冲突",
                            ):
                                self._identity(adapter, mismatch)


if __name__ == "__main__":
    unittest.main()
