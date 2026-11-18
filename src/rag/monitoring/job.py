"""The monitoring job: Langfuse -> metrics table -> alerts.

    python -m rag.monitoring.job              (or: make metrics)
    python -m rag.monitoring.job --since 7d   first run: how far back to read
    python -m rag.monitoring.job --no-eval    skip the (paid) online faithfulness grading

Steps, in order:
  1. collect   new finished traces from Langfuse -> `requests` rows
  2. evaluate  grade a sample of new answers for faithfulness (LLM judge)
  3. aggregate requests -> `metrics` table (p50/p95, cost, rates per time bucket)
  4. alert     check the last hour against the alert rules, notify on change

Safe to run any time and as often as you like: re-reading a trace updates its row
instead of duplicating it. In production you'd schedule it (cron / a scheduler)
every hour; locally, run it after generating traffic.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone

from rag.config import load_config
from rag.monitoring import alerts, online_eval, store
from rag.monitoring.collect import collect
from rag.monitoring.metrics import bucketize, to_frame


def parse_age(text: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([mhd])", text)
    if not m:
        raise argparse.ArgumentTypeError("use e.g. 30m, 6h or 7d")
    unit = {"m": "minutes", "h": "hours", "d": "days"}[m[2]]
    return timedelta(**{unit: int(m[1])})


def run(since: timedelta, do_eval: bool = True, do_alerts: bool = True) -> dict:
    from rag.tracing import get_langfuse

    lf = get_langfuse()
    if lf is None:
        sys.exit("Langfuse is not configured: run with the .env loaded (make metrics).")
    cfg = load_config()
    mon = cfg["monitoring"]
    now = datetime.now(timezone.utc)
    report = {}

    with store.connect() as conn:
        # 1. collect - from the last sync (minus an overlap for late arrivals), or `since` on the first run
        last = store.get_state(conn, "synced_until")
        start = (datetime.fromisoformat(last) - timedelta(minutes=mon["resync_minutes"])) if last else now - since
        rows = collect(lf, start, now, mon["environment"])
        store.upsert_requests(conn, rows)
        store.set_state(conn, "synced_until", now.isoformat())
        report["collected"] = len(rows)
        print(f"1. collect    {len(rows)} requests from Langfuse ({start:%Y-%m-%d %H:%M} -> {now:%H:%M} UTC)")

        # 2. online eval
        oe = mon["online_eval"]
        newly = online_eval.mark_samples(conn, oe["sample_rate"])
        conn.commit()
        if do_eval:
            res = online_eval.grade_pending(conn, lf, cfg["eval"], oe["max_per_run"])
            report["online_eval"] = res
            print(f"2. evaluate   {newly} newly sampled; graded {res.get('graded', 0)}"
                  + (f", mean faithfulness {res['mean']}, judge cost ${res['judge_cost_usd']:.3f}" if res.get("graded") else ""))
        else:
            print(f"2. evaluate   skipped ({newly} newly sampled, waiting)")

        # 3. aggregate - recompute the whole table: it's small, and it keeps it exactly in sync
        df = to_frame(conn.execute("SELECT * FROM requests WHERE environment=?", (mon["environment"],)).fetchall())
        metrics = bucketize(df, mon["bucket"])
        conn.execute("DELETE FROM metrics WHERE bucket=?", (mon["bucket"],))
        if not metrics.empty:
            metrics = metrics.assign(bucket_start=metrics["bucket_start"].map(lambda t: t.isoformat()))
            metrics.to_sql("metrics", conn, if_exists="append", index=False)
        report["buckets"] = int((metrics["prompt_version"] == "ALL").sum()) if not metrics.empty else 0
        print(f"3. aggregate  {len(df)} requests -> {report['buckets']} {mon['bucket']} buckets in {store.db_path().name}")

        # 4. alerts
        if do_alerts:
            results = alerts.evaluate(conn, mon["alerts"], now)
            firing = [r["rule"] for r in results if r["status"] == "FIRING"]
            report["firing"] = firing
            if results and results[0]["status"] == "insufficient_data":
                print(f"4. alerts     not checked: only {results[0]['requests']} requests in the window "
                      f"(need {mon['alerts']['min_requests']})")
            else:
                print(f"4. alerts     {len(firing)} firing" + (f": {', '.join(firing)}" if firing else ""))
    lf.flush()  # send the faithfulness scores
    return report


def main() -> None:
    p = argparse.ArgumentParser(description="Pull traces from Langfuse, compute metrics, check alerts.")
    p.add_argument("--since", type=parse_age, default=timedelta(days=7), help="first run only: how far back (default 7d)")
    p.add_argument("--no-eval", action="store_true", help="skip online faithfulness grading (costs ~$0.03/answer)")
    p.add_argument("--no-alerts", action="store_true")
    args = p.parse_args()
    run(args.since, do_eval=not args.no_eval, do_alerts=not args.no_alerts)


if __name__ == "__main__":
    main()
