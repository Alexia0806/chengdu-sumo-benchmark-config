#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec bash "$REPO_ROOT/src/sumo_benchmark/shell/run_chengdu_3tl_att_awt_relaxed_x1p8_matrix.sh" "$@"

