#!/usr/bin/env bash
set -euo pipefail

ROOT=/root/autodl-tmp/tsc-cycle-benchmark
GROUP=/root/autodl-tmp/tsc-cycle-benchmark/runs/deepsignal_cycleplan/chengdu_fixed15_strict_20260616
TLS_FILE="$GROUP/chengdu_fixed15_tls.csv"
OUT="$GROUP/00_default_sumo"

mkdir -p "$OUT"
cd "$ROOT"

PYTHONUNBUFFERED=1 /root/autodl-tmp/TSC_CYCLE_v1/.venv/bin/python \
  scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py \
  --benchmark-root "$ROOT/DeepSignal-benchmark" \
  --sumo-home /usr/share/sumo \
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
