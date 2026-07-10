#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"

ROOT="${PROJECT_ROOT:-$REPO_ROOT}"
GROUP=$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_fixed15_strict_20260616
TLS_FILE="$GROUP/chengdu_fixed15_tls.csv"
OUT="$GROUP/00_default_sumo"

mkdir -p "$OUT"
cd "$ROOT"

PYTHONUNBUFFERED=1 $TSC_CYCLE_ROOT/.venv/bin/python \
  scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py \
  --benchmark-root "$ROOT/DeepSignal-benchmark" \
  --sumo-home "$SUMO_HOME" \
  --scenario sumo_llm \
  --tls-file "$TLS_FILE" \
  --controller fixed \
  --phase-queue-mode raw \
  --queue-threshold 10 \
  --warmup-seconds 300 \
  --metric-seconds 2400 \
  --simulation-seconds 2700 \
  --decision-interval-seconds 60 \
  --output-dir "$OUT" \
  --no-log-events-to-stderr
