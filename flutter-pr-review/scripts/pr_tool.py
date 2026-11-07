#!/usr/bin/env python3
"""Deterministic context-bounding and validation CLI for flutter-pr-review.

Zero third-party dependencies (Python 3.9+ stdlib + `gh` + `git`).
Spools raw GitHub/diff/CI payloads under /tmp/flutter-pr-review/<owner>-<repo>-<pr>/
and emits compact, pre-computed views for Gates 1-7 and final draft QA.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

DEFAULT_SPOOL_ROOT = Path("/tmp/flutter-pr-review")

# Gate 5: Exact checks that are never actionable signals on an open PR.
NOT_A_SIGNAL_CHECKS = frozenset({
    "Google testing",
    "Merge Queue Guard",
    "flutter-gold",
    "tree-status",
})

FAILURE_CONCLUSIONS = frozenset({
    "FAILURE",
    "TIMED_OUT",
    "CANCELLED",
    "STARTUP_FAILURE",
    "ACTION_REQUIRED",
})

FAILURE_STATES = frozenset({
    "ERROR",
    "FAILURE",
})

# Gate 3: Stray AI/agent artifact globs.
STRAY_AI_GLOBS = (
    "*.plan.md",
    "*_plan.md",
    "PLAN.md",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".cursor/*",
    ".cursor/**",
    ".gemini/*",
    ".gemini/**",
    ".claude/*",
    ".claude/**",
    ".windsurf/*",
    ".windsurf/**",
    ".aider*",
)

GENERATED_OR_ZERO_WEIGHT_GLOBS = (
    "*.g.dart",
    "*.freezed.dart",
    "*.mocks.dart",
    "*.pb.dart",
    "*.pbenum.dart",
    "*.pbjson.dart",
    "*.pbserver.dart",
    "*.lock",
    "pubspec.lock",
    "*.png",
    "*.golden",
)

# Common Flutter lifecycle hooks surfaced automatically in `--for-diff` slices.
FLUTTER_LIFECYCLE_METHODS = frozenset({
    "initState",
    "didUpdateWidget",
    "didChangeDependencies",
    "activate",
    "deactivate",
    "reassemble",
    "dispose",
    "createState",
    "createElement",
    "createRenderObject",
    "updateRenderObject",
    "didUnmountRenderObject",
    "attach",
    "detach",
    "adoptChild",
    "dropChild",
    "performResize",
    "performLayout",
    "computeDryLayout",
    "paint",
    "describeSemanticsConfiguration",
    "assembleSemanticsNode",
    "handleEvent",
    "hitTest",
    "hitTestSelf",
    "hitTestChildren",
    "markNeedsLayout",
    "markNeedsPaint",
    "markNeedsSemanticsUpdate",
    "notifyListeners",
    "addListener",
    "removeListener",
    "mount",
    "unmount",
    "update",
    "rebuild",
})

DIFF_CHUNK_MAX_LINES = 700
DIFF_CHUNK_MAX_BYTES = 35_000


class ToolError(RuntimeError):
    """Raised on recoverable CLI / environment errors."""


def run_cmd(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Execute a subprocess and capture UTF-8 text output."""
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"Required executable not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"Command timed out after {timeout}s: {' '.join(args)}") from exc
    if check and proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()
        raise ToolError(f"Command failed ({proc.returncode}): {' '.join(args)}\n{stderr}")
    return proc


def run_git(args: list[str], *, cwd: Path | None = None, check: bool = True) -> str:
    """Run git with hooks and external diff/textconv disabled for deterministic output."""
    cmd = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "diff.external=",
        *args,
    ]
    return run_cmd(cmd, cwd=cwd, check=check).stdout


def run_gh(args: list[str], *, cwd: Path | None = None, check: bool = True) -> str:
    """Run GitHub CLI command and return stdout."""
    return run_cmd(["gh", *args], cwd=cwd, check=check).stdout


def normalize_repo_slug(
    repo: str | None,
    worktree: Path | None = None,
    *,
    pr_number: int | None = None,
    spool_root: Path | None = None,
) -> str:
    """Resolve `<owner>/<repo>` from explicit flag, worktree git remote, or prior spool state."""
    if repo and "/" in repo:
        return repo.strip()
    cwd = worktree if (worktree is not None and worktree.is_dir()) else None
    proc = run_cmd(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        cwd=cwd,
        check=False,
    )
    out = (proc.stdout or "").strip() if proc.returncode == 0 else ""
    if out and "/" in out:
        return out
    if pr_number is not None:
        root = spool_root or DEFAULT_SPOOL_ROOT
        if root.is_dir():
            matched_repos: set[str] = set()
            suffix = f"-{pr_number}"
            for child in root.iterdir():
                if not child.is_dir() or not child.name.endswith(suffix):
                    continue
                st_file = child / "state.json"
                if not st_file.is_file():
                    continue
                try:
                    st = json.loads(st_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                st_repo = st.get("repo")
                if st.get("pr_number") == pr_number and isinstance(st_repo, str) and "/" in st_repo:
                    matched_repos.add(st_repo.strip())
            if len(matched_repos) == 1:
                return next(iter(matched_repos))
    raise ToolError("Unable to infer <owner>/<repo>; pass --repo owner/repo.")


def resolve_spool_dir(repo: str, pr_number: int, spool_root: Path | None = None) -> Path:
    """Return `/tmp/flutter-pr-review/<owner>-<repo>-<pr>` and ensure it exists."""
    root = spool_root or DEFAULT_SPOOL_ROOT
    safe_slug = repo.replace("/", "-")
    out = root / f"{safe_slug}-{pr_number}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def is_test_path(path: str) -> bool:
    """Return True if a repo-relative file path is a test file."""
    parts = Path(path).parts
    if "test" in parts or "tests" in parts or "integration_test" in parts or "test_driver" in parts:
        return True
    return path.endswith("_test.dart") or path.endswith("_test.py")


def is_zero_weight_path(path: str) -> bool:
    """Return True for generated, lockfile, or binary golden files."""
    name = Path(path).name
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat) for pat in GENERATED_OR_ZERO_WEIGHT_GLOBS)


def is_comment_or_import_line(code_line: str) -> bool:
    """Return True if an added/removed Dart line is blank, comment-only, or an import/export."""
    stripped = code_line.strip()
    if not stripped:
        return True
    if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
        return True
    if stripped.startswith(("import ", "export ", "part ")):
        return True
    return False


HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_unified_diff(diff_text: str) -> list[dict[str, Any]]:
    """Parse unified diff into structured per-file entries with right-side line tracking.

    Each file entry contains:
      - path: right-side path (or old path if deleted)
      - old_path, new_path: raw paths
      - is_deleted: bool
      - additions, deletions: int
      - right_lines: sorted list of valid RIGHT-side line numbers in the diff
      - added_right_lines: sorted list of `+` RIGHT-side line numbers
      - hunks: list of {header, new_start, new_count, annotated_lines, raw_lines}
    """
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_hunk: dict[str, Any] | None = None
    old_ln = 0
    new_ln = 0

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            if current is not None:
                current["right_lines"] = sorted(current["_right_set"])
                current["added_right_lines"] = sorted(current["_added_set"])
                del current["_right_set"]
                del current["_added_set"]
                files.append(current)
            m = re.match(r"^diff --git a/(.*?) b/(.*)$", raw_line)
            old_p = m.group(1) if m else ""
            new_p = m.group(2) if m else ""
            current = {
                "header": [raw_line],
                "old_path": old_p,
                "new_path": new_p,
                "path": new_p or old_p,
                "is_deleted": False,
                "additions": 0,
                "deletions": 0,
                "_right_set": set(),
                "_added_set": set(),
                "hunks": [],
            }
            current_hunk = None
            continue

        if current is None:
            continue

        if current_hunk is None:
            if raw_line.startswith("deleted file mode"):
                current["is_deleted"] = True
            elif raw_line.startswith("--- "):
                if raw_line[4:].strip() != "/dev/null":
                    p = raw_line[4:].strip()
                    current["old_path"] = p[2:] if p.startswith("a/") else p
            elif raw_line.startswith("+++ "):
                if raw_line[4:].strip() == "/dev/null":
                    current["is_deleted"] = True
                else:
                    p = raw_line[4:].strip()
                    clean = p[2:] if p.startswith("b/") else p
                    current["new_path"] = clean
                    current["path"] = clean

        hunk_match = HUNK_HEADER_RE.match(raw_line)
        if hunk_match:
            old_ln = int(hunk_match.group(1))
            new_ln = int(hunk_match.group(3))
            new_cnt = int(hunk_match.group(4)) if hunk_match.group(4) is not None else 1
            current_hunk = {
                "header": raw_line,
                "new_start": new_ln,
                "new_count": new_cnt,
                "raw_lines": [raw_line],
                "annotated_lines": [raw_line],
            }
            current["hunks"].append(current_hunk)
            continue

        if current_hunk is None:
            current["header"].append(raw_line)
            continue

        current_hunk["raw_lines"].append(raw_line)
        if raw_line.startswith("+"):
            current["additions"] += 1
            if not current["is_deleted"]:
                current["_right_set"].add(new_ln)
                current["_added_set"].add(new_ln)
            current_hunk["annotated_lines"].append(f"[R{new_ln:>5}] {raw_line}")
            new_ln += 1
        elif raw_line.startswith("-"):
            current["deletions"] += 1
            current_hunk["annotated_lines"].append(f"[     -] {raw_line}")
            old_ln += 1
        elif raw_line.startswith(" "):
            if not current["is_deleted"]:
                current["_right_set"].add(new_ln)
            current_hunk["annotated_lines"].append(f"[R{new_ln:>5}] {raw_line}")
            old_ln += 1
            new_ln += 1
        else:
            current_hunk["annotated_lines"].append(f"         {raw_line}")

    if current is not None:
        current["right_lines"] = sorted(current["_right_set"])
        current["added_right_lines"] = sorted(current["_added_set"])
        del current["_right_set"]
        del current["_added_set"]
        files.append(current)

    return files


