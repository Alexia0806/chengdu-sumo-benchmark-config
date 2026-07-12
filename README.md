# 成都 SUMO 信号控制 Benchmark

这个仓库用于在成都 SUMO 路网中评测交通信号控制器。程序通过 TraCI 驱动 SUMO 仿真，在固定时间、max-pressure 或模型控制模式下生成下一周期绿灯配时，执行控制后记录队列、通行、延误、tripinfo ATT/AWT 和模型可控性指标。

仓库只保存可复现实验需要的代码、SUMO 场景和文档。模型权重、完整运行结果、远端缓存和上游仓库副本不应提交到 Git。交付打包时 `.gitattributes` 会排除历史实验图片、模型卡草稿、本地输出和日志，避免旧结果进入新的实验包。

## 核心功能

- 成都 SUMO 场景：`chengdu/` 和 `chengdu_benchmark/scenarios/sumo_llm/`。
- 闭环信号控制 runner：`fixed`、`max_pressure`、`model` 三类控制器。
- 模型后端：Hugging Face、本地 llama.cpp/GGUF、OpenAI-compatible API。
- 正式三路口矩阵：GPT-OSS-20B、Qwen3-4B、Gemma3-12B-IT、Qwen3.6-27B、DeepSignal-CyclePlan-4B-V2。
- 指标记录：逐秒 `step_metrics.jsonl`、每路口 `per_tl.jsonl`、汇总 `summary.json`、矩阵汇总 CSV/Markdown。
- 辅助工具：候选路口选择、短 probe 过滤、窗口重算、target-peak 公平性重算。

## 目录结构

```text
.
├── chengdu/                              # 可直接用 SUMO 打开的成都场景
├── chengdu_benchmark/scenarios/sumo_llm/ # benchmark runner 使用的成都场景
├── src/sumo_benchmark/
│   ├── benchmark/                        # 闭环仿真 runner 的功能代码
│   ├── tools/                            # 汇总、筛选、排序、检查等工具代码
│   └── shell/                            # shell runner 共享逻辑
├── scripts/
│   ├── deepsignal_cycleplan_benchmark_chengdu_metrics.py  # 兼容启动 wrapper
│   ├── run_chengdu_3tl_att_awt_relaxed_x1p8_matrix.sh     # 正式矩阵启动脚本
│   ├── run_chengdu_tls_short_probe_fixed_maxpressure.sh    # fixed/max-pressure probe 启动脚本
│   └── check_repo_quality.sh                              # 质量检查启动脚本
├── tests/                                # 不依赖 SUMO/模型的单元测试
├── requirements.yaml                     # 系统、模型、路径配置清单
├── pyproject.toml                        # src-layout 包配置
├── .gitattributes                        # git archive 打包排除规则
└── MANIFEST.md                           # 核心资产说明
```

`scripts/` 只保留启动层，真实功能代码在 `src/sumo_benchmark/`。保留 `scripts/` wrapper 是为了让旧命令继续可用。

## 成都场景目录关系

`chengdu/` 和 `chengdu_benchmark/scenarios/sumo_llm/` 使用同一份成都路网和需求文件，文件内容对应关系如下：

| 独立场景 | benchmark 场景 | 关系 |
| --- | --- | --- |
| `chengdu/chengdu.net.xml` | `chengdu_benchmark/scenarios/sumo_llm/ChengduCity.net.xml` | 内容相同，文件名适配不同 SUMO 配置 |
| `chengdu/morning_rush_hour.rou.xml` | `chengdu_benchmark/scenarios/sumo_llm/morning_rush_hour.rou.xml` | 内容相同 |
| `chengdu/rush_hour_flow.rou.xml` | `chengdu_benchmark/scenarios/sumo_llm/rush_hour_flow.rou.xml` | 内容相同 |
| `chengdu/gui-settings.cfg` | `chengdu_benchmark/scenarios/sumo_llm/gui-settings.cfg` | 内容相同 |
| `chengdu/chengdu.sumocfg` | `chengdu_benchmark/scenarios/sumo_llm/osm.sumocfg` | 配置文件不同，引用的 net 文件名和仿真结束时间不同 |

