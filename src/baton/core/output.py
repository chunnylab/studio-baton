"""Two ways to report the same result: for a person, and for a program.

Every command produces one result object. In human mode it is printed as
aligned text; with ``--json`` it is printed as a single JSON document on
stdout and nothing else. An agent driving Baton always passes ``--json`` and
reads the envelope, which has the same shape for success and failure.

Progress and diagnostics go to stderr in both modes, so ``--json`` output stays
parseable even when a pipeline is chatty.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, TextIO


@dataclass
class Reporter:
    """Renders results and progress for one command invocation.

    Streams are resolved at write time rather than captured at construction, so
    a caller that redirects ``sys.stdout`` (a test harness, or a wrapper
    piping output) sees what it expects.
    """

    json_mode: bool = False
    quiet: bool = False

    @property
    def _out(self) -> TextIO:
        return sys.stdout

    @property
    def _err(self) -> TextIO:
        return sys.stderr

    # -- progress (stderr, never part of the JSON document) ----------------

    def step(self, message: str) -> None:
        """Announce a stage of a long-running command."""
        if not self.quiet:
            print(f"→ {message}", file=self._err, flush=True)

    def ok(self, message: str) -> None:
        """Note a completed stage."""
        if not self.quiet:
            print(f"✓ {message}", file=self._err, flush=True)

    def warn(self, message: str) -> None:
        """Note something the operator should know but that does not stop work."""
        print(f"! {message}", file=self._err, flush=True)

    # -- results (stdout) --------------------------------------------------

    def result(self, payload: dict[str, Any], *, human: str | None = None, ok: bool = True) -> None:
        """Emit the command's result.

        Args:
            payload: JSON-serialisable result body. ``ok`` is added.
            human: Text shown instead of the payload when not in JSON mode.
                Falls back to pretty-printed JSON when omitted.
            ok: Whether the command succeeded. Set ``False`` for commands whose
                whole output *is* the report of what failed, such as ``doctor``:
                the caller still needs the body, not an error envelope.
        """
        if self.json_mode:
            document = {"ok": ok, **payload}
            print(json.dumps(document, ensure_ascii=False, indent=2), file=self._out)
            return
        if human is not None:
            if human:
                print(human, file=self._out)
            return
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=self._out)

    def failure(self, payload: dict[str, Any], *, human: str) -> None:
        """Emit a failure envelope.

        In JSON mode the envelope goes to stdout so a caller reads results and
        errors from one place; in human mode the text goes to stderr so shell
        pipelines are not polluted by error prose.

        ``ok`` is guaranteed here rather than assumed of the caller. Commands
        whose refusal *is* their report: `prep`, `course`: pass the report
        itself, which is a body and not an envelope, and the field went missing
        on exactly the paths a caller most needs to branch on.
        """
        if self.json_mode:
            document = {"ok": False, **payload}
            print(json.dumps(document, ensure_ascii=False, indent=2), file=self._out)
        else:
            print(human, file=self._err)


def format_error(payload: dict[str, Any]) -> str:
    """Render an error envelope as text for a person.

    Structured detail is shown as indented lines rather than raw JSON: a
    blocked send should read like a checklist, because that is how it gets
    fixed.
    """
    lines = [f"✗ {payload.get('message', 'Command failed.')}"]

    details = payload.get("details") or {}
    for key in ("missing", "violations", "candidates"):
        items = details.get(key)
        if not items:
            continue
        lines.append("")
        lines.append(f"  {key}:")
        for item in items:
            if isinstance(item, dict):
                label = item.get("field") or item.get("name") or item.get("path") or ""
                reason = item.get("reason") or item.get("message") or item.get("hint") or ""
                lines.append(f"    - {label}{': ' if label and reason else ''}{reason}")
            else:
                lines.append(f"    - {item}")

    if payload.get("remedy"):
        lines.append("")
        lines.append(f"  {payload['remedy']}")
    return "\n".join(lines)
