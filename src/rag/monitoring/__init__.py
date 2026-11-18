"""Phase 2 monitoring: Langfuse traces -> metrics over time -> dashboard + alerts.

    collect.py      read finished traces from Langfuse, one row per request
    online_eval.py  grade a sample of live answers for faithfulness (LLM judge)
    metrics.py      roll requests up into time buckets (p50/p95, rates, cost)
    alerts.py       check alert rules on the recent window, notify on change
    store.py        the SQLite file that holds it all (data/metrics.db)
    job.py          `python -m rag.monitoring.job` runs the steps in order
"""
