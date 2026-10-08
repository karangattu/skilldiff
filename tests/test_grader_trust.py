"""Grader-trust features: scope helpers, grader_ignore, multiple known-good solutions,
grader notes, blast-radius classification, auth probe, and regrade."""

import json
import stat
from pathlib import Path

import pytest
import yaml

from skilldiff.cli import cmd_check, cmd_init, cmd_regrade, cmd_run
from skilldiff.config import GraderConfig, load_task
from skilldiff.diagnose import diagnose_run
from skilldiff.grader import (
    Grader,
    GradeResult,
    check_blast_radius,
    validate_grader_against_directories,
    validate_grader_payload,
)
from skilldiff.reporter import _score_cell, extract_checks, extract_notes
from skilldiff.runner import AgentRunner, auth_login_hint, looks_like_auth_error
from skilldiff.scope import (
    filter_diff,
    is_blast_violation,
    path_matches,
    scope_pattern_warnings,
)


class Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


# ------------------------------------------------------------------ scope helpers


def test_path_matches_at_root_and_any_depth():
    assert path_matches("data/a.csv", ["data/*"])
    assert path_matches("outputs/measurements/copy/data/a.csv", ["data/*"])
    assert not path_matches("app.R", ["data/*"])


def test_filter_diff_drops_ignored_files_only():
    diff = (
        "diff --git a/app.R b/app.R\n--- a/app.R\n+++ b/app.R\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/outputs/x.R b/outputs/x.R\nnew file mode 100644\n--- /dev/null\n"
        "+++ b/outputs/x.R\n@@ -0,0 +1 @@\n+copy\n"
    )
    kept = filter_diff(diff, ["outputs/*"])
    assert "app.R" in kept and "outputs/x.R" not in kept
    assert filter_diff(diff, []) == diff


def test_blast_radius_skips_ignored_paths():
    changed = ["app.R", "outputs/measurements/baseline/R/helper.R"]
    forbidden = ["R/helper.R"]
    violation = check_blast_radius(changed, ["app.R", "outputs/*"], forbidden)
    assert violation and is_blast_violation(violation)
    assert check_blast_radius(changed, ["app.R", "outputs/*"], forbidden, ["outputs/*"]) is None


def test_is_blast_violation_only_matches_scope_errors():
    assert not is_blast_violation("Traceback (most recent call last)")
    assert not is_blast_violation(None)


def test_scope_pattern_warning_for_nested_copies():
    warnings = scope_pattern_warnings("t", ["data/*"], [])
    assert warnings and "grader_ignore" in warnings[0]
    assert scope_pattern_warnings("t", ["data/*"], ["outputs/*"]) == []
    assert scope_pattern_warnings("t", [], []) == []


# ------------------------------------------------------------------ grader_ignore


def test_grader_sees_workspace_without_ignored_paths(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "outputs").mkdir(parents=True)
    (workspace / "outputs" / "audit.txt").write_text("renderUI")
    (workspace / "app.txt").write_text("fine")
    # Fails if the grader can still see outputs/, and records the changed-files list.
    command = (
        'test ! -e outputs && cp "$SKILLDIFF_CHANGED_FILES_FILE" ../seen.txt '
        "&& echo '{\"score\": 1.0}'"
    )
    grader = Grader(
        GraderConfig(type="command", command=command), "s", "m", grader_ignore=["outputs/*"]
    )
    result = grader.grade_candidate(
        workspace, "r", "diff", "t", ["app.txt", "outputs/audit.txt"]
    )
    assert result.grade_status == "graded" and result.score == 1.0, result.feedback
    assert (workspace / "outputs" / "audit.txt").exists(), "the real workspace is untouched"


def test_grader_without_ignore_still_sees_everything(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "outputs").mkdir(parents=True)
    command = "test ! -e outputs && echo '{\"score\": 1.0}' || echo '{\"score\": 0.0}'"
    grader = Grader(GraderConfig(type="command", command=command), "s", "m")
    assert grader.grade_candidate(workspace, "", "", "", []).score == 0.0


