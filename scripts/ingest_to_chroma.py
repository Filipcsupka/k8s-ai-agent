"""
Ingest reviewed+correct investigation JSON files into ChromaDB.

Reads from INVESTIGATIONS_DIR (default /data/investigations/).
Only ingests records where reviewed=true AND correct=true.
Uses nomic-embed-text via Ollama for embeddings.
Collection: k8s-runbooks.

Usage:
  python -m scripts.ingest_to_chroma
  INVESTIGATIONS_DIR=/tmp/investigations python -m scripts.ingest_to_chroma

Doc IDs are filenames — re-running is safe (upsert).
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
        "alert_name": rec.get("alert_name", "unknown"),
        "namespace": rec.get("namespace", "unknown"),
        "pod": rec.get("pod", ""),
        "severity": rec.get("severity", "unknown"),
        "timestamp": rec.get("timestamp", ""),
        "tool_calls": ",".join(rec.get("tool_calls", [])),
        "duration_sec": rec.get("duration_sec", 0.0),
    }


def main() -> None:
    approved = load_approved(INVESTIGATIONS_DIR)
    if not approved:
        logger.info("No approved investigations to ingest. Mark records reviewed=true + correct=true first.")
        return

    logger.info("Found %d approved investigations", len(approved))

    embed_fn = OllamaEmbeddingFunction(
        url=f"{OLLAMA_URL}/api/embeddings",
        model_name=EMBED_MODEL,
    )

    client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    collection = client.get_or_create_collection(
        name=COLLECTION,
        embedding_function=embed_fn,
        metadata={"hnsw:space": "cosine"},
    )

    ids = [fname for fname, _ in approved]
    documents = [build_document(rec) for _, rec in approved]
    metadatas = [build_metadata(rec) for _, rec in approved]

    # Upsert in batches of 50 to avoid request size limits
    batch = 50
    ingested = 0
    for i in range(0, len(ids), batch):
        collection.upsert(
            ids=ids[i:i+batch],
            documents=documents[i:i+batch],
            metadatas=metadatas[i:i+batch],
        )
        ingested += len(ids[i:i+batch])
        logger.info("Upserted %d / %d", ingested, len(ids))

    logger.info("Done. Collection '%s' now has %d total documents.", COLLECTION, collection.count())


if __name__ == "__main__":
    main()
