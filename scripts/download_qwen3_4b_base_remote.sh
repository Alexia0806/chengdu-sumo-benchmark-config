#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"

export HF_HOME=$HF_HOME
export HUGGINGFACE_HUB_CACHE=$HF_HOME/hub
export HF_ENDPOINT=https://hf-mirror.com
mkdir -p $MODELS_ROOT $HF_HOME

$TSC_CYCLE_ROOT/.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="Qwen/Qwen3-4B",
    local_dir="$MODELS_ROOT/Qwen3-4B",
    local_dir_use_symlinks=False,
    resume_download=True,
)
print("download_complete $MODELS_ROOT/Qwen3-4B")
PY
