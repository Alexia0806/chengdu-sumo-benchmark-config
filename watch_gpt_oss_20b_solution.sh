#!/usr/bin/env bash
set -euo pipefail

RUN_ROOT="/root/autodl-tmp/tsc-cycle-benchmark/runs/deepsignal_cycleplan/chengdu_2tl_J54_432452987_unbalanced_x1p5_gpt_oss_20b_solution_temp0102_20260625"
STATUS_PY="/root/autodl-tmp/model_downloads_20260624/check_gpt_oss_solution_status.py"
WATCH_LOG="/root/autodl-tmp/model_downloads_20260624/gpt_oss_20b_solution_watcher.log"
NOHUP_LOG="/root/autodl-tmp/model_downloads_20260624/gpt_oss_20b_2tl_unbalanced_x1p5_solution_full.nohup"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-120}"

echo "watcher_start ts=$(date -Is) interval=${INTERVAL_SECONDS}s run_root=$RUN_ROOT" >> "$WATCH_LOG"

while true; do
  {
    echo "===== poll $(date -Is) ====="
    echo "--- procs ---"
    ps -eo pid,ppid,stat,etime,%cpu,%mem,args \
      | grep -E 'gpt_oss_20b_solution|deepsignal_cycleplan_benchmark|sumo' \
      | grep -v grep || true
    echo "--- gpu ---"
    nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu,power.draw \
      --format=csv,noheader,nounits 2>/dev/null || true
    echo "--- orchestrator ---"
    tail -n 10 "$RUN_ROOT/logs/orchestrator.log" 2>/dev/null || true
    echo "--- status ---"
    /root/miniconda3/bin/python3 "$STATUS_PY" 2>/dev/null || true
    echo "--- log tail ---"
    tail -n 20 "$NOHUP_LOG" 2>/dev/null || true
  } >> "$WATCH_LOG" 2>&1

  if ! pgrep -f "gpt_oss_20b_solution_temp.*deepsignal_cycleplan_benchmark" >/dev/null 2>&1 \
    && ! pgrep -f "run_gpt_oss_20b_2tl_unbalanced_x1p5_solution_full.sh" >/dev/null 2>&1; then
    {
      echo "===== final $(date -Is) ====="
      echo "benchmark_process_not_running"
      /root/miniconda3/bin/python3 "$STATUS_PY" 2>/dev/null || true
      tail -n 30 "$RUN_ROOT/logs/orchestrator.log" 2>/dev/null || true
    } >> "$WATCH_LOG" 2>&1
    break
  fi

  sleep "$INTERVAL_SECONDS"
done

echo "watcher_exit ts=$(date -Is)" >> "$WATCH_LOG"
