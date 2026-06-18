#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path
from statistics import mean, median
from typing import Any


METRICS = [
    "format_success_rate",
    "control_usable_rate",
    "avg_queue_vehicles",
    "p95_queue_vehicles",
    "max_queue_vehicles",
    "avg_delay_per_vehicle_sec",
    "throughput_veh_per_min",
    "queue_over_threshold_seconds",
    "queue_over_threshold_seconds_t10",
    "queue_over_threshold_seconds_t20",
    "queue_over_threshold_seconds_t30",
    "queue_over_threshold_seconds_t40",
    "queue_over_threshold_fraction",
    "max_continuous_queue_over_threshold_seconds",
    "max_continuous_queue_over_threshold_seconds_t10",
    "max_continuous_queue_over_threshold_seconds_t20",
    "max_continuous_queue_over_threshold_seconds_t30",
    "max_continuous_queue_over_threshold_seconds_t40",
    "fallback_plans_applied",
    "avg_response_time_sec",
    "network_att_sec",
    "network_awt_sec",
    "network_trip_completion_ratio",
    "target_tl_att_sec",
    "target_tl_awt_sec",
    "target_tl_trip_completion_ratio",
    "network_metric_departed_vehicle_count",
    "network_trip_completed_count",
    "target_tl_seen_vehicle_count",
    "target_tl_trip_completed_count",
]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def values(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [float(row[key]) for row in rows if row.get(key) is not None]


def label_parts(name: str) -> tuple[str, str]:
    match = re.search(r"_x(\d+p\d+|\d+)$", name)
    scale = match.group(1).replace("p", ".") if match else ""
    group = name[: match.start()] if match else name
    group = re.sub(r"^\d+_", "", group)
    return group, scale


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: summarize_chengdu_peak_matrix.py RUN_ROOT", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).resolve()
    summary_rows: list[dict[str, Any]] = []
    for per_tl in sorted(root.glob("*/per_tl.jsonl")):
        case_dir = per_tl.parent
        rows = load_jsonl(per_tl)
        group, scale = label_parts(case_dir.name)
        summary: dict[str, Any] = {
            "case": case_dir.name,
            "group": group,
            "demand_scale": scale or (rows[0].get("demand_scale") if rows else None),
            "completed_tls": len(rows),
            "failures": len(load_jsonl(case_dir / "failures.jsonl")),
        }
        for key in METRICS:
            vals = values(rows, key)
            summary[f"{key}_mean"] = mean(vals) if vals else None
            summary[f"{key}_median"] = median(vals) if vals else None
        parse_total = 0
        for row in rows:
            parse_total += sum(int(v) for v in (row.get("parse_errors") or {}).values())
        summary["parse_error_total"] = parse_total
        summary_rows.append(summary)

    csv_path = root / "matrix_summary.csv"
    fieldnames = ["case", "group", "demand_scale", "completed_tls", "failures"]
    for key in METRICS:
        fieldnames.extend([f"{key}_mean", f"{key}_median"])
    fieldnames.append("parse_error_total")
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary_rows)

    print(f"# Chengdu 3-TL Peak Matrix\n")
    print(f"- run_root: `{root}`")
    print(f"- csv: `{csv_path}`")
    print(f"- cases: {len(summary_rows)}\n")
    print("| group | scale | TLs | control usable mean | avg queue mean | delay mean | throughput mean | target ATT | target AWT | network ATT | network AWT | over-threshold sec mean | fallback mean | parse errors |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in sorted(summary_rows, key=lambda item: (float(item["demand_scale"]), item["group"])):
        def fmt(value: Any) -> str:
            if value is None:
                return "-"
            return f"{float(value):.3f}"

        print(
            "| {group} | {scale} | {tls} | {control} | {queue} | {delay} | {throughput} | {target_att} | {target_awt} | {network_att} | {network_awt} | {over} | {fallback} | {parse} |".format(
                group=row["group"],
                scale=row["demand_scale"],
                tls=row["completed_tls"],
                control=fmt(row.get("control_usable_rate_mean")),
                queue=fmt(row.get("avg_queue_vehicles_mean")),
                delay=fmt(row.get("avg_delay_per_vehicle_sec_mean")),
                throughput=fmt(row.get("throughput_veh_per_min_mean")),
                target_att=fmt(row.get("target_tl_att_sec_mean")),
                target_awt=fmt(row.get("target_tl_awt_sec_mean")),
                network_att=fmt(row.get("network_att_sec_mean")),
                network_awt=fmt(row.get("network_awt_sec_mean")),
                over=fmt(row.get("queue_over_threshold_seconds_mean")),
                fallback=fmt(row.get("fallback_plans_applied_mean")),
                parse=row["parse_error_total"],
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
