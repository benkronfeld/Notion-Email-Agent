# Project status

**Last updated:** 2026-09-29
**Current phase:** M2 — **V1 complete**. Build phases 1–6 implemented and verified.

## Milestones

Two milestones, defined by the success criteria in spec §1.2 and delivered by the six build phases in Appendix C.

| Milestone | Scope | Success criteria | Status |
|---|---|---|---|
| **M1 — MVP** | Build phases 1–3. Reminders only; no LLM call anywhere in the flow. | §1.2 criteria 1–5 | **Complete** |
| **M2 — V1** | Build phases 4–6. Adds reply-to-edit. | §1.2 criteria 6–10 | **Complete** |

### M1 — MVP success criteria

1. A new item in either database is discovered within about one hourly sync.
2. Correct targets are computed; missed windows are never sent late.
3. Completed and archived items send nothing.
4. Each reminder is sent once, within about 5 minutes of its target.
5. Every event is in the audit log.

### M2 — V1 success criteria

6. A reply maps to exactly one item via stored Gmail thread/message IDs (never by name).
7. Clear status changes, clear date changes, and combined changes are applied and read-back verified.
8. Ambiguous replies produce a question and zero writes.
9. Duplicate inbound messages are processed once.
10. The owner receives an accurate confirmation or failure email for every actionable reply.

## What's been accomplished

**Design: complete.** The spec was finalized 2026-09-27 and amended during V1 where the implementation settled something it had left open (see *Decisions recorded during V1* below).

**Repository setup: complete.** `.gitignore`, `.env.example`, `CLAUDE.md`, `Architecture.md`, `Project_status.md`, `Changelog.md`, and the `/update-docs` command.

**Implementation: complete for both milestones.** **571 tests pass** — 392 unit, 25 contract, 144 integration against a real PostgreSQL 16, and 10 end-to-end. Every green run is reproducible with `docker compose up -d --wait db` and `uv run pytest`.

| # | Build phase | Milestone | Status |
|---|---|---|---|
| 1 | Notion read + local DB | M1 | Complete |
| 2 | Reminder engine | M1 | Complete |
| 3 | Email sending + thread mapping | M1 | Complete |
| 4 | Inbound handling | M2 | Complete |
| 5 | AI interpretation | M2 | Complete |
| 6 | Safe writes | M2 | Complete |

### How the work is verified

- **Unit (392).** The pure domain: the 11:59 PM rule across both DST boundaries, the reminder
  rules, the composer, Notion normalization, config, the `Clock` port, the planner's case
  matrix, the date resolver's full §2.3.4 table, the validator, Gmail parsing, and the write
  pipeline's read-back rules.
- **Contract (25).** The DeepSeek adapter against mocked responses: the request payload
  (`temperature`, generic JSON mode, thinking disabled), every invalid-output path, and the
  one-retry-then-safe-fallback rule. No network.
- **Integration (144).** Against a real PostgreSQL 16: the `FOR UPDATE SKIP LOCKED` claim
  under two concurrent sessions, `apply_plan`'s transitions, the scheduler tick's guards and
  backoff ladder, the sync and its cursor handling, the admin API over the real lifespan, and
  the reply pipeline — dedupe, sender allowlist, thread mapping, the clarification loop, a
  verified write, and a write that does not land.
- **End-to-end (10).** A frozen-clock dry run driving the real app with fake adapters. The
  reminder dry run covers sync, send-once, and never-late; the reply dry run covers a reply
  mapped by stored thread id, applied, read back, and confirmed — plus an ambiguous reply
  producing a question and no write, and a duplicate processed once.

**Not verified:** any live Notion, Gmail, or DeepSeek call — by design (CLAUDE.md
constraint 5).

**Verified in CI.** `.github/workflows/ci.yml` runs the whole suite on every push and pull
request, against a PostgreSQL 16 service container on `ubuntu-latest`, after `uv sync
--frozen`, ruff check, `ruff format --check`, mypy, and `alembic upgrade head`. The merged V1
tree passes it (`525c35a`, 58s). That is worth more than it sounds: it is a *different*
environment from the local Windows run — Linux, a fresh database, a frozen lockfile — so it
independently confirms the suite is not passing by accident of this machine.

## What's next

The code is complete. What remains is **deployment**, and it is the same gate it was before
V1: nothing has ever spoken to the real Notion, Gmail, or DeepSeek.

1. **Google OAuth refresh token lifetime.** Set the consent screen to "In production" —
   tokens for apps left in "Testing" expire after about 7 days, and reminders would stop
   silently.
2. **Sender Gmail address.** Create the dedicated account and set `GMAIL_SENDER_ADDRESS`.
3. **Lehigh mail filtering.** Confirm mail from the sender isn't junked by the school's mail
   system; consider allow-listing it.
4. **DeepSeek API key.** Set `DEEPSEEK_API_KEY` — V1 has a real call site now, and it is the
   only one in the system.

Then a first live pass, in this order: `uv run python -m app.cli gmail-auth`, a manual
`POST /admin/sync`, a manual `POST /admin/scheduler/run`, and finally a real reply to a real
reminder.

## Decisions recorded during V1

Three things the spec left open were settled while implementing, and the spec was amended in
the same change (CLAUDE.md constraint 7):

- **A reverted due date no longer loses its reminders.** §2.3.2.B step 2 revived a `skipped`
  row but not a `superseded` one, so a due date moved away and back silently received no
  reminder. A `superseded` row whose snapshot matches the restored date now revives under the
  same guards as a skip.
- **Stale-claim recovery writes the thread-mapping rows.** It previously marked a reminder
  `sent` without creating `email_threads`/`outbound_messages`, so a reply to a recovered
  reminder would have been `inbound_unmapped`.
- **The service now has a production entrypoint.** `uvicorn app.main:app` could never have
  worked: `main.py` exposes `create_app` and nothing built a real `AppContainer`, so no test
  ever noticed. `src/app/bootstrap.py` holds `build_container`, and `src/app/asgi.py` is the
  entrypoint. It lives outside `main.py` so the test suite never reads `.env`.

`inbound_stuck` was added to the closed `audit_log.event_type` vocabulary (§2.3.5) for the
same reason: §2.3.2.E required stuck rows be flagged but did not name an event.

## How to update this file

Update at every milestone boundary, at the end of each build phase, and whenever scope changes — then add a matching entry to [`Changelog.md`](Changelog.md). Keep the phase table honest: "in progress" should name what is actually working, not what is being attempted.