def compute_diff_weights(parsed_files: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute raw and substantive weighted diff lines (prod=1.0, test=0.25, comments/gen=0.0)."""
    raw_changed = 0
    prod_substantive = 0
    test_substantive = 0
    prod_files = 0
    test_files = 0

    for entry in parsed_files:
        path = entry["path"]
        raw_changed += entry["additions"] + entry["deletions"]
        is_test = is_test_path(path)
        if is_test:
            test_files += 1
        elif not is_zero_weight_path(path):
            prod_files += 1

        if is_zero_weight_path(path):
            continue

        substantive = 0
        for hunk in entry["hunks"]:
            for line in hunk["raw_lines"][1:]:
                if line.startswith(("+", "-")):
                    if not is_comment_or_import_line(line[1:]):
                        substantive += 1
        if is_test:
            test_substantive += substantive
        else:
            prod_substantive += substantive

    weighted = round(prod_substantive * 1.0 + test_substantive * 0.25, 1)
    return {
        "raw_changed_lines": raw_changed,
        "exceeds_200_raw_lines": raw_changed > 200,
        "prod_substantive_lines": prod_substantive,
        "test_substantive_lines": test_substantive,
        "weighted_lines": weighted,
        "prod_files_count": prod_files,
        "test_files_count": test_files,
        "total_files_count": len(parsed_files),
    }


def chunk_annotated_diff(
    parsed_files: list[dict[str, Any]],
    *,
    max_lines: int = DIFF_CHUNK_MAX_LINES,
    max_bytes: int = DIFF_CHUNK_MAX_BYTES,
) -> list[dict[str, Any]]:
    """Pack annotated file hunks into <=700-line / <=35KB pages without splitting mid-line."""
    chunks: list[dict[str, Any]] = []
    cur_lines: list[str] = []
    cur_bytes = 0
    cur_files: list[str] = []

    def flush() -> None:
        nonlocal cur_lines, cur_bytes, cur_files
        if not cur_lines:
            return
        chunks.append({
            "index": len(chunks) + 1,
            "files": list(dict.fromkeys(cur_files)),
            "line_count": len(cur_lines),
            "text": "\n".join(cur_lines),
        })
        cur_lines = []
        cur_bytes = 0
        cur_files = []

    for entry in parsed_files:
        file_banner = f"=== {entry['path']} (+{entry['additions']}/-{entry['deletions']}) ==="
        if not entry["hunks"]:
            block = [file_banner, *entry["header"]]
            b_bytes = sum(len(x.encode("utf-8")) + 1 for x in block)
            if cur_lines and (len(cur_lines) + len(block) > max_lines or cur_bytes + b_bytes > max_bytes):
                flush()
            cur_lines.extend(block)
            cur_bytes += b_bytes
            cur_files.append(entry["path"])
            continue

        need_banner = True
        for hunk in entry["hunks"]:
            annotated = hunk["annotated_lines"]
            # Split gigantic single hunk into sub-slices while keeping header context.
            pos = 0
            while pos < len(annotated):
                prefix = [file_banner] if need_banner else []
                if pos > 0:
                    prefix.append(f"{hunk['header']} (continued)")
                room = max(20, max_lines - len(cur_lines) - len(prefix))
                if cur_lines and room < 30:
                    flush()
                    prefix = [file_banner] + ([f"{hunk['header']} (continued)"] if pos > 0 else [])
                    room = max(20, max_lines - len(prefix))
                slice_lines = annotated[pos : pos + room]
                # Enforce byte ceiling as well.
                while len(slice_lines) > 1:
                    b_len = sum(len(x.encode("utf-8")) + 1 for x in (*prefix, *slice_lines))
                    if cur_bytes + b_len <= max_bytes or not cur_lines:
                        if b_len <= max_bytes:
                            break
                    if cur_lines:
                        flush()
                        prefix = [file_banner] + ([f"{hunk['header']} (continued)"] if pos > 0 else [])
                        continue
                    slice_lines = slice_lines[: max(1, len(slice_lines) // 2)]
                block = [*prefix, *slice_lines]
                cur_lines.extend(block)
                cur_bytes += sum(len(x.encode("utf-8")) + 1 for x in block)
                cur_files.append(entry["path"])
                need_banner = False
                pos += len(slice_lines)
                if pos < len(annotated):
                    flush()
                    need_banner = True

    flush()
    return chunks


FROZEN_FLUTTER_PREFIXES = (
    "packages/flutter/lib/src/material/",
    "packages/flutter/lib/src/cupertino/",
    "packages/flutter/test/material/",
    "packages/flutter/test/cupertino/",
)
FROZEN_FLUTTER_EXACT = frozenset({
    "packages/flutter/lib/material.dart",
    "packages/flutter/lib/cupertino.dart",
})


def is_frozen_material_cupertino_path(path: str) -> bool:
    """Return True if path is under frozen Material/Cupertino dirs in flutter/flutter."""
    return path in FROZEN_FLUTTER_EXACT or path.startswith(FROZEN_FLUTTER_PREFIXES)


def is_stray_ai_path(path: str) -> bool:
    """Return True if path matches Gate 3 stray AI/scratch file globs."""
    name = Path(path).name
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(name, g) for g in STRAY_AI_GLOBS)


def extract_checklist_items(markdown_text: str) -> list[str]:
    """Extract normalized bullet text from `- [ ] ...` / `- [x] ...` checklist lines."""
    items: list[str] = []
    for line in markdown_text.splitlines():
        m = re.match(r"^\s*[-*]\s*\[[ xX]\]\s*(.+?)\s*$", line)
        if m:
            cleaned = re.sub(r"<!--.*?-->", "", m.group(1)).strip()
            if cleaned:
                items.append(cleaned)
    return items


PR_TEMPLATE_CANDIDATES = (
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
    "docs/PULL_REQUEST_TEMPLATE.md",
)


def _worktree_matches_repo(worktree: Path, repo: str) -> bool:
    """Return True if `worktree` is non-git or its `origin` remote matches `repo`'s name."""
    if not (worktree / ".git").exists():
        return True
    proc = run_cmd(
        ["git", "-c", "core.hooksPath=/dev/null", "remote", "get-url", "origin"],
        cwd=worktree,
        check=False,
    )
    origin = (proc.stdout or "").strip() if proc.returncode == 0 else ""
    if not origin:
        return True
    origin_clean = origin.rstrip("/").removesuffix(".git")
    origin_name = origin_clean.rsplit("/", 1)[-1].rsplit(":", 1)[-1].lower()
    target_name = repo.split("/", 1)[-1].lower()
    return origin_name == target_name


def resolve_pr_template(
    repo: str,
    worktree: Path | None,
    *,
    spool_dir: Path | None = None,
    base_ref: str | None = None,
) -> tuple[str | None, str]:
    """Load PR template from matching local worktree, spool cache, or `gh api` fallback."""
    if worktree and worktree.is_dir() and _worktree_matches_repo(worktree, repo):
        for rel in PR_TEMPLATE_CANDIDATES:
            candidate = worktree / rel
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace"), "worktree"

    cached = (spool_dir / "PULL_REQUEST_TEMPLATE.md") if spool_dir else None
    if cached and cached.is_file():
        return cached.read_text(encoding="utf-8", errors="replace"), "spool"

    cwd = worktree if (worktree is not None and worktree.is_dir()) else None
    endpoints: list[str] = []
    if base_ref:
        quoted_ref = urllib.parse.quote(base_ref, safe="")
        endpoints.append(f"repos/{repo}/contents/.github/PULL_REQUEST_TEMPLATE.md?ref={quoted_ref}")
    for rel in PR_TEMPLATE_CANDIDATES:
        ep = f"repos/{repo}/contents/{rel}"
        if ep not in endpoints:
            endpoints.append(ep)

    for ep in endpoints:
        proc = run_cmd(
            ["gh", "api", ep, "-H", "Accept: application/vnd.github.raw"],
            cwd=cwd,
            check=False,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip():
            if cached:
                cached.write_text(proc.stdout, encoding="utf-8")
            return proc.stdout, "gh_api"

    return None, "missing"


def evaluate_gates_1_to_5(
    *,
    repo: str,
    pr_meta: dict[str, Any],
    changed_paths: list[str],
    worktree: Path | None,
    template_text: str | None = None,
    template_source: str | None = None,
) -> dict[str, Any]:
    """Deterministically compute Gate 1-5 mechanical signals from PR metadata and paths."""
    labels = [lbl.get("name", "") for lbl in (pr_meta.get("labels") or []) if isinstance(lbl, dict)]
    label_set_lower = {lbl.lower() for lbl in labels}
    rollup = pr_meta.get("statusCheckRollup") or []

    # Gate 1: cla/google
    cla_state = "MISSING"
    for item in rollup:
        name = (item.get("name") or item.get("context") or "").strip()
        if name.lower() == "cla/google":
            raw = (item.get("conclusion") or item.get("state") or "PENDING").upper()
            cla_state = raw
            if raw == "SUCCESS":
                break
    gate1 = {
        "cla_google_state": cla_state,
        "passed": cla_state == "SUCCESS",
    }

    # Gate 2: frozen Material/Cupertino in flutter/flutter
    if repo.lower() == "flutter/flutter":
        frozen = [p for p in changed_paths if is_frozen_material_cupertino_path(p)]
        non_frozen = [p for p in changed_paths if not is_frozen_material_cupertino_path(p)]
        if frozen and not non_frozen:
            g2_status = "only_material_cupertino"
        elif frozen and non_frozen:
            g2_status = "mixed_with_framework"
        else:
            g2_status = "ok"
    else:
        frozen = []
        non_frozen = list(changed_paths)
        g2_status = "ok"
    gate2 = {
        "status": g2_status,
        "frozen_paths": frozen,
        "other_paths_count": len(non_frozen),
    }

    # Gate 3: PR template checklist & stray AI files
    body = pr_meta.get("body") or ""
    has_prelaunch_heading = bool(
        re.search(r"^##+\s+Pre-(?:launch|Review) Checklist", body, re.IGNORECASE | re.MULTILINE)
    )
    body_boxes = extract_checklist_items(body)
    missing_template_items: list[str] = []
    template_total = 0
    resolved_tpl_text = template_text
    resolved_tpl_source = template_source or "missing"
    if resolved_tpl_text is None and worktree:
        for rel in PR_TEMPLATE_CANDIDATES:
            tpl_path = worktree / rel
            if tpl_path.is_file():
                resolved_tpl_text = tpl_path.read_text(encoding="utf-8", errors="replace")
                resolved_tpl_source = "worktree"
                break
    if resolved_tpl_text is not None:
        tpl_items = extract_checklist_items(resolved_tpl_text)
        template_total = len(tpl_items)
        body_norm = re.sub(r"\s+", " ", body.lower())
        for t_item in tpl_items:
            # Match using first 35 chars of normalized text so minor formatting edits still match.
            probe = re.sub(r"\s+", " ", t_item).strip().lower()[:35]
            if probe and probe not in body_norm:
                missing_template_items.append(t_item[:90])
    stray_files = [p for p in changed_paths if is_stray_ai_path(p)]
    gate3 = {
        "has_prelaunch_checklist_heading": has_prelaunch_heading,
        "body_checkbox_count": len(body_boxes),
        "template_checkbox_count": template_total,
        "template_source": resolved_tpl_source,
        "missing_template_items": missing_template_items,
        "stray_ai_files": stray_files,
        "note": "Also verify semantically whether PR description matches the diff (Gate 3 #2).",
    }

    # Gate 4: Tests required
    test_files = [p for p in changed_paths if is_test_path(p)]
    non_test_files = [p for p in changed_paths if not is_test_path(p)]
    has_test_exempt = "test-exempt" in label_set_lower
    gate4 = {
        "has_test_exempt_label": has_test_exempt,
        "touched_test_files": test_files,
        "touched_non_test_files": non_test_files,
        "needs_exemption_or_tests_check": (not has_test_exempt) and (len(test_files) == 0),
    }

    # Gate 5: CI & CICD label
    has_cicd = "cicd" in label_set_lower
    failing_checks: list[dict[str, Any]] = []
    ignored_non_signal = 0
    pending_ignored = 0
    passed_count = 0

    for item in rollup:
        name = (item.get("name") or item.get("context") or "").strip()
        if not name or name.lower() == "cla/google":
            continue
        if name in NOT_A_SIGNAL_CHECKS:
            ignored_non_signal += 1
            continue
        status = (item.get("status") or "").upper()
        conclusion = (item.get("conclusion") or "").upper()
        state = (item.get("state") or "").upper()
        details_url = item.get("detailsUrl") or item.get("targetUrl") or ""

        # Pending / queued / in-progress checks are never a signal on an open PR.
        if status in {"QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"} and not conclusion:
            pending_ignored += 1
            continue
        if not conclusion and state in {"PENDING", "EXPECTED"}:
            pending_ignored += 1
            continue

        is_fail = (conclusion in FAILURE_CONCLUSIONS) or (not conclusion and state in FAILURE_STATES)
        if is_fail:
            gh_run_id = None
            m_run = re.search(r"/actions/runs/(\d+)", details_url)
            if m_run:
                gh_run_id = m_run.group(1)
            failing_checks.append({
                "name": name,
                "conclusion": conclusion or state,
                "details_url": details_url,
                "gh_run_id": gh_run_id,
            })
        else:
            passed_count += 1

    gate5 = {
        "has_cicd_label": has_cicd,
        "no_cicd_silent_stop": not has_cicd,
        "actionable_failing_checks": failing_checks,
        "passing_checks_count": passed_count,
        "pending_checks_ignored_count": pending_ignored,
        "non_signal_checks_ignored_count": ignored_non_signal,
    }

    return {
        "labels": labels,
        "gate1_cla": gate1,
        "gate2_repo": gate2,
        "gate3_process_ai": gate3,
        "gate4_tests": gate4,
        "gate5_ci": gate5,
    }


DETAILS_BLOCK_RE = re.compile(r"<details\b[^>]*>(.*?)</details>", re.IGNORECASE | re.DOTALL)


def truncate_noisy_markdown(text: str, *, max_chars: int = 2500) -> str:
    """Collapse bulky `<details>` blocks (e.g., `flutter doctor -v`) and cap total length."""
    if not text:
        return ""

    def _replace_details(match: re.Match[str]) -> str:
        inner = match.group(1)
        if len(inner) <= 400:
            return match.group(0)
        summary_m = re.search(r"<summary>(.*?)</summary>", inner, re.IGNORECASE | re.DOTALL)
        label = summary_m.group(1).strip() if summary_m else "details"
        return f"[collapsed <details>: {label} ({len(inner)} chars; see raw JSON in spool dir)]"

    cleaned = DETAILS_BLOCK_RE.sub(_replace_details, text).strip()
    if len(cleaned) > max_chars:
        return cleaned[:max_chars] + f"\n... [truncated {len(cleaned) - max_chars} chars; full text in spool dir]"
    return cleaned


def extract_linked_issue_refs(body: str, default_repo: str) -> list[tuple[str, int]]:
    """Find GitHub issue numbers referenced in PR body (`Fixes #123`, full issue URLs, etc.)."""
    found: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    for m in re.finditer(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)", body or ""):
        key = (m.group(1), int(m.group(2)))
        if key not in seen:
            seen.add(key)
            found.append(key)

    for m in re.finditer(r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|issue|related to)?\s*(?<![/\w])#(\d+)\b", body or "", re.IGNORECASE):
        key = (default_repo, int(m.group(1)))
        if key not in seen:
            seen.add(key)
            found.append(key)

    return found[:5]


NOISY_BOT_LOGINS = frozenset({
    "google-cla",
    "fluttergithubbot",
    "github-actions",
    "codecov",
})


def is_noisy_bot_login(login: str) -> bool:
    """Return True if `login` (with optional `[bot]` suffix) is a noisy non-review bot."""
    normalized = login.strip().lower().removesuffix("[bot]")
    return normalized in NOISY_BOT_LOGINS


GRAPHQL_THREADS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!, $threadsCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviews(last: 100) {
        nodes {
          author { login }
          state
          submittedAt
          body
        }
      }
      comments(last: 100) {
        nodes {
          author { login }
          createdAt
          body
        }
      }
      reviewThreads(first: 100, after: $threadsCursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          isResolved
          isOutdated
          path
          line
          comments(first: 50) {
            nodes {
              author { login }
              createdAt
              body
            }
          }
        }
      }
    }
  }
}
"""

GRAPHQL_THREADS_PAGE_QUERY = """
query($owner: String!, $name: String!, $pr: Int!, $threadsCursor: String!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $threadsCursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          isResolved
          isOutdated
          path
          line
          comments(first: 50) {
            nodes {
              author { login }
              createdAt
              body
            }
          }
        }
      }
    }
  }
}
"""


def fetch_pr_threads_graphql(
    owner: str,
    name: str,
    pr_num: int,
    *,
    cwd: Path | None = None,
    max_pages: int = 10,
) -> dict[str, Any]:
    """Fetch PR reviews, comments, and all paginated `reviewThreads` via GraphQL."""
    first_raw = run_gh(
        [
            "api",
            "graphql",
            "-f",
            f"query={GRAPHQL_THREADS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"pr={pr_num}",
        ],
        cwd=cwd,
    )
    merged: dict[str, Any] = json.loads(first_raw)
    pr_node = ((merged.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    threads_conn = pr_node.get("reviewThreads") or {}
    all_nodes = list(threads_conn.get("nodes") or [])
    page_info = threads_conn.get("pageInfo") or {}

    pages_fetched = 1
    while page_info.get("hasNextPage") and page_info.get("endCursor") and pages_fetched < max_pages:
        cursor = str(page_info["endCursor"])
        page_raw = run_gh(
            [
                "api",
                "graphql",
                "-f",
                f"query={GRAPHQL_THREADS_PAGE_QUERY}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"pr={pr_num}",
                "-F",
                f"threadsCursor={cursor}",
            ],
            cwd=cwd,
        )
        page_data = json.loads(page_raw)
        next_conn = (
            (((page_data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("reviewThreads")
            or {}
        )
        all_nodes.extend(next_conn.get("nodes") or [])
        page_info = next_conn.get("pageInfo") or {}
        pages_fetched += 1

    if isinstance(threads_conn, dict):
        threads_conn["nodes"] = all_nodes
        threads_conn["pageInfo"] = page_info
    return merged


def format_compacted_threads(
    gql_data: dict[str, Any],
    *,
    agent_login: str,
) -> tuple[str, dict[str, int | bool]]:
    """Format GraphQL PR reviews, comments, and inline threads into compact Markdown."""
    pr_node = ((gql_data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    reviews = (pr_node.get("reviews") or {}).get("nodes") or []
    issue_comments = (pr_node.get("comments") or {}).get("nodes") or []
    threads = (pr_node.get("reviewThreads") or {}).get("nodes") or []

    lines: list[str] = ["# Existing PR Threads & Reviews"]
    has_self_review = False
    unresolved_count = 0
    resolved_count = 0

    # 1. Top-level review bodies (always keep self reviews & non-empty reviews)
    kept_reviews: list[str] = []
    for rev in reviews:
        if not isinstance(rev, dict):
            continue
        login = ((rev.get("author") or {}).get("login") or "unknown").strip()
        if login == agent_login:
            has_self_review = True
        body = (rev.get("body") or "").strip()
        state = rev.get("state") or ""
        if not body and login != agent_login:
            continue
        tag = " [PRIOR SELF REVIEW]" if login == agent_login else ""
        kept_reviews.append(
            f"- **{login}** ({state}, {rev.get('submittedAt', '')}){tag}:\n"
            f"  {truncate_noisy_markdown(body, max_chars=1500)}"
        )
    if kept_reviews:
        lines.append("\n## Review Summaries")
        lines.extend(kept_reviews)

    # 2. Inline threads (unresolved / gemini-code-assist / self in full; resolved collapsed to 1 line)
    unresolved_lines: list[str] = []
    resolved_lines: list[str] = []
    for idx, th in enumerate(threads, start=1):
        if not isinstance(th, dict):
            continue
        is_resolved = bool(th.get("isResolved"))
        is_outdated = bool(th.get("isOutdated"))
        path = th.get("path") or "?"
        line_no = th.get("line")
        loc = f"{path}:{line_no}" if line_no else path
        c_nodes = [c for c in ((th.get("comments") or {}).get("nodes") or []) if isinstance(c, dict)]
        if not c_nodes:
            continue
        authors = [((c.get("author") or {}).get("login") or "unknown") for c in c_nodes]
        if agent_login in authors:
            has_self_review = True

        keep_full = (
            (not is_resolved)
            or (agent_login in authors)
            or any("gemini-code-assist" in a.lower() for a in authors)
        )
        if is_resolved:
            resolved_count += 1
        else:
            unresolved_count += 1

        if keep_full:
            flags = []
            if is_resolved:
                flags.append("RESOLVED")
            if is_outdated:
                flags.append("OUTDATED")
            flag_str = f" [{' | '.join(flags)}]" if flags else " [OPEN]"
            block = [f"### Thread #{idx} — `{loc}`{flag_str}"]
            for c in c_nodes:
                c_login = (c.get("author") or {}).get("login") or "unknown"
                c_body = truncate_noisy_markdown((c.get("body") or "").strip(), max_chars=1200)
                block.append(f"- **{c_login}**: {c_body}")
            unresolved_lines.append("\n".join(block))
        else:
            first_snippet = re.sub(r"\s+", " ", (c_nodes[0].get("body") or "").strip())[:120]
            resolved_lines.append(f"- [RESOLVED] `{loc}` ({', '.join(dict.fromkeys(authors))}): {first_snippet}")

    lines.append(f"\n## Inline Review Threads ({unresolved_count} open, {resolved_count} resolved)")
    if unresolved_lines:
        lines.extend(unresolved_lines)
    else:
        lines.append("_No open/bot/self inline threads._")
    if resolved_lines:
        lines.append("\n### Collapsed Resolved Human Threads")
        lines.extend(resolved_lines)

    # 3. PR conversation comments (skip CLA / label bot noise unless self)
    convo: list[str] = []
    for c in issue_comments:
        if not isinstance(c, dict):
            continue
        login = ((c.get("author") or {}).get("login") or "unknown").strip()
        if login == agent_login:
            has_self_review = True
        if is_noisy_bot_login(login) and login != agent_login:
            continue
        body = truncate_noisy_markdown((c.get("body") or "").strip(), max_chars=1200)
        if body:
            convo.append(f"- **{login}** ({c.get('createdAt', '')}): {body}")
    if convo:
        lines.append("\n## PR Conversation Comments")
        lines.extend(convo)

    stats: dict[str, int | bool] = {
        "has_prior_self_review": has_self_review,
        "open_inline_threads": unresolved_count,
        "resolved_inline_threads": resolved_count,
        "conversation_comments_kept": len(convo),
    }
    return "\n\n".join(lines) + "\n", stats


def load_spool_state(spool_dir: Path) -> dict[str, Any]:
    """Load persisted `state.json` from a prior `preflight` run."""
    state_file = spool_dir / "state.json"
    if not state_file.is_file():
        raise ToolError(
            f"Spool state not found at {state_file}. Run `pr_tool.py preflight` first."
        )
    return json.loads(state_file.read_text(encoding="utf-8"))


def cmd_preflight(args: argparse.Namespace) -> int:
    """Fetch PR artifacts once to `/tmp/...` and emit compact Gate 1-5 + diff-weight digest."""
    worktree = Path(args.worktree).resolve() if args.worktree else Path.cwd()
    gh_cwd = worktree if worktree.is_dir() else None
    spool_root = Path(args.spool_root) if args.spool_root else None
    pr_num = int(args.pr)
    repo = normalize_repo_slug(args.repo, gh_cwd, pr_number=pr_num, spool_root=spool_root)
    spool_dir = resolve_spool_dir(repo, pr_num, spool_root)

    agent_login = run_gh(["api", "user", "--jq", ".login"], cwd=gh_cwd).strip()
    pr_raw = run_gh(
        [
            "pr",
            "view",
            str(pr_num),
            "--repo",
            repo,
            "--json",
            "number,title,body,author,labels,isDraft,mergeable,headRefOid,baseRefName,files,statusCheckRollup,url",
        ],
        cwd=gh_cwd,
    )
    (spool_dir / "pr.json").write_text(pr_raw, encoding="utf-8")
    pr_meta = json.loads(pr_raw)

    diff_raw = run_gh(["pr", "diff", str(pr_num), "--repo", repo], cwd=gh_cwd)
    (spool_dir / "diff.patch").write_text(diff_raw, encoding="utf-8")
    parsed_diff = parse_unified_diff(diff_raw)
    weights = compute_diff_weights(parsed_diff)

    changed_paths = [
        f.get("path", "")
        for f in (pr_meta.get("files") or [])
        if isinstance(f, dict) and f.get("path")
    ] or [entry["path"] for entry in parsed_diff]

    tpl_text, tpl_source = resolve_pr_template(
        repo=repo,
        worktree=gh_cwd,
        spool_dir=spool_dir,
        base_ref=pr_meta.get("baseRefName"),
    )
    gates = evaluate_gates_1_to_5(
        repo=repo,
        pr_meta=pr_meta,
        changed_paths=changed_paths,
        worktree=gh_cwd,
        template_text=tpl_text,
        template_source=tpl_source,
    )

    # Fetch and spool threads separately (anti-anchoring: do not print threads in preflight!).
    owner, name = repo.split("/", 1)
    gql_data = fetch_pr_threads_graphql(owner, name, pr_num, cwd=gh_cwd)
    (spool_dir / "threads_raw.json").write_text(json.dumps(gql_data, indent=2), encoding="utf-8")
    threads_md, thread_stats = format_compacted_threads(gql_data, agent_login=agent_login)
    (spool_dir / "threads.md").write_text(threads_md, encoding="utf-8")

    # Fetch linked issues + compacted comments for Gate 6 repro discovery.
    issue_refs = extract_linked_issue_refs(pr_meta.get("body") or "", repo)
    linked_issues: list[dict[str, Any]] = []
    for issue_repo, issue_num in issue_refs:
        proc = run_cmd(
            [
                "gh",
                "issue",
                "view",
                str(issue_num),
                "--repo",
                issue_repo,
                "--json",
                "number,title,state,url,body,labels,comments",
            ],
            cwd=gh_cwd,
            check=False,
        )
        if proc.returncode != 0:
            continue
        (spool_dir / f"issue_{issue_repo.replace('/', '-')}_{issue_num}.json").write_text(
            proc.stdout, encoding="utf-8"
        )
        iss = json.loads(proc.stdout)
        comments_compact = []
        for c in (iss.get("comments") or [])[:15]:
            c_login = ((c.get("author") or {}).get("login") or "unknown").strip()
            if is_noisy_bot_login(c_login):
                continue
            comments_compact.append({
                "author": c_login,
                "body": truncate_noisy_markdown(c.get("body") or "", max_chars=1200),
            })
        linked_issues.append({
            "repo": issue_repo,
            "number": iss.get("number"),
            "title": iss.get("title"),
            "state": iss.get("state"),
            "url": iss.get("url"),
            "body": truncate_noisy_markdown(iss.get("body") or "", max_chars=2500),
            "comments": comments_compact,
        })

    state_payload = {
        "repo": repo,
        "pr_number": pr_num,
        "url": pr_meta.get("url"),
        "title": pr_meta.get("title"),
        "author": (pr_meta.get("author") or {}).get("login"),
        "is_draft": pr_meta.get("isDraft"),
        "mergeable": pr_meta.get("mergeable"),
        "head_sha": pr_meta.get("headRefOid"),
        "base_ref": pr_meta.get("baseRefName"),
        "agent_login": agent_login,
        "worktree": str(worktree),
        "spool_dir": str(spool_dir),
        "diff_weights": weights,
        "files_summary": [
            {
                "path": e["path"],
                "additions": e["additions"],
                "deletions": e["deletions"],
                "is_test": is_test_path(e["path"]),
            }
            for e in parsed_diff
        ],
        "gates": gates,
        "threads_deferred": {
            **thread_stats,
            "threads_file": str(spool_dir / "threads.md"),
            "instruction": "Read threads AFTER Gate 3 and initial Gate 6 code pass via `pr_tool.py threads`.",
        },
        "pr_body": truncate_noisy_markdown(pr_meta.get("body") or "", max_chars=4000),
        "linked_issues": linked_issues,
    }
    (spool_dir / "state.json").write_text(json.dumps(state_payload, indent=2), encoding="utf-8")
    print(json.dumps(state_payload, indent=2))
    return 0


def cmd_threads(args: argparse.Namespace) -> int:
    """Print compacted existing PR threads saved by `preflight`."""
    worktree = Path(args.worktree).resolve() if args.worktree else Path.cwd()
    spool_root = Path(args.spool_root) if args.spool_root else None
    pr_num = int(args.pr)
    repo = normalize_repo_slug(args.repo, worktree, pr_number=pr_num, spool_root=spool_root)
    spool_dir = resolve_spool_dir(repo, pr_num, spool_root)
    threads_file = spool_dir / "threads.md"
    if not threads_file.is_file():
        raise ToolError(f"Threads file missing at {threads_file}. Run `pr_tool.py preflight` first.")
    sys.stdout.write(threads_file.read_text(encoding="utf-8"))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    """Print right-side-annotated (`[R<line>]`) diff chunks (<=700 lines / <=35KB each)."""
    worktree = Path(args.worktree).resolve() if args.worktree else Path.cwd()
    spool_root = Path(args.spool_root) if args.spool_root else None
    pr_num = int(args.pr)
    repo = normalize_repo_slug(args.repo, worktree, pr_number=pr_num, spool_root=spool_root)
    spool_dir = resolve_spool_dir(repo, pr_num, spool_root)
    diff_path = spool_dir / "diff.patch"
    if not diff_path.is_file():
        raise ToolError(f"Diff patch missing at {diff_path}. Run `pr_tool.py preflight` first.")

    parsed = parse_unified_diff(diff_path.read_text(encoding="utf-8", errors="replace"))
    if args.file:
        wanted = set(args.file)
        parsed = [e for e in parsed if e["path"] in wanted or any(e["path"].endswith(w) for w in wanted)]
    if args.exclude_tests:
        parsed = [e for e in parsed if not is_test_path(e["path"])]

    chunks = chunk_annotated_diff(parsed)
    if not chunks:
        print("No matching diff hunks.")
        return 0

    if args.list_chunks:
        manifest = [
            {"chunk": c["index"], "lines": c["line_count"], "files": c["files"]}
            for c in chunks
        ]
        print(json.dumps({"total_chunks": len(chunks), "chunks": manifest}, indent=2))
        return 0

    chunk_idx = args.chunk or 1
    if chunk_idx < 1 or chunk_idx > len(chunks):
        raise ToolError(f"Requested --chunk {chunk_idx}, but diff has {len(chunks)} chunk(s).")

    chosen = chunks[chunk_idx - 1]
    print(f"# Diff Chunk {chosen['index']}/{len(chunks)} ({chosen['line_count']} lines)")
    print(chosen["text"])
    if len(chunks) > 1 and chunk_idx < len(chunks):
        print(f"\n# Next chunk: pass `--chunk {chunk_idx + 1}` (of {len(chunks)})")
    return 0


CI_FAILURE_LINE_RE = re.compile(
    r"("
    r"EXCEPTION CAUGHT BY|"
    r"The following \w+ was thrown|"
    r"\[E\]\s*$|"
    r"\bFAIL(?:ED|URE)?\b|"
    r"\berror\s*[•:]|"
    r"Unhandled exception:|"
    r"Some tests failed\.|"
    r"Process exited with code [1-9]|"
    r"infra_failure|timed out|oom-kill"
    r")",
    re.IGNORECASE,
)


def extract_failure_windows(
    log_text: str,
    *,
    before: int = 8,
    after: int = 20,
    max_lines: int = 120,
) -> str:
    """Extract merged failure windows around Flutter/Dart/CI error markers, capped at max_lines."""
    raw_lines = log_text.splitlines()
    if not raw_lines:
        return "(empty log)"
    if len(raw_lines) <= max_lines:
        return "\n".join(raw_lines)

    spans: list[tuple[int, int]] = []
    for idx, line in enumerate(raw_lines):
        if CI_FAILURE_LINE_RE.search(line):
            s = max(0, idx - before)
            e = min(len(raw_lines), idx + after + 1)
            if spans and s <= spans[-1][1] + 2:
                spans[-1] = (spans[-1][0], max(spans[-1][1], e))
            else:
                spans.append((s, e))

    if not spans:
        tail = raw_lines[-max_lines:]
        return f"# No structured error marker matched; showing last {len(tail)} lines:\n" + "\n".join(tail)

    out: list[str] = []
    for s, e in spans:
        header = f"--- [log lines {s + 1}..{e} of {len(raw_lines)}] ---"
        if len(out) + 1 >= max_lines:
            break
        out.append(header)
        remaining = max_lines - len(out)
        chunk = raw_lines[s : min(e, s + remaining)]
        out.extend(chunk)
        if len(out) >= max_lines:
            out.append(f"... [capped at {max_lines} lines; full log saved in spool dir]")
            break
    return "\n".join(out)


LUCI_STEP_LOG_PRIORITY = (
    "stdout",
    "test_results",
    "stderr",
    "execution details",
    "$execution details",
)
LUCI_MAX_RAW_LOG_BYTES = 25_000_000
COCOON_FAILING_TASK_STATUSES = frozenset({
    "failed",
    "infra failure",
    "cancelled",
    "canceled",
})
MAX_DASHBOARD_DETAILED_BUILDS = 5


def _post_buildbucket_prpc(
    method: str,
    payload_obj: dict[str, Any],
    *,
    timeout: int = 30,
) -> tuple[str, dict[str, Any]]:
    """POST a JSON request to LUCI Buildbucket v2 PRPC (`GetBuild`, `SearchBuilds`)."""
    url = f"https://cr-buildbucket.appspot.com/prpc/buildbucket.v2.Builds/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload_obj).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except OSError:
            detail = ""
        suffix = f" ({detail[:240]})" if detail else ""
        raise ToolError(f"LUCI Buildbucket {method} failed: HTTP Error {exc.code}: {exc.reason}{suffix}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ToolError(f"LUCI Buildbucket {method} failed: {exc}") from exc

    if raw.startswith(")]}'"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else "{}"
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise ToolError(f"Invalid JSON from LUCI Buildbucket {method}: {exc}") from exc
    return raw, data


def fetch_luci_build_summary(
    build_id: str,
    spool_dir: Path | None = None,
    *,
    max_lines: int = 80,
) -> str:
    """Query LUCI Buildbucket v2 GetBuild PRPC endpoint and extract failing steps + logs."""
    try:
        raw, data = _post_buildbucket_prpc(
            "GetBuild",
            {
                "id": str(build_id),
                "mask": {"fields": "id,builder,status,summaryMarkdown,steps"},
            },
        )
    except ToolError as exc:
        raise ToolError(f"Failed to query LUCI Buildbucket for build {build_id}: {exc}") from exc

    if spool_dir:
        (spool_dir / f"luci_{build_id}.json").write_text(raw, encoding="utf-8")

    builder_name = ((data.get("builder") or {}).get("builder") or "").strip()
    label = f"{build_id} ({builder_name})" if builder_name else str(build_id)
    parts: list[str] = [
        f"# LUCI Build {label} — status: {data.get('status', 'UNKNOWN')}\n"
        f"URL: https://ci.chromium.org/b/{build_id}",
    ]
    build_summary_md = (data.get("summaryMarkdown") or "").strip()
    if build_summary_md:
        parts.append("## Build Summary\n" + extract_failure_windows(build_summary_md, max_lines=min(60, max_lines)))

    failing_steps = [
        s for s in (data.get("steps") or [])
        if s.get("status") in {"FAILURE", "INFRA_FAILURE", "CANCELED"}
    ]
    # Prefer leaf steps over parent wrapper steps when available.
    step_names = {s.get("name", "") for s in failing_steps}
    leaf_steps = [
        s for s in failing_steps
        if not any(other != s.get("name", "") and other.startswith(s.get("name", "") + "|") for other in step_names)
    ] or failing_steps

    priority_rank = {name: idx for idx, name in enumerate(LUCI_STEP_LOG_PRIORITY)}
    for step in leaf_steps[:5]:
        s_name = step.get("name", "?")
        s_status = step.get("status", "?")
        parts.append(f"## Failing Step: `{s_name}` ({s_status})")
        step_summary_md = (step.get("summaryMarkdown") or "").strip()
        if step_summary_md and step_summary_md != build_summary_md:
            parts.append(extract_failure_windows(step_summary_md, max_lines=min(40, max_lines)))

        candidates = [
            le for le in (step.get("logs") or [])
            if isinstance(le, dict)
            and le.get("name") in priority_rank
            and "logs.chromium.org" in (le.get("viewUrl") or "")
        ]
        candidates.sort(key=lambda le: priority_rank[le["name"]])

        fetched_primary = False
        primary_had_marker = False
        for log_entry in candidates:
            log_name = str(log_entry.get("name") or "log")
            if fetched_primary and primary_had_marker:
                break
            if fetched_primary and log_name != "stderr":
                continue
            view_url = log_entry.get("viewUrl") or ""
            sep = "&" if "?" in view_url else "?"
            raw_url = f"{view_url}{sep}format=raw"
            try:
                with urllib.request.urlopen(raw_url, timeout=30) as lresp:
                    log_text = lresp.read(LUCI_MAX_RAW_LOG_BYTES).decode("utf-8", errors="replace")
            except (urllib.error.URLError, TimeoutError):
                if not fetched_primary:
                    parts.append(f"(Could not fetch raw log: {view_url})")
                continue

            if not log_text.strip():
                continue
            if spool_dir:
                safe_step = re.sub(r"[^A-Za-z0-9_.-]+", "_", s_name)[:60]
                safe_log = re.sub(r"[^A-Za-z0-9_.-]+", "_", log_name)[:20]
                (spool_dir / f"luci_{build_id}_{safe_step}_{safe_log}.log").write_text(log_text, encoding="utf-8")

            if not fetched_primary:
                fetched_primary = True
                primary_had_marker = any(CI_FAILURE_LINE_RE.search(ln) for ln in log_text.splitlines())
                parts.append(extract_failure_windows(log_text, max_lines=max_lines))
            else:
                parts.append(
                    f"### Step `{log_name}`\n"
                    + extract_failure_windows(log_text, max_lines=max(20, max_lines // 2))
                )
                break

    return "\n\n".join(parts) + "\n"


def parse_flutter_dashboard_url(target: str, default_repo: str | None = None) -> dict[str, str] | None:
    """Parse `https://flutter-dashboard.appspot.com/#/presubmit?repo=...&sha=...` (or root URL)."""
    try:
        parsed = urllib.parse.urlsplit(target.strip())
    except ValueError:
        return None
    host = (parsed.netloc or "").lower()
    if host != "flutter-dashboard.appspot.com" and not host.endswith(".flutter-dashboard.appspot.com"):
        return None

    params: dict[str, list[str]] = {}
    if parsed.query:
        params.update(urllib.parse.parse_qs(parsed.query))
    if "?" in (parsed.fragment or ""):
        frag_query = parsed.fragment.split("?", 1)[1]
        for k, v in urllib.parse.parse_qs(frag_query).items():
            params.setdefault(k, v)

    def_owner, def_name = "flutter", "flutter"
    if default_repo and "/" in default_repo:
        def_owner, def_name = default_repo.split("/", 1)

    owner = ((params.get("owner") or [""])[0]).strip() or def_owner
    repo_name = ((params.get("repo") or [""])[0]).strip() or def_name
    sha = ((params.get("sha") or [""])[0]).strip()
    return {"owner": owner, "repo": repo_name, "sha": sha}


def search_luci_presubmit_builds_by_sha(
    sha: str,
    *,
    project: str = "flutter",
    bucket: str = "try",
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    """Query Buildbucket `SearchBuilds` for `sha/git/<sha>` and return latest failing builds per builder."""
    all_builds: list[dict[str, Any]] = []
    page_token = ""
    for _ in range(max_pages):
        req_body: dict[str, Any] = {
            "predicate": {
                "builder": {"project": project, "bucket": bucket},
                "tags": [{"key": "buildset", "value": f"sha/git/{sha}"}],
            },
            "pageSize": 1000,
            "mask": {"fields": "id,builder,status,createTime,summaryMarkdown,input.properties"},
        }
        if page_token:
            req_body["pageToken"] = page_token
        _, res = _post_buildbucket_prpc("SearchBuilds", req_body)
        all_builds.extend(b for b in (res.get("builds") or []) if isinstance(b, dict))
        page_token = str(res.get("nextPageToken") or "")
        if not page_token:
            break

    by_builder: dict[str, list[dict[str, Any]]] = {}
    for b in all_builds:
        bname = ((b.get("builder") or {}).get("builder") or "").strip()
        if bname:
            by_builder.setdefault(bname, []).append(b)

    failing: list[dict[str, Any]] = []
    for bname in sorted(by_builder):
        attempts = by_builder[bname]
        attempts.sort(
            key=lambda item: (
                str(item.get("createTime") or ""),
                int(item["id"]) if str(item.get("id", "")).isdigit() else 0,
            ),
            reverse=True,
        )
        latest = attempts[0]
        props = ((latest.get("input") or {}).get("properties") or {})
        if props.get("bringup") is True:
            continue
        status = str(latest.get("status") or "").upper()
        if status in {"FAILURE", "INFRA_FAILURE", "CANCELED"}:
            failing.append({
                "stage": "try",
                "job_name": bname,
                "status": status,
                "build_id": str(latest.get("id") or "").strip(),
                "attempt_number": len(attempts),
                "total_attempts": len(attempts),
                "summary": str(latest.get("summaryMarkdown") or "").strip(),
            })
    return failing


def _fetch_cocoon_json(url: str, *, timeout: int = 20) -> Any:
    """GET JSON from `flutter-dashboard.appspot.com/api/public/...`."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return raw, json.loads(raw)


