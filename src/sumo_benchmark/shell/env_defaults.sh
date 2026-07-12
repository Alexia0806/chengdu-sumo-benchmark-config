#!/usr/bin/env bash
# Shared path defaults for Chengdu benchmark launchers.
#
# Override any value before invoking a script, for example:
#   AUTODL_ROOT=/data PROJECT_ROOT=/data/tsc-cycle-benchmark bash scripts/run_...

if [[ -n "${CHENGDU_ENV_DEFAULTS_LOADED:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi
CHENGDU_ENV_DEFAULTS_LOADED=1

SHELL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SHELL_DIR/../../.." && pwd)"
SCRIPT_DIR="$REPO_ROOT/scripts"

: "${AUTODL_ROOT:=${HOME:-.}/autodl-tmp}"
: "${PROJECT_ROOT:=$REPO_ROOT}"
: "${TSC_CYCLE_ROOT:=$AUTODL_ROOT/TSC_CYCLE_v1}"
: "${MODELS_ROOT:=$AUTODL_ROOT/models}"
: "${RUNS_ROOT:=$PROJECT_ROOT/runs/deepsignal_cycleplan}"
if [[ -n "${BENCH_ROOT:-}" ]]; then
  BENCH_ROOT_WAS_SET=1
else
  BENCH_ROOT_WAS_SET=0
fi
: "${BENCH_ROOT:=$PROJECT_ROOT/chengdu_benchmark}"
: "${DEEPSIGNAL_BENCH_ROOT:=$PROJECT_ROOT/DeepSignal-benchmark}"
: "${DEFAULT_TARGET_TLS:=cluster_4550018629_4550018932 cluster_432429373_5213238455 cluster_1916386555_432429395}"
: "${DEFAULT_UNBALANCED_X15_TLS:=cluster_432429373_5213238455}"
: "${DEFAULT_TARGET_PEAK_ROUTE_SELECTION:=diverse_sources}"
if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_BIN_WAS_SET=1
else
  PYTHON_BIN_WAS_SET=0
  PYTHON_BIN="$TSC_CYCLE_ROOT/.venv/bin/python"
fi
if [[ -z "${SYSTEM_PYTHON_BIN:-}" ]]; then
  SYSTEM_PYTHON_BIN="$(command -v python3 || command -v python || printf 'python3')"
fi
if [[ "$PYTHON_BIN_WAS_SET" == "0" && ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$SYSTEM_PYTHON_BIN"
fi
: "${LLAMA_CPP_ROOT:=$AUTODL_ROOT/llama.cpp.vendor}"
: "${LLAMA_SERVER:=$LLAMA_CPP_ROOT/build-cuda/bin/llama-server}"
: "${HF_HOME:=$AUTODL_ROOT/hf-cache}"
: "${HUGGINGFACE_HUB_CACHE:=$HF_HOME/hub}"
: "${WATCH_ROOT:=$AUTODL_ROOT/watchers}"
: "${PATCH_ROOT:=$AUTODL_ROOT/codex_patch_20260701}"
: "${TMP_ROOT:=${TMPDIR:-/tmp}}"

if [[ -z "${SUMO_HOME:-}" ]]; then
  if command -v sumo >/dev/null 2>&1; then
    sumo_bin="$(command -v sumo)"
    sumo_candidate="$(cd "$(dirname "$sumo_bin")/.." 2>/dev/null && pwd)"
    if [[ -d "$sumo_candidate/tools" ]]; then
      SUMO_HOME="$sumo_candidate"
    fi
  fi
fi
if [[ -z "${SUMO_HOME:-}" ]]; then
  SUMO_HOME="."
fi
export SCRIPT_DIR REPO_ROOT
export AUTODL_ROOT PROJECT_ROOT TSC_CYCLE_ROOT MODELS_ROOT RUNS_ROOT
export BENCH_ROOT BENCH_ROOT_WAS_SET DEEPSIGNAL_BENCH_ROOT DEFAULT_TARGET_TLS DEFAULT_UNBALANCED_X15_TLS
export DEFAULT_TARGET_PEAK_ROUTE_SELECTION
export PYTHON_BIN SYSTEM_PYTHON_BIN
export LLAMA_CPP_ROOT LLAMA_SERVER
export HF_HOME HUGGINGFACE_HUB_CACHE WATCH_ROOT PATCH_ROOT
export TMP_ROOT
export SUMO_HOME
