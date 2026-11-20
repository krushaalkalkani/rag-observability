"""Monitoring dashboard: quality, latency and cost over time.

    make metrics      # pull the latest traces into data/metrics.db first
    make dashboard    # -> http://localhost:8502

Built to answer "what got worse, when, and what changed just before?":
- every chart marks the moments a new prompt_version / config / commit first appeared
  (dashed vertical lines) - the usual suspects for any change in the lines;
- "Compare versions" puts prompt versions, configs or commits side by side on any metric;
- "Worst requests" links straight to the Langfuse trace of each bad request.

All numbers come from rag.monitoring.metrics.summarize - the same definitions the
alert rules and the stored metrics table use.
"""

import os
from datetime import timedelta

import altair as alt
import pandas as pd
import streamlit as st

from rag.config import load_config
from rag.monitoring import store
from rag.monitoring.alerts import OPS, fmt_value
from rag.monitoring.metrics import BUCKETS, bucketize, summarize, to_frame

# Validated categorical palette (dataviz reference instance), assigned in this fixed order.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
CRITICAL = "#d03b3b"  # status color: reserved for alert thresholds, always with a text label
MUTED = "#8a8984"
RANGES = {"Last hour": timedelta(hours=1), "Last 6 hours": timedelta(hours=6), "Last 24 hours": timedelta(days=1),
          "Last 7 days": timedelta(days=7), "All time": None}

st.set_page_config(page_title="RAG Monitoring", page_icon=":bar_chart:", layout="wide")
cfg = load_config()["monitoring"]
RULES = {r["metric"]: r for r in cfg["alerts"]["rules"]}
TRACE_URL = os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000") + "/project/rag-10k/traces/"


@st.cache_data(ttl=30)
def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    with store.connect() as conn:
        req = to_frame(conn.execute("SELECT * FROM requests WHERE environment=?", (cfg["environment"],)).fetchall())
        alerts = pd.DataFrame([dict(r) for r in conn.execute("SELECT * FROM alert_state").fetchall()])
    return req, alerts


req_all, alert_state = load()
st.title("RAG monitoring")
if req_all.empty:
    st.info("No data yet. Generate traffic (`make traffic N=50`) and run `make metrics`.")
    st.stop()

# ---------- filters: one row above everything ----------
c1, c2, c3, c4 = st.columns([1.2, 1, 2.4, 0.6])
range_name = c1.selectbox("Time range", list(RANGES), index=2)
bucket = c2.selectbox("Bucket", list(BUCKETS), index=0, help="Size of each point on the charts")
versions = sorted(req_all["prompt_version"].dropna().unique())
picked = c3.multiselect("Prompt versions", versions, default=versions)
if c4.button("Refresh", help="Re-read data/metrics.db (run `make metrics` to pull new traces)"):
    load.clear()
    st.rerun()

now = pd.Timestamp.now(tz="UTC")
span = RANGES[range_name]
req = req_all[req_all["prompt_version"].isin(picked)]
if span is not None:
    req = req[req["ts"] >= now - span]
if req.empty:
    st.warning("No requests in this range / selection.")
    st.stop()

# Change markers: first time each (prompt_version, config_hash, git_sha) combination was seen.
changes = (req_all.groupby(["prompt_version", "config_hash", "git_sha"], dropna=False)["ts"].min()
           .reset_index().rename(columns={"ts": "first_seen"}).sort_values("first_seen"))
changes = changes[changes["first_seen"] >= req["ts"].min()]
changes["first_seen_utc"] = changes["first_seen"].dt.strftime("%b %d %H:%M")
changes["label"] = changes["prompt_version"] + " · cfg " + changes["config_hash"].fillna("?") + " · " + changes["git_sha"].fillna("?")

