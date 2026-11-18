"""Tracing helpers: must be harmless when Langfuse isn't configured, and the
metadata/cost numbers they attach must be right."""

import copy

import pytest

from rag import tracing
from rag.config import load_config


@pytest.fixture
def no_langfuse(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    tracing.get_langfuse.cache_clear()
    yield
    tracing.get_langfuse.cache_clear()


def test_tracing_is_a_noop_without_keys(no_langfuse):
    assert tracing.get_langfuse() is None
    with tracing.trace_request("t", input={}, user_id=None, session_id=None, metadata={}, tags=[]) as root:
        with tracing.observe("step") as step:
            step.update(output="anything")
        root.score_trace(name="x", value=1)
    assert root.trace_id is None
    assert tracing.record_feedback("0" * 32, True) is False
    tracing.flush()  # must not raise


def test_errors_still_propagate_through_noop(no_langfuse):
    with pytest.raises(ValueError):
        with tracing.observe("step"):
            raise ValueError("boom")


def test_config_hash_is_stable_and_tracks_behaviour_changes():
    cfg = load_config()
    assert tracing.config_hash(cfg) == tracing.config_hash(copy.deepcopy(cfg))
    changed = copy.deepcopy(cfg)
    changed["retrieval"]["top_k"] += 1
    assert tracing.config_hash(changed) != tracing.config_hash(cfg)
    moved = copy.deepcopy(cfg)
    moved["paths"]["chroma_dir"] = "/somewhere/else"  # not behaviour: same hash
    assert tracing.config_hash(moved) == tracing.config_hash(cfg)


def test_cost_uses_pricing_table_per_million_tokens():
    cost = tracing.estimate_cost("claude-haiku-5-5", 3_000, 400)
    assert cost == {"input": 0.0003, "output": 0.0002, "total": 0.0005}
    assert tracing.estimate_cost("llama3.2:3b", 3_000, 400)["total"] == 0.0
    assert tracing.estimate_cost("unknown-model", 10, 10) is None
    assert tracing.estimate_cost("claude-haiku-5-5", None, 10) is None


def test_chunk_summary_keeps_ids_and_scores_not_full_text():
    chunk = {"chunk_id": "TSLA-2025-00042", "ticker": "TSLA", "section": "Risk Factors", "text": "word " * 500,
             "vector_score": 0.8, "bm25_score": None, "rerank_score": 0.9}
    s = tracing.chunk_summary(chunk, rank=1)
    assert s["rank"] == 1 and s["chunk_id"] == "TSLA-2025-00042"
    assert s["vector_score"] == 0.8 and s["rerank_score"] == 0.9 and "bm25_score" not in s
    assert len(s["preview"]) <= 160
