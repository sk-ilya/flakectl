#!/usr/bin/env python3
"""Extract results from progress.md into report.md and report.json.

Parses the agent-filled progress.md coordination file and produces:
- report.md    -- final report organized by root cause (human-readable)
- report.json  -- structured data for programmatic use
"""

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone

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


def parse_field(text, field):
    """Extract a field value from a section of text."""
    match = re.search(rf"- \*\*{field}\*\*:\s*(.*)", text)
    return match.group(1).strip() if match else ""


def parse_categories_section(content):
    """Extract category descriptions from the Categories So Far section."""
    match = re.search(
        r"<!-- CATEGORIES START -->(.*?)<!-- CATEGORIES END -->",
        content, re.DOTALL,
    )
    if not match:
        return {}
    cats = {}
    for line in match.group(1).strip().split("\n"):
        m = re.match(r"- `([^`]+)`\s*--\s*(.*)", line.strip())
        if m:
            cats[m.group(1)] = m.group(2).strip()
    return cats


def parse_jobs(run_body):
    """Parse individual job subsections from a run body."""
    job_pattern = r"#### job: `([^`]+)`(.*?)(?=#### job:|$)"
    matches = re.findall(job_pattern, run_body, re.DOTALL)

    jobs = []
    for job_name, job_body in matches:
        jobs.append({
            "job_name": job_name.strip(),
            "step": parse_field(job_body, "step"),
            "job_id": parse_field(job_body, "job_id"),
            "category": parse_field(job_body, "category"),
            "is_flake": parse_field(job_body, "is_flake"),
            "test_id": parse_field(job_body, "test-id"),
            "failed_test": parse_field(job_body, "failed_test"),
            "error_message": parse_field(job_body, "error_message"),
            "summary": parse_field(job_body, "summary"),
        })
    return jobs


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


def _build_category_data(sorted_cats, cat_descriptions, analysis_date):
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
                "jobs_failed": len(run_rows),
            })

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
        })
    return categories


def run(
    input_path: str = "progress.md",
    output_md: str = "report.md",
    output_json: str = "report.json",
) -> int:
    """Extract results from progress.md into report files. Returns exit code."""
    with open(input_path) as f:
        content = f.read()

    cat_descriptions = parse_categories_section(content)

    pattern = r"<!-- BEGIN RUN (\d+) -->(.*?)<!-- END RUN \1 -->"
    sections = re.findall(pattern, content, re.DOTALL)

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

    _VALID_PREFIXES = ("test-flake/", "infra-flake/", "bug/", "build-error/")

    by_cat = defaultdict(list)
    for r in classified:
        cat = r["category"]
        if cat and cat.startswith(_VALID_PREFIXES):
            category, _ = _split_category(cat)
            by_cat[category].append(r)

    sorted_cats = sorted(
        by_cat.items(),
        key=lambda x: len(set(r["run_id"] for r in x[1])),
        reverse=True,
    )

    total_runs = len(set(r["run_id"] for r in classified))
    total_flake_runs, total_bug_runs, total_unclear_runs = _summarize_runs(classified)

    analysis_date = datetime.now(timezone.utc).date()

    categories = _build_category_data(sorted_cats, cat_descriptions, analysis_date)

    # ---- Write report.md ----
    with open(output_md, "w") as f:
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
        f.write("| # | Category | Subcategory | Runs/Jobs | Flake? | Last Occurred |\n")
        f.write("|---|----------|-------------|-----------|--------|---------------|\n")

        for i, cat_data in enumerate(categories, 1):
            subcats_str = ", ".join(cat_data["subcategories"])
            f.write(
                f"| {i} | `{cat_data['name']}` "
                f"| {subcats_str} "
                f"| {cat_data['run_count']}/{cat_data['job_count']} "
                f"| {cat_data['flake']} | {cat_data['last_occurred']} |\n"
            )

        f.write(
            f"\n**Total: {total_runs} failed runs, "
            f"{len(classified)} failed jobs**\n\n"
        )

        f.write("---\n\n")
        f.write("## Root Causes (Detail)\n\n")

        for i, cat_data in enumerate(categories, 1):
            f.write(f"### {i}. `{cat_data['name']}`\n\n")

            if cat_data["description"]:
                f.write(f"**Description:** {cat_data['description']}\n\n")

            f.write(f"- **Failed runs:** {cat_data['run_count']}\n")
            f.write(f"- **Failed jobs:** {cat_data['job_count']}\n")
            if cat_data["test_ids"]:
                f.write(f"- **Test IDs:** {', '.join(cat_data['test_ids'])}\n")

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

        if pending or errored:
            f.write("---\n\n")
            f.write("## Unfinished Runs\n\n")
            f.write("| Run ID | Status |\n")
            f.write("|--------|--------|\n")
            for r in pending + errored:
                f.write(f"| [{r['run_id']}]({r['run_url']}) | {r['status']} |\n")

    logger.info("Wrote %s", output_md)

    # ---- Write report.json ----
    json_categories = []
    for cat_data in categories:
        json_categories.append({
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
        })

    unfinished = [
        {"run_id": r["run_id"], "status": r["status"], "run_url": r["run_url"]}
        for r in pending + errored
    ]

    report_json = {
        "date": analysis_date.isoformat(),
        "total_runs": total_runs,
        "flake_runs": total_flake_runs,
        "real_failure_runs": total_bug_runs,
        "unclear_runs": total_unclear_runs,
        "total_jobs": len(classified),
        "categories": json_categories,
        "unfinished_runs": unfinished,
    }

    with open(output_json, "w") as f:
        json.dump(report_json, f, indent=2)

    logger.info("Wrote %s", output_json)

    # Print summary
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
