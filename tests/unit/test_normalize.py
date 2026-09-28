"""Unit tests for `app.integrations.notion.normalize` — pure, no I/O, no network.

`NotionRestClient` is never imported here, so no test in this file can construct the
adapter or open a connection (CLAUDE.md constraint 5). Pages are hand-built `NotionPage`
values in the exact JSON shape the API returns.

A concrete zone is needed to assert the 11:59 PM rule. The *application* never hardcodes
one (constraint 3) — the test names its zone here, once, and passes it in like config.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from app.domain.types import NormalizedItem, NotionPage, SourceDb
from app.integrations.notion.normalize import (
    course_page_id_of,
    normalize_page,
    title_of,
    type_mismatch,
)

EASTERN = ZoneInfo("America/New_York")
DUE_TIME = time(23, 59)
EDITED = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


# ── Builders: the exact property shapes from the spec ───────────────────────


def _title_prop(value: str) -> dict[str, Any]:
    return {
        "id": "title",
        "type": "title",
        "title": [{"type": "text", "plain_text": value, "text": {"content": value}}],
    }


def _relation(page_id: str) -> dict[str, Any]:
    return {"id": "rel", "type": "relation", "relation": [{"id": page_id}], "has_more": False}


def _empty_relation() -> dict[str, Any]:
    return {"id": "rel", "type": "relation", "relation": [], "has_more": False}


def _select(name: str) -> dict[str, Any]:
    return {"id": "sel", "type": "select", "select": {"id": "s1", "name": name}}


def _date(start: str | None) -> dict[str, Any]:
    return {"id": "dat", "type": "date", "date": {"start": start, "end": None, "time_zone": None}}


def _status(name: str) -> dict[str, Any]:
    return {"id": "sta", "type": "status", "status": {"id": "st1", "name": name}}


def _checkbox(value: bool) -> dict[str, Any]:
    return {"id": "chk", "type": "checkbox", "checkbox": value}


def _page(
    properties: dict[str, Any],
    *,
    page_id: str = "page-1",
    data_source_id: str = "ds-1",
    url: str | None = "https://www.notion.so/page-1",
    last_edited_time: datetime = EDITED,
    in_trash: bool = False,
) -> NotionPage:
    return NotionPage(
        page_id=page_id,
        data_source_id=data_source_id,
        url=url,
        properties=properties,
        last_edited_time=last_edited_time,
        in_trash=in_trash,
    )


def _item_props(**overrides: Any) -> dict[str, Any]:
    """The canonical Assignments & Readings page, as raw Notion properties."""
    props: dict[str, Any] = {
        "Name": _title_prop("Problem Set 4"),
        "Course": _relation("course-page-9"),
        "Type": _select("Assignment"),
        "Due Date": _date("2026-10-03"),
        "Status": _status("Not started"),
        "Done": _checkbox(False),
    }
    props.update(overrides)
    return props


def _normalize(page: NotionPage, source_db: SourceDb = "assignments_readings") -> NormalizedItem:
    return normalize_page(page, source_db=source_db, tz=EASTERN, default_due_time=DUE_TIME)


# ── Title discovery ─────────────────────────────────────────────────────────


def test_title_is_discovered_by_type_not_by_name() -> None:
    page = _page({"Task": _title_prop("Problem Set 4"), "Course": _relation("course-page-9")})
    assert title_of(page) == "Problem Set 4"


def test_title_discovery_ignores_a_non_title_property_named_name() -> None:
    page = _page(
        {
            "Name": {
                "id": "x",
                "type": "rich_text",
                "rich_text": [{"type": "text", "plain_text": "not the title"}],
            },
            "Title": _title_prop("The Real Title"),
        }
    )
    assert title_of(page) == "The Real Title"


def test_title_is_joined_across_rich_text_segments() -> None:
    page = _page(
        {
            "Name": {
                "id": "title",
                "type": "title",
                "title": [
                    {"type": "text", "plain_text": "Problem ", "text": {"content": "Problem "}},
                    {"type": "text", "plain_text": "Set 4", "text": {"content": "Set 4"}},
                ],
            }
        }
    )
    assert title_of(page) == "Problem Set 4"


def test_title_of_a_course_page_uses_the_same_rule() -> None:
    """Course-name resolution reads a related page's title (§2.3.2.A)."""
    course_page = _page({"Course / Class": _title_prop("Microeconomics")})
    assert title_of(course_page) == "Microeconomics"


