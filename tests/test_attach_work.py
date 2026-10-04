"""`learner attach-work`: a recording that skipped the pipeline, put on its page.

The video pipeline writes the lesson's own recording onto the session page,
but it is the only writer. A recording that did not go through it: a
teacher's own edit, a clip shared directly: had links in the database and
nothing on any page, and the Drive side of a recording never landed on a page
at all.
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path

import pytest
from tests.test_send_video import FakeMessenger  # noqa: F401 - keeps linters honest about reuse

import baton
from baton.adapters.docs.base import Block, DocStatus
from baton.adapters.fakes import FakeDocStore
from baton.cli.app import run
from baton.exits import Exit
from baton.pipelines.recording import attach_work, compose_recording, recording_blocks
from baton.pipelines.staging import PublishedRecord, StagingStore

MIGRATIONS = Path(baton.__file__).resolve().parent / "migrations"

WORK = {
    "id": "w1",
    "learner_id": "1",
    "title": "Blackbird: full take",
    "type": "performance",
    "video_link": "https://youtu.be/attach-me",
    "drive_link": "https://drive.google.com/file/keep-me/view",
    "performed_date": "2026-08-23",
}


@pytest.fixture
def studio(profile, monkeypatch):
    """One learner, session 3 In progress, one recorded work with both sides."""
    db_path = profile / "data" / "studio.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.executescript((MIGRATIONS / "sqlite.sql").read_text(encoding="utf-8"))
    connection.executescript((MIGRATIONS / "seed_example.sql").read_text(encoding="utf-8"))
    connection.execute(
        "INSERT INTO works (learner_id, title, type, video_link, drive_link, performed_date) "
        "VALUES ('1', ?, 'performance', ?, ?, '2026-08-23')",
        (WORK["title"], WORK["video_link"], WORK["drive_link"]),
    )
    connection.commit()
    connection.close()

    (profile / "baton.yaml").write_text(
        textwrap.dedent(
            """
            version: 1
            labels:
              learner: student
              session: week
            db:
              driver: sqlite
              sqlite:
                path: data/studio.db
            docs:
              driver: notion
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    docs = FakeDocStore(
        statuses={
            "doc-ada-01": DocStatus(doc_id="doc-ada-01", status="Complete"),
            "doc-ada-02": DocStatus(doc_id="doc-ada-02", status="Not started"),
            "doc-ada-03": DocStatus(doc_id="doc-ada-03", status="In progress"),
        },
        blocks={"doc-ada-03": []},
        wording={"done": "Done", "in_progress": "In progress", "not_started": "Not started"},
    )
    monkeypatch.setattr("baton.cli.cmd_learner.open_docs", lambda _config: docs)
    monkeypatch.setattr("baton.cli.cmd_lesson.open_docs", lambda _config: docs)
    monkeypatch.setattr("baton.pipelines.recording.open_docs", lambda _config: docs)
    return profile, docs


def call(studio, *args):
    return run(["--profile", str(studio[0]), "--json", "learner", *args])


def out(capsys):
    return json.loads(capsys.readouterr().out)


def _work(**overrides):
    from baton.domain.models import Work

    return Work(**{**WORK, **overrides})


# -- the blocks ---------------------------------------------------------------


def test_the_section_matches_the_shape_the_old_push_wrote():
    blocks = recording_blocks(_work())

    assert blocks[0]["type"] == "heading_3"
    assert blocks[0]["heading_3"]["rich_text"][0]["text"]["content"] == "🎬 ผลงาน Record"
    title_runs = blocks[1]["paragraph"]["rich_text"]
    assert title_runs[1]["text"]["content"] == WORK["title"]
    assert title_runs[1]["annotations"]["bold"] is True
    assert blocks[2]["video"]["external"]["url"] == WORK["video_link"]
    assert blocks[3]["bookmark"]["url"] == WORK["drive_link"]


def test_only_the_sides_the_work_has_are_built():
    blocks = recording_blocks(_work(drive_link=""))

    assert [b["type"] for b in blocks] == ["heading_3", "paragraph", "video"]


