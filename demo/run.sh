#!/usr/bin/env bash
# 30 秒看到工具在跑。**不接触你的 HOME**：所有状态写进本目录 .run/。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(dirname "$HERE")"

# 环境隔离：否则 memory_recall.py 会往 ~/.openclaw/data/ 写检索指标
export MEMORY_HOME="$HERE/.run"
export MEM_DAILY_NOTES_DIR="$HERE/corpus"
export MEM_DATA_DIR="$HERE/.run/data"
export MEM_SESSIONS_DIR="$HERE/.run/sessions"
export MEM_MEMORY_MD="$HERE/.run/MEMORY.md"
export MEMCHECK_HEALTH="$HERE/.run/health"
export MEMCHECK_ACCEPTED="$HERE/.run/baseline-accepted.json"
export MEMCHECK_SCRIPTS="$REPO/scripts"
mkdir -p "$MEM_DATA_DIR" "$MEMCHECK_HEALTH"

python3 "$REPO/scripts/golden_eval.py" \
  --golden "$HERE/golden.json" --corpus "$HERE/corpus" \
  --out "$HERE/reports" --variant question "$@"