def test_title_is_empty_when_no_title_property_exists() -> None:
    assert title_of(_page({"Status": _status("Not started")})) == ""


# ── Course relation ─────────────────────────────────────────────────────────


def test_course_relation_returns_the_single_page_id() -> None:
    page = _page({"Course": _relation("course-page-9")})
    assert course_page_id_of(page, "Course") == "course-page-9"


def test_course_relation_empty_returns_none() -> None:
    assert course_page_id_of(_page({"Course": _empty_relation()}), "Course") is None


def test_course_property_absent_returns_none() -> None:
    assert course_page_id_of(_page({"Name": _title_prop("x")}), "Course") is None


def test_normalize_keeps_course_page_id_and_leaves_course_name_unresolved() -> None:
    item = _normalize(_page(_item_props()))
    assert item.course_page_id == "course-page-9"
    assert item.course is None


def test_normalize_with_empty_course_relation_leaves_both_none() -> None:
    item = _normalize(_page(_item_props(Course=_empty_relation())))
    assert item.course_page_id is None
    assert item.course is None


# ── Due dates ───────────────────────────────────────────────────────────────


def test_date_only_becomes_the_default_due_time_in_the_configured_zone() -> None:
    item = _normalize(_page(_item_props(**{"Due Date": _date("2026-10-03")})))
    assert item.due_date == date(2026, 10, 3)
    assert item.due_at == datetime(2026, 10, 4, 3, 59, tzinfo=UTC)
    assert item.due_at is not None
    assert item.due_at.isoformat() == "2026-10-04T03:59:00+00:00"
    assert item.due_has_time is False


def test_date_with_a_time_and_an_explicit_offset_is_honoured() -> None:
    item = _normalize(_page(_item_props(**{"Due Date": _date("2026-10-03T14:30:00-04:00")})))
    assert item.due_date == date(2026, 10, 3)
    assert item.due_at == datetime(2026, 10, 3, 18, 30, tzinfo=UTC)
    assert item.due_has_time is True


def test_date_with_a_time_and_no_offset_is_read_in_the_configured_zone() -> None:
    item = _normalize(_page(_item_props(**{"Due Date": _date("2026-10-03T14:30:00")})))
    assert item.due_at == datetime(2026, 10, 3, 18, 30, tzinfo=UTC)
    assert item.due_has_time is True


def test_null_date_leaves_all_three_due_fields_empty() -> None:
    item = _normalize(_page(_item_props(**{"Due Date": _date(None)})))
    assert item.due_date is None
    assert item.due_at is None
    assert item.due_has_time is False


def test_absent_date_property_leaves_all_three_due_fields_empty() -> None:
    props = _item_props()
    del props["Due Date"]
    item = _normalize(_page(props))
    assert item.due_date is None
    assert item.due_at is None
    assert item.due_has_time is False


# ── Done / Status ───────────────────────────────────────────────────────────


def test_done_checkbox_true_is_read() -> None:
    item = _normalize(_page(_item_props(Done=_checkbox(True))))
    assert item.done is True
    assert item.is_complete("Completed") is True


def test_done_checkbox_false_is_read() -> None:
    assert _normalize(_page(_item_props(Done=_checkbox(False)))).done is False


def test_done_absent_defaults_to_false() -> None:
    props = _item_props()
    del props["Done"]
    item = _normalize(_page(props))
    assert item.done is False
    assert item.is_complete("Completed") is False


def test_status_property_type_is_read_from_status_not_select() -> None:
    item = _normalize(_page(_item_props(Status=_status("In progress"))))
    assert item.status == "In progress"


