"""Document-store behaviour: the preserve policy, and Notion's shapes.

The preserve policy is the safety-critical half. It encodes what used to be a
prose warning: "never clear the whole page, the recordings are on it", so a
summary rewrite physically cannot destroy an uploaded video.
"""

from __future__ import annotations

import pytest

from baton.adapters.docs import PreservePolicy, PreserveRule
from baton.adapters.docs.base import Block
from baton.adapters.docs.notion import NotionDocStore
from baton.errors import ConfigError

# A page as it really looks after a few weeks: summary text interleaved with
# a recording, a sheet-music embed, and a practice-track callout.
PAGE = [
    Block(id="b1", type="heading_2", text="Lesson 3"),
    Block(id="b2", type="paragraph", text="Worked through the B section."),
    Block(id="b3", type="video", url="https://example.invalid/watch/lesson-3"),
    Block(id="b4", type="bulleted_list_item", text="Slow the tempo to 80bpm"),
    Block(id="b5", type="embed", url="https://example.invalid/sheets/blackbird.pdf"),
    Block(id="b6", type="callout", text="Practice track", icon="🎧"),
    Block(id="b7", type="callout", text="Watch the third finger", icon="⚠️"),
    Block(id="b8", type="divider"),
]

POLICY = PreservePolicy.from_config(
    [
        {"type": "video"},
        {"type": "embed"},
        {"type": "callout", "icon": "🎧"},
    ]
)


def test_recordings_and_attachments_survive_a_rewrite():
    preserved, replaceable = POLICY.partition(PAGE)

    assert [b.id for b in preserved] == ["b3", "b5", "b6"]
    assert "b3" not in [b.id for b in replaceable]


def test_summary_text_is_replaceable():
    _, replaceable = POLICY.partition(PAGE)

    assert [b.id for b in replaceable] == ["b1", "b2", "b4", "b7", "b8"]


def test_icon_narrows_a_rule_to_one_kind_of_callout():
    """The practice-track callout is protected; an ordinary tip is not."""
    assert POLICY.preserves(PAGE[5]) is True  # 🎧
    assert POLICY.preserves(PAGE[6]) is False  # ⚠️


def test_startswith_narrows_by_text():
    policy = PreservePolicy.from_config([{"type": "heading_3", "startswith": "🎵"}])

    assert policy.preserves(Block(id="x", type="heading_3", text="🎵 Blackbird")) is True
    assert policy.preserves(Block(id="y", type="heading_3", text="Focus areas")) is False


def test_an_unknown_block_type_is_replaceable_not_silently_protected():
    """The policy is an allowlist: a type nobody configured is summary text.

    The alternative (protecting anything unrecognised) would quietly stop
    rewrites from working and be very hard to diagnose.
    """
    exotic = Block(id="z", type="synced_block", text="")

    assert POLICY.preserves(exotic) is False


def test_empty_policy_makes_everything_replaceable():
    policy = PreservePolicy.from_config([])
    preserved, replaceable = policy.partition(PAGE)

    assert preserved == []
    assert len(replaceable) == len(PAGE)


def test_partition_is_a_complete_split():
    preserved, replaceable = POLICY.partition(PAGE)

    assert len(preserved) + len(replaceable) == len(PAGE)
    assert {b.id for b in preserved} & {b.id for b in replaceable} == set()


@pytest.mark.parametrize("bad", ["not a list", [{"icon": "🎧"}], [["video"]]])
def test_malformed_preserve_config_is_rejected(bad):
    with pytest.raises(ConfigError):
        PreservePolicy.from_config(bad)


def test_rule_requires_every_stated_condition():
    rule = PreserveRule(type="callout", icon="🎧", startswith="Practice")

    assert rule.matches(Block(id="a", type="callout", text="Practice track", icon="🎧"))
    assert not rule.matches(Block(id="b", type="callout", text="Other", icon="🎧"))
    assert not rule.matches(Block(id="c", type="callout", text="Practice track", icon="📌"))


# -- Notion property flattening ---------------------------------------------


