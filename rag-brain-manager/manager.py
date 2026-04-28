#!/usr/bin/env python3
"""
rag-brain-manager — ChromaDB knowledge base manager with GCS sync.

Heavy-lifter pattern: all ChromaDB and GCS logic lives here; the OpenCLAW
agent only triggers CLI commands.

Architecture:
    1. Sync Down  — pull ChromaDB files from GCS to local storage
    2. Execute    — run the requested vector operation locally
    3. Embed      — pluggable embedding model (default: gemini-embedding-001)
    4. Sync Up    — push modified state back to GCS (mutating actions only)

Multi-collection design:
    Every action accepts --collection to target a specific collection.
    Falls back to CHROMA_COLLECTION_NAME env var, then 'open_brain'.
    Use --all-collections with --action query to search across every collection.

SPEC-RAG-001 additions:
    - LlamaIndex integration for improved indexing/retrieval (NFR-RAG-030 fallback)
    - Pluggable embedding models via RAG_EMBEDDING_MODEL env var / --embedding-model
    - Hybrid search (semantic + BM25 + RRF fusion)
    - Cross-encoder reranking
    - Embedding benchmark harness
    - Collection re-embedding migration
    - Metadata sidecar ingestion

Environment variables (injected via Doppler):
    GEMINI_API_KEY            Google AI API key for gemini-embedding-001 (preferred)
    GOOGLE_API_KEY            Accepted as fallback for GEMINI_API_KEY
    GCP_SERVICE_ACCOUNT_KEY   JSON string of GCP service account
    CHROMA_GCS_BUCKET         GCS bucket name
    CHROMA_COLLECTION_NAME    Default collection name
    CHROMA_LOCAL_DIR          Override local ChromaDB path
    RAG_EMBEDDING_MODEL       Active embedding model (default: gemini-embedding-001)
    RAG_RERANK_ENABLED        Enable cross-encoder reranking (default: false)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

import chromadb
from google import genai
from google.cloud import storage

log = logging.getLogger("rag-brain-manager")
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stderr,
)

LOCAL_CHROMA_DIR = os.environ.get(
    "CHROMA_LOCAL_DIR", "/home/node/.openclaw/chroma_state"
)
CHROMA_SERVER_URL = os.environ.get("CHROMA_SERVER_URL", "")
GCS_BUCKET_NAME = os.environ.get("CHROMA_GCS_BUCKET", "ai-letter-analyst-state")
DEFAULT_COLLECTION = os.environ.get("CHROMA_COLLECTION_NAME", "open_brain")
EMBEDDING_MODEL = "models/gemini-embedding-001"

# Legacy chunking constants (used by direct fallback path)
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200

# ChromaDB collection name rules: 3-63 chars, alphanumeric/hyphen/underscore,
# must start and end with alphanumeric.
_COLLECTION_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,61}[a-zA-Z0-9]$")


# ── SPEC-RAG-001: LlamaIndex conditional import (NFR-RAG-030) ─────────

_LLAMAINDEX_AVAILABLE = False
try:
    from llama_index.core import Settings, StorageContext, VectorStoreIndex
    from llama_index.core.node_parser import MarkdownNodeParser
    from llama_index.vector_stores.chroma import ChromaVectorStore
    _LLAMAINDEX_AVAILABLE = True
except ImportError:
    log.warning("LlamaIndex not available — using direct ChromaDB fallback")


# ── SPEC-RAG-001: Pluggable Embedding Model Registry (REQ-RAG-002) ────


def _ensure_hf_hub_token() -> None:
    """Issues #221/#222: sync DON_HUGGINGFACE → HF_TOKEN for huggingface_hub downloads."""
    token = (
        os.environ.get("DON_HUGGINGFACE", "").strip()
        or os.environ.get("HF_TOKEN", "").strip()
        or os.environ.get("HUGGINGFACE_HUB_TOKEN", "").strip()
    )
    if token:
        os.environ.setdefault("HF_TOKEN", token)
        os.environ.setdefault("HUGGINGFACE_HUB_TOKEN", token)


# Issue #200: switch default away from Gemini to a local, API-key-free model so
# RAG queries survive a Gemini spending-cap outage.  The env var allows operators
# to override without a code change (set RAG_EMBEDDING_MODEL in Doppler).
DEFAULT_EMBEDDING_MODEL_ID = os.environ.get("RAG_EMBEDDING_MODEL", "nomic-embed-text-v1.5")

# Local HuggingFace fallback used when the primary model fails (never fall back to Gemini).
LOCAL_EMBED_FALLBACK = "nomic-embed-text-v1.5"

EMBEDDING_REGISTRY: dict[str, dict[str, Any]] = {
    "gemini-embedding-001": {
        "class": "GeminiEmbedding",
        "model_name": "models/gemini-embedding-001",
        "dimensions": 768,
        "requires_api_key": True,
        "api_key_env": "GEMINI_API_KEY",
        "local": False,
    },
    # Primary default (Issue #200): local model, no API key, 768-dim (matches Gemini dim).
    "nomic-embed-text-v1.5": {
        "class": "HuggingFaceEmbedding",
        "model_name": "nomic-ai/nomic-embed-text-v1.5",
        "dimensions": 768,
        "requires_api_key": False,
        "local": True,
        "trust_remote_code": True,
    },
    "bge-large-en-v1.5": {
        "class": "HuggingFaceEmbedding",
        "model_name": "BAAI/bge-large-en-v1.5",
        "dimensions": 1024,
        "requires_api_key": False,
        "local": True,
    },
    "snowflake-arctic-embed-l": {
        "class": "HuggingFaceEmbedding",
        "model_name": "Snowflake/snowflake-arctic-embed-l",
        "dimensions": 1024,
        "requires_api_key": False,
        "local": True,
    },
    # OpenAI-compatible embedding endpoint — works with real OpenAI (text-embedding-3-small)
    # or any provider that exposes POST /v1/embeddings (e.g. a local Ollama server).
    # Set OPENAI_EMBEDDING_API_KEY and optionally OPENAI_EMBEDDING_BASE_URL in Doppler.
    "text-embedding-3-small": {
        "class": "OpenAIEmbedding",
        "model_name": "text-embedding-3-small",
        "dimensions": 1536,
        "requires_api_key": True,
        "api_key_env": "OPENAI_API_KEY",
        "local": False,
    },
}

# Default reranker model (REQ-RAG-501)
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


def get_embed_model(model_id: str) -> Any:
    """Factory: instantiate a LlamaIndex embedding model by registry ID (REQ-RAG-207).

    Returns the LlamaIndex embedding object, or None if LlamaIndex is unavailable.
    Fallback chain: primary → LOCAL_EMBED_FALLBACK (never Gemini — Issue #200).
    """
    if model_id not in EMBEDDING_REGISTRY:
        print(json.dumps({
            "error": "UNKNOWN_EMBEDDING_MODEL",
            "model": model_id,
            "supported": list(EMBEDDING_REGISTRY.keys()),
        }))
        sys.exit(1)

    if not _LLAMAINDEX_AVAILABLE:
        return None

    spec = EMBEDDING_REGISTRY[model_id]

    try:
        if spec["class"] == "GeminiEmbedding":
            from llama_index.embeddings.gemini import GeminiEmbedding
            api_key = _get_google_api_key()
            return GeminiEmbedding(model_name=spec["model_name"], api_key=api_key)
        elif spec["class"] == "HuggingFaceEmbedding":
            _ensure_hf_hub_token()
            from llama_index.embeddings.huggingface import HuggingFaceEmbedding
            kwargs: dict[str, Any] = {"model_name": spec["model_name"]}
            if spec.get("trust_remote_code"):
                kwargs["trust_remote_code"] = True
            return HuggingFaceEmbedding(**kwargs)
        elif spec["class"] == "OpenAIEmbedding":
            from llama_index.embeddings.openai import OpenAIEmbedding
            api_key = os.environ.get(spec.get("api_key_env", "OPENAI_API_KEY"), "")
            base_url = os.environ.get("OPENAI_EMBEDDING_BASE_URL", None)
            kwargs_oa: dict[str, Any] = {"model": spec["model_name"], "api_key": api_key}
            if base_url:
                kwargs_oa["api_base"] = base_url
            return OpenAIEmbedding(**kwargs_oa)
        else:
            log.warning("Unknown embedding class %s", spec["class"])
            return None
    except Exception as exc:
        # Issue #200: never fall back to Gemini — it may be quota-exhausted.
        # Fall back to the local HuggingFace model instead.
        if model_id != LOCAL_EMBED_FALLBACK:
            log.warning(
                "Failed to load embedding model %s: %s — falling back to %s",
                model_id, exc, LOCAL_EMBED_FALLBACK,
            )
            return get_embed_model(LOCAL_EMBED_FALLBACK)
        log.error("Failed to load fallback embedding model %s: %s", model_id, exc)
        return None


