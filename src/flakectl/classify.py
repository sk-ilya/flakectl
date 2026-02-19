#!/usr/bin/env python3
"""Orchestrate flaky test classification using Claude Agent SDK.

Launches one classifier agent per pending run. Each agent writes to its
own per-run file under runs/ (zero contention). The orchestrator polls
per-run files for completion, incrementally merges finished runs back
into progress.md so still-running agents can read it for context,
retries failed agents, marks persistent failures as error, then merges
categories.
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)

from flakectl.prompts.classifier import CLASSIFIER_AGENT_PROMPT
from flakectl.tools import create_github_tools_server

logger = logging.getLogger(__name__)

_AGENT_COLORS = [
    "\033[91m", "\033[92m", "\033[93m", "\033[94m", "\033[95m", "\033[96m",
    "\033[31m", "\033[32m", "\033[33m", "\033[34m", "\033[35m", "\033[36m",
]
_RESET = "\033[0m"


def _agent_color(run_id: str) -> str:
    """Return a deterministic ANSI color for a given run ID."""
    return _AGENT_COLORS[hash(run_id) % len(_AGENT_COLORS)]


def _tool_summary(block: ToolUseBlock) -> str:
    """Format a one-line summary of a tool call."""
    return f"{block.name}: {json.dumps(block.input, ensure_ascii=False)}"


def _get_runs_by_status(progress_path: str, status: str) -> list[str]:
    """Parse progress.md and return run IDs matching the given status."""
    content = Path(progress_path).read_text()
    block_pattern = r"<!-- BEGIN RUN (\d+) -->(.*?)<!-- END RUN \1 -->"
    status_pattern = rf"- \*\*status\*\*: {re.escape(status)}"
    return [
        rid for rid, body in re.findall(block_pattern, content, re.DOTALL)
        if re.search(status_pattern, body)
    ]


def get_pending_runs(progress_path: str) -> list[str]:
    """Parse progress.md and return list of pending run IDs."""
    return _get_runs_by_status(progress_path, "pending")


def get_done_runs(progress_path: str) -> list[str]:
    """Parse progress.md and return list of done run IDs."""
    return _get_runs_by_status(progress_path, "done")


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
    if run_id not in _get_runs_by_status(progress_path, expected_status):
        logger.error("Run %s merge verification FAILED -- "
                     "status not %r in %s after write",
                     run_id, expected_status, progress_path)
        return False

    return True


def _parse_field(text: str, field: str) -> str:
    """Extract a field value from a section of text."""
    match = re.search(rf"- \*\*{field}\*\*:\s*(.*)", text)
    return match.group(1).strip() if match else ""


def _rebuild_categories_section(progress_path: str) -> None:
    """Rebuild the Categories So Far section from actual run data.

    Scans all done/classified run blocks, extracts category fields,
    groups by category (first 2 path segments), and replaces the
    CATEGORIES START/END block with accurate entries.
    """
    content = Path(progress_path).read_text()

    _VALID_PREFIXES = ("test-flake/", "infra-flake/", "bug/", "build-error/")

    block_pattern = r"<!-- BEGIN RUN (\d+) -->(.*?)<!-- END RUN \1 -->"
    cats: dict[str, str] = {}  # category -> first summary
    for _, body in re.findall(block_pattern, content, re.DOTALL):
        status = _parse_field(body, "status")
        if status not in ("done", "classified"):
            continue
        job_pattern = r"#### job: `[^`]+`(.*?)(?=#### job:|\Z)"
        for job_body in re.findall(job_pattern, body, re.DOTALL):
            cat_val = _parse_field(job_body, "category")
            if not cat_val or not cat_val.startswith(_VALID_PREFIXES):
                continue
            parts = cat_val.split("/")
            cat_key = "/".join(parts[:2]) if len(parts) >= 2 else cat_val
            if cat_key not in cats:
                summary = _parse_field(job_body, "summary")
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


def _is_run_done(run_file: str, run_id: str) -> bool:
    """Check if a per-run file has status 'done'."""
    return run_id in _get_runs_by_status(run_file, "done")


def _is_run_classified(run_file: str, run_id: str) -> bool:
    """Check if a per-run file has status 'classified'."""
    return run_id in _get_runs_by_status(run_file, "classified")


_RECHECK_PROMPT = """\
Your results have been merged into progress.md. Other agents have also
merged their results while you were working.

Re-read progress.md now and check all categories across all completed runs.
Compare by category (the first two path segments). If another agent used
a different category for the same root cause (same fix), update your
per-run file to use that category instead (keeping your own subcategory).
Pick the category used by the most runs; break ties by earliest
run_started_at. Do NOT edit progress.md directly.

Then set your run status to "done" (replace "classified" with "done").

