# Changelog

All notable changes to this project are recorded here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) in spirit — dates are `YYYY-MM-DD`, entries are grouped as Added / Changed / Fixed / Removed. Record a change when it lands, under the current date. Cover application code, schema and migrations, configuration, and documentation; skip trivial edits.

## [Unreleased]

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
