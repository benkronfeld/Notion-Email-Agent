"""The `NotionClient` port and its REST implementation (spec §2.3.6, §2.3.2.A).

**Frozen interface.** This Protocol mirrors §2.3.6 exactly. One refinement: the methods
are `async` and return concrete lists rather than an `Iterable`, because the app runs on
an async event loop and page counts here are small. See the spec amendment in §2.3.6.

`update_page` is V1 (build phase 6): the port's shape is fixed by §2.3.6, and
`NotionRestClient` implements it as one `PATCH` built from only the changed properties.
Nothing in the MVP calls it.

`NotionRestClient` below is the live adapter. It is constructed once at startup and held
on `AppContainer`; **no test may construct it or trigger a request** (CLAUDE.md
constraint 5). Tests implement this Protocol with a fake instead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, Protocol

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from app.config import Settings
from app.domain.types import NotionPage


class NotionClient(Protocol):
    """Read access to the two Notion databases, plus the V1 write method."""

    async def resolve_data_source_id(self, database_id: str) -> str:
        """Resolve a `database_id` to its `data_source_id`.

        API version 2025-09-03 split databases from data sources: `GET /v1/databases/{id}`
        returns a `data_sources` array, and queries go to the data source. Callers cache
        the result, so an implementation may cache too.
        """
        ...

    async def list_changed_pages(
        self, data_source_id: str, since: datetime | None
    ) -> list[NotionPage]:
        """Pages in a data source edited after `since` (the incremental sync query).

        `since=None` means no filter. The caller applies the 5-minute overlap.
        """
        ...

    async def list_all_pages(self, data_source_id: str) -> list[NotionPage]:
        """Every page in a data source, including trashed ones.

        `in_trash` is populated: trashed pages drop out of ordinary query results, so the
        daily full reconcile depends on this flag to detect archiving (FR-9).
        """
        ...

    async def get_page(self, page_id: str) -> NotionPage | None:
        """One page by id, or None if it is gone.

        Also used to resolve a `Course` relation target and, in the MVP's pre-send check,
        to confirm the item is still current before an email goes out.
        """
        ...

    async def update_page(
        self,
        page_id: str,
        status_property: str,
        status_name: str | None,
        done_property: str | None,
        done_value: bool | None,
        due_property: str,
        due_date: date | None,
    ) -> None:
        """V1 — not called by the MVP.

        Writes by option name. `done_property` is None when the item's database has
        `HAS_DONE=false`, in which case the caller must not pass a `done_value`.
        """
        ...


# ── Errors ──────────────────────────────────────────────────────────────────

_NOTION_BASE_URL = "https://api.notion.com"
_MAX_ATTEMPTS = 4
_TIMEOUT_SECONDS = 30.0
_ERROR_BODY_CHARS = 200


class NotionError(RuntimeError):
    """A Notion call failed in a way that retrying the same request cannot fix."""


class NotionTransientError(NotionError):
    """HTTP 429 or 5xx. The only failures `_request` retries."""


class NotionNotFoundError(NotionError):
    """HTTP 404 — the page/database is gone, or this token cannot see it."""


class NotionRestClient(NotionClient):
    """`NotionClient` over the Notion REST API, API version `2025-09-03` (§2.3.2.A).

    The data-sources model this version introduced is the reason for two shapes here:
    `GET /v1/databases/{id}` only resolves a `database_id` to a `data_source_id`, and
    every query goes to `POST /v1/data_sources/{id}/query`. Normalization therefore reads
    `parent.data_source_id`, never `parent.database_id`.

    Retries: exponential backoff with jitter, on HTTP 429 and 5xx only. A 4xx is a bug,
    an expired token, or a bad id — retrying it just delays the failure.
    """

    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> None:
        """Build the adapter. `http_client` is injectable so the owner can supply one."""
        # The token lives only in this header dict; it is never logged (constraint 2).
        self._headers: dict[str, str] = {
            "Authorization": f"Bearer {settings.notion_token}",
            "Notion-Version": settings.notion_version,
            "Content-Type": "application/json",
        }
        self._owns_http = http_client is None
        self._http = (
            http_client
            if http_client is not None
            else httpx.AsyncClient(base_url=_NOTION_BASE_URL, timeout=_TIMEOUT_SECONDS)
        )

    async def aclose(self) -> None:
        """Close the HTTP client, but only one this instance created."""
        if self._owns_http:
            await self._http.aclose()

    # ── NotionClient ────────────────────────────────────────────────────────

    async def resolve_data_source_id(self, database_id: str) -> str:
        """`GET /v1/databases/{id}` → the first `data_sources[].id`.

        Each configured database is expected to have exactly one data source (§2.3.2.A);
        the caller caches the mapping in `system_state`.
        """
        payload = await self._request("GET", f"/v1/databases/{database_id}")
        for source in _as_object_list(payload.get("data_sources")):
            source_id = source.get("id")
            if isinstance(source_id, str) and source_id:
                return source_id
        raise NotionError(f"database {database_id} exposes no data source")

    async def list_changed_pages(
        self, data_source_id: str, since: datetime | None
    ) -> list[NotionPage]:
        """Incremental query filtered on `last_edited_time > since`. `since=None` unfilters.

        The 5-minute overlap is the caller's business, not this method's (§2.3.2.A).
        """
        body: dict[str, Any] = {}
        if since is not None:
            body["filter"] = {
                "timestamp": "last_edited_time",
                "last_edited_time": {"after": _iso_utc(since)},
            }
        return await self._query_all(data_source_id, body)

    async def list_all_pages(self, data_source_id: str) -> list[NotionPage]:
        """Every page in the data source, unfiltered. `in_trash` is carried on each page."""
        return await self._query_all(data_source_id, {})

    async def get_page(self, page_id: str) -> NotionPage | None:
        """One page by id, or None when Notion answers 404."""
        try:
            payload = await self._request("GET", f"/v1/pages/{page_id}")
        except NotionNotFoundError:
            return None
        return _page_from_json(payload, fallback_data_source_id=None)

    async def update_page(
        self,
        page_id: str,
        status_property: str,
        status_name: str | None,
        done_property: str | None,
        done_value: bool | None,
        due_property: str,
        due_date: date | None,
    ) -> None:
        """One `PATCH /v1/pages/{id}` carrying only the properties that changed (§2.3.4).

        **The body is assembled, not decided here.** Which properties changed is
        `NotionWriter`'s decision; a `None` argument means "not part of this write" and is
        left out of the body entirely, so this method can only ever write the three
        properties CLAUDE.md constraint 6 allows. It writes them by **option name**, which is
        why nothing here pre-fetches Notion's option ids.

        `None` cannot express "clear the due date", and this app never clears one, so the
        ambiguity is harmless: an absent `due_date` means "unchanged", not "empty". (A
        deliberate `{"date": None}` would be the way to clear it, and it would need its own
        argument rather than an overloaded `None`.)

        `done_property is None` means the item's database has `HAS_DONE=false`; passing a
        `done_value` alongside it is a caller bug, not something to silently drop, so it
        raises. Nothing here retries beyond `_request`'s own transient policy: a write is
        applied at most once per attempt, and verification is the caller's job.

        Both refusals live in `_update_body`, which is pure — so they are checked before any
        request is built, and a unit test can reach them without constructing this class
        (constraint 5).
        """
        body = _update_body(
            status_property=status_property,
            status_name=status_name,
            done_property=done_property,
            done_value=done_value,
            due_property=due_property,
            due_date=due_date,
        )
        await self._request("PATCH", f"/v1/pages/{page_id}", json_body=body)

    # ── Transport ───────────────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(NotionTransientError),
        stop=stop_after_attempt(_MAX_ATTEMPTS),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        reraise=True,
    )
    async def _request(
        self, method: str, path: str, *, json_body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """One HTTP call, retried only when Notion says "later".

        Raises `NotionTransientError` for 429/5xx (retried by the decorator),
        `NotionNotFoundError` for 404, and `NotionError` for every other 4xx or a body
        that is not a JSON object.
        """
        kwargs: dict[str, Any] = {"headers": self._headers}
        if json_body is not None:
            kwargs["json"] = json_body
        response = await self._http.request(method, path, **kwargs)

        status = response.status_code
        if status == 429 or status >= 500:
            raise NotionTransientError(f"{method} {path} -> HTTP {status}: {_brief(response)}")
        if status == 404:
            raise NotionNotFoundError(f"{method} {path} -> HTTP 404")
        if status >= 400:
            raise NotionError(f"{method} {path} -> HTTP {status}: {_brief(response)}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise NotionError(f"{method} {path} -> body is not JSON") from exc
        if not isinstance(payload, dict):
            raise NotionError(f"{method} {path} -> JSON body is not an object")
        return payload

    async def _query_all(self, data_source_id: str, body: Mapping[str, Any]) -> list[NotionPage]:
        """Paginate a data-source query on `next_cursor` / `has_more`."""
        path = f"/v1/data_sources/{data_source_id}/query"
        pages: list[NotionPage] = []
        cursor: str | None = None
        while True:
            request_body: dict[str, Any] = dict(body)
            if cursor is not None:
                request_body["start_cursor"] = cursor
            payload = await self._request("POST", path, json_body=request_body)
            for raw in _as_object_list(payload.get("results")):
                pages.append(_page_from_json(raw, fallback_data_source_id=data_source_id))
            next_cursor = payload.get("next_cursor")
            if payload.get("has_more") is not True or not isinstance(next_cursor, str):
                return pages
            if not next_cursor:
                return pages
            cursor = next_cursor


# ── Payload building ────────────────────────────────────────────────────────


def _update_body(
    *,
    status_property: str,
    status_name: str | None,
    done_property: str | None,
    done_value: bool | None,
    due_property: str,
    due_date: date | None,
) -> dict[str, Any]:
    """The `PATCH /v1/pages/{id}` body for the properties that are part of this write.

    Pure, and separate from the transport on purpose: the three payload shapes below are the
    write surface (CLAUDE.md constraint 6) and are the one thing here worth pinning in a unit
    test, which cannot construct `NotionRestClient` at all (constraint 5).

    Shapes, verified against Notion's `PATCH /v1/pages/{id}` reference (§2.3.4):

    - status: `{"<StatusProp>": {"status": {"name": "<name>"}}}`
    - checkbox: `{"<DoneProp>": {"checkbox": true|false}}`
    - date: `{"<DueProp>": {"date": {"start": "YYYY-MM-DD"}}}`

    Writing by option **name** is what makes the option-id pre-fetch unnecessary. The
    databases use a `status` property type (not `select`), so the `select` variant §2.3.4
    mentions as a fallback is not produced.

    Two refusals, both raising `ValueError` before any request exists:

    - Nothing to change. An empty `properties` object is accepted by Notion and changes
      nothing, so sending it would be a silent no-op reported as a successful write.
    - A `done_value` with no `done_property`. That combination means the caller thinks the
      database has a `Done` property when `HAS_DONE` says it does not; dropping the value
      quietly would hide the disagreement.
    """
    if status_name is None and done_value is None and due_date is None:
        raise ValueError("update_page called with nothing to change")
    if done_property is None and done_value is not None:
        raise ValueError(
            "done_value was given without a done_property; this database has no `Done` "
            "property (settings.has_done_property is False)"
        )

    properties: dict[str, Any] = {}
    if status_name is not None:
        properties[status_property] = {"status": {"name": status_name}}
    if done_property is not None and done_value is not None:
        properties[done_property] = {"checkbox": done_value}
    if due_date is not None:
        properties[due_property] = {"date": {"start": due_date.isoformat()}}
    return {"properties": properties}


# ── Payload parsing ─────────────────────────────────────────────────────────


def _as_object_list(value: Any) -> list[Mapping[str, Any]]:
    """The JSON objects in `value`, ignoring anything that is not one."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [entry for entry in value if isinstance(entry, Mapping)]


