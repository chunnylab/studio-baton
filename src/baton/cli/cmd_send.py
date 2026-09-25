"""``baton send``: deliver a lesson message, or refuse to.

One learner or several, one invocation either way. The original skill's rule
holds: when a user names several learners, they all go through one command:
opening a terminal per learner is how sends got lost.
"""

from __future__ import annotations

import argparse
import contextlib
from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any

from .. import contracts
from ..adapters.cal import open_calendar
from ..adapters.chat import open_chat
from ..adapters.chat.base import Messenger, resolve_contact
from ..adapters.chat.guard import GuardedMessenger
from ..adapters.db import store_scope
from ..adapters.docs import VIDEO_LINK_BLOCKS, find_video_link, open_docs
from ..core.receipts import DEFAULT_WINDOW_HOURS, Receipts
from ..core.video_waivers import DEFAULT_TTL_MINUTES, VideoWaivers, generate_code
from ..domain.localdate import DateFormat
from ..domain.models import Learner, Work
from ..domain.prep import SectionRules
from ..domain.resolve import resolve_learner
from ..domain.status import StatusVocabulary
from ..domain.whenever import parse_date
from ..errors import BatonError, ConfigError, GateError, NeedsHumanError, StateError, UsageError
from ..exits import Exit
from ..pipelines.learner import LearnerHistory
from ..pipelines.lesson_video import send_video
from ..pipelines.recording import list_candidates, send_recording
from ..pipelines.schedule import Scheduler
from ..pipelines.send import evaluate, gather_context, send_lesson
from ..pipelines.staging import (
    PUBLISHED,
    RECORDING,
    STAGED,
    SUMMARISED,
    PieceSnapshot,
    PublishedRecord,
    StagingStore,
)
from .guard import guarded
from .naming import warn_if_inactive

