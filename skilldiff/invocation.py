"""How the agent is told to use the skill.

``implicit`` leaves the task prompt alone. The agent has to notice the skill
on its own, so adoption measures whether the SKILL.md description triggers on
the task wording.

``explicit`` names the skill in the prompt. Adoption then measures whether the
agent uses a skill it is told to use, not whether it would have found one.
The gap between the two is the discovery gap.
"""

from typing import Optional

IMPLICIT = "implicit"
EXPLICIT = "explicit"
INVOCATION_MODES = (IMPLICIT, EXPLICIT)

# Placeholder a custom prompt template uses to name the arm's skill(s).
SKILL_PLACEHOLDER = "{skill}"


def resolve_mode(experiment_mode: str, task_mode: Optional[str] = None) -> str:
    """The mode for one task: the task's own setting wins over the experiment."""
    if task_mode:
        return str(task_mode).strip().lower()
    return str(experiment_mode or IMPLICIT).strip().lower()


def skill_reference(names: list[str]) -> str:
    """Backticked skill name(s) for a prompt: `` `a` `` or `` `a`, `b` ``."""
    unique = [n for n in dict.fromkeys(n for n in names if n)]
    return ", ".join(f"`{n}`" for n in unique)


def build_instruction(names: list[str], template: Optional[str] = None) -> str:
    """The sentence that names the skill for the agent.

    Without a template the default reads "Use the `a` skill for this task."
    A template replaces it and gets ``{skill}`` substituted with the same
    backticked reference.
    """
    if not names:
        return ""
    if template is None:
        if len(names) == 1:
            return f"Use the {skill_reference(names)} skill for this task."
        return f"Use the skills {skill_reference(names)} for this task."
    return template.replace(SKILL_PLACEHOLDER, skill_reference(names))


def template_names_skill(template: Optional[str] = None) -> bool:
    """Whether the instruction names the skill, and so tells the arms apart.

    The default names it. A custom template that omits ``{skill}`` says the
    same thing to every arm, which keeps A/B revision runs fair.
    """
    if template is None:
        return True
    return SKILL_PLACEHOLDER in template


def instruction_for_arm(
    experiment_mode: str,
    task_mode: Optional[str] = None,
    names: Optional[list[str]] = None,
    template: Optional[str] = None,
) -> str:
    """The sentence to prepend to an arm's prompt, or "" for none.

    Only arms that actually carry a skill get one, so a control arm without
    the skill is never told to use it.
    """
    if not names:
        return ""
    if resolve_mode(experiment_mode, task_mode) != EXPLICIT:
        return ""
    return build_instruction(names, template)
