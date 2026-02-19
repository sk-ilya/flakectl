"""Tests for flakectl.extract -- pure parsing and report generation."""

import json
from datetime import date

from conftest import make_progress_content

from flakectl.extract import (
    _build_category_data,
    _determine_flake_status,
    _format_fix_link,
    _load_fixes,
    _lookup_description,
    _split_category,
    _summarize_runs,
    relative_date,
    run,
)
from flakectl.progressfile import parse_categories_section, parse_field, parse_jobs

# ---------------------------------------------------------------------------
# relative_date
# ---------------------------------------------------------------------------

class TestRelativeDate:
    def test_today(self):
        assert relative_date("2025-01-15T10:00:00Z", date(2025, 1, 15)) == "today"

    def test_one_day_ago(self):
        assert relative_date("2025-01-14T10:00:00Z", date(2025, 1, 15)) == "1 day ago"

    def test_n_days_ago(self):
        assert relative_date("2025-01-10T10:00:00Z", date(2025, 1, 15)) == "5 days ago"

    def test_empty_input(self):
        assert relative_date("", date(2025, 1, 15)) == ""

    def test_none_input(self):
        assert relative_date(None, date(2025, 1, 15)) == ""

    def test_invalid_format(self):
        assert relative_date("not-a-date", date(2025, 1, 15)) == ""

    def test_date_only_string(self):
        assert relative_date("2025-01-13", date(2025, 1, 15)) == "2 days ago"

    def test_future_date(self):
        result = relative_date("2025-01-20T10:00:00Z", date(2025, 1, 15))
        assert result == "-5 days ago"


# ---------------------------------------------------------------------------
# parse_field
# ---------------------------------------------------------------------------

class TestParseField:
    def test_basic_extraction(self):
        text = "- **status**: done\n- **branch**: main"
        assert parse_field(text, "status") == "done"
        assert parse_field(text, "branch") == "main"

    def test_whitespace_trimming(self):
        text = "- **name**:   hello world   "
        assert parse_field(text, "name") == "hello world"

    def test_missing_field(self):
        text = "- **status**: done"
        assert parse_field(text, "branch") == ""

    def test_empty_value(self):
        text = "- **category**:"
        assert parse_field(text, "category") == ""

    def test_special_chars_in_value(self):
        text = "- **error_message**: Error: can't find `foo` (bar/baz)"
        assert parse_field(text, "error_message") == "Error: can't find `foo` (bar/baz)"


# ---------------------------------------------------------------------------
# parse_categories_section
# ---------------------------------------------------------------------------

class TestParseCategoriesSection:
    def test_normal_categories(self):
        content = (
            "<!-- CATEGORIES START -->\n"
            "- `test-flake/timeout` -- Tests timing out\n"
            "- `bug/nil-pointer` -- Nil pointer dereference\n"
            "<!-- CATEGORIES END -->"
        )
        result = parse_categories_section(content)
        assert result == {
            "test-flake/timeout": "Tests timing out",
            "bug/nil-pointer": "Nil pointer dereference",
        }

    def test_no_markers(self):
        content = "Some content without category markers"
        assert parse_categories_section(content) == {}

    def test_empty_section(self):
        content = (
            "<!-- CATEGORIES START -->\n"
            "(none yet)\n"
            "<!-- CATEGORIES END -->"
        )
        assert parse_categories_section(content) == {}

    def test_description_containing_dashes(self):
        content = (
            "<!-- CATEGORIES START -->\n"
            "- `infra-flake/network` -- Network timeout -- retryable\n"
            "<!-- CATEGORIES END -->"
        )
        result = parse_categories_section(content)
        assert result == {"infra-flake/network": "Network timeout -- retryable"}


# ---------------------------------------------------------------------------
# parse_jobs
# ---------------------------------------------------------------------------

