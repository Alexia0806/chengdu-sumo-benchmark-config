#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"

PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
BENCH_ROOT="${DEEPSIGNAL_BENCH_ROOT:-$PROJECT_ROOT/DeepSignal-benchmark}"
RUN_ROOT="${RUN_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_3tl_phi4_strict_$(date +%Y%m%d)}"
RUNNER="$PROJECT_ROOT/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py"
SUMMARIZER="$PROJECT_ROOT/scripts/summarize_chengdu_peak_matrix.py"
PYTHON_BIN="${PYTHON_BIN:-$TSC_CYCLE_ROOT/.venv/bin/python}"
HF_REPO="${HF_REPO:-microsoft/phi-4}"
MODEL_PATH="${MODEL_PATH:-/dev/shm/phi-4}"
MODEL_LABEL="${MODEL_LABEL:-phi4}"
HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
MODEL_MIN_FREE_GB="${MODEL_MIN_FREE_GB:-35}"
TARGET_TLS="${TARGET_TLS:-$DEFAULT_TARGET_TLS}"
WARMUP_SECONDS="${WARMUP_SECONDS:-300}"
METRIC_SECONDS="${METRIC_SECONDS:-1200}"
TARGET_PEAK_VPH_PER_ROUTE="${TARGET_PEAK_VPH_PER_ROUTE:-240}"
TARGET_PEAK_ROUTES_PER_TL="${TARGET_PEAK_ROUTES_PER_TL:-8}"
TARGET_PEAK_ROUTE_SELECTION="${TARGET_PEAK_ROUTE_SELECTION:-$DEFAULT_TARGET_PEAK_ROUTE_SELECTION}"
TRIPINFO_DRAIN_SECONDS="${TRIPINFO_DRAIN_SECONDS:-600}"
N_PREDICT="${N_PREDICT:-512}"
HF_DTYPE="${HF_DTYPE:-bfloat16}"
PROMPT_FORMAT="${PROMPT_FORMAT:-deepsignal_json}"
ONLINE_CONTROL_MODE="${ONLINE_CONTROL_MODE:-strict}"
HF_CHAT_TEMPLATE_MESSAGE_MODE="${HF_CHAT_TEMPLATE_MESSAGE_MODE:-split_system_user}"
TLS_FILE="$RUN_ROOT/chengdu_3tl_tls.csv"
LOG_DIR="$RUN_ROOT/logs"
ORCH_LOG="$LOG_DIR/orchestrator.log"
EXPECTED_TL_COUNT="$(wc -w <<< "$TARGET_TLS" | tr -d ' ')"

mkdir -p "$RUN_ROOT" "$LOG_DIR" "$RUN_ROOT/scripts" "$(dirname "$MODEL_PATH")"
cp "$0" "$RUN_ROOT/scripts/$(basename "$0")" 2>/dev/null || true
echo "$$" > "$RUN_ROOT/orchestrator.pid"

{
  echo "scenario,tl_id"
  for tl_id in $TARGET_TLS; do
    echo "sumo_llm,$tl_id"
  done
} > "$TLS_FILE"

TARGET_TLS_JSON="$(
  TARGET_TLS="$TARGET_TLS" python3 - <<'PY'
import json
import os

print(json.dumps(os.environ["TARGET_TLS"].split()))
PY
)"

target_peak_args=()
for tl_id in $TARGET_TLS; do
  target_peak_args+=(--target-peak-tl-id "$tl_id")
done

log_event() {
  printf '[%s] %s\n' "$(date -Is)" "$1" | tee -a "$ORCH_LOG"
}

ensure_model() {
  if [[ -f "$MODEL_PATH/config.json" ]]; then
    log_event "MODEL_READY path=$MODEL_PATH"
    return 0
  fi

  local free_kb
  local required_kb
  free_kb="$(df -Pk "$(dirname "$MODEL_PATH")" | awk 'NR==2 {print $4}')"
  required_kb="$("$PYTHON_BIN" - <<PY
print(int(float("$MODEL_MIN_FREE_GB") * 1024 * 1024))
PY
)"
  if (( free_kb < required_kb )); then
    log_event "MODEL_DOWNLOAD_NEEDS_SPACE free_gb=$(awk -v kb="$free_kb" 'BEGIN {printf \"%.1f\", kb/1024/1024}') required_gb=$MODEL_MIN_FREE_GB path=$(dirname "$MODEL_PATH")"
    return 72
  fi

  log_event "MODEL_DOWNLOAD_START repo=$HF_REPO path=$MODEL_PATH hf_endpoint=${HF_ENDPOINT:-default}"
  HF_REPO="$HF_REPO" MODEL_PATH="$MODEL_PATH" HF_ENDPOINT="$HF_ENDPOINT" "$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path

from huggingface_hub import snapshot_download

repo = os.environ["HF_REPO"]
target = Path(os.environ["MODEL_PATH"])
endpoint = os.environ.get("HF_ENDPOINT") or None
target.mkdir(parents=True, exist_ok=True)
if endpoint:
    os.environ["HF_ENDPOINT"] = endpoint
snapshot_download(
    repo_id=repo,
    local_dir=str(target),
    local_dir_use_symlinks=False,
    resume_download=True,
    allow_patterns=[
        "*.json",
        "*.safetensors",
        "*.model",
        "*.txt",
        "*.py",
        "tokenizer*",
        "generation_config.json",
        "model.safetensors.index.json",
    ],
)
PY
  log_event "MODEL_DOWNLOAD_DONE repo=$HF_REPO path=$MODEL_PATH"
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

