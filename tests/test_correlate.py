"""Tests for flakectl.correlate -- non-agent parts only."""

import json

from conftest import make_progress_content

from flakectl.correlate import _extract_branches, _has_categories

# ---------------------------------------------------------------------------
# _extract_branches
# ---------------------------------------------------------------------------

class TestExtractBranches:
    def test_extracts_unique_branches(self):
        content = make_progress_content([
            {"run_id": "1", "status": "done", "branch": "main",
             "jobs": [{"name": "j1", "category": "test-flake/timeout",
                       "is_flake": "yes"}]},
            {"run_id": "2", "status": "done", "branch": "feat-x",
             "jobs": [{"name": "j2", "category": "test-flake/timeout",
                       "is_flake": "yes"}]},
            {"run_id": "3", "status": "done", "branch": "main",
             "jobs": [{"name": "j3", "category": "bug/crash",
                       "is_flake": "no"}]},
        ])
        result = _extract_branches(content)
        assert result == ["feat-x", "main"]

    def test_empty_content(self):
        assert _extract_branches("# Empty") == []

    def test_no_runs(self):
        content = make_progress_content([])
        assert _extract_branches(content) == []


# ---------------------------------------------------------------------------
# _has_categories
# ---------------------------------------------------------------------------

class TestHasCategories:
    def test_has_categories_section(self):
        content = (
            "<!-- CATEGORIES START -->\n"
            "- `test-flake/timeout` -- Tests timing out\n"
            "<!-- CATEGORIES END -->"
        )
        assert _has_categories(content) is True

    def test_has_filled_category_fields(self):
        content = make_progress_content([
            {"run_id": "1", "status": "done",
             "jobs": [{"name": "j1", "category": "test-flake/timeout",
                       "is_flake": "yes"}]},
        ])
        assert _has_categories(content) is True

    def test_no_categories_at_all(self):
        content = make_progress_content([
            {"run_id": "1", "status": "pending",
             "jobs": [{"name": "j1"}]},
        ])
        assert _has_categories(content) is False

    def test_empty_categories_section(self):
        content = (
            "<!-- CATEGORIES START -->\n"
            "(none yet)\n"
            "<!-- CATEGORIES END -->\n"
            "No runs."
        )
        assert _has_categories(content) is False


# ---------------------------------------------------------------------------
# run() edge cases (no agent, just file handling)
# ---------------------------------------------------------------------------

class TestRunNoCategories:
    def test_writes_empty_fixes_json(self, tmp_path):
        from flakectl.correlate import run

        content = make_progress_content([
            {"run_id": "1", "status": "pending",
             "jobs": [{"name": "j1"}]},
        ])
        progress = tmp_path / "progress.md"
        progress.write_text(content)

        rc = run("org/repo", str(progress), workdir=str(tmp_path))
        assert rc == 0

        fixes_path = tmp_path / "fixes.json"
        assert fixes_path.exists()
        data = json.loads(fixes_path.read_text())
        assert data == {"fixes": []}
