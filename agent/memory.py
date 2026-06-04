"""Chroma vector store helpers: runbook RAG + persistent incident memory.

Two collections live in one local persistent Chroma instance:

* ``runbooks``        — chunks of the Markdown runbook corpus (FR-3).
* ``incident_memory`` — one document per resolved incident (FR-6); this is the
  project's differentiator vs K8sGPT/HolmesGPT (persistent, cross-incident).

Embeddings are computed locally with sentence-transformers (all-MiniLM-L6-v2)
so there is zero cloud-embedding cost — Groq has no embeddings API.
"""

from __future__ import annotations

import glob
import json
import os
from functools import lru_cache
from typing import Any

from agent.config import get_settings

# --- text chunking -----------------------------------------------------------

_CHUNK_CHARS = 1000
_CHUNK_OVERLAP = 150


def _chunk_markdown(text: str) -> list[str]:
    """Naive but adequate fixed-size character chunking with overlap.

    Good enough for <=50 small runbook files (NFR). Can be swapped for a
    header-aware splitter later without changing callers.
    """
    text = text.strip()
    if len(text) <= _CHUNK_CHARS:
        return [text] if text else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + _CHUNK_CHARS
        chunks.append(text[start:end])
        start = end - _CHUNK_OVERLAP
    return chunks


# --- chroma client / collections --------------------------------------------


@lru_cache
def _client():
    import chromadb

    settings = get_settings()
    os.makedirs(settings.chroma_persist_dir, exist_ok=True)
    return chromadb.PersistentClient(path=settings.chroma_persist_dir)


@lru_cache
def _embedding_fn():
    # Imported lazily; pulls model weights on first use (cached on disk after).
    from chromadb.utils import embedding_functions

    settings = get_settings()
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=settings.embedding_model
    )


def _collection(name: str):
    return _client().get_or_create_collection(name=name, embedding_function=_embedding_fn())


# --- runbook ingestion + search (FR-3) ---------------------------------------


def load_runbooks(reset: bool = False) -> int:
    """Load (chunk + embed) all runbook .md files into the runbook collection.

    Returns the number of chunks indexed. Idempotent on document ids.
    """
    settings = get_settings()
    if reset:
        try:
            _client().delete_collection(settings.runbook_collection)
        except Exception:  # noqa: BLE001 - collection may not exist yet
            pass
    coll = _collection(settings.runbook_collection)

    ids, docs, metas = [], [], []
    for path in sorted(glob.glob(os.path.join(settings.runbook_dir, "*.md"))):
        fname = os.path.basename(path)
        with open(path, encoding="utf-8") as fh:
            for i, chunk in enumerate(_chunk_markdown(fh.read())):
                ids.append(f"{fname}::{i}")
                docs.append(chunk)
                metas.append({"source": fname, "chunk": i})

    if docs:
        coll.upsert(ids=ids, documents=docs, metadatas=metas)
    return len(docs)


def search_runbooks(query: str, top_k: int = 3) -> list[str]:
    settings = get_settings()
    coll = _collection(settings.runbook_collection)
    if coll.count() == 0:
        return []
    res = coll.query(query_texts=[query], n_results=min(top_k, coll.count()))
    return (res.get("documents") or [[]])[0]


# --- incident memory (FR-6) --------------------------------------------------


def persist_incident(incident: dict[str, Any]) -> str:
    """Write a resolved incident to long-term memory.

    ``incident`` should include: alert_name, root_cause, actions_taken,
    outcome, timestamp (SRS FR-6). Returns the stored document id.
    """
    settings = get_settings()
    coll = _collection(settings.incident_collection)

    incident_id = incident.get("incident_id") or incident.get("timestamp", "incident")
    # Document text is what future incidents semantically search against.
    doc = (
        f"Alert: {incident.get('alert_name')}\n"
        f"Root cause: {incident.get('root_cause')}\n"
        f"Actions taken: {incident.get('actions_taken')}\n"
        f"Outcome: {incident.get('outcome')}"
    )
    coll.upsert(
        ids=[incident_id],
        documents=[doc],
        metadatas=[{k: json.dumps(v) if isinstance(v, (list, dict)) else v
                    for k, v in incident.items()}],
    )
    return incident_id


def search_incidents(query: str, top_k: int = 3) -> list[str]:
    settings = get_settings()
    coll = _collection(settings.incident_collection)
    if coll.count() == 0:
        return []
    res = coll.query(query_texts=[query], n_results=min(top_k, coll.count()))
    return (res.get("documents") or [[]])[0]
