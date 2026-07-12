#!/usr/bin/env python3
"""Pre-screen SUMO traffic lights for short probe experiments.

The script follows the paper-style setup at a practical level: choose an
interior connected sub-network first, then run fixed/maxpressure probes on that
candidate set before selecting the final 3-5 intersections.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from itertools import combinations
from pathlib import Path
from typing import Any


def parse_sumocfg_net(sumocfg: Path) -> Path:
    root = ET.parse(sumocfg).getroot()
    for el in root.iter():
        if el.tag.split("}")[-1] != "net-file":
            continue
        value = el.attrib.get("value") or (el.text.strip() if el.text else "")
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = sumocfg.parent / path
        return path.resolve()
    raise ValueError(f"missing net-file in {sumocfg}")


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def edge_info(net_root: ET.Element) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for edge in net_root.iter("edge"):
        edge_id = edge.attrib.get("id")
        if not edge_id or edge_id.startswith(":"):
            continue
        lanes = list(edge.findall("lane"))
        lengths: list[float] = []
        for lane in lanes:
            try:
                lengths.append(float(lane.attrib.get("length", "0")))
            except ValueError:
                continue
        out[edge_id] = {
            "from": edge.attrib.get("from"),
            "to": edge.attrib.get("to"),
            "lanes": len(lanes),
            "length": mean(lengths) or 0.0,
        }
    return out


def junction_info(net_root: ET.Element) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for junction in net_root.iter("junction"):
        jid = junction.attrib.get("id")
        if not jid or jid.startswith(":"):
            continue
        try:
            x = float(junction.attrib.get("x", "nan"))
            y = float(junction.attrib.get("y", "nan"))
        except ValueError:
            continue
        out[jid] = {
            "id": jid,
            "type": junction.attrib.get("type", ""),
            "x": x,
            "y": y,
            "fringe": junction.attrib.get("fringe", ""),
            "inc_lanes": (junction.attrib.get("incLanes") or "").split(),
        }
    return out


def tl_ids(net_root: ET.Element) -> set[str]:
    return {
        el.attrib["id"]
        for el in net_root.iter("tlLogic")
        if el.attrib.get("id")
    }


def controlled_pairs(net_root: ET.Element, tls: set[str]) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {tl_id: [] for tl_id in tls}
    seen: set[tuple[str, str, str]] = set()
    for conn in net_root.iter("connection"):
        tl_id = conn.attrib.get("tl")
        if tl_id not in tls:
            continue
        src = conn.attrib.get("from")
        dst = conn.attrib.get("to")
        if not src or not dst or src.startswith(":") or dst.startswith(":"):
            continue
        key = (tl_id, src, dst)
        if key in seen:
            continue
        seen.add(key)
        out[tl_id].append((src, dst))
    return out


def network_bbox(junctions: dict[str, dict[str, Any]]) -> tuple[float, float, float, float]:
    xs = [float(row["x"]) for row in junctions.values()]
    ys = [float(row["y"]) for row in junctions.values()]
    return min(xs), max(xs), min(ys), max(ys)


def adjacency_graph(
    tls: set[str],
    junctions: dict[str, dict[str, Any]],
    edges: dict[str, dict[str, Any]],
    max_neighbor_distance: float,
) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {tl_id: set() for tl_id in tls}
    for info in edges.values():
        src = info.get("from")
        dst = info.get("to")
        if src in tls and dst in tls and src != dst:
            graph[src].add(dst)
            graph[dst].add(src)

    # Some SUMO networks split roads through non-signal junctions. Add a
    # conservative geometric fallback so nearby interior TLS can still form a
    # probe corridor candidate.
    positions = {
        tl_id: (float(junctions[tl_id]["x"]), float(junctions[tl_id]["y"]))
        for tl_id in tls
        if tl_id in junctions
    }
    for a, b in combinations(sorted(positions), 2):
        if distance(positions[a], positions[b]) <= max_neighbor_distance:
            graph[a].add(b)
            graph[b].add(a)
    return graph


def score_tl(
    tl_id: str,
    junctions: dict[str, dict[str, Any]],
    pairs: list[tuple[str, str]],
    edges: dict[str, dict[str, Any]],
    bbox: tuple[float, float, float, float],
    graph: dict[str, set[str]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    junction = junctions.get(tl_id, {})
    x = float(junction.get("x", 0.0))
    y = float(junction.get("y", 0.0))
    min_x, max_x, min_y, max_y = bbox
    boundary_distance = min(x - min_x, max_x - x, y - min_y, max_y - y)
    source_edges = sorted({src for src, _dst in pairs})
    dest_edges = sorted({dst for _src, dst in pairs})
    viable_pairs = [
        (src, dst)
        for src, dst in pairs
        if float(edges.get(src, {}).get("length") or 0.0) >= args.min_source_length
        and float(edges.get(dst, {}).get("length") or 0.0) >= args.min_dest_length
    ]
    viable_sources = sorted({src for src, _dst in viable_pairs})
    source_lengths = [float(edges.get(src, {}).get("length") or 0.0) for src in source_edges]
    dest_lengths = [float(edges.get(dst, {}).get("length") or 0.0) for dst in dest_edges]
    source_lanes = [float(edges.get(src, {}).get("lanes") or 0.0) for src in source_edges]
    neighbor_count = len(graph.get(tl_id, set()))
    fringe_outer = junction.get("fringe") == "outer"

    hard_reasons: list[str] = []
    if fringe_outer:
        hard_reasons.append("fringe_outer")
    if boundary_distance < args.min_boundary_distance:
        hard_reasons.append("near_bbox_boundary")
    if len(pairs) < args.min_controlled_pairs:
        hard_reasons.append("too_few_controlled_pairs")
    if len(viable_pairs) < args.target_peak_routes_per_tl:
        hard_reasons.append("too_few_viable_pairs")
    if len(viable_sources) < min(args.target_peak_routes_per_tl, len(viable_pairs)):
        hard_reasons.append("low_source_diversity")
    if neighbor_count < args.min_neighbor_count:
        hard_reasons.append("weak_tls_connectivity")

    score = (
        min(boundary_distance, 800.0) / 20.0
        + len(viable_pairs) * 3.0
        + len(viable_sources) * 8.0
        + sum(source_lanes) * 2.0
        + neighbor_count * 10.0
        - len(hard_reasons) * 40.0
    )
    return {
        "scenario": args.scenario,
        "tl_id": tl_id,
        "selected_prescreen": not hard_reasons,
        "prescreen_reject_reasons": ",".join(hard_reasons),
        "score": score,
        "x": x,
        "y": y,
        "fringe": junction.get("fringe", ""),
        "boundary_distance_m": boundary_distance,
        "controlled_pair_count": len(pairs),
        "source_edge_count": len(source_edges),
        "dest_edge_count": len(dest_edges),
        "viable_pair_count": len(viable_pairs),
        "viable_source_edge_count": len(viable_sources),
        "mean_source_length_m": mean(source_lengths),
        "mean_dest_length_m": mean(dest_lengths),
        "source_lane_total": sum(source_lanes),
        "tls_neighbor_count": neighbor_count,
        "neighbor_tls": ",".join(sorted(graph.get(tl_id, set()))),
    }


def connected(graph: dict[str, set[str]], nodes: tuple[str, ...]) -> bool:
    target = set(nodes)
    seen = {nodes[0]}
    queue: deque[str] = deque([nodes[0]])
    while queue:
        node = queue.popleft()
        for nxt in graph.get(node, set()) & target:
            if nxt in seen:
                continue
            seen.add(nxt)
            queue.append(nxt)
    return seen == target


def group_candidates(
    rows: list[dict[str, Any]],
    graph: dict[str, set[str]],
    min_size: int,
    max_size: int,
    limit: int,
) -> list[dict[str, Any]]:
    selected = [row for row in rows if row["selected_prescreen"]]
    score_by_id = {row["tl_id"]: float(row["score"]) for row in selected}
    ids = sorted(score_by_id)
    groups: list[dict[str, Any]] = []
    for size in range(min_size, max_size + 1):
        for combo in combinations(ids, size):
            if not connected(graph, combo):
                continue
            scores = [score_by_id[node] for node in combo]
            internal_edges = sum(len(graph[node] & set(combo)) for node in combo) / 2.0
            density = internal_edges / max(1.0, size * (size - 1) / 2.0)
            groups.append(
                {
                    "group_tls": ",".join(combo),
                    "group_size": size,
                    "group_score": statistics.mean(scores) + 20.0 * density,
                    "mean_tls_score": statistics.mean(scores),
                    "min_tls_score": min(scores),
                    "connectivity_density": density,
                }
            )
    groups.sort(key=lambda row: float(row["group_score"]), reverse=True)
    return groups[:limit]


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
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sumocfg", type=Path)
    source.add_argument("--net", type=Path)
    parser.add_argument("--scenario", default="sumo_llm")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--group-limit", type=int, default=30)
    parser.add_argument("--group-min-size", type=int, default=3)
    parser.add_argument("--group-max-size", type=int, default=5)
    parser.add_argument("--target-peak-routes-per-tl", type=int, default=2)
    parser.add_argument("--min-source-length", type=float, default=80.0)
    parser.add_argument("--min-dest-length", type=float, default=80.0)
    parser.add_argument("--min-boundary-distance", type=float, default=250.0)
    parser.add_argument("--min-controlled-pairs", type=int, default=6)
    parser.add_argument("--min-neighbor-count", type=int, default=1)
    parser.add_argument("--max-neighbor-distance", type=float, default=450.0)
    args = parser.parse_args()

    net_path = parse_sumocfg_net(args.sumocfg) if args.sumocfg else args.net
    assert net_path is not None
    net_root = ET.parse(net_path).getroot()
    junctions = junction_info(net_root)
    tls = tl_ids(net_root)
    edges = edge_info(net_root)
    pairs_by_tl = controlled_pairs(net_root, tls)
    bbox = network_bbox(junctions)
    graph = adjacency_graph(tls, junctions, edges, args.max_neighbor_distance)

    rows = [
        score_tl(tl_id, junctions, pairs_by_tl.get(tl_id, []), edges, bbox, graph, args)
        for tl_id in sorted(tls)
        if tl_id in junctions
    ]
    rows.sort(key=lambda row: float(row["score"]), reverse=True)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "candidate_tls_prescreen.csv", rows)

    selected = [row for row in rows if row["selected_prescreen"]][: args.top_n]
    tls_rows = [{"scenario": args.scenario, "tl_id": row["tl_id"]} for row in selected]
    write_csv(args.output_dir / "candidate_tls_short_probe.csv", tls_rows, ["scenario", "tl_id"])
    write_csv(
        args.output_dir / "candidate_tls_groups.csv",
        group_candidates(rows, graph, args.group_min_size, args.group_max_size, args.group_limit),
    )

    print(args.output_dir / "candidate_tls_prescreen.csv")
    print(args.output_dir / "candidate_tls_short_probe.csv")
    print(args.output_dir / "candidate_tls_groups.csv")


if __name__ == "__main__":
    main()
