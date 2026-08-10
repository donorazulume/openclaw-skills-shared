"""Resolve GitHub PAT for Issue #290 exec-sanitized environments.

Delegates to the unified token_resolver (Issue #290 follow-up).
"""

from __future__ import annotations

import sys
import pathlib
_LIB_DIR = str(pathlib.Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
from token_resolver import resolve_secret


def resolve_github_pat() -> str | None:
    """PAT from env (if present) or first readable mirrored secrets file."""
    tok = resolve_secret("GITHUB_TOKEN")
    if tok:
        return tok
    return resolve_secret("GH_TOKEN")
