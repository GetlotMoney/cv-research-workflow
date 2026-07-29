from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


class FrameworkIntakeError(ValueError):
    """The repository cannot be inspected or drafted safely."""


class ScanLimitExceeded(FrameworkIntakeError):
    """The bounded repository scan would exceed an explicit limit."""


class DraftPreparationError(FrameworkIntakeError):
    """Draft creation failed and any partial target was deliberately preserved."""

    def __init__(
        self,
        message: str,
        *,
        status: str,
        draft_root: str,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.draft_root = draft_root


_COMPONENT_EXPLANATIONS = {
    "data": "候选的数据读取与样本整理代码；这里只按文件名和目录名猜测。",
    "model": "候选的模型结构代码；这里只按文件名和目录名猜测。",
    "losses": "候选的损失计算代码；这里只按文件名和目录名猜测。",
    "trainer": "候选的训练流程代码；这里只按文件名和目录名猜测。",
    "evaluator": "候选的评估流程代码；这里只按文件名和目录名猜测。",
    "inferencer": "候选的推理或预测入口；这里只按文件名和目录名猜测。",
    "metrics": "候选的指标计算代码；这里只按文件名和目录名猜测。",
    "configs": "候选的配置文件；这里只按文件名、目录名和扩展名猜测。",
}
_COMPONENT_HINTS = {
    "data": ("data", "dataset", "dataloader", "loader"),
    "model": ("model", "models", "network", "backbone", "encoder", "decoder"),
    "losses": ("loss", "losses", "criterion"),
    "trainer": ("train", "trainer", "training", "engine"),
    "evaluator": ("eval", "evaluate", "evaluator", "evaluation", "test"),
    "inferencer": ("infer", "inference", "inferencer", "predict", "demo"),
    "metrics": ("metric", "metrics"),
    "configs": ("config", "configs", "configuration", "options"),
}
_ENTRYPOINT_NAMES = {
    "main.py": "文件名通常表示程序总入口。",
    "train.py": "文件名通常表示训练入口。",
    "trainer.py": "文件名通常表示训练入口。",
    "evaluate.py": "文件名通常表示评估入口。",
    "eval.py": "文件名通常表示评估入口。",
    "test.py": "文件名可能表示评估或测试入口。",
    "inference.py": "文件名通常表示推理入口。",
    "infer.py": "文件名通常表示推理入口。",
    "predict.py": "文件名通常表示预测入口。",
    "demo.py": "文件名通常表示示例入口。",
    "run.py": "文件名通常表示运行入口。",
}
_IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "wandb",
}
_DATA_DIRECTORIES = {
    "artifacts",
    "checkpoints",
    "datasets_raw",
    "raw_data",
    "runs",
    "weights",
}
_WEIGHT_EXTENSIONS = {
    ".bin",
    ".ckpt",
    ".h5",
    ".joblib",
    ".model",
    ".onnx",
    ".pkl",
    ".pth",
    ".pt",
    ".safetensors",
    ".tflite",
}
_DATA_EXTENSIONS = {
    ".7z",
    ".avi",
    ".bmp",
    ".bz2",
    ".csv",
    ".flac",
    ".gif",
    ".gz",
    ".jpeg",
    ".jpg",
    ".jsonl",
    ".mat",
    ".mkv",
    ".mp3",
    ".mp4",
    ".npy",
    ".npz",
    ".parquet",
    ".png",
    ".tar",
    ".tfrecord",
    ".tsv",
    ".wav",
    ".webm",
    ".webp",
    ".xz",
    ".zip",
}
_KNOWN_BINARY_EXTENSIONS = {
    ".a",
    ".class",
    ".dll",
    ".dylib",
    ".exe",
    ".o",
    ".obj",
    ".pdf",
    ".pyc",
    ".so",
}
_SECRET_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_ecdsa",
    "id_rsa",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "service-account.json",
}
_SECRET_EXTENSIONS = {
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
}
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def scan_gzsl_repository(
    path: str | os.PathLike[str],
    *,
    max_files: int = 2_000,
    max_read_bytes: int = 8 * 1024 * 1024,
    max_file_bytes: int = 512 * 1024,
    max_total_bytes: int = 64 * 1024 * 1024,
) -> dict[str, Any]:
    """Inspect a local Git repository with bounded, read-only heuristics."""

    _validate_positive_limit("max_files", max_files)
    _validate_positive_limit("max_read_bytes", max_read_bytes)
    _validate_positive_limit("max_file_bytes", max_file_bytes)
    _validate_positive_limit("max_total_bytes", max_total_bytes)
    repository = _safe_existing_directory(path, label="仓库")
    top_level = _git_repository_root(repository)
    if top_level != repository:
        raise FrameworkIntakeError(
            f"必须传入 Git 仓库根目录，不能传入仓库中的子目录：{repository}"
        )
    footprint = _bounded_directory_footprint(
        repository,
        max_files=max_files,
        max_total_bytes=max_total_bytes,
        skipped_root_names={".git"},
    )

    commit = _run_git(repository, "rev-parse", "HEAD")
    repository_clean = not bool(
        _run_git(repository, "status", "--porcelain=v1", "--untracked-files=normal")
    )
    scanned_paths: list[str] = []
    excluded: list[dict[str, str]] = []
    secret_files: list[str] = []
    links: list[str] = []
    component_candidates = {name: set() for name in _COMPONENT_EXPLANATIONS}
    candidate_entrypoints: list[dict[str, str]] = []
    file_count = 0
    read_bytes = 0
    stack = [repository]

    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise FrameworkIntakeError(f"无法安全读取目录：{directory}") from exc
        for entry in entries:
            relative = Path(entry.path).relative_to(repository).as_posix()
            if _directory_entry_is_link(entry):
                raise FrameworkIntakeError(
                    f"仓库含有符号链接或重解析点，拒绝扫描：{relative}"
                )
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as exc:
                raise FrameworkIntakeError(f"无法安全检查路径：{relative}") from exc

            if is_directory:
                lowered = entry.name.lower()
                if lowered in _IGNORED_DIRECTORIES:
                    if lowered != ".git":
                        excluded.append(
                            {"path": relative, "reason": "缓存、构建或工具目录"}
                        )
                    continue
                if lowered in _DATA_DIRECTORIES:
                    excluded.append({"path": relative, "reason": "权重或数据目录"})
                    _collect_component_candidates(relative, component_candidates)
                    continue
                stack.append(Path(entry.path))
                continue
            if not is_file:
                excluded.append({"path": relative, "reason": "不是普通文件"})
                continue

            file_count += 1
            if file_count > max_files:
                raise ScanLimitExceeded(
                    f"仓库文件数超过扫描上限 {max_files}，扫描已停止。"
                )
            lowered_name = entry.name.lower()
            suffix = Path(lowered_name).suffix
            if _looks_like_secret(lowered_name):
                secret_files.append(relative)
                excluded.append({"path": relative, "reason": "常见秘密或密钥文件"})
                continue
            if suffix in _WEIGHT_EXTENSIONS:
                excluded.append({"path": relative, "reason": "模型权重文件"})
                continue
            if suffix in _DATA_EXTENSIONS:
                excluded.append({"path": relative, "reason": "数据或媒体文件"})
                continue
            if suffix in _KNOWN_BINARY_EXTENSIONS:
                excluded.append({"path": relative, "reason": "二进制文件"})
                continue
            try:
                size = entry.stat(follow_symlinks=False).st_size
            except OSError as exc:
                raise FrameworkIntakeError(f"无法读取文件信息：{relative}") from exc
            if size > max_file_bytes:
                excluded.append({"path": relative, "reason": "单文件超过读取上限"})
                continue
            if read_bytes + size > max_read_bytes:
                raise ScanLimitExceeded(
                    f"读取量将超过扫描上限 {max_read_bytes} 字节，扫描已停止。"
                )
            try:
                content = Path(entry.path).read_bytes()
            except OSError as exc:
                raise FrameworkIntakeError(f"无法安全读取文件：{relative}") from exc
            read_bytes += len(content)
            if b"\x00" in content:
                excluded.append({"path": relative, "reason": "二进制文件"})
                continue
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                excluded.append(
                    {"path": relative, "reason": "无法按 UTF-8 安全读取的文件"}
                )
                continue

            scanned_paths.append(relative)
            _collect_component_candidates(relative, component_candidates)
            reason = _entrypoint_reason(relative)
            if reason is not None:
                candidate_entrypoints.append({"path": relative, "reason": reason})

    components = {
        name: {
            "explanation": explanation,
            "candidates": sorted(component_candidates[name]),
        }
        for name, explanation in _COMPONENT_EXPLANATIONS.items()
    }
    return {
        "schema": "cvwf.gzsl-framework-scan.v1",
        "repository_root": str(repository),
        "repository_clean": repository_clean,
        "clean": repository_clean,
        "commit": commit,
        "candidate_entrypoints": sorted(
            candidate_entrypoints, key=lambda item: item["path"]
        ),
        "components": components,
        "scanned_paths": sorted(scanned_paths),
        "scanned_file_count": len(scanned_paths),
        "visited_file_count": footprint["file_count"],
        "read_bytes": read_bytes,
        "total_bytes": footprint["total_bytes"],
        "excluded": sorted(excluded, key=lambda item: item["path"]),
        "safety": {
            "secret_files": sorted(secret_files),
            "links_or_reparse_points": sorted(links),
        },
        "scan_limits": {
            "max_files": max_files,
            "max_read_bytes": max_read_bytes,
            "max_file_bytes": max_file_bytes,
            "max_total_bytes": max_total_bytes,
        },
        "semantic_validation": {
            "passed": False,
            "message": (
                "这些只是按路径和文件名得到的候选项，尚未证明语义正确、"
                "接口兼容或与标准流程等价。"
            ),
        },
    }


