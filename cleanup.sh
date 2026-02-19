#!/usr/bin/env bash
# Remove generated artifacts from a flakectl run.
set -euo pipefail

rm -f failed_jobs.csv
rm -f progress.md
rm -f report.md report.json summary.txt
rm -f fixes.json
rm -f candidates_commits.tsv candidates_prs.tsv
rm -rf files/
rm -rf runs/
