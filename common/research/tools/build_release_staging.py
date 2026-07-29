#!/usr/bin/env python3
"""从两个只读源码仓库构建一个新的、可复查的发布暂存目录。"""

from __future__ import annotations

import argparse
import ast
import errno
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tokenize
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from typing import Iterable


MAX_RELEASE_BYTES = 50 * 1024 * 1024
MANIFEST_NAME = "release-manifest.json"
EXCLUSIONS_NAME = "release-exclusions.json"

_RESEARCH_TOP_FILES = {
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "CHANGELOG.md",
    "LICENSE",
    "README.md",
    "REPOSITORY_INDEX.json",
    "REPOSITORY_INDEX.md",
    "SECURITY.md",
    "pyproject.toml",
}
_RESEARCH_TOP_DIRS = {"docs", "skills", "tests", "tools"}
_PAPERFLOW_TOP_FILES = {
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "pytest.ini",
    "requirements-v2.txt",
}
_PAPERFLOW_TOP_DIRS = {
    "docs",
    "paperflow_v2",
    "paper_writer_tool",
    "tests",
    "tests_v2",
    "tools",
}
_PAPERFLOW_DATA_PREFIXES = {
    ("data", "model_configs"),
    ("data", "paper_specs"),
}


def safe_git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for variable in tuple(environment):
        if variable.upper().startswith("GIT_"):
            environment.pop(variable, None)
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ATTR_NOSYSTEM": "1",
        }
    )
    return environment


_TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".md",
    ".ps1",
    ".py",
    ".sha256",
    ".sql",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_TEXT_NAMES = {".gitattributes", ".gitignore", "LICENSE"}
_FORBIDDEN_SUFFIXES = {
    ".7z",
    ".bin",
    ".ckpt",
    ".db",
    ".dll",
    ".exe",
    ".gif",
    ".gz",
    ".jpeg",
    ".jpg",
    ".log",
    ".npy",
    ".npz",
    ".onnx",
    ".pdf",
    ".pickle",
    ".pkl",
    ".png",
    ".pt",
    ".pth",
    ".pyc",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tiff",
    ".webp",
    ".zip",
}
_EXCLUDED_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "artifacts",
    "cache",
    "caches",
    "checkpoints",
    "datasets",
    "dist",
    "logs",
    "models",
    "node_modules",
    "output",
    "outputs",
    "runs",
    "venv",
    "weights",
}
_PAPERFLOW_EXCLUDED_DATA = {
    "evaluation",
    "paper_sources",
    "project_facts",
    "result_inputs",
    "shared_library",
    "writing_inputs",
}
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_DRIVE_ABSOLUTE = re.compile(
    r"(?<![A-Za-z0-9])(?:\\\\[?.]\\)?[A-Za-z]:[\\/]"
)
_WINDOWS_DEVICE_ABSOLUTE = re.compile(
    r"(?<![A-Za-z0-9])\\\\[?.]\\"
    r"(?:[A-Za-z]:[\\/]|PIPE[\\/][^\\/\s\"']+)"
)
_WINDOWS_UNC_ABSOLUTE = re.compile(
    r"(?<![:/\\])(?:\\\\|//)[^\\/\s\"'${}@{}]+"
    r"[\\/][^\\/\s\"'${}@{}]+"
)
_POSIX_PRIVATE_ABSOLUTE = re.compile(
    r"(?<![A-Za-z0-9:])/(?:home|Users|root|var|opt)(?:/|(?=$))"
)
_POSIX_MOUNT_ABSOLUTE = re.compile(
    r"(?<![A-Za-z0-9:])/mnt/[A-Za-z](?:/|(?=$))"
)
_PATH_TO_EXAMPLE = re.compile(
    r"(?<![A-Za-z0-9])(?:\\\\[?.]\\)?[A-Za-z]:[\\/]+"
    r"path[\\/]+to[\\/]+"
    r"(?:your-research-project|paperflow(?:[\\/]+runtime_v2)?|"
    r"cuda-env[\\/]+python\.exe)"
    r"(?=$|[\s\"'`])"
)
_USERS_ELLIPSIS_EXAMPLE = re.compile(
    r"(?<![A-Za-z0-9])(?:\\\\[?.]\\)?[A-Za-z]:[\\/]+"
    r"Users[\\/]+\.\.\.(?=$|[\s\"'`])"
)
_REPLACE_REFERENCE_EXAMPLE = re.compile(
    r"(?<![A-Za-z0-9])(?:\\\\[?.]\\)?[A-Za-z]:[\\/]+"
    r"请替换[\\/]+reference\.pdf(?=$|[\s\"'`])"
)
_DOMAIN_DATA_EXAMPLE = re.compile(
    r"(?<![A-Za-z0-9])(?:\\\\[?.]\\)?[A-Za-z]:[\\/]+"
    r"(?:my-data(?:[\\/]+val[\\/]+cat[\\/]+001\.jpg)?|"
    r"gzsl[\\/]+data\.npz|seg-data|sr-data)"
    r"(?=$|[\s\"'`])"
)
_PAPER_INGEST_EXAMPLE = re.compile(
    r"(?<![A-Za-z0-9])(?:\\\\[?.]\\)?[A-Za-z]:[\\/]+"
    r"待测试[\\/]+new-paper\.pdf(?=$|[\s\"'`])"
)
_DEVICE_ELLIPSIS_EXAMPLE = re.compile(
    r"(?<![A-Za-z0-9])(?:[\\/]{2,})\?[\\/]+"
    r"[A-Za-z]:[\\/]+\.\.\.(?=$|[\s\"'`])"
)
_DRIVE_ELLIPSIS_EXAMPLE = re.compile(
    r"(?<![A-Za-z0-9])(?:\\\\[?.]\\)?[A-Za-z]:[\\/]+"
    r"\.\.\.(?=$|[\s\"'`])"
)
_SERVER_SHARE_EXAMPLE = re.compile(
    r"(?<![A-Za-z0-9])(?:[\\/]{2,})server[\\/]+share"
    r"(?=$|[\s\"'`])"
)
_PAPERFLOW_IMPORT_COMMAND_EXAMPLE = re.compile(
    r"(?<![A-Za-z0-9])(?:\\\\[?.]\\)?[A-Za-z]:[\\/]+"
    r"(?:papers[\\/]+my-paper|deliveries[\\/]+PKG-\d+|paperflow-runtime)"
    r"(?=$|[\s\"'`])"
)
_SHORT_DRIVE_FRAGMENT_EXAMPLE = re.compile(
    r"(?<![A-Za-z0-9])[Qq]:[\\/]an(?=$|[\s\"'`])"
)
_HISTORICAL_DOC_GROUPS = {
    "reviews": "历史审核记录不进入可移植发布包",
    "superpowers": "历史计划、规格和审核过程记录不进入可移植发布包",
    "workflow_audit": "历史运行与渲染审计不进入可移植发布包",
    "html_archive": "历史 HTML 归档不进入可移植发布包",
    "deployment": "本机部署记录不进入可移植发布包",
}
_LOCAL_PAPERFLOW_DOCS = {
    "paperflow_workbench_guide.md",
    "research_paperflow_guide.md",
}
_LOCAL_PAPERFLOW_TOOL_PREFIXES = (
    "build_bilingual_",
    "build_dvsie_",
    "build_recovered_",
    "build_v1_original_",
    "download_unlimited_ocr",
    "start_paperflow_unlimited_ocr_",
    "start_unlimited_ocr_",
    "verify_dvsie_",
    "verify_recovered_",
)
_LOCAL_RESEARCH_AUDIT_TOOLS = {
    "build_audit_package.py",
}
_PORTABLE_SOURCE_LITERAL_VALUES = {
    "paperflow_v2/parsers.py": (
        "C:" + "/" + "/".join(("Windows", "Fonts", "msyh.ttc")),
        "C:" + "/" + "/".join(("Windows", "Fonts", "arial.ttf")),
    ),
}

