import json

from skilldiff.adoption import (
    adoption_actions,
    adoption_summary,
    decision_runs,
    merge_gate,
    no_effect_findings,
)
from skilldiff.diagnose import diagnose_run


def _run(
    arm: str,
    *,
    rep: int,
    invoked,
    category: str = "intended",
    score: float = 0.5,
    status: str = "ok",
    loads=None,
    split: str = "dev",
    task: str = "t1",
) -> dict:
    run = {
        "arm": arm,
        "model": "m",
        "task_id": task,
        "task_category": category,
        "task_split": split,
        "repetition": rep,
        "status": status,
        "score": score,
        "success": score >= 1.0,
    }
    if invoked is not _MISSING:
        run["skill_invoked"] = invoked
    if loads is not None:
        run["skill_invocations"] = loads
    return run


_MISSING = object()


def test_plain_adoption_ignores_failed_runs_and_unknowns():
    results = {
        "preset": "skill",
        "arm_labels": {"control": "Control", "treatment": "Skill"},
        "task_details": [
            {"id": "t1", "category": "intended", "prompt": "fix the total"}
        ],
    }
    runs = {
        "control": [_run("control", rep=1, invoked=False)],
        "treatment": [
            _run("treatment", rep=1, invoked=True, loads=2),
            _run("treatment", rep=2, invoked=False),
            _run("treatment", rep=3, invoked=None),
            _run("treatment", rep=4, invoked=False, status="timeout"),
        ],
    }
    summary = adoption_summary(results, runs)
    assert list(summary["arms"]) == ["treatment"]
    treatment = summary["arms"]["treatment"]
    assert treatment["hits"] == 1
    assert treatment["known"] == 2
    assert treatment["eligible"] == 3
    assert treatment["unknown"] == 1
    assert treatment["failed"] == 1
    assert treatment["hit_rate"] == 0.5
    assert treatment["loads"] == 2
    assert treatment["reading"] == "Partial adoption"
    # Two measured runs cannot read a rate from: thin evidence, not a failure.
    assert treatment["thin"] is True
    assert summary["adoption_thin_arms"] == ["treatment"]
    assert summary["adoption_failed_arms"] == []
    assert summary["adoption_failed"] is False


def test_adoption_gate_needs_three_measured_runs():
    results = {"preset": "skill", "task_details": []}
    thin = {
        "treatment": [
            _run("treatment", rep=1, invoked=True),
            _run("treatment", rep=2, invoked=False),
        ]
    }
    summary = adoption_summary(results, thin)
    assert summary["adoption_thin_arms"] == ["treatment"]
    assert summary["adoption_failed"] is False

    measured = {
        "treatment": [
            _run("treatment", rep=1, invoked=True),
            _run("treatment", rep=2, invoked=False),
            _run("treatment", rep=3, invoked=False),
        ]
    }
    summary = adoption_summary(results, measured)
    assert summary["adoption_failed_arms"] == ["treatment"]
    assert summary["adoption_failed"] is True


def test_adoption_gate_fails_when_the_skill_never_loaded():
    results = {"preset": "skill", "task_details": []}
    runs = {"treatment": [_run("treatment", rep=1, invoked=False)]}
    summary = adoption_summary(results, runs)
    assert summary["arms"]["treatment"]["status"] == "not used"
    assert summary["adoption_failed_arms"] == ["treatment"]
    assert summary["adoption_failed"] is True


def test_unmeasured_adoption_blocks_but_unreported_adoption_does_not():
    results = {"preset": "skill", "task_details": []}
    unmeasured = {"treatment": [_run("treatment", rep=1, invoked=None)]}
    summary = adoption_summary(results, unmeasured)
    assert summary["arms"]["treatment"]["status"] == "unmeasured"
    assert summary["adoption_unmeasured_arms"] == ["treatment"]
    assert summary["adoption_failed"] is True

    # A run record with no adoption field at all was never measured.
    unreported = {"treatment": [_run("treatment", rep=1, invoked=_MISSING)]}
    summary = adoption_summary(results, unreported)
    assert summary["arms"]["treatment"]["status"] == "not measured"
    assert summary["adoption_unmeasured_arms"] == []
    assert summary["adoption_not_measured_arms"] == ["treatment"]
    assert summary["adoption_failed"] is False


def test_decision_basis_uses_held_out_runs():
    results = {
        "preset": "skill",
        "task_details": [
            {"id": "dev1", "category": "intended", "split": "dev"},
            {"id": "ho1", "category": "intended", "split": "held-out"},
        ],
    }
    runs = {
        "treatment": [
            _run("treatment", rep=1, invoked=False, task="dev1"),
            _run("treatment", rep=1, invoked=True, task="ho1", split="held-out"),
        ]
    }
    decision_runs_by_arm, basis = decision_runs(results, runs)
    assert basis == "held-out"
    assert [run["task_id"] for run in decision_runs_by_arm["treatment"]] == ["ho1"]

    summary = adoption_summary(results, decision_runs_by_arm, basis=basis)
    assert summary["arms"]["treatment"]["hits"] == 1
    assert summary["adoption_failed"] is False


