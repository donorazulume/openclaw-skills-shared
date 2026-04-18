import sys
import unittest
from unittest.mock import MagicMock, patch, call
import importlib.util
import os
import base64
import json
import tempfile
from pathlib import Path
from email import message_from_bytes
from email.mime.multipart import MIMEMultipart

# Mock modules to avoid ImportErrors and side effects
sys.modules['google.oauth2.credentials'] = MagicMock()
sys.modules['google_auth_oauthlib.flow'] = MagicMock()
sys.modules['google.auth.transport.requests'] = MagicMock()
sys.modules['googleapiclient.discovery'] = MagicMock()
sys.modules['requests'] = MagicMock()

# Import the module (same directory as this test; works in CI and local clones)
_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
_TRIAGE_PATH = os.path.join(_SKILL_DIR, "triage.py")
spec = importlib.util.spec_from_file_location("triage", _TRIAGE_PATH)
triage = importlib.util.module_from_spec(spec)
sys.modules["triage"] = triage
spec.loader.exec_module(triage)


# ── Helpers ───────────────────────────────────────────────────────────

def _make_mock_service(message_id="msg_abc123"):
    """Return a mock Gmail service that records the sent raw payload."""
    svc = MagicMock()
    svc.users().messages().send.return_value.execute.return_value = {
        "id": message_id,
    }
    return svc


def _decode_mime(service_mock):
    """Extract and decode the MIME message from the mock service call."""
    call_kwargs = service_mock.users().messages().send.call_args
    raw_b64 = call_kwargs[1]["body"]["raw"] if call_kwargs[1] else call_kwargs[0][0]["raw"]
    raw_bytes = base64.urlsafe_b64decode(raw_b64)
    return message_from_bytes(raw_bytes)


# ── Expert Judgment (existing tests, preserved) ──────────────────────

class TestExpertJudgment(unittest.TestCase):
    def test_urgent_patterns(self):
        msg = {"payload": {"headers": [{"name": "Subject", "value": "Urgent: Project Update"}]}}
        self.assertEqual(triage.expert_judgment(msg), "01_Action")

        msg = {"payload": {"headers": [{"name": "Subject", "value": "Action Required: Login"}]}}
        self.assertEqual(triage.expert_judgment(msg), "01_Action")

    def test_waiting_patterns(self):
        msg = {"payload": {"headers": [{"name": "Subject", "value": "Budget Pending Approval"}]}}
        self.assertEqual(triage.expert_judgment(msg), "02_Waiting")

        msg = {"payload": {"headers": [{"name": "Subject", "value": "Awaiting Response"}]}}
        self.assertEqual(triage.expert_judgment(msg), "02_Waiting")

    def test_financial_patterns(self):
        msg = {"payload": {"headers": [{"name": "Subject", "value": "Invoice #12345"}]}}
        self.assertEqual(triage.expert_judgment(msg), "PARA/Areas")

        msg = {"payload": {"headers": [{"name": "Subject", "value": "Your Receipt from Amazon"}]}}
        self.assertEqual(triage.expert_judgment(msg), "PARA/Areas")

    def test_vip_patterns(self):
        msg = {"payload": {"headers": [{"name": "Subject", "value": "A message from the CEO"}]}}
        self.assertEqual(triage.expert_judgment(msg), "01_Action")

        msg = {"payload": {"headers": [{"name": "From", "value": "founder@startup.com"}, {"name": "Subject", "value": "Ideas"}]}}
        self.assertEqual(triage.expert_judgment(msg), "01_Action")

    def test_no_match(self):
        msg = {"payload": {"headers": [{"name": "Subject", "value": "Hello there"}]}}
        self.assertIsNone(triage.expert_judgment(msg))

    def test_case_insensitive(self):
        msg = {"payload": {"headers": [{"name": "Subject", "value": "urgent update"}]}}
        self.assertEqual(triage.expert_judgment(msg), "01_Action")


# ── Label helpers (existing tests, preserved) ────────────────────────

