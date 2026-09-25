"""
ai_design_render.py — draws the finished post images for the AI Post
Designer (2026-09-25). The AI supplies a picture (or the business's own
product photo is used) and the words; THIS file lays the headline, price,
logo and contact line on top with real fonts, so spelling and prices are
always exactly what the business typed or what My Products holds — AI image
models still garble text.

Everything is drawn on an RGBA canvas and combined with alpha_composite /
paste-with-mask. Never draw semi-transparent fills straight onto an RGB
image: PIL doesn't blend them there (the 2026-09-02 invisible-gold-text bug).

4 styles  x 4 colour sets x 4 layouts, two sizes:
  square 1080x1080 (Instagram, and the default for every network)
  wide   1200x628  (Facebook / LinkedIn / X link-style)
"""
import io
import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

FONT_DIR = os.path.join(os.path.dirname(__file__), "static", "fonts", "designer")
SIZES = {"square": (1080, 1080), "wide": (1200, 628)}
STYLES = ("bold", "elegant", "playful", "minimal")
LAYOUTS = ("right", "centre", "top", "big")
STYLE_LABELS = {"bold": "Bold", "elegant": "Elegant", "playful": "Playful", "minimal": "Minimal"}
LAYOUT_LABELS = {"right": "Photo right", "centre": "Photo centre", "top": "Photo top", "big": "Bigger photo"}

# style -> (headline font, weight, price font, weight, headline uppercase?)
_STYLE_FONTS = {
    "bold":    ("Montserrat.ttf", "Black",     "Montserrat.ttf", "ExtraBold", True),
    "elegant": ("NotoSerif.ttf",  "Bold",      "Montserrat.ttf", "SemiBold",  False),
    "playful": ("Baloo2.ttf",     "ExtraBold", "Baloo2.ttf",     "ExtraBold", False),
    "minimal": ("Inter.ttf",      "Bold",      "Inter.ttf",      "SemiBold",  False),
}
_SMALL_FONT = ("Inter.ttf", "SemiBold")
INK = "#111827"


@lru_cache(maxsize=256)
def _font(file: str, weight: str, size: int):
    f = ImageFont.truetype(os.path.join(FONT_DIR, file), size)
    try:
        for name in f.get_variation_names():
            label = name.decode() if isinstance(name, bytes) else name
            if label == weight:
                f.set_variation_by_name(name)
                break
    except Exception:
        pass  # not a variable font
    return f


def _rgb(hex_colour: str):
    h = (hex_colour or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)


def _lum(rgb):
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _on(bg_rgb):
    """Readable text colour for a background."""
    return (255, 255, 255) if _lum(bg_rgb) < 0.4 else _rgb(INK)


def palette(kit: dict, index: int) -> dict:
    """4 colour sets built from the Brand Kit's 3 colours."""
    m, a, b, k = _rgb(kit.get("color_main")), _rgb(kit.get("color_accent")), _rgb(kit.get("color_bg")), _rgb(INK)
    sets = [
        {"bg": m, "text": _on(m), "badge": a, "badge_text": _on(a), "deco": a},
        {"bg": a, "text": _on(a), "badge": m, "badge_text": _on(m), "deco": m},
        {"bg": b, "text": m if _lum(b) > 0.4 else _on(b), "badge": m, "badge_text": _on(m), "deco": a},
        {"bg": k, "text": (255, 255, 255), "badge": a, "badge_text": _on(a), "deco": a},
    ]
    return sets[index % 4]


def palette_swatches(kit: dict):
    """(left, right) hex pairs for the colour buttons."""
    out = []
    for i in range(4):
        p = palette(kit, i)
        out.append(("#%02x%02x%02x" % p["bg"], "#%02x%02x%02x" % p["badge"]))
    return out


# ── text helpers ────────────────────────────────────────────────────────────

def _wrap(draw, text, font, max_w):
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit(draw, text, file, weight, max_w, max_h, start, min_size=18, max_lines=4, spacing=1.08):
    """Largest font size at which `text` wraps into the box."""
    size = start
    while size >= min_size:
        f = _font(file, weight, size)
        lines = _wrap(draw, text, f, max_w)
        too_wide = any(draw.textlength(l, font=f) > max_w for l in lines)
        h = int(len(lines) * size * spacing)
        if len(lines) <= max_lines and h <= max_h and not too_wide:
            return f, lines, size
        size -= 2
    f = _font(file, weight, min_size)
    return f, _wrap(draw, text, f, max_w)[:max_lines], min_size


