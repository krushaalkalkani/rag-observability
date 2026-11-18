.PHONY: setup ingest ask serve ui golden ablation eval ci-local test langfuse-env langfuse-up langfuse-down langfuse-logs langfuse-reset

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
	uv run python eval/ablation.py

test:
	uv run pytest -q

eval:           ## full evaluation on the golden set (RAGAS judge, ~$3.50)
	$(UV_RUN) python eval/run_eval.py

ci-local:       ## exactly what GitHub Actions runs: fixture index + gated eval (~$2)
	RAG_CHROMA_DIR=data/chroma_ci uv run python -m rag.ingest --from-processed data/fixtures
	RAG_CHROMA_DIR=data/chroma_ci uv run python eval/run_eval.py --tickers AAPL TSLA NVDA --name ci --gate

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
