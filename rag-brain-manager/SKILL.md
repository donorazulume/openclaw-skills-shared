---
name: rag-brain-manager
description: Synchronizes, queries, and manages Roho's multi-collection ChromaDB knowledge base with hybrid search, pluggable embeddings, and benchmarking.
metadata: {"openclaw":{"requires":{"bins":["python3"]},"emoji":"🧠","skillVersion":"1.1.0"}}
---

Manages the ChromaDB vector database (Central Brain). Supports two modes:

- **Server mode** (default when `CHROMA_SERVER_URL` is set): connects to the shared `rag-brain` ChromaDB container via HTTP. No GCS sync needed.
- **Embedded mode** (fallback): uses local persistent storage synced to the `ai-letter-analyst-state` GCS bucket.

Supports multiple named collections — each collection is a separate knowledge domain (e.g. `letters`, `mortgages`, `projects`).

Supports pluggable embedding models, hybrid search (semantic + BM25 keyword), cross-encoder reranking, and embedding benchmarking (SPEC-RAG-001). **SPEC-RAG-003:** routine retrieval should prefer **`openbrain-client --action smart-query`** against the MCP server so governance + compression apply; **SPEC-RAG-004:** optional **`--expand-query`** lives on **`openbrain-client`** (not this skill) — `rag-brain-manager` talks to Chroma directly and does not run the LLM rewriter. Direct **`rag-brain-manager`** remains for operator/Chroma maintenance.

**Issue #309:** Shared logic lives in **`packages/openclaw_skills/openclaw_skills/rag_brain_manager.py`**; this skill’s **`manager.py`** bootstraps `sys.path` and delegates — sync **`packages/openclaw_skills`** whenever you sync **`skills/`** / **`skills-amara/`**.

## Issue #300 — hostname and bare `chromadb` clients

- **Hostname:** **`rag-brain`** (never assume the service is named `chromadb` in Compose).
- **Do not** use raw `chromadb.HttpClient(...)` against `rag-brain:8000` without the same embedding pipeline as MCP / `rag-brain-manager`; the default ONNX mini-LM embeddings are **384-d** while `open_brain` expects **768-d** (`nomic-embed-text-v1.5`). See **[docs/open-brain-chroma-query-paths.md](../../docs/open-brain-chroma-query-paths.md)**.

## Workflow

**Server mode** (`CHROMA_SERVER_URL=http://rag-brain:8000`):
1. **Connect** — HTTP client to the ChromaDB server.
2. **Execute** — run the requested vector operation.
3. **Embed** — vectorize text using the active embedding model.

**Embedded mode** (no `CHROMA_SERVER_URL`):
1. **Sync Down** — pull ChromaDB files from GCS to local persistent storage.
2. **Execute** — run the requested vector operation locally.
3. **Embed** — vectorize text using the active embedding model.
4. **Sync Up** — if the operation mutated data, push updated files back to GCS.

Roho chains `document-processor` (PDF → Markdown → metadata.json) then this skill (Markdown → vectors).

## Multi-Collection Design

Every action accepts `--collection <name>` to target a specific collection.
Precedence: `--collection` CLI flag > `CHROMA_COLLECTION_NAME` env var > `open_brain` default.

Use `--all-collections` with `--action query` to search across every collection at once.

Collection names: 3–63 chars, alphanumeric/hyphen/underscore, must start and end with alphanumeric.

## Capabilities

### Discovery
- **list-collections**: List all collections with document counts, source counts, embedding model, and health warnings.
- **report**: Health summary — all collections with document counts and embedding model info.

### Collection Lifecycle
- **create-collection**: Explicitly create a named collection (idempotent — no error if it exists).
- **delete-collection**: Permanently delete a collection and sync the change to GCS.

### Ingestion
- **ingest**: Chunk a Markdown file, embed each chunk, upsert into a named collection. Automatically merges `.metadata.json` sidecar if present. Syncs to GCS.
- **delete-source**: Remove all chunks from a specific source document (use before re-ingesting an updated file).

