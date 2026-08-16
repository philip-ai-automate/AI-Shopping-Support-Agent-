"""
ProfitBuyz R&D Activity Log — a real, small tool instead of a hand-edited page.

Add an entry whenever real R&D work happens (a build, a decision, a test, a
business development). Stored in SQLite so it can be exported later when the
June 2028 settlement evidence package gets compiled.

Password-protected (HTTP Basic) because this records business detail that
feeds an immigration case — not something to leave on a guessable public URL.
"""
import os
import secrets
import sqlite3
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

load_dotenv()

PREFIX = "/rd-log"
DB_PATH = Path(__file__).parent / "rdlog.db"
AUTH_USER = os.environ.get("RDLOG_USER", "admin")
AUTH_PASSWORD = os.environ.get("RDLOG_PASSWORD", "")

app = FastAPI(title="ProfitBuyz R&D Activity Log")
security = HTTPBasic()

WORKSTREAMS = [
    ("4.1", "Price Intelligence", "#A2670F", "#FBF0DC"),
    ("4.2", "B2B Suite", "#2B6CB0", "#E4EEFB"),
    ("4.3", "Stock List Tool", "#7A3305", "#FDECDD"),
    ("4.4", "AI Sales Agent", "#1F8A5F", "#E4F4EC"),
    ("4.5", "Scalability Infra", "#7A5AF8", "#EDE9FE"),
    ("4.6", "Storefront R&D", "#5B6478", "#EDEFF3"),
    ("PRG", "Programme", "#1F8A5F", "#E4F4EC"),
]
WS_LOOKUP = {code: (label, ink, wash) for code, label, ink, wash in WORKSTREAMS}


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date TEXT NOT NULL,
            workstream TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            evidence TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    return conn


def _check_auth(creds: HTTPBasicCredentials = Depends(security)):
    if not AUTH_PASSWORD:
        raise HTTPException(500, "RDLOG_PASSWORD not configured on the server yet.")
    ok_user = secrets.compare_digest(creds.username, AUTH_USER)
    ok_pass = secrets.compare_digest(creds.password, AUTH_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
            detail="Wrong username or password.",
        )
    return creds.username


PAGE_CSS = """
:root {
  --bg:#F5F6F8; --surface:#FFFFFF; --ink:#141B2E; --ink-muted:#4B5468; --ink-faint:#7B8296;
  --accent:#F97316; --accent-ink:#7A3305; --accent-wash:#FDECDD;
  --line:#DDE1E8; --ok:#1F8A5F; --ok-wash:#E4F4EC;
}
* { box-sizing:border-box; }
body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--ink); }
header { background:var(--surface); border-bottom:1px solid var(--line); padding:18px 28px; display:flex; align-items:center; gap:12px; }
header .dot { width:10px; height:10px; border-radius:50%; background:var(--accent); }
header h1 { font-size:17px; margin:0; }
main { max-width:880px; margin:0 auto; padding:28px; display:flex; flex-direction:column; gap:24px; }
.card { background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:20px 24px; }
.card h2 { font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--ink-muted); margin:0 0 14px; }
label { display:block; font-size:13px; color:var(--ink-muted); margin-bottom:4px; margin-top:12px; }
label:first-child { margin-top:0; }
input[type=text], input[type=date], select, textarea {
  width:100%; border:1px solid var(--line); border-radius:5px; padding:8px 10px; font-size:14px; font-family:inherit;
}
textarea { resize:vertical; min-height:70px; }
button { border:none; border-radius:6px; padding:10px 18px; font-size:13.5px; font-weight:600; cursor:pointer; background:var(--accent); color:white; margin-top:16px; }
button.secondary { background:var(--surface); color:#B33; border:1px solid var(--line); padding:4px 10px; font-size:12px; margin:0; }
.entry { display:flex; gap:14px; padding:16px 0; border-bottom:1px solid var(--line); }
.entry:last-child { border-bottom:none; }
.entry .date { font-family:ui-monospace,monospace; font-size:12.5px; color:var(--ink-faint); min-width:64px; padding-top:2px; }
.entry-body { flex:1; display:flex; flex-direction:column; gap:5px; }
.entry-head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.tag { font-family:ui-monospace,monospace; font-size:11px; font-weight:600; padding:2px 9px; border-radius:100px; }
.entry h3 { margin:0; font-size:15px; }
.entry p { margin:0; font-size:14px; color:var(--ink-muted); }
.entry .evidence { font-family:ui-monospace,monospace; font-size:11.5px; color:var(--ink-faint); }
.empty { color:var(--ink-muted); font-size:14px; padding:20px 0; }
a.export { font-size:13px; color:var(--accent-ink); }
"""


