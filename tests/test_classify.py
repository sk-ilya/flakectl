"""Tests for progress file helpers and classifier utilities."""


from conftest import make_progress_content

from flakectl.agentlog import agent_color
from flakectl.progressfile import (
    get_done_runs,
    get_pending_runs,
    get_runs_by_status,
    is_run_classified,
    is_run_done,
    mark_runs_as_error,
    merge_run,
    rebuild_categories_section,
    split_progress,
)
from flakectl.prompts.classifier import build_system_prompt

# ---------------------------------------------------------------------------
# agent_color
# ---------------------------------------------------------------------------

class TestAgentColor:
    def test_deterministic(self):
        c1 = agent_color("12345")
        c2 = agent_color("12345")
        assert c1 == c2

    def test_returns_ansi_escape(self):
        color = agent_color("12345")
        assert color.startswith("\033[")


# ---------------------------------------------------------------------------
# get_runs_by_status / get_pending_runs / get_done_runs
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
        result = get_runs_by_status(str(p), "pend")
        assert result == ["100"]

    def test_no_false_positive_across_statuses(self, tmp_path):
        # "done" should not match "pending"
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)
        result = get_runs_by_status(str(p), "done")
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

        # run 100 should be error
        assert get_runs_by_status(str(p), "error") == ["100"]
        # run 200 should still be pending
        assert get_runs_by_status(str(p), "pending") == ["200"]

    def test_done_not_overwritten(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "done", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        mark_runs_as_error(str(p), ["100"])

        # Should still be done since only pending -> error
        assert get_runs_by_status(str(p), "done") == ["100"]

    def test_multiple_ids(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
            {"run_id": "200", "status": "pending", "jobs": [{"name": "j2"}]},
            {"run_id": "300", "status": "pending", "jobs": [{"name": "j3"}]},
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        mark_runs_as_error(str(p), ["100", "300"])

        assert sorted(get_runs_by_status(str(p), "error")) == ["100", "300"]
        assert get_runs_by_status(str(p), "pending") == ["200"]


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

        split_progress(str(p), ["100"])

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
        assert get_runs_by_status(str(p), "done") == ["100"]

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
# is_run_done / is_run_classified
# ---------------------------------------------------------------------------

class TestIsRunDone:
    def test_true_when_done(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "done", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "run-100.md"
        p.write_text(content)
        assert is_run_done(str(p), "100") is True

    def test_false_when_pending(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "run-100.md"
        p.write_text(content)
        assert is_run_done(str(p), "100") is False


class TestIsRunClassified:
    def test_true_when_classified(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "classified", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "run-100.md"
        p.write_text(content)
        assert is_run_classified(str(p), "100") is True

    def test_false_when_not_classified(self, tmp_path):
        content = make_progress_content([
            {"run_id": "100", "status": "pending", "jobs": [{"name": "j1"}]},
        ])
        p = tmp_path / "run-100.md"
        p.write_text(content)
        assert is_run_classified(str(p), "100") is False


# ---------------------------------------------------------------------------
# build_system_prompt
# ---------------------------------------------------------------------------

class TestBuildSystemPrompt:
    def test_without_context(self):
        prompt = build_system_prompt()
        assert len(prompt) > 0
        assert "Repository-specific context" not in prompt

    def test_with_context(self):
        prompt = build_system_prompt("This repo uses Go 1.22")
        assert "Repository-specific context" in prompt
        assert "This repo uses Go 1.22" in prompt


# ---------------------------------------------------------------------------
# rebuild_categories_section
# ---------------------------------------------------------------------------

class TestRebuildCategoriesSection:
    def _read_cats(self, path):
        """Read the categories section and return as dict."""
        import re
        content = path.read_text()
        m = re.search(
            r"<!-- CATEGORIES START -->(.*?)<!-- CATEGORIES END -->",
            content, re.DOTALL,
        )
        if not m:
            return {}
        cats = {}
        for line in m.group(1).strip().split("\n"):
            lm = re.match(r"- `([^`]+)`(?:\s*--\s*(.*))?", line.strip())
            if lm:
                cats[lm.group(1)] = (lm.group(2) or "").strip()
        return cats

    def test_single_done_run(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout/TestA",
                    "is_flake": "yes",
                    "summary": "Timed out waiting for response",
                }],
            },
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        rebuild_categories_section(str(p))

        cats = self._read_cats(p)
        assert "test-flake/timeout" in cats
        assert "Timed out" in cats["test-flake/timeout"]

    def test_multiple_categories(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout/TestA",
                    "summary": "Timed out",
                }],
            },
            {
                "run_id": "200",
                "status": "done",
                "jobs": [{
                    "name": "j2",
                    "category": "infra-flake/registry-502",
                    "summary": "Registry down",
                }],
            },
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        rebuild_categories_section(str(p))

        cats = self._read_cats(p)
        assert len(cats) == 2
        assert "test-flake/timeout" in cats
        assert "infra-flake/registry-502" in cats

    def test_pending_runs_excluded(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout",
                    "summary": "Done category",
                }],
            },
            {
                "run_id": "200",
                "status": "pending",
                "jobs": [{
                    "name": "j2",
                    "category": "bug/crash",
                    "summary": "Should not appear",
                }],
            },
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        rebuild_categories_section(str(p))

        cats = self._read_cats(p)
        assert "test-flake/timeout" in cats
        assert "bug/crash" not in cats

    def test_classified_runs_included(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "classified",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/race",
                    "summary": "Race condition",
                }],
            },
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        rebuild_categories_section(str(p))

        cats = self._read_cats(p)
        assert "test-flake/race" in cats

    def test_deduplicates_same_category(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout/TestA",
                    "summary": "First summary",
                }],
            },
            {
                "run_id": "200",
                "status": "done",
                "jobs": [{
                    "name": "j2",
                    "category": "test-flake/timeout/TestB",
                    "summary": "Second summary",
                }],
            },
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        rebuild_categories_section(str(p))

        cats = self._read_cats(p)
        assert len(cats) == 1
        assert "test-flake/timeout" in cats
        # Uses first summary seen
        assert "First summary" in cats["test-flake/timeout"]

    def test_multi_job_run(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "jobs": [
                    {
                        "name": "j1",
                        "category": "test-flake/timeout/TestA",
                        "summary": "Timeout in TestA",
                    },
                    {
                        "name": "j2",
                        "category": "infra-flake/network",
                        "summary": "Network error",
                    },
                ],
            },
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        rebuild_categories_section(str(p))

        cats = self._read_cats(p)
        assert len(cats) == 2
        assert "test-flake/timeout" in cats
        assert "infra-flake/network" in cats

    def test_no_done_runs_writes_none_yet(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "pending",
                "jobs": [{"name": "j1"}],
            },
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        rebuild_categories_section(str(p))

        text = p.read_text()
        assert "(none yet)" in text

    def test_replaces_stale_section(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout",
                    "summary": "Correct category",
                }],
            },
        ])
        # Inject a wrong category into the section
        content = content.replace(
            "(none yet)",
            "- `bug/wrong-category` -- This is stale",
        )
        p = tmp_path / "progress.md"
        p.write_text(content)

        rebuild_categories_section(str(p))

        cats = self._read_cats(p)
        assert "bug/wrong-category" not in cats
        assert "test-flake/timeout" in cats

    def test_long_summary_truncated(self, tmp_path):
        long_summary = "A" * 200
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout",
                    "summary": long_summary,
                }],
            },
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        rebuild_categories_section(str(p))

        cats = self._read_cats(p)
        desc = cats["test-flake/timeout"]
        assert len(desc) <= 125  # 117 + "..."
        assert desc.endswith("...")

    def test_empty_category_field_skipped(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "jobs": [{"name": "j1", "category": ""}],
            },
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        rebuild_categories_section(str(p))

        text = p.read_text()
        assert "(none yet)" in text

    def test_categories_sorted_alphabetically(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "jobs": [
                    {"name": "j1", "category": "test-flake/zz-last", "summary": "Z"},
                    {"name": "j2", "category": "bug/aa-first", "summary": "A"},
                    {"name": "j3", "category": "infra-flake/mm-middle", "summary": "M"},
                ],
            },
        ])
        p = tmp_path / "progress.md"
        p.write_text(content)

        rebuild_categories_section(str(p))

        text = p.read_text()
        pos_bug = text.index("bug/aa-first")
        pos_infra = text.index("infra-flake/mm-middle")
        pos_test = text.index("test-flake/zz-last")
        assert pos_bug < pos_infra < pos_test
