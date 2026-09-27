"""`baton learner` end to end: real SQLite, a stubbed document store.

The database is the real driver against the shipped migration; only the
document store is substituted, because reaching Notion in CI is not a test of
anything Baton controls.
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path

import pytest

import baton
from baton.adapters.docs.base import Block, DocStatus
from baton.adapters.fakes import FakeDocStore
from baton.cli.app import run
from baton.domain.models import Piece
from baton.exits import Exit
from baton.pipelines.staging import LessonDraft, PieceSnapshot, PublishedRecord

MIGRATIONS = Path(baton.__file__).resolve().parent / "migrations"

# Ada has sessions 1-3; Bruno 1-2; Clara 1; Devon 1. Seeded doc ids are
# predictable, so the document states can be scripted precisely.
DOC_STATES = {
    "doc-ada-01": DocStatus(doc_id="doc-ada-01", status="Complete", date="2026-05-01"),
    "doc-ada-02": DocStatus(doc_id="doc-ada-02", status="Complete", date="2026-06-01"),
    "doc-ada-03": DocStatus(doc_id="doc-ada-03", status="Not started"),
    "doc-bruno-01": DocStatus(doc_id="doc-bruno-01", status="In progress"),
    "doc-bruno-02": DocStatus(doc_id="doc-bruno-02", status="Not started"),
    "doc-clara-01": DocStatus(doc_id="doc-clara-01", status="In progress"),
    "doc-devon-01": DocStatus(doc_id="doc-devon-01", status="Not started"),
}


@pytest.fixture
def studio(profile, monkeypatch):
    """A seeded profile whose document store is a scripted fake."""
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
              piece: piece
            db:
              driver: sqlite
              sqlite:
                path: data/studio.db
              aliases:
                ada: Ada Whitfield
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

    fake = FakeDocStore(
        statuses=dict(DOC_STATES),
        # Devon's page is "not started" but already has content on it.
        blocks={"doc-devon-01": [Block(id="x", type="paragraph", text="draft")]},
    )
    monkeypatch.setattr("baton.cli.cmd_learner.open_docs", lambda _config: fake)
    return profile, fake


def call(studio, *args):
    profile, _ = studio
    return run(["--profile", str(profile), "--json", "learner", *args])


def test_list_returns_every_learner(studio, capsys):
    assert call(studio, "list") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 4
    assert payload["learners"][0]["name"] == "Ada Whitfield"


def test_latest_reports_the_newest_done_session(studio, capsys):
    assert call(studio, "latest", "Ada Whitfield") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["latest_done"]["number"] == 2
    assert payload["latest_done"]["date"] == "2026-06-01"


def test_latest_is_not_the_highest_number(studio, capsys):
    """Ada's session 3 exists and is unstarted; the answer must still be 2."""
    call(studio, "latest", "Ada Whitfield")

    assert json.loads(capsys.readouterr().out)["latest_done"]["number"] != 3


def test_next_skips_a_page_that_already_has_content(studio, capsys):
    """Devon's only session is unstarted but has a draft on it, so there is
    no free session: reporting one would invite overwriting the draft."""
    assert call(studio, "next", "Devon Marsh") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["next_empty"] is None
    assert payload["highest_number"] == 1


def test_next_finds_a_genuinely_empty_session(studio, capsys):
    assert call(studio, "next", "Ada Whitfield") == Exit.OK

    assert json.loads(capsys.readouterr().out)["next_empty"]["number"] == 3


def test_in_progress_spans_learners(studio, capsys, monkeypatch):
    """The morning check reads the calendar window, then only those learners'
    pages. Bruno's second lesson is on the calendar too, but its page is Not
    started: the page is the truth, so he owes one summary, not two."""
    from datetime import datetime, timedelta

    from baton.adapters.cal.base import CalendarEvent
    from baton.adapters.fakes import FakeCalendar

    _profile, fake = studio
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT17:00:00")
    calendar = FakeCalendar(
        [
            CalendarEvent(
                id="e1", title="Bruno Castell (lesson 1)", start=yesterday, end=yesterday
            ),
            CalendarEvent(id="e2", title="Clara Nguyen (lesson 1)", start=yesterday, end=yesterday),
            CalendarEvent(
                id="e3", title="Bruno Castell (lesson 2)", start=yesterday, end=yesterday
            ),
        ]
    )
    monkeypatch.setattr("baton.cli.cmd_calendar.open_calendar", lambda _config: calendar)
    monkeypatch.setattr("baton.cli.cmd_calendar.open_docs", lambda _config: fake)

    assert call(studio, "in-progress") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    names = [row["learner"]["name"] for row in payload["in_progress"]]
    assert names == ["Bruno Castell", "Clara Nguyen"]


