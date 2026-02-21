#!/usr/bin/env python3
"""Extract results from progress.md into report.md and report.json.

Parses the agent-filled progress.md coordination file and produces:
- report.md    -- final report organized by root cause (human-readable)
- report.json  -- structured data for programmatic use
"""

import json
import logging
import os
import re
from collections import defaultdict
from datetime import UTC, datetime

from flakectl.progressfile import (
    RUN_BLOCK_RE,
    VALID_CATEGORY_PREFIXES,
    parse_categories_section,
    parse_field,
    parse_jobs,
)

logger = logging.getLogger(__name__)


def _split_category(cat: str) -> tuple[str, str]:
    """Split a full category into (category, subcategory).

    When there are 3+ segments, the last one is the subcategory.
    'test-flake/timeout/78753' -> ('test-flake/timeout', '78753')
    'infra-flake/registry-502' -> ('infra-flake/registry-502', '')
    """
    parts = cat.split("/")
    if len(parts) >= 3:
        return "/".join(parts[:-1]), parts[-1]
    return cat, ""


def relative_date(date_str, ref_date):
    """Return a human-friendly relative date string."""
    if not date_str:
        return ""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        try:
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return ""
    delta = (ref_date - dt).days
    if delta == 0:
        return "today"
    elif delta == 1:
        return "1 day ago"
    else:
        return f"{delta} days ago"


def _determine_flake_status(cat_rows: list[dict]) -> str:
    """Determine aggregate flake status from a list of categorized rows."""
    flake_vals = set(r["is_flake"] for r in cat_rows)
    if flake_vals == {"yes"}:
        return "yes"
    if flake_vals == {"no"}:
        return "no"
    return "mixed"


def _summarize_runs(classified_rows: list[dict]) -> tuple[int, int, int]:
    """Return counts of (flake_runs, real_failure_runs, unclear_runs)."""
    by_run: dict[str, set[str]] = {}
    for row in classified_rows:
        rid = row["run_id"]
        if rid not in by_run:
            by_run[rid] = set()
        if row["is_flake"]:
            by_run[rid].add(row["is_flake"])

    flake_runs = 0
    real_failure_runs = 0
    unclear_runs = 0
    for flags in by_run.values():
        if "no" in flags:
            real_failure_runs += 1
        elif "yes" in flags:
            flake_runs += 1
        else:
            unclear_runs += 1

    return flake_runs, real_failure_runs, unclear_runs


def _lookup_description(category: str, cat_descriptions: dict[str, str]) -> str:
    """Look up a description for a category.

    Tries exact match first, then matches any full category whose first two
    segments equal the given category.
    """
    if category in cat_descriptions:
        return cat_descriptions[category]
    for full_cat, desc in cat_descriptions.items():
        if _split_category(full_cat)[0] == category:
            return desc
    return ""


