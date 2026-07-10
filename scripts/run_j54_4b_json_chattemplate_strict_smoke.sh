#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/root/autodl-tmp/tsc-cycle-benchmark}"
BENCH_ROOT="$PROJECT_ROOT/DeepSignal-benchmark"
RUN_ROOT="${RUN_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/j54_qwen3_4b_base_json_chattemplate_strict_smoke_20260622}"
RUNNER="$PROJECT_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py"
SUMMARIZER="$PROJECT_ROOT/scripts/summarize_chengdu_peak_matrix.py"
PYTHON_BIN="${PYTHON_BIN:-/root/autodl-tmp/TSC_CYCLE_v1/.venv/bin/python}"
MODEL_PATH="${MODEL_PATH:-/root/autodl-tmp/models/Qwen3-4B}"
TARGET_PEAK_VPH_PER_ROUTE="${TARGET_PEAK_VPH_PER_ROUTE:-240}"
TARGET_PEAK_ROUTES_PER_TL="${TARGET_PEAK_ROUTES_PER_TL:-8}"
TRIPINFO_DRAIN_SECONDS="${TRIPINFO_DRAIN_SECONDS:-600}"
TLS_FILE="$RUN_ROOT/j54_tls.csv"
LOG_DIR="$RUN_ROOT/logs"
ORCH_LOG="$LOG_DIR/orchestrator.log"

mkdir -p "$RUN_ROOT" "$LOG_DIR" "$RUN_ROOT/scripts"
cp "$0" "$RUN_ROOT/scripts/$(basename "$0")" 2>/dev/null || true
echo "$$" > "$RUN_ROOT/orchestrator.pid"

cat > "$TLS_FILE" <<'CSV'
scenario,tl_id
sumo_llm,J54
CSV

log_event() {
  printf '[%s] %s\n' "$(date -Is)" "$1" | tee -a "$ORCH_LOG"
}

temp_label() {
  case "$1" in
    0.0) echo temp00 ;;
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
  local temperature="$3"
  local out_dir="$RUN_ROOT/$case_name"
  mkdir -p "$out_dir"
  if [[ -f "$out_dir/per_tl.jsonl" ]] && [[ "$(wc -l < "$out_dir/per_tl.jsonl")" -ge 1 ]] && [[ ! -s "$out_dir/failures.jsonl" ]]; then
    log_event "SKIP $case_name already_complete"
    return 0
  fi

  log_event "START $case_name demand_scale=$demand_scale temperature=$temperature"
  PYTHONUNBUFFERED=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True "$PYTHON_BIN" "$RUNNER" \
    --benchmark-root "$BENCH_ROOT" \
    --sumo-home /usr/share/sumo \
    --scenario sumo_llm \
    --tls-file "$TLS_FILE" \
    --output-dir "$out_dir" \
    --input-mode github_official \
    --prompt-format deepsignal_json \
    --no-prefill \
    --online-control-mode strict \
    --json-stop-after-first \
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
    --target-peak-vph-per-route "$TARGET_PEAK_VPH_PER_ROUTE" \
    --target-peak-routes-per-tl "$TARGET_PEAK_ROUTES_PER_TL" \
    --continue-on-run-error \
    --controller model \
    --model-backend hf \
    --hf-model-path "$MODEL_PATH" \
    --hf-dtype bfloat16 \
    --hf-use-chat-template \
    --no-hf-chat-template-enable-thinking \
    --temperature "$temperature" \
    --n-predict 512 \
    --model-fail-policy keep_default \
    2>&1 | tee "$LOG_DIR/$case_name.console.log"
  local rc=${PIPESTATUS[0]}
  log_event "DONE $case_name rc=$rc"
  return "$rc"
}

cat > "$RUN_ROOT/experiment_matrix.json" <<JSON
{
  "run_root": "$RUN_ROOT",
  "purpose": "J54 Qwen3-4B base smoke test with JSON-only prompt, HF chat template, first-JSON strict parsing, and strict online control",
  "tls": ["J54"],
  "demand_scales": [1.0, 1.5],
  "temperatures": [0.0, 0.1],
  "model": "$MODEL_PATH",
  "prompt_format": "deepsignal_json",
  "online_control_mode": "strict",
  "json_stop_after_first": true,
  "hf_use_chat_template": true,
  "hf_chat_template_enable_thinking": false,
  "model_fail_policy": "keep_default",
  "n_predict": 256
}
JSON

log_event "RUN_START run_root=$RUN_ROOT model=$MODEL_PATH prompt_format=deepsignal_json online_control_mode=strict json_stop_after_first=true hf_use_chat_template=true fail_policy=keep_default"
status=0
for temp in 0.0 0.1; do
  label="$(temp_label "$temp")"
  for scale in 1.0 1.5; do
    tag="$(scale_tag "$scale")"
    run_case "04_qwen3_4b_base_json_chattemplate_strict_${label}_x${tag}" "$scale" "$temp" || status=$?
  done
done

"$PYTHON_BIN" "$SUMMARIZER" "$RUN_ROOT" | tee "$RUN_ROOT/matrix_summary.md"
summary_rc=${PIPESTATUS[0]}
[[ "$summary_rc" -eq 0 ]] || status="$summary_rc"
log_event "SUMMARY_WRITTEN $RUN_ROOT/matrix_summary.csv"
log_event "ALL_DONE rc=$status run_root=$RUN_ROOT"
exit "$status"
