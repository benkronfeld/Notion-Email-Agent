# Architecture

> **Status: complete — build phases 1–6 are built.** Sections 1, 3 and 4 describe the system as it now exists, including the reply flow. `MailClient.poll_new` and `NotionClient.update_page` are implemented; `InboundService`, `NotionWriter`, `date_resolver`, `validator`, and the DeepSeek `IntentInterpreter` are written. Section references (§) point to [`project_spec final.md`](project_spec%20final.md), which is authoritative if the two ever disagree.

## 1. Overview

A single-user Python service that keeps a local copy of two Notion databases, emails the owner a reminder before each deadline, and (from V1) lets the owner change an item's status or due date by replying to a reminder email.

It talks to three external systems:

| System | Role |
|---|---|
| **Notion API** | Source of truth for items; also the target of validated writes |
| **Gmail API** | Outbound reminders and confirmations; inbound by polling (no Pub/Sub) |
| **DeepSeek API** | Converts one reply into one structured intent. Nothing else. |

There is no frontend. The owner interacts through Notion and email. A token-protected admin API exists for health checks, manual job triggers, and inspecting state (§2.3.6).

**Deployment:** one always-on container plus one managed PostgreSQL 16, both on Railway. FastAPI serves the admin API; APScheduler runs four jobs inside the app lifespan, guarded by a Postgres advisory lock so overlapping deploys can't double-process. The ASGI entrypoint is `app.asgi:app`; `app.main` stays import-pure (it exposes `create_app` and no module-level app) so that the one module which reads `.env` is never imported by the test suite.

## 2. Data flow

```
                 ┌─────────────────────────┐
                 │  Notion (source of truth)│
                 │  Assignments & Readings  │
                 │  Exams & Projects        │
                 └───────▲─────────┬────────┘
     validated writes +  │         │ hourly incremental sync
     read-back verify    │         │ + daily full reconcile
                         │         ▼
┌────────────────────────┴───────────────────────────────────┐
│  Service (FastAPI + APScheduler), one container             │
│                                                             │
│  Jobs:  notion_sync (60m) · full_reconcile (daily)          │
│         reminder_scheduler (5m) · gmail_poller (90s)        │
│                                                             │
│  Domain (pure):  due-time · target planner · date resolver  │
│  Services:  ReminderService · InboundService · Validator ·  │
│             NotionWriter · AlertService · AuditLog          │
│  Ports:  NotionClient · MailClient · IntentInterpreter · Clock│
└───────▲───────────────────┬───────────────────┬────────────┘
        │                   │                   │
   PostgreSQL          Gmail API           DeepSeek API
 (items, reminders,   (send reminders,     (reply → structured
  threads, audit…)     poll replies)        intent JSON only)
```

### 2.1 Reminder flow — no LLM anywhere in the path

```
Notion → sync → items → planner → reminders
       → scheduler claims due rows → Gmail send
       → email_threads / outbound_messages → audit
```

Deterministic end to end. An item is normalized and upserted; `reconcile_reminders` derives its reminder rows from the due date and item kind; the scheduler claims whichever are due and sends one email per reminder in a new Gmail thread.

### 2.2 Reply flow — the LLM appears in exactly one step

```
Gmail poll → dedupe + sender check → thread → item_id → context
           → DeepSeek → validated intent → resolver/validator
           → (clarify | Notion write → read-back → local update
                      + reconcile → confirmation) → audit
```

**The LLM boundary is the central design decision.** The model's only job is turning reply text into a structured intent (`action`, `status`, `due_date_text`). It never schedules, decides eligibility, deduplicates, identifies items, or computes dates. Everything downstream is deterministic code that validates the intent and performs the write. When anything is uncertain, the system writes nothing and asks.

## 3. Component architecture

### 3.1 Layering

| Layer | Location | Rule |
|---|---|---|
| **Domain** | `src/app/domain/` | Pure, no I/O. All decision logic lives here and is unit-tested directly. |
| **Services** | `src/app/services/` | Orchestration. Owns the flows; delegates decisions to domain and effects to ports. |
| **Integrations** | `src/app/integrations/` | Adapters implementing ports. The only place that speaks HTTP to a third party. |
| **Persistence** | `src/app/db/` | SQLAlchemy models + repositories. Owns the constraints that enforce idempotency. |
| **API** | `src/app/api/` | Admin/ops surface only. No public endpoints. |

