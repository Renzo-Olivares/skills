---
name: flutter-pr-review
description: "Gate-based protocol for reviewing a Flutter pull request (flutter/flutter, flutter/packages). Use when drafting a code review of a PR or verifying someone else's draft: CLA, right-repository, CI triage, AI-contribution-policy, and test-requirement gates; regression-test verification on BASE vs PR; design review (API surface, producer vs consumer fixes, breaking changes, architecture); style nits; and the exact review payload/body format."
---

# Flutter PR Review Protocol

## Principles & voice

- **Embrace the yak shave.** Prefer asking for the fix in THIS PR over proposing follow-up PRs or filing issues.
- **Graceful professionalism.** Be polite and grateful; respect the contributor. Every comment explains **what** is happening, **why**, and the **next steps**, and sets expectations.
- **Transparency and honesty** over diplomacy-by-omission.
- Prefer a question to an assertion when unsure. Prefix non-blocking items `nit:`, style items `style nit:`.

## How gates work

The review is a sequence of gates, run in order.

- **STOP** — the review ends here. The draft's body is the comment to the author (what / why / next steps); `comments` is empty unless the gate says otherwise; record `STOPPED_AT: <gate>` in the internal record. One exception: Gate 5's no-`CICD` STOP, whose body is empty.
- **NOTE** — record the point (in the body or as an inline comment) and continue.
- Closing a PR, applying labels, and approving are **human actions**. The protocol only *recommends* them via `RECOMMENDED: approve | request changes | comment | close | blocked`.

Inputs to gather once: `pr view --json title,body,author,labels,isDraft,mergeable,files,statusCheckRollup,url`, `pr diff`, `pr checks`, linked issues with comments, all review threads (including `gemini-code-assist` and any earlier review by this reviewer), and the worktree at the PR head.

## Gate 1 — CLA

