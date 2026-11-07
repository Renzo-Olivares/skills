#!/usr/bin/env python3
"""Unit tests for flutter-pr-review/scripts/pr_tool.py."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import pr_tool  # noqa: E402


SAMPLE_DIFF = """diff --git a/packages/flutter/lib/src/widgets/foo.dart b/packages/flutter/lib/src/widgets/foo.dart
index 1111111..2222222 100644
--- a/packages/flutter/lib/src/widgets/foo.dart
+++ b/packages/flutter/lib/src/widgets/foo.dart
@@ -10,5 +10,8 @@ class Foo extends StatefulWidget {
   final int count;
-  void oldHelper() {}
+  // Just a comment (0x weight)
+  void newHelper() {
+    _updateCount();
+  }
   @override
diff --git a/packages/flutter/test/widgets/foo_test.dart b/packages/flutter/test/widgets/foo_test.dart
index 3333333..4444444 100644
--- a/packages/flutter/test/widgets/foo_test.dart
+++ b/packages/flutter/test/widgets/foo_test.dart
@@ -1,3 +1,7 @@
+import 'package:flutter_test/flutter_test.dart';
+
 void main() {
+  testWidgets('works', (tester) async {
+    expect(1, 1);
+  });
 }
diff --git a/packages/flutter/lib/src/widgets/foo.g.dart b/packages/flutter/lib/src/widgets/foo.g.dart
index 5555555..6666666 100644
--- a/packages/flutter/lib/src/widgets/foo.g.dart
+++ b/packages/flutter/lib/src/widgets/foo.g.dart
@@ -1,2 +1,4 @@
+// GENERATED
+final int generatedVal = 42;
"""


class DiffAndGatesTest(unittest.TestCase):
    def test_parse_unified_diff_and_weights(self) -> None:
        parsed = pr_tool.parse_unified_diff(SAMPLE_DIFF)
        self.assertEqual(len(parsed), 3)

        prod = parsed[0]
        self.assertEqual(prod["path"], "packages/flutter/lib/src/widgets/foo.dart")
        self.assertEqual(prod["additions"], 4)
        self.assertEqual(prod["deletions"], 1)
        # Context line 10, added 11..14, context line 15 => all valid right-side anchor lines.
        self.assertEqual(prod["right_lines"], [10, 11, 12, 13, 14, 15])
        self.assertEqual(prod["added_right_lines"], [11, 12, 13, 14])
        annotated = "\n".join(prod["hunks"][0]["annotated_lines"])
        self.assertIn("[R   12] +  void newHelper() {", annotated)
        self.assertIn("[     -] -  void oldHelper() {}", annotated)

        weights = pr_tool.compute_diff_weights(parsed)
        # Prod substantive: -oldHelper (1) + 3 code lines (comment excluded) = 4
        # Test substantive: 3 code lines (import & blank excluded) * 0.25 = 0.75 -> rounded 4.8
        # Generated foo.g.dart: 0
        self.assertEqual(weights["prod_substantive_lines"], 4)
        self.assertEqual(weights["test_substantive_lines"], 3)
        self.assertAlmostEqual(weights["weighted_lines"], 4.8)
        self.assertEqual(weights["prod_files_count"], 1)
        self.assertEqual(weights["test_files_count"], 1)

    def test_chunk_annotated_diff_splits_large_hunks(self) -> None:
        big_lines = ["diff --git a/lib/big.dart b/lib/big.dart", "--- a/lib/big.dart", "+++ b/lib/big.dart", "@@ -1,1 +1,900 @@"]
        big_lines.extend(f"+  final int x{i} = {i};" for i in range(900))
        parsed = pr_tool.parse_unified_diff("\n".join(big_lines))
        chunks = pr_tool.chunk_annotated_diff(parsed, max_lines=300, max_bytes=20_000)
        self.assertGreaterEqual(len(chunks), 3)
        for ch in chunks:
            self.assertLessEqual(ch["line_count"], 300)
            self.assertIn("=== lib/big.dart", ch["text"])

    def test_gates_1_to_5_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp)
            (wt / ".github").mkdir()
            (wt / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text(
                "## Pre-launch Checklist\n"
                "- [ ] I read the [Contributor Guide] and followed the process.\n"
                "- [ ] I listed at least one issue that this PR fixes.\n"
                "- [ ] I added new tests to check the change.\n",
                encoding="utf-8",
            )
            pr_meta = {
                "body": "Summary\n\n## Pre-launch Checklist\n- [x] I read the [Contributor Guide] and followed the process.\n",
                "labels": [{"name": "a: typography"}],
                "statusCheckRollup": [
                    {"name": "cla/google", "conclusion": "SUCCESS"},
                    {"name": "Google testing", "status": "IN_PROGRESS", "conclusion": ""},
                    {"name": "Merge Queue Guard", "status": "QUEUED", "conclusion": ""},
                    {"name": "flutter-gold", "state": "PENDING"},
                    {"name": "tree-status", "state": "FAILURE"},
                    {
                        "name": "Linux framework_tests_misc",
                        "conclusion": "FAILURE",
                        "detailsUrl": "https://ci.chromium.org/b/8765432109876543210",
                    },
                    {
                        "name": "analyze",
                        "conclusion": "FAILURE",
                        "detailsUrl": "https://github.com/flutter/flutter/actions/runs/99887766/job/123",
                    },
                    {"name": "Windows build", "conclusion": "SUCCESS"},
                ],
            }
            changed = [
                "packages/flutter/lib/src/material/button.dart",
                "packages/flutter/lib/src/widgets/basic.dart",
                "fix.plan.md",
            ]
            res = pr_tool.evaluate_gates_1_to_5(
                repo="flutter/flutter",
                pr_meta=pr_meta,
                changed_paths=changed,
                worktree=wt,
            )
            self.assertTrue(res["gate1_cla"]["passed"])
            self.assertEqual(res["gate2_repo"]["status"], "mixed_with_framework")
            self.assertEqual(res["gate3_process_ai"]["stray_ai_files"], ["fix.plan.md"])
            self.assertEqual(len(res["gate3_process_ai"]["missing_template_items"]), 2)
            self.assertTrue(res["gate4_tests"]["needs_exemption_or_tests_check"])
            self.assertFalse(res["gate5_ci"]["has_cicd_label"])
            self.assertTrue(res["gate5_ci"]["no_cicd_silent_stop"])
            # Non-signal checks (including failing `tree-status` and pending `Google testing`) must be stripped!
            failing = res["gate5_ci"]["actionable_failing_checks"]
            self.assertEqual([f["name"] for f in failing], ["Linux framework_tests_misc", "analyze"])
            self.assertEqual(failing[1]["gh_run_id"], "99887766")

    def test_gate2_only_material_cupertino_vs_packages_repo(self) -> None:
        only_mat = pr_tool.evaluate_gates_1_to_5(
            repo="flutter/flutter",
            pr_meta={"labels": [{"name": "CICD"}], "statusCheckRollup": [{"name": "cla/google", "state": "SUCCESS"}]},
            changed_paths=["packages/flutter/lib/material.dart", "packages/flutter/test/material/ink_test.dart"],
            worktree=None,
        )
        self.assertEqual(only_mat["gate2_repo"]["status"], "only_material_cupertino")

        in_packages = pr_tool.evaluate_gates_1_to_5(
            repo="flutter/packages",
            pr_meta={"labels": [{"name": "CICD"}], "statusCheckRollup": []},
            changed_paths=["packages/material_ui/lib/material.dart"],
            worktree=None,
        )
        self.assertEqual(in_packages["gate2_repo"]["status"], "ok")

    def test_format_compacted_threads_and_details_truncation(self) -> None:
        gql = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviews": {
                            "nodes": [
                                {"author": {"login": "my-review-bot"}, "state": "CHANGES_REQUESTED", "submittedAt": "2026-01-01", "body": "Prior bot summary"}
                            ]
                        },
                        "comments": {
                            "nodes": [
                                {"author": {"login": "google-cla[bot]"}, "createdAt": "2026-01-01", "body": "CLA noise"},
                                {"author": {"login": "contributor"}, "createdAt": "2026-01-02", "body": "Fixed!<details><summary>flutter doctor</summary>" + ("x" * 900) + "</details>"},
                            ]
                        },
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "isResolved": True,
                                    "isOutdated": True,
                                    "path": "lib/a.dart",
                                    "line": 10,
                                    "comments": {"nodes": [{"author": {"login": "human1"}, "body": "Old nit resolved"}]},
                                },
                                {
                                    "isResolved": True,
                                    "isOutdated": False,
                                    "path": "lib/b.dart",
                                    "line": 22,
                                    "comments": {"nodes": [{"author": {"login": "gemini-code-assist[bot]"}, "body": "Check nullability"}]},
                                },
                            ]
                        },
                    }
                }
            }
        }
        md, stats = pr_tool.format_compacted_threads(gql, agent_login="my-review-bot")
        self.assertTrue(stats["has_prior_self_review"])
        self.assertIn("[PRIOR SELF REVIEW]", md)
        self.assertNotIn("CLA noise", md)
        self.assertIn("[collapsed <details>: flutter doctor", md)
        # gemini-code-assist kept in full even when resolved; human resolved thread collapsed to 1 line.
        self.assertIn("gemini-code-assist[bot]", md)
        self.assertIn("- [RESOLVED] `lib/a.dart:10` (human1): Old nit resolved", md)



SAMPLE_DART_SRC = """// Top file comment with unbalanced {
import 'package:flutter/widgets.dart';

/// Controls selection overlay state.
class _DemoState extends State<DemoWidget> {
  /// Active overlay handle.
  OverlayEntry? _selectionOverlay;
  int _counter = 0;

  String get label => 'value {with brace} and ${_counter + 1}';

  @override
  void initState() {
    super.initState();
    _selectionOverlay = null;
  }

  @override
  void didUpdateWidget(
    covariant DemoWidget oldWidget,
  ) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.enabled != widget.enabled) {
      _selectionOverlay?.remove();
      _selectionOverlay = null;
    }
  }

  void _unrelatedHelper() {
    /* nested /* block { comment */ still comment } */
    final rawStr = r'raw {brace}';
    assert(rawStr.isNotEmpty);
  }

  void _rebuildOverlay() {
    _selectionOverlay?.markNeedsBuild();
  }

  @override
  void dispose() {
    _selectionOverlay?.remove();
    super.dispose();
  }
}

