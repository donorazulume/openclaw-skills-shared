#!/usr/bin/env python3
"""gmail-executive — Executive Triage System (ETS), MCP-M365 edition.

This skill is ported from Google Gmail to Microsoft 365 (SPEC-ARCH-001).
Every operation routes through ``openclaw-mcp-m365`` via ``skills/lib/mcp_m365.py``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from typing import Any

import time

script_dir = os.path.dirname(os.path.abspath(__file__))
for extra_path in [
    os.path.abspath(os.path.join(script_dir, "..", "lib")),
    os.path.abspath(os.path.join(script_dir, "..", "..", "skills", "lib")),
    os.path.abspath(os.path.join(script_dir, "..", "..", "workspace", "skills", "lib")),
]:
    if extra_path not in sys.path and os.path.exists(extra_path):
        sys.path.insert(0, extra_path)

from email_utils import markdown_to_html  # noqa: E402
import mcp_google  # noqa: E402
import mcp_m365  # noqa: E402
import prompt_injection  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("gmail-executive")


def check_and_update_heartbeat_state(state_file_path: str = "memory/heartbeat-state.json") -> dict[str, Any]:
    """Update lastChecks timestamp in heartbeat-state.json and flag if >48h stale (Issue #511)."""
    now_ts = int(time.time())
    state_data: dict[str, Any] = {"lastChecks": now_ts, "lastRuns": now_ts, "status": "healthy"}
    if os.path.exists(state_file_path):
        try:
            with open(state_file_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
                if isinstance(existing, dict):
                    last_check = existing.get("lastChecks", 0)
                    if isinstance(last_check, (int, float)) and (now_ts - last_check > 48 * 3600):
                        log.warning(
                            "Heartbeat state was %d hours stale (>48h gap). Resetting and flagging liveness warning.",
                            (now_ts - last_check) // 3600,
                        )
                        existing["liveness_warning"] = (
                            f"Heartbeat was stale by {(now_ts - last_check) // 3600} hours before reset."
                        )
                    state_data.update(existing)
        except Exception as exc:
            log.warning("Could not parse existing heartbeat state from %s: %s", state_file_path, exc)

    state_data["lastChecks"] = now_ts
    state_data["lastRuns"] = now_ts

    try:
        os.makedirs(os.path.dirname(os.path.abspath(state_file_path)), exist_ok=True)
        with open(state_file_path, "w", encoding="utf-8") as f:
            json.dump(state_data, f, indent=2)
    except Exception as exc:
        log.warning("Failed writing heartbeat state to %s: %s", state_file_path, exc)

    return state_data


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
    """Invoke an MCP tool (openclaw-mcp-google or openclaw-mcp-m365) and unwrap error envelopes."""
    if tool.startswith("google_"):
        try:
            result = mcp_google.call(tool, arguments)
        except mcp_google.GoogleMCPError as exc:
            sys.exit(f"ERROR: MCP Google call '{tool}' failed: {exc}")
    else:
        try:
            result = mcp_m365.call(tool, arguments)
        except mcp_m365.M365MCPError as exc:
            sys.exit(f"ERROR: MCP M365 call '{tool}' failed: {exc}")

    if isinstance(result, dict) and isinstance(result.get("error"), dict):
        err = result["error"]
        sys.exit(
            f"ERROR: tool {tool} returned {err.get('code') or '?'}: {err.get('message') or err}"
        )
    if not isinstance(result, dict):
        sys.exit(f"ERROR: tool {tool} returned non-dict payload: {type(result).__name__}")
    return result


def expert_judgment_from_headers(subject: str, from_addr: str) -> str | None:
    """Classify message priority from header strings. Returns target category or None."""
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
    """M365 categories are dynamically assigned, no pre-creation needed."""
    print("Executive Triage System — Category Initialization (M365)\n")
    for name in ETS_LABELS:
        print(f"  ✓ Category ready: {name}")
    print("\nDone. All categories are ready for dynamic assignment.")


def get_status() -> None:
    """Print unread + total counts for INBOX and each ETS category."""
    print("Executive Triage System — Status (M365)\n")
    print(f"{'Category':<25} {'Unread':>8}")
    print("-" * 35)

    try:
        res = _call("m365_mail_list", folder="inbox", top=100)
    except SystemExit as exc:
        # Fallback if list fails
        print(f"Failed to fetch mail list: {exc}")
        return

    messages = res.get("messages") or []

    cat_counts: dict[str, dict[str, int]] = {
        cat: {"unread": 0, "total": 0} for cat in ["INBOX"] + ETS_LABELS
    }

    for msg in messages:
        is_unread = not msg.get("isRead", False)
        cat_counts["INBOX"]["total"] += 1
        if is_unread:
            cat_counts["INBOX"]["unread"] += 1

        cats = msg.get("categories") or []
        for cat in cats:
            if cat in cat_counts:
                cat_counts[cat]["total"] += 1
                if is_unread:
                    cat_counts[cat]["unread"] += 1

    for cat in ["INBOX"] + ETS_LABELS:
        unread = cat_counts[cat]["unread"]
        total = cat_counts[cat]["total"]
        print(f"{cat:<25} {unread:>8}  (total: {total})")
    print()


def triage(limit: int = 50) -> None:
    """Triage recent INBOX messages and assign ETS categories."""
    res = _call("m365_mail_list", folder="inbox", top=limit)
    messages = res.get("messages") or []
    if not messages:
        print("Inbox is empty — nothing to triage.")
        return

    moved: dict[str, int] = {}
    skipped = 0

    print("Executive Triage System — Triage Run (M365)\n")
    print(f"Scanning {len(messages)} message(s) from INBOX…\n")

    for msg in messages:
        cats = msg.get("categories") or []
        # Skip if already has an ETS category
        if any(cat in ETS_LABELS for cat in cats):
            skipped += 1
            continue

        subject = msg.get("subject") or ""
        from_dict = msg.get("from") or {}
        from_addr = (from_dict.get("emailAddress", {}).get("address") if isinstance(from_dict, dict) else str(from_dict)) or ""

        target = expert_judgment_from_headers(subject, from_addr) or _rule_target(subject, from_addr)
        if not target:
            skipped += 1
            continue

        new_cats = list(set(cats + [target]))
        _call("m365_mail_update_categories", message_id=msg["id"], categories=new_cats)
        moved[target] = moved.get(target, 0) + 1

    total_moved = sum(moved.values())
    print(f"{'Category':<25} {'Categorized':>8}")
    print("-" * 35)
    for label, count in sorted(moved.items()):
        print(f"{label:<25} {count:>8}")
    print("-" * 35)
    print(f"{'Total categorized':<25} {total_moved:>8}")
    print(f"{'Remaining in INBOX':<25} {skipped:>8}\n")


def triage_report(limit: int = 15) -> None:
    """Triage messages and print a JSON report with previews for high priority."""
    res = _call("m365_mail_list", folder="inbox", top=limit)
    messages = res.get("messages") or []
    if not messages:
        print(json.dumps({
            "summary": {"total_processed": 0, "moved": {}, "remained_inbox": 0},
            "emails": [],
        }, indent=2))
        return

    email_records: list[dict[str, Any]] = []
    moved: dict[str, int] = {}

    for msg in messages:
        subject = msg.get("subject") or ""
        from_dict = msg.get("from") or {}
        from_addr = (from_dict.get("emailAddress", {}).get("address") if isinstance(from_dict, dict) else str(from_dict)) or ""
        snippet = msg.get("bodyPreview") or ""
        cats = msg.get("categories") or []

        has_ets_cat = any(cat in ETS_LABELS for cat in cats)
        target = None
        classification = "unmatched"
        importance = "normal"

        if not has_ets_cat:
            target = expert_judgment_from_headers(subject, from_addr)
            classification = "expert_judgment"
            importance = "medium"
            if not target:
                target = _rule_target(subject, from_addr)
                classification = "rule"
                importance = "low"

        record: dict[str, Any] = {
            "id": msg["id"],
            "from": from_addr,
            "subject": subject,
            "snippet": snippet,
        }

        if target:
            record["label"] = target
            record["classification"] = classification
            record["importance"] = "high" if target in ("01_Action", "PARA/Areas") else importance
            new_cats = list(set(cats + [target]))
            _call("m365_mail_update_categories", message_id=msg["id"], categories=new_cats)
            moved[target] = moved.get(target, 0) + 1
        else:
            record["label"] = cats[0] if cats else "INBOX"
            record["importance"] = "high" if any(c in ("01_Action", "PARA/Areas") for c in cats) else "normal"
            record["classification"] = "already_triaged" if has_ets_cat else "unmatched"

        email_records.append(record)

    # Fetch body preview and attachments for high importance ones
    important_ids = [r["id"] for r in email_records if r.get("importance") == "high"]
    for rid in important_ids:
        body = _call("m365_mail_read", message_id=rid)
        body_val = body.get("body", {})
        text = (body_val.get("content", "") if isinstance(body_val, dict) else (body_val or "")) or ""
        attachments = body.get("attachments", [])
        for r in email_records:
            if r["id"] == rid:
                from_addr = r.get("from", "")
                is_trusted = any(from_addr.lower().startswith(prefix) for prefix in ["don@", "roho@"])
                sanitized_text, _ = prompt_injection.sanitize_text(text, is_trusted=is_trusted)
                truncated_text = sanitized_text[:3000]
                r["body_preview"] = prompt_injection.wrap_content(
                    truncated_text,
                    source=f"email from {from_addr}",
                    metadata=f"subject: {r.get('subject', '')}"
                )
                if attachments:
                    r["attachments"] = attachments
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


def download_attachment(
    message_id: str,
    part_id: str,
    out_dir: str | None = None,
    max_size_mb: int = 25,
) -> dict[str, Any]:
    """Download base64 attachment content via MCP and write binary payload to disk (SPEC-GMAIL-001 C3)."""
    import base64
    from pathlib import Path

    res = _call(
        "google_mail_download_attachment",
        message_id=message_id,
        part_id=part_id,
        max_size_mb=max_size_mb,
    )

    filename = res.get("filename") or f"attachment_{part_id}"
    mime_type = res.get("mime_type", "application/octet-stream")
    content_b64 = res.get("content_b64", "")
    size_bytes = res.get("size_bytes", 0)

    save_dir = Path(out_dir) if out_dir else Path.cwd() / "attachments"
    save_dir.mkdir(parents=True, exist_ok=True)
    out_path = save_dir / filename

    raw_bytes = base64.b64decode(content_b64)
    out_path.write_bytes(raw_bytes)

    out_payload = {
        "status": "success",
        "message_id": message_id,
        "part_id": part_id,
        "filename": filename,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "file_path": str(out_path.resolve()),
    }
    print(json.dumps(out_payload, indent=2))
    return out_payload


def track_important_threads(limit: int = 10) -> dict[str, Any]:
    """Read full message threads for high-priority items and track decisions & action points (SPEC-GMAIL-001 C4)."""
    res = _call("google_mail_search", query="label:01_Action OR label:02_Waiting OR label:IMPORTANT", max_results=limit)
    stubs = res.get("messages") or []
    seen_thread_ids: set[str] = set()
    thread_records: list[dict[str, Any]] = []

    for stub in stubs:
        tid = stub.get("thread_id") or stub.get("threadId") or stub.get("id")
        if not tid or tid in seen_thread_ids:
            continue
        seen_thread_ids.add(tid)

        try:
            thread_data = _call("google_mail_get_thread", thread_id=tid, fmt="full")
        except SystemExit:
            continue

        msgs = thread_data.get("messages") or []
        msgs_summary = []
        all_attachments = []

        for m in msgs:
            m_headers = m.get("headers") or {}
            sender = m_headers.get("From") or m.get("from", "unknown")
            date_val = m_headers.get("Date") or m.get("date", "")
            body_txt = m.get("body", "") or m.get("snippet", "")
            atts = m.get("attachments", [])
            if atts:
                all_attachments.extend(atts)

            msgs_summary.append({
                "id": m.get("id"),
                "sender": sender,
                "date": date_val,
                "body_preview": body_txt[:1000],
                "attachment_count": len(atts),
            })

        subject = (msgs[0].get("headers", {}).get("Subject") if msgs else "") or "(no subject)"
        thread_records.append({
            "thread_id": tid,
            "subject": subject,
            "message_count": len(msgs),
            "attachments": all_attachments,
            "messages": msgs_summary,
        })

    report = {
        "status": "success",
        "tracked_threads": len(thread_records),
        "threads": thread_records,
    }
    print(json.dumps(report, indent=2))
    return report


def draft_reply(message_id: str, body_text: str) -> None:
    """Create a draft reply on a message using MCP M365."""
    html_body = markdown_to_html(body_text)
    draft = _call(
        "m365_mail_create_draft_reply",
        message_id=message_id,
        body_html=html_body,
    )
    print("Draft reply created successfully (M365).")
    print(f"  Draft ID:  {draft.get('draft_id', '?')}")
    print(f"  Subject:   {draft.get('subject', '?')}")


def list_labels() -> None:
    """List ETS categories."""
    print("Executive Triage System — Categories (M365)\n")
    print(f"{'Category':<40} {'Status'}")
    print("-" * 70)
    for cat in sorted(ETS_LABELS):
        print(f"{cat:<40} Active")
    print()


def digest() -> None:
    """List unread messages in 01_Action and 03_Read."""
    print("Executive Triage System — Digest (M365)\n")
    
    res = _call("m365_mail_list", folder="inbox", filter_unread=True, top=50)
    unread_messages = res.get("messages") or []

    for cat_name in ("01_Action", "03_Read"):
        msgs = [m for m in unread_messages if cat_name in (m.get("categories") or [])]
        print(f"[{cat_name}]  {len(msgs)} unread message(s)")
        if not msgs:
            print()
            continue
        print(f"  {'#':<4} {'From':<35} {'Subject'}")
        print(f"  {'-' * 80}")
        for i, m in enumerate(msgs, 1):
            from_dict = m.get("from") or {}
            sender = from_dict.get("emailAddress", {}).get("address", "—")
            if len(sender) > 33:
                sender = sender[:30] + "…"
            print(f"  {i:<4} {sender:<35} {m.get('subject', '(no subject)')}")
        print()


def _validate_email(address: str) -> bool:
    return bool(EMAIL_RE.match(address.strip()))


def _inject_forced_cc(cc: list[str]) -> list[str]:
    normalised = {addr.strip().lower() for addr in cc}
    if FORCED_CC_ADDRESS.lower() not in normalised:
        cc = list(cc) + [FORCED_CC_ADDRESS]
    return cc


def send_email(
    to: list[str],
    subject: str,
    body_markdown: str,
    cc: list[str] | None = None,
    _quiet: bool = False,
) -> dict[str, Any]:
    """Compose and send an email through MCP M365."""
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
        result = _call(
            "m365_mail_send",
            to=to,
            cc=cc if cc else None,
            subject=subject,
            body_html=html_body,
        )
    except SystemExit as exc:
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


def search_emails(query: str, limit: int = 5) -> None:
    """Search messages using query filters in python and print as JSON."""
    import datetime

    # 1. Parse query
    from_match = re.search(r"from:(\S+)", query)
    newer_match = re.search(r"newer_than:(\S+)", query)

    from_domain = from_match.group(1).lower() if from_match else None
    newer_str = newer_match.group(1).lower() if newer_match else None

    # Calculate cutoff date if newer_than is specified
    cutoff_date = None
    since_iso = None
    if newer_str:
        days_match = re.match(r"(\d+)d", newer_str)
        if days_match:
            days = int(days_match.group(1))
            cutoff_date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
            since_iso = cutoff_date.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 2. Fetch recent messages with since filter if available, top=100
    kwargs: dict[str, Any] = {"folder": "inbox", "top": 100}
    if since_iso:
        kwargs["since"] = since_iso

    try:
        res = _call("m365_mail_list", **kwargs)
    except Exception as exc:
        log.warning("m365_mail_list with since filter failed, falling back to default list: %s", exc)
        res = _call("m365_mail_list", folder="inbox", top=100)

    messages = res.get("messages") or []

    filtered = []
    for msg in messages:
        from_dict = msg.get("from") or {}
        from_addr = (from_dict.get("emailAddress", {}).get("address") or "").lower()
        subject = (msg.get("subject") or "").lower()

        # Filter by from or subject domain match (e.g. pensioncraft)
        if from_domain:
            domain_part = from_domain.replace("@", "").split(".")[0]
            if from_domain not in from_addr and domain_part not in from_addr and domain_part not in subject:
                continue

        # Filter by date
        if cutoff_date:
            dt_str = msg.get("receivedDateTime", "")
            if dt_str:
                try:
                    dt_str = dt_str.replace("Z", "+00:00")
                    msg_dt = datetime.datetime.fromisoformat(dt_str)
                    if msg_dt < cutoff_date:
                        continue
                except Exception:
                    pass

        filtered.append(msg)
        if len(filtered) >= limit:
            break

    results = []
    for msg in filtered:
        body_res = _call("m365_mail_read", message_id=msg["id"])
        msg_body = body_res.get("body", {}).get("content", "") or ""

        results.append({
            "id": msg["id"],
            "from": msg.get("from", {}).get("emailAddress", {}).get("address", ""),
            "subject": msg.get("subject", ""),
            "receivedDateTime": msg.get("receivedDateTime", ""),
            "body": msg_body,
            "snippet": msg.get("bodyPreview", "")
        })

    print(json.dumps(results, indent=2))


# ── CLI ──────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Executive Triage System (ETS) — MCP M365 & Google edition")
    parser.add_argument(
        "--action",
        required=True,
        choices=[
            "init", "status", "triage", "triage-report", "draft", "send",
            "labels", "digest", "search", "download-attachment", "track-threads",
        ],
    )
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--thread-id", dest="thread_id")  # Mapped to message_id in M365
    parser.add_argument("--message-id", dest="message_id")
    parser.add_argument("--part-id", dest="part_id")
    parser.add_argument("--out-dir", dest="out_dir")
    parser.add_argument("--to")
    parser.add_argument("--cc")
    parser.add_argument("--subject")
    parser.add_argument("--body-markdown", dest="body_markdown")
    parser.add_argument("--body")
    parser.add_argument("--query")
    parser.add_argument("--batch-size", type=int, default=15, help=argparse.SUPPRESS)
    parser.add_argument("--batch-delay", type=float, default=1.0, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.action == "init":
        init_labels()
    elif args.action == "status":
        get_status()
    elif args.action == "triage":
        triage(limit=args.limit)
    elif args.action == "triage-report":
        triage_report(limit=args.limit)
    elif args.action == "download-attachment":
        msg_id = args.message_id or args.thread_id
        if not msg_id or not args.part_id:
            parser.error("--message-id (or --thread-id) and --part-id are required for download-attachment")
        download_attachment(msg_id, args.part_id, out_dir=args.out_dir)
    elif args.action == "track-threads":
        track_important_threads(limit=args.limit)
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
    elif args.action == "search":
        if not args.query:
            parser.error("--query is required for search")
        search_emails(args.query, limit=args.limit)


if __name__ == "__main__":
    main()
