"""Retired shim (SPEC-GAUTH-001 revised, #323/#324).

The gateway no longer mints or holds Google OAuth credentials. Every Google API
operation goes through ``openclaw-mcp-google`` over HTTP.

Importing this module raises :class:`RuntimeError` so any caller that still relies
on ``get_credentials`` is forced to migrate to :mod:`mcp_google`.

Migration cheat-sheet:

    # before
    from google_clients import get_credentials
    creds = get_credentials(SCOPES)
    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()

    # after
    import mcp_google
    profile = mcp_google.call("google_mail_search", {"query": "me", "max_results": 1})
    # or use a tailored MCP tool (google_mail_send, google_drive_list, ...).
"""

raise RuntimeError(
    "skills/lib/google_clients.py is retired (#323/#324). "
    "Import `mcp_google` and call `mcp_google.call('google_mail_*' | 'google_drive_*' | 'google_calendar_*', ...)` instead."
)
