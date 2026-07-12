#!/usr/bin/env bash
# Shared helpers for Chengdu benchmark shell launchers.

if [[ -n "${CHENGDU_RUNNER_COMMON_LOADED:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi
CHENGDU_RUNNER_COMMON_LOADED=1

chengdu_timestamp() {
  date -Is 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S%z'
}

log_event() {
  local msg="$1"
  local log_path="${ORCH_LOG:-}"
  if [[ -z "$log_path" && -n "${LOG_DIR:-}" ]]; then
    log_path="$LOG_DIR/orchestrator.log"
  fi
  if [[ -n "$log_path" ]]; then
    printf '[%s] %s\n' "$(chengdu_timestamp)" "$msg" | tee -a "$log_path"
  else
    printf '[%s] %s\n' "$(chengdu_timestamp)" "$msg"
  fi
}

count_words() {
  local values="$1"
  set -- $values
  echo "$#"
}

words_json() {
  VALUES="$1" python3 - <<'PY'
import json
import os

print(json.dumps(os.environ["VALUES"].split()))
PY
}

float_words_json() {
  VALUES="$1" python3 - <<'PY'
import json
import os

print(json.dumps([float(value) for value in os.environ["VALUES"].split()]))
PY
}

max_float_word() {
  VALUES="$1" python3 - <<'PY'
import os

print(max(float(value) for value in os.environ["VALUES"].split()))
PY
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

prepare_run_workspace() {
  local run_root="$1"
  local log_dir="$2"
  local source_script="${3:-}"
  mkdir -p "$run_root" "$log_dir" "$run_root/scripts"
  if [[ -n "$source_script" && -f "$source_script" ]]; then
    cp "$source_script" "$run_root/scripts/$(basename "$source_script")" 2>/dev/null || true
  fi
  echo "$$" > "$run_root/orchestrator.pid"
}

write_tls_file() {
  local target_path="$1"
  local scenario="$2"
  shift 2
  {
    echo "scenario,tl_id"
    for tl_id in $*; do
      echo "$scenario,$tl_id"
    done
  } > "$target_path"
}

resolve_benchmark_root() {
  local project_root="$1"
  if [[ "${BENCH_ROOT_WAS_SET:-0}" == "1" && -n "${BENCH_ROOT:-}" ]]; then
    echo "$BENCH_ROOT"
  elif [[ -d "$project_root/chengdu_benchmark" ]]; then
    echo "$project_root/chengdu_benchmark"
  elif [[ -n "${DEEPSIGNAL_BENCH_ROOT:-}" ]]; then
    echo "$DEEPSIGNAL_BENCH_ROOT"
  else
    echo "${BENCH_ROOT:-$project_root/chengdu_benchmark}"
  fi
}

resolve_sumocfg() {
  local bench_root="$1"
  local scenario="${2:-sumo_llm}"
  local explicit_path="${3:-}"
  if [[ -n "$explicit_path" ]]; then
    echo "$explicit_path"
    return
  fi
  for candidate in \
    "$bench_root/scenarios/$scenario/osm.sumocfg" \
    "$bench_root/$scenario/osm.sumocfg" \
    "$bench_root/data/$scenario/osm.sumocfg" \
    "$bench_root/DeepSignal/data/$scenario/osm.sumocfg"; do
    if [[ -f "$candidate" ]]; then
      echo "$candidate"
      return
    fi
  done
  echo ""
}
