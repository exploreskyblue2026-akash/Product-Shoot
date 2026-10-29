"""v2 pipeline: the real product (cut-out from the Product Library) is placed into an AI-generated empty scene.

The product pixels are never redrawn, so logos, text, colour and shape stay exactly as photographed.
Scenes come from a reusable pool (brand × category × shot) or are generated fresh, following the scene strategy:
  auto  – hero and premium brands fresh; other shots from the pool when a scene is available, else fresh (added to pool)
  fresh – every shot gets a new scene
  pool  – reuse whenever possible (cheapest)
  mix   – hero fresh, everything else pool
"""
import io
import re

from PIL import Image, ImageChops, ImageFilter, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .connectors import storage
from .db import LibImage, LibProduct, Product, SceneAsset, Shot, get_setting, log
from .dataset import brand_from_text, library

W, H = storage.OUT_W, storage.OUT_H

# where and how big the product sits in each shot (box as a share of the frame, base line, horizontal centre)
LAYOUT = {
    "hero":        {"w": 0.66, "h": 0.62, "base": 0.80, "cx": 0.50, "where": "in the centre of the frame, lower half"},
    "use_case":    {"w": 0.55, "h": 0.50, "base": 0.82, "cx": 0.44, "where": "slightly left of centre, resting on the surface"},
    "detail":      {"w": 0.86, "h": 0.80, "base": 0.90, "cx": 0.50, "where": "filling most of the frame"},
    "arrangement": {"w": 0.52, "h": 0.48, "base": 0.74, "cx": 0.50, "where": "in the centre, with props arranged around the edges"},
    "lifestyle":   {"w": 0.44, "h": 0.42, "base": 0.84, "cx": 0.56, "where": "in the lower centre, slightly right"},
}

CATEGORY_WORDS = [
    ("pen_fine", ("fountain", "rollerball", "ink pen")),
    ("pen_everyday", ("pen", "pencil", "ballpoint", "ball pen", "stylus", "marker")),
    ("planner", ("planner", "organiser book", "agenda")),
    ("notebook", ("notebook", "diary", "journal", "notepad", "book", "pad")),
    ("art_paint", ("paint", "pastel", "colour", "color", "watercolour", "oil", "acrylic", "crayon")),
    ("art_tools", ("brush", "easel", "palette", "sketch", "eraser", "sharpener")),
    ("camera", ("camera", "instax", "film")),
    ("tech_acc", ("cable", "charger", "tech", "electronic", "usb", "connected", "power bank", "phone")),
    ("wallet", ("wallet", "card case", "card holder", "key", "keyring", "money clip", "purse")),
    ("bag_travel", ("pouch", "bag", "backpack", "travel", "case", "organizer", "organiser", "luggage")),
    ("desk_acc", ("stapler", "desk", "mat", "matt", "tray", "holder", "clip", "tape", "scissors", "ruler")),
    ("home_decor", ("decor", "vase", "candle", "figure", "frame", "gift", "mug", "bottle")),
]


def category_for(s: Session, text: str) -> str:
    cats = library(s, "categories")
    low = (text or "").lower()
    for cid, words in CATEGORY_WORDS:
        if cid in cats and any(re.search(rf"\b{re.escape(w)}", low) for w in words):
            return cid
    return next(iter(cats))


def brand_for(s: Session, text: str) -> str:
    b = brand_from_text(s, text or "")
    return b if b and b != "_default" else "_default"


# ------------------------------------------------------------------ queue from the shoot sheet
def load_shoot_sheet(s: Session) -> dict:
    """Create studio queue items ONLY for the codes in the shoot sheet. Nothing else is queued."""
    items = get_setting(s, "shoot_list", []) or []
    if not items:
        raise ValueError("No shoot sheet loaded. Add it on the Product Library page first.")
    items = [{**it, "priority": n + 1} for n, it in enumerate(items)]
    r = load_items(s, items)
    log(s, "v2", f"Shoot sheet loaded: {r['added']} new, {r['updated']} updated, {r['waiting']} waiting, {r['missing']} not in master")
    s.commit()
    return r