def _resolve_embedding_model_id(args_model: Optional[str]) -> str:
    """Resolve the active embedding model ID from CLI flag or env var."""
    return args_model or DEFAULT_EMBEDDING_MODEL_ID


# ── Auth & Client Init ───────────────────────────────────────────────


def _get_google_api_key() -> str:
    key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )
    if not key:
        sys.exit(
            "ERROR: Neither GEMINI_API_KEY nor GOOGLE_API_KEY is set.\n"
            "GEMINI_API_KEY is already stored in Doppler (openclaw-docker project).\n"
            "Restart the container to pick it up."
        )
    return key


def _get_gcs_client() -> storage.Client:
    raw = os.environ.get("GCP_SERVICE_ACCOUNT_KEY", "").strip()
    if not raw:
        sys.exit(
            "ERROR: GCP_SERVICE_ACCOUNT_KEY is not set.\n"
            "Store the GCP service account JSON in Doppler and restart."
        )
    try:
        sa_info = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit("ERROR: GCP_SERVICE_ACCOUNT_KEY is not valid JSON.")
    return storage.Client.from_service_account_info(sa_info)


def _get_genai_client() -> genai.Client:
    return genai.Client(api_key=_get_google_api_key())


# ── Collection Helpers ───────────────────────────────────────────────


def validate_collection_name(name: str) -> str:
    """Validate and return the collection name, or exit with a clear error."""
    if not name:
        sys.exit("ERROR: Collection name cannot be empty.")
    if len(name) < 3:
        sys.exit(f"ERROR: Collection name '{name}' is too short (min 3 chars).")
    if len(name) > 63:
        sys.exit(f"ERROR: Collection name '{name}' is too long (max 63 chars).")
    if not _COLLECTION_NAME_RE.match(name):
        sys.exit(
            f"ERROR: Collection name '{name}' is invalid.\n"
            "Must start and end with alphanumeric, contain only a-z A-Z 0-9 _ -"
        )
    return name


def resolve_collection_name(cli_value: str | None) -> str:
    """Precedence: --collection CLI arg > CHROMA_COLLECTION_NAME env > default."""
    name = cli_value or DEFAULT_COLLECTION
    return validate_collection_name(name)


def _get_collection(
    chroma_client: chromadb.ClientAPI, name: str, create_if_missing: bool = True
) -> Any:
    """Fetch a collection, optionally creating it if absent."""
    if create_if_missing:
        return chroma_client.get_or_create_collection(name=name)
    try:
        return chroma_client.get_collection(name=name)
    except Exception:
        existing = [c.name for c in chroma_client.list_collections()]
        sys.exit(
            f"ERROR: Collection '{name}' does not exist.\n"
            f"Available: {', '.join(existing) or '(none)'}\n"
            "Use --action create-collection --collection <name> to create it."
        )


def _get_collection_embedding_model(collection: Any) -> str:
    """Read embedding_model from collection metadata, defaulting to gemini (INV-RAG-004)."""
    meta = getattr(collection, "metadata", None) or {}
    return meta.get("embedding_model", "gemini-embedding-001")


def _set_collection_metadata(collection: Any, model_id: str) -> None:
    """Update collection metadata with embedding model info (REQ-RAG-203)."""
    from datetime import datetime, timezone
    spec = EMBEDDING_REGISTRY.get(model_id, {})
    try:
        collection.modify(metadata={
            "embedding_model": model_id,
            "embedding_dim": spec.get("dimensions", 768),
            "indexing_framework": "llamaindex" if _LLAMAINDEX_AVAILABLE else "direct",
            "last_ingested_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        log.warning("Could not update collection metadata: %s", exc)


# ── GCS Sync ─────────────────────────────────────────────────────────


def sync_from_gcs(gcs_client: storage.Client) -> int:
    """Pull ChromaDB files from GCS to local storage. Returns file count."""
    log.info("Syncing ChromaDB state FROM gs://%s", GCS_BUCKET_NAME)
    bucket = gcs_client.bucket(GCS_BUCKET_NAME)
    blobs = list(bucket.list_blobs())

    os.makedirs(LOCAL_CHROMA_DIR, exist_ok=True)

    def _download_blob(blob) -> int:
        if blob.name.endswith("/"):
            return 0
        local_path = os.path.join(LOCAL_CHROMA_DIR, blob.name)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        blob.download_to_filename(local_path)
        return 1

    count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = executor.map(_download_blob, blobs)
        count = sum(results)

    log.info("Synced %d file(s) from GCS.", count)
    return count


def sync_to_gcs(gcs_client: Optional[storage.Client]) -> int:
    if gcs_client is None:
        return 0
    """Push local ChromaDB files back to GCS. Returns file count."""
    log.info("Syncing ChromaDB state TO gs://%s", GCS_BUCKET_NAME)
    bucket = gcs_client.bucket(GCS_BUCKET_NAME)

    local_files = []
    for root, _, files in os.walk(LOCAL_CHROMA_DIR):
        for fname in files:
            local_files.append(os.path.join(root, fname))

    def _upload_file(local_path) -> int:
        relative_path = os.path.relpath(local_path, LOCAL_CHROMA_DIR)
        blob = bucket.blob(relative_path)
        blob.upload_from_filename(local_path)
        return 1

    count = 0
    if local_files:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            results = executor.map(_upload_file, local_files)
            count = sum(results)

    log.info("Synced %d file(s) to GCS.", count)
    return count


# ── Embeddings (direct/legacy path) ──────────────────────────────────


def _check_gemini_quota_error(exc: Exception) -> None:
    """Re-raise as SystemExit with a clear message if the error is a Gemini quota breach.

    Issue #200: the google.genai SDK retries 429s with exponential backoff for several
    minutes before raising.  Catching and re-raising immediately as SystemExit saves the
    entire retry window (up to 5 min) and gives a clear, actionable error message.
    """
    msg = str(exc)
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "spending cap" in msg.lower():
        sys.exit(
            "ERROR: Gemini API quota exceeded (spending cap). All Gemini embedding calls "
            "are blocked until the cap is raised.\n"
            "  Fix 1 (immediate): raise/remove the spending cap in Google AI Studio billing.\n"
            "  Fix 2 (durable):   set RAG_EMBEDDING_MODEL=nomic-embed-text-v1.5 in Doppler, "
            "then run --action re-embed --embedding-model nomic-embed-text-v1.5 on all "
            "collections to migrate stored embeddings.\n"
            f"  Original error: {msg}"
        )


def get_embedding(client: genai.Client, text: str) -> list[float]:
    """Generate an embedding vector using gemini-embedding-001 (legacy direct path)."""
    try:
        result = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
        )
        return list(result.embeddings[0].values)
    except Exception as exc:
        _check_gemini_quota_error(exc)
        raise


def get_embeddings_batch(
    client: genai.Client, texts: list[str]
) -> list[list[float]]:
    """Batch-embed multiple texts using gemini-embedding-001 (legacy direct path)."""
    try:
        result = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=texts,
        )
        return [list(e.values) for e in result.embeddings]
    except Exception as exc:
        _check_gemini_quota_error(exc)
        raise


# ── Unified embedding dispatch (Issue #200) ──────────────────────────
# All query/ingest/re-embed callsites should go through these helpers so that
# the active embedding model is respected and Gemini is never hard-required.

