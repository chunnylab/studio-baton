"""Offering a learner's recorded works, and sending the chosen links.

A studio records its learners: YouTube for sharing, Drive beside it for the
copy parents keep. Both live on the ``works`` row, and a parent asking "วิดีโอ
ครั้งก่อนหน่อยครับ" means one specific recording, not whatever came out last.
So sending is two steps and never guesses: list what exists, let a person pick
one by number, then deliver exactly that one's links.

The first step ends with :class:`~baton.errors.NeedsHumanError` because "which
recording?" genuinely is a person's decision: the database orders by date,
but the teacher knows whether the parent wanted Week 3 or the recital cut.
That refusal carries the whole candidate list, so whoever drives Baton can
relay it and answer with ``--pick N`` without anything being remembered.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from ..adapters.chat.base import Messenger
from ..adapters.docs import open_docs
from ..adapters.docs.base import PreservePolicy
from ..core.config import Config
from ..domain.localdate import DateFormat
from ..domain.models import Learner, Work
from ..domain.whenever import today_in
from ..errors import BatonError, GateError, StateError, UsageError
from .learner import SessionView
from .publish import SummaryPublisher
from .send import _instrument_icon
from .staging import PUBLISHED, RECORDING, LessonDraft, PieceSnapshot, PublishedRecord, StagingStore


def list_candidates(works: list[Work]) -> list[dict[str, Any]]:
    """Recorded works as numbered choices, newest first.

    The already-fetched list is passed in rather than queried here, so the
    list a person reads is byte-for-byte the one ``--pick N`` indexes: one
    read, one order, and no window where the store changes between listing
    and sending. Order stands as :meth:`LearnerStore.list_works` returned it:
    ``performed_date`` descending where the profile maps one, otherwise id
    descending.
    """
    return [
        {
            "n": index,
            "id": work.id,
            "name": work.title,
            "type": work.type,
            "performed_date": work.performed_date,
            "video_link": work.video_link,
            "drive_link": work.drive_link,
        }
        for index, work in enumerate(works, start=1)
    ]


#: The two homes a recording may have, with the label each gets in a message.
#: One missing is ordinary: some sessions were filmed once; that side simply
#: does not appear.
_LINK_LABELS = (
    ("video_link", "📹 YouTube:"),
    ("drive_link", "📁 Drive:"),
)


def compose_recording(
    work: Work,
    *,
    learner_name: str = "",
    instrument: str = "",
    date: str | None = None,
    doc_url: str = "",
) -> str:
    """The message families receive for one recording.

    Deterministic by design, unlike :func:`baton.pipelines.send.compose_message`
    which varies its opening and closing phrases: a links-only message sent a
    second time (say, a parent lost it) reads the same rather than sounding
    freshly cheerful about repeating oneself.

    Raises:
        GateError: When neither home holds a link: a message announcing a
            recording with none in it is worse than no message, so the block
            is fail-closed like every other send gate.
    """
    present = [
        (label, getattr(work, name)) for name, label in _LINK_LABELS if getattr(work, name).strip()
    ]
    if not present:
        raise GateError(
            f"The work “{work.title}” has no recording link to send.",
            missing=[{"field": name, "reason": f"`{name}` is empty"} for name, _ in _LINK_LABELS],
            remedy="Record the links on the work: `baton learner add-work "
            "--video-link/--drive-link` writes a new one; the existing row can "
            "be edited where it lives.",
        )

    icon = _instrument_icon(instrument) if instrument else "🎵"
    header = f"{icon} ผลงานบันทึกการเรียน"
    if learner_name:
        header += f"ของ {learner_name}"

    parts = [work.title]
    if work.type:
        parts.append(f"({work.type})")
    performed = work.performed_date if date is None else date
    if performed:
        parts.append(performed)
    body = "".join(
        [
            f"\n\n📌 {' '.join(parts)}",
            *(f"\n\n{label}\n{value}" for label, value in present),
        ]
    )
    if doc_url:
        # Without this line the message is a dead end: links to the recording,
        # nothing saying which lesson it came from. Fail-open by design: the
        # old sender attached it when it could and sent without it when it
        # could not, and a link that cannot be found must not block the links
        # that exist.
        body += f"\n\n📝 รายละเอียดการเรียน: {doc_url}"
    return header + body


def send_recording(
    messenger: Messenger,
    *,
    recipient_id: str,
    work: Work,
    learner_name: str,
    instrument: str = "",
    date: str | None = None,
    doc_url: str = "",
    dry_run: bool = False,
    message: str = "",
) -> dict[str, Any]:
    """Compose the links message and (unless ``dry_run``) deliver it.

    Args:
        date: The performed date already written the studio's way. ``None``
            uses the work's own value, so a caller with no date configuration
            gets exactly what the record holds.
        doc_url: The lesson page the recording belongs to, appended when given.
            Empty omits the line: it is never a gate.
        message: A stored recording message to send verbatim. Empty composes it
            from the work, which is the path `send recording` uses.
    """
    message = message or compose_recording(
        work, learner_name=learner_name, instrument=instrument, date=date, doc_url=doc_url
    )
    if dry_run:
        return {
            "dry_run": True,
            "learner": learner_name,
            "recipient": recipient_id,
            "message": message,
            "sent": False,
        }

    outcome = messenger.send(recipient_id, message)
    return {
        "learner": learner_name,
        "recipient": recipient_id,
        "message": message,
        **outcome.to_dict(),
    }


#: The heading a work's recording is filed under on a session page, the same
#: words the studio's old push wrote: a family looking for the clip finds it
#: under the same heading it has always been under.
_RECORDS_HEADING = "🎬 ผลงาน Record"


def recording_blocks(work: Work, *, message: str = "") -> list[dict[str, Any]]:
    """The section one work's recording becomes on a session page.

    With ``message``, each line of the exact family message is written under
    the heading, followed by the YouTube and Drive sides as native blocks.
    Without it, the older attach-only shape remains: a heading, a bold title,
    and whichever sides the row has.
    """
    text_blocks: list[dict[str, Any]]
    if message:
        text_blocks = [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]},
            }
            for line in message.splitlines()
            if line.strip()
        ]
    else:
        text_blocks = [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": "📌 "}},
                        {
                            "type": "text",
                            "text": {"content": work.title},
                            "annotations": {"bold": True},
                        },
                    ]
                },
            }
        ]

    blocks: list[dict[str, Any]] = [
        {
            "object": "block",
            "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": _RECORDS_HEADING}}]},
        },
        *text_blocks,
    ]
    if work.video_link:
        blocks.append(
            {
                "object": "block",
                "type": "video",
                "video": {"type": "external", "external": {"url": work.video_link}},
            }
        )
    if work.drive_link:
        blocks.append({"object": "block", "type": "bookmark", "bookmark": {"url": work.drive_link}})
    return blocks


def _url_of(block: dict[str, Any]) -> str:
    """The URL a link block carries, whatever shape it was built in."""
    body = block.get(block.get("type", ""), {})
    if isinstance(body, dict) and "external" in body:
        return str(body["external"].get("url", ""))
    return str(body.get("url", "")) if isinstance(body, dict) else ""


def _block_entry(block: Any) -> dict[str, Any]:
    return {"id": block.id, "type": block.type, "text": block.text}


def _already_for(work: Work, on_page: set[str]) -> list[str]:
    return [link for link in (work.video_link, work.drive_link) if link and link in on_page]


def _section_is_filed(blocks: list[Any]) -> bool:
    return any(
        block.type.startswith("heading") and block.text == _RECORDS_HEADING for block in blocks
    )


def _existing_recording_section(
    blocks: list[Any], work: Work, *, message: str
) -> list[dict[str, Any]]:
    """The already-filed section a retry can claim, if it is complete enough.

    Bare links are not enough: the video pipeline can put the same URL on the
    page without this work's heading and title. Claiming that section would
    make `lesson unpublish` remove something Baton did not own.
    """

    expected_entries: list[dict[str, Any]] = [{"type": "heading_3", "text": _RECORDS_HEADING}]
    if message:
        expected_entries.extend(
            {"type": "paragraph", "text": line} for line in message.splitlines() if line.strip()
        )
    else:
        expected_entries.append({"type": "paragraph", "text": f"📌 {work.title}"})
    if work.video_link:
        expected_entries.append({"type": "video", "text": ""})
    if work.drive_link:
        expected_entries.append({"type": "bookmark", "text": ""})

    for index, block in enumerate(blocks):
        if block.type.startswith("heading") and block.text == _RECORDS_HEADING:
            entries = [_block_entry(block)]
            urls: set[str] = set()
            for candidate in blocks[index + 1 :]:
                if candidate.type.startswith("heading"):
                    break
                entries.append(_block_entry(candidate))
                if candidate.url:
                    urls.add(candidate.url)
            actual_shape = [{"type": entry["type"], "text": entry["text"]} for entry in entries]
            if actual_shape == expected_entries and urls == {work.video_link, work.drive_link} - {
                ""
            }:
                return entries
    return []


def check_recording_draft(
    config: Config, learner: Learner, view: SessionView, work_id: str | None = None
) -> LessonDraft | None:
    """The draft a recording lesson may continue, or None to start a fresh one.

    A finished lesson's draft is replaced, as `lesson stage` replaces it:
    every learner with a summary last week still has that published draft
    on disk. Any other lesson's draft is work still owed and is refused.
    ``work_id`` None means a work not yet written, so a half-finished
    recording for this session is refused too, rather than given a second
    work. Callers run this before writing the work, so a refusal records
    nothing.
    """
    staging = StagingStore(config.state_dir / "lessons")
    draft = staging.get(learner.id)
    if draft is None:
        return None
    same = draft.kind == RECORDING and draft.session_number == view.session.number
    if not same and draft.status == PUBLISHED:
        return None
    if not same:
        raise UsageError(
            f"{learner.name} already has a {draft.kind} draft for {draft.session_number}.",
            remedy=f'Publish or remove it first (`baton lesson publish "{learner.name}"`, '
            f'or `baton lesson remove "{learner.name}"` after checking it).',
        )
    if draft.work_id and draft.work_id != (work_id or ""):
        raise StateError(
            f"A recording draft for {learner.name} already names work `{draft.work_id}`.",
            remedy=f"Recover that work, or remove the draft after checking `{draft.work_id}`.",
        )
    return draft


def _recording_draft(
    config: Config, learner: Learner, view: SessionView, work: Work
) -> LessonDraft:
    """Create or recover the one staging draft for this recording lesson."""

    staging = StagingStore(config.state_dir / "lessons")
    draft = check_recording_draft(config, learner, view, work.id)
    if draft is None:
        draft = LessonDraft(
            learner_id=learner.id,
            learner_name=learner.name,
            session_number=view.session.number,
            piece_snapshot=PieceSnapshot.capture(None),
            doc_id=view.session.doc_id,
            titles=work.title,
        )
    draft.kind = RECORDING
    draft.work_id = work.id
    draft.work = work
    draft.summary = None
    staging.save(draft)
    return draft


def complete_recording_work(
    config: Config, learner: Learner, view: SessionView, work: Work
) -> dict[str, Any]:
    """Write a work to its session page and make the lesson sendable."""

    staging = StagingStore(config.state_dir / "lessons")
    draft = _recording_draft(config, learner, view, work)
    docs = open_docs(config)
    try:
        status_before = docs.get_status(view.session.doc_id, with_blocks=False)
        message = compose_recording(
            work,
            learner_name=learner.name,
            instrument=learner.instrument,
            date=DateFormat.from_config(config.section("chat.date")).of_text(work.performed_date),
            doc_url=status_before.url,
        )
        attached = attach_work(docs, view.session.doc_id, work, message=message)
    except Exception as exc:
        draft.record_target("docs", "failed", error=str(exc))
        staging.save(draft)
        raise

    previous_blocks = draft.targets.get("docs", {}).get("blocks", [])
    draft.record_target(
        "docs",
        "ok",
        appended=attached["appended"],
        blocks=attached.get("blocks") or previous_blocks,
    )
    staging.save(draft)

    publisher = SummaryPublisher(
        docs,
        PreservePolicy.from_config(config.get("docs.preserve", [])),
    )
    completed = publisher.complete(
        view.session.doc_id,
        date=work.performed_date or today_in(config.timezone).isoformat(),
        titles=work.title,
    )
    draft.note_target("docs", completed=completed)
    staging.save(draft)

    status = docs.get_status(view.session.doc_id, with_blocks=False)
    recorded_blocks = attached.get("blocks") or previous_blocks
    draft.status = PUBLISHED
    staging.save(draft)
    PublishedRecord(config.state_dir / "published").save(
        draft, short_message=message, doc_url=status.url, blocks=recorded_blocks
    )
    return {
        "learner": learner.to_dict(),
        "work": work.to_dict(),
        "doc_id": view.session.doc_id,
        "doc_url": status.url,
        "appended": attached["appended"],
        "already_on_page": attached["already_on_page"],
        "blocks": recorded_blocks,
        "completed": completed,
        "kind": RECORDING,
        "message": message,
        "published": True,
    }


def attach_work(docs: Any, doc_id: str, work: Work, *, message: str = "") -> dict[str, Any]:
    """Put a recorded work onto a session page, without duplicating it.

    The old script cleared every video and bookmark on the page before
    writing. That rule dates from when it was the only writer; today the
    video pipeline puts the lesson's own recording on the same page, and
    clearing all video blocks would take that off with it. The guard is the
    URL instead: a link already on the page is not written twice, and
    anything else on the page is not Baton's to remove.

    Returns what was appended, and the sides that were already there.
    """
    if not work.video_link and not work.drive_link:
        raise GateError(
            f"The work “{work.title}” has no link to put on the page.",
            missing=[{"field": name, "reason": f"`{name}` is empty"} for name, _ in _LINK_LABELS],
            remedy="Record the link on the work first: `baton learner add-work` "
            "writes a new one; the existing row can be edited where it lives.",
        )

    before = docs.list_blocks(doc_id)
    on_page = {block.url for block in before if block.url}
    existing = _existing_recording_section(before, work, message=message)

    # A complete retry claims the section it can recognise in full. A heading
    # that no longer matches stops the publish rather than growing a second
    # copy of the same message underneath an edited one.
    if existing:
        return {
            "doc_id": doc_id,
            "appended": 0,
            "already_on_page": _already_for(work, on_page),
            "blocks": existing,
        }
    if message and _section_is_filed(before):
        raise StateError(f"The recording section for “{work.title}” on {doc_id} no longer matches.")

    # The link blocks carry the URLs; the heading and the title do not, so
    # they are added as a pair only when at least one link is going on. A
    # section heading with no link under it would be a label pointing at
    # nothing, and re-adding the pair when both links are already there is
    # the duplication this exists to prevent.
    wanted = recording_blocks(work, message=message)
    links = [block for block in wanted if _url_of(block)]
    fresh_links = [block for block in links if _url_of(block) not in on_page]
    already = _already_for(work, on_page)
    if not fresh_links and not message:
        return {
            "doc_id": doc_id,
            "appended": 0,
            "already_on_page": already,
            "blocks": [],
        }

    text_blocks = [block for block in wanted if _url_of(block) == ""]
    blocks = [*text_blocks, *fresh_links]
    docs.append_blocks(doc_id, blocks)
    added: list[dict[str, Any]] = []
    with suppress(BatonError):
        known = {block.id for block in before}
        added = [_block_entry(block) for block in docs.list_blocks(doc_id) if block.id not in known]
    return {
        "doc_id": doc_id,
        "appended": len(blocks),
        "already_on_page": already,
        "blocks": added,
    }
