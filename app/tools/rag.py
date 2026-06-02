"""
RAG tool — query ChromaDB for similar past investigations.

Returns top-3 approved diagnoses ranked by cosine similarity.
Returns empty string if ChromaDB unreachable (agent degrades gracefully).
"""

import logging
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_collection = None


def _get_collection():
    global _collection
    if _collection is not None:
        return _collection

    try:
        import chromadb
        from chromadb.utils.embedding_functions import OllamaEmbeddingFunction
        from app.config import settings

        embed_fn = OllamaEmbeddingFunction(
            url=f"{settings.chroma_ollama_embed_url}/api/embeddings",
            model_name=settings.chroma_embed_model,
        )
        client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
        _collection = client.get_or_create_collection(
            name=settings.chroma_collection,
            embedding_function=embed_fn,
        )
        return _collection
    except Exception as e:
        logger.warning("ChromaDB unavailable: %s", e)
        return None


@tool
def search_past_diagnoses(query: str) -> str:
    """Search past approved investigations for similar alerts.

    Use this FIRST when you receive an alert — similar past diagnoses
    can immediately reveal the root cause and the fix that worked.

    Args:
        query: alert name + namespace + any known symptoms, e.g.
               "KubePodCrashLooping chaos namespace OOMKilled"

    Returns:
        Top matching past diagnoses with their recommended fixes,
        or empty string if no relevant history found.
    """
    col = _get_collection()
    if col is None:
        return ""

    try:
        count = col.count()
        if count == 0:
            return ""

        results = col.query(
            query_texts=[query],
            n_results=min(3, count),
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        logger.warning("RAG query failed: %s", e)
        return ""

    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]

    if not docs:
        return ""

    # cosine distance → similarity (1 - distance); skip poor matches (dist > 0.5)
    parts = ["## Past Similar Investigations"]
    shown = 0
    for doc, meta, dist in zip(docs, metas, dists):
        if dist > 0.5:
            continue
        similarity = round((1 - dist) * 100, 1)
        parts.append(
            f"\n### Match {shown + 1} ({similarity}% similar)"
            f"\nAlert: {meta.get('alert_name')} | Namespace: {meta.get('namespace')}"
            f"\nTools used: {meta.get('tool_calls')}\n"
            f"{doc}"
        )
        shown += 1

    if shown == 0:
        return ""

    return "\n".join(parts)
