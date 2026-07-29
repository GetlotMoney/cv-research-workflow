from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from .codebases import CODEBASE_SCHEMA, register_codebase
from .io import parse_json_object_bytes
from .project import is_link_or_reparse


DOMAIN_PACK_SCHEMA = "cv-experiment-workflow.domain-pack.v1"
DOMAIN_PACK_REGISTRY_SCHEMA = (
    "cv-experiment-workflow.domain-pack-registry.v1"
)
DOMAIN_PACK_FIELDS = {
    "schema",
    "id",
    "version",
    "template_id",
    "primary_direction",
    "license",
    "source_references",
    "files",
}
DOMAIN_PACK_FILE_FIELDS = {"path", "size", "sha256"}
SOURCE_REFERENCE_FIELDS = {"name", "url", "revision", "license"}
DOWNLOAD_MANIFEST_SCHEMA = "cv-experiment-workflow.download-manifest.v1"
DOWNLOAD_MANIFEST_MAX_BYTES = 512 * 1024
DOWNLOAD_MANIFEST_FIELDS = {
    "schema",
    "automatic_download",
    "bundled_large_files",
    "optional_resources",
}
DOWNLOAD_RESOURCE_FIELDS = {
    "name",
    "kind",
    "url",
    "version",
    "revision",
    "license",
    "license_url",
    "sha256",
}
DOWNLOAD_RESOURCE_KINDS = {
    "dependency_release_page",
    "dataset_release_page",
    "source_release_page",
    "model_release_page",
    "direct_artifact",
}
FLOATING_RESOURCE_TOKENS = {
    "current",
    "develop",
    "development",
    "head",
    "latest",
    "main",
    "master",
    "nightly",
    "stable",
    "trunk",
}
GENERIC_RESOURCE_TERMINALS = {
    "download",
    "downloads",
    "release",
    "releases",
}
REGISTRY_FIELDS = {"schema", "packs"}
REGISTRY_ENTRY_FIELDS = {
    "id",
    "version",
    "template_id",
    "primary_direction",
    "directory",
}
DIRECTIONS = {"det", "cls", "seg", "instseg", "sr", "gzsl"}
ALLOWED_LICENSES = {
    "MIT",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
}
SEMANTIC_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
)
SHA256 = re.compile(r"[0-9a-f]{64}")
GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
TEMPLATE_ID = re.compile(r"PACK-[A-Z0-9]+(?:-[A-Z0-9]+)*")
MAX_MANIFEST_SIZE = 1024 * 1024
MAX_PAYLOAD_FILE_SIZE = 16 * 1024 * 1024
MAX_PAYLOAD_SIZE = 50 * 1024 * 1024
MAX_PAYLOAD_FILES = 4096
MAX_TEXT = 4096
MAX_PATH = 1024
MAX_NAME = 256
GIT_AUTHOR_NAME = "CV Domain Pack Factory"
GIT_AUTHOR_EMAIL = "domain-pack-factory@localhost"
GIT_SAFE_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "credential.helper=",
    "-c",
    "credential.interactive=false",
    "-c",
    "core.pager=cat",
    "-c",
    "interactive.diffFilter=",
    "-c",
    "diff.external=",
    "-c",
    "commit.gpgSign=false",
    "-c",
    "tag.gpgSign=false",
)
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
    "COM¹",
    "COM²",
    "COM³",
    "LPT¹",
    "LPT²",
    "LPT³",
}
WINDOWS_FORBIDDEN_CHARACTERS = set('<>:"/\\|?*')
BUILTIN_DOMAIN_PACKS_ROOT = (
    Path(__file__).resolve().parents[2] / "assets" / "domain-packs"
)


class DomainRepositoryRegistrationError(RuntimeError):
    """仓库已创建，但 Codebase 登记失败；仓库会原样保留。"""

    def __init__(
        self,
        *,
        repo_path: str,
        commit: str,
        tag: str,
        cause: BaseException,
    ) -> None:
        self.repo_path = repo_path
        self.commit = commit
        self.tag = tag
        self.cause = cause
        super().__init__(
            "方向仓库已经创建，但 Codebase 登记失败；仓库已保留："
            f"path={repo_path} commit={commit} tag={tag}；原因：{cause}"
        )


def discover_domain_packs() -> list[dict[str, str]]:
    """只读核验并列出 Skill 内置方向包，不接受外部资源根。"""
    root = _safe_existing_directory(
        BUILTIN_DOMAIN_PACKS_ROOT,
        "内置方向包目录",
    )
    registry_path = root / "registry.json"
    raw_registry = _read_safe_regular_file(
        registry_path,
        limit=MAX_MANIFEST_SIZE,
        label="方向包 registry.json",
    )
    try:
        decoded = raw_registry.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"方向包 registry.json 必须是严格 UTF-8 JSON：{error}"
        ) from error
    if not isinstance(payload, dict) or set(payload) != REGISTRY_FIELDS:
        raise ValueError("方向包 registry.json 字段无效")
    if payload.get("schema") != DOMAIN_PACK_REGISTRY_SCHEMA:
        raise ValueError("方向包 registry.json schema 无效")
    raw_packs = payload.get("packs")
    if (
        not isinstance(raw_packs, list)
        or not raw_packs
        or len(raw_packs) > len(DIRECTIONS)
    ):
        raise ValueError("方向包 registry packs 必须包含 1..6 个对象")

    packs: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    directories: set[str] = set()
    for raw_pack in raw_packs:
        if (
            not isinstance(raw_pack, dict)
            or set(raw_pack) != REGISTRY_ENTRY_FIELDS
        ):
            raise ValueError("方向包 registry 条目字段无效")
        pack_id = raw_pack.get("id")
        version = raw_pack.get("version")
        template_id = raw_pack.get("template_id")
        primary_direction = raw_pack.get("primary_direction")
        directory = raw_pack.get("directory")
        if pack_id not in DIRECTIONS:
            raise ValueError("方向包 registry id 无效")
        if (
            not isinstance(version, str)
            or SEMANTIC_VERSION.fullmatch(version) is None
        ):
            raise ValueError("方向包 registry version 无效")
        if template_id != f"PACK-{pack_id.upper()}":
            raise ValueError("方向包 registry template_id 与方向不一致")
        if primary_direction != pack_id:
            raise ValueError(
                "方向包 registry primary_direction 与 id 不一致"
            )
        expected_directory = f"{pack_id}-v{version}"
        if directory != expected_directory:
            raise ValueError(
                "方向包 registry directory 必须由 id 和 version 唯一确定"
            )
        identity = (pack_id, version)
        if identity in identities or directory in directories:
            raise ValueError("方向包 registry 条目重复")
        pack_root = _safe_existing_directory(
            root / directory,
            f"内置方向包 {pack_id}",
        )
        if pack_root.parent != root:
            raise ValueError("内置方向包目录逃逸")
        manifest = validate_domain_pack(pack_root)
        for key in (
            "id",
            "version",
            "template_id",
            "primary_direction",
        ):
            if manifest[key] != raw_pack[key]:
                raise ValueError(
                    f"方向包 registry 与 pack.json 不一致：{key}"
                )
        identities.add(identity)
        directories.add(directory)
        packs.append({
            "id": pack_id,
            "version": version,
            "template_id": template_id,
            "primary_direction": primary_direction,
            "directory": directory,
        })
    if packs != sorted(packs, key=lambda item: (item["id"], item["version"])):
        raise ValueError("方向包 registry packs 必须按 id/version 排序")
    return packs


