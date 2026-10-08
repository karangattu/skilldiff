"""Recovery must not lose paid work or combine different experiments."""

import copy
import json
import os
import shutil
from pathlib import Path

import pytest

from skilldiff.config import ExperimentConfig, GraderConfig, TaskConfig
from skilldiff.experiment import ExperimentRunner
from skilldiff.runner import AgentRunner


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setattr("skilldiff.experiment._cli_version", lambda _: "test-cli 1")
    skill = tmp_path / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: test\n---\noriginal")
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "data.txt").write_text("original")
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    source = tasks / "task.yaml"
    source.write_text("id: t1\nprompt: Test\n")
    graders = tmp_path / "graders"
    graders.mkdir()
    (graders / "grade.py").write_text("print('{\"score\": 1}')")
    cfg = ExperimentConfig(
        name="recovery",
        skill=skill,
        models=["test"],
        tasks_patterns=[],
        runs=1,
        config_path=tmp_path / "skilldiff.yaml",
    )
    task = TaskConfig(
        id="t1",
        prompt="Test",
        repo="../fixture",
        source_path=source,
        grader=GraderConfig(command='python3 "$SKILLDIFF_TASK_DIR/../graders/grade.py"'),
    )
    return cfg, [task]


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_resume_reuses_pairs_and_allows_more_repetitions(experiment):
    cfg, tasks = experiment
    first = ExperimentRunner(cfg, tasks).run()
    root = Path(first["run_dir"])
    old_records = {p: p.read_bytes() for p in root.rglob("run.json")}
    same = ExperimentRunner(cfg, tasks).run(resume=root)
    assert same["runs"]["control"] == first["runs"]["control"]
    cfg.runs = 2
    extended = ExperimentRunner(cfg, tasks).run(resume=root)
    assert len(extended["runs"]["control"]) == 2
    assert extended["seed"] == first["seed"]
    assert all(p.read_bytes() == data for p, data in old_records.items())


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout_seconds", 1),
        ("parallel", 2),
        ("seed", -1),
        ("runs", 0),
        ("failure_policy", {"agent_failure": "zero"}),
        ("thresholds", {"acceptable_score_regression_pp": 8}),
        ("cost_basis", "api-equivalent"),
        ("pricing", {"rates": {"test": {"input": 2}}}),
        ("claude.max_turns", 1),
        ("claude.extra_args", ["--some-flag"]),
    ],
)
def test_settings_refusal_preserves_every_file(experiment, field, value):
    cfg, tasks = experiment
    root = Path(ExperimentRunner(cfg, tasks).run()["run_dir"])
    before = files(root)
    owner = cfg
    if "." in field:
        parent, field = field.split(".")
        owner = getattr(cfg, parent)
    setattr(owner, field, value)
    with pytest.raises(ValueError, match=field):
        ExperimentRunner(cfg, tasks).run(resume=root)
    assert files(root) == before


@pytest.mark.parametrize("parallel,baseline", [(1, False), (2, False), (2, True)])
def test_later_pairs_use_frozen_inputs(experiment, parallel, baseline):
    cfg, tasks = experiment
    cfg.runs, cfg.parallel = 3, parallel
    if baseline:
        cfg.skill_a = cfg.skill
        cfg.skill_b = cfg.skill.parent / "second"
        cfg.skill_b.mkdir()
        (cfg.skill_b / "SKILL.md").write_text("second original")
        cfg.skill = None
        cfg.include_baseline = True
    source = cfg.skill or cfg.skill_a
    fixture = source.parent / "fixture" / "data.txt"
    original = (source / "SKILL.md").read_text()

    class InspectingAgent(AgentRunner):
        def run(self, prompt, cwd, model, config):
            assert (cwd / "data.txt").read_text() == "original"
            installed = cwd / ".claude/skills/my-skill/SKILL.md"
            if installed.exists():
                assert installed.read_text() == original
            (source / "SKILL.md").write_text("changed")
            fixture.write_text("changed")
            cfg.timeout_seconds = 1
            tasks[0].prompt = "changed"
            assert prompt == "Test"
            assert config.timeout_seconds == 1800
            return super().run(prompt, cwd, model, config)

    result = ExperimentRunner(cfg, tasks, agent_runner=InspectingAgent()).run()
    assert len(result["runs"]["control"]) == 3
    if baseline:
        assert len(result["runs"]["baseline"]) == 3


