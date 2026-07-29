from __future__ import annotations

import argparse
import json
from pathlib import Path

from .data import build_dataset_manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验 GZSL NPZ 并输出稳定的数据内容清单",
    )
    parser.add_argument("--data-path", required=True, type=Path)
    arguments = parser.parse_args()
    print(
        json.dumps(
            build_dataset_manifest(arguments.data_path),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
