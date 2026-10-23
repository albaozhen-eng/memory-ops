#!/usr/bin/env bash
# 阴性对照：证明门禁**不是永远绿**。三组对照全在临时目录里做，不碰 HOME。
# 关键：基线一律在健康状态下登记，再用被破坏的输入去比 —— 否则比不出退化。
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(dirname "$HERE")"
T="$(mktemp -d)"; GOOD="$HERE/corpus"
export MEMORY_HOME="$T/home" MEM_DATA_DIR="$T/data" MEM_SESSIONS_DIR="$T/sessions"
export MEMCHECK_HEALTH="$T/health"
export MEMCHECK_ACCEPTED="$T/baseline-accepted.json" MEMCHECK_SCRIPTS="$REPO/scripts"
mkdir -p "$MEM_DATA_DIR" "$MEMCHECK_HEALTH"

run() { python3 "$REPO/scripts/golden_eval.py" --golden "$HERE/golden.json" \
        --corpus "$1" --out "$T/out" --variant question "${@:2}"; }

echo "### 基线（健康状态）先登记"
run "$GOOD" --accept 2>&1 | grep -E "已登记|recall@top3" | sed 's/^/  /'

echo
echo "### 对照 1：语料削到只剩 1 篇 → 期望判红"
S="$T/starved"; mkdir -p "$S"; LAST="$(ls "$GOOD" | tail -1)"
head -20 "$GOOD/$LAST" > "$S/$LAST"
run "$S" --gate 2>&1 | sed -n '/门禁/,$p' | head -3 | sed 's/^/  /'

echo
echo "### 对照 2：破坏检索（拿掉 plugmem_config.py）→ 期望判红且说「分数不可信」"
B="$T/broken"; mkdir -p "$B"; cp "$REPO"/scripts/*.py "$B/"
python3 -c "import os;os.remove('$B/plugmem_config.py')"
MEMCHECK_SCRIPTS="$B" python3 "$B/golden_eval.py" --golden "$HERE/golden.json" \
  --corpus "$GOOD" --out "$T/out2" --variant question --accept >/dev/null 2>&1
MEMCHECK_SCRIPTS="$B" python3 "$B/golden_eval.py" --golden "$HERE/golden.json" \
  --corpus "$GOOD" --out "$T/out2" --variant question --gate 2>&1 | sed -n '/门禁/,$p' | head -3 | sed 's/^/  /'

echo
echo "### 对照 3：语料目录为空 → 期望探针判红"
E="$T/empty"; mkdir -p "$E"
MEM_DAILY_NOTES_DIR="$E" python3 "$REPO/scripts/memory_health.py" 2>&1 | sed 's/^/  /'
echo "  exit=$? （2 = 有红灯）"
