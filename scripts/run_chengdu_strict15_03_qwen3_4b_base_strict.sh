#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"

ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
GROUP="$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_fixed15_strict_20260616"
TLS_FILE="$GROUP/chengdu_fixed15_tls.csv"
OUT="$GROUP/03_qwen3_4b_base_strict_min_green_temp04"

mkdir -p "$OUT"
cd "$ROOT"

PYTHONUNBUFFERED=1 "$PYTHON_BIN" \
  scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py \
  --benchmark-root "$ROOT/DeepSignal-benchmark" \
  --sumo-home "$SUMO_HOME" \
  --scenario sumo_llm \
  --tls-file "$TLS_FILE" \
  --controller model \
  --model-backend hf \
  --hf-model-path "$MODELS_ROOT/Qwen3-4B" \
  --hf-dtype bfloat16 \
  --hf-device-map auto \
  --prompt-format deepsignal \
  --no-prefill \
  --temperature 0.4 \
  --model-fail-policy min_green \
  --phase-queue-mode raw \
  --queue-threshold 10 \
  --warmup-seconds 300 \
  --metric-seconds 2400 \
  --simulation-seconds 2700 \
  --decision-interval-seconds 60 \
  --n-predict 512 \
  --timeout-sec 120 \
  --output-dir "$OUT" \
  --no-log-events-to-stderr
