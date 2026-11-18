"""Turn eval/golden_seed.yaml into eval/golden_set.jsonl.

For every answerable question, source_chunk_ids = all chunks whose text contains
the evidence quote (usually 1, sometimes 2 because neighbouring chunks overlap).
Fails loudly if a quote is not found, so a bad label can never slip in silently.

    uv run python eval/build_golden.py
"""

import glob
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def main():
    seed = yaml.safe_load(open(ROOT / "eval" / "golden_seed.yaml"))
    chunks = [json.loads(line) for f in sorted(glob.glob(str(ROOT / "data/processed/*.chunks.jsonl"))) for line in open(f)]
    normed = [(c["chunk_id"], norm(c["text"])) for c in chunks]

    rows, missing = [], []
    for i, item in enumerate(seed):
        row = {
            "id": f"q{i + 1:03d}",
            "question": item["question"],
            "reference_answer": item.get("reference_answer", ""),
            "source_chunk_ids": [],
            "should_refuse": bool(item.get("should_refuse", False)),
        }
        if not row["should_refuse"]:
            # evidence may be one quote or a list (when a fact is stated in several places)
            quotes = item["evidence"] if isinstance(item["evidence"], list) else [item["evidence"]]
            row["evidence"] = quotes
            for quote in quotes:
                found = [cid for cid, text in normed if norm(quote) in text]
                if not found:
                    missing.append(f"{item['question']}  <-  {quote!r}")
                row["source_chunk_ids"] += [cid for cid in found if cid not in row["source_chunk_ids"]]
        rows.append(row)

    if missing:
        sys.exit("Evidence not found for:\n  " + "\n  ".join(missing))
    with open(ROOT / "eval" / "golden_set.jsonl", "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    n_ans = sum(not r["should_refuse"] for r in rows)
    print(f"Wrote {len(rows)} questions ({n_ans} answerable, {len(rows) - n_ans} should-refuse) to eval/golden_set.jsonl")


if __name__ == "__main__":
    main()
