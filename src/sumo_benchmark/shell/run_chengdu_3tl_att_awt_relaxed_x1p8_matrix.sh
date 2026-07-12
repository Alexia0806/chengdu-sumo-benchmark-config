#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"
source "$PROJECT_ROOT/src/sumo_benchmark/shell/chengdu_runner_common.sh"

PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
BENCH_ROOT="$(resolve_benchmark_root "$PROJECT_ROOT")"
RUN_ROOT="${RUN_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_3tl_formal_gptoss_qwen4b_gemma12_qwen36_deepsignal4b_x1p2_x1p5_temp02_$(date +%Y%m%d)}"
RUNNER="$PROJECT_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py"
PYTHON_BIN="${PYTHON_BIN:-$TSC_CYCLE_ROOT/.venv/bin/python}"
DEMAND_SCALES="${DEMAND_SCALES:-1.2 1.5}"
TEMPERATURES="${TEMPERATURES:-0.2}"
FORMAL_TARGET_TLS="cluster_4550018629_4550018932 cluster_432429373_5213238455 cluster_1916386555_432429395"
TARGET_TLS="${TARGET_TLS:-${DEFAULT_TARGET_TLS:-$FORMAL_TARGET_TLS}}"
WARMUP_SECONDS="${WARMUP_SECONDS:-300}"
METRIC_SECONDS="${METRIC_SECONDS:-1200}"
TARGET_PEAK_VPH_PER_ROUTE="${TARGET_PEAK_VPH_PER_ROUTE:-240}"
TARGET_PEAK_ROUTES_PER_TL="${TARGET_PEAK_ROUTES_PER_TL:-8}"
TARGET_PEAK_ROUTE_SELECTION="${TARGET_PEAK_ROUTE_SELECTION:-$DEFAULT_TARGET_PEAK_ROUTE_SELECTION}"
TRIPINFO_DRAIN_SECONDS="${TRIPINFO_DRAIN_SECONDS:-600}"
ONLINE_CONTROL_MODE="${ONLINE_CONTROL_MODE:-strict}"
BASE_ONLINE_CONTROL_MODE="${BASE_ONLINE_CONTROL_MODE:-strict}"
ACTION_DELAY_CYCLES="${ACTION_DELAY_CYCLES:-1}"
ALLOW_NONSTANDARD_WINDOW="${ALLOW_NONSTANDARD_WINDOW:-0}"
DEEPSIGNAL_REASONING_MAX_CHARS="${DEEPSIGNAL_REASONING_MAX_CHARS:-160}"
N_PREDICT="${N_PREDICT:-512}"
TIMEOUT_SEC="${TIMEOUT_SEC:-1800}"
HF_DTYPE="${HF_DTYPE:-bfloat16}"
HF_DEVICE_MAP="${HF_DEVICE_MAP:-auto}"
HF_N_PREDICT="${HF_N_PREDICT:-$N_PREDICT}"
HF_TIMEOUT_SEC="${HF_TIMEOUT_SEC:-$TIMEOUT_SEC}"
GPTOSS_N_PREDICT="${GPTOSS_N_PREDICT:-1024}"
GPTOSS_TIMEOUT_SEC="${GPTOSS_TIMEOUT_SEC:-$TIMEOUT_SEC}"
QWEN36_N_PREDICT="${QWEN36_N_PREDICT:-$N_PREDICT}"
QWEN36_TIMEOUT_SEC="${QWEN36_TIMEOUT_SEC:-$TIMEOUT_SEC}"
FP16_N_PREDICT="${FP16_N_PREDICT:-$N_PREDICT}"
FP16_TIMEOUT_SEC="${FP16_TIMEOUT_SEC:-$TIMEOUT_SEC}"
RUN_DEFAULT="${RUN_DEFAULT:-0}"
RUN_GPTOSS20B="${RUN_GPTOSS20B:-1}"
RUN_QWEN4B="${RUN_QWEN4B:-1}"
RUN_GEMMA12B="${RUN_GEMMA12B:-1}"
RUN_QWEN36="${RUN_QWEN36:-1}"
RUN_DEEPSIGNAL4B="${RUN_DEEPSIGNAL4B:-1}"

