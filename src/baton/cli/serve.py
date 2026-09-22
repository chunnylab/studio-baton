"""A localhost checklist for marking learners as no longer studying.

The constraints are deliberate:

* **stdlib only** (``http.server``, ``html``, ``urllib.parse``): the CLI has
  no web framework anywhere, and one screen of HTML does not earn one.
* **single-threaded**: ``SqliteStore`` holds one connection, and SQLite
  connections are not thread-safe. ``HTTPServer``, never the threading
  variant, so a request cannot hop threads onto it.
* **bound to 127.0.0.1**: this is a screen for the teacher at the machine
  (over a port forward if remote), not a service.
* **render/apply split**: the page and the write are plain functions the
  tests drive directly; only the socket glue knows a socket exists.

The page never trusts the browser for anything: the form posts learner ids,
the ids are matched against the live roster before anything is written, and
unknown ids are dropped rather than guessed at.
"""

from __future__ import annotations

import html
from collections.abc import Callable, Sequence
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from ..domain.models import Learner
from ..errors import BatonError, ConfigError
from ..exits import Exit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .app import Context

#: A ticked form bigger than this is not a person clicking a studio's roster.
_MAX_BODY = 64 * 1024


def render_checklist(learners: Sequence[Learner], *, label: str) -> str:
    """The page: every active learner as a checkbox, the rest named below.

    Thai names round-trip through ``html.escape`` like any other text; the
    page declares utf-8 so the browser renders them as typed.
    """
    active = [item for item in learners if item.is_active]
    inactive = [item for item in learners if not item.is_active]

    rows = "".join(
        f'<label style="display:block;margin:0.4em 0">'
        f'<input type="checkbox" name="learner" value="{html.escape(item.id)}"> '
        f"{html.escape(item.name)}"
        f"</label>"
        for item in active
    )
    gone = "".join(f"<li>{html.escape(item.name)}</li>" for item in inactive)
    return f"""<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<title>Who stopped studying?</title>
<style>
 body {{ font-family: system-ui, sans-serif; max-width: 32em; margin: 2em auto; }}
 button {{ font-size: 1.1em; padding: 0.5em 1em; margin-top: 1em; }}
</style>
</head>
<body>
<h1>Who stopped studying?</h1>
<p>Tick the {html.escape(label)}s who finished and are not continuing, then submit.
Nothing is deleted: they leave the rosters and name matching, and
<code>baton learner activate</code> brings any of them back.</p>
<form method="POST" action="/">
{rows or f"<p>No active {html.escape(label)}s are recorded.</p>"}
<button type="submit">Mark ticked as no longer studying</button>
</form>
{"<h2>Already inactive</h2><ul>" + gone + "</ul>" if gone else ""}
</body>
</html>"""


def render_confirmation(applied: Sequence[str], failed: Sequence[tuple[str, str]]) -> str:
    """The receipt after a submit: what was marked, what refused, and a way back."""
    done = "".join(f"<li>{html.escape(name)}</li>" for name in applied)
    missed = "".join(
        f"<li>{html.escape(name)}: {html.escape(reason)}</li>" for name, reason in failed
    )
    return f"""<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<title>Marked</title>
<style>
 body {{ font-family: system-ui, sans-serif; max-width: 32em; margin: 2em auto; }}
</style>
</head>
<body>
<h1>Marked as no longer studying</h1>
{f"<ul>{done}</ul>" if done else "<p>Nothing was ticked, so nothing changed.</p>"}
{f"<h2>Could not be marked</h2><ul>{missed}</ul>" if missed else ""}
<p><a href="/">Back to the checklist</a></p>
</body>
</html>"""


def parse_form(body: bytes) -> list[str]:
    """The ticked learner ids from a POST body, in the order the form held them."""
    if len(body) > _MAX_BODY:
        raise ConfigError(
            "The submitted checklist is larger than any roster could be.",
            remedy="Reload the page and submit again.",
        )
    parsed = parse_qs(body.decode("utf-8", errors="replace"))
    return list(parsed.get("learner", []))


def build_checklist_server(
    *,
    port: int,
    page: Callable[[], str],
    apply: Callable[[list[str]], tuple[list[str], list[tuple[str, str]]]],
    log: Callable[[str], None],
) -> HTTPServer:
    """The server, built but not served. Tests drive it on their own thread.

    Raises:
        ConfigError: The port would not bind.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/":
                self.send_error(404)
                return
            body = page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                applied, failed = apply(parse_form(body))
            except ConfigError as exc:
                message = exc.message.encode("utf-8")
                self.send_response(413)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(message)))
                self.end_headers()
                self.wfile.write(message)
                return
            out = render_confirmation(applied, failed).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, format: str, *args: object) -> None:
            # http.server logs to stderr by default anyway; routing it through
            # the callback keeps that decision at the caller.
            log(format % args)

    try:
        return HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise ConfigError(
            f"The checklist could not listen on port {port}: {exc}",
            remedy="Pass --port another number and try again.",
        ) from exc


def serve_checklist(
    *,
    port: int,
    page: Callable[[], str],
    apply: Callable[[list[str]], tuple[list[str], list[tuple[str, str]]]],
    log: Callable[[str], None],
) -> None:
    """Serve the checklist until interrupted.

    Raises:
        ConfigError: The port would not bind.
        KeyboardInterrupt: Propagated to the caller after the socket closes,
            so the caller can still report what was applied.
    """
    server = build_checklist_server(port=port, page=page, apply=apply, log=log)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def run_checklist(ctx: Context, *, port: int) -> Exit:
    """`learner deactivate --serve`: open the checklist, apply the ticks."""
    from ..adapters.db import store_scope

    deactivated: list[str] = []
    with store_scope(ctx.config) as store:
        label = ctx.config.label("learner")

        def page() -> str:
            return render_checklist(store.list_learners(), label=label)

        def apply(ids: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
            # The form is only ever a suggestion: ids are matched against the
            # live roster, and a stale or forged one is dropped, not acted on.
            roster = {item.id: item for item in store.list_learners()}
            applied: list[str] = []
            failed: list[tuple[str, str]] = []
            for learner_id in dict.fromkeys(ids):
                learner = roster.get(learner_id)
                if learner is None:
                    continue
                try:
                    store.set_active(learner.id, False)
                except BatonError as exc:
                    failed.append((learner.name, exc.message))
                    continue
                if learner.name not in deactivated:
                    deactivated.append(learner.name)
                applied.append(learner.name)
            return applied, failed

        ctx.report.step(f"checklist ready: http://127.0.0.1:{port}/ (Ctrl-C to finish)")
        with suppress(KeyboardInterrupt):
            # The teacher closed the screen, not the studio: a normal exit whose
            # envelope reports what actually landed.
            serve_checklist(port=port, page=page, apply=apply, log=ctx.report.step)

    ctx.report.result(
        {
            "served": True,
            "host": "127.0.0.1",
            "port": port,
            "deactivated": deactivated,
            "count": len(deactivated),
        },
        human=(
            f"Marked {len(deactivated)} as no longer studying: {', '.join(deactivated)}."
            if deactivated
            else "The checklist closed without marking anyone."
        ),
    )
    return Exit.OK
