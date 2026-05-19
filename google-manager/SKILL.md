---
name: google-manager
description: Manage Gmail, Calendar, and Drive with P.A.R.A. enforcement.
metadata: {"openclaw":{"requires":{"bins":["python3"]},"emoji":"📅"}}
---

Use this skill to read/send emails, manage calendar events, and organize Drive files.

## Capabilities

- **Gmail:** Read inbox, search messages, send emails, manage labels (`01_Action`, `02_Waiting`, etc.).
- **Calendar:** List events, add events, reschedule/update events.
- **Drive:** Overview of full folder structure, list folder contents, find files, create P.A.R.A. folders, rename and organize files.

## Required Environment Variables

Per SPEC-GAUTH-001 v2.0.0 (#323/#324) all Google operations go through
`openclaw-mcp-google`. This skill no longer holds OAuth credentials.

`MCP_GOOGLE_URL` — Base URL (default `http://openclaw-mcp-google:8103`).
`MCP_TOKEN_GOOGLE_ROHO` — Bearer token for `openclaw-mcp-google` (Doppler-injected).

## Usage

### Gmail: Triage Inbox

```bash
python3 {baseDir}/manager.py --service gmail --action triage --limit 20
```

### Gmail: Search Messages

Search using standard Gmail query syntax (e.g., `from:user`, `subject:invoice`, `is:unread`).

```bash
python3 {baseDir}/manager.py --service gmail --action search --query "from:linda subject:project" --limit 10
```

### Gmail: Send Email (HTML-first — MANDATORY for all outbound communications)

Always use `--body-markdown` so the email is delivered as HTML with a plain-text fallback.
`--body` is a legacy plain-text-only fallback — avoid it for external or high-status emails.

```bash
python3 {baseDir}/manager.py --service gmail --action send \
  --to "recipient@example.com" \
  --subject "Weekly Report" \
  --body-markdown "## Highlights\n\n- **Item 1**: Done\n- Item 2: In progress"
```

### Gmail: Create Labels

```bash
python3 {baseDir}/manager.py --service gmail --action create-labels
```

### Calendar: Daily Agenda

```bash
python3 {baseDir}/manager.py --service calendar --action list --time-min "today"
```

### Calendar: Add Event

```bash
python3 {baseDir}/manager.py --service calendar --action add --summary "Meeting" --start "2025-10-27T10:00:00" --duration 60
```

### Calendar: Update Event

```bash
python3 {baseDir}/manager.py --service calendar --action update --event-id "EVENT_ID" --summary "Updated Meeting"
```

### Drive: Full Structure Overview (start here for any Drive task)

```bash
python3 {baseDir}/manager.py --service drive --action overview
```

### Drive: List Folder Contents

List files inside any folder by name or by Drive folder ID.

```bash
python3 {baseDir}/manager.py --service drive --action list-folder --folder "00_Inbox"
python3 {baseDir}/manager.py --service drive --action list-folder --folder "00_Inbox" --limit 100
python3 {baseDir}/manager.py --service drive --action list-folder --folder "FOLDER_ID"
```

### Drive: Find Files

```bash
python3 {baseDir}/manager.py --service drive --action find --query "quarterly report"
```

### Drive: Download a File

Download any Drive file to the local container filesystem.
- **Native files** (PDF, DOCX, XLSX, images): downloaded as-is.
- **Google Workspace files** (Docs → PDF, Sheets → CSV, Slides → PDF): automatically exported.

```bash
python3 {baseDir}/manager.py --service drive --action download \
  --file-id "FILE_ID" \
  --output-dir "/home/node/.openclaw/downloads"
```

Override the local filename:
```bash
python3 {baseDir}/manager.py --service drive --action download \
  --file-id "FILE_ID" \
  --output-dir "/home/node/.openclaw/downloads" \
  --filename "my_document.pdf"
```

Output JSON: `{"status":"success","local_path":"/home/node/.openclaw/downloads/file.pdf",...}`
Chain directly into document-processor using the `local_path` value.

### Drive: Full PDF → RAG Ingestion Pipeline

```bash
# Step 1: List files in a folder to get IDs
python3 {baseDir}/manager.py --service drive --action list-folder --folder "00_Inbox"

# Step 2: Download a specific file
python3 {baseDir}/manager.py --service drive --action download \
  --file-id "FILE_ID" --output-dir "/home/node/.openclaw/downloads"

# Step 3: Convert PDF to Markdown
python3 {skillsDir}/document-processor/processor.py \
  --action convert \
  --input "/home/node/.openclaw/downloads/document.pdf" \
  --output "/home/node/.openclaw/downloads"

# Step 4: Ingest into RAG knowledge base
python3 {skillsDir}/rag-brain-manager/manager.py \
  --action ingest \
  --collection letters \
  --file "/home/node/.openclaw/downloads/document.md" \
  --source-name "2025-04-22_Precise-Mortgages_Arrears"
```

### Drive: Initialize P.A.R.A.

```bash
python3 {baseDir}/manager.py --service drive --action init-para
```

### Drive: Organize & Rename File

```bash
python3 {baseDir}/manager.py --service drive --action organize --file-id "FILE_ID" --target-folder "01_Projects/NewApp" --rename "Tech Spec"
```

## Output

The script prints plain-text summaries to stdout. Return this summary to the user verbatim.
