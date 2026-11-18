"""Admin › Backups — reads the status files written by /root/phixtra-backup
scripts (backup.sh, restore_test.sh via status.py). Never touches the backup
key or the backups themselves; buttons only drop a request file that
runner.sh (cron, every minute) picks up. 5 Nov 2026, Part 7A item 1."""
import json, os, datetime as _dt
from zoneinfo import ZoneInfo

BASE = "/root/phixtra-backup"
STATUS = os.path.join(BASE, "status")
REQ = os.path.join(BASE, "requests")
UK = ZoneInfo("Europe/London")
NIGHTLY_SERVER_HOUR = 3            # cron 0 3 * * * (server time Europe/Amsterdam)
SERVER_TZ = ZoneInfo("Europe/Amsterdam")
KEEP = "30 daily + 12 monthly"


def _load(name, default=None):
    try:
        with open(os.path.join(STATUS, name)) as f:
            return json.load(f)
    except Exception:
        return default


def _t(s):
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        if "." in s:                                  # restic: nanoseconds
            head, rest = s.split(".", 1)
            s = head + rest[-6:]                      # keep "+01:00"
        elif len(s) > 5 and s[-5] in "+-" and ":" not in s[-5:]:
            s = s[:-2] + ":" + s[-2:]                 # +0100 → +01:00
        return _dt.datetime.fromisoformat(s).astimezone(UK)
    except Exception:
        return None


def next_nightly():
    now = _dt.datetime.now(SERVER_TZ)
    n = now.replace(hour=NIGHTLY_SERVER_HOUR, minute=0, second=0, microsecond=0)
    if n <= now:
        n += _dt.timedelta(days=1)
    return n.astimezone(UK)


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def view():
    running = _load("running.json")
    lb = _load("last_backup.json") or {}
    lr = _load("last_restore.json") or {}
    repo = _load("repo.json") or {}
    snaps = []
    for s in reversed(_load("snapshots.json", []) or []):
        sm = s.get("summary") or {}
        start, end = _t(sm.get("backup_start")), _t(sm.get("backup_end"))
        snaps.append({"id": s.get("id"), "time": _t(s.get("time")),
                      "kind": "Manual" if "manual" in (s.get("tags") or []) else "Nightly",
                      "added": human_bytes(sm.get("data_added_packed")) if sm else "—",
                      "total": human_bytes(sm.get("total_bytes_processed")) if sm else "—",
                      "files": sm.get("total_files_processed"),
                      "mins": round((end - start).total_seconds() / 60, 1) if start and end else None})
    tests = []
    try:
        with open(os.path.join(STATUS, "restore_tests.jsonl")) as f:
            for line in f:
                try:
                    tests.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    for t in tests:
        t["at_uk"] = _t(t.get("at"))
        sid, _, stime = (t.get("snapshot") or "").partition(" ")
        st = None
        try:
            st = _dt.datetime.fromisoformat(stime).replace(tzinfo=SERVER_TZ).astimezone(UK)
        except Exception:
            pass
        t["snap_label"] = (st.strftime("%a %-d %b %H:%M") if st else sid) if sid != "none" else "—"
    tests.reverse()
    last = snaps[0] if snaps else None
    age_h = None
    if last and last["time"]:
        age_h = (_dt.datetime.now(UK) - last["time"]).total_seconds() / 3600
    started, finished = _t(lb.get("started")), _t(lb.get("finished"))
    return {
        "running": running and {"kind": running.get("kind"), "since": _t(running.get("started"))},
        "requested": {k: os.path.exists(os.path.join(REQ, k)) for k in ("backup", "restore")},
        "last_ok": lb.get("ok"), "last_msg": lb.get("message") or "",
        "last_finished": finished,
        "last_mins": round((finished - started).total_seconds() / 60, 1) if started and finished else None,
        "stale": age_h is not None and age_h > 26,
        "last": last, "snaps": snaps, "tests": tests[:24],
        "restore_last": {"ok": lr.get("ok"), "finished": _t(lr.get("finished")), "msg": lr.get("message") or ""},
        "stored": human_bytes(repo.get("stored_bytes")) if repo.get("stored_bytes") else None,
        "next": next_nightly(), "keep": KEEP,
        "key_saved": _load("key_saved.json"),
        "offsite": None,                               # step 2 (off-site copy) not set up yet
    }


def request_job(kind):
    if kind != "backup":                       # 18 Nov: the old "restore" request file was never read (fix 16)
        return False
    os.makedirs(REQ, exist_ok=True)
    with open(os.path.join(REQ, kind), "w") as f:
        f.write(_dt.datetime.now(UK).isoformat())
    return True


