#!/usr/bin/env python3
"""Orchestrate flaky test classification using Claude Agent SDK.

Launches one classifier agent per pending run. Each agent writes to its
own per-run file under runs/ (zero contention). Classification runs in
two phases separated by a synchronization gate:

  Phase 1 (classify): all agents classify in parallel, merging results
  into progress.md as they finish. Agents wait at the gate after merging.

  Phase 2 (recheck): once all agents have classified (or failed), the
  gate opens and agents recheck categories with full visibility of every
  classification. Same client session -- agents have full context.

This ensures every recheck sees every category, eliminating race
conditions where early rechecks miss late classifications.
"""

import asyncio
import logging
import os
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
)

from flakectl.agentlog import RESET, agent_color, log_blocks
from flakectl.github import ensure_repo_clones
from flakectl.progressfile import (
    get_commit_shas,
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
from flakectl.tools import create_tools_server

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


async def _run_and_merge(
    run_id: str, repo: str, run_file: str, cwd: str,
    progress_path: str, merged: set[str],
    merge_lock: asyncio.Lock,
    classify_counter: list[int],
    total_agents: int,
    recheck_gate: asyncio.Event,
    repo_path: str = "",
    context: str = "",
    model: str = "sonnet",
    max_turns: int = 50,
) -> None:
    """Classify a run, wait for all agents, then recheck.

    Uses a single ClaudeSDKClient session for both phases so the recheck
    agent has full conversation context from classification. A gate event
    synchronizes between phases: the agent waits after classify until all
    agents have finished classifying (or crashed), then proceeds to recheck
    with full visibility of every category.
    """
    system_prompt = build_system_prompt(context=context)
    repo_msg = (
        f"Source repository at this commit is cloned at `{repo_path}`. "
        if repo_path else ""
    )
    task = (
        f"Classify run {run_id}. REPO={repo}. "
        f"Your progress file: {run_file}. "
        f"{repo_msg}"
        f"Read progress.md for context (categories, prior runs)."
    )
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt,
        allowed_tools=["Read", "Edit", "Grep", "Glob",
                       "mcp__github__download_log",
                       "mcp__github__git",
                       "mcp__github__gh"],
        permission_mode="acceptEdits",
        max_turns=max_turns,
        cwd=cwd,
        mcp_servers={"github": create_tools_server(repo, repo_dir=repo_path)},
    )
    c = agent_color(run_id)
    classified = False
    counted = False  # whether this agent has incremented classify_counter

    def _signal_classify_done():
        """Increment counter and open gate if all agents are done."""
        nonlocal counted
        if counted:
            return
        counted = True
        classify_counter[0] += 1
        if classify_counter[0] >= total_agents and not recheck_gate.is_set():
            rebuild_categories_section(progress_path)
            logger.info("All %d agents finished classify, "
                        "opening recheck gate", total_agents)
            recheck_gate.set()

    try:
        async with ClaudeSDKClient(options=options) as client:
            # Phase 1: Classify
            ok = await _run_agent_phase(
                client, task, is_run_classified, run_file, run_id,
                "classify", c, max_turns=max_turns)
            if ok:
                async with merge_lock:
                    ok = merge_run(progress_path, run_id, run_file,
                                   expected_status="classified")
                if ok:
                    classified = True
                    rebuild_categories_section(progress_path)
                    logger.info("%s[run %s] Preliminary merge into %s%s",
                                c, run_id, progress_path, RESET)
                else:
                    logger.error("%s[run %s] Preliminary merge FAILED%s",
                                 c, run_id, RESET)

            # Signal classify done and wait for all agents
            _signal_classify_done()
            await recheck_gate.wait()

            if not classified:
                return

            # Phase 2: Recheck (same session, full context)
            ok = await _run_agent_phase(
                client, RECHECK_PROMPT, is_run_done, run_file, run_id,
                "recheck", c)
            if ok:
                async with merge_lock:
                    ok = merge_run(progress_path, run_id, run_file)
                if ok:
                    merged.add(run_id)
                    logger.info("%s[run %s] Final merge into %s%s",
                                c, run_id, progress_path, RESET)
                else:
                    logger.error("%s[run %s] Final merge FAILED%s",
                                 c, run_id, RESET)
    except Exception as e:
        logger.warning("%s[run %s] Agent crashed: %s%s",
                       c, run_id, e, RESET)
        _signal_classify_done()


