#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"

PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
BENCH_ROOT="${DEEPSIGNAL_BENCH_ROOT:-$PROJECT_ROOT/DeepSignal-benchmark}"
RUN_ROOT="${RUN_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_3tl_att_awt_relaxed_nochat_thinking_x1p8_no_x1p0_temp0102_$(date +%Y%m%d)}"
RUNNER="$PROJECT_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py"
PYTHON_BIN="${PYTHON_BIN:-$TSC_CYCLE_ROOT/.venv/bin/python}"
DEMAND_SCALES="${DEMAND_SCALES:-1.2 1.5 1.8}"
TEMPERATURES="${TEMPERATURES:-0.1 0.2}"
TARGET_TLS="${TARGET_TLS:-$DEFAULT_TARGET_TLS}"
WARMUP_SECONDS="${WARMUP_SECONDS:-300}"
METRIC_SECONDS="${METRIC_SECONDS:-1200}"
TARGET_PEAK_VPH_PER_ROUTE="${TARGET_PEAK_VPH_PER_ROUTE:-240}"
TARGET_PEAK_ROUTES_PER_TL="${TARGET_PEAK_ROUTES_PER_TL:-8}"
TRIPINFO_DRAIN_SECONDS="${TRIPINFO_DRAIN_SECONDS:-600}"
ONLINE_CONTROL_MODE="${ONLINE_CONTROL_MODE:-strict}"
BASE_ONLINE_CONTROL_MODE="${BASE_ONLINE_CONTROL_MODE:-strict}"
ACTION_DELAY_CYCLES="${ACTION_DELAY_CYCLES:-1}"
DEEPSIGNAL_REASONING_MAX_CHARS="${DEEPSIGNAL_REASONING_MAX_CHARS:-160}"
N_PREDICT="${N_PREDICT:-512}"
TIMEOUT_SEC="${TIMEOUT_SEC:-1800}"
HF_DTYPE="${HF_DTYPE:-bfloat16}"
HF_DEVICE_MAP="${HF_DEVICE_MAP:-auto}"
PARALLEL_QWEN="${PARALLEL_QWEN:-1}"
RETRY_FAILED_SEQUENTIAL="${RETRY_FAILED_SEQUENTIAL:-1}"
RUN_DEFAULT="${RUN_DEFAULT:-1}"
RUN_QWEN4B="${RUN_QWEN4B:-1}"
RUN_QWEN9B="${RUN_QWEN9B:-1}"
RUN_GEMMA12B="${RUN_GEMMA12B:-1}"
RUN_PHI4="${RUN_PHI4:-0}"

QWEN4B_PROMPT_FORMAT="${QWEN4B_PROMPT_FORMAT:-deepsignal}"
QWEN9B_PROMPT_FORMAT="${QWEN9B_PROMPT_FORMAT:-deepsignal}"
GEMMA12B_PROMPT_FORMAT="${GEMMA12B_PROMPT_FORMAT:-deepsignal}"
PHI4_PROMPT_FORMAT="${PHI4_PROMPT_FORMAT:-deepsignal}"
TLS_FILE="$RUN_ROOT/chengdu_3tl_tls.csv"
LOG_DIR="$RUN_ROOT/logs"
ORCH_LOG="$LOG_DIR/orchestrator.log"
EXPECTED_TL_COUNT="$(wc -w <<< "$TARGET_TLS" | tr -d ' ')"

mkdir -p "$RUN_ROOT" "$LOG_DIR" "$RUN_ROOT/scripts"
cp "$0" "$RUN_ROOT/scripts/$(basename "$0")"
echo "$$" > "$RUN_ROOT/orchestrator.pid"

{
  echo "scenario,tl_id"
  for tl_id in $TARGET_TLS; do
    echo "sumo_llm,$tl_id"
  done
} > "$TLS_FILE"

TARGET_TLS_JSON="$(
  TARGET_TLS="$TARGET_TLS" python3 - <<'PY'
import json
import os

print(json.dumps(os.environ["TARGET_TLS"].split()))
PY
)"

