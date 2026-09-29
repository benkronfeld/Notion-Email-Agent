# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status: complete (MVP + V1)

Both milestones are implemented and verified: **571 tests pass** — 392 unit, 25 contract, 144 integration against a real PostgreSQL 16, and 10 end-to-end. Build phases 1–6 are done, so the reply-to-edit flow works end to end and the LLM appears in exactly one place: `DeepSeekInterpreter`, turning reply text into a structured intent.

Nothing has ever spoken to a live Notion, Gmail, or DeepSeek — by design (constraint 5), and that is now the only thing between this and a working deployment. See `Project_status.md` for the four go-live items.

`.env` holds real secrets and is gitignored; `.env.example` is the tracked template. Every configurable value is documented there.

`project_spec final.md` is the authoritative design document. Read the relevant section before proposing changes, and treat it as the source of truth: if code and spec disagree, the spec wins unless the user says otherwise. Section numbers in this file refer to it. Keep it updated when a decision changes, rather than letting it drift from the implementation.

## Documentation

All four are plain files in the repo root.

- [project_spec final.md](project_spec%20final.md) — the authoritative design document covering product requirements, technical design, the database schema, and the decision log; read the relevant section before changing behavior.
- [Architecture.md](Architecture.md) — system design: overview, both data flows, component architecture, and the invariants that span components.
- [Project_status.md](Project_status.md) — the project milestones, what's been accomplished against them, and what's next.
- [Changelog.md](Changelog.md) — dated record of changes to code, schema, config, and docs.

Update these after any major milestone or major addition to the project, then add a matching `Changelog.md` entry. The `/update-docs` command (`.claude/commands/update-docs.md`) does this pass for you.

## Project goals

A single-user backend service that reads school deadlines from two Notion databases, emails reminders at fixed times before each deadline, and (in V1) lets the owner reply to a reminder to change that item's status and/or due date. Full jobs-to-be-done and non-goals are in §1.1; the goal here is small, reliable, and auditable — it touches real deadlines, so being conservative beats being clever.

### Milestones

**MVP** (build phases 1–3, reminders only, no LLM): §1.2 criteria 1–5 — discovery within one
hourly sync, correct targets with no late sends, nothing for completed or archived items,
each reminder sent once within about five minutes, every event audited. **Complete.**

**V1** (build phases 4–6, reply-to-edit): §1.2 criteria 6–10 — a reply maps to exactly one
item by stored Gmail IDs; clear and combined changes are applied and read-back verified;
ambiguous replies produce a question and zero writes; duplicates are processed once; every
actionable reply gets an accurate confirmation or failure email. **Complete.**

The two are sequenced deliberately and the rule still applies to future work: **no LLM call
belongs in the reminder flow.** The model's only job is turning reply text into a structured
intent — it never schedules, decides eligibility, deduplicates, identifies items, or computes
dates. Keep new decision logic in `domain/`, where it is pure and unit-testable.

## Commands

Python 3.12 is pinned in `.python-version`; `uv` fetches it. The app is not an installed
package, so `pytest` and `mypy` put `src/` on the path themselves — for a one-off script,
set `PYTHONPATH=src` (`$env:PYTHONPATH="src"` on PowerShell).

```bash
uv sync                       # install dependencies
uv run pytest -m "not integration"   # unit suite only; green with no database
uv run pytest                 # everything; integration + e2e skip when no Postgres
uv run ruff check --fix       # lint (format: uv run ruff format)
uv run mypy                   # strict
```

**Running part of the suite.** The full run takes about a minute, so narrow it while working:

```bash
uv run pytest tests/unit/test_planner.py -q                  # one file
uv run pytest "tests/unit/test_planner.py::TestRevertedDueDate::test_a_reverted_due_date_revives_the_superseded_rows"
uv run pytest -k "reverted_due_date" -q                      # by keyword
```

**Database.** Integration and end-to-end tests need Postgres. Docker Desktop must be
running.

```bash
docker compose up -d --wait db        # local PostgreSQL 16 on 127.0.0.1:5432
uv run alembic -x url=$DATABASE_URL upgrade head   # apply migrations
```

The test database is created and migrated automatically by the integration harness. Use
`TEST_DATABASE_URL` to point a run at its own database — useful when two runs must not
truncate each other's rows. Use `127.0.0.1`, **not** `localhost`: `localhost` resolves to
`::1` first on Windows and Docker publishes the container on IPv4 loopback only.

