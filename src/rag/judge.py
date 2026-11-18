"""The LLM judge used to grade answers (RAGAS metrics).

Shared by the offline eval (eval/run_eval.py, golden set) and the online eval
(rag/monitoring/online_eval.py, a sample of live traffic), so both report
faithfulness measured the same way, by the same judge model.
"""


class UsageCounter:
    """Wraps the Anthropic client so we can report what the judge cost."""

    def __init__(self, client):
        self.input_tokens = self.output_tokens = self.calls = 0
        original = client.messages.create

        async def counted(*args, **kwargs):
            resp = await original(*args, **kwargs)
            self.calls += 1
            self.input_tokens += resp.usage.input_tokens
            self.output_tokens += resp.usage.output_tokens
            return resp

        client.messages.create = counted


def make_judge(model: str, effort: str):
    import instructor
    from anthropic import AsyncAnthropic
    from ragas.llms import llm_factory

    client = AsyncAnthropic()
    usage = UsageCounter(client)
    # JSON mode (not forced tool calls): Opus 5.5 rejects forced tool_choice.
    llm = llm_factory(
        model, provider="anthropic", client=client, mode=instructor.Mode.ANTHROPIC_JSON,
        max_tokens=8000, output_config={"effort": effort},
    )
    return llm, usage
