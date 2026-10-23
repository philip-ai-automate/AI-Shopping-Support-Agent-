"""KeyLand: the agent's voice note on a lead (2026-10-22).

After meeting or calling a buyer, the agent records what happened on the lead
page. KeyLand keeps the recording, turns it into text (AI backend
/estate-transcribe), and the AI reads it into a proposal (/estate-agent-note):
a line for the interaction log, the buyer's details, readiness and the next
action. The agent checks it and saves; nothing changes before they do.

Table (new, created once):
  re_agent_voice_notes  each recording: who, lead, length, text, the log entry it became
"""
import os
import re
import subprocess
from datetime import datetime

import psycopg2.extras

from db import get_db_connection

AI_URL = os.getenv("AI_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
VOICE_DIR = os.getenv("KEYLAND_VOICE_DIR", "/root/phixtra-app/data/keyland_voice")
MAX_SECS = 300            # agent notes: 5 minutes at most
MAX_BYTES = 15 * 1024 * 1024

_ready = False


def ensure_tables():
    global _ready
    if _ready:
        return
    conn = get_db_connection()
    if not conn:
        return
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('re_agent_voice_notes') IS NOT NULL")
        if not cur.fetchone()[0]:
            cur.execute("SET lock_timeout = '3s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS re_agent_voice_notes (
                id BIGSERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                contact_id INTEGER NOT NULL,
                staff_id INTEGER,
                file_path TEXT NOT NULL,
                seconds INTEGER,
                transcript TEXT,
                interaction_id INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS re_agent_voice_notes_contact ON re_agent_voice_notes (contact_id)")
        _ready = True
    except Exception as e:
        print(f"⚠️ [KL AGENT VOICE] ensure_tables: {e}")
    finally:
        conn.close()


def _probe_secs(path: str):
    try:
        out = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return int(round(float(out))) if out else None
    except Exception:
        return None


def save_recording(tenant_id: int, contact_id: int, staff_id, data: bytes) -> tuple:
    """Keep the recording as mp3 (plays on every phone). Returns (note_id, seconds, problem)."""
    ensure_tables()
    if not data or len(data) > MAX_BYTES:
        return None, None, "The recording is empty or too large."
    folder = os.path.join(VOICE_DIR, str(tenant_id), "agent")
    os.makedirs(folder, exist_ok=True)
    stem = os.path.join(folder, f"{contact_id}_{datetime.now():%Y%m%d%H%M%S}_{os.getpid()}")
    raw = stem + ".upload"
    with open(raw, "wb") as f:
        f.write(data)
    mp3 = stem + ".mp3"
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-ac", "1", "-b:a", "48k", mp3],
                       check=True, timeout=120)
    except Exception as e:
        print(f"⚠️ [KL AGENT VOICE] convert failed: {e}")
        return None, None, "The recording couldn't be read. Please record it again."
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass
    secs = _probe_secs(mp3)
    if secs is not None and secs > MAX_SECS:
        os.remove(mp3)
        return None, secs, "That's longer than 5 minutes. Please record a shorter note with the main points."
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""INSERT INTO re_agent_voice_notes (tenant_id, contact_id, staff_id, file_path, seconds)
                   VALUES (%s,%s,%s,%s,%s) RETURNING id""", (tenant_id, contact_id, staff_id, mp3, secs))
    note_id = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    return note_id, secs, ""


def note(tenant_id: int, note_id: int):
    ensure_tables()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM re_agent_voice_notes WHERE id=%s AND tenant_id=%s", (note_id, tenant_id))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


def _api_key(tenant_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""SELECT api_key_plain FROM re_api_keys WHERE tenant_id=%s AND is_active AND api_key_plain IS NOT NULL
                   ORDER BY id LIMIT 1""", (tenant_id,))
    r = cur.fetchone()
    cur.close(); conn.close()
    return r[0] if r else None


def transcribe(tenant_id: int, note_id: int, http=None) -> str:
    """Speech → text. Returns '' if it couldn't be heard."""
    import base64
    import requests
    http = http or requests
    n = note(tenant_id, note_id)
    key = _api_key(tenant_id)
    if not n or not key:
        return ""
    with open(n["file_path"], "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    try:
        r = http.post(AI_URL + "/estate-transcribe", json={"api_key": key, "audio_b64": b64, "filename": "voice.mp3"},
                      timeout=90)
        text = (r.json().get("text") or "").strip() if r.status_code == 200 else ""
    except Exception as e:
        print(f"⚠️ [KL AGENT VOICE] transcribe: {e}")
        text = ""
    if text:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE re_agent_voice_notes SET transcript=%s WHERE id=%s", (text, note_id))
        conn.commit(); cur.close(); conn.close()
    return text


def read(tenant_id: int, contact_id: int, transcript: str, now_local: str, http=None) -> dict:
    """The AI's proposal for the lead from the agent's words."""
    import requests
    http = http or requests
    try:
        r = http.post(AI_URL + "/estate-agent-note", json={"tenant_id": tenant_id, "customer_id": contact_id,
                                                            "transcript": transcript, "now_local": now_local}, timeout=90)
        return r.json() if r.status_code == 200 else {"ok": False}
    except Exception as e:
        print(f"⚠️ [KL AGENT VOICE] read: {e}")
        return {"ok": False}


def num(v):
    try:
        x = float(re.sub(r"[^\d.]", "", str(v or "")))
        return x if x > 0 else None
    except ValueError:
        return None
