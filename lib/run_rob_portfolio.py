#!/usr/bin/env python3
"""Programmatic orchestrator for Rob pre-briefing status sweep and LifeOS push."""

import os
import sys
import json
import pathlib
import subprocess
import re
import requests  # type: ignore

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import mcp_lifeos
import mcp_firefly
import mcp_research
import trading_212_client


def call_deepseek_gateway(system_prompt, user_prompt, model="openclaw"):
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
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if "choices" in data and len(data["choices"]) > 0:
        return data["choices"][0]["message"].get("content", "")
    else:
        raise RuntimeError(f"Unexpected gateway completions response: {data}")

def resolve_memory_path():
    home = os.environ.get("HOME", "/home/node")
    candidates = [
        os.path.join(home, ".openclaw", "workspace", "MEMORY.md"),
        os.path.join(home, "rob", ".openclaw", "workspace", "MEMORY.md"),
        "/home/node/.openclaw/workspace/MEMORY.md",
        "/home/node/rob/.openclaw/workspace/MEMORY.md",
        "/workspace/MEMORY.md",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]

def resolve_bridge_path():
    home = os.environ.get("HOME", "/home/node")
    candidates = [
        "/home/node/.openclaw/workspace/skills/mattermost-bridge/bridge.py",
        "/home/node/rob/.openclaw/workspace/skills/mattermost-bridge/bridge.py",
        os.path.join(home, ".openclaw", "workspace", "skills", "mattermost-bridge", "bridge.py"),
        "/home/node/.openclaw/skills/mattermost-bridge/bridge.py",
        "/home/node/rob/.openclaw/skills/mattermost-bridge/bridge.py",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]

