"""The CI gate must fail on any metric below threshold - including a missing metric."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("run_eval", Path(__file__).parents[1] / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_eval)

THRESHOLDS = {"faithfulness": 0.85, "hit_rate@5": 0.80}


def test_gate_passes_when_all_metrics_meet_thresholds():
    assert run_eval.check_gate({"faithfulness": 0.9, "hit_rate@5": 0.8}, THRESHOLDS) == []


def test_gate_fails_on_low_or_missing_metric():
    failures = run_eval.check_gate({"faithfulness": 0.6, "hit_rate@5": None}, THRESHOLDS)
    assert len(failures) == 2  # a metric the judge failed to compute must not silently pass


def test_aggregate_counts_wrong_refusals():
    rows = [
        {"should_refuse": False, "refused": True, "hit_rank": None, "citation_coverage": None, "latency_ms": 10},
        {"should_refuse": False, "refused": False, "hit_rank": 2, "citation_coverage": 1.0, "latency_ms": 20,
         "faithfulness": 1.0, "answer_correctness": 1.0, "context_recall": 1.0},
        {"should_refuse": True, "refused": True, "hit_rank": None, "citation_coverage": None, "latency_ms": 5},
    ]
    m = run_eval.aggregate(rows)
    assert m["hit_rate@5"] == 0.5 and m["mrr@5"] == 0.25
    assert m["false_refusal_rate"] == 0.5 and round(m["refusal_accuracy"], 2) == 0.67
    assert m["faithfulness"] == 1.0  # only answered questions count for faithfulness
