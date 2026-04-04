#!/usr/bin/env python3
"""Open Brain client — HTTP interface to the Open Brain MCP server.

Provides CLI access to all Open Brain capabilities: entity CRUD,
hybrid search, semantic query/ingest, and collection management.

Environment variables:
  OPENBRAIN_URL       — Base URL of the MCP server (default: http://openclaw-mcp-server:8100)
  MCP_TOKEN_ROHO      — Bearer token for Roho (fallback: MCP_TOKEN)
  MCP_TOKEN_AMARA     — Bearer token for Amara
  MCP_TOKEN           — Generic bearer token fallback
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request

log = logging.getLogger("openbrain-client")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s", stream=sys.stderr)

BASE_URL = os.environ.get("OPENBRAIN_URL", "http://openclaw-mcp-server:8100")

ACTIONS = [
    "health",
    "entity-create",
    "entity-read",
    "entity-update",
    "entity-delete",
    "entity-search",
    "semantic-query",
    "semantic-ingest",
    "collection-manage",
]


def _get_token() -> str:
    """Resolve bearer token from environment, preferring agent-specific keys."""
    for key in ("MCP_TOKEN_ROHO", "MCP_TOKEN_AMARA", "MCP_TOKEN"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return ""


def _api(method: str, path: str, data: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    headers = {"Content-Type": "application/json"}
    token = _get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read())
        except Exception:
            detail = {"raw": exc.reason}
        return {"error": {"code": exc.code, "detail": detail}}
    except urllib.error.URLError as exc:
        return {"error": {"code": "CONNECTION_ERROR", "message": str(exc.reason)}}


def _parse_json_arg(value: str | None, name: str) -> dict | list | None:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": {"code": "INVALID_JSON", "message": f"--{name}: {exc}"}}))
        sys.exit(1)


def do_health(_args: argparse.Namespace) -> dict:
    return _api("GET", "/health")


def do_entity_create(args: argparse.Namespace) -> dict:
    if not args.type:
        return {"error": {"code": "MISSING_ARG", "message": "--type is required"}}
    data = _parse_json_arg(args.data, "data") or {}
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else []
    return _api("POST", "/api/entities", {
        "entity_type": args.type,
        "data": data,
        "tags": tags,
        "priority": args.priority,
        "notify": args.notify,
    })


def do_entity_read(args: argparse.Namespace) -> dict:
    if not args.type:
        return {"error": {"code": "MISSING_ARG", "message": "--type is required"}}
    if args.id:
        return _api("GET", f"/api/entities/{args.type}/{args.id}")
    params = f"?limit={args.limit}&offset={args.offset}&order_by={args.order_by}&order_dir={args.order_dir}"
    return _api("GET", f"/api/entities/{args.type}{params}")


def do_entity_update(args: argparse.Namespace) -> dict:
    if not args.type or not args.id:
        return {"error": {"code": "MISSING_ARG", "message": "--type and --id are required"}}
    updates = _parse_json_arg(args.data, "data") or {}
    return _api("PATCH", f"/api/entities/{args.type}/{args.id}", {
        "updates": updates,
        "reason": args.reason or "",
    })


def do_entity_delete(args: argparse.Namespace) -> dict:
    if not args.type or not args.id or not args.reason:
        return {"error": {"code": "MISSING_ARG", "message": "--type, --id, and --reason are required"}}
    return _api("DELETE", f"/api/entities/{args.type}/{args.id}?reason={urllib.request.quote(args.reason)}")


def do_entity_search(args: argparse.Namespace) -> dict:
    if not args.query:
        return {"error": {"code": "MISSING_ARG", "message": "--query is required"}}
    types = [t.strip() for t in args.types.split(",")] if args.types else []
    filters = _parse_json_arg(args.filters, "filters") or {}
    return _api("POST", "/api/search", {
        "query": args.query,
        "entity_types": types,
        "structured_filters": filters,
        "semantic_weight": args.semantic_weight,
        "limit": args.limit,
    })


def do_semantic_query(args: argparse.Namespace) -> dict:
    if not args.query:
        return {"error": {"code": "MISSING_ARG", "message": "--query is required"}}
    where = _parse_json_arg(args.where_filter, "where-filter") or {}
    return _api("POST", "/api/semantic/query", {
        "query": args.query,
        "collection": args.collection,
        "n_results": args.n_results,
        "where_filter": where,
    })


def do_semantic_ingest(args: argparse.Namespace) -> dict:
    if not args.source_id:
        return {"error": {"code": "MISSING_ARG", "message": "--source-id is required"}}

    content = args.content or ""
    if args.file:
        try:
            with open(args.file, "r") as f:
                content = f.read()
        except OSError as exc:
            return {"error": {"code": "FILE_ERROR", "message": str(exc)}}

    if not content:
        return {"error": {"code": "MISSING_ARG", "message": "--content or --file is required"}}

    metadata = _parse_json_arg(args.metadata, "metadata") or {}
    return _api("POST", "/api/semantic/ingest", {
        "content": content,
        "source_id": args.source_id,
        "collection": args.collection or "open_brain",
        "metadata": metadata,
    })


def do_collection_manage(args: argparse.Namespace) -> dict:
    if not args.collection_action:
        return {"error": {"code": "MISSING_ARG", "message": "--collection-action is required"}}
    params = f"?collection_name={args.collection}" if args.collection else ""
    return _api("POST", f"/api/collections/{args.collection_action}{params}")


DISPATCH = {
    "health": do_health,
    "entity-create": do_entity_create,
    "entity-read": do_entity_read,
    "entity-update": do_entity_update,
    "entity-delete": do_entity_delete,
    "entity-search": do_entity_search,
    "semantic-query": do_semantic_query,
    "semantic-ingest": do_semantic_ingest,
    "collection-manage": do_collection_manage,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Open Brain client — interact with the Open Brain MCP server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--action", required=True, choices=ACTIONS, help="Operation to perform")

    entity = parser.add_argument_group("entity arguments")
    entity.add_argument("--type", help="Entity type (contact, property, financial_entry, task, document_meta, agent_state, lead)")
    entity.add_argument("--id", help="Entity UUID")
    entity.add_argument("--data", help="JSON object for entity data or updates")
    entity.add_argument("--tags", help="Comma-separated tags")
    entity.add_argument("--priority", default="normal", choices=["low", "normal", "high", "critical"])
    entity.add_argument("--notify", action="store_true", help="Send Mattermost notification for high/critical")
    entity.add_argument("--reason", help="Reason for update or deletion")
    entity.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    entity.add_argument("--offset", type=int, default=0, help="Pagination offset")
    entity.add_argument("--order-by", default="created_at", help="Sort field")
    entity.add_argument("--order-dir", default="desc", choices=["asc", "desc"])

    search = parser.add_argument_group("search arguments")
    search.add_argument("--query", help="Natural language search query")
    search.add_argument("--types", help="Comma-separated entity types to search")
    search.add_argument("--filters", help="JSON structured filters")
    search.add_argument("--semantic-weight", type=float, default=0.5, help="0.0=structured, 1.0=semantic (default: 0.5)")

    semantic = parser.add_argument_group("semantic arguments")
    semantic.add_argument("--collection", help="ChromaDB collection name")
    semantic.add_argument("--n-results", type=int, default=5, help="Number of results (default: 5)")
    semantic.add_argument("--content", help="Text content to ingest")
    semantic.add_argument("--file", help="File path to ingest (reads content from file)")
    semantic.add_argument("--source-id", help="Unique source identifier for ingest")
    semantic.add_argument("--metadata", help="JSON metadata for ingest")
    semantic.add_argument("--where-filter", help="JSON ChromaDB where filter for query")

    coll = parser.add_argument_group("collection arguments")
    coll.add_argument("--collection-action", choices=["list", "create", "delete", "report"],
                       help="Collection management action")

    args = parser.parse_args()
    handler = DISPATCH[args.action]
    result = handler(args)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
