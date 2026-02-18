"""Tests for flakectl.fetch -- data filtering, validation, CSV I/O."""

import csv
from unittest.mock import patch

import pytest

from flakectl.fetch import (
    build_csv_rows,
    filter_runs_by_date,
    get_first_failed_step,
    parse_list_arg,
    validate_workflows,
    write_csv,
)


# ---------------------------------------------------------------------------
# filter_runs_by_date
# ---------------------------------------------------------------------------

class TestFilterRunsByDate:
    def test_empty_list(self):
        assert filter_runs_by_date([], "2025-01-01") == []

    def test_all_pass(self):
        runs = [
            {"created_at": "2025-01-10T10:00:00Z"},
            {"created_at": "2025-01-11T10:00:00Z"},
        ]
        result = filter_runs_by_date(runs, "2025-01-01")
        assert len(result) == 2

    def test_all_excluded(self):
        runs = [
            {"created_at": "2024-12-01T10:00:00Z"},
            {"created_at": "2024-12-15T10:00:00Z"},
        ]
        result = filter_runs_by_date(runs, "2025-01-01")
        assert result == []

    def test_boundary_exact_gte(self):
        runs = [{"created_at": "2025-01-01T00:00:00Z"}]
        result = filter_runs_by_date(runs, "2025-01-01")
        assert len(result) == 1

    def test_mixed(self):
        runs = [
            {"created_at": "2024-12-31T23:59:59Z"},
            {"created_at": "2025-01-01T00:00:01Z"},
            {"created_at": "2025-01-05T10:00:00Z"},
        ]
        result = filter_runs_by_date(runs, "2025-01-01")
        assert len(result) == 2


# ---------------------------------------------------------------------------
# get_first_failed_step
# ---------------------------------------------------------------------------

class TestGetFirstFailedStep:
    def test_found(self):
        steps = [
            {"name": "Checkout", "conclusion": "success"},
            {"name": "Run tests", "conclusion": "failure"},
            {"name": "Upload", "conclusion": "skipped"},
        ]
        assert get_first_failed_step(steps) == "Run tests"

    def test_multiple_failures_returns_first(self):
        steps = [
            {"name": "Build", "conclusion": "failure"},
            {"name": "Test", "conclusion": "failure"},
        ]
        assert get_first_failed_step(steps) == "Build"

    def test_none_failed(self):
        steps = [
            {"name": "Checkout", "conclusion": "success"},
            {"name": "Build", "conclusion": "success"},
        ]
        assert get_first_failed_step(steps) == ""

    def test_empty_list(self):
        assert get_first_failed_step([]) == ""


# ---------------------------------------------------------------------------
# parse_list_arg
# ---------------------------------------------------------------------------

class TestParseListArg:
    def test_wildcard_returns_none(self):
        assert parse_list_arg("*") is None

    def test_single_value(self):
        assert parse_list_arg("main") == ["main"]

    def test_multiple_with_spaces(self):
        assert parse_list_arg("main, develop, feature") == ["main", "develop", "feature"]

    def test_empty_segments_filtered(self):
        assert parse_list_arg("main,,develop,") == ["main", "develop"]


# ---------------------------------------------------------------------------
# validate_workflows
# ---------------------------------------------------------------------------

class TestValidateWorkflows:
    def test_none_passthrough(self):
        validate_workflows(None)  # should not raise

    def test_valid_yml(self):
        validate_workflows(["ci.yml", "test.yaml"])  # should not raise

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="must be YAML filenames"):
            validate_workflows(["ci.yml", "invalid-name"])


# ---------------------------------------------------------------------------
# build_csv_rows (mocked)
# ---------------------------------------------------------------------------

class TestBuildCsvRows:
    @patch("flakectl.fetch.list_failed_jobs")
    def test_basic_row_building(self, mock_list):
        mock_list.return_value = [
            {
                "id": 200,
                "name": "unit-tests",
                "conclusion": "failure",
                "steps": [
                    {"name": "Checkout", "conclusion": "success"},
                    {"name": "Run tests", "conclusion": "failure"},
                ],
                "completed_at": "2025-01-15T10:05:00Z",
            }
        ]
        runs = [{
            "id": 100,
            "url": "https://example.com/100",
            "head_branch": "main",
            "event": "push",
            "head_sha": "abc123",
            "created_at": "2025-01-15T10:00:00Z",
            "run_attempt": 1,
        }]
        rows = build_csv_rows("org/repo", runs)
        assert len(rows) == 1
        assert rows[0]["run_id"] == 100
        assert rows[0]["failed_job_name"] == "unit-tests"
        assert rows[0]["failure_step"] == "Run tests"

    @patch("flakectl.fetch.list_failed_jobs")
    def test_no_failed_jobs_skips_run(self, mock_list):
        mock_list.return_value = []
        runs = [{
            "id": 100,
            "url": "https://example.com/100",
            "head_branch": "main",
            "event": "push",
            "head_sha": "abc123",
            "created_at": "2025-01-15T10:00:00Z",
            "run_attempt": 1,
        }]
        rows = build_csv_rows("org/repo", runs)
        assert rows == []

    @patch("flakectl.fetch.list_failed_jobs")
    def test_failure_step_extraction(self, mock_list):
        mock_list.return_value = [
            {
                "id": 200,
                "name": "build",
                "conclusion": "failure",
                "steps": [
                    {"name": "Setup", "conclusion": "success"},
                ],
                "completed_at": "2025-01-15T10:05:00Z",
            }
        ]
        runs = [{
            "id": 100,
            "url": "https://example.com/100",
            "head_branch": "main",
            "event": "push",
            "head_sha": "abc123",
            "created_at": "2025-01-15T10:00:00Z",
            "run_attempt": 1,
        }]
        rows = build_csv_rows("org/repo", runs)
        assert rows[0]["failure_step"] == ""


# ---------------------------------------------------------------------------
# write_csv
# ---------------------------------------------------------------------------

class TestWriteCsv:
    def test_sorts_descending_by_date(self, tmp_path):
        rows = [
            {
                "run_id": "1", "run_url": "u1", "branch": "main",
                "event": "push", "commit_sha": "a", "failed_job_name": "j1",
                "run_started_at": "2025-01-10T00:00:00Z",
                "job_completed_at": "", "run_attempt": "1", "failure_step": "",
            },
            {
                "run_id": "2", "run_url": "u2", "branch": "main",
                "event": "push", "commit_sha": "b", "failed_job_name": "j2",
                "run_started_at": "2025-01-15T00:00:00Z",
                "job_completed_at": "", "run_attempt": "1", "failure_step": "",
            },
        ]
        out = tmp_path / "output.csv"
        write_csv(rows, str(out))

        with open(out) as f:
            reader = list(csv.DictReader(f))

        assert reader[0]["run_id"] == "2"
        assert reader[1]["run_id"] == "1"

    def test_correct_column_order(self, tmp_path):
        rows = [{
            "run_id": "1", "run_url": "u", "branch": "main",
            "event": "push", "commit_sha": "a", "failed_job_name": "j",
            "run_started_at": "2025-01-15T00:00:00Z",
            "job_completed_at": "", "run_attempt": "1", "failure_step": "s",
        }]
        out = tmp_path / "output.csv"
        write_csv(rows, str(out))

        with open(out) as f:
            header = f.readline().strip()

        expected = "run_id,run_url,branch,event,commit_sha,failed_job_name,run_started_at,job_completed_at,run_attempt,failure_step"
        assert header == expected
