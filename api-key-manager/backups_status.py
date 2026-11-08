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
    if kind not in ("backup", "restore"):
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
