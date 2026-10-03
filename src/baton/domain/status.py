"""Translating a studio's own status wording into Baton's three states.

A studio writes "Done", "Complete", "เสร็จแล้ว", or anything else. Baton needs
to reason about exactly three states, so ``docs.statuses`` maps its vocabulary
onto the studio's and this module does the lookup in both directions.

Matching is case- and space-insensitive: a status typed by hand into Notion
picks up stray capitals and trailing spaces, and treating "In Progress " as a
different state from "In progress" would silently hide a session in progress.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: Baton's canonical states, in the order a session moves through them.
DONE = "done"
IN_PROGRESS = "in_progress"
NOT_STARTED = "not_started"
UNKNOWN = ""


def _fold(value: str) -> str:
    return " ".join(str(value).strip().casefold().split())


@dataclass(frozen=True)
class StatusVocabulary:
    """Two-way mapping between canonical states and a studio's wording."""

    #: canonical key -> the studio's exact wording, as written to the document
    wording: dict[str, str]

    @classmethod
    def from_config(cls, statuses: Mapping[str, object]) -> StatusVocabulary:
        return cls(wording={str(k): str(v) for k, v in statuses.items()})

    def canonical(self, raw: str) -> str:
        """The canonical state for a status read off a document.

        Returns:
            One of :data:`CANONICAL`, or ``""`` when the document carries a
            status this profile does not describe. Unknown is returned rather
            than guessed: a fourth status like "Cancelled" must not be quietly
            filed as "not started" and then get picked up as the next free
            session.
        """
        folded = _fold(raw)
        if not folded:
            return UNKNOWN
        for key, value in self.wording.items():
            if _fold(value) == folded:
                return key
        return UNKNOWN

    def label(self, canonical_key: str) -> str:
        """The studio's wording for a canonical state.

        Falls back to the key itself so a profile that omits one state still
        produces readable output instead of an empty string.
        """
        return self.wording.get(canonical_key, canonical_key)