`chengdu/` 是便于人工直接打开或快速检查的独立 SUMO 场景，`chengdu.sumocfg` 使用 `chengdu.net.xml`，默认结束时间是 `3599.75s`。

`chengdu_benchmark/scenarios/sumo_llm/` 是程序化 benchmark 布局。runner 通过 `--benchmark-root chengdu_benchmark --scenario sumo_llm` 定位这里的 `osm.sumocfg`，该配置引用 `ChengduCity.net.xml`，结束时间设为 `1e9`，实际 warmup、metric、simulation/drain 窗口由 runner 参数控制。

## 环境准备

最低要求：

- Python 3.10+ 推荐；当前轻量测试 Python 3.9 也可运行。
- Bash。
- SUMO，且能找到 `sumo` 可执行文件和 TraCI 工具。
- 如果跑模型控制，需要对应模型文件和后端依赖。

常用环境变量：

```bash
export SUMO_HOME=/path/to/sumo
export PYTHONPATH="$SUMO_HOME/tools:${PYTHONPATH:-}"

export PROJECT_ROOT=/path/to/chengdu-sumo-benchmark-config
export MODELS_ROOT=/path/to/models
export PYTHON_BIN=/path/to/python
export LLAMA_SERVER=/path/to/llama-server
```

`scripts/env_defaults.sh` 会自动设置常见路径。若未显式指定 `PYTHON_BIN`，且默认 TSC 虚拟环境不存在，会回退到系统 `python3`/`python`。

可选模型后端依赖：

```bash
# Hugging Face 后端
python3 -m pip install torch transformers peft huggingface_hub

# OpenAI-compatible 后端
export OPENAI_API_KEY=...
```

## 模型文件准备

仓库不包含模型权重，也不提供下载脚本。正式 runner 只读取本地路径；请先把模型放到 `MODELS_ROOT`，或通过环境变量覆盖每个模型的实际位置。

推荐目录约定：

```bash
export MODELS_ROOT=/path/to/models
mkdir -p "$MODELS_ROOT"

export GPTOSS20B_PATH="$MODELS_ROOT/gpt-oss-20b"
export QWEN4B_PATH="$MODELS_ROOT/Qwen3-4B"
export GEMMA12B_PATH="$MODELS_ROOT/gemma-3-12b-it"
export QWEN36_PATH="$MODELS_ROOT/Qwen3.6-27B"
export DEEPSIGNAL4B_GGUF_PATH="$MODELS_ROOT/model-fp16-20260519.gguf"
```

Hugging Face 模型可用 `huggingface-cli` 或 `huggingface_hub.snapshot_download` 手工下载。示例：

```bash
python3 -m pip install huggingface_hub

# 如果需要镜像，可显式设置；不需要时不要设置。
export HF_ENDPOINT=https://hf-mirror.com

huggingface-cli download Qwen/Qwen3-4B \
  --local-dir "$MODELS_ROOT/Qwen3-4B" \
  --resume-download

huggingface-cli download google/gemma-3-12b-it \
  --local-dir "$MODELS_ROOT/gemma-3-12b-it" \
  --resume-download
```

`GPT-OSS-20B` 和 `Qwen3.6-27B` 使用你实际有权限访问的 Hugging Face repo 或本地 checkpoint，下载后分别放到 `$GPTOSS20B_PATH` 和 `$QWEN36_PATH`。如果 repo 需要鉴权，先执行：

```bash
huggingface-cli login
```

`DeepSignal-CyclePlan-4B-V2` 使用 GGUF 文件，正式配置默认文件名是 `model-fp16-20260519.gguf`。把该文件放到 `$DEEPSIGNAL4B_GGUF_PATH`，并确保 `LLAMA_SERVER` 指向可执行的 `llama-server`。

## 快速检查

检查 SUMO 场景：

```bash
sumo -c chengdu/chengdu.sumocfg
```

列出 benchmark 场景：

```bash
python3 scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py \
  --benchmark-root chengdu_benchmark \
  --scenario sumo_llm \
  --list-scenarios
```

