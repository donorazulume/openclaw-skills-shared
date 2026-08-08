import unittest
from unittest.mock import patch, MagicMock, call
import sys
import os

# SPEC-GAUTH-001 v2.0.0 (#323/#324): clickup-manager no longer imports Google
# OAuth libraries — Calendar reads come from openclaw-mcp-google. We still mock
# `requests`, `pytz`, and `dateutil` so the local pytest env can collect the
# module even when those system packages aren't installed.
sys.modules['requests'] = MagicMock()
sys.modules['pytz'] = MagicMock()
sys.modules['dateutil'] = MagicMock()
# `manager` imports `mcp_google` from the sibling `lib/` folder; add it to path.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
sys.modules['mcp_google'] = MagicMock()

# Add the directory to sys.path to import manager
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if 'manager' in sys.modules:
    del sys.modules['manager']

import manager  # noqa: E402

class TestRefactorWorkspace(unittest.TestCase):
    @patch('manager._get')
    @patch('manager._post')
    @patch('manager.log') # suppress logging during tests
    def test_refactor_workspace_creates_everything_fresh(self, mock_log, mock_post, mock_get):
        """Test refactor_workspace when nothing exists."""

        # We need to simulate state change.
        # Initially, get_spaces returns empty.
        # After creation, get_spaces should return the created spaces.

        self.spaces_created = False

        def side_effect_get(path, params=None):
            if path.endswith("/space"):
                if self.spaces_created:
                    return {"spaces": [{"id": "new_id", "name": "Second Brain"}, {"id": "new_id", "name": "Dev Studio"}]}
                return {"spaces": []}
            if path.endswith("/folder"):
                return {"folders": []}
            if path.endswith("/list"):
                return {"lists": []}
            if path.endswith("/tag"):
                return {"tags": []}
            return {}

        mock_get.side_effect = side_effect_get

        # When _post is called to create a space, we flip the flag for the next _get call
        def side_effect_post(path, body):
            if path.endswith("/space"):
                self.spaces_created = True
            return {"id": "new_id"}

        mock_post.side_effect = side_effect_post

        # Run the function
        manager.refactor_workspace("team_123")

        # Verify calls

        # Create Space 'Second Brain'
        self.assertIn(call('/team/team_123/space', {
            "name": "Second Brain",
            "multiple_assignees": True,
            "features": {
                "due_dates": {"enabled": True},
                "priorities": {"enabled": True},
                "tags": {"enabled": True},
                "time_estimates": {"enabled": True},
            },
        }), mock_post.mock_calls)

        # Create Folder '00 Inbox' in the new space (id 'new_id')
        self.assertIn(call('/space/new_id/folder', {"name": "00 Inbox"}), mock_post.mock_calls)

        # Create List 'Capture' in the new folder (id 'new_id')
        self.assertIn(call('/folder/new_id/list', {"name": "Capture"}), mock_post.mock_calls)

        # Create Tag '@DeepWork' in the new space (id 'new_id')
        self.assertIn(call('/space/new_id/tag', {"tag": {"name": "@DeepWork"}}), mock_post.mock_calls)


    @patch('manager._get')
    @patch('manager._post')
    @patch('manager.log')
    def test_refactor_workspace_idempotent(self, mock_log, mock_post, mock_get):
        """Test refactor_workspace when everything already exists."""

        # Setup mocks to return existing structures
        def side_effect_get(path, params=None):
            if path.endswith("/space"):
                return {"spaces": [{"id": "s1", "name": "Second Brain"}, {"id": "s2", "name": "Dev Studio"}]}
            if "/space/s1/folder" in path:
                return {"folders": [{"id": "f1", "name": "00 Inbox"}]}
            if "/space/s2/folder" in path:
                return {"folders": []}
            if "/folder/f1/list" in path:
                return {"lists": [{"id": "l1", "name": "Capture"}]}
            if path.endswith("/tag"):
                 return {"tags": [{"name": "@DeepWork"}, {"name": "@Admin"}, {"name": "@Errands"}, {"name": "@Waiting"}, {"name": "@Someday"}]}
            return {"folders": [], "lists": [], "tags": []}

        mock_get.side_effect = side_effect_get

        # Run the function
        manager.refactor_workspace("team_123")

        calls = mock_post.mock_calls

        # Check Second Brain creation - should NOT happen
        second_brain_creation = call('/team/team_123/space', {
            "name": "Second Brain",
            "multiple_assignees": True,
            "features": {
                "due_dates": {"enabled": True},
                "priorities": {"enabled": True},
                "tags": {"enabled": True},
                "time_estimates": {"enabled": True},
            },
        })
        self.assertNotIn(second_brain_creation, calls)

        # Check '00 Inbox' creation - should NOT happen
        inbox_creation = call('/space/s1/folder', {"name": "00 Inbox"})
        self.assertNotIn(inbox_creation, calls)

        # Check 'Capture' creation - should NOT happen
        capture_creation = call('/folder/f1/list', {"name": "Capture"})
        self.assertNotIn(capture_creation, calls)

        # Check '@DeepWork' creation in s1 - should NOT happen
        deepwork_creation = call('/space/s1/tag', {"tag": {"name": "@DeepWork"}})
        self.assertNotIn(deepwork_creation, calls)


class TestExpertJudgment(unittest.TestCase):
    def test_expert_judgment(self):
        """Test expert_judgment function."""
        # Urgent with due date -> OK
        self.assertFalse(manager.expert_judgment({
            "priority": {"priority": "urgent"},
            "due_date": "12345"
        }))
        # Urgent without due date -> True (Flagged)
        self.assertTrue(manager.expert_judgment({
            "priority": {"priority": "urgent"},
            "due_date": None
        }))
        # High without due date -> True (Flagged)
        self.assertTrue(manager.expert_judgment({
            "priority": {"priority": "high"},
            "due_date": None
        }))
        # Normal without due date -> OK
        self.assertFalse(manager.expert_judgment({
            "priority": {"priority": "normal"},
            "due_date": None
        }))


class TestAssigneeAndGetTask(unittest.TestCase):
    def test_resolve_assignee_id(self):
        self.assertEqual(manager.resolve_assignee_id("amara"), 106596088)
        self.assertEqual(manager.resolve_assignee_id("roho"), 106596087)
        self.assertEqual(manager.resolve_assignee_id("123456"), 123456)

    @patch('manager._get')
    def test_get_task_with_none_assignee_fields(self, mock_get):
        """Issue #497: verify get_task handles assignees with username=None and email=None."""
        mock_get.return_value = {
            "id": "86cb2p63e",
            "name": "UTB Task",
            "status": {"status": "in progress"},
            "assignees": [{"id": 106596088, "username": None, "email": None}],
        }
        # Should not raise TypeError: sequence item 0: expected str instance, NoneType
        try:
            manager.get_task("86cb2p63e")
        except TypeError as e:
            self.fail(f"get_task raised TypeError on None assignee fields: {e}")

    @patch('manager._put')
    def test_claim_task(self, mock_put):
        """Issue #498: verify claim_task calls PUT /task/{id} with correct assignee."""
        mock_put.return_value = {"id": "task123", "url": "https://app.clickup.com/t/task123"}
        manager.claim_task("task123", assignee="amara")
        mock_put.assert_called_once_with("/task/task123", {"assignees": {"add": [106596088]}})


if __name__ == '__main__':
    unittest.main()

