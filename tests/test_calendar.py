"""Date arithmetic, schedule parsing, and the ordering that keeps the calendar
and the documents from disagreeing.

The ordering tests are the important ones. Every other property here is about
not asking a language model to do arithmetic.
"""

from __future__ import annotations

import json
from datetime import date, time, timedelta

import pytest

from baton.adapters.cal.base import CalendarEvent
from baton.adapters.docs.base import DocStatus
from baton.adapters.fakes import FakeCalendar, FakeDocStore
from baton.domain.models import Learner, Session
from baton.domain.status import StatusVocabulary
from baton.domain.whenever import parse_date, parse_schedule, parse_time, today_in
from baton.errors import GateError, StateError, UpstreamError, UsageError
from baton.exits import Exit
from baton.pipelines.schedule import Scheduler, event_title

VOCAB = StatusVocabulary.from_config(
    {"done": "Done", "in_progress": "In progress", "not_started": "Not started"}
)
ADA = Learner(id="1", name="Ada Whitfield", instrument="guitar")
SESSION = Session(id="s", learner_id="1", number=3, doc_id="doc-3")
TODAY = date(2026, 8, 16)


# -- dates -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("2026-08-20", date(2026, 8, 20)),
        ("today", date(2026, 8, 16)),
        ("tomorrow", date(2026, 8, 17)),
        ("yesterday", date(2026, 8, 15)),
        ("+3", date(2026, 8, 19)),
        ("-2", date(2026, 8, 14)),
        ("TOMORROW", date(2026, 8, 17)),
        ("  today  ", date(2026, 8, 16)),
    ],
)
def test_date_expressions_resolve(expression, expected):
    assert parse_date(expression, reference=TODAY) == expected


def test_configured_shorthand_resolves():
    """Thai studios type พน for tomorrow; another studio types something else,
    which is why the tokens are configuration rather than code."""
    shorthand = {"พน": 1, "วน": 0, "มร": 2}

    assert parse_date("พน", shorthand=shorthand, reference=TODAY) == date(2026, 8, 17)
    assert parse_date("มร", shorthand=shorthand, reference=TODAY) == date(2026, 8, 18)


def test_an_offset_crosses_a_month_boundary():
    assert parse_date("+20", reference=date(2026, 8, 16)) == date(2026, 9, 5)


def test_an_unknown_expression_lists_what_is_understood():
    with pytest.raises(UsageError) as excinfo:
        parse_date("next tuesday", shorthand={"พน": 1}, reference=TODAY)

    assert "today" in excinfo.value.details["understood"]
    assert "พน" in excinfo.value.details["understood"]


def test_an_impossible_date_is_rejected_rather_than_rounded():
    with pytest.raises(UsageError):
        parse_date("2026-02-30", reference=TODAY)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("17:00", time(17, 0)), ("17.00", time(17, 0)), ("9", time(9, 0)), ("09:05", time(9, 5))],
)
def test_time_expressions_resolve(value, expected):
    assert parse_time(value) == expected


@pytest.mark.parametrize("value", ["25:00", "12:75", "half past", ""])
def test_impossible_times_are_rejected(value):
    with pytest.raises(UsageError):
        parse_time(value)


# -- weekdays -----------------------------------------------------------------
# TODAY, 2026-08-16, is a Sunday. A weekday always means its next occurrence,
# and today itself never counts: the rule the studio's original scripts
# lived by.

WEEKDAYS = {"จันทร์": 0, "อังคาร": 1, "พุธ": 2, "พฤหัส": 3, "ศุกร์": 4, "เสาร์": 5, "อาทิตย์": 6}


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("วันจันทร์", date(2026, 8, 17)),  # with the วัน- prefix, as people say it
        ("ศุกร์", date(2026, 8, 21)),
        ("วันศุกร์", date(2026, 8, 21)),
        ("เสาร์", date(2026, 8, 22)),
        ("อาทิตย์", date(2026, 8, 23)),  # today is Sunday; the next one is a week out
    ],
)
def test_weekday_names_mean_the_next_occurrence(expression, expected):
    assert parse_date(expression, weekdays=WEEKDAYS, reference=TODAY) == expected


def test_a_weekday_never_resolves_to_today():
    """Asked on a Sunday, "วันอาทิตย์" means next Sunday, not the day asked on."""
    assert parse_date("วันอาทิตย์", weekdays=WEEKDAYS, reference=TODAY) == TODAY + timedelta(days=7)


def test_a_weekday_crosses_a_month_boundary():
    assert parse_date("พฤหัส", weekdays=WEEKDAYS, reference=date(2026, 8, 31)) == date(2026, 9, 3)


def test_weekday_configuration_mistakes_are_named():
    with pytest.raises(UsageError, match="non-numeric"):
        parse_date("ศุกร์", weekdays={"ศุกร์": "friday"}, reference=TODAY)

    with pytest.raises(UsageError, match="no day of the week"):
        parse_date("ศุกร์", weekdays={"ศุกร์": 9}, reference=TODAY)


def test_an_unknown_expression_lists_the_weekdays_too():
    with pytest.raises(UsageError) as excinfo:
        parse_date("sometime", weekdays=WEEKDAYS, reference=TODAY)

    assert "ศุกร์" in excinfo.value.details["understood"]


