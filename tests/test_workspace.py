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


