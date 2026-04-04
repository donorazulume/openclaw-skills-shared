
import sys
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, timezone
import io

# ----------------------------------------------------------------------
# 1. Mock external dependencies BEFORE importing manager
# ----------------------------------------------------------------------
sys.modules["google"] = MagicMock()
sys.modules["google.auth"] = MagicMock()
sys.modules["google.auth.transport"] = MagicMock()
sys.modules["google.auth.transport.requests"] = MagicMock()
sys.modules["google.oauth2"] = MagicMock()
sys.modules["google.oauth2.credentials"] = MagicMock()
sys.modules["googleapiclient"] = MagicMock()
sys.modules["googleapiclient.discovery"] = MagicMock()
sys.modules["googleapiclient.errors"] = MagicMock()
sys.modules["googleapiclient.http"] = MagicMock()
sys.modules["requests"] = MagicMock()

# Specific attributes
sys.modules["google.auth.transport.requests"].Request = MagicMock()
sys.modules["google.oauth2.credentials"].Credentials = MagicMock()
sys.modules["googleapiclient.discovery"].build = MagicMock()
# manager.py: from googleapiclient.errors import HttpError
class MockHttpError(Exception):
    def __init__(self, resp, content, uri=None):
        self.resp = resp
        self.content = content
        self.uri = uri
sys.modules["googleapiclient.errors"].HttpError = MockHttpError

# Dateutil
# manager.py: from dateutil import parser as dtparser
mock_dateutil = MagicMock()
mock_parser = MagicMock()
def parse_iso(iso_str):
    # Removing 'Z' for fromisoformat compatibility if needed, though usually standard ISO is fine
    # Adding +00:00 if Z is present
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"
    return datetime.fromisoformat(iso_str)
mock_parser.parse.side_effect = parse_iso
mock_dateutil.parser = mock_parser
sys.modules["dateutil"] = mock_dateutil

# ----------------------------------------------------------------------
# 2. Import module under test
# ----------------------------------------------------------------------
import os
# Ensure we can import manager from current directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import manager

# ----------------------------------------------------------------------
# 3. Test Class
# ----------------------------------------------------------------------
class TestExpertJudgment(unittest.TestCase):
    def setUp(self):
        self.mock_service = MagicMock()
        self.mock_events = self.mock_service.events.return_value
        self.mock_list = self.mock_events.list.return_value

    def _set_events(self, items):
        self.mock_list.execute.return_value = {"items": items}

    def test_no_meetings(self):
        """Should return True when there are no meetings."""
        self._set_events([])
        with patch('sys.stdout', new=io.StringIO()) as fake_out:
            result = manager.expert_judgment(self.mock_service)
        self.assertTrue(result)
        self.assertNotIn("Calendar Overload", fake_out.getvalue())

    def test_under_limit(self):
        """Should return True for meetings < 6 hours (e.g. 2 hours)."""
        # 2 hours total
        items = [
            {
                "summary": "Meeting 1",
                "start": {"dateTime": "2023-01-01T09:00:00+00:00"},
                "end": {"dateTime": "2023-01-01T11:00:00+00:00"}, # 120 mins
            }
        ]
        self._set_events(items)
        with patch('sys.stdout', new=io.StringIO()) as fake_out:
            result = manager.expert_judgment(self.mock_service)
        self.assertTrue(result)
        self.assertNotIn("Calendar Overload", fake_out.getvalue())

    def test_over_limit(self):
        """Should return False for meetings > 6 hours (e.g. 7 hours)."""
        # 7 hours total
        items = [
            {
                "summary": "Long Meeting",
                "start": {"dateTime": "2023-01-01T09:00:00+00:00"},
                "end": {"dateTime": "2023-01-01T16:00:00+00:00"}, # 7 hours = 420 mins
            }
        ]
        self._set_events(items)
        with patch('sys.stdout', new=io.StringIO()) as fake_out:
            result = manager.expert_judgment(self.mock_service)
        self.assertFalse(result)
        self.assertIn("Calendar Overload", fake_out.getvalue())

    def test_ignore_focus_time(self):
        """Should ignore events with 'Focus Time' in summary."""
        items = [
            {
                "summary": "Focus Time",
                "start": {"dateTime": "2023-01-01T09:00:00+00:00"},
                "end": {"dateTime": "2023-01-01T17:00:00+00:00"}, # 8 hours
            }
        ]
        self._set_events(items)
        result = manager.expert_judgment(self.mock_service)
        self.assertTrue(result)

    def test_ignore_ooo(self):
        """Should ignore events with 'OOO' in summary."""
        items = [
            {
                "summary": "I am OOO",
                "start": {"dateTime": "2023-01-01T09:00:00+00:00"},
                "end": {"dateTime": "2023-01-01T17:00:00+00:00"}, # 8 hours
            }
        ]
        self._set_events(items)
        result = manager.expert_judgment(self.mock_service)
        self.assertTrue(result)

    def test_ignore_all_day_events(self):
        """Should ignore all-day events (no dateTime)."""
        items = [
            {
                "summary": "All Day Event",
                "start": {"date": "2023-01-01"}, # No dateTime
                "end": {"date": "2023-01-02"},
            }
        ]
        self._set_events(items)
        result = manager.expert_judgment(self.mock_service)
        self.assertTrue(result)

    def test_large_meeting_warning(self):
        """Should print warning for large meetings (> 10 attendees)."""
        items = [
            {
                "summary": "Town Hall",
                "start": {"dateTime": "2023-01-01T10:00:00+00:00"},
                "end": {"dateTime": "2023-01-01T11:00:00+00:00"},
                "attendees": [{"email": f"user{i}@example.com"} for i in range(11)]
            }
        ]
        self._set_events(items)
        with patch('sys.stdout', new=io.StringIO()) as fake_out:
            result = manager.expert_judgment(self.mock_service)

        # Result should be True (only 1 hour)
        self.assertTrue(result)
        # Check for warning
        self.assertIn("Large Meeting", fake_out.getvalue())
        self.assertIn("Town Hall", fake_out.getvalue())

    def test_api_exception(self):
        """Should fail gracefully (return True) on API error."""
        self.mock_list.execute.side_effect = Exception("API Error")
        result = manager.expert_judgment(self.mock_service)
        self.assertTrue(result)

