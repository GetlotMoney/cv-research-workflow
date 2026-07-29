from __future__ import annotations

import os
import re
import stat
import subprocess
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io import (
    DEFAULT_JSON_LIMIT,
    atomic_create_json,
    read_bounded_json_object,
    read_bounded_regular_file,
)
from .git_safe import GIT_SAFE_CONFIG, run_git_bounded
from .locking import project_snapshot_lock, project_write_lock
from .project import PROJECT_SCHEMA_V2, is_link_or_reparse
from .records import (
    _initialized_project,
    _next_id,
    _preflight_workflow_command_locked,
    _validate_project,
)


CODEBASE_SCHEMA = "cv-experiment-workflow.codebase.v1"
CODEBASE_ID = re.compile(r"CB-[0-9]{4}")
CODEBASE_NAME = re.compile(r"CB-[0-9]{4}\.json")
COMMIT = re.compile(r"[0-9a-f]{40}")
UNTRACKED_LEDGER_PATH = re.compile(
    r"\.experiment-workflow/(?:"
    r"project\.json|workflow\.lock\.json|adapter\.json|evidence\.jsonl|"
    r"artifacts/index\.json|"
    r"ideas/IDEA-[0-9]{4}\.json|"
    r"modules/MOD-[0-9]{4}\.json|"
    r"runs/RUN-[0-9]{4}/(?:"
    r"run\.json|execution_snapshot\.manifest\.json|"
    r"execution_snapshot(?:/.*)?"
    r")|"
    r"sources/SRC-[0-9]{4}\.json|"
    r"tasks/TASK-[0-9]{4}\.json|"
    r"versions/VER-[0-9]{4}\.json|"
    r"templates/TPL-[0-9]{4}\.json|"
    r"codebases/CB-[0-9]{4}\.json"
    r")"
)
DIRECTIONS = {"det", "cls", "seg", "instseg", "sr", "gzsl"}
SOURCES = {"domain_pack", "external", "manual"}
MAX_MANIFEST_SIZE = 256 * 1024
MAX_TEXT = 4096
CODEBASE_FIELDS = {
    "schema",
    "id",
    "name",
    "primary_direction",
    "repo_path",
    "source",
    "template_id",
    "template_version",
    "default_branch",
    "initial_commit",
    "initial_tag",
    "created_at",
}
CODEBASE_MANIFEST_FIELDS = CODEBASE_FIELDS - {"id", "created_at"}
EXPECTED_GIT_FIELDS = {"branch", "commit", "tag", "require_clean"}
RISKY_EXECUTABLE_EXTENSIONS = (
    "py",
    "pyc",
    "pyo",
    "pyd",
    "so",
    "dll",
    "exe",
    "com",
    "bat",
    "cmd",
    "ps1",
    "psm1",
    "scr",
    "msi",
    "sh",
    "bash",
    "zsh",
    "fish",
    "js",
    "mjs",
    "cjs",
    "jar",
    "class",
    "rb",
    "pl",
    "php",
    "lua",
)
RISKY_IGNORED_PATHSPECS = tuple(
    pathspec
    for extension in RISKY_EXECUTABLE_EXTENSIONS
    for pathspec in (
        f":(glob,icase)*.{extension}",
        f":(glob,icase)**/*.{extension}",
    )
)
def register_codebase(
    project: Path,
    manifest: Path,
    repo_root: Path,
) -> dict[str, Any]:
    """登记当前干净 Git 顶层的初始身份，绝不覆盖已有对象。"""
    root = _initialized_project(project)
    with project_write_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _preflight_workflow_command_locked(control)
        facts = _normalize_manifest(_read_manifest(manifest))
        first_identity = _verify_repository_identity(
            repo_root,
            facts,
            project_control=control,
        )
        directory = _ensure_codebase_directory(control)
        codebase_id = _next_id(directory, "CB", CODEBASE_ID)
        payload = {
            "schema": CODEBASE_SCHEMA,
            "id": codebase_id,
            **facts,
            "created_at": _utc_now(),
        }
        validate_codebase(payload, expected_id=codebase_id)
        second_identity = _verify_repository_identity(
            repo_root,
            facts,
            project_control=control,
        )
        if second_identity != first_identity:
            raise ValueError("Codebase Git 身份在核验期间发生变化，拒绝登记")
        if not atomic_create_json(
            directory / f"{codebase_id}.json",
            payload,
            transaction_id=uuid.uuid4().hex,
        ):
            raise FileExistsError(
                f"Codebase 已存在，拒绝覆盖：{directory / f'{codebase_id}.json'}"
            )
        return payload