run_case() {
  local case_name="$1"
  local demand_scale="$2"
  local temperature="$3"
  local out_dir="$RUN_ROOT/$case_name"
  mkdir -p "$out_dir"
  if [[ -f "$out_dir/per_tl.jsonl" ]] && [[ "$(wc -l < "$out_dir/per_tl.jsonl")" -ge "$EXPECTED_TL_COUNT" ]] && [[ ! -s "$out_dir/failures.jsonl" ]]; then
    log_event "SKIP $case_name already_complete"
    return 0
  fi

  log_event "START $case_name demand_scale=$demand_scale temperature=$temperature online_control_mode=$ONLINE_CONTROL_MODE"
  PYTHONUNBUFFERED=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True "$PYTHON_BIN" "$RUNNER" \
    --benchmark-root "$BENCH_ROOT" \
    --sumo-home "$SUMO_HOME" \
    --scenario sumo_llm \
    --tls-file "$TLS_FILE" \
    --output-dir "$out_dir" \
    --input-mode github_official \
    --prompt-format "$PROMPT_FORMAT" \
    --no-prefill \
    --online-control-mode "$ONLINE_CONTROL_MODE" \
    --warmup-seconds "$WARMUP_SECONDS" \
    --metric-seconds "$METRIC_SECONDS" \
    --decision-interval-seconds 60 \
    --min-green 10 \
    --max-green 90 \
    --phase-queue-mode split-overlap \
    --queue-threshold 10 \
    --queue-thresholds 10 20 30 40 \
    --tripinfo-metrics \
    --tripinfo-drain-seconds "$TRIPINFO_DRAIN_SECONDS" \
    --pred-wait-forecaster rolling_mean \
    --demand-scale "$demand_scale" \
    "${target_peak_args[@]}" \
    --target-peak-vph-per-route "$TARGET_PEAK_VPH_PER_ROUTE" \
    --target-peak-routes-per-tl "$TARGET_PEAK_ROUTES_PER_TL" \
    --target-peak-route-selection "$TARGET_PEAK_ROUTE_SELECTION" \
    --continue-on-run-error \
    --controller model \
    --model-backend hf \
    --hf-model-path "$MODEL_PATH" \
    --hf-dtype "$HF_DTYPE" \
    --hf-use-chat-template \
    --no-hf-chat-template-enable-thinking \
    --hf-chat-template-message-mode "$HF_CHAT_TEMPLATE_MESSAGE_MODE" \
    --hf-skip-special-tokens \
    --temperature "$temperature" \
    --n-predict "$N_PREDICT" \
    --model-fail-policy keep_default \
    2>&1 | tee "$LOG_DIR/$case_name.console.log"
  local rc=${PIPESTATUS[0]}
  log_event "DONE $case_name rc=$rc"
  return "$rc"
}

cat > "$RUN_ROOT/experiment_matrix.json" <<JSON
{
  "run_root": "$RUN_ROOT",
  "purpose": "Chengdu 3-TL $MODEL_LABEL experiment matrix with $PROMPT_FORMAT prompt, HF chat template, $ONLINE_CONTROL_MODE online control, and keep-default fail policy",
  "tls": $TARGET_TLS_JSON,
  "demand_scales": [1.0, 1.2, 1.5, 1.8],
  "temperatures": [0.1, 0.2, 0.4],
  "hf_repo": "$HF_REPO",
  "model": "$MODEL_PATH",
  "prompt_format": "$PROMPT_FORMAT",
  "online_control_mode": "$ONLINE_CONTROL_MODE",
  "hf_use_chat_template": true,
  "hf_chat_template_message_mode": "$HF_CHAT_TEMPLATE_MESSAGE_MODE",
  "hf_skip_special_tokens": true,
  "hf_dtype": "$HF_DTYPE",
  "model_fail_policy": "keep_default",
  "n_predict": $N_PREDICT,
  "target_peak": {
    "vph_per_route_base": $TARGET_PEAK_VPH_PER_ROUTE,
    "routes_per_tl": $TARGET_PEAK_ROUTES_PER_TL,
    "route_selection": "$TARGET_PEAK_ROUTE_SELECTION",
    "max_demand_scale": 1.8
  },
  "queue_thresholds": [10, 20, 30, 40],
  "tripinfo": {
    "enabled": true,
    "drain_seconds": $TRIPINFO_DRAIN_SECONDS,
    "metrics": ["network_att_sec", "network_awt_sec", "target_tl_att_sec", "target_tl_awt_sec"]
  }
}
JSON

log_event "RUN_START run_root=$RUN_ROOT repo=$HF_REPO model=$MODEL_PATH prompt_format=$PROMPT_FORMAT online_control_mode=$ONLINE_CONTROL_MODE hf_chat_template_message_mode=$HF_CHAT_TEMPLATE_MESSAGE_MODE"
ensure_model

status=0
for temp in 0.1 0.2 0.4; do
  label="$(temp_label "$temp")"
  for scale in 1.0 1.2 1.5 1.8; do
    tag="$(scale_tag "$scale")"
    run_case "05_${MODEL_LABEL}_${PROMPT_FORMAT}_chattemplate_${ONLINE_CONTROL_MODE}_${label}_x${tag}" "$scale" "$temp" || status=$?
  done
done

"$PYTHON_BIN" "$SUMMARIZER" "$RUN_ROOT" | tee "$RUN_ROOT/matrix_summary.md"
summary_rc=${PIPESTATUS[0]}
[[ "$summary_rc" -eq 0 ]] || status="$summary_rc"
log_event "SUMMARY_WRITTEN $RUN_ROOT/matrix_summary.csv"
log_event "ALL_DONE rc=$status run_root=$RUN_ROOT"
exit "$status"
