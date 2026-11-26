"""
inbox_attach.py — Inbox: send a photo, video or document (26 Nov 2026,
approved mock-up "Board 10") and the WhatsApp 24-hour rule (mock-up
"Board 11").

Files the team sends from the Inbox are kept on the big disk
(INBOX_FILE_DIR, /vault) under random names, one row each in inbox_files:
  * WhatsApp / Messenger: kept so the Inbox can show what was sent
    (logged-in route /inbox/files/<token>.<ext>, same business only).
  * Website chat: chat boxes show text only, so the visitor gets a link
    (public route /f/<token>, that one file only) — kept 90 days, then the
    file and its row are removed.

Checks (pure, no database): the file's extension AND its first bytes must
agree, and each kind has its own size limit (WhatsApp's own limits).
"""
import io
import os
import re
import secrets
import zipfile

import psycopg2.extras

from db import get_db_connection

FILE_DIR = os.getenv("INBOX_FILE_DIR", "/vault/phixtra-media/inbox-files")
PUBLIC_DAYS = 90
GRAPH = os.getenv("META_GRAPH_URL", "https://graph.facebook.com/v19.0").rstrip("/")

MB = 1024 * 1024
LIMITS = {"image": 5 * MB, "video": 16 * MB, "document": 25 * MB}
KIND_WORDS = {"image": "Photos", "video": "Videos", "document": "Documents"}

# extension -> (kind, content type, words for messages)
TYPES = {
    "jpg":  ("image", "image/jpeg", "JPG photo"),
    "jpeg": ("image", "image/jpeg", "JPG photo"),
    "png":  ("image", "image/png", "PNG photo"),
    "mp4":  ("video", "video/mp4", "MP4 video"),
    "3gp":  ("video", "video/3gpp", "3GP video"),
    "pdf":  ("document", "application/pdf", "PDF"),
    "doc":  ("document", "application/msword", "Word document"),
    "docx": ("document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Word document"),
    "xls":  ("document", "application/vnd.ms-excel", "Excel spreadsheet"),
    "xlsx": ("document", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "Excel spreadsheet"),
    "csv":  ("document", "text/csv", "CSV file"),
    "txt":  ("document", "text/plain", "text file"),
}
NOT_ALLOWED = ("This file type can't be sent. Send a photo (JPG or PNG), a video (MP4 or 3GP) "
               "or a document (PDF, Word, Excel, CSV or text).")

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_READY = False


# ── Pure checks ─────────────────────────────────────────────────────────────

def safe_filename(name: str) -> str:
    """The name the customer sees: no folders, no odd characters, not too long."""
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[\x00-\x1f<>:\"|?*]+", "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return "file"
    if len(name) > 100:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:90] + "." + ext[:8]) if dot else name[:100]
    return name


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


def _looks_like(ext: str, data: bytes) -> bool:
    """Do the file's first bytes match what its extension says it is?"""
    head = data[:16]
    if ext in ("jpg", "jpeg"):
        return head[:3] == b"\xff\xd8\xff"
    if ext == "png":
        return head[:8] == b"\x89PNG\r\n\x1a\n"
    if ext in ("mp4", "3gp"):
        return len(data) > 12 and data[4:8] == b"ftyp"
    if ext == "pdf":
        return data[:1024].lstrip()[:5] == b"%PDF-"
    if ext in ("doc", "xls"):
        return head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    if ext in ("docx", "xlsx"):
        if head[:4] != b"PK\x03\x04":
            return False
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except Exception:
            return False
        return ("word/document.xml" in names) if ext == "docx" else ("xl/workbook.xml" in names)
    if ext in ("csv", "txt"):
        sample = data[:65536]
        if b"\x00" in sample:
            return False
        for enc in ("utf-8", "cp1252"):
            try:
                sample.decode(enc)
                return True
            except UnicodeDecodeError:
                continue
        return False
    return False


