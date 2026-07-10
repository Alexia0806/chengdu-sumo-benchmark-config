#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/root/autodl-tmp/tsc-cycle-benchmark}"
if [[ -z "${BENCH_ROOT:-}" ]]; then
  if [[ -d "$PROJECT_ROOT/chengdu_benchmark" ]]; then
    BENCH_ROOT="$PROJECT_ROOT/chengdu_benchmark"
  else
    BENCH_ROOT="$PROJECT_ROOT/DeepSignal-benchmark"
  fi
fi
RUNNER="${RUNNER:-$PROJECT_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py}"
WINDOW_SUMMARIZER="${WINDOW_SUMMARIZER:-$PROJECT_ROOT/scripts/summarize_step_metric_windows.py}"
PYTHON_BIN="${PYTHON_BIN:-/root/autodl-tmp/TSC_CYCLE_v1/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

RUN_ROOT="${RUN_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_unbalanced_x1p2_ft_maxpressure_20260701_$(date +%Y%m%dT%H%M%S)}"
LOG_DIR="$RUN_ROOT/logs"
TLS_DIR="$RUN_ROOT/tls"
ORCH_LOG="$LOG_DIR/orchestrator.log"

TARGET_TLS="${TARGET_TLS:-J54 314655170 432452987}"
CASE_KEYS="${CASE_KEYS:-ft9b ft9b3500 max_pressure}"
MISSING_MODEL_POLICY="${MISSING_MODEL_POLICY:-fail}"
DRY_RUN="${DRY_RUN:-0}"

WARMUP_SECONDS="${WARMUP_SECONDS:-300}"
METRIC_SECONDS="${METRIC_SECONDS:-1200}"
DECISION_INTERVAL_SECONDS="${DECISION_INTERVAL_SECONDS:-60}"
TRIPINFO_DRAIN_SECONDS="${TRIPINFO_DRAIN_SECONDS:-600}"
ACTION_DELAY_CYCLES="${ACTION_DELAY_CYCLES:-1}"
MIN_GREEN="${MIN_GREEN:-10}"
MAX_GREEN="${MAX_GREEN:-90}"
QUEUE_THRESHOLDS="${QUEUE_THRESHOLDS:-10 20 30 40}"

TEMPERATURE="${TEMPERATURE:-0.2}"
DEMAND_SCALE="${DEMAND_SCALE:-1.2}"
TARGET_PEAK_VPH_PER_ROUTE="${TARGET_PEAK_VPH_PER_ROUTE:-480}"
TARGET_PEAK_ROUTES_PER_TL="${TARGET_PEAK_ROUTES_PER_TL:-2}"
DEEPSIGNAL_REASONING_MAX_CHARS="${DEEPSIGNAL_REASONING_MAX_CHARS:-160}"

QWEN9B_PATH="${QWEN9B_PATH:-/root/autodl-tmp/models/Qwen3.5-9B-Base}"
FT9B_ADAPTER="${FT9B_ADAPTER:-/root/autodl-tmp/TSC_CYCLE_v1/runs/qwen35-9b-text-5090-1p5epoch-20260615T072040Z/adapter}"
FT9B_3500_ADAPTER="${FT9B_3500_ADAPTER:-/root/autodl-tmp/TSC_CYCLE_v1/runs/qwen35-9b-text-5090-3500-3epoch-20260617T044255Z/adapter}"
HF_DTYPE="${HF_DTYPE:-bfloat16}"
HF_DEVICE_MAP="${HF_DEVICE_MAP:-auto}"
HF_N_PREDICT="${HF_N_PREDICT:-512}"
HF_TIMEOUT_SEC="${HF_TIMEOUT_SEC:-1800}"

mkdir -p "$RUN_ROOT" "$LOG_DIR" "$TLS_DIR" "$RUN_ROOT/scripts"
cp "$0" "$RUN_ROOT/scripts/$(basename "$0")" 2>/dev/null || true
echo "$$" > "$RUN_ROOT/orchestrator.pid"

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

write_tls_file() {
  local tls_file="$TLS_DIR/unbalanced_x1p2.csv"
  {
    echo "scenario,tl_id"
    for tl_id in $TARGET_TLS; do
      echo "sumo_llm,$tl_id"
    done
  } > "$tls_file"
  echo "$tls_file"
}

queue_threshold_args=()
for threshold in $QUEUE_THRESHOLDS; do
  queue_threshold_args+=("$threshold")
done

