"""The full loop: stage → contract → ingest → render → publish.

Runs the real SQLite driver and a scripted document store, so the only thing
substituted is the network.
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
from baton.exits import Exit
from baton.pipelines.staging import LessonDraft, PublishedRecord, StagingStore

MIGRATIONS = Path(baton.__file__).resolve().parent / "migrations"

SUMMARY = {
    "overview": ["Held the tempo through the whole B section."],
    "covered": [{"topic": "Blackbird bars 9-16", "detail": "Thumb-and-finger pattern"}],
    "focus": [{"issue": "Late change to C", "fix": "Four beats each, alone"}],
    "goals": ["Bars 9-16 at 80bpm with the backing track"],
    "short_summary": {
        "covered": "Blackbird bars 9 to 16",
        "progress": "Tempo held without stopping",
        "homework": "Bars 9-16 at 80bpm",
    },
}


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
              preserve:
                - type: video
                - type: embed
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (profile / "theory.json").write_text(
        json.dumps({"vibrato": "Pick first, then oscillate from the wrist."}),
        encoding="utf-8",
    )

    fake = FakeDocStore(
        statuses={
            "doc-ada-01": DocStatus(doc_id="doc-ada-01", status="Complete", date="2026-05-01"),
            "doc-ada-02": DocStatus(doc_id="doc-ada-02", status="Complete", date="2026-06-01"),
            "doc-ada-03": DocStatus(doc_id="doc-ada-03", status="Not started"),
        },
        blocks={
            "doc-ada-03": [
                Block(id="vid", type="video", url="https://example.invalid/watch/ada-3"),
            ]
        },
        wording={"done": "Complete", "in_progress": "In progress"},
    )
    for module in ("cmd_learner", "cmd_lesson"):
        monkeypatch.setattr(f"baton.cli.{module}.open_docs", lambda _config: fake)
    return profile, fake


def call(studio, *args):
    profile, _ = studio
    return run(["--profile", str(profile), "--json", "lesson", *args])


def out(capsys):
    return json.loads(capsys.readouterr().out)


def stage_ada(studio, capsys):
    assert call(studio, "stage", "Ada Whitfield", "--session", "3", "--context", "notes") == Exit.OK
    return out(capsys)


# -- stage -------------------------------------------------------------------


def test_stage_accepts_an_explicit_session(studio, capsys):
    assert call(studio, "stage", "Ada Whitfield", "--session", "3") == Exit.OK

    assert out(capsys)["session_number"] == 3


def test_staging_an_explicit_session_needs_no_document_reads(studio, capsys):
    """The teacher writes the notes straight after the lesson, often on a
    phone. When the session is named, both the number and the document id come
    from the database, so a document-store outage must not block staging."""
    from baton.errors import UpstreamError

    _, docs = studio
    docs.fail_with = UpstreamError("notion is down", service="notion")

    assert call(studio, "stage", "Ada Whitfield", "--session", "3", "--context", "n") == Exit.OK
    assert out(capsys)["session_number"] == 3


def test_choosing_the_session_automatically_does_read_the_documents(studio, capsys):
    """ "Free" is a fact about the page, not the database row, so this path
    genuinely needs the document store and must fail when it is unreachable."""
    from baton.errors import UpstreamError

    _, docs = studio
    docs.fail_with = UpstreamError("notion is down", service="notion")

    assert call(studio, "stage", "Ada Whitfield") == Exit.UPSTREAM


def test_stage_refuses_a_session_that_does_not_exist(studio, capsys):
    assert call(studio, "stage", "Ada Whitfield", "--session", "99") == Exit.USAGE

    assert "99" in out(capsys)["message"]


def test_stage_reads_context_from_a_file(studio, capsys, tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("worked on bars 9-16", encoding="utf-8")

    call(studio, "stage", "Ada Whitfield", "--session", "3", "--context-file", str(notes))
    capsys.readouterr()

    call(studio, "show", "Ada Whitfield")
    assert out(capsys)["context"] == "worked on bars 9-16"


# -- contract ----------------------------------------------------------------


def test_contract_gives_the_model_everything_it_needs(studio, capsys):
    stage_ada(studio, capsys)

    assert call(studio, "contract", "Ada Whitfield") == Exit.OK
    payload = out(capsys)

    assert payload["schema"]["title"] == "Lesson summary"
    assert payload["context"]["session_number"] == 3
    assert payload["context"]["lesson_notes"] == "notes"
    assert payload["context"]["current_piece"]["title"] == "Blackbird"
    assert payload["constraints"]["available_callout_ids"] == ["vibrato"]


def test_contract_needs_a_staged_lesson_first(studio, capsys):
    assert call(studio, "contract", "Bruno Castell") == Exit.USAGE

    assert "stage" in out(capsys)["remedy"]


# -- ingest ------------------------------------------------------------------


def test_ingest_accepts_a_valid_summary(studio, capsys):
    stage_ada(studio, capsys)

    assert call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(SUMMARY)) == Exit.OK
    assert out(capsys)["has_summary"] is True


def test_ingest_rejects_a_summary_with_emoji_in_the_message(studio, capsys):
    stage_ada(studio, capsys)
    bad = json.loads(json.dumps(SUMMARY))
    bad["short_summary"]["progress"] = "Brilliant 🎉"

    assert call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(bad)) == Exit.CONTRACT

    payload = out(capsys)
    assert payload["error"] == "contract"
    assert any("emoji" in v["reason"] for v in payload["details"]["violations"])


def test_a_rejected_summary_is_not_stored(studio, capsys):
    """Nothing partial. A rejected attempt must leave no trace, or the next
    command would publish half-validated content."""
    stage_ada(studio, capsys)
    bad = {"overview": ["only this"]}

    call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(bad))
    capsys.readouterr()

    call(studio, "show", "Ada Whitfield")
    assert out(capsys)["summary"] is None


def test_ingest_rejects_an_unknown_callout_id(studio, capsys):
    stage_ada(studio, capsys)
    bad = {**SUMMARY, "callouts": ["invented-technique"]}

    assert call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(bad)) == Exit.CONTRACT
    assert any("invented-technique" in v["reason"] for v in out(capsys)["details"]["violations"])


def test_malformed_json_is_a_contract_failure_not_a_usage_error(studio, capsys):
    """The model produced it, so the model is what has to fix it, which means
    exit 4, the code that tells an agent to try again."""
    stage_ada(studio, capsys)

    assert call(studio, "ingest", "Ada Whitfield", "--json-text", "{not json") == Exit.CONTRACT
    assert "JSON" in out(capsys)["message"]


def test_ingest_reads_a_file(studio, capsys, tmp_path):
    stage_ada(studio, capsys)
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(SUMMARY), encoding="utf-8")

    assert call(studio, "ingest", "Ada Whitfield", "--file", str(path)) == Exit.OK


# -- render ------------------------------------------------------------------


def test_render_produces_markdown(studio, capsys):
    stage_ada(studio, capsys)
    call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(SUMMARY))
    capsys.readouterr()

    assert call(studio, "render", "Ada Whitfield") == Exit.OK
    assert "Blackbird bars 9-16" in out(capsys)["markdown"]


def test_render_produces_the_parent_message(studio, capsys):
    stage_ada(studio, capsys)
    call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(SUMMARY))
    capsys.readouterr()

    call(studio, "render", "Ada Whitfield", "--format", "message")
    assert out(capsys)["message"].startswith("• Covered: Blackbird bars 9 to 16")


def test_render_needs_an_accepted_summary(studio, capsys):
    stage_ada(studio, capsys)

    assert call(studio, "render", "Ada Whitfield") == Exit.USAGE


# -- publish -----------------------------------------------------------------


def prepared(studio, capsys):
    stage_ada(studio, capsys)
    call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(SUMMARY))
    capsys.readouterr()


def test_publish_writes_the_summary_and_keeps_the_recording(studio, capsys):
    prepared(studio, capsys)
    _, docs = studio

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK
    payload = out(capsys)

    assert payload["appended"] > 0
    assert payload["preserved"] == 1
    assert "vid" in {block.id for block in docs.list_blocks("doc-ada-03")}


def test_publish_updates_the_youtube_description_when_a_video_is_linked(
    studio, capsys, monkeypatch
):
    """The just-published summary lands on the video's description too, the
    way the studio's previous pipeline did, but gated through Baton's own
    ownership check rather than trusting whatever link sits on the page."""
    from baton.adapters.fakes import FakePublisher

    _, docs = studio
    docs.blocks["doc-ada-03"] = [
        Block(id="vid", type="video", url="https://youtu.be/dQw4w9WgXcQ"),
    ]
    fake_publisher = FakePublisher()
    monkeypatch.setattr("baton.cli.cmd_lesson.open_publisher", lambda _config: fake_publisher)
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK
    payload = out(capsys)

    assert payload["youtube"] == {"status": "ok", "video_id": "dQw4w9WgXcQ"}
    description = fake_publisher.descriptions["dQw4w9WgXcQ"]
    assert "Blackbird bars 9-16" in description
    assert "Ada Whitfield" in description


def test_publish_skips_the_youtube_step_when_youtube_is_not_configured(studio, capsys):
    """The fixture's `baton.yaml` has no `media:` section: the ordinary shape
    of a studio that only uses Baton for lesson summaries. This must not be
    an error; `open_publisher` raising `ConfigError` is the expected signal
    that there is nothing to update."""
    _, docs = studio
    docs.blocks["doc-ada-03"] = [
        Block(id="vid", type="video", url="https://youtu.be/dQw4w9WgXcQ"),
    ]
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK
    assert out(capsys)["youtube"] is None


def test_publish_skips_the_youtube_step_when_there_is_no_video_yet(studio, capsys, monkeypatch):
    """`doc-ada-03`'s only block in the base fixture is a non-YouTube link:
    the ordinary state before a recording has been attached."""
    from baton.adapters.fakes import FakePublisher

    monkeypatch.setattr("baton.cli.cmd_lesson.open_publisher", lambda _config: FakePublisher())
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK
    assert out(capsys)["youtube"] is None


def test_publish_reports_but_does_not_fail_on_a_foreign_video(studio, capsys, monkeypatch):
    """A reference link on the document: a tutorial on someone else's
    channel: must not be overwritten, but the summary itself already landed
    on the page, so the command still succeeds overall."""
    from baton.adapters.fakes import FakePublisher

    _, docs = studio
    docs.blocks["doc-ada-03"] = [
        Block(id="vid", type="video", url="https://youtu.be/dQw4w9WgXcQ"),
    ]
    fake_publisher = FakePublisher()
    fake_publisher.foreign_video_ids.add("dQw4w9WgXcQ")
    monkeypatch.setattr("baton.cli.cmd_lesson.open_publisher", lambda _config: fake_publisher)
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK
    payload = out(capsys)

    assert payload["appended"] > 0  # the summary itself still landed
    assert payload["youtube"]["status"] == "error"
    assert "dQw4w9WgXcQ" not in fake_publisher.descriptions


def test_publish_dry_run_changes_nothing(studio, capsys):
    prepared(studio, capsys)
    _, docs = studio
    before = list(docs.list_blocks("doc-ada-03"))

    assert call(studio, "publish", "Ada Whitfield", "--dry-run") == Exit.OK

    assert out(capsys)["dry_run"] is True
    assert docs.list_blocks("doc-ada-03") == before


def test_publishing_twice_is_refused_by_default(studio, capsys):
    """A second append leaves two copies of the summary on the page with
    nothing to tell them apart."""
    prepared(studio, capsys)
    call(studio, "publish", "Ada Whitfield")
    capsys.readouterr()
    _, docs = studio
    after_first = len(docs.list_blocks("doc-ada-03"))

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    assert out(capsys)["skipped"] == "already published"
    assert len(docs.list_blocks("doc-ada-03")) == after_first


def test_force_publishes_again(studio, capsys):
    prepared(studio, capsys)
    call(studio, "publish", "Ada Whitfield")
    capsys.readouterr()

    assert call(studio, "publish", "Ada Whitfield", "--force") == Exit.OK
    assert out(capsys)["appended"] > 0


def test_a_failed_publish_is_recorded_and_can_be_retried(studio, capsys):
    """The draft must survive the failure carrying the reason, so a re-run
    resumes rather than starting from nothing."""
    from baton.errors import UpstreamError

    prepared(studio, capsys)
    _, docs = studio
    docs.fail_with = UpstreamError("notion is down", service="notion")

    assert call(studio, "publish", "Ada Whitfield") == Exit.UPSTREAM
    capsys.readouterr()

    docs.fail_with = None
    call(studio, "show", "Ada Whitfield")
    payload = out(capsys)
    assert payload["targets"]["docs"]["status"] == "failed"
    assert payload["summary"] is not None  # the work was not lost

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK


def test_publish_stores_the_message_for_the_send_step(studio, capsys):
    """Composed once, at publish time, so what is sent later is exactly what
    was reviewed here."""
    profile, _ = studio
    prepared(studio, capsys)
    call(studio, "publish", "Ada Whitfield")
    capsys.readouterr()

    records = list((profile / "state" / "published").glob("*.json"))
    assert records
    saved = json.loads(records[0].read_text(encoding="utf-8"))
    assert saved["short_message"].startswith("• Covered:")
    assert saved["session_number"] == 3


def test_publish_marks_the_session_done(studio, capsys):
    """A summary on the page is not the same as a session that is over.

    Everything downstream reads the status, not the blocks: `next` writes to a
    fresh in-progress session, `prep` only briefs finished ones. Leaving the
    status alone after publishing kept pointing the next summary at the session
    that had just been written.
    """
    prepared(studio, capsys)
    _, docs = studio

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    assert docs.get_status("doc-ada-03").status == "Complete"


def test_publish_fills_the_date_and_titles_a_session_had_none_of(studio, capsys):
    """`prep` requires both, and nothing else in the loop ever wrote them."""
    prepared(studio, capsys)
    _, docs = studio

    call(studio, "publish", "Ada Whitfield")

    status = docs.get_status("doc-ada-03")
    assert status.date  # today, in the profile's timezone
    assert "Blackbird bars 9-16" in status.titles


def test_publish_does_not_overwrite_a_date_the_studio_already_set(studio, capsys):
    """The booked date is the day the lesson happened. Publishing can happen
    the following morning, so what it can infer is the worse record."""
    prepared(studio, capsys)
    _, docs = studio
    docs.set_properties("doc-ada-03", {"date": "2026-07-04", "titles": "Blackbird, by ear"})

    call(studio, "publish", "Ada Whitfield")

    status = docs.get_status("doc-ada-03")
    assert status.date == "2026-07-04"
    assert status.titles == "Blackbird, by ear"


def test_a_summary_that_lands_but_cannot_be_marked_done_says_so(studio, capsys):
    """The two writes fail independently. Reporting success here would hide
    exactly the state this fix exists to prevent: a published session that
    still looks in progress."""
    prepared(studio, capsys)
    _, docs = studio
    docs.fail_on_properties = True

    assert call(studio, "publish", "Ada Whitfield") == Exit.UPSTREAM
    payload = out(capsys)

    assert "marked done" in payload["message"]
    assert docs.list_blocks("doc-ada-03")  # the summary did land


def test_re_running_after_that_finishes_the_session_without_appending_again(studio, capsys):
    """The blocks are already where they belong, so the retry is the property
    write alone: appending a second copy is what the publish gate prevents."""
    prepared(studio, capsys)
    _, docs = studio
    docs.fail_on_properties = True
    call(studio, "publish", "Ada Whitfield")
    capsys.readouterr()
    after_first = len(docs.list_blocks("doc-ada-03"))

    docs.fail_on_properties = False
    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    assert len(docs.list_blocks("doc-ada-03")) == after_first
    assert docs.get_status("doc-ada-03").status == "Complete"


def test_publish_needs_a_summary(studio, capsys):
    stage_ada(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.USAGE


# -- housekeeping ------------------------------------------------------------


def test_list_shows_staged_lessons(studio, capsys):
    stage_ada(studio, capsys)

    assert call(studio, "list") == Exit.OK
    assert out(capsys)["count"] == 1


def test_remove_discards_a_draft(studio, capsys):
    stage_ada(studio, capsys)

    assert call(studio, "remove", "Ada Whitfield") == Exit.OK
    assert out(capsys)["removed"] is True

    call(studio, "list")
    assert out(capsys)["count"] == 0


def test_clear_requires_confirmation(studio, capsys):
    stage_ada(studio, capsys)

    assert call(studio, "clear") == Exit.USAGE
    capsys.readouterr()

    call(studio, "list")
    assert out(capsys)["count"] == 1


def test_clear_with_yes_discards_everything(studio, capsys):
    stage_ada(studio, capsys)

    assert call(studio, "clear", "--yes") == Exit.OK
    assert out(capsys)["removed"] == 1


def test_the_resolution_gate_applies_here_too(studio, capsys):
    assert call(studio, "stage", "Whitfield") == Exit.NEEDS_HUMAN
    assert out(capsys)["error"] == "needs_human"


def test_re_staging_does_not_reopen_the_publish_gate(studio, capsys):
    """The draft is one file per learner, overwritten by every `stage`.

    The "already published" gate used to key on a mark inside that file, so
    staging again between two publishes wiped the evidence and the second
    publish appended a duplicate summary to the same page: the exact outcome
    the gate exists to prevent. The published record, which is per session and
    survives re-staging, is the durable answer.
    """
    prepared(studio, capsys)
    call(studio, "publish", "Ada Whitfield")
    capsys.readouterr()
    _, docs = studio
    after_first = len(docs.list_blocks("doc-ada-03"))

    # Staging the same session again: a studio correcting a title, say.
    stage_ada(studio, capsys)
    call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(SUMMARY))
    capsys.readouterr()

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    assert out(capsys)["skipped"] == "already published"
    assert len(docs.list_blocks("doc-ada-03")) == after_first


# -- publish resumes the description update it owes (F5) ----------------------


def _video_on(docs):
    """Put the lesson's recording on the document, keeping whatever else is
    on the page."""
    docs.blocks["doc-ada-03"] = [
        block for block in docs.blocks.get("doc-ada-03", []) if block.type != "video"
    ] + [Block(id="vid", type="video", url="https://youtu.be/dQw4w9WgXcQ")]


def test_a_failed_description_update_is_retried_by_a_re_run(studio, capsys, monkeypatch):
    """The summary is on the page for good; the description update is the part
    that can fail (here: a foreign-owned video) and be finished later: without
    appending a second copy of the summary to get there."""
    from baton.adapters.fakes import FakePublisher

    _, docs = studio
    _video_on(docs)
    prepared(studio, capsys)

    fake_publisher = FakePublisher()
    fake_publisher.foreign_video_ids = {"dQw4w9WgXcQ"}
    monkeypatch.setattr("baton.cli.cmd_lesson.open_publisher", lambda _config: fake_publisher)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK
    payload = out(capsys)
    assert payload["youtube"]["status"] == "error"
    after_first = len(docs.list_blocks("doc-ada-03"))

    # The ownership problem is fixed (the video is ours now, say).
    fake_publisher.foreign_video_ids.clear()
    capsys.readouterr()

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    payload = out(capsys)
    assert payload["youtube"]["status"] == "ok"
    assert payload["youtube"]["video_id"] == "dQw4w9WgXcQ"
    # Nothing was appended a second time.
    assert len(docs.list_blocks("doc-ada-03")) == after_first


def test_the_recording_landing_later_gets_its_description(studio, capsys, monkeypatch):
    """The ordinary order for a filmed lesson: publish, then the video pipeline
    uploads and links the recording. The first publish found no video and owed
    nothing; the re-run writes the description once one exists."""
    from baton.adapters.fakes import FakePublisher

    _, docs = studio
    prepared(studio, capsys)

    fake_publisher = FakePublisher()
    monkeypatch.setattr("baton.cli.cmd_lesson.open_publisher", lambda _config: fake_publisher)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK
    assert out(capsys)["youtube"] is None
    after_first = len(docs.list_blocks("doc-ada-03"))

    # The recording lands on the document afterwards.
    _video_on(docs)
    capsys.readouterr()

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    payload = out(capsys)
    assert payload["youtube"]["status"] == "ok"
    assert payload["youtube"]["video_id"] == "dQw4w9WgXcQ"
    assert len(docs.list_blocks("doc-ada-03")) == after_first


def test_a_re_run_stops_retrying_once_attempts_run_out(studio, capsys, monkeypatch):
    """A permanently refused update must not be retried on every publish for
    the rest of the session's life."""
    from baton.adapters.fakes import FakePublisher

    _, docs = studio
    _video_on(docs)
    prepared(studio, capsys)

    fake_publisher = FakePublisher()
    fake_publisher.foreign_video_ids = {"dQw4w9WgXcQ"}
    monkeypatch.setattr("baton.cli.cmd_lesson.open_publisher", lambda _config: fake_publisher)

    call(studio, "publish", "Ada Whitfield")  # attempt 1
    capsys.readouterr()
    call(studio, "publish", "Ada Whitfield")  # attempt 2
    capsys.readouterr()
    call(studio, "publish", "Ada Whitfield")  # attempt 3
    third = out(capsys)
    assert third["youtube"]["status"] == "error"
    updates_attempted = fake_publisher.descriptions
    capsys.readouterr()

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    payload = out(capsys)
    assert payload["skipped"] == "already published"
    # The cap held: no fourth call was made.
    assert len(updates_attempted) == len(fake_publisher.descriptions)


