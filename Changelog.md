# Changelog

All notable changes to this project are recorded here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) in spirit — dates are `YYYY-MM-DD`, entries are grouped as Added / Changed / Fixed / Removed. Record a change when it lands, under the current date. Cover application code, schema and migrations, configuration, and documentation; skip trivial edits.

## [Unreleased]

### 2026-09-29

#### Added

- **V1 — reply-to-edit (build phases 4–6).** The owner can now reply to a reminder to change an item's status and/or due date, and every write is read back before it is confirmed. This is the first and only place an LLM is used.
  - **Inbound handling (phase 4).** `integrations/gmail/parse.py` (reply-text extraction, `From:` parsing, auto-reply detection, payload decoding) and `integrations/gmail/poller.py` (the `history.list` logic and its bounded-search fallback). `GmailClient.poll_new` is implemented against the live API, with every blocking call dispatched through `asyncio.to_thread`; HTTP 404 falls back to `in:inbox newer_than:2d` and relies on the dedupe, while any other error propagates so a failed poll never looks like "nothing new". `db/repositories/inbound.py` adds the dedupe claim, the terminal transition, and the stuck-row reader. `db/repositories/threads.py` gains the readers the reply mapper needs.
  - **AI interpretation (phase 5).** `domain/intents.py` (the Pydantic intent schema, `extra="forbid"`, with per-action consistency validation), `domain/date_resolver.py` (the §2.3.4 table, grammar-first, with `Ambiguous` as the default for anything unrecognised), `domain/validator.py` (intent + item → `ApplyChange` | `NoChangeNeeded` | `NeedsClarification`), and `integrations/llm/{prompts,deepseek}.py`. The adapter uses generic JSON mode only, disables thinking, validates every response through Pydantic, and retries exactly once before falling back to a clarification; a transport error propagates rather than being disguised as a question.
  - **Safe writes (phase 6).** `integrations/notion/writer.py` — live fetch, one `PATCH` of only the changed properties written by option name, with `Done` derived from `status`, then read-back verification of every changed property. A write that does not land is reported as a failure ("the change was NOT made"), never as a success. `NotionRestClient.update_page` is implemented, building a single PATCH body from the non-`None` arguments.
  - **`services/inbound_service.py`** — §2.3.2.E's pipeline in its specified order, including the clarification loop (`MAX_CLARIFICATION_ROUNDS`, thread `state`, `pending_clarification`), the stuck-row check, and the alerts for a DeepSeek outage and a stuck message.
  - **Ops.** The `gmail_poller` job (90 s) and `POST /admin/gmail/poll`; four new alert types (`gmail_poll_failed`, `gmail_auth_failed`, `deepseek_failed`, `inbound_stuck`); the reply-flow email templates (`render_clarification`, `render_confirmation`, `render_failure`, `render_notice`).
  - **Production wiring.** `src/app/bootstrap.py` (`build_container`) and `src/app/asgi.py` (the uvicorn entrypoint). See *Fixed* below.
  - **Tests:** 392 unit, 25 contract (new directory), 144 integration, 10 end-to-end — 571 total, up from 333.
- `tests/contract/` — the interpreter against mocked LLM responses, including every invalid-output path and the request payload that pins the DeepSeek API contract.
- **`openai>=3.22.0`** as a dependency, which makes the §2.2 stack true. Note the SDK bundles its own transport (`httpx2`) alongside the repo's pinned `httpx`, and it raises on an empty API key at client construction — so `DeepSeekInterpreter` builds its client lazily.

#### Fixed

- **The service could not start.** `Dockerfile` ran `uvicorn app.main:app` and `CLAUDE.md` documented the same command, but `main.py` defines no module-level `app` and **nothing in `src/` constructed a real `AppContainer`** — every test injects its own, so no test caught it. `src/app/bootstrap.py` now holds `build_container(settings)` and `src/app/asgi.py` holds the `app` uvicorn serves. It is deliberately not in `main.py`: the test harness imports `create_app` from there, and a module-level app would have made every integration test read the real `.env` (constraints 1 and 2). The entrypoint is now `app.asgi:app`.
- **A reverted due date lost its reminders.** Spec §2.3.2.B step 2 revived a `skipped` reminder but not a `superseded` one, so a due date moved away and back left the original rows occupying the unique key and the restored date received no reminder — a silently lost reminder. A `superseded` row whose `due_at_snapshot` matches the restored date now flips back to `pending` under the same guards as a skip, and `_flip_to_pending` accepts it. A `sent` row is still never revived.
- **Stale-claim recovery wrote no thread-mapping rows.** It marked a reminder `sent` after a crash without creating `email_threads`/`outbound_messages`, so a reply to a recovered reminder would have found no mapping and been audited `inbound_unmapped`. Recovery now writes both rows from the ids Gmail returns, idempotently, so a second pass neither raises nor duplicates.
- **`email_threads.provider_thread_id` was `UNIQUE` in migration `0001` but not in the model**, so the model and the schema disagreed. Declared on the model for parity.
- **Two docstrings in `src/app/db/repositories/reminders.py` stated things the reverted-due-date fix had made false** — that a resurrection "only touches a `skipped` one", and that "a superseded row is never revived by the planner". The behaviour they described (`skip_reason = None` on supersession) is unchanged and still correct; the reasoning given for it was not. Comments only.
- **`CLAUDE.md` and `Project_status.md` claimed `.github/workflows/ci.yml` had never run.** It has, on every push and pull request since 2026-09-28, and the claim was wrong when it was written — the repository has had a remote the whole time. Both now describe what CI actually does and point at `gh run list`. This supersedes the same claim in the 2026-09-28 entry below.

#### Changed

