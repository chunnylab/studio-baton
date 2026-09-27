"""``baton learner``: look up people, their sessions, pieces, and work.

Every subcommand that takes a name runs it through the resolution gate first,
so an ambiguous name exits ``3`` with candidates rather than acting on a guess.
"""

from __future__ import annotations

import argparse
import re
from typing import TYPE_CHECKING, Any

from ..adapters.db import store_scope
from ..adapters.docs import VIDEO_LINK_BLOCKS, find_video_link, open_docs, recording_in
from ..adapters.docs.base import PreservePolicy
from ..domain.localdate import DateFormat
from ..domain.models import WEEKDAYS, Learner, Session, Work
from ..domain.notion_urls import detect_week, parse_page_id
from ..domain.prep import SectionRules
from ..domain.resolve import normalise, resolve_learner
from ..domain.status import StatusVocabulary
from ..domain.whenever import today_in
from ..errors import BatonError, ConfigError, GateError, NeedsHumanError, UpstreamError, UsageError
from ..exits import Exit
from ..pipelines.learner import LearnerHistory, PublishedPieceUpdater, SessionView
from ..pipelines.recording import (
    attach_work,
    complete_recording_work,
    compose_recording,
    list_candidates,
    recording_blocks,
)
from ..pipelines.staging import PublishedRecord
from .cmd_calendar import _scheduler
from .naming import warn_if_inactive

if TYPE_CHECKING:
    from .app import Context

