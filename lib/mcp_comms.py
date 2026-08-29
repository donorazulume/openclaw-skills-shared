"""Thin HTTP client for ``openclaw-mcp-comms`` (SPEC-ARCH-001)."""

from __future__ import annotations

import logging
import os
import pathlib
import sys
from typing import Any

_LIB_DIR = str(pathlib.Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
from token_resolver import resolve_secret

log = logging.getLogger("openclaw.mcp_comms")

DEFAULT_URL = "http://openclaw-mcp-comms:8102"
DEFAULT_TIMEOUT_SEC = 30


class CommsMCPError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _base_url() -> str:
    return os.environ.get("MCP_COMMS_URL", DEFAULT_URL).rstrip("/")


def _bearer() -> str:
    # Try agent-specific dedicated token first
    agent = os.environ.get("OPENCLAW_AGENT_NAME", "roho").upper()
    tok = resolve_secret(f"MCP_TOKEN_COMMS_{agent}")
    if not tok:
        # Try system-wide dedicated comms token fallback next
        tok = resolve_secret("MCP_TOKEN_COMMS_ROHO")
    if not tok:
        # Fall back to general agent token ONLY if it is not the Notion token
        candidate = resolve_secret(f"MCP_TOKEN_{agent}")
        if candidate:
            notion_token = resolve_secret(f"MCP_TOKEN_NOTION_{agent}")
            if not notion_token or candidate != notion_token:
                tok = candidate
    if not tok:
        # Fall back to system general token ONLY if it is not the Notion token
        candidate = resolve_secret("MCP_TOKEN_ROHO")
        if candidate:
            notion_token = resolve_secret("MCP_TOKEN_NOTION_ROHO")
            if not notion_token or candidate != notion_token:
                tok = candidate
    if not tok:
        raise CommsMCPError("No bearer token found for MCP Comms.")
    return tok


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_bearer()}",
        "Content-Type": "application/json",
    }


def call(endpoint: str, body: dict[str, Any] | None = None, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Any:
    """Invoke a REST API endpoint on openclaw-mcp-comms."""
    try:
        import requests  # type: ignore
    except ImportError as exc:

        raise CommsMCPError(f"`requests` not available: {exc}") from exc

    ep = endpoint.lstrip("/")
    url = f"{_base_url()}/{ep}"
    
    # Intelligently select GET vs POST
    method = "GET" if "channels" in endpoint else "POST"
    
    try:
        if method == "GET":
            resp = requests.get(url, headers=_headers(), timeout=timeout)
        else:
            resp = requests.post(url, json=body or {}, headers=_headers(), timeout=timeout)
    except requests.RequestException as exc:
        raise CommsMCPError(f"MCP Comms unreachable at {url}: {exc}", status=0) from exc

    if resp.status_code == 401:
        raise CommsMCPError("MCP Comms rejected bearer token", status=401)
    if resp.status_code >= 400:
        raise CommsMCPError(
            f"MCP Comms HTTP {resp.status_code} for {endpoint}: {resp.text[:300]}",
            status=resp.status_code,
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise CommsMCPError(f"Bad JSON from MCP Comms: {exc}", status=resp.status_code) from exc


def request(method: str, path: str, payload: dict | None = None, params: dict | None = None, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Any:
    """Proxy general HTTP requests to Mattermost API via Comms MCP server."""
    body = {
        "method": method,
        "path": path,
        "payload": payload,
        "params": params,
    }
    return call("api/comms/request", body, timeout=timeout)

