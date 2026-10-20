from pathlib import Path

from skilldiff.workspace import Workspace


def test_workspace_control_setup(tmp_path: Path):
    ws_dir = tmp_path / "ws_control"
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill Content")

    ws = Workspace(root=ws_dir, is_treatment=False, skill_dir=skill_dir)
    ws.setup()

    assert (ws_dir / ".git").exists()
    assert not (ws_dir / ".claude").exists()


def test_workspace_treatment_setup(tmp_path: Path):
    ws_dir = tmp_path / "ws_treat"
    skill_dir = tmp_path / "code-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill Content")

    ws = Workspace(root=ws_dir, is_treatment=True, skill_dir=skill_dir)
    ws.setup()

    assert (ws_dir / ".git").exists()
    assert (ws_dir / ".claude" / "skills" / "code-review" / "SKILL.md").exists()


def test_workspace_diff_tracking(tmp_path: Path):
    ws_dir = tmp_path / "ws_diff"
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill")

    ws = Workspace(root=ws_dir, is_treatment=False, skill_dir=skill_dir)
    ws.setup()

    (ws_dir / "solution.py").write_text("print('hello')\n")
    diff_text, changed_files = ws.get_diff()

    assert "solution.py" in changed_files
    assert "+print('hello')" in diff_text


def test_workspace_diff_binary(tmp_path: Path):
    ws_dir = tmp_path / "ws_bin"
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill")

    ws = Workspace(root=ws_dir, is_treatment=False, skill_dir=skill_dir)
    ws.setup()

    (ws_dir / "data.bin").write_bytes(b"\x00\x01\x02\x03\xff")
    diff_text, changed_files = ws.get_diff()

    assert "data.bin" in changed_files
    assert "GIT binary patch" in diff_text


def test_reflink_and_cow_copy(tmp_path: Path):
    from skilldiff.snapshots import copy_file_cow, reflink_copy, supports_reflink

    src = tmp_path / "original.txt"
    src.write_text("initial content")
    dst = tmp_path / "cloned.txt"

    copy_file_cow(src, dst)
    assert dst.read_text() == "initial content"

    src.write_text("modified content")
    assert dst.read_text() == "initial content"

    copy_file_cow(src, dst)
    assert dst.read_text() == "modified content"

    assert isinstance(supports_reflink(), bool)
    dst2 = tmp_path / "reflink_dst.txt"
    if reflink_copy(src, dst2):
        assert dst2.read_text() == "modified content"

    dst3 = tmp_path / "cow_opt_in.txt"
    copy_file_cow(src, dst3, reflink=True)
    assert dst3.read_text() == "modified content"


def test_workspace_cleanup(tmp_path: Path):
    ws_dir = tmp_path / "ws_cleanup"
    ws = Workspace(root=ws_dir, is_treatment=False, skill_dir=None)
    ws.setup()
    assert ws_dir.exists()
    assert (ws_dir / ".git").exists()

    ws.cleanup()
    assert not ws_dir.exists()


def test_workspace_clone_from_template_and_fast_diff(tmp_path: Path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "app.py").write_text("x = 1\n")
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Skill")

    tmpl = Workspace(
        root=tmp_path / "template",
        is_treatment=True,
        skill_dir=skill_dir,
        fixture_repo=fixture,
        harness="antigravity",
    )
    tmpl.setup()
    assert tmpl._initial_index is not None

    cloned = Workspace(
        root=tmp_path / "cloned",
        is_treatment=True,
        skill_dir=skill_dir,
        fixture_repo=fixture,
        harness="antigravity",
    )
    cloned.clone_from(tmpl)
    assert cloned.initial_commit == tmpl.initial_commit
    assert cloned.get_diff() == ("", [])
    assert (cloned.root / ".agents" / "skills" / "my-skill" / "SKILL.md").is_file()

    (cloned.root / "app.py").write_text("x = 2\n")
    diff_text, changed = cloned.get_diff()
    assert changed == ["app.py"]
    assert "+x = 2" in diff_text

    cloned.setup()
    assert cloned.get_diff() == ("", [])


def test_workspace_setup_does_not_launch_background_git_maintenance(tmp_path, monkeypatch):
    """A template must stay stable while repetitions copy its Git object files."""
    global_config = tmp_path / 'global-git-config'
    global_config.write_text('[maintenance]\n\tauto = true\n[gc]\n\tauto = 1\n')
    trace = tmp_path / 'git-trace'
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(global_config))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_TRACE', str(trace))
    template = Workspace(tmp_path / 'template', is_treatment=False, skill_dir=None)
    template.setup()
    activity = trace.read_text()
    assert 'maintenance run' not in activity and 'gc --auto' not in activity
    clone = Workspace(tmp_path / 'clone', is_treatment=False, skill_dir=None)
    clone.clone_from(template)
    assert clone.initial_commit == template.initial_commit
    assert clone.get_diff() == ('', [])
