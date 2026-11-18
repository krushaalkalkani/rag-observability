# Observability, Phase 1 — tracing every request

Every question the RAG system answers is recorded as a **trace** in a self-hosted
[Langfuse](https://langfuse.com). Open any trace to see exactly what was retrieved,
how it was re-ranked, what was sent to the LLM, what came back, what it cost, and
which guardrail decided what.

## Setup (once)

Needs Docker Desktop running.

```bash
make langfuse-up      # creates .env (random secrets + API keys), starts Langfuse
```

First start takes about a minute (database migrations). Then open
<http://localhost:3000> and log in with `LANGFUSE_INIT_USER_EMAIL` /
`LANGFUSE_INIT_USER_PASSWORD` from `.env`. The org, project ("10-K RAG") and API
keys already exist — `.env` created them ("headless init"), so there's no sign-up.

`.env` is git-ignored. It is read by Docker Compose (container secrets) **and** by
the app: every `make ask` / `make serve` / `make eval` passes it to Python with
`uv run --env-file .env`. Without `.env` (CI, unit tests) tracing switches itself
off and nothing else changes.

| Command | What it does |
|---|---|
| `make langfuse-up` | start Langfuse (data persists in Docker volumes) |
| `make langfuse-down` | stop it |
| `make langfuse-logs` | follow the server logs |
| `make langfuse-reset` | **delete all traces** and start fresh |

Services in [docker-compose.yml](../docker-compose.yml): `langfuse-web` (UI + API, port
3000), `langfuse-worker` (background ingestion), Postgres (users, keys, scores),
ClickHouse (the traces), Redis (queue), MinIO (blob storage). Only port 3000 and
MinIO's 9090 are published, on localhost only.

## Generate traces

```bash
make ask Q="What was JPMorgan's CET1 capital ratio?"   # prints a link to the trace
make serve   # terminal 1: API
make ui      # terminal 2: UI with 👍/👎 and an "Open trace" link under each answer
```

## How to read a trace

```
rag-request                 question in, answer out, latency, total cost, scores
 ├─ retrieval               the final top-5 chunks handed to the LLM (ids, scores, preview)
 │   ├─ vector-search       top-20 by meaning (cosine similarity)
 │   ├─ bm25-search         top-20 by exact words (+ the tokens BM25 searched for)
 │   ├─ rrf-fusion          merged order; which chunks BOTH retrievers found
 │   └─ rerank              every candidate: old rank -> new rank, score, kept?
 ├─ retrieval-gate          best re-rank score vs threshold: refuse before calling the LLM?
 ├─ llm-generation          exact system + user prompt, output, tokens, $ cost, time-to-first-token
 └─ citation-check          per-sentence support check, dropped sentences + why, coverage
```

What is recorded where:

| Question you might ask | Where to look |
|---|---|
| What was retrieved? | `retrieval` output |
| How did re-ranking change the order? | `rerank` output: `old_rank`, `new_rank`, `moved` |
| What exactly did the LLM see? | `llm-generation` input (full prompt, `prompt_version` in metadata) |
| Why was the answer refused? | `retrieval-gate` (low score) or `citation-check` (unsupported sentences) |
| Which step was slow? | Timeline tab: every step has its own duration |
| What did it cost? | `llm-generation` cost; trace total in the header |
| Which code/config produced it? | trace metadata: `prompt_version`, `config_hash`, `git_sha` |
| Did the user like it? | `user_feedback` score (1 = 👍, 0 = 👎) |

### Metadata on every trace

| Field | Meaning |
|---|---|
| `prompt_version` | which file in `configs/prompts/` was used (also the trace's `version` and a tag) |
| `config_hash` | 12-char fingerprint of the behaviour-relevant config (retrieval, reranker, guardrails, llm, ...). Same hash = same settings |
| `git_sha` | commit of the code; `+dirty` = uncommitted changes; `unknown` outside a git repo |
| `retrieval_mode` | vector / bm25 / hybrid / hybrid_rerank (also a tag) |
| user / session id | anonymous random id per UI browser session — nothing personal |

### Scores on every trace

`refused` (boolean), `citation_coverage` (0–1, share of answer sentences backed by
their cited chunk), and `user_feedback` when someone clicks 👍/👎. Phase 2 charts these.

## Design decisions

- **Cost is computed by us, not Langfuse.** Prices live in `configs/config.yaml`
  (`pricing`). Langfuse's built-in price for the Anthropic SDK's own span left out
  the *thinking* tokens that Haiku bills as output: on one test request it priced
  238 output tokens instead of 816, under-reporting the total by about a third.
- **Only our spans are exported** (`should_export_span=is_langfuse_span`). The
  Anthropic SDK emits its own OpenTelemetry span per call; exporting it too showed a
  second "generation" and double-counted cost.
- **Streaming LLM calls** so we can timestamp the first token: time-to-first-token is
  what a user feels, and total latency hides it.
- **Tracing can't break answering.** Without keys every tracing call is a no-op;
  a step called outside a request (e.g. the API's warm-up) isn't traced.
- **Errors are visible.** An exception in any step marks that step and the trace
  `ERROR` with the message, then re-raises.

## Langfuse v4 notes

This runs Langfuse server v4 ("events" storage). Its public API no longer serves
`GET /api/public/traces` or reading scores; read observations with the SDK's
`Langfuse().iter_observation_pages(trace_id=...)` (ask for the `time` field group
to get time-to-first-token). Writing scores works as before.
