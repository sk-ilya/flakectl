# flakectl

**Stop guessing which CI failures are real.** flakectl analyzes your GitHub Actions history, reads the logs, and tells you exactly which failures are flaky tests, which are infrastructure hiccups, and which are actual bugs -- so your team can stop re-running pipelines and start fixing root causes.

```
18 failed runs analyzed: 18 caused by flakes, 0 caused by real failures.

 1. test-flake/78753-hooks-lifecycle-timeout        runs=9   jobs=19  flake=yes
 2. test-flake/78684-fleet-update-not-scheduled      runs=2   jobs=3   flake=yes
 3. infra-flake/registry-502-blob-fetch              runs=1   jobs=1   flake=yes
    ...
```

Under the hood, flakectl spawns a fleet of Claude agents (via the [Claude Agent SDK](https://docs.anthropic.com/en/docs/agents-sdk)) that work in parallel -- each one downloads a failed run's logs, greps through them, classifies every job, and collaborates with the other agents to build a consistent set of root-cause categories. The result is a structured report you can post to Slack, attach to a ticket, or feed into dashboards.

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

1. **Fetch** -- queries the GitHub API for failed workflow runs within a time window
2. **Progress** -- builds a coordination file (`progress.md`) listing every failed run and its jobs
3. **Classify** -- launches one Claude agent per run; each agent downloads logs, identifies root causes, and writes classification to an isolated per-run file (zero contention)
4. **Extract** -- parses the classified results into `report.md` and `report.json`
5. **Summarize** -- generates a plain-text `summary.txt` from the report (2-3 sentences for Slack/CI status)

## What you get

### report.md -- human-readable, sorted by impact

```markdown
# Flaky Test Analysis

**18 failed runs** analyzed: **18 caused by flakes**, **0 caused by real failures**.

| # | Category | Runs/Jobs | Flake? | Last Occurred |
|---|----------|-----------|--------|---------------|
| 1 | `test-flake/78753-hooks-lifecycle-timeout` | 9/19 | yes | today |
| 2 | `test-flake/78684-fleet-update-not-scheduled` | 2/3 | yes | 6 days ago |

## Root Causes (Detail)

### 1. `test-flake/78753-hooks-lifecycle-timeout`

**Description:** Device lifecycle hooks test timeout waiting for renderedVersion update

- **Failed runs:** 9
- **Failed jobs:** 19
- **Test IDs:** 78753
- **Example error:** `Device failed to update to renderedVersion 8 (current=7)`
- **Example summary:** Flake: Test 78753 timed out after 5m29s waiting for the device
  to update. The device got stuck with "waiting for dependencies"...

| Run ID | Branch | Date | Jobs Failed |
|--------|--------|------|-------------|
| 21878356178 | main | 2026-02-10 | 2 |
| 21884267011 | main | 2026-02-10 | 2 |
```

### report.json -- structured data for automation

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

### summary.txt -- short CI/Slack summary

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
  --since 7 \
  --workflow "ci.yaml"
```

This chains all five steps: fetch -> progress -> classify -> extract -> summarize.

Use `--model` to select the Claude model for classifier agents (default: `sonnet`):

```bash
flakectl run --repo my-org/my-repo --workflow "ci.yaml" --model opus
```

Requires `ANTHROPIC_API_KEY` and `GITHUB_TOKEN` (or `GH_TOKEN`) environment variables.

Exit codes:
- `0` -- analysis completed
- `20` -- no failed runs found; pipeline stops early and writes no-failures `summary.txt`, `report.md`, and `report.json`

### Individual subcommands

```bash
# 1. Fetch failed CI jobs
flakectl fetch \
  --repo my-org/my-repo \
  --branch main \
  --since 7 \
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

Use `--context` to give classifier agents repo-specific knowledge -- test frameworks in use, known infrastructure quirks, job naming conventions, or anything that helps produce more accurate classifications.

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

## GitHub Action

```yaml
- uses: sk-ilya/flakectl@main
  id: flakes
  with:
    repo: my-org/my-repo
    branch: main
    since: '7'
    workflow: 'ci.yaml'
    anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
    github_token: ${{ secrets.GITHUB_TOKEN }}
```

### Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `repo` | yes | | Repository to analyze (`owner/name`) |
| `branch` | no | `main` | Branch name, comma-separated list, or `*` for all |
| `since` | no | `7` | Look-back period in days |
| `workflow` | yes | | Workflow YAML file, comma-separated list, or `*` for all |
| `skip-jobs` | no | | Comma-separated job names to exclude from analysis |
| `context` | no | | Repo-specific context for classifier agents (see below) |
| `model` | no | `sonnet` | Claude model for classifier agents (e.g. `sonnet`, `opus`, `haiku`) |
| `anthropic_api_key` | yes | | Anthropic API key |
| `github_token` | yes | | GitHub token with `actions:read` on the target repo |

### Outputs

| Output | Description |
|--------|-------------|
| `report` | Path to `report.md` |
| `results` | Path to `report.json` |
| `status` | `ok` or `no-failures` |
| `summary` | Short text summary |

### Secrets

| Secret | Purpose |
|--------|---------|
| `ANTHROPIC_API_KEY` | Claude agents for log analysis |
| `GITHUB_TOKEN` | Token with `actions:read` on the target repo |

### Example: weekly analysis with Slack notification

```yaml
name: Flaky Test Analysis
on:
  schedule:
    - cron: '0 9 * * 1'  # Monday 9am UTC
  workflow_dispatch:
    inputs:
      branch:
        description: 'Branch (name, comma-separated, or * for all)'
        default: 'main'
      since:
        description: 'Look-back period in days'
        default: '7'

jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - name: Analyze flakes
        id: flakes
        uses: sk-ilya/flakectl@main
        with:
          repo: my-org/my-repo
          branch: ${{ inputs.branch || 'main' }}
          since: ${{ inputs.since || '7' }}
          workflow: 'e2e.yaml'
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          github_token: ${{ secrets.GITHUB_TOKEN }}

      - name: Upload report
        uses: actions/upload-artifact@v4
        with:
          name: flaky-test-report
          path: |
            ${{ steps.flakes.outputs.report }}
            ${{ steps.flakes.outputs.results }}

      - name: Create gist
        id: gist
        run: |
          URL=$(gh gist create --public=false \
            --desc "Flaky Test Analysis - $(date +%Y-%m-%d)" \
            "${{ steps.flakes.outputs.report }}" 2>&1 | tail -1)
          echo "url=$URL" >> $GITHUB_OUTPUT
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      - name: Post to Slack
        if: steps.flakes.outcome == 'success'
        uses: slackapi/slack-github-action@v2.1.0
        with:
          webhook: ${{ secrets.SLACK_WEBHOOK_URL }}
          webhook-type: incoming-webhook
          payload: |
            {
              "blocks": [
                {
                  "type": "header",
                  "text": {"type": "plain_text", "text": "Flaky Test Report"}
                },
                {
                  "type": "section",
                  "text": {"type": "mrkdwn", "text": "${{ steps.flakes.outputs.summary }}"}
                },
                {
                  "type": "actions",
                  "elements": [
                    {
                      "type": "button",
                      "text": {"type": "plain_text", "text": "Full Report"},
                      "url": "${{ steps.gist.outputs.url }}"
                    }
                  ]
                }
              ]
            }
```
