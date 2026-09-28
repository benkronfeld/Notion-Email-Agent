# Project status

**Last updated:** 2026-09-28
**Current phase:** M1 — **MVP complete**. Build phases 1–3 implemented and verified; V1 not started.

## Milestones

Two milestones, defined by the success criteria in spec §1.2 and delivered by the six build phases in Appendix C.

| Milestone | Scope | Success criteria | Status |
|---|---|---|---|
| **M1 — MVP** | Build phases 1–3. Reminders only; **no LLM call anywhere in the flow.** | §1.2 criteria 1–5 | **Complete** |
| **M2 — V1** | Build phases 4–6. Adds reply-to-edit. | §1.2 criteria 6–10 | **Not started** |

M1 is independently useful and testable on its own. M2 must not begin, and no LLM call may enter the flow, before M1 is complete and working.

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

**Design: complete.** The spec was finalized 2026-09-27. Every question resolvable from chat — property types, Notion API version, DeepSeek structured-output support, what gets sent to the model — was closed and recorded in Appendix A. Three items remain that need a real account or a real send to verify (see below).

**Repository setup: complete.** `.gitignore`, `.env.example`, `CLAUDE.md`, `Architecture.md`, `Project_status.md`, and `Changelog.md` are in place, along with a `/update-docs` command in `.claude/commands/` that keeps these documents current. Configuration is documented; secrets live in a gitignored `.env`.

**Implementation: complete for M1.** Build phases 1–3 are done and verified: **333 tests
pass** — 199 unit, 129 integration against a real PostgreSQL 16, and 5 end-to-end. No V1
work has started and no LLM call exists anywhere in the code.

| # | Build phase | Milestone | Status |
|---|---|---|---|
| 1 | Notion read + local DB | M1 | Complete |
| 2 | Reminder engine | M1 | Complete |
| 3 | Email sending + thread mapping | M1 | Complete |
| 4 | Inbound handling | M2 | Not started |
| 5 | AI interpretation | M2 | Not started |
| 6 | Safe writes | M2 | Not started |

### How M1 is verified

- **Unit (199).** The pure domain: the 11:59 PM rule across both DST boundaries, the
  reminder rules, the composer, Notion normalization, config, the `Clock` port, and the
  planner's case matrix — including idempotence and the skip→pending resurrection.
- **Integration (129).** Against a real PostgreSQL 16: the `FOR UPDATE SKIP LOCKED` claim
  under two concurrent sessions, `apply_plan`'s transitions, the scheduler tick's guards and
  backoff ladder, the sync and its cursor handling, and the admin API over the real lifespan.
- **End-to-end (5).** A frozen-clock dry run driving the real app with fake adapters: sync,
  send exactly once, never twice, and skip a reminder whose deadline has passed rather than
  sending it late.

**Not verified:** any live Notion, Gmail, or DeepSeek call — by design (CLAUDE.md
constraint 5). `.github/workflows/ci.yml` has never run, because this repo has no remote.

## What's next

**M2 — V1 begins with Phase 4, and must not begin until M1 is running for real.** M1 being
*verified* is not the same as M1 being *deployed*: no live Notion or Gmail call has ever
been made, so the three Appendix D items below are the real gate.

**Phase 4 — inbound handling.** The reply pipeline's deterministic half: the Gmail poller,
dedupe against `processed_inbound_messages`, the sender allowlist, thread→item mapping by
stored Gmail IDs (never by name), and reply-text extraction. No LLM yet.

**Phase 5 — AI interpretation.** The intent schema, the DeepSeek adapter, the date resolver,
the validator, and the clarification loop. This is where the first LLM call enters the
system — exactly one, and only to turn reply text into a structured intent.

**Phase 6 — safe writes.** `NotionWriter`, read-back verification, confirmation and failure
emails, and the local update plus `reconcile_reminders`.

### Carried into V1

- **Stale-claim recovery does not write thread-mapping rows.** It marks a reminder `sent`
  but creates no `email_threads`/`outbound_messages` row, so a reply to a recovered reminder
  would miss V1's mapping. Needs a deliberate decision before Phase 4.
- **A reverted due date loses its reminders.** If a due date moves away and back, the
  original rows stay `superseded` (spec §2.3.2.B step 2 revives `skipped` rows only), so the
  restored date gets no reminder. Documented in `tests/unit/test_planner.py`.

## Blockers and open items

Three items need a real account or a real send, so they can only be closed by deploying
(Appendix D). **These are the gate between "M1 verified" and "M1 working".**

1. **Google OAuth refresh token lifetime.** Set the consent screen to "In production" — tokens for apps left in "Testing" expire after about 7 days, and reminders would stop silently.
2. **Sender Gmail address.** Create the dedicated account and set `GMAIL_SENDER_ADDRESS`.
3. **Lehigh mail filtering.** Confirm mail from the sender isn't junked by the school's mail system; consider allow-listing it.

## How to update this file

Update at every milestone boundary, at the end of each build phase, and whenever scope changes — then add a matching entry to [`Changelog.md`](Changelog.md). Keep the phase table honest: "in progress" should name what is actually working, not what is being attempted.
