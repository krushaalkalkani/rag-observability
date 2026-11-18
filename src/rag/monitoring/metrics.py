"""Roll per-request rows up into metrics. The ONE place every metric is defined -
the stored metrics table, the alert check and the dashboard all call `summarize`,
so a number means the same thing everywhere.

Why p50 and p95 instead of the average? An average hides the slow tail: 19 fast
requests and one 30-second one average out to "fine". p95 = the latency that 95%
of requests beat, so it tells you how bad it is for the unlucky 1 in 20.
"""

from __future__ import annotations

import pandas as pd

from rag.monitoring.store import METRIC_COLUMNS

# Latency columns that get a p50 and a p95: (metric prefix, requests column)
LATENCIES = [("latency", "latency_ms"), ("retrieval", "retrieval_ms"), ("rerank", "rerank_ms"),
             ("generation", "generation_ms"), ("ttft", "ttft_ms")]


def _pct(s: pd.Series, q: float) -> float | None:
    s = s.dropna()
    return round(float(s.quantile(q)), 1) if len(s) else None


def _mean(s: pd.Series, digits: int = 4) -> float | None:
    s = s.dropna()
    return round(float(s.mean()), digits) if len(s) else None


def summarize(df: pd.DataFrame) -> dict:
    """All metrics for one group of requests."""
    ok = df[df["error"] == 0]                      # requests that produced an answer
    answered = ok[ok["refused"] == 0]              # ...that were not refusals
    graded = df["faithfulness"].dropna()
    out = {"requests": len(df)}
    for name, col in LATENCIES:
        out[f"{name}_p50_ms"], out[f"{name}_p95_ms"] = _pct(ok[col], 0.50), _pct(ok[col], 0.95)
    tokens = (ok["input_tokens"].fillna(0) + ok["output_tokens"].fillna(0))
    out |= {
        "cost_per_request_usd": _mean(ok["cost_usd"], 7),
        "cost_total_usd": round(float(ok["cost_usd"].fillna(0).sum()), 6),
        "tokens_per_request": _mean(tokens, 1),
        # citation coverage: average share of answer sentences that are backed by their source
        "citation_coverage": _mean(answered["citation_coverage"]),
        # fully grounded: share of answers where EVERY sentence was backed
        "fully_grounded_rate": round(float((answered["citation_coverage"] >= 0.999).mean()), 4) if len(answered) else None,
        "refusal_rate": round(float((ok["refused"] == 1).mean()), 4) if len(ok) else None,
        "error_rate": round(float((df["error"] == 1).mean()), 4) if len(df) else None,
        "faithfulness": round(float(graded.mean()), 4) if len(graded) else None,
        "faithfulness_n": len(graded),
    }
    return out


BUCKETS = {"5min": "5min", "15min": "15min", "1h": "1h", "6h": "6h", "1d": "1D"}


def to_frame(rows) -> pd.DataFrame:
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    return df


def bucketize(df: pd.DataFrame, bucket: str) -> pd.DataFrame:
    """Metrics per (time bucket, prompt_version), plus an "ALL" row per bucket."""
    if df.empty:
        return pd.DataFrame(columns=list(METRIC_COLUMNS))
    df = df.assign(bucket_start=df["ts"].dt.floor(BUCKETS[bucket]))
    rows = []
    for start, group in df.groupby("bucket_start"):
        rows.append({"bucket_start": start, "bucket": bucket, "prompt_version": "ALL", **summarize(group)})
        for version, sub in group.groupby("prompt_version", dropna=False):
            rows.append({"bucket_start": start, "bucket": bucket, "prompt_version": version or "unknown", **summarize(sub)})
    return pd.DataFrame(rows)[list(METRIC_COLUMNS)]