GPTOSS20B_PATH="${GPTOSS20B_PATH:-$MODELS_ROOT/gpt-oss-20b}"
QWEN4B_PATH="${QWEN4B_PATH:-$MODELS_ROOT/Qwen3-4B}"
GEMMA12B_PATH="${GEMMA12B_PATH:-$MODELS_ROOT/gemma-3-12b-it}"
QWEN36_PATH="${QWEN36_PATH:-$MODELS_ROOT/Qwen3.6-27B}"
DEEPSIGNAL4B_GGUF_PATH="${DEEPSIGNAL4B_GGUF_PATH:-$MODELS_ROOT/model-fp16-20260519.gguf}"

GPTOSS20B_PROMPT_FORMAT="${GPTOSS20B_PROMPT_FORMAT:-deepsignal_solution_first}"
QWEN4B_PROMPT_FORMAT="${QWEN4B_PROMPT_FORMAT:-deepsignal}"
GEMMA12B_PROMPT_FORMAT="${GEMMA12B_PROMPT_FORMAT:-deepsignal}"
QWEN36_PROMPT_FORMAT="${QWEN36_PROMPT_FORMAT:-deepsignal_json}"
DEEPSIGNAL4B_PROMPT_FORMAT="${DEEPSIGNAL4B_PROMPT_FORMAT:-deepsignal}"
TLS_FILE="$RUN_ROOT/chengdu_3tl_tls.csv"
LOG_DIR="$RUN_ROOT/logs"
ORCH_LOG="$LOG_DIR/orchestrator.log"
EXPECTED_TL_COUNT="$(count_words "$TARGET_TLS")"

prepare_run_workspace "$RUN_ROOT" "$LOG_DIR" "$0"
write_tls_file "$TLS_FILE" sumo_llm "$TARGET_TLS"

TARGET_TLS_JSON="$(words_json "$TARGET_TLS")"
DEMAND_SCALES_JSON="$(float_words_json "$DEMAND_SCALES")"
TEMPERATURES_JSON="$(float_words_json "$TEMPERATURES")"
MAX_DEMAND_SCALE="$(max_float_word "$DEMAND_SCALES")"

target_peak_args=()
for tl_id in $TARGET_TLS; do
  target_peak_args+=(--target-peak-tl-id "$tl_id")
done

window_args=()
if [[ "$ALLOW_NONSTANDARD_WINDOW" == "1" ]]; then
  window_args+=(--allow-nonstandard-window)
fi

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

  log_event "START $case_name demand_scale=$demand_scale target_peak_vph_per_route=$TARGET_PEAK_VPH_PER_ROUTE target_peak_routes_per_tl=$TARGET_PEAK_ROUTES_PER_TL target_peak_route_selection=$TARGET_PEAK_ROUTE_SELECTION tripinfo_drain=$TRIPINFO_DRAIN_SECONDS online_control_mode=$ONLINE_CONTROL_MODE base_online_control_mode=$BASE_ONLINE_CONTROL_MODE action_delay_cycles=$ACTION_DELAY_CYCLES n_predict=$N_PREDICT timeout_sec=$TIMEOUT_SEC"
  set +e
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
    "${window_args[@]}" \
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
    --target-peak-route-selection "$TARGET_PEAK_ROUTE_SELECTION" \
    --n-predict "$N_PREDICT" \
    --timeout-sec "$TIMEOUT_SEC" \
    --continue-on-run-error \
    "$@" 2>&1 | tee "$LOG_DIR/$case_name.console.log"
  local rc="${PIPESTATUS[0]}"
  set -e
  if [[ "$rc" -ne 0 ]]; then
    log_event "FAIL $case_name rc=$rc"
    return "$rc"
  fi
  log_event "DONE $case_name"
}

