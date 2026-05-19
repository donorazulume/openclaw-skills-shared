#!/usr/bin/env python3
"""google-manager — Gmail, Calendar, Drive CLI built on openclaw-mcp-google.

SPEC-GAUTH-001 v2.0.0 (#323/#324). All Google API calls go through
``openclaw-mcp-google`` (port 8103) via :mod:`mcp_google`. This skill no longer
holds Google OAuth credentials.

Env (Doppler-injected):
    MCP_GOOGLE_URL            Base URL (default ``http://openclaw-mcp-google:8103``)
    MCP_TOKEN_GOOGLE_ROHO     Bearer token for openclaw-mcp-google
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from dateutil import parser as dtparser

sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'lib'))
import mcp_google  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("google-manager")

GMAIL_LABELS = ["01_Action", "02_Waiting", "03_Read"]
PARA_FOLDERS = ["00_Inbox", "01_Projects", "02_Areas", "03_Resources", "04_Archives"]
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"


def _call(tool: str, **arguments: Any) -> dict[str, Any]:
    try:
        result = mcp_google.call(tool, arguments)
    except mcp_google.GoogleMCPError as exc:
        sys.exit(f"ERROR: MCP Google call '{tool}' failed: {exc}")
    if isinstance(result, dict) and isinstance(result.get("error"), dict):
        err = result["error"]
        sys.exit(f"ERROR: tool {tool} returned {err.get('code') or '?'}: {err.get('message') or err}")
    if not isinstance(result, dict):
        sys.exit(f"ERROR: tool {tool} returned non-dict payload: {type(result).__name__}")
    return result


# ══════════════════════════════════════════════════════════════════════
#  GMAIL
# ══════════════════════════════════════════════════════════════════════


def gmail_triage(limit: int = 20) -> None:
    body = _call("google_mail_search", query="in:inbox", max_results=limit)
    messages = body.get("messages") or []
    if not messages:
        print("Inbox is empty.")
        return
    print(f"Gmail Inbox — showing {len(messages)} message(s)\n")
    print(f"{'#':<4} {'From':<35} {'Subject'}")
    print("-" * 90)
    for i, m in enumerate(messages, 1):
        sender = m.get("from", "—")
        if len(sender) > 33:
            sender = sender[:30] + "…"
        print(f"{i:<4} {sender:<35} {m.get('subject', '(no subject)')}")
    print(f"\nShowing {len(messages)} of INBOX. Use --limit to see more.")
    _gmail_inbox_expert_judgment()


def gmail_search(query: str, limit: int = 20) -> None:
    body = _call("google_mail_search", query=query, max_results=limit)
    messages = body.get("messages") or []
    if not messages:
        print(f"No messages found matching: {query}")
        return
    print(f"Gmail Search — '{query}' ({len(messages)} result(s))\n")
    print(f"{'#':<4} {'From':<35} {'Subject'}")
    print("-" * 90)
    for i, m in enumerate(messages, 1):
        sender = m.get("from", "—")
        if len(sender) > 33:
            sender = sender[:30] + "…"
        print(f"{i:<4} {sender:<35} {m.get('subject', '(no subject)')}")
    print(f"\nShowing top {len(messages)} result(s). Use --limit to see more.")


def gmail_send(
    to: str,
    subject: str,
    body: str | None = None,
    body_markdown: str | None = None,
) -> None:
    if body_markdown:
        from email_utils import markdown_to_html
        html_body = markdown_to_html(body_markdown)
    elif body:
        html_body = f"<pre>{body}</pre>"
    else:
        sys.exit("ERROR: send requires --body-markdown or --body")
    result = _call("google_mail_send", to=to, subject=subject, body_html=html_body)
    print("Email sent (via MCP Google).")
    print(f"  To:         {to}")
    print(f"  Subject:    {subject}")
    print(f"  Message ID: {result.get('message_id', '—')}")


def gmail_create_labels() -> None:
    print("Gmail — Label Initialization\n")
    created = 0
    for name in GMAIL_LABELS:
        res = _call("google_mail_create_label", name=name)
        if res.get("status") == "created":
            print(f"  + Created: {name}")
            created += 1
        else:
            print(f"  ✓ Exists:  {name}")
    print(f"\nDone. {created} label(s) created, {len(GMAIL_LABELS) - created} already existed.")


def _gmail_inbox_expert_judgment() -> None:
    info = _call("google_mail_label_info", label="INBOX")
    unread = info.get("messages_unread", 0)
    total = info.get("messages_total", 0)
    if unread > 50:
        print(f"\n[Expert Judgment] ⚠ Inbox Overflow: {unread} unread messages.")
    if total > 2000:
        print(f"\n[Expert Judgment] ⚠ Inbox Size Warning: {total} total messages.")


# ══════════════════════════════════════════════════════════════════════
#  CALENDAR
# ══════════════════════════════════════════════════════════════════════


def calendar_list(time_min: str | None = None) -> None:
    if time_min and time_min.lower() == "today":
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_min:
        start = dtparser.parse(time_min)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    else:
        start = datetime.now(timezone.utc)
    end = start + timedelta(days=1)

    body = _call(
        "google_calendar_list_events",
        start=start.isoformat(),
        end=end.isoformat(),
    )
    events = body.get("events") or []
    date_str = start.strftime("%A %d %B %Y")
    if not events:
        print(f"No events found for {date_str}.")
        return
    print(f"Calendar — {date_str}\n")
    print(f"{'#':<4} {'Time':<14} {'Summary':<45} {'Event ID'}")
    print("-" * 100)
    for i, e in enumerate(events, 1):
        s_raw = e.get("start", "")
        e_raw = e.get("end", "")
        try:
            s = dtparser.parse(s_raw).strftime("%H:%M")
            e_t = dtparser.parse(e_raw).strftime("%H:%M")
            time_str = f"{s}–{e_t}"
        except (ValueError, TypeError):
            time_str = s_raw[:10] if s_raw else "all-day"
        summary = e.get("summary", "(no title)")
        eid = e.get("id", "—")
        eid_short = eid[:20] + "…" if len(eid) > 20 else eid
        print(f"{i:<4} {time_str:<14} {summary:<45} {eid_short}")
    print(f"\nTotal: {len(events)} event(s)")


def calendar_add(summary: str, start_iso: str, duration_mins: int = 60) -> None:
    start_dt = dtparser.parse(start_iso)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(minutes=duration_mins)
    result = _call(
        "google_calendar_create_event",
        summary=summary,
        start=start_dt.isoformat(),
        end=end_dt.isoformat(),
    )
    print("Event created successfully.")
    print(f"  Summary:  {summary}")
    print(f"  Start:    {start_dt.isoformat()}")
    print(f"  End:      {end_dt.isoformat()}")
    print(f"  Event ID: {result.get('event_id', '—')}")
    print(f"  Link:     {result.get('html_link', '—')}")


def calendar_update(event_id: str, **kwargs: Any) -> None:
    updates: dict[str, Any] = {}
    if kwargs.get("summary"):
        updates["summary"] = kwargs["summary"]
    if kwargs.get("start"):
        start_dt = dtparser.parse(kwargs["start"])
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        duration = int(kwargs.get("duration") or 60)
        end_dt = start_dt + timedelta(minutes=duration)
        updates["start"] = start_dt.isoformat()
        updates["end"] = end_dt.isoformat()
    if kwargs.get("description"):
        updates["description"] = kwargs["description"]
    if kwargs.get("location"):
        updates["location"] = kwargs["location"]

    if not updates:
        print("Nothing to update — no fields provided.")
        return

    _call("google_calendar_update_event", event_id=event_id, updates=updates)
    print("Event updated successfully.")
    print(f"  Event ID: {event_id}")
    for k, v in updates.items():
        print(f"  {k}: {v}")


# ══════════════════════════════════════════════════════════════════════
#  DRIVE
# ══════════════════════════════════════════════════════════════════════


def _drive_resolve_or_create_folder(path: str) -> str | None:
    """Resolve a slash-separated folder path, creating each missing segment.

    Returns the leaf folder ID, or None if no path was given.
    """
    parts = [p.strip() for p in path.split("/") if p.strip()]
    if not parts:
        return None
    parent: str | None = None
    for name in parts:
        body = _call(
            "google_drive_list",
            folder_id=parent or "",
            query=f"name = '{name}' and mimeType = '{DRIVE_FOLDER_MIME}'",
            max_results=1,
        )
        candidates = body.get("files") or []
        if candidates:
            parent = candidates[0].get("id")
            continue
        created = _call("google_drive_create_folder", name=name, parent_id=parent)
        parent = created.get("folder_id") or created.get("id")
    return parent


def drive_init_para() -> None:
    print("Google Drive — P.A.R.A. Initialization\n")
    created = 0
    existing = 0
    for name in PARA_FOLDERS:
        body = _call(
            "google_drive_list",
            query=f"name = '{name}' and mimeType = '{DRIVE_FOLDER_MIME}'",
            max_results=1,
        )
        files = body.get("files") or []
        if files:
            print(f"  ✓ Exists:  {name}  (id: {files[0].get('id', '?')})")
            existing += 1
            continue
        res = _call("google_drive_create_folder", name=name)
        print(f"  + Created: {name}  (id: {res.get('folder_id', '?')})")
        created += 1
    print(f"\nDone. {created} folder(s) created, {existing} already existed.")


def drive_organize(
    file_id: str,
    target_folder: str | None = None,
    rename_desc: str | None = None,
) -> None:
    meta = _call("google_drive_get_file", file_id=file_id)
    current_name = meta.get("name", "Untitled")
    current_parents = meta.get("parents") or []
    print(f"Organizing file: {current_name} ({file_id})\n")

    new_name: str | None = None
    description: str | None = None
    add_parent_id: str | None = None
    remove_parent_ids: list[str] | None = None

    if rename_desc:
        date_prefix = datetime.now().strftime("%Y-%m-%d")
        if "." in current_name:
            ext = current_name.rsplit(".", 1)[-1]
            new_name = f"{date_prefix} - {rename_desc}.{ext}"
        else:
            new_name = f"{date_prefix} - {rename_desc}"
        print(f"  Rename:  {current_name} → {new_name}")

    existing_desc = meta.get("description", "") or ""
    tag = "Managed by OpenClaw"
    if tag not in existing_desc:
        description = f"{existing_desc}\n{tag}".strip() if existing_desc else tag
        print(f"  Tag:     Added '{tag}' to description")

    if target_folder:
        target_id = _drive_resolve_or_create_folder(target_folder)
        if target_id:
            add_parent_id = target_id
            remove_parent_ids = current_parents or None
            print(f"  Move:    → {target_folder} ({target_id})")

    if new_name is None and description is None and add_parent_id is None:
        print("  Nothing to change.")
        return

    _call(
        "google_drive_update_file",
        file_id=file_id,
        new_name=new_name,
        description=description,
        add_parent_id=add_parent_id,
        remove_parent_ids=remove_parent_ids,
    )
    print("\nDone.")


def drive_find(query: str) -> None:
    body = _call("google_drive_search", query=query, max_results=25)
    files = body.get("files") or []
    if not files:
        print(f"No files found matching: {query}")
        return
    print(f"Drive — {len(files)} file(s) matching '{query}'\n")
    print(f"{'#':<4} {'Modified':<12} {'Name':<45} {'ID'}")
    print("-" * 100)
    for i, f in enumerate(files, 1):
        mod = (f.get("modifiedTime") or "—")[:10]
        name = f.get("name", "—")
        fid = f.get("id", "—")
        suffix = " 📁" if f.get("mimeType") == DRIVE_FOLDER_MIME else ""
        print(f"{i:<4} {mod:<12} {name}{suffix:<45} {fid}")
    print()


def drive_list_folder(folder: str, limit: int = 50) -> None:
    if len(folder) > 20 and folder.replace("-", "").replace("_", "").isalnum():
        folder_id = folder
        folder_name = folder
    else:
        body = _call(
            "google_drive_list",
            query=f"name = '{folder}' and mimeType = '{DRIVE_FOLDER_MIME}'",
            max_results=1,
        )
        files = body.get("files") or []
        if not files:
            print(f"ERROR: Folder '{folder}' not found in Drive.")
            return
        folder_id = files[0].get("id", "")
        folder_name = folder

    body = _call("google_drive_list", folder_id=folder_id, max_results=limit)
    files = body.get("files") or []
    if not files:
        print(f"Folder '{folder_name}' is empty.")
        return

    folders = [f for f in files if f.get("mimeType") == DRIVE_FOLDER_MIME]
    docs = [f for f in files if f.get("mimeType") != DRIVE_FOLDER_MIME]
    print(f"Drive — '{folder_name}' ({folder_id})\n")
    print(f"  {len(folders)} subfolder(s), {len(docs)} file(s)  [showing up to {limit}]\n")
    print(f"{'#':<4} {'Modified':<12} {'Type':<18} {'Name':<45} {'ID'}")
    print("-" * 120)
    for i, f in enumerate(files, 1):
        mod = (f.get("modifiedTime") or "—")[:10]
        name = f.get("name", "—")
        fid = f.get("id", "—")
        mime = f.get("mimeType", "")
        if mime == DRIVE_FOLDER_MIME:
            ftype = "folder"
        else:
            ftype = mime.split(".")[-1][:18] if "." in mime else mime[:18]
        print(f"{i:<4} {mod:<12} {ftype:<18} {name:<45} {fid}")
    print(f"\nTotal shown: {len(files)}")


def drive_overview() -> None:
    body = _call("google_drive_list", folder_id="root", max_results=200)
    files = body.get("files") or []
    if not files:
        print("Drive appears empty or inaccessible.")
        return
    folders = [f for f in files if f.get("mimeType") == DRIVE_FOLDER_MIME]
    loose = [f for f in files if f.get("mimeType") != DRIVE_FOLDER_MIME]
    print("Google Drive — Overview\n")
    print(f"Root: {len(folders)} folder(s), {len(loose)} loose file(s)\n")
    print(f"{'Folder':<30} {'Items':>6}")
    print("-" * 50)
    for folder in folders:
        body2 = _call("google_drive_list", folder_id=folder.get("id", ""), max_results=1000)
        children = body2.get("files") or []
        print(f"  📁 {folder.get('name', '?'):<28} {len(children):>6}")
    if loose:
        print(f"\nLoose files at root ({len(loose)}):")
        for f in loose[:10]:
            print(f"  📄 {f.get('name', '?')}")
        if len(loose) > 10:
            print(f"  … and {len(loose) - 10} more")
    print()
    print("Use --action list-folder --folder <name> to browse any folder's contents.")


def drive_download(file_id: str, output_dir: str, filename: str | None = None) -> None:
    import base64
    from pathlib import Path

    body = _call("google_drive_download", file_id=file_id)
    drive_name = body.get("name") or file_id
    content_b64 = body.get("content_b64") or ""
    if not content_b64:
        sys.exit(f"ERROR: empty download body for {file_id}")
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    local_name = filename or drive_name
    local_path = os.path.join(output_dir, local_name)
    with open(local_path, "wb") as fh:
        fh.write(base64.b64decode(content_b64))
    print(json.dumps({
        "action": "download",
        "status": "success",
        "drive_file_id": file_id,
        "drive_name": drive_name,
        "local_path": local_path,
        "size_bytes": os.path.getsize(local_path),
        "mime_type": body.get("mime_type", ""),
    }))


# ══════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(description="Google Manager — Gmail, Calendar, Drive (via openclaw-mcp-google)")
    parser.add_argument("--service", required=True, choices=["gmail", "calendar", "drive"])
    parser.add_argument("--action", required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--to")
    parser.add_argument("--subject")
    parser.add_argument("--body")
    parser.add_argument("--body-markdown", dest="body_markdown")
    parser.add_argument("--time-min")
    parser.add_argument("--summary")
    parser.add_argument("--start")
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--event-id")
    parser.add_argument("--description")
    parser.add_argument("--location")
    parser.add_argument("--file-id")
    parser.add_argument("--target-folder")
    parser.add_argument("--rename")
    parser.add_argument("--query")
    parser.add_argument("--folder")
    parser.add_argument("--output-dir", default="/home/node/.openclaw/downloads")
    parser.add_argument("--filename")
    args = parser.parse_args()

    if args.service == "gmail":
        if args.action == "triage":
            gmail_triage(limit=args.limit)
        elif args.action == "search":
            if not args.query:
                parser.error("--query is required for gmail search")
            gmail_search(args.query, limit=args.limit)
        elif args.action == "send":
            if not args.to or not args.subject:
                parser.error("--to and --subject are required for gmail send")
            gmail_send(args.to, args.subject, body=args.body, body_markdown=args.body_markdown)
        elif args.action == "create-labels":
            gmail_create_labels()
        else:
            parser.error(f"Unknown gmail action: {args.action}")

    elif args.service == "calendar":
        if args.action == "list":
            calendar_list(time_min=args.time_min)
        elif args.action == "add":
            if not args.summary or not args.start:
                parser.error("--summary and --start are required for calendar add")
            calendar_add(args.summary, args.start, args.duration)
        elif args.action == "update":
            if not args.event_id:
                parser.error("--event-id is required for calendar update")
            calendar_update(
                args.event_id,
                summary=args.summary,
                start=args.start,
                duration=args.duration,
                description=args.description,
                location=args.location,
            )
        else:
            parser.error(f"Unknown calendar action: {args.action}")

    elif args.service == "drive":
        if args.action == "init-para":
            drive_init_para()
        elif args.action == "organize":
            if not args.file_id:
                parser.error("--file-id is required for drive organize")
            drive_organize(args.file_id, target_folder=args.target_folder, rename_desc=args.rename)
        elif args.action == "find":
            if not args.query:
                parser.error("--query is required for drive find")
            drive_find(args.query)
        elif args.action == "list-folder":
            if not args.folder:
                parser.error("--folder is required for drive list-folder")
            drive_list_folder(args.folder, limit=args.limit)
        elif args.action == "overview":
            drive_overview()
        elif args.action == "download":
            if not args.file_id:
                parser.error("--file-id is required for drive download")
            drive_download(args.file_id, args.output_dir, filename=args.filename)
        else:
            parser.error(f"Unknown drive action: {args.action}")


if __name__ == "__main__":
    main()
