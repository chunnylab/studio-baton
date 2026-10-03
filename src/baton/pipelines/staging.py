"""Lesson drafts on their way to being published.

A lesson is staged, then summarised, then published: often across separate
commands, sometimes across separate days, and occasionally across a crash. Each
draft is one atomically-written file under ``<state>/lessons/``.

Publishing records what succeeded per target. Re-running skips a target that is
already done, which is what makes ``publish`` safe to retry: appending the same
summary twice leaves a document with two copies of it, and nothing in Notion
will tell you which is which.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ..core import jsonio
from ..core.paths import safe_key as _slug
from ..domain.models import Piece, Work
from ..errors import UsageError

#: Draft lifecycle. `summarised` means a validated summary is attached.
STAGED = "staged"
SUMMARISED = "summarised"
PUBLISHED = "published"
SUMMARY = "summary"
RECORDING = "recording"


PieceSnapshotStatus = Literal["captured", "none", "unavailable"]

#: What :meth:`StagingStore.clear` hands back: names deleted, and one entry
#: per draft deliberately kept, with each unfinished target's status. A module
#: alias because ``list`` names a method on the class it would otherwise be
#: annotated inside.
ClearResult = tuple[list[str], list[dict[str, Any]]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _invalid_snapshot() -> UsageError:
    return UsageError(
        "The staged lesson has an invalid piece snapshot.",
        remedy="Re-stage the lesson so Baton can capture the Song DB row again.",
    )


@dataclass(frozen=True)
class PieceSnapshot:
    """The Song DB state observed when one lesson was staged."""

    status: PieceSnapshotStatus
    captured_at: str = ""
    piece: Piece | None = None

    @classmethod
    def capture(cls, piece: Piece | None) -> PieceSnapshot:
        if piece is None:
            return cls(status="none", captured_at=_now())
        return cls(status="captured", captured_at=_now(), piece=piece)

    @classmethod
    def unavailable(cls) -> PieceSnapshot:
        """Represent a legacy record written before snapshots existed."""
        return cls(status="unavailable")

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> PieceSnapshot:
        """Read a snapshot from a draft or published-record mapping."""
        if "piece_snapshot" not in record:
            return cls.unavailable()

        raw = record["piece_snapshot"]
        if not isinstance(raw, Mapping):
            raise _invalid_snapshot()

        status = raw.get("status")
        captured_at = raw.get("captured_at", "")
        piece_data = raw.get("piece")

        if status == "unavailable":
            if captured_at or piece_data is not None:
                raise _invalid_snapshot()
            return cls.unavailable()

        if not isinstance(captured_at, str) or not captured_at:
            raise _invalid_snapshot()
        if status == "none":
            if piece_data is not None:
                raise _invalid_snapshot()
            return cls(status="none", captured_at=captured_at)
        if status != "captured" or not isinstance(piece_data, Mapping):
            raise _invalid_snapshot()

        values = {
            name: piece_data.get(name, "")
            for name in ("id", "title", "source_link", "practice_track", "sheet_link")
        }
        if any(not isinstance(value, str) for value in values.values()):
            raise _invalid_snapshot()
        if not values["id"].strip() or not values["title"].strip():
            raise _invalid_snapshot()
        return cls(
            status="captured",
            captured_at=captured_at,
            piece=Piece(**values),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "captured_at": self.captured_at,
            "piece": self.piece.to_dict() if self.piece is not None else None,
        }

    def same_content(self, other: PieceSnapshot) -> bool:
        """Compare observed Song DB content while ignoring capture time."""
        own_piece = self.piece.to_dict() if self.piece is not None else None
        other_piece = other.piece.to_dict() if other.piece is not None else None
        return self.status == other.status and own_piece == other_piece


@dataclass
class LessonDraft:
    """One lesson being prepared."""

    learner_id: str
    learner_name: str
    session_number: int
    piece_snapshot: PieceSnapshot = field(default_factory=PieceSnapshot.unavailable)
    doc_id: str = ""
    titles: str = ""
    context: str = ""
    """Raw notes the teacher gave: what the model summarises from."""
    corrected_context: str = ""
    """The same notes with spellings fixed, when a studio keeps a vocabulary.

    Served to the model instead of `context` by `lesson contract`. The raw
    notes are never overwritten by this: a correction the teacher disagrees
    with has to be checkable against what they actually wrote, and the raw
    copy is also what a later re-stage should not silently lose.
    """
    previous_context: str = ""
    """The previous session's summary, for scope rather than for copying."""
    summary: dict[str, Any] | None = None
    """The validated structure. ``None`` until `ingest` accepts one."""
    kind: str = SUMMARY
    """Whether this draft publishes a teaching summary or a recorded work."""
    work_id: str = ""
    """The selected work when ``kind`` is ``recording``."""
    work: Work | None = None
    """The work as it existed when the recording lesson was completed."""
    status: str = STAGED
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    targets: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Per-target publish state: ``{"docs": {"status": "ok", "at": ...}}``."""

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "learner_id": self.learner_id,
            "learner_name": self.learner_name,
            "session_number": self.session_number,
            "piece_snapshot": self.piece_snapshot.to_dict(),
            "doc_id": self.doc_id,
            "titles": self.titles,
            "context": self.context,
            "corrected_context": self.corrected_context,
            "previous_context": self.previous_context,
            "summary": self.summary,
            "kind": self.kind,
            "work_id": self.work_id,
            "work": self.work.to_dict() if self.work is not None else None,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "targets": self.targets,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LessonDraft:
        return cls(
            learner_id=str(data.get("learner_id", "")),
            learner_name=str(data.get("learner_name", "")),
            session_number=int(data.get("session_number", 0)),
            piece_snapshot=PieceSnapshot.from_record(data),
            doc_id=str(data.get("doc_id", "")),
            titles=str(data.get("titles", "")),
            context=str(data.get("context", "")),
            corrected_context=str(data.get("corrected_context", "")),
            previous_context=str(data.get("previous_context", "")),
            summary=data.get("summary"),
            kind=str(data.get("kind", SUMMARY)),
            work_id=str(data.get("work_id", "")),
            work=Work.from_dict(data["work"]) if isinstance(data.get("work"), dict) else None,
            status=str(data.get("status", STAGED)),
            created_at=str(data.get("created_at", _now())),
            updated_at=str(data.get("updated_at", _now())),
            targets=dict(data.get("targets", {})),
        )

    # -- summaries ---------------------------------------------------------

    def summary_view(self) -> dict[str, Any]:
        """A compact description for listings."""
        return {
            "learner_id": self.learner_id,
            "learner_name": self.learner_name,
            "session_number": self.session_number,
            "status": self.status,
            "has_summary": self.summary is not None,
            "kind": self.kind,
            "work_id": self.work_id,
            "targets": {name: state.get("status") for name, state in self.targets.items()},
            "updated_at": self.updated_at,
        }

    # -- publish state -----------------------------------------------------

    def target_done(self, name: str) -> bool:
        """Whether a target already succeeded and must not be repeated."""
        return self.targets.get(name, {}).get("status") == "ok"

    def record_target(self, name: str, status: str, **extra: Any) -> None:
        """Record the outcome of publishing to one target."""
        state = dict(self.targets.get(name, {}))
        state["status"] = status
        if status == "ok":
            state.pop("error", None)
        state["at"] = _now()
        state["attempts"] = int(state.get("attempts", 0)) + 1
        state.update(extra)
        self.targets[name] = state
        self.updated_at = _now()

    def note_target(self, name: str, **extra: Any) -> None:
        """Add to what is known about a target without counting a new attempt.

        A publish that appended and then finished the session is one attempt,
        not two; counting the second write separately would make the record
        read as a retry that never happened.
        """
        state = dict(self.targets.get(name, {}))
        state.update(extra)
        self.targets[name] = state
        self.updated_at = _now()


class StagingStore:
    """Drafts on disk, one file per learner."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, learner_id: str) -> Path:
        return self.root / f"{_slug(learner_id)}.json"

    # -- reads -------------------------------------------------------------

    def get(self, learner_id: str) -> LessonDraft | None:
        data = jsonio.read_json(self._path(learner_id), None)
        return LessonDraft.from_dict(data) if isinstance(data, dict) else None

    def require(self, learner_id: str, learner_name: str) -> LessonDraft:
        """Fetch a draft, or explain how to create one.

        Raises:
            UsageError: No draft is staged for this learner.
        """
        draft = self.get(learner_id)
        if draft is None:
            raise UsageError(
                f"No lesson is staged for {learner_name}.",
                remedy=f'Run `baton lesson stage "{learner_name}"` first.',
            )
        return draft

    def list(self) -> list[LessonDraft]:
        """Every draft, oldest first."""
        if not self.root.is_dir():
            return []
        drafts = []
        for path in sorted(self.root.glob("*.json")):
            data = jsonio.read_json(path, None)
            if isinstance(data, dict):
                drafts.append(LessonDraft.from_dict(data))
        return sorted(drafts, key=lambda draft: draft.created_at)

    # -- writes ------------------------------------------------------------

    def save(self, draft: LessonDraft) -> None:
        draft.updated_at = _now()
        jsonio.write_json(self._path(draft.learner_id), draft.to_dict())

    def remove(self, learner_id: str) -> bool:
        """Delete one draft. Returns whether there was anything to delete."""
        path = self._path(learner_id)
        existed = path.exists()
        for candidate in (path, jsonio.backup_path(path)):
            candidate.unlink(missing_ok=True)
        return existed

    def clear(self, *, keep_unfinished: bool = False) -> ClearResult:
        """Delete drafts, naming what was removed and what was deliberately not.

        A publish that did not finish leaves a draft with an unfinished target,
        and that draft holds the only note that the work is still owed: the
        next ``stage`` overwrites the file wholesale, and the published record
        only exists once the summary went out. So with ``keep_unfinished`` a
        draft in that state survives the sweep and is reported back instead;
        ``--force`` is what deletes it for real.

        A draft with no targets at all has never been published and has nothing
        owed on it, so it clears either way.

        Returns:
            ``(removed, kept)``: the names deleted, and one entry per kept
            draft carrying its name and each unfinished target's status.
        """
        removed: list[str] = []
        kept: list[dict[str, Any]] = []
        for draft in self.list():
            unfinished = {
                name: state.get("status", "")
                for name, state in draft.targets.items()
                if state.get("status") != "ok"
            }
            if keep_unfinished and unfinished:
                kept.append({"learner_name": draft.learner_name, "targets": unfinished})
                continue
            if self.remove(draft.learner_id):
                removed.append(draft.learner_name)
        return removed, kept


