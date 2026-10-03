"""Learner records in a local SQLite file.

The default driver, and the reason Baton is usable within a couple of minutes:
no account, no project, no network. It is not a toy (a one-person studio with
a few dozen learners will never outgrow it), but it is also the store used by
every offline test, so it stays honest.

Identifiers are interpolated into SQL because SQLite cannot parameterise table
or column names. Every one of them has passed :func:`check_identifier` at
:class:`~baton.adapters.db.mapping.Schema` construction; values are always
bound.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ...core.config import Config
from ...domain.models import Learner, LessonSlot, Piece, Session, Work, weekday_rank
from ...errors import ConfigError, StateError, UpstreamError, UsageError
from .base import FieldMap
from .base import to_bool as _to_bool
from .base import to_text as _text
from .mapping import Schema

#: How long a statement waits for a competing writer before giving up. SQLite
#: allows one writer at a time, so two Baton commands overlapping (a nightly
#: video job and a morning lookup) is ordinary, not exceptional. Without this
#: the loser fails instantly.
DEFAULT_BUSY_TIMEOUT_MS = 5000

#: Substrings SQLite uses for "someone else is holding it", as opposed to
#: "what you asked for is not there". Both arrive as `OperationalError`.
_CONTENTION = ("database is locked", "database table is locked", "database is busy")


def _is_contention(exc: sqlite3.OperationalError) -> bool:
    """Whether this failure is a busy database rather than a wrong schema."""
    message = str(exc).lower()
    return any(marker in message for marker in _CONTENTION)


def _contention_error(exc: sqlite3.OperationalError) -> UpstreamError:
    """The error a busy database deserves: transient, and worth retrying.

    Raised as `UpstreamError` deliberately. It is what `FallbackStore` fails
    over on, and a database that is merely busy is exactly the case a fallback
    exists for: where a misreported schema error would never divert.
    """
    return UpstreamError(
        f"The database is in use by another process: {exc}",
        service="sqlite",
        remedy="Wait for the other command to finish and run this again. If it "
        "happens often, raise db.sqlite.busy_timeout_ms.",
    )


class SqliteStore:
    """A :class:`~baton.adapters.db.base.LearnerStore` backed by one file."""

    driver = "sqlite"

    def __init__(
        self, path: Path, schema: Schema, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
    ) -> None:
        self.path = path
        self.schema = schema
        #: Tables whose columns have been checked against config already.
        self._verified: set[str] = set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = sqlite3.connect(str(self.path))
        except sqlite3.Error as exc:
            raise ConfigError(
                f"Cannot open the database at {self.path}: {exc}",
                remedy="Check db.sqlite.path in baton.yaml and that the directory is writable.",
            ) from exc
        self._db.row_factory = sqlite3.Row
        # Survives a crash mid-write without a separate journal to clean up.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        # Wait for a competing writer rather than failing the moment one exists.
        self._db.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")

    @classmethod
    def from_config(cls, config: Config) -> SqliteStore:
        return cls(
            config.path("db.sqlite.path"),
            Schema.from_config(config),
            busy_timeout_ms=int(config.get("db.sqlite.busy_timeout_ms", DEFAULT_BUSY_TIMEOUT_MS)),
        )

    # -- helpers -----------------------------------------------------------

    def _ensure_columns(self, fields: FieldMap) -> None:
        """Check the table has every mapped column, once per table per process.

        Reading the table definition rather than waiting for a query to fail
        buys two things: it works on an empty table, and the error names the
        missing column instead of relaying SQLite's phrasing.
        """
        if fields.table in self._verified:
            return
        try:
            info = list(self._db.execute(f"PRAGMA table_info({fields.table})"))
        except sqlite3.Error as exc:
            raise UpstreamError(f"SQLite query failed: {exc}", service="sqlite") from exc

        if not info:
            raise ConfigError(
                f"The database has no table `{fields.table}`.",
                remedy="Run the migration in migrations/sqlite.sql, or correct "
                "db.tables in baton.yaml.",
                details={"table": fields.table},
            )

        present = {str(row["name"]) for row in info}
        missing = sorted(set(fields.columns.values()) - present)
        if missing:
            raise ConfigError(
                f"Table `{fields.table}` has no column(s): {', '.join(missing)}.",
                remedy="Correct db.fields in baton.yaml to match your schema, or "
                "run the migration in migrations/sqlite.sql.",
                details={"table": fields.table, "missing": missing},
            )
        self._verified.add(fields.table)

    def _select(self, fields: FieldMap, *, where: str = "", order: str = "") -> str:
        """Select whole rows.

        ``SELECT *`` rather than the mapped columns so that ``Learner.raw``
        keeps the columns a studio has and Baton knows nothing about. Mapped
        columns are verified separately by :meth:`_ensure_columns`, so this
        does not weaken the schema check.
        """
        sql = f"SELECT * FROM {fields.table}"  # noqa: S608 - identifiers validated
        if where:
            sql += f" WHERE {where}"
        if order:
            sql += f" ORDER BY {order}"
        return sql

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        try:
            return list(self._db.execute(sql, params))
        except sqlite3.OperationalError as exc:
            if _is_contention(exc):
                raise _contention_error(exc) from exc
            # Otherwise a table or column that config promised and the database
            # does not have. Say which, rather than leaking SQL.
            raise ConfigError(
                f"The database does not match the configured schema: {exc}",
                remedy="Run the migration in migrations/sqlite.sql, or correct "
                "db.tables / db.fields in baton.yaml. `baton doctor` checks this.",
                details={"sql_error": str(exc)},
            ) from exc
        except sqlite3.Error as exc:
            raise UpstreamError(f"SQLite query failed: {exc}", service="sqlite") from exc

    def _write(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        try:
            cursor = self._db.execute(sql, params)
            self._db.commit()
            return cursor
        except sqlite3.OperationalError as exc:
            if _is_contention(exc):
                raise _contention_error(exc) from exc
            raise ConfigError(
                f"The database does not match the configured schema: {exc}",
                remedy="Run the migration in migrations/sqlite.sql, or correct "
                "db.tables / db.fields in baton.yaml.",
                details={"sql_error": str(exc)},
            ) from exc
        except sqlite3.Error as exc:
            raise UpstreamError(f"SQLite write failed: {exc}", service="sqlite") from exc

    def _get(self, row: sqlite3.Row, fields: FieldMap, name: str, default: Any = "") -> Any:
        if not fields.has(name):
            return default
        try:
            return row[fields.column(name)]
        except (IndexError, KeyError):
            return default

    # -- learners ----------------------------------------------------------

    def _learner(self, row: sqlite3.Row) -> Learner:
        fields = self.schema.learners
        current = self._get(row, fields, "current_piece_id", None)
        deleted = self._get(row, fields, "deleted_at", None)
        return Learner(
            id=_text(row[fields.column("id")]),
            name=_text(row[fields.column("name")]),
            instrument=_text(self._get(row, fields, "instrument")),
            tone=_text(self._get(row, fields, "tone")),
            has_instrument=_to_bool(self._get(row, fields, "has_instrument", False)),
            current_piece_id=None if current in (None, "") else _text(current),
            # Unmapped (or absent) means active: a studio that has not adopted
            # the column keeps every learner in matching, exactly as before.
            is_active=_to_bool(self._get(row, fields, "is_active", True)),
            # Unmapped (or NULL) means not trashed, the same graceful default.
            deleted_at=None if deleted in (None, "") else _text(deleted),
            raw=dict(row),
        )

    def list_learners(self, include_trashed: bool = False) -> list[Learner]:
        fields = self.schema.learners
        self._ensure_columns(fields)
        where = ""
        if not include_trashed and fields.has("deleted_at"):
            where = f"{fields.column('deleted_at')} IS NULL"
        sql = self._select(fields, where=where, order=f"{fields.column('name')} ASC")
        return [self._learner(row) for row in self._query(sql)]

    def get_learner(self, learner_id: str) -> Learner | None:
        fields = self.schema.learners
        self._ensure_columns(fields)
        sql = self._select(fields, where=f"{fields.column('id')} = ?")
        rows = self._query(sql, (learner_id,))
        return self._learner(rows[0]) if rows else None

    def set_current_piece(self, learner_id: str, piece_id: str | None) -> None:
        fields = self.schema.learners
        self._ensure_columns(fields)
        if not fields.has("current_piece_id"):
            raise ConfigError(
                "This profile does not map a current piece column.",
                remedy="Add db.fields.learner.current_piece_id to baton.yaml.",
            )
        sql = (
            f"UPDATE {fields.table} SET {fields.column('current_piece_id')} = ? "  # noqa: S608
            f"WHERE {fields.column('id')} = ?"
        )
        cursor = self._write(sql, (piece_id, learner_id))
        # An UPDATE that matched nothing is a silent success: the same shape
        # of bug `update_piece` already guards against on the PostgREST side.
        # `learner assign` reports "is now working on" straight after this
        # returns, so a no-op here reads to the teacher as a completed change.
        if cursor.rowcount == 0:
            raise StateError(
                f"No learner with id {learner_id}; the assignment was not written.",
                remedy="Re-read the learner (`baton learner list`) and try again.",
            )

    def set_active(self, learner_id: str, active: bool) -> None:
        fields = self.schema.learners
        self._ensure_columns(fields)
        if not fields.has("is_active"):
            raise ConfigError(
                "This profile does not map an active-status column.",
                remedy="Add db.fields.learner.is_active to baton.yaml.",
            )
        sql = (
            f"UPDATE {fields.table} SET {fields.column('is_active')} = ? "  # noqa: S608
            f"WHERE {fields.column('id')} = ?"
        )
        cursor = self._write(sql, (1 if active else 0, learner_id))
        # Same refusal as `set_current_piece`: a status change that matched no
        # row must not read back as "marked" to the teacher.
        if cursor.rowcount == 0:
            raise StateError(
                f"No learner with id {learner_id}; the status was not written.",
                remedy="Re-read the learner (`baton learner list`) and try again.",
            )

    def trash_learner(self, learner_id: str) -> None:
        fields = self.schema.learners
        self._ensure_columns(fields)
        if not fields.has("deleted_at"):
            raise ConfigError(
                "This profile does not map a trash column.",
                remedy="Add db.fields.learner.deleted_at to baton.yaml.",
            )
        sql = (
            f"UPDATE {fields.table} SET {fields.column('deleted_at')} = datetime('now') "  # noqa: S608
            f"WHERE {fields.column('id')} = ?"
        )
        cursor = self._write(sql, (learner_id,))
        if cursor.rowcount == 0:
            raise StateError(
                f"No learner with id {learner_id}; nothing was trashed.",
                remedy="Re-read the learner (`baton learner list`) and try again.",
            )

    def untrash_learner(self, learner_id: str) -> None:
        fields = self.schema.learners
        self._ensure_columns(fields)
        if not fields.has("deleted_at"):
            raise ConfigError(
                "This profile does not map a trash column.",
                remedy="Add db.fields.learner.deleted_at to baton.yaml.",
            )
        sql = (
            f"UPDATE {fields.table} SET {fields.column('deleted_at')} = NULL "  # noqa: S608
            f"WHERE {fields.column('id')} = ?"
        )
        cursor = self._write(sql, (learner_id,))
        if cursor.rowcount == 0:
            raise StateError(
                f"No learner with id {learner_id}; nothing was untrashed.",
                remedy="Re-read the learner (`baton learner list`) and try again.",
            )

    def rename_learner(self, learner_id: str, name: str) -> None:
        fields = self.schema.learners
        self._ensure_columns(fields)
        sql = (
            f"UPDATE {fields.table} SET {fields.column('name')} = ? "  # noqa: S608
            f"WHERE {fields.column('id')} = ?"
        )
        cursor = self._write(sql, (name, learner_id))
        # Same refusal as `set_current_piece`: a rename that matched no row
        # must not read back as "renamed" to the teacher.
        if cursor.rowcount == 0:
            raise StateError(
                f"No learner with id {learner_id}; the name was not written.",
                remedy="Re-read the learner (`baton learner list`) and try again.",
            )

    def update_learner(self, learner_id: str, fields: Mapping[str, Any]) -> None:
        schema = self.schema.learners
        self._ensure_columns(schema)
        assignments: list[str] = []
        values: list[Any] = []
        for field, value in fields.items():
            if field not in ("instrument", "tone", "has_instrument"):
                raise ConfigError(
                    f"{field!r} is not an editable learner field.",
                    remedy="Editable fields are: instrument, tone, has_instrument.",
                )
            assignments.append(f"{schema.column(field)} = ?")
            values.append(value)
        sql = (
            f"UPDATE {schema.table} SET {', '.join(assignments)} "  # noqa: S608
            f"WHERE {schema.column('id')} = ?"
        )
        values.append(learner_id)
        cursor = self._write(sql, tuple(values))
        if cursor.rowcount == 0:
            raise StateError(
                f"No learner with id {learner_id}; nothing was written.",
                remedy="Re-read the learner (`baton learner list`) and try again.",
            )

    def add_learner(self, learner: Learner, extra: Mapping[str, Any] | None = None) -> Learner:
        fields = self.schema.learners
        self._ensure_columns(fields)
        payload: dict[str, Any] = {fields.column("name"): learner.name}
        for name, value in (
            ("instrument", learner.instrument),
            ("tone", learner.tone),
            ("has_instrument", learner.has_instrument),
        ):
            if fields.has(name) and value:
                payload[fields.column(name)] = value
        if extra:
            payload.update(fields.extra_columns(extra, setting="db.fields.learner"))

        columns = ", ".join(payload)
        placeholders = ", ".join("?" for _ in payload)
        sql = f"INSERT INTO {fields.table} ({columns}) VALUES ({placeholders})"  # noqa: S608
        cursor = self._write(sql, tuple(payload.values()))

        created = self.get_learner(str(cursor.lastrowid))
        if created is None:  # pragma: no cover - the row was just written
            raise UpstreamError(
                "SQLite accepted the learner but the row cannot be read back.",
                service="sqlite",
            )
        return created

    # -- sessions ----------------------------------------------------------

    def _session(self, row: sqlite3.Row) -> Session:
        fields = self.schema.sessions
        return Session(
            id=_text(row[fields.column("id")]),
            learner_id=_text(row[fields.column("learner_id")]),
            number=int(row[fields.column("number")] or 0),
            doc_id=_text(self._get(row, fields, "doc_id")),
            raw=dict(row),
        )

    def list_sessions(self, learner_id: str) -> list[Session]:
        fields = self.schema.sessions
        self._ensure_columns(fields)
        sql = self._select(
            fields,
            where=f"{fields.column('learner_id')} = ?",
            order=f"{fields.column('number')} ASC",
        )
        return [self._session(row) for row in self._query(sql, (learner_id,))]

    def get_session(self, learner_id: str, number: int) -> Session | None:
        fields = self.schema.sessions
        self._ensure_columns(fields)
        sql = self._select(
            fields,
            where=f"{fields.column('learner_id')} = ? AND {fields.column('number')} = ?",
        )
        rows = self._query(sql, (learner_id, number))
        return self._session(rows[0]) if rows else None

    def add_session(self, session: Session, extra: Mapping[str, Any] | None = None) -> Session:
        fields = self.schema.sessions
        self._ensure_columns(fields)
        payload: dict[str, Any] = {
            fields.column("learner_id"): session.learner_id,
            fields.column("number"): session.number,
        }
        if fields.has("doc_id") and session.doc_id:
            payload[fields.column("doc_id")] = session.doc_id
        if extra:
            payload.update(fields.extra_columns(extra, setting="db.fields.session"))

        columns = ", ".join(payload)
        placeholders = ", ".join("?" for _ in payload)
        sql = f"INSERT INTO {fields.table} ({columns}) VALUES ({placeholders})"  # noqa: S608
        self._write(sql, tuple(payload.values()))

        created = self.get_session(session.learner_id, session.number)
        if created is None:  # pragma: no cover - the row was just written
            raise UpstreamError(
                "SQLite accepted the session but the row cannot be read back.",
                service="sqlite",
            )
        return created

    # -- pieces ------------------------------------------------------------

    def _piece(self, row: sqlite3.Row) -> Piece:
        fields = self.schema.pieces
        return Piece(
            id=_text(row[fields.column("id")]),
            title=_text(row[fields.column("title")]),
            source_link=_text(self._get(row, fields, "source_link")),
            practice_track=_text(self._get(row, fields, "practice_track")),
            sheet_link=_text(self._get(row, fields, "sheet_link")),
            raw=dict(row),
        )

    def list_pieces(self) -> list[Piece]:
        fields = self.schema.pieces
        self._ensure_columns(fields)
        sql = self._select(fields, order=f"{fields.column('title')} ASC")
        return [self._piece(row) for row in self._query(sql)]

    def get_piece(self, piece_id: str) -> Piece | None:
        fields = self.schema.pieces
        self._ensure_columns(fields)
        sql = self._select(fields, where=f"{fields.column('id')} = ?")
        rows = self._query(sql, (piece_id,))
        return self._piece(rows[0]) if rows else None

    def add_piece(self, piece: Piece) -> Piece:
        fields = self.schema.pieces
        self._ensure_columns(fields)
        payload: dict[str, Any] = {fields.column("title"): piece.title}
        for name, value in (
            ("source_link", piece.source_link),
            ("practice_track", piece.practice_track),
            ("sheet_link", piece.sheet_link),
        ):
            if fields.has(name) and value:
                payload[fields.column(name)] = value

        columns = ", ".join(payload)
        placeholders = ", ".join("?" for _ in payload)
        sql = f"INSERT INTO {fields.table} ({columns}) VALUES ({placeholders})"  # noqa: S608
        cursor = self._write(sql, tuple(payload.values()))

        created = self.get_piece(str(cursor.lastrowid))
        if created is None:  # pragma: no cover - the row was just written
            raise UpstreamError(
                "SQLite accepted the piece but the row cannot be read back.",
                service="sqlite",
            )
        return created

    def update_piece(self, piece_id: str, changes: Mapping[str, str]) -> Piece | None:
        fields = self.schema.pieces
        self._ensure_columns(fields)
        # `extra_columns` is the mapping step here too: an unmapped field name
        # is a configuration error, not a silent no-op.
        payload = fields.extra_columns(changes, setting="db.fields.piece")
        assignments = ", ".join(f"{column} = ?" for column in payload)
        sql = (
            f"UPDATE {fields.table} SET {assignments} "  # noqa: S608
            f"WHERE {fields.column('id')} = ?"
        )
        cursor = self._write(sql, (*payload.values(), piece_id))
        if cursor.rowcount == 0:
            return None
        return self.get_piece(piece_id)

    def delete_piece(self, piece_id: str) -> bool:
        fields = self.schema.pieces
        self._ensure_columns(fields)
        sql = f"DELETE FROM {fields.table} WHERE {fields.column('id')} = ?"  # noqa: S608
        cursor = self._write(sql, (piece_id,))
        return cursor.rowcount > 0

    # -- works -------------------------------------------------------------

    def _work(self, row: sqlite3.Row) -> Work:
        fields = self.schema.works
        return Work(
            id=_text(row[fields.column("id")]),
            learner_id=_text(row[fields.column("learner_id")]),
            title=_text(row[fields.column("title")]),
            type=_text(self._get(row, fields, "type", "performance")) or "performance",
            video_link=_text(self._get(row, fields, "video_link")),
            drive_link=_text(self._get(row, fields, "drive_link")),
            performed_date=_text(self._get(row, fields, "performed_date")),
            raw=dict(row),
        )

    def list_works(self, learner_id: str) -> list[Work]:
        fields = self.schema.works
        self._ensure_columns(fields)
        order = (
            f"{fields.column('performed_date')} DESC"
            if fields.has("performed_date")
            else f"{fields.column('id')} DESC"
        )
        sql = self._select(fields, where=f"{fields.column('learner_id')} = ?", order=order)
        return [self._work(row) for row in self._query(sql, (learner_id,))]

    def add_work(self, work: Work) -> Work:
        fields = self.schema.works
        self._ensure_columns(fields)
        payload: dict[str, Any] = {
            fields.column("learner_id"): work.learner_id,
            fields.column("title"): work.title,
        }
        for name, value in (
            ("type", work.type),
            ("video_link", work.video_link),
            ("drive_link", work.drive_link),
            ("performed_date", work.performed_date),
        ):
            if fields.has(name) and value:
                payload[fields.column(name)] = value

        columns = ", ".join(payload)
        placeholders = ", ".join("?" for _ in payload)
        sql = f"INSERT INTO {fields.table} ({columns}) VALUES ({placeholders})"  # noqa: S608
        cursor = self._write(sql, tuple(payload.values()))

        created = self.get_work(str(cursor.lastrowid))
        return created if created is not None else work

    def get_work(self, work_id: str) -> Work | None:
        fields = self.schema.works
        self._ensure_columns(fields)
        sql = self._select(fields, where=f"{fields.column('id')} = ?")
        rows = self._query(sql, (work_id,))
        return self._work(rows[0]) if rows else None

    # -- lesson slots --------------------------------------------------------

    def _slot(self, row: sqlite3.Row) -> LessonSlot:
        fields = self.schema.slots
        return LessonSlot(
            id=_text(row[fields.column("id")]),
            learner_id=_text(row[fields.column("learner_id")]),
            weekday=_text(row[fields.column("weekday")]),
            start=_text(row[fields.column("start_time")]),
            raw=dict(row),
        )

    def list_slots(self, learner_id: str | None = None) -> list[LessonSlot]:
        fields = self.schema.slots
        self._ensure_columns(fields)
        where = ""
        params: tuple[Any, ...] = ()
        if learner_id is not None:
            where = f"{fields.column('learner_id')} = ?"
            params = (learner_id,)
        rows = self._query(self._select(fields, where=where), params)
        # Weekday words do not sort into a week on their own (Friday would
        # lead), so the Monday-first order is applied here rather than in SQL.
        slots = [self._slot(row) for row in rows]
        slots.sort(key=lambda slot: (weekday_rank(slot.weekday), slot.start))
        return slots

    def set_slots(self, learner_id: str, slots: Sequence[tuple[str, str]]) -> list[LessonSlot]:
        fields = self.schema.slots
        self._ensure_columns(fields)
        seen: set[tuple[str, str]] = set()
        for weekday, start in slots:
            if (weekday, start) in seen:
                raise UsageError(
                    f"The slot {weekday} {start} appears twice in one request.",
                    remedy="Send each hour once; a longer lesson is two consecutive slots.",
                )
            seen.add((weekday, start))

        # One transaction for the whole replace, so a failure halfway cannot
        # leave this learner with half of their new schedule.
        columns = (
            fields.column("learner_id"),
            fields.column("weekday"),
            fields.column("start_time"),
        )
        placeholders = ", ".join("?" for _ in columns)
        try:
            self._db.execute(
                f"DELETE FROM {fields.table} WHERE {fields.column('learner_id')} = ?",  # noqa: S608
                (learner_id,),
            )
            for weekday, start in slots:
                self._db.execute(
                    f"INSERT INTO {fields.table} ({', '.join(columns)}) "  # noqa: S608
                    f"VALUES ({placeholders})",
                    (learner_id, weekday, start),
                )
            self._db.commit()
        except sqlite3.IntegrityError as exc:
            self._db.rollback()
            raise UsageError(
                f"The database refused the schedule: {exc}",
                remedy="A duplicate slot is the usual cause; send each weekday-hour pair once.",
            ) from exc
        except sqlite3.Error as exc:
            self._db.rollback()
            raise UpstreamError(f"SQLite write failed: {exc}", service="sqlite") from exc
        return self.list_slots(learner_id)

    # -- lifecycle ---------------------------------------------------------

    def health(self) -> None:
        """Touch every configured table so a schema drift surfaces here."""
        if not self.path.exists():
            raise ConfigError(
                f"No database at {self.path}.",
                remedy="Create it with the migration in migrations/sqlite.sql.",
            )
        for fields in (
            self.schema.learners,
            self.schema.sessions,
            self.schema.pieces,
            self.schema.works,
            self.schema.slots,
        ):
            self._ensure_columns(fields)
            self._query(self._select(fields) + " LIMIT 1")

    def close(self) -> None:
        self._db.close()
