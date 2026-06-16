#!/usr/bin/env python3
"""Run a CyclePlan-style SUMO benchmark on the DeepSignal scenarios.

This runner intentionally avoids DeepSignal's FastAPI/MCP service.  It uses
TraCI directly so the benchmark can evaluate this repo's CyclePlan model while
reusing only the DeepSignal scenario files.
"""

from __future__ import annotations

import argparse
import csv
import copy
import gzip
import json
import math
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DEFAULT_BENCHMARK_ROOT = PROJECT_ROOT / "DeepSignal-benchmark"
DEFAULT_GGUF = (
    PROJECT_ROOT
    / "artifacts"
    / "autodl_exports"
    / "autodl-qwen35-9b-20260525T183023Z"
    / "model.q4_K_M.gguf"
)
DEFAULT_SUMO_HOME = Path(
    "/Library/Frameworks/EclipseSUMO.framework/Versions/1.25.0/EclipseSUMO/share/sumo"
)
OPENAI_JSON_ONLY_SYSTEM_PROMPT = (
    "Return only the final JSON answer requested by the user. "
    "Do not include reasoning, XML tags, markdown, code fences, or any prose. "
    "If the user prompt asks for reasoning tags, omit them and emit only valid JSON."
)
BENCHMARK_LOG_SCHEMA_VERSION = 1
BENCHMARK_LOG_PREFIX = "[cycleplan]"


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[int(rank)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


GITHUB_SCENARIOS: dict[str, dict[str, Any]] = {
    "BadHersfeld_osm_osm": {"config": "osm.sumocfg", "usage": "train"},
    "bologna_acosta_run": {"config": "run.sumocfg", "usage": "train"},
    "bologna_joined_run": {"config": "run.sumocfg", "usage": "train"},
    "bologna_pasubio_run": {"config": "run.sumocfg", "usage": "train"},
    "Doerpfeldstr_all_modes": {"config": "all_modes.sumocfg", "usage": "train"},
    "port_tutorials_port_brunswick_osm": {"config": "osm.sumocfg", "usage": "train"},
    "arterial4x4": {"config": "arterial4x4.sumocfg", "usage": "train"},
    "grid4x4": {"config": "grid4x4.sumocfg", "usage": "train"},
    "cologne1": {"config": "cologne1.sumocfg", "usage": "eval"},
    "cologne3": {"config": "cologne3.sumocfg", "usage": "eval"},
    "cologne8": {"config": "cologne8.sumocfg", "usage": "eval"},
    "ingolstadt1": {"config": "ingolstadt1.sumocfg", "usage": "eval"},
    "ingolstadt21": {"config": "ingolstadt21.sumocfg", "usage": "eval"},
    "ingolstadt7": {"config": "ingolstadt7.sumocfg", "usage": "eval"},
    "sumo_llm": {"config": "osm.sumocfg", "usage": "eval"},
}

SCENARIOS: dict[str, dict[str, Any]] = {
    name: meta
    for name, meta in GITHUB_SCENARIOS.items()
    if name not in {"arterial4x4", "grid4x4"}
}


@dataclass
class GreenPhase:
    phase_id: int
    state: str
    lanes_in: list[str]
    lanes_out: list[str]
    default_duration: int
    min_green: int
    max_green: int
    capacity: int


@dataclass
class ModelResult:
    solution: dict[str, int] | None
    format_ok: bool
    control_usable: bool
    violations: list[str]
    elapsed_sec: float | None
    raw_text: str
    parse_error: str | None


@dataclass
class PhaseForecast:
    phase_id: int
    pred_wait_predicted: float
    pred_wait_observed: float | None
    observed_history: list[float]
    pred_wait_source: str
    fallback_to_observed: bool


@dataclass
class StepMetricSample:
    phase_queues_raw: list[float]
    phase_queues_split_overlap: list[float]
    incoming_vehicle_count: int
    passage_count: int
    local_delay_delta_s: float


class RollingMeanPredWaitForecaster:
    """Auditable next-cycle wait predictor for GitHub README parity runs.

    The official repository does not publish the CyclePlan pred_wait model.
    This forecaster is therefore explicitly marked as a replacement predictor
    in predictor_meta.json and prediction_inputs.jsonl.
    """

    name = "rolling_mean"

    def __init__(self, history_steps: int, min_history_steps: int) -> None:
        if history_steps < 1:
            raise ValueError("history_steps must be >= 1")
        if min_history_steps < 1:
            raise ValueError("min_history_steps must be >= 1")
        if min_history_steps > history_steps:
            raise ValueError("min_history_steps must be <= history_steps")
        self.history_steps = history_steps
        self.min_history_steps = min_history_steps
        self._history: dict[int, deque[float]] = {}

    def observe(self, traci: Any, green_phases: list[GreenPhase]) -> None:
        observed = observe_phase_waits(traci, green_phases)
        for phase in green_phases:
            bucket = self._history.setdefault(
                phase.phase_id,
                deque(maxlen=self.history_steps),
            )
            bucket.append(float(observed[phase.phase_id]))

    def ready(self, green_phases: list[GreenPhase]) -> bool:
        return all(
            len(self._history.get(phase.phase_id, ())) >= self.min_history_steps
            for phase in green_phases
        )

    def forecast(self, green_phases: list[GreenPhase]) -> list[PhaseForecast]:
        if not self.ready(green_phases):
            raise RuntimeError(
                "pred_wait forecaster is not ready; collect more history before model calls"
            )
        forecasts: list[PhaseForecast] = []
        for phase in green_phases:
            history = list(self._history[phase.phase_id])
            predicted = float(mean(history[-self.history_steps :]))
            forecasts.append(
                PhaseForecast(
                    phase_id=phase.phase_id,
                    pred_wait_predicted=predicted,
                    pred_wait_observed=history[-1] if history else None,
                    observed_history=history,
                    pred_wait_source="rolling_mean_history",
                    fallback_to_observed=False,
                )
            )
        return forecasts


_HF_CACHE: dict[str, Any] = {}
_SUMO_HELP_CACHE: dict[str, str] = {}


def effective_sumo_home(sumo_home: Path) -> Path:
    env_sumo_home = os.environ.get("SUMO_HOME")
    if env_sumo_home and Path(sumo_home) == DEFAULT_SUMO_HOME:
        return Path(env_sumo_home)
    return Path(sumo_home)


def _ensure_sumo_imports(sumo_home: Path) -> None:
    resolved_sumo_home = effective_sumo_home(sumo_home)
    os.environ["SUMO_HOME"] = str(resolved_sumo_home)
    tools = resolved_sumo_home / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))


def _import_traci() -> Any:
    import traci  # type: ignore

    return traci


def _make_traci_label(scenario: str, tl_id: str) -> str:
    raw = f"{scenario}__{tl_id}__{time.monotonic_ns()}"
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)


def _read_xml(path: Path) -> ET.ElementTree:
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as fh:
            return ET.parse(fh)
    return ET.parse(path)


def resolve_sumocfg(benchmark_root: Path, scenario: str) -> Path:
    meta = SCENARIOS[scenario]
    path = benchmark_root / "scenarios" / scenario / meta["config"]
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def _sumo_binary_path(args: argparse.Namespace) -> Path:
    path = effective_sumo_home(args.sumo_home) / "bin" / ("sumo-gui" if args.gui else "sumo")
    if path.exists():
        return path
    return Path("sumo-gui" if args.gui else "sumo")


def _sumo_help_text(args: argparse.Namespace) -> str:
    binary = str(_sumo_binary_path(args))
    cached = _SUMO_HELP_CACHE.get(binary)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except Exception:
        text = ""
    _SUMO_HELP_CACHE[binary] = text
    return text


def _sumo_supports_option(args: argparse.Namespace, option_name: str) -> bool:
    help_text = _sumo_help_text(args)
    if not help_text:
        return True
    return f"--{option_name}" in help_text


def runtime_sumocfg(sumocfg: Path, args: argparse.Namespace) -> Path:
    """Return a SUMO config compatible with the installed SUMO binary.

    Older SUMO builds reject some newer config options.  When that happens,
    create a sibling compat copy with unsupported options removed.
    """
    unsupported_tags = []
    if not _sumo_supports_option(args, "time-to-teleport.bidi"):
        unsupported_tags.append("time-to-teleport.bidi")
    if not unsupported_tags:
        return sumocfg

    tree = ET.parse(sumocfg)
    root = tree.getroot()
    removed = 0
    for parent in list(root.iter()):
        for child in list(parent):
            tag = child.tag.split("}")[-1]
            if tag in unsupported_tags:
                parent.remove(child)
                removed += 1
    if removed == 0:
        return sumocfg

    compat_path = sumocfg.with_name(f".compat.{sumocfg.name}")
    tree.write(compat_path, encoding="utf-8", xml_declaration=True)
    print_benchmark_log(
        "sumo_config_compat_written",
        {
            "sumocfg": str(sumocfg),
            "compat_path": str(compat_path),
            "removed_options": unsupported_tags,
        },
    )
    return compat_path