DOWNLOAD_URLS = [
    {
        "id": "pytorch",
        "purpose": "六方向模板运行所需 PyTorch；发布物不捆绑依赖。",
        "url": "https://pytorch.org/get-started/locally/",
        "kind": "landing_page",
        "version": None,
        "revision": None,
        "sha256": None,
    },
    {
        "id": "torchvision",
        "purpose": "可选正式视觉后端。",
        "url": "https://pytorch.org/vision/stable/index.html",
        "kind": "landing_page",
        "version": None,
        "revision": None,
        "sha256": None,
    },
    {
        "id": "pycocotools",
        "purpose": "可选 COCO 检测与分割评估后端。",
        "url": "https://github.com/ppwwyyxx/cocoapi",
        "kind": "landing_page",
        "version": None,
        "revision": None,
        "sha256": None,
    },
    {
        "id": "coco-data",
        "purpose": "可选真实检测与分割数据；大数据不进入发布物。",
        "url": "https://cocodataset.org/#download",
        "kind": "landing_page",
        "version": None,
        "revision": None,
        "sha256": None,
    },
    {
        "id": "docling",
        "purpose": "PaperFlow 可选 PDF 版面解析后端。",
        "url": "https://github.com/docling-project/docling",
        "kind": "landing_page",
        "version": None,
        "revision": None,
        "sha256": None,
    },
    {
        "id": "bge-m3",
        "purpose": "PaperFlow 可选本地语义检索模型。",
        "url": "https://huggingface.co/BAAI/bge-m3",
        "kind": "landing_page",
        "version": None,
        "revision": None,
        "sha256": None,
    },
    {
        "id": "rapidocr",
        "purpose": "PaperFlow 可选扫描页 OCR 后端。",
        "url": "https://github.com/RapidAI/RapidOCR",
        "kind": "landing_page",
        "version": None,
        "revision": None,
        "sha256": None,
    },
]


def has_reparse_flag(metadata: object) -> bool:
    """兼容 Windows 和测试替身地识别 reparse 属性。"""

    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    return bool(attributes & flag)


