"""Tests for shared github_token resolver (Issue #290)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import github_token as gt


class TestResolveGithubPat(unittest.TestCase):
    def test_env_wins(self):
        prev = os.environ.get("GITHUB_TOKEN")
        try:
            os.environ["GITHUB_TOKEN"] = "pat-env"
            self.assertEqual(gt.resolve_github_pat(), "pat-env")
        finally:
            if prev is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = prev

    def test_file_fallback_openclaw_workspace_relative(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            sec = Path(tmp) / "secrets"
            sec.mkdir()
            tok_file = sec / "github_token"
            tok_file.write_text("pat-from-ws-parent", encoding="utf-8")

            prev_ws = os.environ.pop("OPENCLAW_WORKSPACE", None)
            prev_tok = os.environ.pop("GITHUB_TOKEN", None)
            prev_gh = os.environ.pop("GH_TOKEN", None)
            try:
                os.environ["OPENCLAW_WORKSPACE"] = str(ws)
                self.assertEqual(gt.resolve_github_pat(), "pat-from-ws-parent")
            finally:
                if prev_ws is None:
                    os.environ.pop("OPENCLAW_WORKSPACE", None)
                else:
                    os.environ["OPENCLAW_WORKSPACE"] = prev_ws
                if prev_tok is not None:
                    os.environ["GITHUB_TOKEN"] = prev_tok
                if prev_gh is not None:
                    os.environ["GH_TOKEN"] = prev_gh

    def test_placeholder_env_skipped_for_file(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tf:
            tf.write("real-pat")
            path = tf.name
        prev_gtf = os.environ.get("GITHUB_TOKEN_FILE")
        prev_tok = os.environ.pop("GITHUB_TOKEN", None)
        prev_gh = os.environ.pop("GH_TOKEN", None)
        try:
            os.environ["GITHUB_TOKEN"] = "${GITHUB_TOKEN}"
            os.environ["GITHUB_TOKEN_FILE"] = path
            self.assertEqual(gt.resolve_github_pat(), "real-pat")
        finally:
            os.unlink(path)
            if prev_gtf is None:
                os.environ.pop("GITHUB_TOKEN_FILE", None)
            else:
                os.environ["GITHUB_TOKEN_FILE"] = prev_gtf
            if prev_tok is not None:
                os.environ["GITHUB_TOKEN"] = prev_tok
            if prev_gh is not None:
                os.environ["GH_TOKEN"] = prev_gh


if __name__ == "__main__":
    unittest.main()
