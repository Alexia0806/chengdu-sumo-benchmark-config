#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/env_defaults.sh"

PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
RUNNER="${RUNNER:-$PROJECT_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py}"
SUMMARIZER="${SUMMARIZER:-$PROJECT_ROOT/scripts/summarize_chengdu_peak_matrix.py}"
PYTHON_BIN="${PYTHON_BIN:-$SYSTEM_PYTHON_BIN}"
BENCH_ROOT="${BENCH_ROOT:-$PROJECT_ROOT/chengdu_benchmark}"
MODEL_DIR="${MODEL_DIR:-$MODELS_ROOT/gpt-oss-20b}"
STAMP="${STAMP:-$(date +%Y%m%dT%H%M%S)}"
RUN_ROOT="${RUN_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_gptoss20b_backfill_matrix_20260627_$STAMP}"
LOG_DIR="$RUN_ROOT/logs"
TLS_DIR="$RUN_ROOT/tls"
ORCH_LOG="$LOG_DIR/orchestrator.log"

SCENARIOS="${SCENARIOS:-unbalanced_x1p5 balanced_x1p5 balanced_x1p2 unbalanced_x1p2}"
TEMPERATURES="${TEMPERATURES:-0.1 0.2}"
TARGET_TLS="${TARGET_TLS:-$DEFAULT_TARGET_TLS}"
UNBALANCED_X15_TLS="${UNBALANCED_X15_TLS:-$DEFAULT_UNBALANCED_X15_TLS}"
RUN_UNBALANCED_X15_FULL3TL="${RUN_UNBALANCED_X15_FULL3TL:-0}"
WARMUP_SECONDS="${WARMUP_SECONDS:-300}"
METRIC_SECONDS="${METRIC_SECONDS:-1200}"
DECISION_INTERVAL_SECONDS="${DECISION_INTERVAL_SECONDS:-60}"
TRIPINFO_DRAIN_SECONDS="${TRIPINFO_DRAIN_SECONDS:-600}"
ACTION_DELAY_CYCLES="${ACTION_DELAY_CYCLES:-1}"
MIN_GREEN="${MIN_GREEN:-10}"
MAX_GREEN="${MAX_GREEN:-90}"
QUEUE_THRESHOLDS="${QUEUE_THRESHOLDS:-10 20 30 40}"
TARGET_PEAK_ROUTE_SELECTION="${TARGET_PEAK_ROUTE_SELECTION:-$DEFAULT_TARGET_PEAK_ROUTE_SELECTION}"
N_PREDICT="${N_PREDICT:-1024}"
TIMEOUT_SEC="${TIMEOUT_SEC:-1800}"

mkdir -p "$RUN_ROOT" "$LOG_DIR" "$TLS_DIR" "$RUN_ROOT/scripts"
cp "$0" "$RUN_ROOT/scripts/$(basename "$0")" 2>/dev/null || true
echo "$$" > "$RUN_ROOT/orchestrator.pid"

log_event() {
  printf '[%s] %s\n' "$(date -Is)" "$1" | tee -a "$ORCH_LOG"
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
    balanced_x1p5|balanced_x1p2) echo "balanced" ;;
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
  SCENARIOS="$SCENARIOS" TARGET_TLS="$TARGET_TLS" UNBALANCED_X15_TLS="$UNBALANCED_X15_TLS" \
  RUN_UNBALANCED_X15_FULL3TL="$RUN_UNBALANCED_X15_FULL3TL" TEMPERATURES="$TEMPERATURES" \
  RUN_ROOT="$RUN_ROOT" WARMUP_SECONDS="$WARMUP_SECONDS" METRIC_SECONDS="$METRIC_SECONDS" \
  TRIPINFO_DRAIN_SECONDS="$TRIPINFO_DRAIN_SECONDS" QUEUE_THRESHOLDS="$QUEUE_THRESHOLDS" \
  TARGET_PEAK_ROUTE_SELECTION="$TARGET_PEAK_ROUTE_SELECTION" \
  "$PYTHON_BIN" - <<'PY' > "$RUN_ROOT/experiment_matrix.json"
import json
import os