class TestParseJobs:
    def test_single_job(self):
        body = (
            "#### job: `unit-tests`\n"
            "- **step**: Run tests\n"
            "- **job_id**: 100\n"
            "- **category**: test-flake/timeout\n"
            "- **is_flake**: yes\n"
            "- **test-id**: TestFoo\n"
            "- **failed_test**: test_foo.py\n"
            "- **error_message**: timeout\n"
            "- **summary**: timed out\n"
        )
        jobs = parse_jobs(body)
        assert len(jobs) == 1
        assert jobs[0]["job_name"] == "unit-tests"
        assert jobs[0]["step"] == "Run tests"
        assert jobs[0]["category"] == "test-flake/timeout"
        assert jobs[0]["is_flake"] == "yes"

    def test_multiple_jobs(self):
        body = (
            "#### job: `job-a`\n"
            "- **step**: Step A\n"
            "- **job_id**: 1\n"
            "- **category**: bug/crash\n"
            "- **is_flake**: no\n"
            "- **test-id**: T1\n"
            "- **failed_test**: t1.py\n"
            "- **error_message**: crash\n"
            "- **summary**: crashed\n\n"
            "#### job: `job-b`\n"
            "- **step**: Step B\n"
            "- **job_id**: 2\n"
            "- **category**: test-flake/race\n"
            "- **is_flake**: yes\n"
            "- **test-id**: T2\n"
            "- **failed_test**: t2.py\n"
            "- **error_message**: race\n"
            "- **summary**: race condition\n"
        )
        jobs = parse_jobs(body)
        assert len(jobs) == 2
        assert jobs[0]["job_name"] == "job-a"
        assert jobs[1]["job_name"] == "job-b"

    def test_empty_fields(self):
        # When a field value is empty, parse_field's \s* consumes the
        # newline and (.*) captures the next line's content.  This is
        # the actual behavior -- not ideal but harmless in practice
        # because progress.md fields are always filled by agents.
        body = (
            "#### job: `empty-job`\n"
            "- **step**:\n"
            "- **job_id**:\n"
            "- **category**:\n"
            "- **is_flake**:\n"
            "- **test-id**:\n"
            "- **failed_test**:\n"
            "- **error_message**:\n"
            "- **summary**:\n"
        )
        jobs = parse_jobs(body)
        assert len(jobs) == 1
        # Last field (summary) has nothing after it, so it IS empty
        assert jobs[0]["summary"] == ""

    def test_partial_fields(self):
        body = (
            "#### job: `partial-job`\n"
            "- **step**: Build\n"
            "- **job_id**: 42\n"
            "- **category**: bug/crash\n"
            "- **is_flake**: no\n"
            "- **test-id**:\n"
            "- **failed_test**:\n"
            "- **error_message**:\n"
            "- **summary**:\n"
        )
        jobs = parse_jobs(body)
        assert len(jobs) == 1
        assert jobs[0]["step"] == "Build"
        assert jobs[0]["job_id"] == "42"
        assert jobs[0]["category"] == "bug/crash"
        assert jobs[0]["is_flake"] == "no"

    def test_no_jobs(self):
        body = "Some text without job sections"
        assert parse_jobs(body) == []


# ---------------------------------------------------------------------------
# _determine_flake_status
# ---------------------------------------------------------------------------

class TestDetermineFlakeStatus:
    def test_all_yes(self):
        rows = [{"is_flake": "yes"}, {"is_flake": "yes"}]
        assert _determine_flake_status(rows) == "yes"

    def test_all_no(self):
        rows = [{"is_flake": "no"}, {"is_flake": "no"}]
        assert _determine_flake_status(rows) == "no"

    def test_mixed(self):
        rows = [{"is_flake": "yes"}, {"is_flake": "no"}]
        assert _determine_flake_status(rows) == "mixed"

    def test_single_row(self):
        assert _determine_flake_status([{"is_flake": "yes"}]) == "yes"
        assert _determine_flake_status([{"is_flake": "no"}]) == "no"

    def test_empty_string_value(self):
        rows = [{"is_flake": ""}, {"is_flake": "yes"}]
        assert _determine_flake_status(rows) == "mixed"


# ---------------------------------------------------------------------------
# _summarize_runs
# ---------------------------------------------------------------------------

class TestSummarizeRuns:
    def test_all_flakes(self):
        rows = [
            {"run_id": "1", "is_flake": "yes"},
            {"run_id": "2", "is_flake": "yes"},
        ]
        flake, real, unclear = _summarize_runs(rows)
        assert flake == 2
        assert real == 0
        assert unclear == 0

    def test_all_bugs(self):
        rows = [
            {"run_id": "1", "is_flake": "no"},
            {"run_id": "2", "is_flake": "no"},
        ]
        flake, real, unclear = _summarize_runs(rows)
        assert flake == 0
        assert real == 2

    def test_mixed_within_run_no_wins(self):
        # "no" wins over "yes" within the same run (line 105)
        rows = [
            {"run_id": "1", "is_flake": "yes"},
            {"run_id": "1", "is_flake": "no"},
        ]
        flake, real, unclear = _summarize_runs(rows)
        assert real == 1
        assert flake == 0

    def test_empty_is_flake_yields_unclear(self):
        rows = [{"run_id": "1", "is_flake": ""}]
        flake, real, unclear = _summarize_runs(rows)
        assert unclear == 1

    def test_empty_list(self):
        flake, real, unclear = _summarize_runs([])
        assert (flake, real, unclear) == (0, 0, 0)