@pytest.mark.parametrize(
    ("prop", "expected"),
    [
        ({"type": "status", "status": {"name": "In progress"}}, "In progress"),
        ({"type": "select", "select": {"name": "Done"}}, "Done"),
        ({"type": "date", "date": {"start": "2026-08-16"}}, "2026-08-16"),
        (
            {"type": "rich_text", "rich_text": [{"plain_text": "Blackbird"}]},
            "Blackbird",
        ),
        ({"type": "title", "title": [{"plain_text": "Lesson 3"}]}, "Lesson 3"),
        ({"type": "number", "number": 3}, "3"),
        ({"type": "checkbox", "checkbox": True}, "True"),
        ({"type": "status", "status": None}, ""),
        ({"type": "people", "people": []}, ""),
    ],
)
def test_a_property_is_read_whatever_type_the_database_used(prop, expected):
    """Studios build the same field as `status`, `select`, or `rich_text`.
    Assuming one shape is why adopting someone else's database used to fail."""
    assert NotionDocStore._read_property(prop) == expected


def test_multi_select_joins_its_names():
    prop = {"type": "multi_select", "multi_select": [{"name": "solo"}, {"name": "exam"}]}

    assert NotionDocStore._read_property(prop) == "solo, exam"


def test_block_parsing_pulls_text_icon_and_url():
    raw = {
        "id": "abc",
        "type": "callout",
        "callout": {
            "rich_text": [{"plain_text": "Practice "}, {"plain_text": "track"}],
            "icon": {"emoji": "🎧"},
        },
    }

    block = NotionDocStore._block(raw)

    assert block.text == "Practice track"
    assert block.icon == "🎧"
    assert block.raw is raw


@pytest.mark.parametrize(
    ("kind", "body", "expected_url"),
    [
        ("video", {"external": {"url": "https://example.invalid/v"}}, "https://example.invalid/v"),
        ("video", {"file": {"url": "https://example.invalid/f"}}, "https://example.invalid/f"),
        ("embed", {"url": "https://example.invalid/e"}, "https://example.invalid/e"),
        ("bookmark", {"url": "https://example.invalid/b"}, "https://example.invalid/b"),
    ],
)
def test_urls_are_extracted_from_every_link_bearing_block(kind, body, expected_url):
    block = NotionDocStore._block({"id": "x", "type": kind, kind: body})

    assert block.url == expected_url


def test_missing_property_config_names_the_setting_to_add():
    store = NotionDocStore(token="t", properties={})

    with pytest.raises(ConfigError) as excinfo:
        store._property_name("status")

    assert "docs.properties.status" in (excinfo.value.remedy or "")


# -- Notion 404s: page-access vs block-already-gone ---------------------------


class _Reply:
    """The two shapes a Notion reply needs for the error paths."""

    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self) -> dict:
        return self._payload


def test_a_page_404_names_the_page_and_both_ways_to_fix_it(monkeypatch):
    """One code, two causes, one order. The remedy must let a person fix the
    sharing (or an agent re-check the id) without a debugging session: the
    old message stopped at "usually not shared"."""
    import baton.adapters.docs.notion as notion_module

    store = NotionDocStore(token="t", properties={})

    def refused(*_args, **_kwargs):
        return _Reply(404, {"object": "error", "code": "object_not_found"})

    monkeypatch.setattr(notion_module, "http_request", refused)

    with pytest.raises(ConfigError) as excinfo:
        store.get_status("abc123")

    assert "abc123" in excinfo.value.message
    remedy = excinfo.value.remedy or ""
    assert "Connections" in remedy
    assert "stale id" in remedy
    assert excinfo.value.details["page_id"] == "abc123"


def test_deleting_a_block_that_is_already_gone_counts_as_deleted(monkeypatch):
    """A publish that died mid-delete leaves blocks already removed; the
    re-run must not wedge on them. Deleting a block that no longer exists is
    the outcome the delete wanted, not an error."""
    import baton.adapters.docs.notion as notion_module

    store = NotionDocStore(token="t", properties={})

    def gone(*_args, **_kwargs):
        return _Reply(404, {"object": "error", "code": "object_not_found"})

    monkeypatch.setattr(notion_module, "http_request", gone)

    assert store.delete_blocks(["b1", "b2"]) == 2


def test_a_status_read_can_decline_to_count_the_blocks(monkeypatch):
    """Counting means listing the page: a request of its own, and one more
    per hundred blocks. Every caller used to pay it, including the ones that
    wanted a single status word."""
    import baton.adapters.docs.notion as notion_module

    store = NotionDocStore(token="t", properties={})
    paths: list[str] = []

    def record(_method, url, **_kwargs):
        paths.append(url)
        if "/children" in url:
            return _Reply(200, {"results": [], "has_more": False})
        return _Reply(200, {"properties": {}, "url": "https://notion.invalid/p"})

    monkeypatch.setattr(notion_module, "http_request", record)

    lean = store.get_status("abc123", with_blocks=False)
    assert len(paths) == 1
    # Not zero: nobody looked, and a zero here reads as "the page is empty",
    # which is what decides whether a summary may be written onto it.
    assert lean.block_count is None

    paths.clear()
    full = store.get_status("abc123")
    assert len(paths) == 2
    assert full.block_count == 0


