import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# Mock heavy dependencies before importing email_ops
sys.modules["google.oauth2.credentials"] = MagicMock()
sys.modules["google_auth_oauthlib.flow"] = MagicMock()
sys.modules["google.auth.transport.requests"] = MagicMock()
sys.modules["googleapiclient.discovery"] = MagicMock()
sys.modules["requests"] = MagicMock()

# Point DATA_DIR to a temp directory for test isolation
_tmpdir = tempfile.mkdtemp()
os.environ["OPENCLAW_DATA_DIR"] = _tmpdir
os.environ["OPENCLAW_AGENT_NAME"] = "test-agent"
os.environ["MATTERMOST_WEBHOOK_SECRET"] = "test-secret-key"

import importlib.util

_OPS_DIR = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "email_ops", os.path.join(_OPS_DIR, "email_ops.py"),
)
email_ops = importlib.util.module_from_spec(_spec)
sys.modules["email_ops"] = email_ops
_spec.loader.exec_module(email_ops)


def _reset_transactions():
    """Wipe the transactions file between tests."""
    tf = email_ops.TRANSACTIONS_FILE
    if tf.exists():
        tf.unlink()


# ── HTML stripping ───────────────────────────────────────────────────


class TestStripHtml(unittest.TestCase):
    def test_simple_html(self):
        html = "<p>Hello <b>world</b></p>"
        result = email_ops.strip_html(html)
        self.assertIn("Hello", result)
        self.assertIn("world", result)
        self.assertNotIn("<p>", result)
        self.assertNotIn("<b>", result)

    def test_script_tags_removed(self):
        html = "<p>Hi</p><script>alert('x')</script><p>Bye</p>"
        result = email_ops.strip_html(html)
        self.assertIn("Hi", result)
        self.assertIn("Bye", result)
        self.assertNotIn("alert", result)

    def test_br_tags_become_newlines(self):
        html = "Line 1<br>Line 2"
        result = email_ops.strip_html(html)
        self.assertIn("Line 1", result)
        self.assertIn("Line 2", result)

    def test_empty_html(self):
        self.assertEqual(email_ops.strip_html(""), "")


# ── Signature stripping ─────────────────────────────────────────────


class TestStripSignatures(unittest.TestCase):
    def test_double_dash_signature(self):
        text = "Hello there.\n-- \nJohn Smith\nCEO"
        result = email_ops.strip_signatures(text)
        self.assertIn("Hello there.", result)
        self.assertNotIn("John Smith", result)

    def test_sent_from_iphone(self):
        text = "Quick reply.\nSent from my iPhone"
        result = email_ops.strip_signatures(text)
        self.assertIn("Quick reply.", result)
        self.assertNotIn("iPhone", result)

    def test_no_signature(self):
        text = "Plain message with no signature."
        self.assertEqual(email_ops.strip_signatures(text), text)


# ── Quoted thread stripping ──────────────────────────────────────────


class TestStripQuotedThreads(unittest.TestCase):
    def test_on_wrote_pattern(self):
        text = (
            "Thanks for the update.\n\n"
            "On Mon, Jan 1, 2026, John Smith wrote:\n"
            "> Previous message content\n"
            "> More quoted text"
        )
        result = email_ops.strip_quoted_threads(text)
        self.assertIn("Thanks for the update.", result)
        self.assertNotIn("Previous message", result)

    def test_original_message_pattern(self):
        text = (
            "Got it, will review.\n\n"
            "---- Original Message ----\n"
            "From: someone@example.com\n"
            "Old content"
        )
        result = email_ops.strip_quoted_threads(text)
        self.assertIn("Got it", result)
        self.assertNotIn("Old content", result)

    def test_angle_bracket_quotes(self):
        text = "My reply.\n> quoted line 1\n> quoted line 2"
        result = email_ops.strip_quoted_threads(text)
        self.assertIn("My reply.", result)
        self.assertNotIn("quoted line 1", result)

    def test_no_quotes(self):
        text = "Clean message."
        self.assertEqual(email_ops.strip_quoted_threads(text), text)


# ── Full preprocessing pipeline ──────────────────────────────────────