def test_re_staging_does_not_reopen_the_description_gate(studio, capsys, monkeypatch):
    """The draft is wiped by every `stage`; the published record is what
    remembers the description was already written. Re-staging to correct a
    title must not re-update the video."""
    from baton.adapters.fakes import FakePublisher

    _, docs = studio
    _video_on(docs)
    prepared(studio, capsys)

    fake_publisher = FakePublisher()
    monkeypatch.setattr("baton.cli.cmd_lesson.open_publisher", lambda _config: fake_publisher)

    call(studio, "publish", "Ada Whitfield")
    capsys.readouterr()
    described = dict(fake_publisher.descriptions)

    stage_ada(studio, capsys)
    call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(SUMMARY))
    capsys.readouterr()

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    payload = out(capsys)
    assert payload["skipped"] == "already published"
    assert fake_publisher.descriptions == described


# -- one studio voice, met to the learner in front of it -------------------------


def _instructions(studio, capsys, name: str, session: str) -> list[str]:
    call(studio, "stage", name, "--session", session, "--context", "notes")
    capsys.readouterr()
    assert call(studio, "contract", name) == Exit.OK
    return out(capsys)["instructions"]


def test_the_contract_carries_the_wording_for_this_learners_tone(studio, capsys):
    """The learners table has carried a `tone` since the first migration and
    it reached the model as a bare word, so a six-year-old and an exam
    candidate were written about in one voice."""
    lines = _instructions(studio, capsys, "Clara Nguyen", "1")  # exam

    assert any("examination" in line for line in lines)