def test_show_joins_both_stores(studio, capsys):
    assert call(studio, "show", "Ada Whitfield") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["current_piece"]["title"] == "Blackbird"
    assert payload["sessions"]["done"] == 2
    assert payload["sessions"]["next_empty"]["number"] == 3


def test_sessions_lists_each_one_with_its_state(studio, capsys):
    assert call(studio, "sessions", "Ada Whitfield") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    assert [s["number"] for s in payload["sessions"]] == [1, 2, 3]
    assert [s["state"] for s in payload["sessions"]] == ["done", "done", "not_started"]


# -- the resolution gate, reached through the CLI ----------------------------


def test_an_ambiguous_name_exits_needs_human_with_candidates(studio, capsys):
    assert call(studio, "show", "a") == Exit.NEEDS_HUMAN

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "needs_human"
    assert len(payload["details"]["candidates"]) > 1


def test_a_unique_partial_still_refuses_to_resolve(studio, capsys):
    """ "Whitfield" matches one person. It still must not resolve."""
    assert call(studio, "show", "Whitfield") == Exit.NEEDS_HUMAN

    payload = json.loads(capsys.readouterr().out)
    assert [c["name"] for c in payload["details"]["candidates"]] == ["Ada Whitfield"]


def test_a_configured_alias_resolves(studio, capsys):
    assert call(studio, "latest", "ada") == Exit.OK

    assert json.loads(capsys.readouterr().out)["learner"]["name"] == "Ada Whitfield"


def test_an_unknown_name_returns_the_roster(studio, capsys):
    assert call(studio, "show", "Nobody") == Exit.NEEDS_HUMAN

    payload = json.loads(capsys.readouterr().out)
    assert len(payload["details"]["candidates"]) == 4


# -- writes ------------------------------------------------------------------


def test_add_work_records_and_reads_back(studio, capsys):
    assert (
        call(
            studio,
            "add-work",
            "Bruno Castell",
            "--title",
            "Take Five",
            "--type",
            "cover",
            "--date",
            "2026-08-01",
        )
        == Exit.OK
    )
    assert json.loads(capsys.readouterr().out)["work"]["title"] == "Take Five"

    call(studio, "works", "Bruno Castell")
    assert [w["title"] for w in json.loads(capsys.readouterr().out)["works"]] == ["Take Five"]


def test_add_work_dry_run_writes_nothing(studio, capsys):
    call(studio, "add-work", "Bruno Castell", "--title", "Nope", "--dry-run")
    assert json.loads(capsys.readouterr().out)["dry_run"] is True

    call(studio, "works", "Bruno Castell")
    assert json.loads(capsys.readouterr().out)["works"] == []


def test_assign_sets_and_clears_the_current_piece(studio, capsys):
    assert call(studio, "assign", "Devon Marsh", "--piece", "1") == Exit.OK
    assert json.loads(capsys.readouterr().out)["assigned"] == "1"

    call(studio, "show", "Devon Marsh")
    assert json.loads(capsys.readouterr().out)["current_piece"]["title"] == "Autumn Leaves"

    assert call(studio, "assign", "Devon Marsh") == Exit.OK
    capsys.readouterr()
    call(studio, "show", "Devon Marsh")
    assert json.loads(capsys.readouterr().out)["current_piece"] is None


def test_assigning_an_unknown_piece_is_a_usage_error(studio, capsys):
    assert call(studio, "assign", "Devon Marsh", "--piece", "999") == Exit.USAGE

    payload = json.loads(capsys.readouterr().out)
    assert "999" in payload["message"]


def test_assign_dry_run_changes_nothing(studio, capsys):
    call(studio, "assign", "Devon Marsh", "--piece", "1", "--dry-run")
    capsys.readouterr()

    call(studio, "show", "Devon Marsh")
    assert json.loads(capsys.readouterr().out)["current_piece"] is None


