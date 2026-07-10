#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/root/autodl-tmp/tsc-cycle-benchmark"
BENCH_ROOT="$PROJECT_ROOT/DeepSignal-benchmark"
RUN_ROOT="${RUN_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_3tl_att_awt_targetpeak_x1p8_20260618}"
RUNNER="$PROJECT_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py"
PYTHON_BIN="${PYTHON_BIN:-/root/autodl-tmp/TSC_CYCLE_v1/.venv/bin/python}"
TARGET_PEAK_VPH_PER_ROUTE="${TARGET_PEAK_VPH_PER_ROUTE:-240}"
TARGET_PEAK_ROUTES_PER_TL="${TARGET_PEAK_ROUTES_PER_TL:-8}"
TRIPINFO_DRAIN_SECONDS="${TRIPINFO_DRAIN_SECONDS:-600}"
BASE_ONLINE_CONTROL_MODE="${BASE_ONLINE_CONTROL_MODE:-strict}"
TLS_FILE="$RUN_ROOT/chengdu_3tl_tls.csv"
LOG_DIR="$RUN_ROOT/logs"
ORCH_LOG="$LOG_DIR/orchestrator.log"

mkdir -p "$RUN_ROOT" "$LOG_DIR" "$RUN_ROOT/scripts"
cp "$0" "$RUN_ROOT/scripts/$(basename "$0")"
echo "$$" > "$RUN_ROOT/orchestrator.pid"

cat > "$TLS_FILE" <<'CSV'
scenario,tl_id
sumo_llm,J54
sumo_llm,314655170
sumo_llm,432452987
CSV

log_event() {
  local msg="$1"
  printf '[%s] %s\n' "$(date -Is)" "$msg" | tee -a "$ORCH_LOG"
}

temp_label() {
  case "$1" in
    0.1) echo temp01 ;;
    0.2) echo temp02 ;;
    0.4) echo temp04 ;;
    *) echo "temp${1/./p}" ;;
  esac
}

scale_tag() {
  echo "${1/./p}"
}

run_case() {
  local case_name="$1"
  local demand_scale="$2"
  shift 2
  local out_dir="$RUN_ROOT/$case_name"
  mkdir -p "$out_dir"
  if [[ -f "$out_dir/per_tl.jsonl" ]] && [[ "$(wc -l < "$out_dir/per_tl.jsonl")" -ge 3 ]] && [[ ! -s "$out_dir/failures.jsonl" ]]; then
    log_event "SKIP $case_name already_complete"
    return 0
  fi

  log_event "START $case_name demand_scale=$demand_scale target_peak_vph_per_route=$TARGET_PEAK_VPH_PER_ROUTE target_peak_routes_per_tl=$TARGET_PEAK_ROUTES_PER_TL tripinfo_drain=$TRIPINFO_DRAIN_SECONDS"
  PYTHONUNBUFFERED=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True "$PYTHON_BIN" "$RUNNER" \
    --benchmark-root "$BENCH_ROOT" \
    --sumo-home /usr/share/sumo \
    --scenario sumo_llm \
    --tls-file "$TLS_FILE" \
    --output-dir "$out_dir" \
    --input-mode github_official \
    --prompt-format deepsignal \
    --no-prefill \
    --warmup-seconds 300 \
    --metric-seconds 1200 \
    --decision-interval-seconds 60 \
    --min-green 10 \
    --max-green 90 \
    --phase-queue-mode split-overlap \
    --queue-threshold 10 \
    --queue-thresholds 10 20 30 40 \
    --tripinfo-metrics \
    --tripinfo-drain-seconds "$TRIPINFO_DRAIN_SECONDS" \
    --pred-wait-forecaster rolling_mean \
    --demand-scale "$demand_scale" \
    --target-peak-tl-id J54 \
    --target-peak-tl-id 314655170 \
    --target-peak-tl-id 432452987 \
    --target-peak-vph-per-route "$TARGET_PEAK_VPH_PER_ROUTE" \
    --target-peak-routes-per-tl "$TARGET_PEAK_ROUTES_PER_TL" \
    --continue-on-run-error \
    "$@" 2>&1 | tee "$LOG_DIR/$case_name.console.log"
  log_event "DONE $case_name"
}

