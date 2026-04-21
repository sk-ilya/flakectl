"""Tests for flakectl.github -- validation, env var handling, and cloning."""

import os
import subprocess
from unittest.mock import patch

import pytest

from flakectl.github import (
    _get_token,
    _validate_repo,
    clone_at_ref,
    ensure_repo_clones,
    get_runs_by_ids,
    list_failed_runs_multi,
)

# ---------------------------------------------------------------------------
# _validate_repo
# ---------------------------------------------------------------------------

class TestValidateRepo:
    def test_valid_owner_name(self):
        _validate_repo("owner/name")  # should not raise

    def test_empty_string(self):
        with pytest.raises(ValueError, match="Invalid repo format"):
            _validate_repo("")

    def test_no_slash(self):
        with pytest.raises(ValueError, match="Invalid repo format"):
            _validate_repo("no-slash")

    def test_too_many_slashes(self):
        with pytest.raises(ValueError, match="Invalid repo format"):
            _validate_repo("a/b/c")


# ---------------------------------------------------------------------------
# _get_token
# ---------------------------------------------------------------------------

class TestGetToken:
    def test_github_token_set(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
        monkeypatch.delenv("GH_TOKEN", raising=False)
        assert _get_token() == "ghp_test123"

    def test_gh_token_fallback(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", "ghp_fallback")
        assert _get_token() == "ghp_fallback"

    def test_neither_set_raises(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="GitHub token not found"):
            _get_token()


# ---------------------------------------------------------------------------
# clone_at_ref
# ---------------------------------------------------------------------------

class TestCloneAtRef:
    def test_skips_if_git_dir_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        dest = tmp_path / "repo"
        dest.mkdir()
        (dest / ".git").mkdir()
        result = clone_at_ref("owner/name", str(dest), "abc123")
        assert result == str(dest.resolve())

    def test_runs_git_commands(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        dest = str(tmp_path / "repo")
        calls = []

        original_run = subprocess.run

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return original_run(
                ["true"], capture_output=True, check=True,
            )

        with patch("flakectl.github.subprocess.run", side_effect=mock_run):
            clone_at_ref("owner/name", dest, "abc123def456")

        assert len(calls) == 4
        assert calls[0] == ["git", "init"]
        assert calls[1][0:3] == ["git", "remote", "add"]
        assert "origin" in calls[1]
        assert calls[2] == [
            "git", "fetch", "--depth", "1", "origin", "abc123def456",
        ]
        assert calls[3] == ["git", "checkout", "FETCH_HEAD"]

    def test_invalid_repo_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        with pytest.raises(ValueError, match="Invalid repo format"):
            clone_at_ref("bad", str(tmp_path / "repo"), "abc123")


# ---------------------------------------------------------------------------
# ensure_repo_clones
# ---------------------------------------------------------------------------

class TestEnsureRepoClones:
    def test_creates_directories(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        base = str(tmp_path / "repos")

        refs = ["aaa11111full", "bbb22222full"]

        with patch("flakectl.github.clone_at_ref") as mock_clone:
            mock_clone.side_effect = lambda repo, dest, ref: os.path.abspath(dest)
            result = ensure_repo_clones("owner/name", base, refs)

        assert len(result) == 2
        assert result["aaa11111full"].endswith("aaa11111")
        assert result["bbb22222full"].endswith("bbb22222")
        assert mock_clone.call_count == 2

    def test_deduplicates_same_prefix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        base = str(tmp_path / "repos")

        # Same 8-char prefix, different full SHAs
        refs = ["aaa11111xxx", "aaa11111yyy"]

        with patch("flakectl.github.clone_at_ref") as mock_clone:
            mock_clone.side_effect = lambda repo, dest, ref: os.path.abspath(dest)
            result = ensure_repo_clones("owner/name", base, refs)

        # Should only clone once for the same prefix
        assert mock_clone.call_count == 1
        assert len(result) == 2

    def test_handles_clone_failure(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        base = str(tmp_path / "repos")

        with patch("flakectl.github.clone_at_ref") as mock_clone:
            mock_clone.side_effect = subprocess.CalledProcessError(
                1, ["git"], stderr=b"fatal: error"
            )
            result = ensure_repo_clones("owner/name", base, ["abc12345full"])

        assert len(result) == 0


# ---------------------------------------------------------------------------
# get_runs_by_ids
# ---------------------------------------------------------------------------

class TestGetRunsByIds:
    def _make_mock_run(self, run_id, branch="main", event="push",
                       conclusion="failure", status="completed"):
        from datetime import UTC, datetime
        from unittest.mock import MagicMock

        run = MagicMock()
        run.id = run_id
        run.html_url = f"https://github.com/owner/name/actions/runs/{run_id}"
        run.name = "CI"
        run.head_branch = branch
        run.event = event
        run.head_sha = f"sha{run_id}"
        run.created_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC)
        run.run_attempt = 1
        run.conclusion = conclusion
        run.status = status
        return run

    @patch("flakectl.github.get_client")
    def test_fetches_single_failed_run(self, mock_get_client):
        mock_repo = mock_get_client.return_value.get_repo.return_value
        mock_repo.get_workflow_run.return_value = self._make_mock_run(100)

        result = get_runs_by_ids("owner/name", [100])

        assert len(result) == 1
        assert result[0]["id"] == 100
        assert result[0]["url"] == "https://github.com/owner/name/actions/runs/100"
        assert result[0]["head_branch"] == "main"
        assert result[0]["head_sha"] == "sha100"
        assert result[0]["created_at"] == "2025-01-15T10:00:00Z"
        assert result[0]["run_attempt"] == 1

    @patch("flakectl.github.get_client")
    def test_skips_successful_runs(self, mock_get_client):
        mock_repo = mock_get_client.return_value.get_repo.return_value
        mock_repo.get_workflow_run.side_effect = [
            self._make_mock_run(100, conclusion="success"),
            self._make_mock_run(200, conclusion="failure"),
            self._make_mock_run(300, conclusion="success"),
        ]

        result = get_runs_by_ids("owner/name", [100, 200, 300])

        assert len(result) == 1
        assert result[0]["id"] == 200

    @patch("flakectl.github.get_client")
    def test_includes_cancelled_runs(self, mock_get_client):
        mock_repo = mock_get_client.return_value.get_repo.return_value
        mock_repo.get_workflow_run.side_effect = [
            self._make_mock_run(100, conclusion="failure"),
            self._make_mock_run(200, conclusion="cancelled"),
            self._make_mock_run(300, conclusion="success"),
        ]

        result = get_runs_by_ids("owner/name", [100, 200, 300])

        assert len(result) == 2
        assert result[0]["id"] == 100
        assert result[1]["id"] == 200

    @patch("flakectl.github.get_client")
    def test_fetches_multiple_failed_runs(self, mock_get_client):
        mock_repo = mock_get_client.return_value.get_repo.return_value
        mock_repo.get_workflow_run.side_effect = [
            self._make_mock_run(100),
            self._make_mock_run(200, branch="develop"),
        ]

        result = get_runs_by_ids("owner/name", [100, 200])

        assert len(result) == 2
        assert result[0]["id"] == 100
        assert result[1]["id"] == 200
        assert result[1]["head_branch"] == "develop"

    def test_invalid_repo_raises(self):
        with pytest.raises(ValueError, match="Invalid repo format"):
            get_runs_by_ids("bad", [100])


# ---------------------------------------------------------------------------
# list_failed_runs_multi
# ---------------------------------------------------------------------------

def _make_run_dict(run_id, branch="main", event="push",
                   created_at="2025-01-15T10:00:00Z"):
    return {
        "id": run_id,
        "url": f"https://github.com/owner/name/actions/runs/{run_id}",
        "name": "CI",
        "head_branch": branch,
        "event": event,
        "head_sha": f"sha{run_id}",
        "created_at": created_at,
        "run_attempt": 1,
    }


class TestListFailedRunsMulti:

    @patch("flakectl.github._check_rate_limit")
    @patch("flakectl.github.get_client")
    @patch("flakectl.github.list_failed_runs")
    def test_fetches_both_failure_and_cancelled(
        self, mock_list, mock_client, mock_rate,
    ):
        def side_effect(repo, limit, wf=None, branch=None, **kwargs):
            status = kwargs.get("status", "failure")
            if status == "failure":
                return [_make_run_dict(100)]
            if status == "cancelled":
                return [_make_run_dict(200)]
            return []

        mock_list.side_effect = side_effect

        result = list_failed_runs_multi("owner/name", 200, None, None)

        assert len(result) == 2
        ids = {r["id"] for r in result}
        assert ids == {100, 200}

    @patch("flakectl.github._check_rate_limit")
    @patch("flakectl.github.get_client")
    @patch("flakectl.github.list_failed_runs")
    def test_deduplicates_by_run_id(
        self, mock_list, mock_client, mock_rate,
    ):
        def side_effect(repo, limit, wf=None, branch=None, **kwargs):
            status = kwargs.get("status", "failure")
            if status == "failure":
                return [_make_run_dict(100), _make_run_dict(200)]
            if status == "cancelled":
                return [_make_run_dict(200), _make_run_dict(300)]
            return []

        mock_list.side_effect = side_effect

        result = list_failed_runs_multi("owner/name", 200, None, None)

        ids = [r["id"] for r in result]
        assert sorted(ids) == [100, 200, 300]

    @patch("flakectl.github._check_rate_limit")
    @patch("flakectl.github.get_client")
    @patch("flakectl.github.list_failed_runs")
    def test_merge_queue_runs_included_for_matching_branch(
        self, mock_list, mock_client, mock_rate,
    ):
        def side_effect(repo, limit, wf=None, branch=None, **kwargs):
            event = kwargs.get("event")
            if event == "merge_group":
                return [_make_run_dict(
                    300,
                    branch="gh-readonly-queue/main/pr-123-abc",
                    event="merge_group",
                )]
            if branch == "main":
                return [_make_run_dict(100)]
            return []

        mock_list.side_effect = side_effect

        result = list_failed_runs_multi("owner/name", 200, None, ["main"])

        ids = {r["id"] for r in result}
        assert 300 in ids

    @patch("flakectl.github._check_rate_limit")
    @patch("flakectl.github.get_client")
    @patch("flakectl.github.list_failed_runs")
    def test_merge_queue_runs_excluded_for_unrelated_branch(
        self, mock_list, mock_client, mock_rate,
    ):
        def side_effect(repo, limit, wf=None, branch=None, **kwargs):
            event = kwargs.get("event")
            if event == "merge_group":
                return [_make_run_dict(
                    300,
                    branch="gh-readonly-queue/develop/pr-456-def",
                    event="merge_group",
                )]
            if branch == "main":
                return [_make_run_dict(100)]
            return []

        mock_list.side_effect = side_effect

        result = list_failed_runs_multi("owner/name", 200, None, ["main"])

        ids = {r["id"] for r in result}
        assert 300 not in ids
        assert 100 in ids

    @patch("flakectl.github._check_rate_limit")
    @patch("flakectl.github.get_client")
    @patch("flakectl.github.list_failed_runs")
    def test_merge_queue_fetch_error_is_nonfatal(
        self, mock_list, mock_client, mock_rate, caplog,
    ):
        def side_effect(repo, limit, wf=None, branch=None, **kwargs):
            event = kwargs.get("event")
            if event == "merge_group":
                raise RuntimeError("API error")
            if branch == "main":
                return [_make_run_dict(100)]
            return []

        mock_list.side_effect = side_effect

        result = list_failed_runs_multi("owner/name", 200, None, ["main"])

        ids = {r["id"] for r in result}
        assert 100 in ids
        assert "Failed to fetch merge_group runs" in caplog.text