def prepare_standardization_draft(
    source: str | os.PathLike[str],
    inbox_root: str | os.PathLike[str],
    slug: str,
    component_map: Mapping[
        str,
        str
        | os.PathLike[str]
        | Sequence[str | os.PathLike[str]],
    ]
    | None = None,
    *,
    max_scan_files: int = 2_000,
    max_scan_total_bytes: int = 64 * 1024 * 1024,
    max_clone_files: int = 10_000,
    max_clone_total_bytes: int = 512 * 1024 * 1024,
) -> dict[str, Any]:
    """Clone a clean repository and create a non-runnable mapping draft."""

    _validate_positive_limit("max_scan_files", max_scan_files)
    _validate_positive_limit("max_scan_total_bytes", max_scan_total_bytes)
    _validate_positive_limit("max_clone_files", max_clone_files)
    _validate_positive_limit("max_clone_total_bytes", max_clone_total_bytes)
    inbox = _safe_existing_directory(inbox_root, label="草稿收件目录")
    if not isinstance(slug, str) or _SLUG.fullmatch(slug) is None:
        raise FrameworkIntakeError(
            "草稿短名只允许小写字母、数字和短横线，长度不能超过 63。"
        )
    target = inbox / slug
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"草稿目标已经存在，拒绝覆盖：{target}")
    if target.parent.resolve(strict=True) != inbox:
        raise FrameworkIntakeError("草稿目标越出了收件目录。")

    scan = scan_gzsl_repository(
        source,
        max_files=max_scan_files,
        max_total_bytes=max_scan_total_bytes,
    )
    repository = Path(scan["repository_root"])
    if not scan["repository_clean"]:
        raise FrameworkIntakeError(
            "源仓库不是干净状态；请先处理未提交改动，再生成隔离草稿。"
        )
    if scan["safety"]["secret_files"]:
        raise FrameworkIntakeError(
            "源仓库含有常见秘密或密钥文件，拒绝复制到草稿。"
        )
    if scan["safety"]["links_or_reparse_points"]:
        raise FrameworkIntakeError(
            "源仓库含有符号链接或重解析点，拒绝复制到草稿。"
        )
    if (repository / "standardization").exists():
        raise FrameworkIntakeError(
            "源仓库已有 standardization 路径，拒绝覆盖其中内容。"
        )
    selected = _validate_component_map(repository, component_map)
    _bounded_git_tree_footprint(
        repository,
        max_files=max_clone_files,
        max_total_bytes=max_clone_total_bytes,
    )

    try:
        clone_completed = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "clone",
                "--no-local",
                "--depth",
                "1",
                "--single-branch",
                "--no-tags",
                "--",
                str(repository),
                str(target),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_git_environment(),
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise _draft_failure(
            target,
            phase="clone",
            detail="Git 不可用或克隆超时。",
        ) from exc
    if clone_completed.returncode != 0:
        detail = clone_completed.stderr.strip() or clone_completed.stdout.strip()
        raise _draft_failure(target, phase="clone", detail=detail)

    try:
        _bounded_directory_footprint(
            target,
            max_files=max_clone_files,
            max_total_bytes=max_clone_total_bytes,
        )
        cloned_commit = _run_git(target, "rev-parse", "HEAD")
        if cloned_commit != scan["commit"]:
            raise FrameworkIntakeError(
                "复制过程中源仓库提交发生变化，草稿已停止生成。"
            )
        standardization = target / "standardization"
        standardization.mkdir()
        _write_json(standardization / "scan.json", scan)
        _write_json(
            standardization / "component-map.json",
            {
                "schema": "cvwf.gzsl-component-map.v2",
                "status": "draft_incomplete",
                "components": {
                    name: {
                        "selected": selected.get(name),
                        "candidates": scan["components"][name]["candidates"],
                        "explanation": scan["components"][name]["explanation"],
                    }
                    for name in _COMPONENT_EXPLANATIONS
                },
                "semantic_validation": {
                    "passed": False,
                    "message": "尚未验证接口、数据含义、指标或结果等价性。",
                },
            },
        )
        (standardization / "README.md").write_text(
            _draft_readme(scan["commit"]),
            encoding="utf-8",
        )
        (standardization / "workflow_adapter.py").write_text(
            _adapter_scaffold(),
            encoding="utf-8",
        )
    except DraftPreparationError:
        raise
    except Exception as exc:
        raise _draft_failure(
            target,
            phase="finalize",
            detail=str(exc),
        ) from exc

    return {
        "schema": "cvwf.gzsl-standardization-draft.v2",
        "status": "draft_incomplete",
        "draft_root": str(target.resolve(strict=True)),
        "source_root": str(repository),
        "source_commit": scan["commit"],
        "runnable": False,
        "message": "隔离草稿已生成，但尚未完成标准化，也不能用于正式运行。",
    }