Migrations are **hand-written, and `--autogenerate` is not a safe default here**: it cannot
express the two partial indexes (`items_active_due_idx`, `reminders_due_idx`) and it drops
CHECK-constraint names, so it would silently produce a schema that is not the one §2.3.5
specifies. `migrations/versions/0001_initial_schema.py` is the model to copy: write the
`op.create_table` yourself, and keep constraint and index names identical to
`app.db.models` so a later `--autogenerate` still renders an empty diff. The URL is resolved
from `-x url=...`, then `alembic.ini`, then `DATABASE_URL`.

**Running the service.**

```bash
uv run uvicorn app.asgi:app --reload      # admin API; scheduler starts in the lifespan
uv run python -m app.cli gmail-auth       # one-time Google OAuth; prints a refresh token
```

Everything except `/healthz` needs `Authorization: Bearer $ADMIN_API_TOKEN`.

Tests are split by kind — `tests/unit`, `tests/contract`, `tests/integration`, `tests/e2e`;
see **Layout** under Architecture. `.github/workflows/ci.yml` runs on push and pull request
(it is verified, not aspirational): `uv sync --frozen`, ruff check, `ruff format --check`,
mypy, `alembic upgrade head`, then `pytest`, against a PostgreSQL 16 service container on
`ubuntu-latest`. Check `gh run list` before claiming a green build.

If a run and another test run must not truncate each other's rows, point one at its own
database with `TEST_DATABASE_URL`. The integration harness creates and migrates whatever
that variable names. The same applies to a stale test process: it can hold an
`idle in transaction` lock on the shared test database and block every later run's
`TRUNCATE`, which shows up as a suite that hangs rather than fails.

**Writing tests.** Four conventions will trip you up otherwise:

- `addopts` includes `--strict-markers --strict-config`. Only `integration` and `e2e` are
  registered, so inventing `@pytest.mark.unit` fails the run. A module that needs a database
  declares `pytestmark = pytest.mark.integration`; an e2e module uses
  `[pytest.mark.integration, pytest.mark.e2e]`.
- `asyncio_mode = "auto"`, so `async def test_...` needs no decorator or `@pytest.mark.asyncio`.
- **Database-backed fixtures are not plugins.** `pytest_plugins` is only honoured in a
  rootdir conftest, and rootdir is `tests/`, so `tests/integration/conftest.py` and
  `tests/e2e/conftest.py` each do `from fixtures.harness import ...  # noqa: F401` by name.
  A new database-backed directory needs the same import list.
- There is no `__init__.py` anywhere under `tests/` — mypy is configured with
  `explicit_package_bases` so that two `conftest.py` files are not the same module. Adding one
  breaks that. Unit tests need no conftest and must stay green with no database reachable.

## Architecture

Two flows share one database, and the boundary between them is the whole design:

1. **Reminder flow — no LLM anywhere.** Notion → sync → `items` → planner → `reminders` → scheduler claims due rows → Gmail send → audit.
2. **Reply flow — LLM in exactly one step.** Gmail poll → dedupe + sender check → thread → `item_id` → DeepSeek → **validated** intent → resolver/validator → (clarify | write → read-back → local update + reconcile → confirm) → audit.

The LLM only ever turns reply text into a structured intent. It never schedules, decides eligibility, dedupes, or identifies items. When it's unsure, the system does nothing and asks.

**Layering.** `src/app/domain/` is pure, no I/O — `due_time`, `reminder_rules`, `planner`, `date_resolver`, `intents`, `validator`. This is the unit-tested core; put decision logic here, not in services. Everything outside it talks to the world through ports: `NotionClient`, `MailClient`, `IntentInterpreter`, `Clock` (§2.3.6). Services orchestrate; adapters implement ports.

**`AppContainer` is the seam the whole testing strategy rests on** (`src/app/container.py`). It is a frozen dataclass holding `settings`, `clock`, `session_factory`, and the three adapter ports. `create_app(settings, container)` takes one rather than building it, which is why the suite can boot the *real* application — real FastAPI app, real scheduler, real Postgres — over fake external systems. `bootstrap.build_container(settings)` is the production counterpart. A service takes the container in its constructor and reaches everything through it; nothing constructs an adapter directly. When a new port is needed, add it here rather than importing an adapter into a service, or tests stop being able to swap it.

**Layout** (§2.3.3):

