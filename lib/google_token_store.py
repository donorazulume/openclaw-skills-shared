"""SPEC-GAUTH-001 — canonical Google OAuth token storage and refresh for Openclaw.

Single entry point: ``get_credentials``. Resolves ``token_file_path()`` as
``GOOGLE_TOKEN_FILE``, else the first writable path among ``/var/openclaw/secrets`` and
``/opt/openclaw/secrets``, else ``~/.openclaw/secrets`` or a per-uid dir under ``$TMPDIR``
(sandbox/dev without mounts). Reads that file when present,
falls back to ``GOOGLE_TOKEN_JSON`` env, refreshes via raw RFC 6749 §6 POST (not
``Credentials.refresh()``), serialises refresh with ``fcntl.flock``, atomically
writes the token file, then best-effort async Doppler sync (current config only).

Logger: ``openclaw.gauth`` — never log raw tokens (use ``<redacted>``).
"""

from __future__ import annotations

import datetime
import fcntl
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials

log = logging.getLogger("openclaw.gauth")

# Full OAuth scope URLs (must match scripts/google-reauth.py).
DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
]

REFRESH_IF_WITHIN_SEC = 300

DOPPLER_API = "https://api.doppler.com/v3"
DOPPLER_PROJECT = "openclaw-docker"
DOPPLER_SYNC_CONFIGS = ["dev", "prd", "dev_personal"]

_GOOGLE_TOKEN_ENV = "GOOGLE_TOKEN_JSON"
_GMAIL_TOKEN_ENV = "GMAIL_TOKEN_JSON"

# Both GOOGLE_TOKEN_JSON and GMAIL_TOKEN_JSON must stay in sync — same OAuth token.
# Gmail triage/dispatch read GMAIL_TOKEN_JSON; MCP Google/google_token_store read GOOGLE_TOKEN_JSON.
# sync_token_to_all_doppler_configs updates both to prevent stale-token disruptions.


class GoogleReauthRequired(Exception):
    """Refresh token revoked, missing, or invalid_grant — browser reauth required."""


class InsecureTokenStore(Exception):
    """Token file exists but permissions are not owner-read/write only (0600)."""


_mem_lock = threading.Lock()
_mem_mtime: float | None = None
_mem_scopes: tuple[str, ...] | None = None
_mem_creds: Credentials | None = None


def _dir_writable(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK)


def _fallback_token_parent() -> Path:
    """ Writable directory for sandbox/laptop runs when canonical paths exist but deny uid. """
    home = Path(os.environ.get("HOME", "") or "").expanduser()
    if home.is_dir():
        d = home / ".openclaw" / "secrets"
        try:
            d.mkdir(parents=True, mode=0o700, exist_ok=True)
        except OSError:
            pass
        if _dir_writable(d):
            return d
    base = Path(tempfile.gettempdir())
    fb = base / f"openclaw-secrets-{os.getuid()}"
    try:
        fb.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        pass
    if _dir_writable(fb):
        return fb
    return base


def token_file_path() -> Path:
    explicit = os.environ.get("GOOGLE_TOKEN_FILE", "").strip()
    if explicit:
        return Path(explicit)

    fname = Path("google-token.json")
    for parent in (
        Path("/var/openclaw/secrets"),
        Path("/opt/openclaw/secrets"),
    ):
        cand = parent / fname
        if _dir_writable(parent):
            return cand

    return _fallback_token_parent() / fname


def lock_file_path() -> Path:
    return token_file_path().parent / "google-token.lock"


def health_file_path() -> Path:
    return token_file_path().parent / "google-token.health"


def meta_file_path() -> Path:
    return token_file_path().parent / "google-token.meta.json"


def token_file_mtime() -> float | None:
    tp = token_file_path()
    if not tp.is_file():
        return None
    return tp.stat().st_mtime


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _client_config_from_env() -> dict[str, Any]:
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data.get("installed") or data.get("web") or {}


def _check_secure_token_file(path: Path) -> None:
    if not path.is_file():
        return
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise InsecureTokenStore(
            f"{path} must be chmod 0600 (got {oct(mode)}); refusing to read per SPEC-GAUTH-001."
        )


def _parse_expiry_remaining_seconds(token_data: dict[str, Any]) -> float | None:
    expiry_str = (token_data.get("expiry") or "").strip()
    if not expiry_str:
        return None
    try:
        clean = expiry_str.rstrip("Z").split(".")[0]
        if expiry_str.endswith("Z"):
            exp = datetime.datetime.fromisoformat(clean).replace(tzinfo=datetime.timezone.utc)
        else:
            exp = datetime.datetime.fromisoformat(clean)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=datetime.timezone.utc)
        return (exp - _utcnow()).total_seconds()
    except Exception:
        return None