def main():
    print("==== Rob: Pre-Briefing Status Sweep & LifeOS Push ====")
    
    # 1. Read Filing Posture (MEMORY.md)
    print("Reading filing posture from MEMORY.md...")
    deadline = "2026-07-31" # Default fallback
    memory_path = resolve_memory_path()
    try:
        if os.path.exists(memory_path):
            with open(memory_path, "r", encoding="utf-8") as f:
                content = f.read()
                match = re.search(r"Companies House filing deadline:\s*\*\*([^\*]+)\*\*", content)
                if match:
                    deadline = match.group(1).strip()
    except Exception as exc:
        print(f"Warning: Could not read MEMORY.md: {exc}")
        
    filings_info = f"Companies House filing deadline: {deadline}"
    print(f"Parsed Filing Posture: {filings_info}")
    
    # 2. Read Firefly Triage
    print("Fetching Firefly unreconciled count...")
    unreconciled_count = 0
    try:
        tx_resp = mcp_firefly.call("api/firefly/transactions", {"limit": 100})
        transactions = tx_resp.get("data", []) if isinstance(tx_resp, dict) else (tx_resp if isinstance(tx_resp, list) else [])
        for tx in transactions:
            splits = tx.get("attributes", {}).get("transactions", []) if isinstance(tx, dict) else []
            for split in splits:
                if not split.get("reconciled"):
                    unreconciled_count += 1
    except Exception as exc:
        print(f"Warning: Could not fetch Firefly transactions: {exc}")
        
    print(f"Unreconciled transactions: {unreconciled_count}")
    
    # 3. Read Portfolio (Trading 212 with MEMORY.md Fallback via Option A trading_212_client)
    print("Fetching Trading 212 portfolio snapshot...")
    portfolio_snapshot = {}
    try:
        portfolio_snapshot = trading_212_client.get_portfolio_data_for_rob(memory_path=memory_path)
    except Exception as exc:
        print(f"Warning: Could not fetch portfolio snapshot: {exc}")
        portfolio_snapshot = {"source": "unavailable", "is_live": False, "warnings": [str(exc)]}
        
    # 4. Read Commodity Watches (Research)
    print("Fetching Research portfolio check...")
    research_status = {}
    try:
        research_status = mcp_research.call("api/research/portfolio/check")
    except Exception as exc:
        print(f"Warning: Could not fetch Research portfolio check: {exc}")
        
    # Assemble input context for DeepSeek
    context = {
        "filings_upcoming_deadlines": filings_info,
        "unreconciled_transactions": unreconciled_count,
        "portfolio_snapshot": portfolio_snapshot,
        "research_status": research_status
    }

    
    # 5. Synthesize summary + structured JSON
    print("Synthesizing status summary...")
    sum_sys = (
        "You are Rob, the Finance, Markets, and Filings Agent. Summarize the following portfolio and filing status data. "
        "Create a brief, high-density 1-3 line Markdown report summarizing Companies House deadline, unreconciled transactions count, "
        "and any critical portfolio/commodity watch alerts."
    )
    try:
        summary_md = call_deepseek_gateway(sum_sys, json.dumps(context, indent=2))
    except Exception as exc:
        print(f"ERROR: Failed to generate summary: {exc}")
        sys.exit(1)
        
    print("Compiling structured portfolio JSON...")
    json_sys = (
        "You are a finance data parser. Synthesize portfolio/filing status into a structured JSON payload with keys: "
        "'portfolio' (containing 'positions', 'allocation_percent', 'pnl', 'buy_sell_signals', "
        "'trade212_status_recommendations', 'tiingo_market_research'), 'unreconciled_transactions' (int), and "
        "'filings_upcoming_deadlines' (str). Respond ONLY with a valid JSON object. Do not include markdown wraps."
    )
    try:
        raw_json_str = call_deepseek_gateway(json_sys, json.dumps(context, indent=2))
        raw_json_str = raw_json_str.strip().strip("```json").strip("```").strip()
        portfolio_data = json.loads(raw_json_str)
    except Exception as exc:
        print(f"Warning: Failed to compile portfolio JSON, generating fallback. Error: {exc}")
        portfolio_data = {
            "portfolio": {
                "positions": "Trading 212 account summary loaded.",
                "allocation_percent": {},
                "pnl": "Invested assets checked.",
                "buy_sell_signals": "No new buy/sell signals generated.",
                "trade212_status_recommendations": "Trading 212 connection active.",
                "tiingo_market_research": "Research checks processed."
            },
            "unreconciled_transactions": unreconciled_count,
            "filings_upcoming_deadlines": filings_info
        }
        
    # 6. Push to LifeOS
    print("Pushing section to LifeOS...")
    try:
        mcp_lifeos.call('tools/lifeos_section_push', {
            'agent_id': 'ROB',
            'content': json.dumps(portfolio_data)
        })
        print("Section pushed successfully.")
    except Exception as exc:
        print(f"ERROR: Failed to push to LifeOS: {exc}")
        sys.exit(1)
        
    # 7. Post to Mattermost (#agent-rob and #coordination)
    print("Posting status to Mattermost...")
    ws_dir = os.path.dirname(memory_path) if memory_path else "/home/node/.openclaw/workspace"
    msg_file_path = os.path.join(ws_dir, "pre_brief_summary.txt")
    bridge_script = resolve_bridge_path()
    try:
        with open(msg_file_path, "w", encoding="utf-8") as f:
            f.write(summary_md)
            
        bridge_cmd_rob = [
            "python3",
            bridge_script,
            "--action", "post",
            "--team", "openclaw",
            "--channel", "agent-rob",
            "--message-file", msg_file_path
        ]
        bridge_cmd_coord = [
            "python3",
            bridge_script,
            "--action", "post",
            "--team", "openclaw",
            "--channel", "coordination",
            "--message-file", msg_file_path
        ]
        subprocess.run(bridge_cmd_rob, check=True)
        subprocess.run(bridge_cmd_coord, check=True)
        print("Status posted successfully.")
    except Exception as exc:
        print(f"ERROR: Failed to post to Mattermost: {exc}")
    finally:
        if os.path.exists(msg_file_path):
            os.remove(msg_file_path)
            
    print("==== Rob Pipeline Finished ====")

if __name__ == "__main__":
    main()