if TYPE_CHECKING:
    from .app import Context


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``send`` command group."""
    parser = subparsers.add_parser(
        "send",
        help="Send a lesson message, refusing when required data is missing.",
        description=(
            "Sends the summary or recording that was published, never a "
            "re-derived one. A missing required field blocks the send with exit 5 "
            "and there is no override, with one exception: a session with no recording link "
            "stops on exit 3 and asks. `send video-waiver` texts a confirmation "
            "code to a configured contact, and --without-video <code> is where "
            "that person's answer sends the lesson with no video section, not "
            "a flag anything running the CLI can pass on its own. A message "
            "that already went out is refused the same way, and that one a "
            "person can override with --again."
        ),
    )
    group = parser.add_subparsers(dest="send_command", metavar="<subcommand>")
    parser.set_defaults(handler=_require_subcommand)

    lesson = group.add_parser("lesson", help="Send one learner's published lesson message.")
    lesson.add_argument("name", metavar="NAME")
    lesson.add_argument("--to", metavar="CONTACT", required=True, help="Configured contact name.")
    lesson.add_argument(
        "--session", type=int, default=None, help="Defaults to the latest published session."
    )
    lesson.add_argument("--dry-run", action="store_true", help="Run the gate and stop.")
    lesson.add_argument(
        "--without-video",
        metavar="CODE",
        default=None,
        help="Send the message with no video section when the session has no "
        "recording link. CODE is the confirmation code `send video-waiver` "
        "texted to a person for this exact learner and session; a session "
        "that does have a recording keeps it regardless.",
    )
    lesson.add_argument(
        "--again",
        action="store_true",
        help="Send even if an identical message already went out. For a "
        "person who has confirmed the first one never arrived.",
    )
    lesson.set_defaults(handler=handle_lesson)

    waiver = group.add_parser(
        "video-waiver",
        help="Text a confirmation code for sending a lesson with no video.",
        description=(
            "For a session `send lesson` refused on exit 3 because no "
            "recording is on the document. Texts a one-time code to --to, or "
            "to `summary.video_waiver.recipient` from the profile when --to "
            "is omitted, through the studio's own messenger and stops: the "
            "code is never printed or returned here, only sent. A person "
            "reads it off their phone and gives it back as `send lesson "
            "... --without-video <code>`, which is the only way the "
            "confirmation re-enters Baton. The code answers this learner's "
            "this session alone and expires "
            f"after {DEFAULT_TTL_MINUTES:g} minutes unanswered."
        ),
    )
    waiver.add_argument("name", metavar="NAME")
    waiver.add_argument(
        "--to",
        metavar="CONTACT",
        help=(
            "Configured contact name. Defaults to "
            "summary.video_waiver.recipient from the profile, which names "
            "the person who answers for the studio rather than leaving that "
            "choice to whoever runs the command."
        ),
    )
    waiver.add_argument(
        "--session", type=int, default=None, help="Defaults to the latest published session."
    )
    waiver.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the message and code that would be sent, and send nothing. "
        "Unlike a real request, the code appears here: this is a person "
        "previewing the flow, not the flow itself.",
    )
    waiver.set_defaults(handler=handle_video_waiver)

    recording = group.add_parser(
        "recording",
        help="Send a recorded work's Drive/YouTube links to one learner's contact.",
        description=(
            "Lists the learner's recorded works and sends the chosen one's "
            "links. Two invocations by design: without --pick the command ends "
            "at exit 3 carrying the numbered list; --pick <n> then delivers "
            "exactly that work. Whatever side of the recording is missing is "
            "simply not sent."
        ),
    )
    recording.add_argument("name", metavar="NAME")
    # Not required at parse time: the listing half needs no recipient at all.
    # Only a picked send goes anywhere.
    recording.add_argument(
        "--to", metavar="CONTACT", help="Configured contact name. Required once --pick is used."
    )
    recording.add_argument(
        "--pick",
        type=int,
        default=None,
        metavar="N",
        help="Send the Nth recording from the newest-first list (1 is the latest).",
    )
    recording.add_argument("--dry-run", action="store_true", help="Compose the message and stop.")
    recording.add_argument(
        "--again",
        action="store_true",
        help="Send even if an identical message already went out. For a "
        "person who has confirmed the first one never arrived.",
    )
    recording.set_defaults(handler=handle_recording)

    batch = group.add_parser("batch", help="Send several learners' messages in one invocation.")
    batch.add_argument("--to", metavar="CONTACT", required=True)
    batch.add_argument(
        "--learner",
        action="append",
        dest="learners",
        metavar="NAME",
        required=True,
        help="One learner per flag. Pass the same command once for a whole day.",
    )
    batch.add_argument("--dry-run", action="store_true")
    # No --without-video here. A code answers one learner's one session, and
    # a batch has many of both: there is no single value that could mean
    # "yes" for all of them. A learner with no recording is reported blocked,
    # same as any other refusal in a batch, and sent alone once a person has
    # requested and answered that one's own waiver.
    batch.add_argument(
        "--again",
        action="store_true",
        help="Send even if an identical message already went out. For a "
        "person who has confirmed the first one never arrived.",
    )
    batch.set_defaults(handler=handle_batch)

    video = group.add_parser(
        "video",
        help="Send a lesson's video by itself, not the whole summary again.",
        description=(
            "The message for a parent who asked for the video: instrument "
            "header, week and date, the titles, a taste of the summary, the "
            "link. Refuses (exit 5) when the session has no video on it: "
            "that is the whole point of the message."
        ),
    )
    video.add_argument("name", metavar="NAME")
    video.add_argument("--to", metavar="CONTACT", required=True, help="Configured contact name.")
    video.add_argument(
        "--session", type=int, default=None, help="Defaults to the latest published session."
    )
    video.add_argument("--dry-run", action="store_true", help="Compose the message and stop.")
    video.add_argument(
        "--again",
        action="store_true",
        help="Send even if an identical message already went out. For a "
        "person who has confirmed the first one never arrived.",
    )
    video.set_defaults(handler=handle_video)

    readiness = group.add_parser(
        "readiness",
        help="Who is booked today, and what would still block their message.",
        description=(
            "One row per learner with a lesson on DATE, read before the sends "
            "start. The last column is the send gate's own verdict, recomputed "
            "from the published record: what it names as missing is exactly "
            "what `send lesson` would refuse on."
        ),
    )
    readiness.add_argument(
        "--date", metavar="DATE", default="today", help="YYYY-MM-DD, today, tomorrow, +2, …"
    )
    readiness.set_defaults(handler=handle_readiness)

    aftermath = group.add_parser(
        "aftermath",
        help="What a teaching day left behind: stuck drafts, orphans, unsent messages.",
        description=(
            "Run after the sends. Reports three kinds of leftovers: drafts that "
            "never reached publish, draft files whose learner no longer exists, "
            "and published lessons with no send receipt. A receipt only proves a "
            "send within its window: beyond that this reports the absence of "
            "evidence, never the certainty that nothing went out."
        ),
    )
    aftermath.add_argument(
        "--date", metavar="DATE", default="today", help="YYYY-MM-DD, today, tomorrow, +2, …"
    )
    aftermath.set_defaults(handler=handle_aftermath)

    contacts = group.add_parser("contacts", help="List configured contacts.")
    contacts.set_defaults(handler=handle_contacts)


def _require_subcommand(ctx: Context) -> Exit:
    raise UsageError(
        "`baton send` needs a subcommand.",
        remedy="Try `baton send lesson <name> --to <contact>`.",
    )


def _resolve(ctx: Context, store, name: str):
    learner = resolve_learner(
        name,
        store.list_learners(),
        aliases=ctx.config.get("db.aliases", {}) or {},
        label=ctx.config.label("learner"),
    )
    warn_if_inactive(ctx, learner)
    return learner


def _required_optional(ctx: Context) -> tuple[list[str], list[str]]:
    gates = ctx.config.section("gates")
    required = [str(item) for item in gates.get("send_lesson_required", [])]
    optional = [str(item) for item in gates.get("send_lesson_optional", [])]
    return required, optional


def _date_format(ctx: Context) -> DateFormat:
    """How this studio writes a lesson date for the families it teaches.

    Empty by default, which passes the document's own value through unchanged.
    The studio this came from writes "23 ส.ค. 2569", and the message said
    "2026-08-23" from the rewrite until this existed.
    """
    return DateFormat.from_config(ctx.config.section("chat.date"))


def _date_and_titles(docs, doc_id: str) -> tuple[str, str]:
    """The document's own date and titles, read fresh rather than from the
    published record: both can be corrected on the document after the
    summary went out, and the message should reflect the current page.

    Same degrade-quietly stance as `find_video_link`: these are cosmetic, not
    gated, so a document-store hiccup here must not block a send that would
    otherwise go.
    """
    if not doc_id:
        return "", ""
    try:
        status = docs.get_status(doc_id, with_blocks=False)
    except BatonError:
        return "", ""
    return status.date, status.titles


def _piece_sources(store, learner: Learner | None, published: Mapping[str, Any]) -> tuple[str, ...]:
    """URLs on the page that are the piece being learnt, not this recording.

    A studio keeps the song on the same page as the lesson, and Notion turns a
    pasted song URL into a bookmark and an embed. Both are shapes
    `find_video_link` reads, and a song on YouTube is indistinguishable from a
    recording on YouTube by its URL alone, which is how a lesson message once
    went out carrying the link to the song's official music video.

    Both the piece the lesson was taught on and the one the learner is on now
    are named, because a song can be changed after the fact: the page is
    corrected to the new source while the record still holds the old one, and
    either can be the link sitting on the page.

    Degrades to naming fewer URLs rather than raising. Everything it protects
    against is a wrong link, and refusing to send at all is worse than the
    ordering rule in `find_video_link` catching the same case on its own.
    """
    links: list[str] = []
    with contextlib.suppress(BatonError):
        snapshot = PieceSnapshot.from_record(published)
        if snapshot.piece is not None and snapshot.piece.source_link:
            links.append(snapshot.piece.source_link)
    if learner is not None and learner.current_piece_id:
        with contextlib.suppress(BatonError):
            piece = store.get_piece(learner.current_piece_id)
            if piece is not None and piece.source_link:
                links.append(piece.source_link)
    return tuple(links)


def _messenger(ctx: Context, *, what: str, key: str) -> Messenger:
    """The configured messenger, wrapped so one message cannot go out twice.

    Every send path in this module goes through here. That is the point: the
    duplicate a harness causes (killing the command in the gap between a
    platform accepting the message and Baton printing that it did) is not
    something any one composer can see coming.

    Args:
        what: How a refusal names the message to a person.
        key: What the message *is*, independent of its wording. Never the text:
            `send lesson` varies its opening and closing phrase deliberately,
            so two sends of one summary are two different strings and only an
            identity can recognise them as the same message.
    """
    window = float(ctx.config.get("chat.duplicate_window_hours", DEFAULT_WINDOW_HOURS))
    return GuardedMessenger(
        open_chat(ctx.config),
        Receipts.for_state(ctx.config.state_dir, window),
        again=bool(getattr(ctx.args, "again", False)),
        what=what,
        key=key,
    )


def _lesson_receipt_key(learner_id: str, session_number: Any) -> str:
    """The identity of one learner's one-session lesson message.

    Shared by `send lesson` and `send aftermath`, because the question
    aftermath asks of the receipts: did this message go out?: is only
    answerable if it is spelled exactly the way the send spelled it. Two
    spellings here would quietly mean two different messages, and aftermath
    would report sends as missing that went out.
    """
    return f"lesson|{learner_id}|{session_number}"


def _recording_receipt_key(learner_id: str, work: Work | Mapping[str, Any]) -> str:
    work_id = work.id if isinstance(work, Work) else str(work.get("id", ""))
    title = work.title if isinstance(work, Work) else str(work.get("title", ""))
    return f"recording|{learner_id}|{work_id or title}"


def _published_receipt_key(
    learner_id: str, session_number: Any, published: Mapping[str, Any]
) -> str:
    if _published_kind(published) == RECORDING:
        work = published.get("work")
        return _recording_receipt_key(
            learner_id, work if isinstance(work, dict) else {"id": "", "title": ""}
        )
    return _lesson_receipt_key(learner_id, session_number)


def _day(ctx: Context, value: str) -> date:
    """A day argument, in the same grammar `calendar book` accepts."""
    return parse_date(
        value,
        timezone=ctx.config.timezone,
        shorthand=ctx.config.section("calendar.date_shorthand"),
        weekdays=ctx.config.section("calendar.weekdays"),
        accept_dmy=bool(ctx.config.get("calendar.accept_dmy", False)),
    )


def _roster_for_day(
    ctx: Context, store, docs, day: date
) -> tuple[list[Learner], list[dict[str, Any]], str]:
    """Who had a lesson on ``day``: the calendar's roster, or the documents'.

    The calendar is the source when there is one: its events were typed by a
    person and name the day directly. Without a calendar configured, the
    roster falls back to the sessions whose documents carry the day's date:
    a weaker claim (a document's date can be blank or mistyped) and labelled
    as such in the report, because a fallback nobody knows is a fallback that
    gets trusted past its worth.

    Returns:
        ``(learners, unmatched_events, source)`` where source is
        ``"calendar"`` or ``"documents"``.
    """
    vocabulary = StatusVocabulary.from_config(ctx.config.section("docs.statuses"))
    try:
        scheduler = Scheduler(
            open_calendar(ctx.config),
            docs,
            vocabulary,
            timezone=ctx.config.timezone,
            session_label=ctx.config.label("session"),
            # The same icons `baton calendar` writes titles with. Without
            # them the title match has only the bare `Name (` prefix to try,
            # so every event this studio books reads as unmatched and both
            # reports show an empty roster on a full teaching day.
            event_emoji=ctx.config.section("calendar.event_emoji"),
            default_emoji=str(ctx.config.get("calendar.default_event_emoji", "")),
        )
        learners, unmatched = scheduler.who_is_booked(store, day)
        return learners, unmatched, "calendar"
    except ConfigError:
        history = LearnerHistory(store, docs, vocabulary)
        learners = []
        # Same scope as the calendar roster above it: a learner who stopped
        # studying does not come back into the day's send list just because
        # the calendar is missing.
        for learner in store.list_learners():
            if not learner.is_active:
                continue
            for view in history.sessions(learner):
                if str(view.doc.date or "")[:10] == day.isoformat():
                    learners.append(learner)
                    break
        return learners, [], "documents"


def _published_kind(published: Mapping[str, Any]) -> str:
    return str(published.get("kind") or "summary")


def _send_recording_record(
    ctx: Context, learner: Learner, published: Mapping[str, Any], *, dry_run: bool
) -> dict[str, Any]:
    """Send the recording a recording-only publish recorded."""

    raw_work = published.get("work")
    if not isinstance(raw_work, dict):
        raise StateError(
            f"The recording lesson for {learner.name} has no work snapshot.",
            remedy="Re-run `learner attach-work --complete` for the chosen work.",
        )
    work = Work.from_dict(raw_work)
    session_number = int(published.get("session_number", 0) or 0)
    doc_url = str(published.get("doc_url", ""))

    missing: list[dict[str, str]] = []
    if not work.id.strip() and not work.title.strip():
        missing.append({"field": "work_identity"})
    if not work.video_link.strip() and not work.drive_link.strip():
        missing.append({"field": "recording_link"})
    if not doc_url:
        missing.append({"field": "doc_link"})
    if not session_number:
        missing.append({"field": "session_number"})
    if missing:
        raise GateError(
            f"The recording lesson for {learner.name} is not sendable.",
            missing=missing,
            remedy="Fix the recorded work or its published record, then send again.",
        )

    messenger = _messenger(
        ctx,
        what=f"{learner.name}'s {ctx.config.label('session')} {session_number} recording",
        key=_recording_receipt_key(learner.id, work),
    )
    recipient_id = messenger.resolve(ctx.args.to)
    result = send_recording(
        messenger,
        recipient_id=recipient_id,
        work=work,
        learner_name=learner.name,
        instrument=learner.instrument,
        date=_date_format(ctx).of_text(work.performed_date),
        doc_url=doc_url,
        dry_run=dry_run,
        message=str(published.get("short_message", "")),
    )
    result["session_number"] = session_number
    result["kind"] = RECORDING
    return result


def _send_one(
    ctx: Context,
    name: str,
    *,
    session: int | None,
    dry_run: bool,
    resolved: Learner | None = None,
) -> dict[str, Any]:
    """Resolve, gather, gate, and send for one learner. Raises on any refusal.

    ``resolved`` lets a batch pass in a learner it resolved up front, so the
    duplicate check and the send act on the same person: the check cannot
    compare raw strings and the send compare records.
    """
    with store_scope(ctx.config) as store:
        learner = resolved if resolved is not None else _resolve(ctx, store, name)
        records = PublishedRecord(ctx.config.state_dir / "published")

        if session is not None:
            published = records.get(learner.id, session)
            if published is None:
                raise UsageError(
                    f"No published lesson for {learner.name} at "
                    f"{ctx.config.label('session')} {session}.",
                    remedy=f'Run `baton lesson publish "{learner.name}"` first.',
                )
        else:
            published = records.latest(learner.id)
            if published is None:
                raise UsageError(
                    f"Nothing has been published for {learner.name} yet.",
                    remedy=f'Run `baton lesson publish "{learner.name}"` first.',
                )

        if _published_kind(published) == RECORDING:
            return _send_recording_record(ctx, learner, published, dry_run=dry_run)

        # Built here, not at the top: the identity of this message is the
        # learner and the session it publishes, and neither is known until the
        # name has been resolved and the published record found.
        label = ctx.config.label("session")
        session_number = published.get("session_number")
        messenger = _messenger(
            ctx,
            what=f"{learner.name}'s {label} {session_number} summary",
            key=_lesson_receipt_key(learner.id, session_number),
        )
        recipient_id = messenger.resolve(ctx.args.to)
        docs = open_docs(ctx.config)
        doc_id = str(published.get("doc_id", ""))
        video_link = find_video_link(
            docs,
            doc_id,
            blocks=tuple(
                str(item) for item in ctx.config.get("docs.video_link_blocks", VIDEO_LINK_BLOCKS)
            ),
            exclude=_piece_sources(store, learner, published),
        )
        date, titles = _date_and_titles(docs, doc_id)
        date = _date_format(ctx).of_text(date)

        required, optional = _required_optional(ctx)
        if not video_link and "video_link" in required:
            # The studio's gate requires the recording, and this session has
            # none on its document. That is a person's call to make, not a gap
            # to fix by default: some lessons genuinely were not filmed.
            #
            # The confirmed answer is a code, not a bare flag. A bare
            # --without-video is something anything running this CLI can pass,
            # including an agent that never asked anyone and is following the
            # contract faithfully by doing so. A code is different: only
            # `send video-waiver` can mint one, only the studio's own
            # messenger ever carries it, and only a person reading their own
            # phone has it to give back. Matching one moves video_link to
            # optional for this one invocation, so the send still warns about
            # it and the message still leaves the video section out (an empty
            # link composes no video line). Nothing else can be relaxed this
            # way; the other required fields keep the hard, unoverridable
            # block, and `candidates` stays empty here for the same reason it
            # does on the no-code path below: this is not data to pick from.
            code = getattr(ctx.args, "without_video", None)
            approver = str(ctx.config.get("summary.video_waiver.recipient", "") or "").strip()
            if code:
                waivers = VideoWaivers.for_state(
                    ctx.config.state_dir,
                    float(ctx.config.get("summary.video_waiver.ttl_minutes", DEFAULT_TTL_MINUTES)),
                )
                waivers.verify_and_consume(
                    learner.id,
                    session_number,
                    code,
                    learner_name=learner.name,
                    label=label,
                    recipient_hint=approver,
                )
                required = [name for name in required if name != "video_link"]
                optional = [*optional, "video_link"]
            else:
                # The remedy is machine-read by whatever drives Baton, so who
                # it names matters as much as the command it gives. Echoing
                # `--to` back once steered a following agent into texting the
                # code to the lesson's own recipient: a person who could then
                # answer the owner's question without the owner ever seeing
                # it. With an approver configured, the remedy needs no --to at
                # all; without one, it says whose phone the code belongs on.
                if approver:
                    request = f'`baton send video-waiver "{learner.name}"`'
                    reaches = f"a confirmation code to {approver}"
                else:
                    request = f'`baton send video-waiver "{learner.name}" --to <contact>`'
                    reaches = (
                        "a confirmation code to the person who answers for "
                        "this studio, not whoever this message is addressed to"
                    )
                raise NeedsHumanError(
                    f"No recording link was found on the document for "
                    f"{learner.name}'s {label} {session_number} message, and "
                    f"the studio's gate requires one.",
                    candidates=[],
                    details={
                        "learner": learner.name,
                        "session_number": session_number,
                        "missing": ["video_link"],
                    },
                    remedy="Nothing was sent. Either put the recording on the "
                    "lesson document and re-run, or run "
                    f"{request} to text {reaches}, then re-run this with "
                    "--without-video <the code they were sent>.",
                )
        return send_lesson(
            messenger,
            store,
            recipient_id=recipient_id,
            learner_id=learner.id,
            published=published,
            video_link=video_link,
            date=date,
            titles=titles,
            required=required,
            optional=optional,
            dry_run=dry_run,
        )


@guarded("send")
def handle_lesson(ctx: Context) -> Exit:
    result = _send_one(ctx, ctx.args.name, session=ctx.args.session, dry_run=ctx.args.dry_run)

    verb = "would send" if ctx.args.dry_run else "sent"
    deliverable = "recording" if str(result.get("kind", "summary")) == RECORDING else "message"
    ctx.report.result(
        result,
        human=f"{verb.capitalize()} {deliverable} for {result['learner']} "
        f"({ctx.config.label('session')} {result['session_number']}) "
        f"to {ctx.args.to}"
        # `not result.get("sent")` has no reachable trigger today: every
        # Messenger.send() either raises (the send failed, and _send_one lets
        # that propagate before this point is ever reached) or returns
        # SendOutcome(sent=True, ...) -- no shipped driver or test fake
        # constructs sent=False. The field stays on SendOutcome for a future
        # driver that reports a failed-but-not-exceptional send (e.g. queued,
        # rejected without a hard error) without raising; this line is what
        # would surface that the moment one exists.
        + ("" if ctx.args.dry_run or result.get("sent") else "  ✗ NOT DELIVERED"),
    )
    return Exit.OK


@guarded("send")
def handle_video_waiver(ctx: Context) -> Exit:
    """Text a one-time code to a person, for one learner's one session.

    The code itself never appears in this command's own result, not in
    `--json`, not in the human line, not in a log line anywhere in Baton.
    Reaching it means reading the message it was sent in. That is the entire
    mechanism: nothing driving this CLI, agent or person, can answer the
    question `send lesson` asked without first having read that message.

    Who that person is comes from ``--to``, else from the profile's
    ``summary.video_waiver.recipient``. The default is deliberate: the
    question the code asks belongs to whoever answers for the studio, and a
    recipient chosen per invocation by whatever is holding the command line
    once went to the lesson's own addressee instead.
    """
    recipient = (
        ctx.args.to or str(ctx.config.get("summary.video_waiver.recipient", "") or "").strip()
    )
    if not recipient:
        raise ConfigError(
            "`send video-waiver` has nobody to text a confirmation code to.",
            remedy=(
                "Pass `--to <contact>`, or set summary.video_waiver.recipient "
                "in the profile to the person who answers for this studio, so "
                "the code reaches the same decision-maker every time instead "
                "of whoever happens to run the command."
            ),
        )
    label = ctx.config.label("session")
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, ctx.args.name)
        records = PublishedRecord(ctx.config.state_dir / "published")
        if ctx.args.session is not None:
            published = records.get(learner.id, ctx.args.session)
            if published is None:
                raise UsageError(
                    f"No published {label} {ctx.args.session} for {learner.name}.",
                    remedy=f'Run `baton lesson publish "{ctx.args.name}"` first.',
                )
        else:
            published = records.latest(learner.id)
            if published is None:
                raise UsageError(
                    f"Nothing has been published for {learner.name} yet.",
                    remedy=f'Run `baton lesson publish "{ctx.args.name}"` first.',
                )
        piece_sources = _piece_sources(store, learner, published)

    session_number = int(published.get("session_number", 0) or 0)
    doc_id = str(published.get("doc_id", ""))

    docs = open_docs(ctx.config)
    video_link = find_video_link(
        docs,
        doc_id,
        blocks=tuple(
            str(item) for item in ctx.config.get("docs.video_link_blocks", VIDEO_LINK_BLOCKS)
        ),
        exclude=piece_sources,
    )
    if video_link:
        raise UsageError(
            f"{learner.name}'s {label} {session_number} already has a recording on its document.",
            remedy="Nothing to waive: `send lesson` will send it with the video section as usual.",
        )

    messenger = open_chat(ctx.config)
    recipient_id = messenger.resolve(recipient)
    ttl_minutes = float(ctx.config.get("summary.video_waiver.ttl_minutes", DEFAULT_TTL_MINUTES))

    def _message(code: str) -> str:
        return (
            f"Baton: {learner.name}'s {label} {session_number} has no recording "
            f"on its document. Reply this code to send the summary without one: "
            f"{code}\nExpires in {ttl_minutes:g} minutes. Ignore this if that "
            f"wasn't your decision."
        )

    if ctx.args.dry_run:
        preview = _message(generate_code())
        ctx.report.result(
            {
                "ok": True,
                "dry_run": True,
                "learner": learner.name,
                "session_number": session_number,
                "would_send_to": recipient,
                "message": preview,
            },
            human=f"Would text a confirmation code to {recipient} for "
            f"{learner.name}'s {label} {session_number}:\n\n{preview}",
        )
        return Exit.OK

    waivers = VideoWaivers.for_state(ctx.config.state_dir, ttl_minutes)
    code = waivers.request(learner.id, session_number, sent_to=recipient)
    messenger.send(recipient_id, _message(code))

    ctx.report.result(
        {
            "ok": True,
            "learner": learner.name,
            "session_number": session_number,
            "sent_to": recipient,
            "expires_in_minutes": ttl_minutes,
        },
        human=f"Texted a confirmation code to {recipient} for {learner.name}'s "
        f"{label} {session_number}. It expires in {ttl_minutes:g} minutes.",
    )
    return Exit.OK


@guarded("send", when=lambda ctx: ctx.args.pick is not None)
def handle_recording(ctx: Context) -> Exit:
    """List a learner's recorded works, or deliver the picked one's links.

    Two invocations and no prompt, because whatever runs Baton drives it with
    commands: the studio's agent relays the list to the person choosing, then
    answers with ``--pick``. Each call reads the store fresh: nothing is
    remembered between them, so a pick always lands on the row the list the
    person answered from was built from.
    """
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, ctx.args.name)
        works = store.list_works(learner.id)

        pick = ctx.args.pick
        if pick is None:
            if not works:
                raise GateError(
                    f"{learner.name} has no recorded work to send.",
                    remedy="Record one first with `baton learner add-work "
                    f'"{ctx.args.name}" --title … (--video-link / --drive-link)`.',
                )
            # Which recording goes out is a person's call: the date ordering
            # suggests, but cannot know, what the parent meant.
            raise NeedsHumanError(
                f"{learner.name} has {len(works)} recorded work(s): which one goes out?",
                candidates=list_candidates(works),
                remedy="Re-run with --pick <number> from that list; 1 is the "
                "most recent. Nothing has been sent.",
            )

        if ctx.args.to is None:
            raise UsageError(
                "A picked recording needs somewhere to go.",
                remedy="Re-run with --to <contact> alongside --pick.",
                details={"candidates": list_candidates(works)},
            )

        if not 1 <= pick <= len(works):
            raise UsageError(
                f"--pick {pick} does not match any of {learner.name}'s "
                f"{len(works)} recorded work(s).",
                remedy=(
                    "--pick counts the newest-first list: 1 is the latest. "
                    "Re-run without --pick to see it."
                    if works
                    else f"No recording exists yet: record one first with "
                    f'`baton learner add-work "{ctx.args.name}" --title …`.'
                ),
                details={"candidates": list_candidates(works)} if works else None,
            )
        index = pick - 1

    # The lesson page the recording belongs to, when one is published: the
    # record already exists, and after a publish it *is* "the latest lesson".
    # Read fail-open, nothing has been published yet, or the record cannot be
    # read, and the links still go out the way the old sender sent them.
    doc_url = ""
    with contextlib.suppress(BatonError):
        latest = PublishedRecord(ctx.config.state_dir / "published").latest(learner.id)
        doc_url = str((latest or {}).get("doc_url", ""))

    # The contact resolves here rather than before the gate: a wrong contact
    # name should not be the error that masks "there is nothing to send".
    work = works[index]
    messenger = _messenger(
        ctx,
        what=f"{learner.name}'s recording “{work.title}”",
        key=_recording_receipt_key(learner.id, work),
    )
    recipient_id = messenger.resolve(ctx.args.to)
    result = send_recording(
        messenger,
        recipient_id=recipient_id,
        work=work,
        learner_name=learner.name,
        instrument=learner.instrument,
        date=_date_format(ctx).of_text(work.performed_date),
        doc_url=doc_url,
        dry_run=ctx.args.dry_run,
    )

    verb = "would send" if ctx.args.dry_run else "sent"
    delivered = "" if ctx.args.dry_run or result.get("sent") else "  ✗ NOT DELIVERED"
    ctx.report.result(
        result,
        human=f"{verb.capitalize()} the recording “{work.title}” for {learner.name} "
        f"to {ctx.args.to}{delivered}",
    )
    return Exit.OK


@guarded("send")
def handle_batch(ctx: Context) -> Exit:
    """Every learner in one invocation, reported together.

    One refusal must not abandon the rest (the teacher still wants the others
    sent), but the summary at the end has to say exactly which did not go.
    """
    requested = list(ctx.args.learners)

    # Resolve the whole batch before anything is sent. Two different strings
    # can name one learner through `db.aliases`: a nickname and the same
    # nickname with `น้อง` in front of it, and a
    # comparison of the raw names lets that pair through, after which each
    # entry sends its own message. The duplicate that matters is the person,
    # not the spelling.
    with store_scope(ctx.config) as store:
        resolved: list[Learner] = []
        for name in requested:
            resolved.append(_resolve(ctx, store, name))

    duplicates = sorted(
        {
            learner.name
            for learner in resolved
            if [item.id for item in resolved].count(learner.id) > 1
        }
    )
    if duplicates:
        raise UsageError(
            "The same learner appears twice in this batch: " + ", ".join(duplicates) + ".",
            remedy="Remove the duplicate; sending twice is what it would cause.",
        )

    results: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    sent = 0

    for name, learner in zip(requested, resolved, strict=True):
        latest = PublishedRecord(ctx.config.state_dir / "published").latest(learner.id)
        try:
            result = _send_one(ctx, name, session=None, dry_run=ctx.args.dry_run, resolved=learner)
            results.append(result)
            if result.get("sent"):
                sent += 1
        except BatonError as err:
            blocked.append(
                {
                    "learner": name,
                    "kind": _published_kind(latest) if latest is not None else "summary",
                    "error": err.to_dict(),
                }
            )

    payload = {
        "requested": len(requested),
        "sent": sent,
        "ready": len(results) - sent,
        "blocked": blocked,
        "results": results,
    }

    lines = [f"{len(requested)} requested, {sent} sent"]
    for item in blocked:
        lines.append(f"  ✗ {item['learner']}: {item['error']['message']}")
    if blocked and not ctx.args.dry_run:
        lines.append("Some messages did not go. Fix those and re-run for them alone.")
    ctx.report.result(payload, human="\n".join(lines))

    # A partial batch is not a failure of the ones that went out, but the exit
    # code must still say that not everything was sent.
    return Exit.OK if not blocked else Exit.GATE


@guarded("send")
def handle_video(ctx: Context) -> Exit:
    """The latest published session's video, sent on its own.

    Everything it needs already exists by the time a lesson is published: the
    record names the session, the document holds the recording, and the
    sections reader knows the lesson. Nothing here re-derives the published
    summary: a taste of it rides along, the whole thing stays one tap away.
    """
    label = ctx.config.label("session")
    # The store stays open past the published record because the piece it
    # names is what tells the song on the page apart from the recording.
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, ctx.args.name)

        records = PublishedRecord(ctx.config.state_dir / "published")
        if ctx.args.session is not None:
            published = records.get(learner.id, ctx.args.session)
            if published is None:
                raise UsageError(
                    f"No published {label} {ctx.args.session} for {learner.name}.",
                    remedy=f'Run `baton lesson publish "{ctx.args.name}"` first.',
                )
        else:
            published = records.latest(learner.id)
            if published is None:
                raise UsageError(
                    f"Nothing has been published for {learner.name} yet.",
                    remedy=f'Run `baton lesson publish "{ctx.args.name}"` first.',
                )
        piece_sources = _piece_sources(store, learner, published)

    session_number = int(published.get("session_number", 0) or 0)
    doc_id = str(published.get("doc_id", ""))

    docs = open_docs(ctx.config)
    video_link = find_video_link(
        docs,
        doc_id,
        blocks=tuple(
            str(item) for item in ctx.config.get("docs.video_link_blocks", VIDEO_LINK_BLOCKS)
        ),
        exclude=piece_sources,
    )
    date, titles = _date_and_titles(docs, doc_id)
    date = _date_format(ctx).of_text(date)
    if not titles:
        titles = str(published.get("titles", ""))

    # The same sections reader prep uses, so the taste matches what the
    # teacher reads before the lesson. Unreadable blocks taste of nothing:
    # the video still goes.
    sections: dict[str, str] = {}
    with contextlib.suppress(BatonError):
        sections = SectionRules.from_config(ctx.config).read(docs.list_blocks(doc_id))

    messenger = _messenger(
        ctx,
        what=f"{learner.name}'s {ctx.config.label('session')} {session_number} video",
        key=f"video|{learner.id}|{session_number}",
    )
    recipient_id = messenger.resolve(ctx.args.to)
    result = send_video(
        messenger,
        recipient_id=recipient_id,
        learner_name=learner.name,
        instrument=learner.instrument,
        session_number=session_number,
        date=date,
        titles=titles,
        video_link=video_link,
        summary_sections=sections,
        session_label=label,
        dry_run=ctx.args.dry_run,
    )

    verb = "would send" if ctx.args.dry_run else "sent"
    delivered = "" if ctx.args.dry_run or result.get("sent") else "  ✗ NOT DELIVERED"
    ctx.report.result(
        result,
        human=f"{verb.capitalize()} the video for {learner.name} "
        f"({label} {session_number}) to {ctx.args.to}{delivered}",
    )
    return Exit.OK


def handle_contacts(ctx: Context) -> Exit:
    contacts = ctx.config.section("chat.contacts")
    payload = {
        "contacts": [
            {"name": str(key), "aliases": list(entry.get("aliases", []))}
            for key, entry in contacts.items()
            if isinstance(entry, dict)
        ]
    }
    if not payload["contacts"]:
        ctx.report.result(payload, human="No contacts configured.")
        return Exit.OK

    lines = []
    for entry in payload["contacts"]:
        aliases = ", ".join(entry["aliases"]) if entry["aliases"] else "-"
        lines.append(f"  {entry['name']:<16} {aliases}")
    ctx.report.result(payload, human="\n".join(lines))
    return Exit.OK


_STAGING_STATES = {
    STAGED: "ยังไม่มีสรุป",
    SUMMARISED: "สรุปแล้ว รอ publish",
    PUBLISHED: "พร้อม (published)",
}


@guarded("send")
def handle_readiness(ctx: Context) -> Exit:
    """The day's sends, read before any of them start.

    The point of running this first is the order of the fixes it suggests:
    a missing video block is fixed on the document, a missing summary means
    going back to `lesson ingest`, and "ยังไม่ publish" means the whole send
    is premature. A report that mixed those layers together would have the
    operator fixing the wrong one first, exactly as happened the day a video
    block was hunted for on a lesson that had never been published.
    """
    day = _day(ctx, str(ctx.args.date))
    with store_scope(ctx.config) as store:
        docs = open_docs(ctx.config)
        learners, unmatched, source = _roster_for_day(ctx, store, docs, day)
        records = PublishedRecord(ctx.config.state_dir / "published")
        staging = StagingStore(ctx.config.state_dir / "lessons")
        required, optional = _required_optional(ctx)
        video_blocks = tuple(
            str(item) for item in ctx.config.get("docs.video_link_blocks", VIDEO_LINK_BLOCKS)
        )
        # The same pool `lesson ingest` warned over, recomputed from the
        # summary as stored, so the vocabulary column means "as published",
        # not "as the model first wrote it".
        vocabulary = [
            str(term)
            for term in ctx.config.get("summary.vocabulary", []) or []
            if str(term).strip()
        ]

        rows: list[dict[str, Any]] = []
        for learner in learners:
            draft = staging.get(learner.id)
            if draft is not None:
                staging_state = _STAGING_STATES.get(draft.status, draft.status)
                record = records.get(learner.id, draft.session_number)
            else:
                staging_state = "ไม่มี draft"
                record = None
            if record is None:
                record = records.latest(learner.id)

            missing: list[dict[str, Any]] = [{"field": "ยังไม่ publish"}]
            warnings: list[dict[str, Any]] = []
            video = ""
            near_misses: list[str] = []
            if record is not None:
                if _published_kind(record) == RECORDING:
                    raw_work = record.get("work")
                    work = Work.from_dict(raw_work) if isinstance(raw_work, dict) else None
                    video = work.video_link if work is not None else ""
                    if work is None:
                        missing = [{"field": "recording_work"}]
                    elif not work.video_link.strip() and not work.drive_link.strip():
                        missing = [{"field": "recording_link"}]
                    elif not str(record.get("doc_url", "")):
                        missing = [{"field": "doc_link"}]
                    else:
                        missing = []
                else:
                    video = find_video_link(
                        docs,
                        str(record.get("doc_id", "")),
                        blocks=video_blocks,
                        exclude=_piece_sources(store, learner, record),
                    )
                    summary = record.get("summary") or {}
                    if vocabulary and isinstance(summary, dict) and summary:
                        near_misses = contracts.vocabulary_near_misses(summary, vocabulary)
                    # The gate's own verdict, from the same `evaluate` the send
                    # refuses through, never a second opinion that could drift.
                    context = gather_context(store, learner.id, record, video_link=video)
                    missing, warnings = evaluate(context, required=required, optional=optional)

            rows.append(
                {
                    "learner": learner.name,
                    "kind": _published_kind(record) if record is not None else "summary",
                    "staging": staging_state,
                    "video_block": bool(video),
                    "vocabulary": (
                        "-"
                        if not vocabulary or record is None or _published_kind(record) == RECORDING
                        else ("ผ่าน" if not near_misses else "ไม่ผ่าน: " + ", ".join(near_misses))
                    ),
                    "missing": [str(item.get("field", "")) for item in missing],
                    "optional_missing": [str(item.get("field", "")) for item in warnings],
                }
            )

        ready = sum(1 for row in rows if not row["missing"])
        payload = {
            "date": day.isoformat(),
            "source": source,
            "learners": rows,
            "ready": ready,
            "total": len(rows),
            "unmatched": unmatched,
        }

        source_label = "ปฏิทิน" if source == "calendar" else "วันที่บนเอกสาร"
        lines = [f"คาบวันที่ {day.isoformat()} (อ่านจาก{source_label}): {len(rows)} คน"]
        for row in rows:
            gap = ", ".join(row["missing"]) or "-"
            if not row["missing"] and row["optional_missing"]:
                gap = "ไม่บล็อก: " + ", ".join(row["optional_missing"])
            lines.append(
                f"  {row['learner']}: {row['kind']} | staging {row['staging']} | "
                f"video {'มี' if row['video_block'] else 'ไม่มี'} | "
                f"vocab {row['vocabulary']} | ขาด {gap}"
            )
        lines.append(f"พร้อมส่ง {ready}/{len(rows)} คน")
        for event in unmatched:
            lines.append(
                f"  รายการในปฏิทินที่จับคู่ชื่อไม่ได้ (ระบบไม่เดาให้): "
                f"{event.get('title', '')} {event.get('start', '')}"
            )
        ctx.report.result(payload, human="\n".join(lines))
        return Exit.OK


@guarded("send")
def handle_aftermath(ctx: Context) -> Exit:
    """What a teaching day left behind, after the sends were supposed to run.

    Exit 0 whatever it finds: this is a report, not a gate. The findings name
    their own remedies elsewhere in Baton (publish, send), so refusing here
    would only teach the operator to stop running it.
    """
    day = _day(ctx, str(ctx.args.date))
    with store_scope(ctx.config) as store:
        docs = open_docs(ctx.config)
        staging = StagingStore(ctx.config.state_dir / "lessons")
        records = PublishedRecord(ctx.config.state_dir / "published")

        stuck: list[dict[str, Any]] = []
        orphans: list[dict[str, Any]] = []
        for draft in staging.list():
            learner = store.get_learner(draft.learner_id)
            if learner is None:
                orphans.append(
                    {
                        "learner_name": draft.learner_name,
                        "learner_id": draft.learner_id,
                        "session_number": draft.session_number,
                        "status": draft.status,
                    }
                )
                continue
            if draft.status != PUBLISHED:
                state = (
                    "ผลงานยังไม่ publish"
                    if draft.kind == RECORDING
                    else ("ยังไม่มีสรุป" if draft.summary is None else "สรุปแล้ว ยังไม่ publish")
                )
                stuck.append(
                    {
                        "learner": learner.name,
                        "session_number": draft.session_number,
                        "kind": draft.kind,
                        "state": state,
                    }
                )
                continue
            unfinished = {
                name: state.get("status", "")
                for name, state in draft.targets.items()
                if state.get("status") != "ok"
            }
            if unfinished:
                stuck.append(
                    {
                        "learner": learner.name,
                        "session_number": draft.session_number,
                        "state": "publish แล้ว target ค้าง: "
                        + ", ".join(f"{name}={state}" for name, state in unfinished.items()),
                    }
                )

        learners, unmatched, source = _roster_for_day(ctx, store, docs, day)

        window = float(ctx.config.get("chat.duplicate_window_hours", DEFAULT_WINDOW_HOURS))
        receipts = Receipts.for_state(ctx.config.state_dir, window)
        contacts = {
            str(key): entry
            for key, entry in ctx.config.section("chat.contacts").items()
            if isinstance(entry, dict)
        }

        service = ""
        chat_unavailable = ""
        try:
            service = str(getattr(open_chat(ctx.config), "service", "chat"))
        except ConfigError as exc:
            chat_unavailable = str(exc)

        unsent: list[dict[str, Any]] = []
        unrecorded: list[str] = []
        checked = 0
        found = 0
        for learner in learners:
            record = records.latest(learner.id)
            if record is None:
                if staging.get(learner.id) is None:
                    unrecorded.append(learner.name)
                continue
            session_number = record.get("session_number")
            if not service:
                continue
            for name in contacts:
                try:
                    _, recipient_id = resolve_contact(ctx.config, name)
                except BatonError as exc:
                    unsent.append(
                        {
                            "learner": learner.name,
                            "session_number": session_number,
                            "contact": name,
                            "state": f"ตรวจไม่ได้: {exc}",
                        }
                    )
                    continue
                key = receipts.digest(
                    service,
                    recipient_id,
                    _published_receipt_key(learner.id, session_number, record),
                )
                checked += 1
                if receipts.find(key) is None:
                    unsent.append(
                        {
                            "learner": learner.name,
                            "session_number": session_number,
                            "contact": name,
                            "state": f"ไม่พบหลักฐานการส่ง (ตรวจภายใน {window:g} ชม.)",
                        }
                    )
                else:
                    found += 1

        payload = {
            "date": day.isoformat(),
            "roster_source": source,
            "stuck_drafts": stuck,
            "orphan_drafts": orphans,
            "no_record": unrecorded,
            "send_checks": {
                "checked": checked,
                "receipts_found": found,
                "window_hours": window,
                "chat_unavailable": chat_unavailable,
            },
            "unsent": unsent,
            "unmatched": unmatched,
        }

        source_label = "ปฏิทิน" if source == "calendar" else "วันที่บนเอกสาร"
        lines = [f"สรุปหลังวันสอน {day.isoformat()} (รายชื่ออ่านจาก{source_label})"]

        lines.append(f"staging ค้าง: {len(stuck)}")
        for item in stuck:
            lines.append(
                f"  {item['learner']} ({ctx.config.label('session')} "
                f"{item['session_number']}): {item['state']}"
            )
        lines.append(f"ไฟล์ตกค้าง (ไม่มีคนนี้ในฐานข้อมูล): {len(orphans)}")
        for item in orphans:
            lines.append(
                f"  {item['learner_name']} ({item['learner_id']}, "
                f"{ctx.config.label('session')} {item['session_number']})"
            )
        lines.append(f"ยังไม่ publish เลย: {len(unrecorded)}")
        for name in unrecorded:
            lines.append(f"  {name}")
        if chat_unavailable:
            lines.append(f"ตรวจการส่งไม่ได้: {chat_unavailable}")
        else:
            lines.append(
                f"publish แล้วยังไม่ส่ง: {len(unsent)} (ตรวจแล้ว {checked} คู่, เจอหลักฐาน {found})"
            )
        for item in unsent:
            lines.append(
                f"  {item['learner']} ({ctx.config.label('session')} {item['session_number']}) "
                f"→ {item['contact']}: {item['state']}"
            )
        for event in unmatched:
            lines.append(
                f"  รายการในปฏิทินที่จับคู่ชื่อไม่ได้ (ระบบไม่เดาให้): "
                f"{event.get('title', '')} {event.get('start', '')}"
            )
        ctx.report.result(payload, human="\n".join(lines))
        return Exit.OK