# -- day-first numeric dates ---------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("12/8/2026", date(2026, 8, 12)),
        ("12-8-2026", date(2026, 8, 12)),
        ("1/2/2026", date(2026, 2, 1)),  # day first, never guessed the other way
        ("12/8", date(2026, 8, 12)),  # no year: the reference's
    ],
)
def test_day_first_dates_resolve_when_the_profile_allows_them(expression, expected):
    assert parse_date(expression, accept_dmy=True, reference=TODAY) == expected


@pytest.mark.parametrize("expression", ["31/2/2026", "12-8/2026", "12/8/26"])
def test_day_first_dates_refuse_instead_of_guessing(expression):
    """An impossible date, a mixed separator, and a two-digit year are all
    rejections: the original scripts silently kept a wrong earlier resolution
    instead, and nobody noticed until a lesson landed on the wrong day."""
    with pytest.raises(UsageError):
        parse_date(expression, accept_dmy=True, reference=TODAY)


def test_day_first_dates_are_off_until_the_profile_turns_them_on():
    with pytest.raises(UsageError):
        parse_date("12/8/2026", reference=TODAY)


# -- the studio's own time words -----------------------------------------------
# The number is read literally: "9 โมง" is 09:00, not the traditional Thai
# count. A period word says which half of the day, and it binds to every hour
# unit alike: the original scripts ignored เย็น on นาฬิกา, booking 18:00 as
# 06:00.

WORDS = {
    "hour_units": ["โมง", "นาฬิกา"],
    "morning": ["เช้า"],
    "evening": ["เย็น", "บ่าย", "กลางคืน"],
    "special": {"เที่ยง": 12, "เที่ยงคืน": 0},
    "evening_count": {"ทุ่ม": 18, "ตี": 0},
}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("9 โมง", time(9, 0)),  # bare: literal, morning is the default reading
        ("6 โมงเช้า", time(6, 0)),
        ("6 โมงเย็น", time(18, 0)),
        ("3 โมงบ่าย", time(15, 0)),
        ("6 โมงกลางคืน", time(18, 0)),
        ("6:30 โมงเย็น", time(18, 30)),  # minutes ride along, never dropped
        ("6.30 โมงเย็น", time(18, 30)),
        ("18 นาฬิกา", time(18, 0)),
        ("6 นาฬิกาเย็น", time(18, 0)),  # the period binds to นาฬิกา too
        ("3 ทุ่ม", time(21, 0)),
        ("ตี 3", time(3, 0)),
        ("เที่ยง", time(12, 0)),
        ("เที่ยงคืน", time(0, 0)),  # the longer word wins over เที่ยง
        ("17:00", time(17, 0)),  # plain digits still work with words configured
        ("9", time(9, 0)),
    ],
)
def test_time_words_resolve(value, expected):
    assert parse_time(value, words=WORDS) == expected


@pytest.mark.parametrize("value", ["11 ทุ่ม", "6 โมงเช้าเย็น", "โมงเย็น"])
def test_time_words_refuse_rather_than_wrap_or_guess(value):
    """Past 23 hours is a typo, not 05:00; contradictory words are a question
    for the person, not a coin flip; a bare unit with no number is not a time."""
    with pytest.raises(UsageError):
        parse_time(value, words=WORDS)


def test_time_words_are_configuration():
    with pytest.raises(UsageError):
        parse_time("6 โมงเย็น")


def test_a_day_list_may_be_written_the_studios_way():
    rows = parse_schedule("6 โมงเช้า น้องจี\n7 โมง -", words=WORDS, default_minutes=60)

    assert rows == [(time(6, 0), time(7, 0), "น้องจี")]


# -- schedule parsing --------------------------------------------------------


def test_a_slot_ends_when_the_next_one_begins():
    """How a back-to-back teaching day actually works."""
    rows = parse_schedule("17:00 Ada\n18:00 Bruno")

    assert rows == [(time(17, 0), time(18, 0), "Ada"), (time(18, 0), time(19, 0), "Bruno")]


def test_a_free_period_is_skipped_but_still_bounds_the_slot_before_it():
    """Without this the 17:00 lesson silently becomes two hours long."""
    rows = parse_schedule("17:00 Ada\n18:00 -\n19:00 Bruno")

    assert rows[0] == (time(17, 0), time(18, 0), "Ada")
    assert [row[2] for row in rows] == ["Ada", "Bruno"]


# The em dash is a marker a teacher types, so it is pinned here as data.
@pytest.mark.parametrize("marker", ["-", "—", "free", "ว่าง"])
def test_every_free_marker_is_recognised(marker):
    rows = parse_schedule(f"17:00 Ada\n18:00 {marker}")

    assert len(rows) == 1


def test_lines_are_sorted_so_an_out_of_order_list_still_works():
    rows = parse_schedule("19:00 Clara\n17:00 Ada\n18:00 Bruno")

    assert [row[2] for row in rows] == ["Ada", "Bruno", "Clara"]


def test_the_last_slot_gets_the_default_length():
    rows = parse_schedule("17:00 Ada", default_minutes=45)

    assert rows[0][1] == time(17, 45)


def test_a_line_without_a_time_names_the_line():
    with pytest.raises(UsageError) as excinfo:
        parse_schedule("17:00 Ada\nBruno at some point")

    assert "Line 2" in str(excinfo.value)


