"""Tests for the MCP-Google edition of google-manager (#323/#324)."""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here.parent / "lib"))

for _mod in ("dateutil",):
    if _mod not in sys.modules:
        # dateutil might not be installed in the local pytest env.
        sys.modules[_mod] = MagicMock()
        sys.modules["dateutil.parser"] = sys.modules[_mod].parser

spec = importlib.util.spec_from_file_location("manager", str(_here / "manager.py"))
manager = importlib.util.module_from_spec(spec)
sys.modules["manager"] = manager
spec.loader.exec_module(manager)


def _fake_call(tool, arguments=None, **_kw):
    arguments = arguments or {}
    if tool == "google_mail_search":
        return {"messages": [{"id": "m1", "subject": "Hello", "from": "a@b.com"}], "total": 1}
    if tool == "google_mail_label_info":
        return {"id": arguments.get("label"), "name": arguments.get("label"), "messages_total": 1, "messages_unread": 0}
    if tool == "google_mail_create_label":
        return {"id": "L_x", "name": arguments.get("name"), "status": "created"}
    if tool == "google_mail_send":
        return {"message_id": "sent-1", "status": "sent"}
    if tool == "google_drive_list":
        return {"files": [], "total": 0}
    if tool == "google_drive_create_folder":
        return {"folder_id": "F_x", "name": arguments.get("name"), "status": "folder_created"}
    if tool == "google_drive_get_file":
        return {"id": arguments.get("file_id"), "name": "doc.pdf", "parents": ["root"], "description": ""}
    if tool == "google_drive_update_file":
        return {"file_id": arguments.get("file_id"), "status": "updated", "parents": []}
    if tool == "google_drive_search":
        return {"files": [], "total": 0}
    if tool == "google_calendar_list_events":
        return {"events": [], "total": 0}
    if tool == "google_calendar_create_event":
        return {"event_id": "evt-1", "summary": arguments.get("summary"), "status": "event_created"}
    if tool == "google_calendar_update_event":
        return {"event_id": arguments.get("event_id"), "status": "event_updated"}
    raise AssertionError(f"unexpected tool: {tool}")


class TestGmailWiring(unittest.TestCase):
    def test_triage_calls_search_and_label_info(self):
        with patch.object(manager.mcp_google, "call", side_effect=_fake_call) as mocked:
            manager.gmail_triage(limit=5)
        calls = [c.args[0] for c in mocked.call_args_list]
        self.assertIn("google_mail_search", calls)
        self.assertIn("google_mail_label_info", calls)

    def test_create_labels_idempotent(self):
        with patch.object(manager.mcp_google, "call", side_effect=_fake_call) as mocked:
            manager.gmail_create_labels()
        calls = [c.args[0] for c in mocked.call_args_list]
        self.assertGreaterEqual(calls.count("google_mail_create_label"), len(manager.GMAIL_LABELS))


class TestDriveWiring(unittest.TestCase):
    def test_init_para_creates_missing(self):
        with patch.object(manager.mcp_google, "call", side_effect=_fake_call) as mocked:
            manager.drive_init_para()
        calls = [c.args[0] for c in mocked.call_args_list]
        self.assertEqual(calls.count("google_drive_list"), len(manager.PARA_FOLDERS))
        self.assertEqual(calls.count("google_drive_create_folder"), len(manager.PARA_FOLDERS))

    def test_organize_renames_and_moves(self):
        captured = []

        def cap(tool, args=None, **_kw):
            captured.append((tool, args or {}))
            return _fake_call(tool, args)

        with patch.object(manager.mcp_google, "call", side_effect=cap):
            manager.drive_organize("file-1", target_folder="01_Projects/Demo", rename_desc="meeting notes")
        tools = [c[0] for c in captured]
        self.assertIn("google_drive_get_file", tools)
        self.assertIn("google_drive_update_file", tools)
        update_args = [c[1] for c in captured if c[0] == "google_drive_update_file"][0]
        self.assertIn("meeting notes", update_args.get("new_name", ""))
        self.assertTrue(update_args.get("add_parent_id"))


class TestNoOAuthImports(unittest.TestCase):
    def test_manager_does_not_import_credentials(self):
        text = (Path(__file__).resolve().parent / "manager.py").read_text(encoding="utf-8")
        for forbidden in ("google.oauth2", "googleapiclient", "from google_clients"):
            self.assertNotIn(forbidden, text, f"{forbidden} should no longer appear in manager.py (#323/#324)")


if __name__ == "__main__":
    unittest.main()
