"""``baton lesson``: stage a summary or resume a recording lesson, then publish.

The loop an agent follows, and the reason it is three commands rather than one:

    baton lesson stage    "Ada"              # collect context
    baton lesson contract "Ada"              # → JSON Schema + that context
    …the model writes JSON…
    baton lesson ingest   "Ada" --file s.json  # validated, or exit 4 with reasons
    baton lesson render   "Ada"              # deterministic preview
    baton lesson publish  "Ada"              # onto the document

A recording-only lesson skips the model contract: `learner add-work --session`
creates its draft, and `lesson publish` is the retry path if document writes
stopped partway.

`contract` hands the model everything it needs and tells it exactly what shape
to return. `ingest` is where a wrong shape stops, nothing is stored, and every
violation comes back at once with a pointer, so the next attempt is informed
rather than another guess.
"""

from __future__ import annotations

import argparse
import contextlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import contracts
from ..adapters.db import store_scope
from ..adapters.db.base import LearnerStore
from ..adapters.docs import VIDEO_LINK_BLOCKS, find_video_link, open_docs
from ..adapters.docs.base import PreservePolicy
from ..adapters.media import open_publisher
from ..adapters.media.google import extract_video_id
from ..domain.footer import Footer
from ..domain.models import Learner
from ..domain.resolve import normalise
from ..domain.status import IN_PROGRESS, StatusVocabulary
from ..domain.whenever import now_in, today_in
from ..errors import (
    BatonError,
    ConfigError,
    NeedsHumanError,
    StateError,
    UpstreamError,
    UsageError,
)
from ..exits import Exit
from ..pipelines.learner import LearnerHistory
from ..pipelines.publish import SummaryPublisher
from ..pipelines.recording import complete_recording_work, recording_blocks
from ..pipelines.staging import (
    PUBLISHED,
    RECORDING,
    STAGED,
    SUMMARISED,
    LessonDraft,
    PieceSnapshot,
    PublishedRecord,
    StagingStore,
)
from ..pipelines.unpublish import apply_plan, plan_legacy, plan_recorded, plan_whole_page
from ..render import piece as render_piece
from ..render import summary as render
from ..render import youtube as render_youtube
from .guard import guarded
from .naming import resolve as _resolve

if TYPE_CHECKING:
    from .app import Context


