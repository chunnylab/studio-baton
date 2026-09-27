"""Session documents in Notion.

Property names and status values are read from configuration, never hardcoded:
the original scattered ``"Status"``, ``"Titles"`` and ``"Date (Flexible)"``
across twenty call sites, which is precisely why adopting someone else's
database meant editing code.

Two API facts shape this module. Notion accepts at most 100 children per
request, so appends are chunked. And a property can be one of several types
(``status``, ``select``, ``rich_text``, …) depending on how the database was
built, so reads accept whichever shape arrives rather than assuming one.
"""

from __future__ import annotations

from typing import Any

from ...core.config import Config
from ...core.retry import http_request
from ...errors import ConfigError, UpstreamError
from .base import (
    Block,
    BlockPosition,
    DocChild,
    DocPage,
    DocStatus,
    PreservePolicy,
    TableRow,
)

API_ROOT = "https://api.notion.com/v1"

#: Notion's documented ceiling on children per append request.
MAX_CHILDREN_PER_REQUEST = 100

#: Property types Notion computes. Writing to one is an error, not a no-op.
_READ_ONLY_PROPERTIES = frozenset(
    {
        "rollup",
        "formula",
        "created_time",
        "created_by",
        "last_edited_time",
        "last_edited_by",
        "unique_id",
        "button",
        "verification",
        "last_visited_time",
    }
)

#: What "empty" is for each writable property type.
_EMPTY_PROPERTY: dict[str, Any] = {
    "rich_text": list,
    "date": lambda: None,
    "select": lambda: None,
    "multi_select": list,
    "number": lambda: None,
    "checkbox": lambda: False,
    "url": lambda: None,
    "email": lambda: None,
    "phone_number": lambda: None,
    "people": list,
    "files": list,
    "relation": list,
    "status": lambda: None,
}

#: Block types whose text lives under a ``rich_text`` array.
_RICH_TEXT_TYPES = frozenset(
    {
        "paragraph",
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "numbered_list_item",
        "to_do",
        "toggle",
        "quote",
        "callout",
        "code",
    }
)


