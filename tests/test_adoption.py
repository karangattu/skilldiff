import json

from skilldiff.adoption import (
    adoption_actions,
    adoption_summary,
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
) -> dict:
    run = {
        "arm": arm,
        "model": "m",
        "task_id": "t1",
        "task_category": category,
        "task_split": "dev",
        "repetition": rep,
        "status": status,
        "score": score,
        "success": score >= 1.0,
        "skill_invoked": invoked,
    }
    if loads is not None:
        run["skill_invocations"] = loads
    return run


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
    assert summary["adoption_failed_arms"] == ["treatment"]


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
