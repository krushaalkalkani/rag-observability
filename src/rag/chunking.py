"""Split a parsed filing into overlapping, token-sized chunks.

Key ideas:
- Size is measured in TOKENS of the embedding model's tokenizer, because that is
  what the embedding model actually reads (and it has a hard 512-token limit).
- Consecutive chunks overlap by `overlap` tokens, so a sentence cut at the end of
  one chunk appears whole at the start of the next.
- Chunks never cross a section boundary, so every chunk has exactly one section.
- chunk_id = TICKER-YEAR-NNNNN. Same input + same config => same ids, which
  makes re-running ingestion idempotent.
"""

from itertools import groupby


def _sections(blocks: list[dict]):
    """Yield (section_name, text, page_spans) for each run of same-section blocks.

    page_spans is a list of (start_char, end_char, page) so we can later find
    which pages any slice of the text came from.
    """
    for section, group in groupby(blocks, key=lambda b: b["section"]):
        parts, spans, pos = [], [], 0
        for block in group:
            parts.append(block["text"])
            spans.append((pos, pos + len(block["text"]), block["page"]))
            pos += len(block["text"]) + 1  # +1 for the "\n" joiner
        yield section, "\n".join(parts), spans


def chunk_document(doc: dict, tokenizer, chunk_size: int = 500, overlap: int = 100) -> list[dict]:
    if not 0 <= overlap < chunk_size:
        raise ValueError("overlap must be >= 0 and smaller than chunk_size")
    step = chunk_size - overlap
    chunks = []

    for section, text, spans in _sections(doc["blocks"]):
        # offset_mapping gives, for every token, its (start_char, end_char) in `text`.
        offsets = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True, verbose=False)[
            "offset_mapping"
        ]
        n_tokens = len(offsets)

        for start in range(0, n_tokens, step):
            end = min(start + chunk_size, n_tokens)
            if start > 0 and end - start <= overlap:
                break  # this tail is already fully inside the previous chunk
            char_start, char_end = offsets[start][0], offsets[end - 1][1]
            pages = [p for s, e, p in spans if s < char_end and e > char_start]

            chunks.append(
                {
                    "chunk_id": f"{doc['ticker']}-{doc['fiscal_year']}-{len(chunks):05d}",
                    "ticker": doc["ticker"],
                    "company": doc["company"],
                    "fiscal_year": doc["fiscal_year"],
                    "section": section,
                    "page_start": min(pages),
                    "page_end": max(pages),
                    "n_tokens": end - start,
                    "source_url": doc["source_url"],
                    "text": text[char_start:char_end],
                }
            )
            if end == n_tokens:
                break
    return chunks
