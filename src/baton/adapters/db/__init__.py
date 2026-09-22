"""Learner-record drivers, selected by one line of configuration."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ...core.config import Config
from ...errors import ConfigError
from .base import FieldMap, LearnerStore, check_identifier
from .fallback import FallbackStore
from .mapping import Schema
from .postgrest import PostgrestStore
from .sqlite import SqliteStore

#: Every driver this build implements. `baton doctor` reports against this, so
#: a typo in `db.driver` is caught before a pipeline runs rather than during one.
DRIVERS = ("sqlite", "supabase", "postgrest")


def _build(driver: str, config: Config) -> Any:
    if driver == "sqlite":
        return SqliteStore.from_config(config)
    if driver == "supabase":
        return PostgrestStore.from_supabase_config(config)
    if driver == "postgrest":
        return PostgrestStore.from_config(config)
    raise ConfigError(
        f"Unknown database driver `{driver}`.",
        remedy=f"Set db.driver to one of: {', '.join(DRIVERS)}.",
        details={"driver": driver, "supported": list(DRIVERS)},
    )


def open_store(config: Config) -> LearnerStore:
    """Build the configured store, wrapping it if a fallback is set.

    Args:
        config: The effective profile configuration.

    Returns:
        A ready :class:`~baton.adapters.db.base.LearnerStore`.

    Raises:
        ConfigError: The driver is unknown, credentials are missing, or the
            configured schema is not usable.
    """
    primary = _build(str(config.get("db.driver", "sqlite")), config)

    fallback = config.get("db.fallback", None)
    if not fallback:
        return primary

    if not isinstance(fallback, dict) or "driver" not in fallback:
        raise ConfigError(
            "`db.fallback` must be a mapping with a `driver` key, or null.",
            remedy="Remove it, or give it the same shape as the primary driver.",
        )

    # The fallback block is read as if it were the primary, so the two are
    # configured identically and a reader does not have to learn a second shape.
    merged = dict(config.data)
    merged["db"] = {**config.section("db"), **fallback, "fallback": None}
    secondary_config = Config(
        data=merged,
        config_file=config.config_file,
        profile_dir=config.profile_dir,
        _state_dir=config.state_dir,
    )
    secondary = _build(str(fallback["driver"]), secondary_config)
    return FallbackStore(primary, secondary)


@contextmanager
def store_scope(config: Config) -> Iterator[LearnerStore]:
    """The one store a CLI command holds, closed on every way out.

    Every command follows the same shape: open, work inside the ``with``,
    close, then report after the block so a report never holds the store
    open. Hand-written ``try/finally store.close()`` per handler said the
    same thing forty-five times and could be forgotten once; this says it
    once and cannot.

    Looks ``open_store`` up at call time, so tests that monkeypatch
    ``baton.adapters.db.open_store`` flow through here unchanged.
    """
    store = open_store(config)
    try:
        yield store
    finally:
        store.close()


__all__ = [
    "DRIVERS",
    "FallbackStore",
    "FieldMap",
    "LearnerStore",
    "PostgrestStore",
    "Schema",
    "SqliteStore",
    "check_identifier",
    "open_store",
    "store_scope",
]