payload = {
    "run_root": os.environ["RUN_ROOT"],
    "model_group": "gpt_oss_20b",
    "purpose": "20260627 GPT-OSS-20B backfill aligned to 20260626 Chengdu matrix",
    "scenarios": os.environ["SCENARIOS"].split(),
    "temperatures": [float(x) for x in os.environ["TEMPERATURES"].split()],
    "tls": os.environ["TARGET_TLS"].split(),
    "unbalanced_x1p5_tls": (
        os.environ["TARGET_TLS"].split()
        if os.environ["RUN_UNBALANCED_X15_FULL3TL"] == "1"
        else os.environ["UNBALANCED_X15_TLS"].split()
    ),
    "prompt_format": "deepsignal_solution_first",
    "online_control_mode": "repaired",
    "hf_chat_template_message_mode": "single_user",
    "hf_attn_implementation": "eager",
    "hf_experts_implementation": "eager",
    "n_predict": 1024,
    "timeout_sec": 1800,
    "metric_window": {
        "warmup_seconds": int(os.environ["WARMUP_SECONDS"]),
        "metric_seconds": int(os.environ["METRIC_SECONDS"]),
        "tripinfo_drain_seconds": int(os.environ["TRIPINFO_DRAIN_SECONDS"]),
    },
    "scenario_definitions": {
        "balanced": {"target_peak_vph_per_route_base": 240, "target_peak_routes_per_tl": 8},
        "unbalanced": {"target_peak_vph_per_route_base": 480, "target_peak_routes_per_tl": 2},
    },
    "queue_thresholds": [int(x) for x in os.environ["QUEUE_THRESHOLDS"].split()],
    "target_peak_route_selection": os.environ["TARGET_PEAK_ROUTE_SELECTION"],
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
}

run_case() {
  local scenario_key="$1"
  local scenario_name="$2"
  local temp="$3"
  local demand_scale="$4"
  local target_peak_vph_per_route="$5"
  local target_peak_routes_per_tl="$6"
  local tls_list="$7"

  local label tag case_name out_dir tls_file expected_tl_count
  label="$(temp_label "$temp")"
  tag="$(scale_tag "$demand_scale")"
  case_name="08_gpt_oss_20b_solution_chattemplate_repaired_${scenario_name}_${label}_x${tag}"
  out_dir="$RUN_ROOT/$case_name"
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

  log_event "START $case_name scenario=$scenario_name temp=$temp demand_scale=$demand_scale tls='$tls_list' target_peak_vph_per_route=$target_peak_vph_per_route target_peak_routes_per_tl=$target_peak_routes_per_tl target_peak_route_selection=$TARGET_PEAK_ROUTE_SELECTION"
  PYTHONUNBUFFERED=1 \
  PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  HF_ATTN_IMPLEMENTATION=eager \
  HF_EXPERTS_IMPLEMENTATION=eager \
  "$PYTHON_BIN" "$RUNNER" \
    --benchmark-root "$BENCH_ROOT" \
    --sumo-home "$SUMO_HOME" \
    --scenario sumo_llm \
    --tls-file "$tls_file" \
    --output-dir "$out_dir" \
    --input-mode github_official \
    --prompt-format deepsignal_solution_first \
    --no-prefill \
    --online-control-mode strict \
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
    --deepsignal-reasoning-max-chars 160 \
    --pred-wait-forecaster rolling_mean \
    --demand-scale "$demand_scale" \
    "${target_peak_args[@]}" \
    --target-peak-vph-per-route "$target_peak_vph_per_route" \
    --target-peak-routes-per-tl "$target_peak_routes_per_tl" \
    --target-peak-route-selection "$TARGET_PEAK_ROUTE_SELECTION" \
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

log_event "RUN_START run_root=$RUN_ROOT model_dir=$MODEL_DIR scenarios='$SCENARIOS' temps='$TEMPERATURES'"
if [[ ! -d "$MODEL_DIR" ]]; then
  log_event "ERROR missing_model_dir=$MODEL_DIR"
  exit 66
fi
if ! nvidia-smi -L >/dev/null 2>&1; then
  log_event "ERROR no_gpu_available"
  exit 64
fi
write_experiment_matrix

status=0
for scenario_key in $SCENARIOS; do
  scale="$(scenario_scale "$scenario_key")"
  vph="$(scenario_peak_vph "$scenario_key")"
  routes="$(scenario_routes_per_tl "$scenario_key")"
  tls_list="$(scenario_tls "$scenario_key")"
  name="$(scenario_label "$scenario_key")"
  for temp in $TEMPERATURES; do
    run_case "$scenario_key" "$name" "$temp" "$scale" "$vph" "$routes" "$tls_list" || status=$?
  done
done

"$PYTHON_BIN" "$SUMMARIZER" "$RUN_ROOT" | tee "$RUN_ROOT/matrix_summary.md" || status=$?
log_event "ALL_DONE rc=$status run_root=$RUN_ROOT"
exit "$status"
