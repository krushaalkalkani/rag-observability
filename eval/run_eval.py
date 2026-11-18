"""End-to-end evaluation of the RAG system on the golden set.

For every golden question we run the real pipeline, then measure:

  Retrieval
    hit_rate@5         a correct source chunk is in the top 5 (our own metric)
    mrr@5              1/rank of the first correct chunk (rewards ranking it first)
    context_recall     RAGAS: can the reference answer be attributed to the retrieved chunks?
  Generation
    faithfulness       RAGAS: share of the answer's claims supported by the retrieved chunks
                       (the hallucination metric; only for answers that were not refused)
    answer_correctness RAGAS AnswerAccuracy vs the reference answer (a wrongly refused
                       answerable question scores 0)
    citation_coverage  share of the model's claim sentences with a valid, supported citation
                       (measured on the raw model output, BEFORE guardrails clean it up)
  Behaviour
    refusal_accuracy   refused exactly when it should
    false_refusal_rate answerable questions that were refused
    latency p50 / p95

    uv run python eval/run_eval.py                              # full golden set
    uv run python eval/run_eval.py --tickers AAPL TSLA NVDA     # subset (what CI runs)
    uv run python eval/run_eval.py --prompt answer_v1 --gate    # exit 1 if below thresholds

Writes eval/reports/eval_<name>.json and .md
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rag.config import load_config  # noqa: E402
from rag.guardrails import CITATION_GROUP_RE  # noqa: E402
from rag.pipeline import answer_question  # noqa: E402
from rag.retrieve import retrieve  # noqa: E402

GATED = ["faithfulness", "hit_rate@5", "refusal_accuracy", "citation_coverage"]


# ---------------------------------------------------------------- judge (RAGAS)
class UsageCounter:
    """Wraps the Anthropic client so we can report what the judge cost."""

    def __init__(self, client):
        self.input_tokens = self.output_tokens = self.calls = 0
        original = client.messages.create

        async def counted(*args, **kwargs):
            resp = await original(*args, **kwargs)
            self.calls += 1
            self.input_tokens += resp.usage.input_tokens
            self.output_tokens += resp.usage.output_tokens
            return resp

        client.messages.create = counted


def make_judge(model: str, effort: str):
    import instructor
    from anthropic import AsyncAnthropic
    from ragas.llms import llm_factory

    client = AsyncAnthropic()
    usage = UsageCounter(client)
    # JSON mode (not forced tool calls): Opus 5.5 rejects forced tool_choice.
    llm = llm_factory(
        model, provider="anthropic", client=client, mode=instructor.Mode.ANTHROPIC_JSON,
        max_tokens=8000, output_config={"effort": effort},
    )
    return llm, usage


async def judge_all(rows: list[dict], cfg: dict) -> dict:
    from ragas.metrics.collections import AnswerAccuracy, ContextRecall, Faithfulness

    llm, usage = make_judge(cfg["judge_model"], cfg["judge_effort"])
    faithfulness, accuracy, recall = Faithfulness(llm=llm), AnswerAccuracy(llm=llm), ContextRecall(llm=llm)
    sem = asyncio.Semaphore(cfg["judge_concurrency"])
    errors = []

    async def score(row, name, coro):
        async with sem:
            try:
                row[name] = round(float((await coro).value), 3)
            except Exception as e:  # a judge failure must not crash the whole eval
                row[name] = None
                errors.append(f"{row['id']} {name}: {type(e).__name__}: {str(e)[:150]}")

    tasks = []
    for row in rows:
        if row["should_refuse"]:
            continue
        ctx, ref, q = row["contexts"], row["reference_answer"], row["question"]
        tasks.append(score(row, "context_recall", recall.ascore(user_input=q, retrieved_contexts=ctx, reference=ref)))
        if row["refused"]:
            row["answer_correctness"] = 0.0  # refusing an answerable question = wrong answer
            continue
        answer = CITATION_GROUP_RE.sub("", row["answer"]).strip()  # judge the text, not the ids
        tasks.append(score(row, "faithfulness", faithfulness.ascore(user_input=q, response=answer, retrieved_contexts=ctx)))
        tasks.append(score(row, "answer_correctness", accuracy.ascore(user_input=q, response=answer, reference=ref)))
    await asyncio.gather(*tasks)
    return {"calls": usage.calls, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens, "errors": errors}


# ---------------------------------------------------------------- run + aggregate
def run_pipeline(golden: list[dict], args) -> list[dict]:
    rows = []
    for i, g in enumerate(golden, 1):
        r = answer_question(g["question"], mode=args.mode, enforce=False if args.no_guardrails else None,
                            prompt_version=args.prompt, log=False)
        ids = [c["chunk_id"] for c in r["retrieved"]]
        rank = next((k + 1 for k, cid in enumerate(ids) if cid in g["source_chunk_ids"]), None)
        rows.append({
            **{k: g[k] for k in ("id", "question", "reference_answer", "should_refuse", "source_chunk_ids")},
            "answer": r["answer"], "refused": r["refused"], "refusal_reason": r["refusal_reason"],
            "citations": [c["chunk_id"] for c in r["citations"]], "retrieved": ids, "hit_rank": rank,
            "citation_coverage": r["citation_coverage"], "latency_ms": r["latency_ms"], "contexts": r["contexts"],
            "prompt_version": r["prompt_version"], "model": r["model"], "retrieval_mode": r["retrieval_mode"],
        })
        print(f"  [{i:2d}/{len(golden)}] {g['id']} refused={r['refused']!s:5} rank={rank}  {g['question'][:60]}", flush=True)
    return rows


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def aggregate(rows: list[dict]) -> dict:
    ans = [r for r in rows if not r["should_refuse"]]
    answered = [r for r in ans if not r["refused"]]
    lat = sorted(r["latency_ms"] for r in rows)
    return {
        "hit_rate@5": mean([r["hit_rank"] is not None for r in ans]),
        "mrr@5": mean([1 / r["hit_rank"] if r["hit_rank"] else 0.0 for r in ans]),
        "context_recall": mean([r.get("context_recall") for r in ans]),
        "faithfulness": mean([r.get("faithfulness") for r in answered]),
        "answer_correctness": mean([r.get("answer_correctness") for r in ans]),
        "citation_coverage": mean([r["citation_coverage"] for r in answered]),
        "refusal_accuracy": mean([r["refused"] == r["should_refuse"] for r in rows]),
        "false_refusal_rate": mean([r["refused"] for r in ans]),
        "p50_latency_ms": lat[len(lat) // 2],
        "p95_latency_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))],
    }


def check_gate(metrics: dict, thresholds: dict) -> list[str]:
    return [f"{k} = {metrics[k]} < threshold {t}" for k, t in thresholds.items() if metrics.get(k) is None or metrics[k] < t]


# ---------------------------------------------------------------- report
def pct(x):
    return "-" if x is None else f"{x:.0%}"


def to_markdown(rep: dict) -> str:
    m, t = rep["metrics"], rep["thresholds"]
    status = "PASSED" if not rep["gate_failures"] else "FAILED"
    lines = [
        f"# Eval report `{rep['name']}` - {rep['timestamp']}", "",
        f"**Gate: {status}**" + ("" if not rep["gate_failures"] else " - " + "; ".join(rep["gate_failures"])), "",
        f"- Questions: {rep['n_questions']} ({rep['n_answerable']} answerable, {rep['n_questions'] - rep['n_answerable']} must-refuse)"
        + (f", tickers {', '.join(rep['tickers'])}" if rep["tickers"] else ""),
        f"- Prompt `{rep['prompt_version']}` · model `{rep['model']}` · retrieval `{rep['retrieval_mode']}` · guardrails {rep['guardrails']}",
        f"- Judge `{rep['judge']['model']}` (effort {rep['judge']['effort']}): {rep['judge']['calls']} calls, "
        f"{rep['judge']['input_tokens']:,} in / {rep['judge']['output_tokens']:,} out tokens, {len(rep['judge']['errors'])} errors", "",
        "| Metric | Value | Threshold |", "|---|---|---|",
    ]
    for k in ["hit_rate@5", "mrr@5", "context_recall", "faithfulness", "answer_correctness", "citation_coverage",
              "refusal_accuracy", "false_refusal_rate"]:
        v = m[k]
        shown = f"{v:.2f}" if k == "mrr@5" and v is not None else pct(v)
        mark = "" if k not in t else (" ✅" if v is not None and v >= t[k] else " ❌")
        lines.append(f"| {k} | {shown}{mark} | {pct(t[k]) if k in t else ''} |")
    lines += [f"| p50 / p95 latency | {m['p50_latency_ms']} / {m['p95_latency_ms']} ms | |", "",
              "## Per question", "",
              "| id | question | hit rank | refused | faithful | correct | cited |", "|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        exp = " (should)" if r["should_refuse"] else ""
        wrong = "" if r["refused"] == r["should_refuse"] else " ⚠️"
        lines.append(f"| {r['id']} | {r['question'][:70]} | {r['hit_rank'] or '-'} | {'yes' if r['refused'] else 'no'}{exp}{wrong} | "
                     f"{pct(r.get('faithfulness'))} | {pct(r.get('answer_correctness'))} | {pct(r['citation_coverage'])} |")
    if rep["judge"]["errors"]:
        lines += ["", "## Judge errors", ""] + [f"- {e}" for e in rep["judge"]["errors"]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tickers", nargs="+", help="only questions about these companies (+ all must-refuse questions)")
    parser.add_argument("--prompt", help="prompt version to evaluate (default: config)")
    parser.add_argument("--mode", help="retrieval mode (default: config)")
    parser.add_argument("--no-guardrails", action="store_true")
    parser.add_argument("--limit", type=int, help="first N questions only (quick smoke test)")
    parser.add_argument("--name", help="report name (default: timestamp)")
    parser.add_argument("--gate", action="store_true", help="exit with code 1 if any metric is below its threshold")
    args = parser.parse_args()

    cfg = load_config()
    golden = [json.loads(line) for line in open(ROOT / "eval" / "golden_set.jsonl")]
    if args.tickers:
        keep = {t.upper() for t in args.tickers}
        golden = [g for g in golden if g["should_refuse"] or g["source_chunk_ids"][0].split("-")[0] in keep]
    golden = golden[: args.limit] if args.limit else golden

    print(f"Running pipeline on {len(golden)} questions")
    retrieve("warm up", "hybrid_rerank")  # load models before timing anything
    t0 = time.time()
    rows = run_pipeline(golden, args)
    print(f"Judging with {cfg['eval']['judge_model']} ...", flush=True)
    judge = asyncio.run(judge_all(rows, cfg["eval"]))

    metrics = aggregate(rows)
    thresholds = cfg["eval"]["thresholds"]
    failures = check_gate(metrics, thresholds)
    for r in rows:
        r.pop("contexts")
    name = args.name or datetime.now().strftime("%Y%m%d-%H%M")
    report = {
        "name": name, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"), "duration_s": round(time.time() - t0),
        "tickers": args.tickers, "n_questions": len(rows), "n_answerable": sum(not r["should_refuse"] for r in rows),
        "prompt_version": rows[0]["prompt_version"], "model": next((r["model"] for r in rows if r["model"]), None),
        "retrieval_mode": rows[0]["retrieval_mode"], "guardrails": not args.no_guardrails,
        "judge": {"model": cfg["eval"]["judge_model"], "effort": cfg["eval"]["judge_effort"], **judge},
        "metrics": metrics, "thresholds": thresholds, "gate_failures": failures, "rows": rows,
    }
    out = ROOT / "eval" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"eval_{name}.json").write_text(json.dumps(report, indent=2))
    (out / f"eval_{name}.md").write_text(to_markdown(report))

    print("\n" + "\n".join(f"  {k:20s} {v}" for k, v in metrics.items()))
    print(f"  judge: {judge['calls']} calls, {judge['input_tokens']:,} in / {judge['output_tokens']:,} out tokens, {len(judge['errors'])} errors")
    print(f"Report: eval/reports/eval_{name}.md")
    if failures:
        print("GATE FAILED:\n  " + "\n  ".join(failures))
        if args.gate:
            sys.exit(1)
    else:
        print("Gate passed.")


if __name__ == "__main__":
    main()
