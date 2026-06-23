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
import shutil
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


def normalized_queue_thresholds(args: argparse.Namespace) -> list[float]:
    raw_thresholds = getattr(args, "queue_thresholds", None) or [getattr(args, "queue_threshold", 10.0)]
    thresholds = sorted({float(value) for value in raw_thresholds})
    if not thresholds:
        raise ValueError("at least one queue threshold is required")
    if any(value < 0 for value in thresholds):
        raise ValueError("queue thresholds must be non-negative")
    return thresholds


def metric_key_float(value: float) -> str:
    text = f"{float(value):g}"
    return text.replace("-", "m").replace(".", "p")


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
    strict_control_usable: bool
    directional_control_usable: bool
    directional_violations: list[str]
    relaxed_solution: dict[str, int] | None
    relaxed_json_success: bool
    relaxed_control_usable: bool
    relaxed_directional_control_usable: bool
    relaxed_directional_violations: list[str]
    relaxed_parse_error: str | None
    repaired_solution: dict[str, int] | None
    repaired_control_usable: bool
    repaired_directional_control_usable: bool
    repaired_directional_violations: list[str]
    repair_actions: list[str]
    repair_error: str | None
    violations: list[str]
    relaxed_violations: list[str]
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
    sumocfg = demand_scaled_sumocfg(sumocfg, args)
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


def demand_scaled_sumocfg(sumocfg: Path, args: argparse.Namespace) -> Path:
    """Create a run-local SUMO config with uniformly scaled route demand."""
    scale = float(getattr(args, "demand_scale", 1.0) or 1.0)
    target_peak_enabled = bool(args.target_peak_tl_id and args.target_peak_vph_per_route > 0)
    if abs(scale - 1.0) < 1e-9 and not target_peak_enabled:
        return sumocfg
    if scale <= 0:
        raise ValueError("--demand-scale must be positive")

    output_dir = getattr(args, "output_dir", None)
    if output_dir is None:
        raise ValueError("--demand-scale requires an explicit --output-dir")
    runtime_dir = Path(output_dir) / "runtime_sumo" / f"demand_x{scale:g}" / sumocfg.parent.name
    runtime_dir.mkdir(parents=True, exist_ok=True)

    tree = ET.parse(sumocfg)
    root = tree.getroot()
    target_peak_file = write_target_peak_route_file(sumocfg, root, runtime_dir, args, scale)
    route_values: list[str] = []
    for el in root.iter():
        if el.tag.split("}")[-1] != "route-files":
            continue
        value = el.attrib.get("value") or (el.text.strip() if el.text else "")
        if value:
            route_values.extend(raw.strip() for raw in value.split(",") if raw.strip())
            scaled_values: list[str] = []
            for raw in value.split(","):
                raw = raw.strip()
                if not raw:
                    continue
                src = Path(raw).expanduser()
                if not src.is_absolute():
                    src = sumocfg.parent / src
                dst = runtime_dir / f"{src.stem}.demand_x{scale:g}{src.suffix}"
                scale_route_file(src, dst, scale)
                scaled_values.append(dst.name)
            if target_peak_file is not None:
                scaled_values.append(target_peak_file.name)
            el.set("value", ",".join(scaled_values))

    if not route_values and target_peak_file is None:
        return sumocfg

    for el in root.iter():
        tag = el.tag.split("}")[-1]
        value = el.attrib.get("value")
        if not value:
            continue
        if tag in {"net-file", "additional-files", "gui-settings-file"}:
            resolved = Path(value).expanduser()
            if not resolved.is_absolute():
                resolved = sumocfg.parent / resolved
            target = runtime_dir / resolved.name
            if resolved.exists() and not target.exists():
                if resolved.is_dir():
                    shutil.copytree(resolved, target)
                else:
                    shutil.copy2(resolved, target)
            el.set("value", target.name)

    scaled_sumocfg = runtime_dir / f"{sumocfg.stem}.demand_x{scale:g}{sumocfg.suffix}"
    tree.write(scaled_sumocfg, encoding="utf-8", xml_declaration=True)
    print_benchmark_log(
        "demand_scaled_sumocfg_written",
        {
            "source_sumocfg": str(sumocfg),
            "scaled_sumocfg": str(scaled_sumocfg),
            "demand_scale": scale,
            "route_files": route_values,
            "target_peak_route_file": str(target_peak_file) if target_peak_file else None,
        },
    )
    return scaled_sumocfg


def write_target_peak_route_file(
    sumocfg: Path,
    root: ET.Element,
    runtime_dir: Path,
    args: argparse.Namespace,
    scale: float,
) -> Path | None:
    tl_ids = list(dict.fromkeys(args.target_peak_tl_id or []))
    if not tl_ids or args.target_peak_vph_per_route <= 0:
        return None

    net_value = None
    for el in root.iter():
        if el.tag.split("}")[-1] == "net-file":
            net_value = el.attrib.get("value") or (el.text.strip() if el.text else None)
            break
    if not net_value:
        raise ValueError("target peak requires a net-file in sumocfg")
    net_path = Path(net_value).expanduser()
    if not net_path.is_absolute():
        net_path = sumocfg.parent / net_path

    net_root = ET.parse(net_path).getroot()
    by_tl: dict[str, list[tuple[str, str]]] = {tl_id: [] for tl_id in tl_ids}
    seen: set[tuple[str, str, str]] = set()
    for conn in net_root.iter("connection"):
        tl_id = conn.attrib.get("tl")
        if tl_id not in by_tl:
            continue
        src = conn.attrib.get("from")
        dst = conn.attrib.get("to")
        if not src or not dst or src.startswith(":") or dst.startswith(":"):
            continue
        key = (tl_id, src, dst)
        if key in seen:
            continue
        seen.add(key)
        by_tl[tl_id].append((src, dst))

    route_root = ET.Element("routes")
    ET.SubElement(route_root, "vType", {"id": "target_peak_car", "maxSpeed": "15", "length": "5"})
    begin = float(args.target_peak_begin)
    end = (
        float(args.target_peak_end)
        if args.target_peak_end is not None
        else float(args.warmup_seconds + args.metric_seconds)
    )
    vph = float(args.target_peak_vph_per_route) * scale
    total_routes = 0
    for tl_id in tl_ids:
        pairs = by_tl.get(tl_id) or []
        if args.target_peak_routes_per_tl > 0:
            pairs = pairs[: args.target_peak_routes_per_tl]
        for idx, (src, dst) in enumerate(pairs):
            route_id = f"target_peak_{tl_id}_{idx}"
            ET.SubElement(route_root, "route", {"id": route_id, "edges": f"{src} {dst}"})
            ET.SubElement(
                route_root,
                "flow",
                {
                    "id": f"{route_id}_flow",
                    "type": "target_peak_car",
                    "route": route_id,
                    "begin": f"{begin:g}",
                    "end": f"{end:g}",
                    "vehsPerHour": f"{vph:.6f}".rstrip("0").rstrip("."),
                    "departLane": "best",
                    "departPos": "random",
                    "departSpeed": "max",
                    "color": "blue",
                },
            )
            total_routes += 1
    if total_routes == 0:
        raise ValueError(f"target peak found no controlled connections for TLs: {tl_ids}")

    out = runtime_dir / f"target_peak.demand_x{scale:g}.rou.xml"
    ET.ElementTree(route_root).write(out, encoding="utf-8", xml_declaration=True)
    print_benchmark_log(
        "target_peak_route_file_written",
        {
            "route_file": str(out),
            "tl_ids": tl_ids,
            "routes_per_tl_limit": args.target_peak_routes_per_tl,
            "route_count": total_routes,
            "vehs_per_hour_per_route": vph,
            "begin": begin,
            "end": end,
        },
    )
    return out


def _scaled_numeric_text(value: str, divisor: float) -> str:
    scaled = float(value) / divisor
    return f"{scaled:.6f}".rstrip("0").rstrip(".")


def scale_route_file(src: Path, dst: Path, scale: float) -> None:
    tree = ET.parse(src)
    root = tree.getroot()
    original_children = list(root)
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "flow":
            if "period" in el.attrib:
                el.set("period", _scaled_numeric_text(el.attrib["period"], scale))
            elif "vehsPerHour" in el.attrib:
                el.set("vehsPerHour", f"{float(el.attrib['vehsPerHour']) * scale:.6f}".rstrip("0").rstrip("."))
            elif "probability" in el.attrib:
                el.set("probability", f"{min(1.0, float(el.attrib['probability']) * scale):.6f}".rstrip("0").rstrip("."))
            elif "number" in el.attrib:
                el.set("number", str(max(1, int(round(float(el.attrib["number"]) * scale)))))

    extra_fraction = scale - 1.0
    if extra_fraction > 0:
        whole_extra = int(math.floor(extra_fraction))
        fractional_extra = extra_fraction - whole_extra
        vehicle_like_tags = {"vehicle", "trip"}
        vehicle_index = 0
        clones: list[ET.Element] = []
        for child in original_children:
            tag = child.tag.split("}")[-1]
            if tag not in vehicle_like_tags or "id" not in child.attrib:
                continue
            copies = whole_extra
            if fractional_extra > 0 and ((vehicle_index * 9973) % 10000) < int(round(fractional_extra * 10000)):
                copies += 1
            for copy_index in range(copies):
                clone = copy.deepcopy(child)
                clone.set("id", f"{child.attrib['id']}_demand{scale:g}_{copy_index + 1}")
                if "depart" in clone.attrib:
                    depart = float(clone.attrib["depart"])
                    clone.set("depart", f"{depart + 0.01 * (copy_index + 1):.6f}".rstrip("0").rstrip("."))
                clones.append(clone)
            vehicle_index += 1
        for clone in clones:
            root.append(clone)

    dst.parent.mkdir(parents=True, exist_ok=True)
    tree.write(dst, encoding="utf-8", xml_declaration=True)


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
    load_kwargs = {
        "trust_remote_code": True,
        "device_map": args.hf_device_map,
        "torch_dtype": torch_dtype,
        "low_cpu_mem_usage": True,
        "attn_implementation": "sdpa",
    }
    model_load_errors: list[str] = []
    try:
        model = AutoModelForCausalLM.from_pretrained(str(model_path), **load_kwargs)
    except Exception as exc:
        model_load_errors.append(f"AutoModelForCausalLM: {type(exc).__name__}: {exc}")
        model = None
        for class_name in ("Gemma3ForConditionalGeneration", "AutoModelForImageTextToText"):
            try:
                module = __import__("transformers", fromlist=[class_name])
                model_cls = getattr(module, class_name)
                model = model_cls.from_pretrained(str(model_path), **load_kwargs)
                break
            except Exception as fallback_exc:
                model_load_errors.append(f"{class_name}: {type(fallback_exc).__name__}: {fallback_exc}")
        if model is None:
            raise RuntimeError("HF model load failed: " + " | ".join(model_load_errors))
    if adapter_path is not None:
        from peft import PeftModel  # type: ignore

        model = PeftModel.from_pretrained(model, str(adapter_path), is_trainable=False)
    model.eval()
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token or tokenizer.unk_token
    _HF_CACHE[cache_key] = {"tokenizer": tokenizer, "model": model}
    return tokenizer, model