def direction_availability() -> dict[str, str]:
    """从统一系统 catalog 读取六方向开放状态。"""
    candidates = [
        parent / "config" / "directions" / "catalog.json"
        for parent in Path(__file__).resolve().parents
    ]
    catalog_path = next((path for path in candidates if path.is_file()), None)
    if catalog_path is None:
        raise ValueError("缺少六方向唯一状态文件 config/directions/catalog.json")
    raw_catalog = _read_safe_regular_file(
        catalog_path,
        limit=MAX_MANIFEST_SIZE,
        label="方向状态 catalog.json",
    )
    try:
        payload = json.loads(raw_catalog.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"六方向状态文件必须是 UTF-8 JSON：{error}") from error
    directions = payload.get("directions") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "cvwf.direction-catalog.v1"
        or not isinstance(directions, list)
        or len(directions) != len(DIRECTIONS)
    ):
        raise ValueError("六方向状态文件 schema 或方向数量无效")
    result: dict[str, str] = {}
    for item in directions:
        if not isinstance(item, dict):
            raise ValueError("六方向状态条目必须是对象")
        slug = item.get("slug")
        status = item.get("status")
        if (
            slug not in DIRECTIONS
            or slug in result
            or status not in {"ready", "pending"}
        ):
            raise ValueError("六方向 slug、状态或唯一性无效")
        result[str(slug)] = str(status)
    if set(result) != DIRECTIONS:
        raise ValueError("六方向状态文件缺失方向")
    return result


def resolve_domain_pack(selector: str) -> Path:
    """把 `cls` 或 `cls@1.0.0` 解析到已核验的内置包目录。"""
    selected = _text(selector, "方向包选择器", maximum=128)
    if selected.count("@") > 1:
        raise ValueError("方向包选择器必须是 id 或 id@version")
    if "@" in selected:
        pack_id, version = selected.split("@", 1)
        if (
            pack_id not in DIRECTIONS
            or SEMANTIC_VERSION.fullmatch(version) is None
        ):
            raise ValueError("方向包选择器必须是 id 或 id@version")
    else:
        pack_id = selected
        version = None
        if pack_id not in DIRECTIONS:
            raise ValueError("未知方向包")
    matches = [
        item
        for item in discover_domain_packs()
        if item["id"] == pack_id
        and (version is None or item["version"] == version)
    ]
    if not matches:
        raise ValueError(f"未找到内置方向包：{selected}")
    if len(matches) != 1:
        versions = ", ".join(item["version"] for item in matches)
        raise ValueError(
            f"方向包 {pack_id} 有多个版本，请写成 id@version：{versions}"
        )
    return (
        BUILTIN_DOMAIN_PACKS_ROOT / matches[0]["directory"]
    ).resolve(strict=True)


def validate_domain_pack(pack_root: Path) -> dict[str, Any]:
    """严格核验一个只含 UTF-8 文本 payload 的方向包。"""
    root = _safe_existing_directory(pack_root, "方向包根目录")
    _validate_pack_layout(root)
    manifest_path = root / "pack.json"
    raw_manifest = _read_safe_regular_file(
        manifest_path,
        limit=MAX_MANIFEST_SIZE,
        label="pack.json",
    )
    try:
        decoded = raw_manifest.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"pack.json 必须是严格 UTF-8 JSON：{error}") from error
    manifest = _normalize_manifest(payload)

    payload_root = _safe_existing_directory(root / "payload", "payload 目录")
    actual_files = _scan_payload(payload_root)
    declared_paths = {entry["path"] for entry in manifest["files"]}
    actual_paths = set(actual_files)
    if declared_paths != actual_paths:
        missing = sorted(declared_paths - actual_paths)
        unlisted = sorted(actual_paths - declared_paths)
        raise ValueError(
            "pack.json files 清单必须精确覆盖 payload 普通文件；"
            f"缺失={missing} 未声明={unlisted}"
        )

    total = 0
    downloads_content: bytes | None = None
    for entry in manifest["files"]:
        source = payload_root.joinpath(*PurePosixPath(entry["path"]).parts)
        content = _read_safe_regular_file(
            source,
            limit=MAX_PAYLOAD_FILE_SIZE,
            label=f"payload 文件 {entry['path']}",
        )
        if len(content) != entry["size"]:
            raise ValueError(
                f"payload 文件 size 与清单不一致：{entry['path']}"
            )
        digest = hashlib.sha256(content).hexdigest()
        if digest != entry["sha256"]:
            raise ValueError(
                f"payload 文件 sha256 摘要与清单不一致：{entry['path']}"
            )
        _require_utf8_text(content, entry["path"])
        if entry["path"] == "DOWNLOADS.json":
            downloads_content = content
        total += len(content)
        if total >= MAX_PAYLOAD_SIZE:
            raise ValueError(
                "payload 总量必须严格小于 50 MiB"
            )
    if downloads_content is None:
        raise ValueError("方向包必须包含 DOWNLOADS.json")
    _validate_download_manifest(downloads_content)
    return manifest


