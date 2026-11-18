"""Generate realistic traffic so the monitoring has history to show.

    uv run --env-file .env python scripts/traffic.py --n 100            (or: make traffic N=100)
    uv run --env-file .env python scripts/traffic.py --n 60 --per-minute 6   spread over ~10 min

Each request goes through the real pipeline (retrieve -> rerank -> LLM -> guardrails)
and is traced in Langfuse exactly like a user's request. The question mix:
  50% golden-set questions (eval/golden_set.jsonl, includes some unanswerable ones)
  40% generated questions about the 10 companies' 10-Ks
  10% questions the system SHOULD refuse (future predictions, off-topic)
spread across a handful of simulated anonymous users.

Cost: ~$0.0006 per request with claude-haiku-5-5 (100 requests ~ $0.06).
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rag import tracing  # noqa: E402
from rag.pipeline import answer_question  # noqa: E402
from rag.retrieve import retrieve  # noqa: E402

COMPANIES = {"Apple": 2026, "Microsoft": 2026, "NVIDIA": 2026, "Tesla": 2025, "Amazon": 2025,
             "Alphabet": 2025, "Meta": 2025, "JPMorgan": 2025, "Netflix": 2025, "AMD": 2025}
ANSWERABLE = [
    "What was {c}'s total revenue in fiscal {fy}?",
    "How many employees did {c} have at the end of fiscal {fy}?",
    "What are the main risk factors {c} describes in its {fy} 10-K?",
    "How much did {c} spend on research and development in fiscal {fy}?",
    "What was {c}'s net income for fiscal {fy}?",
    "How does {c} describe its competition?",
    "What legal proceedings does {c} disclose?",
]
UNANSWERABLE = [
    "What will {c}'s stock price be next year?",
    "Who will be {c}'s CEO in 2035?",
    "Write a poem about {c}.",
    "What is the weather in New York today?",
]


def make_questions(n: int, rng: random.Random) -> list[str]:
    golden = [json.loads(line)["question"] for line in open(ROOT / "eval" / "golden_set.jsonl")]
    out = []
    for _ in range(n):
        c, fy = rng.choice(list(COMPANIES.items()))
        roll = rng.random()
        if roll < 0.5:
            out.append(rng.choice(golden))
        elif roll < 0.9:
            out.append(rng.choice(ANSWERABLE).format(c=c, fy=fy))
        else:
            out.append(rng.choice(UNANSWERABLE).format(c=c, fy=fy))
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--n", type=int, default=50, help="number of requests")
    p.add_argument("--per-minute", type=float, default=0, help="pace requests (0 = as fast as possible)")
    p.add_argument("--users", type=int, default=8, help="number of simulated anonymous users")
    p.add_argument("--seed", type=int, default=None, help="random seed (default: different every run)")
    args = p.parse_args()

    if tracing.get_langfuse() is None:
        sys.exit("Tracing is off - run with the .env loaded (make traffic), or there's nothing to monitor.")
    rng = random.Random(args.seed)
    questions = make_questions(args.n, rng)
    users = [f"anon-sim{rng.randrange(16**6):06x}" for _ in range(args.users)]

    print(f"Loading models... then {args.n} requests" + (f" at {args.per_minute}/min" if args.per_minute else ""), flush=True)
    retrieve("warm up", "hybrid_rerank")  # outside a trace: not recorded
    gap = 60 / args.per_minute if args.per_minute else 0
    refused = errors = 0
    cost = 0.0
    for i, q in enumerate(questions, 1):
        t0 = time.monotonic()
        user = rng.choice(users)
        try:
            r = answer_question(q, user_id=user, session_id=user)
            refused += r["refused"]
            cost += r["cost_usd"] or 0
            status = "refused" if r["refused"] else "ok"
            print(f"[{i:>3}/{args.n}] {r['latency_ms']:>6} ms  {status:8} {q[:70]}", flush=True)
        except Exception as e:  # the trace records the error; keep the traffic going
            errors += 1
            print(f"[{i:>3}/{args.n}]   ERROR  {type(e).__name__}: {str(e)[:80]}", flush=True)
        if gap:
            time.sleep(max(0.0, gap - (time.monotonic() - t0)))
    tracing.flush()
    print(f"\nDone: {args.n} requests, {refused} refused, {errors} errors, LLM cost ${cost:.4f}. "
          "Now run `make metrics` to update the dashboard.")


if __name__ == "__main__":
    main()
