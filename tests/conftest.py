"""Shared fixtures and helpers for flakectl tests."""


def make_progress_content(runs):
    """Generate progress.md content from a list of run dicts.

    Each run dict should have:
        run_id, status, jobs (list of job dicts),
        and optionally: run_url, branch, event, run_started_at,
        run_attempt, commit_sha.

    Each job dict should have:
        name, and optionally: step, job_id, category, is_flake,
        test-id, failed_test, error_message, summary.
    """
    lines = [
        "# CI Failure Classification Progress\n",
        "## Categories So Far",
        "<!-- CATEGORIES START -->",
        "(none yet)",
        "<!-- CATEGORIES END -->\n",
        "---\n",
    ]

    for run in runs:
        rid = run["run_id"]
        status = run.get("status", "pending")
        run_url = run.get("run_url", f"https://github.com/org/repo/actions/runs/{rid}")
        branch = run.get("branch", "main")
        event = run.get("event", "push")
        run_started_at = run.get("run_started_at", "2025-01-15T10:00:00Z")
        run_attempt = run.get("run_attempt", "1")
        commit_sha = run.get("commit_sha", "abc123")

        lines.append(f"<!-- BEGIN RUN {rid} -->")
        lines.append(f"## run_id: {rid}")
        lines.append(f"- **status**: {status}")
        lines.append(f"- **run_url**: {run_url}")
        lines.append(f"- **branch**: {branch}")
        lines.append(f"- **event**: {event}")
        lines.append(f"- **run_started_at**: {run_started_at}")
        lines.append(f"- **run_attempt**: {run_attempt}")
        lines.append(f"- **commit_sha**: {commit_sha}")
        lines.append("")

        for job in run.get("jobs", []):
            name = job.get("name", "test-job")
            step = job.get("step", "")
            job_id = job.get("job_id", "")
            category = job.get("category", "")
            is_flake = job.get("is_flake", "")
            test_id = job.get("test_id", "")
            failed_test = job.get("failed_test", "")
            error_message = job.get("error_message", "")
            summary = job.get("summary", "")

            lines.append(f"#### job: `{name}`")
            lines.append(f"- **step**: {step}")
            lines.append(f"- **job_id**: {job_id}")
            lines.append(f"- **category**: {category}")
            lines.append(f"- **is_flake**: {is_flake}")
            lines.append(f"- **test-id**: {test_id}")
            lines.append(f"- **failed_test**: {failed_test}")
            lines.append(f"- **error_message**: {error_message}")
            lines.append(f"- **summary**: {summary}")
            lines.append("")

        lines.append(f"<!-- END RUN {rid} -->")
        lines.append("")

    return "\n".join(lines)


def make_csv_content(rows):
    """Generate CSV content string from a list of row dicts.

    Each row dict should have keys matching the CSV columns:
        run_id, run_url, branch, event, commit_sha,
        failed_job_name, run_started_at, job_completed_at,
        run_attempt, failure_step.
    """
    fieldnames = [
        "run_id", "run_url", "branch", "event", "commit_sha",
        "failed_job_name", "run_started_at", "job_completed_at",
        "run_attempt", "failure_step",
    ]
    lines = [",".join(fieldnames)]
    for row in rows:
        values = [str(row.get(f, "")) for f in fieldnames]
        lines.append(",".join(values))
    return "\n".join(lines) + "\n"


# Sample data constants

SAMPLE_RUN_DONE = {
    "run_id": "12345",
    "status": "done",
    "run_url": "https://github.com/org/repo/actions/runs/12345",
    "branch": "main",
    "event": "push",
    "run_started_at": "2025-01-15T10:00:00Z",
    "jobs": [
        {
            "name": "unit-tests",
            "step": "Run tests",
            "job_id": "100",
            "category": "test-flake/timeout",
            "is_flake": "yes",
            "test_id": "TestTimeout",
            "failed_test": "test_timeout.py::test_slow",
            "error_message": "TimeoutError: test exceeded 30s",
            "summary": "Test timed out due to slow CI runner",
        },
    ],
}

SAMPLE_RUN_PENDING = {
    "run_id": "12346",
    "status": "pending",
    "run_url": "https://github.com/org/repo/actions/runs/12346",
    "branch": "main",
    "event": "push",
    "run_started_at": "2025-01-15T11:00:00Z",
    "jobs": [
        {
            "name": "integration-tests",
            "step": "Run integration",
        },
    ],
}

SAMPLE_CSV_ROW = {
    "run_id": "12345",
    "run_url": "https://github.com/org/repo/actions/runs/12345",
    "branch": "main",
    "event": "push",
    "commit_sha": "abc123def456",
    "failed_job_name": "unit-tests",
    "run_started_at": "2025-01-15T10:00:00Z",
    "job_completed_at": "2025-01-15T10:05:00Z",
    "run_attempt": "1",
    "failure_step": "Run tests",
}