def load_items(s: Session, items: list[dict]) -> dict:
    """Queue exactly these products (from the shoot sheet or a brand queue). item: key, code, shots, note, priority."""
    added = updated = waiting = missing = 0
    existing = {(p.data or {}).get("lib_key"): p for p in s.scalars(select(Product))
                if (p.data or {}).get("v2") and p.stage not in ("saved", "rejected")}
    for n, it in enumerate(items):
        key = it.get("key")
        lp = s.scalar(select(LibProduct).where(LibProduct.key == key)) if key else None
        img = s.scalar(select(LibImage).where(LibImage.key == key, LibImage.primary == True)) if key else None  # noqa: E712
        from .library import cutouts_on, refs_for
        engine = "composite" if cutouts_on(s) else "ai"
        if engine == "ai":
            q = (img.meta or {}) if img else {}
            ready = bool(img and img.cut_status != "skip" and (not q.get("flags") or q.get("override")))
        else:
            ready = bool(img and img.cut_path and img.cut_status in ("ok", "check"))
        p = existing.get(key) if key else None
        if p is None:
            p = Product(code=(lp.product_id if lp else it["code"])[:80])
            s.add(p)
            added += 1
        else:
            updated += 1
        text = " ".join(filter(None, [lp.name if lp else "", lp.category if lp else ""]))
        p.name = (lp.name if lp else it["code"])[:300]
        if it.get("brand_key"):
            p.brand_id = it["brand_key"]
        else:                                            # same brand matching as the Brands pages (tab name first)
            from .brands import brand_key_for, brand_rows
            bk = brand_key_for(lp, brand_rows(s)) if lp else "_default"
            p.brand_id = bk if bk != "_default" else brand_for(s, f"{lp.brand if lp else ''} {text}")
        p.category_id = category_for(s, text)
        p.data = {**(p.data or {}), "v2": True, "lib_key": key or "", "shots": it.get("shots") or [],
                  "note": it.get("note", ""), "priority": it.get("priority", n + 1), "brand_label": lp.brand if lp else "",
                  "cutout": img.cut_path if ready and engine == "composite" else "", "paused": False,
                  "engine": engine, "refs": refs_for(s, key) if (ready and key) else []}
        if not key:
            p.stage, p.error = "error", f"{it['code']} is not in the master sheet"
            missing += 1
        elif not ready:
            p.stage, p.error = "needs_photo", ("Photo " + " and ".join((img.meta or {}).get("flags", [])) + ": add a better one or press 'Use anyway' in the Library"
                                               if img and engine == "ai" and (img.meta or {}).get("flags") else
                                               "Waiting for its cut-out on the Library page" if img and engine == "composite"
                                               else "No photo in the Library for this code")
            waiting += 1
        else:
            p.raw_path, p.stage, p.error = img.path, ("queue" if p.stage in ("needs_photo", "error", "queue", "") else p.stage), ""
    s.commit()
    return {"added": added, "updated": updated, "waiting": waiting, "missing": missing, "total": len(items)}


# ------------------------------------------------------------------ prompts
def scene_prompt(spec: dict, kind: str) -> str:
    f = spec["fill"]
    where = LAYOUT[kind]["where"]
    return (
        f"Professional product-photography BACKGROUND PLATE for an e-commerce shot ({kind}), 4:5 vertical, photorealistic.\n"
        f"{spec['light']}\n"
        f"Shot: {kind}. Camera: {f['shot_camera']}. Depth of field: {f['shot_dof']}.\n"
        f"Scene: {f['scene']}. Surface: {f['surface']}.\n"
        f"Mood: {f['mood_name']} — palette {f['mood_palette']}; {f['mood_styling']}.\n"
        f"Brand feel: {f['brand_positioning']} (palette {f['brand_palette']}).\n"
        f"Props: {f['props']}. Keep props small and at the edges of the frame, never in the middle.\n"
        f"VERY IMPORTANT: do NOT show any product, packaging, pen, case, bag, box or the item being sold. "
        f"Leave a clean, empty, evenly lit area {where} where a product will be placed later; the surface must "
        f"continue naturally and flat through that area so an object can stand on it. Soft, diffused light from the "
        f"upper left. No people, no faces, no hands. No text, letters, logos or watermarks.\n"
        f"Avoid: {f['negatives']}"
    )