def test_assign_can_plan_and_update_old_published_piece_sections(studio, capsys):
    profile, docs = studio
    old_piece = Piece(
        id="2",
        title="Blackbird",
        source_link="https://example.invalid/blackbird",
        sheet_link="https://example.invalid/sheets/blackbird.pdf",
    )
    records = PublishedRecord(profile / "state" / "published")
    for number in (1, 2):
        doc_id = f"doc-ada-0{number}"
        records.save(
            LessonDraft(
                "1",
                "Ada Whitfield",
                number,
                piece_snapshot=PieceSnapshot.capture(old_piece),
                doc_id=doc_id,
            ),
            short_message="summary",
        )
        docs.blocks[doc_id] = [
            Block(id=f"old-heading-{number}", type="heading_2", text="🎵 Blackbird"),
            Block(id=f"old-source-{number}", type="bookmark", url=old_piece.source_link),
            Block(id=f"old-sheet-{number}", type="embed", url=old_piece.sheet_link),
            Block(id=f"summary-{number}", type="paragraph", text="Keep the lesson summary"),
            Block(id=f"video-{number}", type="video", url=f"https://youtu.be/lesson-{number}"),
        ]

    assert (
        call(
            studio,
            "assign",
            "Ada Whitfield",
            "--piece",
            "1",
            "--update-published",
            "--dry-run",
        )
        == Exit.OK
    )
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["published_updates"]["would_update"] == 2
    assert all(
        any(block.text == "🎵 Blackbird" for block in docs.list_blocks(doc))
        for doc in ("doc-ada-01", "doc-ada-02")
    )

    assert (
        call(
            studio,
            "assign",
            "Ada Whitfield",
            "--piece",
            "1",
            "--update-published",
        )
        == Exit.OK
    )
    result = json.loads(capsys.readouterr().out)

    assert result["assigned"] == "1"
    assert result["published_updates"]["updated"] == 2
    for number in (1, 2):
        blocks = docs.list_blocks(f"doc-ada-0{number}")
        assert any(block.text == "🎵 Autumn Leaves" for block in blocks)
        assert any(block.id == f"summary-{number}" for block in blocks)
        assert any(block.id == f"video-{number}" for block in blocks)


def test_pieces_lists_the_catalogue(studio, capsys):
    assert call(studio, "pieces") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 4
    assert payload["pieces"][0]["title"] == "Autumn Leaves"


def test_labels_from_the_profile_reach_human_output(profile, studio, capsys):
    """The profile renames "week" to "lesson"; output must follow."""
    p, _ = studio
    run(["--profile", str(p), "learner", "next", "Ada Whitfield"])

    assert "lesson" in capsys.readouterr().out


def test_subcommand_is_required(studio, capsys):
    p, _ = studio
    assert run(["--profile", str(p), "learner"]) == Exit.USAGE


def test_in_progress_can_show_recording_readiness(studio, capsys, monkeypatch):
    """The teacher's morning column: which unfinished lessons already have
    their recording on the page. The old report answered this by scanning
    every page of every learner; this reads only the window's candidates."""
    from datetime import datetime, timedelta

    from baton.adapters.cal.base import CalendarEvent
    from baton.adapters.fakes import FakeCalendar

    _profile, fake = studio
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT17:00:00")
    calendar = FakeCalendar(
        [
            CalendarEvent(
                id="e1", title="Bruno Castell (lesson 1)", start=yesterday, end=yesterday
            ),
            CalendarEvent(id="e2", title="Clara Nguyen (lesson 1)", start=yesterday, end=yesterday),
        ]
    )
    monkeypatch.setattr("baton.cli.cmd_calendar.open_calendar", lambda _config: calendar)
    monkeypatch.setattr("baton.cli.cmd_calendar.open_docs", lambda _config: fake)

    # Bruno's unfinished lesson already has its recording; Clara's does not.
    fake.blocks["doc-bruno-01"] = [
        Block(id="v", type="video", url="https://youtu.be/bruno-1"),
    ]

    assert call(studio, "in-progress", "--videos") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    by_name = {row["learner"]["name"]: row for row in payload["in_progress"]}
    assert by_name["Bruno Castell"]["video_link"] == "https://youtu.be/bruno-1"
    assert by_name["Clara Nguyen"]["video_link"] == ""