def _name_argument(parser: argparse.ArgumentParser) -> None:
    """Take the learner either positionally or as ``--learner``.

    Every other command that names a person one at a time takes it
    positionally, and `send batch` takes `--learner`. An agent driving Baton
    reaches for the flag often enough that `baton lesson publish --learner X
    --session N` was a usage error in production; the positional stays the
    documented form and the flag is simply also accepted.
    """
    parser.add_argument("name", metavar="NAME", nargs="?", default="")
    parser.add_argument(
        "--learner",
        metavar="NAME",
        default="",
        help="The learner, if you would rather not pass NAME positionally.",
    )


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``lesson`` command group."""
    parser = subparsers.add_parser(
        "lesson",
        help="Stage, summarise, and publish a lesson.",
        description=(
            "Baton renders the document; a model only supplies validated data. "
            "Run `lesson contract` to get the schema and context, then `ingest`."
        ),
    )
    group = parser.add_subparsers(dest="lesson_command", metavar="<subcommand>")
    parser.set_defaults(handler=_require_subcommand)

    stage = group.add_parser("stage", help="Start a draft, collecting context automatically.")
    _name_argument(stage)
    stage.add_argument(
        "--session",
        type=int,
        default=None,
        help="Session number. Defaults to the next unstarted, empty one.",
    )
    stage.add_argument("--titles", default="", help="Repertoire covered, for the document.")
    stage.add_argument("--context", default="", help="The teacher's notes for this lesson.")
    stage.add_argument("--context-file", default="", metavar="PATH")
    stage.add_argument(
        "--corrected",
        default="",
        help=(
            "The notes again with spellings fixed, for a profile that keeps a "
            "vocabulary. What `lesson contract` serves to the model; the raw "
            "notes are kept alongside and are never overwritten."
        ),
    )
    stage.add_argument("--corrected-file", default="", metavar="PATH")
    stage.add_argument(
        "--no-previous",
        action="store_true",
        help="Skip fetching the previous session's summary.",
    )
    stage.set_defaults(handler=handle_stage)

    contract = group.add_parser(
        "contract",
        help="Print the schema and context a model needs to write the summary.",
        description=(
            "Everything required to produce a valid summary, in one document: "
            "the JSON Schema, the lesson context, and the learner's teaching profile."
        ),
    )
    _name_argument(contract)
    contract.set_defaults(handler=handle_contract)

    ingest = group.add_parser(
        "ingest",
        help="Validate and store a model-written summary.",
        description="Exits 4 with every violation if the JSON does not conform. Nothing is stored.",
    )
    _name_argument(ingest)
    ingest.add_argument("--file", metavar="PATH", help="JSON file. Use - for stdin.")
    ingest.add_argument("--json-text", metavar="JSON", help="JSON as a literal argument.")
    ingest.set_defaults(handler=handle_ingest)

    render_cmd = group.add_parser("render", help="Preview the rendered summary.")
    _name_argument(render_cmd)
    render_cmd.add_argument(
        "--format", choices=("markdown", "blocks", "message"), default="markdown"
    )
    render_cmd.set_defaults(handler=handle_render)

    listing = group.add_parser("list", help="Every staged lesson.")
    listing.set_defaults(handler=handle_list)

    show = group.add_parser("show", help="One draft in full.")
    _name_argument(show)
    show.set_defaults(handler=handle_show)

    publish = group.add_parser(
        "publish",
        help="Write the summary onto the session document.",
        description=(
            "Appends the summary and removes the previous replaceable blocks. "
            "Blocks matching docs.preserve (recordings, embeds) are kept."
        ),
    )
    _name_argument(publish)
    publish.add_argument(
        "--session",
        type=int,
        default=None,
        help="Refuse unless the staged draft is for this session number.",
    )
    publish.add_argument("--dry-run", action="store_true", help="Report what would change.")
    publish.add_argument(
        "--force",
        action="store_true",
        help="Publish again even if this document was already published to.",
    )
    publish.set_defaults(handler=handle_publish)

    unpublish = group.add_parser(
        "unpublish",
        help="Take a published summary back off the session document.",
        description=(
            "Removes only the blocks there is evidence Baton wrote: those the "
            "publish recorded, or an exact match of the stored rendering. A block "
            "edited or added by hand stops the command (exit 3) rather than being "
            "deleted. The session returns to in-progress and the draft comes back, "
            "so the lesson can be corrected and published again. A message already "
            "sent is not retracted."
        ),
    )
    _name_argument(unpublish)
    unpublish.add_argument(
        "--session",
        type=int,
        default=None,
        help="Which published session. Defaults to the most recent.",
    )
    unpublish.add_argument("--dry-run", action="store_true", help="Report what would be removed.")
    unpublish.add_argument(
        "--whole-page",
        action="store_true",
        help="Remove every block on the page, including ones the teacher wrote.",
    )
    unpublish.add_argument(
        "--force",
        action="store_true",
        help="Required with --whole-page: confirms deleting blocks that are not Baton's.",
    )
    unpublish.set_defaults(handler=handle_unpublish)

    stage_set = group.add_parser(
        "stage-set",
        help="Amend one field of a staged lesson.",
        description=(
            "For correcting a typo or a title after staging without re-running "
            "the stage step. Only the fields that are plain text can be set: "
            "the summary itself is only accepted through `lesson ingest`."
        ),
    )
    _name_argument(stage_set)
    stage_set.add_argument(
        "--field",
        choices=("titles", "context", "corrected_context"),
        required=True,
    )
    stage_set.add_argument(
        "--value",
        required=True,
        help="The new value. Use the empty string to clear a field.",
    )
    stage_set.set_defaults(handler=handle_stage_set)

    remove = group.add_parser("remove", help="Discard a draft.")
    _name_argument(remove)
    remove.set_defaults(handler=handle_remove)

    clear = group.add_parser("clear", help="Discard every draft.")
    clear.add_argument("--yes", action="store_true", help="Required: this deletes drafts.")
    clear.add_argument(
        "--force",
        action="store_true",
        help=(
            "Also clear drafts whose publish left unfinished targets: the "
            "draft file is the only record that the work is still owed."
        ),
    )
    clear.set_defaults(handler=handle_clear)


def _require_subcommand(ctx: Context) -> Exit:
    raise UsageError(
        "`baton lesson` needs a subcommand.",
        remedy="Try `baton lesson list`, or `baton lesson stage <name>`.",
    )


# -- shared plumbing ---------------------------------------------------------


def _staging(ctx: Context) -> StagingStore:
    return StagingStore(ctx.config.state_dir / "lessons")


def _published(ctx: Context) -> PublishedRecord:
    return PublishedRecord(ctx.config.state_dir / "published")


def _recording_view(ctx: Context, store, learner: Learner, draft):
    """The session a recording draft owns, including a half-finished retry."""

    history = LearnerHistory(
        store,
        open_docs(ctx.config),
        StatusVocabulary.from_config(ctx.config.section("docs.statuses")),
        max_parallel_reads=int(ctx.config.get("docs.max_parallel_reads", 4)),
    )
    views = history.sessions(learner)
    view = next((item for item in views if item.number == draft.session_number), None)
    label = ctx.config.label("session")
    if view is None or not view.session.doc_id:
        raise UsageError(
            f"{learner.name} has no {label} {draft.session_number} with a document.",
            remedy=f'Run `baton learner sessions "{learner.name}"` to see what exists.',
        )
    if view.state not in {"in_progress", "done"}:
        raise UsageError(
            f"{learner.name}'s {label} {draft.session_number} is {view.state or 'unknown'}.",
            remedy="A recording lesson can finish while its document is open or just completed.",
        )
    return view


def _require_summary_draft(draft: LessonDraft, learner_name: str) -> None:
    if draft.kind == RECORDING:
        raise UsageError(
            f"{learner_name}'s staged lesson is a recording, not a teaching summary.",
            remedy=f'Publish it with `baton lesson publish "{learner_name}"`, or use '
            "`baton lesson remove` after checking the recording draft.",
        )


def _named(ctx: Context) -> str:
    """The learner this invocation is about, from NAME or ``--learner``.

    Both is fine when they agree: an agent that passes the flag and the
    positional has said the same thing twice, not two different things. Two
    different names is refused rather than one of them silently winning: the
    cost of publishing the wrong person's lesson is a message to the wrong
    family.
    """
    positional = str(getattr(ctx.args, "name", "") or "")
    flag = str(getattr(ctx.args, "learner", "") or "")
    label = ctx.config.label("learner")
    if positional and flag and normalise(positional) != normalise(flag):
        raise UsageError(
            f'Two different {label}s were given: "{positional}" and "{flag}".',
            remedy=f"Pass the {label} once: as NAME, or as --learner.",
        )
    name = positional or flag
    if not name:
        raise UsageError(
            f"`baton lesson {ctx.args.lesson_command}` needs a {label}.",
            remedy=f'Name one: `baton lesson {ctx.args.lesson_command} "<name>"`.',
        )
    return name


def _capture_piece_snapshot(store: LearnerStore, learner: Learner) -> PieceSnapshot:
    """Read the assigned Song DB row once, at the lesson boundary."""
    if learner.current_piece_id is None:
        return PieceSnapshot.capture(None)
    piece = store.get_piece(learner.current_piece_id)
    if piece is None:
        raise UsageError(
            f'{learner.name} is assigned missing piece id "{learner.current_piece_id}".',
            remedy="Fix or clear the learner's current piece, then re-stage the lesson.",
        )
    return PieceSnapshot.capture(piece)


def _piece_sources(
    store: LearnerStore, learner: Learner, snapshot: PieceSnapshot
) -> tuple[str, ...]:
    """URLs on the page that are the piece being learnt, not this recording.

    The song a lesson works on sits on the same page as the lesson, and Notion
    turns a pasted song URL into a bookmark and an embed: shapes
    `find_video_link` reads. A song on YouTube looks exactly like a recording
    on YouTube, which is how a publish once tried to write a lesson summary
    onto a record label's music video and was refused by YouTube itself.

    Both the piece this lesson was taught on and the one the learner is on now
    are named, because a song can be changed after the fact: the page is
    corrected to the new source while the snapshot still holds the old one.
    """
    links: list[str] = []
    if snapshot.piece is not None and snapshot.piece.source_link:
        links.append(snapshot.piece.source_link)
    if learner.current_piece_id:
        with contextlib.suppress(BatonError):
            piece = store.get_piece(learner.current_piece_id)
            if piece is not None and piece.source_link:
                links.append(piece.source_link)
    return tuple(links)


def _require_force_compatible(
    draft: LessonDraft, published: Mapping[str, Any] | None, *, force: bool
) -> None:
    """Refuse a rewrite when old preserved resources cannot be attributed."""
    if not force or published is None:
        return
    previous = PieceSnapshot.from_record(published)
    if previous.status == "unavailable" or not draft.piece_snapshot.same_content(previous):
        raise UsageError(
            "Cannot force-publish this lesson with a different or unknown piece snapshot.",
            remedy="Keep the published snapshot and correct the document manually; "
            "automatic cross-snapshot repair is not available.",
        )


def _short_summary_rules(ctx: Context) -> dict[str, Any]:
    rules = ctx.config.section("summary.short_summary")
    return {
        "max_lines": int(rules.get("max_lines", 5)),
        "allow_emoji": bool(rules.get("allow_emoji", False)),
        "allow_links": bool(rules.get("allow_links", False)),
    }


def _body_rules(ctx: Context, learner: Learner) -> dict[str, Any]:
    """The document-body rules, as `validate_lesson_summary` takes them.

    Learner-aware in one place only: the phrases that put a goal inside the
    next lesson are refused for someone who can practise at home and accepted
    for someone who cannot, because for them the next lesson is where the
    goals honestly belong. The attitude phrases are refused either way:
    nobody can tick off "be more open".
    """
    rules = ctx.config.section("summary.body")
    goals = [str(item) for item in rules.get("goals_attitude", [])]
    if learner.has_instrument:
        goals += [str(item) for item in rules.get("goals_not_practicable", [])]
    return {
        "max_repeats": int(rules.get("max_repeats", 2)),
        "vague_phrases": [str(item) for item in rules.get("vague_phrases", [])],
        "trait_language": [str(item) for item in rules.get("trait_language", [])],
        "goals_not_practicable": goals,
    }


def _no_instrument(ctx: Context, key: str) -> str:
    """One piece of the no-instrument-at-home wording, from config.

    ``instruction`` falls back to the older `summary.no_instrument_at_home`
    key, which is what deployed profiles set before this block existed.
    """
    value = str(ctx.config.get(f"summary.no_instrument.{key}", "")).strip()
    if not value and key == "instruction":
        value = str(ctx.config.get("summary.no_instrument_at_home", "")).strip()
    return value


def _tone_override(ctx: Context, learner: Learner, key: str) -> str:
    """One wording this learner's tone asks to change, from config.

    A tone can rename the `goals` heading and the parent message's homework
    label, the same substitution `has_instrument` already makes. It lives in
    config rather than as tone names in code: a studio that invents a fifth
    tone gets the behaviour without waiting for a release, and a tone nobody
    wrote an override for changes nothing.
    """
    tone = str(learner.tone or "").strip()
    if not tone:
        return ""
    return str(ctx.config.get(f"summary.tone_overrides.{tone}.{key}", "")).strip()


def _goals_wording(ctx: Context, learner: Learner, key: str) -> str:
    """What `goals` is called for this learner, or "" for the profile default.

    Two things can rename it and they can both apply at once, so the order is
    deliberate: a tone is a choice about how to teach, while no instrument at
    home is a fact about what the learner can physically do. The fact wins.
    """
    wording = _tone_override(ctx, learner, key)
    if not learner.has_instrument:
        wording = _no_instrument(ctx, key) or wording
    return wording


def _sections(ctx: Context, learner: Learner) -> dict[str, Any]:
    """Section headings for this learner's page.

    A learner with no instrument at home, or one taught in a tone that does
    not set homework, gets a different heading over `goals`: calling it
    homework when none is coming is the heading contradicting the lesson
    underneath it.
    """
    sections = dict(ctx.config.get("summary.sections", {}) or {})
    heading = _goals_wording(ctx, learner, "section")
    if heading:
        sections["goals"] = heading
    return sections


def _message_labels(ctx: Context, learner: Learner) -> dict[str, Any]:
    """Labels for the parent's message, with the same substitution."""
    labels = dict(ctx.config.get("summary.short_summary.labels", {}) or {})
    label = _goals_wording(ctx, learner, "message_label")
    if label:
        labels["homework"] = label
    return labels


