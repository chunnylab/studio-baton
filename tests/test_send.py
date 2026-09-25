"""The send gate, and everything around it.

The matrix at the heart of this file (pull one required field out at a time)
is the whole point of the phase. A send that quietly goes out incomplete is
the failure mode the original system's gate existed to prevent, and "no
override flag" only means something if it survives contact with tests.
"""

from __future__ import annotations

import json
import re
import sqlite3
import textwrap
from pathlib import Path

import pytest

import baton
from baton.adapters.chat.base import SendOutcome
from baton.adapters.docs.base import Block, DocStatus
from baton.adapters.fakes import FakeDocStore, FakeLearnerStore
from baton.cli.app import run
from baton.domain.models import Learner, Piece
from baton.errors import GateError, NeedsHumanError, UpstreamError
from baton.exits import Exit
from baton.pipelines.send import compose_message, gate_check, gather_context, send_lesson
from baton.pipelines.staging import PieceSnapshot

MIGRATIONS = Path(baton.__file__).resolve().parent / "migrations"

PUBLISHED = {
    "learner_id": "1",
    "learner_name": "Ada Whitfield",
    "session_number": 3,
    "doc_id": "doc-ada-03",
    "doc_url": "https://example.invalid/lesson-3",
    "short_message": "• Covered: Blackbird\n• Progress: Tempo held",
}

STORE = FakeLearnerStore(
    learners=[Learner(id="1", name="Ada Whitfield", instrument="guitar", current_piece_id="2")],
    pieces=[Piece(id="2", title="Blackbird", practice_track="https://example.invalid/track.mp3")],
)

REQUIRED = ["doc_link", "short_summary", "session_number"]
OPTIONAL = ["practice_track", "video_link"]


class FakeMessenger:
    """Records sends; fails on demand."""

    driver = "fake"

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_with = fail_with

    def resolve(self, name: str) -> str:
        try:
            return {"teacher": "U-teacher", "me": "U-teacher"}[name]
        except KeyError:
            from baton.errors import NeedsHumanError

            raise NeedsHumanError(
                f'No contact matches "{name}".',
                candidates=[{"name": "teacher"}],
            ) from None

    def send(self, recipient_id: str, text: str) -> SendOutcome:
        if self.fail_with:
            raise self.fail_with
        self.sent.append((recipient_id, text))
        return SendOutcome(sent=True, recipient=recipient_id)

    def health(self) -> None:
        pass


# Gate field names → SendContext attribute names.
def context(**overrides) -> object:
    """A complete context, addressed by *gate* field name so a test can blank
    exactly the field the gate is asked about."""
    from baton.pipelines.send import SendContext

    attributes = {
        "doc_link": "doc_url",
        "short_summary": "short_message",
        "session_number": "session_number",
        "video_link": "video_link",
        "practice_track": "practice_track",
    }
    values = {
        "doc_link": "https://example.invalid/lesson-3",
        "short_summary": "• Covered: Blackbird",
        "session_number": 3,
        "video_link": "https://example.invalid/watch/3",
        "practice_track": "https://example.invalid/track.mp3",
    }
    for key, value in overrides.items():
        values[key] = value
    kwargs = {attributes[key]: value for key, value in values.items()}
    return SendContext(learner_name="Ada Whitfield", doc_id="doc-ada-03", **kwargs)


# -- the gate ----------------------------------------------------------------


@pytest.mark.parametrize("field", ["doc_link", "short_summary", "session_number"])
def test_a_missing_required_field_blocks_the_send(field):
    """One test per field. Each of these is a message that must not go out."""
    with pytest.raises(GateError) as excinfo:
        gate_check(
            context(**{field: ""}),  # type: ignore[arg-type]
            required=REQUIRED,
            optional=OPTIONAL,
        )

    assert [item["field"] for item in excinfo.value.missing] == [field]
    assert excinfo.value.missing[0]["how_to_fix"]


def test_the_block_names_every_missing_field_at_once():
    with pytest.raises(GateError) as excinfo:
        gate_check(
            context(doc_link="", short_summary="", session_number=0),  # type: ignore[arg-type]
            required=REQUIRED,
            optional=OPTIONAL,
        )

    assert {item["field"] for item in excinfo.value.missing} == {
        "doc_link",
        "short_summary",
        "session_number",
    }


def test_the_block_says_nothing_was_sent_and_that_there_is_no_override():
    with pytest.raises(GateError) as excinfo:
        gate_check(context(doc_link=""), required=REQUIRED, optional=OPTIONAL)  # type: ignore[arg-type]

    assert "Nothing was sent" in (excinfo.value.remedy or "")
    assert "no flag" in (excinfo.value.remedy or "").lower()


def test_whitespace_only_values_count_as_missing():
    with pytest.raises(GateError):
        gate_check(
            context(short_summary="   "),  # type: ignore[arg-type]
            required=REQUIRED,
            optional=OPTIONAL,
        )


def test_a_complete_context_passes_and_warns_about_the_optional_gaps():
    _, warnings = gate_check(
        context(video_link="", practice_track=""),  # type: ignore[arg-type]
        required=REQUIRED,
        optional=OPTIONAL,
    )

    assert {item["field"] for item in warnings} == {"video_link", "practice_track"}


def test_zero_is_a_missing_session_number_but_zero_is_valid_nowhere_else():
    with pytest.raises(GateError):
        gate_check(
            context(session_number=0),  # type: ignore[arg-type]
            required=["session_number"],
            optional=[],
        )