等价包内模块入口：

```bash
PYTHONPATH=src python3 -m sumo_benchmark.benchmark.chengdu_metrics \
  --benchmark-root chengdu_benchmark \
  --scenario sumo_llm \
  --list-scenarios
```

列出可控信号灯 ID：

```bash
python3 scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py \
  --benchmark-root chengdu_benchmark \
  --scenario sumo_llm \
  --list-tl-ids
```

本地 fixed-controller smoke：

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

Hugging Face 模型 smoke：

```bash
python3 scripts/deepsignal_cycleplan_benchmark_chengdu_metrics.py \
  --benchmark-root chengdu_benchmark \
  --scenario sumo_llm \
  --tl-id cluster_4550018629_4550018932 \
  --controller model \
  --model-backend hf \
  --hf-model-path "$MODELS_ROOT/Qwen3-4B" \
  --prompt-format deepsignal \
  --no-hf-use-chat-template \
  --no-prefill \
  --online-control-mode strict \
  --output-dir runs/local_hf_cluster_smoke \
  --warmup-seconds 10 \
  --metric-seconds 20 \
  --allow-nonstandard-window
```

## 正式实验怎么跑

当前正式 runner：

```bash
bash scripts/run_chengdu_3tl_att_awt_relaxed_x1p8_matrix.sh
```

默认正式口径：

| 项 | 默认值 |
| --- | --- |
| 路口 | `cluster_4550018629_4550018932`、`cluster_432429373_5213238455`、`cluster_1916386555_432429395` |
| demand scale | `1.2 1.5` |
| temperature | `0.2` |
| metric window | warmup `300s`，metric `1200s`，即 `300-1500s` |
| target peak | `240 vph/route`，每 TL `8` 条 route |
| target peak route selection | `diverse_sources` |
| decision interval | `60s` |
| action delay | `1` 个决策周期 |
| min/max green | `10s / 90s` |
| phase queue mode | `split-overlap` |
| pred wait forecaster | `rolling_mean` |
| online control mode | `strict` |

正式模型矩阵：

| 模型 | 默认运行开关 | 后端 | prompt | chat template |
| --- | --- | --- | --- | --- |
| GPT-OSS-20B | `RUN_GPTOSS20B=1` | HF | `deepsignal_solution_first` | `single_user` |
| Qwen3-4B | `RUN_QWEN4B=1` | HF | `deepsignal` | 关闭 |
| Gemma3-12B-IT | `RUN_GEMMA12B=1` | HF | `deepsignal` | 关闭 |
| Qwen3.6-27B | `RUN_QWEN36=1` | HF | `deepsignal_json` | `split_system_user` |
| DeepSignal-CyclePlan-4B-V2 | `RUN_DEEPSIGNAL4B=1` | llama/GGUF | `deepsignal` | 无 |

模型路径可通过环境变量覆盖：

```bash
export GPTOSS20B_PATH="$MODELS_ROOT/gpt-oss-20b"
export QWEN4B_PATH="$MODELS_ROOT/Qwen3-4B"
export GEMMA12B_PATH="$MODELS_ROOT/gemma-3-12b-it"
export QWEN36_PATH="$MODELS_ROOT/Qwen3.6-27B"
export DEEPSIGNAL4B_GGUF_PATH="$MODELS_ROOT/model-fp16-20260519.gguf"
```

只跑部分模型示例：

```bash
RUN_GPTOSS20B=0 RUN_GEMMA12B=0 RUN_QWEN36=0 \
RUN_QWEN4B=1 RUN_DEEPSIGNAL4B=1 \
bash scripts/run_chengdu_3tl_att_awt_relaxed_x1p8_matrix.sh
```

修改需求倍率或温度：

```bash
DEMAND_SCALES="1.2 1.5" TEMPERATURES="0.2" \
bash scripts/run_chengdu_3tl_att_awt_relaxed_x1p8_matrix.sh
```

## 输出文件怎么看

每个 case 目录通常包含：

