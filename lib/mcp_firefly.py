"""Thin HTTP client for ``openclaw-mcp-firefly``."""

from __future__ import annotations

import logging
import os
import sys
import pathlib
from typing import Any

try:
    import requests  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]
except ImportError:
    requests = None

_LIB_DIR = str(pathlib.Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
from token_resolver import resolve_secret

log = logging.getLogger("openclaw.mcp_firefly")

DEFAULT_URL = "http://openclaw-mcp-firefly:8115"
DEFAULT_TIMEOUT_SEC = 30


class FireflyMCPError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _base_url() -> str:
    return os.environ.get("FIREFLY_MCP_URL", os.environ.get("MCP_FIREFLY_URL", DEFAULT_URL)).rstrip("/")


def _bearer() -> str:
    # Try agent-specific dedicated token first
    agent = os.environ.get("OPENCLAW_AGENT_NAME", "roho").upper()
    tok = resolve_secret(f"MCP_TOKEN_FIREFLY_{agent}")
    if not tok:
        tok = resolve_secret(f"MCP_TOKEN_{agent}")
    if not tok:
        tok = resolve_secret("MCP_TOKEN_FIREFLY_ROHO")
    if not tok:
        tok = resolve_secret("MCP_TOKEN_ROHO")
    if not tok:
        raise FireflyMCPError("No bearer token found for MCP Firefly.")
    return tok


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_bearer()}",
        "Content-Type": "application/json",
    }


def call(
    endpoint: str,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    *,
    method: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> Any:
    """Invoke a REST API endpoint on openclaw-mcp-firefly."""
    if requests is None:
        raise FireflyMCPError("`requests` not available")

    ep = endpoint.lstrip("/")
    url = f"{_base_url()}/{ep}"

    if not method:
        ep_lower = endpoint.lower()
        if any(x in ep_lower for x in ["categorize", "reconcile", "test", "trigger", "webhook", "import", "upload"]):
            method = "POST"
        elif any(x in ep_lower for x in ["update", "put"]):
            method = "PUT"
        elif any(x in ep_lower for x in ["delete", "remove"]):
            method = "DELETE"
        elif any(x in ep_lower for x in ["create", "post"]) or (body and any(k in body for k in ["description", "amount", "source_id", "destination_id", "transactions"])):
            method = "POST"
        else:
            method = "GET"

    method = method.upper()

    try:
        if method == "GET":
            query_params = params or body
            resp = requests.get(url, params=query_params, headers=_headers(), timeout=timeout)
        elif method == "POST":
            resp = requests.post(url, json=body or {}, params=params, headers=_headers(), timeout=timeout)
        elif method == "PUT":
            resp = requests.put(url, json=body or {}, params=params, headers=_headers(), timeout=timeout)
        elif method == "DELETE":
            resp = requests.delete(url, params=params or body, headers=_headers(), timeout=timeout)
        else:
            raise FireflyMCPError(f"Unsupported HTTP method: {method}")
    except requests.RequestException as exc:
        raise FireflyMCPError(f"MCP Firefly unreachable at {url}: {exc}", status=0) from exc

    if resp.status_code == 401:
        raise FireflyMCPError("MCP Firefly rejected bearer token", status=401)
    if resp.status_code >= 400:
        raise FireflyMCPError(
            f"MCP Firefly HTTP {resp.status_code} for {endpoint}: {resp.text[:300]}",
            status=resp.status_code,
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise FireflyMCPError(f"Bad JSON from MCP Firefly: {exc}", status=resp.status_code) from exc