def _validate_positive_limit(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FrameworkIntakeError(f"{name} 必须是正整数。")


def _bounded_directory_footprint(
    root: Path,
    *,
    max_files: int,
    max_total_bytes: int,
    skipped_root_names: set[str] | None = None,
) -> dict[str, int]:
    """Measure a directory without following links and stop at explicit limits."""

    skipped = skipped_root_names or set()
    file_count = 0
    total_bytes = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise FrameworkIntakeError(f"无法安全读取目录：{directory}") from exc
        for entry in entries:
            relative = Path(entry.path).relative_to(root).as_posix()
            if _directory_entry_is_link(entry):
                raise FrameworkIntakeError(
                    f"目录含有符号链接或重解析点，拒绝继续：{relative}"
                )
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as exc:
                raise FrameworkIntakeError(f"无法安全检查路径：{relative}") from exc
            if is_directory:
                if directory == root and entry.name.lower() in skipped:
                    continue
                stack.append(Path(entry.path))
                continue
            if not is_file:
                raise FrameworkIntakeError(f"路径不是普通文件：{relative}")
            try:
                size = entry.stat(follow_symlinks=False).st_size
            except OSError as exc:
                raise FrameworkIntakeError(f"无法读取文件信息：{relative}") from exc
            file_count += 1
            if file_count > max_files:
                raise ScanLimitExceeded(
                    f"文件数超过安全上限 {max_files}，操作已停止。"
                )
            total_bytes += size
            if total_bytes > max_total_bytes:
                raise ScanLimitExceeded(
                    f"总体积超过安全上限 {max_total_bytes} 字节，操作已停止。"
                )
    return {"file_count": file_count, "total_bytes": total_bytes}


def _bounded_git_tree_footprint(
    repository: Path,
    *,
    max_files: int,
    max_total_bytes: int,
) -> dict[str, int]:
    """Measure the committed snapshot with bounded streaming Git output."""

    command = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(repository),
        "ls-tree",
        "-r",
        "-z",
        "--format=%(objectmode) %(objecttype) %(objectsize)",
        "HEAD",
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
        )
    except FileNotFoundError as exc:
        raise FrameworkIntakeError("Git 不可用，无法检查克隆体积。") from exc
    assert process.stdout is not None
    file_count = 0
    total_bytes = 0
    try:
        while True:
            record = _read_nul_record(process.stdout, max_bytes=128)
            if record is None:
                break
            try:
                mode, object_type, raw_size = record.decode("ascii").split()
            except (UnicodeDecodeError, ValueError) as exc:
                raise FrameworkIntakeError("Git 返回了无法识别的树信息。") from exc
            if mode == "120000" or object_type != "blob":
                raise FrameworkIntakeError(
                    "提交中含有符号链接、子模块或其他重解析入口，拒绝克隆。"
                )
            try:
                size = int(raw_size)
            except ValueError as exc:
                raise FrameworkIntakeError("Git 返回了无效的文件大小。") from exc
            file_count += 1
            if file_count > max_files:
                raise ScanLimitExceeded(
                    f"克隆文件数超过安全上限 {max_files}，克隆未开始。"
                )
            total_bytes += size
            if total_bytes > max_total_bytes:
                raise ScanLimitExceeded(
                    f"克隆总体积超过安全上限 {max_total_bytes} 字节，克隆未开始。"
                )
        try:
            return_code = process.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise FrameworkIntakeError("Git 检查克隆体积超时。") from exc
        if return_code != 0:
            assert process.stderr is not None
            detail = process.stderr.read(4096).decode("utf-8", errors="replace").strip()
            raise FrameworkIntakeError(f"Git 检查克隆体积失败：{detail}")
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.communicate()
        raise
    finally:
        process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    return {"file_count": file_count, "total_bytes": total_bytes}