def test_clearing_empties_only_the_named_properties(monkeypatch):
    """A cancel undoes one booking. It must send the date's own empty shape
    and leave every other column on the page exactly as a person left it."""
    import baton.adapters.docs.notion as notion_module

    store = NotionDocStore(
        token="t", properties={"date": "Date", "status": "Status", "titles": "Titles"}
    )
    patches: list[dict] = []

    def record(method, _url, **kwargs):
        if method == "PATCH":
            patches.append(kwargs.get("json") or {})
            return _Reply(200, {})
        return _Reply(
            200,
            {
                "properties": {
                    "Date": {"type": "date", "date": {"start": "2026-09-26"}},
                    "Status": {"type": "status", "status": {"name": "In progress"}},
                    "Titles": {"type": "rich_text", "rich_text": [{"plain_text": "Blues"}]},
                }
            },
        )

    monkeypatch.setattr(notion_module, "http_request", record)

    assert store.clear_properties("abc123", ["date"]) == ["date"]
    assert patches == [{"properties": {"Date": {"date": None}}}]


def test_clearing_a_column_the_page_lacks_sends_nothing(monkeypatch):
    import baton.adapters.docs.notion as notion_module

    store = NotionDocStore(token="t", properties={"date": "Date"})
    methods: list[str] = []

    def record(method, _url, **_kwargs):
        methods.append(method)
        return _Reply(200, {"properties": {}})

    monkeypatch.setattr(notion_module, "http_request", record)

    assert store.clear_properties("abc123", ["date"]) == []
    assert methods == ["GET"]


# -- where new blocks land ---------------------------------------------------
#
# A store cannot move a block that already exists: Notion has no such
# operation, and "move it to the top" can only be spelled as inserting there.
# The video pipeline needs it because encoding outlasts the writing of the
# summary, so the recording usually arrives after the summary is already down.


def _capture_appends(monkeypatch):
    import baton.adapters.docs.notion as notion_module

    bodies: list[dict] = []

    def record(_method, _url, **kwargs):
        bodies.append(kwargs.get("json") or {})
        return _Reply(200, {"results": []})

    monkeypatch.setattr(notion_module, "http_request", record)
    return bodies


def _paragraphs(count: int) -> list[dict]:
    return [
        {"object": "block", "type": "paragraph", "paragraph": {"n": index}}
        for index in range(count)
    ]


def test_appending_says_nothing_about_position(monkeypatch):
    """The default has to stay wire-identical. Every existing caller appends,
    and a `position` Notion did not ask for is a new way for them to fail."""
    bodies = _capture_appends(monkeypatch)
    store = NotionDocStore(token="t", properties={})

    store.append_blocks("doc", _paragraphs(2))

    assert len(bodies) == 1
    assert "position" not in bodies[0]


def test_inserting_at_the_start_asks_notion_for_it(monkeypatch):
    """`start` is one of the three the API accepts (`after_block`, `start`,
    `end`), and it is accepted by the version this client pins."""
    bodies = _capture_appends(monkeypatch)
    store = NotionDocStore(token="t", properties={})

    store.append_blocks("doc", _paragraphs(2), position="start")

    assert bodies[0]["position"] == {"type": "start"}


def test_a_long_payload_going_to_the_start_keeps_its_reading_order(monkeypatch):
    """Each request puts its own chunk at the top, so sending them in reading
    order leaves the last chunk above the first: a payload reversed a hundred
    blocks at a time, and only ever visible past the chunking threshold."""
    bodies = _capture_appends(monkeypatch)
    store = NotionDocStore(token="t", properties={})

    store.append_blocks("doc", _paragraphs(250), position="start")

    assert [len(body["children"]) for body in bodies] == [50, 100, 100]
    # Reassembled in the order the requests land, the page reads as it was given.
    landed = [block["paragraph"]["n"] for body in bodies for block in body["children"]]
    assert landed == list(range(200, 250)) + list(range(100, 200)) + list(range(100))


def test_appending_a_long_payload_still_goes_in_reading_order(monkeypatch):
    bodies = _capture_appends(monkeypatch)
    store = NotionDocStore(token="t", properties={})

    store.append_blocks("doc", _paragraphs(250))

    assert [len(body["children"]) for body in bodies] == [100, 100, 50]
