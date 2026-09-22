"""``baton calendar``: book lessons, keeping documents and calendar in step.

Replaces the original's two scripts and the prose rule for choosing between
them. There is one command; passing a learner is what selects the lesson path,
including its ordered gate.
"""

from __future__ import annotations

import argparse
from datetime import time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..adapters.cal import open_calendar
from ..adapters.cal.base import CalendarEvent
from ..adapters.db import store_scope
from ..adapters.docs import open_docs
from ..domain.models import Learner
from ..domain.resolve import resolve_learner, resolve_learner_loose
from ..domain.status import StatusVocabulary
from ..domain.whenever import combine, parse_date, parse_schedule, parse_time, today_in
from ..errors import BatonError, UsageError
from ..exits import Exit
from ..pipelines.learner import LearnerHistory
from ..pipelines.schedule import Scheduler, StandingSync
from .guard import guarded
from .naming import warn_if_inactive

if TYPE_CHECKING:
    from .app import Context


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``calendar`` command group."""
    parser = subparsers.add_parser(
        "calendar",
        help="Book lessons and cancel them, keeping documents and calendar in step.",
        description=(
            "A lesson is marked in progress on its document first; the event is "
            "only created if that succeeded, so the two can never disagree."
        ),
    )
    group = parser.add_subparsers(dest="calendar_command", metavar="<subcommand>")
    parser.set_defaults(handler=_require_subcommand)

    book = group.add_parser("book", help="Book one lesson.")
    book.add_argument("name", metavar="NAME")
    book.add_argument("date", metavar="DATE", help="YYYY-MM-DD, today, tomorrow, +2, …")
    book.add_argument("start", metavar="START", help="17:00 or 17.00")
    book.add_argument("end", metavar="END", nargs="?", help="Defaults to one hour later.")
    book.add_argument("--session", type=int, default=None, help="Defaults to the next free one.")
    book.add_argument("--dry-run", action="store_true")
    book.set_defaults(handler=handle_book)

    schedule = group.add_parser(
        "schedule",
        help="Book a whole day from a list of times and names.",
        description=(
            "One line per slot: `17:00 Ada Whitfield`. A slot ends when the next "
            "begins; `-` marks a free period, which is skipped but still bounds "
            "the slot before it."
        ),
    )
    schedule.add_argument("date", metavar="DATE")
    schedule.add_argument("--text", help="The schedule, newline separated.")
    schedule.add_argument("--file", metavar="PATH", help="Read the schedule from a file.")
    schedule.add_argument("--dry-run", action="store_true")
    schedule.set_defaults(handler=handle_schedule)

    cancel = group.add_parser(
        "cancel",
        help="Remove a booking and roll its session back.",
        description="Deletes the event, then marks the session not started: in that order.",
    )
    cancel.add_argument("name", metavar="NAME")
    cancel.add_argument("date", metavar="DATE")
    cancel.add_argument("--session", type=int, default=None)
    cancel.add_argument("--dry-run", action="store_true")
    cancel.set_defaults(handler=handle_cancel)

    listing = group.add_parser(
        "list",
        help="Show a day's events, or a whole range with --from/--to.",
        description=(
            "One day by default. `--from` and `--to` together show every day "
            "in the range, empty days included: a gap is information."
        ),
    )
    listing.add_argument("date", metavar="DATE", nargs="?", default=None)
    listing.add_argument("--from", dest="from_date", metavar="DATE", help="First day of a range.")
    listing.add_argument("--to", dest="to_date", metavar="DATE", help="Last day of a range.")
    listing.set_defaults(handler=handle_list)

    when = group.add_parser(
        "date",
        help="Resolve a date expression to YYYY-MM-DD.",
        description=(
            "Date arithmetic belongs in code, not in a model's head: an "
            "off-by-one books a lesson on the wrong day."
        ),
    )
    when.add_argument("expression", metavar="EXPR")
    when.set_defaults(handler=handle_date)

    standing = group.add_parser(
        "standing",
        help="Show the standing weekly series the schedule keeps on the calendar.",
        description=(
            "One series per lesson slot of every active learner, marked so "
            "this list never includes an event a person typed. Booked "
            "lessons (the `Name (Week N)` kind) are a different thing and "
            "live on `calendar list`."
        ),
    )
    standing.add_argument("--name", metavar="NAME", default=None, help="One learner only.")
    standing.set_defaults(handler=handle_standing)

    standing_sync = group.add_parser(
        "standing-sync",
        help="Rebuild the standing weekly series from the schedule.",
        description=(
            "Deletes every series this sync owns, then creates one per slot "
            "of every active learner (--name: that learner only, whose "
            "series are deleted even if inactive, which is how someone who "
            "stopped leaves the calendar). Idempotent. A learner deactivated "
            "through the web is not auto-synced: run this once after "
            "deactivating somebody."
        ),
    )
    standing_sync.add_argument("--name", metavar="NAME", default=None, help="One learner only.")
    standing_sync.add_argument("--dry-run", action="store_true", help="Show the plan, and stop.")
    standing_sync.set_defaults(handler=handle_standing_sync)


def _require_subcommand(ctx: Context) -> Exit:
    raise UsageError(
        "`baton calendar` needs a subcommand.",
        remedy="Try `baton calendar list`, or `baton calendar book <name> today 17:00`.",
    )


# -- plumbing ----------------------------------------------------------------


def _resolve(ctx: Context, store, name: str):
    learner = resolve_learner(
        name,
        store.list_learners(),
        aliases=ctx.config.get("db.aliases", {}) or {},
        label=ctx.config.label("learner"),
    )
    warn_if_inactive(ctx, learner)
    return learner


def _resolve_for_booking(ctx: Context, store, name: str) -> tuple[Learner, str]:
    """Booking's resolver: the strict gate, plus the one-partial-match relaxation.

    Names on a day's schedule are typed by hand, often shortened to what the
    family's contact card says. A partial match landing on exactly one person
    resolves, and is announced, so nobody discovers the guess after the fact.
    Everything else still stops and asks; see `resolve_learner_loose`.
    """
    return resolve_learner_loose(
        name,
        store.list_learners(),
        aliases=ctx.config.get("db.aliases", {}) or {},
        label=ctx.config.label("learner"),
    )


def _date(ctx: Context, value: str):
    return parse_date(
        value,
        timezone=ctx.config.timezone,
        shorthand=ctx.config.section("calendar.date_shorthand"),
        weekdays=ctx.config.section("calendar.weekdays"),
        accept_dmy=bool(ctx.config.get("calendar.accept_dmy", False)),
    )


def _time_words(ctx: Context):
    """The profile's time vocabulary, or ``None`` when it has none."""
    words = ctx.config.section("calendar.time_words")
    return words or None


