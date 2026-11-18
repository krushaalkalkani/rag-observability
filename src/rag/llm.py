"""LLM provider abstraction: the rest of the code calls `get_llm().generate(...)`
and never cares whether the model is Claude (API) or a local Ollama model.

`generate` returns an LLMResult: the text PLUS what observability needs -
token counts (for cost) and when the first token arrived (time-to-first-token,
the latency a user actually feels in a streaming UI).
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from rag.config import load_config
from rag.guardrails import REFUSAL


@dataclass
class LLMResult:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    first_token_at: datetime | None = None  # wall-clock time of the first output token
    stop_reason: str | None = None


class AnthropicLLM:
    def __init__(self, model: str, max_tokens: int, effort: str = "low"):
        import anthropic

        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
        self.model, self.max_tokens, self.effort = model, max_tokens, effort
        self.name = f"anthropic:{model}"
        self.parameters = {"max_tokens": max_tokens, "effort": effort}

    def generate(self, system: str, user: str) -> LLMResult:
        # Streaming lets us timestamp the first token. We still return the full text.
        first_token_at = None
        with self.client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.effort},
        ) as stream:
            for _ in stream.text_stream:
                if first_token_at is None:
                    first_token_at = datetime.now(timezone.utc)
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":  # the API's own safety classifier declined
            text = REFUSAL
        else:
            text = "".join(block.text for block in msg.content if block.type == "text").strip()
        return LLMResult(text, msg.usage.input_tokens, msg.usage.output_tokens, first_token_at, msg.stop_reason)


class OllamaLLM:
    def __init__(self, model: str, host: str):
        self.model, self.host = model, host.rstrip("/")
        self.name = f"ollama:{model}"
        self.parameters = {"temperature": 0}

    def generate(self, system: str, user: str) -> LLMResult:
        import requests

        resp = requests.post(
            f"{self.host}/api/chat",
            json={
                "model": self.model,
                "stream": False,
                "options": self.parameters,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
            timeout=300,
        )
        resp.raise_for_status()
        data = resp.json()
        # Ollama reports token counts too: prompt_eval_count (input) and eval_count (output).
        return LLMResult(data["message"]["content"].strip(), data.get("prompt_eval_count"), data.get("eval_count"),
                         stop_reason=data.get("done_reason"))


def get_llm():
    cfg = load_config()["llm"]
    if cfg["provider"] == "anthropic":
        return AnthropicLLM(**cfg["anthropic"])
    if cfg["provider"] == "ollama":
        return OllamaLLM(**cfg["ollama"])
    raise ValueError(f"Unknown llm.provider: {cfg['provider']!r} (use 'anthropic' or 'ollama')")