def test_in_progress_without_the_flag_reads_no_blocks(studio, capsys, monkeypatch):
    """The default report stays as cheap as it was: no block reads, no field."""
    from datetime import datetime, timedelta

    from baton.adapters.cal.base import CalendarEvent
    from baton.adapters.fakes import FakeCalendar

    _profile, fake = studio
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT17:00:00")
    calendar = FakeCalendar(
        [CalendarEvent(id="e1", title="Bruno Castell (lesson 1)", start=yesterday, end=yesterday)]
    )
    monkeypatch.setattr("baton.cli.cmd_calendar.open_calendar", lambda _config: calendar)
    monkeypatch.setattr("baton.cli.cmd_calendar.open_docs", lambda _config: fake)

    assert call(studio, "in-progress") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    assert "video_link" not in payload["in_progress"][0]
    assert fake.blocks.get("doc-bruno-01") is None


# -- activate / deactivate ------------------------------------------------------


def _deactivate_in_sqlite(studio, name: str) -> None:
    """The studio's own tool is `learner deactivate`; the raw UPDATE here is
    the database's own truth for tests that start from an already-gone learner."""
    profile, _ = studio
    connection = sqlite3.connect(profile / "data" / "studio.db")
    connection.execute("UPDATE learners SET is_active = 0 WHERE name = ?", (name,))
    connection.commit()
    connection.close()


def test_deactivate_hides_a_learner_from_the_default_list(studio, capsys):
    assert call(studio, "deactivate", "Clara Nguyen") == Exit.OK
    capsys.readouterr()

    assert call(studio, "list") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 3
    assert payload["scope"] == "active"
    assert payload["hidden_inactive"] == 1
    assert all(item["name"] != "Clara Nguyen" for item in payload["learners"])

    assert call(studio, "list", "--all") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 4
    clara = next(item for item in payload["learners"] if item["name"] == "Clara Nguyen")
    assert clara["is_active"] is False


def test_the_default_list_names_how_many_it_is_hiding(studio, capsys):
    assert call(studio, "deactivate", "Clara Nguyen", "Devon Marsh") == Exit.OK
    capsys.readouterr()

    profile, _ = studio
    assert run(["--profile", str(profile), "learner", "list"]) == Exit.OK
    human = capsys.readouterr().out

    assert "(2 no longer studying; --all shows them)" in human


def test_deactivate_resolves_every_name_before_writing(studio, capsys):
    """One typo in the batch must not leave the earlier names already marked."""
    assert call(studio, "deactivate", "Clara Nguyen", "Nobody At All") == Exit.NEEDS_HUMAN
    capsys.readouterr()

    assert call(studio, "list", "--all") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    clara = next(item for item in payload["learners"] if item["name"] == "Clara Nguyen")
    assert clara["is_active"] is True


def test_deactivate_is_idempotent(studio, capsys):
    assert call(studio, "deactivate", "Clara Nguyen") == Exit.OK
    capsys.readouterr()
    assert call(studio, "deactivate", "Clara Nguyen") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["learners"][0]["is_active"] is False


def test_activate_brings_a_learner_back(studio, capsys):
    assert call(studio, "deactivate", "Clara Nguyen") == Exit.OK
    capsys.readouterr()

    assert call(studio, "activate", "Clara Nguyen") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["active"] is True
    assert payload["learners"][0]["is_active"] is True


def test_show_still_reaches_an_inactive_learners_history(studio, capsys):
    assert call(studio, "deactivate", "Clara Nguyen") == Exit.OK
    capsys.readouterr()

    assert call(studio, "show", "Clara Nguyen") == Exit.OK
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["learner"]["is_active"] is False
    # The warning rides stderr so the JSON envelope stays one document.
    assert "no longer studying" in captured.err


# -- rename ---------------------------------------------------------------------


def test_rename_rewrites_the_row_and_the_history_follows(studio, capsys):
    """The replacement flow from production (2026-09-13): a new learner
    takes over a leaver's slot, so the row keeps its id, its sessions, and
    its piece assignment, and only the name moves."""
    assert call(studio, "assign", "Clara Nguyen", "--piece", "1") == Exit.OK
    capsys.readouterr()

    assert call(studio, "rename", "Clara Nguyen", "--to", "Clara Nunez") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["renamed_from"] == "Clara Nguyen"
    assert payload["learner"]["name"] == "Clara Nunez"

    assert call(studio, "show", "Clara Nunez") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["learner"]["id"] == "3"
    assert payload["current_piece"]["title"] == "Autumn Leaves"
    # The old name no longer resolves: identity moved with the row.
    assert call(studio, "show", "Clara Nguyen") == Exit.NEEDS_HUMAN


