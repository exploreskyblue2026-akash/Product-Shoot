"""Product analysis + shot planning (one call to the best model), and the prompt renderer.

Flow:  verified photos of ONE product code + master-sheet row + brand rules + shot rules
       -> best vision model (one call) -> plan JSON:
            photos (keep / discard + angle), identity, appearance, size, use, category,
            hero_features, risks, must_keep, and a plan for each of the 4 shots
       -> stored on the library product (data["plan"]); wrong photos go to Discarded
       -> at generation time each shot's prompt = STATIC rules + this shot's plan, rendered as labelled sentences

Only the product's own photos are used. Nothing is searched for on the web.
"""
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import storage
from .db import LibImage, LibProduct, Setting, get_setting, log

SHOTS = ("hero", "detail", "use_case", "lifestyle")
SHOT_TITLE = {"hero": "1 · HERO", "detail": "2 · DETAIL", "use_case": "3 · USE CASE", "lifestyle": "4 · LIFESTYLE"}

DEFAULT_STATIC = """Listing image, 4:5 vertical, 1200 x 1500 px.
Soft, diffused studio light only: no direct sun, no hard light, no harsh or sharp shadows, no glare, no hot spots.
Never show studio equipment: no lights, softboxes, stands or reflectors anywhere in the frame. The light looks studio-made, the equipment is never seen.
Real physics: every object rests on a surface or is held naturally, with a real contact shadow. Nothing floats, hovers or sits at an impossible tilt.
The product never changes: same shape, proportions, colours, materials, finish, pattern and its placement, every printed word, logo, hardware, item count and order as in the real photos.
Show only what the photos show: never invent a side, an inside, a part or a feature that is not visible in the photos.
Real-world scale: product and props at true relative size.
The product is the hero: the sharpest, brightest and most important element, never cropped, at least 8% margin.
Every shot has related props (the number varies by shot); never the bare product alone.
Props are plain, unbranded, real everyday objects: no logos, no brand names, no text, no other products of any brand, no invented branded items, no logo stickers. Props never touch or cover the product's logo or print.
No text, captions, watermarks or graphics anywhere except what is printed on the product.
Hands, when used: natural, correct anatomy (five fingers, real nails), clean, age-appropriate for the user; hands never cover the logo or print.
Colour truth: neutral white balance; the product's colours match the real photos with no colour cast.
The 4 shots look like one photoshoot: same brand look and colour family.
Clean, uncluttered backgrounds with gentle depth of field in scenes.
Photographic realism: a real camera photograph, never a 3D render, illustration or obvious AI look."""

SCHEMA = {
    "photos": [{"index": 0, "is_this_product": True, "usable": True, "angle": "front|angle|side|back|top|detail|in_use|packaging|group",
                "reason_if_discarded": ""}],
    "identity": {"type": "", "form": "", "parts": [""], "item_count": 1, "states": "e.g. closed; can open"},
    "appearance": {"colours": [{"name": "", "hex": "#", "where": ""}], "materials": [""], "finish": "",
                   "pattern": "what it is and where it sits", "printed_text": [{"text": "exact", "where": "", "style": ""}],
                   "logo": {"text": "", "form": "printed|embossed|patch|label", "where": ""}, "hardware": [""],
                   "order_of_items": "for sets: exact order, else empty"},
    "size": {"dimensions": "", "estimated": True, "scale_hint": "e.g. fits in one hand"},
    "use": {"purpose": "", "user": "e.g. kids 6-12", "how_used": "", "hands": "child|adult|none", "goes_with": [""]},
    "category": {"name": "", "environment": "best setting", "prop_types": [""], "mood": ""},
    "hero_features": ["1-3 things a buyer must see, most important first"],
    "risks": ["what an image AI is likely to get wrong"],
    "must_keep": ["short list of things that must never change"],
    "shots": {k: {"goal": "", "scene": "", "surface": "", "camera": "angle + lens feel", "composition": "placement + % of frame",
                  "props": ["plain, unbranded"], "hands": "none or who/how", "product_state": "", "light": "", "notes": ""}
              for k in SHOTS},
    "review_needed": False,
    "review_reason": "",
}

