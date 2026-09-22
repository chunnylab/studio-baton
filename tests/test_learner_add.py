"""`baton learner add`: enrolment and its session pages, end to end.

Two harnesses: real SQLite (via the shipped migration) for the write path
itself, and a monkeypatched `FakeLearnerStore` for the config-driven refusals
(instrument/tone lists, unmapped extra fields) where the interesting behaviour
is entirely in the CLI layer, not the database.
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path

import pytest
import yaml

import baton
from baton.adapters.fakes import FakeLearnerStore
from baton.cli.app import run
from baton.exits import Exit

MIGRATIONS = Path(baton.__file__).resolve().parent / "migrations"

ADA_URL = "https://myworkspace.notion.site/1-16cf38e8e88b830f8167819ac35a6428"
BRUNO_URL = "https://myworkspace.notion.site/2-27df49f9f99c941f9278920bd46b7539"
DB_URL = "https://myworkspace.notion.site/38ff59f9f99c941f9278920bd46b7540"


@pytest.fixture
def studio(profile):
    """A real, empty (unseeded) SQLite database: enrolment is the point."""
    db_path = profile / "data" / "studio.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.executescript((MIGRATIONS / "sqlite.sql").read_text(encoding="utf-8"))
    connection.close()

    (profile / "baton.yaml").write_text(
        textwrap.dedent(
            """
            version: 1
            db:
              driver: sqlite
              sqlite:
                path: data/studio.db
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return profile


def call(profile, *args):
    return run(["--profile", str(profile), "--json", "learner", "add", *args])


def list_learners(profile, capsys):
    run(["--profile", str(profile), "--json", "learner", "list"])
    return json.loads(capsys.readouterr().out)


def test_add_creates_a_learner(studio, capsys):
    assert call(studio, "Elin Frost", "--instrument", "violin", "--tone", "child") == Exit.OK
    payload = json.loads(capsys.readouterr().out)

    assert payload["learner"]["name"] == "Elin Frost"
    assert payload["learner"]["instrument"] == "violin"
    assert payload["learner"]["tone"] == "child"
    assert payload["sessions"] == []

    listed = list_learners(studio, capsys)
    assert [item["name"] for item in listed["learners"]] == ["Elin Frost"]


def test_add_refuses_an_exact_duplicate(studio, capsys):
    assert call(studio, "Elin Frost", "--instrument", "violin") == Exit.OK
    capsys.readouterr()

    assert call(studio, "Elin Frost", "--instrument", "violin") == Exit.GATE
    payload = json.loads(capsys.readouterr().out)
    assert "already recorded" in payload["message"]

    listed_after = list_learners(studio, capsys)
    assert len(listed_after["learners"]) == 1


def test_add_names_similarly_spelled_learners_without_blocking(studio, capsys):
    assert call(studio, "Elin Frost", "--instrument", "violin") == Exit.OK
    capsys.readouterr()

    assert call(studio, "Elin Frostberg", "--instrument", "violin") == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["similar"] == ["Elin Frost"]


