---
description: Update the project documents listed in CLAUDE.md's Documentation section
argument-hint: [what changed — e.g. "finished phase 1", or blank to infer from the repo]
allowed-tools: Read, Edit, Write, Glob, Grep, Bash
---

Bring the project documents named in CLAUDE.md's **Documentation** section up to date with work that has actually landed.

Scope for this run: **$ARGUMENTS**

If the scope above is empty, infer it in step 2.

## 1. Read the document set from source

Read `CLAUDE.md` and take the file list from its `## Documentation` section. **Do not rely on a remembered list** — that section is the registry, and it changes. Read each listed file before editing it.

If a registered file is missing, report that and stop rather than creating it. A missing document is a decision for the user.

## 2. Establish what actually changed

Work down this list and use the first source that applies:

1. **The scope given above**, if any. Treat it as a claim to verify, not a fact to record — confirm it against the repo before writing it down.
2. **Git history**, if this is a git repo: `git log --oneline -20`, `git status --short`, and `git diff` for anything uncommitted. Note that this project is not currently a git repository; check rather than assuming.
3. **The repo's real state vs. what the documents claim.** Compare what exists on disk (`Glob`, directory listings, file timestamps) against what each document says is done. A document that says "not started" while the code exists is the signal.
4. **The current conversation**, if the work happened in this session.

If you cannot establish what changed with confidence, say so and ask. Do not fill the gap with plausible-sounding progress — a fabricated changelog entry is worse than a missing one, because it is trusted.

## 3. Route each change to the right document

Each document has one job. Put a change in the one that owns it, and cross-reference rather than restating it elsewhere.

| Kind of change | Document | What it looks like there |
|---|---|---|
| Something was added, changed, fixed, or removed | `Changelog.md` | A dated entry under the current date, grouped Added / Changed / Fixed / Removed |
| A build phase or milestone moved | `Project_status.md` | Updated phase table + "What's next"; refresh the **Last updated** line |
| The system's shape changed — flows, components, layering, invariants | `Architecture.md` | The affected section; it describes design, not history |
| A requirement, constraint, or decision changed | `project_spec final.md` | The relevant section **and** the decision log (Appendix A) if it was a decision |

`project_spec final.md` is authoritative and is not a scratchpad. Edit it only when a decision or requirement genuinely changed — not to record that work happened. That is what the changelog is for.

## 4. Write honestly

These documents are load-bearing: they are what a future reader trusts instead of re-deriving the state from scratch. Three rules:

- **Never mark work complete that you have not verified.** "In progress" must name what is actually working, not what is being attempted.
- **Never invent a date, a decision, or a rationale.** If you do not know why a choice was made, write what changed and leave the why out.
- **Preserve the existing structure and voice** of each file. Match its heading levels, its tables, its level of detail. You are updating these documents, not rewriting them.

Keep entries proportionate. A one-line change gets a one-line entry; a new component gets a paragraph. Do not pad.

## 5. Report

Finish with a short summary, in this shape:

```
Updated:
  Changelog.md       — <what you recorded>
  Project_status.md  — <what moved>

Unchanged:
  Architecture.md    — <why nothing needed to change>

Needs your call:
  <anything you could not verify, or a doc that should exist but doesn't>
```

Name anything you left alone deliberately, so the user can tell the difference between "checked and fine" and "didn't look".
