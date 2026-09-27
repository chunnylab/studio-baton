"""In-memory adapters for tests.

Shipped inside the package rather than kept in ``tests/`` on purpose: they are
part of the contract. Anyone writing a driver can run their implementation
against the same expectations, and anyone extending Baton can test a pipeline
end to end without a Notion token or a network.

They are deliberately strict: a fake that is more forgiving than the real
thing produces tests that pass while production fails.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from ..domain.models import Learner, LessonSlot, Piece, Session, Work, weekday_rank
from ..errors import ConfigError, StateError, UsageError
from .db.base import LearnerStore
from .docs.base import (
    Block,
    BlockPosition,
    DocChild,
    DocPage,
    DocStatus,
    PreservePolicy,
    TableRow,
)


class FakeLearnerStore:
    """A :class:`LearnerStore` held entirely in memory."""

    driver = "fake"

    def __init__(
        self,
        learners: list[Learner] | None = None,
        sessions: list[Session] | None = None,
        pieces: list[Piece] | None = None,
        works: list[Work] | None = None,
        slots: list[LessonSlot] | None = None,
    ) -> None:
        self.learners = list(learners or [])
        self.sessions = list(sessions or [])
        self.pieces = list(pieces or [])
        self.works = list(works or [])
        self.slots = list(slots or [])
        self.closed = False
        #: Set to an exception to make every call raise: for testing the
        #: failover and error paths without an actual outage.
        self.fail_with: Exception | None = None
        self._ids = itertools.count(start=1000)

    def _check(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    # -- learners ----------------------------------------------------------

    def list_learners(self, include_trashed: bool = False) -> list[Learner]:
        self._check()
        pool = self.learners if include_trashed else [p for p in self.learners if not p.deleted_at]
        return sorted(pool, key=lambda item: item.name)

    def get_learner(self, learner_id: str) -> Learner | None:
        self._check()
        return next((item for item in self.learners if item.id == str(learner_id)), None)

    def set_current_piece(self, learner_id: str, piece_id: str | None) -> None:
        self._check()
        for index, learner in enumerate(self.learners):
            if learner.id == str(learner_id):
                # `replace`, not a hand-rebuilt Learner: a reconstruction that
                # names every field silently drops the next one added (exactly
                # what `is_active` would have suffered).
                self.learners[index] = replace(learner, current_piece_id=piece_id)
                return
        # The same refusal the real stores make: a write that matched no row
        # is not a success, and `learner assign` printed "is now working on"
        # over one for as long as this was silent anywhere.
        raise StateError(
            f"No learner with id {learner_id}; the assignment was not written.",
            remedy="Re-read the learner (`baton learner list`) and try again.",
        )

    def set_active(self, learner_id: str, active: bool) -> None:
        self._check()
        for index, learner in enumerate(self.learners):
            if learner.id == str(learner_id):
                self.learners[index] = replace(learner, is_active=active)
                return
        raise StateError(
            f"No learner with id {learner_id}; the status was not written.",
            remedy="Re-read the learner (`baton learner list`) and try again.",
        )

    def trash_learner(self, learner_id: str) -> None:
        self._check()
        for index, learner in enumerate(self.learners):
            if learner.id == str(learner_id):
                self.learners[index] = replace(learner, deleted_at="2026-01-01T00:00:00+00:00")
                return
        raise StateError(
            f"No learner with id {learner_id}; nothing was trashed.",
            remedy="Re-read the learner (`baton learner list`) and try again.",
        )

    def untrash_learner(self, learner_id: str) -> None:
        self._check()
        for index, learner in enumerate(self.learners):
            if learner.id == str(learner_id):
                self.learners[index] = replace(learner, deleted_at=None)
                return
        raise StateError(
            f"No learner with id {learner_id}; nothing was untrashed.",
            remedy="Re-read the learner (`baton learner list`) and try again.",
        )

    def rename_learner(self, learner_id: str, name: str) -> None:
        self._check()
        for index, learner in enumerate(self.learners):
            if learner.id == str(learner_id):
                self.learners[index] = replace(learner, name=name)
                return
        raise StateError(
            f"No learner with id {learner_id}; the name was not written.",
            remedy="Re-read the learner (`baton learner list`) and try again.",
        )

    def update_learner(self, learner_id: str, fields: Mapping[str, Any]) -> None:
        self._check()
        for index, learner in enumerate(self.learners):
            if learner.id == str(learner_id):
                self.learners[index] = replace(learner, **dict(fields))
                return
        raise StateError(
            f"No learner with id {learner_id}; nothing was written.",
            remedy="Re-read the learner (`baton learner list`) and try again.",
        )

    def add_learner(self, learner: Learner, extra: Mapping[str, Any] | None = None) -> Learner:
        self._check()
        created = Learner(
            id=str(next(self._ids)),
            name=learner.name,
            instrument=learner.instrument,
            tone=learner.tone,
            has_instrument=learner.has_instrument,
            raw=dict(extra or {}),
        )
        self.learners.append(created)
        return created

    # -- sessions ----------------------------------------------------------

    def list_sessions(self, learner_id: str) -> list[Session]:
        self._check()
        found = [item for item in self.sessions if item.learner_id == str(learner_id)]
        return sorted(found, key=lambda item: item.number)

    def get_session(self, learner_id: str, number: int) -> Session | None:
        self._check()
        return next(
            (
                item
                for item in self.sessions
                if item.learner_id == str(learner_id) and item.number == int(number)
            ),
            None,
        )

    def add_session(self, session: Session, extra: Mapping[str, Any] | None = None) -> Session:
        self._check()
        created = Session(
            id=str(next(self._ids)),
            learner_id=session.learner_id,
            number=session.number,
            doc_id=session.doc_id,
            raw=dict(extra or {}),
        )
        self.sessions.append(created)
        return created

    # -- pieces ------------------------------------------------------------

    def list_pieces(self) -> list[Piece]:
        self._check()
        return sorted(self.pieces, key=lambda item: item.title)

    def get_piece(self, piece_id: str) -> Piece | None:
        self._check()
        return next((item for item in self.pieces if item.id == str(piece_id)), None)

    def add_piece(self, piece: Piece) -> Piece:
        self._check()
        created = Piece(
            id=str(next(self._ids)),
            title=piece.title,
            source_link=piece.source_link,
            practice_track=piece.practice_track,
            sheet_link=piece.sheet_link,
        )
        self.pieces.append(created)
        return created

    def update_piece(self, piece_id: str, changes: Mapping[str, str]) -> Piece | None:
        self._check()
        editable = ("title", "source_link", "practice_track", "sheet_link")
        for index, piece in enumerate(self.pieces):
            if piece.id != str(piece_id):
                continue
            fields: dict[str, Any] = {name: changes[name] for name in editable if name in changes}
            updated = replace(piece, **fields)
            self.pieces[index] = updated
            return updated
        return None

    def delete_piece(self, piece_id: str) -> bool:
        self._check()
        before = len(self.pieces)
        self.pieces = [item for item in self.pieces if item.id != str(piece_id)]
        return len(self.pieces) < before

    # -- works -------------------------------------------------------------

    def list_works(self, learner_id: str) -> list[Work]:
        self._check()
        found = [item for item in self.works if item.learner_id == str(learner_id)]
        return sorted(found, key=lambda item: item.performed_date, reverse=True)

    def add_work(self, work: Work) -> Work:
        self._check()
        created = Work(
            id=str(next(self._ids)),
            learner_id=work.learner_id,
            title=work.title,
            type=work.type,
            video_link=work.video_link,
            drive_link=work.drive_link,
            performed_date=work.performed_date,
        )
        self.works.append(created)
        return created

    # -- lesson slots --------------------------------------------------------

    def list_slots(self, learner_id: str | None = None) -> list[LessonSlot]:
        self._check()
        if learner_id is None:
            found = list(self.slots)
        else:
            found = [item for item in self.slots if item.learner_id == str(learner_id)]
        return sorted(found, key=lambda item: (weekday_rank(item.weekday), item.start))

    def set_slots(self, learner_id: str, slots: Sequence[tuple[str, str]]) -> list[LessonSlot]:
        self._check()
        seen: set[tuple[str, str]] = set()
        for weekday, start in slots:
            if (weekday, start) in seen:
                raise UsageError(
                    f"The slot {weekday} {start} appears twice in one request.",
                    remedy="Send each hour once; a longer lesson is two consecutive slots.",
                )
            seen.add((weekday, start))
        self.slots = [item for item in self.slots if item.learner_id != str(learner_id)]
        created = [
            LessonSlot(
                id=str(next(self._ids)),
                learner_id=str(learner_id),
                weekday=weekday,
                start=start,
            )
            for weekday, start in slots
        ]
        self.slots.extend(created)
        return self.list_slots(str(learner_id))

    # -- lifecycle ---------------------------------------------------------

    def health(self) -> None:
        self._check()

    def close(self) -> None:
        self.closed = True


class FakeDocStore:
    """A :class:`~baton.adapters.docs.base.DocStore` held in memory.

    Enforces the two real constraints that bite in production: appending more
    than 100 children at once is rejected, and deleting an unknown block id
    fails rather than silently succeeding.
    """

    driver = "fake"
    MAX_CHILDREN_PER_REQUEST = 100

    def __init__(
        self,
        statuses: dict[str, DocStatus] | None = None,
        blocks: dict[str, list[Block]] | None = None,
        preserve: PreservePolicy | None = None,
        pages: dict[str, DocPage] | None = None,
        children: dict[str, list[DocChild]] | None = None,
        tables: dict[str, list[TableRow]] | None = None,
        wording: dict[str, str] | None = None,
    ) -> None:
        self.statuses = dict(statuses or {})
        self.wording = dict(wording or {})
        """The profile's own status words, as the real adapter resolves them.
        Left empty, a canonical key is written through unchanged."""
        self.blocks = {key: list(value) for key, value in (blocks or {}).items()}
        self.preserve = preserve or PreservePolicy(rules=())
        self.appended: list[dict[str, Any]] = []
        self.created_pages: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None
        self._ids = itertools.count(start=1)
        # Filing: which page holds what, and what each table contains.
        self.pages = dict(pages or {})
        self.children = {key: list(value) for key, value in (children or {}).items()}
        self.tables = {key: list(value) for key, value in (tables or {}).items()}
        self.reset_calls: list[str] = []
        self.trashed: set[str] = set()
        self.fail_on_properties = False
        """Fail property writes only. Appending a summary and finishing the
        session are separate requests, and only the second one is retryable."""
        self.deleted_missing: list[str] = []
        """Ids the last `delete_blocks` was asked for that were already gone."""

    # -- filing ------------------------------------------------------------

    def get_page(self, doc_id: str) -> DocPage:
        self._check()
        page = self.pages.get(doc_id)
        if page is None:
            return DocPage(doc_id=doc_id, title="", parent_id="", parent_kind="")
        return DocPage(
            doc_id=page.doc_id,
            title=page.title,
            parent_id=page.parent_id,
            parent_kind=page.parent_kind,
            trashed=doc_id in self.trashed or page.trashed,
            url=page.url,
        )

    def list_children(self, doc_id: str) -> list[DocChild]:
        self._check()
        return list(self.children.get(doc_id, []))

    def get_table(self, table_id: str) -> DocPage:
        self._check()
        return self.get_page(table_id)

    def table_rows(self, table_id: str) -> list[TableRow]:
        self._check()
        return list(self.tables.get(table_id, []))

    def reset_properties(self, doc_id: str) -> list[str]:
        self._check()
        self.reset_calls.append(doc_id)
        current = self.statuses.get(doc_id)
        if current is not None:
            self.statuses[doc_id] = DocStatus(doc_id=doc_id, url=current.url)
        return ["date", "status", "titles"]

    def clear_properties(self, doc_id: str, keys: list[str]) -> list[str]:
        self._check()
        if self.fail_on_properties:
            from ..errors import UpstreamError

            raise UpstreamError("notion rejected the property write", service="notion")
        current = self.get_status(doc_id)
        cleared = sorted(key for key in keys if key in ("status", "date", "titles"))
        self.statuses[doc_id] = DocStatus(
            doc_id=doc_id,
            status="" if "status" in cleared else current.status,
            date="" if "date" in cleared else current.date,
            titles="" if "titles" in cleared else current.titles,
            url=current.url,
        )
        return cleared

    def restore(self, doc_id: str) -> bool:
        self._check()
        self.trashed.discard(doc_id)
        return True

    def _check(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def get_status(self, doc_id: str, *, with_blocks: bool = True) -> DocStatus:
        self._check()
        current = self.statuses.get(doc_id, DocStatus(doc_id=doc_id))
        return DocStatus(
            doc_id=current.doc_id or doc_id,
            status=current.status,
            date=current.date,
            titles=current.titles,
            # `None` when declined, exactly as the real store answers: a fake
            # that counted anyway would hide a caller reading a count it never
            # asked for.
            block_count=len(self.blocks.get(doc_id, [])) if with_blocks else None,
            url=current.url,
        )

    def set_status(self, doc_id: str, status: str) -> None:
        self._check()
        self.set_properties(doc_id, {"status": status})

    def set_properties(self, doc_id: str, values: dict[str, str]) -> list[str]:
        self._check()
        if self.fail_on_properties:
            from ..errors import UpstreamError

            raise UpstreamError("notion rejected the property write", service="notion")
        wanted = {key: str(value) for key, value in values.items() if str(value)}
        if not wanted:
            return []
        if "status" in wanted:
            wanted["status"] = self.wording.get(wanted["status"], wanted["status"])
        current = self.get_status(doc_id)
        self.statuses[doc_id] = DocStatus(
            doc_id=doc_id,
            status=wanted.get("status", current.status),
            date=wanted.get("date", current.date),
            titles=wanted.get("titles", current.titles),
            url=current.url,
        )
        return sorted(wanted)

    def list_blocks(self, doc_id: str) -> list[Block]:
        self._check()
        return list(self.blocks.get(doc_id, []))

    def append_blocks(
        self,
        doc_id: str,
        blocks: list[dict[str, Any]],
        *,
        position: BlockPosition = "end",
    ) -> None:
        """Add blocks, splitting into request-sized batches like the real store.

        The per-request ceiling is a transport detail the adapter hides, so the
        fake hides it too: a fake that refuses what production accepts sends
        tests chasing a limit that is not really there. Each batch is recorded
        in `appended`, so a test can still assert that the split happened.

        ``"start"`` puts the blocks above what is already there, in the order
        they were given. The real store reverses its chunks to manage that; the
        fake does one list insert, so a test that passes here would still catch
        a chunking mistake there through `list_blocks`.
        """
        self._check()
        for start in range(0, len(blocks), self.MAX_CHILDREN_PER_REQUEST):
            self.appended.append(
                {
                    "doc_id": doc_id,
                    "count": len(blocks[start : start + self.MAX_CHILDREN_PER_REQUEST]),
                }
            )
        created = [
            Block(
                id=f"new-{next(self._ids)}",
                type=str(block.get("type", "paragraph")),
                text=_plain_text(block),
                url=_block_url(block),
                raw=block,
            )
            for block in blocks
        ]
        existing = self.blocks.setdefault(doc_id, [])
        if position == "start":
            existing[:0] = created
        else:
            existing.extend(created)

    def create_page(self, parent_id: str, title: str, blocks: list[dict[str, Any]]) -> DocStatus:
        self._check()
        page_id = f"page-{next(self._ids)}"
        self.created_pages.append({"parent_id": parent_id, "title": title, "id": page_id})
        self.blocks[page_id] = []
        if blocks:
            self.append_blocks(page_id, blocks)
        status = DocStatus(
            doc_id=page_id,
            titles=title,
            block_count=len(blocks),
            url=f"https://example.invalid/{page_id}",
        )
        self.statuses[page_id] = status
        return status

    def delete_blocks(self, block_ids: list[str]) -> int:
        """Delete, tolerating ids that are already gone: as Notion does.

        The real store treats a 404 on a block delete as the outcome the
        delete wanted: a publish that died mid-delete, or a person who removed
        the block by hand, must not wedge the re-run. This fake used to raise
        instead, which meant a resume path could pass every test here and
        still be the one production behaviour nobody had exercised.

        Unknown ids are recorded in `deleted_missing` so a test can assert the
        tolerance rather than merely benefit from it.
        """
        self._check()
        known = {block.id for blocks in self.blocks.values() for block in blocks}
        self.deleted_missing = [block_id for block_id in block_ids if block_id not in known]
        removing = set(block_ids)
        for doc_id, blocks in self.blocks.items():
            self.blocks[doc_id] = [block for block in blocks if block.id not in removing]
        return len(removing)

    def health(self) -> None:
        self._check()


def _plain_text(block: dict[str, Any]) -> str:
    """Pull text out of a Notion-shaped block payload."""
    kind = str(block.get("type", ""))
    body = block.get(kind, {}) or {}
    return "".join(part.get("text", {}).get("content", "") for part in body.get("rich_text", []))


def _block_url(block: dict[str, Any]) -> str:
    """Pull the URL out of a link-bearing block payload.

    The real store keeps this, so the fake must too: a fake that quietly drops
    a field makes a test fail for a reason production never would.
    """
    kind = str(block.get("type", ""))
    body = block.get(kind, {}) or {}
    if not isinstance(body, dict):
        return ""
    source = body.get("external") or body.get("file") or {}
    if isinstance(source, dict) and source.get("url"):
        return str(source["url"])
    return str(body.get("url", ""))


class FakeMediaSource:
    """Clips held in memory, with real files written on download.

    Records every trash call, because *when* the source is discarded is the
    property the video pipeline's tests exist to pin down.
    """

    driver = "fake"

    def __init__(self, clips: list[Any] | None = None) -> None:
        self.clips = list(clips or [])
        self.downloaded: list[str] = []
        self.trashed: list[str] = []
        self.fail_with: Exception | None = None

    def _check(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def list_pending(self) -> list[Any]:
        self._check()
        return list(self.clips)

    def download(self, clip: Any, destination: Any) -> Any:
        self._check()
        from pathlib import Path

        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-video-bytes")
        self.downloaded.append(clip.id)
        return path

    def trash(self, clip_ids: list[str]) -> list[Any]:
        self._check()
        from .media.base import TRASHED, TrashOutcome

        self.trashed.extend(clip_ids)
        return [TrashOutcome(clip_id, TRASHED) for clip_id in clip_ids]

    def health(self) -> None:
        self._check()


class FakeEncoder:
    """Writes a placeholder output file, atomically enough for tests."""

    driver = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.fail_with: Exception | None = None

    def combine(self, inputs: list[Any], output: Any, profile: Any) -> Any:
        if self.fail_with is not None:
            raise self.fail_with
        # The real encoder refuses an empty input list; a fake that accepted
        # one let a resume with zero clips complete and mark itself done.
        if not inputs:
            raise ConfigError("Cannot combine an empty list of clips.")
        from pathlib import Path

        from .media.base import CombineResult

        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"combined")
        self.calls.append(([str(i) for i in inputs], str(output)))
        return CombineResult(path, "fake")

    def health(self) -> None:
        pass


class FakePublisher:
    """Hands out sequential video ids and counts uploads.

    The upload count is what proves a resumed run does not publish a second
    copy of a child's lesson.
    """

    driver = "fake"

    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None
        self._ids = itertools.count(start=1)
        #: video ids the fake pretends belong to some other channel: for
        #: exercising the ownership refusal without a real API call.
        self.foreign_video_ids: set[str] = set()
        self.descriptions: dict[str, str] = {}

    def upload(
        self, path: Any, *, title: str = "", description: str = "", privacy: str = "unlisted"
    ) -> Any:
        if self.fail_with is not None:
            raise self.fail_with
        from .media.base import UploadResult

        video_id = f"vid{next(self._ids)}"
        self.uploads.append({"path": str(path), "title": title, "privacy": privacy})
        self.descriptions[video_id] = description
        return UploadResult(video_id=video_id, url=f"https://youtu.be/{video_id}", title=title)

    def update_description(self, video_id: str, description: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        if video_id in self.foreign_video_ids:
            from ..errors import UpstreamError

            raise UpstreamError(f"Video {video_id} belongs to another channel.", service="youtube")
        self.descriptions[video_id] = description

    def owns(self, video_id: str) -> bool:
        return bool(video_id) and video_id not in self.foreign_video_ids

    def health(self) -> None:
        pass


class FakeCalendar:
    """Events in memory, with ids handed out in order."""

    driver = "fake"

    def __init__(self, events: list[Any] | None = None) -> None:
        self.events: list[Any] = list(events or [])
        self.deleted: list[str] = []
        #: Standing series keyed by event id: the spec each one was built
        #: from, so `list_standing` can filter by learner without pretending
        #: CalendarEvent carries fields it does not have.
        self.standing_specs: dict[str, Any] = {}
        self.fail_with: Exception | None = None
        self._ids = itertools.count(start=1)

    def _check(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def create(self, event: Any) -> Any:
        self._check()
        from .cal.base import CalendarEvent

        created = CalendarEvent(
            id=f"ev{next(self._ids)}",
            title=event.title,
            start=event.start,
            end=event.end,
            description=event.description,
        )
        self.events.append(created)
        return created

    def list_between(self, start: str, end: str) -> list[Any]:
        self._check()
        # Standing series are not bookings: the real driver keeps them out of
        # range answers and so does the fake, or a pipeline tested here would
        # behave differently in production.
        found = [
            event
            for event in self.events
            if start <= event.start < end and event.id not in self.standing_specs
        ]
        return sorted(found, key=lambda event: event.start)

    def delete(self, event_id: str) -> None:
        self._check()
        # Already gone is the desired state, matching the real driver.
        self.events = [event for event in self.events if event.id != event_id]
        self.standing_specs.pop(event_id, None)
        self.deleted.append(event_id)

    def list_standing(self, learner_id: str | None = None) -> list[Any]:
        self._check()
        wanted = [
            event
            for event in self.events
            if event.id in self.standing_specs
            and (learner_id is None or self.standing_specs[event.id].learner_id == str(learner_id))
        ]
        return sorted(wanted, key=lambda event: (event.title, event.start))

    def create_standing(self, spec: Any, *, first_date: str) -> Any:
        self._check()
        from .cal.base import CalendarEvent

        created = CalendarEvent(
            id=f"st{next(self._ids)}",
            title=spec.title,
            start=f"{first_date}T{spec.start}:00",
            end=f"{first_date}T{spec.end}:00",
            description=spec.description,
        )
        self.events.append(created)
        self.standing_specs[created.id] = spec
        return created

    def health(self) -> None:
        self._check()


__all__ = [
    "FakeCalendar",
    "FakeDocStore",
    "FakeEncoder",
    "FakeLearnerStore",
    "FakeMediaSource",
    "FakePublisher",
    "LearnerStore",
]
