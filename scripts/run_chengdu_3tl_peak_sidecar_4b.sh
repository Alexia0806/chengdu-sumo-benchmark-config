#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"

PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
BENCH_ROOT="${DEEPSIGNAL_BENCH_ROOT:-$PROJECT_ROOT/DeepSignal-benchmark}"
RUN_ROOT="${RUN_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_3tl_min10_targetpeak_20260617}"
RUNNER="$PROJECT_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py"
PYTHON_BIN="$TSC_CYCLE_ROOT/.venv/bin/python"
TEMPERATURE="${TEMPERATURE:-0.4}"
TEMP_LABEL="${TEMP_LABEL:-temp04}"
TARGET_TLS="${TARGET_TLS:-$DEFAULT_TARGET_TLS}"
WARMUP_SECONDS="${WARMUP_SECONDS:-300}"
METRIC_SECONDS="${METRIC_SECONDS:-1200}"
TARGET_PEAK_VPH_PER_ROUTE="${TARGET_PEAK_VPH_PER_ROUTE:-240}"
TARGET_PEAK_ROUTES_PER_TL="${TARGET_PEAK_ROUTES_PER_TL:-8}"
TLS_FILE="$RUN_ROOT/chengdu_3tl_tls.csv"
LOG_DIR="$RUN_ROOT/logs"
ORCH_LOG="$LOG_DIR/sidecar_4b.log"
EXPECTED_TL_COUNT="$(wc -w <<< "$TARGET_TLS" | tr -d ' ')"

mkdir -p "$LOG_DIR" "$RUN_ROOT/scripts"
cp "$0" "$RUN_ROOT/scripts/$(basename "$0")"

{
  echo "scenario,tl_id"
  for tl_id in $TARGET_TLS; do
    echo "sumo_llm,$tl_id"
  done
} > "$TLS_FILE"

target_peak_args=()
for tl_id in $TARGET_TLS; do
  target_peak_args+=(--target-peak-tl-id "$tl_id")
done

log_event() {
  printf '[%s] %s\n' "$(date -Is)" "$1" | tee -a "$ORCH_LOG"
}

run_case() {
  local case_name="$1"
  local demand_scale="$2"
  shift 2
  local out_dir="$RUN_ROOT/$case_name"
  mkdir -p "$out_dir"
  if [[ -f "$out_dir/per_tl.jsonl" ]] && [[ "$(wc -l < "$out_dir/per_tl.jsonl")" -ge "$EXPECTED_TL_COUNT" ]] && [[ ! -s "$out_dir/failures.jsonl" ]]; then
    log_event "SKIP $case_name already_complete"
    return
  fi
  log_event "START $case_name demand_scale=$demand_scale"
  PYTHONUNBUFFERED=1 "$PYTHON_BIN" "$RUNNER" \
    --benchmark-root "$BENCH_ROOT" \
    --sumo-home "$SUMO_HOME" \
    --scenario sumo_llm \
    --tls-file "$TLS_FILE" \
    --output-dir "$out_dir" \
    --input-mode github_official \
    --prompt-format deepsignal \
    --no-prefill \
    --warmup-seconds "$WARMUP_SECONDS" \
    --metric-seconds "$METRIC_SECONDS" \
    --decision-interval-seconds 60 \
    --min-green 10 \
    --max-green 90 \
    --phase-queue-mode split-overlap \
    --queue-threshold 10 \
    --pred-wait-forecaster rolling_mean \
    --demand-scale "$demand_scale" \
    "${target_peak_args[@]}" \
    --target-peak-vph-per-route "$TARGET_PEAK_VPH_PER_ROUTE" \
    --target-peak-routes-per-tl "$TARGET_PEAK_ROUTES_PER_TL" \
    --temperature "$TEMPERATURE" \
    --continue-on-run-error \
    "$@" 2>&1 | tee "$LOG_DIR/$case_name.sidecar.console.log"
  log_event "DONE $case_name"
}

for scale in 1.0 1.2 1.5; do
  tag="${scale/./p}"
  run_case "04_qwen3_4b_base_min_green_${TEMP_LABEL}_x${tag}" "$scale" \
    --controller model \
    --model-backend hf \
    --hf-model-path $MODELS_ROOT/Qwen3-4B \
    --hf-dtype bfloat16 \
    --model-fail-policy min_green

  run_case "05_qwen3_4b_base_first_min_green_${TEMP_LABEL}_x${tag}" "$scale" \
    --controller model \
    --model-backend hf \
    --hf-model-path $MODELS_ROOT/Qwen3-4B \
    --hf-dtype bfloat16 \
    --model-fail-policy first_min_green
done

log_event "ALL_DONE sidecar_4b"