def test_unknown_status_passes_through_untouched_and_stays_active() -> None:
    item = _normalize(_page(_item_props(Status=_status("Waiting on advisor"))))
    assert item.status == "Waiting on advisor"
    assert item.is_complete("Completed") is False


def test_completed_status_counts_as_complete_without_done() -> None:
    item = _normalize(_page(_item_props(Status=_status("Completed"), Done=_checkbox(False))))
    assert item.done is False
    assert item.is_complete("Completed") is True


def test_absent_status_property_falls_back_to_the_notion_default() -> None:
    props = _item_props()
    del props["Status"]
    assert _normalize(_page(props)).status == "Not started"


# ── Type is metadata, never the classifier ──────────────────────────────────


def test_database_classifies_the_item_kind() -> None:
    item = _normalize(_page(_item_props()))
    assert item.source_db == "assignments_readings"
    assert item.item_kind == "assignment_reading"


def test_exams_database_classifies_as_exam_project() -> None:
    item = _normalize(_page(_item_props(**{"Type": _select("Exam")})), source_db="exams_projects")
    assert item.item_kind == "exam_project"
    assert item.notion_type == "Exam"


def test_type_mismatch_is_true_when_exam_sits_in_the_assignments_database() -> None:
    item = _normalize(_page(_item_props(**{"Type": _select("Exam")})))
    assert item.item_kind == "assignment_reading"  # the database wins (§1.2)
    assert item.notion_type == "Exam"
    assert type_mismatch(item) is True


def test_type_mismatch_is_false_when_database_and_type_agree() -> None:
    assert type_mismatch(_normalize(_page(_item_props()))) is False


def test_type_mismatch_is_false_when_type_is_absent() -> None:
    props = _item_props()
    del props["Type"]
    item = _normalize(_page(props))
    assert item.notion_type is None
    assert type_mismatch(item) is False


def test_type_mismatch_is_false_for_an_unmapped_type_value() -> None:
    item = _normalize(_page(_item_props(**{"Type": _select("Reading")})))
    assert item.notion_type == "Reading"
    assert type_mismatch(item) is False


# ── Pass-through fields ─────────────────────────────────────────────────────


class TestPassThrough:
    """The fields copied straight off the page, with no interpretation."""

    def test_identity_and_link_fields(self) -> None:
        page = _page(
            _item_props(),
            page_id="page-42",
            data_source_id="ds-abc",
            url="https://www.notion.so/page-42",
        )
        item = _normalize(page)
        assert item.notion_page_id == "page-42"
        assert item.notion_data_source_id == "ds-abc"
        assert item.notion_url == "https://www.notion.so/page-42"
        assert item.notion_last_edited_time == EDITED
        assert item.name == "Problem Set 4"

    def test_timezone_is_the_zone_that_was_passed_in(self) -> None:
        assert _normalize(_page(_item_props())).timezone == "America/New_York"

    def test_in_trash_is_carried_over(self) -> None:
        assert _normalize(_page(_item_props(), in_trash=True)).in_trash is True

    def test_missing_url_becomes_none(self) -> None:
        assert _normalize(_page(_item_props(), url=None)).notion_url is None


def test_renamed_properties_can_be_supplied_from_config() -> None:
    """Property names are configurable (`.env`); passing them keeps a rename working."""
    props = {
        "Title": _title_prop("Lab Report"),
        "Class": _relation("course-page-3"),
        "Kind": _select("Exam"),
        "Deadline": _date("2026-11-30"),
        "State": _status("In progress"),
        "Finished": _checkbox(True),
    }
    item = normalize_page(
        _page(props),
        source_db="assignments_readings",
        tz=EASTERN,
        default_due_time=DUE_TIME,
        prop_course="Class",
        prop_type="Kind",
        prop_due="Deadline",
        prop_status="State",
        prop_done="Finished",
    )
    assert item.name == "Lab Report"
    assert item.course_page_id == "course-page-3"
    assert item.due_date == date(2026, 11, 30)
    assert item.status == "In progress"
    assert item.done is True
    assert type_mismatch(item) is True