def test_pr_preset_reports_no_adoption():
    results = {"preset": "pr", "comparison": {"control_commit": "a"}}
    runs = {
        "control": [_run("control", rep=1, invoked=True)],
        "treatment": [_run("treatment", rep=1, invoked=True)],
    }
    summary = adoption_summary(results, runs)
    assert summary["arms"] == {}
    assert summary["adoption_failed"] is False


def _experiment(tmp_path):
    from skilldiff.config import ExperimentConfig

    skill = tmp_path / "skill"
    skill.mkdir(exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\nname: s\ndescription: d\n---\n", encoding="utf-8"
    )
    return ExperimentConfig(
        name="adopt", skill=skill, models=["sonnet"], tasks_patterns=[], runs=3
    )


def _arm_runs(arm: str, task: str, split: str, invoked) -> list[dict]:
    return [
        {
            "model": "sonnet",
            "task_id": task,
            "repetition": rep,
            "arm": arm,
            "status": "ok",
            "score": 0.5,
            "grade_status": "graded",
            "task_split": split,
            "skill_invoked": invoked,
        }
        for rep in (1, 2, 3)
    ]


def test_aggregation_judges_adoption_on_the_decision_basis(tmp_path):
    from skilldiff.aggregation import aggregate_results
    from skilldiff.config import TaskConfig

    cfg = _experiment(tmp_path)
    dev = TaskConfig(id="dev1", prompt="p", category="intended", split="dev")
    held = TaskConfig(id="ho1", prompt="p", category="intended", split="held-out")
    run_dir = tmp_path / "runs" / "2026-10-15T000000Z"
    run_dir.mkdir(parents=True)
    control = _arm_runs("control", "dev1", "dev", False) + _arm_runs(
        "control", "ho1", "held-out", False
    )
    # The skill rarely loaded on dev but always loaded on held-out runs.
    treatment = _arm_runs("treatment", "dev1", "dev", False) + _arm_runs(
        "treatment", "ho1", "held-out", True
    )
    results = aggregate_results(
        cfg, [dev, held], run_dir, "2026-10-15T000000Z", control, treatment, [], False
    )
    assert results["adoption_basis"] == "held-out"
    assert results["adoption_failed"] is False
    assert results["skill_usage"]["treatment"]["hits"] == 3
    assert results["skill_usage"]["treatment"]["eligible"] == 6
    assert results["adoption_failed_arms"] == []

    # The same experiment with a held-out arm that never loaded fails.
    treatment = _arm_runs("treatment", "dev1", "dev", False) + _arm_runs(
        "treatment", "ho1", "held-out", False
    )
    results = aggregate_results(
        cfg, [dev, held], run_dir, "2026-10-15T000000Z", control, treatment, [], False
    )
    assert results["adoption_basis"] == "held-out"
    assert results["adoption_failed"] is True
    assert results["adoption_failed_arms"] == ["treatment"]


def test_ab_adoption_reports_both_arms_and_trigger_failures():
    results = {
        "preset": "revision",
        "skill_a": "skills/a",
        "skill_b": "skills/b",
        "arm_labels": {"control": "Skill A", "treatment": "Skill B"},
        "task_details": [
            {"id": "t1", "category": "intended", "prompt": "fix the total"},
            {"id": "irrelevant", "category": "irrelevant", "prompt": "rename a file"},
        ],
    }
    runs = {
        "control": [
            _run("control", rep=1, invoked=True),
            _run("control", rep=2, invoked=True),
        ],
        "treatment": [
            _run("treatment", rep=1, invoked=False),
            _run("treatment", rep=2, invoked=True),
            _run("treatment", rep=3, invoked=True),
        ],
        "baseline": [_run("baseline", rep=1, invoked=None)],
    }
    summary = adoption_summary(results, runs)
    assert list(summary["arms"]) == ["control", "treatment"]
    assert summary["arms"]["control"]["hits"] == 2
    assert summary["arms"]["treatment"]["hits"] == 2
    assert summary["arms"]["treatment"]["partial_intended_tasks"] == ["t1"]
    assert summary["adoption_failed_arms"] == []

    actions = adoption_actions(results, summary)
    advice = " ".join(action["advice"] for action in actions)
    assert "loaded inconsistently on intended task(s)" in advice
    assert not any(
        action["arm"] == "control" and action["kind"] == "description"
        for action in actions
    )
    assert any(action["kind"] == "confound" for action in actions)


