# skills/lib/research_gate.py
"""Mandatory Research Protocol Gate validator for financial recommendations (Issue #556).

Enforces that any buy/sell signal, trade-212 recommendation, or commodity breach alert
carries a valid, pinned research block with Open Brain entity IDs and freshness checks
before being posted.
"""

import datetime
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_MACRO_FRESHNESS_DAYS = 7
DEFAULT_TACTICAL_FRESHNESS_DAYS = 1

class ResearchGateError(Exception):
    """Exception raised when a recommendation fails structural research gate checks."""


def validate_research_gate(
    payload: dict[str, Any],
    freshness_days: int = DEFAULT_MACRO_FRESHNESS_DAYS
) -> tuple[bool, str, str | None]:
    """Validates the research block on a recommendation payload.

    Returns:
        (is_valid_structure, status_code, message)
        status_code can be:
        - "VALID": Passed structural and freshness checks.
        - "STALE": Structure valid, but age exceeds freshness_days -> route to HITL.
        - "REJECTED": Missing or empty research structure -> hard block live recommendation.
    """
    research = payload.get("research")
    if not research or not isinstance(research, dict):
        return False, "REJECTED", "[ESCALATE] research-gate: no research pass (missing research block)"

    research_ids = research.get("id")
    if not research_ids or not isinstance(research_ids, list) or len(research_ids) == 0:
        return False, "REJECTED", "[ESCALATE] research-gate: no research pass (empty research id[])"

    captured_at_str = research.get("captured_at")
    if not captured_at_str or not isinstance(captured_at_str, str):
        return False, "REJECTED", "[ESCALATE] research-gate: no research pass (missing captured_at timestamp)"

    try:
        # Parse ISO datetime
        captured_at = datetime.datetime.fromisoformat(captured_at_str.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        
        # Calculate age
        age = now - captured_at
        if age.total_seconds() < 0:
            # Future timestamp fallback
            age = datetime.timedelta(seconds=0)

        max_age = datetime.timedelta(days=freshness_days)
        if age > max_age:
            return True, "STALE", f"stale-flag: research captured_at ({captured_at_str}) exceeds {freshness_days}d freshness window -> auto-route to ROHO/DON HITL"
    except Exception as exc:
        return False, "REJECTED", f"[ESCALATE] research-gate: invalid captured_at format ({captured_at_str}): {exc}"

    return True, "VALID", None


def construct_research_block(
    openbrain_ids: list[str],
    sources: list[str],
    captured_at: str | None = None,
    freshness_days: int = DEFAULT_MACRO_FRESHNESS_DAYS,
    tier: str = "AUTO"
) -> dict[str, Any]:
    """Helper to build a schema-compliant research block."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cap_time = captured_at or now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    stale_time = (now_utc + datetime.timedelta(days=freshness_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    return {
        "id": openbrain_ids,
        "sources": sources,
        "captured_at": cap_time,
        "stale_after": stale_time,
        "tier": tier,
    }
