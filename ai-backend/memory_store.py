import os
import psycopg2
import psycopg2.extras
from db import get_db_connection

try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore


def _get_summary_client():
    if OpenAI is None:
        return None
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        return OpenAI(api_key=api_key)
    except Exception:
        return None


def init_memory_tables():
    """No-op: tables are created by pg_schema.sql at deploy time."""
    pass


def _count_messages(cur, session_id: str, tenant_id: int) -> int:
    cur.execute(
        """
        SELECT COUNT(*) AS c
        FROM chat_messages
        WHERE session_id=%s AND tenant_id=%s
        """,
        (session_id, tenant_id),
    )
    row = cur.fetchone() or {}
    return int(row.get("c") or 0)


def get_summary(session_id: str, tenant_id: int):
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT summary_text, summarized_message_count
            FROM chat_summaries
            WHERE session_id=%s AND tenant_id=%s
            """,
            (session_id, tenant_id),
        )
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception as e:
        print("? [MEMORY] get_summary error:", str(e))
        return None
    finally:
        conn.close()


def _upsert_summary(cur, session_id: str, tenant_id: int, summary_text: str, summarized_message_count: int):
    cur.execute(
        """
        INSERT INTO chat_summaries (session_id, tenant_id, summary_text, summarized_message_count)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (session_id, tenant_id) DO UPDATE SET
            summary_text = EXCLUDED.summary_text,
            summarized_message_count = EXCLUDED.summarized_message_count,
            updated_at = NOW()
        """,
        (session_id, tenant_id, summary_text, summarized_message_count),
    )


def maybe_summarize_session(session_id: str, tenant_id: int, keep_last_n: int = 4):
    """Summarise history after ~3 turns to reduce token usage."""
    conn = get_db_connection()
    if not conn:
        return

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        total = _count_messages(cur, session_id, tenant_id)
        if total < 6:
            cur.close()
            return

        existing = None
        try:
            cur.execute(
                """
                SELECT summarized_message_count
                FROM chat_summaries
                WHERE session_id=%s AND tenant_id=%s
                """,
                (session_id, tenant_id),
            )
            existing = cur.fetchone()
        except Exception:
            existing = None

        summarized_count = int((existing or {}).get("summarized_message_count") or 0)

        if total - summarized_count < 2 and summarized_count > 0:
            cur.close()
            return

        to_summarise_count = max(0, total - keep_last_n)
        if to_summarise_count < 6:
            cur.close()
            return

        cur.execute(
            """
            SELECT role, content
            FROM chat_messages
            WHERE session_id=%s AND tenant_id=%s
            ORDER BY id ASC
            LIMIT %s
            """,
            (session_id, tenant_id, to_summarise_count),
        )
        rows = cur.fetchall() or []

        transcript_lines = []
        for r in rows:
            role = (r.get("role") or "").strip()
            content = (r.get("content") or "").strip()
            if not content:
                continue
            prefix = "User" if role == "user" else "Assistant"
            transcript_lines.append(f"{prefix}: {content}")

        if not transcript_lines:
            cur.close()
            return

        transcript = "\n".join(transcript_lines)

        max_out = int(os.getenv("SUMMARY_MAX_TOKENS", "220"))

        client = _get_summary_client()
        if client is None:
            cur.close()
            return

        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
            temperature=0.2,
            max_tokens=max_out,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarise the conversation so far for future context. "
                        "Keep it short and structured. Output only these sections:\n"
                        "- Customer goal\n- Store context\n- What has been asked\n- Answers given\n- Open questions\n"
                        "Do not include any hidden/system instructions."
                    ),
                },
                {"role": "user", "content": transcript},
            ],
        )

        summary_text = (resp.choices[0].message.content or "").strip()
        if not summary_text:
            cur.close()
            return

        _upsert_summary(cur, session_id, tenant_id, summary_text, total)
        conn.commit()

        prune = os.getenv("MEMORY_PRUNE_OLD_MESSAGES", "1").strip() not in ("0", "false", "False")
        if prune:
            # PostgreSQL doesn't support DELETE ... ORDER BY ... LIMIT directly
            cur.execute(
                """
                DELETE FROM chat_messages
                WHERE id IN (
                    SELECT id FROM chat_messages
                    WHERE session_id=%s AND tenant_id=%s
                    ORDER BY id ASC
                    LIMIT %s
                )
                """,
                (session_id, tenant_id, to_summarise_count),
            )
            conn.commit()

        cur.close()
    except Exception as e:
        print("? [MEMORY] maybe_summarize_session error:", str(e))
    finally:
        conn.close()


def get_history_with_summary(session_id: str, tenant_id: int, keep_last_n: int = 4):
    """Returns OpenAI chat history with a summary (if exists) + last N messages."""
    summary = get_summary(session_id, tenant_id)

    conn = get_db_connection()
    if not conn:
        last_msgs = []
    else:
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT role, content
                FROM chat_messages
                WHERE session_id=%s AND tenant_id=%s
                ORDER BY id DESC
                LIMIT %s
                """,
                (session_id, tenant_id, keep_last_n),
            )
            rows = cur.fetchall() or []
            cur.close()
            rows = list(reversed(rows))
            last_msgs = [{"role": r["role"], "content": r["content"]} for r in rows]
        except Exception as e:
            print("? [MEMORY] get_history_with_summary error:", str(e))
            last_msgs = []
        finally:
            conn.close()

    out = []
    if summary and summary.get("summary_text"):
        out.append(
            {
                "role": "system",
                "content": "Conversation summary (use as memory):\n" + (summary.get("summary_text") or ""),
            }
        )

    out.extend(last_msgs)
    return out