class TestPreprocessEmail(unittest.TestCase):
    def test_html_conversion_and_stripping(self):
        html_body = (
            "<html><body>"
            "<p>Important update.</p>"
            "<blockquote>Previous reply</blockquote>"
            "<p>-- <br>John Smith<br>CEO</p>"
            "</body></html>"
        )
        cleaned, raw_tok, clean_tok = email_ops.preprocess_email(html_body, "text/html")
        self.assertIn("Important update", cleaned)
        self.assertLess(clean_tok, raw_tok)

    def test_plain_text_passthrough(self):
        text = "Just a simple message."
        cleaned, raw_tok, clean_tok = email_ops.preprocess_email(text, "text/plain")
        self.assertEqual(cleaned, text)

    def test_token_reduction_spec_tc4(self):
        """Test Case 4: legal disclaimer (in signature) + 3 quoted replies → stripped."""
        new_content = "Net-new text here."
        disclaimer = "\n-- \n" + "\n".join(
            ["LEGAL DISCLAIMER LINE " + str(i) for i in range(50)]
        )
        quoted = (
            "\n\nOn Mon, Jan 1, 2026, Legal Team wrote:\n"
            + "\n".join([f"> Reply {i} with long legal text padding" for i in range(30)])
        )
        raw = f"{new_content}{disclaimer}{quoted}"
        cleaned, raw_tok, clean_tok = email_ops.preprocess_email(raw, "text/plain")
        self.assertIn("Net-new text", cleaned)
        self.assertLess(clean_tok, raw_tok * 0.20)


# ── PII redaction ────────────────────────────────────────────────────


class TestRedactPii(unittest.TestCase):
    def test_phone_number_redacted(self):
        text = "Call me at 555-123-4567 or +44 7911123456."
        result = email_ops.redact_pii(text)
        self.assertNotIn("555-123-4567", result)
        self.assertNotIn("7911123456", result)
        self.assertIn("[PHONE]", result)


# ── Domain classification ────────────────────────────────────────────


class TestClassifyRecipients(unittest.TestCase):
    def test_all_internal(self):
        self.assertEqual(
            email_ops.classify_recipients(["roho@chimexhldg.com", "amara@chimexhldg.com"]),
            "internal",
        )

    def test_external(self):
        self.assertEqual(
            email_ops.classify_recipients(["vendor@example.com"]),
            "external",
        )

    def test_mixed(self):
        self.assertEqual(
            email_ops.classify_recipients(["roho@chimexhldg.com", "vendor@example.com"]),
            "external",
        )


# ── Whitelist ────────────────────────────────────────────────────────


class TestWhitelist(unittest.TestCase):
    def setUp(self):
        email_ops.WHITELIST_DOMAINS.clear()
        email_ops.WHITELIST_ADDRESSES.clear()

    def test_internal_always_passes(self):
        self.assertTrue(email_ops.is_whitelisted(["a@chimexhldg.com"]))

    def test_external_not_whitelisted(self):
        self.assertFalse(email_ops.is_whitelisted(["x@vendor.com"]))

    def test_whitelisted_domain(self):
        email_ops.WHITELIST_DOMAINS.add("vendor.com")
        self.assertTrue(email_ops.is_whitelisted(["x@vendor.com"]))

    def test_whitelisted_address(self):
        email_ops.WHITELIST_ADDRESSES.add("specific@vendor.com")
        self.assertTrue(email_ops.is_whitelisted(["specific@vendor.com"]))
        self.assertFalse(email_ops.is_whitelisted(["other@vendor.com"]))


# ── Loop detection ───────────────────────────────────────────────────


class TestLoopDetection(unittest.TestCase):
    def setUp(self):
        _reset_transactions()

    def test_no_loop(self):
        self.assertFalse(email_ops.check_loop("thread-1"))

    def test_loop_detected(self):
        for _ in range(email_ops.LOOP_THRESHOLD):
            email_ops.create_transaction(
                agent_id="test", thread_id="thread-loop",
                direction="OUTBOUND", status="SENT",
            )
        self.assertTrue(email_ops.check_loop("thread-loop"))


# ── HMAC tokens ──────────────────────────────────────────────────────


class TestApprovalToken(unittest.TestCase):
    def test_roundtrip(self):
        token = email_ops.make_approval_token("txn-123")
        self.assertTrue(email_ops.verify_approval_token("txn-123", token))

    def test_invalid_token(self):
        self.assertFalse(email_ops.verify_approval_token("txn-123", "bad"))


