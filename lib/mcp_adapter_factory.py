"""Unified MCP Adapter Factory for OpenClaw (SPEC-LANGCHAIN-001).

Wraps MCP JSON-RPC protocol service invocations (e.g. m365, google, orchestrator, productivity)
using langchain-mcp-adapters when available, integrating seamlessly with OpenClaw's token_resolver
and governed environment structure.
"""

from __future__ import annotations

import logging
import os
import sys
import pathlib
from typing import Any, Dict, Optional

_LIB_DIR = str(pathlib.Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from token_resolver import resolve_secret

log = logging.getLogger("openclaw.mcp_adapter_factory")

# Known MCP JSON-RPC services and their default settings
MCP_SERVICES = {
    "google": {
        "default_url": "http://openclaw-mcp-google:8103",
        "url_env": "MCP_GOOGLE_URL",
        "token_envs": ["MCP_TOKEN_GOOGLE_ROHO", "MCP_TOKEN_ROHO"],
        "endpoint": "/mcp",
    },
    "m365": {
        "default_url": "http://openclaw-mcp-m365:8101",
        "url_env": "MCP_M365_URL",
        "token_envs": ["MCP_TOKEN_M365_ROHO", "MCP_TOKEN_M365", "MCP_TOKEN_ROHO"],
        "endpoint": "/mcp",
    },
    "orchestrator": {
        "default_url": "http://openclaw-mcp-orchestrator:8109",
        "url_env": "MCP_ORCHESTRATOR_URL",
        "token_envs": ["MCP_TOKEN_ORCH_ROHO", "MCP_TOKEN_ORCH", "MCP_TOKEN_ROHO"],
        "endpoint": "/mcp",
    },
    "productivity": {
        "default_url": "http://openclaw-mcp-productivity:8104",
        "url_env": "MCP_PRODUCTIVITY_URL",
        "token_envs": ["MCP_TOKEN_PROD_ROHO", "MCP_PRODUCTIVITY_TOKEN", "MCP_TOKEN_ROHO"],
        "endpoint": "/mcp",
    },
}


def resolve_service_config(service_name: str) -> Dict[str, str]:
    """Resolve base URL and bearer token for an MCP service."""
    srv = MCP_SERVICES.get(service_name.lower())
    if not srv:
        raise ValueError(f"Unknown MCP service: {service_name}")

    base_url = os.environ.get(srv["url_env"], srv["default_url"]).rstrip("/")
    
    token = None
    agent = os.environ.get("OPENCLAW_AGENT_NAME", "roho").upper()
    for env_var in [f"MCP_TOKEN_{service_name.upper()}_{agent}"] + srv["token_envs"]:
        token = resolve_secret(env_var)
        if token:
            break

    return {
        "base_url": base_url,
        "mcp_url": f"{base_url}{srv['endpoint']}",
        "token": token or "",
    }


def is_langchain_adapter_available() -> bool:
    """Check if langchain_mcp_adapters package is installed."""
    try:
        import langchain_mcp_adapters  # type: ignore # noqa: F401
        return True
    except ImportError:
        return False


def call_mcp_tool(service_name: str, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
    """Unified entrypoint to invoke an MCP tool on an openclaw MCP service.
    
    Uses langchain-mcp-adapters when installed, or falls back to standard HTTP JSON-RPC dispatch.
    """
    srv_name = service_name.lower()
    if srv_name not in MCP_SERVICES:
        # Dispatch to bespoke REST client module if available
        module_name = f"mcp_{srv_name}"
        mod = __import__(module_name)
        return mod.call(tool_name, arguments or {})

    cfg = resolve_service_config(srv_name)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if cfg["token"]:
        headers["Authorization"] = f"Bearer {cfg['token']}"

    if is_langchain_adapter_available():
        try:
            import asyncio
            from langchain_mcp_adapters.client import MultiServerMCPClient

            async def _run_adapter() -> Any:
                async with MultiServerMCPClient({
                    srv_name: {
                        "transport": "streamable_http",
                        "url": cfg["mcp_url"],
                        "headers": headers,
                    }
                }) as client:
                    tools = await client.get_tools()
                    target_tool = next((t for t in tools if t.name == tool_name), None)
                    if target_tool:
                        res = await target_tool.ainvoke(arguments or {})
                        return res
                return None

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                # Avoid nested loop issues in async contexts
                adapter_res = asyncio.run_coroutine_threadsafe(_run_adapter(), loop).result(timeout=30.0)
            else:
                adapter_res = asyncio.run(_run_adapter())

            if adapter_res is not None:
                return adapter_res
        except Exception as exc:
            log.warning("langchain-mcp-adapters invocation failed for %s:%s (%s), falling back to JSON-RPC HTTP", srv_name, tool_name, exc)

    # Standard JSON-RPC POST fallback
    import requests
    import uuid

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments or {},
        },
    }

    resp = requests.post(cfg["mcp_url"], json=payload, headers=headers, timeout=30.0)
    resp.raise_for_status()
    
    data = resp.json()
    if "error" in data:
        err = data["error"] or {}
        raise RuntimeError(f"MCP {srv_name} JSON-RPC error: {err.get('message') or err}")
        
    result = data.get("result") or {}
    content = result.get("content") or []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            return item.get("text")
    return result
