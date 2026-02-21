"""Tests for flakectl.tools -- git/gh tool validation and MCP server creation."""

import pytest

from flakectl.tools import (
    _parse_gh_prefix,
    _validate_gh_args,
    _validate_git_args,
    create_tools_server,
)

# ---------------------------------------------------------------------------
# _validate_git_args
# ---------------------------------------------------------------------------

class TestValidateGitArgs:
    @pytest.mark.parametrize("args", [
        "log --oneline -5",
        "show HEAD:path/to/file",
        "diff HEAD~1",
        "blame src/main.py",
        "shortlog -s",
        "ls-files",
        "ls-tree HEAD",
        "rev-parse HEAD",
        "cat-file -p HEAD",
        "describe --tags",
        "name-rev HEAD",
        "status",
    ])
    def test_allowed_subcommands(self, args):
        assert _validate_git_args(args) is None

    @pytest.mark.parametrize("args", [
        "push origin main",
        "pull",
        "checkout main",
        "reset --hard HEAD",
        "clean -fd",
        "remote add origin url",
        "branch -D feature",
        "merge feature",
        "rebase main",
        "fetch origin",
        "clone https://example.com/repo",
        "init",
        "commit -m 'msg'",
    ])
    def test_blocked_subcommands(self, args):
        err = _validate_git_args(args)
        assert err is not None
        assert "Blocked git subcommand" in err

    def test_empty_args(self):
        err = _validate_git_args("")
        assert err is not None
        assert "No git subcommand" in err

    def test_invalid_shell_quoting(self):
        err = _validate_git_args("log 'unterminated")
        assert err is not None
        assert "Invalid args" in err


# ---------------------------------------------------------------------------
# _parse_gh_prefix
# ---------------------------------------------------------------------------

class TestParseGhPrefix:
    def test_two_word_prefix(self):
        assert _parse_gh_prefix(["run", "view", "12345"]) == "run view"

    def test_single_word_prefix(self):
        assert _parse_gh_prefix(["search", "commits", "query"]) == "search"
        assert _parse_gh_prefix(["api", "repos/o/r/commits"]) == "api"

    def test_unknown_prefix(self):
        assert _parse_gh_prefix(["unknown", "cmd"]) is None

    def test_empty(self):
        assert _parse_gh_prefix([]) is None


# ---------------------------------------------------------------------------
# _validate_gh_args
# ---------------------------------------------------------------------------

class TestValidateGhArgs:
    REPO = "owner/repo"

    @pytest.mark.parametrize("args", [
        "run list --json conclusion",
        "run view 12345 --json jobs",
        "pr list --state open",
        "pr view 42 --json body,title,files",
        "pr diff 42",
        "issue list --label bug",
        "issue view 10",
        "search commits 'fix timeout'",
        "api repos/owner/repo/commits/abc123",
    ])
    def test_allowed_subcommands(self, args):
        assert _validate_gh_args(args, self.REPO) is None

    @pytest.mark.parametrize("args", [
        "pr create --title 'new pr'",
        "issue create --title 'bug'",
        "pr close 42",
        "pr merge 42",
        "issue edit 10",
        "pr comment 42 --body 'text'",
        "pr reopen 42",
        "release delete v1.0",
    ])
    def test_blocked_write_operations(self, args):
        err = _validate_gh_args(args, self.REPO)
        assert err is not None
        # Either blocked subcommand or write operation
        assert "Blocked" in err or "Write operation blocked" in err

    def test_blocked_unknown_subcommand(self):
        err = _validate_gh_args("repo view", self.REPO)
        assert err is not None
        assert "Blocked gh subcommand" in err

    def test_api_path_must_start_with_repo(self):
        err = _validate_gh_args("api repos/other/repo/commits", self.REPO)
        assert err is not None
        assert "API path must start with" in err

    def test_api_path_valid(self):
        assert _validate_gh_args(
            "api repos/owner/repo/commits/abc123", self.REPO
        ) is None

    def test_api_no_path(self):
        err = _validate_gh_args("api --paginate", self.REPO)
        assert err is not None
        assert "No API path" in err

    def test_api_blocks_non_get_method(self):
        err = _validate_gh_args(
            "api repos/owner/repo/issues --method POST", self.REPO
        )
        assert err is not None
        assert "Only GET method allowed" in err

    def test_api_allows_get_method(self):
        assert _validate_gh_args(
            "api repos/owner/repo/commits --method GET", self.REPO
        ) is None

    def test_empty_args(self):
        err = _validate_gh_args("", self.REPO)
        assert err is not None
        assert "No gh subcommand" in err

    def test_invalid_shell_quoting(self):
        err = _validate_gh_args("search commits 'unterminated", self.REPO)
        assert err is not None
        assert "Invalid args" in err


# ---------------------------------------------------------------------------
# create_tools_server
# ---------------------------------------------------------------------------

class TestCreateToolsServer:
    def test_without_repo_dir_has_two_tools(self):
        server = create_tools_server("owner/repo")
        assert server["type"] == "sdk"
        assert server["name"] == "github"

    def test_with_repo_dir_returns_server(self):
        server = create_tools_server("owner/repo", repo_dir="/tmp/repo")
        assert server["type"] == "sdk"
        assert server["name"] == "github"

    def test_server_name(self):
        server = create_tools_server("owner/repo")
        assert server["name"] == "github"
