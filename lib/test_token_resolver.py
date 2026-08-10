"""Tests for shared token_resolver helper (Issue #290 follow-up)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Add skills/lib to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import token_resolver as tr


class TestResolveSecret(unittest.TestCase):
    def test_env_wins(self):
        prev = os.environ.get("MCP_TOKEN_ROHO")
        try:
            os.environ["MCP_TOKEN_ROHO"] = "mcp-env-value"
            self.assertEqual(tr.resolve_secret("MCP_TOKEN_ROHO"), "mcp-env-value")
        finally:
            if prev is None:
                os.environ.pop("MCP_TOKEN_ROHO", None)
            else:
                os.environ["MCP_TOKEN_ROHO"] = prev

    def test_placeholder_env_skipped(self):
        prev = os.environ.get("MCP_TOKEN_ROHO")
        prev_file = os.environ.get("MCP_TOKEN_ROHO_FILE")
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tf:
            tf.write("real-mcp-value")
            path = tf.name
        try:
            os.environ["MCP_TOKEN_ROHO"] = "${MCP_TOKEN_ROHO}"
            os.environ["MCP_TOKEN_ROHO_FILE"] = path
            self.assertEqual(tr.resolve_secret("MCP_TOKEN_ROHO"), "real-mcp-value")
        finally:
            os.unlink(path)
            if prev is None:
                os.environ.pop("MCP_TOKEN_ROHO", None)
            else:
                os.environ["MCP_TOKEN_ROHO"] = prev
            if prev_file is None:
                os.environ.pop("MCP_TOKEN_ROHO_FILE", None)
            else:
                os.environ["MCP_TOKEN_ROHO_FILE"] = prev_file

    def test_file_fallback_openclaw_workspace_relative(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            sec = Path(tmp) / "secrets"
            sec.mkdir()
            tok_file = sec / "mcp_token_roho"
            tok_file.write_text("mcp-from-ws-parent", encoding="utf-8")

            prev_ws = os.environ.pop("OPENCLAW_WORKSPACE", None)
            prev_tok = os.environ.pop("MCP_TOKEN_ROHO", None)
            try:
                os.environ["OPENCLAW_WORKSPACE"] = str(ws)
                self.assertEqual(tr.resolve_secret("MCP_TOKEN_ROHO"), "mcp-from-ws-parent")
            finally:
                if prev_ws is None:
                    os.environ.pop("OPENCLAW_WORKSPACE", None)
                else:
                    os.environ["OPENCLAW_WORKSPACE"] = prev_ws
                if prev_tok is not None:
                    os.environ["MCP_TOKEN_ROHO"] = prev_tok


if __name__ == "__main__":
    unittest.main()
