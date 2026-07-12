#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/env_defaults.sh"

if [[ -z "${PROJECT_ROOT:-}" ]]; then
  if [[ -f "scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py" ]]; then
    PROJECT_ROOT="$(pwd)"
  else
    PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  fi
fi
BENCH_ROOT="${BENCH_ROOT:-$PROJECT_ROOT/chengdu_benchmark}"
RUNNER="${RUNNER:-$PROJECT_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py}"
WINDOW_SUMMARIZER="${WINDOW_SUMMARIZER:-$PROJECT_ROOT/scripts/summarize_step_metric_windows.py}"
CANDIDATE_RANKER="${CANDIDATE_RANKER:-$PROJECT_ROOT/scripts/rank_chengdu_tls_benchmark_candidates.py}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

RUN_ROOT="${RUN_ROOT:-$PROJECT_ROOT/outputs/chengdu_20260519_all_tls_$(date +%Y%m%dT%H%M%S)}"
LOG_DIR="$RUN_ROOT/logs"
TLS_DIR="$RUN_ROOT/tls"
CASE_DIR="$RUN_ROOT/runs"
mkdir -p "$RUN_ROOT" "$LOG_DIR" "$TLS_DIR" "$CASE_DIR" "$RUN_ROOT/scripts"
cp "$0" "$RUN_ROOT/scripts/$(basename "$0")" 2>/dev/null || true
echo "$$" > "$RUN_ROOT/orchestrator.pid"

SCENARIO="${SCENARIO:-sumo_llm}"
SUMOCFG="${SUMOCFG:-$BENCH_ROOT/scenarios/$SCENARIO/osm.sumocfg}"
NET_FILE="${NET_FILE:-$BENCH_ROOT/scenarios/$SCENARIO/ChengduCity.net.xml}"
PRESCREEN_CSV="${PRESCREEN_CSV:-$PROJECT_ROOT/outputs/chengdu_tls_candidate_selection_20260706/candidate_tls_prescreen.csv}"

CASE_KEYS="${CASE_KEYS:-model}"
TL_LIMIT="${TL_LIMIT:-0}"
TLS_FILE="${TLS_FILE:-}"
DRY_RUN="${DRY_RUN:-0}"

WARMUP_SECONDS="${WARMUP_SECONDS:-300}"
METRIC_SECONDS="${METRIC_SECONDS:-1200}"
SIMULATION_SECONDS="${SIMULATION_SECONDS:-1500}"
TRIPINFO_DRAIN_SECONDS="${TRIPINFO_DRAIN_SECONDS:-600}"
DECISION_INTERVAL_SECONDS="${DECISION_INTERVAL_SECONDS:-60}"
ACTION_DELAY_CYCLES="${ACTION_DELAY_CYCLES:-1}"
MIN_GREEN="${MIN_GREEN:-10}"
MAX_GREEN="${MAX_GREEN:-90}"
QUEUE_THRESHOLDS="${QUEUE_THRESHOLDS:-10 20 30 40}"

TEMPERATURE="${TEMPERATURE:-0.2}"
DEMAND_SCALE="${DEMAND_SCALE:-1.2}"
TARGET_PEAK_VPH_PER_ROUTE="${TARGET_PEAK_VPH_PER_ROUTE:-480}"
TARGET_PEAK_ROUTES_PER_TL="${TARGET_PEAK_ROUTES_PER_TL:-2}"
TARGET_PEAK_ROUTE_SELECTION="${TARGET_PEAK_ROUTE_SELECTION:-diverse_sources}"
TARGET_PEAK_MIN_SOURCE_LENGTH="${TARGET_PEAK_MIN_SOURCE_LENGTH:-80}"
TARGET_PEAK_MIN_DEST_LENGTH="${TARGET_PEAK_MIN_DEST_LENGTH:-80}"
DEEPSIGNAL_REASONING_MAX_CHARS="${DEEPSIGNAL_REASONING_MAX_CHARS:-160}"

