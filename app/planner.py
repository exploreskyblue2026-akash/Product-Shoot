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
from .dataset import or_gate
from .db import LibImage, LibProduct, Setting, get_setting, log

SHOTS = ("hero", "lifestyle", "use_case", "detail")            # listing order (approved 12 Nov 2026)
SHOT_TITLE = {"hero": "1 · HERO", "lifestyle": "2 · LIFESTYLE", "use_case": "3 · USE CASE", "detail": "4 · DETAIL / FEATURE"}
B_PASS = 9                                                     # scored checkpoints B1-B11: at least 9 must pass

# What each slot must do: fixed camera / framing rules (the dynamic plan fills in the rest).
SLOT_STATIC = {
    "hero": "HERO: the product itself, clean, premium, instantly readable. Camera direction from the product's form "
            "(flat/thin -> straight top-down flat lay; long & thin -> top-down or high 60-75 deg; standing -> eye level or "
            "low 3/4; boxy -> 3/4 at 30-45 deg; soft -> eye level). 85-100mm look (50-85mm for top-down), never wide angle. "
            "Product 55-70% of the frame, at least 8% clean margin, nothing cropped, f/8-f/11 so the whole product is sharp. "
            "One simple surface (plus a plain wall only when not top-down); no window, furniture edge or line through the "
            "product. At most 2-3 small props at the edges, never overlapping the product.",
    "lifestyle": "LIFESTYLE: the product in its owner's real world. 35-50mm look, product 30-45% of the frame and clearly "
                 "the focus. A real moment, 3-5 props the owner really has, part of a person allowed (never a face). "
                 "Background tells the place, softly out of focus.",
    "use_case": "USE CASE: the main feature being used correctly. 50-100mm macro look. Hands of the right age, natural "
                "grip, five fingers. The correct part touches the correct surface and the result of the action is "
                "visible. Focus on the contact point; the product's label stays readable.",
    "detail": "DETAIL / FEATURE: the key differentiator - a mechanism, a second function, what is inside or included, a "
              "comparison, or a material when the material is the selling point. Not a plain zoom. 100mm macro look, "
              "f/8 or more, an angle that explains the feature, show the feature AND its result. No decorative props.",
}

DEFAULT_LOOK = """One photographer, one session: the same light, colour and finish in every image.
Light: soft overcast daylight from a large window on the left (north-facing, never direct sun); clear direction with gentle falloff to the right and soft natural shading.
Never: sun patches, light streaks, window or blind shadow patterns, hard shadows, spotlights, coloured light.
Colour: neutral white balance (about 5200 K); whites stay white, no orange, peach or one-colour cast; muted, true-to-life grading.
Materials: real textured surfaces with slight imperfection (plaster or limewash, stone, linen, paper with tooth, wood with grain); mix neutral tones, never one colour everywhere.
Reflective parts: soft gradient highlights; never a reflected room, window or person."""

DEFAULT_STATIC = """Listing image, 4:5 portrait, 1200 x 1500 px; main subject near the centre.
A real photograph from a full-frame camera with natural fine grain; never a 3D render, illustration or obvious AI look.
Perspective: verticals straight, horizon level, no wide-angle or fisheye distortion; top-down shots perfectly parallel to the surface.
The product is the sharpest element in the frame.
Real physics: every object rests, stands, leans or is held naturally with a real contact shadow; nothing floats, hovers, intersects or balances impossibly. A thin product lies flat; a standing product stands.
The product never changes: same shape, proportions, colours, materials, pattern, every printed word, logo, hardware, item count and order as in the real photos.
Show only what the photos show: never invent a side, an inside, a part or a mechanism.
Correct use: the right end / part touches the right surface, held the way it is really used.
Real-world scale between product and props.
Props: plain, unbranded, real objects the product's owner really has; never other products of any brand; never looking like part of the product; never covering its logo or print.
No text, captions, watermarks or graphics except what is printed on the product.
Hands when used: right age for the user, natural grip, five fingers, real nails; never a face.
Avoid: CGI-clean surfaces, showroom rooms, windows / door frames / table edges cutting the frame, stock AI props (trailing pothos, earbuds cases, pastel book stacks, latte cups) unless the audience calls for them.
The 4 shots look like one photoshoot."""

