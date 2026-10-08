"""
Email outreach helpers (2026-10-08) — shared by every business's Email Campaigns:

- render_designed_email(): the branded layout (business logo, brand colour,
  sign-off, footer address + unsubscribe) around the Template Builder fields,
  with inline styles so it survives Gmail/Outlook.
- Name filling: {{First Name}}, {{Business Name}} … resolved per recipient from
  the CRM (lead first, then contact), with a safe fallback ("there").
- Tracking: every link is swapped for /e/c/<token>/<n> and an open pixel added;
  the click/open routes in portal_routes.py log to email_events, which the
  lead's history reads.
- make_video_poster(): turns a video link (+ optional picture) into a still
  with a play button, since email can't play video.

Nothing here is specific to one business.
"""
import hashlib
import html as _html
import io
import os
import re
import secrets

import requests

PORTAL_BASE = "https://portal.phixtra.com"
DEFAULT_BRAND = "#0B1D40"
_FONT = "Inter,'Segoe UI',Arial,sans-serif"
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static", "uploads", "email_campaign_images")


# ── Brand settings ──────────────────────────────────────────────────────────

def get_brand(cur, tenant_id: int) -> dict:
    cols = ("logo_url", "brand_color", "footer_address", "footer_note", "signoff")
    cur.execute(f"SELECT {', '.join(cols)} FROM email_brand_settings WHERE tenant_id=%s", (tenant_id,))
    row = cur.fetchone()
    if not row:
        return {}
    if not isinstance(row, dict):
        row = dict(zip(cols, row))
    return {k: v for k, v in row.items() if v}


def safe_color(value: str | None) -> str:
    v = (value or "").strip()
    return v if re.fullmatch(r"#[0-9A-Fa-f]{6}", v) else DEFAULT_BRAND


# ── Body styling (Quill output → email-safe inline styles) ─────────────────

def _add_style(tag_html: str, style: str) -> str:
    if re.search(r'\sstyle\s*=\s*"', tag_html, flags=re.I):
        return re.sub(r'(\sstyle\s*=\s*")', lambda m: m.group(1) + style, tag_html, count=1, flags=re.I)
    return tag_html[:-1] + f' style="{style}">' if not tag_html.endswith("/>") else tag_html[:-2] + f' style="{style}"/>'


def style_body(body_html: str, brand_color: str) -> str:
    tint = "#9DB8E8" if brand_color.upper() == DEFAULT_BRAND else brand_color
    rules = {
        "p": "margin:0 0 16px;",
        "blockquote": f"margin:0 0 8px;padding:2px 0 2px 14px;border-left:3px solid {tint};color:{brand_color};",
        "img": "display:block;max-width:100%;height:auto;border:0;border-radius:12px;margin:8px auto 20px;",
        "ul": "margin:0 0 16px;padding-left:22px;",
        "ol": "margin:0 0 16px;padding-left:22px;",
        "li": "margin:0 0 6px;",
        "h1": f"margin:0 0 14px;font-size:24px;line-height:1.3;color:{brand_color};",
        "h2": f"margin:0 0 12px;font-size:20px;line-height:1.3;color:{brand_color};",
        "h3": f"margin:0 0 10px;font-size:17px;line-height:1.3;color:{brand_color};",
        "a": f"color:{brand_color};",
    }
    out = body_html or ""
    for tag, style in rules.items():
        out = re.sub(rf"<{tag}(\s[^>]*)?/?>", lambda m: _add_style(m.group(0), style), out, flags=re.I)
    # Quill writes a blank line as <p><br></p>. Every paragraph already has a
    # gap under it, so blank lines are dropped rather than doubling the gap.
    out = re.sub(r"<p[^>]*>\s*<br\s*/?>\s*</p>", "", out)
    return out


# ── Layout ──────────────────────────────────────────────────────────────────