class PublishedRecord:
    """What was published, kept after the draft is cleared.

    The message a parent receives is composed at publish time, and the command
    that sends it runs later: often much later. Keeping the rendered message
    here means the send step never has to re-derive it, and so can never
    produce a different message from the one that was reviewed.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, learner_id: str, session_number: int) -> Path:
        return self.root / f"{_slug(learner_id)}-{int(session_number)}.json"

    def save(
        self,
        draft: LessonDraft,
        *,
        short_message: str,
        doc_url: str = "",
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Write the record, including the blocks the publish put on the page.

        ``blocks`` (``[{"id", "type", "text"}, ...]``) is what makes a later
        `lesson unpublish` able to name exactly what it owns: matched by id,
        not by re-deriving what the renderer would say today. Records written
        before the list existed simply carry none, and unpublish falls back to
        matching the stored summary's rendering instead.
        """
        jsonio.write_json(
            self._path(draft.learner_id, draft.session_number),
            {
                "learner_id": draft.learner_id,
                "learner_name": draft.learner_name,
                "session_number": draft.session_number,
                "piece_snapshot": draft.piece_snapshot.to_dict(),
                "doc_id": draft.doc_id,
                "doc_url": doc_url,
                "titles": draft.titles,
                "short_message": short_message,
                "kind": draft.kind,
                "work_id": draft.work_id,
                "work": draft.work.to_dict() if draft.work is not None else None,
                # Kept alongside the message composed from it, because the two
                # answer different questions later. The message is what the
                # family was told; the summary is what the lesson was, and it
                # is what the next lesson has to be compared against. A draft
                # is overwritten by the next `stage`, and the document can be
                # edited by hand, so this is the only durable copy.
                "summary": draft.summary,
                "blocks": blocks or [],
                "published_at": _now(),
            },
        )

    def remove(self, learner_id: str, session_number: int) -> bool:
        """Delete one published record. Returns whether there was one.

        Called by `lesson unpublish` after the page and the draft have been
        settled; removing it first would leave a crash mid-way looking like a
        publish that never happened.
        """
        path = self._path(learner_id, session_number)
        existed = path.exists()
        for candidate in (path, jsonio.backup_path(path)):
            candidate.unlink(missing_ok=True)
        return existed

    def get(self, learner_id: str, session_number: int) -> dict[str, Any] | None:
        data = jsonio.read_json(self._path(learner_id, session_number), None)
        return data if isinstance(data, dict) else None

    def note_youtube(self, learner_id: str, session_number: int, state: dict[str, Any]) -> None:
        """Fold a description-update outcome into an existing record.

        The record is written before the description step runs (the message
        must survive even when the video update fails), so the outcome is
        folded in afterwards rather than the record rewritten, and
        ``published_at`` keeps meaning "when the summary went out".

        This is the durable half of the youtube target's memory: re-staging
        overwrites the draft wholesale, and a re-run that could no longer tell
        whether the description had been written would either skip work still
        owed or repeat work already done.
        """
        path = self._path(learner_id, session_number)
        data = jsonio.read_json(path, None)
        if not isinstance(data, dict):
            return
        data["youtube"] = state
        jsonio.write_json(path, data)

    def latest(self, learner_id: str) -> dict[str, Any] | None:
        """The most recently published session for a learner.

        The filename prefix alone cannot separate `ada` from `ada-1`: the
        shorter id is a prefix of the longer one, so a glob for one sweeps in
        the other's records, and the wrong learner's message would be sent.
        The learner id stored inside each record is the identity; the filename
        is only the index.
        """
        if not self.root.is_dir():
            return None
        prefix = f"{_slug(learner_id)}-"
        records = []
        for path in self.root.glob(f"{prefix}*.json"):
            data = jsonio.read_json(path, None)
            if isinstance(data, dict) and str(data.get("learner_id", "")) == str(learner_id):
                records.append(data)
        if not records:
            return None

        def _order(item: dict[str, Any]) -> tuple[str, int]:
            """Newest first by time, then by session.

            `published_at` is written to the second, so two publishes inside
            one second compare equal and `max` falls back to whichever the
            filesystem's glob happened to yield first: an ordering that
            differs between Linux and macOS and is not an ordering at all.
            Left there, `send lesson` with no `--session` could pick the
            earlier of the two and send last week's message. The session
            number is the tie-break because it is the thing that actually
            advances.
            """
            try:
                session = int(item.get("session_number", 0) or 0)
            except (TypeError, ValueError):
                session = 0
            return str(item.get("published_at", "")), session

        return max(records, key=_order)