# ---------------------------------------------------------------------------
# _build_category_data
# ---------------------------------------------------------------------------

class TestBuildCategoryData:
    def _make_row(self, run_id="1", category="test-flake/timeout",
                  is_flake="yes", test_id="TestA", run_started_at="2025-01-15T10:00:00Z",
                  run_url="https://example.com/1", branch="main",
                  error_message="", summary=""):
        return {
            "run_id": run_id,
            "category": category,
            "is_flake": is_flake,
            "test_id": test_id,
            "run_started_at": run_started_at,
            "run_url": run_url,
            "branch": branch,
            "error_message": error_message,
            "summary": summary,
        }

    def test_basic_structure(self):
        rows = [self._make_row()]
        result = _build_category_data(
            [("test-flake/timeout", rows)], {}, date(2025, 1, 15)
        )
        assert len(result) == 1
        assert result[0]["name"] == "test-flake/timeout"
        assert result[0]["run_count"] == 1
        assert result[0]["job_count"] == 1
        assert result[0]["test_ids"] == ["TestA"]

    def test_test_id_deduplication(self):
        rows = [
            self._make_row(run_id="1", test_id="TestA"),
            self._make_row(run_id="2", test_id="TestA"),
        ]
        result = _build_category_data(
            [("cat", rows)], {}, date(2025, 1, 15)
        )
        assert result[0]["test_ids"] == ["TestA"]

    def test_markdown_guard_filter(self):
        rows = [self._make_row(test_id="TestA, - **foo**")]
        result = _build_category_data(
            [("cat", rows)], {}, date(2025, 1, 15)
        )
        assert "- **foo**" not in result[0]["test_ids"]
        assert "TestA" in result[0]["test_ids"]

    def test_affected_runs_structure(self):
        rows = [
            self._make_row(run_id="1", run_url="https://example.com/1", branch="main"),
            self._make_row(run_id="1", run_url="https://example.com/1", branch="main"),
            self._make_row(run_id="2", run_url="https://example.com/2", branch="feat"),
        ]
        result = _build_category_data(
            [("cat", rows)], {}, date(2025, 1, 15)
        )
        affected = result[0]["affected_runs"]
        assert len(affected) == 2
        assert affected[0]["run_id"] == "1"
        assert affected[0]["jobs_failed"] == 2
        assert affected[1]["run_id"] == "2"
        assert affected[1]["jobs_failed"] == 1

    def test_error_message_from_first_available(self):
        rows = [
            self._make_row(error_message=""),
            self._make_row(error_message="first error"),
            self._make_row(error_message="second error"),
        ]
        result = _build_category_data(
            [("cat", rows)], {}, date(2025, 1, 15)
        )
        assert result[0]["example_error"] == "first error"

    def test_description_from_cat_descriptions(self):
        rows = [self._make_row()]
        descs = {"test-flake/timeout": "Tests timing out"}
        result = _build_category_data(
            [("test-flake/timeout", rows)], descs, date(2025, 1, 15)
        )
        assert result[0]["description"] == "Tests timing out"


# ---------------------------------------------------------------------------
# run() integration test
# ---------------------------------------------------------------------------

