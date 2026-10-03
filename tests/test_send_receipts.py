"""The same message must not go to a parent twice.

The duplicate this prevents is not caused by a bug in Baton. It is caused by a
harness: the agent's per-call time limit expires in the gap between LINE
accepting the message and Baton printing that it did, the call is killed, and
the agent (correctly, on the evidence it has) sends again.
"""

from __future__ import annotations

import json

import pytest
from tests.test_send import call, publish, studio  # noqa: F401 - fixture reuse

from baton.adapters.chat.base import SendOutcome
from baton.adapters.chat.guard import GuardedMessenger
from baton.core.receipts import Receipts
from baton.errors import DuplicateSendError, UpstreamError
from baton.exits import Exit

# -- the store -------------------------------------------------------------


def test_a_receipt_is_found_again_inside_the_window(tmp_path):
    receipts = Receipts(tmp_path / "r.json", window_hours=12)
    key = Receipts.digest("line", "U-teacher", "hello")

    receipts.record(key, recipient="U-teacher")

    assert receipts.find(key) is not None


def test_a_receipt_expires_out_of_the_window(tmp_path):
    receipts = Receipts(tmp_path / "r.json", window_hours=12)
    key = Receipts.digest("line", "U-teacher", "hello")
    receipts.record(key)

    stale = Receipts(tmp_path / "r.json", window_hours=0.0000001)

    assert stale.find(key) is None


def test_a_different_message_is_a_different_send(tmp_path):
    """A corrected summary is a new message, not a suppressed repeat."""
    receipts = Receipts(tmp_path / "r.json")
    receipts.record(Receipts.digest("line", "U-teacher", "hello"))

    assert receipts.find(Receipts.digest("line", "U-teacher", "hello, fixed")) is None


def test_the_same_message_to_another_household_is_a_different_send(tmp_path):
    receipts = Receipts(tmp_path / "r.json")
    receipts.record(Receipts.digest("line", "U-one", "hello"))

    assert receipts.find(Receipts.digest("line", "U-two", "hello")) is None


def test_the_message_itself_is_never_stored(tmp_path):
    """The state directory sits beside real learner data.

    A plain-text archive of everything ever said to a parent is not something
    a tool should create as a side effect of being careful.
    """
    receipts = Receipts(tmp_path / "r.json")
    receipts.record(Receipts.digest("line", "U-teacher", "เจอกันสัปดาห์หน้าครับ"))

    assert "เจอกัน" not in (tmp_path / "r.json").read_text(encoding="utf-8")


def test_expired_entries_are_dropped_when_the_next_one_is_written(tmp_path):
    Receipts(tmp_path / "r.json", window_hours=0.0000001).record(Receipts.digest("l", "u", "old"))
    Receipts(tmp_path / "r.json", window_hours=0.0000001).record(Receipts.digest("l", "u", "new"))

    stored = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["receipts"]

    assert len(stored) == 1


# -- the wrapper -----------------------------------------------------------


class Recorder:
    """A messenger that counts what actually left."""

    service = "telegram"

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_with = fail_with

    def resolve(self, name: str) -> str:
        return f"U-{name}"

    def health(self) -> None:
        return None

    def send(self, recipient_id: str, text: str) -> SendOutcome:
        if self.fail_with:
            raise self.fail_with
        self.sent.append((recipient_id, text))
        return SendOutcome(sent=True, recipient=recipient_id)


def _guarded(tmp_path, inner, **kwargs):
    return GuardedMessenger(inner, Receipts(tmp_path / "r.json"), **kwargs)


def test_the_second_identical_send_is_refused(tmp_path):
    inner = Recorder()
    _guarded(tmp_path, inner).send("U-teacher", "summary")

    with pytest.raises(DuplicateSendError):
        _guarded(tmp_path, inner).send("U-teacher", "summary")

    assert len(inner.sent) == 1


def test_again_delivers_anyway(tmp_path):
    """The one thing a receipt cannot know is whether the message arrived."""
    inner = Recorder()
    _guarded(tmp_path, inner).send("U-teacher", "summary")

    _guarded(tmp_path, inner, again=True).send("U-teacher", "summary")

    assert len(inner.sent) == 2


