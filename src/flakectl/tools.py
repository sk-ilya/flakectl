"""Custom MCP tools for classifier and correlator sub-agents.

Provides get_jobs and download_log tools so the classifier agent can
interact with GitHub Actions without shelling out to CLI commands.
Also provides gh_search for the correlator agent.
"""

import os
import shlex
import subprocess
import sys

from claude_agent_sdk import create_sdk_mcp_server, tool

from flakectl.github import (
    download_job_log,
    get_commit_info,
    get_file_content,
    list_directory,
    list_failed_jobs,
)


@tool(
    "get_jobs",
    "List failed jobs for a GitHub Actions workflow run. "
    "Returns tab-separated lines of job_id and job_name.",
    {"repo": str, "run_id": int},
)
async def get_jobs(args):
    run_id = args["run_id"]
    print(f"[run {run_id}] Fetching failed jobs...", file=sys.stderr, flush=True)
    try:
        jobs = list_failed_jobs(args["repo"], run_id)
        names = ", ".join(j["name"] for j in jobs)
        print(
            f"[run {run_id}] Found {len(jobs)} failed job(s): {names}",
            file=sys.stderr, flush=True,
        )
        lines = [f"{job['id']}\t{job['name']}" for job in jobs]
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}
    except Exception as e:
        print(f"[run {run_id}] Error: {e}", file=sys.stderr, flush=True)
        return {
            "content": [{"type": "text", "text": f"Error fetching jobs: {e}"}],
            "is_error": True,
        }


@tool(
    "download_log",
    "Download the full log for a specific GitHub Actions job and save it "
    "to a file. The response includes line count so you can estimate size. "
    "Keep log queries small: use Grep with head_limit and Read only with "
    "small offset/limit windows.",
    {"repo": str, "job_id": int, "output": str},
)
async def download_log(args):
    job_id = args["job_id"]
    output = os.path.join("files", os.path.basename(args["output"]))
    os.makedirs("files", exist_ok=True)
    if os.path.exists(output):
        with open(output) as f:
            total_lines = sum(1 for _ in f)
        msg = f"Already saved to {output} ({total_lines} lines, cached)"
        print(f"[job {job_id}] {msg}", file=sys.stderr, flush=True)
        return {"content": [{"type": "text", "text": msg}]}
    print(
        f"[job {job_id}] Downloading log -> {output}...",
        file=sys.stderr, flush=True,
    )
    try:
        log = download_job_log(args["repo"], job_id)
        total_lines = log.count("\n") + 1
        with open(output, "w") as f:
            f.write(log)
        msg = f"Log saved to {output} ({total_lines} lines)"
        print(f"[job {job_id}] {msg}", file=sys.stderr, flush=True)
        return {"content": [{"type": "text", "text": msg}]}
    except Exception as e:
        print(f"[job {job_id}] Error: {e}", file=sys.stderr, flush=True)
        return {
            "content": [{"type": "text", "text": f"Error downloading log: {e}"}],
            "is_error": True,
        }


@tool(
    "get_file",
    "Download a file from the source repository at a specific git ref "
    "(commit SHA, branch, or tag) and save it to disk. The file is saved "
    "under files/{ref}/{repo_path} -- the response includes the local path. "
    "Use Grep/Read with head_limit/offset/limit to navigate the saved file "
    "(same as with log files). Max file size 1MB.",
    {"repo": str, "path": str, "ref": str},
)
async def get_file(args):
    path = args["path"]
    ref = args["ref"]
    output = os.path.join("files", ref[:8], path)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    if os.path.exists(output):
        with open(output) as f:
            total_lines = sum(1 for _ in f)
        msg = f"Already saved to {output} ({total_lines} lines, cached)"
        print(f"[repo] {msg}", file=sys.stderr, flush=True)
        return {"content": [{"type": "text", "text": msg}]}
    print(f"[repo] Reading {path}@{ref[:8]}...", file=sys.stderr, flush=True)
    try:
        content = get_file_content(args["repo"], path, ref)
        total_lines = content.count("\n") + 1
        with open(output, "w") as f:
            f.write(content)
        msg = f"Saved to {output} ({total_lines} lines)"
        print(f"[repo] {msg}", file=sys.stderr, flush=True)
        return {"content": [{"type": "text", "text": msg}]}
    except Exception as e:
        print(f"[repo] Error reading {path}: {e}", file=sys.stderr, flush=True)
        return {
            "content": [{"type": "text", "text": f"Error reading file: {e}"}],
            "is_error": True,
        }