def test_the_required_set_is_configuration_not_code():
    """A studio that does not require the document link may drop it; one that
    also requires the recording may add it. The block itself is not negotiable."""
    _, _warnings = gate_check(context(doc_link=""), required=["short_summary"], optional=[])  # type: ignore[arg-type]


# -- composition -------------------------------------------------------------


def test_the_message_is_the_published_one_plus_batons_own_links():
    message = compose_message(context())  # type: ignore[arg-type]

    assert "• Covered: Blackbird" in message
    assert "https://example.invalid/lesson-3" in message
    assert "เฉพาะ Video: https://example.invalid/watch/3" in message


def test_the_recording_line_is_absent_when_there_is_no_recording():
    message = compose_message(context(video_link=""))  # type: ignore[arg-type]

    assert "เฉพาะ Video:" not in message
    assert "https://example.invalid/lesson-3" in message


def test_the_instrument_icon_matches_the_learner():
    message = compose_message(context())  # type: ignore[arg-type]
    from baton.pipelines.send import SendContext

    guitar = compose_message(
        SendContext(
            learner_name="Ada",
            doc_id="d",
            doc_url="u",
            short_message="s",
            session_number=1,
            instrument="กีตาร์",
        )
    )
    drums = compose_message(
        SendContext(
            learner_name="Ada",
            doc_id="d",
            doc_url="u",
            short_message="s",
            session_number=1,
            instrument="กลอง",
        )
    )

    assert message  # a context with no instrument still composes
    assert guitar.startswith("🎸")
    assert drums.startswith("🥁")


def test_titles_appear_only_when_present():
    from baton.pipelines.send import SendContext

    with_title = compose_message(
        SendContext(
            learner_name="Ada",
            doc_id="d",
            doc_url="u",
            short_message="s",
            session_number=1,
            titles="Blackbird",
        )
    )
    without_title = compose_message(
        SendContext(
            learner_name="Ada", doc_id="d", doc_url="u", short_message="s", session_number=1
        )
    )

    assert "🎵 Blackbird" in with_title
    assert "🎵" not in without_title


def test_gathered_context_pulls_the_practice_track_from_the_assigned_piece():
    gathered = gather_context(STORE, "1", PUBLISHED, video_link="v")

    assert gathered.practice_track.endswith("track.mp3")
    assert gathered.learner_name == "Ada Whitfield"


def test_gathered_context_pulls_the_instrument_from_the_learner():
    gathered = gather_context(STORE, "1", PUBLISHED, video_link="v")

    assert gathered.instrument == "guitar"


def test_gathered_context_passes_through_the_documents_date_and_titles():
    gathered = gather_context(STORE, "1", PUBLISHED, date="2026-08-23", titles="Blackbird")

    assert gathered.date == "2026-08-23"
    assert gathered.titles == "Blackbird"


def test_gathered_context_survives_a_learner_with_no_piece():
    bare = FakeLearnerStore(learners=[Learner(id="9", name="No Piece")])

    gathered = gather_context(bare, "9", {**PUBLISHED, "learner_id": "9"})

    assert gathered.practice_track == ""


# -- delivery ----------------------------------------------------------------


def test_a_successful_send_records_the_truth():
    messenger = FakeMessenger()

    result = send_lesson(messenger, STORE, recipient_id="U1", learner_id="1", published=PUBLISHED)

    assert result["sent"] is True
    assert messenger.sent[0][0] == "U1"


def test_dry_run_passes_the_gate_and_sends_nothing():
    messenger = FakeMessenger()

    result = send_lesson(
        messenger, STORE, recipient_id="U1", learner_id="1", published=PUBLISHED, dry_run=True
    )

    assert result["dry_run"] is True
    assert result["sent"] is False
    assert messenger.sent == []


def test_a_refused_delivery_raises_rather_than_returning_sent_false():
    """A platform answering 'no' must not be interpretable as success."""
    messenger = FakeMessenger(fail_with=UpstreamError("line refused", service="line"))

    with pytest.raises(UpstreamError):
        send_lesson(messenger, STORE, recipient_id="U1", learner_id="1", published=PUBLISHED)


# -- contact resolution ------------------------------------------------------


def test_a_partial_contact_name_does_not_resolve():
    from baton.adapters.chat.base import resolve_contact

    (Path("unused"),)
    contacts = {
        "contacts": {
            "office": {"id_env": "BATON_A", "aliases": ["boss"]},
            "office2": {"id_env": "BATON_B", "aliases": []},
        }
    }

    class FakeConfig:
        def section(self, _name):
            return contacts["contacts"]

    with pytest.raises(NeedsHumanError) as excinfo:
        resolve_contact(FakeConfig(), "off")  # type: ignore[arg-type]

    assert excinfo.value.exit_code == Exit.NEEDS_HUMAN


def test_an_alias_resolves_to_its_contact():
    from baton.adapters.chat.base import resolve_contact

    class FakeConfig:
        def section(self, _name):
            return {"office": {"id_env": "BATON_A", "aliases": ["boss", "manager"]}}

        def secret(self, _key, *, required=True):
            return "U123office"

    key, recipient_id = resolve_contact(FakeConfig(), "boss")  # type: ignore[arg-type]

    # The platform id, not the name of the variable holding it: what leaves
    # this function is what a driver puts in the payload.
    assert key == "office"
    assert recipient_id == "U123office"


