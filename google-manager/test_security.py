
import sys
import unittest
from unittest.mock import MagicMock, patch
import os

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

# Specific attributes
sys.modules["google.auth.transport.requests"].Request = MagicMock()
sys.modules["google.oauth2.credentials"].Credentials = MagicMock()
sys.modules["googleapiclient.discovery"].build = MagicMock()

class MockHttpError(Exception):
    def __init__(self, resp, content, uri=None):
        self.resp = resp
        self.content = content
        self.uri = uri
sys.modules["googleapiclient.errors"].HttpError = MockHttpError

# Dateutil
mock_dateutil = MagicMock()
mock_parser = MagicMock()
mock_dateutil.parser = mock_parser
sys.modules["dateutil"] = mock_dateutil

# ----------------------------------------------------------------------
# 2. Import module under test
# ----------------------------------------------------------------------
# Append the directory containing manager.py to sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import manager

class TestDriveSecurity(unittest.TestCase):
    def setUp(self):
        self.mock_svc = MagicMock()
        self.mock_files = self.mock_svc.files.return_value
        self.mock_list = self.mock_files.list

    def test_drive_find_folder_sanitization(self):
        """Test that single quotes are escaped in _drive_find_folder."""
        payload = "foo' OR '1'='1"
        manager._drive_find_folder(self.mock_svc, payload)

        call_args = self.mock_list.call_args
        self.assertIsNotNone(call_args)
        kwargs = call_args[1]
        q = kwargs.get('q')

        # Verify sanitization
        expected_part = "name = 'foo\\' OR \\'1\\'=\\'1'"
        self.assertIn(expected_part, q)
        self.assertNotIn("name = 'foo' OR '1'='1'", q)

    def test_drive_find_folder_backslash_sanitization(self):
        """Test that backslashes are escaped in _drive_find_folder."""
        payload = "foo\\bar"
        manager._drive_find_folder(self.mock_svc, payload)

        call_args = self.mock_list.call_args
        self.assertIsNotNone(call_args)
        kwargs = call_args[1]
        q = kwargs.get('q')

        # Verify sanitization: foo\bar becomes foo\\bar in query
        expected_part = "name = 'foo\\\\bar'"
        self.assertIn(expected_part, q)

    def test_drive_find_sanitization(self):
        """Test that single quotes are escaped in drive_find."""
        payload = "foo' OR '1'='1"
        # drive_find prints to stdout, so we suppress it
        with patch('sys.stdout', new=MagicMock()):
            manager.drive_find(self.mock_svc, payload)

        call_args = self.mock_list.call_args
        self.assertIsNotNone(call_args)
        kwargs = call_args[1]
        q = kwargs.get('q')

        # Verify sanitization
        expected_part = "name contains 'foo\\' OR \\'1\\'=\\'1'"
        self.assertIn(expected_part, q)
        self.assertNotIn("name contains 'foo' OR '1'='1'", q)

    def test_drive_find_backslash_sanitization(self):
        """Test that backslashes are escaped in drive_find."""
        payload = "foo\\bar"
        # drive_find prints to stdout, so we suppress it
        with patch('sys.stdout', new=MagicMock()):
            manager.drive_find(self.mock_svc, payload)

        call_args = self.mock_list.call_args
        self.assertIsNotNone(call_args)
        kwargs = call_args[1]
        q = kwargs.get('q')

        # Verify sanitization
        expected_part = "name contains 'foo\\\\bar'"
        self.assertIn(expected_part, q)

    def test_drive_resolve_path_sanitization(self):
        """Test that backslashes are escaped in _drive_resolve_path to prevent injection."""
        # This payload attempts to use a backslash to escape the closing quote
        payload = "foo\\"

        # Mock execute to return empty files so it doesn't crash
        self.mock_list.return_value.execute.return_value = {"files": []}

        manager._drive_resolve_path(self.mock_svc, payload)

        call_args = self.mock_list.call_args
        self.assertIsNotNone(call_args)
        kwargs = call_args[1]
        q = kwargs.get('q')

        # We expect double backslash to escape the backslash itself: foo\\
        # In the query string, this looks like: name = 'foo\\'
        expected_part = "name = 'foo\\\\'"
        self.assertIn(expected_part, q)

if __name__ == '__main__':
    unittest.main()
