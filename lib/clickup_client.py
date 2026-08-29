"""Single authoritative ClickUp REST API v2 Client for OpenClaw (SPEC-CUOR-002).

Consolidates all ClickUp HTTP requests across the ecosystem into a single client
supporting both async (FastMCP microservice) and sync (CLI shim) call patterns with
exponential backoff on 429 (rate limits) and 5xx errors.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import random
from typing import Any

import httpx

log = logging.getLogger("mcp-orch.clickup")

BASE_URL = "https://api.clickup.com/api/v2"

MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 2.0
MAX_JITTER_SECONDS = 1.0


class ClickUpError(Exception):
    """ClickUp API Exception with optional status code."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def get_api_key() -> str:
    """Get ClickUp API key from environment."""
    key = (
        os.environ.get("CLICKUP_API_KEY")
        or os.environ.get("CLICKUP_API_TOKEN")
        or os.environ.get("CLICKUP_TOKEN")
        or ""
    ).strip()
    if not key:
        raise ClickUpError("CLICKUP_API_KEY env var not set", status=None)
    return key


def get_headers(*, json_body: bool = True) -> dict[str, str]:
    """Build request headers with auth."""
    h = {"Authorization": get_api_key()}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _run_sync(coro: Any) -> Any:
    """Execute coroutine synchronously."""
    if not inspect.isawaitable(coro):
        return coro
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import nest_asyncio  # type: ignore # pyright: ignore[reportMissingImports]

        nest_asyncio.apply()
        return loop.run_until_complete(coro)
    return asyncio.run(coro)  # type: ignore


async def _ensure_async(res_or_coro: Any) -> Any:
    """Ensure result is awaited if it is a coroutine or awaitable."""
    if inspect.isawaitable(res_or_coro):
        return await res_or_coro
    return res_or_coro


