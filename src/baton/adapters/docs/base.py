"""What a session-document store must do.

A "document" is the page a learner and their family actually read: what was
covered, what to practise, the recording. Baton needs four things from it:
read the status and date, list the blocks, replace the summary without
destroying anything else, and append.

The fourth is the one that matters. :class:`PreservePolicy` encodes the rule
that used to live in prose ("never clear the whole page: the videos and sheet
links are on it"), so a rewrite physically cannot delete an uploaded recording.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class DocStatus:
    """The state of one session document."""

    doc_id: str
    status: str = ""
    date: str = ""
    titles: str = ""
    block_count: int | None = 0
    """How many blocks are on the page: ``None`` when nobody counted.

    Counting means listing the page, which is a request the caller pays for,
    so `get_status(with_blocks=False)` skips it and says so here rather than
    reporting a zero that reads as "the page is empty".
    """

    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "status": self.status,
            "date": self.date,
            "titles": self.titles,
            "block_count": self.block_count,
            "url": self.url,
        }


@dataclass(frozen=True)
class Block:
    """One block on a document, in the store's own shape.

    ``raw`` is kept whole so a block can be classified and then left alone
    without Baton having to model every block type a studio might use.
    """

    id: str
    type: str
    text: str = ""
    icon: str = ""
    url: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


#: Block shapes that may hold a session's recording link. Notion's UI turns a
#: pasted URL into a bookmark, so a recording added by hand rather than by the
#: pipeline is a bookmark: reading `video` blocks alone made such a page look
#: to Baton like it had no recording at all.
VIDEO_LINK_BLOCKS: tuple[str, ...] = ("video", "bookmark", "embed")

#: URL markers that make a bookmark or embed a recording rather than a link to
#: something else (a sheet, an article). A `video` block is exempt: its URL was
#: chosen as a recording, whatever host it is on. The studio this came from
#: filtered exactly these hosts in exactly these shapes.
_VIDEO_HOSTS: tuple[str, ...] = ("youtube", "youtu.be")


def video_identity(url: str) -> str:
    """A comparable identity for a video URL.

    ``youtu.be/ID``, ``watch?v=ID`` and ``/embed/ID`` are one video written
    three ways, and Notion rewrites freely between them, so comparing the
    strings lets the same video pass twice as two different ones. Anything
    that is not a YouTube URL has no id to compare and falls back to the URL
    itself, folded only where a difference never means a different page.
    """
    from ..media.google import extract_video_id  # pure string parsing, no extra needed

    return extract_video_id(url) or url.strip().rstrip("/").casefold()


def find_video_link(
    docs: DocStore,
    doc_id: str,
    *,
    blocks: tuple[str, ...] = VIDEO_LINK_BLOCKS,
    exclude: Iterable[str] = (),
) -> str:
    """This session's recording link on its document, or "" when there is none.

    Shared by every caller that needs "the recording, if any": `baton send`
    and the YouTube description step both read the same document the same
    way, rather than each growing its own copy that quietly drifts from the
    other (the original system had this duplicated across skills).

    Two rules keep the *song being learnt* from being read as the recording of
    the lesson. A studio keeps both on the same page, and once the song is a
    YouTube link (for a pop song it always is), nothing in the shape of the
    URL tells the two apart:

    * a `video` block wins over a bookmark or an embed however far up the page
      it sits. The pipeline writes the recording as a `video` block; Notion
      turns a pasted song URL into a bookmark and an embed, and those land
      below it. Reading the last link on the page therefore read the song.
    * a URL named in ``exclude`` is never the recording, in any shape.

    Args:
        blocks: Block types that may hold the link. The default matches the
            shapes the previous system accepted; ``docs.video_link_blocks``
            overrides it per studio.
        exclude: URLs that are something other than this lesson's recording:
            in practice the piece's own ``source_link``. Compared by video
            identity, not as strings, so the song still matches after Notion
            has rewritten it into another of YouTube's URL shapes.

    A document-store failure degrades to "no recording link" rather than
    raising: the field is optional everywhere it is read, so an outage here
    must not stop whatever the caller was doing for its own required data.
    """
    from ...errors import BatonError

    if not doc_id:
        return ""
    try:
        page = docs.list_blocks(doc_id)
    except BatonError:
        return ""
    return recording_in(page, blocks=blocks, exclude=exclude)


def recording_in(
    page: list[Block],
    *,
    blocks: tuple[str, ...] = VIDEO_LINK_BLOCKS,
    exclude: Iterable[str] = (),
) -> str:
    """The recording link among blocks already read; see :func:`find_video_link`.

    Split out so a caller that reads a page for other reasons too (every
    lesson of a course, section by section) picks the recording by the same
    two rules without reading the page a second time.
    """
    excluded = {video_identity(url) for url in exclude if url}
    candidates = [
        block
        for block in reversed(page)
        if block.type in blocks
        and block.url
        and (block.type == "video" or any(host in block.url for host in _VIDEO_HOSTS))
        and video_identity(block.url) not in excluded
    ]
    for block in candidates:
        if block.type == "video":
            return block.url
    return candidates[0].url if candidates else ""


@dataclass(frozen=True)
class PreserveRule:
    """One condition under which a block survives a rewrite."""

    type: str
    icon: str = ""
    startswith: str = ""

    def matches(self, block: Block) -> bool:
        """Whether this rule protects ``block``.

        All stated conditions must hold. A rule naming only a type protects
        every block of that type; adding ``icon`` or ``startswith`` narrows it,
        which is how a studio protects its practice-track callouts without also
        freezing every other callout on the page.
        """
        if block.type != self.type:
            return False
        if self.icon and block.icon != self.icon:
            return False
        return not (self.startswith and not block.text.startswith(self.startswith))


@dataclass(frozen=True)
class PreservePolicy:
    """The set of rules deciding what a summary rewrite may remove."""

    rules: tuple[PreserveRule, ...]

    @classmethod
    def from_config(cls, raw: Any) -> PreservePolicy:
        """Build from the ``docs.preserve`` list.

        Raises:
            ConfigError: The list is malformed.
        """
        from ...errors import ConfigError

        if raw is None:
            return cls(rules=())
        if not isinstance(raw, list):
            raise ConfigError(
                "`docs.preserve` must be a list of rules.",
                remedy="Each entry is a mapping with a `type`, optionally an "
                "`icon` or `startswith`.",
            )
        rules = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict) or "type" not in entry:
                raise ConfigError(
                    f"`docs.preserve[{index}]` must be a mapping with a `type`.",
                    remedy="For example: `- {type: video}`.",
                )
            rules.append(
                PreserveRule(
                    type=str(entry["type"]),
                    icon=str(entry.get("icon", "")),
                    startswith=str(entry.get("startswith", "")),
                )
            )
        return cls(rules=tuple(rules))

    def preserves(self, block: Block) -> bool:
        """Whether any rule protects this block."""
        return any(rule.matches(block) for rule in self.rules)

    def partition(self, blocks: list[Block]) -> tuple[list[Block], list[Block]]:
        """Split blocks into ``(preserved, replaceable)``.

        Returns:
            Two lists. Everything not explicitly preserved is replaceable:
            the policy is an allowlist, so a block type nobody thought about
            is treated as summary text rather than silently protected.
        """
        preserved = [b for b in blocks if self.preserves(b)]
        replaceable = [b for b in blocks if not self.preserves(b)]
        return preserved, replaceable


@dataclass(frozen=True)
class DocPage:
    """Where a page sits, so a course's filing can be read rather than assumed.

    ``parent_id`` is whatever holds the page: in Notion that can be another
    page or a block, because a studio may keep its course pages inside a
    callout rather than directly under a page. Callers only ever compare it or
    list its children, so the distinction is carried in ``parent_kind`` for
    reporting and otherwise left alone.
    """

    doc_id: str
    title: str
    parent_id: str
    parent_kind: str
    trashed: bool = False
    url: str = ""


@dataclass(frozen=True)
class DocChild:
    """One entry inside a page or block: a sub-page, or an embedded table."""

    child_id: str
    kind: str
    title: str


@dataclass(frozen=True)
class TableRow:
    """One row of an embedded table, read through the configured properties."""

    row_id: str
    title: str
    date: str
    status: str


#: Where new blocks go. Deliberately not the full set the stores support:
#: nothing needs to anchor to a particular block, and a position that names one
#: would have to carry a block id through every layer to say so.
BlockPosition = Literal["start", "end"]


@runtime_checkable
class DocStore(Protocol):
    """Read and update session documents."""

    def get_status(self, doc_id: str, *, with_blocks: bool = True) -> DocStatus:
        """Status, date, titles, and (unless declined) block count.

        Counting the blocks means listing the page: a second request, and one
        more again per hundred blocks. Callers that only want the status word
        or the date pass ``with_blocks=False`` and get ``block_count=None``.
        """
        ...

    def get_page(self, doc_id: str) -> DocPage:
        """Identity and parentage of one page."""
        ...

    def list_children(self, doc_id: str) -> list[DocChild]:
        """Sub-pages and embedded tables directly inside a page or block."""
        ...

    def get_table(self, table_id: str) -> DocPage:
        """Identity and parentage of an embedded table.

        ``parent_id`` is the page the table sits on: read that page to learn
        what the table is part of.
        """
        ...

    def table_rows(self, table_id: str) -> list[TableRow]:
        """Every row of an embedded table."""
        ...

    def reset_properties(self, doc_id: str) -> list[str]:
        """Clear every writable property except the title. Returns their names."""
        ...

    def clear_properties(self, doc_id: str, keys: list[str]) -> list[str]:
        """Empty only the named properties, by their ``docs.properties`` key.

        The narrow counterpart to :meth:`reset_properties`, for undoing one
        write exactly: a cancel empties the date a booking set and leaves
        everything a person typed on the page alone. A key the page has no
        column for is skipped, as :meth:`set_properties` skips it.

        Returns:
            The keys actually cleared.
        """
        ...

    def restore(self, doc_id: str) -> bool:
        """Bring a page back from the trash. True when it is usable afterwards."""
        ...

    def set_status(self, doc_id: str, status: str) -> None:
        """Set the document's status property to a configured value."""
        ...

    def set_properties(self, doc_id: str, values: dict[str, str]) -> list[str]:
        """Set configured properties by their ``docs.properties`` key.

        Keys are Baton's own (``status``, ``date``, ``titles``), not the
        studio's column names, so callers never have to know what a profile
        called its columns. An empty value is skipped rather than written:
        clearing a property is :meth:`reset_properties`' job, and a publish
        that half-knows a date must not erase the one already there.

        Returns:
            The keys actually written.
        """
        ...

    def list_blocks(self, doc_id: str) -> list[Block]:
        """Every top-level block, in order."""
        ...

    def append_blocks(
        self,
        doc_id: str,
        blocks: list[dict[str, Any]],
        *,
        position: BlockPosition = "end",
    ) -> None:
        """Add blocks, chunking to respect the store's per-request limit.

        Args:
            doc_id: The page the blocks go on.
            blocks: The blocks, in the order they should read.
            position: ``"end"`` appends, which is what every caller wanted
                until the video pipeline needed to put a recording above a
                summary that was published before it arrived.

        A store cannot *move* a block that already exists, because Notion has
        no such operation, so ``"start"`` is how a block reaches the top.
        """
        ...

    def create_page(self, parent_id: str, title: str, blocks: list[dict[str, Any]]) -> DocStatus:
        """Create a sub-page under ``parent_id`` and return its identity."""
        ...

    def delete_blocks(self, block_ids: list[str]) -> int:
        """Delete blocks by id. Returns how many were removed."""
        ...

    def health(self) -> None:
        """Prove the store is reachable and the credentials work."""
        ...
