#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"

ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
GROUP=$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_fixed15_20260616
TLS_FILE="$GROUP/chengdu_fixed15_tls.csv"
OUT="$GROUP/02_model_fp16_20260519_temp04"

mkdir -p "$OUT"
cd "$ROOT"

PYTHONUNBUFFERED=1 $TSC_CYCLE_ROOT/.venv/bin/python \
  scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py \
  --benchmark-root "$ROOT/DeepSignal-benchmark" \
  --sumo-home "$SUMO_HOME" \
  --scenario sumo_llm \
  --tls-file "$TLS_FILE" \
  --controller model \
  --model-backend llama \
  --gguf-path $MODELS_ROOT/model-fp16-20260519.gguf \
  --llama-server $LLAMA_CPP_ROOT/build-cuda/bin/llama-server \
  --prompt-format deepsignal \
  --no-prefill \
  --temperature 0.4 \
  --warmup-seconds 300 \
  --metric-seconds 2400 \
  --simulation-seconds 2700 \
  --decision-interval-seconds 60 \
  --n-predict 512 \
  --timeout-sec 120 \
  --output-dir "$OUT" \
  --no-log-events-to-stderr
