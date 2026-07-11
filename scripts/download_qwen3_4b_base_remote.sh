#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env_defaults.sh"

export HF_HOME
export HUGGINGFACE_HUB_CACHE="${HUGGINGFACE_HUB_CACHE:-$HF_HOME/hub}"
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
mkdir -p "$MODELS_ROOT" "$HF_HOME"

"$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path

from huggingface_hub import snapshot_download

target = Path(os.environ["MODELS_ROOT"]) / "Qwen3-4B"
snapshot_download(
    repo_id="Qwen/Qwen3-4B",
    local_dir=str(target),
    local_dir_use_symlinks=False,
    resume_download=True,
)
print(f"download_complete {target}")
PY