SCHEMA = {
    "photos": [{"index": 0, "is_this_product": True, "usable": True, "angle": "front|angle|side|back|top|detail|in_use|packaging|group",
                "reason_if_discarded": "",
                "triage": {"verdict": "use|edit|reference|reject", "slot": "lifestyle|use_case|detail|none",
                           "only_this_product": True,
                           "edits": ["only for 'edit': small fixes, e.g. remove the '95gr' icon; soften harsh light"],
                           "why": "one sentence"}}],
    "checks": {"A1_facts": {"pass": True, "note": "sources read"}, "A2_features": {"pass": True, "note": ""},
               "A3_how_used": {"pass": True, "note": ""}, "A4_views": {"pass": True, "note": "missing views, if any"},
               "A5_text": {"pass": True, "note": ""}, "A6_in_box": {"pass": True, "note": "included vs props"},
               "B1_user": True, "B2_world": True, "B3_props": True, "B4_scale": True, "B5_colours": True,
               "B6_feature_map": True, "B7_angles": True, "B8_moments": True, "B9_market": True, "B10_safety": True,
               "B11_form": True},
    "form": {"class": "flat|long|standing|boxy|soft", "dimensions_mm": "W x H x D", "placement": "how it rests",
             "hero_camera": "e.g. straight top-down 90 deg", "informative_face": "which face carries the label / print",
             "reflective": False},
    "features": [{"name": "e.g. eraser (top)", "how_used": "which part touches what, grip, visible result",
                  "slot": "hero|lifestyle|use_case|detail"}],
    "in_the_box": ["what is sold"],
    "audience": {"age": "e.g. 11-14", "life_stage": "school|college|office|home|hobby|gift", "confidence": "high|medium|low",
                 "world": "where they use it", "owned_props": [""], "banned": ["props / settings wrong for this user"]},
    "colour_plan": {"product": "", "surface": "", "background": "", "accents": "1-2 prop colours echoing the product"},
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
    "shots": {k: {"goal": "", "feature": "the feature this slot shows", "moment": "the real moment", "scene": "the real place, e.g. kid's study desk by a window",
                  "surface": "material + colour + texture, e.g. pale oak desk, fine grain",
                  "backdrop": "what is behind, e.g. plain warm-white wall, softly out of focus",
                  "camera": "angle, e.g. 30 degrees above, three-quarter front", "lens": "e.g. 85 mm product lens look",
                  "depth_of_field": "e.g. product fully sharp, background softly out of focus",
                  "orientation": "how the product lies / stands and which face points to the camera",
                  "composition": "product position + % of frame + negative space",
                  "props": [{"item": "e.g. spiral sketchbook", "material": "", "colour": "", "size_vs_product": "e.g. 2x longer",
                             "position": "e.g. back-left, half out of frame, out of focus", "why": "why it belongs with this product"}],
                  "hands": "none or who/how", "product_state": "", "light": "direction, softness, how reflective parts look",
                  "notes": "",
                  "reference_photo": "index of the photo the image model should copy for this shot"}
              for k in SHOTS},
    "look": {"backdrop_family": "one colour family for all 4 shots, e.g. warm off-white and pale oak",
             "palette": "3-4 colours (hex) that the set and props stay within, chosen from the brand palette and the product",
             "mood": "e.g. calm, bright, playful school morning", "material_rendering": "how this product's materials must look under the house light"},
    "best_reference": "index of the ONE photo that shows the whole single product most clearly (shape, colour, clip, logo)",
    "self_check": ["each studio rule you checked the plans against and what you changed"],
    "review_needed": False,
    "review_reason": "",
}