class TestEnsureLabel(unittest.TestCase):
    def test_label_exists(self):
        mock_service = MagicMock()
        existing = {"TestLabel": "label_id_123"}
        label_id = triage._ensure_label(mock_service, "TestLabel", existing)
        self.assertEqual(label_id, "label_id_123")
        mock_service.users().labels().create.assert_not_called()

    def test_label_does_not_exist(self):
        mock_service = MagicMock()
        existing = {}
        mock_create = mock_service.users().labels().create.return_value
        mock_create.execute.return_value = {"id": "new_label_id"}
        label_id = triage._ensure_label(mock_service, "NewLabel", existing)
        self.assertEqual(label_id, "new_label_id")
        self.assertEqual(existing["NewLabel"], "new_label_id")
        mock_service.users().labels().create.assert_called_once()


# ── Draft Reply (existing tests, preserved) ──────────────────────────

class TestDraftReply(unittest.TestCase):
    def test_draft_reply(self):
        mock_service = MagicMock()
        mock_thread = {
            "messages": [{
                "payload": {
                    "headers": [
                        {"name": "From", "value": "sender@example.com"},
                        {"name": "Subject", "value": "Original Subject"},
                        {"name": "Message-ID", "value": "<msg_id_123>"},
                    ]
                }
            }]
        }
        mock_service.users().threads().get.return_value.execute.return_value = mock_thread
        mock_service.users().drafts().create.return_value.execute.return_value = {"id": "draft_123"}

        with patch('builtins.print'):
            triage.draft_reply(mock_service, "thread_123", "Body text")

        mock_service.users().threads().get.assert_called_with(
            userId="me", id="thread_123", format="metadata",
            metadataHeaders=["From", "Subject", "Message-ID"]
        )
        args, kwargs = mock_service.users().drafts().create.call_args
        body = kwargs['body']
        self.assertEqual(body['message']['threadId'], "thread_123")
        self.assertIn('raw', body['message'])

    def test_draft_reply_empty_thread(self):
        mock_service = MagicMock()
        mock_thread = {"messages": []}
        mock_service.users().threads().get.return_value.execute.return_value = mock_thread
        with self.assertRaises(SystemExit):
            triage.draft_reply(mock_service, "thread_empty", "Body")


# ── Markdown → HTML conversion ───────────────────────────────────────

class TestMarkdownToHtml(unittest.TestCase):
    def test_bold(self):
        html = triage._markdown_to_html("**Bold** text")
        self.assertIn("<strong>Bold</strong>", html)
        self.assertIn("text", html)

    def test_italic(self):
        html = triage._markdown_to_html("*italic* word")
        self.assertIn("<em>italic</em>", html)

    def test_heading(self):
        html = triage._markdown_to_html("### Section Title")
        self.assertIn("<h3>Section Title</h3>", html)

    def test_unordered_list(self):
        html = triage._markdown_to_html("* Item 1\n* Item 2")
        self.assertIn("<li>", html)
        self.assertIn("Item 1", html)
        self.assertIn("Item 2", html)

    def test_link(self):
        html = triage._markdown_to_html("[Click here](https://example.com)")
        self.assertIn('href="https://example.com"', html)
        self.assertIn("Click here", html)

    def test_nl2br_single_newlines(self):
        html = triage._markdown_to_html("Line 1\nLine 2")
        self.assertIn("<br", html)

    def test_html_escaping_xss_prevention(self):
        html = triage._markdown_to_html("<script>alert('xss')</script>")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_html_tag_escaping(self):
        """Test Case 3 from spec: agent hallucinates HTML."""
        html = triage._markdown_to_html("<b>Bold</b>")
        self.assertIn("&lt;b&gt;", html)
        self.assertIn("&lt;/b&gt;", html)
        self.assertNotIn("<b>", html)

    def test_spec_data_model_contract(self):
        """Verify the spec's Data Model transformation contract."""
        html = triage._markdown_to_html("Hello **Don**,\n\n* Item 1")
        self.assertIn("<strong>Don</strong>", html)
        self.assertIn("<li>", html)
        self.assertIn("Item 1", html)


# ── Markdown → Plain text ────────────────────────────────────────────

