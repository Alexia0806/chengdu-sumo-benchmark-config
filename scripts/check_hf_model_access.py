#!/usr/bin/env python3
from __future__ import annotations

import sys

from huggingface_hub import HfApi


def main() -> int:
    repo = sys.argv[1] if len(sys.argv) > 1 else "google/gemma-3-4b-it"
    api = HfApi()
    try:
        info = api.model_info(repo, files_metadata=True)
    except Exception as exc:
        print("ERROR", type(exc).__name__, str(exc))
        return 1

    print("OK", info.modelId, "gated", getattr(info, "gated", None), "private", getattr(info, "private", None))
    total = 0
    for sibling in info.siblings:
        name = sibling.rfilename
        size = getattr(sibling, "size", None) or 0
        if name.endswith((".safetensors", ".json", ".model", ".txt")) or name.startswith("tokenizer"):
            total += size
            print(size, name)
    print("TOTAL_ALLOW_GB", round(total / 1024**3, 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