def test_blank_lines_are_ignored():
    assert len(parse_schedule("\n17:00 Ada\n\n18:00 Bruno\n\n")) == 2


# -- titles ------------------------------------------------------------------


def test_the_title_carries_the_instrument_emoji():
    title = event_title(ADA, 3, session_label="lesson", emoji={"guitar": "🎸", "drums": "🥁"})

    assert title == "🎸 Ada Whitfield (lesson 3)"


def test_an_unmapped_instrument_falls_back_to_the_default():
    title = event_title(ADA, 3, emoji={"drums": "🥁"}, default_emoji="🎵")

    assert title.startswith("🎵 ")


def test_no_emoji_configured_means_no_prefix():
    assert event_title(ADA, 3) == "Ada Whitfield (week 3)"


# -- the ordered gate --------------------------------------------------------


def build(*, doc_status="Not started", docs_fail=None, calendar_fail=None):
    docs = FakeDocStore(statuses={"doc-3": DocStatus(doc_id="doc-3", status=doc_status)})
    docs.fail_with = docs_fail
    calendar = FakeCalendar()
    calendar.fail_with = calendar_fail
    scheduler = Scheduler(calendar, docs, VOCAB, timezone="Asia/Bangkok", session_label="lesson")
    return scheduler, calendar, docs


def test_booking_marks_the_document_then_creates_the_event():
    scheduler, calendar, docs = build()

    result = scheduler.book(ADA, SESSION, date(2026, 8, 20), "17:00")

    assert result.doc_updated is True
    assert docs.get_status("doc-3").status == "in_progress"
    assert len(calendar.events) == 1
    assert calendar.events[0].start.startswith("2026-08-20T17:00:00+07:00")


def test_booking_the_same_session_twice_in_one_day_is_refused():
    """A re-submitted booking used to leave two identical events, and a later
    cancel removed only one of them: the calendar said the lesson existed
    after it had been cancelled."""
    scheduler, calendar, _docs = build()

    scheduler.book(ADA, SESSION, date(2026, 8, 20), "17:00")

    with pytest.raises(GateError) as excinfo:
        scheduler.book(ADA, SESSION, date(2026, 8, 20), "18:00")

    assert len(calendar.events) == 1
    assert "already" in excinfo.value.message
    assert "Nothing was booked twice" in (excinfo.value.remedy or "")
    assert f"event {calendar.events[0].id}" in excinfo.value.message


def test_a_dry_run_reports_the_duplicate_it_would_refuse():
    scheduler, calendar, _docs = build()

    scheduler.book(ADA, SESSION, date(2026, 8, 20), "17:00")

    with pytest.raises(GateError):
        scheduler.book(ADA, SESSION, date(2026, 8, 20), "18:00", dry_run=True)

    assert len(calendar.events) == 1


def test_a_second_different_session_the_same_day_is_a_real_double_lesson():
    """Two lessons in one day is a real studio arrangement; only a second
    copy of the *same* session is a re-submission."""
    docs = FakeDocStore(
        statuses={
            "doc-3": DocStatus(doc_id="doc-3", status="In progress"),
            "doc-4": DocStatus(doc_id="doc-4", status="Not started"),
        }
    )
    calendar = FakeCalendar()
    scheduler = Scheduler(calendar, docs, VOCAB, timezone="Asia/Bangkok", session_label="lesson")
    later = Session(id="s4", learner_id="1", number=4, doc_id="doc-4")
    scheduler.book(ADA, SESSION, date(2026, 8, 20), "17:00")

    result = scheduler.book(ADA, later, date(2026, 8, 20), "10:00")

    assert result.event is not None
    assert len(calendar.events) == 2


def test_no_event_is_created_when_the_document_update_fails():
    """The property this ordering exists for. An event with no matching session
    is the drift that made the original system untrustworthy."""
    scheduler, calendar, _docs = build(docs_fail=UpstreamError("notion down", service="notion"))

    with pytest.raises(StateError) as excinfo:
        scheduler.book(ADA, SESSION, date(2026, 8, 20), "17:00")

    assert calendar.events == []
    assert "Nothing was booked" in (excinfo.value.remedy or "")


def test_the_end_time_defaults_to_an_hour_later():
    scheduler, calendar, _docs = build()

    scheduler.book(ADA, SESSION, date(2026, 8, 20), "17:00")

    assert calendar.events[0].end.startswith("2026-08-20T18:00:00")


def test_an_explicit_end_time_is_used():
    scheduler, calendar, _docs = build()

    scheduler.book(ADA, SESSION, date(2026, 8, 20), "17:00", "17:45")

    assert calendar.events[0].end.startswith("2026-08-20T17:45:00")


def test_dry_run_touches_neither_store():
    scheduler, calendar, docs = build()

    result = scheduler.book(ADA, SESSION, date(2026, 8, 20), "17:00", dry_run=True)

    assert calendar.events == []
    assert docs.get_status("doc-3").status == "Not started"
    assert result.event.title.endswith("(lesson 3)")


def test_the_event_carries_the_studio_timezone_not_the_servers():
    """A studio in Bangkok on a UTC host would otherwise book seven hours out."""
    scheduler, calendar, _docs = build()

    scheduler.book(ADA, SESSION, date(2026, 8, 20), "17:00")

    assert "+07:00" in calendar.events[0].start