def render_designed_email(*, hero_heading, body_html, cta_text, cta_url, image_url, from_name,
                          brand=None, video_url=None, video_poster_url=None, signoff=None) -> str:
    """One branded HTML email. Leaves {{UNSUBSCRIBE_URL}} for the send loop."""
    brand = brand or {}
    color = safe_color(brand.get("brand_color"))
    esc = _html.escape
    name = from_name or "this business"

    if brand.get("logo_url"):
        head = (f'<img src="{esc(brand["logo_url"])}" alt="{esc(name)}" width="130" '
                f'style="display:block;width:130px;max-width:130px;height:auto;border:0;">')
    else:
        head = f'<div style="font-family:{_FONT};font-size:18px;font-weight:700;color:{color};">{esc(name)}</div>'

    parts = []
    if hero_heading:
        parts.append(f'<h1 style="margin:0 0 20px;font-family:{_FONT};font-size:24px;line-height:1.3;'
                     f'color:{color};font-weight:800;">{esc(hero_heading)}</h1>')
    if image_url:
        parts.append(f'<img src="{esc(image_url)}" alt="" style="display:block;max-width:100%;height:auto;'
                     f'border-radius:12px;margin:0 0 24px;border:0;">')
    video = ""
    if video_url and video_poster_url:
        video = (f'<a href="{esc(video_url)}" style="display:block;margin:4px 0 20px;text-decoration:none;">'
                 f'<img src="{esc(video_poster_url)}" alt="Play the video" width="528" '
                 f'style="display:block;width:100%;max-width:528px;height:auto;border:0;border-radius:12px;"></a>')
    body = style_body(body_html, color)
    # A {{Video}} line in the message puts the video there; otherwise it goes
    # under the message. With no video set, the marker line is just removed.
    marker = re.compile(r"<p[^>]*>\s*\{\{\s*video\s*\}\}\s*</p>|\{\{\s*video\s*\}\}", re.I)
    placed = bool(marker.search(body))
    body = marker.sub(lambda m: video, body, count=1)
    body = marker.sub("", body)
    parts.append(f'<div style="font-family:{_FONT};font-size:16px;line-height:1.6;color:#111E2D;">{body}</div>')
    if video and not placed:
        parts.append(video)
    if cta_text and cta_url:
        parts.append(
            '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:16px 0 8px;"><tr>'
            f'<td style="border-radius:10px;background:{color};">'
            f'<a href="{esc(cta_url)}" style="display:inline-block;padding:14px 30px;font-family:{_FONT};'
            f'font-size:15px;font-weight:700;color:#ffffff;text-decoration:none;">{esc(cta_text)}</a>'
            '</td></tr></table>')
    if signoff:
        lines = [esc(l) for l in signoff.strip().splitlines() if l.strip()]
        if lines:
            first, rest = lines[0], lines[1:]
            parts.append(f'<div style="padding-top:18px;font-family:{_FONT};font-size:15px;line-height:1.5;color:#111E2D;">'
                         f'<p style="margin:0;font-weight:600;">{first}</p>'
                         + "".join(f'<p style="margin:0;color:#5A6B85;font-size:14px;">{l}</p>' for l in rest)
                         + '</div>')

    # Why they're getting it — each business words this itself (a newsletter
    # to customers and a first-contact email need different lines).
    note = (brand.get("footer_note") or f"You're receiving this because you're a contact of {name}.").strip()
    address = brand.get("footer_address")
    footer_addr = f'{esc(address)}<br>' if address else ''
    return f'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F3F5F9;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F3F5F9;padding:24px 12px;">
<tr><td align="center">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:14px;border:1px solid #E1E7F0;">
    <tr><td style="padding:26px 36px 6px;">{head}</td></tr>
    <tr><td style="padding:18px 36px 28px;">{"".join(parts)}</td></tr>
  </table>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;">
    <tr><td style="padding:18px 36px;font-family:{_FONT};font-size:12px;line-height:1.6;color:#5A6B85;text-align:center;">
      {footer_addr}{esc(note)}
      <a href="{{{{UNSUBSCRIBE_URL}}}}" style="color:#5A6B85;text-decoration:underline;">Unsubscribe</a>
    </td></tr>
  </table>