def _vocabulary(ctx: Context) -> list[str]:
    """The profile's settled spellings, for the contract and for `ingest`.

    The pool exists because a spelling the model is free to reinvent is a
    spelling it will reinvent: a piece title becomes "Encore", then "Encoure",
    and the family reading two pages a month apart sees two different songs.
    An empty pool contributes nothing, so a studio without one keeps today's
    behaviour exactly.
    """
    pool = ctx.config.get("summary.vocabulary", []) or []
    return [str(term) for term in pool if str(term).strip()]


def _voice(ctx: Context, learner: Learner) -> list[str]:
    """How to write for this learner in particular, from their own record.

    The learners table has carried a `tone`, an `instrument`, and whether there
    is an instrument at home since the first migration, and all three reached
    the model as bare words with nothing saying what to do about them, so a
    six-year-old and an exam candidate got the same voice, and a drum lesson
    and a guitar lesson got the same notation. An unrecognised value
    contributes nothing rather than guessing: these columns are free text, and
    a studio that invented a word for one has not yet said what it means.
    """
    lines = []
    tone = str(learner.tone or "").strip()
    guidance = str(ctx.config.section("summary.tones").get(tone, "")).strip()
    if guidance:
        lines.append(guidance)
    instrument = str(learner.instrument or "").strip()
    notation = str(ctx.config.section("summary.instruments").get(instrument, "")).strip()
    if notation:
        lines.append(notation)
    if not learner.has_instrument:
        without = _no_instrument(ctx, "instruction")
        if without:
            lines.append(without)
    heading = _goals_wording(ctx, learner, "section")
    if heading:
        # The model is told what the section it is filling will be called.
        # Writing "practise at home" under a heading that says "next lesson"
        # is the same contradiction from the other direction.
        lines.append(f'The `goals` section is published under the heading "{heading}".')
    level = _prompt_level(ctx, learner)
    guidance = str(ctx.config.section("summary.prompt_levels").get(level, "")).strip()
    if guidance:
        lines.append(guidance)
    return lines


def _prompt_level(ctx: Context, learner: Learner) -> str:
    """This learner's prompt level, as the studio's own column spells it.

    A studio-specific column, so it is read through `db.fields` like every
    other one and comes back as text: the level is a key into
    `summary.prompt_levels`, not a number anything does arithmetic on.
    """
    column = str(ctx.config.get("db.fields.learner.prompt_level", "prompt_level"))
    raw = learner.raw or {}
    value = raw.get(column, raw.get("prompt_level"))
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _footer(ctx: Context) -> Footer:
    """The disclosure appended to every published summary, from config."""
    return Footer.from_config(ctx.config.section("summary.footer"))


def _theory(ctx: Context) -> dict[str, str]:
    """Theory callouts from the profile, as ``{id: text}``.

    Lives in the profile rather than the package: a studio's teaching notes are
    its own, and in the private deployment they are the material that must not
    be published.
    """
    path = ctx.config.profile_dir / "theory.json"
    if not path.is_file():
        return {}
    from ..core import jsonio

    data = jsonio.read_json(path, {})
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _previous_context(ctx: Context, record: Mapping[str, Any] | None) -> str:
    """What the last lesson was, for the next one to be written against.

    The full summary where the record has one, the parent's message otherwise.
    Only the message used to be kept, and three bullet lines are a thin thing
    to judge a week's progress from: the `progress` section asks what changed
    since last time, and the answer has to be compared against what actually
    happened, not against the sentence a family was sent about it.

    Records written before summaries were stored keep working on the message,
    which is what they have.
    """
    if not record:
        return ""
    summary = record.get("summary")
    if isinstance(summary, dict):
        return render.to_markdown(
            summary,
            sections=ctx.config.get("summary.sections", {}),
            callout_texts=_theory(ctx),
        )
    return str(record.get("short_message", ""))


def _titles_from(summary: dict[str, Any] | None) -> str:
    """What the session covered, as one line for the document's own column.

    Falls back to the topics the summary already lists, because the column is
    read by `prep` and by anyone scanning the course at a glance, and a
    studio that never passes `--titles` would otherwise leave it empty
    forever.
    """
    if not summary:
        return ""
    topics = [
        str(entry.get("topic", "")).strip()
        for entry in summary.get("covered", [])
        if isinstance(entry, dict) and str(entry.get("topic", "")).strip()
    ]
    return ", ".join(topics)


def _update_youtube_description(
    ctx: Context, docs, doc_id: str, learner, draft: LessonDraft, *, exclude: tuple[str, ...] = ()
) -> dict[str, Any] | None:
    """Best-effort: put the just-published summary on the lesson's YouTube
    video description too, the way the studio's previous pipeline did.

    Returns ``None`` when there was nothing to do (no YouTube configured, no
    video on the document yet, or the link on the document is not a YouTube
    URL at all): that is the ordinary case for most sessions, not a failure.
    ``exclude`` names URLs that are the piece's own source rather than the
    recording, so the summary is never written onto the song's own video.
    A configured-but-failing update (wrong owner, API error) is reported but
    never raised: the document is already published, and this is a nice-to-
    have on top of it, not a reason to make the command exit non-zero.
    """
    video_link = find_video_link(
        docs,
        doc_id,
        blocks=tuple(
            str(item) for item in ctx.config.get("docs.video_link_blocks", VIDEO_LINK_BLOCKS)
        ),
        exclude=exclude,
    )
    video_id = extract_video_id(video_link) if video_link else None
    if not video_id:
        return None

    date = ""
    with contextlib.suppress(BatonError):  # cosmetic; the description renders without it
        date = docs.get_status(doc_id, with_blocks=False).date

    description = render_youtube.format_description(
        draft.summary or {},
        instrument=learner.instrument,
        week=draft.session_number,
        student_name=learner.name,
        date=date,
    )
    try:
        # Credentials are resolved lazily (on first API call, not on
        # construction), so a studio that never configured YouTube only
        # surfaces `ConfigError` here, not from `open_publisher` itself.
        publisher = open_publisher(ctx.config)
        publisher.update_description(video_id, description)
    except ConfigError:
        # This studio has not configured YouTube at all, nothing to update.
        return None
    except BatonError as exc:
        return {"status": "error", "video_id": video_id, "error": str(exc)}
    return {"status": "ok", "video_id": video_id}


