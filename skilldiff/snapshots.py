import ctypes
import hashlib
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

from skilldiff.config import ExperimentConfig, TaskConfig
from skilldiff.persistence import atomic_json, read_json

SNAPSHOT_VERSION = 1
LOCK_NAMES = (
    "requirements.txt",
    "requirements.lock",
    "uv.lock",
    "poetry.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "pyproject.toml",
)

_CLONEFILE = None
if sys.platform == "darwin":
    try:
        _libc = ctypes.CDLL(None)
        if hasattr(_libc, "clonefile"):
            _clone = _libc.clonefile
            _clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
            _clone.restype = ctypes.c_int
            _CLONEFILE = _clone
    except Exception:
        _CLONEFILE = None

_FICLONE = 0x40049409 if sys.platform.startswith("linux") else None


def supports_reflink() -> bool:
    """Whether copy-on-write clonefile or FICLONE ioctl is potentially available."""
    return _CLONEFILE is not None or _FICLONE is not None


def reflink_copy(src: Path | str, dst: Path | str) -> bool:
    """Attempt a copy-on-write reflink (APFS on macOS, FICLONE on Linux)."""
    src_str = os.fspath(src)
    dst_str = os.fspath(dst)
    if _CLONEFILE is not None:
        try:
            if os.path.exists(dst_str) or os.path.islink(dst_str):
                try:
                    os.unlink(dst_str)
                except OSError:
                    pass
            ret = _CLONEFILE(src_str.encode("utf-8"), dst_str.encode("utf-8"), 0)
            if ret == 0:
                return True
        except Exception:
            pass
    elif _FICLONE is not None:
        try:
            import fcntl

            with open(src_str, "rb") as f_src:
                with open(dst_str, "wb") as f_dst:
                    fcntl.ioctl(f_dst.fileno(), _FICLONE, f_src.fileno())
            shutil.copystat(src_str, dst_str)
            return True
        except Exception:
            pass
    return False


def copy_file_cow(src: Path | str, dst: Path | str, *, reflink: bool | None = None) -> None:
    if reflink is None:
        reflink = os.environ.get("SKILLDIFF_REFLINK", "").lower() in {"1", "true", "yes"}
    if reflink and reflink_copy(src, dst):
        return
    shutil.copy2(src, dst)



_FILE_HASH_CACHE: dict[tuple[str, int, int], str] = {}


def clear_file_hash_cache() -> None:
    _FILE_HASH_CACHE.clear()


def file_hash(path: Path, stat_result: os.stat_result | None = None) -> str:
    cache_key = None
    try:
        st = stat_result or path.stat()
        cache_key = (str(path.resolve()), st.st_mtime_ns, st.st_size)
        if cache_key in _FILE_HASH_CACHE:
            return _FILE_HASH_CACHE[cache_key]
    except OSError:
        pass

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    result = digest.hexdigest()
    if cache_key is not None:
        _FILE_HASH_CACHE[cache_key] = result
    return result


def tree_contents(root: Path, *, grader: bool = False) -> list[dict[str, Any]]:
    """Inventory regular files, directories, and confined links; fail on unreadable input."""
    root = root.resolve(strict=True)
    entries: list[dict[str, Any]] = []

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir()):
            if path.name == ".git":
                continue
            if grader and (
                path.name in {"__pycache__", ".pytest_cache"} or path.suffix in {".pyc", ".pyo"}
            ):
                continue
            rel = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                try:
                    target = path.resolve(strict=True).relative_to(root)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise ValueError(f"Input symlink escapes or is broken: {path}") from exc
                if ".git" in target.parts:
                    raise ValueError(f"Input symlink points into excluded .git: {path}")
                # Directory cycles would make copying into workspaces unsafe.
                if path.resolve() in path.parents:
                    raise ValueError(f"Input symlink forms a directory cycle: {path}")
                entries.append({"path": rel, "type": "link", "target": target.as_posix()})
            elif stat.S_ISDIR(mode):
                entries.append({"path": rel, "type": "directory"})
                visit(path)
            elif stat.S_ISREG(mode):
                try:
                    st = path.stat()
                except OSError:
                    st = None
                entries.append(
                    {
                        "path": rel,
                        "type": "file",
                        "sha256": file_hash(path, stat_result=st),
                        "executable": bool(mode & 0o111),
                    }
                )
            else:
                raise ValueError(f"Unsupported input file: {path}")

    visit(root)
    return entries


