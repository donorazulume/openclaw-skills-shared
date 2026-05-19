---
name: gmail-executive
description: Manage Gmail using the Executive Triage System (ETS) — Labeling, Drafting, Sending, and P.A.R.A. Sorting.
metadata: {"openclaw":{"requires":{"bins":["python3"]},"emoji":"📧"}}
---

Use this skill to triage the Inbox, send emails, manage labels, and maintain an executive email workflow.

## Executive Gmail Triage directive (`triage` / `triage-report`)

These actions implement the cron / executive workflow rules:

| Rule | Behavior in `triage.py` |
|------|-------------------------|
| **Unread only** | `users.messages.list` uses **both** system labels `INBOX` and `UNREAD` (Gmail ANDs `labelIds`). Only unread mail in the inbox is scanned, classified, and reported. Read messages left in INBOX are **not** processed. |
| **Mark as read when triaged** | When a message is classified and moved to an ETS label, `messages.batchModify` removes **`INBOX`** and **`UNREAD`**, so triaged items are marked read. Messages that **do not** match any rule stay unread in INBOX. |

Other actions (`status`, `digest`, `ingest`, …) are not bound by this unread-only scope unless documented on those actions.

## Capabilities

- **Initialize:** Sets up the required labels (`01_Action`, `02_Waiting`, `03_Read`, `PARA/…`).
- **Triage:** Scans **unread** INBOX mail only, applies rule-based classification with expert priority detection (urgent/deadline → `01_Action`, pending approval → `02_Waiting`, newsletters → `03_Read`, invoices → `PARA/Areas`), moves matching messages to Stacks and **marks them read**.
- **Send:** Compose and send emails. Provide the body in **standard Markdown** — the execution layer converts it to HTML and plain-text automatically. **Do not use HTML.** `don@chimexhldg.com` is always CC'd. Optional **file attachments** (`--attach`, repeatable) send as `multipart/mixed` with validation (size, extension allowlist; see **Attachments** below).
- **Draft:** Creates draft replies for specific threads (uses `gmail.compose`).
- **Labels:** List all Gmail labels.
- **Digest:** Lists unread items in `01_Action` and `03_Read`.
- **Status:** Count unread messages per ETS stack.

## Required Environment Variables

Per SPEC-GAUTH-001 v2.0.0 (#323/#324), this skill does **not** hold Google OAuth
credentials. Every Gmail operation routes through `openclaw-mcp-google` over HTTP.

`MCP_GOOGLE_URL` — Base URL of the MCP Google service (default `http://openclaw-mcp-google:8103`).
`MCP_TOKEN_GOOGLE_ROHO` — Bearer token for `openclaw-mcp-google`.

`GOOGLE_TOKEN_JSON` and `GMAIL_TOKEN_JSON` are retired from the gateway / agent
containers — only the `openclaw-mcp-google` container ever sees them.

## Usage

### Initialize System (Run Once)

```bash
python3 {baseDir}/triage.py --action init
```

### Run Triage (Sort Inbox)

```bash
python3 {baseDir}/triage.py --action triage --limit 15
```

Processes **unread** INBOX messages only (see **Executive Gmail Triage directive** above). For cron: use `--limit 10` or `--limit 15` to avoid TPM/RPM spikes. Run more frequently (e.g. every 15 min) instead of large batches hourly.

### Run Triage with Executive Report (for cron / agent consumption)

```bash
python3 {baseDir}/triage.py --action triage-report --limit 15
```

Same **unread-only** scan and **mark-as-read-on-move** behavior as `--action triage`, then outputs structured JSON: every processed email with sender, subject, snippet, classification label, importance level, and — for high-importance emails (`01_Action`, `PARA/Areas`) — the full body text (truncated to 3 000 chars). Use this action in cron jobs so the agent can compose an executive summary, query the RAG brain for context on important items, and post the summary to Mattermost.

### Scheduled job: executive summary to Mattermost (#agent-roho) — Issue #177

The cron **`payload.message`** must instruct Roho to run **`triage-report`** (not `triage`/`status` alone), then compose a full executive summary and post it with **`mattermost-bridge`** to channel **`agent-roho`**. **Do not** email that executive summary for this scheduled run.

Canonical payload text lives in the repo at **`config/cron-payloads/gmail-executive-triage.payload.txt`**. On the agent container (after entrypoint / deploy sync) the same file exists at **`/home/node/.openclaw/cron-payloads/gmail-executive-triage.payload.txt`** and **`/home/node/.openclaw/workspace/config/cron-payloads/gmail-executive-triage.payload.txt`**.

Apply it to the live job:

```bash
python3 ~/.openclaw/skills/cron-manager/manager.py --action set-message \
  --job-id "<GMAIL_TRIAGE_JOB_UUID>" \
  --file /home/node/.openclaw/cron-payloads/gmail-executive-triage.payload.txt
```

Then run **`python3 ~/.openclaw/skills/cron-manager/manager.py --action diagnose`** — it warns if `triage-report` / Mattermost posting instructions are missing.

See also: **`docs/CRON-GMAIL-EXECUTIVE-TRIAGE.md`**.

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

Attach one or more files (PDF, Office, images, ZIP, etc. — validated; max size per Gmail):

```bash
python3 {baseDir}/triage.py --action send --to "client@example.com" --subject "Signed agreement" \
  --body-markdown "Please find the agreement attached." \
  --attach "/path/to/contract.pdf"
```

List attachment metadata on a stored message:

```bash
python3 {baseDir}/triage.py --action list-attachments --message-id "<MESSAGE_ID>"
```

Download an attachment by id from that listing:

```bash
python3 {baseDir}/triage.py --action download-attachment \
  --message-id "<MESSAGE_ID>" --attachment-id "<ATTACHMENT_ID>" --output "/path/to/out.pdf"
```

Canonical reference: [GMAIL-ATTACHMENTS.md](https://github.com/donorazulume/openclaw-roho/blob/main/docs/GMAIL-ATTACHMENTS.md) in **openclaw-roho** (limits, env vars, security notes).

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

## Email Operations (EMAIL-OPS-001)

Use `email_ops.py` for **managed email workflows** with approval gating, token optimization, and audit trails.

### Send with Approval Gating (preferred for outbound)

Internal emails (`@chimexhldg.com`) are auto-approved. External emails require human approval via Mattermost.

```bash
python3 {baseDir}/email_ops.py --action send-gated --to "vendor@example.com" --subject "Quote Request" --body-markdown "Please send the latest quote for **Project Alpha**."
```

**Attachments:** `send-gated` / `finalize` do not yet persist attachment paths on pending transactions. For outbound mail **with files**, use `triage.py --action send` with `--attach` (or extend `email_ops` in a follow-up).

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
