#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/env_defaults.sh"

PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
RUNNER="$PROJECT_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py"
PYTHON_BIN="${PYTHON_BIN:-$SYSTEM_PYTHON_BIN}"
BENCH_ROOT="${BENCH_ROOT:-$PROJECT_ROOT/chengdu_benchmark}"
MODEL_DIR="$MODELS_ROOT/gpt-oss-20b"
RUN_ROOT="$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_2tl_J54_432452987_unbalanced_x1p5_gpt_oss_20b_temp0102_20260625"
LOG_DIR="$RUN_ROOT/logs"
TLS_FILE="$RUN_ROOT/chengdu_2tl_tls.csv"
ORCH_LOG="$LOG_DIR/orchestrator.log"
TARGET_PEAK_VPH_PER_ROUTE=480
TARGET_PEAK_ROUTES_PER_TL=2
TRIPINFO_DRAIN_SECONDS=600
N_PREDICT=384
TIMEOUT_SEC=1200

mkdir -p "$RUN_ROOT" "$LOG_DIR"
echo "$$" > "$RUN_ROOT/orchestrator.pid"
cat > "$TLS_FILE" <<'CSV'
scenario,tl_id
sumo_llm,J54
sumo_llm,432452987
CSV

log_event() {
  printf '[%s] %s\n' "$(date -Is)" "$1" | tee -a "$ORCH_LOG"
}

run_case() {
  local temp="$1"
  local temp_tag="${temp/./}"
  local case_name="gpt_oss_20b_temp${temp_tag}_unbalanced_x1p5"
  local out_dir="$RUN_ROOT/$case_name"
  mkdir -p "$out_dir"

  if [[ -f "$out_dir/per_tl.jsonl" ]] && [[ "$(wc -l < "$out_dir/per_tl.jsonl")" -ge 2 ]] && [[ ! -s "$out_dir/failures.jsonl" ]]; then
    log_event "SKIP $case_name already_complete"
    return 0
  fi

  log_event "START $case_name temp=$temp"
  PYTHONUNBUFFERED=1 \
  PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  HF_ATTN_IMPLEMENTATION=eager \
  HF_EXPERTS_IMPLEMENTATION=eager \
  "$PYTHON_BIN" "$RUNNER" \
    --benchmark-root "$BENCH_ROOT" \
    --sumo-home "$SUMO_HOME" \
    --scenario sumo_llm \
    --tls-file "$TLS_FILE" \
    --output-dir "$out_dir" \
    --input-mode github_official \
    --prompt-format deepsignal_json \
    --no-prefill \
    --online-control-mode repaired_directional \
    --directional-control-min-delta-sec 5 \
    --directional-control-saturation-gap 0.30 \
    --directional-control-green-tolerance-sec 10 \
    --warmup-seconds 300 \
    --metric-seconds 1200 \
    --decision-interval-seconds 60 \
    --action-delay-cycles 1 \
    --min-green 10 \
    --max-green 90 \
    --phase-queue-mode split-overlap \
    --queue-threshold 10 \
    --queue-thresholds 10 20 30 40 \
    --tripinfo-metrics \
    --tripinfo-drain-seconds "$TRIPINFO_DRAIN_SECONDS" \
    --deepsignal-reasoning-max-chars 160 \
    --pred-wait-forecaster rolling_mean \
    --demand-scale 1.5 \
    --target-peak-tl-id J54 \
    --target-peak-tl-id 432452987 \
    --target-peak-vph-per-route "$TARGET_PEAK_VPH_PER_ROUTE" \
    --target-peak-routes-per-tl "$TARGET_PEAK_ROUTES_PER_TL" \
    --continue-on-run-error \
    --controller model \
    --model-backend hf \
    --hf-model-path "$MODEL_DIR" \
    --hf-dtype auto \
    --hf-device-map auto \
    --hf-use-chat-template \
    --hf-chat-template-message-mode single_user \
    --no-hf-chat-template-enable-thinking \
    --hf-skip-special-tokens \
    --temperature "$temp" \
    --n-predict "$N_PREDICT" \
    --timeout-sec "$TIMEOUT_SEC" \
    --model-fail-policy keep_default \
    2>&1 | tee "$LOG_DIR/$case_name.console.log"
  local rc=${PIPESTATUS[0]}
  log_event "DONE $case_name rc=$rc"
  return "$rc"
}

cat > "$RUN_ROOT/experiment_matrix.json" <<JSON
{
  "run_root": "$RUN_ROOT",
  "model_group": "gpt_oss_20b",
  "tls": ["J54", "432452987"],
  "scenario_variant": "unbalanced_target_peak",
  "demand_scales": [1.5],
  "temperatures": [0.1, 0.2],
  "window_dashboard": {"pressure_start": 300, "pressure_end": 900, "official_start": 300, "official_end": 1500},
  "target_peak": {"vph_per_route_base": $TARGET_PEAK_VPH_PER_ROUTE, "routes_per_tl": $TARGET_PEAK_ROUTES_PER_TL},
  "prompt_format": "deepsignal_json",
  "online_control_mode": "repaired_directional",
  "hf_chat_template_message_mode": "single_user",
  "hf_attn_implementation": "eager",
  "hf_experts_implementation": "eager",
  "n_predict": $N_PREDICT,
  "timeout_sec": $TIMEOUT_SEC,
  "queue_thresholds": [10, 20, 30, 40],
  "tripinfo": {"enabled": true, "drain_seconds": $TRIPINFO_DRAIN_SECONDS}
}
JSON

log_event "RUN_START run_root=$RUN_ROOT"
status=0
run_case 0.1 || status=$?
run_case 0.2 || status=$?
"$PYTHON_BIN" "$PROJECT_ROOT/scripts/summarize_chengdu_peak_matrix.py" "$RUN_ROOT" | tee "$RUN_ROOT/matrix_summary.md" || status=$?
log_event "ALL_DONE rc=$status run_root=$RUN_ROOT"
exit "$status"
