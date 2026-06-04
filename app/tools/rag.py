"""
RAG tools — query ChromaDB for runbooks and past investigations.

Two tools:
  lookup_runbook       — direct metadata fetch by alert type, always finds runbook
  search_past_diagnoses — similarity search over approved past investigations only

Returns empty string if ChromaDB unreachable (agent degrades gracefully).
"""

import logging
from typing import Optional, Tuple
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_collection = None
_embed_fn = None

# Maps AlertManager alert names → runbook slug (alert_name metadata in ChromaDB)
ALERT_TO_RUNBOOK: dict[str, str] = {
    "KubePodCrashLooping": "crashloop",
    "CrashLoopBackOff": "crashloop",
    "KubePodOOMKilled": "oomkilled",
    "OOMKilled": "oomkilled",
    "KubeImagePullError": "imagepull",
    "ImagePullBackOff": "imagepull",
    "ErrImagePull": "imagepull",
    "KubeDeploymentReplicasMismatch": "replicasmismatch",
    "KubeDeploymentRolloutStuck": "rolloutstuck",
    "KubePodNotReady": "podnotready",
    "PodNotReady": "podnotready",
}


def _get_collection():
    global _collection, _embed_fn
    if _collection is not None:
        return _collection, _embed_fn

    try:
        import chromadb
        from chromadb.utils.embedding_functions import OllamaEmbeddingFunction
        from app.config import settings

        # Do NOT pass embedding_function to get_or_create_collection — chromadb
        # 0.6 serialises it and sends to the server which fails (_type KeyError).
        # Embeddings computed client-side; query uses query_embeddings= instead.
        _embed_fn = OllamaEmbeddingFunction(
            url=f"{settings.chroma_ollama_embed_url}/api/embeddings",
            model_name=settings.chroma_embed_model,
        )
        client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
        _collection = client.get_or_create_collection(name=settings.chroma_collection)
        return _collection, _embed_fn
    except Exception as e:
        logger.warning("ChromaDB unavailable: %s", e)
        return None, None


@tool
def lookup_runbook(alert_name: str) -> str:
    """Fetch the investigation runbook for this alert type.

    Call this as your VERY FIRST tool — returns the authoritative guide
    with the exact tool sequence, exit code meanings, and fix commands
    for this alert type. Uses a direct metadata lookup (no similarity
    threshold) so it always works even with sparse embeddings.

    Args:
        alert_name: exact alertname from alert labels, e.g.
                    "KubePodCrashLooping", "OOMKilled", "ImagePullBackOff"

    Returns:
        Full runbook guide, or empty string if no runbook for this alert type.
    """
    col, _ = _get_collection()
    if col is None:
        return ""

    slug = ALERT_TO_RUNBOOK.get(alert_name)
    if not slug:
        return ""

    try:
        results = col.get(
            where={"$and": [{"type": {"$eq": "runbook"}}, {"alert_name": {"$eq": slug}}]},
            include=["documents"],
        )
        docs = results.get("documents", [])
        if docs:
            logger.info("[tool] lookup_runbook → found runbook for alert=%s (slug=%s)", alert_name, slug)
            return f"## Runbook: {slug}\n\n{docs[0]}"
        logger.info("[tool] lookup_runbook → no runbook for alert=%s (slug=%s)", alert_name, slug)
        return ""
    except Exception as e:
        logger.warning("Runbook lookup failed for %s: %s", alert_name, e)
        return ""


@tool
def search_past_diagnoses(query: str) -> str:
    """Search past approved investigations for similar alerts.

    Call this SECOND (after lookup_runbook) — checks if this exact
    alert+namespace combination was already solved. A high-similarity
    match (≥80%) means the same fix likely applies again.

    Args:
        query: alert name + namespace + any known symptoms, e.g.
               "KubePodCrashLooping chaos namespace OOMKilled"

    Returns:
        Top matching past diagnoses with similarity score,
        or empty string if no relevant history found.
    """
    col, embed_fn = _get_collection()
    if col is None:
        return ""

    try:
        count = col.count()
        if count == 0:
            return ""

        query_embedding = embed_fn([query])
        # Filter to investigations only — runbooks are handled by lookup_runbook
        results = col.query(
            query_embeddings=query_embedding,
            n_results=min(3, count),
            where={"type": {"$eq": "investigation"}},
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


def check_high_similarity_match(alert_name: str, namespace: str) -> Optional[Tuple[str, float]]:
    """
    Direct ChromaDB check: return (diagnosis, similarity) for past investigations
    with dist < 0.15 (≥85% similar) matching this exact alert+namespace.
    Returns None if no such match exists or ChromaDB is unreachable.
    Called by run_agent before LangGraph to short-circuit known issues.
    """
    col, embed_fn = _get_collection()
    if col is None or embed_fn is None:
        return None

    try:
        count = col.count()
        if count == 0:
            return None

        query = f"{alert_name} {namespace}"
        query_embedding = embed_fn([query])

        results = col.query(
            query_embeddings=query_embedding,
            n_results=1,
            where={"$and": [
                {"type": {"$eq": "investigation"}},
                {"alert_name": {"$eq": alert_name}},
                {"namespace": {"$eq": namespace}},
            ]},
            include=["documents", "distances"],
        )

        docs = results.get("documents", [[]])[0]
        dists = results.get("distances", [[]])[0]

        if docs and dists and dists[0] < 0.15:
            similarity = round((1 - dists[0]) * 100, 1)
            return docs[0], similarity

    except Exception as e:
        logger.warning("RAG short-circuit check failed: %s", e)

    return None
