"""Resolve GitHub PAT for Issue #290 exec-sanitized environments.

OpenClaw's `tools.exec` strips GITHUB_TOKEN/GH_TOKEN from subprocess env on
host-side invocations to prevent token exfiltration via a compromised skill.
The gateway entrypoint mirrors the compose-injected PAT to a file at
`~/.openclaw/secrets/github_token` (see openclaw-docker/scripts/entrypoint.sh
+ Issue #290). This helper transparently lets Python skills recover the PAT
from either env (when available, e.g. non-main sandbox subprocess) or the
mirrored secrets file (when env is sanitised, e.g. main-session host exec).

Contract for skills that touch GitHub:
    from github_token import resolve_github_pat
    token = resolve_github_pat()
    if not token:
        sys.exit("ERROR: GITHUB_TOKEN unavailable (env + mirror both empty).")

Do NOT read `os.environ["GITHUB_TOKEN"]` directly — that fails under the
sanitised host-exec path and is the exact regression this shim prevents.
"""

from __future__ import annotations

import os
from pathlib import Path


def _normalize_github_pat(raw: str | None) -> str | None:
    """Return a usable PAT or None (strips whitespace, rejects placeholders)."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    if s.startswith("${") and "}" in s:
        return None
    return s


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = os.path.normpath(str(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _candidate_token_files() -> list[Path]:
    """Ordered list of mirror-file candidates.

    Priority:
      1. Explicit override via $GITHUB_TOKEN_FILE (for tests / custom deploys).
      2. $OPENCLAW_WORKSPACE-relative (gateway container path).
      3. Canonical container path (/home/node/.openclaw/secrets/github_token).
      4. VM host bind path (/opt/openclaw/data/secrets/github_token) — used
         when a subprocess runs outside the gateway container (rare).
      5. $HOME-relative (CI / laptop invocations).
    """
    paths: list[Path] = []

    override = os.environ.get("GITHUB_TOKEN_FILE")
    if override:
        paths.append(Path(override))

    ws = os.environ.get("OPENCLAW_WORKSPACE", "").strip()
    if ws:
        paths.append(Path(ws).resolve().parent / "secrets" / "github_token")

    paths.extend(
        [
            Path("/home/node/.openclaw/secrets/github_token"),
            Path("/opt/openclaw/data/secrets/github_token"),
        ]
    )

    home = Path.home()
    if str(home) not in ("/", ""):
        paths.append(home / ".openclaw" / "secrets" / "github_token")

    return _dedupe_paths(paths)


def resolve_github_pat() -> str | None:
    """Return the GitHub PAT from env or a mirrored secrets file; None if absent."""
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        tok = _normalize_github_pat(os.environ.get(key))
        if tok:
            return tok

    for path in _candidate_token_files():
        try:
            if path.is_file():
                tok = _normalize_github_pat(path.read_text(encoding="utf-8"))
                if tok:
                    return tok
        except OSError:
            continue
    return None