async def _async_request_json_impl(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Internal HTTP request implementation."""
    url = f"{BASE_URL}{path}"
    payload = json_body if json_body is not None else body
    q_params = query if query is not None else params
    headers = get_headers(json_body=payload is not None or method in ("POST", "PUT"))

    for attempt in range(MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=payload,
                    params=q_params,
                )

                if resp.status_code == 429:
                    if attempt >= MAX_RETRIES:
                        raise ClickUpError(
                            f"ClickUp rate limit exceeded after {MAX_RETRIES} retries on {path}",
                            status=429,
                        )
                    backoff = BASE_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, MAX_JITTER_SECONDS)
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            backoff = max(backoff, float(retry_after))
                        except ValueError:
                            pass
                    log.warning("ClickUp 429 on %s, retry %d/%d in %.1fs", path, attempt + 1, MAX_RETRIES, backoff)
                    await asyncio.sleep(backoff)
                    continue

                if resp.status_code >= 500:
                    if attempt >= MAX_RETRIES:
                        raise ClickUpError(f"ClickUp 5xx server error on {path}: {resp.text}", status=resp.status_code)
                    backoff = 2.0 + random.uniform(0, 0.5)
                    log.warning("ClickUp %d on %s, retry in %.1fs", resp.status_code, path, backoff)
                    await asyncio.sleep(backoff)
                    continue

                if resp.status_code >= 400:
                    err_msg = resp.text
                    try:
                        parsed = resp.json()
                        err_msg = parsed.get("err", err_msg)
                    except Exception:
                        pass
                    raise ClickUpError(str(err_msg), status=resp.status_code)

                if not resp.text:
                    return {}
                return resp.json()
        except httpx.RequestError as e:
            if attempt >= MAX_RETRIES:
                raise ClickUpError(f"Network error on {path}: {e}", status=None) from e
            await asyncio.sleep(1.0)
    return {}


def request_json(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    payload = json_body if json_body is not None else body
    q_params = query if query is not None else params
    coro = _async_request_json_impl(method, path, body=payload, json_body=payload, query=q_params, params=q_params)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def comment_text(c: dict[str, Any]) -> str:
    """Extract plain text from a ClickUp comment object."""
    if not c:
        return ""
    t = c.get("comment_text")
    if isinstance(t, str):
        return t
    if isinstance(t, list) and t:
        parts = []
        for block in t:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
        return "\n".join(parts)
    return str(c.get("text") or "")


# ── ClickUp Primitives (Dual Async / Sync) ───────────────────────────

def get_team_id(team_id: str | None = None, team_name: str | None = None) -> Any:
    async def _impl():
        if team_id and team_id.strip():
            return team_id.strip()
        env_team = os.environ.get("CLICKUP_TEAM_ID", "").strip()
        if env_team:
            return env_team
        res = await _ensure_async(request_json("GET", "/team"))
        teams = (res.get("teams") or []) if isinstance(res, dict) else []
        if not teams:
            raise ClickUpError("No ClickUp workspaces returned from /team")
        if team_name:
            clean_name = team_name.lower().strip()
            for t in teams:
                if t.get("name", "").lower().strip() == clean_name:
                    return str(t["id"])
        return str(teams[0]["id"])

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def get_workspaces() -> Any:
    """Fetch all available ClickUp workspaces/teams."""
    async def _impl():
        res = await _ensure_async(request_json("GET", "/team"))
        teams = (res.get("teams") or []) if isinstance(res, dict) else []
        out = []
        for t in teams:
            out.append({
                "id": str(t.get("id")),
                "name": t.get("name"),
                "members": len(t.get("members") or []),
            })
        return out

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)



def get_task(
    task_id: str,
    *,
    include_markdown_description: bool = True,
    include_subtasks: bool = False,
) -> Any:
    async def _impl():
        q: dict[str, str] = {}
        if include_markdown_description:
            q["include_markdown_description"] = "true"
        if include_subtasks:
            q["include_subtasks"] = "true"
        res = await _ensure_async(request_json("GET", f"/task/{task_id}", query=q or None))
        return res if isinstance(res, dict) else {}

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def create_task(
    list_id: str,
    name: str,
    description: str = "",
    priority: int | None = None,
    assignees: list[int | str] | None = None,
    custom_fields: list[dict[str, Any]] | None = None,
    status: str | None = None,
) -> Any:
    async def _impl():
        b: dict[str, Any] = {
            "name": name,
            "markdown_description": description,
        }
        if priority is not None:
            b["priority"] = priority
        if assignees:
            b["assignees"] = [int(a) if str(a).isdigit() else a for a in assignees]
        if custom_fields:
            b["custom_fields"] = custom_fields
        if status:
            b["status"] = status

        res = await _ensure_async(request_json("POST", f"/list/{list_id}/task", body=b))
        return res if isinstance(res, dict) else {}

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def update_task(
    task_id: str,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    priority: int | None = None,
) -> Any:
    async def _impl():
        b: dict[str, Any] = {}
        if name is not None:
            b["name"] = name
        if description is not None:
            b["markdown_description"] = description
        if status is not None:
            b["status"] = status
        if priority is not None:
            b["priority"] = priority

        res = await _ensure_async(request_json("PUT", f"/task/{task_id}", body=b))
        return res if isinstance(res, dict) else {}

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def update_task_assignees(
    task_id: str,
    add_assignees: list[int | str] | None = None,
    rem_assignees: list[int | str] | None = None,
) -> Any:
    async def _impl():
        b: dict[str, Any] = {"assignees": {}}
        if add_assignees:
            b["assignees"]["add"] = [int(a) if str(a).isdigit() else a for a in add_assignees]
        if rem_assignees:
            b["assignees"]["rem"] = [int(a) if str(a).isdigit() else a for a in rem_assignees]
        res = await _ensure_async(request_json("PUT", f"/task/{task_id}", body=b))
        return res if isinstance(res, dict) else {}

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def set_task_custom_field(task_id: str, field_id: str, value: Any) -> Any:
    async def _impl():
        res = await _ensure_async(request_json("POST", f"/task/{task_id}/field/{field_id}", body={"value": value}))
        return res if isinstance(res, dict) else {}

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def add_comment(task_id: str, comment_text: str) -> Any:
    async def _impl():
        res = await _ensure_async(request_json("POST", f"/task/{task_id}/comment", body={"comment_text": comment_text}))
        return res if isinstance(res, dict) else {}

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def get_task_comments(task_id: str) -> Any:
    async def _impl():
        res = await _ensure_async(request_json("GET", f"/task/{task_id}/comment"))
        return list(res.get("comments") or []) if isinstance(res, dict) else []

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def list_tasks_paginated(
    list_id: str,
    *,
    include_closed: bool = False,
    extra_query: dict[str, str] | None = None,
) -> Any:
    async def _impl():
        out: list[dict[str, Any]] = []
        page = 0
        while True:
            q: dict[str, str] = {"page": str(page)}
            if include_closed:
                q["include_closed"] = "true"
            if extra_query:
                q.update(extra_query)
            res = await _ensure_async(request_json("GET", f"/list/{list_id}/task", query=q))
            batch = res.get("tasks") if isinstance(res, dict) else []
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def list_team_tasks_paginated(
    team_id: str,
    *,
    include_closed: bool = False,
    extra_query: dict[str, str] | None = None,
) -> Any:
    async def _impl():
        out: list[dict[str, Any]] = []
        page = 0
        while page < 50:
            q: dict[str, str] = {"page": str(page)}
            if include_closed:
                q["include_closed"] = "true"
            if extra_query:
                q.update(extra_query)
            res = await _ensure_async(request_json("GET", f"/team/{team_id}/task", query=q))
            batch = res.get("tasks") if isinstance(res, dict) else []
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def get_list_fields(list_id: str) -> Any:
    async def _impl():
        res = await _ensure_async(request_json("GET", f"/list/{list_id}/field"))
        return list(res.get("fields") or []) if isinstance(res, dict) else []

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def get_space_lists(space_id: str) -> Any:
    async def _impl():
        res = await _ensure_async(request_json("GET", f"/space/{space_id}/list"))
        return list(res.get("lists") or []) if isinstance(res, dict) else []

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def get_folder_lists(folder_id: str) -> Any:
    async def _impl():
        res = await _ensure_async(request_json("GET", f"/folder/{folder_id}/list"))
        return list(res.get("lists") or []) if isinstance(res, dict) else []

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def get_space_folders(space_id: str) -> Any:
    async def _impl():
        res = await _ensure_async(request_json("GET", f"/space/{space_id}/folder"))
        return list(res.get("folders") or []) if isinstance(res, dict) else []

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def get_team_spaces(team_id: str) -> Any:
    async def _impl():
        res = await _ensure_async(request_json("GET", f"/team/{team_id}/space"))
        return list(res.get("spaces") or []) if isinstance(res, dict) else []

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def iter_list_ids_in_space(space_id: str) -> Any:
    async def _impl():
        ids: list[str] = []
        for lst in await _ensure_async(get_space_lists(space_id)):
            ids.append(str(lst["id"]))
        for folder in await _ensure_async(get_space_folders(space_id)):
            for lst in await _ensure_async(get_folder_lists(folder["id"])):
                ids.append(str(lst["id"]))
        return ids

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def get_webhooks(team_id: str) -> Any:
    async def _impl():
        res = await _ensure_async(request_json("GET", f"/team/{team_id}/webhook"))
        return res if isinstance(res, dict) else {}

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def create_webhook(team_id: str, body: dict[str, Any]) -> Any:
    async def _impl():
        res = await _ensure_async(request_json("POST", f"/team/{team_id}/webhook", body=body))
        return res if isinstance(res, dict) else {}

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def delete_webhook(webhook_id: str) -> Any:
    async def _impl():
        res = await _ensure_async(request_json("DELETE", f"/webhook/{webhook_id}"))
        return res if isinstance(res, dict) else {}

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def upload_task_attachment(task_id: str, file_path: str) -> Any:
    async def _impl():
        url = f"{BASE_URL}/task/{task_id}/attachment"
        headers = {"Authorization": get_api_key()}
        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(file_path, "rb") as f:
                files = {"attachment": (os.path.basename(file_path), f)}
                resp = await client.post(url, headers=headers, files=files)
                if resp.status_code >= 400:
                    raise ClickUpError(resp.text, status=resp.status_code)
                return resp.json()

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def delete_task_custom_field(task_id: str, field_id: str) -> Any:
    async def _impl():
        res = await _ensure_async(request_json("DELETE", f"/task/{task_id}/field/{field_id}"))
        return res if isinstance(res, dict) else {}

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def delete_task(task_id: str) -> Any:
    async def _impl():
        res = await _ensure_async(request_json("DELETE", f"/task/{task_id}"))
        return res if isinstance(res, dict) else {}

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)


def move_task(task_id: str, list_id: str) -> Any:
    async def _impl():
        res = await _ensure_async(request_json("POST", f"/task/{task_id}/location", body={"list_id": list_id}))
        return res if isinstance(res, dict) else {}

    coro = _impl()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return coro
    return _run_sync(coro)

