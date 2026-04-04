---
name: openbrain-client
description: HTTP client for the Open Brain MCP server — entity CRUD, hybrid search, semantic query/ingest, and collection management
metadata: {"openclaw":{"requires":{"bins":["python3"]},"emoji":"🧠"}}
---

# Open Brain Client

HTTP client for the **Open Brain MCP server** (SPEC-MCP-001). Provides
full access to structured entity operations, semantic search, document
ingestion, and ChromaDB collection management.

## Environment Variables

| Variable | Description |
|---|---|
| `OPENBRAIN_URL` | MCP server base URL (default: `http://openclaw-mcp-server:8100`) |
| `MCP_TOKEN_ROHO` | Bearer token for Roho |
| `MCP_TOKEN_AMARA` | Bearer token for Amara |

## Actions

### health — Check MCP server status

```bash
python3 {baseDir}/client.py --action health
```

Returns PostgreSQL and ChromaDB connectivity status.

### entity-create — Create a structured entity

```bash
python3 {baseDir}/client.py --action entity-create \
  --type contact \
  --data '{"name": "John Smith", "email": "john@example.com", "phone": "+44..."}' \
  --tags "tenant,priority" \
  --priority high \
  --notify
```

Entity types: `contact`, `property`, `financial_entry`, `task`, `document_meta`, `agent_state`, `lead`

### entity-read — Read entities by type or ID

```bash
# List all contacts (newest first)
python3 {baseDir}/client.py --action entity-read --type contact --limit 10

# Get a specific entity by UUID
python3 {baseDir}/client.py --action entity-read --type property --id "uuid-here"
```

Options: `--limit`, `--offset`, `--order-by` (created_at|updated_at|priority), `--order-dir` (asc|desc)

### entity-update — Update an existing entity

```bash
python3 {baseDir}/client.py --action entity-update \
  --type contact --id "uuid-here" \
  --data '{"phone": "+44 new number"}' \
  --reason "Updated phone number"
```

### entity-delete — Soft-delete an entity (Roho only)

```bash
python3 {baseDir}/client.py --action entity-delete \
  --type contact --id "uuid-here" \
  --reason "Duplicate record"
```

### entity-search — Combined structured + semantic search

```bash
python3 {baseDir}/client.py --action entity-search \
  --query "properties with lease ending soon" \
  --types "property,contact" \
  --semantic-weight 0.7 \
  --limit 10
```

`--semantic-weight`: 0.0 = pure structured (PostgreSQL), 1.0 = pure semantic (ChromaDB)

### semantic-query — Search ChromaDB collections

```bash
python3 {baseDir}/client.py --action semantic-query \
  --query "tenant complaints about heating" \
  --collection open_brain \
  --n-results 5
```

### semantic-ingest — Ingest documents into ChromaDB

```bash
# Ingest from inline content
python3 {baseDir}/client.py --action semantic-ingest \
  --source-id "meeting-notes-2026-03" \
  --content "# Meeting Notes\n\nDiscussed lease renewals..."

# Ingest from file
python3 {baseDir}/client.py --action semantic-ingest \
  --source-id "policy-doc-v2" \
  --file /path/to/document.md \
  --collection open_brain
```

### collection-manage — Manage ChromaDB collections

```bash
# List all collections
python3 {baseDir}/client.py --action collection-manage --collection-action list

# Collection health report
python3 {baseDir}/client.py --action collection-manage --collection-action report

# Create a new collection
python3 {baseDir}/client.py --action collection-manage --collection-action create --collection my_collection

# Delete a collection
python3 {baseDir}/client.py --action collection-manage --collection-action delete --collection my_collection
```

## Output Format

All actions return JSON to stdout. Errors follow the structure:

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable description"
  }
}
```

## Authentication

The client uses bearer tokens from environment variables. Token resolution order:
1. `MCP_TOKEN_ROHO` (Roho's dedicated token)
2. `MCP_TOKEN_AMARA` (Amara's dedicated token)
3. `MCP_TOKEN` (generic fallback)