def _read_nul_record(stream: Any, *, max_bytes: int) -> bytes | None:
    record = bytearray()
    while True:
        byte = stream.read(1)
        if byte == b"":
            return bytes(record) if record else None
        if byte == b"\0":
            return bytes(record)
        record.extend(byte)
        if len(record) > max_bytes:
            raise FrameworkIntakeError("Git 树信息单条记录超过安全长度。")


def _safe_existing_directory(
    path: str | os.PathLike[str],
    *,
    label: str,
) -> Path:
    try:
        supplied = Path(path).expanduser()
    except TypeError as exc:
        raise FrameworkIntakeError(f"{label}路径无效。") from exc
    if not supplied.exists():
        raise FileNotFoundError(f"{label}不存在：{supplied}")
    if _path_chain_contains_link(supplied):
        raise FrameworkIntakeError(f"{label}路径含有符号链接或重解析点。")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise FrameworkIntakeError(f"无法解析{label}路径：{supplied}") from exc
    if not resolved.is_dir():
        raise NotADirectoryError(f"{label}不是目录：{resolved}")
    return resolved


def _path_chain_contains_link(path: Path) -> bool:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if not current.exists() and not current.is_symlink():
            break
        try:
            metadata = current.lstat()
        except OSError:
            return True
        if current.is_symlink() or (
            getattr(metadata, "st_file_attributes", 0) & 0x400
        ):
            return True
    return False


