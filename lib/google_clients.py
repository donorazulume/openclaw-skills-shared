"""
Shared Google Authentication Logic.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials


# ── Logging ──────────────────────────────────────────────────────────

log = logging.getLogger("google-auth-lib")

# Doppler secrets that store the token — we write back to all of these
# after a successful refresh so the stored token stays current.
_TOKEN_DOPPLER_SECRETS = ["GOOGLE_TOKEN_JSON", "GMAIL_TOKEN_JSON"]


def _build_token_dict(creds: Credentials) -> dict[str, Any]:
    """Serialise a Credentials object back to the token.json dict format."""
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else [],
        "expiry": creds.expiry.isoformat() + "Z" if creds.expiry else None,
    }


def _write_token_to_doppler(creds: Credentials, source_env_var: str) -> None:
    """Push the refreshed token back to Doppler so it never goes stale.

    Uses DOPPLER_READ_TOKEN (a config token with write access) already
    injected into the container via .env.  Writes to every secret name
    in _TOKEN_DOPPLER_SECRETS plus the source_env_var.

    Failure is intentionally non-fatal — a warning is logged and execution
    continues with the in-memory credentials.
    """
    doppler_token = os.environ.get("DOPPLER_READ_TOKEN", "").strip()
    doppler_project = os.environ.get("DOPPLER_PROJECT", "openclaw-docker").strip()
    doppler_config = os.environ.get("DOPPLER_CONFIG", "dev").strip()

    if not doppler_token:
        log.warning(
            "DOPPLER_READ_TOKEN not set — refreshed Google token not persisted to Doppler."
        )
        return

    token_json = json.dumps(_build_token_dict(creds))

    # Write to all canonical token secret names (deduplicated)
    secret_names = list(dict.fromkeys(_TOKEN_DOPPLER_SECRETS + [source_env_var]))

    payload = {
        "project": doppler_project,
        "config": doppler_config,
        "secrets": {name: token_json for name in secret_names},
    }

    try:
        resp = requests.post(
            "https://api.doppler.com/v3/configs/config/secrets",
            headers={
                "Authorization": f"Bearer {doppler_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            log.info(
                "Refreshed Google token persisted to Doppler (%s/%s): %s",
                doppler_project, doppler_config,
                ", ".join(secret_names),
            )
        else:
            log.warning(
                "Doppler token write returned HTTP %d — token refreshed in memory only.",
                resp.status_code,
            )
    except Exception as exc:
        log.warning("Doppler write failed (%s) — token refreshed in memory only.", exc)


def get_credentials(
    scopes: list[str],
    token_json_env_vars: list[str] | None = None,
) -> Credentials:
    """Build OAuth2 credentials from environment variables.

    After a successful token refresh the new access token is written back to
    Doppler so the stored credentials never appear stale.  The refresh_token
    (permanent unless revoked) is preserved in both the in-memory credentials
    and the Doppler secret, ensuring uninterrupted long-term operation.
    """
    if token_json_env_vars is None:
        token_json_env_vars = ["GOOGLE_TOKEN_JSON"]

    token_json = ""
    used_env_var = ""

    for env_var in token_json_env_vars:
        token_json = os.environ.get(env_var, "").strip()
        if token_json:
            used_env_var = env_var
            break

    if not token_json:
        checked_vars = ", ".join(token_json_env_vars)
        sys.exit(
            f"ERROR: No Google OAuth2 token found in environment variables: {checked_vars}. "
            "Store your OAuth2 token.json content in one of these env vars via Doppler."
        )

    try:
        token_data = json.loads(token_json)
    except json.JSONDecodeError as exc:
        sys.exit(f"ERROR: {used_env_var} is not valid JSON — {exc}")

    creds = Credentials.from_authorized_user_info(token_data, scopes)

    if creds and creds.expired and creds.refresh_token:
        log.info("Access token expired — refreshing via refresh_token…")
        try:
            creds.refresh(Request())
            log.info("Token refreshed successfully (new expiry: %s).", creds.expiry)
            # Update the in-process env var so subsequent calls in the same
            # process see a fresh token without needing to refresh again.
            refreshed_json = json.dumps(_build_token_dict(creds))
            os.environ[used_env_var] = refreshed_json
            # Persist the refreshed token to Doppler so it never goes stale
            _write_token_to_doppler(creds, used_env_var)
        except Exception as exc:
            log.error("Token refresh failed: %s", exc)

    if not creds or not creds.valid:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
        if creds_json:
            log.warning(
                "Token invalid and credentials.json available — "
                "but interactive OAuth flow cannot run inside Docker. "
                "Re-run scripts/google-reauth.py on your host and update Doppler."
            )
        sys.exit(
            "ERROR: Google credentials are not valid and cannot be refreshed. "
            f"Re-authorize on your host machine and update {used_env_var} in Doppler."
        )

    return creds
