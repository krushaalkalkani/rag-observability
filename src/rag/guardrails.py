"""Citation enforcement: make sure every claim in an answer is backed by a source.

Three layers, cheapest first:
  1. Retrieval gate  - if the best re-rank score is tiny, nothing relevant was
                       found: refuse without even calling the LLM.
  2. Model refusal   - the prompt tells the LLM to refuse when excerpts don't help.
  3. Sentence check  - after generation, split the answer into sentences. A claim
                       sentence survives only if it (a) cites a chunk we actually
                       retrieved and (b) is supported by that chunk: every number
                       in it appears in the chunk, and most of its content words do.
                       Unsupported sentences are removed; if too few survive, the
                       whole answer becomes a refusal.

The support check is deliberately simple (word + number overlap, no extra LLM
call): it is fast, free, deterministic, and easy to explain. It catches the
classic hallucinations - wrong numbers, invented ids, uncited claims. Phase 3
measures faithfulness properly with an LLM judge (RAGAS).
"""

import re

from rag.index import embedding_text

REFUSAL = "I can't answer that from the documents."
CHUNK_ID_RE = re.compile(r"\b([A-Z][A-Z.]{0,5}-\d{4}-\d{5})\b")
CITATION_GROUP_RE = re.compile(r"\s*(\[\s*[A-Z][A-Z.]{0,5}-\d{4}-\d{5}\s*(?:[,;]\s*[A-Z][A-Z.]{0,5}-\d{4}-\d{5}\s*)*\])+")
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Abbreviations whose "." must not end a sentence.
ABBREVIATIONS = ["U.S.", "U.K.", "Inc.", "Corp.", "Co.", "Ltd.", "No.", "e.g.", "i.e.", "Mr.", "Ms.", "Dr.", "vs.", "approx.", "Int'l.", "N.A."]
STOPWORDS = set(
    "a an and are as at be been being but by can could did do does for from had has have how if in into is it its "
    "may might more most of on or our over such than that the their them then there these they this those to "
    "under up was we were what when where which while who will with would also about after based including "
    "per primarily approximately".split()
)


# ---------- splitting ----------
def split_sentences(answer: str) -> list[str]:
    """Split an answer into sentences, keeping each sentence's citations attached."""
    protected = answer
    for abbr in ABBREVIATIONS:
        protected = protected.replace(abbr, abbr.replace(".", "․"))  # look-alike dot
    sentences = []
    for line in protected.split("\n"):
        line = line.strip()
        if not line:
            continue
        # split after . ! ? (optionally followed by citations) when the next sentence starts
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z*\"(])|(?<=\])\s+(?=[A-Z*\"(])", line)
        sentences.extend(p.strip() for p in parts if p.strip())
    merged: list[str] = []
    for s in sentences:  # a bare "[ID]" fragment belongs to the sentence before it
        if merged and CITATION_GROUP_RE.fullmatch(" " + s):
            merged[-1] += " " + s
        else:
            merged.append(s)
    return [s.replace("․", ".") for s in merged]


def is_claim(sentence: str) -> bool:
    """Lead-ins ("Key risks include:") and headings make no factual claim."""
    bare = CITATION_GROUP_RE.sub("", sentence).strip().strip("*#-• ").strip()
    if not bare or bare.endswith(":") or sentence.lstrip().startswith("#"):
        return False
    if CHUNK_ID_RE.search(sentence):  # anything the model cited is a claim, however short
        return True
    return len(re.findall(r"[A-Za-z]{2,}", bare)) >= 3


# ---------- support check ----------
def _stem(word: str) -> str:
    return word[:6]  # crude stemming: "manufacturing" ~ "manufactured"


def content_words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in STOPWORDS}


def numbers(text: str) -> set[str]:
    out = set()
    for n in NUMBER_RE.findall(text):
        n = n.replace(",", "").rstrip(".")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        out.add(n)
    return out


def check_sentence(sentence: str, by_id: dict[str, dict], cfg: dict) -> dict:
    ids = list(dict.fromkeys(CHUNK_ID_RE.findall(sentence)))
    valid = [i for i in ids if i in by_id]
    claim_text = CITATION_GROUP_RE.sub("", sentence)
    result = {"sentence": sentence, "cited": valid, "invalid_ids": [i for i in ids if i not in by_id]}
    if not valid:
        return {**result, "supported": False, "reason": "no valid citation"}

    source = "\n".join(embedding_text(by_id[i]) for i in valid)
    missing_numbers = numbers(claim_text) - numbers(source)
    words = content_words(claim_text)
    overlap = len(words & content_words(source)) / len(words) if words else 1.0
    result["word_overlap"] = round(overlap, 2)

    if cfg["require_numbers_in_source"] and missing_numbers:
        return {**result, "supported": False, "reason": f"numbers not in source: {sorted(missing_numbers)}"}
    if overlap < cfg["min_word_overlap"]:
        return {**result, "supported": False, "reason": f"low word overlap {overlap:.2f}"}
    return {**result, "supported": True, "reason": "ok"}


# ---------- enforcement ----------
def enforce_citations(answer: str, chunks: list[dict], cfg: dict) -> dict:
    """Return the cleaned answer plus a report of what was checked/removed."""
    if answer.strip() == REFUSAL:
        return {"answer": REFUSAL, "refused": True, "refusal_reason": "model_refused", "checks": [], "citation_coverage": None}

    by_id = {c["chunk_id"]: c for c in chunks}
    claims = [s for s in split_sentences(answer) if is_claim(s)]
    checks = [check_sentence(s, by_id, cfg) for s in claims]
    supported = [c for c in checks if c["supported"]]
    coverage = len(supported) / len(checks) if checks else 0.0

    if not supported or coverage < cfg["min_supported_fraction"]:
        return {
            "answer": REFUSAL,
            "refused": True,
            "refusal_reason": "unsupported_answer",
            "checks": checks,
            "citation_coverage": round(coverage, 3),
        }

    cleaned = answer
    for c in checks:
        if not c["supported"]:
            # keep a leading bullet marker so the rest of that bullet stays a bullet
            body = re.sub(r"^[-*•]\s+", "", c["sentence"])
            cleaned = cleaned.replace(body, "")
    cleaned = re.sub(r"(?m)^(\s*[-*•])\s+", r"\1 ", cleaned)
    cleaned = re.sub(r"(?m)^\s*[-*•]\s*$\n?", "", cleaned)  # bullets left empty
    cleaned = re.sub(r"[ \t]{2,}", " ", re.sub(r"\n{3,}", "\n\n", cleaned)).strip()
    return {
        "answer": cleaned,
        "refused": False,
        "refusal_reason": None,
        "checks": checks,
        "citation_coverage": round(coverage, 3),
    }
