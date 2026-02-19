#!/usr/bin/env python3
"""Read failed_jobs.csv and generate progress.md for agent classification."""

import csv
import logging

logger = logging.getLogger(__name__)


def run(
    csv_path: str = "failed_jobs.csv",
    output_path: str = "progress.md",
    skip_jobs: list[str] | None = None,
) -> int:
    """Generate progress.md from a failed jobs CSV. Returns exit code."""
    skip = set(skip_jobs) if skip_jobs else set()

    with open(csv_path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # Group by run_id, preserving CSV order (newest first)
    runs = {}
    for row in rows:
        rid = row["run_id"]
        if rid not in runs:
            runs[rid] = {
                "run_id": rid,
                "run_url": row["run_url"],
                "branch": row["branch"],
                "event": row["event"],
                "commit_sha": row["commit_sha"],
                "run_started_at": row["run_started_at"],
                "run_attempt": row["run_attempt"],
                "jobs": [],
            }
        if row["failed_job_name"] in skip:
            continue
        runs[rid]["jobs"].append({
            "name": row["failed_job_name"],
            "step": row["failure_step"],
            "completed_at": row["job_completed_at"],
        })

    total_jobs = 0
    with open(output_path, "w") as out:
        out.write("# CI Failure Classification Progress\n\n")
        out.write("## Categories So Far\n")
        out.write("<!-- CATEGORIES START -->\n")
        out.write("(none yet)\n")
        out.write("<!-- CATEGORIES END -->\n\n")
        out.write("---\n\n")

        count = 0
        for rid, run_data in runs.items():
            # Skip runs where all jobs were filtered out
            if not run_data["jobs"]:
                continue

            count += 1
            out.write(f"<!-- BEGIN RUN {rid} -->\n")
            out.write(f"## run_id: {rid}\n")
            out.write("- **status**: pending\n")
            out.write(f"- **run_url**: {run_data['run_url']}\n")
            out.write(f"- **branch**: {run_data['branch']}\n")
            out.write(f"- **event**: {run_data['event']}\n")
            out.write(f"- **run_started_at**: {run_data['run_started_at']}\n")
            out.write(f"- **run_attempt**: {run_data['run_attempt']}\n")
            out.write(f"- **commit_sha**: {run_data['commit_sha']}\n\n")

            for job in run_data["jobs"]:
                total_jobs += 1
                out.write(f"#### job: `{job['name']}`\n")
                out.write(f"- **step**: {job['step']}\n")
                out.write("- **job_id**:\n")
                out.write("- **category**:\n")
                out.write("- **is_flake**:\n")
                out.write("- **test-id**:\n")
                out.write("- **failed_test**:\n")
                out.write("- **error_message**:\n")
                out.write("- **summary**:\n\n")

            out.write(f"<!-- END RUN {rid} -->\n\n")

    logger.info("Generated %s with %d runs, %d failed jobs",
                output_path, count, total_jobs)
    return 0