# ---------- alerts ----------
if not alert_state.empty and alert_state["firing"].any():
    for _, a in alert_state[alert_state["firing"] == 1].iterrows():
        rule = next(r for r in cfg["alerts"]["rules"] if r["name"] == a["rule"])
        st.error(f"**FIRING: {a['rule']}** — {rule['metric']} = {fmt_value(rule['metric'], a['value'])} "
                 f"({rule['op']} {fmt_value(rule['metric'], rule['threshold'])}, last {cfg['alerts']['window_minutes']} min) "
                 f"since {a['since'][:16]}Z", icon="🚨")
elif not alert_state.empty:
    st.success(f"All {len(alert_state)} alert rules OK at the last `make metrics` run.", icon="✅")
else:
    st.caption("Alerts not evaluated yet (needs ≥ "
               f"{cfg['alerts']['min_requests']} requests in the last {cfg['alerts']['window_minutes']} min).")

# ---------- headline numbers for the selected range ----------
m = summarize(req)
tiles = [  # (label, metric); two rows of four so values never get cut off
    ("Requests", "requests"), ("p50 latency", "latency_p50_ms"), ("p95 latency", "latency_p95_ms"),
    ("Cost / request", "cost_per_request_usd"), ("Citation coverage", "citation_coverage"),
    ("Refusal rate", "refusal_rate"), ("Error rate", "error_rate"), ("Faithfulness (sampled)", "faithfulness"),
]
for row in (tiles[:4], tiles[4:]):
    for col, (label, metric) in zip(st.columns(4), row):
        value = m.get(metric)
        shown = str(value) if metric == "requests" else (
            f"{value / 1000:.2f} s" if metric.endswith("_ms") and value is not None else fmt_value(metric, value))
        rule = RULES.get(metric)
        help_text = f"Alert when {rule['op']} {fmt_value(metric, rule['threshold'])} over the last " \
                    f"{cfg['alerts']['window_minutes']} min" if rule else None
        if metric == "faithfulness":
            help_text += f" · {m['faithfulness_n']} answers graded in this range"
        col.metric(label, shown, help=help_text)
        if rule and value is not None and OPS[rule["op"]](value, rule["threshold"]):
            col.caption(f":red[▲ beyond alert threshold {rule['op']} {fmt_value(metric, rule['threshold'])}]")

# ---------- charts ----------
buckets = bucketize(req, bucket)
overall = buckets[buckets["prompt_version"] == "ALL"]
hover = alt.selection_point(fields=["bucket_start"], nearest=True, on="pointerover", empty=False, clear="pointerout")


