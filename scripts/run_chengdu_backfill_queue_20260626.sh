#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"

PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
RUNNER_SH="${RUNNER_SH:-$PROJECT_ROOT/scripts/run_chengdu_backfill_aligned_matrix_20260626.sh}"
STAMP="${STAMP:-$(date +%Y%m%dT%H%M%S)}"
QUEUE_ROOT="${QUEUE_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_backfill_aligned_queue_20260626_$STAMP}"
LOG_DIR="$QUEUE_ROOT/logs"
ORCH_LOG="$LOG_DIR/queue.log"

SCENARIOS="${SCENARIOS:-unbalanced_x1p5 balanced_x1p5 balanced_x1p2 unbalanced_x1p2}"
TEMPERATURES="${TEMPERATURES:-0.1 0.2}"
RUN_DEFAULT="${RUN_DEFAULT:-0}"
RUN_UNBALANCED_X15_FULL3TL="${RUN_UNBALANCED_X15_FULL3TL:-0}"
PARALLEL_QWEN="${PARALLEL_QWEN:-1}"

mkdir -p "$LOG_DIR"
echo "$$" > "$QUEUE_ROOT/queue.pid"

log_event() {
  printf '[%s] %s\n' "$(date -Is)" "$1" | tee -a "$ORCH_LOG"
}

gpu_total_mib() {
  nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' '
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
    python3 "$PROJECT_ROOT/scripts/summarize_chengdu_peak_matrix.py" "$run_root" \
      > "$run_root/matrix_summary.md" 2>> "$LOG_DIR/${model_key}.summary.err" || true
  fi
}

retry_if_failed() {
  local model_key="$1"
  local rc="$2"
  if [[ "$rc" == "0" ]]; then
    return 0
  fi
  log_event "RETRY_SEQUENTIAL model=$model_key previous_rc=$rc"
  wait_for_idle "retry_$model_key"
  run_group "$model_key"
}

log_event "QUEUE_START queue_root=$QUEUE_ROOT scenarios='$SCENARIOS' temps='$TEMPERATURES' run_default=$RUN_DEFAULT parallel_qwen=$PARALLEL_QWEN gpu_total_mib=$(gpu_total_mib || true)"
if ! nvidia-smi -L >/dev/null 2>&1; then
  log_event "ERROR no_gpu_available"
  exit 64
fi

qwen4_rc=0
qwen9_rc=0
if [[ "$PARALLEL_QWEN" == "1" && "$(gpu_total_mib)" -ge 32000 ]]; then
  log_event "QWEN_PARALLEL_START"
  (run_group qwen4b) &
  qwen4_pid=$!
  echo "$qwen4_pid" > "$QUEUE_ROOT/qwen4b.pid"
  (run_group qwen9b) &
  qwen9_pid=$!
  echo "$qwen9_pid" > "$QUEUE_ROOT/qwen9b.pid"
  wait "$qwen4_pid" || qwen4_rc=$?
  wait "$qwen9_pid" || qwen9_rc=$?
  log_event "QWEN_PARALLEL_DONE qwen4b_rc=$qwen4_rc qwen9b_rc=$qwen9_rc"
  retry_if_failed qwen4b "$qwen4_rc"
  retry_if_failed qwen9b "$qwen9_rc"
else
  log_event "QWEN_SEQUENTIAL_START reason='parallel_disabled_or_gpu_too_small'"
  wait_for_idle qwen4b
  run_group qwen4b
  wait_for_idle qwen9b
  run_group qwen9b
fi

for model_key in fp16 phi4 gemma12; do
  wait_for_idle "$model_key"
  run_group "$model_key"
done

for model_key in qwen4b qwen9b fp16 phi4 gemma12; do
  summarize_group "$model_key"
done

log_event "QUEUE_DONE queue_root=$QUEUE_ROOT"