def _post_hf_generate(
    prompt: str,
    args: argparse.Namespace,
    messages: list[dict[str, str]] | None = None,
) -> tuple[str, dict[str, Any]]:
    tokenizer, model = _load_hf_stack(args)
    import torch  # type: ignore

    t0 = time.time()
    rendered_prompt = prompt
    used_chat_template = False
    if args.hf_use_chat_template:
        if not getattr(tokenizer, "chat_template", None):
            raise ValueError("--hf-use-chat-template requested but tokenizer has no chat_template")
        chat_messages = messages or [{"role": "user", "content": prompt}]
        try:
            rendered_prompt = tokenizer.apply_chat_template(
                chat_messages,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=args.hf_chat_template_enable_thinking,
            )
        except TypeError:
            rendered_prompt = tokenizer.apply_chat_template(
                chat_messages,
                tokenize=False,
                add_generation_prompt=True,
            )
        used_chat_template = True

    inputs = tokenizer(rendered_prompt, return_tensors="pt")
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
    text = tokenizer.decode(generated, skip_special_tokens=args.hf_skip_special_tokens)
    elapsed = time.time() - t0
    return text, {
        "elapsed_sec": elapsed,
        "http_status": None,
        "timeout": False,
        "hf_use_chat_template": used_chat_template,
        "hf_chat_template_message_mode": args.hf_chat_template_message_mode,
        "hf_skip_special_tokens": args.hf_skip_special_tokens,
    }


def build_hf_chat_messages(prompt: str, args: argparse.Namespace) -> list[dict[str, str]]:
    if args.hf_chat_template_message_mode == "single_user":
        return [{"role": "user", "content": prompt.replace("<|endoftext|>", "\n\n").strip()}]
    if args.prompt_format in {"deepsignal", "deepsignal_json"} and "<|endoftext|>" in prompt:
        system_content, user_content = prompt.split("<|endoftext|>", 1)
        return [
            {"role": "system", "content": system_content.strip()},
            {"role": "user", "content": user_content.strip()},
        ]
    return [{"role": "user", "content": prompt}]


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
- prediction.phase_waits[*].default_duration：SUMO 原始固定配时的该相位绿灯时长，单位秒，可作为基准参考。
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
- 当总需求压力不高时，避免无意义地整体拉长周期；可参考 default_duration 进行缩短或小幅调整。

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


def build_deepsignal_json_prompt(prediction_input: dict[str, Any], prefill: bool) -> str:
    if prefill:
        raise ValueError("deepsignal_json prompt does not support assistant prefill")
    input_json = json.dumps(prediction_input, indent=2, ensure_ascii=False)
    eos_token = "<|endoftext|>"
    system_content = """你是交通信号配时优化专家。
请只输出最终 JSON，不要输出推理过程、XML 标签、Markdown、代码块或任何解释文字。"""
    user_content = f"""【cycle_predict_input_json】{input_json}【/cycle_predict_input_json】

任务（必须完成）：
基于 prediction.phase_waits 的 pred_saturation，在满足全部硬约束前提下，输出下一周期各相位最终绿灯时间 final（单位：秒）。

输入字段说明：
- prediction.phase_waits[*].default_duration：SUMO 原始固定配时的该相位绿灯时长，单位秒，可作为基准参考。
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
- 当总需求压力不高时，避免无意义地整体拉长周期；可参考 default_duration 进行缩短或小幅调整。

输出要求（必须严格遵守）：
1) 只输出 JSON，不允许输出其它文本。
2) JSON 顶层必须是数组(list)，每个元素必须是 {{"phase_id": <int>, "final": <int>}}。
3) 必须覆盖 prediction.phase_waits 中所有相位ID，不能缺少或多余，不能输出额外字段。
4) 不要输出示例，不要重复题目，不要输出第二个 JSON。"""
    return system_content + eos_token + user_content


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


def extract_solution_json_relaxed(text: str) -> tuple[Any | None, str | None]:
    """Extract the first JSON payload that could plausibly contain a plan.

    This intentionally differs from the strict protocol parser: it tolerates
    missing XML-like tags, Markdown fences, explanations before/after JSON, and
    trailing text after a valid JSON object/array. It still returns only parsed
    JSON; schema repair is handled separately and logged.
    """
    stripped = text.strip()
    candidates: list[str] = []
    if "<SOLUTION>" in stripped:
        start = stripped.rfind("<SOLUTION>") + len("<SOLUTION>")
        end = stripped.rfind("</SOLUTION>")
        candidates.append(stripped[start:end].strip() if end > start else stripped[start:].strip())
    candidates.append(stripped)
    decoder = json.JSONDecoder()
    errors: list[str] = []
    for candidate in candidates:
        for idx, char in enumerate(candidate):
            if char not in "[{":
                continue
            try:
                parsed, _ = decoder.raw_decode(candidate[idx:])
                return parsed, None
            except json.JSONDecodeError as exc:
                errors.append(f"json_decode@{idx}: {exc}")
    if errors:
        return None, errors[0]
    return None, "json_not_found"


def first_complete_json_text(text: str) -> str | None:
    """Return only the first complete JSON object/array embedded in text."""
    decoder = json.JSONDecoder()
    stripped = text.strip()
    for idx, char in enumerate(stripped):
        if char not in "[{":
            continue
        try:
            _, end = decoder.raw_decode(stripped[idx:])
        except json.JSONDecodeError:
            continue
        return stripped[idx : idx + end].strip()
    return None


def has_solution_block(text: str) -> bool:
    return "<SOLUTION>" in text and "</SOLUTION>" in text


def _coerce_int(value: Any) -> tuple[int | None, str | None]:
    if isinstance(value, bool):
        return None, "bool_not_int"
    if isinstance(value, int):
        return int(value), None
    if isinstance(value, float) and math.isfinite(value):
        if abs(value - round(value)) <= 1e-6:
            return int(round(value)), "float_to_int"
        return None, "non_integral_float"
    if isinstance(value, str):
        text = value.strip()
        try:
            as_float = float(text)
        except ValueError:
            return None, "string_not_numeric"
        if math.isfinite(as_float) and abs(as_float - round(as_float)) <= 1e-6:
            return int(round(as_float)), "numeric_string_to_int"
        return None, "non_integral_numeric_string"
    return None, "not_int_like"


def _solution_payload_from_relaxed_json(parsed: Any) -> Any:
    if isinstance(parsed, list):
        return parsed
    if not isinstance(parsed, dict):
        return parsed
    for key in ("solution", "SOLUTION", "final_solution", "phase_waits", "phases", "plan"):
        value = parsed.get(key)
        if isinstance(value, (list, dict)):
            return value
    return parsed


def normalize_solution_relaxed(parsed: Any) -> tuple[dict[str, int] | None, str | None, list[str]]:
    payload = _solution_payload_from_relaxed_json(parsed)
    actions: list[str] = []
    if isinstance(payload, dict):
        out: dict[str, int] = {}
        for key, value in payload.items():
            if isinstance(value, dict):
                value = (
                    value.get("final")
                    if "final" in value
                    else value.get("green_time")
                    if "green_time" in value
                    else value.get("green")
                    if "green" in value
                    else value.get("duration")
                    if "duration" in value
                    else value.get("green_duration")
                )
                actions.append("dict_nested_value")
            phase_id, phase_action = _coerce_int(key)
            final, final_action = _coerce_int(value)
            if phase_id is None:
                return None, f"phase_id_{phase_action}", actions
            if final is None:
                return None, f"final_{final_action}", actions
            if phase_action:
                actions.append(f"phase_id_{phase_action}")
            if final_action:
                actions.append(f"final_{final_action}")
            out[str(phase_id)] = final
        return out, None, sorted(set(actions))
    if isinstance(payload, list):
        out = {}
        for item in payload:
            if not isinstance(item, dict):
                return None, "array_item_not_object", actions
            phase_value = item.get("phase_id", item.get("phase", item.get("id")))
            final_value = (
                item.get("final")
                if "final" in item
                else item.get("green_time")
                if "green_time" in item
                else item.get("green")
                if "green" in item
                else item.get("duration")
                if "duration" in item
                else item.get("green_duration")
            )
            if set(item) != {"phase_id", "final"}:
                actions.append("drop_extra_or_alias_fields")
            phase_id, phase_action = _coerce_int(phase_value)
            final, final_action = _coerce_int(final_value)
            if phase_id is None:
                return None, f"phase_id_{phase_action}", actions
            if final is None:
                return None, f"final_{final_action}", actions
            if phase_action:
                actions.append(f"phase_id_{phase_action}")
            if final_action:
                actions.append(f"final_{final_action}")
            out[str(phase_id)] = final
        return out, None, sorted(set(actions))
    return None, "solution_not_dict_or_array", actions


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


