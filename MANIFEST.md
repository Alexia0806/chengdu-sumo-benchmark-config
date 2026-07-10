# Manifest

## Scenario Assets

| Path | Purpose |
| --- | --- |
| `chengdu/chengdu.sumocfg` | Standalone Chengdu SUMO configuration. |
| `chengdu/chengdu.net.xml` | Standalone Chengdu road network. |
| `chengdu/morning_rush_hour.rou.xml` | Standalone main demand file. |
| `chengdu/rush_hour_flow.rou.xml` | Standalone rush-hour demand file. |
| `chengdu/gui-settings.cfg` | Standalone SUMO GUI settings. |
| `chengdu_benchmark/scenarios/sumo_llm/osm.sumocfg` | Benchmark-layout Chengdu SUMO configuration. |
| `chengdu_benchmark/scenarios/sumo_llm/ChengduCity.net.xml` | Benchmark-layout road network. |
| `chengdu_benchmark/scenarios/sumo_llm/morning_rush_hour.rou.xml` | Benchmark-layout main demand file. |
| `chengdu_benchmark/scenarios/sumo_llm/rush_hour_flow.rou.xml` | Benchmark-layout rush-hour demand file. |
| `chengdu_benchmark/scenarios/sumo_llm/gui-settings.cfg` | Benchmark-layout GUI settings. |

## Core Python Entry Points

| Path | Purpose |
| --- | --- |
| `scripts/deepsignal_cycleplan_benchmark_chengdu.py` | Closed-loop Chengdu benchmark runner. |
| `scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py` | Metrics-focused runner with step logs, target peak demand, tripinfo ATT/AWT, and aggregates. |
| `scripts/select_chengdu_tls_candidates.py` | Select candidate Chengdu traffic-light controllers from the SUMO network. |
| `scripts/filter_chengdu_tls_probe_results.py` | Filter short-probe results into final TLS candidate sets. |
| `scripts/rank_chengdu_tls_benchmark_candidates.py` | Rank candidate TLS benchmark outputs. |
| `scripts/recompute_target_peak_fairness_metrics.py` | Recompute target-peak fairness metrics from completed runs. |
| `scripts/summarize_chengdu_peak_matrix.py` | Summarize Chengdu peak matrix results. |
| `scripts/summarize_step_metric_windows.py` | Recompute metric windows from `step_metrics.jsonl`. |

## Operational Scripts

`scripts/run_*.sh`, `scripts/watch_*.sh`, `run_gpt_oss_20b_2tl_unbalanced_x1p5*.sh`, and `watch_gpt_oss_20b_solution.sh` are AutoDL-oriented launch/watch helpers for recorded Chengdu experiment workflows. They source `scripts/env_defaults.sh` so repository, model, SUMO, cache, and run-output paths can be overridden with environment variables instead of editing scripts.

## Documentation And Tests

| Path | Purpose |
| --- | --- |
| `README.md` | Project setup, usage, configuration, and development guide. |
| `README_DeepSignal_20260519_HF.md` | English model-card draft for DeepSignal-CyclePlan-4B-V2. |
| `README_DeepSignal_20260519_HF_zh.md` | Chinese model-card draft for DeepSignal-CyclePlan-4B-V2. |
| `images/` | Comparison images referenced by model-card drafts. |
| `lmstudio_deepsignal_20260519_chat_template.jinja` | LM Studio chat template for the DeepSignal prompt format. |
| `tests/` | Lightweight unit tests for parsing, validation, and route-selection helpers. |
| `requirements.yaml` | System, Python, backend, and path-variable requirements for full closed-loop benchmark runs. |

## Excluded From Git

Local run outputs, caches, copied upstream repositories, temporary files, model artifacts, dashboard exports, and large SUMO tripinfo outputs are intentionally excluded. Keep generated files under `runs/`, `outputs/`, or `tmp/`.
