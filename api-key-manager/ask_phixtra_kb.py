"""
ask_phixtra_kb.py: Knowledge for Ask PhiXtra (24 Nov 2026).

Ask PhiXtra answers from the live portal map. For things outside the portal
(Meta's rules, a banned WhatsApp account, quality ratings) the PhiXtra owner
writes Knowledge entries in Admin › Ask PhiXtra › Knowledge. Entries live in the
database (table ask_phixtra_knowledge); nothing is written in the code.

For each question, a small AI step picks the entries whose topic fits the
question by meaning (any wording, spelling or language). Most questions use none.
Approved mock-up: https://claude.ai/artifact/RKVGwdzSfLsGZuZbU6fNkk
"""
import re

import psycopg2.extras
from flask import flash, redirect, render_template, request, url_for

from db import get_db_connection, insert_audit_log

MAX_PICK = 3          # entries added to one answer at most
MAX_TITLE = 200
MAX_FACTS = 6000

_READY = False


def _db():
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def ensure_table():
    global _READY
    if _READY:
        return
    conn, cur = _db()
    try:
        cur.execute("SET lock_timeout = '3s'")
        cur.execute("""CREATE TABLE IF NOT EXISTS ask_phixtra_knowledge (
            id BIGSERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            facts TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            from_question TEXT,
            created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_used_at TIMESTAMPTZ)""")
        conn.commit()
        _READY = True
    finally:
        cur.close(); conn.close()


def active_entries() -> list:
    try:
        ensure_table()
        conn, cur = _db()
        try:
            cur.execute("SELECT id, title, facts FROM ask_phixtra_knowledge WHERE active ORDER BY id")
            return cur.fetchall()
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print("⚠️ Ask PhiXtra knowledge load:", e)
        return []


def pick(question: str, llm) -> list:
    """The entries that fit this question, by meaning: [(title, facts)]. llm = ask_phixtra._llm."""
    entries = active_entries()
    if not entries:
        return []
    numbered = "\n".join(f"{i + 1}. {e['title']}" for i, e in enumerate(entries))
    try:
        out = llm("You choose which knowledge entries help answer a question from a business using the PhiXtra "
                  "portal. Match by meaning, not exact words; spelling mistakes, slang and other languages are fine. "
                  "Pick none if the question is only about using the portal itself (menus, pages, settings). "
                  f"Pick at most {MAX_PICK}.\nENTRIES:\n{numbered}\nQUESTION: {question}\n"
                  "Return JSON {\"entries\": [numbers]}")
        nums = [int(n) for n in (out.get("entries") or []) if str(n).isdigit()]
    except Exception as e:
        print("⚠️ Ask PhiXtra knowledge pick:", e)
        return []
    chosen = [entries[n - 1] for n in dict.fromkeys(nums) if 1 <= n <= len(entries)][:MAX_PICK]
    if chosen:
        try:
            conn, cur = _db()
            cur.execute("UPDATE ask_phixtra_knowledge SET last_used_at = NOW() WHERE id = ANY(%s)",
                        ([e["id"] for e in chosen],))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print("⚠️ Ask PhiXtra knowledge used:", e)
    return [(e["title"], e["facts"]) for e in chosen]


# ── Admin › Ask PhiXtra › Knowledge (owner only) ──────────────────────────