def _validate_download_manifest(content: bytes) -> dict[str, Any]:
    if len(content) > DOWNLOAD_MANIFEST_MAX_BYTES:
        raise ValueError(
            "DOWNLOADS.json 体积超过 512 KiB 上限"
        )
    payload = parse_json_object_bytes(content, "DOWNLOADS.json")
    if set(payload) != DOWNLOAD_MANIFEST_FIELDS:
        raise ValueError(
            "DOWNLOADS.json 字段无效；说明文字应写入 README，"
            "清单只保留 schema/automatic_download/"
            "bundled_large_files/optional_resources"
        )
    if payload.get("schema") != DOWNLOAD_MANIFEST_SCHEMA:
        raise ValueError("DOWNLOADS.json schema 无效")
    if payload.get("automatic_download") is not False:
        raise ValueError(
            "DOWNLOADS.json automatic_download 必须为 false，"
            "方向模板不得自动下载"
        )
    if payload.get("bundled_large_files") != []:
        raise ValueError(
            "DOWNLOADS.json bundled_large_files 必须为空，"
            "方向模板不得捆绑大文件"
        )

    resources = payload.get("optional_resources")
    if (
        not isinstance(resources, list)
        or isinstance(resources, (str, bytes))
        or not 1 <= len(resources) <= 64
    ):
        raise ValueError(
            "DOWNLOADS.json optional_resources 必须包含 1..64 个资源"
        )

    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    identities: set[tuple[str, str]] = set()
    for index, raw_resource in enumerate(resources):
        label = f"下载资源[{index}]"
        if (
            not isinstance(raw_resource, dict)
            or set(raw_resource) != DOWNLOAD_RESOURCE_FIELDS
        ):
            raise ValueError(f"{label}字段无效")
        name = _text(raw_resource.get("name"), f"{label} name", maximum=256)
        name_key = unicodedata.normalize("NFC", name).casefold()
        if name_key in names:
            raise ValueError(f"{label} name 重复")
        names.add(name_key)

        kind = raw_resource.get("kind")
        if kind not in DOWNLOAD_RESOURCE_KINDS:
            raise ValueError(
                f"{label} kind 必须是固定发布页或 direct_artifact"
            )
        url = _download_https_url(
            raw_resource.get("url"),
            f"{label} url",
            allow_fragment=kind != "direct_artifact",
        )
        version = _fixed_resource_text(
            raw_resource.get("version"),
            f"{label} version",
        )
        revision = _fixed_resource_text(
            raw_resource.get("revision"),
            f"{label} revision",
        )
        identity = (url, revision)
        if identity in identities:
            raise ValueError(f"{label} URL/revision 身份重复")
        identities.add(identity)
        license_name = _text(
            raw_resource.get("license"),
            f"{label} license",
            maximum=1024,
        )
        license_url = _download_https_url(
            raw_resource.get("license_url"),
            f"{label} license_url",
            allow_fragment=True,
        )
        sha256 = raw_resource.get("sha256")
        if kind == "direct_artifact":
            if (
                not isinstance(sha256, str)
                or SHA256.fullmatch(sha256) is None
            ):
                raise ValueError(
                    f"{label} direct_artifact 必须提供 64 位 sha256 摘要"
                )
        elif sha256 is not None:
            raise ValueError(
                f"{label}发布页不是直接制品，sha256 必须显式为 null"
            )
        normalized.append({
            "name": name,
            "kind": kind,
            "url": url,
            "version": version,
            "revision": revision,
            "license": license_name,
            "license_url": license_url,
            "sha256": sha256,
        })
    return {
        "schema": DOWNLOAD_MANIFEST_SCHEMA,
        "automatic_download": False,
        "bundled_large_files": [],
        "optional_resources": normalized,
    }


def _fixed_resource_text(value: object, label: str) -> str:
    text = _text(value, label, maximum=512)
    tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", text.casefold())
        if token
    }
    floating = sorted(tokens & FLOATING_RESOURCE_TOKENS)
    if floating:
        raise ValueError(
            f"{label} 必须固定到明确版本，不能使用：{floating}"
        )
    return text


def _download_https_url(
    value: object,
    label: str,
    *,
    allow_fragment: bool,
) -> str:
    url = _text(value, label, maximum=2048)
    if not url.startswith("https://") or "\\" in url:
        raise ValueError(f"{label} 必须是规范 HTTPS URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{label} 必须是规范 HTTPS URL") from error
    decoded_path = parsed.path
    for _ in range(16):
        next_path = unquote(decoded_path)
        if next_path == decoded_path:
            break
        decoded_path = next_path
    else:
        raise ValueError(f"{label} 路径编码层数过多")
    decoded_components = [
        part.casefold()
        for part in decoded_path.split("/")
        if part
    ]
    decoded_tokens = {
        token
        for component in decoded_components
        for token in re.split(r"[^a-z0-9]+", component)
        if token
    }
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or "%" in parsed.netloc
        or bool(parsed.query)
        or (not allow_fragment and parsed.fragment)
        or "\\" in decoded_path
        or any(part in {".", ".."} for part in decoded_path.split("/"))
        or any(
            unicodedata.category(character).startswith("C")
            for character in decoded_path
        )
        or bool(decoded_tokens & FLOATING_RESOURCE_TOKENS)
        or (
            bool(decoded_components)
            and decoded_components[-1] in GENERIC_RESOURCE_TERMINALS
        )
    ):
        raise ValueError(f"{label} 必须是规范 HTTPS URL")
    try:
        parsed.hostname.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} 主机名必须是规范 ASCII") from error
    canonical_host = parsed.hostname.lower()
    if ":" in canonical_host:
        canonical_host = f"[{canonical_host}]"
    canonical_netloc = (
        canonical_host
        if port is None
        else f"{canonical_host}:{port}"
    )
    if parsed.netloc != canonical_netloc or urlunsplit(parsed) != url:
        raise ValueError(f"{label} 必须是规范 HTTPS URL")
    return url


def materialize_domain_repository(
    pack_root: Path,
    destination: Path,
    name: str,
) -> dict[str, Any]:
    return _materialize_domain_repository(
        pack_root,
        destination,
        name,
        _allow_pending_for_test=False,
    )


def _materialize_domain_repository_for_test(
    pack_root: Path,
    destination: Path,
    name: str,
) -> dict[str, Any]:
    """仅供测试夹具构造非当前开放方向的临时仓库。"""
    return _materialize_domain_repository(
        pack_root,
        destination,
        name,
        _allow_pending_for_test=True,
    )


