#!/usr/bin/env python3
"""Rank Chengdu TLS candidates from full per-TL benchmark window metrics."""

from __future__ import annotations

import argparse
import csv
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def parse_bool(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        out = float(text)
    except ValueError:
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def parse_int(value: Any) -> int | None:
    raw = parse_float(value)
    return int(raw) if raw is not None else None


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = sorted({key for row in rows for key in row})
    preferred = [
        "rank",
        "candidate_ok",
        "candidate_tier",
        "recommendation_reason",
        "window_label",
        "tl_id",
        "phase_green_count",
        "selected_prescreen",
        "prescreen_reject_reasons",
        "fringe",
        "boundary_distance_m",
        "tls_neighbor_count",
        "target_tl_trip_completion_ratio",
        "target_tl_trip_completed_count",
        "target_tl_awt_sec",
        "target_tl_att_sec",
        "target_tl_travel_time_delay_sec",
        "avg_queue_length_vehicles",
        "p95_queue_length_vehicles",
        "max_queue_length_vehicles",
        "avg_delay_per_vehicle_sec",
        "throughput_veh_per_min",
        "control_usable_rate",
        "avg_response_time_sec",
        "benchmark_candidate_score",
        "case_dir",
    ]
    fieldnames = [key for key in preferred if key in keys] + [key for key in keys if key not in preferred]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def phase_counts(net_file: Path) -> dict[str, int]:
    root = ET.parse(net_file).getroot()
    out: dict[str, int] = {}
    for tl in root.iter("tlLogic"):
        tl_id = tl.attrib.get("id")
        if not tl_id:
            continue
        out[tl_id] = sum(1 for phase in tl.findall("phase") if any(ch in (phase.attrib.get("state") or "") for ch in "Gg"))
    return out


def prescreen_map(path: Path | None) -> dict[str, dict[str, str]]:
    if not path or not path.exists():
        return {}
    rows = read_csv(path)
    return {row.get("tl_id", ""): row for row in rows if row.get("tl_id")}


def add_candidate_fields(
    row: dict[str, str],
    *,
    prescreen: dict[str, dict[str, str]],
    phase_by_tl: dict[str, int],
    args: argparse.Namespace,
) -> dict[str, Any]:
    tl_id = row.get("tl_id", "")
    pre = prescreen.get(tl_id, {})
    out: dict[str, Any] = {**row}
    out.update(
        {
            "phase_green_count": phase_by_tl.get(tl_id),
            "selected_prescreen": pre.get("selected_prescreen", ""),
            "prescreen_reject_reasons": pre.get("prescreen_reject_reasons", ""),
            "fringe": pre.get("fringe", ""),
            "boundary_distance_m": pre.get("boundary_distance_m", ""),
            "controlled_pair_count": pre.get("controlled_pair_count", ""),
            "viable_pair_count": pre.get("viable_pair_count", ""),
            "source_lane_total": pre.get("source_lane_total", ""),
            "tls_neighbor_count": pre.get("tls_neighbor_count", ""),
            "neighbor_tls": pre.get("neighbor_tls", ""),
            "prescreen_score": pre.get("score", ""),
        }
    )

    completion = parse_float(row.get("target_tl_trip_completion_ratio"))
    completed = parse_int(row.get("target_tl_trip_completed_count"))
    active = parse_bool(row.get("active_tl"))
    step_rows = parse_int(row.get("step_metric_rows")) or 0
    expected_steps = parse_int(row.get("metric_window_steps")) or 0
    usable = parse_float(row.get("control_usable_rate"))
    avg_q = parse_float(row.get("avg_queue_length_vehicles") or row.get("avg_queue_vehicles"))
    p95_q = parse_float(row.get("p95_queue_length_vehicles") or row.get("p95_queue_vehicles"))
    max_q = parse_float(row.get("max_queue_length_vehicles") or row.get("max_queue_vehicles"))
    awt = parse_float(row.get("target_tl_awt_sec"))
    att = parse_float(row.get("target_tl_att_sec"))
    travel_time_delay = parse_float(row.get("target_tl_travel_time_delay_sec"))
    avg_delay = parse_float(row.get("avg_delay_per_vehicle_sec"))
    selected = parse_bool(pre.get("selected_prescreen"))
    reject_reasons = pre.get("prescreen_reject_reasons", "")
    phase_count = phase_by_tl.get(tl_id, 0)
    neighbor_count = parse_int(pre.get("tls_neighbor_count")) or 0

    hard_reasons: list[str] = []
    caution_reasons: list[str] = []
    if selected is False:
        hard_reasons.append(f"prescreen_rejected:{reject_reasons or 'unknown'}")
    if pre.get("fringe") == "outer":
        hard_reasons.append("fringe_outer")
    if active is False:
        hard_reasons.append("inactive_metric_window")
    if expected_steps and step_rows < int(expected_steps * args.min_step_coverage):
        hard_reasons.append("insufficient_step_rows")
    if usable is not None and usable < args.min_control_usable:
        hard_reasons.append("low_control_usable")
    if completion is not None and completion < args.min_completion_ratio:
        hard_reasons.append("low_target_completion_ratio")
    if completed is not None and completed < args.min_completed_count:
        hard_reasons.append("low_target_completed_count")
    if avg_q is None or p95_q is None:
        hard_reasons.append("missing_queue_length_metrics")
    if awt is None or att is None:
        hard_reasons.append("missing_target_att_awt")
    if travel_time_delay is None:
        hard_reasons.append("missing_travel_time_delay")
    if p95_q is not None and p95_q > args.max_p95_queue:
        hard_reasons.append("source_or_target_queue_too_high")
    if max_q is not None and max_q > args.max_queue:
        hard_reasons.append("extreme_queue")

    if phase_count <= 2:
        caution_reasons.append("simple_2_green_phase")
    if neighbor_count < 2:
        caution_reasons.append("weak_corridor_connectivity")
    if avg_q is not None and avg_q < args.min_avg_queue_signal:
        caution_reasons.append("too_easy_low_avg_queue")
    if p95_q is not None and p95_q < args.min_p95_queue_signal:
        caution_reasons.append("too_easy_low_p95_queue")

    candidate_ok = not hard_reasons
    if candidate_ok and not caution_reasons:
        tier = "A"
    elif candidate_ok:
        tier = "B"
    else:
        tier = "reject"

    # Lower score is better. Keep it transparent: performance metrics plus
    # penalties for routes that are less useful as benchmark intersections.
    score = 0.0
    score += awt if awt is not None else 500.0
    score += 0.45 * (travel_time_delay if travel_time_delay is not None else 500.0)
    score += 1.0 * (avg_q if avg_q is not None else 100.0)
    score += 0.05 * (avg_delay if avg_delay is not None else 1000.0)
    score += 30.0 * len(hard_reasons)
    score += 8.0 * len(caution_reasons)
    if phase_count >= 3:
        score -= 10.0
    if neighbor_count >= 2:
        score -= 5.0

    out["candidate_ok"] = candidate_ok
    out["candidate_tier"] = tier
    out["hard_reject_reasons"] = ",".join(hard_reasons)
    out["caution_reasons"] = ",".join(caution_reasons)
    out["recommendation_reason"] = "ok" if candidate_ok else ";".join(hard_reasons)
    out["benchmark_candidate_score"] = round(score, 6)
    return out


def write_markdown(path: Path, rows: list[dict[str, Any]], primary_window: str) -> None:
    top = [row for row in rows if row.get("window_label") == primary_window and row.get("candidate_ok") is True]
    top = sorted(top, key=lambda row: (row.get("candidate_tier") != "A", float(row.get("benchmark_candidate_score") or 1e9)))[:10]
    lines = [
        "# Chengdu TLS Candidate Ranking",
        "",
        f"Primary window: `{primary_window}`",
        "",
        "The ranking keeps low-completion and fringe intersections in the audit table, but excludes them from the recommended list.",
        "Queue length is reported as the target-intersection unique incoming-lane vehicle count; travel delay is SUMO `tripinfo.timeLoss`, following the performance-metric definitions in Song et al., Transportation Research Part C 160 (2024) 104528.",
        "",
        "## Top candidates",
        "",
        "| Rank | TL ID | Tier | Green phases | Target AWT | Travel delay | Queue length | P95 queue | Completion | Notes |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for idx, row in enumerate(top, 1):
        lines.append(
                "| {rank} | `{tl}` | {tier} | {phases} | {awt} | {delay} | {avgq} | {p95q} | {completion} | {notes} |".format(
                rank=idx,
                tl=row.get("tl_id", ""),
                tier=row.get("candidate_tier", ""),
                phases=row.get("phase_green_count", ""),
                awt=row.get("target_tl_awt_sec", ""),
                delay=row.get("target_tl_travel_time_delay_sec", ""),
                avgq=row.get("avg_queue_length_vehicles", row.get("avg_queue_vehicles", "")),
                p95q=row.get("p95_queue_length_vehicles", row.get("p95_queue_vehicles", "")),
                completion=row.get("target_tl_trip_completion_ratio", ""),
                notes=row.get("caution_reasons", "") or "ok",
            )
        )
    lines.extend(
        [
            "",
            "## Files",
            "",
            "- `all_tls_ranked.csv`: all rows, including rejects.",
            "- `recommended_tls.csv`: filtered candidates in the primary window.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-metrics", type=Path, required=True)
    parser.add_argument("--net-file", type=Path, required=True)
    parser.add_argument("--prescreen", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--primary-window", default="metric_300_900")
    parser.add_argument("--controller", default="model")
    parser.add_argument("--min-completion-ratio", type=float, default=0.70)
    parser.add_argument("--min-completed-count", type=int, default=20)
    parser.add_argument("--min-control-usable", type=float, default=0.95)
    parser.add_argument("--min-step-coverage", type=float, default=0.95)
    parser.add_argument("--max-p95-queue", type=float, default=120.0)
    parser.add_argument("--max-queue", type=float, default=220.0)
    parser.add_argument("--min-avg-queue-signal", type=float, default=1.0)
    parser.add_argument("--min-p95-queue-signal", type=float, default=6.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    phase_by_tl = phase_counts(args.net_file)
    pre = prescreen_map(args.prescreen)
    raw_rows = [
        row
        for row in read_csv(args.window_metrics)
        if (not args.controller or row.get("controller") == args.controller)
    ]
    ranked = [
        add_candidate_fields(row, prescreen=pre, phase_by_tl=phase_by_tl, args=args)
        for row in raw_rows
    ]
    ranked.sort(
        key=lambda row: (
            row.get("window_label") != args.primary_window,
            row.get("candidate_tier") == "reject",
            row.get("candidate_tier") != "A",
            float(row.get("benchmark_candidate_score") or 1e9),
        )
    )
    primary = [row for row in ranked if row.get("window_label") == args.primary_window]
    recommended = [row for row in primary if row.get("candidate_ok") is True]
    recommended.sort(key=lambda row: (row.get("candidate_tier") != "A", float(row.get("benchmark_candidate_score") or 1e9)))
    for idx, row in enumerate(recommended, 1):
        row["rank"] = idx
    for idx, row in enumerate(ranked, 1):
        row.setdefault("rank", idx)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "all_tls_ranked.csv", ranked)
    write_csv(args.output_dir / "recommended_tls.csv", recommended)
    write_markdown(args.output_dir / "candidate_recommendations.md", ranked, args.primary_window)
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "rows": len(ranked),
                "primary_rows": len(primary),
                "recommended": len(recommended),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