INSTRUCTIONS = """You are the art director and product analyst of an in-house e-commerce studio. Study ONLY the photos above
(they are all filed under one product code) and the facts given (master sheet, and the brand's OFFICIAL product page
when present: use its dimensions and description). Do not use any other source, do not guess from the web.

Do this in order and return ONE JSON object exactly in the schema at the end (the PRODUCT MEMORY):
0. INPUT TRIAGE - judge every photo on its own as a finished 4:5 (1200x1500) listing image for one slot (hero, lifestyle,
   use_case, detail): "use" = already right (product exact and sharp, slot purpose clear, soft light, no other brands,
   no heavy graphics, enough resolution); "edit" = right content and angle with small gaps fixable without touching the
   product (crop, colour / white balance, remove small icons, badges or dimension lines, soften harsh light, clean the
   background) - list the edits; "reference" = useful product truth but not publishable (dimension drawings, plain
   packshots, odd angles); "reject" = blurry, wrong product, watermark, low quality. At most one photo per slot, the best.
   The HERO is never taken from a photo: it is always staged by the approach, so slot is only lifestyle, use_case or
   detail. "only_this_product" is false when any other product is visible (other designs, pencil cases, sharpeners,
   toys - even of the same brand); such a photo can never be "use" or "edit". When the photos show several designs or
   models, say which one this code is in identity and mark photos of other models is_this_product false.
1. PHOTO CHECK - for every photo (index 0..n-1, in the order given): is it THIS product (same code / name / colourway)? Is it
   usable as a product reference (not a catalogue page, not a group of different products, not blurry or tiny)? Its angle.
   Mark wrong or unusable photos with a short reason. Be strict: a different colourway or design is NOT this product.
2. IDENTITY - type, form, parts, item count, states. If the photos show several identical items (e.g. 3 of the same pen)
   but the name does not say set/pack, the product is ONE item; say so. If they are different variants, only the one
   matching the code/name is this product.
3. EXACT APPEARANCE - describe colours exactly as they look in the photos (e.g. gunmetal / smoky dark chrome is NOT
   bright silver); when lighting makes a colour uncertain, say so. Colours with hex and where they are, materials, finish, pattern and its placement, EVERY printed
   word exactly as written with its position, the logo (text, form, position), hardware, and the exact order for sets.
4. SIZE - use any measurement visible in a photo or given in the facts; otherwise estimate from the product type and mark
   "estimated": true.
5. USE & USER - purpose, who uses it (age group), how it is held or used, which hands (child / adult / none), what goes with it.
6. CATEGORY & ENVIRONMENT - category, the best real setting to shoot it (e.g. kid's study desk, office desk, art table),
   matching PROP TYPES (plain, generic, unbranded) and mood.
7. HERO FEATURES - the 1-3 things a buyer must see. RISKS - what an image generator would likely get wrong. MUST KEEP.
7b. CHECKPOINTS - fill "checks". A (must pass): A1 facts from photos + official page / store text, A2 every function
   named, A3 how each is used (which part touches what), A4 a real photo of every part a slot will show (else say which
   view is missing), A5 exact text, A6 what is in the box vs props. B (true/false): B1 user and age, B2 their world,
   B3 props they own, B4 real size, B5 colour plan (product colours, 1-2 prop accents, a different neutral background,
   never one colour everywhere), B6 every key feature mapped to a slot, B7 four different angles, B8 a real moment per
   slot, B9 fits SkyBlue's Indian customers, B10 safe and suitable (no blades near kids' products), B11 form and
   placement. FORM: decide from the real W x H x D, never from the reference photo's angle: flat/thin (depth < 15% of the
   longest side or < 3 cm) lies flat, hero straight top-down; long & thin lies flat, top-down or high; standing stands,
   eye level or low 3/4; boxy on its widest base, 3/4 at 30-45 deg; soft slightly slouched or worn, eye level. The face
   with the label faces the camera and its text reads upright. If the physics do not work, change the placement.
   AUDIENCE: read the age and life stage from colours, print, size, design and price; their world, the props they own
   and a banned list (e.g. laptop and coffee cup for a kids' bag).
8. SHOT PLANS - plan the 4 listing shots (order: hero, lifestyle, use_case, detail) for THIS product, following the studio rules and shot rules below and the brand
   rules. Every shot has props (hero: {hero_props}). The DETAIL shot is NOT a plain zoom: show the most important hero
   feature in a way that explains it (e.g. a fountain-pen nib writing, a multi-tool opened out, the tips of a colour set in
   order). USE CASE shows the right user using it in the right place (hands only if they fit the use and the brand allows).
   LIFESTYLE shows its world, wider, in the brand mood. Only plan angles and parts the photos actually show.
   ONE LOOK: all 4 shots of this product (and every product of this brand) must look like ONE studio session. The
   light set-up, colour temperature and finish are fixed by the HOUSE LOOK below and must never change between shots.
   Choose in "look" one backdrop family, a 3-4 colour palette (from the brand palette and the product) and a mood that
   every shot keeps. Only the set, props, camera angle and the product's orientation change from shot to shot.
   SET DESIGN for every shot, like a real in-house photographer: a specific surface (material, colour, texture) and
   backdrop that suit the product's category, user and the brand palette; 1-3 props for the hero (more for lifestyle),
   each a real, plain, unbranded object with its material, colour, real size compared with the product, exact position
   and why it belongs (e.g. kids' stationery -> drawing book, crayons; office pen -> notebook, desk tray; art material ->
   paper, palette). Props never touch or hide the logo or print and are never other products of any brand.
   Light: soft studio light, its direction, how shiny / metallic / glossy parts should reflect.
   Keep every field concrete and short (one sentence).
   SELF-CHECK: before answering, check every shot plan against the STUDIO RULES (nothing floating, no studio lights in
   frame, no branded or logo props, real scale, product unchanged and fully visible, props make sense) and fix it; list
   what you checked in self_check.
9. REFERENCES - best_reference: the index of the photo showing the whole product most clearly. For each shot,
   reference_photo: the index of the photo the image model should copy (usually best_reference; a close-up for detail
   only if it shows the planned feature).
10. review_needed = true when the photos are unclear, contradictory, show several products, or the product is hard to
   reproduce (fine text, complex pattern, many items); say why in review_reason.

SLOT RULES (fixed per slot):
{slots}

HOUSE LOOK (fixed for every image):
{look}

STUDIO RULES (always):
{static}

BRAND:
{brand}

PRODUCT FACTS FROM THE MASTER SHEET:
{facts}

SCHEMA (return JSON only, no markdown):
{schema}"""


