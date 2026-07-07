#!/usr/bin/env python3
"""rag-brain-manager — thin MCP-first HTTP client shim.

All ChromaDB and GCS operations are delegated to the Open Brain MCP server
running at http://openclaw-mcp-server:8100.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("rag-brain-manager")
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stderr,
)

BASE_URL = os.environ.get("OPENBRAIN_URL", "http://openclaw-mcp-server:8100").rstrip("/")
DEFAULT_COLLECTION = os.environ.get("CHROMA_COLLECTION_NAME", "open_brain")


def _get_token() -> str:
    """Resolve bearer token from environment, preferring agent-specific keys."""
    for key in ("MCP_TOKEN_ROHO", "MCP_TOKEN_AMARA", "MCP_TOKEN_ROB", "MCP_TOKEN"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return ""


def _api(method: str, path: str, data: dict | None = None, *, timeout: float | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    headers = {"Content-Type": "application/json"}
    token = _get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    t = timeout or 30.0

    try:
        with urllib.request.urlopen(req, timeout=t) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
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


def _load_metadata_sidecar(file_path: str) -> dict[str, Any]:
    p = Path(file_path)
    sidecar_path = p.parent / f"{p.stem}.metadata.json"
    if not sidecar_path.exists():
        return {}
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        result: dict[str, Any] = {}
        for key in ("document_type", "date_issued", "sender", "subject", "urgency"):
            if key in data and isinstance(data[key], str):
                result[key] = data[key]
        if "entities" in data and isinstance(data["entities"], list):
            result["entities"] = ",".join(str(e) for e in data["entities"])
        return result
    except Exception as exc:
        log.warning("Failed to read metadata sidecar %s: %s", sidecar_path, exc)
        return {}


def _finding_doc_id(repo_full: str, fp: str) -> str:
    slug = repo_full.split("/", 1)[1]
    return f"roho_review__{slug}__{fp}"


def _fingerprint_review(repo_full: str, finding: dict[str, Any]) -> str:
    h = hashlib.sha256(
        f"{repo_full}|{finding.get('category','')}|{finding.get('affected_path','')}|{finding.get('title','')}".encode()
    ).hexdigest()[:12]
    return h


def canonical_finding_document(repo_full: str, finding: dict[str, Any]) -> str:
    title = str(finding.get("title") or "").strip()
    summary = str(finding.get("summary") or "").strip()
    sugg = str(finding.get("suggested_resolution") or "").strip()
    body = f"[{repo_full}] {title}\n\n{summary}\n\nSuggested resolution: {sugg}\n".strip()
    if len(body) > 600:
        body = body[:597].rstrip() + "..."
    return body


def _status_history_parse(s: str | None) -> list[dict[str, Any]]:
    if not s:
        return []
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _status_history_dump(hist: list[dict[str, Any]]) -> str:
    return json.dumps(hist, separators=(",", ":"))


def _chroma_flat_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = json.dumps(v, separators=(",", ":"))
    return out


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_MARK_STATUS_LEGAL = frozenset({"open", "filed", "fixed", "wontfix", "false-positive"})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Lightweight MCP-first ChromaDB Knowledge Base Manager Client"
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
            "upsert-finding",
            "mark-status",
            "query-findings",
        ],
        help="Action to perform.",
    )
    parser.add_argument(
        "--collection",
        type=str,
        default=None,
        help=f"Target collection name. Defaults to '{DEFAULT_COLLECTION}'.",
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
        help="Restore from a specific backup timestamp.",
    )
    parser.add_argument(
        "--embedding-model",
        type=str,
        default=None,
        help="Embedding model to use.",
    )
    parser.add_argument(
        "--search-mode",
        choices=["semantic", "keyword", "hybrid"],
        default="semantic",
        help="Search mode for query action.",
    )
    parser.add_argument(
        "--semantic-weight",
        type=float,
        default=0.5,
        help="Weight for semantic results in hybrid fusion.",
    )
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="Enable cross-encoder reranking of results.",
    )
    parser.add_argument(
        "--where",
        type=str,
        default=None,
        help="Metadata filter as JSON string.",
    )
    parser.add_argument(
        "--benchmark-file",
        type=str,
        default=None,
        help="Path to JSONL benchmark file.",
    )
    parser.add_argument(
        "--embedding-models",
        type=str,
        default=None,
        help="Comma-separated list of models to benchmark.",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="upsert-finding: full GitHub repo.",
    )
    parser.add_argument(
        "--finding-json",
        type=str,
        default=None,
        help="upsert-finding: JSON string.",
    )
    parser.add_argument(
        "--finding-file",
        type=str,
        default=None,
        help="upsert-finding: path to JSON file.",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="upsert-finding: ecosystem run / ULID string.",
    )
    parser.add_argument(
        "--week-iso",
        type=str,
        default=None,
        help="upsert-finding: ISO week label.",
    )
    parser.add_argument(
        "--gateway-version",
        type=str,
        default="",
        help="upsert-finding: OpenClaw gateway version string.",
    )
    parser.add_argument(
        "--issue-url",
        type=str,
        default="",
        help="upsert-finding: GitHub issue URL if filed.",
    )
    parser.add_argument(
        "--fingerprint",
        type=str,
        default=None,
        help="mark-status: 12-char fingerprint.",
    )
    parser.add_argument(
        "--mark-to-status",
        type=str,
        default=None,
        choices=sorted(_MARK_STATUS_LEGAL),
        help="mark-status: target lifecycle status.",
    )
    parser.add_argument(
        "--reason",
        type=str,
        default="",
        help="mark-status: reason string.",
    )
    parser.add_argument(
        "--since-weeks",
        type=int,
        default=None,
        help="query-findings: last_seen_at cutoff.",
    )
    parser.add_argument(
        "--text-query",
        type=str,
        default=None,
        help="query-findings: text query string.",
    )

    args = parser.parse_args()
    collection_name = args.collection or DEFAULT_COLLECTION

    # ── Handle simple collection management actions ──
    if args.action == "list-collections":
        res = _api("POST", "/api/collections/list")
        if "error" in res:
            print(json.dumps(res))
            sys.exit(1)
        collections = res.get("collections", [])
        print(json.dumps({
            "action": "list-collections",
            "total_collections": len(collections),
            "collections": collections,
        }, indent=2))
        return

    if args.action == "create-collection":
        res = _api("POST", f"/api/collections/create?collection_name={collection_name}")
        if "error" in res:
            print(json.dumps(res))
            sys.exit(1)
        print(json.dumps({
            "action": "create-collection",
            "status": "created",
            "collection": collection_name,
        }))
        return

    if args.action == "delete-collection":
        res = _api("POST", f"/api/collections/delete?collection_name={collection_name}")
        if "error" in res:
            print(json.dumps(res))
            sys.exit(1)
        print(json.dumps({
            "action": "delete-collection",
            "status": "deleted",
            "collection": collection_name,
        }))
        return

    if args.action == "report":
        res = _api("POST", "/api/collections/report")
        print(json.dumps(res, indent=2))
        return

    if args.action == "optimize":
        # Stub per Option A: MCP handles optimization automatically.
        print(json.dumps({
            "action": "optimize",
            "status": "success",
            "message": "Optimization deferred: handled automatically by Open Brain MCP server.",
        }))
        return

    # ── Actions that are deferred or unsupported ──
    if args.action in ("backup", "restore", "benchmark", "re-embed"):
        print(json.dumps({
            "error": "UNSUPPORTED_ACTION",
            "action": args.action,
            "message": f"Action '{args.action}' is deferred or not supported via MCP endpoint.",
        }))
        sys.exit(1)

    # ── Ingest Action ──
    if args.action == "ingest":
        if not args.file or not args.source_name:
            parser.error("--file and --source-name are required for ingest.")
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as exc:
            print(json.dumps({"error": {"code": "FILE_ERROR", "message": str(exc)}}))
            sys.exit(1)

        metadata = _load_metadata_sidecar(args.file)
        metadata["source"] = args.source_name

        res = _api(
            "POST",
            "/api/semantic/ingest",
            {
                "content": content,
                "source_id": args.source_name,
                "collection": collection_name,
                "metadata": metadata,
            },
            timeout=300,
        )
        print(json.dumps(res))
        return

    # ── Delete Source Action ──
    if args.action == "delete-source":
        if not args.source_name:
            parser.error("--source-name is required for delete-source.")
        # Under upsert semantics, ingesting an empty document replaces the source with 0 chunks.
        res = _api(
            "POST",
            "/api/semantic/ingest",
            {
                "content": "",
                "source_id": args.source_name,
                "collection": collection_name,
            },
            timeout=30.0,
        )
        print(json.dumps({
            "action": "delete-source",
            "status": "success",
            "source_removed": args.source_name,
            "detail": res,
        }))
        return

    # ── Query Action ──
    if args.action == "query":
        if not args.query:
            parser.error("--query is required for query action.")
        where = _parse_json_arg(args.where, "where")

        if args.all_collections:
            list_res = _api("POST", "/api/collections/list")
            cols = list_res.get("collections", [])
            t0 = time.time()
            all_matches = []
            searched = []
            for col in cols:
                name = col.get("name")
                if not name:
                    continue
                searched.append(name)
                payload = {
                    "query": args.query,
                    "collection": name,
                    "n_results": args.n_results,
                    "where_filter": where,
                    "search_mode": args.search_mode,
                    "semantic_weight": args.semantic_weight,
                }
                q_res = _api("POST", "/api/semantic/query", payload)
                matches = q_res.get("matches", [])
                for m in matches:
                    m["collection"] = name
                all_matches.extend(matches)

            # Sort globally by distance/score
            if args.search_mode == "hybrid":
                all_matches.sort(key=lambda m: m.get("fusion_score", 0), reverse=True)
            elif args.search_mode == "keyword":
                all_matches.sort(key=lambda m: m.get("bm25_score", 0), reverse=True)
            else:
                all_matches.sort(key=lambda m: m.get("distance") if m.get("distance") is not None else float("inf"))

            print(json.dumps({
                "action": "query",
                "query": args.query,
                "collections_searched": searched,
                "n_results": args.n_results,
                "search_mode": args.search_mode,
                "matches": all_matches[:args.n_results],
                "query_latency_ms": int((time.time() - t0) * 1000),
                "reranked": False,
            }, indent=2))
        else:
            payload = {
                "query": args.query,
                "collection": collection_name,
                "n_results": args.n_results,
                "where_filter": where,
                "search_mode": args.search_mode,
                "semantic_weight": args.semantic_weight,
            }
            res = _api("POST", "/api/semantic/query", payload)
            print(json.dumps(res, indent=2))
        return

    # ── Upsert Finding Action ──
    if args.action == "upsert-finding":
        if not args.repo:
            parser.error("--repo is required for upsert-finding")
        blob: dict[str, Any] | None = None
        if args.finding_file:
            try:
                blob = json.loads(Path(args.finding_file).read_text(encoding="utf-8"))
            except Exception as exc:
                parser.error(f"finding-file unreadable: {exc}")
        elif args.finding_json:
            try:
                blob = json.loads(args.finding_json)
            except json.JSONDecodeError as exc:
                parser.error(f"finding-json invalid: {exc}")
        else:
            parser.error("upsert-finding requires --finding-json or --finding-file")

        if not isinstance(blob, dict):
            parser.error("finding must be a JSON object")
        if not args.run_id or not args.week_iso:
            parser.error("upsert-finding requires --run-id and --week-iso")

        fp = args.fingerprint or _fingerprint_review(args.repo, blob)
        doc_id = _finding_doc_id(args.repo, fp)
        now = _now_iso_z()
        canonical = canonical_finding_document(args.repo, blob)
        slug = args.repo.split("/", 1)[1]

        # Get existing finding to preserve history
        get_res = _api("POST", "/api/semantic/get", {
            "collection": collection_name,
            "ids": [doc_id],
        })

        existing_ids = get_res.get("ids", [])
        hist: list[dict[str, Any]] = []
        meta: dict[str, Any] = {}

        if not existing_ids:
            meta = {
                "source": "roho_review",
                "repo": args.repo,
                "repo_slug": slug,
                "severity": str(blob.get("severity") or "medium"),
                "category": str(blob.get("category") or ""),
                "affected_path": str(blob.get("affected_path") or "*"),
                "fingerprint": fp,
                "status": "open",
                "first_seen_at": now,
                "last_seen_at": now,
                "seen_count": 1,
                "run_id": args.run_id,
                "week_iso": args.week_iso,
                "gateway_version": args.gateway_version or "",
                "issue_url": args.issue_url or "",
                "estimated_effort": str(blob.get("estimated_effort") or "small"),
                "status_history": _status_history_dump(hist),
                "finding_title": str(blob.get("title") or "")[:240],
            }
        else:
            om = dict(get_res.get("metadatas", [{}])[0] or {})
            first_seen = str(om.get("first_seen_at") or now)
            old_status = str(om.get("status") or "open")
            seen = int(om.get("seen_count") or 0)
            hist = _status_history_parse(str(om.get("status_history")))
            prev_run = str(om.get("run_id") or "")

            seen += 1
            if prev_run != args.run_id:
                if old_status in ("fixed", "wontfix", "false-positive"):
                    hist.append({
                        "ts": now,
                        "from": old_status,
                        "to": old_status,
                        "reason": "Re-detected — status preserved per SPEC-SYSADMIN.1-207",
                    })

            meta = {
                **om,
                "source": "roho_review",
                "repo": args.repo,
                "repo_slug": slug,
                "severity": str(blob.get("severity") or om.get("severity") or "medium"),
                "category": str(blob.get("category") or om.get("category") or ""),
                "affected_path": str(blob.get("affected_path") or om.get("affected_path") or "*"),
                "fingerprint": fp,
                "first_seen_at": first_seen,
                "last_seen_at": now,
                "seen_count": seen,
                "run_id": args.run_id,
                "week_iso": args.week_iso,
                "gateway_version": args.gateway_version or str(om.get("gateway_version") or ""),
                "issue_url": args.issue_url or str(om.get("issue_url") or ""),
                "estimated_effort": str(blob.get("estimated_effort") or om.get("estimated_effort") or "small"),
                "status": old_status,
                "status_history": _status_history_dump(hist),
                "finding_title": str(blob.get("title") or om.get("finding_title") or "")[:240],
            }

        # Generate embedding via MCP embed route
        emb_res = _api("POST", "/api/embed", {"texts": [canonical]})
        embeddings = emb_res.get("embeddings", [])
        if not embeddings:
            print(json.dumps({"error": "EMBEDDING_FAILED"}))
            sys.exit(1)

        flat_meta = _chroma_flat_metadata(meta)
        upsert_res = _api("POST", "/api/semantic/upsert", {
            "collection": collection_name,
            "ids": [doc_id],
            "documents": [canonical],
            "embeddings": embeddings,
            "metadatas": [flat_meta],
        })

        if "error" in upsert_res:
            print(json.dumps(upsert_res))
            sys.exit(1)

        print(json.dumps({
            "action": "upsert-finding",
            "status": "success",
            "upserted": True,
            "collection": collection_name,
            "id": doc_id,
            "fingerprint": fp,
            "metadata": meta,
        }, indent=2))
        return

    # ── Mark Status Action ──
    if args.action == "mark-status":
        if not args.fingerprint or not args.mark_to_status:
            parser.error("mark-status requires --fingerprint and --mark-to-status")

        now = _now_iso_z()
        # Find document by fingerprint
        get_res = _api("POST", "/api/semantic/get", {
            "collection": collection_name,
            "where": {"fingerprint": args.fingerprint},
        })

        ids = list(get_res.get("ids") or [])
        metas = list(get_res.get("metadatas") or [])
        documents = list(get_res.get("documents") or [])

        if not ids:
            log.warning(
                "mark-status: fingerprint %s not found in collection %s (no-op, exit 0)",
                args.fingerprint,
                collection_name,
            )
            print(json.dumps({
                "action": "mark-status",
                "matched": 0,
                "updated": 0,
                "skipped_same_status": 0,
                "collection": collection_name,
            }, indent=2))
            return

        touched = 0
        skipped = 0

        for doc_id, om, doc in zip(ids, metas, documents):
            old_status = str(om.get("status") or "open")
            if old_status == args.mark_to_status:
                skipped += 1
                continue

            hist = _status_history_parse(str(om.get("status_history")))
            hist.append({
                "ts": now,
                "from": old_status,
                "to": args.mark_to_status,
                "reason": args.reason or "",
            })

            meta = {
                **om,
                "status": args.mark_to_status,
                "status_history": _status_history_dump(hist),
            }

            flat_meta = _chroma_flat_metadata(meta)

            # Retrieve embedding to avoid modifying it
            emb_res = _api("POST", "/api/embed", {"texts": [doc]})
            embeddings = emb_res.get("embeddings", [])
            if not embeddings:
                print(json.dumps({"error": "EMBEDDING_FAILED"}))
                sys.exit(1)

            upsert_res = _api("POST", "/api/semantic/upsert", {
                "collection": collection_name,
                "ids": [doc_id],
                "documents": [doc],
                "embeddings": embeddings,
                "metadatas": [flat_meta],
            })
            if "error" in upsert_res:
                print(json.dumps(upsert_res))
                sys.exit(1)
            touched += 1

        print(json.dumps({
            "action": "mark-status",
            "matched": len(ids),
            "updated": touched,
            "skipped_same_status": skipped,
            "collection": collection_name,
        }, indent=2))
        return

    # ── Query Findings Action ──
    if args.action == "query-findings":
        where_dict = _parse_json_arg(args.where, "where") or {}
        if not isinstance(where_dict, dict):
            where_dict = {}
        if "source" not in where_dict:
            where_dict["source"] = "roho_review"

        cutoff_dt: datetime | None = None
        if isinstance(args.since_weeks, int) and args.since_weeks > 0:
            cutoff_dt = datetime.now(timezone.utc) - timedelta(weeks=args.since_weeks)

        cap_n = max(1, min(args.n_results, 500))
        rows = []

        if args.text_query and args.text_query.strip():
            payload = {
                "query": args.text_query,
                "collection": collection_name,
                "n_results": cap_n * 3,
                "where_filter": where_dict,
                "search_mode": args.search_mode,
                "semantic_weight": args.semantic_weight,
            }
            q_res = _api("POST", "/api/semantic/query", payload)
            matches = q_res.get("matches", [])
            for m in matches:
                doc_id = m.get("id")
                meta = m.get("metadata", {})
                doc = m.get("document", "")
                rows.append((doc_id, meta, doc))
        else:
            get_res = _api("POST", "/api/semantic/get", {
                "collection": collection_name,
                "where": where_dict,
                "limit": cap_n * 2,
            })
            ids = get_res.get("ids", [])
            metas = get_res.get("metadatas", [])
            docs = get_res.get("documents", [])
            for doc_id, meta, doc in zip(ids, metas, docs):
                rows.append((doc_id, meta, doc))

        # Filter by weeks if specified
        output_matches = []
        for doc_id, meta, doc in rows:
            if cutoff_dt:
                lsa_str = meta.get("last_seen_at")
                if lsa_str:
                    try:
                        # Simple ISO timestamp parse
                        lsa_dt = datetime.fromisoformat(lsa_str.replace("Z", "+00:00"))
                        if lsa_dt < cutoff_dt:
                            continue
                    except Exception:
                        pass
            output_matches.append({
                "id": doc_id,
                "metadata": meta,
                "document": doc,
            })

        print(json.dumps({
            "action": "query-findings",
            "collection": collection_name,
            "matches": output_matches[:cap_n],
        }, indent=2))
        return


if __name__ == "__main__":
    main()