- **V1 merged to `main` and pushed** (`525c35a`, a `--no-ff` merge of `feat/v1`). The merged tree is byte-identical to the verified `feat/v1` tip, and CI passed on it in 58s — the first time the full suite has run on Linux against a fresh PostgreSQL 16 in a clean checkout, which is independent of the local Windows run.
- `src/app/domain/types.py` — the V1 placeholders are replaced by the real shapes: `InboundMessage` (what the mail adapter hands back), a typed `PollResult`, and an expanded `InterpretationContext`. `Intent` moved to `domain/intents.py` as a Pydantic model.
- `src/app/services/alert_service.py` — `ALERT_TYPES` grows from three to seven, matching the failures §2.3.4 names.
- `src/app/jobs.py` — the module docstring's claim that there is "deliberately no Gmail poll job" is no longer true; the poller is registered on `GMAIL_POLL_INTERVAL_SEC` and its failures alert `gmail_poll_failed`.
- `tests/integration/test_admin_api.py` — `test_gmail_poll_is_404` is replaced by tests asserting the route exists, requires the admin token, and runs the poller job.
- `tests/unit/test_fixtures_smoke.py` — the MVP's "no write" and "no LLM" guards are replaced by tests of the V1 behaviour, while the unscripted-interpreter guard is kept.
- `project_spec final.md` — §2.2 (the `openai` pin and the lazy client), §2.3.2.B (the reverted-due-date rule, and the "Known gap" note replaced by the closure), §2.3.2.C (recovery writes the thread rows), §2.3.3 (the tree gains `asgi.py`, `bootstrap.py`, `db/repositories/inbound.py`, `tests/contract/`), §2.3.4 (the alert vocabulary as a table), §2.3.5 (`inbound_stuck` added to the closed audit vocabulary).
- `Architecture.md`, `Project_status.md`, `CLAUDE.md` — updated for V1: the run command is `app.asgi:app`, both milestones are complete, and the "Carried into V1" items are closed.
- `CLAUDE.md` — reviewed against the codebase once V1 landed. Corrected a duplicated `.env` paragraph, added `container.py`/`AppContainer` to the layout and architecture sections (the dependency-injection seam every test relies on was undocumented) and `tests/e2e/` to the tree, and reconciled the open-items list with the four go-live items. Added the non-obvious traps: how to run a single test, that migrations are hand-written because `--autogenerate` cannot express the partial indexes, and the test conventions (`--strict-markers`, `asyncio_mode=auto`, the harness fixtures imported by name into each database-backed conftest, and the deliberate absence of `__init__.py` under `tests/`).

### 2026-09-28

#### Added

- **The MVP application (build phases 1–3)** — the whole reminder flow end to end, with no LLM call anywhere in it.
  - **Scaffolding:** `pyproject.toml` (Python 3.12 and the §2.2 dependency set), `uv.lock`, `.python-version`, ruff/mypy/pytest configuration, `Dockerfile`, `docker-compose.yml` (PostgreSQL 16), and a GitHub Actions workflow. The workflow has never run — this repository has no remote — so it is unverified configuration.
  - **Domain (pure, no I/O):** `due_time.py` (the 11:59 PM rule), `reminder_rules.py`, `types.py`, and `planner.py` — `plan_reminders` and its action union. The planner returns typed actions rather than writing rows, which is what makes it unit-testable without a database.
  - **Persistence:** the §2.3.5 schema as SQLAlchemy 2.0 models plus a handwritten Alembic migration, and repositories for items, reminders, threads, courses, audit, and `system_state`.
  - **Integrations:** a Notion REST client (the 2025-09-03 data-sources model) with a pure normalizer, a Gmail client, and the plain-text composer.
  - **Services:** `SyncService` (incremental sync with a 5-minute cursor overlap, course-cache resolution, full reconcile), `ReminderService` (the claim → guard → send tick, backoff, stale-claim recovery, the outbound kill switch), `AlertService`, and an audit wrapper.
  - **App:** `create_app` with APScheduler in the lifespan and a Postgres advisory lock around job runs, the admin API from §2.3.6, and the `gmail-auth` CLI.
  - **Tests:** 199 unit, 129 integration against a real PostgreSQL 16, and 5 end-to-end.
- `Architecture.md` — system design, data flows, component architecture, and cross-cutting invariants.
- `Project_status.md` — milestones, current progress, and next steps.
- `Changelog.md` — this file.
- `CLAUDE.md` — repository guidance for Claude Code: goals, commands, architecture, constraints, build phases.
- `.env.example` — template documenting every environment variable, with spec defaults and the values that must be supplied.
- `.gitignore` — ignores `.env` and `.env.*`, re-includes `.env.example`, plus standard Python and editor artifacts.
- `.claude/commands/update-docs.md` — a `/update-docs` slash command that brings the documents named in `CLAUDE.md` up to date from the repository's actual state.

#### Changed

- `project_spec final.md` — §2.3.3's tree now lists the four files the implementation needed that it did not name: `docker-compose.yml`, `src/app/container.py` (the dependency-injection seam), `.github/workflows/ci.yml`, and `tests/e2e/`. §2.3.2.C records the retry backoff curve and why its base is 30 seconds. §2.3.2.B notes the reverted-due-date gap.
- `CLAUDE.md` — the `## Commands` section replaced with real commands, and the status line updated from "pre-implementation".
- Project directory renamed to `notion-email-agent`; the project-structure tree in the spec (§2.3.3) and `CLAUDE.md` updated to match.

### 2026-09-27

#### Added

- `project_spec final.md` — the authoritative design document: product requirements, technical design, database schema, decision log, edge-case coverage, build phases, and open items.