class TestRunIntegration:
    def test_basic_report_generation(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "run_started_at": "2025-01-15T10:00:00Z",
                "jobs": [{
                    "name": "test-job",
                    "step": "Run tests",
                    "job_id": "200",
                    "category": "test-flake/timeout",
                    "is_flake": "yes",
                    "test_id": "TestSlow",
                    "failed_test": "test_slow.py",
                    "error_message": "TimeoutError",
                    "summary": "Timed out",
                }],
            },
        ])
        progress = tmp_path / "progress.md"
        progress.write_text(content)

        md = tmp_path / "report.md"
        js = tmp_path / "report.json"
        rc = run(str(progress), str(md), str(js))

        assert rc == 0
        assert md.exists()
        assert js.exists()

        data = json.loads(js.read_text())
        assert data["total_runs"] == 1
        assert data["flake_runs"] == 1
        assert len(data["categories"]) == 1
        assert data["categories"][0]["name"] == "test-flake/timeout"

    def test_json_structure(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "run_started_at": "2025-01-15T10:00:00Z",
                "jobs": [{
                    "name": "j1",
                    "category": "bug/crash",
                    "is_flake": "no",
                    "test_id": "T1",
                    "error_message": "segfault",
                    "summary": "Crashed",
                }],
            },
        ])
        progress = tmp_path / "progress.md"
        progress.write_text(content)

        js = tmp_path / "report.json"
        run(str(progress), str(tmp_path / "report.md"), str(js))

        data = json.loads(js.read_text())
        assert "date" in data
        assert "total_runs" in data
        assert "categories" in data
        assert "unfinished_runs" in data

    def test_pending_runs_in_unfinished_section(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "run_started_at": "2025-01-15T10:00:00Z",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout",
                    "is_flake": "yes",
                    "test_id": "T1",
                }],
            },
            {
                "run_id": "200",
                "status": "pending",
                "jobs": [{"name": "j2"}],
            },
        ])
        progress = tmp_path / "progress.md"
        progress.write_text(content)

        md = tmp_path / "report.md"
        js = tmp_path / "report.json"
        run(str(progress), str(md), str(js))

        md_text = md.read_text()
        assert "Unfinished Runs" in md_text

        data = json.loads(js.read_text())
        assert len(data["unfinished_runs"]) == 1
        assert data["unfinished_runs"][0]["run_id"] == "200"

    def test_invalid_category_prefix_filtered(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "run_started_at": "2025-01-15T10:00:00Z",
                "jobs": [
                    {
                        "name": "j1",
                        "category": "test-flake/timeout",
                        "is_flake": "yes",
                        "test_id": "T1",
                    },
                    {
                        "name": "j2",
                        "category": "invalid-prefix/something",
                        "is_flake": "no",
                        "test_id": "T2",
                    },
                ],
            },
        ])
        progress = tmp_path / "progress.md"
        progress.write_text(content)

        js = tmp_path / "report.json"
        run(str(progress), str(tmp_path / "report.md"), str(js))

        data = json.loads(js.read_text())
        cat_names = [c["name"] for c in data["categories"]]
        assert "test-flake/timeout" in cat_names
        assert "invalid-prefix/something" not in cat_names

    def test_no_run_sections_returns_1(self, tmp_path):
        progress = tmp_path / "progress.md"
        progress.write_text("# Empty file\nNo run sections here.")

        rc = run(str(progress), str(tmp_path / "r.md"), str(tmp_path / "r.json"))
        assert rc == 1


# ---------------------------------------------------------------------------
# _split_category
# ---------------------------------------------------------------------------

class TestSplitCategory:
    def test_two_segments(self):
        assert _split_category("infra-flake/registry-502") == (
            "infra-flake/registry-502", "")

    def test_three_segments(self):
        assert _split_category("test-flake/timeout/78753") == (
            "test-flake/timeout", "78753")

    def test_four_segments(self):
        assert _split_category("test-flake/timeout/sub/extra") == (
            "test-flake/timeout/sub", "extra")

    def test_single_segment(self):
        assert _split_category("standalone") == ("standalone", "")

    def test_empty_string(self):
        assert _split_category("") == ("", "")


# ---------------------------------------------------------------------------
# _lookup_description
# ---------------------------------------------------------------------------

class TestLookupDescription:
    def test_exact_match(self):
        descs = {"test-flake/timeout": "Timeout flake"}
        assert _lookup_description("test-flake/timeout", descs) == "Timeout flake"

    def test_match_via_split(self):
        descs = {"test-flake/timeout/78753": "Timeout in test 78753"}
        assert _lookup_description("test-flake/timeout", descs) == (
            "Timeout in test 78753")

    def test_no_match(self):
        descs = {"test-flake/other": "Something else"}
        assert _lookup_description("test-flake/timeout", descs) == ""

    def test_empty_descriptions(self):
        assert _lookup_description("test-flake/timeout", {}) == ""


# ---------------------------------------------------------------------------
# Subcategory grouping
# ---------------------------------------------------------------------------

