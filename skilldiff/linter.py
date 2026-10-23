import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "when", "where", "what", "your",
    "you", "are", "but", "not", "has", "have", "had", "from", "into", "onto", "its",
    "it", "is", "be", "was", "were", "will", "would", "can", "could", "should", "may",
    "might", "must", "does", "did", "do", "doing", "done", "also", "just", "only",
    "than", "then", "there", "their", "them", "they", "each", "every", "some", "any",
    "all", "both", "one", "two", "use", "using", "used", "app", "application", "file",
    "please", "fix", "issue", "problem", "broken", "currently", "instead", "right",
    "now", "still", "never", "always", "make", "makes", "made", "get", "gets", "got",
}


def _prompt_terms(text: str) -> set[str]:
    terms = set()
    for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text or ""):
        word = token.lower().replace("_", "-")
        if word in _STOPWORDS or len(word) < 3:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if len(word) > len(suffix) + 3 and word.endswith(suffix):
                word = word[: -len(suffix)]
                break
        terms.add(word)
    return terms


def load_task_prompts(prompts_path: Path) -> list[tuple[str, str]]:
    """Read prompt text from task YAML files without validating the whole task."""
    path = Path(prompts_path).expanduser()
    if path.is_dir():
        files = sorted([*path.rglob("*.yaml"), *path.rglob("*.yml")])
    elif any(char in str(path) for char in "*?["):
        files = sorted(Path().glob(str(path)))
    else:
        files = [path]
    prompts: list[tuple[str, str]] = []
    for file in files:
        try:
            data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        task_id = str(data.get("id") or file.stem)
        values = data.get("prompts")
        if not isinstance(values, list):
            values = [data.get("prompt") or ""]
        for value in values:
            text = str(value or "").strip()
            if text:
                prompts.append((task_id, text))
    return prompts


def lint_prompt_coverage(skill_dir: Path, prompts_path: Path) -> dict[str, Any]:
    """Report task prompts that share little language with the skill description."""
    result = lint_skill(skill_dir)
    if not result.valid:
        return {"tasks": [], "warnings": [], "stats": {}}
    skill_md = Path(skill_dir).resolve() / "SKILL.md"
    content = skill_md.read_text(encoding="utf-8")
    end_idx = content.find("\n---", 3)
    frontmatter = content[3:end_idx] if end_idx != -1 else ""
    try:
        meta = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError:
        meta = {}
    description = str(meta.get("description") or "")
    description_terms = _prompt_terms(description)
    body_terms = _prompt_terms(content[end_idx + 4 :] if end_idx != -1 else "")
    known_terms = description_terms | body_terms
    leakage_candidates = body_terms - description_terms

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    uncovered = 0
    leaked = 0
    covered_scores: list[float] = []
    prompt_items = load_task_prompts(prompts_path)
    if not prompt_items:
        warnings.append(f"No task prompts found at {prompts_path}")
    for task_id, prompt in prompt_items:
        terms = _prompt_terms(prompt)
        if not terms:
            continue
        matched = sorted(terms & known_terms)
        missing = sorted(terms - known_terms)
        leaked_terms = sorted(terms & leakage_candidates)
        score = len(matched) / len(terms)
        covered_scores.append(score)
        row = {
            "task_id": task_id,
            "coverage": round(score, 3),
            "matched_terms": matched[:10],
            "missing_terms": missing[:10],
            "leaked_terms": leaked_terms[:10],
        }
        rows.append(row)
        if not matched:
            uncovered += 1
            warnings.append(
                f"Task '{task_id}' shares no distinctive terms with the skill "
                f"description; possible trigger gap. Prompt terms include: "
                + ", ".join(missing[:6])
            )
        elif score < 0.1:
            uncovered += 1
            warnings.append(
                f"Task '{task_id}' has low trigger overlap ({score:.0%}); possible "
                "trigger gap. Missing terms include: " + ", ".join(missing[:6])
            )
        if len(leaked_terms) >= 2:
            leaked += 1
            warnings.append(
                f"Task '{task_id}' leaks solution terms found only in the skill body: "
                + ", ".join(leaked_terms[:6])
                + ". Describe the user symptom instead of the internal fix to avoid "
                "spoiling the test for the control arm."
            )
    average = sum(covered_scores) / len(covered_scores) if covered_scores else None
    return {
        "tasks": rows,
        "warnings": warnings,
        "stats": {
            "tasks": len(rows),
            "uncovered": uncovered,
            "leaked": leaked,
            "average_coverage": round(average, 3) if average is not None else None,
        },
    }



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
        if path.name != name_str:
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