def test_a_contact_whose_id_env_is_unset_is_a_config_problem():
    from baton.adapters.chat.base import resolve_contact
    from baton.errors import ConfigError

    class FakeConfig:
        def section(self, _name):
            return {"office": {"id_env": "BATON_A", "aliases": []}}

        def secret(self, key, *, required=True):
            raise ConfigError(
                "Environment variable BATON_A is not set.",
                remedy="Export BATON_A (or add it to your .env) and re-run.",
            )

    with pytest.raises(ConfigError) as excinfo:
        resolve_contact(FakeConfig(), "office")  # type: ignore[arg-type]

    assert "BATON_A" in str(excinfo.value)


def test_an_unset_id_env_does_not_leak_the_variable_name_into_a_payload(monkeypatch):
    """End to end for the dereference: a real Config, a real env var."""
    from baton.adapters.chat.base import resolve_contact
    from baton.core.config import Config

    config = Config(
        data={"chat": {"contacts": {"office": {"id_env": "BATON_OFFICE_ID", "aliases": []}}}},
        config_file=Path("baton.yaml"),
        profile_dir=Path("."),
    )

    monkeypatch.delenv("BATON_OFFICE_ID", raising=False)
    with pytest.raises(Exception) as unset:
        resolve_contact(config, "office")
    assert "BATON_OFFICE_ID" in str(unset.value)

    monkeypatch.setenv("BATON_OFFICE_ID", "U8800office")
    key, recipient_id = resolve_contact(config, "office")
    assert (key, recipient_id) == ("office", "U8800office")


def test_a_duplicate_alias_under_one_contact_is_one_match():
    """The same alias written twice is a typo in baton.yaml, not an ambiguity."""
    from baton.adapters.chat.base import resolve_contact

    class FakeConfig:
        def section(self, _name):
            return {"me": {"id_env": "BATON_A", "aliases": ["boss", "boss"]}}

        def secret(self, _key, *, required=True):
            return "U1"

    key, recipient_id = resolve_contact(FakeConfig(), "boss")  # type: ignore[arg-type]

    assert (key, recipient_id) == ("me", "U1")


def test_a_contacts_own_key_wins_over_another_contacts_alias():
    from baton.adapters.chat.base import resolve_contact

    class FakeConfig:
        def section(self, _name):
            return {
                "dad": {"id_env": "BATON_DAD", "aliases": ["mum"]},
                "mum": {"id_env": "BATON_MUM", "aliases": []},
            }

        def secret(self, key, *, required=True):
            return {"chat.contacts.dad.id_env": "Udad", "chat.contacts.mum.id_env": "Umum"}[key]

    key, recipient_id = resolve_contact(FakeConfig(), "mum")  # type: ignore[arg-type]

    assert (key, recipient_id) == ("mum", "Umum")


def test_a_contact_without_an_id_env_is_a_config_problem():
    from baton.adapters.chat.base import resolve_contact

    class FakeConfig:
        def section(self, _name):
            return {"office": {"aliases": ["boss"]}}

    with pytest.raises(NeedsHumanError) as excinfo:
        resolve_contact(FakeConfig(), "office")  # type: ignore[arg-type]

    assert "id_env" in str(excinfo.value)


# -- end to end ----------------------------------------------------------------


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

    docs = FakeDocStore(
        statuses={"doc-ada-03": DocStatus(doc_id="doc-ada-03", status="Complete")},
        blocks={
            "doc-ada-03": [Block(id="v", type="video", url="https://example.invalid/watch/ada-3")]
        },
    )
    monkeypatch.setattr("baton.cli.cmd_send.open_docs", lambda _config: docs)

    messenger = FakeMessenger()
    monkeypatch.setattr("baton.cli.cmd_send.open_chat", lambda _config: messenger)
    monkeypatch.setenv("BATON_TEACHER", "U-teacher")
    return profile, messenger, docs


def _relax_gate(profile) -> None:
    """Move the recording link to the optional list, as a studio with no
    recordings would."""
    config = profile / "baton.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + textwrap.dedent(
            """
            gates:
              send_lesson_required: [doc_link, short_summary, session_number]
              send_lesson_optional: [practice_track, video_link]
            """
        ),
        encoding="utf-8",
    )


def _set_waiver_recipient(profile, contact) -> None:
    """Name the studio's approver for no-recording sends, as a studio that
    has decided who answers that question would."""
    config = profile / "baton.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + textwrap.dedent(
            f"""
            summary:
              video_waiver:
                recipient: {contact}
            """
        ),
        encoding="utf-8",
    )


def publish(profile, learner_id, **overrides):
    """Write a published record directly, as `lesson publish` would have."""
    from datetime import datetime, timezone

    from baton.core import jsonio

    record = {
        **PUBLISHED,
        "learner_id": learner_id,
        "learner_name": overrides.pop("learner_name", "Ada Whitfield"),
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **overrides,
    }
    path = profile / "state" / "published" / f"{learner_id}-{record['session_number']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_json(path, record)
    return record


def call(studio, *args):
    profile = studio[0]
    return run(["--profile", str(profile), "--json", "send", *args])


def test_a_complete_publishment_sends_through_the_cli(studio, capsys):
    profile, messenger, _docs = studio
    publish(profile, "1")

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.OK
    payload = json.loads(capsys.readouterr().out)

    assert payload["sent"] is True
    assert messenger.sent[0][0] == "U-teacher"
    assert "https://example.invalid/watch/ada-3" in messenger.sent[0][1]