def check_file(filename: str, data: bytes):
    """(info, None) when the file can be sent, else (None, plain message).
    info = {name, ext, kind, mime, size}."""
    if not filename or data is None or len(data) == 0:
        return None, "Choose a photo, video or document to send."
    name = safe_filename(filename)
    ext = _ext(name)
    if ext not in TYPES:
        return None, NOT_ALLOWED
    kind, mime, words = TYPES[ext]
    if len(data) > LIMITS[kind]:
        return None, (f"This file is too big to send. {KIND_WORDS[kind]} can be up to "
                      f"{LIMITS[kind] // MB} MB.")
    if not _looks_like(ext, data):
        return None, f"This file isn't a real {words}, so it wasn't sent. Save it again and try once more."
    if ext == "jpeg":
        ext = "jpg"
    return {"name": name, "ext": ext, "kind": kind, "mime": mime, "size": len(data)}, None


_IMG_MAGIC = ((b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"))


def _shrink_photo(data: bytes):
    """A phone camera photo over WhatsApp's 5 MB: the same photo as a
    smaller JPEG (longest side 2560 px), or None."""
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((2560, 2560))
        for q in (85, 75, 65):
            out = io.BytesIO()
            img.save(out, "JPEG", quality=q, optimize=True)
            if out.tell() <= LIMITS["image"]:
                return out.getvalue()
    except Exception as e:
        print("⚠️ inbox_attach shrink:", e)
    return None


def prepare(filename: str, data: bytes):
    """check_file, after two kindnesses for phone photos: a photo with no
    usual name ending is named from its first bytes, and a photo over 5 MB
    is made smaller (still checked again). (info, data, problem)."""
    name = safe_filename(filename or "")
    if data and _ext(name) not in TYPES:
        for magic, ext in _IMG_MAGIC:
            if data.startswith(magic):
                name = (name.rsplit(".", 1)[0] if "." in name else name) + "." + ext
                break
    ext = _ext(name)
    if data and ext in TYPES and TYPES[ext][0] == "image" and len(data) > LIMITS["image"] \
            and len(data) <= LIMITS["document"] and _looks_like(ext, data):
        small = _shrink_photo(data)
        if small:
            data = small
            name = (name.rsplit(".", 1)[0]) + ".jpg"
    info, problem = check_file(name, data)
    return info, data, problem


def kind_from_url(url: str):
    """'image' / 'video' / 'document' for an Inbox file link (/inbox/files/<token>.<ext>)."""
    ext = _ext((url or "").split("?", 1)[0])
    return TYPES[ext][0] if ext in TYPES else None


def size_words(n: int) -> str:
    if n >= MB:
        return f"{n / MB:.1f} MB"
    return f"{max(1, round(n / 1024))} KB"


def first_name(name):
    """First word of a contact's name, or None when there's no real name
    (empty, a phone number)."""
    word = (name or "").strip().split(" ")[0].strip(",.") if name else ""
    if not word or any(ch.isdigit() for ch in word) or word.startswith("+"):
        return None
    return word[:40]


_NAME_BLANK_RE = re.compile(
    r"(?i)\b(hi|hello|hey|hiya|dear|good\s+(?:morning|afternoon|evening))\s*,?\s*\{\{\s*1\s*\}\}")


def blank_is_name(body: str) -> bool:
    """Is {{1}} clearly the customer's name ("Hi {{1}}", "Dear {{1}}")?"""
    return bool(_NAME_BLANK_RE.search(body or ""))


def webchat_line(name: str, url: str) -> str:
    return f"\U0001F4CE {name}: {url}"


_WEB_LINE_RE = re.compile(r"\n?\U0001F4CE [^\n]*: https?://\S+/f/([A-Za-z0-9_-]{20,64})\s*$")


def split_webchat_text(text: str):
    """(text without the file line, token) for a website-chat reply that
    carried a file; (text, None) otherwise."""
    m = _WEB_LINE_RE.search(text or "")
    if not m:
        return text, None
    return (text[:m.start()]).rstrip(), m.group(1)


# ── Storage ─────────────────────────────────────────────────────────────────

def ensure_table(cur=None):
    global _READY
    if _READY:
        return
    own = cur is None
    if own:
        conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SET LOCAL lock_timeout = '3s'")
        cur.execute("""CREATE TABLE IF NOT EXISTS inbox_files (
            id BIGSERIAL PRIMARY KEY,
            token VARCHAR(64) NOT NULL UNIQUE,
            tenant_id INTEGER NOT NULL,
            conv_key VARCHAR(200) NOT NULL,
            channel VARCHAR(12) NOT NULL,
            filename TEXT NOT NULL,
            ext VARCHAR(8) NOT NULL,
            kind VARCHAR(12) NOT NULL,
            mime VARCHAR(120) NOT NULL,
            size_bytes INTEGER NOT NULL,
            is_public BOOLEAN NOT NULL DEFAULT FALSE,
            expires_at TIMESTAMPTZ,
            log_ref VARCHAR(40),
            sent_by_label TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_inbox_files_ref ON inbox_files(tenant_id, log_ref)")
        if own:
            conn.commit()
        _READY = True
    finally:
        if own:
            cur.close(); conn.close()


def _path(tenant_id: int, token: str, ext: str) -> str:
    return os.path.join(FILE_DIR, str(int(tenant_id)), f"{token}.{ext}")


def save(tenant_id: int, conv_key: str, channel: str, info: dict, data: bytes,
         public: bool = False, sent_by: str = None) -> str:
    """Keep the file; returns its token."""
    ensure_table()
    token = secrets.token_urlsafe(24)
    path = _path(tenant_id, token, info["ext"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO inbox_files (token, tenant_id, conv_key, channel, filename, ext, kind, mime,
                                                size_bytes, is_public, expires_at, sent_by_label)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               CASE WHEN %s THEN NOW() + make_interval(days => %s) END, %s)""",
                    (token, tenant_id, conv_key[:200], channel, info["name"], info["ext"], info["kind"], info["mime"],
                     info["size"], public, public, PUBLIC_DAYS, (sent_by or "")[:200]))
        conn.commit()
    except Exception:
        conn.rollback()
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    finally:
        cur.close(); conn.close()
    return token


def link(token: str, log_ref: str) -> None:
    """Tie the file to the message row it was sent with ('fb:<id>', 'wr:<id>', 'wa:<id>')."""
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE inbox_files SET log_ref=%s WHERE token=%s", (log_ref[:40], token))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print("⚠️ inbox_attach.link:", e)


def forget(token: str) -> None:
    """The send failed: remove the file and its row."""
    row = get(token)
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("DELETE FROM inbox_files WHERE token=%s", (token,))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print("⚠️ inbox_attach.forget:", e)
    if row:
        try:
            os.remove(_path(row["tenant_id"], token, row["ext"]))
        except OSError:
            pass


def get(token: str):
    if not token or not _TOKEN_RE.match(token):
        return None
    try:
        ensure_table()
        conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT token, tenant_id, conv_key, channel, filename, ext, kind, mime, size_bytes,
                              is_public, expires_at, (expires_at IS NOT NULL AND expires_at < NOW()) AS expired
                         FROM inbox_files WHERE token=%s""", (token,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return row
    except Exception as e:
        print("⚠️ inbox_attach.get:", e)
        return None


def file_path(row: dict) -> str:
    return _path(row["tenant_id"], row["token"], row["ext"])


def inbox_url(token: str, ext: str) -> str:
    return f"/inbox/files/{token}.{ext}"


def by_refs(tenant_id: int, refs: list) -> dict:
    """{log_ref: {url, kind, name}} for message rows that carried a file."""
    refs = [r for r in refs if r]
    if not refs:
        return {}
    try:
        ensure_table()
        conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT log_ref, token, ext, kind, filename FROM inbox_files
                        WHERE tenant_id=%s AND log_ref = ANY(%s)""", (tenant_id, refs))
        rows = cur.fetchall() or []
        cur.close(); conn.close()
        return {r["log_ref"]: {"url": inbox_url(r["token"], r["ext"]), "kind": r["kind"], "name": r["filename"]}
                for r in rows}
    except Exception as e:
        print("⚠️ inbox_attach.by_refs:", e)
        return {}


def names_by_tokens(tenant_id: int, tokens: list) -> dict:
    tokens = [t for t in tokens if t and _TOKEN_RE.match(t)]
    if not tokens:
        return {}
    try:
        ensure_table()
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT token, filename FROM inbox_files WHERE tenant_id=%s AND token = ANY(%s)",
                    (tenant_id, tokens))
        out = {t: n for t, n in cur.fetchall() or []}
        cur.close(); conn.close()
        return out
    except Exception as e:
        print("⚠️ inbox_attach.names_by_tokens:", e)
        return {}


