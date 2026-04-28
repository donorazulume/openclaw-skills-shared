#!/usr/bin/env python3
"""
Minimal Mattermost channel-join helper (stdlib only — no requests).

Used by post-deploy when bridge.py may be an older build without --action join.
Same REST flow as bridge.cmd_join / _join_self_to_channel.

Environment:
  MATTERMOST_URL        (default http://mattermost:8065)
  MATTERMOST_BOT_TOKEN  (required)

Usage:
  python3 mm_join.py <channel_name>
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

MM_URL = os.environ.get("MATTERMOST_URL", "http://mattermost:8065").rstrip("/")
MM_TOKEN = os.environ.get("MATTERMOST_BOT_TOKEN", "")


def _api(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{MM_URL}/api/v4{path}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {MM_TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()[:2000]
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"detail": raw}
        base: dict = {"error": f"HTTP {e.code}"}
        if isinstance(parsed, dict):
            base.update(parsed)
        else:
            base["detail"] = raw
        return base


def _resolve_channel(name: str) -> str | None:
    me = _api("GET", "/users/me")
    if me.get("error") and "id" not in me:
        return None
    uid = me.get("id")
    if not uid:
        return None
    teams = _api("GET", f"/users/{uid}/teams")
    if isinstance(teams, dict) and teams.get("error"):
        return None
    for team in teams or []:
        tid = team.get("id")
        if not tid:
            continue
        ch = _api("GET", f"/teams/{tid}/channels/name/{name}")
        if ch.get("id") and not str(ch.get("error", "")).startswith("HTTP"):
            return ch["id"]
    return None


def join_channel(name: str) -> dict:
    if not MM_TOKEN:
        return {"error": "MATTERMOST_BOT_TOKEN is not set"}
    channel_id = _resolve_channel(name)
    if not channel_id:
        return {"error": f"Channel '{name}' not found"}
    me = _api("GET", "/users/me")
    if me.get("error") and "id" not in me:
        return me
    bot_id = me.get("id")
    if not bot_id:
        return {"error": "Could not resolve bot user id"}
    result = _api("POST", f"/channels/{channel_id}/members", {"user_id": bot_id})
    if not result.get("error"):
        return {**result, "channel": name, "status": "joined"}
    # Duplicate membership often returns 400 — treat as success for deploy
    err = str(result.get("error", ""))
    msg = json.dumps(result).lower()
    if err.startswith("HTTP 4") and (
        "already" in msg or "member" in msg or "exists" in msg or "duplicate" in msg
    ):
        return {"status": "ok", "channel": name, "already_member": True}
    return result


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: mm_join.py <channel_name>"}))
        sys.exit(1)
    name = sys.argv[1].strip()
    if not name:
        print(json.dumps({"error": "Empty channel name"}))
        sys.exit(1)
    out = join_channel(name)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