class TestMarkdownToPlaintext(unittest.TestCase):
    def test_bold_stripping(self):
        text = triage._markdown_to_plaintext("**Bold** text")
        self.assertEqual(text, "Bold text")

    def test_italic_stripping(self):
        text = triage._markdown_to_plaintext("*italic* word")
        self.assertEqual(text, "italic word")

    def test_heading_stripping(self):
        text = triage._markdown_to_plaintext("### Section Title")
        self.assertEqual(text, "Section Title")

    def test_link_conversion(self):
        text = triage._markdown_to_plaintext("[Click](https://example.com)")
        self.assertEqual(text, "Click (https://example.com)")

    def test_list_conversion(self):
        text = triage._markdown_to_plaintext("* Item 1\n* Item 2")
        self.assertIn("- Item 1", text)
        self.assertIn("- Item 2", text)

    def test_html_tag_stripping(self):
        text = triage._markdown_to_plaintext("<b>Bold</b>")
        self.assertEqual(text, "Bold")

    def test_inline_code_stripping(self):
        text = triage._markdown_to_plaintext("Use `print()` here")
        self.assertEqual(text, "Use print() here")

    def test_image_stripping(self):
        text = triage._markdown_to_plaintext("![Logo](https://img.png)")
        self.assertEqual(text, "Logo")

    def test_spec_data_model_contract(self):
        """Verify the spec's plain-text transformation contract."""
        text = triage._markdown_to_plaintext("Hello **Don**,\n\n* Item 1")
        self.assertIn("Hello Don,", text)
        self.assertIn("- Item 1", text)


# ── Forced CC injection ──────────────────────────────────────────────

class TestInjectForcedCc(unittest.TestCase):
    def test_empty_cc_injects_don(self):
        result = triage._inject_forced_cc([])
        self.assertEqual(result, ["don@chimexhldg.com"])

    def test_dedup_exact_match(self):
        result = triage._inject_forced_cc(["don@chimexhldg.com"])
        self.assertEqual(result.count("don@chimexhldg.com"), 1)

    def test_dedup_case_insensitive(self):
        result = triage._inject_forced_cc(["DON@CHIMEXHLDG.COM"])
        lower_versions = [a for a in result if a.lower() == "don@chimexhldg.com"]
        self.assertEqual(len(lower_versions), 1)

    def test_preserves_other_addresses(self):
        result = triage._inject_forced_cc(["other@example.com"])
        self.assertIn("other@example.com", result)
        self.assertIn("don@chimexhldg.com", result)
        self.assertEqual(len(result), 2)

    def test_mixed_case_dedup(self):
        result = triage._inject_forced_cc(["Don@ChimexHldg.com"])
        lower_versions = [a for a in result if a.lower() == "don@chimexhldg.com"]
        self.assertEqual(len(lower_versions), 1)


# ── Email validation ─────────────────────────────────────────────────

class TestValidateEmail(unittest.TestCase):
    def test_valid_emails(self):
        self.assertTrue(triage._validate_email("user@example.com"))
        self.assertTrue(triage._validate_email("first.last@domain.co.uk"))
        self.assertTrue(triage._validate_email("user+tag@example.com"))

    def test_invalid_emails(self):
        self.assertFalse(triage._validate_email("not-an-email"))
        self.assertFalse(triage._validate_email("@missing-local.com"))
        self.assertFalse(triage._validate_email("missing@.com"))
        self.assertFalse(triage._validate_email(""))

    def test_strips_whitespace(self):
        self.assertTrue(triage._validate_email("  user@example.com  "))


# ── send_email (integration tests) ──────────────────────────────────