```
notion-email-agent/              # this repo's root directory
├── pyproject.toml
├── uv.lock
├── Dockerfile
├── .env.example
├── alembic.ini
├── migrations/                     # Alembic versions
├── src/app/
│   ├── main.py                     # FastAPI app, lifespan starts scheduler
│   ├── asgi.py                     # uvicorn entrypoint (app.asgi:app); the only module
│   │                               # that reads the environment at import time
│   ├── bootstrap.py                # build_container(settings): the real adapter set
│   ├── config.py                   # Pydantic Settings
│   ├── clock.py                    # Clock port + SystemClock
│   ├── container.py                # AppContainer: the dependency-injection seam
│   ├── logging.py
│   ├── db/
│   │   ├── session.py
│   │   ├── models.py               # SQLAlchemy models
│   │   └── repositories/           # items, reminders, threads, inbound, audit, state
│   ├── domain/                     # pure, no I/O
│   │   ├── due_time.py             # date-only -> 11:59 PM tz -> UTC
│   │   ├── reminder_rules.py       # kind -> [(type, offset)]
│   │   ├── planner.py              # reconcile_reminders decision logic
│   │   ├── date_resolver.py        # "Friday" -> date, or Ambiguous
│   │   ├── intents.py              # Pydantic intent schema + enums
│   │   └── validator.py            # intent + item -> command | clarification
│   ├── integrations/
│   │   ├── notion/
│   │   │   ├── client.py           # NotionClient port + REST implementation
│   │   │   ├── normalize.py        # page JSON -> NormalizedItem
│   │   │   └── writer.py           # PATCH + read-back verification
│   │   ├── gmail/
│   │   │   ├── client.py           # MailClient port + Gmail implementation
│   │   │   ├── compose.py          # reminder/clarification/confirmation/failure templates
│   │   │   ├── parse.py            # headers, From, reply-text extraction
│   │   │   └── poller.py           # history.list logic
│   │   └── llm/
│   │       ├── interpreter.py      # IntentInterpreter port
│   │       ├── deepseek.py         # implementation
│   │       └── prompts.py
│   ├── services/
│   │   ├── sync_service.py
│   │   ├── reminder_service.py     # scheduler tick, claim, send, recovery
│   │   ├── inbound_service.py      # reply pipeline
│   │   ├── alert_service.py
│   │   └── audit.py
│   ├── jobs.py                     # APScheduler wiring + advisory lock
│   ├── api/
│   │   ├── deps.py                 # admin token auth
│   │   └── routes.py
│   └── cli.py                      # gmail-auth, backfill, one-off tools
└── tests/
    ├── unit/                       # the pure domain, the composer, parsing, the writer
    ├── contract/                   # interpreter with recorded/mocked LLM outputs
    ├── integration/                # real Postgres + fake Notion/Gmail adapters
    ├── e2e/                        # frozen-clock dry runs: the real app, fake ports
    └── fixtures/                   # fake ports + the shared harness (harness.py)
```

Every path above now exists. The tree describes the code as built, not an intended shape.

### `reconcile_reminders(item, now)` is the crux

The idempotent function in §2.3.2.B decides all reminder state. It's called on every item upsert and after every successful write. It is safe to run any number of times because of `UNIQUE(item_id, reminder_type, due_at_snapshot)` — a new due date is simply a new key, which is how the Option A fresh-schedule behavior falls out for free.

Two rules that are easy to get wrong:

- Reminders whose target already passed when the item was discovered are recorded `skipped(missed_window)` and **never sent retroactively**. Later targets still fire. Nothing sends once `now >= due_at` (FR-4).
- The skip→pending transition is deliberately symmetric: un-completing or un-archiving an item resurrects a still-future reminder.

### Notion adapter details that bite

- API version `2025-09-03` split databases from **data sources**. `NOTION_DB_*` are *database* IDs; the `data_source_id` is resolved at startup via `GET /v1/databases/{id}` → `data_sources[]` and cached in `system_state`. Queries go to `POST /v1/data_sources/{id}/query`. Normalization reads `parent.data_source_id`, **not** `database_id`.
- Deletion/archiving is detected via `in_trash` on the daily full reconcile, because trashed pages drop out of query results (FR-9).
- `Course` is a **Relation**, so it returns a page ID, not a name. Resolve it through the `courses` cache; on a miss, `GET /v1/pages/{id}` for the title. The app never writes to `Course`.
- `Due Date` is date-only → 11:59 PM in `TIMEZONE`, stored as UTC `timestamptz`. "X days remaining" formula columns are read-only and never touched.
- `Status` is a `status` property type (not `select`) in both databases.

