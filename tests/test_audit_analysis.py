import json
from argparse import Namespace

import pytest

from skilldiff.cli import cmd_diagnose
from skilldiff.compare import compare_results, format_comparison
from skilldiff.diagnose import diagnose_run
from skilldiff.profiler import measure_skill_footprint


def run(rep=1, **changes):
    record = {
        "model": "m",
        "task_id": "t",
        "repetition": rep,
        "status": "ok",
        "grade_status": "graded",
        "score": 1.0,
        "success": True,
        "cost": 1.0,
        "duration": 10.0,
        "input_tokens": 10,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "skill_invoked": False,
    }
    return record | changes


def results(records, policy="exclude", **changes):
    return {
        "name": "audit",
        "models": ["m"],
        "tasks": ["t"],
        "harness": "claude",
        "failure_policy": {"agent_failure": policy},
        "runs": {"treatment": records},
    } | changes


def diagnose(tmp_path, data):
    path = tmp_path / "results.json"
    path.write_text(json.dumps(data))
    return diagnose_run(path)


def test_compare_ignores_unmatched_repetitions():
    comparison = compare_results(results([run(), run(2, cost=9)]), results([run()]))
    assert comparison["efficiency"]["cost"] == {
        "a": 1.0,
        "b": 1.0,
        "delta": 0.0,
        "a_runs": 1,
        "b_runs": 1,
        "matched": True,
    }
    assert "[n=1/1 runs]" in format_comparison(comparison)


def test_compare_uses_both_known_observations_for_each_metric():
    a = results([run(), run(2, cost=99, duration=20, input_tokens=20)])
    b = results(
        [
            run(
                duration=None,
                input_tokens=None,
                output_tokens=None,
                cache_read_tokens=None,
                cache_creation_tokens=None,
            ),
            run(2, cost=None),
        ]
    )
    comparison = compare_results(a, b)
    expected = {"cost": (1, 1, 0), "time": (20, 10, -10), "tokens": (20, 10, -10)}
    for metric, values in expected.items():
        summary = comparison["efficiency"][metric]
        assert (summary["a"], summary["b"], summary["delta"]) == values
        assert summary["a_runs"] == summary["b_runs"] == 1


def test_compare_does_not_fall_back_to_totals_when_records_have_no_shared_pair():
    a = results([run()], overall={"skill": {"total_cost": 99}})
    b = results([run(2)], overall={"skill": {"total_cost": 1}})
    summary = compare_results(a, b)["efficiency"]["cost"]
    assert summary["delta"] is None
    assert summary["a_runs"] == summary["b_runs"] == 0


@pytest.mark.parametrize("policy,expected_a,expected_count", [("exclude", 1, 1), ("zero", 5, 2)])
def test_compare_applies_each_saved_failure_policy(policy, expected_a, expected_count):
    a = results([run(), run(2, status="timeout", cost=9)], policy)
    b = results([run(), run(2)], policy)
    summary = compare_results(a, b)["efficiency"]["cost"]
    assert summary["a"] == expected_a
    assert summary["b"] == 1
    assert summary["a_runs"] == summary["b_runs"] == expected_count


def test_compare_warns_and_strictly_refuses_different_failure_policies():
    a, b = results([run()], "exclude"), results([run()], "zero")
    assert any(
        "failure polic" in warning.lower()
        for warning in compare_results(a, b)["provenance_warnings"]
    )
    with pytest.raises(ValueError, match="(?i)failure polic"):
        compare_results(a, b, strict=True)


@pytest.mark.parametrize("policy", ["exclude", "zero"])
def test_compare_ignores_failed_session_partial_checks(policy):
    a = results([run(feedback='{"checks": [true]}')], policy)
    b = results([run(status="timeout", feedback='{"checks": [false]}')], policy)
    assert compare_results(a, b)["newly_failing"] == []


def test_compare_check_changes_use_only_shared_repetitions():
    a = results(
        [
            run(feedback='{"checks": [false]}'),
            run(2, feedback='{"checks": [true]}'),
            run(3, feedback='{"checks": [true]}'),
        ]
    )
    b = results([run(feedback='{"checks": [false]}')])
    assert compare_results(a, b)["newly_failing"] == []


def test_compare_check_changes_use_only_both_known_checks():
    a = results([run(feedback='{"checks": [true]}'), run(2, feedback='{"checks": [false]}')])
    b = results([run(), run(2, feedback='{"checks": [false]}')])
    assert compare_results(a, b)["newly_failing"] == []


def test_compare_score_and_adoption_ignore_unmatched_repetitions():
    a = results(
        [run(skill_invoked=True), run(2, score=0, skill_invoked=False)],
        overall={"skill": {"task_score": 0.5}},
    )
    b = results([run(skill_invoked=True)], overall={"skill": {"task_score": 1.0}})
    comparison = compare_results(a, b)
    assert comparison["score"]["a"] == comparison["score"]["b"] == 100
    assert comparison["score"]["delta_pp"] == 0
    assert comparison["score"]["a_runs"] == comparison["score"]["b_runs"] == 1
    assert comparison["adoption"] == {"a": "1/1", "b": "1/1"}


@pytest.mark.parametrize("policy,expected", [("exclude", 100), ("zero", 50)])
def test_compare_scores_apply_saved_agent_failure_policy(policy, expected):
    a = results(
        [run(), run(2, status="timeout", score=1.0)], policy, overall={"skill": {"task_score": 1.0}}
    )
    b = results([run(), run(2)], policy, overall={"skill": {"task_score": 1.0}})
    assert compare_results(a, b)["score"]["a"] == expected