def _entry_form_html(err: str = "") -> str:
    options = "".join(f'<option value="{code}">{code} — {label}</option>' for code, label, _, _ in WORKSTREAMS)
    err_html = f'<p style="color:#B33;font-size:13px;margin:0 0 10px;">{err}</p>' if err else ""
    today = date.today().isoformat()
    return f"""
    <div class="card">
      <h2>Add an entry</h2>
      {err_html}
      <form method="post" action="{PREFIX}/add">
        <label>Date</label>
        <input type="date" name="entry_date" value="{today}" required />
        <label>Workstream</label>
        <select name="workstream" required>{options}</select>
        <label>What happened (short title)</label>
        <input type="text" name="title" placeholder="e.g. Fixed the price-update cron bug" required />
        <label>Detail — plain language, a sentence or two</label>
        <textarea name="description" placeholder="Why it matters, what problem it solved" required></textarea>
        <label>Evidence (optional) — a commit message, a live URL, a test result</label>
        <input type="text" name="evidence" placeholder="e.g. git commit abc123, or verified live 08 Aug 2026" />
        <button type="submit">Add entry</button>
      </form>
    </div>
    """


def _entries_html(rows) -> str:
    if not rows:
        return '<div class="card"><p class="empty">No entries yet — add the first one above.</p></div>'
    items = []
    for r in rows:
        label, ink, wash = WS_LOOKUP.get(r["workstream"], (r["workstream"], "#5B6478", "#EDEFF3"))
        evidence_html = f'<div class="evidence">Evidence: {r["evidence"]}</div>' if r["evidence"] else ""
        items.append(f"""
        <div class="entry">
          <div class="date">{r["entry_date"]}</div>
          <div class="entry-body">
            <div class="entry-head">
              <span class="tag" style="background:{wash};color:{ink};">{r["workstream"]} {label}</span>
              <form method="post" action="{PREFIX}/delete/{r['id']}" onsubmit="return confirm('Delete this entry?');" style="margin-left:auto;">
                <button type="submit" class="secondary">delete</button>
              </form>
            </div>
            <h3>{r["title"]}</h3>
            <p>{r["description"]}</p>
            {evidence_html}
          </div>
        </div>
        """)
    return f'<div class="card"><h2>{len(rows)} entries — newest first</h2>{"".join(items)}</div>'


@app.get(PREFIX + "/", response_class=HTMLResponse)
async def index(user: str = Depends(_check_auth)):
    conn = _db()
    rows = conn.execute("SELECT * FROM entries ORDER BY entry_date DESC, id DESC").fetchall()
    conn.close()
    return f"""<!doctype html><html><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>ProfitBuyz — R&amp;D Activity Log</title><style>{PAGE_CSS}</style></head>
<body>
<header><span class="dot"></span><h1>ProfitBuyz — R&amp;D Activity Log</h1>
<a class="export" style="margin-left:auto;" href="{PREFIX}/export.csv">Export CSV</a></header>
<main>
{_entry_form_html()}
{_entries_html(rows)}
</main></body></html>"""


@app.post(PREFIX + "/add")
async def add_entry(
    entry_date: str = Form(...), workstream: str = Form(...),
    title: str = Form(...), description: str = Form(...), evidence: str = Form(""),
    user: str = Depends(_check_auth),
):
    conn = _db()
    conn.execute(
        "INSERT INTO entries (entry_date, workstream, title, description, evidence) VALUES (?, ?, ?, ?, ?)",
        (entry_date, workstream, title.strip(), description.strip(), evidence.strip()),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(PREFIX + "/", status_code=303)


@app.post(PREFIX + "/delete/{entry_id}")
async def delete_entry(entry_id: int, user: str = Depends(_check_auth)):
    conn = _db()
    conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(PREFIX + "/", status_code=303)


@app.get(PREFIX + "/export.csv")
async def export_csv(user: str = Depends(_check_auth)):
    import csv
    import io
    conn = _db()
    rows = conn.execute("SELECT * FROM entries ORDER BY entry_date ASC, id ASC").fetchall()
    conn.close()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "workstream", "title", "description", "evidence"])
    for r in rows:
        writer.writerow([r["entry_date"], r["workstream"], r["title"], r["description"], r["evidence"]])
    from fastapi.responses import Response
    return Response(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=rd_activity_log.csv"},
    )


@app.get(PREFIX + "/api/health")
async def health():
    return {"ok": True, "auth_configured": bool(AUTH_PASSWORD)}
