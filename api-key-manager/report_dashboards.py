"""Dashboards (Reports & Dashboards, phase R3 — 19 Nov 2026).

A dashboard is saved reports laid out as boxes (report_defs rows), plus text
notes. The dashboard's own filters (period, team member, compare) apply to
every box that uses them. Approved mock-up: EPpwZTeFt5iccD8SH155P1, screens 1,
4, 5 and 6. Sharing and "only my numbers" rules come in R4; until then every
dashboard is open to everyone in the business who may use Reports, and each
box still shows only the records that person may see (report_builder rules).
"""
import json

import psycopg2.extras

from db import get_db_connection
import report_builder as RB

SIZES = [("s", "Small"), ("m", "Medium"), ("l", "Large"), ("w", "Full width")]
SIZE_KEYS = {k for k, _ in SIZES}
MAX_BOXES = 40
DASH_FILTERS = [("period", "Time"), ("member", "Team member")]

_tables_ok = False


def ensure_tables():
    global _tables_ok
    if _tables_ok:
        return
    conn = get_db_connection()
    RB.ensure_tables(conn)
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('report_dashboards') IS NOT NULL")
        if not cur.fetchone()[0]:
            cur.execute("SET lock_timeout = '3s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS report_dashboards (
                               id             BIGSERIAL PRIMARY KEY,
                               tenant_id      INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                               name           VARCHAR(80) NOT NULL,
                               filters        JSONB NOT NULL DEFAULT '["period","member","cmp"]'::jsonb,
                               position       INTEGER NOT NULL DEFAULT 0,
                               created_by_key VARCHAR(64),
                               created_by     VARCHAR(200),
                               created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                               updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS report_dashboard_boxes (
                               id             BIGSERIAL PRIMARY KEY,
                               dashboard_id   BIGINT NOT NULL REFERENCES report_dashboards(id) ON DELETE CASCADE,
                               report_id      BIGINT REFERENCES report_defs(id) ON DELETE CASCADE,
                               note           TEXT,
                               size           VARCHAR(2) NOT NULL DEFAULT 'm',
                               position       INTEGER NOT NULL DEFAULT 0,
                               use_filters    BOOLEAN NOT NULL DEFAULT TRUE,
                               created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_report_dash_tenant ON report_dashboards (tenant_id, position)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_report_dash_boxes ON report_dashboard_boxes (dashboard_id, position)")
            conn.commit()
        _tables_ok = True
    except Exception as e:
        conn.rollback()
        print("⚠️ report_dashboards tables:", e)
    finally:
        cur.close()
        conn.close()


_widget_cols_ok = False


def ensure_widget_columns():
    """Widgets on a grid (21 Nov 2026): kind, its settings and its place. Lock limited; small new table."""
    global _widget_cols_ok
    if _widget_cols_ok:
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                        WHERE table_name='report_dashboard_boxes' AND column_name IN ('kind','wcfg','gx','gy','gw','gh')""")
        if cur.fetchone()[0] < 6:
            cur.execute("SET lock_timeout = '3s'")
            cur.execute("ALTER TABLE report_dashboard_boxes ADD COLUMN IF NOT EXISTS kind VARCHAR(12)")
            cur.execute("ALTER TABLE report_dashboard_boxes ADD COLUMN IF NOT EXISTS wcfg JSONB NOT NULL DEFAULT '{}'::jsonb")
            for c in ("gx", "gy", "gw", "gh"):
                cur.execute(f"ALTER TABLE report_dashboard_boxes ADD COLUMN IF NOT EXISTS {c} SMALLINT")
            conn.commit()
        _widget_cols_ok = True
    except Exception as e:
        conn.rollback()
        print("⚠️ report widget columns:", e)
    finally:
        cur.close()
        conn.close()


def _db():
    ensure_tables()
    ensure_widget_columns()
    conn = get_db_connection()
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def list_dashboards(tenant_id: int) -> list:
    conn, cur = _db()
    try:
        cur.execute("""SELECT d.*,
                              (SELECT COUNT(*) FROM report_dashboard_boxes b WHERE b.dashboard_id = d.id) AS boxes
                         FROM report_dashboards d WHERE d.tenant_id=%s ORDER BY d.position, d.id""", (tenant_id,))
        return cur.fetchall()
    finally:
        conn.close()


def get_dashboard(tenant_id: int, dash_id: int):
    conn, cur = _db()
    try:
        cur.execute("SELECT * FROM report_dashboards WHERE id=%s AND tenant_id=%s", (dash_id, tenant_id))
        d = cur.fetchone()
        if not d:
            return None
        cur.execute("""SELECT b.*, r.name AS report_name, r.subject, r.config
                         FROM report_dashboard_boxes b LEFT JOIN report_defs r ON r.id = b.report_id
                        WHERE b.dashboard_id=%s ORDER BY b.position, b.id""", (dash_id,))
        d["boxes"] = cur.fetchall()
        return d
    finally:
        conn.close()


def create_dashboard(tenant_id: int, name: str, actor: dict) -> int:
    conn, cur = _db()
    try:
        cur.execute("""INSERT INTO report_dashboards (tenant_id, name, position, created_by_key, created_by)
                       VALUES (%s,%s,(SELECT COALESCE(MAX(position),0)+1 FROM report_dashboards WHERE tenant_id=%s),%s,%s)
                       RETURNING id""", (tenant_id, name[:80], tenant_id, actor["key"], actor["label"]))
        did = cur.fetchone()["id"]
        conn.commit()
        return did
    finally:
        conn.close()


def rename_dashboard(tenant_id: int, dash_id: int, name: str) -> bool:
    conn, cur = _db()
    try:
        cur.execute("UPDATE report_dashboards SET name=%s, updated_at=NOW() WHERE id=%s AND tenant_id=%s",
                    (name[:80], dash_id, tenant_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_dashboard(tenant_id: int, dash_id: int) -> bool:
    conn, cur = _db()
    try:
        cur.execute("DELETE FROM report_dashboards WHERE id=%s AND tenant_id=%s", (dash_id, tenant_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def add_box(tenant_id: int, dash_id: int, report_id: int = None, note: str = None, size: str = None):
    """Adds a report (or a text note) at the end. None if not allowed."""
    conn, cur = _db()
    try:
        cur.execute("SELECT 1 FROM report_dashboards WHERE id=%s AND tenant_id=%s", (dash_id, tenant_id))
        if not cur.fetchone():
            return None
        if report_id:
            cur.execute("SELECT config FROM report_defs WHERE id=%s AND tenant_id=%s", (report_id, tenant_id))
            r = cur.fetchone()
            if not r:
                return None
            cfg = r["config"] or {}
            size = size or ("w" if cfg.get("chart") == "table" else
                            ("s" if cfg.get("chart") == "number" or not cfg.get("g1") else "m"))
        cur.execute("SELECT COUNT(*) AS n, COALESCE(MAX(position),0) AS p FROM report_dashboard_boxes WHERE dashboard_id=%s",
                    (dash_id,))
        row = cur.fetchone()
        if row["n"] >= MAX_BOXES:
            return None
        cur.execute("""INSERT INTO report_dashboard_boxes (dashboard_id, report_id, note, size, position)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (dash_id, report_id, (note or "")[:2000] if not report_id else None,
                     size if size in SIZE_KEYS else "m", row["p"] + 1))
        bid = cur.fetchone()["id"]
        cur.execute("UPDATE report_dashboards SET updated_at=NOW() WHERE id=%s", (dash_id,))
        conn.commit()
        return bid
    finally:
        conn.close()


def add_widget(tenant_id: int, dash_id: int, kind: str, place: tuple, pick: str = None, report_id: int = None,
               note: str = None):
    """Adds a widget at a place on the grid (x, y, w, h). None if not allowed or the dashboard is full."""
    conn, cur = _db()
    try:
        cur.execute("SELECT 1 FROM report_dashboards WHERE id=%s AND tenant_id=%s", (dash_id, tenant_id))
        if not cur.fetchone():
            return None
        if report_id:
            cur.execute("SELECT 1 FROM report_defs WHERE id=%s AND tenant_id=%s", (report_id, tenant_id))
            if not cur.fetchone():
                return None
        cur.execute("SELECT COUNT(*) AS n, COALESCE(MAX(position),0) AS p FROM report_dashboard_boxes WHERE dashboard_id=%s",
                    (dash_id,))
        row = cur.fetchone()
        if row["n"] >= MAX_BOXES:
            return None
        x, y, w, h = place
        cur.execute("""INSERT INTO report_dashboard_boxes (dashboard_id, report_id, note, size, position, kind, wcfg, gx, gy, gw, gh)
                       VALUES (%s,%s,%s,'m',%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (dash_id, report_id, (note or "")[:2000] if kind == "heading" else None, row["p"] + 1, kind,
                     json.dumps({"pick": pick} if pick else {}), x, y, w, h))
        bid = cur.fetchone()["id"]
        cur.execute("UPDATE report_dashboards SET updated_at=NOW() WHERE id=%s", (dash_id,))
        conn.commit()
        return bid
    finally:
        conn.close()


def update_widget(tenant_id: int, dash_id: int, box_id: int, pick: str = None, report_id: int = None,
                  note: str = None, use_filters: bool = True) -> bool:
    conn, cur = _db()
    try:
        cur.execute("""UPDATE report_dashboard_boxes b SET
                              wcfg = CASE WHEN %s IS NOT NULL THEN jsonb_build_object('pick', %s::text) ELSE b.wcfg END,
                              report_id = COALESCE(%s, b.report_id),
                              note = CASE WHEN %s IS NOT NULL THEN %s ELSE b.note END,
                              use_filters = %s
                         FROM report_dashboards d
                        WHERE b.id=%s AND b.dashboard_id=%s AND d.id=b.dashboard_id AND d.tenant_id=%s""",
                    (pick, pick, report_id, note, (note or "")[:2000], bool(use_filters), box_id, dash_id, tenant_id))
        ok = cur.rowcount > 0
        cur.execute("UPDATE report_dashboards SET updated_at=NOW() WHERE id=%s AND tenant_id=%s", (dash_id, tenant_id))
        conn.commit()
        return ok
    finally:
        conn.close()


def set_widget(tenant_id: int, dash_id: int, box_id: int, wcfg: dict, report_id: int = None, note: str = None) -> bool:
    """A widget's Settings (21 Nov 2026): its own settings, and what it shows."""
    conn, cur = _db()
    try:
        cur.execute("""UPDATE report_dashboard_boxes b SET wcfg=%s, use_filters=TRUE,
                              report_id = COALESCE(%s, b.report_id),
                              note = CASE WHEN %s IS NOT NULL THEN %s ELSE b.note END
                         FROM report_dashboards d
                        WHERE b.id=%s AND b.dashboard_id=%s AND d.id=b.dashboard_id AND d.tenant_id=%s""",
                    (json.dumps(wcfg), report_id, note, (note or "")[:2000], box_id, dash_id, tenant_id))
        ok = cur.rowcount > 0
        cur.execute("UPDATE report_dashboards SET updated_at=NOW() WHERE id=%s AND tenant_id=%s", (dash_id, tenant_id))
        conn.commit()
        return ok
    finally:
        conn.close()


def save_layout(tenant_id: int, dash_id: int, boxes: list, filters: list, name: str = None) -> bool:
    """boxes: [{id, size, use_filters, note?}] in the new order; boxes left out are removed."""
    conn, cur = _db()
    try:
        cur.execute("SELECT 1 FROM report_dashboards WHERE id=%s AND tenant_id=%s FOR UPDATE", (dash_id, tenant_id))
        if not cur.fetchone():
            return False
        cur.execute("SELECT id FROM report_dashboard_boxes WHERE dashboard_id=%s", (dash_id,))
        have = {r["id"] for r in cur.fetchall()}
        keep = []
        for pos, b in enumerate(boxes[:MAX_BOXES], 1):
            try:
                bid = int(b.get("id"))
            except (TypeError, ValueError):
                continue
            if bid not in have:
                continue
            keep.append(bid)
            size = b.get("size") if b.get("size") in SIZE_KEYS else "m"
            note = b.get("note")
            cur.execute("""UPDATE report_dashboard_boxes SET position=%s, size=%s, use_filters=%s,
                                  note=CASE WHEN report_id IS NULL AND %s IS NOT NULL THEN %s ELSE note END
                            WHERE id=%s AND dashboard_id=%s""",
                        (pos, size, bool(b.get("use_filters", True)), note, (note or "")[:2000], bid, dash_id))
            if all(k in b for k in ("x", "y", "w", "h")):      # widgets on the grid (21 Nov 2026)
                import report_widgets as RW
                pl = RW.clean_place(b["x"], b["y"], b["w"], b["h"], str(b.get("kind") or "report"))
                if pl:
                    cur.execute("UPDATE report_dashboard_boxes SET gx=%s, gy=%s, gw=%s, gh=%s WHERE id=%s AND dashboard_id=%s",
                                pl + (bid, dash_id))
        gone = list(have - set(keep))
        if gone:
            cur.execute("DELETE FROM report_dashboard_boxes WHERE dashboard_id=%s AND id = ANY(%s)", (dash_id, gone))
        flt = [f for f in (filters or []) if f in dict(DASH_FILTERS)]
        cur.execute("UPDATE report_dashboards SET filters=%s, updated_at=NOW()" + (", name=%s" if name else "") +
                    " WHERE id=%s", ([json.dumps(flt)] + ([name[:80]] if name else []) + [dash_id]))
        conn.commit()
        return True
    finally:
        conn.close()


def dashboards_using(tenant_id: int, report_id: int) -> list:
    conn, cur = _db()
    try:
        cur.execute("""SELECT DISTINCT d.name FROM report_dashboard_boxes b JOIN report_dashboards d ON d.id = b.dashboard_id
                        WHERE d.tenant_id=%s AND b.report_id=%s ORDER BY d.name""", (tenant_id, report_id))
        return [r["name"] for r in cur.fetchall()]
    finally:
        conn.close()
