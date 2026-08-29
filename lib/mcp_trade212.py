"""Thin HTTP client for ``openclaw-mcp-trade212`` (SPEC-TIINGO-002)."""

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

log = logging.getLogger("openclaw.mcp_trade212")

DEFAULT_URL = "http://openclaw-mcp-trade212:8107"
DEFAULT_TIMEOUT_SEC = 30


class Trade212MCPError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _base_url() -> str:
    return os.environ.get("TRADE212_MCP_URL", os.environ.get("MCP_TRADE212_URL", DEFAULT_URL)).rstrip("/")


def _bearer() -> str:
    # Try agent-specific dedicated token first
    agent = os.environ.get("OPENCLAW_AGENT_NAME", "roho").upper()
    tok = resolve_secret(f"MCP_TOKEN_TRADE212_{agent}")
    if not tok:
        tok = resolve_secret(f"MCP_TOKEN_{agent}")
    if not tok:
        tok = resolve_secret("MCP_TOKEN_TRADE212_ROHO")
    if not tok:
        tok = resolve_secret("MCP_TOKEN_ROHO")
    if not tok:
        raise Trade212MCPError("No bearer token found for MCP Trade212.")
    return tok


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_bearer()}",
        "Content-Type": "application/json",
    }


def call(endpoint: str, body: dict[str, Any] | None = None, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Any:
    """Invoke a REST API endpoint on openclaw-mcp-trade212."""
    try:
        import requests
    except ImportError as exc:
        raise Trade212MCPError(f"`requests` not available: {exc}") from exc

    ep = endpoint.lstrip("/")
    url = f"{_base_url()}/{ep}"
    
    # Intelligently select GET vs POST based on endpoint name
    method = "GET" if any(x in endpoint for x in ["account-summary", "portfolio", "open-orders", "instruments", "history"]) else "POST"
    
    try:
        if method == "GET":
            resp = requests.get(url, headers=_headers(), timeout=timeout)
        else:
            resp = requests.post(url, json=body or {}, headers=_headers(), timeout=timeout)
    except requests.RequestException as exc:
        raise Trade212MCPError(f"MCP Trade212 unreachable at {url}: {exc}", status=0) from exc

    if resp.status_code == 401:
        raise Trade212MCPError("MCP Trade212 rejected bearer token", status=401)
    if resp.status_code >= 400:
        raise Trade212MCPError(
            f"MCP Trade212 HTTP {resp.status_code} for {endpoint}: {resp.text[:300]}",
            status=resp.status_code,
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise Trade212MCPError(f"Bad JSON from MCP Trade212: {exc}", status=resp.status_code) from exc