def token_expiring_within(token_data: dict[str, Any], within_sec: int = REFRESH_IF_WITHIN_SEC) -> bool:
    rem = _parse_expiry_remaining_seconds(token_data)
    if rem is None:
        return True
    return rem < within_sec


def _load_token_dict_raw() -> tuple[dict[str, Any], str]:
    """Return (token_dict, source) where source is 'file' or 'env'."""
    tp = token_file_path()
    if tp.is_file():
        _check_secure_token_file(tp)
        try:
            data = json.loads(tp.read_text(encoding="utf-8"))
            return data, "file"
        except json.JSONDecodeError as exc:
            raise GoogleReauthRequired(f"token file invalid JSON: {exc}") from exc

    raw_env = os.environ.get(_GOOGLE_TOKEN_ENV, "").strip()
    if raw_env:
        try:
            data = json.loads(raw_env)
            return data, "env"
        except json.JSONDecodeError as exc:
            raise GoogleReauthRequired(f"{_GOOGLE_TOKEN_ENV} invalid JSON: {exc}") from exc

    raise GoogleReauthRequired(
        "No Google token: missing token file and GOOGLE_TOKEN_JSON — run `make google-reauth` (SPEC-GAUTH-001)."
    )


def _credentials_valid_enough(token_data: dict[str, Any], scopes: list[str]) -> bool:
    try:
        creds = Credentials.from_authorized_user_info(token_data, scopes)
    except Exception:
        return False
    return bool(creds and creds.valid and not token_expiring_within(token_data, REFRESH_IF_WITHIN_SEC))


def _invalidate_memory_cache() -> None:
    global _mem_mtime, _mem_scopes, _mem_creds
    with _mem_lock:
        _mem_mtime = None
        _mem_scopes = None
        _mem_creds = None


def _refresh_via_rest(token_data: dict[str, Any], client_config: dict[str, Any]) -> dict[str, Any]:
    import requests

    refresh_token = token_data.get("refresh_token", "")
    if not refresh_token:
        raise GoogleReauthRequired("refresh_token missing in stored token")

    client_id = client_config.get("client_id", "")
    client_secret = client_config.get("client_secret", "")
    token_uri = client_config.get("token_uri", "https://oauth2.googleapis.com/token")
    if not client_id or not client_secret:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON missing client_id/client_secret")

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            resp = requests.post(token_uri, data=payload, timeout=20)
            if resp.status_code == 200:
                body = resp.json()
                expires_in = int(body.get("expires_in", 3600))
                new_expiry = _utcnow() + datetime.timedelta(seconds=expires_in)
                updated = {
                    **token_data,
                    "token": body["access_token"],
                    "expiry": new_expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "refresh_token": body.get("refresh_token", refresh_token),
                }
                log.info(
                    "gauth.refresh.success expiry=%s",
                    updated.get("expiry", "<redacted>"),
                )
                return updated

            err = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            code = err.get("error", "unknown")
            desc = err.get("error_description", "")
            log.critical(
                "gauth.refresh.failure http=%s error=%s desc=%s",
                resp.status_code,
                code,
                desc or "<redacted>",
            )
            if code == "invalid_grant":
                _record_invalid_grant_health({"error": code, "error_description": desc})
                raise GoogleReauthRequired(f"invalid_grant: {desc}".strip())
            raise RuntimeError(f"{code}: {desc}")

        except GoogleReauthRequired:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            log.warning("gauth.refresh.network_attempt=%s err=%s", attempt + 1, exc)
            if attempt == 0:
                time.sleep(2)
    raise RuntimeError(f"OAuth token endpoint unreachable after retry: {last_exc}")


