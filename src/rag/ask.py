"""Command-line interface.

    python -m rag.ask "What are Tesla's main risk factors?"
    python -m rag.ask "..." --show-sources       # print the full text of each cited chunk
    python -m rag.ask "..." --debug              # retrieval scores + per-sentence guardrail checks
    python -m rag.ask "..." --mode vector --no-guardrails   # compare against Phase 1 behaviour
    python -m rag.ask --chunk TSLA-2025-00042    # print one chunk by id
"""

import argparse
import textwrap

from rag import tracing
from rag.index import get_chunk
from rag.pipeline import answer_question


def print_chunk(c: dict) -> None:
    print(f"\n=== {c['chunk_id']} | {c['company']} FY{c['fiscal_year']} | {c['section']} | pages {c['page_start']}-{c['page_end']}")
    print(f"    source: {c['source_url']}")
    print(textwrap.indent(c["text"], "    "))


def main():
    parser = argparse.ArgumentParser(description="Ask a question about the 10-K filings.")
    parser.add_argument("question", nargs="?")
    parser.add_argument("--mode", choices=["vector", "bm25", "hybrid", "hybrid_rerank"], help="retrieval mode (default: config)")
    parser.add_argument("--no-guardrails", action="store_true", help="skip refusal gate + citation enforcement")
    parser.add_argument("--prompt", help="prompt version, e.g. answer_v1 (default: config)")
    parser.add_argument("--show-sources", action="store_true", help="print the full text of every cited chunk")
    parser.add_argument("--debug", action="store_true", help="show retrieval scores and sentence checks")
    parser.add_argument("--chunk", help="print a single chunk by id and exit")
    args = parser.parse_args()

    if args.chunk:
        chunk = get_chunk(args.chunk)
        print_chunk(chunk) if chunk else print(f"No chunk with id {args.chunk}")
        return
    if not args.question:
        parser.error("give a question, or --chunk ID")

    r = answer_question(args.question, mode=args.mode, enforce=False if args.no_guardrails else None, prompt_version=args.prompt)

    if args.debug:
        print("RETRIEVED")
        for c in r["retrieved"]:
            scores = "  ".join(f"{k.replace('_score', '')}={c[k]}" for k in c if k.endswith("_score") and c[k] is not None)
            print(f"  {c['chunk_id']:18s} {c['section'][:40]:40s} {scores}")
        print("\nSENTENCE CHECKS")
        for s in r["sentence_checks"]:
            print(f"  {'OK ' if s['supported'] else 'DROP'} {s['reason']:32s} {s['sentence'][:90]}")
        print()

    print("ANSWER\n" + r["answer"] + "\n")
    if r["refused"]:
        print(f"(refused: {r['refusal_reason']})")
    else:
        print("CITATIONS")
        for c in r["citations"]:
            print(f"  [{c['chunk_id']}] {c['company']} | FY{c['fiscal_year']} | {c['section']} | pages {c['page_start']}-{c['page_end']}")
    print(
        f"\nprompt={r['prompt_version']}  model={r['model']}  mode={r['retrieval_mode']}  "
        f"guardrails={r['guardrails']}  latency={r['latency_ms']}ms (retrieval {r['retrieval_ms']}ms)"
    )
    if r["cost_usd"] is not None:
        print(f"tokens in/out={r['input_tokens']}/{r['output_tokens']}  cost=${r['cost_usd']:.6f}  ttft={r['ttft_ms']}ms")
    tracing.flush()  # a CLI exits right away: send the trace before it does
    if url := tracing.trace_url(r["trace_id"]):
        print(f"trace: {url}")
    if args.show_sources:
        for c in r["citations"]:
            print_chunk(c)


if __name__ == "__main__":
    main()