def _time(ctx: Context, value: str):
    return parse_time(value, words=_time_words(ctx))


def _scheduler(ctx: Context) -> Scheduler:
    return Scheduler(
        open_calendar(ctx.config),
        open_docs(ctx.config),
        StatusVocabulary.from_config(ctx.config.section("docs.statuses")),
        timezone=ctx.config.timezone,
        session_label=ctx.config.label("session"),
        require_doc_update=bool(ctx.config.get("calendar.require_doc_update", True)),
        event_emoji=ctx.config.section("calendar.event_emoji"),
        default_emoji=str(ctx.config.get("calendar.default_event_emoji", "")),
        default_minutes=int(ctx.config.get("calendar.default_minutes", 60)),
        time_words=_time_words(ctx),
    )


def _pick_session(ctx: Context, store, learner, wanted: int | None):
    """The session this booking belongs to.

    An explicit number is taken from the database without reading documents;
    otherwise the next free one is chosen, which does require reading them.
    """
    if wanted is not None:
        session = store.get_session(learner.id, wanted)
        if session is None:
            raise UsageError(
                f"{learner.name} has no {ctx.config.label('session')} numbered {wanted}.",
                remedy=f'Run `baton learner sessions "{learner.name}"` to see what exists.',
            )
        return session

    history = LearnerHistory(
        store,
        open_docs(ctx.config),
        StatusVocabulary.from_config(ctx.config.section("docs.statuses")),
        max_parallel_reads=int(ctx.config.get("docs.max_parallel_reads", 4)),
    )
    views = history.sessions(learner)
    active = history.in_progress(views)
    chosen = active[0] if active else history.next_empty(views)
    if chosen is None:
        raise UsageError(
            f"{learner.name} has no free {ctx.config.label('session')} to book.",
            remedy="Pass --session explicitly, or create the next one first.",
        )
    return chosen.session