class TestDriveResolvePath(unittest.TestCase):
    def setUp(self):
        self.mock_service = MagicMock()
        self.mock_files = self.mock_service.files.return_value
        self.mock_list = self.mock_files.list.return_value

    def test_batch_fetch_behavior(self):
        """Should resolve path with a single batch API call."""
        path = "A/B/C"

        def list_side_effect(**kwargs):
            q = kwargs.get('q', '')
            mock_request = MagicMock()

            # Check if it's the batch query
            if "name = 'A'" in q and "name = 'B'" in q and "name = 'C'" in q:
                # Return all files
                mock_request.execute.return_value = {
                    "files": [
                        {"id": "id_A", "name": "A", "parents": []},
                        {"id": "id_B", "name": "B", "parents": ["id_A"]},
                        {"id": "id_C", "name": "C", "parents": ["id_B"]}
                    ]
                }
            else:
                 mock_request.execute.return_value = {"files": []}

            return mock_request

        self.mock_files.list.side_effect = list_side_effect

        result = manager._drive_resolve_path(self.mock_service, path)

        self.assertEqual(result, "id_C")
        # It should be 1 call
        self.assertEqual(self.mock_files.list.call_count, 1)

    def test_path_not_found(self):
        """Should return None if path is incomplete."""
        path = "A/B/Missing"

        def list_side_effect(**kwargs):
            q = kwargs.get('q', '')
            mock_request = MagicMock()
            if "name = 'A'" in q:
                 mock_request.execute.return_value = {
                    "files": [
                        {"id": "id_A", "name": "A", "parents": []},
                        {"id": "id_B", "name": "B", "parents": ["id_A"]},
                        # C is missing from result or Missing is missing
                    ]
                }
            else:
                 mock_request.execute.return_value = {"files": []}
            return mock_request

        self.mock_files.list.side_effect = list_side_effect
        result = manager._drive_resolve_path(self.mock_service, path)
        self.assertIsNone(result)

    def test_broken_chain(self):
        """Should return None if parent-child relationship is broken."""
        path = "A/B"

        def list_side_effect(**kwargs):
            mock_request = MagicMock()
            # Both A and B exist, but B is not child of A
            mock_request.execute.return_value = {
                "files": [
                    {"id": "id_A", "name": "A", "parents": []},
                    {"id": "id_B", "name": "B", "parents": ["id_Other"]},
                ]
            }
            return mock_request

        self.mock_files.list.side_effect = list_side_effect
        result = manager._drive_resolve_path(self.mock_service, path)
        self.assertIsNone(result)

    def test_duplicate_folder_names(self):
        """Should resolve correct path when folder names are duplicated."""
        path = "A/B"

        def list_side_effect(**kwargs):
            mock_request = MagicMock()
            # Scenario:
            # - Folder A1 (id=id_A1) at root
            # - Folder A2 (id=id_A2) at root
            # - Folder B1 (id=id_B1) inside A2
            # Greedy algorithm might pick A1, find no B inside, and fail.
            # Correct algorithm picks both A1 and A2, finds B1 inside A2, and succeeds with B1.

            mock_request.execute.return_value = {
                "files": [
                    {"id": "id_A1", "name": "A", "parents": []},
                    {"id": "id_A2", "name": "A", "parents": []},
                    {"id": "id_B1", "name": "B", "parents": ["id_A2"]},
                ]
            }
            return mock_request

        self.mock_files.list.side_effect = list_side_effect
        result = manager._drive_resolve_path(self.mock_service, path)
        self.assertEqual(result, "id_B1")

if __name__ == '__main__':
    unittest.main()
