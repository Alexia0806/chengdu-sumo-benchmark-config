---
license: cc-by-nc-4.0
language:
- en
pipeline_tag: text-generation
tags:
- gguf
- qwen3
- llama.cpp
- lmstudio
- traffic-signal-control
- traffic-optimization
- simulation
- cycle-planning
- deepsignal
---

# DeepSignal-CyclePlan-4B-V2 (F16 GGUF)

This repository releases DeepSignal-CyclePlan-4B-V2 (F16 GGUF) for local inference with llama.cpp, LM Studio, and other GGUF-compatible runtimes. The model is designed for cycle-level green-time allocation: given predicted phase-level traffic states for an intersection, it outputs the final green-light duration for each phase in the next signal cycle while respecting phase-specific minimum and maximum green constraints.

This version follows the Qwen3 4B architecture family and is a new version release in the DeepSignal-CyclePlan-4B series. It is intended for SUMO simulation, traffic signal timing research, and local controller prototyping.

## Chengdu SUMO Scenario Configuration

The evaluation uses the Chengdu SUMO scenario included in this repository. The closed-loop evaluation runner reads `chengdu_benchmark/scenarios/sumo_llm/osm.sumocfg`, which loads the `ChengduCity.net.xml` network and two demand files: `morning_rush_hour.rou.xml` and `rush_hour_flow.rou.xml`. The standalone copy at `chengdu/chengdu.sumocfg` uses the same network and route-file structure.

The Chengdu network contains 46 traffic-signal controllers, 852 road edges, and 579 junctions. During evaluation, selected target signal controllers are controlled in a cycle-level closed loop: at each control cycle, the model receives phase-level predicted waiting vehicles, saturation, and green-time constraints, then outputs the green duration for every phase in the next cycle.

## Models

This repository currently contains:

- `DeepSignal-CyclePlan-4B-V2`: cycle-level green-time allocation for all phases in the upcoming signal cycle

## Model Files

| Filename | Task | Quantization | Size | Notes |
|---|---|---:|---:|---|
| `DeepSignal-CyclePlan-4B-V2-F16.gguf` | Cycle planning | F16 | ~7.5 GB | High-fidelity release |

## DeepSignal-CyclePlan-4B-V2

`DeepSignal-CyclePlan-4B-V2` takes the predicted traffic state for the next cycle and returns a machine-readable timing plan. The model was evaluated with a DeepSignal-style prompt that asks for a short reasoning block followed by a strict JSON plan inside `<SOLUTION>...</SOLUTION>`.

### Recommended Prompt Format

System prompt:

```text
You are a traffic signal timing optimization expert.
```

User Prompt template:

```text
【cycle_predict_input_json】{
  "prediction": {
    "as_of": "<timestamp>",
    "phase_waits": [
      {
        "phase_id": <int>,
        "pred_wait": <float>,
        "pred_saturation": <float>,
        "min_green": <int>,
        "max_green": <int>,
        "capacity": <int>
      }
      // ... more phases
    ]
  }
}【/cycle_predict_input_json】

Task (must complete):
Based on prediction.phase_waits[*].pred_saturation, output the final green time final for each phase in the next cycle (unit: seconds), while satisfying all hard constraints.

Input field descriptions:
- prediction.phase_waits[*].min_green / max_green: lower and upper green-time bounds, in seconds.
- prediction.phase_waits[*].pred_wait: predicted waiting vehicles.
- prediction.phase_waits[*].pred_saturation: predicted saturation (pred_wait / capacity).
- prediction.phase_waits[*].capacity: phase capacity, for reference only.

Hard constraints (must satisfy):
1) Fixed phase order: consider and output strictly in the order of prediction.phase_waits; no skipping and no reordering.
2) Per-phase constraint: final must satisfy prediction.phase_waits[*].min_green <= final <= prediction.phase_waits[*].max_green.
3) final must be an integer in seconds.

Decision hint (non-hard constraint):
- The final decision should be based primarily on pred_saturation; capacity is for reference only.

Output requirements (must strictly follow):
1) First output <start_working_out>...</end_working_out>; include only the reasoning process there, not the final JSON.
2) Then output <SOLUTION>...</SOLUTION>; inside <SOLUTION>, only the final JSON is allowed.
3) JSON top level must be an object/dict; keys are phase IDs as strings and values are integer seconds. Keys must use double quotes.
4) The JSON must cover all phase IDs in prediction.phase_waits, with no missing or extra phases.
5) Do not output any text outside <start_working_out>...</end_working_out> and <SOLUTION>...</SOLUTION>.
```