def _retry_youtube_description(
    ctx: Context,
    draft: LessonDraft,
    learner: Learner,
    published_record: Mapping | None,
    *,
    exclude: tuple[str, ...] = (),
) -> dict | None:
    """The description update a re-run of publish owes, if any.

    Two cases, both with the summary already on the page:

    - a previous attempt failed and has attempts left;
    - no attempt was ever made, because the recording had not landed on the
      document when the summary was published. That is the ordinary order for
      a lesson filmed while the summary is written: publish, then the video
      pipeline uploads and links the recording. Without this second case the
      description is never written at all, since nothing recorded the missed
      work as pending.

    Two places remember the outcome, and only one survives re-staging: the
    draft is overwritten wholesale by every `stage`, while the published
    record is per session. Both are consulted, for the same reason the
    docs-completion check consults both.

    Returns the outcome dict (``status`` plus extras) once recorded on the
    draft and folded into the published record, or ``None`` when there was
    nothing to do: already done, out of attempts, or still no video to
    describe.
    """
    recorded = dict((published_record or {}).get("youtube") or {})

    if draft.target_done("youtube") or recorded.get("status") == "ok":
        return None
    cap = int(ctx.config.get("media.youtube.description_attempts", 3))
    attempted = max(
        int(draft.targets.get("youtube", {}).get("attempts", 0)),
        int(recorded.get("attempts", 0)),
    )
    if attempted >= cap:
        return None

    result = _update_youtube_description(
        ctx, open_docs(ctx.config), draft.doc_id, learner, draft, exclude=exclude
    )
    if result is None:
        # No video on the document yet: still nothing to describe, and not a
        # spent attempt. The wide case above picks it up once one lands.
        return None
    status = result["status"]
    extra = {key: value for key, value in result.items() if key != "status"}
    draft.record_target("youtube", status, **extra)
    state = dict(draft.targets["youtube"])
    _published(ctx).note_youtube(draft.learner_id, draft.session_number, state)
    return result


def _recording_line(recording: dict[str, Any] | None) -> str:
    """One line saying what `_stitch_recording` did, or nothing at all."""
    if not recording:
        return ""
    if recording["status"] == "linked":
        return f"\n  linked the uploaded recording: {recording['url']}"
    return f"\n  the uploaded recording could NOT be linked: {recording['error']}"


def _uploaded_recording(ctx: Context, draft: LessonDraft) -> str:
    """The recording already uploaded for this session, as a URL, or "".

    Read through the narrow part of the video job record that is a contract
    between the two halves of the pipeline: who and which session the job was
    for, and whether `uploaded` happened. A job that has uploaded holds the
    only durable note that the recording exists at all, and nothing else in
    Baton can answer "is there one?" once the run has ended.

    Archived jobs are read too. A folder holds one live job, and the week a
    learner's next lesson is collected the finished one is moved aside, so
    without this, a page could only be repaired until the next lesson was
    filmed, which is exactly the stretch of days a repair is wanted for.
    """
    from ..pipelines.video import VideoJob, VideoJobStore

    def owns_this_session(job: VideoJob) -> bool:
        if int(job.session_number or 0) != int(draft.session_number):
            return False
        # Ids are the identity when both sides have one; a job recorded before
        # the learner was matched has only the folder name it came from.
        if job.learner_id and draft.learner_id:
            return str(job.learner_id) == str(draft.learner_id)
        return job.learner_name == draft.learner_name

    jobs = VideoJobStore(ctx.config.state_dir / "video").list(include_archived=True)
    candidates = [
        job
        for job in jobs
        if owns_this_session(job)
        and job.done("uploaded")
        and (str(job.steps.get("uploaded", {}).get("url", "")) or job.video_url)
    ]
    if not candidates:
        return ""
    # One session can leave more than one record: a job archived, then a
    # re-run of the same week. The most recent one is the upload that stands,
    # and `list` yields live records before archived ones, so a tie on a
    # timestamp written to the second still resolves to the live job.
    newest = max(candidates, key=lambda job: job.updated_at)
    return str(newest.steps.get("uploaded", {}).get("url", "")) or newest.video_url


def _stitch_recording(
    ctx: Context, docs, draft: LessonDraft, *, exclude: tuple[str, ...] = ()
) -> dict[str, Any] | None:
    """Put an already-uploaded recording on the page when nothing else did.

    The video pipeline links the recording itself, and in the ordinary run it
    has done so long before anyone publishes. But a run that uploaded and then
    failed (the upload is recorded before anything else can fail, on purpose)
    leaves the video on YouTube and the page with no link to it. Nothing
    downstream recovers from that on its own: `send` refuses for a missing
    recording and says to add a video block by hand, which is exactly what a
    studio had to do at half past nine at night.

    So publish repairs it. Publish is where a person already is when the
    recording matters, and it is the last step before the message goes out.

    Returns ``None`` when there was nothing to do: the page already shows a
    recording, or no upload has happened for this session. A failure to append
    is reported rather than raised: the summary is on the page by now, and the
    remedy the gate prints still works.
    """
    if not draft.doc_id:
        return None
    if find_video_link(
        docs,
        draft.doc_id,
        blocks=tuple(
            str(item) for item in ctx.config.get("docs.video_link_blocks", VIDEO_LINK_BLOCKS)
        ),
        exclude=exclude,
    ):
        return None

    url = _uploaded_recording(ctx, draft)
    if not url:
        return None
    try:
        docs.append_blocks(
            draft.doc_id,
            [
                {
                    "object": "block",
                    "type": "video",
                    "video": {"type": "external", "external": {"url": url}},
                }
            ],
        )
    except BatonError as exc:
        return {"status": "error", "url": url, "error": str(exc)}
    return {"status": "linked", "url": url}


def _finish_session(
    ctx: Context,
    publisher: SummaryPublisher,
    staging: StagingStore,
    draft: LessonDraft,
    learner_name: str,
    *,
    resumed: bool = False,
    youtube_result: dict | None = None,
    recording: dict[str, Any] | None = None,
) -> Exit:
    """Mark a published session done on its document.

    Separate from writing the blocks because the two can fail independently:
    the summary is on the page for good once it is appended, while this write
    can be retried without consequence. Recording which one happened is what
    lets `publish` be re-run to finish a session without appending a second
    copy of the same summary.

    ``youtube_result`` and ``recording``, on a resumed run that also owed a
    description update or a link to the recording, are carried into the report
    so one run's outcome reads as one line.
    """
    label = ctx.config.label("session")
    try:
        written = publisher.complete(
            draft.doc_id,
            date=today_in(ctx.config.timezone).isoformat(),
            titles=draft.titles or _titles_from(draft.summary),
        )
    except Exception as exc:
        draft.note_target("docs", completed=False, complete_error=str(exc))
        staging.save(draft)
        raise UpstreamError(
            f"{learner_name}'s summary is on the page, but {label} "
            f"{draft.session_number} could not be marked done: {exc}",
            service="docs",
            remedy=f'Re-run `baton lesson publish "{learner_name}"`. The summary is '
            f"already published, so a re-run only finishes the {label}. It will "
            "not append a second copy.",
        ) from exc

    draft.note_target("docs", completed=written)
    staging.save(draft)

    if resumed:
        youtube_line = ""
        if youtube_result and youtube_result["status"] == "ok":
            youtube_line = "; updated the YouTube description now"
        elif youtube_result and youtube_result["status"] == "error":
            youtube_line = f"; the YouTube description still failed: {youtube_result['error']}"
        ctx.report.result(
            {
                **draft.summary_view(),
                "completed": written,
                "youtube": youtube_result,
                "recording": recording,
            },
            human=f"{learner_name} {label} {draft.session_number} was already "
            f"published; marked it done now{youtube_line}.{_recording_line(recording)}",
        )
    return Exit.OK


def _read_payload(ctx: Context) -> Any:
    """Read the model's JSON from --file, stdin, or --json-text."""
    source = ctx.args.file
    text: str

    if ctx.args.json_text and source:
        raise UsageError(
            "Pass either --file or --json-text, not both.",
            remedy="Choose one source for the summary.",
        )
    if ctx.args.json_text:
        text = ctx.args.json_text
    elif source == "-":
        import sys

        text = sys.stdin.read()
    elif source:
        path = Path(source).expanduser()
        if not path.is_file():
            raise UsageError(
                f"No such file: {path}",
                remedy="Check the path, or pipe the JSON in with `--file -`.",
            )
        text = path.read_text(encoding="utf-8")
    else:
        raise UsageError(
            "`lesson ingest` needs the summary JSON.",
            remedy="Pass --file <path>, --file - to read stdin, or --json-text.",
        )

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # Malformed JSON is a contract failure, not a usage error: the model
        # produced it and the model is what has to fix it.
        from ..errors import ContractError

        raise ContractError(
            f"The summary is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno}).",
            violations=[{"path": "/", "reason": f"invalid JSON at line {exc.lineno}"}],
            remedy="Return only the JSON object, with no prose around it.",
        ) from exc