def _embed_single(
    text: str,
    model_id: str,
    genai_client: Optional["genai.Client"] = None,
) -> list[float]:
    """Embed one text string with the specified model."""
    if model_id == "gemini-embedding-001":
        if genai_client is None:
            sys.exit(
                "ERROR: genai_client is required to embed with gemini-embedding-001 but was not "
                "provided.  Set RAG_EMBEDDING_MODEL=nomic-embed-text-v1.5 in Doppler to use a "
                "local model instead."
            )
        return get_embedding(genai_client, text)
    em = get_embed_model(model_id)
    if em is None:
        sys.exit(
            f"ERROR: Cannot embed with '{model_id}' — LlamaIndex is unavailable.  "
            f"Install llama-index-embeddings-huggingface and retry."
        )
    return em.get_text_embedding(text)


def _embed_texts(
    texts: list[str],
    model_id: str,
    genai_client: Optional["genai.Client"] = None,
) -> list[list[float]]:
    """Batch-embed a list of texts with the specified model."""
    if model_id == "gemini-embedding-001":
        if genai_client is None:
            sys.exit(
                "ERROR: genai_client is required to embed with gemini-embedding-001 but was not "
                "provided.  Set RAG_EMBEDDING_MODEL=nomic-embed-text-v1.5 in Doppler to use a "
                "local model instead."
            )
        return get_embeddings_batch(genai_client, texts)
    em = get_embed_model(model_id)
    if em is None:
        sys.exit(
            f"ERROR: Cannot embed with '{model_id}' — LlamaIndex is unavailable.  "
            f"Install llama-index-embeddings-huggingface and retry."
        )
    return em.get_text_embedding_batch(texts, show_progress=False)


# ── Markdown-Aware Chunking (legacy path) ────────────────────────────

_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)


def chunk_markdown(
    text: str,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[str]:
    """Split markdown into chunks, preferring heading boundaries."""
    sections = _split_on_headings(text)

    chunks: list[str] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue

        if len(section) <= chunk_size:
            chunks.append(section)
        else:
            chunks.extend(_split_long_section(section, chunk_size, overlap))

    return [c for c in chunks if c.strip()]


def _split_on_headings(text: str) -> list[str]:
    """Split text at markdown heading boundaries."""
    positions = [m.start() for m in _HEADING_RE.finditer(text)]
    if not positions:
        return [text]

    sections: list[str] = []
    if positions[0] > 0:
        sections.append(text[: positions[0]])

    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        sections.append(text[pos:end])

    return sections


def _split_long_section(
    text: str, chunk_size: int, overlap: int
) -> list[str]:
    """Split a long section on paragraph boundaries, then by character."""
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        candidate = f"{current}\n\n{para}".strip() if current else para.strip()
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(para) > chunk_size:
                chunks.extend(_split_by_chars(para, chunk_size, overlap))
                current = ""
            else:
                current = para.strip()

    if current:
        chunks.append(current)

    return chunks


def _split_by_chars(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Last-resort character-based splitting with overlap."""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


# ── Metadata Sidecar Support (REQ-RAG-007) ───────────────────────────


def _load_metadata_sidecar(file_path: str) -> dict[str, Any]:
    """Load .metadata.json sidecar if present (REQ-RAG-700). Returns empty dict if absent."""
    p = Path(file_path)
    sidecar_path = p.parent / f"{p.stem}.metadata.json"
    if not sidecar_path.exists():
        return {}
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        # Extract scalar fields suitable for ChromaDB metadata (REQ-RAG-701)
        result: dict[str, Any] = {}
        for key in ("document_type", "date_issued", "sender", "subject", "urgency"):
            if key in data and isinstance(data[key], str):
                result[key] = data[key]
        # Entities: list → comma-separated string (ChromaDB requires scalar values)
        if "entities" in data and isinstance(data["entities"], list):
            result["entities"] = ",".join(str(e) for e in data["entities"])
        log.info("Loaded metadata sidecar from %s", sidecar_path)
        return result
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to read metadata sidecar %s: %s", sidecar_path, exc)
        return {}


# ── Hybrid Search Helpers (REQ-RAG-004) ──────────────────────────────


def _build_bm25_index(collection: Any) -> Any:
    """Build an in-memory BM25 index from a collection's documents (REQ-RAG-401)."""
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        log.warning("rank_bm25 not available — falling back to semantic-only")
        return None

    all_data = collection.get(include=["documents", "metadatas"])
    docs = all_data.get("documents", [])
    ids = all_data.get("ids", [])
    metas = all_data.get("metadatas", []) or [{}] * len(ids)

    if not docs:
        return None

    # Tokenize for BM25
    tokenized = [doc.lower().split() for doc in docs]

    try:
        bm25 = BM25Okapi(tokenized)
    except Exception as exc:
        log.warning("BM25 index build failed: %s", exc)
        return None

    return {
        "bm25": bm25,
        "ids": ids,
        "docs": docs,
        "metas": metas,
    }


def _bm25_search(
    bm25_data: dict[str, Any],
    query: str,
    n_results: int,
    collection_name: str,
) -> list[dict[str, Any]]:
    """Run a BM25 keyword search and return ranked results."""
    bm25 = bm25_data["bm25"]
    tokenized_query = query.lower().split()
    scores = bm25.get_scores(tokenized_query)

    # Get top-N indices by BM25 score
    ranked_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:n_results]

    matches = []
    for idx in ranked_indices:
        if scores[idx] <= 0:
            continue
        matches.append({
            "id": bm25_data["ids"][idx],
            "collection": collection_name,
            "bm25_score": float(scores[idx]),
            "metadata": bm25_data["metas"][idx] if idx < len(bm25_data["metas"]) else {},
            "document": bm25_data["docs"][idx],
        })
    return matches


def _reciprocal_rank_fusion(
    semantic_results: list[dict[str, Any]],
    keyword_results: list[dict[str, Any]],
    semantic_weight: float,
    n_results: int,
    k: int = 60,
) -> list[dict[str, Any]]:
    """Fuse semantic and keyword results using RRF (REQ-RAG-402)."""
    scores: dict[str, float] = {}
    result_map: dict[str, dict[str, Any]] = {}

    # Score semantic results
    for rank, r in enumerate(semantic_results):
        doc_id = r["id"]
        scores[doc_id] = scores.get(doc_id, 0) + semantic_weight * (1.0 / (k + rank + 1))
        result_map[doc_id] = r

    # Score keyword results
    keyword_weight = 1.0 - semantic_weight
    for rank, r in enumerate(keyword_results):
        doc_id = r["id"]
        scores[doc_id] = scores.get(doc_id, 0) + keyword_weight * (1.0 / (k + rank + 1))
        if doc_id not in result_map:
            result_map[doc_id] = r

    # Sort by fused score
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:n_results]

    fused = []
    for doc_id, score in ranked:
        entry = dict(result_map[doc_id])
        entry["fusion_score"] = round(score, 6)
        fused.append(entry)

    return fused


# ── Cross-Encoder Reranking (REQ-RAG-005) ────────────────────────────


def _rerank_results(
    query: str,
    matches: list[dict[str, Any]],
    n_results: int,
) -> list[dict[str, Any]]:
    """Rerank results using a cross-encoder model (REQ-RAG-500)."""
    _ensure_hf_hub_token()
    try:
        from llama_index.postprocessor.sbert_rerank import SentenceTransformerRerank
    except ImportError:
        try:
            from sentence_transformers import CrossEncoder
            model = CrossEncoder(RERANK_MODEL)
            pairs = [(query, m.get("document", "")) for m in matches]
            scores = model.predict(pairs)
            for i, m in enumerate(matches):
                m["rerank_score"] = float(scores[i])
            matches.sort(key=lambda m: m.get("rerank_score", 0), reverse=True)
            return matches[:n_results]
        except ImportError:
            log.warning("sentence-transformers not available — skipping rerank")
            return matches[:n_results]

    # Use LlamaIndex reranker if available
    try:
        reranker = SentenceTransformerRerank(model=RERANK_MODEL, top_n=n_results)
        # Build query bundles for the reranker — use raw sentence-transformers instead
        from sentence_transformers import CrossEncoder
        model = CrossEncoder(RERANK_MODEL)
        pairs = [(query, m.get("document", "")) for m in matches]
        scores = model.predict(pairs)
        for i, m in enumerate(matches):
            m["rerank_score"] = float(scores[i])
        matches.sort(key=lambda m: m.get("rerank_score", 0), reverse=True)
        return matches[:n_results]
    except Exception as exc:
        log.warning("Reranking failed: %s — returning unranked results", exc)
        return matches[:n_results]


