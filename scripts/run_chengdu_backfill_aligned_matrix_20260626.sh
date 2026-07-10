#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"

PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
if [[ -z "${BENCH_ROOT:-}" ]]; then
  if [[ -d "$PROJECT_ROOT/chengdu_benchmark" ]]; then
    BENCH_ROOT="${BENCH_ROOT:-$PROJECT_ROOT/chengdu_benchmark}"
  else
    BENCH_ROOT="${DEEPSIGNAL_BENCH_ROOT:-$PROJECT_ROOT/DeepSignal-benchmark}"
  fi
fi
RUNNER="${RUNNER:-$PROJECT_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py}"
SUMMARIZER="${SUMMARIZER:-$PROJECT_ROOT/scripts/summarize_chengdu_peak_matrix.py}"
PYTHON_BIN="${PYTHON_BIN:-$TSC_CYCLE_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

RUN_ROOT="${RUN_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_backfill_aligned_20260626_$(date +%Y%m%dT%H%M%S)}"
LOG_DIR="$RUN_ROOT/logs"
TLS_DIR="$RUN_ROOT/tls"
ORCH_LOG="$LOG_DIR/orchestrator.log"

TARGET_TLS="${TARGET_TLS:-$DEFAULT_TARGET_TLS}"
UNBALANCED_X15_TLS="${UNBALANCED_X15_TLS:-$DEFAULT_UNBALANCED_X15_TLS}"
RUN_UNBALANCED_X15_FULL3TL="${RUN_UNBALANCED_X15_FULL3TL:-0}"
TEMPERATURES="${TEMPERATURES:-0.1 0.2}"
SCENARIOS="${SCENARIOS:-unbalanced_x1p5 balanced_x1p5 balanced_x1p2 unbalanced_x1p2}"
MODEL_KEYS="${MODEL_KEYS:-fp16 phi4 gemma12 qwen9b qwen4b}"
RUN_DEFAULT="${RUN_DEFAULT:-0}"
DEFAULT_REUSE_POLICY="${DEFAULT_REUSE_POLICY:-prefer_existing}"
DEFAULT_REUSE_ROOTS="${DEFAULT_REUSE_ROOTS:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_3tl_min10_targetpeak_20260617 $PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_3tl_unbalanced_peak_x1p5_temp01_20260617 $PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_3tl_unbalanced_peak_x1p5_temp02_20260617 $PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_3tl_unbalanced_peak_x1p5_temp04_20260617}"
DRY_RUN="${DRY_RUN:-0}"
MISSING_MODEL_POLICY="${MISSING_MODEL_POLICY:-skip}"

WARMUP_SECONDS="${WARMUP_SECONDS:-300}"
METRIC_SECONDS="${METRIC_SECONDS:-1200}"
DECISION_INTERVAL_SECONDS="${DECISION_INTERVAL_SECONDS:-60}"
TRIPINFO_DRAIN_SECONDS="${TRIPINFO_DRAIN_SECONDS:-600}"
ACTION_DELAY_CYCLES="${ACTION_DELAY_CYCLES:-1}"
MIN_GREEN="${MIN_GREEN:-10}"
MAX_GREEN="${MAX_GREEN:-90}"
DEEPSIGNAL_REASONING_MAX_CHARS="${DEEPSIGNAL_REASONING_MAX_CHARS:-160}"
QUEUE_THRESHOLDS="${QUEUE_THRESHOLDS:-10 20 30 40}"
TARGET_PEAK_ROUTE_SELECTION="${TARGET_PEAK_ROUTE_SELECTION:-$DEFAULT_TARGET_PEAK_ROUTE_SELECTION}"

HF_DTYPE="${HF_DTYPE:-bfloat16}"
HF_DEVICE_MAP="${HF_DEVICE_MAP:-auto}"
HF_N_PREDICT="${HF_N_PREDICT:-512}"
HF_TIMEOUT_SEC="${HF_TIMEOUT_SEC:-1800}"
QWEN36_N_PREDICT="${QWEN36_N_PREDICT:-512}"
QWEN36_TIMEOUT_SEC="${QWEN36_TIMEOUT_SEC:-2400}"
PHI4_N_PREDICT="${PHI4_N_PREDICT:-512}"
PHI4_TIMEOUT_SEC="${PHI4_TIMEOUT_SEC:-600}"
FP16_N_PREDICT="${FP16_N_PREDICT:-512}"
FP16_TIMEOUT_SEC="${FP16_TIMEOUT_SEC:-1800}"

