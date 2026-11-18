"""Alert rules: check the recent window of requests against thresholds.

A rule reads like a sentence - "p95 latency over the last 60 min > 3000 ms" - and
lives in configs/config.yaml (monitoring.alerts.rules).

Notifications go out only when a rule CHANGES state (OK -> FIRING, FIRING -> OK),
never on every run: an alert that repeats every hour gets ignored. Each message
says what is wrong AND gives the first clues to why: the metric split by
prompt_version / config_hash, and whether a new version appeared in the window.
"""

from __future__ import annotations

import json
import operator
import os
from datetime import datetime, timedelta, timezone

from rag.config import PROJECT_ROOT
from rag.monitoring.metrics import summarize, to_frame

OPS = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le}
ALERT_LOG = PROJECT_ROOT / "logs" / "alerts.jsonl"


def fmt_value(metric: str, value) -> str:
    if value is None:
        return "n/a"
    if metric.endswith("_ms"):
        return f"{value:.0f} ms"
    if metric.endswith("_usd"):
        return f"${value:.5f}"
    return f"{value:.1%}" if metric.endswith("rate") or metric in ("citation_coverage",) else f"{value:.3f}"


def _clues(df, metric: str) -> list[str]:
    """Split the metric by version, and flag versions first seen in this window."""
    clues = []
    for col in ("prompt_version", "config_hash", "git_sha"):
        groups = df.groupby(col, dropna=False)
        if groups.ngroups > 1:
            parts = [f"{k}: {fmt_value(metric, summarize(g)[metric])} (n={len(g)})" for k, g in groups]
            clues.append(f"by {col} -> " + ", ".join(parts))
    return clues


def evaluate(conn, alert_cfg: dict, now: datetime | None = None, notify=None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=alert_cfg["window_minutes"])
    df = to_frame(conn.execute("SELECT * FROM requests WHERE ts >= ?", (since.isoformat(),)).fetchall())
    if len(df) < alert_cfg["min_requests"]:
        return [{"rule": r["name"], "status": "insufficient_data", "requests": len(df)} for r in alert_cfg["rules"]]

    # Versions seen in this window but never before it = a change was shipped recently.
    first_seen = conn.execute(
        "SELECT prompt_version, config_hash, git_sha, MIN(ts) AS first FROM requests "
        "GROUP BY prompt_version, config_hash, git_sha HAVING first >= ?", (since.isoformat(),)).fetchall()
    new_versions = [f"{r['prompt_version']} / config {r['config_hash']} / git {r['git_sha']} (first seen {r['first'][:16]}Z)"
                    for r in first_seen]

    metrics = summarize(df)
    results = []
    for rule in alert_cfg["rules"]:
        value = metrics.get(rule["metric"])
        # Some metrics are only measured on a sample (faithfulness): don't judge them on too few.
        enough = all((metrics.get(k) or 0) >= n for k, n in rule.get("min_count", {}).items())
        firing = enough and value is not None and OPS[rule["op"]](value, rule["threshold"])
        prev = conn.execute("SELECT firing FROM alert_state WHERE rule=?", (rule["name"],)).fetchone()
        changed = bool(prev["firing"]) != firing if prev else firing
        result = {
            "rule": rule["name"], "status": "FIRING" if firing else "OK", "changed": changed,
            "metric": rule["metric"], "value": value, "op": rule["op"], "threshold": rule["threshold"],
            "requests": len(df), "window_minutes": alert_cfg["window_minutes"], "ts": now.isoformat(timespec="seconds"),
        }
        if firing:
            result["clues"] = _clues(df, rule["metric"]) + [f"new in window: {v}" for v in new_versions]
        results.append(result)
        conn.execute(
            "INSERT INTO alert_state VALUES (?, ?, ?, ?) ON CONFLICT(rule) DO UPDATE SET "
            "firing=excluded.firing, value=excluded.value, since=CASE WHEN alert_state.firing=excluded.firing "
            "THEN alert_state.since ELSE excluded.since END",
            (rule["name"], int(firing), value, now.isoformat(timespec="seconds")),
        )
        if changed:
            (notify or send)(result)
    return results


def message(r: dict) -> str:
    value, threshold = fmt_value(r["metric"], r["value"]), fmt_value(r["metric"], r["threshold"])
    if r["status"] == "FIRING":
        text = f"🔴 FIRING {r['rule']}: {r['metric']} = {value}, alert threshold {r['op']} {threshold}"
    else:
        text = f"✅ RESOLVED {r['rule']}: {r['metric']} back to {value} (alert threshold {r['op']} {threshold})"
    text += f" - last {r['window_minutes']} min, {r['requests']} requests"
    return "\n".join([text, *(f"   • {c}" for c in r.get("clues", []))])


def send(r: dict) -> None:
    """Console + logs/alerts.jsonl always; Slack too if SLACK_WEBHOOK_URL is set."""
    text = message(r)
    print(text)
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERT_LOG, "a") as f:
        f.write(json.dumps(r) + "\n")
    if url := os.environ.get("SLACK_WEBHOOK_URL"):
        import requests

        try:
            requests.post(url, json={"text": text}, timeout=10).raise_for_status()
        except requests.RequestException as e:
            print(f"   (Slack notification failed: {e})")
