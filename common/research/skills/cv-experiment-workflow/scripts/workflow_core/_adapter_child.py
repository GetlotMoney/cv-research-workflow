from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit("受控 Adapter child 参数不足")
    snapshot_root, mode, module, *module_args = sys.argv[1:]
    scripts_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(scripts_root))
    from workflow_core.adapters import _run_snapshot_child

    _run_snapshot_child(snapshot_root, mode, module, module_args)


if __name__ == "__main__":
    main()
