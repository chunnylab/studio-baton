"""``baton prep``: the day's lesson-prep report, behind a hard gate.

Walking into a lesson means knowing three things about the last one: what was
covered, what was set to practise, and where the teaching goes next. The
pages hold all of it, but assembling it by hand is exactly the work an agent
does badly under time pressure: sections get dropped, links get mangled, and
the teacher walks in half-briefed. So the report is built by the command, and
the agent's job is to hand it over untouched.

Two rules make that safe, and both are enforced here rather than asked of
whoever is driving:

* **Fail closed.** A learner appears in the report only when every required
  field of their latest session is present: name, date, pieces, the page
  link, the overview, the content, the homework. A learner whose page is
  half-written is listed as blocked, with the fields it lacks; when nobody
  passes, there is no report at all and the command exits ``5``.
* **The report is the output.** What Baton prints is what the teacher reads.
  An agent relays it verbatim or not at all; re-composing it is how links
  went missing in the system this replaces.

Who to prepare comes from the calendar by default (every learner booked on
the day) or from explicit ``--learner`` flags. The next goal is a warning
rather than a requirement: plenty of pages end without one, and a missing
plan is worth seeing, not worth blocking the whole briefing over.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

from ..adapters.db import store_scope
from ..domain.models import Learner
from ..domain.prep import SectionRules, missing_fields
from ..domain.status import StatusVocabulary
from ..domain.whenever import today_in
from ..errors import BatonError, GateError
from ..exits import Exit
from ..pipelines.learner import LearnerHistory, SessionView
from .cmd_calendar import _date, _scheduler
from .cmd_learner import _resolve

if TYPE_CHECKING:
    from .app import Context

#: Section order in the report, matching how a page reads top to bottom.
_REPORT_ORDER = ("overview", "content", "focus", "homework", "next_goal")


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``prep`` command."""
    parser = subparsers.add_parser(
        "prep",
        help="The day's lesson-prep report, one learner per booked lesson.",
        description=(
            "For every learner booked on the day (or each --learner given): "
            "their latest finished session, read as sections. A learner whose "
            "page is missing a required field is listed as blocked, not "
            "reported half-read; the command exits 5 when nobody passes. The "
            "printed report is the briefing: relay it verbatim."
        ),
    )
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="The day to prepare. Defaults to today.",
    )
    parser.add_argument(
        "--learner",
        action="append",
        metavar="NAME",
        help="Prepare this learner instead of the day's bookings. Repeatable.",
    )
    parser.set_defaults(handler=handle_prep)


def _entry_skeleton(
    learner: Learner, view: SessionView | None, rules: SectionRules
) -> dict[str, Any]:
    """The identity fields every entry carries, sections blank until read."""
    entry: dict[str, Any] = {
        "learner": learner.name,
        "week": view.number if view else "",
        "date": view.doc.date if view else "",
        "titles": view.doc.titles if view else "",
        "notion_link": "",
    }
    if view is not None and view.session.doc_id:
        entry["notion_link"] = view.doc.url or f"https://notion.so/{view.session.doc_id}"
    entry.update(dict.fromkeys(rules.keywords, ""))
    return entry


def _prep_fields(
    history: LearnerHistory, learner: Learner, rules: SectionRules
) -> tuple[dict[str, Any], list[str]]:
    """One learner's briefing entry, and what stops it being reported.

    Returns:
        The entry (always built, so a blocked learner shows what it does have)
        and the blocking failures: unreadable page, no finished session, or
        the missing required fields.
    """
    view = history.latest_done(history.sessions(learner))
    if view is None or not view.session.doc_id:
        return _entry_skeleton(learner, view, rules), ["latest_done"]

    try:
        sections = rules.read(history.docs.list_blocks(view.session.doc_id))
    except BatonError as exc:
        return _entry_skeleton(learner, view, rules), [f"unreadable page: {exc.message}"]

    entry = _entry_skeleton(learner, view, rules) | sections
    # Practice goals are homework in intent; a page that states them under
    # that heading instead of a checklist still tells the teacher what to
    # assign next.
    if not entry.get("homework"):
        entry["homework"] = sections.get("practice_goals", "")
    return entry, []


def handle_prep(ctx: Context) -> Exit:
    day = _date(ctx, ctx.args.date) if ctx.args.date else today_in(ctx.config.timezone)
    scheduler = _scheduler(ctx)
    rules = SectionRules.from_config(ctx.config)
    required = [str(field) for field in ctx.config.get("prep.required", []) or []]
    warning_fields = [str(field) for field in ctx.config.get("prep.warning", []) or []]

    with store_scope(ctx.config) as store:
        unmatched: list[dict[str, Any]] = []
        if ctx.args.learner:
            learners = [_resolve(ctx, store, name) for name in ctx.args.learner]
        else:
            learners, unmatched = scheduler.who_is_booked(store, day)
        if not learners:
            raise GateError(
                f"No {ctx.config.label('learners')} to prepare for {day.isoformat()}.",
                missing=[{"field": "learners", "date": day.isoformat()}],
                remedy=(
                    "Nobody is booked that day. Pass --learner to prepare "
                    "someone anyway, or check `baton calendar list`."
                ),
            )

        # The scheduler already opened the document store; reading through the
        # same one keeps the command to a single client of the API.
        history = LearnerHistory(
            store,
            scheduler.docs,
            StatusVocabulary.from_config(ctx.config.section("docs.statuses")),
            max_parallel_reads=int(ctx.config.get("docs.max_parallel_reads", 4)),
        )
        ready: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for learner in learners:
            entry, failures = _prep_fields(history, learner, rules)
            missing = failures or missing_fields(entry, required)
            if missing:
                blocked.append({"learner": learner.name, "missing": missing})
            else:
                entry["warnings"] = missing_fields(entry, warning_fields)
                ready.append(entry)

    human = _report(ctx, ready, blocked, day.isoformat())
    payload = {
        "date": day.isoformat(),
        "ready": ready,
        "blocked": blocked,
        "unmatched_events": unmatched,
        "count": len(ready),
        # Carried in the payload too, so a caller in --json mode can relay the
        # briefing verbatim instead of re-composing it, which is how links
        # went missing in the system this replaces.
        "report": human,
    }

    if not ready:
        ctx.report.failure(payload, human=human)
        return Exit.GATE
    ctx.report.result(payload, human=human)
    return Exit.OK


def _report(
    ctx: Context, ready: list[dict[str, Any]], blocked: list[dict[str, Any]], day: str
) -> str:
    """The briefing itself: what the teacher reads, so what gets relayed."""
    label = ctx.config.label("session")
    lines = [f"Lesson prep {day}  ({len(ready)} of {len(ready) + len(blocked)} ready)"]
    for entry in ready:
        lines.append(f"\n{entry['learner']}  (latest {label} {entry['week']} | {entry['date']})")
        if entry.get("titles"):
            lines.append(f"  titles: {entry['titles']}")
        for name in _REPORT_ORDER:
            text = str(entry.get(name, "") or "")
            if text:
                lines.append(f"  {name.replace('_', ' ')}: {text}")
            elif name in entry.get("warnings", []):
                lines.append(f"  {name.replace('_', ' ')}: (none stated)")
        lines.append(f"  page: {entry['notion_link']}")
    if blocked:
        lines.append("\nBLOCKED, incomplete, not reported:")
        for item in blocked:
            lines.append(f"  • {item['learner']}: missing {', '.join(item['missing'])}")
    return "\n".join(lines)