def validate_codebase(
    payload: dict[str, Any],
    *,
    expected_id: str,
) -> None:
    """严格校验一条不可变 Codebase 记录的静态字段。"""
    if (
        not isinstance(payload, dict)
        or set(payload) != CODEBASE_FIELDS
        or payload.get("schema") != CODEBASE_SCHEMA
        or payload.get("id") != expected_id
        or CODEBASE_ID.fullmatch(expected_id) is None
    ):
        raise ValueError(f"Codebase 记录字段、schema 或 ID 无效：{expected_id}")
    normalized = _normalize_manifest({
        key: payload[key]
        for key in CODEBASE_MANIFEST_FIELDS
    })
    for key, value in normalized.items():
        if payload[key] != value:
            raise ValueError(f"Codebase 规范化事实发生漂移：{expected_id}")
    _validate_created_at(payload["created_at"])


def validate_codebases_locked(
    control: Path,
) -> dict[str, dict[str, Any]]:
    """在调用者持锁时读取 Codebase；历史 v2 没有目录时返回空集合。"""
    directory = Path(control) / "codebases"
    if not os.path.lexists(directory):
        return {}
    _real_directory(directory, "codebases 目录")
    records: dict[str, dict[str, Any]] = {}
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if (
            CODEBASE_NAME.fullmatch(entry.name) is None
            or is_link_or_reparse(entry)
            or not entry.is_file()
        ):
            raise ValueError(f"Codebase 目录存在未知文件或目录：{entry}")
        try:
            payload = read_bounded_json_object(
                entry,
                DEFAULT_JSON_LIMIT,
                f"Codebase {entry.stem}",
            )
        except (OSError, ValueError) as error:
            raise ValueError(f"无法读取 Codebase JSON：{entry}；{error}") from error
        validate_codebase(payload, expected_id=entry.stem)
        records[entry.stem] = payload
    return records


def load_codebase_locked(control: Path, codebase_id: str) -> dict[str, Any]:
    if not isinstance(codebase_id, str) or CODEBASE_ID.fullmatch(codebase_id) is None:
        raise ValueError(f"Codebase ID 无效：{codebase_id}")
    payload = validate_codebases_locked(control).get(codebase_id)
    if payload is None:
        raise ValueError(f"Codebase 不存在：{codebase_id}")
    return payload