class TestSendEmail(unittest.TestCase):

    def setUp(self):
        triage._email_counters["agent_emails_sent_total"] = 0
        triage._email_counters["agent_email_format_errors"] = 0

    def test_happy_path_standard_execution(self):
        """Test Case 1 from spec: standard Markdown email."""
        svc = _make_mock_service("msg_001")

        with patch('builtins.print'):
            result = triage.send_email(
                svc,
                to=["client@example.com"],
                subject="Update",
                body_markdown="**Bold** text",
                cc=[],
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["message_id"], "msg_001")
        self.assertIn("client@example.com", result["message"])

        mime_msg = _decode_mime(svc)

        self.assertEqual(mime_msg.get_content_type(), "multipart/alternative")
        self.assertIn("client@example.com", mime_msg["to"])
        self.assertIn("don@chimexhldg.com", mime_msg["cc"])

        parts = list(mime_msg.walk())
        content_types = [p.get_content_type() for p in parts]
        self.assertIn("text/plain", content_types)
        self.assertIn("text/html", content_types)

        html_part = next(p for p in parts if p.get_content_type() == "text/html")
        html_body = html_part.get_payload(decode=True).decode()
        self.assertIn("<strong>Bold</strong>", html_body)

        plain_part = next(p for p in parts if p.get_content_type() == "text/plain")
        plain_body = plain_part.get_payload(decode=True).decode()
        self.assertIn("Bold text", plain_body)
        self.assertNotIn("**", plain_body)

    def test_cc_deduplication(self):
        """Test Case 2 from spec: agent manually includes Don."""
        svc = _make_mock_service()

        with patch('builtins.print'):
            result = triage.send_email(
                svc,
                to=["client@example.com"],
                subject="Update",
                body_markdown="Hello",
                cc=["don@chimexhldg.com"],
            )

        self.assertEqual(result["status"], "success")
        mime_msg = _decode_mime(svc)
        cc_header = mime_msg["cc"]
        self.assertEqual(cc_header.count("don@chimexhldg.com"), 1)

    def test_html_escaping_in_body(self):
        """Test Case 3 from spec: agent hallucinates HTML."""
        svc = _make_mock_service()

        with patch('builtins.print'):
            result = triage.send_email(
                svc,
                to=["client@example.com"],
                subject="Test",
                body_markdown="<b>Bold</b>",
            )

        self.assertEqual(result["status"], "success")
        mime_msg = _decode_mime(svc)
        html_part = next(
            p for p in mime_msg.walk() if p.get_content_type() == "text/html"
        )
        html_body = html_part.get_payload(decode=True).decode()
        self.assertIn("&lt;b&gt;", html_body)
        self.assertNotIn("<b>", html_body)

    def test_missing_recipient(self):
        """Error: no 'to' addresses provided."""
        svc = _make_mock_service()

        with patch('builtins.print'):
            result = triage.send_email(svc, to=[], subject="X", body_markdown="Y")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "MISSING_RECIPIENT")
        svc.users().messages().send.assert_not_called()

    def test_invalid_to_email(self):
        """Error: invalid email in 'to' list."""
        svc = _make_mock_service()

        with patch('builtins.print'):
            result = triage.send_email(
                svc, to=["not-valid"], subject="X", body_markdown="Y"
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "INVALID_EMAIL_FORMAT")
        self.assertIn("not-valid", result["message"])

    def test_invalid_cc_email(self):
        """Error: invalid email in 'cc' list."""
        svc = _make_mock_service()

        with patch('builtins.print'):
            result = triage.send_email(
                svc,
                to=["ok@example.com"],
                subject="X",
                body_markdown="Y",
                cc=["bad-cc"],
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "INVALID_EMAIL_FORMAT")

    def test_transport_error(self):
        """Error: Gmail API raises an exception."""
        svc = MagicMock()
        svc.users().messages().send.return_value.execute.side_effect = \
            RuntimeError("SMTP connection refused")

        with patch('builtins.print'):
            result = triage.send_email(
                svc,
                to=["client@example.com"],
                subject="Test",
                body_markdown="Hello",
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "TRANSPORT_ERROR")
        self.assertIn("SMTP connection refused", result["message"])

    def test_forced_cc_always_present(self):
        """CC is injected even when agent provides no CC at all."""
        svc = _make_mock_service()

        with patch('builtins.print'):
            triage.send_email(
                svc,
                to=["client@example.com"],
                subject="S",
                body_markdown="B",
            )

        mime_msg = _decode_mime(svc)
        self.assertIn("don@chimexhldg.com", mime_msg["cc"])

    def test_multipart_alternative_structure(self):
        """Payload is always multipart/alternative with plain and html parts."""
        svc = _make_mock_service()

        with patch('builtins.print'):
            triage.send_email(
                svc,
                to=["a@b.com"],
                subject="S",
                body_markdown="text",
            )

        mime_msg = _decode_mime(svc)
        self.assertEqual(mime_msg.get_content_type(), "multipart/alternative")

        payloads = mime_msg.get_payload()
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0].get_content_type(), "text/plain")
        self.assertEqual(payloads[1].get_content_type(), "text/html")

    def test_multiple_recipients(self):
        """Multiple 'to' addresses are comma-joined in the MIME header."""
        svc = _make_mock_service()

        with patch('builtins.print'):
            triage.send_email(
                svc,
                to=["a@b.com", "c@d.com"],
                subject="S",
                body_markdown="body",
            )

        mime_msg = _decode_mime(svc)
        self.assertIn("a@b.com", mime_msg["to"])
        self.assertIn("c@d.com", mime_msg["to"])

    def test_counters_increment_on_success(self):
        svc = _make_mock_service()

        with patch('builtins.print'):
            triage.send_email(svc, to=["a@b.com"], subject="S", body_markdown="B")

        self.assertEqual(triage._email_counters["agent_emails_sent_total"], 1)
        self.assertEqual(triage._email_counters["agent_email_format_errors"], 0)

    def test_counters_increment_on_validation_error(self):
        svc = _make_mock_service()

        with patch('builtins.print'):
            triage.send_email(svc, to=[], subject="S", body_markdown="B")

        self.assertEqual(triage._email_counters["agent_email_format_errors"], 1)
        self.assertEqual(triage._email_counters["agent_emails_sent_total"], 0)

    def test_rich_markdown_rendering(self):
        """Complex Markdown with headings, bold, lists, and links."""
        svc = _make_mock_service()
        md = (
            "### Weekly Report\n\n"
            "Hello **Don**,\n\n"
            "Here are the updates:\n\n"
            "* Completed [Task A](https://example.com/a)\n"
            "* Waiting on _approval_ for Task B\n\n"
            "Best regards"
        )

        with patch('builtins.print'):
            result = triage.send_email(
                svc, to=["don@chimexhldg.com"], subject="Report", body_markdown=md,
            )

        self.assertEqual(result["status"], "success")
        mime_msg = _decode_mime(svc)

        html_part = next(
            p for p in mime_msg.walk() if p.get_content_type() == "text/html"
        )
        html_body = html_part.get_payload(decode=True).decode()
        self.assertIn("<h3>", html_body)
        self.assertIn("<strong>Don</strong>", html_body)
        self.assertIn("<li>", html_body)
        self.assertIn("href=", html_body)
        self.assertIn("<em>approval</em>", html_body)

        plain_part = next(
            p for p in mime_msg.walk() if p.get_content_type() == "text/plain"
        )
        plain_body = plain_part.get_payload(decode=True).decode()
        self.assertIn("Weekly Report", plain_body)
        self.assertNotIn("###", plain_body)
        self.assertNotIn("**", plain_body)
        self.assertNotIn("_approval_", plain_body)
        self.assertIn("approval", plain_body)

    def test_xss_script_injection_blocked(self):
        """Script tags in body_markdown are escaped, not rendered."""
        svc = _make_mock_service()

        with patch('builtins.print'):
            triage.send_email(
                svc,
                to=["a@b.com"],
                subject="S",
                body_markdown="<script>alert('xss')</script>",
            )

        mime_msg = _decode_mime(svc)
        html_part = next(
            p for p in mime_msg.walk() if p.get_content_type() == "text/html"
        )
        html_body = html_part.get_payload(decode=True).decode()
        self.assertNotIn("<script>", html_body)

    def test_cc_case_insensitive_dedup_in_send(self):
        """Mixed-case don@ address is not duplicated."""
        svc = _make_mock_service()

        with patch('builtins.print'):
            triage.send_email(
                svc,
                to=["a@b.com"],
                subject="S",
                body_markdown="B",
                cc=["DON@CHIMEXHLDG.COM"],
            )

        mime_msg = _decode_mime(svc)
        cc_lower = mime_msg["cc"].lower()
        self.assertEqual(cc_lower.count("don@chimexhldg.com"), 1)

    def test_output_is_valid_json(self):
        """The stdout output is valid JSON matching the response schema."""
        svc = _make_mock_service("msg_json_test")
        captured = []

        with patch('builtins.print', side_effect=lambda s, **kw: captured.append(s)):
            triage.send_email(
                svc, to=["a@b.com"], subject="S", body_markdown="B",
            )

        self.assertEqual(len(captured), 1)
        parsed = json.loads(captured[0])
        self.assertEqual(parsed["status"], "success")
        self.assertEqual(parsed["message_id"], "msg_json_test")
        self.assertIn("message", parsed)

    def test_send_with_file_attachment_mixed_mime(self):
        """With attachments, root is multipart/mixed; body stays alternative + file."""
        svc = _make_mock_service("msg_att_1")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "note.txt"
            p.write_text("hello attachment", encoding="utf-8")

            with patch('builtins.print'):
                result = triage.send_email(
                    svc,
                    to=["client@example.com"],
                    subject="Docs",
                    body_markdown="Please see attached.",
                    cc=[],
                    attachments=[str(p)],
                )

        self.assertEqual(result["status"], "success")
        self.assertIn("attachment_paths", result)
        mime_msg = _decode_mime(svc)
        self.assertEqual(mime_msg.get_content_type(), "multipart/mixed")
        subtypes = [
            p.get_content_type()
            for p in mime_msg.walk()
        ]
        self.assertIn("multipart/alternative", subtypes)
        self.assertIn("text/plain", subtypes)
        self.assertIn("text/html", subtypes)
        # Attached part
        attach_parts = [
            x for x in mime_msg.walk()
            if x.get_content_disposition() == "attachment"
        ]
        self.assertEqual(len(attach_parts), 1)
        self.assertEqual(
            attach_parts[0].get_payload(decode=True),
            b"hello attachment",
        )

    def test_attachment_blocked_extension(self):
        svc = _make_mock_service()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "malware.exe"
            p.write_bytes(b"MZ")

            with patch('builtins.print'):
                result = triage.send_email(
                    svc,
                    to=["a@b.com"],
                    subject="S",
                    body_markdown="B",
                    attachments=[str(p)],
                )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "ATTACHMENT_BLOCKED")
        svc.users().messages().send.assert_not_called()


