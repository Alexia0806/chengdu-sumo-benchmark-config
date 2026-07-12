# Archived Launchers

This directory keeps historical one-off launch and watcher scripts from earlier remote benchmark rounds. They are retained for traceability only.

Current reproducible entry points live in `scripts/`:

- `run_chengdu_3tl_att_awt_relaxed_x1p8_matrix.sh`
- `run_chengdu_tls_short_probe_fixed_maxpressure.sh`
- `deepsignal_cycleplan_benchmark_chengdu_metrics.py`

Clean release packages exclude this archive through `.gitattributes` so old experiment workflows and result-oriented scripts do not leak into new benchmark bundles.
