"""Tests for gmail-executive triage.py (MCP-Google edition, #323/#324).

The old Gmail-service-mock tests were replaced when triage.py stopped minting
Google OAuth credentials. All Gmail operations now go through openclaw-mcp-google
via ``skills/lib/mcp_google.call(...)``. These tests cover the pure classification
helpers and the high-level CLI actions with ``mcp_google.call`` patched out.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here.parent / "lib"))

# Optional deps the gateway image ships but the local pytest env may not.
for _mod in ("bleach", "markdown"):
    sys.modules.setdefault(_mod, MagicMock())

spec = importlib.util.spec_from_file_location("triage", str(_here / "triage.py"))
triage = importlib.util.module_from_spec(spec)
sys.modules["triage"] = triage
spec.loader.exec_module(triage)


class TestExpertJudgment(unittest.TestCase):
    """Pure classifier — operates on header strings, no I/O."""

    def test_urgent_goes_to_01_action(self):
        self.assertEqual(triage.expert_judgment_from_headers("Urgent: project", "boss@example.com"), "01_Action")
        self.assertEqual(triage.expert_judgment_from_headers("Action Required: Login", ""), "01_Action")

    def test_waiting_patterns(self):
        self.assertEqual(triage.expert_judgment_from_headers("Budget Pending Approval", ""), "02_Waiting")
        self.assertEqual(triage.expert_judgment_from_headers("Awaiting response", ""), "02_Waiting")

    def test_financial_goes_to_para_areas(self):
        self.assertEqual(triage.expert_judgment_from_headers("Invoice #123", ""), "PARA/Areas")

    def test_vip_in_from_address(self):
        self.assertEqual(triage.expert_judgment_from_headers("Hello", "ceo@example.com"), "01_Action")

    def test_unmatched_returns_none(self):
        self.assertIsNone(triage.expert_judgment_from_headers("Hello", "friend@example.com"))


class TestRuleTarget(unittest.TestCase):
    def test_newsletter_match(self):
        self.assertEqual(triage._rule_target("Weekly digest", "newsletter@x.com"), "03_Read")

    def test_invoice_match(self):
        self.assertEqual(triage._rule_target("Your invoice", "x@y.com"), "PARA/Areas")

    def test_no_match(self):
        self.assertIsNone(triage._rule_target("Lunch?", "friend@y.com"))


class TestEmailValidator(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(triage._validate_email("user@example.com"))

    def test_invalid(self):
        self.assertFalse(triage._validate_email("not-an-email"))
        self.assertFalse(triage._validate_email("user@"))


class TestForcedCC(unittest.TestCase):
    def test_injects_when_missing(self):
        out = triage._inject_forced_cc(["x@y.com"])
        self.assertIn(triage.FORCED_CC_ADDRESS, out)

    def test_does_not_double_add(self):
        out = triage._inject_forced_cc([triage.FORCED_CC_ADDRESS, "x@y.com"])
        self.assertEqual(sum(1 for a in out if a.lower() == triage.FORCED_CC_ADDRESS.lower()), 1)


def _fake_call(tool, arguments=None, **_kwargs):
    """Stub for mcp_google.call(...). Returns canned responses by tool name."""
    arguments = arguments or {}
    if tool == "google_mail_list_labels":
        return {
            "labels": [
                {"id": "INBOX", "name": "INBOX", "type": "system"},
                {"id": "L_action", "name": "01_Action", "type": "user"},
                {"id": "L_waiting", "name": "02_Waiting", "type": "user"},
                {"id": "L_read", "name": "03_Read", "type": "user"},
                {"id": "L_proj", "name": "PARA/Projects", "type": "user"},
                {"id": "L_areas", "name": "PARA/Areas", "type": "user"},
                {"id": "L_res", "name": "PARA/Resources", "type": "user"},
                {"id": "L_arch", "name": "PARA/Archives", "type": "user"},
            ],
            "total": 8,
        }
    if tool == "google_mail_create_label":
        return {"id": f"L_{arguments['name']}", "name": arguments["name"], "status": "exists"}
    if tool == "google_mail_search":
        return {
            "messages": [
                {"id": "m1", "subject": "URGENT: production down", "from": "alerts@x.com"},
                {"id": "m2", "subject": "Weekly digest", "from": "newsletter@x.com"},
                {"id": "m3", "subject": "Random chatter", "from": "buddy@x.com"},
            ],
            "total": 3,
        }
    if tool == "google_mail_label_batch":
        return {"updated": len(arguments.get("message_ids") or []), "status": "labels_updated"}
    if tool == "google_mail_label_info":
        return {"id": arguments.get("label"), "name": arguments.get("label"), "messages_total": 10, "messages_unread": 2, "exists": True}
    if tool == "google_mail_send":
        return {"message_id": "sent-1", "thread_id": "t1", "status": "sent"}
    raise AssertionError(f"unexpected tool call: {tool}")


class TestTriageCLIWiring(unittest.TestCase):
    """Each CLI action should only call openclaw-mcp-google — never anything else."""

    def setUp(self):
        os.environ.setdefault("MCP_TOKEN_GOOGLE_ROHO", "test-token")

    def test_triage_classifies_and_moves(self):
        with patch.object(triage.mcp_google, "call", side_effect=_fake_call) as mocked:
            triage.triage(limit=10)
        calls = [c.args[0] for c in mocked.call_args_list]
        # Must list labels, search inbox, and batch-move at least one bucket.
        self.assertIn("google_mail_list_labels", calls)
        self.assertIn("google_mail_search", calls)
        self.assertTrue(any(c == "google_mail_label_batch" for c in calls))

    def test_status_uses_label_info(self):
        with patch.object(triage.mcp_google, "call", side_effect=_fake_call) as mocked:
            triage.get_status()
        calls = [c.args[0] for c in mocked.call_args_list]
        self.assertGreaterEqual(calls.count("google_mail_label_info"), len(triage.ETS_LABELS) + 1)

    def test_send_email_routes_via_mcp(self):
        with patch.object(triage.mcp_google, "call", side_effect=_fake_call) as mocked:
            result = triage.send_email(["a@b.com"], "Hi", "**Body**", _quiet=True)
        self.assertEqual(result["status"], "success")
        # Only google_mail_send should be invoked.
        called = [c.args[0] for c in mocked.call_args_list]
        self.assertIn("google_mail_send", called)


class TestSendEmailValidation(unittest.TestCase):
    def test_missing_to_rejected(self):
        result = triage.send_email([], "Hi", "Body", _quiet=True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "MISSING_RECIPIENT")

    def test_invalid_to_rejected(self):
        result = triage.send_email(["not-an-email"], "Hi", "Body", _quiet=True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "INVALID_EMAIL_FORMAT")


if __name__ == "__main__":
    unittest.main()
