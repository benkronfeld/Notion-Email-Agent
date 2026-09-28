"""Page normalization: a `NotionPage` reduced to a `NormalizedItem` (spec §2.3.2.A, §1.2).

Pure. No I/O, no config reads, and nothing here can raise a network error: the timezone
and the default due time arrive as arguments, so the whole module is unit-testable with
hand-built `NotionPage` values.

Two rules from §1.2 drive everything:

- The **database wins**. `item_kind` comes from `source_db`, never from the `Type` select;
  `Type` is stored as metadata and only flagged when it disagrees (`type_mismatch`).
- Completion is **derived, not read from one field**: this module reports `status` and
  `done` separately and never collapses them. `NormalizedItem.is_complete()` is the only
  place the OR happens.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo

from app.domain.due_time import compute_due_at_from_notion_date
from app.domain.types import (
    NOTION_TYPE_TO_KIND,
    SOURCE_DB_TO_KIND,
    NormalizedItem,
    NotionPage,
    SourceDb,
)

# What a page is reported as when its `Status` property is missing or empty. Both databases
# default new rows to this value (§1.2) and it is an active status, so a page we cannot
# read is never mistaken for a completed one (FR-3). Deliberately *not* a closed list:
# an unrecognized status passes through untouched and is treated as active downstream.
DEFAULT_STATUS = "Not started"

# These default property names mirror the `Settings` defaults (§2.2). Callers that have
# settings should pass `settings.notion_prop_*` so a renamed property keeps working.
_DEFAULT_PROP_COURSE = "Course"
_DEFAULT_PROP_TYPE = "Type"
_DEFAULT_PROP_DUE = "Due Date"
_DEFAULT_PROP_STATUS = "Status"
_DEFAULT_PROP_DONE = "Done"


# ── Property lookup ─────────────────────────────────────────────────────────


def _entry(page: NotionPage, prop_name: str) -> Mapping[str, Any] | None:
    """The named property's entry, or None when it is absent or not an object."""
    if not prop_name:
        return None
    value = page.properties.get(prop_name)
    return value if isinstance(value, Mapping) else None


def _rich_text(parts: Any) -> str:
    """Concatenate a rich-text array's `plain_text` into one string."""
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        text = part.get("plain_text")
        if text is None:
            inner = part.get("text")
            if isinstance(inner, Mapping):
                text = inner.get("content")
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


def title_of(page: NotionPage) -> str:
    """The page's title text, discovered by property TYPE == `title`.

    The title property's display name is not fixed (it is not necessarily "Name"), so it
    is found by type and needs no configuration. Works unchanged on a **Course page**,
    which is how course-name resolution reads a related page's title (§2.3.2.A).
    """
    for value in page.properties.values():
        if isinstance(value, Mapping) and value.get("type") == "title":
            return _rich_text(value.get("title"))
    return ""


def course_page_id_of(page: NotionPage, prop_name: str) -> str | None:
    """The single related page id from the `Course` relation, or None when empty.

    The relation holds a page id, not a name — the name comes from the `courses` cache in
    the sync service (§1.2). A relation is capped at one page in these databases, so the
    first id is the only id.
    """
    entry = _entry(page, prop_name)
    if entry is None or entry.get("type") != "relation":
        return None
    relation = entry.get("relation")
    if not isinstance(relation, list):
        return None
    for target in relation:
        if not isinstance(target, Mapping):
            continue
        page_id = target.get("id")
        if isinstance(page_id, str) and page_id:
            return page_id
    return None


def type_mismatch(item: NormalizedItem) -> bool:
    """True when the `Type` select disagrees with the database-derived kind (§1.2).

    A missing or unmapped `Type` value cannot disagree with anything, so it is False —
    the database still wins and no `type_mismatch` event is written for it.
    """
    if item.notion_type is None:
        return False
    mapped = NOTION_TYPE_TO_KIND.get(item.notion_type)
    if mapped is None:
        return False
    return mapped != item.item_kind


# ── Value readers ───────────────────────────────────────────────────────────


def _select_name(page: NotionPage, prop_name: str) -> str | None:
    entry = _entry(page, prop_name)
    if entry is None:
        return None
    value = entry.get("select")
    if not isinstance(value, Mapping):
        return None
    name = value.get("name")
    return name if isinstance(name, str) else None


def _status_name(page: NotionPage, prop_name: str) -> str:
    """The `Status` value. `Status` is a **`status`** property type, not a `select`.

    The value is passed through untouched — never validated against a fixed list — so a
    status added in Notion later is still read correctly (FR-3).
    """
    entry = _entry(page, prop_name)
    if entry is None:
        return DEFAULT_STATUS
    value = entry.get("status")
    if not isinstance(value, Mapping):
        return DEFAULT_STATUS
    name = value.get("name")
    if isinstance(name, str) and name:
        return name
    return DEFAULT_STATUS


def _checkbox_value(page: NotionPage, prop_name: str) -> bool:
    """The `Done` checkbox, False when the property is absent (databases with HAS_DONE=false)."""
    entry = _entry(page, prop_name)
    if entry is None:
        return False
    value = entry.get("checkbox")
    return value if isinstance(value, bool) else False


def _date_start(page: NotionPage, prop_name: str) -> str | None:
    """The date property's `start`, or None when the property or its date is empty."""
    entry = _entry(page, prop_name)
    if entry is None:
        return None
    value = entry.get("date")
    if not isinstance(value, Mapping):
        return None
    start = value.get("start")
    if isinstance(start, str) and start.strip():
        return start
    return None


# ── The normalizer ──────────────────────────────────────────────────────────


def normalize_page(
    page: NotionPage,
    *,
    source_db: SourceDb,
    tz: ZoneInfo,
    default_due_time: time,
    prop_course: str = _DEFAULT_PROP_COURSE,
    prop_type: str = _DEFAULT_PROP_TYPE,
    prop_due: str = _DEFAULT_PROP_DUE,
    prop_status: str = _DEFAULT_PROP_STATUS,
    prop_done: str = _DEFAULT_PROP_DONE,
) -> NormalizedItem:
    """Reduce one Notion page to the fields the `items` table stores (§2.3.5).

    `course` is left None: resolving the relation's page id to a course name needs the
    `courses` cache, which lives in the sync service (§2.3.2.A).

    Formula properties ("X days remaining" columns) are read-only via the API and are
    never touched here.
    """
    due_date = None
    due_at = None
    due_has_time = False
    start = _date_start(page, prop_due)
    if start is not None:
        due_date, due_at, due_has_time = compute_due_at_from_notion_date(
            start, tz, default_due_time
        )

    return NormalizedItem(
        notion_page_id=page.page_id,
        notion_data_source_id=page.data_source_id,
        source_db=source_db,
        # The database is the primary classifier; `Type` is metadata only (§1.2).
        item_kind=SOURCE_DB_TO_KIND[source_db],
        notion_type=_select_name(page, prop_type),
        name=title_of(page),
        course_page_id=course_page_id_of(page, prop_course),
        course=None,
        status=_status_name(page, prop_status),
        done=_checkbox_value(page, prop_done),
        due_date=due_date,
        due_at=due_at,
        due_has_time=due_has_time,
        timezone=str(tz),
        notion_url=page.url,
        notion_last_edited_time=page.last_edited_time,
        in_trash=page.in_trash,
    )
