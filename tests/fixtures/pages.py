"""Notion page builders — the exact JSON shape the normalizer consumes (§2.3.2.A).

Every builder returns an `app.domain.types.NotionPage`, the adapter-boundary type. The
`properties` mapping is written out in full and **by hand** rather than derived from a
Notion SDK, so a change in the normalizer's expectations shows up as a test failure here
instead of as a silent mismatch against a mock that mocks too much.

Two deliberate details:

- The title property key is `title_prop` (default `"Name"`), never hardcoded: the app
  discovers the title property by its Notion *type* (`"type": "title"`), and
  `NOTION_PROP_TITLE` may be blank. A test renames it (e.g. `title_prop="Task"`) to prove
  discovery is by type, not by name (see tests/unit/test_notion_normalize.py style).
- `Course` is a Relation, so it carries a page ID, not a name, and `relation` is `[]`
  when no course is linked (§2.3.2.A).

Nothing here calls a clock: `last_edited_time` defaults to a fixed aware instant, so
`list_changed_pages(..., since=...)` filters deterministically (CLAUDE.md constraint 4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain.types import NotionPage

# A fixed, aware instant. Never `datetime.now()`: the incremental-sync window is computed
# from this value, so it must be identical on every run.
DEFAULT_LAST_EDITED_TIME = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

# The data source the default `make_notion_page` lives in. Matches the default
# `database_id -> data_source_id` mapping in `tests/fixtures/fakes.py`
# (`"db-assignments" -> "ds-1"`), so a page built here resolves through a container built
# there without any extra wiring.
DEFAULT_DATA_SOURCE_ID = "ds-1"

# Fixed property IDs. Notion sends them; nothing reads them, so any stable string works.
_TITLE_PROP_ID = "title"
_COURSE_PROP_ID = "abc"
_TYPE_PROP_ID = "def"
_DUE_PROP_ID = "ghi"
_STATUS_PROP_ID = "jkl"
_DONE_PROP_ID = "mno"


def _title_value(name: str) -> dict[str, Any]:
    """A `title` property holding one plain-text run."""
    return {
        "id": _TITLE_PROP_ID,
        "type": "title",
        "title": [{"type": "text", "plain_text": name, "text": {"content": name}}],
    }


def make_notion_page(
    *,
    page_id: str = "page-1",
    data_source_id: str = DEFAULT_DATA_SOURCE_ID,
    name: str = "Problem Set 4",
    course_page_id: str = "course-1",
    type_name: str = "Assignment",
    due_start: str = "2026-10-03",
    status: str = "Not started",
    done: bool = False,
    url: str | None = None,
    last_edited_time: datetime = DEFAULT_LAST_EDITED_TIME,
    in_trash: bool = False,
    title_prop: str = "Name",
    include_done: bool = True,
) -> NotionPage:
    """One page in the shape `GET /v1/pages/{id}` and the data-source query return.

    `include_done=False` models a database whose `HAS_DONE` flag is false (the adapter must
    then never write `Done`). `course_page_id=""` models an unlinked Course relation.
    """
    properties: dict[str, Any] = {
        title_prop: _title_value(name),
        "Course": {
            "id": _COURSE_PROP_ID,
            "type": "relation",
            "relation": [{"id": course_page_id}] if course_page_id else [],
            "has_more": False,
        },
        "Type": {
            "id": _TYPE_PROP_ID,
            "type": "select",
            "select": {"id": "x", "name": type_name},
        },
        "Due Date": {
            "id": _DUE_PROP_ID,
            "type": "date",
            "date": {"start": due_start, "end": None, "time_zone": None},
        },
        "Status": {
            "id": _STATUS_PROP_ID,
            "type": "status",
            "status": {"id": "y", "name": status},
        },
    }
    if include_done:
        properties["Done"] = {"id": _DONE_PROP_ID, "type": "checkbox", "checkbox": done}

    return NotionPage(
        page_id=page_id,
        data_source_id=data_source_id,
        url=url,
        properties=properties,
        last_edited_time=last_edited_time,
        in_trash=in_trash,
    )


def make_assignment(**overrides: Any) -> NotionPage:
    """An `assignment_reading` page (`Type = Assignment`). Pass any builder kwarg to override."""
    return make_notion_page(type_name="Assignment", **overrides)


def make_exam(**overrides: Any) -> NotionPage:
    """An `exam_project` page (`Type = Exam`). Override `name`/`due_start` as needed."""
    return make_notion_page(type_name="Exam", **overrides)


def make_course_page(
    *,
    page_id: str,
    title: str,
    data_source_id: str = "ds-courses",
    last_edited_time: datetime = DEFAULT_LAST_EDITED_TIME,
    in_trash: bool = False,
) -> NotionPage:
    """A "Course / Class" page — what a `Course` relation points at.

    Only the title property matters: the app reads it to resolve the relation target into a
    display name and caches it in `courses` (§2.3.2.A). The app never writes to it.
    """
    return NotionPage(
        page_id=page_id,
        data_source_id=data_source_id,
        url=None,
        properties={"Name": _title_value(title)},
        last_edited_time=last_edited_time,
        in_trash=in_trash,
    )
