"""``baton song``: the catalogue of pieces a studio's learners work through.

A sibling of ``baton learner``, split out because a piece is not owned by any
one learner: it is a shared catalogue, assigned and reassigned by
``learner assign``. Deleting one is refused outright while a learner still
points at it: the studio's own foreign key already enforces this at the
database, but the refusal is nicer read here, with the learners' names in it,
than as a constraint-violation error from whichever driver is configured.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

from ..adapters.db import store_scope
from ..domain.models import Piece
from ..errors import GateError, UpstreamError, UsageError
from ..exits import Exit

if TYPE_CHECKING:
    from .app import Context


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``song`` command group."""
    parser = subparsers.add_parser(
        "song",
        help="List, search, add, edit, and remove pieces.",
        description="The shared catalogue `learner assign` points learners at.",
    )
    group = parser.add_subparsers(dest="song_command", metavar="<subcommand>")
    parser.set_defaults(handler=_require_subcommand)

    listing = group.add_parser("list", help="Every piece.")
    listing.set_defaults(handler=handle_list)

    search = group.add_parser("search", help="Pieces whose title contains a word.")
    search.add_argument("query", metavar="QUERY")
    search.set_defaults(handler=handle_search)

    show = group.add_parser("show", help="One piece, and who is working on it.")
    show.add_argument("id", metavar="ID")
    show.set_defaults(handler=handle_show)

    add = group.add_parser("add", help="Add a piece to the catalogue.")
    add.add_argument("title", metavar="TITLE")
    add.add_argument("--practice-track", default="", metavar="URL")
    add.add_argument("--sheet-link", default="", metavar="URL")
    add.add_argument("--source-link", default="", metavar="URL")
    add.set_defaults(handler=handle_add)

    update = group.add_parser(
        "update",
        help="Change a piece's title or links.",
        description="A flag not given leaves that field alone. Pass an empty "
        'value ("") to clear it.',
    )
    update.add_argument("id", metavar="ID")
    update.add_argument("--title", default=None)
    update.add_argument("--practice-track", default=None, metavar="URL")
    update.add_argument("--sheet-link", default=None, metavar="URL")
    update.add_argument("--source-link", default=None, metavar="URL")
    update.set_defaults(handler=handle_update)

    remove = group.add_parser(
        "remove",
        help="Delete a piece.",
        description="Refused while any learner is still assigned to it.",
    )
    remove.add_argument("id", metavar="ID")
    remove.add_argument("--dry-run", action="store_true")
    remove.set_defaults(handler=handle_remove)


def _require_subcommand(ctx: Context) -> Exit:
    raise UsageError(
        "`baton song` needs a subcommand.",
        remedy='Try `baton song list`, or `baton song add "<title>"`.',
    )


def _get_or_raise(store, piece_id: str, *, label: str) -> Piece:
    piece = store.get_piece(piece_id)
    if piece is None:
        raise UsageError(
            f"No {label} has id `{piece_id}`.",
            remedy="Run `baton song list` to see the ids that exist.",
        )
    return piece


def _piece_line(item: Piece, width: int) -> str:
    extras = []
    if item.practice_track:
        extras.append("track")
    if item.sheet_link:
        extras.append("sheet")
    if item.source_link:
        extras.append("source")
    return f"  {item.id:>4}  {item.title:<{width}}  {' '.join(extras)}"


# -- handlers ------------------------------------------------------------