The domain layer is where the value is: `due_time`, `reminder_rules`, `planner`, `date_resolver`, `intents`, `validator`. Keeping them I/O-free is what makes the tricky parts (reminder math, date resolution, completion logic) testable without a database or a network.

### 3.2 Ports

Every external dependency sits behind an interface so it can be swapped and faked (§2.3.6).

| Port | Abstracts | Why it's a port |
|---|---|---|
| `NotionClient` | Notion REST API | Tests use a fake; the data-sources API model may shift |
| `MailClient` | Gmail API (send, poll, find-by-token) | No live calls in tests; stale-claim recovery needs `find_sent_by_token` |
| `IntentInterpreter` | DeepSeek | Contract tests run recorded LLM outputs; provider is replaceable |
| `Clock` | Wall-clock time | Tests need frozen time; no `datetime.now()` in application code |

### 3.3 Component inventory

| Component | Responsibility | §|
|---|---|---|
| `SyncService` | Hourly incremental sync + daily full reconcile; normalize, upsert, then reconcile reminders | 2.3.2.A |
| `ReminderService` | Scheduler tick: claim → pre-send check → send → mark sent; stale-claim recovery | 2.3.2.C |
| `InboundService` | The reply pipeline: dedupe, sender check, item mapping, interpret, validate, write, confirm | 2.3.2.E |
| `NotionWriter` | Live fetch, single `PATCH` of changed properties, read-back verification | 2.3.4 |
| `AlertService` | Rate-limited owner alerts on persistent failure, per failure type | 2.3.4 |
| `AuditLog` | Append-only event writes; the system's history | 2.3.5 |
| `planner` (domain) | `reconcile_reminders` — decides every reminder row's state | 2.3.2.B |
| `date_resolver` (domain) | Grammar-first phrase → date, or `Ambiguous` | 2.3.4 |
| `validator` (domain) | Intent + item → `ApplyChange` \| `NoChangeNeeded` \| `NeedsClarification` | 2.3.4 |

### 3.4 Jobs

| Job | Interval | Does |
|---|---|---|
| `notion_sync` | 60 min | Incremental page query per data source, upsert, reconcile |
| `full_reconcile` | Daily, 04:00 ET | Query all pages; deactivate anything trashed or missing |
| `reminder_scheduler` | 5 min | Claim and send due reminders |
| `gmail_poller` | 90 s | Poll for replies, run the inbound pipeline, then flag stuck `processing` rows |

The Gmail poller is the only job that both reads mail and can write to Notion, and it is the
only place an LLM is consulted. Everything it produces goes through `InboundService`, where
the dedupe, the deterministic validation, and the write verification live; the job body adds
nothing of its own beyond flagging abandoned messages.

## 4. Invariants that span components

These hold across the whole system and are enforced in different layers:

- **Idempotency is a database property, not a code path.** Unique constraints on `idempotency_key`, `(item_id, reminder_type, due_at_snapshot)`, and `processed_inbound_messages.provider_message_id`; `FOR UPDATE SKIP LOCKED` for claims; a Postgres advisory lock around job runs. A duplicate send is acceptable; a silently lost reminder is not.
- **Writes are verified, never assumed.** Every Notion write is a live fetch → single `PATCH` → read-back → compare. A mismatch is a failure, reported as such.
- **The write surface is exactly three properties:** `Status`, `Done`, `Due Date`. `Done` is derived from `status` inside `NotionWriter` — the validator has no opinion on it.
- **Completion is derived, not read from one field:** `done OR status == 'Completed'`, checked independently.
- **The audit log is append-only** and its `event_type` vocabulary is closed (§2.3.5).
- **Time is UTC in the database**, converted to `America/New_York` only at human boundaries, always via the injected `Clock`.

## 5. Where to read more

| Topic | Spec |
|---|---|
| Product requirements, jobs to be done, non-goals | §1.1–1.2 |
| Functional requirements (FR-1 … FR-13) | §1.2 |
| Tech stack and configuration | §2.2 |
| System design: sync, planner, scheduler, inbound pipeline | §2.3.2 |
| Project structure | §2.3.3 |
| Components: date resolver, interpreter, validator, writer, templates | §2.3.4 |
| Database schema | §2.3.5 |
| API design and internal ports | §2.3.6 |
| Why decisions were made | Appendix A |
| Edge cases | Appendix B |
| Build order | Appendix C |
| Deferred verification items | Appendix D |
