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

---

# Observability, Phase 2 — quality metrics over time

Tracing (Phase 1) answers "what happened in *this* request?". Phase 2 answers
"is the system getting worse, since when, and why?".

```
Langfuse ──► make metrics ──► data/metrics.db ──► make dashboard (http://localhost:8502)
              1 collect          requests  (one row per request)
              2 evaluate         metrics   (rolled up per hour + prompt_version)
              3 aggregate        alert_state
              4 alert ──► console + logs/alerts.jsonl (+ Slack if SLACK_WEBHOOK_URL is set)
```

## Commands

| Command | What it does |
|---|---|
| `make traffic N=100 RATE=6` | simulated users: golden + generated questions through the real pipeline, 6/min (~$0.0006 each) |
| `make metrics` | the monitoring job: pull new traces, grade the 10% sample, roll up, check alerts |
| `make dashboard` | the dashboard on <http://localhost:8502> |

In production `make metrics` would run on a schedule. Locally, e.g. hourly with cron:
`0 * * * * cd /path/to/03-rag-observability && make metrics >> logs/metrics-job.log 2>&1`

## The metrics

All metrics are defined once, in [`rag/monitoring/metrics.py`](../src/rag/monitoring/metrics.py)
(`summarize`), and used by the stored table, the alerts and the dashboard alike.

| Metric | Definition | Why |
|---|---|---|
| latency p50 / p95 | median / 95th percentile, end to end and per step (retrieval, rerank, LLM, first token) | an average hides the slow tail; p95 is what the unlucky 1-in-20 user gets |
| cost / request | LLM cost from token counts × the pricing table (refusals before the LLM cost $0) | catches prompt bloat and model swaps |
| citation coverage | mean share of an answer's sentences backed by the chunk they cite (answered requests) | the cheap, every-request grounding signal |
| fully grounded rate | share of answers where **every** sentence was backed | stricter version of the above |
| refusal rate | refused / requests that didn't error | too high = retrieval or prompt broke; too low = guardrails off |
| error rate | requests where any step failed / all requests | |
| faithfulness (sampled) | RAGAS faithfulness from the LLM judge on 10% of answered requests | the real hallucination metric — too expensive for every request |

**Online faithfulness without ground truth.** Live questions have no reference
answer, so correctness can't be checked, but faithfulness can: does each claim come
from the retrieved chunks? The job picks a stable 10% sample (by trace-id hash),
rebuilds the exact chunks from the trace, and grades with the same judge as the CI
eval (`eval.judge_model`), so online and offline numbers are comparable. Scores
also go back to Langfuse as a `faithfulness` score on the trace. Capped at
`max_per_run` per job run; ~$0.03 per graded answer.

## Alerts

Rules live in `configs/config.yaml` → `monitoring.alerts.rules` and are checked on
the last 60 minutes (only with ≥ 10 requests, so 2 bad requests out of 3 can't page
anyone). A notification goes out only when a rule changes state (FIRING / RESOLVED),
and a FIRING message carries the first clues: the metric split by `prompt_version`,
`config_hash` and `git_sha`, plus any version first seen in the window.

### Calibration

Thresholds were set from a 90-request baseline (`make traffic N=90 RATE=6`,
2026-11-18, `answer_v2`, `hybrid_rerank`, Haiku 5.5, laptop CPU re-ranker): far enough
beyond normal variation that noise doesn't fire them.

| Metric | Baseline | Threshold | Note |
|---|---|---|---|
| p95 latency | 5.8 s (p50 2.5 s) | > 8 s | the spec's example 3 s would fire constantly: the LLM step alone has p95 4.2 s |
| citation coverage | 95.0% | < 90% | very stable between windows |
| refusal rate | 30% (28–39% per window) | > 45% | follows what users ask; read it with `refusal_reason` |
| error rate | 0% | > 2% | |
| cost / request | $0.00053 | > $0.001 | ~2x: a model swap or prompt bloat |
| faithfulness (sampled) | 1.00 (n=4) | < 0.85, with ≥ 5 graded | `min_count` stops one bad grade from firing |

