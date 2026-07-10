#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"

PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
PATCH_ROOT="${PATCH_ROOT:-$PATCH_ROOT}"
RUN_ROOT="${RUN_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_unbalanced_x1p2_ft_maxpressure_20260701_auto}"
LOG_FILE="${LOG_FILE:-$PATCH_ROOT/watcher.log}"
POLL_SECONDS="${POLL_SECONDS:-120}"
STABLE_CHECKS_REQUIRED="${STABLE_CHECKS_REQUIRED:-3}"

QWEN9B_PATH="${QWEN9B_PATH:-$MODELS_ROOT/Qwen3.5-9B-Base}"
FT9B_ADAPTER="${FT9B_ADAPTER:-$TSC_CYCLE_ROOT/runs/qwen35-9b-text-5090-1p5epoch-20260615T072040Z/adapter}"
FT9B_3500_ADAPTER="${FT9B_3500_ADAPTER:-$TSC_CYCLE_ROOT/runs/qwen35-9b-text-5090-3500-3epoch-20260617T044255Z/adapter}"

mkdir -p "$(dirname "$LOG_FILE")"

log_event() {
  local msg="$1"
  printf '[%s] %s\n' "$(date -Is)" "$msg" | tee -a "$LOG_FILE"
}

bench_root() {
  if [[ -f "$PROJECT_ROOT/chengdu_benchmark/scenarios/sumo_llm/osm.sumocfg" ]]; then
    echo "$PROJECT_ROOT/chengdu_benchmark"
  elif [[ -f "$PROJECT_ROOT/DeepSignal-benchmark/scenarios/sumo_llm/osm.sumocfg" ]]; then
    echo "$PROJECT_ROOT/DeepSignal-benchmark"
  else
    echo ""
  fi
}

scenario_dir() {
  local bench
  bench="$(bench_root)"
  if [[ -n "$bench" ]]; then
    echo "$bench/scenarios/sumo_llm"
  else
    echo ""
  fi
}

qwen9b_weight_count() {
  find "$QWEN9B_PATH" -maxdepth 1 -type f \( -name '*.safetensors' -o -name '*.bin' \) 2>/dev/null | wc -l
}

missing_requirements() {
  local bench
  bench="$(bench_root)"
  [[ -n "$bench" ]] || echo "benchmark_root"
  if [[ -n "$bench" ]]; then
    [[ -f "$bench/scenarios/sumo_llm/osm.sumocfg" ]] || echo "sumo_llm_sumocfg"
    [[ -f "$bench/scenarios/sumo_llm/ChengduCity.net.xml" ]] || echo "sumo_llm_net"
    [[ -f "$bench/scenarios/sumo_llm/rush_hour_flow.rou.xml" ]] || echo "sumo_llm_rush_hour_routes"
    [[ -f "$bench/scenarios/sumo_llm/morning_rush_hour.rou.xml" ]] || echo "sumo_llm_morning_routes"
  fi
  [[ -f "$QWEN9B_PATH/config.json" ]] || echo "qwen9b_base_config"
  if [[ ! -d "$QWEN9B_PATH" ]] || [[ "$(qwen9b_weight_count)" -lt 1 ]]; then
    echo "qwen9b_base_weights"
  fi
  [[ -f "$FT9B_ADAPTER/adapter_model.safetensors" ]] || echo "ft9b_adapter"
  [[ -f "$FT9B_3500_ADAPTER/adapter_model.safetensors" ]] || echo "ft9b3500_adapter"
  [[ -f "$PATCH_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py" ]] || echo "patched_runner"
  [[ -f "$PATCH_ROOT/scripts/summarize_step_metric_windows.py" ]] || echo "window_summarizer"
  [[ -f "$PATCH_ROOT/scripts/run_chengdu_unbalanced_x1p2_ft_maxpressure_20260701.sh" ]] || echo "run_script"
}

