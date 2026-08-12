#!/usr/bin/env python3
"""Unified CLI runner script to statefully merge and push sections to LifeOS.

Avoids dynamic writing of python scripts in cron jobs to prevent sandbox preflight blocks.
"""

from __future__ import annotations

import argparse
import json
import sys
import pathlib

# Add current folder to sys.path so we can import mcp_lifeos
_LIB_DIR = str(pathlib.Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import mcp_lifeos  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Statefully merge and push a section to LifeOS")
    parser.add_argument("--key", required=True, help="Section key, e.g. 'inbox' or 'calendar'")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--data", help="JSON data string to write under the key")
    group.add_argument("--file", help="File containing JSON data to write under the key")
    args = parser.parse_args()

    # Load new section data
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            new_data = json.load(f)
    else:
        new_data = json.loads(args.data)

    # 1. Retrieve existing Roho sections for today
    res = mcp_lifeos.call("tools/lifeos_state_get", {"key": "roho_today_sections"})
    state_val = res.get("value") if isinstance(res, dict) else None
    data = json.loads(state_val) if state_val else {}

    # 2. Update/insert section
    data[args.key] = new_data

    # 3. Save updated combined sections to state
    mcp_lifeos.call("tools/lifeos_state_set", {
        "key": "roho_today_sections",
        "value": json.dumps(data)
    })

    # 4. Push to LifeOS
    push_res = mcp_lifeos.call("tools/lifeos_section_push", {
        "agent_id": "ROHO",
        "content": json.dumps(data)
    })
    print(json.dumps({"status": "SUCCESS", "response": push_res}))


if __name__ == "__main__":
    main()
