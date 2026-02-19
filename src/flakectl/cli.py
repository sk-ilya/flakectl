#!/usr/bin/env python3
"""Unified CLI for flakectl -- CI failure classifier."""

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime

from flakectl import __version__


def _parse_csv_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _write_no_failures_outputs(
    output_dir: str,
    repo: str,
    branch: str,
    workflow: str,
    lookback_days: int,
) -> None:
    """Write summary/report files for a no-failures run."""
    summary = "No failed workflow runs found for the selected filters."
    date_str = datetime.now(UTC).date().isoformat()

    summary_path = os.path.join(output_dir, "summary.txt")
    with open(summary_path, "w") as f:
        f.write(summary + "\n")

    report_md_path = os.path.join(output_dir, "report.md")
    with open(report_md_path, "w") as f:
        f.write("# Flaky Test Analysis\n\n")
        f.write(f"**Date:** {date_str}\n\n")
        f.write(summary + "\n\n")
        f.write(f"- Repository: `{repo}`\n")
        f.write(f"- Branch filter: `{branch}`\n")
        f.write(f"- Workflow filter: `{workflow}`\n")
        f.write(f"- Look-back days: `{lookback_days}`\n")

    report_json_path = os.path.join(output_dir, "report.json")
    with open(report_json_path, "w") as f:
        json.dump(
            {
                "date": date_str,
                "status": "no-failures",
                "message": summary,
                "total_runs": 0,
                "flake_runs": 0,
                "real_failure_runs": 0,
                "unclear_runs": 0,
                "total_jobs": 0,
                "categories": [],
                "unfinished_runs": [],
            },
            f,
            indent=2,
        )

    logger = logging.getLogger(__name__)
    logger.info(summary)
    logger.info("Wrote %s", summary_path)
    logger.info("Wrote %s", report_md_path)
    logger.info("Wrote %s", report_json_path)


def cmd_fetch(args):
    from flakectl.fetch import run
    return run(args.repo, args.lookback_days, args.workflow, args.branch, args.output)


def cmd_progress(args):
    from flakectl.progress import run
    skip = _parse_csv_list(args.skip_jobs) if args.skip_jobs else []
    return run(args.input, args.output, skip_jobs=skip)


def _resolve_context(value: str) -> str:
    """Resolve --context value: if it starts with @, read from file."""
    if not value:
        return ""
    if value.startswith("@"):
        path = value[1:]
        with open(path) as f:
            return f.read()
    return value


def cmd_classify(args):
    from flakectl.classify import run
    context = _resolve_context(args.context)
    return run(
        args.repo, args.progress, context=context, model=args.model,
        stale_timeout_min=args.stale_timeout, max_turns=args.max_turns,
    )


def cmd_correlate(args):
    from flakectl.correlate import run
    return run(
        args.repo, args.progress,
        lookback_days=args.lookback_days,
        workdir=os.path.dirname(args.progress) or ".",
        model=args.model, max_turns=args.max_turns,
    )


def cmd_extract(args):
    from flakectl.extract import run
    return run(args.input, args.output_md, args.output_json,
               fixes_path=args.fixes)


