"""The things a teaching studio keeps track of.

Deliberately small. A learner, the numbered sessions they work through, the
pieces they are studying, and the recordings that come out of it: that is the
whole model, and every adapter maps its own storage onto exactly this.

Each model keeps a ``raw`` dict of the source record. Studios have columns
Baton knows nothing about, and dropping them on the floor would make Baton a
lossy layer over the user's own data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Learner:
    """A person being taught."""

    id: str
    name: str
    instrument: str = ""
    tone: str = ""
    has_instrument: bool = False
    current_piece_id: str | None = None
    # Someone who stopped studying is still a person the studio taught: their
    # record stays, but matching and rosters read this flag.
    is_active: bool = True
    # Orthogonal to is_active. Set means trashed: fully out of the way, hidden
    # even from `learner list --all`, unlike an inactive learner. Nothing is
    # deleted; `learner untrash` clears this and nothing else changes.
    deleted_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "instrument": self.instrument,
            "tone": self.tone,
            "has_instrument": self.has_instrument,
            "current_piece_id": self.current_piece_id,
            "is_active": self.is_active,
            "deleted_at": self.deleted_at,
        }


@dataclass(frozen=True)
class Session:
    """One numbered session, and the document that records it.

    ``status`` and ``date`` are absent here on purpose: they live on the
    document, not in the database, and reading them means asking the
    :class:`~baton.adapters.docs.base.DocStore`. Copying them into the database
    is what let the two disagree in the original system.
    """

    id: str
    learner_id: str
    number: int
    doc_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "learner_id": self.learner_id,
            "number": self.number,
            "doc_id": self.doc_id,
        }


@dataclass(frozen=True)
class Piece:
    """Something being studied: a song, an étude, an exam piece."""

    id: str
    title: str
    source_link: str = ""
    practice_track: str = ""
    sheet_link: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "source_link": self.source_link,
            "practice_track": self.practice_track,
            "sheet_link": self.sheet_link,
        }


@dataclass(frozen=True)
class Work:
    """A finished performance or recording worth keeping."""

    id: str
    learner_id: str
    title: str
    type: str = "performance"
    video_link: str = ""
    #: A second home of the same recording: the Drive file beside the YouTube
    #: upload. Studios that keep only one link leave it empty.
    drive_link: str = ""
    performed_date: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "learner_id": self.learner_id,
            "title": self.title,
            "type": self.type,
            "video_link": self.video_link,
            "drive_link": self.drive_link,
            "performed_date": self.performed_date,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Work:
        """Read a work snapshot stored beside a published lesson."""

        return cls(
            id=str(data.get("id", "")),
            learner_id=str(data.get("learner_id", "")),
            title=str(data.get("title", "")),
            type=str(data.get("type", "performance")),
            video_link=str(data.get("video_link", "")),
            drive_link=str(data.get("drive_link", "")),
            performed_date=str(data.get("performed_date", "")),
        )


#: The seven weekday names a slot may carry, Monday first. These are the
#: words the Notion dashboard's day tags use, chosen over integers so the two
#: stores never need a translation table between them.
WEEKDAYS: tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def weekday_rank(weekday: str) -> int:
    """Where a weekday name sits in a Monday-first week.

    Raises:
        ValueError: The name is not one of the seven. Callers that accept
            user input validate first; this is the ordering key, not a parser.
    """
    return WEEKDAYS.index(weekday)


@dataclass(frozen=True)
class LessonSlot:
    """One recurring weekly hour: a learner, a weekday, a start time.

    The calendar holds booked lessons with real dates; this is the standing
    weekly pattern behind them. ``start`` is a 24-hour ``HH:MM`` string, kept
    as text on purpose: it names a time of day, never a moment in time, so a
    timezone would be a lie. ``duration_minutes`` is deliberately absent: a
    slot *is* one hour in this model, and the booking pipeline decides real
    event lengths when it creates them.
    """

    id: str
    learner_id: str
    weekday: str
    start: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "learner_id": self.learner_id,
            "weekday": self.weekday,
            "start": self.start,
        }