# -- cancelling --------------------------------------------------------------


def test_cancelling_removes_the_event_then_rolls_the_document_back():
    scheduler, calendar, docs = build()
    scheduler.book(ADA, SESSION, date(2026, 8, 16), "17:00")

    result = scheduler.cancel(ADA, SESSION, date(2026, 8, 16), today=TODAY)

    assert calendar.events == []
    assert docs.get_status("doc-3").status == "not_started"
    assert len(result["deleted"]) == 1


def test_cancelling_clears_the_date_the_booking_wrote():
    """Booking writes status and date together; a cancel that restored only the
    status left the page dated for a lesson that never happened, and on the
    flood day every one of those had to be patched by hand."""
    scheduler, _calendar, docs = build()
    scheduler.book(ADA, SESSION, date(2026, 8, 16), "17:00")
    assert docs.get_status("doc-3").date == "2026-08-16"

    result = scheduler.cancel(ADA, SESSION, date(2026, 8, 16), today=TODAY)

    assert docs.get_status("doc-3").date == ""
    assert result["cleared"] == ["date"]


def test_cancel_dry_run_names_the_date_it_would_clear():
    scheduler, _calendar, docs = build()
    scheduler.book(ADA, SESSION, TODAY, "17:00")

    result = scheduler.cancel(ADA, SESSION, TODAY, today=TODAY, dry_run=True)

    assert result["would_clear"] == ["date"]
    assert docs.get_status("doc-3").date == TODAY.isoformat()


def test_cancelling_outside_the_window_is_blocked():
    """Rewriting last week's records is usually a mistake, not an intention."""
    scheduler, calendar, docs = build()
    scheduler.book(ADA, SESSION, date(2026, 8, 5), "17:00")

    with pytest.raises(GateError) as excinfo:
        scheduler.cancel(ADA, SESSION, date(2026, 8, 5), today=TODAY, rollback_window_days=1)

    assert excinfo.value.exit_code == Exit.GATE
    assert len(calendar.events) == 1  # untouched
    assert docs.get_status("doc-3").status == "in_progress"


def test_a_lesson_weeks_ahead_can_be_cancelled():
    """The window guards the past; a booking next month has no history yet."""
    scheduler, calendar, docs = build()
    scheduler.book(ADA, SESSION, date(2026, 9, 13), "17:00")

    result = scheduler.cancel(ADA, SESSION, date(2026, 9, 13), today=TODAY, rollback_window_days=1)

    assert result["status"] == "not_started"
    assert len(calendar.events) == 0
    assert docs.get_status("doc-3").status == "not_started"


def test_the_window_is_configurable():
    scheduler, _calendar, _docs = build()

    result = scheduler.cancel(ADA, SESSION, date(2026, 8, 5), today=TODAY, rollback_window_days=30)

    assert result["status"] == "not_started"


def test_a_completed_session_is_not_cancelled():
    scheduler, _calendar, _docs = build(doc_status="Done")

    with pytest.raises(GateError) as excinfo:
        scheduler.cancel(ADA, SESSION, TODAY, today=TODAY)

    assert "already done" in str(excinfo.value)


def test_cancel_dry_run_changes_nothing():
    scheduler, calendar, docs = build()
    scheduler.book(ADA, SESSION, TODAY, "17:00")

    result = scheduler.cancel(ADA, SESSION, TODAY, today=TODAY, dry_run=True)

    assert result["dry_run"] is True
    assert len(calendar.events) == 1
    assert docs.get_status("doc-3").status == "in_progress"


def test_cancelling_twice_is_not_an_error():
    """The desired state is reached either way; a retry must not fail."""
    scheduler, _calendar, _docs = build()
    scheduler.book(ADA, SESSION, TODAY, "17:00")

    scheduler.cancel(ADA, SESSION, TODAY, today=TODAY)
    again = scheduler.cancel(ADA, SESSION, TODAY, today=TODAY)

    assert again["deleted"] == []


def test_only_this_learners_events_are_cancelled():
    scheduler, calendar, _docs = build()
    scheduler.book(ADA, SESSION, TODAY, "17:00")
    bruno = Learner(id="2", name="Bruno Castell")
    scheduler.book(
        bruno, Session(id="s2", learner_id="2", number=1, doc_id="doc-3"), TODAY, "18:00"
    )

    scheduler.cancel(ADA, SESSION, TODAY, today=TODAY)

    assert [event.title for event in calendar.events] == ["Bruno Castell (lesson 1)"]


# -- a name that is a prefix of another name ---------------------------------


def test_cancelling_ann_does_not_touch_anna():
    """Substring matching made "Ann" a match for "Anna (lesson 3)"; the title
    Baton writes is `Name (label N)`, so the match anchors on that shape."""
    scheduler, calendar, _docs = build()
    ann = Learner(id="1", name="Ann")
    anna = Learner(id="2", name="Anna")
    scheduler.book(ann, Session(id="s1", learner_id="1", number=1, doc_id="doc-3"), TODAY, "17:00")
    scheduler.book(anna, Session(id="s2", learner_id="2", number=3, doc_id="doc-3"), TODAY, "18:00")

    scheduler.cancel(
        ann, Session(id="s1", learner_id="1", number=1, doc_id="doc-3"), TODAY, today=TODAY
    )

    assert [event.title for event in calendar.events] == ["Anna (lesson 3)"]