def is_link_or_reparse(path: Path) -> bool:
    metadata = os.lstat(path)
    return stat.S_ISLNK(metadata.st_mode) or has_reparse_flag(metadata)


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _cross_api_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    # Windows 的 path-stat 与 handle-fstat 对 st_ctime_ns 的映射不同；
    # ctime 仍分别在 lstat 前后、fstat 前后比较，跨 API 只比较其余身份。
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def stable_read_file(
    path: Path,
    *,
    limit: int = MAX_RELEASE_BYTES,
) -> bytes:
    """只读取一次身份稳定的普通文件，拒绝 link/reparse 与并发替换。"""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("稳定读取上限必须是非负整数")
    lexical = Path(os.path.abspath(path))
    before = os.lstat(lexical)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or has_reparse_flag(before)
    ):
        raise ValueError(f"稳定读取只接受普通文件，拒绝 link/reparse：{lexical}")
    if int(before.st_size) > limit:
        raise ValueError(
            f"稳定读取文件体积 {before.st_size} 超过上限 {limit}：{lexical}"
        )

    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(lexical, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or has_reparse_flag(opened)
            or _cross_api_identity(before) != _cross_api_identity(opened)
        ):
            raise ValueError(f"文件身份在打开前发生变化：{lexical}")
        chunks: list[bytes] = []
        captured = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            captured += len(chunk)
            if captured > limit:
                raise ValueError(
                    f"稳定读取文件在读取期间超过上限 {limit}：{lexical}"
                )
            chunks.append(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    final = os.lstat(lexical)
    if (
        not stat.S_ISREG(final.st_mode)
        or stat.S_ISLNK(final.st_mode)
        or has_reparse_flag(final)
        or _file_identity(opened) != _file_identity(after)
        or _file_identity(before) != _file_identity(final)
        or _cross_api_identity(after) != _cross_api_identity(final)
        or len(content) != int(after.st_size)
    ):
        raise ValueError(f"文件在读取期间发生身份、体积或时间变化：{lexical}")
    return content


def contains_private_absolute_path(text: str) -> bool:
    """识别 Windows、UNC、设备路径与常见私有 POSIX 绝对路径。"""

    return bool(
        _WINDOWS_DRIVE_ABSOLUTE.search(text)
        or _WINDOWS_DEVICE_ABSOLUTE.search(text)
        or _WINDOWS_UNC_ABSOLUTE.search(text)
        or _POSIX_PRIVATE_ABSOLUTE.search(text)
        or _POSIX_MOUNT_ABSOLUTE.search(text)
    )


def _is_test_fixture_path(relative: str) -> bool:
    parts = tuple(
        part.casefold()
        for part in PurePosixPath(relative).parts
    )
    return bool(parts) and parts[0] in {"tests", "tests_v2"}


def _mask_portable_path_examples(
    text: str,
    component: str,
    relative: str,
) -> str:
    pure = PurePosixPath(relative)
    parts = tuple(part.casefold() for part in pure.parts)
    normalized = pure.as_posix().casefold()
    patterns: tuple[re.Pattern[str], ...] = ()
    if component == "research" and normalized == "readme.md":
        patterns = (_PATH_TO_EXAMPLE,)
    elif (
        component == "research"
        and len(parts) >= 2
        and parts[:2] == ("docs", "integration")
    ):
        patterns = (_USERS_ELLIPSIS_EXAMPLE,)
    elif (
        component == "research"
        and normalized == "skills/cv-experiment-workflow/assets/console/app.js"
    ):
        patterns = (_REPLACE_REFERENCE_EXAMPLE,)
    elif (
        component == "research"
        and pure.name.casefold() == "readme.md"
        and "assets/domain-packs/" in normalized
    ):
        patterns = (_DOMAIN_DATA_EXAMPLE,)
    elif component == "paperflow" and normalized == "readme.md":
        patterns = (_PAPER_INGEST_EXAMPLE,)
    elif (
        component == "paperflow"
        and len(parts) >= 2
        and parts[:2] == ("docs", "integration")
    ):
        patterns = (
            _DEVICE_ELLIPSIS_EXAMPLE,
            _DRIVE_ELLIPSIS_EXAMPLE,
            _USERS_ELLIPSIS_EXAMPLE,
            _SERVER_SHARE_EXAMPLE,
            _PAPERFLOW_IMPORT_COMMAND_EXAMPLE,
            _SHORT_DRIVE_FRAGMENT_EXAMPLE,
        )
    elif (
        component == "paperflow"
        and len(parts) == 2
        and parts[0] == "docs"
        and parts[1].startswith("cv_paper_workflow_")
        and pure.suffix.casefold() == ".html"
    ):
        patterns = (_PAPER_INGEST_EXAMPLE,)
    masked = text
    for pattern in patterns:
        masked = pattern.sub("<portable-path-example>", masked)
    return masked


def _mask_known_portable_source_literals(
    text: str,
    relative: str,
) -> str:
    allowed_values = _PORTABLE_SOURCE_LITERAL_VALUES.get(
        PurePosixPath(relative).as_posix().casefold(),
        (),
    )
    if not allowed_values:
        return text
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (IndentationError, SyntaxError, tokenize.TokenError):
        return text
    masked_tokens: list[tokenize.TokenInfo] = []
    for token in tokens:
        replacement = token
        if token.type == tokenize.STRING:
            try:
                value = ast.literal_eval(token.string)
            except (SyntaxError, ValueError):
                value = None
            if isinstance(value, str) and value in allowed_values:
                replacement = tokenize.TokenInfo(
                    token.type,
                    repr("<portable-system-path>"),
                    token.start,
                    token.end,
                    token.line,
                )
        masked_tokens.append(replacement)
    return tokenize.untokenize(masked_tokens)


def content_contains_private_path(
    text: str,
    *,
    component: str,
    relative: str,
    private_roots: Iterable[Path] = (),
) -> bool:
    """区分真实环境路径与测试、说明中的可移植合成路径。"""

    inspected = (
        _mask_known_portable_source_literals(text, relative)
        if component == "paperflow"
        else text
    )
    sensitive_roots = [Path.home(), *(Path(item) for item in private_roots)]
    if any(_text_contains_marker(inspected, root) for root in sensitive_roots):
        return True
    if _is_test_fixture_path(relative):
        return False
    return contains_private_absolute_path(
        _mask_portable_path_examples(
            inspected,
            component,
            relative,
        )
    )


def parse_strict_json_object(content: bytes, label: str) -> dict[str, object]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"{label} 不允许非有限 JSON 数值：{value}")

    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} 含重复字段：{key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            content.decode("utf-8", errors="strict"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} 必须是严格 UTF-8 JSON object") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} 顶层必须是 JSON object")
    return value