# ── Body extraction ──────────────────────────────────────────────────

class TestExtractBodyText(unittest.TestCase):

    def test_plain_text(self):
        encoded = base64.urlsafe_b64encode(b"Hello world").decode()
        payload = {"mimeType": "text/plain", "body": {"data": encoded}}
        self.assertEqual(triage._extract_body_text(payload), "Hello world")

    def test_multipart_prefers_plain(self):
        plain = base64.urlsafe_b64encode(b"Plain version").decode()
        html = base64.urlsafe_b64encode(b"<b>HTML version</b>").decode()
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": plain}},
                {"mimeType": "text/html", "body": {"data": html}},
            ],
        }
        self.assertEqual(triage._extract_body_text(payload), "Plain version")

    def test_html_fallback(self):
        html = base64.urlsafe_b64encode(b"<p>Hello <b>Don</b></p>").decode()
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": html}},
            ],
        }
        result = triage._extract_body_text(payload)
        self.assertIn("Hello", result)
        self.assertIn("Don", result)
        self.assertNotIn("<b>", result)

    def test_nested_multipart(self):
        plain = base64.urlsafe_b64encode(b"Nested plain").decode()
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": plain}},
                    ],
                },
            ],
        }
        self.assertEqual(triage._extract_body_text(payload), "Nested plain")

    def test_empty_payload(self):
        self.assertEqual(triage._extract_body_text({}), "")


