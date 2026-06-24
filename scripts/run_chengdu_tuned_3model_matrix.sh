#!/usr/bin/env bash
set -euo pipefail

MATRIX_ROOT="${MATRIX_ROOT:-/root/autodl-tmp/tsc-cycle-benchmark/runs/deepsignal_cycleplan/chengdu_3model_tuned_matrix_20260624}"
SMOKE_SCRIPT="${SMOKE_SCRIPT:-/root/autodl-tmp/tsc-cycle-benchmark/scripts/run_chengdu_j54_reasoning_nextcycle_smoke.sh}"
PYTHON_BIN="${PYTHON_BIN:-/root/autodl-tmp/TSC_CYCLE_v1/.venv/bin/python}"

TLS="${TLS:-J54 314655170 432452987}"
SCALES="${SCALES:-1.0 1.2 1.5 1.8}"
TEMPS="${TEMPS:-0.1}"

WARMUP_SECONDS="${WARMUP_SECONDS:-300}"
METRIC_SECONDS="${METRIC_SECONDS:-1200}"
TRIPINFO_DRAIN_SECONDS="${TRIPINFO_DRAIN_SECONDS:-600}"
DECISION_INTERVAL_SECONDS="${DECISION_INTERVAL_SECONDS:-60}"
TARGET_PEAK_VPH_PER_ROUTE="${TARGET_PEAK_VPH_PER_ROUTE:-240}"
TARGET_PEAK_ROUTES_PER_TL="${TARGET_PEAK_ROUTES_PER_TL:-8}"
N_PREDICT="${N_PREDICT:-2048}"
TIMEOUT_SEC="${TIMEOUT_SEC:-1800}"
ONLINE_CONTROL_MODE="${ONLINE_CONTROL_MODE:-repaired}"
ACTION_DELAY_CYCLES="${ACTION_DELAY_CYCLES:-1}"
REASONING_MAX_CHARS="${REASONING_MAX_CHARS:-160}"
HF_DTYPE="${HF_DTYPE:-bfloat16}"
HF_DEVICE_MAP="${HF_DEVICE_MAP:-auto}"

PARALLEL_QWEN="${PARALLEL_QWEN:-1}"
RETRY_FAILED_SEQUENTIAL="${RETRY_FAILED_SEQUENTIAL:-1}"
FORCE_RERUN="${FORCE_RERUN:-0}"

mkdir -p "$MATRIX_ROOT/logs" "$MATRIX_ROOT/cells"
STATUS_JSONL="$MATRIX_ROOT/logs/status.jsonl"

log_status() {
  local event="$1"
  local payload="${2:-{}}"
  "$PYTHON_BIN" - "$STATUS_JSONL" "$event" "$payload" <<'PY'
import datetime, json, pathlib, sys

path = pathlib.Path(sys.argv[1])
event = sys.argv[2]
payload = json.loads(sys.argv[3])
payload = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), "event": event, **payload}
with path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
PY
}

has_completed_per_tl() {
  local run_root="$1"
  find "$run_root" -mindepth 2 -maxdepth 2 -name per_tl.jsonl -size +0c 2>/dev/null | grep -q .
}

has_failures() {
  local run_root="$1"
  find "$run_root" -mindepth 2 -maxdepth 2 -name failures.jsonl -size +0c 2>/dev/null | grep -q .
}

