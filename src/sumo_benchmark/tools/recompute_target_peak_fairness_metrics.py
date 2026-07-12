#!/usr/bin/env python3
"""Recompute target-peak demand fairness metrics from SUMO tripinfo files.

The older README-alignment ATT/AWT tables use completed vehicles only.  This
script keeps those completed-trip means, but also exposes the planned demand
denominator and effective metrics that include SUMO departDelay.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import statistics
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_MODEL_LABELS = {
    "01_9b_adapter_temp02_unbalanced_x1p2": "Fine-tuned 9B",
    "06_9b_adapter_3500_3ep_temp02_unbalanced_x1p2": "Fine-tuned 9B 3500x3ep",
    "09_max_pressure_temp02_unbalanced_x1p2": "Max pressure",
    "02_qwen35_9b_base_nochat_repaired_deepsignal_unbalanced_temp02_x1p2": "Qwen3.5-9B",
    "03_model_fp16_20260519_unbalanced_temp02_x1p2": "DeepSignal-20260519 F16",
    "04_qwen3_4b_base_nochat_repaired_deepsignal_unbalanced_temp02_x1p2": "Qwen3-4B",
    "05_gemma3_12b_it_nochat_repaired_deepsignal_unbalanced_temp02_x1p2": "Gemma3-12B-IT",
}

DEFAULT_MODEL_ORDER = [
    "Fine-tuned 9B",
    "Fine-tuned 9B 3500x3ep",
    "Max pressure",
    "DeepSignal-20260519 F16",
    "Qwen3.5-9B",
    "Gemma3-12B-IT",
    "Qwen3-4B",
]


def mean_or_none(values: list[float | None]) -> float | None:
    nums = [value for value in values if value is not None]
    return sum(nums) / len(nums) if nums else None


def float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pct(value: float | None) -> float | None:
    return 100.0 * value if value is not None else None


def parse_windows(raw: str) -> list[tuple[float, float, str]]:
    windows: list[tuple[float, float, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"window must be START:END, got {item!r}")
        start_s, end_s = item.split(":", 1)
        start = float(start_s)
        end = float(end_s)
        if end <= start:
            raise ValueError(f"window end must be greater than start: {item!r}")
        windows.append((start, end, f"{int(start)}_{int(end)}"))
    if not windows:
        raise ValueError("at least one window is required")
    return windows


def infer_source_group(root: Path) -> str:
    name = root.name
    if "ft_maxpressure" in name:
        return "20260701_ft_maxpressure"
    if "readme_models" in name:
        return "20260702_readme_rerun_models"
    return name


def discover_case_dirs(roots: list[Path]) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for root in roots:
        source_group = infer_source_group(root)
        if (root / "runtime_sumo").exists():
            out.append((source_group, root))
            continue
        for child in sorted(root.iterdir() if root.exists() else []):
            if child.is_dir() and (child / "runtime_sumo").exists():
                out.append((source_group, child))
    return out


def parse_step_metrics(path: Path | None) -> dict[tuple[str, str, str], dict[str, str]]:
    if path is None or not path.exists():
        return {}
    out: dict[tuple[str, str, str], dict[str, str]] = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            window = (row.get("window_label") or "").replace("metric_", "")
            case = row.get("case_name") or ""
            tl_id = row.get("tl_id") or ""
            if case and window and tl_id:
                out[(case, window, tl_id)] = row
    return out


def target_peak_route_file(case_dir: Path) -> Path | None:
    candidates = list(case_dir.glob("runtime_sumo/**/target_peak*.rou.xml"))
    return candidates[0] if candidates else None


def discover_tl_ids(route_file: Path) -> list[str]:
    out: list[str] = []
    root = ET.parse(route_file).getroot()
    for flow in root.iter("flow"):
        flow_id = flow.attrib.get("id", "")
        if not flow_id.startswith("target_peak_"):
            continue
        # target_peak_<tl_id>_<route_index>_flow
        tail = flow_id.removeprefix("target_peak_")
        if "_" not in tail:
            continue
        tl_id = tail.rsplit("_", 2)[0]
        if tl_id not in out:
            out.append(tl_id)
    return out


def parse_flows(route_file: Path, tl_id: str) -> list[tuple[str, list[tuple[str, float]]]]:
    root = ET.parse(route_file).getroot()
    flows: list[tuple[str, list[tuple[str, float]]]] = []
    for flow in root.iter("flow"):
        flow_id = flow.attrib.get("id", "")
        route = flow.attrib.get("route", "")
        if not (flow_id.startswith(f"target_peak_{tl_id}_") or route.startswith(f"target_peak_{tl_id}_")):
            continue
        begin = float(flow.attrib.get("begin", "0"))
        end = float(flow.attrib.get("end", "0"))
        vph = float(flow.attrib.get("vehsPerHour", "0"))
        if vph <= 0:
            continue
        period = 3600.0 / vph
        count = max(0, int(math.ceil((end - begin) / period - 1e-9)))
        entries: list[tuple[str, float]] = []
        for idx in range(count):
            planned = begin + idx * period
            if planned < end - 1e-9:
                entries.append((f"{flow_id}.{idx}", planned))
        flows.append((flow_id, entries))
    return flows


def scheduled_entries(
    flows: list[tuple[str, list[tuple[str, float]]]],
    start: float,
    end: float,
) -> list[tuple[str, float]]:
    entries: list[tuple[str, float]] = []
    for _flow_id, flow_entries in flows:
        for vehicle_id, planned in flow_entries:
            if start <= planned < end:
                entries.append((vehicle_id, planned))
    return entries


def parse_tripinfo(path: Path, tl_id: str) -> dict[str, dict[str, float]]:
    prefix = f"target_peak_{tl_id}_"
    trips: dict[str, dict[str, float]] = {}
    if not path.exists():
        return trips
    for _event, elem in ET.iterparse(path, events=("end",)):
        if elem.tag != "tripinfo":
            elem.clear()
            continue
        vehicle_id = elem.attrib.get("id", "")
        if not vehicle_id.startswith(prefix):
            elem.clear()
            continue
        try:
            depart = float(elem.attrib.get("depart", "nan"))
            depart_delay = float(elem.attrib.get("departDelay", "0"))
            arrival = float(elem.attrib.get("arrival", "-1"))
            duration = float(elem.attrib.get("duration", "-1"))
            waiting = float(elem.attrib.get("waitingTime", "0"))
            time_loss = float(elem.attrib.get("timeLoss", "0"))
        except ValueError:
            elem.clear()
            continue
        completed = bool(arrival >= 0.0 and duration >= 0.0)
        trips[vehicle_id] = {
            "planned": depart - depart_delay if depart >= 0.0 else float("nan"),
            "completed": float(completed),
            "departed": float(depart >= 0.0),
            "depart": depart,
            "departDelay": depart_delay,
            "arrival": arrival,
            "duration": duration,
            "waitingTime": waiting,
            "timeLoss": time_loss,
            "effective_awt": depart_delay + waiting if completed else float("nan"),
            "effective_att": depart_delay + duration if completed else float("nan"),
            "effective_delay": depart_delay + time_loss if completed else float("nan"),
        }
        elem.clear()
    return trips


def build_per_tl_rows(
    case_dirs: list[tuple[str, Path]],
    windows: list[tuple[float, float, str]],
    step_metrics: dict[tuple[str, str, str], dict[str, str]],
    sim_end: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source_group, case_dir in case_dirs:
        route_file = target_peak_route_file(case_dir)
        if route_file is None:
            continue
        model = DEFAULT_MODEL_LABELS.get(case_dir.name, case_dir.name)
        for tl_id in discover_tl_ids(route_file):
            flows = parse_flows(route_file, tl_id)
            tripinfo = case_dir / "sumo_outputs" / "tripinfo" / f"sumo_llm__{tl_id}.tripinfo.xml"
            trips = parse_tripinfo(tripinfo, tl_id)
            for start, end, label in windows:
                scheduled = scheduled_entries(flows, start, end)
                scheduled_ids = [vehicle_id for vehicle_id, _planned in scheduled]
                completed = [
                    trips[vehicle_id]
                    for vehicle_id in scheduled_ids
                    if vehicle_id in trips and bool(trips[vehicle_id].get("completed"))
                ]
                departed_by_end = [
                    trips[vehicle_id]
                    for vehicle_id in scheduled_ids
                    if vehicle_id in trips
                    and bool(trips[vehicle_id].get("completed"))
                    and trips[vehicle_id]["depart"] >= 0.0
                    and trips[vehicle_id]["depart"] < end
                ]
                arrived_by_end = [
                    trips[vehicle_id]
                    for vehicle_id in scheduled_ids
                    if vehicle_id in trips
                    and bool(trips[vehicle_id].get("completed"))
                    and trips[vehicle_id]["arrival"] >= 0.0
                    and trips[vehicle_id]["arrival"] < end
                ]
                departed_in_window = [
                    trip
                    for trip in trips.values()
                    if bool(trip.get("completed")) and start <= trip["depart"] < end
                ]

                censored_att_values: list[float] = []
                censored_awt_values: list[float] = []
                missing_penalty_values: list[float] = []
                for vehicle_id, planned in scheduled:
                    trip = trips.get(vehicle_id)
                    if trip is not None and bool(trip.get("completed")):
                        censored_att_values.append(trip["effective_att"])
                        censored_awt_values.append(trip["effective_awt"])
                    else:
                        penalty = max(0.0, sim_end - planned)
                        censored_att_values.append(penalty)
                        censored_awt_values.append(penalty)
                        missing_penalty_values.append(penalty)

                step = step_metrics.get((case_dir.name, label, tl_id), {})
                metric_minutes = (end - start) / 60.0
                scheduled_count = len(scheduled)
                completed_count = len(completed)
                rows.append(
                    {
                        "source_group": source_group,
                        "model": model,
                        "case": case_dir.name,
                        "window": label,
                        "tl_id": tl_id,
                        "scheduled_target_peak_veh": scheduled_count,
                        "completed_by_sim_end_veh": completed_count,
                        "departed_by_window_end_completed_veh": len(departed_by_end),
                        "arrived_by_window_end_veh": len(arrived_by_end),
                        "departed_in_window_completed_veh_actual_depart": len(departed_in_window),
                        "missing_or_unfinished_by_sim_end_veh": scheduled_count - completed_count,
                        "completed_by_sim_end_rate_pct": pct(completed_count / scheduled_count if scheduled_count else None),
                        "arrived_by_window_end_rate_pct": pct(len(arrived_by_end) / scheduled_count if scheduled_count else None),
                        "mean_depart_delay_s_completed": mean_or_none([trip["departDelay"] for trip in completed]),
                        "mean_waiting_time_s_completed": mean_or_none([trip["waitingTime"] for trip in completed]),
                        "mean_duration_s_completed": mean_or_none([trip["duration"] for trip in completed]),
                        "mean_time_loss_s_completed": mean_or_none([trip["timeLoss"] for trip in completed]),
                        "effective_awt_s_completed": mean_or_none([trip["effective_awt"] for trip in completed]),
                        "effective_att_s_completed": mean_or_none([trip["effective_att"] for trip in completed]),
                        "effective_delay_s_completed": mean_or_none([trip["effective_delay"] for trip in completed]),
                        "censored_effective_awt_s_demand_lower_bound": mean_or_none(censored_awt_values),
                        "censored_effective_att_s_demand_lower_bound": mean_or_none(censored_att_values),
                        "missing_penalty_mean_s": mean_or_none(missing_penalty_values),
                        "target_arrival_throughput_veh_per_min": len(arrived_by_end) / metric_minutes,
                        "local_throughput_veh_per_min": float_or_none(step.get("throughput_veh_per_min")),
                        "avg_queue_vehicles": float_or_none(step.get("avg_queue_vehicles")),
                        "avg_delay_per_vehicle_sec_local": float_or_none(step.get("avg_delay_per_vehicle_sec")),
                        "metric_basis": (
                            "target_peak planned-departure window; SUMO tripinfo completed trips; "
                            "effective metrics add departDelay; censored lower bound assigns "
                            "sim_end-planned_depart to missing/unfinished vehicles"
                        ),
                    }
                )
    order = {model: idx for idx, model in enumerate(DEFAULT_MODEL_ORDER)}
    rows.sort(key=lambda row: (row["window"], order.get(row["model"], 99), row["tl_id"]))
    return rows


def aggregate_rows(rows: list[dict[str, Any]], included_tls: list[str]) -> list[dict[str, Any]]:
    filtered = [row for row in rows if row["tl_id"] in included_tls]
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in filtered:
        groups[(row["source_group"], row["model"], row["case"], row["window"])].append(row)

    completed_metric_cols = [
        "mean_depart_delay_s_completed",
        "mean_waiting_time_s_completed",
        "mean_duration_s_completed",
        "mean_time_loss_s_completed",
        "effective_awt_s_completed",
        "effective_att_s_completed",
        "effective_delay_s_completed",
    ]
    out: list[dict[str, Any]] = []
    for (source_group, model, case, window), group_rows in groups.items():
        scheduled = sum(int(row["scheduled_target_peak_veh"]) for row in group_rows)
        completed = sum(int(row["completed_by_sim_end_veh"]) for row in group_rows)
        arrived = sum(int(row["arrived_by_window_end_veh"]) for row in group_rows)
        missing = sum(int(row["missing_or_unfinished_by_sim_end_veh"]) for row in group_rows)
        queue_values = [row["avg_queue_vehicles"] for row in group_rows if row["avg_queue_vehicles"] is not None]
        completion_rates = [
            row["completed_by_sim_end_rate_pct"]
            for row in group_rows
            if row["completed_by_sim_end_rate_pct"] is not None
        ]
        aggregate: dict[str, Any] = {
            "source_group": source_group,
            "model": model,
            "case": case,
            "window": window,
            "included_tls": ",".join(included_tls),
            "scheduled_target_peak_veh": scheduled,
            "completed_by_sim_end_veh": completed,
            "arrived_by_window_end_veh": arrived,
            "missing_or_unfinished_by_sim_end_veh": missing,
            "completed_by_sim_end_rate_pct": pct(completed / scheduled if scheduled else None),
            "arrived_by_window_end_rate_pct": pct(arrived / scheduled if scheduled else None),
            "target_arrival_throughput_veh_per_min": sum(
                float(row["target_arrival_throughput_veh_per_min"]) for row in group_rows
            ),
            "local_throughput_veh_per_min_mean": mean_or_none(
                [row["local_throughput_veh_per_min"] for row in group_rows]
            ),
            "avg_queue_vehicles_mean": mean_or_none([row["avg_queue_vehicles"] for row in group_rows]),
            "avg_delay_per_vehicle_sec_local_mean": mean_or_none(
                [row["avg_delay_per_vehicle_sec_local"] for row in group_rows]
            ),
            "avg_queue_vehicles_std_across_tls": (
                statistics.pstdev(queue_values) if len(queue_values) > 1 else None
            ),
            "completed_rate_std_across_tls_pct": (
                statistics.pstdev(completion_rates) if len(completion_rates) > 1 else None
            ),
            "metric_basis": (
                "aggregate of target_peak planned-window demand metrics; completed means are "
                "vehicle-weighted by completed target trips; censored means are scheduled-demand-weighted"
            ),
        }
        for col in completed_metric_cols:
            numerator = sum(
                (float(row[col]) if row[col] is not None else 0.0) * int(row["completed_by_sim_end_veh"])
                for row in group_rows
            )
            aggregate[f"{col}_vehicle_weighted"] = numerator / completed if completed else None
        for col in [
            "censored_effective_awt_s_demand_lower_bound",
            "censored_effective_att_s_demand_lower_bound",
        ]:
            numerator = sum(
                (float(row[col]) if row[col] is not None else 0.0) * int(row["scheduled_target_peak_veh"])
                for row in group_rows
            )
            aggregate[col] = numerator / scheduled if scheduled else None
        out.append(aggregate)

    order = {model: idx for idx, model in enumerate(DEFAULT_MODEL_ORDER)}
    out.sort(key=lambda row: (row["window"], order.get(row["model"], 99)))
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("roots", nargs="+", type=Path, help="Experiment roots or case directories.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--windows", default="300:900,300:1500")
    parser.add_argument("--sim-end", type=float, default=2100.0)
    parser.add_argument("--window-metrics", type=Path, default=None)
    parser.add_argument(
        "--aggregate-tls",
        action="append",
        default=[],
        help="Comma-separated TLS IDs for an aggregate CSV. Can be passed multiple times.",
    )
    args = parser.parse_args()

    windows = parse_windows(args.windows)
    case_dirs = discover_case_dirs(args.roots)
    step_metrics = parse_step_metrics(args.window_metrics)
    per_tl_rows = build_per_tl_rows(case_dirs, windows, step_metrics, args.sim_end)

    write_csv(args.output_dir / "target_peak_fairness_per_tl_planned_window.csv", per_tl_rows)
    for raw_group in args.aggregate_tls:
        included_tls = [item.strip() for item in raw_group.split(",") if item.strip()]
        if not included_tls:
            continue
        suffix = "_".join(included_tls)
        if len(suffix) > 120:
            digest = hashlib.sha1(suffix.encode("utf-8")).hexdigest()[:12]
            suffix = f"{len(included_tls)}tls_{digest}"
        write_csv(
            args.output_dir / f"target_peak_fairness_aggregate_{suffix}.csv",
            aggregate_rows(per_tl_rows, included_tls),
        )


if __name__ == "__main__":
    main()
