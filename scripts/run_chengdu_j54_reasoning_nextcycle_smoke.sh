#!/usr/bin/env bash
set -euo pipefail

RUN_ROOT="${RUN_ROOT:-/root/autodl-tmp/tsc-cycle-benchmark/runs/deepsignal_cycleplan/chengdu_j54_reasoning_nextcycle_smoke_20260624}"
SCRIPT="${SCRIPT:-/root/autodl-tmp/tsc-cycle-benchmark/scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py}"
PYTHON_BIN="${PYTHON_BIN:-/root/autodl-tmp/TSC_CYCLE_v1/.venv/bin/python}"
BENCHMARK_ROOT="${BENCHMARK_ROOT:-/root/autodl-tmp/tsc-cycle-benchmark/DeepSignal-benchmark}"
SUMO_HOME="${SUMO_HOME:-/usr/share/sumo}"
SCENARIO="${SCENARIO:-sumo_llm}"
TL_ID="${TL_ID:-J54}"
DEMAND_SCALE="${DEMAND_SCALE:-1.2}"
TARGET_PEAK_VPH_PER_ROUTE="${TARGET_PEAK_VPH_PER_ROUTE:-240}"
TARGET_PEAK_ROUTES_PER_TL="${TARGET_PEAK_ROUTES_PER_TL:-8}"
TEMPERATURE="${TEMPERATURE:-0.1}"
N_PREDICT="${N_PREDICT:-1024}"
TIMEOUT_SEC="${TIMEOUT_SEC:-1200}"
ONLINE_CONTROL_MODE="${ONLINE_CONTROL_MODE:-repaired}"
ACTION_DELAY_CYCLES="${ACTION_DELAY_CYCLES:-1}"
REASONING_MAX_CHARS="${REASONING_MAX_CHARS:-160}"
HF_DTYPE="${HF_DTYPE:-bfloat16}"
HF_DEVICE_MAP="${HF_DEVICE_MAP:-auto}"
USE_CHAT_TEMPLATE="${USE_CHAT_TEMPLATE:-0}"
RUN_DEFAULT="${RUN_DEFAULT:-1}"

mkdir -p "$RUN_ROOT/logs"
STATUS_JSONL="$RUN_ROOT/logs/status.jsonl"

log_status() {
  local event="$1"
  local payload="${2:-{}}"
  "$PYTHON_BIN" - "$STATUS_JSONL" "$event" "$payload" <<'PY'
import datetime, json, pathlib, sys
path = pathlib.Path(sys.argv[1])
event = sys.argv[2]
try:
    payload = json.loads(sys.argv[3])
except json.JSONDecodeError:
    raw_payload = sys.argv[3]
    payload = None
    trial = raw_payload
    while trial.endswith("}") and payload is None:
        trial = trial[:-1]
        try:
            payload = json.loads(trial)
        except json.JSONDecodeError:
            pass
    if payload is None:
        payload = {"raw_payload": raw_payload}
payload = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), "event": event, **payload}
with path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
PY
}

run_case() {
  local model_key="$1"
  local model_path="$2"
  local case_dir="$RUN_ROOT/${model_key}_reasoning_nextcycle_${TL_ID}_temp${TEMPERATURE/./}_x${DEMAND_SCALE/./p}"
  local console_log="$RUN_ROOT/logs/${model_key}.console.log"
  local -a chat_template_args=()
  if [[ "$USE_CHAT_TEMPLATE" == "1" ]]; then
    chat_template_args=(--hf-use-chat-template --hf-chat-template-message-mode single_user --hf-chat-template-enable-thinking)
  fi

  log_status "case_start" "{\"model_key\":\"$model_key\",\"model_path\":\"$model_path\",\"case_dir\":\"$case_dir\"}"
  "$PYTHON_BIN" "$SCRIPT" \
    --benchmark-root "$BENCHMARK_ROOT" \
    --sumo-home "$SUMO_HOME" \
    --scenario "$SCENARIO" \
    --tl-id "$TL_ID" \
    --output-dir "$case_dir" \
    --input-mode github_official \
    --prompt-format deepsignal \
    --deepsignal-reasoning-max-chars "$REASONING_MAX_CHARS" \
    --no-prefill \
    --online-control-mode "$ONLINE_CONTROL_MODE" \
    --action-delay-cycles "$ACTION_DELAY_CYCLES" \
    --warmup-seconds 300 \
    --metric-seconds 1200 \
    --decision-interval-seconds 60 \
    --min-green 10 \
    --max-green 90 \
    --phase-queue-mode split-overlap \
    --queue-threshold 10 \
    --queue-thresholds 10 20 30 40 \
    --tripinfo-metrics \
    --tripinfo-drain-seconds 600 \
    --pred-wait-forecaster rolling_mean \
    --demand-scale "$DEMAND_SCALE" \
    --target-peak-tl-id "$TL_ID" \
    --target-peak-vph-per-route "$TARGET_PEAK_VPH_PER_ROUTE" \
    --target-peak-routes-per-tl "$TARGET_PEAK_ROUTES_PER_TL" \
    --continue-on-run-error \
    --controller model \
    --model-backend hf \
    --hf-model-path "$model_path" \
    --hf-dtype "$HF_DTYPE" \
    --hf-device-map "$HF_DEVICE_MAP" \
    "${chat_template_args[@]}" \
    --temperature "$TEMPERATURE" \
    --model-fail-policy keep_default \
    --n-predict "$N_PREDICT" \
    --timeout-sec "$TIMEOUT_SEC" 2>&1 | tee "$console_log"
  log_status "case_complete" "{\"model_key\":\"$model_key\",\"case_dir\":\"$case_dir\"}"
}

