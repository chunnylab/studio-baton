"""The two reports that bracket a teaching day's sends.

`send readiness` is read before the sends start; `send aftermath` is read
after. Both answer questions an operator actually asks at those moments (
who is booked, what would block their message, what never went out) and
both are reports, not gates: exit 0 whatever they find, because a report
that refuses only teaches the operator to stop running it.

The roster has two sources (the calendar, or the dates on the documents),
and the send gate's verdict appears here through the very `evaluate` the
send refuses through, which is the property under test: what the report
names as missing is what the send would refuse on.
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

import baton
from baton.adapters.cal.base import CalendarEvent
from baton.adapters.chat.base import SendOutcome
from baton.adapters.docs.base import Block, DocStatus
from baton.adapters.fakes import FakeCalendar, FakeDocStore
from baton.cli.app import run
from baton.core import jsonio
from baton.errors import NeedsHumanError
from baton.exits import Exit
from baton.pipelines.staging import LessonDraft, StagingStore

MIGRATIONS = Path(baton.__file__).resolve().parent / "migrations"

DAY = "2026-07-15"

RECORD = {
    "learner_id": "1",
    "learner_name": "Ada Whitfield",
    "session_number": 3,
    "doc_id": "doc-ada-03",
    "doc_url": "https://example.invalid/lesson-3",
    "short_message": "• Covered: Blackbird bars 9-16\n• Homework: 80bpm with the track",
}


class FakeMessenger:
    """Records sends; resolves the one contact this studio has."""

    driver = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def resolve(self, name: str) -> str:
        if name in {"teacher", "me"}:
            return "U-teacher"
        raise NeedsHumanError(
            f'No contact matches "{name}".',
            candidates=[{"name": "teacher"}],
        ) from None

    def send(self, recipient_id: str, text: str) -> SendOutcome:
        self.sent.append((recipient_id, text))
        return SendOutcome(sent=True, recipient=recipient_id)

    def health(self) -> None:
        pass


@pytest.fixture
def studio(profile, monkeypatch):
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
            timezone: Asia/Bangkok
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
            chat:
              driver: line
              contacts:
                teacher:
                  id_env: BATON_TEACHER
                  aliases: [me]
            calendar:
              event_emoji:
                guitar: "🎸"
              default_event_emoji: "🎵"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    # No calendar section, so the roster falls back to the documents,
    # which needs a document carrying the day's date.
    docs = FakeDocStore(
        statuses={"doc-ada-03": DocStatus(doc_id="doc-ada-03", status="Complete", date=DAY)},
        blocks={
            "doc-ada-03": [Block(id="v", type="video", url="https://example.invalid/watch/ada-3")]
        },
    )
    messenger = FakeMessenger()
    monkeypatch.setattr("baton.cli.cmd_send.open_docs", lambda _config: docs)
    monkeypatch.setattr("baton.cli.cmd_send.open_chat", lambda _config: messenger)
    monkeypatch.setenv("BATON_TEACHER", "U-teacher")
    return profile, messenger


def call(studio, *args):
    profile, _ = studio
    return run(["--profile", str(profile), "--json", "send", *args])


def out(capsys):
    return json.loads(capsys.readouterr().out)


def text(studio, capsys, *args):
    """The same report, read the way an operator reads it.

    Both reports are read-only, so running one twice costs nothing and the
    lines a person actually scans stay under test alongside the payload an
    agent parses.
    """
    profile, _ = studio
    assert run(["--profile", str(profile), "send", *args]) == Exit.OK
    return capsys.readouterr().out


def publish(studio, **overrides) -> None:
    """Write a published record directly, as `lesson publish` would have."""
    profile, _ = studio
    record = {
        **RECORD,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **overrides,
    }
    path = (
        profile / "state" / "published" / f"{record['learner_id']}-{record['session_number']}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_json(path, record)


def stage_draft(studio, learner_id: str, learner_name: str, session_number: int = 3) -> None:
    """Leave a draft on disk, as `lesson stage` would have."""
    profile, _ = studio
    StagingStore(profile / "state" / "lessons").save(
        LessonDraft(learner_id=learner_id, learner_name=learner_name, session_number=session_number)
    )


# -- readiness --------------------------------------------------------------


def test_the_roster_falls_back_to_the_documents_and_says_so(studio, capsys):
    publish(studio)

    assert call(studio, "readiness", "--date", DAY) == Exit.OK
    payload = out(capsys)

    assert payload["source"] == "documents"
    assert payload["total"] == 1
    assert payload["ready"] == 1
    row = payload["learners"][0]
    assert row["learner"] == "Ada Whitfield"
    assert row["missing"] == []
    assert row["video_block"] is True
    assert "อ่านจากวันที่บนเอกสาร" in text(studio, capsys, "readiness", "--date", DAY)


def test_a_published_recording_is_ready_without_a_summary(studio, capsys):
    publish(
        studio,
        kind="recording",
        summary=None,
        short_message="RECORD MESSAGE",
        work={
            "id": "7",
            "learner_id": "1",
            "title": "Uptown Funk",
            "type": "cover",
            "video_link": "https://youtu.be/up-funk",
            "drive_link": "",
            "performed_date": DAY,
        },
    )

    assert call(studio, "readiness", "--date", DAY) == Exit.OK
    payload = out(capsys)

    assert payload["ready"] == 1
    row = payload["learners"][0]
    assert row["kind"] == "recording"
    assert row["missing"] == []
    assert row["video_block"] is True


def test_the_report_names_what_the_send_would_refuse_on(studio, capsys):
    publish(studio, short_message="")

    assert call(studio, "readiness", "--date", DAY) == Exit.OK
    payload = out(capsys)
    assert payload["ready"] == 0
    assert payload["learners"][0]["missing"] == ["short_summary"]

    # Same gap, same name, from the send itself: the report and the refusal
    # share one `evaluate`, so they cannot drift.
    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher", "--dry-run") == Exit.GATE
    refusal = out(capsys)
    assert refusal["error"] == "gate"
    assert [item["field"] for item in refusal["details"]["missing"]] == ["short_summary"]


def test_a_deactivated_learner_drops_out_of_the_documents_roster(studio, capsys):
    """The documents fallback has the same scope as the calendar roster: a
    learner who stopped studying does not surface just because their old
    page still carries the day's date."""
    publish(studio)
    profile, _ = studio
    connection = sqlite3.connect(profile / "data" / "studio.db")
    connection.execute("UPDATE learners SET is_active = 0 WHERE name = 'Ada Whitfield'")
    connection.commit()
    connection.close()

    assert call(studio, "readiness", "--date", DAY) == Exit.OK
    payload = out(capsys)

    assert payload["source"] == "documents"
    assert payload["total"] == 0
    assert payload["learners"] == []


