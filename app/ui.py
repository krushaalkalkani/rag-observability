"""Streamlit UI. Talks to the FastAPI server, so start that first:

    make serve      # terminal 1
    make ui         # terminal 2  -> http://localhost:8501
"""

import os
import re
import uuid

import requests
import streamlit as st

API_URL = os.environ.get("RAG_API_URL", "http://localhost:8000")
CHUNK_ID_RE = re.compile(r"\[([A-Z][A-Z.]{0,5}-\d{4}-\d{5})\]")

# An anonymous id per browser session: groups this visitor's questions into one
# Langfuse session without collecting anything personal.
if "session_id" not in st.session_state:
    st.session_state.session_id = f"anon-{uuid.uuid4().hex[:12]}"


def send_feedback(trace_id: str) -> None:
    value = st.session_state.get(f"fb_{trace_id}")  # 1 = thumbs up, 0 = thumbs down
    if value is None:
        return
    try:
        requests.post(f"{API_URL}/feedback", json={"trace_id": trace_id, "thumbs_up": value == 1}, timeout=10).raise_for_status()
        st.toast("Thanks - feedback saved to the trace.")
    except requests.RequestException as e:
        st.toast(f"Could not save feedback: {e}")


st.set_page_config(page_title="Ask My Docs - 10-K", page_icon=":page_facing_up:", layout="centered")
st.title("Ask My Docs")
st.caption("Questions about the latest 10-K filings of AAPL, MSFT, NVDA, TSLA, AMZN, GOOGL, META, JPM, NFLX, AMD")

examples = ["What are Tesla's main risk factors?", "How fast did Microsoft's cloud business grow in fiscal 2026?",
            "What was JPMorgan's CET1 capital ratio?", "What will Apple's revenue be in 2030?"]
picked = st.pills("Examples", examples, label_visibility="collapsed")
question = st.text_input("Your question", value=picked or "")

if question:
    # Streamlit re-runs this whole script on every click (e.g. the feedback buttons),
    # so keep the last answer instead of asking the API - and paying for the LLM - again.
    cached = st.session_state.get("last")
    if cached and cached["question"] == question:
        r = cached
    else:
        with st.spinner("Retrieving, re-ranking, answering..."):
            try:
                sid = st.session_state.session_id
                resp = requests.post(f"{API_URL}/ask", json={"question": question, "session_id": sid, "user_id": sid}, timeout=120)
                resp.raise_for_status()
                r = resp.json()
                st.session_state.last = r
            except requests.RequestException as e:
                st.error(f"Could not reach the API at {API_URL} - is `make serve` running?\n\n{e}")
                st.stop()

    if r["refused"]:
        st.warning(f"{r['answer']}  \n_(reason: {r['refusal_reason']})_")
    else:
        # Replace long chunk ids with short numbered markers [1], [2] that match the source list below.
        number = {c["chunk_id"]: i for i, c in enumerate(r["citations"], start=1)}
        st.markdown(CHUNK_ID_RE.sub(lambda m: f"**[{number[m.group(1)]}]**" if m.group(1) in number else "", r["answer"]))

        st.subheader("Sources")
        st.caption("Click a source to read the exact passage the answer is based on.")
        for i, c in enumerate(r["citations"], start=1):
            pages = f"p.{c['page_start']}" if c["page_start"] == c["page_end"] else f"pp.{c['page_start']}-{c['page_end']}"
            with st.expander(f"[{i}] {c['company']} · FY{c['fiscal_year']} · {c['section']} · {pages}"):
                st.caption(f"`{c['chunk_id']}` · [open filing on SEC.gov]({c['source_url']})")
                st.text(c["text"])

    cost = f" · ${r['cost_usd']:.5f}" if r.get("cost_usd") else ""
    st.caption(f"prompt `{r['prompt_version']}` · model `{r['model']}` · retrieval `{r['retrieval_mode']}` · {r['latency_ms']} ms{cost}")
    if r.get("trace_id"):
        left, right = st.columns([1, 3])
        with left:
            st.feedback("thumbs", key=f"fb_{r['trace_id']}", on_change=send_feedback, args=(r["trace_id"],))
        with right:
            if r.get("trace_url"):
                st.markdown(f"[Open this request's trace in Langfuse]({r['trace_url']})")
    with st.expander("Debug: retrieved chunks and scores"):
        st.dataframe(r["retrieved"], hide_index=True)
