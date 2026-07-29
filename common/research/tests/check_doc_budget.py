from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "cv-experiment-workflow"
REFERENCE_NAMES = (
    "workflow.md",
    "code-and-provenance.md",
    "agents.md",
    "gzsl.md",
)
FORBIDDEN_NAMES = {"README.md", "CHANGELOG.md", "INSTALLATION_GUIDE.md"}


def line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def check_doc_budget() -> tuple[int, int]:
    skill_lines = line_count(SKILL / "SKILL.md")
    if skill_lines > 220:
        raise AssertionError(f"SKILL.md has {skill_lines} lines; limit is 220")

    references = [SKILL / "references" / name for name in REFERENCE_NAMES]
    missing = [path.name for path in references if not path.is_file()]
    if missing:
        raise AssertionError(f"missing required references: {', '.join(missing)}")
    reference_lines = sum(line_count(path) for path in references)
    if reference_lines > 800:
        raise AssertionError(
            f"four references have {reference_lines} lines; limit is 800"
        )

    forbidden = []
    forbidden_names = {name.upper() for name in FORBIDDEN_NAMES}
    for path in SKILL.rglob("*"):
        if not path.is_file() or path.name.upper() not in forbidden_names:
            continue
        relative = path.relative_to(SKILL)
        is_domain_repository_readme = (
            path.name.upper() == "README.MD"
            and len(relative.parts) == 5
            and relative.parts[:2] == ("assets", "domain-packs")
            and relative.parts[3:] == ("payload", "README.md")
        )
        if not is_domain_repository_readme:
            forbidden.append(relative.as_posix())
    forbidden.sort()
    if forbidden:
        raise AssertionError(f"forbidden Skill docs: {', '.join(forbidden)}")
    return skill_lines, reference_lines


if __name__ == "__main__":
    skill_lines, reference_lines = check_doc_budget()
    print(f"doc budget ok: SKILL={skill_lines}/220, references={reference_lines}/800")
