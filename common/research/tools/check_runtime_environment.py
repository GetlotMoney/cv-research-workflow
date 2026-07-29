#!/usr/bin/env python3
"""检查 Windows 科研工作流、六方向包与 PaperFlow 运行条件。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable


TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from build_release_staging import (  # noqa: E402
    MAX_RELEASE_BYTES,
    has_reparse_flag,
    parse_strict_json_object,
    stable_read_file,
    validate_release_paths,
    write_new_json_report,
)
from run_release_checks import (  # noqa: E402
    _start_owned_process,
    _terminate_owned_process_tree,
)


EXPECTED_DIRECTIONS = ("cls", "det", "seg", "instseg", "sr", "gzsl")
PACK_SHA256 = re.compile(r"[0-9a-f]{64}")
SEMANTIC_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
)
MAX_CAPTURED_OUTPUT_BYTES = 2 * 1024 * 1024
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_MISSING = "MISSING_REQUIRED"
STATUS_PENDING = "PENDING"
STATUS_OPTIONAL_AVAILABLE = "OPTIONAL_AVAILABLE"
STATUS_OPTIONAL_MISSING = "OPTIONAL_NOT_INSTALLED"
_SMOKE_GUARD_SCHEMA = "cv-experiment-workflow.smoke-guard.v1"


@dataclass(frozen=True)
class _ValidatedPackSnapshot:
    payload_root: Path
    snapshot: tuple[tuple[str, ...], dict[str, bytes]]
    required_modules: tuple[str, ...]
    version: str
    device: str
    cpu_fallback: bool | None

    @property
    def name(self) -> str:
        return self.payload_root.name


_PACK_SMOKE_BOOTSTRAP = r"""
import json
import os
import runpy
import socket
import subprocess
import sys
from pathlib import Path

source_root = sys.argv[1]
module_name = sys.argv[2]
guard_path = Path(sys.argv[3])
module_arguments = sys.argv[4:]
violations = []
_guard_json_dumps = json.dumps
_guard_os_open = os.open
_guard_os_write = os.write
_guard_os_close = os.close
_guard_open_flags = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
)

class GuardViolation(PermissionError):
    pass

def block(kind, event):
    violations.append({"kind": kind, "event": event})
    raise GuardViolation("smoke Python API guard blocked: " + event)

def audit(event, arguments):
    if event in {
        "socket.__new__",
        "socket.bind",
        "socket.connect",
        "socket.getaddrinfo",
        "socket.gethostbyaddr",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.getnameinfo",
        "socket.sendmsg",
        "socket.sendto",
    }:
        block("network", event)
    if (
        event.startswith("subprocess.")
        or event == "os.system"
        or event.startswith("os.spawn")
        or event.startswith("os.posix_spawn")
    ):
        block("process", event)
    if event == "import" and arguments:
        top_level = str(arguments[0]).split(".", 1)[0]
        if top_level in {"pip", "ensurepip", "conda", "mamba"}:
            block("install", "import:" + top_level)

sys.addaudithook(audit)

def blocked_network(*args, **kwargs):
    return block("network", "socket.monkeypatch")

def blocked_process(*args, **kwargs):
    return block("process", "subprocess.monkeypatch")

_original_socket = socket.socket
_original_popen = subprocess.Popen

class GuardedSocket(_original_socket):
    def __new__(cls, *args, **kwargs):
        return blocked_network(*args, **kwargs)

class GuardedPopen(_original_popen):
    def __init__(self, *args, **kwargs):
        blocked_process(*args, **kwargs)

socket.socket = GuardedSocket
socket.create_connection = blocked_network
socket.getaddrinfo = blocked_network
socket.gethostbyaddr = blocked_network
socket.gethostbyname = blocked_network
socket.gethostbyname_ex = blocked_network
socket.getnameinfo = blocked_network
subprocess.Popen = GuardedPopen
subprocess.run = blocked_process
subprocess.call = blocked_process
subprocess.check_call = blocked_process
subprocess.check_output = blocked_process
os.system = blocked_process
for name in tuple(dir(os)):
    if name.startswith("spawn") or name.startswith("posix_spawn"):
        setattr(os, name, blocked_process)

pending = None
try:
    sys.path.insert(0, source_root)
    sys.argv = [module_name, *module_arguments]
    runpy.run_module(module_name, run_name="__main__", alter_sys=False)
except SystemExit as error:
    if error.code not in (None, 0):
        pending = error
except BaseException as error:
    pending = error