def test_a_different_tone_gets_different_wording(studio, capsys):
    lines = _instructions(studio, capsys, "Bruno Castell", "1")  # casual

    assert any("for enjoyment" in line for line in lines)
    assert not any("examination" in line for line in lines)


def test_a_learner_with_no_instrument_at_home_changes_the_goals(studio, capsys):
    """The schema has promised this since the first migration and nothing
    delivered it. A goal they cannot possibly do is the fastest way to teach a
    family that the goals are not meant seriously."""
    lines = _instructions(studio, capsys, "Bruno Castell", "1")  # has_instrument = 0

    assert any("no instrument at home" in line for line in lines)


def test_a_learner_who_practises_at_home_is_not_told_otherwise(studio, capsys):
    lines = _instructions(studio, capsys, "Clara Nguyen", "1")

    assert not any("no instrument at home" in line for line in lines)


def test_an_unrecognised_tone_says_nothing_rather_than_guessing(studio, capsys):
    """The column is free text. A studio that invented a word for it has not
    yet said what the word means, and a guess would be this tool inventing a
    teaching voice for someone else's studio."""
    connection = sqlite3.connect(studio[0] / "data" / "studio.db")
    connection.execute("UPDATE learners SET tone = 'jazz-ish' WHERE id = 3")
    connection.commit()
    connection.close()

    lines = _instructions(studio, capsys, "Clara Nguyen", "1")

    assert not any("examination" in line for line in lines)
    assert any("language of this profile" in line for line in lines)  # the rest survives