def validate_directional_control(
    phase_waits: list[dict[str, Any]],
    solution: dict[str, int] | None,
    args: argparse.Namespace,
) -> tuple[bool, list[str]]:
    control_usable, base_violations = validate_plan(phase_waits, solution)
    if not control_usable or base_violations:
        return False, base_violations or ["not_control_usable"]

    min_delta = float(args.directional_control_min_delta_sec)
    saturation_gap = float(args.directional_control_saturation_gap)
    green_tolerance = float(args.directional_control_green_tolerance_sec)

    expected = [str(item["phase_id"]) for item in phase_waits]
    finals = [int(solution[phase_id]) for phase_id in expected]
    defaults = [int(item["default_duration"]) for item in phase_waits]
    saturations = [float(item.get("pred_saturation") or 0.0) for item in phase_waits]
    violations: list[str] = []

    if max(abs(final - default) for final, default in zip(finals, defaults)) < min_delta:
        violations.append("no_nontrivial_adjustment")

    for high_idx, high_sat in enumerate(saturations):
        for low_idx, low_sat in enumerate(saturations):
            if high_sat <= low_sat + saturation_gap:
                continue
            if finals[high_idx] + green_tolerance < finals[low_idx]:
                violations.append("higher_saturation_less_green")
                break
        if "higher_saturation_less_green" in violations:
            break

    return not violations, violations


def strict_format_ok(
    text: str,
    parsed: Any,
    phase_waits: list[dict[str, Any]],
    *,
    require_solution_block: bool = True,
) -> bool:
    if require_solution_block and not has_solution_block(text):
        return False
    if not isinstance(parsed, list):
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


def repair_solution_for_online_control(
    phase_waits: list[dict[str, Any]],
    solution: dict[str, int] | None,
) -> tuple[dict[str, int] | None, bool, list[str], str | None]:
    if solution is None:
        return None, False, [], "no_solution"
    expected = [str(item["phase_id"]) for item in phase_waits]
    got = set(solution.keys())
    missing = [phase_id for phase_id in expected if phase_id not in got]
    if missing:
        return None, False, [], "missing_phase"
    actions: list[str] = []
    extra = sorted(got - set(expected))
    if extra:
        actions.append("drop_extra_phases")
    if list(solution.keys()) != expected:
        actions.append("reorder_phases")
    by_id = {str(item["phase_id"]): item for item in phase_waits}
    repaired: dict[str, int] = {}
    for phase_id in expected:
        final = int(solution[phase_id])
        info = by_id[phase_id]
        lo = int(info["min_green"])
        hi = int(info["max_green"])
        clipped = max(lo, min(hi, final))
        if clipped != final:
            actions.append("clip_to_bounds")
        repaired[phase_id] = clipped
    control_usable, violations = validate_plan(phase_waits, repaired)
    if not control_usable or violations:
        return None, False, actions, ",".join(violations)
    return repaired, True, sorted(set(actions)), None


def model_result_error(parse_error: str, elapsed_sec: float | None = None) -> ModelResult:
    return ModelResult(
        solution=None,
        format_ok=False,
        control_usable=False,
        strict_control_usable=False,
        directional_control_usable=False,
        directional_violations=["unparseable"],
        relaxed_solution=None,
        relaxed_json_success=False,
        relaxed_control_usable=False,
        relaxed_directional_control_usable=False,
        relaxed_directional_violations=["unparseable"],
        relaxed_parse_error=parse_error,
        repaired_solution=None,
        repaired_control_usable=False,
        repaired_directional_control_usable=False,
        repaired_directional_violations=["unparseable"],
        repair_actions=[],
        repair_error=parse_error,
        violations=["unparseable"],
        relaxed_violations=["unparseable"],
        elapsed_sec=elapsed_sec,
        raw_text="",
        parse_error=parse_error,
    )