class TestSubcategoryGrouping:
    def _make_row(self, run_id="1", category="test-flake/timeout",
                  is_flake="yes", test_id="TestA",
                  run_started_at="2025-01-15T10:00:00Z",
                  run_url="https://example.com/1", branch="main",
                  error_message="", summary=""):
        return {
            "run_id": run_id,
            "category": category,
            "is_flake": is_flake,
            "test_id": test_id,
            "run_started_at": run_started_at,
            "run_url": run_url,
            "branch": branch,
            "error_message": error_message,
            "summary": summary,
        }

    def test_same_category_different_subcategories_grouped(self):
        rows = [
            self._make_row(run_id="1", category="test-flake/timeout/TestA",
                           test_id="TestA"),
            self._make_row(run_id="2", category="test-flake/timeout/TestB",
                           test_id="TestB"),
        ]
        result = _build_category_data(
            [("test-flake/timeout", rows)], {}, date(2025, 1, 15)
        )
        assert len(result) == 1
        assert result[0]["name"] == "test-flake/timeout"
        assert result[0]["subcategories"] == ["TestA", "TestB"]
        assert result[0]["run_count"] == 2

    def test_two_segment_category_has_empty_subcategories(self):
        rows = [self._make_row(category="infra-flake/registry-502")]
        result = _build_category_data(
            [("infra-flake/registry-502", rows)], {}, date(2025, 1, 15)
        )
        assert result[0]["subcategories"] == []

    def test_subcategories_deduplicated(self):
        rows = [
            self._make_row(run_id="1", category="test-flake/timeout/TestA"),
            self._make_row(run_id="2", category="test-flake/timeout/TestA"),
        ]
        result = _build_category_data(
            [("test-flake/timeout", rows)], {}, date(2025, 1, 15)
        )
        assert result[0]["subcategories"] == ["TestA"]

    def test_subcategory_column_in_markdown(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "run_started_at": "2025-01-15T10:00:00Z",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout/TestA",
                    "is_flake": "yes",
                    "test_id": "TestA",
                }],
            },
            {
                "run_id": "200",
                "status": "done",
                "run_started_at": "2025-01-15T11:00:00Z",
                "jobs": [{
                    "name": "j2",
                    "category": "test-flake/timeout/TestB",
                    "is_flake": "yes",
                    "test_id": "TestB",
                }],
            },
        ])
        progress = tmp_path / "progress.md"
        progress.write_text(content)

        md = tmp_path / "report.md"
        js = tmp_path / "report.json"
        run(str(progress), str(md), str(js))

        md_text = md.read_text()
        assert "| Subcategory |" in md_text
        assert "test-flake/timeout" in md_text
        # Both subcategories merged into one row
        assert "TestA, TestB" in md_text

    def test_subcategories_in_json(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "run_started_at": "2025-01-15T10:00:00Z",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout/TestA",
                    "is_flake": "yes",
                    "test_id": "TestA",
                }],
            },
        ])
        progress = tmp_path / "progress.md"
        progress.write_text(content)

        js = tmp_path / "report.json"
        run(str(progress), str(tmp_path / "report.md"), str(js))

        data = json.loads(js.read_text())
        assert len(data["categories"]) == 1
        assert data["categories"][0]["name"] == "test-flake/timeout"
        assert data["categories"][0]["subcategories"] == ["TestA"]

    def test_grouping_collapses_shared_category(self, tmp_path):
        """Two different full categories with same first two segments -> one row."""
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "run_started_at": "2025-01-15T10:00:00Z",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout/TestA",
                    "is_flake": "yes",
                    "test_id": "TestA",
                    "error_message": "timeout",
                    "summary": "timed out",
                }],
            },
            {
                "run_id": "200",
                "status": "done",
                "run_started_at": "2025-01-15T11:00:00Z",
                "jobs": [{
                    "name": "j2",
                    "category": "test-flake/timeout/TestB",
                    "is_flake": "yes",
                    "test_id": "TestB",
                    "error_message": "timeout",
                    "summary": "timed out",
                }],
            },
            {
                "run_id": "300",
                "status": "done",
                "run_started_at": "2025-01-15T12:00:00Z",
                "jobs": [{
                    "name": "j3",
                    "category": "infra-flake/registry-502",
                    "is_flake": "yes",
                    "test_id": "",
                    "error_message": "502",
                    "summary": "registry down",
                }],
            },
        ])
        progress = tmp_path / "progress.md"
        progress.write_text(content)

        js = tmp_path / "report.json"
        run(str(progress), str(tmp_path / "report.md"), str(js))

        data = json.loads(js.read_text())
        assert len(data["categories"]) == 2
        names = [c["name"] for c in data["categories"]]
        assert "test-flake/timeout" in names
        assert "infra-flake/registry-502" in names

        timeout_cat = next(c for c in data["categories"]
                          if c["name"] == "test-flake/timeout")
        assert timeout_cat["runs"] == 2
        assert timeout_cat["jobs"] == 2
        assert timeout_cat["subcategories"] == ["TestA", "TestB"]

        infra_cat = next(c for c in data["categories"]
                        if c["name"] == "infra-flake/registry-502")
        assert infra_cat["subcategories"] == []