def _materialize_domain_repository(
    pack_root: Path,
    destination: Path,
    name: str,
    *,
    _allow_pending_for_test: bool,
) -> dict[str, Any]:
    """从已核验方向包原子创建一个无远端、干净的 main Git 仓库。"""
    display_name = _text(name, "仓库名称", maximum=MAX_NAME)
    manifest = validate_domain_pack(pack_root)
    if not _allow_pending_for_test:
        availability = direction_availability()
        direction = manifest["primary_direction"]
        if availability[direction] != "ready":
            raise ValueError(f"研究方向尚未开放：{direction}")
    root = _safe_existing_directory(pack_root, "方向包根目录")
    target, parent = _prepare_destination(destination)
    lock_path, lock_identity = _acquire_destination_lock(parent, target.name)
    staging: Path | None = None
    staging_identity: tuple[int, int] | None = None
    owned_entries: dict[str, tuple[str, tuple[int, int]]] = {}
    git_started = False
    published = False
    try:
        _raise_if_target_conflicts(target)
        staging, staging_identity = _create_staging(parent, target.name)
        _copy_payload(
            root / "payload",
            staging,
            manifest,
            owned_entries,
        )
        tag = f"domain-pack/{manifest['id']}/v{manifest['version']}"
        git_started = True
        _initialize_git_repository(staging, display_name, tag)
        commit = _verify_materialized_repository(
            staging,
            manifest,
            tag,
        )
        _publish_directory_no_replace(staging, target)
        published = True
        final_root = _safe_existing_directory(target, "新方向仓库")
        final_commit = _verify_materialized_repository(
            final_root,
            manifest,
            tag,
            expected_commit=commit,
        )
        return {
            "repo_path": str(final_root),
            "default_branch": "main",
            "initial_commit": final_commit,
            "initial_tag": tag,
            "pack_id": manifest["id"],
            "pack_version": manifest["version"],
            "template_id": manifest["template_id"],
            "primary_direction": manifest["primary_direction"],
        }
    except BaseException as error:
        if git_started:
            residual = target if published else staging
            raise RuntimeError(
                "方向仓库创建失败；Git 已经启动，外部过程产生的复杂树"
                "不会自动递归删除，已保留现场；"
                f"残留路径：{residual}；原始错误：{error}"
            ) from error
        if (
            not published
            and staging is not None
            and staging_identity is not None
        ):
            cleanup_error = _cleanup_owned_staging(
                staging,
                staging_identity,
                owned_entries,
            )
            if cleanup_error is not None:
                raise RuntimeError(
                    "方向仓库创建失败，且自有 staging 无法安全清理；"
                    f"残留路径：{staging}；清理错误：{cleanup_error}"
                ) from error
        raise
    finally:
        _unlink_owned_file(lock_path, lock_identity)


def create_domain_repository(
    project: Path,
    pack_root: Path,
    destination: Path,
    name: str,
) -> dict[str, Any]:
    """创建方向仓库，并用已有 register_codebase 登记其初始身份。"""
    repository = materialize_domain_repository(
        pack_root,
        destination,
        name,
    )
    manifest = {
        "schema": CODEBASE_SCHEMA,
        "name": _text(name, "仓库名称", maximum=MAX_NAME),
        "primary_direction": repository["primary_direction"],
        "repo_path": repository["repo_path"],
        "source": "domain_pack",
        "template_id": repository["template_id"],
        "template_version": repository["pack_version"],
        "default_branch": repository["default_branch"],
        "initial_commit": repository["initial_commit"],
        "initial_tag": repository["initial_tag"],
    }
    temporary_manifest: Path | None = None
    temporary_identity: tuple[int, int] | None = None
    try:
        temporary_manifest, temporary_identity = _write_temporary_manifest(
            Path(repository["repo_path"]).parent,
            manifest,
        )
        codebase = register_codebase(
            Path(project),
            temporary_manifest,
            Path(repository["repo_path"]),
        )
    except Exception as error:
        raise DomainRepositoryRegistrationError(
            repo_path=repository["repo_path"],
            commit=repository["initial_commit"],
            tag=repository["initial_tag"],
            cause=error,
        ) from error
    finally:
        if temporary_manifest is not None and temporary_identity is not None:
            _unlink_owned_file(temporary_manifest, temporary_identity)
    return {
        "repository": repository,
        "codebase": codebase,
    }


