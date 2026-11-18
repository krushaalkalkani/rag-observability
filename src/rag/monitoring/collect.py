"""Read finished traces from Langfuse and flatten each into one `requests` row.

A trace is a tree of steps (see rag/tracing.py). For metrics we only need a few
numbers per request: how long each step took, tokens and cost, whether it was
refused or failed, and its citation coverage. This module turns the tree into
that flat row.

Langfuse v4 serves observations (steps), not whole traces, so we read:
  1. the root steps ("rag-request") that started in the time window - with their
     input/output (question, answer, refused, coverage) and metadata;
  2. all steps of those traces - for per-step latency, tokens, cost, errors.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta

ROOT = "rag-request"
STEP_COLUMNS = {  # span name -> requests column with its duration
    "retrieval": "retrieval_ms",
    "vector-search": "vector_ms",
    "bm25-search": "bm25_ms",
    "rerank": "rerank_ms",
    "llm-generation": "generation_ms",
}


def _val(x):
    return getattr(x, "value", x)  # enums (e.g. level) -> plain string


def _ms(seconds: float | None) -> float | None:
    return round(seconds * 1000, 1) if seconds is not None else None


def parse_json(x):
    if isinstance(x, str):
        try:
            return json.loads(x)
        except json.JSONDecodeError:
            return x
    return x


def iter_observations(lf, **filters):
    for page in lf.iter_observation_pages(limit=1000, **filters):
        for o in page.data:
            yield o.dict()  # camelCase keys, as in the Langfuse API docs


def collect(lf, start: datetime, end: datetime, environment: str) -> list[dict]:
    """One row per trace whose root step started in [start, end) and has finished."""
    roots = {
        o["traceId"]: o
        for o in iter_observations(lf, name=ROOT, environment=[environment], from_start_time=start, to_start_time=end,
                       fields="core,basic,time,io,metadata")
        if o["endTime"] is not None  # still running: pick it up next time
    }
    if not roots:
        return []

    # Child steps start after their root, so look a little past `end` for them.
    steps = defaultdict(list)
    for o in iter_observations(lf, environment=[environment], from_start_time=start, to_start_time=end + timedelta(minutes=10),
                   fields="core,basic,time,usage,model"):
        if o["traceId"] in roots:
            steps[o["traceId"]].append(o)

    return [_row(root, steps[tid]) for tid, root in roots.items()]


def _row(root: dict, steps: list[dict]) -> dict:
    meta = root.get("metadata") or {}
    out = parse_json(root.get("output")) or {}
    out = out if isinstance(out, dict) else {}
    inp = parse_json(root.get("input")) or {}

    errors = [s for s in [root, *steps] if _val(s.get("level")) == "ERROR"]
    row = {
        "trace_id": root["traceId"],
        "ts": root["startTime"].isoformat(),
        "environment": root.get("environment"),
        "prompt_version": meta.get("prompt_version") or root.get("version"),
        "config_hash": meta.get("config_hash"),
        "git_sha": meta.get("git_sha"),
        "retrieval_mode": meta.get("retrieval_mode"),
        "session_id": root.get("sessionId") or None,
        "question": inp.get("question") if isinstance(inp, dict) else None,
        "error": int(bool(errors)),
        "error_message": next((e.get("statusMessage") for e in errors if e.get("statusMessage")), None),
        "refused": int(out["refused"]) if "refused" in out else None,
        "refusal_reason": out.get("refusal_reason"),
        "citation_coverage": out.get("citation_coverage"),
        "latency_ms": _ms(root.get("latency")),
    }
    for s in steps:
        if s["name"] in STEP_COLUMNS:
            row[STEP_COLUMNS[s["name"]]] = _ms(s.get("latency"))
        if s["name"] == "llm-generation":
            usage, cost = s.get("usageDetails") or {}, s.get("costDetails") or {}
            row |= {
                "ttft_ms": _ms(s.get("timeToFirstToken")),
                "input_tokens": usage.get("input"),
                "output_tokens": usage.get("output"),
                "cost_usd": cost.get("total"),
                "model": s.get("model"),
            }
    if row.get("cost_usd") is None and not row["error"]:
        row["cost_usd"] = 0.0  # refused before the LLM call: the request cost nothing
    return row
