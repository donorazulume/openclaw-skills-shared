import unittest
from unittest.mock import patch
from datetime import datetime, timezone
import sys
import os

# Add the directory to sys.path to import manager
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import manager

class TestExpertJudgment(unittest.TestCase):
    @patch('manager.datetime')
    def test_expert_judgment(self, mock_datetime):
        """Test expert_judgment logic for stale tasks and high priority tasks without due dates."""
        # Set a fixed time: 2023-10-27 12:00:00 UTC
        fixed_now = datetime(2023, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
        mock_datetime.now.return_value = fixed_now

        # Calculate milliseconds for fixed_now
        now_ms = int(fixed_now.timestamp() * 1000)

        # 1. Stale task (updated > 14 days ago)
        # 15 days ago
        updated_ms = now_ms - (15 * 24 * 60 * 60 * 1000)
        task_stale = {
            "date_updated": str(updated_ms),
            "priority": {"priority": "normal"},
            "due_date": str(now_ms + 100000)
        }
        self.assertTrue(manager.expert_judgment(task_stale), "Task older than 14 days should be stale")

        # 2. Recent task (updated < 14 days ago)
        # 13 days ago
        updated_ms = now_ms - (13 * 24 * 60 * 60 * 1000)
        task_recent = {
            "date_updated": str(updated_ms),
            "priority": {"priority": "normal"},
            "due_date": str(now_ms + 100000)
        }
        self.assertFalse(manager.expert_judgment(task_recent), "Task updated recently should not be stale")

        # 3. High Priority, No Due Date
        task_high_nodue = {
            "date_updated": str(now_ms), # Updated just now
            "priority": {"priority": "high"},
            # "due_date": missing
        }
        self.assertTrue(manager.expert_judgment(task_high_nodue), "High priority task with no due date should be flagged")

        # 4. Urgent Priority, No Due Date
        task_urgent_nodue = {
            "date_updated": str(now_ms),
            "priority": {"priority": "urgent"},
        }
        self.assertTrue(manager.expert_judgment(task_urgent_nodue), "Urgent priority task with no due date should be flagged")

        # 5. Normal Priority, No Due Date
        task_normal_nodue = {
            "date_updated": str(now_ms),
            "priority": {"priority": "normal"},
        }
        self.assertFalse(manager.expert_judgment(task_normal_nodue), "Normal priority task with no due date should not be flagged")

        # 6. High Priority, With Due Date
        task_high_due = {
            "date_updated": str(now_ms),
            "priority": {"priority": "high"},
            "due_date": str(now_ms + 100000)
        }
        self.assertFalse(manager.expert_judgment(task_high_due), "High priority task with due date should not be flagged")

        # 7. Priority is not a dict
        task_prio_nodict = {
            "date_updated": str(now_ms),
            "priority": "high", # malformed priority field
        }
        self.assertFalse(manager.expert_judgment(task_prio_nodict), "Malformed priority should be treated as none")

        # 8. Missing priority
        task_no_prio = {
             "date_updated": str(now_ms),
             # missing priority
        }
        self.assertFalse(manager.expert_judgment(task_no_prio), "Missing priority should be treated as none")

        # 9. Missing update date
        task_no_update = {
            # missing date_updated
            "priority": {"priority": "normal"},
        }
        self.assertFalse(manager.expert_judgment(task_no_update), "Missing update date should not trigger staleness check")


if __name__ == '__main__':
    unittest.main()