def test_not_publishing_is_a_layer_below_the_gate(studio, capsys):
    assert call(studio, "readiness", "--date", DAY) == Exit.OK
    payload = out(capsys)

    row = payload["learners"][0]
    assert row["missing"] == ["ยังไม่ publish"]
    assert row["staging"] == "ไม่มี draft"
    assert payload["ready"] == 0
    assert "ขาด ยังไม่ publish" in text(studio, capsys, "readiness", "--date", DAY)


def test_an_icon_prefixed_title_still_names_its_learner(studio, capsys, monkeypatch):
    """`baton calendar` writes `🎸 Name (lesson N)`, so the roster reads past it.

    The reports used to build their scheduler without the profile's icons,
    leaving the title match only the bare `Name (` prefix to try. Every event
    this studio books carries an instrument icon, so a full teaching day
    reported an empty roster and every booking as unmatched.
    """
    publish(studio)
    monkeypatch.setattr(
        "baton.cli.cmd_send.open_calendar",
        lambda _config: FakeCalendar(
            events=[
                CalendarEvent(
                    id="ev-1",
                    title="🎸 Ada Whitfield (lesson 3)",
                    start=f"{DAY}T10:00:00+07:00",
                    end=f"{DAY}T11:00:00+07:00",
                ),
            ]
        ),
    )

    assert call(studio, "readiness", "--date", DAY) == Exit.OK
    payload = out(capsys)

    assert payload["source"] == "calendar"
    assert payload["unmatched"] == []
    assert [row["learner"] for row in payload["learners"]] == ["Ada Whitfield"]


