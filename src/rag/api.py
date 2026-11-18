"""HTTP API.

    uvicorn rag.api:app --port 8000      (or: make serve)

    POST /ask            {"question": "..."} -> answer, citations, refused, prompt_version, latency_ms, trace_id
    POST /feedback       {"trace_id": "...", "thumbs_up": true} -> stored as a Langfuse score
    GET  /chunks/{id}    full text + metadata of one chunk
    GET  /health
Interactive docs: http://localhost:8000/docs
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from rag import tracing
from rag.index import get_chunk
from rag.pipeline import answer_question
from rag.retrieve import retrieve


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000, examples=["What are Tesla's main risk factors?"])
    mode: str | None = Field(None, description="vector | bm25 | hybrid | hybrid_rerank (default: config)")
    # Anonymous ids (random, set by the client) - group traces per visitor / per chat session.
    user_id: str | None = Field(None, max_length=200, pattern=r"^[\x20-\x7e]*$")
    session_id: str | None = Field(None, max_length=200, pattern=r"^[\x20-\x7e]*$")


class Citation(BaseModel):
    chunk_id: str
    company: str
    ticker: str
    fiscal_year: int
    section: str
    page_start: int
    page_end: int
    source_url: str
    text: str


class AskResponse(BaseModel):
    answer: str
    citations: list[Citation]
    refused: bool
    refusal_reason: str | None
    prompt_version: str
    model: str | None
    retrieval_mode: str
    latency_ms: int
    ttft_ms: int | None
    cost_usd: float | None
    retrieved: list[dict]
    trace_id: str | None
    trace_url: str | None


class FeedbackRequest(BaseModel):
    trace_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    thumbs_up: bool
    comment: str | None = Field(None, max_length=2000)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the embedding model, BM25 index and re-ranker ONCE at startup (~15s),
    # so the first real request isn't slow.
    retrieve("warm up", "hybrid_rerank")
    yield
    tracing.flush()  # send any buffered traces before the server exits


app = FastAPI(title="Ask My Docs - 10-K RAG", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


# A plain `def` (not async): FastAPI runs it in a worker thread, so a slow
# LLM call doesn't block other requests.
@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    if req.mode and req.mode not in {"vector", "bm25", "hybrid", "hybrid_rerank"}:
        raise HTTPException(422, f"unknown mode {req.mode!r}")
    r = answer_question(req.question, mode=req.mode, user_id=req.user_id, session_id=req.session_id)
    return {**r, "trace_url": tracing.trace_url(r["trace_id"])}


@app.post("/feedback")
def feedback(req: FeedbackRequest):
    if not tracing.record_feedback(req.trace_id, req.thumbs_up, req.comment):
        raise HTTPException(503, "tracing is not configured (set LANGFUSE_* keys)")
    return {"status": "recorded"}


@app.get("/chunks/{chunk_id}", response_model=Citation)
def chunk(chunk_id: str):
    c = get_chunk(chunk_id)
    if c is None:
        raise HTTPException(404, f"no chunk {chunk_id}")
    return c
