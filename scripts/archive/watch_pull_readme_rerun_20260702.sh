#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/env_defaults.sh"

SSH_PORT="${SSH_PORT:-25480}"
SSH_HOST="${SSH_HOST:-root@connect.westd.seetacloud.com}"
REMOTE_RUN_ROOT="${REMOTE_RUN_ROOT:-$PROJECT_ROOT/runs/deepsignal_cycleplan/chengdu_unbalanced_x1p2_readme_models_20260701_auto}"
REMOTE_PY="${REMOTE_PY:-$TSC_CYCLE_ROOT/.venv/bin/python}"
REMOTE_SUMMARIZER="${REMOTE_SUMMARIZER:-$PROJECT_ROOT/scripts/summarize_step_metric_windows.py}"
LOCAL_DEST="${LOCAL_DEST:-outputs/remote_benchmark_results_20260702/readme_unbalanced_x1p2_rerun_models}"
POLL_SECONDS="${POLL_SECONDS:-300}"

CASES=(
  "03_model_fp16_20260519_unbalanced_temp02_x1p2"
  "02_qwen35_9b_base_nochat_repaired_deepsignal_unbalanced_temp02_x1p2"
  "05_gemma3_12b_it_nochat_repaired_deepsignal_unbalanced_temp02_x1p2"
  "04_qwen3_4b_base_nochat_repaired_deepsignal_unbalanced_temp02_x1p2"
)

ssh_cmd() {
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=25 -p "$SSH_PORT" "$SSH_HOST" "$@"
}

remote_status() {
  local case_list
  case_list="$(printf '%s\n' "${CASES[@]}")"
  ssh_cmd "REMOTE_RUN_ROOT='$REMOTE_RUN_ROOT' CASE_LIST='$case_list' bash -s" <<'REMOTE'
set -euo pipefail
echo "time=$(date -Is)"
main_pid="$(cat "$REMOTE_RUN_ROOT/orchestrator.pid" 2>/dev/null || true)"
if [[ -n "$main_pid" ]] && kill -0 "$main_pid" 2>/dev/null; then
  echo "main_alive=1"
else
  echo "main_alive=0"
fi
nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader 2>/dev/null | sed 's/^/gpu=/' || true
all_done=1
any_fail=0
while IFS= read -r case_name; do
  [[ -z "$case_name" ]] && continue
  case_dir="$REMOTE_RUN_ROOT/$case_name"
  per=NA
  step=NA
  fail=NA
  [[ -f "$case_dir/per_tl.jsonl" ]] && per="$(wc -l < "$case_dir/per_tl.jsonl")"
  [[ -f "$case_dir/step_metrics.jsonl" ]] && step="$(wc -l < "$case_dir/step_metrics.jsonl")"
  [[ -f "$case_dir/failures.jsonl" ]] && fail="$(wc -l < "$case_dir/failures.jsonl")"
  echo "case=$case_name per_tl=$per step=$step fail=$fail"
  if [[ "$per" != "3" || "$step" != "3600" || "$fail" != "0" ]]; then
    all_done=0
  fi
  if [[ "$fail" != "0" && "$fail" != "NA" ]]; then
    any_fail=1
  fi
done <<< "$CASE_LIST"
echo "all_done=$all_done"
echo "any_fail=$any_fail"
tail -8 "$REMOTE_RUN_ROOT/logs/orchestrator.log" 2>/dev/null | sed 's/^/log=/' || true
REMOTE
}

remote_all_done() {
  local status="$1"
  grep -q '^all_done=1$' <<< "$status" && grep -q '^any_fail=0$' <<< "$status"
}

rerun_remote_summary() {
  ssh_cmd "REMOTE_RUN_ROOT='$REMOTE_RUN_ROOT' REMOTE_PY='$REMOTE_PY' REMOTE_SUMMARIZER='$REMOTE_SUMMARIZER' bash -s" <<'REMOTE'
set -euo pipefail
"$REMOTE_PY" "$REMOTE_SUMMARIZER" "$REMOTE_RUN_ROOT" \
  --window 300:900:metric_300_900 \
  --window 300:1500:metric_300_1500 \
  --output-dir "$REMOTE_RUN_ROOT/window_metrics"
REMOTE
}

pull_results() {
  mkdir -p "$LOCAL_DEST"
  rsync -az --delete \
    -e "ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=25 -p $SSH_PORT" \
    "$SSH_HOST:$REMOTE_RUN_ROOT/" \
    "$LOCAL_DEST/"
}

postprocess_local() {
  python3 scripts/summarize_step_metric_windows.py "$LOCAL_DEST" \
    --window 300:900:metric_300_900 \
    --window 300:1500:metric_300_1500 \
    --output-dir "$LOCAL_DEST/window_metrics"
  python3 outputs/readme_candidate_dashboards/build_rerun_readme_alignment.py \
    --run-root "$LOCAL_DEST"
}

main() {
  while true; do
    if ! status="$(remote_status)"; then
      echo "time=$(date '+%Y-%m-%dT%H:%M:%S%z')"
      echo "status_error=remote_status_failed"
      sleep "$POLL_SECONDS"
      continue
    fi
    printf '%s\n' "$status"
    if remote_all_done "$status"; then
      echo "status=complete"
      break
    fi
    sleep "$POLL_SECONDS"
  done
  rerun_remote_summary
  pull_results
  postprocess_local
  echo "local_dest=$LOCAL_DEST"
  echo "alignment_csv=outputs/readme_candidate_dashboards/deepsignal_unbal_x1p2_rerun_exact_300_900_300_1500.csv"
  echo "comparison_csv=outputs/readme_candidate_dashboards/deepsignal_unbal_x1p2_rerun_vs_previous_300_900.csv"
  echo "alignment_md=outputs/readme_candidate_dashboards/deepsignal_unbal_x1p2_rerun_alignment.md"
}

main "$@"