def test_a_published_recording_lesson_sends_the_record_message(studio, capsys):
    profile, messenger, _docs = studio
    work = {
        "id": "7",
        "learner_id": "1",
        "title": "Uptown Funk",
        "type": "cover",
        "video_link": "https://youtu.be/up-funk",
        "drive_link": "",
        "performed_date": "2026-08-20",
    }
    publish(
        profile,
        "1",
        kind="recording",
        work=work,
        summary=None,
        short_message="RECORD MESSAGE",
    )

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.OK
    payload = json.loads(capsys.readouterr().out)

    assert payload["kind"] == "recording"
    assert payload["session_number"] == 3
    assert payload["message"] == "RECORD MESSAGE"
    assert messenger.sent[0][1] == "RECORD MESSAGE"


def test_dry_run_blocks_before_sending_when_a_field_is_missing(studio, capsys):
    profile, messenger, _docs = studio
    publish(profile, "1", doc_url="")

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher", "--dry-run") == Exit.GATE
    payload = json.loads(capsys.readouterr().out)

    assert [m["field"] for m in payload["details"]["missing"]] == ["doc_link"]
    assert messenger.sent == []


def test_sending_for_an_unpublished_learner_is_a_usage_error(studio, capsys):
    assert call(studio, "lesson", "Bruno Castell", "--to", "teacher") == Exit.USAGE

    assert "publish" in json.loads(capsys.readouterr().out)["remedy"]


def test_batch_sends_every_complete_learner_and_reports_the_blocked_one(studio, capsys):
    profile, messenger, _docs = studio
    publish(profile, "1")
    publish(profile, "2", learner_name="Bruno Castell", doc_url="", doc_id="doc-b-1")

    code = call(
        studio,
        "batch",
        "--to",
        "teacher",
        "--learner",
        "Ada Whitfield",
        "--learner",
        "Bruno Castell",
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == Exit.GATE
    assert payload["sent"] == 1
    assert [b["learner"] for b in payload["blocked"]] == ["Bruno Castell"]
    assert len(messenger.sent) == 1


def test_a_batch_rejects_a_duplicated_learner(studio, capsys):
    assert (
        call(
            studio,
            "batch",
            "--to",
            "teacher",
            "--learner",
            "Ada Whitfield",
            "--learner",
            "Ada Whitfield",
        )
        == Exit.USAGE
    )

    payload = json.loads(capsys.readouterr().out)
    assert "Ada Whitfield" in payload["message"]


def test_a_blocked_batch_recording_keeps_its_kind_for_the_ledger(studio, capsys):
    profile, _messenger, _docs = studio
    publish(
        profile,
        "1",
        kind="recording",
        summary=None,
        doc_url="",
        short_message="RECORD MESSAGE",
        work={
            "id": "7",
            "learner_id": "1",
            "title": "Uptown Funk",
            "video_link": "https://youtu.be/up-funk",
        },
    )

    code = call(studio, "batch", "--to", "teacher", "--learner", "Ada Whitfield")
    payload = json.loads(capsys.readouterr().out)

    assert code == Exit.GATE
    assert payload["blocked"][0]["kind"] == "recording"


def test_a_batch_rejects_two_spellings_of_one_learner(studio, capsys):
    """ "Ada" is an alias of Ada Whitfield. The pair is not a duplicate as
    strings, which is exactly how it used to pass the check, send twice, and
    land as two LINE messages in one sitting."""
    config = studio[0] / "baton.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + textwrap.dedent(
            """
            db:
              aliases:
                Ada: Ada Whitfield
            """
        ),
        encoding="utf-8",
    )

    assert (
        call(
            studio,
            "batch",
            "--to",
            "teacher",
            "--learner",
            "Ada Whitfield",
            "--learner",
            "Ada",
        )
        == Exit.USAGE
    )

    payload = json.loads(capsys.readouterr().out)
    # The refusal names the person, not the spelling.
    assert "Ada Whitfield" in payload["message"]
    assert studio[1].sent == []


def test_an_unknown_contact_exits_needs_human(studio, capsys):
    profile = studio[0]
    publish(profile, "1")

    assert call(studio, "lesson", "Ada Whitfield", "--to", "stranger") == Exit.NEEDS_HUMAN

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "needs_human"
    assert "teacher" in [c["name"] for c in payload["details"]["candidates"]]


def test_a_document_outage_stops_the_send_and_asks_while_the_recording_is_required(studio, capsys):
    """The packaged gate requires the recording link, and an unreadable
    document means the link is *unknown*, which Baton does not resolve on its
    own, in either direction. The send stops on exit 3 and asks a person, the
    same place a genuinely unfilmed lesson stops."""
    from baton.errors import UpstreamError

    profile = studio[0]
    docs = studio[2]
    publish(profile, "1")
    docs.fail_with = UpstreamError("notion is down", service="notion")

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.NEEDS_HUMAN

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "needs_human"
    assert studio[1].sent == []


def test_a_studio_that_does_not_record_can_make_the_link_optional(studio, capsys):
    """Moving video_link to the optional list is the supported way out for a
    studio with no recordings: the send goes, warns, and leaves the recording
    line off the message."""
    from baton.errors import UpstreamError

    profile = studio[0]
    docs = studio[2]
    _relax_gate(profile)
    publish(profile, "1")
    docs.fail_with = UpstreamError("notion is down", service="notion")

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["sent"] is True
    assert "Recording:" not in payload["message"]
    assert {w["field"] for w in payload["warnings"]} >= {"video_link"}


# -- a lesson with no recording is a person's call --------------------------------


