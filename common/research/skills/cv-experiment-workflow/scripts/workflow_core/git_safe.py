from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path


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
)
DEFAULT_GIT_STDERR_LIMIT = 64 * 1024


@dataclass(frozen=True, slots=True)
class BoundedGitResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def safe_git_environment(
    *,
    ceiling_directory: Path | None = None,
) -> dict[str, str]:
    environment = os.environ.copy()
    blocked = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_REPLACE_REF_BASE",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_EXEC_PATH",
        "GIT_PREFIX",
    }
    blocked.update(
        name
        for name in environment
        if name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    )
    for name in blocked:
        environment.pop(name, None)
    environment.update(
        {
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GIT_PAGER": "",
            "PAGER": "",
        }
    )
    if ceiling_directory is not None:
        environment["GIT_CEILING_DIRECTORIES"] = str(
            Path(ceiling_directory).resolve(strict=True)
        )
    return environment


def run_git_bounded(
    root: Path,
    *arguments: str,
    stdout_limit: int,
    stderr_limit: int = DEFAULT_GIT_STDERR_LIMIT,
    timeout: float = 30,
) -> BoundedGitResult:
    if stdout_limit < 0 or stderr_limit < 0:
        raise ValueError("Git output limits must be non-negative")
    command = [
        "git",
        "--no-replace-objects",
        "--no-pager",
        *GIT_SAFE_CONFIG,
        "-C",
        str(root),
        *arguments,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=safe_git_environment(),
        )
    except OSError as error:
        raise ValueError(f"Unable to start Git: {root}") from error
    assert process.stdout is not None
    assert process.stderr is not None

    outputs: dict[str, bytearray] = {
        "stdout": bytearray(),
        "stderr": bytearray(),
    }
    exceeded: list[str] = []
    guard = threading.Lock()

    def read_stream(name: str, stream, limit: int) -> None:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            with guard:
                target = outputs[name]
                remaining = limit - len(target)
                if remaining >= len(chunk):
                    target.extend(chunk)
                    continue
                if remaining > 0:
                    target.extend(chunk[:remaining])
                exceeded.append(name)
            try:
                process.kill()
            except OSError:
                pass
            return

    threads = [
        threading.Thread(
            target=read_stream,
            args=("stdout", process.stdout, stdout_limit),
            daemon=True,
        ),
        threading.Thread(
            target=read_stream,
            args=("stderr", process.stderr, stderr_limit),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    try:
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait()
            for thread in threads:
                thread.join(timeout=5)
            raise ValueError(f"Git command timed out: {arguments[0]}") from error
        for thread in threads:
            thread.join(timeout=5)
        if any(thread.is_alive() for thread in threads):
            process.kill()
            raise ValueError("Git output reader did not terminate")
        if exceeded:
            raise ValueError(
                "Git output exceeded its bounded limit: "
                + ", ".join(sorted(set(exceeded)))
            )
        return BoundedGitResult(
            returncode=returncode,
            stdout=bytes(outputs["stdout"]),
            stderr=bytes(outputs["stderr"]),
        )
    finally:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for thread in threads:
            thread.join(timeout=5)
        process.stdout.close()
        process.stderr.close()
