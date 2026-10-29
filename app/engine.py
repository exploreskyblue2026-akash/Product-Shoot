"""Accuracy engine: keep the real product, change only what is around it.

Per shot the tool picks a method (see `plan`):
  edit       - the real photo is placed on a 4:5 canvas and the AI edits ONLY the background, light and shadow;
               afterwards the original product pixels are put back (product = exact).
  edit_zoom  - same, product placed smaller so the AI builds a wider scene around it (lifestyle).
  crop       - close-up cropped from the real photo and sharpened, no AI at all (detail).
  fresh      - full generation from the Product Kit + memory, for poses/angles no photo has; strict QC.

The product outline comes from the photo's plain background (catalogue / brand / studio photos), so no extra AI or
heavy model is needed. When the background is not plain (product already in a scene), the method falls back to fresh.
"""
import io

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

W, H = 1200, 1500

LAYOUT = {                      # product box as share of the canvas, base line, horizontal centre
    "hero":      {"w": 0.64, "h": 0.66, "base": 0.86, "cx": 0.50},
    "use_case":  {"w": 0.56, "h": 0.56, "base": 0.84, "cx": 0.46},
    "lifestyle": {"w": 0.40, "h": 0.40, "base": 0.80, "cx": 0.52},
    "detail":    {"w": 0.80, "h": 0.80, "base": 0.92, "cx": 0.50},
}

DEFAULT_METHOD = {"hero": "edit", "detail": "crop", "use_case": "edit", "lifestyle": "edit_zoom"}
ANGLE_FOR = {"hero": ("front", "angle", "side"), "detail": ("detail", "front", "angle"),
             "use_case": ("angle", "side", "in_use", "front"), "lifestyle": ("angle", "side", "front")}


def _img(b) -> Image.Image:
    im = b if isinstance(b, Image.Image) else Image.open(io.BytesIO(b))
    return ImageOps.exif_transpose(im).convert("RGB")


def _jpeg(im: Image.Image, q: int = 90) -> bytes:
    """Lossless hand-over: the final JPEG is written once, by storage.to_output."""
    out = io.BytesIO()
    im.convert("RGB").save(out, "PNG")
    return out.getvalue()


