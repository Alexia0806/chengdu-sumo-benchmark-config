# Chengdu SUMO Benchmark Config

This repository contains the Chengdu SUMO scenario files and the Chengdu-specific benchmark launch scripts.

## Layout

- `chengdu/`: standalone SUMO scenario using `chengdu.sumocfg`.
- `chengdu_benchmark/scenarios/sumo_llm/`: same Chengdu scenario in the layout expected by the benchmark runner, using `osm.sumocfg`.
- `scripts/deepsignal_cycleplan_benchmark_chengdu.py`: Chengdu benchmark runner.
- `scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py`: Chengdu benchmark runner with metric logging.
- `scripts/run_chengdu_fixed15_qwen3_4b_base.sh`: AutoDL launch script for Qwen3-4B HF inference.
- `scripts/run_chengdu_fixed15_model_fp16_20260519.sh`: AutoDL launch script for the fp16 GGUF model.
- `scripts/download_qwen3_4b_base_remote.sh`: helper for downloading `Qwen/Qwen3-4B` on the remote host.

## Pull In Another Environment

```bash
git clone <repo-url>
cd <repo-name>
```

For plain SUMO validation:

```bash
sumo -c chengdu/chengdu.sumocfg
```

For the benchmark layout, place or clone this repository at the path expected by the run scripts, or edit `ROOT` in the shell scripts:

```bash
bash scripts/run_chengdu_fixed15_qwen3_4b_base.sh
bash scripts/run_chengdu_fixed15_model_fp16_20260519.sh
```

The shell scripts currently assume the AutoDL paths under `/root/autodl-tmp/` and a SUMO installation at `/usr/share/sumo`.