def test_blast_violation_is_classified_and_ignored_paths_pass(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    grader = Grader(
        GraderConfig(type="command", command="echo '{\"score\": 1.0}'"),
        "s",
        "m",
        allowed_paths=["app.R"],
    )
    blocked = grader.grade_candidate(workspace, "", "", "", ["app.R", "notes.md"])
    assert blocked.grade_status == "error" and is_blast_violation(blocked.feedback)
    allowed = Grader(
        GraderConfig(type="command", command="echo '{\"score\": 1.0}'"),
        "s",
        "m",
        allowed_paths=["app.R"],
        grader_ignore=["notes.md"],
    ).grade_candidate(workspace, "", "", "", ["app.R", "notes.md"])
    assert allowed.grade_status == "graded" and allowed.score == 1.0


# --------------------------------------------------- multiple known-good solutions


def test_every_known_good_solution_must_score_full(tmp_path):
    class Outcomes(Grader):
        def grade_workspace(self, workspace_dir):
            return {
                "untouched": GradeResult(0, False, "candidate-A"),
                "good_a": GradeResult(1, True, "candidate-A"),
                "good_b": GradeResult(0.5, False, "candidate-A"),  # rejects a valid variant
                "broken": GradeResult(0, False, "candidate-A"),
            }[workspace_dir.name]

    grader = Outcomes(None, "s", "m")
    ok_single = validate_grader_against_directories(
        grader, tmp_path / "untouched", tmp_path / "good_a", [tmp_path / "broken"]
    )
    assert ok_single["verdict"] == "ok"
    strict = validate_grader_against_directories(
        grader,
        tmp_path / "untouched",
        [tmp_path / "good_a", tmp_path / "good_b"],
        [tmp_path / "broken"],
    )
    assert strict["verdict"] == "grader-too-strict"
    assert any("known-good solution 1" in c for c in strict["checks"])


# --------------------------------------------------------------------- config


def _write_task(tmp_path: Path, **extra) -> Path:
    path = tmp_path / "t.yaml"
    path.write_text(
        yaml.safe_dump(
            {"id": "t", "prompt": "do it", "grader": {"type": "command", "command": "true"}}
            | extra
        )
    )
    return path


def test_task_accepts_list_of_good_solutions_and_grader_ignore(tmp_path):
    task = load_task(
        _write_task(
            tmp_path,
            validation={"good": ["a", "b"], "broken": ["c"]},
            grader_ignore=["outputs/*"],
        )
    )
    assert task.validation["good"] == ["a", "b"]
    assert task.grader_ignore == ["outputs/*"]
    assert load_task(_write_task(tmp_path, validation={"good": "a"})).validation["good"] == "a"


def test_task_rejects_non_list_grader_ignore(tmp_path):
    with pytest.raises(ValueError, match="grader_ignore"):
        load_task(_write_task(tmp_path, grader_ignore="outputs/*"))


# ----------------------------------------------------------------- grader notes


def test_notes_are_accepted_but_not_counted_as_checks():
    payload = {
        "score": 1.0,
        "checks": [{"name": "a", "passed": True}],
        "notes": {"calls": 3, "err": "none"},
    }
    assert validate_grader_payload(payload) is None
    assert validate_grader_payload({"score": 1, "notes": "plain"}) is None
    assert validate_grader_payload({"score": 1, "notes": ["a", "b"]}) is None
    assert "notes" in validate_grader_payload({"score": 1, "notes": 5})
    assert "notes" in validate_grader_payload({"score": 1, "notes": [1, 2]})
    run = {"feedback": json.dumps(payload)}
    assert [name for name, _ in extract_checks(run)] == ["a"]
    assert extract_notes(run) == ["calls: 3", "err: none"]
    assert extract_notes({"feedback": json.dumps({"score": 1, "notes": "plain"})}) == ["plain"]
    assert extract_notes({"feedback": "not json"}) == []


# ------------------------------------------------- reporting and diagnosis of blast


def blast_run(**changes):
    return {
        "model": "m",
        "task_id": "t",
        "repetition": 1,
        "arm": "treatment",
        "status": "ok",
        "grade_status": "error",
        "score": None,
        "feedback": "Blast radius violation: modified forbidden path 'x' (matches 'x')",
        "skill_invoked": True,
    } | changes


def test_score_cell_labels_blast_radius_not_grader_error():
    assert _score_cell(blast_run()) == "N/A (blast radius)"
    assert _score_cell(blast_run(feedback="Traceback ...")) == "N/A (grader error)"
    assert _score_cell(blast_run(feedback="x", grade_error_kind="blast_radius")) == (
        "N/A (blast radius)"
    )


def test_diagnose_separates_blast_radius_from_grader_failures(tmp_path):
    data = {
        "name": "d",
        "models": ["m"],
        "tasks": ["t"],
        "harness": "claude",
        "runs": {
            "control": [blast_run(arm="control", grade_status="graded", score=1.0, feedback="")],
            "treatment": [blast_run()],
        },
    }
    path = tmp_path / "results.json"
    path.write_text(json.dumps(data))
    report = diagnose_run(path)
    assert len(report["blast_violations"]) == 1
    assert report["grader_failures"] == []
    advice = " ".join(report["recommendations"])
    assert "allowed_paths" in advice and "grader_ignore" in advice
    assert "skill body" not in advice


# ------------------------------------------------------------------- auth probe


def _fake_claude(tmp_path: Path, body: str) -> Path:
    fake = tmp_path / "claude"
    fake.write_text(f"#!/bin/sh\n{body}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def test_looks_like_auth_error_and_hint():
    assert looks_like_auth_error("Failed to authenticate: OAuth session expired")
    assert looks_like_auth_error("Not logged in")
    assert not looks_like_auth_error("rate limit exceeded")
    assert "claude auth login" in auth_login_hint("claude")


def test_probe_claude_auth_success_and_failure(tmp_path, monkeypatch):
    from skilldiff.config import ClaudeConfig

    ok = _fake_claude(
        tmp_path,
        'echo \'{"type":"result","subtype":"success","is_error":false,"result":"OK",'
        '"total_cost_usd":0.001,"num_turns":1,"usage":{}}\'',
    )
    runner = AgentRunner()
    ok_cfg = ClaudeConfig(bin_path=str(ok))
    assert runner.probe_claude_auth("sonnet", ok_cfg, timeout=20) == (
        True,
        "authenticated call succeeded",
    )
    bad = _fake_claude(
        tmp_path,
        'echo \'{"type":"result","subtype":"error","is_error":true,'
        '"result":"Failed to authenticate: OAuth session expired"}\'; exit 1',
    )
    success, detail = runner.probe_claude_auth("sonnet", ClaudeConfig(bin_path=str(bad)), 20)
    assert success is False and "OAuth session expired" in detail


def test_check_fails_on_expired_login_and_skips_with_no_probe(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKILLDIFF_MOCK_RUNNER", raising=False)
    cmd_init(Args(force=False))
    bad = _fake_claude(
        tmp_path,
        'case "$1" in --version) echo 9.9.9; exit 0;; esac\n'
        'echo \'{"type":"result","subtype":"error","is_error":true,'
        '"result":"Failed to authenticate: OAuth session expired"}\'; exit 1',
    )
    monkeypatch.setenv("CLAUDE_BIN", str(bad))
    code = cmd_check(Args(config="skilldiff.yaml", no_grade=True))
    out = capsys.readouterr().out
    assert code == 1, out
    assert "FAIL  claude auth probe failed" in out and "claude auth login" in out

    code = cmd_check(Args(config="skilldiff.yaml", no_grade=True, no_probe=True))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "auth probe" not in out


def test_run_reports_runtime_errors_without_a_traceback(tmp_path, monkeypatch, capsys):
    from skilldiff.experiment import ExperimentRunner

    monkeypatch.chdir(tmp_path)
    cmd_init(Args(force=False))

    def boom(self, resume=False):
        raise RuntimeError("Harness 'claude' could not run any session: Failed to authenticate")

    monkeypatch.setattr(ExperimentRunner, "run", boom)
    assert cmd_run(Args(config="skilldiff.yaml")) == 1
    err = capsys.readouterr().err
    assert "Experiment error: Harness 'claude' could not run" in err


# ----------------------------------------------------------- check: validation


def _scaffold_validation(tmp_path: Path, good: list[str], forbidden: list[str] | None = None):
    for name, content in {"fixture": "", "g1": "solved", "g2": "solved", "bad": "nope"}.items():
        directory = tmp_path / name
        directory.mkdir()
        (directory / "result.txt").write_text(content)
    (tmp_path / "skill").mkdir()
    (tmp_path / "skill" / "SKILL.md").write_text("---\nname: skill\ndescription: d\n---\nbody\n")
    (tmp_path / "tasks").mkdir()
    task = {
        "id": "t",
        "prompt": "p",
        "repo": "../fixture",
        "grader": {
            "type": "command",
            "command": "grep -q solved result.txt && echo '{\"score\":1}' || echo '{\"score\":0}'",
        },
        "validation": {"good": good if len(good) != 1 else good[0], "broken": ["../bad"]},
    }
    task["validation"]["good"] = (
        [f"../{g}" for g in good] if len(good) != 1 else f"../{good[0]}"
    )
    if forbidden:
        task["forbidden_paths"] = forbidden
    (tmp_path / "tasks" / "t.yaml").write_text(yaml.safe_dump(task))
    (tmp_path / "skilldiff.yaml").write_text(
        yaml.safe_dump(
            {"name": "e", "skill": "./skill", "models": ["sonnet"], "tasks": ["./tasks/*.yaml"]}
        )
    )


def test_check_warns_when_only_one_known_good_solution(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_BIN", "/bin/echo")
    _scaffold_validation(tmp_path, ["g1"])
    cmd_check(Args(config="skilldiff.yaml", no_grade=False, no_probe=True))
    out = capsys.readouterr().out
    assert "grader validation ok" in out
    assert "only one validation.good solution" in out


def test_check_accepts_several_known_good_solutions_without_warning(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_BIN", "/bin/echo")
    _scaffold_validation(tmp_path, ["g1", "g2"])
    cmd_check(Args(config="skilldiff.yaml", no_grade=False, no_probe=True))
    out = capsys.readouterr().out
    assert "grader validation ok" in out
    assert "only one validation.good solution" not in out


def test_check_warns_about_forbidden_patterns_matching_nested_copies(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_BIN", "/bin/echo")
    _scaffold_validation(tmp_path, ["g1", "g2"], forbidden=["data/*"])
    cmd_check(Args(config="skilldiff.yaml", no_grade=False, no_probe=True))
    out = capsys.readouterr().out
    assert "forbidden_paths pattern 'data/*' also matches nested copies" in out


# --------------------------------------------------------------------- regrade


def _mock_run(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setenv("SKILLDIFF_MOCK_SCRIPT", "echo hello > made.txt")
    cmd_init(Args(force=False))
    task = tmp_path / "tasks" / "changelog-entry.yaml"
    data = yaml.safe_load(task.read_text())
    data["grader"] = {"type": "command", "command": "test -f made.txt"}
    data.pop("validation", None)
    task.write_text(yaml.safe_dump(data))
    (tmp_path / "skilldiff.yaml").write_text(
        "name: regrade-exp\nskill: ./skills/changelog-style\nmodels:\n  - sonnet\n"
        "tasks:\n  - ./tasks/*.yaml\nruns: 1\n"
    )
    assert cmd_run(Args(config="skilldiff.yaml", runs=1)) == 0
    capsys.readouterr()
    return task


def _results(tmp_path: Path) -> dict:
    run_dir = sorted((tmp_path / "runs").iterdir())[-1]
    return json.loads((run_dir / "results.json").read_text())


def test_regrade_rescored_runs_from_saved_diffs(tmp_path, monkeypatch, capsys):
    task = _mock_run(tmp_path, monkeypatch, capsys)
    assert _results(tmp_path)["overall"]["skill"]["task_score"] == 1.0

    # A corrected grader that now rejects the saved work.
    data = yaml.safe_load(task.read_text())
    data["grader"]["command"] = "grep -q goodbye made.txt"
    task.write_text(yaml.safe_dump(data))

    assert cmd_regrade(Args(run_dir=None, config="skilldiff.yaml", task=None, dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "would change 2" in out and "100% -> 0%" in out
    assert _results(tmp_path)["overall"]["skill"]["task_score"] == 1.0, "dry run writes nothing"

    assert cmd_regrade(Args(run_dir=None, config="skilldiff.yaml", task=None, dry_run=False)) == 0
    out = capsys.readouterr().out
    assert "changed 2" in out

    results = _results(tmp_path)
    assert results["overall"]["skill"]["task_score"] == 0.0
    assert results["overall"]["control"]["task_score"] == 0.0
    assert results["regrades"][0]["runs_changed"] == 2
    assert any("Regraded 2 run(s)" in w for w in results["warnings"])
    run_dir = sorted((tmp_path / "runs").iterdir())[-1]
    record = json.loads((run_dir / "sonnet/changelog-entry/treatment/001/run.json").read_text())
    assert record["score"] == 0.0
    assert record["regrade_history"][0]["score"] == 1.0
    assert "Regraded 2 run(s)" in (run_dir / "report.md").read_text()
    # The run's own metadata is preserved, including the original pre-registered settings.
    assert results["name"] == "regrade-exp" and results["runs_per_arm"] == 1


def test_regrade_keeps_run_when_diff_cannot_be_rebuilt(tmp_path, monkeypatch, capsys):
    _mock_run(tmp_path, monkeypatch, capsys)
    run_dir = sorted((tmp_path / "runs").iterdir())[-1]
    for arm in ("control", "treatment"):
        patch = run_dir / "sonnet/changelog-entry" / arm / "001" / "diff.patch"
        patch.write_text("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
    assert cmd_regrade(Args(run_dir=None, config="skilldiff.yaml", task=None, dry_run=True)) == 0
    captured = capsys.readouterr()
    assert "skipped" in captured.err and "could not apply the saved diff" in captured.err
    assert _results(tmp_path)["overall"]["skill"]["task_score"] == 1.0


def test_regrade_requires_a_run_and_a_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cmd_regrade(Args(run_dir=None, config=None, task=None, dry_run=False)) == 1
    assert "No experiment run" in capsys.readouterr().err


def test_validate_grader_with_reference_solutions(tmp_path):
    untouched = tmp_path / "untouched"
    untouched.mkdir()
    (untouched / "app.py").write_text("old = 1")

    good = tmp_path / "good"
    good.mkdir()
    (good / "app.py").write_text("new = 2")

    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "app.py").write_text("ref = 3")

    class MockGrader:
        allowed_paths = []
        forbidden_paths = []
        grader_ignore = []

        def grade_workspace(self, ws):
            content = (ws / "app.py").read_text()
            if "old" in content:
                return GradeResult(score=0.0, success=False, label="ctrl")
            return GradeResult(score=1.0, success=True, label="pass")

    report = validate_grader_against_directories(
        MockGrader(), untouched, good_dir=good, reference_dirs=[ref]
    )
    assert report["verdict"] == "ok"
    assert "reference solution scores 100%" in report["checks"]


def test_validate_grader_flags_deprecated_api(tmp_path):
    untouched = tmp_path / "untouched"
    untouched.mkdir()
    (untouched / "app.py").write_text("old = 1")

    good = tmp_path / "good"
    good.mkdir()
    (good / "app.py").write_text("@render.download\ndef foo(): pass")

    class MockGrader:
        allowed_paths = []
        forbidden_paths = []
        grader_ignore = []

        def grade_workspace(self, ws):
            return GradeResult(score=1.0, success=True, label="pass")

    report = validate_grader_against_directories(
        MockGrader(), untouched, good_dir=good, deprecated_patterns=["render.download"]
    )
    assert report["verdict"] == "deprecated-api-used"
    assert any("uses deprecated API 'render.download'" in c for c in report["checks"])

