"""Regression gate: compare a PR's eval report against the baseline from main.

    python eval/compare.py BASELINE.json NEW.json [--comment comment.md]
    exit code 0 = pass, 1 = fail

A PR fails when, compared to main:
  - a gated quality metric drops by more than its allowed amount
    (eval.regression.max_drop in configs/config.yaml), or
  - p95 latency grows by more than eval.regression.max_p95_latency_increase
    (only when both runs used the same kind of machine - laptop vs CI isn't comparable), or
  - any metric is below its absolute threshold (eval.thresholds, the Project 1 gate).

It writes a Markdown summary for the PR comment: what changed (prompt / config /
code), the metric table (main, this PR, delta, allowed), why it failed in plain
words, and the individual questions that got worse - so a failing build explains
itself without anyone opening a log.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

from rag.config import load_config  # noqa: E402

MARKER = "<!-- eval-gate -->"  # lets CI find and update its own comment instead of adding a new one
SHOWN = ["hit_rate@5", "mrr@5", "context_recall", "faithfulness", "answer_correctness", "citation_coverage",
         "refusal_accuracy", "false_refusal_rate", "p95_latency_ms"]
LOWER_IS_BETTER = {"false_refusal_rate", "p95_latency_ms"}


def _common(base: dict, new: dict) -> tuple[dict, dict, str | None]:
    """If the PR changed the golden set, compare only the questions both runs share."""
    b_ids, n_ids = {r["id"] for r in base["rows"]}, {r["id"] for r in new["rows"]}
    if b_ids == n_ids:
        return base["metrics"], new["metrics"], None
    from run_eval import aggregate  # same definitions as the eval itself

    keep = b_ids & n_ids
    note = (f"The question set changed ({len(b_ids)} on main, {len(n_ids)} here): metrics are compared "
            f"on the {len(keep)} questions both runs share.")
    return (aggregate([r for r in base["rows"] if r["id"] in keep]),
            aggregate([r for r in new["rows"] if r["id"] in keep]), note)


def compare(base: dict, new: dict, reg: dict) -> dict:
    bm, nm, note = _common(base, new)
    rows, failures, notes = [], [], [note] if note else []
    same_machine = base.get("runner") == new.get("runner")
    for k in SHOWN:
        b, n = bm.get(k), nm.get(k)
        row = {"metric": k, "base": b, "new": n, "delta": None if b is None or n is None else n - b, "allowed": None, "ok": None}
        if k in reg["max_drop"]:
            row["allowed"] = -reg["max_drop"][k]
            drop = (b or 0) - (n or 0)  # a metric that disappeared (None) counts as 0
            row["ok"] = drop <= reg["max_drop"][k] + 1e-9
            if not row["ok"] and n is None:
                failures.append(f"**{k}** could not be measured (was {_fmt(k, b)} on main): "
                                "no question was answered, so there was nothing to grade")
            elif not row["ok"]:
                failures.append(f"**{k}** dropped from {_fmt(k, b)} to {_fmt(k, n)} "
                                f"({_delta(k, b, n)}; at most {reg['max_drop'][k] * 100:.0f} points allowed)")
        elif k == "p95_latency_ms" and b and n:
            row["allowed"] = reg["max_p95_latency_increase"]
            if same_machine:
                row["ok"] = n / b - 1 <= reg["max_p95_latency_increase"]
                if not row["ok"]:
                    failures.append(f"**p95 latency** grew from {b:,.0f} ms to {n:,.0f} ms (+{n / b - 1:.0%}; "
                                    f"at most +{reg['max_p95_latency_increase']:.0%} allowed)")
            else:
                notes.append(f"p95 latency not gated: main ran on `{base.get('runner')}`, this run on "
                             f"`{new.get('runner')}` - different machines aren't comparable.")
        rows.append(row)

    regressed = {r["metric"] for r in rows if r["ok"] is False}
    for f in new.get("gate_failures", []):  # Project 1's absolute floors still apply
        if f.split(" = ")[0] not in regressed:   # already explained above
            failures.append(f"below the absolute threshold: {f}")
    if (base.get("prompt_version") == new.get("prompt_version") and base.get("prompt_hash") and new.get("prompt_hash")
            and base["prompt_hash"] != new["prompt_hash"]):
        notes.append(f"⚠️ The prompt file `{new['prompt_version']}` was edited **without bumping its version**. "
                     "Give changed prompts a new version so traces and reports can tell them apart.")
    for label, rep_ in (("main", base), ("this PR", new)):
        errors = rep_.get("judge", {}).get("errors") or []
        if errors:
            notes.append(f"⚠️ {len(errors)} judge call(s) failed on {label} ({errors[0].split(':')[1].strip()}, ...): "
                         "LLM-judged metrics are averaged over fewer questions. Re-run if a result is borderline.")
    return {"rows": rows, "failures": failures, "notes": notes, "worse": worse_questions(base, new)}


def worse_questions(base: dict, new: dict, limit: int = 8) -> list[dict]:
    """Questions whose result got worse, most severe first."""
    by_id = {r["id"]: r for r in base["rows"]}
    out = []
    for n in new["rows"]:
        b = by_id.get(n["id"])
        if not b:
            continue
        why, severity = [], 0
        if not n["should_refuse"]:
            if b["hit_rank"] and not n["hit_rank"]:
                why.append(f"source chunk no longer retrieved (was rank {b['hit_rank']})"); severity += 3
            elif b["hit_rank"] and n["hit_rank"] and n["hit_rank"] > b["hit_rank"]:
                why.append(f"source chunk rank {b['hit_rank']} → {n['hit_rank']}"); severity += 1
            if n["refused"] and not b["refused"]:
                why.append("now refused (answerable question)"); severity += 3
        elif b["refused"] and not n["refused"]:
            why.append("answered a question it must refuse"); severity += 4
        for k, label, step in (("faithfulness", "faithfulness", 0.2), ("citation_coverage", "citation coverage", 0.25),
                               ("answer_correctness", "correctness", 0.3)):
            bv, nv = b.get(k), n.get(k)
            if bv is not None and (nv is None and not n["refused"] or nv is not None and bv - nv >= step):
                why.append(f"{label} {_pct(bv)} → {_pct(nv)}"); severity += 2
        if why:
            out.append({"id": n["id"], "question": n["question"], "why": "; ".join(why), "severity": severity})
    return sorted(out, key=lambda r: -r["severity"])[:limit]


# ---------- formatting ----------
def _pct(x):
    return "–" if x is None else f"{x:.0%}"


def _fmt(k, x):
    if x is None or x != x:  # None or NaN
        return "–"
    return f"{x:,.0f} ms" if k.endswith("_ms") else f"{x:.2f}" if k == "mrr@5" else f"{x:.1%}"


def _delta(k, b, n):
    if b is None or n is None or b != b or n != n:
        return "–"
    if k.endswith("_ms"):
        return f"{n / b - 1:+.0%}"
    return f"{(n - b) * 100:+.1f} pts"


def to_markdown(base: dict, new: dict, res: dict) -> str:
    failed = bool(res["failures"])
    head = (f"## {'❌' if failed else '✅'} Eval gate: {'FAILED' if failed else 'passed'}"
            + (f" — {len(res['failures'])} problem{'s' if len(res['failures']) > 1 else ''} vs `main`" if failed else " — no regressions vs `main`"))
    changes = []
    for key, label in (("prompt_version", "prompt"), ("prompt_hash", "prompt file"), ("config_hash", "config"), ("git_sha", "code")):
        b, n = base.get(key), new.get(key)
        if b is None and n is None:
            continue  # older reports don't record it
        changes.append(f"{label} `{b or '–'}` → **`{n or '–'}`**" if b != n else f"{label} `{n}` (unchanged)")
    lines = [MARKER, head, "", "**What this PR changes:** " + " · ".join(changes), ""]
    if failed:
        lines += ["### Why it failed", ""] + [f"- {f}" for f in res["failures"]] + [""]
    lines += ["| Metric | main | this PR | Δ | allowed | |", "|---|---|---|---|---|---|"]
    for r in res["rows"]:
        k = r["metric"]
        allowed = ("" if r["allowed"] is None else f"+{r['allowed']:.0%}" if k.endswith("_ms")
                   else f"{r['allowed'] * 100:.0f} pts")
        mark = {True: "✅", False: "❌", None: ""}[r["ok"]]
        lines.append(f"| {k} | {_fmt(k, r['base'])} | {_fmt(k, r['new'])} | {_delta(k, r['base'], r['new'])} | {allowed} | {mark} |")
    if res["worse"]:
        lines += ["", f"### Questions that got worse ({len(res['worse'])} shown)", "", "| id | question | what changed |", "|---|---|---|"]
        lines += [f"| {w['id']} | {w['question'][:80]} | {w['why']} |" for w in res["worse"]]
    if res["notes"]:
        lines += ["", "### Notes", ""] + [f"- {n}" for n in res["notes"]]
    lines += ["", f"<sub>{new['n_questions']} golden questions ({', '.join(new.get('tickers') or ['all'])}) · "
                  f"model `{new['model']}` · judge `{new['judge']['model']}` · baseline from `main` at `{base.get('git_sha')}` "
                  f"({base.get('timestamp')}, `{base.get('runner')}`) · tolerances in `configs/config.yaml` → `eval.regression`</sub>"]
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description="Compare an eval report against the baseline from main.")
    p.add_argument("baseline", type=Path)
    p.add_argument("new", type=Path)
    p.add_argument("--comment", type=Path, help="write the PR comment Markdown here")
    args = p.parse_args()

    base, new = json.loads(args.baseline.read_text()), json.loads(args.new.read_text())
    res = compare(base, new, load_config()["eval"]["regression"])
    md = to_markdown(base, new, res)
    if args.comment:
        args.comment.write_text(md)
    print(md)
    sys.exit(1 if res["failures"] else 0)


if __name__ == "__main__":
    main()
