"""``baton doctor``: check the installation before it is trusted with real work.

This is the merged descendant of the per-skill preflight scripts. It runs every
cheap check that can fail at 2am and reports all of them at once, because
finding three problems in one run beats discovering them one re-run at a time.

Doctor changes nothing a caller can observe and never falls back to a secondary
store: its job is to notice that the primary is down, not to paper over it. The
one thing it writes is a probe file under the state directory, created and
deleted again to prove the directory is writable, which also creates the state
directory if it is missing.
"""

from __future__ import annotations

import argparse
import os
import shutil
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..adapters import cal as cal_adapters
from ..adapters import chat as chat_adapters
from ..adapters import db as db_adapters
from ..adapters import docs as doc_adapters
from ..core import i18n
from ..errors import BatonError
from ..exits import Exit

if TYPE_CHECKING:
    from .app import Context

KNOWN_DB_DRIVERS = db_adapters.DRIVERS
KNOWN_DOC_DRIVERS = doc_adapters.DRIVERS
KNOWN_CHAT_DRIVERS = chat_adapters.DRIVERS
KNOWN_CALENDAR_DRIVERS = cal_adapters.DRIVERS

#: The credentials the calendar needs. Checked, but never required: a studio can
#: run Baton with no calendar at all, and a fresh profile should not fail
#: `doctor` for a Google project its owner has not created yet.
CALENDAR_SECRETS = ("refresh_token_env", "client_id_env", "client_secret_env")


@dataclass
class Check:
    """One named pass/fail observation."""

    name: str
    passed: bool
    detail: str = ""
    remedy: str = ""