_STATE_MARK = {"done": "✓", "in_progress": "▶", "not_started": "·", "": "?"}


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``learner`` command group."""
    parser = subparsers.add_parser(
        "learner",
        help="Enrol a learner, then look up sessions, pieces, and recorded work.",
        description="Read-mostly queries across the database and the session documents.",
    )
    group = parser.add_subparsers(dest="learner_command", metavar="<subcommand>")
    parser.set_defaults(handler=_require_subcommand)

    listing = group.add_parser(
        "list",
        help="List the learners still studying.",
        description=(
            "Active learners only by default; --all includes everyone who "
            "stopped, each marked (inactive). Nothing is ever deleted, so "
            "--all is also the honest full roster. --trashed shows only "
            "learners `learner trash` moved out of the way."
        ),
    )
    listing.add_argument(
        "--all", action="store_true", help="Include learners who stopped studying."
    )
    listing.add_argument(
        "--trashed", action="store_true", help="Show only trashed learners, instead."
    )
    listing.set_defaults(handler=handle_list)

    show = group.add_parser(
        "show",
        help="Everything about one learner: current piece, latest session, next free one.",
    )
    show.add_argument("name", metavar="NAME")
    show.set_defaults(handler=handle_show)

    sessions = group.add_parser("sessions", help="Every session with its document status.")
    sessions.add_argument("name", metavar="NAME")
    sessions.set_defaults(handler=handle_sessions)

    lessons = group.add_parser(
        "lessons",
        help="Every lesson that happened, read section by section, with its recording.",
        description=(
            "Reads each done or in-progress session page once: its sections "
            "(the ones `prep` reads) and its recording link, picked by the same "
            "rules `send` uses, so the song being learnt is never taken for the "
            "recording. --session reads that one session whatever its state."
        ),
    )
    lessons.add_argument("name", metavar="NAME")
    lessons.add_argument("--session", type=int, default=None, metavar="N")
    lessons.set_defaults(handler=handle_lessons)

    latest = group.add_parser(
        "latest",
        help="The most recent session that actually happened.",
        description=(
            "The newest session whose document is marked done, not the highest "
            "number. Sessions get skipped, so a high number proves nothing."
        ),
    )
    latest.add_argument("name", metavar="NAME")
    latest.set_defaults(handler=handle_latest)

    next_free = group.add_parser(
        "next",
        help="The lowest session a new lesson may land on.",
        description=(
            "A not-started page with no content is free. A page in progress is "
            "the target while it is fresh: the studio's flow books a lesson, the "
            "page turns In progress, and the summary is written onto that page. "
            "Only a page still in progress more than learner.next_stale_days "
            "past its date is passed over as abandoned. An unstarted page that "
            "already has blocks on it is work in progress, and is never free."
        ),
    )
    next_free.add_argument("name", metavar="NAME")
    next_free.set_defaults(handler=handle_next)

    active = group.add_parser(
        "in-progress",
        help="Who still owes a summary, from the calendar window.",
        description=(
            "Reads the last learner.in_progress_days of calendar events (today "
            "included) and checks only those learners' pages: one calendar call "
            "plus one document read per lesson, not every page of every learner. "
            "A lesson counts only while its page still says In progress: "
            "cancelled and summarized lessons drop out on their own. Sessions "
            "booked for a coming day are outside the window; `calendar list` "
            "answers who is coming."
        ),
    )
    active.add_argument(
        "--videos",
        action="store_true",
        help="Also read the candidates' pages for their recording, so the "
        "teacher sees which unfinished lessons already have one.",
    )
    active.set_defaults(handler=handle_in_progress)

    works = group.add_parser("works", help="Recorded performances and recordings.")
    works.add_argument("name", metavar="NAME")
    works.set_defaults(handler=handle_works)

    add = group.add_parser(
        "add",
        help="Enrol a new learner.",
        description="Refuses on an exact-name duplicate, and writes nothing "
        "unless every page URL given resolves.",
    )
    add.add_argument("name", metavar="NAME")
    add.add_argument("--instrument", required=True)
    add.add_argument("--tone", default="")
    add.add_argument("--has-instrument", action="store_true")
    add.add_argument(
        "--prompt-level", type=int, default=None, metavar="N", help="A studio-specific column."
    )
    add.add_argument("--master-link", default="", metavar="URL")
    add.add_argument(
        "--db-link",
        default="",
        metavar="URL",
        help="The Notion database the session pages live in.",
    )
    add.add_argument(
        "--page-urls",
        nargs="*",
        default=(),
        metavar="URL",
        help="Session pages; the week comes from each URL's slug, or counts "
        "up from 1 when a URL has none.",
    )
    add.add_argument(
        "--pages",
        nargs="*",
        default=(),
        metavar="W<n>-URL",
        help="Session pages named by week, e.g. W1-https://... Takes "
        "precedence over --page-urls when both are given.",
    )
    add.add_argument(
        "--dry-run", action="store_true", help="Show what would be enrolled, and stop."
    )
    add.set_defaults(handler=handle_add)

    rename = group.add_parser(
        "rename",
        help="Change the name a learner is recorded under.",
        description=(
            "Rewrites the database row only. Calendar events, session pages, "
            "and source folders that already carry the old name keep it; new "
            "bookings and clips match the new name from here on. This is the "
            "replacement flow: a new learner taking over a leaver's slot, "
            "history included, without re-keying anything. Refuses a name "
            "another learner already has."
        ),
    )
    rename.add_argument("name", metavar="NAME")
    rename.add_argument("--to", required=True, metavar="NEW_NAME", dest="new_name")
    rename.add_argument("--dry-run", action="store_true", help="Show the change, and stop.")
    rename.set_defaults(handler=handle_rename)

    add_work = group.add_parser(
        "add-work",
        help="Record a finished performance, optionally completing its lesson.",
    )
    add_work.add_argument("name", metavar="NAME")
    add_work.add_argument("--title", required=True)
    add_work.add_argument("--type", default="performance", help="performance, cover, exam, …")
    add_work.add_argument("--video-link", default="")
    add_work.add_argument(
        "--drive-link", default="", help="A second home of the recording (Drive file)."
    )
    add_work.add_argument("--date", default="", metavar="YYYY-MM-DD")
    add_work.add_argument(
        "--session",
        type=int,
        default=None,
        metavar="N",
        help="Treat this work as the completed lesson N; omit it to record library work only.",
    )
    add_work.add_argument(
        "--dry-run", action="store_true", help="Show what would be recorded, and stop."
    )
    add_work.set_defaults(handler=handle_add_work)

    pieces = group.add_parser("pieces", help="List every piece.")
    pieces.set_defaults(handler=handle_pieces)

    assign = group.add_parser("assign", help="Set or clear the piece a learner is working on.")
    assign.add_argument("name", metavar="NAME")
    assign.add_argument(
        "--piece", default="", metavar="PIECE_ID", help="Piece id. Omit to clear the assignment."
    )
    assign.add_argument(
        "--update-published",
        action="store_true",
        help="Replace this assignment's rendered piece section on already-published sessions.",
    )
    assign.add_argument("--dry-run", action="store_true")
    assign.set_defaults(handler=handle_assign)

    attach = group.add_parser(
        "attach-work",
        help="Put a recorded work onto its session page.",
        description=(
            "A recording that did not go through the video pipeline: a "
            "teacher's own edit, a clip shared directly: has links in the "
            "database and nothing on the page. This writes the same section "
            "the old push wrote, under the same heading, onto the session "
            "In progress by default or --session N. Links already on the page "
            "are not written twice, and nothing else on the page is removed."
            " With --complete it also marks the lesson done and publishes the "
            "recording as that session's deliverable."
        ),
    )
    attach.add_argument("name", metavar="NAME")
    attach.add_argument(
        "--pick",
        type=int,
        default=None,
        metavar="N",
        help="The Nth work from the newest-first list (1 is the latest). "
        "Without it the command lists and asks.",
    )
    attach.add_argument(
        "--session", type=int, default=None, help="Defaults to the session in progress."
    )
    attach.add_argument(
        "--complete",
        action="store_true",
        help="Also finish the session and publish this recording as the lesson.",
    )
    attach.add_argument("--dry-run", action="store_true", help="Report what would be written.")
    attach.set_defaults(handler=handle_attach_work)

    activate = group.add_parser(
        "activate",
        help="Mark learners as studying here again.",
        description=(
            "The reverse of deactivate: the learner returns to matching "
            "candidates and rosters. Their history was never gone."
        ),
    )
    activate.add_argument("names", nargs="*", metavar="NAME")
    activate.set_defaults(handler=handle_activate)

    deactivate = group.add_parser(
        "deactivate",
        help="Mark learners as no longer studying. Nothing is deleted.",
        description=(
            "Their history stays reachable by exact name; they leave the "
            "matching candidates and the rosters. Reversible with `baton "
            "learner activate`. --serve opens a localhost checklist to tick "
            "several at once instead of naming them here."
        ),
    )
    deactivate.add_argument("names", nargs="*", metavar="NAME")
    deactivate.add_argument(
        "--serve",
        action="store_true",
        help="Open a localhost checklist instead of naming learners.",
    )
    deactivate.add_argument("--port", type=int, default=8765, metavar="N", help="Port for --serve.")
    deactivate.set_defaults(handler=handle_deactivate)

    trash = group.add_parser(
        "trash",
        help="Move a learner out of the way entirely. Reversible.",
        description=(
            "Unlike deactivate, a trashed learner is hidden even from `learner "
            "list --all`, and their name stops resolving for every other "
            "command. The row, sessions, pieces, and recorded work are kept "
            "exactly as they are. Reversible with `baton learner untrash`."
        ),
    )
    trash.add_argument("name", metavar="NAME")
    trash.add_argument("--dry-run", action="store_true", help="Show the change, and stop.")
    trash.set_defaults(handler=handle_trash)

    untrash = group.add_parser(
        "untrash",
        help="Bring a trashed learner back. Nothing about their history moved.",
    )
    untrash.add_argument("name", metavar="NAME")
    untrash.set_defaults(handler=handle_untrash)

    edit = group.add_parser(
        "edit",
        help="Change a learner's instrument, tone, or own-instrument flag. Nothing else moves.",
        description=(
            "Edits the database row only, the same contract as `learner "
            "rename`: pages, folders, and calendar entries keep whatever "
            "they already carry, and what changes is what future reads "
            "match against. At least one field must be given."
        ),
    )
    edit.add_argument("name", metavar="NAME")
    edit.add_argument(
        "--instrument",
        metavar="INSTRUMENT",
        default=None,
        help="The instrument, in the studio's words.",
    )
    edit.add_argument(
        "--tone",
        metavar="TONE",
        default=None,
        help="The summary tone, e.g. standard, casual, child.",
    )
    edit.add_argument(
        "--has-instrument",
        dest="has_instrument",
        action="store_true",
        default=None,
        help="Record that they own an instrument.",
    )
    edit.add_argument(
        "--no-has-instrument",
        dest="has_instrument",
        action="store_false",
        help="Record that they do not own one yet.",
    )
    edit.add_argument("--dry-run", action="store_true", help="Show the change, and stop.")
    edit.set_defaults(handler=handle_edit)

    schedule = group.add_parser(
        "schedule",
        help="Show a learner's recurring weekly slots.",
        description=(
            "The standing weekly pattern: which days, what times. The "
            "calendar holds booked lessons with real dates; this is what "
            "backs them. Empty is a normal answer."
        ),
    )
    schedule.add_argument("name", metavar="NAME")
    schedule.set_defaults(handler=handle_schedule)

    schedule_set = group.add_parser(
        "schedule-set",
        help="Replace a learner's whole weekly schedule.",
        description=(
            "Sends the full final set: every --slot is the whole schedule, "
            "so a retry cannot double-book. Omitting --slot clears it. A "
            "slot held by another active learner refuses the whole save and "
            "names them; slots of learners who stopped or were trashed "
            "never clash, because they are not coming. One slot is one "
            "hour, so a two-hour lesson is two consecutive slots."
        ),
    )
    schedule_set.add_argument("name", metavar="NAME")
    schedule_set.add_argument(
        "--slot",
        action="append",
        default=None,
        metavar="DAY HH:MM",
        help='One weekly hour, e.g. "Monday 16:00". Repeat the flag for '
        "every slot; the set replaces the old schedule entirely.",
    )
    schedule_set.add_argument("--dry-run", action="store_true", help="Show the change, and stop.")
    schedule_set.set_defaults(handler=handle_schedule_set)


def _require_subcommand(ctx: Context) -> Exit:
    raise UsageError(
        "`baton learner` needs a subcommand.",
        remedy="Try `baton learner list`, or `baton learner show <name>`.",
    )


# -- shared plumbing ---------------------------------------------------------


def _next_stale_days(ctx: Context) -> int | None:
    """`learner.next_stale_days`, where `null` means never abandon a page."""
    value = ctx.config.get("learner.next_stale_days", 1)
    return None if value is None else int(value)


def _history(ctx: Context, store) -> LearnerHistory:
    return LearnerHistory(
        store,
        open_docs(ctx.config),
        StatusVocabulary.from_config(ctx.config.section("docs.statuses")),
        max_parallel_reads=int(ctx.config.get("docs.max_parallel_reads", 4)),
        next_stale_days=_next_stale_days(ctx),
    )


def _resolve(ctx: Context, store, name: str):
    """Resolve a typed name, or raise NeedsHumanError with candidates.

    A trashed learner's name never resolves here: ``store.list_learners()``
    excludes them by default, so trash acts on every other command exactly
    like the learner is gone. Only ``trash``/``untrash`` themselves need to
    find one; they use :func:`_resolve_including_trashed` instead.
    """
    learner = resolve_learner(
        name,
        store.list_learners(),
        aliases=ctx.config.get("db.aliases", {}) or {},
        label=ctx.config.label("learner"),
    )
    warn_if_inactive(ctx, learner)
    return learner


def _recording_session(ctx: Context, store, learner: Learner, wanted: int) -> SessionView:
    """The in-progress session a recording-only lesson will complete."""

    history = _history(ctx, store)
    view = next((item for item in history.sessions(learner) if item.number == wanted), None)
    label = ctx.config.label("session")
    if view is None or not view.session.doc_id:
        raise UsageError(
            f"{learner.name} has no {label} {wanted} with a document.",
            remedy=f'Run `baton learner sessions "{learner.name}"` to see what exists.',
        )
    if view.state != "in_progress":
        raise UsageError(
            f"{learner.name}'s {label} {wanted} is {view.state or 'unknown'}, not in progress.",
            remedy="Recording-only publishing replaces a summary only while the session is open.",
        )
    if PublishedRecord(ctx.config.state_dir / "published").get(learner.id, wanted) is not None:
        raise UsageError(
            f"{learner.name}'s {label} {wanted} already has a published lesson.",
            remedy="Unpublish that lesson first, or record the work without --session.",
        )
    return view


def _recording_preview(
    ctx: Context, learner: Learner, view: SessionView, work: Work
) -> tuple[str, list[dict[str, Any]]]:
    """The exact message and page blocks a completed recording will produce."""

    status = open_docs(ctx.config).get_status(view.session.doc_id, with_blocks=False)
    message = compose_recording(
        work,
        learner_name=learner.name,
        instrument=learner.instrument,
        date=DateFormat.from_config(ctx.config.section("chat.date")).of_text(work.performed_date),
        doc_url=status.url,
    )
    return message, recording_blocks(work, message=message)


def _resolve_including_trashed(ctx: Context, store, name: str):
    """Like :func:`_resolve`, but a trashed learner's exact name still finds
    them. Used only by ``learner trash``/``learner untrash``."""
    return resolve_learner(
        name,
        store.list_learners(include_trashed=True),
        aliases=ctx.config.get("db.aliases", {}) or {},
        label=ctx.config.label("learner"),
    )


def _session_line(view: SessionView, vocabulary: StatusVocabulary, label: str) -> str:
    mark = _STATE_MARK.get(view.state, "?")
    status = view.doc.status or vocabulary.label(view.state) or "unknown"
    bits = [f"  {mark} {label} {view.number:<3} {status}"]
    if view.doc.date:
        bits.append(f"  {view.doc.date}")
    if view.doc.titles:
        bits.append(f"  {view.doc.titles}")
    if view.doc.block_count:
        bits.append(f"  ({view.doc.block_count} blocks)")
    return "".join(bits)


# -- handlers ----------------------------------------------------------------


def handle_list(ctx: Context) -> Exit:
    if ctx.args.all and ctx.args.trashed:
        raise UsageError(
            "--all and --trashed cannot both be given.",
            remedy="--all shows everyone still in the roster; --trashed shows "
            "who was moved out of it. Pick one.",
        )

    with store_scope(ctx.config) as store:
        if ctx.args.trashed:
            everyone = store.list_learners(include_trashed=True)
            learners = [item for item in everyone if item.deleted_at]
            titles = {p.id: p.title for p in store.list_pieces()} if learners else {}
            by_learner: dict[str, list[dict[str, Any]]] = {}
        else:
            everyone = store.list_learners()
            learners = everyone if ctx.args.all else [item for item in everyone if item.is_active]
            hidden = 0 if ctx.args.all else len(everyone) - len(learners)
            # One extra read, only when there is a list to annotate: the piece
            # catalogue is small, and knowing who is on what without a second
            # command is the whole point of a roster.
            titles = {p.id: p.title for p in store.list_pieces()} if learners else {}
            # The weekly slots ride along so a roster consumer (the admin page)
            # reads everything in one call. One query for every learner, not
            # one per learner; trashed rows carry none, so that branch skips
            # the read entirely.
            by_learner = {}
            if learners:
                for slot in store.list_slots():
                    by_learner.setdefault(slot.learner_id, []).append(slot.to_dict())

    if ctx.args.trashed:
        payload = {
            "learners": [item.to_dict() for item in learners],
            "count": len(learners),
            "scope": "trashed",
        }
        human = (
            f"No trashed {ctx.config.label('learners')}."
            if not learners
            else "\n".join(f"  {item.name}  (trashed {item.deleted_at})" for item in learners)
        )
        ctx.report.result(payload, human=human)
        return Exit.OK

    payload = {
        "learners": [dict(item.to_dict(), slots=by_learner.get(item.id, [])) for item in learners],
        "count": len(learners),
        "scope": "all" if ctx.args.all else "active",
        "hidden_inactive": hidden,
    }
    if not learners:
        human = (
            f"No {ctx.config.label('learners')} recorded."
            if hidden == 0
            else f"No active {ctx.config.label('learners')}; {hidden} no longer "
            f"studying (--all shows them)."
        )
        ctx.report.result(payload, human=human)
        return Exit.OK

    width = max(len(item.name) for item in learners)
    lines = []
    for item in learners:
        line = f"  {item.name:<{width}}  {item.instrument or '-':<10} {item.tone or '-'}"
        if item.current_piece_id:
            line += f"  : {titles.get(item.current_piece_id, '?')}"
        if not item.is_active:
            line += "  (inactive)"
        lines.append(line)
    if hidden:
        lines.append(f"  ({hidden} no longer studying; --all shows them)")
    ctx.report.result(payload, human="\n".join(lines))
    return Exit.OK


def handle_show(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, ctx.args.name)
        history = _history(ctx, store)
        views = history.sessions(learner)
        payload = history.summarise(learner, views, today=today_in(ctx.config.timezone))

    label = ctx.config.label("session")
    lines = [f"{learner.name}  ({learner.instrument or 'no instrument recorded'})"]
    if payload["current_piece"]:
        lines.append(f"  working on : {payload['current_piece']['title']}")
    lines.append(f"  tone       : {learner.tone or '-'}")
    lines.append(f"  practises at home: {'yes' if learner.has_instrument else 'no'}")
    lines.append("")

    latest = payload["sessions"]["latest_done"]
    upcoming = payload["sessions"]["next_empty"]
    lines.append(
        f"  latest done: {label} {latest['number']} ({latest['date'] or 'no date'})"
        if latest
        else f"  latest done: none, no {label} is marked done yet"
    )
    lines.append(
        f"  next free  : {label} {upcoming['number']}"
        if upcoming
        else f"  next free  : none: every {label} is started or has content"
    )
    for view in payload["sessions"]["in_progress"]:
        lines.append(f"  in progress: {label} {view['number']}")
    totals = payload["sessions"]
    lines.append(f"  total      : {totals['total']} ({totals['done']} done)")

    ctx.report.result(payload, human="\n".join(lines))
    return Exit.OK


def handle_sessions(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, ctx.args.name)
        history = _history(ctx, store)
        views = history.sessions(learner)

    label = ctx.config.label("session")
    payload = {
        "learner": learner.to_dict(),
        "sessions": [view.to_dict(history.vocabulary) for view in views],
    }
    if not views:
        ctx.report.result(payload, human=f"{learner.name} has no {ctx.config.label('sessions')}.")
        return Exit.OK

    lines = [f"{learner.name}"] + [_session_line(view, history.vocabulary, label) for view in views]
    ctx.report.result(payload, human="\n".join(lines))
    return Exit.OK


def handle_lessons(ctx: Context) -> Exit:
    from ..adapters.media.google import extract_video_id  # pure string parsing

    rules = SectionRules.from_config(ctx.config)
    allowed = tuple(
        str(item) for item in ctx.config.get("docs.video_link_blocks", VIDEO_LINK_BLOCKS)
    )
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, ctx.args.name)
        history = _history(ctx, store)
        views = history.sessions(learner)
        # The piece's own link sits on the same page as the recording and is
        # the same kind of YouTube URL; naming it keeps it from being taken
        # for the lesson's recording.
        exclude: list[str] = []
        if learner.current_piece_id:
            try:
                piece = store.get_piece(learner.current_piece_id)
            except BatonError:
                piece = None
            if piece and piece.source_link:
                exclude.append(piece.source_link)

    wanted = [
        view
        for view in views
        if view.session.doc_id
        and (
            view.number == ctx.args.session
            if ctx.args.session is not None
            else view.state in ("done", "in_progress")
        )
    ]
    lessons: list[dict[str, Any]] = []
    for view in wanted:
        entry: dict[str, Any] = {
            "number": view.number,
            "state": view.state,
            "status": view.doc.status,
            "date": view.doc.date,
            "titles": view.doc.titles,
            "sections": {},
            "recording": "",
            "recording_id": "",
            "unreadable": view.unreadable,
        }
        try:
            blocks = history.docs.list_blocks(view.session.doc_id)
        except BatonError as exc:
            entry["unreadable"] = exc.message
        else:
            entry["sections"] = rules.read(blocks)
            link = recording_in(blocks, blocks=allowed, exclude=exclude)
            entry["recording"] = link
            entry["recording_id"] = extract_video_id(link) or ""
        lessons.append(entry)

    label = ctx.config.label("session")
    payload = {"learner": learner.to_dict(), "lessons": lessons}
    lines = [learner.name] + [
        f"  {label} {item['number']:>2}  {item['state'] or '?':<11} {item['date'] or '-':<10}"
        f"  {'video' if item['recording'] else '     '}  {item['titles'] or ''}"
        + (f"  (unreadable: {item['unreadable']})" if item["unreadable"] else "")
        for item in lessons
    ]
    ctx.report.result(payload, human="\n".join(lines))
    return Exit.OK


def handle_latest(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, ctx.args.name)
        history = _history(ctx, store)
        view = history.latest_done(history.sessions(learner))

    label = ctx.config.label("session")
    payload: dict[str, Any] = {
        "learner": learner.to_dict(),
        "latest_done": view.to_dict(history.vocabulary) if view else None,
    }
    if view is None:
        ctx.report.result(
            payload,
            human=f"{learner.name} has no {label} marked done yet.",
        )
        return Exit.OK

    # The page's headings carry what properties cannot: what was covered, what
    # was set to practise, where the teaching goes next. One extra read turns
    # "which session was last" into "prepare the next lesson" in one command.
    sections: dict[str, str] = {}
    unreadable = ""
    if view.session.doc_id:
        try:
            blocks = history.docs.list_blocks(view.session.doc_id)
            sections = SectionRules.from_config(ctx.config).read(blocks)
        except BatonError as exc:
            unreadable = exc.message
    payload["sections"] = sections
    if unreadable:
        payload["sections_unreadable"] = unreadable

    lines = [
        f"{learner.name} {label} {view.number}"
        f"{f'  {view.doc.date}' if view.doc.date else ''}"
        f"{f'  {view.doc.titles}' if view.doc.titles else ''}",
        f"  doc: {view.session.doc_id}",
    ]
    if unreadable:
        lines.append(f"  sections unreadable: {unreadable}")
    lines += [f"  {name.replace('_', ' ')}: {text}" for name, text in sections.items() if text]
    ctx.report.result(payload, human="\n".join(lines))
    return Exit.OK


def handle_next(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, ctx.args.name)
        history = _history(ctx, store)
        views = history.sessions(learner)
        view = history.next_empty(views, today=today_in(ctx.config.timezone))

    label = ctx.config.label("session")
    payload = {
        "learner": learner.to_dict(),
        "next_empty": view.to_dict(history.vocabulary) if view else None,
        "highest_number": max((v.number for v in views), default=0),
    }
    if view is None:
        ctx.report.result(
            payload,
            human=f"{learner.name} has no free {label}. "
            f"Highest recorded: {payload['highest_number']}.",
        )
        return Exit.OK

    ctx.report.result(
        payload,
        human=f"{learner.name}: next free {label} is {view.number}\n  doc: {view.session.doc_id}",
    )
    return Exit.OK


def handle_in_progress(ctx: Context) -> Exit:
    scheduler = _scheduler(ctx)
    with store_scope(ctx.config) as store:
        report = scheduler.in_progress(
            store,
            window_days=int(ctx.config.get("learner.in_progress_days", 14)),
        )

    label = ctx.config.label("session")

    # The candidates' recordings, when asked for: one extra block read per
    # in-progress session, still bounded by the calendar window. The old
    # report answered this by scanning every page of every learner; the same
    # column without the whole-world scan. A page that cannot be read here
    # reads as no recording, matching how the send path degrades.
    recordings: dict[str, str] = {}
    if ctx.args.videos:
        docs = open_docs(ctx.config)
        shapes = tuple(
            str(item) for item in ctx.config.get("docs.video_link_blocks", VIDEO_LINK_BLOCKS)
        )
        for _learner, view in report.found:
            recordings[view.session.doc_id] = find_video_link(
                docs, view.session.doc_id, blocks=shapes
            )

    payload = {
        "in_progress": [
            {
                "learner": learner.to_dict(),
                **view.to_dict(scheduler.vocabulary),
                **({"video_link": recordings[view.session.doc_id]} if ctx.args.videos else {}),
            }
            for learner, view in report.found
        ],
        "unreadable": report.unreadable,
        "unmatched_events": report.unmatched,
        "window": report.window,
        "count": len(report.found),
    }

    def _mark(view) -> str:
        if not ctx.args.videos:
            return ""
        return "  🎬" if recordings.get(view.session.doc_id) else "  -"

    lines: list[str] = []
    if report.found:
        width = max(len(learner.name) for learner, _ in report.found)
        lines += [
            f"  {learner.name:<{width}}  {label} {view.number}"
            f"{f'  {view.doc.titles}' if view.doc.titles else ''}{_mark(view)}"
            for learner, view in report.found
        ]
    lines += [
        f"  ⚠ {entry['learner']}'s {label} {entry['number']} could not be read: {entry['why']}"
        for entry in report.unreadable
    ]
    lines += [
        f"  ? “{entry['title']}” names no {label} this studio booked" for entry in report.unmatched
    ]
    if not lines:
        lines = [f"No {label} is in progress in the last {report.window['days']} days."]
    lines.append(f"  window: {report.window['start']} … {report.window['through']}")
    ctx.report.result(payload, human="\n".join(lines))
    return Exit.OK


def _check_mapped(fields: dict[str, Any], extra: dict[str, Any], *, setting: str) -> None:
    """Refuse before any write when an extra field has no column configured.

    The same check :meth:`~baton.adapters.db.base.FieldMap.extra_columns`
    makes at write time, run here first so a learner and their first session
    are not created before the *second* session's extra field turns out to
    be unmapped.
    """
    for key in extra:
        if key not in fields:
            raise ConfigError(
                f"`{setting}.{key}` is not mapped, so `{key}` cannot be written.",
                remedy=f"Add it under {setting} in baton.yaml, or drop the field.",
                details={"setting": setting, "field": key},
            )


def _validate_choice(ctx: Context, value: str, *, setting: str, field: str) -> None:
    """Refuse a value the profile's own list does not name.

    An empty list means the profile has not restricted this field: every
    value is accepted, because a public tool has no business guessing a
    studio's vocabulary for it.
    """
    allowed = [str(item) for item in (ctx.config.get(setting, []) or [])]
    if allowed and value not in allowed:
        raise UsageError(
            f"`{value}` is not a configured {field}.",
            remedy=f"Use one of: {', '.join(allowed)}.",
            details={"setting": setting, "understood": allowed},
        )


def _resolve_pages(args: Any) -> list[tuple[int, str]]:
    """(week, page id) pairs from --pages or --page-urls, in the order given.

    ``--pages`` wins over ``--page-urls`` when both are given, matching the
    original script's order. A URL Baton cannot read a page id from is
    refused rather than skipped: silently dropping a page a person meant to
    add is worse than stopping to ask about it.

    Raises:
        UsageError: A ``--pages`` token is not ``W<week>-<url>``, or a URL
            carries no Notion page id.
    """
    entries: list[tuple[int, str]] = []
    if args.pages:
        for token in args.pages:
            match = re.match(r"^[Ww](\d+)-(.+)$", token)
            if not match:
                raise UsageError(
                    f"`{token}` is not `W<week>-<url>`.",
                    remedy="Each --pages token is a week number, a dash, then the page URL.",
                )
            url = match.group(2)
            page_id = parse_page_id(url)
            if page_id is None:
                raise UsageError(
                    f"No Notion page id could be read from `{url}`.",
                    remedy="Paste the page's own URL, not a shortened or edited one.",
                )
            entries.append((int(match.group(1)), page_id))
        return entries

    next_week = 1
    for url in args.page_urls:
        page_id = parse_page_id(url)
        if page_id is None:
            raise UsageError(
                f"No Notion page id could be read from `{url}`.",
                remedy="Paste the page's own URL, not a shortened or edited one.",
            )
        week = detect_week(url) or next_week
        entries.append((week, page_id))
        next_week = week + 1
    return entries


def handle_add(ctx: Context) -> Exit:
    """Enrol a learner, then their session pages, in that order.

    Nothing is written until every input has resolved: the name is not a
    duplicate, the instrument and tone (when the profile restricts them) are
    known values, every page URL carries a page id, and every extra field:
    a prompt level, a master link, a session's Notion database id: has a
    place to be written. A studio-specific column named on the command line
    with nowhere configured to put it is a configuration error, not a
    silently dropped write.
    """
    args = ctx.args
    _validate_choice(ctx, args.instrument, setting="learner.instruments", field="instrument")
    if args.tone:
        _validate_choice(ctx, args.tone, setting="learner.tones", field="tone")

    pages = _resolve_pages(args)

    database_id = ""
    if args.db_link:
        parsed = parse_page_id(args.db_link)
        if parsed is None:
            raise UsageError(
                f"No Notion database id could be read from `{args.db_link}`.",
                remedy="Paste the database's own URL.",
            )
        database_id = parsed

    learner_fields = ctx.config.section("db.fields.learner")
    session_fields = ctx.config.section("db.fields.session")
    learner_extra: dict[str, Any] = {}
    if args.prompt_level is not None:
        learner_extra["prompt_level"] = args.prompt_level
    if args.master_link:
        learner_extra["master_link"] = args.master_link
    session_extra_keys = []
    if database_id:
        session_extra_keys.append("database_id")
        session_extra_keys.append("notion_db_full_link")
    elif pages and "database_id" in session_fields:
        # The overlay's own schema has a NOT NULL database_id column: a page
        # with no database id would fail the whole insert, so this is caught
        # before the learner is even created rather than mid-way through.
        raise UsageError(
            f"{ctx.config.label('session')} pages need --db-link: this "
            "profile's session table requires a database id.",
            remedy="Pass --db-link with the pages' Notion database URL.",
        )
    _check_mapped(learner_fields, learner_extra, setting="db.fields.learner")
    _check_mapped(
        session_fields, dict.fromkeys(session_extra_keys, ""), setting="db.fields.session"
    )

    with store_scope(ctx.config) as store:
        existing = store.list_learners()
        wanted = normalise(args.name)
        duplicate = next((p for p in existing if normalise(p.name) == wanted), None)
        if duplicate is not None:
            raise GateError(
                f"{args.name} is already recorded (id={duplicate.id}).",
                remedy=f'Use `baton learner show "{args.name}"` to see the '
                "existing record. Nothing was written.",
            )
        # Either direction: a name that extends an existing one ("Elin
        # Frostberg" against "Elin Frost") or is extended by one, either way
        # worth a second look without blocking the enrolment over it.
        similar = [
            p.name for p in existing if wanted in normalise(p.name) or normalise(p.name) in wanted
        ]

        proposed = Learner(
            id="",
            name=args.name,
            instrument=args.instrument,
            tone=args.tone,
            has_instrument=args.has_instrument,
        )

        if args.dry_run:
            ctx.report.result(
                {
                    "would_add": proposed.to_dict(),
                    "would_add_extra": learner_extra,
                    "would_add_sessions": [
                        {"number": week, "doc_id": page_id} for week, page_id in pages
                    ],
                    "similar": similar,
                    "dry_run": True,
                },
                human=f"Would enrol {args.name} ({args.instrument})"
                + (f" with {len(pages)} session page(s)." if pages else "."),
            )
            return Exit.OK

        created = store.add_learner(proposed, extra=learner_extra or None)
        created_sessions: list[Session] = []
        try:
            for week, page_id in pages:
                session_extra: dict[str, Any] = {}
                if database_id:
                    session_extra["database_id"] = database_id
                    session_extra["notion_db_full_link"] = args.db_link
                created_sessions.append(
                    store.add_session(
                        Session(id="", learner_id=created.id, number=week, doc_id=page_id),
                        extra=session_extra or None,
                    )
                )
        except BatonError as exc:
            raise UpstreamError(
                f"{created.name} was enrolled, but a session page failed to write: {exc.message}",
                service="db",
                remedy="The learner and any listed sessions already exist: "
                "check what landed before retrying the rest by hand.",
                details={
                    "learner": created.to_dict(),
                    "sessions_written": [s.to_dict() for s in created_sessions],
                },
            ) from exc

    ctx.report.result(
        {
            "learner": created.to_dict(),
            "sessions": [s.to_dict() for s in created_sessions],
            "similar": similar,
        },
        human=f"Enrolled {created.name} ({created.instrument})"
        + (f" with {len(created_sessions)} session page(s)." if created_sessions else ".")
        + (f" Similarly named: {', '.join(similar)}." if similar else ""),
    )
    return Exit.OK


def handle_rename(ctx: Context) -> Exit:
    """Rename a learner in the database, and only there.

    The name is identity everywhere: calendar titles, source folders, session
    pages. Those were created under the old name and stay under it; what a
    rename moves is what *future* matching reads. A duplicate new name is
    refused before anything is written, for the same reason `learner add`
    refuses one: two rows one keystroke apart is how a booking lands on the
    wrong child.
    """
    args = ctx.args
    new_name = args.new_name.strip()
    if not new_name:
        raise UsageError(
            "The new name is empty.",
            remedy='Pass --to "New Name".',
        )

    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, args.name)
        wanted = normalise(new_name)
        duplicate = next((p for p in store.list_learners() if normalise(p.name) == wanted), None)
        if duplicate is not None and duplicate.id != learner.id:
            raise GateError(
                f"{new_name} is already recorded (id={duplicate.id}).",
                remedy=f'Use `baton learner show "{new_name}"` to see the existing '
                "record. Nothing was written.",
            )

        if args.dry_run:
            ctx.report.result(
                {"from": learner.to_dict(), "to": new_name, "dry_run": True},
                human=f"Would rename {learner.name} to {new_name} in the database.",
            )
            return Exit.OK

        store.rename_learner(learner.id, new_name)

    ctx.report.result(
        {
            "learner": {**learner.to_dict(), "name": new_name},
            "renamed_from": learner.name,
        },
        human=(
            f"Renamed {learner.name} to {new_name} in the database.\n"
            "    Calendar events, pages, and folders already carrying the old "
            "name keep it; new bookings match the new name."
        ),
    )
    return Exit.OK


def handle_trash(ctx: Context) -> Exit:
    """Move a learner out of the way entirely, in the database, and only there.

    Unlike deactivate, this hides them even from `learner list --all` and
    stops their name from resolving for every other command. Nothing about
    the row, sessions, pieces, or recorded work changes, and nothing outside
    the database is touched: Notion pages, source folders, and calendar
    history are this command's business to leave exactly as they are.
    Trashing twice is safe and changes nothing further.
    """
    with store_scope(ctx.config) as store:
        learner = _resolve_including_trashed(ctx, store, ctx.args.name)
        if ctx.args.dry_run:
            ctx.report.result(
                {"learner": learner.to_dict(), "dry_run": True},
                human=f"Would trash {learner.name} in the database.",
            )
            return Exit.OK
        store.trash_learner(learner.id)

    ctx.report.result(
        {"learner": {**learner.to_dict(), "deleted_at": learner.deleted_at or "now"}},
        human=(
            f"Trashed {learner.name} in the database.\n"
            "    Nothing outside it moved. Reversible with "
            f'`baton learner untrash "{learner.name}"`.'
        ),
    )
    return Exit.OK


def handle_untrash(ctx: Context) -> Exit:
    """The reverse of `learner trash`. Safe on a learner who was never trashed."""
    with store_scope(ctx.config) as store:
        learner = _resolve_including_trashed(ctx, store, ctx.args.name)
        store.untrash_learner(learner.id)

    ctx.report.result(
        {"learner": {**learner.to_dict(), "deleted_at": None}},
        human=f"{learner.name} is back. Nothing about their history moved.",
    )
    return Exit.OK


def handle_edit(ctx: Context) -> Exit:
    """Edit a learner's instrument, tone, or own-instrument flag.

    The database row only, like `learner rename`: session pages, source
    folders, and calendar entries keep what they carry. Fields not named
    on the command line are left exactly as they were.
    """
    args = ctx.args
    fields: dict[str, Any] = {}
    if args.instrument is not None:
        if not args.instrument.strip():
            raise UsageError(
                "The instrument is empty.",
                remedy='Pass --instrument "กีตาร์", or leave it out.',
            )
        fields["instrument"] = args.instrument.strip()
    if args.tone is not None:
        if not args.tone.strip():
            raise UsageError("The tone is empty.", remedy="Pass --tone standard, or leave it out.")
        fields["tone"] = args.tone.strip()
    if args.has_instrument is not None:
        fields["has_instrument"] = args.has_instrument
    if not fields:
        raise UsageError(
            "`baton learner edit` needs at least one field to change.",
            remedy="Editable fields: --instrument, --tone, --has-instrument/--no-has-instrument.",
        )

    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, args.name)
        if args.dry_run:
            ctx.report.result(
                {"learner": learner.to_dict(), "changes": fields, "dry_run": True},
                human=f"Would change {learner.name}: {fields}.",
            )
            return Exit.OK
        store.update_learner(learner.id, fields)
        fresh = store.get_learner(learner.id)

    written = fresh if fresh is not None else learner
    ctx.report.result(
        {"learner": written.to_dict(), "changes": fields},
        human=f"Changed {fields} on {written.name}. Nothing outside the row moved.",
    )
    return Exit.OK


_SLOT_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_slot(raw: str) -> tuple[str, str]:
    """One ``--slot`` value, ``"Monday 16:00"``, into its canonical parts.

    Raises:
        UsageError: Not a weekday name and a 24-hour time, in that order.
    """
    parts = raw.split()
    if len(parts) != 2:
        raise UsageError(
            f"`{raw}` is not a slot.",
            remedy='A slot is "DAY HH:MM", e.g. "Monday 16:00".',
        )
    day_word, time_word = parts
    weekday = next((day for day in WEEKDAYS if day.casefold() == day_word.casefold()), "")
    if not weekday:
        raise UsageError(
            f"`{day_word}` is not a weekday.",
            remedy=f"A slot's day is one of: {', '.join(WEEKDAYS)}.",
        )
    match = _SLOT_TIME.fullmatch(time_word)
    if not match:
        raise UsageError(
            f"`{time_word}` is not a 24-hour time.",
            remedy='A slot\'s time is "HH:MM", e.g. "16:00".',
        )
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise UsageError(
            f"`{time_word}` is not a time of day.",
            remedy="Hours run 00-23 and minutes 00-59.",
        )
    return weekday, f"{hour:02d}:{minute:02d}"


def _parse_slots(raw_slots: list[str] | None) -> list[tuple[str, str]]:
    """Every ``--slot`` value, refusing an in-set duplicate before any read."""
    parsed = [_parse_slot(raw) for raw in raw_slots or []]
    seen: set[tuple[str, str]] = set()
    for slot in parsed:
        if slot in seen:
            raise UsageError(
                f"The slot {slot[0]} {slot[1]} appears twice in one request.",
                remedy="Send each hour once; a longer lesson is two consecutive slots.",
            )
        seen.add(slot)
    return parsed


def handle_schedule(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, ctx.args.name)
        slots = store.list_slots(learner.id)

    payload = {"learner": learner.to_dict(), "slots": [slot.to_dict() for slot in slots]}
    if not slots:
        human = f"No slots recorded for {learner.name}."
    else:
        width = max(len(slot.weekday) for slot in slots)
        human = "\n".join(f"  {slot.weekday:<{width}}  {slot.start}" for slot in slots)
    ctx.report.result(payload, human=human)
    return Exit.OK


def handle_schedule_set(ctx: Context) -> Exit:
    """Replace one learner's weekly schedule, whole set at a time.

    The clash gate runs before the dry-run branch, so a dry run reports the
    refusal it would refuse (the same order ``calendar book`` uses). Slots
    held by inactive or trashed learners never clash: they are not coming.
    """
    slots = _parse_slots(ctx.args.slot)
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, ctx.args.name)
        others = {
            item.id: item
            for item in store.list_learners()
            if item.id != learner.id and item.is_active
        }
        holders = {
            (slot.weekday, slot.start): others[slot.learner_id].name
            for slot in store.list_slots()
            if slot.learner_id in others
        }
        for weekday, start in slots:
            holder = holders.get((weekday, start))
            if holder is not None:
                raise GateError(
                    f"{weekday} {start} is already {holder}'s slot.",
                    missing=[
                        {
                            "field": "slot",
                            "reason": f"{weekday} {start} is taken by {holder}",
                            "how_to_fix": f"Pick another hour, or clear it from {holder} first.",
                        }
                    ],
                    remedy="Nothing was saved. The studio teaches one learner "
                    "at a time, so one weekday-hour belongs to one learner.",
                )
        if ctx.args.dry_run:
            ctx.report.result(
                {"learner": learner.to_dict(), "slots": slots, "dry_run": True},
                human=f"Would set {learner.name}'s schedule to "
                f"{[f'{day} {start}' for day, start in slots]}.",
            )
            return Exit.OK
        written = store.set_slots(learner.id, slots)

    ctx.report.result(
        {"learner": learner.to_dict(), "slots": [slot.to_dict() for slot in written]},
        human=f"Set {len(written)} slot(s) for {learner.name}. The old schedule is gone.",
    )
    return Exit.OK


def handle_works(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, ctx.args.name)
        works = store.list_works(learner.id)

    payload = {"learner": learner.to_dict(), "works": [item.to_dict() for item in works]}
    if not works:
        ctx.report.result(payload, human=f"{learner.name} has no recorded work.")
        return Exit.OK

    lines = [f"{learner.name}"] + [
        f"  {item.performed_date or '          '}  {item.title}  ({item.type})"
        + (f"  {item.video_link}" if item.video_link else "")
        for item in works
    ]
    ctx.report.result(payload, human="\n".join(lines))
    return Exit.OK


def handle_add_work(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, ctx.args.name)
        view = None
        if ctx.args.session is not None:
            view = _recording_session(ctx, store, learner, ctx.args.session)
            if not ctx.args.video_link.strip() and not ctx.args.drive_link.strip():
                raise GateError(
                    "A recording-only lesson needs a YouTube or Drive link.",
                    missing=[
                        {
                            "field": "video_link",
                            "reason": "at least one recording link is required",
                        },
                        {
                            "field": "drive_link",
                            "reason": "at least one recording link is required",
                        },
                    ],
                    remedy="Pass --video-link or --drive-link, or omit --session to record "
                    "library work without completing a lesson.",
                )
        proposed = Work(
            id="",
            learner_id=learner.id,
            title=ctx.args.title,
            type=ctx.args.type,
            video_link=ctx.args.video_link,
            drive_link=ctx.args.drive_link,
            performed_date=ctx.args.date,
        )
        if ctx.args.dry_run:
            message, blocks = (
                _recording_preview(ctx, learner, view, proposed) if view is not None else ("", [])
            )
            ctx.report.result(
                {
                    "learner": learner.to_dict(),
                    "would_add": proposed.to_dict(),
                    "would_complete": view.session.number if view is not None else None,
                    "would_message": message,
                    "would_append": len(blocks),
                    "dry_run": True,
                },
                human=(
                    f"Would complete {learner.name}'s "
                    f"{ctx.config.label('session')} {view.session.number} with "
                    f"{proposed.title} ({proposed.type})"
                    if view is not None
                    else f"Would record for {learner.name}: {proposed.title} ({proposed.type})"
                ),
            )
            return Exit.OK

        created = store.add_work(proposed)

    if view is not None:
        result = complete_recording_work(ctx.config, learner, view, created)
        ctx.report.result(
            result,
            human=(
                f"Published {created.title} as {learner.name}'s "
                f"{ctx.config.label('session')} {view.session.number}: "
                f"{result['appended']} blocks written, the {ctx.config.label('session')} is done"
            ),
        )
        return Exit.OK

    ctx.report.result(
        {"learner": learner.to_dict(), "work": created.to_dict()},
        human=f"Recorded for {learner.name}: {created.title} ({created.type})",
    )
    return Exit.OK


def handle_attach_work(ctx: Context) -> Exit:
    """One recorded work, written onto the session page it belongs to.

    Which recording goes on is a person's call, so the command is two steps
    like `send recording`: without --pick it ends at exit 3 carrying the
    numbered list; --pick N then writes exactly that one.
    """
    label = ctx.config.label("session")
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, ctx.args.name)
        works = store.list_works(learner.id)

        pick = ctx.args.pick
        if pick is None:
            if not works:
                raise GateError(
                    f"{learner.name} has no recorded work to attach.",
                    remedy="Record one first with `baton learner add-work "
                    f'"{ctx.args.name}" --title … (--video-link / --drive-link)`.',
                )
            raise NeedsHumanError(
                f"{learner.name} has {len(works)} recorded work(s): which one goes on the page?",
                candidates=list_candidates(works),
                remedy="Re-run with --pick <number> from that list; 1 is the "
                "most recent. Nothing was written.",
            )
        if not 1 <= pick <= len(works):
            raise UsageError(
                f"--pick {pick} does not match any of {learner.name}'s "
                f"{len(works)} recorded work(s).",
                remedy="Re-run without --pick to see the newest-first list.",
                details={"candidates": list_candidates(works)} if works else None,
            )

        # Which page: the session in progress by default (the recording
        # belongs to the lesson it came from) or the one named.
        history = _history(ctx, store)
        views = history.sessions(learner)
        if ctx.args.session is not None:
            view = next((v for v in views if v.session.number == ctx.args.session), None)
            if view is None or not view.session.doc_id:
                raise UsageError(
                    f"{learner.name} has no {label} {ctx.args.session} to attach to.",
                    remedy=f'Run `baton learner sessions "{ctx.args.name}"` to see what exists.',
                )
        else:
            active = [v for v in views if v.state == "in_progress" and v.session.doc_id]
            if len(active) != 1:
                numbers = ", ".join(str(v.session.number) for v in active)
                which = (
                    "none is in progress"
                    if not active
                    else f"{len(active)} are in progress ({numbers})"
                )
                raise UsageError(
                    f"Cannot tell which {label} {learner.name}'s recording belongs to: {which}.",
                    remedy="Re-run with --session N. Nothing was written.",
                )
            view = active[0]
        doc_id = view.session.doc_id
        work = works[pick - 1]
        if ctx.args.complete:
            view = _recording_session(ctx, store, learner, view.session.number)

    if ctx.args.dry_run:
        message, would = (
            _recording_preview(ctx, learner, view, work)
            if ctx.args.complete
            else ("", recording_blocks(work))
        )
        ctx.report.result(
            {
                "learner": learner.to_dict(),
                "work": work.to_dict(),
                "doc_id": doc_id,
                "would_append": len(would),
                "would_complete": view.session.number if ctx.args.complete else None,
                "would_message": message,
                "dry_run": True,
            },
            human=(
                f"Would publish “{work.title}” as {learner.name}'s {label} {view.session.number}"
                if ctx.args.complete
                else f"Would write {len(would)} blocks for “{work.title}” onto "
                f"{learner.name}'s {label} {view.session.number}."
            ),
        )
        return Exit.OK

    if ctx.args.complete:
        result = complete_recording_work(ctx.config, learner, view, work)
        ctx.report.result(
            result,
            human=(
                f"Published “{work.title}” as {learner.name}'s {label} "
                f"{view.session.number}: {result['appended']} blocks written, "
                f"the {label} is done"
            ),
        )
        return Exit.OK

    result = attach_work(open_docs(ctx.config), doc_id, work)
    ctx.report.result(
        {"learner": learner.to_dict(), "work": work.to_dict(), **result},
        human=(
            f"Attached “{work.title}” to {learner.name}'s {label} "
            f"{view.session.number}: {result['appended']} blocks written"
            + (
                f", {len(result['already_on_page'])} already there"
                if result["already_on_page"]
                else ""
            )
        ),
    )
    return Exit.OK


def handle_pieces(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        pieces = store.list_pieces()

    payload = {"pieces": [item.to_dict() for item in pieces], "count": len(pieces)}
    if not pieces:
        ctx.report.result(payload, human=f"No {ctx.config.label('pieces')} recorded.")
        return Exit.OK

    width = max(len(item.title) for item in pieces)
    lines = []
    for item in pieces:
        extras = []
        if item.practice_track:
            extras.append("track")
        if item.sheet_link:
            extras.append("sheet")
        lines.append(f"  {item.id:>4}  {item.title:<{width}}  {' '.join(extras)}")
    ctx.report.result(payload, human="\n".join(lines))
    return Exit.OK


def handle_assign(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, ctx.args.name)
        piece_id = ctx.args.piece or None

        piece = None
        if piece_id:
            piece = store.get_piece(piece_id)
            if piece is None:
                raise UsageError(
                    f"No {ctx.config.label('piece')} has id `{piece_id}`.",
                    remedy="Run `baton learner pieces` to see the ids that exist.",
                )

        target = piece.title if piece else "nothing"
        published_plan: dict[str, Any] | None = None
        updater: PublishedPieceUpdater | None = None
        if ctx.args.update_published:
            updater = PublishedPieceUpdater(
                open_docs(ctx.config),
                PublishedRecord(ctx.config.state_dir / "published"),
                PreservePolicy.from_config(ctx.config.get("docs.preserve", [])),
            )
            published_plan = updater.plan(
                store.list_sessions(learner.id),
                from_piece_id=learner.current_piece_id,
                to_piece=piece,
            )

        if ctx.args.dry_run:
            visible_plan = (
                {key: value for key, value in published_plan.items() if key != "_plans"}
                if published_plan is not None
                else None
            )
            page_count = int((visible_plan or {}).get("would_update", 0))
            ctx.report.result(
                {
                    "learner": learner.to_dict(),
                    "would_assign": piece_id,
                    "published_updates": visible_plan,
                    "dry_run": True,
                },
                human=f"Would set {learner.name} to {target}"
                + (f" and update {page_count} published page(s)." if visible_plan else "."),
            )
            return Exit.OK

        published_updates = updater.apply(published_plan) if updater and published_plan else None
        store.set_current_piece(learner.id, piece_id)

    ctx.report.result(
        {
            "learner": learner.to_dict(),
            "assigned": piece_id,
            "published_updates": published_updates,
        },
        human=f"{learner.name} is now working on {target}."
        + (
            f" Updated {published_updates['updated']} published page(s)."
            if published_updates is not None
            else ""
        ),
    )
    return Exit.OK


def _set_active_many(ctx: Context, names: list[str], *, active: bool) -> Exit:
    """Flip one status per named learner, after every name has resolved.

    Nothing is written until the whole batch resolves: a typo in the last
    name must not leave the first three already marked, and re-running the
    corrected command is then exactly what it says it is.
    """
    with store_scope(ctx.config) as store:
        resolved = [_resolve(ctx, store, name) for name in names]

        written: list[Learner] = []
        for learner in resolved:
            try:
                store.set_active(learner.id, active)
            except BatonError as exc:
                raise UpstreamError(
                    f"{len(written)} of {len(resolved)} status changes were written "
                    f"before one failed: {exc.message}",
                    service="db",
                    remedy="Check what landed with `baton learner list --all`, "
                    "then re-run the names that are still missing.",
                    details={
                        "written": [item.to_dict() for item in written],
                        "remaining": [item.name for item in resolved[len(written) :]],
                    },
                ) from exc
            # The store keeps its own truth; read the flag back from the row
            # the store just confirmed rather than guessing its shape.
            fresh = store.get_learner(learner.id)
            written.append(fresh if fresh is not None else learner)

    verb = "studying here again" if active else "no longer studying"
    names_text = ", ".join(item.name for item in written)
    human = (
        f"Marked {len(written)} {ctx.config.label('learners')} as {verb}: {names_text}."
        if len(written) != 1
        else f"Marked {names_text} as {verb}."
    )
    ctx.report.result(
        {"active": active, "count": len(written), "learners": [item.to_dict() for item in written]},
        human=human,
    )
    return Exit.OK


def handle_activate(ctx: Context) -> Exit:
    if not ctx.args.names:
        raise UsageError(
            "`baton learner activate` needs at least one name.",
            remedy='Try `baton learner activate "Clara Nguyen"`.',
        )
    return _set_active_many(ctx, ctx.args.names, active=True)


def handle_deactivate(ctx: Context) -> Exit:
    if ctx.args.serve and ctx.args.names:
        raise UsageError(
            "`baton learner deactivate --serve` takes no names.",
            remedy="Tick the learners in the checklist it opens, or drop --serve.",
        )
    if not ctx.args.serve and not ctx.args.names:
        raise UsageError(
            "`baton learner deactivate` needs names, or --serve for the checklist.",
            remedy='Try `baton learner deactivate "Clara Nguyen"`, or --serve.',
        )
    if ctx.args.serve:
        # Imported here, not at the top, so the CLI's import cost stays flat
        # for everyone who never serves a checklist (the `_ask` precedent).
        from .serve import run_checklist

        return run_checklist(ctx, port=ctx.args.port)
    return _set_active_many(ctx, ctx.args.names, active=False)
