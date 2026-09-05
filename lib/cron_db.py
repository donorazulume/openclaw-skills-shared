import json
import os
import re
import shutil
import sqlite3
import sys
import time

OPENCLAW_HOME_DIR = os.getenv("OPENCLAW_HOME", os.path.expanduser("~/.openclaw"))
JOBS_PATH = os.getenv("CRON_STORE_PATH", os.path.join(OPENCLAW_HOME_DIR, "cron", "jobs.json"))
SQLITE_PATH = os.path.join(OPENCLAW_HOME_DIR, "state", "openclaw.sqlite")

def _get_openclaw_home() -> str:
    home = os.getenv("OPENCLAW_HOME", os.path.expanduser("~/.openclaw"))
    if home.endswith("jobs.json"):
        return os.path.dirname(os.path.dirname(home))
    return home

def _get_sqlite_path() -> str:
    return os.path.join(_get_openclaw_home(), "state", "openclaw.sqlite")

def get_effective_jobs_path() -> str:
    """Return the active cron jobs registry file path.

    If jobs.json.migrated exists, it is the active live registry created/written by OpenClaw gateway.
    Otherwise fallback to jobs.json.
    """
    base_path = os.getenv("CRON_STORE_PATH", os.path.join(_get_openclaw_home(), "cron", "jobs.json"))
    migrated_path = base_path + ".migrated"

    if os.path.exists(migrated_path):
        return os.path.realpath(migrated_path)
    return os.path.realpath(base_path)


def get_all_store_keys() -> list[str]:
    base_path = os.path.realpath(os.getenv("CRON_STORE_PATH", os.path.join(_get_openclaw_home(), "cron", "jobs.json")))
    migrated_path = base_path + ".migrated"
    keys = [base_path]
    if os.path.exists(migrated_path):
        keys.append(os.path.realpath(migrated_path))
    return list(dict.fromkeys(keys))


def _get_table_columns(cursor, table_name: str) -> set[str]:
    try:
        cursor.execute(f"PRAGMA table_info({table_name})")
        return {row[1] for row in cursor.fetchall()}
    except Exception:
        return set()


def load_jobs() -> dict:
    if os.path.exists(_get_sqlite_path()):
        try:
            conn = sqlite3.connect(_get_sqlite_path())
            cursor = conn.cursor()
            cols = _get_table_columns(cursor, "cron_jobs")
            if cols:
                cursor.execute("""
                    SELECT job_id, name, payload_kind, enabled, job_json, state_json
                    FROM cron_jobs
                    WHERE store_key = ?
                """, (get_effective_jobs_path(),))
                seen_job_ids = set()
                unique_jobs = []
                for r in cursor.fetchall():
                    jid, name, payload_kind, enabled, job_json_str, state_json_str = r
                    if jid in seen_job_ids:
                        continue
                    seen_job_ids.add(jid)

                    try:
                        job = json.loads(job_json_str) if job_json_str else {}
                    except Exception:
                        job = {}

                    job["id"] = jid
                    if name and not job.get("name"):
                        job["name"] = name
                    job["enabled"] = bool(enabled)
                    sched = job.setdefault("schedule", {})
                    if not sched.get("kind"):
                        if sched.get("expr"):
                            sched["kind"] = "cron"
                        elif sched.get("everyMs"):
                            sched["kind"] = "every"
                        elif sched.get("at"):
                            sched["kind"] = "at"

                    payload = job.setdefault("payload", {})
                    if payload_kind:
                        payload["kind"] = payload_kind
                    elif not payload.get("kind"):
                        payload["kind"] = "agentTurn"

                    try:
                        state = json.loads(state_json_str) if state_json_str else {}
                    except Exception:
                        state = {}
                    job["state"] = state

                    unique_jobs.append(job)
                conn.close()
                if unique_jobs:
                    return {"jobs": unique_jobs}
            else:
                conn.close()
        except Exception as e:
            print(f"DEBUG: SQLite load failed ({e}), falling back to JSON", file=sys.stderr)

    path_to_use = get_effective_jobs_path()

    if os.path.exists(path_to_use):
        with open(path_to_use) as f:
            raw = json.load(f)
            jobs = raw.get("jobs", [])
            seen_job_ids = set()
            unique_jobs = []
            for j in jobs:
                jid = j.get("id")
                if jid:
                    if jid in seen_job_ids:
                        continue
                    seen_job_ids.add(jid)
                unique_jobs.append(j)
            return {"jobs": unique_jobs}
    return {"jobs": []}