target_peak_args=()
for tl_id in $TARGET_TLS; do
  target_peak_args+=(--target-peak-tl-id "$tl_id")
done

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
  if [[ -f "$out_dir/per_tl.jsonl" ]] && [[ "$(wc -l < "$out_dir/per_tl.jsonl")" -ge "$EXPECTED_TL_COUNT" ]] && [[ ! -s "$out_dir/failures.jsonl" ]]; then
    log_event "SKIP $case_name already_complete"
    return 0
  fi

  log_event "START $case_name demand_scale=$demand_scale target_peak_vph_per_route=$TARGET_PEAK_VPH_PER_ROUTE target_peak_routes_per_tl=$TARGET_PEAK_ROUTES_PER_TL tripinfo_drain=$TRIPINFO_DRAIN_SECONDS online_control_mode=$ONLINE_CONTROL_MODE base_online_control_mode=$BASE_ONLINE_CONTROL_MODE action_delay_cycles=$ACTION_DELAY_CYCLES n_predict=$N_PREDICT timeout_sec=$TIMEOUT_SEC"
  PYTHONUNBUFFERED=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True "$PYTHON_BIN" "$RUNNER" \
    --benchmark-root "$BENCH_ROOT" \
    --sumo-home "$SUMO_HOME" \
    --scenario sumo_llm \
    --tls-file "$TLS_FILE" \
    --output-dir "$out_dir" \
    --input-mode github_official \
    --prompt-format deepsignal \
    --no-prefill \
    --online-control-mode "$ONLINE_CONTROL_MODE" \
    --warmup-seconds "$WARMUP_SECONDS" \
    --metric-seconds "$METRIC_SECONDS" \
    --decision-interval-seconds 60 \
    --action-delay-cycles "$ACTION_DELAY_CYCLES" \
    --min-green 10 \
    --max-green 90 \
    --phase-queue-mode split-overlap \
    --queue-threshold 10 \
    --queue-thresholds 10 20 30 40 \
    --tripinfo-metrics \
    --tripinfo-drain-seconds "$TRIPINFO_DRAIN_SECONDS" \
    --deepsignal-reasoning-max-chars "$DEEPSIGNAL_REASONING_MAX_CHARS" \
    --pred-wait-forecaster rolling_mean \
    --demand-scale "$demand_scale" \
    "${target_peak_args[@]}" \
    --target-peak-vph-per-route "$TARGET_PEAK_VPH_PER_ROUTE" \
    --target-peak-routes-per-tl "$TARGET_PEAK_ROUTES_PER_TL" \
    --n-predict "$N_PREDICT" \
    --timeout-sec "$TIMEOUT_SEC" \
    --continue-on-run-error \
    "$@" 2>&1 | tee "$LOG_DIR/$case_name.console.log"
  log_event "DONE $case_name"
}

