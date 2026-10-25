"""Product photo gallery (2026-10-25, approved mock-up
https://claude.ai/artifact/117JTXRzEwCjZSZk5yWnNn).

A product can have several photos: the first (position 0) is the main one.
Photos added by hand are made smaller (sharp on any phone, ~0.3 MB) and kept
on the big disk (PRODUCT_PHOTO_DIR, /vault). Photos from a spreadsheet or a
WooCommerce store stay where they are; only the link is kept.

Labels (Front, Back, … plus an optional colour) are written or confirmed by
the business. The photo tool (clip-embedder, our own server, no AI tokens)
only *suggests* a view; a suggestion is never used by the AI until the
business confirms it. Limit per product: plans.photos_per_product (-1 = none).
"""
import io
import os
import re
import uuid

import requests
from PIL import Image, ImageOps

from db import get_db_connection

MEDIA_DIR = os.getenv("PRODUCT_PHOTO_DIR", "/vault/phixtra-media/product-photos")
MEDIA_URL = "/media/product-photos"
CLIP_URL = os.getenv("CLIP_EMBEDDER_URL", "http://127.0.0.1:8020").rstrip("/")
BASE_URL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")

MAX_EDGE, THUMB_EDGE, MIN_EDGE = 1600, 400, 150
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif", "heic", "heif"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

VIEWS = [("front", "Front"), ("back", "Back"), ("side", "Side"), ("closeup", "Close-up"),
         ("worn", "Worn / In use"), ("other", "Other")]
VIEW_LABELS = dict(VIEWS)
# Phrases the photo tool compares a photo with. Generic on purpose (any product).
_PROMPTS = {
    "front": ["a photo of the front of a product", "front view of an item for sale"],
    "back": ["a photo of the back of a product", "rear view of an item, seen from behind"],
    "side": ["a photo of the side of a product", "side view of an item, in profile"],
    "closeup": ["a close-up photo of fabric or material texture", "a close-up detail of a product"],
    "worn": ["a person wearing or using the product", "a model wearing the clothing"],
}
_text_vecs = {}
_FNAME_RE = re.compile(r"^[0-9a-f]{32}(_t)?\.jpg$")


# ── plan limit ──────────────────────────────────────────────────────────────

def limit_for(cur, tenant_id: int):
    """Photos allowed per product: an int, or None for no limit (Enterprise)."""
    cur.execute("SELECT p.photos_per_product FROM tenants t JOIN plans p ON p.id = t.plan_id WHERE t.id=%s", (tenant_id,))
    row = cur.fetchone()
    n = (row[0] if not isinstance(row, dict) else row["photos_per_product"]) if row else 8
    return None if n is None or int(n) < 0 else int(n)


# ── reading ─────────────────────────────────────────────────────────────────

def photos(cur, tenant_id: int, key: str) -> list:
    cur.execute("""SELECT id, source, url, thumb_url, position, view_label, colour, suggested_view, width, height
                   FROM product_photos WHERE tenant_id=%s AND product_key=%s ORDER BY position, id""", (tenant_id, key))
    out = []
    for r in cur.fetchall():
        r = dict(r)
        r["view_text"] = VIEW_LABELS.get(r["view_label"] or "", "")
        if r["suggested_view"] not in VIEW_LABELS:
            r["suggested_view"] = None
        r["suggested_text"] = VIEW_LABELS.get(r["suggested_view"] or "", "")
        r["label_text"] = " · ".join(x for x in (r["view_text"], r["colour"] or "") if x)
        out.append(r)
    return out


def counts(cur, tenant_id: int, keys: list) -> dict:
    """{key: (photos, without a label)} for a page of products."""
    if not keys:
        return {}
    cur.execute("""SELECT product_key, count(*) AS n, count(*) FILTER (WHERE view_label IS NULL) AS unlabelled
                   FROM product_photos WHERE tenant_id=%s AND product_key = ANY(%s) GROUP BY 1""", (tenant_id, list(keys)))
    return {r["product_key"]: (int(r["n"]), int(r["unlabelled"])) for r in cur.fetchall()}


def unlabelled_keys(cur, tenant_id: int) -> set:
    cur.execute("SELECT DISTINCT product_key FROM product_photos WHERE tenant_id=%s AND view_label IS NULL", (tenant_id,))
    return {r["product_key"] for r in cur.fetchall()}


# ── the photo tool (our own server) ─────────────────────────────────────────

def _embed_image(jpeg: bytes):
    try:
        r = requests.post(f"{CLIP_URL}/embed-image", files={"file": ("p.jpg", jpeg, "image/jpeg")}, timeout=30)
        return r.json().get("embedding") if r.ok else None
    except Exception as e:
        print("⚠️ product_photos embed_image:", e)
        return None


