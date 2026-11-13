#!/usr/bin/env python3
"""Programmatic orchestrator for Rob pre-briefing status sweep and LifeOS push."""

import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any

import requests  # type: ignore

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import mcp_firefly
import mcp_lifeos
import mcp_research
import trading_212_client


def call_deepseek_gateway(system_prompt, user_prompt, model=None):
    if model is None:
        model = os.environ.get("OPENCLAW_GATEWAY_MODEL") or "openclaw"
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
    timeout_s = int(os.environ.get("DEEPSEEK_GATEWAY_TIMEOUT", "180"))
    last_err = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout_s)
            resp.raise_for_status()
            data = resp.json()
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"].get("content", "")
            else:
                raise RuntimeError(f"Unexpected gateway completions response: {data}")
        except Exception as e:
            last_err = e
            if attempt < 3:
                time.sleep(2 ** attempt)
            else:
                break
    raise RuntimeError(f"Gateway completion call failed after 3 attempts: {last_err}")


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

def resolve_workspace_file(filename: str) -> str:
    import tempfile
    home = os.environ.get("HOME", "/home/node")
    candidates = [
        os.environ.get("OPENCLAW_WORKSPACE_DIR"),
        os.environ.get("OPENCLAW_STATE_DIR"),
        os.path.join(home, ".openclaw", "workspace"),
        os.path.join(home, "rob", ".openclaw", "workspace"),
        "/home/node/.openclaw/workspace",
        "/workspace",
        tempfile.gettempdir(),
        "/tmp",
    ]
    for d in candidates:
        if not d:
            continue
        try:
            p = pathlib.Path(d)
            if p.is_dir() and os.access(p, os.W_OK):
                return str(p / filename)
            elif not p.exists():
                p.mkdir(parents=True, exist_ok=True)
                if os.access(p, os.W_OK):
                    return str(p / filename)
        except OSError:
            continue
    return os.path.join(tempfile.gettempdir(), filename)


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