def test_the_exact_match_still_works_with_an_instrument_emoji():
    scheduler = Scheduler(
        FakeCalendar(),
        FakeDocStore(statuses={"doc-3": DocStatus(doc_id="doc-3", status="Not started")}),
        VOCAB,
        timezone="Asia/Bangkok",
        session_label="lesson",
        event_emoji={"piano": "🎹"},
        default_emoji="🎵",
    )
    ada = Learner(id="1", name="Ada", instrument="piano")
    adrian = Learner(id="2", name="Adrian", instrument="piano")
    scheduler.book(ada, SESSION, TODAY, "17:00")
    scheduler.book(
        adrian, Session(id="s2", learner_id="2", number=1, doc_id="doc-3"), TODAY, "18:00"
    )

    scheduler.cancel(
        adrian, Session(id="s2", learner_id="2", number=1, doc_id="doc-3"), TODAY, today=TODAY
    )

    assert [event.title for event in scheduler.calendar.events] == ["🎹 Ada (lesson 3)"]


def test_cancelling_a_session_with_no_document_removes_the_event():
    """`doc_id=""` is the schema's default; reading its status asked the store
    for a page id that is empty and reported a sharing problem."""
    scheduler, calendar, docs = build()
    bare = Session(id="s9", learner_id="1", number=9, doc_id="")
    scheduler.book(ADA, bare, TODAY, "17:00", dry_run=False)

    result = scheduler.cancel(ADA, bare, TODAY, today=TODAY)

    assert result["deleted"]
    assert calendar.events == []
    # The other session's document was never read or reset.
    assert docs.statuses["doc-3"].status == "Not started"


# -- times that are not a real lesson ----------------------------------------


def test_a_reversed_range_is_refused_rather_than_booked():
    scheduler, _calendar, _docs = build()

    with pytest.raises(UsageError) as excinfo:
        scheduler.book(ADA, SESSION, date(2026, 8, 20), "18:00", "17:00")

    assert "cannot end" in str(excinfo.value)


def test_a_late_start_crossing_midnight_ends_the_next_day():
    """23:30 + 60 minutes is 00:30 tomorrow. Comparing bare `time` values
    rolled it back to 00:30 the same day: an event that ended before it
    began."""
    scheduler, _calendar, _docs = build()

    result = scheduler.book(ADA, SESSION, date(2026, 8, 20), "23:30")

    assert result.event is not None
    assert result.event.end.startswith("2026-08-21T00:30")
    assert result.event.end > result.event.start


def test_an_equal_start_and_end_is_refused():
    scheduler, _calendar, _docs = build()

    with pytest.raises(UsageError):
        scheduler.book(ADA, SESSION, date(2026, 8, 20), "17:00", "17:00")


def test_a_schedule_with_one_name_twice_is_refused(profile):
    """Two slots for one learner hand `_pick_session` the same in-progress
    session twice and put the lesson on the calendar twice; the send batch
    has refused this from the start."""
    from baton.cli.app import run as cli_run

    # The duplicate check sits before any store is opened, so a loadable
    # profile is all the state this needs.
    code = cli_run(
        [
            "--profile",
            str(profile),
            "calendar",
            "schedule",
            "--date",
            "2026-08-20",
            "--text",
            "17:00 Ada Whitfield\n18:00 ada whitfield",
        ]
    )

    assert code == Exit.USAGE


# -- who is mid-session: the calendar window ---------------------------------

BRUNO = Learner(id="2", name="Bruno Castell")


def _window(calendar, docs=None):
    """A scheduler over the given calendar, plus its learner store."""
    from baton.adapters.fakes import FakeLearnerStore

    store = FakeLearnerStore(
        learners=[ADA, BRUNO],
        sessions=[
            Session(id="s3", learner_id="1", number=3, doc_id="doc-3"),
            Session(id="b9", learner_id="2", number=9, doc_id="doc-9"),
            Session(id="b4", learner_id="2", number=4, doc_id="doc-4"),
        ],
    )
    docs = docs or FakeDocStore(
        statuses={
            "doc-3": DocStatus(doc_id="doc-3", status="In progress"),
            "doc-9": DocStatus(doc_id="doc-9", status="In progress"),
            "doc-4": DocStatus(doc_id="doc-4", status="In progress"),
        }
    )
    scheduler = Scheduler(calendar, docs, VOCAB, timezone="Asia/Bangkok", session_label="lesson")
    return scheduler, store


def _event(title, start):
    return CalendarEvent(id=f"ev-{title[:6]}", title=title, start=start, end=start)


def test_in_progress_reads_the_window_not_the_world():
    """One calendar call, then one page per candidate. A lesson outside the
    window is never looked at, whatever its page says."""
    calendar = FakeCalendar(
        [
            _event("Ada Whitfield (lesson 3)", "2026-08-14T17:00:00+07:00"),
            _event("Bruno Castell (lesson 9)", "2026-08-15T10:00:00+07:00"),
            _event("Bruno Castell (lesson 4)", "2026-07-20T10:00:00+07:00"),  # 27 days back
        ]
    )
    scheduler, store = _window(calendar)

    report = scheduler.in_progress(store, today=date(2026, 8, 16))

    assert [(learner.name, view.number) for learner, view in report.found] == [
        ("Ada Whitfield", 3),
        ("Bruno Castell", 9),
    ]
    assert report.unmatched == []