# ── Transaction state machine ────────────────────────────────────────


class TestTransactionStateMachine(unittest.TestCase):
    def setUp(self):
        _reset_transactions()

    def test_create_and_load(self):
        txn = email_ops.create_transaction(
            agent_id="roho", thread_id="t1", direction="OUTBOUND", status="DRAFTED",
            to=["a@b.com"], subject="Hi", body_markdown="Hello",
        )
        self.assertEqual(txn["status"], "DRAFTED")
        data = email_ops._load_transactions()
        self.assertIn(txn["transaction_id"], data["transactions"])

    def test_valid_transition(self):
        txn = email_ops.create_transaction(
            agent_id="roho", thread_id="t2", direction="OUTBOUND", status="DRAFTED",
        )
        updated = email_ops.update_transaction(txn["transaction_id"], "APPROVED")
        self.assertEqual(updated["status"], "APPROVED")
        self.assertEqual(len(updated["history"]), 2)

    def test_invalid_transition_raises(self):
        txn = email_ops.create_transaction(
            agent_id="roho", thread_id="t3", direction="OUTBOUND", status="SENT",
        )
        with self.assertRaises(ValueError):
            email_ops.update_transaction(txn["transaction_id"], "DRAFTED")

    def test_pending_cannot_send(self):
        """Invariant: PENDING_APPROVAL must not reach SENT directly."""
        txn = email_ops.create_transaction(
            agent_id="roho", thread_id="t4", direction="OUTBOUND", status="DRAFTED",
        )
        email_ops.update_transaction(txn["transaction_id"], "PENDING_APPROVAL")
        with self.assertRaises(ValueError):
            email_ops.update_transaction(txn["transaction_id"], "SENT")


# ── Send gated (integration) ────────────────────────────────────────


