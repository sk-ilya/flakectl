"""Centralized GitHub client using PyGithub.

All GitHub API calls go through this module.
"""

import functools
import logging
import os
from typing import Any

import requests
from github import Github

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def get_client() -> Github:
    """Create a Github client from GITHUB_TOKEN or GH_TOKEN env var.

    Cached for the lifetime of the process since the token comes from
    environment variables which don't change during a run.
    """
    return Github(_get_token())


def _get_token() -> str:
    """Return the GitHub token from environment."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError(
            "GitHub token not found. Set GITHUB_TOKEN or GH_TOKEN environment variable."
        )
    return token


def _validate_repo(repo_slug: str) -> None:
    """Validate that repo_slug is in 'owner/name' format."""
    if not repo_slug or repo_slug.count("/") != 1:
        raise ValueError(
            f"Invalid repo format: '{repo_slug}'. Expected 'owner/name'."
        )


def _resolve_workflow(repo, workflow: str):
    """Resolve a workflow by filename."""
    if not workflow.endswith((".yml", ".yaml")):
        raise ValueError(
            f"Workflow must be a filename ending in .yml or .yaml: '{workflow}'"
        )
    return repo.get_workflow(workflow)


def list_failed_runs(
    repo_slug: str,
    limit: int,
    workflow: str | None = None,
    branch: str | None = None,
) -> list[dict]:
    """Get failed workflow runs.

    Returns dicts with keys: id, url, name, head_branch, event, head_sha,
    created_at, run_attempt.
    """
    _validate_repo(repo_slug)
    client = get_client()
    repo = client.get_repo(repo_slug)

    kwargs = {"status": "failure"}
    if branch:
        kwargs["branch"] = branch

    if workflow:
        wf = _resolve_workflow(repo, workflow)
        runs = wf.get_runs(**kwargs)
    else:
        runs = repo.get_workflow_runs(**kwargs)

    results = []
    for run in runs[:limit]:
        results.append({
            "id": run.id,
            "url": run.html_url,
            "name": run.name,
            "head_branch": run.head_branch,
            "event": run.event,
            "head_sha": run.head_sha,
            "created_at": run.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_attempt": run.run_attempt,
        })

    return results


def _check_rate_limit(client: Github) -> None:
    """Log a warning if the GitHub API rate limit is running low."""
    try:
        rate = client.get_rate_limit().core
        if rate.remaining < 50:
            logger.warning(
                "GitHub API rate limit low: %d/%d remaining, resets at %s",
                rate.remaining, rate.limit, rate.reset,
            )
    except Exception:
        pass


def list_failed_runs_multi(
    repo_slug: str,
    limit: int,
    workflows: list[str] | None,
    branches: list[str] | None,
) -> list[dict]:
    """Get failed runs across multiple workflows and branches.

    Deduplicates by run ID, sorts by created_at descending.
    """
    seen_ids: set[int] = set()
    all_runs: list[dict] = []

    wf_list = workflows if workflows else [None]
    br_list = branches if branches else [None]

    for wf in wf_list:
        for br in br_list:
            try:
                runs = list_failed_runs(repo_slug, limit, wf, br)
            except Exception as e:
                wf_name = wf if wf is not None else "*"
                br_name = br if br is not None else "*"
                raise RuntimeError(
                    "GitHub workflow run lookup failed "
                    f"(workflow={wf_name}, branch={br_name}): {e}"
                ) from e

            for run in runs:
                if run["id"] not in seen_ids:
                    seen_ids.add(run["id"])
                    all_runs.append(run)

    _check_rate_limit(get_client())
    all_runs.sort(key=lambda r: r["created_at"], reverse=True)
    return all_runs


def list_failed_jobs(repo_slug: str, run_id: int) -> list[dict]:
    """Get failed jobs for a specific workflow run.

    Returns dicts with keys: id, name, conclusion, steps, completed_at.
    Filters to conclusion == "failure".
    """
    _validate_repo(repo_slug)
    client = get_client()
    repo = client.get_repo(repo_slug)
    run = repo.get_workflow_run(run_id)

    results = []
    for job in run.jobs():
        if job.conclusion == "failure":
            steps = []
            for step in job.steps:
                steps.append({
                    "name": step.name,
                    "conclusion": step.conclusion,
                    "number": step.number,
                })
            results.append({
                "id": job.id,
                "name": job.name,
                "conclusion": job.conclusion,
                "steps": steps,
                "completed_at": (
                    job.completed_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if job.completed_at
                    else ""
                ),
            })

    return results


def download_job_log(repo_slug: str, job_id: int) -> str:
    """Download the log for a specific job.

    Uses the GitHub API to fetch job logs, following the 302 redirect.
    Returns log text.
    """
    _validate_repo(repo_slug)
    token = _get_token()
    url = f"https://api.github.com/repos/{repo_slug}/actions/jobs/{job_id}/logs"
    resp = requests.get(
        url,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# Repository content access (read-only, cached)
# ---------------------------------------------------------------------------

_repo_cache: dict[tuple, Any] = {}


def _cached(key: tuple, fn):
    """Return cached result or call fn(), cache it, and return it."""
    if key in _repo_cache:
        return _repo_cache[key]
    result = fn()
    _repo_cache[key] = result
    return result


def get_file_content(repo_slug: str, path: str, ref: str) -> str:
    """Get file content from a repository at a specific ref.

    Uses the GitHub Contents API with raw media type.
    Returns decoded text. Cached by (repo, path, ref).
    """
    def _fetch():
        _validate_repo(repo_slug)
        token = _get_token()
        url = f"https://api.github.com/repos/{repo_slug}/contents/{path}"
        resp = requests.get(
            url,
            params={"ref": ref},
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.raw+json",
            },
        )
        resp.raise_for_status()
        return resp.text

    return _cached(("file", repo_slug, path, ref), _fetch)


def get_commit_info(repo_slug: str, sha: str) -> dict:
    """Get commit metadata and list of changed files.

    Returns a dict with keys: sha, message, author, date, files.
    Each file has: filename, status, additions, deletions (no patches).
    Cached by (repo, sha).
    """
    def _fetch():
        _validate_repo(repo_slug)
        client = get_client()
        repo = client.get_repo(repo_slug)
        commit = repo.get_commit(sha)

        files = []
        for f in commit.files:
            files.append({
                "filename": f.filename,
                "status": f.status,
                "additions": f.additions,
                "deletions": f.deletions,
            })

        return {
            "sha": commit.sha,
            "message": commit.commit.message,
            "author": commit.commit.author.name,
            "date": commit.commit.author.date.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "files": files,
        }

    return _cached(("commit", repo_slug, sha), _fetch)


def list_directory(repo_slug: str, path: str, ref: str) -> list[dict]:
    """List files and directories at a path in the repository.

    Returns a list of dicts with keys: name, type, path, size.
    type is 'file' or 'dir'. Cached by (repo, path, ref).
    """
    def _fetch():
        _validate_repo(repo_slug)
        token = _get_token()
        url = f"https://api.github.com/repos/{repo_slug}/contents/{path}"
        resp = requests.get(
            url,
            params={"ref": ref},
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict):
            return [{"name": data["name"], "type": data["type"],
                     "path": data["path"], "size": data.get("size", 0)}]

        return [
            {"name": item["name"], "type": item["type"],
             "path": item["path"], "size": item.get("size", 0)}
            for item in data
        ]

    return _cached(("dir", repo_slug, path, ref), _fetch)