def _load_fixes(fixes_path: str | None) -> dict[str, list[dict]]:
    """Load fixes.json and return mapping: category_name -> list of fix items.

    Returns empty dict if file is None, missing, or malformed.
    """
    if not fixes_path or not os.path.exists(fixes_path):
        return {}
    try:
        with open(fixes_path) as f:
            data = json.load(f)
        return {
            entry["category"]: entry["items"]
            for entry in data.get("fixes", [])
            if entry.get("category") and entry.get("items")
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.warning("Could not parse %s, skipping fixes", fixes_path)
        return {}


def _format_fix_link(item: dict) -> str:
    """Format a fix item as a markdown link.

    PR:     [#123](url) or [#123](url) (possibly)
    Commit: [abc1234](url) or [abc1234](url) (possibly)
    """
    text = f"#{item['id']}" if item.get("type") == "pr" else item["sha"][:7]
    link = f"[{text}]({item['url']})"
    if item.get("confidence") == "possible":
        link += " (possibly)"
    return link


def _to_utc_epoch(ts: str) -> float:
    """Parse an ISO timestamp or date string to UTC epoch seconds.

    Handles full ISO timestamps (with Z or timezone offset),
    and date-only strings (treated as midnight UTC).
    Returns 0.0 for missing/invalid input.
    """
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def _sort_fixes(items: list[dict]) -> list[dict]:
    """Sort fix items: match confidence first, then possible.

    Within each group, sort by date newest first.
    Missing dates sort to the end.
    """
    def key(item):
        confidence_order = 0 if item.get("confidence") == "match" else 1
        ts = _to_utc_epoch(item.get("date", ""))
        return (confidence_order, 0 if ts else 1, -ts)

    return sorted(items, key=key)


def _format_fix_detail_line(item: dict) -> str:
    """Format one fix as a detail line for the detail section.

    Commit: '  - YYYY-MM-DD [hash](url) title'
    PR:     '  - YYYY-MM-DD [#id](url) title'
    """
    raw = item.get("date", "")
    d = raw[:10] if raw else ""
    title = item.get("title", "")

    if item.get("type") == "pr":
        link = f"[#{item['id']}]({item['url']})"
    else:
        link = f"[{item['sha'][:7]}]({item['url']})"

    parts = []
    if d:
        parts.append(d)
    parts.append(link)
    if title:
        parts.append(title)

    return "  - " + " ".join(parts)


def _write_summary_table(f, indexed_cats: list[tuple[int, dict]]) -> None:
    """Write one summary table for a list of (global_index, cat_data) pairs."""
    f.write("| # | Category | Subcategory | Runs/Jobs | Last Occurred | Fix(-es) |\n")
    f.write("|---|----------|-------------|-----------|---------------|----------|\n")

    for idx, cat_data in indexed_cats:
        subcats_str = ", ".join(cat_data["subcategories"])
        # Summary table shows only match-confidence fixes, sorted by date newest first
        match_fixes = [
            item for item in cat_data.get("fixes", [])
            if item.get("confidence") == "match"
        ]
        match_fixes = _sort_fixes(match_fixes)
        fix_str = ", ".join(_format_fix_link(item) for item in match_fixes)
        f.write(
            f"| {idx} | `{cat_data['name']}` "
            f"| {subcats_str} "
            f"| {cat_data['run_count']}/{cat_data['job_count']} "
            f"| {cat_data['last_occurred']} "
            f"| {fix_str} |\n"
        )


def _write_detail_section(f, idx: int, cat_data: dict) -> None:
    """Write one root-cause detail block."""
    f.write(f"### {idx}. `{cat_data['name']}`\n\n")

    if cat_data["description"]:
        f.write(f"**Description:** {cat_data['description']}\n\n")

    f.write(f"- **Failed runs:** {cat_data['run_count']}\n")
    f.write(f"- **Failed jobs:** {cat_data['job_count']}\n")
    if cat_data["test_ids"]:
        f.write(f"- **Test IDs:** {', '.join(cat_data['test_ids'])}\n")

    if cat_data.get("fixes"):
        fixes = cat_data["fixes"]
        # Split into commits and PRs
        commits = [item for item in fixes if item.get("type") == "commit"]
        prs = [item for item in fixes if item.get("type") == "pr"]
        # Sort each group
        commits = _sort_fixes(commits)
        prs = _sort_fixes(prs)
        # Commits first, then PRs
        ordered = commits + prs

        match_items = [item for item in ordered if item.get("confidence") == "match"]
        possible_items = [item for item in ordered if item.get("confidence") != "match"]

        f.write("- **Fix(-es):**\n")
        for item in match_items:
            f.write(_format_fix_detail_line(item) + "\n")
        if possible_items:
            f.write("  <details><summary>Possible fixes</summary>\n\n")
            for item in possible_items:
                f.write(_format_fix_detail_line(item) + "\n")
            f.write("  </details>\n")

    error = cat_data["example_error"]
    if error:
        if len(error) > 200:
            error = error[:200] + "..."
        f.write(f"- **Example error:** `{error}`\n")

    summary = cat_data["example_summary"]
    if summary:
        if len(summary) > 600:
            summary = summary[:600] + "..."
        f.write(f"- **Example summary:** {summary}\n")

    f.write("\n")

    f.write("| Run ID | Branch | Date | Jobs Failed |\n")
    f.write("|--------|--------|------|-------------|\n")
    for affected_run in cat_data["affected_runs"]:
        branch = affected_run["branch"]
        if len(branch) > 40:
            branch = branch[:37] + "..."
        f.write(
            f"| [{affected_run['run_id']}]({affected_run['run_url']}) | {branch} "
            f"| {affected_run['date']} | {affected_run['jobs_failed']} |\n"
        )
    f.write("\n")


def _build_category_data(sorted_cats, cat_descriptions, analysis_date,
                         fixes_by_cat=None):
    """Build a list of category summary dicts from sorted categories."""
    categories = []
    for cat, cat_rows in sorted_cats:
        unique_run_ids = sorted(set(r["run_id"] for r in cat_rows))
        test_ids = sorted(set(
            tid.strip()
            for r in cat_rows
            for tid in r["test_id"].split(",")
            # Guard: agents occasionally include markdown field markers in test-id
            if tid.strip() and not tid.strip().startswith("- **")
        ))
        flake = _determine_flake_status(cat_rows)

        cat_dates = [r["run_started_at"].replace("Z", "+00:00")
                     for r in cat_rows if r["run_started_at"]]
        last_date_str = max(cat_dates) if cat_dates else ""
        last_rel = relative_date(last_date_str, analysis_date)

        example_error = next(
            (r["error_message"] for r in cat_rows if r["error_message"]), "")
        example_summary = next(
            (r["summary"] for r in cat_rows if r["summary"]), "")

        affected = []
        for rid in unique_run_ids:
            run_rows = [r for r in cat_rows if r["run_id"] == rid]
            r0 = run_rows[0]
            affected.append({
                "run_id": rid,
                "run_url": r0["run_url"],
                "branch": r0["branch"],
                "date": r0["run_started_at"][:10] if r0["run_started_at"] else "",
                "run_started_at": r0["run_started_at"] or "",
                "jobs_failed": len(run_rows),
            })

        affected.sort(
            key=lambda r: _to_utc_epoch(r["run_started_at"]), reverse=True,
        )

        subcats = sorted(set(
            _split_category(r["category"])[1]
            for r in cat_rows
            if _split_category(r["category"])[1]
        ))

        categories.append({
            "name": cat,
            "description": _lookup_description(cat, cat_descriptions),
            "flake": flake,
            "run_count": len(unique_run_ids),
            "job_count": len(cat_rows),
            "test_ids": test_ids,
            "subcategories": subcats,
            "example_error": example_error,
            "example_summary": example_summary,
            "last_occurred": last_rel,
            "affected_runs": affected,
            "fixes": (fixes_by_cat or {}).get(cat, []),
        })
    return categories


def _write_report_md(path, categories, total_runs, total_jobs,
                     total_flake_runs, total_bug_runs, total_unclear_runs,
                     unfinished, analysis_date):
    """Write the markdown report file."""
    # Partition categories into flakes and real failures with continuous numbering
    flake_cats = []
    real_cats = []
    for i, cat_data in enumerate(categories, 1):
        if cat_data["flake"] == "yes":
            flake_cats.append((i, cat_data))
        else:
            real_cats.append((i, cat_data))

    with open(path, "w") as f:
        f.write("# Flaky Test Analysis\n\n")
        f.write(f"**Date:** {analysis_date.isoformat()}\n\n")
        f.write(f"**{total_runs} failed runs** analyzed: "
                f"**{total_flake_runs} caused by flakes**, "
                f"**{total_bug_runs} caused by real failures**")
        if total_unclear_runs:
            f.write(f", **{total_unclear_runs} unclear**")
        f.write(".\n\n")
        f.write("Each category below maps to exactly **1 root cause / 1 fix**.\n\n")

        f.write("## Summary\n\n")

        if flake_cats:
            f.write("### Flakes\n\n")
            _write_summary_table(f, flake_cats)
            f.write("\n")

        if real_cats:
            f.write("### Real Failures\n\n")
            _write_summary_table(f, real_cats)
            f.write("\n")

        f.write(
            f"**Total: {total_runs} failed runs, "
            f"{total_jobs} failed jobs**\n\n"
        )

        f.write("---\n\n")
        f.write("## Root Causes (Detail)\n\n")

        # Detail sections: flakes first, then real failures
        for idx, cat_data in flake_cats + real_cats:
            _write_detail_section(f, idx, cat_data)

        if unfinished:
            f.write("---\n\n")
            f.write("## Unfinished Runs\n\n")
            f.write("| Run ID | Status |\n")
            f.write("|--------|--------|\n")
            for r in unfinished:
                f.write(f"| [{r['run_id']}]({r['run_url']}) | {r['status']} |\n")

    logger.info("Wrote %s", path)


def _write_report_json(path, categories, total_runs, total_jobs,
                       total_flake_runs, total_bug_runs, total_unclear_runs,
                       unfinished, analysis_date):
    """Write the JSON report file."""
    json_categories = []
    for cat_data in categories:
        json_cat = {
            "name": cat_data["name"],
            "description": cat_data["description"],
            "is_flake": cat_data["flake"],
            "runs": cat_data["run_count"],
            "jobs": cat_data["job_count"],
            "test_ids": cat_data["test_ids"],
            "subcategories": cat_data["subcategories"],
            "example_error": cat_data["example_error"],
            "example_summary": cat_data["example_summary"],
            "affected_runs": cat_data["affected_runs"],
        }
        if cat_data.get("fixes"):
            json_cat["fixes"] = [
                {
                    "type": item.get("type"),
                    "id": item.get("id"),
                    "sha": item.get("sha"),
                    "url": item.get("url"),
                    "title": item.get("title", ""),
                    "date": item.get("date", ""),
                    "confidence": item.get("confidence"),
                }
                for item in cat_data["fixes"]
            ]
        json_categories.append(json_cat)

    report_json = {
        "date": analysis_date.isoformat(),
        "total_runs": total_runs,
        "flake_runs": total_flake_runs,
        "real_failure_runs": total_bug_runs,
        "unclear_runs": total_unclear_runs,
        "total_jobs": total_jobs,
        "categories": json_categories,
        "unfinished_runs": unfinished,
    }

    with open(path, "w") as f:
        json.dump(report_json, f, indent=2)

    logger.info("Wrote %s", path)


def run(
    input_path: str = "progress.md",
    output_md: str = "report.md",
    output_json: str = "report.json",
    fixes_path: str | None = None,
) -> int:
    """Extract results from progress.md into report files. Returns exit code."""
    with open(input_path) as f:
        content = f.read()

    cat_descriptions = parse_categories_section(content)

    # Load fix correlations (auto-detect fixes.json in same dir if not given)
    if fixes_path is None:
        auto_path = os.path.join(os.path.dirname(input_path) or ".", "fixes.json")
        if os.path.exists(auto_path):
            fixes_path = auto_path
    fixes_by_cat = _load_fixes(fixes_path)

    sections = re.findall(RUN_BLOCK_RE, content, re.DOTALL)

    if not sections:
        logger.warning("No run sections found in progress.md")
        return 1

    results = []
    run_statuses = []

    for run_id, body in sections:
        status = parse_field(body, "status")
        run_url = parse_field(body, "run_url")
        branch = parse_field(body, "branch")
        event = parse_field(body, "event")
        run_started_at = parse_field(body, "run_started_at")

        run_statuses.append({
            "run_id": run_id, "status": status, "run_url": run_url,
        })

        jobs = parse_jobs(body)
        for job in jobs:
            results.append({
                "run_id": run_id,
                "run_url": run_url,
                "branch": branch,
                "event": event,
                "run_started_at": run_started_at,
                "job_name": job["job_name"],
                "step": job["step"],
                "job_id": job["job_id"],
                "category": job["category"],
                "is_flake": job["is_flake"],
                "test_id": job["test_id"],
                "failed_test": job["failed_test"],
                "error_message": job["error_message"],
                "summary": job["summary"],
                "status": status,
            })

    done = [r for r in run_statuses if r["status"] == "done"]
    pending = [r for r in run_statuses if r["status"] == "pending"]
    errored = [r for r in run_statuses if r["status"] == "error"]

    logger.info("Total runs: %d", len(run_statuses))
    logger.info("Total failed jobs: %d", len(results))
    logger.info("Done: %d, Pending: %d, Error: %d",
                len(done), len(pending), len(errored))

    if pending:
        logger.warning("%d runs not yet analyzed", len(pending))

    # ---- Build report data ----
    classified = [r for r in results if r["status"] == "done"]

    by_cat = defaultdict(list)
    for r in classified:
        cat = r["category"]
        if cat and cat.startswith(VALID_CATEGORY_PREFIXES):
            category, _ = _split_category(cat)
            by_cat[category].append(r)

    sorted_cats = sorted(
        by_cat.items(),
        key=lambda x: len(set(r["run_id"] for r in x[1])),
        reverse=True,
    )

    total_runs = len(set(r["run_id"] for r in classified))
    total_flake_runs, total_bug_runs, total_unclear_runs = _summarize_runs(classified)

    analysis_date = datetime.now(UTC).date()

    categories = _build_category_data(
        sorted_cats, cat_descriptions, analysis_date,
        fixes_by_cat=fixes_by_cat,
    )

    total_jobs = len(classified)
    unfinished = [
        {"run_id": r["run_id"], "status": r["status"], "run_url": r["run_url"]}
        for r in pending + errored
    ]

    _write_report_md(output_md, categories, total_runs, total_jobs,
                     total_flake_runs, total_bug_runs, total_unclear_runs,
                     unfinished, analysis_date)
    _write_report_json(output_json, categories, total_runs, total_jobs,
                       total_flake_runs, total_bug_runs, total_unclear_runs,
                       unfinished, analysis_date)

    # Log summary
    logger.info("")
    logger.info("=== Flaky Test Analysis Summary ===")
    logger.info("")
    logger.info("  Flake runs: %d / %d", total_flake_runs, total_runs)
    logger.info("  Real failure runs: %d / %d", total_bug_runs, total_runs)
    if total_unclear_runs:
        logger.info("  Unclear runs: %d / %d", total_unclear_runs, total_runs)
    logger.info("")
    for i, cat_data in enumerate(categories, 1):
        logger.info(
            "  %2d. %-55s  runs=%2d  jobs=%2d  flake=%s",
            i, cat_data["name"], cat_data["run_count"],
            cat_data["job_count"], cat_data["flake"],
        )

    return 0