# -- the attach, and its idempotency ------------------------------------------


def test_a_fresh_work_writes_the_whole_section(studio):
    docs = studio[1]

    result = attach_work(docs, "doc-ada-03", _work())

    assert result["appended"] == 4
    urls = [b.url for b in docs.list_blocks("doc-ada-03")]
    assert WORK["video_link"] in urls
    assert WORK["drive_link"] in urls


def test_a_link_already_on_the_page_is_not_written_twice(studio):
    """The old rule cleared every video and bookmark first: safe when it was
    the only writer, fatal now that the pipeline puts the lesson's own video
    on the same page. The guard is the URL."""
    docs = studio[1]

    first = attach_work(docs, "doc-ada-03", _work())
    second = attach_work(docs, "doc-ada-03", _work())

    assert first["appended"] == 4
    assert second["appended"] == 0
    assert set(second["already_on_page"]) == {WORK["video_link"], WORK["drive_link"]}
    # Still exactly one section on the page.
    assert len(docs.list_blocks("doc-ada-03")) == 4


def test_one_new_side_carries_a_fresh_section_for_it(studio):
    docs = studio[1]
    docs.blocks["doc-ada-03"] = [
        Block(id="v", type="video", url=WORK["video_link"]),
    ]

    result = attach_work(docs, "doc-ada-03", _work())

    # Heading + title + the Drive side; the video side was already there.
    assert result["appended"] == 3
    assert result["already_on_page"] == [WORK["video_link"]]


def test_the_lessons_own_video_is_never_touched(studio):
    docs = studio[1]
    docs.blocks["doc-ada-03"] = [
        Block(id="lesson", type="video", url="https://youtu.be/the-lesson-itself"),
    ]

    attach_work(docs, "doc-ada-03", _work())

    urls = [b.url for b in docs.list_blocks("doc-ada-03")]
    assert urls.count("https://youtu.be/the-lesson-itself") == 1


def test_a_work_with_no_links_is_refused(studio):
    import pytest

    from baton.errors import GateError

    with pytest.raises(GateError, match="no link to put on the page"):
        attach_work(studio[1], "doc-ada-03", _work(video_link="", drive_link=""))


# -- the command ---------------------------------------------------------------


def test_without_a_pick_it_lists_and_asks(studio, capsys):
    assert call(studio, "attach-work", "Ada Whitfield") == Exit.NEEDS_HUMAN

    payload = out(capsys)
    assert payload["details"]["candidates"][0]["name"] == WORK["title"]
    assert studio[1].list_blocks("doc-ada-03") == []


def test_it_targets_the_session_in_progress(studio, capsys):
    assert call(studio, "attach-work", "Ada Whitfield", "--pick", "1") == Exit.OK

    payload = out(capsys)
    assert payload["doc_id"] == "doc-ada-03"
    assert payload["appended"] == 4


def test_an_explicit_session_is_honoured(studio, capsys):
    assert call(studio, "attach-work", "Ada Whitfield", "--pick", "1", "--session", "1") == Exit.OK

    payload = out(capsys)
    assert payload["doc_id"] == "doc-ada-01"


def test_no_session_in_progress_names_the_way_out(studio, capsys):
    docs = studio[1]
    docs.statuses["doc-ada-03"] = DocStatus(doc_id="doc-ada-03", status="Not started")

    assert call(studio, "attach-work", "Ada Whitfield", "--pick", "1") == Exit.USAGE

    payload = out(capsys)
    assert "none is in progress" in payload["message"]
    assert "--session" in payload["remedy"]


def test_dry_run_writes_nothing(studio, capsys):
    assert call(studio, "attach-work", "Ada Whitfield", "--pick", "1", "--dry-run") == Exit.OK

    payload = out(capsys)
    assert payload["dry_run"] is True
    assert payload["would_append"] == 4
    assert studio[1].list_blocks("doc-ada-03") == []