def _normalize_manifest(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != DOMAIN_PACK_FIELDS:
        raise ValueError(
            "方向包 manifest 字段无效；必须严格声明 "
            "schema/id/version/template_id/primary_direction/license/"
            "source_references/files"
        )
    if payload.get("schema") != DOMAIN_PACK_SCHEMA:
        raise ValueError("方向包 manifest schema 无效")
    pack_id = payload.get("id")
    if pack_id not in DIRECTIONS:
        raise ValueError(
            "方向包 id 必须是 det/cls/seg/instseg/sr/gzsl"
        )
    version = payload.get("version")
    if (
        not isinstance(version, str)
        or SEMANTIC_VERSION.fullmatch(version) is None
    ):
        raise ValueError("方向包 version 必须是 x.y.z 三段语义版本")
    template_id = _text(payload.get("template_id"), "template_id")
    if (
        TEMPLATE_ID.fullmatch(template_id) is None
        or template_id != f"PACK-{pack_id.upper()}"
    ):
        raise ValueError(
            f"方向包 template_id 必须与方向一致：PACK-{pack_id.upper()}"
        )
    primary_direction = payload.get("primary_direction")
    if primary_direction != pack_id:
        raise ValueError("方向包 primary_direction 必须与 id 一致")
    license_name = _allowed_license(payload.get("license"), "license")
    source_references = _normalize_source_references(
        payload.get("source_references")
    )
    files = _normalize_file_records(payload.get("files"))
    return {
        "schema": DOMAIN_PACK_SCHEMA,
        "id": pack_id,
        "version": version,
        "template_id": template_id,
        "primary_direction": primary_direction,
        "license": license_name,
        "source_references": source_references,
        "files": files,
    }


def _normalize_source_references(value: object) -> list[dict[str, str]]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 64
    ):
        raise ValueError("source_references 必须是 1..64 个严格来源对象")
    references: list[dict[str, str]] = []
    identities: set[tuple[str, str, str, str]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != SOURCE_REFERENCE_FIELDS:
            raise ValueError(
                "source_references 条目必须严格包含 "
                "name/url/revision/license"
            )
        name = _text(item.get("name"), "source_references name")
        url = _https_url(item.get("url"))
        revision = item.get("revision")
        if (
            not isinstance(revision, str)
            or GIT_COMMIT.fullmatch(revision) is None
        ):
            raise ValueError(
                "source_references revision 必须是 40 位小写 hex"
            )
        license_name = _allowed_license(
            item.get("license"),
            "source_references license",
        )
        identity = (name, url, revision, license_name)
        if identity in identities:
            raise ValueError("source_references 对象不得重复")
        identities.add(identity)
        references.append({
            "name": name,
            "url": url,
            "revision": revision,
            "license": license_name,
        })
    return references


def _normalize_file_records(value: object) -> list[dict[str, Any]]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_PAYLOAD_FILES
    ):
        raise ValueError(
            f"files 必须是 1..{MAX_PAYLOAD_FILES} 条文件记录"
        )
    records: list[dict[str, Any]] = []
    paths: set[str] = set()
    windows_paths: set[str] = set()
    windows_components: dict[tuple[tuple[str, ...], str], str] = {}
    declared_total = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != DOMAIN_PACK_FILE_FIELDS:
            raise ValueError("files 条目必须严格包含 path/size/sha256")
        relative = _normalize_payload_path(item.get("path"))
        if relative in paths:
            raise ValueError(f"files path 重复：{relative}")
        windows_key = unicodedata.normalize("NFC", relative).casefold()
        if windows_key in windows_paths:
            raise ValueError(f"files 存在 Windows 大小写冲突：{relative}")
        normalized_parent: tuple[str, ...] = ()
        for component in PurePosixPath(relative).parts:
            normalized_component = unicodedata.normalize(
                "NFC",
                component,
            ).casefold()
            component_key = (normalized_parent, normalized_component)
            existing_component = windows_components.get(component_key)
            if (
                existing_component is not None
                and existing_component != component
            ):
                raise ValueError(
                    "files 存在 Windows 目录分量 NFC/大小写冲突："
                    f"{existing_component} != {component}"
                )
            windows_components[component_key] = component
            normalized_parent = (*normalized_parent, normalized_component)
        size = item.get("size")
        if (
            type(size) is not int
            or size < 0
            or size > MAX_PAYLOAD_FILE_SIZE
        ):
            raise ValueError(
                f"payload 文件大小无效或属于大文件：{relative}"
            )
        digest = item.get("sha256")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise ValueError(f"payload 文件 sha256 无效：{relative}")
        declared_total += size
        if declared_total >= MAX_PAYLOAD_SIZE:
            raise ValueError("payload 总量必须严格小于 50 MiB")
        paths.add(relative)
        windows_paths.add(windows_key)
        records.append({
            "path": relative,
            "size": size,
            "sha256": digest,
        })
    return records


def _normalize_payload_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_PATH
        or "\\" in value
    ):
        raise ValueError("payload path 必须是规范 POSIX 相对路径")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != value
    ):
        raise ValueError("payload path 不得是绝对路径、包含 .. 或非规范分段")
    for part in pure.parts:
        _validate_windows_component(part, label=f"payload path {value}")
        if part.casefold() == ".git":
            raise ValueError("payload path 不得写入 .git")
    return value


def _validate_windows_component(component: str, *, label: str) -> None:
    if (
        not component
        or component in {".", ".."}
        or component[-1] in {" ", "."}
        or len(component) > 255
        or any(character in WINDOWS_FORBIDDEN_CHARACTERS for character in component)
        or any(
            ord(character) < 32
            or unicodedata.category(character).startswith("C")
            for character in component
        )
    ):
        raise ValueError(f"{label} 含 Windows 不允许的文件名字符")
    base = component.split(".", 1)[0].upper()
    if base in WINDOWS_RESERVED_NAMES:
        raise ValueError(f"{label} 使用 Windows 保留名：{component}")


def _validate_pack_layout(root: Path) -> None:
    try:
        entries = list(os.scandir(root))
    except OSError as error:
        raise ValueError(f"无法读取方向包根目录：{root}；{error}") from error
    names = {entry.name for entry in entries}
    if names != {"pack.json", "payload"}:
        raise ValueError(
            "方向包根目录只能包含 pack.json 与 payload；"
            f"实际={sorted(names)}"
        )
    for entry in entries:
        path = root / entry.name
        if entry.is_symlink() or is_link_or_reparse(path):
            raise ValueError(f"方向包根目录不得包含 link/reparse：{path}")
        metadata = entry.stat(follow_symlinks=False)
        if entry.name == "pack.json" and not stat.S_ISREG(metadata.st_mode):
            raise ValueError("pack.json 必须是普通文件")
        if entry.name == "payload" and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("payload 必须是普通目录")


def _scan_payload(payload_root: Path) -> dict[str, os.stat_result]:
    files: dict[str, os.stat_result] = {}
    windows_paths: set[str] = set()

    def visit(directory: Path, prefix: tuple[str, ...]) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as error:
            raise ValueError(
                f"无法读取 payload 目录：{directory}；{error}"
            ) from error
        for entry in entries:
            path = directory / entry.name
            relative_parts = (*prefix, entry.name)
            relative = PurePosixPath(*relative_parts).as_posix()
            _validate_windows_component(
                entry.name,
                label=f"payload path {relative}",
            )
            if entry.name.casefold() == ".git":
                raise ValueError("payload path 不得写入 .git")
            if entry.is_symlink() or is_link_or_reparse(path):
                raise ValueError(
                    f"payload 不得包含 link/reparse：{relative}"
                )
            try:
                # Windows 的 DirEntry.stat() 可能把 st_nlink 报成 0；
                # Path.lstat() 才能可靠区分普通文件与 hardlink。
                metadata = path.lstat()
            except OSError as error:
                raise ValueError(
                    f"无法读取 payload 条目：{relative}；{error}"
                ) from error
            if stat.S_ISDIR(metadata.st_mode):
                visit(path, relative_parts)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(
                    f"payload 只允许普通文件和目录：{relative}"
                )
            if metadata.st_nlink != 1:
                raise ValueError(
                    f"payload 不得包含 hardlink/硬链接：{relative}"
                )
            windows_key = unicodedata.normalize("NFC", relative).casefold()
            if windows_key in windows_paths:
                raise ValueError(
                    f"payload 存在 Windows 大小写冲突：{relative}"
                )
            windows_paths.add(windows_key)
            files[relative] = metadata

    visit(payload_root, ())
    return files


