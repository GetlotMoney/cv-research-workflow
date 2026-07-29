from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "skills" / "cv-experiment-workflow" / "scripts" / "rw.py"
SCRIPTS = CLI.parent
SUBPROCESS_TIMEOUT_SECONDS = 15.0


def run_cli(
    *args: object,
    check: bool = True,
    timeout: float = SUBPROCESS_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", str(CLI), *(str(arg) for arg in args)],
        capture_output=True,
        encoding="utf-8",
        text=True,
        check=False,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"CLI failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def cli_json(*args: object) -> dict[str, Any]:
    result = run_cli(*args)
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise AssertionError("CLI stdout must contain one JSON object")
    return payload


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"expected a JSON object: {path}")
    return payload
