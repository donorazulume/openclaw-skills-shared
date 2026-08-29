#!/usr/bin/env python3
"""Programmatic orchestrator for compiling and delivering the LifeOS Morning Briefing.

Performs briefing compilation (server-side with local fallbacks), emails a reference copy
to donorazulume@gmail.com, sends an actionable summary DM to Don, and logs the execution.
"""

import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, time

import requests  # type: ignore

# Ensure we can resolve local imports from this directory
lib_dir = str(pathlib.Path(__file__).resolve().parent)
if lib_dir not in sys.path:
    sys.path.insert(0, lib_dir)

import email_utils
import mcp_bible
import mcp_google
import mcp_lifeos
import mcp_m365


def build_bible_section() -> str:
    """Fetch daily devotional reading and verse of the day from openclaw-mcp-bible.
    
    Provides strict graceful degradation so briefing generation never fails even if Bible MCP
    is unreachable or returns an error.
    """
    try:
        devotional = mcp_bible.call("get_daily_devotional")
        if isinstance(devotional, dict) and not devotional.get("error"):
            title = devotional.get("title", "Daily Devotional")
            theme = devotional.get("theme", "")
            date_str = devotional.get("date", "")
            ref = devotional.get("bible_reference", "")
            body = devotional.get("body", "")
            
            section = "\n\n### 📖 Scripture & Daily Devotional (UCB Word For Today)\n"
            if date_str:
                section += f"**Date**: {date_str}\n"
            if theme:
                section += f"**Theme**: {theme}\n"
            section += f"**Title**: {title}\n"
            if ref:
                section += f"**Reading**: {ref}\n"
            if body:
                snippet = body if len(body) <= 500 else body[:500] + "..."
                section += f"\n> {snippet}\n"
            return section
    except Exception as exc:
        print(f"WARNING: build_bible_section failed gracefully: {exc}")
    return ""


def resolve_bridge_path():
    home = os.environ.get("HOME", "/home/node")
    base_dir = pathlib.Path(__file__).resolve().parents[2]
    candidates = [
        os.path.join(home, ".openclaw", "workspace", "skills", "mattermost-bridge", "bridge.py"),
        os.path.join(str(base_dir), "skills", "mattermost-bridge", "bridge.py"),
        "/home/node/.openclaw/workspace/skills/mattermost-bridge/bridge.py",
        os.path.join(home, ".openclaw", "skills", "mattermost-bridge", "bridge.py"),
        "/home/node/.openclaw/skills/mattermost-bridge/bridge.py",
        "/home/node/amara/.openclaw/skills/mattermost-bridge/bridge.py",
        "/home/node/rob/.openclaw/skills/mattermost-bridge/bridge.py",
        "/workspace/skills/mattermost-bridge/bridge.py",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]

def resolve_script_path(rel_path):
    home = os.environ.get("HOME", "/home/node")
    base_dir = pathlib.Path(__file__).resolve().parents[2]
    candidates = [
        os.path.join(home, ".openclaw", "workspace", rel_path),
        os.path.join(str(base_dir), rel_path),
        os.path.join("/workspace", rel_path),
        os.path.join("/home/node/.openclaw/workspace", rel_path),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]

def resolve_workspace_file(filename):
    home = os.environ.get("HOME", "/home/node")
    base_dir = pathlib.Path(__file__).resolve().parents[2]
    candidates = [
        os.path.join(home, ".openclaw", "workspace", filename),
        os.path.join(str(base_dir), filename),
        os.path.join("/workspace", filename),
        os.path.join("/home/node/.openclaw/workspace", filename),
    ]
    for c in candidates:
        dir_name = os.path.dirname(c)
        if os.path.exists(dir_name) and os.access(dir_name, os.W_OK):
            return c
    return os.path.join(str(base_dir), filename)

def call_deepseek_gateway(system_prompt, user_prompt, model="openclaw", max_retries=3):
    url = os.environ.get("DEEPSEEK_GATEWAY_URL", "http://openclaw:18789/v1/chat/completions")
    token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,
    }
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"].get("content", "")
            else:
                raise RuntimeError(f"Unexpected gateway completions response: {data}")
        except Exception as exc:
            last_err = exc
            print(f"Warning: call_deepseek_gateway attempt {attempt}/{max_retries} failed: {exc}")
            if attempt < max_retries:
                import time as _time
                _time.sleep(attempt * 2)
    raise RuntimeError(f"All {max_retries} gateway attempts failed: {last_err}")

