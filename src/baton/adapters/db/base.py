"""What a learner store must do, and how config maps onto a real schema.

The protocol is small and read-heavy. Writes are limited to the few things a
pipeline genuinely has to record, because every write is a way for Baton to
damage data a studio already owns.

:class:`FieldMap` is what makes "adopt an existing schema" a config change
rather than a fork: every table and column name comes from ``baton.yaml``, and
identifiers are validated before they are ever interpolated into SQL.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ...domain.models import Learner, LessonSlot, Piece, Session, Work
from ...errors import ConfigError


def to_bool(value: Any) -> bool:
    """A stored flag as a bool. Databases without a boolean type, and the
    studios filling them, spell it every possible way."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return False


def to_text(value: Any) -> str:
    """A stored value as text, with ``None`` read as empty."""
    return "" if value is None else str(value)


#: A plain SQL identifier. Anything else is rejected rather than quoted, so a
#: typo in baton.yaml surfaces as a config error instead of broken SQL, and a
#: hand-edited profile cannot smuggle a fragment into a query.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def check_identifier(value: str, *, setting: str) -> str:
    """Validate a table or column name from configuration.

    Args:
        value: The configured name.
        setting: Dotted config path, used in the error message.

    Returns:
        The name, unchanged.

    Raises:
        ConfigError: The name is not a plain SQL identifier.
    """
    if not isinstance(value, str) or not _IDENTIFIER.match(value):
        raise ConfigError(
            f"`{setting}` must be a plain table or column name, got {value!r}.",
            remedy="Use letters, digits, and underscores only, starting with a "
            "letter or underscore.",
            details={"setting": setting, "value": value},
        )
    return value


@dataclass(frozen=True)
class FieldMap:
    """Column names for one entity, resolved from configuration."""

    table: str
    columns: dict[str, str]

    @classmethod
    def build(
        cls,
        *,
        table: str,
        table_setting: str,
        columns: dict[str, Any],
        columns_setting: str,
        required: tuple[str, ...],
    ) -> FieldMap:
        """Validate and freeze the mapping for one entity.

        Raises:
            ConfigError: A required field is unmapped, or a name is not a legal
                identifier.
        """
        check_identifier(table, setting=table_setting)
        resolved: dict[str, str] = {}
        for key, value in columns.items():
            resolved[key] = check_identifier(str(value), setting=f"{columns_setting}.{key}")
        missing = [name for name in required if name not in resolved]
        if missing:
            raise ConfigError(
                f"`{columns_setting}` is missing: {', '.join(missing)}.",
                remedy="Add the missing column mappings to baton.yaml.",
                details={"setting": columns_setting, "missing": missing},
            )
        return cls(table=table, columns=resolved)

    def column(self, name: str) -> str:
        """The configured column for a domain field.

        Raises:
            ConfigError: The field is not mapped.
        """
        try:
            return self.columns[name]
        except KeyError:
            raise ConfigError(
                f"No column is mapped for `{name}` on table `{self.table}`.",
                remedy="Add it under db.fields in baton.yaml.",
            ) from None

    def has(self, name: str) -> bool:
        """Whether a domain field is mapped at all.

        Optional fields (a tone column a studio does not keep, say) are
        simply absent, and readers fall back to a default rather than failing.
        """
        return name in self.columns

    def extra_columns(self, extra: Mapping[str, Any], *, setting: str) -> dict[str, Any]:
        """Resolve write-time extras (columns the model has no field for).

        Args:
            extra: Domain field name to value, from a caller that knows the
                studio keeps more than the model does.
            setting: Dotted config path the mapping lives under, for the
                error message when a key is unmapped.

        Returns:
            Column name to value, ready for the payload.

        Raises:
            ConfigError: A key has no mapped column. Raised before anything
                is written: dropping a column the studio relies on is how a
                record ends up half-created.
        """
        resolved: dict[str, Any] = {}
        for key, value in extra.items():
            if not self.has(key):
                raise ConfigError(
                    f"`{setting}.{key}` is not mapped, so `{key}` cannot be written.",
                    remedy=f"Add it under {setting} in baton.yaml, or drop the field.",
                    details={"setting": setting, "field": key},
                )
            resolved[self.column(key)] = value
        return resolved


