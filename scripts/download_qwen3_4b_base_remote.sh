#!/usr/bin/env bash
set -euo pipefail

export HF_HOME=/root/autodl-tmp/hf-cache
export HUGGINGFACE_HUB_CACHE=/root/autodl-tmp/hf-cache/hub
export HF_ENDPOINT=https://hf-mirror.com
mkdir -p /root/autodl-tmp/models /root/autodl-tmp/hf-cache

/root/autodl-tmp/TSC_CYCLE_v1/.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="Qwen/Qwen3-4B",
    local_dir="/root/autodl-tmp/models/Qwen3-4B",
    local_dir_use_symlinks=False,
    resume_download=True,
)
print("download_complete /root/autodl-tmp/models/Qwen3-4B")
PY