def _git_repository_root(repository: Path) -> Path:
    try:
        raw_root = _run_git(repository, "rev-parse", "--show-toplevel")
        return Path(raw_root).resolve(strict=True)
    except (FrameworkIntakeError, FileNotFoundError, OSError) as exc:
        raise FrameworkIntakeError(
            f"只接受本地 Git 仓库目录：{repository}"
        ) from exc


def _run_git(repository: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(repository),
                *arguments,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_git_environment(),
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise FrameworkIntakeError("Git 不可用或命令执行超时。") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise FrameworkIntakeError(f"Git 检查失败：{detail}")
    return completed.stdout.strip()


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    blocked_names = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    }
    for name in tuple(environment):
        if (
            name.upper() in blocked_names
            or name.upper().startswith("GIT_CONFIG_KEY_")
            or name.upper().startswith("GIT_CONFIG_VALUE_")
        ):
            environment.pop(name, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def _directory_entry_is_link(entry: os.DirEntry[str]) -> bool:
    try:
        metadata = entry.stat(follow_symlinks=False)
        return entry.is_symlink() or (
            getattr(metadata, "st_file_attributes", 0) & 0x400
        ) != 0
    except OSError:
        return True


def _looks_like_secret(lowered_name: str) -> bool:
    if lowered_name in _SECRET_NAMES:
        return True
    if lowered_name.startswith(".env.") and not lowered_name.endswith(
        (".example", ".sample", ".template")
    ):
        return True
    return Path(lowered_name).suffix in _SECRET_EXTENSIONS


def _collect_component_candidates(
    relative: str,
    candidates: dict[str, set[str]],
) -> None:
    lowered = relative.lower()
    tokens = {
        token
        for token in re.split(r"[/_.-]+", lowered)
        if token
    }
    for component, hints in _COMPONENT_HINTS.items():
        if any(hint in tokens for hint in hints):
            candidates[component].add(relative)
    if Path(lowered).suffix in {".yaml", ".yml", ".toml", ".ini", ".cfg"}:
        candidates["configs"].add(relative)


def _entrypoint_reason(relative: str) -> str | None:
    name = PurePosixPath(relative).name.lower()
    if name in _ENTRYPOINT_NAMES:
        return _ENTRYPOINT_NAMES[name]
    if name == "__main__.py":
        return "Python 约定把这个文件作为包的运行入口。"
    return None


def _validate_component_map(
    repository: Path,
    component_map: Mapping[
        str,
        str
        | os.PathLike[str]
        | Sequence[str | os.PathLike[str]],
    ]
    | None,
) -> dict[str, list[str]]:
    if component_map is None:
        return {}
    if not isinstance(component_map, Mapping):
        raise FrameworkIntakeError("component_map 必须是组件名到仓库内路径的映射。")
    selected: dict[str, list[str]] = {}
    for component, raw_paths in component_map.items():
        if component not in _COMPONENT_EXPLANATIONS:
            raise FrameworkIntakeError(f"不支持的组件类型：{component}")
        if isinstance(raw_paths, (str, os.PathLike)):
            candidates = [raw_paths]
        elif isinstance(raw_paths, Sequence):
            candidates = list(raw_paths)
        else:
            raise FrameworkIntakeError(
                f"{component} 必须是一个路径或由多个路径组成的列表。"
            )
        if not candidates:
            continue
        normalized: list[str] = []
        for raw_path in candidates:
            if not isinstance(raw_path, (str, os.PathLike)):
                raise FrameworkIntakeError(f"{component} 的路径必须是文字路径。")
            path_text = os.fspath(raw_path).replace("\\", "/")
            pure_path = PurePosixPath(path_text)
            if (
                not path_text
                or pure_path.is_absolute()
                or PureWindowsPath(path_text).is_absolute()
                or ".." in pure_path.parts
            ):
                raise FrameworkIntakeError(f"{component} 的路径越出了源仓库。")
            candidate = repository.joinpath(*pure_path.parts)
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as exc:
                raise FrameworkIntakeError(
                    f"{component} 指向的路径不存在：{path_text}"
                ) from exc
            if repository not in resolved.parents and resolved != repository:
                raise FrameworkIntakeError(f"{component} 的路径越出了源仓库。")
            if _path_chain_contains_link(candidate):
                raise FrameworkIntakeError(
                    f"{component} 的路径含有链接或重解析点。"
                )
            if candidate.is_dir():
                raise FrameworkIntakeError(f"{component} 必须指向普通文件。")
            if _looks_like_secret(candidate.name.lower()):
                raise FrameworkIntakeError(
                    f"{component} 不能指向秘密或密钥文件。"
                )
            relative = pure_path.as_posix()
            if relative not in normalized:
                normalized.append(relative)
        selected[component] = normalized
    return selected


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _draft_readme(commit: str) -> str:
    return f"""# GZSL 外来框架标准化草稿

状态：**未完成，不能用于正式运行。**

这个目录是从源仓库提交 `{commit}` 隔离复制出的草稿。扫描结果只根据路径和
文件名给出候选位置，没有证明代码语义正确、接口兼容或结果等价。

在人工核对数据含义、模型输入输出、训练方式、评估口径和指标实现之前，
`workflow_adapter.py` 会主动拒绝运行。完成核对后也应另行实现适配器并补测试，
不能直接把这个安全脚手架改成“假装成功”。
"""


def _adapter_scaffold() -> str:
    return '''"""Fail-closed scaffold for an unfinished GZSL standardization."""

from __future__ import annotations

from typing import NoReturn


STANDARDIZATION_COMPLETE = False


class StandardizationIncompleteError(RuntimeError):
    """Raised because this draft has not passed semantic validation."""


def run(*_args: object, **_kwargs: object) -> NoReturn:
    raise StandardizationIncompleteError(
        "GZSL framework standardization is incomplete; execution is disabled."
    )
'''


def _draft_failure(
    target: Path,
    *,
    phase: str,
    detail: str,
) -> DraftPreparationError:
    target_exists = target.exists() or target.is_symlink()
    status = (
        "draft_failed_preserved"
        if target_exists
        else "draft_failed_no_target"
    )
    if target_exists:
        disposition = f"失败现场已保留：{target}"
    else:
        disposition = "克隆未产生可保留的草稿目录"
    return DraftPreparationError(
        (
            f"无法创建隔离草稿；状态={status}；阶段={phase}；"
            f"{disposition}。原因：{detail or 'Git 未提供错误详情'}"
        ),
        status=status,
        draft_root=str(target),
    )
