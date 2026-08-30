---
name: gmail-executive
description: Manage Gmail using the Executive Triage System (ETS) — Labeling, Drafting, Sending, and P.A.R.A. Sorting.
metadata: {"openclaw":{"requires":{"bins":["python3"]},"emoji":"📧"}}
---

Use this skill to triage the Inbox, send emails, manage labels, and maintain an executive email workflow.

## Capabilities

- **Initialize:** Sets up the required labels (`01_Action`, `02_Waiting`, `03_Read`, `PARA/…`).
- **Triage:** Scans INBOX, applies rule-based classification with expert priority detection (urgent/deadline → `01_Action`, pending approval → `02_Waiting`, newsletters → `03_Read`, invoices → `PARA/Areas`), moves to Stacks.
- **Send:** Compose and send emails. Provide the body in **standard Markdown** — the execution layer converts it to HTML and plain-text automatically. **Do not use HTML.** `don@chimexhldg.com` is always CC'd.
- **Draft:** Creates draft replies for specific threads (uses `gmail.compose`).
- **Labels:** List all Gmail labels.
- **Digest:** Lists unread items in `01_Action` and `03_Read`.
- **Status:** Count unread messages per ETS stack.

## Required Environment Variables

Per SPEC-GAUTH-001 revised (#323/#324), this skill does **not** hold Google OAuth
credentials. Every Gmail operation routes through `openclaw-mcp-google` over HTTP.

`MCP_GOOGLE_URL` — Base URL of the MCP Google service (default `http://openclaw-mcp-google:8103`).
`MCP_TOKEN_GOOGLE_ROHO` — Bearer token for `openclaw-mcp-google` (set in Doppler;
mirrored into the gateway by `docker-compose.prod.yml`).

`GOOGLE_TOKEN_JSON` and `GMAIL_TOKEN_JSON` are managed in Doppler by system-admin scripts for provisioning the `openclaw-mcp-google` microservice container. Gateway agent skills access Google capabilities exclusively over HTTP via `MCP_TOKEN_GOOGLE_ROHO`.

## Usage

### Initialize System (Run Once)

```bash
python3 {baseDir}/triage.py --action init
```

### Primary Event-Driven Trigger (Gmail PubSub -> /hooks/wake)

Per `SPEC-EVDRV-002`, incoming email delivery is event-driven via Gmail PubSub watcher pushed to `/hooks/gmail-pubsub` -> `/hooks/wake`. When Roho receives an event wake turn with payload `{"event_type": "gmail_pubsub_wake", "history_id": "<ID>"}`, Roho executes:

```bash
python3 {baseDir}/triage.py --action event-triage --history-id <HISTORY_ID> --limit 15
```

This performs real-time delta processing via `google_mail_get_history`, extracting and triaging only new messages added since `start_history_id`, dispatching domain tasks to Amara/Rob as needed, and posting real-time delta updates to `#agent-roho`.

### Scheduled Reconciliation Safety Net (triage-report / silent-when-clean)

Per `SPEC-EVDRV-004` and `REQ-CRON-013`, scheduled runs (`0 */6 * * *`) use `--action triage-report` as a reconciliation backstop to catch dropped events or sync gaps:

```bash
python3 {baseDir}/triage.py --action triage-report --limit 15
```

Performs inbox triage and outputs a structured JSON report. If all emails are already triaged and no new action items exist, the reconciliation run exits silently without posting to Mattermost.

### Executive Summary to Mattermost (#agent-roho)

When processing triage reports with actionable items, post executive summaries with `mattermost-bridge` to channel `agent-roho`. Do not email scheduled executive summaries.

Canonical payload text lives in `config/cron-payloads/gmail-executive-triage.payload.txt`.

### Get Status (Count Stacks)

```bash
python3 {baseDir}/triage.py --action status
```

### Send Email

```bash
python3 {baseDir}/triage.py --action send --to "recipient@example.com" --subject "Subject" --body-markdown "**Hello**, please see the update below."
```

The body must be standard Markdown. The execution layer converts it to rich HTML and a plain-text fallback automatically. **Do not write raw HTML.** `don@chimexhldg.com` is automatically CC'd on every outbound email.

Optionally CC additional recipients (comma-separated):

```bash
python3 {baseDir}/triage.py --action send --to "client@example.com" --cc "colleague@example.com" --subject "Update" --body-markdown "* Item 1\n* Item 2"
```

### Draft a Reply

```bash
python3 {baseDir}/triage.py --action draft --thread-id <THREAD_ID> --body "Thanks, I'll review this week."
```

### List Labels

```bash
python3 {baseDir}/triage.py --action labels
```

### Get Digest

```bash
python3 {baseDir}/triage.py --action digest
```

### Download Email Attachment (SPEC-GMAIL-001 C3)

Download binary content for an email attachment part (base64 decoded and saved to disk):

```bash
python3 {baseDir}/triage.py --action download-attachment --message-id <MSG_ID> --part-id <PART_ID> [--out-dir ./downloads]
```

### Track Important Threads (SPEC-GMAIL-001 C4)

Reads full message chains for high-priority items (`01_Action`, `02_Waiting`), extracting sender history, full body text, and attachments across the thread:

```bash
python3 {baseDir}/triage.py --action track-threads --limit 10
```

## Email Operations (EMAIL-OPS-001)

Use `email_ops.py` for **managed email workflows** with approval gating, token optimization, and audit trails.

### Send with Approval Gating (preferred for outbound)

Internal emails (`@chimexhldg.com`) are auto-approved. External emails require human approval via Mattermost.

```bash
python3 {baseDir}/email_ops.py --action send-gated --to "vendor@example.com" --subject "Quote Request" --body-markdown "Please send the latest quote for **Project Alpha**."
```

### Ingest & Preprocess Inbox

Fetches new emails, strips HTML/signatures/quoted threads, and structures output for token-efficient consumption.

```bash
python3 {baseDir}/email_ops.py --action ingest --limit 10
```

### Finalize an Approval (called via Mattermost callback)

```bash
python3 {baseDir}/email_ops.py --action finalize --transaction-id <UUID> --decision approve
```

### Check Transaction Status

```bash
python3 {baseDir}/email_ops.py --action status
```

## Output

The script prints JSON summaries to stdout. Return this summary to the user verbatim.
