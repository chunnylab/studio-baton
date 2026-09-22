"""`baton course` end to end: real SQLite, a stubbed document store.

Two arrangements are exercised, because both exist in real studios and the
command has to serve them without anyone reorganising their documents: a
learner with a folder page for finished courses, and a learner who keeps them
beside the live one.
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

import baton
from baton.adapters.docs.base import Block, DocChild, DocPage, DocStatus, TableRow
from baton.adapters.fakes import FakeDocStore
from baton.cli.app import run
from baton.domain.archive import SpanFormat, archive_title, strip_span
from baton.errors import UpstreamError
from baton.exits import Exit

MIGRATIONS = Path(baton.__file__).resolve().parent / "migrations"

COURSE_PAGE = "page-course"
TABLE = "table-course"
MENU = "block-menu"
FOLDER = "page-archives"

ROWS = [
    TableRow(row_id="doc-ada-01", title="1", date="2026-05-16", status="Complete"),
    TableRow(row_id="doc-ada-02", title="2", date="2026-06-01", status="Complete"),
    TableRow(row_id="doc-ada-03", title="3", date="2026-08-07", status="Complete"),
]


def _docs(*, with_folder: bool, course_title: str = "Course 12") -> FakeDocStore:
    """A course page inside a menu block, optionally beside a folder page."""
    menu_children = [DocChild(child_id=COURSE_PAGE, kind="page", title=course_title)]
    if with_folder:
        menu_children.append(DocChild(child_id=FOLDER, kind="page", title="Archives"))

    return FakeDocStore(
        statuses={row.row_id: DocStatus(doc_id=row.row_id, status=row.status) for row in ROWS},
        blocks={"doc-ada-01": [Block(id="b1", type="paragraph", text="summary")]},
        pages={
            # Each session document is a row of the course table.
            **{
                row.row_id: DocPage(
                    doc_id=row.row_id, title=row.title, parent_id=TABLE, parent_kind="database_id"
                )
                for row in ROWS
            },
            TABLE: DocPage(
                doc_id=TABLE, title="Ada Course 12", parent_id=COURSE_PAGE, parent_kind="page_id"
            ),
            COURSE_PAGE: DocPage(
                doc_id=COURSE_PAGE, title=course_title, parent_id=MENU, parent_kind="block_id"
            ),
        },
        children={MENU: menu_children, FOLDER: []},
        tables={TABLE: list(ROWS)},
    )


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
            courses:
              archive:
                span:
                  era: buddhist
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    holder: dict[str, FakeDocStore] = {}

    def use(fake: FakeDocStore) -> FakeDocStore:
        holder["docs"] = fake
        monkeypatch.setattr("baton.cli.cmd_course.open_docs", lambda _config: fake)
        return fake

    return profile, use


def call(studio, *args):
    profile, _ = studio
    return run(["--profile", str(profile), "--json", "course", *args])


# -- naming ------------------------------------------------------------------


def test_the_year_appears_once_when_both_ends_share_it():
    span = SpanFormat(era="buddhist")
    assert span.span(date(2026, 5, 16), date(2026, 8, 7)) == "16/05 - 07/08/69"


def test_the_year_appears_on_both_ends_when_they_differ():
    span = SpanFormat(era="buddhist")
    assert span.span(date(2025, 10, 24), date(2026, 1, 30)) == "24/10/68 - 30/01/69"


def test_a_span_already_in_the_title_is_replaced_not_repeated():
    span = SpanFormat(era="buddhist")
    title = archive_title(
        "Course 8 (29/04 - 31/05/69)", date(2026, 4, 29), date(2026, 5, 31), span_format=span
    )
    assert title == "Course 8 (29/04 - 31/05/69)"


def test_brackets_that_are_not_spans_survive():
    span = SpanFormat(era="buddhist")
    assert strip_span("Course 12 (Drum)", span) == "Course 12 (Drum)"


def test_a_label_sits_between_the_course_and_its_span():
    span = SpanFormat(era="buddhist")
    title = archive_title(
        "Course 12", date(2025, 3, 14), date(2025, 7, 11), span_format=span, label="Worth It"
    )
    assert title == "Course 12 (Worth It) (14/03 - 11/07/68)"


# -- plan --------------------------------------------------------------------


def test_plan_files_into_the_folder_when_one_exists(studio, capsys):
    _, use = studio
    use(_docs(with_folder=True))

    assert call(studio, "plan", "Ada Whitfield") == Exit.OK
    payload = json.loads(capsys.readouterr().out)

    assert payload["archive"]["title"] == "Course 12 (16/05 - 07/08/69)"
    assert payload["archive"]["destination_id"] == FOLDER
    assert payload["archive"]["needs_move"] is True
    assert payload["rows"] == 3


def test_plan_leaves_the_copy_in_place_when_there_is_no_folder(studio, capsys):
    _, use = studio
    use(_docs(with_folder=False))

    assert call(studio, "plan", "Ada Whitfield") == Exit.OK
    payload = json.loads(capsys.readouterr().out)

    assert payload["archive"]["destination_id"] == MENU
    assert payload["archive"]["needs_move"] is False


def test_plan_refuses_when_the_course_is_already_filed(studio, capsys):
    _, use = studio
    fake = _docs(with_folder=True)
    fake.children[FOLDER] = [
        DocChild(child_id="page-old", kind="page", title="Course 12 (16/05 - 07/08/69)")
    ]
    use(fake)

    assert call(studio, "plan", "Ada Whitfield") == Exit.GATE


def test_a_live_page_named_like_the_archive_is_not_mistaken_for_one(studio, capsys):
    """A studio that renames its live page for the span it is teaching.

    The course page then carries the exact name the copy will take. Counting it
    as an existing archive would make this learner permanently unarchivable.
    """
    _, use = studio
    fake = _docs(with_folder=False, course_title="Course 12 (16/05 - 07/08/69)")
    use(fake)

    assert call(studio, "plan", "Ada Whitfield") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["renames_live_page"] is True


# -- verify ------------------------------------------------------------------


def _filed_copy(fake: FakeDocStore, *, parent: str, title: str, rows: list[TableRow]) -> str:
    copy_id, copy_table = "page-copy", "table-copy"
    fake.pages[copy_id] = DocPage(
        doc_id=copy_id, title=title, parent_id=parent, parent_kind="page_id"
    )
    fake.children[copy_id] = [DocChild(child_id=copy_table, kind="table", title="Ada Course 12")]
    fake.tables[copy_table] = list(rows)
    fake.children.setdefault(parent, []).append(
        DocChild(child_id=copy_id, kind="page", title=title)
    )
    return copy_id


def _receipts(profile: Path) -> list[Path]:
    return list((profile / "state" / "courses").glob("*.json"))


def test_verify_passes_on_a_complete_copy(studio, capsys):
    _, use = studio
    fake = use(_docs(with_folder=True))
    copy_id = _filed_copy(fake, parent=FOLDER, title="Course 12 (16/05 - 07/08/69)", rows=ROWS)

    assert call(studio, "verify", "Ada Whitfield", "--page", copy_id) == Exit.OK
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_verify_writes_a_receipt_for_the_exact_course(studio, capsys):
    profile, use = studio
    fake = use(_docs(with_folder=True))
    copy_id = _filed_copy(fake, parent=FOLDER, title="Course 12 (16/05 - 07/08/69)", rows=ROWS)

    assert call(studio, "verify", "Ada Whitfield", "--page", copy_id) == Exit.OK
    capsys.readouterr()

    files = _receipts(profile)
    assert len(files) == 1
    receipt = json.loads(files[0].read_text(encoding="utf-8"))
    assert receipt["schema"] == 1
    assert receipt["learner"] == {"id": "1", "name": "Ada Whitfield"}
    assert receipt["course"] == {"page_id": COURSE_PAGE, "table_id": TABLE}
    assert receipt["archive"]["page_id"] == copy_id
    assert receipt["archive"]["title"] == "Course 12 (16/05 - 07/08/69)"
    assert receipt["rows"] == sorted([[row.title, row.date, row.status] for row in ROWS])
    assert receipt["sessions"] == [
        {"number": 1, "doc_id": "doc-ada-01"},
        {"number": 2, "doc_id": "doc-ada-02"},
        {"number": 3, "doc_id": "doc-ada-03"},
    ]


def test_verify_fails_when_rows_are_missing(studio, capsys):
    _, use = studio
    fake = use(_docs(with_folder=True))
    copy_id = _filed_copy(fake, parent=FOLDER, title="Course 12 (16/05 - 07/08/69)", rows=ROWS[:2])

    assert call(studio, "verify", "Ada Whitfield", "--page", copy_id) == Exit.GATE
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "2 rows" in " ".join(payload["problems"])


def test_verify_refuses_the_live_course_page(studio, capsys):
    """The check that stops a clear from destroying the course it should keep."""
    _, use = studio
    use(_docs(with_folder=False, course_title="Course 12 (16/05 - 07/08/69)"))

    assert call(studio, "verify", "Ada Whitfield", "--page", COURSE_PAGE) == Exit.GATE


# -- clear -------------------------------------------------------------------


def test_clear_dry_run_touches_nothing(studio, capsys):
    """`--dry-run` is a listing, so it stands outside the archive rule."""

    _, use = studio
    fake = use(_docs(with_folder=True))

    assert call(studio, "clear", "Ada Whitfield", "--dry-run") == Exit.OK
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert fake.reset_calls == []


def test_clear_empties_every_session_and_keeps_the_rows(studio, capsys):
    _, use = studio
    fake = use(_docs(with_folder=True))
    _filed_copy(fake, parent=FOLDER, title="Course 12 (16/05 - 07/08/69)", rows=ROWS)

    assert call(studio, "clear", "Ada Whitfield") == Exit.OK
    payload = json.loads(capsys.readouterr().out)

    assert payload["cleared"] == [1, 2, 3]
    assert payload["blocks_removed"] == 1
    assert set(fake.reset_calls) == {"doc-ada-01", "doc-ada-02", "doc-ada-03"}
    # The rows themselves are untouched: the next course reuses them.
    assert len(fake.tables[TABLE]) == 3
    # The record names the copy that stood between the clear and the course.
    assert payload["archive"]["title"] == "Course 12 (16/05 - 07/08/69)"
    assert payload["archive"]["page_id"] == "page-copy"


def test_clear_resumes_from_a_receipt_after_the_live_table_was_partly_emptied(studio, capsys):
    """The receipt, not a half-cleared table, defines the interrupted course."""

    profile, use = studio
    fake = use(_docs(with_folder=True))
    copy_id = _filed_copy(fake, parent=FOLDER, title="Course 12 (16/05 - 07/08/69)", rows=ROWS)
    copy_table = fake.children[copy_id][0].child_id
    assert call(studio, "verify", "Ada Whitfield", "--page", copy_id) == Exit.OK
    capsys.readouterr()
    archived_before = list(fake.tables[copy_table])

    fake.tables[TABLE][0] = TableRow(
        row_id=ROWS[0].row_id, title=ROWS[0].title, date="", status="Not started"
    )

    assert call(studio, "clear", "Ada Whitfield") == Exit.OK
    payload = json.loads(capsys.readouterr().out)

    assert payload["cleared"] == [1, 2, 3]
    assert payload["archive"]["verified_receipt"] is True
    assert fake.tables[copy_table] == archived_before
    assert _receipts(profile) == []


def test_clear_keeps_the_receipt_when_a_session_write_fails(studio, capsys, monkeypatch):
    profile, use = studio
    fake = use(_docs(with_folder=True))
    copy_id = _filed_copy(fake, parent=FOLDER, title="Course 12 (16/05 - 07/08/69)", rows=ROWS)
    assert call(studio, "verify", "Ada Whitfield", "--page", copy_id) == Exit.OK
    capsys.readouterr()
    original_reset = fake.reset_properties

    def reset(doc_id: str):
        if doc_id == "doc-ada-02":
            raise UpstreamError("notion rejected the property write", service="notion")
        return original_reset(doc_id)

    monkeypatch.setattr(fake, "reset_properties", reset)
    assert call(studio, "clear", "Ada Whitfield") == Exit.UPSTREAM
    capsys.readouterr()

    assert len(_receipts(profile)) == 1


def test_clear_refuses_when_the_verified_archive_changed(studio, capsys):
    profile, use = studio
    fake = use(_docs(with_folder=True))
    copy_id = _filed_copy(fake, parent=FOLDER, title="Course 12 (16/05 - 07/08/69)", rows=ROWS)
    copy_table = fake.children[copy_id][0].child_id
    assert call(studio, "verify", "Ada Whitfield", "--page", copy_id) == Exit.OK
    capsys.readouterr()
    fake.tables[copy_table][0] = replace(fake.tables[copy_table][0], status="Edited")

    assert call(studio, "clear", "Ada Whitfield") == Exit.GATE
    capsys.readouterr()

    assert fake.reset_calls == []
    assert len(_receipts(profile)) == 1


def test_clear_refuses_when_the_live_course_changed_after_verification(studio, capsys):
    profile, use = studio
    fake = use(_docs(with_folder=True))
    copy_id = _filed_copy(fake, parent=FOLDER, title="Course 12 (16/05 - 07/08/69)", rows=ROWS)
    assert call(studio, "verify", "Ada Whitfield", "--page", copy_id) == Exit.OK
    capsys.readouterr()
    fake.tables[TABLE][1] = replace(fake.tables[TABLE][1], date="2026-06-02")

    assert call(studio, "clear", "Ada Whitfield") == Exit.GATE
    capsys.readouterr()

    assert fake.reset_calls == []
    assert len(_receipts(profile)) == 1


def test_clear_refuses_a_receipt_for_different_session_documents(studio, capsys):
    profile, use = studio
    fake = use(_docs(with_folder=True))
    copy_id = _filed_copy(fake, parent=FOLDER, title="Course 12 (16/05 - 07/08/69)", rows=ROWS)
    assert call(studio, "verify", "Ada Whitfield", "--page", copy_id) == Exit.OK
    capsys.readouterr()

    connection = sqlite3.connect(profile / "data" / "studio.db")
    connection.execute(
        "UPDATE sessions SET doc_id = ? WHERE learner_id = 1 AND number = 3",
        ("doc-ada-replacement",),
    )
    connection.commit()
    connection.close()

    assert call(studio, "clear", "Ada Whitfield") == Exit.GATE
    capsys.readouterr()

    assert fake.reset_calls == []
    assert len(_receipts(profile)) == 1


def test_clear_can_be_limited_to_one_session(studio, capsys):
    """A partial clear is a mid-course tool: no finished course to file, no gate."""

    _, use = studio
    fake = use(_docs(with_folder=True))

    assert call(studio, "clear", "Ada Whitfield", "--session", "2") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["cleared"] == [2]
    assert payload["archive"] is None
    assert fake.reset_calls == ["doc-ada-02"]


def test_clear_refuses_when_no_copy_is_filed(studio, capsys):
    """The rule the studio set: a full clear never runs before the archive."""

    _, use = studio
    fake = use(_docs(with_folder=True))

    assert call(studio, "clear", "Ada Whitfield") == Exit.GATE
    assert fake.reset_calls == []


def test_clear_refuses_when_the_filed_copy_is_incomplete(studio, capsys):
    _, use = studio
    fake = use(_docs(with_folder=True))
    _filed_copy(fake, parent=FOLDER, title="Course 12 (16/05 - 07/08/69)", rows=ROWS[:2])

    assert call(studio, "clear", "Ada Whitfield") == Exit.GATE
    assert fake.reset_calls == []


def test_clear_refuses_when_the_copy_was_trashed_after_filing(studio, capsys):
    """A verify that passed yesterday protects nothing today: the gate re-reads."""

    _, use = studio
    fake = use(_docs(with_folder=True))
    copy_id = _filed_copy(fake, parent=FOLDER, title="Course 12 (16/05 - 07/08/69)", rows=ROWS)
    fake.pages[copy_id] = replace(fake.pages[copy_id], trashed=True)

    assert call(studio, "clear", "Ada Whitfield") == Exit.GATE
    assert fake.reset_calls == []


def test_clear_accepts_a_copy_filed_beside_the_live_course(studio, capsys):
    _, use = studio
    fake = use(_docs(with_folder=False))
    _filed_copy(fake, parent=MENU, title="Course 12 (16/05 - 07/08/69)", rows=ROWS)

    assert call(studio, "clear", "Ada Whitfield") == Exit.OK
    assert json.loads(capsys.readouterr().out)["cleared"] == [1, 2, 3]


def test_clear_finds_a_copy_that_was_filed_with_a_label(studio, capsys):
    _, use = studio
    fake = use(_docs(with_folder=True))
    _filed_copy(fake, parent=FOLDER, title="Course 12 (Worth It) (16/05 - 07/08/69)", rows=ROWS)

    assert call(studio, "clear", "Ada Whitfield", "--label", "Worth It") == Exit.OK
    assert json.loads(capsys.readouterr().out)["cleared"] == [1, 2, 3]


def test_clear_without_the_label_does_not_find_the_labeled_copy(studio, capsys):
    _, use = studio
    fake = use(_docs(with_folder=True))
    _filed_copy(fake, parent=FOLDER, title="Course 12 (Worth It) (16/05 - 07/08/69)", rows=ROWS)

    assert call(studio, "clear", "Ada Whitfield") == Exit.GATE
    assert fake.reset_calls == []
