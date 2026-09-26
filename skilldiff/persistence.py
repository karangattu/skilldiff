"""Durable file publication and exclusive ownership of experiment output."""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def atomic_write(path: Path, content: str) -> None:
    """Publish a complete file, leaving the previous version intact on failure."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2, allow_nan=False))


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read saved state {path}: {exc}") from exc


@contextmanager
def run_lock(root: Path) -> Iterator[None]:
    """OS locks release after crashes; the stable lock file lives outside the run.

    Never unlink this file: replacing its inode could let two writers acquire
    different locks for the same run. Validation does not modify the run itself.
    """
    root = root.resolve()
    with (root.parent / f".{root.name}.lock").open("a+b") as stream:
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt

                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ValueError(f"Run is already locked by another writer: {root}") from exc
        try:
            yield
        finally:
            if os.name == "posix":
                fcntl.flock(stream, fcntl.LOCK_UN)
            else:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