@pytest.mark.parametrize("damage", ["record", "missing", "artifact", "baseline"])
def test_corrupt_completed_pair_stops_without_writes(experiment, damage):
    cfg, tasks = experiment
    if damage == "baseline":
        cfg.skill_a, cfg.skill_b, cfg.skill = cfg.skill, cfg.skill, None
        cfg.include_baseline = True
    root = Path(ExperimentRunner(cfg, tasks).run()["run_dir"])
    arm = "baseline" if damage == "baseline" else "control"
    record = root / "test/t1" / arm / "001/run.json"
    if damage == "record":
        record.write_text("{")
    elif damage == "artifact":
        record.with_name("transcript.txt").unlink()
    else:
        record.unlink()
    before = files(root)
    with pytest.raises(ValueError, match="(?i)(record|artifact|missing|read|pair)"):
        ExperimentRunner(cfg, tasks).run(resume=root)
    assert files(root) == before


def test_snapshot_tampering_refuses_resume(experiment):
    cfg, tasks = experiment
    root = Path(ExperimentRunner(cfg, tasks).run()["run_dir"])
    snapshots = list((root / "inputs").rglob("SKILL.md"))
    assert snapshots, "The run must retain its skill snapshot"
    snapshots[0].write_text("tampered")
    before = files(root)
    with pytest.raises(ValueError, match="(?i)snapshot"):
        ExperimentRunner(cfg, tasks).run(resume=root)
    assert files(root) == before


def test_grader_change_does_not_complete_pair(experiment):
    cfg, tasks = experiment
    grader = cfg.skill.parent / "graders/grade.py"

    class EditingAgent(AgentRunner):
        def run(self, *args, **kwargs):
            grader.write_text("print('{\"score\": 0}')")
            return super().run(*args, **kwargs)

    with pytest.raises(ValueError, match="(?i)grader"):
        ExperimentRunner(cfg, tasks, agent_runner=EditingAgent()).run()
    checkpoint = next((cfg.skill.parent / "runs").glob("*/checkpoint.json"))
    assert json.loads(checkpoint.read_text())["completed"] == []


def test_unchanged_task_ids_support_both_metadata_shapes(experiment):
    cfg, tasks = experiment
    runner = ExperimentRunner(cfg, tasks)
    runner._provenance_snapshot = {"skill_hash": "s", "tasks_hash": "t"}
    metadata = runner._metadata("test")
    assert runner._resume_mismatches(metadata) == []
    metadata = copy.deepcopy(metadata)
    metadata["tasks"] = ["t1"]
    assert runner._resume_mismatches(metadata) == []


def test_atomic_checkpoint_failure_preserves_previous_file(experiment, monkeypatch):
    cfg, tasks = experiment
    runner = ExperimentRunner(cfg, tasks)
    root = cfg.skill.parent
    checkpoint = root / "checkpoint.json"
    checkpoint.write_text('{"completed": []}')
    before = checkpoint.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("disk failure")

    monkeypatch.setattr("os.replace", fail)
    with pytest.raises(OSError, match="disk failure"):
        runner._write_checkpoint(root)
    assert checkpoint.read_bytes() == before


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_arm_write_failure_cannot_publish_completion(experiment, monkeypatch, failure):
    cfg, tasks = experiment
    original = getattr(os, failure)

    def fail(*args, **kwargs):
        if failure == "fsync" or str(args[1]).endswith("diff.patch"):
            raise OSError("injected write failure")
        return original(*args, **kwargs)

    runner = ExperimentRunner(cfg, tasks)
    directory = cfg.skill.parent / "arm"
    directory.mkdir()
    with monkeypatch.context() as patch:
        patch.setattr(os, failure, fail)
        with pytest.raises(OSError, match="injected"):
            runner._save_run_artifacts(directory, {"complete": True}, "transcript", "diff")
    assert not (directory / "run.json").exists()