cat > "$RUN_ROOT/experiment_matrix.json" <<JSON
{
  "run_root": "$RUN_ROOT",
  "tls": $TARGET_TLS_JSON,
  "demand_scales": [1.2, 1.5, 1.8],
  "temperatures": [0.1, 0.2],
  "metric_window": {
    "warmup_seconds": $WARMUP_SECONDS,
    "metric_seconds": $METRIC_SECONDS,
    "metric_start_second": $WARMUP_SECONDS,
    "metric_end_second": $((WARMUP_SECONDS + METRIC_SECONDS))
  },
  "excluded_model_groups": ["Fine-tuned 9B", "model-fp16-20260519.gguf", "first_min_green"],
  "model_groups": [
    "SUMO default",
    "Qwen3 4B base no-chat thinking",
    "Qwen3.5 9B base no-chat thinking",
    "Gemma 3 12B it no-chat thinking",
    "Phi-4 no-chat thinking"
  ],
  "prompt_policy": {
    "base_models_chat_template": false,
    "base_prompt_formats": {
      "qwen3_4b_base": "$QWEN4B_PROMPT_FORMAT",
      "qwen35_9b_base": "$QWEN9B_PROMPT_FORMAT",
      "gemma3_12b_it": "$GEMMA12B_PROMPT_FORMAT",
      "phi4": "$PHI4_PROMPT_FORMAT"
    },
    "reasoning_max_chars": $DEEPSIGNAL_REASONING_MAX_CHARS,
    "base_lenient_json_extraction": true,
    "prefill": false,
    "online_control_mode": "$ONLINE_CONTROL_MODE",
    "base_online_control_mode": "$BASE_ONLINE_CONTROL_MODE",
    "action_delay_cycles": $ACTION_DELAY_CYCLES,
    "reporting": {
      "strict_format_success_rate": "benchmark protocol compliance",
      "strict_control_usable_rate": "strict protocol + executable plan",
      "relaxed_json_success_rate": "JSON found without requiring protocol tags",
      "relaxed_control_usable_rate": "relaxed JSON already executable",
      "repaired_control_usable_rate": "relaxed JSON executable after safe repair"
    }
  },
  "base_control_policy": {
    "online_control_mode": "$BASE_ONLINE_CONTROL_MODE",
    "fail_policy": "keep_default"
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

log_event "RUN_START run_root=$RUN_ROOT online_control_mode=$ONLINE_CONTROL_MODE"
log_event "MATRIX tls=$TARGET_TLS scales=$DEMAND_SCALES temps=$TEMPERATURES run_default=$RUN_DEFAULT run_qwen4b=$RUN_QWEN4B run_qwen9b=$RUN_QWEN9B run_gemma12b=$RUN_GEMMA12B run_phi4=$RUN_PHI4 chat_template=0 queue_thresholds=10,20,30,40"

run_default_group() {
  for scale in $DEMAND_SCALES; do
    tag="$(scale_tag "$scale")"
    run_case "00_default_sumo_x${tag}" "$scale" \
      --controller fixed \
      --input-mode legacy_snapshot
  done
}

run_hf_model_group() {
  local group_name="$1"
  local case_prefix="$2"
  local model_path="$3"
  local prompt_format="$4"

  log_event "GROUP_START $group_name model_path=$model_path prompt_format=$prompt_format chat_template=0"
  for temp in $TEMPERATURES; do
    label="$(temp_label "$temp")"
    for scale in $DEMAND_SCALES; do
      tag="$(scale_tag "$scale")"
      run_case "${case_prefix}_${BASE_ONLINE_CONTROL_MODE}_${prompt_format}_${label}_x${tag}" "$scale" \
        --controller model \
        --model-backend hf \
        --hf-model-path "$model_path" \
        --hf-dtype "$HF_DTYPE" \
        --hf-device-map "$HF_DEVICE_MAP" \
        --prompt-format "$prompt_format" \
        --no-hf-use-chat-template \
        --no-hf-chat-template-enable-thinking \
        --hf-skip-special-tokens \
        --temperature "$temp" \
        --online-control-mode "$BASE_ONLINE_CONTROL_MODE" \
        --model-fail-policy keep_default
    done
  done
  log_event "GROUP_DONE $group_name"
}

run_qwen4b_group() {
  run_hf_model_group \
    "qwen4b" \
    "04_qwen3_4b_base_nochat" \
    "$MODELS_ROOT/Qwen3-4B" \
    "$QWEN4B_PROMPT_FORMAT"
}

run_qwen9b_group() {
  run_hf_model_group \
    "qwen9b" \
    "02_qwen35_9b_base_nochat" \
    "$MODELS_ROOT/Qwen3.5-9B-Base" \
    "$QWEN9B_PROMPT_FORMAT"
}

run_gemma12b_group() {
  run_hf_model_group \
    "gemma12b" \
    "05_gemma3_12b_it_nochat" \
    "$MODELS_ROOT/gemma-3-12b-it" \
    "$GEMMA12B_PROMPT_FORMAT"
}

run_phi4_group() {
  run_hf_model_group \
    "phi4" \
    "06_phi4_nochat" \
    "$MODELS_ROOT/phi-4" \
    "$PHI4_PROMPT_FORMAT"
}

if [[ "$RUN_DEFAULT" == "1" ]]; then
  run_default_group
else
  log_event "SKIP_GROUP default run_default=$RUN_DEFAULT"
fi

failed_qwen_groups=()
if [[ "$RUN_QWEN4B" == "1" || "$RUN_QWEN9B" == "1" ]]; then
if [[ "$PARALLEL_QWEN" == "1" ]]; then
  log_event "QWEN_PARALLEL_START"
  qwen4b_pid=""
  qwen9b_pid=""
  if [[ "$RUN_QWEN4B" == "1" ]]; then
    (run_qwen4b_group) > "$LOG_DIR/qwen4b.worker.log" 2>&1 &
    qwen4b_pid=$!
    echo "$qwen4b_pid" > "$LOG_DIR/qwen4b.worker.pid"
  else
    log_event "SKIP_GROUP qwen4b run_qwen4b=$RUN_QWEN4B"
  fi

  if [[ "$RUN_QWEN9B" == "1" ]]; then
    (run_qwen9b_group) > "$LOG_DIR/qwen9b.worker.log" 2>&1 &
    qwen9b_pid=$!
    echo "$qwen9b_pid" > "$LOG_DIR/qwen9b.worker.pid"
  else
    log_event "SKIP_GROUP qwen9b run_qwen9b=$RUN_QWEN9B"
  fi

  if [[ -n "$qwen4b_pid" ]]; then
    wait "$qwen4b_pid" || failed_qwen_groups+=("qwen4b")
  fi
  if [[ -n "$qwen9b_pid" ]]; then
    wait "$qwen9b_pid" || failed_qwen_groups+=("qwen9b")
  fi
  log_event "QWEN_PARALLEL_DONE failed_groups=${failed_qwen_groups[*]:-none}"
else
  if [[ "$RUN_QWEN4B" == "1" ]]; then
    run_qwen4b_group || failed_qwen_groups+=("qwen4b")
  else
    log_event "SKIP_GROUP qwen4b run_qwen4b=$RUN_QWEN4B"
  fi
  if [[ "$RUN_QWEN9B" == "1" ]]; then
    run_qwen9b_group || failed_qwen_groups+=("qwen9b")
  else
    log_event "SKIP_GROUP qwen9b run_qwen9b=$RUN_QWEN9B"
  fi
fi
else
  log_event "SKIP_GROUP qwen run_qwen4b=$RUN_QWEN4B run_qwen9b=$RUN_QWEN9B"
fi

if [[ "${#failed_qwen_groups[@]}" -gt 0 && "$RETRY_FAILED_SEQUENTIAL" == "1" ]]; then
  log_event "QWEN_RETRY_SEQUENTIAL failed_groups=${failed_qwen_groups[*]}"
  for failed_group in "${failed_qwen_groups[@]}"; do
    if [[ "$failed_group" == "qwen4b" ]]; then
      run_qwen4b_group
    elif [[ "$failed_group" == "qwen9b" ]]; then
      run_qwen9b_group
    fi
  done
elif [[ "${#failed_qwen_groups[@]}" -gt 0 ]]; then
  log_event "QWEN_FAILED_NO_RETRY failed_groups=${failed_qwen_groups[*]}"
  exit 1
fi

if [[ "$RUN_GEMMA12B" == "1" ]]; then
  run_gemma12b_group
else
  log_event "SKIP_GROUP gemma12b run_gemma12b=$RUN_GEMMA12B"
fi

if [[ "$RUN_PHI4" == "1" ]]; then
  run_phi4_group
else
  log_event "SKIP_GROUP phi4 run_phi4=$RUN_PHI4"
fi

python3 "$PROJECT_ROOT/scripts/summarize_chengdu_peak_matrix.py" "$RUN_ROOT" | tee "$RUN_ROOT/matrix_summary.md"
log_event "SUMMARY_WRITTEN $RUN_ROOT/matrix_summary.csv"
log_event "ALL_DONE $RUN_ROOT"
