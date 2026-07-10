#!/usr/bin/env bash
set -euo pipefail

MAIN_RUN="${MAIN_RUN:-/root/autodl-tmp/tsc-cycle-benchmark/runs/deepsignal_cycleplan/chengdu_unbalanced_x1p2_readme_models_20260701_auto}"
PAR_RUN="${PAR_RUN:-/root/autodl-tmp/tsc-cycle-benchmark/runs/deepsignal_cycleplan/chengdu_unbalanced_x1p2_readme_models_20260701_qwen4b_parallel}"
SCRIPT="${SCRIPT:-/root/autodl-tmp/tsc-cycle-benchmark/scripts/run_chengdu_unbalanced_x1p2_readme_models_20260701.sh}"
SUMMARIZER="${SUMMARIZER:-/root/autodl-tmp/tsc-cycle-benchmark/scripts/summarize_step_metric_windows.py}"
PYTHON_BIN="${PYTHON_BIN:-/root/autodl-tmp/TSC_CYCLE_v1/.venv/bin/python}"
CASE="${CASE:-04_qwen3_4b_base_nochat_repaired_deepsignal_unbalanced_temp02_x1p2}"
LOG="${LOG:-/root/autodl-tmp/codex_patch_20260701/qwen4b_parallel_watch.log}"

mkdir -p "$(dirname "$LOG")" "$PAR_RUN"

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*" | tee -a "$LOG"
}

complete_case() {
  local d="$1/$CASE"
  [[ -d "$d" ]] || return 1
  local per step fail
  per=$(wc -l < "$d/per_tl.jsonl" 2>/dev/null || echo 0)
  step=$(wc -l < "$d/step_metrics.jsonl" 2>/dev/null || echo 0)
  fail=$(wc -l < "$d/failures.jsonl" 2>/dev/null || echo 0)
  [[ "$per" -ge 3 && "$step" -ge 3600 && "$fail" -eq 0 ]]
}

main_case_started() {
  local d="$MAIN_RUN/$CASE"
  [[ -f "$d/config.json" ]] || return 1
  local step
  step=$(wc -l < "$d/step_metrics.jsonl" 2>/dev/null || echo 0)
  [[ "$step" -gt 0 ]]
}

copy_if_safe() {
  if complete_case "$PAR_RUN"; then
    if complete_case "$MAIN_RUN"; then
      log "MAIN_ALREADY_COMPLETE case=$CASE"
      return 0
    fi
    if main_case_started; then
      log "MAIN_CASE_ALREADY_STARTED skip_copy case=$CASE"
      return 1
    fi
    log "COPY_PARALLEL_RESULT_TO_MAIN case=$CASE"
    rm -rf "$MAIN_RUN/$CASE"
    cp -a "$PAR_RUN/$CASE" "$MAIN_RUN/$CASE"
    touch "$MAIN_RUN/$CASE/.copied_from_qwen4b_parallel"
    return 0
  fi
  return 1
}

log "WATCH_START main=$MAIN_RUN parallel=$PAR_RUN"
if complete_case "$MAIN_RUN"; then
  log "MAIN_QWEN4B_ALREADY_COMPLETE"
  exit 0
fi

if ! complete_case "$PAR_RUN"; then
  if [[ ! -f "$PAR_RUN/orchestrator.pid" ]] || ! ps -p "$(cat "$PAR_RUN/orchestrator.pid" 2>/dev/null || echo 0)" >/dev/null 2>&1; then
    log "LAUNCH_QWEN4B_PARALLEL"
    setsid env RUN_ROOT="$PAR_RUN" CASE_KEYS="qwen4b" bash "$SCRIPT" \
      > "$PAR_RUN/orchestrator.nohup.log" 2>&1 < /dev/null &
    echo $! > "$PAR_RUN/orchestrator.pid"
    log "LAUNCHED pid=$(cat "$PAR_RUN/orchestrator.pid")"
  else
    log "PARALLEL_ALREADY_RUNNING pid=$(cat "$PAR_RUN/orchestrator.pid")"
  fi
fi

while true; do
  par_pid=$(cat "$PAR_RUN/orchestrator.pid" 2>/dev/null || echo 0)
  main_pid=$(cat "$MAIN_RUN/orchestrator.pid" 2>/dev/null || echo 0)
  par_per=$(wc -l < "$PAR_RUN/$CASE/per_tl.jsonl" 2>/dev/null || echo 0)
  par_step=$(wc -l < "$PAR_RUN/$CASE/step_metrics.jsonl" 2>/dev/null || echo 0)
  main_per=$(wc -l < "$MAIN_RUN/$CASE/per_tl.jsonl" 2>/dev/null || echo 0)
  main_step=$(wc -l < "$MAIN_RUN/$CASE/step_metrics.jsonl" 2>/dev/null || echo 0)
  par_alive=$(ps -p "$par_pid" >/dev/null 2>&1 && echo 1 || echo 0)
  main_alive=$(ps -p "$main_pid" >/dev/null 2>&1 && echo 1 || echo 0)
  log "STATUS par_pid=$par_pid par_alive=$par_alive par_per=$par_per par_step=$par_step main_pid=$main_pid main_alive=$main_alive main_per=$main_per main_step=$main_step"
  if copy_if_safe; then
    log "COPY_DONE_OR_MAIN_COMPLETE"
    break
  fi
  if [[ "$par_pid" != "0" ]] && [[ "$par_alive" == "0" ]] && ! complete_case "$PAR_RUN"; then
    log "PARALLEL_ENDED_INCOMPLETE leave_main_sequential_to_run"
    exit 0
  fi
  sleep 60
done

main_pid=$(cat "$MAIN_RUN/orchestrator.pid" 2>/dev/null || echo 0)
if ! ps -p "$main_pid" >/dev/null 2>&1; then
  log "REGENERATE_MAIN_WINDOW_METRICS"
  "$PYTHON_BIN" "$SUMMARIZER" "$MAIN_RUN" \
    --window 300:900:metric_300_900 \
    --window 300:1500:metric_300_1500 \
    --output-dir "$MAIN_RUN/window_metrics" >> "$LOG" 2>&1 || true
fi
log "WATCH_DONE"