def _draw_lines(draw, lines, font, size, x, y, fill, align="left", box_w=0, spacing=1.08):
    for i, line in enumerate(lines):
        lx = x
        if align == "center":
            lx = x + (box_w - draw.textlength(line, font=font)) / 2
        draw.text((lx, y + i * size * spacing), line, font=font, fill=fill)
    return y + len(lines) * size * spacing


# ── picture helpers ─────────────────────────────────────────────────────────

def _cover(img, w, h):
    return ImageOps.fit(img.convert("RGBA"), (max(1, int(w)), max(1, int(h))), Image.LANCZOS, centering=(0.5, 0.45))


def _rounded_mask(w, h, r):
    m = Image.new("L", (int(w), int(h)), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, int(w) - 1, int(h) - 1), radius=int(r), fill=255)
    return m


def _placeholder(w, h, pal):
    """Used when there is no photo at all: a soft brand-colour pattern."""
    img = Image.new("RGBA", (int(w), int(h)), pal["bg"] + (255,))
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i in range(-int(h), int(w), 60):
        d.line([(i, 0), (i + int(h), int(h))], fill=pal["deco"] + (70,), width=18)
    return Image.alpha_composite(img, layer)


def _paste_photo(canvas, photo, box, radius=0, shadow=False):
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = x1 - x0, y1 - y0
    pic = _cover(photo, w, h)
    if shadow:
        sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle((x0 + 6, y0 + 14, x1 + 6, y1 + 14), radius=max(radius, 1), fill=(0, 0, 0, 90))
        canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(18)))
    if radius:
        canvas.paste(pic, (x0, y0), _rounded_mask(w, h, radius))
    else:
        canvas.paste(pic, (x0, y0), pic)


