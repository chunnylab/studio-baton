"""A messenger that refuses to deliver the same message twice.

Wrapping rather than teaching each pipeline to check is deliberate. There are
four commands that send (`send lesson`, `send recording`, `send video`, and the
batch), each with its own composer, and a check written into all four is a
check that the fifth one will forget. Sitting on the protocol boundary means
the guarantee is "nothing leaves this process twice", which is the guarantee
worth having, and a future send path inherits it without knowing it exists.

See :mod:`baton.core.receipts` for what a receipt is and why it is not the
message.
"""

from __future__ import annotations

from typing import Any

from ...core.receipts import Receipts
from .base import Messenger, SendOutcome


class GuardedMessenger:
    """A :class:`Messenger` that records what it sends and refuses repeats.

    Args:
        inner: The real messenger. Everything except :meth:`send` is passed
            straight through.
        receipts: Where deliveries are recorded.
        again: The caller passed ``--again``; deliver even if a receipt exists.
        what: How the refusal should name this message to a person, e.g.
            ``"Ada Whitfield's lesson 12 summary"``.
        key: What identifies this message regardless of how it was worded:
            ``"lesson|17|3"``. Required in practice for anything a composer
            varies: `send lesson` chooses its opening and closing phrase at
            random, so the same summary is never the same string twice. Omit it
            and the message text is used instead, which is right for a message
            that is fully determined by its content.

    The receipt is written *after* the platform accepts the message, so a send
    that fails leaves nothing behind to block the retry it deserves. The window
    between acceptance and the write is the one place a duplicate can still
    slip through: it is microseconds of local file I/O against a network round
    trip, and closing it fully would mean writing a receipt for a send that may
    never happen, which is the worse failure: a summary silently never sent.
    """

    def __init__(
        self,
        inner: Messenger,
        receipts: Receipts,
        *,
        again: bool = False,
        what: str = "This message",
        key: str | None = None,
    ) -> None:
        self._inner = inner
        self._receipts = receipts
        self._again = again
        self._what = what
        self._key = key

    # -- Messenger ---------------------------------------------------------

    def resolve(self, name: str) -> str:
        return self._inner.resolve(name)

    def health(self) -> None:
        self._inner.health()

    def _digest(self, recipient_id: str, text: str) -> tuple[str, str]:
        service = str(getattr(self._inner, "service", "chat"))
        return service, Receipts.digest(service, recipient_id, self._key if self._key else text)

    def receipt(self, recipient_id: str, text: str = "") -> dict[str, Any] | None:
        """The receipt that would refuse :meth:`send`, if any. Reads only.

        A dry run asks this, so a gate that passes on paper does not hide the
        refusal the real send would meet.
        """
        if self._again:
            return None
        return self._receipts.find(self._digest(recipient_id, text)[1])

    def send(self, recipient_id: str, text: str) -> SendOutcome:
        service, key = self._digest(recipient_id, text)
        self._receipts.guard(key, what=self._what, again=self._again)

        outcome = self._inner.send(recipient_id, text)

        if outcome.sent:
            self._receipts.record(key, service=service, recipient=recipient_id, what=self._what)
        return outcome

    def __getattr__(self, item: str) -> Any:
        """Anything else belongs to the driver underneath.

        Drivers carry extras the protocol does not name: `service`, the
        webhook's health endpoint, and code that reaches for one should not
        have to know a guard is in the way.
        """
        return getattr(self._inner, item)


__all__ = ["GuardedMessenger"]
