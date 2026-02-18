"""Tests for flakectl.classify -- file-based helpers (no SDK)."""

import pytest

from conftest import make_progress_content
from flakectl.classify import (
    _agent_color,
    _build_system_prompt,
    _get_runs_by_status,
    _is_run_classified,
    _is_run_done,
    get_done_runs,
    get_pending_runs,
    mark_runs_as_error,
    merge_run,
    split_progress,
)


# ---------------------------------------------------------------------------
# _agent_color
# ---------------------------------------------------------------------------

class TestAgentColor:
    def test_deterministic(self):
        c1 = _agent_color("12345")
        c2 = _agent_color("12345")
        assert c1 == c2

    def test_returns_ansi_escape(self):
        color = _agent_color("12345")
        assert color.startswith("\033[")


# ---------------------------------------------------------------------------
# _get_runs_by_status / get_pending_runs / get_done_runs
# ---------------------------------------------------------------------------

class TestGetRunsByStatus:
    def test_get_pending_runs(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
            {"run_id": "200", "status": "done", "jobs": [{"name": "j2"}]},
            {"run_id": "300", "status": "pending", "jobs": [{"name": "j3"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)
        result = get_pending_runs(str(p))
        assert sorted(result) == ["100", "300"]

    def test_get_done_runs(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
            {"run_id": "200", "status": "done", "jobs": [{"name": "j2"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)
        result = get_done_runs(str(p))
        assert result == ["200"]

    def test_no_matches_returns_empty(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)
        result = get_done_runs(str(p))
        assert result == []

    def test_prefix_match_behavior(self, tmp_path):
        # re.escape only escapes regex special chars, does not add word
        # boundaries -- so a prefix like "pend" will match "pending".
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)
        result = _get_runs_by_status(str(p), "pend")
        assert result == ["100"]

    def test_no_false_positive_across_statuses(self, tmp_path):
        # "done" should not match "pending"
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)
        result = _get_runs_by_status(str(p), "done")
        assert result == []


# ---------------------------------------------------------------------------
# mark_runs_as_error
# ---------------------------------------------------------------------------

class TestMarkRunsAsError:
    def test_pending_becomes_error(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
            {"run_id": "200", "status": "pending", "jobs": [{"name": "j2"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        mark_runs_as_error(str(p), ["100"])

        text = p.read_text()
        # run 100 should be error
        assert _get_runs_by_status(str(p), "error") == ["100"]
        # run 200 should still be pending
        assert _get_runs_by_status(str(p), "pending") == ["200"]

    def test_done_not_overwritten(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "done", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        mark_runs_as_error(str(p), ["100"])

        # Should still be done since only pending -> error
        assert _get_runs_by_status(str(p), "done") == ["100"]

    def test_multiple_ids(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
            {"run_id": "200", "status": "pending", "jobs": [{"name": "j2"}]},
            {"run_id": "300", "status": "pending", "jobs": [{"name": "j3"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        mark_runs_as_error(str(p), ["100", "300"])

        assert sorted(_get_runs_by_status(str(p), "error")) == ["100", "300"]
        assert _get_runs_by_status(str(p), "pending") == ["200"]


# ---------------------------------------------------------------------------
# split_progress
# ---------------------------------------------------------------------------

class TestSplitProgress:
    def test_creates_files_in_runs_dir(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
            {"run_id": "200", "status": "pending", "jobs": [{"name": "j2"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        result = split_progress(str(p), ["100", "200"])

        assert "100" in result
        assert "200" in result
        assert (tmp_path / "runs" / "run-100.md").exists()
        assert (tmp_path / "runs" / "run-200.md").exists()

    def test_content_matches_block(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        result = split_progress(str(p), ["100"])

        run_content = (tmp_path / "runs" / "run-100.md").read_text()
        assert "<!-- BEGIN RUN 100 -->" in run_content
        assert "<!-- END RUN 100 -->" in run_content

    def test_missing_run_id_skipped(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        result = split_progress(str(p), ["100", "999"])

        assert "100" in result
        assert "999" not in result


# ---------------------------------------------------------------------------
# merge_run
# ---------------------------------------------------------------------------

class TestMergeRun:
    def test_basic_merge(self, tmp_path):
        # Original progress with pending run
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        # Per-run file with done status
        run_content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout",
                    "is_flake": "yes",
                }],
            },
        ])
        # Extract just the run block from the full progress content
        import re
        match = re.search(
            r"(<!-- BEGIN RUN 100 -->.*?<!-- END RUN 100 -->)",
            run_content, re.DOTALL,
        )
        run_file = tmp_path / "run-100.md"
        run_file.write_text(match.group(1) + "\n")

        result = merge_run(str(p), "100", str(run_file))
        assert result is True
        assert _get_runs_by_status(str(p), "done") == ["100"]

    def test_run_not_in_progress_returns_false(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        # Run file for a different run ID
        run_file = tmp_path / "run-999.md"
        run_file.write_text("<!-- BEGIN RUN 999 -->\n- **status**: done\n<!-- END RUN 999 -->\n")

        result = merge_run(str(p), "999", str(run_file))
        assert result is False

    def test_verification_fails_returns_false(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        # Run file says "pending" but expected_status is "done"
        import re
        match = re.search(
            r"(<!-- BEGIN RUN 100 -->.*?<!-- END RUN 100 -->)",
            content, re.DOTALL,
        )
        run_file = tmp_path / "run-100.md"
        run_file.write_text(match.group(1) + "\n")

        result = merge_run(str(p), "100", str(run_file), expected_status="done")
        assert result is False


# ---------------------------------------------------------------------------
# _is_run_done / _is_run_classified
# ---------------------------------------------------------------------------

class TestIsRunDone:
    def test_true_when_done(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "done", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "run-100.md"
        p.write_text(content)
        assert _is_run_done(str(p), "100") is True

    def test_false_when_pending(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "run-100.md"
        p.write_text(content)
        assert _is_run_done(str(p), "100") is False


class TestIsRunClassified:
    def test_true_when_classified(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "classified", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "run-100.md"
        p.write_text(content)
        assert _is_run_classified(str(p), "100") is True

    def test_false_when_not_classified(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "run-100.md"
        p.write_text(content)
        assert _is_run_classified(str(p), "100") is False


# ---------------------------------------------------------------------------
# _build_system_prompt
# ---------------------------------------------------------------------------

class TestBuildSystemPrompt:
    def test_without_context(self):
        prompt = _build_system_prompt()
        assert len(prompt) > 0
        assert "Repository-specific context" not in prompt

    def test_with_context(self):
        prompt = _build_system_prompt("This repo uses Go 1.22")
        assert "Repository-specific context" in prompt
        assert "This repo uses Go 1.22" in prompt