def test_a_session_with_no_recording_stops_and_asks_a_person(studio, capsys):
    """The gate requires the recording, but a lesson that was never filmed is
    a decision rather than a data gap: the send stops on exit 3 naming what is
    missing, and nothing goes out until a person answers."""
    profile, messenger, docs = studio
    publish(profile, "1")
    docs.blocks["doc-ada-03"] = []

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.NEEDS_HUMAN

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "needs_human"
    details = payload["details"]
    assert details["missing"] == ["video_link"]
    assert details["learner"] == "Ada Whitfield"
    assert messenger.sent == []


def test_the_stop_does_not_hand_a_working_bypass_to_whatever_is_reading(studio, capsys):
    """`candidates` is the machine-readable half of exit 3, and everywhere else
    it carries data to pick between: matched learners, configured contacts.
    A bare `--without-video` used to be a working bypass sitting right there
    in the contract; an agent following it faithfully would take it, which is
    the opposite of what stopping to ask a person is for.

    Nothing in this response is a secret to withhold: the remedy is allowed
    to name `send video-waiver` and `--without-video`, because neither one
    alone completes anything. What matters is checked below: no code has been
    requested, so no value passed to --without-video succeeds."""
    profile, messenger, docs = studio
    publish(profile, "1")
    docs.blocks["doc-ada-03"] = []

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.NEEDS_HUMAN

    payload = json.loads(capsys.readouterr().out)
    assert payload["details"]["candidates"] == []
    assert messenger.sent == []

    # No waiver was ever requested, so nothing --without-video is given works.
    guessed = call(
        studio, "lesson", "Ada Whitfield", "--to", "teacher", "--without-video", "000000"
    )
    assert guessed == Exit.NEEDS_HUMAN
    assert messenger.sent == []


def _request_waiver(studio, capsys, name="Ada Whitfield", **extra):
    """Ask for a code and hand back the exact string it sent: standing in for
    the person reading their own phone, since nothing else in the test can."""
    _profile, messenger, _docs = studio
    args = ["video-waiver", name, "--to", "teacher"]
    for key, value in extra.items():
        args += [f"--{key.replace('_', '-')}", str(value)]
    assert call(studio, *args) == Exit.OK
    capsys.readouterr()
    text = messenger.sent[-1][1]
    match = re.search(r"[A-Z2-9]{6}", text)
    assert match, f"no code-shaped token in: {text!r}"
    return match.group(0)


def test_video_waiver_texts_a_code_and_the_command_never_returns_it(studio, capsys):
    profile, messenger, docs = studio
    publish(profile, "1")
    docs.blocks["doc-ada-03"] = []

    assert call(studio, "video-waiver", "Ada Whitfield", "--to", "teacher") == Exit.OK

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["sent_to"] == "teacher"
    assert len(messenger.sent) == 1
    text = messenger.sent[0][1]
    match = re.search(r"[A-Z2-9]{6}", text)
    assert match
    # The command's own stdout must not carry the value it just had a
    # messenger deliver: that is the entire mechanism.
    assert match.group(0) not in out


def test_video_waiver_refuses_a_session_that_already_has_a_recording(studio, capsys):
    profile, _messenger, _docs = studio
    publish(profile, "1")  # the fixture's page carries a video block

    assert call(studio, "video-waiver", "Ada Whitfield", "--to", "teacher") == Exit.USAGE


def test_video_waiver_dry_run_previews_without_sending_or_writing_state(studio, capsys):
    profile, messenger, docs = studio
    publish(profile, "1")
    docs.blocks["doc-ada-03"] = []

    assert call(studio, "video-waiver", "Ada Whitfield", "--to", "teacher", "--dry-run") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert re.search(r"[A-Z2-9]{6}", payload["message"])
    assert messenger.sent == []

    # A dry run must not answer for the real request that comes later: the
    # previewed code is never live.
    from baton.core.video_waivers import VideoWaivers

    assert VideoWaivers.for_state(profile / "state")._load() == {}


def test_video_waiver_defaults_to_the_configured_recipient(studio, capsys):
    """Without `--to`, the code goes to the contact the profile names, and
    the result says who that was: the studio decides once in baton.yaml who
    answers the no-recording question, rather than leaving that choice to
    whatever is holding the command line."""
    profile, messenger, docs = studio
    publish(profile, "1")
    docs.blocks["doc-ada-03"] = []
    _set_waiver_recipient(profile, "me")

    assert call(studio, "video-waiver", "Ada Whitfield") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["sent_to"] == "me"
    assert messenger.sent[0][0] == "U-teacher"


def test_an_explicit_to_wins_over_the_configured_default(studio, capsys):
    profile, _messenger, docs = studio
    publish(profile, "1")
    docs.blocks["doc-ada-03"] = []
    _set_waiver_recipient(profile, "me")

    assert call(studio, "video-waiver", "Ada Whitfield", "--to", "teacher") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["sent_to"] == "teacher"


def test_no_recipient_configured_or_passed_is_a_config_error(studio, capsys):
    """A studio that has never named an approver gets exit 2 and an
    instruction to name one, not a silent choice made by the command."""
    profile, messenger, docs = studio
    publish(profile, "1")
    docs.blocks["doc-ada-03"] = []

    assert call(studio, "video-waiver", "Ada Whitfield") == Exit.CONFIG

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "config"
    assert messenger.sent == []


