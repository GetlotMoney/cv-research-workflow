from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "cv-experiment-workflow" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from workflow_core.domain_packs import (  # noqa: E402
    DIRECTIONS,
    DOMAIN_PACK_REGISTRY_SCHEMA,
    DOMAIN_PACK_SCHEMA,
    SEMANTIC_VERSION,
    is_link_or_reparse,
    validate_domain_pack,
)


PACKS_ROOT = (
    ROOT
    / "skills"
    / "cv-experiment-workflow"
    / "assets"
    / "domain-packs"
)
REGISTRY_FIELDS = {"schema", "packs"}
REGISTRY_ENTRY_FIELDS = {
    "id",
    "version",
    "template_id",
    "primary_direction",
    "directory",
}
PROVENANCE_FIELDS = {"license", "source_references"}
CACHE_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "node_modules",
}
ALLOWED_SUFFIXES = {
    ".cfg",
    ".csv",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
}
ALLOWED_SUFFIXLESS = {"LICENSE"}
ALLOWED_DOTFILES = {".gitattributes", ".gitignore"}
REPARSE_POINT = 0x400


def _read_json(path: Path, label: str) -> dict[str, Any]:
    _require_plain_file(path, label)
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"非有限数字：{value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} 必须是严格 UTF-8 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} 必须是 JSON object")
    return payload


def _require_plain_file(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} 不存在：{path}") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or bool(getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT)
        or is_link_or_reparse(path)
    ):
        raise ValueError(f"{label} 必须是普通文件，不能是 link/reparse：{path}")
    if getattr(metadata, "st_nlink", 1) != 1:
        raise ValueError(f"{label} 不能是 hardlink：{path}")
    return metadata


def _require_plain_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} 不存在：{path}") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or bool(getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT)
        or is_link_or_reparse(path)
    ):
        raise ValueError(f"{label} 必须是普通目录，不能是 link/reparse：{path}")


def _scan_payload(payload: Path) -> list[Path]:
    _require_plain_directory(payload, "payload")
    files: list[Path] = []

    def visit(directory: Path) -> None:
        _require_plain_directory(directory, "payload 子目录")
        with os.scandir(directory) as entries:
            children = sorted(entries, key=lambda item: item.name)
        for entry in children:
            path = directory / entry.name
            if entry.name.casefold() in CACHE_NAMES:
                raise ValueError(f"payload 含缓存或危险目录：{path}")
            if entry.is_symlink() or is_link_or_reparse(path):
                raise ValueError(f"payload 含 link/reparse：{path}")
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                visit(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"payload 含未知特殊项：{path}")
            _require_plain_file(path, "payload 文件")
            if path.name in ALLOWED_DOTFILES or path.name in ALLOWED_SUFFIXLESS:
                pass
            elif path.name.startswith("."):
                raise ValueError(f"payload 含未知危险隐藏文件：{path}")
            elif path.suffix.casefold() not in ALLOWED_SUFFIXES:
                raise ValueError(
                    f"payload 含未知危险或二进制扩展名：{path}"
                )
            content = path.read_bytes()
            if b"\x00" in content:
                raise ValueError(f"payload 含二进制 NUL：{path}")
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(f"payload 文件不是 UTF-8 文本：{path}") from error
            files.append(path)

    visit(payload)
    return sorted(files, key=lambda path: path.relative_to(payload).as_posix())