run_default_case() {
  local case_dir="$RUN_ROOT/sumo_default_${TL_ID}_x${DEMAND_SCALE/./p}"
  local console_log="$RUN_ROOT/logs/sumo_default.console.log"

  log_status "case_start" "{\"model_key\":\"sumo_default\",\"case_dir\":\"$case_dir\"}"
  "$PYTHON_BIN" "$SCRIPT" \
    --benchmark-root "$BENCHMARK_ROOT" \
    --sumo-home "$SUMO_HOME" \
    --scenario "$SCENARIO" \
    --tl-id "$TL_ID" \
    --output-dir "$case_dir" \
    --warmup-seconds 300 \
    --metric-seconds 1200 \
    --min-green 10 \
    --max-green 90 \
    --phase-queue-mode split-overlap \
    --queue-threshold 10 \
    --queue-thresholds 10 20 30 40 \
    --tripinfo-metrics \
    --tripinfo-drain-seconds 600 \
    --demand-scale "$DEMAND_SCALE" \
    --target-peak-tl-id "$TL_ID" \
    --target-peak-vph-per-route "$TARGET_PEAK_VPH_PER_ROUTE" \
    --target-peak-routes-per-tl "$TARGET_PEAK_ROUTES_PER_TL" \
    --continue-on-run-error \
    --controller fixed 2>&1 | tee "$console_log"
  log_status "case_complete" "{\"model_key\":\"sumo_default\",\"case_dir\":\"$case_dir\"}"
}

log_status "smoke_start" "{\"run_root\":\"$RUN_ROOT\",\"tl_id\":\"$TL_ID\",\"prompt_format\":\"deepsignal\",\"online_control_mode\":\"$ONLINE_CONTROL_MODE\",\"action_delay_cycles\":$ACTION_DELAY_CYCLES,\"reasoning_max_chars\":$REASONING_MAX_CHARS,\"run_default\":$RUN_DEFAULT}"

if [[ "$RUN_DEFAULT" == "1" ]]; then
  run_default_case
fi
run_case "qwen3_4b_base" "/root/autodl-tmp/models/Qwen3-4B"
run_case "qwen35_9b_base" "/root/autodl-tmp/models/Qwen3.5-9B-Base"

"$PYTHON_BIN" - "$RUN_ROOT" <<'PY'
import json, pathlib, statistics, sys
root = pathlib.Path(sys.argv[1])
rows = []
for per_tl in sorted(root.glob("*/per_tl.jsonl")):
    with per_tl.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                row["case_dir"] = str(per_tl.parent)
                rows.append(row)
summary = {
    "run_root": str(root),
    "rows": len(rows),
    "cases": [
        {
            "case_dir": row["case_dir"],
            "controller": row.get("controller"),
            "tl_id": row.get("tl_id"),
            "model_calls": row.get("model_calls"),
            "strict_format_success_rate": row.get("strict_format_success_rate"),
            "strict_control_usable_rate": row.get("strict_control_usable_rate"),
            "relaxed_json_success_rate": row.get("relaxed_json_success_rate"),
            "relaxed_control_usable_rate": row.get("relaxed_control_usable_rate"),
            "repaired_control_usable_rate": row.get("repaired_control_usable_rate"),
            "plans_queued": row.get("plans_queued"),
            "delayed_plans_applied": row.get("delayed_plans_applied"),
            "plans_applied_rate": row.get("plans_applied_rate"),
            "avg_response_time_sec": row.get("avg_response_time_sec"),
            "avg_queue_vehicles": row.get("avg_queue_vehicles"),
            "target_tl_att_sec": row.get("target_tl_att_sec"),
            "target_tl_awt_sec": row.get("target_tl_awt_sec"),
            "network_att_sec": row.get("network_att_sec"),
            "network_awt_sec": row.get("network_awt_sec"),
        }
        for row in rows
    ],
}
(root / "smoke_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
PY

log_status "smoke_complete" "{\"run_root\":\"$RUN_ROOT\"}"
