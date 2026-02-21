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
