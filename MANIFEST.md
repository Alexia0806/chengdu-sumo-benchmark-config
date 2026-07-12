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
| `scripts/check_repo_quality.sh` | Run lightweight shell syntax, Python compile, unit-test, and hardcoded-path checks. |
| `scripts/env_defaults.sh` | Shared runtime path defaults for operational shell launchers. |

## Operational Scripts

Current launchers are intentionally small in number:

| Path | Purpose |
| --- | --- |
| `scripts/run_chengdu_3tl_att_awt_relaxed_x1p8_matrix.sh` | Formal three-TL matrix runner. |
| `scripts/run_chengdu_tls_short_probe_fixed_maxpressure.sh` | Short fixed/max-pressure probe for congestion sanity checks and TLS screening. |
| `scripts/lib/chengdu_runner_common.sh` | Shared shell helpers for runner logging, workspace setup, SUMO config resolution, TLS CSV writing, and matrix metadata formatting. |

Historical one-off launch/watch scripts live under `scripts/archive/` for traceability only. They are excluded from clean packages.

## Documentation And Tests

| Path | Purpose |
| --- | --- |
| `README.md` | Project setup, usage, configuration, and development guide. |
| `lmstudio_deepsignal_20260519_chat_template.jinja` | LM Studio chat template for the DeepSignal prompt format. |
| `tests/` | Lightweight unit tests for parsing, validation, and route-selection helpers. |
| `requirements.yaml` | System, Python, backend, and path-variable requirements for full closed-loop benchmark runs. |

## Excluded From Git

Local run outputs, caches, copied upstream repositories, temporary files, model artifacts, dashboard exports, and large SUMO tripinfo outputs are intentionally excluded. Keep generated files under `runs/`, `outputs/`, or `tmp/`.

## Excluded From Clean Packages

Clean `git archive` packages also exclude archived launchers, historical model-card drafts, comparison images, local output directories, logs, CSV/JSONL summaries, and compressed run artifacts. The package should contain runnable code, SUMO scenarios, tests, configuration docs, and current launch scripts, not previous experiment results.