Input JSON format:

Wrap the input with `【cycle_predict_input_json】...【/cycle_predict_input_json】` tags. The core field is `prediction.phase_waits`, an array of per-phase objects with `phase_id`, `pred_wait`, `pred_saturation`, `min_green`, `max_green`, and `capacity`. Here `pred_saturation = pred_wait / capacity`.

Output format:

The output must contain a reasoning block followed by a JSON object inside `<SOLUTION>...</SOLUTION>`, for example `<SOLUTION>{"1": 55, "2": 30}</SOLUTION>`. Each key is a phase ID string, and each value is the allocated green time in integer seconds.

### Quickstart with llama.cpp

```bash
llama-cli -m DeepSignal-CyclePlan-4B-V2-F16.gguf \
  --ctx-size 4096 \
  --temp 0.2 \
  --n-predict 2048 \
  -p 'You are a traffic signal timing optimization expert.
【cycle_predict_input_json】{
  "prediction": {
    "as_of": "2026-04-27 00:02:27",
    "phase_waits": [
      {"phase_id": 1, "pred_wait": 0.4, "pred_saturation": 0.0083, "min_green": 50, "max_green": 80, "capacity": 48},
      {"phase_id": 2, "pred_wait": 1.0, "pred_saturation": 0.0250, "min_green": 20, "max_green": 45, "capacity": 40}
    ]
  }
}【/cycle_predict_input_json】

Task (must complete):
Based on prediction.phase_waits[*].pred_saturation, output the final green time final for each phase in the next cycle (unit: seconds), while satisfying all hard constraints.

Input field descriptions:
- prediction.phase_waits[*].min_green / max_green: lower and upper green-time bounds, in seconds.
- prediction.phase_waits[*].pred_wait: predicted waiting vehicles.
- prediction.phase_waits[*].pred_saturation: predicted saturation (pred_wait / capacity).
- prediction.phase_waits[*].capacity: phase capacity, for reference only.

Hard constraints (must satisfy):
1) Fixed phase order: consider and output strictly in the order of prediction.phase_waits; no skipping and no reordering.
2) Per-phase constraint: final must satisfy prediction.phase_waits[*].min_green <= final <= prediction.phase_waits[*].max_green.
3) final must be an integer in seconds.

Output requirements (must strictly follow):
1) First output <start_working_out>...</end_working_out>; include only the reasoning process there, not the final JSON.
2) Then output <SOLUTION>...</SOLUTION>; inside <SOLUTION>, only the final JSON is allowed.
3) JSON top level must be an object/dict; keys are phase IDs as strings and values are integer seconds.'
```

### Expected Output

The final answer should contain a machine-readable plan:

```text
<start_working_out>...</end_working_out>
<SOLUTION>{"1": 55, "2": 30}</SOLUTION>
```

### Download Example

```bash
huggingface-cli download AIMS2025/DeepSignal-CyclePlan-4B-V2 \
  DeepSignal-CyclePlan-4B-V2-F16.gguf \
  --local-dir .
```

## Evaluation (Traffic Simulation)

### Evaluation Setup

We evaluate `DeepSignal-CyclePlan-4B-V2` in a SUMO closed-loop traffic simulation. At each decision cycle, the model receives predicted phase-level waiting vehicles, predicted saturation, and phase-specific minimum and maximum green constraints. The generated timing plan is then applied to SUMO, and the simulation records both traffic-operation metrics and model-execution metrics. All evaluations were conducted on an NVIDIA GeForce RTX 5090 GPU.

The comparison table in this README uses the `300-900s` evaluation window and model temperature `0.2` to inspect early vehicle-level waiting behavior, queue level, and control-output stability.

### Metrics

The evaluation focuses on two types of metrics: traffic-operation quality and whether model outputs can reliably enter the closed-loop controller.

- **Avg Queue**: average number of queued vehicles at the controlled target intersections during the evaluation window. Lower is better.
- **Target AWT**: average waiting time of target-demand vehicles associated with the target intersections, computed from SUMO `tripinfo` vehicle records, in seconds. Lower is better.
- **Avg Delay**: average vehicle delay during the simulation, in seconds per vehicle. Lower is better.
- **Control Usable**: percentage of model outputs that can be parsed, pass timing-constraint checks, and be used as executable control plans. Higher is better.
- **Avg Response**: average model response time for one timing-plan generation, in seconds.

#### Metric Definitions