def test_the_calendar_is_the_roster_when_there_is_one(studio, capsys, monkeypatch):
    publish(studio)
    monkeypatch.setattr(
        "baton.cli.cmd_send.open_calendar",
        lambda _config: FakeCalendar(
            events=[
                CalendarEvent(
                    id="ev-1",
                    title="Ada Whitfield (lesson 3)",
                    start=f"{DAY}T10:00:00+07:00",
                    end=f"{DAY}T11:00:00+07:00",
                ),
                CalendarEvent(
                    id="ev-2",
                    title="Bruno Castell (lesson 2)",
                    start=f"{DAY}T13:00:00+07:00",
                    end=f"{DAY}T14:00:00+07:00",
                ),
                CalendarEvent(
                    id="ev-3",
                    title="น้องมานี (lesson 1)",
                    start=f"{DAY}T15:00:00+07:00",
                    end=f"{DAY}T16:00:00+07:00",
                ),
            ]
        ),
    )

    assert call(studio, "readiness", "--date", DAY) == Exit.OK
    payload = out(capsys)

    assert payload["source"] == "calendar"
    printed = text(studio, capsys, "readiness", "--date", DAY)
    assert "อ่านจากปฏิทิน" in printed
    # Bruno is booked but unpublished, so he is counted, not hidden.
    assert payload["total"] == 2
    assert payload["ready"] == 1
    # An event naming no learner is listed, never guessed at.
    assert [event["title"] for event in payload["unmatched"]] == ["น้องมานี (lesson 1)"]
    assert "รายการในปฏิทินที่จับคู่ชื่อไม่ได้" in printed


# -- aftermath --------------------------------------------------------------


def test_a_published_lesson_with_no_receipt_is_reported_unsent(studio, capsys):
    publish(studio)

    assert call(studio, "aftermath", "--date", DAY) == Exit.OK
    payload = out(capsys)

    assert payload["roster_source"] == "documents"
    assert payload["stuck_drafts"] == []
    assert payload["no_record"] == []
    assert payload["send_checks"]["checked"] == 1
    assert payload["send_checks"]["receipts_found"] == 0
    assert len(payload["unsent"]) == 1
    assert payload["unsent"][0]["contact"] == "teacher"
    assert payload["unsent"][0]["state"].startswith("ไม่พบหลักฐานการส่ง")
    assert "publish แล้วยังไม่ส่ง: 1" in text(studio, capsys, "aftermath", "--date", DAY)


def test_a_receipt_counts_as_proof_the_message_went_out(studio, capsys):
    publish(studio)
    _profile, messenger = studio

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.OK
    capsys.readouterr()
    assert len(messenger.sent) == 1

    assert call(studio, "aftermath", "--date", DAY) == Exit.OK
    payload = out(capsys)

    assert payload["send_checks"] == {
        "checked": 1,
        "receipts_found": 1,
        "window_hours": payload["send_checks"]["window_hours"],
        "chat_unavailable": "",
    }
    assert payload["unsent"] == []
    assert "เจอหลักฐาน 1" in text(studio, capsys, "aftermath", "--date", DAY)


def test_aftermath_checks_a_recording_receipt_by_work_identity(studio, capsys):
    publish(
        studio,
        kind="recording",
        summary=None,
        short_message="RECORD MESSAGE",
        work={
            "id": "7",
            "learner_id": "1",
            "title": "Uptown Funk",
            "type": "cover",
            "video_link": "https://youtu.be/up-funk",
            "drive_link": "",
            "performed_date": DAY,
        },
    )
    _profile, messenger = studio

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.OK
    capsys.readouterr()
    assert len(messenger.sent) == 1

    assert call(studio, "aftermath", "--date", DAY) == Exit.OK
    payload = out(capsys)

    assert payload["send_checks"]["receipts_found"] == 1
    assert payload["unsent"] == []


def test_stuck_drafts_and_orphan_files_are_different_problems(studio, capsys):
    stage_draft(studio, "1", "Ada Whitfield")
    stage_draft(studio, "999", "Ghost Student", session_number=1)

    assert call(studio, "aftermath", "--date", DAY) == Exit.OK
    payload = out(capsys)

    assert payload["stuck_drafts"] == [
        {
            "learner": "Ada Whitfield",
            "session_number": 3,
            "kind": "summary",
            "state": "ยังไม่มีสรุป",
        }
    ]
    assert payload["orphan_drafts"] == [
        {
            "learner_name": "Ghost Student",
            "learner_id": "999",
            "session_number": 1,
            "status": "staged",
        }
    ]
    # Ada has a draft, so she is stalled rather than unpublished.
    assert payload["no_record"] == []
    printed = text(studio, capsys, "aftermath", "--date", DAY)
    assert "staging ค้าง: 1" in printed
    assert "ไฟล์ตกค้าง (ไม่มีคนนี้ในฐานข้อมูล): 1" in printed


def test_a_roster_learner_with_nothing_at_all_is_named(studio, capsys):
    assert call(studio, "aftermath", "--date", DAY) == Exit.OK
    payload = out(capsys)

    assert payload["no_record"] == ["Ada Whitfield"]
    assert "ยังไม่ publish เลย: 1" in text(studio, capsys, "aftermath", "--date", DAY)
