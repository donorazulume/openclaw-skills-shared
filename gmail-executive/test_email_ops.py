"""Tests for gmail-executive email_ops.py after the MCP-Google migration (#323/#324).

The old tests relied on Gmail-service mocks that no longer fit the API. These
focus on the parts that didn't change (preprocessing, HMAC, state machine) plus
the new MCP-routing wiring for ingest_emails / send_gated / finalize.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))
sys.path.insert(0, str(_here.parent / "lib"))

for _mod in ("bleach", "markdown", "requests"):
    sys.modules.setdefault(_mod, MagicMock())

# Point transaction storage at a temp dir so tests don't write into the gateway home.
_tmp = tempfile.mkdtemp(prefix="email_ops_tests_")
os.environ["OPENCLAW_DATA_DIR"] = _tmp

# Triage module must load first because email_ops imports send_email lazily.
triage_spec = importlib.util.spec_from_file_location("triage", str(_here / "triage.py"))
triage = importlib.util.module_from_spec(triage_spec)
sys.modules["triage"] = triage
triage_spec.loader.exec_module(triage)

spec = importlib.util.spec_from_file_location("email_ops", str(_here / "email_ops.py"))
email_ops = importlib.util.module_from_spec(spec)
sys.modules["email_ops"] = email_ops
spec.loader.exec_module(email_ops)


class TestPreprocess(unittest.TestCase):
    def test_strips_signature(self):
        body = "Hello there\n\n-- \nBest,\nAlice"
        cleaned, raw, clean = email_ops.preprocess_email(body, "text/plain")
        self.assertIn("Hello there", cleaned)
        self.assertNotIn("Best,\nAlice", cleaned)
        self.assertGreaterEqual(raw, clean)

    def test_html_to_text(self):
        body = "<p>Hello</p><script>alert(1)</script><div>World</div>"
        cleaned, _r, _c = email_ops.preprocess_email(body, "text/html")
        self.assertIn("Hello", cleaned)
        self.assertNotIn("alert", cleaned)


class TestClassification(unittest.TestCase):
    def test_internal_recipients(self):
        self.assertEqual(email_ops.classify_recipients(["x@chimexhldg.com"]), "internal")

    def test_external_recipients(self):
        self.assertEqual(email_ops.classify_recipients(["x@gmail.com"]), "external")

    def test_mixed_is_external(self):
        self.assertEqual(
            email_ops.classify_recipients(["x@chimexhldg.com", "y@gmail.com"]),
            "external",
        )


class TestApprovalToken(unittest.TestCase):
    def test_roundtrip(self):
        tok = email_ops.make_approval_token("txn-1")
        self.assertTrue(email_ops.verify_approval_token("txn-1", tok))

    def test_rejects_wrong_token(self):
        self.assertFalse(email_ops.verify_approval_token("txn-1", "bogus"))


class TestTransactionStateMachine(unittest.TestCase):
    def test_create_then_update(self):
        txn = email_ops.create_transaction(
            agent_id="test", thread_id="t1", direction="OUTBOUND",
            status="DRAFTED", to=["a@chimexhldg.com"], subject="Hi", body_markdown="**hi**",
        )
        updated = email_ops.update_transaction(txn["transaction_id"], "APPROVED", actor="unit")
        self.assertEqual(updated["status"], "APPROVED")

    def test_invalid_transition_raises(self):
        txn = email_ops.create_transaction(
            agent_id="test", thread_id="t2", direction="OUTBOUND",
            status="DRAFTED", to=["a@chimexhldg.com"], subject="Hi", body_markdown="**hi**",
        )
        with self.assertRaises(ValueError):
            email_ops.update_transaction(txn["transaction_id"], "SENT", actor="unit")


def _fake_mg_call(tool, arguments=None, **_kwargs):
    arguments = arguments or {}
    if tool == "google_mail_search":
        return {"messages": [{"id": "msg-x", "subject": "Hi", "from": "alice@example.com"}], "total": 1}
    if tool == "google_mail_read":
        return {
            "id": "msg-x", "thread_id": "thread-x",
            "subject": "Hi", "from": "alice@example.com",
            "date": "2026-05-19", "body": "Hello world\n\n-- \nBest, Alice",
        }
    if tool == "google_mail_send":
        return {"message_id": "sent-1", "thread_id": "t1", "status": "sent"}
    raise AssertionError(f"unexpected MCP call: {tool}")


class TestIngestRoutesViaMCP(unittest.TestCase):
    def test_ingest_calls_mcp_only(self):
        with patch.object(triage.mcp_google, "call", side_effect=_fake_mg_call) as mocked:
            ingested = email_ops.ingest_emails(limit=5)
        self.assertEqual(len(ingested), 1)
        calls = [c.args[0] for c in mocked.call_args_list]
        self.assertIn("google_mail_search", calls)
        self.assertIn("google_mail_read", calls)


class TestSendGatedRoutesViaMCP(unittest.TestCase):
    def test_internal_auto_send(self):
        with patch.object(triage.mcp_google, "call", side_effect=_fake_mg_call) as mocked:
            result = email_ops.send_gated(
                to=["a@chimexhldg.com"], subject="Hi", body_markdown="**hi**", thread_id="thread-a",
            )
        self.assertEqual(result["status"], "success")
        called = [c.args[0] for c in mocked.call_args_list]
        self.assertIn("google_mail_send", called)


if __name__ == "__main__":
    unittest.main()