def _windows_normalized(path: Path) -> str:
    return str(path.resolve(strict=False)).replace("/", "\\").rstrip("\\").casefold()


def _is_within(candidate: Path, root: Path) -> bool:
    candidate_text = _windows_normalized(candidate)
    root_text = _windows_normalized(root)
    return candidate_text == root_text or candidate_text.startswith(root_text + "\\")


def _existing_ancestors(path: Path) -> Iterable[Path]:
    # 不调用 resolve，否则一个 junction/symlink 祖先会先被展开，随后反而
    # 看不到它本身的 reparse 属性。
    current = Path(os.path.abspath(path))
    seen: set[str] = set()
    while True:
        marker = _windows_normalized(current)
        if marker in seen:
            break
        seen.add(marker)
        if os.path.lexists(current):
            yield current
        if current.parent == current:
            break
        current = current.parent


def validate_output_destination(
    output: Path,
    research_root: Path,
    paperflow_root: Path,
    *,
    protected_roots: Iterable[Path] = (),
) -> Path:
    """确认目标是一个尚不存在、且不落入任何受保护范围的新目录。"""

    target = Path(output).resolve(strict=False)
    if os.path.lexists(target):
        raise ValueError(f"发布目标已存在，拒绝覆盖：{target}")
    for protected in (Path(item) for item in protected_roots):
        if _is_within(target, protected):
            raise ValueError(f"发布目标落入显式受保护根目录：{protected}")
    for source in (Path(research_root), Path(paperflow_root)):
        if _is_within(target, source):
            raise ValueError("发布目标不能写入科研或 PaperFlow 源码根目录")
    for ancestor in _existing_ancestors(target.parent):
        if is_link_or_reparse(ancestor):
            raise ValueError(f"发布目标祖先不得包含 link/reparse：{ancestor}")
    return target


def validate_new_json_output(
    output: Path,
    *,
    forbidden_roots: Iterable[Path] = (),
) -> Path:
    """验证一次性 JSON 报告目标；不创建父目录，也不允许覆盖。"""

    target = Path(os.path.abspath(output))
    if os.path.lexists(target):
        raise ValueError(f"JSON 报告目标已存在，拒绝覆盖：{target}")
    validate_release_paths([target.name])
    for protected in (Path(item) for item in forbidden_roots):
        if _is_within(target, protected):
            raise ValueError(f"JSON 报告目标落入受保护或源码范围：{protected}")
    parent = target.parent
    if not parent.is_dir():
        raise ValueError("JSON 报告父目录必须预先存在且是普通目录")
    for ancestor in _existing_ancestors(parent):
        if is_link_or_reparse(ancestor):
            raise ValueError(f"JSON 报告祖先不得包含 link/reparse：{ancestor}")
    return target