def _read_safe_regular_file(
    path: Path,
    *,
    limit: int,
    label: str,
) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} 不存在或不可读：{path}；{error}") from error
    if (
        not stat.S_ISREG(before.st_mode)
        or is_link_or_reparse(path)
        or before.st_nlink != 1
    ):
        link_label = "hardlink/硬链接" if before.st_nlink != 1 else "link/reparse"
        raise ValueError(f"{label} 必须是非 {link_label} 的普通文件：{path}")
    if before.st_size > limit:
        raise ValueError(f"{label} 文件大小超过上限：{path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"无法安全打开 {label}：{path}；{error}") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _file_identity(opened) != _file_identity(before)
        ):
            raise ValueError(f"{label} 在打开期间身份发生变化：{path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise ValueError(f"{label} 文件大小超过上限：{path}")
        content = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        after = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} 在读取后消失：{path}；{error}") from error
    if (
        is_link_or_reparse(path)
        or not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or _file_identity(after) != _file_identity(before)
        or after.st_size != len(content)
    ):
        raise ValueError(f"{label} 在读取期间发生变化：{path}")
    return content


def _require_utf8_text(content: bytes, relative: str) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(
            f"payload 只允许 UTF-8 文本，拒绝二进制：{relative}"
        ) from error
    if "\x00" in text:
        raise ValueError(
            f"payload 只允许 UTF-8 文本，拒绝二进制 NUL：{relative}"
        )
    for character in text:
        if character in {"\n", "\r", "\t", "\f"}:
            continue
        if unicodedata.category(character) in {"Cc", "Cs"}:
            raise ValueError(
                f"payload 只允许 UTF-8 文本，拒绝二进制控制字节：{relative}"
            )


def _copy_payload(
    payload_root: Path,
    staging: Path,
    manifest: dict[str, Any],
    owned_entries: dict[str, tuple[str, tuple[int, int]]],
) -> None:
    for entry in manifest["files"]:
        parts = PurePosixPath(entry["path"]).parts
        source = payload_root.joinpath(*parts)
        content = _read_safe_regular_file(
            source,
            limit=MAX_PAYLOAD_FILE_SIZE,
            label=f"payload 文件 {entry['path']}",
        )
        if (
            len(content) != entry["size"]
            or hashlib.sha256(content).hexdigest() != entry["sha256"]
        ):
            raise ValueError(
                f"payload 文件在复制前与清单不一致：{entry['path']}"
        )
        _require_utf8_text(content, entry["path"])
        _create_owned_parent_directories(
            staging,
            parts[:-1],
            owned_entries,
        )
        target = staging.joinpath(*parts)
        try:
            with target.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise ValueError(
                f"无法写入 staging payload：{entry['path']}；{error}"
            ) from error
        metadata = target.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or is_link_or_reparse(target)
        ):
            raise ValueError(
                f"staging payload 不是自有普通文件：{entry['path']}"
            )
        owned_entries[entry["path"]] = (
            "file",
            _file_identity(metadata),
        )


def _create_owned_parent_directories(
    staging: Path,
    parts: tuple[str, ...],
    owned_entries: dict[str, tuple[str, tuple[int, int]]],
) -> None:
    current = staging
    relative_parts: list[str] = []
    for part in parts:
        current = current / part
        relative_parts.append(part)
        relative = PurePosixPath(*relative_parts).as_posix()
        known = owned_entries.get(relative)
        if known is not None:
            metadata = current.lstat()
            if (
                known[0] != "directory"
                or not stat.S_ISDIR(metadata.st_mode)
                or is_link_or_reparse(current)
                or _file_identity(metadata) != known[1]
            ):
                raise ValueError(
                    f"staging 自有目录身份发生变化：{relative}"
                )
            continue
        try:
            current.mkdir()
        except OSError as error:
            raise ValueError(
                f"无法创建 staging 自有目录：{relative}；{error}"
            ) from error
        metadata = current.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or is_link_or_reparse(current)
        ):
            raise ValueError(
                f"staging 新建目录不是普通目录：{relative}"
            )
        owned_entries[relative] = (
            "directory",
            _file_identity(metadata),
        )


def _initialize_git_repository(root: Path, name: str, tag: str) -> None:
    _git(root, "init", "--initial-branch=main", "--template=")
    _git(root, "config", "--local", "commit.gpgSign", "false")
    _git(root, "config", "--local", "tag.gpgSign", "false")
    _git(root, "config", "--local", "core.hooksPath", os.devnull)
    _git(root, "add", "--all", "--force", "--", ".")
    _git(
        root,
        "commit",
        "--no-gpg-sign",
        "--no-verify",
        "-m",
        f"Initialize {name} from domain pack",
    )
    _git(root, "tag", "--no-sign", tag)


