"""Thin HTTP client for ``openclaw-mcp-google`` (SPEC-GAUTH-001 revised, #323/#324).

The gateway and every Roho skill MUST go through this module for Google Workspace
operations. No process other than the MCP Google container is allowed to hold a
Google OAuth refresh token; this client speaks MCP JSON-RPC 2.0 over Streamable HTTP
(or SSE, depending on the negotiated transport).

Why a hand-rolled client and not the MCP Python SDK?
- Skills run as short-lived subprocesses; importing the full ``mcp`` package per call
  is ~500ms of overhead on cold start. ``requests`` is already in the gateway image.
- We need synchronous calls from sync skill code. The SDK exposes async-only clients.

Env contract (set in docker-compose for openclaw + openclaw-amara):
- ``MCP_GOOGLE_URL`` — default ``http://openclaw-mcp-google:8103``
- ``MCP_TOKEN_GOOGLE_ROHO`` — agent bearer token; same token that the MCP Google
  ``auth.init_tokens`` loads under that env name.

Public surface:
- :class:`GoogleMCPError`
- :func:`call(tool, arguments)` — invoke any MCP tool exposed by openclaw-mcp-google.
- :func:`health()` — GET /health, dict.
- :func:`admin(endpoint, method='GET', json=None)` — GET/POST /admin/* helpers.
"""

from __future__ import annotations

import json as _json
import logging
import os
import uuid
from typing import Any

log = logging.getLogger("openclaw.mcp_google")

DEFAULT_URL = "http://openclaw-mcp-google:8103"
DEFAULT_TIMEOUT_SEC = 30


