"""SQLite storage for monitoring.

Three tables:
  requests     one row per request (trace) - the raw facts, copied from Langfuse.
               Keeping raw rows means any metric can be recomputed later, at any
               bucket size, and the dashboard can drill down to single requests.
  metrics      the "metrics table": requests rolled up per time bucket and
               prompt_version (plus an "ALL" row). This is what you'd keep long-term.
  alert_state  whether each alert rule is currently firing (so we notify on change,
               not on every run).
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from rag.config import load_config

# Columns of the requests table, in order. Times in ms, cost in USD.
REQUEST_COLUMNS = {
    "trace_id": "TEXT PRIMARY KEY",
    "ts": "TEXT NOT NULL",              # request start, ISO-8601 UTC
    "environment": "TEXT",
    "prompt_version": "TEXT",
    "config_hash": "TEXT",
    "git_sha": "TEXT",
    "retrieval_mode": "TEXT",
    "session_id": "TEXT",
    "question": "TEXT",
    "error": "INTEGER NOT NULL",        # 1 if any step failed
    "error_message": "TEXT",
    "refused": "INTEGER",
    "refusal_reason": "TEXT",
    "citation_coverage": "REAL",        # share of answer sentences backed by their citation
    "latency_ms": "REAL",               # end to end
    "retrieval_ms": "REAL",
    "vector_ms": "REAL",
    "bm25_ms": "REAL",
    "rerank_ms": "REAL",
    "generation_ms": "REAL",
    "ttft_ms": "REAL",
    "input_tokens": "INTEGER",
    "output_tokens": "INTEGER",
    "cost_usd": "REAL",
    "model": "TEXT",
    "eval_status": "TEXT",              # NULL = not sampled | pending | done | skipped | failed
    "faithfulness": "REAL",             # online LLM-judge score, sampled requests only
}

METRIC_COLUMNS = {
    "bucket_start": "TEXT NOT NULL",
    "bucket": "TEXT NOT NULL",          # bucket size, e.g. "1h"
    "prompt_version": "TEXT NOT NULL",  # or "ALL"
    "requests": "INTEGER",
    "latency_p50_ms": "REAL", "latency_p95_ms": "REAL",
    "retrieval_p50_ms": "REAL", "retrieval_p95_ms": "REAL",
    "rerank_p50_ms": "REAL", "rerank_p95_ms": "REAL",
    "generation_p50_ms": "REAL", "generation_p95_ms": "REAL",
    "ttft_p50_ms": "REAL", "ttft_p95_ms": "REAL",
    "cost_per_request_usd": "REAL", "cost_total_usd": "REAL",
    "tokens_per_request": "REAL",
    "citation_coverage": "REAL", "fully_grounded_rate": "REAL",
    "refusal_rate": "REAL", "error_rate": "REAL",
    "faithfulness": "REAL", "faithfulness_n": "INTEGER",
}


def db_path() -> Path:
    return load_config()["paths"]["metrics_db"]


@contextmanager
def connect(path: Path | None = None):
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _create(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _create(conn: sqlite3.Connection) -> None:
    cols = ", ".join(f"{k} {v}" for k, v in REQUEST_COLUMNS.items())
    conn.execute(f"CREATE TABLE IF NOT EXISTS requests ({cols})")
    conn.execute("CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts)")
    cols = ", ".join(f"{k} {v}" for k, v in METRIC_COLUMNS.items())
    conn.execute(f"CREATE TABLE IF NOT EXISTS metrics ({cols}, PRIMARY KEY (bucket_start, bucket, prompt_version))")
    conn.execute("CREATE TABLE IF NOT EXISTS alert_state (rule TEXT PRIMARY KEY, firing INTEGER, value REAL, since TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT)")


def upsert_requests(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Insert or update request rows, but never overwrite an existing online-eval result
    (a re-synced trace keeps the faithfulness score it already got)."""
    if not rows:
        return
    keys = list(REQUEST_COLUMNS)
    keep = {"eval_status", "faithfulness"}
    updates = ", ".join(f"{k}=excluded.{k}" for k in keys if k != "trace_id" and k not in keep)
    conn.executemany(
        f"INSERT INTO requests ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))}) "
        f"ON CONFLICT(trace_id) DO UPDATE SET {updates}",
        [tuple(r.get(k) for k in keys) for r in rows],
    )


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO sync_state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