def _verify_materialized_repository(
    root: Path,
    manifest: dict[str, Any],
    tag: str,
    *,
    expected_commit: str | None = None,
) -> str:
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch != "main":
        raise ValueError(f"新方向仓库默认分支不是 main：{branch}")
    commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}").lower()
    if GIT_COMMIT.fullmatch(commit) is None:
        raise ValueError("新方向仓库 HEAD 不是 40 位 commit")
    if expected_commit is not None and commit != expected_commit:
        raise ValueError("新方向仓库在原子发布后 commit 身份发生变化")
    tag_commit = _git(
        root,
        "rev-parse",
        "--verify",
        f"refs/tags/{tag}^{{commit}}",
    ).lower()
    if tag_commit != commit:
        raise ValueError("方向包 tag 未精确指向首 commit")
    if _git(root, "cat-file", "-t", f"refs/tags/{tag}") != "commit":
        raise ValueError("方向包 tag 必须是轻量 tag")
    if _git(root, "remote"):
        raise ValueError("新方向仓库不得含 Git remote")
    if _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        ".",
    ):
        raise ValueError("新方向仓库首 commit 后必须 clean")
    committed = set(
        filter(
            None,
            _git(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines(),
        )
    )
    declared = {entry["path"] for entry in manifest["files"]}
    if committed != declared:
        raise ValueError(
            "新方向仓库首 commit 未精确包含全部 payload 文件"
        )
    for entry in manifest["files"]:
        blob = _git_bytes(
            root,
            "cat-file",
            "blob",
            f"HEAD:{entry['path']}",
        )
        if (
            len(blob) != entry["size"]
            or hashlib.sha256(blob).hexdigest() != entry["sha256"]
        ):
            raise ValueError(
                "新方向仓库首 commit 的 Git blob bytes 与 manifest "
                f"size/sha256 不一致：{entry['path']}"
            )
    author = _git(root, "show", "-s", "--format=%an%x00%ae", "HEAD")
    if author != f"{GIT_AUTHOR_NAME}\x00{GIT_AUTHOR_EMAIL}":
        raise ValueError("新方向仓库首 commit author 不固定")
    return commit


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = _safe_git_environment()
    try:
        return subprocess.run(
            [
                "git",
                "--no-pager",
                *GIT_SAFE_CONFIG,
                "-C",
                str(root),
                *arguments,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(
            f"无法执行 Git：{root}；{' '.join(arguments)}；{error}"
        ) from error


def _git(root: Path, *arguments: str) -> str:
    result = _run_git(root, *arguments)
    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"exit {result.returncode}"
        )
        raise ValueError(
            f"Git {' '.join(arguments)} 失败：{detail}"
        )
    return result.stdout.strip()


def _git_bytes(root: Path, *arguments: str) -> bytes:
    environment = _safe_git_environment()
    try:
        result = subprocess.run(
            [
                "git",
                "--no-pager",
                *GIT_SAFE_CONFIG,
                "-C",
                str(root),
                *arguments,
            ],
            capture_output=True,
            check=False,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(
            f"无法执行 Git：{root}；{' '.join(arguments)}；{error}"
        ) from error
    if result.returncode != 0:
        detail = (
            result.stderr.decode("utf-8", errors="replace").strip()
            or result.stdout.decode("utf-8", errors="replace").strip()
            or f"exit {result.returncode}"
        )
        raise ValueError(
            f"Git {' '.join(arguments)} 失败：{detail}"
        )
    return bytes(result.stdout)


def _safe_git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if (
            key in {
                "GIT_CONFIG_COUNT",
                "GIT_CONFIG_PARAMETERS",
                "GIT_DIR",
                "GIT_WORK_TREE",
                "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_COMMON_DIR",
                "GIT_TEMPLATE_DIR",
                "GIT_EXTERNAL_DIFF",
            }
            or key.startswith("GIT_CONFIG_KEY_")
            or key.startswith("GIT_CONFIG_VALUE_")
        ):
            environment.pop(key, None)
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "GIT_PAGER": "",
        "PAGER": "",
        "GIT_AUTHOR_NAME": GIT_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": GIT_AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": GIT_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": GIT_AUTHOR_EMAIL,
    })
    return environment


def _prepare_destination(destination: Path) -> tuple[Path, Path]:
    candidate = _absolute_lexical_path(Path(destination).expanduser())
    if not candidate.name:
        raise ValueError("目标仓库路径必须有目录名")
    _validate_windows_component(candidate.name, label="目标仓库目录名")
    parent = _safe_existing_directory(candidate.parent, "目标仓库父目录")
    target = parent / candidate.name
    _raise_if_target_conflicts(target)
    return target, parent


def _raise_if_target_conflicts(target: Path) -> None:
    if os.path.lexists(target):
        raise FileExistsError(f"目标已存在，拒绝覆盖：{target}")
    key = unicodedata.normalize("NFC", target.name).casefold()
    try:
        siblings = os.scandir(target.parent)
    except OSError as error:
        raise ValueError(
            f"无法检查目标仓库父目录：{target.parent}；{error}"
        ) from error
    with siblings:
        for entry in siblings:
            if unicodedata.normalize("NFC", entry.name).casefold() == key:
                raise FileExistsError(
                    f"目标存在 Windows 大小写冲突，拒绝覆盖：{entry.path}"
                )


def _acquire_destination_lock(
    parent: Path,
    target_name: str,
) -> tuple[Path, tuple[int, int]]:
    key = unicodedata.normalize("NFC", target_name).casefold().encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()[:24]
    path = parent / f".domain-pack-create-{digest}.lock"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise FileExistsError(
            f"同一目标正在创建或保留了安全锁，拒绝并发覆盖：{target_name}"
        ) from error
    except OSError as error:
        raise ValueError(f"无法创建方向仓库安全锁：{path}；{error}") from error
    try:
        token = f"cv-domain-pack-create.v1 {uuid.uuid4().hex}\n".encode("ascii")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
            identity = _file_identity(os.fstat(handle.fileno()))
        return path, identity
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _create_staging(
    parent: Path,
    target_name: str,
) -> tuple[Path, tuple[int, int]]:
    for _ in range(16):
        staging = parent / (
            f".{target_name}.domain-pack-staging-{uuid.uuid4().hex}"
        )
        try:
            staging.mkdir(mode=0o700)
        except FileExistsError:
            continue
        except OSError as error:
            raise ValueError(
                f"无法创建同父目录 staging：{staging}；{error}"
            ) from error
        metadata = staging.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or is_link_or_reparse(staging)
        ):
            raise ValueError(f"新建 staging 不是普通目录：{staging}")
        return staging, _file_identity(metadata)
    raise FileExistsError("无法取得唯一方向仓库 staging 名称")


def _publish_directory_no_replace(staging: Path, target: Path) -> None:
    _raise_if_target_conflicts(target)
    if os.name == "nt":
        try:
            os.rename(staging, target)
        except OSError as error:
            if os.path.lexists(target):
                raise FileExistsError(
                    f"目标在原子发布前已出现，拒绝覆盖：{target}"
                ) from error
            raise
        return
    if os.name == "posix":
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise RuntimeError("当前平台不支持目录原子不覆盖发布")
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,
            os.fsencode(staging),
            -100,
            os.fsencode(target),
            1,
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(
                f"目标在原子发布前已出现，拒绝覆盖：{target}"
            )
        raise OSError(
            error_number,
            os.strerror(error_number),
            str(target),
        )
    raise RuntimeError("当前平台不支持目录原子不覆盖发布")


