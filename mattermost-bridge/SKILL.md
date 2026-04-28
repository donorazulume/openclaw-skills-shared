---
name: mattermost-bridge
description: Post messages with file attachments, read channels, manage threads, dispatch tasks, resolve usernames, send DMs, upload files, and join channels via the Mattermost coordination bus.
metadata: {"openclaw":{"requires":{"bins":["python3"],"env":["MATTERMOST_URL","MATTERMOST_BOT_TOKEN"]},"emoji":"💬"}}
---

Use this skill when the agent needs to communicate with other agents, post status updates, dispatch tasks, or read coordination messages on the Mattermost server.

All inter-agent coordination MUST be routed through Mattermost so the Admin (Don) has full visibility.

## Mattermost agent protocol (mandatory)

See **`docs/MATTERMOST-AGENT-PROTOCOL.md`** in this repo and the [canonical doc](https://github.com/donorazulume/openclaw-docker/blob/main/docs/MATTERMOST-AGENT-PROTOCOL.md) in **openclaw-docker**.

- **Channels:** Use **public/team channels** for all **agent↔agent** work. **Always @mention** everyone you address (`@amara`, `@roho`, `@don`, or the bot’s Mattermost handle) so notifications fire and gateways in **oncall** mode wake the right recipient.
- **DMs:** The **`dm`** action is **only for Don** (`--username don`). **Do not** DM other agents or bots — coordinate in a channel with @mentions.
- **`dispatch`:** Still a **channel** post (not a private DM); it adds `@recipient` in the message body for visibility.

### Multi-team servers (Issue #195)

If the Mattermost server has **multiple teams**, set **`MATTERMOST_TEAM_ID`** (or **`MATTERMOST_TEAM_NAME`**) in the agent environment so `--channel agent-amara` resolves to the **correct** team’s channel. Without this, posts can succeed in the API but appear in another team’s channel.

## Post a message to a channel

```bash
python3 {baseDir}/bridge.py --action post --channel coordination --message "Task assigned to Amara: review lease agreement."
```

**403 Forbidden:** If the bot is not a member of the channel, the bridge **joins the bot and retries** once (default). Use `--no-auto-join` only if you must not auto-join.

```bash
python3 {baseDir}/bridge.py --action post --channel agent-roho --message "Summary" --no-auto-join
```

## Join the bot to a channel (explicit)

Use when you prefer to join before posting, or when debugging:

```bash
python3 {baseDir}/bridge.py --action join --channel agent-roho
```

## Resolve a username to a user id

Use for cron `to` fields, integrations, or before targeting a user by id:

```bash
python3 {baseDir}/bridge.py --action resolve-user --username don
```

Returns JSON with `user.id`, `username`, etc.

## Send a direct message (Don only)

For a **true DM** (private **Don ↔ this agent**), use **`--username don` only**. Do not DM other agents or bots.

`dispatch` posts to a **channel** and adds `@recipient` in the text — that is **not** a DM.

```bash
python3 {baseDir}/bridge.py --action dm --username don --message "Private note for you."
```

## Post with structured task dispatch

```bash
python3 {baseDir}/bridge.py --action dispatch --channel coordination --recipient amara --task-id "TASK-001" --priority high --message "Review Q1 lease renewals for Chimex properties."
```

## Read recent messages from a channel

```bash
python3 {baseDir}/bridge.py --action read --channel coordination --limit 20
```

## Reply in a thread

```bash
python3 {baseDir}/bridge.py --action thread --post-id "abc123def456" --message "ACK — starting work on this now."
```

## List available channels

```bash
python3 {baseDir}/bridge.py --action channels
```

## Health check

```bash
python3 {baseDir}/bridge.py --action health
```

## Add a reaction to a message (emoji)

Uses `POST /api/v4/reactions`. On **403**, joins the bot to the post’s channel and retries once (default), same as `post`.

```bash
python3 {baseDir}/bridge.py --action react --post-id abc123def456 --emoji +1
```

`--emoji` accepts Mattermost names (`+1`, `white_check_mark`) or `👍` (mapped to `+1`). Use `--no-auto-join` to disable join-and-retry.

## Attach files to a post (SPEC-MMATT-001)

Use `--file-path` to attach one or more files (max 5) to any `post`, `dispatch`, `thread`, or `dm` action. Files are uploaded via the Mattermost Files API before the post is created.

```bash
python3 {baseDir}/bridge.py --action post --channel agent-roho --message "Daily report attached." --file-path /tmp/report.html
```

Multiple files:

```bash
python3 {baseDir}/bridge.py --action post --channel coordination --message "Report + data" --file-path /tmp/report.html /tmp/data.csv
```

Attach a file to a DM:

```bash
python3 {baseDir}/bridge.py --action dm --username don --message "Private report for review." --file-path /tmp/sensitive-report.pdf
```

Attach a file to a thread reply:

```bash
python3 {baseDir}/bridge.py --action thread --post-id abc123def456 --message "Updated analysis attached." --file-path /tmp/analysis.html
```

Attach a file to a task dispatch:

```bash
python3 {baseDir}/bridge.py --action dispatch --channel coordination --recipient amara --priority high --message "Lease document for review." --file-path /tmp/lease.pdf
```

## Upload files without posting

Use `--action upload` to pre-upload files and get `file_id` values without creating a post:

```bash
python3 {baseDir}/bridge.py --action upload --channel coordination --file-path /tmp/report.html
```

Returns JSON with `file_ids` for use in custom integrations.

## File attachment limits

- Maximum **5 files** per post (Mattermost server default)
- Maximum **50 MB** per file (configurable via `MATTERMOST_MAX_FILE_SIZE_MB` env var)
- Zero-byte files are rejected
- Path traversal (`../`) is blocked
- MIME type is auto-detected from file extension
- On 403, the bridge auto-joins the channel and retries the upload (same as post)

## Channel conventions

| Channel | Purpose |
|---------|---------|
| `coordination` | All agent-to-agent delegation and task handoffs |
| `alerts` | Error logs, critical failures, urgent escalations |
| `agent-roho` | Roho's internal monologue and debug output |
| `agent-amara` | Amara's internal monologue and debug output |
| `agent-hmrc` | HMRC Agent status and filing updates |
| `agent-letter-analyst` | Letter Analyst processing status |
| `agent-droid` | Droid mobile operations log |

## Message protocol

Dispatch messages use a JSON payload in a code block:

```json
{
  "sender": "roho",
  "recipient": "amara",
  "task_id": "TASK-001",
  "payload": "Review Q1 lease renewals",
  "priority": "high"
}
```

## Fallback behaviour

If Mattermost is unreachable, the bridge logs the message to stderr and returns an `ERR_COMM_FALLBACK` status. The agent should retry or fall back to direct HTTP bridges.

## More help

- Repo: `docs/MATTERMOST-TROUBLESHOOTING.md` — 403 errors, private channels, admin steps.
