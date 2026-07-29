"""构建通用科研项目实例的完整人工审核包。

这个脚本只复制当前工作树中的文件并生成清单，不修改训练代码、实验账本或 Git 历史。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import subprocess
import tempfile
import tokenize
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


DOC_VERSION = "DOC-AUDIT-V1.0.0"
PACKAGE_DIRNAME = "audit-package"
ZIP_NAME = f"cv-research-workflow-audit-package-{DOC_VERSION}.zip"
GENERATED_DIRS = (
    "code-snapshot",
    "ledger-snapshot",
    "evidence-original-docs",
    "code-review",
    "manifests",
    "verification",
)
SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".runtime"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def require_within(path: Path, parent: Path) -> None:
    if not is_within(path, parent):
        raise RuntimeError(f"拒绝操作项目范围外路径：{path}")


def git_value(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip()


def git_state(root: Path) -> dict[str, object]:
    return {
        "root": str(root.resolve()),
        "head": git_value(root, "rev-parse", "HEAD"),
        "branch": git_value(root, "branch", "--show-current"),
        "dirty": bool(git_value(root, "status", "--porcelain")),
        "snapshot_semantics": "当前工作树字节快照，包含尚未提交的在途修改",
    }


def should_skip(path: Path) -> bool:
    return bool(SKIP_PARTS.intersection(path.parts)) or path.suffix.lower() in SKIP_SUFFIXES


def iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
        if path.is_symlink():
            raise RuntimeError(f"审核包不跟随符号链接或目录联接：{path}")
        if path.is_file() and not should_skip(path.relative_to(root)):
            yield path


def copy_one(
    source: Path,
    destination: Path,
    package_root: Path,
    source_map: list[dict[str, object]],
    category: str,
) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"缺少需要打包的文件：{source}")
    if source.is_symlink():
        raise RuntimeError(f"审核包不跟随符号链接：{source}")
    require_within(destination, package_root)
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖已有审核文件：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    source_digest = sha256(source)
    snapshot_digest = sha256(destination)
    source_map.append(
        {
            "category": category,
            "source_absolute_path": str(source.resolve()),
            "snapshot_relative_path": destination.relative_to(package_root).as_posix(),
            "bytes": source.stat().st_size,
            "sha256": source_digest,
            "identical": source_digest == snapshot_digest,
        }
    )


def copy_tree(
    source_root: Path,
    destination_root: Path,
    package_root: Path,
    source_map: list[dict[str, object]],
    category: str,
) -> None:
    if not source_root.is_dir():
        return
    for source in iter_files(source_root):
        copy_one(
            source,
            destination_root / source.relative_to(source_root),
            package_root,
            source_map,
            category,
        )


def copy_selected_roots(
    repository: Path,
    snapshot_root: Path,
    directories: tuple[str, ...],
    files: tuple[str, ...],
    package_root: Path,
    source_map: list[dict[str, object]],
    category: str,
) -> None:
    for directory in directories:
        copy_tree(
            repository / directory,
            snapshot_root / directory,
            package_root,
            source_map,
            category,
        )
    for filename in files:
        source = repository / filename
        if source.exists():
            copy_one(
                source,
                snapshot_root / filename,
                package_root,
                source_map,
                category,
            )


def first_doc_line(node: ast.Module) -> str:
    doc = ast.get_docstring(node, clean=True)
    return doc.splitlines()[0].strip() if doc else "未写模块级说明；请结合文件名和符号表审核。"


def code_role(relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/").lower()
    name = Path(normalized).name
    if "/tests/" in f"/{normalized}" or normalized.startswith("tests/"):
        return "自动化测试：证明正常流程、边界条件和回归行为。"
    if name == "rw.py":
        return "命令行总入口：把用户命令分发到工作流内核。"
    if "workflow_adapter" in name:
        return "项目适配器：把通用任务翻译成当前 CV 项目的真实执行命令。"
    if "project_skills" in name:
        return "项目专属 Skill 生成与身份绑定。"
    if "releases" in name:
        return "模板、安装副本和实例之间的发布锁定与漂移检查。"
    if any(key in normalized for key in ("task", "run", "evidence", "records")):
        return "机器账本：记录任务、运行、证据和状态变化。"
    if any(key in normalized for key in ("idea", "source", "hypothesis")):
        return "论文与想法：保存来源、假设、版本和可检验条件。"
    if any(key in normalized for key in ("promotion", "claim", "evaluation")):
        return "评估与晋级：决定结果能否进入下一阶段或支持论文主张。"
    if any(key in normalized for key in ("template", "module", "catalog")):
        return "模板与模块目录：定义可复用实验骨架和路线能力。"
    if normalized.startswith("src/") or "/src/" in f"/{normalized}":
        return "实例业务代码：当前 CV 项目的最小可运行实现。"
    return "工作流支撑代码：请优先核对输入、输出、失败处理和文件写入范围。"


def build_code_notes(package_root: Path) -> None:
    snapshot_root = package_root / "code-snapshot"
    sections: list[str] = [
        "# 逐文件代码审核索引",
        "",
        f"- 文档版本：`{DOC_VERSION}`",
        "- 用途：帮助人工审核者逐个打开代码文件；这里不是运行时代码，也不替代源码内注释。",
        "- 读取顺序：先看“职责”，再看顶层类/函数，最后打开相邻测试核对实际行为。",
        "",
    ]
    repository_dirs = (
        ("通用模板仓库", snapshot_root / "template-repository"),
        ("科研项目实例", snapshot_root / "project-instance"),
    )
    for label, repository_dir in repository_dirs:
        sections.extend([f"## {label}", ""])
        python_files = [
            path
            for path in iter_files(repository_dir)
            if path.suffix.lower() == ".py"
        ]
        for path in python_files:
            relative = path.relative_to(repository_dir).as_posix()
            with tokenize.open(path) as stream:
                source = stream.read()
            tree = ast.parse(source, filename=str(path))
            symbols: list[str] = []
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    symbols.append(f"`class {node.name}`")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                    symbols.append(f"`{prefix} {node.name}`")
            line_count = len(source.splitlines())
            sections.extend(
                [
                    f"### `{relative}`",
                    "",
                    f"- 职责：{code_role(relative)}",
                    f"- 模块说明：{first_doc_line(tree)}",
                    f"- 规模：{line_count} 行。",
                    f"- 顶层入口：{', '.join(symbols) if symbols else '无顶层类或函数；通常是配置、常量或包入口。'}",
                    f"- 审核路径：`code-snapshot/{repository_dir.name}/{relative}`",
                    "",
                ]
            )
    output = package_root / "code-review" / "FILE_BY_FILE_CODE_NOTES.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(sections), encoding="utf-8")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def package_files(package_root: Path, *, include_checksums: bool) -> list[Path]:
    files = list(iter_files(package_root))
    if not include_checksums:
        checksum_path = package_root / "manifests" / "SHA256SUMS.txt"
        files = [path for path in files if path != checksum_path]
    return files


def validate_python(package_root: Path) -> dict[str, object]:
    checked = 0
    for path in package_files(package_root, include_checksums=False):
        if path.suffix.lower() != ".py":
            continue
        with tokenize.open(path) as stream:
            source = stream.read()
        compile(source, str(path), "exec", dont_inherit=True)
        checked += 1
    return {"status": "PASS", "python_files_checked": checked}


def validate_source_map(package_root: Path, source_map: list[dict[str, object]]) -> None:
    failures: list[str] = []
    for entry in source_map:
        snapshot = package_root / str(entry["snapshot_relative_path"])
        source = Path(str(entry["source_absolute_path"]))
        if not source.exists() or not snapshot.exists():
            failures.append(str(entry["snapshot_relative_path"]))
            continue
        if sha256(source) != sha256(snapshot):
            failures.append(str(entry["snapshot_relative_path"]))
    if failures:
        raise RuntimeError(f"源文件和审核副本不一致：{failures[:10]}")


def write_checksums(package_root: Path) -> int:
    checksum_path = package_root / "manifests" / "SHA256SUMS.txt"
    lines = [
        f"{sha256(path)}  {path.relative_to(package_root).as_posix()}"
        for path in package_files(package_root, include_checksums=False)
    ]
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def verify_checksums(package_root: Path) -> None:
    checksum_path = package_root / "manifests" / "SHA256SUMS.txt"
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        target = package_root / relative
        if sha256(target) != expected:
            raise RuntimeError(f"审核包校验失败：{relative}")


def create_zip(package_root: Path, zip_path: Path) -> None:
    require_within(zip_path, package_root.parent / "deliverables")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in package_files(package_root, include_checksums=True):
            archive.write(path, Path(PACKAGE_DIRNAME) / path.relative_to(package_root))
    with zipfile.ZipFile(zip_path, "r") as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ZIP 内文件损坏：{bad}")
        with tempfile.TemporaryDirectory(prefix="cv-audit-verify-") as temp_dir:
            archive.extractall(temp_dir)
            extracted = Path(temp_dir) / PACKAGE_DIRNAME
            verify_checksums(extracted)


def write_zip_sidecar(zip_path: Path) -> None:
    sidecar = zip_path.with_suffix(zip_path.suffix + ".sha256")
    sidecar.write_text(f"{sha256(zip_path)}  {zip_path.name}\n", encoding="utf-8")


def build(template_root: Path, instance_root: Path) -> dict[str, object]:
    package_root = instance_root / "docs" / PACKAGE_DIRNAME
    if not package_root.is_dir():
        raise FileNotFoundError(f"缺少人工说明目录：{package_root}")
    for dirname in GENERATED_DIRS:
        if (package_root / dirname).exists():
            raise FileExistsError(f"生成目录已经存在，请先人工确认：{package_root / dirname}")
    required_docs = [package_root / f"{number:02d}_" for number in range(13)]
    existing_names = {path.name for path in package_root.glob("*.md")}
    missing_numbers = [
        number
        for number, prefix in enumerate(required_docs)
        if not any(name.startswith(prefix.name) for name in existing_names)
    ]
    if missing_numbers:
        raise RuntimeError(f"缺少说明文档编号：{missing_numbers}")

    source_map: list[dict[str, object]] = []
    template_snapshot = package_root / "code-snapshot" / "template-repository"
    instance_snapshot = package_root / "code-snapshot" / "project-instance"
    copy_selected_roots(
        template_root,
        template_snapshot,
        ("skills/cv-experiment-workflow", "tests", "tools"),
        (
            "AGENTS.md",
            "CHANGELOG.md",
            "LICENSE",
            "pyproject.toml",
            "README.md",
            "REPOSITORY_INDEX.json",
            "REPOSITORY_INDEX.md",
        ),
        package_root,
        source_map,
        "通用模板代码快照",
    )
    copy_selected_roots(
        instance_root,
        instance_snapshot,
        ("src", "tests", "configs"),
        (
            ".gitignore",
            "AGENTS.md",
            "pyproject.toml",
            "README.md",
            "REPOSITORY_INDEX.json",
            "REPOSITORY_INDEX.md",
            "SKILL.md",
            "WORKFLOW.md",
            "workflow_adapter.py",
        ),
        package_root,
        source_map,
        "项目实例代码快照",
    )
    copy_tree(
        instance_root / ".experiment-workflow",
        package_root / "ledger-snapshot" / ".experiment-workflow",
        package_root,
        source_map,
        "机器账本快照",
    )

    evidence_root = package_root / "evidence-original-docs"
    for source, relative, category in (
        (
            instance_root / "docs" / "WORKFLOW_ARCHITECTURE_AND_IDEA_TO_CODE.md",
            Path("instance/docs/WORKFLOW_ARCHITECTURE_AND_IDEA_TO_CODE.md"),
            "实例架构原文",
        ),
        (
            instance_root / "docs" / "TECH_STACK_HISTORY.md",
            Path("instance/docs/TECH_STACK_HISTORY.md"),
            "实例技术演进原文",
        ),
        (
            template_root / "docs" / "TECH_STACK_HISTORY.md",
            Path("template/docs/TECH_STACK_HISTORY.md"),
            "模板技术演进原文",
        ),
        (
            template_root
            / "docs"
            / "diagrams"
            / "cv_research_workflow_current_framework_strict_editorial_v3.html",
            Path(
                "template/docs/diagrams/"
                "cv_research_workflow_current_framework_strict_editorial_v3.html"
            ),
            "流程图原文",
        ),
    ):
        if source.exists():
            copy_one(source, evidence_root / relative, package_root, source_map, category)
    for directory in ("experiments", "manifests", "reviews"):
        copy_tree(
            instance_root / "docs" / directory,
            evidence_root / "instance" / "docs" / directory,
            package_root,
            source_map,
            f"实例 {directory} 原始证据",
        )
    copy_tree(
        template_root / "docs" / "reviews",
        evidence_root / "template" / "docs" / "reviews",
        package_root,
        source_map,
        "模板审核原始证据",
    )

    build_code_notes(package_root)
    manifests = package_root / "manifests"
    write_json(
        manifests / "SOURCE_SNAPSHOT_MAP.json",
        {
            "schema": "cv.audit-source-map/v1",
            "document_version": DOC_VERSION,
            "entries": source_map,
        },
    )
    validate_source_map(package_root, source_map)
    python_result = validate_python(package_root)
    counts: dict[str, int] = {}
    for entry in source_map:
        category = str(entry["category"])
        counts[category] = counts.get(category, 0) + 1
    package_info = {
        "schema": "cv.audit-package/v1",
        "document_version": DOC_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "供用户逐文件审核新科研工作流的代码、账本、证据与说明",
        "template_repository": git_state(template_root),
        "instance_repository": git_state(instance_root),
        "version_identities": {
            "system": "SYS-V2.10.1",
            "generic_skill_release": "SKILL-RELEASE-V1.2.1",
            "project_skill": "SKILL-PROJECT-V1.0.0",
            "document_package": DOC_VERSION,
        },
        "source_snapshot_counts": counts,
        "exclusions": [
            ".git 与 Git 内部对象",
            "runs/ 下的大型运行产物",
            "数据集、模型权重和外部环境",
            "__pycache__、.pytest_cache、.pyc 等可再生缓存",
            ".experiment-workflow/.runtime 临时锁和运行态缓存",
        ],
        "review_policy": {
            "rounds": 2,
            "reason": (
                "本次新增文档、只读快照和独立打包工具；"
                "不改变训练、评估、状态机或实验运行行为。"
            ),
        },
    }
    write_json(manifests / "PACKAGE_INFO.json", package_info)
    write_json(
        package_root / "verification" / "BUILD_RESULT.json",
        {
            "schema": "cv.audit-build-result/v1",
            "document_version": DOC_VERSION,
            "status": "PASS",
            "source_files_compared": len(source_map),
            "all_source_snapshots_identical": all(
                bool(entry["identical"]) for entry in source_map
            ),
            "python_syntax": python_result,
            "zip_verification": "脚本返回成功前会执行 ZIP CRC 与解压后 SHA-256 复核",
        },
    )
    audit_result = package_root / "verification" / "AUDIT_RESULT.md"
    audit_result.write_text(
        "# 两轮审核结果\n\n"
        f"- 文档版本：`{DOC_VERSION}`\n"
        "- 当前状态：审核进行中，不能视为最终放行。\n"
        "- 第一轮：待填写事实准确性结论。\n"
        "- 第二轮：待填写文件与 ZIP 完整性结论。\n",
        encoding="utf-8",
    )
    checksum_count = write_checksums(package_root)
    verify_checksums(package_root)
    instance_zip = instance_root / "docs" / "deliverables" / ZIP_NAME
    create_zip(package_root, instance_zip)
    write_zip_sidecar(instance_zip)
    template_zip = template_root / "docs" / "deliverables" / ZIP_NAME
    template_zip.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(instance_zip, template_zip)
    shutil.copy2(
        instance_zip.with_suffix(instance_zip.suffix + ".sha256"),
        template_zip.with_suffix(template_zip.suffix + ".sha256"),
    )
    return {
        "status": "PASS",
        "package_root": str(package_root),
        "instance_zip": str(instance_zip),
        "template_zip": str(template_zip),
        "source_files": len(source_map),
        "checksummed_files": checksum_count,
        "python_files": python_result["python_files_checked"],
        "zip_sha256": sha256(instance_zip),
    }


def refresh_source_snapshots(
    package_root: Path,
    template_root: Path,
    instance_root: Path,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """最终封包前把已登记快照刷新为当前工作树字节，并更新自动生成索引。"""

    source_map_path = package_root / "manifests" / "SOURCE_SNAPSHOT_MAP.json"
    source_map_document = json.loads(source_map_path.read_text(encoding="utf-8"))
    source_map = list(source_map_document["entries"])
    builder_source = template_root / "tools" / "build_audit_package.py"
    builder_relative = "code-snapshot/template-repository/tools/build_audit_package.py"
    if not any(
        entry["snapshot_relative_path"] == builder_relative for entry in source_map
    ):
        source_map.append(
            {
                "category": "通用模板代码快照",
                "source_absolute_path": str(builder_source.resolve()),
                "snapshot_relative_path": builder_relative,
                "bytes": 0,
                "sha256": "",
                "identical": False,
            }
        )
    for entry in source_map:
        source = Path(str(entry["source_absolute_path"]))
        snapshot = package_root / str(entry["snapshot_relative_path"])
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"最终封包前源文件缺失或变成链接：{source}")
        require_within(snapshot, package_root)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, snapshot)
        source_digest = sha256(source)
        entry["bytes"] = source.stat().st_size
        entry["sha256"] = source_digest
        entry["identical"] = source_digest == sha256(snapshot)
    source_map_document["entries"] = source_map
    write_json(source_map_path, source_map_document)
    validate_source_map(package_root, source_map)
    build_code_notes(package_root)

    python_result = validate_python(package_root)
    counts: dict[str, int] = {}
    for entry in source_map:
        category = str(entry["category"])
        counts[category] = counts.get(category, 0) + 1
    package_info_path = package_root / "manifests" / "PACKAGE_INFO.json"
    package_info = json.loads(package_info_path.read_text(encoding="utf-8"))
    package_info["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    package_info["template_repository"] = git_state(template_root)
    package_info["instance_repository"] = git_state(instance_root)
    package_info["source_snapshot_counts"] = counts
    package_info["review_policy"] = {
        "rounds": 2,
        "reason": (
            "本次新增文档、只读快照和独立打包工具；"
            "不改变训练、评估、状态机或实验运行行为。"
        ),
    }
    write_json(package_info_path, package_info)
    write_json(
        package_root / "verification" / "BUILD_RESULT.json",
        {
            "schema": "cv.audit-build-result/v1",
            "document_version": DOC_VERSION,
            "status": "PASS",
            "source_files_compared": len(source_map),
            "all_source_snapshots_identical": all(
                bool(entry["identical"]) for entry in source_map
            ),
            "python_syntax": python_result,
            "zip_verification": "脚本返回成功前会执行 ZIP CRC 与解压后 SHA-256 复核",
        },
    )
    return source_map, python_result


def finalize(template_root: Path, instance_root: Path) -> dict[str, object]:
    package_root = instance_root / "docs" / PACKAGE_DIRNAME
    for dirname in GENERATED_DIRS:
        if not (package_root / dirname).is_dir():
            raise FileNotFoundError(f"缺少生成目录，不能完成最终封包：{dirname}")
    source_map, python_result = refresh_source_snapshots(
        package_root,
        template_root,
        instance_root,
    )
    checksum_count = write_checksums(package_root)
    verify_checksums(package_root)
    instance_zip = instance_root / "docs" / "deliverables" / ZIP_NAME
    template_zip = template_root / "docs" / "deliverables" / ZIP_NAME
    create_zip(package_root, instance_zip)
    write_zip_sidecar(instance_zip)
    template_zip.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(instance_zip, template_zip)
    shutil.copy2(
        instance_zip.with_suffix(instance_zip.suffix + ".sha256"),
        template_zip.with_suffix(template_zip.suffix + ".sha256"),
    )
    if sha256(instance_zip) != sha256(template_zip):
        raise RuntimeError("实例仓库与模板仓库中的 ZIP 不一致")
    return {
        "status": "PASS",
        "package_root": str(package_root),
        "instance_zip": str(instance_zip),
        "template_zip": str(template_zip),
        "source_files": len(source_map),
        "checksummed_files": checksum_count,
        "python_files": python_result["python_files_checked"],
        "zip_sha256": sha256(instance_zip),
        "zip_bytes": instance_zip.stat().st_size,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template-root", type=Path, required=True)
    parser.add_argument("--instance-root", type=Path, required=True)
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="审核完成后重新生成 SHA-256 清单与两个仓库中的最终 ZIP。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    template_root = args.template_root.resolve()
    instance_root = args.instance_root.resolve()
    result = (
        finalize(template_root, instance_root)
        if args.finalize
        else build(template_root, instance_root)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
