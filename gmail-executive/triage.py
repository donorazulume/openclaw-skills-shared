#!/usr/bin/env python3
"""gmail-executive — Executive Triage System (ETS), MCP-Google edition.

This skill no longer mints Google OAuth credentials. Every Gmail API operation
routes through ``openclaw-mcp-google`` (SPEC-GAUTH-001 revised / #323 / #324) via
``skills/lib/mcp_google.py``. The classification logic (ETS labels, expert
judgment, triage rules) still lives here; only the transport changed.

Environment (set by docker-compose.prod.yml / docker-compose.amara.yml):
    MCP_GOOGLE_URL            Base URL of openclaw-mcp-google (default ``http://openclaw-mcp-google:8103``)
    MCP_TOKEN_GOOGLE_ROHO     Bearer token consumed by MCP Google ``auth.init_tokens``
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'lib'))
from email_utils import markdown_to_html  # noqa: E402
import mcp_google  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("gmail-executive")

ETS_LABELS = [
    "01_Action",
    "02_Waiting",
    "03_Read",
    "PARA/Projects",
    "PARA/Areas",
    "PARA/Resources",
    "PARA/Archives",
]

TRIAGE_RULES: list[tuple[str, str, str]] = [
    ("from", r"newsletter|digest|noreply|no-reply|unsubscribe", "03_Read"),
    ("subject", r"(?i)\b(invoice|receipt|payment|billing)\b", "PARA/Areas"),
]

FORCED_CC_ADDRESS = "don@chimexhldg.com"
EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

_email_counters: dict[str, int] = {
    "agent_emails_sent_total": 0,
    "agent_email_format_errors": 0,
}


def _call(tool: str, **arguments: Any) -> dict[str, Any]:
    """Invoke an openclaw-mcp-google tool and unwrap ``{"error": …}`` envelopes."""
    try:
        result = mcp_google.call(tool, arguments)
    except mcp_google.GoogleMCPError as exc:
        sys.exit(f"ERROR: MCP Google call '{tool}' failed: {exc}")
    if isinstance(result, dict) and isinstance(result.get("error"), dict):
        err = result["error"]
        sys.exit(
            f"ERROR: tool {tool} returned {err.get('code') or '?'}: {err.get('message') or err}"
        )
    if not isinstance(result, dict):
        sys.exit(f"ERROR: tool {tool} returned non-dict payload: {type(result).__name__}")
    return result


def _label_index() -> dict[str, str]:
    """Map label name → label ID from MCP Google."""
    body = _call("google_mail_list_labels")
    return {lbl["name"]: lbl["id"] for lbl in body.get("labels") or [] if lbl.get("name")}


def _ensure_label(name: str, cache: dict[str, str]) -> str:
    if name in cache:
        return cache[name]
    body = _call("google_mail_create_label", name=name)
    label_id = body.get("id", "")
    if label_id:
        cache[name] = label_id
    return label_id


def expert_judgment_from_headers(subject: str, from_addr: str) -> str | None:
    """Classify message priority from header strings. Returns target label or None."""
    s = subject.lower()
    f = from_addr.lower()

    urgent_patterns = [
        r"\burgent\b", r"\bdeadline\b", r"\basap\b",
        r"\baction required\b", r"\bimmediate\b", r"\btime.?sensitive\b",
        r"\bsecurity alert\b", r"\bnew login\b", r"\bverify your account\b",
        r"\bconfidential\b",
    ]
    for pat in urgent_patterns:
        if re.search(pat, s, re.IGNORECASE):
            return "01_Action"

    waiting_patterns = [
        r"\bpending\b.*\b(approval|review)\b",
        r"\bawaiting\b", r"\bfollow.?up\b",
        r"\bdelivery\b", r"\btracking\b", r"\bshipped\b",
    ]
    for pat in waiting_patterns:
        if re.search(pat, s, re.IGNORECASE):
            return "02_Waiting"

    financial_patterns = [
        r"\binvoice\b", r"\breceipt\b", r"\bpayment\b", r"\bbilling\b", r"\bstatement\b",
    ]
    for pat in financial_patterns:
        if re.search(pat, s, re.IGNORECASE):
            return "PARA/Areas"

    vip_patterns = [
        r"\bceo\b", r"\bfounder\b", r"\bpresident\b", r"\bdirector\b",
        r"\bvp\b", r"\bboard\b",
    ]
    for pat in vip_patterns:
        if re.search(pat, s, re.IGNORECASE) or re.search(pat, f, re.IGNORECASE):
            return "01_Action"

    return None


def _rule_target(subject: str, from_addr: str) -> str | None:
    for field, pattern, target_label in TRIAGE_RULES:
        value = from_addr if field == "from" else subject
        if re.search(pattern, value, re.IGNORECASE):
            return target_label
    return None


# ── Actions ──────────────────────────────────────────────────────────


def init_labels() -> None:
    """Ensure all ETS labels exist (idempotent)."""
    cache = _label_index()
    created = 0
    print("Executive Triage System — Label Initialization\n")
    for name in ETS_LABELS:
        if name in cache:
            print(f"  ✓ Exists:  {name}")
            continue
        body = _call("google_mail_create_label", name=name)
        status = body.get("status", "")
        if status == "created":
            print(f"  + Created: {name}")
            created += 1
        else:
            print(f"  ✓ Exists:  {name}")
        if body.get("id"):
            cache[name] = body["id"]
    print(f"\nDone. {created} label(s) created, {len(ETS_LABELS) - created} already existed.")


def get_status() -> None:
    """Print unread + total counts for INBOX and each ETS label."""
    check_labels = ["INBOX"] + ETS_LABELS
    print("Executive Triage System — Status\n")
    print(f"{'Label':<25} {'Unread':>8}")
    print("-" * 35)
    for label in check_labels:
        info = _call("google_mail_label_info", label=label)
        if info.get("exists") is False:
            print(f"{label:<25}      —   (label not found)")
            continue
        unread = info.get("messages_unread", 0)
        total = info.get("messages_total", 0)
        print(f"{label:<25} {unread:>8}  (total: {total})")
    print()


def _gmail_search_ids(query: str, max_results: int) -> list[dict[str, Any]]:
    body = _call("google_mail_search", query=query, max_results=max_results)
    return body.get("messages") or []


def triage(limit: int = 50, query: str = "in:inbox is:unread") -> None:
    """Triage the most recent INBOX messages and move them to ETS labels."""
    messages = _gmail_search_ids(query, max_results=limit)
    if not messages:
        print("Inbox is empty — nothing to triage.")
        return

    cache = _label_index()
    for name in ETS_LABELS:
        _ensure_label(name, cache)
    inbox_id = cache.get("INBOX") or "INBOX"

    moves: dict[str, list[str]] = {}
    moved: dict[str, int] = {}
    skipped = 0

    print("Executive Triage System — Triage Run\n")
    print(f"Scanning {len(messages)} message(s) from INBOX…\n")

    for msg in messages:
        subject = msg.get("subject") or ""
        from_addr = msg.get("from") or ""
        target = expert_judgment_from_headers(subject, from_addr) or _rule_target(subject, from_addr)
        if not target:
            skipped += 1
            continue
        target_id = _ensure_label(target, cache)
        if not target_id:
            skipped += 1
            continue
        moves.setdefault(target, []).append(msg["id"])
        moved[target] = moved.get(target, 0) + 1

    for target_label, ids in moves.items():
        target_id = cache[target_label]
        body = _call(
            "google_mail_label_batch",
            message_ids=ids,
            add_labels=[target_id],
            remove_labels=[inbox_id],
        )
        if body.get("status") != "labels_updated":
            log.warning("batch move to %s returned %s", target_label, body)

    total_moved = sum(moved.values())
    print(f"{'Label':<25} {'Moved':>8}")
    print("-" * 35)
    for label, count in sorted(moved.items()):
        print(f"{label:<25} {count:>8}")
    print("-" * 35)
    print(f"{'Total moved':<25} {total_moved:>8}")
    print(f"{'Remained in INBOX':<25} {skipped:>8}\n")


def triage_report(limit: int = 15, query: str = "in:inbox is:unread") -> None:
    """Same classification as :func:`triage`, plus a JSON report w/ bodies for high-priority."""
    messages = _gmail_search_ids(query, max_results=limit)
    if not messages:
        print(json.dumps({
            "summary": {"total_processed": 0, "moved": {}, "remained_inbox": 0},
            "emails": [],
        }, indent=2))
        return

    cache = _label_index()
    for name in ETS_LABELS:
        _ensure_label(name, cache)
    inbox_id = cache.get("INBOX") or "INBOX"

    email_records: list[dict[str, Any]] = []
    moves: dict[str, list[str]] = {}
    moved: dict[str, int] = {}

    for msg in messages:
        subject = msg.get("subject") or ""
        from_addr = msg.get("from") or ""
        snippet = msg.get("snippet") or ""

        target = expert_judgment_from_headers(subject, from_addr)
        classification = "expert_judgment"
        importance = "medium"
        if not target:
            target = _rule_target(subject, from_addr)
            classification = "rule"
            importance = "low"

        import datetime
        internal_date_ms = msg.get("internalDate", "")
        received_at = ""
        if internal_date_ms:
            try:
                dt = datetime.datetime.fromtimestamp(int(internal_date_ms) / 1000.0, datetime.timezone.utc)
                received_at = dt.isoformat()
            except Exception:
                pass

        record: dict[str, Any] = {
            "id": msg["id"],
            "from": from_addr,
            "subject": subject,
            "snippet": snippet,
            "internalDate": internal_date_ms,
            "received_at": received_at,
        }
        if target:
            record["label"] = target
            record["classification"] = classification
            record["importance"] = "high" if target in ("01_Action", "PARA/Areas") else importance
            moves.setdefault(target, []).append(msg["id"])
            moved[target] = moved.get(target, 0) + 1
        else:
            record["label"] = "INBOX"
            record["importance"] = "normal"
            record["classification"] = "unmatched"
        email_records.append(record)

    for target_label, ids in moves.items():
        target_id = cache[target_label]
        _call(
            "google_mail_label_batch",
            message_ids=ids,
            add_labels=[target_id],
            remove_labels=[inbox_id],
        )

    important_ids = [r["id"] for r in email_records if r.get("importance") == "high"]
    for rid in important_ids:
        body = _call("google_mail_read", message_id=rid)
        text = body.get("body") or ""
        for r in email_records:
            if r["id"] == rid:
                r["body_preview"] = text[:3000]
                break

    skipped = sum(1 for r in email_records if r["label"] == "INBOX")
    print(json.dumps(
        {
            "summary": {
                "total_processed": len(messages),
                "moved": moved,
                "remained_inbox": skipped,
            },
            "emails": email_records,
        },
        indent=2,
    ))


def draft_reply(thread_id: str, body_text: str) -> None:
    """Create a draft reply on a given thread using MCP Google."""
    thread = _call("google_mail_get_thread", thread_id=thread_id, fmt="metadata")
    messages = thread.get("messages") or []
    if not messages:
        sys.exit(f"ERROR: Thread {thread_id} has no messages.")
    last = messages[-1]
    headers = last.get("headers") or {}
    reply_to = headers.get("From", "")
    subject = headers.get("Subject", "")
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    message_id = headers.get("Message-ID", "")

    html_body = markdown_to_html(body_text)
    draft = _call(
        "google_mail_draft",
        to=reply_to,
        subject=subject,
        body_html=html_body,
        thread_id=thread_id,
        in_reply_to=message_id or None,
        references=message_id or None,
    )
    print("Draft created successfully.")
    print(f"  Draft ID:  {draft.get('draft_id', '?')}")
    print(f"  Thread:    {draft.get('thread_id', thread_id)}")
    print(f"  To:        {reply_to}")
    print(f"  Subject:   {subject}")


def list_labels() -> None:
    body = _call("google_mail_list_labels")
    labels = body.get("labels") or []
    print(f"Gmail Labels ({len(labels)} total)\n")
    print(f"{'Label':<40} {'ID'}")
    print("-" * 70)
    for lbl in sorted(labels, key=lambda x: x.get("name", "")):
        print(f"{lbl.get('name', ''):<40} {lbl.get('id', '')}")
    print()


def digest() -> None:
    """List unread messages in 01_Action and 03_Read via MCP Google search."""
    print("Executive Triage System — Digest\n")
    for label_name in ("01_Action", "03_Read"):
        body = _call("google_mail_search", query=f"label:{label_name} is:unread", max_results=25)
        msgs = body.get("messages") or []
        print(f"[{label_name}]  {len(msgs)} unread message(s)")
        if not msgs:
            print()
            continue
        print(f"  {'#':<4} {'From':<35} {'Subject'}")
        print(f"  {'-' * 80}")
        for i, m in enumerate(msgs, 1):
            sender = m.get("from", "—")
            if len(sender) > 33:
                sender = sender[:30] + "…"
            print(f"  {i:<4} {sender:<35} {m.get('subject', '(no subject)')}")
        print()


# ── Email sending (kept as compose helper; transport via MCP Google) ──


def _validate_email(address: str) -> bool:
    return bool(EMAIL_RE.match(address.strip()))


def _markdown_to_plaintext(md_text: str) -> str:
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
    normalised = {addr.strip().lower() for addr in cc}
    if FORCED_CC_ADDRESS.lower() not in normalised:
        cc = list(cc) + [FORCED_CC_ADDRESS]
    return cc


def _build_multipart(to: list[str], cc: list[str], subject: str, body_markdown: str) -> str:
    html_body = markdown_to_html(body_markdown)
    plain_body = _markdown_to_plaintext(body_markdown)
    mime = MIMEMultipart("alternative")
    mime["to"] = ", ".join(to)
    mime["cc"] = ", ".join(cc)
    mime["subject"] = subject
    mime.attach(MIMEText(plain_body, "plain"))
    mime.attach(MIMEText(html_body, "html"))
    return base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")  # legacy helper


def send_email(
    to: list[str],
    subject: str,
    body_markdown: str,
    cc: list[str] | None = None,
    _quiet: bool = False,
) -> dict[str, Any]:
    """Compose and send an email through MCP Google.

    MCP Google ``google_mail_send`` accepts a single ``to`` plus optional ``cc``/``bcc``
    strings. We forced-CC don@chimexhldg.com and join multiple ``to`` addresses with
    ``, ``; downstream Gmail header parsing handles the multi-recipient string.
    """
    agent_name = os.environ.get("OPENCLAW_AGENT_NAME", "unknown")

    if not to:
        _email_counters["agent_email_format_errors"] += 1
        log.error("[%s] send_email: no recipients provided", agent_name)
        return {"status": "error", "error_code": "MISSING_RECIPIENT", "message": "At least one recipient (to) is required."}

    invalid_to = [a for a in to if not _validate_email(a)]
    if invalid_to:
        _email_counters["agent_email_format_errors"] += 1
        log.error("[%s] send_email: invalid 'to' addresses: %s", agent_name, invalid_to)
        return {"status": "error", "error_code": "INVALID_EMAIL_FORMAT", "message": f"Invalid email address(es): {', '.join(invalid_to)}"}

    cc = list(cc or [])
    invalid_cc = [a for a in cc if not _validate_email(a)]
    if invalid_cc:
        _email_counters["agent_email_format_errors"] += 1
        log.error("[%s] send_email: invalid 'cc' addresses: %s", agent_name, invalid_cc)
        return {"status": "error", "error_code": "INVALID_EMAIL_FORMAT", "message": f"Invalid CC address(es): {', '.join(invalid_cc)}"}

    cc = _inject_forced_cc(cc)
    html_body = markdown_to_html(body_markdown)

    try:
        result = mcp_google.call(
            "google_mail_send",
            {
                "to": ", ".join(to),
                "cc": ", ".join(cc) if cc else None,
                "subject": subject,
                "body_html": html_body,
            },
        )
    except mcp_google.GoogleMCPError as exc:
        _email_counters["agent_email_format_errors"] += 1
        log.error("[%s] send_email transport error: %s", agent_name, exc)
        return {"status": "error", "error_code": "TRANSPORT_ERROR", "message": f"Failed to send email: {exc}"}

    if isinstance(result, dict) and isinstance(result.get("error"), dict):
        err = result["error"]
        _email_counters["agent_email_format_errors"] += 1
        return {"status": "error", "error_code": err.get("code", "SEND_FAILED"), "message": err.get("message", "send failed")}

    message_id = (result or {}).get("message_id", "—")
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


# ── CLI ──────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail Executive Triage System (ETS) — MCP Google edition")
    parser.add_argument(
        "--action",
        required=True,
        choices=["init", "status", "triage", "triage-report", "draft", "send", "labels", "digest"],
    )
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--thread-id", dest="thread_id")
    parser.add_argument("--to")
    parser.add_argument("--cc")
    parser.add_argument("--subject")
    parser.add_argument("--body-markdown", dest="body_markdown")
    parser.add_argument("--body")
    parser.add_argument("--query", default="in:inbox is:unread")
    # Kept for CLI compatibility; batching is now server-side via google_mail_label_batch.
    parser.add_argument("--batch-size", type=int, default=15, help=argparse.SUPPRESS)
    parser.add_argument("--batch-delay", type=float, default=1.0, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.action == "init":
        init_labels()
    elif args.action == "status":
        get_status()
    elif args.action == "triage":
        triage(limit=args.limit, query=args.query)
    elif args.action == "triage-report":
        triage_report(limit=args.limit, query=args.query)
    elif args.action == "draft":
        if not args.thread_id:
            parser.error("--thread-id is required for draft")
        if not args.body:
            parser.error("--body is required for draft")
        draft_reply(args.thread_id, args.body)
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
        result = send_email(to_list, args.subject, body, cc=cc_list)
        if result["status"] == "error":
            sys.exit(1)
    elif args.action == "labels":
        list_labels()
    elif args.action == "digest":
        digest()


if __name__ == "__main__":
    main()