# -- handlers ----------------------------------------------------------------


def handle_stage(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, _named(ctx))
        existing = _staging(ctx).get(learner.id)
        if existing is not None and existing.kind == RECORDING and existing.status != PUBLISHED:
            raise UsageError(
                f"{learner.name} still has an unpublished recording draft for "
                f"{existing.session_number}.",
                remedy=f'Finish it with `baton lesson publish "{learner.name}"`, or remove '
                "the draft after checking the recording.",
            )

        context = ctx.args.context
        if ctx.args.context_file:
            path = Path(ctx.args.context_file).expanduser()
            if not path.is_file():
                raise UsageError(f"No such file: {path}")
            context = path.read_text(encoding="utf-8")

        corrected = ctx.args.corrected
        if ctx.args.corrected_file:
            if corrected:
                raise UsageError(
                    "Pass either --corrected or --corrected-file, not both.",
                    remedy="Point --corrected-file at a file, or pass the text itself.",
                )
            path = Path(ctx.args.corrected_file).expanduser()
            if not path.is_file():
                raise UsageError(f"No such file: {path}")
            corrected = path.read_text(encoding="utf-8")

        session_number: int
        doc_id: str
        titles = ctx.args.titles
        previous_context = ""

        if ctx.args.session is not None:
            # An explicitly named session needs no document reads at all: the
            # number and the document id both come from the database. Staging
            # then works during a document-store outage, which matters because
            # the notes are usually written straight after the lesson.
            session = store.get_session(learner.id, ctx.args.session)
            if session is None:
                raise UsageError(
                    f"{learner.name} has no {ctx.config.label('session')} "
                    f"numbered {ctx.args.session}.",
                    remedy=f'Run `baton learner sessions "{learner.name}"` to see what exists.',
                )
            session_number = session.number
            doc_id = session.doc_id
        else:
            # Choosing for the caller does require reading the documents:
            # "free" is a fact about the page, not about the database row.
            history = LearnerHistory(
                store,
                open_docs(ctx.config),
                StatusVocabulary.from_config(ctx.config.section("docs.statuses")),
                max_parallel_reads=int(ctx.config.get("docs.max_parallel_reads", 4)),
            )
            views = history.sessions(learner)
            chosen = history.next_empty(views)
            if chosen is None:
                raise UsageError(
                    f"{learner.name} has no free {ctx.config.label('session')} to stage.",
                    remedy="Pass --session explicitly, or create the next one first.",
                )
            session_number = chosen.number
            doc_id = chosen.session.doc_id
            titles = titles or chosen.doc.titles

            if not ctx.args.no_previous:
                previous = history.latest_done(views)
                if previous is not None:
                    record = _published(ctx).get(learner.id, previous.number)
                    previous_context = _previous_context(ctx, record)

        if ctx.args.session is not None and not ctx.args.no_previous:
            # Read the previous summary from Baton's own records rather than
            # from the document store, so this path stays offline too.
            record = _published(ctx).latest(learner.id)
            if record and int(record.get("session_number", 0)) < session_number:
                previous_context = _previous_context(ctx, record)

        piece_snapshot = _capture_piece_snapshot(store, learner)
        draft = LessonDraft(
            learner_id=learner.id,
            learner_name=learner.name,
            session_number=session_number,
            piece_snapshot=piece_snapshot,
            doc_id=doc_id,
            titles=titles,
            context=context,
            corrected_context=corrected,
            previous_context=previous_context,
        )

    _staging(ctx).save(draft)
    label = ctx.config.label("session")
    ctx.report.result(
        draft.summary_view(),
        human=f"Staged {learner.name} {label} {draft.session_number}\n"
        f'  next: baton lesson contract "{learner.name}"'
        + (
            "\n  corrected notes recorded: the contract serves these, the raw "
            "notes are kept on the draft"
            if corrected
            else ""
        ),
    )
    return Exit.OK


def handle_contract(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, _named(ctx))

    draft = _staging(ctx).require(learner.id, learner.name)
    _require_summary_draft(draft, learner.name)
    learner_context = learner.to_dict()
    learner_context.pop("current_piece_id", None)
    piece = draft.piece_snapshot.piece
    rules = _short_summary_rules(ctx)
    theory = _theory(ctx)
    vocabulary = _vocabulary(ctx)

    # The corrected copy is what the model reads, so a spelling the teacher
    # fixed once stays fixed. The raw notes stay on the draft: a correction is
    # checkable only against what was actually written, and `lesson show`
    # keeps both for exactly that.
    lesson_notes = draft.corrected_context or draft.context

    # The default wording tells the model `goals` is homework. For a learner
    # whose `goals` is published as next lesson's aim (no instrument at home,
    # or a tone that does not set homework), that instruction contradicts both
    # the heading above the section and the tone guidance below it, and the
    # model has to pick one. It picked the homework line often enough to be
    # worth removing the choice.
    goals_rule = (
        "`goals` says what to aim for by the next lesson"
        if _goals_wording(ctx, learner, "section")
        else "`goals` says what to practise at home"
    )

    payload = {
        "schema": contracts.load_schema(contracts.LESSON_SUMMARY),
        "context": {
            "learner": learner_context,
            "session_number": draft.session_number,
            "titles": draft.titles,
            "current_piece": piece.to_dict() if piece else None,
            "lesson_notes": lesson_notes,
            "previous_session_summary": draft.previous_context,
        },
        "constraints": {
            "body": _body_rules(ctx, learner),
            "short_summary": rules,
            "available_callout_ids": sorted(theory),
            "language": ctx.config.locale,
            # Present even when empty, so a caller can see the pool was
            # considered rather than wonder whether the key was dropped.
            "vocabulary": vocabulary,
        },
        "instructions": [
            "Return one JSON object matching `schema`, and nothing else.",
            "Base every statement on `lesson_notes`. Do not invent progress.",
            "Say what is still difficult plainly; pair each difficulty with a fix.",
            "Use `previous_session_summary` to judge what is new, not to repeat it.",
            "Put what changed in `progress`, as the state before and the state "
            "now, not as a rating. `overview` says how the session went; "
            "`progress` says what is different; `covered` says what was worked "
            f"on; `focus` says what is still hard; {goals_rule}. "
            "One fact belongs in one of them.",
            "Describe what was observed rather than what it means: what they "
            "managed, how much help it took, what changed.",
            "Only use callout ids from `available_callout_ids`; never write theory text.",
            "Add `teacher_followup`: one to four short suggestions for the "
            "teacher, not the family, on what to watch for or develop next "
            "lesson. Draw them only from `lesson_notes`, phrase them as "
            "suggestions the teacher may ignore, and write them in the "
            "profile's language. They are never shown on the page or in the "
            "message.",
            f"Write in the language of this profile ({ctx.config.locale}).",
            *(
                [
                    "Spell names and terms exactly as they are spelled in "
                    "`constraints.vocabulary`; never invent a variant of one."
                ]
                if vocabulary
                else []
            ),
            *_voice(ctx, learner),
        ],
    }

    ctx.report.result(
        payload,
        human=json.dumps(payload, ensure_ascii=False, indent=2),
    )
    return Exit.OK


def handle_ingest(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, _named(ctx))

    staging = _staging(ctx)
    draft = staging.require(learner.id, learner.name)
    _require_summary_draft(draft, learner.name)
    payload = _read_payload(ctx)
    theory = _theory(ctx)

    # Raises ContractError (exit 4) with every violation. Nothing below runs
    # until the payload is acceptable, so a rejected summary is never stored.
    summary = contracts.validate_lesson_summary(
        payload,
        known_callouts=set(theory) if theory else None,
        # The first lesson of a course has nothing to compare against and is
        # not asked to invent a comparison; every lesson after it is.
        expect_progress=bool(draft.previous_context.strip()),
        **_body_rules(ctx, learner),
        **_short_summary_rules(ctx),
    )

    draft.summary = summary
    draft.status = SUMMARISED
    staging.save(draft)

    # Layer three of the vocabulary design: after acceptance, never a gate. A
    # summary rejected over a spelling is a summary that stops being produced,
    # so a near-miss is reported to the operator: on stderr, in `--json` mode
    # too, and the lesson stays on its way to render and publish.
    vocabulary = _vocabulary(ctx)
    warnings = contracts.vocabulary_near_misses(summary, vocabulary) if vocabulary else []

    ctx.report.result(
        {**draft.summary_view(), "warnings": warnings},
        human=f"Accepted summary for {learner.name}, "
        f"{ctx.config.label('session')} {draft.session_number}\n"
        f'  next: baton lesson render "{learner.name}"'
        + "".join(f"\n  note: {text}" for text in warnings),
    )
    for text in warnings:
        # Stderr is where an agent is told to look between commands; a person
        # reading the terminal already has the note inline above, and the
        # same line twice would look like a bug.
        if ctx.report.json_mode:
            ctx.report.warn(text)
    return Exit.OK