def test_rename_refuses_a_name_another_learner_has(studio, capsys):
    assert call(studio, "rename", "Clara Nguyen", "--to", "Ada Whitfield") == Exit.GATE
    capsys.readouterr()

    assert call(studio, "list", "--all") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    names = [item["name"] for item in payload["learners"]]
    assert names.count("Ada Whitfield") == 1
    assert "Clara Nguyen" in names


def test_rename_dry_run_writes_nothing(studio, capsys):
    assert call(studio, "rename", "Clara Nguyen", "--to", "Clara Nunez", "--dry-run") == (Exit.OK)
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["to"] == "Clara Nunez"

    assert call(studio, "show", "Clara Nguyen") == Exit.OK


def test_rename_of_an_unknown_name_is_needs_human(studio, capsys):
    assert call(studio, "rename", "Nobody At All", "--to", "Someone") == Exit.NEEDS_HUMAN


# -- trash / untrash --------------------------------------------------------


def test_trash_hides_a_learner_even_from_all(studio, capsys):
    """Unlike deactivate, trash is gone from --all too."""
    assert call(studio, "trash", "Clara Nguyen") == Exit.OK
    capsys.readouterr()

    assert call(studio, "list", "--all") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert all(item["name"] != "Clara Nguyen" for item in payload["learners"])

    assert call(studio, "list", "--trashed") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == "trashed"
    trashed = next(item for item in payload["learners"] if item["name"] == "Clara Nguyen")
    assert trashed["deleted_at"] is not None


def test_trash_stops_the_name_from_resolving_for_ordinary_commands(studio, capsys):
    assert call(studio, "trash", "Clara Nguyen") == Exit.OK
    capsys.readouterr()

    assert call(studio, "show", "Clara Nguyen") == Exit.NEEDS_HUMAN
    assert call(studio, "rename", "Clara Nguyen", "--to", "Someone Else") == Exit.NEEDS_HUMAN


def test_trash_is_idempotent(studio, capsys):
    assert call(studio, "trash", "Clara Nguyen") == Exit.OK
    capsys.readouterr()
    assert call(studio, "trash", "Clara Nguyen") == Exit.OK


def test_untrash_brings_a_learner_back_with_history_intact(studio, capsys):
    assert call(studio, "assign", "Clara Nguyen", "--piece", "1") == Exit.OK
    capsys.readouterr()
    assert call(studio, "trash", "Clara Nguyen") == Exit.OK
    capsys.readouterr()

    assert call(studio, "untrash", "Clara Nguyen") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["learner"]["deleted_at"] is None

    assert call(studio, "list", "--all") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    clara = next(item for item in payload["learners"] if item["name"] == "Clara Nguyen")
    assert clara["is_active"] is True

    assert call(studio, "show", "Clara Nguyen") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["current_piece"]["title"] == "Autumn Leaves"


def test_trash_dry_run_writes_nothing(studio, capsys):
    assert call(studio, "trash", "Clara Nguyen", "--dry-run") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True

    assert call(studio, "list", "--all") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert any(item["name"] == "Clara Nguyen" for item in payload["learners"])


def test_trash_of_an_unknown_name_is_needs_human(studio, capsys):
    assert call(studio, "trash", "Nobody At All") == Exit.NEEDS_HUMAN


def test_untrash_of_a_name_that_was_never_trashed_still_resolves(studio, capsys):
    assert call(studio, "untrash", "Clara Nguyen") == Exit.OK


def test_list_and_all_refuse_together_with_trashed(studio, capsys):
    assert call(studio, "list", "--all", "--trashed") == Exit.USAGE


# -- edit ----------------------------------------------------------------------


def test_edit_changes_the_named_fields_and_nothing_else(studio, capsys):
    assert call(studio, "edit", "Clara Nguyen", "--instrument", "violin") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["changes"] == {"instrument": "violin"}
    assert payload["learner"]["instrument"] == "violin"

    assert call(studio, "show", "Clara Nguyen") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["learner"]["instrument"] == "violin"


def test_edit_has_instrument_flag_round_trip(studio, capsys):
    assert call(studio, "edit", "Clara Nguyen", "--no-has-instrument") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["learner"]["has_instrument"] is False

    assert call(studio, "edit", "Clara Nguyen", "--has-instrument") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["learner"]["has_instrument"] is True