def mark_key_saved(by):
    tmp = os.path.join(STATUS, "key_saved.json.tmp")
    with open(tmp, "w") as f:
        json.dump({"by": by, "at": _dt.datetime.now(UK).strftime("%-d %b %Y %H:%M")}, f)
    os.replace(tmp, os.path.join(STATUS, "key_saved.json"))


# ── Disk space (8 Nov 2026) ─────────────────────────────────────────────
DISK_LIMIT = 85
DISKS = [("/", "Main drive", "System, websites, MariaDB, logs"),
         ("/vault", "Big drive", "Backups, product photos"),
         ("/srv/pgcrypt", "PhiXtra database area", "Encrypted PostgreSQL")]


def disk_view():
    import shutil
    out = []
    for path, name, holds in DISKS:
        try:
            u = shutil.disk_usage(path)
        except Exception:
            continue
        pct = round(u.used * 100 / u.total)
        out.append({"path": path, "name": name, "holds": holds, "pct": pct,
                    "used": human_bytes(u.used), "free": human_bytes(u.free),
                    "total": human_bytes(u.total), "warn": pct >= DISK_LIMIT})
    return out


# ══════════════════════════════════════════════════════════════════════
# Backups & Restore (14 Nov 2026, approved mock-up XwMNVPaYNf9MADHaZty3wc)
# Jobs run in /root/phixtra-backup/jobs.py (service phixtra-backup-jobs);
# this page only writes request files and reads jobs/<id>.json.
# ══════════════════════════════════════════════════════════════════════
import glob as _glob, re as _re, secrets as _secrets, subprocess as _sp

JOBS = os.path.join(BASE, "jobs")
MANIFESTS = os.path.join(STATUS, "manifests")
ENV_FILE = "/root/.config/phixtra-backup/env"
ACTIVE_STATES = ("queued", "running", "stopping", "awaiting_confirm", "switching", "undoing")
KIND_LABEL = {"test": "Test", "db": "Restore", "site": "Restore", "mail": "Restore", "files": "Download"}
STATE_LABEL = {"queued": "Starting", "running": "Running", "stopping": "Stopping…", "awaiting_confirm": "Waiting for your confirmation",
               "switching": "Switching", "undoing": "Undoing", "done": "Done", "failed": "Failed",
               "stopped": "Stopped", "cancelled": "Cancelled", "undone": "Undone"}


def _restic_env():
    env = os.environ.copy()
    try:
        for line in open(ENV_FILE):
            m = _re.match(r"\s*(?:export\s+)?([A-Z_]+)=(.*)", line)
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    except Exception:
        pass
    return env


def snap_ids():
    return [s.get("id") for s in (_load("snapshots.json", []) or [])]


def snap_label(sid):
    for s in _load("snapshots.json", []) or []:
        if s.get("id") == sid:
            t = _t(s.get("time"))
            return t.strftime("%a %-d %b, %H:%M") if t else sid
    return sid


def _job_view(j):
    if not j:
        return None
    j = dict(j)
    j["state_label"] = STATE_LABEL.get(j.get("state"), j.get("state"))
    j["snap_label"] = snap_label(j.get("snap"))
    for k in ("created", "finished", "undo_until", "confirmed_at"):
        t = _t(j.get(k))
        j[k + "_uk"] = t.strftime("%a %-d %b, %H:%M") if t else None
    started = _t(j.get("created"))
    if started:
        end = _t(j.get("finished")) or _dt.datetime.now(UK)
        j["elapsed"] = int((end - started).total_seconds())
    if j.get("download") and os.path.exists(j["download"].get("file") or ""):
        j["download_ready"] = True
    for k in ("work", "pid", "pgid", "tmpdir"):
        j.pop(k, None)
    return j


def _target_key(j):
    return (j.get("kind"), None if j.get("kind") == "db" else j.get("target"))


def undoable(rows=None):
    """Fix 5 (18 Nov): restores that can still be undone — the newest finished
    restore of each database/site/mailbox, while its 7 days last. Same rule
    as jobs.py undo_open()."""
    if rows is None:
        rows = _all_jobs()
    seen, out = set(), []
    now = _dt.datetime.now(UK)
    for j in rows:                                   # newest first
        if j.get("kind") not in ("db", "site", "mail") or not j.get("undo") or j.get("state") not in ("done", "undone"):
            continue
        k = _target_key(j)
        if k in seen:
            continue
        seen.add(k)
        t = _t(j.get("undo_until"))
        if j.get("state") == "done" and t and t > now and not j.get("needs_attention"):
            out.append(_job_view(j))
    return out


def can_undo(jid):
    return any(u["id"] == jid for u in undoable())


def _all_jobs():
    rows = []
    for p in sorted(_glob.glob(os.path.join(JOBS, "*.json")), reverse=True):
        try:
            with open(p) as f:
                rows.append(json.load(f))
        except Exception:
            pass
    return rows