# ---------------------------------------------------------------- inputs
def house_look(s: Session) -> str:
    return (get_setting(s, "house_look", "") or "").strip() or DEFAULT_LOOK


def static_rules(s: Session) -> str:
    own = (get_setting(s, "static_rules", "") or "").strip()     # edited in Admin -> Autopilot rules
    return own or DEFAULT_STATIC


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
    imgs.sort(key=lambda i: ({"store": 0, "website": 1}.get(i.source, 2), not i.primary, i.source == "kit", -(i.w * i.h)))
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
             "photo_names": [i.src_name for i in imgs],
             "official_website": {k: v for k, v in (ld.get("web") or {}).items() if k in ("url", "title", "description", "dimensions")},
             "skyblue_store": {"title": (ld.get("store") or {}).get("title", ""), "description": ld.get("store_description", "")}}
    busy = any(w in (lp.name or "").lower() for w in ("set", " x", "pack", "assorted", "colours", "colors"))
    prompt = INSTRUCTIONS.format(slots="\n".join(f"- {v}" for v in SLOT_STATIC.values()), hero_props="0-1 small props, the set itself is busy" if busy else "1-3 small props at the edges",
                                 look=house_look(s), static=static_rules(s), brand=brand_txt,
                                 facts=json.dumps(facts, ensure_ascii=False), schema=json.dumps(SCHEMA, ensure_ascii=False))
    model = or_gate(s, get_setting(s, "model_planner", ""))
    plan, used = prov.plan_product([storage.read(i.path) for i in imgs], prompt, model)
    _, bd = _brand(s, lp)
    plan["_brand"] = {k: bd.get(k) for k in ("brand_name", "palette_hex", "hero_backdrop", "mood_override") if bd.get(k)}
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


