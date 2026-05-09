#!/usr/bin/env python3
"""
mattermost-bridge — OpenClaw skill for inter-agent coordination via Mattermost.

Wraps the Mattermost REST API v4. Every agent in the ecosystem uses this skill
to post messages, read channels, dispatch tasks, and reply in threads.

If Mattermost is unreachable, falls back to stderr logging (ERR_COMM_FALLBACK).

Environment variables:
    MATTERMOST_URL        Base URL of the Mattermost server (e.g. http://mattermost:8065)
    MATTERMOST_BOT_TOKEN  Bot access token for this agent
    MATTERMOST_TEAM_ID    Optional. When set, channel names resolve only in this team (fixes
                          wrong-channel posts when multiple teams share the same channel name).
    MATTERMOST_TEAM_NAME  Optional. Team handle (e.g. chimex-holdings) — resolved to an id if
                          MATTERMOST_TEAM_ID is unset (same goal as TEAM_ID).
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("mattermost-bridge")

MM_URL = os.environ.get("MATTERMOST_URL", "http://mattermost:8065")
MM_TOKEN = os.environ.get("MATTERMOST_BOT_TOKEN", "")
# Issue #195: multi-team servers — pin resolution so #agent-amara etc. map to the intended team
MM_TEAM_ID = os.environ.get("MATTERMOST_TEAM_ID", "").strip()
MM_TEAM_NAME = os.environ.get("MATTERMOST_TEAM_NAME", "").strip()

AGENT_NAME = os.environ.get("OPENCLAW_AGENT_NAME", "roho")

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 15
UPLOAD_READ_TIMEOUT = 60  # Extended timeout for file uploads (NFR-MMATT-001)

MAX_FILES_PER_POST = 5  # Mattermost server default limit
DEFAULT_MAX_FILE_SIZE_MB = 50
MAX_FILE_SIZE_BYTES = int(
    os.environ.get("MATTERMOST_MAX_FILE_SIZE_MB", DEFAULT_MAX_FILE_SIZE_MB)
) * 1024 * 1024


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {MM_TOKEN}",
        "Content-Type": "application/json",
    }


def _api(
    method: str,
    path: str,
    payload: Optional[Any] = None,
) -> dict[str, Any]:
    """Call Mattermost REST API. Returns parsed JSON or error dict.

    `payload` may be a dict (object) or a list (e.g. direct-channel creation).
    """
    url = f"{MM_URL}/api/v4{path}"
    try:
        resp = requests.request(
            method, url, headers=_headers(), json=payload,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        if resp.status_code >= 400:
            err: dict[str, Any] = {
                "error": f"HTTP {resp.status_code}",
                "detail": resp.text[:500],
            }
            if resp.status_code == 403:
                err["hint"] = (
                    "403 often means the bot is not a channel member (join the channel first), "
                    "or the channel is private and an admin must invite the bot. "
                    "Try: --action join --channel <name>, post/react with default auto-join, "
                    "or see docs/MATTERMOST-TROUBLESHOOTING.md."
                )
            return err
        return resp.json() if resp.text else {"status": "ok"}
    except requests.ConnectionError as exc:
        log.error("ERR_COMM_FALLBACK: Mattermost unreachable at %s — %s", MM_URL, exc)
        return {"error": "ERR_COMM_FALLBACK", "detail": str(exc)}
    except requests.Timeout:
        log.error("ERR_COMM_FALLBACK: Mattermost request timed out")
        return {"error": "ERR_COMM_FALLBACK", "detail": "timeout"}


_channel_cache: dict[str, str] = {}

# Mattermost channel IDs are 26-char lowercase alphanumeric (openclaw-docker#301).
_CHANNEL_ID_PATTERN = re.compile(r"^[a-z0-9]{26}$")


def _resolve_team_id_pref() -> Optional[str]:
    """Prefer MATTERMOST_TEAM_ID, else resolve MATTERMOST_TEAM_NAME via API."""
    if MM_TEAM_ID:
        return MM_TEAM_ID
    if MM_TEAM_NAME:
        team = _api("GET", f"/teams/name/{MM_TEAM_NAME}")
        if isinstance(team, dict) and team.get("id"):
            return team["id"]
    return None


def _resolve_channel(name: str) -> Optional[str]:
    """Resolve a channel name or channel ID to its ID. Caches results.

    When MATTERMOST_TEAM_ID or MATTERMOST_TEAM_NAME is set, only that team is used
    (avoids posting to a channel name that exists in another team — Issue #195).
    """
    if name in _channel_cache:
        return _channel_cache[name]

    if _CHANNEL_ID_PATTERN.match(name):
        ch = _api("GET", f"/channels/{name}")
        if isinstance(ch, dict) and ch.get("id") and not ch.get("error"):
            _channel_cache[name] = ch["id"]
            return ch["id"]
        return None

    pref_tid = _resolve_team_id_pref()
    if pref_tid:
        ch = _api("GET", f"/teams/{pref_tid}/channels/name/{name}")
        if isinstance(ch, dict) and ch.get("id"):
            _channel_cache[name] = ch["id"]
            return ch["id"]
        return None

    me = _api("GET", "/users/me")
    if "error" in me:
        return None

    teams = _api("GET", f"/users/{me['id']}/teams")
    if isinstance(teams, dict) and "error" in teams:
        return None

    team_list = teams if isinstance(teams, list) else []
    matches: list[tuple[str, str, str]] = []
    for team in team_list:
        ch = _api("GET", f"/teams/{team['id']}/channels/name/{name}")
        if isinstance(ch, dict) and ch.get("id"):
            label = team.get("display_name") or team.get("name") or team["id"]
            matches.append((label, team["id"], ch["id"]))

    if not matches:
        return None
    if len(matches) == 1:
        _channel_cache[name] = matches[0][2]
        return matches[0][2]

    matches.sort(key=lambda m: m[0].lower())
    log.warning(
        "channel %r exists in %d teams (e.g. %s). Posts may land in the wrong team. "
        "Set MATTERMOST_TEAM_ID or MATTERMOST_TEAM_NAME in Doppler for this agent.",
        name,
        len(matches),
        ", ".join(m[0] for m in matches[:5]),
    )
    _channel_cache[name] = matches[0][2]
    return matches[0][2]


def _join_self_to_channel(channel_id: str) -> dict[str, Any]:
    """Add the authenticated bot user to a channel (fixes many 403 post errors)."""
    me = _api("GET", "/users/me")
    if "error" in me:
        return me
    bot_id = me["id"]
    return _api("POST", f"/channels/{channel_id}/members", {"user_id": bot_id})


def _is_http_error(resp: dict[str, Any], code: int) -> bool:
    err = resp.get("error", "")
    return isinstance(err, str) and err == f"HTTP {code}"


# ── File upload helpers (SPEC-MMATT-001) ─────────────────────────────


def _upload_headers() -> dict[str, str]:
    """Auth header without Content-Type (let requests set multipart boundary)."""
    return {"Authorization": f"Bearer {MM_TOKEN}"}


def _validate_file_path(file_path: str) -> Optional[dict[str, Any]]:
    """Validate a file path for upload. Returns error dict or None if valid.

    Checks: exists, not empty, under size limit, no path traversal (NFR-MMATT-007).
    """
    p = Path(file_path)

    # Path traversal check — resolve and ensure no '..' components
    try:
        resolved = p.resolve()
        if ".." in str(p):
            return {"error": "INVALID_PATH", "detail": "Path traversal not allowed", "file": file_path}
    except (OSError, ValueError) as exc:
        return {"error": "INVALID_PATH", "detail": str(exc), "file": file_path}

    if not resolved.exists():
        return {"error": "FILE_NOT_FOUND", "detail": f"File does not exist: {file_path}", "file": file_path}

    if not resolved.is_file():
        return {"error": "INVALID_PATH", "detail": f"Not a regular file: {file_path}", "file": file_path}

    size = resolved.stat().st_size
    if size == 0:
        return {"error": "EMPTY_FILE", "detail": "0 bytes", "file": file_path}

    if size > MAX_FILE_SIZE_BYTES:
        size_mb = size / (1024 * 1024)
        limit_mb = MAX_FILE_SIZE_BYTES / (1024 * 1024)
        return {
            "error": "FILE_TOO_LARGE",
            "detail": f"{size_mb:.1f} MB exceeds {limit_mb:.0f} MB limit",
            "file": file_path,
        }

    return None  # valid


def _upload_file(channel_id: str, file_path: str) -> dict[str, Any]:
    """Upload a single file to Mattermost via POST /api/v4/files (REQ-MMATT-002).

    Returns:
        On success: {"file_id": str, "filename": str, "size": int, "mime_type": str}
        On error:   {"error": str, "detail": str, "file": str}
    """
    # Validate
    err = _validate_file_path(file_path)
    if err:
        log.warning("Skipping %s: %s", file_path, err.get("error"))
        return err

    resolved = Path(file_path).resolve()
    filename = resolved.name
    size = resolved.stat().st_size
    mime_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"

    log.info("Uploading %s (%d bytes) to channel %s...", filename, size, channel_id)

    url = f"{MM_URL}/api/v4/files?channel_id={channel_id}"
    try:
        with open(resolved, "rb") as f:
            resp = requests.post(
                url,
                headers=_upload_headers(),
                files={"files": (filename, f, mime_type)},
                timeout=(CONNECT_TIMEOUT, UPLOAD_READ_TIMEOUT),
            )

        if resp.status_code == 413:
            log.warning("Upload failed for %s: server rejected (413)", filename)
            return {
                "error": "SERVER_FILE_TOO_LARGE",
                "detail": "Mattermost server rejected: file exceeds server MaxFileSize",
                "file": file_path,
            }

        if resp.status_code == 403:
            log.warning("Upload failed for %s: 403 Forbidden", filename)
            return {
                "error": "HTTP 403",
                "detail": resp.text[:500],
                "file": file_path,
            }

        if resp.status_code >= 400:
            log.warning("Upload failed for %s: HTTP %d", filename, resp.status_code)
            return {
                "error": f"HTTP {resp.status_code}",
                "detail": resp.text[:500],
                "file": file_path,
            }

        data = resp.json()
        file_infos = data.get("file_infos", [])
        if not file_infos:
            return {"error": "NO_FILE_INFO", "detail": "Upload succeeded but no file_infos returned", "file": file_path}

        fi = file_infos[0]
        log.info("Uploaded %s → file_id=%s", filename, fi["id"])
        return {
            "file_id": fi["id"],
            "filename": fi.get("name", filename),
            "size": fi.get("size", size),
            "mime_type": fi.get("mime_type", mime_type),
        }
    except requests.ConnectionError as exc:
        log.error("ERR_COMM_FALLBACK: Mattermost unreachable during upload — %s", exc)
        return {"error": "ERR_COMM_FALLBACK", "detail": str(exc), "file": file_path}
    except requests.Timeout:
        log.warning("Upload failed for %s: timed out after %ds", filename, UPLOAD_READ_TIMEOUT)
        return {"error": "UPLOAD_TIMEOUT", "detail": f"File upload timed out after {UPLOAD_READ_TIMEOUT}s", "file": file_path}


def _upload_files(channel_id: str, file_paths: list[str]) -> dict[str, Any]:
    """Upload multiple files and return all file IDs (REQ-MMATT-013).

    Returns:
        On success:  {"file_ids": [...], "files": [...]}
        On partial:  {"file_ids": [...], "files": [...], "errors": [...]}
        On failure:  {"error": str, "detail": str}
    """
    file_ids: list[str] = []
    files_info: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for fp in file_paths:
        result = _upload_file(channel_id, fp)
        if "file_id" in result:
            file_ids.append(result["file_id"])
            files_info.append(result)
        else:
            errors.append({"file": fp, "error": result.get("error", "UNKNOWN"), "detail": result.get("detail", "")})

    if not file_ids:
        log.error("All %d file uploads failed — aborting", len(file_paths))
        return {"error": "ALL_UPLOADS_FAILED", "detail": f"All {len(file_paths)} file(s) failed to upload", "errors": errors}

    out: dict[str, Any] = {"file_ids": file_ids, "files": files_info}
    if errors:
        out["errors"] = errors
    return out


def _handle_file_uploads(
    channel_id: str,
    file_paths: list[str],
    auto_join: bool,
) -> Optional[dict[str, Any]]:
    """Upload files with auto-join retry on 403 (REQ-MMATT-012).

    Returns upload result dict, or None if no files to upload.
    Prints error JSON and calls sys.exit(1) on total failure.
    """
    if not file_paths:
        return None

    # Validate count early (ERR-MMATT-003)
    if len(file_paths) > MAX_FILES_PER_POST:
        print(json.dumps({
            "error": "TOO_MANY_FILES",
            "detail": f"Maximum {MAX_FILES_PER_POST} files per post. Got: {len(file_paths)}",
        }))
        sys.exit(1)

    upload_result = _upload_files(channel_id, file_paths)

    # If first upload got 403 and auto-join is on, join and retry all failed files
    if "error" in upload_result and auto_join:
        # Total failure — check if any were 403
        errs = upload_result.get("errors", [])
        if any(e.get("error") == "HTTP 403" for e in errs):
            log.info("403 on upload — joining channel %s and retrying", channel_id)
            join_res = _join_self_to_channel(channel_id)
            if "error" not in join_res:
                upload_result = _upload_files(channel_id, file_paths)
    elif upload_result.get("errors") and auto_join:
        # Partial failure — retry only the 403 failures
        retryable = [e["file"] for e in upload_result["errors"] if e.get("error") == "HTTP 403"]
        if retryable:
            log.info("403 on some uploads — joining channel %s and retrying %d file(s)", channel_id, len(retryable))
            join_res = _join_self_to_channel(channel_id)
            if "error" not in join_res:
                retry_result = _upload_files(channel_id, retryable)
                if "file_ids" in retry_result:
                    upload_result["file_ids"].extend(retry_result["file_ids"])
                    upload_result["files"].extend(retry_result["files"])
                    # Remove retried files from errors
                    retried_set = set(retryable)
                    upload_result["errors"] = [
                        e for e in upload_result["errors"]
                        if e["file"] not in retried_set or e.get("error") != "HTTP 403"
                    ]
                    if not upload_result["errors"]:
                        del upload_result["errors"]

    if "error" in upload_result and "file_ids" not in upload_result:
        print(json.dumps(upload_result, indent=2))
        sys.exit(1)

    return upload_result


# Mattermost reaction API expects short names (e.g. "+1" for :+1:, not raw Unicode).
_EMOJI_TO_NAME: dict[str, str] = {
    "\U0001f44d": "+1",  # 👍
    "\U0001f44e": "-1",  # 👎
    "\u2764": "heart",  # ❤
    "\U0001f600": "grinning",
    "\U0001f389": "tada",
}


def _normalize_emoji_name(raw: str) -> str:
    """Map Unicode / aliases to Mattermost emoji_name for POST /reactions."""
    s = (raw or "+1").strip()
    if s.startswith(":") and s.endswith(":"):
        s = s[1:-1]
    if s in _EMOJI_TO_NAME:
        return _EMOJI_TO_NAME[s]
    lower = s.lower().replace(" ", "_")
    aliases = {
        "thumbsup": "+1",
        "thumb_up": "+1",
        "like": "+1",
        "yes": "+1",
        "thumbs_down": "-1",
        "no": "-1",
    }
    return aliases.get(lower, s)


def _save_reaction(post_id: str, emoji_name: str, auto_join: bool) -> dict[str, Any]:
    """POST /api/v4/reactions — add emoji_name to post_id as the bot user."""
    me = _api("GET", "/users/me")
    if "error" in me:
        return me
    bot_id = me["id"]
    body = {"user_id": bot_id, "post_id": post_id, "emoji_name": emoji_name}
    result = _api("POST", "/reactions", body)
    if auto_join and _is_http_error(result, 403):
        post = _api("GET", f"/posts/{post_id}")
        if "error" not in post:
            ch_id = post.get("channel_id")
            if ch_id:
                join_res = _join_self_to_channel(ch_id)
                if "error" not in join_res:
                    result = _api("POST", "/reactions", body)
                    if "error" not in result:
                        result["retried_after_join"] = True
                else:
                    result["join_attempt"] = join_res
    return result


def cmd_react(args: argparse.Namespace) -> None:
    """Add an emoji reaction to a post (same membership rules as posting)."""
    if not args.post_id:
        print(json.dumps({"error": "--post-id is required for react"}))
        sys.exit(1)
    emoji_name = _normalize_emoji_name(args.emoji or "+1")
    result = _save_reaction(args.post_id, emoji_name, args.auto_join)
    print(json.dumps({**result, "emoji_name": emoji_name}, indent=2, default=str))


def cmd_post(args: argparse.Namespace) -> None:
    """Post a message to a channel (REQ-MMATT-003: with optional file attachments)."""
    channel_id = _resolve_channel(args.channel)
    if not channel_id:
        print(json.dumps({"error": f"Channel '{args.channel}' not found"}))
        sys.exit(1)

    # Upload files if provided
    upload_result = _handle_file_uploads(channel_id, args.file_path, args.auto_join)
    file_ids = upload_result["file_ids"] if upload_result and "file_ids" in upload_result else []

    payload: dict[str, Any] = {
        "channel_id": channel_id,
        "message": args.message,
    }
    if file_ids:
        payload["file_ids"] = file_ids

    result = _api("POST", "/posts", payload)
    if args.auto_join and _is_http_error(result, 403):
        join_res = _join_self_to_channel(channel_id)
        if "error" not in join_res:
            # Re-upload files after join (uploads are channel-scoped)
            if args.file_path:
                upload_result = _upload_files(channel_id, args.file_path)
                file_ids = upload_result.get("file_ids", [])
                payload["file_ids"] = file_ids if file_ids else []
            result = _api("POST", "/posts", payload)
            if "error" not in result:
                result["retried_after_join"] = True
        else:
            result["join_attempt"] = join_res

    # Merge upload metadata into output
    if upload_result and "files" in upload_result:
        result["files_uploaded"] = upload_result["files"]
        if upload_result.get("errors"):
            result["upload_warnings"] = upload_result["errors"]
    if file_ids:
        result.setdefault("file_ids", file_ids)
        log.info("Posted to %s with %d file(s) attached", args.channel, len(file_ids))

    print(json.dumps(result, indent=2, default=str))


def cmd_join(args: argparse.Namespace) -> None:
    """Explicitly add the bot to a channel (public or private if permitted)."""
    channel_id = _resolve_channel(args.channel)
    if not channel_id:
        print(json.dumps({"error": f"Channel '{args.channel}' not found"}))
        sys.exit(1)
    result = _join_self_to_channel(channel_id)
    print(json.dumps(result, indent=2, default=str))


def cmd_resolve_user(args: argparse.Namespace) -> None:
    """Resolve a Mattermost username to a user id (for DMs, cron `to` fields, etc.)."""
    username = (args.username or "").strip().lstrip("@")
    if not username:
        print(json.dumps({"error": "--username is required"}))
        sys.exit(1)
    path = f"/users/username/{quote(username)}"
    raw = _api("GET", path)
    if "error" in raw:
        print(json.dumps(raw, indent=2))
        sys.exit(1)
    out = {
        "id": raw.get("id"),
        "username": raw.get("username"),
        "nickname": raw.get("nickname"),
        "first_name": raw.get("first_name"),
        "last_name": raw.get("last_name"),
        "is_bot": raw.get("is_bot"),
    }
    print(json.dumps({"ok": True, "user": out}, indent=2))


def cmd_dm(args: argparse.Namespace) -> None:
    """Open or reuse a DM channel and post a message (REQ-MMATT-006: with optional files)."""
    username = (args.username or "").strip().lstrip("@")
    if not username:
        print(json.dumps({"error": "--username is required for dm"}))
        sys.exit(1)
    if not args.message:
        print(json.dumps({"error": "--message is required for dm"}))
        sys.exit(1)

    target = _api("GET", f"/users/username/{quote(username)}")
    if "error" in target:
        print(json.dumps(target, indent=2))
        sys.exit(1)

    me = _api("GET", "/users/me")
    if "error" in me:
        print(json.dumps(me, indent=2))
        sys.exit(1)

    bot_id = me["id"]
    target_id = target["id"]
    if bot_id == target_id:
        print(json.dumps({"error": "Cannot DM yourself"}))
        sys.exit(1)

    # Mattermost expects two user ids (sorted) for direct channel creation.
    pair = sorted([bot_id, target_id])
    ch = _api("POST", "/channels/direct", pair)
    if "error" in ch:
        print(json.dumps(ch, indent=2))
        sys.exit(1)

    channel_id = ch.get("id")
    if not channel_id:
        print(json.dumps({"error": "No channel id returned from direct channel API"}))
        sys.exit(1)

    # Upload files if provided
    upload_result = _handle_file_uploads(channel_id, args.file_path, args.auto_join)
    file_ids = upload_result["file_ids"] if upload_result and "file_ids" in upload_result else []

    payload: dict[str, Any] = {
        "channel_id": channel_id,
        "message": args.message,
    }
    if file_ids:
        payload["file_ids"] = file_ids

    result = _api("POST", "/posts", payload)
    if args.auto_join and _is_http_error(result, 403):
        join_res = _join_self_to_channel(channel_id)
        if "error" not in join_res:
            if args.file_path:
                upload_result = _upload_files(channel_id, args.file_path)
                file_ids = upload_result.get("file_ids", [])
                payload["file_ids"] = file_ids if file_ids else []
            result = _api("POST", "/posts", payload)

    out: dict[str, Any] = {"channel_id": channel_id, "target_user_id": target_id, "post": result}
    if upload_result and "files" in upload_result:
        out["files_uploaded"] = upload_result["files"]
        if upload_result.get("errors"):
            out["upload_warnings"] = upload_result["errors"]
    if file_ids:
        out["file_ids"] = file_ids
        log.info("DM to %s with %d file(s) attached", username, len(file_ids))
    print(json.dumps(out, indent=2, default=str))


def cmd_dispatch(args: argparse.Namespace) -> None:
    """Post a structured task dispatch (REQ-MMATT-004: with optional file attachments)."""
    if not args.recipient:
        print(json.dumps({"error": "--recipient is required for dispatch"}))
        sys.exit(1)
    if not args.message:
        print(json.dumps({"error": "--message is required for dispatch"}))
        sys.exit(1)
    channel_id = _resolve_channel(args.channel)
    if not channel_id:
        print(json.dumps({"error": f"Channel '{args.channel}' not found"}))
        sys.exit(1)

    # Upload files if provided
    upload_result = _handle_file_uploads(channel_id, args.file_path, args.auto_join)
    file_ids = upload_result["file_ids"] if upload_result and "file_ids" in upload_result else []

    task_id = args.task_id or f"TASK-{uuid.uuid4().hex[:8].upper()}"
    dispatch_payload: dict[str, Any] = {
        "sender": AGENT_NAME,
        "recipient": args.recipient,
        "task_id": task_id,
        "payload": args.message,
        "priority": args.priority,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if file_ids:
        dispatch_payload["attachments_count"] = len(file_ids)

    message = (
        f"**Task Dispatch** → @{args.recipient}\n"
        f"Priority: **{args.priority}**\n\n"
        f"{args.message}\n\n"
        f"```json\n{json.dumps(dispatch_payload, indent=2)}\n```"
    )

    post_payload: dict[str, Any] = {
        "channel_id": channel_id,
        "message": message,
        "props": {"dispatch": dispatch_payload},
    }
    if file_ids:
        post_payload["file_ids"] = file_ids

    result = _api("POST", "/posts", post_payload)

    if upload_result and "files" in upload_result:
        result["files_uploaded"] = upload_result["files"]
        if upload_result.get("errors"):
            result["upload_warnings"] = upload_result["errors"]
    if file_ids:
        result.setdefault("file_ids", file_ids)
        log.info("Dispatched to %s with %d file(s) attached", args.channel, len(file_ids))

    print(json.dumps(result, indent=2))


def cmd_read(args: argparse.Namespace) -> None:
    """Read recent messages from a channel."""
    channel_id = _resolve_channel(args.channel)
    if not channel_id:
        print(json.dumps({"error": f"Channel '{args.channel}' not found"}))
        sys.exit(1)

    posts = _api("GET", f"/channels/{channel_id}/posts?per_page={args.limit}")
    if "error" in posts:
        print(json.dumps(posts, indent=2))
        sys.exit(1)

    order = posts.get("order", [])
    post_map = posts.get("posts", {})
    messages = []
    for post_id in order:
        p = post_map.get(post_id, {})
        messages.append({
            "id": p.get("id"),
            "user_id": p.get("user_id"),
            "message": p.get("message", ""),
            "create_at": p.get("create_at"),
            "root_id": p.get("root_id", ""),
        })

    print(json.dumps({"channel": args.channel, "count": len(messages), "messages": messages}, indent=2))


def cmd_thread(args: argparse.Namespace) -> None:
    """Reply in a thread (REQ-MMATT-005: with optional file attachments)."""
    if not args.post_id:
        print(json.dumps({"error": "--post-id is required for thread"}))
        sys.exit(1)
    if not args.message:
        print(json.dumps({"error": "--message is required for thread"}))
        sys.exit(1)
    post = _api("GET", f"/posts/{args.post_id}")
    if "error" in post:
        print(json.dumps(post, indent=2))
        sys.exit(1)

    root_id = post.get("root_id") or post.get("id")
    channel_id = post.get("channel_id")

    # Upload files using the parent post's channel_id (REQ-MMATT-005)
    upload_result = _handle_file_uploads(channel_id, args.file_path, args.auto_join)
    file_ids = upload_result["file_ids"] if upload_result and "file_ids" in upload_result else []

    payload: dict[str, Any] = {
        "channel_id": channel_id,
        "root_id": root_id,
        "message": args.message,
    }
    if file_ids:
        payload["file_ids"] = file_ids

    result = _api("POST", "/posts", payload)

    if upload_result and "files" in upload_result:
        result["files_uploaded"] = upload_result["files"]
        if upload_result.get("errors"):
            result["upload_warnings"] = upload_result["errors"]
    if file_ids:
        result.setdefault("file_ids", file_ids)
        log.info("Thread reply with %d file(s) attached", len(file_ids))

    print(json.dumps(result, indent=2))


def cmd_upload(args: argparse.Namespace) -> None:
    """Upload file(s) without creating a post (REQ-MMATT-007)."""
    if not args.file_path:
        print(json.dumps({"error": "--file-path is required for upload"}))
        sys.exit(1)

    channel_id = _resolve_channel(args.channel)
    if not channel_id:
        print(json.dumps({"error": f"Channel '{args.channel}' not found"}))
        sys.exit(1)

    upload_result = _handle_file_uploads(channel_id, args.file_path, args.auto_join)
    out: dict[str, Any] = {
        "action": "upload",
        "channel": args.channel,
    }
    if upload_result:
        out["files"] = upload_result.get("files", [])
        out["file_ids"] = upload_result.get("file_ids", [])
        if upload_result.get("errors"):
            out["errors"] = upload_result["errors"]
    print(json.dumps(out, indent=2))


def cmd_channels(args: argparse.Namespace) -> None:
    """List channels the bot can see."""
    me = _api("GET", "/users/me")
    if "error" in me:
        print(json.dumps(me, indent=2))
        sys.exit(1)

    teams = _api("GET", f"/users/{me['id']}/teams")
    if isinstance(teams, dict) and "error" in teams:
        print(json.dumps(teams, indent=2))
        sys.exit(1)

    all_channels = []
    for team in (teams if isinstance(teams, list) else []):
        channels = _api("GET", f"/users/{me['id']}/teams/{team['id']}/channels")
        if isinstance(channels, list):
            for ch in channels:
                all_channels.append({
                    "name": ch.get("name"),
                    "display_name": ch.get("display_name"),
                    "id": ch.get("id"),
                    "team": team.get("name"),
                    "type": ch.get("type"),
                })

    print(json.dumps({"channels": all_channels}, indent=2))


def cmd_health(args: argparse.Namespace) -> None:
    """Check Mattermost server health."""
    result = _api("GET", "/system/ping")
    if "error" in result:
        print(json.dumps({"status": "unhealthy", **result}))
        sys.exit(1)
    print(json.dumps({"status": "healthy", "server": MM_URL, "response": result}))


def main() -> None:
    if not MM_TOKEN:
        print(json.dumps({"error": "MATTERMOST_BOT_TOKEN is not set"}))
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Mattermost Bridge — OpenClaw Agent Coordination")
    parser.add_argument("--action", required=True,
                        choices=[
                            "post", "read", "thread", "dispatch", "channels", "health",
                            "join", "resolve-user", "dm", "react", "upload",
                        ])
    parser.add_argument("--channel", default="coordination")
    parser.add_argument("--message", default="")
    parser.add_argument("--post-id", default="")
    parser.add_argument("--recipient", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--priority", default="normal", choices=["low", "normal", "high", "critical"])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--username",
        default="",
        help="Mattermost username without @ (for resolve-user, dm)",
    )
    # Default: retry post after joining channel on 403 (fixes bot not in channel).
    parser.set_defaults(auto_join=True)
    parser.add_argument(
        "--no-auto-join",
        dest="auto_join",
        action="store_false",
        help="Do not join channel and retry when Mattermost returns 403 (post, dm, react)",
    )
    parser.add_argument(
        "--emoji",
        default="+1",
        help="Reaction short name or emoji (e.g. +1, 👍, white_check_mark) for react action",
    )
    parser.add_argument(
        "--file-path",
        nargs="+",
        default=[],
        help="One or more file paths to attach to the post (max 5). "
             "Files are uploaded via Mattermost Files API before posting.",
    )

    args = parser.parse_args()

    actions = {
        "post": cmd_post,
        "read": cmd_read,
        "thread": cmd_thread,
        "dispatch": cmd_dispatch,
        "channels": cmd_channels,
        "health": cmd_health,
        "join": cmd_join,
        "resolve-user": cmd_resolve_user,
        "dm": cmd_dm,
        "react": cmd_react,
        "upload": cmd_upload,
    }
    actions[args.action](args)


if __name__ == "__main__":
    main()
