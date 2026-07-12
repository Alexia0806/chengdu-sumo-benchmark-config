#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"
source "$PROJECT_ROOT/src/sumo_benchmark/shell/chengdu_runner_common.sh"

PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
BENCH_ROOT="$(resolve_benchmark_root "$PROJECT_ROOT")"

PYTHON_BIN="${PYTHON_BIN:-$TSC_CYCLE_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

RUNNER="${RUNNER:-$PROJECT_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py}"
TLS_SELECTOR="${TLS_SELECTOR:-$PROJECT_ROOT/scripts/select_chengdu_tls_candidates.py}"
WINDOW_SUMMARIZER="${WINDOW_SUMMARIZER:-$PROJECT_ROOT/scripts/summarize_step_metric_windows.py}"
FAIRNESS_SUMMARIZER="${FAIRNESS_SUMMARIZER:-$PROJECT_ROOT/scripts/recompute_target_peak_fairness_metrics.py}"
PROBE_FILTER="${PROBE_FILTER:-$PROJECT_ROOT/scripts/filter_chengdu_tls_probe_results.py}"

RUN_ROOT="${RUN_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_tls_short_probe_$(date +%Y%m%dT%H%M%S)}"
LOG_DIR="$RUN_ROOT/logs"
TLS_DIR="$RUN_ROOT/tls"
SELECTION_DIR="$RUN_ROOT/tls_selection"
prepare_run_workspace "$RUN_ROOT" "$LOG_DIR" "$0"
mkdir -p "$TLS_DIR" "$SELECTION_DIR"

SUMOCFG="${SUMOCFG:-}"
TLS_FILE="${TLS_FILE:-}"
TOP_N="${TOP_N:-20}"
CASE_KEYS="${CASE_KEYS:-fixed max_pressure}"
DRY_RUN="${DRY_RUN:-0}"

WARMUP_SECONDS="${WARMUP_SECONDS:-300}"
METRIC_SECONDS="${METRIC_SECONDS:-600}"
SIMULATION_SECONDS="${SIMULATION_SECONDS:-900}"
TRIPINFO_DRAIN_SECONDS="${TRIPINFO_DRAIN_SECONDS:-600}"
DECISION_INTERVAL_SECONDS="${DECISION_INTERVAL_SECONDS:-60}"
ACTION_DELAY_CYCLES="${ACTION_DELAY_CYCLES:-1}"
DEMAND_SCALE="${DEMAND_SCALE:-1.2}"
TARGET_PEAK_VPH_PER_ROUTE="${TARGET_PEAK_VPH_PER_ROUTE:-480}"
TARGET_PEAK_ROUTES_PER_TL="${TARGET_PEAK_ROUTES_PER_TL:-2}"
TARGET_PEAK_ROUTE_SELECTION="${TARGET_PEAK_ROUTE_SELECTION:-diverse_sources}"
TARGET_PEAK_MIN_SOURCE_LENGTH="${TARGET_PEAK_MIN_SOURCE_LENGTH:-80}"
TARGET_PEAK_MIN_DEST_LENGTH="${TARGET_PEAK_MIN_DEST_LENGTH:-80}"
QUEUE_THRESHOLDS="${QUEUE_THRESHOLDS:-10 20 30 40}"

MIN_COMPLETED_RATE_PCT="${MIN_COMPLETED_RATE_PCT:-60}"
MIN_ARRIVED_RATE_PCT="${MIN_ARRIVED_RATE_PCT:-35}"
MAX_SOURCE_AVG_QUEUE="${MAX_SOURCE_AVG_QUEUE:-45}"
MAX_SOURCE_P95_QUEUE="${MAX_SOURCE_P95_QUEUE:-90}"

queue_threshold_args=()
for threshold in $QUEUE_THRESHOLDS; do
  queue_threshold_args+=("$threshold")
done

prepare_tls_file() {
  if [[ -n "$TLS_FILE" ]]; then
    cp "$TLS_FILE" "$TLS_DIR/candidate_tls_short_probe.csv"
    echo "$TLS_DIR/candidate_tls_short_probe.csv"
    return
  fi
  local sumocfg_path
  sumocfg_path="$(resolve_sumocfg "$BENCH_ROOT" sumo_llm "$SUMOCFG")"
  if [[ -z "$sumocfg_path" || ! -f "$sumocfg_path" ]]; then
    log_event "ERROR cannot_resolve_sumocfg bench_root=$BENCH_ROOT set SUMOCFG=/path/to/osm.sumocfg or TLS_FILE=/path/to/tls.csv"
    exit 2
  fi
  "$PYTHON_BIN" "$TLS_SELECTOR" \
    --sumocfg "$sumocfg_path" \
    --output-dir "$SELECTION_DIR" \
    --top-n "$TOP_N" \
    --target-peak-routes-per-tl "$TARGET_PEAK_ROUTES_PER_TL" \
    > "$LOG_DIR/tls_selector.log"
  echo "$SELECTION_DIR/candidate_tls_short_probe.csv"
}

