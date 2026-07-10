# Chengdu SUMO Benchmark Config

This repository packages the Chengdu SUMO scenario and the Chengdu-specific benchmark scripts used to evaluate cycle-level traffic signal controllers. The main workflow runs SUMO through TraCI, optionally calls a model backend, applies generated green-time plans, and writes JSONL/CSV summaries for queue, delay, tripinfo ATT/AWT, and control-usability metrics.

The repository is intentionally focused on the Chengdu benchmark assets. Full model weights, local run outputs, copied upstream repositories, and generated dashboards are not part of the tracked source tree.

## Core Features

- Chengdu SUMO scenario in both standalone and benchmark-runner layouts.
- Closed-loop benchmark runner for fixed, model, and max-pressure controllers.
- Metrics runner with step-level JSONL logging, target-peak synthetic demand, tripinfo ATT/AWT parsing, and aggregate summaries.
- Candidate traffic-light selection, probe filtering, fairness recomputation, and window summarization utilities.
- AutoDL-oriented shell launchers for common Chengdu experiment matrices and watcher workflows.
- Model-card support files and comparison images for DeepSignal-CyclePlan-4B-V2 documentation.

## Tech Stack

- Python 3.10+ style scripts using the standard library for orchestration, XML/CSV/JSON processing, and tests.
- SUMO and TraCI for simulation.
- Optional model backends:
  - llama.cpp server for GGUF models.
  - Hugging Face `transformers`/`torch`/`peft` for local HF checkpoints and adapters.
  - OpenAI-compatible chat/completions endpoints.
- Shell scripts for remote AutoDL batch execution and result watching.

## Directory Structure

```text
.
├── chengdu/                              # Standalone SUMO config: chengdu.sumocfg
├── chengdu_benchmark/scenarios/sumo_llm/ # Benchmark-runner Chengdu scenario layout
├── images/                               # README/model-card comparison images
├── scripts/
│   ├── deepsignal_cycleplan_benchmark_chengdu.py
│   ├── deepsignal_cycleplan_benchmark_chengdu_metrics.py
│   ├── select_chengdu_tls_candidates.py
│   ├── filter_chengdu_tls_probe_results.py
│   ├── summarize_step_metric_windows.py
│   └── run_*.sh / watch_*.sh             # Remote experiment launch/watch scripts
├── tests/                                # Lightweight helper tests, no SUMO/model required
├── requirements.yaml                     # System/backend/path requirements reference
├── README_DeepSignal_20260519_HF*.md      # Model-card drafts
├── MANIFEST.md                           # Tracked source asset manifest
└── lmstudio_deepsignal_20260519_chat_template.jinja
```

Generated directories such as `runs/`, `outputs/`, `tmp/`, and copied upstream repositories are ignored by Git.

## Installation

Clone the repository and enter it:

```bash
git clone <repo-url>
cd chengdu-sumo-benchmark-config
```

For local SUMO runs, install SUMO and expose its tools:

```bash
export SUMO_HOME=/path/to/sumo
export PYTHONPATH="$SUMO_HOME/tools:${PYTHONPATH:-}"
```

For model-backed runs, install only the backend you need. Examples:

```bash
# Hugging Face backend
python3 -m pip install torch transformers peft

# Hugging Face model metadata helper
python3 -m pip install huggingface_hub
```

The repository does not currently include a pinned `requirements.txt`; remote scripts assume the AutoDL environment used during the experiments.

## Running

Validate the standalone SUMO scenario:

```bash
sumo -c chengdu/chengdu.sumocfg
```

List available benchmark scenarios from the tracked Chengdu benchmark layout:

```bash
python3 scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py \
  --benchmark-root chengdu_benchmark \
  --scenario sumo_llm \
  --list-scenarios
```

List traffic-light IDs:

```bash
python3 scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py \
  --benchmark-root chengdu_benchmark \
  --scenario sumo_llm \
  --list-tl-ids
```

Run a small fixed-controller local smoke, assuming SUMO/TraCI is available:

```bash
python3 scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py \
  --benchmark-root chengdu_benchmark \
  --scenario sumo_llm \
  --tl-id cluster_4550018629_4550018932 \
  --controller fixed \
  --output-dir runs/local_fixed_cluster_smoke \
  --warmup-seconds 10 \
  --metric-seconds 20 \
  --allow-nonstandard-window
```

Run a Hugging Face model-backed case:

```bash
python3 scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py \
  --benchmark-root chengdu_benchmark \
  --scenario sumo_llm \
  --tl-id cluster_4550018629_4550018932 \
  --controller model \
  --model-backend hf \
  --hf-model-path /path/to/model \
  --prompt-format deepsignal_json \
  --online-control-mode strict \
  --output-dir runs/local_hf_cluster
```