def _brief(response: httpx.Response) -> str:
    """A short, non-secret excerpt of an error body for the exception message."""
    return response.text[:_ERROR_BODY_CHARS]


def _iso_utc(moment: datetime) -> str:
    """An aware datetime as Notion's ISO 8601 form, e.g. `2026-09-28T12:00:00.000Z`."""
    if moment.tzinfo is None:
        raise ValueError("`since` must be timezone-aware; naive datetimes are never used")
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime:
    """Parse Notion's ISO 8601 timestamp into an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        raise NotionError("page payload carries no timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise NotionError(f"unparseable timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise NotionError(f"timestamp {value!r} has no UTC offset")
    return parsed.astimezone(UTC)


def _page_from_json(raw: Mapping[str, Any], *, fallback_data_source_id: str | None) -> NotionPage:
    """Build a `NotionPage` from one raw page object.

    `fallback_data_source_id` covers a page whose `parent` is not a `data_source_id` —
    for a query result the data source is already known, so the caller passes the id it
    queried.
    """
    page_id = raw.get("id")
    if not isinstance(page_id, str) or not page_id:
        raise NotionError("page payload carries no id")

    data_source_id = fallback_data_source_id
    parent = raw.get("parent")
    if isinstance(parent, Mapping) and parent.get("type") == "data_source_id":
        parent_id = parent.get("data_source_id")
        if isinstance(parent_id, str) and parent_id:
            data_source_id = parent_id
    if not data_source_id:
        raise NotionError(f"page {page_id} has no parent.data_source_id")

    properties = raw.get("properties")
    url = raw.get("url")
    return NotionPage(
        page_id=page_id,
        data_source_id=data_source_id,
        url=url if isinstance(url, str) else None,
        properties=properties if isinstance(properties, Mapping) else {},
        last_edited_time=_parse_iso(raw.get("last_edited_time")),
        in_trash=raw.get("in_trash") is True,
    )
