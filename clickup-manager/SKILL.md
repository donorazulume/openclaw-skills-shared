---
name: clickup-manager
description: Comprehensive ClickUp workspace manager — workspace discovery, search, CRUD, daily planning, PARA triage, and Google Calendar sync.
metadata: {"openclaw":{"requires":{"bins":["python3"]},"emoji":"📅"}}
---

Comprehensive ClickUp workspace manager for Roho. Provides full workspace discovery (browse all spaces, folders, lists), search by name, task CRUD, daily planning with Google Calendar sync, and PARA + GTD + Agile architecture enforcement.

**When you don't know a List ID or Space ID, use the discovery and search actions first** — never ask the user for IDs you can look up yourself.

## Capabilities

### Workspace Discovery (use these to find IDs autonomously)

- **list-spaces**: List all ClickUp spaces in the workspace with their IDs. **Start here when exploring.**
- **list-folders**: List all folders in a given space (by ID or name), including folderless lists.
- **list-all-lists**: Retrieve ALL lists across the entire workspace (or scoped to a space/folder) with names, IDs, and task counts. Use this to get a comprehensive map.
- **hierarchy**: Print the full workspace hierarchy tree (Spaces → Folders → Lists).

### Search (find things by name without needing IDs)

- **search-lists**: Search for a ClickUp list by partial or full name (case-insensitive). Returns matching List IDs. **Use this when you know a list name like "ScanSnap" but not its ID.**
- **search-tasks**: Search for tasks across the entire workspace (or within a specific list) by keyword in the task name.

### Task Management (CRUD)

- **get-tasks**: List tasks in a specific list by ID.
- **get-task**: Get full details of a specific task by ID.
- **get-list**: Get detailed info about a specific list (statuses, space, folder, task count).
- **create-task**: Create a new task in a specific list with optional priority and context tag.
- **update-task**: Update a task's status, priority, name, due date, or description.
- **delete-task**: Permanently delete a task by ID.
- **move-task**: Move a task between arbitrary lists.
- **triage**: Move a task from '00 Inbox' to a PARA folder with context tagging.

### LifeOS Orchestration

- **plan-day**: Schedule pending tasks into morning (05:00–08:30) and evening (17:30–21:00) windows, avoiding calendar conflicts and the 9-5 UK work block.
- **refactor**: One-click setup of the LifeOS hierarchy (Spaces, Folders, Lists, and Tags).
- **executive-view**: "The Executive" — tasks due today, grouped by priority.
- **weekly-review**: "The Architect" — audit all projects and areas for stale/overdue items.
- **repo-audit**: "The Manager" — list technical tasks and bugs in the Dev Studio.

## Usage

### Workspace Discovery

**List all spaces:**
```bash
python3 {baseDir}/manager.py --action list-spaces
```

**List folders in a space (by name or ID):**
```bash
python3 {baseDir}/manager.py --action list-folders --space "Second Brain"
python3 {baseDir}/manager.py --action list-folders --space "90121345678"
```

**List ALL lists in the workspace:**
```bash
python3 {baseDir}/manager.py --action list-all-lists
```

**List all lists within a specific space:**
```bash
python3 {baseDir}/manager.py --action list-all-lists --space "Second Brain"
```

**List all lists within a specific folder:**
```bash
python3 {baseDir}/manager.py --action list-all-lists --folder-id "90121345678"
```

**Print full hierarchy tree:**
```bash
python3 {baseDir}/manager.py --action hierarchy
```

### Search

**Search for a list by name (e.g. find "ScanSnap"):**
```bash
python3 {baseDir}/manager.py --action search-lists --query "ScanSnap"
```

**Search for tasks by keyword across the workspace:**
```bash
python3 {baseDir}/manager.py --action search-tasks --query "invoice"
```

**Search for tasks within a specific list:**
```bash
python3 {baseDir}/manager.py --action search-tasks --query "invoice" --list-id "12345678"
```

### Task Management

**List tasks in a list:**
```bash
python3 {baseDir}/manager.py --action get-tasks --list-id "12345678"
```

**Get task details:**
```bash
python3 {baseDir}/manager.py --action get-task --task-id "abc123"
```

**Get list details:**
```bash
python3 {baseDir}/manager.py --action get-list --list-id "12345678"
```

**Create task:**
```bash
python3 {baseDir}/manager.py --action create-task --list-id "12345678" --name "Review Q3 Report" --desc "Check the financials." --context "@DeepWork"
```

**Update task:**
```bash
python3 {baseDir}/manager.py --action update-task --task-id "abc123" --status "in progress" --priority "high"
```

**Delete task:**
```bash
python3 {baseDir}/manager.py --action delete-task --task-id "abc123"
```

**Move task between lists:**
```bash
python3 {baseDir}/manager.py --action move-task --task-id "abc123" --list-id "901521243355"
```

### Triage & LifeOS

**Triage a task from Inbox:**
```bash
python3 {baseDir}/manager.py --action triage --task-id "abc123" --target "Projects" --context "@DeepWork"
```

**Daily Planning (run at 04:00 AM for a 04:30 AM briefing):**
```bash
python3 {baseDir}/manager.py --action plan-day --list-id "$CLICKUP_LIST_ID"
```

**Initialize/Fix LifeOS Structure:**
```bash
python3 {baseDir}/manager.py --action refactor
```

**Get Today's Executive Agenda:**
```bash
python3 {baseDir}/manager.py --action executive-view
```

**Weekly Review (Architect Persona):**
```bash
python3 {baseDir}/manager.py --action weekly-review
```

**Dev Studio Repository Audit:**
```bash
python3 {baseDir}/manager.py --action repo-audit
```

## Workflow: Finding a List by Name

When you need to interact with a list but only know its name (e.g. "ScanSnap"):

1. Run `--action search-lists --query "ScanSnap"` to find the List ID
2. Use the returned List ID with `--action get-tasks --list-id "<id>"` or any other action

Never ask the user for an ID you can look up. Use discovery and search first.

## Environment Variables

* `CLICKUP_API_KEY` — Required for API access (stored in Doppler).
* `CLICKUP_TEAM_ID` — Workspace ID (auto-detected if not set).
* `CLICKUP_LIST_ID` — Primary LifeOS Inbox/Task list for plan-day.
* `GOOGLE_TOKEN_JSON` — OAuth2 token for Google Calendar (stored in Doppler).
* `GOOGLE_CALENDAR_ID` — Calendar to check for conflicts (default: `primary`).
* `TIMEZONE` — Scheduling timezone (default: `Europe/London`).

## Output

The script prints plain-text summaries to stdout. Return this summary to the user verbatim.
