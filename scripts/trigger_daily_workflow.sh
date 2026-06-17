#!/usr/bin/env bash
set -euo pipefail

PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

repo="S1lverYin/paper-daily"
workflow="daily.yml"
log_prefix="$(date '+%Y-%m-%d %H:%M:%S %Z')"

if [[ "${DRY_RUN:-}" == "1" ]]; then
  echo "[$log_prefix] Dry run: checking GitHub CLI access"
  gh auth status
  gh workflow view "$workflow" -R "$repo" >/dev/null
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Dry run OK"
  exit 0
fi

echo "[$log_prefix] Triggering ${repo}/${workflow}"
gh workflow run "$workflow" -R "$repo" -f lookback_days=1

sleep 5
latest_run=$(
  gh run list \
    -R "$repo" \
    --workflow "$workflow" \
    --event workflow_dispatch \
    --limit 1 \
    --json databaseId,url,status,createdAt \
    --jq '.[0] | "\(.databaseId) \(.status) \(.createdAt) \(.url)"'
)

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Latest dispatch: ${latest_run}"
