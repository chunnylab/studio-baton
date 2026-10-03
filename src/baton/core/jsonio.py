"""Crash-safe JSON and text persistence.

Ported from the original workspace ``scripts/utils.py``. Locking uses POSIX
``fcntl``; Baton does not support Windows.

The guarantee callers rely on: a kill at any instant leaves either the previous
file fully intact or the new file fully written, never a truncated one. Every
piece of resumable pipeline state in Baton goes through here.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@contextmanager
def _locked(path: Path, exclusive: bool) -> Iterator[None]:
    """Hold an advisory lock on a sidecar ``.lock`` file for the duration.

    Advisory locking is best-effort by design: it serialises Baton against
    itself (two commands touching one state file), which is the only contention
    that actually happens. It is not a defence against an unrelated process
    rewriting the file.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)


def backup_path(path: Path) -> Path:
    """Sidecar snapshot path used as the recovery source for a corrupt file."""
    return path.with_suffix(path.suffix + ".bak")


def _snapshot(path: Path) -> None:
    """Copy ``path`` onto its ``.bak`` atomically.

    The backup is the only recovery source when the live file is truncated, so
    it cannot itself be written in a way a crash can truncate. Copying straight
    onto the destination (which is what this used to do) leaves a half-written
    backup if the power goes during the copy, losing the one good copy of the
    state at exactly the moment it is needed.
    """
    backup = backup_path(path)
    tmp = backup.with_suffix(backup.suffix + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(path.read_bytes())
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, backup)


def _quarantine(path: Path) -> Path | None:
    """Move an unreadable file aside, keeping its bytes for inspection.

    Returns the new path, or ``None`` if it could not be moved: this runs on
    the failure path of a function that promises never to raise, so it must not
    become the thing that raises.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    target = path.with_suffix(path.suffix + f".corrupt-{stamp}")
    try:
        os.replace(path, target)
    except OSError:
        return None
    return target


def write_json(path: str | Path, data: Any) -> None:
    """Write ``data`` as JSON atomically, snapshotting the old file first.

    Writes to a temp file in the same directory, fsyncs it, then ``os.replace``
    (atomic on one filesystem) into place while holding an exclusive lock.

    Args:
        path: Destination file.
        data: Any JSON-serialisable value. The existing file, if any, is
            copied to ``<name>.bak`` before it is replaced.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")

    with _locked(path, exclusive=True):
        if path.exists():
            _snapshot(path)
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)


def read_json(path: str | Path, default: Any = None) -> Any:
    """Read JSON, falling back to the ``.bak`` snapshot. Never raises.

    A missing file yields ``default``. A corrupt or unreadable file is retried
    from the backup snapshot before giving up, so a state file truncated by a
    power cut degrades to the previous good state instead of crashing a
    pipeline mid-run.

    Args:
        path: File to read.
        default: Returned when neither the file nor its backup is usable.

    Returns:
        The parsed value, or ``default``.
    """
    path = Path(path)

    def _load(candidate: Path) -> tuple[bool, Any]:
        if not candidate.exists():
            return False, None
        try:
            with _locked(candidate, exclusive=False), open(candidate, encoding="utf-8") as handle:
                return True, json.load(handle)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return False, None

    ok, value = _load(path)
    if ok:
        return value

    ok, value = _load(backup_path(path))
    if ok:
        return value

    # Both copies are gone. Returning `default` here is right (a pipeline
    # should not die on unreadable state), but doing it silently makes a
    # corrupt draft indistinguishable from one that was never written, which
    # is how a lesson someone typed up disappears without anyone learning
    # that it did. Keep the bytes, and say so.
    if path.exists():
        kept = _quarantine(path)
        where = f" Its contents were kept at {kept.name}." if kept else ""
        print(
            f"! {path.name} could not be read and neither could its backup; "
            f"continuing without it.{where}",
            file=sys.stderr,
            flush=True,
        )

    return default