def cmd_run(args):
    """Chain all steps: fetch -> progress -> classify -> correlate -> extract -> summarize."""
    from flakectl.classify import run as classify_run
    from flakectl.correlate import run as correlate_run
    from flakectl.extract import run as extract_run
    from flakectl.fetch import STATUS_NO_FAILURES
    from flakectl.fetch import run as fetch_run
    from flakectl.progress import run as progress_run

    logger = logging.getLogger(__name__)
    base = args.output_dir
    os.makedirs(base, exist_ok=True)

    csv_path = os.path.join(base, "failed_jobs.csv")
    progress_path = os.path.join(base, "progress.md")

    rc = fetch_run(args.repo, args.lookback_days, args.workflow, args.branch, csv_path)
    if rc == STATUS_NO_FAILURES:
        _write_no_failures_outputs(
            output_dir=base,
            repo=args.repo,
            branch=args.branch,
            workflow=args.workflow,
            lookback_days=args.lookback_days,
        )
        return STATUS_NO_FAILURES
    if rc != 0:
        return rc

    skip = _parse_csv_list(args.skip_jobs) if args.skip_jobs else []
    rc = progress_run(csv_path, progress_path, skip_jobs=skip)
    if rc != 0:
        return rc

    context = _resolve_context(args.context)
    rc = classify_run(
        args.repo, progress_path, workdir=base, context=context,
        model=args.model, stale_timeout_min=args.stale_timeout,
        max_turns=args.max_turns_classify,
    )
    if rc != 0:
        return rc

    # Correlate categories with fix commits/PRs (non-fatal)
    rc = correlate_run(
        args.repo, progress_path,
        lookback_days=args.lookback_days,
        workdir=base, model=args.model, max_turns=args.max_turns_correlate,
    )
    if rc != 0:
        logger.warning("Correlate step failed (non-fatal), continuing")

    fixes_path = os.path.join(base, "fixes.json")
    report_md = os.path.join(base, "report.md")
    rc = extract_run(
        progress_path,
        report_md,
        os.path.join(base, "report.json"),
        fixes_path=fixes_path if os.path.exists(fixes_path) else None,
    )
    if rc != 0:
        return rc

    from flakectl.classify import run_summarize
    run_summarize(report_md, progress_path, workdir=base, model=args.model)

    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="flakectl",
        description="CI failure classifier -- categorizes flaky tests, infra flakes, and real bugs",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging (verbose output)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- fetch ---
    p_fetch = subparsers.add_parser(
        "fetch", help="Fetch failed CI jobs from GitHub Actions",
    )
    p_fetch.add_argument(
        "--repo", required=True,
        help="Target repository (owner/name)",
    )
    p_fetch.add_argument(
        "--lookback-days", type=int, default=7,
        help="Look-back period in days (default: 7)",
    )
    p_fetch.add_argument(
        "--workflow", default="*",
        help="Workflow file: name.yaml, comma-separated list, or * for all (default: *)",
    )
    p_fetch.add_argument(
        "--branch", default="main",
        help="Branch filter: name, comma-separated list, or * for all (default: main)",
    )
    p_fetch.add_argument(
        "--output", default="failed_jobs.csv",
        help="Output CSV file path (default: failed_jobs.csv)",
    )
    p_fetch.set_defaults(func=cmd_fetch)

    # --- progress ---
    p_progress = subparsers.add_parser(
        "progress", help="Generate progress.md from failed jobs CSV",
    )
    p_progress.add_argument(
        "--input", default="failed_jobs.csv",
        help="Input CSV file path (default: failed_jobs.csv)",
    )
    p_progress.add_argument(
        "--output", default="progress.md",
        help="Output progress file path (default: progress.md)",
    )
    p_progress.add_argument(
        "--skip-jobs", default="",
        help="Comma-separated list of job names to skip (default: none)",
    )
    p_progress.set_defaults(func=cmd_progress)

    # --- classify ---
    p_classify = subparsers.add_parser(
        "classify", help="Classify CI failures using Claude Agent SDK",
    )
    p_classify.add_argument(
        "--repo", required=True,
        help="Target repository (owner/name)",
    )
    p_classify.add_argument(
        "--progress", default="progress.md",
        help="Path to progress.md (default: progress.md)",
    )
    p_classify.add_argument(
        "--context", default="",
        help="Repo-specific context for classifier agents (inline text or @file)",
    )
    p_classify.add_argument(
        "--model", default="sonnet",
        help="Claude model for classifier agents (default: sonnet)",
    )
    p_classify.add_argument(
        "--stale-timeout", type=int, default=60,
        help="Minutes with no progress before giving up (default: 60)",
    )
    p_classify.add_argument(
        "--max-turns", type=int, default=50,
        help="Maximum turns per classifier agent (default: 50)",
    )
    p_classify.set_defaults(func=cmd_classify)

    # --- correlate ---
    p_correlate = subparsers.add_parser(
        "correlate",
        help="Correlate classified categories with fix commits/PRs",
    )
    p_correlate.add_argument(
        "--repo", required=True,
        help="Target repository (owner/name)",
    )
    p_correlate.add_argument(
        "--progress", default="progress.md",
        help="Path to progress.md (default: progress.md)",
    )
    p_correlate.add_argument(
        "--lookback-days", type=int, default=7,
        help="Look-back period in days (default: 7)",
    )
    p_correlate.add_argument(
        "--model", default="sonnet",
        help="Claude model for correlator agent (default: sonnet)",
    )
    p_correlate.add_argument(
        "--max-turns", type=int, default=80,
        help="Maximum turns per correlator agent (default: 80)",
    )
    p_correlate.set_defaults(func=cmd_correlate)

    # --- extract ---
    p_extract = subparsers.add_parser(
        "extract", help="Extract results from progress.md into report files",
    )
    p_extract.add_argument(
        "--input", default="progress.md",
        help="Input progress file path (default: progress.md)",
    )
    p_extract.add_argument(
        "--output-md", default="report.md",
        help="Output markdown report path (default: report.md)",
    )
    p_extract.add_argument(
        "--output-json", default="report.json",
        help="Output JSON report path (default: report.json)",
    )
    p_extract.add_argument(
        "--fixes", default=None,
        help="Path to fixes.json from correlate step (optional, auto-detected if in same dir)",
    )
    p_extract.set_defaults(func=cmd_extract)

    # --- run (full pipeline) ---
    p_run = subparsers.add_parser(
        "run",
        help="Run the full pipeline: fetch -> progress -> classify"
        " -> correlate -> extract -> summarize",
    )
    p_run.add_argument(
        "--repo", required=True,
        help="Target repository (owner/name)",
    )
    p_run.add_argument(
        "--lookback-days", type=int, default=7,
        help="Look-back period in days (default: 7)",
    )
    p_run.add_argument(
        "--workflow", default="*",
        help="Workflow file: name.yaml, comma-separated list, or * for all (default: *)",
    )
    p_run.add_argument(
        "--branch", default="main",
        help="Branch filter: name, comma-separated list, or * for all (default: main)",
    )
    p_run.add_argument(
        "--skip-jobs", default="",
        help="Comma-separated list of job names to skip (default: none)",
    )
    p_run.add_argument(
        "--output-dir", default=".",
        help="Directory for output files (default: current directory)",
    )
    p_run.add_argument(
        "--context", default="",
        help="Repo-specific context for classifier agents (inline text or @file)",
    )
    p_run.add_argument(
        "--model", default="sonnet",
        help="Claude model for classifier agents (default: sonnet)",
    )
    p_run.add_argument(
        "--stale-timeout", type=int, default=60,
        help="Minutes with no progress before giving up (default: 60)",
    )
    p_run.add_argument(
        "--max-turns-classify", type=int, default=60,
        help="Maximum turns per classifier agent (default: 60)",
    )
    p_run.add_argument(
        "--max-turns-correlate", type=int, default=80,
        help="Maximum turns for the correlator agent (default: 80)",
    )
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format=(
            "%(message)s" if level == logging.INFO
            else "%(asctime)s %(name)s %(levelname)s %(message)s"
        ),
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
