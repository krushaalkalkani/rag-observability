"""Online evaluation: grade a sample of LIVE answers for faithfulness.

In production there is no reference answer, so "is it correct?" can't be checked.
"Is it faithful?" can: does every claim in the answer come from the retrieved
chunks? That's the hallucination metric, and an LLM judge can grade it with no
ground truth - the same RAGAS Faithfulness the offline eval (CI) uses, with the
same judge model and the same chunk text, so online and offline numbers compare.

Grading costs money (~$0.03 per answer), so only a sample is graded:
- sampling is decided by the trace id (a hash), so it's random across requests
  but stable: re-running the job never re-rolls the dice;
- only real answers are graded (refusals and errors have nothing to grade);
- the score goes to the local table AND back to Langfuse as a "faithfulness"
  score on the trace, so it's visible next to the request it grades.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from rag.guardrails import CITATION_GROUP_RE
from rag.monitoring.collect import ROOT, iter_observations, parse_json


def is_sampled(trace_id: str, rate: float) -> bool:
    """Deterministic coin flip: the first 8 hex digits of a trace id are uniformly random."""
    return int(trace_id[:8], 16) / 0xFFFFFFFF < rate


def mark_samples(conn, rate: float) -> int:
    """Decide, once per request, whether it will be graded."""
    rows = conn.execute("SELECT trace_id, error, refused FROM requests WHERE eval_status IS NULL").fetchall()
    n = 0
    for r in rows:
        if r["error"] or r["refused"] is None:
            status = "skipped"            # nothing to grade
        elif r["refused"]:
            status = "skipped"
        elif is_sampled(r["trace_id"], rate):
            status, n = "pending", n + 1
        else:
            status = "not_sampled"
        conn.execute("UPDATE requests SET eval_status=? WHERE trace_id=?", (status, r["trace_id"]))
    return n


def _load_case(lf, trace_id: str, ts: str) -> dict | None:
    """The question, the final answer and the exact chunks the LLM saw, rebuilt from the trace."""
    from rag.index import embedding_text, get_chunk

    start = datetime.fromisoformat(ts) - timedelta(minutes=1)
    steps = {o["name"]: o for o in iter_observations(lf, trace_id=trace_id, from_start_time=start, fields="core,basic,io")
             if o["name"] in (ROOT, "retrieval")}
    if ROOT not in steps or "retrieval" not in steps:
        return None
    root_in, root_out = parse_json(steps[ROOT].get("input")) or {}, parse_json(steps[ROOT].get("output")) or {}
    retrieved = parse_json(steps["retrieval"].get("output")) or []
    chunks = [get_chunk(c["chunk_id"]) for c in retrieved]
    if not root_out.get("answer") or not all(chunks):
        return None
    return {
        "question": root_in.get("question"),
        "answer": CITATION_GROUP_RE.sub("", root_out["answer"]).strip(),  # judge the text, not the ids
        "contexts": [embedding_text(c) for c in chunks],                  # same text the offline eval uses
    }


async def _grade(cases: dict[str, dict], cfg: dict) -> tuple[dict[str, float | None], dict]:
    from ragas.metrics.collections import Faithfulness

    from rag.judge import make_judge

    llm, usage = make_judge(cfg["judge_model"], cfg["judge_effort"])
    metric = Faithfulness(llm=llm)
    sem = asyncio.Semaphore(cfg["judge_concurrency"])
    scores: dict[str, float | None] = {}

    async def one(tid, case):
        async with sem:
            try:
                res = await metric.ascore(user_input=case["question"], response=case["answer"],
                                          retrieved_contexts=case["contexts"])
                scores[tid] = round(float(res.value), 3)
            except Exception as e:  # one judge failure must not stop the others
                print(f"  judge failed on {tid}: {type(e).__name__}: {str(e)[:120]}")
                scores[tid] = None

    await asyncio.gather(*(one(t, c) for t, c in cases.items()))
    return scores, {"calls": usage.calls, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}


def grade_pending(conn, lf, eval_cfg: dict, max_n: int) -> dict:
    """Grade up to `max_n` pending samples (oldest first). Returns a small report."""
    pending = conn.execute("SELECT trace_id, ts FROM requests WHERE eval_status='pending' ORDER BY ts LIMIT ?",
                           (max_n,)).fetchall()
    if not pending:
        return {"graded": 0}
    cases = {}
    for r in pending:
        case = _load_case(lf, r["trace_id"], r["ts"])
        if case is None:
            conn.execute("UPDATE requests SET eval_status='failed' WHERE trace_id=?", (r["trace_id"],))
        else:
            cases[r["trace_id"]] = case
    scores, usage = asyncio.run(_grade(cases, eval_cfg)) if cases else ({}, {})

    for tid, score in scores.items():
        if score is None:
            conn.execute("UPDATE requests SET eval_status='failed' WHERE trace_id=?", (tid,))
            continue
        conn.execute("UPDATE requests SET eval_status='done', faithfulness=? WHERE trace_id=?", (score, tid))
        lf.create_score(name="faithfulness", value=score, data_type="NUMERIC", trace_id=tid,
                        comment="online sample, RAGAS judge")
    from rag.tracing import estimate_cost

    cost = estimate_cost(eval_cfg["judge_model"], usage.get("input_tokens"), usage.get("output_tokens")) if usage else None
    done = [s for s in scores.values() if s is not None]
    return {"graded": len(done), "failed": len(pending) - len(done),
            "mean": round(sum(done) / len(done), 3) if done else None,
            "judge_cost_usd": cost["total"] if cost else 0.0}
