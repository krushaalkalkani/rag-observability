"""Cross-encoder re-ranking.

Bi-encoder (our embedding model): encodes the query and each chunk SEPARATELY
into vectors, then compares vectors. Fast - chunk vectors are precomputed - but
the query and chunk never "see" each other, so fine detail gets lost.

Cross-encoder (this file): reads the query and ONE chunk TOGETHER in a single
pass and outputs a relevance score. Much more accurate, but it must run once
per (query, chunk) pair at question time, so we only use it on the ~20
candidates the fast retrievers already found. Retrieve wide, re-rank narrow.
"""

from functools import lru_cache

from rag import tracing
from rag.config import load_config
from rag.index import embedding_text


@lru_cache
def get_reranker():
    from sentence_transformers import CrossEncoder

    cfg = load_config()["reranker"]
    return CrossEncoder(cfg["model"], max_length=cfg["max_length"])


def rerank(query: str, chunks: list[dict], top_n: int) -> list[dict]:
    if not chunks:
        return []
    before = {c["chunk_id"]: i for i, c in enumerate(chunks, 1)}  # rank after RRF fusion
    with tracing.observe("rerank", input={"query": query, "candidates": list(before), "top_n": top_n},
                         metadata={"model": load_config()["reranker"]["model"]}) as span:
        # Score is a probability-like 0-1 relevance (sigmoid of the model's output).
        scores = get_reranker().predict([(query, embedding_text(c)) for c in chunks], show_progress_bar=False)
        ranked = [{**c, "rerank_score": round(float(s), 4)} for c, s in zip(chunks, scores)]
        ranked.sort(key=lambda c: c["rerank_score"], reverse=True)
        # Before -> after for every candidate: shows exactly what the re-ranker promoted or dropped.
        span.update(output=[
            {"new_rank": i, "old_rank": before[c["chunk_id"]], "moved": before[c["chunk_id"]] - i,
             "chunk_id": c["chunk_id"], "rerank_score": c["rerank_score"], "kept": i <= top_n}
            for i, c in enumerate(ranked, 1)
        ])
    return ranked[:top_n]
