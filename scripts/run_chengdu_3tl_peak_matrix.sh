#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"

PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
BENCH_ROOT="${DEEPSIGNAL_BENCH_ROOT:-$PROJECT_ROOT/DeepSignal-benchmark}"
RUN_ROOT="${RUN_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_3tl_min10_targetpeak_20260617}"
RUNNER="$PROJECT_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py"
PYTHON_BIN="${PYTHON_BIN:-$TSC_CYCLE_ROOT/.venv/bin/python}"
TEMPERATURE="${TEMPERATURE:-0.4}"
TEMP_LABEL="${TEMP_LABEL:-temp04}"
SKIP_4B="${SKIP_4B:-0}"
TARGET_TLS="${TARGET_TLS:-$DEFAULT_TARGET_TLS}"
WARMUP_SECONDS="${WARMUP_SECONDS:-300}"
METRIC_SECONDS="${METRIC_SECONDS:-1200}"
TARGET_PEAK_VPH_PER_ROUTE="${TARGET_PEAK_VPH_PER_ROUTE:-240}"
TARGET_PEAK_ROUTES_PER_TL="${TARGET_PEAK_ROUTES_PER_TL:-8}"
TARGET_PEAK_ROUTE_SELECTION="${TARGET_PEAK_ROUTE_SELECTION:-$DEFAULT_TARGET_PEAK_ROUTE_SELECTION}"
BASE_ONLINE_CONTROL_MODE="${BASE_ONLINE_CONTROL_MODE:-strict}"
TLS_FILE="$RUN_ROOT/chengdu_3tl_tls.csv"
LOG_DIR="$RUN_ROOT/logs"
ORCH_LOG="$LOG_DIR/orchestrator.log"
EXPECTED_TL_COUNT="$(wc -w <<< "$TARGET_TLS" | tr -d ' ')"

mkdir -p "$RUN_ROOT" "$LOG_DIR" "$RUN_ROOT/scripts"
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
  local msg="$1"
  printf '[%s] %s\n' "$(date -Is)" "$msg" | tee -a "$ORCH_LOG"
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
    --target-peak-route-selection "$TARGET_PEAK_ROUTE_SELECTION" \
    --temperature "$TEMPERATURE" \
    --continue-on-run-error \
    "$@" 2>&1 | tee "$LOG_DIR/$case_name.console.log"
  log_event "DONE $case_name"
}

for scale in 1.0 1.2 1.5; do
  tag="${scale/./p}"
  run_case "00_default_sumo_x${tag}" "$scale" \
    --controller fixed \
    --input-mode legacy_snapshot

  run_case "01_9b_adapter_${TEMP_LABEL}_x${tag}" "$scale" \
    --controller model \
    --model-backend hf \
    --hf-model-path "$MODELS_ROOT/Qwen3.5-9B-Base" \
    --hf-adapter-path "$TSC_CYCLE_ROOT/runs/qwen35-9b-text-5090-1p5epoch-20260615T072040Z/adapter" \
    --hf-dtype bfloat16 \
    --model-fail-policy keep_default

  run_case "02_9b_base_hf_${TEMP_LABEL}_x${tag}" "$scale" \
    --controller model \
    --model-backend hf \
    --hf-model-path "$MODELS_ROOT/Qwen3.5-9B-Base" \
    --hf-dtype bfloat16 \
    --prompt-format deepsignal_json \
    --online-control-mode "$BASE_ONLINE_CONTROL_MODE" \
    --model-fail-policy keep_default

  run_case "03_model_fp16_20260519_${TEMP_LABEL}_x${tag}" "$scale" \
    --controller model \
    --model-backend llama \
    --gguf-path "$MODELS_ROOT/model-fp16-20260519.gguf" \
    --llama-server "$LLAMA_SERVER" \
    --ngl 99 \
    --threads 8 \
    --ctx-size 4096 \
    --n-predict 512 \
    --timeout-sec 600 \
    --server-startup-sec 240 \
    --model-fail-policy keep_default

  if [[ "$SKIP_4B" != "1" ]]; then
    run_case "04_qwen3_4b_base_${BASE_ONLINE_CONTROL_MODE}_${TEMP_LABEL}_x${tag}" "$scale" \
      --controller model \
      --model-backend hf \
      --hf-model-path "$MODELS_ROOT/Qwen3-4B" \
      --hf-dtype bfloat16 \
      --prompt-format deepsignal_json \
      --hf-use-chat-template \
      --no-hf-chat-template-enable-thinking \
      --hf-skip-special-tokens \
      --online-control-mode "$BASE_ONLINE_CONTROL_MODE" \
      --model-fail-policy keep_default
  fi
done

python3 "$PROJECT_ROOT/scripts/summarize_chengdu_peak_matrix.py" "$RUN_ROOT" | tee "$RUN_ROOT/matrix_summary.md"
log_event "ALL_DONE $RUN_ROOT"