@dataclass
class Report:
    """Accumulated checks for one doctor run."""

    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "", remedy: str = "") -> None:
        self.checks.append(Check(name=name, passed=passed, detail=detail, remedy=remedy))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``doctor`` command."""
    parser = subparsers.add_parser(
        "doctor",
        help="Check configuration, credentials, and drivers.",
        description="Run every cheap health check and report all failures at once.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also require credentials for every database and chat driver this build knows, "
        "not only the selected pair.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip the checks that contact a service. Config and schema mapping are still checked.",
    )
    parser.set_defaults(handler=handle)


def _check_secret(
    ctx: Context, report: Report, t: i18n.Translator, dotted: str, *, required: bool
) -> None:
    """Record whether the credential named by ``dotted`` is present."""
    env_name = ctx.config.get(dotted, None)
    if not env_name:
        return
    label = t("doctor.check.secret", env=env_name)
    present = bool(os.environ.get(str(env_name), ""))
    if present:
        report.add(label, passed=True)
    elif required:
        report.add(
            label,
            passed=False,
            detail="not set",
            remedy=f"Export {env_name}, or add it to the profile's .env.",
        )
    else:
        report.add(label, passed=True, detail="not set (optional)")


def _check_driver(
    ctx: Context, report: Report, t: i18n.Translator, dotted: str, known: tuple[str, ...], kind: str
) -> str:
    """Record whether the configured driver name is one this build implements."""
    driver = str(ctx.config.get(dotted, ""))
    label = t("doctor.check.driver", kind=kind, driver=driver or "-")
    if driver in known:
        report.add(label, passed=True)
    else:
        report.add(
            label,
            passed=False,
            detail=f"expected one of: {', '.join(known)}",
            remedy=f"Set `{dotted}` to a supported driver.",
        )
    return driver


def _check_schema(ctx: Context, report: Report) -> None:
    """Resolve the configured table and column names without touching a service.

    Catches the single most common misconfiguration (a column renamed in
    baton.yaml that does not exist, or a name that is not a legal identifier)
    while still working on a laptop with no network.
    """
    from ..adapters.db.mapping import Schema

    try:
        Schema.from_config(ctx.config)
        report.add("Database schema mapping is complete and valid", passed=True)
    except BatonError as err:
        report.add(
            "Database schema mapping is complete and valid",
            passed=False,
            detail=err.message,
            remedy=err.remedy or "",
        )

    try:
        doc_adapters.PreservePolicy.from_config(ctx.config.get("docs.preserve", []))
        report.add("Document preserve rules parse", passed=True)
    except BatonError as err:
        report.add(
            "Document preserve rules parse",
            passed=False,
            detail=err.message,
            remedy=err.remedy or "",
        )


def _calendar_configured(ctx: Context) -> bool:
    """Whether every credential the configured calendar needs is present."""
    driver = str(ctx.config.get("calendar.driver", ""))
    if driver not in KNOWN_CALENDAR_DRIVERS:
        return False
    for key in CALENDAR_SECRETS:
        env_name = ctx.config.get(f"calendar.{driver}.{key}", None)
        if not env_name or not os.environ.get(str(env_name), ""):
            return False
    return True


def _check_encoder(ctx: Context, report: Report, *, required: bool) -> None:
    """Whether the configured encoder binary is on PATH.

    `baton video` shells out to it, so a missing binary is a pipeline that dies
    at the encode step after the clips have already been downloaded.
    """
    if str(ctx.config.get("media.encode.driver", "")) != "ffmpeg":
        return
    binary = str(ctx.config.get("media.encode.binary", "ffmpeg"))
    found = shutil.which(binary)
    if found:
        report.add(f"Encoder `{binary}` is on PATH", passed=True, detail=found)
    elif required:
        report.add(
            f"Encoder `{binary}` is on PATH",
            passed=False,
            detail="not found",
            remedy=f"Install {binary}, or set media.encode.binary to its full path.",
        )
    else:
        report.add(
            f"Encoder `{binary}` is on PATH",
            passed=True,
            detail="not found: `baton video` cannot encode until it is",
        )


def _probe(report: Report, label: str, run: Callable[[], None]) -> None:
    """Record one reachability check, whatever shape its failure arrives in.

    `BatonError` carries its own remedy. Anything else (a vendor SDK raising
    its own exception type, a socket giving up) is still a failed check, not a
    traceback: doctor exists to name every problem in one run, and a stack trace
    ends the run at the first one.
    """
    try:
        run()
        report.add(label, passed=True)
    except BatonError as err:
        report.add(label, passed=False, detail=err.message, remedy=err.remedy or "")
    except Exception as exc:  # an unexpected failure is still a failed check
        report.add(
            label,
            passed=False,
            detail=f"{type(exc).__name__}: {exc}",
            remedy="Unexpected failure from this driver; the detail above is its own message.",
        )


#: Seconds between page reads. Notion averages three requests a second per
#: integration, and a burst past it did not always fail loudly: on the flood
#: day a scan of a hundred databases came back short with no error at all.
PAGE_READ_PACING = 0.35


def _check_pages(ctx: Context, report: Report, store: Any) -> None:
    """Read each active learner's latest session page, the way work will.

    A 404 and an outage need different fixes, so they are reported as two
    checks: a page Notion cannot see is a sharing problem a person solves in
    Notion, and saying "the service may be down" about it sends them to wait
    for something that is not coming.
    """
    import time

    docs = doc_adapters.open_docs(ctx.config)
    unseen: list[str] = []
    unanswered: list[str] = []
    read = 0
    for index, learner in enumerate(item for item in store.list_learners() if item.is_active):
        with_page = [s for s in store.list_sessions(learner.id) if s.doc_id]
        if not with_page:
            continue
        latest = max(with_page, key=lambda session: session.number)
        if index:
            time.sleep(PAGE_READ_PACING)
        try:
            docs.get_status(latest.doc_id, with_blocks=False)
            read += 1
        except BatonError as err:
            if err.details.get("status_code") == 404:
                unseen.append(learner.name)
            else:
                unanswered.append(learner.name)

    total = read + len(unseen) + len(unanswered)
    label = "Every active learner's latest session page can be read"
    if not unseen and not unanswered:
        report.add(label, passed=True, detail=f"{read} page(s)")
        return
    if unseen:
        report.add(
            label,
            passed=False,
            detail=f"{len(unseen)} of {total} not visible to the integration: {', '.join(unseen)}",
            remedy="This is sharing, not an outage. Notion answers 404 for a page "
            "the integration was never given. Share the page that holds these "
            "learners' sessions (⋯ → Connections): sharing a parent covers "
            "everything under it. If the page was deleted instead, fix the "
            "session's doc id.",
        )
    if unanswered:
        report.add(
            "Notion answered every page read",
            passed=False,
            detail=f"{len(unanswered)} of {total} failed upstream: {', '.join(unanswered)}",
            remedy="Rate limit or outage, not sharing: these pages were not "
            "refused, they were not answered. Wait a minute and run doctor again.",
        )


def _check_db_url(ctx: Context, report: Report, driver: str) -> None:
    """Catch a Supabase URL given to the postgrest driver before any request.

    The two drivers differ in headers and path, and the wrong pairing fails
    far from its cause: 401 for the missing ``apikey``, or a doubled
    ``/rest/v1`` the gateway answers with an error that points elsewhere.
    """
    from ..adapters.db.postgrest import url_problem

    env_name = ctx.config.get(f"db.{driver}.url_env", None)
    url = os.environ.get(str(env_name), "") if env_name else ""
    if not url:
        return
    problem = url_problem(driver, url)
    label = "Database URL suits the selected driver"
    if problem is None:
        report.add(label, passed=True)
    else:
        report.add(label, passed=False, detail=problem[0], remedy=problem[1])


def _check_reachable(ctx: Context, report: Report) -> None:
    """Open each store and prove it answers.

    The database check reads one row from every configured table, so a table
    that exists under a different name fails here rather than at 2am inside a
    pipeline.
    """
    store: Any = None

    def database() -> None:
        nonlocal store
        store = db_adapters.open_store(ctx.config)
        store.health()

    before = len(report.failed)
    try:
        _probe(report, "Database is reachable and every table resolves", database)
        _probe(
            report,
            "Document store accepts the credentials",
            lambda: doc_adapters.open_docs(ctx.config).health(),
        )
        # A token that works proves nothing about the pages: on the day this
        # check was written, doctor was green while a whole branch of session
        # pages answered 404 because it had never been shared. Only worth
        # asking once both stores answered at all.
        if store is not None and len(report.failed) == before:
            _check_pages(ctx, report, store)
    finally:
        if store is not None:
            store.close()

    # Only when the credentials are actually there. A profile that has never
    # been pointed at a Google project is not broken, but one whose refresh
    # token has quietly expired is, and `learner in-progress` reads the
    # calendar every morning, so that expiry has to surface here rather than
    # halfway through the first question of the day.
    if _calendar_configured(ctx):
        _probe(
            report,
            "Calendar accepts the credentials",
            lambda: cal_adapters.open_calendar(ctx.config).health(),
        )


def handle(ctx: Context) -> Exit:
    """Run the checks and report."""
    report = Report()

    # Configuration has to load before anything else can be checked; a failure
    # here raises ConfigError and is rendered by the CLI shell.
    config = ctx.config
    t = ctx.t

    report.add(t("doctor.check.config"), passed=True, detail=str(config.config_file))

    locale = config.locale
    report.add(
        t("doctor.check.locale"),
        passed=locale in i18n.available_locales(),
        detail=locale,
        remedy=f"Available locales: {', '.join(i18n.available_locales())}.",
    )

    try:
        zoneinfo.ZoneInfo(config.timezone)
        report.add(t("doctor.check.timezone"), passed=True, detail=config.timezone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        report.add(
            t("doctor.check.timezone"),
            passed=False,
            detail=config.timezone,
            remedy="Use an IANA timezone name such as Asia/Bangkok or Europe/London.",
        )

    probe = config.state_dir / ".doctor-write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report.add(t("doctor.check.state_dir"), passed=True, detail=str(config.state_dir))
    except OSError as exc:
        report.add(
            t("doctor.check.state_dir"),
            passed=False,
            detail=f"{config.state_dir}: {exc}",
            remedy="Grant write access, or point BATON_STATE_DIR somewhere writable.",
        )

    # A claim about the design rather than a setting: there is no client to
    # select, so a profile naming a provider is a profile waiting for a call
    # that never comes. Better said here than discovered at the first summary.
    provider = str(config.get("llm.provider", "none")).strip().lower()
    report.add(
        t("doctor.check.llm"),
        passed=provider in ("", "none"),
        detail=provider or "none",
        remedy="Baton has no model client: summaries are written by whichever "
        "agent drives the CLI and submitted through `lesson ingest`. Set "
        "`llm.provider: none`.",
    )

    db_driver = _check_driver(ctx, report, t, "db.driver", KNOWN_DB_DRIVERS, "database")
    doc_driver = _check_driver(ctx, report, t, "docs.driver", KNOWN_DOC_DRIVERS, "documents")
    chat_driver = _check_driver(ctx, report, t, "chat.driver", KNOWN_CHAT_DRIVERS, "chat")
    cal_driver = _check_driver(
        ctx, report, t, "calendar.driver", KNOWN_CALENDAR_DRIVERS, "calendar"
    )

    selected = {
        f"db.{db_driver}": ("url_env", "key_env", "jwt_env"),
        f"docs.{doc_driver}": ("token_env",),
        f"chat.{chat_driver}": ("token_env", "url_env"),
    }
    for section, keys in selected.items():
        for key in keys:
            _check_secret(ctx, report, t, f"{section}.{key}", required=True)

    for key in CALENDAR_SECRETS:
        _check_secret(ctx, report, t, f"calendar.{cal_driver}.{key}", required=ctx.args.strict)

    _check_encoder(ctx, report, required=ctx.args.strict)

    _check_db_url(ctx, report, db_driver)
    _check_schema(ctx, report)
    if not ctx.args.offline:
        _check_reachable(ctx, report)

    if ctx.args.strict:
        for kind, drivers in (
            ("db", KNOWN_DB_DRIVERS),
            ("chat", KNOWN_CHAT_DRIVERS),
        ):
            for driver in drivers:
                for key in ("token_env", "url_env", "key_env", "jwt_env"):
                    _check_secret(ctx, report, t, f"{kind}.{driver}.{key}", required=True)

    total = len(report.checks)
    failed = report.failed

    payload = {
        # First field on purpose. `doctor` is the command a harness runs before
        # it trusts anything, and "which Baton am I talking to" is part of that
        # answer: a skill written for a newer version otherwise finds out by
        # getting `usage` back from a command that simply does not exist yet.
        "version": __version__,
        "profile": str(config.profile_dir),
        "config_file": str(config.config_file),
        "locale": locale,
        "timezone": config.timezone,
        "drivers": {
            "db": db_driver,
            "docs": doc_driver,
            "chat": chat_driver,
            "calendar": cal_driver,
        },
        "checks": [
            {
                "name": c.name,
                "passed": c.passed,
                "detail": c.detail,
                **({"remedy": c.remedy} if c.remedy and not c.passed else {}),
            }
            for c in report.checks
        ],
        "passed": total - len(failed),
        "failed": len(failed),
    }

    lines = [
        t("doctor.title"),
        # No catalogue key: a version string reads the same in every language,
        # and inventing one to translate the word "baton" helps nobody.
        f"  baton {__version__}",
        f"  {t('doctor.profile')}: {config.profile_dir}",
        f"  {t('doctor.locale')}: {locale}    {t('doctor.timezone')}: {config.timezone}",
        "",
    ]
    for check in report.checks:
        mark = "✓" if check.passed else "✗"
        suffix = f"  ({check.detail})" if check.detail else ""
        lines.append(f"  {mark} {check.name}{suffix}")
        if not check.passed and check.remedy:
            lines.append(f"      {check.remedy}")
    lines.append("")
    lines.append(
        t("doctor.pass") if not failed else t("doctor.fail", failed=len(failed), total=total)
    )

    ctx.report.result(payload, human="\n".join(lines), ok=not failed)

    if failed:
        # The full report is printed above rather than replaced by an error, so
        # the operator sees every problem in one pass. The exit code still says
        # "configuration is broken", but quietly, since the detail is already
        # on screen.
        return Exit.CONFIG
    return Exit.OK
