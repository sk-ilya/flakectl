#!/usr/bin/env python3
"""Correlate classified root-cause categories with potential fix commits/PRs.

Reads progress.md categories, launches a Claude agent that searches GitHub
for commits and open PRs that might fix each root cause, and writes results
to fixes.json. The extract step then incorporates fix links into the report.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
)

from flakectl.agentlog import log_blocks
from flakectl.github import clone_at_ref
from flakectl.progressfile import RUN_BLOCK_RE, parse_categories_section
from flakectl.prompts.correlator import CORRELATOR_AGENT_PROMPT
from flakectl.tools import create_tools_server

logger = logging.getLogger(__name__)


def _extract_branches(content: str) -> list[str]:
    """Extract unique branch names from done runs in progress.md."""
    branches = set()
    for _, body in re.findall(RUN_BLOCK_RE, content, re.DOTALL):
        match = re.search(r"- \*\*branch\*\*:\s*(.*)", body)
        if match:
            branch = match.group(1).strip()
            if branch:
                branches.add(branch)
    return sorted(branches)


def _has_categories(content: str) -> bool:
    """Check if progress.md has any classified categories.

    Checks both the Categories So Far section and filled-in category
    fields in done runs.
    """
    cats = parse_categories_section(content)
    if cats:
        return True
    # Also check for filled category fields in done runs
    return bool(re.search(
        r"- \*\*category\*\*:\s*(test-flake|infra-flake|bug|build-error)/",
        content,
    ))


def _dump_candidates(
    repo: str, since_date: str, cwd: str,
) -> tuple[str, int, str, int]:
    """Dump commits and open PRs to local TSV files for the agent to grep.

    Returns (commits_path, n_commits, prs_path, n_prs).
    """
    commits_path = os.path.join(cwd, "candidates_commits.tsv")
    prs_path = os.path.join(cwd, "candidates_prs.tsv")

    # All commits in the lookback window
    jq_commits = (
        '.[] | "\\(.sha)\\t\\(.commit.author.date)'
        '\\t\\(.commit.message | split("\\n")[0])"'
    )
    try:
        result = subprocess.run(
            ["gh", "api",
             f"repos/{repo}/commits?since={since_date}T00:00:00Z&per_page=100",
             "--paginate", "--jq", jq_commits],
            capture_output=True, text=True, timeout=60,
        )
        Path(commits_path).write_text(result.stdout)
        n_commits = result.stdout.count("\n")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("Failed to dump commits: %s", e)
        Path(commits_path).write_text("")
        n_commits = 0

    # All open PRs
    jq_prs = '.[] | "#\\(.number)\\t\\(.createdAt)\\t\\(.title)\\t\\(.url)"'
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--state", "open",
             "--json", "number,title,url,createdAt", "--jq", jq_prs],
            capture_output=True, text=True, timeout=60,
        )
        Path(prs_path).write_text(result.stdout)
        n_prs = result.stdout.count("\n")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("Failed to dump PRs: %s", e)
        Path(prs_path).write_text("")
        n_prs = 0

    return commits_path, n_commits, prs_path, n_prs


async def _run_correlator(
    repo: str,
    progress_path: str,
    lookback_days: int,
    branches: list[str],
    cwd: str,
    commits_path: str,
    n_commits: int,
    prs_path: str,
    n_prs: int,
    repo_path: str = "",
    model: str = "sonnet",
    max_turns: int = 80,
) -> None:
    """Launch a single correlator agent to match categories to fixes."""
    since_date = (
        datetime.now(UTC) - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")

    repo_msg = (
        f"Source repository is cloned at `{repo_path}`.\n"
        if repo_path else ""
    )
    task = (
        f"Match root-cause categories in {progress_path} to fix commits/PRs.\n"
        f"REPO={repo}. Lookback: {lookback_days} days (since {since_date}).\n"
        f"Branches with failures: {', '.join(branches) if branches else 'main'}.\n"
        f"{repo_msg}\n"
        f"Pre-fetched data (Grep these first -- free, no rate limit):\n"
        f"- {commits_path}: all {n_commits} commits in the lookback window "
        f"(TSV: sha, date, subject)\n"
        f"- {prs_path}: all {n_prs} open PRs "
        f"(TSV: #number, date, title, url)\n"
        f"\n"
        f"Write results to fixes.json."
    )

    options = ClaudeAgentOptions(
        model=model,
        system_prompt=CORRELATOR_AGENT_PROMPT,
        allowed_tools=[
            "Read", "Grep", "Write", "Glob",
            "mcp__github__git",
            "mcp__github__gh",
        ],
        permission_mode="acceptEdits",
        max_turns=max_turns,
        cwd=cwd,
        mcp_servers={"github": create_tools_server(repo, repo_dir=repo_path)},
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(task)
        async for message in client.receive_messages():
            if isinstance(message, ResultMessage):
                logger.info(
                    "[correlate] Done in %d turns", message.num_turns,
                )
                break
            elif isinstance(message, AssistantMessage):
                log_blocks(message, "[correlate] ")


def run(
    repo: str,
    progress_path: str = "progress.md",
    lookback_days: int = 7,
    workdir: str | None = None,
    model: str = "sonnet",
    max_turns: int = 80,
) -> int:
    """Run the correlator step. Returns 0 on success."""
    cwd = workdir or os.getcwd()
    fixes_path = os.path.join(cwd, "fixes.json")

    # Read progress.md
    content = Path(progress_path).read_text()

    # Check if any categories exist
    if not _has_categories(content):
        logger.info("No categories found in %s, skipping correlation",
                     progress_path)
        Path(fixes_path).write_text('{"fixes": []}\n')
        return 0

    # Extract branches from classified runs
    branches = _extract_branches(content)
    logger.info("Found branches: %s", branches or ["(none)"])

    # Pre-dump commits and open PRs for the agent to grep
    since_date = (
        datetime.now(UTC) - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")
    commits_path, n_commits, prs_path, n_prs = _dump_candidates(
        repo, since_date, cwd,
    )
    logger.info("Dumped %d commits to %s, %d open PRs to %s",
                n_commits, commits_path, n_prs, prs_path)

    # Ensure a repo clone exists for the correlator (clone at default branch)
    repo_base = os.path.join(cwd, "repo")
    repo_head = os.path.join(repo_base, "HEAD")
    if not os.path.exists(os.path.join(repo_head, ".git")):
        logger.info("Cloning %s at HEAD into %s...", repo, repo_head)
        try:
            clone_at_ref(repo, repo_head, "HEAD")
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "Failed to clone repo for correlator: %s",
                exc.stderr.decode() if exc.stderr else exc,
            )
            repo_head = ""
    repo_path = repo_head if os.path.exists(
        os.path.join(repo_head, ".git")
    ) else ""

    # Launch agent
    logger.info("Starting correlator agent (model=%s, max_turns=%d)",
                model, max_turns)
    asyncio.run(_run_correlator(
        repo, progress_path, lookback_days, branches, cwd,
        commits_path=commits_path, n_commits=n_commits,
        prs_path=prs_path, n_prs=n_prs,
        repo_path=repo_path,
        model=model, max_turns=max_turns,
    ))

    # Ensure fixes.json exists (create empty if agent didn't write it)
    if not os.path.exists(fixes_path):
        logger.warning("Agent did not write fixes.json, creating empty file")
        Path(fixes_path).write_text('{"fixes": []}\n')
    else:
        # Validate JSON
        try:
            with open(fixes_path) as f:
                data = json.load(f)
            if "fixes" not in data:
                logger.warning("fixes.json missing 'fixes' key, rewriting")
                Path(fixes_path).write_text('{"fixes": []}\n')
            else:
                n = sum(len(entry.get("items", [])) for entry in data["fixes"])
                logger.info("fixes.json: %d categories, %d fix items",
                            len(data["fixes"]), n)
        except (json.JSONDecodeError, TypeError):
            logger.warning("fixes.json is malformed, rewriting empty")
            Path(fixes_path).write_text('{"fixes": []}\n')

    return 0
