"""Prompt loading (versioned) + building the prompt from chunks + calling the LLM."""

from datetime import datetime, timezone
from functools import lru_cache

import yaml

from rag import tracing
from rag.config import PROMPTS_DIR, load_config
from rag.guardrails import CHUNK_ID_RE
from rag.llm import get_llm

CITATION_FIELDS = ("chunk_id", "company", "ticker", "fiscal_year", "section", "page_start", "page_end", "source_url", "text")


@lru_cache
def load_prompt(version: str | None = None) -> dict:
    """Prompts live in configs/prompts/<version>.yaml with version/description/system/template."""
    version = version or load_config()["prompt"]["version"]
    with open(PROMPTS_DIR / f"{version}.yaml") as f:
        prompt = yaml.safe_load(f)
    assert prompt["version"] == version, f"{version}.yaml declares version {prompt['version']!r}"
    return prompt


def prompt_hash(version: str | None = None) -> str:
    """Fingerprint of the prompt file's CONTENT. Two runs with the same prompt_version
    but different hashes mean someone edited a prompt in place without bumping its
    version - the CI comparison flags that."""
    import hashlib

    version = version or load_config()["prompt"]["version"]
    return hashlib.sha256((PROMPTS_DIR / f"{version}.yaml").read_bytes()).hexdigest()[:12]


def format_context(chunks: list[dict]) -> str:
    parts = []
    for c in chunks:
        pages = f"p.{c['page_start']}" if c["page_start"] == c["page_end"] else f"pp.{c['page_start']}-{c['page_end']}"
        header = f"[{c['chunk_id']}] {c['company']} ({c['ticker']}) | FY{c['fiscal_year']} | {c['section']} | {pages}"
        parts.append(f"{header}\n{c['text']}")
    return "\n\n---\n\n".join(parts)


@lru_cache
def _llm():
    return get_llm()


def generate_answer(question: str, chunks: list[dict], prompt_version: str | None = None) -> dict:
    prompt = load_prompt(prompt_version)
    llm = _llm()
    system = prompt["system"]
    user = prompt["template"].format(context=format_context(chunks), question=question)

    # A "generation" observation is a span with LLM-specific fields: Langfuse shows
    # the exact messages sent, the output, tokens, cost, and time-to-first-token.
    with tracing.observe(
        "llm-generation",
        as_type="generation",
        model=llm.model,
        model_parameters=llm.parameters,
        input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        version=prompt["version"],
        metadata={"prompt_version": prompt["version"], "provider": llm.name.split(":")[0]},
    ) as gen:
        t0 = datetime.now(timezone.utc)
        res = llm.generate(system, user)
        cost = tracing.estimate_cost(llm.model, res.input_tokens, res.output_tokens)
        ttft_ms = round((res.first_token_at - t0).total_seconds() * 1000) if res.first_token_at else None
        usage = {}
        if res.input_tokens is not None and res.output_tokens is not None:
            usage = {"input": res.input_tokens, "output": res.output_tokens, "total": res.input_tokens + res.output_tokens}
        gen.update(
            output=res.text,
            usage_details=usage or None,
            cost_details=cost,
            completion_start_time=res.first_token_at,  # Langfuse computes time-to-first-token from this
            metadata={"stop_reason": res.stop_reason, "ttft_ms": ttft_ms},
        )
    return {
        "answer": res.text,
        "prompt_version": prompt["version"],
        "model": llm.name,
        "input_tokens": res.input_tokens,
        "output_tokens": res.output_tokens,
        "cost_usd": cost["total"] if cost else None,
        "ttft_ms": ttft_ms,
    }


def extract_citations(answer: str, chunks: list[dict]) -> list[dict]:
    """Structured citations for every retrieved chunk id that appears in the answer."""
    by_id = {c["chunk_id"]: c for c in chunks}
    ids = dict.fromkeys(CHUNK_ID_RE.findall(answer))
    return [{k: by_id[i][k] for k in CITATION_FIELDS} for i in ids if i in by_id]
