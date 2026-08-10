"""Unit tests for shared email_utils (Markdown→HTML via bleach)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from email_utils import markdown_to_html


class TestMarkdownToHtmlBleach(unittest.TestCase):
    def test_fenced_code(self):
        md = "```python\nx = 1\n```"
        html = markdown_to_html(md)
        self.assertIn("<pre>", html)
        self.assertIn("code", html.lower())
        self.assertIn("x = 1", html)

    def test_horizontal_rule(self):
        html = markdown_to_html("---")
        self.assertIn("<hr", html)

    def test_img_tag_stripped(self):
        html = markdown_to_html("![](https://evil.example/x.png)")
        # When bleach is present, <img> tag is stripped
        from email_utils import bleach
        if bleach is not None:
            self.assertNotIn("<img", html.lower())

    def test_hr_allowed(self):
        html = markdown_to_html("text\n\n---\n\nmore")
        self.assertIn("<hr", html.lower())


if __name__ == "__main__":
    unittest.main()