def _logo(canvas, kit, logo_path, pos, max_h, colour, max_w=None, pill=None):
    """Logo image if the Brand Kit has one, else the business name. `pill`
    (a background colour) puts it on a small solid tag so it stays readable
    on top of a photo."""
    x, y, anchor = pos
    max_w = max_w or canvas.width * 0.32
    tile = None
    if logo_path and os.path.exists(logo_path):
        try:
            lg = Image.open(logo_path).convert("RGBA")
            ratio = min(max_h / lg.height, max_w / lg.width)
            tile = lg.resize((max(1, int(lg.width * ratio)), max(1, int(lg.height * ratio))), Image.LANCZOS)
        except Exception:
            tile = None
    if tile is None:
        name = (kit.get("display_name") or "").upper()[:34]
        if not name:
            return
        size = int(max_h * 0.55)
        d0 = ImageDraw.Draw(canvas)
        f = _font("Montserrat.ttf", "ExtraBold", size)
        while d0.textlength(name, font=f) > max_w and size > 10:
            size -= 1
            f = _font("Montserrat.ttf", "ExtraBold", size)
        tw = int(d0.textlength(name, font=f))
        asc, desc = f.getmetrics()
        tile = Image.new("RGBA", (tw + 2, asc + desc), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((0, 0), name, font=f, fill=colour)
    if pill is not None:
        px_, py_ = int(max_h * 0.35), int(max_h * 0.2)
        wrap = Image.new("RGBA", (tile.width + px_ * 2, tile.height + py_ * 2), (0, 0, 0, 0))
        ImageDraw.Draw(wrap).rounded_rectangle((0, 0, wrap.width - 1, wrap.height - 1), radius=int(wrap.height / 2), fill=pill + (235,))
        wrap.paste(tile, (px_, py_), tile)
        tile = wrap
    px = x - tile.width if anchor.endswith("r") else x
    canvas.paste(tile, (int(px), int(y)), tile)


def _badge(canvas, text, font, x, y, fill, text_fill, pad_x, pad_y, radius, rotate=0, circle=False):
    d0 = ImageDraw.Draw(canvas)
    tw = d0.textlength(text, font=font)
    asc, desc = font.getmetrics()
    th = asc + desc
    if circle:
        dia = int(max(tw, th) + pad_x * 2)
        tile = Image.new("RGBA", (dia, dia), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        td.ellipse((0, 0, dia - 1, dia - 1), fill=fill + (255,))
        td.text(((dia - tw) / 2, (dia - th) / 2), text, font=font, fill=text_fill)
    else:
        w, h = int(tw + pad_x * 2), int(th + pad_y * 2)
        tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        td.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=fill + (255,))
        td.text((pad_x, pad_y), text, font=font, fill=text_fill)
    if rotate:
        tile = tile.rotate(rotate, expand=True, resample=Image.BICUBIC)
    canvas.paste(tile, (int(x), int(y)), tile)
    return tile.size


# ── the renderer ────────────────────────────────────────────────────────────

def render(variant: dict, kit: dict, photo_path=None, logo_path=None, size="square") -> bytes:
    """PNG bytes for one design at one size."""
    W, H = SIZES.get(size, SIZES["square"])
    style = variant.get("style") if variant.get("style") in STYLES else "bold"
    layout = variant.get("layout") if variant.get("layout") in LAYOUTS else "right"
    pal = palette(kit, int(variant.get("palette") or 0))
    hfile, hweight, pfile, pweight, upper = _STYLE_FONTS[style]
    headline = (variant.get("headline") or "").strip()
    if upper:
        headline = headline.upper()
    price = (variant.get("price_label") or "").strip()
    contact = (kit.get("contact_line") or "").strip() if variant.get("show_contact") else ""
    show_logo = variant.get("show_logo", True)

    bg = pal["bg"]
    if style == "minimal":
        bg = (255, 255, 255) if _lum(pal["bg"]) < 0.4 or pal["bg"] == _rgb(INK) else pal["bg"]
    canvas = Image.new("RGBA", (W, H), bg + (255,))
    text_col = _on(bg) if style == "minimal" else pal["text"]
    if style == "minimal" and _lum(bg) > 0.4:
        text_col = _rgb(INK)

    try:
        photo = Image.open(photo_path) if photo_path and os.path.exists(photo_path) else None
    except Exception:
        photo = None
    if photo is None:
        photo = _placeholder(W, H, palette(kit, int(variant.get("palette") or 0) + 1))

    # Playful gets soft background circles first.
    if style == "playful":
        deco = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        dd = ImageDraw.Draw(deco)
        dd.ellipse((W * 0.70, -H * 0.12, W * 1.12, H * 0.30), fill=pal["deco"] + (120,))
        dd.ellipse((-W * 0.10, H * 0.78, W * 0.20, H * 1.10), fill=pal["badge"] + (90,))
        canvas.alpha_composite(deco)

    m = int(min(W, H) * 0.06)          # outer margin
    wide = size == "wide"

    # Photo box + text box for each layout.
    if layout == "big":
        _paste_photo(canvas, photo, (0, 0, W, H))
        shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shade)
        if wide:
            for i in range(int(W * 0.62)):
                a = int(215 * (1 - i / (W * 0.62)))
                sd.line([(i, 0), (i, H)], fill=pal["bg"] + (a,))
            tbox = (m, m, W * 0.55, H - m)
        else:
            for i in range(int(H * 0.55)):
                a = int(225 * (i / (H * 0.55)))
                sd.line([(0, H * 0.45 + i), (W, H * 0.45 + i)], fill=pal["bg"] + (a,))
            tbox = (m, H * 0.56, W - m, H - m)
        canvas.alpha_composite(shade)
        text_col = pal["text"]
        align = "left"
    elif layout == "right" or (wide and layout in ("centre",)):
        split = 0.46 if not wide else 0.50
        if wide and layout == "centre":
            _paste_photo(canvas, photo, (W * 0.54, m, W - m, H - m), radius=28, shadow=True)
        else:
            _paste_photo(canvas, photo, (W * split, 0, W, H), radius=0)
        tbox = (m, m * 1.4, W * (split - 0.04), H - m)
        align = "left"
    elif layout == "top":
        if wide:
            _paste_photo(canvas, photo, (0, 0, W * 0.48, H))
            tbox = (W * 0.52, m * 1.4, W - m, H - m)
        else:
            _paste_photo(canvas, photo, (0, 0, W, H * 0.60))
            tbox = (m, H * 0.64, W - m, H - m)
        align = "left"
    else:  # centre, square
        r = int(W * 0.06) if style != "playful" else int(W * 0.30)
        _paste_photo(canvas, photo, (W * 0.17, H * 0.12, W * 0.83, H * 0.60), radius=r, shadow=True)
        tbox = (m, H * 0.64, W - m, H - m)
        align = "center"

    d = ImageDraw.Draw(canvas)
    x0, y0, x1, y1 = [int(v) for v in tbox]
    bw, bh = x1 - x0, y1 - y0
    # Measure everything below the headline first, so the whole stack fits.
    psize = int(min(W, H) * (0.058 if style != "elegant" else 0.045))
    cs = int(min(W, H) * 0.026)
    logo_in_column = show_logo and layout == "right"
    reserve_price = int(psize * (2.1 if style in ("bold", "playful") else 1.6)) if price else 0
    reserve_contact = int(cs * 2.0) if contact else 0
    reserve_logo = int(min(W, H) * 0.055 * 1.6) if logo_in_column else 0
    head_h = max(40, bh - reserve_price - reserve_contact - reserve_logo)
    start = int(min(W, H) * (0.115 if style == "bold" else 0.10))

    # Headline
    if style == "playful" and headline:
        f, lines, fs = _fit(d, headline, hfile, hweight, bw * 0.84, head_h * 0.62, start, max_lines=3)
        tile_text = "\n".join(lines)
        tmp = Image.new("RGBA", (int(bw), int(head_h)), (0, 0, 0, 0))
        td = ImageDraw.Draw(tmp)
        widest = max(td.textlength(l, font=f) for l in lines)
        pad = fs * 0.35
        box_w, box_h = widest + pad * 2, len(lines) * fs * 1.05 + pad * 1.4
        sticker = Image.new("RGBA", (int(box_w), int(box_h)), (0, 0, 0, 0))
        sd2 = ImageDraw.Draw(sticker)
        sd2.rounded_rectangle((0, 0, box_w - 1, box_h - 1), radius=int(fs * 0.4), fill=pal["badge"] + (255,))
        for i, l in enumerate(lines):
            sd2.text((pad, pad * 0.5 + i * fs * 1.05), l, font=f, fill=pal["badge_text"])
        sticker = sticker.rotate(4, expand=True, resample=Image.BICUBIC)
        sx = x0 + (bw - sticker.width) / 2 if align == "center" else x0
        canvas.paste(sticker, (int(sx), int(y0)), sticker)
        y_after = y0 + sticker.height + fs * 0.2
        del tile_text
    elif headline:
        f, lines, fs = _fit(d, headline, hfile, hweight, bw, head_h, start)
        y_after = _draw_lines(d, lines, f, fs, x0, y0, text_col, align=align, box_w=bw)
        if style == "elegant":
            lx = x0 + (bw - bw * 0.18) / 2 if align == "center" else x0
            d.line([(lx, y_after + fs * 0.25), (lx + bw * 0.18, y_after + fs * 0.25)], fill=pal["badge"], width=4)
            y_after += fs * 0.5
    else:
        y_after = y0

    # Price
    if price:
        pf = _font(pfile, pweight, psize)
        while d.textlength(price, font=pf) > bw * 0.9 and psize > 16:
            psize -= 2
            pf = _font(pfile, pweight, psize)
        py = y_after + psize * 0.45
        if style == "bold":
            tw = d.textlength(price, font=pf)
            bx = x0 + (bw - tw - psize) / 2 if align == "center" else x0
            bs = _badge(canvas, price, pf, bx, py, pal["badge"], pal["badge_text"], psize * 0.5, psize * 0.28, int(psize * 0.25))
            y_after = py + bs[1]
        elif style == "playful":
            tw = d.textlength(price, font=pf)
            bx = x0 + (bw - tw) / 2 if align == "center" else x0
            bs = _badge(canvas, price, pf, bx, py, pal["badge"], pal["badge_text"], psize * 0.55, psize * 0.3, int(psize), rotate=-5)
            y_after = py + bs[1]
        elif style == "elegant":
            spaced = price.upper()
            y_after = _draw_lines(d, [spaced], pf, psize, x0, py, pal["badge"] if _lum(bg) > 0.4 else text_col, align=align, box_w=bw)
        else:
            y_after = _draw_lines(d, [price], pf, psize, x0, py, pal["badge"] if _lum(bg) > 0.4 else text_col, align=align, box_w=bw)

    # Contact line straight under the price / headline.
    if contact:
        cf = _font(_SMALL_FONT[0], _SMALL_FONT[1], cs)
        cy = y_after + cs * 0.7
        cw = d.textlength(contact, font=cf)
        cx = x0 + (bw - cw) / 2 if align == "center" else x0
        d.text((cx, cy), contact, font=cf, fill=text_col)

    # Logo: bottom of the text column when that column is solid colour and
    # has room; otherwise a small tag in the top-right corner.
    if show_logo:
        fh = int(min(W, H) * 0.055)
        if layout == "right" and not (wide and layout == "centre"):
            _logo(canvas, kit, logo_path, (x0, H - m - fh, "l"), fh, text_col, max_w=bw)
        else:
            _logo(canvas, kit, logo_path, (W - m * 0.6, m * 0.6, "r"), fh * 0.8, pal["text"], pill=pal["bg"])

    out = io.BytesIO()
    canvas.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()