def jobs_state():
    """What the page polls: the active (or just finished) job + recent history."""
    allrows = _all_jobs()
    rows = allrows[:40]
    active = next((j for j in rows if j.get("state") in ACTIVE_STATES), None)
    if not active and rows:
        last = rows[0]
        fin = _t(last.get("finished"))
        if fin and (_dt.datetime.now(UK) - fin).total_seconds() < 6 * 3600:
            active = last
    pending = bool(_glob.glob(os.path.join(REQ, "*.json")))
    return {"active": _job_view(active), "busy": bool(active and active.get("state") in ACTIVE_STATES) or pending,
            "pending": pending, "history": [_job_view(j) for j in rows[:25]], "undoable": undoable(allrows)}


def get_job(jid):
    if not _re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-z]+", jid or ""):
        return None
    try:
        with open(os.path.join(JOBS, jid + ".json")) as f:
            return json.load(f)
    except Exception:
        return None


def send_request(obj):
    os.makedirs(REQ, exist_ok=True)
    p = os.path.join(REQ, f"{int(_dt.datetime.now().timestamp() * 1000)}-{_secrets.token_hex(3)}.json")
    with open(p + ".tmp", "w") as f:
        json.dump(obj, f)
    os.replace(p + ".tmp", p)


def sites():
    out = []
    for p in sorted(_glob.glob("/home/*/public_html/wp-config.php")):
        out.append(p.split("/")[2])
    return out


def mailboxes():
    return sorted("/".join(p.split("/")[3:5]) for p in _glob.glob("/home/vmail/*/*/Maildir"))


def inside(sid):
    """What a backup holds: the record written at backup time, else the
    database files read from the backup itself."""
    if sid not in snap_ids():
        return None
    m = None
    try:
        with open(os.path.join(MANIFESTS, sid + ".json")) as f:
            m = json.load(f)
    except Exception:
        pass
    if m:
        m["recorded"] = True
        for k in ("databases", "sites", "mailboxes", "other"):
            for x in m.get(k, []):
                x["size"] = human_bytes(x.get("bytes")) if x.get("bytes") is not None else "—"
        return m
    out = {"recorded": False, "portal": {}, "databases": [], "sites": [{"site": s, "size": "—"} for s in sites()],
           "mailboxes": [{"box": b, "size": "—"} for b in mailboxes()], "other": []}
    try:
        r = _sp.run(["restic", "ls", "--json", sid, "/root/phixtra-backup/dumps"], capture_output=True, text=True,
                    timeout=120, env=_restic_env())
        for line in r.stdout.splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            n = d.get("name") or ""
            if d.get("type") == "file" and (n.startswith("pg_") or n.startswith("mysql_")) and "globals" not in n:
                out["databases"].append({"name": _re.sub(r"^(pg_|mysql_)|\.(dump|sql\.gz)$", "", n),
                                         "kind": "PostgreSQL" if n.startswith("pg_") else "MariaDB",
                                         "size": human_bytes(d.get("size"))})
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


BROWSE_ROOTS = ["/home", "/root/phixtra-app", "/vault/phixtra-media", "/etc", "/var/spool/cron", "/usr/local/bin"]


def browse(sid, path):
    """One folder of a backup (not recursive) for picking files to download."""
    if sid not in snap_ids():
        return {"error": "Unknown backup."}
    path = "/" + "/".join(p for p in (path or "").split("/") if p and p not in (".", ".."))
    if path == "/":
        return {"path": "/", "items": [{"name": r, "path": r, "type": "dir"} for r in BROWSE_ROOTS]}
    if not any(path == r or path.startswith(r + "/") for r in BROWSE_ROOTS):
        return {"error": "Only websites, mailboxes, the PhiXtra app, media and settings can be downloaded here."}
    r = _sp.run(["restic", "ls", "--json", sid, path], capture_output=True, text=True, timeout=180, env=_restic_env())
    items = []
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        p = d.get("path") or ""
        if d.get("struct_type") != "node" or p == path or os.path.dirname(p) != path:
            continue
        items.append({"name": d.get("name"), "path": p, "type": "dir" if d.get("type") == "dir" else "file",
                      "size": human_bytes(d.get("size")) if d.get("type") == "file" else ""})
    items.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return {"path": path, "items": items[:800], "more": len(items) > 800}


def download_path(jid):
    j = get_job(jid)
    d = (j or {}).get("download") or {}
    f = d.get("file") or ""
    if not f.startswith("/vault/restore-downloads/") or not os.path.exists(f):
        return None
    t = _t(d.get("until"))
    if t and _dt.datetime.now(UK) > t:
        return None
    return f