def _view_vectors():
    if not _text_vecs:
        try:
            for view, phrases in _PROMPTS.items():
                r = requests.post(f"{CLIP_URL}/embed-text", json={"texts": phrases}, timeout=30)
                vecs = r.json()["embeddings"]
                avg = [sum(v[i] for v in vecs) / len(vecs) for i in range(len(vecs[0]))]
                norm = sum(x * x for x in avg) ** 0.5 or 1.0
                _text_vecs[view] = [x / norm for x in avg]
        except Exception as e:
            print("⚠️ product_photos view vectors:", e)
            _text_vecs.clear()
    return _text_vecs


def suggest_view(embedding):
    """The closest view, or None. A suggestion only; the business confirms it."""
    vecs = _view_vectors()
    if not embedding or not vecs:
        return None
    scores = {v: sum(a * b for a, b in zip(embedding, t)) for v, t in vecs.items()}
    return max(scores, key=scores.get)


def _vec(embedding):
    return "[" + ",".join(f"{x:.6f}" for x in embedding) + "]" if embedding else None


# ── writing ─────────────────────────────────────────────────────────────────

def _shrink(raw: bytes):
    """(large jpeg, thumb jpeg, width, height) or raises ValueError(reason)."""
    try:
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im)
        im.load()
    except Exception:
        raise ValueError("isn't a photo we can read")
    if min(im.size) < MIN_EDGE:
        raise ValueError(f"is too small (at least {MIN_EDGE} pixels each side)")
    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        im = im.convert("RGBA"); bg.paste(im, mask=im.split()[-1]); im = bg
    else:
        im = im.convert("RGB")
    big = im.copy(); big.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    small = im.copy(); small.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.LANCZOS)
    b1, b2 = io.BytesIO(), io.BytesIO()
    big.save(b1, "JPEG", quality=82, optimize=True, progressive=True)
    small.save(b2, "JPEG", quality=78, optimize=True)
    return b1.getvalue(), b2.getvalue(), big.size[0], big.size[1]


def add_upload(tenant_id: int, key: str, file_storage, actor: str):
    """Save one uploaded photo. Returns (photo dict, None) or (None, why not)."""
    name = (file_storage.filename or "photo").strip()
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED_EXT:
        return None, f"{name}: use a JPG, PNG or WebP photo"
    raw = file_storage.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return None, f"{name}: is bigger than 25 MB"
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        limit = limit_for(cur, tenant_id)
        cur.execute("SELECT count(*), coalesce(max(position), -1) FROM product_photos WHERE tenant_id=%s AND product_key=%s",
                    (tenant_id, key))
        _r = cur.fetchone()
        have, last = (_r["count"], _r["coalesce"]) if isinstance(_r, dict) else _r
        if limit is not None and have >= limit:
            return None, f"{name}: your plan allows {limit} photos per product"
        try:
            big, thumb, w, h = _shrink(raw)
        except ValueError as e:
            return None, f"{name}: {e}"
        folder = os.path.join(MEDIA_DIR, str(int(tenant_id)))
        os.makedirs(folder, exist_ok=True)
        fid = uuid.uuid4().hex
        with open(os.path.join(folder, f"{fid}.jpg"), "wb") as f:
            f.write(big)
        with open(os.path.join(folder, f"{fid}_t.jpg"), "wb") as f:
            f.write(thumb)
        emb = _embed_image(big)
        url = f"{BASE_URL}{MEDIA_URL}/{int(tenant_id)}/{fid}.jpg"
        turl = f"{BASE_URL}{MEDIA_URL}/{int(tenant_id)}/{fid}_t.jpg"
        cur.execute("""INSERT INTO product_photos (tenant_id, product_key, source, url, thumb_url, position, suggested_view,
                                                   image_embedding, width, height, bytes, created_by)
                       VALUES (%s,%s,'upload',%s,%s,%s,%s,%s::vector,%s,%s,%s,%s) RETURNING id""",
                    (tenant_id, key, url, turl, last + 1, suggest_view(emb), _vec(emb), w, h, len(big), actor))
        pid = cur.fetchone()[0]
        _sync_main(cur, tenant_id, key)
        conn.commit()
        return {"id": pid, "url": url, "thumb_url": turl}, None
    finally:
        conn.close()


def set_label(tenant_id: int, key: str, photo_id: int, view: str, colour: str) -> bool:
    if view not in VIEW_LABELS:
        return False
    colour = re.sub(r"\s+", " ", (colour or "").strip())[:40] or None
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""UPDATE product_photos SET view_label=%s, colour=%s, updated_at=NOW()
                       WHERE tenant_id=%s AND product_key=%s AND id=%s""", (view, colour, tenant_id, key, photo_id))
        ok = cur.rowcount == 1
        conn.commit()
        return ok
    finally:
        conn.close()


def reorder(tenant_id: int, key: str, ids: list) -> bool:
    """ids in their new order; the first becomes the main photo."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM product_photos WHERE tenant_id=%s AND product_key=%s", (tenant_id, key))
        have = {r[0] for r in cur.fetchall()}
        ids = [int(i) for i in ids if str(i).isdigit() and int(i) in have]
        if set(ids) != have:
            return False
        for pos, pid in enumerate(ids):
            cur.execute("UPDATE product_photos SET position=%s, updated_at=NOW() WHERE id=%s", (pos, pid))
        _sync_main(cur, tenant_id, key)
        conn.commit()
        return True
    finally:
        conn.close()


