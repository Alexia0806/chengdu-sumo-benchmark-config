#!/usr/bin/env bash
set -euo pipefail

WATCH_NAME="${WATCH_NAME:-benchmark_watch_20260627}"
PROJECT_ROOT="${PROJECT_ROOT:-/root/autodl-tmp/tsc-cycle-benchmark}"
WATCH_DIR="${WATCH_DIR:-/root/autodl-tmp/watchers}"
LOG_FILE="${LOG_FILE:-$WATCH_DIR/${WATCH_NAME}.log}"
INTERVAL_SEC="${INTERVAL_SEC:-60}"
RUN_ROOT_GLOB="${RUN_ROOT_GLOB:-$PROJECT_ROOT/runs/deepsignal_cycleplan}"

mkdir -p "$WATCH_DIR"
echo "$$" > "$WATCH_DIR/${WATCH_NAME}.pid"

while true; do
  {
    echo "===== $(date -Is) ====="
    nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>&1 || true
    echo "--- processes ---"
    ps -eo pid,ppid,etime,stat,cmd \
      | grep -E 'deepsignal_cycleplan_benchmark_chengdu_metrics.py|llama-server|/usr/share/sumo/bin/sumo|run_chengdu_.*20260627|gptoss20b|small_models_resume' \
      | grep -v grep || true
    echo "--- latest summaries ---"
    find "$RUN_ROOT_GLOB" -maxdepth 5 -type f -name matrix_summary.csv -printf '%TY-%Tm-%TdT%TH:%TM:%TS %s %p\n' 2>/dev/null \
      | sort | tail -12 || true
    echo "--- latest logs ---"
    find "$RUN_ROOT_GLOB" /root/autodl-tmp/watchers -maxdepth 5 -type f \( -name '*.log' -o -name '*.console.log' \) -printf '%TY-%Tm-%TdT%TH:%TM:%TS %s %p\n' 2>/dev/null \
      | sort | tail -20 || true
    echo "--- disk ---"
    df -h /root/autodl-tmp /root 2>/dev/null || true
  } >> "$LOG_FILE" 2>&1
  sleep "$INTERVAL_SEC"
done
