"""The PR regression gate (eval/compare.py): fail on real drops, pass on noise."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from compare import compare, to_markdown  # noqa: E402

REG = {"max_drop": {"hit_rate@5": 0.02, "faithfulness": 0.03, "citation_coverage": 0.03, "refusal_accuracy": 0.05},
       "max_p95_latency_increase": 0.50}
METRICS = {"hit_rate@5": 0.93, "mrr@5": 0.7, "context_recall": 0.96, "faithfulness": 0.99, "answer_correctness": 0.87,
           "citation_coverage": 0.95, "refusal_accuracy": 1.0, "false_refusal_rate": 0.0, "p95_latency_ms": 3000}


def row(i, **kw):
    r = {"id": f"q{i:03d}", "question": f"question {i}?", "should_refuse": False, "hit_rank": 1, "refused": False,
         "faithfulness": 1.0, "citation_coverage": 1.0, "answer_correctness": 1.0}
    return r | kw


def report(metrics=None, rows=None, **kw):
    rep = {"metrics": {**METRICS, **(metrics or {})}, "rows": rows or [row(i) for i in range(5)], "gate_failures": [],
           "prompt_version": "answer_v2", "prompt_hash": "aaa", "config_hash": "c1", "git_sha": "g1", "runner": "github-actions",
           "n_questions": 5, "tickers": ["AAPL"], "model": "m", "judge": {"model": "j"}, "timestamp": "t"}
    return rep | kw


def test_noise_within_tolerance_passes():
    res = compare(report(), report(metrics={"faithfulness": 0.975, "citation_coverage": 0.93, "p95_latency_ms": 3500}), REG)
    assert res["failures"] == []


def test_drop_that_still_clears_the_absolute_floor_fails():
    # 0.95 -> 0.88: above an 0.85 floor, but a 7-point regression vs main
    res = compare(report(), report(metrics={"citation_coverage": 0.88}, prompt_version="answer_v3", prompt_hash="bbb"), REG)
    assert len(res["failures"]) == 1 and "citation_coverage" in res["failures"][0]
    md = to_markdown(report(), report(prompt_version="answer_v3"), res)
    assert "FAILED" in md and "`answer_v2` → **`answer_v3`**" in md


def test_losing_one_retrieval_hit_fails():
    res = compare(report(), report(metrics={"hit_rate@5": 0.893}), REG)  # 1 of 28 questions
    assert any("hit_rate@5" in f for f in res["failures"])


def test_latency_only_gated_on_the_same_kind_of_machine():
    slow = report(metrics={"p95_latency_ms": 6000})
    assert any("p95 latency" in f for f in compare(report(), slow, REG)["failures"])
    res = compare(report(runner="local-darwin"), slow, REG)
    assert res["failures"] == [] and any("not gated" in n for n in res["notes"])


def test_prompt_edited_without_version_bump_is_flagged():
    res = compare(report(), report(prompt_hash="changed"), REG)
    assert any("without bumping its version" in n for n in res["notes"])


def test_questions_that_got_worse_are_named():
    new_rows = [row(0, hit_rank=None, refused=True, faithfulness=None)] + [row(i) for i in range(1, 5)]
    res = compare(report(), report(rows=new_rows), REG)
    assert res["worse"][0]["id"] == "q000"
    assert "no longer retrieved" in res["worse"][0]["why"] and "now refused" in res["worse"][0]["why"]


def test_absolute_floor_still_applies_without_a_regression():
    res = compare(report(), report(gate_failures=["answer_correctness = 0.5 < threshold 0.6"]), REG)
    assert res["failures"] == ["below the absolute threshold: answer_correctness = 0.5 < threshold 0.6"]