def handle_render(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, _named(ctx))

    draft = _staging(ctx).require(learner.id, learner.name)
    _require_summary_draft(draft, learner.name)
    if draft.summary is None:
        raise UsageError(
            f"No summary has been accepted for {learner.name} yet.",
            remedy=f'Run `baton lesson contract "{learner.name}"`, then `ingest`.',
        )

    sections = _sections(ctx, learner)
    theory = _theory(ctx)

    if ctx.args.format == "blocks":
        blocks = render_piece.to_blocks(draft.piece_snapshot) + render.to_blocks(
            draft.summary,
            sections=sections,
            callout_texts=theory,
            callout_icon=str(ctx.config.get("summary.callout_icon", "")),
        )
        ctx.report.result(
            {"blocks": blocks, "count": len(blocks)},
            human=json.dumps(blocks, ensure_ascii=False, indent=2),
        )
        return Exit.OK

    if ctx.args.format == "message":
        message = render.short_message(
            draft.summary,
            bullet=str(ctx.config.get("summary.short_summary.bullet", "•")),
            labels=_message_labels(ctx, learner),
        )
        ctx.report.result({"message": message}, human=message)
        return Exit.OK

    markdown = "\n\n".join(
        part
        for part in (
            render_piece.to_markdown(draft.piece_snapshot),
            render.to_markdown(
                draft.summary,
                sections=sections,
                callout_texts=theory,
                footer_lines=_footer(ctx).render(now_in(ctx.config.timezone)),
            ),
        )
        if part
    )
    ctx.report.result({"markdown": markdown}, human=markdown)
    return Exit.OK


def _list_target_states(ctx: Context, draft: LessonDraft) -> dict[str, dict[str, Any]]:
    """Full per-target state for a listing, with the two blind spots filled.

    `summary_view` reports the one-word status the publish loop stamps, and a
    heartbeat asking "is this stuck?" needs the error, the time, and how many
    tries were spent. Two facts a bare draft cannot see are consulted here:

    * a target never stamped on the draft may still be done. `stage`
      overwrites the draft wholesale; the published record is the copy that
      survives it, so its existence is proof the summary went out.
    * the youtube state is whichever of the two memories is newer. A
      description retry after a re-stage is newer than the original publish,
      and trusting the draft alone would show an error the record already
      resolved (or the reverse). ISO timestamps sort as strings.
    """
    states = {name: dict(state) for name, state in draft.targets.items()}
    published = _published(ctx).get(draft.learner_id, draft.session_number)
    if published is not None:
        if "docs" not in states:
            states["docs"] = {"status": "ok", "source": "published_record"}
        recorded = published.get("youtube")
        if isinstance(recorded, dict):
            known = states.get("youtube")
            if known is None or str(recorded.get("at", "")) > str(known.get("at", "")):
                states["youtube"] = {**dict(recorded), "source": "published_record"}
    return states


def _target_remedy(name: str, learner_name: str) -> str:
    """What to do about a target that did not finish, as one line."""
    if name == "youtube":
        return (
            f're-run `baton lesson publish "{learner_name}"` to retry the '
            "description update: attempts are capped, so read the error first"
        )
    return (
        f're-run `baton lesson publish "{learner_name}"`: it resumes where it '
        "stopped and does not append the summary twice"
    )


def _target_detail_lines(
    learner_name: str, states: dict[str, dict[str, Any]], recording: str
) -> list[str]:
    """The indented lines under one draft in `lesson list`."""
    lines: list[str] = []
    for name, state in sorted(states.items()):
        bits = [f"{name}: {state.get('status', 'unknown')}"]
        if state.get("at"):
            bits.append(f"at {state['at']}")
        if state.get("attempts") not in (None, 0):
            bits.append(f"{state['attempts']} attempt(s)")
        lines.append(f"      {', '.join(bits)}")
        if state.get("video_id"):
            lines.append(f"        video: {state['video_id']}")
        if state.get("error"):
            lines.append(f"        error: {state['error']}")
        if state.get("source") == "published_record":
            lines.append("        (state came from the published record, not this draft)")
        if state.get("status") != "ok":
            lines.append(f"        fix: {_target_remedy(name, learner_name)}")
    if recording and states.get("youtube", {}).get("status") != "ok":
        lines.append(f"      recording uploaded, not yet described: {recording}")
    return lines


def handle_list(ctx: Context) -> Exit:
    drafts = _staging(ctx).list()
    lessons = []
    for draft in drafts:
        view = draft.summary_view()
        states = _list_target_states(ctx, draft)
        # `targets` here is the full state per target: stage and ingest keep
        # returning the compact one-word view, but this listing is what a
        # heartbeat reads to decide whether a lesson is stuck, and one word
        # cannot carry an error message or a spent retry.
        view["targets"] = states
        try:
            view["recording"] = _uploaded_recording(ctx, draft)
        except Exception:
            # A listing must never fail on a side record; no recording line
            # is a smaller loss than no listing at all.
            view["recording"] = ""
        lessons.append(view)
    payload = {"lessons": lessons, "count": len(drafts)}

    if not drafts:
        ctx.report.result(payload, human="No lessons staged.")
        return Exit.OK

    label = ctx.config.label("session")
    width = max(len(draft.learner_name) for draft in drafts)
    lines = []
    for draft, view in zip(drafts, lessons, strict=True):
        detail = "recording" if draft.kind == RECORDING else None
        lines.append(
            f"  {draft.learner_name:<{width}}  {label} {draft.session_number:<3} "
            f"{draft.status}  {detail or ('summary ✓' if draft.summary else 'summary -')}"
        )
        lines.extend(_target_detail_lines(draft.learner_name, view["targets"], view["recording"]))
    ctx.report.result(payload, human="\n".join(lines))
    return Exit.OK


def handle_show(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, _named(ctx))

    draft = _staging(ctx).require(learner.id, learner.name)
    ctx.report.result(
        draft.to_dict(), human=json.dumps(draft.to_dict(), ensure_ascii=False, indent=2)
    )
    return Exit.OK


