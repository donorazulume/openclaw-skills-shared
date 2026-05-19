#!/usr/bin/env python3
"""
email_ops — EMAIL-OPS-001: Autonomous Agent Email Management & HITL Approval.

Provides:
  - Token-optimized email ingestion (HTML strip, signature/quote removal)
  - EmailTransaction state machine with persistent JSON storage
  - Domain classification (internal vs external) for outbound gating
  - Human-in-the-Loop approval via Mattermost interactive messages
  - Auto-responder loop detection and quarantine
  - Telemetry counters and PII-redacted logging

Environment variables:
    OPENCLAW_AGENT_NAME        Agent identifier (roho | amara)
    MATTERMOST_URL             Mattermost server base URL
    MATTERMOST_BOT_TOKEN       Bot token for posting approval requests
    MATTERMOST_WEBHOOK_SECRET  Shared secret for approval callback HMAC
    EMAIL_APPROVAL_CHANNEL     Channel for approval requests (default: alerts)
    EMAIL_WHITELIST_DOMAINS    Comma-separated external domains to auto-approve
    EMAIL_WHITELIST_ADDRESSES  Comma-separated external addresses to auto-approve
"""

from __future__ import annotations

import argparse
import hashlib
import hmac as hmac_mod
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests

# Resolve paths for sibling imports
_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SKILL_DIR)
sys.path.append(os.path.join(_SKILL_DIR, "..", "lib"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("email-ops")

# ── Constants ────────────────────────────────────────────────────────

INTERNAL_DOMAIN = os.environ.get("CHIMEX_DOMAIN", "chimexhldg.com")
AGENT_NAME = os.environ.get("OPENCLAW_AGENT_NAME", "roho")

DATA_DIR = Path(os.environ.get("OPENCLAW_DATA_DIR", "/home/node/.openclaw"))
TRANSACTIONS_FILE = DATA_DIR / "email_transactions.json"

MM_URL = os.environ.get("MATTERMOST_URL", "http://mattermost:8065")
MM_TOKEN = os.environ.get("MATTERMOST_BOT_TOKEN", "")
MM_WEBHOOK_SECRET = os.environ.get("MATTERMOST_WEBHOOK_SECRET", "")
APPROVAL_CHANNEL = os.environ.get("EMAIL_APPROVAL_CHANNEL", "alerts")
APPROVAL_CALLBACK_BASE = os.environ.get(
    "EMAIL_APPROVAL_CALLBACK_URL", "http://mm-webhook-relay:8066",
)

WHITELIST_DOMAINS: set[str] = set(
    filter(None, os.environ.get("EMAIL_WHITELIST_DOMAINS", "").lower().split(","))
)
WHITELIST_ADDRESSES: set[str] = set(
    filter(None, os.environ.get("EMAIL_WHITELIST_ADDRESSES", "").lower().split(","))
)

LOOP_THRESHOLD = int(os.environ.get("EMAIL_LOOP_THRESHOLD", "5"))
LOOP_WINDOW_SECONDS = int(os.environ.get("EMAIL_LOOP_WINDOW_SECONDS", "3600"))

VALID_TRANSITIONS: dict[str, set[str]] = {
    "RECEIVED":         {"DRAFTED", "QUARANTINED"},
    "DRAFTED":          {"PENDING_APPROVAL", "APPROVED", "QUARANTINED"},
    "PENDING_APPROVAL": {"APPROVED", "REJECTED"},
    "APPROVED":         {"SENT"},
    "REJECTED":         set(),
    "SENT":             set(),
    "QUARANTINED":      set(),
}

_counters: dict[str, int] = {
    "agent.email.ingested": 0,
    "agent.email.sent": 0,
    "agent.email.sent.internal": 0,
    "agent.email.sent.external": 0,
    "agent.email.tokens_saved": 0,
}

# ── HTML → Plain-text extractor ──────────────────────────────────────


class _HTMLTextExtractor(HTMLParser):
    """Strips all HTML tags, keeping visible text content."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self._skip = True
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head"):
            self._skip = False
        elif tag in ("p", "div", "tr", "table"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def strip_html(html_body: str) -> str:
    """Convert an HTML email body to plain text."""
    extractor = _HTMLTextExtractor()
    extractor.feed(html_body)
    return extractor.get_text()


# ── Email pre-processing ─────────────────────────────────────────────

_SIG_PATTERNS = [
    re.compile(r"^-- ?\n.*", re.DOTALL | re.MULTILINE),
    re.compile(r"^Sent from my (?:iPhone|iPad|Galaxy|Android).*", re.MULTILINE),
    re.compile(r"^Get Outlook for .*", re.MULTILINE),
    re.compile(r"^_{10,}.*", re.DOTALL | re.MULTILINE),
    re.compile(r"^-{10,}.*", re.DOTALL | re.MULTILINE),
]

_BLOCK_QUOTE_START = re.compile(r"^On .{10,80} wrote:\s*$", re.MULTILINE)
_ORIGINAL_MSG = re.compile(
    r"^-{4,}\s*(?:Original Message|Forwarded message)\s*-{4,}.*",
    re.DOTALL | re.MULTILINE,
)
_OUTLOOK_HEADER = re.compile(
    r"^From:\s+.+\nSent:\s+.+\nTo:\s+.+\n(?:Cc:\s+.+\n)?Subject:\s+.+",
    re.MULTILINE,
)
_LINE_QUOTE = re.compile(r"^>+ ?", re.MULTILINE)


def strip_signatures(text: str) -> str:
    """Remove common email signatures from *text*."""
    for pat in _SIG_PATTERNS:
        text = pat.sub("", text)
    return text.strip()


def strip_quoted_threads(text: str) -> str:
    """Remove quoted reply chains and forwarded-message headers."""
    for pat in (_BLOCK_QUOTE_START, _ORIGINAL_MSG, _OUTLOOK_HEADER):
        m = pat.search(text)
        if m:
            return text[: m.start()].strip()

    lines = text.split("\n")
    cleaned = [ln for ln in lines if not _LINE_QUOTE.match(ln)]
    return "\n".join(cleaned).strip()


def preprocess_email(
    raw_body: str, content_type: str = "text/plain",
) -> tuple[str, int, int]:
    """Full preprocessing pipeline.

    Returns *(cleaned_text, raw_token_estimate, clean_token_estimate)*.
    Token estimate: ~4 chars per token (rough GPT/Gemini heuristic).
    """
    raw_tokens = len(raw_body) // 4

    text = strip_html(raw_body) if "html" in content_type.lower() else raw_body
    text = strip_signatures(text)
    text = strip_quoted_threads(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    clean_tokens = len(text) // 4
    return text, raw_tokens, clean_tokens


# ── PII redaction (for logging only) ─────────────────────────────────

_PII_PATTERNS = [
    (re.compile(r"\+\d{1,3}\s?\d{6,14}"), "[PHONE]"),
    (re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"), "[PHONE]"),
]


def redact_pii(text: str) -> str:
    for pat, repl in _PII_PATTERNS:
        text = pat.sub(repl, text)
    return text


# ── Transaction persistence ──────────────────────────────────────────


def _load_transactions() -> dict[str, Any]:
    if not TRANSACTIONS_FILE.exists():
        return {"transactions": {}}
    try:
        return json.loads(TRANSACTIONS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to load transactions file: %s", exc)
        return {"transactions": {}}


def _save_transactions(data: dict[str, Any]) -> None:
    TRANSACTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRANSACTIONS_FILE.write_text(json.dumps(data, indent=2, default=str))


def create_transaction(
    agent_id: str,
    thread_id: str,
    direction: str,
    status: str,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    subject: str = "",
    body_markdown: str = "",
) -> dict[str, Any]:
    txn_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    txn: dict[str, Any] = {
        "transaction_id": txn_id,
        "agent_id": agent_id,
        "thread_id": thread_id,
        "direction": direction,
        "status": status,
        "to": to or [],
        "cc": cc or [],
        "subject": subject,
        "body_markdown": body_markdown,
        "created_at": now,
        "updated_at": now,
        "token_usage": len(body_markdown) // 4,
        "approval_user": None,
        "rejection_reason": None,
        "history": [{"status": status, "timestamp": now, "actor": "system"}],
    }

    data = _load_transactions()
    data["transactions"][txn_id] = txn
    _save_transactions(data)

    log.info(
        "[%s] txn created: %s status=%s direction=%s to=%s",
        agent_id, txn_id, status, direction, redact_pii(str(to)),
    )
    return txn


def update_transaction(
    txn_id: str, new_status: str, actor: str = "system", **kwargs: Any,
) -> dict[str, Any]:
    data = _load_transactions()
    txn = data["transactions"].get(txn_id)
    if not txn:
        raise ValueError(f"Transaction {txn_id} not found")

    current = txn["status"]
    if new_status not in VALID_TRANSITIONS.get(current, set()):
        raise ValueError(f"Invalid transition: {current} → {new_status}")

    now = datetime.now(timezone.utc).isoformat()
    txn["status"] = new_status
    txn["updated_at"] = now
    txn["history"].append({"status": new_status, "timestamp": now, "actor": actor})
    for key, val in kwargs.items():
        if key in txn:
            txn[key] = val

    data["transactions"][txn_id] = txn
    _save_transactions(data)

    log.info("[%s] txn %s: %s → %s (actor=%s)", txn["agent_id"], txn_id, current, new_status, actor)
    return txn


# ── Domain classification ────────────────────────────────────────────


def classify_recipients(addresses: list[str]) -> str:
    """Return ``'internal'`` if every address is @chimexhldg.com, else ``'external'``."""
    for addr in addresses:
        domain = addr.strip().lower().rsplit("@", 1)[-1]
        if domain != INTERNAL_DOMAIN:
            return "external"
    return "internal"


def is_whitelisted(addresses: list[str]) -> bool:
    """Return True if every *external* address is whitelisted."""
    for addr in addresses:
        lower = addr.strip().lower()
        domain = lower.rsplit("@", 1)[-1]
        if domain == INTERNAL_DOMAIN:
            continue
        if lower in WHITELIST_ADDRESSES:
            continue
        if domain in WHITELIST_DOMAINS:
            continue
        return False
    return True


# ── Loop detection ───────────────────────────────────────────────────


def check_loop(thread_id: str) -> bool:
    """Return True if ≥ LOOP_THRESHOLD outbound emails were sent to *thread_id* within the window."""
    data = _load_transactions()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=LOOP_WINDOW_SECONDS)).isoformat()
    count = sum(
        1 for t in data["transactions"].values()
        if t["thread_id"] == thread_id
        and t["direction"] == "OUTBOUND"
        and t["created_at"] > cutoff
        and t["status"] in ("SENT", "APPROVED", "DRAFTED", "PENDING_APPROVAL")
    )
    return count >= LOOP_THRESHOLD


# ── HMAC approval tokens ────────────────────────────────────────────


def make_approval_token(txn_id: str) -> str:
    secret = MM_WEBHOOK_SECRET or "default-secret"
    return hmac_mod.new(secret.encode(), txn_id.encode(), hashlib.sha256).hexdigest()[:32]


def verify_approval_token(txn_id: str, token: str) -> bool:
    return hmac_mod.compare_digest(make_approval_token(txn_id), token)


# ── Mattermost helpers ───────────────────────────────────────────────


def _mm_api(method: str, path: str, payload: dict | None = None) -> dict[str, Any]:
    url = f"{MM_URL}/api/v4{path}"
    try:
        resp = requests.request(
            method, url,
            headers={"Authorization": f"Bearer {MM_TOKEN}", "Content-Type": "application/json"},
            json=payload, timeout=10,
        )
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:500]}
        return resp.json() if resp.text else {"status": "ok"}
    except Exception as exc:
        log.error("Mattermost API error: %s", exc)
        return {"error": "connection_failed", "detail": str(exc)}


_mm_channel_cache: dict[str, str] = {}


def _resolve_mm_channel(name: str) -> str | None:
    if name in _mm_channel_cache:
        return _mm_channel_cache[name]
    me = _mm_api("GET", "/users/me")
    if "error" in me:
        return None
    teams = _mm_api("GET", f"/users/{me['id']}/teams")
    if isinstance(teams, dict) and "error" in teams:
        return None
    for team in (teams if isinstance(teams, list) else []):
        ch = _mm_api("GET", f"/teams/{team['id']}/channels/name/{name}")
        if isinstance(ch, dict) and ch.get("id"):
            _mm_channel_cache[name] = ch["id"]
            return ch["id"]
    return None


def post_approval_request(txn: dict[str, Any]) -> dict[str, Any]:
    """Post an interactive Approve / Reject message to Mattermost."""
    channel_id = _resolve_mm_channel(APPROVAL_CHANNEL)
    if not channel_id:
        log.error("Cannot resolve MM channel: %s", APPROVAL_CHANNEL)
        return {"error": "channel_not_found"}

    txn_id = txn["transaction_id"]
    token = make_approval_token(txn_id)
    callback = f"{APPROVAL_CALLBACK_BASE}/api/email-approval"

    body_preview = txn["body_markdown"][:500]
    if len(txn["body_markdown"]) > 500:
        body_preview += "…"

    pretext = (
        f":email: **Email Approval Required** — `{txn['agent_id']}`\n\n"
        f"**To:** {', '.join(txn['to'])}\n"
        f"**Subject:** {txn['subject']}\n"
        f"**Transaction:** `{txn_id}`\n\n"
        f"---\n{body_preview}\n---"
    )

    payload: dict[str, Any] = {
        "channel_id": channel_id,
        "message": "",
        "props": {
            "attachments": [{
                "fallback": f"Email approval: {txn['subject']}",
                "color": "#FFA500",
                "pretext": pretext,
                "actions": [
                    {
                        "id": "approve",
                        "name": "Approve & Send",
                        "integration": {
                            "url": callback,
                            "context": {"transaction_id": txn_id, "action": "approve", "token": token},
                        },
                        "style": "good",
                    },
                    {
                        "id": "reject",
                        "name": "Reject",
                        "integration": {
                            "url": callback,
                            "context": {"transaction_id": txn_id, "action": "reject", "token": token},
                        },
                        "style": "danger",
                    },
                ],
            }],
        },
    }

    result = _mm_api("POST", "/posts", payload)
    if "error" not in result:
        log.info("[%s] Approval posted to #%s for txn %s", txn["agent_id"], APPROVAL_CHANNEL, txn_id)
    return result


# Body decoding is now handled by openclaw-mcp-google's google_mail_read tool,
# which returns plaintext (or HTML stripped to plaintext) for the message body.
# The legacy Gmail MIME walker that lived here has been removed (#323/#324).


# ── Ingestion ────────────────────────────────────────────────────────


def ingest_emails(limit: int = 10) -> list[dict[str, Any]]:
    """Fetch, preprocess, and structure new INBOX emails for agent consumption.

    Reads from openclaw-mcp-google over HTTP (SPEC-GAUTH-001 revised / #323) —
    no Gmail credentials are minted in this process. The high-water mark is kept
    by Gmail message ID instead of historyId because the MCP search tool returns
    only stub fields; messages already seen in a previous run are skipped.
    """
    import mcp_google  # local import; gateway-only env

    hwm_file = DATA_DIR / "email_hwm.json"
    hwm: dict[str, Any] = {}
    if hwm_file.exists():
        try:
            hwm = json.loads(hwm_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    seen_ids: set[str] = set(hwm.get("seen_message_ids") or [])

    try:
        search_body = mcp_google.call(
            "google_mail_search",
            {"query": "in:inbox", "max_results": limit},
        )
    except mcp_google.GoogleMCPError as exc:
        print(json.dumps({"status": "error", "error_code": "MCP_GOOGLE_UNREACHABLE", "message": str(exc)}))
        return []

    stubs = (search_body or {}).get("messages") or []
    if not stubs:
        print(json.dumps({"status": "ok", "ingested": 0, "message": "No new messages"}))
        return []

    ingested: list[dict[str, Any]] = []
    total_raw = total_clean = 0
    new_seen: list[str] = []

    for stub in stubs:
        msg_id = stub.get("id", "")
        if not msg_id or msg_id in seen_ids:
            continue
        try:
            msg = mcp_google.call("google_mail_read", {"message_id": msg_id})
        except mcp_google.GoogleMCPError as exc:
            log.warning("ingest: read failed for %s: %s", msg_id, exc)
            continue
        if not isinstance(msg, dict) or msg.get("error"):
            continue

        sender = msg.get("from", "unknown")
        subject = msg.get("subject", "(no subject)")
        date_str = msg.get("date", "")
        thread_id = msg.get("thread_id", "")
        raw_body = msg.get("body", "") or msg.get("snippet", "")
        # google_mail_read returns plaintext where possible; treat as text/plain.
        cleaned, raw_tok, clean_tok = preprocess_email(raw_body, "text/plain")
        total_raw += raw_tok
        total_clean += clean_tok

        formatted = f"[{sender}] at [{date_str}]: {cleaned}"

        txn = create_transaction(
            agent_id=AGENT_NAME, thread_id=thread_id,
            direction="INBOUND", status="RECEIVED",
            subject=subject, body_markdown=cleaned,
        )

        ingested.append({
            "transaction_id": txn["transaction_id"],
            "thread_id": thread_id,
            "message_id": msg_id,
            "sender": sender,
            "subject": subject,
            "formatted": formatted,
            "raw_tokens": raw_tok,
            "clean_tokens": clean_tok,
        })
        _counters["agent.email.ingested"] += 1
        new_seen.append(msg_id)

    if new_seen:
        merged = (list(seen_ids) + new_seen)[-1000:]
        hwm["seen_message_ids"] = merged
        hwm["updated_at"] = datetime.now(timezone.utc).isoformat()
        hwm_file.parent.mkdir(parents=True, exist_ok=True)
        hwm_file.write_text(json.dumps(hwm))

    saved = total_raw - total_clean
    _counters["agent.email.tokens_saved"] += saved
    log.info(
        "[%s] Ingested %d emails — raw=%d clean=%d saved=%d (%.0f%%)",
        AGENT_NAME, len(ingested), total_raw, total_clean,
        saved, (saved / max(total_raw, 1)) * 100,
    )

    print(json.dumps({
        "status": "ok",
        "ingested": len(ingested),
        "tokens": {"raw": total_raw, "clean": total_clean, "saved": saved},
        "emails": ingested,
    }, indent=2))
    return ingested


# ── Gated send ───────────────────────────────────────────────────────


def send_gated(
    to: list[str],
    subject: str,
    body_markdown: str,
    cc: list[str] | None = None,
    thread_id: str = "",
    bypass_approval: bool = False,
) -> dict[str, Any]:
    """Draft and route an outbound email through the approval pipeline.

    Internal emails are auto-approved.  External emails require HITL approval
    via Mattermost unless the recipients are whitelisted and *bypass_approval*
    is True. All Gmail transport now flows through openclaw-mcp-google
    (SPEC-GAUTH-001 revised / #323).
    """
    from triage import send_email as gmail_send

    cc = list(cc or [])
    all_recipients = to + cc

    # Loop detection
    if thread_id and check_loop(thread_id):
        txn = create_transaction(
            agent_id=AGENT_NAME, thread_id=thread_id, direction="OUTBOUND",
            status="DRAFTED", to=to, cc=cc, subject=subject, body_markdown=body_markdown,
        )
        update_transaction(txn["transaction_id"], "QUARANTINED", actor="system")

        ch = _resolve_mm_channel("alerts")
        if ch:
            _mm_api("POST", "/posts", {
                "channel_id": ch,
                "message": (
                    f":warning: **Auto-responder loop detected** — `{AGENT_NAME}`\n\n"
                    f"Thread `{thread_id}` hit {LOOP_THRESHOLD}+ outbound emails in 1 h.\n"
                    f"Transaction `{txn['transaction_id']}` quarantined."
                ),
            })

        result = {
            "status": "error", "error_code": "QUARANTINED",
            "message": f"Loop detected on thread {thread_id}. Email quarantined.",
            "transaction_id": txn["transaction_id"],
        }
        print(json.dumps(result, indent=2))
        return result

    txn = create_transaction(
        agent_id=AGENT_NAME, thread_id=thread_id or str(uuid.uuid4()),
        direction="OUTBOUND", status="DRAFTED",
        to=to, cc=cc, subject=subject, body_markdown=body_markdown,
    )
    txn_id = txn["transaction_id"]

    domain_class = classify_recipients(all_recipients)

    # ── Internal → auto-approve ──────────────────────────────────────
    if domain_class == "internal":
        update_transaction(txn_id, "APPROVED", actor="system:auto-approve-internal")
        send_result = gmail_send(to, subject, body_markdown, cc=cc, _quiet=True)
        if send_result.get("status") == "success":
            update_transaction(txn_id, "SENT", actor="system")
            _counters["agent.email.sent"] += 1
            _counters["agent.email.sent.internal"] += 1
            result = {
                "status": "success",
                "message": f"Internal email sent (auto-approved). Transaction: {txn_id}",
                "transaction_id": txn_id,
                "message_id": send_result.get("message_id"),
            }
        else:
            result = {
                "status": "error", "error_code": "SEND_FAILED",
                "message": f"Auto-approved but send failed: {send_result.get('message')}",
                "transaction_id": txn_id,
            }
        print(json.dumps(result, indent=2))
        return result

    # ── External + whitelisted + bypass flag ─────────────────────────
    if bypass_approval and is_whitelisted(all_recipients):
        update_transaction(txn_id, "APPROVED", actor="system:whitelist-bypass")
        send_result = gmail_send(to, subject, body_markdown, cc=cc, _quiet=True)
        if send_result.get("status") == "success":
            update_transaction(txn_id, "SENT", actor="system")
            _counters["agent.email.sent"] += 1
            _counters["agent.email.sent.external"] += 1
            result = {
                "status": "success",
                "message": f"External email sent (whitelisted). Transaction: {txn_id}",
                "transaction_id": txn_id,
                "message_id": send_result.get("message_id"),
            }
        else:
            result = {
                "status": "error", "error_code": "SEND_FAILED",
                "message": f"Whitelisted but send failed: {send_result.get('message')}",
                "transaction_id": txn_id,
            }
        print(json.dumps(result, indent=2))
        return result

    # ── External, not whitelisted → HITL ─────────────────────────────
    update_transaction(txn_id, "PENDING_APPROVAL", actor="system:external-hitl")
    post_approval_request(txn)

    result = {
        "status": "pending_approval",
        "message": (
            f"External email requires human approval. "
            f"Request posted to #{APPROVAL_CHANNEL}. Transaction: {txn_id}"
        ),
        "transaction_id": txn_id,
    }
    print(json.dumps(result, indent=2))
    return result


# ── Finalize ─────────────────────────────────────────────────────────


def finalize(
    txn_id: str,
    decision: str,
    actor: str = "admin",
    reason: str = "",
) -> dict[str, Any]:
    """Execute an approval or rejection on a PENDING_APPROVAL transaction."""
    from triage import send_email as gmail_send

    data = _load_transactions()
    txn = data["transactions"].get(txn_id)

    if not txn:
        result = {"status": "error", "error_code": "NOT_FOUND", "message": f"Transaction {txn_id} not found"}
        print(json.dumps(result, indent=2))
        return result

    if txn["status"] != "PENDING_APPROVAL":
        result = {
            "status": "error", "error_code": "INVALID_STATE",
            "message": f"Transaction {txn_id} is {txn['status']}, not PENDING_APPROVAL",
        }
        print(json.dumps(result, indent=2))
        return result

    if decision == "approve":
        update_transaction(txn_id, "APPROVED", actor=f"human:{actor}", approval_user=actor)
        send_result = gmail_send(
            txn["to"], txn["subject"], txn["body_markdown"],
            cc=txn.get("cc"), _quiet=True,
        )
        if send_result.get("status") == "success":
            update_transaction(txn_id, "SENT", actor="system")
            _counters["agent.email.sent"] += 1
            _counters["agent.email.sent.external"] += 1
            result = {
                "status": "success",
                "message": f"Email approved and sent. Transaction: {txn_id}",
                "transaction_id": txn_id,
                "message_id": send_result.get("message_id"),
            }
        else:
            result = {
                "status": "error", "error_code": "SEND_FAILED",
                "message": f"Approved but send failed: {send_result.get('message')}",
                "transaction_id": txn_id,
            }

    elif decision == "reject":
        update_transaction(txn_id, "REJECTED", actor=f"human:{actor}", rejection_reason=reason)
        result = {
            "status": "rejected",
            "message": f"Email rejected by {actor}. Transaction: {txn_id}",
            "transaction_id": txn_id,
            "rejection_reason": reason or "No reason provided",
        }

    else:
        result = {
            "status": "error", "error_code": "INVALID_DECISION",
            "message": f"Decision must be 'approve' or 'reject', got '{decision}'",
        }

    print(json.dumps(result, indent=2))
    return result


# ── Status ───────────────────────────────────────────────────────────


def show_status() -> None:
    data = _load_transactions()
    txns = data.get("transactions", {})

    by_status: dict[str, int] = {}
    for t in txns.values():
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1

    pending = [
        {"transaction_id": t["transaction_id"], "agent_id": t["agent_id"],
         "to": t["to"], "subject": t["subject"], "created_at": t["created_at"]}
        for t in txns.values() if t["status"] == "PENDING_APPROVAL"
    ]

    print(json.dumps({
        "total": len(txns),
        "by_status": by_status,
        "pending_approvals": pending,
        "counters": _counters,
    }, indent=2))


# ── CLI ──────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="EMAIL-OPS-001: Agent Email Management & HITL Approval",
    )
    parser.add_argument(
        "--action", required=True,
        choices=["ingest", "send-gated", "finalize", "status"],
    )
    parser.add_argument("--to", help="Recipient(s), comma-separated.")
    parser.add_argument("--cc", help="CC recipient(s), comma-separated.")
    parser.add_argument("--subject")
    parser.add_argument("--body-markdown", dest="body_markdown")
    parser.add_argument("--body", dest="body_legacy", help=argparse.SUPPRESS)
    parser.add_argument("--thread-id", dest="thread_id", default="")
    parser.add_argument("--transaction-id", dest="transaction_id")
    parser.add_argument("--decision", choices=["approve", "reject"])
    parser.add_argument("--reason", default="")
    parser.add_argument("--bypass-approval", dest="bypass_approval", action="store_true")
    parser.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()

    if args.action == "status":
        show_status()
        return

    if args.action == "ingest":
        ingest_emails(limit=args.limit)

    elif args.action == "send-gated":
        if not args.to:
            parser.error("--to is required")
        if not args.subject:
            parser.error("--subject is required")
        body = args.body_markdown or args.body_legacy
        if not body:
            parser.error("--body-markdown is required")
        to_list = [a.strip() for a in args.to.split(",") if a.strip()]
        cc_list = [a.strip() for a in (args.cc or "").split(",") if a.strip()]
        send_gated(
            to_list, args.subject, body,
            cc=cc_list, thread_id=args.thread_id,
            bypass_approval=args.bypass_approval,
        )

    elif args.action == "finalize":
        if not args.transaction_id:
            parser.error("--transaction-id is required")
        if not args.decision:
            parser.error("--decision is required")
        finalize(args.transaction_id, args.decision, reason=args.reason)


if __name__ == "__main__":
    main()