run_cell() {
  local group="$1"
  local model_key="$2"
  local model_path="$3"
  local prompt_format="$4"
  local template_thinking_label="$5"
  local tl_id="$6"
  local scale="$7"
  local temp="$8"
  local safe_scale="${scale/./p}"
  local safe_temp="${temp/./}"
  local cell_id="${model_key}_${tl_id}_temp${safe_temp}_x${safe_scale}"
  local run_root="$MATRIX_ROOT/cells/$cell_id"
  local cell_log="$MATRIX_ROOT/logs/${cell_id}.log"

  if [[ "$FORCE_RERUN" != "1" ]] && has_completed_per_tl "$run_root" && ! has_failures "$run_root"; then
    log_status "cell_skip_completed" "{\"group\":\"$group\",\"model_key\":\"$model_key\",\"tl_id\":\"$tl_id\",\"scale\":\"$scale\",\"temperature\":\"$temp\",\"run_root\":\"$run_root\"}"
    return 0
  fi

  if [[ -d "$run_root" ]]; then
    mv "$run_root" "${run_root}.archive_$(date +%Y%m%dT%H%M%S)"
  fi
  mkdir -p "$run_root"

  log_status "cell_start" "{\"group\":\"$group\",\"model_key\":\"$model_key\",\"tl_id\":\"$tl_id\",\"scale\":\"$scale\",\"temperature\":\"$temp\",\"prompt_format\":\"$prompt_format\",\"run_root\":\"$run_root\"}"

  local model_specs="${model_key}|${model_path}|0|single_user|${template_thinking_label}"
  (
    set -euo pipefail
    env \
      RUN_ROOT="$run_root" \
      MODEL_SPECS="$model_specs" \
      TL_ID="$tl_id" \
      DEMAND_SCALE="$scale" \
      TEMPERATURE="$temp" \
      WARMUP_SECONDS="$WARMUP_SECONDS" \
      METRIC_SECONDS="$METRIC_SECONDS" \
      TRIPINFO_DRAIN_SECONDS="$TRIPINFO_DRAIN_SECONDS" \
      DECISION_INTERVAL_SECONDS="$DECISION_INTERVAL_SECONDS" \
      TARGET_PEAK_VPH_PER_ROUTE="$TARGET_PEAK_VPH_PER_ROUTE" \
      TARGET_PEAK_ROUTES_PER_TL="$TARGET_PEAK_ROUTES_PER_TL" \
      ALLOW_NONSTANDARD_WINDOW=0 \
      N_PREDICT="$N_PREDICT" \
      TIMEOUT_SEC="$TIMEOUT_SEC" \
      ONLINE_CONTROL_MODE="$ONLINE_CONTROL_MODE" \
      ACTION_DELAY_CYCLES="$ACTION_DELAY_CYCLES" \
      REASONING_MAX_CHARS="$REASONING_MAX_CHARS" \
      RUN_DEFAULT=0 \
      USE_CHAT_TEMPLATE=0 \
      HF_CHAT_TEMPLATE_MESSAGE_MODE=single_user \
      HF_CHAT_TEMPLATE_ENABLE_THINKING="$template_thinking_label" \
      PROMPT_FORMAT="$prompt_format" \
      HF_DTYPE="$HF_DTYPE" \
      HF_DEVICE_MAP="$HF_DEVICE_MAP" \
      "$SMOKE_SCRIPT"
  ) > "$cell_log" 2>&1

  if has_failures "$run_root"; then
    log_status "cell_complete_with_failures" "{\"group\":\"$group\",\"model_key\":\"$model_key\",\"tl_id\":\"$tl_id\",\"scale\":\"$scale\",\"temperature\":\"$temp\",\"run_root\":\"$run_root\",\"log\":\"$cell_log\"}"
  else
    log_status "cell_complete" "{\"group\":\"$group\",\"model_key\":\"$model_key\",\"tl_id\":\"$tl_id\",\"scale\":\"$scale\",\"temperature\":\"$temp\",\"run_root\":\"$run_root\",\"log\":\"$cell_log\"}"
  fi
}

run_group() {
  local group="$1"
  local model_key="$2"
  local model_path="$3"
  local prompt_format="$4"
  local template_thinking_label="$5"

  log_status "group_start" "{\"group\":\"$group\",\"model_key\":\"$model_key\",\"prompt_format\":\"$prompt_format\"}"
  local tl_id scale temp
  for temp in $TEMPS; do
    for scale in $SCALES; do
      for tl_id in $TLS; do
        run_cell "$group" "$model_key" "$model_path" "$prompt_format" "$template_thinking_label" "$tl_id" "$scale" "$temp"
      done
    done
  done
  log_status "group_complete" "{\"group\":\"$group\",\"model_key\":\"$model_key\"}"
}

write_summary() {
  "$PYTHON_BIN" - "$MATRIX_ROOT" <<'PY'
import csv
import json
import pathlib
import statistics
import sys

root = pathlib.Path(sys.argv[1])
rows = []
for per_tl in sorted(root.glob("cells/*/*/per_tl.jsonl")):
    with per_tl.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            row["cell_run_root"] = str(per_tl.parents[1])
            row["case_dir"] = str(per_tl.parent)
            rows.append(row)

csv_path = root / "matrix_summary.csv"
fields = [
    "cell_run_root",
    "case_dir",
    "controller",
    "model_backend",
    "hf_model_path",
    "tl_id",
    "demand_scale",
    "temperature",
    "prompt_format",
    "model_calls",
    "strict_format_success_rate",
    "strict_control_usable_rate",
    "relaxed_json_success_rate",
    "relaxed_control_usable_rate",
    "repaired_control_usable_rate",
    "plans_queued",
    "delayed_plans_applied",
    "plans_applied_rate",
    "avg_response_time_sec",
    "avg_queue_vehicles",
    "p95_queue_vehicles",
    "max_queue_vehicles",
    "avg_delay_per_vehicle_sec",
    "throughput_veh_per_min",
    "target_tl_att_sec",
    "target_tl_awt_sec",
    "network_att_sec",
    "network_awt_sec",
]
with csv_path.open("w", encoding="utf-8", newline="") as fh:
    writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)

failures = []
for failure_path in sorted(root.glob("cells/*/*/failures.jsonl")):
    if failure_path.stat().st_size > 0:
        failures.append(str(failure_path))

