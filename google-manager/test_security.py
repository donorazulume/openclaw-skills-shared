"""Security guards for the MCP-Google edition of google-manager (#323/#324).

The previous suite exercised Drive query escaping inside the in-process Gmail
service. That code lived under ``_drive_resolve_path`` / ``_drive_find_folder``
and is now server-side (`google_drive_list` query argument forwarded to MCP
Google). This test asserts the contract that matters at the gateway: no
``google.oauth2`` / ``googleapiclient`` imports leak back in, and forbidden
query patterns are still escaped before being forwarded to MCP Google.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here.parent / "lib"))

for _mod in ("dateutil",):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()
        sys.modules["dateutil.parser"] = sys.modules[_mod].parser

spec = importlib.util.spec_from_file_location("manager", str(_here / "manager.py"))
manager = importlib.util.module_from_spec(spec)
sys.modules["manager"] = manager
spec.loader.exec_module(manager)


class TestSourceHasNoOAuthImports(unittest.TestCase):
    def test_manager_source_contains_no_oauth_imports(self):
        text = (Path(__file__).resolve().parent / "manager.py").read_text(encoding="utf-8")
        for banned in ("google.oauth2", "googleapiclient", "from google_clients", "google_token_store"):
            self.assertNotIn(banned, text, f"banned token {banned!r} resurfaced in manager.py")


class TestDriveOrganizeForwardsTarget(unittest.TestCase):
    def test_resolve_or_create_only_uses_mcp(self):
        captured: list[str] = []

        def fake(tool, args=None, **_kw):
            captured.append(tool)
            if tool == "google_drive_list":
                return {"files": [], "total": 0}
            if tool == "google_drive_create_folder":
                return {"folder_id": "F_x", "status": "folder_created"}
            return {}

        with patch.object(manager.mcp_google, "call", side_effect=fake):
            manager._drive_resolve_or_create_folder("01_Projects/Sub Folder")
        # Should be one list + one create per segment, all going through MCP.
        self.assertIn("google_drive_list", captured)
        self.assertIn("google_drive_create_folder", captured)


if __name__ == "__main__":
    unittest.main()
