# Project 3 — Monitoring & Observability for the RAG System

> **For Claude (the builder):** This project **extends Project 1 (`01-production-rag`)** — it must be built inside that same codebase (or a fork of it), not from scratch. If Project 1 isn't present, stop and say so. Build phase by phase; after each phase, stop, show how to run it, and explain what was built in simple language — the owner (Krushal) is learning through this project.

## 1. What we are building
An **observability layer** for the RAG system: full request tracing, quality and cost metrics over time, a dashboard, alerting on degradation, and **regression gating** in CI.

The goal deliverable: *"When someone asks 'what happened when quality degraded last Tuesday?', you open a dashboard, point to the anomaly and walk through the root cause."*

## 2. Why it matters
In production, building the system is ~30% of the work; the other ~70% is knowing whether it works, why it fails, and fixing it fast. Almost no portfolio shows this. It signals system-level thinking — exactly the gap AI teams are hiring for.

## 3. Tech stack
| Layer | Choice | Notes |
|---|---|---|
| Tracing | **Langfuse (self-hosted via Docker)** | open source, no usage limits; LangSmith/Braintrust are alternatives |
| Metrics store | Langfuse + a local SQLite/Postgres table for aggregates | |
| Dashboard | Langfuse UI + a custom Streamlit/Grafana page for p50/p95, cost, citation coverage | |
| Online eval | Lightweight faithfulness/citation checks on a sample of live traffic | |
| CI | GitHub Actions (reuse Project 1's eval) | |
| Load/traffic | a script replaying the golden set + synthetic questions to generate history | |

## 4. Architecture
```
 request ─► [trace start]
             ├─ span: hybrid retrieval  (query, BM25 ids, vector ids, scores)
             ├─ span: rerank            (before/after order, scores)
             ├─ span: guardrail         (supported? refused?)
             ├─ span: LLM generation    (prompt_version, full prompt, output, tokens, cost, TTFT)
             └─ [trace end] total latency, citation coverage, error?
                     │
                     ▼
               Langfuse ─► dashboards / alerts ─► nightly aggregate job ─► metrics table
                                                                │
 GitHub PR ─► eval on golden set ─► compare to baseline ─► pass / FAIL build
```

## 5. Phases

### Phase 1 — Tracing every step
Tasks:
1. `docker compose` for self-hosted Langfuse; document setup.
2. Instrument every pipeline stage as a span: retrieved chunk ids + scores, re-ranker reordering, final prompt (with `prompt_version`), response, token counts, model name, cost estimate, errors.
3. Attach metadata: `prompt_version`, `config_hash`, `git_sha`, user/session id (anonymous).
4. Optional: user feedback (👍/👎) in the UI stored as a Langfuse score.

Acceptance: open any request in Langfuse and see exactly what was retrieved, how it was re-ranked, what was sent to the LLM and what came back.

### Phase 2 — Quality metrics over time
Track and chart:
- **Latency p50 and p95** (not just average — averages hide worst cases), per stage and end-to-end
- **Cost per request** in dollars (token pricing table in config; $0 for local models but still track tokens)
- **Citation coverage** (% of answers fully grounded)
- **Refusal rate** and **failure/error rate**
- **Sampled online faithfulness** (e.g. 10% of live requests)

Tasks:
1. Aggregation job (hourly/daily) → metrics table.
2. Dashboard page with time-series charts + per-`prompt_version` comparison.
3. **Alert rules** (e.g. p95 > 3s, citation coverage < 90%, error rate > 2%) → log/console/Slack webhook (optional).
4. **Incident drill:** deliberately ship a bad change (e.g. smaller top-k or a worse prompt), generate traffic, then use the dashboard to find and explain the root cause. Write it up as a short **postmortem** in `docs/incident-001.md`.

Acceptance: the dashboard clearly shows the injected degradation and the postmortem traces it to the exact change.

### Phase 3 — Regression gating
Tasks:
1. Golden-set eval runs on every PR (from Project 1); results compared against the stored baseline from `main`.
2. Build **fails** if faithfulness, hit rate, citation coverage drop beyond thresholds, or p95 latency regresses beyond X%.
3. CI posts a summary table as a PR comment (metric, baseline, new, delta).
4. Prompts and configs versioned alongside code; each eval report records `prompt_version` + `git_sha`.

Acceptance: a PR changing only a prompt can fail CI with a clear explanation.

## 6. README deliverables
- Screenshot of a full trace
- Dashboard screenshot (p50/p95, cost, coverage over time)
- The incident postmortem
- Example failing PR with CI comment

## 7. Definition of done
- [ ] Every request fully traced in self-hosted Langfuse
- [ ] p50/p95 latency, cost/request, citation coverage, refusal & error rate tracked over time
- [ ] Alert rules
- [ ] Incident drill + written postmortem
- [ ] CI regression gate with PR comment

## 8. Resume bullet template
- Added end-to-end observability to a RAG system with self-hosted Langfuse tracing, tracking p50/p95 latency, cost per request and citation coverage; diagnosed an injected quality regression to root cause in **under X minutes**.
- Built CI regression gating that blocks merges when faithfulness or retrieval metrics drop beyond threshold, catching **N** regressions during development.

## 9. Interview questions this prepares you for
- Why p95 instead of average latency?
- What do you log for an LLM request and why?
- How do you monitor quality when there's no ground truth in production?
- Walk me through debugging a quality drop.
- How do you treat prompt changes safely?