class NotionDocStore:
    """A :class:`~baton.adapters.docs.base.DocStore` backed by Notion."""

    driver = "notion"

    def __init__(
        self,
        token: str,
        *,
        api_version: str = "2022-06-28",
        properties: dict[str, str] | None = None,
        statuses: dict[str, str] | None = None,
        preserve: PreservePolicy | None = None,
        timeout: float = 30.0,
        api_root: str = API_ROOT,
    ) -> None:
        self.token = token
        self.api_version = api_version
        self.properties = properties or {}
        self.statuses = statuses or {}
        self.preserve = preserve or PreservePolicy(rules=())
        self.timeout = timeout
        self.api_root = api_root.rstrip("/")

    @classmethod
    def from_config(cls, config: Config) -> NotionDocStore:
        return cls(
            token=str(config.secret("docs.notion.token_env")),
            api_version=str(config.get("docs.notion.api_version", "2022-06-28")),
            properties={k: str(v) for k, v in config.section("docs.properties").items()},
            statuses={k: str(v) for k, v in config.section("docs.statuses").items()},
            preserve=PreservePolicy.from_config(config.get("docs.preserve", [])),
        )

    # -- transport ---------------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": self.api_version,
            "Accept": "application/json",
        }

    def _request(
        self, method: str, path: str, json_body: Any = None, *, op: str = "page"
    ) -> dict[str, Any]:
        headers = self._headers
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        response = http_request(
            method,
            f"{self.api_root}{path}",
            service="notion",
            headers=headers,
            json=json_body,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            raise ConfigError(
                "Notion rejected the integration token.",
                remedy="Check the token named by docs.notion.token_env.",
            )
        if response.status_code == 404:
            if op == "block-delete":
                # The block is already gone: deleted by a person in Notion,
                # or by an earlier run of this same publish that died before
                # recording it. Deletion is idempotent; refusing here is what
                # used to wedge a publish halfway with no way to resume.
                # A page-access 404 cannot reach this branch: publishing
                # appends to the page before it deletes, so access to the
                # page is already proven by the time a block is deleted.
                return {}
            parts = path.strip("/").split("/")
            page_id = parts[1] if len(parts) > 1 else path
            raise ConfigError(
                f"Notion cannot see page `{page_id}` (404).",
                remedy="Two causes, in order of likelihood. The page is not shared "
                "with the integration: in Notion, open the page, ⋯ → Connections, "
                "add this integration, or the page was deleted and the session "
                "points at a stale id. Share the page or fix the id, then re-run: "
                "a publish that failed here had already appended, and re-running "
                "replaces cleanly.",
                details={"page_id": page_id, "status_code": 404},
            )
        if response.status_code >= 400:
            detail = response.text[:300]
            raise UpstreamError(
                f"Notion rejected the request: {detail}",
                service="notion",
                status=response.status_code,
            )

        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(
                "Notion returned a response that is not JSON.",
                service="notion",
                status=response.status_code,
            ) from exc

    # -- properties --------------------------------------------------------

    def _property_name(self, key: str) -> str:
        try:
            return self.properties[key]
        except KeyError:
            raise ConfigError(
                f"No Notion property is configured for `{key}`.",
                remedy=f"Add docs.properties.{key} to baton.yaml.",
            ) from None

    @staticmethod
    def _read_property(prop: dict[str, Any]) -> str:
        """Flatten whichever property shape Notion returned into text."""
        kind = prop.get("type", "")
        value = prop.get(kind)
        if value is None:
            return ""
        if kind in ("status", "select"):
            return str(value.get("name", "")) if isinstance(value, dict) else ""
        if kind == "date":
            return str(value.get("start", "")) if isinstance(value, dict) else ""
        if kind in ("rich_text", "title"):
            return "".join(part.get("plain_text", "") for part in value)
        if kind == "multi_select":
            return ", ".join(item.get("name", "") for item in value)
        if kind in ("number", "checkbox", "url", "email", "phone_number"):
            return str(value)
        return ""

    def get_status(self, doc_id: str, *, with_blocks: bool = True) -> DocStatus:
        page = self._request("GET", f"/pages/{doc_id}")
        properties = page.get("properties", {}) or {}

        def read(key: str) -> str:
            name = self.properties.get(key)
            if not name or name not in properties:
                return ""
            return self._read_property(properties[name])

        # Listing the page is a request of its own, and one more per hundred
        # blocks after that. Every caller used to pay it, including the ones
        # that wanted a single status word, which, over a day's roster, is
        # twice the calls for a number nobody read.
        blocks = self.list_blocks(doc_id) if with_blocks else None
        return DocStatus(
            doc_id=doc_id,
            status=read("status"),
            date=read("date"),
            titles=read("titles"),
            block_count=len(blocks) if blocks is not None else None,
            url=str(page.get("url", "")),
        )

    @staticmethod
    def _write_property(kind: str, value: str) -> dict[str, Any] | None:
        """The body Notion expects for one property of type ``kind``.

        Returns ``None`` for a type Baton has no faithful way to write, so an
        unknown column is left alone rather than filled with a guess.
        """
        if kind in ("status", "select"):
            return {kind: {"name": value}}
        if kind == "date":
            return {"date": {"start": value}}
        if kind in ("rich_text", "title"):
            return {kind: [{"type": "text", "text": {"content": value}}]}
        if kind == "multi_select":
            parts = [part.strip() for part in value.split(",")]
            return {"multi_select": [{"name": part} for part in parts if part]}
        if kind in ("url", "email", "phone_number"):
            return {kind: value}
        return None

    def set_properties(self, doc_id: str, values: dict[str, str]) -> list[str]:
        """Set configured properties, matching each column's own type.

        The page is read first to learn what type every target column actually
        is. Notion validates the body against the property's type, so a payload
        assembled from a guess ("it is probably a select") is rejected outright
        on the studios whose guess was wrong, and this is the write that
        finishes a session, which must not be the one that fails.
        """
        wanted = {key: str(value) for key, value in values.items() if str(value)}
        if not wanted:
            return []
        # A caller says `done`; the profile decides that reads "Done", or
        # "เสร็จแล้ว". Resolving here means every caller can be written in
        # Baton's vocabulary.
        if "status" in wanted:
            wanted["status"] = self.statuses.get(wanted["status"], wanted["status"])

        page = self._request("GET", f"/pages/{doc_id}")
        properties = page.get("properties", {}) or {}

        payload: dict[str, Any] = {}
        written: list[str] = []
        for key, value in wanted.items():
            name = self._property_name(key)
            prop = properties.get(name)
            if not isinstance(prop, dict):
                continue
            kind = str(prop.get("type", ""))
            if kind in _READ_ONLY_PROPERTIES:
                continue
            body = self._write_property(kind, value)
            if body is None:
                continue
            payload[name] = body
            written.append(key)

        if payload:
            self._request("PATCH", f"/pages/{doc_id}", {"properties": payload})
        return written

    def set_status(self, doc_id: str, status: str) -> None:
        """Set the status property, resolving a configured key to its value.

        Accepts either a key from ``docs.statuses`` (``done``) or the literal
        value (``Done``), so callers can be written against Baton's vocabulary
        while the profile decides the wording.
        """
        self.set_properties(doc_id, {"status": status})

    # -- blocks ------------------------------------------------------------

    @classmethod
    def _block(cls, raw: dict[str, Any]) -> Block:
        kind = str(raw.get("type", ""))
        body = raw.get(kind, {}) or {}

        text = ""
        if kind in _RICH_TEXT_TYPES:
            text = "".join(part.get("plain_text", "") for part in body.get("rich_text", []))

        icon = ""
        icon_body = body.get("icon") or {}
        if isinstance(icon_body, dict):
            icon = str(icon_body.get("emoji", ""))

        url = ""
        if kind == "video":
            source = body.get("external") or body.get("file") or {}
            url = str(source.get("url", "")) if isinstance(source, dict) else ""
        elif kind in ("embed", "bookmark", "link_preview"):
            url = str(body.get("url", ""))

        return Block(id=str(raw.get("id", "")), type=kind, text=text, icon=icon, url=url, raw=raw)

    def list_blocks(self, doc_id: str) -> list[Block]:
        """Every top-level block, following pagination to the end."""
        blocks: list[Block] = []
        cursor: str | None = None
        while True:
            path = f"/blocks/{doc_id}/children?page_size=100"
            if cursor:
                path = f"{path}&start_cursor={cursor}"
            payload = self._request("GET", path)
            blocks.extend(self._block(raw) for raw in payload.get("results", []))
            if not payload.get("has_more"):
                return blocks
            cursor = payload.get("next_cursor")
            if not cursor:
                return blocks

    def append_blocks(
        self,
        doc_id: str,
        blocks: list[dict[str, Any]],
        *,
        position: BlockPosition = "end",
    ) -> None:
        """Add blocks in chunks of at most 100.

        Chunking is not an optimisation: Notion rejects a larger request
        outright, and the original system worked around it by asking the model
        to split the payload itself.

        Chunks go out in reverse for ``"start"``. Each request puts its chunk
        at the top, so sending them in reading order would leave the last chunk
        above the first: the payload reversed a hundred blocks at a time.

        ``position`` is accepted by the API version this client pins
        (2022-06-28), verified against the live API rather than assumed.
        """
        chunks = [
            blocks[start : start + MAX_CHILDREN_PER_REQUEST]
            for start in range(0, len(blocks), MAX_CHILDREN_PER_REQUEST)
        ]
        for chunk in reversed(chunks) if position == "start" else chunks:
            body: dict[str, Any] = {"children": chunk}
            if position != "end":
                body["position"] = {"type": position}
            self._request("PATCH", f"/blocks/{doc_id}/children", body)

    def create_page(self, parent_id: str, title: str, blocks: list[dict[str, Any]]) -> DocStatus:
        """Create a sub-page, then append anything past the first request.

        Notion accepts at most 100 children when creating a page, so a longer
        note is created with the first batch and topped up, rather than the
        caller being told to shorten it.
        """
        batches = [
            blocks[index : index + MAX_CHILDREN_PER_REQUEST]
            for index in range(0, len(blocks), MAX_CHILDREN_PER_REQUEST)
        ] or [[]]

        created = self._request(
            "POST",
            "/pages",
            {
                "parent": {"page_id": parent_id},
                "properties": {"title": {"title": [{"text": {"content": title[:2000]}}]}},
                "children": batches[0],
            },
        )
        page_id = str(created.get("id", ""))
        if not page_id:
            raise UpstreamError("Notion created a page but returned no id.", service="notion")

        for batch in batches[1:]:
            self.append_blocks(page_id, batch)

        return DocStatus(
            doc_id=page_id,
            titles=title,
            block_count=len(blocks),
            url=str(created.get("url", "")),
        )

    def delete_blocks(self, block_ids: list[str]) -> int:
        deleted = 0
        for block_id in block_ids:
            self._request("DELETE", f"/blocks/{block_id}", op="block-delete")
            deleted += 1
        return deleted

    # -- filing ------------------------------------------------------------

    @staticmethod
    def _title_of(page: dict[str, Any]) -> str:
        for prop in (page.get("properties") or {}).values():
            if prop.get("type") == "title":
                return "".join(part.get("plain_text", "") for part in prop.get("title", [])).strip()
        return ""

    @staticmethod
    def _parentage(raw: dict[str, Any]) -> tuple[str, str]:
        """Whatever holds this thing, and what kind of holder it is.

        A course page kept inside a callout reports a block parent, not a page
        one. Both are usable (children can be listed from either), so the id
        is taken from whichever key is present instead of demanding a page.
        """
        parent = raw.get("parent") or {}
        kind = str(parent.get("type", ""))
        return str(parent.get(kind, "") or ""), kind

    def get_page(self, doc_id: str) -> DocPage:
        page = self._request("GET", f"/pages/{doc_id}")
        parent_id, parent_kind = self._parentage(page)
        return DocPage(
            doc_id=str(page.get("id", doc_id)),
            title=self._title_of(page),
            parent_id=parent_id,
            parent_kind=parent_kind,
            trashed=bool(page.get("archived") or page.get("in_trash")),
            url=str(page.get("url", "")),
        )

    def list_children(self, doc_id: str) -> list[DocChild]:
        """Sub-pages and embedded tables, in the order they appear."""
        children: list[DocChild] = []
        cursor: str | None = None
        while True:
            path = f"/blocks/{doc_id}/children?page_size=100"
            if cursor:
                path = f"{path}&start_cursor={cursor}"
            payload = self._request("GET", path)
            for raw in payload.get("results", []):
                kind = str(raw.get("type", ""))
                if kind not in ("child_page", "child_database"):
                    continue
                children.append(
                    DocChild(
                        child_id=str(raw.get("id", "")),
                        kind="page" if kind == "child_page" else "table",
                        title=str((raw.get(kind) or {}).get("title", "")).strip(),
                    )
                )
            if not payload.get("has_more"):
                return children
            cursor = payload.get("next_cursor")
            if not cursor:
                return children

    def get_table(self, table_id: str) -> DocPage:
        """The table's own identity; its parent is the page it sits on."""
        table = self._request("GET", f"/databases/{table_id}")
        parent_id, parent_kind = self._parentage(table)
        title = "".join(part.get("plain_text", "") for part in table.get("title", [])).strip()
        return DocPage(
            doc_id=str(table.get("id", table_id)),
            title=title,
            parent_id=parent_id,
            parent_kind=parent_kind,
            trashed=bool(table.get("archived") or table.get("in_trash")),
            url=str(table.get("url", "")),
        )

    def table_rows(self, table_id: str) -> list[TableRow]:
        """Every row, read through the configured property names."""
        rows: list[TableRow] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            payload = self._request("POST", f"/databases/{table_id}/query", body)
            for raw in payload.get("results", []):
                properties = raw.get("properties", {}) or {}

                def read(key: str, properties: dict[str, Any] = properties) -> str:
                    name = self.properties.get(key)
                    if not name or name not in properties:
                        return ""
                    return self._read_property(properties[name])

                rows.append(
                    TableRow(
                        row_id=str(raw.get("id", "")),
                        title=self._title_of(raw),
                        date=read("date"),
                        status=read("status"),
                    )
                )
            if not payload.get("has_more"):
                return rows
            cursor = payload.get("next_cursor")
            if not cursor:
                return rows

    def reset_properties(self, doc_id: str) -> list[str]:
        """Empty every writable property but the title.

        The title is what identifies a row once its contents are gone, so
        clearing it would leave a course of blank lines nobody can file.
        """
        page = self._request("GET", f"/pages/{doc_id}")
        payload: dict[str, Any] = {}
        for name, prop in (page.get("properties") or {}).items():
            kind = str(prop.get("type", ""))
            if kind in _READ_ONLY_PROPERTIES or kind == "title":
                continue
            empty = _EMPTY_PROPERTY.get(kind)
            if empty is None:
                continue
            if kind == "status":
                not_started = self.statuses.get("not_started")
                payload[name] = {"status": {"name": not_started} if not_started else None}
                continue
            payload[name] = {kind: empty()}
        if payload:
            self._request("PATCH", f"/pages/{doc_id}", {"properties": payload})
        return sorted(payload)

    def clear_properties(self, doc_id: str, keys: list[str]) -> list[str]:
        """Empty the configured properties named by ``keys``, and nothing else."""
        if not keys:
            return []
        page = self._request("GET", f"/pages/{doc_id}")
        properties = page.get("properties", {}) or {}

        payload: dict[str, Any] = {}
        cleared: list[str] = []
        for key in keys:
            name = self._property_name(key)
            prop = properties.get(name)
            if not isinstance(prop, dict):
                continue
            kind = str(prop.get("type", ""))
            empty = _EMPTY_PROPERTY.get(kind)
            if kind in _READ_ONLY_PROPERTIES or kind == "title" or empty is None:
                continue
            payload[name] = {kind: empty()}
            cleared.append(key)

        if payload:
            self._request("PATCH", f"/pages/{doc_id}", {"properties": payload})
        return cleared

    def restore(self, doc_id: str) -> bool:
        """Bring a page back from the trash, if that is where it is."""
        page = self._request("GET", f"/pages/{doc_id}")
        if not (page.get("archived") or page.get("in_trash")):
            return True
        restored = self._request("PATCH", f"/pages/{doc_id}", {"archived": False})
        return not (restored.get("archived") or restored.get("in_trash"))

    # -- lifecycle ---------------------------------------------------------

    def health(self) -> None:
        """Confirm the token is valid by identifying the integration."""
        self._request("GET", "/users/me")