INSTRUCTIONS = """You are the art director and product analyst of an in-house e-commerce studio. Study ONLY the photos above
(they are all filed under one product code) and the facts given. Do not use any other source, do not guess from the web.

Do this in order and return ONE JSON object exactly in the schema at the end:
1. PHOTO CHECK - for every photo (index 0..n-1, in the order given): is it THIS product (same code / name / colourway)? Is it
   usable as a product reference (not a catalogue page, not a group of different products, not blurry or tiny)? Its angle.
   Mark wrong or unusable photos with a short reason. Be strict: a different colourway or design is NOT this product.
2. IDENTITY - type, form, parts, item count, states.
3. EXACT APPEARANCE - colours with hex and where they are, materials, finish, pattern and its placement, EVERY printed
   word exactly as written with its position, the logo (text, form, position), hardware, and the exact order for sets.
4. SIZE - use any measurement visible in a photo or given in the facts; otherwise estimate from the product type and mark
   "estimated": true.
5. USE & USER - purpose, who uses it (age group), how it is held or used, which hands (child / adult / none), what goes with it.
6. CATEGORY & ENVIRONMENT - category, the best real setting to shoot it (e.g. kid's study desk, office desk, art table),
   matching PROP TYPES (plain, generic, unbranded) and mood.
7. HERO FEATURES - the 1-3 things a buyer must see. RISKS - what an image generator would likely get wrong. MUST KEEP.
8. SHOT PLANS - plan the 4 listing shots for THIS product, following the studio rules and shot rules below and the brand
   rules. Every shot has props (hero: {hero_props}). The DETAIL shot is NOT a plain zoom: show the most important hero
   feature in a way that explains it (e.g. a fountain-pen nib writing, a multi-tool opened out, the tips of a colour set in
   order). USE CASE shows the right user using it in the right place (hands only if they fit the use and the brand allows).
   LIFESTYLE shows its world, wider, in the brand mood. Only plan angles and parts the photos actually show.
   Keep every plan concrete and short (each field one sentence).
9. review_needed = true when the photos are unclear, contradictory, show several products, or the product is hard to
   reproduce (fine text, complex pattern, many items); say why in review_reason.

STUDIO RULES (always):
{static}

SHOT RULES:
{shot_rules}

BRAND:
{brand}

PRODUCT FACTS FROM THE MASTER SHEET:
{facts}

SCHEMA (return JSON only, no markdown):
{schema}"""


# ---------------------------------------------------------------- inputs
def static_rules(s: Session) -> str:
    own = (get_setting(s, "static_rules", "") or "").strip()     # edited in Admin -> Autopilot rules
    if own:
        return own
    from .dataset import live_rules
    rv = live_rules(s)
    return ((rv.data or {}).get("static_rules") if rv else None) or DEFAULT_STATIC


def _shot_rules(s: Session) -> str:
    from .dataset import live_rules
    rv = live_rules(s)
    out = []
    for sh in ((rv.data or {}).get("shot_directions") or {}).get("shots", []) if rv else []:
        sub = sh.get("macro") or {}
        out.append(f"- {sh['id']}: {sh.get('purpose', '')}. Camera: {sh.get('camera') or sub.get('camera', '')}. "
                   f"Framing: {sh.get('framing') or sub.get('framing', '')}. Props: {sh.get('props') or sub.get('props', '')}.")
    return "\n".join(out)


def _brand(s: Session, lp: LibProduct) -> tuple[str, dict]:
    from .brands import brand_key_for, brand_rows
    rows = brand_rows(s)
    key = brand_key_for(lp, rows)
    d = (rows[key].data or {}) if key in rows else {}
    lines = [f"Brand: {d.get('brand_name') or lp.brand or 'unbranded'}"]
    for k, lbl in (("positioning", "Positioning"), ("palette_hex", "Brand colours"), ("brand_dos", "Do"),
                   ("brand_donts", "Don't"), ("golden_style", "Look of our approved shots")):
        if d.get(k) not in (None, "", "-"):
            lines.append(f"{lbl}: {d[k]}")
    for kind, card in ((d.get("shots") or {}).items()):
        if (card or {}).get("mode") == "custom":
            bits = [f"{f}: {card[f]}" for f in ("angle", "scene", "background", "props", "pose", "extra") if card.get(f)]
            if bits:
                lines.append(f"Brand rule for {kind}: " + "; ".join(bits))
    return "\n".join(lines), d


def photos_for(s: Session, key: str, limit: int = 6) -> list[LibImage]:
    from .connectors.mirror import restore
    imgs = [i for i in s.scalars(select(LibImage).where(LibImage.key == key, LibImage.dup_of.is_(None)))
            if not (i.meta or {}).get("discarded") and i.cut_status != "skip" and (i.meta or {}).get("kit") in (None, "approved")]
    real = [i for i in imgs if i.source not in ("catalogue", "kit")]
    imgs = real or imgs
    imgs.sort(key=lambda i: (not i.primary, i.source == "kit", -(i.w * i.h)))
    return [i for i in imgs if restore(i.path)][:limit]