def test_adding_a_work_for_a_session_publishes_the_send_record(studio, capsys):
    """A recording-only lesson follows the summary boundary, without a summary."""

    profile, docs = studio
    code = run(
        [
            "--profile",
            str(profile),
            "--json",
            "learner",
            "add-work",
            "Ada Whitfield",
            "--title",
            "Blackbird: finished take",
            "--type",
            "cover",
            "--video-link",
            WORK["video_link"],
            "--drive-link",
            WORK["drive_link"],
            "--date",
            "2026-08-23",
            "--session",
            "3",
        ]
    )

    assert code == Exit.OK
    payload = out(capsys)
    assert payload["kind"] == "recording"
    assert payload["published"] is True

    status = docs.get_status("doc-ada-03")
    assert status.status == "Done"
    assert status.date == "2026-08-23"
    assert status.titles == "Blackbird: finished take"

    blocks = docs.list_blocks("doc-ada-03")
    page_text = "\n".join(block.text for block in blocks)
    message = payload["message"]
    assert all(line in page_text for line in message.splitlines() if line)
    assert WORK["video_link"] in {block.url for block in blocks}
    assert WORK["drive_link"] in {block.url for block in blocks}

    draft = StagingStore(profile / "state" / "lessons").get("1")
    assert draft is not None
    assert draft.kind == "recording"
    assert draft.summary is None
    assert draft.status == "published"
    assert draft.work is not None
    assert draft.work.title == "Blackbird: finished take"

    record = PublishedRecord(profile / "state" / "published").get("1", 3)
    assert record is not None
    assert record["kind"] == "recording"
    assert record["work"]["title"] == "Blackbird: finished take"
    assert record["short_message"] == message
    assert record["blocks"]

    assert (
        run(["--profile", str(profile), "--json", "lesson", "contract", "Ada Whitfield"])
        == Exit.USAGE
    )
    assert "recording" in json.loads(capsys.readouterr().out)["message"]


def test_unpublishing_a_recording_lesson_restores_the_open_draft(studio, capsys):
    profile, docs = studio
    run(
        [
            "--profile",
            str(profile),
            "--json",
            "learner",
            "add-work",
            "Ada Whitfield",
            "--title",
            "Blackbird: withdrawn take",
            "--video-link",
            WORK["video_link"],
            "--session",
            "3",
        ]
    )
    capsys.readouterr()
    before = len(docs.list_blocks("doc-ada-03"))

    assert (
        run(["--profile", str(profile), "--json", "lesson", "unpublish", "Ada Whitfield"])
        == Exit.OK
    )
    payload = out(capsys)

    assert payload["removed"] == before
    assert docs.list_blocks("doc-ada-03") == []
    assert docs.get_status("doc-ada-03").status == "In progress"
    assert PublishedRecord(profile / "state" / "published").get("1", 3) is None
    draft = StagingStore(profile / "state" / "lessons").get("1")
    assert draft is not None
    assert draft.kind == "recording"
    assert draft.status == "staged"

    assert (
        run(["--profile", str(profile), "--json", "lesson", "publish", "Ada Whitfield"]) == Exit.OK
    )
    capsys.readouterr()
    assert docs.get_status("doc-ada-03").status == "Done"
    assert PublishedRecord(profile / "state" / "published").get("1", 3) is not None


def test_completing_an_existing_work_publishes_it_as_the_lesson(studio, capsys):
    profile, docs = studio

    assert (
        call(
            studio,
            "attach-work",
            "Ada Whitfield",
            "--pick",
            "1",
            "--complete",
            "--session",
            "3",
        )
        == Exit.OK
    )
    payload = out(capsys)

    assert payload["kind"] == "recording"
    assert payload["published"] is True
    assert docs.get_status("doc-ada-03").status == "Done"
    assert PublishedRecord(profile / "state" / "published").get("1", 3) is not None