run_case() {
  local case_key="$1"
  local case_name="$2"
  shift 2
  local out_dir="$RUN_ROOT/$case_name"
  mkdir -p "$out_dir"

  log_event "START case=$case_name controller=$case_key"
  if [[ "$DRY_RUN" == "1" ]]; then
    log_event "DRY_RUN case=$case_name"
    return 0
  fi

  PYTHONUNBUFFERED=1 "$PYTHON_BIN" "$RUNNER" \
    --benchmark-root "$BENCH_ROOT" \
    --sumo-home "$SUMO_HOME" \
    --scenario sumo_llm \
    --tls-file "$TLS_FILE_EFFECTIVE" \
    --output-dir "$out_dir" \
    --warmup-seconds "$WARMUP_SECONDS" \
    --metric-seconds "$METRIC_SECONDS" \
    --simulation-seconds "$SIMULATION_SECONDS" \
    --decision-interval-seconds "$DECISION_INTERVAL_SECONDS" \
    --action-delay-cycles "$ACTION_DELAY_CYCLES" \
    --min-green 10 \
    --max-green 90 \
    --phase-queue-mode split-overlap \
    --queue-threshold 10 \
    --queue-thresholds "${queue_threshold_args[@]}" \
    --record-step-metrics \
    --record-step-vehicle-ids \
    --tripinfo-metrics \
    --tripinfo-drain-seconds "$TRIPINFO_DRAIN_SECONDS" \
    --tripinfo-write-unfinished \
    --tripinfo-write-undeparted \
    --demand-scale "$DEMAND_SCALE" \
    --target-peak-vph-per-route "$TARGET_PEAK_VPH_PER_ROUTE" \
    --target-peak-routes-per-tl "$TARGET_PEAK_ROUTES_PER_TL" \
    --target-peak-route-selection "$TARGET_PEAK_ROUTE_SELECTION" \
    --target-peak-min-source-length "$TARGET_PEAK_MIN_SOURCE_LENGTH" \
    --target-peak-min-dest-length "$TARGET_PEAK_MIN_DEST_LENGTH" \
    --continue-on-run-error \
    "$@" 2>&1 | tee "$LOG_DIR/$case_name.console.log"
  log_event "DONE case=$case_name"
}

TLS_FILE_EFFECTIVE="$(prepare_tls_file)"
log_event "RUN_START run_root=$RUN_ROOT bench_root=$BENCH_ROOT tls_file=$TLS_FILE_EFFECTIVE case_keys='$CASE_KEYS'"
log_event "PROBE_WINDOW warmup=$WARMUP_SECONDS metric=$METRIC_SECONDS simulation=$SIMULATION_SECONDS drain=$TRIPINFO_DRAIN_SECONDS"
log_event "TARGET_PEAK demand_scale=$DEMAND_SCALE vph_per_route=$TARGET_PEAK_VPH_PER_ROUTE routes_per_tl=$TARGET_PEAK_ROUTES_PER_TL selection=$TARGET_PEAK_ROUTE_SELECTION"

for case_key in $CASE_KEYS; do
  case "$case_key" in
    fixed)
      run_case fixed "00_fixed_short_probe_x${DEMAND_SCALE/./p}" --controller fixed --input-mode legacy_snapshot
      ;;
    max_pressure)
      run_case max_pressure "01_max_pressure_short_probe_x${DEMAND_SCALE/./p}" --controller max_pressure --input-mode legacy_snapshot
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
    --output-dir "$RUN_ROOT/window_metrics" | tee "$RUN_ROOT/window_metrics.log"

  "$PYTHON_BIN" "$FAIRNESS_SUMMARIZER" "$RUN_ROOT" \
    --output-dir "$RUN_ROOT/fairness_metrics" \
    --window-metrics "$RUN_ROOT/window_metrics/window_metrics_per_tl.csv" \
    --windows 300:900 \
    --aggregate-tls "$(tail -n +2 "$TLS_FILE_EFFECTIVE" | cut -d, -f2 | paste -sd, -)" \
    | tee "$RUN_ROOT/fairness_metrics.log"

  "$PYTHON_BIN" "$PROBE_FILTER" \
    --fairness-per-tl "$RUN_ROOT/fairness_metrics/target_peak_fairness_per_tl_planned_window.csv" \
    --probe-root "$RUN_ROOT" \
    --prescreen "$SELECTION_DIR/candidate_tls_prescreen.csv" \
    --output-dir "$RUN_ROOT/probe_filter" \
    --window 300_900 \
    --min-completed-rate-pct "$MIN_COMPLETED_RATE_PCT" \
    --min-arrived-rate-pct "$MIN_ARRIVED_RATE_PCT" \
    --max-source-avg-queue "$MAX_SOURCE_AVG_QUEUE" \
    --max-source-p95-queue "$MAX_SOURCE_P95_QUEUE" \
    | tee "$RUN_ROOT/probe_filter.log"
fi

log_event "ALL_DONE run_root=$RUN_ROOT"