def fetch_flutter_dashboard_presubmit_summary(
    owner: str,
    repo_name: str,
    sha: str,
    spool_dir: Path | None = None,
    *,
    max_lines: int = 120,
) -> str:
    """Resolve Flutter's consolidated `Dashboard Checks` URL into failing LUCI job logs."""
    failing_entries: list[dict[str, Any]] = []
    counts_note = ""
    guard_ok = False
    guard_err: Exception | None = None

    guard_qs = urllib.parse.urlencode({"owner": owner, "repo": repo_name, "sha": sha})
    guard_url = f"https://flutter-dashboard.appspot.com/api/public/get-presubmit-guard?{guard_qs}"
    try:
        guard_raw, guard_data = _fetch_cocoon_json(guard_url, timeout=20)
        if isinstance(guard_data, dict):
            guard_ok = True
            if spool_dir:
                (spool_dir / f"dashboard_guard_{sha[:12]}.json").write_text(guard_raw, encoding="utf-8")
            check_run_id = guard_data.get("check_run_id")
            succeeded = 0
            in_progress = 0
            neutral = 0
            for st in (guard_data.get("stages") or []):
                if not isinstance(st, dict):
                    continue
                st_name = str(st.get("name") or "default")
                for job_name, raw_status in (st.get("jobs") or {}).items():
                    s_val = str(raw_status or "").strip()
                    s_low = s_val.lower()
                    if s_low in COCOON_FAILING_TASK_STATUSES:
                        failing_entries.append({
                            "stage": st_name,
                            "job_name": str(job_name),
                            "status": s_val,
                            "build_id": "",
                            "attempt_number": 0,
                            "total_attempts": 0,
                            "summary": "",
                        })
                    elif s_low == "succeeded":
                        succeeded += 1
                    elif s_low in {"in progress", "new"}:
                        in_progress += 1
                    else:
                        neutral += 1
            counts_note = (
                f", {succeeded} succeeded, {in_progress} in progress, {neutral} neutral/skipped"
            )

            if check_run_id and failing_entries:
                def _load_job(entry: dict[str, Any]) -> None:
                    j_qs = urllib.parse.urlencode({
                        "check_run_id": str(check_run_id),
                        "job_name": entry["job_name"],
                        "repo": repo_name,
                        "owner": owner,
                    })
                    j_url = f"https://flutter-dashboard.appspot.com/api/public/get-presubmit-job?{j_qs}"
                    try:
                        _, attempts = _fetch_cocoon_json(j_url, timeout=15)
                    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
                        return
                    if isinstance(attempts, list) and attempts:
                        valid = [a for a in attempts if isinstance(a, dict)]
                        if not valid:
                            return
                        valid.sort(
                            key=lambda a: (
                                int(a.get("attempt_number") or 0),
                                int(a.get("creation_time") or 0),
                            ),
                            reverse=True,
                        )
                        latest = valid[0]
                        entry["build_id"] = str(latest.get("build_id") or "").strip()
                        entry["attempt_number"] = int(latest.get("attempt_number") or len(valid))
                        entry["total_attempts"] = len(valid)
                        entry["summary"] = str(latest.get("summary") or "").strip()

                if len(failing_entries) == 1:
                    _load_job(failing_entries[0])
                else:
                    with ThreadPoolExecutor(max_workers=min(4, len(failing_entries))) as pool:
                        list(pool.map(_load_job, failing_entries))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        guard_err = exc

    # Fallback to Buildbucket SearchBuilds if Cocoon guard failed or any failing job lacks a build_id.
    if (not guard_ok) or any(not e.get("build_id") for e in failing_entries):
        try:
            bb_failing = search_luci_presubmit_builds_by_sha(sha, project=owner)
        except ToolError as bb_exc:
            if not guard_ok:
                raise ToolError(
                    f"Failed to resolve Flutter Dashboard presubmit for {owner}/{repo_name}@{sha}: "
                    f"dashboard error ({guard_err}); Buildbucket fallback error ({bb_exc})"
                ) from bb_exc
            bb_failing = []

        if not guard_ok:
            failing_entries = bb_failing
        else:
            by_name = {b["job_name"]: b for b in bb_failing}
            for entry in failing_entries:
                if not entry.get("build_id") and entry["job_name"] in by_name:
                    fb = by_name[entry["job_name"]]
                    entry["build_id"] = fb["build_id"]
                    entry["attempt_number"] = entry["attempt_number"] or fb["attempt_number"]
                    entry["total_attempts"] = entry["total_attempts"] or fb["total_attempts"]
                    if not entry.get("summary"):
                        entry["summary"] = fb["summary"]

    header = (
        f"# Flutter Dashboard Presubmit (`{owner}/{repo_name}` @ `{sha[:12]}`) — "
        f"{len(failing_entries)} failing job(s){counts_note}"
    )
    if not failing_entries:
        return header + "\n\nNo failing presubmit jobs found for this commit.\n"

    lines: list[str] = [header, "## Failing Presubmit Jobs"]
    job_list: list[str] = []
    for entry in failing_entries:
        att = (
            f", attempt {entry['attempt_number']}/{entry['total_attempts']}"
            if entry.get("total_attempts")
            else ""
        )
        b_url = f" — https://ci.chromium.org/b/{entry['build_id']}" if entry.get("build_id") else ""
        job_list.append(f"- `{entry['job_name']}` ({entry['status']}{att}){b_url}")
    lines.append("\n".join(job_list))

    detailed = failing_entries[:MAX_DASHBOARD_DETAILED_BUILDS]
    overflow = failing_entries[MAX_DASHBOARD_DETAILED_BUILDS:]
    per_build_lines = max_lines if len(detailed) == 1 else max(45, min(80, max_lines))

    def _render_detailed(entry: dict[str, Any]) -> str:
        bid = entry.get("build_id") or ""
        if bid:
            try:
                return fetch_luci_build_summary(bid, spool_dir, max_lines=per_build_lines).rstrip()
            except ToolError as exc:
                fallback = extract_failure_windows(entry.get("summary") or "", max_lines=40)
                return f"# Job `{entry['job_name']}` (build {bid} lookup error: {exc})\n\n{fallback}"
        fallback = extract_failure_windows(entry.get("summary") or "(no summary available)", max_lines=40)
        return f"# Job `{entry['job_name']}` ({entry['status']})\n\n{fallback}"

    if len(detailed) == 1:
        lines.append(_render_detailed(detailed[0]))
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(detailed))) as pool:
            lines.extend(pool.map(_render_detailed, detailed))

    if overflow:
        extra_blocks = [
            f"## Additional Failing Jobs ({len(overflow)} more; pass build URL to `pr_tool.py ci-log` for full step logs)"
        ]
        for entry in overflow:
            b_ref = f"https://ci.chromium.org/b/{entry['build_id']}" if entry.get("build_id") else "no build_id"
            extra_blocks.append(f"### `{entry['job_name']}` ({b_ref})")
            if entry.get("summary"):
                extra_blocks.append(extract_failure_windows(entry["summary"], max_lines=15))
        lines.append("\n\n".join(extra_blocks))

    return "\n\n".join(lines) + "\n"


