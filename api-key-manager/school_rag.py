"""
school_rag.py — RAG helpers for the school parent AI bot's knowledge base.

Embeds Q&A entries (school_knowledge) and uploaded documents
(school_kb_documents) into a single retrieval index (school_kb_chunks) that
the WhatsApp bot (school-wa-gateway/ai_handler.py) searches at chat time.

Mirrors the merchant product catalog's proven pgvector pattern
(pg_schema.sql `documents` table / ai-backend/search.py) — same embedding
model, same vector(1536) + tsvector hybrid-search shape.
"""
import os
import re

from db import get_db_connection

_EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
_openai_client = None


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _openai_client


def embed_text(text: str) -> list | None:
    """Return a 1536-dim embedding for text, or None on any failure.
    Never raises — embedding failure must not block saving content."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        resp = _get_openai_client().embeddings.create(model=_EMBED_MODEL, input=[text])
        return [float(x) for x in resp.data[0].embedding]
    except Exception as e:
        print(f"⚠️ [school_rag] embed_text error: {e}")
        return None


def _vec_literal(vec: list) -> str:
    return "[" + ",".join(str(x) for x in vec) + "]"


# ── Q&A sync ─────────────────────────────────────────────────────────────────

def sync_qa_chunk(knowledge_id: int):
    """Upsert the school_kb_chunks row mirroring one school_knowledge entry.
    Call after every knowledge_add / knowledge_edit / knowledge_delete write."""
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute(
            "SELECT school_id, category, question, answer, is_active "
            "FROM school_knowledge WHERE id=%s",
            (knowledge_id,)
        )
        row = cur.fetchone()

        if not row or not row[4]:
            # Deleted, or deactivated — remove its chunk if present.
            cur.execute(
                "DELETE FROM school_kb_chunks WHERE source_type='qa' AND source_id=%s",
                (knowledge_id,)
            )
            conn.commit()
            return

        school_id, category, question, answer, _ = row
        content = f"Q: {question}\nA: {answer}"
        embedding = embed_text(content)
        vec = _vec_literal(embedding) if embedding else None

        cur.execute("""
            INSERT INTO school_kb_chunks (school_id, source_type, source_id, chunk_index, title, content, embedding)
            VALUES (%s,'qa',%s,0,%s,%s,%s)
            ON CONFLICT (source_type, source_id, chunk_index)
            DO UPDATE SET title=EXCLUDED.title, content=EXCLUDED.content, embedding=EXCLUDED.embedding
        """, (school_id, knowledge_id, category, content, vec))
        conn.commit()
    except Exception as e:
        print(f"⚠️ [school_rag] sync_qa_chunk error: {e}")
        conn.rollback()
    finally:
        cur.close(); conn.close()


# ── Chunking ─────────────────────────────────────────────────────────────────

def chunk_text(text: str, target_words: int = 450, overlap_words: int = 90) -> list:
    """Paragraph-aware splitter. ~450 words ≈ 600 tokens for English prose.
    No tokenizer dependency — word count is a close-enough proxy for chunk sizing."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []

    chunks = []
    current_words: list = []

    for para in paragraphs:
        para_words = para.split()
        if len(current_words) + len(para_words) > target_words and current_words:
            chunks.append(" ".join(current_words))
            # carry the tail of the previous chunk forward for context continuity
            current_words = current_words[-overlap_words:] if overlap_words else []
        current_words.extend(para_words)
        # A single paragraph longer than target_words on its own — flush immediately,
        # otherwise it would swallow every following paragraph into one giant chunk.
        while len(current_words) > target_words * 1.5:
            chunks.append(" ".join(current_words[:target_words]))
            current_words = current_words[target_words - overlap_words:]

    if current_words:
        chunks.append(" ".join(current_words))

    return chunks


# ── Document text extraction ────────────────────────────────────────────────

def _extract_text(file_path: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(file_path)
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    if ext == ".docx":
        from docx import Document as DocxDocument
        doc = DocxDocument(file_path)
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    # .txt, .md, anything else readable as plain text
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def process_document(document_id: int):
    """Extract → chunk → embed → store. Updates school_kb_documents.status
    to 'ready' or 'failed'. Designed to run in a background thread so the
    upload request returns immediately."""
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute(
            "SELECT school_id, filename, file_path FROM school_kb_documents WHERE id=%s",
            (document_id,)
        )
        row = cur.fetchone()
        if not row:
            return
        school_id, filename, file_path = row

        text = _extract_text(file_path)
        chunks = chunk_text(text)

        if not chunks:
            cur.execute(
                "UPDATE school_kb_documents SET status='failed', error_message=%s WHERE id=%s",
                ("No extractable text found in this file.", document_id)
            )
            conn.commit()
            return

        for i, chunk in enumerate(chunks):
            embedding = embed_text(chunk)
            vec = _vec_literal(embedding) if embedding else None
            cur.execute("""
                INSERT INTO school_kb_chunks (school_id, source_type, source_id, chunk_index, title, content, embedding)
                VALUES (%s,'document',%s,%s,%s,%s,%s)
            """, (school_id, document_id, i, f"{filename} — part {i+1}", chunk, vec))

        cur.execute(
            "UPDATE school_kb_documents SET status='ready', chunk_count=%s WHERE id=%s",
            (len(chunks), document_id)
        )
        conn.commit()
    except Exception as e:
        print(f"⚠️ [school_rag] process_document error: {e}")
        conn.rollback()
        try:
            cur.execute(
                "UPDATE school_kb_documents SET status='failed', error_message=%s WHERE id=%s",
                (str(e)[:500], document_id)
            )
            conn.commit()
        except Exception:
            pass
    finally:
        cur.close(); conn.close()


def delete_document(document_id: int, school_id: int) -> bool:
    """Delete a document's file, its chunks, and its tracking row."""
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute(
            "SELECT file_path FROM school_kb_documents WHERE id=%s AND school_id=%s",
            (document_id, school_id)
        )
        row = cur.fetchone()
        if not row:
            return False
        file_path = row[0]
        cur.execute(
            "DELETE FROM school_kb_chunks WHERE source_type='document' AND source_id=%s",
            (document_id,)
        )
        cur.execute(
            "DELETE FROM school_kb_documents WHERE id=%s AND school_id=%s",
            (document_id, school_id)
        )
        conn.commit()
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
        return True
    except Exception as e:
        print(f"⚠️ [school_rag] delete_document error: {e}")
        conn.rollback()
        return False
    finally:
        cur.close(); conn.close()
