"""``baton init``: create a working profile.

The first two minutes decide whether anyone tries this at all, so ``init``
produces something that already runs: a config, an ``.env`` naming every
variable that profile needs, a database with the schema in it, and optionally
sample data to look at. It finishes by running the checks itself rather than
telling the reader to.

It is also fully non-interactive with flags, because the first thing a studio
does after trying it is put it in a container.
"""

from __future__ import annotations

import argparse
import sqlite3
import zoneinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..core import i18n
from ..core.config import packaged_defaults
from ..errors import UsageError
from ..exits import Exit

if TYPE_CHECKING:
    from .app import Context

# Packaged, not repo-relative: `baton init` has to work for someone who
# pip-installed Baton and has no checkout to read SQL out of.
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

DB_DRIVERS = ("sqlite", "supabase", "postgrest")
CHAT_DRIVERS = ("line", "telegram", "webhook")

#: Credentials each choice needs, so the generated .env lists exactly those.
_ENV_FOR = {
    "docs": [("NOTION_API_TOKEN", "Notion integration token, from notion.so/my-integrations")],
    "db:supabase": [
        (
            "SUPABASE_PROJECT_URL",
            "Your Supabase project URL, https://<ref>.supabase.co, without /rest/v1",
        ),
        ("SUPABASE_PROJECT_API", "A service-role or anon key with access to the tables"),
    ],
    "db:postgrest": [
        (
            "POSTGREST_URL",
            "Base URL of your PostgREST instance. A *.supabase.co URL belongs "
            "to --db supabase instead: this driver sends no apikey header",
        ),
        ("POSTGREST_JWT", "JWT for a role that can read and write the tables"),
    ],
    "chat:line": [
        ("LINE_CHANNEL_ACCESS_TOKEN", "Channel access token from the LINE Developers console")
    ],
    "chat:telegram": [("TELEGRAM_BOT_TOKEN", "Bot token from @BotFather")],
    "chat:webhook": [
        ("BATON_WEBHOOK_URL", "Where to POST messages"),
        ("BATON_WEBHOOK_SECRET", "Optional shared secret; requests are signed with it"),
    ],
    "notes": [("BATON_NOTES_PARENT", "Page id that quick notes are created under")],
}


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``init`` command."""
    parser = subparsers.add_parser(
        "init",
        help="Create a profile you can run straight away.",
        description=(
            "Writes baton.yaml and .env, creates the database, and runs the "
            "offline checks. Ask it questions, or pass every answer as a flag."
        ),
    )
    parser.add_argument("directory", nargs="?", default=".", help="Where to create the profile.")
    parser.add_argument("--locale", choices=i18n.available_locales(), help="Message language.")
    parser.add_argument("--timezone", help="IANA name, e.g. Asia/Bangkok.")
    parser.add_argument("--db", choices=DB_DRIVERS, help="Where learner records live.")
    parser.add_argument("--chat", choices=CHAT_DRIVERS, help="How messages are sent.")
    parser.add_argument("--learner-label", help="What you call a student.")
    parser.add_argument("--learner-plural", help="Plural of that. Defaults sensibly.")
    parser.add_argument("--session-label", help="What you call one session.")
    parser.add_argument("--session-plural", help="Plural of that. Defaults sensibly.")
    parser.add_argument(
        "--sample-data",
        action="store_true",
        help="Insert invented learners so there is something to look at.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Take the defaults for anything not given, and ask nothing.",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite an existing profile.")
    parser.set_defaults(handler=handle)

    schema = subparsers.add_parser(
        "schema",
        help="Print the SQL for a database you manage yourself.",
        description=(
            "Someone who pip-installed Baton has no checkout to copy SQL out "
            "of, so it is printed on request."
        ),
    )
    schema.add_argument(
        "driver",
        choices=("sqlite", "postgres", "sample-data"),
        help="Which script to print.",
    )
    schema.set_defaults(handler=handle_schema)


def handle_schema(ctx: Context) -> Exit:
    """Print a migration script."""
    filename = {
        "sqlite": "sqlite.sql",
        "postgres": "postgres.sql",
        "sample-data": "seed_example.sql",
    }[ctx.args.driver]
    sql = (MIGRATIONS / filename).read_text(encoding="utf-8")

    ctx.report.result({"driver": ctx.args.driver, "sql": sql}, human=sql.rstrip())
    return Exit.OK


def _ask(ctx: Context, prompt: str, default: str, choices: tuple[str, ...] | None = None) -> str:
    """One question, with the default in brackets.

    Non-interactive when ``--yes`` is passed or stdin is not a terminal: a
    scaffolder that blocks on a prompt inside a Dockerfile is a scaffolder
    nobody can automate.
    """
    import sys

    if ctx.args.yes or not sys.stdin.isatty():
        return default

    suffix = f" ({'/'.join(choices)})" if choices else ""
    while True:
        answer = input(f"{prompt}{suffix} [{default}]: ").strip()
        if not answer:
            return default
        if choices and answer not in choices:
            print(f"  Choose one of: {', '.join(choices)}")
            continue
        return answer


def pluralise(label: str) -> str:
    """A plural for a label, guessed only where guessing is safe.

    Appending "s" is right for most English nouns and wrong everywhere else:
    Thai has no plural marker at all, so "นักเรียนs" is simply broken text on
    every page it reaches. The guess is therefore limited to plain ASCII words,
    and anything else is left as it is. `--learner-plural` overrides either way.
    """
    if label.isascii() and label.isalpha():
        return label + ("es" if label.endswith(("s", "x", "z", "ch", "sh")) else "s")
    return label


def _profile_config(answers: dict[str, str]) -> dict[str, Any]:
    """The baton.yaml a set of answers produces.

    Only what differs from the packaged defaults, so the file stays short
    enough to read and edit.
    """
    config: dict[str, Any] = {
        "version": 1,
        "locale": answers["locale"],
        "timezone": answers["timezone"],
        "labels": {
            "learner": answers["learner_label"],
            "learners": answers["learner_plural"],
            "session": answers["session_label"],
            "sessions": answers["session_plural"],
        },
        "db": {"driver": answers["db"]},
        "chat": {
            "driver": answers["chat"],
            "contacts": {
                "me": {"id_env": "BATON_CONTACT_ME", "aliases": ["me", "teacher"]},
            },
        },
    }
    if answers["db"] == "sqlite":
        config["db"]["sqlite"] = {"path": "data/studio.db"}
    return config


def _env_template(answers: dict[str, str]) -> str:
    """An .env listing exactly the variables this profile needs."""
    lines = [
        "# Credentials for this profile. Baton loads this file into the",
        "# environment when it loads the profile; a variable already exported in",
        "# your shell wins over the value here. Secrets never go in baton.yaml.",
        "#",
        "# Keep this file out of version control. Nothing here excludes it for you:",
        "# add `.env` to the .gitignore of whatever repository this profile lives in.",
        "",
    ]
    groups = [
        ("Session documents", _ENV_FOR["docs"]),
        ("Messaging", _ENV_FOR[f"chat:{answers['chat']}"]),
        ("Quick notes", _ENV_FOR["notes"]),
    ]
    if answers["db"] != "sqlite":
        groups.insert(0, ("Learner records", _ENV_FOR[f"db:{answers['db']}"]))

    for heading, entries in groups:
        lines.append(f"# --- {heading} " + "-" * max(0, 60 - len(heading)))
        for name, description in entries:
            lines.append(f"# {description}")
            lines.append(f"{name}=")
            lines.append("")

    lines.append("# --- Contacts " + "-" * 61)
    lines.append("# The platform id of whoever receives lesson messages.")
    lines.append("BATON_CONTACT_ME=")
    lines.append("")
    return "\n".join(lines)


def _upgrade_database(connection: sqlite3.Connection) -> None:
    """Bring a database the schema predates up to the current columns.

    ``CREATE TABLE IF NOT EXISTS`` leaves an older `works` table exactly as it
    was, which is correct for tables baton does not own, but means re-running
    ``init`` on an existing profile would never add a column Baton now expects.
    The added ones are therefore applied as explicit, idempotent ALTERs here,
    so upgrading is "run init again" rather than hand-editing SQL. New columns
    only: existing rows keep their values (defaults fill what they lack).
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(works)")}
    if columns and "drive_link" not in columns:
        connection.execute("ALTER TABLE works ADD COLUMN drive_link TEXT NOT NULL DEFAULT ''")

    learner_columns = {row[1] for row in connection.execute("PRAGMA table_info(learners)")}
    if learner_columns and "is_active" not in learner_columns:
        # Existing rows default to active: upgrading changes no roster until
        # someone is deliberately marked with `learner deactivate`.
        connection.execute("ALTER TABLE learners ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if learner_columns and "deleted_at" not in learner_columns:
        # NULL means not trashed: upgrading traps nobody until `learner
        # trash` is run deliberately.
        connection.execute("ALTER TABLE learners ADD COLUMN deleted_at TEXT DEFAULT NULL")


def _create_database(path: Path, *, sample_data: bool) -> int:
    """Build the SQLite schema, optionally with the sample rows.

    Returns:
        How many learners the database now has.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript((MIGRATIONS / "sqlite.sql").read_text(encoding="utf-8"))
        _upgrade_database(connection)
        if sample_data:
            connection.executescript((MIGRATIONS / "seed_example.sql").read_text(encoding="utf-8"))
        connection.commit()
        # The table name is a literal on purpose (M30): this schema was just
        # created by sqlite.sql, which always uses the canonical names. The
        # db.tables mapping exists for pointing baton at an *external*
        # database whose tables are named differently: something `init`
        # never does, so routing this one count through the mapper would be
        # machinery with no caller it serves.
        return int(connection.execute("SELECT count(*) FROM learners").fetchone()[0])
    finally:
        connection.close()


def handle(ctx: Context) -> Exit:
    """Create the profile."""
    directory = Path(ctx.args.directory).expanduser().resolve()
    config_path = directory / "baton.yaml"

    if config_path.exists() and not ctx.args.force:
        raise UsageError(
            f"There is already a profile at {config_path}.",
            remedy="Pass --force to overwrite it, or choose another directory.",
        )

    # `docs.driver` is deliberately not a prompt (M30): the registry has
    # exactly one real implementation (`notion`), so the question would have
    # one answer and a user who pressed enter would learn nothing. When a
    # second document driver lands, it joins db/chat here.
    answers = {
        "locale": ctx.args.locale or _ask(ctx, "Language", "en", tuple(i18n.available_locales())),
        "timezone": ctx.args.timezone or _ask(ctx, "Timezone", "UTC"),
        "db": ctx.args.db or _ask(ctx, "Where do learner records live", "sqlite", DB_DRIVERS),
        "chat": ctx.args.chat
        or _ask(ctx, "How are messages sent", packaged_defaults()["chat"]["driver"], CHAT_DRIVERS),
        "learner_label": ctx.args.learner_label or _ask(ctx, "You call them a", "student"),
        "session_label": ctx.args.session_label or _ask(ctx, "You call one session a", "week"),
    }
    answers["learner_plural"] = ctx.args.learner_plural or pluralise(answers["learner_label"])
    answers["session_plural"] = ctx.args.session_plural or pluralise(answers["session_label"])

    try:
        zoneinfo.ZoneInfo(answers["timezone"])
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        raise UsageError(
            f"`{answers['timezone']}` is not a known timezone.",
            remedy="Use an IANA name such as Asia/Bangkok or Europe/London.",
        ) from exc

    directory.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(
            _profile_config(answers), sort_keys=False, allow_unicode=True, default_flow_style=False
        ),
        encoding="utf-8",
    )

    env_path = directory / ".env.example"
    env_path.write_text(_env_template(answers), encoding="utf-8")

    created: dict[str, Any] = {
        "profile": str(directory),
        "config": str(config_path),
        "env_example": str(env_path),
        "answers": answers,
    }

    if answers["db"] == "sqlite":
        learners = _create_database(
            directory / "data" / "studio.db", sample_data=ctx.args.sample_data
        )
        created["database"] = str(directory / "data" / "studio.db")
        created["learners"] = learners

    next_steps = [
        f"cp {env_path.name} .env    # then fill it in",
        f"export BATON_PROFILE={directory}",
        "baton doctor",
    ]
    if answers["db"] != "sqlite":
        next_steps.insert(0, "Run migrations/postgres.sql against your database")

    created["next_steps"] = next_steps

    lines = [
        f"Created a profile in {directory}",
        f"  baton.yaml     {answers['learner_plural']}, {answers['session_plural']}, "
        f"{answers['locale']}, {answers['timezone']}",
        "  .env.example   the variables this profile needs",
    ]
    if "database" in created:
        detail = f"{created['learners']} sample learner(s)" if ctx.args.sample_data else "empty"
        lines.append(f"  data/studio.db {detail}")
    lines.append("")
    lines.append("Next:")
    lines += [f"  {step}" for step in next_steps]

    ctx.report.result(created, human="\n".join(lines))
    return Exit.OK