cat > "$RUN_ROOT/experiment_matrix.json" <<JSON
{
  "run_root": "$RUN_ROOT",
  "tls": ["J54", "314655170", "432452987"],
  "demand_scales": [1.0, 1.2, 1.5, 1.8],
  "temperatures": [0.1, 0.2, 0.4],
  "excluded_model_groups": ["Fine-tuned 9B / 01_9b_adapter"],
  "model_groups": [
    "SUMO default",
    "Base 9B HF",
    "model-fp16-20260519.gguf",
    "Base 4B + strict keep_default"
  ],
  "prompt_policy": {
    "base_models_chat_template": true,
    "base_lenient_json_extraction": true,
    "prompt_format": "deepsignal",
    "prefill": false,
    "base_online_control_mode": "$BASE_ONLINE_CONTROL_MODE"
  },
  "target_peak": {
    "vph_per_route_base": $TARGET_PEAK_VPH_PER_ROUTE,
    "routes_per_tl": $TARGET_PEAK_ROUTES_PER_TL,
    "max_demand_scale": 1.8
  },
  "queue_thresholds": [10, 20, 30, 40],
  "tripinfo": {
    "enabled": true,
    "drain_seconds": $TRIPINFO_DRAIN_SECONDS,
    "metrics": ["network_att_sec", "network_awt_sec", "target_tl_att_sec", "target_tl_awt_sec"]
  }
}
JSON

log_event "RUN_START run_root=$RUN_ROOT"
log_event "MATRIX tls=J54,314655170,432452987 scales=1.0,1.2,1.5,1.8 temps=0.1,0.2,0.4 excluded=Fine-tuned_9B queue_thresholds=10,20,30,40"

for scale in 1.0 1.2 1.5 1.8; do
  tag="$(scale_tag "$scale")"
  run_case "00_default_sumo_x${tag}" "$scale" \
    --controller fixed \
    --input-mode legacy_snapshot
done

for temp in 0.1 0.2 0.4; do
  label="$(temp_label "$temp")"
  for scale in 1.0 1.2 1.5 1.8; do
    tag="$(scale_tag "$scale")"

    run_case "02_9b_base_hf_${label}_x${tag}" "$scale" \
      --controller model \
      --model-backend hf \
      --hf-model-path /root/autodl-tmp/models/Qwen3.5-9B-Base \
      --hf-dtype bfloat16 \
      --prompt-format deepsignal_json \
      --temperature "$temp" \
      --online-control-mode "$BASE_ONLINE_CONTROL_MODE" \
      --model-fail-policy keep_default

    run_case "03_model_fp16_20260519_${label}_x${tag}" "$scale" \
      --controller model \
      --model-backend llama \
      --gguf-path /root/autodl-tmp/models/model-fp16-20260519.gguf \
      --llama-server /root/autodl-tmp/llama.cpp.vendor/build-cuda/bin/llama-server \
      --ngl 99 \
      --threads 8 \
      --ctx-size 4096 \
      --n-predict 512 \
      --timeout-sec 600 \
      --server-startup-sec 240 \
      --temperature "$temp" \
      --model-fail-policy keep_default

    run_case "04_qwen3_4b_base_${BASE_ONLINE_CONTROL_MODE}_${label}_x${tag}" "$scale" \
      --controller model \
      --model-backend hf \
      --hf-model-path /root/autodl-tmp/models/Qwen3-4B \
      --hf-dtype bfloat16 \
      --prompt-format deepsignal_json \
      --hf-use-chat-template \
      --no-hf-chat-template-enable-thinking \
      --hf-skip-special-tokens \
      --temperature "$temp" \
      --online-control-mode "$BASE_ONLINE_CONTROL_MODE" \
      --model-fail-policy keep_default
  done
done

python3 "$PROJECT_ROOT/scripts/summarize_chengdu_peak_matrix.py" "$RUN_ROOT" | tee "$RUN_ROOT/matrix_summary.md"
log_event "SUMMARY_WRITTEN $RUN_ROOT/matrix_summary.csv"
log_event "ALL_DONE $RUN_ROOT"