def test_notation_guidance_follows_the_learners_instrument(studio, capsys):
    """`instrument` is a column like `tone` is, and it reached the model just
    as bare. A drum part written as guitar tab is unreadable to the family it
    is for."""
    lines = _instructions(studio, capsys, "Bruno Castell", "1")  # drums

    assert any("drum tab" in line for line in lines)
    assert not any("chords and tab" in line for line in lines)


def test_a_guitarist_gets_the_chord_convention_instead(studio, capsys):
    lines = _instructions(studio, capsys, "Clara Nguyen", "1")  # piano: no entry

    assert not any("drum tab" in line for line in lines)

    guitar = _instructions(studio, capsys, "Ada Whitfield", "3")

    assert any("chords and tab" in line for line in guitar)


# -- what the next lesson is written against -------------------------------------


def test_the_previous_lesson_arrives_in_full_not_as_the_parents_message(studio, capsys):
    """Only the three-line message used to be kept, and it is a thin thing to
    judge a week's progress from: `progress` asks what changed since last
    time, and the answer has to be measured against what actually happened."""
    _publish_session_two(studio, capsys)

    assert call(studio, "stage", "Ada Whitfield", "--session", "3", "--context", "n") == Exit.OK
    capsys.readouterr()
    assert call(studio, "contract", "Ada Whitfield") == Exit.OK
    previous = out(capsys)["context"]["previous_session_summary"]

    # The detail of the lesson, which never reached the parent's message.
    assert "Thumb-and-finger pattern" in previous
    assert "Late change to C" in previous


