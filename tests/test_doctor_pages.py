"""Doctor proves the work can run, not only that the credentials are valid.

On the flood day doctor went green on a token that worked while a whole
branch of session pages answered 404, and the error that finally surfaced
guessed at an outage. These tests hold doctor to reading the pages work will
read, and to telling "not shared" apart from "not answered".
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path

import pytest

import baton
from baton.adapters.db.postgrest import PostgrestStore, url_problem
from baton.adapters.fakes import FakeDocStore
from baton.cli import cmd_doctor
from baton.cli.app import run
from baton.core.config import load as load_config
from baton.errors import ConfigError, UpstreamError
from baton.exits import Exit

MIGRATIONS = Path(baton.__file__).resolve().parent / "migrations"


class _PickyDocs(FakeDocStore):
    """A document store that cannot see some pages and cannot reach others."""

    def __init__(self, unseen=(), unanswered=()):
        super().__init__()
        self.unseen = set(unseen)
        self.unanswered = set(unanswered)
        self.read: list[str] = []

    def get_status(self, doc_id, *, with_blocks=True):
        self.read.append(doc_id)
        if doc_id in self.unseen:
            raise ConfigError(
                f"Notion cannot see page `{doc_id}` (404).",
                details={"page_id": doc_id, "status_code": 404},
            )
        if doc_id in self.unanswered:
            raise UpstreamError("Notion rejected the request: 503", service="notion", status=503)
        return super().get_status(doc_id, with_blocks=with_blocks)


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
            locale: en
            timezone: Asia/Bangkok
            db:
              driver: sqlite
              sqlite:
                path: data/studio.db
            docs:
              driver: notion
              properties:
                status: Status
            chat:
              driver: webhook
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NOTION_API_TOKEN", "t")
    monkeypatch.setenv("BATON_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setattr(cmd_doctor, "PAGE_READ_PACING", 0)
    return profile


def doctor(profile, monkeypatch, capsys, docs):
    monkeypatch.setattr("baton.adapters.docs.open_docs", lambda _config: docs)
    code = run(["--profile", str(profile), "--json", "doctor"])
    return code, json.loads(capsys.readouterr().out)


def page_checks(payload):
    return [c for c in payload["checks"] if "page" in c["name"].lower()]


def test_doctor_reads_each_active_learners_latest_page(studio, monkeypatch, capsys):
    docs = _PickyDocs()

    code, payload = doctor(studio, monkeypatch, capsys, docs)

    assert code == Exit.OK
    # Latest session only: Ada has three pages and one is read.
    assert "doc-ada-03" in docs.read and "doc-ada-01" not in docs.read
    assert page_checks(payload)[0]["passed"] is True


def test_a_page_the_integration_cannot_see_is_named_as_sharing(studio, monkeypatch, capsys):
    docs = _PickyDocs(unseen={"doc-bruno-02"})

    code, payload = doctor(studio, monkeypatch, capsys, docs)

    assert code == Exit.CONFIG
    failed = [c for c in page_checks(payload) if not c["passed"]]
    assert len(failed) == 1
    assert "Bruno Castell" in failed[0]["detail"]
    assert "not an outage" in failed[0]["remedy"]
    assert "Connections" in failed[0]["remedy"]


def test_an_unanswered_page_is_reported_apart_from_sharing(studio, monkeypatch, capsys):
    docs = _PickyDocs(unseen={"doc-bruno-02"}, unanswered={"doc-ada-03"})

    code, payload = doctor(studio, monkeypatch, capsys, docs)

    assert code == Exit.CONFIG
    failed = {c["name"]: c for c in payload["checks"] if not c["passed"]}
    assert "Notion answered every page read" in failed
    upstream = failed["Notion answered every page read"]
    assert "Ada Whitfield" in upstream["detail"]
    assert "not sharing" in upstream["remedy"]


def test_offline_reads_no_pages(studio, monkeypatch, capsys):
    docs = _PickyDocs(unseen={"doc-bruno-02"})
    monkeypatch.setattr("baton.adapters.docs.open_docs", lambda _config: docs)

    code = run(["--profile", str(studio), "--json", "doctor", "--offline"])

    assert code == Exit.OK
    assert docs.read == []


# -- the two drivers that look alike -------------------------------------------


@pytest.mark.parametrize(
    ("driver", "url", "wrong"),
    [
        ("postgrest", "https://abcd.supabase.co", True),
        ("postgrest", "https://abcd.supabase.co/rest/v1", True),
        ("postgrest", "https://db.example.invalid", False),
        ("supabase", "https://abcd.supabase.co", False),
        ("supabase", "https://abcd.supabase.co/rest/v1", False),
    ],
)
def test_a_supabase_url_on_the_postgrest_driver_is_named(driver, url, wrong):
    assert (url_problem(driver, url) is not None) is wrong


def test_the_supabase_driver_does_not_double_the_rest_path(profile, monkeypatch):
    """The path ended up as /rest/v1/rest/v1, and the gateway's answer to that
    pointed at anything but the URL."""
    monkeypatch.setenv("SUPABASE_PROJECT_URL", "https://abcd.supabase.co/rest/v1/")
    monkeypatch.setenv("SUPABASE_PROJECT_API", "k")

    store = PostgrestStore.from_supabase_config(load_config(profile))

    assert store.base_url == "https://abcd.supabase.co/rest/v1"


def test_doctor_names_the_wrong_driver_before_any_request(profile, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_API_TOKEN", "t")
    monkeypatch.setenv("BATON_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setenv("BATON__DB__DRIVER", "postgrest")
    monkeypatch.setenv("POSTGREST_URL", "https://abcd.supabase.co")
    monkeypatch.setenv("POSTGREST_JWT", "j")

    code = run(["--profile", str(profile), "--json", "doctor", "--offline"])

    assert code == Exit.CONFIG
    payload = json.loads(capsys.readouterr().out)
    failed = [c for c in payload["checks"] if not c["passed"]]
    assert [c["name"] for c in failed] == ["Database URL suits the selected driver"]
    assert "db.driver: supabase" in failed[0]["remedy"]
