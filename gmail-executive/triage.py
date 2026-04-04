#!/usr/bin/env python3
"""
gmail-executive — Executive Triage System (ETS) for Gmail.

Heavy-lifter pattern: all Gmail API logic lives here; the OpenCLAW agent
only triggers CLI commands.

`triage` and `triage-report` scan unread INBOX only (`labelIds`: INBOX + UNREAD) and
remove UNREAD when moving classified mail (mark read). See SKILL.md.

Environment variables (injected via Doppler):
    GOOGLE_TOKEN_JSON         OAuth2 token JSON (contents of token.json)
    GOOGLE_CREDENTIALS_JSON   OAuth2 client credentials JSON (contents of credentials.json)
    GMAIL_TOKEN_JSON          Legacy alias for GOOGLE_TOKEN_JSON
"""

from __future__ import annotations

import argparse
import base64
import html as html_module
import json
import logging
import os
import random
import re
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import markdown as md_lib
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# Add shared lib to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'lib'))
from google_clients import get_credentials

# ── Logging ──────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("gmail-executive")

# ── Constants ────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
]

ETS_LABELS = [
    "01_Action",
    "02_Waiting",
    "03_Read",
    "PARA/Projects",
    "PARA/Areas",
    "PARA/Resources",
    "PARA/Archives",
]

# Hardcoded triage rules: (field, pattern, target_label)
TRIAGE_RULES: list[tuple[str, str, str]] = [
    ("from", r"newsletter|digest|noreply|no-reply|unsubscribe", "03_Read"),
    ("subject", r"(?i)\b(invoice|receipt|payment|billing)\b", "PARA/Areas"),
]

BATCH_CHUNK_SIZE = 15
BATCH_DELAY_SECONDS = 1.0
MAX_RETRIES = 3
BASE_RETRY_DELAY = 2.0

# Gmail system label ids (users.messages.list ANDs multiple labelIds).
INBOX_LABEL_ID = "INBOX"
UNREAD_LABEL_ID = "UNREAD"

FORCED_CC_ADDRESS = "don@chimexhldg.com"
EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

_email_counters: dict[str, int] = {
    "agent_emails_sent_total": 0,
    "agent_email_format_errors": 0,
}

# ── Auth ─────────────────────────────────────────────────────────────


def _authenticate() -> Credentials:
    """Build Gmail credentials from environment variables."""
    # Support legacy GMAIL_TOKEN_JSON if GOOGLE_TOKEN_JSON is missing
    return get_credentials(SCOPES, token_json_env_vars=["GOOGLE_TOKEN_JSON", "GMAIL_TOKEN_JSON"])


def _service(creds: Credentials):
    """Build the Gmail API service."""
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ── Label helpers ────────────────────────────────────────────────────


def _get_all_labels(service) -> dict[str, str]:
    """Return a dict of {label_name: label_id} for all labels."""
    result = service.users().labels().list(userId="me").execute()
    labels = result.get("labels", [])
    return {lbl["name"]: lbl["id"] for lbl in labels}


