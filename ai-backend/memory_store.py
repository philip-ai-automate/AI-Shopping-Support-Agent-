from db import get_db_connection

import os

# NOTE: We keep summarisation as BEST-EFFORT.
# If Azure OpenAI credentials are missing or the SDK is unavailable,
# the chat endpoint must still work normally.
try:
    from openai import AzureOpenAI  # type: ignore
except Exception:  # pragma: no cover
    AzureOpenAI = None  # type: ignore


def _get_summary_client():
    if AzureOpenAI is None:
        return None
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_key = os.getenv("AZURE_OPENAI_KEY")
    if not endpoint or not api_key:
        return None
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
    try:
        return AzureOpenAI(api_key=api_key, api_version=api_version, azure_endpoint=endpoint)
    except Exception:
        return None

def init_memory_tables():
    """
    Creates memory tables if they do not exist.
    This prevents 'nothing saved' when tables were never created.
    """
    conn = get_db_connection()
    if not conn:
        print("? [MEMORY] Cannot init tables: DB connection failed")
        return

    try:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
              session_id VARCHAR(64) PRIMARY KEY,
              tenant_id INT NOT NULL,
              created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              INDEX (tenant_id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
              id BIGINT AUTO_INCREMENT PRIMARY KEY,
              session_id VARCHAR(64) NOT NULL,
              tenant_id INT NOT NULL,
              role ENUM('user','assistant') NOT NULL,
              content TEXT NOT NULL,
              created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              INDEX (session_id),
              INDEX (tenant_id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_summaries (
              session_id VARCHAR(64) NOT NULL,
              tenant_id INT NOT NULL,
              summary_text TEXT NOT NULL,
              summarized_message_count INT NOT NULL DEFAULT 0,
              updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              PRIMARY KEY (session_id, tenant_id)
            )
        """)

        conn.commit()
        cur.close()
        print("? [MEMORY] Tables ready")
    except Exception as e:
        print("? [MEMORY] init_memory_tables error:", str(e))
    finally:
        conn.close()


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
        cur = conn.cursor(dictionary=True)
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
        return row
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
        ON DUPLICATE KEY UPDATE
            summary_text = VALUES(summary_text),
            summarized_message_count = VALUES(summarized_message_count)
        """,
        (session_id, tenant_id, summary_text, summarized_message_count),
    )


def maybe_summarize_session(session_id: str, tenant_id: int, keep_last_n: int = 4):
    """Summarise history after ~3 turns to reduce token usage.

    - Trigger when total messages >= 6 (3 user+assistant turns).
    - Summary will cover everything except the last keep_last_n messages.
    - Optionally prune older messages after successful summarisation.
    """
    conn = get_db_connection()
    if not conn:
        return

    try:
        cur = conn.cursor(dictionary=True)
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

        # Only resummarise when there is new material worth it.
        # (At least 2 new messages since the last summary.)
        if total - summarized_count < 2 and summarized_count > 0:
            cur.close()
            return

        # Fetch messages to summarise (everything except the last keep_last_n)
        to_summarise_count = max(0, total - keep_last_n)
        if to_summarise_count < 6:
            # Don't summarise too early; we want meaning.
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

        # Summarise with a small, structured summary.
        max_out = int(os.getenv("SUMMARY_MAX_TOKENS", "220"))

        client = _get_summary_client()
        if client is None:
            cur.close()
            return

        resp = client.chat.completions.create(
            model=os.getenv("AZURE_OPENAI_DEPLOYMENT"),
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

        # Prune old messages to enforce "replace with summary" behavior.
        prune = os.getenv("MEMORY_PRUNE_OLD_MESSAGES", "1").strip() not in ("0", "false", "False")
        if prune:
            # Keep only the last keep_last_n messages.
            cur.execute(
                """
                DELETE FROM chat_messages
                WHERE session_id=%s AND tenant_id=%s
                ORDER BY id ASC
                LIMIT %s
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

    # Always fetch the last N messages (chronological)
    conn = get_db_connection()
    if not conn:
        last_msgs = []
    else:
        try:
            cur = conn.cursor(dictionary=True)
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
            rows.reverse()
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
            ON DUPLICATE KEY UPDATE last_seen = CURRENT_TIMESTAMP
        """, (session_id, tenant_id))
        conn.commit()
        cur.close()
    except Exception as e:
        print("? [MEMORY] ensure_session error:", str(e))
    finally:
        conn.close()


def add_message(session_id: str, tenant_id: int, role: str, content: str):
    conn = get_db_connection()
    if not conn:
        print("? [MEMORY] add_message: DB connection failed")
        return

    try:
        cur = conn.cursor()
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
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT role, content
            FROM chat_messages
            WHERE session_id = %s AND tenant_id = %s
            ORDER BY id DESC
            LIMIT %s
        """, (session_id, tenant_id, limit))
        rows = cur.fetchall()
        cur.close()
        rows.reverse()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as e:
        print("? [MEMORY] get_recent_history error:", str(e))
        return []
    finally:
        conn.close()