Let $t$ denote a simulation step in the evaluation window, and let $l$ denote controlled inbound lanes or phase-related lanes at the target intersections. Queue and waiting-time statistics are computed from SUMO/TraCI vehicle states.

- Intersection queue:

$$
q(t)=\sum_l q_l(t)
$$

- Average queue over the evaluation window:

$$
\mathrm{AvgQueue}=\frac{1}{T}\sum_{t=1}^{T}q(t)
$$

- Target-intersection average waiting time (AWT):

$$
\mathrm{TargetAWT}=\frac{\sum_{i \in \mathcal{V}_{target}} w_i}{|\mathcal{V}_{target}|}
$$

Here $\mathcal{V}_{target}$ is the set of target-demand vehicles associated with the target intersections, identified by the `target_peak_<tl_id>_` vehicle-id prefix, that depart within the evaluation window and complete their trips. $w_i$ is the accumulated waiting time of vehicle $i$ recorded in SUMO `tripinfo`.

- Target-intersection average travel time (ATT):

$$
\mathrm{TargetATT}=\frac{\sum_{i \in \mathcal{V}_{target}} \tau_i}{|\mathcal{V}_{target}|}
$$

Here $\tau_i=a_i-d_i$, where $d_i$ is the departure time of vehicle $i$ and $a_i$ is its arrival time. In SUMO `tripinfo`, this corresponds to the completed-trip `duration`. ATT measures the total travel time from departure to arrival, including driving, stopping, waiting, and queue-induced time.

### Model Comparison (Chengdu, 300-900s)$^{**}$

| Model | Temp | Target AWT (s) | Target ATT (s) | Avg Queue | Avg Delay (s/veh) | Control Usable | Avg Response (s) |
|:---:|---:|---:|---:|---:|---:|---:|---:|
| **DeepSignal-CyclePlan-4B-V2 (Ours)** | 0.2 | **61.43** | 138.15 | **15.54** | **112.11** | **100.00%** | **0.91** |
| Qwen3.6-27B | 0.2 | 67.48 | **133.68** | 16.13 | 112.95 | 56.67% | 6.02 |
| Qwen3.5-9B | 0.2 | 78.34 | 149.16 | 16.88 | 112.90 | 53.33% | 3.70 |
| Gemma3-12B-IT | 0.2 | 82.11 | 148.01 | 18.30 | 118.43 | 56.67% | 82.51 |
| Qwen3-4B | 0.2 | 98.10 | 160.70 | 19.93 | 129.70 | 20.00% | 40.84 |
| GPT-OSS-20B | 0.2 | 92.53 | 153.73 | 18.78 | 123.80 | 76.67% | 35.58 |

`**`: All rows use the `300-900s` evaluation window. `Target AWT / ATT` are computed from SUMO `tripinfo` records for target-demand vehicles whose `depart` time falls inside the window and whose trips are completed. `Avg Queue` and `Avg Delay` provide additional views of congestion level and vehicle delay.

**Conclusion**: In the `300-900s` early-congestion window, **DeepSignal-CyclePlan-4B-V2** obtains the lowest Target AWT (`61.43s`), the lowest Avg Queue (`15.54`), and the lowest Avg Delay (`112.11s/veh`). It also keeps **100%** Control Usable and an average response time of about **0.91s**.

![CyclePlan-4B-V2 300-900s model comparison](images/deepsignal_chengdu_300_900_comparison.png)

### CyclePlan Metric Notes

For CyclePlan models, this README focuses on whether the model output can enter the closed-loop controller and whether the resulting closed loop improves traffic metrics:

- **Control Usable**: whether the output can be parsed as valid JSON and satisfies hard constraints including phase coverage, phase order, integer seconds, and min/max green duration.
- **Queue Length / Target AWT / Target ATT / Travel Time Delay / Avg Delay**: congestion, waiting-time, travel-time, and delay metrics. Lower is better.
- **Avg Response**: average time required by the model to generate one timing plan. Lower is better.

## Intended Use

This model is intended for research on traffic signal timing optimization, SUMO simulation, and local controller prototyping. It should be paired with a validator that enforces phase order, phase coverage, integer timing, and min/max green constraints before applying any plan.

## Limitations

- The model is not a general-purpose traffic-control system and should not be deployed directly to real intersections without independent safety validation.
- Evaluation results are from SUMO-based Chengdu scenarios and may not transfer to other cities, detector layouts, phase definitions, or demand distributions.
- The model can still emit malformed or incomplete text. Production use requires strict parsing, repair or fallback logic, and hard constraint checks.

## License

This project is licensed under the Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0). Commercial use is strictly prohibited.