### The `Done` / `Status` coupling — the one deliberate deviation

`is_complete(item) = item.done OR item.status == 'Completed'` — **both checked independently**, because rows predating this system may have `Done = true` with `Status` stuck at `Not started` (FR-3).

On write, the two always move together: setting status to `Completed` also writes `Done = true` in the same PATCH; `Not started`/`In progress` write `Done = false`. This replicates Notion's "Mark as done" button, which the API cannot invoke directly. It only applies when the item's database has `NOTION_*_HAS_DONE=true`.

Where this lives matters: the **Validator has no opinion on `Done` at all** — `ApplyChange` carries only `status` and `due_date`. `Done` is derived purely inside `NotionWriter`. The write pipeline as a whole only ever touches `Status`, `Done`, and `Due Date`.

### Writes are never fire-and-forget

Live-fetch the page → capture previous values → one `PATCH` with only changed properties, writing by **option name** → read back → compare **all** changed properties including `Done`. A mismatch is a failure, reported honestly ("the change was NOT made"), never a success claim. No blind retries: one retry on transient 5xx/429, then re-verify.

### Idempotency is enforced by the database, not by hoping

| Risk | Control |
|---|---|
| Duplicate reminder job / retry | `UNIQUE(idempotency_key)`, `UNIQUE(item_id, reminder_type, due_at_snapshot)`, `FOR UPDATE SKIP LOCKED` claim |
| Duplicate inbound message | `UNIQUE(provider_message_id)` in `processed_inbound_messages` |
| Two app instances during deploy | Postgres advisory lock around job runs |
| Crash mid-send | Stale-claim recovery via the `ref_token` in the email footer |
| Crash mid-inbound | Stuck `processing` rows are flagged and alerted — **never silently retried** |

A duplicate send is preferred over a silently lost reminder.

### LLM and date resolution

- The LLM returns only the user's **date phrase** (`due_date_text`). The app resolves it deterministically, grammar-first, relative to the email timestamp (§2.3.4). "Friday" resolves; "next Friday", "push it back", "end of the week" are `Ambiguous` → clarification, no write.
- DeepSeek's API supports generic JSON mode only — **no JSON-schema `response_format`**. The prompt must state field names/types and include an example; Pydantic validation on the response is mandatory, not a fallback. Thinking defaults to ON and must be disabled via `extra_body={"thinking": {"type": "disabled"}}`. Empty content is a known JSON-mode failure mode — treat it as invalid output (one retry, then safe-fallback clarification).
- The email body is untrusted data, never instructions.

### Inbound pipeline ordering

The order in §2.3.2.E is load-bearing: dedupe `INSERT ... ON CONFLICT DO NOTHING` first, then sender allowlist (plus auto-reply headers), then resolve item **by stored Gmail thread/message IDs, never by name**. An unmappable reply fails safe — `inbound_unmapped` audit event and a plain notice, no guessing.

`inbound_unmapped` is deliberately distinct from `inbound_ignored` so a sender problem is never conflated with a thread-mapping problem. The `audit_log.event_type` list in §2.3.5 is a closed vocabulary — add new events there when you add one. The audit log is append-only; the app never issues UPDATE or DELETE against it.

## Constraints

Hard rules for this repo. Everything below is a "never", not a preference.

### 1. Never point a dev or test run at production

This app writes to real Notion databases and sends real email to a real person's inbox. A careless run can move someone's actual deadline or mail them at midnight.

- Never run against the production Notion databases, the production Postgres, the real sender account, or `REMINDER_RECIPIENT` unless the user has explicitly asked for that.
- Assume every write and every send is real until you have verified otherwise — check which `NOTION_DB_*`, `DATABASE_URL`, and `GMAIL_SENDER_ADDRESS` are in effect before triggering anything that sends or writes.
- Prefer dry runs, fake adapters, and the admin API's read-only endpoints. `MAX_OUTBOUND_EMAILS_PER_HOUR` is a kill switch, not a testing convenience — don't raise it to make a test pass.

### 2. Never read or expose secrets