def delete(tenant_id: int, key: str, photo_id: int) -> bool:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""DELETE FROM product_photos WHERE tenant_id=%s AND product_key=%s AND id=%s
                       RETURNING source, url, thumb_url""", (tenant_id, key, photo_id))
        row = cur.fetchone()
        if not row:
            return False
        if row[0] == "upload":
            for u in (row[1], row[2]):
                fn = (u or "").rsplit("/", 1)[-1]
                if _FNAME_RE.match(fn):
                    try:
                        os.remove(os.path.join(MEDIA_DIR, str(int(tenant_id)), fn))
                    except OSError:
                        pass
        cur.execute("SELECT id FROM product_photos WHERE tenant_id=%s AND product_key=%s ORDER BY position, id", (tenant_id, key))
        for pos, (pid,) in enumerate(cur.fetchall()):
            cur.execute("UPDATE product_photos SET position=%s WHERE id=%s", (pos, pid))
        _sync_main(cur, tenant_id, key)
        conn.commit()
        return True
    finally:
        conn.close()


def _sync_main(cur, tenant_id: int, key: str):
    """Products added by hand keep products.image_url = the main photo, so
    every place that already shows one photo keeps working."""
    if not key.startswith("p:"):
        return
    cur.execute("SELECT url FROM product_photos WHERE tenant_id=%s AND product_key=%s ORDER BY position, id LIMIT 1",
                (tenant_id, key))
    row = cur.fetchone()
    url = (row["url"] if isinstance(row, dict) else row[0]) if row else None
    cur.execute("UPDATE products SET image_url=%s, updated_at=NOW() WHERE tenant_id=%s AND id=%s",
                (url, tenant_id, key[2:]))


def adopt_legacy(cur, tenant_id: int, key: str, image_url: str):
    """A product saved before galleries existed: its one photo becomes the main
    photo of its gallery (no label yet). Leaves the file where it is."""
    if not image_url:
        return
    cur.execute("SELECT 1 FROM product_photos WHERE tenant_id=%s AND product_key=%s LIMIT 1", (tenant_id, key))
    if cur.fetchone():
        return
    url = image_url if image_url.startswith("http") else f"{BASE_URL}{image_url}"
    cur.execute("""INSERT INTO product_photos (tenant_id, product_key, source, url, thumb_url, position, external_url, created_by)
                   VALUES (%s,%s,'upload',%s,%s,0,%s,'Saved before galleries')""", (tenant_id, key, url, url, image_url))


def safe_media_name(fname: str) -> bool:
    return bool(_FNAME_RE.match(fname or ""))


# ── photos given as links: spreadsheet import, WooCommerce (2026-10-25) ─────

_VIEW_WORDS = [("closeup", ("close-up", "close up", "closeup", "detail", "zoom", "fabric", "texture")),
               ("worn", ("worn", "wearing", "in use", "model", "lifestyle", "on body")),
               ("front", ("front",)), ("back", ("back", "rear", "behind")), ("side", ("side", "profile")),
               ("other", ("other",))]


_FILLER = {"it", "the", "a", "an", "of", "in", "on", "with", "and", "view", "photo", "picture", "image", "shot",
           "colour", "color", "pic", "img"}


def label_from_text(label: str):
    """A label the business wrote ("Back", "Red", "Back red") → (view, colour).
    Words that aren't a view become the colour. A colour alone means view
    "other" (the business told us what it is, so it counts as labelled)."""
    text = re.sub(r"\s+", " ", (label or "").strip())
    if not text:
        return None, None
    low, view = text.lower(), None
    for v, words in _VIEW_WORDS:
        for w in words:
            if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low):
                view = view or v
                low = re.sub(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", " ", low)
    words = [w for w in re.split(r"[\s,;:-]+", low) if w and w not in _FILLER]
    colour = " ".join(words).strip()
    colour = colour.title()[:40] if colour else None
    if not view and colour:
        view = "other"
    return view, colour


_LINK_RE = re.compile(r"(?:([^|,;\n]*?)\|)?\s*(https?://[^\s,;|]+)")


def parse_cell(text: str) -> list:
    """A spreadsheet photo cell → [(view, colour, url)] in order. Accepts links
    separated by commas, semicolons, spaces or new lines, each optionally
    written as Label|link."""
    out, seen = [], set()
    for m in _LINK_RE.finditer(text or ""):
        label, url = (m.group(1) or "").strip(), m.group(2).strip()
        if url in seen:
            continue
        seen.add(url)
        view, colour = label_from_text(label)
        out.append((view, colour, url[:1024]))
    return out


def main_link(text: str) -> str:
    items = parse_cell(text)
    return items[0][2] if items else ""


def sync_links(cur, tenant_id: int, key: str, items: list, source: str, actor: str = ""):
    """Make the gallery's linked photos match `items` [(view, colour, url)] in
    that order (up to the plan's limit). A label given in `items` is used;
    otherwise a label the business already confirmed for that link is kept.
    Links no longer listed are dropped. Uploaded photos come after the links."""
    limit = limit_for(cur, tenant_id)
    if limit is not None:
        items = items[:limit]
    urls = [u for _v, _c, u in items]
    cur.execute("""DELETE FROM product_photos WHERE tenant_id=%s AND product_key=%s AND source=%s
                   AND NOT (external_url = ANY(%s))""", (tenant_id, key, source, urls or [""]))
    for pos, (view, colour, url) in enumerate(items):
        cur.execute("""INSERT INTO product_photos (tenant_id, product_key, source, url, thumb_url, external_url, position,
                                                   view_label, colour, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (tenant_id, product_key, external_url) WHERE external_url IS NOT NULL DO UPDATE SET
                           position = EXCLUDED.position, url = EXCLUDED.url, source = EXCLUDED.source,
                           view_label = COALESCE(EXCLUDED.view_label, product_photos.view_label),
                           colour = COALESCE(EXCLUDED.colour, product_photos.colour), updated_at = NOW()""",
                    (tenant_id, key, source, url, url, url, pos, view, colour, actor or source))
    cur.execute("""SELECT id FROM product_photos WHERE tenant_id=%s AND product_key=%s
                   ORDER BY (external_url = ANY(%s)) DESC, position, id""", (tenant_id, key, urls or [""]))
    for pos, row in enumerate(cur.fetchall()):
        pid = row["id"] if isinstance(row, dict) else row[0]
        cur.execute("UPDATE product_photos SET position=%s WHERE id=%s", (pos, pid))
    _sync_main(cur, tenant_id, key)


def set_label_any(cur, tenant_id: int, key: str, photo_id: int, view: str, colour: str) -> bool:
    """set_label inside the caller's transaction (store products)."""
    if view not in VIEW_LABELS:
        return False
    colour = re.sub(r"\s+", " ", (colour or "").strip())[:40] or None
    cur.execute("""UPDATE product_photos SET view_label=%s, colour=%s, updated_at=NOW()
                   WHERE tenant_id=%s AND product_key=%s AND id=%s""", (view, colour, tenant_id, key, photo_id))
    return cur.rowcount == 1


def _fetch(url: str):
    """A linked photo's bytes (store or spreadsheet link), or None. Some stores'
    certificates don't check out from this server; it's a public product photo,
    so a second try without the check is acceptable."""
    for verify in (True, False):
        try:
            r = requests.get(url, timeout=12, verify=verify, stream=True,
                             headers={"User-Agent": "PhiXtra product photos"})
            if not r.ok:
                return None
            raw = r.raw.read(MAX_UPLOAD_BYTES + 1, decode_content=True)
            return raw if len(raw) <= MAX_UPLOAD_BYTES else None
        except requests.exceptions.SSLError:
            continue
        except Exception as e:
            print("⚠️ product_photos fetch:", url[:80], e)
            return None
    return None


def fill_suggestions(cur, tenant_id: int, key: str, most: int = 8) -> int:
    """Linked photos (store / spreadsheet) that haven't been looked at yet get
    a fingerprint (photo search) and a suggested view. Returns how many."""
    cur.execute("""SELECT id, url FROM product_photos WHERE tenant_id=%s AND product_key=%s AND source <> 'upload'
                   AND image_embedding IS NULL AND suggested_view IS NULL ORDER BY position LIMIT %s""", (tenant_id, key, most))
    rows = cur.fetchall()
    done = 0
    for row in rows:
        pid, url = (row["id"], row["url"]) if isinstance(row, dict) else row
        raw = _fetch(url)
        emb = None
        if raw:
            try:
                big, _t, w, h = _shrink(raw)
                emb = _embed_image(big)
            except ValueError:
                pass
        sv = suggest_view(emb) or "none"          # "none" = looked at, nothing to suggest (not tried again)
        cur.execute("UPDATE product_photos SET suggested_view=%s, image_embedding=%s::vector WHERE id=%s",
                    (sv if sv in VIEW_LABELS else None if sv != "none" else "none", _vec(emb), pid))
        done += 1
    return done
