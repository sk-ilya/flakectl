# Flake Control

**Stop guessing which CI failures are real.** flakectl analyzes your GitHub Actions history, reads the logs, and tells you exactly which failures are flaky tests, which are infrastructure hiccups, and which are actual bugs. Your team can stop re-running pipelines and start fixing root causes.

```
18 failed runs analyzed: 18 caused by flakes, 0 caused by real failures.

 1. test-flake/hooks-lifecycle-timeout               runs=9   jobs=19  flake=yes
 2. test-flake/fleet-update-not-scheduled            runs=2   jobs=3   flake=yes
 3. infra-flake/registry-502-blob-fetch              runs=1   jobs=1   flake=yes
    ...
```

Under the hood, flakectl spawns a fleet of Claude agents (via the [Claude Agent SDK](https://docs.anthropic.com/en/docs/agents-sdk)) that work in parallel. Each one downloads a failed run's logs, greps through them, classifies every job, and collaborates with the other agents to build a consistent set of root-cause categories. For each root cause, the report links to recent commits and open PRs that attempt to address it, so you can see at a glance what has already been done or is in progress. The result is a structured report you can post to Slack, attach to a ticket, or feed into dashboards.

## Outputs and report structure

The main output is **`report.md`**: a human-readable root-cause report you can quickly scan and act on.

### `report.md` at a glance

```markdown
# Flaky Test Analysis

**Date:** 2026-02-19

**12 failed runs** analyzed: **12 caused by flakes**, **0 caused by real failures**.

## Summary

| # | Category                               | Subcategory  | Runs/Jobs | Flake? | Last Occurred | Fix   |
|---|----------------------------------------|--------------|----------|--------|---------------|-------|
| 1 | `test-flake/hooks-lifecycle-timeout`   | 12345, 12346 | 9/12     | yes    | 2 days ago    | PR(s) |
| 2 | `infra-flake/registry-502-bad-gateway` |              | 1/1      | yes    | 6 days ago    |       |

## Root Causes (Detail)

### 1. `test-flake/hooks-lifecycle-timeout`

**Description:** Flake: a lifecycle test intermittently times out waiting for a device state transition to complete.

- **Failed runs:** 9
- **Failed jobs:** 12
- **Test IDs:** 12345, 12346
- **Fix:** PR(s) (possibly)
- **Example error:** `Timed out after 5m waiting for device status to converge`

| Run ID | Branch | Date | Jobs Failed |
|--------|--------|------|------------|
| [21952435434](https://github.com/my-org/my-repo/actions/runs/21952435434) | main | 2026-02-12 | 1 |
```

## Classification categories

Each failure is assigned a root-cause category.

| Type | Flake? | Meaning | Example |
|------|--------|---------|---------|
| `test-flake/<cause>/<id>` | yes | Intermittent test failure (timing, races, fragile assertions) | `test-flake/hooks-timeout/78753` |
| `test-flake/systemic-<cause>` | yes | Environment broken, all tests on a VM fail | `test-flake/systemic-device-stuck-v0` |
| `infra-flake/<cause>` | yes | CI infrastructure issue (network, image pull, registry) | `infra-flake/registry-502-blob-fetch` |
| `bug/<cause>/<id>` | no | Test correctly caught a real code defect | `bug/nil-pointer/TestParseConfig` |
| `build-error/<cause>` | no | Code doesn't compile | `build-error/undefined-symbol` |

The classifier understands Ginkgo, Go testing, pytest, JUnit, Jest, and RSpec test output formats.

## Use in GitHub Actions

The simplest way to use flakectl is as a GitHub Action in a scheduled or manually triggered workflow.

- Pin `uses:` to a tag or commit SHA for reproducible runs.
- If you use the default `github.token`, you may need explicit permissions (especially in locked-down orgs):

```yaml
permissions:
  actions: read
  contents: read
  pull-requests: read
```

```yaml
- uses: sk-ilya/flakectl@main
  id: flakes
  with:
    repo: my-org/my-repo
    branch: main
    lookback-days: '7'
    workflow: 'ci.yaml'
    anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
```

### Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `repo` | yes | | Repository to analyze, e.g. `my-org/my-repo` |
| `branch` | no | `main` | Only analyze runs from this branch. Accepts a single name, a comma-separated list, or `*` for all branches |
| `lookback-days` | no | `7` | How far back to search for failed runs, in days |
| `workflow` | yes | | Which workflow file(s) to analyze (filename from `.github/workflows/`, e.g. `ci.yaml`). Accepts a single name, a comma-separated list, or `*` for all |
| `skip-jobs` | no | | Job names to ignore (comma-separated). Useful for aggregation or reporting jobs that don't contain real test output |
| `context` | no | | Free-text hints for the classifier, e.g. test framework details, known infra quirks, or job naming conventions |
| `model` | no | `sonnet` | Claude model to use: `sonnet`, `opus`, or `haiku` |
| `stale-timeout` | no | `60` | Safety limit: if no run finishes for this many minutes, cancel remaining work and report what was completed so far |
| `max-turns-classify` | no | `60` | Safety limit: maximum number of LLM round-trips each classifier agent can make before stopping |
| `max-turns-correlate` | no | `80` | Safety limit: maximum number of LLM round-trips the correlator agent can make before stopping |
| `anthropic_api_key` | yes | | Anthropic API key (for Claude) |
| `github_token` | no | `${{ github.token }}` | GitHub token with `actions:read` permission on the target repo. Override with a PAT for cross-repo analysis |

### Outputs

| Output | Description |
|--------|-------------|
| `report` | Path to `report.md` |
| `results` | Path to `report.json` |
| `status` | `ok` or `no-failures` |
| `summary` | Short text summary |

The action also appends `report.md` to the job's step summary (`$GITHUB_STEP_SUMMARY`), so you can read the full report directly in the GitHub Actions UI.

### Tip: upload the report as a build artifact

If you want the full `report.md` / `report.json` attached to the workflow run (not just the step summary), upload them as artifacts:

```yaml
- name: Upload flakectl report
  if: always()
  uses: actions/upload-artifact@v4
  with:
    name: flakectl-report
    path: |
      ${{ steps.flakes.outputs.report }}
      ${{ steps.flakes.outputs.results }}
```

### Example: weekly analysis with Slack notification

<details>
<summary>Example workflow</summary>

```yaml
name: Weekly Flake Analysis
on:
  schedule:
    - cron: '0 9 * * 1'  # Monday 9am UTC
  workflow_dispatch:

jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - name: Analyze flakes
        id: flakes
        uses: sk-ilya/flakectl@main
        with:
          repo: my-org/my-repo
          branch: main
          lookback-days: '7'
          workflow: e2e.yaml
          skip-jobs: e2e-summary
          context: >-
            This repo uses Ginkgo with numeric test labels (e.g. [78753]).
            The e2e-test jobs run on ephemeral VMs.
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}

      - name: Notify Slack
        if: success()
        run: |
          SUMMARY="${{ steps.flakes.outputs.summary }}"
          RUN_URL="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
          curl -sf -X POST "${{ secrets.SLACK_WEBHOOK_URL }}" \
            -H "Content-Type: application/json" \
            -d "$(jq -n \
              --arg summary "$SUMMARY" \
              --arg link "$RUN_URL" \
              '{summary: $summary, report_link: $link}')"
```

</details>

## How it works

```
GitHub Actions API           Claude Agent SDK                          Report
      |                            |                                     |
  1. fetch                   3. classify          4. correlate      5. extract
  failed runs ──> CSV ──> progress.md ──> agent per run ──> fixes ──> report.md
                    |                      |    |    |       .json     report.json
                 2. progress          reads logs, greps
                 (task list)          for failure patterns           6. summarize
                                                                     summary.txt
```

1. **Fetch** queries the GitHub API for failed workflow runs within a time window
2. **Progress** builds a coordination file (`progress.md`) listing every failed run and its jobs
3. **Classify** launches one Claude agent per run; each agent downloads logs, identifies root causes, and writes classification to an isolated per-run file (zero contention)
4. **Correlate** searches recent commits and open PRs for fixes that match each root-cause category, producing `fixes.json`
5. **Extract** parses the classified results and fix correlations into `report.md` and `report.json`
6. **Summarize** generates a plain-text `summary.txt` from the report (2-3 sentences for Slack/CI status)

## Installation (local)

Requires Python 3.12+.

For best results from the fix correlation step, install the [GitHub CLI](https://cli.github.com/) (`gh`). (On `ubuntu-latest` GitHub Actions runners, `gh` is already available.)

```bash
pip install .
```

Or directly from git:

```bash
pip install git+https://github.com/sk-ilya/flakectl.git
```

## CLI (local)

```bash
flakectl run \
  --repo my-org/my-repo \
  --branch main \
  --lookback-days 7 \
  --workflow "ci.yaml" \
  --output-dir out
```

Requires `ANTHROPIC_API_KEY` and `GITHUB_TOKEN` (or `GH_TOKEN`) environment variables. If `gh` is installed, the correlate step will also use the same token for searching commits/PRs.

For better classifications on large repos, pass repo-specific hints via `--context` (inline text or `@file`).

By default, outputs are written to the current directory. Use `--output-dir` to write everything into a dedicated folder.

### `flakectl run` flags

| Flag | Default | Notes |
|------|---------|-------|
| `--repo` | (required) | `owner/name` |
| `--workflow` | `*` | YAML filename(s) from `.github/workflows/` (comma-separated) or `*` for all |
| `--branch` | `main` | Branch name(s) (comma-separated) or `*` for all |
| `--lookback-days` | `7` | Look-back window in days |
| `--output-dir` | `.` | Where to write `report.md`, `report.json`, and intermediates |
| `--skip-jobs` | (empty) | Comma-separated job names to ignore |
| `--context` | (empty) | Inline text or `@file` (read file contents) |
| `--model` | `sonnet` | Passed through to the Claude Agent SDK |
| `--stale-timeout` | `60` | Minutes with no progress before stopping remaining work |
| `--max-turns-classify` | `60` | Max turns per classifier agent |
| `--max-turns-correlate` | `80` | Max turns for the correlator agent |

## Automation outputs

If you want to post-process results or feed them into tooling, flakectl also produces:

- **`report.json`**: structured report data
- **`summary.txt`**: short 2-3 sentence summary
- **`failed_jobs.csv`**, **`progress.md`**, **`fixes.json`**: intermediate artifacts useful for debugging/automation

```json
{
  "date": "2026-02-19",
  "total_runs": 12,
  "flake_runs": 12,
  "real_failure_runs": 0,
  "unclear_runs": 0,
  "total_jobs": 16,
  "categories": [
    {
      "name": "test-flake/hooks-lifecycle-timeout",
      "description": "Flake: a lifecycle test intermittently times out waiting for a device state transition to complete.",
      "is_flake": "yes",
      "runs": 9,
      "jobs": 12,
      "test_ids": ["12345", "12346"],
      "subcategories": ["12345", "12346"],
      "affected_runs": [
        {
          "run_id": "21952435434",
          "run_url": "https://github.com/my-org/my-repo/actions/runs/21952435434",
          "branch": "main",
          "date": "2026-02-12",
          "jobs_failed": 1
        }
      ]
    }
  ]
}
```

Note: if **no failed runs** are found, `flakectl run` exits with status code `20` and writes a stub `report.json` containing `"status": "no-failures"` plus an empty `categories` list.

## Project layout

The GitHub Action is defined in `action.yml`, and the CLI/package lives in `src/flakectl/`.
