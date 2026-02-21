"""MCP tools for classifier and correlator sub-agents.

Provides download_log, git, and gh tools. All tools have repo/clone_dir
baked in via closure -- agents never pass repo slugs.
"""

import os
import shlex
import subprocess
import sys

from claude_agent_sdk import create_sdk_mcp_server, tool

from flakectl.github import download_job_log

# Read-only git subcommands that agents are allowed to run.
_GIT_ALLOWED = frozenset({
    "log", "show", "diff", "blame", "shortlog", "ls-files", "ls-tree",
    "rev-parse", "cat-file", "describe", "name-rev", "status",
})

# Allowed gh subcommand prefixes (first one or two positional args).
_GH_ALLOWED_PREFIXES = frozenset({
    "run list", "run view", "pr list", "pr view", "pr diff",
    "issue list", "issue view", "search", "api",
})

# gh subcommands that accept the --repo flag.
_GH_REPO_SUBCOMMANDS = frozenset({
    "run list", "run view", "pr list", "pr view", "pr diff",
    "issue list", "issue view",
})

# Write operations that must be blocked in gh args.
_GH_WRITE_OPS = frozenset({
    "create", "delete", "edit", "close", "merge", "reopen", "comment",
})


def _mcp_error(msg: str) -> dict:
    """Return an MCP error response."""
    return {
        "content": [{"type": "text", "text": msg}],
        "is_error": True,
    }


def _mcp_text(text: str) -> dict:
    """Return an MCP text response."""
    return {"content": [{"type": "text", "text": text}]}


def _validate_git_args(args_str: str) -> str | None:
    """Validate git args. Returns error message or None if valid."""
    try:
        parts = shlex.split(args_str)
    except ValueError as e:
        return f"Invalid args: {e}"
    if not parts:
        return "No git subcommand provided"
    subcmd = parts[0]
    if subcmd not in _GIT_ALLOWED:
        return (
            f"Blocked git subcommand: '{subcmd}'. "
            f"Allowed: {', '.join(sorted(_GIT_ALLOWED))}"
        )
    return None


def _parse_gh_prefix(parts: list[str]) -> str | None:
    """Extract the gh subcommand prefix from parsed args.

    Returns the matched prefix string, or None if no allowed prefix matches.
    """
    if len(parts) >= 2:
        two_word = f"{parts[0]} {parts[1]}"
        if two_word in _GH_ALLOWED_PREFIXES:
            return two_word
    if parts and parts[0] in _GH_ALLOWED_PREFIXES:
        return parts[0]
    return None


def _validate_gh_args(args_str: str, repo: str) -> str | None:
    """Validate gh args. Returns error message or None if valid."""
    try:
        parts = shlex.split(args_str)
    except ValueError as e:
        return f"Invalid args: {e}"
    if not parts:
        return "No gh subcommand provided"

    prefix = _parse_gh_prefix(parts)
    if prefix is None:
        return (
            f"Blocked gh subcommand: '{' '.join(parts[:2])}'. "
            f"Allowed: {', '.join(sorted(_GH_ALLOWED_PREFIXES))}"
        )

    # Block write operations anywhere in the args
    for part in parts:
        if part.lower() in _GH_WRITE_OPS:
            return f"Write operation blocked: '{part}'"

    # For 'api' subcommand, validate the URL path
    if prefix == "api":
        # Find the API path (first positional arg after 'api')
        api_args = parts[1:]
        api_path = None
        for arg in api_args:
            if not arg.startswith("-"):
                api_path = arg
                break
        if api_path is None:
            return "No API path provided"

        # Validate path starts with repos/{repo}/
        expected = f"repos/{repo}/"
        if not api_path.startswith(expected):
            return (
                f"API path must start with '{expected}', "
                f"got: '{api_path}'"
            )

        # Block non-GET methods
        for i, arg in enumerate(api_args):
            if arg in ("--method", "-X") and i + 1 < len(api_args):
                method = api_args[i + 1].upper()
                if method != "GET":
                    return f"Only GET method allowed, got: {method}"

    return None