def write_new_json_report(
    output: Path,
    value: object,
    *,
    forbidden_roots: Iterable[Path] = (),
) -> Path:
    """以同目录硬链接发布 JSON，原子保证目标不存在时才创建。"""

    target = validate_new_json_output(
        output,
        forbidden_roots=forbidden_roots,
    )
    content = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # hard link 与目标在同一目录；目标已存在时 Windows/POSIX 都会失败，
        # 因而不会像 os.replace 那样覆盖用户文件。
        os.link(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def validate_release_paths(paths: Iterable[str]) -> list[str]:
    """按 Windows 最严格路径规则验证相对路径，并阻断大小写碰撞。"""

    result: list[str] = []
    folded: dict[str, str] = {}
    for raw in paths:
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError("发布路径必须是非空字符串")
        if "\\" in raw:
            raise ValueError(f"发布路径必须使用正斜杠：{raw}")
        if raw.startswith("/") or _DRIVE_PATH.match(raw):
            raise ValueError(f"发布路径不得是绝对路径：{raw}")
        pure = PurePosixPath(raw)
        if any(part in {"", ".", ".."} for part in pure.parts):
            raise ValueError(f"发布路径不得逃逸或包含空层级：{raw}")
        if pure.as_posix() != raw:
            raise ValueError(f"发布路径不是规范相对路径：{raw}")
        if unicodedata.normalize("NFC", raw) != raw:
            raise ValueError(f"发布路径必须使用 Unicode NFC：{raw}")
        for part in pure.parts:
            if part.endswith((" ", ".")) or ":" in part:
                raise ValueError(f"发布路径不符合 Windows 文件名规则：{raw}")
            if any(ord(character) < 32 for character in part):
                raise ValueError(f"发布路径包含控制字符：{raw}")
            stem = part.split(".", 1)[0].rstrip(" .").upper()
            if stem in _WINDOWS_RESERVED:
                raise ValueError(f"发布路径使用 Windows 保留名称：{raw}")
        key = unicodedata.normalize("NFC", raw).casefold()
        if key in folded:
            raise ValueError(
                f"发布路径在 Windows 上发生大小写碰撞：{folded[key]} / {raw}"
            )
        folded[key] = raw
        result.append(raw)
    return result


def enforce_size_limit(total_bytes: int, limit: int = MAX_RELEASE_BYTES) -> None:
    if total_bytes > limit:
        raise ValueError(
            f"发布体积 {total_bytes} 字节超过上限 {limit} 字节（默认 50 MiB）"
        )


def _git_tracked_files(root: Path) -> list[str]:
    environment = safe_git_environment()
    top_level = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=False,
        timeout=30,
        env=environment,
    )
    if top_level.returncode != 0:
        detail = top_level.stderr.decode(
            "utf-8",
            errors="replace",
        ).strip()
        raise ValueError(
            f"source root is not a readable Git repository: {root}: {detail}"
        )
    try:
        reported_root = Path(
            top_level.stdout.decode("utf-8", errors="strict").strip()
        ).resolve(strict=True)
    except (OSError, UnicodeError) as error:
        raise ValueError("Git top-level must be a valid UTF-8 path") from error
    if reported_root != root.resolve(strict=True):
        raise ValueError("Git top-level does not match the release source root")
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        check=False,
        timeout=30,
        env=environment,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"源码根目录不是可读取的 Git 仓库：{root}；{detail}")
    try:
        paths = [
            value
            for value in result.stdout.decode("utf-8", errors="strict").split("\0")
            if value
        ]
    except UnicodeDecodeError as error:
        raise ValueError("Git 跟踪路径必须是 UTF-8") from error
    return validate_release_paths(paths)


def _known_excluded_path(component: str, relative: str) -> str | None:
    pure = PurePosixPath(relative)
    lower_parts = tuple(part.casefold() for part in pure.parts)
    normalized = pure.as_posix().casefold()
    if component == "paperflow" and normalized == "security.md":
        return "用户专属安全策略文件不读取、不复制，也不进入通用候选包"
    if pure.name.casefold() == "tech_stack_history.md":
        return "技术演进与机器部署历史不进入可移植发布包"
    if len(lower_parts) >= 2 and lower_parts[0] == "docs":
        group = lower_parts[1]
        if group in _HISTORICAL_DOC_GROUPS:
            return _HISTORICAL_DOC_GROUPS[group]
        if len(lower_parts) >= 3 and lower_parts[:3] == (
            "docs",
            "diagrams",
            "audit",
        ):
            return "渲染审计记录不进入可移植发布包"
        if component == "paperflow" and group == "skills":
            return "带本机安装路径的 Skill 文档镜像不进入可移植发布包"
        if (
            component == "paperflow"
            and pure.name.casefold() in _LOCAL_PAPERFLOW_DOCS
        ):
            return "带本机源码定位的工作台或联调说明不进入可移植发布包"
    if component == "paperflow" and normalized.startswith("tools/"):
        tool_name = pure.name.casefold()
        if tool_name.startswith(_LOCAL_PAPERFLOW_TOOL_PREFIXES):
            return "本机安装、下载、冒烟或项目专用生成工具不进入可移植发布包"
    if component == "paperflow" and lower_parts[:1] == ("tests",):
        return "旧版 PaperFlow tests 不属于当前 pytest.ini 指定的 tests_v2 必需测试"
    if (
        component == "research"
        and normalized.startswith("tools/")
        and pure.name.casefold() in _LOCAL_RESEARCH_AUDIT_TOOLS
    ):
        return "面向本机人工复核的历史审计包生成工具不进入通用发布包"
    if (
        component == "research"
        and normalized.startswith("docs/deliverables/")
        and "audit-package" in pure.name.casefold()
    ):
        return "历史人工审计包及其校验旁车文件不进入通用发布包"
    if any(part in _EXCLUDED_DIR_NAMES for part in lower_parts):
        return "运行目录、缓存、数据、模型或产物不进入发布物"
    suffix = pure.suffix.casefold()
    if suffix in _FORBIDDEN_SUFFIXES:
        return f"二进制、数据库、模型或运行产物后缀 {suffix} 不进入发布物"
    if component == "paperflow" and len(lower_parts) >= 2:
        if (
            lower_parts[0] == "data"
            and lower_parts[1] in _PAPERFLOW_EXCLUDED_DATA
        ):
            return "项目私有数据或运行生成目录不进入通用发布物"
        if (
            lower_parts[0] == "data"
            and lower_parts[1] == "model_configs"
            and ".local." in pure.name.casefold()
        ):
            return "本机专用配置含机器路径，不进入通用发布物"
    return None