- Never open `.env`, and never print its values. `.env.example` is the only env file to read.
- Never commit secrets, and never put real credentials, tokens, or IDs into tests, fixtures, logs, or commit messages.
- If you need to know whether a variable is set, check for presence without echoing the value.

### 3. Time is always America/New_York

The deployment timezone is **`America/New_York`** — the owner's local time, and the zone every deadline, reminder target, and date phrase is reasoned about in. Treat Eastern as the answer whenever a time question comes up.

It is configuration (`TIMEZONE`, default `America/New_York`). Read it from config; **never hardcode `"America/New_York"` or a fixed offset like `-05:00` in logic.** Behave as if the answer is always America/New_York, but arrive there through the config value. Use `zoneinfo`, never a hand-rolled offset.

- A date-only due date means **11:59 PM Eastern** on that date, stored as UTC `timestamptz`. Oct 3 is `2026-10-03 23:59 America/New_York` = `2026-10-04 03:59 UTC` while EDT is in effect — not `2026-10-03 23:59 UTC`.
- Offsets are absolute durations, so across a DST change the local wall-clock time shifts by an hour. This is accepted (FR-2) — don't "fix" it.
- The daily full reconcile runs at 04:00 Eastern.

### 4. Use the injected `Clock`; never call `datetime.now()`

Reaching for the system clock directly makes tests flaky and reminder targets wrong, which is the entire product.

- All time comes from the injected `Clock` port. No `datetime.now()`, `date.today()`, `time.time()`, or equivalent in application code.
- Store UTC `timestamptz`. Convert to `TIMEZONE` only at the boundary where a human reads or writes a date.
- Never use naive datetimes. Every datetime is timezone-aware.

### 5. Tests never call live services

No test, CI job, fixture, or local experiment makes a live Notion, Gmail, or DeepSeek call.

- Unit tests cover the pure `domain/` functions.
- `tests/contract/` uses recorded or mocked LLM outputs.
- `tests/integration/` uses a real Postgres with fake Notion and Gmail adapters.

### 6. The write surface is exactly three properties

- The app writes only `Status`, `Done`, and `Due Date`. Never write `Course`, `Type`, the title, or any other property; never touch formula properties, which are read-only via the API.
- Adding a writable property is a spec change, not an implementation detail.

### 7. Change discipline

- The stack is fixed by §2.2. Adding a dependency means updating the spec in the same change.
- Schema changes go through Alembic. §2.3.5 is the contract, not a suggestion.
- `audit_log.event_type` is a closed vocabulary — add a new event to §2.3.5 when you add one.
- Don't weaken an idempotency control (unique constraints, `FOR UPDATE SKIP LOCKED` claims, the advisory lock). A duplicate send is acceptable; a silently lost reminder is not.

## Build phases (Appendix C)

All six are complete. The order below is the order they were built in, and it is why the
reminder flow is LLM-free: V1 could not start until the MVP was working.

1. **Notion read + local DB**: config, Notion adapter, normalization, 11:59 PM rule, upsert, daily reconcile. **Done**
2. **Reminder engine**: rules, planner/`reconcile_reminders`, scheduler with claims, missed-window and completed handling, audit. **Done**
3. **Email sending + thread mapping**: Gmail OAuth CLI, compose, send, store message/thread IDs, stale-claim recovery, failure alerts. **MVP done**
4. **Inbound handling**: poller, dedupe, sender allowlist, item mapping, reply-text extraction. **Done**
5. **AI interpretation**: intent schema, DeepSeek adapter, date resolver, validator, clarification loop. **Done**
6. **Safe writes**: NotionWriter, read-back verification, confirmation/failure emails, local update plus reconcile. **V1 done**

## Open items (Appendix D)

Four things that need a real account or a real call to verify — don't assume they're done:

1. The Google OAuth consent screen must be set to "In production" — refresh tokens for
   "Testing"-status apps expire after about 7 days and reminders stop silently.
2. The dedicated sender Gmail account must exist, and `GMAIL_SENDER_ADDRESS` must name it.
3. Lehigh's mail filtering should be checked — mail from the sender account may be junked.
4. `DEEPSEEK_API_KEY` must be set. V1 introduced the system's only LLM call site, so this is
   now load-bearing rather than optional; without it every reply fails to interpret.

These are the gate between "verified by tests" and "working". Nothing in this repository has
ever made a live call to Notion, Gmail, or DeepSeek (constraint 5), so the first live pass is
its own piece of work rather than a formality.