Remote matrix scripts live under `scripts/run_*.sh`. They source `scripts/env_defaults.sh`, which derives path defaults from `AUTODL_ROOT`, `PROJECT_ROOT`, `MODELS_ROOT`, `RUNS_ROOT`, `SUMO_HOME`, and backend-specific variables. Override those variables before invoking a launcher when running outside the original AutoDL layout.

## Configuration

Important runner options:

- `--benchmark-root`: root containing `scenarios/<name>/<config>`. Use `chengdu_benchmark` for the tracked Chengdu scenario.
- `--scenario`: scenario name; the tracked scenario is `sumo_llm`.
- `--tl-id` or `--tls-file`: choose one or more traffic-light controllers.
- `--controller`: `fixed`, `model`, or `max_pressure`.
- `--model-backend`: `llama`, `hf`, or `openai` when `--controller model` is used.
- `--warmup-seconds` and `--metric-seconds`: metric window configuration.
- `--demand-scale`: uniform demand multiplier in the metrics runner.
- `--target-peak-*`: add synthetic target-peak flows for selected TLs.
- `--record-step-metrics`: write per-second `step_metrics.jsonl`.
- `--tripinfo-metrics`: parse SUMO tripinfo for ATT/AWT summaries.

Useful environment variables:

- `AUTODL_ROOT`: base directory used by remote-style launch scripts; defaults to `$HOME/autodl-tmp`.
- `SUMO_HOME`: SUMO installation root; its `tools` directory provides TraCI.
- `PROJECT_ROOT`: remote shell-script repository root.
- `MODELS_ROOT`: local directory containing model checkpoints.
- `RUNS_ROOT`: default parent for benchmark outputs.
- `DEFAULT_TARGET_TLS`: default three-intersection matrix set; currently `cluster_4550018629_4550018932 cluster_432429373_5213238455 cluster_1916386555_432429395`.
- `TARGET_TLS`: per-run override for matrix traffic-light IDs.
- `TARGET_PEAK_ROUTE_SELECTION`: synthetic target-peak route selection policy; formal matrices default to `diverse_sources`.
- `WARMUP_SECONDS` / `METRIC_SECONDS`: remote matrix runner window; the default `300 / 1200` reports the `300-1500s` metric interval.
- `PYTHON_BIN`: Python executable used by remote launchers.
- `LLAMA_SERVER`: llama.cpp server executable for GGUF workflows.
- `HF_ATTN_IMPLEMENTATION` / `HF_EXPERTS_IMPLEMENTATION`: optional Hugging Face loading knobs used by the metrics runner.
- `OPENAI_API_KEY` and `--openai-base-url`: OpenAI-compatible backend credentials and endpoint.

See `requirements.yaml` for the full system, Python, backend, and path-variable checklist.

## Common Commands

```bash
# Syntax-check all Python scripts and tests
python3 -m compileall -q scripts tests

# Run lightweight unit tests
python3 -m unittest discover -s tests -v

# Select candidate Chengdu traffic lights
python3 scripts/select_chengdu_tls_candidates.py \
  --sumocfg chengdu_benchmark/scenarios/sumo_llm/osm.sumocfg \
  --output-dir outputs/chengdu_tls_candidates

# Summarize per-step metric windows from a completed run
python3 scripts/summarize_step_metric_windows.py \
  runs/<run-dir> \
  --output-dir outputs/window_metrics
```

## Example Workflow

1. Select or provide target TL IDs.
2. Run a fixed or max-pressure baseline to confirm SUMO and output paths.
3. Run model-backed cases with `--controller model`.
4. Inspect `summary.json`, `per_tl.jsonl`, `model_calls.jsonl`, `failures.jsonl`, and optional `step_metrics.jsonl`.
5. Recompute windows or fairness metrics with the helper scripts when comparing runs.

## Development Notes

- Keep generated outputs under ignored paths: `runs/`, `outputs/`, or `tmp/`.
- Prefer adding small, backend-free tests around parsing, validation, and aggregation helpers.
- Do not commit model weights, SUMO run dumps, copied upstream repositories, or AutoDL cache artifacts.
- Shell scripts are operational experiment launchers; preserve their defaults unless changing a documented experiment workflow.
- The metrics runner validates common invalid boundaries early, but full simulation correctness still depends on SUMO availability and scenario files.

## Known Limitations

- Full benchmark runs are not hermetic; they require SUMO, model files, and backend-specific dependencies.
- Full remote matrices still need external model files and backend runtimes; configure paths through `scripts/env_defaults.sh` variables or `requirements.yaml`.
- The tracked repository contains only the Chengdu scenario bundle, not the full upstream `DeepSignal-benchmark` repository.
- Local tests do not execute SUMO or model inference; they cover parsing, validation, and route-selection helpers only.
