"""Thin HTTP client for ``openclaw-mcp-m365`` (SPEC-ARCH-001).

The gateway and skills go through this module for Microsoft 365 / Graph Online
operations. It speaks MCP JSON-RPC 2.0 over Streamable HTTP.

Public surface:
- :class:`M365MCPError`
- :func:`call(tool, arguments)` — invoke any MCP tool exposed by openclaw-mcp-m365.
- :func:`health()` — GET /health, dict.
"""

from __future__ import annotations

import json as _json
import logging
import os
import uuid
from typing import Any

log = logging.getLogger("openclaw.mcp_m365")

DEFAULT_URL = "http://openclaw-mcp-m365:8101"
DEFAULT_TIMEOUT_SEC = 30


class M365MCPError(RuntimeError):
    """Raised when an M365 MCP call fails (HTTP, transport, or tool-level error)."""

    def __init__(self, message: str, *, status: int = 0, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _base_url() -> str:
    return (os.environ.get("MCP_M365_URL") or os.environ.get("M365_MCP_URL") or DEFAULT_URL).rstrip("/")


def _bearer() -> str:
    # Try dedicated M365 tokens first, then Open Brain fallbacks
    for var in [
        "MCP_TOKEN_M365",
        "MCP_TOKEN_M365_ROHO",
        "MCP_TOKEN_M365_AMARA",
        "MCP_TOKEN_M365_ROB",
        "MCP_TOKEN_ROHO",
        "MCP_TOKEN_AMARA",
        "MCP_TOKEN_ROB",
    ]:
        tok = os.environ.get(var, "").strip()
        if tok:
            return tok

    raise M365MCPError(
        "No M365 token found in environment. Please set MCP_TOKEN_M365_* or fallback variables."
    )


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
        raise M365MCPError("Empty SSE payload from MCP M365", status=200)
    try:
        return _json.loads(payload)
    except _json.JSONDecodeError as exc:
        raise M365MCPError(f"Bad SSE JSON from MCP M365: {exc}", status=200) from exc


def _decode_response(resp: Any) -> dict[str, Any]:
    """Return parsed JSON body whether the server sent JSON or text/event-stream."""
    ctype = (resp.headers.get("content-type") or "").lower()
    if "text/event-stream" in ctype:
        return _parse_sse_payload(resp.text)
    try:
        return resp.json()
    except ValueError as exc:
        raise M365MCPError(f"Bad JSON from MCP M365: {exc}", status=resp.status_code) from exc


def _extract_tool_result(rpc: dict[str, Any]) -> Any:
    """Pull the tool output out of a JSON-RPC ``tools/call`` response."""
    if "error" in rpc:
        err = rpc["error"] or {}
        raise M365MCPError(
            f"MCP M365 JSON-RPC error: {err.get('message') or err}",
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


def _initialize(sse_resp: Any, lines_iter: Any, post_url: str, *, timeout: float) -> None:
    """Perform the MCP initialization handshake on an SSE session.

    The MCP protocol requires:
    1. ``initialize`` request → server responds with capabilities
    2. ``notifications/initialized`` notification (no response expected)

    Raises ``M365MCPError`` if initialization fails.
    """
    import requests  # type: ignore

    init_id = str(uuid.uuid4())
    init_body = {
        "jsonrpc": "2.0",
        "id": init_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "roho-mcp-m365-client", "version": "1.0"},
        },
    }
    post_headers = _headers(accept_sse=False)

    # Step 1: POST initialize request
    try:
        init_resp = requests.post(post_url, json=init_body, headers=post_headers, timeout=timeout)
    except requests.RequestException as exc:
        raise M365MCPError(f"Failed to POST initialize: {exc}", status=0) from exc

    if init_resp.status_code >= 400:
        raise M365MCPError(
            f"MCP M365 initialize HTTP {init_resp.status_code}: {init_resp.text[:300]}",
            status=init_resp.status_code,
        )

    # Step 2: Wait for initialize response on the SSE stream
    initialized = False
    for line in lines_iter:
        if not line:
            continue
        if line.startswith("data:"):
            data_str = line[len("data:"):].strip()
            try:
                payload = _json.loads(data_str)
            except _json.JSONDecodeError:
                continue

            if isinstance(payload, dict) and payload.get("id") == init_id:
                if "error" in payload:
                    err = payload["error"] or {}
                    raise M365MCPError(
                        f"MCP M365 initialize rejected: {err.get('message') or err}",
                        code=str(err.get("code") or ""),
                    )
                # Initialize succeeded
                initialized = True
                log.debug("MCP M365 initialize handshake OK")
                break

    if not initialized:
        raise M365MCPError("SSE stream closed before initialize response")

    # Step 3: POST notifications/initialized (no-id notification)
    notif_body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    try:
        notif_resp = requests.post(post_url, json=notif_body, headers=post_headers, timeout=timeout)
    except requests.RequestException as exc:
        raise M365MCPError(f"Failed to POST initialized notification: {exc}", status=0) from exc

    if notif_resp.status_code >= 400:
        raise M365MCPError(
            f"MCP M365 initialized notification HTTP {notif_resp.status_code}: {notif_resp.text[:300]}",
            status=notif_resp.status_code,
        )
    log.debug("MCP M365 initialized notification sent")