QWEN36_PATH="${QWEN36_PATH:-$MODELS_ROOT/Qwen3.6-27B}"
QWEN4B_PATH="${QWEN4B_PATH:-$MODELS_ROOT/Qwen3-4B}"
QWEN9B_PATH="${QWEN9B_PATH:-$MODELS_ROOT/Qwen3.5-9B-Base}"
GEMMA12_PATH="${GEMMA12_PATH:-$MODELS_ROOT/gemma-3-12b-it}"
PHI4_PATH="${PHI4_PATH:-$MODELS_ROOT/phi-4}"
FP16_GGUF_PATH="${FP16_GGUF_PATH:-$MODELS_ROOT/model-fp16-20260519.gguf}"
LLAMA_SERVER="${LLAMA_SERVER:-$LLAMA_CPP_ROOT/build-cuda/bin/llama-server}"

mkdir -p "$RUN_ROOT" "$LOG_DIR" "$TLS_DIR" "$RUN_ROOT/scripts"
cp "$0" "$RUN_ROOT/scripts/$(basename "$0")" 2>/dev/null || true
echo "$$" > "$RUN_ROOT/orchestrator.pid"

log_event() {
  local msg="$1"
  printf '[%s] %s\n' "$(date -Is)" "$msg" | tee -a "$ORCH_LOG"
}

scale_tag() {
  echo "${1/./p}"
}

temp_label() {
  case "$1" in
    0.1) echo temp01 ;;
    0.2) echo temp02 ;;
    0.4) echo temp04 ;;
    *) echo "temp${1/./p}" ;;
  esac
}

scenario_label() {
  case "$1" in
    balanced_x1p5) echo "balanced" ;;
    balanced_x1p2) echo "balanced" ;;
    unbalanced_x1p5)
      if [[ "$RUN_UNBALANCED_X15_FULL3TL" == "1" ]]; then
        echo "unbalanced_full3tl"
      else
        echo "unbalanced_subset"
      fi
      ;;
    unbalanced_x1p2) echo "unbalanced" ;;
    *) echo "$1" ;;
  esac
}

scenario_scale() {
  case "$1" in
    balanced_x1p5|unbalanced_x1p5) echo "1.5" ;;
    balanced_x1p2|unbalanced_x1p2) echo "1.2" ;;
    *) log_event "ERROR unknown_scenario=$1"; return 2 ;;
  esac
}

scenario_peak_vph() {
  case "$1" in
    balanced_*) echo "240" ;;
    unbalanced_*) echo "480" ;;
    *) log_event "ERROR unknown_scenario=$1"; return 2 ;;
  esac
}

scenario_routes_per_tl() {
  case "$1" in
    balanced_*) echo "8" ;;
    unbalanced_*) echo "2" ;;
    *) log_event "ERROR unknown_scenario=$1"; return 2 ;;
  esac
}

scenario_tls() {
  case "$1" in
    unbalanced_x1p5)
      if [[ "$RUN_UNBALANCED_X15_FULL3TL" == "1" ]]; then
        echo "$TARGET_TLS"
      else
        echo "$UNBALANCED_X15_TLS"
      fi
      ;;
    *) echo "$TARGET_TLS" ;;
  esac
}

write_tls_file() {
  local scenario_key="$1"
  local tls_list="$2"
  local tls_file="$TLS_DIR/${scenario_key}.csv"
  {
    echo "scenario,tl_id"
    for tl_id in $tls_list; do
      echo "sumo_llm,$tl_id"
    done
  } > "$tls_file"
  echo "$tls_file"
}

queue_threshold_args=()
for threshold in $QUEUE_THRESHOLDS; do
  queue_threshold_args+=("$threshold")
done

