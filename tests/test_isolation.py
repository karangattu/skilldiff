"""Exercise session temp files and advisory host exposure through public flows."""

import json
import os
import sys
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from skilldiff.cli import _format_results, build_parser, cmd_check, cmd_init
from skilldiff.config import ExperimentConfig, TaskConfig
from skilldiff.experiment import ExperimentRunner
from skilldiff.host_exposure import scan_skill_copies
from skilldiff.runner import AgentRunner


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode", "antigravity"])
def test_parallel_sessions_share_temp_only_with_their_own_turns(tmp_path, monkeypatch, harness):
    monkeypatch.delenv("SKILLDIFF_MOCK_RUNNER", raising=False)
    inherited = tmp_path / "host-temp"
    inherited.mkdir()
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(inherited))
    binary = tmp_path / "agent"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib,sys,tempfile\n"
        "if '--help' in sys.argv: sys.exit(0)\n"
        "directory=pathlib.Path(tempfile.gettempdir())\n"
        "marker=directory/'previous-turn'\n"
        "evidence={'temp':str(directory),'previous':marker.exists(),"
        "'env':[os.environ.get(k) for k in ('TMPDIR','TMP','TEMP')]}\n"
        "with pathlib.Path('evidence.jsonl').open('a') as stream:\n"
        " stream.write(json.dumps(evidence)+'\\n')\n"
        "marker.write_text('session')\n"
        "print(json.dumps({'type':'result','result':'ok'}))\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    config = ExperimentConfig(
        name="t", skill=None, models=["m"], tasks_patterns=[], harness=harness
    )
    getattr(config, harness).bin_path = str(binary)
    workspaces = [tmp_path / arm / "workspace" for arm in ("control", "treatment")]
    for workspace in workspaces:
        workspace.mkdir(parents=True)
    runner = AgentRunner()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda workspace: runner.run(["one", "two"], workspace, "m", config),
            workspaces,
        ))
    assert all(result.status == "ok" for result in results)
    temp_paths = []
    for workspace in workspaces:
        lines = (workspace / "evidence.jsonl").read_text().splitlines()
        turns = [json.loads(line) for line in lines]
        assert [turn["previous"] for turn in turns] == [False, True]
        assert turns[0]["temp"] == turns[1]["temp"]
        directory = Path(turns[0]["temp"])
        assert directory.is_relative_to(workspace.parent.resolve())
        assert not directory.is_relative_to(workspace.resolve())
        assert all(turn["env"] == [str(directory)] * 3 for turn in turns)
        assert not directory.exists(), "session temp files must be cleaned up"
        temp_paths.append(directory)
    assert temp_paths[0] != temp_paths[1]
    assert list(inherited.iterdir()) == []
    assert os.environ["TMPDIR"] == str(inherited), "parallel runs must not mutate host env"


def skill_config(tmp_path, isolation="local"):
    skill = tmp_path / "repo" / "skills" / "sample"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: sample\ndescription: Example.\n---\n")
    return ExperimentConfig(
        name="t", skill=skill, models=["m"], tasks_patterns=[], isolation=isolation
    )


def test_local_preflight_warns_about_live_sources_and_saved_snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = skill_config(tmp_path)
    snapshot = tmp_path / "runs" / "old" / "inputs" / "0" / "sample"
    snapshot.mkdir(parents=True)
    (snapshot / "SKILL.md").write_bytes((config.skill / "SKILL.md").read_bytes())
    warnings = ExperimentRunner(config, [], output_dir=tmp_path / "runs").preflight_warnings()
    text = "\n".join(warnings)
    assert "Potential host skill exposure" in text
    assert str(config.skill.resolve()) in text
    assert str(snapshot.resolve()) in text
    assert "not proof of contamination" in text
    assert "filesystem reads" in text