| 文件 | 内容 |
| --- | --- |
| `per_tl.jsonl` | 每个 TL 一行，包含运行配置、控制成功率、队列、通行、延误、ATT/AWT 等核心指标 |
| `summary.json` | 当前 case 的聚合指标；多 TL 时会做加权汇总 |
| `model_calls.jsonl` | 每次模型调用的输入、输出、解析结果、是否入队/执行、fallback 原因 |
| `prediction_inputs.jsonl` | 发给模型的结构化交通状态 |
| `step_metrics.jsonl` | 指标窗口内逐秒样本，可用于重算 `300-900s`、`300-1500s` 等窗口 |
| `failures.jsonl` | 单个 TL 运行失败记录 |
| `benchmark_events.jsonl` | SUMO 启动、模型调用、方案执行、tripinfo 解析等事件 |
| `sumo_outputs/tripinfo/*.tripinfo.xml` | SUMO tripinfo，ATT/AWT 和 completion ratio 来自这里 |

正式矩阵跑完后，runner 会调用：

```bash
python3 scripts/summarize_chengdu_peak_matrix.py <RUN_ROOT>
```

等价包内模块入口：

```bash
PYTHONPATH=src python3 -m sumo_benchmark.tools.summarize_chengdu_peak_matrix <RUN_ROOT>
```

生成：

- `matrix_summary.csv`
- `matrix_per_tl_summary.csv`
- `matrix_summary.md`

需要从逐秒记录重算窗口时：

```bash
python3 scripts/summarize_step_metric_windows.py \
  runs/<run-root> \
  --windows 300:900 300:1500 \
  --output-dir outputs/window_metrics
```

## 指标来源和计算口径

所有在线控制、队列、通行、局部延误指标只在 metric window 内统计。正式实验默认 metric window 是 `300-1500s`。tripinfo 会在控制窗口结束后额外 drain 一段时间，正式 runner 默认 `600s`，用于让已进入系统的车辆尽量完成行程；控制和队列统计不会延长到 drain 阶段。

### Queue Length

主表中建议优先看：

- `avg_queue_length_vehicles`
- `p95_queue_length_vehicles`
- `max_queue_length_vehicles`

这三个字段的口径是：每秒取目标路口所有受控进口车道上的唯一车辆 ID 数量，形成时间序列：

```text
Q_len(t) = |unique vehicles on controlled incoming lanes at second t|
avg_queue_length_vehicles = mean_t Q_len(t)
p95_queue_length_vehicles = percentile_95_t Q_len(t)
max_queue_length_vehicles = max_t Q_len(t)
```

代码字段 `queue_length_scope` 固定写为：

```text
target_intersection_unique_incoming_lanes_vehicle_count
```

也就是说，它不是按相位重复加和的队列，也不是只数 halting 车辆，而是目标路口进口车道上的唯一车辆数。这个口径用于你当前正式比较。

### Phase Queue / Halting Queue

另有一组相位队列字段：

- `avg_queue_vehicles`
- `p95_queue_vehicles`
- `max_queue_vehicles`
- `avg_queue_vehicles_raw`
- `avg_queue_vehicles_split_overlap`

这组使用 SUMO 的 `lane.getLastStepHaltingNumber(lane)`，即每条车道当前 halting 车辆数。对每个绿灯相位：

```text
raw_phase_queue(phase, t) = sum halting vehicles on lanes_in of phase
```

正式 runner 使用 `--phase-queue-mode split-overlap`。如果同一进口车道被多个相位共享，该车道 halting 数会按共享次数拆分：

```text
split_phase_queue(phase, t)
  = sum_lanes_in(halting(lane, t) / number_of_phases_using_lane)
```

`avg_queue_vehicles` 和 `p95_queue_vehicles` 会选择当前 `phase_queue_mode` 对应的相位队列序列。正式实验中它们来自 `split-overlap`。

### Queue Over Threshold

每秒取当前路口所有相位队列的最大值：

```text
max_phase_queue(t) = max_phase selected_phase_queue(phase, t)
```

对于阈值 `10, 20, 30, 40`：