@runtime_checkable
class LearnerStore(Protocol):
    """Read and write the records a teaching studio keeps.

    Implementations must not raise for "not found": they return ``None`` or an
    empty list. Only genuine faults (unreachable service, bad credentials,
    schema mismatch) raise, and those raise the typed errors in
    :mod:`baton.errors` so the CLI maps them onto stable exit codes.
    """

    # -- learners ----------------------------------------------------------

    def list_learners(self, include_trashed: bool = False) -> list[Learner]:
        """Every learner, ordered by name.

        Trashed learners are excluded by default, even from ``--all``: trash
        means fully out of the way, unlike an inactive learner. Pass
        ``include_trashed=True`` only for ``learner untrash`` and ``learner
        list --trashed``, the two places that need to find one.
        """
        ...

    def get_learner(self, learner_id: str) -> Learner | None:
        """One learner by id, or ``None``."""
        ...

    def set_current_piece(self, learner_id: str, piece_id: str | None) -> None:
        """Assign (or clear) the piece a learner is working on."""
        ...

    def set_active(self, learner_id: str, active: bool) -> None:
        """Mark a learner as still studying here (or not).

        Records are never deleted: a learner who stopped is flagged, not
        removed, so their history stays reachable by exact name while
        matching and rosters carry on without them.
        """
        ...

    def trash_learner(self, learner_id: str) -> None:
        """Hide a learner from every listing and from ordinary name resolution.

        The row, sessions, pieces, and recorded work are all kept exactly as
        they are; nothing is deleted. Reversible with :meth:`untrash_learner`.
        Safe to call twice.
        """
        ...

    def untrash_learner(self, learner_id: str) -> None:
        """The reverse of :meth:`trash_learner`. Safe to call on a learner
        who was never trashed."""
        ...

    def update_learner(self, learner_id: str, fields: Mapping[str, Any]) -> None:
        """Change a learner's editable fields, and nothing else.

        Keys are domain names, not column names: ``instrument``,
        ``tone``, ``has_instrument``. A key this store does not know is a
        :class:`~baton.errors.ConfigError` rather than a silent skip, and
        an unknown id refuses instead of succeeding. Everything the row
        holds besides the named fields stays exactly as it was.
        """
        ...

    def rename_learner(self, learner_id: str, name: str) -> None:
        """Change the name a learner is recorded under.

        The database row only. Calendar events, session pages, and source
        folders already carrying the old name keep it; what moves is what
        future bookings and clips match against. That is the studio's
        replacement flow: a new learner taking over an old slot, history
        included, without touching anything already written.
        """
        ...

    def add_learner(self, learner: Learner, extra: Mapping[str, Any] | None = None) -> Learner:
        """Enrol a learner. Returns them with the id the store assigned.

        ``extra`` carries studio-specific columns the model has no field for
        (a prompt level, a master link), keyed by domain name and mapped
        through ``db.fields`` like every other column. A key the profile does
        not map is a configuration error raised before anything is written:
        silently dropping a column the studio relies on is how a record ends
        up half-created.
        """
        ...

    # -- sessions ----------------------------------------------------------

    def list_sessions(self, learner_id: str) -> list[Session]:
        """A learner's sessions, ordered by number."""
        ...

    def get_session(self, learner_id: str, number: int) -> Session | None:
        """One numbered session, or ``None``."""
        ...

    def add_session(self, session: Session, extra: Mapping[str, Any] | None = None) -> Session:
        """Record one numbered session page. Returns it with its assigned id.

        ``extra`` is :meth:`add_learner`'s, for the columns a studio's session
        table keeps beyond the model (a Notion database id, say).
        """
        ...

    # -- pieces ------------------------------------------------------------

    def list_pieces(self) -> list[Piece]:
        """Every piece, ordered by title."""
        ...

    def get_piece(self, piece_id: str) -> Piece | None:
        """One piece by id, or ``None``."""
        ...

    def add_piece(self, piece: Piece) -> Piece:
        """Add a piece of music to study. Returns it with its assigned id."""
        ...

    def update_piece(self, piece_id: str, changes: Mapping[str, str]) -> Piece | None:
        """Change the mapped fields of one piece.

        Keys are domain field names; an empty string *clears* the field
        rather than writing a literal empty value: the same convention the
        :class:`~baton.domain.models.Piece` model already uses for "no link",
        so a cleared field and a field that was never set read identically
        everywhere else in Baton. Returns the updated piece, or ``None`` when
        no piece has that id: the caller decides whether that is an error,
        and it usually is: a typo in an id should not read as a successful
        edit.
        """
        ...

    def delete_piece(self, piece_id: str) -> bool:
        """Remove a piece outright. Returns whether a row was actually deleted.

        Callers must refuse while any learner still references the piece;
        this method does not check, because the guard needs the learners'
        names to say who is holding on.
        """
        ...

    # -- works -------------------------------------------------------------

    def list_works(self, learner_id: str) -> list[Work]:
        """A learner's recorded works, newest first."""
        ...

    def add_work(self, work: Work) -> Work:
        """Record a finished work. Returns it with its assigned id."""
        ...

    # -- lesson slots --------------------------------------------------------

    def list_slots(self, learner_id: str | None = None) -> list[LessonSlot]:
        """Recurring weekly slots, ordered Monday-first then by start time.

        Every slot in the studio when ``learner_id`` is ``None``, one
        learner's slots otherwise. The all-studio read is what the clash
        gate in ``learner schedule-set`` checks against, so it must stay a
        single round trip, not a query per learner.
        """
        ...

    def set_slots(self, learner_id: str, slots: Sequence[tuple[str, str]]) -> list[LessonSlot]:
        """Replace a learner's whole weekly schedule in one transaction.

        ``slots`` is the full final set of ``(weekday, start)`` pairs; the
        caller sends everything, so a retry cannot double-book. The same
        contract the dashboard tags use. Duplicates within ``slots`` are a
        :class:`~baton.errors.UsageError`; whether a slot may be taken at
        all (another learner holding the same weekday and start) is checked
        by the caller, which knows the other learners' statuses. Returns the
        stored set, ids included.
        """
        ...

    # -- lifecycle ---------------------------------------------------------

    def health(self) -> None:
        """Prove the store is reachable and the schema matches config.

        Raises:
            BatonError: With the specific reason. Used by ``baton doctor``,
                which is why it checks the schema and not just connectivity:
                a renamed column should be caught before a pipeline hits it.
        """
        ...

    def close(self) -> None:
        """Release any held resources."""
        ...