### Querying
- **query**: Search within a single collection (or all collections with `--all-collections`). Supports three search modes:
  - `semantic` (default) — pure vector similarity search
  - `keyword` — BM25 keyword-based search
  - `hybrid` — fuses semantic + keyword results via Reciprocal Rank Fusion (RRF)
- **`--rerank`**: Optional cross-encoder reranking for higher-precision results.
- **`--where`**: Metadata filters (JSON string) passed to ChromaDB `where` parameter.

### Embedding Management
- **benchmark**: Evaluate embedding models against a curated JSONL test set. Computes NDCG@5, MRR@5, Recall@5 per model.
- **re-embed**: Migrate a collection to a new embedding model without data loss.

### Findings (SPEC-SYSADMIN-002.1)

Structured `roho_review` rows in `open_brain` — one Chroma document per finding, canonical text ≤600 chars.

- **upsert-finding**: `--repo donorazulume/openclaw-docker --finding-json '{...}' --run-id ULID --week-iso 2026-W19 [--issue-url URL] [--fingerprint ...]`
- **mark-status**: `--fingerprint <12c> --mark-to-status fixed|wontfix|false-positive|open|filed --reason "closed on GitHub"` (never deletes embeddings)
- **query-findings**: `--where '{"source":"roho_review","repo":"donorazulume/openclaw-rob"}' --since-weeks 8 --n-results 30 [--text-query "optional semantic"]`

### Maintenance
- **optimize**: Deduplicate chunks by content hash within a collection. Supports `--dry-run`.

### Backup & Recovery
- **backup**: Export ALL collections (documents, embeddings, metadata) to GCS as timestamped JSON files. Format-independent — survives ChromaDB version upgrades and volume corruption.
- **restore**: Restore collections from a GCS backup. Uses the latest backup by default, or `--backup-timestamp` for a specific one.

## Usage

### List all collections and their sources
```bash
python3 {baseDir}/manager.py --action list-collections
```

### Create a new collection
```bash
python3 {baseDir}/manager.py --action create-collection --collection mortgage_docs
```

### Ingest a Markdown file into a specific collection
```bash
python3 {baseDir}/manager.py --action ingest \
  --collection mortgage_docs \
  --file "/path/to/Precise-Mortgages-Arrears.md" \
  --source-name "2025-04-22_Precise-Mortgages_Arrears"
```

### Ingest with a specific embedding model
```bash
python3 {baseDir}/manager.py --action ingest \
  --collection mortgage_docs \
  --file "/path/to/document.md" \
  --source-name "document-v1" \
  --embedding-model nomic-embed-text-v1.5
```

### Query — semantic (default)
```bash
python3 {baseDir}/manager.py --action query \
  --collection mortgage_docs \
  --query "What are the arrears terms?" \
  --n-results 5
```

### Query — hybrid search (semantic + keyword)
```bash
python3 {baseDir}/manager.py --action query \
  --collection mortgage_docs \
  --query "account number 12345" \
  --search-mode hybrid \
  --semantic-weight 0.3
```

### Query — keyword-only search
```bash
python3 {baseDir}/manager.py --action query \
  --collection letters \
  --query "Precise Mortgages" \
  --search-mode keyword
```

### Query with cross-encoder reranking
```bash
python3 {baseDir}/manager.py --action query \
  --collection mortgage_docs \
  --query "arrears balance Precise Mortgages" \
  --rerank \
  --n-results 5
```

### Query with metadata filters
```bash
python3 {baseDir}/manager.py --action query \
  --collection letters \
  --query "overdue payment" \
  --where '{"document_type": "notice", "urgency": "high"}'
```

### Query across ALL collections (merged, ranked by relevance)
```bash
python3 {baseDir}/manager.py --action query \
  --all-collections \
  --query "Precise Mortgages interest rate" \
  --n-results 5 \
  --search-mode hybrid
```

### Benchmark embedding models
```bash
python3 {baseDir}/manager.py --action benchmark \
  --benchmark-file /path/to/benchmark.jsonl \
  --embedding-models gemini-embedding-001,nomic-embed-text-v1.5,bge-large-en-v1.5
```