def ts_chart(metrics: dict[str, str], title: str, fmt: str, threshold_metric: str | None = None,
             data: pd.DataFrame | None = None, color_by: str | None = None, height: int = 230) -> alt.LayerChart:
    """Line chart over time: one line per series (fixed color order), crosshair tooltip,
    change markers, and an optional labelled alert-threshold line."""
    data = overall if data is None else data
    if color_by:
        long = data[["bucket_start", color_by, "requests", *metrics]].rename(columns={color_by: "series", list(metrics)[0]: "value"})
        domain = list(dict.fromkeys(long["series"]))
    else:
        long = data.melt(id_vars=["bucket_start", "requests"], value_vars=list(metrics), var_name="series", value_name="value")
        long["series"] = long["series"].map(metrics)
        domain = list(metrics.values())
    long = long.dropna(subset=["value"]).sort_values("bucket_start")
    # Break a line where buckets are missing (no requests) instead of drawing a straight
    # line across the gap, which would suggest data that doesn't exist.
    step = pd.Timedelta(BUCKETS[bucket])
    long["segment"] = long.groupby("series")["bucket_start"].transform(lambda t: (t.diff() > step).cumsum())
    # Dots: shown on hover; always shown for a point with no line through it (alone between gaps),
    # or for every point when there are few.
    seg_size = long.groupby(["series", "segment"])["value"].transform("size")
    long["dot"] = ((seg_size == 1) | (len(long) <= 12)).astype(float) * 0.9
    # Tooltip time as plain UTC text (Vega-Lite's temporal tooltip formatting printed NaN).
    long["when"] = long["bucket_start"].dt.strftime("%b %d %H:%M")
    color = alt.Color("series:N", scale=alt.Scale(domain=domain, range=SERIES[: len(domain)]),
                      legend=alt.Legend(title=None, orient="top", direction="horizontal") if len(domain) > 1 else None)
    # Everything in UTC, matching the alert messages and tables.
    x = alt.X("bucket_start:T", title=None, scale=alt.Scale(type="utc"),
              axis=alt.Axis(format="%H:%M", labelColor=MUTED, grid=False))
    y = alt.Y("value:Q", title=None, axis=alt.Axis(format=fmt, labelColor=MUTED, gridOpacity=0.3, tickCount=4))
    base = alt.Chart(long).encode(x=x, y=y, color=color, detail="segment:N")
    lines = base.mark_line(strokeWidth=2, interpolate="monotone")
    points = base.mark_circle(size=64).encode(
        opacity=alt.condition(hover, alt.value(1), alt.Opacity("dot:Q", scale=None, legend=None)))
    tooltip = [alt.Tooltip("when:N", title="bucket (UTC)"), alt.Tooltip("series:N"),
               alt.Tooltip("value:Q", format=fmt), alt.Tooltip("requests:Q")]
    rule = alt.Chart(long).mark_rule(color=MUTED).encode(x="bucket_start:T").transform_filter(hover)
    hits = alt.Chart(long).mark_rule(opacity=0, strokeWidth=12).encode(x="bucket_start:T", tooltip=tooltip).add_params(hover)
    layers = [hits, lines, points, rule]
    if not changes.empty:
        layers.append(alt.Chart(changes).mark_rule(strokeDash=[4, 3], color=MUTED, strokeWidth=1).encode(
            x="first_seen:T", tooltip=[alt.Tooltip("label:N", title="first seen"),
                                       alt.Tooltip("first_seen_utc:N", title="at (UTC)")]))
    subtitle = []
    if threshold_metric and threshold_metric in RULES:
        r = RULES[threshold_metric]
        layers.append(alt.Chart(pd.DataFrame({"y": [r["threshold"]]}))
                      .mark_rule(color=CRITICAL, strokeDash=[6, 4], strokeWidth=1.5).encode(y="y:Q"))
        subtitle = [f"red dashed line = alert threshold {r['op']} {fmt_value(threshold_metric, r['threshold'])}"]
    return alt.layer(*layers).properties(
        title=alt.TitleParams(title, subtitle=subtitle, anchor="start", fontSize=14, subtitleColor=CRITICAL), height=height)


st.caption("Times in UTC. Dashed grey lines = first appearance of a prompt version / config / commit "
           "(hover them to see which). Lines break where a bucket had no requests.")
left, right = st.columns(2)
left.altair_chart(ts_chart({"latency_p50_ms": "p50", "latency_p95_ms": "p95"}, "End-to-end latency (ms)", ",.0f",
                           "latency_p95_ms"), use_container_width=True)
right.altair_chart(ts_chart({"retrieval_p95_ms": "retrieval", "rerank_p95_ms": "rerank", "generation_p95_ms": "LLM generation",
                            "ttft_p95_ms": "LLM first token"}, "p95 latency by step (ms)", ",.0f"), use_container_width=True)
left, right = st.columns(2)
left.altair_chart(ts_chart({"citation_coverage": "citation coverage", "fully_grounded_rate": "fully grounded answers"},
                           "Grounding", ".0%", "citation_coverage"), use_container_width=True)
right.altair_chart(ts_chart({"refusal_rate": "refusal rate", "error_rate": "error rate"}, "Refusals and errors", ".0%",
                            "refusal_rate"), use_container_width=True)
left, right = st.columns(2)
left.altair_chart(ts_chart({"cost_per_request_usd": "cost / request"}, "Cost per request (USD)", "$.5f",
                           "cost_per_request_usd"), use_container_width=True)
right.altair_chart(ts_chart({"faithfulness": "faithfulness"}, f"Faithfulness, {cfg['online_eval']['sample_rate']:.0%} sample (LLM judge)",
                            ".2f", "faithfulness"), use_container_width=True)

