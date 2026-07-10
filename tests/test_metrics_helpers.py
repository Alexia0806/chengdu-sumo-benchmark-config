from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
METRICS_PATH = ROOT / "scripts" / "deepsignal_cycleplan_benchmark_chengdu_metrics.py"


def load_metrics_module():
    spec = importlib.util.spec_from_file_location("chengdu_metrics", METRICS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {METRICS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


metrics = load_metrics_module()


def valid_args(**overrides):
    values = {
        "warmup_seconds": 300,
        "metric_seconds": 1200,
        "decision_interval_seconds": 60,
        "min_green": 10,
        "max_green": 90,
        "demand_scale": 1.0,
        "target_peak_vph_per_route": 0.0,
        "target_peak_routes_per_tl": 8,
        "target_peak_min_source_length": 0.0,
        "target_peak_min_dest_length": 0.0,
        "target_peak_begin": 0.0,
        "target_peak_end": None,
        "forecaster_min_history_steps": 5,
        "forecaster_history_steps": 60,
        "tripinfo_drain_seconds": 0,
        "queue_threshold": 10.0,
        "queue_thresholds": None,
        "gui_delay_ms": None,
        "traci_start_retries": 1,
        "phase_queue_mode": "raw",
        "simulation_seconds": None,
        "input_mode": "legacy_snapshot",
        "controller": "fixed",
        "pred_wait_forecaster": "none",
        "prompt_format": "native",
        "prefill": True,
        "model_backend": "llama",
        "openai_json_system_prompt": True,
        "allow_nonstandard_window": False,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


class MetricsHelperTests(unittest.TestCase):
    def test_relaxed_json_payloads_prefer_solution_block_and_dedupe(self):
        text = (
            "before {bad json "
            "<SOLUTION>[{\"phase_id\": 0, \"final\": 30}]</SOLUTION> "
            "after [{\"phase_id\": 0, \"final\": 30}]"
        )

        payloads, errors = metrics.iter_relaxed_json_payloads(text)

        self.assertEqual(payloads[0], [{"phase_id": 0, "final": 30}])
        self.assertIn({"phase_id": 0, "final": 30}, payloads)
        self.assertTrue(errors)
        self.assertEqual(metrics.extract_solution_json_relaxed(text), (payloads[0], None))

    def test_normalize_solution_relaxed_accepts_aliases_and_numeric_strings(self):
        parsed = {
            "plan": [
                {"id": "0", "green_time": "35", "comment": "extra"},
                {"phase": 1.0, "duration": 20.0},
            ]
        }

        solution, error, actions = metrics.normalize_solution_relaxed(parsed)

        self.assertEqual(solution, {"0": 35, "1": 20})
        self.assertIsNone(error)
        self.assertIn("drop_extra_or_alias_fields", actions)
        self.assertIn("final_numeric_string_to_int", actions)

    def test_target_peak_route_selection_best_edges(self):
        pairs = [("short", "dst"), ("long", "dst"), ("mid", "wide_dst")]
        edge_info = {
            "short": {"length": 50.0, "lanes": 1},
            "mid": {"length": 150.0, "lanes": 1},
            "long": {"length": 400.0, "lanes": 2},
            "dst": {"length": 80.0, "lanes": 1},
            "wide_dst": {"length": 120.0, "lanes": 3},
        }
        args = types.SimpleNamespace(
            target_peak_route_selection="best_edges",
            target_peak_min_source_length=0.0,
            target_peak_min_dest_length=0.0,
        )

        selected = metrics.select_target_peak_route_pairs(pairs, edge_info, args)

        self.assertEqual(selected[0], ("long", "dst"))
        self.assertCountEqual(selected, pairs)

    def test_target_peak_route_selection_diverse_sources_first(self):
        pairs = [("a", "dst1"), ("a", "dst2"), ("b", "dst1")]
        edge_info = {
            "a": {"length": 200.0, "lanes": 2},
            "b": {"length": 100.0, "lanes": 1},
            "dst1": {"length": 80.0, "lanes": 1},
            "dst2": {"length": 70.0, "lanes": 1},
        }
        args = types.SimpleNamespace(
            target_peak_route_selection="diverse_sources",
            target_peak_min_source_length=0.0,
            target_peak_min_dest_length=0.0,
        )

        selected = metrics.select_target_peak_route_pairs(pairs, edge_info, args)

        self.assertEqual(len({selected[0][0], selected[1][0]}), 2)
        self.assertCountEqual(selected, pairs)

    def test_validate_runtime_args_rejects_invalid_boundaries(self):
        for override in (
            {"warmup_seconds": -1},
            {"metric_seconds": 0},
            {"decision_interval_seconds": 0},
            {"min_green": 0},
            {"max_green": 9},
            {"demand_scale": 0},
            {"target_peak_vph_per_route": -1},
            {"target_peak_routes_per_tl": -1},
            {"target_peak_min_source_length": -1},
            {"target_peak_end": 10.0, "target_peak_begin": 10.0},
        ):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    metrics.validate_runtime_args(valid_args(**override))

    def test_validate_runtime_args_accepts_default_valid_args(self):
        metrics.validate_runtime_args(valid_args())


if __name__ == "__main__":
    unittest.main()