finally:
    guard_status = "FAIL" if violations else "PASS"
    guard_bytes = _guard_json_dumps(
        {
            "schema": "cv-experiment-workflow.smoke-guard.v1",
            "status": guard_status,
            "violations": violations,
        },
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    guard_fd = _guard_os_open(
        guard_path,
        _guard_open_flags,
        0o600,
    )
    try:
        guard_written = 0
        while guard_written < len(guard_bytes):
            written = _guard_os_write(
                guard_fd,
                guard_bytes[guard_written:],
            )
            if written <= 0:
                raise OSError("guard report write made no progress")
            guard_written += written
    finally:
        _guard_os_close(guard_fd)
if pending is not None:
    raise pending
if guard_status != "PASS":
    raise SystemExit(97)
"""

_OPTIONAL_COMPONENTS = (
    (
        "torchvision",
        "torchvision",
        "可选正式视觉数据与模型后端",
        "https://pytorch.org/vision/stable/index.html",
    ),
    (
        "pycocotools",
        "pycocotools",
        "可选 COCO 检测与分割评估后端",
        "https://github.com/ppwwyyxx/cocoapi",
    ),
    (
        "docling",
        "docling",
        "PaperFlow 可选 PDF 版面解析后端",
        "https://github.com/docling-project/docling",
    ),
    (
        "bge_m3_backend",
        "sentence_transformers",
        "PaperFlow 可选 BGE-M3 语义检索后端",
        "https://huggingface.co/BAAI/bge-m3",
    ),
    (
        "rapidocr",
        "rapidocr_onnxruntime",
        "PaperFlow 可选扫描页 OCR 后端",
        "https://github.com/RapidAI/RapidOCR",
    ),
)


def _start_bounded_output_capture(
    process: subprocess.Popen[str],
) -> tuple[dict[str, object], list[threading.Thread]]:
    state: dict[str, object] = {
        "stdout": [],
        "stderr": [],
        "captured_bytes": 0,
        "limit_exceeded": False,
        "errors": [],
        "lock": threading.Lock(),
    }

    def read_stream(name: str) -> None:
        stream = getattr(process, name, None)
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                encoded_size = len(
                    chunk.encode("utf-8", errors="replace")
                )
                lock = state["lock"]
                with lock:
                    captured = int(state["captured_bytes"])
                    if (
                        encoded_size
                        <= MAX_CAPTURED_OUTPUT_BYTES - captured
                    ):
                        chunks = state[name]
                        assert isinstance(chunks, list)
                        chunks.append(chunk)
                        state["captured_bytes"] = captured + encoded_size
                    else:
                        state["limit_exceeded"] = True
        except BaseException as error:
            errors = state["errors"]
            assert isinstance(errors, list)
            errors.append(f"{name}: {error}")

    threads = [
        threading.Thread(
            target=read_stream,
            args=(name,),
            daemon=True,
        )
        for name in ("stdout", "stderr")
    ]
    for thread in threads:
        thread.start()
    return state, threads


def _finish_bounded_output_capture(
    state: dict[str, object],
    threads: list[threading.Thread],
) -> tuple[str, str, bool, int, str]:
    for thread in threads:
        thread.join(timeout=5)
    errors = list(state["errors"])
    if any(thread.is_alive() for thread in threads):
        errors.append("输出读取线程未在受控进程树回收后结束")
    stdout_chunks = state["stdout"]
    stderr_chunks = state["stderr"]
    assert isinstance(stdout_chunks, list)
    assert isinstance(stderr_chunks, list)
    return (
        "".join(stdout_chunks),
        "".join(stderr_chunks),
        bool(state["limit_exceeded"]),
        int(state["captured_bytes"]),
        "；".join(str(item) for item in errors),
    )


def _module_available(module_name: str) -> bool:
    result = _run_command(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            (
                "import importlib.util, sys; "
                f"sys.exit(0 if importlib.util.find_spec({module_name!r}) "
                "is not None else 1)"
            ),
        ],
        timeout=10,
        env=_guard_environment_hints(1597),
    )
    return result.returncode == 0


def _run_command(
    command: list[str],
    *,
    timeout: int,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic_ns()
    try:
        process, owner = _start_owned_process(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(
            command,
            125,
            stdout="",
            stderr=f"受控子进程启动失败：{error}",
        )
    identity = {
        "pid": process.pid,
        "started_monotonic_ns": started,
    }
    capture_state, capture_threads = _start_bounded_output_capture(process)
    timed_out = False
    cleanup_error = ""
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        cleanup_error = str(error)
    try:
        _terminate_owned_process_tree(process, identity, owner)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        cleanup_error = str(error)
    try:
        owner.close()
    except (OSError, RuntimeError) as error:
        cleanup_error = cleanup_error or str(error)
    (
        stdout,
        stderr,
        output_limit_exceeded,
        _captured_output_bytes,
        capture_error,
    ) = _finish_bounded_output_capture(capture_state, capture_threads)
    if capture_error:
        cleanup_error = cleanup_error or capture_error
    if timed_out:
        return_code = 124
        stderr = (stderr + "\n" if stderr else "") + f"受控子进程超过 {timeout} 秒"
    elif cleanup_error:
        return_code = 125
    elif output_limit_exceeded:
        return_code = 126
        stderr = (
            (stderr + "\n" if stderr else "")
            + f"受控子进程输出超过 {MAX_CAPTURED_OUTPUT_BYTES} 字节上限"
        )
    else:
        return_code = int(process.returncode)
    if cleanup_error:
        stderr = (
            (stderr + "\n" if stderr else "")
            + f"owned 进程树回收失败：{cleanup_error}"
        )
    return subprocess.CompletedProcess(
        command,
        return_code,
        stdout=stdout,
        stderr=stderr,
    )


def _probe_python_module(
    module_name: str,
    *,
    expression: str | None = None,
    timeout: int = 20,
) -> tuple[bool, str]:
    if not _module_available(module_name):
        return False, f"未安装或无法发现 {module_name}"
    code = expression or (
        f"import {module_name}; "
        f"print(getattr({module_name}, '__version__', 'available'))"
    )
    result = _run_command(
        [sys.executable, "-I", "-B", "-c", code],
        timeout=timeout,
        env=_guard_environment_hints(1601),
    )
    detail = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        return False, f"{module_name} 导入失败：{detail[-1000:] or '无输出'}"
    return True, detail or f"{module_name} 可导入"


def _required_item(
    item_id: str,
    available: bool,
    detail: str,
) -> dict[str, object]:
    return {
        "id": item_id,
        "required": True,
        "status": STATUS_PASS if available else STATUS_MISSING,
        "detail": detail,
    }


def _sqlite_fts5_available() -> tuple[bool, str]:
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE VIRTUAL TABLE smoke USING fts5(content)")
            connection.execute("INSERT INTO smoke(content) VALUES ('paperflow')")
            count = connection.execute(
                "SELECT count(*) FROM smoke WHERE smoke MATCH 'paperflow'"
            ).fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error as error:
        return False, f"SQLite FTS5 不可用：{error}"
    return count == 1, f"SQLite {sqlite3.sqlite_version}，FTS5 可用"


def _read_json(path: Path) -> dict[str, object]:
    return parse_strict_json_object(
        stable_read_file(path),
        path.name,
    )


def _pack_locations(research_root: Path) -> dict[str, tuple[Path, str]]:
    packs_root = (
        research_root
        / "skills"
        / "cv-experiment-workflow"
        / "assets"
        / "domain-packs"
    )
    registry_path = packs_root / "registry.json"
    if not registry_path.is_file():
        return {}
    registry = _read_json(registry_path)
    records = registry.get("packs")
    if (
        registry.get("schema")
        != "cv-experiment-workflow.domain-pack-registry.v1"
        or not isinstance(records, list)
    ):
        raise ValueError("方向包 registry.json 结构无效")
    locations: dict[str, tuple[Path, str]] = {}
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "id",
                "version",
                "template_id",
                "primary_direction",
                "directory",
            }
        ):
            raise ValueError("方向包 registry 条目必须是对象")
        direction = record.get("id")
        version = record.get("version")
        template_id = record.get("template_id")
        primary_direction = record.get("primary_direction")
        directory = record.get("directory")
        if (
            not isinstance(direction, str)
            or direction not in EXPECTED_DIRECTIONS
            or not isinstance(version, str)
            or SEMANTIC_VERSION.fullmatch(version) is None
            or template_id != f"PACK-{direction.upper()}"
            or primary_direction != direction
            or not isinstance(directory, str)
            or directory != f"{direction}-v{version}"
            or "/" in directory
            or "\\" in directory
            or directory in {"", ".", ".."}
            or direction in locations
        ):
            raise ValueError("方向包 registry 条目无效或重复")
        location = (packs_root / directory).resolve(strict=False)
        if location.parent != packs_root.resolve(strict=True):
            raise ValueError("方向包 registry 路径逃逸")
        locations[direction] = (location, version)
    return locations


def _validate_pack_contract(
    pack_root: Path,
    direction: str,
    expected_version: str | None = None,
) -> _ValidatedPackSnapshot:
    if {item.name for item in pack_root.iterdir()} != {"pack.json", "payload"}:
        raise ValueError("方向包根目录必须精确包含 pack.json 与 payload")
    pack_path = pack_root / "pack.json"
    pack_metadata = os.lstat(pack_path)
    if (
        not stat.S_ISREG(pack_metadata.st_mode)
        or stat.S_ISLNK(pack_metadata.st_mode)
        or has_reparse_flag(pack_metadata)
        or int(getattr(pack_metadata, "st_nlink", 1)) != 1
    ):
        raise ValueError("方向包 pack.json 必须是独立普通文件，拒绝 hardlink")
    pack = _read_json(pack_path)
    pack_version = pack.get("version")
    if (
        pack.get("schema") != "cv-experiment-workflow.domain-pack.v1"
        or pack.get("id") != direction
        or not isinstance(pack_version, str)
        or SEMANTIC_VERSION.fullmatch(pack_version) is None
        or (
            expected_version is not None
            and pack_version != expected_version
        )
        or pack.get("template_id") != f"PACK-{direction.upper()}"
        or pack.get("primary_direction") != direction
    ):
        raise ValueError("pack.json 身份与方向 registry 不一致")
    records = pack.get("files")
    if not isinstance(records, list) or not 1 <= len(records) <= 512:
        raise ValueError("pack.json files 必须包含 1..512 个文件条目")
    declared_paths: list[str] = []
    declared: dict[str, tuple[int, str]] = {}
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "size", "sha256"}
            or not isinstance(record.get("path"), str)
            or isinstance(record.get("size"), bool)
            or not isinstance(record.get("size"), int)
            or record["size"] < 0
            or not isinstance(record.get("sha256"), str)
            or PACK_SHA256.fullmatch(record["sha256"]) is None
        ):
            raise ValueError("pack.json 文件清单条目无效")
        declared_paths.append(record["path"])
        declared[record["path"]] = (record["size"], record["sha256"])
    validate_release_paths(declared_paths)
    if declared_paths != sorted(declared_paths):
        raise ValueError("pack.json 文件清单必须按路径排序")

    payload_root = pack_root / "payload"
    directories, snapshot = _snapshot_pack_tree(payload_root)
    if set(snapshot) != set(declared):
        raise ValueError("pack.json 文件清单未精确覆盖 payload")
    for relative, content in snapshot.items():
        size, digest = declared[relative]
        if (
            len(content) != size
            or hashlib.sha256(content).hexdigest() != digest
        ):
            raise ValueError(
                f"方向包文件体积或 SHA-256 与清单不一致：{relative}"
            )

    manifest_content = snapshot.get("domain-pack.json")
    if manifest_content is None:
        raise ValueError("缺少 payload/domain-pack.json")
    manifest = parse_strict_json_object(
        manifest_content,
        "payload/domain-pack.json",
    )
    if (
        manifest.get("schema")
        != "cv-experiment-workflow.runnable-domain-pack.v1"
        or manifest.get("version") != pack_version
    ):
        raise ValueError("domain-pack.json schema 或 version 无效")
    if manifest.get("id") != direction:
        raise ValueError("domain-pack.json 的 id 与方向不一致")
    if manifest.get("primary_direction") != direction:
        raise ValueError("domain-pack.json 的 primary_direction 与方向不一致")
    if manifest.get("template_id") != f"PACK-{direction.upper()}":
        raise ValueError("domain-pack.json 的 template_id 与方向不一致")
    commands = manifest.get("commands")
    entrypoints = manifest.get("entrypoints")
    if (
        not isinstance(commands, list)
        or commands != ["train", "evaluate", "infer", "synthetic_smoke"]
    ):
        raise ValueError("方向包必须声明 train/evaluate/infer/synthetic_smoke")
    direct_entrypoints = {
        "train": f"python -m {direction}.train",
        "evaluate": f"python -m {direction}.evaluate",
        "infer": f"python -m {direction}.infer",
        "synthetic_smoke": f"python -m {direction}.smoke",
    }
    allowed_entrypoints = [direct_entrypoints]
    if direction in {"det", "instseg"}:
        allowed_entrypoints.append(
            {
                name: command.replace(
                    f"python -m {direction}.",
                    f"python -m src.{direction}.",
                    1,
                )
                for name, command in direct_entrypoints.items()
            }
        )
    if not isinstance(entrypoints, dict) or entrypoints not in allowed_entrypoints:
        raise ValueError("方向包缺少完整四入口")
    smoke = manifest.get("synthetic_smoke")
    expected_device = "cuda" if direction == "gzsl" else "cpu"
    if (
        direction == "gzsl"
        and isinstance(smoke, dict)
        and smoke.get("device") != "cuda"
    ):
        raise ValueError("GZSL 固定使用 CUDA，明确拒绝 CPU")
    if (
        not isinstance(smoke, dict)
        or smoke.get("device") != expected_device
        or smoke.get("run_kind") != "synthetic_debug_only"
        or smoke.get("paper_eligible") is not False
    ):
        raise ValueError("方向包 smoke 的设备或证据合同无效")
    cpu_fallback = smoke.get("cpu_fallback")
    if direction == "gzsl" and cpu_fallback is not False:
        raise ValueError("GZSL 必须固定使用 CUDA，且不得提供 CPU fallback")
    formal_evaluator = manifest.get("formal_evaluator")
    required_modules: tuple[str, ...] = ()
    if formal_evaluator is not None:
        package = (
            formal_evaluator.get("package")
            if isinstance(formal_evaluator, dict)
            else None
        )
        if (
            not isinstance(package, str)
            or not package
            or formal_evaluator.get("fallback") is not False
        ):
            raise ValueError("正式评估器必须声明依赖包并禁止静默降级")
        required_modules = (package,)
    return _ValidatedPackSnapshot(
        payload_root=payload_root,
        snapshot=(directories, snapshot),
        required_modules=required_modules,
        version=pack_version,
        device=expected_device,
        cpu_fallback=cpu_fallback,
    )


def _guard_environment_hints(seed: int) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": str(seed),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PIP_NO_INDEX": "1",
            "NO_PROXY": "*",
            "no_proxy": "*",
            "CUDA_VISIBLE_DEVICES": "",
        }
    )
    return environment


def _snapshot_pack_tree(
    root: Path,
) -> tuple[tuple[str, ...], dict[str, bytes]]:
    lexical = Path(os.path.abspath(root))
    metadata = os.lstat(lexical)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or has_reparse_flag(metadata)
    ):
        raise ValueError("方向包 payload 必须是普通目录，不能是 link/reparse")
    directories: list[str] = []
    files: dict[str, bytes] = {}
    remaining = MAX_RELEASE_BYTES
    for current, directory_names, file_names in os.walk(
        lexical,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        for name in directory_names:
            child = current_path / name
            child_metadata = os.lstat(child)
            if (
                stat.S_ISLNK(child_metadata.st_mode)
                or has_reparse_flag(child_metadata)
            ):
                raise ValueError(f"方向包不得包含 link/reparse：{child}")
            directories.append(child.relative_to(lexical).as_posix())
        for name in file_names:
            child = current_path / name
            relative = child.relative_to(lexical).as_posix()
            child_metadata = os.lstat(child)
            if int(getattr(child_metadata, "st_nlink", 1)) != 1:
                raise ValueError(
                    f"方向包不得包含 hardlink：{relative}"
                )
            content = stable_read_file(child, limit=remaining)
            files[relative] = content
            remaining -= len(content)
    ordered_directories = tuple(sorted(directories))
    ordered_files = dict(sorted(files.items()))
    validate_release_paths([*ordered_directories, *ordered_files])
    return ordered_directories, ordered_files


def _copy_snapshot(
    snapshot: tuple[tuple[str, ...], dict[str, bytes]],
    target: Path,
) -> None:
    target.mkdir(parents=True, exist_ok=False)
    directories, files = snapshot
    for relative in directories:
        target.joinpath(*PurePosixPath(relative).parts).mkdir(
            parents=True,
            exist_ok=False,
        )
    for relative, content in files.items():
        destination = target.joinpath(*PurePosixPath(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def _read_guard_report(path: Path) -> dict[str, object]:
    try:
        report = parse_strict_json_object(
            stable_read_file(path),
            "smoke-guard.json",
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        return {
            "schema": _SMOKE_GUARD_SCHEMA,
            "status": STATUS_FAIL,
            "violations": [
                {
                    "kind": "guard",
                    "event": f"guard report missing or invalid: {error}",
                }
            ],
        }
    if (
        not isinstance(report, dict)
        or set(report) != {"schema", "status", "violations"}
        or report.get("schema") != _SMOKE_GUARD_SCHEMA
        or report.get("status") not in {STATUS_PASS, STATUS_FAIL}
        or not isinstance(report.get("violations"), list)
        or not all(
            isinstance(item, dict)
            and set(item) == {"kind", "event"}
            and item.get("kind") in {"network", "process", "install"}
            and isinstance(item.get("event"), str)
            and item["event"]
            for item in report["violations"]
        )
        or (report["status"] == STATUS_PASS and report["violations"])
    ):
        return {
            "schema": _SMOKE_GUARD_SCHEMA,
            "status": STATUS_FAIL,
            "violations": [
                {"kind": "guard", "event": "guard report schema invalid"}
            ],
        }
    return report


def _run_pack_smoke(
    pack_root: Path | _ValidatedPackSnapshot,
    direction: str,
    device: str,
    seed: int,
    timeout: int,
) -> dict[str, object]:
    """从临时副本运行合成闭环，并记录常用 Python API 的违规尝试。"""

    started = time.monotonic()
    if isinstance(pack_root, _ValidatedPackSnapshot):
        source_root = pack_root.payload_root
        source_before = pack_root.snapshot
    else:
        source_root = Path(pack_root)
        source_before = _snapshot_pack_tree(source_root)
    with tempfile.TemporaryDirectory(prefix=f"pack-{direction}-smoke-") as temporary:
        temporary_root = Path(temporary)
        safe_source = temporary_root / "source"
        _copy_snapshot(source_before, safe_source)
        guard_path = temporary_root / "smoke-guard.json"
        command = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _PACK_SMOKE_BOOTSTRAP,
            str(safe_source),
            f"{direction}.smoke",
            str(guard_path),
            "--work-dir",
            str(temporary_root / "run"),
            "--device",
            device,
            "--seed",
            str(seed),
        ]
        process_started = time.monotonic_ns()
        try:
            process, owner = _start_owned_process(
                command,
                cwd=safe_source,
                env=_guard_environment_hints(seed),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            try:
                source_immutable = (
                    _snapshot_pack_tree(source_root) == source_before
                )
            except (OSError, ValueError):
                source_immutable = False
            return {
                "status": STATUS_FAIL,
                "detail": f"受控 smoke 进程启动失败：{error}",
                "duration_seconds": round(time.monotonic() - started, 3),
                "timed_out": False,
                "process_identity": None,
                "process_owner": None,
                "process_tree_cleanup": "NOT_STARTED",
                "output_limit_exceeded": False,
                "captured_output_bytes": 0,
                "guard": _read_guard_report(guard_path),
                "source_immutable": source_immutable,
            }
        process_identity = {
            "pid": process.pid,
            "started_monotonic_ns": process_started,
        }
        capture_state, capture_threads = _start_bounded_output_capture(
            process
        )
        timed_out = False
        cleanup_error = ""
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            cleanup_error = str(error)
        try:
            _terminate_owned_process_tree(process, process_identity, owner)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            cleanup_error = str(error)
        try:
            owner.close()
        except (OSError, RuntimeError) as error:
            cleanup_error = cleanup_error or str(error)
        (
            stdout,
            stderr,
            output_limit_exceeded,
            captured_output_bytes,
            capture_error,
        ) = _finish_bounded_output_capture(
            capture_state,
            capture_threads,
        )
        for stream_name in ("stdout", "stderr"):
            stream = getattr(process, stream_name, None)
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, RuntimeError, ValueError) as error:
                cleanup_error = cleanup_error or str(error)
        if capture_error:
            cleanup_error = cleanup_error or capture_error
        guard = _read_guard_report(guard_path)
        try:
            source_immutable = _snapshot_pack_tree(source_root) == source_before
        except (OSError, ValueError):
            source_immutable = False
    duration = round(time.monotonic() - started, 3)
    if timed_out:
        return {
            "status": STATUS_FAIL,
            "detail": (
                f"合成 CPU contract smoke 超过 {timeout} 秒"
                + (
                    f"；进程树回收失败：{cleanup_error}"
                    if cleanup_error
                    else ""
                )
            ),
            "duration_seconds": duration,
            "timed_out": True,
            "process_identity": process_identity,
            "process_owner": getattr(owner, "kind", "unknown"),
            "process_tree_cleanup": "FAIL" if cleanup_error else "PASS",
            "output_limit_exceeded": output_limit_exceeded,
            "captured_output_bytes": captured_output_bytes,
            "guard": guard,
            "source_immutable": source_immutable,
        }
    if (
        cleanup_error
        or output_limit_exceeded
        or process.returncode != 0
        or guard["status"] != STATUS_PASS
        or not source_immutable
    ):
        detail = (stderr or stdout or "无输出").strip()[-2000:]
        return {
            "status": STATUS_FAIL,
            "detail": (
                "合成 CPU contract smoke 失败："
                + (
                    f"owned 进程树回收失败：{cleanup_error}"
                    if cleanup_error
                    else f"输出超过 {MAX_CAPTURED_OUTPUT_BYTES} 字节上限"
                    if output_limit_exceeded
                    else "常用 Python API 尝试守卫发现违规"
                    if guard["status"] != STATUS_PASS
                    else "原始方向包在运行期间发生变化"
                    if not source_immutable
                    else detail
                )
            ),
            "duration_seconds": duration,
            "timed_out": False,
            "process_identity": process_identity,
            "process_owner": getattr(owner, "kind", "unknown"),
            "process_tree_cleanup": "FAIL" if cleanup_error else "PASS",
            "output_limit_exceeded": output_limit_exceeded,
            "captured_output_bytes": captured_output_bytes,
            "guard": guard,
            "source_immutable": source_immutable,
        }
    return {
        "status": STATUS_PASS,
        "detail": (
            "合成 CPU train/evaluate/infer contract smoke 通过；"
            "常用 Python API 守卫未记录违规尝试"
        ),
        "duration_seconds": duration,
        "timed_out": False,
        "process_identity": process_identity,
        "process_owner": getattr(owner, "kind", "unknown"),
        "process_tree_cleanup": "PASS",
        "output_limit_exceeded": False,
        "captured_output_bytes": captured_output_bytes,
        "guard": guard,
        "source_immutable": source_immutable,
    }


def _required_environment_items() -> list[dict[str, object]]:
    system_name = platform.system()
    python_version = tuple(sys.version_info[:3])
    git = shutil.which("git")
    node = shutil.which("node")

    git_ok = False
    git_detail = "未找到 Git"
    if git:
        result = _run_command([git, "--version"], timeout=10)
        git_ok = result.returncode == 0
        git_detail = (result.stdout or result.stderr).strip() or "Git 无版本输出"

    node_ok = False
    node_detail = "未找到 Node.js"
    if node:
        result = _run_command([node, "--version"], timeout=10)
        node_ok = result.returncode == 0
        node_detail = (result.stdout or result.stderr).strip() or "Node.js 无版本输出"

    torch_ok, torch_detail = _probe_python_module(
        "torch",
        expression=(
            "import torch; "
            "x = torch.ones(1, device='cpu'); "
            "print(torch.__version__, x.device)"
        ),
        timeout=30,
    )
    fastapi_ok, fastapi_detail = _probe_python_module("fastapi")
    uvicorn_ok, uvicorn_detail = _probe_python_module("uvicorn")
    fts_ok, fts_detail = _sqlite_fts5_available()
    return [
        _required_item(
            "windows",
            system_name == "Windows",
            f"当前系统：{system_name}；本候选版只验收 Windows",
        ),
        _required_item(
            "python",
            python_version >= (3, 10, 0),
            "Python " + ".".join(str(part) for part in python_version),
        ),
        _required_item("git", git_ok, git_detail),
        _required_item("pytorch", torch_ok, torch_detail),
        _required_item("node", node_ok, node_detail),
        _required_item("paperflow_fastapi", fastapi_ok, fastapi_detail),
        _required_item("paperflow_uvicorn", uvicorn_ok, uvicorn_detail),
        _required_item("paperflow_sqlite_fts5", fts_ok, fts_detail),
    ]


def _optional_readiness() -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for item_id, module_name, detail, url in _OPTIONAL_COMPONENTS:
        available, probe_detail = _probe_python_module(module_name)
        result.append(
            {
                "id": item_id,
                "required": False,
                "status": (
                    STATUS_OPTIONAL_AVAILABLE
                    if available
                    else STATUS_OPTIONAL_MISSING
                ),
                "detail": f"{detail}；{probe_detail}",
                "download_url": url,
            }
        )
    data_items = (
        (
            "coco_data",
            "COCO_ROOT",
            "可选 COCO 真实数据",
            "https://cocodataset.org/#download",
        ),
        (
            "real_cv_data",
            "CV_REAL_DATA_ROOT",
            "可选用户真实 CV 数据",
            "https://paperswithcode.com/datasets?mod=images",
        ),
    )
    for item_id, variable, detail, url in data_items:
        raw = os.environ.get(variable, "")
        available = bool(raw) and Path(raw).is_dir()
        result.append(
            {
                "id": item_id,
                "required": False,
                "status": (
                    STATUS_OPTIONAL_AVAILABLE
                    if available
                    else STATUS_OPTIONAL_MISSING
                ),
                "detail": (
                    f"{detail}已登记"
                    if available
                    else f"{detail}未登记；大数据不会自动下载或打包"
                ),
                "download_url": url,
            }
        )
    return result


def check_runtime_environment(
    research_root: Path,
    *,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """返回完整 JSON 结果；可选项缺失不影响 ok，必需项缺失会失败。"""

    root = Path(research_root).resolve(strict=True)
    emit = progress or (lambda _message: None)
    required = _required_environment_items()
    direction_statuses = _load_direction_statuses(root)
    try:
        locations = _pack_locations(root)
        registry_error = ""
    except ValueError as error:
        locations = {}
        registry_error = str(error)

    pack_results: list[dict[str, object]] = []
    for index, direction in enumerate(EXPECTED_DIRECTIONS):
        seed = 1729 + index * 101
        timeout = 45
        location_record = locations.get(direction)
        ready = direction_statuses[direction] == "ready"
        base: dict[str, object] = {
            "id": direction,
            "required": ready,
            "availability": "ready" if ready else "pending",
            "seed": seed,
            "timeout_seconds": timeout,
        }
        if location_record is None:
            pack_results.append(
                {
                    **base,
                    "status": STATUS_MISSING,
                    "detail": registry_error or f"缺少必需方向包 PACK-{direction.upper()}",
                }
            )
            continue
        location, registry_version = location_record
        base["version"] = registry_version
        try:
            payload = _validate_pack_contract(
                location,
                direction,
                expected_version=registry_version,
            )
        except ValueError as error:
            pack_results.append(
                {**base, "status": STATUS_FAIL, "detail": str(error)}
            )
            continue
        base.update(
            {
                "version": payload.version,
                "device": payload.device,
                "cpu_fallback": payload.cpu_fallback,
            }
        )
        if not ready:
            pack_results.append(
                {
                    **base,
                    "status": STATUS_PENDING,
                    "detail": "方向待做；目录、版本、文件清单和 manifest 静态检查通过",
                }
            )
            continue
        missing_modules = [
            module_name
            for module_name in payload.required_modules
            if not _module_available(module_name)
        ]
        if missing_modules:
            pack_results.append(
                {
                    **base,
                    "status": STATUS_MISSING,
                    "detail": (
                        "正式评估所需依赖未安装："
                        + "、".join(missing_modules)
                        + "；不会静默改用不等价评估"
                    ),
                    "missing_modules": missing_modules,
                }
            )
            continue
        emit(f"[PASS] {direction}：GPU-only 静态合同通过")
        pack_results.append(
            {
                **base,
                "status": STATUS_PASS,
                "detail": "当前 ready；GPU-only 静态合同通过，未启动真实 CUDA",
            }
        )

    guard_reports = [
        item["guard"]
        for item in pack_results
        if isinstance(item.get("guard"), dict)
    ]
    guard_complete = all(
        item["status"] in {STATUS_MISSING, STATUS_PENDING, STATUS_PASS}
        or isinstance(item.get("guard"), dict)
        for item in pack_results
    )
    blocked_attempts = [
        violation
        for guard in guard_reports
        for violation in guard.get("violations", [])
        if isinstance(violation, dict)
    ]
    guard_ok = (
        guard_complete
        and all(guard.get("status") == STATUS_PASS for guard in guard_reports)
    )
    ok = (
        all(item["status"] == STATUS_PASS for item in required)
        and all(
            item["status"] in {STATUS_PASS, STATUS_PENDING}
            for item in pack_results
        )
        and guard_ok
    )
    return {
        "schema": "cv-experiment-workflow.runtime-environment.v1",
        "ok": ok,
        "python_api_attempt_guard": {
            "schema": "cv-experiment-workflow.python-api-attempt-guard.v1",
            "status": (
                STATUS_PASS
                if guard_ok
                else STATUS_FAIL
            ),
            "scope": (
                "检测并阻断审计钩子与 monkeypatch 覆盖的常用 Python API "
                "网络、子进程和安装尝试"
            ),
            "hard_isolation": False,
            "observed_attempts": blocked_attempts,
            "limitations": [
                "这不是操作系统级网络沙箱，不能证明完全断网",
                "未覆盖 os.startfile、ctypes 直接调用系统 API 等旁路",
                "受测代码可直接改写环境变量；离线相关环境变量只是提示，不是硬隔离",
            ],
        },
        "required": required,
        "domain_packs": pack_results,
        "optional_readiness": _optional_readiness(),
    }


def _load_direction_statuses(research_root: Path) -> dict[str, str]:
    candidates = [
        base / "config" / "directions" / "catalog.json"
        for base in (research_root, *research_root.parents)
    ]
    catalog_path = next((path for path in candidates if path.is_file()), None)
    if catalog_path is None:
        raise ValueError("缺少六方向唯一状态文件 config/directions/catalog.json")
    try:
        payload = parse_strict_json_object(
            stable_read_file(catalog_path),
            label="Direction catalog",
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f"六方向状态文件无效：{error}") from error
    directions = payload.get("directions")
    if (
        payload.get("schema") != "cvwf.direction-catalog.v1"
        or not isinstance(directions, list)
        or len(directions) != len(EXPECTED_DIRECTIONS)
    ):
        raise ValueError("六方向状态文件 schema 或方向数量无效")
    result: dict[str, str] = {}
    for item in directions:
        if not isinstance(item, dict):
            raise ValueError("六方向状态条目必须是对象")
        slug = item.get("slug")
        status = item.get("status")
        if (
            slug not in EXPECTED_DIRECTIONS
            or slug in result
            or status not in {"ready", "pending"}
        ):
            raise ValueError("六方向 slug、状态或唯一性无效")
        result[str(slug)] = str(status)
    if set(result) != set(EXPECTED_DIRECTIONS):
        raise ValueError("六方向状态文件缺失方向")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "检查 Windows、依赖与六个 CV 方向包，并检测常用 Python API 尝试。"
        )
    )
    parser.add_argument(
        "--research-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--json-out", required=True, type=Path)
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
        report = check_runtime_environment(
            args.research_root,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
    except (OSError, ValueError) as error:
        report = {
            "schema": "cv-experiment-workflow.runtime-environment.v1",
            "ok": False,
            "error": str(error),
            "required": [],
            "domain_packs": [],
            "optional_readiness": [],
        }
    try:
        write_new_json_report(
            args.json_out,
            report,
            forbidden_roots=(args.research_root, *args.protected_root),
        )
    except (OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "schema": "cv-experiment-workflow.runtime-environment.v1",
                    "ok": False,
                    "error": f"环境报告未写入：{error}",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
