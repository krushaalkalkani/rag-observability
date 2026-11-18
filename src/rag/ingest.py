"""Offline ingestion pipeline: download -> parse -> chunk -> embed -> store.

    python -m rag.ingest                 # all companies in config.yaml
    python -m rag.ingest --tickers TSLA  # just one

Safe to run again and again (idempotent):
- download skips files that already exist on disk,
- parse/chunk are deterministic (same input -> same chunk ids),
- indexing deletes a filing's old chunks before writing the new ones.
"""

import argparse
import json
import os
from collections import Counter
from pathlib import Path

from rag.chunking import chunk_document
from rag.config import load_config
from rag.parse import parse_filing
from rag.sec import SecClient


def download(cfg: dict, tickers: list[str]) -> list[dict]:
    """Download the latest 10-K(s) per ticker. Returns the filing metadata list."""
    sec_cfg = cfg["sec"]
    user_agent = os.environ.get("SEC_USER_AGENT", sec_cfg["user_agent"])
    client = SecClient(user_agent, sec_cfg["max_requests_per_second"])
    filings = []
    for ticker in tickers:
        for filing in client.latest_10k_filings(ticker, sec_cfg["filings_per_company"]):
            folder = cfg["paths"]["raw_dir"] / ticker
            folder.mkdir(parents=True, exist_ok=True)
            html_path = folder / f"{filing['fiscal_year']}_{filing['accession']}.html"
            if html_path.exists():
                print(f"  {ticker} FY{filing['fiscal_year']}: already downloaded")
            else:
                print(f"  {ticker} FY{filing['fiscal_year']}: downloading {filing['source_url']}")
                html_path.write_bytes(client.get(filing["source_url"]).content)
            html_path.with_suffix(".json").write_text(json.dumps(filing, indent=2))
            filing["html_path"] = str(html_path)
            filings.append(filing)
    return filings


def process_filing(cfg: dict, filing: dict, tokenizer) -> list[dict]:
    """Parse + chunk one filing, saving both steps to data/processed for inspection."""
    html = open(filing["html_path"], encoding="utf-8", errors="ignore").read()
    doc = {k: v for k, v in filing.items() if k != "html_path"}
    doc["blocks"] = parse_filing(html)

    out = cfg["paths"]["processed_dir"]
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{filing['ticker']}_{filing['fiscal_year']}"
    (out / f"{stem}.parsed.json").write_text(json.dumps(doc, indent=1))

    ch = cfg["chunking"]
    chunks = chunk_document(doc, tokenizer, ch["chunk_size"], ch["chunk_overlap"])
    with open(out / f"{stem}.chunks.jsonl", "w") as f:
        for c in chunks:
            f.write(json.dumps(c) + "\n")
    return chunks


def ingest_from_processed(cfg: dict, folder: Path) -> None:
    """Index already-parsed filings (no network). CI uses this with the small
    fixture corpus in data/fixtures/, so every PR builds a real index from scratch.
    Chunking is deterministic, so chunk ids match the golden set."""
    from rag.index import get_collection, get_embedder, index_document_chunks

    tokenizer = get_embedder().tokenizer
    ch = cfg["chunking"]
    for path in sorted(folder.glob("*.parsed.json")):
        doc = json.loads(path.read_text())
        chunks = chunk_document(doc, tokenizer, ch["chunk_size"], ch["chunk_overlap"])
        index_document_chunks(doc["ticker"], doc["fiscal_year"], chunks)
        print(f"  {doc['ticker']} FY{doc['fiscal_year']}: {len(chunks)} chunks")
    print(f"Done. Chroma at {cfg['paths']['chroma_dir']} holds {get_collection().count()} chunks.")


def main():
    cfg = load_config()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tickers", nargs="+", default=cfg["sec"]["companies"])
    parser.add_argument("--from-processed", metavar="DIR",
                        help="skip download+parse: chunk and index the *.parsed.json files in DIR (used by CI)")
    args = parser.parse_args()
    if args.from_processed:
        return ingest_from_processed(cfg, Path(args.from_processed))
    tickers = [t.upper() for t in args.tickers]

    print("1/3 Downloading filings from SEC EDGAR")
    filings = download(cfg, tickers)

    # Heavy imports (torch etc.) only after the network step succeeded.
    from rag.index import get_collection, get_embedder, index_document_chunks

    tokenizer = get_embedder().tokenizer
    print("2/3 Parsing + chunking, 3/3 embedding + storing in Chroma")
    for filing in filings:
        chunks = process_filing(cfg, filing, tokenizer)
        sections = Counter(c["section"] for c in chunks)
        print(f"  {filing['ticker']} FY{filing['fiscal_year']}: {len(chunks)} chunks, {len(sections)} sections")
        index_document_chunks(filing["ticker"], filing["fiscal_year"], chunks)

    print(f"Done. Chroma now holds {get_collection().count()} chunks.")


if __name__ == "__main__":
    main()