def save_jobs(data: dict) -> None:
    if os.path.exists(_get_sqlite_path()):
        try:
            conn = sqlite3.connect(_get_sqlite_path())
            cursor = conn.cursor()
            cols = _get_table_columns(cursor, "cron_jobs")
            if cols:
                for j in data.get("jobs", []):
                    if isinstance(j, dict) and not j.get("id"):
                        name = j.get("name", "")
                        j["id"] = re.sub(r'[^a-zA-Z0-9_-]', '-', name.lower()).strip('-') or f"job-{int(time.time())}"
                valid_job_ids = [j["id"] for j in data.get("jobs", []) if isinstance(j, dict) and "id" in j and j.get("id")]
                now_ms = int(time.time() * 1000)
                for store_key in get_all_store_keys():
                    if "declaration_key" in cols:
                        if valid_job_ids:
                            placeholders = ",".join("?" for _ in valid_job_ids)
                            cursor.execute(f"DELETE FROM cron_jobs WHERE store_key = ? AND declaration_key IS NULL AND job_id NOT IN ({placeholders})", [store_key] + valid_job_ids)
                        else:
                            cursor.execute("DELETE FROM cron_jobs WHERE store_key = ? AND declaration_key IS NULL", (store_key,))
                    else:
                        if valid_job_ids:
                            placeholders = ",".join("?" for _ in valid_job_ids)
                            cursor.execute(f"DELETE FROM cron_jobs WHERE store_key = ? AND job_id NOT IN ({placeholders})", [store_key] + valid_job_ids)
                        else:
                            cursor.execute("DELETE FROM cron_jobs WHERE store_key = ?", (store_key,))

                    for j in data.get("jobs", []):
                        if not isinstance(j, dict) or not j.get("id"):
                            continue
                        jid = j["id"]
                        jname = j.get("name", jid)
                        jdesc = j.get("description")
                        jenabled = 1 if j.get("enabled", True) else 0
                        jagent = j.get("agentId", "main")
                        payload = j.get("payload", {})
                        pkind = payload.get("kind", "command")
                        sched = j.get("schedule", {})
                        delivery = j.get("delivery", {})
                        state = j.get("state", {})
                        jjson = json.dumps(j)
                        sjson = json.dumps(state)

                        if "declaration_key" in cols:
                            cursor.execute("""
                                INSERT INTO cron_jobs (
                                    store_key, job_id, declaration_key, owner_agent_id, name, description,
                                    enabled, agent_id, payload_kind, job_json, state_json, runtime_updated_at_ms,
                                    schedule_identity, sort_order, updated_at
                                ) VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)
                                ON CONFLICT(store_key, job_id) DO UPDATE SET
                                    name = excluded.name,
                                    description = excluded.description,
                                    enabled = excluded.enabled,
                                    payload_kind = excluded.payload_kind,
                                    job_json = excluded.job_json,
                                    updated_at = excluded.updated_at
                            """, (store_key, jid, jname, jdesc, jenabled, jagent, pkind, jjson, sjson, now_ms, now_ms))
                        else:
                            cursor.execute("SELECT job_id FROM cron_jobs WHERE store_key = ? AND job_id = ?", (store_key, jid))
                            res = cursor.fetchone()
                            timeout_seconds = int(payload.get("timeoutMs") / 1000) if payload.get("timeoutMs") is not None else None
                            consecutive_errors = state.get("consecutiveErrors", 0)
                            last_run_status = state.get("lastStatus", "ok")
                            last_error = state.get("lastError")
                            last_delivery_status = state.get("lastDeliveryStatus")
                            last_delivery_error = state.get("lastDeliveryError")

                            if res:
                                cursor.execute("""
                                    UPDATE cron_jobs 
                                    SET name = ?,
                                        schedule_kind = ?,
                                        schedule_expr = ?,
                                        schedule_tz = ?,
                                        payload_kind = ?,
                                        payload_message = ?, 
                                        payload_model = ?, 
                                        payload_timeout_seconds = ?, 
                                        session_target = ?, 
                                        delivery_mode = ?, 
                                        delivery_channel = ?, 
                                        delivery_to = ?,
                                        enabled = ?,
                                        consecutive_errors = ?,
                                        last_run_status = ?,
                                        last_error = ?,
                                        last_delivery_status = ?,
                                        last_delivery_error = ?,
                                        job_json = ?,
                                        state_json = ?,
                                        updated_at = STRFTIME('%s', 'now')
                                    WHERE store_key = ? AND job_id = ?
                                """, (
                                    jname,
                                    sched.get("kind", "cron"),
                                    sched.get("expr"),
                                    sched.get("tz"),
                                    pkind,
                                    payload.get("message"),
                                    payload.get("model"),
                                    timeout_seconds,
                                    j.get("sessionTarget") or "isolated",
                                    delivery.get("mode"),
                                    delivery.get("channel"),
                                    delivery.get("to"),
                                    jenabled,
                                    consecutive_errors,
                                    last_run_status,
                                    last_error,
                                    last_delivery_status,
                                    last_delivery_error,
                                    jjson,
                                    sjson,
                                    store_key,
                                    jid
                                ))
                            else:
                                cursor.execute("""
                                    INSERT INTO cron_jobs (
                                        store_key, job_id, name, enabled, created_at_ms,
                                        schedule_kind, schedule_expr, schedule_tz,
                                        session_target, wake_mode, payload_kind, payload_message, payload_model,
                                        delivery_mode, delivery_channel, delivery_to,
                                        consecutive_errors, last_run_status, last_error, last_delivery_status, last_delivery_error,
                                        job_json, state_json, updated_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, STRFTIME('%s', 'now'))
                                """, (
                                    store_key,
                                    jid,
                                    jname,
                                    jenabled,
                                    j.get("createdAtMs", now_ms),
                                    sched.get("kind", "cron"),
                                    sched.get("expr"),
                                    sched.get("tz"),
                                    j.get("sessionTarget", "isolated"),
                                    j.get("wakeMode", "now"),
                                    pkind,
                                    payload.get("message"),
                                    payload.get("model"),
                                    delivery.get("mode"),
                                    delivery.get("channel"),
                                    delivery.get("to"),
                                    consecutive_errors,
                                    last_run_status,
                                    last_error,
                                    last_delivery_status,
                                    last_delivery_error,
                                    jjson,
                                    sjson
                                ))
                conn.commit()
            conn.close()
        except Exception as e:
            print(f"DEBUG: SQLite save failed ({e}), falling back to JSON", file=sys.stderr)

    for store_key in get_all_store_keys():
        os.makedirs(os.path.dirname(store_key), exist_ok=True)
        with open(store_key, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")


def purge_stale_jobs_db() -> int:
    """Purge stale rows from SQLite cron_jobs table that are not in the effective canonical jobs set.

    Reconciles all store keys (both jobs.json and jobs.json.migrated) in SQLite DB.
    """
    if not os.path.exists(_get_sqlite_path()):
        return 0

    data = load_jobs()
    for j in data.get("jobs", []):
        if isinstance(j, dict) and not j.get("id"):
            name = j.get("name", "")
            j["id"] = re.sub(r'[^a-zA-Z0-9_-]', '-', name.lower()).strip('-') or f"job-{int(time.time())}"
    valid_ids = [j["id"] for j in data.get("jobs", []) if isinstance(j, dict) and "id" in j and j.get("id")]

    purged_count = 0
    try:
        conn = sqlite3.connect(_get_sqlite_path())
        cursor = conn.cursor()
        cols = _get_table_columns(cursor, "cron_jobs")
        for store_key in get_all_store_keys():
            if "declaration_key" in cols:
                if valid_ids:
                    placeholders = ",".join("?" for _ in valid_ids)
                    cursor.execute(f"DELETE FROM cron_jobs WHERE store_key = ? AND declaration_key IS NULL AND job_id NOT IN ({placeholders})", [store_key] + valid_ids)
                    purged_count += cursor.rowcount
                else:
                    cursor.execute("DELETE FROM cron_jobs WHERE store_key = ? AND declaration_key IS NULL", (store_key,))
                    purged_count += cursor.rowcount
            else:
                if valid_ids:
                    placeholders = ",".join("?" for _ in valid_ids)
                    cursor.execute(f"DELETE FROM cron_jobs WHERE store_key = ? AND job_id NOT IN ({placeholders})", [store_key] + valid_ids)
                    purged_count += cursor.rowcount
                else:
                    cursor.execute("DELETE FROM cron_jobs WHERE store_key = ?", (store_key,))
                    purged_count += cursor.rowcount

            if "consecutive_errors" in cols and "last_run_status" in cols:
                cursor.execute("UPDATE cron_jobs SET consecutive_errors = 0, last_run_status = 'ok', last_error = NULL, last_delivery_status = NULL, last_delivery_error = NULL WHERE store_key = ? AND (consecutive_errors > 0 OR last_run_status = 'error')", (store_key,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DEBUG: SQLite purge failed ({e})", file=sys.stderr)

    return purged_count



def delete_job(job_id: str) -> None:
    # Delete from JSON
    path_to_use = get_effective_jobs_path()
    if os.path.exists(path_to_use):
        try:
            with open(path_to_use) as f:
                data = json.load(f)
            jobs = data.get("jobs", [])
            data["jobs"] = [j for j in jobs if j.get("id") != job_id]
            with open(path_to_use, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
        except Exception as e:
            print(f"DEBUG: JSON delete failed ({e})", file=sys.stderr)
            
    # Delete from SQLite
    if os.path.exists(_get_sqlite_path()):
        try:
            conn = sqlite3.connect(_get_sqlite_path())
            cursor = conn.cursor()
            cursor.execute("DELETE FROM cron_jobs WHERE store_key = ? AND job_id = ?", (get_effective_jobs_path(), job_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"DEBUG: SQLite delete failed ({e})", file=sys.stderr)


def set_job_in_flight(job_id: str, in_flight: bool = True) -> None:
    """Issue #513: Persist in-flight status for a running cron job."""
    jobs_data = load_jobs()
    for j in jobs_data.get("jobs", []):
        if j.get("id") == job_id:
            state = j.setdefault("state", {})
            state["in_flight"] = in_flight
            state["in_flight_ts"] = int(time.time()) if in_flight else None
            break
    save_jobs(jobs_data)


def reconcile_interrupted_jobs() -> list[str]:
    """Issue #513: Startup reconciliation for jobs interrupted by gateway restart."""
    recovered = []
    jobs_data = load_jobs()
    for j in jobs_data.get("jobs", []):
        state = j.get("state", {})
        if state.get("in_flight"):
            job_name = j.get("name") or j.get("id")
            recovered.append(job_name)
            state["in_flight"] = False
            state["last_interrupted"] = int(time.time())
            state["last_interrupted_reason"] = "[RECOVERED_INTERRUPTED_CRON] Gateway restart during execution"
    if recovered:
        save_jobs(jobs_data)
    return recovered