async def _classify_all(
    run_ids: list[str],
    repo: str,
    progress_path: str,
    run_files: dict[str, str],
    cwd: str,
    repo_paths: dict[str, str] | None = None,
    context: str = "",
    model: str = "sonnet",
    stale_timeout_min: int = 60,
    max_turns: int = 50,
) -> tuple[set[str], set[str]]:
    """Launch classifier agents with two-phase synchronization.

    Phase 1: all agents classify in parallel, merging as they finish.
    Phase 2: after all classify, agents recheck with full category visibility.

    Returns (done, unfinished).
    """
    run_id_set = set(run_ids)
    merged: set[str] = set()
    merge_lock = asyncio.Lock()
    rp = repo_paths or {}

    # Synchronization: counter tracks how many agents finished classify,
    # gate blocks recheck until all are done.
    classify_counter = [0]
    recheck_gate = asyncio.Event()

    tasks = {
        rid: asyncio.create_task(
            _run_and_merge(rid, repo, run_files[rid], cwd,
                           progress_path, merged, merge_lock,
                           classify_counter, len(run_ids), recheck_gate,
                           repo_path=rp.get(rid, ""),
                           context=context, model=model,
                           max_turns=max_turns))
        for rid in run_ids
    }
    logger.info("Launched %d classifier agents", len(tasks))

    # Periodic status reporter
    async def _status_reporter():
        total = len(run_ids)
        while True:
            await asyncio.sleep(30)
            n_classified = classify_counter[0]
            n_done = len(merged)
            n_tasks_finished = sum(1 for t in tasks.values() if t.done())
            if recheck_gate.is_set():
                logger.info(
                    "Status: classify %d/%d, recheck done %d/%d, "
                    "tasks finished %d/%d",
                    n_classified, total, n_done, n_classified,
                    n_tasks_finished, total)
            else:
                logger.info(
                    "Status: classify %d/%d (waiting for gate), "
                    "tasks finished %d/%d",
                    n_classified, total, n_tasks_finished, total)

    # Watchdog: if no progress for stale_timeout_min, force the gate open
    async def _watchdog():
        timeout = stale_timeout_min * 60
        await asyncio.sleep(timeout)
        if not recheck_gate.is_set():
            logger.warning(
                "Stale timeout (%.0f min): %d/%d agents classified, "
                "forcing recheck gate",
                stale_timeout_min, classify_counter[0], len(run_ids))
            rebuild_categories_section(progress_path)
            recheck_gate.set()

    status_task = asyncio.create_task(_status_reporter())
    watchdog = asyncio.create_task(_watchdog())

    # Wait for all agent tasks to complete
    await asyncio.gather(*tasks.values(), return_exceptions=True)
    status_task.cancel()
    watchdog.cancel()

    # Merge any stragglers (classified but not merged, or done but not merged)
    done, classified_only = set(), set()
    for rid in run_id_set:
        if rid in run_files:
            if is_run_done(run_files[rid], rid):
                done.add(rid)
            elif is_run_classified(run_files[rid], rid):
                classified_only.add(rid)

    for rid in (done | classified_only) - merged:
        c = agent_color(rid)
        is_done = rid in done
        ok = merge_run(progress_path, rid, run_files[rid],
                       expected_status=None if is_done else "classified")
        if ok:
            if not is_done:
                promote_run_status(progress_path, rid, "classified", "done")
            merged.add(rid)
            logger.info("%s[run %s] Straggler merged into %s%s",
                        c, rid, progress_path, RESET)

    if merged:
        rebuild_categories_section(progress_path)

    unfinished = run_id_set - merged
    logger.info("Merge summary: %d merged, %d unfinished",
                len(merged), len(unfinished))
    return merged, unfinished


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

    # Pre-clone the repository for each unique commit SHA
    sha_map = get_commit_shas(progress, pending)
    unique_shas = sorted(set(sha_map.values()))
    repo_paths: dict[str, str] = {}
    if unique_shas:
        logger.info("Cloning %s at %d unique ref(s)...", repo, len(unique_shas))
        repo_base = os.path.join(cwd, "repo")
        clone_map = ensure_repo_clones(repo, repo_base, unique_shas)
        for rid, sha in sha_map.items():
            if sha in clone_map:
                repo_paths[rid] = clone_map[sha]

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

    done, unfinished = await _classify_all(
        run_ids, repo, progress, run_files, cwd,
        repo_paths=repo_paths, context=context,
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