class TestCollectAttachmentMetadata(unittest.TestCase):

    def test_finds_nested_attachment(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": base64.urlsafe_b64encode(b"hi").decode()},
                        },
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "scan.pdf",
                    "body": {"attachmentId": "ANG123", "size": 4444},
                    "partId": "1",
                },
            ],
        }
        meta = triage.collect_attachment_metadata(payload)
        self.assertEqual(len(meta), 1)
        self.assertEqual(meta[0]["attachment_id"], "ANG123")
        self.assertEqual(meta[0]["filename"], "scan.pdf")
        self.assertEqual(meta[0]["mime_type"], "application/pdf")


# ── Unread-only INBOX + mark read on triage ───────────────────────────

class TestTriageUnreadInbox(unittest.TestCase):
    """Directive: scan unread only; batchModify removes UNREAD when moving (SKILL.md)."""

    def test_triage_messages_list_requires_inbox_and_unread(self):
        mock_service = MagicMock()
        mock_service.users().labels().list.return_value.execute.return_value = {
            "labels": [
                {"name": "01_Action", "id": "lbl_a"},
                {"name": "03_Read", "id": "lbl_3"},
                {"name": "PARA/Areas", "id": "lbl_p"},
                {"name": "02_Waiting", "id": "lbl_w"},
            ],
        }
        mock_service.users().messages().list.return_value.execute.return_value = {"messages": []}
        with patch("builtins.print"):
            triage.triage(mock_service, limit=10)
        mock_service.users().messages().list.assert_called_once()
        kwargs = mock_service.users().messages().list.call_args.kwargs
        self.assertEqual(kwargs["labelIds"], [triage.INBOX_LABEL_ID, triage.UNREAD_LABEL_ID])
        self.assertEqual(kwargs["userId"], "me")

    def test_triage_report_messages_list_requires_inbox_and_unread(self):
        mock_service = MagicMock()
        mock_service.users().labels().list.return_value.execute.return_value = {"labels": []}
        mock_service.users().messages().list.return_value.execute.return_value = {"messages": []}
        with patch("builtins.print"):
            triage.triage_report(mock_service, limit=10)
        mock_service.users().messages().list.assert_called_once()
        kwargs = mock_service.users().messages().list.call_args.kwargs
        self.assertEqual(kwargs["labelIds"], [triage.INBOX_LABEL_ID, triage.UNREAD_LABEL_ID])