write_experiment_matrix() {
  SCENARIOS="$SCENARIOS" MODEL_KEYS="$MODEL_KEYS" TARGET_TLS="$TARGET_TLS" \
  UNBALANCED_X15_TLS="$UNBALANCED_X15_TLS" RUN_UNBALANCED_X15_FULL3TL="$RUN_UNBALANCED_X15_FULL3TL" \
  TEMPERATURES="$TEMPERATURES" RUN_DEFAULT="$RUN_DEFAULT" DRY_RUN="$DRY_RUN" \
  DEFAULT_REUSE_POLICY="$DEFAULT_REUSE_POLICY" DEFAULT_REUSE_ROOTS="$DEFAULT_REUSE_ROOTS" \
  RUN_ROOT="$RUN_ROOT" WARMUP_SECONDS="$WARMUP_SECONDS" METRIC_SECONDS="$METRIC_SECONDS" \
  TRIPINFO_DRAIN_SECONDS="$TRIPINFO_DRAIN_SECONDS" QUEUE_THRESHOLDS="$QUEUE_THRESHOLDS" \
  TARGET_PEAK_ROUTE_SELECTION="$TARGET_PEAK_ROUTE_SELECTION" \
  "$PYTHON_BIN" - <<'PY' > "$RUN_ROOT/experiment_matrix.json"
import json
import os

payload = {
    "run_root": os.environ["RUN_ROOT"],
    "purpose": "20260626 aligned backfill: missing unbalanced x1.5 TL plus balanced/unbalanced x1.2/x1.5 3-TL cases",
    "scenarios": os.environ["SCENARIOS"].split(),
    "model_keys": os.environ["MODEL_KEYS"].split(),
    "run_default": os.environ["RUN_DEFAULT"] == "1",
    "default_reuse_policy": os.environ["DEFAULT_REUSE_POLICY"],
    "default_reuse_roots": os.environ["DEFAULT_REUSE_ROOTS"].split(),
    "dry_run": os.environ["DRY_RUN"] == "1",
    "tls": os.environ["TARGET_TLS"].split(),
    "unbalanced_x1p5_tls": (
        os.environ["TARGET_TLS"].split()
        if os.environ["RUN_UNBALANCED_X15_FULL3TL"] == "1"
        else os.environ["UNBALANCED_X15_TLS"].split()
    ),
    "temperatures": [float(x) for x in os.environ["TEMPERATURES"].split()],
    "scenario_definitions": {
        "balanced": {
            "target_peak_vph_per_route_base": 240,
            "target_peak_routes_per_tl": 8,
            "meaning": "distributed target peak; project legacy balanced_target_peak protocol",
        },
        "unbalanced": {
            "target_peak_vph_per_route_base": 480,
            "target_peak_routes_per_tl": 2,
            "meaning": "concentrated target peak; project legacy unbalanced_peak protocol",
        },
    },
    "metric_window": {
        "warmup_seconds": int(os.environ["WARMUP_SECONDS"]),
        "metric_seconds": int(os.environ["METRIC_SECONDS"]),
        "metric_start_second": int(os.environ["WARMUP_SECONDS"]),
        "metric_end_second": int(os.environ["WARMUP_SECONDS"]) + int(os.environ["METRIC_SECONDS"]),
        "tripinfo_drain_seconds": int(os.environ["TRIPINFO_DRAIN_SECONDS"]),
    },
    "queue_thresholds": [int(x) for x in os.environ["QUEUE_THRESHOLDS"].split()],
    "target_peak_route_selection": os.environ["TARGET_PEAK_ROUTE_SELECTION"],
    "notes": [
        "SUMO default is not rerun by default; downstream aggregation should reuse matching existing default rows first.",
        "Unbalanced x1.5 defaults to the missing TL only, so it is a per-TL backfill for alignment with existing 2-TL runs.",
        "Set RUN_UNBALANCED_X15_FULL3TL=1 to rerun unbalanced x1.5 as a clean same-run 3-TL aggregate.",
        "Official metrics use 300-1500s; dashboard may also compute 300-900s pressure diagnostics from prediction_inputs.jsonl.",
    ],
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
}

need_model_gpu() {
  [[ -n "$(echo "$MODEL_KEYS" | xargs)" ]]
}

require_gpu_if_needed() {
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  if ! need_model_gpu; then
    return 0
  fi
  if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi -L >/dev/null 2>&1; then
    log_event "NO_GPU_FOR_MODEL_RUNS model_keys='$MODEL_KEYS' hint='Use MODEL_KEYS=\"\" to run SUMO default only in no-GPU mode, or rerun after GPU is attached.'"
    exit 64
  fi
}

skip_or_fail_missing() {
  local model_key="$1"
  local path="$2"
  if [[ "$MISSING_MODEL_POLICY" == "skip" ]]; then
    log_event "SKIP_MODEL model=$model_key reason=missing_path path=$path"
    return 0
  fi
  log_event "ERROR model=$model_key missing_path=$path"
  return 66
}

ensure_path() {
  local model_key="$1"
  local path="$2"
  local kind="${3:-dir}"
  if [[ "$kind" == "file" ]]; then
    [[ -f "$path" ]] || { skip_or_fail_missing "$model_key" "$path"; return 1; }
  else
    [[ -d "$path" ]] || { skip_or_fail_missing "$model_key" "$path"; return 1; }
  fi
  return 0
}

run_case() {
  local scenario_key="$1"
  local scenario_name="$2"
  local case_name="$3"
  local demand_scale="$4"
  local target_peak_vph_per_route="$5"
  local target_peak_routes_per_tl="$6"
  local tls_list="$7"
  shift 7

  local out_dir="$RUN_ROOT/$case_name"
  local tls_file expected_tl_count
  tls_file="$(write_tls_file "$scenario_key" "$tls_list")"
  expected_tl_count="$(wc -w <<< "$tls_list" | tr -d ' ')"
  mkdir -p "$out_dir"

  if [[ -f "$out_dir/per_tl.jsonl" ]] && [[ "$(wc -l < "$out_dir/per_tl.jsonl")" -ge "$expected_tl_count" ]] && [[ ! -s "$out_dir/failures.jsonl" ]]; then
    log_event "SKIP $case_name already_complete"
    return 0
  fi

  target_peak_args=()
  for tl_id in $tls_list; do
    target_peak_args+=(--target-peak-tl-id "$tl_id")
  done

  log_event "START $case_name scenario=$scenario_name demand_scale=$demand_scale tls='$tls_list' target_peak_vph_per_route=$target_peak_vph_per_route target_peak_routes_per_tl=$target_peak_routes_per_tl target_peak_route_selection=$TARGET_PEAK_ROUTE_SELECTION dry_run=$DRY_RUN"
  if [[ "$DRY_RUN" == "1" ]]; then
    log_event "DRY_RUN $case_name command='$PYTHON_BIN $RUNNER ...'"
    return 0
  fi

  PYTHONUNBUFFERED=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True "$PYTHON_BIN" "$RUNNER" \
    --benchmark-root "$BENCH_ROOT" \
    --sumo-home "$SUMO_HOME" \
    --scenario sumo_llm \
    --tls-file "$tls_file" \
    --output-dir "$out_dir" \
    --warmup-seconds "$WARMUP_SECONDS" \
    --metric-seconds "$METRIC_SECONDS" \
    --decision-interval-seconds "$DECISION_INTERVAL_SECONDS" \
    --action-delay-cycles "$ACTION_DELAY_CYCLES" \
    --min-green "$MIN_GREEN" \
    --max-green "$MAX_GREEN" \
    --phase-queue-mode split-overlap \
    --queue-threshold 10 \
    --queue-thresholds "${queue_threshold_args[@]}" \
    --tripinfo-metrics \
    --tripinfo-drain-seconds "$TRIPINFO_DRAIN_SECONDS" \
    --deepsignal-reasoning-max-chars "$DEEPSIGNAL_REASONING_MAX_CHARS" \
    --pred-wait-forecaster rolling_mean \
    --demand-scale "$demand_scale" \
    "${target_peak_args[@]}" \
    --target-peak-vph-per-route "$target_peak_vph_per_route" \
    --target-peak-routes-per-tl "$target_peak_routes_per_tl" \
    --target-peak-route-selection "$TARGET_PEAK_ROUTE_SELECTION" \
    --continue-on-run-error \
    "$@" 2>&1 | tee "$LOG_DIR/$case_name.console.log"
  log_event "DONE $case_name"
}

run_default_case() {
  local scenario_key="$1"
  local scenario_name="$2"
  local scale="$3"
  local vph="$4"
  local routes="$5"
  local tls_list="$6"
  local tag
  tag="$(scale_tag "$scale")"
  run_case "$scenario_key" "$scenario_name" "00_default_sumo_${scenario_name}_x${tag}" "$scale" "$vph" "$routes" "$tls_list" \
    --input-mode legacy_snapshot \
    --prompt-format deepsignal \
    --controller fixed
}

run_model_case() {
  local model_key="$1"
  local scenario_key="$2"
  local scenario_name="$3"
  local scale="$4"
  local vph="$5"
  local routes="$6"
  local tls_list="$7"
  local temp="$8"
  local tag label
  tag="$(scale_tag "$scale")"
  label="$(temp_label "$temp")"

  case "$model_key" in
    qwen36)
      ensure_path "$model_key" "$QWEN36_PATH" dir || return 0
      run_case "$scenario_key" "$scenario_name" "07_qwen36_27b_deepsignal_json_chattemplate_strict_${scenario_name}_${label}_x${tag}" "$scale" "$vph" "$routes" "$tls_list" \
        --input-mode github_official \
        --prompt-format deepsignal_json \
        --no-prefill \
        --online-control-mode strict \
        --n-predict "$QWEN36_N_PREDICT" \
        --timeout-sec "$QWEN36_TIMEOUT_SEC" \
        --controller model \
        --model-backend hf \
        --hf-model-path "$QWEN36_PATH" \
        --hf-dtype "$HF_DTYPE" \
        --hf-device-map "$HF_DEVICE_MAP" \
        --hf-use-chat-template \
        --hf-chat-template-message-mode split_system_user \
        --no-hf-chat-template-enable-thinking \
        --hf-skip-special-tokens \
        --temperature "$temp" \
        --model-fail-policy keep_default
      ;;
    fp16)
      ensure_path "$model_key" "$FP16_GGUF_PATH" file || return 0
      ensure_path "$model_key" "$LLAMA_SERVER" file || return 0
      run_case "$scenario_key" "$scenario_name" "03_model_fp16_20260519_${scenario_name}_${label}_x${tag}" "$scale" "$vph" "$routes" "$tls_list" \
        --input-mode github_official \
        --prompt-format deepsignal \
        --no-prefill \
        --online-control-mode strict \
        --n-predict "$FP16_N_PREDICT" \
        --timeout-sec "$FP16_TIMEOUT_SEC" \
        --controller model \
        --model-backend llama \
        --gguf-path "$FP16_GGUF_PATH" \
        --llama-server "$LLAMA_SERVER" \
        --ngl 99 \
        --threads 8 \
        --ctx-size 4096 \
        --server-startup-sec 240 \
        --temperature "$temp" \
        --model-fail-policy keep_default
      ;;
    phi4)
      ensure_path "$model_key" "$PHI4_PATH" dir || return 0
      run_case "$scenario_key" "$scenario_name" "06_phi4_deepsignal_json_chattemplate_strict_${scenario_name}_${label}_x${tag}" "$scale" "$vph" "$routes" "$tls_list" \
        --input-mode github_official \
        --prompt-format deepsignal_json \
        --no-prefill \
        --online-control-mode strict \
        --n-predict "$PHI4_N_PREDICT" \
        --timeout-sec "$PHI4_TIMEOUT_SEC" \
        --controller model \
        --model-backend hf \
        --hf-model-path "$PHI4_PATH" \
        --hf-dtype "$HF_DTYPE" \
        --hf-device-map "$HF_DEVICE_MAP" \
        --hf-use-chat-template \
        --hf-chat-template-message-mode split_system_user \
        --no-hf-chat-template-enable-thinking \
        --hf-skip-special-tokens \
        --temperature "$temp" \
        --model-fail-policy keep_default
      ;;
    gemma12)
      ensure_path "$model_key" "$GEMMA12_PATH" dir || return 0
      run_case "$scenario_key" "$scenario_name" "05_gemma3_12b_it_nochat_strict_deepsignal_${scenario_name}_${label}_x${tag}" "$scale" "$vph" "$routes" "$tls_list" \
        --input-mode github_official \
        --prompt-format deepsignal \
        --no-prefill \
        --online-control-mode strict \
        --n-predict "$HF_N_PREDICT" \
        --timeout-sec "$HF_TIMEOUT_SEC" \
        --controller model \
        --model-backend hf \
        --hf-model-path "$GEMMA12_PATH" \
        --hf-dtype "$HF_DTYPE" \
        --hf-device-map "$HF_DEVICE_MAP" \
        --no-hf-use-chat-template \
        --no-hf-chat-template-enable-thinking \
        --hf-skip-special-tokens \
        --temperature "$temp" \
        --model-fail-policy keep_default
      ;;
    qwen9b)
      ensure_path "$model_key" "$QWEN9B_PATH" dir || return 0
      run_case "$scenario_key" "$scenario_name" "02_qwen35_9b_base_nochat_strict_deepsignal_${scenario_name}_${label}_x${tag}" "$scale" "$vph" "$routes" "$tls_list" \
        --input-mode github_official \
        --prompt-format deepsignal \
        --no-prefill \
        --online-control-mode strict \
        --n-predict "$HF_N_PREDICT" \
        --timeout-sec "$HF_TIMEOUT_SEC" \
        --controller model \
        --model-backend hf \
        --hf-model-path "$QWEN9B_PATH" \
        --hf-dtype "$HF_DTYPE" \
        --hf-device-map "$HF_DEVICE_MAP" \
        --no-hf-use-chat-template \
        --no-hf-chat-template-enable-thinking \
        --hf-skip-special-tokens \
        --temperature "$temp" \
        --model-fail-policy keep_default
      ;;
    qwen4b)
      ensure_path "$model_key" "$QWEN4B_PATH" dir || return 0
      run_case "$scenario_key" "$scenario_name" "04_qwen3_4b_base_nochat_strict_deepsignal_${scenario_name}_${label}_x${tag}" "$scale" "$vph" "$routes" "$tls_list" \
        --input-mode github_official \
        --prompt-format deepsignal \
        --no-prefill \
        --online-control-mode strict \
        --n-predict "$HF_N_PREDICT" \
        --timeout-sec "$HF_TIMEOUT_SEC" \
        --controller model \
        --model-backend hf \
        --hf-model-path "$QWEN4B_PATH" \
        --hf-dtype "$HF_DTYPE" \
        --hf-device-map "$HF_DEVICE_MAP" \
        --no-hf-use-chat-template \
        --no-hf-chat-template-enable-thinking \
        --hf-skip-special-tokens \
        --temperature "$temp" \
        --model-fail-policy keep_default
      ;;
    *)
      log_event "ERROR unknown_model_key=$model_key"
      return 2
      ;;
  esac
}