def test_a_record_written_before_summaries_were_kept_still_works(studio, capsys):
    """Records published by an older Baton have only the message. A thinner
    context is better than none, and better than a crash."""
    from baton.core import jsonio

    _publish_session_two(studio, capsys)
    path = next((studio[0] / "state" / "published").glob("1-2.json"))
    record = jsonio.read_json(path, {})
    record.pop("summary")
    jsonio.write_json(path, record)

    assert call(studio, "stage", "Ada Whitfield", "--session", "3", "--context", "n") == Exit.OK
    capsys.readouterr()
    call(studio, "contract", "Ada Whitfield")

    assert "Blackbird bars 9 to 16" in out(capsys)["context"]["previous_session_summary"]


# -- progress is asked for once there is something to compare with ---------------

PROGRESS = [{"before": "Needed the count called out", "after": "Counts through unaided"}]


def test_a_first_summary_needs_no_progress_section(studio, capsys):
    """Nothing has been published for Ada, so `stage` finds no previous
    summary and the lesson is not asked to invent a comparison."""
    stage_ada(studio, capsys)

    assert call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(SUMMARY)) == Exit.OK


def _publish_session_two(studio, capsys) -> None:
    """Leave a published session 2 behind, so staging session 3 has a previous
    summary to carry in."""
    call(studio, "stage", "Ada Whitfield", "--session", "2", "--context", "notes")
    call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(SUMMARY))
    call(studio, "publish", "Ada Whitfield")
    capsys.readouterr()


def test_a_later_summary_is_refused_without_one(studio, capsys):
    """Once a previous session's message exists, `stage` carries it into the
    draft, and the contract instruction to judge what is new finally has a
    field to put the answer in."""
    _publish_session_two(studio, capsys)
    stage_ada(studio, capsys)

    code = call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(SUMMARY))

    assert code == Exit.CONTRACT
    payload = out(capsys)
    assert "/progress" in [v["path"] for v in payload["details"]["violations"]]


def test_the_same_summary_with_progress_is_accepted(studio, capsys):
    _publish_session_two(studio, capsys)
    stage_ada(studio, capsys)

    with_progress = {**SUMMARY, "progress": PROGRESS}

    assert call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(with_progress)) == (
        Exit.OK
    )


def test_staging_without_the_previous_summary_drops_the_requirement(studio, capsys):
    """`--no-previous` says there is nothing to compare against, and the rule
    follows that rather than the session number."""
    _publish_session_two(studio, capsys)
    assert (
        call(studio, "stage", "Ada Whitfield", "--session", "3", "--no-previous", "--context", "n")
        == Exit.OK
    )
    capsys.readouterr()

    assert call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(SUMMARY)) == Exit.OK


def test_the_contract_hands_the_model_the_body_rules(studio, capsys):
    """A rule the model is judged against but never shown is a trap. The
    contract carries the phrase lists and the repeat limit beside the schema."""
    stage_ada(studio, capsys)

    assert call(studio, "contract", "Ada Whitfield") == Exit.OK
    payload = out(capsys)

    assert payload["constraints"]["body"]["max_repeats"] == 2
    assert "progress" in payload["schema"]["properties"]
    assert any("progress" in line for line in payload["instructions"])


def test_the_rendered_summary_shows_progress_as_a_change(studio, capsys):
    stage_ada(studio, capsys)
    call(
        studio,
        "ingest",
        "Ada Whitfield",
        "--json-text",
        json.dumps({**SUMMARY, "progress": PROGRESS}),
    )
    capsys.readouterr()

    assert call(studio, "render", "Ada Whitfield") == Exit.OK

    markdown = out(capsys)["markdown"]
    assert "Needed the count called out → Counts through unaided" in markdown


# -- the song being learnt is not the lesson's recording -------------------------

SONG = "https://www.youtube.com/watch?v=aSongVideo1"
RECORDING = "https://youtu.be/aLessonRec1"


def _teach(profile, source_link: str, *, piece_id: int = 2) -> None:
    """Give the piece a source link on a video host, as a pop song has."""
    connection = sqlite3.connect(profile / "data" / "studio.db")
    connection.execute("UPDATE pieces SET source_link = ? WHERE id = ?", (source_link, piece_id))
    connection.commit()
    connection.close()


def test_publish_does_not_write_the_summary_onto_the_songs_own_video(studio, capsys, monkeypatch):
    """What production did: the page held the song and no recording, so the
    description step took the song's video for the lesson's and YouTube
    refused it: `Video <id> belongs to <a record label's channel>, not to the
    configured account.` There is nothing to describe here, and saying so is
    the whole answer."""
    from baton.adapters.fakes import FakePublisher

    profile, docs = studio
    _teach(profile, SONG)
    docs.blocks["doc-ada-03"] = [
        Block(id="head", type="heading_2", text="🎵 Die With a Smile"),
        Block(id="bm", type="bookmark", url=SONG),
        Block(id="em", type="embed", url=SONG),
    ]
    fake_publisher = FakePublisher()
    monkeypatch.setattr("baton.cli.cmd_lesson.open_publisher", lambda _config: fake_publisher)
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    assert out(capsys)["youtube"] is None
    assert fake_publisher.descriptions == {}


def test_publish_describes_the_recording_that_sits_beside_the_song(studio, capsys, monkeypatch):
    from baton.adapters.fakes import FakePublisher

    profile, docs = studio
    _teach(profile, SONG)
    docs.blocks["doc-ada-03"] = [
        Block(id="recording", type="video", url=RECORDING),
        Block(id="em", type="embed", url=SONG),
    ]
    fake_publisher = FakePublisher()
    monkeypatch.setattr("baton.cli.cmd_lesson.open_publisher", lambda _config: fake_publisher)
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    assert out(capsys)["youtube"] == {"status": "ok", "video_id": "aLessonRec1"}
    assert "aSongVideo1" not in fake_publisher.descriptions


