"""Retired shared Google OAuth client (SPEC-GAUTH-001 v2.0.0, #323/#324).

`openclaw-mcp-google` (port 8103 in the openclaw-docker stack) is the sole process
that holds the Google OAuth refresh token, refreshes it, or writes Doppler. Every
skill MUST now call MCP Google over HTTP via :mod:`mcp_google`.

Importing this module raises :class:`RuntimeError` so the next caller is forced
to migrate to `mcp_google.call("google_mail_*" / "google_drive_*" / "google_calendar_*", ...)`.
"""

raise RuntimeError(
    "openclaw-skills-shared/lib/google_clients.py is retired (#323/#324). "
    "Import `mcp_google` and call `mcp_google.call(<tool>, <args>)` instead — "
    "see openclaw-docker/skills/lib/mcp_google.py for the public surface."
)
