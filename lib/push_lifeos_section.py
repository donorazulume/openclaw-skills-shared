#!/usr/bin/env python3
"""Unified CLI runner script to statefully merge and push sections to LifeOS.

Avoids dynamic writing of python scripts in cron jobs to prevent sandbox preflight blocks.
Enforces LifeOS SPEC-LIFEOS-CORE multi-agent contribution model and section key ownership.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import pathlib
import sys

# Add current folder to sys.path so we can import mcp_lifeos
_LIB_DIR = str(pathlib.Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import mcp_lifeos

# LifeOS Invariant #5: Canonical section ownership mapping
AGENT_SECTION_OWNERSHIP: dict[str, set[str]] = {
    "ROHO": {"calendar", "inbox", "system"},
    "ROB": {"finance", "statutory"},
    "AMARA": {"property"},
    "LETTER_ANALYST": {"documents", "intake"},
}


def normalize_agent_id(raw_agent: str | None) -> str:
    if not raw_agent:
        raw_agent = os.environ.get("OPENCLAW_AGENT_NAME", "roho")
    norm = raw_agent.strip().replace("-", "_").upper()
    if norm in ("LETTERANALYST", "LETTER_ANALYST"):
        return "LETTER_ANALYST"
    return norm


def main() -> None:
    parser = argparse.ArgumentParser(description="Statefully merge and push a section to LifeOS")
    parser.add_argument("--key", required=True, help="Section key, e.g. 'inbox', 'calendar', 'finance', 'property'")
    parser.add_argument(
        "--agent",
        default=None,
        help="Agent identifier (roho, rob, amara, letter-analyst). Defaults to OPENCLAW_AGENT_NAME or roho."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--data", help="JSON data string to write under the key")
    group.add_argument("--file", help="File containing JSON data to write under the key")
    args = parser.parse_args()

    agent_id = normalize_agent_id(args.agent)
    if agent_id not in AGENT_SECTION_OWNERSHIP:
        raise ValueError(f"Unknown agent '{agent_id}'. Allowed agents: {list(AGENT_SECTION_OWNERSHIP.keys())}")

    allowed_keys = AGENT_SECTION_OWNERSHIP[agent_id]
    if args.key not in allowed_keys:
        raise ValueError(
            f"Agent '{agent_id}' is not authorized to push section key '{args.key}'. "
            f"Allowed section keys for {agent_id}: {list(allowed_keys)}"
        )

    # Load new section data
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            new_data = json.load(f)
    else:
        new_data = json.loads(args.data)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state_key = f"{agent_id.lower()}_today_sections_{date_str}"

    # 1. Retrieve existing sections for today for this agent
    res = mcp_lifeos.call("tools/lifeos_state_get", {"key": state_key})
    state_val = res.get("value") if isinstance(res, dict) else None
    data = json.loads(state_val) if state_val else {}

    # 2. Update/insert section
    data[args.key] = new_data

    # 3. Save updated combined sections to state
    mcp_lifeos.call("tools/lifeos_state_set", {
        "key": state_key,
        "value": json.dumps(data)
    })

    # 4. Push to LifeOS
    push_res = mcp_lifeos.call("tools/lifeos_section_push", {
        "agent_id": agent_id,
        "content": json.dumps(data)
    })
    print(json.dumps({"status": "SUCCESS", "agent_id": agent_id, "state_key": state_key, "response": push_res}))


if __name__ == "__main__":
    main()
