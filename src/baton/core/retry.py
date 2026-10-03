"""Retry helpers with exponential backoff and jitter.

Ported from the original ``scripts/utils.py``. The behaviour that matters and
is preserved verbatim: a non-retryable HTTP response is *returned* so the
caller can branch on its status, while only exhausted retries raise. Silently
swallowing a 401 as "transient" is how a pipeline ends up looping against a
revoked token.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from typing import Any, TypeVar

import requests

from ..errors import UpstreamError

T = TypeVar("T")

#: Statuses worth trying again: rate limiting and the 5xx family.
RETRYABLE_STATUS: tuple[int, ...] = (429, 500, 502, 503, 504)

#: A Telegram bot token travels in the URL path (``/bot<id>:<secret>/...``),
#: and requests embeds the full URL in its connection errors. One dropped
#: connection would otherwise print the credential to stderr, into a JSON
#: envelope, and into a job log that outlives the run.
_CREDENTIAL_IN_URL = re.compile(r"\b(bot|access_token|api_key|token)=[^&\s]+")
_BOT_TOKEN_IN_PATH = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


def redact(text: str) -> str:
    """Hide credentials that can ride inside a URL before it reaches a message."""
    text = _BOT_TOKEN_IN_PATH.sub("/bot***", text)
    return _CREDENTIAL_IN_URL.sub(r"\1=***", text)


def backoff_delay(attempt: int, *, base: float = 2.0, cap: float = 30.0) -> float:
    """Delay before retry ``attempt`` (0-indexed), capped, with jitter added.

    Jitter matters when several students are pushed in parallel: without it,
    every worker retries on the same tick and re-creates the burst that caused
    the rate limit.
    """
    # Not a security decision: this jitter only decorrelates retry timing.
    return min(cap, base * (2**attempt)) + random.uniform(0, 1)  # noqa: S311


def retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    """Call ``fn`` until it succeeds or ``attempts`` is exhausted.

    Args:
        fn: Zero-argument callable to run.
        attempts: Total tries, including the first.
        exceptions: Exception types treated as transient.

    Returns:
        Whatever ``fn`` returned on its first success.

    Raises:
        The last exception, once every attempt has been used.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except exceptions:
            if attempt + 1 >= attempts:
                raise
            time.sleep(backoff_delay(attempt))
    raise AssertionError("unreachable: retry loop exited without returning or raising")


def http_request(
    method: str,
    url: str,
    *,
    service: str = "upstream",
    timeout: float = 30.0,
    attempts: int = 3,
    **kwargs: Any,
) -> requests.Response:
    """``requests.request`` with a mandatory timeout and transient-fault retries.

    Args:
        method: HTTP verb.
        url: Target URL.
        service: Name used in the raised :class:`UpstreamError` so operators can
            tell Notion from YouTube at a glance.
        timeout: Per-attempt timeout. Always applied: an un-timed request is
            how a nightly job hangs until someone notices the next morning.
        attempts: Total tries, including the first. Statuses in
            :data:`RETRYABLE_STATUS` trigger another attempt.
        **kwargs: Forwarded to ``requests.request``.

    Returns:
        The final response, including non-retryable error responses.

    Raises:
        UpstreamError: Connection or timeout faults persisted across attempts.
    """
    kwargs.setdefault("timeout", timeout)
    transient = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
    last_exc: BaseException | None = None

    for attempt in range(attempts):
        try:
            # A timeout is always present: kwargs.setdefault above guarantees it.
            response = requests.request(method, url, **kwargs)  # noqa: S113
            if response.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
                time.sleep(backoff_delay(attempt))
                continue
            return response
        except transient as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(backoff_delay(attempt))

    raise UpstreamError(
        redact(f"{service} did not respond after {attempts} attempts: {last_exc}"),
        service=service,
        attempts=attempts,
        remedy="Check network access and the service status page, then re-run: "
        "resumable pipelines skip the steps that already succeeded.",
    )
