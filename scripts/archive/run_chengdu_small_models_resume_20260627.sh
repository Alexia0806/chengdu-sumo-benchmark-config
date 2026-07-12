#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/env_defaults.sh"

PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
RUNNER_SH="${RUNNER_SH:-$PROJECT_ROOT/scripts/run_chengdu_backfill_aligned_matrix_20260626.sh}"
STAMP="${STAMP:-$(date +%Y%m%dT%H%M%S)}"
QUEUE_ROOT="${QUEUE_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_small_models_resume_20260627_$STAMP}"
LOG_DIR="$QUEUE_ROOT/logs"

SCENARIOS="${SCENARIOS:-unbalanced_x1p5 balanced_x1p5 balanced_x1p2 unbalanced_x1p2}"
TEMPERATURES="${TEMPERATURES:-0.1 0.2}"
RUN_DEFAULT="${RUN_DEFAULT:-0}"
RUN_UNBALANCED_X15_FULL3TL="${RUN_UNBALANCED_X15_FULL3TL:-0}"
PYTHON_BIN="${PYTHON_BIN:-$TSC_CYCLE_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

mkdir -p "$LOG_DIR"
echo "$$" > "$QUEUE_ROOT/queue.pid"

log_event() {
  printf '[%s] %s\n' "$(date -Is)" "$1" | tee -a "$LOG_DIR/queue.log"
}

active_benchmark_count() {
  ps -eo pid=,command= \
    | grep -E "deepsignal_cycleplan_benchmark_chengdu_metrics.py|llama-server|${SUMO_HOME}/bin/sumo" \
    | grep -v grep \
    | wc -l \
    | tr -d ' '
}

wait_for_idle() {
  local label="$1"
  for _ in $(seq 1 720); do
    if [[ "$(active_benchmark_count)" == "0" ]]; then
      return 0
    fi
    log_event "WAIT_IDLE label=$label active=$(active_benchmark_count)"
    sleep 60
  done
  log_event "ERROR wait_for_idle_timeout label=$label"
  return 1
}

run_group() {
  local model_key="$1"
  local run_root="$QUEUE_ROOT/$model_key"
  local log_file="$LOG_DIR/${model_key}.console.log"
  mkdir -p "$run_root"
  log_event "GROUP_START model=$model_key run_root=$run_root"
  RUN_ROOT="$run_root" \
  SCENARIOS="$SCENARIOS" \
  TEMPERATURES="$TEMPERATURES" \
  RUN_DEFAULT="$RUN_DEFAULT" \
  RUN_UNBALANCED_X15_FULL3TL="$RUN_UNBALANCED_X15_FULL3TL" \
  MODEL_KEYS="$model_key" \
  PYTHON_BIN="$PYTHON_BIN" \
  HF_DTYPE=bfloat16 \
  HF_DEVICE_MAP=auto \
  PHI4_N_PREDICT=384 \
  PHI4_TIMEOUT_SEC=600 \
  HF_N_PREDICT=2048 \
  HF_TIMEOUT_SEC=1800 \
  DRY_RUN=0 \
  bash "$RUNNER_SH" 2>&1 | tee "$log_file"
  local rc=${PIPESTATUS[0]}
  log_event "GROUP_DONE model=$model_key rc=$rc run_root=$run_root"
  return "$rc"
}

summarize_group() {
  local model_key="$1"
  local run_root="$QUEUE_ROOT/$model_key"
  if [[ -d "$run_root" && -f "$PROJECT_ROOT/scripts/summarize_chengdu_peak_matrix.py" ]]; then
    "$PYTHON_BIN" "$PROJECT_ROOT/scripts/summarize_chengdu_peak_matrix.py" "$run_root" \
      > "$run_root/matrix_summary.md" 2>> "$LOG_DIR/${model_key}.summary.err" || true
  fi
}

log_event "QUEUE_START queue_root=$QUEUE_ROOT scenarios='$SCENARIOS' temps='$TEMPERATURES' run_default=$RUN_DEFAULT"
if ! nvidia-smi -L >/dev/null 2>&1; then
  log_event "ERROR no_gpu_available"
  exit 64
fi

status=0
for model_key in phi4 gemma12; do
  wait_for_idle "$model_key"
  run_group "$model_key" || status=$?
  summarize_group "$model_key"
done

log_event "QUEUE_DONE rc=$status queue_root=$QUEUE_ROOT"
exit "$status"