@tool(
    "get_commit",
    "Get commit metadata and list of changed files for a specific commit SHA. "
    "Returns commit message, author, date, and for each changed file: filename, "
    "status (added/modified/removed), additions, and deletions. No patch diffs "
    "are included -- use get_file to read specific files if needed.",
    {"repo": str, "sha": str},
)
async def get_commit(args):
    sha = args["sha"]
    print(f"[repo] Fetching commit {sha[:8]}...", file=sys.stderr, flush=True)
    try:
        info = get_commit_info(args["repo"], sha)
        parts = [
            f"commit {info['sha']}",
            f"Author: {info['author']}",
            f"Date: {info['date']}",
            "",
            info["message"],
            "",
            f"--- {len(info['files'])} file(s) changed ---",
        ]
        for f in info["files"]:
            parts.append(
                f"{f['status']}: {f['filename']} "
                f"(+{f['additions']} -{f['deletions']})"
            )
        text = "\n".join(parts)
        print(
            f"[repo] Commit {sha[:8]}: {len(info['files'])} file(s) changed",
            file=sys.stderr, flush=True,
        )
        return {"content": [{"type": "text", "text": text}]}
    except Exception as e:
        print(f"[repo] Error fetching commit: {e}", file=sys.stderr, flush=True)
        return {
            "content": [{"type": "text", "text": f"Error fetching commit: {e}"}],
            "is_error": True,
        }


@tool(
    "list_repo_dir",
    "List files and directories at a path in the source repository at a "
    "specific git ref. Returns tab-separated lines with type (f/d), path, "
    "and size. Use this to navigate the repo before reading specific files.",
    {"repo": str, "path": str, "ref": str},
)
async def list_repo_dir(args):
    path = args["path"]
    ref = args["ref"]
    print(f"[repo] Listing {path}@{ref[:8]}...", file=sys.stderr, flush=True)
    try:
        entries = list_directory(args["repo"], path, ref)
        lines = []
        for entry in entries:
            prefix = "d" if entry["type"] == "dir" else "f"
            lines.append(f"{prefix}\t{entry['path']}\t{entry['size']}")
        text = "\n".join(lines)
        print(
            f"[repo] Listed {path}: {len(entries)} entries",
            file=sys.stderr, flush=True,
        )
        return {"content": [{"type": "text", "text": text}]}
    except Exception as e:
        print(f"[repo] Error listing {path}: {e}", file=sys.stderr, flush=True)
        return {
            "content": [{"type": "text", "text": f"Error listing directory: {e}"}],
            "is_error": True,
        }


def create_github_tools_server():
    """Create an MCP server with GitHub Actions and repository tools."""
    return create_sdk_mcp_server(
        name="github",
        version="1.0.0",
        tools=[get_jobs, download_log, get_file, get_commit, list_repo_dir],
    )


def _mcp_error(msg: str) -> dict:
    """Return an MCP error response."""
    return {
        "content": [{"type": "text", "text": msg}],
        "is_error": True,
    }


def create_correlate_tools_server(repo: str):
    """Create an MCP server with tools for the correlate agent.

    The repo parameter is baked into gh_search so the agent cannot
    query arbitrary repositories.
    """
    _ALLOWED_SUBJECTS = {"commits", "prs", "issues", "code"}

    @tool(
        "gh_search",
        "Search GitHub using the gh CLI. "
        "Runs: gh search <subject> --repo <REPO> <args>. "
        "The --repo flag is pre-set -- do NOT include --repo or -R. "
        "subject: commits, prs, issues, or code. "
        "args: free-form gh search flags and query terms. "
        "Use --json for structured output (e.g. --json sha,commit for "
        "commits; --json number,title,url for PRs). "
        "RATE LIMIT: ~30 requests/minute. Plan searches upfront, use "
        "broad queries, prefer test IDs as search terms.",
        {"subject": str, "args": str},
    )
    async def gh_search(params):
        subject = params["subject"]
        if subject not in _ALLOWED_SUBJECTS:
            return _mcp_error(
                f"Invalid subject '{subject}'. "
                f"Must be one of: {', '.join(sorted(_ALLOWED_SUBJECTS))}"
            )
        try:
            parsed_args = shlex.split(params["args"])
        except ValueError as e:
            return _mcp_error(f"Invalid args: {e}")

        cmd = ["gh", "search", subject, "--repo", repo, *parsed_args]
        print(
            f"[correlate] gh search {subject} {params['args'][:120]}",
            file=sys.stderr, flush=True,
        )
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            return _mcp_error(
                "gh CLI not found. Install from https://cli.github.com"
            )
        except subprocess.TimeoutExpired:
            return _mcp_error("Command timed out after 30 seconds")

        if result.returncode != 0:
            msg = f"Error (exit {result.returncode}): {result.stderr.strip()}"
            print(f"[correlate] {msg[:200]}", file=sys.stderr, flush=True)
            return _mcp_error(msg)

        output = result.stdout
        lines = output.count("\n")
        print(
            f"[correlate] Got {lines} result line(s)",
            file=sys.stderr, flush=True,
        )
        return {"content": [{"type": "text", "text": output}]}

    return create_sdk_mcp_server(
        name="correlate",
        version="1.0.0",
        tools=[gh_search, get_commit, get_file],
    )