Hint: a single full read of progress.md should give you everything you
need -- avoid multiple greps or partial reads.
"""


def _build_system_prompt(context: str = "") -> str:
    """Build the system prompt for classifier agents."""
    prompt = CLASSIFIER_AGENT_PROMPT
    if context:
        prompt += (
            "\n\n## Repository-specific context (provided by the user -- high priority)\n\n"
            + context
        )
    return prompt


async def _run_and_merge(
    run_id: str, repo: str, run_file: str, cwd: str,
    progress_path: str, merged: set[str],
    merge_lock: asyncio.Lock,
    context: str = "",
    model: str = "sonnet",
    max_turns: int = 50,
) -> None:
    """Classify a run, merge results, recheck categories, then final-merge.

    Uses a single ClaudeSDKClient session for both classification and
    recheck. The merge between phases ensures the recheck reads up-to-date
    categories from progress.md.
    """
    system_prompt = _build_system_prompt(context=context)
    task = (
        f"Classify run {run_id}. REPO={repo}. "
        f"Your progress file: {run_file}. "
        f"Read progress.md for context (categories, prior runs)."
    )
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt,
        allowed_tools=["Read", "Edit", "Grep",
                       "mcp__github__get_jobs", "mcp__github__download_log",
                       "mcp__github__get_file", "mcp__github__get_commit",
                       "mcp__github__list_repo_dir"],
        permission_mode="acceptEdits",
        max_turns=max_turns,
        cwd=cwd,
        mcp_servers={"github": create_github_tools_server()},
    )
    c = _agent_color(run_id)
    try:
        async with ClaudeSDKClient(options=options) as client:
            # --- Phase 1: Classify ---
            await client.query(task)
            async for message in client.receive_messages():
                if isinstance(message, ResultMessage):
                    if _is_run_classified(run_file, run_id):
                        logger.info(
                            "%s[run %s] Classified in %d turns%s",
                            c, run_id, message.num_turns, _RESET)
                    else:
                        reason = ("hit max_turns=%d" % max_turns
                                  if message.num_turns >= max_turns
                                  else "exited early")
                        logger.warning(
                            "%s[run %s] Classify exited WITHOUT completing "
                            "(%s, %d turns)%s",
                            c, run_id, reason,
                            message.num_turns, _RESET)
                    break
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            logger.info("%s[run %s] %s%s",
                                        c, run_id,
                                        _tool_summary(block), _RESET)
                        elif (isinstance(block, TextBlock)
                              and block.text.strip()):
                            logger.info(
                                "%s[run %s] %s%s",
                                c, run_id,
                                block.text.strip()[:600], _RESET)

            if not _is_run_classified(run_file, run_id):
                return

            # --- Preliminary merge: make results visible to other agents ---
            async with merge_lock:
                ok = merge_run(progress_path, run_id, run_file,
                               expected_status="classified")
            if not ok:
                logger.error("%s[run %s] Preliminary merge FAILED%s",
                             c, run_id, _RESET)
                return
            _rebuild_categories_section(progress_path)
            logger.info("%s[run %s] Preliminary merge into %s%s",
                        c, run_id, progress_path, _RESET)

            # --- Phase 2: Recheck (same session, agent has full context) ---
            await client.query(_RECHECK_PROMPT)
            async for message in client.receive_messages():
                if isinstance(message, ResultMessage):
                    if _is_run_done(run_file, run_id):
                        logger.info(
                            "%s[run %s] Recheck done in %d turns%s",
                            c, run_id, message.num_turns, _RESET)
                    else:
                        logger.warning(
                            "%s[run %s] Recheck exited without setting done%s",
                            c, run_id, _RESET)
                    break
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            logger.info("%s[run %s] recheck: %s%s",
                                        c, run_id,
                                        _tool_summary(block), _RESET)

            if not _is_run_done(run_file, run_id):
                return

            # --- Final merge: update with any category changes from recheck ---
            async with merge_lock:
                ok = merge_run(progress_path, run_id, run_file)
            if ok:
                merged.add(run_id)
                _rebuild_categories_section(progress_path)
                logger.info("%s[run %s] Final merge into %s%s",
                            c, run_id, progress_path, _RESET)
            else:
                logger.error("%s[run %s] Final merge FAILED%s",
                             c, run_id, _RESET)
    except Exception as e:
        logger.warning("%s[run %s] Agent crashed: %s%s",
                       c, run_id, e, _RESET)


async def _classify_all(
    run_ids: list[str],
    repo: str,
    progress_path: str,
    run_files: dict[str, str],
    cwd: str,
    context: str = "",
    model: str = "sonnet",
    max_retries: int = 2,
    stale_timeout_min: int = 60,
    max_turns: int = 50,
) -> tuple[set[str], set[str]]:
    """Launch classifier agents and poll until all finish or retries exhausted.

    Each classifier runs as an independent ClaudeSDKClient session that writes
    to its own per-run file (no contention). Each task merges its results into
    progress.md immediately on completion via _run_and_merge, so still-running
    agents see newly-completed classifications without waiting for a poll cycle.
    Returns (done, unfinished).
    """
    run_id_set = set(run_ids)
    retries: dict[str, int] = {rid: 0 for rid in run_ids}
    merged: set[str] = set()
    merge_lock = asyncio.Lock()

    tasks = {
        rid: asyncio.create_task(
            _run_and_merge(rid, repo, run_files[rid], cwd,
                           progress_path, merged, merge_lock,
                           context=context, model=model,
                           max_turns=max_turns))
        for rid in run_ids
    }
    logger.info("Launched %d classifier agents", len(tasks))

    prev_progress = 0
    last_progress_time = time.monotonic()

    while True:
        await asyncio.sleep(30)

        # Check per-run files for completion
        done = set()
        classified = set()
        for rid in run_id_set:
            if rid in run_files:
                if _is_run_done(run_files[rid], rid):
                    done.add(rid)
                elif _is_run_classified(run_files[rid], rid):
                    classified.add(rid)
        remaining = run_id_set - done

        logger.info("Poll: %d/%d done, %d classified, %d remaining",
                    len(done), len(run_ids), len(classified),
                    len(remaining))

        if not remaining:
            # Per-run files are all "done", but tasks may still be
            # merging.  Continue looping until tasks complete.
            if all(tasks[rid].done() for rid in run_id_set):
                break
            # else: keep polling -- tasks are finishing their merges
            continue

        # Relaunch crashed agents that haven't exhausted retries
        for rid in remaining:
            if tasks[rid].done() and retries[rid] < max_retries:
                retries[rid] += 1
                logger.info("[run %s] Relaunching (retry %d/%d)",
                            rid, retries[rid], max_retries)
                tasks[rid] = asyncio.create_task(
                    _run_and_merge(rid, repo, run_files[rid], cwd,
                                   progress_path, merged, merge_lock,
                                   context=context, model=model,
                                   max_turns=max_turns))

        # All remaining agents exited and retries exhausted
        if all(tasks[rid].done() for rid in remaining):
            logger.info("All agents for %d remaining runs have exited",
                        len(remaining))
            break

        # No progress for stale_timeout_min -> give up
        current_progress = len(done) + len(classified) + len(merged)
        if current_progress > prev_progress:
            last_progress_time = time.monotonic()
            prev_progress = current_progress
        else:
            elapsed_min = (time.monotonic() - last_progress_time) / 60
            if elapsed_min >= stale_timeout_min:
                logger.warning("No progress for %.0f min, stopping",
                               elapsed_min)
                break

    # Cancel any still-running agents (only reached on timeout/exhausted retries)
    pending_tasks = [t for t in tasks.values() if not t.done()]
    if pending_tasks:
        logger.warning("Cancelling %d stuck tasks", len(pending_tasks))
        for task in pending_tasks:
            task.cancel()
        # Yield to let cancellation propagate
        await asyncio.sleep(0)

    # Final check + merge any stragglers (done or classified)
    done = set()
    classified_only = set()
    for rid in run_id_set:
        if rid in run_files:
            if _is_run_done(run_files[rid], rid):
                done.add(rid)
            elif _is_run_classified(run_files[rid], rid):
                classified_only.add(rid)
    stragglers = done - merged
    if stragglers:
        logger.info("Merging %d straggler runs (done): %s",
                    len(stragglers), sorted(stragglers))
    for rid in stragglers:
        c = _agent_color(rid)
        ok = merge_run(progress_path, rid, run_files[rid])
        if ok:
            merged.add(rid)
            logger.info("%s[run %s] Straggler merged into %s%s",
                        c, rid, progress_path, _RESET)
        else:
            logger.error("%s[run %s] Straggler merge FAILED%s", c, rid, _RESET)
    # Also merge runs stuck at "classified" (recheck was cancelled)
    classified_stragglers = classified_only - merged
    if classified_stragglers:
        logger.info("Merging %d straggler runs (classified only): %s",
                    len(classified_stragglers), sorted(classified_stragglers))
    for rid in classified_stragglers:
        c = _agent_color(rid)
        ok = merge_run(progress_path, rid, run_files[rid],
                       expected_status="classified")
        if ok:
            # Promote status from "classified" to "done" so extract.py
            # includes these runs in the final report.
            content = Path(progress_path).read_text()
            pattern = (
                r"(<!-- BEGIN RUN " + re.escape(rid) + r" -->.*?)"
                r"- \*\*status\*\*: classified"
            )
            content = re.sub(pattern, r"\1- **status**: done",
                             content, count=1, flags=re.DOTALL)
            Path(progress_path).write_text(content)
            merged.add(rid)
            done.add(rid)
            logger.info("%s[run %s] Classified straggler merged into %s%s",
                        c, rid, progress_path, _RESET)
        else:
            logger.error("%s[run %s] Classified straggler merge FAILED%s",
                         c, rid, _RESET)

    # Rebuild categories one final time after all stragglers
    if merged:
        _rebuild_categories_section(progress_path)

    unfinished = run_id_set - merged
    logger.info("Merge summary: %d merged, %d done, %d unfinished",
                len(merged), len(done), len(unfinished))
    return done, unfinished


async def run_orchestrator(repo: str, progress: str, workdir: str | None = None, context: str = "", model: str = "sonnet", stale_timeout_min: int = 60, max_turns: int = 50):
    pending = get_pending_runs(progress)
    if not pending:
        logger.info("No pending runs found")
        return

    logger.info("Found %d pending runs", len(pending))
    cwd = workdir or os.getcwd()

    # Split into per-run files (agents write here, zero contention)
    run_files = split_progress(progress, pending)
    run_ids = [rid for rid in pending if rid in run_files]
    missing = sorted(set(pending) - set(run_ids))

    if missing:
        logger.error("Unable to split %d pending run(s): %s",
                     len(missing), missing)
        mark_runs_as_error(progress, missing)
    if not run_ids:
        logger.warning("No runnable runs found after split")
        return

    # Agents read progress.md for context, write to per-run files
    # Orchestrator incrementally merges completed runs back into progress.md
    done, unfinished = await _classify_all(
        run_ids, repo, progress, run_files, cwd, context=context,
        model=model, stale_timeout_min=stale_timeout_min,
        max_turns=max_turns)
    unfinished |= set(missing)

    logger.info("Classification complete: %d done, %d unfinished %s",
                len(done), len(unfinished), sorted(unfinished))

    if unfinished:
        mark_runs_as_error(progress, sorted(unfinished))
        logger.warning("Marked %d runs as error: %s",
                       len(unfinished), sorted(unfinished))



def _log_message(message):
    """Log SDK messages: ResultMessage at INFO, everything else at DEBUG."""
    if isinstance(message, AssistantMessage):
        parts = []
        for block in message.content:
            if isinstance(block, TextBlock) and block.text.strip():
                parts.append(f"text={block.text.strip()[:300]}")
            elif isinstance(block, ToolUseBlock):
                parts.append(f"tool={block.name}")
        logger.debug("AssistantMessage: %s", ", ".join(parts))
    elif isinstance(message, ResultMessage):
        logger.info("Session complete: %d turns",
                     message.num_turns)
    elif isinstance(message, SystemMessage):
        logger.debug("SystemMessage: %s", message.subtype)
    elif isinstance(message, UserMessage):
        logger.debug("UserMessage (tool_use_id=%s)",
                      getattr(message, "parent_tool_use_id", None))


async def _run_summarize(report_md: str, progress: str, cwd: str, model: str = "sonnet") -> None:
    """Launch a postprocess agent to write summary.txt from report.md."""
    system = "Summarize CI failure classification results in concise plain text."
    task = f"""\
