#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"

PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
CURRENT_ROOT="$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_3tl_min10_targetpeak_20260617"
LOG="$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_temperature_sweep_20260617.log"

log_event() {
  printf '[%s] %s\n' "$(date -Is)" "$1" | tee -a "$LOG"
}

pid_alive() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  local pid
  pid="$(cat "$file" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

case_complete() {
  local run_root="$1"
  local case_name="$2"
  local file="$run_root/$case_name/per_tl.jsonl"
  [[ -f "$file" ]] && [[ "$(wc -l < "$file")" -ge 3 ]]
}

wait_for_current_temp04() {
  log_event "WAIT current_temp04 $CURRENT_ROOT"
  while true; do
    local alive=0
    pid_alive "$CURRENT_ROOT/orchestrator.pid" && alive=1
    pid_alive "$CURRENT_ROOT/sidecar_4b.pid" && alive=1
    if [[ "$alive" == "0" ]]; then
      python3 "$PROJECT_ROOT/scripts/summarize_chengdu_peak_matrix.py" "$CURRENT_ROOT" | tee "$CURRENT_ROOT/matrix_summary.md"
      log_event "DONE current_temp04"
      return
    fi
    sleep 60
  done
}

run_temperature() {
  local temperature="$1"
  local label="$2"
  local run_root="$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_3tl_min10_targetpeak_${label}_20260617"
  mkdir -p "$run_root/logs"
  log_event "START temperature=$temperature label=$label run_root=$run_root"
  RUN_ROOT="$run_root" TEMPERATURE="$temperature" TEMP_LABEL="$label" SKIP_4B=1 \
    nohup "$PROJECT_ROOT/scripts/run_chengdu_3tl_peak_matrix.sh" > "$run_root/logs/nohup.out" 2>&1 &
  echo $! > "$run_root/orchestrator.pid"
  RUN_ROOT="$run_root" TEMPERATURE="$temperature" TEMP_LABEL="$label" \
    nohup "$PROJECT_ROOT/scripts/run_chengdu_3tl_peak_sidecar_4b.sh" > "$run_root/logs/sidecar_4b.nohup.out" 2>&1 &
  echo $! > "$run_root/sidecar_4b.pid"

  while true; do
    local alive=0
    pid_alive "$run_root/orchestrator.pid" && alive=1
    pid_alive "$run_root/sidecar_4b.pid" && alive=1
    if [[ "$alive" == "0" ]]; then
      python3 "$PROJECT_ROOT/scripts/summarize_chengdu_peak_matrix.py" "$run_root" | tee "$run_root/matrix_summary.md"
      log_event "DONE temperature=$temperature label=$label"
      return
    fi
    sleep 60
  done
}

wait_for_current_temp04
run_temperature "0.2" "temp02"
run_temperature "0.1" "temp01"
log_event "ALL_DONE temperature_sweep"