def _resolve_head_sha_for_ci(
    repo: str | None,
    pr_num: int | None,
    spool_dir: Path,
    gh_cwd: Path | None,
) -> str | None:
    """Resolve PR HEAD SHA from spooled `state.json` or `gh pr view` when needed."""
    st_file = spool_dir / "state.json"
    if st_file.is_file():
        try:
            st = json.loads(st_file.read_text(encoding="utf-8"))
            sha = str(st.get("head_sha") or "").strip()
            if sha:
                return sha
        except (OSError, json.JSONDecodeError):
            pass
    if repo and pr_num is not None:
        out = run_gh(
            ["pr", "view", str(pr_num), "--repo", repo, "--json", "headRefOid", "--jq", ".headRefOid"],
            cwd=gh_cwd,
            check=False,
        ).strip()
        if out:
            return out
    return None


def cmd_ci_log(args: argparse.Namespace) -> int:
    """Fetch GitHub Actions `--log-failed`, Flutter Dashboard Presubmit, or LUCI Buildbucket failure windows."""
    worktree = Path(args.worktree).resolve() if args.worktree else Path.cwd()
    gh_cwd = worktree if worktree.is_dir() else None
    spool_root = Path(args.spool_root) if args.spool_root else None
    pr_num = int(args.pr) if args.pr is not None else None

    repo: str | None = None
    if args.repo or pr_num is not None:
        try:
            repo = normalize_repo_slug(args.repo, gh_cwd, pr_number=pr_num, spool_root=spool_root)
        except ToolError:
            repo = None

    spool_dir = (
        resolve_spool_dir(repo, pr_num, spool_root)
        if (repo and pr_num is not None)
        else (spool_root or DEFAULT_SPOOL_ROOT)
    )
    spool_dir.mkdir(parents=True, exist_ok=True)
    target = args.target.strip()

    # Case 1: LUCI Buildbucket URL (`/b/<id>`, `/b<id>`, `/build/<id>`) or 16+ digit build ID
    luci_m = re.search(r"(?:/b/|/b|/build/)(\d{15,})\b", target) or re.match(r"^(\d{16,})$", target)
    if luci_m:
        sys.stdout.write(fetch_luci_build_summary(luci_m.group(1), spool_dir, max_lines=args.max_lines))
        return 0

    # Case 2: Flutter consolidated Dashboard URL (`https://flutter-dashboard.appspot.com/#/presubmit?repo=...&sha=...`)
    dash = parse_flutter_dashboard_url(target, default_repo=repo)
    if dash is not None:
        owner = dash["owner"]
        repo_name = dash["repo"]
        sha = dash["sha"]
        if not sha:
            full_repo = repo or f"{owner}/{repo_name}"
            sha = _resolve_head_sha_for_ci(full_repo, pr_num, spool_dir, gh_cwd) or ""
            if not sha:
                raise ToolError(
                    "Flutter Dashboard URL has no `sha=` parameter; pass `--pr <N> --repo <owner/repo>` "
                    "or use the `#/presubmit?repo=...&sha=<sha>` URL."
                )
            # Check GitHub check-runs first in case a non-LUCI dashboard check (e.g. `ci.yaml validation`) failed.
            cr_proc = run_cmd(
                ["gh", "api", f"repos/{full_repo}/commits/{sha}/check-runs"],
                cwd=gh_cwd,
                check=False,
            )
            printed_check_output = False
            need_presubmit = False
            if cr_proc.returncode == 0 and cr_proc.stdout.strip():
                try:
                    cr_data = json.loads(cr_proc.stdout)
                    for cr in (cr_data.get("check_runs") or []):
                        if not isinstance(cr, dict):
                            continue
                        conc = str(cr.get("conclusion") or "").upper()
                        d_url = str(cr.get("details_url") or "")
                        if conc not in FAILURE_CONCLUSIONS or "flutter-dashboard.appspot.com" not in d_url:
                            continue
                        cr_name = str(cr.get("name") or "?")
                        if cr_name == "Dashboard Checks" or "#/presubmit" in d_url:
                            need_presubmit = True
                            continue
                        out_obj = cr.get("output") or {}
                        body_txt = "\n\n".join(
                            x.strip()
                            for x in (out_obj.get("title") or "", out_obj.get("summary") or "", out_obj.get("text") or "")
                            if isinstance(x, str) and x.strip()
                        )
                        print(f"# Check Run `{cr_name}` ({conc})\n{body_txt or '(no check-run output text)'}\n")
                        printed_check_output = True
                except json.JSONDecodeError:
                    pass
            if printed_check_output and not need_presubmit:
                return 0

        sys.stdout.write(
            fetch_flutter_dashboard_presubmit_summary(
                owner,
                repo_name,
                sha,
                spool_dir,
                max_lines=args.max_lines,
            )
        )
        return 0

    # Case 3: GitHub Actions run URL or numeric run_id
    gh_m = re.search(r"(?:https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?/actions/runs/(\d+)", target)
    raw_id_m = None if gh_m else re.match(r"^(\d+)$", target)
    if gh_m or raw_id_m:
        url_repo = gh_m.group(1) if gh_m else None
        run_id = gh_m.group(2) if gh_m else raw_id_m.group(1)  # type: ignore[union-attr]
        effective_repo = repo or url_repo
        cmd = ["run", "view", run_id, "--log-failed"]
        if effective_repo:
            cmd.extend(["--repo", effective_repo])
        raw_log = run_gh(cmd, cwd=gh_cwd)
        log_file = spool_dir / f"gh_run_{run_id}.log"
        log_file.write_text(raw_log, encoding="utf-8")
        print(f"# GitHub Actions Run {run_id} (full failed log saved to {log_file})")
        print(extract_failure_windows(raw_log, max_lines=args.max_lines))
        return 0

    # Case 4: GitHub CheckRun URL (`https://github.com/<owner>/<repo>/runs/<check_run_id>`)
    cr_m = re.search(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/runs/(\d+)", target)
    if cr_m:
        cr_repo, cr_id = cr_m.group(1), cr_m.group(2)
        cr_raw = run_gh(["api", f"repos/{cr_repo}/check-runs/{cr_id}"], cwd=gh_cwd)
        cr_obj = json.loads(cr_raw)
        details_url = str(cr_obj.get("details_url") or "").strip()
        if details_url and details_url != target and (
            parse_flutter_dashboard_url(details_url, default_repo=cr_repo) is not None
            or re.search(r"(?:/b/|/b|/build/)(\d{15,})\b", details_url)
        ):
            delegated = argparse.Namespace(**vars(args))
            delegated.target = details_url
            delegated.repo = args.repo or cr_repo
            return cmd_ci_log(delegated)
        out_obj = cr_obj.get("output") or {}
        text_parts = [
            f"# GitHub Check Run `{cr_obj.get('name', cr_id)}` — conclusion: {cr_obj.get('conclusion', 'UNKNOWN')}",
        ]
        for field in ("title", "summary", "text"):
            val = (out_obj.get(field) or "").strip()
            if val:
                text_parts.append(val)
        print(extract_failure_windows("\n\n".join(text_parts), max_lines=args.max_lines))
        return 0

    raise ToolError(
        f"Unrecognized CI target '{target}'. Pass a GitHub Actions run ID/URL, "
        "Flutter Dashboard presubmit URL, or LUCI Buildbucket URL/ID."
    )


def slugify_github_heading(heading_text: str) -> str:
    """Compute GitHub Markdown heading anchor slug (stripping links/emphasis/backticks)."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", heading_text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("`", "").replace("*", "").replace("_", "_")
    text = text.strip().lower()
    # GitHub's slugger removes punctuation except `-`, `_`, and spaces, then converts spaces to `-`.
    cleaned_chars: list[str] = []
    for ch in text:
        if ch.isalnum() or ch in {" ", "-", "_"}:
            cleaned_chars.append(ch)
    slug = "".join(cleaned_chars)
    slug = slug.replace(" ", "-")
    return slug


def parse_style_guide(md_text: str) -> list[dict[str, Any]]:
    """Index Markdown headings with exact GitHub `#<anchor>` slugs and line spans."""
    lines = md_text.splitlines()
    sections: list[dict[str, Any]] = []
    seen_slugs: dict[str, int] = {}
    in_fence = False

    for idx, line in enumerate(lines, start=1):
        if re.match(r"^\s*(`{3,}|~{3,})", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if not m:
            continue
        level = len(m.group(1))
        title = m.group(2).strip()
        base_slug = slugify_github_heading(title)
        if not base_slug:
            continue
        if base_slug in seen_slugs:
            seen_slugs[base_slug] += 1
            anchor = f"{base_slug}-{seen_slugs[base_slug]}"
        else:
            seen_slugs[base_slug] = 0
            anchor = base_slug
        sections.append({
            "level": level,
            "title": title,
            "anchor": anchor,
            "start_line": idx,
            "end_line": len(lines),
        })

    # Compute section end_line (up to next heading of same or higher level).
    for i, sec in enumerate(sections):
        for j in range(i + 1, len(sections)):
            if sections[j]["level"] <= sec["level"]:
                sec["end_line"] = sections[j]["start_line"] - 1
                break
    return sections


def resolve_style_guide_path(worktree: Path) -> Path:
    """Locate `docs/contributing/Style-guide-for-Flutter-repo.md` in the worktree."""
    candidates = [
        worktree / "docs" / "contributing" / "Style-guide-for-Flutter-repo.md",
        worktree / "Style-guide-for-Flutter-repo.md",
    ]
    for p in candidates:
        if p.is_file():
            return p
    raise ToolError(
        f"Style-guide-for-Flutter-repo.md not found under {worktree}. "
        "Pass --style-file if reviewing outside flutter/flutter."
    )


def cmd_style(args: argparse.Namespace) -> int:
    """Output verified TOC anchors or single sections from Style-guide-for-Flutter-repo.md."""
    worktree = Path(args.worktree).resolve() if args.worktree else Path.cwd()
    style_path = Path(args.style_file).resolve() if args.style_file else resolve_style_guide_path(worktree)
    text = style_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    sections = parse_style_guide(text)

    if args.section:
        query = args.section.lstrip("#").strip().lower()
        exact = [s for s in sections if s["anchor"] == query]
        matches = exact or [s for s in sections if query in s["anchor"] or query in s["title"].lower()]
        if not matches:
            raise ToolError(f"No style-guide heading matched '{args.section}'. Run with `--toc` first.")
        chosen = matches[0]
        s_ln, e_ln = chosen["start_line"], min(chosen["end_line"], chosen["start_line"] + 160)
        url = (
            "https://github.com/flutter/flutter/blob/master/"
            f"docs/contributing/Style-guide-for-Flutter-repo.md#{chosen['anchor']}"
        )
        print(f"# Section: {chosen['title']} (L{s_ln}-L{chosen['end_line']})")
        print(f"# Verified URL: {url}\n")
        print("\n".join(lines[s_ln - 1 : e_ln]))
        if chosen["end_line"] > e_ln:
            print(f"\n... [section continues to L{chosen['end_line']}]")
        return 0

    # Default / --toc: compact table of contents with verified `#anchor`
    print(f"# Style Guide Headings ({len(sections)} anchors from {style_path})")
    for s in sections:
        indent = "  " * (s["level"] - 1)
        print(f"{indent}- {s['title']}  ->  #{s['anchor']}  [L{s['start_line']}-L{s['end_line']}]")
    return 0



def mask_dart_comments_and_strings(src: str) -> str:
    """Replace Dart comments and string contents with spaces while preserving newlines and `${}` balance."""
    out = list(src)
    n = len(src)
    i = 0
    # Stack entries for string interpolation: (quote_str, is_raw, brace_depth_inside_interp)
    interp_stack: list[tuple[str, bool, int]] = []

    def scan_string_body(start_body: int, quote: str, is_raw: bool) -> int:
        qlen = len(quote)
        j = start_body
        while j < n:
            if src.startswith(quote, j):
                for k in range(j, min(n, j + qlen)):
                    if out[k] != "\n":
                        out[k] = " "
                return j + qlen
            ch = src[j]
            if not is_raw and ch == "\\":
                if out[j] != "\n":
                    out[j] = " "
                if j + 1 < n and out[j + 1] != "\n":
                    out[j + 1] = " "
                j += 2
                continue
            if not is_raw and src.startswith("${", j):
                out[j] = " "
                out[j + 1] = "{"
                interp_stack.append((quote, is_raw, 1))
                return j + 2
            if ch != "\n":
                out[j] = " "
            j += 1
        return n

    def enter_string(pos: int, quote: str, is_raw: bool) -> int:
        qlen = len(quote)
        for k in range(pos, min(n, pos + qlen)):
            if out[k] != "\n":
                out[k] = " "
        return scan_string_body(pos + qlen, quote, is_raw)

    while i < n:
        # Line comment
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
            continue
        # Nested block comment
        if src.startswith("/*", i):
            depth = 1
            out[i] = " "
            out[i + 1] = " "
            i += 2
            while i < n and depth > 0:
                if src.startswith("/*", i):
                    depth += 1
                    out[i] = " "
                    out[i + 1] = " "
                    i += 2
                elif src.startswith("*/", i):
                    depth -= 1
                    out[i] = " "
                    out[i + 1] = " "
                    i += 2
                else:
                    if out[i] != "\n":
                        out[i] = " "
                    i += 1
            continue
        # Interpolation brace tracking
        if interp_stack:
            if src[i] == "{":
                q, raw_f, bdepth = interp_stack[-1]
                interp_stack[-1] = (q, raw_f, bdepth + 1)
                i += 1
                continue
            if src[i] == "}":
                q, raw_f, bdepth = interp_stack[-1]
                if bdepth == 1:
                    interp_stack.pop()
                    out[i] = "}"
                    i = scan_string_body(i + 1, q, raw_f)
                    continue
                interp_stack[-1] = (q, raw_f, bdepth - 1)
                i += 1
                continue
        # Raw strings
        if src.startswith(("r'''", 'r"""'), i):
            q = src[i + 1 : i + 4]
            out[i] = " "
            i = enter_string(i + 1, q, True)
            continue
        if src.startswith(("r'", 'r"'), i):
            q = src[i + 1]
            out[i] = " "
            i = enter_string(i + 1, q, True)
            continue
        # Normal triple/single strings
        if src.startswith(("'''", '"""'), i):
            i = enter_string(i, src[i : i + 3], False)
            continue
        if src[i] in {"'", '"'}:
            i = enter_string(i, src[i], False)
            continue
        i += 1

    return "".join(out)


TYPE_DECL_RE = re.compile(
    r"\b(class|mixin|extension(?:\s+type)?|enum)\s+([A-Za-z_$][A-Za-z0-9_$]*)"
)
IDENT_RE = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\b")
DART_KEYWORDS = frozenset({
    "abstract", "as", "assert", "async", "await", "base", "break", "case", "catch",
    "class", "const", "continue", "covariant", "default", "deferred", "do", "dynamic",
    "else", "enum", "export", "extends", "extension", "external", "factory", "false",
    "final", "finally", "for", "Function", "get", "hide", "if", "implements", "import",
    "in", "interface", "is", "late", "library", "mixin", "new", "null", "of", "on",
    "operator", "part", "required", "rethrow", "return", "sealed", "set", "show",
    "static", "super", "switch", "sync", "this", "throw", "true", "try", "typedef",
    "var", "void", "when", "while", "with", "yield", "int", "double", "num", "bool",
    "String", "List", "Map", "Set", "Object", "Future", "Stream", "Iterable", "Widget",
    "BuildContext", "Key", "State",
})


def _doc_start_for_line(raw_lines: list[str], decl_line_1based: int) -> int:
    """Walk upward from decl_line_1based across contiguous `///` comments and `@` annotations."""
    ln = decl_line_1based
    while ln > 1:
        prev = raw_lines[ln - 2].strip()
        if prev.startswith("///") or prev.startswith("@"):
            ln -= 1
        else:
            break
    return ln


def _parse_class_members(
    raw_lines: list[str],
    masked_lines: list[str],
    body_start_line: int,
    body_end_line: int,
    class_name: str,
) -> list[dict[str, Any]]:
    """Scan brace-depth-1 declarations inside a Dart class/mixin/extension body."""
    members: list[dict[str, Any]] = []
    depth = 0
    paren = 0
    stmt_start: int | None = None
    stmt_tokens: list[str] = []

    for ln in range(body_start_line, body_end_line + 1):
        mline = masked_lines[ln - 1]
        for idx, ch in enumerate(mline):
            # Ignore opening `{` of the class itself on body_start_line before depth hits 1.
            if depth == 0:
                if ch == "{":
                    depth = 1
                continue
            if depth == 1 and stmt_start is None and not ch.isspace() and ch != "}":
                stmt_start = ln
                stmt_tokens = []
            if stmt_start is not None and depth == 1:
                stmt_tokens.append(ch)

            if ch == "(":
                paren += 1
            elif ch == ")" and paren > 0:
                paren -= 1
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return members
                if depth == 1 and paren == 0 and stmt_start is not None:
                    members.append(_classify_member(raw_lines, stmt_start, ln, "".join(stmt_tokens), class_name))
                    stmt_start = None
                    stmt_tokens = []
            elif ch == ";" and depth == 1 and paren == 0 and stmt_start is not None:
                members.append(_classify_member(raw_lines, stmt_start, ln, "".join(stmt_tokens), class_name))
                stmt_start = None
                stmt_tokens = []

    return members


def _classify_member(
    raw_lines: list[str],
    start_ln: int,
    end_ln: int,
    header_src: str,
    class_name: str,
) -> dict[str, Any]:
    """Classify a depth-1 class declaration into field, getter, setter, constructor, or method."""
    # Trim body after `{` or `=>` or `=` to inspect signature only.
    sig = header_src
    for sep in ("=>", "{", "="):
        pos = sig.find(sep)
        if pos != -1:
            sig = sig[:pos]
    sig_compact = re.sub(r"\s+", " ", sig).strip().rstrip(";")

    has_arrow_or_body = ("=>" in header_src) or ("{" in header_src)
    has_parens = "(" in sig_compact

    kind = "field"
    name = "?"
    if re.search(r"\bget\s+([A-Za-z_$][A-Za-z0-9_$]*)", sig_compact):
        m = re.search(r"\bget\s+([A-Za-z_$][A-Za-z0-9_$]*)", sig_compact)
        kind = "getter"
        name = m.group(1) if m else "?"
    elif re.search(r"\bset\s+([A-Za-z_$][A-Za-z0-9_$]*)", sig_compact):
        m = re.search(r"\bset\s+([A-Za-z_$][A-Za-z0-9_$]*)", sig_compact)
        kind = "setter"
        name = m.group(1) if m else "?"
    elif has_parens:
        before_paren = sig_compact.split("(", 1)[0].strip()
        m_ctor = re.search(rf"\b{re.escape(class_name)}(?:\.([A-Za-z_$][A-Za-z0-9_$]*))?$", before_paren)
        if m_ctor:
            kind = "constructor"
            name = f"{class_name}.{m_ctor.group(1)}" if m_ctor.group(1) else class_name
        else:
            kind = "method"
            ids = IDENT_RE.findall(before_paren)
            name = ids[-1] if ids else "?"
    elif has_arrow_or_body:
        kind = "method"
        ids = IDENT_RE.findall(sig_compact)
        name = ids[-1] if ids else "?"
    else:
        kind = "field"
        # Handle multi-variable or typed field declarations; take declared identifier before `,` or end.
        first_decl = sig_compact.split(",", 1)[0].strip()
        ids = IDENT_RE.findall(first_decl)
        name = ids[-1] if ids else "?"

    doc_start = _doc_start_for_line(raw_lines, start_ln)
    return {
        "kind": kind,
        "name": name,
        "doc_start_line": doc_start,
        "start_line": start_ln,
        "end_line": end_ln,
        "signature": sig_compact[:110],
    }


def parse_dart_outline(src: str) -> dict[str, Any]:
    """Parse top-level Dart declarations and class/mixin/extension members with line spans."""
    raw_lines = src.splitlines()
    masked = mask_dart_comments_and_strings(src)
    masked_lines = masked.splitlines()

    declarations: list[dict[str, Any]] = []
    depth = 0
    paren = 0
    stmt_start: int | None = None
    stmt_chars: list[str] = []

    for ln, mline in enumerate(masked_lines, start=1):
        for ch in mline:
            if depth == 0 and stmt_start is None and not ch.isspace():
                stmt_start = ln
                stmt_chars = []
            if stmt_start is not None and depth == 0:
                stmt_chars.append(ch)

            if ch == "(":
                paren += 1
            elif ch == ")" and paren > 0:
                paren -= 1
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth = max(0, depth - 1)
                if depth == 0 and paren == 0 and stmt_start is not None:
                    header = re.sub(r"\s+", " ", "".join(stmt_chars)).strip()
                    m_type = TYPE_DECL_RE.search(header)
                    if m_type:
                        kind = m_type.group(1).split()[0]
                        tname = m_type.group(2)
                        members = _parse_class_members(raw_lines, masked_lines, stmt_start, ln, tname)
                        declarations.append({
                            "kind": kind,
                            "name": tname,
                            "doc_start_line": _doc_start_for_line(raw_lines, stmt_start),
                            "start_line": stmt_start,
                            "end_line": ln,
                            "members": members,
                        })
                    else:
                        before_p = header.split("(", 1)[0].strip()
                        ids = IDENT_RE.findall(before_p)
                        fn_name = ids[-1] if ids else "?"
                        declarations.append({
                            "kind": "function",
                            "name": fn_name,
                            "doc_start_line": _doc_start_for_line(raw_lines, stmt_start),
                            "start_line": stmt_start,
                            "end_line": ln,
                            "members": [],
                        })
                    stmt_start = None
                    stmt_chars = []
            elif ch == ";" and depth == 0 and paren == 0 and stmt_start is not None:
                header = re.sub(r"\s+", " ", "".join(stmt_chars)).strip()
                if not header.startswith(("import ", "export ", "part ", "library ")):
                    before_eq = header.split("=", 1)[0].split("(", 1)[0].strip().rstrip(";")
                    ids = IDENT_RE.findall(before_eq)
                    if ids:
                        declarations.append({
                            "kind": "top_level",
                            "name": ids[-1],
                            "doc_start_line": _doc_start_for_line(raw_lines, stmt_start),
                            "start_line": stmt_start,
                            "end_line": ln,
                            "members": [],
                        })
                stmt_start = None
                stmt_chars = []

    return {
        "total_lines": len(raw_lines),
        "declarations": declarations,
        "_masked_lines": masked_lines,
        "_raw_lines": raw_lines,
    }



def _merge_line_spans(spans: list[tuple[int, int]], gap: int = 3) -> list[tuple[int, int]]:
    """Merge overlapping or nearly-adjacent `[start, end]` line ranges."""
    if not spans:
        return []
    ordered = sorted(spans)
    merged: list[tuple[int, int]] = [ordered[0]]
    for s, e in ordered[1:]:
        ps, pe = merged[-1]
        if s <= pe + gap:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def slice_dart_for_diff(
    outline: dict[str, Any],
    *,
    touched_lines: set[int],
    changed_code_lines: list[str],
    extra_symbols: list[str] | None = None,
) -> str:
    """Build a state- and lifecycle-aware reading map for a Dart source file."""
    total = outline["total_lines"]
    masked_lines: list[str] = outline["_masked_lines"]
    decls: list[dict[str, Any]] = outline["declarations"]

    out: list[str] = [f"# Dart Subsystem Slice ({total} lines total)"]
    if total <= 1000:
        out.append(
            f"NOTE: File is {total} lines (<= 1,000) — read the full file (L1-L{total}) "
            "to keep complete subsystem context."
        )

    # Candidate identifiers from changed diff lines (excluding keywords and 1-2 char locals).
    diff_idents: set[str] = set(extra_symbols or [])
    for code in changed_code_lines:
        masked_frag = mask_dart_comments_and_strings(code)
        for ident in IDENT_RE.findall(masked_frag):
            if ident not in DART_KEYWORDS and (ident.startswith("_") or len(ident) >= 3):
                diff_idents.add(ident)

    touched_decls = [
        d for d in decls
        if any(d["doc_start_line"] <= ln <= d["end_line"] for ln in touched_lines)
        or (extra_symbols and d["name"] in extra_symbols)
    ]
    if not touched_decls and decls:
        out.append("\n## Top-Level Outline (no specific diff lines matched)")
        for d in decls:
            out.append(f"- {d['kind']} `{d['name']}` [L{d['doc_start_line']}-L{d['end_line']}]")
        return "\n".join(out) + "\n"

    for d in touched_decls:
        out.append(
            f"\n## Enclosing {d['kind']} `{d['name']}` [L{d['doc_start_line']}-L{d['end_line']}]"
        )
        members: list[dict[str, Any]] = d.get("members") or []
        if not members:
            out.append(f"- Read full {d['kind']}: [L{d['doc_start_line']}-L{d['end_line']}]")
            continue

        header_end = max(d["start_line"], members[0]["doc_start_line"] - 1)
        out.append(f"- Class docs & header: [L{d['doc_start_line']}-L{header_end}]")

        fields = [m for m in members if m["kind"] == "field"]
        field_names = {f["name"] for f in fields if f["name"] != "?"}
        member_names = {m["name"] for m in members if m["name"] != "?"}
        if fields:
            field_spans = _merge_line_spans([(f["doc_start_line"], f["end_line"]) for f in fields], gap=4)
            span_str = ", ".join(f"[L{s}-L{e}]" for s, e in field_spans)
            out.append(f"- All field declarations ({len(fields)} fields): {span_str}")

        touched_members = [
            m for m in members
            if any(m["doc_start_line"] <= ln <= m["end_line"] for ln in touched_lines)
            or (extra_symbols and m["name"] in extra_symbols)
        ]
        if touched_members:
            out.append("- Touched members:")
            for m in touched_members:
                out.append(
                    f"  * {m['kind']} `{m['name']}` [L{m['doc_start_line']}-L{m['end_line']}]"
                )

        # State symbols = class fields/members mentioned in changed code + touched member names.
        tracked_symbols = (diff_idents & (field_names | member_names)) | {
            m["name"] for m in touched_members if m["name"] != "?"
        }
        if extra_symbols:
            tracked_symbols.update(extra_symbols)
        # Remove super-generic lifecycle names from peer symbol search so we don't match `build` calls everywhere.
        state_symbols = {s for s in tracked_symbols if s not in {"build", "toString", "hashCode"}}

        touched_set = {(m["start_line"], m["end_line"]) for m in touched_members}
        lifecycle_members = [
            m for m in members
            if m["name"].strip() in FLUTTER_LIFECYCLE_METHODS
            and (m["start_line"], m["end_line"]) not in touched_set
        ]
        if lifecycle_members:
            out.append("- Lifecycle methods on this class:")
            for m in lifecycle_members:
                out.append(
                    f"  * {m['kind']} `{m['name']}` [L{m['doc_start_line']}-L{m['end_line']}]"
                )

        # Find every other member in this class referencing `state_symbols`.
        if state_symbols:
            sym_pat = re.compile(r"\b(" + "|".join(re.escape(s) for s in sorted(state_symbols)) + r")\b")
            already_listed = touched_set | {(m["start_line"], m["end_line"]) for m in lifecycle_members}
            peer_hits: list[tuple[dict[str, Any], list[str]]] = []
            for m in members:
                if m["kind"] == "field" or (m["start_line"], m["end_line"]) in already_listed:
                    continue
                body_masked = "\n".join(masked_lines[m["start_line"] - 1 : m["end_line"]])
                hits = sorted(set(sym_pat.findall(body_masked)))
                if hits:
                    peer_hits.append((m, hits))
            if peer_hits:
                out.append(f"- Peer members referencing touched state ({', '.join(sorted(state_symbols))}):")
                for m, hits in peer_hits:
                    out.append(
                        f"  * {m['kind']} `{m['name']}` [L{m['doc_start_line']}-L{m['end_line']}] "
                        f"(refs: {', '.join(hits)})"
                    )

    # Also show brief index of any other top-level classes in the same file (coupled Widget/Element/RenderObject).
    untouched_types = [d for d in decls if d not in touched_decls and d["kind"] in {"class", "mixin", "extension", "enum"}]
    if untouched_types:
        out.append("\n## Other Types in This File (check if coupled producer/consumer)")
        for d in untouched_types:
            out.append(f"- {d['kind']} `{d['name']}` [L{d['doc_start_line']}-L{d['end_line']}]")

    return "\n".join(out) + "\n"


TEST_BLOCK_START_RE = re.compile(r"^\s*(testWidgets|test|group)\s*\(\s*(['\"])(.*?)\2")
REGRESSION_COMMENT_RE = re.compile(
    r"(regression test|https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+)",
    re.IGNORECASE,
)


def extract_test_regression_style(
    src: str,
    *,
    symbols: set[str],
) -> str:
    """Index a `*_test.dart` file: shared top helpers, regression comment conventions, and matching test spans."""
    raw_lines = src.splitlines()
    masked = mask_dart_comments_and_strings(src)
    masked_lines = masked.splitlines()
    total = len(raw_lines)

    out: list[str] = [f"# Test File Index ({total} lines)"]

    # 1. Find `void main(` line to bound top-of-file helpers.
    main_ln = 1
    for idx, line in enumerate(masked_lines, start=1):
        if re.search(r"\bmain\s*\(", line):
            main_ln = idx
            break
    if main_ln > 1:
        out.append(f"- Imports & shared top-of-file test helpers: [L1-L{main_ln - 1}]")

    # 2. Sample existing regression comments in this file (up to 5 examples with ±2 lines).
    reg_examples: list[str] = []
    last_hit = -10
    for idx, line in enumerate(raw_lines, start=1):
        if REGRESSION_COMMENT_RE.search(line) and idx > last_hit + 4:
            last_hit = idx
            s = max(1, idx - 1)
            e = min(total, idx + 3)
            snippet = "\n".join(f"    L{ln}: {raw_lines[ln - 1]}" for ln in range(s, e + 1))
            reg_examples.append(snippet)
            if len(reg_examples) >= 5:
                break
    out.append("\n## Existing Regression Comment Conventions in This File")
    if reg_examples:
        out.extend(reg_examples)
    else:
        out.append("  (No existing `Regression test` / issue URL comments found in this file.)")

    # 3. Scan top-level `testWidgets` / `test` / `group` blocks inside `main()` using paren depth.
    blocks: list[dict[str, Any]] = []
    i = main_ln
    while i <= total:
        m = TEST_BLOCK_START_RE.match(raw_lines[i - 1])
        if not m:
            i += 1
            continue
        kind, _, title = m.group(1), m.group(2), m.group(3)
        # Track paren depth from `(` of `testWidgets(` on masked_lines until matching `)`.
        pdepth = 0
        started = False
        end_ln = i
        for ln in range(i, total + 1):
            for ch in masked_lines[ln - 1]:
                if ch == "(":
                    pdepth += 1
                    started = True
                elif ch == ")" and started:
                    pdepth -= 1
                    if pdepth == 0:
                        end_ln = ln
                        break
            if started and pdepth == 0:
                break
        if kind in {"testWidgets", "test"}:
            blocks.append({"kind": kind, "title": title, "start_line": i, "end_line": end_ln})
            i = max(i + 1, end_ln + 1)
        else:
            # Step inside `group(` so nested `testWidgets` blocks are indexed too.
            i += 1

    if blocks:
        out.append(f"\n## Indexed Test Blocks ({len(blocks)} tests; last test ends at [L{blocks[-1]['start_line']}-L{blocks[-1]['end_line']}])")
        if symbols:
            sym_re = re.compile(r"\b(" + "|".join(re.escape(s) for s in sorted(symbols)) + r")\b")
            matched = []
            for b in blocks:
                segment = "\n".join(raw_lines[b["start_line"] - 1 : b["end_line"]])
                hits = sorted(set(sym_re.findall(segment)))
                if hits:
                    matched.append((b, hits))
            if matched:
                out.append(f"Tests referencing ({', '.join(sorted(symbols))}):")
                for b, hits in matched[:20]:
                    out.append(
                        f"- `{b['title']}` [L{b['start_line']}-L{b['end_line']}] (refs: {', '.join(hits)})"
                    )
            else:
                out.append(f"(No existing tests matched symbols: {', '.join(sorted(symbols))})")
        else:
            for b in blocks[:15]:
                out.append(f"- `{b['title']}` [L{b['start_line']}-L{b['end_line']}]")
            if len(blocks) > 15:
                out.append(f"... ({len(blocks) - 15} more; pass `--symbol <Name>` to filter)")

    return "\n".join(out) + "\n"


def cmd_outline(args: argparse.Namespace) -> int:
    """Print Dart file structural outline, `--for-diff` state/lifecycle slice, or `--regression-style`."""
    worktree = Path(args.worktree).resolve() if args.worktree else Path.cwd()
    target = Path(args.file)
    if not target.is_absolute():
        target = (worktree / target).resolve()
    if not target.is_file():
        raise ToolError(f"Dart file not found: {target}")

    src = target.read_text(encoding="utf-8", errors="replace")
    extra_syms = set(args.symbol or [])

    # Load diff context for this file if `--pr` was provided (or auto-discovered).
    touched_lines: set[int] = set()
    changed_code_lines: list[str] = []
    if args.lines:
        for spec in args.lines:
            m = re.match(r"^(\d+)(?:-(\d+))?$", spec.strip())
            if m:
                s = int(m.group(1))
                e = int(m.group(2) or s)
                touched_lines.update(range(s, e + 1))

    if args.pr:
        spool_root = Path(args.spool_root) if args.spool_root else None
        pr_num = int(args.pr)
        repo = normalize_repo_slug(args.repo, worktree, pr_number=pr_num, spool_root=spool_root)
        spool_dir = resolve_spool_dir(repo, pr_num, spool_root)
        diff_patch = spool_dir / "diff.patch"
        if diff_patch.is_file():
            parsed_diff = parse_unified_diff(diff_patch.read_text(encoding="utf-8", errors="replace"))
            rel_candidates = {args.file}
            try:
                rel_candidates.add(str(target.relative_to(worktree)))
            except ValueError:
                pass
            for entry in parsed_diff:
                if entry["path"] in rel_candidates or any(entry["path"].endswith(c) for c in rel_candidates):
                    touched_lines.update(entry["added_right_lines"] or entry["right_lines"])
                    for h in entry["hunks"]:
                        for rl in h["raw_lines"][1:]:
                            if rl.startswith(("+", "-")):
                                changed_code_lines.append(rl[1:])
                elif args.regression_style:
                    # Also harvest changed class/method names from production files in the PR!
                    for h in entry["hunks"]:
                        for rl in h["raw_lines"][1:]:
                            if rl.startswith("+") and not is_comment_or_import_line(rl[1:]):
                                for ident in IDENT_RE.findall(rl[1:]):
                                    if ident not in DART_KEYWORDS and ident[0].isupper():
                                        extra_syms.add(ident)

    if args.regression_style:
        sys.stdout.write(extract_test_regression_style(src, symbols=extra_syms))
        return 0

    outline = parse_dart_outline(src)
    if args.for_diff:
        sys.stdout.write(
            slice_dart_for_diff(
                outline,
                touched_lines=touched_lines,
                changed_code_lines=changed_code_lines,
                extra_symbols=sorted(extra_syms),
            )
        )
        return 0

    print(f"# Dart Outline: {target.name} ({outline['total_lines']} lines)")
    for d in outline["declarations"]:
        print(f"- {d['kind']} `{d['name']}` [L{d['doc_start_line']}-L{d['end_line']}]")
        for m in d.get("members") or []:
            print(f"    * {m['kind']} `{m['name']}` [L{m['doc_start_line']}-L{m['end_line']}]")
    return 0



STYLE_URL_ANCHOR_RE = re.compile(
    r"https://github\.com/flutter/flutter/blob/(?:master|main)/"
    r"docs/contributing/Style-guide-for-Flutter-repo\.md#([A-Za-z0-9_-]+)"
)
FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
HTTP_URL_RE = re.compile(r"https?://\S+")
PROSE_LINE_NUM_RE = re.compile(
    r"(?:"
    r"\b(?:on|at|around|near|see|above|below)\s+lines?\s+\d+\b"
    r"|(?:^|\(\s*)lines?\s+\d+\b"
    r"|\blines?\s+\d+\s*(?:[-–]|\.\.)\s*\d+\b"
    r"|\[R\d+\]"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
PROSE_L_ANCHOR_RE = re.compile(r"\bL\d+(?:\s*[-–]\s*L?\d+)?\b")
BARE_REGRESSION_ISSUE_RE = re.compile(r"regression test for\s+#\d+\b", re.IGNORECASE)
BOOKKEEPING_LEAK_RE = re.compile(r"\b(STOPPED_AT:|RECOMMENDED:|SKIPPED:|COVERAGE_STATUS:)")
VALID_RECOMMENDED = frozenset({"approve", "request changes", "comment", "close", "blocked"})


def _strip_fenced_code(md: str) -> str:
    """Remove fenced/inline code spans and HTTP URLs before prose-level regex checks."""
    without_code = INLINE_CODE_RE.sub("", FENCED_CODE_RE.sub("", md))
    return HTTP_URL_RE.sub("", without_code)


def validate_review_files(
    *,
    draft_json_path: Path,
    draft_md_path: Path | None,
    parsed_diff: list[dict[str, Any]] | None,
    valid_style_anchors: set[str] | None,
    expected_head_sha: str | None = None,
    agent_login: str | None = None,
    has_cicd_label: bool | None = None,
    raw_changed_lines: int | None = None,
) -> list[dict[str, str]]:
    """Validate `drafts/<repo>-<N>.json` and `.md`, returning `{error, fix}` items."""
    issues: list[dict[str, str]] = []

    def err(msg: str, fix: str) -> None:
        issues.append({"error": msg, "fix": fix})

    if not draft_json_path.is_file():
        err(f"Draft JSON not found: {draft_json_path}", "Write the review payload JSON before validating.")
        return issues

    try:
        payload = json.loads(draft_json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        err(f"Invalid JSON in {draft_json_path}: {exc}", "Fix JSON syntax (run `python3 -m json.tool`).")
        return issues

    if not isinstance(payload, dict):
        err("Payload root must be a JSON object.", 'Use `{"commit_id": "...", "body": "...", "comments": []}`.')
        return issues

    if "event" in payload:
        err(
            "Payload contains forbidden `event` key.",
            "Remove `event` from the JSON payload; record the action under `RECOMMENDED:` in the `.md` file.",
        )

    commit_id = payload.get("commit_id")
    body = payload.get("body")
    comments = payload.get("comments")

    if not isinstance(commit_id, str) or not commit_id.strip():
        err("Missing or empty `commit_id`.", "Set `commit_id` to the PR's `<HEAD_SHA>`.")
    elif expected_head_sha and commit_id != expected_head_sha:
        err(
            f"`commit_id` ({commit_id}) does not match PR HEAD SHA ({expected_head_sha}).",
            f'Update `"commit_id"` to `"{expected_head_sha}"`.',
        )

    if not isinstance(body, str):
        err("`body` must be a string.", 'Set `"body"` to a string (use `""` only on Gate 5 no-`CICD` STOP).')
        body = ""
    if not isinstance(comments, list):
        err("`comments` must be a JSON array.", 'Set `"comments"` to `[]` or a list of inline comment objects.')
        comments = []

    # Parse `.md` internal record first so we know if this is Gate 5 no-CICD STOP.
    md_text = ""
    stopped_at_val = ""
    recommended_val = ""
    if draft_md_path is not None:
        if not draft_md_path.is_file():
            err(f"Internal record not found: {draft_md_path}", "Write `drafts/<repo>-<N>.md` alongside the `.json`.")
        else:
            md_text = draft_md_path.read_text(encoding="utf-8", errors="replace")
            m_stop = re.search(r"^STOPPED_AT:\s*(.+)$", md_text, re.MULTILINE)
            m_rec = re.search(r"^RECOMMENDED:\s*([a-z ]+?)(?:\s*[—-].*)?$", md_text, re.MULTILINE)
            has_read = bool(re.search(r"^READ:", md_text, re.MULTILINE))
            has_skipped = bool(re.search(r"^SKIPPED:", md_text, re.MULTILINE))

            if not m_stop:
                err("Internal `.md` is missing `STOPPED_AT:`.", "Add `STOPPED_AT: <gate number or none>` to `.md`.")
            else:
                stopped_at_val = m_stop.group(1).strip()

            if not m_rec:
                err("Internal `.md` is missing `RECOMMENDED:`.", "Add `RECOMMENDED: approve | request changes | comment | close | blocked — <reason>`.")
            else:
                recommended_val = m_rec.group(1).strip().lower()
                if recommended_val not in VALID_RECOMMENDED:
                    err(
                        f"Invalid `RECOMMENDED:` value '{recommended_val}'.",
                        f"Use one of: {', '.join(sorted(VALID_RECOMMENDED))}.",
                    )

            if not has_read:
                err("Internal `.md` is missing `READ:`.", "List files/ranges, threads, docs read, and BASE/PR test outcomes under `READ:`.")
            if not has_skipped:
                err("Internal `.md` is missing `SKIPPED:`.", "Add `SKIPPED: none` or list what was not read/run and why.")

            # Ensure non-signal checks are never listed under SKIPPED.
            skipped_section = ""
            m_skip_sec = re.search(r"^SKIPPED:(.*?)(?:^[A-Z_]+:|\Z)", md_text, re.MULTILINE | re.DOTALL)
            if m_skip_sec:
                skipped_section = m_skip_sec.group(1)
                for non_sig in NOT_A_SIGNAL_CHECKS:
                    if non_sig.lower() in skipped_section.lower():
                        err(
                            f"`SKIPPED:` mentions non-signal check `{non_sig}`.",
                            "Remove Gate 5 non-signal checks (`Google testing`, `Merge Queue Guard`, `flutter-gold`, `tree-status`) from `SKIPPED:`.",
                        )

            # >200-line zero-comment approval rule.
            if (
                recommended_val == "approve"
                and len(comments) == 0
                and (raw_changed_lines or 0) > 200
            ):
                m_read_sec = re.search(r"^READ:(.*?)(?:^SKIPPED:|\Z)", md_text, re.MULTILINE | re.DOTALL)
                read_body = (m_read_sec.group(1) if m_read_sec else "").strip()
                has_specific_path = bool(re.search(r"[A-Za-z0-9_/-]+\.[A-Za-z0-9]+", read_body))
                if not has_specific_path or read_body.lower() in {"the diff", "diff"}:
                    err(
                        "Zero-comment `approve` on a >200-line diff lacks specific file/test names in `READ:`.",
                        "Name the specific source files, line ranges, and tests in `READ:` that justify the approval.",
                    )

    is_no_cicd_stop = (has_cicd_label is False) or (
        "5" in stopped_at_val and recommended_val == "blocked" and body == ""
    )

    # Check body & footer rules.
    if is_no_cicd_stop and has_cicd_label is False:
        if body != "" or len(comments) != 0:
            err(
                "Gate 5 no-`CICD` STOP must have an empty `body` and empty `comments`.",
                'Set `"body": ""` and `"comments": []` when `CICD` label is absent.',
            )
    else:
        expected_login = agent_login or "<agent-login>"
        if agent_login:
            expected_footer = (
                f"— {agent_login} is an AI reviewer; a human maintainer makes the final call "
                "(see this account's profile for who to contact)."
            )
            body_lines = [ln.strip() for ln in body.strip().splitlines()]
            if not body_lines or body_lines[-1] != expected_footer:
                err(
                    "Draft `body` is missing the exact required AI-reviewer footer on its last line.",
                    f"End `body` on its own line with: `{expected_footer}`",
                )
        else:
            if "is an AI reviewer; a human maintainer makes the final call" not in body:
                err(
                    f"Draft `body` is missing the AI-reviewer footer (`— {expected_login} is an AI reviewer...`).",
                    "Append the exact required AI-reviewer footer on its own final line.",
                )

    # Check author-facing texts (body + inline comments) for leaks and anchor validity.
    right_map: dict[str, set[int]] = {}
    if parsed_diff is not None:
        for entry in parsed_diff:
            right_map[entry["path"]] = set(entry["right_lines"])

    all_texts: list[tuple[str, str]] = [("body", body)]
    for idx, c in enumerate(comments):
        loc_label = f"comments[{idx}]"
        if not isinstance(c, dict):
            err(f"{loc_label} is not an object.", "Each inline comment must be `{path, line, side, body}`.")
            continue
        c_path = c.get("path")
        c_line = c.get("line")
        c_side = c.get("side")
        c_body = c.get("body")
        loc_label = f"comments[{idx}] ({c_path}:{c_line})"

        if not isinstance(c_path, str) or not c_path:
            err(f"{loc_label}: missing `path`.", "Set `path` to a repo-relative path from `pr diff`.")
        if not isinstance(c_line, int) or isinstance(c_line, bool):
            err(f"{loc_label}: `line` must be an integer.", "Set `line` to an integer `[R<line>]` from `pr_tool.py diff`.")
        if c_side != "RIGHT":
            err(f"{loc_label}: `side` must be `\"RIGHT\"`.", 'Set `"side": "RIGHT"`.')
        if not isinstance(c_body, str) or not c_body.strip():
            err(f"{loc_label}: `body` must be a non-empty string.", "Provide actionable comment prose.")
            c_body = ""

        start_line = c.get("start_line")
        if start_line is not None:
            if not isinstance(start_line, int) or start_line >= (c_line if isinstance(c_line, int) else 0):
                err(f"{loc_label}: `start_line` ({start_line}) must be an integer < `line` ({c_line}).", "Fix or remove `start_line`.")
            if c.get("start_side", "RIGHT") != "RIGHT":
                err(f"{loc_label}: `start_side` must be `\"RIGHT\"`.", 'Set `"start_side": "RIGHT"`.')

        if parsed_diff is not None and isinstance(c_path, str) and isinstance(c_line, int):
            if c_path not in right_map:
                err(
                    f"{loc_label}: path `{c_path}` is not present in `pr diff`.",
                    "Only anchor inline comments on files modified in `pr diff`.",
                )
            else:
                valid_r = right_map[c_path]
                if c_line not in valid_r:
                    err(
                        f"{loc_label}: line {c_line} is not a right-side diff hunk line in `{c_path}`.",
                        f"Run `pr_tool.py diff --pr <N> --file {c_path}` and choose a `[R<line>]` line.",
                    )
                if isinstance(start_line, int) and start_line not in valid_r:
                    err(
                        f"{loc_label}: `start_line` {start_line} is not a right-side diff hunk line in `{c_path}`.",
                        f"Run `pr_tool.py diff --pr <N> --file {c_path}` and choose a `[R<line>]` start_line.",
                    )

        # Inline-only prose checks: no prose line numbers; style nit must link style guide.
        prose_only = _strip_fenced_code(c_body)
        if PROSE_LINE_NUM_RE.search(prose_only) or PROSE_L_ANCHOR_RE.search(prose_only):
            err(
                f"{loc_label}: inline comment mentions a line number in prose.",
                "Remove `line <N>` from prose; the inline diff anchor already carries the line.",
            )
        if c_body.lstrip().lower().startswith("style nit:") and not STYLE_URL_ANCHOR_RE.search(c_body):
            err(
                f"{loc_label}: `style nit:` comment is missing a `Style-guide-for-Flutter-repo.md#<anchor>` link.",
                "Link `https://github.com/flutter/flutter/blob/master/docs/contributing/Style-guide-for-Flutter-repo.md#<anchor>`.",
            )
        all_texts.append((loc_label, c_body))

    for label, txt in all_texts:
        if "file://" in txt:
            err(
                f"{label}: contains local `file://` URI.",
                "Replace `file:///...` links with backticked repo paths or GitHub `https://` URLs.",
            )
        if BOOKKEEPING_LEAK_RE.search(txt):
            err(
                f"{label}: leaks internal bookkeeping label (`STOPPED_AT:`/`RECOMMENDED:`/`SKIPPED:`).",
                "Move reviewer bookkeeping strictly into `drafts/<repo>-<N>.md`.",
            )
        if BARE_REGRESSION_ISSUE_RE.search(txt):
            err(
                f"{label}: uses bare `#<id>` in regression-test comment text.",
                "Use the full URL `https://github.com/<owner>/<repo>/issues/<id>`.",
            )
        for non_sig in NOT_A_SIGNAL_CHECKS:
            if non_sig.lower() in txt.lower():
                err(
                    f"{label}: mentions non-signal CI check `{non_sig}`.",
                    "Say nothing about `Google testing`, `Merge Queue Guard`, `flutter-gold`, or `tree-status`.",
                )
        if valid_style_anchors is not None:
            for m_anc in STYLE_URL_ANCHOR_RE.finditer(txt):
                anc = m_anc.group(1)
                if anc not in valid_style_anchors:
                    err(
                        f"{label}: style-guide anchor `#{anc}` does not exist in worktree copy.",
                        "Run `pr_tool.py style --toc` and use a verified `#<anchor>`.",
                    )

    return issues


def cmd_validate(args: argparse.Namespace) -> int:
    """CLI handler for deterministic validation of `drafts/<repo>-<N>.json` and `.md`."""
    worktree = Path(args.worktree).resolve() if args.worktree else Path.cwd()
    json_path = Path(args.draft_json).resolve()
    md_path = Path(args.draft_md).resolve() if args.draft_md else json_path.with_suffix(".md")

    parsed_diff: list[dict[str, Any]] | None = None
    expected_sha: str | None = None
    agent_login: str | None = None
    has_cicd: bool | None = None
    raw_changed: int | None = None

    if args.pr:
        spool_root = Path(args.spool_root) if args.spool_root else None
        pr_num = int(args.pr)
        repo = normalize_repo_slug(args.repo, worktree, pr_number=pr_num, spool_root=spool_root)
        spool_dir = resolve_spool_dir(repo, pr_num, spool_root)
        diff_file = spool_dir / "diff.patch"
        if diff_file.is_file():
            parsed_diff = parse_unified_diff(diff_file.read_text(encoding="utf-8", errors="replace"))
        state_file = spool_dir / "state.json"
        if state_file.is_file():
            st = json.loads(state_file.read_text(encoding="utf-8"))
            expected_sha = st.get("head_sha")
            agent_login = st.get("agent_login")
            has_cicd = ((st.get("gates") or {}).get("gate5_ci") or {}).get("has_cicd_label")
            raw_changed = (st.get("diff_weights") or {}).get("raw_changed_lines")

    valid_anchors: set[str] | None = None
    try:
        style_path = Path(args.style_file).resolve() if args.style_file else resolve_style_guide_path(worktree)
        valid_anchors = {s["anchor"] for s in parse_style_guide(style_path.read_text(encoding="utf-8", errors="replace"))}
    except ToolError:
        valid_anchors = None

    issues = validate_review_files(
        draft_json_path=json_path,
        draft_md_path=md_path,
        parsed_diff=parsed_diff,
        valid_style_anchors=valid_anchors,
        expected_head_sha=expected_sha,
        agent_login=agent_login,
        has_cicd_label=has_cicd,
        raw_changed_lines=raw_changed,
    )
    if not issues:
        print(f"VALID: {json_path.name} and {md_path.name} passed all checks.")
        return 0

    print(f"INVALID ({len(issues)} issue(s)):")
    for idx, item in enumerate(issues, start=1):
        print(f"{idx}. ERROR: {item['error']}\n   Fix: {item['fix']}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    """Build top-level CLI parser and subcommands."""
    p = argparse.ArgumentParser(
        prog="pr_tool.py",
        description="Deterministic context-bounding & validation helper for flutter-pr-review.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    def add_common(sp: argparse.ArgumentParser, *, require_pr: bool = True) -> None:
        if require_pr:
            sp.add_argument("--pr", type=int, required=True, help="Pull request number.")
        else:
            sp.add_argument("--pr", type=int, default=None, help="Optional pull request number.")
        sp.add_argument("--repo", default=None, help="GitHub repo `<owner>/<repo>` (default: inferred from worktree).")
        sp.add_argument("--worktree", default=None, help="Path to PR worktree (default: CWD).")
        sp.add_argument("--spool-root", default=None, help="Override spool root (default: /tmp/flutter-pr-review).")

    sp_pre = sub.add_parser("preflight", help="Fetch PR artifacts once and emit Gate 1-5 + diff-weight digest.")
    add_common(sp_pre, require_pr=True)
    sp_pre.set_defaults(func=cmd_preflight)

    sp_thr = sub.add_parser("threads", help="Print compacted existing PR review threads (run after Gate 3 / initial code read).")
    add_common(sp_thr, require_pr=True)
    sp_thr.set_defaults(func=cmd_threads)

    sp_diff = sub.add_parser("diff", help="Print `[R<line>]`-annotated diff chunks (<=700 lines / <=35KB).")
    add_common(sp_diff, require_pr=True)
    sp_diff.add_argument("--file", action="append", help="Filter to specific file path(s) (repeatable).")
    sp_diff.add_argument("--exclude-tests", action="store_true", help="Exclude test files.")
    sp_diff.add_argument("--chunk", type=int, default=1, help="1-based chunk page number (default: 1).")
    sp_diff.add_argument("--list-chunks", action="store_true", help="List chunk manifest without printing diff lines.")
    sp_diff.set_defaults(func=cmd_diff)

    sp_ci = sub.add_parser(
        "ci-log",
        help="Extract bounded failure windows from GitHub Actions, Flutter Dashboard Presubmit, or LUCI Buildbucket.",
    )
    add_common(sp_ci, require_pr=False)
    sp_ci.add_argument(
        "target",
        help="GitHub Actions run ID/URL, Flutter Dashboard presubmit URL, or LUCI Buildbucket URL/ID.",
    )
    sp_ci.add_argument("--max-lines", type=int, default=120, help="Maximum output lines (default: 120).")
    sp_ci.set_defaults(func=cmd_ci_log)

    sp_out = sub.add_parser("outline", help="Lexer-aware Dart outline, `--for-diff` slice, or `--regression-style` test index.")
    add_common(sp_out, require_pr=False)
    sp_out.add_argument("file", help="Path to Dart file (relative to worktree or absolute).")
    sp_out.add_argument("--for-diff", action="store_true", help="Emit state- & lifecycle-aware slice for >1,000-line prod files.")
    sp_out.add_argument("--regression-style", action="store_true", help="Emit test helpers, regression comment style, and matching test spans.")
    sp_out.add_argument("--lines", action="append", help="Explicit touched line range(s) e.g. `120-145` when running without `--pr`.")
    sp_out.add_argument("--symbol", action="append", help="Explicit symbol(s) to trace across members/tests.")
    sp_out.set_defaults(func=cmd_outline)

    sp_sty = sub.add_parser("style", help="List verified `#anchor` slugs or print one section of Style-guide-for-Flutter-repo.md.")
    sp_sty.add_argument("--worktree", default=None, help="Path to Flutter worktree (default: CWD).")
    sp_sty.add_argument("--style-file", default=None, help="Explicit path to Style-guide-for-Flutter-repo.md.")
    sp_sty.add_argument("--toc", action="store_true", help="Print compact heading -> `#anchor` table of contents.")
    sp_sty.add_argument("--section", default=None, help="Fetch a single section by anchor or heading substring.")
    sp_sty.set_defaults(func=cmd_style)

    sp_val = sub.add_parser("validate", help="Validate `drafts/<repo>-<N>.json` and `.md` invariants with `Fix:` hints.")
    add_common(sp_val, require_pr=False)
    sp_val.add_argument("draft_json", help="Path to `drafts/<repo>-<N>.json`.")
    sp_val.add_argument("draft_md", nargs="?", default=None, help="Path to `drafts/<repo>-<N>.md` (default: sibling `.md`).")
    sp_val.add_argument("--style-file", default=None, help="Explicit path to Style-guide-for-Flutter-repo.md.")
    sp_val.set_defaults(func=cmd_validate)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ToolError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