# ---------------------------------------------------------------- run the analysis
def make_plan(s: Session, key: str, prov, charge=None) -> dict:
    lp = s.scalar(select(LibProduct).where(LibProduct.key == key))
    if lp is None:
        raise ValueError("Unknown product")
    imgs = photos_for(s, key)
    if not imgs:
        raise ValueError("No usable photos for this product")
    brand_txt, _ = _brand(s, lp)
    ld = lp.data or {}
    facts = {"product_id": lp.product_id, "name": lp.name, "brand": lp.brand, "category": lp.category,
             "ean": lp.ean, "seo_title": (ld.get("titles") or {}).get("seo optimised title", ""),
             "photo_names": [i.src_name for i in imgs]}
    busy = any(w in (lp.name or "").lower() for w in ("set", " x", "pack", "assorted", "colours", "colors"))
    prompt = INSTRUCTIONS.format(hero_props="0-1 small props, the set itself is busy" if busy else "1-3 small props at the edges",
                                 static=static_rules(s), shot_rules=_shot_rules(s), brand=brand_txt,
                                 facts=json.dumps(facts, ensure_ascii=False), schema=json.dumps(SCHEMA, ensure_ascii=False))
    model = (get_setting(s, "model_planner", "") or "").strip()
    plan, used = prov.plan_product([storage.read(i.path) for i in imgs], prompt, model)
    if charge:
        from . import config
        charge(used)
    # photo check -> Discarded section
    discarded = []
    checks = {int(p.get("index", -1)): p for p in plan.get("photos") or [] if isinstance(p, dict)}
    for n, img in enumerate(imgs):
        c = checks.get(n) or {}
        if c.get("angle"):
            img.meta = {**(img.meta or {}), "angle": str(c["angle"]).split("|")[0].strip().lower()}
        bad = c.get("is_this_product") is False or c.get("usable") is False
        if bad and not (img.meta or {}).get("keep"):
            img.meta = {**(img.meta or {}), "discarded": {"reason": c.get("reason_if_discarded") or "not this product",
                                                           "by": "ai", "at": _now()}}
            img.primary = False
            discarded.append(img.id)
    left = [i for i in imgs if i.id not in discarded]
    if left and not any(i.primary for i in left):
        left[0].primary = True
    record = {"json": plan, "model": used, "at": _now(), "photos": [i.id for i in imgs], "discarded": discarded,
              "approved": False, "approved_by": "", "edited": False, "stale": False,
              "status": "ok" if left else "no_valid_photos"}
    old_mem = ld.get("memory") or {}
    if old_mem.get("approved") and old_mem.get("facts"):          # facts a person approved always win
        mem = old_mem
    else:
        mem = {**old_mem, "facts": memory_facts(plan, ld), "auto": True, "photos": len(left)}
    lp.data = {**ld, "plan": record, "memory": mem}
    storage.save(f"plans/{storage.safe(lp.product_id or key)}.json",
                 json.dumps(record, ensure_ascii=False, indent=1).encode())
    log(s, "planner", f"{lp.product_id or key}: analysed {len(imgs)} photo(s) with {used}"
                      + (f", {len(discarded)} discarded" if discarded else "")
                      + (" · needs review" if plan.get("review_needed") else ""))
    s.commit()
    return record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="minutes")


def memory_facts(plan: dict, ld: dict) -> dict:
    """The analysis in the older 'product memory' shape, so QC and descriptions keep working."""
    ap, idn = plan.get("appearance") or {}, plan.get("identity") or {}
    text = [t.get("text", "") if isinstance(t, dict) else str(t) for t in ap.get("printed_text") or []]
    logo = (ap.get("logo") or {}).get("text")
    if logo and logo not in text:
        text.insert(0, logo)
    watch = ((ld.get("memory") or {}).get("facts") or {}).get("watch") or []
    return {"summary": f"{idn.get('type', '')}: {idn.get('form', '')}".strip(": "),
            "text": [t for t in text if t],
            "colours": [f"{c.get('name', '')} {c.get('hex', '')} ({c.get('where', '')})".strip() if isinstance(c, dict) else str(c)
                        for c in ap.get("colours") or []],
            "shape": idn.get("form", ""), "parts": idn.get("parts") or [],
            "material": ", ".join(ap.get("materials") or []) + (f"; {ap['finish']}" if ap.get("finish") else ""),
            "size": (plan.get("size") or {}).get("dimensions", ""), "must_keep": plan.get("must_keep") or [],
            "watch": list(dict.fromkeys(watch + (plan.get("risks") or [])))[:10]}


def needs_review(s: Session, rec: dict | None) -> bool:
    if not rec or rec.get("approved"):
        return False
    mode = get_setting(s, "plan_review", "flagged")
    return mode == "always" or (mode == "flagged" and bool((rec.get("json") or {}).get("review_needed")))


def mark_stale(s: Session, key: str):
    lp = s.scalar(select(LibProduct).where(LibProduct.key == key)) if key else None
    if lp is not None and (lp.data or {}).get("plan"):
        lp.data = {**lp.data, "plan": {**lp.data["plan"], "stale": True}}


