---
name: flutter-pr-review
description: "Gate-based protocol for reviewing a Flutter pull request (flutter/flutter). Use when drafting a code review of a PR or verifying someone else's draft: CLA, right-repository, CI triage, AI-contribution-policy, and test-requirement gates; regression-test verification on BASE vs PR; design review (API surface, producer vs consumer fixes, breaking changes, architecture); style nits; and the exact review payload/body format."
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

## Context & bounded-read discipline

Treat `scripts/pr_tool.py` (`python3 <skill_dir>/scripts/pr_tool.py <subcommand> ...`) as a **pre-verified black-box CLI** — run it directly and **never** read or inspect `scripts/*.py` into context in the main session or in subagents (run `<subcommand> -h` only if a flag is unclear). Run commands from the PR worktree, or pass `--worktree <path>` (plus `--repo <owner/repo>` if not inferable from `origin`) on any subcommand:

- **Staged preflight first:** Run `pr_tool.py preflight --pr <N> [--repo <owner/repo>] [--worktree <path>]` once at the start. It spools raw PR metadata, diff, review threads, and linked issues under `/tmp/flutter-pr-review/` and prints deterministic Gate 1–5 signals, substantive diff weights (`prod = 1.0x`, `test = 0.25x`, `comments/generated = 0x`), and compacted linked issues. Evaluate Gates 1–5 in order before reading worktree source/test files or the style guide (inspecting `pr_tool.py diff` only if needed for Gate 3 #2 or Gate 4). On any Gate 1–5 STOP, write the draft, run `pr_tool.py validate`, and stop.
- **Unanchored first pass:** Inspect changed hunks via `pr_tool.py diff --pr <N> [--file <path> ...] [--exclude-tests] [--chunk <K> | --list-chunks]` (`≤700`-line / `≤35 KB` pages annotated with `[R<line>]` right-side line numbers) and trace the changed code **before** running `pr_tool.py threads --pr <N>`, so `gemini-code-assist` and earlier threads do not anchor Gate 3 or Gate 6; then read `threads` to deduplicate and check re-review items.
- **Semantic read bounds (Gate 6):**
  - **Production files `≤1,000` lines:** Read in full to keep complete class/subsystem context.
  - **Production files `>1,000` lines:** Run `pr_tool.py outline --pr <N> <file> --for-diff [--symbol <name> ...]` first and read the returned class docs/fields, lifecycle methods, and peer members referencing touched state — plus any additional ranges needed when following invariants (no hard line cap).
  - **Test files (`*_test.dart`):** Do not read multi-thousand-line test files end-to-end; run `pr_tool.py outline --pr <N> <test_file> --regression-style [--symbol <name> ...]` to locate shared top-of-file helpers, existing regression-comment phrasing, and tests referencing touched symbols, then read targeted ranges. Run local tests with `flutter test --reporter=compact --no-pub`.
- **Scout subagents (broad read-only scans only):** Keep core subsystem reading, producer-vs-consumer judgment, and failing-test proofs in the main session. Delegate read-only scans to a subagent (instructing it not to read `<skill_dir>/scripts/*.py` either) — returning `READ:` (`path:start-end` ranges or style `#anchor`s inspected, merged into `drafts/<repo>-<N>.md`) and `FINDINGS:` (hotspots + `≤5`-line quotes) — when:
  - Repo-wide caller/subclass/override discovery (`rg`) spans **>5 non-diff files** (main session reads the 2–3 flagged hotspot ranges directly if subtle).
  - Substantive diff exceeds **250 weighted lines** or **>5 production files** (initial file/test responsibility map).
  - **Gate 7 style sweep** when substantive diff exceeds **40 weighted lines** or spans >2 style-guide sections.

## Gate 1 — CLA

- **Check:** the author has signed the Google CLA.
- **Signal:** `statusCheckRollup` entry named `cla/google` is `SUCCESS`.
- **On fail:** STOP, `RECOMMENDED: blocked`.
- **Comment must say:** thanks; we cannot review until the CLA is signed (https://cla.developers.google.com/); once it is, the check turns green and review resumes.

## Gate 2 — Right repository

- **Check:** the change is not editing the framework's frozen/deprecated Material or Cupertino libraries or tests.
- **Signal:** changed paths in `flutter/flutter` under `packages/flutter/lib/src/{material,cupertino}/**`, `packages/flutter/lib/{material,cupertino}.dart`, or `packages/flutter/test/{material,cupertino}/**`.
- **Only Material/Cupertino paths changed:** STOP, `RECOMMENDED: close`.
  - **Comment must say:** these libraries are frozen/deprecated in `flutter/flutter` and now live in `flutter/packages` as `packages/material_ui` / `packages/cupertino_ui`; please re-submit the same change there; we recommend closing this PR.
- **Also changes `widgets` (or other non-deprecated framework layers/tests):** STOP, `RECOMMENDED: request changes`.
  - **Comment must say:** Material and Cupertino are frozen/deprecated in `flutter/flutter` (now `packages/material_ui` / `packages/cupertino_ui` in `flutter/packages`), while `widgets` and lower framework layers stay in `flutter/flutter`. Suggest the author first determine whether the Material/Cupertino changes are actually needed for this change (and drop them if not); if they are needed, split the PR so the `widgets`/framework changes stay in this PR and the Material/Cupertino parts go to `packages/material_ui` / `packages/cupertino_ui` in `flutter/packages`. Review resumes once the Material/Cupertino changes are removed here.

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
  - **Checks reported:** for every failing check (conclusion `failure`, `timed_out`, `cancelled`, `startup_failure` or `action_required`; status `error` or `failure`) other than `cla/google` and the not-a-signal set below (`preflight`'s `actionable_failing_checks`), find out **what** failed via `pr_tool.py ci-log --pr <N> <gh_run_id_or_detailsUrl>` (extracts bounded failure windows from GitHub Actions `--log-failed` or LUCI Buildbucket; full log saved in `/tmp/flutter-pr-review/` if deeper context is needed) and classify it from the log. Do not re-run it locally.
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
- **Author's regression test.** Exists, FAILS on BASE, PASSES on the PR, and links the full issue URL (`https://github.com/<owner>/<repo>/issues/<id>`, not bare `#<id>`) in a regression comment — accept `// Regression test for <url>`, `// This is a regression test for <url>` (with or without trailing `.`, above the test or at the top of its body), or whatever full-URL convention the file already uses (do not nitpick between valid forms, here or in Gate 7). When asking to add or fix one, match the phrasing and placement already used in that file if present. Anything missing → NOTE with an inline comment on the test; continue.
- **Test layering (flutter/flutter).** Tests for `lib/src/widgets/` import only `package:flutter/widgets.dart` (+ test packages) and use `WidgetsApp`/`TestWidgetsApp`, never `material.dart`/`cupertino.dart`; material/cupertino tests may use `MaterialApp`/`CupertinoApp`.
- **Prove every bug you claim.** A comment asserting the PR introduces a bug must come with a failing test you wrote and ran (quote the failure). If you cannot make it fail, downgrade to a question or drop it.
- **New API.** Is each new public member necessary to fix the bug? If not, ask to remove it (inline).
- **Producer vs consumer.** Does the fix happen where the bad state is produced, or does it add guards in one consumer? Guards-only fixes leave the next consumer to hit the same bug — say so and ask for the producer-level fix.
- **Breaking changes.** Changed signatures, types, long-standing assumptions or invariants: if not necessary, ask to revert; if necessary, point to the breaking-change policy and the migration-guide requirement.
- **Architecture.** Sound within its subsystem — invariants, lifecycle, layering (read callers, adjacent code, existing tests; not just the hunks).
- **On a re-review:** comment only on what changed or is still open; explicitly acknowledge what the author addressed.

## Gate 7 — Style

Check the diff against `docs/contributing/Style-guide-for-Flutter-repo.md` (worktree copy) using `pr_tool.py style --toc` and `pr_tool.py style --section <anchor>` rather than reading the entire style guide into context. Each finding is an inline comment prefixed `style nit:` linking `https://github.com/flutter/flutter/blob/master/docs/contributing/Style-guide-for-Flutter-repo.md#<anchor>`, where `<anchor>` is verified by `pr_tool.py style` against the worktree copy (lowercase, inline formatting/punctuation stripped, spaces → `-`); if no dedicated heading exists, link the closest parent heading. No formatter-level nits (`dart format` catches those).

## The review draft

**Payload** (`drafts/<repo>-<N>.json`, exactly what the GitHub reviews API accepts):

```json
{"commit_id": "<HEAD_SHA>", "body": "...", "comments": [{"path": "...", "line": 123, "side": "RIGHT", "body": "..."}]}
```

- No `event` key. Optional `start_line`/`start_side` for ranges. Every `path`/`line` must be a `[R<line>]` right-side line present in `pr_tool.py diff`. Must pass `pr_tool.py validate --pr <N> drafts/<repo>-<N>.json drafts/<repo>-<N>.md` (and `python3 -m json.tool`) with zero errors before finishing.
- The payload is author-facing only: nothing in `body` or `comments` is reviewer bookkeeping, and no local `file://` links.

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
