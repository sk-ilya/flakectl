"""Tests for flakectl.github -- validation and env var handling."""

import pytest

from flakectl.github import _get_token, _validate_repo


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