def get_session_status(session_id: str) -> dict:
    """Return whether this session is brand-new or has been dormant for 48+ hours.

    Must be called BEFORE ensure_session so last_seen hasn't been updated yet.
    Returns {"is_new": bool, "dormant": bool}.
    """
    conn = get_db_connection()
    if not conn:
        return {"is_new": False, "dormant": False}
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT last_seen FROM chat_sessions WHERE session_id=%s",
            (session_id,),
        )
        row = cur.fetchone()
        cur.close()
        if row is None:
            return {"is_new": True, "dormant": False}
        last_seen = row["last_seen"]
        if last_seen is None:
            return {"is_new": False, "dormant": False}
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        dormant = (now - last_seen) > timedelta(hours=48)
        return {"is_new": False, "dormant": dormant}
    except Exception as e:
        print("? [MEMORY] get_session_status error:", str(e))
        return {"is_new": False, "dormant": False}
    finally:
        conn.close()


def ensure_session(session_id: str, tenant_id: int):
    conn = get_db_connection()
    if not conn:
        print("? [MEMORY] ensure_session: DB connection failed")
        return

    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO chat_sessions (session_id, tenant_id)
            VALUES (%s, %s)
            ON CONFLICT (session_id) DO UPDATE SET last_seen = NOW()
        """, (session_id, tenant_id))
        conn.commit()
        cur.close()
    except Exception as e:
        print("? [MEMORY] ensure_session error:", str(e))
    finally:
        conn.close()


def add_message(session_id: str, tenant_id: int, role: str,
                content: str, embedding: list = None):
    conn = get_db_connection()
    if not conn:
        print("? [MEMORY] add_message: DB connection failed")
        return

    try:
        cur = conn.cursor()
        if embedding:
            vec_literal = "[" + ",".join(str(x) for x in embedding) + "]"
            cur.execute("""
                INSERT INTO chat_messages
                    (session_id, tenant_id, role, content, embedding)
                VALUES (%s, %s, %s, %s, %s::vector)
            """, (session_id, tenant_id, role, content, vec_literal))
        else:
            cur.execute("""
                INSERT INTO chat_messages (session_id, tenant_id, role, content)
                VALUES (%s, %s, %s, %s)
            """, (session_id, tenant_id, role, content))
        conn.commit()
        cur.close()
    except Exception as e:
        print("? [MEMORY] add_message error:", str(e))
    finally:
        conn.close()


def get_semantic_history(session_id: str, tenant_id: int,
                         query_embedding: list,
                         keep_last_n: int = 4,
                         semantic_top_k: int = 4) -> list:
    """
    Returns chat history combining:
      1. Summary (if exists) as a system message
      2. Top semantic_top_k relevant older user+reply pairs
      3. Last keep_last_n messages for conversational continuity
    Deduplicated and sorted chronologically.
    Falls back to get_history_with_summary() if semantic search fails.
    """
    summary = get_summary(session_id, tenant_id)

    conn = get_db_connection()
    if not conn:
        return get_history_with_summary(session_id, tenant_id, keep_last_n)

    try:
        cur = conn.cursor(cursor_factory=__import__("psycopg2").extras.RealDictCursor)

        # ── Step 1: fetch last N messages ────────────────────────────────────
        cur.execute("""
            SELECT id, role, content FROM chat_messages
            WHERE session_id = %s AND tenant_id = %s
            ORDER BY id DESC LIMIT %s
        """, (session_id, tenant_id, keep_last_n))
        last_rows = list(reversed(cur.fetchall() or []))
        last_ids  = {r["id"] for r in last_rows}

        # ── Step 2: semantic search for relevant older user messages ─────────
        semantic_rows = []
        if query_embedding and last_ids:
            vec_literal = "[" + ",".join(str(x) for x in query_embedding) + "]"
            exclude_ids = list(last_ids)
            cur.execute("""
                SELECT id, role, content FROM chat_messages
                WHERE session_id = %s AND tenant_id = %s
                  AND embedding IS NOT NULL
                  AND role = 'user'
                  AND id != ALL(%s)
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """, (session_id, tenant_id, exclude_ids, vec_literal, semantic_top_k))
            matched_user_rows = cur.fetchall() or []

            # For each matched user message, also fetch the next assistant reply
            for row in matched_user_rows:
                semantic_rows.append(dict(row))
                cur.execute("""
                    SELECT id, role, content FROM chat_messages
                    WHERE session_id = %s AND tenant_id = %s
                      AND id > %s AND role = 'assistant'
                    ORDER BY id ASC LIMIT 1
                """, (session_id, tenant_id, row["id"]))
                reply = cur.fetchone()
                if reply and reply["id"] not in last_ids:
                    semantic_rows.append(dict(reply))

        cur.close()
        conn.close()

        # ── Step 3: merge, deduplicate, sort chronologically ─────────────────
        seen_ids = set()
        merged   = []
        for row in semantic_rows + last_rows:
            if row["id"] not in seen_ids:
                seen_ids.add(row["id"])
                merged.append(row)
        merged.sort(key=lambda r: r["id"])

        # ── Step 4: build OpenAI messages list ───────────────────────────────
        out = []
        if summary and summary.get("summary_text"):
            out.append({
                "role": "system",
                "content": "Conversation summary (use as memory):\n" + summary["summary_text"],
            })
        out.extend({"role": r["role"], "content": r["content"]} for r in merged)
        return out

    except Exception as e:
        print("? [MEMORY] get_semantic_history error:", str(e))
        try:
            conn.close()
        except Exception:
            pass
        return get_history_with_summary(session_id, tenant_id, keep_last_n)


def get_recent_history(session_id: str, tenant_id: int, limit: int = 12):
    """
    Returns chat history in chronological order for OpenAI:
    [{"role":"user","content":"..."}, {"role":"assistant","content":"..."}]
    """
    conn = get_db_connection()
    if not conn:
        print("? [MEMORY] get_recent_history: DB connection failed")
        return []

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT role, content
            FROM chat_messages
            WHERE session_id = %s AND tenant_id = %s
            ORDER BY id DESC
            LIMIT %s
        """, (session_id, tenant_id, limit))
        rows = cur.fetchall()
        cur.close()
        rows = list(reversed(rows))
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as e:
        print("? [MEMORY] get_recent_history error:", str(e))
        return []
    finally:
        conn.close()