def main():
    print("==== Step 1: Compile Today's Briefing ====")
    markdown_brief = ""
    for attempt in range(1, 4):
        try:
            print(f"Requesting compiled briefing from LifeOS MCP (attempt {attempt}/3)...")
            briefing_res = mcp_lifeos.call("tools/lifeos_briefing_get_today")
            # FastMCP returns a JSON string, which mcp_lifeos resolves
            if isinstance(briefing_res, str):
                try:
                    briefing_res = json.loads(briefing_res)
                except Exception:
                    pass
            if isinstance(briefing_res, dict):
                markdown_brief = briefing_res.get("content")
            else:
                markdown_brief = str(briefing_res)
                
            if not markdown_brief or markdown_brief.strip() == "" or "Error" in markdown_brief:
                raise ValueError("Invalid briefing content received from MCP")
            print("Briefing compiled successfully via LifeOS MCP.")
            break
        except Exception as e:
            print(f"WARNING: LifeOS MCP briefing compilation attempt {attempt}/3 failed: {e}")
            if attempt < 3:
                import time as _time
                _time.sleep(attempt * 2)
            else:
                print("Falling back to manual compilation...")
    
    if not markdown_brief:
        
        # Fallback 1: Fetch calendar raw (server-side fetch failed)
        now = datetime.now()
        start_of_day = datetime.combine(now.date(), time.min)
        end_of_day = datetime.combine(now.date(), time.max)
        start_iso = start_of_day.astimezone().isoformat()
        end_iso = end_of_day.astimezone().isoformat()
        events_summary = []
        try:
            events_res = mcp_google.call("google_calendar_list_events", {
                "start": start_iso,
                "end": end_iso,
                "calendar_id": "primary"
            })
            events = events_res.get("events", [])
            for ev in events:
                start_val = ev.get("start")
                start_time = start_val.get("dateTime", start_val.get("date")) if isinstance(start_val, dict) else start_val
                end_val = ev.get("end")
                end_time = ev.get("dateTime", ev.get("date")) if isinstance(end_val, dict) else end_val
                events_summary.append({"summary": ev.get("summary"), "start": start_time, "end": end_time})
        except Exception as cal_err:
            print(f"Fallback Calendar fetch failed: {cal_err}")

        # Fallback 2: Read workspace files
        state_text = ""
        for filename in ["USER.md", "MEMORY.md", "HEARTBEAT.md", "SOUL.md", "LESSONS.md"]:
            path = os.path.join("/home/node/.openclaw/workspace", filename)
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        state_text += f"\n--- {filename} ---\n{f.read()}\n"
                except Exception as read_err:
                    print(f"Could not read {filename}: {read_err}")
                    
        # Fallback 3: Query RAG directly across multi-agent domains
        try:
            openbrain_client = resolve_script_path("skills/openbrain-client/client.py")
            cmd = [
                "python3", openbrain_client,
                "--action", "semantic-query",
                "--collection", "open_brain",
                "--query", "active status, portfolio, property, alerts, priorities",
                "--search-mode", "hybrid",
                "--expand-query",
                "--n-results", "10"
            ]
            rag_output = subprocess.check_output(cmd, text=True, timeout=30)
        except Exception as rag_err:
            print(f"Fallback RAG query failed: {rag_err}")
            rag_output = "No RAG context available."

        system_prompt = (
            "You are Roho, the Executive Coordinator for LifeOS. Your task is to compile a comprehensive, "
            "well-formed daily morning briefing summarizing the status of the entire agent ecosystem "
            "(Roho - Executive, Rob - Financial/Markets/Filings, Amara - Property/Operations). "
            "Include key schedule items, financial posture, property highlights, active alerts, and immediate action items."
        )
        user_prompt = (
            f"Please compile today's LifeOS Morning Briefing based on these multi-agent inputs:\n\n"
            f"--- Calendar Events ---\n{json.dumps(events_summary, indent=2)}\n\n"
            f"--- Direct State Files ---\n{state_text}\n\n"
            f"--- Retrieved Multi-Agent Context ---\n{rag_output}\n"
        )
        try:
            markdown_brief = call_deepseek_gateway(system_prompt, user_prompt, model="openclaw")
        except Exception as llm_err:
            print(f"CRITICAL: Fallback LLM briefing compilation failed: {llm_err}")
            markdown_brief = f"# LifeOS Daily Briefing Fallback\n\nFailed to compile full briefing. Calendar events:\n{json.dumps(events_summary, indent=2)}"

    bible_section = build_bible_section()
    if bible_section and bible_section not in markdown_brief:
        markdown_brief += bible_section

    briefing_file_path = resolve_workspace_file("briefing.md")
    with open(briefing_file_path, "w", encoding="utf-8") as f:
        f.write(markdown_brief)


    print("\n==== Step 2: Deliver Reference Email ====")
    try:
        date_str = datetime.now().strftime("%Y-%m-%d")
        subject_str = f"LifeOS Morning Briefing — {date_str}"
        target_addr = "donorazulume@gmail.com"
        
        # Convert Markdown briefing to rich, well-formed HTML
        html_body = email_utils.markdown_to_html(markdown_brief)
        
        print(f"Delivering reference email via M365 MCP to {target_addr}...")
        try:
            mcp_m365.call("m365_mail_send", {
                "to": [target_addr],
                "subject": subject_str,
                "body_html": html_body
            })
            print(f"Email sent successfully to {target_addr} via M365 MCP tool.")
        except Exception as mcp_err:
            print(f"WARNING: Direct M365 MCP tool call failed: {mcp_err}. Trying M365 proxy endpoint...")
            mcp_m365.request(  # type: ignore # pyright: ignore[reportAttributeAccessIssue]
                "POST",
                "/me/sendMail",
                payload={
                    "message": {
                        "subject": subject_str,
                        "body": {"contentType": "HTML", "content": html_body},
                        "toRecipients": [{"emailAddress": {"address": target_addr}}]
                    },
                    "saveToSentItems": True
                }
            )
            print(f"Email sent successfully to {target_addr} via M365 proxy.")
    except Exception as e:
        print(f"ERROR: Failed to send email: {e}")

    dm_file_path = resolve_workspace_file("dm_message.txt")
    print("\n==== Step 3: Deliver Actionable Summary DM ====")
    try:
        print("Requesting executive summary from LifeOS MCP...")
        summary_res = mcp_lifeos.call("tools/lifeos_briefing_get_executive_summary")
        if isinstance(summary_res, str):
            try:
                summary_res = json.loads(summary_res)
            except Exception:
                pass
        if isinstance(summary_res, dict):
            dm_message = summary_res.get("executive_summary")
        else:
            dm_message = str(summary_res)
            
        if not dm_message or dm_message.strip() == "" or "Error" in dm_message:
            raise ValueError("Invalid summary received from LifeOS MCP")
    except Exception as e:
        print(f"WARNING: LifeOS MCP executive summary failed: {e}. Falling back to gateway query...")
        try:
            system_prompt = (
                "You are a precise summarizer. Summarize the daily briefing into a concise, 3-5 bullet actionable summary "
                "and a single explicit question for Don. The summary must be brief, direct, and action-oriented."
            )
            user_prompt = f"Briefing:\n{markdown_brief}"
            dm_message = call_deepseek_gateway(system_prompt, user_prompt, model="openclaw")
        except Exception as llm_err:
            print(f"ERROR: Fallback summary generation failed: {llm_err}")
            dm_message = "Actionable summary could not be compiled."

    try:
        with open(dm_file_path, "w", encoding="utf-8") as f:
            f.write(dm_message)

        print("Posting DM to Mattermost...")
        dm_cmd = [
            "python3", resolve_bridge_path(),
            "--action", "dm",
            "--username", "don",
            "--message-file", dm_file_path
        ]
        subprocess.run(dm_cmd, check=True)
        print("DM posted successfully.")
    except Exception as e:
        print(f"ERROR: Failed to deliver Mattermost DM: {e}")

    print("\n==== Step 4: Deliver Archive Log ====")
    try:
        date_str = datetime.now().strftime("%Y-%m-%d")
        archive_msg = f"LifeOS Morning Briefing for {date_str} compiled and delivered successfully."
        archive_cmd = [
            "python3", resolve_bridge_path(),
            "--action", "post",
            "--channel", "1ch9knt6w3bc3gkw4a7w33k6qh",
            "--message", archive_msg
        ]
        subprocess.run(archive_cmd, check=True)
        print("Archive log posted successfully.")
    except Exception as e:
        print(f"ERROR: Failed to post archive log: {e}")

    print("\n==== Step 5: Log Delivery to LifeOS MCP ====")
    try:
        mcp_lifeos.call("tools/lifeos_deliver", {"channel": "comms,smtp", "content": "delivered"})
        print("Delivery logged successfully.")
    except Exception as e:
        print(f"ERROR: Failed to log delivery back to LifeOS MCP: {e}")

    # Cleanup temp files
    for path in [briefing_file_path, dm_file_path]:
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                print(f"WARNING: Failed to remove temporary file {path}: {e}")

    print("\n==== Morning Briefing Pipeline Finished ====")

if __name__ == "__main__":
    main()