# -- publish links an upload the pipeline never got onto the page ----------------


def _uploaded(profile, url: str = RECORDING, **overrides) -> None:
    """Record a video job that reached `uploaded` and stopped there: the
    state a run leaves behind when it dies after YouTube accepted the file."""
    from baton.pipelines.video import VideoJob, VideoJobStore

    job = VideoJob(
        learner_folder=overrides.pop("learner_folder", "Ada Whitfield"),
        learner_id=overrides.pop("learner_id", "1"),
        learner_name=overrides.pop("learner_name", "Ada Whitfield"),
        session_number=overrides.pop("session_number", 3),
        doc_id="doc-ada-03",
        video_id="aLessonRec1",
        video_url=url,
        **overrides,
    )
    job.record("uploaded", video_id=job.video_id, url=url)
    VideoJobStore(profile / "state" / "video").save(job)


def test_publish_links_an_upload_the_pipeline_left_off_the_page(studio, capsys):
    """The recording was on YouTube from twenty to nine; the page had no link
    to it, so `send` refused with "add a video block to the document" and a
    person had to do exactly that by hand. Publish is where they already are.
    """
    profile, docs = studio
    _uploaded(profile)
    docs.blocks["doc-ada-03"] = []
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    payload = out(capsys)
    assert payload["recording"] == {"status": "linked", "url": RECORDING}
    assert RECORDING in {block.url for block in docs.list_blocks("doc-ada-03")}


def test_a_linked_upload_then_gets_its_youtube_description(studio, capsys, monkeypatch):
    """Linking it is what makes the description step have something to do:
    the two run in that order for that reason."""
    from baton.adapters.fakes import FakePublisher

    profile, docs = studio
    _uploaded(profile)
    docs.blocks["doc-ada-03"] = []
    fake_publisher = FakePublisher()
    monkeypatch.setattr("baton.cli.cmd_lesson.open_publisher", lambda _config: fake_publisher)
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    assert out(capsys)["youtube"] == {"status": "ok", "video_id": "aLessonRec1"}


def test_a_forced_republish_links_the_upload_too(studio, capsys):
    """`publish --force` is what the studio actually reached for, and it
    reported `preserved: 0, youtube: null`, nothing kept, nothing linked."""
    profile, docs = studio
    docs.blocks["doc-ada-03"] = []
    prepared(studio, capsys)
    call(studio, "publish", "Ada Whitfield")
    capsys.readouterr()

    _uploaded(profile)  # the upload lands afterwards, unlinked

    assert call(studio, "publish", "Ada Whitfield", "--force") == Exit.OK

    payload = out(capsys)
    assert payload["recording"] == {"status": "linked", "url": RECORDING}
    assert RECORDING in {block.url for block in docs.list_blocks("doc-ada-03")}


def test_a_re_run_of_an_already_published_lesson_links_the_upload(studio, capsys):
    """Without --force, the summary is left alone, but the recording is one
    of the writes a re-run legitimately still owes."""
    profile, docs = studio
    docs.blocks["doc-ada-03"] = []
    prepared(studio, capsys)
    call(studio, "publish", "Ada Whitfield")
    capsys.readouterr()
    after_first = len(docs.list_blocks("doc-ada-03"))

    _uploaded(profile)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    payload = out(capsys)
    assert payload["recording"] == {"status": "linked", "url": RECORDING}
    assert len(docs.list_blocks("doc-ada-03")) == after_first + 1


def test_publish_does_not_link_a_second_copy_of_a_recording_already_shown(studio, capsys):
    """The ordinary case: the pipeline linked it itself. Nothing to repair,
    and repairing it anyway would put the video on the page twice."""
    profile, docs = studio
    _uploaded(profile)
    docs.blocks["doc-ada-03"] = [Block(id="vid", type="video", url=RECORDING)]
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    payload = out(capsys)
    assert payload["recording"] is None
    videos = [block.url for block in docs.list_blocks("doc-ada-03") if block.type == "video"]
    assert videos == [RECORDING]


def test_publish_leaves_a_hand_pasted_recording_alone(studio, capsys):
    """A recording someone put on the page by hand is a recording as far as
    the gate is concerned, so there is nothing missing to repair, and adding
    the pipeline's copy beside it would give the page two.

    The fixture's `docs.preserve` keeps `embed`, so this one survives the
    republish; a shape the policy does not protect is deleted by publish
    itself, and then there genuinely is nothing on the page.
    """
    profile, docs = studio
    _uploaded(profile, url="https://youtu.be/otherupload")
    docs.blocks["doc-ada-03"] = [Block(id="em", type="embed", url=RECORDING)]
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    assert out(capsys)["recording"] is None
    assert not [block for block in docs.list_blocks("doc-ada-03") if block.type == "video"]


def test_publish_links_an_upload_whose_job_has_been_archived(studio, capsys):
    """A folder holds one live job, so the week the next lesson is collected
    the finished one is moved aside. The page it never linked is still worth
    repairing, and the days between the upload and someone noticing are
    exactly when the next lesson gets filmed.
    """
    from baton.pipelines.video import VideoJobStore

    profile, docs = studio
    _uploaded(profile)
    store = VideoJobStore(profile / "state" / "video")
    store.archive(store.get("Ada Whitfield"))
    assert store.list() == [], "precondition: the live listing no longer shows it"
    docs.blocks["doc-ada-03"] = []
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    payload = out(capsys)
    assert payload["recording"] == {"status": "linked", "url": RECORDING}
    assert RECORDING in {block.url for block in docs.list_blocks("doc-ada-03")}


def test_publish_prefers_the_most_recent_record_of_one_session(studio, capsys):
    """Archiving keeps the old record beside the new one. A week re-run after
    a bad upload has two, and the one that stands is the later."""
    from baton.pipelines.video import VideoJobStore

    profile, docs = studio
    _uploaded(profile, url="https://youtu.be/supersededXX")
    store = VideoJobStore(profile / "state" / "video")
    store.archive(store.get("Ada Whitfield"))
    _uploaded(profile)  # the re-run, live and newer
    docs.blocks["doc-ada-03"] = []
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    assert out(capsys)["recording"] == {"status": "linked", "url": RECORDING}


