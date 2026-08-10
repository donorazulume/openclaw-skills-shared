"""
mattermost_formatter.py — Mattermost Agent Messaging Protocol (MSG-001) implementation.

Provides format_agent_response() to transform raw agent outputs into standardized,
tiered, sanitized, and well-formed Mattermost post payloads per MSG-001.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, TypedDict

log = logging.getLogger("mattermost-formatter")

MAX_POST_CHARS = 4000
SHORT_FORM_MAX = 280
MEDIUM_FORM_MAX = 1500

CREDENTIAL_REGEX = re.compile(
    r"(?i)(api_key|bearer|secret)\s*[:=]\s*[\"']?[a-zA-Z0-9_\-]+",
    re.IGNORECASE,
)
GENERIC_TOKEN_REGEX = re.compile(
    r"(?i)\b(AIzaSy[a-zA-Z0-9_\-]{33}|sk-proj-[a-zA-Z0-9_\-]{20,}|ghp_[a-zA-Z0-9]{36})\b"
)


class AgentResponsePayload(TypedDict, total=False):
    agent_id: str
    channel_id: str
    channel_type: str  # "O", "P", "D"
    target_agent_id: Optional[str]
    root_id: Optional[str]
    raw_content: str


class MattermostPostPayload(TypedDict, total=False):
    channel_id: str
    root_id: Optional[str]
    message: str
    props: Optional[Dict[str, Any]]
    overflow_posts: Optional[List[Dict[str, Any]]]


def sanitize_output(content: str) -> str:
    """Scrub internal system credentials, Bearer tokens, or API keys."""
    if not content:
        return content

    def _redact_pair(match: re.Match[str]) -> str:
        match.group(0)
        key_part = match.group(1)
        return f"{key_part}=[REDACTED_CREDENTIAL]"

    sanitized = CREDENTIAL_REGEX.sub(_redact_pair, content)
    sanitized = GENERIC_TOKEN_REGEX.sub("[REDACTED_TOKEN]", sanitized)
    return sanitized


def repair_markdown(content: str) -> str:
    """Auto-close unclosed triple backtick code blocks (ERR_INVALID_MARKDOWN)."""
    if not content:
        return content

    # Count unescaped ``` fences
    fences = len(re.findall(r"(?<!\\)```", content))
    if fences % 2 != 0:
        log.warning("Markdown auto-repair triggered (ERR_INVALID_MARKDOWN): closing unclosed code block")
        content = content.rstrip() + "\n```"
    return content


def inject_a2a_mention(
    content: str,
    channel_type: str,
    target_agent_id: Optional[str],
) -> str:
    """Inject mandatory @<target_agent_handle> at index 0 for A2A coordination (REQ-A2A-001)."""
    if channel_type in ("O", "P") and target_agent_id:
        target_handle = target_agent_id.lstrip("@").strip()
        if not target_handle:
            return content

        mention_str = f"@{target_handle}"
        if mention_str not in content:
            log.info("Auto-injecting missing A2A mention %s at index 0", mention_str)
            content = f"{mention_str} {content.lstrip()}"

    return content


def normalize_headers(content: str, force_h3: bool = False, strip_headers: bool = False) -> str:
    """Format headers: strip H1/H2/H3 for DMs/Short-form or convert H1/H2 to H3 for Medium/Long form."""
    lines = content.splitlines()
    new_lines: List[str] = []

    for line in lines:
        stripped = line.strip()
        if strip_headers:
            if stripped.startswith("#"):
                # Remove header symbols and replace with bold text if non-empty
                header_text = re.sub(r"^#+\s*", "", stripped)
                if header_text:
                    new_lines.append(f"**{header_text}**")
                else:
                    new_lines.append("")
                continue
        elif force_h3:
            if re.match(r"^#{1,2}\s+", stripped):
                # Replace # or ## with ###
                new_line = re.sub(r"^#{1,2}\s+", "### ", stripped)
                new_lines.append(new_line)
                continue

        new_lines.append(line)

    return "\n".join(new_lines)


def ensure_code_language_tags(content: str) -> str:
    """Ensure code blocks specify language tags (default to text)."""
    def _add_tag(match: re.Match[str]) -> str:
        fence = match.group(1)
        lang = match.group(2)
        code = match.group(3)
        if not lang or not lang.strip():
            return f"{fence}text\n{code}{fence}"
        return match.group(0)

    pattern = re.compile(r"(```)([a-zA-Z0-9_\-]*\n)(.*?)(```)", re.DOTALL)
    return pattern.sub(_add_tag, content)


def format_agent_response(payload: AgentResponsePayload) -> MattermostPostPayload:
    """Core MSG-001 Transformer Function.
    
    Transforms AgentResponsePayload into MattermostPostPayload respecting
    formatting tiers, DM protocols, A2A mention injection, credential scrubbing,
    markdown auto-repair, and character safety limits.
    """
    raw_content = payload.get("raw_content", "") or ""
    channel_id = payload.get("channel_id", "")
    channel_type = (payload.get("channel_type") or "O").upper()
    target_agent_id = payload.get("target_agent_id")
    root_id = payload.get("root_id")

    # 1. Credential Sanitization
    content = sanitize_output(raw_content)

    # 2. Markdown Auto-repair
    content = repair_markdown(content)

    # 3. A2A Mention Injection
    content = inject_a2a_mention(content, channel_type, target_agent_id)

    # Determine Tier & DM styling
    char_count = len(content)

    if channel_type == "D":
        # REQ-MSG-004: Direct Message Protocol
        # Exclude structural headers (H1, H2, H3), suppress thread creation (keep linear)
        content = normalize_headers(content, strip_headers=True)
        # Remove TL;DR blockquotes in DMs if present
        content = re.sub(r"(?m)^>\s*\*\*TL;DR:\*\*\s*", "", content)
        effective_root_id = None  # DMs stay linear
    else:
        effective_root_id = root_id

        if char_count < SHORT_FORM_MAX:
            # REQ-MSG-001: Short-Form (<280 chars)
            # Single block, no H1/H2/H3 headers
            content = normalize_headers(content, strip_headers=True)

        elif SHORT_FORM_MAX <= char_count <= MEDIUM_FORM_MAX:
            # REQ-MSG-002: Medium-Form (280–1500 chars)
            # Must start with concise TL;DR blockquote, ### headers only
            content = normalize_headers(content, force_h3=True)
            content = ensure_code_language_tags(content)

            if not re.search(r"(?i)>\s*\*\*TL;DR:\*\*", content):
                # Auto-generate TL;DR from first paragraph
                lines = [l.strip() for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]
                tldr_text = lines[0][:150] + "..." if lines else "Summary of response below."
                tldr_quote = f"> **TL;DR:** {tldr_text}\n\n"
                content = tldr_quote + content

        else:
            # REQ-MSG-003: Long-Form (>1500 chars)
            content = normalize_headers(content, force_h3=True)
            content = ensure_code_language_tags(content)

            if not re.search(r"(?i)>\s*\*\*TL;DR:\*\*", content):
                lines = [l.strip() for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]
                tldr_text = lines[0][:150] + "..." if lines else "Full breakdown attached in thread."
                tldr_quote = f"> **TL;DR:** {tldr_text}\n\n"
                content = tldr_quote + content

    # 4. Overflow Handling (> 4000 chars / ERR_MSG_TOO_LONG)
    overflow_posts: List[Dict[str, Any]] = []
    if len(content) > MAX_POST_CHARS:
        log.warning("Message length %d exceeds MAX_POST_CHARS (%d) — splitting into thread", len(content), MAX_POST_CHARS)

        # Primary post gets first ~1000 chars + notification
        primary_body = content[:1000] + "\n\n*(Content exceeds 4000 characters — detailed breakdown continues in thread below)*"
        remaining_content = content[1000:]

        # Split remaining content into 3500-char chunks
        chunk_size = 3500
        for i in range(0, len(remaining_content), chunk_size):
            chunk = remaining_content[i:i + chunk_size]
            overflow_posts.append({
                "channel_id": channel_id,
                "message": chunk,
            })

        content = primary_body

    result: MattermostPostPayload = {
        "channel_id": channel_id,
        "root_id": effective_root_id,
        "message": content,
        "props": {"formatted_by": "MSG-001"},
    }

    if overflow_posts:
        result["overflow_posts"] = overflow_posts

    return result