def _wait_for_response(lines_iter: Any, req_id: str, *, timeout: float) -> dict[str, Any]:
    """Read SSE stream lines until we find a JSON-RPC response matching *req_id*."""
    for line in lines_iter:
        if not line:
            continue
        if line.startswith("data:"):
            data_str = line[len("data:"):].strip()
            try:
                payload = _json.loads(data_str)
            except _json.JSONDecodeError:
                continue

            if isinstance(payload, dict) and payload.get("id") == req_id:
                return payload

    raise M365MCPError(f"SSE stream closed before receiving response for request {req_id}")


def call(tool: str, arguments: dict[str, Any] | None = None, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Any:
    """Invoke a tool on openclaw-mcp-m365 over FastMCP StreamableHTTP (POST /mcp)."""
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise M365MCPError(f"`requests` not available: {exc}") from exc

    req_id = str(uuid.uuid4())
    body = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }

    base_url = _base_url()
    mcp_url = f"{base_url}/mcp"
    headers = _headers(accept_sse=False)

    resp = None
    urls_to_try = [mcp_url, "http://127.0.0.1:8111/mcp", "http://localhost:8111/mcp", "http://127.0.0.1:8101/mcp"]
    primary_exc = None
    last_exc = None

    for idx, u in enumerate(urls_to_try):
        try:
            resp = requests.post(u, json=body, headers=headers, timeout=timeout)
            break
        except requests.RequestException as exc:
            if idx == 0:
                primary_exc = exc
            last_exc = exc
            continue

    if resp is None:
        err = primary_exc or last_exc
        raise M365MCPError(f"MCP tool call {tool} failed at {mcp_url}: {err}", status=0)

    if resp.status_code == 200:
        rpc = _decode_response(resp)
        return _extract_tool_result(rpc)
    elif resp.status_code == 401:
        raise M365MCPError("MCP M365 rejected bearer token — verify token value.", status=401)
    elif resp.status_code >= 400:
        raise M365MCPError(
            f"MCP M365 POST HTTP {resp.status_code} for tool={tool}: {resp.text[:300]}",
            status=resp.status_code,
        )


def health(*, timeout: float = 5.0) -> dict[str, Any]:
    """Hit ``GET /health`` on MCP M365 (no auth required)."""
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise M365MCPError(f"`requests` not available: {exc}") from exc

    urls_to_try = [f"{_base_url()}/health", "http://127.0.0.1:8111/health", "http://localhost:8111/health", "http://127.0.0.1:8101/health"]
    resp = None
    last_exc = None

    for u in urls_to_try:
        try:
            resp = requests.get(u, timeout=timeout)
            break
        except requests.RequestException as exc:
            last_exc = exc
            continue

    if resp is None:
        raise M365MCPError(f"MCP M365 /health unreachable: {last_exc}", status=0)
    if resp.status_code != 200:
        raise M365MCPError(f"MCP M365 /health HTTP {resp.status_code}", status=resp.status_code)
    try:
        return resp.json()
    except ValueError as exc:
        raise M365MCPError(f"MCP M365 /health bad JSON: {exc}", status=200) from exc


def request(method: str, path: str, payload: dict | None = None, params: dict | None = None, use_app_token: bool = False, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Any:
    """Proxy general HTTP requests to Microsoft Graph API via M365 MCP server."""
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise M365MCPError(f"`requests` not available: {exc}") from exc

    body = {
        "method": method,
        "path": path,
        "payload": payload,
        "params": params,
        "use_app_token": use_app_token,
    }

    base_url = _base_url()
    urls_to_try = [
        f"{base_url}/api/m365/request",
        "http://127.0.0.1:8111/api/m365/request",
        "http://localhost:8111/api/m365/request",
        "http://127.0.0.1:8101/api/m365/request",
    ]
    resp = None
    last_exc = None

    for u in urls_to_try:
        try:
            resp = requests.post(u, json=body, headers=_headers(accept_sse=False), timeout=timeout)
            break
        except requests.RequestException as exc:
            last_exc = exc
            continue

    if resp is None:
        raise M365MCPError(f"M365 MCP proxy unreachable at {base_url}/api/m365/request: {last_exc}", status=0)

    if resp.status_code == 401:
        raise M365MCPError("M365 MCP proxy rejected bearer token", status=401)
    if resp.status_code >= 400:
        raise M365MCPError(
            f"M365 MCP proxy HTTP {resp.status_code} for {path}: {resp.text[:300]}",
            status=resp.status_code,
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise M365MCPError(f"Bad JSON from M365 MCP proxy: {exc}", status=resp.status_code) from exc

