"""Thin HTTP client for ``openclaw-mcp-research`` (SPEC-RES-001)."""

from __future__ import annotations

import logging
import os
from typing import Any
import sys
import pathlib
_LIB_DIR = str(pathlib.Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
from token_resolver import resolve_secret

log = logging.getLogger("openclaw.mcp_research")

DEFAULT_URL = "http://openclaw-mcp-research:8105"
DEFAULT_TIMEOUT_SEC = 30


class ResearchMCPError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _base_url() -> str:
    return os.environ.get("MCP_RESEARCH_URL", DEFAULT_URL).rstrip("/")


def _bearer() -> str:
    # Try agent-specific dedicated token first
    agent = os.environ.get("OPENCLAW_AGENT_NAME", "roho").upper()
    tok = resolve_secret(f"MCP_TOKEN_RESEARCH_{agent}")
    if not tok:
        tok = resolve_secret(f"MCP_TOKEN_{agent}")
    if not tok:
        tok = resolve_secret("MCP_TOKEN_RESEARCH_ROHO")
    if not tok:
        tok = resolve_secret("MCP_TOKEN_ROHO")
    if not tok:
        raise ResearchMCPError("No bearer token found for MCP Research.")
    return tok


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_bearer()}",
        "Content-Type": "application/json",
    }


def call(endpoint: str, body: dict[str, Any] | None = None, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Any:
    """Invoke a REST API endpoint on openclaw-mcp-research."""
    try:
        import requests
    except ImportError as exc:
        raise ResearchMCPError(f"`requests` not available: {exc}") from exc

    ep = endpoint.lstrip("/")
    url = f"{_base_url()}/{ep}"
    try:
        resp = requests.post(url, json=body or {}, headers=_headers(), timeout=timeout)
    except requests.RequestException as exc:
        raise ResearchMCPError(f"MCP Research unreachable at {url}: {exc}", status=0) from exc

    if resp.status_code == 401:
        raise ResearchMCPError("MCP Research rejected bearer token", status=401)
    if resp.status_code >= 400:
        raise ResearchMCPError(
            f"MCP Research HTTP {resp.status_code} for {endpoint}: {resp.text[:300]}",
            status=resp.status_code,
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise ResearchMCPError(f"Bad JSON from MCP Research: {exc}", status=resp.status_code) from exc
