"""Tracing: record every step of every request in self-hosted Langfuse.

Vocabulary (same as OpenTelemetry, which Langfuse is built on):
- TRACE        = one user request, end to end ("rag-request").
- OBSERVATION  = one step inside it (a "span"). Steps nest, so a trace is a tree:

    rag-request                      question in, answer out, metadata, scores
     ├─ retrieval                    query -> final top-k chunks
     │   ├─ vector-search            chunk ids + cosine scores
     │   ├─ bm25-search              chunk ids + BM25 scores
     │   ├─ rrf-fusion               merged order (= order BEFORE re-ranking)
     │   └─ rerank                   order AFTER re-ranking, who moved where
     ├─ retrieval-gate               refuse early if nothing relevant?
     ├─ llm-generation               full prompt, output, tokens, cost, time-to-first-token
     └─ citation-check               per-sentence support check, coverage, refused?

Each observation records its own start/end time, so per-step latency is free.

Design rule: tracing must never change or break the pipeline. Without Langfuse
keys in the environment (unit tests, CI) every helper here is a no-op, and a
step called outside a trace (e.g. the API's warm-up `retrieve()`) is not traced.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
from functools import lru_cache
from typing import Any, Iterator

from rag.config import PROJECT_ROOT, load_config


class _Noop:
    """Stands in for a Langfuse observation when tracing is off."""

    id = None
    trace_id = None

    def update(self, **_: Any) -> "_Noop":
        return self

    def score_trace(self, **_: Any) -> None:
        pass


NOOP = _Noop()


@lru_cache
def get_langfuse():
    """The Langfuse client, or None if tracing is disabled / not configured."""
    if os.environ.get("RAG_TRACING", "1") == "0":
        return None
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    from langfuse import Langfuse
    from langfuse.span_filter import is_langfuse_span

    # Reads LANGFUSE_PUBLIC_KEY / SECRET_KEY / BASE_URL from the env.
    # Export ONLY our own spans. By default Langfuse also exports spans that other
    # libraries emit via OpenTelemetry - the Anthropic SDK emits one per API call, which
    # shows up as a second "generation" inside ours and double-counts cost (and prices
    # it wrong: it leaves out the thinking tokens that Haiku bills as output).
    return Langfuse(should_export_span=is_langfuse_span)


def _mark_error(obs, exc: Exception) -> None:
    obs.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}"[:500])


@contextlib.contextmanager
def trace_request(name: str, *, input: Any, user_id: str | None, session_id: str | None,
                  metadata: dict[str, str], tags: list[str]) -> Iterator[Any]:
    """Start a new trace (the root observation) for one request."""
    lf = get_langfuse()
    if lf is None:
        yield NOOP
        return
    from langfuse import propagate_attributes

    with lf.start_as_current_observation(name=name, as_type="chain", input=input, metadata=metadata) as root:
        # propagate_attributes copies these onto the trace AND every child step, so you
        # can filter traces by user/session/tag and group cost by prompt_version.
        with propagate_attributes(user_id=user_id, session_id=session_id, metadata=metadata,
                                  tags=tags, trace_name=name, version=metadata.get("prompt_version")):
            try:
                yield root
            except Exception as e:
                _mark_error(root, e)
                raise


@contextlib.contextmanager
def observe(name: str, as_type: str = "span", **kwargs: Any) -> Iterator[Any]:
    """Record one pipeline step as a child of the current trace (no-op outside a trace)."""
    lf = get_langfuse()
    if lf is None or lf.get_current_trace_id() is None:
        yield NOOP
        return
    with lf.start_as_current_observation(name=name, as_type=as_type, **kwargs) as obs:
        try:
            yield obs
        except Exception as e:
            _mark_error(obs, e)  # the step shows up red in the UI with the error message
            raise


def flush() -> None:
    """Send buffered events now. The SDK batches in the background; short-lived
    programs (the CLI, scripts) must flush before exiting or traces get lost."""
    lf = get_langfuse()
    if lf is not None:
        lf.flush()


def trace_url(trace_id: str | None) -> str | None:
    lf = get_langfuse()
    if lf is None or not trace_id:
        return None
    try:
        return lf.get_trace_url(trace_id=trace_id)
    except Exception:  # needs a round-trip to the server; never let it break a request
        return None


def record_feedback(trace_id: str, thumbs_up: bool, comment: str | None = None) -> bool:
    """Store a user's 👍/👎 as a Langfuse score on the trace."""
    lf = get_langfuse()
    if lf is None:
        return False
    lf.create_score(name="user_feedback", value=1 if thumbs_up else 0, data_type="BOOLEAN",
                    trace_id=trace_id, comment=comment)
    return True


# ---------- metadata: what exactly produced this answer? ----------
# Sections of config.yaml that change the system's behaviour. Paths and logging
# are left out: they differ between machines but don't change answers.
_BEHAVIOUR_SECTIONS = ("chunking", "embedding", "retrieval", "reranker", "guardrails", "llm", "prompt")


def config_hash(cfg: dict | None = None) -> str:
    """Short fingerprint of the behaviour-relevant config. Same hash = same settings,
    so two traces with different hashes were produced by different configs."""
    cfg = cfg or load_config()
    relevant = {k: cfg.get(k) for k in _BEHAVIOUR_SECTIONS}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, default=str).encode()).hexdigest()[:12]


@lru_cache
def git_sha() -> str:
    """Commit of the code that produced the answer ("+dirty" = uncommitted changes)."""
    if sha := os.environ.get("GIT_SHA") or os.environ.get("GITHUB_SHA"):
        return sha[:12]
    try:
        sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=PROJECT_ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=PROJECT_ROOT,
                               capture_output=True, text=True).stdout.strip()
        return sha + ("+dirty" if dirty else "")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


# ---------- cost ----------
def estimate_cost(model: str, input_tokens: int | None, output_tokens: int | None) -> dict[str, float] | None:
    """Dollar cost from the pricing table in config.yaml (USD per 1M tokens).
    Local models are listed at $0 - free in dollars, but tokens are still tracked."""
    price = load_config().get("pricing", {}).get(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    cost_in = input_tokens * price["input"] / 1_000_000
    cost_out = output_tokens * price["output"] / 1_000_000
    return {"input": round(cost_in, 8), "output": round(cost_out, 8), "total": round(cost_in + cost_out, 8)}


# ---------- compact chunk summaries for span inputs/outputs ----------
def chunk_summary(c: dict, rank: int | None = None) -> dict:
    """What a human needs to see about a retrieved chunk in the trace view: which chunk,
    where it's from, its scores, and the first words of its text (the full text is in
    the LLM prompt of the llm-generation step)."""
    out = {"rank": rank} if rank is not None else {}
    out |= {"chunk_id": c["chunk_id"], "ticker": c.get("ticker"), "section": c.get("section")}
    out |= {k: v for k, v in c.items() if k.endswith("_score") and v is not None}
    out["preview"] = " ".join(c.get("text", "").split())[:160]
    return out