skip_or_fail_missing() {
  local case_key="$1"
  local path="$2"
  if [[ "$MISSING_MODEL_POLICY" == "skip" ]]; then
    log_event "SKIP_CASE case=$case_key reason=missing_path path=$path"
    return 0
  fi
  log_event "ERROR case=$case_key missing_path=$path"
  return 66
}

ensure_path() {
  local case_key="$1"
  local path="$2"
  [[ -d "$path" ]] || { skip_or_fail_missing "$case_key" "$path"; return 1; }
  return 0
}

require_gpu_for_hf() {
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  if [[ "$CASE_KEYS" != *ft9b* ]]; then
    return 0
  fi
  if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi -L >/dev/null 2>&1; then
    log_event "ERROR no_cuda_gpu_for_hf_cases case_keys='$CASE_KEYS'"
    exit 64
  fi
}

write_experiment_matrix() {
  CASE_KEYS="$CASE_KEYS" TARGET_TLS="$TARGET_TLS" RUN_ROOT="$RUN_ROOT" \
  WARMUP_SECONDS="$WARMUP_SECONDS" METRIC_SECONDS="$METRIC_SECONDS" \
  TRIPINFO_DRAIN_SECONDS="$TRIPINFO_DRAIN_SECONDS" QUEUE_THRESHOLDS="$QUEUE_THRESHOLDS" \
  TEMPERATURE="$TEMPERATURE" DEMAND_SCALE="$DEMAND_SCALE" \
  TARGET_PEAK_VPH_PER_ROUTE="$TARGET_PEAK_VPH_PER_ROUTE" TARGET_PEAK_ROUTES_PER_TL="$TARGET_PEAK_ROUTES_PER_TL" \
  "$PYTHON_BIN" - <<'PY' > "$RUN_ROOT/experiment_matrix.json"
import json
import os

payload = {
    "purpose": "20260701 backfill: Fine-tuned 9B old adapter, Fine-tuned 9B 3500x3ep, and max_pressure on Chengdu unbalanced x1.2 T=0.2 three-TL protocol.",
    "run_root": os.environ["RUN_ROOT"],
    "case_keys": os.environ["CASE_KEYS"].split(),
    "scenario": "sumo_llm",
    "scenario_label": "unbalanced_x1p2",
    "tls": os.environ["TARGET_TLS"].split(),
    "temperature": float(os.environ["TEMPERATURE"]),
    "demand_scale": float(os.environ["DEMAND_SCALE"]),
    "target_peak_vph_per_route": float(os.environ["TARGET_PEAK_VPH_PER_ROUTE"]),
    "target_peak_routes_per_tl": int(os.environ["TARGET_PEAK_ROUTES_PER_TL"]),
    "metric_windows_required": [
        {"label": "metric_300_900", "start": 300, "end": 900},
        {"label": "metric_300_1500", "start": 300, "end": 1500},
    ],
    "runner_window": {
        "warmup_seconds": int(os.environ["WARMUP_SECONDS"]),
        "metric_seconds": int(os.environ["METRIC_SECONDS"]),
        "tripinfo_drain_seconds": int(os.environ["TRIPINFO_DRAIN_SECONDS"]),
    },
    "queue_thresholds": [float(x) for x in os.environ["QUEUE_THRESHOLDS"].split()],
    "raw_metric_files": [
        "per_tl.jsonl",
        "model_calls.jsonl",
        "prediction_inputs.jsonl",
        "step_metrics.jsonl",
        "sumo_outputs/tripinfo/*.tripinfo.xml",
    ],
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
}

run_case() {
  local case_key="$1"
  local case_name="$2"
  shift 2

  local out_dir="$RUN_ROOT/$case_name"
  local tls_file expected_tl_count
  tls_file="$(write_tls_file)"
  expected_tl_count="$(wc -w <<< "$TARGET_TLS" | tr -d ' ')"
  mkdir -p "$out_dir"

  if [[ -f "$out_dir/per_tl.jsonl" ]] \
    && [[ "$(wc -l < "$out_dir/per_tl.jsonl")" -ge "$expected_tl_count" ]] \
    && [[ -s "$out_dir/step_metrics.jsonl" ]] \
    && [[ ! -s "$out_dir/failures.jsonl" ]]; then
    log_event "SKIP $case_name already_complete"
    return 0
  fi

  target_peak_args=()
  for tl_id in $TARGET_TLS; do
    target_peak_args+=(--target-peak-tl-id "$tl_id")
  done

  log_event "START $case_name case_key=$case_key tls='$TARGET_TLS' dry_run=$DRY_RUN"
  if [[ "$DRY_RUN" == "1" ]]; then
    log_event "DRY_RUN $case_name command='$PYTHON_BIN $RUNNER ...'"
    return 0
  fi

  PYTHONUNBUFFERED=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True "$PYTHON_BIN" "$RUNNER" \
    --benchmark-root "$BENCH_ROOT" \
    --sumo-home /usr/share/sumo \
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
    --record-step-metrics \
    --record-step-vehicle-ids \
    --tripinfo-metrics \
    --tripinfo-drain-seconds "$TRIPINFO_DRAIN_SECONDS" \
    --demand-scale "$DEMAND_SCALE" \
    "${target_peak_args[@]}" \
    --target-peak-vph-per-route "$TARGET_PEAK_VPH_PER_ROUTE" \
    --target-peak-routes-per-tl "$TARGET_PEAK_ROUTES_PER_TL" \
    --continue-on-run-error \
    "$@" 2>&1 | tee "$LOG_DIR/$case_name.console.log"
  log_event "DONE $case_name"
}

run_ft_case() {
  local case_key="$1"
  local case_name="$2"
  local adapter_path="$3"
  ensure_path "$case_key" "$QWEN9B_PATH" || return 0
  ensure_path "$case_key" "$adapter_path" || return 0
  run_case "$case_key" "$case_name" \
    --input-mode github_official \
    --prompt-format deepsignal \
    --deepsignal-reasoning-max-chars "$DEEPSIGNAL_REASONING_MAX_CHARS" \
    --no-prefill \
    --pred-wait-forecaster rolling_mean \
    --online-control-mode strict \
    --controller model \
    --model-backend hf \
    --hf-model-path "$QWEN9B_PATH" \
    --hf-adapter-path "$adapter_path" \
    --hf-dtype "$HF_DTYPE" \
    --hf-device-map "$HF_DEVICE_MAP" \
    --no-hf-use-chat-template \
    --no-hf-chat-template-enable-thinking \
    --hf-skip-special-tokens \
    --temperature "$TEMPERATURE" \
    --n-predict "$HF_N_PREDICT" \
    --timeout-sec "$HF_TIMEOUT_SEC" \
    --model-fail-policy keep_default
}

run_max_pressure_case() {
  local case_key="$1"
  local case_name="$2"
  run_case "$case_key" "$case_name" \
    --input-mode legacy_snapshot \
    --prompt-format deepsignal \
    --controller max_pressure
}

write_experiment_matrix
require_gpu_for_hf

tag="$(scale_tag "$DEMAND_SCALE")"
label="$(temp_label "$TEMPERATURE")"
log_event "RUN_START run_root=$RUN_ROOT bench_root=$BENCH_ROOT case_keys='$CASE_KEYS'"
log_event "WINDOW warmup=$WARMUP_SECONDS metric=$METRIC_SECONDS drain=$TRIPINFO_DRAIN_SECONDS step_metrics=on windows='300:900 300:1500'"
log_event "SCENARIO unbalanced_x1p2 demand_scale=$DEMAND_SCALE target_peak_vph_per_route=$TARGET_PEAK_VPH_PER_ROUTE target_peak_routes_per_tl=$TARGET_PEAK_ROUTES_PER_TL tls='$TARGET_TLS'"

for case_key in $CASE_KEYS; do
  case "$case_key" in
    ft9b)
      run_ft_case "$case_key" "01_9b_adapter_${label}_unbalanced_x${tag}" "$FT9B_ADAPTER"
      ;;
    ft9b3500)
      run_ft_case "$case_key" "06_9b_adapter_3500_3ep_${label}_unbalanced_x${tag}" "$FT9B_3500_ADAPTER"
      ;;
    max_pressure)
      run_max_pressure_case "$case_key" "09_max_pressure_${label}_unbalanced_x${tag}"
      ;;
    *)
      log_event "ERROR unknown_case_key=$case_key"
      exit 2
      ;;
  esac
done

if [[ "$DRY_RUN" != "1" ]]; then
  "$PYTHON_BIN" "$WINDOW_SUMMARIZER" "$RUN_ROOT" \
    --window 300:900:metric_300_900 \
    --window 300:1500:metric_300_1500 \
    --output-dir "$RUN_ROOT/window_metrics" | tee "$RUN_ROOT/window_metrics.log"
  log_event "WINDOW_SUMMARY_WRITTEN $RUN_ROOT/window_metrics/window_metrics_by_case.csv"
fi
log_event "ALL_DONE run_root=$RUN_ROOT"