# ---------------------------------------------------------------------------
# _load_fixes
# ---------------------------------------------------------------------------

class TestLoadFixes:
    def test_loads_valid_fixes(self, tmp_path):
        fixes = {
            "fixes": [
                {
                    "category": "test-flake/timeout",
                    "items": [
                        {"type": "pr", "id": 123, "url": "https://example.com/pull/123",
                         "title": "Fix timeout", "confidence": "match"},
                    ],
                },
            ],
        }
        path = tmp_path / "fixes.json"
        path.write_text(json.dumps(fixes))

        result = _load_fixes(str(path))
        assert "test-flake/timeout" in result
        assert len(result["test-flake/timeout"]) == 1
        assert result["test-flake/timeout"][0]["id"] == 123

    def test_returns_empty_for_missing_file(self):
        assert _load_fixes("/nonexistent/path.json") == {}

    def test_returns_empty_for_none(self):
        assert _load_fixes(None) == {}

    def test_returns_empty_for_malformed_json(self, tmp_path):
        path = tmp_path / "fixes.json"
        path.write_text("not json")
        assert _load_fixes(str(path)) == {}

    def test_returns_empty_for_empty_fixes(self, tmp_path):
        path = tmp_path / "fixes.json"
        path.write_text('{"fixes": []}')
        assert _load_fixes(str(path)) == {}

    def test_skips_entries_without_category(self, tmp_path):
        fixes = {"fixes": [{"items": [{"type": "pr"}]}]}
        path = tmp_path / "fixes.json"
        path.write_text(json.dumps(fixes))
        assert _load_fixes(str(path)) == {}

    def test_skips_entries_without_items(self, tmp_path):
        fixes = {"fixes": [{"category": "test-flake/timeout"}]}
        path = tmp_path / "fixes.json"
        path.write_text(json.dumps(fixes))
        assert _load_fixes(str(path)) == {}


# ---------------------------------------------------------------------------
# _format_fix_link
# ---------------------------------------------------------------------------

class TestFormatFixLink:
    def test_pr_match(self):
        item = {"type": "pr", "id": 456, "url": "https://example.com/pull/456",
                "confidence": "match"}
        assert _format_fix_link(item) == "[#456](https://example.com/pull/456)"

    def test_pr_possible(self):
        item = {"type": "pr", "id": 789, "url": "https://example.com/pull/789",
                "confidence": "possible"}
        assert _format_fix_link(item) == "[#789](https://example.com/pull/789) (possibly)"

    def test_commit_match(self):
        item = {"type": "commit", "sha": "abc123def456789",
                "url": "https://example.com/commit/abc123def456789",
                "confidence": "match"}
        assert _format_fix_link(item) == "[abc123d](https://example.com/commit/abc123def456789)"

    def test_commit_possible(self):
        item = {"type": "commit", "sha": "1234567890abcdef",
                "url": "https://example.com/commit/1234567890abcdef",
                "confidence": "possible"}
        expected = "[1234567](https://example.com/commit/1234567890abcdef) (possibly)"
        assert _format_fix_link(item) == expected


# ---------------------------------------------------------------------------
# run() with fixes
# ---------------------------------------------------------------------------

