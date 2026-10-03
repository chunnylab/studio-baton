"""Detached job execution, supervision, and whole-run locking.

Video work runs for tens of minutes. An agent's session may not. ``--detach``
hands the work to a supervisor process that outlives the CLI call, the agent
session, and the terminal that started it, while recording enough state that
``baton job status`` can always say what happened, even after a crash or a
reboot.

Shape of a job on disk, under ``<state>/jobs/<id>/``::

    meta.json     lifecycle record: status, pid, exit code, timestamps
    log.txt       everything the job printed, stdout and stderr
    heartbeat     mtime touched every few seconds while the supervisor lives

The supervisor is ``baton job supervise`` (hidden from help): it re-executes any
command, captures its output, and writes the lifecycle record. Wrapping rather
than embedding is what lets *any* long command get job semantics, baton or not,
and keeps pipeline code free of supervisor concerns: a pipeline records its own
steps in its own state file and never talks to this module directly.

Liveness is derived, never trusted blindly: a job whose recorded status is
``running`` but whose supervisor pid is dead (or whose heartbeat went stale)
reads as ``orphaned``. Because pipelines are resumable, the answer to an
orphaned job is to re-run the same command, not to investigate.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..errors import BusyError, UsageError
from . import jsonio

JOBS_DIRNAME = "jobs"

#: Statuses a job can settle into. ``running`` is the only transient one.
TERMINAL_STATUSES = frozenset({"done", "failed", "stopped", "orphaned"})

#: A supervisor touches the heartbeat every HEARTBEAT_SECONDS; a file older than
#: HEARTBEAT_STALE_SECONDS means the supervisor is alive but wedged (a hung
#: encode, a full disk): reported so a human decides rather than the job
#: pretending to be fine.
HEARTBEAT_SECONDS = 5.0
HEARTBEAT_STALE_SECONDS = 300.0

#: Finished jobs older than this stop appearing in `job list`. A record that
#: ended days ago is history, not a live problem, but it used to sit in the
#: listing until the 14-day prune deleted it: a failed job from half a week
#: back read as tonight's failure. `--all` shows everything; `job status <id>`
#: always works.
DEFAULT_LIST_STALE_DAYS = 3.0

if sys.platform == "win32":  # pragma: no cover - platform specific
    import ctypes
    import msvcrt
else:
    import fcntl


def _now() -> str:
    # Millisecond precision: `job list` sorts by id, and the id embeds this
    # timestamp, so two jobs started within a second of each other must still
    # compare in the order they were started.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def pid_alive(pid: int | None) -> bool:
    """Whether a process is running right now.

    Must never be given ``os.kill`` on Windows: there, ``os.kill`` with a
    non-signal argument *terminates* the target instead of probing it. The
    ctypes path is the safe probe.
    """
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover - platform specific
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True  # cannot tell; assume alive rather than declare orphaned
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


@dataclass
class JobInfo:
    """The lifecycle record of one job, as reported to callers."""

    id: str
    name: str
    command: list[str]
    status: str  # running | done | failed | stopped | orphaned
    pid: int | None = None
    exit_code: int | None = None
    started_at: str = ""
    ended_at: str = ""
    heartbeat_age: float | None = None
    log: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "command": self.command,
            "started_at": self.started_at,
        }
        if self.pid is not None:
            payload["pid"] = self.pid
        if self.exit_code is not None:
            payload["exit_code"] = self.exit_code
        if self.ended_at:
            payload["ended_at"] = self.ended_at
        if self.heartbeat_age is not None:
            payload["heartbeat_age_seconds"] = round(self.heartbeat_age, 1)
        if self.log:
            payload["log"] = self.log
        return payload


class JobRunner:
    """Spawn, inspect, wait on, and stop detached jobs for one profile."""

    def __init__(
        self,
        state_dir: Path,
        config_file: Path,
        *,
        list_stale_days: float = DEFAULT_LIST_STALE_DAYS,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.jobs_dir = self.state_dir / JOBS_DIRNAME
        self.config_file = config_file
        self.list_stale_days = list_stale_days

    # -- paths -------------------------------------------------------------

    def _job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def _meta_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "meta.json"

    def _log_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "log.txt"

    def _heartbeat_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "heartbeat"

    # -- records -----------------------------------------------------------

    def _read_meta(self, job_id: str) -> dict[str, Any] | None:
        meta = jsonio.read_json(self._meta_path(job_id), None)
        return meta if isinstance(meta, dict) else None

    def _write_meta(self, job_id: str, **fields: Any) -> None:
        """Merge ``fields`` into a job's record atomically.

        Merge rather than overwrite: the parent and the supervisor both write
        this file, and whichever loses the race must not clobber the other.
        """
        meta = self._read_meta(job_id) or {}
        meta.update(fields)
        jsonio.write_json(self._meta_path(job_id), meta)

    # -- spawn -------------------------------------------------------------

    def spawn(self, argv: list[str], *, name: str | None = None) -> JobInfo:
        """Start ``argv`` detached under a supervisor and return immediately.

        Args:
            argv: The command to run, ``["--"]``-safe: it is passed through
                untouched.
            name: Human label for listings. Defaults to the command's basename.

        Returns:
            The initial :class:`JobInfo` (``status=running``).
        """
        if not argv:
            raise ValueError("spawn requires a non-empty command")
        now = _utcnow()
        job_id = f"{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}-{uuid.uuid4().hex[:4]}"
        label = name or Path(argv[0]).name

        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        # Long-lived on purpose: handed to the supervisor, which owns it from
        # here on. A `with` block would close it when this method returns.
        log_handle = open(self._log_path(job_id), "ab")  # noqa: SIM115

        # Record before spawning so `job status` in the microseconds after
        # spawn never reports an unknown id.
        self._write_meta(
            job_id,
            id=job_id,
            name=label,
            command=list(argv),
            status="running",
            started_at=_now(),
        )

        supervisor_argv = [
            sys.executable,
            "-m",
            "baton",
            "--profile",
            str(self.config_file),
            "job",
            "supervise",
            "--id",
            job_id,
            "--",
            *argv,
        ]
        # A grandchild must not see BATON_JOB_ID or it would try to record a
        # lifecycle the supervisor already owns.
        env = {k: v for k, v in os.environ.items() if k != "BATON_JOB_ID"}

        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "env": env,
            "cwd": os.getcwd(),
            "close_fds": True,
        }
        if sys.platform == "win32":  # pragma: no cover - platform specific
            popen_kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            # New session: no controlling terminal, immune to the HUP that
            # arrives when the invoking terminal closes.
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(supervisor_argv, **popen_kwargs)
        self._write_meta(job_id, pid=process.pid)
        # The supervisor owns the log handle from here; leaving it open here too
        # only delays cleanup on exotic platforms. The Popen child dup'd it.
        log_handle.close()

        return JobInfo(
            id=job_id,
            name=label,
            command=list(argv),
            status="running",
            pid=process.pid,
            started_at=_now(),
            log=str(self._log_path(job_id)),
        )

    # -- supervise (runs inside the detached process) ----------------------

    def supervise(self, job_id: str, argv: list[str]) -> int:
        """Run ``argv`` as the supervisor, recording its lifecycle. Exit code
        of this method is the child's exit code, so the supervisor's own exit
        matches the work it supervised."""
        # _write_meta -> write_json mkdir()s its parent unconditionally, so an
        # id nobody spawned would otherwise mint a fresh job dir instead of
        # failing. spawn() always pre-creates the dir before invoking this, so
        # its absence here means the caller passed an id that was never ours.
        if not self._job_dir(job_id).exists():
            raise UsageError(
                f"No job matches id `{job_id}`.",
                remedy="`job supervise` is internal; jobs are created with `job spawn`.",
            )
        self._write_meta(job_id, pid=os.getpid(), status="running")
        heartbeat = self._heartbeat_path(job_id)
        heartbeat.touch()

        stop_requested = threading.Event()

        def _beat() -> None:
            while not stop_requested.wait(HEARTBEAT_SECONDS):
                with suppress(OSError):
                    heartbeat.touch()

        beater = threading.Thread(target=_beat, name="baton-heartbeat", daemon=True)
        beater.start()

        # Long-lived on purpose: it is the child's stdout/stderr, closed in
        # the finally block below once the child has exited.
        log_handle = open(self._log_path(job_id), "ab")  # noqa: SIM115
        child: subprocess.Popen[bytes] | None = None
        status = "failed"
        exit_code = 1
        try:

            def _on_signal(signum: int, _frame: Any) -> None:
                stop_requested.set()
                if child is not None:
                    with suppress(OSError):
                        child.terminate()

            # Installed before Popen, not after: a SIGTERM landing in that
            # window would otherwise have no handler to catch it and no
            # child reference yet to terminate, leaving meta stuck at
            # "running" until orphan detection found it on a later pass.
            for sig in (signal.SIGTERM, signal.SIGINT):
                with suppress(ValueError, OSError):
                    signal.signal(sig, _on_signal)

            child = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env={k: v for k, v in os.environ.items() if k != "BATON_JOB_ID"},
            )

            exit_code = child.wait()
            status = (
                "stopped" if stop_requested.is_set() else ("done" if exit_code == 0 else "failed")
            )
            return exit_code
        finally:
            stop_requested.set()
            if child is not None and child.poll() is None:
                with suppress(OSError):
                    child.kill()
                with suppress(Exception):
                    child.wait(timeout=5)
            log_handle.close()
            self._write_meta(job_id, status=status, exit_code=exit_code, ended_at=_now())

    # -- inspect -----------------------------------------------------------

    def get(self, job_id: str) -> JobInfo | None:
        """Read one job, deriving ``orphaned`` from observed liveness."""
        meta = self._read_meta(job_id)
        if meta is None:
            return None

        status = str(meta.get("status", "running"))
        hb_path = self._heartbeat_path(job_id)
        heartbeat_age: float | None = None
        # exists() and stat() are two calls: a concurrent prune can remove the
        # job dir between them, and a vanished heartbeat means "no heartbeat",
        # not a crash.
        with suppress(FileNotFoundError):
            if hb_path.exists():
                heartbeat_age = time.time() - hb_path.stat().st_mtime

        if status == "running":
            alive = pid_alive(meta.get("pid"))
            wedged = heartbeat_age is not None and heartbeat_age > HEARTBEAT_STALE_SECONDS
            if not alive or wedged:
                # Derived, not persisted: if the supervisor is merely between
                # heartbeats this stays "running" on the next read, and a job
                # that recorded a terminal status never passes through here.
                status = "orphaned"

        return JobInfo(
            id=str(meta.get("id", job_id)),
            name=str(meta.get("name", "")),
            command=[str(part) for part in meta.get("command", [])],
            status=status,
            pid=meta.get("pid"),
            exit_code=meta.get("exit_code"),
            started_at=str(meta.get("started_at", "")),
            ended_at=str(meta.get("ended_at", "")),
            heartbeat_age=heartbeat_age,
            log=str(self._log_path(job_id)),
        )

    def list(self, *, include_stale: bool = False) -> list[JobInfo]:
        """All jobs, newest first (ids start with a timestamp, so they sort).

        Args:
            include_stale: Also list finished jobs older than
                :attr:`list_stale_days`. Running jobs are always listed:
                hiding one, however stale its heartbeat, would hide a live
                process and the lock it may hold.
        """
        if not self.jobs_dir.is_dir():
            return []
        cutoff = _utcnow() - timedelta(days=self.list_stale_days)
        jobs: list[JobInfo] = []
        for entry in sorted(self.jobs_dir.iterdir(), reverse=True):
            info = self.get(entry.name)
            if info is None:
                continue
            stale = (self._settled_at(info) or _utcnow()) < cutoff
            if not include_stale and info.status != "running" and stale:
                continue
            jobs.append(info)
        return jobs

    def _settled_at(self, info: JobInfo) -> datetime | None:
        """When a terminal job ended, or ``None`` when it never said."""
        ended = info.ended_at or info.started_at
        if not ended:
            return None
        try:
            settled = datetime.fromisoformat(ended)
        except ValueError:
            return None
        return settled if settled.tzinfo else settled.replace(tzinfo=timezone.utc)

    def logs(self, job_id: str, *, tail: int | None = None) -> str:
        """Read a job's log, optionally keeping only the last ``tail`` lines."""
        path = self._log_path(job_id)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        if tail is not None and tail > 0:
            text = "\n".join(text.splitlines()[-tail:])
        return text

    # -- control -----------------------------------------------------------

    def wait(self, job_id: str, *, timeout: float | None = None) -> JobInfo | None:
        """Block until a job reaches a terminal status or the timeout expires.

        Args:
            job_id: Job to wait on.
            timeout: Seconds to wait. ``None`` waits forever.

        Returns:
            The final :class:`JobInfo` (terminal status), the last observed
            one on timeout, or ``None`` when the id is unknown.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            info = self.get(job_id)
            if info is None or info.status in TERMINAL_STATUSES:
                return info
            if deadline is not None and time.monotonic() >= deadline:
                return info
            time.sleep(0.3)

    def _require(self, job_id: str) -> JobInfo:
        """Read a job that is known to exist.

        Callers reach this only after a prior read succeeded; the record cannot
        vanish underneath them, so a miss here is a bug rather than a condition
        to handle.
        """
        info = self.get(job_id)
        if info is None:
            raise KeyError(job_id)
        return info

    def stop(self, job_id: str, *, grace: float = 10.0) -> JobInfo:
        """Ask a running job to stop, escalating to a kill after ``grace`` seconds.

        Returns:
            The job's final info.
        """
        info = self._require(job_id)
        if info.status in TERMINAL_STATUSES:
            return info

        pid = info.pid
        if pid:
            if sys.platform == "win32":  # pragma: no cover - platform specific
                # No signal semantics on Windows: ask the whole tree to close,
                # then force it.
                with suppress(OSError):
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T"],  # noqa: S607 - on PATH by definition on Windows
                        capture_output=True,
                        check=False,
                    )
                settled = self.wait(job_id, timeout=grace)
                if settled is not None and settled.status in TERMINAL_STATUSES:
                    return settled
                with suppress(OSError):
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],  # noqa: S607
                        capture_output=True,
                        check=False,
                    )
            else:
                # SIGTERM first so the supervisor can record `stopped` and let
                # the child clean up; SIGKILL only once grace has run out.
                with suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + grace
                while time.monotonic() < deadline:
                    current = self._require(job_id)
                    if current.status in TERMINAL_STATUSES:
                        return current
                    time.sleep(0.2)
                with suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)

        settled = self.wait(job_id, timeout=5.0)
        return settled if settled is not None else self._require(job_id)

    def prune(self, *, older_than_days: int = 14) -> int:
        """Delete finished jobs older than the cutoff. Running jobs are never touched.

        Returns:
            How many job directories were removed.
        """
        cutoff = _utcnow() - timedelta(days=older_than_days)
        removed = 0
        if not self.jobs_dir.is_dir():
            return 0
        for entry in self.jobs_dir.iterdir():
            info = self.get(entry.name)
            if info is None or info.status not in TERMINAL_STATUSES:
                continue
            settled_at = self._settled_at(info)
            if settled_at is None or settled_at >= cutoff:
                continue
            for victim in entry.rglob("*"):
                with suppress(OSError):
                    victim.unlink()
            try:
                entry.rmdir()
            except OSError:
                # Something survived the unlink sweep (a subdirectory, a
                # concurrent writer). The dir is still on disk, so it must
                # not be counted as removed.
                continue
            removed += 1
        return removed


@dataclass
class RunLock:
    """Whole-run exclusive lock, held by an open handle.

    Unlike the per-write locks in :mod:`baton.core.jsonio`, this one is held for
    the life of the process: the OS releases it when the process dies, whatever
    the manner of death. That is the property that makes it safe against
    collision: a crashed run cannot leave a stale lock behind, so there is no
    "delete the lockfile by hand" recovery procedure to document.

    The holder's pid is written inside for the collision message.
    """

    path: Path
    _handle: Any = field(default=None, init=False, repr=False)

    def acquire(self) -> None:
        """Take the lock or raise :class:`BusyError`.

        Raises:
            BusyError: Another live process holds it. The message names the
                holder so the operator can wait on or stop that run instead.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Held open for the life of the lock: closing it *is* releasing it.
        handle = open(self.path, "a+b")  # noqa: SIM115
        try:
            if sys.platform == "win32":  # pragma: no cover - platform specific
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            holder = "unknown"
            with suppress(OSError):
                holder = self.path.read_text(encoding="utf-8").strip() or "unknown"
            raise BusyError(
                f"Another run already holds {self.path.name}.",
                remedy="Wait for it to finish (`baton job list`), or stop it "
                "(`baton job stop <id>`), then re-run.",
                details={"lock": str(self.path), "holder_pid": holder},
            ) from None
        self._handle = handle
        with suppress(OSError):
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n".encode())
            handle.flush()

    def release(self) -> None:
        if self._handle is not None:
            with suppress(OSError):
                if sys.platform == "win32":  # pragma: no cover - platform specific
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
        self._handle = None

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def run_lock(state_dir: Path, name: str) -> RunLock:
    """A named whole-run lock under the state directory."""
    return RunLock(Path(state_dir) / "locks" / f"{name}.lock")