Re-calibrate after any intended change to the system (new model, new prompt version).

## Incident drill (runbook)

The point: prove the monitoring can find a real regression and trace it to the exact
change. Do it like a real incident: one person ships the bad change, then *uses only
the dashboard and Langfuse* to find it.

1. **Baseline.** `make traffic N=60 RATE=6`, then `make metrics`. The dashboard is
   green.
2. **Ship a bad change, and commit it**, so `git_sha` and `config_hash` record it.
   Suggestions, from obvious to subtle:
   - `retrieval.top_k: 5 → 2`: fewer chunks, so more refusals and lower coverage;
   - `retrieval.mode: hybrid_rerank → vector`: the re-ranker is off, so worse chunks
     and lower faithfulness, but *faster*;
   - `prompt.version: answer_broken_demo`: the grounding rules are gone.
3. **Generate traffic.** `make traffic N=60 RATE=6`, then `make metrics`. Note the
   time the alert fired.
4. **Investigate from the dashboard:** which metric moved, when, which dashed "first
   seen" line it lines up with, the version comparison, then open the worst requests
   in Langfuse and compare their `retrieval` / `rerank` / `citation-check` steps with
   a healthy trace.
5. **Roll back** (revert the commit), run more traffic, and watch the alert RESOLVE.
6. **Write `docs/incident-001.md`:** summary, timeline (UTC), detection (which alert,
   how long after the change), root cause (the exact config line + commit), evidence
   (dashboard screenshot and two trace links: bad vs good), fix, and what would have
   caught it sooner (Phase 3's CI gate).

---

# Observability, Phase 3 — regression gating in CI

Monitoring (Phase 2) finds a regression *after* users hit it. The gate stops it
*before* merge: every pull request runs the golden-set eval and is compared with
`main`. Incident 001 is why it matters: that change made every online quality metric
look *better*, but it dropped retrieval hit rate, and the gate measures hit rate
offline against known answers.

```
push to main ──► eval ──► artifact "eval-baseline"
                                  │
pull request ──► eval ──► eval/compare.py ◄──┘ ──► PR comment (metric, main, PR, Δ, allowed)
                                  └──► job fails if a metric regressed
```

## What fails a PR

| Check | Rule | Why this number |
|---|---|---|
| hit_rate@5 | may not drop > 2 pts | retrieval is deterministic (0 change between identical runs), so losing a single question fails |
| faithfulness | may not drop > 3 pts | judge noise between identical runs: 0.3 pts |
| citation_coverage | may not drop > 3 pts | noise: 1.2 pts |
| refusal_accuracy | may not drop > 5 pts | one of 39 questions may flip, two may not |
| p95 latency | may not grow > 50% | identical runs differed by +29% (39 calls to a shared API); only compared when both ran on the same kind of machine |
| absolute floors | `eval.thresholds` (Project 1) | still apply |

Tolerances live in `configs/config.yaml` → `eval.regression`, calibrated on two runs
of the same system (2026-11-17 vs 2026-11-18). Comparing to `main` catches what fixed
floors miss: 0.99 → 0.88 clears a 0.85 floor but is an 11-point regression.

## Versioning: every report knows what it measured

Each eval report records `prompt_version`, `prompt_hash` (the prompt file's content),
`config_hash`, `git_sha` and `runner`. The PR comment starts with "what this PR
changes", and it warns when a prompt file was **edited without bumping its version**,
which would make old and new traces indistinguishable.

## Running it

| Command | What it does |
|---|---|
| `make ci-local` | exactly what CI runs on a PR, compared with the committed `eval/baseline/main.json` (~$2) |
| `make baseline` | after `make ci-local` on main: make that report the committed fallback baseline |
| `uv run python eval/compare.py A.json B.json` | compare any two eval reports |

On GitHub the baseline comes from the latest successful `main` run's `eval-baseline`
artifact; the committed file is only the fallback before `main` has run once. The
committed baseline ran on a laptop, so latency is not gated against it, only against
a CI baseline.
