"""
mattermost_formatter.py — Mattermost Agent Messaging Protocol (MSG-001) implementation.

Provides format_agent_response() to transform raw agent outputs into standardized,
tiered, sanitized, and well-formed Mattermost post payloads per MSG-001.
"""

from __future__ import annotations

import json
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
    target_agent_id: str | None
    root_id: str | None
    raw_content: str


class MattermostPostPayload(TypedDict, total=False):
    channel_id: str
    root_id: str | None
    message: str
    props: dict[str, Any] | None
    overflow_posts: list[dict[str, Any]] | None


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
    target_agent_id: str | None,
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
    new_lines: list[str] = []

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


def _handle_raw_json_or_suppression(content: str) -> tuple[str, bool]:
    """Check for NO_REPLY or raw CLI JSON dumps and convert or suppress per MSG-001.

    Returns (processed_content, is_suppressed).
    """
    stripped = content.strip()
    if not stripped or stripped == "NO_REPLY":
        return "", True

    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                summary = data.get("summary")
                emails = data.get("emails")
                if summary is not None or emails is not None:
                    total = 0
                    if isinstance(summary, dict):
                        total = summary.get("total_processed", 0)
                    email_list = emails if isinstance(emails, list) else []
                    if total == 0 and not email_list:
                        # Clean/empty inbox: suppress per Silent-When-Clean (REQ-CRON-013)
                        return "", True

                    # Convert to human-readable MSG-001 Markdown
                    lines = [
                        "### 📥 Executive Email Triage Summary",
                        f"> **TL;DR**: Triaged {len(email_list)} email(s) ({total} total processed).",
                        "",
                    ]
                    for em in email_list:
                        if isinstance(em, dict):
                            lines.append(f"- **From**: {em.get('from', 'unknown')}")
                            lines.append(f"  **Subject**: {em.get('subject', 'No Subject')}")
                            lines.append(f"  **Category**: `{em.get('label', 'General')}`")
                            snip = em.get("snippet", "")
                            if snip:
                                lines.append(f"  **Snippet**: {snip[:200]}")
                    return "\n".join(lines), False
        except Exception:
            pass

    return content, False


def _chunk_text_by_paragraphs(text: str, max_chunk_chars: int = 3500) -> list[str]:
    """Split text into chunks up to max_chunk_chars, respecting paragraph (\\n\\n) boundaries.

    If an individual paragraph exceeds max_chunk_chars, it is split on newlines (\\n)
    or sliced so no chunk ever exceeds max_chunk_chars or MAX_POST_CHARS (4000).
    Raises ValueError if any chunk exceeds MAX_POST_CHARS.
    """
    if not text:
        return []

    raw_paragraphs = text.split("\n\n")
    paragraphs: list[str] = []
    for p in raw_paragraphs:
        p_str = p.strip()
        if not p_str:
            continue
        if len(p_str) <= max_chunk_chars:
            paragraphs.append(p_str)
        else:
            lines = p_str.split("\n")
            cur_line_chunk: list[str] = []
            cur_line_len = 0
            for line in lines:
                if len(line) > max_chunk_chars:
                    if cur_line_chunk:
                        paragraphs.append("\n".join(cur_line_chunk))
                        cur_line_chunk = []
                        cur_line_len = 0
                    for k in range(0, len(line), max_chunk_chars):
                        paragraphs.append(line[k : k + max_chunk_chars])
                elif cur_line_len + len(line) + 1 > max_chunk_chars:
                    paragraphs.append("\n".join(cur_line_chunk))
                    cur_line_chunk = [line]
                    cur_line_len = len(line)
                else:
                    cur_line_chunk.append(line)
                    cur_line_len += len(line) + 1
            if cur_line_chunk:
                paragraphs.append("\n".join(cur_line_chunk))

    chunks: list[str] = []
    current_chunk: list[str] = []
    current_len = 0

    for p in paragraphs:
        added_len = len(p) if current_len == 0 else len(p) + 2
        if current_len + added_len <= max_chunk_chars:
            current_chunk.append(p)
            current_len += added_len
        else:
            if current_chunk:
                chunk_str = "\n\n".join(current_chunk)
                if len(chunk_str) > MAX_POST_CHARS:
                    raise ValueError(f"Generated chunk length {len(chunk_str)} exceeds MAX_POST_CHARS ({MAX_POST_CHARS})")
                chunks.append(chunk_str)
            current_chunk = [p]
            current_len = len(p)

    if current_chunk:
        chunk_str = "\n\n".join(current_chunk)
        if len(chunk_str) > MAX_POST_CHARS:
            raise ValueError(f"Generated chunk length {len(chunk_str)} exceeds MAX_POST_CHARS ({MAX_POST_CHARS})")
        chunks.append(chunk_str)

    return chunks


def format_agent_response(payload: AgentResponsePayload | dict[str, Any]) -> Any:
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

    # 0. Check for suppression or raw JSON dumps (Issue #812 / MSG-001 / REQ-CRON-013)
    content, is_suppressed = _handle_raw_json_or_suppression(raw_content)
    if is_suppressed:
        return {
            "channel_id": channel_id,
            "root_id": root_id,
            "message": "",
            "props": {"formatted_by": "MSG-001", "suppressed": True},
            "overflow_posts": [],
        }

    # 1. Credential Sanitization
    content = sanitize_output(content)

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

    # 4. Overflow Handling (> 4000 chars / ERR_MSG_TOO_LONG / Issue #818)
    overflow_posts: list[dict[str, Any]] = []
    if len(content) > MAX_POST_CHARS:
        log.warning("Message length %d exceeds MAX_POST_CHARS (%d) — splitting into thread", len(content), MAX_POST_CHARS)

        # Primary post gets first paragraph chunk near 1000-1500 chars + continuation notice
        notice = "\n\n*(Content exceeds 4000 characters — detailed breakdown continues in thread below)*"
        target_split = 1200

        # Look for paragraph boundary between 600 and 1600
        p_matches = [m.start() for m in re.finditer(r"\n\n", content[:1800])]
        valid_p = [pos for pos in p_matches if 600 <= pos <= 1600]
        if valid_p:
            split_pos = min(valid_p, key=lambda pos: abs(pos - target_split))
        else:
            l_matches = [m.start() for m in re.finditer(r"\n", content[:1800])]
            valid_l = [pos for pos in l_matches if 600 <= pos <= 1600]
            if valid_l:
                split_pos = min(valid_l, key=lambda pos: abs(pos - target_split))
            else:
                split_pos = 1200

        primary_body = content[:split_pos].rstrip() + notice
        remaining_content = content[split_pos:].lstrip()

        chunks = _chunk_text_by_paragraphs(remaining_content, max_chunk_chars=3500)
        for chunk in chunks:
            if len(chunk) > MAX_POST_CHARS:
                raise ValueError(f"Generated chunk length {len(chunk)} exceeds MAX_POST_CHARS ({MAX_POST_CHARS})")
            overflow_posts.append({
                "channel_id": channel_id,
                "message": chunk,
            })

        if len(primary_body) > MAX_POST_CHARS:
            raise ValueError(f"Primary body length {len(primary_body)} exceeds MAX_POST_CHARS ({MAX_POST_CHARS})")

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
