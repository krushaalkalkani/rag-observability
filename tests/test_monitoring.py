"""Monitoring math and alert behaviour - no Langfuse or LLM needed."""

import random
from datetime import datetime, timedelta, timezone

import pytest

from rag.monitoring import alerts, store
from rag.monitoring.metrics import bucketize, summarize, to_frame
from rag.monitoring.online_eval import is_sampled, mark_samples

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def req(i: int, minutes_ago: float = 5, **kw) -> dict:
    row = {k: None for k in store.REQUEST_COLUMNS}
    row |= {"trace_id": f"{i:032x}", "ts": (NOW - timedelta(minutes=minutes_ago)).isoformat(), "environment": "default",
            "prompt_version": "answer_v2", "config_hash": "c1", "git_sha": "g1", "error": 0, "refused": 0,
            "citation_coverage": 1.0, "latency_ms": 1000.0, "cost_usd": 0.0005, "input_tokens": 3000, "output_tokens": 200}
    return row | kw


def test_percentiles_expose_the_slow_tail_the_mean_hides():
    rows = [req(i, latency_ms=1000.0) for i in range(19)] + [req(19, latency_ms=30_000.0)]
    m = summarize(to_frame(rows))
    assert m["latency_p50_ms"] == 1000.0
    assert m["latency_p95_ms"] > 2000  # the one 30 s request shows up at p95...
    assert m["requests"] == 20


def test_rates_use_the_right_denominators():
    rows = [req(0), req(1, citation_coverage=0.5), req(2, refused=1, citation_coverage=None),
            req(3, error=1, refused=None, citation_coverage=None, cost_usd=None)]
    m = summarize(to_frame(rows))
    assert m["error_rate"] == 0.25            # errors out of all requests
    assert m["refusal_rate"] == pytest.approx(1 / 3, abs=1e-4)  # refusals out of requests that didn't error
    assert m["citation_coverage"] == 0.75     # mean over answered (non-refused) requests only
    assert m["fully_grounded_rate"] == 0.5


def test_bucketize_gives_overall_and_per_version_rows():
    rows = [req(0, minutes_ago=70), req(1, minutes_ago=5), req(2, minutes_ago=4, prompt_version="answer_v1")]
    b = bucketize(to_frame(rows), "1h")
    assert set(b["prompt_version"]) == {"ALL", "answer_v1", "answer_v2"}
    assert b[b["prompt_version"] == "ALL"]["requests"].sum() == 3


def test_sampling_is_stable_and_close_to_the_rate():
    rng = random.Random(0)  # trace ids are random 32-hex-digit strings
    ids = [f"{rng.getrandbits(128):032x}" for _ in range(5000)]
    picked = [t for t in ids if is_sampled(t, 0.1)]
    assert 0.08 < len(picked) / len(ids) < 0.12
    assert picked == [t for t in ids if is_sampled(t, 0.1)]  # same answer every time


def test_only_answered_requests_are_sampled(tmp_path):
    with store.connect(tmp_path / "m.db") as conn:
        store.upsert_requests(conn, [req(0, trace_id="0" * 32), req(1, trace_id="00" + "f" * 30, refused=1),
                                     req(2, trace_id="000" + "f" * 29, error=1)])
        mark_samples(conn, 0.5)
        status = dict(conn.execute("SELECT trace_id, eval_status FROM requests").fetchall())
    assert sorted(status.values()) == ["pending", "skipped", "skipped"]


RULES = {"window_minutes": 60, "min_requests": 3,
         "rules": [{"name": "coverage_low", "metric": "citation_coverage", "op": "<", "threshold": 0.9}]}


def test_alert_notifies_only_when_state_changes(tmp_path):
    sent = []
    with store.connect(tmp_path / "m.db") as conn:
        store.upsert_requests(conn, [req(i) for i in range(5)])
        alerts.evaluate(conn, RULES, NOW, notify=sent.append)
        assert sent == []                                     # healthy: nothing sent

        bad = [req(10 + i, minutes_ago=2, citation_coverage=0.4, prompt_version="answer_bad", config_hash="c2")
               for i in range(5)]
        store.upsert_requests(conn, bad)
        alerts.evaluate(conn, RULES, NOW, notify=sent.append)
        assert [s["status"] for s in sent] == ["FIRING"]
        assert any("answer_bad" in c for c in sent[0]["clues"])  # the clue points at the new version

        alerts.evaluate(conn, RULES, NOW, notify=sent.append)
        assert len(sent) == 1                                 # still firing: no repeat

        later = NOW + timedelta(hours=2)
        store.upsert_requests(conn, [req(20 + i, minutes_ago=-115) for i in range(5)])  # healthy, 5 min before `later`
        alerts.evaluate(conn, RULES, later, notify=sent.append)
        assert [s["status"] for s in sent] == ["FIRING", "OK"]  # resolved


def test_alerts_wait_for_enough_data(tmp_path):
    with store.connect(tmp_path / "m.db") as conn:
        store.upsert_requests(conn, [req(0, citation_coverage=0.1)])
        res = alerts.evaluate(conn, RULES, NOW, notify=lambda r: pytest.fail("should not notify"))
    assert res[0]["status"] == "insufficient_data"


def test_resync_keeps_existing_faithfulness(tmp_path):
    with store.connect(tmp_path / "m.db") as conn:
        store.upsert_requests(conn, [req(0)])
        conn.execute("UPDATE requests SET eval_status='done', faithfulness=0.9")
        store.upsert_requests(conn, [req(0, latency_ms=999.0)])  # same trace read again
        row = conn.execute("SELECT faithfulness, latency_ms FROM requests").fetchone()
    assert row["faithfulness"] == 0.9 and row["latency_ms"] == 999.0


def test_sampled_metric_waits_for_enough_samples(tmp_path):
    rules = {"window_minutes": 60, "min_requests": 3, "rules": [
        {"name": "faith_low", "metric": "faithfulness", "op": "<", "threshold": 0.85, "min_count": {"faithfulness_n": 5}}]}
    with store.connect(tmp_path / "m.db") as conn:
        store.upsert_requests(conn, [req(i, faithfulness=0.2 if i < 2 else None) for i in range(6)])
        res = alerts.evaluate(conn, rules, NOW, notify=lambda r: pytest.fail("2 samples must not fire"))
        assert res[0]["status"] == "OK"
        store.upsert_requests(conn, [req(10 + i, faithfulness=0.2) for i in range(3)])  # now 5 graded
        sent = []
        alerts.evaluate(conn, rules, NOW, notify=sent.append)
    assert [s["status"] for s in sent] == ["FIRING"]


def test_resolved_message_does_not_read_like_a_breach():
    msg = alerts.message({"rule": "r", "status": "OK", "metric": "refusal_rate", "value": 0.298, "op": ">",
                          "threshold": 0.3, "window_minutes": 60, "requests": 94})
    assert msg.startswith("✅ RESOLVED") and "back to 29.8%" in msg
