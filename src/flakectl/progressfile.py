#!/usr/bin/env python3
"""Parse, query, and mutate progress.md coordination files.

Provides constants, parsers, and file-level operations for the progress.md
format used to coordinate classifier agents.
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

RUN_BLOCK_RE = r"<!-- BEGIN RUN (\d+) -->(.*?)<!-- END RUN \1 -->"
VALID_CATEGORY_PREFIXES = ("test-flake/", "infra-flake/", "bug/", "build-error/")


# ---------------------------------------------------------------------------
# Pure parsers (no file I/O)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# File-level queries
# ---------------------------------------------------------------------------

def get_runs_by_status(progress_path: str, status: str) -> list[str]:
    """Parse progress.md and return run IDs matching the given status."""
    content = Path(progress_path).read_text()
    status_pattern = rf"- \*\*status\*\*: {re.escape(status)}"
    return [
        rid for rid, body in re.findall(RUN_BLOCK_RE, content, re.DOTALL)
        if re.search(status_pattern, body)
    ]


def get_pending_runs(progress_path: str) -> list[str]:
    """Parse progress.md and return list of pending run IDs."""
    return get_runs_by_status(progress_path, "pending")


def get_done_runs(progress_path: str) -> list[str]:
    """Parse progress.md and return list of done run IDs."""
    return get_runs_by_status(progress_path, "done")


def is_run_done(run_file: str, run_id: str) -> bool:
    """Check if a per-run file has status 'done'."""
    return run_id in get_runs_by_status(run_file, "done")


def is_run_classified(run_file: str, run_id: str) -> bool:
    """Check if a per-run file has status 'classified'."""
    return run_id in get_runs_by_status(run_file, "classified")


# ---------------------------------------------------------------------------
# File-level mutations
# ---------------------------------------------------------------------------

def mark_runs_as_error(progress_path: str, run_ids: list[str]) -> None:
    """Set status to 'error' for the given run IDs in progress.md.

    Only replaces 'pending' status -- will not overwrite 'done' if a
    sub-agent finished between the check and the write.
    """
    content = Path(progress_path).read_text()
    for rid in run_ids:
        pattern = (
            r"(<!-- BEGIN RUN " + re.escape(rid) + r" -->.*?)"
            r"- \*\*status\*\*: pending"
        )
        replacement = r"\1- **status**: error"
        content = re.sub(pattern, replacement, content, count=1, flags=re.DOTALL)
    Path(progress_path).write_text(content)


def split_progress(progress_path: str, run_ids: list[str]) -> dict[str, str]:
    """Split progress.md into per-run files. Returns {run_id: file_path}."""
    content = Path(progress_path).read_text()
    runs_dir = Path(progress_path).parent / "runs"
    runs_dir.mkdir(exist_ok=True)

    run_files = {}
    for rid in run_ids:
        pattern = rf"(<!-- BEGIN RUN {re.escape(rid)} -->.*?<!-- END RUN {re.escape(rid)} -->)"
        match = re.search(pattern, content, re.DOTALL)
        if not match:
            logger.warning("Run %s not found in %s", rid, progress_path)
            continue
        run_file = runs_dir / f"run-{rid}.md"
        run_file.write_text(match.group(1) + "\n")
        run_files[rid] = str(run_file)

    return run_files


def merge_run(progress_path: str, run_id: str, run_file_path: str,
              expected_status: str = "done") -> bool:
    """Merge one per-run file back into progress.md. Returns True on success."""
    content = Path(progress_path).read_text()
    run_content = Path(run_file_path).read_text()

    pattern = rf"(<!-- BEGIN RUN {re.escape(run_id)} -->.*?<!-- END RUN {re.escape(run_id)} -->)"
    match = re.search(pattern, run_content, re.DOTALL)
    if not match:
        logger.warning("Run section not found in %s, skipping", run_file_path)
        return False

    new_content, count = re.subn(pattern, match.group(1), content, count=1, flags=re.DOTALL)
    if count == 0:
        logger.warning("Run %s block not found in %s, nothing to replace",
                       run_id, progress_path)
        return False

    Path(progress_path).write_text(new_content)

    # Verify the merge
    if run_id not in get_runs_by_status(progress_path, expected_status):
        logger.error("Run %s merge verification FAILED -- "
                     "status not %r in %s after write",
                     run_id, expected_status, progress_path)
        return False

    return True


def rebuild_categories_section(progress_path: str) -> None:
    """Rebuild the Categories So Far section from actual run data.

    Scans all done/classified run blocks, extracts category fields,
    groups by category (first 2 path segments), and replaces the
    CATEGORIES START/END block with accurate entries.
    """
    content = Path(progress_path).read_text()

    cats: dict[str, str] = {}  # category -> first summary
    for _, body in re.findall(RUN_BLOCK_RE, content, re.DOTALL):
        status = parse_field(body, "status")
        if status not in ("done", "classified"):
            continue
        job_pattern = r"#### job: `[^`]+`(.*?)(?=#### job:|\Z)"
        for job_body in re.findall(job_pattern, body, re.DOTALL):
            cat_val = parse_field(job_body, "category")
            if not cat_val or not cat_val.startswith(VALID_CATEGORY_PREFIXES):
                continue
            parts = cat_val.split("/")
            cat_key = "/".join(parts[:2]) if len(parts) >= 2 else cat_val
            if cat_key not in cats:
                summary = parse_field(job_body, "summary")
                if summary and len(summary) > 120:
                    summary = summary[:117] + "..."
                cats[cat_key] = summary

    if cats:
        lines = []
        for cat_key in sorted(cats):
            desc = cats[cat_key]
            if desc:
                lines.append(f"- `{cat_key}` -- {desc}")
            else:
                lines.append(f"- `{cat_key}`")
        section = "\n".join(lines)
    else:
        section = "(none yet)"

    new_content = re.sub(
        r"<!-- CATEGORIES START -->.*?<!-- CATEGORIES END -->",
        f"<!-- CATEGORIES START -->\n{section}\n<!-- CATEGORIES END -->",
        content, count=1, flags=re.DOTALL,
    )
    Path(progress_path).write_text(new_content)


def promote_run_status(progress_path: str, run_id: str,
                       from_status: str, to_status: str) -> None:
    """Change a single run's status from from_status to to_status.

    Idempotent -- if from_status doesn't match, the file is unchanged.
    """
    content = Path(progress_path).read_text()
    pattern = (
        r"(<!-- BEGIN RUN " + re.escape(run_id) + r" -->.*?)"
        r"- \*\*status\*\*: " + re.escape(from_status)
    )
    content = re.sub(pattern, rf"\1- **status**: {to_status}",
                     content, count=1, flags=re.DOTALL)
    Path(progress_path).write_text(content)