def create_tools_server(repo: str, repo_dir: str | None = None):
    """Create MCP server with download_log, git, and gh tools.

    All tools have repo/clone_dir baked in -- agents never pass repo slugs.

    Parameters
    ----------
    repo : str
        GitHub repo slug (owner/name).
    repo_dir : str | None
        Path to the local repo clone. If provided, the git tool is included.
    """

    @tool(
        "download_log",
        "Download the full log for a specific GitHub Actions job and save it "
        "to a file. The response includes line count so you can estimate size. "
        "Keep log queries small: use Grep with head_limit and Read only with "
        "small offset/limit windows.",
        {"job_id": int, "output": str},
    )
    async def download_log_tool(args):
        job_id = args["job_id"]
        output = os.path.join("files", os.path.basename(args["output"]))
        os.makedirs("files", exist_ok=True)
        if os.path.exists(output):
            with open(output) as f:
                total_lines = sum(1 for _ in f)
            msg = f"Already saved to {output} ({total_lines} lines, cached)"
            print(f"[job {job_id}] {msg}", file=sys.stderr, flush=True)
            return _mcp_text(msg)
        print(
            f"[job {job_id}] Downloading log -> {output}...",
            file=sys.stderr, flush=True,
        )
        try:
            log = download_job_log(repo, job_id)
            total_lines = log.count("\n") + 1
            with open(output, "w") as f:
                f.write(log)
            msg = f"Log saved to {output} ({total_lines} lines)"
            print(f"[job {job_id}] {msg}", file=sys.stderr, flush=True)
            return _mcp_text(msg)
        except Exception as e:
            print(f"[job {job_id}] Error: {e}", file=sys.stderr, flush=True)
            return _mcp_error(f"Error downloading log: {e}")

    @tool(
        "git",
        "Run a read-only git command on the cloned repo. "
        "The clone directory is set automatically. "
        "Allowed subcommands: log, show, diff, blame, shortlog, ls-files, "
        "ls-tree, rev-parse, cat-file, describe, name-rev, status. "
        "Note: the repo is a shallow clone (--depth 1), so history commands "
        "are limited. Use `gh api` for commit history/diffs. "
        "Commands that work well: show HEAD:path, ls-files, ls-tree, status.",
        {"args": str},
    )
    async def git_cmd(params):
        args_str = params["args"]
        err = _validate_git_args(args_str)
        if err:
            return _mcp_error(err)

        cmd = f"git -C {shlex.quote(repo_dir)} {args_str}"
        print(f"[git] {cmd[:200]}", file=sys.stderr, flush=True)
        try:
            result = subprocess.run(
                ["git", "-C", repo_dir, *shlex.split(args_str)],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return _mcp_error("Command timed out after 30 seconds")

        if result.returncode != 0:
            msg = f"Error (exit {result.returncode}): {result.stderr.strip()}"
            print(f"[git] {msg[:200]}", file=sys.stderr, flush=True)
            return _mcp_error(msg)

        output = result.stdout
        if len(output) > 100_000:
            output = output[:100_000] + "\n... (truncated at 100K chars)"
        chars = len(output)
        lines = output.count("\n")
        print(f"[git] {chars} chars, {lines} line(s)", file=sys.stderr, flush=True)
        return _mcp_text(output)

    @tool(
        "gh",
        "Run a read-only gh CLI command scoped to the repo. "
        "The --repo flag is injected automatically for subcommands that "
        "accept it -- do NOT include --repo or -R in your args. "
        "Allowed subcommands: run list, run view, pr list, pr view, "
        "pr diff, issue list, issue view, search, api. "
        "For api: path must start with repos/OWNER/REPO/. "
        "Examples: "
        "`run list --commit {sha} --json conclusion,name`, "
        "`run view {run_id} --json jobs`, "
        "`pr view {number} --json body,title,files`, "
        "`api repos/OWNER/REPO/commits/{sha}`, "
        "`search commits --repo OWNER/REPO 'query'`.",
        {"args": str},
    )
    async def gh_cmd(params):
        args_str = params["args"]
        err = _validate_gh_args(args_str, repo)
        if err:
            return _mcp_error(err)

        try:
            parts = shlex.split(args_str)
        except ValueError as e:
            return _mcp_error(f"Invalid args: {e}")

        prefix = _parse_gh_prefix(parts)

        # Auto-inject --repo for subcommands that accept it
        cmd_parts = ["gh", *parts]
        if (
            prefix in _GH_REPO_SUBCOMMANDS
            and "--repo" not in parts
            and "-R" not in parts
        ):
                # Insert --repo after the subcommand prefix
                prefix_len = len(prefix.split())
                cmd_parts = (
                    ["gh"] + parts[:prefix_len]
                    + ["--repo", repo]
                    + parts[prefix_len:]
                )

        # For search subcommand, inject --repo if not present
        if prefix == "search" and "--repo" not in parts and "-R" not in parts:
            cmd_parts = ["gh", *parts[:2], "--repo", repo, *parts[2:]]

        cmd_display = " ".join(cmd_parts)
        print(
            f"[gh] {cmd_display[:200]}",
            file=sys.stderr, flush=True,
        )
        try:
            result = subprocess.run(
                cmd_parts, capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            return _mcp_error(
                "gh CLI not found. Install from https://cli.github.com"
            )
        except subprocess.TimeoutExpired:
            return _mcp_error("Command timed out after 30 seconds")

        if result.returncode != 0:
            msg = f"Error (exit {result.returncode}): {result.stderr.strip()}"
            print(f"[gh] {msg[:200]}", file=sys.stderr, flush=True)
            return _mcp_error(msg)

        output = result.stdout
        if len(output) > 100_000:
            output = output[:100_000] + "\n... (truncated at 100K chars)"
        chars = len(output)
        lines = output.count("\n")
        print(f"[gh] {chars} chars, {lines} line(s)", file=sys.stderr, flush=True)
        return _mcp_text(output)

    tools = [download_log_tool]
    if repo_dir:
        tools.append(git_cmd)
    tools.append(gh_cmd)
    return create_sdk_mcp_server(name="github", version="1.0.0", tools=tools)
