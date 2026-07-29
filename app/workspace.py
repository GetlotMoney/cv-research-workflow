from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any


_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_DISPLAY_NAME = re.compile(r"[\w\u4e00-\u9fff（）()· -]{1,64}", re.UNICODE)


def instantiate_workspace(
    system_root: Path,
    *,
    display_name: str,
    slug: str,
) -> dict[str, Any]:
    root = Path(system_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"系统总目录无效：{root}")
    if (
        not isinstance(display_name, str)
        or _DISPLAY_NAME.fullmatch(display_name.strip()) is None
    ):
        raise ValueError(
            "个人工作流名称只允许中英文、数字、空格、括号、短横线和下划线，"
            "长度为 1 到 64"
        )
    if not isinstance(slug, str) or _SLUG.fullmatch(slug) is None:
        raise ValueError("个人短名只允许小写字母、数字和短横线")

    direction_catalog = _read_direction_catalog(
        root / "config" / "directions" / "catalog.json"
    )
    users_root = root / "users"
    users_root.mkdir(exist_ok=True)
    resolved_users_root = users_root.resolve(strict=True)
    if (
        not resolved_users_root.is_dir()
        or resolved_users_root != users_root.absolute()
        or resolved_users_root.parent != root
    ):
        raise ValueError("users 必须是系统总目录内的真实目录，不能是目录链接")
    workspace_root = users_root / slug
    if workspace_root.exists():
        raise FileExistsError(f"个人工作流已经存在：{workspace_root}")

    workspace_id = str(uuid.uuid4())
    skill_id = f"{slug}-cv-workflow-{workspace_id[:8]}"
    staging = users_root / f".{slug}-initializing-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        for name in (
            "repositories",
            "deliveries",
            "inbox",
            "materials",
            ".runtime",
        ):
            (staging / name).mkdir()
        manifest = {
            "schema": "cvwf.personal-workspace.v2",
            "workspace_id": workspace_id,
            "slug": slug,
            "display_name": display_name.strip(),
            "skill_id": skill_id,
            "status": "active",
            "direction_catalog_version": direction_catalog["version"],
        }
        _write_json(staging / "workspace.json", manifest)
        _write_json(
            staging / "REPOSITORY_INDEX.json",
            {
                "schema": "cvwf.repository-index.v2",
                "workspace_id": workspace_id,
                "repositories": [],
            },
        )
        (staging / "SKILL.md").write_text(
            _skill_text(
                root=root,
                workspace_root=workspace_root,
                display_name=display_name.strip(),
                skill_id=skill_id,
            ),
            encoding="utf-8",
        )
        _promote_staging(staging, workspace_root)
    except BaseException:
        if staging.is_dir():
            import shutil

            shutil.rmtree(staging)
        raise
    return {
        "schema": "cvwf.workspace-instantiation.v1",
        "status": "created",
        "workspace_id": workspace_id,
        "skill_id": skill_id,
        "workspace_root": str(workspace_root),
    }


def _promote_staging(staging: Path, workspace_root: Path) -> None:
    for attempt in range(5):
        try:
            staging.rename(workspace_root)
            return
        except PermissionError as error:
            if workspace_root.exists():
                raise FileExistsError(
                    f"个人工作流已经存在：{workspace_root}"
                ) from error
            if getattr(error, "winerror", None) not in {5, 32} or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def _read_direction_catalog(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(f"六方向 catalog 不存在：{path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"六方向 catalog 不是有效 JSON：{path}") from error
    if not isinstance(payload, dict):
        raise ValueError("六方向 catalog 顶层必须是对象")
    if payload.get("schema") != "cvwf.direction-catalog.v1":
        raise ValueError("六方向 catalog schema 不受支持")
    if not isinstance(payload.get("version"), str) or not payload["version"]:
        raise ValueError("六方向 catalog version 不能为空")
    if not isinstance(payload.get("directions"), list):
        raise ValueError("六方向 catalog directions 必须是列表")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _skill_text(
    *,
    root: Path,
    workspace_root: Path,
    display_name: str,
    skill_id: str,
) -> str:
    return f"""---
name: {skill_id}
description: "Use when 用户要求在{display_name}中选择研究方向、创建或继续方向仓库、管理科研实验。"
---

# {display_name}

这是通用 CV 科研工作流实例化后的个人入口，不复制通用状态机。

## 固定位置

- 系统总目录：`{root}`
- 个人工作区：`{workspace_root}`
- 仓库目录：`repositories`
- 外来材料入口：`inbox`
- 研究材料目录：`materials`
- 科研交付目录：`deliveries`

## 三层关系

1. 使用 `cv-experiment-workflow` 处理所有方向共用的科研规则。
2. 本 Skill 负责个人工作区和方向仓库。
3. 进入具体仓库后，读取该仓库的 `SKILL.md`。

本阶段只管理科研，不自动启动论文写作。
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="从通用模板实例化个人工作流")
    parser.add_argument("--system-root", required=True, type=Path)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--slug", required=True)
    arguments = parser.parse_args()
    result = instantiate_workspace(
        arguments.system_root,
        display_name=arguments.display_name,
        slug=arguments.slug,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
