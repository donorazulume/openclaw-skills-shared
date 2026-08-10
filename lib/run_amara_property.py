#!/usr/bin/env python3
"""Programmatic orchestrator for Amara property mailbox monitoring and LifeOS push."""

import os
import sys
import json
import pathlib
import subprocess
import requests  # type: ignore

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import mcp_lifeos

def resolve_bridge_path():
    home = os.environ.get("HOME", "/home/node")
    candidates = [
        os.path.join(home, ".openclaw", "workspace", "skills", "mattermost-bridge", "bridge.py"),
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

def resolve_manager_path():
    home = os.environ.get("HOME", "/home/node")
    candidates = [
        os.path.join(home, ".openclaw", "skills", "chimex-property-manager", "amara_manager.py"),
        os.path.join(home, ".openclaw", "workspace", "skills", "chimex-property-manager", "amara_manager.py"),
        os.path.join(home, "amara", ".openclaw", "skills", "chimex-property-manager", "amara_manager.py"),
        "/home/node/.openclaw/skills/chimex-property-manager/amara_manager.py",
        "/home/node/amara/.openclaw/skills/chimex-property-manager/amara_manager.py",
        "/workspace/skills/chimex-property-manager/amara_manager.py",
        "/home/node/.openclaw/skills-amara/chimex-property-manager/amara_manager.py",
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
        os.path.join(home, "amara", ".openclaw", "workspace", filename),
        os.path.join(str(base_dir), filename),
        os.path.join("/workspace", filename),
        os.path.join("/home/node/.openclaw/workspace", filename),
    ]
    for c in candidates:
        dir_name = os.path.dirname(c)
        if os.path.exists(dir_name) and os.access(dir_name, os.W_OK):
            return c
    return os.path.join(str(base_dir), filename)

def main():
    print("==== Amara: Property Mail Monitor & LifeOS Push ====")
    
    # 1. Read mailbox
    print("Reading Amara's mailbox...")
    cmd = [
        "python3",
        resolve_manager_path(),
        "--action", "read",
        "--email", "amara@chimexhldg.com",
        "--limit", "15"
    ]
    try:
        res = subprocess.run(cmd, check=True, text=True, capture_output=True, timeout=60)
        mail_output = res.stdout
    except Exception as exc:
        print(f"ERROR: Failed to read mailbox: {exc}")
        sys.exit(1)
        
    if not mail_output.strip():
        mail_output = "No emails found or empty output."
        
    # 2. Summarize
    print("Summarizing property mail logs...")
    sum_sys = (
        "You are Amara, the property manager. Summarize the following mail logs. Identify notable subjects, "
        "urgency/financial/security flags, and action items. Tag any tenant/contractor escalations with [ESCALATE] "
        "clearly so Roho can pick them up."
    )
    try:
        summary_md = call_deepseek_gateway(sum_sys, mail_output)
    except Exception as exc:
        print(f"ERROR: Failed to generate summary: {exc}")
        sys.exit(1)
        
    # 3. Parse into property JSON structure
    print("Compiling structured property JSON...")
    json_sys = (
        "You are a property data parser. Extract property status into a structured JSON payload with keys: "
        "'property' (which must contain 'rent_status', 'maintenance', 'key_dates'). Respond ONLY with a valid "
        "JSON object. Do not include markdown wraps."
    )
    try:
        raw_json_str = call_deepseek_gateway(json_sys, summary_md)
        # Clean up any potential markdown wraps
        raw_json_str = raw_json_str.strip().strip("```json").strip("```").strip()
        property_data = json.loads(raw_json_str)
    except Exception as exc:
        print(f"Warning: Failed to compile property JSON, generating fallback. Error: {exc}")
        property_data = {
            "property": {
                "rent_status": "Email processed. Rent status unparsed.",
                "maintenance": "Email processed. Maintenance logs unparsed.",
                "key_dates": "Email processed. Key dates unparsed."
            }
        }
        
    # 4. Push to LifeOS
    print("Pushing section to LifeOS...")
    try:
        mcp_lifeos.call('tools/lifeos_section_push', {
            'agent_id': 'AMARA',
            'content': json.dumps(property_data)
        })
        print("Section pushed successfully.")
    except Exception as exc:
        print(f"ERROR: Failed to push to LifeOS: {exc}")
        sys.exit(1)
        
    # 5. Post status to Mattermost #agent-amara
    print("Posting status to Mattermost...")
    msg_file_path = resolve_workspace_file("summary_message.txt")
    try:
        with open(msg_file_path, "w", encoding="utf-8") as f:
            f.write(summary_md)
            
        bridge_cmd = [
            "python3",
            resolve_bridge_path(),
            "--action", "post",
            "--team", "openclaw",
            "--channel", "agent-amara",
            "--message-file", msg_file_path
        ]
        subprocess.run(bridge_cmd, check=True)
        print("Status posted successfully.")
    except Exception as exc:
        print(f"ERROR: Failed to post to Mattermost: {exc}")
    finally:
        if os.path.exists(msg_file_path):
            os.remove(msg_file_path)
            
    print("==== Amara Pipeline Finished ====")

if __name__ == "__main__":
    main()