### Re-embed a collection with a new model
```bash
# Dry run first (validates without deleting original)
python3 {baseDir}/manager.py --action re-embed \
  --collection mortgage_docs \
  --embedding-model nomic-embed-text-v1.5 \
  --dry-run

# Execute migration
python3 {baseDir}/manager.py --action re-embed \
  --collection mortgage_docs \
  --embedding-model nomic-embed-text-v1.5
```

### Re-ingest an updated document (delete old chunks first)
```bash
python3 {baseDir}/manager.py --action delete-source \
  --collection mortgage_docs \
  --source-name "2025-04-22_Precise-Mortgages_Arrears"

python3 {baseDir}/manager.py --action ingest \
  --collection mortgage_docs \
  --file "/path/to/updated.md" \
  --source-name "2025-04-22_Precise-Mortgages_Arrears"
```

### Optimize (deduplicate) a collection
```bash
python3 {baseDir}/manager.py --action optimize --collection mortgage_docs --dry-run
python3 {baseDir}/manager.py --action optimize --collection mortgage_docs
```

### Delete a collection
```bash
python3 {baseDir}/manager.py --action delete-collection --collection old_collection
```

### Health report
```bash
python3 {baseDir}/manager.py --action report
```

### Back up all collections to GCS
```bash
python3 {baseDir}/manager.py --action backup
```

### Restore from the latest backup
```bash
python3 {baseDir}/manager.py --action restore
```

### Restore from a specific backup timestamp
```bash
python3 {baseDir}/manager.py --action restore --backup-timestamp 20260307T120000Z
```

## Embedding Models

| Model ID | Type | Dimensions | Notes |
|---|---|---|---|
| `nomic-embed-text-v1.5` | Local (HuggingFace) | 768 | Default. CPU inference. Good general-purpose. |
| `gemini-embedding-001` | API (Gemini) | 768 | Deprecated fallback. Uses `GEMINI_API_KEY`. |
| `bge-large-en-v1.5` | Local (HuggingFace) | 1024 | CPU inference. BAAI model. |
| `snowflake-arctic-embed-l` | Local (HuggingFace) | 1024 | CPU inference. Strong retrieval benchmarks. |

Each collection tracks its embedding model in metadata. Ingesting with a different model than the collection's stored model will error — use `--action re-embed` to migrate.

## Collection Architecture (SPEC-RAG-002)

Knowledge is organised into two primary collections with a clear public/private boundary:

| Collection | Scope | Contents |
|---|---|---|
| `open_brain` | **Private** (default) | Unified internal knowledge base. Migrated from roho_knowledge + roho_knowledge_base. |
| `don_corpus` | **Public** | Professional portfolio and public-facing content. Used by `don_ai_interface`. |

Migrated documents carry an `original_collection` metadata field for provenance tracking. Domain-specific collections (e.g. `letters`, `mortgage_docs`) may be created for specialised use cases, but `open_brain` is the primary default target for private ingestion and queries.

## Environment Variables

* `GEMINI_API_KEY` — Google AI API key for `gemini-embedding-001` (already in Doppler). `GOOGLE_API_KEY` accepted as fallback.
* `RAG_EMBEDDING_MODEL` — Active embedding model ID (default: `nomic-embed-text-v1.5`). Override per-invocation with `--embedding-model`.
* `RAG_RERANK_ENABLED` — Set to `true` to enable cross-encoder reranking by default (default: `false`). Override per-invocation with `--rerank`.
* `GCP_SERVICE_ACCOUNT_KEY` — JSON string of a GCP service account with Storage Object Admin on the bucket.
* `CHROMA_GCS_BUCKET` — GCS bucket name (default: `ai-letter-analyst-state`).
* `CHROMA_COLLECTION_NAME` — Default collection when `--collection` is omitted (default: `open_brain`).
* `CHROMA_SERVER_URL` — ChromaDB server URL (e.g. `http://rag-brain:8000`). When set, uses server mode (recommended). When empty, falls back to embedded mode with GCS sync.
* `CHROMA_LOCAL_DIR` — Override local ChromaDB path (default: `/home/node/.openclaw/chroma_state`). Only used in embedded mode.

All secrets stored in Doppler (`openclaw-docker` project).

## Output

All actions print JSON to stdout. Logs go to stderr.