# ── Expert Judgment ──────────────────────────────────────────────────


def expert_judgment(item: dict[str, Any]) -> str | None:
    """Flag potential issues with ingestion or collection health."""
    if "file_size" in item:
        size = item["file_size"]
        if size < 50:
            return "File too small (<50 chars) — likely empty or metadata-only"
        if size > 5 * 1024 * 1024:
            return f"Very large file ({size // (1024*1024)} MB) — consider splitting before ingest"

    if "doc_count" in item and item["doc_count"] == 0:
        return "Collection is empty — no documents ingested yet"

    if "total" in item and "duplicates" in item:
        total = item["total"]
        dupes = item["duplicates"]
        if total > 0 and (dupes / total) > 0.2:
            return f"High duplicate ratio ({dupes}/{total} = {dupes/total:.0%}) — run optimize"

    return None


# ── Actions ──────────────────────────────────────────────────────────


def handle_create_collection(
    chroma_client: chromadb.ClientAPI,
    collection_name: str,
    gcs_client: storage.Client,
) -> None:
    """Create a named collection (no-op if it already exists)."""
    existing_names = {c.name for c in chroma_client.list_collections()}
    if collection_name in existing_names:
        print(json.dumps({
            "action": "create-collection",
            "status": "already_exists",
            "collection": collection_name,
        }))
        return

    chroma_client.create_collection(name=collection_name)
    log.info("Created collection '%s'. Syncing to GCS…", collection_name)
    sync_to_gcs(gcs_client)

    print(json.dumps({
        "action": "create-collection",
        "status": "created",
        "collection": collection_name,
    }))


def handle_delete_collection(
    chroma_client: chromadb.ClientAPI,
    collection_name: str,
    gcs_client: storage.Client,
) -> None:
    """Delete a named collection and sync the change to GCS."""
    existing = [c.name for c in chroma_client.list_collections()]
    if collection_name not in existing:
        sys.exit(
            f"ERROR: Collection '{collection_name}' does not exist.\n"
            f"Available: {', '.join(existing) or '(none)'}"
        )

    col = chroma_client.get_collection(name=collection_name)
    doc_count = col.count()

    chroma_client.delete_collection(name=collection_name)
    log.info("Deleted collection '%s' (%d docs). Syncing to GCS…", collection_name, doc_count)
    sync_to_gcs(gcs_client)

    remaining = [c.name for c in chroma_client.list_collections()]
    print(json.dumps({
        "action": "delete-collection",
        "status": "deleted",
        "collection": collection_name,
        "documents_removed": doc_count,
        "remaining_collections": remaining,
    }))


def handle_list_collections(chroma_client: chromadb.ClientAPI) -> None:
    """List all collections with document and source counts."""
    collections = chroma_client.list_collections()
    entries: list[dict[str, Any]] = []

    for col in collections:
        count = col.count()
        entry: dict[str, Any] = {
            "name": col.name,
            "document_count": count,
        }

        # Include embedding model from collection metadata (REQ-RAG-203)
        col_meta = getattr(col, "metadata", None) or {}
        if "embedding_model" in col_meta:
            entry["embedding_model"] = col_meta["embedding_model"]
            entry["embedding_dim"] = col_meta.get("embedding_dim")

        if count > 0:
            all_meta = col.get(include=["metadatas"])
            sources = sorted({
                m.get("source", "unknown")
                for m in (all_meta.get("metadatas") or [])
                if m
            })
            entry["sources"] = sources
            entry["source_count"] = len(sources)

        flag = expert_judgment({"doc_count": count})
        if flag:
            entry["warning"] = flag

        entries.append(entry)

    print(json.dumps({
        "action": "list-collections",
        "total_collections": len(entries),
        "collections": entries,
    }, indent=2))


def handle_query(
    collection: Any,
    genai_client: genai.Client,
    query_text: str,
    n_results: int,
    collection_name: str,
    search_mode: str = "semantic",
    semantic_weight: float = 0.5,
    rerank: bool = False,
    where_filter: Optional[str] = None,
    embedding_model_id: Optional[str] = None,
) -> None:
    """Semantic/hybrid/keyword search within a single collection (REQ-RAG-004)."""
    t0 = time.time()

    # Resolve embedding model for this collection (REQ-RAG-205)
    col_model = _get_collection_embedding_model(collection)
    query_model_id = col_model  # Always use collection's model for query

    # Parse where filter (REQ-RAG-704)
    where_dict = None
    if where_filter:
        try:
            where_dict = json.loads(where_filter)
        except json.JSONDecodeError:
            print(json.dumps({"error": "INVALID_WHERE_FILTER", "detail": "Must be valid JSON"}))
            sys.exit(1)

    # Determine how many candidates to fetch (more if reranking)
    fetch_n = n_results * 3 if rerank else n_results

    # Semantic retrieval
    semantic_matches: list[dict[str, Any]] = []
    if search_mode in ("semantic", "hybrid"):
        # Issue #200: use the collection's stored embedding model, not hardcoded Gemini.
        query_embedding = _embed_single(query_text, query_model_id, genai_client)
        query_kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": min(fetch_n, collection.count()) if collection.count() > 0 else fetch_n,
            "include": ["documents", "metadatas", "distances"],
        }
        if where_dict:
            query_kwargs["where"] = where_dict

        results = collection.query(**query_kwargs)
        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]

        for i, doc_id in enumerate(ids):
            semantic_matches.append({
                "id": doc_id,
                "collection": collection_name,
                "distance": dists[i] if i < len(dists) else None,
                "metadata": metas[i] if i < len(metas) else {},
                "document": docs[i] if i < len(docs) else "",
            })

    # BM25 keyword retrieval (REQ-RAG-401)
    keyword_matches: list[dict[str, Any]] = []
    bm25_build_ms = 0
    if search_mode in ("keyword", "hybrid"):
        bm25_t0 = time.time()
        bm25_data = _build_bm25_index(collection)
        bm25_build_ms = int((time.time() - bm25_t0) * 1000)
        if bm25_data:
            keyword_matches = _bm25_search(bm25_data, query_text, fetch_n, collection_name)
        elif search_mode == "keyword":
            log.warning("BM25 unavailable — no results for keyword-only mode")

    # Fuse results
    if search_mode == "hybrid" and semantic_matches and keyword_matches:
        matches = _reciprocal_rank_fusion(
            semantic_matches, keyword_matches, semantic_weight, fetch_n,
        )
    elif search_mode == "keyword":
        matches = keyword_matches
    else:
        matches = semantic_matches

    # Rerank (REQ-RAG-500)
    reranked = False
    rerank_model = None
    if rerank and matches:
        rerank_t0 = time.time()
        matches = _rerank_results(query_text, matches, n_results)
        reranked = True
        rerank_model = RERANK_MODEL
        rerank_ms = int((time.time() - rerank_t0) * 1000)
    else:
        matches = matches[:n_results]
        rerank_ms = 0

    query_latency_ms = int((time.time() - t0) * 1000)

    output: dict[str, Any] = {
        "action": "query",
        "query": query_text,
        "collection": collection_name,
        "n_results": n_results,
        "search_mode": search_mode,
        "matches": matches,
        "query_latency_ms": query_latency_ms,
    }
    if reranked:
        output["reranked"] = True
        output["rerank_model"] = rerank_model
        output["rerank_latency_ms"] = rerank_ms
    else:
        output["reranked"] = False
    if bm25_build_ms:
        output["bm25_build_time_ms"] = bm25_build_ms
    if where_dict:
        output["where_filter"] = where_dict

    print(json.dumps(output, indent=2))


