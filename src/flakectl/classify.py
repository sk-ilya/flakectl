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
import logging
import os
import time
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
)

from flakectl.agentlog import RESET, agent_color, log_blocks
from flakectl.progressfile import (
    get_done_runs,
    get_pending_runs,
    is_run_classified,
    is_run_done,
    mark_runs_as_error,
    merge_run,
    promote_run_status,
    rebuild_categories_section,
    split_progress,
)
from flakectl.prompts.classifier import RECHECK_PROMPT, build_system_prompt
from flakectl.tools import create_github_tools_server

logger = logging.getLogger(__name__)


async def _run_agent_phase(
    client, prompt, check_fn, run_file: str, run_id: str,
    phase_name: str, color: str, max_turns: int | None = None,
) -> bool:
    """Run one agent phase (query + receive loop), return whether check_fn passes.

    Logs success/failure with turn count. When max_turns is provided, the
    failure message notes whether the turn limit was hit.
    """
    await client.query(prompt)
    async for message in client.receive_messages():
        if isinstance(message, ResultMessage):
            if check_fn(run_file, run_id):
                logger.info(
                    "%s[run %s] %s done in %d turns%s",
                    color, run_id, phase_name.capitalize(),
                    message.num_turns, RESET)
            else:
                if max_turns is not None and message.num_turns >= max_turns:
                    reason = f"hit max_turns={max_turns}"
                else:
                    reason = "exited early"
                logger.warning(
                    "%s[run %s] %s exited without completing "
                    "(%s, %d turns)%s",
                    color, run_id, phase_name.capitalize(), reason,
                    message.num_turns, RESET)
            break
        elif isinstance(message, AssistantMessage):
            log_blocks(message, f"{color}[run {run_id}] {phase_name}: ", RESET)
    return check_fn(run_file, run_id)


def _scan_run_statuses(
    run_ids: set[str], run_files: dict[str, str],
) -> tuple[set[str], set[str]]:
    """Scan per-run files, return (done, classified) sets."""
    done = set()
    classified = set()
    for rid in run_ids:
        if rid in run_files:
            if is_run_done(run_files[rid], rid):
                done.add(rid)
            elif is_run_classified(run_files[rid], rid):
                classified.add(rid)
    return done, classified


def _merge_stragglers(
    run_id_set: set[str], run_files: dict[str, str],
    progress_path: str, merged: set[str],
) -> set[str]:
    """Merge unmerged done/classified runs. Returns final done set. Mutates merged.

    Called after all asyncio tasks are done/cancelled -- no concurrency concerns.
    Done stragglers are merged first, then classified stragglers (promoted to done).
    """
    done, classified_only = _scan_run_statuses(run_id_set, run_files)

    stragglers = done - merged
    if stragglers:
        logger.info("Merging %d straggler runs (done): %s",
                    len(stragglers), sorted(stragglers))
    for rid in stragglers:
        c = agent_color(rid)
        ok = merge_run(progress_path, rid, run_files[rid])
        if ok:
            merged.add(rid)
            logger.info("%s[run %s] Straggler merged into %s%s",
                        c, rid, progress_path, RESET)
        else:
            logger.error("%s[run %s] Straggler merge FAILED%s", c, rid, RESET)

    classified_stragglers = classified_only - merged
    if classified_stragglers:
        logger.info("Merging %d straggler runs (classified only): %s",
                    len(classified_stragglers), sorted(classified_stragglers))
    for rid in classified_stragglers:
        c = agent_color(rid)
        ok = merge_run(progress_path, rid, run_files[rid],
                       expected_status="classified")
        if ok:
            promote_run_status(progress_path, rid, "classified", "done")
            merged.add(rid)
            done.add(rid)
            logger.info("%s[run %s] Classified straggler merged into %s%s",
                        c, rid, progress_path, RESET)
        else:
            logger.error("%s[run %s] Classified straggler merge FAILED%s",
                         c, rid, RESET)

    return done


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
    system_prompt = build_system_prompt(context=context)
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
    c = agent_color(run_id)
    try:
        async with ClaudeSDKClient(options=options) as client:
            # Phase 1: Classify
            ok = await _run_agent_phase(
                client, task, is_run_classified, run_file, run_id,
                "classify", c, max_turns=max_turns)
            if not ok:
                return

            # Preliminary merge: make results visible to other agents
            async with merge_lock:
                ok = merge_run(progress_path, run_id, run_file,
                               expected_status="classified")
            if not ok:
                logger.error("%s[run %s] Preliminary merge FAILED%s",
                             c, run_id, RESET)
                return
            rebuild_categories_section(progress_path)
            logger.info("%s[run %s] Preliminary merge into %s%s",
                        c, run_id, progress_path, RESET)

            # Phase 2: Recheck (same session, agent has full context)
            ok = await _run_agent_phase(
                client, RECHECK_PROMPT, is_run_done, run_file, run_id,
                "recheck", c)
            if not ok:
                return

            # Final merge: update with any category changes from recheck
            async with merge_lock:
                ok = merge_run(progress_path, run_id, run_file)
            if ok:
                merged.add(run_id)
                rebuild_categories_section(progress_path)
                logger.info("%s[run %s] Final merge into %s%s",
                            c, run_id, progress_path, RESET)
            else:
                logger.error("%s[run %s] Final merge FAILED%s",
                             c, run_id, RESET)
    except Exception as e:
        logger.warning("%s[run %s] Agent crashed: %s%s",
                       c, run_id, e, RESET)


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

        done, classified = _scan_run_statuses(run_id_set, run_files)
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

    done = _merge_stragglers(run_id_set, run_files, progress_path, merged)

    if merged:
        rebuild_categories_section(progress_path)

    unfinished = run_id_set - merged
    logger.info("Merge summary: %d merged, %d done, %d unfinished",
                len(merged), len(done), len(unfinished))
    return done, unfinished


async def run_orchestrator(
    repo: str, progress: str, workdir: str | None = None,
    context: str = "", model: str = "sonnet",
    stale_timeout_min: int = 60, max_turns: int = 50,
):
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
            if isinstance(message, ResultMessage):
                logger.info("[summarize] Done in %d turns",
                            message.num_turns)
                break
            elif isinstance(message, AssistantMessage):
                log_blocks(message, "[summarize] ")

    summary_path = Path(cwd) / "summary.txt"
    if summary_path.exists():
        logger.info("\n%s", summary_path.read_text().strip())


def run_summarize(
    report_md: str, progress: str = "progress.md",
    workdir: str | None = None, model: str = "sonnet",
) -> None:
    """Generate summary.txt from report.md."""
    cwd = workdir or os.getcwd()
    asyncio.run(_run_summarize(report_md, progress, cwd, model=model))


def run(
    repo: str, progress: str = "progress.md", workdir: str | None = None,
    context: str = "", model: str = "sonnet",
    stale_timeout_min: int = 60, max_turns: int = 50,
) -> int:
    """Run the classification orchestrator. Returns 0 on success, 1 if no runs completed."""
    asyncio.run(run_orchestrator(
        repo, progress, workdir, context=context, model=model,
        stale_timeout_min=stale_timeout_min, max_turns=max_turns,
    ))
    done = get_done_runs(progress)
    if not done:
        return 1
    return 0
