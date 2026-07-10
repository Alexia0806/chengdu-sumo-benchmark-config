#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/root/autodl-tmp/tsc-cycle-benchmark}"
RUNNER_SH="${RUNNER_SH:-$PROJECT_ROOT/scripts/run_chengdu_backfill_aligned_matrix_20260626.sh}"
PYTHON_BIN="${PYTHON_BIN:-/root/miniconda3/bin/python3.12}"
QUEUE_ROOT="${QUEUE_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_qwen36_loaderfix_queue_20260626_$(date +%Y%m%dT%H%M%S)}"
LOG_DIR="$QUEUE_ROOT/logs"

mkdir -p "$LOG_DIR"
echo "$$" > "$QUEUE_ROOT/queue.pid"

log_event() {
  printf '[%s] %s\n' "$(date -Is)" "$1" | tee -a "$LOG_DIR/queue.log"
}

run_qwen36_matrix() {
  local run_root="$1"
  local scenarios="$2"
  local temperatures="$3"
  RUN_ROOT="$run_root" \
  SCENARIOS="$scenarios" \
  TEMPERATURES="$temperatures" \
  RUN_DEFAULT=0 \
  RUN_UNBALANCED_X15_FULL3TL=0 \
  MODEL_KEYS="qwen36" \
  PYTHON_BIN="$PYTHON_BIN" \
  HF_DTYPE="bfloat16" \
  HF_DEVICE_MAP="auto" \
  QWEN36_N_PREDICT=384 \
  QWEN36_TIMEOUT_SEC=2400 \
  DRY_RUN=0 \
  bash "$RUNNER_SH"
}

assert_smoke_ok() {
  local smoke_root="$1"
  if grep -R "HF model load failed" "$smoke_root" >/dev/null 2>&1; then
    log_event "SMOKE_FAILED reason=hf_model_load_failed smoke_root=$smoke_root"
    return 1
  fi

  local per_tl_rows
  per_tl_rows="$(find "$smoke_root" -name per_tl.jsonl -type f -exec awk 'END {total += NR} END {print total + 0}' {} + 2>/dev/null || echo 0)"
  if [[ -z "$per_tl_rows" || "$per_tl_rows" == "0" ]]; then
    log_event "SMOKE_FAILED reason=no_per_tl_rows smoke_root=$smoke_root"
    return 1
  fi
  log_event "SMOKE_OK per_tl_rows=$per_tl_rows smoke_root=$smoke_root"
}

log_event "QUEUE_START queue_root=$QUEUE_ROOT"
log_event "SMOKE_START scenario=unbalanced_x1p5 temp=0.1 model=qwen36"
SMOKE_ROOT="$QUEUE_ROOT/smoke_unbalanced_x1p5_temp01"
set +e
run_qwen36_matrix "$SMOKE_ROOT" "unbalanced_x1p5" "0.1" 2>&1 | tee "$LOG_DIR/qwen36_smoke.console.log"
smoke_rc=${PIPESTATUS[0]}
set -e
log_event "SMOKE_DONE rc=$smoke_rc smoke_root=$SMOKE_ROOT"
if (( smoke_rc != 0 )); then
  exit "$smoke_rc"
fi
assert_smoke_ok "$SMOKE_ROOT"

FULL_ROOT="$QUEUE_ROOT/qwen36_full_matrix"
log_event "FULL_MATRIX_START run_root=$FULL_ROOT scenarios='unbalanced_x1p5 balanced_x1p5 balanced_x1p2 unbalanced_x1p2' temps='0.1 0.2'"
set +e
run_qwen36_matrix "$FULL_ROOT" "unbalanced_x1p5 balanced_x1p5 balanced_x1p2 unbalanced_x1p2" "0.1 0.2" 2>&1 | tee "$LOG_DIR/qwen36_full.console.log"
full_rc=${PIPESTATUS[0]}
set -e
log_event "FULL_MATRIX_DONE rc=$full_rc run_root=$FULL_ROOT"
exit "$full_rc"
