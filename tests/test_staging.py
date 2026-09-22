"""Published records, separated by the identity inside them.

The filename is an index built from a learner id that may contain `-`, so a
glob for one learner can sweep in another's records. These tests pin the
separation: what `latest` returns must belong to the learner it was asked
about, by the id stored in the record, not by the filename's prefix.
"""

from __future__ import annotations

from pathlib import Path

import baton.pipelines.staging as staging
from baton.pipelines.staging import LessonDraft, PublishedRecord


class _Clock:
    """`_now()` has second precision; two saves in one second would tie."""

    def __init__(self) -> None:
        self.tick = 0

    def __call__(self) -> str:
        self.tick += 1
        return f"2026-08-17T00:00:{self.tick:02d}+00:00"


def _draft(learner_id: str, name: str, session: int) -> LessonDraft:
    return LessonDraft(
        learner_id=learner_id,
        learner_name=name,
        session_number=session,
        doc_id=f"doc-{learner_id}-{session}",
        titles="Blackbird",
    )


def test_latest_never_returns_another_learners_record(tmp_path: Path):
    """`ada` and `ada-1`: the shorter id is a prefix of the longer one, and a
    bare glob for `ada-*` picks up `ada-1`'s files. The message composed for
    one learner must never be handed to the other's family."""
    records = PublishedRecord(tmp_path)
    records.save(_draft("ada-1", "Ada the Second", 4), short_message="for ada-1")
    records.save(_draft("ada", "Ada Whitfield", 1), short_message="for ada")

    latest = records.latest("ada")

    assert latest is not None
    assert latest["learner_id"] == "ada"
    assert latest["short_message"] == "for ada"


def test_latest_picks_the_most_recent_among_only_that_learners_records(tmp_path: Path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(staging, "_now", clock)
    records = PublishedRecord(tmp_path)
    records.save(_draft("ada", "Ada Whitfield", 1), short_message="older")
    records.save(_draft("ada-1", "Ada the Second", 2), short_message="the other ada")
    # Same learner, same session, republished: the newer write wins.
    records.save(_draft("ada", "Ada Whitfield", 1), short_message="newer")

    latest = records.latest("ada")

    assert latest is not None
    assert latest["short_message"] == "newer"


def test_two_publishes_in_one_second_are_ordered_by_session(tmp_path: Path, monkeypatch):
    """The tie the `_Clock` above exists to avoid, met head-on.

    `published_at` is written to the second, so a studio that publishes two
    sessions in the same second leaves two records that compare equal. `max`
    then returned whichever the filesystem's glob yielded first (Linux and
    macOS disagreed, which is how this surfaced), and `send lesson` with no
    `--session` could pick the earlier one and send last week's message.
    """
    monkeypatch.setattr(staging, "_now", lambda: "2026-08-17T00:00:01+00:00")
    records = PublishedRecord(tmp_path)
    records.save(_draft("ada", "Ada Whitfield", 3), short_message="last week")
    records.save(_draft("ada", "Ada Whitfield", 4), short_message="this week")

    latest = records.latest("ada")

    assert latest is not None
    assert latest["short_message"] == "this week"
    assert latest["session_number"] == 4


def test_a_learner_with_no_records_gets_nothing_even_when_a_neighbour_has(tmp_path: Path):
    records = PublishedRecord(tmp_path)
    records.save(_draft("ada-1", "Ada the Second", 3), short_message="for ada-1")

    assert records.latest("ada") is None
    assert records.latest("ada-1") is not None


# -- clearing drafts ---------------------------------------------------------


def test_a_successful_retry_retires_the_previous_error(tmp_path: Path):
    draft = _draft("ada", "Ada Whitfield", 3)
    draft.record_target("youtube", "error", error="HttpError 400: invalidDescription")

    draft.record_target("youtube", "ok", video_id="rPrspj5oNIk")

    assert draft.targets["youtube"] == {
        "status": "ok",
        "attempts": 2,
        "video_id": "rPrspj5oNIk",
        "at": draft.targets["youtube"]["at"],
    }


def _store(tmp_path: Path) -> staging.StagingStore:
    return staging.StagingStore(tmp_path / "lessons")


def test_clear_keeps_a_draft_whose_publish_left_work_owed(tmp_path: Path):
    """A publish that stopped partway leaves the draft as the only record that
    the work is still owed: the next `stage` overwrites the file wholesale,
    and the published record only exists once the summary went out."""
    store = _store(tmp_path)
    draft = _draft("ada", "Ada Whitfield", 3)
    draft.record_target("docs", "error", error="notion 502")
    store.save(draft)

    removed, kept = store.clear(keep_unfinished=True)

    assert removed == []
    assert kept == [{"learner_name": "Ada Whitfield", "targets": {"docs": "error"}}]
    assert store.get("ada") is not None


def test_clear_still_sweeps_a_fully_published_draft(tmp_path: Path):
    """Every target done means nothing is owed, so the selective sweep has no
    reason to keep the draft around."""
    store = _store(tmp_path)
    draft = _draft("ada", "Ada Whitfield", 3)
    draft.record_target("docs", "ok")
    draft.record_target("youtube", "ok")
    store.save(draft)

    removed, kept = store.clear(keep_unfinished=True)

    assert removed == ["Ada Whitfield"]
    assert kept == []
    assert store.get("ada") is None


def test_a_draft_never_published_clears_either_way(tmp_path: Path):
    """No targets means no publish ever started and nothing is owed."""
    store = _store(tmp_path)
    store.save(_draft("ada", "Ada Whitfield", 3))

    removed, kept = store.clear(keep_unfinished=True)

    assert (removed, kept) == (["Ada Whitfield"], [])


def test_clear_without_the_flag_removes_everything(tmp_path: Path):
    store = _store(tmp_path)
    draft = _draft("ada", "Ada Whitfield", 3)
    draft.record_target("docs", "error", error="notion 502")
    store.save(draft)
    store.save(_draft("bruno", "Bruno Castell", 1))

    removed, kept = store.clear()

    assert removed == ["Ada Whitfield", "Bruno Castell"]
    assert kept == []
