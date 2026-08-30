"""Trading 212 Client Library for Rob (Option A — SPEC-TIINGO-002 / Issue #449).

Provides live portfolio data fetching from openclaw-mcp-trade212 with calculated allocations,
PnL metrics, and graceful degradation fallback to MEMORY.md parsing for Rob's analysis pipeline.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import re
import sys
from typing import Any

_LIB_DIR = str(pathlib.Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import mcp_trade212

log = logging.getLogger("openclaw.trading_212_client")

DEFAULT_TIMEOUT_SEC = 15.0


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def resolve_memory_path() -> str:
    """Locate MEMORY.md across standard OpenClaw workspace directory candidates."""
    home = os.environ.get("HOME", "/home/node")
    candidates = [
        os.path.join(home, ".openclaw", "workspace", "MEMORY.md"),
        os.path.join(home, "rob", ".openclaw", "workspace", "MEMORY.md"),
        "/home/node/.openclaw/workspace/MEMORY.md",
        "/home/node/rob/.openclaw/workspace/MEMORY.md",
        "/workspace/MEMORY.md",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def get_account_summary(timeout: float = DEFAULT_TIMEOUT_SEC) -> dict[str, Any]:
    """Fetch live account summary from openclaw-mcp-trade212."""
    res = mcp_trade212.call("api/trade212/account-summary", timeout=timeout)
    if isinstance(res, dict) and "data" in res:
        return res["data"] if isinstance(res["data"], dict) else res
    return res if isinstance(res, dict) else {}


def get_portfolio_positions(timeout: float = DEFAULT_TIMEOUT_SEC) -> list[dict[str, Any]]:
    """Fetch open portfolio positions from openclaw-mcp-trade212 and compute allocations."""
    res = mcp_trade212.call("api/trade212/portfolio", timeout=timeout)
    raw_positions: list[dict[str, Any]] = []
    if isinstance(res, dict):
        raw_data = res.get("data")
        if isinstance(raw_data, list):
            raw_positions = raw_data
        elif isinstance(raw_data, dict):
            raw_positions = raw_data.get("positions", [])
    elif isinstance(res, list):
        raw_positions = res

    positions: list[dict[str, Any]] = []
    total_invested = 0.0

    # First pass: calculate market value per item and total invested value
    for item in raw_positions:
        ticker = str(item.get("ticker") or item.get("symbol") or "").upper()
        quantity = float(item.get("quantity") or item.get("qty") or 0.0)
        avg_price = float(item.get("averagePrice") or item.get("average_price") or item.get("cost_basis") or 0.0)
        curr_price = float(item.get("currentPrice") or item.get("current_price") or avg_price)
        ppl = float(item.get("ppl") or item.get("unrealized_pnl") or ((curr_price - avg_price) * quantity))
        raw_curr = str(item.get("currency") or "").upper()
        is_pence = (
            raw_curr in ("GBX", "GBP")
            or ticker.endswith(("_L_EQ", "_GB_EQ", "_UK_EQ"))
            or ticker.startswith("SGLN")
        ) and (raw_curr == "GBX" or ticker.endswith(("_L_EQ", "_GB_EQ", "_UK_EQ")) or ticker.startswith("SGLN") or curr_price > 100.0)

        if "value_in_account_currency" in item and item["value_in_account_currency"] is not None:
            market_val = float(item["value_in_account_currency"])
        elif is_pence:
            market_val = (quantity * curr_price) / 100.0 if curr_price > 100.0 else (quantity * curr_price)
        else:
            market_val = float(item.get("marketValue") or item.get("market_value") or item.get("value") or (quantity * curr_price))

        if is_pence and avg_price > 100.0:
            avg_price = avg_price / 100.0
        if is_pence and curr_price > 100.0:
            curr_price = curr_price / 100.0

        total_invested += market_val
        positions.append({
            "ticker": ticker,
            "quantity": quantity,
            "average_price": round(avg_price, 4),
            "current_price": round(curr_price, 4),
            "market_value": round(market_val, 2),
            "ppl": round(ppl, 2),
            "allocation_percent": 0.0,
        })

    # Second pass: calculate allocation percentages
    if total_invested > 0:
        for pos in positions:
            pos["allocation_percent"] = round((pos["market_value"] / total_invested) * 100.0, 2)

    return positions


def get_open_orders(timeout: float = DEFAULT_TIMEOUT_SEC) -> list[dict[str, Any]]:
    """Fetch active open orders from openclaw-mcp-trade212."""
    res = mcp_trade212.call("api/trade212/open-orders", timeout=timeout)
    if isinstance(res, dict):
        raw_orders = res.get("data")
        if isinstance(raw_orders, list):
            return raw_orders
        elif isinstance(raw_orders, dict):
            return raw_orders.get("orders", [])
    elif isinstance(res, list):
        return res
    return []


def get_transaction_history(limit: int = 50, timeout: float = DEFAULT_TIMEOUT_SEC) -> list[dict[str, Any]]:
    """Fetch executed trade transaction history from openclaw-mcp-trade212 (Issue #507)."""
    try:
        res = mcp_trade212.call("api/trade212/history/orders", timeout=timeout)
    except Exception:
        res = mcp_trade212.call("api/trade212/transactions", timeout=timeout)
    if isinstance(res, dict):
        raw_trades = res.get("data")
        if isinstance(raw_trades, list):
            return raw_trades
        elif isinstance(raw_trades, dict):
            return raw_trades.get("trades", [])
    elif isinstance(res, list):
        return res
    return []


def get_funding_transactions(limit: int = 100, timeout: float = DEFAULT_TIMEOUT_SEC) -> dict[str, Any]:
    """Fetch capital funding transactions (deposits/withdrawals) from openclaw-mcp-trade212."""
    res = mcp_trade212.call("api/trade212/transactions/funding", timeout=timeout)
    if isinstance(res, dict) and "data" in res:
        return res["data"] if isinstance(res["data"], dict) else res
    return res if isinstance(res, dict) else {}


def _sync_daily_state(total_val: float) -> None:
    """Ensure trade212_daily_state.json is updated on daily portfolio fetch (Issue #658)."""
    home = os.environ.get("HOME", "/home/node")
    candidates = [
        pathlib.Path(home) / ".openclaw" / "workspace" / "trade212_daily_state.json",
        pathlib.Path(home) / "rob" / ".openclaw" / "workspace" / "trade212_daily_state.json",
        pathlib.Path("/workspace/trade212_daily_state.json"),
    ]
    today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

    for p in candidates:
        try:
            if p.parent.is_dir() and os.access(p.parent, os.W_OK):
                update_needed = True
                if p.is_file():
                    try:
                        curr = json.loads(p.read_text(encoding="utf-8"))
                        if curr.get("date") == today_str and "start_of_day_value" in curr:
                            update_needed = False
                    except Exception:
                        update_needed = True
                if update_needed:
                    p.write_text(
                        json.dumps(
                            {
                                "date": today_str,
                                "start_of_day_value": total_val,
                                "last_synced_ts": now_ts,
                                "last_synced_iso": _now_iso(),
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                break
        except Exception:
            continue


def get_live_portfolio_snapshot(timeout: float = DEFAULT_TIMEOUT_SEC) -> dict[str, Any]:
    """Retrieve full live portfolio data snapshot from openclaw-mcp-trade212."""
    account = get_account_summary(timeout=timeout)
    positions = get_portfolio_positions(timeout=timeout)
    orders = get_open_orders(timeout=timeout)

    total_val = float(account.get("total") or account.get("invested", 0.0) + account.get("cash", 0.0))
    total_ppl = float(account.get("ppl") or account.get("result") or sum(p["ppl"] for p in positions))
    net_supplied = account.get("netSupplied") if account.get("netSupplied") is not None else account.get("net_supplied")

    # Issue #658: Sync daily state file on live fetch
    if total_val > 0:
        _sync_daily_state(total_val)

    return {
        "source": "live_trading212",
        "is_live": True,
        "data_quality": "HIGH (Live Broker Feed)",
        "timestamp": _now_iso(),
        "account_summary": {
            "cash": float(account.get("cash", 0.0)),
            "invested": float(account.get("invested", 0.0)),
            "total": total_val,
            "currency": str(account.get("currency", "GBP")),
            "ppl": total_ppl,
            "result": float(account.get("result", total_ppl)),
            "net_supplied": net_supplied,
            "netSupplied": net_supplied,
        },
        "positions": positions,
        "open_orders": orders,
        "warnings": [],
    }


def parse_memory_md_portfolio(memory_path: str | pathlib.Path | None = None) -> dict[str, Any]:
    """Parse MEMORY.md for position estimates and PnL fallback when live broker is unavailable."""
    path_to_read = str(memory_path or resolve_memory_path())
    positions: list[dict[str, Any]] = []
    warnings = ["Live Trading 212 fetch unavailable; using MEMORY.md fallback estimates."]

    if os.path.exists(path_to_read):
        try:
            content = pathlib.Path(path_to_read).read_text(encoding="utf-8")
            pattern = re.compile(
                r"-\s*\*\*([A-Z0-9\.\-]+)\*\*:\s*([\d\.]+)\s*(?:shares|units)?\s*@\s*\$?([\d\.]+)(?:\s*\((?:Est\.\s*)?PnL\s*([+\-]?\$?[\d\.]+)\))?",
                re.IGNORECASE,
            )
            matches = pattern.findall(content)
            for m in matches:
                ticker = m[0].upper()
                qty = float(m[1])
                avg_price = float(m[2])
                ppl_raw = m[3].replace("$", "").replace("+", "") if m[3] else "0.0"
                ppl = float(ppl_raw) if ppl_raw else 0.0
                positions.append({
                    "ticker": ticker,
                    "quantity": qty,
                    "average_price": avg_price,
                    "current_price": avg_price,
                    "market_value": round(qty * avg_price, 2),
                    "ppl": ppl,
                    "allocation_percent": 0.0,
                })
        except Exception as exc:
            warnings.append(f"Failed to parse MEMORY.md at {path_to_read}: {exc}")
    else:
        warnings.append(f"MEMORY.md not found at {path_to_read}.")

    return {
        "source": "memory_md_fallback",
        "is_live": False,
        "data_quality": "LOW (MEMORY.md Fallback Estimate)",
        "timestamp": _now_iso(),
        "account_summary": {
            "cash": 0.0,
            "invested": sum(p["market_value"] for p in positions),
            "total": sum(p["market_value"] for p in positions),
            "currency": "GBP",
            "ppl": sum(p["ppl"] for p in positions),
            "result": sum(p["ppl"] for p in positions),
        },
        "positions": positions,
        "open_orders": [],
        "warnings": warnings,
    }


def get_portfolio_data_for_rob(
    memory_path: str | pathlib.Path | None = None,
    prefer_live: bool = True,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """High-level Rob portfolio data fetcher with live broker priority & MEMORY.md fallback."""
    if prefer_live:
        try:
            log.info("Fetching live Trading 212 portfolio data for Rob...")
            snapshot = get_live_portfolio_snapshot(timeout=timeout)
            log.info("Successfully retrieved live Trading 212 portfolio data (%d positions).", len(snapshot["positions"]))
            return snapshot
        except Exception as exc:
            log.warning("Live Trading 212 fetch failed for Rob (%s). Falling back to MEMORY.md...", exc)

    log.info("Falling back to MEMORY.md portfolio parsing for Rob...")
    fallback = parse_memory_md_portfolio(memory_path=memory_path)
    return fallback


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Trading 212 Client Library (Option A / Issue #449)")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")
    parser.add_argument("--live-only", action="store_true", help="Only attempt live broker fetch without fallback")
    args = parser.parse_args()

    if args.live_only:
        data = get_live_portfolio_snapshot()
    else:
        data = get_portfolio_data_for_rob()

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"Data Source: {data['source']} ({data['data_quality']})")
        print(f"Timestamp: {data['timestamp']}")
        print(f"Account Total: {data['account_summary']['total']} {data['account_summary']['currency']}")
        print(f"Positions Count: {len(data['positions'])}")
        for pos in data['positions']:
            print(f"  - {pos['ticker']}: {pos['quantity']} shares @ ${pos['average_price']} (PnL: ${pos['ppl']}, Alloc: {pos['allocation_percent']}%)")
        if data['warnings']:
            print("Warnings:", data['warnings'])