# ------------------------------------------------------------------ product outline from a plain background
def background_colour(im: Image.Image) -> tuple[tuple, float]:
    """Median colour of a thin border strip + how uniform it is (lower = plainer)."""
    w, h = im.size
    b = max(2, min(w, h) // 50)
    strips = [im.crop((0, 0, w, b)), im.crop((0, h - b, w, h)), im.crop((0, 0, b, h)), im.crop((w - b, 0, w, h))]
    px = [p for s in strips for p in s.resize((max(1, s.width // 4), max(1, s.height // 4))).getdata()]
    med = tuple(sorted(c[i] for c in px)[len(px) // 2] for i in range(3))
    spread = sum(abs(c[0] - med[0]) + abs(c[1] - med[1]) + abs(c[2] - med[2]) for c in px) / (3 * len(px))
    return med, spread


def product_mask(im: Image.Image, tol: int = 26) -> tuple[Image.Image | None, str]:
    """White = product. Background = everything connected to the border that looks like the border colour.
    Returns (mask, '') or (None, reason) when the photo has no plain background."""
    im = im.convert("RGB")
    bg, spread = background_colour(im)
    if spread > 18:
        return None, "background is not plain"
    small = im.copy()
    small.thumbnail((700, 700))
    diff = ImageChops.difference(small, Image.new("RGB", small.size, bg)).convert("L")
    likebg = diff.point(lambda v: 255 if v <= tol else 0)
    likebg = likebg.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    w, h = likebg.size
    seeds = [(x, y) for x in range(0, w, max(1, w // 24)) for y in (0, h - 1)] + \
            [(x, y) for y in range(0, h, max(1, h // 24)) for x in (0, w - 1)]
    for sx, sy in seeds:
        if likebg.getpixel((sx, sy)) == 255:
            ImageDraw.floodfill(likebg, (sx, sy), 128)
    mask = likebg.point(lambda v: 0 if v == 128 else 255)
    mask = mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))       # close pin holes
    mask = _main_parts(mask)                                                            # drop captions, specks
    share = sum(1 for v in mask.getdata() if v) / (w * h)
    if share < 0.02:
        return None, "product not found on the background"
    if share > 0.96:
        return None, "product fills the whole photo"
    return mask.resize(im.size, Image.LANCZOS).point(lambda v: 255 if v > 127 else 0), ""


def _main_parts(mask: Image.Image) -> Image.Image:
    """Keep the product: the largest connected shape and any shape at least 15% of its size (a lid, a second item);
    drop captions, price stickers and specks that sit apart from it."""
    g = mask.copy()
    g.thumbnail((180, 180))
    w, h = g.size
    px = g.load()
    seen = [[False] * w for _ in range(h)]
    comps = []
    for y in range(h):
        for x in range(w):
            if px[x, y] > 127 and not seen[y][x]:
                stack, pts = [(x, y)], []
                seen[y][x] = True
                while stack:
                    cx, cy = stack.pop()
                    pts.append((cx, cy))
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if 0 <= nx < w and 0 <= ny < h and not seen[ny][nx] and px[nx, ny] > 127:
                            seen[ny][nx] = True
                            stack.append((nx, ny))
                comps.append(pts)
    if len(comps) <= 1:
        return mask
    big = max(len(c) for c in comps)
    drop = Image.new("L", (w, h), 0)
    dpx = drop.load()
    for c in comps:
        if len(c) < big * 0.15:
            for x, y in c:
                dpx[x, y] = 255
    k = max(5, (mask.width // w) * 2 + 1) | 1                   # dilate by about one grid cell at full size
    drop = drop.resize(mask.size, Image.NEAREST).filter(ImageFilter.MaxFilter(min(k, 21)))
    out = ImageChops.subtract(mask, drop)
    return out.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))   # wipe thin leftovers


# ------------------------------------------------------------------ canvas
def place(photo: bytes, kind: str) -> dict:
    """Real photo on a 4:5 canvas, product sized and placed for the shot. Returns canvas, mask, bg, or reason."""
    im = _img(photo)
    mask, why = product_mask(im)
    if mask is None:
        return {"ok": False, "reason": why}
    box = mask.getbbox()
    prod, pmask = im.crop(box), mask.crop(box)
    L = LAYOUT.get(kind, LAYOUT["hero"])
    scale = min(L["w"] * W / prod.width, L["h"] * H / prod.height)
    pw, ph = max(1, round(prod.width * scale)), max(1, round(prod.height * scale))
    prod, pmask = prod.resize((pw, ph), Image.LANCZOS), pmask.resize((pw, ph), Image.LANCZOS)
    x = max(0, min(W - pw, round(L["cx"] * W - pw / 2)))
    y = max(0, min(H - ph, round(L["base"] * H - ph)))
    bg, _ = background_colour(im)
    canvas = Image.new("RGB", (W, H), bg)
    canvas.paste(prod, (x, y), pmask)
    cmask = Image.new("L", (W, H), 0)
    cmask.paste(pmask, (x, y))
    upscale = scale > 1.0
    return {"ok": True, "canvas": canvas, "mask": cmask, "bg": bg, "box": (x, y, x + pw, y + ph),
            "upscaled": round(scale, 2) if upscale else 1.0}


def restore(generated: bytes, placed: dict) -> tuple[bytes, dict]:
    """Put the original product pixels back if the AI kept the product where it was.
    Returns the final JPEG and info {restored, drift}."""
    gen = _img(generated).resize((W, H), Image.LANCZOS)
    canvas, mask = placed["canvas"], placed["mask"]
    # how much did the product region change? (grey, small, inside the product only)
    sm = (300, 375)
    a = gen.resize(sm).convert("L")
    b = canvas.resize(sm).convert("L")
    m = mask.resize(sm).point(lambda v: 255 if v > 200 else 0)
    d = ImageChops.difference(a, b)
    stat = ImageStat.Stat(d, m)
    drift = round(stat.mean[0], 1) if stat.count[0] else 255.0
    if drift > 45:                                   # the model moved or redrew the product: keep its image, QC decides
        return _jpeg(gen, 92), {"restored": False, "drift": drift}
    soft = mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.6))
    out = Image.composite(canvas, gen, soft)
    return _jpeg(out, 92), {"restored": True, "drift": drift}


def detail_crop(photo: bytes, min_side: int = 520) -> tuple[bytes | None, str]:
    """Close-up from the real photo: the 4:5 window with the most detail (logo, text, texture), sharpened.
    None when the photo is too small for a convincing close-up."""
    im = _img(photo)
    mask, _ = product_mask(im)
    box = mask.getbbox() if mask is not None else (0, 0, im.width, im.height)
    bw, bh = box[2] - box[0], box[3] - box[1]
    ch = int(min(bh * 0.62, bw * 0.62 * 5 / 4))
    cw = int(ch * 4 / 5)
    if ch < min_side:
        return None, f"photo too small for a close-up ({im.width}×{im.height})"
    edges = im.convert("L").filter(ImageFilter.FIND_EDGES)
    best, best_e = None, -1.0
    steps = 6
    for i in range(steps + 1):
        for j in range(steps + 1):
            x = box[0] + (bw - cw) * i // steps if bw > cw else box[0] + (bw - cw) // 2
            y = box[1] + (bh - ch) * j // steps if bh > ch else box[1] + (bh - ch) // 2
            x, y = max(0, min(im.width - cw, x)), max(0, min(im.height - ch, y))
            e = ImageStat.Stat(edges.crop((x, y, x + cw, y + ch)).resize((40, 50))).mean[0]
            if e > best_e:
                best, best_e = (x, y), e
    x, y = best
    crop = im.crop((x, y, x + cw, y + ch)).resize((W, H), Image.LANCZOS)
    crop = crop.filter(ImageFilter.UnsharpMask(radius=1.6, percent=70, threshold=2))
    return _jpeg(crop, 92), ""


# ------------------------------------------------------------------ planning + prompts
def plan(kind: str, angles: dict, card: dict | None = None) -> tuple[str, str]:
    """Pick the method for one shot. angles: {angle: rel path}. card: the brand's shot card (method + pose)."""
    card = card or {}
    method = card.get("method") or "auto"
    pick = next((angles[a] for a in ANGLE_FOR.get(kind, ("front",)) if a in angles), None) or next(iter(angles.values()), None)
    if method == "fresh" or (method == "auto" and card.get("pose") and kind in ("use_case", "lifestyle")):
        return "fresh", pick
    if method == "edit":
        return ("edit_zoom" if kind == "lifestyle" else "edit"), pick
    return DEFAULT_METHOD.get(kind, "edit"), pick


def edit_prompt(spec: dict, kind: str, card: dict | None = None) -> str:
    """Short, ordered instructions for an EDIT of the real photo (product kept, surroundings made)."""
    f, card = spec.get("fill", {}), card or {}
    scene = card.get("scene") or f.get("scene", "clean studio set")
    surface = card.get("background") or f.get("surface", "soft warm white")
    props = card.get("props") or f.get("props", "none")
    lines = [
        "EDIT the photo above. The product in it is real and must stay EXACTLY as it is: same position, size, angle, "
        "shape, proportions, colours, logo and printed text. Do not redraw, move, rotate, resize or restyle it.",
        f"Replace only the plain background with: {scene}. Surface: {surface}.",
        f"Light: {spec.get('light', 'soft diffused light from the upper left')} Add a soft, realistic contact shadow "
        "under the product that matches this light.",
        f"Props: {props}. Keep props smaller than the product, at the edges, never covering it.",
        "Photorealistic e-commerce product photo, 4:5, sharp, clean, premium. No text, captions, watermarks or logos "
        "that are not on the product.",
    ]
    if kind == "use_case":
        lines.append(card.get("pose") and f"Show it in use: {card['pose']}. Hands may appear but must never cover the "
                     "product's logo or text." or "Show the place where it is used; the product stays the clear hero.")
    if kind == "lifestyle":
        lines.append("Build a wider, styled scene around the product; it may sit smaller in the frame but stays "
                     "unchanged and clearly visible.")
    if card.get("extra"):
        lines.append(f"Brand note: {card['extra']}")
    mem = (f.get("memory_text") or "").strip()
    if mem:
        lines.append("Product facts (must stay true): " + mem)
    return "\n".join(x for x in lines if x)


ANGLE_PROMPT = {
    "angle": "a three-quarter view, rotated about 45 degrees to the right, camera slightly above",
    "side": "a straight side view (profile)",
    "top": "a top-down view, camera directly above",
    "back": "the back view",
}


def kit_prompt(angle: str, facts: str = "") -> str:
    return (f"Studio packshot of EXACTLY the same product as in the reference photos, shown from {ANGLE_PROMPT.get(angle, angle)}. "
            "Plain pure white seamless background, soft even light, the whole product in frame with a clear margin. "
            "Same shape, proportions, colours, materials, logo and printed text: change only the viewing angle. "
            "Do not invent text or parts that are not visible. No props, no hands, no shadows other than a faint "
            "contact shadow." + (f"\nProduct facts: {facts}" if facts else ""))
