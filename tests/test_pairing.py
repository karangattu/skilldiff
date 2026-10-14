import json
from pathlib import Path

from skilldiff.pairing import (
    _Pair,
    balanced_arm_order,
    balanced_three_order,
    postprocess_grade,
    safe_path_component,
    save_run_artifacts,
)


def test_safe_path_component():
    assert safe_path_component("claude-3.5-sonnet") == "claude-3.5-sonnet"
    assert safe_path_component("task/with/slashes") == "task_with_slashes"
    assert safe_path_component("__invalid__") == "invalid"
    assert safe_path_component("") == "item"


def test_balanced_arm_order_rotation():
    order1 = balanced_arm_order("model", "task", 1, 42)
    order2 = balanced_arm_order("model", "task", 2, 42)
    assert set(order1) == {"control", "treatment"}
    assert set(order2) == {"control", "treatment"}
    assert order1 != order2


def test_balanced_three_order_rotation():
    order1 = balanced_three_order("model", "task", 1, 42)
    order2 = balanced_three_order("model", "task", 2, 42)
    order3 = balanced_three_order("model", "task", 3, 42)
    assert set(order1) == {"control", "treatment", "baseline"}
    assert len(order1) == 3
    # First arm should rotate across repetitions
    first_arms = {order1[0], order2[0], order3[0]}
    assert len(first_arms) == 3


def test_postprocess_grade_harness_error():
    rec = {
        "status": "error",
        "failure_kind": "harness_error",
        "score": 1.0,
        "success": True,
        "grade_status": "graded",
    }
    postprocess_grade(rec)
    assert rec["score"] is None
    assert rec["partial_score"] == 1.0
    assert rec["success"] is False
    assert rec["grade_status"] == "ungraded"
    assert rec["analysis_excluded"] is True


def test_postprocess_grade_agent_failure_policy():
    rec = {
        "status": "error",
        "failure_kind": "timeout",
        "score": 0.8,
        "success": True,
        "grade_status": "graded",
    }
    postprocess_grade(rec, {"agent_failure": "zero"})
    assert rec["partial_score"] == 0.8
    assert rec["score"] == 0.0

    rec2 = {
        "status": "error",
        "failure_kind": "timeout",
        "score": 0.8,
        "success": True,
        "grade_status": "graded",
    }
    postprocess_grade(rec2, {"agent_failure": "exclude"})
    assert rec2["analysis_excluded"] is True


def test_save_run_artifacts(tmp_path: Path):
    arm_dir = tmp_path / "arm"
    arm_dir.mkdir()
    record = {"status": "ok", "score": 1.0}
    save_run_artifacts(arm_dir, record, "transcript content", "diff content")

    assert (arm_dir / "transcript.txt").read_text() == "transcript content"
    assert (arm_dir / "diff.patch").read_text() == "diff content"
    assert (arm_dir / "run.json").exists()
    saved_record = json.loads((arm_dir / "run.json").read_text())
    assert "artifact_hashes" in saved_record
    assert "transcript.txt" in saved_record["artifact_hashes"]
    assert "diff.patch" in saved_record["artifact_hashes"]


def test_experiment_reexports_pairing():
    import skilldiff.experiment as exp

    assert exp._Pair is _Pair
    assert exp.balanced_arm_order is balanced_arm_order
    assert exp.balanced_three_order is balanced_three_order
    assert exp.safe_path_component is safe_path_component
