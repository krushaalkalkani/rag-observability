# Eval runs are traced under the Langfuse environment "eval", so they never count
# as production traffic in the monitoring (which reads environment "default").
.PHONY: setup ingest ask serve ui golden ablation eval ci-local test langfuse-env langfuse-up langfuse-down langfuse-logs langfuse-reset traffic metrics dashboard baseline

# Load .env (Langfuse keys) into every `uv run` when it exists; without it tracing is simply off.
UV_RUN = uv run $(if $(wildcard .env),--env-file .env)

setup:          ## install Python + all dependencies into .venv
	uv sync

ingest:         ## download 10-Ks, parse, chunk, embed, store in Chroma (safe to re-run)
	uv run python -m rag.ingest

ask:            ## usage: make ask Q="What are Tesla's main risk factors?"
	$(UV_RUN) python -m rag.ask "$(Q)" --show-sources

serve:          ## API on http://localhost:8000 (docs at /docs)
	$(UV_RUN) uvicorn rag.api:app --port 8000

ui:             ## Streamlit UI on http://localhost:8501 (needs `make serve` running)
	uv run streamlit run app/ui.py

golden:         ## rebuild eval/golden_set.jsonl from eval/golden_seed.yaml
	uv run python eval/build_golden.py

ablation:       ## vector vs hybrid vs hybrid+rerank (+guardrails) -> eval/reports/
	LANGFUSE_TRACING_ENVIRONMENT=eval $(UV_RUN) python eval/ablation.py

test:
	uv run pytest -q

eval:           ## full evaluation on the golden set (RAGAS judge, ~$3.50)
	LANGFUSE_TRACING_ENVIRONMENT=eval $(UV_RUN) python eval/run_eval.py

ci-local:       ## what CI runs on a PR: fixture index + eval + compare with eval/baseline/main.json (~$2)
	RAG_CHROMA_DIR=data/chroma_ci uv run python -m rag.ingest --from-processed data/fixtures
	RAG_CHROMA_DIR=data/chroma_ci LANGFUSE_TRACING_ENVIRONMENT=eval $(UV_RUN) python eval/run_eval.py --tickers AAPL TSLA NVDA --name ci
	uv run python eval/compare.py eval/baseline/main.json eval/reports/eval_ci.json --comment eval/reports/pr_comment.md

baseline:       ## make the last `make ci-local` report the committed fallback baseline (run on main)
	mkdir -p eval/baseline && cp eval/reports/eval_ci.json eval/baseline/main.json

# ---------- Observability (Project 3) ----------
langfuse-env:   ## create .env with random secrets + Langfuse API keys (once)
	python3 scripts/init_env.py

langfuse-up: langfuse-env  ## start self-hosted Langfuse -> http://localhost:3000
	docker compose up -d
	@echo "Langfuse starting on http://localhost:3000 (first start ~1 min). Login is in .env"

langfuse-down:  ## stop Langfuse (traces are kept in Docker volumes)
	docker compose down

langfuse-logs:
	docker compose logs -f langfuse-web langfuse-worker

langfuse-reset: ## DELETE all Langfuse data (volumes) - asks no questions
	docker compose down -v

traffic:        ## simulated users: make traffic N=100 [RATE=6]  (~$0.0006/request)
	$(UV_RUN) python scripts/traffic.py --n $(or $(N),50) --per-minute $(or $(RATE),0)

metrics:        ## Langfuse -> data/metrics.db: collect, grade 10% (~$0.03 each), aggregate, alert
	$(UV_RUN) python -m rag.monitoring.job

dashboard:      ## metrics dashboard on http://localhost:8502
	$(UV_RUN) streamlit run app/dashboard.py --server.port 8502
