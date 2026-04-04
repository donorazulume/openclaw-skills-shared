#!/usr/bin/env python3
"""
google-manager — Unified Gmail, Calendar, and Drive manager for OpenCLAW.

Heavy-lifter pattern: all Google API logic lives here; the OpenCLAW agent
only triggers CLI commands.

Environment variables (injected via Doppler):
    GOOGLE_TOKEN_JSON         OAuth2 token JSON (contents of token.json)
    GOOGLE_CREDENTIALS_JSON   OAuth2 client credentials JSON (contents of credentials.json)
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from dateutil import parser as dtparser
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

# Add shared lib to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'lib'))
from google_clients import get_credentials
from email_utils import markdown_to_html, markdown_to_plaintext

# ── Logging ──────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("google-manager")

# ── Constants ────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
]

GMAIL_LABELS = ["01_Action", "02_Waiting", "03_Read"]

PARA_FOLDERS = [
    "00_Inbox",
    "01_Projects",
    "02_Areas",
    "03_Resources",
    "04_Archives",
]

DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"

# ── Auth ─────────────────────────────────────────────────────────────


def _authenticate() -> Credentials:
    """Build OAuth2 credentials from environment variables."""
    return get_credentials(SCOPES)


# ── Service builders ─────────────────────────────────────────────────


def _gmail(creds: Credentials):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _calendar(creds: Credentials):
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _drive(creds: Credentials):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ══════════════════════════════════════════════════════════════════════
#  GMAIL
# ══════════════════════════════════════════════════════════════════════


def _batch_fetch_messages(svc, messages):
    """Helper to batch fetch message details."""
    msg_map = {}
    batch = svc.new_batch_http_request()

    def callback(request_id, response, exception):
        if not exception:
            msg_map[request_id] = response

    for msg_stub in messages:
        batch.add(
            svc.users().messages().get(
                userId="me", id=msg_stub["id"], format="metadata",
                metadataHeaders=["From", "Subject"]
            ),
            callback=callback,
            request_id=msg_stub["id"]
        )
    batch.execute()
    return msg_map


def gmail_triage(svc, limit: int = 20) -> None:
    """List recent INBOX messages with sender, subject, and snippet."""
    results = svc.users().messages().list(
        userId="me", labelIds=["INBOX"], maxResults=limit,
    ).execute()
    messages = results.get("messages", [])

    if not messages:
        print("Inbox is empty.")
        return

    print(f"Gmail Inbox — showing {len(messages)} message(s)\n")
    print(f"{'#':<4} {'From':<35} {'Subject'}")
    print("-" * 90)

    msg_map = _batch_fetch_messages(svc, messages)

    for i, stub in enumerate(messages, 1):
        msg = msg_map.get(stub["id"])
        if not msg:
            continue
        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        sender = headers.get("from", "—")
        if len(sender) > 33:
            sender = sender[:30] + "…"
        subject = headers.get("subject", "(no subject)")
        print(f"{i:<4} {sender:<35} {subject}")

    print(f"\nShowing {len(messages)} of INBOX. Use --limit to see more.")

    expert_judgment(gmail_service=svc)


def gmail_search(svc, query: str, limit: int = 20) -> None:
    """Search Gmail messages using a query string (standard Gmail syntax)."""
    results = svc.users().messages().list(
        userId="me", q=query, maxResults=limit,
    ).execute()
    messages = results.get("messages", [])

    if not messages:
        print(f"No messages found matching: {query}")
        return

    print(f"Gmail Search — '{query}' ({len(messages)} result(s))\n")
    print(f"{'#':<4} {'From':<35} {'Subject'}")
    print("-" * 90)

    msg_map = _batch_fetch_messages(svc, messages)

    for i, stub in enumerate(messages, 1):
        msg = msg_map.get(stub["id"])
        if not msg:
            continue
        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        sender = headers.get("from", "—")
        if len(sender) > 33:
            sender = sender[:30] + "…"
        subject = headers.get("subject", "(no subject)")
        print(f"{i:<4} {sender:<35} {subject}")

    print(f"\nShowing top {len(messages)} result(s). Use --limit to see more.")


def gmail_send(
    svc,
    to: str,
    subject: str,
    body: str | None = None,
    body_markdown: str | None = None,
) -> None:
    """Compose and send an email (HTML-first via body_markdown).

    When *body_markdown* is provided the body is converted to a
    multipart/alternative message (HTML + plain-text fallback).
    *body* is a legacy plain-text fallback — prefer *body_markdown*.
    """
    if body_markdown:
        html_body = markdown_to_html(body_markdown)
        plain_body = markdown_to_plaintext(body_markdown)
        mime: MIMEMultipart | MIMEText = MIMEMultipart("alternative")
        mime["to"] = to
        mime["subject"] = subject
        mime.attach(MIMEText(plain_body, "plain", "utf-8"))
        mime.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        mime = MIMEText(body or "", "plain", "utf-8")
        mime["to"] = to
        mime["subject"] = subject

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
    result = svc.users().messages().send(
        userId="me", body={"raw": raw},
    ).execute()

    fmt = "HTML" if body_markdown else "plain text"
    print(f"Email sent ({fmt}) successfully.")
    print(f"  To:         {to}")
    print(f"  Subject:    {subject}")
    print(f"  Message ID: {result.get('id', '—')}")


def gmail_create_labels(svc) -> None:
    """Ensure the ETS triage labels exist in Gmail using batch requests."""
    existing = {
        lbl["name"]: lbl["id"]
        for lbl in svc.users().labels().list(userId="me").execute().get("labels", [])
    }

    print("Gmail — Label Initialization\n")
    created = 0
    batch = svc.new_batch_http_request()

    def callback(request_id, response, exception):
        nonlocal created
        if exception:
            print(f"  ✗ Failed:  {request_id} — {exception}")
        else:
            print(f"  + Created: {request_id}")
            created += 1

    pending = False
    for name in GMAIL_LABELS:
        if name in existing:
            print(f"  ✓ Exists:  {name}")
        else:
            batch.add(
                svc.users().labels().create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                ),
                callback=callback,
                request_id=name
            )
            pending = True

    if pending:
        batch.execute()

    print(f"\nDone. {created} label(s) created, {len(GMAIL_LABELS) - created} already existed.")


def expert_judgment(calendar_service=None, gmail_service=None, drive_service=None) -> bool:
    """Check for calendar overload, overflowing inbox, or risky files."""
    status = True

    # ── Calendar Check ────────────────────────────────────────────────
    if calendar_service:
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)

        try:
            events_result = calendar_service.events().list(
                calendarId="primary",
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            events = events_result.get("items", [])

            total_minutes = 0
            for event in events:
                # Skip all-day events (usually not meetings)
                if "dateTime" not in event.get("start", {}):
                    continue

                start_dt = dtparser.parse(event["start"]["dateTime"])
                end_dt = dtparser.parse(event["end"]["dateTime"])
                duration = (end_dt - start_dt).total_seconds() / 60

                # Heuristic: exclude "Focus Time" or "OOO"
                summary = event.get("summary", "").lower()
                if "focus" in summary or "ooo" in summary:
                    continue

                # Check for large meetings
                attendees = event.get("attendees", [])
                if len(attendees) > 10:
                    print(f"\n[Expert Judgment] ⚠ Large Meeting: '{event.get('summary')}' has {len(attendees)} attendees.")

                total_minutes += duration

            if total_minutes > 360:  # 6 hours
                print(f"\n[Expert Judgment] ⚠ Calendar Overload: {total_minutes/60:.1f} hours of meetings today.")
                status = False
        except Exception:
            pass  # Fail gracefully if calendar access issue

    # ── Gmail Check ───────────────────────────────────────────────────
    if gmail_service:
        try:
            label = gmail_service.users().labels().get(userId="me", id="INBOX").execute()
            unread = label.get("messagesUnread", 0)
            total = label.get("messagesTotal", 0)

            if unread > 50:
                print(f"\n[Expert Judgment] ⚠ Inbox Overflow: {unread} unread messages.")
                status = False

            if total > 2000:
                print(f"\n[Expert Judgment] ⚠ Inbox Size Warning: {total} total messages.")
                status = False

        except Exception:
            pass # Fail gracefully

    # ── Drive Check ───────────────────────────────────────────────────
    if drive_service:
        try:
            # Check for sensitive filenames exposed
            query = "name contains 'password' or name contains 'secret' and trashed = false"
            results = drive_service.files().list(
                q=query, spaces="drive", fields="files(id, name)", pageSize=5
            ).execute()
            files = results.get("files", [])
            if files:
                print(f"\n[Expert Judgment] ⚠ Security Risk: Found {len(files)} file(s) with 'password'/'secret' in name.")
                for f in files:
                    print(f"  - {f['name']} ({f['id']})")
                status = False
        except Exception:
            pass

    return status


# ══════════════════════════════════════════════════════════════════════
#  CALENDAR
# ══════════════════════════════════════════════════════════════════════


def calendar_list(svc, time_min: str | None = None) -> None:
    """List upcoming calendar events."""
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

    events_result = svc.events().list(
        calendarId="primary",
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        maxResults=50,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    events = events_result.get("items", [])

    date_str = start.strftime("%A %d %B %Y")
    if not events:
        print(f"No events found for {date_str}.")
        return

    print(f"Calendar — {date_str}\n")
    print(f"{'#':<4} {'Time':<14} {'Summary':<45} {'Event ID'}")
    print("-" * 100)

    for i, event in enumerate(events, 1):
        start_raw = event["start"].get("dateTime", event["start"].get("date", ""))
        end_raw = event["end"].get("dateTime", event["end"].get("date", ""))
        try:
            s = dtparser.parse(start_raw).strftime("%H:%M")
            e = dtparser.parse(end_raw).strftime("%H:%M")
            time_str = f"{s}–{e}"
        except (ValueError, TypeError):
            time_str = start_raw[:10] if start_raw else "all-day"

        summary = event.get("summary", "(no title)")
        eid = event.get("id", "—")
        # Truncate long event IDs for readability
        eid_short = eid[:20] + "…" if len(eid) > 20 else eid
        print(f"{i:<4} {time_str:<14} {summary:<45} {eid_short}")

    print(f"\nTotal: {len(events)} event(s)")

    expert_judgment(calendar_service=svc)


def calendar_add(svc, summary: str, start_iso: str, duration_mins: int = 60) -> None:
    """Insert a new calendar event."""
    start_dt = dtparser.parse(start_iso)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(minutes=duration_mins)

    event_body = {
        "summary": summary,
        "start": {"dateTime": start_dt.isoformat()},
        "end": {"dateTime": end_dt.isoformat()},
    }

    event = svc.events().insert(calendarId="primary", body=event_body).execute()

    print(f"Event created successfully.")
    print(f"  Summary:  {summary}")
    print(f"  Start:    {start_dt.isoformat()}")
    print(f"  End:      {end_dt.isoformat()}")
    print(f"  Event ID: {event.get('id', '—')}")
    print(f"  Link:     {event.get('htmlLink', '—')}")


def calendar_update(svc, event_id: str, **kwargs: Any) -> None:
    """Patch an existing calendar event with provided fields."""
    patch_body: dict[str, Any] = {}

    if kwargs.get("summary"):
        patch_body["summary"] = kwargs["summary"]
    if kwargs.get("start"):
        start_dt = dtparser.parse(kwargs["start"])
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        patch_body["start"] = {"dateTime": start_dt.isoformat()}
        duration = int(kwargs.get("duration", 60))
        end_dt = start_dt + timedelta(minutes=duration)
        patch_body["end"] = {"dateTime": end_dt.isoformat()}
    if kwargs.get("description"):
        patch_body["description"] = kwargs["description"]
    if kwargs.get("location"):
        patch_body["location"] = kwargs["location"]

    if not patch_body:
        print("Nothing to update — no fields provided.")
        return

    event = svc.events().patch(
        calendarId="primary", eventId=event_id, body=patch_body,
    ).execute()

    print(f"Event updated successfully.")
    print(f"  Event ID: {event_id}")
    for key, val in patch_body.items():
        if isinstance(val, dict):
            val = val.get("dateTime", val)
        print(f"  {key}: {val}")


# ══════════════════════════════════════════════════════════════════════
#  DRIVE — The P.A.R.A. Enforcer
# ══════════════════════════════════════════════════════════════════════


def _drive_find_folder(svc, name: str, parent_id: str | None = None) -> str | None:
    """Find a folder by name, optionally within a parent. Returns ID or None."""
    # Sanitize name to prevent query injection
    name = name.replace("\\", "\\\\").replace("'", "\\'")
    q = f"name = '{name}' and mimeType = '{DRIVE_FOLDER_MIME}' and trashed = false"
    if parent_id:
        q += f" and '{parent_id}' in parents"

    results = svc.files().list(
        q=q, spaces="drive", fields="files(id, name)", pageSize=1,
    ).execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def _drive_create_folder(svc, name: str, parent_id: str | None = None) -> str:
    """Create a folder and return its ID."""
    body: dict[str, Any] = {
        "name": name,
        "mimeType": DRIVE_FOLDER_MIME,
    }
    if parent_id:
        body["parents"] = [parent_id]

    folder = svc.files().create(body=body, fields="id").execute()
    return folder["id"]


def _drive_resolve_path(svc, path: str) -> str | None:
    """Resolve a slash-separated folder path to a Drive folder ID.

    Example: '01_Projects/MyProject' -> folder ID of MyProject
    """
    parts = [p.strip() for p in path.split("/") if p.strip()]
    if not parts:
        return None

    # Optimisation: Batch fetch all potential folder candidates
    # Unique names to query
    names = list(set(parts))
    # Escape single quotes for Drive query
    query_parts = []
    for n in names:
        safe_name = n.replace("\\", "\\\\").replace("'", "\\'")
        query_parts.append(f"name = '{safe_name}'")

    # Construct OR query
    name_query = " or ".join(query_parts)
    q = f"mimeType = '{DRIVE_FOLDER_MIME}' and trashed = false and ({name_query})"

    # Fetch all candidates
    # We might need pagination if there are many folders with same names, but pageSize=1000 is usually enough
    results = svc.files().list(
        q=q, spaces="drive", fields="files(id, name, parents)", pageSize=1000
    ).execute()
    files = results.get("files", [])

    if not files:
        return None

    # Index candidates by name
    candidates = defaultdict(list)
    for f in files:
        candidates[f.get("name")].append(f)

    # Resolve path level by level
    # Start with valid parent IDs as {None} (representing root/start of search)
    # Since the first part can be anywhere (per original logic), we treat None as a wildcard parent for the first level.
    valid_parent_ids = {None}

    for i, part in enumerate(parts):
        potential_matches = candidates.get(part, [])
        next_valid_parent_ids = set()

        for match in potential_matches:
            # Check if this folder's parent is in our valid set
            if i == 0:
                # Accept all candidates for the first part (wildcard parent check)
                next_valid_parent_ids.add(match.get("id"))
            else:
                # Check if any parent of this folder is in the valid set from previous level
                parents = match.get("parents", [])
                if any(p in valid_parent_ids for p in parents):
                    next_valid_parent_ids.add(match.get("id"))

        if not next_valid_parent_ids:
            return None

        valid_parent_ids = next_valid_parent_ids

    # Return one of the valid IDs (arbitrarily pick first one found in the set)
    return list(valid_parent_ids)[0] if valid_parent_ids else None


def drive_init_para(svc) -> None:
    """Ensure root P.A.R.A. folders exist in Drive using batch requests."""
    print("Google Drive — P.A.R.A. Initialization\n")

    # First, check existence (Drive API list is hard to batch for distinct queries easily without complexity,
    # but creation can be batched).
    # We will check existence one-by-one (fast enough usually) or could batch the list queries if needed.
    # For simplicity and to follow the pattern, we'll check then batch create.

    to_create = []
    created = 0
    existing_count = 0

    # Optimisation: List all potential PARA folders in one API call
    name_query = " or ".join(f"name = '{name}'" for name in PARA_FOLDERS)
    q = f"mimeType = '{DRIVE_FOLDER_MIME}' and trashed = false and ({name_query})"

    results = svc.files().list(
        q=q, spaces="drive", fields="files(id, name)", pageSize=100
    ).execute()
    found_files = results.get("files", [])
    existing_map = {f["name"]: f["id"] for f in found_files}

    for folder_name in PARA_FOLDERS:
        if folder_name in existing_map:
            print(f"  ✓ Exists:  {folder_name}  (id: {existing_map[folder_name]})")
            existing_count += 1
        else:
            to_create.append(folder_name)

    if not to_create:
        print(f"\nDone. 0 folder(s) created, {existing_count} already existed.")
        return

    batch = svc.new_batch_http_request()

    def callback(request_id, response, exception):
        nonlocal created
        if exception:
            print(f"  ✗ Failed:  {request_id} — {exception}")
        else:
            print(f"  + Created: {request_id}  (id: {response.get('id')})")
            created += 1

    for folder_name in to_create:
        body = {
            "name": folder_name,
            "mimeType": DRIVE_FOLDER_MIME,
        }
        batch.add(
            svc.files().create(body=body, fields="id"),
            callback=callback,
            request_id=folder_name
        )

    batch.execute()

    print(f"\nDone. {created} folder(s) created, "
          f"{existing_count} already existed.")

    expert_judgment(drive_service=svc)


def drive_organize(
    svc,
    file_id: str,
    target_folder: str | None = None,
    rename_desc: str | None = None,
) -> None:
    """Organize a Drive file: rename with date prefix, tag, and move."""
    # Verify the file exists
    try:
        file_meta = svc.files().get(
            fileId=file_id, fields="id, name, parents, description",
        ).execute()
    except HttpError as exc:
        if exc.resp.status == 404:
            sys.exit(f"ERROR: File not found — {file_id}")
        raise

    current_name = file_meta.get("name", "Untitled")
    current_parents = file_meta.get("parents", [])
    print(f"Organizing file: {current_name} ({file_id})\n")

    update_body: dict[str, Any] = {}
    add_parents = ""
    remove_parents = ""

    # 1. Rename with date prefix
    if rename_desc:
        date_prefix = datetime.now().strftime("%Y-%m-%d")
        # Preserve file extension if present
        if "." in current_name:
            ext = current_name.rsplit(".", 1)[-1]
            new_name = f"{date_prefix} - {rename_desc}.{ext}"
        else:
            new_name = f"{date_prefix} - {rename_desc}"
        update_body["name"] = new_name
        print(f"  Rename:  {current_name} → {new_name}")

    # 2. Tag with description
    existing_desc = file_meta.get("description", "") or ""
    tag = "Managed by OpenClaw"
    if tag not in existing_desc:
        new_desc = f"{existing_desc}\n{tag}".strip() if existing_desc else tag
        update_body["description"] = new_desc
        print(f"  Tag:     Added '{tag}' to description")

    # 3. Move to target folder
    if target_folder:
        target_id = _drive_resolve_path(svc, target_folder)
        if not target_id:
            # Try creating the path
            parts = [p.strip() for p in target_folder.split("/") if p.strip()]
            parent = None
            for part in parts:
                fid = _drive_find_folder(svc, part, parent)
                if not fid:
                    fid = _drive_create_folder(svc, part, parent)
                    log.info("Created folder: %s (%s)", part, fid)
                parent = fid
            target_id = parent

        if target_id:
            add_parents = target_id
            remove_parents = ",".join(current_parents) if current_parents else ""
            print(f"  Move:    → {target_folder} ({target_id})")

    if not update_body and not add_parents:
        print("  Nothing to change.")
        return

    svc.files().update(
        fileId=file_id,
        body=update_body if update_body else None,
        addParents=add_parents or None,
        removeParents=remove_parents or None,
        fields="id, name, parents",
    ).execute()

    print("\nDone.")


def drive_find(svc, query: str) -> None:
    """Search Drive for files matching a query string."""
    # Sanitize query to prevent injection
    query = query.replace("\\", "\\\\").replace("'", "\\'")
    q = f"name contains '{query}' and trashed = false"
    results = svc.files().list(
        q=q, spaces="drive",
        fields="files(id, name, mimeType, modifiedTime, parents)",
        pageSize=25, orderBy="modifiedTime desc",
    ).execute()
    files = results.get("files", [])

    if not files:
        print(f"No files found matching: {query}")
        return

    print(f"Drive — {len(files)} file(s) matching '{query}'\n")
    print(f"{'#':<4} {'Modified':<12} {'Name':<45} {'ID'}")
    print("-" * 100)

    for i, f in enumerate(files, 1):
        mod = f.get("modifiedTime", "—")[:10]
        name = f.get("name", "—")
        fid = f.get("id", "—")
        is_folder = f.get("mimeType") == DRIVE_FOLDER_MIME
        suffix = " 📁" if is_folder else ""
        print(f"{i:<4} {mod:<12} {name}{suffix:<45} {fid}")

    print()


def drive_list_folder(svc, folder: str, limit: int = 50) -> None:
    """List the contents of a Drive folder, by name or ID.

    If 'folder' looks like a Drive file ID (long alphanumeric string), it is
    used directly; otherwise the folder is resolved by name.
    """
    # Determine whether 'folder' is an ID or a name
    if len(folder) > 20 and folder.replace("-", "").replace("_", "").isalnum():
        folder_id = folder
        folder_name = folder
    else:
        folder_id = _drive_find_folder(svc, folder)
        folder_name = folder
        if not folder_id:
            print(f"ERROR: Folder '{folder}' not found in Drive.")
            print("Tip: use --action find --query to search for it by name first.")
            return

    log.info("Listing contents of folder %s (%s)", folder_name, folder_id)
    q = f"'{folder_id}' in parents and trashed = false"
    results = svc.files().list(
        q=q, spaces="drive",
        fields="files(id, name, mimeType, modifiedTime, size)",
        pageSize=limit,
        orderBy="folder,name",
    ).execute()
    files = results.get("files", [])

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
        mod = f.get("modifiedTime", "—")[:10]
        name = f.get("name", "—")
        fid = f.get("id", "—")
        mime = f.get("mimeType", "")
        if mime == DRIVE_FOLDER_MIME:
            ftype = "folder"
        else:
            ftype = mime.split(".")[-1][:18] if "." in mime else mime[:18]
        print(f"{i:<4} {mod:<12} {ftype:<18} {name:<45} {fid}")

    print(f"\nTotal shown: {len(files)}")
    if len(files) >= limit:
        print(f"(Use --limit to increase. There may be more items.)")


def drive_overview(svc) -> None:
    """Print a high-level summary of the full Drive structure.

    Shows all top-level folders with file counts, and recursively lists
    PARA sub-folders with their own counts.
    """
    print("Google Drive — Overview\n")

    # Get everything at root level
    root_results = svc.files().list(
        q="'root' in parents and trashed = false",
        spaces="drive",
        fields="files(id, name, mimeType)",
        pageSize=200,
        orderBy="folder,name",
    ).execute()
    root_items = root_results.get("files", [])

    if not root_items:
        print("Drive appears empty or inaccessible.")
        return

    root_folders = [f for f in root_items if f.get("mimeType") == DRIVE_FOLDER_MIME]
    root_files = [f for f in root_items if f.get("mimeType") != DRIVE_FOLDER_MIME]

    print(f"Root: {len(root_folders)} folder(s), {len(root_files)} loose file(s)\n")
    print(f"{'Folder':<30} {'Items':>6}   {'Subfolders'}")
    print("-" * 70)

    batch = svc.new_batch_http_request()
    folder_results = {}

    def callback(request_id, response, exception):
        if exception:
            folder_results[request_id] = []
        else:
            folder_results[request_id] = response.get("files", [])

    for folder in root_folders:
        fid = folder["id"]
        batch.add(
            svc.files().list(
                q=f"'{fid}' in parents and trashed = false",
                spaces="drive",
                fields="files(id, name, mimeType)",
                pageSize=1000,
            ),
            callback=callback,
            request_id=fid
        )

    if root_folders:
        batch.execute()

    for folder in root_folders:
        fid = folder["id"]
        fname = folder["name"]

        children = folder_results.get(fid, [])
        child_count = len(children)
        subfolders = [c["name"] for c in children if c.get("mimeType") == DRIVE_FOLDER_MIME]

        sub_str = ", ".join(subfolders[:5])
        if len(subfolders) > 5:
            sub_str += f" (+{len(subfolders)-5} more)"

        print(f"  {'📁 ' + fname:<28} {child_count:>6}   {sub_str}")

    if root_files:
        print(f"\nLoose files at root ({len(root_files)}):")
        for f in root_files[:10]:
            print(f"  📄 {f['name']}")
        if len(root_files) > 10:
            print(f"  … and {len(root_files) - 10} more")

    print()
    print("Use --action list-folder --folder <name> to browse any folder's contents.")

    expert_judgment(drive_service=svc)


# ── MIME type mappings for Google Workspace export ───────────────────

# Maps Google Workspace MIME types → (export_mime, file_extension)
# We prefer PDF for documents/presentations (compatible with document-processor)
# and CSV for spreadsheets.
_WORKSPACE_EXPORT: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document":     ("application/pdf", ".pdf"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet":  ("text/csv", ".csv"),
    "application/vnd.google-apps.drawing":      ("image/png", ".png"),
    "application/vnd.google-apps.script":       ("application/json", ".json"),
}

_SAFE_FILENAME_RE = re.compile(r'[^\w\-.]')


def _safe_filename(name: str) -> str:
    """Sanitize a Drive filename for use as a local file name."""
    return _SAFE_FILENAME_RE.sub("_", name)


def drive_download(
    svc,
    file_id: str,
    output_dir: str,
    filename: str | None = None,
) -> None:
    """Download a Drive file to a local directory.

    Handles three cases:
    1. Google Workspace files (Docs, Sheets, Slides, Drawings):
       exported to PDF/CSV via the export_media endpoint.
    2. Native binary files (PDF, images, Office docs, etc.):
       downloaded directly via get_media.
    3. Shortcuts / unsupported types: error with clear message.

    Prints a JSON result with the local file path so downstream skills
    (document-processor, rag-brain-manager) can chain directly.
    """
    # Fetch metadata
    try:
        meta = svc.files().get(
            fileId=file_id,
            fields="id, name, mimeType, size, modifiedTime",
        ).execute()
    except HttpError as exc:
        if exc.resp.status == 404:
            sys.exit(f"ERROR: File not found — id={file_id}")
        raise

    drive_name = meta.get("name", file_id)
    mime = meta.get("mimeType", "")
    size_bytes = int(meta.get("size", 0) or 0)

    log.info("Downloading '%s' (%s) …", drive_name, mime)

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Determine download strategy and output extension
    if mime in _WORKSPACE_EXPORT:
        export_mime, ext = _WORKSPACE_EXPORT[mime]
        base = _safe_filename(Path(drive_name).stem if "." in drive_name else drive_name)
        local_name = filename or (base + ext)
        local_path = os.path.join(output_dir, local_name)

        request = svc.files().export_media(fileId=file_id, mimeType=export_mime)
        log.info("Exporting Google Workspace file as %s → %s", export_mime, local_path)

    elif mime == "application/vnd.google-apps.folder":
        sys.exit("ERROR: Cannot download a folder. Use --action list-folder to browse it.")

    elif mime.startswith("application/vnd.google-apps."):
        # Unhandled Workspace type (Forms, Sites, etc.)
        sys.exit(
            f"ERROR: Unsupported Google Workspace type '{mime}'. "
            "Supported: Docs, Sheets, Slides, Drawings, Scripts."
        )
    else:
        # Native file — download as-is
        base = _safe_filename(drive_name)
        local_name = filename or base
        local_path = os.path.join(output_dir, local_name)

        request = svc.files().get_media(fileId=file_id)
        log.info("Downloading native file (%d bytes) → %s", size_bytes, local_path)

    # Stream the download
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    with open(local_path, "wb") as f:
        f.write(buffer.getvalue())

    final_size = os.path.getsize(local_path)
    log.info("Download complete: %d bytes → %s", final_size, local_path)

    print(json.dumps({
        "action": "download",
        "status": "success",
        "drive_file_id": file_id,
        "drive_name": drive_name,
        "local_path": local_path,
        "size_bytes": final_size,
        "mime_type": mime,
    }))


# ══════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Google Manager — Gmail, Calendar, and Drive (P.A.R.A.)",
    )
    parser.add_argument(
        "--service", required=True,
        choices=["gmail", "calendar", "drive"],
        help="Google service to use.",
    )
    parser.add_argument(
        "--action", required=True,
        help="Action to perform (varies per service).",
    )

    # Shared
    parser.add_argument("--limit", type=int, default=20, help="Max results.")

    # Gmail
    parser.add_argument("--to", help="Recipient email (gmail send).")
    parser.add_argument("--subject", help="Email subject (gmail send).")
    parser.add_argument("--body", help="Plain-text email body (legacy; prefer --body-markdown).")
    parser.add_argument(
        "--body-markdown", dest="body_markdown",
        help="Email body as Markdown — converted to HTML + plain-text fallback (recommended).",
    )

    # Calendar
    parser.add_argument("--time-min", help="Start time filter (calendar list). Use 'today' for today.")
    parser.add_argument("--summary", help="Event summary (calendar add/update).")
    parser.add_argument("--start", help="Event start ISO datetime (calendar add/update).")
    parser.add_argument("--duration", type=int, default=60, help="Event duration in minutes (default: 60).")
    parser.add_argument("--event-id", help="Event ID (calendar update).")
    parser.add_argument("--description", help="Event description (calendar update).")
    parser.add_argument("--location", help="Event location (calendar update).")

    # Drive
    parser.add_argument("--file-id", help="Drive file ID (drive organize / download).")
    parser.add_argument("--target-folder", help="Target folder path (drive organize).")
    parser.add_argument("--rename", help="New descriptive name — date prefix added automatically (drive organize).")
    parser.add_argument("--query", help="Search query (gmail search, drive find).")
    parser.add_argument("--folder", help="Folder name or ID (drive list-folder).")
    parser.add_argument(
        "--output-dir",
        default="/home/node/.openclaw/downloads",
        help="Local directory to save downloaded files (drive download). Default: ~/.openclaw/downloads",
    )
    parser.add_argument(
        "--filename",
        help="Override the local filename for drive download (optional).",
    )

    args = parser.parse_args()

    creds = _authenticate()

    # ── Gmail dispatch ────────────────────────────────────────────
    if args.service == "gmail":
        svc = _gmail(creds)

        if args.action == "triage":
            gmail_triage(svc, limit=args.limit)

        elif args.action == "search":
            if not args.query:
                parser.error("--query is required for gmail search")
            gmail_search(svc, args.query, limit=args.limit)

        elif args.action == "send":
            if not args.to:
                parser.error("--to is required for gmail send")
            if not args.subject:
                parser.error("--subject is required for gmail send")
            if not args.body_markdown and not args.body:
                parser.error("--body-markdown (or --body for plain text) is required for gmail send")
            gmail_send(
                svc, args.to, args.subject,
                body=args.body,
                body_markdown=args.body_markdown,
            )

        elif args.action == "create-labels":
            gmail_create_labels(svc)

        else:
            parser.error(
                f"Unknown gmail action: {args.action}. "
                "Choose from: triage, search, send, create-labels"
            )

    # ── Calendar dispatch ─────────────────────────────────────────
    elif args.service == "calendar":
        svc = _calendar(creds)

        if args.action == "list":
            calendar_list(svc, time_min=args.time_min)

        elif args.action == "add":
            if not args.summary:
                parser.error("--summary is required for calendar add")
            if not args.start:
                parser.error("--start is required for calendar add")
            calendar_add(svc, args.summary, args.start, args.duration)

        elif args.action == "update":
            if not args.event_id:
                parser.error("--event-id is required for calendar update")
            calendar_update(
                svc, args.event_id,
                summary=args.summary,
                start=args.start,
                duration=args.duration,
                description=args.description,
                location=args.location,
            )

        else:
            parser.error(
                f"Unknown calendar action: {args.action}. "
                "Choose from: list, add, update"
            )

    # ── Drive dispatch ────────────────────────────────────────────
    elif args.service == "drive":
        svc = _drive(creds)

        if args.action == "init-para":
            drive_init_para(svc)

        elif args.action == "organize":
            if not args.file_id:
                parser.error("--file-id is required for drive organize")
            drive_organize(
                svc, args.file_id,
                target_folder=args.target_folder,
                rename_desc=args.rename,
            )

        elif args.action == "find":
            if not args.query:
                parser.error("--query is required for drive find")
            drive_find(svc, args.query)

        elif args.action == "list-folder":
            if not args.folder:
                parser.error("--folder (name or ID) is required for drive list-folder")
            drive_list_folder(svc, args.folder, limit=args.limit)

        elif args.action == "overview":
            drive_overview(svc)

        elif args.action == "download":
            if not args.file_id:
                parser.error("--file-id is required for drive download")
            drive_download(svc, args.file_id, args.output_dir, filename=args.filename)

        else:
            parser.error(
                f"Unknown drive action: {args.action}. "
                "Choose from: init-para, organize, find, list-folder, overview, download"
            )


if __name__ == "__main__":
    main()
