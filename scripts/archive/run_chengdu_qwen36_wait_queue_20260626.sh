#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/env_defaults.sh"

PROJECT_ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
RUNNER_SH="${RUNNER_SH:-$PROJECT_ROOT/scripts/run_chengdu_backfill_aligned_matrix_20260626.sh}"
PYTHON_BIN="${PYTHON_BIN:-$SYSTEM_PYTHON_BIN}"
MODEL_DIR="${MODEL_DIR:-$MODELS_ROOT/Qwen3.6-27B}"
QUEUE_ROOT="${QUEUE_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_qwen36_backfill_wait_queue_20260626_$(date +%Y%m%dT%H%M%S)}"
LOG_DIR="$QUEUE_ROOT/logs"
WAIT_LOG="$LOG_DIR/wait_qwen36_complete.log"
ORCH_LOG="$LOG_DIR/queue.log"

SCENARIOS="${SCENARIOS:-unbalanced_x1p5 balanced_x1p5 balanced_x1p2 unbalanced_x1p2}"
TEMPERATURES="${TEMPERATURES:-0.1 0.2}"
RUN_DEFAULT="${RUN_DEFAULT:-0}"
RUN_UNBALANCED_X15_FULL3TL="${RUN_UNBALANCED_X15_FULL3TL:-0}"
CHECK_INTERVAL_SEC="${CHECK_INTERVAL_SEC:-120}"
MAX_WAIT_SEC="${MAX_WAIT_SEC:-43200}"

mkdir -p "$LOG_DIR"
echo "$$" > "$QUEUE_ROOT/watcher.pid"

log_event() {
  printf '[%s] %s\n' "$(date -Is)" "$1" | tee -a "$ORCH_LOG"
}

check_model_complete() {
  "$PYTHON_BIN" - "$MODEL_DIR" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
if not root.exists():
    print(f"missing_dir {root}")
    raise SystemExit(1)
config = root / "config.json"
if not config.exists():
    print("missing config.json")
    raise SystemExit(1)
incomplete = sorted(p.name for p in root.glob(".*.safetensors.*"))
if incomplete:
    print("incomplete_temp_files " + ",".join(incomplete[:8]))
    raise SystemExit(1)
index = root / "model.safetensors.index.json"
if index.exists():
    data = json.loads(index.read_text())
    expected = sorted(set(data.get("weight_map", {}).values()))
    missing = [name for name in expected if not (root / name).exists()]
    if missing:
        print("missing_index_files " + ",".join(missing[:8]))
        raise SystemExit(1)
    print(f"complete index_files={len(expected)} size_gb={sum((root / n).stat().st_size for n in expected)/1024**3:.2f}")
    raise SystemExit(0)
safetensors = sorted(root.glob("model-*.safetensors"))
if len(safetensors) < 15:
    print(f"missing_shards have={len(safetensors)} need>=15")
    raise SystemExit(1)
print(f"complete shard_count={len(safetensors)} size_gb={sum(p.stat().st_size for p in safetensors)/1024**3:.2f}")
PY
}

check_runtime_ready() {
  "$PYTHON_BIN" - <<'PY'
import importlib.util
missing = [
    name
    for name in ("torch", "transformers", "accelerate", "safetensors", "traci", "sumolib", "numpy")
    if importlib.util.find_spec(name) is None
]
if missing:
    raise SystemExit("missing_runtime_modules=" + ",".join(missing))
PY
}

wait_for_model() {
  local elapsed=0
  while (( elapsed <= MAX_WAIT_SEC )); do
    if msg="$(check_model_complete 2>&1)"; then
      echo "[$(date -Is)] MODEL_COMPLETE $msg" | tee -a "$WAIT_LOG"
      return 0
    fi
    echo "[$(date -Is)] MODEL_WAIT elapsed=${elapsed}s status=$msg" | tee -a "$WAIT_LOG"
    sleep "$CHECK_INTERVAL_SEC"
    elapsed=$((elapsed + CHECK_INTERVAL_SEC))
  done
  echo "[$(date -Is)] MODEL_WAIT_TIMEOUT max_wait=${MAX_WAIT_SEC}s" | tee -a "$WAIT_LOG"
  return 1
}

log_event "WATCHER_START queue_root=$QUEUE_ROOT model_dir=$MODEL_DIR scenarios='$SCENARIOS' temps='$TEMPERATURES' run_default=$RUN_DEFAULT"
check_runtime_ready
wait_for_model

log_event "QWEN36_RUN_START"
RUN_ROOT="$QUEUE_ROOT/qwen36" \
SCENARIOS="$SCENARIOS" \
TEMPERATURES="$TEMPERATURES" \
RUN_DEFAULT="$RUN_DEFAULT" \
RUN_UNBALANCED_X15_FULL3TL="$RUN_UNBALANCED_X15_FULL3TL" \
MODEL_KEYS="qwen36" \
PYTHON_BIN="$PYTHON_BIN" \
HF_DTYPE="bfloat16" \
HF_DEVICE_MAP="auto" \
DRY_RUN=0 \
bash "$RUNNER_SH" 2>&1 | tee "$LOG_DIR/qwen36.console.log"
rc=${PIPESTATUS[0]}
log_event "QWEN36_RUN_DONE rc=$rc run_root=$QUEUE_ROOT/qwen36"
exit "$rc"