def test_edit_dry_run_writes_nothing(studio, capsys):
    assert call(studio, "edit", "Clara Nguyen", "--tone", "child", "--dry-run") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True

    assert call(studio, "show", "Clara Nguyen") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["learner"]["tone"] != "child"


def test_edit_without_any_field_is_usage(studio, capsys):
    assert call(studio, "edit", "Clara Nguyen") == Exit.USAGE


def test_edit_of_an_unknown_name_is_needs_human(studio, capsys):
    assert call(studio, "edit", "Nobody At All", "--tone", "child") == Exit.NEEDS_HUMAN


def test_the_status_commands_need_something_to_do(studio, capsys):
    assert call(studio, "deactivate") == Exit.USAGE
    assert call(studio, "activate") == Exit.USAGE
    capsys.readouterr()

    profile, _ = studio
    args = [
        "--profile",
        str(profile),
        "--json",
        "learner",
        "deactivate",
        "--serve",
        "Clara Nguyen",
    ]
    assert run(args) == Exit.USAGE


def test_an_inactive_learner_leaves_the_ambiguity_choices(studio, capsys):
    _deactivate_in_sqlite(studio, "Bruno Castell")
    capsys.readouterr()

    # "a" partial-matches everyone; Bruno (inactive) must drop out of the
    # choices while the active learners stay, which is the booking that
    # prompted the feature.
    assert call(studio, "show", "a") == Exit.NEEDS_HUMAN

    payload = json.loads(capsys.readouterr().out)
    names = [c["name"] for c in payload["details"]["candidates"]]
    assert "Bruno Castell" not in names
    assert "Ada Whitfield" in names


def test_a_partial_that_only_matches_the_gone_is_named_in_the_remedy(studio, capsys):
    _deactivate_in_sqlite(studio, "Clara Nguyen")
    capsys.readouterr()

    assert call(studio, "show", "Cla") == Exit.NEEDS_HUMAN

    payload = json.loads(capsys.readouterr().out)
    assert payload["details"]["candidates"] == []
    assert "Clara Nguyen" in payload["remedy"]


# -- lessons: every lesson, section by section, with its recording -------------


def _lesson_page(overview: str, *extra: Block) -> list[Block]:
    return [
        Block(id="h1", type="heading_2", text="ภาพรวมการเรียน"),
        Block(id="p1", type="paragraph", text=overview),
        Block(id="h2", type="heading_2", text="การบ้าน"),
        Block(id="p2", type="paragraph", text="ซ้อม G C D"),
        *extra,
    ]


def test_lessons_reads_every_lesson_that_happened(studio, capsys):
    """Done and in-progress sessions only: an unstarted page has nothing to recall."""
    _, fake = studio
    fake.blocks["doc-ada-01"] = _lesson_page(
        "เริ่มคอร์ด",
        Block(id="v", type="video", text="", url="https://youtu.be/AbCdEfGhIjK"),
    )
    fake.blocks["doc-ada-02"] = _lesson_page("คล่องขึ้น")

    assert call(studio, "lessons", "Ada Whitfield") == Exit.OK

    lessons = json.loads(capsys.readouterr().out)["lessons"]
    assert [lesson["number"] for lesson in lessons] == [1, 2]
    assert lessons[0]["sections"]["overview"] == "เริ่มคอร์ด"
    assert lessons[0]["recording_id"] == "AbCdEfGhIjK"
    assert lessons[1]["recording"] == ""


def test_the_song_being_learnt_is_not_taken_for_the_recording(studio, capsys):
    profile, fake = studio
    connection = sqlite3.connect(profile / "data" / "studio.db")
    connection.execute(
        "UPDATE pieces SET source_link = 'https://www.youtube.com/watch?v=SongSongSon' WHERE id = 2"
    )
    connection.commit()
    connection.close()
    fake.blocks["doc-ada-02"] = _lesson_page(
        "เพลงใหม่",
        Block(id="b", type="bookmark", text="", url="https://youtu.be/SongSongSon"),
    )

    call(studio, "lessons", "Ada Whitfield")

    lessons = json.loads(capsys.readouterr().out)["lessons"]
    assert lessons[1]["recording"] == ""


def test_lessons_can_read_one_session_whatever_its_state(studio, capsys):
    assert call(studio, "lessons", "Ada Whitfield", "--session", "3") == Exit.OK
    lessons = json.loads(capsys.readouterr().out)["lessons"]
    assert [lesson["number"] for lesson in lessons] == [3]