cat > "$RUN_ROOT/experiment_matrix.json" <<JSON
{
  "run_root": "$RUN_ROOT",
  "tls": $TARGET_TLS_JSON,
  "demand_scales": $DEMAND_SCALES_JSON,
  "temperatures": $TEMPERATURES_JSON,
  "metric_window": {
    "warmup_seconds": $WARMUP_SECONDS,
    "metric_seconds": $METRIC_SECONDS,
    "metric_start_second": $WARMUP_SECONDS,
    "metric_end_second": $((WARMUP_SECONDS + METRIC_SECONDS))
  },
  "excluded_model_groups": ["SUMO default", "Qwen3.5 9B", "Phi-4", "first_min_green"],
  "model_groups": [
    "GPT-OSS-20B",
    "Qwen3 4B base no-chat thinking",
    "Gemma 3 12B it no-chat thinking",
    "Qwen3.6 27B",
    "DeepSignal-CyclePlan-4B-V2"
  ],
  "prompt_policy": {
    "base_models_chat_template": "per_model",
    "base_prompt_formats": {
      "gpt_oss_20b": "$GPTOSS20B_PROMPT_FORMAT",
      "qwen3_4b_base": "$QWEN4B_PROMPT_FORMAT",
      "gemma3_12b_it": "$GEMMA12B_PROMPT_FORMAT",
      "qwen36_27b": "$QWEN36_PROMPT_FORMAT",
      "deepsignal_cycleplan_4b_v2": "$DEEPSIGNAL4B_PROMPT_FORMAT"
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
    "route_selection": "$TARGET_PEAK_ROUTE_SELECTION",
    "max_demand_scale": $MAX_DEMAND_SCALE
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
log_event "MATRIX tls=$TARGET_TLS scales=$DEMAND_SCALES temps=$TEMPERATURES run_default=$RUN_DEFAULT run_gptoss20b=$RUN_GPTOSS20B run_qwen4b=$RUN_QWEN4B run_gemma12b=$RUN_GEMMA12B run_qwen36=$RUN_QWEN36 run_deepsignal4b=$RUN_DEEPSIGNAL4B queue_thresholds=10,20,30,40"

run_default_group() {
  for scale in $DEMAND_SCALES; do
    tag="$(scale_tag "$scale")"
    run_case "00_default_sumo_x${tag}" "$scale" \
      --controller fixed \
      --input-mode legacy_snapshot || return $?
  done
}

run_hf_model_group() {
  local group_name="$1"
  local case_prefix="$2"
  local model_path="$3"
  local prompt_format="$4"
  shift 4

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
        --model-fail-policy keep_default \
        "$@" || return $?
    done
  done
  log_event "GROUP_DONE $group_name"
}

run_gptoss20b_group() {
  log_event "GROUP_START gptoss20b model_path=$GPTOSS20B_PATH prompt_format=$GPTOSS20B_PROMPT_FORMAT chat_template=single_user"
  for temp in $TEMPERATURES; do
    label="$(temp_label "$temp")"
    for scale in $DEMAND_SCALES; do
      tag="$(scale_tag "$scale")"
      HF_ATTN_IMPLEMENTATION=eager HF_EXPERTS_IMPLEMENTATION=eager \
      run_case "08_gpt_oss_20b_solution_chattemplate_${BASE_ONLINE_CONTROL_MODE}_${GPTOSS20B_PROMPT_FORMAT}_${label}_x${tag}" "$scale" \
        --controller model \
        --model-backend hf \
        --hf-model-path "$GPTOSS20B_PATH" \
        --hf-dtype auto \
        --hf-device-map "$HF_DEVICE_MAP" \
        --prompt-format "$GPTOSS20B_PROMPT_FORMAT" \
        --hf-use-chat-template \
        --hf-chat-template-message-mode single_user \
        --no-hf-chat-template-enable-thinking \
        --hf-skip-special-tokens \
        --temperature "$temp" \
        --n-predict "$GPTOSS_N_PREDICT" \
        --timeout-sec "$GPTOSS_TIMEOUT_SEC" \
        --online-control-mode "$BASE_ONLINE_CONTROL_MODE" \
        --model-fail-policy keep_default || return $?
    done
  done
  log_event "GROUP_DONE gptoss20b"
}

run_qwen4b_group() {
  run_hf_model_group \
    "qwen4b" \
    "04_qwen3_4b_base_nochat" \
    "$QWEN4B_PATH" \
    "$QWEN4B_PROMPT_FORMAT"
}

run_gemma12b_group() {
  run_hf_model_group \
    "gemma12b" \
    "05_gemma3_12b_it_nochat" \
    "$GEMMA12B_PATH" \
    "$GEMMA12B_PROMPT_FORMAT"
}

run_qwen36_group() {
  log_event "GROUP_START qwen36 model_path=$QWEN36_PATH prompt_format=$QWEN36_PROMPT_FORMAT chat_template=split_system_user"
  for temp in $TEMPERATURES; do
    label="$(temp_label "$temp")"
    for scale in $DEMAND_SCALES; do
      tag="$(scale_tag "$scale")"
      run_case "07_qwen36_27b_chattemplate_${BASE_ONLINE_CONTROL_MODE}_${QWEN36_PROMPT_FORMAT}_${label}_x${tag}" "$scale" \
        --controller model \
        --model-backend hf \
        --hf-model-path "$QWEN36_PATH" \
        --hf-dtype "$HF_DTYPE" \
        --hf-device-map "$HF_DEVICE_MAP" \
        --prompt-format "$QWEN36_PROMPT_FORMAT" \
        --hf-use-chat-template \
        --hf-chat-template-message-mode split_system_user \
        --no-hf-chat-template-enable-thinking \
        --hf-skip-special-tokens \
        --temperature "$temp" \
        --n-predict "$QWEN36_N_PREDICT" \
        --timeout-sec "$QWEN36_TIMEOUT_SEC" \
        --online-control-mode "$BASE_ONLINE_CONTROL_MODE" \
        --model-fail-policy keep_default || return $?
    done
  done
  log_event "GROUP_DONE qwen36"
}

run_deepsignal4b_group() {
  log_event "GROUP_START deepsignal4b model_path=$DEEPSIGNAL4B_GGUF_PATH prompt_format=$DEEPSIGNAL4B_PROMPT_FORMAT backend=llama"
  for temp in $TEMPERATURES; do
    label="$(temp_label "$temp")"
    for scale in $DEMAND_SCALES; do
      tag="$(scale_tag "$scale")"
      run_case "03_deepsignal_cycleplan_4b_v2_${BASE_ONLINE_CONTROL_MODE}_${DEEPSIGNAL4B_PROMPT_FORMAT}_${label}_x${tag}" "$scale" \
        --controller model \
        --model-backend llama \
        --gguf-path "$DEEPSIGNAL4B_GGUF_PATH" \
        --llama-server "$LLAMA_SERVER" \
        --ngl 99 \
        --threads 8 \
        --ctx-size 4096 \
        --server-startup-sec 240 \
        --prompt-format "$DEEPSIGNAL4B_PROMPT_FORMAT" \
        --temperature "$temp" \
        --n-predict "$FP16_N_PREDICT" \
        --timeout-sec "$FP16_TIMEOUT_SEC" \
        --online-control-mode "$BASE_ONLINE_CONTROL_MODE" \
        --model-fail-policy keep_default || return $?
    done
  done
  log_event "GROUP_DONE deepsignal4b"
}

if [[ "$RUN_DEFAULT" == "1" ]]; then
  run_default_group
else
  log_event "SKIP_GROUP default run_default=$RUN_DEFAULT"
fi

if [[ "$RUN_GPTOSS20B" == "1" ]]; then
  run_gptoss20b_group
else
  log_event "SKIP_GROUP gptoss20b run_gptoss20b=$RUN_GPTOSS20B"
fi

if [[ "$RUN_QWEN4B" == "1" ]]; then
  run_qwen4b_group
else
  log_event "SKIP_GROUP qwen4b run_qwen4b=$RUN_QWEN4B"
fi

if [[ "$RUN_GEMMA12B" == "1" ]]; then
  run_gemma12b_group
else
  log_event "SKIP_GROUP gemma12b run_gemma12b=$RUN_GEMMA12B"
fi

if [[ "$RUN_QWEN36" == "1" ]]; then
  run_qwen36_group
else
  log_event "SKIP_GROUP qwen36 run_qwen36=$RUN_QWEN36"
fi

if [[ "$RUN_DEEPSIGNAL4B" == "1" ]]; then
  run_deepsignal4b_group
else
  log_event "SKIP_GROUP deepsignal4b run_deepsignal4b=$RUN_DEEPSIGNAL4B"
fi

"$SYSTEM_PYTHON_BIN" "$PROJECT_ROOT/scripts/summarize_chengdu_peak_matrix.py" "$RUN_ROOT" | tee "$RUN_ROOT/matrix_summary.md"
log_event "SUMMARY_WRITTEN $RUN_ROOT/matrix_summary.csv"
log_event "ALL_DONE $RUN_ROOT"
