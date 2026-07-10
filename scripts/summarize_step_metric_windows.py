#!/usr/bin/env python3
"""Recompute Chengdu benchmark windows from step_metrics.jsonl.

The runner keeps its historical aggregate summary unchanged. This script reads
the per-second metric log and slices the same run into comparable windows, for
example 300-900s and 300-1500s.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import deepsignal_cycleplan_benchmark_chengdu_metrics as runner  # noqa: E402


DEFAULT_WINDOWS = ("300:900:metric_300_900", "300:1500:metric_300_1500")
TRIPINFO_CACHE: dict[Path, dict[str, tuple[float, float, float]]] = {}


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def parse_window_spec(text: str) -> tuple[int, int, str]:
    parts = text.split(":")
    if len(parts) == 2:
        start, end = int(parts[0]), int(parts[1])
        label = f"metric_{start}_{end}"
    elif len(parts) == 3:
        start, end, label = int(parts[0]), int(parts[1]), parts[2]
    else:
        raise ValueError(f"bad window spec {text!r}; expected start:end[:label]")
    if end <= start:
        raise ValueError(f"bad window spec {text!r}; end must be greater than start")
    return start, end, label


def find_case_dirs(roots: list[Path]) -> list[Path]:
    case_dirs: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        root = root.resolve()
        candidates = [root] if (root / "step_metrics.jsonl").exists() else [
            path.parent for path in root.rglob("step_metrics.jsonl")
        ]
        for case_dir in candidates:
            if case_dir in seen:
                continue
            seen.add(case_dir)
            case_dirs.append(case_dir)
    return sorted(case_dirs)


def group_step_rows(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            str(row.get("scenario", "")),
            str(row.get("usage", "")),
            str(row.get("tl_id", "")),
        )
        grouped.setdefault(key, []).append(row)
    for items in grouped.values():
        items.sort(key=lambda item: int(item.get("step", 0)))
    return grouped


def per_tl_source_map(case_dir: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in read_jsonl(case_dir / "per_tl.jsonl"):
        key = (
            str(row.get("scenario", "")),
            str(row.get("usage", "")),
            str(row.get("tl_id", "")),
        )
        out[key] = row
    return out


def case_thresholds(config: dict[str, Any], rows: list[dict[str, Any]]) -> list[float]:
    raw = config.get("queue_thresholds_effective") or config.get("queue_thresholds")
    if raw:
        return sorted({float(value) for value in raw})
    for row in rows:
        flags = row.get("queue_over_threshold")
        if isinstance(flags, dict) and flags:
            # Metric keys are lossy for arbitrary decimals, so only use them as
            # a presence hint and fall back to the standard dashboard thresholds.
            return [10.0, 20.0, 30.0, 40.0]
    return [10.0, 20.0, 30.0, 40.0]


def primary_threshold(config: dict[str, Any], thresholds: list[float]) -> float:
    value = config.get("queue_threshold")
    if value is not None:
        return float(value)
    return thresholds[0]


def extend_numeric(rows: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        raw = row.get(key)
        if isinstance(raw, list):
            values.extend(float(value) for value in raw if value is not None)
    return values


def continuous_seconds(values: list[float | None], threshold: float) -> int:
    longest = 0
    current = 0
    for value in values:
        if value is not None and value > threshold:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def queue_length_samples(rows: list[dict[str, Any]]) -> list[float]:
    samples: list[float] = []
    for row in rows:
        value = row.get("incoming_vehicle_count")
        if value is None:
            value = len(row.get("incoming_vehicle_ids", []) or [])
        samples.append(float(value or 0.0))
    return samples


def tripinfo_path_for(case_dir: Path, source_row: dict[str, Any], scenario: str, tl_id: str) -> Path | None:
    raw = source_row.get("tripinfo_path")
    if raw:
        path = Path(str(raw))
        if path.exists():
            return path
    fallback = case_dir / "sumo_outputs" / "tripinfo" / f"{runner.safe_path_token(scenario)}__{runner.safe_path_token(tl_id)}.tripinfo.xml"
    return fallback if fallback.exists() else (Path(str(raw)) if raw else fallback)


def cached_tripinfo_records(path: Path) -> dict[str, tuple[float, float, float]]:
    resolved = path.resolve()
    if resolved not in TRIPINFO_CACHE:
        TRIPINFO_CACHE[resolved] = {
            veh_id: (duration, waiting_time, time_loss)
            for veh_id, duration, waiting_time, time_loss in runner.iter_tripinfo_metrics(resolved)
        }
    return TRIPINFO_CACHE[resolved]


def tripinfo_metrics_for_sets(
    tripinfo_path: Path | None,
    network_metric_departed_vehicle_ids: set[str],
    target_tl_seen_vehicle_ids: set[str],
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
        "network_travel_time_delay_total_sec": 0.0,
        "network_att_sec": None,
        "network_awt_sec": None,
        "network_travel_time_delay_sec": None,
        "target_tl_seen_vehicle_count": len(target_tl_seen_vehicle_ids),
        "target_tl_trip_completed_count": 0,
        "target_tl_trip_completion_ratio": None,
        "target_tl_travel_time_total_sec": 0.0,
        "target_tl_waiting_time_total_sec": 0.0,
        "target_tl_travel_time_delay_total_sec": 0.0,
        "target_tl_att_sec": None,
        "target_tl_awt_sec": None,
        "target_tl_travel_time_delay_sec": None,
    }
    if tripinfo_path is None:
        return base
    if not tripinfo_path.exists():
        base["tripinfo_parse_error"] = "tripinfo_missing"
        return base

    try:
        records = cached_tripinfo_records(tripinfo_path)
    except Exception as exc:
        base["tripinfo_parse_error"] = f"{type(exc).__name__}: {exc}"
        return base

    network_rows = [records[veh_id] for veh_id in network_metric_departed_vehicle_ids if veh_id in records]
    target_rows = [records[veh_id] for veh_id in target_tl_seen_vehicle_ids if veh_id in records]

    def sums(rows: list[tuple[float, float, float]]) -> tuple[float, float, float]:
        return (
            float(sum(item[0] for item in rows)),
            float(sum(item[1] for item in rows)),
            float(sum(item[2] for item in rows)),
        )

    network_duration, network_waiting, network_delay = sums(network_rows)
    target_duration, target_waiting, target_delay = sums(target_rows)
    network_departed = len(network_metric_departed_vehicle_ids)
    target_seen = len(target_tl_seen_vehicle_ids)
    base.update(
        {
            "network_trip_completed_count": len(network_rows),
            "network_trip_completion_ratio": (
                len(network_rows) / network_departed if network_departed else None
            ),
            "network_travel_time_total_sec": network_duration,
            "network_waiting_time_total_sec": network_waiting,
            "network_travel_time_delay_total_sec": network_delay,
            "network_att_sec": network_duration / len(network_rows) if network_rows else None,
            "network_awt_sec": network_waiting / len(network_rows) if network_rows else None,
            "network_travel_time_delay_sec": network_delay / len(network_rows) if network_rows else None,
            "target_tl_trip_completed_count": len(target_rows),
            "target_tl_trip_completion_ratio": (
                len(target_rows) / target_seen if target_seen else None
            ),
            "target_tl_travel_time_total_sec": target_duration,
            "target_tl_waiting_time_total_sec": target_waiting,
            "target_tl_travel_time_delay_total_sec": target_delay,
            "target_tl_att_sec": target_duration / len(target_rows) if target_rows else None,
            "target_tl_awt_sec": target_waiting / len(target_rows) if target_rows else None,
            "target_tl_travel_time_delay_sec": target_delay / len(target_rows) if target_rows else None,
        }
    )
    return base


def metric_row_for_window(
    *,
    case_dir: Path,
    config: dict[str, Any],
    source_row: dict[str, Any],
    key: tuple[str, str, str],
    all_rows: list[dict[str, Any]],
    window_start: int,
    window_end: int,
    window_label: str,
) -> dict[str, Any]:
    scenario, usage, tl_id = key
    rows = [
        row
        for row in all_rows
        if window_start <= int(row.get("step", -1)) < window_end
    ]
    thresholds = case_thresholds(config, all_rows)
    threshold = primary_threshold(config, thresholds)
    metric_seconds = window_end - window_start
    metric_minutes = max(1.0 / 60.0, metric_seconds / 60.0)

    raw_samples = extend_numeric(rows, "phase_queues_raw")
    split_samples = extend_numeric(rows, "phase_queues_split_overlap")
    selected_samples = extend_numeric(rows, "selected_phase_queues")
    queue_length = queue_length_samples(rows)
    if not selected_samples:
        phase_queue_mode = str(source_row.get("phase_queue_mode") or config.get("phase_queue_mode") or "raw")
        selected_samples = split_samples if phase_queue_mode == "split-overlap" else raw_samples
    else:
        phase_queue_mode = str(rows[0].get("phase_queue_mode") or source_row.get("phase_queue_mode") or "raw")

    step_max_values = [
        float(row["max_queue_selected"]) if row.get("max_queue_selected") is not None else None
        for row in rows
    ]
    incoming_observations = sum(int(row.get("incoming_vehicle_count") or 0) for row in rows)
    passage_count = sum(int(row.get("passage_count") or 0) for row in rows)
    local_delay_total = sum(float(row.get("local_delay_delta_s") or 0.0) for row in rows)
    network_ids: set[str] = set()
    target_ids: set[str] = set()
    for row in rows:
        network_ids.update(str(value) for value in row.get("network_departed_vehicle_ids", []) or [])
        target_ids.update(str(value) for value in row.get("incoming_vehicle_ids", []) or [])

    queue_over_by_key: dict[str, float] = {}
    queue_fraction_by_key: dict[str, float | None] = {}
    max_continuous_by_key: dict[str, float] = {}
    for item in thresholds:
        suffix = runner.metric_key_float(item)
        seconds = sum(1 for value in step_max_values if value is not None and value > item)
        queue_over_by_key[suffix] = float(seconds)
        queue_fraction_by_key[suffix] = seconds / max(1, metric_seconds) if rows else None
        max_continuous_by_key[suffix] = float(continuous_seconds(step_max_values, item))

    metric_active = bool(incoming_observations > 0 or passage_count > 0 or any(value > 0 for value in raw_samples))
    tripinfo_path = tripinfo_path_for(case_dir, source_row, scenario, tl_id)
    drain_seconds = int(config.get("tripinfo_drain_seconds") or config.get("metric_window", {}).get("tripinfo_drain_seconds") or 0)
    total_steps = int(config.get("simulation_seconds") or config.get("metric_window", {}).get("metric_end_second") or window_end) + drain_seconds
    tripinfo_metrics = tripinfo_metrics_for_sets(
        tripinfo_path,
        network_ids,
        target_ids,
        total_steps,
        drain_seconds,
    )

    raw_avg_queue = mean(raw_samples) if raw_samples else None
    split_avg_queue = mean(split_samples) if split_samples else None
    selected_avg_queue = mean(selected_samples) if selected_samples else None
    selected_p95_queue = runner.percentile(selected_samples, 0.95)
    selected_max_queue = max(selected_samples) if selected_samples else None
    avg_queue_length = mean(queue_length) if queue_length else None
    p95_queue_length = runner.percentile(queue_length, 0.95)
    max_queue_length = max(queue_length) if queue_length else None
    avg_delay = local_delay_total / passage_count if passage_count else None
    throughput = passage_count / metric_minutes
    primary_suffix = runner.metric_key_float(threshold)

    row: dict[str, Any] = {
        "case_name": case_dir.name,
        "case_dir": str(case_dir),
        "window_label": window_label,
        "window_start_second": window_start,
        "window_end_second": window_end,
        "scenario": scenario,
        "usage": usage,
        "tl_id": tl_id,
        "sumocfg": source_row.get("sumocfg"),
        "controller": source_row.get("controller") or (rows[0].get("controller") if rows else None),
        "input_mode": source_row.get("input_mode") or (rows[0].get("input_mode") if rows else None),
        "model_backend": source_row.get("model_backend"),
        "prompt_format": source_row.get("prompt_format"),
        "model_fail_policy": source_row.get("model_fail_policy"),
        "demand_scale": source_row.get("demand_scale") or (rows[0].get("demand_scale") if rows else config.get("demand_scale")),
        "warmup_seconds": window_start,
        "metric_seconds": metric_seconds,
        "eval_minutes": metric_minutes,
        "phase_queue_mode": phase_queue_mode,
        "step_metric_rows": len(rows),
        "metric_window_steps": metric_seconds,
        "format_success_rate": source_row.get("format_success_rate"),
        "control_usable_rate": source_row.get("control_usable_rate"),
        "strict_format_success_rate": source_row.get("strict_format_success_rate"),
        "strict_control_usable_rate": source_row.get("strict_control_usable_rate"),
        "relaxed_json_success_rate": source_row.get("relaxed_json_success_rate"),
        "relaxed_control_usable_rate": source_row.get("relaxed_control_usable_rate"),
        "repaired_control_usable_rate": source_row.get("repaired_control_usable_rate"),
        "repair_applied_rate": source_row.get("repair_applied_rate"),
        "lint_success_rate": source_row.get("lint_success_rate"),
        "model_calls": source_row.get("model_calls"),
        "decision_count": source_row.get("decision_count"),
        "plans_applied_rate": source_row.get("plans_applied_rate"),
        "fallback_plan_rate": source_row.get("fallback_plan_rate"),
        "controlled_tls_count": 1,
        "active_tl": metric_active,
        "inactive_reason": None if metric_active else "no_vehicle_observations_in_step_window",
        "metric_vehicle_observations": float(incoming_observations),
        "throughput_total_intersection_passages": float(passage_count),
        "passage_per_metric_observation": passage_count / incoming_observations if incoming_observations else None,
        "passage_seen_ratio_approx": passage_count / incoming_observations if incoming_observations else None,
        "queue_sample_count": len(selected_samples),
        "raw_queue_sample_count": len(raw_samples),
        "split_queue_sample_count": len(split_samples),
        "queue_length_sample_count": len(queue_length),
        "queue_length_scope": "target_intersection_unique_incoming_lanes_vehicle_count",
        "queue_threshold": threshold,
        "queue_thresholds": thresholds,
        "queue_over_threshold_seconds": queue_over_by_key.get(primary_suffix, 0.0),
        "queue_over_threshold_fraction": queue_fraction_by_key.get(primary_suffix),
        "max_continuous_queue_over_threshold_seconds": max_continuous_by_key.get(primary_suffix, 0.0),
        "queue_over_threshold_seconds_by_threshold": queue_over_by_key,
        "queue_over_threshold_fraction_by_threshold": queue_fraction_by_key,
        "max_continuous_queue_over_threshold_seconds_by_threshold": max_continuous_by_key,
        "local_delay_total_s": local_delay_total,
        "local_delay_per_intersection_minute_sec": local_delay_total / metric_minutes if metric_active else None,
        "sum_response_time_s": source_row.get("sum_response_time_s") or 0.0,
        "raw_avg_queue_vehicles": raw_avg_queue,
        "avg_queue_vehicles_raw": raw_avg_queue,
        "avg_queue_vehicles_split_overlap": split_avg_queue,
        "p95_queue_vehicles_raw": runner.percentile(raw_samples, 0.95),
        "p95_queue_vehicles_split_overlap": runner.percentile(split_samples, 0.95),
        "max_queue_vehicles_raw": max(raw_samples) if raw_samples else None,
        "max_queue_vehicles_split_overlap": max(split_samples) if split_samples else None,
        "avg_queue_length_vehicles": avg_queue_length,
        "p95_queue_length_vehicles": p95_queue_length,
        "max_queue_length_vehicles": max_queue_length,
        "raw_avg_delay_per_vehicle_sec": avg_delay,
        "raw_throughput_veh_per_min": throughput,
        "avg_queue_vehicles": selected_avg_queue if metric_active else None,
        "p95_queue_vehicles": selected_p95_queue if metric_active else None,
        "max_queue_vehicles": selected_max_queue if metric_active else None,
        "avg_delay_per_vehicle_sec": avg_delay if metric_active else None,
        "throughput_veh_per_min": throughput if metric_active else None,
        "avg_response_time_sec": source_row.get("avg_response_time_sec"),
        "parse_errors": source_row.get("parse_errors"),
        "source_per_tl_metric_seconds": source_row.get("metric_seconds"),
        **tripinfo_metrics,
    }
    for item in thresholds:
        suffix = runner.metric_key_float(item)
        row[f"queue_over_threshold_seconds_t{suffix}"] = queue_over_by_key[suffix]
        row[f"queue_over_threshold_fraction_t{suffix}"] = queue_fraction_by_key[suffix]
        row[f"max_continuous_queue_over_threshold_seconds_t{suffix}"] = max_continuous_by_key[suffix]
    return row


def flatten_case_summary(case_name: str, case_dir: str, window_label: str, summary: dict[str, Any]) -> dict[str, Any]:
    overall = summary.get("overall", {})
    denom = overall.get("denominators", {})
    row: dict[str, Any] = {
        "case_name": case_name,
        "case_dir": case_dir,
        "window_label": window_label,
        "n_runs": summary.get("n_runs"),
        "n_active_runs": summary.get("n_active_runs"),
        "inactive_run_rate": summary.get("inactive_run_rate"),
    }
    for key in (
        "format_success_rate",
        "control_usable_rate",
        "repaired_control_usable_rate",
        "plans_applied_rate",
        "avg_queue_vehicles",
        "p95_queue_vehicles",
        "max_queue_vehicles",
        "avg_queue_length_vehicles",
        "p95_queue_length_vehicles",
        "max_queue_length_vehicles",
        "avg_delay_per_vehicle_sec",
        "local_delay_per_intersection_minute_sec",
        "throughput_veh_per_min",
        "queue_over_threshold_seconds",
        "queue_over_threshold_fraction",
        "max_continuous_queue_over_threshold_seconds",
        "network_att_sec",
        "network_awt_sec",
        "network_travel_time_delay_sec",
        "network_trip_completion_ratio",
        "target_tl_att_sec",
        "target_tl_awt_sec",
        "target_tl_travel_time_delay_sec",
        "target_tl_trip_completion_ratio",
        "avg_response_time_sec",
    ):
        row[key] = overall.get(key)
        row[f"{key}_median"] = overall.get(f"{key}_median")
    for key, value in denom.items():
        row[f"denominator_{key}"] = value
    return row


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    preferred = [
        "case_name",
        "window_label",
        "scenario",
        "usage",
        "tl_id",
        "controller",
        "input_mode",
        "demand_scale",
        "metric_seconds",
        "step_metric_rows",
        "avg_queue_vehicles",
        "p95_queue_vehicles",
        "max_queue_vehicles",
        "avg_queue_length_vehicles",
        "p95_queue_length_vehicles",
        "max_queue_length_vehicles",
        "avg_delay_per_vehicle_sec",
        "throughput_veh_per_min",
        "local_delay_per_intersection_minute_sec",
        "queue_over_threshold_seconds",
        "queue_over_threshold_fraction",
        "max_continuous_queue_over_threshold_seconds",
        "network_att_sec",
        "network_awt_sec",
        "network_travel_time_delay_sec",
        "network_trip_completion_ratio",
        "target_tl_att_sec",
        "target_tl_awt_sec",
        "target_tl_travel_time_delay_sec",
        "target_tl_trip_completion_ratio",
        "case_dir",
    ]
    all_keys = sorted({key for row in rows for key in row})
    fieldnames = [key for key in preferred if key in all_keys] + [
        key for key in all_keys if key not in preferred
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                    if isinstance(value, (dict, list))
                    else value
                    for key, value in row.items()
                }
            )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path, help="Run root or case directory.")
    parser.add_argument(
        "--window",
        action="append",
        default=None,
        help="Window as start:end[:label]. May be repeated.",
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    windows = [parse_window_spec(item) for item in (args.window or list(DEFAULT_WINDOWS))]
    case_dirs = find_case_dirs(args.roots)
    if not case_dirs:
        raise SystemExit("no case directories with step_metrics.jsonl found")

    output_dir = args.output_dir or (args.roots[0].resolve() / "window_metrics")
    per_tl_rows: list[dict[str, Any]] = []
    case_rows: list[dict[str, Any]] = []
    summary_payload: dict[str, Any] = {
        "windows": [
            {"start": start, "end": end, "label": label}
            for start, end, label in windows
        ],
        "case_dirs": [str(path) for path in case_dirs],
        "cases": {},
    }

    for case_dir in case_dirs:
        config = read_json(case_dir / "config.json")
        step_rows = read_jsonl(case_dir / "step_metrics.jsonl")
        grouped = group_step_rows(step_rows)
        sources = per_tl_source_map(case_dir)
        case_payload: dict[str, Any] = {}
        for window_start, window_end, window_label in windows:
            window_rows: list[dict[str, Any]] = []
            for key, rows in grouped.items():
                source_row = sources.get(key, {})
                row = metric_row_for_window(
                    case_dir=case_dir,
                    config=config,
                    source_row=source_row,
                    key=key,
                    all_rows=rows,
                    window_start=window_start,
                    window_end=window_end,
                    window_label=window_label,
                )
                per_tl_rows.append(row)
                window_rows.append(row)
            summary = runner.summarize(window_rows, [])
            case_payload[window_label] = summary
            case_rows.append(flatten_case_summary(case_dir.name, str(case_dir), window_label, summary))
        summary_payload["cases"][case_dir.name] = case_payload

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "window_metrics_per_tl.csv", per_tl_rows)
    write_csv(output_dir / "window_metrics_by_case.csv", case_rows)
    (output_dir / "window_metrics_summary.json").write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"output_dir": str(output_dir), "case_count": len(case_dirs)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