# ---------------------------------------------------------------- render the prompt for the image model
def _j(v) -> str:
    if isinstance(v, list):
        return ", ".join(_j(x) for x in v if x)
    if isinstance(v, dict):
        return ", ".join(str(x) for x in v.values() if x)
    return str(v or "").strip()


def render(plan: dict, kind: str, static: str, anchored: bool, approved_facts: str = "") -> str:
    """Labelled plain-English sections, most important first. The image model never sees raw JSON."""
    ap, idn, sh = plan.get("appearance") or {}, plan.get("identity") or {}, (plan.get("shots") or {}).get(kind) or {}
    texts = [f'"{t["text"]}" ({t.get("where", "")})' if isinstance(t, dict) else f'"{t}"' for t in ap.get("printed_text") or []]
    logo = ap.get("logo") or {}
    product = [f"{idn.get('type', '')}, {idn.get('form', '')}".strip(", ")]
    if ap.get("colours"):
        product.append("colours " + _j([f"{c.get('name')} {c.get('hex', '')} on {c.get('where', '')}" if isinstance(c, dict) else c
                                        for c in ap["colours"]]))
    if ap.get("materials") or ap.get("finish"):
        product.append(f"{_j(ap.get('materials'))} {ap.get('finish', '')}".strip())
    if ap.get("pattern"):
        product.append(f"pattern: {ap['pattern']}")
    if logo.get("text"):
        product.append(f'logo "{logo["text"]}" ({logo.get("form", "")}, {logo.get("where", "")})')
    if texts:
        product.append("printed text exactly: " + ", ".join(texts))
    if ap.get("hardware"):
        product.append("hardware: " + _j(ap["hardware"]))
    if idn.get("item_count") not in (None, "", 1, "1"):
        product.append(f"{idn['item_count']} items" + (f" in this exact order: {ap['order_of_items']}" if ap.get("order_of_items") else ""))
    size = plan.get("size") or {}
    lines = [
        "REFERENCE IMAGES: The first image is the real product. " + (
            "The second image is our approved hero shot of the same product: the product must look identical to it. "
            if anchored else "") + "Copy the product from these exactly; do not redesign anything.",
        "PRODUCT - DO NOT CHANGE: " + "; ".join(p for p in product if p) + ".",
        f"MUST KEEP: {_j(plan.get('must_keep'))}." if plan.get("must_keep") else "",
        approved_facts,
        f"SHOT {SHOT_TITLE.get(kind, kind)}: {sh.get('goal', '')} Scene: {sh.get('scene', '')} on {sh.get('surface', '')}. "
        f"Product: {sh.get('product_state', 'as in the photos')}.",
        f"CAMERA & COMPOSITION: {sh.get('camera', '')}. {sh.get('composition', '')}.",
        f"LIGHT: {sh.get('light') or 'soft diffused studio light from the upper left, gentle natural shadow'}.",
        f"PROPS: {_j(sh.get('props'))} - plain, unbranded, real scale, at the edges, never covering the logo or print."
        if sh.get("props") else "",
        f"HANDS / PEOPLE: {sh['hands']}." if sh.get("hands") and str(sh["hands"]).lower() not in ("none", "no", "") else "HANDS / PEOPLE: none.",
        f"SCALE: {size.get('dimensions', '')}{' (approx.)' if size.get('estimated') else ''}; {size.get('scale_hint', '')}."
        if size.get("dimensions") or size.get("scale_hint") else "",
        f"NOTE: {sh['notes']}" if sh.get("notes") else "",
        "STUDIO RULES: " + " ".join(x.strip() for x in static.splitlines() if x.strip()),
        "WATCH OUT: " + _j(plan.get("risks")) + "." if plan.get("risks") else "",
        "NEVER: redesign or redraw the product, change the logo or any printed word, add text or logos, add other branded "
        "products, let anything float, show studio lights, show a side the photos do not show.",
    ]
    return "\n".join(x for x in lines if x)


def qc_facts(plan: dict) -> str:
    ap = plan.get("appearance") or {}
    t = [x.get("text", "") if isinstance(x, dict) else str(x) for x in ap.get("printed_text") or []]
    bits = [f"printed text: {', '.join(x for x in t if x)}" if t else "",
            f"logo: {(ap.get('logo') or {}).get('text', '')} at {(ap.get('logo') or {}).get('where', '')}" if (ap.get("logo") or {}).get("text") else "",
            f"item count: {(plan.get('identity') or {}).get('item_count')}",
            f"must keep: {_j(plan.get('must_keep'))}" if plan.get("must_keep") else ""]
    return "; ".join(b for b in bits if b)
