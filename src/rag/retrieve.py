"""Retrieval: vector search, BM25 keyword search, and their fusion (hybrid).

Why two retrievers?
- Vector search matches MEANING: "head office" finds "headquarters".
- BM25 matches exact WORDS: "CET1", "MI308", "$2.5 billion" - rare terms and
  numbers that embeddings tend to blur together.
Each catches what the other misses, so we run both and merge the rankings.
"""

import re
from functools import lru_cache

import numpy as np
from rank_bm25 import BM25Okapi

from rag import tracing
from rag.config import load_config
from rag.index import embed_texts, embedding_text, get_all_chunks, get_collection

STOPWORDS = set(
    "a an and are as at be by did do does for from had has have how in is it its of on or "
    "our than that the their this to was we were what when where which who why will with".split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase words and numbers ("478,834", "14.6" stay whole); drop filler words."""
    return [t for t in re.findall(r"[a-z0-9]+(?:[.,][0-9]+)*", text.lower()) if t not in STOPWORDS]


# ---------- vector search ----------
def vector_search(query: str, k: int) -> list[dict]:
    with tracing.observe("vector-search", as_type="retriever", input={"query": query, "k": k}) as span:
        res = get_collection().query(
            query_embeddings=embed_texts([query], is_query=True), n_results=k, include=["documents", "metadatas", "distances"]
        )
        hits = [
            # Chroma returns cosine DISTANCE (0 = identical). Similarity = 1 - distance.
            {"chunk_id": cid, "text": text, "vector_score": round(1 - dist, 4), **meta}
            for cid, text, meta, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0])
        ]
        span.update(output=[{"rank": i, "chunk_id": c["chunk_id"], "vector_score": c["vector_score"]} for i, c in enumerate(hits, 1)])
    return hits


# ---------- BM25 keyword search ----------
@lru_cache
def get_bm25() -> tuple[BM25Okapi, list[dict]]:
    """Build the BM25 index in memory from the chunks stored in Chroma.

    Building from Chroma (instead of saving a separate file) means the keyword
    index can never get out of sync with the vector index. 2.4k chunks take <1s.
    """
    chunks = get_all_chunks()
    return BM25Okapi([tokenize(embedding_text(c)) for c in chunks]), chunks


def bm25_search(query: str, k: int) -> list[dict]:
    tokens = tokenize(query)
    with tracing.observe("bm25-search", as_type="retriever", input={"query": query, "tokens": tokens, "k": k}) as span:
        bm25, chunks = get_bm25()
        scores = bm25.get_scores(tokens)
        top = np.argsort(-scores)[:k]
        hits = [{**chunks[i], "bm25_score": round(float(scores[i]), 3)} for i in top if scores[i] > 0]
        span.update(output=[{"rank": i, "chunk_id": c["chunk_id"], "bm25_score": c["bm25_score"]} for i, c in enumerate(hits, 1)])
    return hits


# ---------- fusion ----------
def reciprocal_rank_fusion(rankings: list[list[dict]], k: int = 60) -> list[dict]:
    """RRF: score(chunk) = sum over rankings of 1 / (k + rank).

    Uses only RANK positions, never raw scores - so we can merge BM25 scores
    (unbounded, e.g. 23.7) with cosine similarities (0-1) without normalizing.
    A chunk ranked high by BOTH retrievers wins; k=60 dampens the gap between
    rank 1 and rank 2 so one retriever can't dominate.
    """
    fused: dict[str, dict] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            entry = fused.setdefault(chunk["chunk_id"], {**chunk, "rrf_score": 0.0})
            entry.update({key: v for key, v in chunk.items() if key.endswith("_score")})
            entry["rrf_score"] += 1.0 / (k + rank)
    return sorted(fused.values(), key=lambda c: c["rrf_score"], reverse=True)


def hybrid_search(query: str, k: int, rrf_k: int = 60) -> list[dict]:
    vec, bm = vector_search(query, k), bm25_search(query, k)
    with tracing.observe("rrf-fusion", input={"vector_ids": [c["chunk_id"] for c in vec],
                                              "bm25_ids": [c["chunk_id"] for c in bm], "rrf_k": rrf_k}) as span:
        fused = reciprocal_rank_fusion([vec, bm], k=rrf_k)
        in_both = {c["chunk_id"] for c in vec} & {c["chunk_id"] for c in bm}
        span.update(
            output=[{"rank": i, "chunk_id": c["chunk_id"], "rrf_score": round(c["rrf_score"], 5), "in_both": c["chunk_id"] in in_both}
                    for i, c in enumerate(fused, 1)],
            metadata={"vector_hits": len(vec), "bm25_hits": len(bm), "found_by_both": len(in_both)},
        )
    return fused


# ---------- entry point ----------
def retrieve(query: str, mode: str | None = None, top_k: int | None = None) -> list[dict]:
    cfg = load_config()["retrieval"]
    mode = mode or cfg["mode"]
    top_k = top_k or cfg["top_k"]

    with tracing.observe("retrieval", as_type="retriever", input={"query": query, "mode": mode, "top_k": top_k}) as span:
        chunks = _retrieve(query, mode, top_k, cfg)
        # The final chunks handed to the LLM, in order - the answer to "what was retrieved?"
        span.update(output=[tracing.chunk_summary(c, rank=i) for i, c in enumerate(chunks, 1)])
    return chunks


def _retrieve(query: str, mode: str, top_k: int, cfg: dict) -> list[dict]:
    n = cfg["candidates_per_retriever"]
    if mode == "vector":
        return vector_search(query, top_k)
    if mode == "bm25":
        return bm25_search(query, top_k)
    if mode == "hybrid":
        return hybrid_search(query, n, cfg["rrf_k"])[:top_k]
    if mode == "hybrid_rerank":
        from rag.rerank import rerank

        candidates = hybrid_search(query, n, cfg["rrf_k"])[: cfg["rerank_candidates"]]
        return rerank(query, candidates, top_k)
    raise ValueError(f"Unknown retrieval mode {mode!r}")