def test_source_change_during_copy_aborts_before_agents(experiment, monkeypatch):
    cfg, tasks = experiment
    original = shutil.copy2

    def copy_and_edit(src, dst, *args, **kwargs):
        result = original(src, dst, *args, **kwargs)
        if Path(src).name == "data.txt":
            Path(src).write_text("changed while copying")
        return result

    monkeypatch.setattr(shutil, "copy2", copy_and_edit)
    with pytest.raises(ValueError, match="changed"):
        ExperimentRunner(cfg, tasks).run()
    assert not list((cfg.skill.parent / "runs").rglob("run.json"))


def test_unreadable_input_aborts_before_agents(experiment, monkeypatch):
    cfg, tasks = experiment
    original = Path.open

    def deny_data(path, *args, **kwargs):
        if path.name == "data.txt":
            raise PermissionError("unreadable data")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_data)
    with pytest.raises(PermissionError, match="unreadable"):
        ExperimentRunner(cfg, tasks).run()
    assert not list((cfg.skill.parent / "runs").rglob("run.json"))


def test_completed_pair_survives_later_checkpoint_failure(experiment, monkeypatch):
    cfg, tasks = experiment
    cfg.runs = 2
    original = os.replace

    def fail_second_pair(src, dst):
        if Path(dst).name == "checkpoint.json":
            data = json.loads(Path(src).read_text())
            if len(data["completed"]) == 2:
                raise OSError("checkpoint disk failure")
        original(src, dst)

    monkeypatch.setattr(os, "replace", fail_second_pair)
    with pytest.raises(OSError, match="checkpoint disk failure"):
        ExperimentRunner(cfg, tasks).run()
    root = next(p for p in (cfg.skill.parent / "runs").iterdir() if p.is_dir())
    assert json.loads((root / "checkpoint.json").read_text())["completed"] == [["test", "t1", 1]]
    assert json.loads((root / "test/t1/control/001/run.json").read_text())["complete"]
    before = files(root)
    with pytest.raises(ValueError, match="Incomplete saved pair"):
        ExperimentRunner(cfg, tasks).run(resume=root)
    assert files(root) == before


def test_grader_that_changes_itself_does_not_complete_pair(experiment):
    cfg, tasks = experiment
    grader = cfg.skill.parent / "graders/grade.py"
    grader.write_text("from pathlib import Path\nPath(__file__).write_text('print(1)')\nprint(1)\n")
    with pytest.raises(ValueError, match="grader"):
        ExperimentRunner(cfg, tasks).run()
    checkpoint = next((cfg.skill.parent / "runs").glob("*/checkpoint.json"))
    assert json.loads(checkpoint.read_text())["completed"] == []


def test_external_and_absolute_internal_links(experiment):
    cfg, tasks = experiment
    fixture = cfg.skill.parent / "fixture"
    (fixture / "alias.txt").symlink_to(fixture / "data.txt")
    result = ExperimentRunner(cfg, tasks).run()
    root = Path(result["run_dir"])
    link = next((root / "inputs").rglob("alias.txt"))
    assert link.is_symlink()
    assert link.resolve().is_relative_to(root / "inputs")
    assert link.read_text() == "original"
    (fixture / "escape").symlink_to(cfg.skill.parent / "graders")
    with pytest.raises(ValueError, match="symlink"):
        ExperimentRunner(cfg, tasks).run()