def test_a_summarized_lesson_drops_out_on_its_own():
    """The event is still on the calendar, but the page says done: the page
    is the truth, and the learner owes nothing."""
    docs = FakeDocStore(
        statuses={
            "doc-3": DocStatus(doc_id="doc-3", status="Done"),
            "doc-9": DocStatus(doc_id="doc-9", status="In progress"),
            "doc-4": DocStatus(doc_id="doc-4", status="In progress"),
        }
    )
    calendar = FakeCalendar([_event("Ada Whitfield (lesson 3)", "2026-08-14T17:00:00+07:00")])
    scheduler, store = _window(calendar, docs)

    report = scheduler.in_progress(store, today=date(2026, 8, 16))

    assert report.found == []


def test_a_booking_for_a_coming_day_is_outside_the_window():
    calendar = FakeCalendar([_event("Ada Whitfield (lesson 3)", "2026-08-17T17:00:00+07:00")])
    scheduler, store = _window(calendar)

    report = scheduler.in_progress(store, today=date(2026, 8, 16))

    assert report.found == []


def test_an_event_typed_by_hand_is_listed_never_guessed_at():
    calendar = FakeCalendar(
        [
            _event("Dentist", "2026-08-15T09:00:00+07:00"),
            _event("Ada Whitfield (lesson 3)", "2026-08-15T17:00:00+07:00"),
        ]
    )
    scheduler, store = _window(calendar)

    report = scheduler.in_progress(store, today=date(2026, 8, 16))

    assert [entry["title"] for entry in report.unmatched] == ["Dentist"]
    assert len(report.found) == 1


def test_one_unreadable_page_does_not_take_the_report_down():
    """The containment the old whole-world scan lacked: a single bad page is
    that learner's line, not the end of the morning check."""

    class OneBadPage(FakeDocStore):
        def get_status(self, doc_id, *, with_blocks=True):
            if doc_id == "doc-9":
                raise UpstreamError("page is gone", service="notion")
            return super().get_status(doc_id, with_blocks=with_blocks)

    calendar = FakeCalendar(
        [
            _event("Ada Whitfield (lesson 3)", "2026-08-14T17:00:00+07:00"),
            _event("Bruno Castell (lesson 9)", "2026-08-15T10:00:00+07:00"),
        ]
    )
    scheduler, store = _window(
        calendar,
        OneBadPage(
            statuses={
                "doc-3": DocStatus(doc_id="doc-3", status="In progress"),
                "doc-9": DocStatus(doc_id="doc-9", status="In progress"),
                "doc-4": DocStatus(doc_id="doc-4", status="In progress"),
            }
        ),
    )

    report = scheduler.in_progress(store, today=date(2026, 8, 16))

    assert [(learner.name, view.number) for learner, view in report.found] == [("Ada Whitfield", 3)]
    assert report.unreadable == [{"learner": "Bruno Castell", "number": 9, "why": "page is gone"}]


def test_an_icon_prefixed_title_still_names_its_learner():
    calendar = FakeCalendar([_event("🎸 Ada Whitfield (lesson 3)", "2026-08-14T17:00:00+07:00")])
    scheduler, store = _window(calendar)
    scheduler.event_emoji = {"guitar": "🎸"}

    report = scheduler.in_progress(store, today=date(2026, 8, 16))

    assert [(learner.name, view.number) for learner, view in report.found] == [("Ada Whitfield", 3)]


# -- listing a range ------------------------------------------------------------


@pytest.fixture
def listed(monkeypatch, profile):
    """`calendar list` against a fake calendar, via the real CLI."""
    calendar = FakeCalendar(
        [
            _event("Ada Whitfield (lesson 3)", "2026-08-14T17:00:00+07:00"),
            _event("Bruno Castell (lesson 9)", "2026-08-15T10:00:00+07:00"),
            _event("Ada Whitfield (lesson 4)", "2026-08-16T17:00:00+07:00"),
        ]
    )
    monkeypatch.setattr("baton.cli.cmd_calendar.open_calendar", lambda _config: calendar)
    return calendar


def _run_list(profile, capsys, *args):
    from baton.cli.app import run

    assert run(["--profile", str(profile), "--json", "calendar", "list", *args]) == Exit.OK
    return json.loads(capsys.readouterr().out)


def test_a_single_day_keeps_its_shape(listed, profile, capsys):
    payload = _run_list(profile, capsys, "2026-08-14")

    assert payload["date"] == "2026-08-14"
    assert [event["title"] for event in payload["events"]] == ["Ada Whitfield (lesson 3)"]


def test_a_range_groups_by_day_and_shows_the_empty_ones(listed, profile, capsys):
    payload = _run_list(profile, capsys, "--from", "2026-08-14", "--to", "2026-08-17")

    assert payload["from"] == "2026-08-14"
    assert payload["to"] == "2026-08-17"
    assert [day["date"] for day in payload["days"]] == [
        "2026-08-14",
        "2026-08-15",
        "2026-08-16",
        "2026-08-17",
    ]
    # The 17th has nothing on it and still appears: a gap is information.
    assert payload["days"][3]["events"] == []
    assert payload["days"][0]["events"][0]["title"] == "Ada Whitfield (lesson 3)"