```text
queue_over_threshold_seconds_tX = count_t(max_phase_queue(t) > X)
queue_over_threshold_fraction_tX = queue_over_threshold_seconds_tX / metric_window_seconds
max_continuous_queue_over_threshold_seconds_tX = 最长连续超阈值秒数
```

### Throughput

程序每秒维护目标路口进口车道车辆集合。若某辆车上一秒在进口车道集合中、当前秒不在，则计为一次通过目标路口进口区域的 passage：

```text
passage_count(t) = |incoming_vehicle_ids(t-1) - incoming_vehicle_ids(t)|
throughput_total_intersection_passages = sum_t passage_count(t)
throughput_veh_per_min = throughput_total_intersection_passages / metric_minutes
```

这个 throughput 是目标路口层面的进口区域通过量，不是全网完成行程数。

### Local Delay

局部延误使用 SUMO 车辆 `timeLoss` 的逐秒增量。对当前仍在目标路口进口车道集合中的车辆：

```text
local_delay_delta(t) = sum_vehicle max(0, timeLoss_vehicle(t) - timeLoss_vehicle(t-1))
local_delay_total_s = sum_t local_delay_delta(t)
avg_delay_per_vehicle_sec = local_delay_total_s / throughput_total_intersection_passages
local_delay_per_intersection_minute_sec = local_delay_total_s / metric_minutes
```

如果窗口内没有 passage，`avg_delay_per_vehicle_sec` 为 `null`。

### Tripinfo ATT / AWT / Completion

SUMO tripinfo 中每个完成车辆提供：

- `duration`：车辆总行程时间。
- `waitingTime`：车辆等待时间。
- `timeLoss`：相对理想行驶的损失时间。

全网口径：

```text
network_metric_departed_vehicle_count = metric window 内出发车辆数
network_trip_completed_count = 这些车辆中在 tripinfo 中有完成记录的数量
network_trip_completion_ratio = completed / departed
network_att_sec = mean(duration of completed metric-window departed vehicles)
network_awt_sec = mean(waitingTime of completed metric-window departed vehicles)
network_travel_time_delay_sec = mean(timeLoss of completed metric-window departed vehicles)
```

目标 TL 口径：

```text
target_tl_seen_vehicle_count = metric window 内曾出现在目标路口进口车道集合中的唯一车辆数
target_tl_trip_completed_count = 这些车辆中在 tripinfo 中有完成记录的数量
target_tl_trip_completion_ratio = completed / seen
target_tl_att_sec = mean(duration of completed target-TL-seen vehicles)
target_tl_awt_sec = mean(waitingTime of completed target-TL-seen vehicles)
target_tl_travel_time_delay_sec = mean(timeLoss of completed target-TL-seen vehicles)
```

如果堵死严重，`target_tl_trip_completion_ratio` 会下降。这时 ATT/AWT 只对已完成车辆求均值，必须和 completion ratio 一起看，否则会低估拥堵。

### 模型控制可用性

模型每 `decision_interval_seconds` 秒生成一次下一周期各相位绿灯时长。正式 runner 使用 `60s`。`action_delay_cycles=1` 表示本周期生成的方案先入队，在下一次决策点应用，避免“看到当前状态后立即改当前周期”的时间穿越。

主要控制指标：

| 字段 | 含义 |
| --- | --- |
| `model_calls` | 模型调用次数 |
| `strict_format_success_rate` | 输出包含协议要求的结构，例如 `<SOLUTION>...</SOLUTION>` 且可按严格格式解析 |
| `strict_control_usable_rate` | 严格解析出的方案完整覆盖相位、顺序合法、绿灯整数且在 min/max 范围内 |
| `relaxed_json_success_rate` | 不严格依赖协议标签时，能在输出中找到 JSON |
| `relaxed_control_usable_rate` | relaxed JSON 已经能直接形成可执行方案 |
| `repaired_control_usable_rate` | relaxed JSON 经过安全归一化后可执行，例如字段别名、数字字符串转整数 |
| `plans_applied_rate` | 实际执行方案数 / 控制决策数 |
| `fallback_plan_rate` | 因模型不可用或输出不可执行而 fallback 的比例 |
| `avg_response_time_sec` | 模型响应时间均值 |

