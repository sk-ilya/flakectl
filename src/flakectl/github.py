"""Centralized GitHub client using PyGithub.

All GitHub API calls go through this module.
"""

import functools
import logging
import os
import subprocess

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
# Local repository cloning
# ---------------------------------------------------------------------------

def clone_at_ref(repo_slug: str, dest: str, ref: str) -> str:
    """Create a shallow clone of a repository at a specific commit.

    Uses git init + fetch --depth 1 + checkout to create a minimal clone
    containing only the tree at the given ref. Idempotent: skips if the
    destination already contains a .git directory.

    Returns the absolute path to the cloned directory.
    """
    _validate_repo(repo_slug)
    git_dir = os.path.join(dest, ".git")
    if os.path.exists(git_dir):
        return os.path.abspath(dest)

    token = _get_token()
    url = f"https://x-access-token:{token}@github.com/{repo_slug}.git"
    os.makedirs(dest, exist_ok=True)

    subprocess.run(
        ["git", "init"],
        cwd=dest, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", url],
        cwd=dest, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "fetch", "--depth", "1", "origin", ref],
        cwd=dest, capture_output=True, check=True, timeout=300,
    )
    subprocess.run(
        ["git", "checkout", "FETCH_HEAD"],
        cwd=dest, capture_output=True, check=True,
    )
    return os.path.abspath(dest)


def ensure_repo_clones(
    repo_slug: str, base_dir: str, refs: list[str],
) -> dict[str, str]:
    """Ensure shallow clones exist for each ref.

    Creates ``{base_dir}/{ref[:8]}/`` directories, one per unique ref
    prefix. Returns a mapping from full ref to the local clone path.
    """
    os.makedirs(base_dir, exist_ok=True)
    result: dict[str, str] = {}
    seen_prefixes: set[str] = set()
    for ref in refs:
        prefix = ref[:8]
        if prefix in seen_prefixes:
            # Two different full SHAs with the same 8-char prefix -- rare
            # but possible.  Skip the duplicate to avoid clobbering.
            result[ref] = os.path.abspath(os.path.join(base_dir, prefix))
            continue
        seen_prefixes.add(prefix)
        dest = os.path.join(base_dir, prefix)
        logger.info("Cloning %s at %s into %s...", repo_slug, prefix, dest)
        try:
            path = clone_at_ref(repo_slug, dest, ref)
            result[ref] = path
            logger.info("Clone ready at %s", path)
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Failed to clone %s at %s: %s", repo_slug, prefix,
                exc.stderr.decode() if exc.stderr else exc,
            )
    return result