def test_a_failed_send_leaves_no_receipt(tmp_path):
    """A send that raised deserves the retry it is going to get."""
    failing = Recorder(fail_with=UpstreamError("telegram refused", service="telegram"))

    with pytest.raises(UpstreamError):
        _guarded(tmp_path, failing).send("U-teacher", "summary")

    working = Recorder()
    _guarded(tmp_path, working).send("U-teacher", "summary")

    assert len(working.sent) == 1


def test_the_refusal_names_the_message_and_carries_the_receipt(tmp_path):
    inner = Recorder()
    _guarded(tmp_path, inner, what="Ada's lesson 3 summary").send("U-teacher", "summary")

    with pytest.raises(DuplicateSendError) as excinfo:
        _guarded(tmp_path, inner, what="Ada's lesson 3 summary").send("U-teacher", "summary")

    payload = excinfo.value.to_dict()
    assert payload["exit_code"] == int(Exit.GATE)
    assert "Ada's lesson 3 summary" in payload["message"]
    assert payload["details"]["already_sent"]["sent_at"]
    assert "--again" in payload["remedy"]


def test_the_driver_underneath_stays_reachable(tmp_path):
    assert _guarded(tmp_path, Recorder()).service == "telegram"


# -- through the CLI -------------------------------------------------------


def test_sending_the_same_lesson_twice_is_refused(studio, capsys):  # noqa: F811
    profile, messenger, _docs = studio
    publish(profile, "1")

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.OK
    capsys.readouterr()

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.GATE
    payload = json.loads(capsys.readouterr().out)

    assert payload["error"] == "gate"
    assert "already sent" in payload["message"]
    assert len(messenger.sent) == 1


def test_again_sends_the_second_time(studio, capsys):  # noqa: F811
    profile, messenger, _docs = studio
    publish(profile, "1")

    call(studio, "lesson", "Ada Whitfield", "--to", "teacher")
    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher", "--again") == Exit.OK

    assert len(messenger.sent) == 2


def test_a_dry_run_writes_no_receipt(studio, capsys):  # noqa: F811
    """Checking the gate must never consume the send that follows it."""
    profile, messenger, _docs = studio
    publish(profile, "1")

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher", "--dry-run") == Exit.OK
    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.OK

    assert len(messenger.sent) == 1


def test_a_dry_run_says_when_the_real_send_would_be_refused(studio, capsys):  # noqa: F811
    """A dry run that passes must not hide the receipt that stops the real send."""
    profile, messenger, _docs = studio
    publish(profile, "1")
    call(studio, "lesson", "Ada Whitfield", "--to", "teacher")
    capsys.readouterr()

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher", "--dry-run") == Exit.OK
    captured = capsys.readouterr()

    assert json.loads(captured.out)["already_sent"]["sent_at"]
    assert "--again" in captured.err
    assert len(messenger.sent) == 1


def test_a_dry_run_before_any_send_reports_nothing_sent(studio, capsys):  # noqa: F811
    profile, _messenger, _docs = studio
    publish(profile, "1")

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher", "--dry-run") == Exit.OK

    assert json.loads(capsys.readouterr().out)["already_sent"] is None


def test_a_corrected_summary_still_needs_a_person_to_say_send_it_again(studio, capsys):  # noqa: F811
    """Republishing does not quietly re-open the send.

    The receipt is keyed on the learner and the session, not on the words:
    it has to be, because the composer picks its opening and closing phrase at
    random and no two sends of one summary are the same string. The cost is
    that a genuine correction is refused too, which is the right cost: "I
    already sent this family lesson 3; send it again?" is a question a person
    should answer, and `--again` is how they answer it.
    """
    profile, messenger, _docs = studio
    publish(profile, "1")
    call(studio, "lesson", "Ada Whitfield", "--to", "teacher")

    publish(profile, "1", short_summary="Corrected: worked on the left hand.")

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.GATE
    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher", "--again") == Exit.OK
    assert len(messenger.sent) == 2


def test_the_next_session_is_not_blocked_by_the_last_one(studio, capsys):  # noqa: F811
    """Keying on the session is what keeps the gate from swallowing next week."""
    profile, messenger, _docs = studio
    publish(profile, "1")
    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.OK

    publish(profile, "1", session_number=4, doc_id="doc-ada-03")

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.OK
    assert len(messenger.sent) == 2
