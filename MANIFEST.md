# Manifest

## Included

| Path | Purpose |
| --- | --- |
| `chengdu/chengdu.sumocfg` | Standalone SUMO configuration. |
| `chengdu/chengdu.net.xml` | Chengdu road network. |
| `chengdu/morning_rush_hour.rou.xml` | Main route demand file. |
| `chengdu/rush_hour_flow.rou.xml` | Additional rush-hour route demand file. |
| `chengdu/gui-settings.cfg` | SUMO GUI settings. |
| `chengdu_benchmark/scenarios/sumo_llm/osm.sumocfg` | Benchmark scenario configuration. |
| `chengdu_benchmark/scenarios/sumo_llm/ChengduCity.net.xml` | Benchmark-layout network file. |
| `chengdu_benchmark/scenarios/sumo_llm/morning_rush_hour.rou.xml` | Benchmark-layout main route file. |
| `chengdu_benchmark/scenarios/sumo_llm/rush_hour_flow.rou.xml` | Benchmark-layout route file. |
| `chengdu_benchmark/scenarios/sumo_llm/gui-settings.cfg` | Benchmark-layout SUMO GUI settings. |
| `scripts/deepsignal_cycleplan_benchmark_chengdu.py` | Chengdu benchmark runner. |
| `scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py` | Chengdu benchmark runner with metrics. |
| `scripts/download_qwen3_4b_base_remote.sh` | Remote Qwen3-4B download helper. |
| `scripts/run_chengdu_fixed15_qwen3_4b_base.sh` | Remote Qwen3-4B benchmark launcher. |
| `scripts/run_chengdu_fixed15_model_fp16_20260519.sh` | Remote GGUF benchmark launcher. |

## Excluded

The repository intentionally excludes local run outputs, Python caches, copied upstream project directories, model artifacts, and zipped/manual completion outputs.