class GoogleMCPError(RuntimeError):
    """Raised when an MCP Google call fails (HTTP, transport, or tool-level error).

    ``status`` is the HTTP status code (or 0 for transport errors); ``code`` is the
    tool error code when MCP Google returned a JSON ``{"error": {"code": …}}`` body.
    """

    def __init__(self, message: str, *, status: int = 0, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _base_url() -> str:
    return os.environ.get("MCP_GOOGLE_URL", DEFAULT_URL).rstrip("/")


def _bearer() -> str:
    tok = os.environ.get("MCP_TOKEN_GOOGLE_ROHO", "").strip()
    if not tok:
        raise GoogleMCPError(
            "MCP_TOKEN_GOOGLE_ROHO is not set — cannot call openclaw-mcp-google. "
            "Doppler should inject this in dev/prd/dev_personal."
        )
    return tok


def _headers(*, accept_sse: bool = True) -> dict[str, str]:
    accept = "application/json, text/event-stream" if accept_sse else "application/json"
    return {
        "Authorization": f"Bearer {_bearer()}",
        "Content-Type": "application/json",
        "Accept": accept,
    }


def _parse_sse_payload(text: str) -> dict[str, Any]:
    """Parse Streamable-HTTP SSE response — concatenate ``data: …`` lines and JSON-decode."""
    chunks: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            chunks.append(line[len("data:"):].lstrip())
    payload = "".join(chunks).strip()
    if not payload:
        raise GoogleMCPError("Empty SSE payload from MCP Google", status=200)
    try:
        return _json.loads(payload)
    except _json.JSONDecodeError as exc:
        raise GoogleMCPError(f"Bad SSE JSON from MCP Google: {exc}", status=200) from exc


def _decode_response(resp: Any) -> dict[str, Any]:
    """Return parsed JSON body whether the server sent JSON or text/event-stream."""
    ctype = (resp.headers.get("content-type") or "").lower()
    if "text/event-stream" in ctype:
        return _parse_sse_payload(resp.text)
    try:
        return resp.json()
    except ValueError as exc:
        raise GoogleMCPError(f"Bad JSON from MCP Google: {exc}", status=resp.status_code) from exc


def _extract_tool_result(rpc: dict[str, Any]) -> Any:
    """Pull the tool output out of a JSON-RPC ``tools/call`` response.

    MCP servers return ``{"result": {"content": [{"type": "text", "text": <json>}], ...}}``
    or sometimes ``{"result": {"structuredContent": <obj>}}``. We tolerate both.
    """
    if "error" in rpc:
        err = rpc["error"] or {}
        raise GoogleMCPError(
            f"MCP Google JSON-RPC error: {err.get('message') or err}",
            code=str(err.get("code") or ""),
        )
    result = rpc.get("result") or {}
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content") or []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            text = item["text"]
            try:
                return _json.loads(text)
            except _json.JSONDecodeError:
                return text
    return result


def call(tool: str, arguments: dict[str, Any] | None = None, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Any:
    """Invoke a tool on openclaw-mcp-google over MCP JSON-RPC 2.0.

    Returns the decoded tool payload (typically a dict). Raises :class:`GoogleMCPError`
    on transport / auth / tool-level failures so callers can surface them in #alerts.
    """
    try:
        import requests
    except ImportError as exc:
        raise GoogleMCPError(f"`requests` not available: {exc}") from exc

    url = f"{_base_url()}/mcp"
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }
    try:
        resp = requests.post(url, json=body, headers=_headers(accept_sse=True), timeout=timeout)
    except requests.RequestException as exc:
        raise GoogleMCPError(f"MCP Google unreachable: {exc}", status=0) from exc

    if resp.status_code == 401:
        raise GoogleMCPError(
            "MCP Google rejected bearer token — verify MCP_TOKEN_GOOGLE_ROHO matches the Doppler value.",
            status=401,
        )
    if resp.status_code >= 400:
        raise GoogleMCPError(
            f"MCP Google HTTP {resp.status_code} for tool={tool}: {resp.text[:300]}",
            status=resp.status_code,
        )
    parsed = _decode_response(resp)
    return _extract_tool_result(parsed)


def health(*, timeout: float = 5.0) -> dict[str, Any]:
    """Hit ``GET /health`` on MCP Google (no auth required)."""
    try:
        import requests
    except ImportError as exc:
        raise GoogleMCPError(f"`requests` not available: {exc}") from exc
    try:
        resp = requests.get(f"{_base_url()}/health", timeout=timeout)
    except requests.RequestException as exc:
        raise GoogleMCPError(f"MCP Google /health unreachable: {exc}", status=0) from exc
    if resp.status_code != 200:
        raise GoogleMCPError(f"MCP Google /health HTTP {resp.status_code}", status=resp.status_code)
    try:
        return resp.json()
    except ValueError as exc:
        raise GoogleMCPError(f"MCP Google /health bad JSON: {exc}", status=200) from exc


def admin(endpoint: str, *, method: str = "GET", json: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
    """Call ``/admin/*`` endpoints. Bearer-token-authenticated.

    Endpoints (see services/openclaw-mcp-google/server.py):
    - ``GET /admin/token-status``
    - ``GET /admin/reauth-status``
    - ``POST /admin/refresh``
    """
    try:
        import requests
    except ImportError as exc:
        raise GoogleMCPError(f"`requests` not available: {exc}") from exc
    url = f"{_base_url()}/admin/{endpoint.lstrip('/')}"
    method_upper = method.upper()
    try:
        if method_upper == "GET":
            resp = requests.get(url, headers=_headers(accept_sse=False), timeout=timeout)
        elif method_upper == "POST":
            resp = requests.post(url, json=json or {}, headers=_headers(accept_sse=False), timeout=timeout)
        else:
            raise GoogleMCPError(f"unsupported admin method: {method}")
    except requests.RequestException as exc:
        raise GoogleMCPError(f"MCP Google /admin/{endpoint} unreachable: {exc}", status=0) from exc

    if resp.status_code in (200, 502):
        try:
            return resp.json()
        except ValueError as exc:
            raise GoogleMCPError(f"MCP Google /admin/{endpoint} bad JSON: {exc}", status=resp.status_code) from exc
    if resp.status_code == 401:
        raise GoogleMCPError("MCP Google /admin rejected bearer token", status=401)
    raise GoogleMCPError(
        f"MCP Google /admin/{endpoint} HTTP {resp.status_code}: {resp.text[:300]}",
        status=resp.status_code,
    )
