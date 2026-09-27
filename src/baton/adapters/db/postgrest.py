"""Learner records over a PostgREST API.

Covers both drivers: Supabase *is* PostgREST with an ``apikey`` header, so the
`supabase` driver is this class with different headers rather than a second
implementation. The original system had a hand-rolled Supabase client and a
separate local-PostgREST fallback client that drifted apart; one class with two
header sets is the fix.

Every request goes through :func:`baton.core.retry.http_request`, so rate limits
and 5xx are retried with backoff and a lost connection becomes a typed
``UpstreamError`` rather than a traceback.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

from ...core.config import Config
from ...core.retry import http_request
from ...domain.models import Learner, LessonSlot, Piece, Session, Work, weekday_rank
from ...errors import ConfigError, StateError, UpstreamError, UsageError
from .base import FieldMap
from .mapping import Schema


def _text(value: Any) -> str:
    return "" if value is None else str(value)


_REST_PATH = "/rest/v1"


def url_problem(driver: str, url: str) -> tuple[str, str] | None:
    """What is wrong with pairing this URL with this driver, if anything.

    Returns:
        ``(detail, remedy)``, or ``None`` when the pair can work. A trailing
        ``/rest/v1`` on a Supabase URL is not a problem: the supabase driver
        strips it rather than sending ``/rest/v1/rest/v1``.
    """
    host = (urlsplit(url).hostname or "").lower()
    if driver == "postgrest" and (host == "supabase.co" or host.endswith(".supabase.co")):
        return (
            f"{host} is a Supabase project, and the postgrest driver sends no "
            "`apikey` header, so Supabase answers every request with 401.",
            "Set `db.driver: supabase` and put the project URL and key in "
            "SUPABASE_PROJECT_URL and SUPABASE_PROJECT_API. Give the URL without "
            "/rest/v1: the supabase driver adds that path itself.",
        )
    return None


def _supabase_base(url: str) -> str:
    """The project URL with any ``/rest/v1`` a person already typed removed."""
    base = url.rstrip("/")
    if base.endswith(_REST_PATH):
        base = base[: -len(_REST_PATH)].rstrip("/")
    return base


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return False


class PostgrestStore:
    """A :class:`~baton.adapters.db.base.LearnerStore` over PostgREST."""

    driver = "postgrest"

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        schema: Schema,
        *,
        driver_name: str = "postgrest",
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self.schema = schema
        self.driver = driver_name
        self.timeout = timeout

    # -- construction ------------------------------------------------------

    @classmethod
    def from_config(cls, config: Config) -> PostgrestStore:
        url = config.secret("db.postgrest.url_env")
        problem = url_problem("postgrest", str(url))
        if problem is not None:
            raise ConfigError(problem[0], remedy=problem[1])
        jwt = config.secret("db.postgrest.jwt_env", required=False)
        headers = {"Accept": "application/json"}
        if jwt:
            headers["Authorization"] = f"Bearer {jwt}"
        schema_name = str(config.get("db.postgrest.schema", "public"))
        if schema_name and schema_name != "public":
            headers["Accept-Profile"] = schema_name
            headers["Content-Profile"] = schema_name
        return cls(str(url), headers, Schema.from_config(config), driver_name="postgrest")

    @classmethod
    def from_supabase_config(cls, config: Config) -> PostgrestStore:
        url = _supabase_base(str(config.secret("db.supabase.url_env")))
        key = str(config.secret("db.supabase.key_env"))
        headers = {
            "Accept": "application/json",
            "apikey": key,
            "Authorization": f"Bearer {key}",
        }
        return cls(
            f"{url}{_REST_PATH}",
            headers,
            Schema.from_config(config),
            driver_name="supabase",
        )

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        table: str,
        *,
        params: str = "",
        json_body: Any = None,
        prefer: str | None = None,
    ) -> Any:
        url = f"{self.base_url}/{quote(table)}"
        if params:
            url = f"{url}?{params}"
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        response = http_request(
            method,
            url,
            service=self.driver,
            headers=headers,
            json=json_body,
            timeout=self.timeout,
        )

        if response.status_code in (401, 403):
            raise ConfigError(
                f"{self.driver} rejected the credentials ({response.status_code}).",
                remedy="Check the API key or JWT named in baton.yaml, and that "
                "the role may read these tables.",
            )
        if response.status_code == 404:
            raise ConfigError(
                f"{self.driver} has no table `{table}`.",
                remedy="Correct db.tables in baton.yaml, or run the migration "
                "in migrations/postgres.sql.",
            )
        if response.status_code >= 400:
            detail = response.text[:300]
            # PostgREST reports an unknown column as 400 with the name in the
            # body: the single most likely misconfiguration, so name it.
            raise ConfigError(
                f"{self.driver} rejected the query: {detail}",
                remedy="Usually a column named in db.fields that does not "
                "exist. `baton doctor` checks the whole schema at once.",
                details={"status": response.status_code, "body": detail},
            )

        if not response.content:
            return []
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(
                f"{self.driver} returned a response that is not JSON.",
                service=self.driver,
                status=response.status_code,
            ) from exc

    def _select_params(self, fields: FieldMap, filters: str = "", order: str = "") -> str:
        columns = ",".join(sorted(set(fields.columns.values())))
        parts = [f"select={columns}"]
        if filters:
            parts.append(filters)
        if order:
            parts.append(f"order={order}")
        return "&".join(parts)

    def _rows(self, fields: FieldMap, filters: str = "", order: str = "") -> list[dict[str, Any]]:
        data = self._request(
            "GET", fields.table, params=self._select_params(fields, filters, order)
        )
        return data if isinstance(data, list) else []

    def _get(self, row: dict[str, Any], fields: FieldMap, name: str, default: Any = "") -> Any:
        return row.get(fields.column(name), default) if fields.has(name) else default

    # -- learners ----------------------------------------------------------

    def _learner(self, row: dict[str, Any]) -> Learner:
        fields = self.schema.learners
        current = self._get(row, fields, "current_piece_id", None)
        deleted = self._get(row, fields, "deleted_at", None)
        return Learner(
            id=_text(row.get(fields.column("id"))),
            name=_text(row.get(fields.column("name"))),
            instrument=_text(self._get(row, fields, "instrument")),
            tone=_text(self._get(row, fields, "tone")),
            has_instrument=_to_bool(self._get(row, fields, "has_instrument", False)),
            current_piece_id=None if current in (None, "") else _text(current),
            # Unmapped (or absent) means active: a studio that has not adopted
            # the column keeps every learner in matching, exactly as before.
            is_active=_to_bool(self._get(row, fields, "is_active", True)),
            # Unmapped (or null) means not trashed, the same graceful default.
            deleted_at=None if deleted in (None, "") else _text(deleted),
            raw=row,
        )

    def list_learners(self, include_trashed: bool = False) -> list[Learner]:
        fields = self.schema.learners
        filters = ""
        if not include_trashed and fields.has("deleted_at"):
            filters = f"{fields.column('deleted_at')}=is.null"
        rows = self._rows(fields, filters=filters, order=f"{fields.column('name')}.asc")
        return [self._learner(row) for row in rows]

    def get_learner(self, learner_id: str) -> Learner | None:
        fields = self.schema.learners
        rows = self._rows(fields, filters=f"{fields.column('id')}=eq.{quote(str(learner_id))}")
        return self._learner(rows[0]) if rows else None

    def set_current_piece(self, learner_id: str, piece_id: str | None) -> None:
        fields = self.schema.learners
        if not fields.has("current_piece_id"):
            raise ConfigError(
                "This profile does not map a current piece column.",
                remedy="Add db.fields.learner.current_piece_id to baton.yaml.",
            )
        updated = self._request(
            "PATCH",
            fields.table,
            params=f"{fields.column('id')}=eq.{quote(str(learner_id))}",
            json_body={fields.column("current_piece_id"): piece_id},
            prefer="return=representation",
        )
        # Representation rather than minimal for the same reason `update_piece`
        # asks for it: PostgREST answers 200 with an empty array when the
        # filter matched nothing, and `learner assign` would report a change
        # that was never written.
        rows = updated if isinstance(updated, list) else [updated]
        if not rows:
            raise StateError(
                f"No learner with id {learner_id}; the assignment was not written.",
                remedy="Re-read the learner (`baton learner list`) and try again.",
            )

    def set_active(self, learner_id: str, active: bool) -> None:
        fields = self.schema.learners
        if not fields.has("is_active"):
            raise ConfigError(
                "This profile does not map an active-status column.",
                remedy="Add db.fields.learner.is_active to baton.yaml.",
            )
        updated = self._request(
            "PATCH",
            fields.table,
            params=f"{fields.column('id')}=eq.{quote(str(learner_id))}",
            json_body={fields.column("is_active"): active},
            prefer="return=representation",
        )
        # Same empty-array trap as `set_current_piece`: a 200 whose
        # representation is empty means the filter matched nothing, and the
        # teacher must not read that as "marked".
        rows = updated if isinstance(updated, list) else [updated]
        if not rows:
            raise StateError(
                f"No learner with id {learner_id}; the status was not written.",
                remedy="Re-read the learner (`baton learner list`) and try again.",
            )

    def trash_learner(self, learner_id: str) -> None:
        fields = self.schema.learners
        if not fields.has("deleted_at"):
            raise ConfigError(
                "This profile does not map a trash column.",
                remedy="Add db.fields.learner.deleted_at to baton.yaml.",
            )
        stamp = datetime.now(timezone.utc).isoformat()
        updated = self._request(
            "PATCH",
            fields.table,
            params=f"{fields.column('id')}=eq.{quote(str(learner_id))}",
            json_body={fields.column("deleted_at"): stamp},
            prefer="return=representation",
        )
        rows = updated if isinstance(updated, list) else [updated]
        if not rows:
            raise StateError(
                f"No learner with id {learner_id}; nothing was trashed.",
                remedy="Re-read the learner (`baton learner list`) and try again.",
            )

    def untrash_learner(self, learner_id: str) -> None:
        fields = self.schema.learners
        if not fields.has("deleted_at"):
            raise ConfigError(
                "This profile does not map a trash column.",
                remedy="Add db.fields.learner.deleted_at to baton.yaml.",
            )
        updated = self._request(
            "PATCH",
            fields.table,
            params=f"{fields.column('id')}=eq.{quote(str(learner_id))}",
            json_body={fields.column("deleted_at"): None},
            prefer="return=representation",
        )
        rows = updated if isinstance(updated, list) else [updated]
        if not rows:
            raise StateError(
                f"No learner with id {learner_id}; nothing was untrashed.",
                remedy="Re-read the learner (`baton learner list`) and try again.",
            )

    def rename_learner(self, learner_id: str, name: str) -> None:
        fields = self.schema.learners
        updated = self._request(
            "PATCH",
            fields.table,
            params=f"{fields.column('id')}=eq.{quote(str(learner_id))}",
            json_body={fields.column("name"): name},
            prefer="return=representation",
        )
        # Same empty-array trap as `set_current_piece`: a 200 whose
        # representation is empty means the filter matched nothing, and the
        # teacher must not read that as "renamed".
        rows = updated if isinstance(updated, list) else [updated]
        if not rows:
            raise StateError(
                f"No learner with id {learner_id}; the name was not written.",
                remedy="Re-read the learner (`baton learner list`) and try again.",
            )

    def update_learner(self, learner_id: str, fields: Mapping[str, Any]) -> None:
        schema = self.schema.learners
        body: dict[str, Any] = {}
        for field, value in fields.items():
            if field not in ("instrument", "tone", "has_instrument"):
                raise ConfigError(
                    f"{field!r} is not an editable learner field.",
                    remedy="Editable fields are: instrument, tone, has_instrument.",
                )
            body[schema.column(field)] = value
        updated = self._request(
            "PATCH",
            schema.table,
            params=f"{schema.column('id')}=eq.{quote(str(learner_id))}",
            json_body=body,
            prefer="return=representation",
        )
        rows = updated if isinstance(updated, list) else [updated]
        if not rows:
            raise StateError(
                f"No learner with id {learner_id}; nothing was written.",
                remedy="Re-read the learner (`baton learner list`) and try again.",
            )

    def add_learner(self, learner: Learner, extra: Mapping[str, Any] | None = None) -> Learner:
        fields = self.schema.learners
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

        created = self._request(
            "POST", fields.table, json_body=payload, prefer="return=representation"
        )
        return self._represented(created, "learner", self.driver, self._learner)

    def add_session(self, session: Session, extra: Mapping[str, Any] | None = None) -> Session:
        fields = self.schema.sessions
        payload: dict[str, Any] = {
            fields.column("learner_id"): session.learner_id,
            fields.column("number"): session.number,
        }
        if fields.has("doc_id") and session.doc_id:
            payload[fields.column("doc_id")] = session.doc_id
        if extra:
            payload.update(fields.extra_columns(extra, setting="db.fields.session"))

        created = self._request(
            "POST", fields.table, json_body=payload, prefer="return=representation"
        )
        return self._represented(created, "session", self.driver, self._session)

    @staticmethod
    def _represented(created: Any, what: str, driver: str, build: Any) -> Any:
        """The row a write returned, or a loud refusal to pretend it has one.

        A server that ignores ``Prefer: return=representation`` may still have
        written the row. Handing back the caller's object would hide that, and
        the next add would duplicate it.
        """
        rows = created if isinstance(created, list) else [created]
        if not rows or not isinstance(rows[0], dict):
            raise UpstreamError(
                f"{driver} accepted the {what} but returned no representation "
                f"to read the assigned id from.",
                service=driver,
                remedy="The row may exist on the server: check for it before "
                "retrying, or this add will run twice.",
            )
        return build(rows[0])

    # -- sessions ----------------------------------------------------------

    def _session(self, row: dict[str, Any]) -> Session:
        fields = self.schema.sessions
        return Session(
            id=_text(row.get(fields.column("id"))),
            learner_id=_text(row.get(fields.column("learner_id"))),
            number=int(row.get(fields.column("number")) or 0),
            doc_id=_text(self._get(row, fields, "doc_id")),
            raw=row,
        )

    def list_sessions(self, learner_id: str) -> list[Session]:
        fields = self.schema.sessions
        rows = self._rows(
            fields,
            filters=f"{fields.column('learner_id')}=eq.{quote(str(learner_id))}",
            order=f"{fields.column('number')}.asc",
        )
        return [self._session(row) for row in rows]

    def get_session(self, learner_id: str, number: int) -> Session | None:
        fields = self.schema.sessions
        rows = self._rows(
            fields,
            filters=(
                f"{fields.column('learner_id')}=eq.{quote(str(learner_id))}"
                f"&{fields.column('number')}=eq.{int(number)}"
            ),
        )
        return self._session(rows[0]) if rows else None

    # -- pieces ------------------------------------------------------------

    def _piece(self, row: dict[str, Any]) -> Piece:
        fields = self.schema.pieces
        return Piece(
            id=_text(row.get(fields.column("id"))),
            title=_text(row.get(fields.column("title"))),
            source_link=_text(self._get(row, fields, "source_link")),
            practice_track=_text(self._get(row, fields, "practice_track")),
            sheet_link=_text(self._get(row, fields, "sheet_link")),
            raw=row,
        )

    def list_pieces(self) -> list[Piece]:
        fields = self.schema.pieces
        rows = self._rows(fields, order=f"{fields.column('title')}.asc")
        return [self._piece(row) for row in rows]

    def get_piece(self, piece_id: str) -> Piece | None:
        fields = self.schema.pieces
        rows = self._rows(fields, filters=f"{fields.column('id')}=eq.{quote(str(piece_id))}")
        return self._piece(rows[0]) if rows else None

    def add_piece(self, piece: Piece) -> Piece:
        fields = self.schema.pieces
        payload: dict[str, Any] = {fields.column("title"): piece.title}
        for name, value in (
            ("source_link", piece.source_link),
            ("practice_track", piece.practice_track),
            ("sheet_link", piece.sheet_link),
        ):
            if fields.has(name) and value:
                payload[fields.column(name)] = value

        created = self._request(
            "POST", fields.table, json_body=payload, prefer="return=representation"
        )
        return self._represented(created, "piece", self.driver, self._piece)

    def update_piece(self, piece_id: str, changes: Mapping[str, str]) -> Piece | None:
        fields = self.schema.pieces
        payload = fields.extra_columns(changes, setting="db.fields.piece")
        updated = self._request(
            "PATCH",
            fields.table,
            params=f"{fields.column('id')}=eq.{quote(str(piece_id))}",
            json_body=payload,
            prefer="return=representation",
        )
        # PostgREST answers 200 with an empty array when the filter matched
        # nothing: a silent success the original script reported as an edit.
        rows = updated if isinstance(updated, list) else [updated]
        return self._piece(rows[0]) if rows and isinstance(rows[0], dict) else None

    def delete_piece(self, piece_id: str) -> bool:
        fields = self.schema.pieces
        deleted = self._request(
            "DELETE",
            fields.table,
            params=f"{fields.column('id')}=eq.{quote(str(piece_id))}",
            prefer="return=representation",
        )
        rows = deleted if isinstance(deleted, list) else [deleted]
        return bool(rows)

    # -- works -------------------------------------------------------------

    def _work(self, row: dict[str, Any]) -> Work:
        fields = self.schema.works
        return Work(
            id=_text(row.get(fields.column("id"))),
            learner_id=_text(row.get(fields.column("learner_id"))),
            title=_text(row.get(fields.column("title"))),
            type=_text(self._get(row, fields, "type", "performance")) or "performance",
            video_link=_text(self._get(row, fields, "video_link")),
            drive_link=_text(self._get(row, fields, "drive_link")),
            performed_date=_text(self._get(row, fields, "performed_date")),
            raw=row,
        )

    def list_works(self, learner_id: str) -> list[Work]:
        fields = self.schema.works
        order_column = "performed_date" if fields.has("performed_date") else "id"
        rows = self._rows(
            fields,
            filters=f"{fields.column('learner_id')}=eq.{quote(str(learner_id))}",
            order=f"{fields.column(order_column)}.desc",
        )
        return [self._work(row) for row in rows]

    def add_work(self, work: Work) -> Work:
        fields = self.schema.works
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

        created = self._request(
            "POST", fields.table, json_body=payload, prefer="return=representation"
        )
        return self._represented(created, "work", self.driver, self._work)

    # -- lesson slots --------------------------------------------------------

    def _slot(self, row: dict[str, Any]) -> LessonSlot:
        fields = self.schema.slots
        return LessonSlot(
            id=_text(row.get(fields.column("id"))),
            learner_id=_text(row.get(fields.column("learner_id"))),
            weekday=_text(row.get(fields.column("weekday"))),
            start=_text(row.get(fields.column("start_time"))),
            raw=row,
        )

    def list_slots(self, learner_id: str | None = None) -> list[LessonSlot]:
        fields = self.schema.slots
        filters = ""
        if learner_id is not None:
            filters = f"{fields.column('learner_id')}=eq.{quote(str(learner_id))}"
        rows = self._rows(fields, filters=filters)
        # Weekday words do not sort into a week on their own (Friday would
        # lead), so the Monday-first order is applied here rather than in the
        # query string.
        slots = [self._slot(row) for row in rows]
        slots.sort(key=lambda slot: (weekday_rank(slot.weekday), slot.start))
        return slots

    def set_slots(self, learner_id: str, slots: Sequence[tuple[str, str]]) -> list[LessonSlot]:
        fields = self.schema.slots
        seen: set[tuple[str, str]] = set()
        for weekday, start in slots:
            if (weekday, start) in seen:
                raise UsageError(
                    f"The slot {weekday} {start} appears twice in one request.",
                    remedy="Send each hour once; a longer lesson is two consecutive slots.",
                )
            seen.add((weekday, start))

        # Plain PostgREST has no multi-statement transaction, so the replace
        # is an ordered pair: clear, then insert the whole set at once. A
        # failure between the two leaves this learner with no slots (a retry
        # re-sends the full set) rather than a mix of old and new hours.
        self._request(
            "DELETE",
            fields.table,
            params=f"{fields.column('learner_id')}=eq.{quote(str(learner_id))}",
        )
        if slots:
            columns = (
                fields.column("learner_id"),
                fields.column("weekday"),
                fields.column("start_time"),
            )
            body = [
                dict(zip(columns, (learner_id, weekday, start), strict=True))
                for weekday, start in slots
            ]
            created = self._request(
                "POST", fields.table, json_body=body, prefer="return=representation"
            )
            rows = created if isinstance(created, list) else [created]
            if not rows or not isinstance(rows[0], dict):
                raise UpstreamError(
                    f"{self.driver} accepted the schedule but returned no "
                    "representation to read the assigned ids from.",
                    service=self.driver,
                    remedy="The rows may exist on the server: check for them "
                    "before retrying, or this replace will run twice.",
                )
        return self.list_slots(learner_id)

    # -- lifecycle ---------------------------------------------------------

    def health(self) -> None:
        """Read one row from every configured table."""
        for fields in (
            self.schema.learners,
            self.schema.sessions,
            self.schema.pieces,
            self.schema.works,
            self.schema.slots,
        ):
            self._request("GET", fields.table, params=f"{self._select_params(fields)}&limit=1")

    def close(self) -> None:
        """Nothing to release: requests are stateless."""