class TestSendGated(unittest.TestCase):
    def setUp(self):
        _reset_transactions()

    @patch("email_ops._resolve_mm_channel", return_value=None)
    @patch("email_ops._mm_api")
    def test_tc1_internal_auto_approve(self, mock_mm, mock_ch):
        """Test Case 1: Amara→Roho = internal auto-approve, no MM notification."""
        mock_gmail = MagicMock(return_value={"status": "success", "message_id": "m1"})
        triage_mock = MagicMock()
        triage_mock.send_email = mock_gmail
        triage_mock._authenticate = MagicMock()
        triage_mock._service = MagicMock()

        with patch("builtins.print"), \
             patch.dict("sys.modules", {"triage": triage_mock}):
            result = email_ops.send_gated(
                MagicMock(), to=["roho@chimexhldg.com"], subject="Hi",
                body_markdown="Hello", cc=[],
            )

        self.assertEqual(result["status"], "success")
        self.assertIn("auto-approved", result["message"])
        mock_gmail.assert_called_once()
        mock_mm.assert_not_called()

    @patch("email_ops._resolve_mm_channel", return_value="ch123")
    @patch("email_ops._mm_api", return_value={"id": "post1"})
    def test_tc2_external_hitl_trigger(self, mock_mm, mock_ch):
        """Test Case 2: Roho→vendor = PENDING_APPROVAL, MM notification sent."""
        with patch("builtins.print"):
            result = email_ops.send_gated(
                MagicMock(), to=["vendor@example.com"], subject="Quote",
                body_markdown="Please send quote",
            )

        self.assertEqual(result["status"], "pending_approval")
        self.assertIn("approval", result["message"].lower())
        mock_mm.assert_called()
        call_args = mock_mm.call_args_list[-1]
        self.assertEqual(call_args[0][0], "POST")

    def test_tc3_finalize_rejection(self):
        """Test Case 3: Reject via Mattermost → status REJECTED."""
        txn = email_ops.create_transaction(
            agent_id="roho", thread_id="t-reject", direction="OUTBOUND",
            status="DRAFTED", to=["v@ext.com"], subject="S", body_markdown="B",
        )
        email_ops.update_transaction(txn["transaction_id"], "PENDING_APPROVAL")

        with patch("builtins.print"):
            result = email_ops.finalize(
                MagicMock(), txn["transaction_id"], "reject", reason="Not appropriate",
            )

        self.assertEqual(result["status"], "rejected")
        self.assertIn("Not appropriate", result["rejection_reason"])

        data = email_ops._load_transactions()
        final = data["transactions"][txn["transaction_id"]]
        self.assertEqual(final["status"], "REJECTED")

    def test_finalize_approve_and_send(self):
        txn = email_ops.create_transaction(
            agent_id="roho", thread_id="t-approve", direction="OUTBOUND",
            status="DRAFTED", to=["v@ext.com"], subject="S", body_markdown="B",
        )
        email_ops.update_transaction(txn["transaction_id"], "PENDING_APPROVAL")

        mock_gmail = MagicMock(return_value={"status": "success", "message_id": "m2"})
        triage_mock = MagicMock()
        triage_mock.send_email = mock_gmail
        sys.modules["triage"] = triage_mock

        with patch("builtins.print"):
            result = email_ops.finalize(
                MagicMock(), txn["transaction_id"], "approve",
            )

        self.assertEqual(result["status"], "success")
        mock_gmail.assert_called_once()

        data = email_ops._load_transactions()
        final = data["transactions"][txn["transaction_id"]]
        self.assertEqual(final["status"], "SENT")

    @patch("email_ops._resolve_mm_channel", return_value="ch-alert")
    @patch("email_ops._mm_api", return_value={"id": "p1"})
    def test_loop_quarantine(self, mock_mm, mock_ch):
        """Auto-responder loop → QUARANTINED."""
        for _ in range(email_ops.LOOP_THRESHOLD):
            email_ops.create_transaction(
                agent_id="test", thread_id="loop-thread",
                direction="OUTBOUND", status="SENT",
            )

        with patch("builtins.print"):
            result = email_ops.send_gated(
                MagicMock(), to=["a@chimexhldg.com"], subject="S",
                body_markdown="B", thread_id="loop-thread",
            )

        self.assertEqual(result["error_code"], "QUARANTINED")

    def test_finalize_wrong_state(self):
        txn = email_ops.create_transaction(
            agent_id="roho", thread_id="t-wrong", direction="OUTBOUND",
            status="DRAFTED",
        )
        with patch("builtins.print"):
            result = email_ops.finalize(MagicMock(), txn["transaction_id"], "approve")
        self.assertEqual(result["error_code"], "INVALID_STATE")

    def test_finalize_not_found(self):
        with patch("builtins.print"):
            result = email_ops.finalize(MagicMock(), "nonexistent", "approve")
        self.assertEqual(result["error_code"], "NOT_FOUND")


# ── Email body extraction ────────────────────────────────────────────


class TestExtractEmailBody(unittest.TestCase):
    def test_plain_text_body(self):
        import base64
        encoded = base64.urlsafe_b64encode(b"Hello world").decode()
        payload = {"mimeType": "text/plain", "body": {"data": encoded}}
        text, mime = email_ops._extract_email_body(payload)
        self.assertEqual(text, "Hello world")
        self.assertEqual(mime, "text/plain")

    def test_multipart_prefers_plain(self):
        import base64
        plain_data = base64.urlsafe_b64encode(b"Plain version").decode()
        html_data = base64.urlsafe_b64encode(b"<b>HTML</b>").decode()
        payload = {
            "mimeType": "multipart/alternative",
            "body": {},
            "parts": [
                {"mimeType": "text/plain", "body": {"data": plain_data}},
                {"mimeType": "text/html", "body": {"data": html_data}},
            ],
        }
        text, mime = email_ops._extract_email_body(payload)
        self.assertEqual(text, "Plain version")
        self.assertEqual(mime, "text/plain")

    def test_html_only_fallback(self):
        import base64
        html_data = base64.urlsafe_b64encode(b"<b>HTML only</b>").decode()
        payload = {
            "mimeType": "multipart/alternative",
            "body": {},
            "parts": [
                {"mimeType": "text/html", "body": {"data": html_data}},
            ],
        }
        text, mime = email_ops._extract_email_body(payload)
        self.assertEqual(text, "<b>HTML only</b>")
        self.assertEqual(mime, "text/html")


if __name__ == "__main__":
    unittest.main()