Read {report_md} and write a plain-text summary (2-3 sentences) to
`summary.txt` using the Write tool. Include only: total runs analyzed,
how many were flakes vs real failures, and the top 1-2 root-cause
categories by frequency. No markdown, no bullet points, no headings,
no special formatting -- just plain sentences with numbers. This text
is used as a Slack message and CI status line.
If you need more detail about specific runs, you can also read {progress}.
"""
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        allowed_tools=["Read", "Write"],
        permission_mode="acceptEdits",
        max_turns=10,
        cwd=cwd,
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(task)
        async for message in client.receive_messages():
            _log_message(message)
            if isinstance(message, ResultMessage):
                break

    summary_path = Path(cwd) / "summary.txt"
    if summary_path.exists():
        logger.info("\n%s", summary_path.read_text().strip())


def run_summarize(report_md: str, progress: str = "progress.md", workdir: str | None = None, model: str = "sonnet") -> None:
    """Generate summary.txt from report.md."""
    cwd = workdir or os.getcwd()
    asyncio.run(_run_summarize(report_md, progress, cwd, model=model))


def run(repo: str, progress: str = "progress.md", workdir: str | None = None, context: str = "", model: str = "sonnet", stale_timeout_min: int = 60, max_turns: int = 50) -> int:
    """Run the classification orchestrator. Returns 0 on success, 1 if no runs completed."""
    asyncio.run(run_orchestrator(repo, progress, workdir, context=context, model=model, stale_timeout_min=stale_timeout_min, max_turns=max_turns))
    done = get_done_runs(progress)
    if not done:
        return 1
    return 0