def test_add_dry_run_writes_nothing(studio, capsys):
    assert (
        call(studio, "Elin Frost", "--instrument", "violin", "--dry-run", "--page-urls", ADA_URL)
        == Exit.OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["would_add"]["name"] == "Elin Frost"
    assert payload["would_add_sessions"] == [
        {"number": 1, "doc_id": "16cf38e8-e88b-830f-8167-819ac35a6428"}
    ]

    assert list_learners(studio, capsys)["learners"] == []


def test_add_with_page_urls_writes_sessions_numbered_from_their_slugs(studio, capsys):
    assert (
        call(studio, "Elin Frost", "--instrument", "violin", "--page-urls", ADA_URL, BRUNO_URL)
        == Exit.OK
    )
    payload = json.loads(capsys.readouterr().out)

    numbers = [session["number"] for session in payload["sessions"]]
    assert numbers == [1, 2]


def test_add_with_pages_flag_uses_the_named_week(studio, capsys):
    assert (
        call(
            studio,
            "Elin Frost",
            "--instrument",
            "violin",
            "--pages",
            f"W5-{ADA_URL}",
        )
        == Exit.OK
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["sessions"][0]["number"] == 5


def test_add_an_unparseable_page_url_is_refused(studio, capsys):
    assert (
        call(studio, "Elin Frost", "--instrument", "violin", "--page-urls", "not-a-notion-url")
        == Exit.USAGE
    )
    payload = json.loads(capsys.readouterr().out)
    assert "page id" in payload["message"]

    assert list_learners(studio, capsys)["learners"] == []


def test_add_a_malformed_pages_token_is_refused(studio, capsys):
    assert (
        call(studio, "Elin Frost", "--instrument", "violin", "--pages", "not-w-dash-url")
        == Exit.USAGE
    )
    payload = json.loads(capsys.readouterr().out)
    assert "W<week>-<url>" in payload["message"]


# -- config-driven refusals, over a fake store --------------------------------


@pytest.fixture
def fake_studio(profile, monkeypatch):
    fake = FakeLearnerStore()
    monkeypatch.setattr("baton.adapters.db.open_store", lambda _config: fake)
    return profile, fake


def _deep_merge(base: dict, overlay: dict) -> dict:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _write_config(profile, overlay: dict) -> None:
    """Write baton.yaml as `overlay` merged onto the fake store's baseline."""
    config = _deep_merge({"version": 1, "db": {"driver": "sqlite"}}, overlay)
    (profile / "baton.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def test_an_unconfigured_instrument_is_refused(fake_studio, capsys):
    profile, _ = fake_studio
    _write_config(profile, {"learner": {"instruments": ["guitar", "drums"]}})

    assert call(profile, "New Person", "--instrument", "violin") == Exit.USAGE
    payload = json.loads(capsys.readouterr().out)
    assert "guitar" in payload["remedy"]


def test_a_configured_instrument_is_accepted(fake_studio, capsys):
    profile, fake = fake_studio
    _write_config(profile, {"learner": {"instruments": ["guitar", "drums"]}})

    assert call(profile, "New Person", "--instrument", "guitar") == Exit.OK
    assert fake.learners[0].instrument == "guitar"


def test_an_unconfigured_tone_is_refused(fake_studio, capsys):
    profile, _ = fake_studio
    _write_config(profile, {"learner": {"tones": ["standard", "child"]}})

    assert call(profile, "New Person", "--instrument", "guitar", "--tone", "casual") == Exit.USAGE


def test_an_unmapped_extra_field_is_refused_before_any_write(fake_studio, capsys):
    profile, fake = fake_studio

    assert (
        call(profile, "New Person", "--instrument", "guitar", "--prompt-level", "2") == Exit.CONFIG
    )
    payload = json.loads(capsys.readouterr().out)
    assert "prompt_level" in payload["message"]
    assert fake.learners == []


def test_a_mapped_extra_field_is_written(fake_studio, capsys):
    profile, fake = fake_studio
    _write_config(profile, {"db": {"fields": {"learner": {"prompt_level": "prompt_level"}}}})

    assert call(profile, "New Person", "--instrument", "guitar", "--prompt-level", "2") == Exit.OK
    assert fake.learners[0].raw == {"prompt_level": 2}


def test_pages_are_refused_without_db_link_when_the_profile_requires_it(fake_studio, capsys):
    profile, fake = fake_studio
    _write_config(profile, {"db": {"fields": {"session": {"database_id": "database_id"}}}})

    assert (
        call(profile, "New Person", "--instrument", "guitar", "--page-urls", ADA_URL) == Exit.USAGE
    )
    payload = json.loads(capsys.readouterr().out)
    assert "--db-link" in payload["message"]
    assert fake.learners == []


def test_a_session_write_failure_reports_what_already_landed(fake_studio, capsys):
    profile, fake = fake_studio
    from baton.errors import UpstreamError

    real_add_session = fake.add_session
    calls = {"n": 0}

    def flaky_add_session(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise UpstreamError("simulated outage", service="sqlite")
        return real_add_session(*args, **kwargs)

    fake.add_session = flaky_add_session  # type: ignore[method-assign]

    assert (
        call(
            profile,
            "New Person",
            "--instrument",
            "guitar",
            "--page-urls",
            ADA_URL,
            BRUNO_URL,
        )
        == Exit.UPSTREAM
    )
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["details"]["sessions_written"]) == 1
    assert payload["details"]["learner"]["name"] == "New Person"
    # The learner itself was enrolled, no rollback across stores.
    assert fake.learners[0].name == "New Person"
