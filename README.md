# Flake Control

**Stop guessing which CI failures are real.** flakectl analyzes your GitHub Actions history, reads the logs, and tells you exactly which failures are flaky tests, which are infrastructure hiccups, and which are actual bugs. Your team can stop re-running pipelines and start fixing root causes.

```
18 failed runs analyzed: 18 caused by flakes, 0 caused by real failures.

 1. test-flake/78753-hooks-lifecycle-timeout        runs=9   jobs=19  flake=yes
 2. test-flake/78684-fleet-update-not-scheduled      runs=2   jobs=3   flake=yes
 3. infra-flake/registry-502-blob-fetch              runs=1   jobs=1   flake=yes
    ...
```

Under the hood, flakectl spawns a fleet of Claude agents (via the [Claude Agent SDK](https://docs.anthropic.com/en/docs/agents-sdk)) that work in parallel. Each one downloads a failed run's logs, greps through them, classifies every job, and collaborates with the other agents to build a consistent set of root-cause categories. The result is a structured report you can post to Slack, attach to a ticket, or feed into dashboards.

## GitHub Action

The simplest way to use flakectl is as a GitHub Action in a scheduled or manually triggered workflow.

```yaml
- uses: sk-ilya/flakectl@main
  id: flakes
  with:
    repo: my-org/my-repo
    branch: main
    lookback-days: '7'
    workflow: 'ci.yaml'
    anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
    github_token: ${{ secrets.GITHUB_TOKEN }}
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
| `max-turns` | no | `50` | Safety limit: maximum number of LLM round-trips each classifier agent can make before stopping |
| `anthropic_api_key` | yes | | Anthropic API key (for Claude) |
| `github_token` | yes | | GitHub token with `actions:read` permission on the target repo |

### Outputs

| Output | Description |
|--------|-------------|
| `report` | Path to `report.md` |
| `results` | Path to `report.json` |
| `status` | `ok` or `no-failures` |
| `summary` | Short text summary |

### Example: weekly analysis with Slack notification

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
          github_token: ${{ secrets.GITHUB_TOKEN }}

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

## How it works

```
GitHub Actions API           Claude Agent SDK                 Report
      |                            |                            |
  1. fetch                   3. classify                   4. extract
  failed runs ──> CSV ──> progress.md ──> agent per run ──> report.md
                    |                      |    |    |       report.json
                 2. progress          reads logs, greps
                 (task list)          for failure patterns  5. summarize
                                                            summary.txt
```

1. **Fetch** queries the GitHub API for failed workflow runs within a time window
2. **Progress** builds a coordination file (`progress.md`) listing every failed run and its jobs
3. **Classify** launches one Claude agent per run; each agent downloads logs, identifies root causes, and writes classification to an isolated per-run file (zero contention)
4. **Extract** parses the classified results into `report.md` and `report.json`
5. **Summarize** generates a plain-text `summary.txt` from the report (2-3 sentences for Slack/CI status)

## What you get

### report.md (human-readable, sorted by impact)

```markdown
# Flaky Test Analysis

**18 failed runs** analyzed: **18 caused by flakes**, **0 caused by real failures**.

| #   | Category                                       | Runs/Jobs | Flake? | Last Occurred |
| --- | ---------------------------------------------- | --------- | ------ | ------------- |
| 1   | `test-flake/78753-hooks-lifecycle-timeout`     | 9/19      | yes    | today         |
| 2   | `test-flake/78684-fleet-update-not-scheduled`  | 2/3       | yes    | 6 days ago    |

## Root Causes (Detail)

### 1. `test-flake/78753-hooks-lifecycle-timeout`

**Description:** Device lifecycle hooks test timeout waiting for renderedVersion update

- **Failed runs:** 9
- **Failed jobs:** 19
- **Test IDs:** 78753
- **Example error:** `Device failed to update to renderedVersion 8 (current=7)`
- **Example summary:** Flake: Test 78753 timed out after 5m29s waiting
  for the device to update. The device got stuck with
  "waiting for dependencies"...

| Run ID      | Branch | Date       | Jobs Failed |
| ----------- | ------ | ---------- | ----------- |
| 21878356178 | main   | 2026-02-10 | 2           |
| 21884267011 | main   | 2026-02-10 | 2           |
```

### report.json (structured data for automation)

```json
{
  "date": "2026-02-17",
  "total_runs": 18,
  "flake_runs": 18,
  "real_failure_runs": 0,
  "categories": [
    {
      "name": "test-flake/78753-hooks-lifecycle-timeout",
      "is_flake": "yes",
      "runs": 9,
      "jobs": 19,
      "test_ids": ["78753"],
      "affected_runs": [...]
    }
  ]
}
```

### summary.txt (short CI/Slack summary)

> Analyzed 18 CI workflow runs. All 18 were classified as flakes with no genuine bugs found. The dominant root cause is test-flake/78753 (device lifecycle hooks timeout), accounting for 9 runs.

## Classification categories

Each failure is assigned a root-cause category. One category = one root cause = one fix.

| Type | Flake? | Meaning | Example |
|------|--------|---------|---------|
| `test-flake/<id>-<cause>` | yes | Intermittent test failure (timing, races, fragile assertions) | `test-flake/78753-hooks-timeout` |
| `test-flake/systemic-<cause>` | yes | Environment broken, all tests on a VM fail | `test-flake/systemic-device-stuck-v0` |
| `infra-flake/<cause>` | yes | CI infrastructure issue (network, image pull, registry) | `infra-flake/registry-502-blob-fetch` |
| `bug/<id>-<cause>` | no | Test correctly caught a real code defect | `bug/TestParseConfig-nil-pointer` |
| `build-error/<cause>` | no | Code doesn't compile | `build-error/undefined-symbol` |

The classifier understands Ginkgo, Go testing, pytest, JUnit, Jest, and RSpec test output formats.

## Installation

```bash
pip install .
```

Or directly from git:

```bash
pip install git+https://github.com/sk-ilya/flakectl.git
```

Requires Python 3.12+.

## CLI usage

### Full pipeline (recommended)

```bash
flakectl run \
  --repo my-org/my-repo \
  --branch main \
  --lookback-days 7 \
  --workflow "ci.yaml"
```

This chains all five steps: fetch, progress, classify, extract, summarize.

Use `--model` to select the Claude model for classifier agents (default: `sonnet`):

```bash
flakectl run --repo my-org/my-repo --workflow "ci.yaml" --model opus
```

Requires `ANTHROPIC_API_KEY` and `GITHUB_TOKEN` (or `GH_TOKEN`) environment variables.

Exit codes:
- `0` analysis completed successfully
- `20` no failed runs found; pipeline stops early and writes no-failures `summary.txt`, `report.md`, and `report.json`

### Individual subcommands

```bash
# 1. Fetch failed CI jobs
flakectl fetch \
  --repo my-org/my-repo \
  --branch main \
  --lookback-days 7 \
  --workflow "e2e.yaml" \
  --output failed_jobs.csv

# 2. Generate progress file
flakectl progress --input failed_jobs.csv --output progress.md

# 3. Classify failures (requires ANTHROPIC_API_KEY)
flakectl classify --repo my-org/my-repo --progress progress.md

# 4. Extract results
flakectl extract --input progress.md --output-md report.md --output-json report.json
```

### Custom context

Use `--context` to give classifier agents repo-specific knowledge: test frameworks in use, known infrastructure quirks, job naming conventions, or anything that helps produce more accurate classifications.

```bash
flakectl run \
  --repo my-org/my-repo \
  --workflow "e2e.yaml" \
  --context "This repo uses Ginkgo with numeric test labels (e.g. [78753]). \
The e2e-test jobs run on ephemeral VMs. The 'e2e-summary' job is an \
aggregation job that just reports results from upstream jobs -- skip it, \
focus on the actual test jobs that contain root causes."
```

You can also point to a file:

```bash
flakectl run --repo my-org/my-repo --workflow "e2e.yaml" --context @context.txt
```
