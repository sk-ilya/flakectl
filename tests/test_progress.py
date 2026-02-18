"""Tests for flakectl.progress -- CSV-to-markdown transform."""

from conftest import make_csv_content
from flakectl.progress import run


class TestProgressRun:
    def test_basic_generation(self, tmp_path):
        csv_content = make_csv_content([
            {
                "run_id": "100",
                "run_url": "https://example.com/100",
                "branch": "main",
                "event": "push",
                "commit_sha": "abc123",
                "failed_job_name": "unit-tests",
                "run_started_at": "2025-01-15T10:00:00Z",
                "job_completed_at": "2025-01-15T10:05:00Z",
                "run_attempt": "1",
                "failure_step": "Run tests",
            },
            {
                "run_id": "100",
                "run_url": "https://example.com/100",
                "branch": "main",
                "event": "push",
                "commit_sha": "abc123",
                "failed_job_name": "lint",
                "run_started_at": "2025-01-15T10:00:00Z",
                "job_completed_at": "2025-01-15T10:03:00Z",
                "run_attempt": "1",
                "failure_step": "Run lint",
            },
            {
                "run_id": "200",
                "run_url": "https://example.com/200",
                "branch": "feat",
                "event": "pull_request",
                "commit_sha": "def456",
                "failed_job_name": "integration",
                "run_started_at": "2025-01-14T09:00:00Z",
                "job_completed_at": "2025-01-14T09:30:00Z",
                "run_attempt": "1",
                "failure_step": "Run integration",
            },
        ])
        csv_path = tmp_path / "failed_jobs.csv"
        csv_path.write_text(csv_content)

        out = tmp_path / "progress.md"
        rc = run(str(csv_path), str(out))

        assert rc == 0
        text = out.read_text()
        assert "<!-- BEGIN RUN 100 -->" in text
        assert "<!-- BEGIN RUN 200 -->" in text
        assert "#### job: `unit-tests`" in text
        assert "#### job: `lint`" in text
        assert "#### job: `integration`" in text

    def test_categories_markers_present(self, tmp_path):
        csv_content = make_csv_content([{
            "run_id": "100",
            "run_url": "u",
            "branch": "main",
            "event": "push",
            "commit_sha": "a",
            "failed_job_name": "j1",
            "run_started_at": "2025-01-15T10:00:00Z",
            "job_completed_at": "",
            "run_attempt": "1",
            "failure_step": "",
        }])
        csv_path = tmp_path / "failed_jobs.csv"
        csv_path.write_text(csv_content)

        out = tmp_path / "progress.md"
        run(str(csv_path), str(out))

        text = out.read_text()
        assert "<!-- CATEGORIES START -->" in text
        assert "<!-- CATEGORIES END -->" in text

    def test_job_field_template_structure(self, tmp_path):
        csv_content = make_csv_content([{
            "run_id": "100",
            "run_url": "u",
            "branch": "main",
            "event": "push",
            "commit_sha": "a",
            "failed_job_name": "j1",
            "run_started_at": "2025-01-15T10:00:00Z",
            "job_completed_at": "",
            "run_attempt": "1",
            "failure_step": "Step X",
        }])
        csv_path = tmp_path / "failed_jobs.csv"
        csv_path.write_text(csv_content)

        out = tmp_path / "progress.md"
        run(str(csv_path), str(out))

        text = out.read_text()
        assert "- **step**: Step X" in text
        assert "- **job_id**:" in text
        assert "- **category**:" in text
        assert "- **is_flake**:" in text
        assert "- **test_id**:" in text

    def test_skip_jobs_filtering(self, tmp_path):
        csv_content = make_csv_content([
            {
                "run_id": "100",
                "run_url": "u",
                "branch": "main",
                "event": "push",
                "commit_sha": "a",
                "failed_job_name": "keep-me",
                "run_started_at": "2025-01-15T10:00:00Z",
                "job_completed_at": "",
                "run_attempt": "1",
                "failure_step": "",
            },
            {
                "run_id": "100",
                "run_url": "u",
                "branch": "main",
                "event": "push",
                "commit_sha": "a",
                "failed_job_name": "skip-me",
                "run_started_at": "2025-01-15T10:00:00Z",
                "job_completed_at": "",
                "run_attempt": "1",
                "failure_step": "",
            },
        ])
        csv_path = tmp_path / "failed_jobs.csv"
        csv_path.write_text(csv_content)

        out = tmp_path / "progress.md"
        run(str(csv_path), str(out), skip_jobs=["skip-me"])

        text = out.read_text()
        assert "#### job: `keep-me`" in text
        assert "#### job: `skip-me`" not in text

    def test_skip_all_jobs_in_run_omits_run_section(self, tmp_path):
        csv_content = make_csv_content([{
            "run_id": "100",
            "run_url": "u",
            "branch": "main",
            "event": "push",
            "commit_sha": "a",
            "failed_job_name": "skip-me",
            "run_started_at": "2025-01-15T10:00:00Z",
            "job_completed_at": "",
            "run_attempt": "1",
            "failure_step": "",
        }])
        csv_path = tmp_path / "failed_jobs.csv"
        csv_path.write_text(csv_content)

        out = tmp_path / "progress.md"
        run(str(csv_path), str(out), skip_jobs=["skip-me"])

        text = out.read_text()
        assert "<!-- BEGIN RUN 100 -->" not in text

    def test_multiple_jobs_same_run(self, tmp_path):
        csv_content = make_csv_content([
            {
                "run_id": "100",
                "run_url": "u",
                "branch": "main",
                "event": "push",
                "commit_sha": "a",
                "failed_job_name": "job-a",
                "run_started_at": "2025-01-15T10:00:00Z",
                "job_completed_at": "",
                "run_attempt": "1",
                "failure_step": "Step A",
            },
            {
                "run_id": "100",
                "run_url": "u",
                "branch": "main",
                "event": "push",
                "commit_sha": "a",
                "failed_job_name": "job-b",
                "run_started_at": "2025-01-15T10:00:00Z",
                "job_completed_at": "",
                "run_attempt": "1",
                "failure_step": "Step B",
            },
        ])
        csv_path = tmp_path / "failed_jobs.csv"
        csv_path.write_text(csv_content)

        out = tmp_path / "progress.md"
        run(str(csv_path), str(out))

        text = out.read_text()
        # Only one run section, but two jobs
        assert text.count("<!-- BEGIN RUN 100 -->") == 1
        assert "#### job: `job-a`" in text
        assert "#### job: `job-b`" in text

    def test_empty_csv(self, tmp_path):
        csv_content = (
            "run_id,run_url,branch,event,commit_sha,"
            "failed_job_name,run_started_at,job_completed_at,"
            "run_attempt,failure_step\n"
        )
        csv_path = tmp_path / "failed_jobs.csv"
        csv_path.write_text(csv_content)

        out = tmp_path / "progress.md"
        rc = run(str(csv_path), str(out))

        assert rc == 0
        text = out.read_text()
        assert "<!-- BEGIN RUN" not in text