def test_the_no_recording_stop_no_longer_echoes_the_lesson_recipient(studio, capsys):
    """The remedy is machine-read by whatever drives Baton. It used to end
    with `--to {the lesson's own recipient}`, and a following agent texted
    the code to exactly that person: someone who could have answered the
    owner's question without the owner ever seeing it. With an approver
    configured, the remedy needs no `--to` at all."""
    profile, _messenger, docs = studio
    publish(profile, "1")
    docs.blocks["doc-ada-03"] = []
    _set_waiver_recipient(profile, "me")

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.NEEDS_HUMAN

    payload = json.loads(capsys.readouterr().out)
    assert "--to" not in payload["remedy"]
    assert payload["remedy"].count("video-waiver") == 1


def test_the_no_recording_stop_names_whose_call_it_is_when_nobody_is_configured(studio, capsys):
    profile, _messenger, docs = studio
    publish(profile, "1")
    docs.blocks["doc-ada-03"] = []

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.NEEDS_HUMAN

    payload = json.loads(capsys.readouterr().out)
    assert "teacher" not in payload["remedy"]
    assert "answers for this studio" in payload["remedy"]


def test_a_refused_code_points_back_at_the_configured_approver(studio, capsys):
    """A wrong or expired code refuses through the store's own remedy, which
    must steer the retry at the same approver the original request reached."""
    profile, messenger, docs = studio
    publish(profile, "1")
    docs.blocks["doc-ada-03"] = []
    _set_waiver_recipient(profile, "me")
    _request_waiver(studio, capsys)
    capsys.readouterr()

    refused = call(
        studio, "lesson", "Ada Whitfield", "--to", "teacher", "--without-video", "WRONG1"
    )
    assert refused == Exit.NEEDS_HUMAN

    payload = json.loads(capsys.readouterr().out)
    assert "--to me" in payload["remedy"]
    assert len(messenger.sent) == 1  # the waiver text only; the refusal sent nothing