正式 runner 使用 `online_control_mode=strict`，因此真正用于控制的是 strict 可执行方案；解析辅助线只用于诊断模型输出质量。

## Prompt 口径

当前正式矩阵：

| 模型 | prompt |
| --- | --- |
| GPT-OSS-20B | `deepsignal_solution_first`：先输出 `<SOLUTION>`，再输出极短 reasoning |
| Qwen3-4B | `deepsignal`：先极短 `<start_working_out>`，再 `<SOLUTION>` |
| Gemma3-12B-IT | `deepsignal` |
| Qwen3.6-27B | `deepsignal_json`：只输出最终 JSON |
| DeepSignal-CyclePlan-4B-V2 | `deepsignal` |

`deepsignal` 和 `deepsignal_solution_first` 都要求最终 JSON 为：

```json
[
  {"phase_id": 0, "final": 30},
  {"phase_id": 1, "final": 20}
]
```

真实 phase 数量和 `phase_id` 来自当前路口 SUMO signal program。`final` 必须是整数秒，并满足 `min_green <= final <= max_green`。

## Target Peak 生成口径

正式 runner 对三个目标路口都加 synthetic target-peak flow：

```text
target_peak_vph_per_route = 240
target_peak_routes_per_tl = 8
target_peak_route_selection = diverse_sources
```

`diverse_sources` 会优先让 target-peak route 分散到不同进口来源，减少某个路口因为 route 来源过于集中导致的比较偏差。实际 demand 还会乘以 `demand_scale`。

## 常用开发命令

```bash
# 轻量质量检查：shell 语法、Python 编译、单元测试、硬编码本地路径扫描
bash scripts/check_repo_quality.sh

# 只跑 Python 单元测试
python3 -m unittest discover -s tests -v

# 检查 Python 语法
python3 -m compileall -q scripts tests
```

## 已知限制

- 完整闭环 benchmark 依赖 SUMO、模型文件、GPU/CPU 后端、HF/llama/OpenAI 环境，本仓库不包含这些外部资产。
- `target_tl_att_sec` 和 `target_tl_awt_sec` 只对 tripinfo 中已完成车辆求均值，必须同时查看 `target_tl_trip_completion_ratio`。
- `throughput_veh_per_min` 是目标路口进口区域 passage，不是全网 trip 完成数。
- `avg_queue_length_vehicles` 是进口车道唯一车辆数；`avg_queue_vehicles` 是按相位 halting queue 计算的队列，两者口径不同。
- LLM 输出诊断中的 relaxed/repaired 指标只用于分析，不代表正式 strict control 一定采用。

## 参考论文和依据

本项目不是逐篇论文的完整复现，而是把两篇交通信号控制论文中的研究问题和评价口径落到成都 SUMO 场景上。具体指标公式以源码实现为准，核心实现位于 `src/sumo_benchmark/benchmark/chengdu_metrics.py`。

1. Aoyu Pang, Maonan Wang, Man-On Pun, Chung Shue Chen, Xi Xiong. “iLLM-TSC: Integration Reinforcement Learning and Large Language Model for Traffic Signal Control Policy Improvement.” arXiv:2407.06025, 2024.
   参考点：该论文讨论 RL 交通信号控制在通信退化和长尾事件下的可靠性问题，并用 LLM 对控制策略进行评估和修正。本项目借鉴其“结构化交通状态 + LLM 决策/校验 + 安全约束过滤”的思想，用 strict/relaxed/repaired 指标记录模型输出是否满足可执行控制约束。

2. Xiang (Ben) Song, Bin Zhou, Dongfang Ma. “Cooperative traffic signal control through a counterfactual multi-agent deep actor critic approach.” Transportation Research Part C, 160:104528, 2024. DOI: `10.1016/j.trc.2024.104528`.
   参考点：该论文面向多路口协同信号控制，使用 queue length、average travel time delay、throughput 等指标比较控制方法。本项目的三路口正式矩阵沿用这些核心评价维度，并额外区分目标路口进口车道唯一车辆数、相位 halting queue、tripinfo ATT/AWT 和 completion ratio。