def _cleanup_owned_staging(
    staging: Path,
    expected_identity: tuple[int, int],
    owned_entries: dict[str, tuple[str, tuple[int, int]]],
) -> BaseException | None:
    try:
        metadata = staging.lstat()
    except FileNotFoundError:
        return None
    except BaseException as error:
        return error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or is_link_or_reparse(staging)
        or _file_identity(metadata) != expected_identity
    ):
        return RuntimeError("staging 所有权或目录身份已经变化，拒绝删除")
    try:
        actual_entries = _snapshot_staging_entries(staging)
    except BaseException as error:
        return error
    unknown = sorted(set(actual_entries) - set(owned_entries))
    missing = sorted(set(owned_entries) - set(actual_entries))
    if unknown or missing:
        return RuntimeError(
            "staging 含未知或缺失子项，拒绝清理；"
            f"未知={unknown} 缺失={missing}"
        )
    for relative, expected in owned_entries.items():
        if actual_entries[relative] != expected:
            return RuntimeError(
                f"staging 子项身份发生变化，拒绝清理：{relative}"
            )
    file_paths = sorted(
        (
            relative
            for relative, (kind, _) in owned_entries.items()
            if kind == "file"
        ),
        key=lambda value: len(PurePosixPath(value).parts),
        reverse=True,
    )
    directory_paths = sorted(
        (
            relative
            for relative, (kind, _) in owned_entries.items()
            if kind == "directory"
        ),
        key=lambda value: len(PurePosixPath(value).parts),
        reverse=True,
    )
    for relative in (*file_paths, *directory_paths):
        kind, identity = owned_entries[relative]
        path = staging.joinpath(*PurePosixPath(relative).parts)
        try:
            current = path.lstat()
        except BaseException as error:
            return error
        if (
            is_link_or_reparse(path)
            or _file_identity(current) != identity
            or (kind == "file" and not stat.S_ISREG(current.st_mode))
            or (kind == "directory" and not stat.S_ISDIR(current.st_mode))
        ):
            return RuntimeError(
                f"staging 子项删除前身份发生变化：{relative}"
            )
        try:
            if kind == "file":
                path.unlink()
            else:
                path.rmdir()
        except BaseException as error:
            return error
    try:
        current_root = staging.lstat()
        if (
            is_link_or_reparse(staging)
            or not stat.S_ISDIR(current_root.st_mode)
            or _file_identity(current_root) != expected_identity
        ):
            return RuntimeError("staging 根目录删除前身份发生变化")
        staging.rmdir()
    except BaseException as error:
        return error
    return None


def _snapshot_staging_entries(
    staging: Path,
) -> dict[str, tuple[str, tuple[int, int]]]:
    entries: dict[str, tuple[str, tuple[int, int]]] = {}

    def visit(directory: Path, prefix: tuple[str, ...]) -> None:
        with os.scandir(directory) as children:
            for child in children:
                path = directory / child.name
                relative_parts = (*prefix, child.name)
                relative = PurePosixPath(*relative_parts).as_posix()
                if child.is_symlink() or is_link_or_reparse(path):
                    raise RuntimeError(
                        f"staging 含未知 link/reparse，拒绝清理：{relative}"
                    )
                metadata = path.lstat()
                if stat.S_ISREG(metadata.st_mode):
                    kind = "file"
                elif stat.S_ISDIR(metadata.st_mode):
                    kind = "directory"
                else:
                    raise RuntimeError(
                        f"staging 含未知特殊子项，拒绝清理：{relative}"
                    )
                entries[relative] = (kind, _file_identity(metadata))
                if kind == "directory":
                    visit(path, relative_parts)

    visit(staging, ())
    return entries


def _write_temporary_manifest(
    parent: Path,
    payload: dict[str, Any],
) -> tuple[Path, tuple[int, int]]:
    descriptor, name = tempfile.mkstemp(
        prefix=".domain-pack-codebase-",
        suffix=".json",
        dir=parent,
    )
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            encoded = (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            identity = _file_identity(os.fstat(handle.fileno()))
        return path, identity
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _unlink_owned_file(path: Path, expected_identity: tuple[int, int]) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        return
    if (
        stat.S_ISREG(metadata.st_mode)
        and not is_link_or_reparse(path)
        and _file_identity(metadata) == expected_identity
    ):
        try:
            path.unlink()
        except OSError:
            pass


def _safe_existing_directory(path: Path, label: str) -> Path:
    candidate = _absolute_lexical_path(Path(path).expanduser())
    for current in (candidate, *candidate.parents):
        if os.path.lexists(current) and is_link_or_reparse(current):
            raise ValueError(
                f"{label} 及其父路径不得包含 link/reparse：{current}"
            )
    if (
        not os.path.lexists(candidate)
        or is_link_or_reparse(candidate)
        or not candidate.is_dir()
    ):
        raise ValueError(f"{label} 必须是普通目录：{candidate}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"无法解析{label}：{candidate}；{error}") from error
    if not resolved.is_dir() or is_link_or_reparse(resolved):
        raise ValueError(f"{label} 必须是普通目录：{resolved}")
    return resolved


def _absolute_lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _text(
    value: object,
    label: str,
    *,
    maximum: int = MAX_TEXT,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(
            unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        raise ValueError(
            f"{label} 必须是 1..{maximum} 字符的规范非空文本"
        )
    return value


def _allowed_license(value: object, label: str) -> str:
    license_name = _text(value, label)
    if license_name not in ALLOWED_LICENSES:
        allowed = "/".join(sorted(ALLOWED_LICENSES))
        raise ValueError(f"{label} 不在许可证 allowlist：{allowed}")
    return license_name


def _https_url(value: object) -> str:
    url = _text(value, "source_references url")
    if not url.startswith("https://") or "\\" in url:
        raise ValueError("source_references url 必须是规范 https URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError(
            "source_references url 必须是规范 https URL"
        ) from error
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise ValueError("source_references url 必须是规范 https URL")
    try:
        parsed.hostname.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError(
            "source_references url 主机名必须是规范 ASCII"
        ) from error
    canonical_host = parsed.hostname.lower()
    if ":" in canonical_host:
        canonical_host = f"[{canonical_host}]"
    canonical_netloc = (
        canonical_host
        if port is None
        else f"{canonical_host}:{port}"
    )
    if (
        parsed.netloc != canonical_netloc
        or urlunsplit(parsed) != url
    ):
        raise ValueError("source_references url 必须是规范 https URL")
    return url


def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino
