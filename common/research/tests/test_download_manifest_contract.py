from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from tests._helpers import SCRIPTS


sys.path.insert(0, str(SCRIPTS))

from workflow_core.domain_packs import (  # noqa: E402
    DOWNLOAD_MANIFEST_MAX_BYTES,
    discover_domain_packs,
    resolve_domain_pack,
    validate_domain_pack,
)


VALID_DOWNLOADS = {
    "schema": "cv-experiment-workflow.download-manifest.v1",
    "automatic_download": False,
    "bundled_large_files": [],
    "optional_resources": [
        {
            "name": "PyTorch 2.12.0 固定发布页",
            "kind": "dependency_release_page",
            "url": "https://github.com/pytorch/pytorch/tree/v2.12.0",
            "version": "2.12.0",
            "revision": "v2.12.0",
            "license": "BSD-3-Clause",
            "license_url": (
                "https://github.com/pytorch/pytorch/blob/"
                "v2.12.0/LICENSE"
            ),
            "sha256": None,
        },
        {
            "name": "离线示例制品",
            "kind": "direct_artifact",
            "url": "https://example.org/releases/example-v1.bin",
            "version": "1.0.0",
            "revision": "example-v1",
            "license": "CC0-1.0",
            "license_url": (
                "https://creativecommons.org/publicdomain/zero/1.0/"
            ),
            "sha256": hashlib.sha256(b"example-v1").hexdigest(),
        },
    ],
}


class DownloadManifestContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pack = self.root / "pack"
        shutil.copytree(resolve_domain_pack("cls"), self.pack)

    def _replace_downloads(self, payload: object) -> None:
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=False,
            )
            + "\n"
        ).encode("utf-8")
        self._replace_download_bytes(encoded)

    def _replace_download_bytes(self, encoded: bytes) -> None:
        downloads = self.pack / "payload" / "DOWNLOADS.json"
        downloads.write_bytes(encoded)

        manifest_path = self.pack / "pack.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        record = next(
            item
            for item in manifest["files"]
            if item["path"] == "DOWNLOADS.json"
        )
        record["size"] = len(encoded)
        record["sha256"] = hashlib.sha256(encoded).hexdigest()
        manifest_path.write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def test_accepts_fixed_pages_and_hashed_direct_artifacts(self) -> None:
        self._replace_downloads(VALID_DOWNLOADS)
        validate_domain_pack(self.pack)

    def test_all_six_builtin_packs_use_the_same_exact_download_contract(
        self,
    ) -> None:
        expected_top = {
            "schema",
            "automatic_download",
            "bundled_large_files",
            "optional_resources",
        }
        expected_resource = {
            "name",
            "kind",
            "url",
            "version",
            "revision",
            "license",
            "license_url",
            "sha256",
        }
        expected_ids = ["cls", "det", "gzsl", "instseg", "seg", "sr"]
        discovered = discover_domain_packs()
        self.assertEqual(expected_ids, [item["id"] for item in discovered])

        for pack_id in expected_ids:
            with self.subTest(pack=pack_id):
                pack = resolve_domain_pack(pack_id)
                validate_domain_pack(pack)
                payload = json.loads(
                    (pack / "payload" / "DOWNLOADS.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(expected_top, set(payload))
                self.assertIs(payload["automatic_download"], False)
                self.assertEqual([], payload["bundled_large_files"])
                self.assertGreaterEqual(len(payload["optional_resources"]), 1)
                for resource in payload["optional_resources"]:
                    self.assertEqual(expected_resource, set(resource))
                    self.assertIn(
                        resource["kind"],
                        {
                            "dependency_release_page",
                            "dataset_release_page",
                            "direct_artifact",
                        },
                    )

    def test_rejects_automatic_or_bundled_large_downloads(self) -> None:
        attacks = []
        automatic = deepcopy(VALID_DOWNLOADS)
        automatic["automatic_download"] = True
        attacks.append(automatic)
        bundled = deepcopy(VALID_DOWNLOADS)
        bundled["bundled_large_files"] = ["weights/model.pt"]
        attacks.append(bundled)

        for payload in attacks:
            with self.subTest(payload=payload):
                self._replace_downloads(payload)
                with self.assertRaisesRegex(
                    ValueError,
                    "自动下载|automatic_download|大文件|bundled",
                ):
                    validate_domain_pack(self.pack)

    def test_rejects_floating_or_unverifiable_resources(self) -> None:
        attacks = []
        for field, value in (
            ("version", "latest"),
            ("revision", "main"),
            ("url", "http://example.org/data"),
            ("url", "https://example.org/releases/latest"),
            ("url", "https://example.org/releases/latest.json"),
            ("url", "https://example.org/releases/model-latest.zip"),
            ("url", "https://example.org/downloads"),
            ("url", "https://example.org/releases/v1?ref=main"),
            ("url", "https://%65xample.org/releases/v1.0.0"),
            ("url", "https://example.org/%252e%252e/private"),
            ("license_url", "https://user:secret@example.org/license"),
            ("license_url", "https://example.org/license?token=secret"),
            ("sha256", "not-a-sha"),
        ):
            payload = deepcopy(VALID_DOWNLOADS)
            payload["optional_resources"][0][field] = value
            attacks.append((field, payload))

        no_direct_hash = deepcopy(VALID_DOWNLOADS)
        no_direct_hash["optional_resources"][1]["sha256"] = None
        attacks.append(("direct hash", no_direct_hash))

        for label, payload in attacks:
            with self.subTest(label=label):
                self._replace_downloads(payload)
                with self.assertRaisesRegex(
                    ValueError,
                    "下载|资源|URL|版本|revision|sha256|摘要",
                ):
                    validate_domain_pack(self.pack)

    def test_rejects_oversize_download_manifest_before_parsing(self) -> None:
        encoded = (
            json.dumps(VALID_DOWNLOADS, ensure_ascii=False).encode("utf-8")
            + b" " * DOWNLOAD_MANIFEST_MAX_BYTES
        )
        self.assertGreater(len(encoded), DOWNLOAD_MANIFEST_MAX_BYTES)
        self._replace_download_bytes(encoded)

        with self.assertRaisesRegex(ValueError, "DOWNLOADS|上限|体积"):
            validate_domain_pack(self.pack)

    def test_rejects_unknown_fields_and_noncanonical_shapes(self) -> None:
        attacks = []
        top_extra = deepcopy(VALID_DOWNLOADS)
        top_extra["implementation_note"] = "说明应写入 README"
        attacks.append(top_extra)
        resource_extra = deepcopy(VALID_DOWNLOADS)
        resource_extra["optional_resources"][0]["note"] = "extra"
        attacks.append(resource_extra)
        empty_resources = deepcopy(VALID_DOWNLOADS)
        empty_resources["optional_resources"] = []
        attacks.append(empty_resources)

        for payload in attacks:
            with self.subTest(payload=payload):
                self._replace_downloads(payload)
                with self.assertRaisesRegex(
                    ValueError,
                    "下载|字段|资源|1..64",
                ):
                    validate_domain_pack(self.pack)


if __name__ == "__main__":
    unittest.main()
