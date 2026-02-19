#!/usr/bin/env python3
"""Fetch failed GitHub Actions CI jobs and export metadata to CSV.

Collects failed workflow run and job information from GitHub Actions
and writes it to a CSV file for flaky test analysis.
"""

import csv
import logging
from datetime import UTC, datetime, timedelta

from flakectl.github import list_failed_jobs, list_failed_runs_multi

logger = logging.getLogger(__name__)

STATUS_OK = 0
STATUS_ERROR = 1
STATUS_NO_FAILURES = 20


def filter_runs_by_date(runs: list[dict], since_date: str) -> list[dict]:
    """Filter runs to only include those since the given ISO date."""
    if not runs:
        return []
    cutoff = datetime.fromisoformat(since_date).replace(tzinfo=UTC)
    result = []
    for run in runs:
        created_at = datetime.fromisoformat(
            run["created_at"].replace("Z", "+00:00")
        )
        if created_at >= cutoff:
            result.append(run)
    return result


def get_first_failed_step(steps: list[dict]) -> str:
    """Extract the name of the first failed step from job steps."""
    for step in steps:
        if step.get("conclusion") == "failure":
            return step.get("name", "")
    return ""


def build_csv_rows(repo: str, runs: list[dict]) -> list[dict]:
    """Iterate runs, fetch failed jobs, and build CSV row dicts."""
    rows = []
    total = len(runs)
    for i, run in enumerate(runs, 1):
        run_id = run["id"]
        logger.info("[%d/%d] Fetching jobs for run %s...", i, total, run_id)

        failed_jobs = list_failed_jobs(repo, run_id)

        if not failed_jobs:
            logger.debug("  No failed jobs found")
            continue

        for job in failed_jobs:
            steps = job.get("steps", [])
            failure_step = get_first_failed_step(steps)

            rows.append({
                "run_id": run_id,
                "run_url": run["url"],
                "branch": run["head_branch"],
                "event": run["event"],
                "commit_sha": run["head_sha"],
                "failed_job_name": job.get("name", ""),
                "run_started_at": run["created_at"],
                "job_completed_at": job.get("completed_at", ""),
                "run_attempt": run.get("run_attempt", ""),
                "failure_step": failure_step,
            })

        logger.debug("  Found %d failed job(s)", len(failed_jobs))

    return rows


def write_csv(rows: list[dict], output_path: str) -> None:
    """Sort rows by date descending and write to CSV."""
    rows.sort(key=lambda r: r["run_started_at"], reverse=True)

    fieldnames = [
        "run_id", "run_url", "branch", "event",
        "commit_sha", "failed_job_name", "run_started_at",
        "job_completed_at", "run_attempt", "failure_step",
    ]

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_list_arg(value: str) -> list[str] | None:
    """Parse a comma-separated or wildcard argument.

    Returns None for '*' (meaning 'all'), or a list of values.
    """
    if value == "*":
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def validate_workflows(workflows: list[str] | None) -> None:
    """Validate workflow filters as YAML filenames."""
    if not workflows:
        return
    invalid = [
        wf for wf in workflows
        if not wf.endswith((".yml", ".yaml"))
    ]
    if invalid:
        raise ValueError(
            "Workflow filters must be YAML filenames ending in .yml or .yaml: "
            + ", ".join(invalid)
        )


def run(
    repo: str,
    lookback_days: int = 7,
    workflow: str = "*",
    branch: str = "main",
    output: str = "failed_jobs.csv",
) -> int:
    """Fetch failed CI jobs and write to CSV. Returns status code."""
    since_date = (
        datetime.now(UTC) - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")

    workflows = parse_list_arg(workflow)
    branches = parse_list_arg(branch)
    try:
        validate_workflows(workflows)
    except ValueError as e:
        logger.error("%s", e)
        return STATUS_ERROR

    logger.info(
        "Fetching failed runs from %s since %s (last %d days)...",
        repo, since_date, lookback_days,
    )

    try:
        runs = list_failed_runs_multi(repo, 200, workflows, branches)
    except Exception as e:
        logger.error("Failed to fetch runs: %s", e)
        return STATUS_ERROR

    if not runs:
        logger.info("No failed runs found.")
        return STATUS_NO_FAILURES

    filtered = filter_runs_by_date(runs, since_date)
    logger.info("Processing %d failed runs", len(filtered))

    if not filtered:
        logger.info("No runs in the specified time range.")
        return STATUS_NO_FAILURES

    try:
        rows = build_csv_rows(repo, filtered)
    except Exception as e:
        logger.error("Failed to fetch failed jobs: %s", e)
        return STATUS_ERROR

    if not rows:
        logger.info("No failed jobs found across all runs.")
        return STATUS_NO_FAILURES

    write_csv(rows, output)
    logger.info("Wrote %d rows to %s", len(rows), output)

    return STATUS_OK
