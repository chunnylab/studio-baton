"""The confirmation code `send lesson --without-video <code>` checks.

The store, in isolation from the CLI plumbing around it: the tests in
`test_send.py` cover the flow end to end; these cover the rules the flow
depends on: one code per session, single-use, expiring, and never guessable
from another session's code.
"""

from __future__ import annotations

import pytest

from baton.core.video_waivers import VideoWaivers, generate_code
from baton.errors import NeedsHumanError

KW = {"learner_name": "Ada Whitfield", "label": "week"}


def test_a_requested_code_verifies(tmp_path):
    waivers = VideoWaivers(tmp_path / "w.json")
    code = waivers.request("1", 3, sent_to="teacher")

    waivers.verify_and_consume("1", 3, code, **KW)  # raises on failure


def test_a_code_is_single_use(tmp_path):
    waivers = VideoWaivers(tmp_path / "w.json")
    code = waivers.request("1", 3, sent_to="teacher")
    waivers.verify_and_consume("1", 3, code, **KW)

    with pytest.raises(NeedsHumanError):
        waivers.verify_and_consume("1", 3, code, **KW)


def test_a_second_request_replaces_the_first_rather_than_stacking(tmp_path):
    """A person who never got the first text asks again; the old code must
    stop working rather than sit as a second live answer to the same
    question."""
    waivers = VideoWaivers(tmp_path / "w.json")
    stale = waivers.request("1", 3, sent_to="teacher")
    fresh = waivers.request("1", 3, sent_to="teacher")

    assert stale != fresh
    with pytest.raises(NeedsHumanError):
        waivers.verify_and_consume("1", 3, stale, **KW)
    waivers.verify_and_consume("1", 3, fresh, **KW)


def test_no_request_means_no_code_verifies(tmp_path):
    waivers = VideoWaivers(tmp_path / "w.json")

    with pytest.raises(NeedsHumanError):
        waivers.verify_and_consume("1", 3, "ANYCODE", **KW)


def test_the_wrong_code_is_refused_and_does_not_consume_the_real_one(tmp_path):
    waivers = VideoWaivers(tmp_path / "w.json")
    code = waivers.request("1", 3, sent_to="teacher")
    wrong = "999999" if code != "999999" else "888888"

    with pytest.raises(NeedsHumanError):
        waivers.verify_and_consume("1", 3, wrong, **KW)

    waivers.verify_and_consume("1", 3, code, **KW)


def test_a_code_only_answers_the_session_it_was_requested_for(tmp_path):
    waivers = VideoWaivers(tmp_path / "w.json")
    ada_code = waivers.request("1", 3, sent_to="teacher")
    waivers.request("2", 3, sent_to="teacher")  # Bruno's own session 3

    with pytest.raises(NeedsHumanError):
        waivers.verify_and_consume("2", 3, ada_code, **KW)

    # Bruno's own session still works for Bruno.
    bruno_code = waivers.request("2", 5, sent_to="teacher")
    waivers.verify_and_consume("2", 5, bruno_code, **KW)


def test_a_code_only_answers_the_learner_it_was_requested_for(tmp_path):
    """Same session number, two different learners: a real shape once a
    studio has more than a handful of people."""
    waivers = VideoWaivers(tmp_path / "w.json")
    ada_code = waivers.request("1", 3, sent_to="teacher")
    waivers.request("2", 3, sent_to="teacher")

    with pytest.raises(NeedsHumanError):
        waivers.verify_and_consume("2", 3, ada_code, **KW)
    waivers.verify_and_consume("1", 3, ada_code, **KW)


def test_an_expired_code_is_refused(tmp_path):
    """Unlike `Receipts`, the expiry is baked into the entry at request time
    rather than recomputed from whatever TTL happens to be configured when
    someone later answers: a studio changing that setting must not shorten
    or extend a code already on its way to someone's phone. So staleness here
    is simulated by backdating the stored `expires_at` directly, not by
    reading it back through a store built with a shorter TTL."""
    waivers = VideoWaivers(tmp_path / "w.json")
    code = waivers.request("1", 3, sent_to="teacher")
    _backdate(tmp_path / "w.json", "1", 3)

    with pytest.raises(NeedsHumanError):
        waivers.verify_and_consume("1", 3, code, **KW)


def _backdate(path, learner_id: str, session_number: int) -> None:
    from baton.core import jsonio

    raw = jsonio.read_json(path, default={})
    raw["waivers"][f"{learner_id}|{session_number}"]["expires_at"] = "2020-01-01T00:00:00+00:00"
    jsonio.write_json(path, raw)


def test_the_refusal_names_how_to_get_a_real_code(tmp_path):
    waivers = VideoWaivers(tmp_path / "w.json")

    with pytest.raises(NeedsHumanError) as excinfo:
        waivers.verify_and_consume("1", 3, "ANYCODE", **KW)

    assert "video-waiver" in (excinfo.value.remedy or "")
    assert excinfo.value.candidates == []


def test_the_refusal_names_the_configured_recipient_when_one_is_given(tmp_path):
    """A refusal is read by whoever is retrying, which is exactly whoever
    mis-typed the code: once the profile has named an approver, the remedy
    points the retry at the same person the original request reached, and a
    wrong guess on the way there never consumes the real code."""
    waivers = VideoWaivers(tmp_path / "w.json")
    code = waivers.request("1", 3, sent_to="me")

    with pytest.raises(NeedsHumanError) as plain:
        waivers.verify_and_consume("1", 3, "WRONGX", **KW)
    with pytest.raises(NeedsHumanError) as hinted:
        waivers.verify_and_consume("1", 3, "WRONGX", **KW, recipient_hint="me")

    assert "--to <contact>" in (plain.value.remedy or "")
    assert "--to me" in (hinted.value.remedy or "")

    waivers.verify_and_consume("1", 3, code, **KW)


def test_a_generated_code_avoids_ambiguous_characters():
    """Read aloud or typed on a phone, 0/O and 1/I/L should never need a
    second look to tell apart."""
    seen = "".join(generate_code() for _ in range(200))

    assert not set(seen) & set("01ILO")