def token_from_inbox_url(url: str):
    m = re.match(r"^/inbox/files/([A-Za-z0-9_-]{20,64})\.[a-z0-9]{2,5}$", url or "")
    return m.group(1) if m else None


def remove_expired() -> int:
    """Website-chat files are kept 90 days."""
    try:
        ensure_table()
        conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""DELETE FROM inbox_files WHERE expires_at IS NOT NULL AND expires_at < NOW()
                       RETURNING tenant_id, token, ext""")
        rows = cur.fetchall() or []
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print("⚠️ inbox_attach.remove_expired:", e)
        return 0
    for r in rows:
        try:
            os.remove(_path(r["tenant_id"], r["token"], r["ext"]))
        except OSError:
            pass
    return len(rows)


# ── Sending (WhatsApp Cloud API, Messenger Send API) ───────────────────────

def wa_send(phone_number_id: str, access_token: str, to: str, info: dict, data: bytes, caption: str):
    """Upload the file to Meta, then send it. (ok, meta_message_id, why)."""
    import requests as _req
    headers = {"Authorization": f"Bearer {access_token}"}
    # WhatsApp takes CSV files as plain text documents.
    up_mime = "text/plain" if info["ext"] == "csv" else info["mime"]
    try:
        up = _req.post(f"{GRAPH}/{phone_number_id}/media", headers=headers,
                       data={"messaging_product": "whatsapp", "type": up_mime},
                       files={"file": (info["name"], data, up_mime)}, timeout=60)
        media_id = up.json().get("id") if up.status_code == 200 else None
        if not media_id:
            print(f"⚠️ inbox WA media upload {up.status_code} {up.text[:300]}")
            return False, None, "WhatsApp didn't accept the file. Please try again."
        body = {"id": media_id}
        if info["kind"] == "document":
            body["filename"] = info["name"]
        if caption:
            body["caption"] = caption[:1024]
        r = _req.post(f"{GRAPH}/{phone_number_id}/messages", headers=headers, timeout=20,
                      json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": to,
                            "type": info["kind"], info["kind"]: body})
        if r.status_code != 200:
            print(f"⚠️ inbox WA media send {r.status_code} {r.text[:300]}")
            return False, None, "WhatsApp didn't accept the message. Please try again."
        meta_id = (r.json().get("messages") or [{}])[0].get("id")
        return True, meta_id, ""
    except Exception as e:
        print("⚠️ inbox WA media:", e)
        return False, None, "Couldn't reach WhatsApp just now. Please try again."


def fb_send(page_token: str, psid: str, info: dict, data: bytes, caption: str):
    """Send the file in Messenger (upload with the message), then the
    caption as a normal message — Messenger files have no caption. (ok, why)."""
    import json as _json
    import requests as _req
    fb_type = {"image": "image", "video": "video"}.get(info["kind"], "file")
    try:
        r = _req.post(f"{GRAPH}/me/messages", params={"access_token": page_token}, timeout=60,
                      data={"recipient": _json.dumps({"id": psid}),
                            "message": _json.dumps({"attachment": {"type": fb_type, "payload": {"is_reusable": False}}}),
                            "messaging_type": "RESPONSE"},
                      files={"filedata": (info["name"], data, info["mime"])})
        if r.status_code != 200:
            print(f"⚠️ inbox Messenger file {r.status_code} {r.text[:300]}")
            try:
                why = (r.json().get("error", {}) or {}).get("message") or ""
            except Exception:
                why = ""
            return False, why or "Facebook didn't accept the file."
        if caption:
            r2 = _req.post(f"{GRAPH}/me/messages", params={"access_token": page_token}, timeout=10,
                           json={"recipient": {"id": psid}, "message": {"text": caption}, "messaging_type": "RESPONSE"})
            if r2.status_code != 200:
                print(f"⚠️ inbox Messenger caption {r2.status_code} {r2.text[:300]}")
        return True, ""
    except Exception as e:
        print("⚠️ inbox Messenger file:", e)
        return False, "Couldn't reach Facebook just now."