@guarded("lesson")
def handle_publish(ctx: Context) -> Exit:
    staging = _staging(ctx)
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, _named(ctx))
        draft = staging.require(learner.id, learner.name)
        label = ctx.config.label("session")
        if ctx.args.session is not None and int(ctx.args.session) != draft.session_number:
            raise UsageError(
                f"{learner.name}'s staged draft is {label} {draft.session_number}, "
                f"not {label} {ctx.args.session}.",
                remedy=f"Publish the draft as it stands with `baton lesson publish "
                f'"{learner.name}"`, or re-stage with `--session {ctx.args.session}` first.',
            )

        if draft.kind == RECORDING:
            if draft.work is None:
                raise StateError(
                    f"The recording draft for {learner.name} has no work snapshot.",
                    remedy="Re-run `learner attach-work --complete --pick N` for the work.",
                )
            existing_record = _published(ctx).get(learner.id, draft.session_number)
            if existing_record is not None and not ctx.args.force:
                if str(existing_record.get("kind") or "summary") != RECORDING:
                    raise StateError(
                        f"{learner.name}'s {label} {draft.session_number} already has a "
                        "published summary.",
                        remedy="Unpublish the summary before publishing a recording.",
                    )
                if draft.status != PUBLISHED and draft.target_done("docs"):
                    draft.status = PUBLISHED
                    staging.save(draft)
                ctx.report.result(
                    {**draft.summary_view(), "skipped": "already published"},
                    human=f"{learner.name} {label} {draft.session_number} was already "
                    "published. Use --force to publish again.",
                )
                return Exit.OK

            view = _recording_view(ctx, store, learner, draft)
            if ctx.args.dry_run:
                blocks = recording_blocks(draft.work)
                ctx.report.result(
                    {
                        **draft.summary_view(),
                        "doc_id": draft.doc_id,
                        "would_append": len(blocks),
                        "would_complete": True,
                        "dry_run": True,
                    },
                    human=f"Would publish {draft.work.title} as {learner.name}'s "
                    f"{label} {draft.session_number}.",
                )
                return Exit.OK

            recording_result = complete_recording_work(ctx.config, learner, view, draft.work)
            ctx.report.result(
                recording_result,
                human=f"Published {draft.work.title} as {learner.name}'s "
                f"{label} {draft.session_number}: "
                f"{recording_result['appended']} blocks written, "
                f"the {label} is done",
            )
            return Exit.OK

        # Read while the store is open: which URLs on the page are the song
        # rather than the recording, so neither the description update nor the
        # gate can mistake one for the other.
        piece_sources = _piece_sources(store, learner, draft.piece_snapshot)

    label = ctx.config.label("session")
    if ctx.args.session is not None and int(ctx.args.session) != draft.session_number:
        # An assertion, not a selector: a learner has one draft at a time, so
        # naming a session cannot choose between them. What it can do is stop
        # a publish that believed it was finishing a different lesson.
        raise UsageError(
            f"{learner.name}'s staged draft is {label} {draft.session_number}, "
            f"not {label} {ctx.args.session}.",
            remedy=f"Publish the draft as it stands with `baton lesson publish "
            f'"{learner.name}"`, or re-stage with `--session {ctx.args.session}` first.',
        )

    if draft.summary is None:
        raise UsageError(
            f"No summary has been accepted for {learner.name} yet.",
            remedy=f'Run `baton lesson contract "{learner.name}"`, then `ingest`.',
        )
    if not draft.doc_id:
        raise UsageError(
            f"{ctx.config.label('session').capitalize()} {draft.session_number} for "
            f"{learner.name} has no document.",
            remedy="Add the document id to the session record, then re-run.",
        )

    # Two places remember a publish, and only one of them survives. The draft
    # is a single file per learner that `stage` overwrites wholesale, so a
    # studio that re-stages the same session to fix a title clears the mark and
    # the gate opens again. The published record is written per session and no
    # `stage` touches it, so it is the one that can still answer tomorrow.
    published_store = _published(ctx)
    published_record = published_store.get(learner.id, draft.session_number)
    published = draft.target_done("docs") or published_record is not None
    _require_force_compatible(draft, published_record, force=ctx.args.force)

    publisher = SummaryPublisher(
        open_docs(ctx.config),
        PreservePolicy.from_config(ctx.config.get("docs.preserve", [])),
        sections=_sections(ctx, learner),
        callout_icon=str(ctx.config.get("summary.callout_icon", "")),
        footer=_footer(ctx),
        timezone=ctx.config.timezone,
    )
    theory = _theory(ctx)

    if ctx.args.dry_run:
        plan = publisher.plan(
            draft.doc_id,
            draft.summary,
            piece_snapshot=draft.piece_snapshot,
            callout_texts=theory,
        )
        ctx.report.result(
            {**plan, "dry_run": True},
            human=f"Would append {plan['would_append']} blocks, delete "
            f"{plan['would_delete']}, and keep {plan['would_preserve']} "
            f"({', '.join(plan['preserved_types']) or 'nothing protected'}), then "
            f"mark the {label} done.",
        )
        return Exit.OK

    if published and not ctx.args.force:
        # Appending the same summary twice leaves two copies on the page and
        # nothing to tell them apart, so a repeat is refused rather than done.
        # The two writes that can legitimately follow a publish are separate
        # from the blocks, though, and re-running is exactly how to finish
        # them: marking the session done, and the YouTube description update.
        docs_state = draft.targets.get("docs", {})

        # Before the description update, which needs a video to describe.
        recording = _stitch_recording(ctx, open_docs(ctx.config), draft, exclude=piece_sources)
        youtube_result = _retry_youtube_description(
            ctx, draft, learner, published_record, exclude=piece_sources
        )
        if youtube_result is not None:
            staging.save(draft)

        if docs_state.get("status") == "ok" and not docs_state.get("completed"):
            return _finish_session(
                ctx,
                publisher,
                staging,
                draft,
                learner.name,
                resumed=True,
                youtube_result=youtube_result,
                recording=recording,
            )

        if youtube_result is not None:
            if youtube_result["status"] == "ok":
                human = (
                    f"{learner.name} {label} {draft.session_number} was already "
                    "published; updated the YouTube description now."
                )
            else:
                human = (
                    f"{learner.name} {label} {draft.session_number} was already "
                    f"published; the YouTube description still failed: "
                    f"{youtube_result['error']}"
                )
            ctx.report.result(
                {**draft.summary_view(), "youtube": youtube_result, "recording": recording},
                human=human + _recording_line(recording),
            )
            return Exit.OK

        ctx.report.result(
            {**draft.summary_view(), "skipped": "already published", "recording": recording},
            human=f"{learner.name} {label} {draft.session_number} "
            f"was already published. Use --force to publish again.{_recording_line(recording)}",
        )
        return Exit.OK

    ctx.report.step(f"publishing {learner.name} {label} {draft.session_number}")
    try:
        result = publisher.publish(
            draft.doc_id,
            draft.summary,
            piece_snapshot=draft.piece_snapshot,
            callout_texts=theory,
        )
    except Exception as exc:
        draft.record_target("docs", "failed", error=str(exc))
        staging.save(draft)
        raise

    draft.record_target("docs", "ok", appended=result.appended, deleted=result.deleted)
    draft.status = PUBLISHED
    staging.save(draft)

    message = render.short_message(
        draft.summary,
        bullet=str(ctx.config.get("summary.short_summary.bullet", "•")),
        labels=_message_labels(ctx, learner),
    )
    published_store.save(draft, short_message=message, doc_url=result.doc_url, blocks=result.blocks)

    # After the blocks, never before: a publish deletes everything the
    # preserve policy does not protect, and a recording linked first would be
    # the thing it deleted.
    docs = open_docs(ctx.config)
    recording = _stitch_recording(ctx, docs, draft, exclude=piece_sources)
    youtube_result = _update_youtube_description(
        ctx, docs, draft.doc_id, learner, draft, exclude=piece_sources
    )
    if youtube_result is not None:
        status = youtube_result["status"]
        extra = {key: value for key, value in youtube_result.items() if key != "status"}
        draft.record_target("youtube", status, **extra)
        staging.save(draft)
        published_store.note_youtube(
            draft.learner_id, draft.session_number, dict(draft.targets["youtube"])
        )

    _finish_session(ctx, publisher, staging, draft, learner.name)

    youtube_line = ""
    if youtube_result and youtube_result["status"] == "ok":
        youtube_line = "\n  YouTube description updated"
    elif youtube_result and youtube_result["status"] == "error":
        youtube_line = f"\n  YouTube description NOT updated: {youtube_result['error']}"

    ctx.report.result(
        {
            **result.to_dict(),
            "completed": draft.targets["docs"].get("completed", {}),
            "youtube": youtube_result,
            "recording": recording,
        },
        human=f"Published {learner.name} {label} "
        f"{draft.session_number}\n"
        f"  appended {result.appended}, removed {result.deleted}, kept {result.preserved}\n"
        f"  marked the {label} done"
        f"{_recording_line(recording)}"
        f"{youtube_line}",
    )
    return Exit.OK


