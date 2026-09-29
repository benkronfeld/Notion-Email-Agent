"""Integration tests for the ops HTTP surface (spec §2.3.6).

These need the docker-compose Postgres, exactly like the rest of `tests/integration/`; the
directory's autouse fixture skips them when no database is reachable, so the unit suite
stays green on a machine without Docker.

The app is booted over the fake Notion/Gmail adapters the harness provides — no test here
contacts a live service (CLAUDE.md constraint 5), and the only database touched is the
localhost test database (constraint 1).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import FrozenClock
from app.db.models import Item, Reminder
from fixtures.fakes import FakeNotionClient

pytestmark = pytest.mark.integration


# (method, path, request kwargs). Every one of these requires the admin token. `Any` values
# so `**kwargs` spreads into `httpx.AsyncClient.request` without a type error.
AUTHENTICATED_ROUTES: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", "/readyz", {}),
    ("GET", "/admin/status", {}),
    ("GET", "/admin/items", {}),
    ("GET", "/admin/reminders", {}),
    ("GET", "/admin/audit", {}),
    ("POST", "/admin/sync", {"json": {"full": False}}),
    ("POST", "/admin/scheduler/run", {}),
    ("POST", f"/admin/reminders/{uuid4()}/cancel", {}),
]


async def _call(
    client: httpx.AsyncClient, method: str, path: str, kwargs: dict[str, Any]
) -> httpx.Response:
    return await client.request(method, path, **kwargs)


# ── Liveness ────────────────────────────────────────────────────────────────


async def test_healthz_needs_no_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ── Authentication ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(("method", "path", "kwargs"), AUTHENTICATED_ROUTES)
async def test_route_without_token_is_401(
    client: httpx.AsyncClient, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    response = await _call(client, method, path, kwargs)
    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path", "kwargs"), AUTHENTICATED_ROUTES)
async def test_route_with_wrong_token_is_401(
    client: httpx.AsyncClient, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    response = await _call(
        client, method, path, {**kwargs, "headers": {"Authorization": "Bearer wrong-token"}}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path", "kwargs"), AUTHENTICATED_ROUTES)
async def test_route_with_correct_token_is_not_401(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    # `cancel` on a made-up id is a 404; the point here is that auth let the request reach
    # the handler at all.
    response = await _call(client, method, path, {**kwargs, "headers": admin_headers})
    assert response.status_code != 401


# ── Readiness and status ────────────────────────────────────────────────────


async def test_readyz_reports_db_and_scheduler(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    response = await client.get("/readyz", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["database_reachable"] is True
    assert body["scheduler_running"] is True


async def test_status_reports_reminder_counts(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    session: AsyncSession,
    frozen_clock: FrozenClock,
) -> None:
    item = await _add_item(session, frozen_clock)
    await _add_reminder(
        session, item, frozen_clock, reminder_type="assignment_48h", status="pending"
    )
    await _add_reminder(
        session, item, frozen_clock, reminder_type="assignment_24h", status="failed"
    )
    await session.commit()

    response = await client.get("/admin/status", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["pending_reminders"] == 1
    assert body["failed_reminders"] == 1
    assert body["scheduler_running"] is True


# ── Triggers ────────────────────────────────────────────────────────────────


async def test_sync_trigger_runs_incremental_sync(
    client: httpx.AsyncClient, admin_headers: dict[str, str], notion: FakeNotionClient
) -> None:
    response = await client.post("/admin/sync", json={"full": False}, headers=admin_headers)
    assert response.status_code == 200
    assert response.json() == {"job": "notion_sync", "ran": True}

    # One incremental query per configured data source — and crucially none through
    # `list_all_pages`, which is the full-reconcile path.
    assert len(notion.list_changed_calls) == len(notion.data_source_ids)
    assert notion.list_all_calls == []


async def test_scheduler_run_trigger(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    response = await client.post("/admin/scheduler/run", headers=admin_headers)
    assert response.status_code == 200
    assert response.json() == {"job": "reminder_scheduler", "ran": True}


# ── Cancel ──────────────────────────────────────────────────────────────────


async def test_cancel_marks_pending_reminder_skipped(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    session: AsyncSession,
    frozen_clock: FrozenClock,
) -> None:
    item = await _add_item(session, frozen_clock)
    reminder = await _add_reminder(session, item, frozen_clock, reminder_type="assignment_48h")
    await session.commit()

    response = await client.post(f"/admin/reminders/{reminder.id}/cancel", headers=admin_headers)
    assert response.status_code == 200
    assert response.json() == {"id": str(reminder.id), "status": "skipped"}

    stored = await session.get(Reminder, reminder.id)
    assert stored is not None
    await session.refresh(stored)
    assert stored.status == "skipped"


async def test_cancel_unknown_reminder_is_404(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    response = await client.post(f"/admin/reminders/{uuid4()}/cancel", headers=admin_headers)
    assert response.status_code == 404


# ── The V1 endpoint (build phase 4) ─────────────────────────────────────────


async def test_gmail_poll_requires_the_admin_token(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Inbound polling exists now, and it is behind the admin token like every other route.

    This was `test_gmail_poll_is_404` while inbound handling was unbuilt. It is worth
    keeping in its new form rather than deleting: the poll can reach Notion through the
    reply pipeline, so "is this route authenticated?" is a question that should keep being
    asked by a test rather than by memory.
    """
    assert (await client.post("/admin/gmail/poll")).status_code == 401
    assert (await client.post("/admin/gmail/poll", headers=admin_headers)).status_code == 200


