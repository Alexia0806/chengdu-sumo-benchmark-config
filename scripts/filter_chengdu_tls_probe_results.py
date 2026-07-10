#!/usr/bin/env python3
"""Filter TLS candidates after fixed/maxpressure short probes."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from itertools import combinations
from pathlib import Path
from typing import Any


def find_case_dirs(roots: list[Path]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for root in roots:
        if (root / "step_metrics.jsonl").exists():
            out[root.name] = root
            continue
        for child in sorted(root.iterdir() if root.exists() else []):
            if child.is_dir() and (child / "step_metrics.jsonl").exists():
                out[child.name] = child
    return out


def route_file(case_dir: Path) -> Path | None:
    matches = list(case_dir.glob("runtime_sumo/**/target_peak*.rou.xml"))
    return matches[0] if matches else None


def target_source_edges(case_dir: Path, tl_id: str) -> set[str]:
    path = route_file(case_dir)
    if path is None or not path.exists():
        return set()
    root = ET.parse(path).getroot()
    route_edges: dict[str, list[str]] = {}
    for route in root.iter("route"):
        rid = route.attrib.get("id")
        edges = (route.attrib.get("edges") or "").split()
        if rid:
            route_edges[rid] = edges
    sources: set[str] = set()
    for flow in root.iter("flow"):
        flow_id = flow.attrib.get("id", "")
        rid = flow.attrib.get("route", "")
        if not flow_id.startswith(f"target_peak_{tl_id}_"):
            continue
        edges = route_edges.get(rid) or []
        if edges:
            sources.add(edges[0])
    return sources


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    idx = (len(values) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(values) - 1)
    frac = idx - lo
    return values[lo] * (1.0 - frac) + values[hi] * frac


def source_queue_stats(case_dir: Path, tl_id: str, window: str) -> dict[str, Any]:
    try:
        start_s, end_s = window.split("_", 1)
        start = float(start_s)
        end = float(end_s)
    except ValueError:
        raise ValueError(f"window must look like 300_900, got {window!r}")
    sources = target_source_edges(case_dir, tl_id)
    if not sources:
        return {
            "target_source_edges": "",
            "target_source_avg_queue": None,
            "target_source_p95_queue": None,
            "target_source_max_queue": None,
            "target_source_queue_over_30_frac": None,
        }
    samples: list[float] = []
    path = case_dir / "step_metrics.jsonl"
    if not path.exists():
        return {
            "target_source_edges": ",".join(sorted(sources)),
            "target_source_avg_queue": None,
            "target_source_p95_queue": None,
            "target_source_max_queue": None,
            "target_source_queue_over_30_frac": None,
        }
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if str(row.get("tl_id")) != tl_id:
                continue
            step = float(row.get("step") or row.get("sim_time_after_step") or 0)
            if not (start <= step < end):
                continue
            lane_queues = row.get("lane_queues") or {}
            total = 0.0
            for lane, value in lane_queues.items():
                if any(str(lane).startswith(f"{source}_") for source in sources):
                    total += float(value or 0.0)
            samples.append(total)
    return {
        "target_source_edges": ",".join(sorted(sources)),
        "target_source_avg_queue": sum(samples) / len(samples) if samples else None,
        "target_source_p95_queue": percentile(samples, 0.95),
        "target_source_max_queue": max(samples) if samples else None,
        "target_source_queue_over_30_frac": (
            sum(1 for value in samples if value > 30.0) / len(samples) if samples else None
        ),
    }


def read_prescreen(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    with path.open(newline="") as handle:
        return {row["tl_id"]: row for row in csv.DictReader(handle)}


def read_fairness(path: Path, window: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("window") == window:
                rows.append(row)
    return rows


def fnum(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def connected(graph: dict[str, set[str]], nodes: tuple[str, ...]) -> bool:
    target = set(nodes)
    seen = {nodes[0]}
    queue: deque[str] = deque([nodes[0]])
    while queue:
        node = queue.popleft()
        for nxt in graph.get(node, set()) & target:
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen == target


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fairness-per-tl", type=Path, required=True)
    parser.add_argument("--probe-root", action="append", type=Path, required=True)
    parser.add_argument("--prescreen", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--window", default="300_900")
    parser.add_argument("--scenario", default="sumo_llm")
    parser.add_argument("--min-completed-rate-pct", type=float, default=60.0)
    parser.add_argument("--min-arrived-rate-pct", type=float, default=35.0)
    parser.add_argument("--max-source-avg-queue", type=float, default=45.0)
    parser.add_argument("--max-source-p95-queue", type=float, default=90.0)
    parser.add_argument("--group-min-size", type=int, default=3)
    parser.add_argument("--group-max-size", type=int, default=5)
    args = parser.parse_args()

    case_dirs = find_case_dirs(args.probe_root)
    prescreen = read_prescreen(args.prescreen)
    rows: list[dict[str, Any]] = []
    for row in read_fairness(args.fairness_per_tl, args.window):
        case_dir = case_dirs.get(row["case"])
        queue_stats = source_queue_stats(case_dir, row["tl_id"], args.window) if case_dir else {}
        pre = prescreen.get(row["tl_id"], {})
        rows.append(
            {
                **row,
                **queue_stats,
                "prescreen_selected": pre.get("selected_prescreen", ""),
                "prescreen_reject_reasons": pre.get("prescreen_reject_reasons", ""),
                "prescreen_score": pre.get("score", ""),
                "neighbor_tls": pre.get("neighbor_tls", ""),
            }
        )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["tl_id"]].append(row)

    summary_rows: list[dict[str, Any]] = []
    for tl_id, tl_rows in grouped.items():
        completed_rates = [fnum(row, "completed_by_sim_end_rate_pct") for row in tl_rows]
        arrived_rates = [fnum(row, "arrived_by_window_end_rate_pct") for row in tl_rows]
        source_avg = [fnum(row, "target_source_avg_queue") for row in tl_rows]
        source_p95 = [fnum(row, "target_source_p95_queue") for row in tl_rows]
        completed_rates = [value for value in completed_rates if value is not None]
        arrived_rates = [value for value in arrived_rates if value is not None]
        source_avg = [value for value in source_avg if value is not None]
        source_p95 = [value for value in source_p95 if value is not None]

        reasons: list[str] = []
        pre = prescreen.get(tl_id, {})
        if pre and pre.get("selected_prescreen") != "True":
            reasons.append(f"prescreen:{pre.get('prescreen_reject_reasons')}")
        if completed_rates and min(completed_rates) < args.min_completed_rate_pct:
            reasons.append("low_completion_rate")
        if arrived_rates and min(arrived_rates) < args.min_arrived_rate_pct:
            reasons.append("low_arrival_rate")
        if source_avg and max(source_avg) > args.max_source_avg_queue:
            reasons.append("source_avg_queue_high")
        if source_p95 and max(source_p95) > args.max_source_p95_queue:
            reasons.append("source_p95_queue_high")

        summary_rows.append(
            {
                "scenario": args.scenario,
                "tl_id": tl_id,
                "selected_after_probe": not reasons,
                "probe_reject_reasons": ",".join(reasons),
                "controller_cases": len(tl_rows),
                "min_completed_rate_pct": min(completed_rates) if completed_rates else None,
                "min_arrived_rate_pct": min(arrived_rates) if arrived_rates else None,
                "max_source_avg_queue": max(source_avg) if source_avg else None,
                "max_source_p95_queue": max(source_p95) if source_p95 else None,
                "mean_completed_rate_pct": statistics.mean(completed_rates) if completed_rates else None,
                "prescreen_score": pre.get("score", ""),
                "neighbor_tls": pre.get("neighbor_tls", ""),
            }
        )
    summary_rows.sort(
        key=lambda row: (
            row["selected_after_probe"],
            float(row["mean_completed_rate_pct"] or 0.0),
            float(row["prescreen_score"] or 0.0),
        ),
        reverse=True,
    )

    final_tls = [
        {"scenario": args.scenario, "tl_id": row["tl_id"]}
        for row in summary_rows
        if row["selected_after_probe"]
    ]
    graph = {
        tl_id: set((prescreen.get(tl_id, {}).get("neighbor_tls") or "").split(",")) - {""}
        for tl_id in prescreen
    }
    selected_ids = [row["tl_id"] for row in summary_rows if row["selected_after_probe"]]
    rate_by_id = {
        row["tl_id"]: float(row["mean_completed_rate_pct"] or 0.0)
        for row in summary_rows
        if row["selected_after_probe"]
    }
    group_rows: list[dict[str, Any]] = []
    for size in range(args.group_min_size, args.group_max_size + 1):
        for combo in combinations(sorted(selected_ids), size):
            if graph and not connected(graph, combo):
                continue
            rates = [rate_by_id[item] for item in combo]
            group_rows.append(
                {
                    "group_tls": ",".join(combo),
                    "group_size": size,
                    "mean_completed_rate_pct": statistics.mean(rates),
                    "min_completed_rate_pct": min(rates),
                }
            )
    group_rows.sort(key=lambda row: (float(row["min_completed_rate_pct"]), float(row["mean_completed_rate_pct"])), reverse=True)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "probe_tls_detail.csv", rows)
    write_csv(args.output_dir / "probe_tls_filter_summary.csv", summary_rows)
    write_csv(args.output_dir / "final_tls_candidates.csv", final_tls, ["scenario", "tl_id"])
    write_csv(args.output_dir / "final_tls_groups.csv", group_rows[:30])
    print(args.output_dir / "probe_tls_filter_summary.csv")
    print(args.output_dir / "final_tls_candidates.csv")
    print(args.output_dir / "final_tls_groups.csv")


if __name__ == "__main__":
    main()