by_model = {}
for row in rows:
    case_dir = pathlib.Path(row["case_dir"]).name
    model = case_dir.split("_reasoning_nextcycle_")[0]
    item = by_model.setdefault(model, {"rows": 0, "strict": [], "relaxed": [], "repaired": [], "response": []})
    item["rows"] += 1
    for key, dest in [
        ("strict_control_usable_rate", "strict"),
        ("relaxed_control_usable_rate", "relaxed"),
        ("repaired_control_usable_rate", "repaired"),
        ("avg_response_time_sec", "response"),
    ]:
        value = row.get(key)
        if isinstance(value, (int, float)):
            item[dest].append(float(value))

summary = {"run_root": str(root), "rows": len(rows), "failure_files": failures, "by_model": {}}
for model, item in by_model.items():
    summary["by_model"][model] = {
        "rows": item["rows"],
        "strict_control_usable_rate_mean": statistics.mean(item["strict"]) if item["strict"] else None,
        "relaxed_control_usable_rate_mean": statistics.mean(item["relaxed"]) if item["relaxed"] else None,
        "repaired_control_usable_rate_mean": statistics.mean(item["repaired"]) if item["repaired"] else None,
        "avg_response_time_sec_mean": statistics.mean(item["response"]) if item["response"] else None,
    }

(root / "matrix_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
md = ["# Chengdu Tuned 3-Model Matrix Summary", "", f"- rows: {len(rows)}", f"- failure files: {len(failures)}", "", "## By Model", ""]
for model, item in summary["by_model"].items():
    md.append(
        f"- `{model}`: rows={item['rows']}, strict={item['strict_control_usable_rate_mean']}, "
        f"relaxed={item['relaxed_control_usable_rate_mean']}, repaired={item['repaired_control_usable_rate_mean']}, "
        f"avg_response={item['avg_response_time_sec_mean']}"
    )
(root / "matrix_summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
PY
}

log_status "matrix_start" "{\"matrix_root\":\"$MATRIX_ROOT\",\"tls\":\"$TLS\",\"scales\":\"$SCALES\",\"temps\":\"$TEMPS\",\"parallel_qwen\":$PARALLEL_QWEN}"

failed_groups=()
if [[ "$PARALLEL_QWEN" == "1" ]]; then
  (run_group "qwen4b" "qwen3_4b_base_tunedcfg" "/root/autodl-tmp/models/Qwen3-4B" "deepsignal_solution_first" "0") > "$MATRIX_ROOT/logs/qwen4b.worker.log" 2>&1 &
  pid4=$!
  echo "$pid4" > "$MATRIX_ROOT/logs/qwen4b.worker.pid"
  (run_group "qwen9b" "qwen35_9b_base_tunedcfg" "/root/autodl-tmp/models/Qwen3.5-9B-Base" "deepsignal" "1") > "$MATRIX_ROOT/logs/qwen9b.worker.log" 2>&1 &
  pid9=$!
  echo "$pid9" > "$MATRIX_ROOT/logs/qwen9b.worker.pid"
  wait "$pid4" || failed_groups+=("qwen4b")
  wait "$pid9" || failed_groups+=("qwen9b")
else
  run_group "qwen4b" "qwen3_4b_base_tunedcfg" "/root/autodl-tmp/models/Qwen3-4B" "deepsignal_solution_first" "0" || failed_groups+=("qwen4b")
  run_group "qwen9b" "qwen35_9b_base_tunedcfg" "/root/autodl-tmp/models/Qwen3.5-9B-Base" "deepsignal" "1" || failed_groups+=("qwen9b")
fi

if [[ "${#failed_groups[@]}" -gt 0 && "$RETRY_FAILED_SEQUENTIAL" == "1" ]]; then
  log_status "qwen_parallel_failed_retry_sequential" "{\"failed_groups\":\"${failed_groups[*]}\"}"
  for failed_group in "${failed_groups[@]}"; do
    if [[ "$failed_group" == "qwen4b" ]]; then
      run_group "qwen4b_retry" "qwen3_4b_base_tunedcfg" "/root/autodl-tmp/models/Qwen3-4B" "deepsignal_solution_first" "0"
    elif [[ "$failed_group" == "qwen9b" ]]; then
      run_group "qwen9b_retry" "qwen35_9b_base_tunedcfg" "/root/autodl-tmp/models/Qwen3.5-9B-Base" "deepsignal" "1"
    fi
  done
elif [[ "${#failed_groups[@]}" -gt 0 ]]; then
  log_status "qwen_parallel_failed_no_retry" "{\"failed_groups\":\"${failed_groups[*]}\"}"
fi

run_group "gemma12b" "gemma3_12b_it_tunedcfg" "/root/autodl-tmp/models/gemma-3-12b-it" "deepsignal_solution_first" "0"
write_summary | tee "$MATRIX_ROOT/logs/summary_build.log"
log_status "matrix_complete" "{\"matrix_root\":\"$MATRIX_ROOT\"}"
