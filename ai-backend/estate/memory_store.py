"""
estate/memory_store.py — Chat memory for PhiXtra Real Estate AI.

Uses re_chat_messages and re_chat_summaries (keyed by tenant_id + customer_id).
Also manages re_customers upsert and buyer qualification updates.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import psycopg2
import psycopg2.extras
from db import get_db_connection

try:
    from openai import OpenAI as _OAI
except Exception:
    _OAI = None


def _get_oai_client():
    if _OAI is None:
        return None
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    try:
        return _OAI(api_key=key)
    except Exception:
        return None


# ── Customer record ───────────────────────────────────────────────────────────

def ensure_customer(tenant_id: int, phone_number: str) -> int | None:
    """Get or create a re_customers row by phone. Returns customer_id or None."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO re_customers (tenant_id, phone_number)
            VALUES (%s, %s)
            ON CONFLICT (tenant_id, phone_number) DO UPDATE
                SET last_seen_at = NOW()
            RETURNING id
        """, (tenant_id, phone_number))
        conn.commit()
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else None
    except Exception as e:
        print(f"⚠️ [ESTATE MEM] ensure_customer error: {e}")
        return None
    finally:
        conn.close()


def update_qualification(tenant_id: int, customer_id: int, fields: dict):
    """Update buyer qualification columns in re_customers. Silently ignores unknown fields."""
    allowed = {
        "budget_min", "budget_max", "preferred_area", "property_type_pref",
        "transaction_pref", "payment_method", "urgency", "bedrooms_pref",
        "name", "lead_status",
    }
    safe = {k: v for k, v in (fields or {}).items() if k in allowed and v is not None and v != ""}
    if not safe:
        return

    cols = ", ".join(f"{k} = %s" for k in safe)
    vals = list(safe.values()) + [tenant_id, customer_id]

    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE re_customers SET {cols}, updated_at = NOW() WHERE tenant_id=%s AND id=%s",
            vals,
        )
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"⚠️ [ESTATE MEM] update_qualification error: {e}")
    finally:
        conn.close()


# ── Message storage ───────────────────────────────────────────────────────────

def add_message(tenant_id: int, customer_id: int, role: str,
                content: str, embedding: list = None):
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        if embedding:
            vec = "[" + ",".join(str(x) for x in embedding) + "]"
            cur.execute("""
                INSERT INTO re_chat_messages
                    (tenant_id, customer_id, role, content, embedding)
                VALUES (%s, %s, %s, %s, %s::vector)
            """, (tenant_id, customer_id, role, content, vec))
        else:
            cur.execute("""
                INSERT INTO re_chat_messages
                    (tenant_id, customer_id, role, content)
                VALUES (%s, %s, %s, %s)
            """, (tenant_id, customer_id, role, content))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"⚠️ [ESTATE MEM] add_message error: {e}")
    finally:
        conn.close()


# ── Summarization ─────────────────────────────────────────────────────────────

def _count_messages(cur, tenant_id: int, customer_id: int) -> int:
    cur.execute(
        "SELECT COUNT(*) AS c FROM re_chat_messages WHERE tenant_id=%s AND customer_id=%s",
        (tenant_id, customer_id),
    )
    row = cur.fetchone() or {}
    return int(row.get("c") or 0)


def _upsert_summary(cur, tenant_id: int, customer_id: int,
                    summary_text: str, message_count: int):
    cur.execute("""
        INSERT INTO re_chat_summaries
            (tenant_id, customer_id, summary_text, message_count)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (tenant_id, customer_id) DO UPDATE SET
            summary_text  = EXCLUDED.summary_text,
            message_count = EXCLUDED.message_count,
            updated_at    = NOW()
    """, (tenant_id, customer_id, summary_text, message_count))


def maybe_summarize(tenant_id: int, customer_id: int, keep_last_n: int = 4):
    """Summarise older history after ~3 turns to keep tokens low."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        total = _count_messages(cur, tenant_id, customer_id)
        if total < 6:
            cur.close()
            return

        cur.execute("""
            SELECT message_count FROM re_chat_summaries
            WHERE tenant_id=%s AND customer_id=%s
        """, (tenant_id, customer_id))
        existing = cur.fetchone()
        summarized = int((existing or {}).get("message_count") or 0)

        if total - summarized < 2 and summarized > 0:
            cur.close()
            return

        to_summarize = max(0, total - keep_last_n)
        if to_summarize < 6:
            cur.close()
            return

        cur.execute("""
            SELECT role, content FROM re_chat_messages
            WHERE tenant_id=%s AND customer_id=%s
            ORDER BY id ASC LIMIT %s
        """, (tenant_id, customer_id, to_summarize))
        rows = cur.fetchall() or []

        lines = []
        for r in rows:
            role    = (r.get("role") or "").strip()
            content = (r.get("content") or "").strip()
            if content:
                lines.append(f"{'Buyer' if role == 'user' else 'Agent'}: {content}")

        if not lines:
            cur.close()
            return

        client = _get_oai_client()
        if not client:
            cur.close()
            return

        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
            temperature=0.2,
            max_tokens=int(os.getenv("SUMMARY_MAX_TOKENS", "220")),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarise this real estate buyer conversation for future context. "
                        "Short structured output only:\n"
                        "- Buyer goal\n- Location interest\n- Budget range\n"
                        "- Property type\n- Timeline/urgency\n- Open questions"
                    ),
                },
                {"role": "user", "content": "\n".join(lines)},
            ],
        )

        text = (resp.choices[0].message.content or "").strip()
        if not text:
            cur.close()
            return

        _upsert_summary(cur, tenant_id, customer_id, text, total)
        conn.commit()

        prune = os.getenv("MEMORY_PRUNE_OLD_MESSAGES", "1").strip() not in ("0", "false", "False")
        if prune:
            cur.execute("""
                DELETE FROM re_chat_messages WHERE id IN (
                    SELECT id FROM re_chat_messages
                    WHERE tenant_id=%s AND customer_id=%s
                    ORDER BY id ASC LIMIT %s
                )
            """, (tenant_id, customer_id, to_summarize))
            conn.commit()

        cur.close()
    except Exception as e:
        print(f"⚠️ [ESTATE MEM] maybe_summarize error: {e}")
    finally:
        conn.close()


