"""A 429 is retried after the wait the server asked for, not the backoff alone."""

from __future__ import annotations

import requests

from baton.core import retry as retry_mod


def _response(status: int, headers: dict[str, str] | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    return response


def test_a_429_waits_for_retry_after(monkeypatch):
    replies = iter([_response(429, {"Retry-After": "18"}), _response(200)])
    slept: list[float] = []
    monkeypatch.setattr(retry_mod.requests, "request", lambda *a, **k: next(replies))
    monkeypatch.setattr(retry_mod.time, "sleep", slept.append)

    assert retry_mod.http_request("PATCH", "https://example.test").status_code == 200
    assert len(slept) == 1 and slept[0] >= 18


def test_retry_after_is_capped_and_tolerates_junk():
    def wait(value: str) -> float | None:
        return retry_mod.retry_after(_response(429, {"Retry-After": value}))

    assert wait("3600") == retry_mod.RETRY_AFTER_CAP
    assert wait("Wed, 21 Oct 2015 07:28:00 GMT") is None
    assert retry_mod.retry_after(_response(503)) is None