def handle_query_all(
    chroma_client: chromadb.ClientAPI,
    genai_client: genai.Client,
    query_text: str,
    n_results: int,
    search_mode: str = "semantic",
    semantic_weight: float = 0.5,
    rerank: bool = False,
    where_filter: Optional[str] = None,
) -> None:
    """Cross-collection search — merges results ranked by distance/score (REQ-RAG-404)."""
    collections = chroma_client.list_collections()
    if not collections:
        print(json.dumps({
            "action": "query",
            "query": query_text,
            "collections_searched": [],
            "matches": [],
            "note": "No collections found.",
        }, indent=2))
        return

    t0 = time.time()
    all_matches: list[dict[str, Any]] = []
    searched: list[str] = []

    where_dict = None
    if where_filter:
        try:
            where_dict = json.loads(where_filter)
        except json.JSONDecodeError:
            pass

    fetch_n = n_results * 3 if rerank else n_results

    # Cache per-model query embeddings so we embed once per model across collections.
    _query_embed_cache: dict[str, list[float]] = {}

    for col_meta in collections:
        col = chroma_client.get_collection(name=col_meta.name)
        count = col.count()
        if count == 0:
            continue

        searched.append(col_meta.name)
        per_col = min(fetch_n, count)

        # Issue #200: embed with each collection's own model, not hardcoded Gemini.
        col_model = _get_collection_embedding_model(col)
        if col_model not in _query_embed_cache:
            try:
                _query_embed_cache[col_model] = _embed_single(query_text, col_model, genai_client)
            except SystemExit:
                raise
            except Exception as exc:
                log.warning("Skipping collection '%s': embedding failed: %s", col_meta.name, exc)
                continue
        query_embedding = _query_embed_cache[col_model]

        # Semantic results
        query_kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": per_col,
            "include": ["documents", "metadatas", "distances"],
        }
        if where_dict:
            query_kwargs["where"] = where_dict

        results = col.query(**query_kwargs)
        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]

        semantic_matches = []
        for i, doc_id in enumerate(ids):
            semantic_matches.append({
                "id": doc_id,
                "collection": col_meta.name,
                "distance": dists[i] if i < len(dists) else None,
                "metadata": metas[i] if i < len(metas) else {},
                "document": docs[i] if i < len(docs) else "",
            })

        # Hybrid: add BM25 results per collection (REQ-RAG-404)
        if search_mode in ("keyword", "hybrid"):
            bm25_data = _build_bm25_index(col)
            if bm25_data:
                kw_matches = _bm25_search(bm25_data, query_text, per_col, col_meta.name)
                if search_mode == "hybrid" and semantic_matches:
                    fused = _reciprocal_rank_fusion(
                        semantic_matches, kw_matches, semantic_weight, per_col,
                    )
                    all_matches.extend(fused)
                elif search_mode == "keyword":
                    all_matches.extend(kw_matches)
                else:
                    all_matches.extend(semantic_matches)
            else:
                all_matches.extend(semantic_matches)
        else:
            all_matches.extend(semantic_matches)

    # Sort globally and take top-N
    if search_mode == "hybrid":
        all_matches.sort(key=lambda m: m.get("fusion_score", 0), reverse=True)
    elif search_mode == "keyword":
        all_matches.sort(key=lambda m: m.get("bm25_score", 0), reverse=True)
    else:
        all_matches.sort(key=lambda m: m.get("distance") or float("inf"))

    # Rerank
    reranked = False
    if rerank and all_matches:
        all_matches = _rerank_results(query_text, all_matches, n_results)
        reranked = True
    else:
        all_matches = all_matches[:n_results]

    query_latency_ms = int((time.time() - t0) * 1000)

    output: dict[str, Any] = {
        "action": "query",
        "query": query_text,
        "collections_searched": searched,
        "n_results": n_results,
        "search_mode": search_mode,
        "matches": all_matches,
        "query_latency_ms": query_latency_ms,
        "reranked": reranked,
    }
    if reranked:
        output["rerank_model"] = RERANK_MODEL

    print(json.dumps(output, indent=2))