# ---------- per-version comparison ----------
st.subheader("Compare versions")
st.caption("A regression usually lines up with a change. Not every change is a prompt change: "
           "split by config or commit too (incident 001 was a config change).")
dims = {"prompt_version": "prompt version", "config_hash": "config", "git_sha": "commit"}
c1, c2 = st.columns([1, 2])
dim = c1.selectbox("Compare by", list(dims), format_func=dims.get)
metric_names = {"citation_coverage": "Citation coverage", "fully_grounded_rate": "Fully grounded answers",
                "refusal_rate": "Refusal rate", "latency_p95_ms": "p95 latency (ms)", "cost_per_request_usd": "Cost / request",
                "tokens_per_request": "Tokens / request", "faithfulness": "Faithfulness", "error_rate": "Error rate"}
pick = c2.selectbox("Metric", list(metric_names), format_func=metric_names.get)
fmt = (",.0f" if pick.endswith("_ms") or pick == "tokens_per_request" else "$.5f" if pick.endswith("_usd")
       else ".2f" if pick == "faithfulness" else ".0%")
# Same bucketing as the charts above, once per version of the chosen dimension.
by_dim = pd.concat([bucketize(g, bucket).query("prompt_version == 'ALL'").assign(version=str(v))
                    for v, g in req.groupby(dim, dropna=False)], ignore_index=True)
st.altair_chart(ts_chart({pick: metric_names[pick]}, f"{metric_names[pick]} by {dims[dim]}", fmt, pick,
                         data=by_dim, color_by="version", height=260), use_container_width=True)
table = pd.DataFrame([{dims[dim]: v, "first seen": g["ts"].min(), **summarize(g)} for v, g in req.groupby(dim, dropna=False)])
st.dataframe(table[[dims[dim], "first seen", "requests", "latency_p50_ms", "latency_p95_ms", "tokens_per_request",
                    "cost_per_request_usd", "citation_coverage", "fully_grounded_rate", "refusal_rate", "error_rate",
                    "faithfulness", "faithfulness_n"]].sort_values("first seen"),
             hide_index=True, use_container_width=True,
             column_config={"first seen": st.column_config.DatetimeColumn("first seen (UTC)", format="MMM D HH:mm")})

# ---------- what changed when ----------
with st.expander(f"What changed when ({len(changes)} versions in range)", expanded=False):
    st.dataframe(changes[["first_seen", "prompt_version", "config_hash", "git_sha"]], hide_index=True, use_container_width=True)

# ---------- worst requests ----------
st.subheader("Worst requests")
kind = st.radio("Show", ["Lowest citation coverage", "Refused", "Slowest", "Errors", "Lowest faithfulness"], horizontal=True)
view = req.assign(trace=TRACE_URL + req["trace_id"])
view = {
    "Lowest citation coverage": view[view["refused"] == 0].sort_values("citation_coverage"),
    "Refused": view[view["refused"] == 1].sort_values("ts", ascending=False),
    "Slowest": view.sort_values("latency_ms", ascending=False),
    "Errors": view[view["error"] == 1].sort_values("ts", ascending=False),
    "Lowest faithfulness": view.dropna(subset=["faithfulness"]).sort_values("faithfulness"),
}[kind].head(25)
st.dataframe(
    view[["ts", "question", "citation_coverage", "faithfulness", "latency_ms", "refusal_reason", "error_message",
          "prompt_version", "config_hash", "git_sha", "trace"]],
    hide_index=True, use_container_width=True,
    column_config={"trace": st.column_config.LinkColumn("trace", display_text="open in Langfuse"),
                   "ts": st.column_config.DatetimeColumn("time (UTC)", format="MMM D HH:mm:ss"),
                   "citation_coverage": st.column_config.NumberColumn(format="percent"),
                   "latency_ms": st.column_config.NumberColumn("latency ms", format="%d")},
)