ready_signature() {
  local scenedir
  scenedir="$(scenario_dir)"
  {
    df -B1 "$AUTODL_ROOT" | awk 'NR==2 {print "df_used=" $3}'
    if [[ -n "$scenedir" ]]; then
      stat -c 'sumocfg=%s:%Y' "$scenedir/osm.sumocfg" 2>/dev/null || true
      stat -c 'net=%s:%Y' "$scenedir/ChengduCity.net.xml" 2>/dev/null || true
      stat -c 'rush_routes=%s:%Y' "$scenedir/rush_hour_flow.rou.xml" 2>/dev/null || true
      stat -c 'morning_routes=%s:%Y' "$scenedir/morning_rush_hour.rou.xml" 2>/dev/null || true
    fi
    find "$QWEN9B_PATH" -maxdepth 1 -type f \( -name '*.json' -o -name '*.safetensors' -o -name '*.bin' \) \
      -printf 'qwen9b/%f=%s:%Y\n' 2>/dev/null | sort | sha256sum | awk '{print "qwen9b_files_sha256=" $1}'
    stat -c 'ft9b_adapter_bytes=%s' "$FT9B_ADAPTER/adapter_model.safetensors" 2>/dev/null || true
    stat -c 'ft9b3500_adapter_bytes=%s' "$FT9B_3500_ADAPTER/adapter_model.safetensors" 2>/dev/null || true
  } | paste -sd ';' -
}

install_patch() {
  mkdir -p "$PROJECT_ROOT/scripts"
  cp "$PATCH_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py" "$PROJECT_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py"
  cp "$PATCH_ROOT/scripts/summarize_step_metric_windows.py" "$PROJECT_ROOT/scripts/summarize_step_metric_windows.py"
  cp "$PATCH_ROOT/scripts/run_chengdu_unbalanced_x1p2_ft_maxpressure_20260701.sh" "$PROJECT_ROOT/scripts/run_chengdu_unbalanced_x1p2_ft_maxpressure_20260701.sh"
  chmod +x "$PROJECT_ROOT/scripts/summarize_step_metric_windows.py" "$PROJECT_ROOT/scripts/run_chengdu_unbalanced_x1p2_ft_maxpressure_20260701.sh"
}

if [[ -f "$RUN_ROOT/.watcher_launched" ]]; then
  log_event "ALREADY_LAUNCHED run_root=$RUN_ROOT"
  exit 0
fi

last_signature=""
stable_checks=0
log_event "WATCHER_START project_root=$PROJECT_ROOT patch_root=$PATCH_ROOT run_root=$RUN_ROOT poll_seconds=$POLL_SECONDS stable_checks_required=$STABLE_CHECKS_REQUIRED"

while true; do
  mapfile -t missing < <(missing_requirements)
  if (( ${#missing[@]} > 0 )); then
    stable_checks=0
    last_signature=""
    log_event "WAIT missing=${missing[*]}"
    sleep "$POLL_SECONDS"
    continue
  fi

  signature="$(ready_signature)"
  if [[ "$signature" == "$last_signature" ]]; then
    stable_checks=$((stable_checks + 1))
  else
    stable_checks=1
    last_signature="$signature"
  fi
  log_event "READY_CHECK stable_checks=$stable_checks signature='$signature'"
  if (( stable_checks >= STABLE_CHECKS_REQUIRED )); then
    break
  fi
  sleep "$POLL_SECONDS"
done

install_patch
mkdir -p "$RUN_ROOT"
touch "$RUN_ROOT/.watcher_launched"
log_event "LAUNCH run_root=$RUN_ROOT bench_root=$(bench_root)"
PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}" \
BENCH_ROOT="$(bench_root)" \
RUN_ROOT="$RUN_ROOT" \
CASE_KEYS="${CASE_KEYS:-ft9b ft9b3500 max_pressure}" \
MISSING_MODEL_POLICY="${MISSING_MODEL_POLICY:-fail}" \
nohup bash "$PROJECT_ROOT/scripts/run_chengdu_unbalanced_x1p2_ft_maxpressure_20260701.sh" \
  > "$RUN_ROOT/orchestrator.nohup.log" 2>&1 &
echo $! > "$RUN_ROOT/orchestrator.pid"
log_event "LAUNCHED pid=$(cat "$RUN_ROOT/orchestrator.pid") nohup_log=$RUN_ROOT/orchestrator.nohup.log"