def test_without_video_sends_with_no_video_section_after_a_waiver_is_answered(studio, capsys):
    """The code is the person's confirmed answer, applied the same way a
    studio relaxes its own gate: the field moves to optional for this one
    send, so the message leaves the video line off, the doc link stays, and
    the result still warns about what is missing."""
    profile, messenger, docs = studio
    publish(profile, "1")
    docs.blocks["doc-ada-03"] = []
    code = _request_waiver(studio, capsys)

    assert (
        call(studio, "lesson", "Ada Whitfield", "--to", "teacher", "--without-video", code)
        == Exit.OK
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["sent"] is True
    assert "เฉพาะ Video:" not in payload["message"]
    assert "https://example.invalid/lesson-3" in payload["message"]
    assert {w["field"] for w in payload["warnings"]} >= {"video_link"}
    assert "เฉพาะ Video:" not in messenger.sent[-1][1]


def test_a_wrong_code_is_refused_and_leaves_the_real_one_live(studio, capsys):
    profile, messenger, docs = studio
    publish(profile, "1")
    docs.blocks["doc-ada-03"] = []
    code = _request_waiver(studio, capsys)

    wrong = "999999" if code != "999999" else "888888"
    assert (
        call(studio, "lesson", "Ada Whitfield", "--to", "teacher", "--without-video", wrong)
        == Exit.NEEDS_HUMAN
    )
    assert len(messenger.sent) == 1  # only the original waiver text; the wrong guess sent nothing

    # The real code still works: a wrong guess did not burn it.
    assert (
        call(studio, "lesson", "Ada Whitfield", "--to", "teacher", "--without-video", code)
        == Exit.OK
    )


def test_a_waiver_code_is_single_use(studio, capsys):
    profile, _messenger, docs = studio
    publish(profile, "1")
    docs.blocks["doc-ada-03"] = []
    code = _request_waiver(studio, capsys)

    assert (
        call(studio, "lesson", "Ada Whitfield", "--to", "teacher", "--without-video", code)
        == Exit.OK
    )
    capsys.readouterr()

    replay = call(studio, "lesson", "Ada Whitfield", "--to", "teacher", "--without-video", code)
    assert replay == Exit.NEEDS_HUMAN


def test_a_waiver_answers_only_the_session_it_was_requested_for(studio, capsys):
    """Bruno's code must not open Ada's session, even to the same contact."""
    profile, _messenger, docs = studio
    publish(profile, "1")
    publish(profile, "2", learner_name="Bruno Castell", doc_id="doc-b-1")
    docs.blocks["doc-ada-03"] = []
    docs.blocks["doc-b-1"] = []
    bruno_code = _request_waiver(studio, capsys, name="Bruno Castell")

    assert (
        call(studio, "lesson", "Ada Whitfield", "--to", "teacher", "--without-video", bruno_code)
        == Exit.NEEDS_HUMAN
    )


def test_without_video_leaves_a_session_that_has_a_recording_alone(studio, capsys):
    """A code answers "there is no recording"; it is not an order to strip
    one that exists, and the gate never even looks at it when the video is
    already there."""
    profile, messenger, _docs = studio
    publish(profile, "1")  # the fixture's page carries a video block

    assert (
        call(studio, "lesson", "Ada Whitfield", "--to", "teacher", "--without-video", "anything")
        == Exit.OK
    )

    capsys.readouterr()
    assert "https://example.invalid/watch/ada-3" in messenger.sent[0][1]


def test_a_batch_reports_the_no_video_learner_as_waiting_on_a_person(studio, capsys):
    """One refusal must not abandon the rest of the batch, and the blocked
    entry must say which *kind* of stop it was, so whoever reads the report
    relays a question rather than hunting for a video block that was never
    filmed."""
    profile, _messenger, docs = studio
    publish(profile, "1")
    publish(profile, "2", learner_name="Bruno Castell", doc_id="doc-b-1")
    docs.blocks["doc-b-1"] = []

    code = call(
        studio,
        "batch",
        "--to",
        "teacher",
        "--learner",
        "Ada Whitfield",
        "--learner",
        "Bruno Castell",
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == Exit.GATE
    assert payload["sent"] == 1
    assert [b["learner"] for b in payload["blocked"]] == ["Bruno Castell"]
    assert payload["blocked"][0]["error"]["error"] == "needs_human"


def test_batch_has_no_blanket_bypass(studio, capsys):
    """A waiver code answers one learner's one session; a batch has many of
    both, so there is no single value `--without-video` could take that would
    mean "yes" for all of them. The flag is not offered on this subcommand at
    all: a person confirms Bruno's session on its own, the same as any exit-3
    stop, and re-sends him alone once they have."""
    code = call(
        studio,
        "batch",
        "--to",
        "teacher",
        "--without-video",
        "whatever",
        "--learner",
        "Ada Whitfield",
    )

    assert code == Exit.USAGE


def test_a_batch_learner_confirmed_separately_can_be_sent_alone_after(studio, capsys):
    profile, _messenger, docs = studio
    publish(profile, "1")
    publish(profile, "2", learner_name="Bruno Castell", doc_id="doc-b-1")
    docs.blocks["doc-b-1"] = []

    batch_code = call(
        studio,
        "batch",
        "--to",
        "teacher",
        "--learner",
        "Ada Whitfield",
        "--learner",
        "Bruno Castell",
    )
    payload = json.loads(capsys.readouterr().out)
    assert batch_code == Exit.GATE
    assert payload["sent"] == 1
    assert [b["learner"] for b in payload["blocked"]] == ["Bruno Castell"]

    waiver_code = _request_waiver(studio, capsys, name="Bruno Castell")
    assert (
        call(studio, "lesson", "Bruno Castell", "--to", "teacher", "--without-video", waiver_code)
        == Exit.OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert "เฉพาะ Video:" not in payload["message"]


def test_contacts_lists_what_is_configured(studio, capsys):
    assert call(studio, "contacts") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["contacts"][0]["name"] == "teacher"
    assert "me" in payload["contacts"][0]["aliases"]


# -- the webhook driver --------------------------------------------------------


class _Captured:
    """Stands in for http_request, recording what the driver would send."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = ""
        self.kwargs: dict | None = None

    def __call__(self, _method, _url, **kwargs):
        self.kwargs = kwargs
        return self


def test_a_signed_webhook_sends_the_bytes_it_signed(monkeypatch):
    """The signature must be over the exact wire bytes, not a re-serialisation:
    separators and escaping differ between any two serialisations, so signing
    one and sending another verifies nowhere."""
    import hashlib
    import hmac as hmac_mod

    from baton.adapters.chat import drivers

    captured = _Captured()
    monkeypatch.setattr(drivers, "http_request", captured)

    messenger = drivers.WebhookMessenger(
        "https://example.invalid/hook", secret="s3cret", config=None
    )
    messenger.send("ทดสอบ", "สวัสดี")

    assert captured.kwargs is not None
    sent = captured.kwargs["data"]
    assert "json" not in captured.kwargs  # one serialisation, not two
    assert captured.kwargs["headers"]["Content-Type"] == "application/json"

    expected = hmac_mod.new(b"s3cret", sent, hashlib.sha256).hexdigest()
    assert captured.kwargs["headers"]["X-Baton-Signature"] == expected

    import json as json_mod

    assert json_mod.loads(sent) == {"recipient": "ทดสอบ", "text": "สวัสดี"}


def test_an_unsigned_webhook_lets_the_http_layer_serialise(monkeypatch):
    from baton.adapters.chat import drivers

    captured = _Captured()
    monkeypatch.setattr(drivers, "http_request", captured)

    messenger = drivers.WebhookMessenger("https://example.invalid/hook", config=None)
    messenger.send("teacher", "hello")

    assert captured.kwargs["json"] == {"recipient": "teacher", "text": "hello"}
    assert "X-Baton-Signature" not in captured.kwargs["headers"]


def test_a_refused_webhook_reports_the_status(monkeypatch):
    from baton.adapters.chat import drivers

    captured = _Captured(status_code=500)
    monkeypatch.setattr(drivers, "http_request", captured)
    messenger = drivers.WebhookMessenger("https://example.invalid/hook", secret="s", config=None)

    with pytest.raises(UpstreamError) as excinfo:
        messenger.send("teacher", "hello")
    assert "HTTP 500" in str(excinfo.value)


def test_a_connection_error_never_prints_a_bot_token():
    """A Telegram token rides in the URL path; requests embeds the URL in its
    connection errors. One failed connection must not write the credential
    into an error message that ends up on stderr and in job logs."""
    import socket

    from baton.core.retry import http_request, redact

    # A closed port on localhost: refused instantly, no external service touched.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    with pytest.raises(UpstreamError) as excinfo:
        http_request(
            "POST",
            f"https://127.0.0.1:{port}/bot123456:SECRETTOKEN/sendMessage",
            service="telegram",
            timeout=0.2,
            attempts=1,
        )

    assert "SECRETTOKEN" not in str(excinfo.value)
    assert "/bot***/sendMessage" in str(excinfo.value)
    assert redact("https://x.invalid/bot1:abc?token=zzz&api_key=qqq") == (
        "https://x.invalid/bot***?token=***&api_key=***"
    )


# -- the practice track belongs to the lesson, not to today (#29) ----------------

MOVED_ON = FakeLearnerStore(
    learners=[Learner(id="1", name="Ada Whitfield", instrument="guitar", current_piece_id="9")],
    pieces=[
        Piece(id="2", title="Blackbird", practice_track="https://example.invalid/track.mp3"),
        Piece(id="9", title="Michelle", practice_track="https://example.invalid/newer.mp3"),
    ],
)

TAUGHT = Piece(id="2", title="Blackbird", practice_track="https://example.invalid/track.mp3")


def _published_with(snapshot: PieceSnapshot) -> dict:
    return {**PUBLISHED, "piece_snapshot": snapshot.to_dict()}


def test_the_snapshot_wins_over_the_learners_newer_piece():
    """The drift this exists to stop: published Monday, sent Friday, after the
    learner moved to another song."""
    gathered = gather_context(MOVED_ON, "1", _published_with(PieceSnapshot.capture(TAUGHT)))

    assert gathered.practice_track == "https://example.invalid/track.mp3"


def test_a_snapshot_of_no_piece_sends_no_track_even_when_one_exists_now():
    """`none` is information, not a gap: the lesson was taught without a piece
    assigned, so there is no track that belongs to it."""
    gathered = gather_context(MOVED_ON, "1", _published_with(PieceSnapshot.capture(None)))

    assert gathered.practice_track == ""


def test_a_record_written_before_snapshots_still_falls_back_to_the_live_piece():
    """Records published by an older Baton carry no snapshot at all. A stale
    track is closer to the truth than dropping the line entirely."""
    assert "piece_snapshot" not in PUBLISHED

    gathered = gather_context(MOVED_ON, "1", PUBLISHED)

    assert gathered.practice_track == "https://example.invalid/newer.mp3"


def test_a_snapshot_whose_piece_lost_its_track_is_not_backfilled_from_today():
    """A captured piece with an empty track means the piece had none then:
    reaching for the current one would reintroduce the drift by the back door."""
    trackless = Piece(id="2", title="Blackbird", practice_track="")

    gathered = gather_context(MOVED_ON, "1", _published_with(PieceSnapshot.capture(trackless)))

    assert gathered.practice_track == ""


# -- the song being learnt is not the lesson's recording -------------------------

SONG = "https://www.youtube.com/watch?v=aSongVideo1"
RECORDING = "https://youtu.be/aLessonRec1"


def _teach(profile, source_link: str, *, piece_id: int = 2) -> None:
    """Give the piece a source link on a video host, as a pop song has."""
    connection = sqlite3.connect(profile / "data" / "studio.db")
    connection.execute("UPDATE pieces SET source_link = ? WHERE id = ?", (source_link, piece_id))
    connection.commit()
    connection.close()


def test_the_song_on_the_page_is_not_sent_as_the_recording(studio, capsys):
    """What production did: the page held the song the lesson worked on and no
    recording yet, and the message went out carrying the link to the song's
    official music video. The gate did not stop it because a link *was* found.

    The send must be refused instead: the recording is genuinely missing, and
    whether the lesson goes out without one is a person's call to make.
    """
    profile, messenger, docs = studio
    _teach(profile, SONG)
    docs.blocks["doc-ada-03"] = [
        Block(id="head", type="heading_2", text="🎵 Die With a Smile"),
        Block(id="bm", type="bookmark", url=SONG),
        Block(id="em", type="embed", url=SONG),
    ]
    publish(profile, "1")

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.NEEDS_HUMAN

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "needs_human"
    assert messenger.sent == []


def test_the_recording_goes_out_when_it_sits_beside_the_song(studio, capsys):
    profile, messenger, docs = studio
    _teach(profile, SONG)
    docs.blocks["doc-ada-03"] = [
        Block(id="recording", type="video", url=RECORDING),
        Block(id="bm", type="bookmark", url=SONG),
        Block(id="em", type="embed", url=SONG),
    ]
    publish(profile, "1")

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.OK

    capsys.readouterr()
    assert RECORDING in messenger.sent[0][1]
    assert SONG not in messenger.sent[0][1]


def test_the_song_taught_that_lesson_is_excluded_after_the_learner_moves_on(studio, capsys):
    """The song can be changed after the lesson was published: that is what
    happened the night this was found. The page then holds the new source,
    while the record holds the old one, and both are the piece rather than the
    recording."""
    profile, messenger, docs = studio
    old_song = "https://youtu.be/oldsong0000"
    _teach(profile, SONG)  # the learner is on the new song now
    docs.blocks["doc-ada-03"] = [Block(id="em", type="embed", url=old_song)]
    publish(
        profile,
        "1",
        piece_snapshot=PieceSnapshot.capture(
            Piece(id="2", title="Die With a Smile", source_link=old_song)
        ).to_dict(),
    )

    assert call(studio, "lesson", "Ada Whitfield", "--to", "teacher") == Exit.NEEDS_HUMAN

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "needs_human"
    assert messenger.sent == []


def test_send_video_carries_the_recording_rather_than_the_song(studio, capsys):
    """`send video` reads the page the same way and had the same bug."""
    profile, messenger, docs = studio
    _teach(profile, SONG)
    docs.blocks["doc-ada-03"] = [
        Block(id="recording", type="video", url=RECORDING),
        Block(id="em", type="embed", url=SONG),
    ]
    publish(profile, "1")

    assert call(studio, "video", "Ada Whitfield", "--to", "teacher") == Exit.OK

    capsys.readouterr()
    assert RECORDING in messenger.sent[0][1]
    assert SONG not in messenger.sent[0][1]