# ── Triage report ────────────────────────────────────────────────────

class TestTriageReport(unittest.TestCase):

    def _setup_service(self, messages, full_messages, full_body_messages=None):
        svc = MagicMock()
        svc.users().labels().list.return_value.execute.return_value = {
            "labels": [
                {"name": "INBOX", "id": "INBOX"},
                {"name": "01_Action", "id": "lbl_action"},
                {"name": "03_Read", "id": "lbl_read"},
                {"name": "PARA/Areas", "id": "lbl_areas"},
            ],
        }
        svc.users().messages().list.return_value.execute.return_value = {
            "messages": messages,
        }

        batch_call_count = [0]

        def fake_batch():
            batch_obj = MagicMock()
            added = []
            call_idx = batch_call_count[0]
            batch_call_count[0] += 1

            def add(request, callback, request_id=None):
                added.append((request, callback, request_id))

            def execute():
                is_body_batch = full_body_messages and call_idx >= 2
                source = full_body_messages if is_body_batch else full_messages
                for req, cb, rid in added:
                    resp = None
                    for m in source:
                        if m["id"] == rid:
                            resp = m
                            break
                    cb(rid, resp, None)
                added.clear()

            batch_obj.add = add
            batch_obj.execute = execute
            return batch_obj

        svc.new_batch_http_request = fake_batch
        return svc

    def test_empty_inbox_json(self):
        svc = MagicMock()
        svc.users().labels().list.return_value.execute.return_value = {"labels": []}
        svc.users().messages().list.return_value.execute.return_value = {"messages": []}

        captured = []
        with patch("builtins.print", side_effect=lambda s, **kw: captured.append(s)):
            triage.triage_report(svc, limit=10)

        report = json.loads(captured[0])
        self.assertEqual(report["summary"]["total_processed"], 0)
        self.assertEqual(report["emails"], [])

    def test_report_classifies_urgent(self):
        msg_stub = [{"id": "m1"}]
        msg_full = [{
            "id": "m1",
            "snippet": "Please respond urgently",
            "payload": {"headers": [
                {"name": "From", "value": "boss@corp.com"},
                {"name": "Subject", "value": "Urgent: Budget Review"},
            ]},
        }]
        body_msg = [{
            "id": "m1",
            "payload": {
                "mimeType": "text/plain",
                "body": {"data": base64.urlsafe_b64encode(b"Full body text here").decode()},
            },
        }]
        svc = self._setup_service(msg_stub, msg_full, body_msg)

        captured = []
        with patch("builtins.print", side_effect=lambda s, **kw: captured.append(s)):
            triage.triage_report(svc, limit=10)

        report = json.loads(captured[0])
        self.assertEqual(len(report["emails"]), 1)
        email = report["emails"][0]
        self.assertEqual(email["label"], "01_Action")
        self.assertEqual(email["importance"], "high")
        self.assertEqual(email["classification"], "expert_judgment")
        self.assertIn("body_preview", email)
        self.assertIn("Full body text", email["body_preview"])


if __name__ == '__main__':
    unittest.main()
