"""Ablation: how much does each Phase 2 component actually help?

Part A - retrieval only (free, no LLM): for each retrieval mode, did a correct
         source chunk land in the top 5?  -> hit rate@5, MRR@5, latency.
Part B - full pipeline (calls the LLM): refusal accuracy, citation coverage,
         whether the answer cites a correct source, end-to-end latency.

    uv run python eval/ablation.py              # Part A + Part B
    uv run python eval/ablation.py --retrieval-only

Writes eval/reports/ablation_<timestamp>.json and .md
"""

import argparse
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

from rag.pipeline import answer_question
from rag.retrieve import retrieve

ROOT = Path(__file__).resolve().parents[1]
MODES = ["vector", "bm25", "hybrid", "hybrid_rerank"]
SETUPS = [  # (label, retrieval mode, guardrails on?)
    ("Vector only", "vector", False),
    ("Hybrid (BM25 + vector)", "hybrid", False),
    ("Hybrid + re-rank", "hybrid_rerank", False),
    ("+ citation enforcement", "hybrid_rerank", True),
]


def p50(xs):
    return round(statistics.median(xs)) if xs else None


def retrieval_eval(golden):
    answerable = [g for g in golden if not g["should_refuse"]]
    rows, per_question = {}, {}
    for mode in MODES:
        hits, rr, lat = [], [], []
        for g in answerable:
            t = time.perf_counter()
            ids = [c["chunk_id"] for c in retrieve(g["question"], mode)]
            lat.append((time.perf_counter() - t) * 1000)
            rank = next((i + 1 for i, cid in enumerate(ids) if cid in g["source_chunk_ids"]), None)
            hits.append(rank is not None)
            rr.append(1 / rank if rank else 0.0)
            per_question.setdefault(g["id"], {})[mode] = rank
        rows[mode] = {"hit_rate@5": round(sum(hits) / len(hits), 3), "mrr@5": round(sum(rr) / len(rr), 3), "p50_ms": p50(lat)}
        print(f"  {mode:14s} {rows[mode]}")

    # Best re-rank score per question: used to calibrate the refusal gate.
    gate = [
        {"id": g["id"], "should_refuse": g["should_refuse"], "best_rerank": retrieve(g["question"], "hybrid_rerank")[0]["rerank_score"]}
        for g in golden
    ]
    return rows, per_question, gate


def pipeline_eval(golden):
    rows, details = {}, {}
    for label, mode, guard in SETUPS:
        correct_refusal, coverage, cites_source, lat, out = [], [], [], [], []
        for g in golden:
            r = answer_question(g["question"], mode=mode, enforce=guard, log=False)
            correct_refusal.append(r["refused"] == g["should_refuse"])
            lat.append(r["latency_ms"])
            if not g["should_refuse"] and not r["refused"]:
                if r["citation_coverage"] is not None:
                    coverage.append(r["citation_coverage"])
                cited = {c["chunk_id"] for c in r["citations"]}
                cites_source.append(bool(cited & set(g["source_chunk_ids"])))
            out.append({"id": g["id"], "refused": r["refused"], "reason": r["refusal_reason"], "answer": r["answer"],
                        "citations": [c["chunk_id"] for c in r["citations"]], "coverage": r["citation_coverage"]})
        n_ans = sum(not g["should_refuse"] for g in golden)
        rows[label] = {
            "refusal_accuracy": round(sum(correct_refusal) / len(golden), 3),
            "answered_rate": round(len(cites_source) / n_ans, 3),
            "cites_correct_source": round(sum(cites_source) / n_ans, 3),
            "citation_coverage": round(statistics.mean(coverage), 3) if coverage else None,
            "p50_latency_ms": p50(lat),
        }
        details[label] = out
        print(f"  {label:26s} {rows[label]}")
    return rows, details


def to_markdown(report):
    lines = [f"# Ablation report - {report['timestamp']}", "",
             f"Golden set: {report['n_questions']} questions ({report['n_answerable']} answerable, "
             f"{report['n_questions'] - report['n_answerable']} should-refuse). Prompt: `{report['prompt_version']}`.", "",
             "## A. Retrieval only (answerable questions)", "",
             "| Mode | Hit rate@5 | MRR@5 | p50 latency (ms) |", "|---|---|---|---|"]
    for mode, r in report["retrieval"].items():
        lines.append(f"| {mode} | {r['hit_rate@5']:.0%} | {r['mrr@5']:.2f} | {r['p50_ms']} |")
    lines += ["", "Rank of the first correct chunk per question (- = not in top 5):", "",
              "| Question | " + " | ".join(MODES) + " |", "|---|" + "---|" * len(MODES)]
    for qid, ranks in report["retrieval_ranks"].items():
        lines.append(f"| {qid} | " + " | ".join(str(ranks[m] or "-") for m in MODES) + " |")
    if report.get("pipeline"):
        lines += ["", "## B. Full pipeline (all questions)", "",
                  "| Setup | Hit rate@5 | Refusal accuracy | Answered (of answerable) | Cites a correct source | Citation coverage | p50 latency (ms) |",
                  "|---|---|---|---|---|---|---|"]
        hit = {lab: report["retrieval"][mode]["hit_rate@5"] for lab, mode, _ in SETUPS}
        for label, r in report["pipeline"].items():
            cov = f"{r['citation_coverage']:.0%}" if r["citation_coverage"] is not None else "-"
            lines.append(f"| {label} | {hit[label]:.0%} | {r['refusal_accuracy']:.0%} | {r['answered_rate']:.0%} | "
                         f"{r['cites_correct_source']:.0%} | {cov} | {r['p50_latency_ms']} |")
    lines += ["", "## Refusal gate calibration (best re-rank score)", "",
              "| Question | Should refuse | Best re-rank score |", "|---|---|---|"]
    for g in sorted(report["gate"], key=lambda x: x["best_rerank"]):
        lines.append(f"| {g['id']} | {'yes' if g['should_refuse'] else 'no'} | {g['best_rerank']:.4f} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--retrieval-only", action="store_true")
    args = parser.parse_args()

    golden = [json.loads(line) for line in open(ROOT / "eval" / "golden_set.jsonl")]
    from rag.generate import load_prompt

    report = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"), "n_questions": len(golden),
              "n_answerable": sum(not g["should_refuse"] for g in golden), "prompt_version": load_prompt()["version"]}
    retrieve("warm up")  # load models first so they don't distort the latency numbers
    print("A. Retrieval")
    report["retrieval"], report["retrieval_ranks"], report["gate"] = retrieval_eval(golden)
    if not args.retrieval_only:
        print("B. Full pipeline")
        report["pipeline"], report["pipeline_details"] = pipeline_eval(golden)

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out = ROOT / "eval" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"ablation_{stamp}.json").write_text(json.dumps(report, indent=2))
    (out / f"ablation_{stamp}.md").write_text(to_markdown(report))
    print(f"Saved eval/reports/ablation_{stamp}.md")


if __name__ == "__main__":
    main()
