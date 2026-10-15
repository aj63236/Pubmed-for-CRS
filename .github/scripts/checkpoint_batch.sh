#!/usr/bin/env bash
# Persist recovery state even when collection fails. Never publish unvalidated days.
set -euo pipefail
git config user.name "腸嚐新知 Bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
for file in data/_batch_pending.json data/_batch_results.json data/_cache_v3.json; do
  if [ -f "$file" ]; then
    git add -- "$file"
  fi
done
if ! git diff --staged --quiet; then
  git commit -m "💾 保存批次恢復狀態與已驗證摘要快取"
  git pull --rebase --autostash origin main
  git push
fi
