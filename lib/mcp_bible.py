"""Thin HTTP client for openclaw-mcp-bible (SPEC-BIBLE-001 / #446)."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any
import pathlib

_LIB_DIR = str(pathlib.Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
from token_resolver import resolve_secret

log = logging.getLogger("openclaw.mcp_bible")

DEFAULT_URL = "http://openclaw-mcp-bible:8114"
DEFAULT_TIMEOUT_SEC = 15.0


class BibleMCPError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _base_url() -> str:
    return os.environ.get("MCP_BIBLE_URL", DEFAULT_URL).rstrip("/")


def _bearer() -> str:
    agent = os.environ.get("OPENCLAW_AGENT_NAME", "roho").upper()
    tok = resolve_secret(f"MCP_TOKEN_BIBLE_{agent}")
    if not tok:
        tok = resolve_secret(f"MCP_TOKEN_{agent}")
    if not tok:
        tok = resolve_secret("MCP_TOKEN_BIBLE_ROHO")
    if not tok:
        tok = resolve_secret("MCP_TOKEN_ROHO")
    if not tok:
        # Fallback dummy token for local dev/testing if unconfigured
        return "dev-bible-token-fallback"
    return tok


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_bearer()}",
        "Content-Type": "application/json",
    }


def call(endpoint: str, body: dict[str, Any] | None = None, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Any:
    """Invoke a REST or MCP tool endpoint on openclaw-mcp-bible."""
    try:
        import requests  # type: ignore

    except ImportError as exc:
        raise BibleMCPError(f"`requests` not available: {exc}") from exc

    ep = endpoint.lstrip("/")
    url = f"{_base_url()}/{ep}"
    try:
        resp = requests.post(url, json=body or {}, headers=_headers(), timeout=timeout)
    except requests.RequestException:
        # Retry with localhost fallback if docker internal hostname fails
        fallback_url = f"http://127.0.0.1:8114/{ep}"
        try:
            resp = requests.post(fallback_url, json=body or {}, headers=_headers(), timeout=timeout)
        except requests.RequestException as exc:
            raise BibleMCPError(f"MCP Bible unreachable at {url} / {fallback_url}: {exc}", status=0) from exc

    if resp.status_code == 401:
        raise BibleMCPError("MCP Bible rejected bearer token", status=401)
    if resp.status_code >= 400:
        raise BibleMCPError(
            f"MCP Bible HTTP {resp.status_code} for {endpoint}: {resp.text[:300]}",
            status=resp.status_code,
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise BibleMCPError(f"Bad JSON from MCP Bible: {exc}", status=resp.status_code) from exc
