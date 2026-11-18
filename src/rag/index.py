"""Embeddings + Chroma vector store.

Embedding = turning text into a vector (384 numbers for bge-small) so that texts
with similar meaning end up close together. Chroma stores the vectors on disk
along with the chunk text and metadata, and finds nearest neighbours fast.
"""

import os
from functools import lru_cache

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import chromadb  # noqa: E402
from chromadb.config import Settings  # noqa: E402

from rag.config import load_config  # noqa: E402

METADATA_KEYS = ["ticker", "company", "fiscal_year", "section", "page_start", "page_end", "n_tokens", "source_url"]


@lru_cache
def get_embedder():
    from sentence_transformers import SentenceTransformer  # slow import, so load lazily

    return SentenceTransformer(load_config()["embedding"]["model"])


@lru_cache
def get_collection():
    cfg = load_config()
    client = chromadb.PersistentClient(
        path=str(cfg["paths"]["chroma_dir"]), settings=Settings(anonymized_telemetry=False)
    )
    # cosine distance: compares direction of vectors, the standard choice for text embeddings
    return client.get_or_create_collection(cfg["vector_store"]["collection"], metadata={"hnsw:space": "cosine"})


def embedding_text(chunk: dict) -> str:
    """What we actually embed: a short header + the chunk.

    Many chunks never mention the company name ("We face intense competition..."),
    so without the header a query about "Tesla" can't tell Tesla's chunk from Ford's.
    """
    return f"{chunk['company']} ({chunk['ticker']}) 10-K fiscal year {chunk['fiscal_year']} - {chunk['section']}\n{chunk['text']}"


def embed_texts(texts: list[str], is_query: bool = False):
    cfg = load_config()["embedding"]
    if is_query:
        texts = [cfg["query_instruction"] + t for t in texts]
    return get_embedder().encode(
        texts, batch_size=cfg["batch_size"], normalize_embeddings=True, show_progress_bar=not is_query
    ).tolist()


def index_document_chunks(ticker: str, fiscal_year: int, chunks: list[dict]) -> None:
    """Replace everything stored for one filing with `chunks` (idempotent)."""
    collection = get_collection()
    # Delete first: if a re-run produces fewer chunks (e.g. after changing chunk_size),
    # old leftover chunks would otherwise stay in the index forever.
    collection.delete(where={"$and": [{"ticker": ticker}, {"fiscal_year": fiscal_year}]})

    embeddings = embed_texts([embedding_text(c) for c in chunks])
    batch = 1000
    for i in range(0, len(chunks), batch):
        part = chunks[i : i + batch]
        collection.upsert(
            ids=[c["chunk_id"] for c in part],
            embeddings=embeddings[i : i + batch],
            documents=[c["text"] for c in part],
            metadatas=[{k: c[k] for k in METADATA_KEYS} for c in part],
        )


def get_all_chunks() -> list[dict]:
    res = get_collection().get(include=["documents", "metadatas"])
    return [{"chunk_id": i, "text": d, **m} for i, d, m in zip(res["ids"], res["documents"], res["metadatas"])]


def get_chunk(chunk_id: str) -> dict | None:
    res = get_collection().get(ids=[chunk_id], include=["documents", "metadatas"])
    if not res["ids"]:
        return None
    return {"chunk_id": chunk_id, "text": res["documents"][0], **res["metadatas"][0]}