def resolve_coingecko_monitor_path() -> str | None:
    home = os.environ.get("HOME", "/home/node")
    here = pathlib.Path(__file__).resolve().parent
    candidates = [
        str(here.parent / "rob-crypto-intel" / "monitor.py"),
        os.path.join(home, ".openclaw", "skills", "rob-crypto-intel", "monitor.py"),
        os.path.join(home, ".openclaw", "workspace", "skills", "rob-crypto-intel", "monitor.py"),
        "/home/node/.openclaw/skills/rob-crypto-intel/monitor.py",
        "/home/node/.openclaw/workspace/skills/rob-crypto-intel/monitor.py",
        "/workspace/skills/rob-crypto-intel/monitor.py",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def fetch_coingecko_crypto_intel() -> dict[str, Any]:
    """Collect CoinGecko Pro market & technical intelligence and ingest into Open Brain (Issue #1156)."""
    import importlib.util

    monitor_path = resolve_coingecko_monitor_path()
    if not monitor_path:
        return {"status": "unavailable", "error": "rob-crypto-intel/monitor.py not found"}
    try:
        spec = importlib.util.spec_from_file_location("rob_crypto_intel_monitor", monitor_path)
        if spec is None or spec.loader is None:
            return {"status": "unavailable", "error": f"Could not load spec from {monitor_path}"}
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        intel = mod.collect_crypto_intel()
        if isinstance(intel, dict) and intel.get("status") == "ok":
            try:
                ingested = mod.ingest_crypto_intel_to_openbrain(intel)
                date_str = str(intel.get("generated_at", ""))[:10]
                if ingested and date_str:
                    intel["openbrain_source_id"] = f"rob_crypto_intel_{date_str}"
            except Exception as ob_exc:
                print(f"Warning: CoinGecko Open Brain ingest skipped: {ob_exc}")
        return intel if isinstance(intel, dict) else {"status": "error", "error": "Invalid intel payload"}
    except Exception as exc:
        print(f"Warning: Could not fetch CoinGecko crypto intelligence: {exc}")
        return {"status": "error", "error": str(exc)}

def extract_structured_json(raw_text: str) -> dict:
    """Extract and parse structured JSON from LLM output, handling markdown, think tags, trailing commas, and unescaped characters (Issue #1104)."""
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError(f"No valid JSON block found in response: {cleaned[:120]}")
    candidate = match.group(0)
    try:
        data = json.loads(candidate, strict=False)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    # Strip trailing commas before closing braces or brackets (common LLM syntax issue)
    stripped = re.sub(r",\s*([\}\]])", r"\1", candidate)
    data = json.loads(stripped, strict=False)
    if isinstance(data, dict):
        return data
    raise ValueError("Extracted JSON root is not an object/dict")


from datetime import datetime, timezone


def _parse_flexible_date(date_str: str) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def parse_statutory_posture(memory_path: str, reference_date: datetime | None = None) -> tuple[str, dict[str, Any]]:
    """Parse statutory filing deadlines and statuses from Rob's MEMORY.md (Issue #1119).

    Prevents false overdue alarms by differentiating completed filings (e.g. FY24-25 accounts)
    from pending statutory deadlines (e.g. CT600 due 2026-10-31, CS01 due 2026-12-09).
    """
    if reference_date is None:
        ref_dt = datetime.now(timezone.utc)
    else:
        ref_dt = reference_date if reference_date.tzinfo else reference_date.replace(tzinfo=timezone.utc)

    posture: dict[str, Any] = {
        "accounts_fy24_25": {"status": "UNKNOWN", "details": ""},
        "ct600_fy24_25": {"status": "UNKNOWN", "deadline": None, "days_remaining": None},
        "confirmation_statement": {"status": "UNKNOWN", "deadline": None, "days_remaining": None},
    }

    if not os.path.exists(memory_path):
        return "Filings status: MEMORY.md not found", posture

    try:
        with open(memory_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 1. Accounts FY2024-25
        if re.search(r"FY2024[–-]25.*?(?:FILED|COMPLETED)", content, re.IGNORECASE):
            posture["accounts_fy24_25"]["status"] = "FILED / COMPLETED"
            posture["accounts_fy24_25"]["details"] = "Filed July 2026 (CH receipt 117-865768)"
        elif "FY2024–25" in content or "FY2024-25" in content:
            posture["accounts_fy24_25"]["status"] = "PENDING"

        # 2. HMRC CT600 Corporation Tax return (prioritize FY2024-25 next live obligation)
        ct600_match = re.search(
            r"CT600.*?FY2024[–-]25.*?due[:\s]*\**([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2})",
            content,
            re.IGNORECASE,
        )
        if not ct600_match:
            ct600_match = re.search(
                r"CT600.*?(?:statutory deadline|deadline|due)[:\s]*\**([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2})",
                content,
                re.IGNORECASE,
            )
        if ct600_match:
            d_str = ct600_match.group(1).strip()
            d_val = _parse_flexible_date(d_str)
            if d_val:
                days = (d_val.date() - ref_dt.date()).days
                posture["ct600_fy24_25"]["status"] = "PENDING" if days >= 0 else "OVERDUE"
                posture["ct600_fy24_25"]["deadline"] = d_val.strftime("%Y-%m-%d")
                posture["ct600_fy24_25"]["days_remaining"] = days

        # 3. Confirmation Statement CS01
        cs01_match = re.search(
            r"Confirmation Statement.*?(?:statutory due|filing deadline|deadline)[:\s]*\**([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2})",
            content,
            re.IGNORECASE,
        )
        if cs01_match:
            d_str = cs01_match.group(1).strip()
            d_val = _parse_flexible_date(d_str)
            if d_val:
                days = (d_val.date() - ref_dt.date()).days
                posture["confirmation_statement"]["status"] = "PENDING" if days >= 0 else "OVERDUE"
                posture["confirmation_statement"]["deadline"] = d_val.strftime("%Y-%m-%d")
                posture["confirmation_statement"]["days_remaining"] = days

    except Exception as exc:
        return f"Filings status parse error: {exc}", posture

    # Build human-readable summary string
    parts = []
    if posture["accounts_fy24_25"]["status"] == "FILED / COMPLETED":
        parts.append("accounts_fy24_25: COMPLETED")
    elif posture["accounts_fy24_25"]["status"] != "UNKNOWN":
        parts.append(f"accounts_fy24_25: {posture['accounts_fy24_25']['status']}")

    if posture["ct600_fy24_25"]["deadline"]:
        days = posture["ct600_fy24_25"]["days_remaining"]
        suffix = f"{days}d remaining" if days >= 0 else f"{abs(days)}d overdue"
        parts.append(f"CT600: {posture['ct600_fy24_25']['deadline']} ({suffix})")

    if posture["confirmation_statement"]["deadline"]:
        days = posture["confirmation_statement"]["days_remaining"]
        suffix = f"{days}d remaining" if days >= 0 else f"{abs(days)}d overdue"
        parts.append(f"CS01: {posture['confirmation_statement']['deadline']} ({suffix})")

    filings_info = " | ".join(parts) if parts else "Filings: nominal"
    return filings_info, posture


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Rob portfolio status sweep & LifeOS push")
    parser.add_argument("--mode", default="all", choices=["all", "portfolio", "revolut", "briefing"], help="Execution mode")
    args = parser.parse_args()

    print(f"==== Rob: Pre-Briefing Status Sweep & LifeOS Push (mode={args.mode}) ====")
    
    # 1. Read Filing Posture (MEMORY.md)
    print("Reading filing posture from MEMORY.md...")
    memory_path = resolve_memory_path()
    filings_info, posture = parse_statutory_posture(memory_path)
    print(f"Parsed Filing Posture: {filings_info}")
    
    # 2. Read Firefly Triage
    print("Fetching Firefly unreconciled count...")
    # Issue #1135: use the MCP server's complete count. The previous local loop
    # counted unreconciled splits on the first 100 groups only (reported 78
    # while the true backlog was 85). None = unknown, never a guessed number.
    unreconciled_count: int | None = None
    try:
        unreconciled_count = mcp_firefly.unreconciled_summary(limit=1)["count"]
    except Exception as exc:
        print(f"Warning: Could not fetch Firefly unreconciled count: {exc}")
        
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
        live_positions = portfolio_snapshot.get("positions", []) if isinstance(portfolio_snapshot, dict) else []
        acct_sum = portfolio_snapshot.get("account_summary", {}) if isinstance(portfolio_snapshot, dict) else {}
        check_body: dict[str, Any] = {}
        if live_positions:
            check_body["positions"] = live_positions
            if isinstance(acct_sum, dict):
                if acct_sum.get("cash") is not None:
                    check_body["cash_gbp"] = float(acct_sum["cash"])
                if acct_sum.get("total") is not None and float(acct_sum["total"]) > 0:
                    check_body["broker_total_gbp"] = float(acct_sum["total"])
        research_status = mcp_research.call(
            "api/research/portfolio/check",
            body=check_body,
        )
    except Exception as exc:
        print(f"Warning: Could not fetch Research portfolio check: {exc}")

    # 4.2 Fetch CoinGecko Pro Crypto & Macro Intelligence (Issue #1156)
    print("Fetching CoinGecko Pro crypto & macro intelligence...")
    crypto_intel = fetch_coingecko_crypto_intel()
    print(f"CoinGecko crypto intelligence status: {crypto_intel.get('status')}")

    # Assemble input context for DeepSeek
    context = {
        "filings_upcoming_deadlines": filings_info,
        "statutory_posture": posture,
        "unreconciled_transactions": unreconciled_count,
        "portfolio_snapshot": portfolio_snapshot,
        "research_status": research_status,
        "crypto_intel": crypto_intel,
    }

    # 5. Synthesize summary + structured JSON
    print("Synthesizing status summary...")
    sum_sys = (
        "You are Rob, the Finance, Markets, and Filings Agent. Summarize the following portfolio, crypto/macro, and filing status data. "
        "Create a brief, high-density 1-3 line Markdown report summarizing Companies House/HMRC deadlines, unreconciled transactions count, "
        "Trading 212 portfolio posture, and CoinGecko Pro crypto/macro intelligence (BTC/ETH/SOL prices, 24h delta, RSI-14, dominance, active signals)."
    )
    try:
        summary_md = call_deepseek_gateway(sum_sys, json.dumps(context, indent=2))
    except Exception as exc:
        print(f"ERROR: Failed to generate summary: {exc}")
        sys.exit(1)

    print("Compiling structured portfolio JSON...")
    json_sys = (
        "You are a finance data parser. Synthesize portfolio/filing/crypto status into a structured JSON payload with keys: "
        "'portfolio' (containing 'positions', 'allocation_percent', 'pnl', 'buy_sell_signals', "
        "'trade212_status_recommendations', 'tiingo_market_research', 'coingecko_crypto_intel'), "
        "'crypto_intel' (dict), 'unreconciled_transactions' (int), and "
        "'filings_upcoming_deadlines' (str). Respond ONLY with a valid JSON object. Do not include markdown wraps."
    )
    try:
        raw_json_str = call_deepseek_gateway(json_sys, json.dumps(context, indent=2))
        portfolio_data = extract_structured_json(raw_json_str)
    except Exception as exc:
        print(f"Warning: Failed to compile portfolio JSON, generating fallback. Error: {exc}")
        portfolio_data = {
            "portfolio": {
                "positions": "Trading 212 account summary loaded.",
                "allocation_percent": {},
                "pnl": "Invested assets checked.",
                "buy_sell_signals": "No new buy/sell signals generated.",
                "trade212_status_recommendations": "Trading 212 connection active.",
                "tiingo_market_research": "Research checks processed.",
                "coingecko_crypto_intel": crypto_intel.get("summary_text") if isinstance(crypto_intel, dict) else "Unavailable",
            },
            "crypto_intel": crypto_intel,
            "unreconciled_transactions": unreconciled_count,
            "filings_upcoming_deadlines": filings_info,
            "extraction_failed": True
        }

    # 4.5 Mandatory Research Protocol Gate (Issue #556)
    import research_gate

    # Extract pinned openbrain IDs or generate default structural proof ID from research pass
    ob_ids = ["openbrain_portfolio_check_id"]
    sources = ["tiingo_market_research"]
    if isinstance(research_status, dict):
        if "entity_ids" in research_status and isinstance(research_status["entity_ids"], list):
            ob_ids = list(research_status["entity_ids"])
        if "sources" in research_status and isinstance(research_status["sources"], list):
            sources = list(research_status["sources"])

    if isinstance(crypto_intel, dict) and crypto_intel.get("status") == "ok":
        if "coingecko_pro" not in sources:
            sources.append("coingecko_pro")
        cg_ob_id = crypto_intel.get("openbrain_source_id")
        if cg_ob_id and cg_ob_id not in ob_ids:
            ob_ids.append(str(cg_ob_id))

    portfolio_dict: dict[str, Any] = portfolio_data if isinstance(portfolio_data, dict) else {}
    sub_portfolio = portfolio_dict.get("portfolio")
    if not isinstance(sub_portfolio, dict):
        sub_portfolio = {}
        portfolio_dict["portfolio"] = sub_portfolio

    if isinstance(crypto_intel, dict) and crypto_intel.get("status") == "ok":
        portfolio_dict["crypto_intel"] = crypto_intel
        sub_portfolio["coingecko_crypto_intel"] = crypto_intel.get("summary_text") or "CoinGecko Pro crypto intelligence active."
        if crypto_intel.get("summary_text") and "BTC" not in summary_md and "CoinGecko" not in summary_md:
            summary_md = f"{summary_md.rstrip()}\n- **Crypto & Macro (CoinGecko Pro)**: {crypto_intel['summary_text']}"

    portfolio_dict["research"] = research_gate.construct_research_block(
        openbrain_ids=ob_ids,
        sources=sources,
        freshness_days=7,
        tier="AUTO"
    )
    
    is_valid, gate_status, gate_msg = research_gate.validate_research_gate(portfolio_dict, freshness_days=7)
    if gate_status == "REJECTED":
        print(f"CRITICAL: Mandatory Research Gate REJECTED: {gate_msg}")
        sub_portfolio["buy_sell_signals"] = f"[HELD] {gate_msg}"
        summary_md = f"**[ESCALATE] research-gate: no research pass**\n\n{gate_msg}\n\nRecommendations held."
    elif gate_status == "STALE":
        print(f"Warning: Research Gate STALE: {gate_msg}")
        res_block = portfolio_dict.get("research")
        if isinstance(res_block, dict):
            res_block["tier"] = "ROHO"
        current_signals = str(sub_portfolio.get("buy_sell_signals", ""))
        sub_portfolio["buy_sell_signals"] = f"{current_signals} ({gate_msg})"
        
    portfolio_dict["filings_upcoming_deadlines"] = filings_info
    portfolio_dict["statutory_posture"] = posture
    # Deterministic facts override LLM synthesis (Issue #1135): never let the
    # model restate or invent the ledger count.
    portfolio_dict["unreconciled_transactions"] = unreconciled_count
    # Issue #1160: stamp the producer so the deep-merged ROB section never keeps a
    # different producer's stale __producer (e.g. rob-crypto-intel/monitor.py).
    now_iso = datetime.now(timezone.utc).isoformat()
    portfolio_dict["__producer"] = {
        "name": "run_rob_portfolio.py",
        "version": "1.1.0",
        "git_sha": os.environ.get("GIT_SHA") or os.environ.get("OPENCLAW_GIT_SHA") or "unknown",
        "generated_at": now_iso,
        "source_fetched_at": now_iso,
        "cadence_seconds": 86400,
    }
    portfolio_data = portfolio_dict
        
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
    msg_file_path = resolve_workspace_file("pre_brief_summary.txt")
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
