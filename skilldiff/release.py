import argparse
import re
import sys
from pathlib import Path


def get_pyproject_version(content: str) -> str:
    match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
    return match.group(1) if match else ""


def extract_release_notes(changelog: str, version: str) -> str:
    clean_version = version.lstrip("v")
    pattern = rf"## \[{re.escape(clean_version)}\][^\n]*\n(.*?)(?=\n## |$)"
    match = re.search(pattern, changelog, re.DOTALL)
    if not match:
        return ""
    return match.group(1).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract release notes for a version tag.")
    parser.add_argument("--tag", required=True, help="Git tag (e.g. v0.11.1)")
    parser.add_argument("--changelog", default="CHANGELOG.md", help="Path to CHANGELOG.md")
    parser.add_argument("--pyproject", default="pyproject.toml", help="Path to pyproject.toml")
    parser.add_argument("--output", help="Path to output file for notes")
    parser.add_argument(
        "--check-version",
        action="store_true",
        help="Error if tag does not match pyproject.toml version",
    )
    args = parser.parse_args()

    clean_tag_version = args.tag.lstrip("v")

    if args.check_version:
        pyproject_path = Path(args.pyproject)
        if not pyproject_path.exists():
            sys.stderr.write(f"pyproject.toml not found at {pyproject_path}\n")
            return 1
        pyproject_version = get_pyproject_version(pyproject_path.read_text(encoding="utf-8"))
        if clean_tag_version != pyproject_version:
            sys.stderr.write(
                f"Tag '{clean_tag_version}' does not match pyproject.toml "
                f"version '{pyproject_version}'\n"
            )
            return 1

    notes = ""
    changelog_path = Path(args.changelog)
    if changelog_path.exists():
        notes = extract_release_notes(
            changelog_path.read_text(encoding="utf-8"), clean_tag_version
        )

    if args.output:
        Path(args.output).write_text(notes + ("\n" if notes else ""), encoding="utf-8")
    else:
        if notes:
            sys.stdout.write(notes + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
