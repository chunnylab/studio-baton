"""Shared CLI plumbing for the moment a typed name becomes a learner.

Every command group resolves a name the same way: the profile's aliases and
its word for "learner", then a warning when the name belongs to someone who
stopped studying. Booking's looser resolver is the one exception and stays
in cmd_calendar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..domain.models import Learner
from ..domain.resolve import inactive_note, resolve_learner

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .app import Context


def warn_if_inactive(ctx: Context, learner: Learner) -> None:
    """Say it on stderr when a resolved learner stopped studying.

    Every command shares this through its resolve wrapper, so the warning
    reads identically wherever an inactive learner's exact name is used.
    In JSON mode the sentence stays off stdout (the envelope stays one
    document); the payload's ``is_active`` field carries the fact there.
    """
    note = inactive_note(learner, ctx.config.label("learner"))
    if note:
        ctx.report.warn(
            f"{note}. Their history stays reachable; reactivate with "
            f'`baton learner activate "{learner.name}"`.'
        )


def resolve(ctx: Context, store: Any, name: str, *, include_trashed: bool = False) -> Learner:
    """Resolve a typed name, or raise NeedsHumanError with candidates.

    A trashed learner's name never resolves by default: ``list_learners()``
    leaves them out, so trash acts on every other command as if the learner
    were gone. Only ``learner trash``/``untrash`` pass ``include_trashed``,
    and they skip the inactive warning, which is about studying, not trash.
    """
    learners = (
        store.list_learners(include_trashed=True) if include_trashed else store.list_learners()
    )
    learner = resolve_learner(
        name,
        learners,
        aliases=ctx.config.get("db.aliases", {}) or {},
        label=ctx.config.label("learner"),
    )
    if not include_trashed:
        warn_if_inactive(ctx, learner)
    return learner