def test_running_it_twice_is_still_one_section(studio, capsys):
    call(studio, "attach-work", "Ada Whitfield", "--pick", "1")
    capsys.readouterr()

    assert call(studio, "attach-work", "Ada Whitfield", "--pick", "1") == Exit.OK

    payload = out(capsys)
    assert payload["appended"] == 0
    assert len(studio[1].list_blocks("doc-ada-03")) == 4


def test_a_retry_claims_the_existing_section_instead_of_losing_ownership(studio):
    docs = studio[1]
    message = compose_recording(_work(), learner_name="Ada Whitfield")
    first = attach_work(docs, "doc-ada-03", _work(), message=message)

    second = attach_work(docs, "doc-ada-03", _work(), message=message)

    assert second["appended"] == 0
    assert [entry["id"] for entry in second["blocks"]] == [entry["id"] for entry in first["blocks"]]
    assert len(docs.list_blocks("doc-ada-03")) == len(first["blocks"])


def test_a_complete_recording_still_writes_the_message_over_bare_links(studio, capsys):
    profile, docs = studio
    docs.blocks["doc-ada-03"] = [
        Block(id="old-video", type="video", url=WORK["video_link"]),
        Block(id="old-drive", type="bookmark", url=WORK["drive_link"]),
    ]

    assert (
        call(
            studio,
            "attach-work",
            "Ada Whitfield",
            "--pick",
            "1",
            "--complete",
            "--session",
            "3",
        )
        == Exit.OK
    )
    payload = out(capsys)

    assert payload["appended"] > 0
    assert payload["message"]
    page_text = "\n".join(block.text for block in docs.list_blocks("doc-ada-03"))
    assert all(line in page_text for line in payload["message"].splitlines() if line)
    record = PublishedRecord(profile / "state" / "published").get("1", 3)
    assert record is not None
    assert record["blocks"]


def _add_work(profile, title="Blackbird: finished take"):
    return run(
        [
            "--profile",
            str(profile),
            "--json",
            "learner",
            "add-work",
            "Ada Whitfield",
            "--title",
            title,
            "--drive-link",
            WORK["drive_link"],
            "--session",
            "3",
        ]
    )


def _stage_summary(profile, session: int, status: str):
    from baton.pipelines.staging import LessonDraft, PieceSnapshot

    staging = StagingStore(profile / "state" / "lessons")
    draft = LessonDraft(
        learner_id="1",
        learner_name="Ada Whitfield",
        session_number=session,
        piece_snapshot=PieceSnapshot.capture(None),
        doc_id=f"doc-ada-0{session}",
    )
    draft.status = status
    staging.save(draft)


def _work_count(profile) -> int:
    connection = sqlite3.connect(profile / "data" / "studio.db")
    try:
        return connection.execute("SELECT COUNT(*) FROM works WHERE learner_id = '1'").fetchone()[0]
    finally:
        connection.close()


def test_the_previous_lessons_published_summary_does_not_block_a_recording(studio, capsys):
    """Every learner who had a summary last week has its published draft on disk.

    `lesson stage` replaces a published draft; a recording lesson must too, or
    no learner with a past summary could ever close a lesson at its recording.
    """
    profile, docs = studio
    _stage_summary(profile, 2, "published")

    assert _add_work(profile) == Exit.OK
    payload = out(capsys)
    assert payload["published"] is True
    assert docs.get_status("doc-ada-03").status == "Done"
    draft = StagingStore(profile / "state" / "lessons").get("1")
    assert draft is not None and draft.kind == "recording" and draft.session_number == 3


def test_an_unfinished_summary_refuses_before_the_work_is_written(studio, capsys):
    """A summary still owed is the teacher's work: refused, and nothing recorded.

    The refusal used to come after the row was written, so every retry of a
    refused press added the same work again.
    """
    profile, docs = studio
    _stage_summary(profile, 3, "staged")
    before = _work_count(profile)

    assert _add_work(profile) == Exit.USAGE
    assert "summary draft" in out(capsys)["message"]
    assert _work_count(profile) == before
    assert docs.get_status("doc-ada-03").status == "In progress"
