#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/env_defaults.sh"

PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
MATRIX_RUNNER="$PROJECT_ROOT/scripts/run_chengdu_phi4_strict_matrix.sh"
PYTHON_BIN="${PYTHON_BIN:-$TSC_CYCLE_ROOT/.venv/bin/python}"
HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
SHUTDOWN_ON_EXIT="${SHUTDOWN_ON_EXIT:-0}"
SHUTDOWN_ONLY_ON_SUCCESS="${SHUTDOWN_ONLY_ON_SUCCESS:-1}"
SHUTDOWN_DELAY_SEC="${SHUTDOWN_DELAY_SEC:-60}"
RUN_STAMP="${RUN_STAMP:-$(date +%Y%m%dT%H%M)}"
RUN_LOG_DIR="$PROJECT_ROOT/runs/deepsignal_cycleplan/two_prompt_phi4_gemma_${RUN_STAMP}"
ORCH_LOG="$RUN_LOG_DIR/orchestrator.log"

mkdir -p "$RUN_LOG_DIR"
echo "$$" > "$RUN_LOG_DIR/orchestrator.pid"

log_event() {
  printf '[%s] %s\n' "$(date -Is)" "$1" | tee -a "$ORCH_LOG"
}

shutdown_host() {
  if ! [[ "$SHUTDOWN_DELAY_SEC" =~ ^[0-9]+$ ]]; then
    log_event "SHUTDOWN_SKIPPED reason=invalid_delay value=$SHUTDOWN_DELAY_SEC"
    return 0
  fi
  log_event "SHUTDOWN_REQUESTED delay_sec=$SHUTDOWN_DELAY_SEC"
  sync || true
  (
    if command -v shutdown >/dev/null 2>&1; then
      shutdown -h "+$(( (SHUTDOWN_DELAY_SEC + 59) / 60 ))" && exit 0
    fi
    sleep "$SHUTDOWN_DELAY_SEC"
    poweroff || halt
  ) >$TMP_ROOT/two_prompt_shutdown.log 2>&1 &
}

finish() {
  local rc=$?
  log_event "PIPELINE_EXIT rc=$rc"
  if [[ "$SHUTDOWN_ON_EXIT" == "1" ]]; then
    if [[ "$SHUTDOWN_ONLY_ON_SUCCESS" == "1" && "$rc" != "0" ]]; then
      log_event "SHUTDOWN_SKIPPED reason=nonzero_exit"
      exit "$rc"
    fi
    shutdown_host
  else
    log_event "SHUTDOWN_SKIPPED reason=disabled"
  fi
  exit "$rc"
}
trap finish EXIT

stop_existing_downloads() {
  pkill -f "$TMP_ROOT/gemma3_12b_download.py" 2>/dev/null || true
}

clean_shm_model() {
  local path="$1"
  if [[ -n "$path" && "$path" == /dev/shm/* ]]; then
    rm -rf "$path"
  fi
}

clean_hf_tmp_cache() {
  rm -rf /dev/shm/hf-home /dev/shm/hf-cache
  mkdir -p /dev/shm/hf-home /dev/shm/hf-cache
}

run_matrix() {
  local model_label="$1"
  local hf_repo="$2"
  local model_path="$3"
  local prompt_format="$4"
  local online_control_mode="$5"
  local n_predict="$6"
  local hf_chat_template_message_mode="$7"
  local prompt_label="${prompt_format}"
  local run_root="$PROJECT_ROOT/runs/deepsignal_cycleplan/${model_label}_${prompt_label}_${online_control_mode}_${RUN_STAMP}"

  log_event "MATRIX_START model=$model_label repo=$hf_repo prompt_format=$prompt_format online_control_mode=$online_control_mode hf_chat_template_message_mode=$hf_chat_template_message_mode run_root=$run_root"
  PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}" \
  RUN_ROOT="$run_root" \
  PYTHON_BIN="$PYTHON_BIN" \
  HF_REPO="$hf_repo" \
  MODEL_PATH="$model_path" \
  MODEL_LABEL="${model_label}_${prompt_label}" \
  HF_ENDPOINT="$HF_ENDPOINT" \
  HF_HOME=/dev/shm/hf-home \
  HF_HUB_CACHE=/dev/shm/hf-cache \
  MODEL_MIN_FREE_GB=30 \
  PROMPT_FORMAT="$prompt_format" \
  ONLINE_CONTROL_MODE="$online_control_mode" \
  N_PREDICT="$n_predict" \
  HF_DTYPE=bfloat16 \
  HF_CHAT_TEMPLATE_MESSAGE_MODE="$hf_chat_template_message_mode" \
  bash "$MATRIX_RUNNER" 2>&1 | tee "$RUN_LOG_DIR/${model_label}_${prompt_label}_${online_control_mode}.console.log"
  log_event "MATRIX_DONE model=$model_label prompt_format=$prompt_format online_control_mode=$online_control_mode run_root=$run_root"
}

log_event "PIPELINE_START run_log_dir=$RUN_LOG_DIR"
stop_existing_downloads
clean_shm_model /dev/shm/gemma-3-12b-it
clean_shm_model /dev/shm/phi-4
clean_hf_tmp_cache

run_matrix "phi4" "microsoft/phi-4" "/dev/shm/phi-4" "deepsignal_json" "strict" "512" "split_system_user"
run_matrix "phi4" "microsoft/phi-4" "/dev/shm/phi-4" "deepsignal" "strict" "512" "split_system_user"

log_event "MODEL_CLEANUP_START model=phi4 path=/dev/shm/phi-4"
clean_shm_model /dev/shm/phi-4
clean_hf_tmp_cache
log_event "MODEL_CLEANUP_DONE model=phi4"

run_matrix "gemma3_12b_it" "google/gemma-3-12b-it" "/dev/shm/gemma-3-12b-it" "deepsignal_json" "strict" "512" "single_user"
run_matrix "gemma3_12b_it" "google/gemma-3-12b-it" "/dev/shm/gemma-3-12b-it" "deepsignal" "strict" "512" "single_user"

log_event "ALL_DONE run_log_dir=$RUN_LOG_DIR"
