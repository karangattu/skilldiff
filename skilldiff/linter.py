import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class LintResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def lint_skill(skill_dir: Path) -> LintResult:
    path = Path(skill_dir).resolve()
    errors: list[str] = []
    warnings: list[str] = []

    if not path.is_dir():
        return LintResult(valid=False, errors=[f"Skill directory not found: {path}"])

    skill_md = path / "SKILL.md"
    if not skill_md.is_file():
        return LintResult(valid=False, errors=[f"Missing SKILL.md in {path}"])

    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        return LintResult(valid=False, errors=[f"Cannot read SKILL.md: {exc}"])

    words = len(content.split())
    chars = len(content)
    est_tokens = round(chars / 3.8)
    stats = {
        "words": words,
        "characters": chars,
        "estimated_tokens": est_tokens,
    }

    if not content.startswith("---"):
        errors.append("SKILL.md must begin with '---' YAML frontmatter")
        return LintResult(valid=False, errors=errors, warnings=warnings, stats=stats)

    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        errors.append("SKILL.md frontmatter is unclosed (missing closing '---')")
        return LintResult(valid=False, errors=errors, warnings=warnings, stats=stats)

    frontmatter_str = content[3:end_idx]
    body = content[end_idx + 4 :].strip()

    try:
        meta = yaml.safe_load(frontmatter_str) or {}
    except yaml.YAMLError as exc:
        errors.append(f"Invalid YAML frontmatter: {exc}")
        return LintResult(valid=False, errors=errors, warnings=warnings, stats=stats)

    if not isinstance(meta, dict):
        errors.append("YAML frontmatter must be a key-value mapping")
        return LintResult(valid=False, errors=errors, warnings=warnings, stats=stats)

    name = meta.get("name")
    if not name or not str(name).strip():
        errors.append("Frontmatter missing required 'name' field")
    else:
        name_str = str(name).strip()
        if not re.match(r"^[a-z0-9][a-z0-9._-]*$", name_str):
            errors.append(
                f"Skill name '{name_str}' must be lowercase alphanumeric with hyphens/dots"
            )
        if path.name != name_str and not (path / "SKILL.md").exists():
            warnings.append(
                f"Skill name '{name_str}' differs from directory name '{path.name}'"
            )

    desc = meta.get("description")
    if not desc or not str(desc).strip():
        errors.append("Frontmatter missing required 'description' field")
    else:
        desc_str = str(desc).strip()
        if len(desc_str) < 20:
            errors.append(
                f"Description is too short ({len(desc_str)} chars); describe triggers and use cases"
            )
        elif len(desc_str) > 800:
            warnings.append(
                f"Description is long ({len(desc_str)} chars); "
                "consider shortening to reduce token overhead"
            )

        generic_triggers = {"helps with code", "useful tool", "general helper", "do coding"}
        if any(g in desc_str.lower() for g in generic_triggers):
            warnings.append(
                "Description contains overly broad phrasing; add specific trigger keywords"
            )

    code_fences = len(re.findall(r"^```", body, re.MULTILINE))
    if code_fences % 2 != 0:
        errors.append("SKILL.md has unclosed code fence (odd number of ``` blocks)")

    if words > 3000:
        warnings.append(
            f"Skill content is large ({words} words / ~{est_tokens} tokens); "
            "consider moving detailed instructions to references/ using progressive disclosure"
        )

    link_pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    for match in link_pattern.finditer(body):
        target = match.group(2).split("#")[0].strip()
        if target and not target.startswith(("http://", "https://", "mailto:", "#")):
            target_path = path / target
            if not target_path.exists():
                warnings.append(f"Broken relative file link: '{target}' does not exist on disk")

    return LintResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        stats=stats,
    )
