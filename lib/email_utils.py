"""
Shared HTML email utilities for all OpenClaw skills.

Provides Markdown→HTML conversion and plain-text stripping used by
gmail-executive, google-manager, and chimex-manager so they all
produce consistent multipart/alternative emails.
"""

from __future__ import annotations

import re
from typing import Any, cast

try:
    import bleach  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]
except ImportError:
    bleach = None

try:
    import markdown as _md_lib  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]
except ImportError:
    _md_lib = None

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

# Allowed subset for professional email HTML (post-Markdown sanitization, REQ-EMAIL-001).
_BLEACH_ALLOWED_TAGS = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "br",
        "code",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "li",
        "ol",
        "p",
        "pre",
        "strong",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

_BLEACH_ALLOWED_ATTRIBUTES = {
    "a": ["href", "title"],
    "table": ["border"],
}

_BLEACH_ALLOWED_PROTOCOLS = frozenset({"http", "https", "mailto"})


def markdown_to_html(md_text: str) -> str:
    """Convert Markdown to well-formed HTML."""
    if _md_lib is not None:
        raw_html = _md_lib.markdown(
            md_text, extensions=["nl2br", "tables", "fenced_code"]
        )
    else:
        lines = [f"<p>{line.strip()}</p>" for line in md_text.splitlines() if line.strip()]
        raw_html = "\n".join(lines)

    if bleach is not None:
        cleaned = bleach.clean(
            raw_html,
            tags=cast(Any, _BLEACH_ALLOWED_TAGS),
            attributes=cast(Any, _BLEACH_ALLOWED_ATTRIBUTES),
            protocols=cast(Any, _BLEACH_ALLOWED_PROTOCOLS),
            strip=True,
        )
        return str(cleaned)
    return raw_html


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