def test_legacy_run_is_readable_but_not_resumable(experiment):
    from skilldiff.compare import load_results

    cfg, tasks = experiment
    result = ExperimentRunner(cfg, tasks).run()
    root = Path(result["run_dir"])
    metadata = json.loads((root / "experiment.json").read_text())
    metadata.pop("snapshot_version")
    (root / "experiment.json").write_text(json.dumps(metadata))
    assert load_results(root)[0]["name"] == "recovery"
    before = files(root)
    with pytest.raises(ValueError, match="frozen-input"):
        ExperimentRunner(cfg, tasks).run(resume=root)
    assert files(root) == before


def test_same_timestamp_does_not_share_run_directory(experiment, monkeypatch):
    from datetime import datetime

    cfg, tasks = experiment

    class FixedTime:
        @staticmethod
        def now(tz):
            return datetime(2026, 9, 26, tzinfo=tz)

    monkeypatch.setattr("skilldiff.experiment.datetime", FixedTime)
    first = ExperimentRunner(cfg, tasks).run()
    before = files(Path(first["run_dir"]))
    second = ExperimentRunner(cfg, tasks).run()
    assert first["run_dir"] != second["run_dir"]
    assert files(Path(first["run_dir"])) == before


def test_concurrent_resume_cannot_enter_locked_run(experiment):
    from skilldiff.persistence import run_lock

    cfg, tasks = experiment
    root = Path(ExperimentRunner(cfg, tasks).run()["run_dir"])
    before = files(root)
    with run_lock(root):
        with pytest.raises(ValueError, match="locked"):
            ExperimentRunner(cfg, tasks).run(resume=root)
    assert files(root) == before
    assert ExperimentRunner(cfg, tasks).run(resume=root)["name"] == "recovery"


def test_changed_cli_version_refuses_without_writes(experiment, monkeypatch):
    cfg, tasks = experiment
    root = Path(ExperimentRunner(cfg, tasks).run()["run_dir"])
    before = files(root)
    monkeypatch.setattr("skilldiff.experiment._cli_version", lambda _: "test-cli 2")
    with pytest.raises(ValueError, match="agent_cli"):
        ExperimentRunner(cfg, tasks).run(resume=root)
    assert files(root) == before


def test_report_command_obeys_run_lock(experiment):
    from argparse import Namespace

    from skilldiff.cli import cmd_report
    from skilldiff.persistence import run_lock

    cfg, tasks = experiment
    root = Path(ExperimentRunner(cfg, tasks).run()["run_dir"])
    before = files(root)
    with run_lock(root):
        assert cmd_report(Namespace(run_dir=str(root))) == 1
    assert files(root) == before


def test_input_path_names_cannot_collide(experiment):
    cfg, tasks = experiment
    cfg.models = ["a/b", "a:b"]
    with pytest.raises(ValueError, match="(?i)collision"):
        ExperimentRunner(cfg, tasks).run()


def test_resume_requires_consistent_checkpoint_seed(experiment):
    cfg, tasks = experiment
    root = Path(ExperimentRunner(cfg, tasks).run()["run_dir"])
    path = root / "checkpoint.json"
    checkpoint = json.loads(path.read_text())
    checkpoint["experiment"]["seed"] += 1
    path.write_text(json.dumps(checkpoint))
    before = files(root)
    with pytest.raises(ValueError, match="seed"):
        ExperimentRunner(cfg, tasks).run(resume=root)
    assert files(root) == before


def test_starting_later_does_not_hide_checkpointed_record_changes(experiment):
    cfg, tasks = experiment
    root = Path(ExperimentRunner(cfg, tasks).run()["run_dir"])
    record = root / "test/t1/control/001/run.json"
    changed = json.loads(record.read_text())
    changed["repetition"] = 9
    record.write_text(json.dumps(changed))
    before = files(root)
    with pytest.raises(ValueError, match="record"):
        ExperimentRunner(cfg, tasks).run(resume=root)
    assert files(root) == before