def _record_invalid_grant_health(extra: dict[str, Any]) -> None:
    hp = health_file_path()
    try:
        hp.parent.mkdir(parents=True, exist_ok=True)
        prev: dict[str, Any] = {}
        if hp.is_file():
            prev = json.loads(hp.read_text(encoding="utf-8") or "{}")
        prev.update(
            {
                "last_invalid_grant_at": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                **extra,
            }
        )
        hp.write_text(json.dumps(prev, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("gauth.health_write_failed err=%s", exc)


def atomic_write_token_file(path: Path, token_data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".google-token.",
        suffix=".tmp",
    )
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as wf:
            json.dump(token_data, wf)
            wf.flush()
            os.fsync(wf.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


class _ExclusiveRefreshLock:
    def __enter__(self):
        self._dir = token_file_path().parent
        self._dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        lock_path = lock_file_path()
        self._fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        log.debug("gauth.lock.wait")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        log.debug("gauth.lock.acquired")
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
        return False


_exclusive_refresh_lock = _ExclusiveRefreshLock


def _token_dict_from_credentials(creds: Credentials) -> dict[str, Any]:
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else [],
        "expiry": creds.expiry.isoformat() + "Z" if creds.expiry else None,
    }


def _credential_fresh_enough(creds: Credentials, _scopes_list: list[str]) -> bool:
    """True if access token is outside the proactive refresh window."""
    try:
        td = _token_dict_from_credentials(creds)
        if not td.get("refresh_token"):
            return False
        return not token_expiring_within(td, REFRESH_IF_WITHIN_SEC)
    except Exception:
        return False


def _spawn_doppler_sync_current_config(token_data: dict[str, Any]) -> None:
    import requests

    doppler_token = (
        os.environ.get("DOPPLER_READ_TOKEN", "").strip()
        or os.environ.get("DOPPLER_TOKEN", "").strip()
    )
    if not doppler_token:
        log.warning("gauth.doppler.skip reason=no_token")
        return

    project = os.environ.get("DOPPLER_PROJECT", DOPPLER_PROJECT).strip()
    config = os.environ.get("DOPPLER_CONFIG", "prd").strip()
    secret_name = _GOOGLE_TOKEN_ENV
    body = json.dumps(token_data)

    def worker() -> None:
        try:
            resp = requests.post(
                f"{DOPPLER_API}/configs/config/secrets",
                headers={
                    "Authorization": f"Bearer {doppler_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "project": project,
                    "config": config,
                    "secrets": {secret_name: body},
                },
                timeout=15,
            )
            if resp.status_code in (200, 201):
                log.info("gauth.doppler.sync_ok config=%s", config)
            else:
                log.warning("gauth.doppler.sync_fail http=%s", resp.status_code)
        except Exception as exc:
            log.warning("gauth.doppler.sync_err err=%s", exc)

    threading.Thread(target=worker, daemon=True, name="gauth-doppler-sync").start()


def get_credentials(scopes: list[str] | None = None) -> Credentials:
    """Return valid credentials, refreshing via REST + token file when needed."""
    scopes_list = list(scopes if scopes is not None else DEFAULT_SCOPES)
    scope_key = tuple(scopes_list)
    tp = token_file_path()
    mtime = tp.stat().st_mtime if tp.is_file() else None

    with _mem_lock:
        global _mem_mtime, _mem_scopes, _mem_creds
        if (
            _mem_creds is not None
            and _mem_mtime == mtime
            and _mem_scopes == scope_key
            and _credential_fresh_enough(_mem_creds, scopes_list)
        ):
            log.debug("gauth.cache.hit")
            return _mem_creds

    token_data, _src = _load_token_dict_raw()
    client_config = _client_config_from_env()
    if not client_config:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON is missing or invalid.")

    if not token_expiring_within(token_data, REFRESH_IF_WITHIN_SEC) and _credentials_valid_enough(
        token_data, scopes_list
    ):
        creds = Credentials.from_authorized_user_info(token_data, scopes_list)
        with _mem_lock:
            _mem_mtime = mtime
            _mem_scopes = scope_key
            _mem_creds = creds
        log.debug("gauth.cache.warm_valid")
        return creds

    with _exclusive_refresh_lock():
        token_data, src = _load_token_dict_raw()
        if not token_expiring_within(token_data, REFRESH_IF_WITHIN_SEC) and _credentials_valid_enough(
            token_data, scopes_list
        ):
            creds = Credentials.from_authorized_user_info(token_data, scopes_list)
            mtime2 = tp.stat().st_mtime if tp.is_file() else None
            with _mem_lock:
                _mem_mtime = mtime2
                _mem_scopes = scope_key
                _mem_creds = creds
            return creds

        try:
            new_data = _refresh_via_rest(token_data, client_config)
        except GoogleReauthRequired:
            raise
        atomic_write_token_file(tp, new_data)
        os.environ[_GOOGLE_TOKEN_ENV] = json.dumps(new_data)
        _spawn_doppler_sync_current_config(new_data)
        creds = Credentials.from_authorized_user_info(new_data, scopes_list)
        mtime3 = tp.stat().st_mtime if tp.is_file() else None
        with _mem_lock:
            _mem_mtime = mtime3
            _mem_scopes = scope_key
            _mem_creds = creds
        return creds


def sync_token_to_all_doppler_configs(token_data: dict[str, Any], dry_run: bool) -> list[str]:
    """Push GOOGLE_TOKEN_JSON and GMAIL_TOKEN_JSON to dev/prd/dev_personal (VM cron). Returns failure messages."""
    import requests

    failures: list[str] = []
    tok = os.environ.get("DOPPLER_READ_TOKEN", "").strip() or os.environ.get("DOPPLER_TOKEN", "").strip()
    if not tok:
        return ["No DOPPLER_READ_TOKEN / DOPPLER_TOKEN — cannot sync Doppler"]

    api_token = tok
    secret_payload = json.dumps(token_data)

    for config in DOPPLER_SYNC_CONFIGS:
        if dry_run:
            log.info("gauth.doppler.skip dry_run config=%s", config)
            continue
        try:
            resp = requests.post(
                f"{DOPPLER_API}/configs/config/secrets",
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={
                    "project": DOPPLER_PROJECT,
                    "config": config,
                    "secrets": {_GOOGLE_TOKEN_ENV: secret_payload},
                },
                timeout=30,
            )
            if resp.status_code not in (200, 201):
                failures.append(f"{DOPPLER_PROJECT}/{config} GOOGLE_TOKEN_JSON: HTTP {resp.status_code}")
        except Exception as exc:
            failures.append(f"{DOPPLER_PROJECT}/{config} GOOGLE_TOKEN_JSON: {exc}")

        # Also sync GMAIL_TOKEN_JSON so Gmail-consuming cron jobs (triage, dispatch)
        # always start with a fresh token on container restart.
        try:
            resp2 = requests.post(
                f"{DOPPLER_API}/configs/config/secrets",
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={
                    "project": DOPPLER_PROJECT,
                    "config": config,
                    "secrets": {_GMAIL_TOKEN_ENV: secret_payload},
                },
                timeout=30,
            )
            if resp2.status_code not in (200, 201):
                failures.append(f"{DOPPLER_PROJECT}/{config} GMAIL_TOKEN_JSON: HTTP {resp2.status_code}")
        except Exception as exc:
            failures.append(f"{DOPPLER_PROJECT}/{config} GMAIL_TOKEN_JSON: {exc}")

    return failures


def hydrate_token_file_from_env_if_missing() -> bool:
    """If token file is absent but GOOGLE_TOKEN_JSON is set, write the file (0600)."""
    tp = token_file_path()
    if tp.is_file():
        return False
    raw = os.environ.get(_GOOGLE_TOKEN_ENV, "").strip()
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    atomic_write_token_file(tp, data)
    log.info("gauth.hydrated_token_file path=%s", tp)
    _invalidate_memory_cache()
    return True


def cron_refresh_and_sync_doppler(*, force: bool, dry_run: bool) -> tuple[dict[str, Any] | None, bool, str | None]:
    """VM/host cron: refresh if needed, sync all Doppler configs.

    Returns (updated_token_data_or_none, skipped_refresh_because_fresh, error_message).
    """
    hydrate_token_file_from_env_if_missing()

    try:
        token_data, _ = _load_token_dict_raw()
    except GoogleReauthRequired as exc:
        return None, False, str(exc)

    client_config = _client_config_from_env()
    if not client_config:
        return None, False, "GOOGLE_CREDENTIALS_JSON missing"

    if not force and not token_expiring_within(token_data, REFRESH_IF_WITHIN_SEC):
        return token_data, True, None

    tp = token_file_path()
    try:
        with _exclusive_refresh_lock():
            token_data, _ = _load_token_dict_raw()
            if not force and not token_expiring_within(token_data, REFRESH_IF_WITHIN_SEC):
                return token_data, True, None
            new_data = _refresh_via_rest(token_data, client_config)
            if not dry_run:
                atomic_write_token_file(tp, new_data)
                os.environ[_GOOGLE_TOKEN_ENV] = json.dumps(new_data)
            return new_data, False, None
    except GoogleReauthRequired as exc:
        return None, False, str(exc)
    except Exception as exc:
        return None, False, str(exc)


def get_drive_service_account_credentials():  # noqa: ANN201 — google auth type
    """Phase F (SPEC-GAUTH-001): Drive shared-folder ingestion via GCP SA."""
    from google.oauth2 import service_account

    raw = os.environ.get("GCP_SERVICE_ACCOUNT_KEY", "").strip()
    if not raw:
        raise RuntimeError("GCP_SERVICE_ACCOUNT_KEY is not set")
    info = json.loads(raw)
    return service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )


def health_check() -> dict[str, Any]:
    """Return last persisted health JSON if present (optional helper)."""
    hp = health_file_path()
    if not hp.is_file():
        return {}
    try:
        return json.loads(hp.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {}
