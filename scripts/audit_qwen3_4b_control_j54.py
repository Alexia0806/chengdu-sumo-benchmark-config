#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


def import_benchmark_module(project_root: Path) -> Any:
    path = project_root / "scripts" / "deepsignal_cycleplan_benchmark_chengdu_metrics.py"
    spec = importlib.util.spec_from_file_location("bench", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def find_json_candidates(text: str):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", text):
        start = match.start()
        try:
            obj, end = decoder.raw_decode(text[start:])
        except Exception:
            continue
        yield obj, start, start + end


def relaxed_extract(bench: Any, text: str):
    chunks: list[tuple[str, str]] = []
    match = re.search(r"<SOLUTION>(.*?)</SOLUTION>", text, flags=re.S | re.I)
    if match:
        chunks.append((match.group(1), "solution_block"))
    chunks.append((text, "raw_scan"))

    last_error = None
    for chunk, source in chunks:
        for obj, _, _ in find_json_candidates(chunk):
            try:
                solution, normalize_error = bench.normalize_solution(obj, allow_dict=True)
            except Exception as exc:  # pragma: no cover - defensive for remote runner variants
                last_error = f"normalize_exception:{type(exc).__name__}:{exc}"
                continue
            if normalize_error is not None:
                last_error = normalize_error
                continue
            return solution, None, source
    return None, last_error or "no_json_candidate", None


def clamped_if_complete(bench: Any, phase_waits: list[dict[str, Any]], solution: dict[str, Any] | None):
    if solution is None:
        return None, False, ["no_solution"]
    expected = {str(item["phase_id"]) for item in phase_waits}
    got = set(solution.keys())
    if got != expected:
        return None, False, [f"phase_set_mismatch expected={sorted(expected)} got={sorted(got)}"]

    by_id = {str(item["phase_id"]): item for item in phase_waits}
    repaired: dict[str, int] = {}
    actions: list[str] = []
    for phase_id, value in solution.items():
        info = by_id[phase_id]
        lo = int(info["min_green"])
        hi = int(info["max_green"])
        try:
            raw_value = int(round(float(value)))
        except Exception:
            return None, False, [f"non_numeric:{phase_id}:{value}"]
        clipped = min(max(raw_value, lo), hi)
        if clipped != raw_value:
            actions.append(f"clip:{phase_id}:{raw_value}->{clipped}")
        repaired[phase_id] = clipped

    ok, violations = bench.validate_plan(phase_waits, repaired)
    return repaired, bool(ok), actions + violations


def load_samples(source_run: Path, sample_indices: list[int], scales: list[str]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for tag in scales:
        path = source_run / f"04_qwen3_4b_base_min_green_temp01_x{tag}" / "prediction_inputs.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not rows:
            raise RuntimeError(f"empty prediction input file: {path}")
        for index in sample_indices:
            row = rows[min(index, len(rows) - 1)]
            samples.append(
                {
                    "sample_id": f"x{tag}_i{index}_t{row.get('sim_time')}",
                    "scale_tag": tag,
                    "sim_time": row.get("sim_time"),
                    "prediction_input": row["prediction_input"],
                }
            )
    return samples


def make_model_args(config: dict[str, Any], model_path: Path):
    return argparse.Namespace(
        model_backend="hf",
        prompt_format=config["prompt_format"],
        prefill=False,
        hf_model_path=model_path,
        hf_adapter_path=None,
        hf_dtype="bfloat16",
        hf_device_map="auto",
        n_predict=config["n_predict"],
        temperature=config["temperature"],
        input_mode="github_official",
    )


def run_audit(args: argparse.Namespace) -> int:
    project_root = args.project_root.resolve()
    source_run = args.source_run.resolve()
    output_dir = args.output_dir.resolve()
    model_path = args.model_path.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    bench = import_benchmark_module(project_root)
    samples = load_samples(source_run, args.sample_indices, args.scales)
    configs = [
        {"label": "deepsignal_temp01_np384", "prompt_format": "deepsignal", "temperature": 0.1, "n_predict": 384},
        {"label": "deepsignal_temp01_np1024", "prompt_format": "deepsignal", "temperature": 0.1, "n_predict": 1024},
        {"label": "jsononly_temp00_np256", "prompt_format": "deepsignal_json", "temperature": 0.0, "n_predict": 256},
        {"label": "jsononly_temp01_np256", "prompt_format": "deepsignal_json", "temperature": 0.1, "n_predict": 256},
    ]

    rows: list[dict[str, Any]] = []
    raw_path = output_dir / "audit_calls.jsonl"
    print(f"AUDIT_START out={output_dir} samples={len(samples)} configs={len(configs)}", flush=True)
    with raw_path.open("w", encoding="utf-8") as raw_fh:
        for config in configs:
            model_args = make_model_args(config, model_path)
            print(f"CONFIG_START {config['label']}", flush=True)
            for sample in samples:
                started = time.time()
                result = bench.call_model(None, sample["prediction_input"], model_args)
                elapsed_total = time.time() - started
                phase_waits = sample["prediction_input"]["prediction"]["phase_waits"]

                strict_ok = bool(result.format_ok and result.control_usable)
                relaxed_solution, relaxed_error, relaxed_source = relaxed_extract(bench, result.raw_text or "")
                relaxed_ok, relaxed_violations = bench.validate_plan(phase_waits, relaxed_solution)
                relaxed_ok = bool(relaxed_solution is not None and relaxed_ok)
                repaired_solution, repaired_ok, repair_notes = clamped_if_complete(
                    bench,
                    phase_waits,
                    relaxed_solution,
                )

                row = {
                    "config": config["label"],
                    "prompt_format": config["prompt_format"],
                    "temperature": config["temperature"],
                    "n_predict": config["n_predict"],
                    "sample_id": sample["sample_id"],
                    "scale_tag": sample["scale_tag"],
                    "sim_time": sample["sim_time"],
                    "strict_format_ok": bool(result.format_ok),
                    "strict_control_usable": strict_ok,
                    "strict_parse_error": result.parse_error,
                    "strict_violations": result.violations,
                    "relaxed_control_usable": relaxed_ok,
                    "relaxed_error": relaxed_error,
                    "relaxed_source": relaxed_source,
                    "relaxed_violations": relaxed_violations,
                    "repaired_control_usable": repaired_ok,
                    "repair_notes": repair_notes,
                    "solution": result.solution,
                    "relaxed_solution": relaxed_solution,
                    "repaired_solution": repaired_solution,
                    "elapsed_sec_model": result.elapsed_sec,
                    "elapsed_sec_total": elapsed_total,
                    "raw_text_prefix": (result.raw_text or "")[:500],
                    "raw_text_tail": (result.raw_text or "")[-1500:],
                }
                rows.append(row)
                raw_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                raw_fh.flush()
                print(
                    "CALL_DONE",
                    config["label"],
                    sample["sample_id"],
                    f"strict={int(strict_ok)} relaxed={int(relaxed_ok)} repaired={int(repaired_ok)}",
                    f"err={result.parse_error}",
                    f"elapsed={elapsed_total:.1f}s",
                    flush=True,
                )

    summary_path = output_dir / "summary_by_config.csv"
    fieldnames = [
        "config",
        "calls",
        "strict_format_rate",
        "strict_control_rate",
        "relaxed_control_rate",
        "repaired_control_rate",
        "avg_elapsed_sec",
        "parse_errors",
    ]
    with summary_path.open("w", encoding="utf-8", newline="") as csv_fh:
        writer = csv.DictWriter(csv_fh, fieldnames=fieldnames)
        writer.writeheader()
        for config in configs:
            subset = [row for row in rows if row["config"] == config["label"]]
            calls = len(subset)

            def rate(key: str) -> float:
                return sum(1 for row in subset if row[key]) / calls if calls else 0.0

            parse_counts = Counter(row["strict_parse_error"] for row in subset)
            writer.writerow(
                {
                    "config": config["label"],
                    "calls": calls,
                    "strict_format_rate": rate("strict_format_ok"),
                    "strict_control_rate": rate("strict_control_usable"),
                    "relaxed_control_rate": rate("relaxed_control_usable"),
                    "repaired_control_rate": rate("repaired_control_usable"),
                    "avg_elapsed_sec": (
                        sum(float(row["elapsed_sec_total"]) for row in subset) / calls if calls else 0.0
                    ),
                    "parse_errors": json.dumps(parse_counts, ensure_ascii=False),
                }
            )

    md_lines = [
        "# J54 Qwen3-4B Base Control Audit",
        "",
        f"- generated_at: {datetime.now().isoformat()}",
        f"- source_run: `{source_run}`",
        f"- samples: {len(samples)}",
        f"- raw: `{raw_path}`",
        "",
        "| config | calls | strict format | strict control | relaxed control | repaired control | avg sec |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in csv.DictReader(summary_path.open(encoding="utf-8")):
        md_lines.append(
            f"| {row['config']} | {row['calls']} | "
            f"{float(row['strict_format_rate']) * 100:.1f}% | "
            f"{float(row['strict_control_rate']) * 100:.1f}% | "
            f"{float(row['relaxed_control_rate']) * 100:.1f}% | "
            f"{float(row['repaired_control_rate']) * 100:.1f}% | "
            f"{float(row['avg_elapsed_sec']):.1f} |"
        )
    summary_md = output_dir / "audit_summary.md"
    summary_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print("AUDIT_DONE", output_dir, flush=True)
    print(summary_md.read_text(encoding="utf-8"), flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path("/root/autodl-tmp/tsc-cycle-benchmark"))
    parser.add_argument(
        "--source-run",
        type=Path,
        default=Path(
            "/root/autodl-tmp/tsc-cycle-benchmark/runs/deepsignal_cycleplan/"
            "chengdu_j54_9bbase_7kft_4b_fp16_att_awt_x1p8_20260622"
        ),
    )
    parser.add_argument("--model-path", type=Path, default=Path("/root/autodl-tmp/models/Qwen3-4B"))
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/root/autodl-tmp/tsc-cycle-benchmark/runs/control_audit/j54_qwen3_4b_base_20260622"),
    )
    parser.add_argument("--sample-indices", type=int, nargs="+", default=[0, 8, 16])
    parser.add_argument("--scales", nargs="+", default=["1p0", "1p2", "1p5", "1p8"])
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run_audit(parse_args()))