# ------------------------------------------------------------------ composite
def composite(scene: bytes, cutout: bytes, kind: str, dark: bool | None = None) -> bytes:
    """Place the cut-out on the scene with a contact shadow and a soft cast shadow (light from the upper left)."""
    bg = ImageOps.fit(Image.open(io.BytesIO(scene)).convert("RGB"), (W, H), Image.LANCZOS)
    prod = Image.open(io.BytesIO(cutout)).convert("RGBA")
    bbox = prod.split()[-1].point(lambda v: 255 if v > 12 else 0).getbbox()
    if bbox:
        prod = prod.crop(bbox)
    L = LAYOUT.get(kind, LAYOUT["hero"])
    scale = min(L["w"] * W / prod.width, L["h"] * H / prod.height)
    pw, ph = max(1, int(prod.width * scale)), max(1, int(prod.height * scale))
    prod = prod.resize((pw, ph), Image.LANCZOS)
    x = int(L["cx"] * W - pw / 2)
    y = int(L["base"] * H - ph)
    x, y = max(0, min(W - pw, x)), max(0, min(H - ph, y))
    # how bright is the spot we put the product on? (shadows weaker on dark sets)
    region = bg.crop((x, y, x + pw, y + ph)).convert("L")
    lum = sum(region.getdata()) / max(1, pw * ph) / 255
    dark = lum < 0.35 if dark is None else dark
    strength = 0.28 if dark else 0.45
    alpha = prod.split()[-1]
    # contact shadow: the product's own bottom edge (where it touches the surface), smeared a little and darkened
    band = max(4, int(ph * 0.12))
    foot = Image.new("L", (pw, ph), 0)
    foot.paste(alpha.crop((0, ph - band, pw, ph)), (0, ph - band))
    contact = Image.new("L", (W, H), 0)
    for dy in range(0, max(3, ph // 60)):
        contact.paste(foot.point(lambda v: int(v * (0.55 if not dark else 0.35))), (x, y + dy), foot)
    contact = contact.filter(ImageFilter.GaussianBlur(max(3, pw // 90)))
    # cast shadow: the silhouette flattened onto the floor, falling back-right (light from the upper left)
    flat_h = max(2, int(ph * 0.22))
    flat = alpha.resize((pw, flat_h), Image.LANCZOS)
    shear = int(pw * 0.10)
    flat = flat.transform((pw + shear, flat_h), Image.AFFINE, (1, shear / flat_h, -shear, 0, 1, 0), Image.BICUBIC)
    cast = Image.new("L", (W, H), 0)
    # anchor the flattened shadow at the row where the product meets the floor
    low = [r for r in range(ph - 1, -1, -1) if alpha.crop((0, r, pw, r + 1)).getbbox()]
    floor_y = y + (low[0] if low else ph)
    cast.paste(flat.point(lambda v: int(v * strength)), (x + int(pw * 0.02), floor_y - flat_h + int(flat_h * 0.15)))
    cast = cast.filter(ImageFilter.GaussianBlur(max(8, pw // 22)))
    shadow = ImageChops.lighter(cast, contact)
    shade = Image.new("RGB", (W, H), (20, 16, 12))
    out = Image.composite(shade, bg, shadow)
    # gentle light-matching: nudge the product towards the scene's average colour temperature (3%)
    avg = bg.resize((1, 1), Image.BOX).getpixel((0, 0))
    tint = Image.new("RGB", prod.size, avg)
    rgb = Image.blend(prod.convert("RGB"), tint, 0.03)
    out.paste(rgb, (x, y), alpha)
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()


# ------------------------------------------------------------------ scene pool
def pool_key(p: Product, kind: str) -> str:
    return f"{p.brand_id or '_default'}|{p.category_id or '-'}|{kind}".lower()


def choose_scene(s: Session, p: Product, kind: str, forced_fresh: bool) -> SceneAsset | None:
    """Return a pool scene to reuse, or None when a fresh one must be generated."""
    strategy = get_setting(s, "scene_strategy", "auto")
    fresh_brands = [b.strip().lower() for b in str(get_setting(s, "fresh_brands", "") or "").split(",") if b.strip()]
    if forced_fresh or strategy == "fresh":
        return None
    if strategy in ("auto", "mix") and kind == "hero":
        return None
    if strategy == "auto" and (p.brand_id or "").lower() in fresh_brands:
        return None
    max_reuse = int(get_setting(s, "scene_reuse", 10) or 10)
    cands = [a for a in s.scalars(select(SceneAsset).where(SceneAsset.pool_key == pool_key(p, kind),
                                                            SceneAsset.retired == False))  # noqa: E712
             if a.uses < max_reuse and p.id not in (a.used_by or [])]
    return min(cands, key=lambda a: (a.uses, a.id)) if cands else None


def requested_shots(s: Session, p: Product) -> list[int]:
    want = (p.data or {}).get("shots") or []
    if want:
        return sorted({int(x) for x in want if 1 <= int(x) <= 4})
    return [int(x) for x in (get_setting(s, "default_shots", [1]) or [1]) if 1 <= int(x) <= 4]


def make_shot(s: Session, p: Product, sh: Shot, spec: dict, prov, cutout: bytes, fresh: bool, charge) -> dict:
    """Generate (or reuse) the scene and composite the product. Returns info for the shot record."""
    from .pipeline import _model_for
    asset = choose_scene(s, p, sh.kind, fresh)
    model, size = _model_for(s, sh.kind)
    prompt = scene_prompt(spec, sh.kind)
    if asset is None:
        est = config.image_cost(model, size)
        cap = float(get_setting(s, "max_cost_product", 0.30))
        if (p.cost or 0) + est > cap:
            raise CostCap(f"Stopped at the ${cap:.2f} per-product cost limit")
        raw_scene = prov.generate_scene(prompt, model, size)
        used = getattr(prov, "last_model", None) or model
        charge(s, p, "scene", used, config.image_cost(used, size), sh)
        scene = storage.to_output(raw_scene)
        asset = SceneAsset(pool_key=pool_key(p, sh.kind), kind=sh.kind,
                           path=storage.save(f"scenes/{storage.safe(pool_key(p, sh.kind))}/{p.id}-{sh.no}-{sh.attempts}.jpg", scene))
        s.add(asset)
        s.flush()
        how = "fresh scene"
    else:
        scene = storage.read(asset.path)
        used = "scene pool"
        how = f"pool scene #{asset.id} (used {asset.uses}×)"
    asset.uses = (asset.uses or 0) + 1
    asset.used_by = list(asset.used_by or []) + [p.id]
    img = composite(scene, cutout, sh.kind)
    sh.prompt, sh.model = prompt, used
    return {"image": img, "scene_id": asset.id, "how": how}


class CostCap(Exception):
    pass


def process(s: Session, p: Product, prov, only: list[int] | None, charge) -> str:
    """Generate the requested shots of one v2 product. Returns a stop reason ('' when all went fine)."""
    from .dataset import build_prompts, live_rules
    from .pipeline import SHOT_NAMES, STOP
    cut_rel = (p.data or {}).get("cutout")
    if not cut_rel:
        return "No cut-out yet: finish it on the Library page"
    cutout = storage.read(cut_rel)
    rv = live_rules(s)
    shots = {sh.no: sh for sh in p.shots}
    for no, kind in SHOT_NAMES.items():
        if no not in shots:
            shots[no] = Shot(no=no, kind=kind)
            p.shots.append(shots[no])
    s.commit()
    notes = {sh.no: sh.note for sh in shots.values() if sh.note}
    specs = {x["no"]: x for x in build_prompts(s, p, {}, rv, notes)}
    want = only or [n for n in requested_shots(s, p) if not shots[n].image_path or shots[n].status in ("wait", "fail")]
    for no in sorted(want):
        if STOP.is_set():
            return "Stopped by you"
        sh = shots[no]
        sh.status, sh.rule_version, sh.qc = "gen", rv.version, {}
        sh.attempts = (sh.attempts or 0) + 1
        s.commit()
        try:
            info = make_shot(s, p, sh, specs[no], prov, cutout, fresh=bool(only), charge=charge)
        except CostCap as e:
            sh.status = "wait"
            s.commit()
            return str(e)
        rel = storage.save(f"shots/{storage.safe(p.code)}/{no}-{sh.kind}-v2-a{sh.attempts}.jpg", info["image"])
        sh.image_path = rel
        sh.qc = {"checks": {"product_exact": True}, "reasons": {}, "v2": {"scene": info["scene_id"], "how": info["how"]}}
        sh.status = "pass"
        sh.note = ""
        s.commit()
    return ""


def retire_scene(s: Session, scene_id: int):
    a = s.get(SceneAsset, scene_id)
    if a:
        a.retired = True
        s.commit()