def test_publish_does_not_link_another_sessions_upload(studio, capsys):
    """Job records outlive the session they were for. Linking week 2's
    recording under week 3 is worse than linking nothing."""
    profile, docs = studio
    _uploaded(profile, session_number=2)
    docs.blocks["doc-ada-03"] = []
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    assert out(capsys)["recording"] is None
    assert docs.list_blocks("doc-ada-03") != []  # the summary landed; no video did
    assert not [block for block in docs.list_blocks("doc-ada-03") if block.type == "video"]


def test_publish_does_not_link_another_learners_upload(studio, capsys):
    profile, docs = studio
    _uploaded(profile, learner_folder="Bruno Castell", learner_id="2", learner_name="Bruno Castell")
    docs.blocks["doc-ada-03"] = []
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    assert out(capsys)["recording"] is None


def test_publish_ignores_a_job_that_never_uploaded(studio, capsys):
    """A job that only downloaded has nothing to link, and its `video_url` is
    empty: reading it as one would put a blank video block on the page."""
    from baton.pipelines.video import VideoJob, VideoJobStore

    profile, docs = studio
    job = VideoJob(
        learner_folder="Ada Whitfield",
        learner_id="1",
        learner_name="Ada Whitfield",
        session_number=3,
    )
    job.record("downloaded", count=3)
    VideoJobStore(profile / "state" / "video").save(job)
    docs.blocks["doc-ada-03"] = []
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    assert out(capsys)["recording"] is None


def test_a_dry_run_links_nothing(studio, capsys):
    profile, docs = studio
    _uploaded(profile)
    docs.blocks["doc-ada-03"] = []
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield", "--dry-run") == Exit.OK

    assert docs.list_blocks("doc-ada-03") == []


def test_a_document_store_that_refuses_the_link_does_not_fail_the_publish(
    studio, capsys, monkeypatch
):
    """The summary is on the page by the time this runs. The remedy the send
    gate prints still works, and a non-zero exit here would say the publish
    itself had failed."""
    from baton.errors import UpstreamError

    profile, docs = studio
    _uploaded(profile)
    docs.blocks["doc-ada-03"] = []
    prepared(studio, capsys)

    class RefusingVideoBlocks:
        """The fixture's store, except that appending a video block fails."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def append_blocks(self, doc_id, blocks):
            if any(block.get("type") == "video" for block in blocks):
                raise UpstreamError("notion refused the block", service="notion")
            return self._inner.append_blocks(doc_id, blocks)

    refusing = RefusingVideoBlocks(docs)
    monkeypatch.setattr("baton.cli.cmd_lesson.open_docs", lambda _config: refusing)

    assert call(studio, "publish", "Ada Whitfield") == Exit.OK

    payload = out(capsys)
    assert payload["recording"]["status"] == "error"
    assert "notion refused the block" in payload["recording"]["error"]
    assert payload["appended"] > 0  # the summary itself still landed


# -- naming the learner ----------------------------------------------------------


def test_the_learner_can_be_named_by_flag_instead_of_positionally(studio, capsys):
    """`baton lesson publish --learner X --session N` was a usage error in
    production, and the agent that typed it had every reason to: `send batch`
    takes `--learner`. Both spellings work now; the positional stays the
    documented one."""
    prepared(studio, capsys)

    assert call(studio, "publish", "--learner", "Ada Whitfield", "--session", "3") == Exit.OK
    assert out(capsys)["appended"] > 0


def test_the_flag_works_for_every_subcommand_that_names_a_learner(studio, capsys):
    """Knowing it works on one and not the next is worse than it never
    working."""
    assert call(studio, "stage", "--learner", "Ada Whitfield", "--session", "3") == Exit.OK
    capsys.readouterr()

    assert call(studio, "contract", "--learner", "Ada Whitfield") == Exit.OK
    capsys.readouterr()
    ingested = call(
        studio, "ingest", "--learner", "Ada Whitfield", "--json-text", json.dumps(SUMMARY)
    )
    assert ingested == Exit.OK
    capsys.readouterr()
    assert call(studio, "render", "--learner", "Ada Whitfield") == Exit.OK
    capsys.readouterr()
    assert call(studio, "show", "--learner", "Ada Whitfield") == Exit.OK
    capsys.readouterr()
    assert call(studio, "remove", "--learner", "Ada Whitfield") == Exit.OK


def test_the_positional_and_the_flag_may_agree(studio, capsys):
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield", "--learner", "Ada Whitfield") == Exit.OK


def test_two_different_learners_in_one_invocation_are_refused(studio, capsys):
    """Silently letting one win publishes somebody's lesson under somebody
    else's name."""
    prepared(studio, capsys)

    assert call(studio, "publish", "Ada Whitfield", "--learner", "Bruno Castell") == Exit.USAGE

    payload = out(capsys)
    assert "Ada Whitfield" in payload["message"]
    assert "Bruno Castell" in payload["message"]


def test_naming_nobody_at_all_says_which_command_wanted_a_name(studio, capsys):
    assert call(studio, "publish") == Exit.USAGE

    payload = out(capsys)
    assert "publish" in payload["message"]
    assert "publish" in payload["remedy"]


# -- publish --session asserts which lesson is being published --------------------


def test_publish_refuses_a_session_the_draft_is_not_for(studio, capsys):
    """A learner has one draft at a time, so `--session` cannot pick between
    them, but it can stop a publish that thought it was finishing a different
    lesson."""
    prepared(studio, capsys)  # staged for session 3

    assert call(studio, "publish", "Ada Whitfield", "--session", "2") == Exit.USAGE

    payload = out(capsys)
    assert "lesson 3" in payload["message"]
    assert "lesson 2" in payload["message"]


def test_publish_refusing_a_session_writes_nothing(studio, capsys):
    prepared(studio, capsys)
    _, docs = studio
    before = list(docs.list_blocks("doc-ada-03"))

    assert call(studio, "publish", "Ada Whitfield", "--session", "2") == Exit.USAGE

    assert docs.list_blocks("doc-ada-03") == before


# -- corrected notes and the vocabulary pool ---------------------------------


def test_stage_keeps_the_corrected_notes_beside_the_raw_ones(studio, capsys):
    assert (
        call(
            studio,
            "stage",
            "Ada Whitfield",
            "--session",
            "3",
            "--context",
            "Encor was hard",
            "--corrected",
            "Encore was hard",
        )
        == Exit.OK
    )
    capsys.readouterr()

    call(studio, "show", "Ada Whitfield")
    draft = out(capsys)
    assert draft["context"] == "Encor was hard"
    assert draft["corrected_context"] == "Encore was hard"