MODEL_BACKEND="${MODEL_BACKEND:-openai}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://127.0.0.1:1234/v1}"
OPENAI_MODEL="${OPENAI_MODEL:-ds20260519_fixed_smoke}"
N_PREDICT="${N_PREDICT:-512}"
TIMEOUT_SEC="${TIMEOUT_SEC:-1800}"
GGUF_PATH="${GGUF_PATH:-}"
LLAMA_SERVER="${LLAMA_SERVER:-}"
SUMO_HOME_ARG="${SUMO_HOME_ARG:-}"

ORCH_LOG="$LOG_DIR/orchestrator.log"

log_event() {
  local msg="$1"
  printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$msg" | tee -a "$ORCH_LOG"
}

safe_token() {
  printf '%s' "$1" | sed 's/[^A-Za-z0-9_.-]/_/g'
}

write_all_tls_file() {
  local out="$TLS_DIR/all_tls.csv"
  if [[ -n "$TLS_FILE" ]]; then
    cp "$TLS_FILE" "$out"
    TLS_FILE_EFFECTIVE="$out"
    return
  fi
  "$PYTHON_BIN" -c 'import csv,sys,xml.etree.ElementTree as ET; net_file,scenario,limit_s=sys.argv[1:4]; limit=int(limit_s); root=ET.parse(net_file).getroot(); ids=sorted(tl.attrib["id"] for tl in root.iter("tlLogic") if tl.attrib.get("id")); ids=ids[:limit] if limit>0 else ids; writer=csv.writer(sys.stdout); writer.writerow(["scenario","tl_id"]); [writer.writerow([scenario,tl_id]) for tl_id in ids]' "$NET_FILE" "$SCENARIO" "$TL_LIMIT" > "$out"
  TLS_FILE_EFFECTIVE="$out"
}

write_single_tls_file() {
  local tl_id="$1"
  local token="$2"
  local out="$TLS_DIR/${token}.csv"
  {
    echo "scenario,tl_id"
    echo "$SCENARIO,$tl_id"
  } > "$out"
  echo "$out"
}

queue_threshold_args=()
for threshold in $QUEUE_THRESHOLDS; do
  queue_threshold_args+=("$threshold")
done

common_runner_args=(
  --benchmark-root "$BENCH_ROOT"
  --scenario "$SCENARIO"
  --warmup-seconds "$WARMUP_SECONDS"
  --metric-seconds "$METRIC_SECONDS"
  --simulation-seconds "$SIMULATION_SECONDS"
  --decision-interval-seconds "$DECISION_INTERVAL_SECONDS"
  --action-delay-cycles "$ACTION_DELAY_CYCLES"
  --min-green "$MIN_GREEN"
  --max-green "$MAX_GREEN"
  --phase-queue-mode split-overlap
  --queue-threshold 10
  --queue-thresholds "${queue_threshold_args[@]}"
  --record-step-metrics
  --record-step-vehicle-ids
  --tripinfo-metrics
  --tripinfo-drain-seconds "$TRIPINFO_DRAIN_SECONDS"
  --tripinfo-write-unfinished
  --tripinfo-write-undeparted
  --demand-scale "$DEMAND_SCALE"
  --target-peak-vph-per-route "$TARGET_PEAK_VPH_PER_ROUTE"
  --target-peak-routes-per-tl "$TARGET_PEAK_ROUTES_PER_TL"
  --target-peak-route-selection "$TARGET_PEAK_ROUTE_SELECTION"
  --target-peak-min-source-length "$TARGET_PEAK_MIN_SOURCE_LENGTH"
  --target-peak-min-dest-length "$TARGET_PEAK_MIN_DEST_LENGTH"
  --continue-on-run-error
)
if [[ -n "$SUMO_HOME_ARG" ]]; then
  common_runner_args+=(--sumo-home "$SUMO_HOME_ARG")
fi
if [[ "$WARMUP_SECONDS" != "300" || "$METRIC_SECONDS" != "1200" ]]; then
  common_runner_args+=(--allow-nonstandard-window)
fi