def test_exposure_warning_is_saved_and_rendered_without_invalidating_run(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = skill_config(tmp_path)
    config.runs = 1
    task = TaskConfig(id="one", prompt="Do it")
    results = ExperimentRunner(config, [task], output_dir=tmp_path / "runs").run()
    exposure = next(w for w in results["warnings"] if "Potential host skill exposure" in w)
    assert results["valid"] is True
    root = Path(results["run_dir"])
    assert exposure in json.loads((root / "results.json").read_text())["warnings"]
    assert exposure in _format_results(results)
    for filename in ("report.md", "report.qmd", "report.html"):
        assert "Potential host skill exposure" in (root / filename).read_text()
    assert str(root / "inputs" / "0" / "sample") in exposure


def test_active_snapshot_is_reported_even_when_copy_discovery_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLDIFF_MOCK_RUNNER", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # Simulate a bounded scan stopping before it reaches the active run. The
    # snapshot paths are already known from the manifest and must not depend on discovery.
    monkeypatch.setattr(
        "skilldiff.host_exposure.scan_skill_copies",
        lambda *args, **kwargs: ([], "Incomplete: entry/time limit reached."),
    )
    config = skill_config(tmp_path)
    config.runs = 1
    results = ExperimentRunner(
        config, [TaskConfig(id="one", prompt="Do it")], output_dir=tmp_path / "runs"
    ).run()
    snapshot = Path(results["run_dir"]) / "inputs" / "0" / "sample"
    assert str(snapshot) in "\n".join(results["warnings"])


@pytest.mark.parametrize("isolation", ["docker", "podman"])
def test_unmounted_host_files_do_not_trigger_container_preflight(tmp_path, monkeypatch, isolation):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = skill_config(tmp_path, isolation)
    inheritance = tmp_path / ".claude" / "CLAUDE.md"
    inheritance.parent.mkdir()
    inheritance.write_text("Host instructions")
    warnings = ExperimentRunner(config, [], output_dir=tmp_path / "runs").preflight_warnings()
    assert warnings == []


def test_check_home_scan_finds_renamed_copy_by_content(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_BIN", "/bin/echo")
    experiment = tmp_path / "experiment"
    assert cmd_init(Namespace(dir=str(experiment), force=False)) == 0
    source = experiment / "skills" / "changelog-style" / "SKILL.md"
    copy = tmp_path / "home" / "other-repo" / "renamed"
    copy.mkdir(parents=True)
    (copy / "SKILL.md").write_bytes(source.read_bytes())
    assert cmd_check(Namespace(
        config=str(experiment / "skilldiff.yaml"), no_probe=True, no_grade=True, scan_home=True
    )) == 0
    output = capsys.readouterr().out
    assert str(copy.resolve()) in output
    assert "not proof of contamination" in output
    assert "cannot certify" in output


def test_check_parser_exposes_optional_home_scan():
    args = build_parser().parse_args(["check", "--scan-home", "--no-probe"])
    assert args.scan_home is True


def test_scan_matches_both_ab_revisions(tmp_path):
    config = skill_config(tmp_path)
    original = config.skill
    config.skill = None
    config.skill_a = original
    config.skill_b = tmp_path / "revised"
    config.skill_b.mkdir()
    (config.skill_b / "SKILL.md").write_text("---\nname: alternate\n---\nNew revision")
    copies = tmp_path / "copies"
    by_name = copies / "sample"
    by_name.mkdir(parents=True)
    (by_name / "SKILL.md").write_text("Old revision with the same directory name")
    by_frontmatter = copies / "renamed"
    by_frontmatter.mkdir()
    (by_frontmatter / "SKILL.md").write_text("---\nname: alternate\n---\nOlder revision")
    unrelated = copies / "other"
    unrelated.mkdir()
    (unrelated / "SKILL.md").write_text("Unrelated")
    found, _ = scan_skill_copies(copies, config.skill_dirs)
    assert set(found) == {by_name.resolve(), by_frontmatter.resolve()}


def test_scan_reports_incomplete_bounds_and_skips_symlink_cycles(tmp_path):
    config = skill_config(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / "loop").symlink_to(home, target_is_directory=True)
    excluded = home / "node_modules" / "sample"
    excluded.mkdir(parents=True)
    (excluded / "SKILL.md").write_bytes((config.skill / "SKILL.md").read_bytes())
    large = home / "sample"
    large.mkdir()
    (large / "SKILL.md").write_bytes(b"x" * (1024 * 1024 + 1))
    found, summary = scan_skill_copies(home, config.skill_dirs)
    assert found == []
    assert "3 skipped" in summary
    assert "cannot certify" in summary
    _, bounded = scan_skill_copies(home, config.skill_dirs, max_entries=1)
    assert "Incomplete" in bounded
    _, timed = scan_skill_copies(home, config.skill_dirs, max_seconds=0)
    assert "Incomplete" in timed


def test_scan_reports_unreadable_paths(tmp_path, monkeypatch):
    config = skill_config(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    denied = home / "denied"
    denied.mkdir()
    real_scandir = os.scandir

    def scandir(path):
        if Path(path) == denied:
            raise PermissionError("access denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    found, summary = scan_skill_copies(home, config.skill_dirs)
    assert found == []
    assert "1 unreadable" in summary
    assert str(denied) in summary


def test_session_temp_is_cleaned_up_after_timeout(tmp_path, monkeypatch):
    monkeypatch.delenv("SKILLDIFF_MOCK_RUNNER", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binary = tmp_path / "slow-agent"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os,pathlib,time\n"
        "pathlib.Path('temp.txt').write_text(os.environ['TMPDIR'])\n"
        "pathlib.Path(os.environ['TMPDIR'],'partial').write_text('partial')\n"
        "time.sleep(30)\n",
    )
    binary.chmod(0o755)
    config = ExperimentConfig(name="t", skill=None, models=["m"], tasks_patterns=[])
    config.claude.bin_path = str(binary)
    result = AgentRunner().run("one", workspace, "m", config, timeout=3)
    assert result.status == "timeout"
    assert not Path((workspace / "temp.txt").read_text()).exists()
