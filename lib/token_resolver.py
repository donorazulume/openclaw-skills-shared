"""Resolve secrets and tokens for OpenClaw exec-sanitized environments.

Provides fallback resolution logic for credentials and environment variables stripped
from subprocess execution environments under tools.exec.security: "full".
"""

from __future__ import annotations

import os
from pathlib import Path


def _normalize_secret(raw: str | None) -> str | None:
    """Return usable secret or None."""
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


def _candidate_secret_files(key_name: str) -> list[Path]:
    paths: list[Path] = []
    fn = key_name.lower()

    # Custom override file: e.g. GITHUB_TOKEN_FILE for GITHUB_TOKEN
    override = os.environ.get(f"{key_name}_FILE")
    if override:
        paths.append(Path(override))

    ws = os.environ.get("OPENCLAW_WORKSPACE", "").strip()
    if ws:
        paths.append(Path(ws).resolve().parent / "secrets" / fn)

    paths.extend(
        [
            Path(f"/home/node/.openclaw/secrets/{fn}"),
            # VM host bind when subprocess is host exec
            Path(f"/opt/openclaw/data/secrets/{fn}"),
        ]
    )

    home = Path.home()
    if str(home) not in ("/", ""):
        paths.append(home / ".openclaw" / "secrets" / fn)

    return _dedupe_paths(paths)


def resolve_secret(key_name: str) -> str | None:
    """Resolve secret from env (if present) or first readable mirrored secrets file."""
    val = _normalize_secret(os.environ.get(key_name))
    if val:
        return val

    for path in _candidate_secret_files(key_name):
        try:
            if path.is_file():
                val = _normalize_secret(path.read_text(encoding="utf-8"))
                if val:
                    return val
        except OSError:
            continue
    return None
