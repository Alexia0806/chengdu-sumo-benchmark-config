#!/usr/bin/env bash
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"

INTERVAL_SEC="${1:-30}"
WATCH_ROOT="${WATCH_ROOT:-$WATCH_ROOT}"
PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
RUN_BASE="${RUN_BASE:-$PROJECT_ROOT/runs/deepsignal_cycleplan}"
MODEL_DIR="${MODEL_DIR:-$MODELS_ROOT/Qwen3.6-27B}"
mkdir -p "$WATCH_ROOT"

LOG_PATH="${WATCH_LOG_PATH:-$WATCH_ROOT/dual5090_qwen36_watch_$(date +%Y%m%dT%H%M%S).log}"
STATUS_PATH="${WATCH_STATUS_PATH:-$WATCH_ROOT/dual5090_qwen36_latest.status}"

echo "$$" > "$WATCH_ROOT/dual5090_qwen36_watch.pid"
echo "watcher_pid=$$" > "$STATUS_PATH"
echo "log_path=$LOG_PATH" >> "$STATUS_PATH"
echo "started_at=$(date -Is)" >> "$STATUS_PATH"

snapshot() {
  {
    echo "===== $(date -Is) ====="
    echo "[gpu]"
    nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw --format=csv,noheader,nounits 2>&1 || true
    echo "[gpu-processes]"
    nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_memory --format=csv,noheader,nounits 2>&1 || true
    echo "[bench-processes]"
    ps -eo pid,ppid,etime,cmd | grep -E "deepsignal_cycleplan_benchmark|run_chengdu|qwen36|Qwen3.6|$SUMO_HOME/bin/sumo" | grep -v grep || true
    echo "[disk]"
    df -h "$AUTODL_ROOT" 2>&1 || true
    if [[ -d "$MODEL_DIR" ]]; then
      echo "[model]"
      du -sh "$MODEL_DIR" 2>&1 || true
      find "$MODEL_DIR" -maxdepth 1 -name "*.safetensors" -printf "%f %s\n" 2>/dev/null | sort | tail -20 || true
      find "$MODEL_DIR" -name "*.incomplete" -printf "%p %s\n" 2>/dev/null | sort | tail -20 || true
    fi
    if [[ -d "$RUN_BASE" ]]; then
      latest_root="$(find "$RUN_BASE" -mindepth 1 -maxdepth 1 -type d -printf "%T@ %p\n" 2>/dev/null | sort -n | tail -1 | cut -d' ' -f2-)"
      echo "[latest-run]"
      echo "${latest_root:-none}"
      if [[ -n "${latest_root:-}" ]]; then
        find "$latest_root" -maxdepth 3 \( -name "*.console.log" -o -name "benchmark.log" -o -name "failures.jsonl" -o -name "per_tl.jsonl" \) -printf "%T@ %p\n" 2>/dev/null | sort -n | tail -8 | cut -d' ' -f2- | while read -r log_file; do
          [[ -f "$log_file" ]] || continue
          echo "--- tail: $log_file ---"
          tail -20 "$log_file" 2>&1 || true
        done
      fi
    fi
    echo
  } >> "$LOG_PATH"
}

trap 'echo "stopped_at=$(date -Is)" >> "$STATUS_PATH"; exit 0' INT TERM

while true; do
  snapshot
  {
    echo "last_snapshot_at=$(date -Is)"
    echo "log_path=$LOG_PATH"
  } > "$STATUS_PATH.tmp"
  mv "$STATUS_PATH.tmp" "$STATUS_PATH"
  sleep "$INTERVAL_SEC"
done