def test_compare_effects_use_the_same_four_arm_observations():
    a = results(
        [],
        runs={"control": [run(score=0), run(2, score=1)], "treatment": [run(), run(2, score=0)]},
        overall={"paired": {"score": {"mean_diff": 0}}},
    )
    b = results(
        [],
        runs={"control": [run(score=0)], "treatment": [run()]},
        overall={"paired": {"score": {"mean_diff": 1}}},
    )
    assert compare_results(a, b)["effect"] == {
        "a_pp": 100,
        "b_pp": 100,
        "delta_pp": 0,
    }


@pytest.mark.parametrize("arm", ["control", "treatment", "baseline"])
def test_diagnose_lists_agent_failures_from_every_arm(tmp_path, arm):
    arms = {"control": [run()], "treatment": [run()], "baseline": [run()]}
    arms[arm] = [run(status="timeout", exit_code=-1, error="deadline")]
    report = diagnose(tmp_path, results([], runs=arms))
    assert report["agent_failures"] == [
        {
            "arm": arm,
            "task_id": "t",
            "model": "m",
            "rep": 1,
            "status": "timeout",
            "error": "deadline",
        }
    ]
    assert not any("Clean run" in item for item in report["recommendations"])


def test_diagnose_reports_grader_failure_and_usable_pair_count(tmp_path):
    report = diagnose(
        tmp_path,
        results(
            [],
            runs={
                "control": [run()],
                "treatment": [run(grade_status="error", score=None)],
            },
        ),
    )
    assert report["grader_failures"][0]["arm"] == "treatment"
    assert report["total_pairs"] == 1
    assert report["usable_score_pairs"] == 0
    assert not any("Clean run" in item for item in report["recommendations"])


def test_diagnose_counts_actual_pairs_and_missing_expected_results(tmp_path):
    report = diagnose(
        tmp_path,
        results(
            [],
            runs={
                "control": [run()],
                "treatment": [run(2)],
            },
            settings={"runs": 2},
        ),
    )
    assert report["total_pairs"] == report["usable_score_pairs"] == 0
    assert report["unmatched_runs"] == 2
    assert report["planned_pairs"] == 2
    assert not any("Clean run" in item for item in report["recommendations"])


def test_diagnose_unknown_adoption_is_not_a_clean_evaluation(tmp_path):
    report = diagnose(
        tmp_path,
        results(
            [],
            runs={
                "control": [run()],
                "treatment": [run(skill_invoked=None)],
            },
        ),
    )
    assert report["unknown_adoption_runs"] == 1
    assert not any("Clean run" in item for item in report["recommendations"])


def test_diagnose_does_not_infer_token_bloat_from_partial_telemetry(tmp_path):
    report = diagnose(
        tmp_path,
        results(
            [],
            runs={
                "control": [run(output_tokens=None)],
                "treatment": [run(input_tokens=100, output_tokens=None)],
            },
        ),
    )
    assert report["token_bloat_tasks"] == []
    assert not any("Clean run" in item for item in report["recommendations"])


def test_diagnose_requires_baseline_pairs_to_match_primary_keys(tmp_path):
    report = diagnose(
        tmp_path,
        results(
            [],
            runs={
                "control": [run()],
                "treatment": [run()],
                "baseline": [run(2)],
            },
            runs_per_arm=1,
            skill_comparison={"include_baseline": True},
        ),
    )
    assert not any("Clean run" in item for item in report["recommendations"])
    assert report["baseline_pairs"] == 0


def test_diagnose_requires_usable_baseline_scores(tmp_path):
    report = diagnose(
        tmp_path,
        results(
            [],
            runs={
                "control": [run()],
                "treatment": [run()],
                "baseline": [run(grade_status="ungraded", score=None)],
            },
            runs_per_arm=1,
            skill_comparison={"include_baseline": True},
        ),
    )
    assert not any("Clean run" in item for item in report["recommendations"])
    assert report["baseline_usable_score_pairs"] == 0


def test_diagnose_requires_records_for_the_planned_tasks(tmp_path):
    report = diagnose(
        tmp_path,
        results(
            [],
            runs={
                "control": [run(task_id="unexpected")],
                "treatment": [run(task_id="unexpected")],
            },
            runs_per_arm=1,
        ),
    )
    assert not any("Clean run" in item for item in report["recommendations"])
    assert report["missing_planned_pairs"] == 1


def test_diagnose_cli_exposes_failures_and_usable_coverage(tmp_path, capsys):
    path = tmp_path / "results.json"
    path.write_text(
        json.dumps(
            results(
                [],
                runs={
                    "control": [run(status="timeout")],
                    "treatment": [run()],
                },
            )
        )
    )
    assert cmd_diagnose(Namespace(run_dir=str(tmp_path), json=False)) == 0
    output = capsys.readouterr().out
    assert "Agent failures: 1" in output
    assert "Usable score pairs: 0/1" in output
    assert "Clean run" not in output


@pytest.mark.parametrize("parent", [".claude", ".codex", ".agents"])
def test_footprint_counts_skills_under_hidden_install_parents(tmp_path, parent):
    skill = tmp_path / parent / "skills" / "test-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("hello world")
    (skill / ".metadata").write_text("excluded")
    cache = skill / "__pycache__"
    cache.mkdir()
    (cache / "module.pyc").write_bytes(b"excluded")
    assert measure_skill_footprint(skill)["total_bytes"] == 11
    assert measure_skill_footprint(skill)["file_count"] == 1