def verify_codebase_gate(
    project: Path,
    codebase_id: str,
    expected_git: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """复核 Codebase 基线；按需再精确核对当前 checkout。"""
    root = _initialized_project(project)
    with project_snapshot_lock(root):
        control = _validate_project(root, expected_schema=PROJECT_SCHEMA_V2)
        _preflight_workflow_command_locked(control)
        return _verify_codebase_gate_locked(
            control,
            codebase_id,
            expected_git,
        )


def _verify_codebase_gate_locked(
    control: Path,
    codebase_id: str,
    expected_git: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在调用者已持有项目锁时复用同一套 live Git gate。"""
    payload = load_codebase_locked(control, codebase_id)
    expected = _normalize_expected_git(expected_git)
    repo_root = Path(payload["repo_path"])
    first_identity = _verify_gate_identity(
        repo_root,
        payload,
        expected,
        project_control=control,
    )
    second_identity = _verify_gate_identity(
        repo_root,
        payload,
        expected,
        project_control=control,
    )
    if second_identity != first_identity:
        raise ValueError("Codebase Git 身份在 gate 核验期间发生变化")
    return {
        "status": "pass",
        "codebase_id": codebase_id,
        "repo_path": first_identity["repo_path"],
        "branch": first_identity["branch"],
        "commit": first_identity["commit"],
        "verification_scope": (
            "baseline"
            if expected is None
            else "current_checkout"
        ),
        "clean_checked": bool(
            expected is not None and expected["require_clean"]
        ),
        "worktree_clean": first_identity["worktree_clean"],
        "source": payload["source"],
        "template_id": payload["template_id"],
        "template_version": payload["template_version"],
        "tag": (
            payload["initial_tag"]
            if expected is None
            else expected["tag"]
        ),
    }


def load_codebase_adapter_spec(
    repository: Path,
    commit: str,
) -> dict[str, Any]:
    """读取固定根路径 Adapter，并证明当前 bytes 就是绑定 commit 的普通 blob。"""
    if not isinstance(commit, str) or COMMIT.fullmatch(commit) is None:
        raise ValueError("Codebase Adapter commit 必须是 40 位小写 hex")
    root = Path(repository).expanduser().absolute()
    _real_directory_without_reparse_ancestors(root, "Codebase Adapter 仓库")
    root = root.resolve(strict=True)
    if _resolve_commit(root, "HEAD^{commit}", "HEAD commit") != commit:
        raise ValueError("Codebase Adapter 只能从绑定 commit 的当前 checkout 加载")

    relative = "workflow_adapter.py"
    candidate = root / relative
    try:
        info = candidate.lstat()
    except OSError as error:
        raise ValueError("Codebase 根目录缺少 workflow_adapter.py") from error
    if (
        is_link_or_reparse(candidate)
        or not stat.S_ISREG(info.st_mode)
        or not candidate.is_file()
    ):
        raise ValueError("workflow_adapter.py 必须是普通文件且不能是 link/reparse")
    source_bytes = read_bounded_regular_file(
        candidate,
        DEFAULT_JSON_LIMIT,
        "Codebase workflow adapter",
    )
    _verify_codebase_adapter_blob(root, commit, relative, source_bytes)

    try:
        after_info = candidate.lstat()
    except OSError as error:
        raise ValueError("workflow_adapter.py 在读取期间消失") from error
    if (
        is_link_or_reparse(candidate)
        or not stat.S_ISREG(after_info.st_mode)
        or not candidate.is_file()
    ):
        raise ValueError("workflow_adapter.py 在读取期间不再是普通文件")
    after_bytes = read_bounded_regular_file(
        candidate,
        DEFAULT_JSON_LIMIT,
        "Codebase workflow adapter",
    )
    if after_bytes != source_bytes:
        raise ValueError("workflow_adapter.py bytes 在读取期间发生变化")
    _verify_codebase_adapter_blob(root, commit, relative, after_bytes)
    if _resolve_commit(root, "HEAD^{commit}", "HEAD commit recheck") != commit:
        raise ValueError("Codebase Adapter 核验期间 HEAD 发生变化")
    return {
        "source_bytes": bytes(source_bytes),
        "source": relative,
        "display_path": str(candidate),
        "repo_url": str(root),
        "commit": commit,
        "import_root": str(root),
    }


def _verify_codebase_adapter_blob(
    repository: Path,
    commit: str,
    relative: str,
    source_bytes: bytes,
) -> None:
    tree = _git(
        repository,
        "ls-tree",
        commit,
        "--",
        relative,
    )
    records = [line for line in tree.splitlines() if line]
    if len(records) != 1 or "\t" not in records[0]:
        raise ValueError(
            "workflow_adapter.py 无法在绑定 commit 中唯一定位为普通 blob"
        )
    metadata, raw_path = records[0].split("\t", 1)
    parts = metadata.split()
    if (
        len(parts) != 3
        or parts[0] not in {"100644", "100755"}
        or parts[1] != "blob"
        or raw_path != relative
    ):
        raise ValueError(
            "workflow_adapter.py 在绑定 commit 中必须是 100644/100755 普通 blob"
        )
    object_name = f"{commit}:{relative}"
    size_text = _git(
        repository,
        "cat-file",
        "-s",
        object_name,
    )
    try:
        size = int(size_text)
    except ValueError as error:
        raise ValueError("workflow_adapter.py Git blob 大小无效") from error
    if not 0 <= size <= DEFAULT_JSON_LIMIT:
        raise ValueError("workflow_adapter.py Git blob 超过大小限制")
    committed_bytes = _git_bytes(
        repository,
        "cat-file",
        "blob",
        object_name,
    )
    if len(committed_bytes) != size or committed_bytes != source_bytes:
        raise ValueError(
            "当前 workflow_adapter.py bytes 与绑定 commit 的 blob 不一致"
        )


def _verify_repository_identity(
    repo_root: Path,
    expected: dict[str, Any],
    *,
    project_control: Path | None = None,
) -> dict[str, str | None]:
    repository = _resolve_repository(repo_root, expected["repo_path"])
    branch = _current_branch(repository, required=True)
    assert branch is not None
    _git(repository, "check-ref-format", "--branch", expected["default_branch"])
    if branch != expected["default_branch"]:
        raise ValueError(
            "Codebase Git branch 与 default_branch 不一致："
            f"{branch} != {expected['default_branch']}"
        )

    commit = _resolve_commit(repository, "HEAD^{commit}", "HEAD commit")
    if commit != expected["initial_commit"]:
        raise ValueError(
            "Codebase Git commit 与 initial_commit 不一致："
            f"{commit} != {expected['initial_commit']}"
        )

    tag_commit: str | None = None
    tag = expected["initial_tag"]
    if tag is not None:
        tag_commit = _verify_tag(repository, tag, commit, "initial_commit")

    _verify_clean_worktree(repository, project_control)
    return {
        "repo_path": str(repository),
        "branch": branch,
        "commit": commit,
        "tag_commit": tag_commit,
    }


def _verify_gate_identity(
    repo_root: Path,
    codebase: dict[str, Any],
    expected_git: dict[str, Any] | None,
    *,
    project_control: Path,
) -> dict[str, str | bool | None]:
    repository = _resolve_repository(repo_root, codebase["repo_path"])
    baseline_commit = _resolve_commit(
        repository,
        f"{codebase['initial_commit']}^{{commit}}",
        "initial commit",
    )
    if baseline_commit != codebase["initial_commit"]:
        raise ValueError("Codebase initial commit 身份不一致")
    baseline_tag_commit: str | None = None
    if codebase["initial_tag"] is not None:
        baseline_tag_commit = _verify_tag(
            repository,
            codebase["initial_tag"],
            baseline_commit,
            "initial_commit",
        )

    current_commit = _resolve_commit(repository, "HEAD^{commit}", "HEAD commit")
    current_branch = _current_branch(repository, required=False)
    expected_tag_commit: str | None = None
    if expected_git is not None:
        _git(
            repository,
            "check-ref-format",
            "--branch",
            expected_git["branch"],
        )
        if current_branch != expected_git["branch"]:
            raise ValueError(
                "Codebase Git branch 与 expected_git 不一致："
                f"{current_branch} != {expected_git['branch']}"
            )
        if current_commit != expected_git["commit"]:
            raise ValueError(
                "Codebase Git commit 与 expected_git 不一致："
                f"{current_commit} != {expected_git['commit']}"
            )
        if expected_git["tag"] is not None:
            expected_tag_commit = _verify_tag(
                repository,
                expected_git["tag"],
                current_commit,
                "expected_git commit",
            )
    worktree_clean = _worktree_is_clean(repository, project_control)
    if (
        expected_git is not None
        and expected_git["require_clean"]
        and not worktree_clean
    ):
        raise ValueError(
            "Codebase Git 工作树必须 clean；tracked、untracked 和 ignored "
            "可执行文件均不允许"
        )

    return {
        "repo_path": str(repository),
        "branch": current_branch,
        "commit": current_commit,
        "tag_commit": expected_tag_commit or baseline_tag_commit,
        "worktree_clean": worktree_clean,
    }


def _resolve_repository(repo_root: Path, expected_path: str) -> Path:
    candidate = Path(repo_root).expanduser().absolute()
    _real_directory_without_reparse_ancestors(candidate, "Codebase repo-root")
    repository = candidate.resolve(strict=True)
    _real_directory(repository, "Codebase repo-root")
    if str(repository) != expected_path:
        raise ValueError("Codebase repo_path 与 repo-root 指向的真实仓库不一致")

    git_top_text = _git(repository, "rev-parse", "--show-toplevel")
    git_top = Path(git_top_text).expanduser().absolute()
    _real_directory(git_top, "Codebase Git 顶层")
    git_top = git_top.resolve(strict=True)
    if git_top != repository:
        raise ValueError("Codebase repo-root 必须是真实 Git 顶层，拒绝错仓")
    return repository


def _current_branch(repository: Path, *, required: bool) -> str | None:
    result = _run_git(
        repository,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    if required:
        raise ValueError("Codebase Git HEAD 处于 detached 状态")
    return None


def _resolve_commit(repository: Path, ref: str, label: str) -> str:
    commit = _git(
        repository,
        "rev-parse",
        "--verify",
        ref,
    ).lower()
    if COMMIT.fullmatch(commit) is None:
        raise ValueError(f"Codebase Git {label} 必须解析为 40 位小写 hex")
    return commit


def _verify_tag(
    repository: Path,
    tag: str,
    expected_commit: str,
    expected_label: str,
) -> str:
    _git(repository, "check-ref-format", f"refs/tags/{tag}")
    tag_commit = _resolve_commit(
        repository,
        f"refs/tags/{tag}^{{commit}}",
        f"tag {tag}",
    )
    if tag_commit != expected_commit:
        raise ValueError(
            f"Codebase Git tag 未精确指向 {expected_label}：{tag}"
        )
    return tag_commit


def _verify_clean_worktree(
    repository: Path,
    project_control: Path | None,
) -> None:
    result = _run_git(
        repository,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        ".",
    )
    _raise_git_failure(result, ("status", "--porcelain=v1", "-z"))
    same_repo_control: Path | None = None
    if project_control is not None:
        control = Path(project_control).resolve(strict=True)
        if control.parent == repository:
            same_repo_control = control
    for record in result.stdout.split("\x00"):
        if not record:
            continue
        if (
            record.startswith("?? ")
            and same_repo_control is not None
            and _allowed_untracked_ledger(
                repository,
                same_repo_control,
                record[3:],
            )
        ):
            continue
        raise ValueError(
            "Codebase Git 工作树必须 clean；tracked 修改和外部仓账本均不豁免"
        )
    _verify_no_ignored_executable_code(repository, same_repo_control)


def _verify_no_ignored_executable_code(
    repository: Path,
    same_repo_control: Path | None,
) -> None:
    result = _run_git(
        repository,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        "--",
        *RISKY_IGNORED_PATHSPECS,
    )
    _raise_git_failure(
        result,
        ("ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
    )
    for relative in result.stdout.split("\x00"):
        if not relative:
            continue
        if (
            same_repo_control is not None
            and _allowed_untracked_ledger(
                repository,
                same_repo_control,
                relative,
            )
        ):
            continue
        raise ValueError(
            "Codebase Git 工作树存在 ignored 可执行文件，require_clean 拒绝"
        )


def _worktree_is_clean(
    repository: Path,
    project_control: Path | None,
) -> bool:
    result = _run_git(
        repository,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        ".",
    )
    _raise_git_failure(result, ("status", "--porcelain=v1", "-z"))
    same_repo_control: Path | None = None
    if project_control is not None:
        control = Path(project_control).resolve(strict=True)
        if control.parent == repository:
            same_repo_control = control
    for record in result.stdout.split("\x00"):
        if not record:
            continue
        if (
            record.startswith("?? ")
            and same_repo_control is not None
            and _allowed_untracked_ledger(
                repository,
                same_repo_control,
                record[3:],
            )
        ):
            continue
        return False

    ignored = _run_git(
        repository,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        "--",
        *RISKY_IGNORED_PATHSPECS,
    )
    _raise_git_failure(
        ignored,
        ("ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
    )
    for relative in ignored.stdout.split("\x00"):
        if not relative:
            continue
        if (
            same_repo_control is not None
            and _allowed_untracked_ledger(
                repository,
                same_repo_control,
                relative,
            )
        ):
            continue
        return False
    return True


def _allowed_untracked_ledger(
    repository: Path,
    control: Path,
    relative: str,
) -> bool:
    if UNTRACKED_LEDGER_PATH.fullmatch(relative) is None:
        return False
    candidate = repository.joinpath(*relative.split("/"))
    try:
        candidate.relative_to(control)
    except ValueError:
        return False
    if is_link_or_reparse(candidate) or not candidate.is_file():
        return False
    for parent in candidate.parents:
        if parent == control:
            return True
        if is_link_or_reparse(parent):
            return False
    return False


def _normalize_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != CODEBASE_MANIFEST_FIELDS:
        raise ValueError("Codebase manifest 字段无效")
    if payload.get("schema") != CODEBASE_SCHEMA:
        raise ValueError("Codebase manifest schema 无效")
    name = _text(payload.get("name"), "Codebase name")
    direction = payload.get("primary_direction")
    if direction not in DIRECTIONS:
        raise ValueError(
            "Codebase primary_direction 必须是 "
            "det/cls/seg/instseg/sr/gzsl"
        )
    repo_path = _absolute_path_text(payload.get("repo_path"))
    source = payload.get("source")
    if source not in SOURCES:
        raise ValueError(
            "Codebase source 必须是 domain_pack/external/manual"
        )
    template_id = _optional_text(payload.get("template_id"), "template_id")
    template_version = _optional_text(
        payload.get("template_version"),
        "template_version",
    )
    if (template_id is None) != (template_version is None):
        raise ValueError("Codebase template_id 与 template_version 必须同时有值或同时为空")
    default_branch = _text(payload.get("default_branch"), "default_branch")
    initial_commit = payload.get("initial_commit")
    if not isinstance(initial_commit, str) or COMMIT.fullmatch(initial_commit) is None:
        raise ValueError("Codebase initial_commit 必须是 40 位小写 hex")
    initial_tag = _optional_text(payload.get("initial_tag"), "initial_tag")
    return {
        "schema": CODEBASE_SCHEMA,
        "name": name,
        "primary_direction": direction,
        "repo_path": repo_path,
        "source": source,
        "template_id": template_id,
        "template_version": template_version,
        "default_branch": default_branch,
        "initial_commit": initial_commit,
        "initial_tag": initial_tag,
    }


def _normalize_expected_git(
    payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if payload is None:
        return None
    if not isinstance(payload, dict) or set(payload) != EXPECTED_GIT_FIELDS:
        raise ValueError(
            "expected_git 必须严格包含 branch/commit/tag/require_clean"
        )
    branch = _text(payload["branch"], "expected_git branch")
    commit = payload["commit"]
    if not isinstance(commit, str) or COMMIT.fullmatch(commit) is None:
        raise ValueError("expected_git commit 必须是 40 位小写 hex")
    tag = _optional_text(payload["tag"], "expected_git tag")
    require_clean = payload["require_clean"]
    if type(require_clean) is not bool:
        raise ValueError("expected_git require_clean 必须是 boolean")
    return {
        "branch": branch,
        "commit": commit,
        "tag": tag,
        "require_clean": require_clean,
    }


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        return read_bounded_json_object(
            Path(path),
            MAX_MANIFEST_SIZE,
            "Codebase manifest",
        )
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取严格 Codebase manifest：{path}；{error}") from error


def _ensure_codebase_directory(control: Path) -> Path:
    directory = Path(control) / "codebases"
    if not os.path.lexists(directory):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        except OSError as error:
            raise ValueError(f"无法创建 codebases 目录：{directory}；{error}") from error
    return _real_directory(directory, "codebases 目录")


def _real_directory(path: Path, label: str) -> Path:
    if (
        not os.path.lexists(path)
        or is_link_or_reparse(path)
        or not path.is_dir()
    ):
        raise ValueError(
            f"{label} 必须是普通目录且不得为 link/reparse：{path}"
        )
    return path


def _real_directory_without_reparse_ancestors(path: Path, label: str) -> Path:
    _real_directory(path, label)
    for candidate in (path, *path.parents):
        if is_link_or_reparse(candidate):
            raise ValueError(
                f"{label} 及其父路径不得包含 link/reparse：{candidate}"
            )
    return path


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        bounded = run_git_bounded(
            root,
            *arguments,
            stdout_limit=DEFAULT_JSON_LIMIT,
            stderr_limit=DEFAULT_JSON_LIMIT,
            timeout=15,
        )
        result = subprocess.CompletedProcess(
            args=["git", *arguments],
            returncode=bounded.returncode,
            stdout=bounded.stdout.decode("utf-8", errors="strict"),
            stderr=bounded.stderr.decode("utf-8", errors="replace"),
        )
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"无法执行 Git 校验：{root}；{error}") from error
    output_size = len(result.stdout.encode("utf-8")) + len(
        result.stderr.encode("utf-8")
    )
    if output_size > DEFAULT_JSON_LIMIT:
        raise ValueError("Git 文本输出超过大小限制")
    return result


def _git(root: Path, *arguments: str) -> str:
    result = _run_git(root, *arguments)
    _raise_git_failure(result, arguments)
    return result.stdout.strip()


def _git_bytes(root: Path, *arguments: str) -> bytes:
    try:
        result = run_git_bounded(
            root,
            *arguments,
            stdout_limit=DEFAULT_JSON_LIMIT,
            stderr_limit=DEFAULT_JSON_LIMIT,
            timeout=15,
        )
    except (OSError, ValueError) as error:
        raise ValueError(f"无法执行 Git bytes 核验：{root}：{error}") from error
    if len(result.stdout) + len(result.stderr) > DEFAULT_JSON_LIMIT:
        raise ValueError("Git bytes 输出超过大小限制")
    if result.returncode != 0:
        detail = (
            result.stderr.decode("utf-8", errors="replace").strip()
            or result.stdout.decode("utf-8", errors="replace").strip()
            or f"exit {result.returncode}"
        )
        raise ValueError(
            f"Git {' '.join(arguments)} 核验失败：{detail}"
        )
    return bytes(result.stdout)


def _raise_git_failure(
    result: subprocess.CompletedProcess[str],
    arguments: tuple[str, ...],
) -> None:
    if result.returncode == 0:
        return
    detail = (
        result.stderr.strip()
        or result.stdout.strip()
        or f"exit {result.returncode}"
    )
    operation = " ".join(arguments)
    raise ValueError(f"Git {operation} 校验失败：{detail}")


def _text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > MAX_TEXT
    ):
        raise ValueError(f"{label} 必须是 1..{MAX_TEXT} 字符的规范非空字符串")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError(f"{label} 不得包含 Unicode 控制类别字符")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _absolute_path_text(value: object) -> str:
    text = _text(value, "Codebase repo_path")
    path = Path(text)
    if not path.is_absolute() or ".." in path.parts or str(path) != text:
        raise ValueError("Codebase repo_path 必须是规范绝对路径")
    return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def _validate_created_at(value: object) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("Codebase created_at 必须是 UTC ISO-8601 时间")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("Codebase created_at 必须是 UTC ISO-8601 时间") from error
    if parsed.tzinfo != timezone.utc:
        raise ValueError("Codebase created_at 必须是 UTC ISO-8601 时间")
