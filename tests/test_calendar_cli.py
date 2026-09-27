"""`calendar book` and `calendar schedule` through the real CLI.

The scheduler is tested directly in `test_calendar.py`; what only the CLI can
answer is the name-resolution contract at the booking boundary, which names
relax, which refuse, and what the report says when a slot is blocked.
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path

import pytest

import baton
from baton.adapters.docs.base import DocStatus
from baton.adapters.fakes import FakeCalendar, FakeDocStore
from baton.cli.app import run
from baton.exits import Exit

MIGRATIONS = Path(baton.__file__).resolve().parent / "migrations"


@pytest.fixture
def studio(profile, monkeypatch):
    """The same seeded studio `test_lesson_cli` uses, wired to booking.

    Booking needs the real store (sessions come from the database) and fakes
    for the two network sides: the document that gets marked in progress and
    the calendar the event lands on.
    """
    db_path = profile / "data" / "studio.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.executescript((MIGRATIONS / "sqlite.sql").read_text(encoding="utf-8"))
    connection.executescript((MIGRATIONS / "seed_example.sql").read_text(encoding="utf-8"))
    connection.close()

    (profile / "baton.yaml").write_text(
        textwrap.dedent(
            """
            version: 1
            labels:
              learner: student
              session: lesson
            db:
              driver: sqlite
              sqlite:
                path: data/studio.db
            docs:
              driver: notion
              statuses:
                done: Complete
                in_progress: In progress
                not_started: Not started
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    docs = FakeDocStore(
        statuses={
            "doc-ada-01": DocStatus(doc_id="doc-ada-01", status="Complete", date="2026-05-01"),
            "doc-ada-02": DocStatus(doc_id="doc-ada-02", status="Complete", date="2026-06-01"),
            "doc-ada-03": DocStatus(doc_id="doc-ada-03", status="Not started"),
            # Bruno books with no --session, so the pick reads his pages: an
            # unknown status is never treated as free, and an unknown page
            # would leave him with "no free lesson to book".
            "doc-bruno-01": DocStatus(doc_id="doc-bruno-01", status="Not started"),
            "doc-bruno-02": DocStatus(doc_id="doc-bruno-02", status="Not started"),
        },
        wording={"done": "Complete", "in_progress": "In progress"},
    )
    calendar = FakeCalendar()
    monkeypatch.setattr("baton.cli.cmd_calendar.open_docs", lambda _config: docs)
    monkeypatch.setattr("baton.cli.cmd_calendar.open_calendar", lambda _config: calendar)
    return profile, docs, calendar


def call(studio, *args):
    profile, _, _ = studio
    return run(["--profile", str(profile), "--json", "calendar", *args])


def out(capsys):
    return json.loads(capsys.readouterr().out)


# -- book ---------------------------------------------------------------------


def test_a_unique_partial_name_books_and_announces_the_match(studio, capsys):
    """The relaxation, end to end: "Ada" resolves because it lands on exactly
    one person, the booking goes through, and the payload says so: an
    announced match is checkable, a silent one is only a guess that worked."""
    _, docs, calendar = studio

    assert call(studio, "book", "Ada", "2026-08-20", "17:00", "--session", "3") == Exit.OK
    payload = out(capsys)

    assert "Ada Whitfield" in payload["matched"]
    assert payload["doc_updated"] is True
    assert [event.title for event in calendar.events] == ["Ada Whitfield (lesson 3)"]
    assert docs.get_status("doc-ada-03").status == "In progress"


def test_an_exact_name_books_without_a_match_note(studio, capsys):

    assert call(studio, "book", "Ada Whitfield", "2026-08-20", "17:00", "--session", "3") == Exit.OK

    assert "matched" not in out(capsys)


def test_an_ambiguous_partial_still_stops_and_asks(studio, capsys):
    """Widening what counts as a match must never widen what counts as one
    answer: two candidates resolve to nobody, booked or not."""
    connection = sqlite3.connect(studio[0] / "data" / "studio.db")
    connection.execute(
        "INSERT INTO learners (name, instrument) VALUES ('Ada Whitfield Jr', 'piano')"
    )
    connection.commit()
    connection.close()

    assert call(studio, "book", "Ada", "2026-08-20", "17:00") == Exit.NEEDS_HUMAN
    payload = out(capsys)

    assert payload["error"] == "needs_human"
    assert sorted(c["name"] for c in payload["details"]["candidates"]) == [
        "Ada Whitfield",
        "Ada Whitfield Jr",
    ]


