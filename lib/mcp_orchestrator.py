"""Thin HTTP client for ``openclaw-mcp-orchestrator`` (SPEC-CUOR-002)."""

from __future__ import annotations

import json as _json
import logging
import os
import pathlib
import sys
import uuid
from typing import Any

_LIB_DIR = str(pathlib.Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
from token_resolver import resolve_secret  # noqa: E402

log = logging.getLogger("openclaw.mcp_orchestrator")

DEFAULT_URL = "http://openclaw-mcp-orchestrator:8109"
DEFAULT_TIMEOUT_SEC = 30


class OrchestratorMCPError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _base_url() -> str:
    return os.environ.get("MCP_ORCHESTRATOR_URL", DEFAULT_URL).rstrip("/")


def _bearer() -> str:
    agent = os.environ.get("OPENCLAW_AGENT_NAME", "roho").upper()
    tok = resolve_secret(f"MCP_TOKEN_ORCH_{agent}")
    if not tok:
        tok = resolve_secret(f"MCP_TOKEN_{agent}")
    if not tok:
        tok = resolve_secret("MCP_TOKEN_ORCH_ROHO")
    if not tok:
        tok = resolve_secret("MCP_TOKEN_ROHO")
    if not tok:
        tok = resolve_secret("MCP_TOKEN")
    if not tok:
        # Fallback to dev/test token
        return "roho-orch-token"
    return tok


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_bearer()}",
        "Content-Type": "application/json",
    }


def call(tool: str, arguments: dict[str, Any] | None = None, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Any:
    """Invoke a tool on openclaw-mcp-orchestrator over MCP JSON-RPC 2.0 or direct HTTP."""
    try:
        import requests  # type: ignore # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise OrchestratorMCPError(f"`requests` not available: {exc}") from exc

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
        # Fallback direct FastAPI endpoint or mock local invocation if standalone
        raise OrchestratorMCPError(f"MCP Orchestrator unreachable at {url}: {exc}", status=0) from exc

    if resp.status_code == 401:
        raise OrchestratorMCPError("MCP Orchestrator rejected bearer token", status=401)
    if resp.status_code >= 400:
        raise OrchestratorMCPError(
            f"MCP Orchestrator HTTP {resp.status_code} for tool={tool}: {resp.text[:300]}",
            status=resp.status_code,
        )
    try:
        rpc = resp.json()
    except ValueError as exc:
        raise OrchestratorMCPError(f"Bad JSON from MCP Orchestrator: {exc}", status=resp.status_code) from exc

    if "error" in rpc:
        err = rpc["error"] or {}
        raise OrchestratorMCPError(f"MCP Orchestrator JSON-RPC error: {err.get('message') or err}")

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


def orch_peer_status(agent_name: str | None = None) -> dict[str, Any]:
    """Query peer container gateway health/liveness via openclaw-mcp-orchestrator:8109."""
    args = {}
    if agent_name:
        args["agent_name"] = agent_name
    return call("orch_peer_status", args)