- **Check:** the author has signed the Google CLA.
- **Signal:** `statusCheckRollup` entry named `cla/google` is `SUCCESS`.
- **On fail:** STOP, `RECOMMENDED: blocked`.
- **Comment must say:** thanks; we cannot review until the CLA is signed (https://cla.developers.google.com/); once it is, the check turns green and review resumes.

## Gate 2 — Right repository

- **Check:** the change is not editing the framework's deprecated Material/Cupertino libraries.
- **Signal:** changed paths under `packages/flutter/lib/src/material/**` or `packages/flutter/lib/src/cupertino/**` (or their tests) in flutter/flutter.
- **On fail:** STOP, `RECOMMENDED: close`.
- **Comment must say:** these libraries are deprecated in the framework; the widgets now live in `flutter/packages` as `packages/material_ui` / `packages/cupertino_ui`; please re-submit the same change there; we recommend closing this PR.

## Gate 3 — Process / AI-contribution-policy red flags

Compare the PR body with the repo template (`.github/PULL_REQUEST_TEMPLATE.md`, `## Pre-launch Checklist`) and with the diff. STOP with `RECOMMENDED: close` if **any** of:

1. The template was replaced wholesale by AI-generated prose **and** at least one obvious checklist item is missing (tests, linked issue, …). *A contributor who skipped the process from the outset is unlikely to follow it during review.*
2. The description does not match the changes. *They did not review both enough to notice — the AI contribution policy was not followed.*
3. Irrelevant AI-generated files are included (agent plans, `*.plan.md`, `AGENTS.md`, `.cursor/`, scratch notes). *They did not review the diff enough to remove them.*

- **Comment must say:** which flag applies; cite `CONTRIBUTING.ai.md` and the Tree Hygiene page; they are welcome to open a new PR that follows the process.

## Gate 4 — Tests required

- **Skip if:** the `test-exempt` label is present.
- **Check:** if the diff adds/changes **no** test files, is the change one of the exemption categories?
  - refactor with no semantic change (e.g. null-safety migration)
  - configuration change
  - analysis change (fixing lints, enabling lints)
  - test-infrastructure change
  - manual dependency roll
  - fix to an existing test
  - cosmetic fix to an unpublished example app
- **Not in the list:** STOP, `RECOMMENDED: request changes`. Comment: tests are required (Tree Hygiene › Tests); name the test file/directory and the behavior to assert; review resumes once added.
- **In the list:** NOTE that the author must request a test exemption in `#hackers` on the Flutter Discord (which adds `test-exempt`); continue.

## Gate 5 — CI

- **Find the failure signal.**
  - **Checks reported:** for every failing check (conclusion `failure`, `timed_out`, `cancelled`, `startup_failure` or `action_required`; status `error` or `failure`) other than `cla/google` and the not-a-signal set below, find out **what** failed (GitHub Actions: `run view <run-id> --log-failed`; LUCI: open `detailsUrl`, extract the failing test names) and classify it from the log. Do not re-run it locally.
  - **Not a signal on an open PR:** `Google testing` (its result is internal; in progress for days), `Merge Queue Guard` (queued until the tree is green), `flutter-gold` and `tree-status` (statuses that wait on everything else or reflect the tree, not the PR), and any other check still pending (`cla/google` stays Gate 1's, whatever its state). Whatever their state: do not investigate, never STOP, and say nothing about them in the body or in `comments` — the author can do nothing about them. They are not `SKIPPED:` items either: nothing the protocol asks you to read was skipped.
  - **No `CICD` label:** CI has not been allowed to run on this head, so nothing may run here either — no local `analyze` / `test` stand-in, and no later gate. STOP, `RECOMMENDED: blocked`, with nothing for the author: the body is empty (no comment, no footer) and the reason (no `CICD` label; a maintainer adds it) goes in the internal record only. Still write the `.json`: `{"commit_id": "<HEAD_SHA>", "body": "", "comments": []}`.
- **Classify** each failure. Every outcome names the check and the reason for the call:
  - (a) **related to the change/approach** → STOP, `RECOMMENDED: request changes`. Comment names the check and the failing tests and asks that the approach be fixed so they pass before review continues.
  - (b) **the old expectation was wrong and the PR's behavior is right** → NOTE: name the check and the test, and why its old expectation is wrong, so the author updates it; continue.
  - (c) **flake / infra** (unrelated area, known `c: flake`, infra error in the log) → NOTE: "`<check>` looks like a flake / infra failure (<reason>), unrelated to this PR; a re-run should clear it"; continue.
  - (d) **inconclusive** → (a) if the failing test is in the changed area; otherwise NOTE: "`<check>` is failing in `<area>`; I couldn't tell from the log whether it's related — worth a look"; continue.

## Gate 6 — Is it the right shape for the problem?

- **Independent regression test.** If the linked issue has a repro, turn it into your own test at `<package>/test/_reviewer_generated/issue_<id>_test.dart`. It must **FAIL on BASE** at the same crash site or with the same wrong expectation the issue describes (not for an unrelated reason or a badly built expectation — fix the test until it does) and **PASS on the PR**.
  - PR does not make it pass → STOP, `RECOMMENDED: request changes`. Comment: the PR does not resolve the issue as reported; include the full test inline and where it still fails.
- **Author's regression test.** Exists, carries `// This is a regression test for https://github.com/<owner>/<repo>/issues/<id>`, FAILS on BASE, PASSES on the PR. Anything missing → NOTE with an inline comment on the test; continue.
- **Test layering (flutter/flutter).** Tests for `lib/src/widgets/` import only `package:flutter/widgets.dart` (+ test packages) and use `WidgetsApp`/`TestWidgetsApp`, never `material.dart`/`cupertino.dart`; material/cupertino tests may use `MaterialApp`/`CupertinoApp`.
- **Prove every bug you claim.** A comment asserting the PR introduces a bug must come with a failing test you wrote and ran (quote the failure). If you cannot make it fail, downgrade to a question or drop it.
- **New API.** Is each new public member necessary to fix the bug? If not, ask to remove it (inline).
- **Producer vs consumer.** Does the fix happen where the bad state is produced, or does it add guards in one consumer? Guards-only fixes leave the next consumer to hit the same bug — say so and ask for the producer-level fix.
- **Breaking changes.** Changed signatures, types, long-standing assumptions or invariants: if not necessary, ask to revert; if necessary, point to the breaking-change policy and the migration-guide requirement.
- **Architecture.** Sound within its subsystem — invariants, lifecycle, layering (read callers, adjacent code, existing tests; not just the hunks).
- **On a re-review:** comment only on what changed or is still open; explicitly acknowledge what the author addressed.

## Gate 7 — Style

Check the diff against `docs/contributing/Style-guide-for-Flutter-repo.md` (worktree copy). Each finding is an inline comment prefixed `style nit:` linking `https://github.com/flutter/flutter/blob/master/docs/contributing/Style-guide-for-Flutter-repo.md#<anchor>`, where `<anchor>` is derived from a heading **verified to exist** in the worktree copy (lowercase, punctuation stripped, spaces → `-`); if no dedicated heading exists, link the closest parent heading. No formatter-level nits (`dart format` catches those).

## The review draft

**Payload** (`drafts/<repo>-<N>.json`, exactly what the GitHub reviews API accepts):

```json
{"commit_id": "<HEAD_SHA>", "body": "...", "comments": [{"path": "...", "line": 123, "side": "RIGHT", "body": "..."}]}
```

- No `event` key. Optional `start_line`/`start_side` for ranges. Every `path`/`line` must be a right-side line present in `pr diff`. Must pass `python3 -m json.tool`.
- The payload is author-facing only: nothing in `body` or `comments` is reviewer bookkeeping.

**Body structure:**

1. Thanks + one-line summary of what the PR does.
2. On a STOP: the drafted comment (what is happening, why, next steps).
3. Notes from NOTE gates.
4. Footer, on its own line (absent only on Gate 5's no-`CICD` STOP): `— <agent-login> is an AI reviewer; a human maintainer makes the final call (see this account's profile for who to contact).` where `<agent-login>` is the login returned by `gh api user --jq .login`.

**Internal record** (`drafts/<repo>-<N>.md`, for the verifier and the human maintainer; never posted):

1. `STOPPED_AT: <gate number or none>`
2. `RECOMMENDED: approve | request changes | comment | close | blocked` — one-clause reason.
3. `READ:` files, threads and docs read; tests run with their BASE/PR outcomes.
4. `SKIPPED:` what was not read or run and why (`SKIPPED: none` if nothing; the checks Gate 5 lists as *Not a signal on an open PR* are never listed here). On an `approve` with zero comments and a >200-line diff, `READ:` must name the specific files and tests that justify the approval, not just "the diff".

**Comment hygiene:** no diff narration; no praise-only comments; no line numbers in prose (the anchor carries that); one comment per repeated issue, listing the other locations; no duplicates of existing threads; no restating CI beyond Gate 5's notes.

## Verifying a draft (QA checklist)

A wrong STOP is worse than a missed nit. Re-derive, do not trust prose.

- **Gate verdict:** recompute `STOPPED_AT` from the signals above — `cla/google` state; changed paths; template-vs-body and the list of stray files; `test-exempt` label, whether the diff touches tests, and whether the exemption category genuinely applies; the `CICD` label; which checks actually failed (read the same logs and agree with the call).
- **STOP bodies:** thank the author; state what / why / next steps; no lecturing; cite the right doc (CLA link, `CONTRIBUTING.ai.md`, Tree Hygiene › Tests, breaking-change policy); `RECOMMENDED` is one of the five values and consistent with the gate. Exception: Gate 5's no-`CICD` STOP has an empty body, and nothing from the PR head is run — not by the drafter, not by you; that is not a `SKIPPED:` item.
- **Regression tests:** re-run the reviewer's `_reviewer_generated/issue_<id>_test.dart` and the author's test on BASE and on the PR. The FAIL-on-BASE must be for the issue's reason. Mandatory whenever the draft claims the PR does not fix the issue.
- **Yak shave:** flag any comment that defers work to a follow-up PR or issue when it could be asked for here.
- **Per inline comment:** claim holds for THIS code at the anchored line (not a false positive, not handled elsewhere in the diff, not contradicted by a test); bug claims backed by a re-runnable failing test; anchor inside the right hunk; not redundant with other threads, CI, or the PR body; what/why/next-steps tone; `nit:` / `style nit:` used correctly; style-guide anchors resolve in the worktree copy; footer present in the body (except on Gate 5's no-`CICD` STOP); nothing claims to be human; `RECOMMENDED` matches severity (blocking issue ⇒ not `approve`).
- **Internal record:** `STOPPED_AT`, `RECOMMENDED`, `READ:` and `SKIPPED:` are in the `.md`, not in the body; `SKIPPED:` hides no gate signal (e.g. a failing check whose log was never read). The checks Gate 5 lists as *Not a signal on an open PR* are neither a gate signal nor a `SKIPPED:` item — not under `SKIPPED:` in the draft's `.md`, nor in your verdict below — whatever their state; neither the body nor any comment mentions them.
- **Verdict format** — the verifier's final reply, in this order, one line per item, same labels as the internal record:
  1. `READ:` lines: each file or hunk range, each existing thread, and `drafts/<repo>-<N>.md` — specific names, never "the diff"; each test re-run as `READ: <test> — BASE PASS|FAIL (<reason>) / PR PASS|FAIL (<reason>)`.
  2. `SKIPPED:` lines: what was not read or re-run and why; `SKIPPED: none` if nothing.
  3. `COVERAGE_STATUS: COMPLETE` (only with `SKIPPED: none`) or `COVERAGE_STATUS: PARTIAL — <what was not read or run, and why>`.
  4. `VERDICT: APPROVED`, or `VERDICT: NEEDS_REVISION` followed by `GATE <n> — WRONG STOP | MISSED STOP: <evidence>` and/or `<path>:<line> — DELETE | EDIT: <why> | ADD: <missing finding>`, plus any change to `STOPPED_AT` / `RECOMMENDED`.

  `PARTIAL` never accompanies `APPROVED`: a draft that could not be fully checked is `VERDICT: NEEDS_REVISION` with the gap as its first finding (`COVERAGE — <what was skipped>`). An `APPROVED` with `PARTIAL`, or with no `COVERAGE_STATUS` line, is malformed and is not an approval.