def test_adoption_action_includes_task_prompt_for_total_miss():
    results = {
        "preset": "skill",
        "skill": "skills/scale",
        "arm_labels": {"treatment": "Scale"},
        "task_details": [
            {
                "id": "t1",
                "category": "intended",
                "prompt": "use the scale to weigh the package",
            }
        ],
    }
    runs = {
        "treatment": [_run("treatment", rep=1, invoked=False, status="ok")],
    }
    summary = adoption_summary(results, runs)
    actions = adoption_actions(results, summary)
    assert any("No intended run loaded Scale" in item["advice"] for item in actions)
    assert any("use the scale to weigh the package" in item["advice"] for item in actions)
    assert any("skilldiff lint skills/scale" in item["advice"] for item in actions)


def test_no_effect_finding_when_hit_runs_do_not_beat_control():
    results = {
        "preset": "skill",
        "task_details": [
            {"id": "t1", "category": "intended", "prompt": "fix the total"}
        ],
    }
    runs = {
        "control": [
            _run("control", rep=1, invoked=False, score=0.8),
            _run("control", rep=2, invoked=False, score=0.8),
        ],
        "treatment": [
            _run("treatment", rep=1, invoked=True, score=0.5, loads=1),
            _run("treatment", rep=2, invoked=True, score=0.5, loads=1),
        ],
    }
    findings = no_effect_findings(results, runs)
    assert len(findings) == 1
    assert findings[0]["task_id"] == "t1"
    actions = adoption_actions(results, adoption_summary(results, runs), findings)
    assert any(action["kind"] == "body" for action in actions)


def test_unavailable_skill_gets_discovery_action():
    results = {
        "preset": "skill",
        "skill": "skills/scale",
        "task_details": [
            {"id": "t1", "category": "intended", "prompt": "use the scale"}
        ],
    }
    runs = {
        "treatment": [
            {
                **_run("treatment", rep=1, invoked=False),
                "skill_available": False,
            }
        ],
    }
    summary = adoption_summary(results, runs)
    actions = adoption_actions(results, summary)
    assert any(action["kind"] == "availability" for action in actions)


def test_diagnose_exposes_skill_usage_and_next_actions(tmp_path):
    results = {
        "name": "adoption",
        "preset": "skill",
        "skill": "skills/scale",
        "harness": "claude",
        "models": ["m"],
        "tasks": ["t1"],
        "tasks_count": 1,
        "runs_per_arm": 1,
        "arm_labels": {"control": "Control", "treatment": "Scale"},
        "task_details": [
            {
                "id": "t1",
                "category": "intended",
                "split": "dev",
                "prompt": "weigh the package with the scale",
            }
        ],
        "runs": {
            "control": [_run("control", rep=1, invoked=False)],
            "treatment": [_run("treatment", rep=1, invoked=False)],
        },
    }
    (tmp_path / "results.json").write_text(json.dumps(results), encoding="utf-8")
    report = diagnose_run(tmp_path)
    assert report["adoption_failed"] is True
    assert report["skill_usage"]["treatment"]["hits"] == 0
    assert any(
        "weigh the package with the scale" in action["advice"]
        for action in report["next_actions"]
    )
    assert any("failed to trigger" in rec for rec in report["recommendations"])


def test_merge_gate_surfaces_thin_held_out_adoption_when_all_runs_exceed_floor():
    results = {
        "preset": "skill",
        "arm_labels": {"treatment": "Scale"},
        "task_details": [
            {"id": "dev1", "category": "intended", "split": "dev"},
            {"id": "ho1", "category": "intended", "split": "held-out"},
        ],
    }
    runs = {
        "treatment": [
            _run("treatment", rep=1, invoked=True, task="dev1", split="dev"),
            _run("treatment", rep=2, invoked=True, task="dev1", split="dev"),
            _run("treatment", rep=3, invoked=True, task="dev1", split="dev"),
            _run("treatment", rep=1, invoked=True, task="ho1", split="held-out"),
            _run("treatment", rep=2, invoked=False, task="ho1", split="held-out"),
        ]
    }
    all_summary = adoption_summary(results, runs, basis="all")
    assert all_summary["arms"]["treatment"]["thin"] is False
    decision_arm_runs, basis = decision_runs(results, runs)
    gate = adoption_summary(results, decision_arm_runs, basis=basis)
    assert gate["adoption_thin_arms"] == ["treatment"]

    actions = adoption_actions(results, merge_gate(all_summary, gate))
    thin_actions = [a for a in actions if a["kind"] == "thin"]
    assert len(thin_actions) == 1
    assert "only 2 held-out run(s)" in thin_actions[0]["advice"]