write_experiment_matrix
require_gpu_if_needed

log_event "RUN_START run_root=$RUN_ROOT bench_root=$BENCH_ROOT dry_run=$DRY_RUN scenarios='$SCENARIOS' model_keys='$MODEL_KEYS' run_default=$RUN_DEFAULT default_reuse_policy=$DEFAULT_REUSE_POLICY"
log_event "WINDOW warmup=$WARMUP_SECONDS metric=$METRIC_SECONDS drain=$TRIPINFO_DRAIN_SECONDS decision_interval=$DECISION_INTERVAL_SECONDS thresholds='$QUEUE_THRESHOLDS'"
log_event "SCENARIO_POLICY balanced='240 vph/route, 8 routes/TL' unbalanced='480 vph/route, 2 routes/TL' unbalanced_x1p5_tls='$(scenario_tls unbalanced_x1p5)'"

for scenario_key in $SCENARIOS; do
  scale="$(scenario_scale "$scenario_key")"
  vph="$(scenario_peak_vph "$scenario_key")"
  routes="$(scenario_routes_per_tl "$scenario_key")"
  tls_list="$(scenario_tls "$scenario_key")"
  name="$(scenario_label "$scenario_key")"

  if [[ "$RUN_DEFAULT" == "1" ]]; then
    run_default_case "$scenario_key" "$name" "$scale" "$vph" "$routes" "$tls_list"
  fi

  for model_key in $MODEL_KEYS; do
    for temp in $TEMPERATURES; do
      run_model_case "$model_key" "$scenario_key" "$name" "$scale" "$vph" "$routes" "$tls_list" "$temp"
    done
  done
done

if [[ "$DRY_RUN" != "1" ]]; then
  "$PYTHON_BIN" "$SUMMARIZER" "$RUN_ROOT" | tee "$RUN_ROOT/matrix_summary.md"
  log_event "SUMMARY_WRITTEN $RUN_ROOT/matrix_summary.csv"
fi
log_event "ALL_DONE run_root=$RUN_ROOT"