def register(bp):
    def _owner():
        from portal_admin_routes import _require_owner
        return _require_owner()

    def _me():
        from portal_admin_routes import _admin_user
        return _admin_user()

    @bp.route("/admin/ask-phixtra/knowledge")
    def admin_knowledge():
        r = _owner()
        if r:
            return r
        ensure_table()
        conn, cur = _db()
        cur.execute("SELECT * FROM ask_phixtra_knowledge ORDER BY active DESC, id")
        rows = cur.fetchall()
        cur.close(); conn.close()
        from ask_phixtra import portal_map, ensure_tables, open_count
        from portal_admin_routes import _disk_warning_ctx
        ensure_tables()
        return render_template("portal/admin_ask_phixtra.html", show="knowledge", rows=[], kb=rows,
                               n_open=open_count(), map_size=len(portal_map()), **_disk_warning_ctx())

    @bp.route("/admin/ask-phixtra/knowledge/new")
    @bp.route("/admin/ask-phixtra/knowledge/<int:kid>")
    def admin_knowledge_edit(kid=None):
        r = _owner()
        if r:
            return r
        ensure_table()
        entry = None
        if kid:
            conn, cur = _db()
            cur.execute("SELECT * FROM ask_phixtra_knowledge WHERE id = %s", (kid,))
            entry = cur.fetchone()
            cur.close(); conn.close()
            if not entry:
                flash("That entry no longer exists.", "warning")
                return redirect(url_for("askphx.admin_knowledge"))
        from portal_admin_routes import _disk_warning_ctx
        return render_template("portal/admin_ask_phixtra_entry.html", entry=entry,
                               from_q=(request.args.get("q") or "")[:500],
                               from_ids=re.sub(r"[^\d,]", "", request.args.get("ids") or "")[:2000],
                               **_disk_warning_ctx())

    @bp.route("/admin/ask-phixtra/knowledge/save", methods=["POST"])
    def admin_knowledge_save():
        r = _owner()
        if r:
            return r
        ensure_table()
        f = request.form
        kid = int(f.get("id") or 0)
        title = (f.get("title") or "").strip()[:MAX_TITLE]
        facts = (f.get("facts") or "").strip()[:MAX_FACTS]
        active = f.get("active") == "1"
        if not title or not facts:
            flash("Please fill in both the title and the facts.", "warning")
            return redirect(url_for("askphx.admin_knowledge_edit", kid=kid) if kid
                            else url_for("askphx.admin_knowledge_edit"))
        me = _me()
        conn, cur = _db()
        if kid:
            cur.execute("""UPDATE ask_phixtra_knowledge SET title=%s, facts=%s, active=%s,
                           updated_by=%s, updated_at=NOW() WHERE id=%s RETURNING id""",
                        (title, facts, active, me, kid))
        else:
            cur.execute("""INSERT INTO ask_phixtra_knowledge (title, facts, active, from_question, created_by, updated_by)
                           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (title, facts, active, (f.get("from_q") or "")[:500] or None, me, me))
        row = cur.fetchone()
        # the question it started from leaves the Unanswered tab
        ids = [int(x) for x in re.findall(r"\d+", f.get("from_ids") or "")][:500]
        if ids and not kid:
            cur.execute("UPDATE ask_phixtra_questions SET dismissed = TRUE WHERE id = ANY(%s)", (ids,))
        conn.commit(); cur.close(); conn.close()
        try:
            insert_audit_log(admin_username=me, action="ask_phixtra_knowledge_" + ("edit" if kid else "add"),
                             details={"id": row["id"] if row else kid, "title": title, "on": active})
        except Exception as e:
            print("⚠️ knowledge audit:", e)
        flash("Saved. Ask PhiXtra uses it from the next question." if active
              else "Saved. It is Off, so Ask PhiXtra won't use it.", "success")
        return redirect(url_for("askphx.admin_knowledge"))

    @bp.route("/admin/ask-phixtra/knowledge/<int:kid>/delete", methods=["POST"])
    def admin_knowledge_delete(kid):
        r = _owner()
        if r:
            return r
        ensure_table()
        conn, cur = _db()
        cur.execute("DELETE FROM ask_phixtra_knowledge WHERE id = %s RETURNING title", (kid,))
        row = cur.fetchone()
        conn.commit(); cur.close(); conn.close()
        if row:
            try:
                insert_audit_log(admin_username=_me(), action="ask_phixtra_knowledge_delete",
                                 details={"id": kid, "title": row["title"]})
            except Exception as e:
                print("⚠️ knowledge audit:", e)
            flash("Entry deleted.", "success")
        return redirect(url_for("askphx.admin_knowledge"))
