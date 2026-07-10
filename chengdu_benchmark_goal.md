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

`$RUNS_ROOT/chengdu_j54_reasoning_nextcycle_smoke_20260624`

Expected active cases:

- `qwen3_4b_base_reasoning_nextcycle_J54_temp01_x1p2`
- `qwen35_9b_base_reasoning_nextcycle_J54_temp01_x1p2`

## Latest Checkpoint

Timestamp: `2026-06-24 09:04 CST`

Completed Qwen short-window smoke:

- Run root: `$RUNS_ROOT/chengdu_j54_reasoning_nextcycle_smoke_20260624`
- Window: `warmup_seconds=60`, `metric_seconds=300`, `tripinfo_drain_seconds=120`, `allow_nonstandard_window=1`
- Prompt/control: `deepsignal_solution_first`, `temperature=0.1`, `n_predict=2048`, `online_control_mode=repaired`, `action_delay_cycles=1`
- `qwen3_4b_base`: 6 calls, strict control `33.3%`, relaxed control `100%`, repaired control `100%`, plan application `83.3%`, average response `39.6s`
- `qwen35_9b_base`: 6 calls, strict control `50.0%`, relaxed control `50.0%`, repaired control `50.0%`, plan application `50.0%`, average response `33.4s`

Gemma12B smoke attempted but not completed:

- Run root: `$RUNS_ROOT/chengdu_j54_gemma12_solution_first_smoke_20260624`
- Case: `gemma3_12b_it_reasoning_nextcycle_J54_temp01_x1p2`
- Model: `$MODELS_ROOT/gemma-3-12b-it`
- Config: `use_chat_template=1`, `hf_chat_template_message_mode=single_user`, `hf_chat_template_enable_thinking=0`, `HF_DTYPE=bfloat16`, `HF_DEVICE_MAP=auto`
- Last known state: process PID `11976` started and Gemma12B loaded weights successfully; GPU memory reached about `24518 MiB used / 7594 MiB free`, utilization about `69%`
- No model-call control-rate evidence was retrieved before SSH failed
- Failure mode: `ssh -p 42904 root@connect.westc.seetacloud.com` returned `Connection refused`; stop remote actions and resume only after checking the run root, PID, logs, and per-case files

Completed Gemma12B comparison after server resumed:

- No-thinking run root: `$RUNS_ROOT/chengdu_j54_gemma12_solution_first_smoke_20260624`
- No-thinking config: `hf_chat_template_enable_thinking=0`, `warmup_seconds=60`, `metric_seconds=180`, `tripinfo_drain_seconds=60`
- No-thinking result: 4 calls, strict control `50.0%`, relaxed control `100.0%`, repaired control `100.0%`, plan application `75.0%`, average response `77.1s`, failures `0`
- Thinking run root: `$RUNS_ROOT/chengdu_j54_gemma12_solution_first_thinking_smoke_20260624`
- Thinking config: `hf_chat_template_enable_thinking=1`, `warmup_seconds=60`, `metric_seconds=120`, `tripinfo_drain_seconds=60`
- Thinking result: 3 calls, strict control `66.7%`, relaxed control `100.0%`, repaired control `100.0%`, plan application `66.7%`, average response `77.3s`, failures `0`
- Both Gemma variants output the same executable default-style plan `{0:80, 1:70, 2:40, 3:40}` in all sampled calls
- Interpretation: thinking did not show a response-time penalty and improved strict-format rate in this tiny sample, but it did not produce a different traffic-control policy; use `hf_chat_template_enable_thinking=1` for Gemma if strict format rate is prioritized, and keep relaxed/repaired metrics separate in all reports

Completed Gemma12B no-chat-template smoke:

- No-chat run root: `$RUNS_ROOT/chengdu_j54_gemma12_solution_first_nochat_smoke_20260624`
- No-chat config: `use_chat_template=0`, `warmup_seconds=60`, `metric_seconds=120`, `tripinfo_drain_seconds=60`
- No-chat result: 3 calls, strict control `100.0%`, relaxed control `100.0%`, repaired control `100.0%`, plan application `66.7%`, average response `78.9s`, failures `0`
- No-chat output format was cleaner than both chat-template variants: it emitted `<SOLUTION>...</SOLUTION>` without Markdown fences in all sampled calls
- All sampled Gemma12B variants still output the same default-style plan `{0:80, 1:70, 2:40, 3:40}`; this proves control usability, not traffic-policy superiority
- Current recommendation for Gemma12B control-rate experiments: prefer `use_chat_template=0` with `deepsignal_solution_first`; keep chat-template thinking as a fallback only if later non-default traffic states show worse behavior

Completed Qwen no-chat prompt-thinking smoke:

- Run root: `$RUNS_ROOT/chengdu_j54_qwen_nochat_prompt_thinking_smoke_20260624`
- Config: `use_chat_template=0`, `prompt_format=deepsignal`, `hf_chat_template_enable_thinking=1` recorded as an experiment label only because the native HF thinking flag is not applied when chat template is disabled
- Window: `warmup_seconds=60`, `metric_seconds=120`, `tripinfo_drain_seconds=60`
- `qwen3_4b_base_nochat_prompt_thinking`: 3 calls, strict control `0.0%`, relaxed control `100.0%`, repaired control `100.0%`, plan application `66.7%`, average response `41.3s`, failures `0`
- `qwen35_9b_base_nochat_prompt_thinking`: 3 calls, strict control `100.0%`, relaxed control `100.0%`, repaired control `100.0%`, plan application `66.7%`, average response `4.2s`, failures `0`
- Interpretation: prompt-level thinking helped 9B a lot on both strict format and latency, but made 4B verbose and strict-format unstable; 4B still remained usable under relaxed/repaired parsing
- Current recommendation for Qwen: use `deepsignal` no-chat prompt-thinking for 9B; use `deepsignal_solution_first` no-chat for 4B unless the goal is specifically to elicit non-default exploratory plans and accept relaxed/repaired parsing

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

- Intersections: `cluster_4550018629_4550018932`, `cluster_432429373_5213238455`, `cluster_1916386555_432429395`
- Scales: `1.0`, `1.2`, `1.5`, `1.8`
- Temperatures: keep unified at `0.1` unless explicitly testing temperature sensitivity
- Prompt/control: start with `deepsignal_solution_first` and `online_control_mode=repaired`
- Exclude `first_min_green`
- Keep `action_delay_cycles=1`
- Keep strict/relaxed/repaired control rates separate
- Include target-TL and network ATT/AWT
- Keep queue threshold records for `10, 20, 30, 40`
- Model order: tune Qwen 4B/9B base first, then extend to Gemma 12B and the remaining non-Qwen benchmark models in small batches