def test_grader_import_cache_does_not_change_tracked_inputs(experiment):
    cfg, tasks = experiment
    graders = cfg.skill.parent / "graders"
    (graders / "helper.py").write_text("score = 1\n")
    (graders / "grade.py").write_text("from helper import score\nprint(score)\n")
    result = ExperimentRunner(cfg, tasks).run()
    root = Path(result["run_dir"])
    assert result["runs"]["control"][0]["score"] == 1
    assert ExperimentRunner(cfg, tasks).run(resume=root)["runs"] == result["runs"]


def test_frozen_task_definition_is_part_of_compatibility(experiment):
    cfg, tasks = experiment
    root = Path(ExperimentRunner(cfg, tasks).run()["run_dir"])
    tasks[0].category = "intended"
    before = files(root)
    with pytest.raises(ValueError, match="hashes"):
        ExperimentRunner(cfg, tasks).run(resume=root)
    assert files(root) == before


def test_corrupted_score_record_is_rejected_before_writes(experiment):
    cfg, tasks = experiment
    root = Path(ExperimentRunner(cfg, tasks).run()["run_dir"])
    record = root / "test/t1/control/001/run.json"
    data = json.loads(record.read_text())
    data["score"] = "corrupt score"
    record.write_text(json.dumps(data))
    before = files(root)
    with pytest.raises(ValueError, match="record"):
        ExperimentRunner(cfg, tasks).run(resume=root)
    assert files(root) == before


def test_fixture_root_symlink_is_resolved_once(experiment):
    cfg, tasks = experiment
    base = cfg.skill.parent
    link = base / "fixture-link"
    link.symlink_to(base / "fixture")
    replacement = base / "replacement"
    replacement.mkdir()
    (replacement / "data.txt").write_text("new fixture")
    tasks[0].repo = "../fixture-link"
    cfg.runs = 2

    class RetargetingAgent(AgentRunner):
        def run(self, prompt, cwd, model, config):
            assert (cwd / "data.txt").read_text() == "original"
            link.unlink()
            link.symlink_to(replacement)
            return super().run(prompt, cwd, model, config)

    result = ExperimentRunner(cfg, tasks, agent_runner=RetargetingAgent()).run()
    assert len(result["runs"]["control"]) == 2


def test_recorded_python_version_is_checked(experiment):
    cfg, tasks = experiment
    runner = ExperimentRunner(cfg, tasks)
    runner._provenance_snapshot = {"skill_hash": "s", "tasks_hash": "t"}
    metadata = runner._metadata("test")
    metadata["system"]["python"] = "0.0.0"
    assert "python version changed" in runner._resume_mismatches(metadata)


def test_interruption_keeps_arm_resources_diff_and_explicit_partial_records(experiment):
    from skilldiff.runner import RunResult

    cfg, tasks = experiment

    class InterruptedRunner(AgentRunner):
        count = 0

        def run(self, prompt, cwd, model, config):
            self.count += 1
            (cwd / 'data.txt').write_text('repaired' if self.count == 1 else 'partial')
            if self.count == 2:
                raise KeyboardInterrupt
            return RunResult(str(prompt), 'fixed', 'agent evidence', 12, .25,
                             100, 20, 2, 0)

    result = ExperimentRunner(cfg, tasks, agent_runner=InterruptedRunner()).run()
    root = Path(result['run_dir'])
    records = [json.loads(path.read_text()) for path in root.rglob('run.json')]
    assert len(records) == 2
    finished = next(record for record in records if record['status'] == 'ok')
    assert finished['input_tokens'] == 100 and finished['cost'] == .25
    assert finished['response'] == 'fixed' and finished['complete'] is False
    assert any('repaired' in path.read_text() for path in root.rglob('diff.patch'))
    assert any(record['status'] == 'interrupted' for record in records)
    assert result['runs']['control'] == result['runs']['treatment'] == []
    assert len(result['incomplete_runs']) == 2