def call_model(
    port: int | None,
    prediction_input: dict[str, Any],
    args: argparse.Namespace,
) -> ModelResult:
    if args.prompt_format == "native":
        prompt = build_native_prompt(prediction_input, args.prefill)
    elif args.prompt_format == "deepsignal":
        prompt = build_deepsignal_prompt(prediction_input, args.prefill)
    else:
        prompt = build_deepsignal_json_prompt(prediction_input, args.prefill)

    if args.model_backend == "openai":
        content, meta = _post_openai_chat(prompt, args)
    elif args.model_backend == "hf":
        messages = build_hf_chat_messages(prompt, args) if args.hf_use_chat_template else None
        content, meta = _post_hf_generate(prompt, args, messages=messages)
    else:
        if port is None:
            raise ValueError("llama backend requires a server port")
        content, meta = _post_completion(port, prompt, args)
    if not content and meta.get("error"):
        parse_error = f"http_error: {meta.get('error')}"
        return model_result_error(parse_error, meta.get("elapsed_sec"))
    raw_text = ("<start_working_out>" + content) if args.prefill else content
    if args.json_stop_after_first and args.prompt_format == "deepsignal_json":
        raw_text = first_complete_json_text(raw_text) or raw_text
    parsed, parse_error = extract_solution_json(raw_text)
    solution, normalize_error = (
        normalize_solution(parsed, allow_dict=args.input_mode != "github_official")
        if parse_error is None
        else (None, None)
    )
    if normalize_error is not None:
        parse_error = normalize_error
    control_usable_raw, violations = validate_plan(
        prediction_input["prediction"]["phase_waits"],
        solution,
    )
    control_usable = bool(control_usable_raw and not violations)
    format_ok = parse_error is None and strict_format_ok(
        raw_text,
        parsed,
        prediction_input["prediction"]["phase_waits"],
        require_solution_block=args.prompt_format == "deepsignal",
    )
    strict_control_usable = bool(format_ok and control_usable)
    directional_control_usable, directional_violations = (
        validate_directional_control(
            prediction_input["prediction"]["phase_waits"],
            solution,
            args,
        )
        if strict_control_usable
        else (False, violations or ["not_strict_control_usable"])
    )

    relaxed_parsed, relaxed_parse_error = extract_solution_json_relaxed(raw_text)
    relaxed_solution, relaxed_normalize_error, relaxed_actions = (
        normalize_solution_relaxed(relaxed_parsed)
        if relaxed_parse_error is None
        else (None, None, [])
    )
    if relaxed_normalize_error is not None:
        relaxed_parse_error = relaxed_normalize_error
    relaxed_json_success = relaxed_parse_error is None and relaxed_solution is not None
    relaxed_control_usable_raw, relaxed_violations = validate_plan(
        prediction_input["prediction"]["phase_waits"],
        relaxed_solution,
    )
    relaxed_control_usable = bool(relaxed_json_success and relaxed_control_usable_raw and not relaxed_violations)
    relaxed_directional_control_usable, relaxed_directional_violations = (
        validate_directional_control(
            prediction_input["prediction"]["phase_waits"],
            relaxed_solution,
            args,
        )
        if relaxed_control_usable
        else (False, relaxed_violations or ["not_relaxed_control_usable"])
    )
    repaired_solution, repaired_control_usable, repair_actions, repair_error = (
        repair_solution_for_online_control(
            prediction_input["prediction"]["phase_waits"],
            relaxed_solution,
        )
    )
    repaired_directional_control_usable, repaired_directional_violations = (
        validate_directional_control(
            prediction_input["prediction"]["phase_waits"],
            repaired_solution,
            args,
        )
        if repaired_control_usable
        else (False, [repair_error or "not_repaired_control_usable"])
    )
    repair_actions = sorted(set(relaxed_actions + repair_actions))
    return ModelResult(
        solution=solution,
        format_ok=format_ok,
        control_usable=control_usable,
        strict_control_usable=strict_control_usable,
        directional_control_usable=directional_control_usable,
        directional_violations=directional_violations,
        relaxed_solution=relaxed_solution,
        relaxed_json_success=relaxed_json_success,
        relaxed_control_usable=relaxed_control_usable,
        relaxed_directional_control_usable=relaxed_directional_control_usable,
        relaxed_directional_violations=relaxed_directional_violations,
        relaxed_parse_error=relaxed_parse_error,
        repaired_solution=repaired_solution,
        repaired_control_usable=repaired_control_usable,
        repaired_directional_control_usable=repaired_directional_control_usable,
        repaired_directional_violations=repaired_directional_violations,
        repair_actions=repair_actions,
        repair_error=repair_error,
        violations=violations,
        relaxed_violations=relaxed_violations,
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
        max_dur = int(round(float(getattr(phase, "maxDur", 0) or 0)))
        min_green = int(args.min_green)
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
                "default_duration": phase.default_duration,
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
                "pred_wait": round(float(pred_wait), 6),
                "pred_saturation": pred_saturation,
                "default_duration": phase.default_duration,
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


def safe_path_token(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)


def tripinfo_output_path(args: argparse.Namespace, scenario: str, tl_id: str) -> Path:
    output_dir = Path(args.output_dir) if args.output_dir else PROJECT_ROOT / "runs" / "deepsignal_cycleplan"
    name = f"{safe_path_token(scenario)}__{safe_path_token(tl_id)}.tripinfo.xml"
    return output_dir / "sumo_outputs" / "tripinfo" / name


def mean_or_none(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def parse_tripinfo_att_awt_metrics(
    tripinfo_path: Path | None,
    network_metric_departed_vehicle_ids: set[str],
    target_tl_seen_vehicle_ids: set[str],
    metric_start: int,
    metric_end: int,
    total_steps: int,
    drain_seconds: int,
) -> dict[str, Any]:
    base = {
        "tripinfo_path": str(tripinfo_path) if tripinfo_path else None,
        "tripinfo_enabled": bool(tripinfo_path),
        "tripinfo_parse_error": None,
        "tripinfo_drain_seconds": drain_seconds,
        "tripinfo_total_steps": total_steps,
        "network_metric_departed_vehicle_count": len(network_metric_departed_vehicle_ids),
        "network_trip_completed_count": 0,
        "network_trip_completion_ratio": None,
        "network_travel_time_total_sec": 0.0,
        "network_waiting_time_total_sec": 0.0,
        "network_att_sec": None,
        "network_awt_sec": None,
        "target_tl_seen_vehicle_count": len(target_tl_seen_vehicle_ids),
        "target_tl_trip_completed_count": 0,
        "target_tl_trip_completion_ratio": None,
        "target_tl_travel_time_total_sec": 0.0,
        "target_tl_waiting_time_total_sec": 0.0,
        "target_tl_att_sec": None,
        "target_tl_awt_sec": None,
    }
    if tripinfo_path is None:
        return base
    if not tripinfo_path.exists():
        base["tripinfo_parse_error"] = "tripinfo_missing"
        return base

    network_durations: list[float] = []
    network_waits: list[float] = []
    target_durations: list[float] = []
    target_waits: list[float] = []
    try:
        root = ET.parse(tripinfo_path).getroot()
        for trip in root.iter("tripinfo"):
            veh_id = trip.attrib.get("id")
            if not veh_id:
                continue
            try:
                duration = float(trip.attrib["duration"])
                waiting_time = float(trip.attrib.get("waitingTime", "0"))
            except (KeyError, TypeError, ValueError):
                continue
            if veh_id in network_metric_departed_vehicle_ids:
                network_durations.append(duration)
                network_waits.append(waiting_time)
            if veh_id in target_tl_seen_vehicle_ids:
                target_durations.append(duration)
                target_waits.append(waiting_time)
    except Exception as exc:
        base["tripinfo_parse_error"] = f"{type(exc).__name__}: {exc}"
        return base

    network_departed = len(network_metric_departed_vehicle_ids)
    target_seen = len(target_tl_seen_vehicle_ids)
    base.update(
        {
            "network_trip_completed_count": len(network_durations),
            "network_trip_completion_ratio": (
                len(network_durations) / network_departed if network_departed else None
            ),
            "network_travel_time_total_sec": float(sum(network_durations)),
            "network_waiting_time_total_sec": float(sum(network_waits)),
            "network_att_sec": mean_or_none(network_durations),
            "network_awt_sec": mean_or_none(network_waits),
            "target_tl_trip_completed_count": len(target_durations),
            "target_tl_trip_completion_ratio": (
                len(target_durations) / target_seen if target_seen else None
            ),
            "target_tl_travel_time_total_sec": float(sum(target_durations)),
            "target_tl_waiting_time_total_sec": float(sum(target_waits)),
            "target_tl_att_sec": mean_or_none(target_durations),
            "target_tl_awt_sec": mean_or_none(target_waits),
        }
    )
    return base


def start_sumo(
    traci: Any,
    sumocfg: Path,
    args: argparse.Namespace,
    label: str,
    *,
    tripinfo_path: Path | None = None,
    end_time: int | None = None,
) -> Any:
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
    if end_time is not None:
        cmd.extend(["--end", str(end_time)])
    if tripinfo_path is not None:
        tripinfo_path.parent.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--tripinfo-output", str(tripinfo_path)])
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
    metric_start = args.warmup_seconds
    metric_end = args.warmup_seconds + args.metric_seconds
    control_total_steps = args.simulation_seconds if args.simulation_seconds is not None else metric_end
    metric_end = min(metric_end, control_total_steps)
    tripinfo_drain_seconds = max(0, int(args.tripinfo_drain_seconds)) if args.tripinfo_metrics else 0
    total_steps = control_total_steps + tripinfo_drain_seconds
    tripinfo_path = tripinfo_output_path(args, scenario, tl_id) if args.tripinfo_metrics else None
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
    if total_steps is not None:
        sumo_cmd_preview.extend(["--end", str(total_steps)])
    if tripinfo_path is not None:
        sumo_cmd_preview.extend(["--tripinfo-output", str(tripinfo_path)])
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
    traci = start_sumo(
        traci_mod,
        sumocfg,
        args,
        traci_label,
        tripinfo_path=tripinfo_path,
        end_time=total_steps,
    )
    forecaster = make_pred_wait_forecaster(args)
    queue_samples_raw: list[float] = []
    queue_samples_split_overlap: list[float] = []
    local_delay_total_s = 0.0
    incoming_vehicle_observations = 0
    throughput_total_intersection_passages = 0
    queue_thresholds = normalized_queue_thresholds(args)
    queue_over_threshold_steps_by_threshold = {threshold: 0 for threshold in queue_thresholds}
    max_continuous_queue_over_threshold_steps_by_threshold = {threshold: 0 for threshold in queue_thresholds}
    current_continuous_queue_over_threshold_steps_by_threshold = {threshold: 0 for threshold in queue_thresholds}
    primary_queue_threshold = float(args.queue_threshold)
    prev_incoming_vehicle_ids: set[str] | None = None
    last_time_loss_by_vehicle: dict[str, float] = {}
    network_metric_departed_vehicle_ids: set[str] = set()
    target_tl_seen_vehicle_ids: set[str] = set()
    response_times: list[float] = []
    format_ok = 0
    control_usable = 0
    strict_control_usable = 0
    directional_control_usable = 0
    relaxed_json_success = 0
    relaxed_control_usable = 0
    relaxed_directional_control_usable = 0
    repaired_control_usable = 0
    repaired_directional_control_usable = 0
    repair_applied = 0
    calls = 0
    applied = 0
    fallback_applied = 0
    parse_errors: dict[str, int] = {}
    directional_violations: dict[str, int] = {}
    relaxed_directional_violations: dict[str, int] = {}
    repaired_directional_violations: dict[str, int] = {}
    relaxed_parse_errors: dict[str, int] = {}
    repair_errors: dict[str, int] = {}
    repair_actions: dict[str, int] = {}
    green_phases: list[GreenPhase] = []
    last_decision = -10**9
    steps_completed = 0
    skipped_forecaster_not_ready = 0
    pending_cycle_plan: dict[str, Any] | None = None
    plans_queued = 0
    delayed_plans_applied = 0

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

        write_benchmark_log(
            benchmark_log_fh,
            "simulation_window_ready",
            {
                "scenario": scenario,
                "usage": usage,
                "tl_id": tl_id,
                "total_steps": total_steps,
                "control_total_steps": control_total_steps,
                "tripinfo_drain_seconds": tripinfo_drain_seconds,
                "metric_start": metric_start,
                "metric_end": metric_end,
                "gui": bool(args.gui),
                "gui_delay_ms": args.gui_delay_ms if args.gui else None,
            },
            to_stderr=args.log_events_to_stderr,
        )

        for step in range(total_steps):
            sim_time = int(traci.simulation.getTime())
            should_decide = (
                args.controller == "model"
                and sim_time < control_total_steps
                and sim_time - last_decision >= args.decision_interval_seconds
            )
            if should_decide:
                if args.action_delay_cycles == 1 and pending_cycle_plan is not None:
                    apply_cycle_plan(traci, tl_id, green_phases, pending_cycle_plan["solution"])
                    applied += 1
                    delayed_plans_applied += 1
                    if pending_cycle_plan.get("fallback_reason"):
                        fallback_applied += 1
                    write_benchmark_log(
                        benchmark_log_fh,
                        "delayed_plan_applied",
                        {
                            "scenario": scenario,
                            "usage": usage,
                            "tl_id": tl_id,
                            "sim_time": sim_time,
                            "applied_solution": pending_cycle_plan["solution"],
                            "applied_control_mode": pending_cycle_plan.get("control_mode"),
                            "fallback_applied": bool(pending_cycle_plan.get("fallback_reason")),
                            "fallback_reason": pending_cycle_plan.get("fallback_reason"),
                            "generated_sim_time": pending_cycle_plan.get("generated_sim_time"),
                            "generated_call_index": pending_cycle_plan.get("generated_call_index"),
                            "action_delay_cycles": args.action_delay_cycles,
                        },
                        to_stderr=args.log_events_to_stderr,
                    )
                    pending_cycle_plan = None
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
                    if result.strict_control_usable:
                        control_usable += 1
                        strict_control_usable += 1
                    if result.directional_control_usable:
                        directional_control_usable += 1
                    if result.relaxed_json_success:
                        relaxed_json_success += 1
                    if result.relaxed_control_usable:
                        relaxed_control_usable += 1
                    if result.relaxed_directional_control_usable:
                        relaxed_directional_control_usable += 1
                    if result.repaired_control_usable:
                        repaired_control_usable += 1
                    if result.repaired_directional_control_usable:
                        repaired_directional_control_usable += 1
                    if result.elapsed_sec is not None:
                        response_times.append(float(result.elapsed_sec))
                    if result.parse_error:
                        parse_errors[result.parse_error] = parse_errors.get(result.parse_error, 0) + 1
                    for violation in result.directional_violations:
                        directional_violations[violation] = directional_violations.get(violation, 0) + 1
                    for violation in result.relaxed_directional_violations:
                        relaxed_directional_violations[violation] = (
                            relaxed_directional_violations.get(violation, 0) + 1
                        )
                    for violation in result.repaired_directional_violations:
                        repaired_directional_violations[violation] = (
                            repaired_directional_violations.get(violation, 0) + 1
                        )
                    if result.relaxed_parse_error:
                        relaxed_parse_errors[result.relaxed_parse_error] = (
                            relaxed_parse_errors.get(result.relaxed_parse_error, 0) + 1
                        )
                    if result.repair_error:
                        repair_errors[result.repair_error] = repair_errors.get(result.repair_error, 0) + 1
                    for action in result.repair_actions:
                        repair_actions[action] = repair_actions.get(action, 0) + 1
                    candidate_solution = None
                    fallback_reason = None
                    candidate_control_mode = None
                    if args.online_control_mode == "strict" and result.strict_control_usable and result.solution is not None:
                        candidate_solution = clip_solution(prediction_input["prediction"]["phase_waits"], result.solution)
                        candidate_control_mode = "strict"
                    elif (
                        args.online_control_mode == "directional"
                        and result.directional_control_usable
                        and result.solution is not None
                    ):
                        candidate_solution = clip_solution(prediction_input["prediction"]["phase_waits"], result.solution)
                        candidate_control_mode = "directional"
                    elif (
                        args.online_control_mode == "relaxed"
                        and result.relaxed_control_usable
                        and result.relaxed_solution is not None
                    ):
                        candidate_solution = clip_solution(
                            prediction_input["prediction"]["phase_waits"],
                            result.relaxed_solution,
                        )
                        candidate_control_mode = "relaxed"
                    elif (
                        args.online_control_mode == "relaxed_directional"
                        and result.relaxed_directional_control_usable
                        and result.relaxed_solution is not None
                    ):
                        candidate_solution = clip_solution(
                            prediction_input["prediction"]["phase_waits"],
                            result.relaxed_solution,
                        )
                        candidate_control_mode = "relaxed_directional"
                    elif (
                        args.online_control_mode == "repaired"
                        and result.repaired_control_usable
                        and result.repaired_solution is not None
                    ):
                        candidate_solution = result.repaired_solution
                        candidate_control_mode = "repaired"
                        if result.repair_actions:
                            repair_applied += 1
                    elif (
                        args.online_control_mode == "repaired_directional"
                        and result.repaired_directional_control_usable
                        and result.repaired_solution is not None
                    ):
                        candidate_solution = result.repaired_solution
                        candidate_control_mode = "repaired_directional"
                        if result.repair_actions:
                            repair_applied += 1

                    if candidate_solution is None and args.model_fail_policy in {
                        "min_green",
                        "first_min_green",
                        "random_valid",
                    }:
                        if args.model_fail_policy == "first_min_green":
                            candidate_solution = {str(green_phases[0].phase_id): int(green_phases[0].min_green)}
                        elif args.model_fail_policy == "random_valid":
                            index = (int(sim_time) + calls + int(args.seed)) % len(green_phases)
                            phase = green_phases[index]
                            candidate_solution = {str(phase.phase_id): int(phase.min_green)}
                        else:
                            candidate_solution = {
                                str(phase.phase_id): int(phase.min_green)
                                for phase in green_phases
                            }
                        candidate_control_mode = f"fallback:{args.model_fail_policy}"
                        fallback_reason = result.parse_error or "model_output_not_control_usable"

                    applied_solution = None
                    queued_solution = None
                    applied_control_mode = None
                    queued_control_mode = None
                    plan_queued = False
                    if candidate_solution is not None and args.action_delay_cycles == 0:
                        apply_cycle_plan(traci, tl_id, green_phases, candidate_solution)
                        applied_solution = candidate_solution
                        applied_control_mode = candidate_control_mode
                        applied += 1
                        if fallback_reason:
                            fallback_applied += 1
                    elif candidate_solution is not None:
                        pending_cycle_plan = {
                            "solution": candidate_solution,
                            "control_mode": candidate_control_mode,
                            "fallback_reason": fallback_reason,
                            "generated_sim_time": sim_time,
                            "generated_call_index": calls,
                        }
                        plans_queued += 1
                        queued_solution = candidate_solution
                        queued_control_mode = candidate_control_mode
                        plan_queued = True
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
                            "control_usable": result.strict_control_usable,
                            "strict_format_ok": result.format_ok,
                            "strict_control_usable": result.strict_control_usable,
                            "directional_control_usable": result.directional_control_usable,
                            "relaxed_json_success": result.relaxed_json_success,
                            "relaxed_control_usable": result.relaxed_control_usable,
                            "relaxed_directional_control_usable": result.relaxed_directional_control_usable,
                            "repaired_control_usable": result.repaired_control_usable,
                            "repaired_directional_control_usable": result.repaired_directional_control_usable,
                            "repair_actions": result.repair_actions,
                            "online_control_mode": args.online_control_mode,
                            "action_delay_cycles": args.action_delay_cycles,
                            "applied_control_mode": applied_control_mode,
                            "queued_control_mode": queued_control_mode,
                            "plan_applied": bool(applied_solution is not None),
                            "plan_queued": plan_queued,
                            "queued_solution": queued_solution,
                            "fallback_selected": bool(fallback_reason),
                            "fallback_applied": bool(fallback_reason and applied_solution is not None),
                            "fallback_reason": fallback_reason,
                            "pending_plan_after_call": bool(pending_cycle_plan is not None),
                            "elapsed_sec": result.elapsed_sec,
                            "parse_error": result.parse_error,
                            "relaxed_parse_error": result.relaxed_parse_error,
                            "repair_error": result.repair_error,
                            "violations": result.violations,
                            "directional_violations": result.directional_violations,
                            "relaxed_directional_violations": result.relaxed_directional_violations,
                            "repaired_directional_violations": result.repaired_directional_violations,
                            "relaxed_violations": result.relaxed_violations,
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
                            "relaxed_solution": result.relaxed_solution,
                            "repaired_solution": result.repaired_solution,
                            "applied_solution": applied_solution,
                            "queued_solution": queued_solution,
                            "online_control_mode": args.online_control_mode,
                            "action_delay_cycles": args.action_delay_cycles,
                            "applied_control_mode": applied_control_mode,
                            "queued_control_mode": queued_control_mode,
                            "plan_queued": plan_queued,
                            "fallback_reason": fallback_reason,
                            "format_ok": result.format_ok,
                            "control_usable": result.strict_control_usable,
                            "strict_format_ok": result.format_ok,
                            "strict_control_usable": result.strict_control_usable,
                            "directional_control_usable": result.directional_control_usable,
                            "relaxed_json_success": result.relaxed_json_success,
                            "relaxed_control_usable": result.relaxed_control_usable,
                            "relaxed_directional_control_usable": result.relaxed_directional_control_usable,
                            "repaired_control_usable": result.repaired_control_usable,
                            "repaired_directional_control_usable": result.repaired_directional_control_usable,
                            "repair_actions": result.repair_actions,
                            "violations": result.violations,
                            "directional_violations": result.directional_violations,
                            "relaxed_directional_violations": result.relaxed_directional_violations,
                            "repaired_directional_violations": result.repaired_directional_violations,
                            "relaxed_violations": result.relaxed_violations,
                            "elapsed_sec": result.elapsed_sec,
                            "parse_error": result.parse_error,
                            "relaxed_parse_error": result.relaxed_parse_error,
                            "repair_error": result.repair_error,
                            "raw_text_tail": result.raw_text[-1000:],
                        },
                    )

            traci.simulationStep()
            steps_completed += 1
            if forecaster is not None and step < control_total_steps:
                forecaster.observe(traci, green_phases)
            current_incoming = incoming_vehicle_ids(traci, green_phases)
            if metric_start <= step < metric_end:
                try:
                    network_metric_departed_vehicle_ids.update(traci.simulation.getDepartedIDList())
                except Exception:
                    pass
                target_tl_seen_vehicle_ids.update(current_incoming)
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
                step_queues = (
                    sample.phase_queues_split_overlap
                    if args.phase_queue_mode == "split-overlap"
                    else sample.phase_queues_raw
                )
                step_max_queue = max(step_queues) if step_queues else None
                for threshold in queue_thresholds:
                    if step_max_queue is not None and step_max_queue > threshold:
                        queue_over_threshold_steps_by_threshold[threshold] += 1
                        current_continuous_queue_over_threshold_steps_by_threshold[threshold] += 1
                        max_continuous_queue_over_threshold_steps_by_threshold[threshold] = max(
                            max_continuous_queue_over_threshold_steps_by_threshold[threshold],
                            current_continuous_queue_over_threshold_steps_by_threshold[threshold],
                        )
                    else:
                        current_continuous_queue_over_threshold_steps_by_threshold[threshold] = 0
                local_delay_total_s += sample.local_delay_delta_s
                incoming_vehicle_observations += sample.incoming_vehicle_count
                throughput_total_intersection_passages += sample.passage_count
                prev_incoming_vehicle_ids = current_incoming
            elif step < metric_start:
                prev_incoming_vehicle_ids = current_incoming
                last_time_loss_by_vehicle.clear()
    finally:
        try:
            traci.close(bool(tripinfo_path))
            write_benchmark_log(
                benchmark_log_fh,
                "sumo_closed",
                {
                    "scenario": scenario,
                    "usage": usage,
                    "tl_id": tl_id,
                    "traci_label": traci_label,
                    "steps_completed": steps_completed,
                    "tripinfo_path": str(tripinfo_path) if tripinfo_path else None,
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
    if primary_queue_threshold not in queue_over_threshold_steps_by_threshold:
        primary_queue_threshold = queue_thresholds[0]
    queue_over_threshold_by_key = {
        metric_key_float(threshold): float(seconds)
        for threshold, seconds in queue_over_threshold_steps_by_threshold.items()
    }
    queue_over_threshold_fraction_by_key = {
        metric_key_float(threshold): (
            seconds / max(1, metric_end - metric_start) if metric_active else None
        )
        for threshold, seconds in queue_over_threshold_steps_by_threshold.items()
    }
    max_continuous_queue_over_threshold_by_key = {
        metric_key_float(threshold): float(seconds)
        for threshold, seconds in max_continuous_queue_over_threshold_steps_by_threshold.items()
    }
    tripinfo_metrics = parse_tripinfo_att_awt_metrics(
        tripinfo_path,
        network_metric_departed_vehicle_ids,
        target_tl_seen_vehicle_ids,
        metric_start,
        metric_end,
        total_steps,
        tripinfo_drain_seconds,
    )
    write_benchmark_log(
        benchmark_log_fh,
        "tripinfo_metrics_parsed",
        {
            "scenario": scenario,
            "usage": usage,
            "tl_id": tl_id,
            "tripinfo_path": tripinfo_metrics["tripinfo_path"],
            "tripinfo_parse_error": tripinfo_metrics["tripinfo_parse_error"],
            "network_metric_departed_vehicle_count": tripinfo_metrics[
                "network_metric_departed_vehicle_count"
            ],
            "network_trip_completed_count": tripinfo_metrics["network_trip_completed_count"],
            "network_att_sec": tripinfo_metrics["network_att_sec"],
            "network_awt_sec": tripinfo_metrics["network_awt_sec"],
            "target_tl_seen_vehicle_count": tripinfo_metrics["target_tl_seen_vehicle_count"],
            "target_tl_trip_completed_count": tripinfo_metrics["target_tl_trip_completed_count"],
            "target_tl_att_sec": tripinfo_metrics["target_tl_att_sec"],
            "target_tl_awt_sec": tripinfo_metrics["target_tl_awt_sec"],
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
        "model_fail_policy": args.model_fail_policy if args.controller == "model" else None,
        "demand_scale": args.demand_scale,
        "warmup_seconds": args.warmup_seconds,
        "metric_seconds": metric_end - metric_start,
        "eval_minutes": metric_minutes,
        "phase_queue_mode": args.phase_queue_mode,
        "forecaster_not_ready_steps": skipped_forecaster_not_ready,
        "online_control_mode": args.online_control_mode if args.controller == "model" else None,
        "action_delay_cycles": args.action_delay_cycles if args.controller == "model" else 0,
        "format_success_rate": (format_ok / calls * 100.0) if calls else None,
        "control_usable_rate": (control_usable / calls * 100.0) if calls else None,
        "strict_format_success_rate": (format_ok / calls * 100.0) if calls else None,
        "strict_control_usable_rate": (strict_control_usable / calls * 100.0) if calls else None,
        "directional_control_usable_rate": (directional_control_usable / calls * 100.0) if calls else None,
        "relaxed_json_success_rate": (relaxed_json_success / calls * 100.0) if calls else None,
        "relaxed_control_usable_rate": (relaxed_control_usable / calls * 100.0) if calls else None,
        "relaxed_directional_control_usable_rate": (
            relaxed_directional_control_usable / calls * 100.0
        )
        if calls
        else None,
        "repaired_control_usable_rate": (repaired_control_usable / calls * 100.0) if calls else None,
        "repaired_directional_control_usable_rate": (
            repaired_directional_control_usable / calls * 100.0
        )
        if calls
        else None,
        "repair_applied_rate": (repair_applied / calls * 100.0) if calls else None,
        "lint_success_rate": (control_usable / calls * 100.0) if calls else None,
        "model_calls": calls,
        "decision_count": calls,
        "plans_queued": plans_queued,
        "delayed_plans_applied": delayed_plans_applied,
        "pending_plan_left_unapplied": bool(pending_cycle_plan is not None),
        "plans_applied": applied,
        "plans_applied_rate": (applied / calls * 100.0) if calls else None,
        "fallback_plans_applied": fallback_applied,
        "fallback_plan_rate": (fallback_applied / calls * 100.0) if calls else None,
        "active_tl": metric_active,
        "inactive_reason": inactive_reason,
        "metric_window_steps": max(0, metric_end - metric_start),
        "metric_vehicle_observations": float(incoming_vehicle_observations),
        "throughput_total_intersection_passages": float(throughput_total_intersection_passages),
        "passage_per_metric_observation": passage_per_metric_observation,
        "passage_seen_ratio_approx": passage_per_metric_observation,
        "queue_sample_count": len(queue_samples_raw),
        "queue_threshold": primary_queue_threshold,
        "queue_thresholds": queue_thresholds,
        "queue_over_threshold_seconds": float(
            queue_over_threshold_steps_by_threshold[primary_queue_threshold]
        ),
        "queue_over_threshold_fraction": (
            queue_over_threshold_steps_by_threshold[primary_queue_threshold]
            / max(1, metric_end - metric_start)
            if metric_active
            else None
        ),
        "max_continuous_queue_over_threshold_seconds": float(
            max_continuous_queue_over_threshold_steps_by_threshold[primary_queue_threshold]
        ),
        "queue_over_threshold_seconds_by_threshold": queue_over_threshold_by_key,
        "queue_over_threshold_fraction_by_threshold": queue_over_threshold_fraction_by_key,
        "max_continuous_queue_over_threshold_seconds_by_threshold": (
            max_continuous_queue_over_threshold_by_key
        ),
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
        "directional_violations": directional_violations,
        "relaxed_directional_violations": relaxed_directional_violations,
        "repaired_directional_violations": repaired_directional_violations,
        "relaxed_parse_errors": relaxed_parse_errors,
        "repair_errors": repair_errors,
        "repair_actions": repair_actions,
        **tripinfo_metrics,
    }
    for threshold in queue_thresholds:
        suffix = metric_key_float(threshold)
        row[f"queue_over_threshold_seconds_t{suffix}"] = queue_over_threshold_by_key[suffix]
        row[f"queue_over_threshold_fraction_t{suffix}"] = queue_over_threshold_fraction_by_key[suffix]
        row[f"max_continuous_queue_over_threshold_seconds_t{suffix}"] = (
            max_continuous_queue_over_threshold_by_key[suffix]
        )
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

    def weighted_sum_over_count(
        items: list[dict[str, Any]],
        total_key: str,
        count_key: str,
    ) -> float | None:
        numerator = sum(float(row.get(total_key) or 0.0) for row in items)
        denominator = sum(float(row.get(count_key) or 0.0) for row in items)
        return numerator / denominator if denominator else None

    def weighted_completion_ratio(
        items: list[dict[str, Any]],
        completed_key: str,
        denominator_key: str,
    ) -> float | None:
        completed = sum(float(row.get(completed_key) or 0.0) for row in items)
        denominator = sum(float(row.get(denominator_key) or 0.0) for row in items)
        return completed / denominator if denominator else None

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

    def weighted_queue_over_threshold_fraction(items: list[dict[str, Any]]) -> float | None:
        numerator = sum(float(row.get("queue_over_threshold_seconds") or 0.0) for row in items)
        denominator = sum(float(row.get("metric_window_steps") or 0.0) for row in items)
        return numerator / denominator if denominator else None

    def metric_bundle(items: list[dict[str, Any]], key: str) -> dict[str, float | None]:
        bucket = vals(items, key)
        if key in {
            "format_success_rate",
            "control_usable_rate",
            "strict_format_success_rate",
            "strict_control_usable_rate",
            "directional_control_usable_rate",
            "relaxed_json_success_rate",
            "relaxed_control_usable_rate",
            "relaxed_directional_control_usable_rate",
            "repaired_control_usable_rate",
            "repaired_directional_control_usable_rate",
            "repair_applied_rate",
            "plans_applied_rate",
            "fallback_plan_rate",
        }:
            value = weighted_rate(items, key)
        elif key in {"avg_queue_vehicles", "p95_queue_vehicles", "max_queue_vehicles"}:
            value = weighted_queue(items, key)
        elif key == "avg_delay_per_vehicle_sec":
            value = weighted_delay(items)
        elif key == "throughput_veh_per_min":
            value = weighted_throughput(items)
        elif key == "avg_response_time_sec":
            value = weighted_response_time(items)
        elif key == "network_att_sec":
            value = weighted_sum_over_count(
                items,
                "network_travel_time_total_sec",
                "network_trip_completed_count",
            )
        elif key == "network_awt_sec":
            value = weighted_sum_over_count(
                items,
                "network_waiting_time_total_sec",
                "network_trip_completed_count",
            )
        elif key == "target_tl_att_sec":
            value = weighted_sum_over_count(
                items,
                "target_tl_travel_time_total_sec",
                "target_tl_trip_completed_count",
            )
        elif key == "target_tl_awt_sec":
            value = weighted_sum_over_count(
                items,
                "target_tl_waiting_time_total_sec",
                "target_tl_trip_completed_count",
            )
        elif key == "network_trip_completion_ratio":
            value = weighted_completion_ratio(
                items,
                "network_trip_completed_count",
                "network_metric_departed_vehicle_count",
            )
        elif key == "target_tl_trip_completion_ratio":
            value = weighted_completion_ratio(
                items,
                "target_tl_trip_completed_count",
                "target_tl_seen_vehicle_count",
            )
        elif key == "local_delay_per_intersection_minute_sec":
            value = weighted_local_delay_per_minute(items)
        elif key in {"passage_per_metric_observation", "passage_seen_ratio_approx"}:
            value = weighted_passage_observation_ratio(items)
        elif key == "queue_over_threshold_fraction":
            value = weighted_queue_over_threshold_fraction(items)
        elif key == "queue_over_threshold_seconds":
            value = float(mean(bucket)) if bucket else None
        elif key == "max_continuous_queue_over_threshold_seconds":
            value = float(mean(bucket)) if bucket else None
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
            "queue_over_threshold_seconds": sum(
                float(row.get("queue_over_threshold_seconds") or 0.0) for row in items
            ),
            "network_metric_departed_vehicle_count": sum(
                float(row.get("network_metric_departed_vehicle_count") or 0.0) for row in items
            ),
            "network_trip_completed_count": sum(
                float(row.get("network_trip_completed_count") or 0.0) for row in items
            ),
            "target_tl_seen_vehicle_count": sum(
                float(row.get("target_tl_seen_vehicle_count") or 0.0) for row in items
            ),
            "target_tl_trip_completed_count": sum(
                float(row.get("target_tl_trip_completed_count") or 0.0) for row in items
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
        "strict_format_success_rate",
        "strict_control_usable_rate",
        "directional_control_usable_rate",
        "relaxed_json_success_rate",
        "relaxed_control_usable_rate",
        "relaxed_directional_control_usable_rate",
        "repaired_control_usable_rate",
        "repaired_directional_control_usable_rate",
        "repair_applied_rate",
        "plans_applied_rate",
        "fallback_plan_rate",
        "avg_queue_vehicles",
        "p95_queue_vehicles",
        "max_queue_vehicles",
        "avg_delay_per_vehicle_sec",
        "local_delay_per_intersection_minute_sec",
        "passage_per_metric_observation",
        "passage_seen_ratio_approx",
        "queue_over_threshold_seconds",
        "queue_over_threshold_fraction",
        "max_continuous_queue_over_threshold_seconds",
        "queue_over_threshold_seconds_t10",
        "queue_over_threshold_seconds_t20",
        "queue_over_threshold_seconds_t30",
        "queue_over_threshold_seconds_t40",
        "max_continuous_queue_over_threshold_seconds_t10",
        "max_continuous_queue_over_threshold_seconds_t20",
        "max_continuous_queue_over_threshold_seconds_t30",
        "max_continuous_queue_over_threshold_seconds_t40",
        "throughput_veh_per_min",
        "avg_response_time_sec",
        "network_att_sec",
        "network_awt_sec",
        "network_trip_completion_ratio",
        "target_tl_att_sec",
        "target_tl_awt_sec",
        "target_tl_trip_completion_ratio",
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
            "strict_format_success_rate",
            "strict_control_usable_rate",
            "directional_control_usable_rate",
            "relaxed_json_success_rate",
            "relaxed_control_usable_rate",
            "relaxed_directional_control_usable_rate",
            "repaired_control_usable_rate",
            "repaired_directional_control_usable_rate",
            "repair_applied_rate",
            "plans_applied_rate",
            "fallback_plan_rate",
            "avg_queue_vehicles",
            "p95_queue_vehicles",
            "max_queue_vehicles",
            "avg_delay_per_vehicle_sec",
            "local_delay_per_intersection_minute_sec",
            "passage_per_metric_observation",
            "passage_seen_ratio_approx",
            "queue_over_threshold_seconds",
            "queue_over_threshold_fraction",
            "max_continuous_queue_over_threshold_seconds",
            "queue_over_threshold_seconds_t10",
            "queue_over_threshold_seconds_t20",
            "queue_over_threshold_seconds_t30",
            "queue_over_threshold_seconds_t40",
            "max_continuous_queue_over_threshold_seconds_t10",
            "max_continuous_queue_over_threshold_seconds_t20",
            "max_continuous_queue_over_threshold_seconds_t30",
            "max_continuous_queue_over_threshold_seconds_t40",
            "throughput_veh_per_min",
            "avg_response_time_sec",
            "network_att_sec",
            "network_awt_sec",
            "network_trip_completion_ratio",
            "target_tl_att_sec",
            "target_tl_awt_sec",
            "target_tl_trip_completion_ratio",
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
            "strict_format_success_rate": "weighted_by_decision_count",
            "strict_control_usable_rate": "weighted_by_decision_count",
            "directional_control_usable_rate": "weighted_by_decision_count",
            "relaxed_json_success_rate": "weighted_by_decision_count",
            "relaxed_control_usable_rate": "weighted_by_decision_count",
            "relaxed_directional_control_usable_rate": "weighted_by_decision_count",
            "repaired_control_usable_rate": "weighted_by_decision_count",
            "repaired_directional_control_usable_rate": "weighted_by_decision_count",
            "repair_applied_rate": "weighted_by_decision_count",
            "plans_applied_rate": "weighted_by_decision_count",
            "fallback_plan_rate": "weighted_by_decision_count",
            "avg_queue_vehicles": "weighted_by_queue_sample_count",
            "p95_queue_vehicles": "weighted_by_queue_sample_count",
            "max_queue_vehicles": "weighted_by_queue_sample_count",
            "avg_delay_per_vehicle_sec": "sum(local_delay_total_s)/sum(throughput_total_intersection_passages)",
            "local_delay_per_intersection_minute_sec": "sum(local_delay_total_s)/sum(eval_minutes*controlled_tls_count)",
            "passage_per_metric_observation": "sum(throughput_total_intersection_passages)/sum(metric_vehicle_observations)",
            "passage_seen_ratio_approx": "same_as_passage_per_metric_observation_not_unique_vehicle_ratio",
            "queue_over_threshold_seconds": "mean_per_run_seconds_where_any_selected_phase_queue_exceeds_threshold",
            "queue_over_threshold_fraction": "sum(queue_over_threshold_seconds)/sum(metric_window_steps)",
            "max_continuous_queue_over_threshold_seconds": "mean_per_run_max_continuous_seconds_where_any_selected_phase_queue_exceeds_threshold",
            "queue_over_threshold_seconds_t10": "mean_per_run_seconds_above_queue_threshold_10",
            "queue_over_threshold_seconds_t20": "mean_per_run_seconds_above_queue_threshold_20",
            "queue_over_threshold_seconds_t30": "mean_per_run_seconds_above_queue_threshold_30",
            "queue_over_threshold_seconds_t40": "mean_per_run_seconds_above_queue_threshold_40",
            "max_continuous_queue_over_threshold_seconds_t10": "mean_per_run_longest_continuous_seconds_above_queue_threshold_10",
            "max_continuous_queue_over_threshold_seconds_t20": "mean_per_run_longest_continuous_seconds_above_queue_threshold_20",
            "max_continuous_queue_over_threshold_seconds_t30": "mean_per_run_longest_continuous_seconds_above_queue_threshold_30",
            "max_continuous_queue_over_threshold_seconds_t40": "mean_per_run_longest_continuous_seconds_above_queue_threshold_40",
            "throughput_veh_per_min": "sum(throughput_total_intersection_passages)/sum(eval_minutes*controlled_tls_count)",
            "avg_response_time_sec": "weighted_by_decision_count",
            "network_att_sec": "sum(network_travel_time_total_sec)/sum(network_trip_completed_count)",
            "network_awt_sec": "sum(network_waiting_time_total_sec)/sum(network_trip_completed_count)",
            "network_trip_completion_ratio": "sum(network_trip_completed_count)/sum(network_metric_departed_vehicle_count)",
            "target_tl_att_sec": "sum(target_tl_travel_time_total_sec)/sum(target_tl_trip_completed_count)",
            "target_tl_awt_sec": "sum(target_tl_waiting_time_total_sec)/sum(target_tl_trip_completed_count)",
            "target_tl_trip_completion_ratio": "sum(target_tl_trip_completed_count)/sum(target_tl_seen_vehicle_count)",
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
    if args.tripinfo_drain_seconds < 0:
        raise ValueError("--tripinfo-drain-seconds must be >= 0")
    normalized_queue_thresholds(args)
    if args.gui_delay_ms is not None and args.gui_delay_ms < 0:
        raise ValueError("--gui-delay-ms must be >= 0")
    if args.traci_start_retries < 1:
        raise ValueError("--traci-start-retries must be >= 1")
    if args.directional_control_min_delta_sec < 0:
        raise ValueError("--directional-control-min-delta-sec must be >= 0")
    if args.directional_control_saturation_gap < 0:
        raise ValueError("--directional-control-saturation-gap must be >= 0")
    if args.directional_control_green_tolerance_sec < 0:
        raise ValueError("--directional-control-green-tolerance-sec must be >= 0")
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
    if args.prompt_format not in {"deepsignal", "deepsignal_json"}:
        raise ValueError(
            "--input-mode github_official requires --prompt-format deepsignal or deepsignal_json"
        )
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
    parser.add_argument("--prompt-format", choices=["native", "deepsignal", "deepsignal_json"], default="native")
    parser.add_argument("--prefill", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--gguf-path", type=Path, default=DEFAULT_GGUF)
    parser.add_argument("--llama-server", type=Path, default=Path("/opt/homebrew/bin/llama-server"))
    parser.add_argument("--sumo-home", type=Path, default=DEFAULT_SUMO_HOME)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--warmup-seconds", type=int, default=300)
    parser.add_argument("--metric-seconds", type=int, default=1200)
    parser.add_argument("--simulation-seconds", type=int, default=None)
    parser.add_argument("--decision-interval-seconds", type=int, default=60)
    parser.add_argument(
        "--action-delay-cycles",
        type=int,
        choices=[0, 1],
        default=0,
        help=(
            "Number of decision cycles between model generation and signal application. "
            "0 preserves the legacy immediate controller; 1 queues each generated "
            "plan and applies it at the next decision point."
        ),
    )
    parser.add_argument("--min-green", type=int, default=10)
    parser.add_argument("--max-green", type=int, default=90)
    parser.add_argument("--capacity-per-lane", type=int, default=30)
    parser.add_argument("--phase-queue-mode", choices=["raw", "split-overlap"], default="raw")
    parser.add_argument("--queue-threshold", type=float, default=10.0)
    parser.add_argument(
        "--queue-thresholds",
        type=float,
        nargs="+",
        default=None,
        help=(
            "Queue thresholds to record in parallel. The legacy queue_over_threshold "
            "fields continue to use --queue-threshold."
        ),
    )
    parser.add_argument(
        "--demand-scale",
        type=float,
        default=1.0,
        help=(
            "Uniform route demand multiplier. flow period is divided by this "
            "factor, and explicit vehicle/trip demand is duplicated "
            "deterministically for factors above 1.0."
        ),
    )
    parser.add_argument(
        "--target-peak-tl-id",
        action="append",
        default=[],
        help=(
            "Traffic light id to receive additional synthetic peak flows. May be repeated. "
            "Flows use controlled from->to connections in the SUMO net."
        ),
    )
    parser.add_argument(
        "--target-peak-vph-per-route",
        type=float,
        default=0.0,
        help="Synthetic peak vehicles/hour per generated from->to route before demand scaling.",
    )
    parser.add_argument(
        "--target-peak-routes-per-tl",
        type=int,
        default=8,
        help="Maximum synthetic from->to routes generated per target TL. 0 means all.",
    )
    parser.add_argument("--target-peak-begin", type=float, default=0.0)
    parser.add_argument("--target-peak-end", type=float, default=None)
    parser.add_argument(
        "--model-fail-policy",
        choices=["keep_default", "min_green", "first_min_green", "random_valid"],
        default="keep_default",
        help=(
            "Policy when a model call is not control-usable. keep_default leaves "
            "the current SUMO signal program untouched; min_green applies a legal "
            "minimum-green plan; first_min_green and random_valid are harsher "
            "stress policies for exposing invalid model outputs."
        ),
    )
    parser.add_argument(
        "--online-control-mode",
        choices=[
            "strict",
            "directional",
            "relaxed",
            "relaxed_directional",
            "repaired",
            "repaired_directional",
        ],
        default="strict",
        help=(
            "Which parsed model output is eligible for actual online control. "
            "strict requires the full benchmark protocol; directional also requires "
            "a non-trivial, saturation-aligned control decision; relaxed accepts a "
            "valid JSON plan without reasoning/SOLUTION tags; relaxed_directional "
            "adds the directional gate to that parsed plan; repaired additionally "
            "allows safe schema repair, reordering, integer coercion, and clipping to min/max; "
            "repaired_directional adds the directional gate after those repairs."
        ),
    )
    parser.add_argument(
        "--json-stop-after-first",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "For JSON-only prompts, evaluate strict parsing against the first complete "
            "JSON object/array embedded in the model response. This measures whether "
            "the decision payload itself is usable when the model repeats or appends text."
        ),
    )
    parser.add_argument(
        "--directional-control-min-delta-sec",
        type=float,
        default=5.0,
        help=(
            "Minimum absolute change from default timing required for "
            "directional_control_usable. This also gates online control when "
            "--online-control-mode is directional, relaxed_directional, or repaired_directional."
        ),
    )
    parser.add_argument(
        "--directional-control-saturation-gap",
        type=float,
        default=0.05,
        help=(
            "Minimum pred_saturation gap used when checking whether higher "
            "saturation phases receive at least comparable green time."
        ),
    )
    parser.add_argument(
        "--directional-control-green-tolerance-sec",
        type=float,
        default=5.0,
        help=(
            "Allowed green-time slack before a higher-saturation phase is counted "
            "as receiving less green than a lower-saturation phase."
        ),
    )
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
    parser.add_argument(
        "--hf-use-chat-template",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Render HF prompts with tokenizer.apply_chat_template when the tokenizer provides one.",
    )
    parser.add_argument(
        "--hf-chat-template-enable-thinking",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Pass enable_thinking to tokenizer.apply_chat_template when supported.",
    )
    parser.add_argument(
        "--hf-chat-template-message-mode",
        choices=["split_system_user", "single_user"],
        default="split_system_user",
        help=(
            "How to map DeepSignal's system/user sentinel into HF chat messages. "
            "single_user is useful for templates that do not accept a system role."
        ),
    )
    parser.add_argument(
        "--hf-skip-special-tokens",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip tokenizer special tokens such as <|im_end|> when decoding HF generations.",
    )
    parser.add_argument("--tripinfo-metrics", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--tripinfo-drain-seconds",
        type=int,
        default=0,
        help=(
            "Extra post-metric SUMO seconds used only to let vehicles complete "
            "tripinfo for ATT/AWT. Model control and queue/delay metrics stop at "
            "the normal metric window."
        ),
    )
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
            "tripinfo_drain_seconds": args.tripinfo_drain_seconds if args.tripinfo_metrics else 0,
            "official_window_enforced": args.input_mode == "github_official",
        },
        "queue_thresholds_effective": normalized_queue_thresholds(args),
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
                            "demand_scale": row["demand_scale"],
                            "action_delay_cycles": row["action_delay_cycles"],
                            "plans_queued": row["plans_queued"],
                            "delayed_plans_applied": row["delayed_plans_applied"],
                            "pending_plan_left_unapplied": row["pending_plan_left_unapplied"],
                            "plans_applied": row["plans_applied"],
                            "plans_applied_rate": row["plans_applied_rate"],
                            "fallback_plans_applied": row["fallback_plans_applied"],
                            "fallback_plan_rate": row["fallback_plan_rate"],
                            "steps_completed": row["steps_completed"],
                            "format_success_rate": row["format_success_rate"],
                            "control_usable_rate": row["control_usable_rate"],
                            "strict_format_success_rate": row["strict_format_success_rate"],
                            "strict_control_usable_rate": row["strict_control_usable_rate"],
                            "directional_control_usable_rate": row["directional_control_usable_rate"],
                            "relaxed_json_success_rate": row["relaxed_json_success_rate"],
                            "relaxed_control_usable_rate": row["relaxed_control_usable_rate"],
                            "relaxed_directional_control_usable_rate": row[
                                "relaxed_directional_control_usable_rate"
                            ],
                            "repaired_control_usable_rate": row["repaired_control_usable_rate"],
                            "repaired_directional_control_usable_rate": row[
                                "repaired_directional_control_usable_rate"
                            ],
                            "repair_applied_rate": row["repair_applied_rate"],
                            "online_control_mode": row["online_control_mode"],
                            "avg_queue_vehicles": row["avg_queue_vehicles"],
                            "p95_queue_vehicles": row["p95_queue_vehicles"],
                            "max_queue_vehicles": row["max_queue_vehicles"],
                            "avg_delay_per_vehicle_sec": row["avg_delay_per_vehicle_sec"],
                            "throughput_veh_per_min": row["throughput_veh_per_min"],
                            "local_delay_per_intersection_minute_sec": row[
                                "local_delay_per_intersection_minute_sec"
                            ],
                            "passage_per_metric_observation": row["passage_per_metric_observation"],
                            "queue_threshold": row["queue_threshold"],
                            "queue_over_threshold_seconds": row["queue_over_threshold_seconds"],
                            "queue_over_threshold_fraction": row["queue_over_threshold_fraction"],
                            "max_continuous_queue_over_threshold_seconds": row[
                                "max_continuous_queue_over_threshold_seconds"
                            ],
                            "queue_over_threshold_seconds_by_threshold": row[
                                "queue_over_threshold_seconds_by_threshold"
                            ],
                            "max_continuous_queue_over_threshold_seconds_by_threshold": row[
                                "max_continuous_queue_over_threshold_seconds_by_threshold"
                            ],
                            "network_metric_departed_vehicle_count": row[
                                "network_metric_departed_vehicle_count"
                            ],
                            "network_trip_completed_count": row["network_trip_completed_count"],
                            "network_trip_completion_ratio": row["network_trip_completion_ratio"],
                            "network_att_sec": row["network_att_sec"],
                            "network_awt_sec": row["network_awt_sec"],
                            "target_tl_seen_vehicle_count": row["target_tl_seen_vehicle_count"],
                            "target_tl_trip_completed_count": row["target_tl_trip_completed_count"],
                            "target_tl_trip_completion_ratio": row[
                                "target_tl_trip_completion_ratio"
                            ],
                            "target_tl_att_sec": row["target_tl_att_sec"],
                            "target_tl_awt_sec": row["target_tl_awt_sec"],
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
