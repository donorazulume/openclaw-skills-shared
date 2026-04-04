"""
Shared HTML email utilities for all OpenClaw skills.

Provides Markdown→HTML conversion and plain-text stripping used by
gmail-executive, google-manager, and chimex-manager so they all
produce consistent multipart/alternative emails.
"""

from __future__ import annotations

import html as _html_mod
import re

import markdown as _md_lib

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def markdown_to_html(md_text: str) -> str:
    """Convert Markdown to well-formed HTML.

    Raw HTML in the source is escaped first to prevent injection and enforce
    Markdown-only authoring (REQ-EMAIL-001, REQ-EMAIL-005).
    """
    sanitized = _html_mod.escape(md_text)
    return _md_lib.markdown(sanitized, extensions=["nl2br", "tables", "fenced_code"])


def markdown_to_plaintext(md_text: str) -> str:
    """Strip Markdown syntax to a clean plain-text fallback (REQ-EMAIL-002)."""
    text = md_text
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}(.+?)_{1,3}", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^\s*[*+]\s+", "- ", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


def validate_email(address: str) -> bool:
    """Return True if *address* is syntactically valid."""
    return bool(EMAIL_RE.match(address.strip()))


def inject_forced_cc(cc: list[str], forced_address: str) -> list[str]:
    """Ensure *forced_address* is present in *cc* (case-insensitive dedup)."""
    normalised = {addr.strip().lower() for addr in cc}
    if forced_address.lower() not in normalised:
        cc = list(cc) + [forced_address]
    return cc