def test_a_range_accepts_the_full_date_grammar(listed, profile, capsys):
    """--from and --to go through the same resolution as every other date."""
    payload = _run_list(profile, capsys, "--from", "+0", "--to", "+1")

    # The grammar resolves against the studio's timezone; the expectation has
    # to use that clock too. date.today() reads the runner's, and a UTC CI run
    # just past Bangkok midnight failed this for hours every day.
    today = today_in("Asia/Bangkok")
    assert payload["from"] == today.isoformat()
    assert payload["to"] == (today + timedelta(days=1)).isoformat()


@pytest.mark.parametrize(
    "args",
    [
        ("2026-08-14", "--from", "2026-08-14", "--to", "2026-08-15"),
        (
            "--from",
            "2026-08-14",
        ),
        ("--from", "2026-08-15", "--to", "2026-08-14"),
    ],
)
def test_range_argument_mistakes_are_refused(listed, profile, args):
    from baton.cli.app import run

    assert run(["--profile", str(profile), "--json", "calendar", "list", *args]) == Exit.USAGE


def test_an_inactive_learners_stale_booking_is_unmatched_not_owed():
    """The leftover event of someone who stopped studying must not keep them
    in "who owes a summary" forever; the person still sees it, filed under
    unmatched next to the dentist."""
    calendar = FakeCalendar(
        [
            _event("Ada Whitfield (lesson 3)", "2026-08-15T17:00:00+07:00"),
            _event("Bruno Castell (lesson 9)", "2026-08-15T10:00:00+07:00"),
        ]
    )
    scheduler, store = _window(calendar)
    store.set_active(BRUNO.id, False)

    report = scheduler.in_progress(store, today=date(2026, 8, 16))

    assert [(learner.name, view.number) for learner, view in report.found] == [("Ada Whitfield", 3)]
    assert [entry["title"] for entry in report.unmatched] == ["Bruno Castell (lesson 9)"]


def test_who_is_booked_skips_a_deactivated_learner():
    """Same scope on the send roster: the day's list is who still studies."""
    calendar = FakeCalendar(
        [
            _event("Ada Whitfield (lesson 3)", "2026-08-15T17:00:00+07:00"),
            _event("Bruno Castell (lesson 9)", "2026-08-15T10:00:00+07:00"),
        ]
    )
    scheduler, store = _window(calendar)
    store.set_active(BRUNO.id, False)

    learners, unmatched = scheduler.who_is_booked(store, day=date(2026, 8, 15))

    assert [learner.name for learner in learners] == ["Ada Whitfield"]
    assert [entry["title"] for entry in unmatched] == ["Bruno Castell (lesson 9)"]


# -- cancelling a whole day ----------------------------------------------------
#
# The flood day: seven lessons cancelled by hand, one command at a time. The
# whole-day cancel has to be at least as careful as those seven were.


FLOOD_DAY = date(2026, 8, 15)


def _day(*titles_and_starts, docs=None):
    events = [
        CalendarEvent(id=f"ev-{index}", title=title, start=start, end=start)
        for index, (title, start) in enumerate(titles_and_starts)
    ]
    calendar = FakeCalendar(events)
    scheduler, store = _window(calendar, docs)
    return scheduler, store, calendar


def test_a_lost_day_cancels_every_booked_lesson_and_clears_their_dates():
    docs = FakeDocStore(
        statuses={
            "doc-3": DocStatus(doc_id="doc-3", status="In progress", date="2026-08-15"),
            "doc-9": DocStatus(doc_id="doc-9", status="In progress", date="2026-08-15"),
        }
    )
    scheduler, store, calendar = _day(
        ("Ada Whitfield (lesson 3)", "2026-08-15T17:00:00+07:00"),
        ("Bruno Castell (lesson 9)", "2026-08-15T10:00:00+07:00"),
        docs=docs,
    )

    result = scheduler.cancel_day(store, FLOOD_DAY, today=TODAY)

    assert calendar.events == []
    assert [item["learner"] for item in result["cancelled"]] == ["Bruno Castell", "Ada Whitfield"]
    for doc_id in ("doc-3", "doc-9"):
        assert docs.get_status(doc_id).status == "not_started"
        assert docs.get_status(doc_id).date == ""
    assert result["failed"] == []


def test_a_lesson_already_done_keeps_its_event():
    """The morning's lesson happened before the day went wrong."""
    docs = FakeDocStore(
        statuses={
            "doc-3": DocStatus(doc_id="doc-3", status="Done", date="2026-08-15"),
            "doc-9": DocStatus(doc_id="doc-9", status="In progress", date="2026-08-15"),
        }
    )
    scheduler, store, calendar = _day(
        ("Ada Whitfield (lesson 3)", "2026-08-15T09:00:00+07:00"),
        ("Bruno Castell (lesson 9)", "2026-08-15T15:00:00+07:00"),
        docs=docs,
    )

    result = scheduler.cancel_day(store, FLOOD_DAY, today=TODAY)

    assert [event.title for event in calendar.events] == ["Ada Whitfield (lesson 3)"]
    assert docs.get_status("doc-3").status == "Done"
    assert [item["title"] for item in result["kept"]] == ["Ada Whitfield (lesson 3)"]


