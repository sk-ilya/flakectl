#!/usr/bin/env bash
# Remove generated artifacts from a flakectl run.
set -euo pipefail

rm -f failed_jobs.csv
rm -f progress.md
rm -f report.md report.json summary.txt
rm -f *_*.log
rm -rf runs/
