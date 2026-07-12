#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/env_defaults.sh"

GROUP="$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_fixed15_strict_20260616"
LOG_DIR="$GROUP/logs"
SCRIPT_DIR="$GROUP/scripts"
mkdir -p "$LOG_DIR"

run_one() {
  local name="$1"
  local script="$2"
  local log="$LOG_DIR/${name}.driver.log"
  printf '[%s] START %s\n' "$(date -Is)" "$name" | tee -a "$LOG_DIR/orchestrator.log"
  bash "$SCRIPT_DIR/$script" > "$log" 2>&1
  printf '[%s] DONE %s\n' "$(date -Is)" "$name" | tee -a "$LOG_DIR/orchestrator.log"
}

run_one 00_default_sumo run_chengdu_strict15_00_default_sumo.sh
run_one 01_9b_adapter_temp04 run_chengdu_strict15_01_9b_adapter.sh
run_one 02_model_fp16_20260519_temp04 run_chengdu_strict15_02_model_fp16_20260519.sh
run_one 03_qwen3_4b_base_strict_min_green_temp04 run_chengdu_strict15_03_qwen3_4b_base_strict.sh