# -- handlers ----------------------------------------------------------------


@guarded("calendar")
def handle_book(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner, matched = _resolve_for_booking(ctx, store, ctx.args.name)
        session = _pick_session(ctx, store, learner, ctx.args.session)
        day = _date(ctx, ctx.args.date)
        result = _scheduler(ctx).book(
            learner, session, day, ctx.args.start, ctx.args.end, dry_run=ctx.args.dry_run
        )

    verb = "Would book" if ctx.args.dry_run else "Booked"
    label = ctx.config.label("session")
    payload = {**result.to_dict(), "date": day.isoformat(), "dry_run": ctx.args.dry_run}
    if matched:
        payload["matched"] = matched
    ctx.report.result(
        payload,
        human=f"{verb} {result.learner_name} {label} {result.session_number} "
        f"on {day.isoformat()} at {ctx.args.start}\n"
        f"  {result.title}"
        + ("" if ctx.args.dry_run else f"\n  document marked in progress: {result.doc_updated}")
        + (f"\n  {matched}" if matched else ""),
    )
    return Exit.OK


@guarded("calendar")
def handle_schedule(ctx: Context) -> Exit:
    if bool(ctx.args.text) == bool(ctx.args.file):
        raise UsageError(
            "Pass exactly one of --text or --file.",
            remedy="The schedule is one line per slot: `17:00 Ada Whitfield`.",
        )

    text = ctx.args.text
    if ctx.args.file:
        path = Path(ctx.args.file).expanduser()
        if not path.is_file():
            raise UsageError(f"No such file: {path}")
        text = path.read_text(encoding="utf-8")

    day = _date(ctx, ctx.args.date)
    slots = parse_schedule(
        text,
        default_minutes=int(ctx.config.get("calendar.default_minutes", 60)),
        words=_time_words(ctx),
    )

    # The same guard the send batch already has: a name twice in one day would
    # have `_pick_session` hand the same in-progress session to two slots and
    # put one learner's lesson on the calendar twice.
    counts: dict[str, int] = {}
    for _start, _end, name in slots:
        counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        raise UsageError(
            f"One name appears more than once in this schedule: {', '.join(duplicates)}.",
            remedy="A duplicate books the same lesson twice in one day. "
            "Fix the name, or remove the extra slot.",
        )

    if not slots:
        ctx.report.result(
            {"date": day.isoformat(), "booked": [], "blocked": []},
            human="Nothing to book: every slot is free.",
        )
        return Exit.OK

    with store_scope(ctx.config) as store:
        scheduler = _scheduler(ctx)
        booked: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []

        # Every slot is resolved before any of them books. With the partial-
        # name relaxation two differently-typed names can land on the same
        # learner ("Pun" and "Pun the younger"), and the duplicate guard above
        # cannot see that because the strings differ. Booking while resolving
        # would hand `_pick_session`'s one in-progress session to both slots.
        resolved: list[tuple[time, time, str, Learner, str]] = []
        for start, end, name in slots:
            try:
                learner, matched = _resolve_for_booking(ctx, store, name)
            except BatonError as err:
                blocked.append(
                    {"slot": start.strftime("%H:%M"), "name": name, "error": err.to_dict()}
                )
                continue
            resolved.append((start, end, name, learner, matched))

        # The relaxation also makes a duplicate the *operator's* mistake to
        # catch rather than a parse error, so it blocks one slot instead of
        # refusing the day: unlike two identical strings, this is a schedule
        # that is right except for one line.
        seen: dict[str, str] = {}
        pending: list[tuple[time, time, str, Learner, str]] = []
        for start, end, name, learner, matched in resolved:
            earlier = seen.get(str(learner.id))
            if earlier is not None:
                blocked.append(
                    {
                        "slot": start.strftime("%H:%M"),
                        "name": name,
                        "error": UsageError(
                            f"This books {learner.name} a second time: the slot "
                            f'for "{earlier}" already did.',
                            remedy="A learner gets one slot per day; remove the "
                            "duplicate line or fix the name.",
                        ).to_dict(),
                    }
                )
                continue
            # The stored line, not just the name: with two Ada-ish lines the
            # operator has to be told which one to delete.
            seen[str(learner.id)] = f"{start.strftime('%H:%M')} {name}"
            pending.append((start, end, name, learner, matched))

        for start, end, _name, learner, matched in pending:
            # One slot failing must not abandon the rest of the day, but the
            # report has to name every one that did not go.
            try:
                session = _pick_session(ctx, store, learner, None)
                result = scheduler.book(
                    learner,
                    session,
                    day,
                    start.strftime("%H:%M"),
                    end.strftime("%H:%M"),
                    dry_run=ctx.args.dry_run,
                )
                entry = {**result.to_dict(), "start": start.strftime("%H:%M")}
                if matched:
                    entry["matched"] = matched
                booked.append(entry)
            except BatonError as err:
                blocked.append(
                    {"slot": start.strftime("%H:%M"), "name": learner.name, "error": err.to_dict()}
                )

    payload = {
        "date": day.isoformat(),
        "requested": len(slots),
        "booked": booked,
        "blocked": blocked,
        "dry_run": ctx.args.dry_run,
    }
    lines = [f"{day.isoformat()}: {len(booked)} of {len(slots)} booked"]
    for item in booked:
        lines.append(f"  ✓ {item['start']}  {item['title']}")
        if item.get("matched"):
            lines.append(f"      {item['matched']}")
    for item in blocked:
        lines.append(f"  ✗ {item['slot']}  {item['name']}: {item['error']['message']}")
    ctx.report.result(payload, human="\n".join(lines))

    return Exit.OK if not blocked else Exit.NEEDS_HUMAN


@guarded("calendar")
def handle_cancel(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, ctx.args.name)
        session = _pick_session(ctx, store, learner, ctx.args.session)
        day = _date(ctx, ctx.args.date)
        result = _scheduler(ctx).cancel(
            learner,
            session,
            day,
            rollback_window_days=int(ctx.config.get("calendar.rollback_window_days", 1)),
            today=today_in(ctx.config.timezone),
            dry_run=ctx.args.dry_run,
        )

    removed = result.get("deleted", result.get("would_delete", []))
    verb = "Would cancel" if ctx.args.dry_run else "Cancelled"
    ctx.report.result(
        {**result, "date": day.isoformat()},
        human=f"{verb} {learner.name} {ctx.config.label('session')} "
        f"{result['session_number']} on {day.isoformat()}\n"
        f"  events removed: {len(removed)}",
    )
    return Exit.OK


def _clock_of(start: str) -> str:
    """The HH:MM a calendar start renders as, or the raw string if it has none.

    All-day events arrive date-only (``YYYY-MM-DD``), and slicing at a fixed
    offset mangled those (M15). The clock is taken only when the character at
    the date/time boundary is the separator a datetime actually has: ``T``
    for RFC 3339, a space for the lenient form, so a date-only start passes
    through whole instead of being cut at a position that means nothing.
    """
    if len(start) >= 16 and start[10] in "T ":
        return start[11:16]
    return start


def handle_list(ctx: Context) -> Exit:
    calendar = open_calendar(ctx.config)

    if ctx.args.from_date is not None or ctx.args.to_date is not None:
        return _list_range(ctx, calendar)

    day = _date(ctx, ctx.args.date or "today")
    start = combine(day, parse_time("00:00"), ctx.config.timezone).isoformat()
    end = combine(day + timedelta(days=1), parse_time("00:00"), ctx.config.timezone).isoformat()
    events: list[CalendarEvent] = calendar.list_between(start, end)

    payload = {"date": day.isoformat(), "events": [event.to_dict() for event in events]}
    if not events:
        ctx.report.result(payload, human=f"Nothing on {day.isoformat()}.")
        return Exit.OK

    lines = [day.isoformat()]
    for event in events:
        lines.append(f"  {_clock_of(event.start)}  {event.title}")
    ctx.report.result(payload, human="\n".join(lines))
    return Exit.OK


def _list_range(ctx: Context, calendar) -> Exit:
    """Every day from --from to --to, empty days included."""
    if ctx.args.date is not None:
        raise UsageError(
            "Pass either a single date or --from/--to, not both.",
            remedy="A date lists that day; --from and --to together list a range.",
        )
    if ctx.args.from_date is None or ctx.args.to_date is None:
        raise UsageError(
            "A range needs both --from and --to.",
            remedy="Pass both, or drop them and pass a single date.",
        )
    first = _date(ctx, ctx.args.from_date)
    last = _date(ctx, ctx.args.to_date)
    if last < first:
        raise UsageError(
            f"The range ends ({last.isoformat()}) before it begins ({first.isoformat()}).",
            remedy="Pass --from as the earlier day, --to as the later one.",
        )

    midnight = parse_time("00:00")
    start = combine(first, midnight, ctx.config.timezone).isoformat()
    end = combine(last + timedelta(days=1), midnight, ctx.config.timezone).isoformat()
    events: list[CalendarEvent] = calendar.list_between(start, end)

    # Every day in the range appears, empty or not: "nothing booked Thursday"
    # is a fact worth seeing in a week view, not a hole to skip over.
    buckets: dict[str, list[CalendarEvent]] = {}
    for offset in range((last - first).days + 1):
        buckets[(first + timedelta(days=offset)).isoformat()] = []
    for event in events:
        buckets.setdefault(event.start[:10], []).append(event)

    days = [
        {"date": iso, "events": [event.to_dict() for event in day_events]}
        for iso, day_events in buckets.items()
    ]
    lines: list[str] = []
    for iso, day_events in buckets.items():
        lines.append(iso)
        lines += [f"  {_clock_of(event.start)}  {event.title}" for event in day_events] or [
            "  nothing"
        ]
    ctx.report.result(
        {"from": first.isoformat(), "to": last.isoformat(), "days": days},
        human="\n".join(lines),
    )
    return Exit.OK


def handle_date(ctx: Context) -> Exit:
    day = _date(ctx, ctx.args.expression)
    ctx.report.result(
        {
            "expression": ctx.args.expression,
            "date": day.isoformat(),
            "timezone": ctx.config.timezone,
        },
        human=day.isoformat(),
    )
    return Exit.OK


# -- the standing weekly schedule ---------------------------------------------


def _standing(ctx: Context) -> StandingSync:
    return StandingSync(
        open_calendar(ctx.config),
        timezone=ctx.config.timezone,
        event_emoji=ctx.config.section("calendar.event_emoji"),
        default_emoji=str(ctx.config.get("calendar.default_event_emoji", "")),
        default_minutes=int(ctx.config.get("calendar.default_minutes", 60)),
    )


def _resolve_for_standing(ctx: Context, store, name: str | None):
    """The learner a scoped standing command works on, if one was named.

    Inactive learners resolve on purpose: deleting their series is half of
    what a scoped sync is for. Trashed learners do not, exactly like every
    other command."""
    if name is None:
        return None
    learner = resolve_learner(
        name,
        store.list_learners(),
        aliases=ctx.config.get("db.aliases", {}) or {},
        label=ctx.config.label("learner"),
    )
    warn_if_inactive(ctx, learner)
    return learner


@guarded("calendar")
def handle_standing(ctx: Context) -> Exit:
    sync = _standing(ctx)
    with store_scope(ctx.config) as store:
        learner = _resolve_for_standing(ctx, store, ctx.args.name)
        learner_id = None if learner is None else learner.id
        series = sync.calendar.list_standing(learner_id)

    payload = {
        "scope": "all" if learner is None else learner.name,
        "count": len(series),
        "standing": [
            {"id": event.id, "title": event.title, "start": event.start} for event in series
        ],
    }
    if not series:
        human = "No standing series on the calendar."
    else:
        human = "\n".join(f"  {event.title}  (from {event.start[:10]})" for event in series)
    ctx.report.result(payload, human=human)
    return Exit.OK


@guarded("calendar")
def handle_standing_sync(ctx: Context) -> Exit:
    sync = _standing(ctx)
    with store_scope(ctx.config) as store:
        learner = _resolve_for_standing(ctx, store, ctx.args.name)
        result = sync.sync(store, learner=learner, dry_run=ctx.args.dry_run)

    if ctx.args.dry_run:
        ctx.report.result(
            {"dry_run": True, **result.to_dict()},
            human="Nothing was changed. The plan is in the JSON payload.",
        )
        return Exit.OK

    ctx.report.result(
        result.to_dict(),
        human=f"Rebuilt the standing series for {result.scope}: deleted "
        f"{result.deleted}, created {result.created}, {len(result.standing)} "
        "now on the calendar.",
    )
    return Exit.OK