def parse_net_file(sumocfg: Path) -> Path:
    root = ET.parse(sumocfg).getroot()
    value: str | None = None
    for el in root.iter():
        if el.tag.endswith("net-file"):
            value = el.attrib.get("value") or (el.text.strip() if el.text else None)
            break
    if not value:
        raise ValueError(f"missing net-file in {sumocfg}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = sumocfg.parent / path
    return path.resolve()


def list_tl_ids(sumocfg: Path) -> list[str]:
    net = parse_net_file(sumocfg)
    root = _read_xml(net).getroot()
    out: set[str] = set()
    for el in root.iter():
        if el.tag.endswith("tlLogic"):
            tl_id = el.attrib.get("id")
            if tl_id:
                out.add(tl_id)
    return sorted(out)


def load_tls_targets(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    text = path.read_text(encoding="utf-8-sig")
    sample = text[:4096]
    dialect = csv.Sniffer().sniff(sample, delimiters=",\t") if sample.strip() else csv.excel_tab
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError(f"empty TLS target file: {path}")
    required = {"scenario", "tl_id"}
    missing = required - set(reader.fieldnames)
    if missing:
        raise ValueError(f"TLS target file missing columns {sorted(missing)}: {path}")
    targets: dict[str, list[str]] = {}
    for line_no, row in enumerate(reader, start=2):
        scenario = (row.get("scenario") or "").strip()
        tl_id = (row.get("tl_id") or "").strip()
        if not scenario or not tl_id:
            raise ValueError(f"TLS target file has blank scenario/tl_id at line {line_no}: {path}")
        targets.setdefault(scenario, [])
        if tl_id not in targets[scenario]:
            targets[scenario].append(tl_id)
    return targets


def scenario_names(args: argparse.Namespace) -> list[str]:
    if args.scenario:
        names = args.scenario
    elif args.usage == "all":
        names = list(SCENARIOS)
    else:
        names = [name for name, meta in SCENARIOS.items() if meta["usage"] == args.usage]
    if args.scenario_limit is not None:
        names = names[: args.scenario_limit]
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        raise ValueError(f"unknown scenarios: {unknown}")
    return names


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(port: int, timeout_sec: int, proc: subprocess.Popen | None = None) -> bool:
    deadline = time.time() + timeout_sec
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (OSError, urllib.error.URLError, ConnectionResetError):
            pass
        time.sleep(2)
    return False


def spawn_llama_server(args: argparse.Namespace, output_dir: Path) -> tuple[subprocess.Popen, int]:
    llama_server = Path(args.llama_server)
    gguf_path = Path(args.gguf_path)
    if not llama_server.exists():
        raise FileNotFoundError(f"llama-server missing: {llama_server}")
    if not gguf_path.exists():
        raise FileNotFoundError(f"GGUF missing: {gguf_path}")

    port = _find_free_port()
    log_path = output_dir / "llama_server.log"
    log_fh = log_path.open("w", encoding="utf-8")
    cmd = [
        str(llama_server),
        "-m",
        str(gguf_path),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "-ngl",
        str(args.ngl),
        "-t",
        str(args.threads),
        "-c",
        str(args.ctx_size),
        "--no-webui",
    ]
    print_benchmark_log(
        "llama_server_spawn",
        {"cmd": cmd, "port": port, "log_path": str(log_path)},
    )
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        preexec_fn=os.setsid,
    )
    if not _wait_health(port, args.server_startup_sec, proc):
        kill_process_group(proc)
        raise RuntimeError(f"llama-server did not become healthy; see {log_path}")
    return proc, port


def kill_process_group(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def _post_completion(port: int, prompt: str, args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    body = json.dumps(
        {
            "prompt": prompt,
            "n_predict": args.n_predict,
            "temperature": args.temperature,
            "top_k": 40 if args.temperature > 0 else 1,
            "seed": args.seed,
            "cache_prompt": True,
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/completion",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=args.timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        elapsed = time.time() - t0
        return str(payload.get("content", "")), {
            "elapsed_sec": elapsed,
            "http_status": resp.status,
            "timeout": False,
        }
    except (TimeoutError, urllib.error.URLError) as exc:
        elapsed = time.time() - t0
        return "", {
            "elapsed_sec": elapsed,
            "http_status": None,
            "timeout": True,
            "error": str(exc),
        }


def _post_openai_chat(prompt: str, args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    base_url = (args.openai_base_url or os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
    api_key = args.openai_api_key or os.environ.get("OPENAI_API_KEY") or "EMPTY"
    if not base_url:
        raise ValueError("--openai-base-url or OPENAI_BASE_URL is required for model-backend=openai")
    messages: list[dict[str, str]] = []
    if args.openai_json_system_prompt:
        messages.append({"role": "system", "content": OPENAI_JSON_ONLY_SYSTEM_PROMPT})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": args.openai_model,
        "messages": messages,
        "temperature": args.temperature,
        "max_tokens": args.n_predict,
        "stream": False,
    }
    if args.openai_stop:
        payload["stop"] = args.openai_stop
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions" if base_url.endswith("/v1") else f"{base_url}/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=args.timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        elapsed = time.time() - t0
        choices = payload.get("choices") or []
        content = ""
        if choices:
            content = str((choices[0].get("message") or {}).get("content") or "")
        return content, {"elapsed_sec": elapsed, "http_status": resp.status, "timeout": False}
    except urllib.error.HTTPError as exc:
        elapsed = time.time() - t0
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body_text = str(exc)
        return "", {
            "elapsed_sec": elapsed,
            "http_status": exc.code,
            "timeout": False,
            "error": body_text[:1000],
        }
    except (TimeoutError, urllib.error.URLError) as exc:
        elapsed = time.time() - t0
        return "", {
            "elapsed_sec": elapsed,
            "http_status": None,
            "timeout": True,
            "error": str(exc),
        }


def _load_hf_stack(args: argparse.Namespace) -> tuple[Any, Any]:
    model_path = Path(args.hf_model_path or "")
    if not model_path.exists():
        raise FileNotFoundError(f"HF model path missing: {model_path}")
    adapter_path = Path(args.hf_adapter_path) if args.hf_adapter_path else None
    if adapter_path is not None and not adapter_path.exists():
        raise FileNotFoundError(f"HF adapter path missing: {adapter_path}")

    cache_key = json.dumps(
        {
            "path": str(model_path.resolve()),
            "adapter_path": str(adapter_path.resolve()) if adapter_path else None,
            "dtype": args.hf_dtype,
            "device_map": args.hf_device_map,
        },
        sort_keys=True,
    )
    cached = _HF_CACHE.get(cache_key)
    if cached is not None:
        return cached["tokenizer"], cached["model"]

    import torch  # type: ignore
    from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore

    dtype_map = {
        "auto": "auto",
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }
    torch_dtype = dtype_map[args.hf_dtype]
    tokenizer_path = adapter_path if adapter_path and (adapter_path / "tokenizer.json").exists() else model_path
    tokenizer = AutoTokenizer.from_pretrained(str(tokenizer_path), trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        str(model_path),
        trust_remote_code=True,
        device_map=args.hf_device_map,
        torch_dtype=torch_dtype,
        low_cpu_mem_usage=True,
        attn_implementation="sdpa",
    )
    if adapter_path is not None:
        from peft import PeftModel  # type: ignore

        model = PeftModel.from_pretrained(model, str(adapter_path), is_trainable=False)
    model.eval()
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token or tokenizer.unk_token
    _HF_CACHE[cache_key] = {"tokenizer": tokenizer, "model": model}
    return tokenizer, model


def _post_hf_generate(prompt: str, args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    tokenizer, model = _load_hf_stack(args)
    import torch  # type: ignore

    t0 = time.time()
    inputs = tokenizer(prompt, return_tensors="pt")
    device = next(model.parameters()).device
    inputs = {key: value.to(device) for key, value in inputs.items()}
    with torch.inference_mode():
        output_ids = model.generate(
            **inputs,
            max_new_tokens=args.n_predict,
            do_sample=args.temperature > 0,
            temperature=args.temperature if args.temperature > 0 else None,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
            use_cache=True,
        )
    generated = output_ids[0, inputs["input_ids"].shape[1] :]
    text = tokenizer.decode(generated, skip_special_tokens=False)
    elapsed = time.time() - t0
    return text, {"elapsed_sec": elapsed, "http_status": None, "timeout": False}


def build_native_prompt(prediction_input: dict[str, Any], prefill: bool) -> str:
    from tsc_cycle.prompt_builder import build_assistant_prefill, build_user_prompt

    prompt = build_user_prompt(prediction_input)
    if prefill:
        prompt += "\n" + build_assistant_prefill()
    return prompt


def build_deepsignal_prompt(prediction_input: dict[str, Any], prefill: bool) -> str:
    input_json = json.dumps(prediction_input, indent=2, ensure_ascii=False)
    eos_token = "<|endoftext|>"
    system_content = """你是交通信号配时优化专家。
请认真分析问题并给出你的推理过程。
将推理过程放在 <start_working_out> 和 </end_working_out> 之间。
然后，将你的最终方案放在 <SOLUTION> 和 </SOLUTION> 之间。"""
    user_content = f"""【cycle_predict_input_json】{input_json}【/cycle_predict_input_json】

任务（必须完成）：
基于 prediction.phase_waits 的 pred_saturation，在满足全部硬约束前提下，输出下一周期各相位最终绿灯时间 final（单位：秒）。

输入字段说明：
- prediction.phase_waits[*].min_green / max_green：绿灯时长上下限，单位秒。
- prediction.phase_waits[*].pred_wait：预测等待车辆数。
- prediction.phase_waits[*].pred_saturation：预测饱和度（pred_wait / capacity）。
- prediction.phase_waits[*].capacity：相位容量，仅供参考。

硬约束（必须满足）：
1) 相位顺序固定：严格按 prediction.phase_waits 的顺序考虑并输出；不可跳相、不可重排。
2) 每相位约束：final 必须满足 prediction.phase_waits[*].min_green ≤ final ≤ prediction.phase_waits[*].max_green。
3) final 必须为整数秒。

决策提示（非硬约束）：
- 最终决策以 pred_saturation 为主，capacity 仅供参考。

输出要求（必须严格遵守）：
1) 必须先输出 <start_working_out>...</end_working_out>，其中只写思考分析过程，不要输出最终 JSON。
2) 随后输出 <SOLUTION>...</SOLUTION>；<SOLUTION> 内只允许最终 JSON，不允许其它文本。
3) JSON 顶层必须是数组(list)，每个元素必须是 {{"phase_id": <int>, "final": <int>}}。
4) 必须覆盖 prediction.phase_waits 中所有相位ID，不能缺少或多余，不能输出额外字段。
5) 除 <start_working_out>...</end_working_out> 与 <SOLUTION>...</SOLUTION> 外，不允许输出任何其它文本。"""
    prompt = system_content + eos_token + user_content
    if prefill:
        prompt += "</end_working_out>"
    return prompt


def extract_solution_json(text: str) -> tuple[Any | None, str | None]:
    stripped = text.strip()
    if "<SOLUTION>" in stripped and "</SOLUTION>" in stripped:
        start = stripped.rfind("<SOLUTION>") + len("<SOLUTION>")
        end = stripped.rfind("</SOLUTION>")
        stripped = stripped[start:end].strip()
    elif "<SOLUTION>" in stripped:
        start = stripped.rfind("<SOLUTION>") + len("<SOLUTION>")
        stripped = stripped[start:].strip()
    else:
        first_obj = stripped.find("{")
        first_arr = stripped.find("[")
        starts = [x for x in (first_obj, first_arr) if x >= 0]
        if starts:
            stripped = stripped[min(starts) :].strip()
    try:
        return json.loads(stripped), None
    except json.JSONDecodeError as exc:
        return None, f"json_decode: {exc}"


def has_solution_block(text: str) -> bool:
    return "<SOLUTION>" in text and "</SOLUTION>" in text


def normalize_solution(
    parsed: Any,
    *,
    allow_dict: bool = True,
) -> tuple[dict[str, int] | None, str | None]:
    if isinstance(parsed, dict):
        if not allow_dict:
            return None, "dict_not_allowed"
        out: dict[str, int] = {}
        for key, value in parsed.items():
            if isinstance(value, bool) or not isinstance(value, int):
                return None, "dict_value_not_int"
            out[str(key)] = int(value)
        return out, None
    if isinstance(parsed, list):
        out = {}
        for item in parsed:
            if not isinstance(item, dict):
                return None, "array_item_not_object"
            if set(item) != {"phase_id", "final"}:
                return None, "array_item_bad_keys"
            phase_id = item.get("phase_id")
            final = item.get("final")
            if isinstance(phase_id, bool) or not isinstance(phase_id, int):
                return None, "phase_id_not_int"
            if isinstance(final, bool) or not isinstance(final, int):
                return None, "final_not_int"
            out[str(phase_id)] = int(final)
        return out, None
    return None, "solution_not_dict_or_array"


def validate_plan(
    phase_waits: list[dict[str, Any]],
    solution: dict[str, int] | None,
) -> tuple[bool, list[str]]:
    if solution is None:
        return False, ["unparseable"]
    expected = [str(item["phase_id"]) for item in phase_waits]
    got = list(solution.keys())
    violations: list[str] = []
    if set(got) != set(expected):
        return False, ["phase_mismatch"]
    if got != expected:
        violations.append("phase_order")
    by_id = {str(item["phase_id"]): item for item in phase_waits}
    for phase_id, final in solution.items():
        if isinstance(final, bool) or not isinstance(final, int):
            violations.append("not_integer")
            continue
        info = by_id[phase_id]
        if final < int(info["min_green"]):
            violations.append("below_min")
        if final > int(info["max_green"]):
            violations.append("above_max")
    return True, violations


def strict_format_ok(text: str, parsed: Any, phase_waits: list[dict[str, Any]]) -> bool:
    if not has_solution_block(text) or not isinstance(parsed, list):
        return False
    expected = [int(item["phase_id"]) for item in phase_waits]
    got: list[int] = []
    for item in parsed:
        if not isinstance(item, dict) or set(item) != {"phase_id", "final"}:
            return False
        phase_id = item.get("phase_id")
        final = item.get("final")
        if isinstance(phase_id, bool) or not isinstance(phase_id, int):
            return False
        if isinstance(final, bool) or not isinstance(final, int):
            return False
        got.append(int(phase_id))
    return got == expected


def clip_solution(phase_waits: list[dict[str, Any]], solution: dict[str, int]) -> dict[str, int]:
    by_id = {str(item["phase_id"]): item for item in phase_waits}
    clipped: dict[str, int] = {}
    for phase_id, final in solution.items():
        info = by_id[phase_id]
        lo = int(info["min_green"])
        hi = int(info["max_green"])
        clipped[phase_id] = max(lo, min(hi, int(final)))
    return clipped


def call_model(
    port: int | None,
    prediction_input: dict[str, Any],
    args: argparse.Namespace,
) -> ModelResult:
    if args.prompt_format == "native":
        prompt = build_native_prompt(prediction_input, args.prefill)
    else:
        prompt = build_deepsignal_prompt(prediction_input, args.prefill)

    if args.model_backend == "openai":
        content, meta = _post_openai_chat(prompt, args)
    elif args.model_backend == "hf":
        content, meta = _post_hf_generate(prompt, args)
    else:
        if port is None:
            raise ValueError("llama backend requires a server port")
        content, meta = _post_completion(port, prompt, args)
    if not content and meta.get("error"):
        parse_error = f"http_error: {meta.get('error')}"
        return ModelResult(
            solution=None,
            format_ok=False,
            control_usable=False,
            violations=["unparseable"],
            elapsed_sec=meta.get("elapsed_sec"),
            raw_text="",
            parse_error=parse_error,
        )
    raw_text = ("<start_working_out>" + content) if args.prefill else content
    parsed, parse_error = extract_solution_json(raw_text)
    solution, normalize_error = (
        normalize_solution(parsed, allow_dict=args.input_mode != "github_official")
        if parse_error is None
        else (None, None)
    )
    if normalize_error is not None:
        parse_error = normalize_error
    control_usable, violations = validate_plan(
        prediction_input["prediction"]["phase_waits"],
        solution,
    )
    format_ok = parse_error is None and strict_format_ok(
        raw_text,
        parsed,
        prediction_input["prediction"]["phase_waits"],
    )
    return ModelResult(
        solution=solution,
        format_ok=format_ok,
        control_usable=control_usable,
        violations=violations,
        elapsed_sec=meta.get("elapsed_sec"),
        raw_text=raw_text,
        parse_error=parse_error,
    )


def phase_lanes(traci: Any, tl_id: str, phase_state: str) -> tuple[list[str], list[str]]:
    controlled_links = traci.trafficlight.getControlledLinks(tl_id)
    incoming: set[str] = set()
    outgoing: set[str] = set()
    for idx, state in enumerate(phase_state):
        if state not in "Gg":
            continue
        if idx >= len(controlled_links):
            continue
        for link in controlled_links[idx]:
            if not link:
                continue
            incoming.add(link[0])
            if len(link) > 1:
                outgoing.add(link[1])
    return sorted(incoming), sorted(outgoing)


def get_green_phases(traci: Any, tl_id: str, args: argparse.Namespace) -> list[GreenPhase]:
    logic = traci.trafficlight.getAllProgramLogics(tl_id)[0]
    out: list[GreenPhase] = []
    for idx, phase in enumerate(logic.phases):
        state = str(phase.state)
        if not any(ch in "Gg" for ch in state):
            continue
        lanes_in, lanes_out = phase_lanes(traci, tl_id, state)
        if not lanes_in:
            continue
        duration = max(1, int(round(float(phase.duration))))
        min_dur = int(round(float(getattr(phase, "minDur", 0) or 0)))
        max_dur = int(round(float(getattr(phase, "maxDur", 0) or 0)))
        min_green = max(args.min_green, min_dur if min_dur > 0 else args.min_green)
        max_green = max(args.max_green, max_dur if max_dur > 0 else args.max_green, min_green)
        capacity = max(1, len(lanes_in) * args.capacity_per_lane)
        out.append(
            GreenPhase(
                phase_id=idx,
                state=state,
                lanes_in=lanes_in,
                lanes_out=lanes_out,
                default_duration=duration,
                min_green=min_green,
                max_green=max_green,
                capacity=capacity,
            )
        )
    return out


def observe_phase_waits(traci: Any, green_phases: list[GreenPhase]) -> dict[int, int]:
    return {
        phase.phase_id: int(sum(traci.lane.getLastStepHaltingNumber(lane) for lane in phase.lanes_in))
        for phase in green_phases
    }


def build_legacy_snapshot_prediction_input(
    traci: Any,
    tl_id: str,
    green_phases: list[GreenPhase],
) -> tuple[dict[str, Any], dict[str, Any]]:
    observed = observe_phase_waits(traci, green_phases)
    waits: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for phase in green_phases:
        pred_wait = observed[phase.phase_id]
        waits.append(
            {
                "phase_id": phase.phase_id,
                "pred_wait": int(pred_wait),
                "pred_saturation": round(float(pred_wait) / phase.capacity, 6),
                "min_green": phase.min_green,
                "max_green": phase.max_green,
                "capacity": phase.capacity,
            }
        )
        sources.append(
            {
                "phase_id": phase.phase_id,
                "pred_wait_predicted": int(pred_wait),
                "pred_wait_observed": int(pred_wait),
                "pred_wait_source": "legacy_current_halting_snapshot",
                "fallback_to_observed": True,
                "lanes_in": phase.lanes_in,
                "capacity": phase.capacity,
            }
        )
    prediction_input = {
        "prediction": {
            "as_of": f"sim_time_{int(traci.simulation.getTime())}",
            "phase_waits": waits,
        }
    }
    audit = {
        "input_mode": "legacy_snapshot",
        "tl_id": tl_id,
        "as_of": prediction_input["prediction"]["as_of"],
        "pred_wait_sources": sources,
    }
    return prediction_input, audit


def build_github_official_prediction_input(
    traci: Any,
    tl_id: str,
    green_phases: list[GreenPhase],
    forecaster: RollingMeanPredWaitForecaster,
) -> tuple[dict[str, Any], dict[str, Any]]:
    forecasts = forecaster.forecast(green_phases)
    by_phase = {item.phase_id: item for item in forecasts}
    waits: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for phase in green_phases:
        forecast = by_phase[phase.phase_id]
        pred_wait = forecast.pred_wait_predicted
        pred_saturation = round(float(pred_wait) / phase.capacity, 6)
        waits.append(
            {
                "phase_id": phase.phase_id,
                "pred_saturation": pred_saturation,
                "min_green": phase.min_green,
                "max_green": phase.max_green,
                "capacity": phase.capacity,
            }
        )
        sources.append(
            {
                "phase_id": phase.phase_id,
                "pred_wait_predicted": pred_wait,
                "pred_wait_observed": forecast.pred_wait_observed,
                "pred_wait_observed_history": forecast.observed_history,
                "pred_wait_source": forecast.pred_wait_source,
                "fallback_to_observed": forecast.fallback_to_observed,
                "forecaster": forecaster.name,
                "forecaster_status": "UNSPECIFIED_BY_GITHUB_AUDITABLE_REPLACEMENT",
                "history_steps": len(forecast.observed_history),
                "lanes_in": phase.lanes_in,
                "capacity": phase.capacity,
                "capacity_source": "lanes_in_count_times_capacity_per_lane",
                "capacity_status": "UNSPECIFIED_BY_GITHUB",
            }
        )
    prediction_input = {
        "prediction": {
            "as_of": f"sim_time_{int(traci.simulation.getTime())}",
            "phase_waits": waits,
        }
    }
    audit = {
        "input_mode": "github_official",
        "tl_id": tl_id,
        "as_of": prediction_input["prediction"]["as_of"],
        "pred_wait_sources": sources,
    }
    return prediction_input, audit


def apply_cycle_plan(
    traci: Any,
    tl_id: str,
    green_phases: list[GreenPhase],
    solution: dict[str, int],
) -> None:
    logic = copy.deepcopy(traci.trafficlight.getAllProgramLogics(tl_id)[0])
    valid_ids = {str(phase.phase_id) for phase in green_phases}
    for idx, phase in enumerate(logic.phases):
        key = str(idx)
        if key not in valid_ids or key not in solution:
            continue
        final = float(solution[key])
        phase.duration = final
        if hasattr(phase, "minDur"):
            phase.minDur = final
        if hasattr(phase, "maxDur"):
            phase.maxDur = final
    traci.trafficlight.setProgramLogic(tl_id, logic)


def incoming_vehicle_ids(traci: Any, green_phases: list[GreenPhase]) -> set[str]:
    lanes_in = sorted({lane for phase in green_phases for lane in phase.lanes_in})
    vehicle_ids: set[str] = set()
    for lane in lanes_in:
        vehicle_ids.update(traci.lane.getLastStepVehicleIDs(lane))
    return vehicle_ids


def phase_queue_samples(
    traci: Any,
    green_phases: list[GreenPhase],
) -> tuple[list[float], list[float], dict[str, float]]:
    lane_phase_counts: dict[str, int] = {}
    for phase in green_phases:
        for lane in phase.lanes_in:
            lane_phase_counts[lane] = lane_phase_counts.get(lane, 0) + 1

    lane_queues = {
        lane: float(traci.lane.getLastStepHaltingNumber(lane))
        for lane in lane_phase_counts
    }
    raw: list[float] = []
    split_overlap: list[float] = []
    for phase in green_phases:
        raw.append(sum(lane_queues[lane] for lane in phase.lanes_in))
        split_overlap.append(
            sum(lane_queues[lane] / lane_phase_counts[lane] for lane in phase.lanes_in)
        )
    return raw, split_overlap, lane_queues


def collect_step_metrics(
    traci: Any,
    tl_id: str,
    green_phases: list[GreenPhase],
    previous_incoming: set[str],
    last_time_loss: dict[str, float],
) -> StepMetricSample:
    del tl_id
    raw_queues, split_queues, _lane_queues = phase_queue_samples(traci, green_phases)
    current_incoming = incoming_vehicle_ids(traci, green_phases)
    passage_count = len(previous_incoming - current_incoming)

    local_delay_delta = 0.0
    for veh_id in current_incoming:
        try:
            current_time_loss = float(traci.vehicle.getTimeLoss(veh_id))
        except Exception:
            continue
        previous_time_loss = last_time_loss.get(veh_id)
        if previous_time_loss is not None:
            local_delay_delta += max(0.0, current_time_loss - previous_time_loss)
        last_time_loss[veh_id] = current_time_loss
    for veh_id in list(last_time_loss):
        if veh_id not in current_incoming:
            last_time_loss.pop(veh_id, None)

    return StepMetricSample(
        phase_queues_raw=raw_queues,
        phase_queues_split_overlap=split_queues,
        incoming_vehicle_count=len(current_incoming),
        passage_count=passage_count,
        local_delay_delta_s=local_delay_delta,
    )


def start_sumo(traci: Any, sumocfg: Path, args: argparse.Namespace, label: str) -> Any:
    ensure_sumo_output_dirs(sumocfg)
    sumo_binary = _sumo_binary_path(args)
    cmd = [
        str(sumo_binary),
        "-c",
        str(sumocfg),
        "--step-length",
        "1.0",
        "--no-warnings",
        "true",
        "--quit-on-end",
        "--seed",
        str(args.seed),
    ]
    if args.gui and args.gui_delay_ms is not None:
        cmd.extend(["--delay", str(args.gui_delay_ms)])
    traci.start(cmd, label=label, doSwitch=False, numRetries=args.traci_start_retries)
    return traci.getConnection(label)


def ensure_sumo_output_dirs(sumocfg: Path) -> None:
    """Create directories referenced by output options in a SUMO config."""
    root = ET.parse(sumocfg).getroot()
    additional_files = _sumocfg_additional_files(sumocfg, root)
    output_prefixes: list[Path] = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if "output" not in tag:
            continue
        value = el.attrib.get("value") or (el.text.strip() if el.text else "")
        if not value or value.lower() in {"true", "false"}:
            continue
        for chunk in value.split(","):
            raw = chunk.strip()
            if not raw:
                continue
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = sumocfg.parent / path
            parent = path.parent
            if str(parent) and str(parent) != ".":
                parent.mkdir(parents=True, exist_ok=True)
            if tag == "output-prefix":
                output_prefixes.append(Path(raw).expanduser())

    for add_file in additional_files:
        for prefix in output_prefixes:
            if prefix.is_absolute():
                target = prefix.parent
            else:
                target = add_file.parent / prefix.parent
            if str(target) and str(target) != ".":
                target.mkdir(parents=True, exist_ok=True)
        _ensure_xml_output_dirs(add_file)


def _sumocfg_additional_files(sumocfg: Path, root: ET.Element) -> list[Path]:
    out: list[Path] = []
    for el in root.iter():
        if not el.tag.endswith("additional-files"):
            continue
        value = el.attrib.get("value") or (el.text.strip() if el.text else "")
        for raw in value.split(","):
            raw = raw.strip()
            if not raw:
                continue
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = sumocfg.parent / path
            if path.exists():
                out.append(path.resolve())
    return out


def _ensure_xml_output_dirs(xml_path: Path) -> None:
    try:
        tree = _read_xml(xml_path)
    except Exception:
        return
    for el in tree.getroot().iter():
        for attr in ("file", "output", "value"):
            value = el.attrib.get(attr)
            if not value or value.lower() in {"true", "false"}:
                continue
            if "output" not in el.tag and attr == "value":
                continue
            for raw in value.split(","):
                raw = raw.strip()
                if not raw:
                    continue
                path = Path(raw).expanduser()
                if not path.is_absolute():
                    path = xml_path.parent / path
                parent = path.parent
                if str(parent) and str(parent) != ".":
                    parent.mkdir(parents=True, exist_ok=True)


def run_one_tl(
    sumocfg: Path,
    scenario: str,
    tl_id: str,
    usage: str,
    port: int | None,
    args: argparse.Namespace,
    calls_fh: Any,
    prediction_inputs_fh: Any,
    benchmark_log_fh: Any,
) -> dict[str, Any]:
    traci_mod = _import_traci()
    traci_label = _make_traci_label(scenario, tl_id)
    sumo_cmd_preview = [
        str(_sumo_binary_path(args)),
        "-c",
        str(sumocfg),
        "--step-length",
        "1.0",
        "--no-warnings",
        "true",
        "--quit-on-end",
        "--seed",
        str(args.seed),
    ]
    write_benchmark_log(
        benchmark_log_fh,
        "sumo_start",
        {
            "scenario": scenario,
            "usage": usage,
            "tl_id": tl_id,
            "traci_label": traci_label,
            "cmd": sumo_cmd_preview,
        },
        to_stderr=args.log_events_to_stderr,
    )
    traci = start_sumo(traci_mod, sumocfg, args, traci_label)
    forecaster = make_pred_wait_forecaster(args)
    queue_samples_raw: list[float] = []
    queue_samples_split_overlap: list[float] = []
    local_delay_total_s = 0.0
    incoming_vehicle_observations = 0
    throughput_total_intersection_passages = 0
    prev_incoming_vehicle_ids: set[str] | None = None
    last_time_loss_by_vehicle: dict[str, float] = {}
    response_times: list[float] = []
    format_ok = 0
    control_usable = 0
    calls = 0
    applied = 0
    parse_errors: dict[str, int] = {}
    green_phases: list[GreenPhase] = []
    last_decision = -10**9
    steps_completed = 0
    metric_start = args.warmup_seconds
    metric_end = args.warmup_seconds + args.metric_seconds
    skipped_forecaster_not_ready = 0

    try:
        green_phases = get_green_phases(traci, tl_id, args)
        if not green_phases:
            raise RuntimeError(f"no green phases with controlled lanes for TL {tl_id}")
        write_benchmark_log(
            benchmark_log_fh,
            "traffic_light_ready",
            {
                "scenario": scenario,
                "usage": usage,
                "tl_id": tl_id,
                "green_phase_count": len(green_phases),
                "input_mode": args.input_mode,
                "controller": args.controller,
            },
            to_stderr=args.log_events_to_stderr,
        )

        total_steps = args.warmup_seconds + args.metric_seconds
        if args.simulation_seconds is not None:
            total_steps = args.simulation_seconds
            metric_end = min(metric_end, total_steps)
        write_benchmark_log(
            benchmark_log_fh,
            "simulation_window_ready",
            {
                "scenario": scenario,
                "usage": usage,
                "tl_id": tl_id,
                "total_steps": total_steps,
                "metric_start": metric_start,
                "metric_end": metric_end,
                "gui": bool(args.gui),
                "gui_delay_ms": args.gui_delay_ms if args.gui else None,
            },
            to_stderr=args.log_events_to_stderr,
        )

        for step in range(total_steps):
            sim_time = int(traci.simulation.getTime())
            should_decide = args.controller == "model" and sim_time - last_decision >= args.decision_interval_seconds
            if should_decide:
                if args.input_mode == "github_official":
                    if forecaster is None:
                        raise RuntimeError("github_official input mode requires a pred_wait forecaster")
                    if not forecaster.ready(green_phases):
                        skipped_forecaster_not_ready += 1
                        if skipped_forecaster_not_ready == 1:
                            write_benchmark_log(
                                benchmark_log_fh,
                                "forecaster_not_ready",
                                {
                                    "scenario": scenario,
                                    "usage": usage,
                                    "tl_id": tl_id,
                                    "sim_time": sim_time,
                                    "required_history_steps": args.forecaster_min_history_steps,
                                },
                                to_stderr=args.log_events_to_stderr,
                            )
                        prediction_input = None
                        prediction_audit = None
                    else:
                        prediction_input, prediction_audit = build_github_official_prediction_input(
                            traci,
                            tl_id,
                            green_phases,
                            forecaster,
                        )
                else:
                    prediction_input, prediction_audit = build_legacy_snapshot_prediction_input(
                        traci,
                        tl_id,
                        green_phases,
                    )
                if prediction_input is None or prediction_audit is None:
                    pass
                else:
                    prediction_audit = {
                        "scenario": scenario,
                        "usage": usage,
                        "tl_id": tl_id,
                        "sim_time": sim_time,
                        "prediction_input": prediction_input,
                        **prediction_audit,
                    }
                    write_jsonl_line(prediction_inputs_fh, prediction_audit)
                    write_benchmark_log(
                        benchmark_log_fh,
                        "prediction_input_ready",
                        {
                            "scenario": scenario,
                            "usage": usage,
                            "tl_id": tl_id,
                            "sim_time": sim_time,
                            "input_mode": args.input_mode,
                            "phase_count": len(prediction_input["prediction"]["phase_waits"]),
                        },
                        to_stderr=args.log_events_to_stderr,
                    )
                    result = call_model(port, prediction_input, args)
                    calls += 1
                    last_decision = sim_time
                    if result.format_ok:
                        format_ok += 1
                    if result.control_usable:
                        control_usable += 1
                    if result.elapsed_sec is not None:
                        response_times.append(float(result.elapsed_sec))
                    if result.parse_error:
                        parse_errors[result.parse_error] = parse_errors.get(result.parse_error, 0) + 1
                    applied_solution = None
                    if result.control_usable and result.solution is not None:
                        applied_solution = clip_solution(
                            prediction_input["prediction"]["phase_waits"],
                            result.solution,
                        )
                        apply_cycle_plan(traci, tl_id, green_phases, applied_solution)
                        applied += 1
                    write_benchmark_log(
                        benchmark_log_fh,
                        "model_call_complete",
                        {
                            "scenario": scenario,
                            "usage": usage,
                            "tl_id": tl_id,
                            "sim_time": sim_time,
                            "call_index": calls,
                            "format_ok": result.format_ok,
                            "control_usable": result.control_usable,
                            "plan_applied": bool(applied_solution is not None),
                            "elapsed_sec": result.elapsed_sec,
                            "parse_error": result.parse_error,
                            "violations": result.violations,
                        },
                        to_stderr=args.log_events_to_stderr,
                    )
                    write_jsonl_line(
                        calls_fh,
                        {
                            "scenario": scenario,
                            "usage": usage,
                            "tl_id": tl_id,
                            "sim_time": sim_time,
                            "input_mode": args.input_mode,
                            "prediction_input": prediction_input,
                            "solution": result.solution,
                            "applied_solution": applied_solution,
                            "format_ok": result.format_ok,
                            "control_usable": result.control_usable,
                            "violations": result.violations,
                            "elapsed_sec": result.elapsed_sec,
                            "parse_error": result.parse_error,
                            "raw_text_tail": result.raw_text[-1000:],
                        },
                    )

            traci.simulationStep()
            steps_completed += 1
            if forecaster is not None:
                forecaster.observe(traci, green_phases)
            current_incoming = incoming_vehicle_ids(traci, green_phases)
            if metric_start <= step < metric_end:
                if prev_incoming_vehicle_ids is None:
                    prev_incoming_vehicle_ids = current_incoming
                sample = collect_step_metrics(
                    traci,
                    tl_id,
                    green_phases,
                    prev_incoming_vehicle_ids,
                    last_time_loss_by_vehicle,
                )
                queue_samples_raw.extend(sample.phase_queues_raw)
                queue_samples_split_overlap.extend(sample.phase_queues_split_overlap)
                local_delay_total_s += sample.local_delay_delta_s
                incoming_vehicle_observations += sample.incoming_vehicle_count
                throughput_total_intersection_passages += sample.passage_count
                prev_incoming_vehicle_ids = current_incoming
            elif step < metric_start:
                prev_incoming_vehicle_ids = current_incoming
                last_time_loss_by_vehicle.clear()
    finally:
        try:
            traci.close(False)
            write_benchmark_log(
                benchmark_log_fh,
                "sumo_closed",
                {
                    "scenario": scenario,
                    "usage": usage,
                    "tl_id": tl_id,
                    "traci_label": traci_label,
                    "steps_completed": steps_completed,
                },
                to_stderr=args.log_events_to_stderr,
            )
        except Exception:
            pass

    metric_minutes = max(1.0 / 60.0, (metric_end - metric_start) / 60.0)
    raw_avg_queue = mean(queue_samples_raw) if queue_samples_raw else None
    split_avg_queue = mean(queue_samples_split_overlap) if queue_samples_split_overlap else None
    raw_p95_queue = percentile(queue_samples_raw, 0.95)
    split_p95_queue = percentile(queue_samples_split_overlap, 0.95)
    raw_max_queue = max(queue_samples_raw) if queue_samples_raw else None
    split_max_queue = max(queue_samples_split_overlap) if queue_samples_split_overlap else None
    selected_queue = split_avg_queue if args.phase_queue_mode == "split-overlap" else raw_avg_queue
    selected_p95_queue = split_p95_queue if args.phase_queue_mode == "split-overlap" else raw_p95_queue
    selected_max_queue = split_max_queue if args.phase_queue_mode == "split-overlap" else raw_max_queue
    raw_avg_delay = (
        local_delay_total_s / throughput_total_intersection_passages
        if throughput_total_intersection_passages
        else None
    )
    raw_throughput = throughput_total_intersection_passages / metric_minutes
    local_delay_per_intersection_minute_sec = local_delay_total_s / metric_minutes
    passage_per_metric_observation = (
        throughput_total_intersection_passages / incoming_vehicle_observations
        if incoming_vehicle_observations
        else None
    )
    metric_active = bool(
        incoming_vehicle_observations > 0
        or throughput_total_intersection_passages > 0
        or any(queue > 0 for queue in queue_samples_raw)
    )
    inactive_reason = None if metric_active else "no_vehicle_observations_in_metric_window"
    if not metric_active:
        write_benchmark_log(
            benchmark_log_fh,
            "metric_window_inactive",
            {
                "scenario": scenario,
                "usage": usage,
                "tl_id": tl_id,
                "metric_start": metric_start,
                "metric_end": metric_end,
                "metric_window_steps": max(0, metric_end - metric_start),
                "incoming_vehicle_observations": incoming_vehicle_observations,
                "throughput_total_intersection_passages": throughput_total_intersection_passages,
                "reason": inactive_reason,
            },
            to_stderr=args.log_events_to_stderr,
        )
    row = {
        "scenario": scenario,
        "usage": usage,
        "sumocfg": str(sumocfg),
        "tl_id": tl_id,
        "steps_completed": steps_completed,
        "green_phase_count": len(green_phases),
        "controlled_tls_count": 1,
        "controller": args.controller,
        "input_mode": args.input_mode,
        "model_backend": args.model_backend if args.controller == "model" else None,
        "prompt_format": args.prompt_format if args.controller == "model" else None,
        "warmup_seconds": args.warmup_seconds,
        "metric_seconds": metric_end - metric_start,
        "eval_minutes": metric_minutes,
        "phase_queue_mode": args.phase_queue_mode,
        "forecaster_not_ready_steps": skipped_forecaster_not_ready,
        "format_success_rate": (format_ok / calls * 100.0) if calls else None,
        "control_usable_rate": (control_usable / calls * 100.0) if calls else None,
        "lint_success_rate": (control_usable / calls * 100.0) if calls else None,
        "model_calls": calls,
        "decision_count": calls,
        "plans_applied": applied,
        "active_tl": metric_active,
        "inactive_reason": inactive_reason,
        "metric_window_steps": max(0, metric_end - metric_start),
        "metric_vehicle_observations": float(incoming_vehicle_observations),
        "throughput_total_intersection_passages": float(throughput_total_intersection_passages),
        "passage_per_metric_observation": passage_per_metric_observation,
        "passage_seen_ratio_approx": passage_per_metric_observation,
        "queue_sample_count": len(queue_samples_raw),
        "local_delay_total_s": local_delay_total_s,
        "local_delay_per_intersection_minute_sec": local_delay_per_intersection_minute_sec if metric_active else None,
        "sum_response_time_s": sum(response_times),
        "raw_avg_queue_vehicles": raw_avg_queue,
        "avg_queue_vehicles_raw": raw_avg_queue,
        "avg_queue_vehicles_split_overlap": split_avg_queue,
        "p95_queue_vehicles_raw": raw_p95_queue,
        "p95_queue_vehicles_split_overlap": split_p95_queue,
        "max_queue_vehicles_raw": raw_max_queue,
        "max_queue_vehicles_split_overlap": split_max_queue,
        "raw_avg_delay_per_vehicle_sec": raw_avg_delay,
        "raw_throughput_veh_per_min": raw_throughput,
        "avg_queue_vehicles": selected_queue if metric_active else None,
        "p95_queue_vehicles": selected_p95_queue if metric_active else None,
        "max_queue_vehicles": selected_max_queue if metric_active else None,
        "avg_delay_per_vehicle_sec": raw_avg_delay if metric_active else None,
        "throughput_veh_per_min": raw_throughput if metric_active else None,
        "avg_response_time_sec": mean(response_times) if response_times else None,
        "parse_errors": parse_errors,
    }
    return row


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}

    def vals(items: list[dict[str, Any]], key: str) -> list[float]:
        return [float(row[key]) for row in items if row.get(key) is not None]

    def weighted_rate(items: list[dict[str, Any]], key: str) -> float | None:
        numerator = 0.0
        denominator = 0.0
        for row in items:
            value = row.get(key)
            weight = float(row.get("decision_count") or 0.0)
            if value is None or weight <= 0:
                continue
            numerator += float(value) * weight
            denominator += weight
        return numerator / denominator if denominator else None

    def weighted_queue(items: list[dict[str, Any]], key: str) -> float | None:
        numerator = 0.0
        denominator = 0.0
        for row in items:
            value = row.get(key)
            weight = float(row.get("queue_sample_count") or 0.0)
            if value is None or weight <= 0:
                continue
            numerator += float(value) * weight
            denominator += weight
        return numerator / denominator if denominator else None

    def weighted_delay(items: list[dict[str, Any]]) -> float | None:
        numerator = sum(float(row.get("local_delay_total_s") or 0.0) for row in items)
        denominator = sum(float(row.get("throughput_total_intersection_passages") or 0.0) for row in items)
        return numerator / denominator if denominator else None

    def weighted_throughput(items: list[dict[str, Any]]) -> float | None:
        numerator = sum(float(row.get("throughput_total_intersection_passages") or 0.0) for row in items)
        denominator = sum(
            float(row.get("eval_minutes") or 0.0) * float(row.get("controlled_tls_count") or 1.0)
            for row in items
        )
        return numerator / denominator if denominator else None

    def weighted_response_time(items: list[dict[str, Any]]) -> float | None:
        numerator = sum(float(row.get("sum_response_time_s") or 0.0) for row in items)
        denominator = sum(float(row.get("decision_count") or 0.0) for row in items)
        return numerator / denominator if denominator else None

    def weighted_local_delay_per_minute(items: list[dict[str, Any]]) -> float | None:
        numerator = sum(float(row.get("local_delay_total_s") or 0.0) for row in items)
        denominator = sum(
            float(row.get("eval_minutes") or 0.0) * float(row.get("controlled_tls_count") or 1.0)
            for row in items
        )
        return numerator / denominator if denominator else None

    def weighted_passage_observation_ratio(items: list[dict[str, Any]]) -> float | None:
        numerator = sum(float(row.get("throughput_total_intersection_passages") or 0.0) for row in items)
        denominator = sum(float(row.get("metric_vehicle_observations") or 0.0) for row in items)
        return numerator / denominator if denominator else None

    def metric_bundle(items: list[dict[str, Any]], key: str) -> dict[str, float | None]:
        bucket = vals(items, key)
        if key in {"format_success_rate", "control_usable_rate"}:
            value = weighted_rate(items, key)
        elif key in {"avg_queue_vehicles", "p95_queue_vehicles", "max_queue_vehicles"}:
            value = weighted_queue(items, key)
        elif key == "avg_delay_per_vehicle_sec":
            value = weighted_delay(items)
        elif key == "throughput_veh_per_min":
            value = weighted_throughput(items)
        elif key == "avg_response_time_sec":
            value = weighted_response_time(items)
        elif key == "local_delay_per_intersection_minute_sec":
            value = weighted_local_delay_per_minute(items)
        elif key in {"passage_per_metric_observation", "passage_seen_ratio_approx"}:
            value = weighted_passage_observation_ratio(items)
        else:
            value = float(mean(bucket)) if bucket else None
        return {
            key: value,
            f"{key}_median": float(median(bucket)) if bucket else None,
        }

    def denominator_bundle(items: list[dict[str, Any]]) -> dict[str, float]:
        return {
            "decision_count": sum(float(row.get("decision_count") or 0.0) for row in items),
            "queue_sample_count": sum(float(row.get("queue_sample_count") or 0.0) for row in items),
            "throughput_total_intersection_passages": sum(
                float(row.get("throughput_total_intersection_passages") or 0.0) for row in items
            ),
            "local_delay_total_s": sum(float(row.get("local_delay_total_s") or 0.0) for row in items),
            "metric_vehicle_observations": sum(
                float(row.get("metric_vehicle_observations") or 0.0) for row in items
            ),
            "eval_intersection_minutes": sum(
                float(row.get("eval_minutes") or 0.0) * float(row.get("controlled_tls_count") or 1.0)
                for row in items
            ),
        }

    def active_count(items: list[dict[str, Any]]) -> int:
        return sum(1 for row in items if row.get("active_tl") is not False)

    def inactive_count(items: list[dict[str, Any]]) -> int:
        return sum(1 for row in items if row.get("active_tl") is False)

    overall = {}
    for key in (
        "format_success_rate",
        "control_usable_rate",
        "avg_queue_vehicles",
        "p95_queue_vehicles",
        "max_queue_vehicles",
        "avg_delay_per_vehicle_sec",
        "local_delay_per_intersection_minute_sec",
        "passage_per_metric_observation",
        "passage_seen_ratio_approx",
        "throughput_veh_per_min",
        "avg_response_time_sec",
    ):
        overall.update(metric_bundle(rows, key))
    overall["denominators"] = denominator_bundle(rows)

    by_usage: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_usage.setdefault(str(row["usage"]), []).append(row)

    usage_summary: dict[str, dict[str, Any]] = {}
    for usage, items in sorted(by_usage.items()):
        n_inactive = inactive_count(items)
        payload: dict[str, Any] = {
            "n_runs": len(items),
            "n_active_runs": active_count(items),
            "n_inactive_runs": n_inactive,
            "inactive_run_rate": (n_inactive / len(items) * 100.0) if items else 0.0,
            "denominators": denominator_bundle(items),
        }
        for key in (
            "format_success_rate",
            "control_usable_rate",
            "avg_queue_vehicles",
            "p95_queue_vehicles",
            "max_queue_vehicles",
            "avg_delay_per_vehicle_sec",
            "local_delay_per_intersection_minute_sec",
            "passage_per_metric_observation",
            "passage_seen_ratio_approx",
            "throughput_veh_per_min",
            "avg_response_time_sec",
        ):
            payload.update(metric_bundle(items, key))
        usage_summary[usage] = payload

    n_inactive = inactive_count(rows)
    return {
        "n_runs": len(rows),
        "n_active_runs": active_count(rows),
        "n_inactive_runs": n_inactive,
        "inactive_run_rate": (n_inactive / len(rows) * 100.0) if rows else 0.0,
        "traffic_metrics_scope": "selected_single_tls_intersection_level",
        "aggregate_method": {
            "format_success_rate": "weighted_by_decision_count",
            "control_usable_rate": "weighted_by_decision_count",
            "avg_queue_vehicles": "weighted_by_queue_sample_count",
            "p95_queue_vehicles": "weighted_by_queue_sample_count",
            "max_queue_vehicles": "weighted_by_queue_sample_count",
            "avg_delay_per_vehicle_sec": "sum(local_delay_total_s)/sum(throughput_total_intersection_passages)",
            "local_delay_per_intersection_minute_sec": "sum(local_delay_total_s)/sum(eval_minutes*controlled_tls_count)",
            "passage_per_metric_observation": "sum(throughput_total_intersection_passages)/sum(metric_vehicle_observations)",
            "passage_seen_ratio_approx": "same_as_passage_per_metric_observation_not_unique_vehicle_ratio",
            "throughput_veh_per_min": "sum(throughput_total_intersection_passages)/sum(eval_minutes*controlled_tls_count)",
            "avg_response_time_sec": "weighted_by_decision_count",
        },
        "inactive_tl_definition": (
            "no incoming vehicle observations, no intersection passages, "
            "and no positive raw phase queue samples in metric window"
        ),
        "overall": overall,
        "by_usage": usage_summary,
    }


def summarize(rows: list[dict[str, Any]], failures: list[dict[str, Any]]) -> dict[str, Any]:
    summary = aggregate(rows)
    summary["n_failures"] = len(failures)
    if failures:
        summary["failures"] = failures[:50]
    return summary


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_jsonl_line(fh: Any, payload: dict[str, Any]) -> None:
    fh.write(json.dumps(_jsonable(payload), ensure_ascii=False, separators=(",", ":")) + "\n")
    fh.flush()


def benchmark_log_row(event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "schema_version": BENCHMARK_LOG_SCHEMA_VERSION,
        "level": "INFO",
        "event": event,
    }
    if payload:
        row.update(payload)
    return row


def print_benchmark_row(row: dict[str, Any]) -> None:
    print(
        f"{BENCHMARK_LOG_PREFIX} "
        f"{json.dumps(_jsonable(row), ensure_ascii=False, separators=(',', ':'))}",
        file=sys.stderr,
    )


def print_benchmark_log(event: str, payload: dict[str, Any] | None = None) -> None:
    print_benchmark_row(benchmark_log_row(event, payload))


def write_benchmark_log(
    fh: Any,
    event: str,
    payload: dict[str, Any] | None = None,
    *,
    to_stderr: bool = False,
) -> None:
    row = benchmark_log_row(event, payload)
    write_jsonl_line(fh, row)
    if to_stderr:
        print_benchmark_row(row)


def github_scenario_audit(benchmark_root: Path) -> dict[str, Any]:
    present: list[str] = []
    missing: list[str] = []
    for name, meta in GITHUB_SCENARIOS.items():
        path = benchmark_root / "scenarios" / name / str(meta["config"])
        if path.exists():
            present.append(name)
        else:
            missing.append(name)
    return {
        "source": "DeepSignal-benchmark/README.md",
        "expected": list(GITHUB_SCENARIOS),
        "present": present,
        "missing": missing,
        "complete": not missing,
    }


def make_pred_wait_forecaster(args: argparse.Namespace) -> RollingMeanPredWaitForecaster | None:
    if args.pred_wait_forecaster == "none":
        return None
    if args.pred_wait_forecaster == "rolling_mean":
        return RollingMeanPredWaitForecaster(
            history_steps=args.forecaster_history_steps,
            min_history_steps=args.forecaster_min_history_steps,
        )
    raise ValueError(f"unsupported pred_wait forecaster: {args.pred_wait_forecaster}")


def predictor_meta(args: argparse.Namespace) -> dict[str, Any]:
    used = args.input_mode == "github_official"
    return {
        "input_mode": args.input_mode,
        "github_cycleplan_public_source": [
            "DeepSignal-benchmark/README.md",
            "DeepSignal-benchmark/README_zh.md",
        ],
        "parity_scope": (
            "README_PUBLIC_FORMAT_WITH_AUDITABLE_REPLACEMENT_PREDICTOR"
            if used
            else "LEGACY_LOCAL_SNAPSHOT_APPROXIMATION"
        ),
        "cannot_claim_official_internal_identity": used,
        "forecaster": {
            "name": args.pred_wait_forecaster,
            "used": used,
            "github_status": "UNSPECIFIED_BY_GITHUB" if used else "NOT_APPLICABLE",
            "implementation_status": (
                "AUDITABLE_REPLACEMENT" if args.pred_wait_forecaster != "none" else "NOT_CONFIGURED"
            ),
            "history_steps": args.forecaster_history_steps,
            "min_history_steps": args.forecaster_min_history_steps,
            "fallback_to_observed_allowed": False,
            "weights": None,
            "training_config": None,
        },
        "capacity": {
            "method": "len(lanes_in) * capacity_per_lane",
            "capacity_per_lane": args.capacity_per_lane,
            "github_status": "UNSPECIFIED_BY_GITHUB",
        },
        "phase_grouping": {
            "method": "TraCI controlled links whose signal state is G/g",
            "github_status": "UNSPECIFIED_BY_GITHUB",
        },
    }


def validate_runtime_args(args: argparse.Namespace) -> None:
    if args.forecaster_min_history_steps > args.forecaster_history_steps:
        raise ValueError("--forecaster-min-history-steps must be <= --forecaster-history-steps")
    if args.gui_delay_ms is not None and args.gui_delay_ms < 0:
        raise ValueError("--gui-delay-ms must be >= 0")
    if args.traci_start_retries < 1:
        raise ValueError("--traci-start-retries must be >= 1")
    if args.phase_queue_mode not in {"raw", "split-overlap"}:
        raise ValueError("--phase-queue-mode must be raw or split-overlap")
    if args.simulation_seconds is not None and args.simulation_seconds < args.warmup_seconds + args.metric_seconds:
        raise ValueError("--simulation-seconds must cover warmup_seconds + metric_seconds")
    if args.input_mode != "github_official":
        return
    if args.controller != "model":
        raise ValueError("--input-mode github_official requires --controller model")
    if args.pred_wait_forecaster == "none":
        raise ValueError(
            "--input-mode github_official requires an explicit --pred-wait-forecaster; "
            "use rolling_mean until the official predictor is available"
        )
    if args.prompt_format != "deepsignal":
        raise ValueError("--input-mode github_official requires --prompt-format deepsignal")
    if args.prefill:
        raise ValueError("--input-mode github_official requires --no-prefill to keep the README prompt intact")
    if args.model_backend == "openai" and args.openai_json_system_prompt:
        raise ValueError(
            "--input-mode github_official with --model-backend openai requires "
            "--no-openai-json-system-prompt"
        )
    if args.warmup_seconds != 300 or args.metric_seconds != 1200:
        raise ValueError(
            "--input-mode github_official requires README window: "
            "--warmup-seconds 300 --metric-seconds 1200"
        )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-root", type=Path, default=DEFAULT_BENCHMARK_ROOT)
    parser.add_argument("--usage", choices=["train", "eval", "all"], default="eval")
    parser.add_argument("--scenario", action="append", help="Scenario name. May be repeated.")
    parser.add_argument("--scenario-limit", type=int, default=None)
    parser.add_argument("--tl-id", action="append", help="Specific TL ID. May be repeated.")
    parser.add_argument("--tls-file", type=Path, default=None, help="CSV/TSV with scenario and tl_id columns.")
    parser.add_argument("--tl-limit", type=int, default=None)
    parser.add_argument("--list-scenarios", action="store_true")
    parser.add_argument("--list-tl-ids", action="store_true")
    parser.add_argument("--controller", choices=["fixed", "model"], default="fixed")
    parser.add_argument("--input-mode", choices=["legacy_snapshot", "github_official"], default="legacy_snapshot")
    parser.add_argument("--model-backend", choices=["llama", "openai", "hf"], default="llama")
    parser.add_argument("--prompt-format", choices=["native", "deepsignal"], default="native")
    parser.add_argument("--prefill", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--gguf-path", type=Path, default=DEFAULT_GGUF)
    parser.add_argument("--llama-server", type=Path, default=Path("/opt/homebrew/bin/llama-server"))
    parser.add_argument("--sumo-home", type=Path, default=DEFAULT_SUMO_HOME)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--warmup-seconds", type=int, default=300)
    parser.add_argument("--metric-seconds", type=int, default=1200)
    parser.add_argument("--simulation-seconds", type=int, default=None)
    parser.add_argument("--decision-interval-seconds", type=int, default=60)
    parser.add_argument("--min-green", type=int, default=10)
    parser.add_argument("--max-green", type=int, default=90)
    parser.add_argument("--capacity-per-lane", type=int, default=30)
    parser.add_argument("--phase-queue-mode", choices=["raw", "split-overlap"], default="raw")
    parser.add_argument("--pred-wait-forecaster", choices=["none", "rolling_mean"], default="none")
    parser.add_argument("--forecaster-history-steps", type=int, default=60)
    parser.add_argument("--forecaster-min-history-steps", type=int, default=5)
    parser.add_argument("--log-events-to-stderr", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--traci-start-retries", type=int, default=60)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--gui-delay-ms", type=int, default=None)
    parser.add_argument("--ngl", type=int, default=99)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--ctx-size", type=int, default=4096)
    parser.add_argument("--n-predict", type=int, default=384)
    parser.add_argument("--timeout-sec", type=int, default=600)
    parser.add_argument("--server-startup-sec", type=int, default=240)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--openai-base-url", default=None)
    parser.add_argument("--openai-api-key", default=None)
    parser.add_argument("--openai-model", default="tsc-cycle")
    parser.add_argument("--openai-json-system-prompt", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--openai-stop", action="append", default=None)
    parser.add_argument("--hf-model-path", type=Path, default=None)
    parser.add_argument("--hf-adapter-path", type=Path, default=None)
    parser.add_argument("--hf-dtype", choices=["auto", "bfloat16", "float16", "float32"], default="auto")
    parser.add_argument("--hf-device-map", default="auto")
    parser.add_argument("--continue-on-run-error", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    _ensure_sumo_imports(args.sumo_home)

    names = scenario_names(args)
    tls_targets = load_tls_targets(args.tls_file) if args.tls_file else {}
    if tls_targets and not args.scenario:
        names = [name for name in names if name in tls_targets]
    unknown_tls_scenarios = [name for name in tls_targets if name not in SCENARIOS]
    if unknown_tls_scenarios:
        raise ValueError(f"TLS target file contains unknown scenarios: {unknown_tls_scenarios}")
    if args.list_scenarios:
        for name in names:
            meta = SCENARIOS[name]
            print(f"{name}\t{meta['usage']}\t{meta['config']}")
        return 0

    if args.list_tl_ids:
        for name in names:
            sumocfg = resolve_sumocfg(args.benchmark_root, name)
            ids = list_tl_ids(sumocfg)
            print(f"{name}\t{len(ids)}\t{','.join(ids)}")
        return 0

    validate_runtime_args(args)

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    output_dir = args.output_dir or (PROJECT_ROOT / "runs" / "deepsignal_cycleplan" / stamp)
    output_dir.mkdir(parents=True, exist_ok=True)
    scenario_audit = github_scenario_audit(args.benchmark_root)
    resolved_sumo_home = effective_sumo_home(args.sumo_home)
    resolved_sumo_binary = _sumo_binary_path(args)
    config_payload = vars(args) | {
        "scenarios": names,
        "tls_targets": tls_targets,
        "github_scenario_audit": scenario_audit,
        "effective_sumo_home": str(resolved_sumo_home),
        "sumo_binary": str(resolved_sumo_binary),
        "metric_window": {
            "readme_total_seconds": 3600,
            "warmup_seconds": args.warmup_seconds,
            "metric_seconds": args.metric_seconds,
            "metric_start_second": args.warmup_seconds,
            "metric_end_second": args.warmup_seconds + args.metric_seconds,
            "official_window_enforced": args.input_mode == "github_official",
        },
    }
    write_json(output_dir / "config.json", config_payload)
    write_json(output_dir / "predictor_meta.json", predictor_meta(args))
    for jsonl_name in ("per_tl.jsonl", "model_calls.jsonl", "failures.jsonl", "prediction_inputs.jsonl"):
        (output_dir / jsonl_name).touch()

    proc: subprocess.Popen | None = None
    port: int | None = None
    if args.controller == "model" and args.model_backend == "llama":
        proc, port = spawn_llama_server(args, output_dir)

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    try:
        with (
            (output_dir / "model_calls.jsonl").open("a", encoding="utf-8") as calls_fh,
            (output_dir / "prediction_inputs.jsonl").open("a", encoding="utf-8") as prediction_inputs_fh,
            (output_dir / "benchmark.log").open("a", encoding="utf-8") as benchmark_log_fh,
        ):
            write_benchmark_log(
                benchmark_log_fh,
                "benchmark_start",
                {
                    "output_dir": str(output_dir),
                    "input_mode": args.input_mode,
                    "scenarios": names,
                    "missing_github_scenarios": scenario_audit["missing"],
                    "gui": bool(args.gui),
                    "sumo_home": str(resolved_sumo_home),
                    "sumo_binary": str(resolved_sumo_binary),
                },
                to_stderr=args.log_events_to_stderr,
            )
            for name in names:
                sumocfg = runtime_sumocfg(resolve_sumocfg(args.benchmark_root, name), args)
                usage = str(SCENARIOS[name]["usage"])
                tl_ids = tls_targets.get(name) or args.tl_id or list_tl_ids(sumocfg)
                if args.tl_limit is not None:
                    tl_ids = tl_ids[: args.tl_limit]
                for tl_id in tl_ids:
                    write_benchmark_log(
                        benchmark_log_fh,
                        "run_start",
                        {"scenario": name, "usage": usage, "tl_id": tl_id, "sumocfg": str(sumocfg)},
                        to_stderr=args.log_events_to_stderr,
                    )
                    try:
                        row = run_one_tl(
                            sumocfg,
                            name,
                            tl_id,
                            usage,
                            port,
                            args,
                            calls_fh,
                            prediction_inputs_fh,
                            benchmark_log_fh,
                        )
                    except Exception as exc:
                        failure = {
                            "scenario": name,
                            "usage": usage,
                            "sumocfg": str(sumocfg),
                            "tl_id": tl_id,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                        failures.append(failure)
                        with (output_dir / "failures.jsonl").open("a", encoding="utf-8") as fh:
                            write_jsonl_line(fh, failure)
                        write_benchmark_log(
                            benchmark_log_fh,
                            "run_failure",
                            failure,
                            to_stderr=args.log_events_to_stderr,
                        )
                        write_json(output_dir / "summary.json", summarize(rows, failures))
                        if args.continue_on_run_error:
                            continue
                        raise
                    rows.append(row)
                    with (output_dir / "per_tl.jsonl").open("a", encoding="utf-8") as fh:
                        write_jsonl_line(fh, row)
                    write_benchmark_log(
                        benchmark_log_fh,
                        "run_complete",
                        {
                            "scenario": name,
                            "usage": usage,
                            "tl_id": tl_id,
                            "model_calls": row["model_calls"],
                            "plans_applied": row["plans_applied"],
                            "steps_completed": row["steps_completed"],
                            "format_success_rate": row["format_success_rate"],
                            "control_usable_rate": row["control_usable_rate"],
                            "avg_queue_vehicles": row["avg_queue_vehicles"],
                            "p95_queue_vehicles": row["p95_queue_vehicles"],
                            "max_queue_vehicles": row["max_queue_vehicles"],
                            "avg_delay_per_vehicle_sec": row["avg_delay_per_vehicle_sec"],
                            "throughput_veh_per_min": row["throughput_veh_per_min"],
                            "local_delay_per_intersection_minute_sec": row[
                                "local_delay_per_intersection_minute_sec"
                            ],
                            "passage_per_metric_observation": row["passage_per_metric_observation"],
                            "avg_response_time_sec": row["avg_response_time_sec"],
                            "parse_errors": row["parse_errors"],
                        },
                        to_stderr=args.log_events_to_stderr,
                    )
                    write_json(output_dir / "summary.json", summarize(rows, failures))
            write_benchmark_log(
                benchmark_log_fh,
                "benchmark_complete",
                {"n_runs": len(rows), "n_failures": len(failures)},
                to_stderr=args.log_events_to_stderr,
            )
    finally:
        kill_process_group(proc)

    summary = summarize(rows, failures)
    write_json(output_dir / "summary.json", summary)
    print(json.dumps({"output_dir": str(output_dir), "summary": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