async def test_gmail_poll_runs_the_poller_job(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    """It returns the `gmail_poller` job result, and reports whether it ran."""
    response = await client.post("/admin/gmail/poll", headers=admin_headers)
    assert response.status_code == 200
    assert response.json() == {"job": "gmail_poller", "ran": True}


# ── Fixture-level row builders ──────────────────────────────────────────────


async def _add_item(session: AsyncSession, clock: FrozenClock) -> Item:
    item = Item(
        notion_page_id=f"page-{uuid4()}",
        notion_data_source_id="ds-1",
        source_db="assignments_readings",
        item_kind="assignment_reading",
        notion_type="Assignment",
        name="Problem Set 4",
        course="CSE 101",
        status="Not started",
        done=False,
        due_date=None,
        due_at=clock.now() + timedelta(days=3),
        due_has_time=False,
        timezone=clock.tz.key,
        notion_url=None,
        is_active=True,
    )
    session.add(item)
    await session.flush()
    await session.refresh(item)
    return item


async def _add_reminder(
    session: AsyncSession,
    item: Item,
    clock: FrozenClock,
    *,
    reminder_type: str,
    status: str = "pending",
) -> Reminder:
    due_at_snapshot: datetime = item.due_at or clock.now() + timedelta(days=3)
    reminder = Reminder(
        item_id=item.id,
        reminder_type=reminder_type,
        due_at_snapshot=due_at_snapshot,
        target_at=due_at_snapshot - timedelta(hours=48),
        status=status,
        idempotency_key=f"{item.notion_page_id}:{reminder_type}:{due_at_snapshot.isoformat()}",
        ref_token=uuid4().hex[:12],
    )
    session.add(reminder)
    await session.flush()
    await session.refresh(reminder)
    return reminder


async def test_created_reminder_is_queryable(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    session: AsyncSession,
    frozen_clock: FrozenClock,
) -> None:
    """Sanity check: the reminder list endpoint reflects rows written by a test."""
    item = await _add_item(session, frozen_clock)
    reminder = await _add_reminder(session, item, frozen_clock, reminder_type="assignment_48h")
    await session.commit()

    response = await client.get("/admin/reminders?status=pending", headers=admin_headers)
    assert response.status_code == 200
    ids = {UUID(row["id"]) for row in response.json()}
    assert reminder.id in ids


async def test_audit_endpoint_lists_a_written_event(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    session: AsyncSession,
) -> None:
    from app.db.repositories import audit as audit_repo

    await audit_repo.append(session, "notion_sync", payload={"source": "test"}, result="ok")
    await session.commit()

    response = await client.get("/admin/audit?event_type=notion_sync", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["event_type"] == "notion_sync"


async def test_items_endpoint_lists_an_active_item(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    session: AsyncSession,
    frozen_clock: FrozenClock,
) -> None:
    item = await _add_item(session, frozen_clock)
    await session.commit()

    response = await client.get("/admin/items", headers=admin_headers)
    assert response.status_code == 200
    names = {row["name"] for row in response.json()}
    assert item.name in names


async def test_reminder_rows_round_trip_through_the_models(
    session: AsyncSession,
    frozen_clock: FrozenClock,
) -> None:
    """Guard the direct model round-trip the cancel test relies on."""
    item = await _add_item(session, frozen_clock)
    reminder = await _add_reminder(session, item, frozen_clock, reminder_type="assignment_48h")
    await session.commit()

    found = (await session.execute(select(Reminder).where(Reminder.id == reminder.id))).scalar_one()
    assert found.status == "pending"
    assert found.item_id == item.id