def handle_list(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        pieces = store.list_pieces()

    payload = {"pieces": [item.to_dict() for item in pieces], "count": len(pieces)}
    if not pieces:
        ctx.report.result(payload, human=f"No {ctx.config.label('pieces')} recorded.")
        return Exit.OK

    width = max(len(item.title) for item in pieces)
    ctx.report.result(payload, human="\n".join(_piece_line(item, width) for item in pieces))
    return Exit.OK


def handle_search(ctx: Context) -> Exit:
    label = ctx.config.label("piece")
    query = ctx.args.query.casefold()
    with store_scope(ctx.config) as store:
        found = [item for item in store.list_pieces() if query in item.title.casefold()]

    payload = {
        "query": ctx.args.query,
        "pieces": [item.to_dict() for item in found],
        "count": len(found),
    }
    if not found:
        ctx.report.result(payload, human=f"No {label} matches “{ctx.args.query}”.")
        return Exit.OK

    width = max(len(item.title) for item in found)
    ctx.report.result(payload, human="\n".join(_piece_line(item, width) for item in found))
    return Exit.OK


def handle_show(ctx: Context) -> Exit:
    label = ctx.config.label("piece")
    with store_scope(ctx.config) as store:
        piece = _get_or_raise(store, ctx.args.id, label=label)
        users = [item for item in store.list_learners() if item.current_piece_id == piece.id]

    payload = {"piece": piece.to_dict(), "used_by": [item.to_dict() for item in users]}
    lines = [
        f"{piece.title}  (id={piece.id})",
        f"  source link  : {piece.source_link or '-'}",
        f"  practice track: {piece.practice_track or '-'}",
        f"  sheet link   : {piece.sheet_link or '-'}",
    ]
    if users:
        lines.append(f"  used by      : {', '.join(item.name for item in users)}")
    else:
        lines.append("  used by      : nobody")
    ctx.report.result(payload, human="\n".join(lines))
    return Exit.OK


def handle_add(ctx: Context) -> Exit:
    with store_scope(ctx.config) as store:
        created = store.add_piece(
            Piece(
                id="",
                title=ctx.args.title,
                source_link=ctx.args.source_link,
                practice_track=ctx.args.practice_track,
                sheet_link=ctx.args.sheet_link,
            )
        )

    ctx.report.result(
        {"piece": created.to_dict()}, human=f"Added {created.title} (id={created.id})."
    )
    return Exit.OK


def handle_update(ctx: Context) -> Exit:
    label = ctx.config.label("piece")
    args = ctx.args
    changes: dict[str, Any] = {}
    for name, flag in (
        ("title", args.title),
        ("source_link", args.source_link),
        ("practice_track", args.practice_track),
        ("sheet_link", args.sheet_link),
    ):
        if flag is not None:
            changes[name] = flag

    if "title" in changes and not changes["title"].strip():
        raise UsageError("--title cannot be blank.", remedy="Give a real title, or omit --title.")
    if not changes:
        raise UsageError(
            "Nothing to update.",
            remedy="Pass at least one of --title, --practice-track, --sheet-link, --source-link.",
        )

    with store_scope(ctx.config) as store:
        _get_or_raise(store, args.id, label=label)
        updated = store.update_piece(args.id, changes)

    if updated is None:
        raise UsageError(
            f"No {label} has id `{args.id}`.",
            remedy="Run `baton song list` to see the ids that exist.",
        )
    ctx.report.result(
        {"piece": updated.to_dict(), "changed": sorted(changes)},
        human=f"Updated {updated.title} (id={updated.id}): {', '.join(sorted(changes))}.",
    )
    return Exit.OK


def handle_remove(ctx: Context) -> Exit:
    label = ctx.config.label("piece")
    with store_scope(ctx.config) as store:
        piece = _get_or_raise(store, ctx.args.id, label=label)
        users = [item for item in store.list_learners() if item.current_piece_id == piece.id]
        if users:
            names = ", ".join(item.name for item in users)
            raise GateError(
                f"{piece.title} is still assigned to {len(users)} learner(s): {names}.",
                remedy="Unassign or reassign them first with `baton learner "
                "assign`, then remove it. Nothing was deleted.",
            )

        if ctx.args.dry_run:
            ctx.report.result(
                {"piece": piece.to_dict(), "dry_run": True},
                human=f"Would remove {piece.title} (id={piece.id}).",
            )
            return Exit.OK

        deleted = store.delete_piece(piece.id)

    if not deleted:
        # get_piece just found it: a deletion that reports nothing removed
        # a moment later is a surprise worth surfacing, not swallowing.
        raise UpstreamError(
            f"{piece.title} was found but the delete removed nothing.",
            service="db",
            remedy="Run `baton song show` on the same id to see its current state.",
        )

    ctx.report.result(
        {"piece": piece.to_dict(), "deleted": True},
        human=f"Removed {piece.title} (id={piece.id}).",
    )
    return Exit.OK
