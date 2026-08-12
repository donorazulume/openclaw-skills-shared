"""Thin HTTP client for ``openclaw-mcp-productivity`` (SPEC-RES-001)."""

from __future__ import annotations

import json as _json
import logging
import os
import uuid
from typing import Any
import sys
import pathlib
_LIB_DIR = str(pathlib.Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
from token_resolver import resolve_secret  # noqa: E402

log = logging.getLogger("openclaw.mcp_productivity")

DEFAULT_URL = "http://openclaw-mcp-productivity:8104"
DEFAULT_TIMEOUT_SEC = 30


class ProductivityMCPError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _base_url() -> str:
    return os.environ.get("MCP_PRODUCTIVITY_URL", DEFAULT_URL).rstrip("/")


def _bearer() -> str:
    # Try agent-specific dedicated token first
    agent = os.environ.get("OPENCLAW_AGENT_NAME", "roho").upper()
    tok = resolve_secret(f"MCP_TOKEN_PROD_{agent}")
    if not tok:
        tok = resolve_secret(f"MCP_TOKEN_{agent}")
    if not tok:
        tok = resolve_secret("MCP_PRODUCTIVITY_TOKEN")
    if not tok:
        tok = resolve_secret("MCP_TOKEN_PROD_ROHO")
    if not tok:
        tok = resolve_secret("MCP_TOKEN_ROHO")
    if not tok:
        raise ProductivityMCPError("No bearer token found for MCP Productivity.")
    return tok


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_bearer()}",
        "Content-Type": "application/json",
    }


def call(tool: str, arguments: dict[str, Any] | None = None, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Any:
    """Invoke a tool on openclaw-mcp-productivity over MCP JSON-RPC 2.0."""
    try:
        import requests
    except ImportError as exc:
        raise ProductivityMCPError(f"`requests` not available: {exc}") from exc

    url = f"{_base_url()}/mcp"
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }
    try:
        resp = requests.post(url, json=body, headers=_headers(), timeout=timeout)
    except requests.RequestException as exc:
        raise ProductivityMCPError(f"MCP Productivity unreachable at {url}: {exc}", status=0) from exc

    if resp.status_code == 401:
        raise ProductivityMCPError("MCP Productivity rejected bearer token", status=401)
    if resp.status_code >= 400:
        raise ProductivityMCPError(
            f"MCP Productivity HTTP {resp.status_code} for tool={tool}: {resp.text[:300]}",
            status=resp.status_code,
        )
    try:
        rpc = resp.json()
    except ValueError as exc:
        raise ProductivityMCPError(f"Bad JSON from MCP Productivity: {exc}", status=resp.status_code) from exc

    if "error" in rpc:
        err = rpc["error"] or {}
        raise ProductivityMCPError(f"MCP Productivity JSON-RPC error: {err.get('message') or err}")

    result = rpc.get("result") or {}
    content = result.get("content") or []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            try:
                return _json.loads(text)
            except _json.JSONDecodeError:
                return text
    return result
