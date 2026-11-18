"""The full question -> answer flow, shared by the CLI, the API and the eval scripts.

    retrieve -> (retrieval gate) -> generate -> (citation enforcement) -> log

Every call is one Langfuse trace (see rag/tracing.py for the tree of steps).
"""

import json
import time
from datetime import datetime, timezone

from rag import tracing
from rag.config import PROJECT_ROOT, load_config
from rag.generate import extract_citations, generate_answer, load_prompt
from rag.guardrails import REFUSAL, enforce_citations
from rag.index import embedding_text
from rag.retrieve import retrieve


def answer_question(
    question: str,
    mode: str | None = None,
    enforce: bool | None = None,
    prompt_version: str | None = None,
    log: bool = True,
    user_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    cfg = load_config()
    mode = mode or cfg["retrieval"]["mode"]
    guard_cfg = cfg["guardrails"]
    enforce = guard_cfg["enabled"] if enforce is None else enforce
    prompt_version = load_prompt(prompt_version)["version"]

    # Metadata = "what exactly produced this answer?" Attached to the trace and every step,
    # so traces can be filtered/compared by prompt version, config or code commit.
    metadata = {
        "prompt_version": prompt_version,
        "config_hash": tracing.config_hash(cfg),
        "git_sha": tracing.git_sha(),
        "retrieval_mode": mode,
        "guardrails": str(enforce).lower(),
    }
    with tracing.trace_request(
        "rag-request", input={"question": question}, user_id=user_id, session_id=session_id,
        metadata=metadata, tags=[prompt_version, mode],
    ) as root:
        t0 = time.perf_counter()
        chunks = retrieve(question, mode)
        t1 = time.perf_counter()

        # Layer 1 guardrail: nothing relevant retrieved -> refuse, skip the LLM call entirely.
        best = chunks[0].get("rerank_score") if chunks else None
        threshold = guard_cfg["min_rerank_score"]
        blocked = enforce and (not chunks or (best is not None and best < threshold))
        with tracing.observe("retrieval-gate", as_type="guardrail",
                             input={"best_rerank_score": best, "min_rerank_score": threshold, "enabled": enforce}) as gate:
            gate.update(output={"passed": not blocked})

        guard, gen = None, None
        if blocked:
            answer, model, refused, reason = REFUSAL, None, True, "low_retrieval_score"
        else:
            gen = generate_answer(question, chunks, prompt_version)
            answer, model = gen["answer"], gen["model"]
            # Layer 2+3: per-sentence support check (always computed, for metrics).
            with tracing.observe("citation-check", as_type="guardrail", input={"answer": answer, "enforced": enforce}) as chk:
                guard = enforce_citations(answer, chunks, guard_cfg)
                checks = guard["checks"]
                chk.update(
                    output={
                        "refused": guard["refused"],
                        "refusal_reason": guard["refusal_reason"],
                        "citation_coverage": guard["citation_coverage"],
                        "sentences_checked": len(checks),
                        "sentences_supported": sum(c["supported"] for c in checks),
                        "dropped": [{"sentence": c["sentence"], "reason": c["reason"]} for c in checks if not c["supported"]],
                        "final_answer": guard["answer"],
                    },
                    level="WARNING" if guard["refused"] else None,
                )
            if enforce:
                answer, refused, reason = guard["answer"], guard["refused"], guard["refusal_reason"]
            else:
                refused = answer.strip() == REFUSAL
                reason = "model_refused" if refused else None
        t2 = time.perf_counter()

        citations = [] if refused else extract_citations(answer, chunks)
        coverage = guard["citation_coverage"] if guard else None
        root.update(
            output={"answer": answer, "refused": refused, "refusal_reason": reason,
                    "citations": [c["chunk_id"] for c in citations], "citation_coverage": coverage},
            metadata={**metadata, "latency_ms": round((t2 - t0) * 1000), "retrieval_ms": round((t1 - t0) * 1000)},
        )
        # Trace-level scores: numbers Langfuse can filter, chart and average across traces.
        root.score_trace(name="refused", value=1 if refused else 0, data_type="BOOLEAN",
                         comment=reason)
        if coverage is not None:
            root.score_trace(name="citation_coverage", value=coverage, data_type="NUMERIC")

    result = {
        "question": question,
        "answer": answer,
        "citations": citations,
        "refused": refused,
        "refusal_reason": reason,
        "prompt_version": prompt_version,
        "model": model,
        "retrieval_mode": mode,
        "guardrails": enforce,
        "latency_ms": round((t2 - t0) * 1000),
        "retrieval_ms": round((t1 - t0) * 1000),
        "ttft_ms": gen["ttft_ms"] if gen else None,
        "input_tokens": gen["input_tokens"] if gen else None,
        "output_tokens": gen["output_tokens"] if gen else None,
        "cost_usd": gen["cost_usd"] if gen else 0.0,
        "best_rerank_score": best,
        "citation_coverage": coverage,
        "sentence_checks": guard["checks"] if guard else [],
        "config_hash": metadata["config_hash"],
        "git_sha": metadata["git_sha"],
        "trace_id": root.trace_id,  # None when tracing is off
        # full text of what the LLM saw - needed by the evaluator (RAGAS), not logged
        "contexts": [embedding_text(c) for c in chunks],
        "retrieved": [
            {k: c.get(k) for k in ("chunk_id", "section", "vector_score", "bm25_score", "rrf_score", "rerank_score")}
            for c in chunks
        ],
    }
    if log:
        _log(result)
    return result


def _log(result: dict) -> None:
    """Append every answer to a JSONL log, so any answer can be traced to the
    exact prompt version, retrieval mode and chunks that produced it."""
    path = PROJECT_ROOT / load_config()["logging"]["answers_log"]
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **result}
    entry["citations"] = [c["chunk_id"] for c in result["citations"]]
    entry.pop("contexts", None)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