def test_the_relaxation_stops_at_booking(studio, capsys):
    """`cancel` keeps the strict gate on purpose: destroying a booking on a
    relaxed guess is a different act from creating one."""
    assert call(studio, "book", "Ada Whitfield", "2026-08-20", "17:00", "--session", "3") == Exit.OK
    capsys.readouterr()

    assert call(studio, "cancel", "Ada", "2026-08-20") == Exit.NEEDS_HUMAN
    assert out(capsys)["error"] == "needs_human"


def test_cancel_day_clears_the_whole_day_end_to_end(studio, capsys):
    """`today` rather than a fixed date: the rollback window is measured from
    the real clock, so a hard-coded day would fall outside it next month."""
    _, docs, calendar = studio
    assert call(studio, "book", "Ada Whitfield", "today", "10:00", "--session", "3") == Exit.OK
    assert call(studio, "book", "Bruno Castell", "today", "11:00") == Exit.OK
    capsys.readouterr()

    assert call(studio, "cancel-day", "today", "--dry-run") == Exit.OK
    assert len(out(capsys)["would_cancel"]) == 2
    assert len(calendar.events) == 2

    assert call(studio, "cancel-day", "today") == Exit.OK
    payload = out(capsys)
    assert [item["learner"] for item in payload["cancelled"]] == ["Ada Whitfield", "Bruno Castell"]
    assert calendar.events == []
    assert docs.get_status("doc-ada-03").date == ""


# -- schedule -----------------------------------------------------------------


def test_a_schedule_books_each_line_and_names_relaxed_matches(studio, capsys):
    _, docs, calendar = studio
    text = "17:00 Ada\n19:00 Ada Whitfield\n20:00 Bruno"

    assert call(studio, "schedule", "2026-08-20", "--text", text) == Exit.NEEDS_HUMAN

    payload = out(capsys)
    assert [item["start"] for item in payload["booked"]] == ["17:00", "20:00"]
    assert any("Ada Whitfield" in item["matched"] for item in payload["booked"])
    assert [event.title for event in calendar.events] == [
        "Ada Whitfield (lesson 3)",
        "Bruno Castell (lesson 1)",
    ]
    assert docs.get_status("doc-ada-03").status == "In progress"


def test_two_differently_typed_names_for_one_learner_block_the_second(studio, capsys):
    """The relaxation makes this an operator's slip ("Ada" and "Ada Whitfield")
    rather than a parse error, so one slot is blocked instead of the day
    refused, and the message names the slot that already booked them."""
    text = "17:00 Ada\n19:00 Ada Whitfield"

    assert call(studio, "schedule", "2026-08-20", "--text", text) == Exit.NEEDS_HUMAN

    payload = out(capsys)
    assert len(payload["booked"]) == 1
    blocked = payload["blocked"][0]
    assert blocked["slot"] == "19:00"
    assert "second time" in blocked["error"]["message"]
    assert "17:00 Ada" in blocked["error"]["message"]


def test_a_slot_with_no_matching_learner_is_blocked_not_fatal(studio, capsys):
    """One mistyped line must not take the rest of the day with it."""
    _, _, calendar = studio
    text = "17:00 Ada\n19:00 Zebedee"

    assert call(studio, "schedule", "2026-08-20", "--text", text) == Exit.NEEDS_HUMAN

    payload = out(capsys)
    assert [item["start"] for item in payload["booked"]] == ["17:00"]
    blocked = payload["blocked"][0]
    assert blocked["name"] == "Zebedee"
    assert blocked["error"]["error"] == "needs_human"
    assert len(calendar.events) == 1


def test_a_dry_run_schedule_touches_nothing(studio, capsys):
    _, docs, calendar = studio

    assert call(studio, "schedule", "2026-08-20", "--text", "17:00 Ada", "--dry-run") == Exit.OK

    payload = out(capsys)
    assert payload["dry_run"] is True
    assert payload["booked"][0]["doc_updated"] is False
    assert calendar.events == []
    assert docs.get_status("doc-ada-03").status == "Not started"
