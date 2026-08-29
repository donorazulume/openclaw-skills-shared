"""Firefly III Data Extractor & FRS 105 Trial Balance / Entity Generator.

Queries openclaw-mcp-firefly for categorised transactions, applies FRS 105
statutory line item mappings and non-P&L cleanliness filters (excluding Revolut
top-ups, card repayments, internal transfers, and reversal accounts), and auto-generates
trial balance CSV and entity YAML payloads for the HMRC WebFiling sidecar.
"""

from __future__ import annotations

import csv
import io
import logging
import pathlib
import sys
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Tuple

_LIB_DIR = str(pathlib.Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import mcp_firefly  # pyright: ignore[reportMissingImports]
import yaml  # pyright: ignore[reportMissingImports,reportMissingTypeStubs]

log = logging.getLogger("openclaw.firefly_extractor")

# Non-P&L categories and terms to exclude from Profit & Loss calculation
NON_PNL_PATTERNS = [
    "revolut top-up",
    "revolut topup",
    "revolut",
    "credit card payment",
    "card repayment",
    "internal transfer",
    "reversal",
    "reconcile",
    "opening balance",
]

DEFAULT_ENTITY_BASE = {
    "company": {
        "name": "Chimex Holdings Ltd",
        "crn": "11647425",
        "sic": "68209",
        "jurisdiction": "england-wales",
        "dormant": False,
        "filing_mode": "web_filing",
    },
    "directors": [
        {
            "name": "Don Orazulume",
            "role": "Director",
        }
    ],
}


def _safe_decimal(val: Any) -> Decimal:
    if val is None:
        return Decimal(0)
    s = str(val).strip().replace(",", "")
    if not s or s.lower() == "nan":
        return Decimal(0)
    try:
        return Decimal(s)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def is_non_pnl_transaction(txn: dict[str, Any]) -> bool:
    """Check if transaction is a non-P&L flow (transfer, top-up, card repayment, reversal)."""
    txn_type = str(txn.get("type", "")).lower()
    if txn_type == "transfer":
        return True

    category = str(txn.get("category", "") or "").lower()
    description = str(txn.get("description", "") or "").lower()
    source = str(txn.get("source", "") or "").lower()
    destination = str(txn.get("destination", "") or "").lower()

    combined = f"{category} {description} {source} {destination}"
    for pat in NON_PNL_PATTERNS:
        if pat in combined:
            return True

    return False


def map_category_to_frs105(category_name: str, description: str = "") -> str:
    """Map a Firefly III category or transaction description to FRS 105 category."""
    cat_lower = (category_name or "").lower()
    desc_lower = (description or "").lower()
    combined = f"{cat_lower} {desc_lower}"

    if any(k in combined for k in ["rent", "rental income", "turnover", "sales", "tenant"]):
        return "Sales"
    elif any(k in combined for k in ["mortgage", "exact mortgages", "precise mortgages", "loan interest"]):
        return "Mortgage Interest"
    elif any(k in combined for k in ["property maintenance", "repairs", "maintenance", "refurbishment"]):
        return "Property Maintenance"
    elif any(k in combined for k in ["director loan", "director's loan", "capital injection", "share capital"]):
        return "Director Loan"
    elif any(k in combined for k in ["mobile", "subscription", "legal", "compliance", "admin", "insurance", "software", "tax"]):
        return "Administrative Expenses"
    else:
        return "Administrative Expenses"


def extract_firefly_data(fy_start: str, fy_end: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch transactions and accounts from Firefly III for date range [fy_start, fy_end]."""
    endpoint = f"api/firefly/transactions?since={fy_start}&until={fy_end}"
    log.info("Fetching transactions from Firefly III: %s", endpoint)

    res = mcp_firefly.call(endpoint)
    data = res.get("data", []) if isinstance(res, dict) else []

    parsed_txns: list[dict[str, Any]] = []
    for item in data:
        splits = item.get("attributes", {}).get("transactions", [])
        for split in splits:
            parsed_txns.append(
                {
                    "date": split.get("date", fy_start),
                    "description": split.get("description", "Transaction"),
                    "amount": _safe_decimal(split.get("amount")),
                    "type": split.get("type", "deposit"),
                    "category": split.get("category_name", ""),
                    "source": split.get("source_name", ""),
                    "destination": split.get("destination_name", ""),
                }
            )

    # Fetch accounts to extract metadata if available
    accounts_res = mcp_firefly.call("api/firefly/accounts")
    accounts_data = accounts_res.get("data", []) if isinstance(accounts_res, dict) else []

    return parsed_txns, {"accounts_count": len(accounts_data)}


def build_trial_balance_csv(transactions: list[dict[str, Any]]) -> str:
    """Filter non-P&L transactions and generate a CSV string for trial balance input."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Description", "Paid Out", "Paid In", "Category", "Type"])

    total_revenue = Decimal(0)

    for t in transactions:
        if is_non_pnl_transaction(t):
            log.debug("Skipping non-P&L transaction: %s", t)
            continue

        amt = t["amount"]
        t_type = str(t.get("type", "")).lower()
        cat = map_category_to_frs105(t.get("category", ""), t.get("description", ""))

        if t_type in ("deposit", "income") or cat == "Sales":
            paid_in = str(abs(amt))
            paid_out = "0"
            total_revenue += abs(amt)
        else:
            paid_in = "0"
            paid_out = str(abs(amt))

        writer.writerow(
            [
                t.get("date", "2025-01-01"),
                t.get("description", "Transaction"),
                paid_out,
                paid_in,
                cat,
                t_type,
            ]
        )

    return output.getvalue()


def build_entity_yaml(fy_start: str, fy_end: str, total_revenue: Decimal, output_mode: str = "web_filing") -> str:
    """Generate YAML entity config with auto-detected dormant status."""
    dormant = total_revenue == Decimal(0)

    entity_config = {
        "company": {
            "name": DEFAULT_ENTITY_BASE["company"]["name"],
            "crn": DEFAULT_ENTITY_BASE["company"]["crn"],
            "sic": DEFAULT_ENTITY_BASE["company"]["sic"],
            "jurisdiction": DEFAULT_ENTITY_BASE["company"]["jurisdiction"],
            "dormant": dormant,
            "filing_mode": output_mode,
            "accounting_period": {
                "start": fy_start,
                "end": fy_end,
            },
        },
        "directors": DEFAULT_ENTITY_BASE["directors"],
    }

    return str(yaml.dump(entity_config, sort_keys=False))


def generate_from_firefly(fy_start: str, fy_end: str, output_mode: str = "web_filing") -> tuple[str, str, bool]:
    """Execute complete Firefly extraction workflow.

    Returns:
        (csv_string, yaml_string, dormant_flag)
    """
    txns, _ = extract_firefly_data(fy_start, fy_end)

    total_revenue = Decimal(0)
    filtered_txns: list[dict[str, Any]] = []

    for t in txns:
        if is_non_pnl_transaction(t):
            continue
        filtered_txns.append(t)
        cat = map_category_to_frs105(t.get("category", ""), t.get("description", ""))
        t_type = str(t.get("type", "")).lower()
        if t_type in ("deposit", "income") or cat == "Sales":
            total_revenue += abs(t["amount"])

    csv_str = build_trial_balance_csv(filtered_txns)
    yaml_str = build_entity_yaml(fy_start, fy_end, total_revenue, output_mode)
    dormant = total_revenue == Decimal(0)

    return csv_str, yaml_str, dormant