def _build_manifest(
    pack_root: Path,
    registry_entry: dict[str, Any],
) -> dict[str, object]:
    if (
        not isinstance(registry_entry, dict)
        or set(registry_entry) != REGISTRY_ENTRY_FIELDS
    ):
        raise ValueError("registry 条目字段无效")
    payload = pack_root / "payload"
    contract = _read_json(payload / "domain-pack.json", "payload/domain-pack.json")
    for key in ("id", "version", "template_id", "primary_direction"):
        if contract.get(key) != registry_entry.get(key):
            raise ValueError(
                f"registry 与 payload/domain-pack.json 身份不一致：{key}"
            )
    if not PROVENANCE_FIELDS.issubset(contract):
        raise ValueError(
            "payload/domain-pack.json 必须明确 license 和 source_references provenance"
        )
    license_name = contract["license"]
    source_references = contract["source_references"]
    if not isinstance(license_name, str) or not isinstance(source_references, list):
        raise ValueError("方向包 provenance 元数据类型无效")
    files: list[dict[str, object]] = []
    for path in _scan_payload(payload):
        content = path.read_bytes()
        files.append({
            "path": path.relative_to(payload).as_posix(),
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    return {
        "schema": DOMAIN_PACK_SCHEMA,
        "id": registry_entry["id"],
        "version": registry_entry["version"],
        "template_id": registry_entry["template_id"],
        "primary_direction": registry_entry["primary_direction"],
        "license": license_name,
        "source_references": source_references,
        "files": files,
    }


def _validate_registry_entry(
    registry_entry: dict[str, Any],
) -> dict[str, str]:
    if (
        not isinstance(registry_entry, dict)
        or set(registry_entry) != REGISTRY_ENTRY_FIELDS
    ):
        raise ValueError("registry 条目字段无效")
    pack_id = registry_entry.get("id")
    version = registry_entry.get("version")
    template_id = registry_entry.get("template_id")
    primary_direction = registry_entry.get("primary_direction")
    directory = registry_entry.get("directory")
    if pack_id not in DIRECTIONS:
        raise ValueError("registry id 无效")
    if (
        not isinstance(version, str)
        or SEMANTIC_VERSION.fullmatch(version) is None
    ):
        raise ValueError("registry version 无效")
    if template_id != f"PACK-{pack_id.upper()}":
        raise ValueError("registry template_id 与 id 身份不一致")
    if primary_direction != pack_id:
        raise ValueError("registry primary_direction 与 id 身份不一致")
    if directory != f"{pack_id}-v{version}":
        raise ValueError("registry directory 必须精确等于 <id>-v<version>")
    return {
        "id": pack_id,
        "version": version,
        "template_id": template_id,
        "primary_direction": primary_direction,
        "directory": directory,
    }


def _resolve_pack_root(
    packs_root: Path,
    registry_entry: dict[str, Any],
) -> tuple[Path, dict[str, str]]:
    selected = _validate_registry_entry(registry_entry)
    lexical_root = Path(packs_root)
    _require_plain_directory(lexical_root, "方向包根目录")
    root = lexical_root.resolve(strict=True)
    candidate = root / selected["directory"]
    _require_plain_directory(candidate, "方向包目录")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("registry directory 解析后逃逸方向包根目录") from error
    _require_plain_file(resolved / "pack.json", "pack.json")
    return resolved, selected


def _rebuild_one(
    packs_root: Path,
    registry_entry: dict[str, Any],
) -> dict[str, object]:
    pack_root, selected = _resolve_pack_root(packs_root, registry_entry)
    manifest = _build_manifest(pack_root, selected)
    _write_manifest(pack_root, manifest)
    return manifest


def _write_manifest(pack_root: Path, manifest: dict[str, object]) -> None:
    target = pack_root / "pack.json"
    _require_plain_file(target, "pack.json")
    original = target.read_bytes()
    encoded = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    temporary = pack_root / f".pack.json.rebuild-{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            validate_domain_pack(pack_root)
        except BaseException:
            restore = pack_root / f".pack.json.restore-{uuid.uuid4().hex}.tmp"
            restore_descriptor = os.open(restore, flags, 0o600)
            with os.fdopen(restore_descriptor, "wb") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(restore, target)
            raise
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从 registry 与每包 provenance 重建精确 size/SHA-256 清单"
    )
    parser.add_argument("--pack", required=True)
    arguments = parser.parse_args()
    registry = _read_json(PACKS_ROOT / "registry.json", "registry.json")
    if (
        set(registry) != REGISTRY_FIELDS
        or registry.get("schema") != DOMAIN_PACK_REGISTRY_SCHEMA
        or not isinstance(registry.get("packs"), list)
    ):
        raise SystemExit("方向包 registry.json 结构无效")
    matches = [
        item
        for item in registry["packs"]
        if isinstance(item, dict) and item.get("id") == arguments.pack
    ]
    if len(matches) != 1:
        raise SystemExit(f"方向包不存在或版本不唯一：{arguments.pack}")
    _rebuild_one(PACKS_ROOT, matches[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