class CoupledController {
  void ping() {}
}
"""

SAMPLE_TEST_DART = """import 'package:flutter_test/flutter_test.dart';

Widget buildHarness(Widget child) => child;

void main() {
  // Regression test for https://github.com/flutter/flutter/issues/12345.
  testWidgets('DemoWidget disposes overlay cleanly', (WidgetTester tester) async {
    expect(1, 1);
  });

  testWidgets('Unrelated test case', (WidgetTester tester) async {
    expect(2, 2);
  });
}
"""


class OutlineStyleAndValidateTest(unittest.TestCase):
    def test_extract_failure_windows(self) -> None:
        noise = [f"00:01 +{i}: passing test {i}" for i in range(200)]
        fail_block = [
            "══╡ EXCEPTION CAUGHT BY FLUTTER TEST FRAMEWORK ╞═════════════════",
            "The following TestFailure was thrown running a test:",
            "  Expected: <true>",
            "    Actual: <false>",
        ]
        log_text = "\n".join(noise[:100] + fail_block + noise[100:])
        excerpt = pr_tool.extract_failure_windows(log_text, max_lines=60)
        self.assertIn("EXCEPTION CAUGHT BY FLUTTER TEST FRAMEWORK", excerpt)
        self.assertLessEqual(len(excerpt.splitlines()), 62)

    def test_style_guide_slugs_and_fences(self) -> None:
        md = (
            "# Style guide for Flutter repo\n\n"
            "## Naming\n"
            "```dart\n"
            "# Not a markdown heading inside code fence\n"
            "```\n"
            "### Avoid `as` casts\n"
            "Use `is` instead.\n"
            "### [Link Title](https://example.com) & Punctuation!\n"
            "### Avoid `as` casts\n"
        )
        sections = pr_tool.parse_style_guide(md)
        anchors = [s["anchor"] for s in sections]
        self.assertEqual(
            anchors,
            [
                "style-guide-for-flutter-repo",
                "naming",
                "avoid-as-casts",
                "link-title--punctuation",
                "avoid-as-casts-1",
            ],
        )

    def test_dart_lexer_outline_and_for_diff_slice(self) -> None:
        outline = pr_tool.parse_dart_outline(SAMPLE_DART_SRC)
        decls = outline["declarations"]
        self.assertEqual([d["name"] for d in decls], ["_DemoState", "CoupledController"])

        demo = decls[0]
        member_map = {m["name"]: m for m in demo["members"]}
        self.assertEqual(member_map["_selectionOverlay"]["kind"], "field")
        self.assertEqual(member_map["label"]["kind"], "getter")
        self.assertEqual(member_map["didUpdateWidget"]["kind"], "method")
        self.assertEqual(member_map["_unrelatedHelper"]["kind"], "method")
        self.assertEqual(member_map["_rebuildOverlay"]["kind"], "method")
        self.assertEqual(member_map["dispose"]["kind"], "method")

        # Simulate diff touching `didUpdateWidget` line referencing `_selectionOverlay`.
        did_update_ln = member_map["didUpdateWidget"]["start_line"] + 3
        slice_md = pr_tool.slice_dart_for_diff(
            outline,
            touched_lines={did_update_ln},
            changed_code_lines=["_selectionOverlay?.remove();"],
        )
        # Must include fields, touched didUpdateWidget, lifecycle methods (initState, dispose),
        # peer `_rebuildOverlay` (references `_selectionOverlay`), and coupled `CoupledController`,
        # while NOT listing `_unrelatedHelper` under state peers!
        self.assertIn("All field declarations", slice_md)
        self.assertIn("`didUpdateWidget`", slice_md)
        self.assertIn("`initState`", slice_md)
        self.assertIn("`dispose`", slice_md)
        self.assertIn("`_rebuildOverlay`", slice_md)
        self.assertNotIn("`_unrelatedHelper`", slice_md)
        self.assertIn("`CoupledController`", slice_md)

    def test_extract_test_regression_style(self) -> None:
        report = pr_tool.extract_test_regression_style(SAMPLE_TEST_DART, symbols={"DemoWidget"})
        self.assertIn("Imports & shared top-of-file test helpers: [L1-L4]", report)
        self.assertIn("https://github.com/flutter/flutter/issues/12345", report)
        self.assertIn("`DemoWidget disposes overlay cleanly`", report)
        self.assertNotIn("`Unrelated test case`", report)

    def test_validate_review_files_catches_all_violations_and_passes_clean(self) -> None:
        parsed_diff = pr_tool.parse_unified_diff(SAMPLE_DIFF)
        valid_anchors = {"avoid-as-casts", "prefer-final"}

        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            jpath = tdir / "flutter-100.json"
            mpath = tdir / "flutter-100.md"

            # 1. Invalid payload with multiple leaks/errors
            jpath.write_text(
                json.dumps({
                    "commit_id": "wrongsha",
                    "event": "REQUEST_CHANGES",
                    "body": "See [foo](file:///Users/me/foo.dart). Google testing is pending. STOPPED_AT: none",
                    "comments": [
                        {
                            "path": "packages/flutter/lib/src/widgets/foo.dart",
                            "line": 999,
                            "side": "RIGHT",
                            "body": (
                                "style nit: On line 12 fix this per "
                                "https://github.com/flutter/flutter/blob/master/docs/contributing/Style-guide-for-Flutter-repo.md#nonexistent "
                                "and add // Regression test for #555"
                            ),
                        }
                    ],
                }),
                encoding="utf-8",
            )
            mpath.write_text(
                "STOPPED_AT: none\n"
                "RECOMMENDED: approve — looks good\n"
                "READ: the diff\n"
                "SKIPPED: flutter-gold\n",
                encoding="utf-8",
            )
            errs = pr_tool.validate_review_files(
                draft_json_path=jpath,
                draft_md_path=mpath,
                parsed_diff=parsed_diff,
                valid_style_anchors=valid_anchors,
                expected_head_sha="abc1234",
                agent_login="bot-reviewer",
                has_cicd_label=True,
                raw_changed_lines=250,
            )
            joined = "\n".join(e["error"] for e in errs)
            self.assertIn("forbidden `event` key", joined)
            self.assertIn("does not match PR HEAD SHA", joined)
            self.assertIn("missing the exact required AI-reviewer footer", joined)
            self.assertIn("not a right-side diff hunk line", joined)
            self.assertIn("mentions a line number in prose", joined)
            self.assertIn("local `file://` URI", joined)
            self.assertIn("leaks internal bookkeeping", joined)
            self.assertIn("bare `#<id>` in regression-test", joined)
            self.assertIn("non-signal CI check `Google testing`", joined)
            self.assertIn("anchor `#nonexistent` does not exist", joined)
            self.assertIn("`SKIPPED:` mentions non-signal check `flutter-gold`", joined)

            # 2. Valid draft passes cleanly
            footer = "— bot-reviewer is an AI reviewer; a human maintainer makes the final call (see this account's profile for who to contact)."
            jpath.write_text(
                json.dumps({
                    "commit_id": "abc1234",
                    "body": f"Thanks for fixing `Foo.newHelper`!\n\n{footer}",
                    "comments": [
                        {
                            "path": "packages/flutter/lib/src/widgets/foo.dart",
                            "line": 12,
                            "side": "RIGHT",
                            "body": (
                                "style nit: Prefer `is` over `as` per "
                                "https://github.com/flutter/flutter/blob/master/docs/contributing/Style-guide-for-Flutter-repo.md#avoid-as-casts."
                            ),
                        }
                    ],
                }),
                encoding="utf-8",
            )
            mpath.write_text(
                "STOPPED_AT: none\n"
                "RECOMMENDED: approve — sound fix with regression test\n"
                "READ: packages/flutter/lib/src/widgets/foo.dart (L1-L80), packages/flutter/test/widgets/foo_test.dart — BASE FAIL / PR PASS\n"
                "SKIPPED: none\n",
                encoding="utf-8",
            )
            clean_errs = pr_tool.validate_review_files(
                draft_json_path=jpath,
                draft_md_path=mpath,
                parsed_diff=parsed_diff,
                valid_style_anchors=valid_anchors,
                expected_head_sha="abc1234",
                agent_login="bot-reviewer",
                has_cicd_label=True,
                raw_changed_lines=250,
            )
            self.assertEqual(clean_errs, [])

            # 3. Gate 5 no-CICD silent STOP passes with empty body and no footer
            jpath.write_text(
                json.dumps({"commit_id": "abc1234", "body": "", "comments": []}),
                encoding="utf-8",
            )
            mpath.write_text(
                "STOPPED_AT: Gate 5\n"
                "RECOMMENDED: blocked — no CICD label on PR\n"
                "READ: preflight metadata\n"
                "SKIPPED: none\n",
                encoding="utf-8",
            )
            nocicd_errs = pr_tool.validate_review_files(
                draft_json_path=jpath,
                draft_md_path=mpath,
                parsed_diff=parsed_diff,
                valid_style_anchors=valid_anchors,
                expected_head_sha="abc1234",
                agent_login="bot-reviewer",
                has_cicd_label=False,
                raw_changed_lines=10,
            )
            self.assertEqual(nocicd_errs, [])


class CliEndToEndTest(unittest.TestCase):
    def test_nested_string_interpolation_and_constructors(self) -> None:
        tricky_dart = (
            "mixin _HelperMixin on State<DemoWidget> {\n"
            "  String fmt(String s) => 'outer ${s.replaceAll(\"{bad\", \"ok\")} end';\n"
            "}\n"
            "class BoxWidget {\n"
            "  final int x;\n"
            "  BoxWidget({required int val}) : x = val {\n"
            "    assert(x >= 0);\n"
            "  }\n"
            "  BoxWidget.zero() : x = 0;\n"
            "}\n"
        )
        outline = pr_tool.parse_dart_outline(tricky_dart)
        self.assertEqual([d["name"] for d in outline["declarations"]], ["_HelperMixin", "BoxWidget"])
        box_members = {m["name"]: m["kind"] for m in outline["declarations"][1]["members"]}
        self.assertEqual(box_members["BoxWidget"], "constructor")
        self.assertEqual(box_members["BoxWidget.zero"], "constructor")
        self.assertEqual(box_members["x"], "field")

    def test_cli_subcommands_end_to_end(self) -> None:
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wt = root / "worktree"
            spool_root = root / "spool"
            wt.mkdir()
            (wt / "docs" / "contributing").mkdir(parents=True)
            style_file = wt / "docs" / "contributing" / "Style-guide-for-Flutter-repo.md"
            style_file.write_text("# Style Guide\n\n## Avoid `as`\nPrefer `is`.\n", encoding="utf-8")

            dart_dir = wt / "packages" / "flutter" / "lib" / "src" / "widgets"
            dart_dir.mkdir(parents=True)
            (dart_dir / "foo.dart").write_text(SAMPLE_DART_SRC, encoding="utf-8")

            spool_dir = pr_tool.resolve_spool_dir("flutter/flutter", 42, spool_root)
            (spool_dir / "diff.patch").write_text(SAMPLE_DIFF, encoding="utf-8")
            (spool_dir / "threads.md").write_text("# Existing PR Threads & Reviews\nNone.\n", encoding="utf-8")
            (spool_dir / "state.json").write_text(
                json.dumps({
                    "head_sha": "deadbeef",
                    "agent_login": "ai-bot",
                    "gates": {"gate5_ci": {"has_cicd_label": True}},
                    "diff_weights": {"raw_changed_lines": 12},
                }),
                encoding="utf-8",
            )

            # 1. diff
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = pr_tool.main([
                    "diff", "--pr", "42", "--repo", "flutter/flutter",
                    "--worktree", str(wt), "--spool-root", str(spool_root),
                    "--exclude-tests",
                ])
            self.assertEqual(rc, 0)
            self.assertIn("[R   12]", buf.getvalue())
            self.assertNotIn("foo_test.dart", buf.getvalue())

            # 2. outline --for-diff
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = pr_tool.main([
                    "outline", "--pr", "42", "--repo", "flutter/flutter",
                    "--worktree", str(wt), "--spool-root", str(spool_root),
                    "packages/flutter/lib/src/widgets/foo.dart", "--for-diff",
                ])
            self.assertEqual(rc, 0)
            self.assertIn("`_DemoState`", buf.getvalue())

            # 3. style --section
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = pr_tool.main(["style", "--worktree", str(wt), "--section", "avoid-as"])
            self.assertEqual(rc, 0)
            self.assertIn("#avoid-as", buf.getvalue())

            # 4. validate
            draft_json = root / "flutter-42.json"
            draft_md = root / "flutter-42.md"
            footer = "— ai-bot is an AI reviewer; a human maintainer makes the final call (see this account's profile for who to contact)."
            draft_json.write_text(
                json.dumps({
                    "commit_id": "deadbeef",
                    "body": f"Thanks!\n\n{footer}",
                    "comments": [],
                }),
                encoding="utf-8",
            )
            draft_md.write_text(
                "STOPPED_AT: none\nRECOMMENDED: approve — clean\nREAD: foo.dart\nSKIPPED: none\n",
                encoding="utf-8",
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = pr_tool.main([
                    "validate", "--pr", "42", "--repo", "flutter/flutter",
                    "--worktree", str(wt), "--spool-root", str(spool_root),
                    str(draft_json), str(draft_md),
                ])
            self.assertEqual(rc, 0)
            self.assertIn("VALID:", buf.getvalue())

    def test_noisy_bot_suffix_and_graphql_pagination_and_prose_lines(self) -> None:
        from unittest import mock

        # 1. `removesuffix("[bot]")` filters fluttergithubbot without mangling trailing b/o/t chars.
        self.assertTrue(pr_tool.is_noisy_bot_login("fluttergithubbot"))
        self.assertTrue(pr_tool.is_noisy_bot_login("fluttergithubbot[bot]"))
        self.assertTrue(pr_tool.is_noisy_bot_login("google-cla[bot]"))
        self.assertFalse(pr_tool.is_noisy_bot_login("fluttergithub"))
        self.assertFalse(pr_tool.is_noisy_bot_login("google-clab"))
        self.assertFalse(pr_tool.is_noisy_bot_login("gemini-code-assist[bot]"))

        # 2. Multi-page `fetch_pr_threads_graphql` cursor pagination merges all thread pages.
        page1 = json.dumps({
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviews": {"nodes": []},
                        "comments": {"nodes": [{"author": {"login": "fluttergithubbot"}, "createdAt": "2026-01-01", "body": "bot ping"}]},
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "CUR_1"},
                            "nodes": [
                                {
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "path": "lib/p1.dart",
                                    "line": 5,
                                    "comments": {"nodes": [{"author": {"login": "rev1"}, "body": "Page 1 thread"}]},
                                }
                            ],
                        },
                    }
                }
            }
        })
        page2 = json.dumps({
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "path": "lib/p2.dart",
                                    "line": 9,
                                    "comments": {"nodes": [{"author": {"login": "rev2"}, "body": "Page 2 thread"}]},
                                }
                            ],
                        }
                    }
                }
            }
        })
        with mock.patch.object(pr_tool, "run_gh", side_effect=[page1, page2]) as mocked_gh:
            merged = pr_tool.fetch_pr_threads_graphql("flutter", "flutter", 99)
            self.assertEqual(mocked_gh.call_count, 2)
        md, stats = pr_tool.format_compacted_threads(merged, agent_login="ai-bot")
        self.assertEqual(stats["open_inline_threads"], 2)
        self.assertEqual(stats["conversation_comments_kept"], 0)
        self.assertIn("Page 1 thread", md)
        self.assertIn("Page 2 thread", md)
        self.assertNotIn("bot ping", md)

        # 3. Prose line-number regex allows text-layout prose, inline code, and GitHub `#L<N>` URLs,
        # while catching `[R12]`, `L42`, and `lines 10-14` citations.
        parsed_diff = pr_tool.parse_unified_diff(SAMPLE_DIFF)
        footer = "— ai-bot is an AI reviewer; a human maintainer makes the final call (see this account's profile for who to contact)."
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jpath = root / "flutter-99.json"
            mpath = root / "flutter-99.md"
            mpath.write_text(
                "STOPPED_AT: none\nRECOMMENDED: comment — nit\nREAD: foo.dart\nSKIPPED: none\n",
                encoding="utf-8",
            )
            jpath.write_text(
                json.dumps({
                    "commit_id": "abc",
                    "body": f"Thanks!\n\n{footer}",
                    "comments": [
                        {
                            "path": "packages/flutter/lib/src/widgets/foo.dart",
                            "line": 12,
                            "side": "RIGHT",
                            "body": (
                                "nit: When `maxLines` is 2, text wraps onto lines 2 and 3 of the paragraph "
                                "(like `'error on line 1'` or "
                                "https://github.com/flutter/flutter/blob/master/packages/flutter/lib/src/widgets/text.dart#L420)."
                            ),
                        }
                    ],
                }),
                encoding="utf-8",
            )
            self.assertEqual(
                pr_tool.validate_review_files(
                    draft_json_path=jpath,
                    draft_md_path=mpath,
                    parsed_diff=parsed_diff,
                    valid_style_anchors=None,
                    expected_head_sha="abc",
                    agent_login="ai-bot",
                    has_cicd_label=True,
                ),
                [],
            )

            for bad_prose in ("nit: Duplicate of L42.", "nit: Also happens on [R14].", "nit: Covers lines 11-14."):
                jpath.write_text(
                    json.dumps({
                        "commit_id": "abc",
                        "body": f"Thanks!\n\n{footer}",
                        "comments": [
                            {
                                "path": "packages/flutter/lib/src/widgets/foo.dart",
                                "line": 12,
                                "side": "RIGHT",
                                "body": bad_prose,
                            }
                        ],
                    }),
                    encoding="utf-8",
                )
                errs = pr_tool.validate_review_files(
                    draft_json_path=jpath,
                    draft_md_path=mpath,
                    parsed_diff=parsed_diff,
                    valid_style_anchors=None,
                    expected_head_sha="abc",
                    agent_login="ai-bot",
                    has_cicd_label=True,
                )
                self.assertTrue(
                    any("mentions a line number in prose" in e["error"] for e in errs),
                    f"Expected prose line-number error for: {bad_prose}",
                )


class RemoteSessionAndLuciDashboardTest(unittest.TestCase):
    def test_luci_get_build_valid_mask_and_log_priority_and_dedup(self) -> None:
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        captured_requests: list[tuple[str, dict[str, object] | None]] = []

        class _FakeResp:
            def __init__(self, body: bytes) -> None:
                self._buf = io.BytesIO(body)

            def read(self, n: int = -1) -> bytes:
                return self._buf.read(n)

            def __enter__(self) -> _FakeResp:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        def fake_urlopen(req_or_url: object, timeout: int = 30) -> _FakeResp:
            if hasattr(req_or_url, "full_url"):
                url = getattr(req_or_url, "full_url")
                data_bytes = getattr(req_or_url, "data", None)
                parsed_body = json.loads(data_bytes.decode("utf-8")) if data_bytes else None
            else:
                url = str(req_or_url)
                parsed_body = None
            captured_requests.append((url, parsed_body))

            if url.endswith("/buildbucket.v2.Builds/GetBuild"):
                # Enforce Buildbucket's repeated-field mask rule so `steps.name` would fail just like prod!
                mask_fields = ((parsed_body or {}).get("mask") or {}).get("fields", "")  # type: ignore[union-attr]
                if "steps." in str(mask_fields):
                    raise AssertionError(f"Invalid Buildbucket repeated-field mask: {mask_fields}")
                payload = ")]}'\n" + json.dumps({
                    "id": "8665814997375925777",
                    "builder": {
                        "project": "flutter",
                        "bucket": "try",
                        "builder": "Windows framework_tests_libraries",
                    },
                    "status": "FAILURE",
                    "summaryMarkdown": "Shared summary block: 1 test failed.",
                    "steps": [
                        {
                            "name": "parent_wrapper",
                            "status": "FAILURE",
                        },
                        {
                            "name": "parent_wrapper|run framework_tests",
                            "status": "FAILURE",
                            "summaryMarkdown": "Shared summary block: 1 test failed.",
                            "logs": [
                                {
                                    "name": "$execution details",
                                    "viewUrl": "https://logs.chromium.org/logs/flutter/b/1/+/exec",
                                },
                                {
                                    "name": "stderr",
                                    "viewUrl": "https://logs.chromium.org/logs/flutter/b/1/+/stderr",
                                },
                                {
                                    "name": "stdout",
                                    "viewUrl": "https://logs.chromium.org/logs/flutter/b/1/+/stdout",
                                },
                            ],
                        },
                    ],
                })
                return _FakeResp(payload.encode("utf-8"))
            if "stdout?format=raw" in url:
                return _FakeResp(
                    b"00:01 +10: passing\n"
                    b"00:02 +10 -1: test/widgets/foo_test.dart: Foo regression [E]\n"
                    b"  Expected: <42>\n"
                    b"    Actual: <0>\n"
                )
            if "stderr?format=raw" in url:
                return _FakeResp(b"minor warning without failure\n")
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool"
            empty_wt = Path(tmp) / "home_antigravity"
            empty_wt.mkdir()
            buf = io.StringIO()
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), redirect_stdout(buf):
                rc = pr_tool.main([
                    "ci-log",
                    "--worktree",
                    str(empty_wt),
                    "--spool-root",
                    str(spool),
                    "https://ci.chromium.org/b/8665814997375925777",
                ])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("# LUCI Build 8665814997375925777 (Windows framework_tests_libraries) — status: FAILURE", out)
            self.assertIn("Foo regression [E]", out)
            # Duplicate step summaryMarkdown identical to build summaryMarkdown must appear only once.
            self.assertEqual(out.count("Shared summary block: 1 test failed."), 1)
            # stdout must be prioritized ahead of alphabetically-earlier $execution details and stderr.
            fetched_urls = [u for u, _ in captured_requests]
            self.assertIn("https://logs.chromium.org/logs/flutter/b/1/+/stdout?format=raw", fetched_urls)
            self.assertNotIn("https://logs.chromium.org/logs/flutter/b/1/+/stderr?format=raw", fetched_urls)

    def test_flutter_dashboard_presubmit_url_and_searchbuilds_fallback(self) -> None:
        import io
        import urllib.error
        from contextlib import redirect_stdout
        from unittest import mock

        dash_url = "https://flutter-dashboard.appspot.com/#/presubmit?repo=flutter&sha=202cddec515bb11706f87037dc65966490e9c35f"
        parsed = pr_tool.parse_flutter_dashboard_url(dash_url)
        self.assertEqual(
            parsed,
            {"owner": "flutter", "repo": "flutter", "sha": "202cddec515bb11706f87037dc65966490e9c35f"},
        )
        self.assertIsNone(pr_tool.parse_flutter_dashboard_url("https://ci.chromium.org/b/8665814997375925777"))

        class _FakeResp:
            def __init__(self, body: bytes) -> None:
                self._buf = io.BytesIO(body)

            def read(self, n: int = -1) -> bytes:
                return self._buf.read(n)

            def __enter__(self) -> _FakeResp:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        # 1. Primary path via Cocoon get-presubmit-guard + get-presubmit-job (ignoring Neutral & Succeeded)
        def cocoon_urlopen(req_or_url: object, timeout: int = 30) -> _FakeResp:
            url = getattr(req_or_url, "full_url", str(req_or_url))
            if "/api/public/get-presubmit-guard" in url:
                return _FakeResp(json.dumps({
                    "pr_num": 196159,
                    "check_run_id": 128310221381,
                    "stages": [
                        {
                            "name": "fusion",
                            "jobs": {
                                "Linux analyze": "Succeeded",
                                "Mac tool_tests_general": "Neutral",
                                "Linux_android_emu test": "In Progress",
                                "Mac framework_tests_impeller": "Failed",
                            },
                        }
                    ],
                }).encode("utf-8"))
            if "/api/public/get-presubmit-job" in url:
                return _FakeResp(json.dumps([
                    {"attempt_number": 1, "build_id": "8665811111111111111", "status": "Failed", "summary": "old attempt"},
                    {"attempt_number": 2, "build_id": "8665822222222222222", "status": "Failed", "summary": "latest attempt"},
                ]).encode("utf-8"))
            if url.endswith("/buildbucket.v2.Builds/GetBuild"):
                return _FakeResp((")]}'\n" + json.dumps({
                    "id": "8665822222222222222",
                    "builder": {"builder": "Mac framework_tests_impeller"},
                    "status": "FAILURE",
                    "steps": [
                        {
                            "name": "impeller_tests",
                            "status": "FAILURE",
                            "logs": [{"name": "stdout", "viewUrl": "https://logs.chromium.org/logs/flutter/b/2/+/stdout"}],
                        }
                    ],
                })).encode("utf-8"))
            if "stdout?format=raw" in url:
                return _FakeResp(
                    "══╡ EXCEPTION CAUGHT BY FLUTTER TEST FRAMEWORK ╞══\nImpeller assertion failed\n".encode("utf-8")
                )
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool"
            wt = Path(tmp) / "wt"
            wt.mkdir()
            buf = io.StringIO()
            with mock.patch("urllib.request.urlopen", side_effect=cocoon_urlopen), redirect_stdout(buf):
                rc = pr_tool.main([
                    "ci-log", "--worktree", str(wt), "--spool-root", str(spool), dash_url,
                ])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("1 failing job(s), 1 succeeded, 1 in progress, 1 neutral/skipped", out)
            self.assertIn("`Mac framework_tests_impeller` (Failed, attempt 2/2) — https://ci.chromium.org/b/8665822222222222222", out)
            self.assertNotIn("Mac tool_tests_general", out)
            self.assertIn("Impeller assertion failed", out)

        # 2. Fallback path via Buildbucket SearchBuilds when Cocoon guard fails (filtering retries & bringup)
        def fallback_urlopen(req_or_url: object, timeout: int = 30) -> _FakeResp:
            url = getattr(req_or_url, "full_url", str(req_or_url))
            if "/api/public/get-presubmit-guard" in url:
                raise urllib.error.URLError("dashboard temporarily unreachable")
            if url.endswith("/buildbucket.v2.Builds/SearchBuilds"):
                return _FakeResp((")]}'\n" + json.dumps({
                    "builds": [
                        # Retried flake: attempt 1 failed, attempt 2 succeeded -> must NOT be reported!
                        {
                            "id": "8665800000000000001",
                            "builder": {"builder": "Linux flaky_shard"},
                            "createTime": "2026-11-07T05:00:00Z",
                            "status": "FAILURE",
                        },
                        {
                            "id": "8665800000000000002",
                            "builder": {"builder": "Linux flaky_shard"},
                            "createTime": "2026-11-07T05:30:00Z",
                            "status": "SUCCESS",
                        },
                        # Bringup target: must NOT be reported!
                        {
                            "id": "8665800000000000003",
                            "builder": {"builder": "Linux experimental_bringup"},
                            "createTime": "2026-11-07T05:30:00Z",
                            "status": "FAILURE",
                            "input": {"properties": {"bringup": True}},
                        },
                        # Real failing builder on latest attempt:
                        {
                            "id": "8665800000000000004",
                            "builder": {"builder": "Linux framework_tests_misc"},
                            "createTime": "2026-11-07T05:30:00Z",
                            "status": "FAILURE",
                            "summaryMarkdown": "Some tests failed.\nFailing tests:\n  test/widgets/ scrollable_test.dart",
                        },
                    ]
                })).encode("utf-8"))
            if url.endswith("/buildbucket.v2.Builds/GetBuild"):
                return _FakeResp((")]}'\n" + json.dumps({
                    "id": "8665800000000000004",
                    "builder": {"builder": "Linux framework_tests_misc"},
                    "status": "FAILURE",
                    "summaryMarkdown": "Some tests failed.\nFailing tests:\n  test/widgets/scrollable_test.dart",
                    "steps": [],
                })).encode("utf-8"))
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool2"
            wt = Path(tmp) / "wt2"
            wt.mkdir()
            buf = io.StringIO()
            with mock.patch("urllib.request.urlopen", side_effect=fallback_urlopen), redirect_stdout(buf):
                rc = pr_tool.main([
                    "ci-log", "--worktree", str(wt), "--spool-root", str(spool), dash_url,
                ])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("Linux framework_tests_misc", out)
            self.assertIn("scrollable_test.dart", out)
            self.assertNotIn("Linux flaky_shard", out)
            self.assertNotIn("Linux experimental_bringup", out)

    def test_preflight_fetches_pr_template_without_checkout_and_infers_repo_from_spool(self) -> None:
        import io
        import subprocess
        from contextlib import redirect_stdout
        from unittest import mock

        remote_template = (
            "## Pre-launch Checklist\n"
            "- [ ] I read the [Contributor Guide] and followed the process.\n"
            "- [ ] I listed at least one issue that this PR fixes.\n"
            "- [ ] I added new tests to check the change.\n"
        )

        def fake_run_cmd(
            args: list[str],
            *,
            cwd: Path | None = None,
            check: bool = True,
            timeout: int = 120,
        ) -> subprocess.CompletedProcess[str]:
            if args[:3] == ["gh", "api", "user"]:
                return subprocess.CompletedProcess(args, 0, stdout="remote-bot\n", stderr="")
            if args[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=json.dumps({
                        "number": 777,
                        "title": "Fix widget",
                        "body": "Fixes nothing\n\n## Pre-launch Checklist\n- [x] I read the [Contributor Guide] and followed the process.\n",
                        "author": {"login": "alice"},
                        "labels": [],
                        "isDraft": False,
                        "mergeable": "MERGEABLE",
                        "headRefOid": "cafebabe1234",
                        "baseRefName": "master",
                        "files": [{"path": "packages/flutter/lib/src/widgets/foo.dart"}],
                        "statusCheckRollup": [{"name": "cla/google", "conclusion": "SUCCESS"}],
                        "url": "https://github.com/flutter/flutter/pull/777",
                    }),
                    stderr="",
                )
            if args[:3] == ["gh", "pr", "diff"]:
                return subprocess.CompletedProcess(args, 0, stdout=SAMPLE_DIFF, stderr="")
            if len(args) >= 3 and args[:2] == ["gh", "api"] and "PULL_REQUEST_TEMPLATE.md" in args[2]:
                return subprocess.CompletedProcess(args, 0, stdout=remote_template, stderr="")
            if args[:3] == ["gh", "api", "graphql"]:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=json.dumps({"data": {"repository": {"pullRequest": {"reviews": {"nodes": []}, "comments": {"nodes": []}, "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}}}),
                    stderr="",
                )
            if args[:3] == ["gh", "repo", "view"]:
                # Simulate `/home/antigravity` outside any git repository!
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="not a git repository")
            raise AssertionError(f"Unexpected subprocess call: {args}")

        with tempfile.TemporaryDirectory() as tmp:
            empty_home = Path(tmp) / "home_antigravity"
            empty_home.mkdir()
            spool_root = Path(tmp) / "spool"

            buf = io.StringIO()
            with mock.patch.object(pr_tool, "run_cmd", side_effect=fake_run_cmd), redirect_stdout(buf):
                rc = pr_tool.main([
                    "preflight",
                    "--pr",
                    "777",
                    "--repo",
                    "flutter/flutter",
                    "--worktree",
                    str(empty_home),
                    "--spool-root",
                    str(spool_root),
                ])
            self.assertEqual(rc, 0)
            state = json.loads(buf.getvalue())
            g3 = state["gates"]["gate3_process_ai"]
            self.assertEqual(g3["template_source"], "gh_api")
            self.assertEqual(g3["template_checkbox_count"], 3)
            self.assertEqual(len(g3["missing_template_items"]), 2)

            # Subsequent commands (`diff`, `validate`) without `--repo` in `/home/antigravity`
            # must infer `flutter/flutter` automatically from `spool_root`!
            buf_diff = io.StringIO()
            with mock.patch.object(pr_tool, "run_cmd", side_effect=fake_run_cmd), redirect_stdout(buf_diff):
                rc_diff = pr_tool.main([
                    "diff",
                    "--pr",
                    "777",
                    "--worktree",
                    str(empty_home),
                    "--spool-root",
                    str(spool_root),
                ])
            self.assertEqual(rc_diff, 0)
            self.assertIn("[R   12]", buf_diff.getvalue())


if __name__ == "__main__":
    unittest.main()
