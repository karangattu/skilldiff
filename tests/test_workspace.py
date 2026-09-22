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