def _allowed_source_path(component: str, relative: str) -> bool:
    pure = PurePosixPath(relative)
    if len(pure.parts) == 1:
        allowed = (
            _RESEARCH_TOP_FILES
            if component == "research"
            else _PAPERFLOW_TOP_FILES
        )
        return pure.name in allowed
    top = pure.parts[0]
    if component == "research":
        return top in _RESEARCH_TOP_DIRS
    if top in _PAPERFLOW_TOP_DIRS:
        return True
    return any(
        tuple(pure.parts[: len(prefix)]) == prefix
        for prefix in _PAPERFLOW_DATA_PREFIXES
    )


def _is_allowed_text_file(relative: str) -> bool:
    pure = PurePosixPath(relative)
    return pure.name in _TEXT_NAMES or pure.suffix.casefold() in _TEXT_SUFFIXES


def _walk_untracked_included_files(
    root: Path,
    component: str,
    tracked: set[str],
) -> list[str]:
    unknown: list[str] = []
    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in directory_names:
            child = current_path / name
            relative = child.relative_to(root).as_posix()
            if name.casefold() == ".git":
                continue
            if is_link_or_reparse(child):
                raise ValueError(f"源码不得包含 link/reparse：{component}/{relative}")
            if _known_excluded_path(component, relative):
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in file_names:
            child = current_path / name
            relative = child.relative_to(root).as_posix()
            if relative == ".git":
                continue
            if is_link_or_reparse(child):
                raise ValueError(f"源码不得包含 link/reparse：{component}/{relative}")
            if relative in tracked or _known_excluded_path(component, relative):
                continue
            unknown.append(relative)
    return sorted(unknown)


def _text_contains_marker(content: str, marker: Path) -> bool:
    def normalize(value: str) -> str:
        return re.sub(r"/+", "/", value.replace("\\", "/")).casefold()

    haystack = normalize(content)
    candidates = {
        str(marker),
        os.path.abspath(marker),
        str(marker.resolve(strict=False)),
    }
    return any(
        bool(needle)
        and normalize(needle) in haystack
        for needle in candidates
    )


def _copy_plan(
    root: Path,
    component: str,
    *,
    size_limit: int,
    protected_roots: Iterable[Path] = (),
) -> tuple[list[tuple[str, bytes]], list[dict[str, str]]]:
    lexical_source = Path(os.path.abspath(root))
    protected = tuple(Path(item) for item in protected_roots)
    if not os.path.lexists(lexical_source) or is_link_or_reparse(lexical_source):
        raise ValueError(f"{component} 源码根必须是普通目录")
    source = lexical_source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError(f"{component} 源码根必须是普通目录")
    tracked_list = _git_tracked_files(source)
    tracked = set(tracked_list)
    unknown = _walk_untracked_included_files(source, component, tracked)
    if unknown:
        raise ValueError(
            f"{component} 白名单区域含未知或未跟踪文件：{', '.join(unknown[:8])}"
        )

    included: list[tuple[str, bytes]] = []
    excluded: list[dict[str, str]] = []
    remaining = size_limit
    for relative in tracked_list:
        path = source.joinpath(*PurePosixPath(relative).parts)
        if not os.path.lexists(path):
            raise ValueError(f"Git 跟踪文件缺失：{component}/{relative}")
        if is_link_or_reparse(path):
            raise ValueError(f"Git 跟踪文件不得是 link/reparse：{component}/{relative}")
        if not path.is_file():
            raise ValueError(f"Git 跟踪项必须是普通文件：{component}/{relative}")
        reason = _known_excluded_path(component, relative)
        release_path = f"{component}/{relative}"
        if reason:
            excluded.append(
                {
                    "path": release_path,
                    "reason": reason,
                    "recovery": "保留在原 Git 仓库，或按 download_urls 重新取得外部内容",
                }
            )
            continue
        if not _allowed_source_path(component, relative):
            raise ValueError(f"{component} 出现固定白名单之外的未知文件：{relative}")
        if not _is_allowed_text_file(relative):
            raise ValueError(f"{component} 出现不允许的文件类型：{relative}")
        content_bytes = stable_read_file(path, limit=remaining)
        try:
            content = content_bytes.decode("utf-8", errors="strict")
        except UnicodeError as error:
            raise ValueError(f"发布源码必须是严格 UTF-8 文本：{relative}") from error
        if content_contains_private_path(
            content,
            component=component,
            relative=relative,
            private_roots=(
                source,
                lexical_source,
                *protected,
            ),
        ):
            raise ValueError(f"文件含机器绝对路径泄漏：{component}/{relative}")
        included.append((release_path, content_bytes))
        remaining -= len(content_bytes)
    return included, excluded


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_json_bytes(value))


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _ordinary_tree_state(root: Path) -> dict[str, tuple[int, str]]:
    state: dict[str, tuple[int, str]] = {}
    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        for name in list(directory_names):
            child = current_path / name
            if is_link_or_reparse(child):
                raise ValueError(f"发布暂存目录出现 link/reparse：{child}")
        for name in file_names:
            child = current_path / name
            relative = child.relative_to(root).as_posix()
            content = stable_read_file(child)
            state[relative] = (len(content), _sha256(content))
    return dict(sorted(state.items()))


