import json
import os
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


def load_jobs() -> dict:
    if os.path.exists(_get_sqlite_path()):
        try:
            conn = sqlite3.connect(_get_sqlite_path())
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    job_id, name, payload_kind, payload_message, payload_model, 
                    payload_timeout_seconds, session_target, delivery_mode, 
                    delivery_channel, delivery_to, enabled, job_json, state_json, schedule_kind 
                FROM cron_jobs
                WHERE store_key = ?
            """, (get_effective_jobs_path(),))
            seen_job_ids = set()
            unique_jobs = []
            for r in cursor.fetchall():
                jid = r[0]
                if jid in seen_job_ids:
                    continue
                seen_job_ids.add(jid)

                try:
                    job = json.loads(r[11]) if r[11] else {}
                except Exception:
                    job = {}
                
                job["id"] = jid
                job["name"] = r[1]
                job["enabled"] = bool(r[10])
                job["sessionTarget"] = r[6]
                
                sched = job.setdefault("schedule", {})
                if not sched.get("kind"):
                    sched_kind_val = r[13] if len(r) > 13 else None
                    if sched_kind_val:
                        sched["kind"] = sched_kind_val
                    elif sched.get("expr"):
                        sched["kind"] = "cron"
                    elif sched.get("everyMs"):
                        sched["kind"] = "every"
                    elif sched.get("at"):
                        sched["kind"] = "at"

                payload = job.setdefault("payload", {})
                if r[2]:
                    payload["kind"] = r[2]
                elif not payload.get("kind"):
                    payload["kind"] = "agentTurn"
                if r[3] is not None:
                    payload["message"] = r[3]
                if r[4] is not None:
                    payload["model"] = r[4]
                if r[5] is not None:
                    payload["timeoutMs"] = r[5] * 1000
                
                delivery = job.setdefault("delivery", {})
                delivery["mode"] = r[7]
                delivery["channel"] = r[8]
                delivery["to"] = r[9]
                
                try:
                    state = json.loads(r[12]) if r[12] else {}
                except Exception:
                    state = {}
                job["state"] = state
                
                unique_jobs.append(job)
            conn.close()
            return {"jobs": unique_jobs}
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
            valid_job_ids = [j["id"] for j in data.get("jobs", []) if "id" in j]
            for store_key in get_all_store_keys():
                if valid_job_ids:
                    placeholders = ",".join("?" for _ in valid_job_ids)
                    cursor.execute(f"DELETE FROM cron_jobs WHERE store_key = ? AND job_id NOT IN ({placeholders})", [store_key] + valid_job_ids)
                else:
                    cursor.execute("DELETE FROM cron_jobs WHERE store_key = ?", (store_key,))

                for j in data.get("jobs", []):
                    payload = j.get("payload", {})
                    delivery = j.get("delivery", {})
                    state = j.get("state", {})
                    sched = j.get("schedule", {})

                    cursor.execute("SELECT job_json FROM cron_jobs WHERE store_key = ? AND job_id = ?", (store_key, j["id"],))
                    res = cursor.fetchone()
                    try:
                        job_json = json.loads(res[0]) if res and res[0] else {}
                    except Exception:
                        job_json = {}

                    for k, v in j.items():
                        if k not in ("id", "state", "raw_job_json"):
                            job_json[k] = v

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
                            j.get("name"),
                            sched.get("kind", "cron"),
                            sched.get("expr"),
                            sched.get("tz"),
                            payload.get("kind", "agentTurn"),
                            payload.get("message"),
                            payload.get("model"),
                            timeout_seconds,
                            j.get("sessionTarget") or "isolated",
                            delivery.get("mode"),
                            delivery.get("channel"),
                            delivery.get("to"),
                            1 if j.get("enabled", True) else 0,
                            consecutive_errors,
                            last_run_status,
                            last_error,
                            last_delivery_status,
                            last_delivery_error,
                            json.dumps(job_json),
                            json.dumps(state),
                            store_key,
                            j["id"]
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
                            j["id"],
                            j.get("name"),
                            1 if j.get("enabled", True) else 0,
                            j.get("createdAtMs", int(time.time() * 1000)),
                            "cron",
                            j.get("schedule", {}).get("expr"),
                            j.get("schedule", {}).get("tz"),
                            j.get("sessionTarget", "isolated"),
                            j.get("wakeMode", "now"),
                            payload.get("kind", "agentTurn"),
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
                            json.dumps(job_json),
                            json.dumps(state)
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
    valid_ids = [j["id"] for j in data.get("jobs", []) if "id" in j]

    purged_count = 0
    try:
        conn = sqlite3.connect(_get_sqlite_path())
        cursor = conn.cursor()
        for store_key in get_all_store_keys():
            if valid_ids:
                placeholders = ",".join("?" for _ in valid_ids)
                cursor.execute(f"DELETE FROM cron_jobs WHERE store_key = ? AND job_id NOT IN ({placeholders})", [store_key] + valid_ids)
                purged_count += cursor.rowcount
            else:
                cursor.execute("DELETE FROM cron_jobs WHERE store_key = ?", (store_key,))
                purged_count += cursor.rowcount
            # Synchronize raw error columns across all retained store key rows
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
