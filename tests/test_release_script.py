import subprocess
import sys
from pathlib import Path

from skilldiff.release import extract_release_notes, get_pyproject_version, main


def test_get_pyproject_version():
    content = '[project]\nname = "demo"\nversion = "1.2.3"\n'
    assert get_pyproject_version(content) == "1.2.3"
    assert get_pyproject_version('[project]\nname = "demo"\n') == ""


def test_extract_release_notes():
    changelog = """# Changelog

## [Unreleased]

## [1.2.3] - 2026-10-04

### Added
- Feature A

### Fixed
- Bug B

## [1.2.2] - 2026-10-01
- Old feature
"""
    notes = extract_release_notes(changelog, "v1.2.3")
    assert "### Added" in notes
    assert "- Feature A" in notes
    assert "### Fixed" in notes
    assert "- Bug B" in notes
    assert "1.2.2" not in notes


def test_extract_release_notes_missing():
    changelog = "# Changelog\n\n## [1.0.0] - 2026-01-01\n- Initial release\n"
    assert extract_release_notes(changelog, "v2.0.0") == ""


def test_cli_tag_mismatch(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.5.0"\n', encoding="utf-8")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n", encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release_notes.py",
            "--tag",
            "v0.6.0",
            "--pyproject",
            str(pyproject),
            "--changelog",
            str(changelog),
            "--check-version",
        ],
    )
    assert main() == 1


def test_cli_success(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.5.0"\n', encoding="utf-8")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## [0.5.0] - 2026-10-04\n\n### Added\n- Verified feature\n",
        encoding="utf-8",
    )
    output = tmp_path / "notes.txt"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release_notes.py",
            "--tag",
            "v0.5.0",
            "--pyproject",
            str(pyproject),
            "--changelog",
            str(changelog),
            "--output",
            str(output),
            "--check-version",
        ],
    )
    assert main() == 0
    assert output.read_text(encoding="utf-8").strip() == "### Added\n- Verified feature"


def test_cli_subprocess(tmp_path):
    root = Path(__file__).resolve().parent.parent
    res = subprocess.run(
        [sys.executable, "-m", "skilldiff.release", "--tag", "v0.11.1", "--check-version"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "### Fixed" in res.stdout
