"""A person's confirmed answer to "send this lesson with no recording?"

`send lesson` refuses a session with no video (:mod:`baton.exits` ``NEEDS_HUMAN``)
and used to accept ``--without-video`` as a bare flag past that stop. A bare
flag is something anything that can run a command line can pass, including
an agent that has never asked anyone, and is following the CLI's own contract
faithfully by doing so.

What is checked instead is a code nobody driving Baton can produce on their
own: :func:`request` sends it to a real person over a channel Baton already
trusts (the studio's configured messenger), and never returns it to whatever
called the command. Only a person reading their own phone has it. ``send
lesson --without-video <code>`` is then not a bypass: it is where that
person's answer re-enters the process that asked the question, and
:func:`verify_and_consume` accepts it exactly once.

Which person the code reaches is the studio's to name, once, in the profile
(``summary.video_waiver.recipient``), not a per-invocation choice for
whatever is holding the command line. The question "send this with no
recording?" belongs to whoever answers for the studio; the first shipped
remedy pointed at the lesson's own recipient instead, and an agent following
it faithfully texted the code to someone who could have answered the owner's
question without the owner ever seeing it.

Scoped to one learner's one session and expiring on its own, so a code
overheard or logged somewhere is not a standing key to anything.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..errors import NeedsHumanError
from . import jsonio

#: Filename under the state directory.
WAIVERS_FILE = "video-waivers.json"

#: How long a code is live. Long enough for a message to be read and answered
#: without anyone watching a clock; short enough that a code from last week is
#: not still sitting in someone's chat history as a working key.
DEFAULT_TTL_MINUTES = 30.0

#: Excludes 0/O and 1/I/L: read aloud or typed on a phone, none of these six
#: should ever need a second look to tell apart.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_LENGTH = 6


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _key(learner_id: str, session_number: Any) -> str:
    return f"{learner_id}|{session_number}"


def generate_code() -> str:
    """A fresh code. A free function so a caller previewing one (``--dry-run``)
    does not have to construct a store to see the shape of what it would send."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))


@dataclass
class VideoWaivers:
    """The confirmation codes outstanding for this profile, keyed by
    ``learner_id|session_number``.

    Args:
        path: The waivers file. Created on first write.
        ttl_minutes: How long a requested code stays answerable.
    """

    path: Path
    ttl_minutes: float = DEFAULT_TTL_MINUTES

    @classmethod
    def for_state(cls, state_dir: Path, ttl_minutes: float = DEFAULT_TTL_MINUTES) -> VideoWaivers:
        return cls(Path(state_dir) / WAIVERS_FILE, ttl_minutes=ttl_minutes)

    # -- reading -------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        raw = jsonio.read_json(self.path, default={})
        entries = raw.get("waivers") if isinstance(raw, dict) else None
        return entries if isinstance(entries, dict) else {}

    def _save(self, entries: dict[str, Any]) -> None:
        jsonio.write_json(self.path, {"version": 1, "waivers": entries})

    # -- requesting ------------------------------------------------------

    def request(self, learner_id: str, session_number: Any, *, sent_to: str) -> str:
        """Issue a fresh code for this learner's session, replacing any live one.

        Returns:
            The code. The caller's only job with it is putting it in the
            message a person will read, never in anything this process
            prints, logs, or returns as a command's own result.
        """
        entries = self._load()
        code = generate_code()
        now = _now()
        entries[_key(learner_id, session_number)] = {
            "code": code,
            "sent_to": sent_to,
            "requested_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(minutes=self.ttl_minutes)).isoformat(timespec="seconds"),
        }
        self._save(entries)
        return code

    # -- answering ---------------------------------------------------------

    def verify_and_consume(
        self,
        learner_id: str,
        session_number: Any,
        code: str,
        *,
        learner_name: str,
        label: str,
        recipient_hint: str = "",
    ) -> None:
        """Accept a person's answer, once.

        Args:
            recipient_hint: The contact the CLI's remedy should name when the
                profile has one configured, so a refused code steers the
                person asking toward the same approver the original request
                reached. Empty means the remedy stays generic.

        Raises:
            NeedsHumanError: No code was requested for this session, the one
                on file has expired, or ``code`` does not match it. The
                message distinguishes the three so a person is not left
                guessing whether to ask again or to type more carefully.

        A matching code is deleted before this returns, whether or not the
        caller goes on to send anything: a code answers one question, and
        leaving it live would let a killed-and-retried command spend it twice
        without anyone having answered a second time.
        """
        entries = self._load()
        entry = entries.get(_key(learner_id, session_number))

        def refuse(reason: str) -> None:
            target = f"--to {recipient_hint}" if recipient_hint else "--to <contact>"
            raise NeedsHumanError(
                f"{reason} for {learner_name}'s {label} {session_number}.",
                candidates=[],
                details={"learner": learner_name, "session_number": session_number},
                remedy="Nothing was sent. Run `baton send video-waiver "
                f'"{learner_name}" {target}` to text a fresh code, then '
                "re-run this with --without-video <the code they were sent>.",
            )

        if not isinstance(entry, dict):
            refuse("No confirmation code has been requested")
            return
        if _now() > (_parse(str(entry.get("expires_at", ""))) or _now()):
            del entries[_key(learner_id, session_number)]
            self._save(entries)
            refuse("The confirmation code for this session has expired")
            return
        if not secrets.compare_digest(str(entry.get("code", "")), str(code)):
            refuse("That confirmation code does not match the one on file")
            return

        del entries[_key(learner_id, session_number)]
        self._save(entries)


__all__ = ["DEFAULT_TTL_MINUTES", "VideoWaivers", "generate_code"]