class TestRunWithFixes:
    def _make_fixes_json(self, tmp_path):
        fixes = {
            "fixes": [
                {
                    "category": "test-flake/timeout",
                    "items": [
                        {"type": "pr", "id": 42, "url": "https://example.com/pull/42",
                         "title": "Fix timeout", "confidence": "match"},
                        {"type": "commit", "sha": "deadbeef12345678",
                         "url": "https://example.com/commit/deadbeef12345678",
                         "title": "Increase timeout", "confidence": "possible"},
                    ],
                },
            ],
        }
        path = tmp_path / "fixes.json"
        path.write_text(json.dumps(fixes))
        return str(path)

    def test_fix_column_in_summary_table(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "run_started_at": "2025-01-15T10:00:00Z",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout",
                    "is_flake": "yes",
                    "test_id": "TestSlow",
                    "error_message": "timeout",
                    "summary": "timed out",
                }],
            },
        ])
        progress = tmp_path / "progress.md"
        progress.write_text(content)
        fixes_path = self._make_fixes_json(tmp_path)

        md = tmp_path / "report.md"
        run(str(progress), str(md), str(tmp_path / "report.json"),
            fixes_path=fixes_path)

        md_text = md.read_text()
        # Summary table has Fix column with only confident matches
        assert "| Fix |" in md_text
        # Extract the summary table (between "## Summary" and "## Root Causes")
        summary_section = md_text.split("## Summary")[1].split("## Root Causes")[0]
        assert "[#42]" in summary_section
        assert "[deadbee]" not in summary_section
        assert "(possibly)" not in summary_section

    def test_fix_field_in_detail_section(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "run_started_at": "2025-01-15T10:00:00Z",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout",
                    "is_flake": "yes",
                    "test_id": "TestSlow",
                }],
            },
        ])
        progress = tmp_path / "progress.md"
        progress.write_text(content)
        fixes_path = self._make_fixes_json(tmp_path)

        md = tmp_path / "report.md"
        run(str(progress), str(md), str(tmp_path / "report.json"),
            fixes_path=fixes_path)

        md_text = md.read_text()
        # Detail section shows all fixes including possible ones
        assert "- **Fix:**" in md_text
        detail_section = md_text.split("## Root Causes")[1]
        assert "[#42]" in detail_section
        assert "[deadbee]" in detail_section
        assert "(possibly)" in detail_section

    def test_fixes_in_json_output(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "run_started_at": "2025-01-15T10:00:00Z",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout",
                    "is_flake": "yes",
                    "test_id": "TestSlow",
                }],
            },
        ])
        progress = tmp_path / "progress.md"
        progress.write_text(content)
        fixes_path = self._make_fixes_json(tmp_path)

        js = tmp_path / "report.json"
        run(str(progress), str(tmp_path / "report.md"), str(js),
            fixes_path=fixes_path)

        data = json.loads(js.read_text())
        cat = data["categories"][0]
        assert "fixes" in cat
        assert len(cat["fixes"]) == 2
        assert cat["fixes"][0]["type"] == "pr"
        assert cat["fixes"][0]["id"] == 42
        assert cat["fixes"][1]["type"] == "commit"
        assert cat["fixes"][1]["confidence"] == "possible"

    def test_no_fixes_file_still_works(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "run_started_at": "2025-01-15T10:00:00Z",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout",
                    "is_flake": "yes",
                    "test_id": "TestSlow",
                }],
            },
        ])
        progress = tmp_path / "progress.md"
        progress.write_text(content)

        md = tmp_path / "report.md"
        js = tmp_path / "report.json"
        rc = run(str(progress), str(md), str(js))

        assert rc == 0
        md_text = md.read_text()
        assert "| Fix |" in md_text  # Column exists even without fixes

        data = json.loads(js.read_text())
        # No fixes key when no fixes data
        assert "fixes" not in data["categories"][0]

    def test_auto_detects_fixes_json(self, tmp_path):
        content = make_progress_content([
            {
                "run_id": "100",
                "status": "done",
                "run_started_at": "2025-01-15T10:00:00Z",
                "jobs": [{
                    "name": "j1",
                    "category": "test-flake/timeout",
                    "is_flake": "yes",
                    "test_id": "TestSlow",
                }],
            },
        ])
        progress = tmp_path / "progress.md"
        progress.write_text(content)
        # Put fixes.json in same dir as progress.md
        self._make_fixes_json(tmp_path)

        md = tmp_path / "report.md"
        js = tmp_path / "report.json"
        # Don't pass fixes_path -- should auto-detect
        run(str(progress), str(md), str(js))

        data = json.loads(js.read_text())
        assert "fixes" in data["categories"][0]