</td></tr></table>
</body></html>'''


# ── Name filling ────────────────────────────────────────────────────────────

MERGE_FIELDS = [
    ("First Name", "there"),
    ("Last Name", ""),
    ("Full Name", "there"),
    ("Business Name", "your business"),
    ("Email", ""),
]
_ALIASES = {"firstname": "first", "first": "first", "lastname": "last", "fullname": "full", "name": "full",
            "contactname": "full", "businessname": "business", "company": "business",
            "companyname": "business", "business": "business", "email": "email"}
_DEFAULTS = {"first": "there", "last": "", "full": "there", "business": "your business", "email": ""}
_TOKEN = re.compile(r"\{\{\s*([A-Za-z][A-Za-z _]*?)\s*(?:\|\s*([^{}]*?)\s*)?\}\}")
_TITLES = {"mr", "mrs", "ms", "miss", "dr", "chief", "engr", "prof", "sir", "madam", "alhaji", "alhaja", "pastor"}


def _split_name(full: str):
    words = [w for w in re.split(r"\s+", (full or "").strip()) if w]
    while words and words[0].lower().rstrip(".") in _TITLES:
        words = words[1:]
    if not words:
        return "", ""
    first = words[0]
    if first.isupper() or first.islower():
        first = first.capitalize()
    return first, " ".join(words[1:])


def recipient_context(cur, tenant_id: int, email: str) -> dict:
    """CRM details for one address: the newest open lead with that email,
    else the contact. Returns ids too, so events land on the right record."""
    ctx = {"email": email, "lead_id": None, "contact_id": None}
    cur.execute("""
        SELECT l.id, l.contact_person, l.customer_name, l.wa_contact_id, c.name AS company_name
        FROM merchant_pipeline_leads l LEFT JOIN crm_companies c ON c.id = l.company_id
        WHERE l.tenant_id=%s AND lower(l.email)=lower(%s)
        ORDER BY (l.dropped_at IS NULL) DESC, l.updated_at DESC NULLS LAST LIMIT 1""", (tenant_id, email))
    lead = cur.fetchone()
    if lead:
        lead = lead if isinstance(lead, dict) else dict(zip(("id", "contact_person", "customer_name", "wa_contact_id", "company_name"), lead))
        ctx.update(lead_id=lead["id"], contact_id=lead["wa_contact_id"],
                   full=(lead["contact_person"] or "").strip(),
                   business=(lead["company_name"] or lead["customer_name"] or "").strip())
    else:
        cur.execute("""SELECT id, display_name FROM wa_contacts WHERE tenant_id=%s AND lower(email)=lower(%s)
                       ORDER BY id DESC LIMIT 1""", (tenant_id, email))
        c = cur.fetchone()
        if c:
            c = c if isinstance(c, dict) else dict(zip(("id", "display_name"), c))
            ctx.update(contact_id=c["id"], full=(c["display_name"] or "").strip())
    # A "full name" that is really the email address or a phone number isn't a name.
    full = ctx.get("full") or ""
    if "@" in full or re.fullmatch(r"[+\d\s()-]+", full or "x"):
        full = ""
    ctx["first"], ctx["last"] = _split_name(full)
    ctx["full"] = full if ctx["first"] else ""
    return ctx


def apply_merge(text: str, ctx: dict, as_html: bool) -> str:
    def sub(m):
        key = _ALIASES.get(re.sub(r"[\s_]", "", m.group(1)).lower())
        if not key:
            return m.group(0)
        val = (ctx.get(key) or "").strip()
        if not val:
            val = m.group(2) if m.group(2) is not None else _DEFAULTS[key]
        return _html.escape(val) if as_html else val
    return _TOKEN.sub(sub, text or "")


# ── Tracking ────────────────────────────────────────────────────────────────

_HREF = re.compile(r'href\s*=\s*"([^"]*)"', re.I)


def trackable_links(html_body: str) -> list:
    return [u for u in _HREF.findall(html_body or "")
            if u.startswith(("http://", "https://")) and "UNSUBSCRIBE_URL" not in u]


def add_tracking(html_body: str, token: str) -> str:
    links = trackable_links(html_body)
    counter = {"i": 0}

    def sub(m):
        url = m.group(1)
        if url.startswith(("http://", "https://")) and "UNSUBSCRIBE_URL" not in url:
            i = counter["i"]; counter["i"] += 1
            if i < len(links):
                return f'href="{PORTAL_BASE}/e/c/{token}/{i}"'
        return m.group(0)
    out = _HREF.sub(sub, html_body)
    pixel = f'<img src="{PORTAL_BASE}/e/o/{token}.gif" width="1" height="1" alt="" style="border:0;width:1px;height:1px;">'
    return re.sub(r"</body>", pixel + "</body>", out, count=1, flags=re.I) if re.search(r"</body>", out, re.I) else out + pixel


def new_token() -> str:
    return secrets.token_urlsafe(18)


def log_event(cur, tenant_id, kind, *, campaign_id=None, recipient_id=None, lead_id=None,
              contact_id=None, email=None, detail=None, actor=None):
    cur.execute("""INSERT INTO email_events (tenant_id, campaign_id, recipient_id, lead_id, contact_id, email, kind, detail, actor)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (tenant_id, campaign_id, recipient_id, lead_id, contact_id, email, kind, detail, actor))


# ── Video play picture ──────────────────────────────────────────────────────

def _youtube_id(url: str):
    m = re.search(r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{6,})", url or "")
    return m.group(1) if m else None


def _load_image(url: str):
    from PIL import Image
    if url.startswith(PORTAL_BASE + "/static/"):
        path = os.path.join(os.path.dirname(__file__), url[len(PORTAL_BASE) + 1:])
        return Image.open(path)
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content))


def make_video_poster(video_url: str, picture_url: str | None, brand_color: str) -> str | None:
    """A 1120x630 still with a play button, saved under static uploads.
    Picture: the one the business uploaded, else the YouTube thumbnail, else
    a plain brand-colour card. Returns its public URL (cached by inputs)."""
    from PIL import Image, ImageDraw, ImageFont
    if not video_url:
        return None
    color = safe_color(brand_color)
    key = hashlib.sha1(f"{video_url}|{picture_url}|{color}|v2".encode()).hexdigest()[:20]
    name = f"poster_{key}.jpg"
    path = os.path.join(_STATIC_DIR, name)
    if not os.path.exists(path):
        os.makedirs(_STATIC_DIR, exist_ok=True)
        base = None
        src = picture_url or (f"https://img.youtube.com/vi/{_youtube_id(video_url)}/hqdefault.jpg"
                              if _youtube_id(video_url) else None)
        if src:
            try:
                base = _load_image(src).convert("RGB")
            except Exception as e:
                print("⚠️ video poster picture load failed:", e)
        W, H = 1120, 630
        if base is None:
            base = Image.new("RGB", (W, H), color)
        else:  # cover-crop to 16:9
            r = max(W / base.width, H / base.height)
            base = base.resize((int(base.width * r) + 1, int(base.height * r) + 1))
            left, top = (base.width - W) // 2, (base.height - H) // 2
            base = base.crop((left, top, left + W, top + H))
        d = ImageDraw.Draw(base, "RGBA")
        # Play button in the bottom-right corner, so it never covers words
        # in the middle of the business's own picture.
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        rad, margin = 46, 40
        cx, cy = W - margin - rad, H - margin - rad - 44
        d.ellipse((cx - rad - 5, cy - rad - 5, cx + rad + 5, cy + rad + 5), fill=(0, 0, 0, 60))
        d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=(255, 255, 255, 240))
        d.polygon([(cx - 14, cy - 24), (cx - 14, cy + 24), (cx + 26, cy)], fill=rgb + (255,))
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
            label = "Watch the video"
            tw = d.textlength(label, font=font)
            right = W - margin
            d.rounded_rectangle((right - tw - 32, H - margin - 36, right, H - margin), radius=18,
                                fill=(255, 255, 255, 235))
            d.text((right - tw / 2 - 16, H - margin - 18), label, fill=rgb, font=font, anchor="mm")
        except Exception:
            pass
        base.save(path, "JPEG", quality=88)
    return f"{PORTAL_BASE}/static/uploads/email_campaign_images/{name}"