model_runner_args=(
  --controller model
  --input-mode github_official
  --prompt-format deepsignal
  --deepsignal-reasoning-max-chars "$DEEPSIGNAL_REASONING_MAX_CHARS"
  --no-prefill
  --pred-wait-forecaster rolling_mean
  --forecaster-history-steps 60
  --forecaster-min-history-steps 5
  --online-control-mode strict
  --temperature "$TEMPERATURE"
  --n-predict "$N_PREDICT"
  --timeout-sec "$TIMEOUT_SEC"
  --model-fail-policy keep_default
)

is_complete() {
  local out_dir="$1"
  [[ -s "$out_dir/per_tl.jsonl" ]] && [[ -s "$out_dir/step_metrics.jsonl" ]] && [[ ! -s "$out_dir/failures.jsonl" ]]
}

run_one() {
  local case_key="$1"
  local tl_id="$2"
  local token="$3"
  local tls_file="$4"
  local out_dir="$CASE_DIR/$case_key/$token"
  local console_log="$LOG_DIR/${case_key}.${token}.console.log"
  mkdir -p "$out_dir"

  if is_complete "$out_dir"; then
    log_event "SKIP_COMPLETE case=$case_key tl_id=$tl_id out_dir=$out_dir"
    return 0
  fi

  log_event "START case=$case_key tl_id=$tl_id out_dir=$out_dir"
  if [[ "$DRY_RUN" == "1" ]]; then
    log_event "DRY_RUN case=$case_key tl_id=$tl_id"
    return 0
  fi

  case "$case_key" in
    model)
      backend_args=()
      case "$MODEL_BACKEND" in
        openai)
          backend_args=(
            --model-backend openai
            --openai-base-url "$OPENAI_BASE_URL"
            --openai-model "$OPENAI_MODEL"
            --no-openai-json-system-prompt
          )
          ;;
        llama)
          if [[ -z "$GGUF_PATH" || -z "$LLAMA_SERVER" ]]; then
            log_event "ERROR MODEL_BACKEND=llama requires GGUF_PATH and LLAMA_SERVER"
            exit 2
          fi
          backend_args=(
            --model-backend llama
            --gguf-path "$GGUF_PATH"
            --llama-server "$LLAMA_SERVER"
            --ngl 99
            --threads 8
            --ctx-size 4096
            --server-startup-sec 240
          )
          ;;
        *)
          log_event "ERROR unknown MODEL_BACKEND=$MODEL_BACKEND"
          exit 2
          ;;
      esac
      PYTHONUNBUFFERED=1 "$PYTHON_BIN" "$RUNNER" \
        "${common_runner_args[@]}" \
        --tls-file "$tls_file" \
        --target-peak-tl-id "$tl_id" \
        --output-dir "$out_dir" \
        "${model_runner_args[@]}" \
        "${backend_args[@]}" \
        2>&1 | tee "$console_log"
      ;;
    fixed)
      PYTHONUNBUFFERED=1 "$PYTHON_BIN" "$RUNNER" \
        "${common_runner_args[@]}" \
        --tls-file "$tls_file" \
        --target-peak-tl-id "$tl_id" \
        --output-dir "$out_dir" \
        --controller fixed \
        --input-mode legacy_snapshot \
        2>&1 | tee "$console_log"
      ;;
    max_pressure)
      PYTHONUNBUFFERED=1 "$PYTHON_BIN" "$RUNNER" \
        "${common_runner_args[@]}" \
        --tls-file "$tls_file" \
        --target-peak-tl-id "$tl_id" \
        --output-dir "$out_dir" \
        --controller max_pressure \
        --input-mode legacy_snapshot \
        2>&1 | tee "$console_log"
      ;;
    *)
      log_event "ERROR unknown case_key=$case_key"
      exit 2
      ;;
  esac
  log_event "DONE case=$case_key tl_id=$tl_id"
}