def gate(plan: dict) -> dict:
    """Checkpoint gate: every A must pass and at least B_PASS of the 11 B checks. Plans without 'checks' (older) pass."""
    ch = plan.get("checks") or {}
    if not ch:
        return {"ok": True, "a_failed": [], "b_score": None, "why": ""}
    a_failed = [k for k, v in ch.items() if k.startswith("A") and isinstance(v, dict) and v.get("pass") is False]
    b = [v for k, v in ch.items() if k.startswith("B")]
    b_score = sum(1 for v in b if (v.get("pass") if isinstance(v, dict) else v) is not False)
    why = []
    for k in a_failed:
        why.append(f"{k}: {(ch[k] or {}).get('note') or 'failed'}")
    if b and b_score < B_PASS:
        why.append(f"checkpoints B {b_score}/{len(b)} (need {B_PASS})")
    return {"ok": not why, "a_failed": a_failed, "b_score": b_score, "why": "; ".join(why)}


def needs_review(s: Session, rec: dict | None) -> bool:
    if not rec or rec.get("approved"):
        return False
    pj = rec.get("json") or {}
    if not gate(pj)["ok"]:
        return True
    mode = get_setting(s, "plan_review", "flagged")
    return mode == "always" or (mode == "flagged" and bool(pj.get("review_needed")))


def review_reason(rec: dict | None) -> str:
    pj = (rec or {}).get("json") or {}
    return gate(pj)["why"] or pj.get("review_reason") or "flagged by the analysis"


def reuse_for(rec: dict | None, kind: str) -> dict | None:
    """Input-image triage: the best input photo for this slot that can be used as-is or after a small edit.
    Returns {"image_id", "verdict": "use"|"edit", "edits": [...], "why"} or None (then the slot is generated)."""
    pj = (rec or {}).get("json") or {}
    ids = (rec or {}).get("photos") or []
    best = None
    for p in pj.get("photos") or []:
        tr = (p or {}).get("triage") or {}
        if kind == "hero" or str(tr.get("slot", "")).strip().lower() != kind or tr.get("verdict") not in ("use", "edit"):
            continue
        if tr.get("only_this_product") is not True:          # other products in frame could look included
            continue
        if p.get("is_this_product") is False or p.get("usable") is False:
            continue
        try:
            img_id = ids[int(p.get("index", -1))]
        except (ValueError, TypeError, IndexError):
            continue
        cand = {"image_id": img_id, "verdict": tr["verdict"], "edits": [e for e in tr.get("edits") or [] if e],
                "why": tr.get("why", "")}
        if best is None or (cand["verdict"] == "use" and best["verdict"] != "use"):
            best = cand
    return best


EDIT_PROMPT = """Edit this real product photograph into a finished SkyBlue listing image. Make ONLY these small changes:
{edits}
Keep everything else exactly as it is: the product (shape, colours, every printed word and logo), its position, the
props, the scene and the camera angle. Do not add anything. Light and colour: soft, even, neutral white balance (about
5200 K), no harsh sun patches, muted natural grading. Output a clean real photograph, 4:5 portrait."""


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


def _prop(x) -> str:
    if not isinstance(x, dict):
        return str(x)
    bits = [x.get("item", ""), ", ".join(v for v in (x.get("material"), x.get("colour")) if v),
            f"{x['size_vs_product']} the product" if x.get("size_vs_product") else "", x.get("position", "")]
    return " - ".join(b for b in bits if b)