# ── History retrieval ─────────────────────────────────────────────────────────

def _get_summary_text(tenant_id: int, customer_id: int) -> str:
    conn = get_db_connection()
    if not conn:
        return ""
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT summary_text FROM re_chat_summaries
            WHERE tenant_id=%s AND customer_id=%s
        """, (tenant_id, customer_id))
        row = cur.fetchone()
        cur.close()
        return (row or {}).get("summary_text") or ""
    except Exception:
        return ""
    finally:
        conn.close()


def get_history_with_summary(tenant_id: int, customer_id: int,
                              keep_last_n: int = 4) -> list:
    """Returns OpenAI-format history: [summary system msg] + last N messages."""
    summary = _get_summary_text(tenant_id, customer_id)
    last_msgs = []

    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT role, content FROM re_chat_messages
                WHERE tenant_id=%s AND customer_id=%s
                ORDER BY id DESC LIMIT %s
            """, (tenant_id, customer_id, keep_last_n))
            rows = list(reversed(cur.fetchall() or []))
            cur.close()
            last_msgs = [{"role": r["role"], "content": r["content"]} for r in rows]
        except Exception as e:
            print(f"⚠️ [ESTATE MEM] get_history_with_summary error: {e}")
        finally:
            conn.close()

    out = []
    if summary:
        out.append({"role": "system",
                    "content": "Conversation summary (use as memory):\n" + summary})
    out.extend(last_msgs)
    return out


def get_semantic_history(tenant_id: int, customer_id: int,
                          query_embedding: list,
                          keep_last_n: int = 4,
                          semantic_top_k: int = 4) -> list:
    """
    Combines: [summary] + semantically relevant older pairs + last N messages.
    Falls back to get_history_with_summary on any error.
    """
    summary = _get_summary_text(tenant_id, customer_id)
    conn = get_db_connection()
    if not conn:
        return get_history_with_summary(tenant_id, customer_id, keep_last_n)

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
            SELECT id, role, content FROM re_chat_messages
            WHERE tenant_id=%s AND customer_id=%s
            ORDER BY id DESC LIMIT %s
        """, (tenant_id, customer_id, keep_last_n))
        last_rows = list(reversed(cur.fetchall() or []))
        last_ids  = {r["id"] for r in last_rows}

        semantic_rows = []
        if query_embedding and last_ids:
            vec = "[" + ",".join(str(x) for x in query_embedding) + "]"
            cur.execute("""
                SELECT id, role, content FROM re_chat_messages
                WHERE tenant_id=%s AND customer_id=%s
                  AND embedding IS NOT NULL AND role = 'user'
                  AND id != ALL(%s)
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """, (tenant_id, customer_id, list(last_ids), vec, semantic_top_k))
            for row in (cur.fetchall() or []):
                semantic_rows.append(dict(row))
                cur.execute("""
                    SELECT id, role, content FROM re_chat_messages
                    WHERE tenant_id=%s AND customer_id=%s
                      AND id > %s AND role = 'assistant'
                    ORDER BY id ASC LIMIT 1
                """, (tenant_id, customer_id, row["id"]))
                reply = cur.fetchone()
                if reply and reply["id"] not in last_ids:
                    semantic_rows.append(dict(reply))

        cur.close()
        conn.close()

        seen   = set()
        merged = []
        for row in semantic_rows + last_rows:
            if row["id"] not in seen:
                seen.add(row["id"])
                merged.append(row)
        merged.sort(key=lambda r: r["id"])

        out = []
        if summary:
            out.append({"role": "system",
                        "content": "Conversation summary:\n" + summary})
        out.extend({"role": r["role"], "content": r["content"]} for r in merged)
        return out

    except Exception as e:
        print(f"⚠️ [ESTATE MEM] get_semantic_history error: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return get_history_with_summary(tenant_id, customer_id, keep_last_n)
