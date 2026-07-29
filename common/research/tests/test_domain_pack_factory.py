from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

from tests._helpers import SCRIPTS, read_json


sys.path.insert(0, str(SCRIPTS))

from workflow_core.project import init_project  # noqa: E402


DOMAIN_PACK_SCHEMA = "cv-experiment-workflow.domain-pack.v1"
FACTORY_MODULE = "workflow_core.domain_packs"
DOWNLOAD_FIXTURE = (
    json.dumps(
        {
            "schema": "cv-experiment-workflow.download-manifest.v1",
            "automatic_download": False,
            "bundled_large_files": [],
            "optional_resources": [{
                "name": "固定测试发布页",
                "kind": "source_release_page",
                "url": "https://example.com/releases/v1.0.0",
                "version": "1.0.0",
                "revision": "v1.0.0",
                "license": "MIT",
                "license_url": "https://example.com/licenses/MIT",
                "sha256": None,
            }],
        },
        ensure_ascii=False,
        indent=2,
    )
    + "\n"
).encode("utf-8")


class DomainPackFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _factory(self) -> ModuleType:
        try:
            return importlib.import_module(FACTORY_MODULE)
        except ModuleNotFoundError as error:
            if error.name == FACTORY_MODULE:
                self.fail("方向包工厂 workflow_core.domain_packs 尚未实现")
            raise

    def _materialize_for_test(
        self,
        factory: ModuleType,
        pack: Path,
        destination: Path,
        name: str,
    ) -> dict[str, object]:
        return factory._materialize_domain_repository_for_test(
            pack,
            destination,
            name,
        )

    def _new_pack(
        self,
        name: str,
        *,
        files: dict[str, bytes] | None = None,
        direction: str = "cls",
    ) -> Path:
        pack = self.root / name
        payload = pack / "payload"
        payload.mkdir(parents=True)
        selected_files = dict(files or {
            ".gitignore": b"__pycache__/\n*.pyc\n",
            "README.md": b"# Classification fixture\n",
            "src/train.py": b"print('fixture')\n",
        })
        selected_files.setdefault("DOWNLOADS.json", DOWNLOAD_FIXTURE)
        for relative, content in selected_files.items():
            target = payload.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self._rewrite_manifest(pack, direction=direction)
        return pack

    def _rewrite_manifest(
        self,
        pack: Path,
        *,
        direction: str = "cls",
    ) -> dict[str, object]:
        payload = pack / "payload"
        files = []
        for path in sorted(
            (entry for entry in payload.rglob("*") if entry.is_file()),
            key=lambda entry: entry.relative_to(payload).as_posix(),
        ):
            content = path.read_bytes()
            files.append({
                "path": path.relative_to(payload).as_posix(),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            })
        manifest: dict[str, object] = {
            "schema": DOMAIN_PACK_SCHEMA,
            "id": direction,
            "version": "1.0.0",
            "template_id": f"PACK-{direction.upper()}",
            "primary_direction": direction,
            "license": "MIT",
            "source_references": [{
                "name": "classification-fixture",
                "url": "https://example.com/classification-fixture.git",
                "revision": "1" * 40,
                "license": "MIT",
            }],
            "files": files,
        }
        (pack / "pack.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest

    def _manifest(self, pack: Path) -> dict[str, object]:
        return read_json(pack / "pack.json")

    def _write_manifest(
        self,
        pack: Path,
        manifest: dict[str, object],
    ) -> None:
        (pack / "pack.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _git(self, repository: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def test_valid_pack_is_strictly_validated_and_payload_is_copied(self) -> None:
        factory = self._factory()
        pack = self._new_pack("valid-pack")
        destination = self.root / "classification-repository"

        manifest = factory.validate_domain_pack(pack)
        repository = self._materialize_for_test(
            factory,
            pack,
            destination,
            "classification",
        )

        self.assertEqual(self._manifest(pack), manifest)
        self.assertEqual(str(destination.resolve()), repository["repo_path"])
        self.assertEqual("cls", repository["pack_id"])
        self.assertEqual("1.0.0", repository["pack_version"])
        self.assertEqual("PACK-CLS", repository["template_id"])
        self.assertEqual(
            b"# Classification fixture\n",
            (destination / "README.md").read_bytes(),
        )
        self.assertFalse((destination / "payload").exists())
        self.assertFalse((destination / "pack.json").exists())

    def test_pending_direction_cannot_materialize_without_test_bypass(self) -> None:
        factory = self._factory()
        pack = self._new_pack("pending-classification-pack")
        destination = self.root / "pending-classification-repository"

        with self.assertRaisesRegex(ValueError, "待做|pending|开放"):
            factory.materialize_domain_repository(
                pack,
                destination,
                "pending-classification",
            )

        self.assertFalse(destination.exists())

    def test_unknown_manifest_fields_unlisted_files_and_bad_hash_are_rejected(
        self,
    ) -> None:
        factory = self._factory()

        unknown = self._new_pack("unknown-field")
        unknown_manifest = self._manifest(unknown)
        unknown_manifest["unexpected"] = True
        self._write_manifest(unknown, unknown_manifest)
        with self.assertRaisesRegex(ValueError, "字段|unknown|unexpected"):
            factory.validate_domain_pack(unknown)

        unlisted = self._new_pack("unlisted-file")
        (unlisted / "payload" / "unlisted.txt").write_text(
            "not declared\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "files|清单|未声明|精确"):
            factory.validate_domain_pack(unlisted)

        bad_hash = self._new_pack("bad-hash")
        bad_hash_manifest = self._manifest(bad_hash)
        bad_hash_manifest["files"][0]["sha256"] = "0" * 64
        self._write_manifest(bad_hash, bad_hash_manifest)
        with self.assertRaisesRegex(ValueError, "sha256|hash|摘要"):
            factory.validate_domain_pack(bad_hash)

    def test_symlink_or_reparse_and_hardlink_payloads_are_rejected(self) -> None:
        factory = self._factory()

        linked = self._new_pack("linked-payload")
        link = linked / "payload" / "linked.py"
        try:
            link.symlink_to(linked / "payload" / "src" / "train.py")
        except OSError as error:
            link = None
            link_error = error
        if link is not None:
            # 工厂统一走 is_link_or_reparse；Windows symlink/junction/reparse
            # 使用同一拒绝分支。
            with self.assertRaisesRegex(ValueError, "link|reparse|链接"):
                factory.validate_domain_pack(linked)
        else:
            self.assertIsInstance(link_error, OSError)

        hardlinked = self._new_pack("hardlinked-payload")
        hardlink = hardlinked / "payload" / "train-copy.py"
        try:
            os.link(
                hardlinked / "payload" / "src" / "train.py",
                hardlink,
                follow_symlinks=False,
            )
        except OSError as error:
            self.skipTest(f"当前文件系统不能创建 hardlink：{error}")
        self._rewrite_manifest(hardlinked)
        with self.assertRaisesRegex(ValueError, "hardlink|硬链接"):
            factory.validate_domain_pack(hardlinked)

    def test_windows_reserved_names_and_case_collisions_are_rejected(self) -> None:
        factory = self._factory()

        reserved = self._new_pack("reserved-name")
        reserved_manifest = self._manifest(reserved)
        reserved_manifest["files"][0]["path"] = "CON.txt"
        self._write_manifest(reserved, reserved_manifest)
        with self.assertRaisesRegex(ValueError, "Windows|保留"):
            factory.validate_domain_pack(reserved)

        collision = self._new_pack("case-collision")
        collision_manifest = self._manifest(collision)
        readme = next(
            item
            for item in collision_manifest["files"]
            if item["path"] == "README.md"
        )
        collision_manifest["files"].append({
            **readme,
            "path": "readme.MD",
        })
        self._write_manifest(collision, collision_manifest)
        with self.assertRaisesRegex(ValueError, "大小写|冲突|重复"):
            factory.validate_domain_pack(collision)

        directory_collision = self._new_pack(
            "directory-case-collision",
            files={"Foo/a.py": b"print('a')\n"},
        )
        directory_collision_manifest = self._manifest(directory_collision)
        directory_collision_manifest["files"].append({
            "path": "foo/b.py",
            "size": len(b"print('b')\n"),
            "sha256": hashlib.sha256(b"print('b')\n").hexdigest(),
        })
        self._write_manifest(
            directory_collision,
            directory_collision_manifest,
        )
        with self.assertRaisesRegex(ValueError, "目录|分量|大小写|冲突"):
            factory.validate_domain_pack(directory_collision)

    def test_unknown_direction_missing_fields_and_missing_files_are_rejected(
        self,
    ) -> None:
        factory = self._factory()

        unknown_direction = self._new_pack("unknown-direction")
        unknown_direction_manifest = self._manifest(unknown_direction)
        unknown_direction_manifest["id"] = "ocr"
        unknown_direction_manifest["primary_direction"] = "ocr"
        unknown_direction_manifest["template_id"] = "PACK-OCR"
        self._write_manifest(unknown_direction, unknown_direction_manifest)
        with self.assertRaisesRegex(ValueError, "id|方向"):
            factory.validate_domain_pack(unknown_direction)

        missing_field = self._new_pack("missing-field")
        missing_field_manifest = self._manifest(missing_field)
        del missing_field_manifest["license"]
        self._write_manifest(missing_field, missing_field_manifest)
        with self.assertRaisesRegex(ValueError, "字段|license"):
            factory.validate_domain_pack(missing_field)

        missing_file = self._new_pack("missing-file")
        (missing_file / "payload" / "README.md").unlink()
        with self.assertRaisesRegex(ValueError, "缺失|files|清单"):
            factory.validate_domain_pack(missing_file)

    def test_absolute_parent_and_non_normalized_payload_paths_are_rejected(
        self,
    ) -> None:
        factory = self._factory()
        invalid_paths = (
            "/absolute.py",
            "../escape.py",
            "src/../escape.py",
        )
        for index, invalid_path in enumerate(invalid_paths):
            with self.subTest(path=invalid_path):
                pack = self._new_pack(f"invalid-path-{index}")
                manifest = self._manifest(pack)
                manifest["files"][0]["path"] = invalid_path
                self._write_manifest(pack, manifest)
                with self.assertRaisesRegex(
                    ValueError,
                    "绝对|\\.\\.|POSIX|规范",
                ):
                    factory.validate_domain_pack(pack)

    def test_license_and_source_references_are_strict(self) -> None:
        factory = self._factory()

        invalid_license = self._new_pack("invalid-pack-license")
        invalid_license_manifest = self._manifest(invalid_license)
        invalid_license_manifest["license"] = "GPL-3.0"
        self._write_manifest(invalid_license, invalid_license_manifest)
        with self.assertRaisesRegex(ValueError, "license|许可证|allowlist"):
            factory.validate_domain_pack(invalid_license)

        invalid_references: tuple[object, ...] = (
            ["legacy-string-reference"],
            [{
                "name": "missing-license",
                "url": "https://example.com/source.git",
                "revision": "2" * 40,
            }],
            [{
                "name": "unknown-field",
                "url": "https://example.com/source.git",
                "revision": "2" * 40,
                "license": "MIT",
                "unexpected": True,
            }],
            [{
                "name": "insecure-url",
                "url": "http://example.com/source.git",
                "revision": "2" * 40,
                "license": "MIT",
            }],
            [{
                "name": "short-revision",
                "url": "https://example.com/source.git",
                "revision": "abc123",
                "license": "MIT",
            }],
            [{
                "name": "bad-reference-license",
                "url": "https://example.com/source.git",
                "revision": "2" * 40,
                "license": "GPL-3.0",
            }],
        )
        for index, references in enumerate(invalid_references):
            with self.subTest(case=index):
                pack = self._new_pack(f"invalid-reference-{index}")
                manifest = self._manifest(pack)
                manifest["source_references"] = references
                self._write_manifest(pack, manifest)
                with self.assertRaisesRegex(
                    ValueError,
                    "source_references|来源|url|revision|license",
                ):
                    factory.validate_domain_pack(pack)

        duplicated = self._new_pack("duplicated-reference")
        duplicated_manifest = self._manifest(duplicated)
        duplicated_manifest["source_references"] *= 2
        self._write_manifest(duplicated, duplicated_manifest)
        with self.assertRaisesRegex(ValueError, "重复|source_references"):
            factory.validate_domain_pack(duplicated)

    def test_binary_individual_large_and_total_large_payloads_are_rejected(
        self,
    ) -> None:
        factory = self._factory()

        binary = self._new_pack(
            "binary-payload",
            files={"binary.txt": b"\x00\x01\x02\x03"},
        )
        with self.assertRaisesRegex(ValueError, "二进制|UTF-8|文本"):
            factory.validate_domain_pack(binary)

        individual = self._new_pack(
            "large-file",
            files={"large.txt": b"x" * 1025},
        )
        with mock.patch.object(factory, "MAX_PAYLOAD_FILE_SIZE", 1024):
            with self.assertRaisesRegex(ValueError, "大|size|大小"):
                factory.validate_domain_pack(individual)

        aggregate = self._new_pack(
            "large-total",
            files={
                "first.txt": b"a" * 12,
                "second.txt": b"b" * 12,
            },
        )
        with mock.patch.object(factory, "MAX_PAYLOAD_SIZE", 24):
            with self.assertRaisesRegex(ValueError, "总量|50|payload"):
                factory.validate_domain_pack(aggregate)

    def test_existing_destination_is_never_overwritten_or_deleted(self) -> None:
        factory = self._factory()
        pack = self._new_pack("target-conflict")
        destination = self.root / "existing"
        destination.mkdir()
        sentinel = destination / "keep.txt"
        sentinel.write_text("keep me\n", encoding="utf-8")

        with self.assertRaisesRegex(FileExistsError, "存在|覆盖"):
            self._materialize_for_test(
                factory,
                pack,
                destination,
                "classification",
            )

        self.assertEqual("keep me\n", sentinel.read_text(encoding="utf-8"))

    def test_git_identity_first_commit_tag_remote_and_clean_state(self) -> None:
        factory = self._factory()
        pack = self._new_pack("git-identity")
        destination = self.root / "git-repository"

        repository = self._materialize_for_test(
            factory,
            pack,
            destination,
            "classification",
        )

        commit = self._git(destination, "rev-parse", "HEAD")
        tag = "domain-pack/cls/v1.0.0"
        self.assertEqual("main", self._git(destination, "branch", "--show-current"))
        self.assertEqual(commit, repository["initial_commit"])
        self.assertEqual(tag, repository["initial_tag"])
        self.assertEqual(
            commit,
            self._git(destination, "rev-parse", f"refs/tags/{tag}^{{commit}}"),
        )
        self.assertEqual(
            "commit",
            self._git(destination, "cat-file", "-t", f"refs/tags/{tag}"),
        )
        self.assertEqual("", self._git(destination, "remote"))
        self.assertEqual(
            "",
            self._git(
                destination,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ),
        )
        committed_files = set(
            self._git(destination, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        )
        declared_files = {
            item["path"]
            for item in self._manifest(pack)["files"]
        }
        self.assertEqual(declared_files, committed_files)
        self.assertEqual(
            "CV Domain Pack Factory\x00domain-pack-factory@localhost",
            self._git(destination, "show", "-s", "--format=%an%x00%ae", "HEAD"),
        )

    def test_git_blob_bytes_must_match_manifest_before_publish(self) -> None:
        factory = self._factory()
        pack = self._new_pack(
            "filtered-git-blob",
            files={
                ".gitattributes": b"*.txt text eol=lf\n",
                "README.txt": b"first\r\nsecond\r\n",
            },
        )
        destination = self.root / "filtered-repository"

        with self.assertRaisesRegex(
            RuntimeError,
            "blob|sha256|摘要|残留路径|保留",
        ):
            self._materialize_for_test(
                factory,
                pack,
                destination,
                "classification",
            )

        self.assertFalse(destination.exists())
        staging = list(
            self.root.glob(".filtered-repository.domain-pack-staging-*")
        )
        self.assertEqual(1, len(staging))
        self.assertEqual(
            b"first\r\nsecond\r\n",
            (staging[0] / "README.txt").read_bytes(),
        )
        self.assertEqual(
            b"first\nsecond\n",
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(staging[0]),
                    "cat-file",
                    "blob",
                    "HEAD:README.txt",
                ],
                capture_output=True,
                check=True,
            ).stdout,
        )

    def test_git_author_is_not_persisted_in_local_repository_config(self) -> None:
        factory = self._factory()
        pack = self._new_pack("one-shot-author")
        destination = self.root / "one-shot-author-repository"

        self._materialize_for_test(
            factory,
            pack,
            destination,
            "classification",
        )

        for key in ("user.name", "user.email"):
            with self.subTest(key=key):
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(destination),
                        "config",
                        "--local",
                        "--get",
                        key,
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                self.assertEqual(1, result.returncode, result.stderr)
                self.assertEqual("", result.stdout)

    def test_git_initialization_ignores_global_template_and_signing(self) -> None:
        factory = self._factory()
        pack = self._new_pack("hostile-global-git")
        destination = self.root / "isolated-git-repository"
        template = self.root / "hostile-template"
        template.mkdir()
        (template / "leaked-from-global.txt").write_text(
            "must not be copied\n",
            encoding="utf-8",
        )
        global_config = self.root / "hostile-global.gitconfig"
        global_config.write_text(
            "[init]\n"
            f"\ttemplateDir = {template.as_posix()}\n"
            "[commit]\n"
            "\tgpgSign = true\n"
            "[tag]\n"
            "\tgpgSign = true\n",
            encoding="utf-8",
        )

        with mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": str(global_config),
                "GIT_CONFIG_NOSYSTEM": "1",
            },
            clear=False,
        ):
            self._materialize_for_test(
                factory,
                pack,
                destination,
                "classification",
            )

        self.assertFalse(
            (destination / ".git" / "leaked-from-global.txt").exists()
        )
        self.assertEqual(
            "false",
            self._git(destination, "config", "--local", "commit.gpgSign"),
        )
        self.assertEqual(
            "false",
            self._git(destination, "config", "--local", "tag.gpgSign"),
        )

    def test_git_failure_preserves_staging_and_reports_residual_path(self) -> None:
        factory = self._factory()
        pack = self._new_pack("failed-staging")
        destination = self.root / "failed-repository"

        with mock.patch.object(
            factory.subprocess,
            "run",
            side_effect=OSError("simulated git failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "Git|git|failure|失败|残留路径|保留",
            ) as raised:
                self._materialize_for_test(
                    factory,
                    pack,
                    destination,
                    "classification",
                )

        self.assertFalse(destination.exists())
        staging = list(
            self.root.glob(".failed-repository.domain-pack-staging-*")
        )
        self.assertEqual(1, len(staging))
        self.assertTrue(staging[0].is_dir())
        self.assertIn(str(staging[0].resolve()), str(raised.exception))

    def test_unknown_user_file_blocks_pre_git_staging_cleanup(self) -> None:
        factory = self._factory()
        pack = self._new_pack("unknown-staging-entry")
        destination = self.root / "unknown-staging-repository"
        original_copy = factory._copy_payload
        injected: list[Path] = []

        def copy_then_inject_unknown(*args: object, **kwargs: object) -> None:
            original_copy(*args, **kwargs)
            staging = Path(args[1])
            unknown = staging / "user-unknown.txt"
            unknown.write_text("must survive\n", encoding="utf-8")
            injected.append(unknown)
            raise ValueError("simulated pre-git failure")

        with mock.patch.object(
            factory,
            "_copy_payload",
            side_effect=copy_then_inject_unknown,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "未知|unknown|残留路径|拒绝清理",
            ):
                self._materialize_for_test(
                    factory,
                    pack,
                    destination,
                    "classification",
                )

        self.assertEqual(1, len(injected))
        self.assertTrue(injected[0].is_file())
        self.assertEqual(
            "must survive\n",
            injected[0].read_text(encoding="utf-8"),
        )

    def test_create_registers_codebase_with_the_repository_identity(self) -> None:
        factory = self._factory()
        pack = self._new_pack("registered-codebase", direction="gzsl")
        project = self.root / "project"
        init_project(project, "factory-project", layout="v2")
        destination = self.root / "registered-repository"

        result = factory.create_domain_repository(
            project,
            pack,
            destination,
            "classification",
        )

        repository = result["repository"]
        codebase = result["codebase"]
        self.assertEqual("CB-0001", codebase["id"])
        self.assertEqual("classification", codebase["name"])
        self.assertEqual("gzsl", codebase["primary_direction"])
        self.assertEqual(repository["repo_path"], codebase["repo_path"])
        self.assertEqual("domain_pack", codebase["source"])
        self.assertEqual("PACK-GZSL", codebase["template_id"])
        self.assertEqual("1.0.0", codebase["template_version"])
        self.assertEqual(repository["initial_commit"], codebase["initial_commit"])
        self.assertEqual(repository["initial_tag"], codebase["initial_tag"])
        self.assertEqual(
            codebase,
            read_json(
                project
                / ".experiment-workflow"
                / "codebases"
                / "CB-0001.json"
            ),
        )

    def test_registration_failure_preserves_repository_and_carries_identity(
        self,
    ) -> None:
        factory = self._factory()
        pack = self._new_pack("registration-failure", direction="gzsl")
        project = self.root / "project"
        init_project(project, "factory-project", layout="v2")
        destination = self.root / "preserved-repository"

        with mock.patch.object(
            factory,
            "register_codebase",
            side_effect=RuntimeError("simulated registration failure"),
        ):
            with self.assertRaises(
                factory.DomainRepositoryRegistrationError
            ) as raised:
                factory.create_domain_repository(
                    project,
                    pack,
                    destination,
                    "classification",
                )

        error = raised.exception
        self.assertTrue(destination.is_dir())
        self.assertEqual(str(destination.resolve()), error.repo_path)
        self.assertEqual(
            self._git(destination, "rev-parse", "HEAD"),
            error.commit,
        )
        self.assertEqual("domain-pack/gzsl/v1.0.0", error.tag)
        self.assertIn(str(destination.resolve()), str(error))
        self.assertIn(error.commit, str(error))
        self.assertIn(error.tag, str(error))

    def test_concurrent_same_destination_has_exactly_one_success(self) -> None:
        factory = self._factory()
        pack = self._new_pack("concurrent-pack")
        destination = self.root / "shared-destination"
        barrier = threading.Barrier(2)
        successes: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def materialize() -> None:
            barrier.wait(timeout=10)
            try:
                successes.append(
                    self._materialize_for_test(
                        factory,
                        pack,
                        destination,
                        "classification",
                    )
                )
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = [threading.Thread(target=materialize) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(1, len(successes))
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], FileExistsError)
        self.assertEqual(
            successes[0]["initial_commit"],
            self._git(destination, "rev-parse", "HEAD"),
        )
        self.assertEqual(
            [],
            list(self.root.glob(".shared-destination.domain-pack-staging-*")),
        )


if __name__ == "__main__":
    unittest.main()