def render(plan: dict, kind: str, static: str, anchored: bool, approved_facts: str = "", look: str = "") -> str:
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
        f"SHOT {SHOT_TITLE.get(kind, kind)}: {sh.get('goal', '')}",
        f"SLOT RULES: {SLOT_STATIC[kind]}" if kind in SLOT_STATIC else "",
        f"FEATURE SHOWN: {sh['feature']}." if sh.get("feature") else "",
        f"MOMENT: {sh['moment']}." if sh.get("moment") else "",
        ("PRODUCT FORM: " + "; ".join(x for x in (fm.get("class"), fm.get("dimensions_mm"), fm.get("placement"),
                                                   f"hero camera {fm['hero_camera']}" if fm.get("hero_camera") and kind == "hero" else "",
                                                   f"label face: {fm['informative_face']}" if fm.get("informative_face") else "") if x) + ".")
        if (fm := plan.get("form") or {}) else "",
        ("USER: " + "; ".join(x for x in (au.get("age"), au.get("life_stage"), au.get("world")) if x) + "."
         + (f" Never show: {_j(au['banned'])}." if au.get("banned") else ""))
        if (au := plan.get("audience") or {}) and kind != "hero" else "",
        ("COLOUR PLAN: " + "; ".join(f"{k} {v}" for k, v in (plan.get("colour_plan") or {}).items() if v)
         + ". Never one colour across background, surface and props.") if plan.get("colour_plan") else "",
        f"SET: {sh.get('scene', '')}. Surface: {sh.get('surface', '')}."
        + (f" Backdrop: {sh['backdrop']}." if sh.get("backdrop") else "")
        + f" Product: {sh.get('product_state', 'as in the photos')}.",
        "CAMERA: " + ". ".join(x for x in (sh.get("camera"), sh.get("lens"), sh.get("depth_of_field")) if x) + ".",
        f"PRODUCT ORIENTATION: {sh['orientation']}." if sh.get("orientation") else "",
        f"COMPOSITION: {sh.get('composition', '')}.",
        "STUDIO LOOK (identical in every image of this shoot): " + " ".join(x.strip() for x in (look or DEFAULT_LOOK).splitlines() if x.strip())
        + (f" Backdrop family: {lk['backdrop_family']}." if (lk := plan.get("look") or {}).get("backdrop_family") else "")
        + (f" Palette: {lk['palette']}." if lk.get("palette") else "")
        + (f" Brand colours: {(plan.get('_brand') or {}).get('palette_hex')}." if (plan.get("_brand") or {}).get("palette_hex") else "")
        + (f" Mood: {lk['mood']}." if lk.get("mood") else ""),
        "LIGHT ON THIS PRODUCT: " + "; ".join(x for x in (lk.get("material_rendering"), sh.get("light")) if x) + "."
        if (lk.get("material_rendering") or sh.get("light")) else "",
        ("PROPS (plain, unbranded, real scale, never covering the logo or print): " + "; ".join(_prop(x) for x in sh["props"]) + ".")
        if sh.get("props") else "",
        f"HANDS / PEOPLE: {sh['hands']}." if sh.get("hands") and str(sh["hands"]).lower() not in ("none", "no", "") else "HANDS / PEOPLE: none.",
        f"SCALE: {size.get('dimensions', '')}{' (approx.)' if size.get('estimated') else ''}; {size.get('scale_hint', '')}."
        if size.get("dimensions") or size.get("scale_hint") else "",
        f"NOTE: {sh['notes']}" if sh.get("notes") else "",
        "STUDIO RULES: " + " ".join(x.strip() for x in static.splitlines() if x.strip()),
        "WATCH OUT: " + _j(plan.get("risks")) + "." if plan.get("risks") else "",
        "UNBRANDED PRODUCT: this product carries no brand. Do not add any brand name, logo, label or lettering that is "
        "not clearly visible in the reference photos; never show 'Made in China' or supplier marks."
        if plan.get("_unbranded") else "",
        "NEVER: redesign or redraw the product, change the logo or any printed word, add text or logos, add other branded "
        "products, let anything float, show studio lights, show a side the photos do not show.",
    ]
    return "\n".join(x for x in lines if x)


def for_product(plan: dict, lp) -> dict:
    """The plan as used for rendering, with what is known about the product outside the analysis (is it unbranded?)."""
    from .dataset import UNBRANDED
    label = str(getattr(lp, "brand", "") or "").strip().lower()
    return {**plan, "_unbranded": True} if label in UNBRANDED and not (plan.get("_brand") or {}).get("brand_name") else plan


def qc_facts(plan: dict) -> str:
    ap = plan.get("appearance") or {}
    t = [x.get("text", "") if isinstance(x, dict) else str(x) for x in ap.get("printed_text") or []]
    bits = [f"printed text: {', '.join(x for x in t if x)}" if t else "",
            f"logo: {(ap.get('logo') or {}).get('text', '')} at {(ap.get('logo') or {}).get('where', '')}" if (ap.get("logo") or {}).get("text") else "",
            f"item count: {(plan.get('identity') or {}).get('item_count')}",
            f"must keep: {_j(plan.get('must_keep'))}" if plan.get("must_keep") else ""]
    return "; ".join(b for b in bits if b)