@guarded("lesson")
def handle_unpublish(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, _named(ctx))

    label = ctx.config.label("session")
    records = _published(ctx)
    if ctx.args.session is not None:
        # Unlike `publish`, where --session asserts which draft is meant, a
        # learner has many published sessions and this is a selector: same
        # meaning it carries in `send lesson --session`.
        session = int(ctx.args.session)
        record = records.get(learner.id, session)
        if record is None:
            raise StateError(
                f"No published {label} {session} for {learner.name}.",
                remedy="Drop --session to take the latest, or check the session number.",
            )
    else:
        record = records.latest(learner.id)
        if record is None:
            raise StateError(
                f"Nothing has been published for {learner.name}.",
                remedy="Unpublishing undoes a publish; there is no record of one.",
            )
        session = int(record.get("session_number", 0) or 0)

    if ctx.args.whole_page and not ctx.args.force:
        raise UsageError(
            "`lesson unpublish --whole-page` removes every block on the page.",
            remedy="Re-run with --force once you have checked what else is on it.",
        )

    doc_id = str(record.get("doc_id", ""))
    if not doc_id:
        raise StateError(
            f"The record for {learner.name}'s {label} {session} has no document id.",
            remedy="Nothing can be taken off a page that cannot be named.",
        )

    docs = open_docs(ctx.config)
    recorded = [item for item in (record.get("blocks") or []) if isinstance(item, Mapping)]
    recording_record = str(record.get("kind") or "summary") == RECORDING
    if ctx.args.whole_page:
        plan = plan_whole_page(docs, doc_id)
    elif recorded:
        plan = plan_recorded(docs, doc_id, recorded)
    elif recording_record:
        raise StateError(
            f"The recording record for {learner.name}'s {label} {session} has no block ids.",
            remedy="Nothing can be removed safely. Audit the page, or use "
            "--whole-page --force after checking what else is on it.",
        )
    else:
        # A record written before block ids were kept: attribute the page by
        # re-rendering the summary the record still holds, with the same
        # renderer configuration the publish used.
        publisher = SummaryPublisher(
            docs,
            PreservePolicy.from_config(ctx.config.get("docs.preserve", [])),
            sections=ctx.config.get("summary.sections", {}),
            callout_icon=str(ctx.config.get("summary.callout_icon", "")),
            footer=_footer(ctx),
            timezone=ctx.config.timezone,
        )
        plan = plan_legacy(
            publisher,
            doc_id,
            summary=record.get("summary") or {},
            piece_snapshot=PieceSnapshot.from_record(record),
            callout_texts=_theory(ctx),
        )

    if ctx.args.dry_run:
        ctx.report.result(
            {**plan.to_dict(), "dry_run": True, "learner": learner.name, "session": session},
            human=f"Would remove {len(plan.delete_blocks)} block(s) from {doc_id} "
            f"({plan.mode} attribution), keep {len(plan.keep_blocks)}."
            + (
                f" {len(plan.edited)} edited, {len(plan.ambiguous)} ambiguous: "
                "those would stop the unpublish."
                if plan.needs_human
                else ""
            ),
        )
        return Exit.OK

    if plan.needs_human:
        raise NeedsHumanError(
            f"Refusing to unpublish {learner.name}'s {label} {session}: "
            "blocks on the page no longer match what was published.",
            candidates=[{"kind": "edited", **entry} for entry in plan.edited]
            + [{"kind": "ambiguous", **entry} for entry in plan.ambiguous],
            remedy="Nothing was removed. Fix the document by hand, or take the "
            "whole page down with --whole-page --force.",
        )

    ctx.report.step(f"unpublishing {learner.name} {label} {session}")
    removed = apply_plan(docs, plan)
    # The inverse of the publish's completion write, so `prep` and `next` see
    # a lesson still in progress rather than finished history.
    docs.set_status(doc_id, IN_PROGRESS)

    # The draft only comes back when it is this lesson's own draft, mid-cycle
    # as it was before publishing: re-staging has usually overwritten it with
    # the next lesson by now, and that draft must not be rewound.
    staging = _staging(ctx)
    draft = staging.get(learner.id)
    restored = False
    restored_kind = ""
    if draft is not None and draft.session_number == session and draft.status == PUBLISHED:
        draft.status = STAGED if draft.kind == RECORDING else SUMMARISED
        draft.targets.pop("docs", None)
        staging.save(draft)
        restored = True
        restored_kind = draft.kind

    record_removed = records.remove(learner.id, session)

    lines = [
        f"Unpublished {learner.name} {label} {session}",
        f"  removed {removed} block(s), kept {len(plan.keep_blocks)}",
    ]
    if plan.missing:
        lines.append(f"  {len(plan.missing)} recorded block(s) were already gone")
    if plan.mode == "legacy":
        lines.append("  attributed by re-rendering the stored summary (record has no block ids)")
    lines.append(f"  the {label} is in progress again")
    if restored:
        lines.append(
            "  draft restored to staged recording"
            if restored_kind == RECORDING
            else "  draft restored to summarised"
        )
    else:
        lines.append("  no staged draft to restore")
    lines.append("  a message already sent is not retracted")
    ctx.report.result(
        {
            **plan.to_dict(),
            "removed": removed,
            "draft_restored": restored,
            "record_removed": record_removed,
        },
        human="\n".join(lines),
    )
    return Exit.OK


@guarded("lesson")
def handle_stage_set(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, _named(ctx))

    staging = _staging(ctx)
    draft = staging.require(learner.id, learner.name)
    _require_summary_draft(draft, learner.name)
    if draft.status == PUBLISHED:
        # Amending a published draft would fork it from the published record:
        # the record, not the draft, is what the next lesson is compared
        # against, so the change would be silently irrelevant anyway.
        raise UsageError(
            f"{learner.name}'s {ctx.config.label('session')} {draft.session_number} "
            "is already published.",
            remedy="Unpublish it first (`baton lesson unpublish`), then amend and publish again.",
        )

    field = str(ctx.args.field)
    new_value = str(ctx.args.value)
    old_value = str(getattr(draft, field))
    if old_value == new_value:
        ctx.report.result(
            {"learner": learner.name, "field": field, "unchanged": True},
            human=f"Nothing to do: `{field}` already holds that value.",
        )
        return Exit.OK

    setattr(draft, field, new_value)
    staging.save(draft)

    old_display = old_value if old_value else "(ว่าง)"
    new_display = new_value if new_value else "(ว่าง)"
    ctx.report.result(
        {
            "learner": learner.name,
            "session_number": draft.session_number,
            "field": field,
            "from": old_value,
            "to": new_value,
        },
        human=f"{learner.name} {ctx.config.label('session')} {draft.session_number}: "
        f"{field}\n  เดิม: {old_display}\n  ใหม่: {new_display}",
    )
    return Exit.OK


def handle_remove(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        learner = _resolve(ctx, store, _named(ctx))

    removed = _staging(ctx).remove(learner.id)
    ctx.report.result(
        {"learner": learner.to_dict(), "removed": removed},
        human=f"Discarded the draft for {learner.name}."
        if removed
        else f"No draft was staged for {learner.name}.",
    )
    return Exit.OK


def handle_clear(ctx: Context) -> Exit:
    if not ctx.args.yes:
        raise UsageError(
            "`lesson clear` discards every staged draft.",
            remedy="Re-run with --yes if that is what you want.",
        )
    # A publish that stopped partway leaves the draft as the only record that
    # the work is still owed, so a sweep that deletes it turns "unfinished"
    # into "nobody remembers". Kept drafts are reported, not hidden; --force
    # is the owner saying the debt is written off.
    removed_names, kept = _staging(ctx).clear(keep_unfinished=not ctx.args.force)
    lines = [f"Discarded {len(removed_names)} draft(s)."]
    if kept:
        lines.append("Kept draft(s) whose publish left unfinished targets:")
        for item in kept:
            for name, status in sorted(item["targets"].items()):
                lines.append(f"  {item['learner_name']}: {name} target {status}")
        lines.append(
            '  Finish them with `baton lesson publish "<name>"`, or discard '
            "them anyway with --force."
        )
    ctx.report.result({"removed": len(removed_names), "kept": kept}, human="\n".join(lines))
    return Exit.OK