def input_sources(config: ExperimentConfig, tasks: list[TaskConfig]) -> list[Path]:
    sources = [p for p in (config.skill, config.skill_a, config.skill_b) if p]
    if not config.pr:
        sources.extend(
            (t.source_path.parent / t.repo).resolve() for t in tasks if t.source_path and t.repo
        )
    return list(dict.fromkeys(p.resolve(strict=True) for p in sources))


def grader_inputs(tasks: list[TaskConfig]) -> dict[str, Any]:
    """Track the same grader directories and locks as provenance, including absences."""
    result: dict[str, Any] = {}
    for task in tasks:
        if not task.source_path:
            continue
        directory = task.source_path.parent
        graders = (
            directory.parent / "graders" if directory.name == "tasks" else directory / "graders"
        )
        result[str(graders)] = tree_contents(graders, grader=True) if graders.exists() else None
        locks = [directory / name for name in LOCK_NAMES]
        locks.extend(
            directory.parent / name for name in ("uv.lock", "requirements.txt", "pyproject.toml")
        )
        for path in locks:
            result[str(path)] = file_hash(path) if path.exists() else None
    return result


def create_snapshots(
    root: Path,
    sources: list[Path],
    guards: dict[str, Any],
    reflink: bool | None = None,
) -> dict[str, Any]:
    entries = []
    # Inventory all sources first so a change to a later source is detected too.
    before = {str(src): tree_contents(src) for src in sources}
    for index, src in enumerate(sources):
        destination = root / "inputs" / str(index) / src.name
        destination.mkdir(parents=True)
        contents = before[str(src)]
        for item in contents:
            target = destination / item["path"]
            if item["type"] == "directory":
                target.mkdir()
            elif item["type"] == "link":
                # Rebase absolute links too, so no link points back at live inputs.
                target.symlink_to(os.path.relpath(destination / item["target"], target.parent))
            else:
                copy_file_cow(src / item["path"], target, reflink=reflink)
        if tree_contents(destination) != contents:
            raise ValueError(f"Inputs changed while preparing snapshot: {src}")
        entries.append(
            {
                "source": str(src),
                "path": destination.relative_to(root).as_posix(),
                "contents": contents,
            }
        )
    for src in sources:
        if tree_contents(src) != before[str(src)]:
            raise ValueError(f"Inputs changed while preparing snapshot: {src}")
    manifest = {"version": SNAPSHOT_VERSION, "sources": entries, "grader_inputs": guards}
    (root / "inputs").mkdir(exist_ok=True)
    atomic_json(root / "inputs/manifest.json", manifest)
    return manifest


def load_snapshots(root: Path, sources: list[Path]) -> dict[str, Any]:
    manifest = read_json(root / "inputs/manifest.json")
    if manifest.get("version") != SNAPSHOT_VERSION:
        raise ValueError("Unsupported frozen-input snapshot version; start a new run")
    entries = manifest.get("sources")
    if not isinstance(entries, list) or [e.get("source") for e in entries] != [
        str(s) for s in sources
    ]:
        raise ValueError("Snapshot sources differ; start a new run")
    for entry in entries:
        path = root / entry["path"]
        if not path.resolve().is_relative_to((root / "inputs").resolve()):
            raise ValueError(f"Snapshot path escapes inputs: {path}")
        if tree_contents(path) != entry.get("contents"):
            raise ValueError(f"Snapshot contents changed: {path}")
        if tree_contents(Path(entry["source"])) != entry["contents"]:
            raise ValueError(f"Source inputs differ from snapshot: {entry['source']}")
    return manifest


def snapshot_paths(root: Path, manifest: dict[str, Any]) -> dict[Path, Path]:
    return {Path(e["source"]): root / e["path"] for e in manifest["sources"]}