def test_events_baton_did_not_write_are_listed_and_left_alone():
    scheduler, store, calendar = _day(
        ("Ada Whitfield (lesson 3)", "2026-08-15T17:00:00+07:00"),
        ("Dentist", "2026-08-15T12:00:00+07:00"),
    )

    result = scheduler.cancel_day(store, FLOOD_DAY, today=TODAY)

    assert [event.title for event in calendar.events] == ["Dentist"]
    assert [item["title"] for item in result["left_alone"]] == ["Dentist"]


def test_the_standing_series_on_the_day_are_reported_not_touched():
    """The real calendar keeps the weekly pattern out of `list_between`, so
    1.10.0 reported nothing about it and a lost day looked empty when it was
    not. A series counts when it began on or before the day, same weekday."""
    from baton.adapters.cal.base import StandingSpec

    scheduler, store, calendar = _day(("Ada Whitfield (lesson 3)", "2026-08-15T17:00:00+07:00"))
    saturday = StandingSpec(
        learner_id="1",
        title="🎸 Ada Whitfield · คาบประจำ",
        weekday="Saturday",
        start="17:00",
        end="18:00",
        timezone="Asia/Bangkok",
    )
    calendar.create_standing(saturday, first_date="2026-08-01")
    later = StandingSpec(
        learner_id="2",
        title="Bruno Castell · คาบประจำ",
        weekday="Saturday",
        start="10:00",
        end="11:00",
        timezone="Asia/Bangkok",
    )
    calendar.create_standing(later, first_date="2026-08-22")
    sunday = StandingSpec(
        learner_id="2",
        title="Bruno Castell · คาบประจำ",
        weekday="Sunday",
        start="10:00",
        end="11:00",
        timezone="Asia/Bangkok",
    )
    calendar.create_standing(sunday, first_date="2026-08-02")

    dry = scheduler.cancel_day(store, FLOOD_DAY, today=TODAY, dry_run=True)
    result = scheduler.cancel_day(store, FLOOD_DAY, today=TODAY)

    assert [item["title"] for item in dry["standing"]] == ["🎸 Ada Whitfield · คาบประจำ"]
    assert [item["title"] for item in result["standing"]] == ["🎸 Ada Whitfield · คาบประจำ"]
    assert len(calendar.list_standing()) == 3


def test_an_unmatched_session_blocks_the_whole_day_before_anything_changes():
    """Fail closed: stopping at the eighth lesson leaves a day neither on nor off."""
    scheduler, store, calendar = _day(
        ("Ada Whitfield (lesson 3)", "2026-08-15T17:00:00+07:00"),
        ("Bruno Castell (lesson 77)", "2026-08-15T10:00:00+07:00"),
    )

    with pytest.raises(GateError) as excinfo:
        scheduler.cancel_day(store, FLOOD_DAY, today=TODAY)

    assert excinfo.value.exit_code == Exit.GATE
    assert "lesson 77" in excinfo.value.missing[0]["reason"]
    assert len(calendar.events) == 2


def test_a_day_weeks_ahead_can_be_cancelled():
    scheduler, store, calendar = _day(
        ("Ada Whitfield (lesson 3)", "2026-09-13T17:00:00+07:00"),
    )

    result = scheduler.cancel_day(store, date(2026, 9, 13), today=TODAY, rollback_window_days=1)

    assert len(result["cancelled"]) == 1
    assert len(calendar.events) == 0


def test_a_day_outside_the_window_is_refused():
    scheduler, store, calendar = _day(
        ("Ada Whitfield (lesson 3)", "2026-08-05T17:00:00+07:00"),
    )

    with pytest.raises(GateError):
        scheduler.cancel_day(store, date(2026, 8, 5), today=TODAY, rollback_window_days=1)

    assert len(calendar.events) == 1


def test_a_whole_day_dry_run_changes_nothing():
    scheduler, store, calendar = _day(
        ("Ada Whitfield (lesson 3)", "2026-08-15T17:00:00+07:00"),
    )

    result = scheduler.cancel_day(store, FLOOD_DAY, today=TODAY, dry_run=True)

    assert [item["learner"] for item in result["would_cancel"]] == ["Ada Whitfield"]
    assert len(calendar.events) == 1


def test_a_second_run_after_a_partial_failure_finishes_the_day():
    """Past the gate, a failure is upstream. The re-run starts from what is left."""
    scheduler, store, calendar = _day(
        ("Ada Whitfield (lesson 3)", "2026-08-15T17:00:00+07:00"),
        ("Bruno Castell (lesson 9)", "2026-08-15T10:00:00+07:00"),
    )
    real_delete = calendar.delete
    calls = {"n": 0}

    def flaky(event_id):
        calls["n"] += 1
        if calls["n"] == 2:
            raise UpstreamError("calendar hiccup", service="calendar")
        return real_delete(event_id)

    calendar.delete = flaky
    first = scheduler.cancel_day(store, FLOOD_DAY, today=TODAY)
    assert len(first["cancelled"]) == 1 and len(first["failed"]) == 1

    calendar.delete = real_delete
    second = scheduler.cancel_day(store, FLOOD_DAY, today=TODAY)

    assert len(second["cancelled"]) == 1
    assert calendar.events == []