def _ensure_label(service, name: str, existing: dict[str, str]) -> str:
    """Create a label if it doesn't exist. Return its ID."""
    if name in existing:
        return existing[name]

    body: dict[str, Any] = {
        "name": name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    created = service.users().labels().create(userId="me", body=body).execute()
    label_id = created["id"]
    log.info("Created label: %s (%s)", name, label_id)
    existing[name] = label_id
    return label_id


def expert_judgment(message: dict[str, Any]) -> str | None:
    """Classify message priority. Returns target label or None."""
    headers = {
        h["name"].lower(): h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }
    subject = headers.get("subject", "").lower()
    from_addr = headers.get("from", "").lower()

    # Urgent / action-required signals → 01_Action
    urgent_patterns = [
        r"\burgent\b", r"\bdeadline\b", r"\basap\b",
        r"\baction required\b", r"\bimmediate\b", r"\btime.?sensitive\b",
        r"\bsecurity alert\b", r"\bnew login\b", r"\bverify your account\b",
        r"\bconfidential\b",
    ]
    for pat in urgent_patterns:
        if re.search(pat, subject, re.IGNORECASE):
            return "01_Action"

    # Waiting-for signals → 02_Waiting
    waiting_patterns = [
        r"\bpending\b.*\b(approval|review)\b",
        r"\bawaiting\b", r"\bfollow.?up\b",
        r"\bdelivery\b", r"\btracking\b", r"\bshipped\b",
    ]
    for pat in waiting_patterns:
        if re.search(pat, subject, re.IGNORECASE):
            return "02_Waiting"

    # Financial signals → PARA/Areas
    financial_patterns = [
        r"\binvoice\b", r"\breceipt\b", r"\bpayment\b", r"\bbilling\b", r"\bstatement\b"
    ]
    for pat in financial_patterns:
        if re.search(pat, subject, re.IGNORECASE):
            return "PARA/Areas"

    # VIP signals → 01_Action
    vip_patterns = [
        r"\bceo\b", r"\bfounder\b", r"\bpresident\b", r"\bdirector\b",
        r"\bvp\b", r"\bboard\b",
    ]
    for pat in vip_patterns:
        if re.search(pat, subject, re.IGNORECASE) or re.search(pat, from_addr, re.IGNORECASE):
            return "01_Action"

    return None


# ── Body extraction ───────────────────────────────────────────────────


def _extract_body_text(payload: dict[str, Any]) -> str:
    """Recursively extract plain text from a Gmail MIME payload."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    parts = payload.get("parts", [])

    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    for part in parts:
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                raw_html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                return re.sub(r"<[^>]+>", " ", raw_html).strip()

    for part in parts:
        if part.get("mimeType", "").startswith("multipart/"):
            result = _extract_body_text(part)
            if result:
                return result

    return ""


# ── Actions ──────────────────────────────────────────────────────────


def init_labels(service) -> None:
    """Ensure all ETS labels exist using batch requests."""
    existing = _get_all_labels(service)
    created_count = 0
    batch = service.new_batch_http_request()

    def callback(request_id, response, exception):
        nonlocal created_count
        if exception:
            print(f"  ✗ Failed:  {request_id} — {exception}")
        else:
            print(f"  + Created: {request_id}")
            created_count += 1

    print("Executive Triage System — Label Initialization\n")
    pending = False
    for label_name in ETS_LABELS:
        if label_name in existing:
            print(f"  ✓ Exists:  {label_name}")
        else:
            body = {
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            }
            batch.add(
                service.users().labels().create(userId="me", body=body),
                callback=callback,
                request_id=label_name
            )
            pending = True

    if pending:
        batch.execute()

    print(f"\nDone. {created_count} label(s) created, "
          f"{len(ETS_LABELS) - created_count} already existed.")


def get_status(service) -> None:
    """Print unread counts for each ETS label and INBOX."""
    existing = _get_all_labels(service)

    check_labels = ["INBOX"] + ETS_LABELS
    print("Executive Triage System — Status\n")
    print(f"{'Label':<25} {'Unread':>8}")
    print("-" * 35)

    batch = service.new_batch_http_request()
    results = {}

    def callback(request_id, response, exception):
        if exception:
            results[request_id] = None
        else:
            results[request_id] = response

    for label_name in check_labels:
        label_id = existing.get(label_name, label_name)
        batch.add(
            service.users().labels().get(userId="me", id=label_id),
            callback=callback,
            request_id=label_name
        )

    batch.execute()

    for label_name in check_labels:
        info = results.get(label_name)
        if info is not None:
            unread = info.get("messagesUnread", 0)
            total = info.get("messagesTotal", 0)
            print(f"{label_name:<25} {unread:>8}  (total: {total})")
        else:
            print(f"{label_name:<25}      —   (label not found)")

    print()


def _chunked_batch_get(
    service,
    msg_stubs: list[dict],
    chunk_size: int = BATCH_CHUNK_SIZE,
    chunk_delay: float = BATCH_DELAY_SECONDS,
    show_progress: bool = False,
) -> list[dict[str, Any]]:
    """Fetch message details in chunked batches with retry on 429s."""
    full_messages: list[dict[str, Any]] = []
    total_chunks = (len(msg_stubs) + chunk_size - 1) // chunk_size

    for chunk_idx in range(0, len(msg_stubs), chunk_size):
        chunk = msg_stubs[chunk_idx : chunk_idx + chunk_size]
        chunk_num = chunk_idx // chunk_size + 1

        if show_progress:
            print(f"  Fetching chunk {chunk_num}/{total_chunks} "
                  f"({chunk_idx + len(chunk)}/{len(msg_stubs)} messages)…")

        succeeded: list[dict] = []
        failed_ids: list[dict] = list(chunk)

        for attempt in range(MAX_RETRIES + 1):
            if not failed_ids:
                break

            if attempt > 0:
                delay = BASE_RETRY_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1)
                log.info("Retry %d/%d for %d failed messages (waiting %.1fs)…",
                         attempt, MAX_RETRIES, len(failed_ids), delay)
                time.sleep(delay)

            retry_ids: list[dict] = []
            batch = service.new_batch_http_request()

            def make_callback(stub):
                def callback(request_id, response, exception):
                    if exception:
                        status = getattr(exception, "status_code", None) or getattr(exception, "resp", {}).get("status")
                        if str(status) == "429":
                            retry_ids.append(stub)
                        else:
                            log.warning("Failed to fetch message %s: %s", request_id, exception)
                    else:
                        succeeded.append(response)
                return callback

            for stub in failed_ids:
                batch.add(
                    service.users().messages().get(
                        userId="me", id=stub["id"], format="metadata",
                        metadataHeaders=["From", "Subject"]
                    ),
                    callback=make_callback(stub),
                    request_id=stub["id"]
                )
            batch.execute()
            failed_ids = retry_ids

        if failed_ids:
            log.error("Gave up on %d message(s) after %d retries.", len(failed_ids), MAX_RETRIES)

        full_messages.extend(succeeded)

        if chunk_idx + chunk_size < len(msg_stubs):
            time.sleep(chunk_delay)

    return full_messages


def triage(
    service,
    limit: int = 50,
    batch_size: int = BATCH_CHUNK_SIZE,
    batch_delay: float = BATCH_DELAY_SECONDS,
) -> None:
    """Fetch **unread** INBOX messages and apply triage rules using chunked batch requests.

    Executive directive: only unread mail is scanned; each message that is classified and
    moved out of INBOX is also marked read (UNREAD label removed).
    """
    existing = _get_all_labels(service)

    # Pre-resolve target label IDs
    label_ids: dict[str, str] = {}
    for _, _, target in TRIAGE_RULES:
        if target not in label_ids:
            label_ids[target] = _ensure_label(service, target, existing)

    inbox_id = INBOX_LABEL_ID

    # Unread only: Gmail ANDs labelIds — require both INBOX and UNREAD.
    results = service.users().messages().list(
        userId="me",
        labelIds=[inbox_id, UNREAD_LABEL_ID],
        maxResults=limit,
    ).execute()
    messages = results.get("messages", [])

    if not messages:
        print("No unread messages in INBOX — nothing to triage.")
        return

    print(f"Executive Triage System — Triage Run\n")
    print(f"Scanning {len(messages)} unread message(s) in INBOX…\n")

    show_progress = len(messages) > 30
    # Logic Hardening: Batch API requests to minimize HTTP overhead.
    full_messages = _chunked_batch_get(
        service, messages,
        chunk_size=batch_size,
        chunk_delay=batch_delay,
        show_progress=show_progress,
    )

    # Apply rules
    moved: dict[str, int] = {}
    skipped = 0
    # Map target_label -> list of message IDs
    moves: dict[str, list[str]] = {}

    for msg in full_messages:
        # Expert judgment (priority classification)
        ej_label = expert_judgment(msg)
        if ej_label:
            if ej_label not in label_ids:
                label_ids[ej_label] = _ensure_label(service, ej_label, existing)
            if ej_label not in moves:
                moves[ej_label] = []
            moves[ej_label].append(msg["id"])
            moved[ej_label] = moved.get(ej_label, 0) + 1
            continue

        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        from_addr = headers.get("from", "")
        subject = headers.get("subject", "")

        matched = False
        for field, pattern, target_label in TRIAGE_RULES:
            value = from_addr if field == "from" else subject
            if re.search(pattern, value, re.IGNORECASE):
                if target_label not in moves:
                    moves[target_label] = []
                moves[target_label].append(msg["id"])

                moved[target_label] = moved.get(target_label, 0) + 1
                matched = True
                break

        if not matched:
            skipped += 1

    # Batch modify (one batch request per target label group)
    if moves:
        batch_mod = service.new_batch_http_request()

        def mod_callback(request_id, response, exception):
            if exception:
                log.error("Failed to batch modify for label %s: %s", request_id, exception)

        for target_label, msg_ids in moves.items():
            target_id = label_ids[target_label]
            batch_mod.add(
                service.users().messages().batchModify(
                    userId="me",
                    body={
                        "ids": msg_ids,
                        "addLabelIds": [target_id],
                        # Mark as read + remove from inbox when triaged (directive).
                        "removeLabelIds": [inbox_id, UNREAD_LABEL_ID],
                    }
                ),
                callback=mod_callback,
                request_id=target_label
            )
        batch_mod.execute()

    # Summary
    total_moved = sum(moved.values())
    print(f"{'Label':<25} {'Moved':>8}")
    print("-" * 35)
    for label, count in sorted(moved.items()):
        print(f"{label:<25} {count:>8}")
    print("-" * 35)
    print(f"{'Total moved':<25} {total_moved:>8}")
    print(f"{'Remained in INBOX':<25} {skipped:>8}")
    print()


def triage_report(
    service,
    limit: int = 15,
    batch_size: int = BATCH_CHUNK_SIZE,
    batch_delay: float = BATCH_DELAY_SECONDS,
) -> None:
    """Run triage and output a structured JSON report with email content.

    Scans **unread** INBOX only; same classification and label moves as triage(),
    including marking triaged messages read. Then fetches full message bodies for
    high-importance emails and outputs JSON for the executive / cron pipeline.
    """
    existing = _get_all_labels(service)

    label_ids: dict[str, str] = {}
    for _, _, target in TRIAGE_RULES:
        if target not in label_ids:
            label_ids[target] = _ensure_label(service, target, existing)

    inbox_id = INBOX_LABEL_ID

    results = service.users().messages().list(
        userId="me",
        labelIds=[inbox_id, UNREAD_LABEL_ID],
        maxResults=limit,
    ).execute()
    messages = results.get("messages", [])

    if not messages:
        print(json.dumps({
            "summary": {"total_processed": 0, "moved": {}, "remained_inbox": 0},
            "emails": [],
        }, indent=2))
        return

    log.info("triage-report: scanning %d message(s)", len(messages))

    full_messages = _chunked_batch_get(
        service, messages,
        chunk_size=batch_size, chunk_delay=batch_delay,
    )

    # --- Classify each message (same logic as triage) ---
    moved: dict[str, int] = {}
    moves: dict[str, list[str]] = {}
    email_records: list[dict[str, Any]] = []

    for msg in full_messages:
        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        from_addr = headers.get("from", "")
        subject = headers.get("subject", "")
        snippet = msg.get("snippet", "")

        ej_label = expert_judgment(msg)
        if ej_label:
            if ej_label not in label_ids:
                label_ids[ej_label] = _ensure_label(service, ej_label, existing)
            moves.setdefault(ej_label, []).append(msg["id"])
            moved[ej_label] = moved.get(ej_label, 0) + 1
            email_records.append({
                "id": msg["id"],
                "from": from_addr,
                "subject": subject,
                "snippet": snippet,
                "label": ej_label,
                "importance": "high" if ej_label in ("01_Action", "PARA/Areas") else "medium",
                "classification": "expert_judgment",
            })
            continue

        matched = False
        for field, pattern, target_label in TRIAGE_RULES:
            value = from_addr if field == "from" else subject
            if re.search(pattern, value, re.IGNORECASE):
                moves.setdefault(target_label, []).append(msg["id"])
                moved[target_label] = moved.get(target_label, 0) + 1
                email_records.append({
                    "id": msg["id"],
                    "from": from_addr,
                    "subject": subject,
                    "snippet": snippet,
                    "label": target_label,
                    "importance": "low",
                    "classification": "rule",
                })
                matched = True
                break

        if not matched:
            email_records.append({
                "id": msg["id"],
                "from": from_addr,
                "subject": subject,
                "snippet": snippet,
                "label": "INBOX",
                "importance": "normal",
                "classification": "unmatched",
            })

    # --- Execute label moves (same as triage) ---
    if moves:
        batch_mod = service.new_batch_http_request()

        def mod_callback(request_id, response, exception):
            if exception:
                log.error("Batch modify failed for %s: %s", request_id, exception)

        for target_label, msg_ids in moves.items():
            target_id = label_ids[target_label]
            batch_mod.add(
                service.users().messages().batchModify(
                    userId="me",
                    body={
                        "ids": msg_ids,
                        "addLabelIds": [target_id],
                        "removeLabelIds": [inbox_id, UNREAD_LABEL_ID],
                    }
                ),
                callback=mod_callback,
                request_id=target_label,
            )
        batch_mod.execute()

    # --- Fetch full bodies for high-importance emails ---
    important = [r for r in email_records if r["importance"] == "high"]
    if important:
        stubs = [{"id": r["id"]} for r in important]

        full_bodies: list[dict[str, Any]] = []
        batch = service.new_batch_http_request()

        def body_callback(request_id, response, exception):
            if exception:
                log.warning("Failed to fetch body for %s: %s", request_id, exception)
            else:
                full_bodies.append(response)

        for stub in stubs:
            batch.add(
                service.users().messages().get(
                    userId="me", id=stub["id"], format="full",
                ),
                callback=body_callback,
                request_id=stub["id"],
            )
        batch.execute()

        body_map = {
            m["id"]: _extract_body_text(m.get("payload", {}))[:3000]
            for m in full_bodies
        }
        for record in email_records:
            if record["id"] in body_map:
                record["body_preview"] = body_map[record["id"]]

    skipped = sum(1 for r in email_records if r["label"] == "INBOX")

    report = {
        "summary": {
            "total_processed": len(full_messages),
            "moved": moved,
            "remained_inbox": skipped,
        },
        "emails": email_records,
    }
    print(json.dumps(report, indent=2))


def draft_reply(service, thread_id: str, body_text: str) -> None:
    """Create a draft reply on a given thread."""
    # Get the original message to extract headers
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["From", "Subject", "Message-ID"],
    ).execute()

    messages = thread.get("messages", [])
    if not messages:
        sys.exit(f"ERROR: Thread {thread_id} has no messages.")

    last_msg = messages[-1]
    headers = {
        h["name"].lower(): h["value"]
        for h in last_msg.get("payload", {}).get("headers", [])
    }

    reply_to = headers.get("from", "")
    subject = headers.get("subject", "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    message_id = headers.get("message-id", "")

    mime = MIMEText(body_text)
    mime["to"] = reply_to
    mime["subject"] = subject
    if message_id:
        mime["In-Reply-To"] = message_id
        mime["References"] = message_id

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")

    draft_body = {
        "message": {
            "raw": raw,
            "threadId": thread_id,
        }
    }

    draft = service.users().drafts().create(userId="me", body=draft_body).execute()
    print(f"Draft created successfully.")
    print(f"  Draft ID:  {draft['id']}")
    print(f"  Thread:    {thread_id}")
    print(f"  To:        {reply_to}")
    print(f"  Subject:   {subject}")


def _validate_email(address: str) -> bool:
    """Return True if *address* looks like a syntactically valid email."""
    return bool(EMAIL_RE.match(address.strip()))


def _markdown_to_html(md_text: str) -> str:
    """Convert Markdown to HTML.

    Any raw HTML in the source is escaped first to prevent XSS and enforce
    Markdown-only authoring by agents (REQ-EMAIL-001, REQ-EMAIL-005).
    """
    sanitized = html_module.escape(md_text)
    return md_lib.markdown(sanitized, extensions=["nl2br", "tables", "fenced_code"])


def _markdown_to_plaintext(md_text: str) -> str:
    """Strip Markdown syntax to produce a clean plain-text fallback (REQ-EMAIL-002)."""
    text = md_text
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}(.+?)_{1,3}", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^\s*[*+]\s+", "- ", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


def _inject_forced_cc(cc: list[str]) -> list[str]:
    """Ensure FORCED_CC_ADDRESS is present in *cc* (case-insensitive dedup)."""
    normalised = {addr.strip().lower() for addr in cc}
    if FORCED_CC_ADDRESS.lower() not in normalised:
        cc = list(cc) + [FORCED_CC_ADDRESS]
    return cc


def send_email(
    service,
    to: list[str],
    subject: str,
    body_markdown: str,
    cc: list[str] | None = None,
    _quiet: bool = False,
) -> dict[str, Any]:
    """Compose and send an email.

    Accepts Markdown in *body_markdown*; the execution layer converts it to
    a multipart/alternative payload (text/plain + text/html).
    don@chimexhldg.com is always CC'd regardless of agent input.
    """
    agent_name = os.environ.get("OPENCLAW_AGENT_NAME", "unknown")

    if not to:
        _email_counters["agent_email_format_errors"] += 1
        log.error("[%s] send_email: no recipients provided", agent_name)
        return {
            "status": "error",
            "error_code": "MISSING_RECIPIENT",
            "message": "At least one recipient (to) is required.",
        }

    invalid_to = [a for a in to if not _validate_email(a)]
    if invalid_to:
        _email_counters["agent_email_format_errors"] += 1
        log.error("[%s] send_email: invalid 'to' addresses: %s", agent_name, invalid_to)
        return {
            "status": "error",
            "error_code": "INVALID_EMAIL_FORMAT",
            "message": f"Invalid email address(es): {', '.join(invalid_to)}",
        }

    cc = list(cc or [])
    invalid_cc = [a for a in cc if not _validate_email(a)]
    if invalid_cc:
        _email_counters["agent_email_format_errors"] += 1
        log.error("[%s] send_email: invalid 'cc' addresses: %s", agent_name, invalid_cc)
        return {
            "status": "error",
            "error_code": "INVALID_EMAIL_FORMAT",
            "message": f"Invalid CC address(es): {', '.join(invalid_cc)}",
        }

    cc = _inject_forced_cc(cc)

    html_body = _markdown_to_html(body_markdown)
    plain_body = _markdown_to_plaintext(body_markdown)

    mime = MIMEMultipart("alternative")
    mime["to"] = ", ".join(to)
    mime["cc"] = ", ".join(cc)
    mime["subject"] = subject
    mime.attach(MIMEText(plain_body, "plain"))
    mime.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")

    try:
        result = service.users().messages().send(
            userId="me", body={"raw": raw},
        ).execute()
    except Exception as exc:
        _email_counters["agent_email_format_errors"] += 1
        log.error("[%s] send_email transport error: %s", agent_name, exc)
        return {
            "status": "error",
            "error_code": "TRANSPORT_ERROR",
            "message": f"Failed to send email: {exc}",
        }

    message_id = result.get("id", "—")
    _email_counters["agent_emails_sent_total"] += 1

    log.info(
        "[%s] Email sent — to=%s cc=%s subject=%r message_id=%s",
        agent_name, to, cc, subject, message_id,
    )

    response: dict[str, Any] = {
        "status": "success",
        "message": f"Email sent successfully to {to} and cc'd to {cc}.",
        "message_id": message_id,
    }
    if not _quiet:
        print(json.dumps(response, indent=2))
    return response


def list_labels(service) -> None:
    """List all Gmail labels with unread counts."""
    labels = _get_all_labels(service)
    print(f"Gmail Labels ({len(labels)} total)\n")
    print(f"{'Label':<40} {'ID'}")
    print("-" * 70)
    for name in sorted(labels.keys()):
        print(f"{name:<40} {labels[name]}")
    print()


def digest(service) -> None:
    """List unread messages in 01_Action and 03_Read."""
    existing = _get_all_labels(service)
    digest_labels = ["01_Action", "03_Read"]

    print("Executive Triage System — Digest\n")

    # ⚡ Bolt: Use a single batch request to prevent N+1 queries when fetching unread messages in digest.
    batch = service.new_batch_http_request()
    batch_results = {}

    def callback(request_id, response, exception):
        if exception:
            batch_results[request_id] = None
        else:
            batch_results[request_id] = response

    valid_labels = []
    for label_name in digest_labels:
        label_id = existing.get(label_name)
        if not label_id:
            print(f"[{label_name}]  — label not found (run init first)\n")
            continue

        valid_labels.append(label_name)
        batch.add(
            service.users().messages().list(
                userId="me", labelIds=[label_id], q="is:unread", maxResults=25
            ),
            callback=callback,
            request_id=label_name
        )

    if valid_labels:
        batch.execute()

    # ⚡ Bolt: Gather all unique message IDs across all labels to fetch them in one go
    all_message_stubs = []
    seen_message_ids = set()

    for label_name in valid_labels:
        results = batch_results.get(label_name)
        messages = results.get("messages", []) if results else []
        for msg_stub in messages:
            if msg_stub["id"] not in seen_message_ids:
                seen_message_ids.add(msg_stub["id"])
                all_message_stubs.append(msg_stub)

    # Fetch all deduplicated messages at once
    msg_map = {}
    if all_message_stubs:
        full_msgs = _chunked_batch_get(service, all_message_stubs)
        msg_map = {m["id"]: m for m in full_msgs}

    for label_name in valid_labels:
        results = batch_results.get(label_name)
        messages = results.get("messages", []) if results else []

        print(f"[{label_name}]  {len(messages)} unread message(s)")
        if not messages:
            print()
            continue

        print(f"  {'#':<4} {'From':<35} {'Subject'}")
        print(f"  {'-' * 80}")

        for i, msg_stub in enumerate(messages, 1):
            msg = msg_map.get(msg_stub["id"])
            if not msg:
                continue

            headers = {
                h["name"].lower(): h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            from_addr = headers.get("from", "—")
            # Truncate long From fields
            if len(from_addr) > 33:
                from_addr = from_addr[:30] + "…"
            subject = headers.get("subject", "(no subject)")
            print(f"  {i:<4} {from_addr:<35} {subject}")

        print()


# ── CLI ──────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gmail Executive Triage System (ETS)",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=["init", "status", "triage", "triage-report", "draft", "send", "labels", "digest"],
        help="Action to perform.",
    )
    parser.add_argument("--limit", type=int, default=15, help="Max messages to triage (default: 15; use 10 for cron to smooth TPM).")
    parser.add_argument("--batch-size", type=int, default=BATCH_CHUNK_SIZE,
                        help=f"Messages per batch chunk (default: {BATCH_CHUNK_SIZE}).")
    parser.add_argument("--batch-delay", type=float, default=BATCH_DELAY_SECONDS,
                        help=f"Seconds between batch chunks (default: {BATCH_DELAY_SECONDS}).")
    parser.add_argument("--thread-id", help="Thread ID for draft reply.")
    parser.add_argument("--to", help="Recipient email(s), comma-separated.")
    parser.add_argument("--cc", help="CC email(s), comma-separated (optional). don@chimexhldg.com is always added.")
    parser.add_argument("--subject", help="Email subject (send/draft).")
    parser.add_argument("--body-markdown", dest="body_markdown",
                        help="Email body in Markdown format for send. Do not use HTML.")
    parser.add_argument("--body", help="Plain-text body for draft reply (or legacy send fallback).")

    args = parser.parse_args()

    creds = _authenticate()
    service = _service(creds)

    if args.action == "init":
        init_labels(service)

    elif args.action == "status":
        get_status(service)

    elif args.action == "triage":
        triage(service, limit=args.limit,
               batch_size=args.batch_size, batch_delay=args.batch_delay)

    elif args.action == "triage-report":
        triage_report(service, limit=args.limit,
                      batch_size=args.batch_size, batch_delay=args.batch_delay)

    elif args.action == "draft":
        if not args.thread_id:
            parser.error("--thread-id is required for draft")
        if not args.body:
            parser.error("--body is required for draft")
        draft_reply(service, args.thread_id, args.body)

    elif args.action == "send":
        if not args.to:
            parser.error("--to is required for send")
        if not args.subject:
            parser.error("--subject is required for send")
        body = args.body_markdown or args.body
        if not body:
            parser.error("--body-markdown is required for send")
        to_list = [a.strip() for a in args.to.split(",") if a.strip()]
        cc_list = [a.strip() for a in (args.cc or "").split(",") if a.strip()]
        result = send_email(service, to_list, args.subject, body, cc=cc_list)
        if result["status"] == "error":
            sys.exit(1)

    elif args.action == "labels":
        list_labels(service)

    elif args.action == "digest":
        digest(service)


if __name__ == "__main__":
    main()