def _record_failed_build_retention(
    error: BaseException,
    path: Path,
    parent: Path,
) -> None:
    """只报告失败暂存位置，不解析、不跟随，也不删除任何目录。"""

    retained = Path(os.path.abspath(path))
    expected_parent = Path(os.path.abspath(parent))
    if (
        retained.parent == expected_parent
        and retained.name.startswith(".release-staging-")
    ):
        status = "retained_failed_release_staging"
    else:
        status = "retained_unverified_failed_release_staging"
    message = f"[WARN] 发布失败暂存已保留（{status}）：{retained}"
    error.retained_staging_path = str(retained)
    error.retention_status = status
    error.retention_message = message
    add_note = getattr(error, "add_note", None)
    if callable(add_note):
        add_note(message)
    print(message, file=sys.stderr)


def _publish_directory_no_replace(source: Path, destination: Path) -> None:
    """用操作系统原子语义发布目录，目标已存在时绝不替换。"""

    if os.name == "nt":
        try:
            os.rename(source, destination)
        except OSError as error:
            if os.path.lexists(destination):
                raise FileExistsError(
                    f"目标在原子发布前已出现，拒绝覆盖：{destination}"
                ) from error
            raise
        return

    if sys.platform.startswith("linux"):
        at_current_working_directory = -100
        flag = 1  # RENAME_NOREPLACE
        symbol = "renameat2"
    elif sys.platform == "darwin":
        at_current_working_directory = -2
        flag = 4  # RENAME_EXCL
        symbol = "renameatx_np"
    else:
        raise OSError(
            getattr(errno, "ENOTSUP", 95),
            "当前平台不支持目录原子无覆盖发布",
            str(destination),
        )

    import ctypes

    library = ctypes.CDLL(None, use_errno=True)
    rename_no_replace = getattr(library, symbol, None)
    if rename_no_replace is None:
        raise OSError(
            getattr(errno, "ENOTSUP", 95),
            "当前平台不支持目录原子无覆盖发布",
            str(destination),
        )
    rename_no_replace.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename_no_replace.restype = ctypes.c_int
    result = rename_no_replace(
        at_current_working_directory,
        os.fsencode(source),
        at_current_working_directory,
        os.fsencode(destination),
        flag,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(
            error_number,
            f"目标在原子发布前已出现，拒绝覆盖：{destination}",
            str(destination),
        )
    raise OSError(
        error_number,
        os.strerror(error_number),
        str(destination),
    )


def build_release_staging(
    research_root: Path,
    paperflow_root: Path,
    output: Path,
    *,
    size_limit: int = MAX_RELEASE_BYTES,
    protected_roots: Iterable[Path] = (),
) -> dict[str, object]:
    """构建新暂存目录；失败时不发布，并保留现场供人工检查。"""

    research_input = Path(research_root)
    paperflow_input = Path(paperflow_root)
    output_input = Path(os.path.abspath(output))
    protected = tuple(Path(item) for item in protected_roots)
    research = research_input.resolve(strict=True)
    paperflow = paperflow_input.resolve(strict=True)
    target = validate_output_destination(
        Path(output),
        research,
        paperflow,
        protected_roots=protected,
    )
    private_scan_roots = (*protected, output_input.parent, target.parent)
    research_files, research_excluded = _copy_plan(
        research_input,
        "research",
        size_limit=size_limit,
        protected_roots=private_scan_roots,
    )
    research_bytes = sum(len(content) for _, content in research_files)
    paperflow_files, paperflow_excluded = _copy_plan(
        paperflow_input,
        "paperflow",
        size_limit=size_limit - research_bytes,
        protected_roots=private_scan_roots,
    )
    plan = sorted(
        research_files + paperflow_files,
        key=lambda item: item[0],
    )
    validate_release_paths(item[0] for item in plan)

    estimated_payload_bytes = sum(len(content) for _, content in plan)
    enforce_size_limit(estimated_payload_bytes, size_limit)

    target.parent.mkdir(parents=True, exist_ok=True)
    for ancestor in _existing_ancestors(target.parent):
        if is_link_or_reparse(ancestor):
            raise ValueError(f"发布目标祖先不得包含 link/reparse：{ancestor}")
    temporary = target.parent / f".release-staging-{uuid.uuid4().hex}"
    if os.path.lexists(temporary):
        raise RuntimeError("发布临时目录意外已存在")
    temporary.mkdir()
    try:
        file_records: list[dict[str, object]] = []
        for release_path, content in plan:
            destination = temporary.joinpath(*PurePosixPath(release_path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            file_records.append(
                {
                    "path": release_path,
                    "size": len(content),
                    "sha256": _sha256(content),
                }
            )
        payload_bytes = sum(
            int(record["size"])
            for record in file_records
        )
        enforce_size_limit(payload_bytes, size_limit)
        exclusions = {
            "schema": "cv-research-paperflow.release-exclusions.v1",
            "excluded": sorted(
                research_excluded + paperflow_excluded,
                key=lambda item: item["path"],
            ),
            "excluded_patterns": [
                ".git/",
                ".venv/ 和 venv/",
                "__pycache__/ 与各类缓存目录",
                "datasets/、weights/、models/、checkpoints/",
                "output/、runs/、logs/、artifacts/",
                "*.db、*.sqlite、*.pt、*.pth、*.ckpt、*.onnx",
                "测试和审核生成的图片、PDF、日志与压缩包",
                "docs/reviews/、docs/superpowers/、docs/workflow_audit/",
                "docs/html_archive/、docs/diagrams/audit/、docs/deployment/",
                "docs/deliverables/ 下的历史 audit-package 及本机审计包生成工具",
                "PaperFlow 旧版 tests/（当前必需测试根为 tests_v2/）",
                "本机安装、下载、冒烟及项目专用文档生成工具",
            ],
            "download_urls": DOWNLOAD_URLS,
        }
        exclusions_bytes = _json_bytes(exclusions)
        control_records = [
            {
                "path": EXCLUSIONS_NAME,
                "size": len(exclusions_bytes),
                "sha256": _sha256(exclusions_bytes),
            }
        ]
        manifest = {
            "schema": "cv-research-paperflow.release-staging.v1",
            "version": "1.0.0",
            "payload_bytes": payload_bytes,
            "file_count": len(file_records),
            "files": file_records,
            "control_files": control_records,
        }
        manifest_bytes = _json_bytes(manifest)
        (temporary / EXCLUSIONS_NAME).write_bytes(exclusions_bytes)
        (temporary / MANIFEST_NAME).write_bytes(manifest_bytes)

        research_final, research_excluded_final = _copy_plan(
            research_input,
            "research",
            size_limit=size_limit,
            protected_roots=private_scan_roots,
        )
        research_final_bytes = sum(
            len(content) for _, content in research_final
        )
        paperflow_final, paperflow_excluded_final = _copy_plan(
            paperflow_input,
            "paperflow",
            size_limit=size_limit - research_final_bytes,
            protected_roots=private_scan_roots,
        )
        final_plan = sorted(
            research_final + paperflow_final,
            key=lambda item: item[0],
        )
        if (
            final_plan != plan
            or research_excluded_final != research_excluded
            or paperflow_excluded_final != paperflow_excluded
        ):
            raise ValueError("源码文件集合或内容在构建期间发生变化")

        expected_state = {
            record["path"]: (record["size"], record["sha256"])
            for record in file_records
        }
        expected_state[EXCLUSIONS_NAME] = (
            len(exclusions_bytes),
            _sha256(exclusions_bytes),
        )
        expected_state[MANIFEST_NAME] = (
            len(manifest_bytes),
            _sha256(manifest_bytes),
        )
        if _ordinary_tree_state(temporary) != dict(sorted(expected_state.items())):
            raise ValueError("发布暂存文件集合或内容在发布前发生变化")
        total_bytes = sum(
            item.stat().st_size
            for item in temporary.rglob("*")
            if item.is_file()
        )
        enforce_size_limit(total_bytes, size_limit)
        if os.path.lexists(target):
            raise ValueError(f"发布目标在构建期间出现，拒绝覆盖：{target}")
        _publish_directory_no_replace(temporary, target)
    except BaseException as error:
        _record_failed_build_retention(error, temporary, target.parent)
        raise

    return {
        "schema": "cv-research-paperflow.release-build-result.v1",
        "ok": True,
        "payload_bytes": payload_bytes,
        "release_bytes": total_bytes,
        "file_count": len(file_records),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从科研与 PaperFlow Git 源码构建不覆盖的清洁发布暂存目录。"
    )
    parser.add_argument("--research-root", required=True, type=Path)
    parser.add_argument("--paperflow-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--protected-root",
        action="append",
        type=Path,
        default=[],
        help="显式保护的禁止写入根目录；可重复提供。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_release_staging(
            args.research_root,
            args.paperflow_root,
            args.out,
            protected_roots=args.protected_root,
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(
            json.dumps(
                {
                    "schema": "cv-research-paperflow.release-build-result.v1",
                    "ok": False,
                    "error": str(error),
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
