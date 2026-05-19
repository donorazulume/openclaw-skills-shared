#!/usr/bin/env python3
"""
clickup-manager — LifeOS Enforcer for ClickUp (PARA + GTD + Agile).

Heavy-lifter pattern: all ClickUp API logic lives here; the OpenCLAW agent
only triggers CLI commands.

Environment variables (injected via Doppler):
    CLICKUP_API_KEY   ClickUp personal API token
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import pytz
import requests
from dateutil import parser as dtparser
# SPEC-GAUTH-001 v2.0.0 (#323/#324): clickup-manager no longer mints Google OAuth
# credentials. Calendar access goes through openclaw-mcp-google via skills/lib/mcp_google.py.

# Add shared lib to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'lib'))
import mcp_google

# ── Logging ──────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("clickup-lifeos")

# ── Constants ────────────────────────────────────────────────────────

API_BASE = "https://api.clickup.com/api/v2"

# LifeOS Architecture — PARA + Agile
LIFEOS_STRUCTURE = {
    "Second Brain": {
        "folders": {
            "00 Inbox": ["Capture"],
            "01 Projects": ["Active Projects"],
            "02 Areas": ["Health", "Finance", "Career", "Relationships"],
            "03 Resources": ["Learning", "References", "Templates"],
            "04 Archives": ["Completed", "On Hold"],
        },
    },
    "Dev Studio": {
        "folders": {
            "Backlog": ["Bugs", "Features", "Tech Debt"],
            "Sprint": ["Current Sprint"],
            "Done": ["Shipped"],
        },
    },
}

# GTD context tags
CONTEXT_TAGS = ["@DeepWork", "@Admin", "@Errands", "@Waiting", "@Someday"]

# ── Auth & HTTP Helpers ──────────────────────────────────────────────

_SESSION = None


def _api_key() -> str:
    key = os.environ.get("CLICKUP_API_KEY", "").strip()
    if not key:
        sys.exit(
            "ERROR: CLICKUP_API_KEY is not set.\n"
            "Store your ClickUp personal API token in Doppler "
            "(openclaw-docker project) and restart the container."
        )
    return key


def _headers() -> dict[str, str]:
    return {"Authorization": _api_key(), "Content-Type": "application/json"}


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update(_headers())
    return _SESSION


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{API_BASE}{path}"
    resp = _get_session().get(url, params=params, timeout=30)
    if resp.status_code == 401:
        sys.exit("ERROR: ClickUp returned 401 — check CLICKUP_API_KEY.")
    resp.raise_for_status()
    return resp.json()


def _post(path: str, body: dict[str, Any]) -> Any:
    url = f"{API_BASE}{path}"
    resp = _get_session().post(url, json=body, timeout=30)
    if resp.status_code == 401:
        sys.exit("ERROR: ClickUp returned 401 — check CLICKUP_API_KEY.")
    resp.raise_for_status()
    return resp.json()


def _put(path: str, body: dict[str, Any]) -> Any:
    url = f"{API_BASE}{path}"
    resp = _get_session().put(url, json=body, timeout=30)
    if resp.status_code == 401:
        sys.exit("ERROR: ClickUp returned 401 — check CLICKUP_API_KEY.")
    resp.raise_for_status()
    return resp.json()


def _delete(path: str) -> None:
    url = f"{API_BASE}{path}"
    resp = _get_session().delete(url, timeout=30)
    if resp.status_code == 401:
        sys.exit("ERROR: ClickUp returned 401 — check CLICKUP_API_KEY.")
    resp.raise_for_status()


# ── Workspace Helpers ────────────────────────────────────────────────


def get_team_id() -> str:
    data = _get("/team")
    teams = data.get("teams", [])
    if not teams:
        sys.exit("ERROR: No ClickUp workspaces found for this API key.")
    return teams[0]["id"]


def _get_spaces(team_id: str) -> list[dict]:
    return _get(f"/team/{team_id}/space", params={"archived": "false"}).get(
        "spaces", []
    )


def _get_folders(space_id: str) -> list[dict]:
    return _get(f"/space/{space_id}/folder", params={"archived": "false"}).get(
        "folders", []
    )


def _get_lists(folder_id: str) -> list[dict]:
    return _get(f"/folder/{folder_id}/list", params={"archived": "false"}).get(
        "lists", []
    )


def _find_space(spaces: list[dict], name: str) -> dict | None:
    return next((s for s in spaces if s["name"] == name), None)


def _find_folder(folders: list[dict], name: str) -> dict | None:
    return next((f for f in folders if f["name"] == name), None)


def _find_list(lists: list[dict], name: str) -> dict | None:
    return next((l for l in lists if l["name"] == name), None)


def expert_judgment(task: dict[str, Any]) -> bool:
    """Judge if a task is stale or misconfigured."""
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    # Check staleness
    updated = task.get("date_updated")
    if updated:
        age_days = (now_ms - int(updated)) / (1000 * 86400)
        if age_days > 14:
            return True

    # Check for High/Urgent priority with no due date
    # Priority object structure: {"priority": "urgent", "color": "#..."}
    prio_obj = task.get("priority")
    if isinstance(prio_obj, dict):
        prio_label = prio_obj.get("priority", "none").lower()
    else:
        prio_label = "none"

    if prio_label in ["urgent", "high"] and not task.get("due_date"):
        log.warning("Expert Judgment: Urgent task %s has no due date!", task.get("id"))
        return True

    # Check for blocker tag
    tags = [t.get("name", "").lower() for t in task.get("tags", [])]
    if "blocker" in tags:
        return True

    return False


# ── LifeOS Refactor ─────────────────────────────────────────────────


def _ensure_space(team_id: str, name: str, spaces: list[dict]) -> str | None:
    """Find or create a Space. Returns the space ID."""
    space = _find_space(spaces, name)
    if space:
        print(f"  [ok] Space: {name}")
        return space["id"]

    print(f"  [+]  Creating Space: {name}")
    try:
        result = _post(
            f"/team/{team_id}/space",
            {
                "name": name,
                "multiple_assignees": True,
                "features": {
                    "due_dates": {"enabled": True},
                    "priorities": {"enabled": True},
                    "tags": {"enabled": True},
                    "time_estimates": {"enabled": True},
                },
            },
        )
        return result["id"]
    except requests.exceptions.HTTPError as e:
        print(f"  [!!] Could not create Space '{name}' — {e}")
        print(f"       (Plan limit reached? Create manually in ClickUp.)")
        return None


def _ensure_folder(space_id: str, name: str, folders: list[dict]) -> str | None:
    """Find or create a Folder. Returns the folder ID."""
    folder = _find_folder(folders, name)
    if folder:
        print(f"    [ok] Folder: {name}")
        return folder["id"]

    print(f"    [+]  Creating Folder: {name}")
    try:
        result = _post(f"/space/{space_id}/folder", {"name": name})
        return result["id"]
    except requests.exceptions.HTTPError as e:
        print(f"    [!!] Could not create Folder '{name}' — {e}")
        return None


def _ensure_list(folder_id: str, name: str, lists: list[dict]) -> None:
    """Find or create a List."""
    lst = _find_list(lists, name)
    if lst:
        print(f"      [ok] List: {name}")
    else:
        print(f"      [+]  Creating List: {name}")
        try:
            _post(f"/folder/{folder_id}/list", {"name": name})
        except requests.exceptions.HTTPError as e:
            print(f"      [!!] Could not create List '{name}' — {e}")


def _ensure_tags(space_id: str, space_name: str) -> None:
    """Ensure context tags exist on a space."""
    existing_tags = {
        t["name"] for t in _get(f"/space/{space_id}/tag").get("tags", [])
    }
    for tag_name in CONTEXT_TAGS:
        if tag_name in existing_tags:
            print(f"    [ok] Tag: {tag_name} ({space_name})")
        else:
            print(f"    [+]  Creating Tag: {tag_name} ({space_name})")
            try:
                _post(f"/space/{space_id}/tag", {"tag": {"name": tag_name}})
            except requests.exceptions.HTTPError:
                log.warning("Could not create tag %s — may already exist", tag_name)


def refactor_workspace(team_id: str) -> None:
    """Create/verify the full LifeOS hierarchy (Spaces, Folders, Lists, Tags)."""
    print("LifeOS Refactor — Verifying Architecture\n")
    spaces = _get_spaces(team_id)

    for space_name, spec in LIFEOS_STRUCTURE.items():
        space_id = _ensure_space(team_id, space_name, spaces)
        if not space_id:
            continue

        # Ensure folders and lists
        folders = _get_folders(space_id)
        for folder_name, list_names in spec["folders"].items():
            folder_id = _ensure_folder(space_id, folder_name, folders)
            if not folder_id:
                continue

            lists = _get_lists(folder_id)
            for list_name in list_names:
                _ensure_list(folder_id, list_name, lists)

    # Ensure context tags exist on each LifeOS space
    print("\n  Verifying context tags...")
    spaces = _get_spaces(team_id)
    for space_name in LIFEOS_STRUCTURE:
        space = _find_space(spaces, space_name)
        if space:
            _ensure_tags(space["id"], space_name)

    print("\nLifeOS Architecture verified. 'Second Brain' and 'Dev Studio' are active.")


# ── Move Task ────────────────────────────────────────────────────────


def _move_task(task_id: str, target_list_id: str) -> str:
    """Move a task to a different list via PUT /task/{id}.

    Uses ``{"list": "<list_id>"}`` which works on all ClickUp plans
    (unlike the Tasks in Multiple Lists endpoint which requires a paid
    ClickApp and returns 403 / TIML_001 when unavailable).

    Returns the name of the source list for logging.
    """
    task = _get(f"/task/{task_id}")
    source_list = task.get("list", {})
    source_list_id = source_list.get("id")
    source_list_name = source_list.get("name", "unknown")

    if source_list_id == target_list_id:
        print(f"Task {task_id} is already in the target list.")
        return source_list_name

    _put(f"/task/{task_id}", {"list": target_list_id})

    return source_list_name


def move_task(task_id: str, target_list_id: str) -> None:
    """CLI-facing move: relocate a task between arbitrary lists."""
    source_name = _move_task(task_id, target_list_id)
    task = _get(f"/task/{task_id}")
    dest_name = task.get("list", {}).get("name", target_list_id)
    print(f"Moved task {task_id}:")
    print(f"  From: {source_name}")
    print(f"  To:   {dest_name}")


# ── Triage (Inbox → PARA) ───────────────────────────────────────────


def triage_task(
    team_id: str, task_id: str, target_folder: str, context_tag: str | None
) -> None:
    """Move a task from 00 Inbox to a PARA folder and apply a context tag."""
    if context_tag and context_tag not in CONTEXT_TAGS:
        sys.exit(
            f"ERROR: Unknown context tag '{context_tag}'. "
            f"Valid: {', '.join(CONTEXT_TAGS)}"
        )

    spaces = _get_spaces(team_id)
    sb = _find_space(spaces, "Second Brain")
    if not sb:
        sys.exit("ERROR: 'Second Brain' space not found. Run --action refactor first.")

    folders = _get_folders(sb["id"])
    target = _find_folder(folders, target_folder)
    if not target:
        available = [f["name"] for f in folders]
        sys.exit(
            f"ERROR: Folder '{target_folder}' not found in Second Brain.\n"
            f"Available: {', '.join(available)}"
        )

    target_lists = _get_lists(target["id"])
    if not target_lists:
        sys.exit(f"ERROR: No lists found in folder '{target_folder}'.")

    target_list_id = target_lists[0]["id"]
    target_list_name = target_lists[0]["name"]

    _move_task(task_id, target_list_id)

    if context_tag:
        try:
            _post(f"/task/{task_id}/tag/{context_tag}", {})
        except requests.exceptions.HTTPError:
            log.warning("Could not apply tag %s — continuing", context_tag)

    print(f"Triaged task {task_id}:")
    print(f"  Moved to: {target_folder} / {target_list_name}")
    if context_tag:
        print(f"  Context:  {context_tag}")


# ── LifeOS Orchestrator (Daily Planning) ────────────────────────────

LIFEOS_TZ = pytz.timezone(os.environ.get("TIMEZONE", "Europe/London"))
DEFAULT_EFFORT_MS = 45 * 60 * 1000  # 45 minutes
SLOT_BUFFER_MINS = 5


def _fetch_calendar_events(day_start: datetime, day_end: datetime) -> list[dict]:
    """Fetch Google Calendar events for a time window via openclaw-mcp-google."""
    body = mcp_google.call(
        "google_calendar_list_events",
        {
            "start": day_start.isoformat(),
            "end": day_end.isoformat(),
            "calendar_id": os.environ.get("GOOGLE_CALENDAR_ID", "primary"),
        },
    )
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        raise RuntimeError(f"MCP Google google_calendar_list_events: {body['error']}")
    return body.get("events") or []


def _parse_event_window(event: dict) -> tuple[datetime, datetime] | None:
    """Extract start/end datetimes from an MCP calendar event, skipping all-day.

    openclaw-mcp-google returns ``start`` / ``end`` as ISO datetime strings (or
    YYYY-MM-DD for all-day events). LifeOS scheduling only cares about timed
    blocks, so we skip anything that doesn't parse with a time component.
    """
    start_raw = event.get("start") or ""
    end_raw = event.get("end") or ""
    if not start_raw or not end_raw or "T" not in start_raw or "T" not in end_raw:
        return None
    return dtparser.parse(start_raw), dtparser.parse(end_raw)


def _subtract_conflicts(
    slots: list[tuple[datetime, datetime]],
    events: list[dict],
) -> list[tuple[datetime, datetime]]:
    """Remove calendar event windows from the available slot list."""
    free: list[tuple[datetime, datetime]] = []
    for slot_start, slot_end in slots:
        current = slot_start
        for ev in events:
            window = _parse_event_window(ev)
            if window is None:
                continue
            ev_start, ev_end = window
            if ev_end <= current or ev_start >= slot_end:
                continue
            if current < ev_start:
                free.append((current, ev_start))
            current = max(current, ev_end)
        if current < slot_end:
            free.append((current, slot_end))
    return free


def plan_day(list_id: str) -> None:
    """LifeOS Orchestrator: schedule pending tasks into available personal slots."""
    now = datetime.now(LIFEOS_TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    # ── 1. Fetch Google Calendar events via openclaw-mcp-google ──────
    log.info("Fetching Google Calendar via openclaw-mcp-google…")
    try:
        events = _fetch_calendar_events(today, tomorrow)
        log.info("Fetched %d calendar event(s) for today.", len(events))
    except Exception as exc:
        log.warning("MCP Google Calendar unavailable — scheduling without conflict check: %s", exc)
        events = []

    # ── 2. Define available LifeOS slots (outside 9–5 UK) ───────────
    morning_start = today.replace(hour=5, minute=0)
    morning_end = today.replace(hour=8, minute=30)
    evening_start = today.replace(hour=17, minute=30)
    evening_end = today.replace(hour=21, minute=0)

    raw_slots = [
        (morning_start, morning_end),
        (evening_start, evening_end),
    ]

    if events:
        free_slots = _subtract_conflicts(raw_slots, events)
    else:
        free_slots = list(raw_slots)

    # ── 3. Fetch and prioritise ClickUp tasks ───────────────────────
    log.info("Fetching ClickUp tasks from list %s…", list_id)
    data = _get(f"/list/{list_id}/task", params={"archived": "false", "subtasks": "true"})
    tasks = data.get("tasks", [])
    pending = [
        t for t in tasks
        if t.get("status", {}).get("status", "").lower() not in ("closed", "done", "complete")
    ]

    def _priority_key(t: dict) -> str:
        p = t.get("priority")
        if isinstance(p, dict):
            return p.get("priority", "4")
        return "4"

    pending.sort(key=_priority_key)
    log.info("Found %d pending task(s) to schedule.", len(pending))

    # ── 4. Slot-fill algorithm ──────────────────────────────────────
    scheduled: list[str] = []
    slot_idx = 0
    cursor = free_slots[0][0] if free_slots else None

    for task in pending:
        if slot_idx >= len(free_slots) or cursor is None:
            break

        est_ms = task.get("time_estimate") or DEFAULT_EFFORT_MS
        duration = timedelta(milliseconds=int(est_ms))

        task_start = cursor
        task_end = task_start + duration

        # Advance to next slot if this task overflows the current one
        while task_end > free_slots[slot_idx][1]:
            slot_idx += 1
            if slot_idx >= len(free_slots):
                break
            cursor = free_slots[slot_idx][0]
            task_start = cursor
            task_end = task_start + duration

        if slot_idx >= len(free_slots):
            break

        _put(f"/task/{task['id']}", {
            "start_date": int(task_start.timestamp() * 1000),
            "due_date": int(task_end.timestamp() * 1000),
        })

        prio = _priority_key(task)
        prio_labels = {"1": "URG", "2": "HI", "3": "NRM", "4": "LOW"}
        label = prio_labels.get(prio, "---")
        scheduled.append(
            f"  [{label:>3}] {task_start.strftime('%H:%M')}–{task_end.strftime('%H:%M')}  {task['name']}"
        )

        cursor = task_end + timedelta(minutes=SLOT_BUFFER_MINS)

    # ── 5. Print compact agenda for the agent ───────────────────────
    overflow = len(pending) - len(scheduled)

    print(f"LifeOS Daily Agenda — {now.strftime('%A %d %b %Y')}")
    print(f"Timezone: {LIFEOS_TZ}  |  9-5 UK work block protected")
    print("=" * 55)

    if free_slots:
        print("\nAvailable windows:")
        for s, e in free_slots:
            print(f"  {s.strftime('%H:%M')}–{e.strftime('%H:%M')}")

    if scheduled:
        print(f"\nScheduled ({len(scheduled)} task(s)):")
        for line in scheduled:
            print(line)
    else:
        print("\nNo tasks scheduled — backlog clear or no slots available.")

    if overflow > 0:
        print(f"\n{overflow} task(s) could not fit in today's windows.")

    print(f"\nTotal: {len(scheduled)} scheduled, {overflow} overflow, {len(pending)} pending.")


# ── Executive View (Daily Driver) ───────────────────────────────────


def executive_view(team_id: str) -> None:
    """Hat 2: The Executive — tasks due today, grouped by priority."""
    now = datetime.now(tz=timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    # End of today (23:59:59 UTC)
    eod = now.replace(hour=23, minute=59, second=59, microsecond=0)
    eod_ms = int(eod.timestamp() * 1000)

    # Optimized: Use filtered team tasks endpoint to fetch relevant tasks
    print("Fetching tasks due today or overdue via workspace filter...")
    resp = _get(
        f"/team/{team_id}/task",
        params={
            "page": 0,
            "due_date_lt": str(eod_ms),
            "include_closed": "false",
            "subtasks": "true",
            # "order_by": "due_date", # Optional
        }
    )
    all_tasks = resp.get("tasks", [])

    # Deduplicate by task ID
    seen: set[str] = set()
    tasks: list[dict] = []
    for t in all_tasks:
        if t["id"] not in seen:
            seen.add(t["id"])
            tasks.append(t)

    overdue = [t for t in tasks if int(t.get("due_date") or "0") < now_ms]
    today = [t for t in tasks if int(t.get("due_date") or "0") >= now_ms]

    print(f"EXECUTIVE VIEW — {datetime.now(tz=timezone.utc).strftime('%A %d %B %Y')}")
    print(f"{'=' * 60}\n")

    if overdue:
        print(f"OVERDUE ({len(overdue)})")
        print("-" * 40)
        for t in overdue:
            _print_task_line(t)
        print()

    if today:
        print(f"DUE TODAY ({len(today)})")
        print("-" * 40)
        for t in today:
            _print_task_line(t)
        print()

    if not tasks:
        print("No tasks due today. Inbox zero achieved.")

    total = len(tasks)
    print(f"\nTotal: {total} task(s) requiring attention.")


def _print_task_line(t: dict) -> None:
    prio = (t.get("priority") or {}).get("priority", "none")
    prio_label = prio.upper() if prio != "none" else "---"
    status = t.get("status", {}).get("status", "—")
    list_name = t.get("list", {}).get("name", "—")
    tags = ", ".join(tag["name"] for tag in t.get("tags", []))
    tag_str = f" [{tags}]" if tags else ""
    print(f"  [{prio_label:>6}] {t['name']:<45} ({status} | {list_name}){tag_str}")


# ── Weekly Review (Architect Persona) ────────────────────────────────


def weekly_review(team_id: str) -> None:
    """Hat 1: The Architect — audit projects and areas for stale/overdue items."""
    print("WEEKLY REVIEW — The Architect\n")
    print("=" * 60)

    spaces = _get_spaces(team_id)
    sb = _find_space(spaces, "Second Brain")
    if not sb:
        sys.exit("ERROR: 'Second Brain' space not found. Run --action refactor.")

    folders = _get_folders(sb["id"])
    review_folders = ["01 Projects", "02 Areas"]

    for folder_name in review_folders:
        folder = _find_folder(folders, folder_name)
        if not folder:
            print(f"\n[SKIP] {folder_name} — not found")
            continue

        print(f"\n{folder_name}")
        print("-" * 40)

        lists = _get_lists(folder["id"])
        for lst in lists:
            tasks_resp = _get(
                f"/list/{lst['id']}/task",
                params={"archived": "false", "subtasks": "true"},
            )
            tasks = tasks_resp.get("tasks", [])

            open_tasks = [t for t in tasks if t.get("status", {}).get("type") != "closed"]
            overdue = []
            stale = []
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

            for t in open_tasks:
                due = t.get("due_date")
                if due and int(due) < now_ms:
                    overdue.append(t)

                if expert_judgment(t):
                    stale.append(t)

            status_icon = "!!" if overdue else ("?" if stale else "ok")
            print(
                f"  [{status_icon}] {lst['name']}: "
                f"{len(open_tasks)} open, {len(overdue)} overdue, {len(stale)} stale (>14d)"
            )

            for t in overdue[:3]:
                print(f"       OVERDUE: {t['name']} (id: {t['id']})")
            for t in stale[:3]:
                print(f"       STALE:   {t['name']} (id: {t['id']})")

    # Inbox check
    inbox = _find_folder(folders, "00 Inbox")
    if inbox:
        inbox_lists = _get_lists(inbox["id"])
        total_inbox = 0
        for lst in inbox_lists:
            count = lst.get("task_count", 0)
            total_inbox += count
        print(f"\n00 Inbox: {total_inbox} item(s) awaiting triage")

    print(f"\nReview complete. Address overdue (!!) items first, then stale (?) items.")


# ── Repo Audit (Dev Studio) ─────────────────────────────────────────


def repo_audit(team_id: str) -> None:
    """Hat 3: The Manager — audit Dev Studio for bugs, features, and sprint status."""
    print("DEV STUDIO REPOSITORY AUDIT\n")
    print("=" * 60)

    spaces = _get_spaces(team_id)
    dev = _find_space(spaces, "Dev Studio")
    if not dev:
        sys.exit("ERROR: 'Dev Studio' space not found. Run --action refactor first.")

    folders = _get_folders(dev["id"])

    for folder in folders:
        print(f"\n{folder['name']}")
        print("-" * 40)

        lists = _get_lists(folder["id"])
        for lst in lists:
            tasks_resp = _get(
                f"/list/{lst['id']}/task",
                params={"archived": "false"},
            )
            tasks = tasks_resp.get("tasks", [])
            open_tasks = [t for t in tasks if t.get("status", {}).get("type") != "closed"]

            print(f"  {lst['name']}: {len(open_tasks)} open task(s)")
            for t in open_tasks[:5]:
                prio = (t.get("priority") or {}).get("priority", "none")
                status = t.get("status", {}).get("status", "—")
                tags = ", ".join(tag["name"] for tag in t.get("tags", []))
                tag_str = f" [{tags}]" if tags else ""
                print(f"    [{prio:>6}] {t['name']:<40} ({status}){tag_str}")
            if len(open_tasks) > 5:
                print(f"    ... and {len(open_tasks) - 5} more")

    print(f"\nAudit complete.")


# ── Workspace Discovery ──────────────────────────────────────────────


def _get_folderless_lists(space_id: str) -> list[dict]:
    """Get lists that live directly under a space (not inside any folder)."""
    return _get(
        f"/space/{space_id}/list", params={"archived": "false"}
    ).get("lists", [])


def _resolve_space(team_id: str, space_ref: str) -> dict | None:
    """Resolve a space by ID or name (case-insensitive)."""
    spaces = _get_spaces(team_id)
    match = next((s for s in spaces if s["id"] == space_ref), None)
    if match:
        return match
    ref_lower = space_ref.lower()
    return next((s for s in spaces if s["name"].lower() == ref_lower), None)


def list_spaces(team_id: str) -> None:
    """List all ClickUp spaces in the workspace."""
    spaces = _get_spaces(team_id)
    if not spaces:
        print("No spaces found in this workspace.")
        return

    print(f"ClickUp Spaces — {len(spaces)} space(s)\n")
    print(f"{'#':<4} {'Name':<35} {'ID':<15} {'Private'}")
    print("-" * 70)
    for i, s in enumerate(spaces, 1):
        name = s.get("name", "—")
        sid = s.get("id", "—")
        private = "Yes" if s.get("private") else "No"
        print(f"{i:<4} {name:<35} {sid:<15} {private}")
    print(f"\nTotal: {len(spaces)} space(s)")


def list_folders(team_id: str, space_ref: str) -> None:
    """List all folders in a given space (by ID or name)."""
    space = _resolve_space(team_id, space_ref)
    if not space:
        print(f"ERROR: Space '{space_ref}' not found. Use --action list-spaces to see available spaces.")
        sys.exit(1)

    space_name = space["name"]
    space_id = space["id"]
    folders = _get_folders(space_id)

    if not folders:
        print(f"No folders found in space '{space_name}' (id: {space_id}).")
        return

    print(f"Folders in '{space_name}' (id: {space_id}) — {len(folders)} folder(s)\n")
    print(f"{'#':<4} {'Name':<40} {'ID':<15} {'Lists'}")
    print("-" * 75)
    for i, f in enumerate(folders, 1):
        fname = f.get("name", "—")
        fid = f.get("id", "—")
        list_count = f.get("list_count", len(f.get("lists", [])))
        print(f"{i:<4} {fname:<40} {fid:<15} {list_count}")

    folderless = _get_folderless_lists(space_id)
    if folderless:
        print(f"\nFolderless lists in '{space_name}': {len(folderless)}")
        for lst in folderless:
            print(f"  List: {lst['name']}  (id: {lst['id']}, tasks: {lst.get('task_count', '?')})")

    print(f"\nTotal: {len(folders)} folder(s)")


def list_all_lists(team_id: str, space_ref: str | None = None, folder_ref: str | None = None) -> None:
    """List all accessible ClickUp lists across workspace, or scoped to a space/folder."""
    all_lists: list[dict[str, str]] = []

    if folder_ref:
        lists = _get_lists(folder_ref)
        folder_data = _get(f"/folder/{folder_ref}")
        folder_name = folder_data.get("name", folder_ref)
        for lst in lists:
            all_lists.append({
                "space": folder_data.get("space", {}).get("name", "—"),
                "folder": folder_name,
                "list_name": lst["name"],
                "list_id": lst["id"],
                "task_count": str(lst.get("task_count", "?")),
            })
    else:
        spaces = _get_spaces(team_id)
        if space_ref:
            space = _resolve_space(team_id, space_ref)
            if not space:
                print(f"ERROR: Space '{space_ref}' not found.")
                sys.exit(1)
            spaces = [space]

        for space in spaces:
            space_name = space["name"]
            space_id = space["id"]

            folders = _get_folders(space_id)
            for folder in folders:
                lists = _get_lists(folder["id"])
                for lst in lists:
                    all_lists.append({
                        "space": space_name,
                        "folder": folder["name"],
                        "list_name": lst["name"],
                        "list_id": lst["id"],
                        "task_count": str(lst.get("task_count", "?")),
                    })

            folderless = _get_folderless_lists(space_id)
            for lst in folderless:
                all_lists.append({
                    "space": space_name,
                    "folder": "(folderless)",
                    "list_name": lst["name"],
                    "list_id": lst["id"],
                    "task_count": str(lst.get("task_count", "?")),
                })

    if not all_lists:
        print("No lists found.")
        return

    scope = "entire workspace"
    if folder_ref:
        scope = f"folder {folder_ref}"
    elif space_ref:
        scope = f"space '{space_ref}'"

    print(f"All ClickUp Lists ({scope}) — {len(all_lists)} list(s)\n")
    print(f"{'#':<4} {'Space':<20} {'Folder':<25} {'List':<30} {'ID':<15} {'Tasks'}")
    print("-" * 120)
    for i, entry in enumerate(all_lists, 1):
        print(
            f"{i:<4} {entry['space']:<20} {entry['folder']:<25} "
            f"{entry['list_name']:<30} {entry['list_id']:<15} {entry['task_count']}"
        )
    print(f"\nTotal: {len(all_lists)} list(s)")


# ── Search ───────────────────────────────────────────────────────────


def search_lists(team_id: str, query: str) -> None:
    """Search for ClickUp lists by partial or full name (case-insensitive)."""
    query_lower = query.lower()
    matches: list[dict[str, str]] = []

    spaces = _get_spaces(team_id)
    for space in spaces:
        space_name = space["name"]
        space_id = space["id"]

        folders = _get_folders(space_id)
        for folder in folders:
            lists = _get_lists(folder["id"])
            for lst in lists:
                if query_lower in lst["name"].lower():
                    matches.append({
                        "space": space_name,
                        "folder": folder["name"],
                        "list_name": lst["name"],
                        "list_id": lst["id"],
                        "task_count": str(lst.get("task_count", "?")),
                    })

        folderless = _get_folderless_lists(space_id)
        for lst in folderless:
            if query_lower in lst["name"].lower():
                matches.append({
                    "space": space_name,
                    "folder": "(folderless)",
                    "list_name": lst["name"],
                    "list_id": lst["id"],
                    "task_count": str(lst.get("task_count", "?")),
                })

    if not matches:
        print(f"No lists matching '{query}' found in workspace.")
        print("Tip: use --action list-all-lists or --action hierarchy for a full overview.")
        return

    print(f"Search results for '{query}' — {len(matches)} match(es)\n")
    print(f"{'#':<4} {'Space':<20} {'Folder':<25} {'List':<30} {'ID':<15} {'Tasks'}")
    print("-" * 120)
    for i, entry in enumerate(matches, 1):
        print(
            f"{i:<4} {entry['space']:<20} {entry['folder']:<25} "
            f"{entry['list_name']:<30} {entry['list_id']:<15} {entry['task_count']}"
        )
    print(f"\nTotal: {len(matches)} match(es)")


def search_tasks(team_id: str, query: str, list_id: str | None = None) -> None:
    """Search for tasks by name across the workspace or within a specific list."""
    query_lower = query.lower()
    matches: list[dict] = []

    if list_id:
        data = _get(f"/list/{list_id}/task", params={"archived": "false", "subtasks": "true"})
        tasks = data.get("tasks", [])
        for t in tasks:
            if query_lower in t.get("name", "").lower():
                matches.append(t)
    else:
        page = 0
        while True:
            data = _get(
                f"/team/{team_id}/task",
                params={"page": str(page), "include_closed": "false", "subtasks": "true"},
            )
            tasks = data.get("tasks", [])
            if not tasks:
                break
            for t in tasks:
                if query_lower in t.get("name", "").lower():
                    matches.append(t)
            if len(tasks) < 100:
                break
            page += 1

    if not matches:
        scope = f"list {list_id}" if list_id else "workspace"
        print(f"No tasks matching '{query}' found in {scope}.")
        return

    seen: set[str] = set()
    unique: list[dict] = []
    for t in matches:
        if t["id"] not in seen:
            seen.add(t["id"])
            unique.append(t)

    print(f"Task search results for '{query}' — {len(unique)} match(es)\n")
    print(f"{'#':<4} {'Name':<45} {'ID':<12} {'Status':<15} {'List':<25} {'Priority'}")
    print("-" * 120)
    for i, t in enumerate(unique, 1):
        name = t.get("name", "—")[:44]
        tid = t.get("id", "—")
        status = t.get("status", {}).get("status", "—")
        list_name = t.get("list", {}).get("name", "—")
        prio = (t.get("priority") or {}).get("priority", "none")
        print(f"{i:<4} {name:<45} {tid:<12} {status:<15} {list_name:<25} {prio}")
    print(f"\nTotal: {len(unique)} task(s)")


# ── List & Task Detail ───────────────────────────────────────────────


def get_list_info(list_id: str) -> None:
    """Get detailed information about a specific ClickUp list."""
    data = _get(f"/list/{list_id}")

    print("List Details\n")
    print(f"  Name:        {data.get('name', '—')}")
    print(f"  ID:          {data.get('id', '—')}")
    print(f"  Space:       {data.get('space', {}).get('name', '—')} (id: {data.get('space', {}).get('id', '—')})")
    print(f"  Folder:      {data.get('folder', {}).get('name', '—')} (id: {data.get('folder', {}).get('id', '—')})")
    print(f"  Task Count:  {data.get('task_count', '?')}")
    print(f"  Status:      {'Archived' if data.get('archived') else 'Active'}")

    statuses = data.get("statuses", [])
    if statuses:
        status_names = [s.get("status", "—") for s in statuses]
        print(f"  Statuses:    {' → '.join(status_names)}")

    print(f"  URL:         https://app.clickup.com/{data.get('id', '')}")


def delete_task(task_id: str) -> None:
    """Delete a task by ID."""
    task = _get(f"/task/{task_id}")
    task_name = task.get("name", "—")
    task_list = task.get("list", {}).get("name", "—")

    _delete(f"/task/{task_id}")
    print(f"Deleted task [{task_id}]: {task_name}")
    print(f"  Was in list: {task_list}")


# ── Original CRUD Actions (preserved) ───────────────────────────────


def print_hierarchy(team_id: str) -> None:
    """Print the full workspace hierarchy: Spaces -> Folders -> Lists."""
    spaces = _get_spaces(team_id)
    if not spaces:
        print("No spaces found in this workspace.")
        return

    print("ClickUp Workspace Hierarchy\n")
    for space in spaces:
        space_name = space.get("name", "—")
        space_id = space.get("id", "—")
        print(f"Space: {space_name}  (id: {space_id})")

        folders = _get_folders(space_id)
        for folder in folders:
            folder_name = folder.get("name", "—")
            folder_id = folder.get("id", "—")
            print(f"  Folder: {folder_name}  (id: {folder_id})")

            lists = _get_lists(folder_id)
            for lst in lists:
                lst_name = lst.get("name", "—")
                lst_id = lst.get("id", "—")
                task_count = lst.get("task_count", "?")
                print(f"    List: {lst_name}  (id: {lst_id}, tasks: {task_count})")

        # Folderless lists
        fl_lists = _get(
            f"/space/{space_id}/list", params={"archived": "false"}
        ).get("lists", [])
        for lst in fl_lists:
            lst_name = lst.get("name", "—")
            lst_id = lst.get("id", "—")
            task_count = lst.get("task_count", "?")
            print(f"  List (folderless): {lst_name}  (id: {lst_id}, tasks: {task_count})")
        print()

    print("Use the list IDs above when creating tasks or fetching task lists.")


def get_tasks(list_id: str) -> None:
    """Fetch and print tasks from a given list."""
    data = _get(f"/list/{list_id}/task", params={"archived": "false"})
    tasks = data.get("tasks", [])

    if not tasks:
        print(f"No tasks found in list {list_id}.")
        return

    print(f"Tasks in list {list_id} — {len(tasks)} task(s)\n")
    print(f"{'#':<4} {'Status':<18} {'Name':<50} {'ID'}")
    print("-" * 100)

    for i, task in enumerate(tasks, 1):
        name = task.get("name", "—")
        task_id = task.get("id", "—")
        status = task.get("status", {}).get("status", "—")
        print(f"{i:<4} {status:<18} {name:<50} {task_id}")

    print(f"\nTotal: {len(tasks)} task(s)")


def get_task(task_id: str) -> None:
    """Fetch and print details for a single task."""
    task = _get(f"/task/{task_id}")

    print(f"Task Details\n")
    print(f"  Name:        {task.get('name', '—')}")
    print(f"  ID:          {task.get('id', '—')}")
    print(f"  Status:      {task.get('status', {}).get('status', '—')}")
    print(f"  Priority:    {(task.get('priority') or {}).get('priority', 'none')}")
    print(f"  URL:         {task.get('url', '—')}")

    assignees = ", ".join(
        a.get("username", a.get("email", "—")) for a in task.get("assignees", [])
    ) or "unassigned"
    print(f"  Assignees:   {assignees}")

    tags = ", ".join(t.get("name", "") for t in task.get("tags", [])) or "none"
    print(f"  Tags:        {tags}")

    due = task.get("due_date")
    if due:
        dt = datetime.fromtimestamp(int(due) / 1000, tz=timezone.utc)
        print(f"  Due:         {dt.strftime('%Y-%m-%d %H:%M')}")
    else:
        print(f"  Due:         not set")

    desc = task.get("description", "").strip()
    if desc:
        if len(desc) > 500:
            desc = desc[:500] + "…"
        print(f"\n  Description:\n    {desc}")


def create_task(
    list_id: str,
    name: str,
    description: str | None = None,
    context_tag: str | None = None,
    priority: str | None = None,
) -> None:
    """Create a new task in the given list, optionally with context tag."""
    body: dict[str, Any] = {"name": name}
    if description:
        body["description"] = description
    if priority:
        prio_map = {"urgent": 1, "high": 2, "normal": 3, "low": 4, "none": None}
        body["priority"] = prio_map.get(priority.lower(), 3)

    result = _post(f"/list/{list_id}/task", body)

    task_id = result.get("id", "—")
    task_url = result.get("url", "—")

    # Apply context tag if specified
    if context_tag:
        if context_tag not in CONTEXT_TAGS:
            log.warning("Unknown context tag '%s' — skipping", context_tag)
        else:
            try:
                _post(f"/task/{task_id}/tag/{context_tag}", {})
            except requests.exceptions.HTTPError:
                log.warning("Could not apply tag %s", context_tag)

    print(f"Created Task [{task_id}]: {name}")
    print(f"   URL: {task_url}")
    if context_tag:
        print(f"   Context: {context_tag}")


def update_task(task_id: str, **kwargs: Any) -> None:
    """Update a task's fields (status, name, description, priority, due_date)."""
    body: dict[str, Any] = {}

    if kwargs.get("status"):
        body["status"] = kwargs["status"]
    if kwargs.get("name"):
        body["name"] = kwargs["name"]
    if kwargs.get("description"):
        body["description"] = kwargs["description"]
    if kwargs.get("priority"):
        prio_map = {"urgent": 1, "high": 2, "normal": 3, "low": 4, "none": None}
        body["priority"] = prio_map.get(kwargs["priority"].lower(), 3)
    if kwargs.get("due_date"):
        dt = dtparser.parse(kwargs["due_date"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        body["due_date"] = int(dt.timestamp() * 1000)

    if not body:
        print("Nothing to update — no fields provided.")
        return

    result = _put(f"/task/{task_id}", body)
    print(f"Updated Task [{task_id}]")
    for key, val in body.items():
        print(f"   {key}: {val}")
    print(f"   URL: {result.get('url', '—')}")


# ── CLI ──────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ClickUp LifeOS Enforcer (PARA + GTD + Agile)"
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=[
            "refactor",
            "hierarchy",
            "triage",
            "move-task",
            "plan-day",
            "executive-view",
            "weekly-review",
            "repo-audit",
            "get-tasks",
            "get-task",
            "get-list",
            "create-task",
            "update-task",
            "delete-task",
            "list-spaces",
            "list-folders",
            "list-all-lists",
            "search-lists",
            "search-tasks",
        ],
        help="Action to perform.",
    )
    parser.add_argument("--list-id", help="ClickUp List ID.")
    parser.add_argument("--task-id", help="ClickUp Task ID.")
    parser.add_argument("--space", help="Space ID or name (list-folders, list-all-lists).")
    parser.add_argument("--folder-id", help="Folder ID (list-all-lists scoping).")
    parser.add_argument("--query", help="Search query (search-lists, search-tasks).")
    parser.add_argument("--name", help="Task name (create/update).")
    parser.add_argument("--desc", help="Task description (create/update).")
    parser.add_argument("--status", help="Task status (update-task).")
    parser.add_argument(
        "--priority",
        help="Task priority: urgent/high/normal/low/none.",
    )
    parser.add_argument("--due-date", help="Due date ISO string (update-task).")
    parser.add_argument(
        "--target",
        help="Target PARA folder for triage (e.g. '01 Projects', '02 Areas').",
    )
    parser.add_argument(
        "--context",
        help="GTD context tag (@DeepWork, @Admin, @Errands, @Waiting, @Someday).",
    )

    args = parser.parse_args()
    team_id = get_team_id()

    if args.action == "refactor":
        refactor_workspace(team_id)

    elif args.action == "hierarchy":
        print_hierarchy(team_id)

    elif args.action == "triage":
        if not args.task_id:
            parser.error("--task-id is required for triage")
        if not args.target:
            parser.error("--target is required for triage (e.g. '01 Projects')")
        triage_task(team_id, args.task_id, args.target, args.context)

    elif args.action == "move-task":
        if not args.task_id:
            parser.error("--task-id is required for move-task")
        if not args.list_id:
            parser.error("--list-id (target) is required for move-task")
        move_task(args.task_id, args.list_id)

    elif args.action == "plan-day":
        if not args.list_id:
            parser.error("--list-id is required for plan-day")
        plan_day(args.list_id)

    elif args.action == "executive-view":
        executive_view(team_id)

    elif args.action == "weekly-review":
        weekly_review(team_id)

    elif args.action == "repo-audit":
        repo_audit(team_id)

    elif args.action == "get-tasks":
        if not args.list_id:
            parser.error("--list-id is required for get-tasks")
        get_tasks(args.list_id)

    elif args.action == "get-task":
        if not args.task_id:
            parser.error("--task-id is required for get-task")
        get_task(args.task_id)

    elif args.action == "create-task":
        if not args.list_id:
            parser.error("--list-id is required for create-task")
        if not args.name:
            parser.error("--name is required for create-task")
        create_task(
            args.list_id, args.name, description=args.desc, context_tag=args.context,
            priority=args.priority,
        )

    elif args.action == "update-task":
        if not args.task_id:
            parser.error("--task-id is required for update-task")
        update_task(
            args.task_id,
            status=args.status,
            name=args.name,
            description=args.desc,
            priority=args.priority,
            due_date=args.due_date,
        )

    elif args.action == "delete-task":
        if not args.task_id:
            parser.error("--task-id is required for delete-task")
        delete_task(args.task_id)

    elif args.action == "list-spaces":
        list_spaces(team_id)

    elif args.action == "list-folders":
        if not args.space:
            parser.error("--space (ID or name) is required for list-folders")
        list_folders(team_id, args.space)

    elif args.action == "list-all-lists":
        list_all_lists(team_id, space_ref=args.space, folder_ref=args.folder_id)

    elif args.action == "search-lists":
        if not args.query:
            parser.error("--query is required for search-lists")
        search_lists(team_id, args.query)

    elif args.action == "search-tasks":
        if not args.query:
            parser.error("--query is required for search-tasks")
        search_tasks(team_id, args.query, list_id=args.list_id)

    elif args.action == "get-list":
        if not args.list_id:
            parser.error("--list-id is required for get-list")
        get_list_info(args.list_id)


if __name__ == "__main__":
    main()