def test_stage_refuses_two_corrected_sources(studio, capsys):
    assert (
        call(
            studio,
            "stage",
            "Ada Whitfield",
            "--session",
            "3",
            "--corrected",
            "a",
            "--corrected-file",
            "b.txt",
        )
        == Exit.USAGE
    )


def _with_vocabulary(studio, terms):
    """Give the profile a vocabulary pool on top of its own baton.yaml."""
    profile, _ = studio
    listed = "\n".join(f"    - {term}" for term in terms)
    config = (profile / "baton.yaml").read_text(encoding="utf-8")
    (profile / "baton.yaml").write_text(
        config + f"summary:\n  vocabulary:\n{listed}\n", encoding="utf-8"
    )


def test_contract_serves_the_corrected_notes_and_the_pool(studio, capsys):
    _with_vocabulary(studio, ["Encore"])
    stage_ada(studio, capsys)
    assert (
        call(
            studio,
            "stage",
            "Ada Whitfield",
            "--session",
            "3",
            "--context",
            "Encor was hard",
            "--corrected",
            "Encore was hard",
        )
        == Exit.OK
    )
    capsys.readouterr()

    assert call(studio, "contract", "Ada Whitfield") == Exit.OK
    payload = out(capsys)
    assert payload["context"]["lesson_notes"] == "Encore was hard"
    assert payload["constraints"]["vocabulary"] == ["Encore"]
    assert any("constraints.vocabulary" in line for line in payload["instructions"])


def test_contract_serves_the_raw_notes_when_nothing_was_corrected(studio, capsys):
    stage_ada(studio, capsys)

    call(studio, "contract", "Ada Whitfield")
    payload = out(capsys)
    assert payload["context"]["lesson_notes"] == "notes"
    assert payload["constraints"]["vocabulary"] == []


def test_ingest_warns_about_a_near_miss_without_refusing_it(studio, capsys):
    """Layer three, and the reason it warns: a summary rejected over a spelling
    is a summary that stops being produced."""
    _with_vocabulary(studio, ["Encore"])
    stage_ada(studio, capsys)
    near = {**SUMMARY, "overview": ["Held the tempo through the whole Encour."]}

    assert call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(near)) == Exit.OK
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["warnings"] == [
        "the summary spells it `encour` at /overview/0 where the vocabulary pool spells it `Encore`"
    ]
    assert "!" in captured.err


def test_ingest_stays_silent_when_the_spelling_matches_the_pool(studio, capsys):
    _with_vocabulary(studio, ["the tempo"])
    stage_ada(studio, capsys)

    assert call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(SUMMARY)) == Exit.OK
    result = out(capsys)
    assert result["warnings"] == []
    assert "!" not in capsys.readouterr().err


# -- listing and clearing drafts ---------------------------------------------


def _record_target(studio, learner_id, name, status, **extra):
    """Stamp a publish outcome onto the draft the way `publish` would."""
    profile, _ = studio
    store = StagingStore(profile / "state" / "lessons")
    draft = store.get(learner_id)
    assert draft is not None
    draft.record_target(name, status, **extra)
    store.save(draft)


def test_list_carries_the_full_target_state(studio, capsys):
    """A heartbeat asking "is this stuck?" needs the error, the time, and how
    many tries were spent, not the one-word status stage returns."""
    learner_id = stage_ada(studio, capsys)["learner_id"]
    _record_target(studio, learner_id, "youtube", "error", error="quota exceeded")

    assert call(studio, "list") == Exit.OK
    targets = out(capsys)["lessons"][0]["targets"]
    assert targets["youtube"]["status"] == "error"
    assert targets["youtube"]["error"] == "quota exceeded"
    assert targets["youtube"]["attempts"] == 1
    assert targets["youtube"]["at"]


def test_list_uses_the_published_record_where_the_draft_is_blind(studio, capsys):
    """`stage` overwrites the draft wholesale; the published record is what
    proves the summary already went out. The listing must show the target done
    even though this fresh draft says nothing about it."""
    learner_id = stage_ada(studio, capsys)["learner_id"]
    profile, _ = studio
    PublishedRecord(profile / "state" / "published").save(
        LessonDraft(learner_id=learner_id, learner_name="Ada Whitfield", session_number=3),
        short_message="went out",
    )

    assert call(studio, "list") == Exit.OK
    targets = out(capsys)["lessons"][0]["targets"]
    assert targets["docs"] == {"status": "ok", "source": "published_record"}


def test_clear_keeps_a_draft_whose_publish_left_work_owed(studio, capsys):
    learner_id = stage_ada(studio, capsys)["learner_id"]
    _record_target(studio, learner_id, "docs", "error", error="notion 502")

    assert call(studio, "clear", "--yes") == Exit.OK
    payload = out(capsys)
    assert payload["removed"] == 0
    assert payload["kept"] == [{"learner_name": "Ada Whitfield", "targets": {"docs": "error"}}]

    call(studio, "list")
    assert out(capsys)["count"] == 1


def test_clear_force_discards_even_the_unfinished_ones(studio, capsys):
    learner_id = stage_ada(studio, capsys)["learner_id"]
    _record_target(studio, learner_id, "docs", "error", error="notion 502")

    assert call(studio, "clear", "--yes", "--force") == Exit.OK
    payload = out(capsys)
    assert payload["removed"] == 1
    assert payload["kept"] == []


def test_teacher_followup_never_reaches_the_page_or_the_message(studio, capsys):
    stage_ada(studio, capsys)
    summary = {**SUMMARY, "teacher_followup": ["Listen for the bar 12 rush"]}
    assert call(studio, "ingest", "Ada Whitfield", "--json-text", json.dumps(summary)) == Exit.OK
    capsys.readouterr()

    call(studio, "render", "Ada Whitfield")
    assert "bar 12 rush" not in out(capsys)["markdown"]

    call(studio, "render", "Ada Whitfield", "--format", "message")
    assert "bar 12 rush" not in out(capsys)["message"]

    from baton.render.summary import to_blocks

    assert "bar 12 rush" not in json.dumps(to_blocks(summary), ensure_ascii=False)


def test_the_contract_asks_for_the_teacher_followup(studio, capsys):
    stage_ada(studio, capsys)
    capsys.readouterr()

    call(studio, "contract", "Ada Whitfield")
    joined = " ".join(out(capsys)["instructions"])
    assert "teacher_followup" in joined
