# Chengdu Benchmark Goal

## Goal

Build a reproducible Chengdu traffic-signal benchmark that measures whether base and fine-tuned LLM controllers can produce executable next-cycle signal plans, and whether those plans improve traffic metrics versus SUMO default under controlled Chengdu scenarios.

## Current Stage

Run a J54-only smoke test before changing the full experiment matrix.

- Intersection: `J54`
- Scenario: `sumo_llm`
- Demand: `1.2x` plus target peak traffic injected into `J54`
- Models: `qwen3_4b_base`, then `qwen35_9b_base`
- Baseline: skipped for this smoke by request; compare to prior same-condition SUMO default only after model control is validated
- Temperature: `0.1`
- Max new tokens: `2048`
- Prompt format: `deepsignal_solution_first`
- Reasoning budget: `160` Chinese characters max inside `<start_working_out>`
- Control mode: `repaired`, with strict/relaxed/repaired rates reported separately
- Action timing: generated plan is queued and applied at the next decision cycle (`action_delay_cycles=1`)
- RUN_DEFAULT: `0`

## Prompt Requirements

- First output the executable `<SOLUTION>` block, then output short reasoning.
- Keep reasoning short.
- Do not repeat the input JSON.
- Do not output Markdown fences, examples, or duplicate JSON.
- Always emit a complete `<SOLUTION>...</SOLUTION>` block.
- `<SOLUTION>` must contain only a JSON list of `{"phase_id": int, "final": int}` items.
- The plan must cover exactly the current phase IDs and obey each phase's `min_green`/`max_green`.

## Control-Rate Evaluation

Report three separate control rates:

- `strict_control_usable_rate`: exact protocol success, including required tags and schema.
- `relaxed_control_usable_rate`: valid executable plan found from the model output even if strict tags are imperfect.
- `repaired_control_usable_rate`: safely repaired executable plan after conservative parsing, coercion, ordering, and clipping.

Target for base-model smoke:

- Relaxed/repaired control rate should be at least `50%`, with `70%-90%` preferred.
- If repaired control is below `50%`, do not use traffic metrics as model capability evidence; inspect prompt/output first.
- If control rate is acceptable, compare queue, delay, throughput, ATT, and AWT.

## Traffic Metrics

Primary metrics:

- `avg_queue_vehicles`
- `p95_queue_vehicles`
- `max_queue_vehicles`
- `avg_delay_per_vehicle_sec`
- `throughput_veh_per_min`
- `target_tl_att_sec`
- `target_tl_awt_sec`
- `network_att_sec`
- `network_awt_sec`

Queue threshold metrics:

- `queue_over_threshold_seconds_by_threshold` for thresholds `10, 20, 30, 40`
- `max_continuous_queue_over_threshold_seconds_by_threshold` for thresholds `10, 20, 30, 40`

## Current Remote Run

Remote server:

`ssh -p 42904 root@connect.westc.seetacloud.com`

Run root:

`/root/autodl-tmp/tsc-cycle-benchmark/runs/deepsignal_cycleplan/chengdu_j54_reasoning_nextcycle_smoke_20260624`

Expected active cases:

- `qwen3_4b_base_reasoning_nextcycle_J54_temp01_x1p2`
- `qwen35_9b_base_reasoning_nextcycle_J54_temp01_x1p2`

## Success Criteria For This Stage

This smoke stage is complete when:

1. 4B and 9B both finish without nonempty `failures.jsonl`.
2. Each case has one completed `per_tl.jsonl` row.
3. `smoke_summary.json` is generated.
4. For each model, strict/relaxed/repaired control rates, queued/applied counts, and response time are reported.
5. If relaxed/repaired control is below `50%`, raw outputs and parse errors are summarized before expanding the matrix.
6. If relaxed/repaired control is within target range, the next full matrix can be planned using the same prompt/control settings.

## Server Failure Protocol

If the remote server refuses SSH or shuts down unexpectedly:

- Stop remote actions immediately; do not assume the benchmark completed.
- Record the latest known run root, active case, prompt format, command, control-rate evidence, and log paths.
- Keep local code committed so the same prompt/parser can be resumed.
- Resume only after checking `orchestrator.pid`, active process list, `logs/status.jsonl`, per-case `per_tl.jsonl`, and `failures.jsonl`.

## Full Matrix Direction After Smoke

Only expand after smoke passes control-rate checks.

Recommended next full matrix:

- Intersections: `J54`, `314655170`, `432452987`
- Scales: `1.0`, `1.2`, `1.5`, `1.8`
- Temperatures: keep unified at `0.1` unless explicitly testing temperature sensitivity
- Prompt/control: start with `deepsignal_solution_first` and `online_control_mode=repaired`
- Exclude `first_min_green`
- Keep `action_delay_cycles=1`
- Keep strict/relaxed/repaired control rates separate
- Include target-TL and network ATT/AWT
- Keep queue threshold records for `10, 20, 30, 40`
- Model order: tune Qwen 4B/9B base first, then extend to Gemma 12B and the remaining non-Qwen benchmark models in small batches
