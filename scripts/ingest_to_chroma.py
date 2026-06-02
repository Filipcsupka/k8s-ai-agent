"""
Ingest knowledge into ChromaDB — two sources:

1. Runbooks (runbooks/*.md) — authoritative fix guides, always ingested.
2. Reviewed investigations (INVESTIGATIONS_DIR/*.json) — past diagnoses marked
   reviewed=true AND correct=true by a human operator.

Uses nomic-embed-text via Ollama for embeddings.
Collection: k8s-runbooks.

Usage:
  python -m scripts.ingest_to_chroma
  INVESTIGATIONS_DIR=/tmp/investigations python -m scripts.ingest_to_chroma

Doc IDs are stable — re-running is safe (upsert).
"""

import json
import logging
import os
import sys

import chromadb
from chromadb.utils.embedding_functions import OllamaEmbeddingFunction

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

INVESTIGATIONS_DIR = os.environ.get("INVESTIGATIONS_DIR", "/data/investigations")
RUNBOOKS_DIR = os.environ.get("RUNBOOKS_DIR", "/app/runbooks")
CHROMA_HOST = os.environ.get("CHROMA_HOST", "chromadb.ai-chat.svc.cluster.local")
CHROMA_PORT = int(os.environ.get("CHROMA_PORT", "8000"))
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama.ai-chat.svc.cluster.local:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
COLLECTION = os.environ.get("CHROMA_COLLECTION", "k8s-runbooks")


def load_approved(inv_dir: str) -> list[tuple[str, dict]]:
    """Return (filename, record) for all reviewed=True + correct=True files."""
    approved = []
    try:
        files = sorted(os.listdir(inv_dir))
    except FileNotFoundError:
        logger.error("Investigations dir not found: %s", inv_dir)
        sys.exit(1)

    for fname in files:
        if not fname.endswith(".json"):
            continue
        path = os.path.join(inv_dir, fname)
        try:
            with open(path) as f:
                rec = json.load(f)
        except Exception as e:
            logger.warning("Skip %s: %s", fname, e)
            continue

        if rec.get("reviewed") is True and rec.get("correct") is True:
            approved.append((fname, rec))

    return approved


def build_document(rec: dict) -> str:
    """Combine alert context + diagnosis into a single text for embedding."""
    parts = [
        f"Alert: {rec.get('alert_name', 'unknown')}",
        f"Namespace: {rec.get('namespace', 'unknown')}",
        f"Pod: {rec.get('pod', '')}",
        f"Tools used: {', '.join(rec.get('tool_calls', []))}",
        "",
        rec.get("diagnosis", ""),
    ]
    if rec.get("notes"):
        parts += ["", f"Notes: {rec['notes']}"]
    return "\n".join(parts)


def build_metadata(rec: dict) -> dict:
    return {
        "type": "investigation",
        "alert_name": rec.get("alert_name", "unknown"),
        "namespace": rec.get("namespace", "unknown"),
        "pod": rec.get("pod", ""),
        "severity": rec.get("severity", "unknown"),
        "timestamp": rec.get("timestamp", ""),
        "tool_calls": ",".join(rec.get("tool_calls", [])),
        "duration_sec": rec.get("duration_sec", 0.0),
    }


def load_runbooks(runbooks_dir: str) -> list[tuple[str, str, dict]]:
    """Return (doc_id, content, metadata) for all markdown runbooks."""
    runbooks = []
    try:
        files = sorted(os.listdir(runbooks_dir))
    except FileNotFoundError:
        logger.warning("Runbooks dir not found: %s — skipping runbook ingestion", runbooks_dir)
        return []

    for fname in files:
        if not fname.endswith(".md"):
            continue
        path = os.path.join(runbooks_dir, fname)
        try:
            with open(path) as f:
                content = f.read().strip()
        except Exception as e:
            logger.warning("Skip runbook %s: %s", fname, e)
            continue
        doc_id = f"runbook-{fname[:-3]}"
        metadata = {"type": "runbook", "source": fname, "alert_name": fname[:-3]}
        runbooks.append((doc_id, content, metadata))

    return runbooks


def _upsert_batch(collection, ids: list, documents: list, metadatas: list, label: str) -> None:
    batch = 50
    ingested = 0
    for i in range(0, len(ids), batch):
        collection.upsert(
            ids=ids[i:i+batch],
            documents=documents[i:i+batch],
            metadatas=metadatas[i:i+batch],
        )
        ingested += len(ids[i:i+batch])
        logger.info("%s: upserted %d / %d", label, ingested, len(ids))


def main() -> None:
    embed_fn = OllamaEmbeddingFunction(
        url=f"{OLLAMA_URL}/api/embeddings",
        model_name=EMBED_MODEL,
    )
    chroma = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    collection = chroma.get_or_create_collection(
        name=COLLECTION,
        embedding_function=embed_fn,
        metadata={"hnsw:space": "cosine"},
    )

    # --- Runbooks (always ingest — authoritative, no review gate) ---
    runbooks = load_runbooks(RUNBOOKS_DIR)
    if runbooks:
        logger.info("Ingesting %d runbooks from %s", len(runbooks), RUNBOOKS_DIR)
        _upsert_batch(
            collection,
            ids=[r[0] for r in runbooks],
            documents=[r[1] for r in runbooks],
            metadatas=[r[2] for r in runbooks],
            label="runbooks",
        )
    else:
        logger.info("No runbooks found in %s", RUNBOOKS_DIR)

    # --- Approved investigations (reviewed=true + correct=true) ---
    approved = load_approved(INVESTIGATIONS_DIR)
    if approved:
        logger.info("Ingesting %d approved investigations from %s", len(approved), INVESTIGATIONS_DIR)
        _upsert_batch(
            collection,
            ids=[fname for fname, _ in approved],
            documents=[build_document(rec) for _, rec in approved],
            metadatas=[build_metadata(rec) for _, rec in approved],
            label="investigations",
        )
    else:
        logger.info("No approved investigations to ingest (mark records reviewed=true + correct=true)")

    total = collection.count()
    logger.info("Done. Collection '%s' has %d total documents.", COLLECTION, total)
    if total == 0:
        logger.warning("Collection is still empty — agent RAG will return no results")


if __name__ == "__main__":
    main()
