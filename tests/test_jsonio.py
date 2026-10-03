"""Crash-safety guarantees of the state layer.

These are the tests that matter most in the whole suite: every resumable
pipeline trusts that a half-written state file is impossible, and that a
corrupt one degrades to the backup instead of taking the run down.
"""

from __future__ import annotations

import json

from baton.core import jsonio


def test_round_trip_preserves_non_ascii(tmp_path):
    target = tmp_path / "state.json"
    payload = {"learner": "น้องมานี", "week": 3, "titles": ["Enemy"]}

    jsonio.write_json(target, payload)

    assert jsonio.read_json(target) == payload
    # Thai must survive as characters, not \u escapes: the file is read by
    # people as often as by code.
    assert "น้องมานี" in target.read_text(encoding="utf-8")


def test_write_creates_parent_directories(tmp_path):
    target = tmp_path / "deep" / "nested" / "state.json"

    jsonio.write_json(target, {"ok": True})

    assert jsonio.read_json(target) == {"ok": True}


def test_second_write_snapshots_the_previous_file(tmp_path):
    target = tmp_path / "state.json"

    jsonio.write_json(target, {"generation": 1})
    jsonio.write_json(target, {"generation": 2})

    assert jsonio.read_json(target) == {"generation": 2}
    assert json.loads(jsonio.backup_path(target).read_text(encoding="utf-8")) == {"generation": 1}


def test_corrupt_file_falls_back_to_backup(tmp_path):
    target = tmp_path / "state.json"
    jsonio.write_json(target, {"generation": 1})
    jsonio.write_json(target, {"generation": 2})

    # Simulate a truncated write from a power cut.
    target.write_text('{"generation": 2', encoding="utf-8")

    assert jsonio.read_json(target) == {"generation": 1}


def test_corrupt_file_without_backup_returns_default(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("not json at all", encoding="utf-8")

    assert jsonio.read_json(target, default={"fresh": True}) == {"fresh": True}


def test_missing_file_returns_default(tmp_path):
    assert jsonio.read_json(tmp_path / "absent.json", default=[]) == []


def test_no_temp_file_survives_a_successful_write(tmp_path):
    target = tmp_path / "state.json"

    jsonio.write_json(target, {"ok": True})

    leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_losing_both_copies_is_said_out_loud(tmp_path, capsys):
    """Returning the default is right; doing it silently is not.

    A corrupt draft and a draft that was never written both used to read as
    "nothing here", so a lesson someone had typed up could disappear with
    nobody learning that it had.
    """
    target = tmp_path / "draft.json"
    jsonio.write_json(target, {"lesson": "typed up by a human"})
    jsonio.write_json(target, {"lesson": "second version"})
    target.write_text("{not json", encoding="utf-8")
    jsonio.backup_path(target).write_text("{also not json", encoding="utf-8")

    assert jsonio.read_json(target, default={}) == {}

    assert "could not be read" in capsys.readouterr().err
    kept = list(tmp_path.glob("draft.json.corrupt-*"))
    assert len(kept) == 1
    assert kept[0].read_text(encoding="utf-8") == "{not json"


def test_a_missing_file_stays_quiet(tmp_path, capsys):
    """Only a file that existed and could not be read is worth a warning."""
    assert jsonio.read_json(tmp_path / "never-written.json", default={}) == {}

    assert capsys.readouterr().err == ""


def test_the_backup_is_written_atomically(tmp_path):
    """The backup is the only recovery source, so a crash during the copy
    must not be able to truncate it."""
    target = tmp_path / "state.json"
    jsonio.write_json(target, {"generation": 1})
    jsonio.write_json(target, {"generation": 2})

    assert json.loads(jsonio.backup_path(target).read_text(encoding="utf-8")) == {"generation": 1}
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".bak.tmp")] == []