def handle_ingest(
    collection: Any,
    genai_client: genai.Client,
    gcs_client: storage.Client,
    file_path: str,
    source_name: str,
    collection_name: str,
    embedding_model_id: str = "gemini-embedding-001",
) -> None:
    """Chunk a markdown file, embed, upsert into ChromaDB, sync to GCS.

    Uses LlamaIndex MarkdownNodeParser when available (REQ-RAG-103), falling
    back to the hand-rolled chunk_markdown() for backward compatibility (NFR-RAG-030).
    """
    if not os.path.exists(file_path):
        sys.exit(f"ERROR: File not found: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()

    flag = expert_judgment({"file_size": len(text)})
    if flag:
        log.warning("Expert Judgment: %s", flag)

    # Check embedding model consistency (REQ-RAG-204)
    col_model = _get_collection_embedding_model(collection)
    if col_model != "gemini-embedding-001" or (
        hasattr(collection, "metadata") and collection.metadata and
        "embedding_model" in collection.metadata
    ):
        if col_model != embedding_model_id:
            print(json.dumps({
                "error": "EMBEDDING_MODEL_MISMATCH",
                "collection_model": col_model,
                "requested_model": embedding_model_id,
                "hint": "Use --action re-embed or create a new collection",
            }))
            sys.exit(1)

    # Load metadata sidecar if present (REQ-RAG-700)
    sidecar_meta = _load_metadata_sidecar(file_path)

    t0 = time.time()
    chunks = chunk_markdown(text)
    if not chunks:
        sys.exit("ERROR: No content chunks extracted from file.")

    log.info(
        "Embedding %d chunk(s) from '%s' into collection '%s' using %s…",
        len(chunks), source_name, collection_name, embedding_model_id,
    )
    # Issue #200: route through _embed_texts so the requested model (not Gemini) is used.
    embeddings = _embed_texts(chunks, embedding_model_id, genai_client)

    ids: list[str] = []
    metadatas: list[dict[str, Any]] = []

    for i, chunk in enumerate(chunks):
        chunk_id = hashlib.md5(f"{collection_name}:{source_name}:{i}".encode()).hexdigest()
        ids.append(chunk_id)
        meta: dict[str, Any] = {
            "source": source_name,
            "collection": collection_name,
            "chunk_index": i,
            "char_count": len(chunk),
            "embedding_model": embedding_model_id,  # REQ-RAG-109
        }
        # Merge sidecar metadata (REQ-RAG-701)
        meta.update(sidecar_meta)
        metadatas.append(meta)

    collection.upsert(
        ids=ids,
        embeddings=embeddings,
        metadatas=metadatas,
        documents=chunks,
    )

    # Update collection metadata (REQ-RAG-203)
    _set_collection_metadata(collection, embedding_model_id)

    embedding_latency_ms = int((time.time() - t0) * 1000)
    log.info("Upsert complete. Syncing to GCS…")
    sync_to_gcs(gcs_client)

    output: dict[str, Any] = {
        "action": "ingest",
        "status": "success",
        "collection": collection_name,
        "source": source_name,
        "chunks_ingested": len(chunks),
        "file": file_path,
        "embedding_model": embedding_model_id,
        "embedding_latency_ms": embedding_latency_ms,
    }
    if sidecar_meta:
        output["sidecar_metadata"] = True
    print(json.dumps(output))


def handle_delete_source(
    collection: Any,
    gcs_client: storage.Client,
    source_name: str,
    collection_name: str,
) -> None:
    """Remove all chunks belonging to a specific source document."""
    all_data = collection.get(include=["metadatas"])
    all_ids = all_data.get("ids", [])
    all_metas = all_data.get("metadatas", []) or []

    to_delete = [
        doc_id
        for doc_id, meta in zip(all_ids, all_metas)
        if (meta or {}).get("source") == source_name
    ]

    if not to_delete:
        print(json.dumps({
            "action": "delete-source",
            "status": "not_found",
            "collection": collection_name,
            "source": source_name,
            "chunks_removed": 0,
            "note": f"No chunks found with source='{source_name}' in '{collection_name}'.",
        }))
        return

    collection.delete(ids=to_delete)
    log.info(
        "Deleted %d chunk(s) for source '%s' from '%s'. Syncing to GCS…",
        len(to_delete), source_name, collection_name,
    )
    sync_to_gcs(gcs_client)

    print(json.dumps({
        "action": "delete-source",
        "status": "deleted",
        "collection": collection_name,
        "source": source_name,
        "chunks_removed": len(to_delete),
    }))


def handle_report(chroma_client: chromadb.ClientAPI) -> None:
    """Health check — list all collections with document counts."""
    collections = chroma_client.list_collections()
    report: list[dict[str, Any]] = []

    for col in collections:
        count = col.count()
        entry: dict[str, Any] = {"name": col.name, "document_count": count}

        # Include embedding model info (REQ-RAG-203)
        col_meta = getattr(col, "metadata", None) or {}
        if "embedding_model" in col_meta:
            entry["embedding_model"] = col_meta["embedding_model"]
            entry["embedding_dim"] = col_meta.get("embedding_dim")

        flag = expert_judgment({"doc_count": count})
        if flag:
            entry["warning"] = flag

        report.append(entry)

    print(json.dumps({
        "action": "report",
        "health": "OK",
        "total_collections": len(report),
        "collections": report,
    }, indent=2))


def handle_backup(
    chroma_client: chromadb.ClientAPI,
    genai_client: genai.Client | None,
) -> None:
    """Export all collections to GCS as a timestamped backup."""
    from datetime import datetime, timezone

    gcs_client = _get_gcs_client()
    bucket = gcs_client.bucket(GCS_BUCKET_NAME)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"backups/{timestamp}"

    collections = chroma_client.list_collections()
    if not collections:
        print(json.dumps({
            "action": "backup",
            "status": "empty",
            "note": "No collections to back up.",
        }))
        return

    total_docs = 0
    backed_up: list[dict[str, Any]] = []

    for col in collections:
        count = col.count()
        if count == 0:
            backed_up.append({"name": col.name, "documents": 0, "status": "empty"})
            continue

        all_data = col.get(
            include=["documents", "metadatas", "embeddings"],
        )

        raw_embeddings = all_data.get("embeddings", [])
        embeddings_list = []
        if raw_embeddings is not None:
            for e in raw_embeddings:
                if hasattr(e, "tolist"):
                    embeddings_list.append(e.tolist())
                else:
                    embeddings_list.append(list(e) if e is not None else [])

        payload = {
            "collection": col.name,
            "count": count,
            "ids": all_data.get("ids", []),
            "documents": all_data.get("documents", []),
            "metadatas": all_data.get("metadatas", []),
            "embeddings": embeddings_list,
        }

        blob_path = f"{prefix}/{col.name}.json"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(
            json.dumps(payload),
            content_type="application/json",
        )
        total_docs += count
        backed_up.append({
            "name": col.name,
            "documents": count,
            "gcs_path": f"gs://{GCS_BUCKET_NAME}/{blob_path}",
        })
        log.info("Backed up '%s' (%d docs) → %s", col.name, count, blob_path)

    manifest = {
        "timestamp": timestamp,
        "collections": backed_up,
        "total_documents": total_docs,
    }
    manifest_blob = bucket.blob(f"{prefix}/manifest.json")
    manifest_blob.upload_from_string(
        json.dumps(manifest, indent=2),
        content_type="application/json",
    )

    print(json.dumps({
        "action": "backup",
        "status": "success",
        "timestamp": timestamp,
        "gcs_prefix": f"gs://{GCS_BUCKET_NAME}/{prefix}/",
        "total_documents": total_docs,
        "collections": backed_up,
    }, indent=2))


def handle_restore(
    chroma_client: chromadb.ClientAPI,
    backup_timestamp: str | None,
) -> None:
    """Restore collections from a GCS backup."""
    gcs_client = _get_gcs_client()
    bucket = gcs_client.bucket(GCS_BUCKET_NAME)

    if backup_timestamp:
        prefix = f"backups/{backup_timestamp}"
    else:
        blobs = list(bucket.list_blobs(prefix="backups/"))
        timestamps = sorted({
            b.name.split("/")[1]
            for b in blobs
            if b.name.count("/") >= 2 and b.name.split("/")[1]
        })
        if not timestamps:
            sys.exit("ERROR: No backups found in GCS.")
        prefix = f"backups/{timestamps[-1]}"
        log.info("Using latest backup: %s", timestamps[-1])

    manifest_blob = bucket.blob(f"{prefix}/manifest.json")
    if not manifest_blob.exists():
        sys.exit(f"ERROR: No manifest.json found at gs://{GCS_BUCKET_NAME}/{prefix}/")

    manifest = json.loads(manifest_blob.download_as_text())
    restored: list[dict[str, Any]] = []

    for entry in manifest.get("collections", []):
        name = entry["name"]
        doc_count = entry.get("documents", 0)
        if doc_count == 0:
            restored.append({"name": name, "documents": 0, "status": "skipped_empty"})
            continue

        data_blob = bucket.blob(f"{prefix}/{name}.json")
        if not data_blob.exists():
            restored.append({"name": name, "status": "missing_data_file"})
            continue

        payload = json.loads(data_blob.download_as_text())
        col = chroma_client.get_or_create_collection(name=name)

        ids = payload.get("ids", [])
        documents = payload.get("documents", [])
        metadatas = payload.get("metadatas", [])
        embeddings = payload.get("embeddings", [])

        if ids and documents and embeddings:
            BATCH = 500
            for i in range(0, len(ids), BATCH):
                batch_end = min(i + BATCH, len(ids))
                col.upsert(
                    ids=ids[i:batch_end],
                    documents=documents[i:batch_end],
                    metadatas=metadatas[i:batch_end] if metadatas else None,
                    embeddings=embeddings[i:batch_end],
                )

            restored.append({"name": name, "documents": len(ids), "status": "restored"})
            log.info("Restored '%s' (%d docs)", name, len(ids))
        else:
            restored.append({"name": name, "status": "incomplete_data"})

    print(json.dumps({
        "action": "restore",
        "status": "success",
        "backup_prefix": f"gs://{GCS_BUCKET_NAME}/{prefix}/",
        "collections": restored,
    }, indent=2))


def handle_optimize(
    collection: Any,
    gcs_client: storage.Client,
    dry_run: bool,
    collection_name: str,
) -> None:
    """Deduplicate chunks by content hash within a collection."""
    all_data = collection.get(include=["documents"])
    all_ids = all_data.get("ids", [])
    all_docs = all_data.get("documents", [])

    doc_hashes: dict[str, str] = {}
    duplicates: list[str] = []

    for doc_id, doc in zip(all_ids, all_docs):
        doc_hash = hashlib.md5(doc.encode()).hexdigest()
        if doc_hash in doc_hashes:
            duplicates.append(doc_id)
        else:
            doc_hashes[doc_hash] = doc_id

    flag = expert_judgment({"total": len(all_ids), "duplicates": len(duplicates)})
    if flag:
        log.warning("Expert Judgment: %s", flag)

    if dry_run:
        print(json.dumps({
            "action": "optimize",
            "status": "dry_run",
            "collection": collection_name,
            "total_documents": len(all_ids),
            "duplicates_found": len(duplicates),
            "duplicate_ids": duplicates[:20],
        }, indent=2))
    else:
        if duplicates:
            collection.delete(ids=duplicates)
            log.info("Removed %d duplicate(s). Syncing to GCS…", len(duplicates))
            sync_to_gcs(gcs_client)
        print(json.dumps({
            "action": "optimize",
            "status": "optimized",
            "collection": collection_name,
            "total_documents": len(all_ids),
            "duplicates_removed": len(duplicates),
        }))


# ── SPEC-RAG-001: Benchmark Harness (REQ-RAG-003) ────────────────────


def handle_benchmark(
    chroma_client: chromadb.ClientAPI,
    genai_client: genai.Client,
    benchmark_file: str,
    embedding_models: Optional[str],
    n_results: int,
) -> None:
    """Evaluate embedding models against a curated benchmark set (REQ-RAG-300)."""
    if not os.path.exists(benchmark_file):
        print(json.dumps({"error": "BENCHMARK_FILE_NOT_FOUND", "path": benchmark_file}))
        sys.exit(1)

    # Parse benchmark JSONL (REQ-RAG-301)
    queries: list[dict[str, Any]] = []
    with open(benchmark_file, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                queries.append(entry)
            except json.JSONDecodeError as exc:
                print(json.dumps({
                    "error": "BENCHMARK_INVALID_FORMAT",
                    "line": line_num,
                    "reason": str(exc),
                }))
                sys.exit(1)

    if not queries:
        print(json.dumps({"error": "BENCHMARK_INVALID_FORMAT", "line": 0, "reason": "Empty file"}))
        sys.exit(1)

    # Determine models to benchmark
    if embedding_models:
        model_ids = [m.strip() for m in embedding_models.split(",")]
    else:
        model_ids = list(EMBEDDING_REGISTRY.keys())

    for mid in model_ids:
        if mid not in EMBEDDING_REGISTRY:
            print(json.dumps({
                "error": "UNKNOWN_EMBEDDING_MODEL",
                "model": mid,
                "supported": list(EMBEDDING_REGISTRY.keys()),
            }))
            sys.exit(1)

    results: list[dict[str, Any]] = []

    for model_id in model_ids:
        log.info("Benchmarking model: %s", model_id)
        model_result = _benchmark_model(
            chroma_client, genai_client, model_id, queries, n_results,
        )
        results.append(model_result)

    # Rank by NDCG@5 (REQ-RAG-304)
    ranking = sorted(results, key=lambda r: r.get("ndcg_at_5", 0), reverse=True)

    print(json.dumps({
        "action": "benchmark",
        "results": results,
        "ranking": [r["model"] for r in ranking],
        "benchmark_file": benchmark_file,
        "total_queries": len(queries),
    }, indent=2))


def _benchmark_model(
    chroma_client: chromadb.ClientAPI,
    genai_client: genai.Client,
    model_id: str,
    queries: list[dict[str, Any]],
    n_results: int,
) -> dict[str, Any]:
    """Run benchmark queries against a single embedding model (REQ-RAG-303)."""
    ndcg_scores: list[float] = []
    mrr_scores: list[float] = []
    recall_scores: list[float] = []
    query_latencies: list[float] = []

    for q in queries:
        query_text = q["query"]
        expected_ids = set(q.get("expected_doc_ids", []))
        collection_name = q.get("collection", DEFAULT_COLLECTION)

        try:
            col = chroma_client.get_collection(name=collection_name)
        except Exception:
            continue

        if col.count() == 0:
            continue

        t0 = time.time()
        query_embedding = get_embedding(genai_client, query_text)
        results = col.query(
            query_embeddings=[query_embedding],
            n_results=min(n_results, col.count()),
            include=["documents", "metadatas", "distances"],
        )
        latency = (time.time() - t0) * 1000
        query_latencies.append(latency)

        result_ids = results.get("ids", [[]])[0]

        # Compute metrics
        ndcg = _compute_ndcg(result_ids, expected_ids, n_results)
        mrr = _compute_mrr(result_ids, expected_ids)
        recall = _compute_recall(result_ids, expected_ids)

        ndcg_scores.append(ndcg)
        mrr_scores.append(mrr)
        recall_scores.append(recall)

    def _avg(lst: list[float]) -> float:
        return round(sum(lst) / len(lst), 4) if lst else 0.0

    return {
        "model": model_id,
        "ndcg_at_5": _avg(ndcg_scores),
        "mrr_at_5": _avg(mrr_scores),
        "recall_at_5": _avg(recall_scores),
        "avg_query_latency_ms": round(_avg(query_latencies), 1),
        "queries_evaluated": len(ndcg_scores),
    }


def _compute_ndcg(result_ids: list[str], expected: set[str], k: int) -> float:
    """Compute NDCG@k."""
    import math
    dcg = 0.0
    for i, doc_id in enumerate(result_ids[:k]):
        if doc_id in expected:
            dcg += 1.0 / math.log2(i + 2)

    ideal_hits = min(len(expected), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))

    return dcg / idcg if idcg > 0 else 0.0


def _compute_mrr(result_ids: list[str], expected: set[str]) -> float:
    """Compute Mean Reciprocal Rank."""
    for i, doc_id in enumerate(result_ids):
        if doc_id in expected:
            return 1.0 / (i + 1)
    return 0.0


def _compute_recall(result_ids: list[str], expected: set[str]) -> float:
    """Compute Recall@k."""
    if not expected:
        return 1.0
    found = sum(1 for doc_id in result_ids if doc_id in expected)
    return found / len(expected)


# ── SPEC-RAG-001: Re-Embed Migration (REQ-RAG-009) ──────────────────


def handle_re_embed(
    chroma_client: chromadb.ClientAPI,
    genai_client: genai.Client,
    gcs_client: Optional[storage.Client],
    collection_name: str,
    target_model_id: str,
    dry_run: bool,
) -> None:
    """Re-embed all documents in a collection with a new model (REQ-RAG-900)."""
    from datetime import datetime, timezone

    if target_model_id not in EMBEDDING_REGISTRY:
        print(json.dumps({
            "error": "UNKNOWN_EMBEDDING_MODEL",
            "model": target_model_id,
            "supported": list(EMBEDDING_REGISTRY.keys()),
        }))
        sys.exit(1)

    # Step 1: Read all documents from original collection
    try:
        original_col = chroma_client.get_collection(name=collection_name)
    except Exception:
        sys.exit(f"ERROR: Collection '{collection_name}' does not exist.")

    original_count = original_col.count()
    if original_count == 0:
        print(json.dumps({
            "action": "re-embed",
            "status": "empty",
            "collection": collection_name,
            "note": "Collection has no documents to re-embed.",
        }))
        return

    log.info("Reading %d documents from '%s'…", original_count, collection_name)
    all_data = original_col.get(include=["documents", "metadatas"])
    original_ids = all_data.get("ids", [])
    original_docs = all_data.get("documents", [])
    original_metas = all_data.get("metadatas", []) or [{}] * len(original_ids)

    # Step 2: Create temporary collection
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    temp_name = f"{collection_name}_reembed_{timestamp}"
    temp_name = temp_name[:63]  # ChromaDB name limit
    log.info("Creating temporary collection '%s'…", temp_name)

    temp_col = chroma_client.get_or_create_collection(name=temp_name)

    try:
        # Step 3: Re-embed all documents
        log.info("Re-embedding %d chunks with %s…", len(original_docs), target_model_id)
        BATCH = 50
        for i in range(0, len(original_docs), BATCH):
            batch_end = min(i + BATCH, len(original_docs))
            batch_docs = original_docs[i:batch_end]
            batch_ids = original_ids[i:batch_end]
            batch_metas = original_metas[i:batch_end]

            # Update metadata with new embedding model
            updated_metas = []
            for m in batch_metas:
                new_m = dict(m) if m else {}
                new_m["embedding_model"] = target_model_id
                updated_metas.append(new_m)

            # Issue #200: use the TARGET model, not the Gemini legacy path.
            embeddings = _embed_texts(batch_docs, target_model_id, genai_client)
            temp_col.upsert(
                ids=batch_ids,
                documents=batch_docs,
                metadatas=updated_metas,
                embeddings=embeddings,
            )
            log.info("Re-embedding chunk %d/%d…", min(batch_end, len(original_docs)), len(original_docs))

        # Step 4: Validate
        new_count = temp_col.count()
        if new_count != original_count:
            print(json.dumps({
                "error": "REEMBED_VALIDATION_FAILED",
                "expected_count": original_count,
                "actual_count": new_count,
            }))
            sys.exit(1)

        if dry_run:
            # Clean up temp collection
            chroma_client.delete_collection(name=temp_name)
            print(json.dumps({
                "action": "re-embed",
                "status": "dry_run",
                "collection": collection_name,
                "target_model": target_model_id,
                "document_count": original_count,
                "validation": "passed",
            }, indent=2))
            return

        # Step 5-7: Delete original, recreate with same name, move data
        log.info("Validation passed. Replacing original collection…")
        chroma_client.delete_collection(name=collection_name)

        new_col = chroma_client.get_or_create_collection(name=collection_name)

        # Copy data from temp to new
        temp_data = temp_col.get(include=["documents", "metadatas", "embeddings"])
        temp_ids = temp_data.get("ids", [])
        temp_docs = temp_data.get("documents", [])
        temp_metas = temp_data.get("metadatas", [])
        temp_embeddings = temp_data.get("embeddings", [])

        if temp_embeddings is not None:
            temp_embeddings = [
                e.tolist() if hasattr(e, "tolist") else list(e) if e is not None else []
                for e in temp_embeddings
            ]

        for i in range(0, len(temp_ids), BATCH):
            batch_end = min(i + BATCH, len(temp_ids))
            new_col.upsert(
                ids=temp_ids[i:batch_end],
                documents=temp_docs[i:batch_end],
                metadatas=temp_metas[i:batch_end] if temp_metas else None,
                embeddings=temp_embeddings[i:batch_end] if temp_embeddings else None,
            )

        # Update collection metadata
        _set_collection_metadata(new_col, target_model_id)

        # Clean up temp collection
        chroma_client.delete_collection(name=temp_name)

        sync_to_gcs(gcs_client)

        print(json.dumps({
            "action": "re-embed",
            "status": "success",
            "collection": collection_name,
            "target_model": target_model_id,
            "document_count": new_count,
        }, indent=2))

    except Exception as exc:
        log.error("Re-embed failed: %s — original collection preserved", exc)
        # Try to clean up temp on error
        try:
            chroma_client.delete_collection(name=temp_name)
        except Exception:
            log.warning("Failed to clean up temporary collection '%s'", temp_name)
        raise


# ── CLI ──────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Roho's ChromaDB Knowledge Base Manager — multi-collection (SPEC-RAG-001)"
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=[
            "query",
            "ingest",
            "report",
            "optimize",
            "list-collections",
            "create-collection",
            "delete-collection",
            "delete-source",
            "backup",
            "restore",
            "benchmark",
            "re-embed",
        ],
        help="Action to perform.",
    )
    parser.add_argument(
        "--collection",
        type=str,
        default=None,
        help=(
            "Target collection name. Overrides CHROMA_COLLECTION_NAME env var. "
            f"Defaults to '{DEFAULT_COLLECTION}'."
        ),
    )
    parser.add_argument(
        "--all-collections",
        action="store_true",
        help="Query across ALL collections and merge results (query only).",
    )
    parser.add_argument("--query", type=str, help="Text to query the DB.")
    parser.add_argument(
        "--n-results", type=int, default=3, help="Number of results for query."
    )
    parser.add_argument(
        "--file", type=str, help="Path to markdown file to ingest."
    )
    parser.add_argument(
        "--source-name", type=str, help="Metadata source name for ingestion / delete-source."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run optimization/re-embed without making permanent changes.",
    )
    parser.add_argument(
        "--backup-timestamp",
        type=str,
        default=None,
        help="Restore from a specific backup timestamp (e.g. 20260307T120000Z). Omit to use latest.",
    )
    # SPEC-RAG-001: New arguments
    parser.add_argument(
        "--embedding-model",
        type=str,
        default=None,
        help=f"Embedding model to use. Supported: {', '.join(EMBEDDING_REGISTRY.keys())}. "
             f"Default: {DEFAULT_EMBEDDING_MODEL_ID}",
    )
    parser.add_argument(
        "--search-mode",
        choices=["semantic", "keyword", "hybrid"],
        default="semantic",
        help="Search mode for query action (REQ-RAG-004). Default: semantic.",
    )
    parser.add_argument(
        "--semantic-weight",
        type=float,
        default=0.5,
        help="Weight for semantic results in hybrid fusion (0.0-1.0). Default: 0.5.",
    )
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="Enable cross-encoder reranking of results (REQ-RAG-005).",
    )
    parser.add_argument(
        "--where",
        type=str,
        default=None,
        help='Metadata filter as JSON string (REQ-RAG-704). Example: \'{"urgency": "high"}\'',
    )
    parser.add_argument(
        "--benchmark-file",
        type=str,
        default=None,
        help="Path to JSONL benchmark file (for benchmark action).",
    )
    parser.add_argument(
        "--embedding-models",
        type=str,
        default=None,
        help="Comma-separated list of models to benchmark. Omit for all registered models.",
    )

    args = parser.parse_args()

    # Check env-based rerank setting
    rerank = args.rerank or os.environ.get("RAG_RERANK_ENABLED", "").lower() in ("true", "1", "yes")

    if CHROMA_SERVER_URL:
        log.info("Connecting to ChromaDB server at %s", CHROMA_SERVER_URL)
        chroma_client = chromadb.HttpClient(host=CHROMA_SERVER_URL.rstrip("/").split("//")[-1].split(":")[0],
                                            port=int(CHROMA_SERVER_URL.rstrip("/").split(":")[-1]) if ":" in CHROMA_SERVER_URL.split("//")[-1] else 8000)
        gcs_client = None
    else:
        gcs_client = _get_gcs_client()
        sync_from_gcs(gcs_client)
        chroma_client = chromadb.PersistentClient(path=LOCAL_CHROMA_DIR)

    # ── Actions that don't need a specific collection ─────────────────
    if args.action == "report":
        handle_report(chroma_client)
        return

    if args.action == "list-collections":
        handle_list_collections(chroma_client)
        return

    if args.action == "backup":
        handle_backup(chroma_client, None)
        return

    if args.action == "restore":
        handle_restore(chroma_client, args.backup_timestamp)
        return

    if args.action == "benchmark":
        if not args.benchmark_file:
            parser.error("--benchmark-file is required for benchmark action.")
        genai_client = _get_genai_client()
        handle_benchmark(
            chroma_client, genai_client,
            args.benchmark_file, args.embedding_models, args.n_results,
        )
        return

    # ── Collection-targeted actions ───────────────────────────────────
    collection_name = resolve_collection_name(args.collection)

    if args.action == "create-collection":
        handle_create_collection(chroma_client, collection_name, gcs_client)
        return

    if args.action == "delete-collection":
        handle_delete_collection(chroma_client, collection_name, gcs_client)
        return

    if args.action == "query":
        if not args.query:
            parser.error("--query is required for query action.")
        embedding_model_id = _resolve_embedding_model_id(args.embedding_model)
        # Issue #200: only require a Gemini key when querying a Gemini-embedded collection.
        # For new collections (nomic-embed-text-v1.5 default) no API key is needed.
        _gemini_key = (os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()
        genai_client = genai.Client(api_key=_gemini_key) if _gemini_key else None
        if args.all_collections:
            handle_query_all(
                chroma_client, genai_client, args.query, args.n_results,
                search_mode=args.search_mode,
                semantic_weight=args.semantic_weight,
                rerank=rerank,
                where_filter=args.where,
            )
        else:
            collection = _get_collection(chroma_client, collection_name)
            handle_query(
                collection, genai_client, args.query, args.n_results, collection_name,
                search_mode=args.search_mode,
                semantic_weight=args.semantic_weight,
                rerank=rerank,
                where_filter=args.where,
                embedding_model_id=embedding_model_id,
            )
        return

    if args.action == "ingest":
        if not args.file or not args.source_name:
            parser.error("--file and --source-name are required for ingest.")
        embedding_model_id = _resolve_embedding_model_id(args.embedding_model)
        # Issue #200: only init Gemini client when explicitly using gemini-embedding-001.
        _gemini_key = (os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()
        genai_client = genai.Client(api_key=_gemini_key) if _gemini_key else None
        collection = _get_collection(chroma_client, collection_name)
        handle_ingest(
            collection, genai_client, gcs_client,
            args.file, args.source_name, collection_name,
            embedding_model_id=embedding_model_id,
        )
        return

    if args.action == "delete-source":
        if not args.source_name:
            parser.error("--source-name is required for delete-source.")
        collection = _get_collection(chroma_client, collection_name, create_if_missing=False)
        handle_delete_source(collection, gcs_client, args.source_name, collection_name)
        return

    if args.action == "optimize":
        collection = _get_collection(chroma_client, collection_name)
        handle_optimize(collection, gcs_client, args.dry_run, collection_name)
        return

    if args.action == "re-embed":
        embedding_model_id = _resolve_embedding_model_id(args.embedding_model)
        if not embedding_model_id or embedding_model_id == "gemini-embedding-001":
            parser.error("--embedding-model is required for re-embed (specify a new model, e.g. nomic-embed-text-v1.5).")
        # Issue #200: re-embed with a local model doesn't need Gemini.
        _gemini_key = (os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()
        genai_client = genai.Client(api_key=_gemini_key) if _gemini_key else None
        handle_re_embed(
            chroma_client, genai_client, gcs_client,
            collection_name, embedding_model_id, args.dry_run,
        )
        return


if __name__ == "__main__":
    main()