write_experiment_manifest() {
  cat > "$RUN_ROOT/experiment_manifest.json" <<EOF
{
  "purpose": "Full Chengdu TLS benchmark candidate scan with DeepSignal-20260519.",
  "run_root": "$RUN_ROOT",
  "scenario": "$SCENARIO",
  "case_keys": "$CASE_KEYS",
  "tls_file": "$TLS_FILE_EFFECTIVE",
  "net_file": "$NET_FILE",
  "prescreen_csv": "$PRESCREEN_CSV",
  "sumo_home_arg": "$SUMO_HOME_ARG",
  "proj_data": "${PROJ_DATA:-}",
  "proj_lib": "${PROJ_LIB:-}",
  "model_backend": "$MODEL_BACKEND",
  "openai_base_url": "$OPENAI_BASE_URL",
  "openai_model": "$OPENAI_MODEL",
  "temperature": $TEMPERATURE,
  "n_predict": $N_PREDICT,
  "timeout_sec": $TIMEOUT_SEC,
  "demand_scale": $DEMAND_SCALE,
  "target_peak_policy": "one target_peak_tl_id per runner invocation",
  "target_peak_vph_per_route": $TARGET_PEAK_VPH_PER_ROUTE,
  "target_peak_routes_per_tl": $TARGET_PEAK_ROUTES_PER_TL,
  "target_peak_route_selection": "$TARGET_PEAK_ROUTE_SELECTION",
  "windows": [
    {"label": "metric_300_900", "start": 300, "end": 900},
    {"label": "metric_300_1500", "start": 300, "end": 1500}
  ],
  "runner_window": {
    "warmup_seconds": $WARMUP_SECONDS,
    "metric_seconds": $METRIC_SECONDS,
    "simulation_seconds": $SIMULATION_SECONDS,
    "tripinfo_drain_seconds": $TRIPINFO_DRAIN_SECONDS,
    "decision_interval_seconds": $DECISION_INTERVAL_SECONDS,
    "action_delay_cycles": $ACTION_DELAY_CYCLES,
    "min_green": $MIN_GREEN,
    "max_green": $MAX_GREEN,
    "queue_thresholds": "$QUEUE_THRESHOLDS"
  },
  "raw_files_per_tl": [
    "per_tl.jsonl",
    "model_calls.jsonl",
    "prediction_inputs.jsonl",
    "step_metrics.jsonl",
    "sumo_outputs/tripinfo/*.tripinfo.xml"
  ]
}
EOF
}

TLS_FILE_EFFECTIVE=""
write_all_tls_file
write_experiment_manifest
log_event "RUN_START run_root=$RUN_ROOT tls_file=$TLS_FILE_EFFECTIVE case_keys='$CASE_KEYS'"
log_event "WINDOW warmup=$WARMUP_SECONDS metric=$METRIC_SECONDS simulation=$SIMULATION_SECONDS drain=$TRIPINFO_DRAIN_SECONDS"
log_event "MODEL backend=$MODEL_BACKEND openai_model=$OPENAI_MODEL temperature=$TEMPERATURE"

tail -n +2 "$TLS_FILE_EFFECTIVE" | while IFS=, read -r scenario tl_id; do
  scenario="${scenario//$'\r'/}"
  tl_id="${tl_id//$'\r'/}"
  [[ -n "$tl_id" ]] || continue
  token="$(safe_token "$tl_id")"
  single_tls_file="$(write_single_tls_file "$tl_id" "$token")"
  for case_key in $CASE_KEYS; do
    run_one "$case_key" "$tl_id" "$token" "$single_tls_file"
  done
done

if [[ "$DRY_RUN" != "1" ]]; then
  "$PYTHON_BIN" "$WINDOW_SUMMARIZER" "$CASE_DIR" \
    --window 300:900:metric_300_900 \
    --window 300:1500:metric_300_1500 \
    --output-dir "$RUN_ROOT/window_metrics" | tee "$RUN_ROOT/window_metrics.log"

  "$PYTHON_BIN" "$CANDIDATE_RANKER" \
    --window-metrics "$RUN_ROOT/window_metrics/window_metrics_per_tl.csv" \
    --net-file "$NET_FILE" \
    --prescreen "$PRESCREEN_CSV" \
    --output-dir "$RUN_ROOT/candidate_ranking" \
    --primary-window metric_300_900 \
    --controller model | tee "$RUN_ROOT/candidate_ranking.log"
  log_event "SUMMARY_WRITTEN window_metrics=$RUN_ROOT/window_metrics candidate_ranking=$RUN_ROOT/candidate_ranking"
fi

log_event "ALL_DONE run_root=$RUN_ROOT"
