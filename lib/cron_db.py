import os
import json
import sqlite3
import sys
import shutil
import time

JOBS_PATH = os.path.expanduser("~/.openclaw/cron/jobs.json")
SQLITE_PATH = os.path.expanduser("~/.openclaw/state/openclaw.sqlite")

def get_effective_jobs_path() -> str:
    """Return the active cron jobs registry file path.

    If jobs.json.migrated exists, it is the active live registry created/written by OpenClaw gateway.
    Otherwise fallback to jobs.json.
    """
    base_path = os.path.expanduser("~/.openclaw/cron/jobs.json")
    migrated_path = base_path + ".migrated"

    if os.path.exists(migrated_path):
        return os.path.realpath(migrated_path)
    return os.path.realpath(base_path)

def load_jobs() -> dict:
    if os.path.exists(SQLITE_PATH):
        try:
            conn = sqlite3.connect(SQLITE_PATH)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    job_id, name, payload_kind, payload_message, payload_model, 
                    payload_timeout_seconds, session_target, delivery_mode, 
                    delivery_channel, delivery_to, enabled, job_json, state_json 
                FROM cron_jobs
                WHERE store_key = ?
            """, (get_effective_jobs_path(),))
            jobs = []
            for r in cursor.fetchall():
                try:
                    job = json.loads(r[11]) if r[11] else {}
                except Exception:
                    job = {}
                
                job["id"] = r[0]
                job["name"] = r[1]
                job["enabled"] = bool(r[10])
                job["sessionTarget"] = r[6]
                
                payload = job.setdefault("payload", {})
                payload["kind"] = r[2] or "agentTurn"
                payload["message"] = r[3]
                payload["model"] = r[4]
                payload["timeoutMs"] = r[5] * 1000 if r[5] is not None else None
                
                delivery = job.setdefault("delivery", {})
                delivery["mode"] = r[7]
                delivery["channel"] = r[8]
                delivery["to"] = r[9]
                
                try:
                    state = json.loads(r[12]) if r[12] else {}
                except Exception:
                    state = {}
                job["state"] = state
                
                jobs.append(job)
            conn.close()
            return {"jobs": jobs}
        except Exception as e:
            print(f"DEBUG: SQLite load failed ({e}), falling back to JSON", file=sys.stderr)

    path_to_use = get_effective_jobs_path()

    if os.path.exists(path_to_use):
        with open(path_to_use) as f:
            return json.load(f)
    return {"jobs": []}

def save_jobs(data: dict) -> None:
    if os.path.exists(SQLITE_PATH):
        try:
            conn = sqlite3.connect(SQLITE_PATH)
            cursor = conn.cursor()
            for j in data.get("jobs", []):
                payload = j.get("payload", {})
                delivery = j.get("delivery", {})
                state = j.get("state", {})
                
                cursor.execute("SELECT job_json FROM cron_jobs WHERE store_key = ? AND job_id = ?", (get_effective_jobs_path(), j["id"],))
                res = cursor.fetchone()
                try:
                    job_json = json.loads(res[0]) if res and res[0] else {}
                except Exception:
                    job_json = {}
                
                for k, v in j.items():
                    if k not in ("id", "state", "raw_job_json"):
                        job_json[k] = v
                
                timeout_seconds = int(payload.get("timeoutMs") / 1000) if payload.get("timeoutMs") is not None else None
                if res:
                    cursor.execute("""
                        UPDATE cron_jobs 
                        SET name = ?,
                            schedule_expr = ?,
                            schedule_tz = ?,
                            payload_message = ?, 
                            payload_model = ?, 
                            payload_timeout_seconds = ?, 
                            session_target = ?, 
                            delivery_mode = ?, 
                            delivery_channel = ?, 
                            delivery_to = ?,
                            enabled = ?,
                            job_json = ?,
                            state_json = ?,
                            updated_at = STRFTIME('%s', 'now')
                        WHERE store_key = ? AND job_id = ?
                    """, (
                        j.get("name"),
                        j.get("schedule", {}).get("expr"),
                        j.get("schedule", {}).get("tz"),
                        payload.get("message"),
                        payload.get("model"),
                        timeout_seconds,
                        j.get("sessionTarget") or "isolated",
                        delivery.get("mode"),
                        delivery.get("channel"),
                        delivery.get("to"),
                        1 if j.get("enabled", True) else 0,
                        json.dumps(job_json),
                        json.dumps(state),
                        get_effective_jobs_path(),
                        j["id"]
                    ))
                else:
                    cursor.execute("""
                        INSERT INTO cron_jobs (
                            store_key, job_id, name, enabled, created_at_ms,
                            schedule_kind, schedule_expr, schedule_tz,
                            session_target, wake_mode, payload_kind, payload_message, payload_model,
                            delivery_mode, delivery_channel, delivery_to,
                            job_json, state_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, STRFTIME('%s', 'now'))
                    """, (
                        get_effective_jobs_path(),
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
                        json.dumps(job_json),
                        json.dumps(state)
                    ))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"DEBUG: SQLite save failed ({e}), falling back to JSON", file=sys.stderr)

    path_to_use = get_effective_jobs_path()

    os.makedirs(os.path.dirname(path_to_use), exist_ok=True)
    with open(path_to_use, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

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
    if os.path.exists(SQLITE_PATH):
        try:
            conn = sqlite3.connect(SQLITE_PATH)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM cron_jobs WHERE store_key = ? AND job_id = ?", (get_effective_jobs_path(), job_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"DEBUG: SQLite delete failed ({e})", file=sys.stderr)
