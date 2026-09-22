"""Sending a recorded work: list it, let a person pick, deliver the links.

Two things are pinned down hard here, because they are the two ways this
command could quietly betray its caller. The first is the two-step contract:
the listing phase must send *nothing*, and ``--pick N`` must mean exactly the
Nth row of the list that was just shown, never anything remembered or
re-derived. The second is the missing-side rule: an incomplete recording sends
the side it has rather than blocking, but a recording with *no* links anywhere
is refused outright.
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path

import pytest

import baton
from baton.adapters.chat.base import SendOutcome
from baton.adapters.fakes import FakeLearnerStore
from baton.cli.app import run
from baton.cli.cmd_init import _upgrade_database
from baton.domain.models import Learner, Work
from baton.errors import GateError, NeedsHumanError, UpstreamError
from baton.exits import Exit
from baton.pipelines.recording import compose_recording, list_candidates, send_recording

MIGRATIONS = Path(baton.__file__).resolve().parent / "migrations"

ADA = Learner(id="1", name="Ada Whitfield")

YT = "https://youtu.be/up-funk"
DRIVE = "https://drive.google.com/file/d/up-funk"

WORK_BOTH = Work(
    id="w1",
    learner_id="1",
    title="Uptown Funk - Bruno Mars",
    type="cover",
    video_link=YT,
    drive_link=DRIVE,
    performed_date="2026-08-20",
)
WORK_YT_ONLY = Work(
    id="w2", learner_id="1", title="Canon in D", type="", video_link="https://youtu.be/canon"
)
WORK_DRIVE_ONLY = Work(
    id="w3",
    learner_id="1",
    title="Drive cut",
    type="performance",
    drive_link="https://drive.google.com/file/d/cut",
    performed_date="2026-08-20",
)
WORK_NOTHING = Work(id="w4", learner_id="1", title="Nothing attached", type="cover")


class FakeMessenger:
    """Records sends; fails on demand. Same contract as the real drivers."""

    driver = "fake"

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_with = fail_with

    def resolve(self, name: str) -> str:
        if name != "teacher":
            raise NeedsHumanError(f'No contact matches "{name}".', candidates=[{"name": "teacher"}])
        return "U-teacher"

    def send(self, recipient_id: str, text: str) -> SendOutcome:
        if self.fail_with:
            raise self.fail_with
        self.sent.append((recipient_id, text))
        return SendOutcome(sent=True, recipient=recipient_id)

    def health(self) -> None:
        pass


# -- composing the message ----------------------------------------------------


def test_both_links_appear_under_their_labels():
    message = compose_recording(WORK_BOTH, learner_name="Ada Whitfield", instrument="กลอง")

    assert message.startswith("🥁 ผลงานบันทึกการเรียนของ Ada Whitfield")
    assert "📌 Uptown Funk - Bruno Mars (cover) 2026-08-20" in message
    assert f"📹 YouTube:\n{YT}" in message
    assert f"📁 Drive:\n{DRIVE}" in message


def test_a_missing_side_sends_the_one_that_exists():
    youtube_only = compose_recording(WORK_YT_ONLY, learner_name="Ada Whitfield")
    drive_only = compose_recording(WORK_DRIVE_ONLY, learner_name="Ada Whitfield")

    assert f"📹 YouTube:\n{'https://youtu.be/canon'}" in youtube_only
    assert "📁" not in youtube_only
    assert "📁 Drive:" in drive_only
    assert "📹" not in drive_only


def test_no_instrument_means_the_plain_music_note_and_no_owner():
    message = compose_recording(WORK_YT_ONLY)

    assert message.startswith("🎵 ผลงานบันทึกการเรียน\n")
    assert "ของ" not in message


def test_a_type_and_date_are_shown_when_present_and_absent_when_not():
    dated = compose_recording(WORK_DRIVE_ONLY, learner_name="Ada Whitfield")

    assert "(performance) 2026-08-20" in dated
    # Canon in D carries no type and no date, no filler either way.
    plain = compose_recording(WORK_YT_ONLY)
    assert "(performance)" not in plain


def test_a_work_with_neither_link_is_refused_fail_closed():
    with pytest.raises(GateError) as excinfo:
        compose_recording(WORK_NOTHING, learner_name="Ada Whitfield")

    assert excinfo.value.exit_code == Exit.GATE
    assert {item["field"] for item in excinfo.value.missing} == {"video_link", "drive_link"}
    assert "add-work" in (excinfo.value.remedy or "")


def test_whitespace_links_count_as_missing():
    bare = Work(id="w5", learner_id="1", title="Blank sides", video_link="   ", drive_link="  ")

    with pytest.raises(GateError):
        compose_recording(bare)


# -- the numbered candidate list ------------------------------------------------


def test_candidates_are_numbered_newest_first():
    store = FakeLearnerStore(learners=[ADA])
    for work in (
        Work(id="a", learner_id="1", title="Old", performed_date="2026-08-01"),
        Work(id="b", learner_id="1", title="Newest", performed_date="2026-08-20"),
        Work(id="c", learner_id="1", title="Undated"),
    ):
        store.add_work(work)

    candidates = list_candidates(store.list_works("1"))

    assert [item["n"] for item in candidates] == [1, 2, 3]
    assert [item["name"] for item in candidates] == ["Newest", "Old", "Undated"]


def test_candidate_entries_carry_everything_a_pick_decision_needs():
    candidates = list_candidates([WORK_BOTH])

    assert candidates[0]["n"] == 1
    assert candidates[0]["id"] == WORK_BOTH.id
    assert candidates[0]["name"] == WORK_BOTH.title
    assert candidates[0]["type"] == "cover"
    assert candidates[0]["performed_date"] == WORK_BOTH.performed_date
    assert candidates[0]["video_link"] == YT
    assert candidates[0]["drive_link"] == DRIVE


# -- delivery -------------------------------------------------------------------


def test_a_send_reaches_the_contact_with_both_links():
    messenger = FakeMessenger()

    result = send_recording(messenger, recipient_id="U-teacher", work=WORK_BOTH, learner_name="Ada")

    assert result["sent"] is True
    assert messenger.sent[0][0] == "U-teacher"
    assert YT in messenger.sent[0][1]
    assert DRIVE in messenger.sent[0][1]


def test_dry_run_composes_but_sends_nothing():
    messenger = FakeMessenger()

    result = send_recording(
        messenger, recipient_id="U-teacher", work=WORK_BOTH, learner_name="Ada", dry_run=True
    )

    assert result["dry_run"] is True
    assert result["sent"] is False
    assert result["message"]
    assert messenger.sent == []


def test_a_refused_delivery_raises_rather_than_returning_sent_false():
    messenger = FakeMessenger(fail_with=UpstreamError("line refused", service="line"))

    with pytest.raises(UpstreamError):
        send_recording(messenger, recipient_id="U1", work=WORK_BOTH, learner_name="Ada")


# -- end to end -----------------------------------------------------------------

DB_ROWS = [
    # Newest first when listed: what the teacher answers "--pick 1" must be.
    (7, "Uptown Funk", "cover", "https://youtu.be/up-funk", "2026-08-20"),
    (3, "Canon in D", "", "https://youtu.be/canon", "2026-08-01"),
]


@pytest.fixture
def rec_studio(profile, monkeypatch):
    """A profile with two recorded works and one Drive-only one, real SQLite."""
    db_path = profile / "data" / "studio.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.executescript((MIGRATIONS / "sqlite.sql").read_text(encoding="utf-8"))
    connection.executescript(
        textwrap.dedent(
            """
            INSERT INTO learners (id, name) VALUES ('1', 'Ada Whitfield');
            INSERT INTO learners (id, name) VALUES ('2', 'Ben NoWork');
            """
        )
    )
    for work_id, title, kind, video, date in DB_ROWS:
        connection.execute(
            "INSERT INTO works (id, learner_id, title, type, video_link, performed_date) "
            "VALUES (?, '1', ?, ?, ?, ?)",
            (work_id, title, kind, video, date),
        )
    connection.commit()
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
            chat:
              driver: line
              contacts:
                teacher:
                  id_env: BATON_TEACHER
                  aliases: [me]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    messenger = FakeMessenger()
    monkeypatch.setattr("baton.cli.cmd_send.open_chat", lambda _config: messenger)
    monkeypatch.setenv("BATON_TEACHER", "U-teacher")
    return profile, messenger


def call(studio, *args):
    profile = studio[0]
    return run(["--profile", str(profile), "--json", "send", "recording", *args])


def read(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_listing_exits_needs_human_and_sends_nothing(rec_studio, capsys):
    _, messenger = rec_studio

    assert call(rec_studio, "Ada Whitfield") == Exit.NEEDS_HUMAN
    payload = read(capsys)

    assert payload["error"] == "needs_human"
    candidates = payload["details"]["candidates"]
    assert [item["n"] for item in candidates] == [1, 2]
    assert [item["name"] for item in candidates] == ["Uptown Funk", "Canon in D"]
    assert all(item["video_link"].startswith("https://") for item in candidates)
    assert "--pick" in payload["remedy"]
    assert messenger.sent == []


def test_phase_1_lists_add_the_drive_column_even_when_unmapped_yet(rec_studio, capsys):
    """A row inserted before the Drive column existed lists its empty side
    instead of failing the listing."""
    connection = sqlite3.connect(Path(rec_studio[0]) / "data" / "studio.db")
    connection.execute(
        "UPDATE works SET drive_link = ? WHERE id = ?", ("https://drive.google.com/d/nf", 7)
    )
    connection.commit()
    connection.close()

    assert call(rec_studio, "Ada Whitfield") == Exit.NEEDS_HUMAN
    payload = read(capsys)

    assert payload["details"]["candidates"][0]["drive_link"] == "https://drive.google.com/d/nf"


def test_pick_one_sends_newest_first_work_through_the_cli(rec_studio, capsys):
    _, messenger = rec_studio

    assert call(rec_studio, "Ada Whitfield", "--to", "teacher", "--pick", "1") == Exit.OK
    payload = read(capsys)

    assert payload["sent"] is True
    assert messenger.sent[0][0] == "U-teacher"
    text = messenger.sent[0][1]
    assert "ผลงานบันทึกการเรียนของ Ada Whitfield" in text
    assert "https://youtu.be/up-funk" in text
    # The second work stays untouched: one pick, one work.
    assert "canon" not in text


def test_a_recording_lesson_and_manual_recording_share_one_receipt(rec_studio, capsys):
    from datetime import datetime, timezone

    from baton.core import jsonio

    profile, messenger = rec_studio
    message = compose_recording(WORK_BOTH, learner_name="Ada Whitfield")
    record = {
        "learner_id": "1",
        "learner_name": "Ada Whitfield",
        "session_number": 3,
        "doc_id": "doc-ada-03",
        "doc_url": "https://example.invalid/lesson-3",
        "kind": "recording",
        "work_id": "7",
        "work": {
            "id": "7",
            "learner_id": "1",
            "title": WORK_BOTH.title,
            "type": WORK_BOTH.type,
            "video_link": YT,
            "drive_link": DRIVE,
            "performed_date": WORK_BOTH.performed_date,
        },
        "summary": None,
        "short_message": message,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = profile / "state" / "published" / "1-3.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_json(path, record)

    assert (
        run(
            [
                "--profile",
                str(profile),
                "--json",
                "send",
                "lesson",
                "Ada Whitfield",
                "--to",
                "teacher",
            ]
        )
        == Exit.OK
    )
    lesson = read(capsys)
    assert lesson["kind"] == "recording"
    assert lesson["message"] == message

    assert call(rec_studio, "Ada Whitfield", "--to", "teacher", "--pick", "1") == Exit.GATE
    refused = read(capsys)
    assert refused["error"] == "gate"
    assert "already sent" in refused["message"]
    assert len(messenger.sent) == 1


def test_pick_two_is_not_pick_one(rec_studio, capsys):
    _, messenger = rec_studio

    assert call(rec_studio, "Ada Whitfield", "--to", "teacher", "--pick", "2") == Exit.OK

    text = messenger.sent[0][1]
    assert "Canon in D" in text
    assert "https://youtu.be/canon" in text


def test_dry_run_reports_the_message_without_pushing(rec_studio, capsys):
    _, messenger = rec_studio

    code = call(rec_studio, "Ada Whitfield", "--to", "teacher", "--pick", "1", "--dry-run")
    assert code == Exit.OK
    payload = read(capsys)

    assert payload["dry_run"] is True
    assert payload["sent"] is False
    assert "https://youtu.be/up-funk" in payload["message"]
    assert messenger.sent == []


def test_a_pick_beyond_the_list_is_usage_and_relists(rec_studio, capsys):
    _, messenger = rec_studio

    assert call(rec_studio, "Ada Whitfield", "--to", "teacher", "--pick", "9") == Exit.USAGE
    payload = read(capsys)

    assert payload["error"] == "usage"
    assert [item["n"] for item in payload["details"]["candidates"]] == [1, 2]
    assert "--pick" in payload["remedy"]
    assert messenger.sent == []


def test_zero_and_negative_picks_match_nothing(rec_studio, capsys):
    _, _messenger = rec_studio

    assert call(rec_studio, "Ada Whitfield", "--to", "teacher", "--pick", "0") == Exit.USAGE
    assert call(rec_studio, "Ada Whitfield", "--to", "teacher", "--pick", "-1") == Exit.USAGE


def test_a_pick_without_a_contact_is_usage_and_nothing_moves(rec_studio, capsys):
    """The listing half needs no recipient; a picked send does."""
    _, messenger = rec_studio

    assert call(rec_studio, "Ada Whitfield", "--pick", "1") == Exit.USAGE
    payload = read(capsys)

    assert "--to" in payload["remedy"]
    assert messenger.sent == []


def test_a_work_with_one_side_sends_that_side_only(rec_studio, capsys):
    """The Drive-only case through the whole CLI: nothing blocks, the missing
    side simply does not appear."""
    profile, messenger = rec_studio
    connection = sqlite3.connect(profile / "data" / "studio.db")
    connection.executescript(
        textwrap.dedent(
            """
            DELETE FROM works;
            INSERT INTO works (id, learner_id, title, type, video_link, performed_date)
            VALUES (5, 1, 'Drive cut', 'performance', '', '2026-08-19');
            UPDATE works SET drive_link = 'https://drive.google.com/d/cut';
            """
        )
    )
    connection.commit()
    connection.close()

    assert call(rec_studio, "Ada Whitfield", "--to", "teacher", "--pick", "1") == Exit.OK

    text = messenger.sent[0][1]
    assert "📁 Drive:\nhttps://drive.google.com/d/cut" in text
    assert "📹" not in text
    assert "youtu" not in text


def test_a_learner_with_no_works_is_refused_by_the_gate(rec_studio, capsys):
    _, messenger = rec_studio

    assert call(rec_studio, "Ben NoWork", "--to", "teacher") == Exit.GATE
    payload = read(capsys)

    assert payload["error"] == "gate"
    assert "add-work" in payload["remedy"]
    assert messenger.sent == []


def test_an_ambiguous_name_is_a_passthrough_of_the_resolution_gate(rec_studio, capsys):
    assert call(rec_studio, "Ada") == Exit.NEEDS_HUMAN
    payload = read(capsys)

    assert payload["error"] == "needs_human"
    assert [item["name"] for item in payload["details"]["candidates"]] == ["Ada Whitfield"]
    assert "full name" in payload["remedy"]


def test_an_unknown_contact_ends_at_needs_human_before_anything_is_sent(rec_studio, capsys):
    _, messenger = rec_studio

    assert call(rec_studio, "Ada Whitfield", "--to", "stranger", "--pick", "1") == (
        Exit.NEEDS_HUMAN
    )
    assert messenger.sent == []


def test_a_line_outage_surfaces_as_upstream(rec_studio, capsys):
    _, messenger = rec_studio
    messenger.fail_with = UpstreamError("line is down", service="line")

    assert call(rec_studio, "Ada Whitfield", "--to", "teacher", "--pick", "1") == Exit.UPSTREAM


def test_barren_pick_on_an_empty_list_names_the_real_problem(rec_studio, capsys):
    profile, _messenger = rec_studio
    connection = sqlite3.connect(profile / "data" / "studio.db")
    connection.execute("DELETE FROM works")
    connection.commit()
    connection.close()

    assert call(rec_studio, "Ada Whitfield", "--to", "teacher", "--pick", "1") == Exit.USAGE
    payload = read(capsys)

    assert "no recording exists yet" in payload["remedy"].lower()


# -- the database carrying a second home -----------------------------------------


def test_the_packaged_schema_has_the_drive_column_up_front():
    connection = sqlite3.connect(":memory:")
    connection.executescript((MIGRATIONS / "sqlite.sql").read_text(encoding="utf-8"))

    columns = {row[1] for row in connection.execute("PRAGMA table_info(works)")}
    _upgrade_database(connection)

    assert "drive_link" in columns
    # The upgrade step must be a no-op on a database that already has it.
    assert "drive_link" in {row[1] for row in connection.execute("PRAGMA table_info(works)")}


def test_upgrading_an_old_database_keeps_rows_and_is_idempotent():
    connection = sqlite3.connect(":memory:")
    # The pre-Drive shape of `works`, built by hand, no recent migration may
    # be used to fake this, or the test would be testing itself.
    connection.executescript(
        """
        CREATE TABLE works (
            id             INTEGER PRIMARY KEY,
            learner_id     TEXT NOT NULL,
            title          TEXT NOT NULL,
            type           TEXT NOT NULL DEFAULT 'performance',
            video_link     TEXT NOT NULL DEFAULT '',
            performed_date TEXT NOT NULL DEFAULT '',
            created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO works (learner_id, title, video_link, performed_date)
        VALUES ('7', 'Old recital', 'https://youtu.be/old', '2026-05-01');
        """
    )

    _upgrade_database(connection)
    _upgrade_database(connection)

    columns = [row[1] for row in connection.execute("PRAGMA table_info(works)")]
    assert columns.count("drive_link") == 1
    row = connection.execute(
        "SELECT title, video_link, drive_link FROM works WHERE title = 'Old recital'"
    ).fetchone()
    assert row == ("Old recital", "https://youtu.be/old", "")
    connection.close()


def test_add_work_records_and_reads_back_the_drive_link(rec_studio, capsys):
    """The seeding loop stays closed without touching Supabase's UI: record a
    work with both homes from the command line, then find them again."""
    profile, _messenger = rec_studio
    prefix = ["--profile", str(profile), "--json"]

    created = run(
        [
            *prefix,
            "learner",
            "add-work",
            "Ada Whitfield",
            "--title",
            "Recital night",
            "--type",
            "exam",
            "--video-link",
            "https://youtu.be/recital",
            "--drive-link",
            "https://drive.google.com/recital",
            "--date",
            "2026-08-25",
        ]
    )
    assert created == Exit.OK
    work = read(capsys)["work"]
    assert work["video_link"] == "https://youtu.be/recital"
    assert work["drive_link"] == "https://drive.google.com/recital"

    assert run([*prefix, "learner", "works", "Ada Whitfield"]) == Exit.OK
    payload = read(capsys)
    recorded = next(item for item in payload["works"] if item["title"] == "Recital night")
    assert recorded["drive_link"] == "https://drive.google.com/recital"


def test_the_fake_store_round_trips_drive_like_sqlite_does():
    """Both shapes the pipeline will meet in tests agree on the field."""
    store = FakeLearnerStore(learners=[ADA])

    stored = store.add_work(WORK_BOTH)
    assert stored.drive_link == DRIVE
    listed = store.list_works("1")[0]
    assert (listed.video_link, listed.drive_link) == (YT, DRIVE)


# -- the lesson page at the end of the message (F16) --------------------------


def _publish_record(profile, learner_id="1", doc_url="https://example.invalid/doc-ada-03"):
    from datetime import datetime, timezone

    from baton.core import jsonio

    record = {
        "learner_id": learner_id,
        "learner_name": "Ada Whitfield",
        "session_number": 3,
        "doc_id": f"doc-ada-0{3}",
        "doc_url": doc_url,
        "titles": "",
        "short_message": "• …",
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = profile / "state" / "published" / f"{learner_id}-3.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_json(path, record)
    return record


def test_the_recording_message_ends_with_the_lesson_page(rec_studio, capsys):
    """A parent tapping the recording can continue into the lesson it came
    from: the line the old sender always attached and the rewrite dropped,
    leaving the message a dead end."""
    profile, _ = rec_studio
    _publish_record(profile)

    assert call(rec_studio, "Ada Whitfield", "--to", "teacher", "--pick", "1") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    assert "📝 รายละเอียดการเรียน: https://example.invalid/doc-ada-03" in payload["message"]
    assert payload["message"].rstrip().endswith("https://example.invalid/doc-ada-03")


def test_a_recording_still_sends_without_any_published_lesson(rec_studio, capsys):
    """Nothing published yet: the links go out the way the old sender sent
    them when its own fetch failed: no line, no block."""
    assert call(rec_studio, "Ada Whitfield", "--to", "teacher", "--pick", "1") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    assert "รายละเอียดการเรียน" not in payload["message"]
    assert payload["sent"] is True
