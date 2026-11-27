"""Shoot-sheet queueing: brand/category matching for products coming in from the master sheet or a brand queue,
and loading those items into the studio queue. Image generation itself lives in pipeline.py."""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import LibImage, LibProduct, Product, SceneAsset, get_setting, log
from .dataset import brand_from_text, library

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
def auto_sync_queue(s: Session) -> int:
    """Runs on every worker tick (every few seconds, not on a timer of its own): the moment a shoot-sheet code
    has a usable photo in the Library, it is pushed into the Studio queue automatically - no 'Load shoot sheet'
    click needed. Unlike load_shoot_sheet()/load_items(), this NEVER touches a product already in the queue
    (no priority reset, no clobbering a manual drag-reorder or pause) - it only ever adds brand-new rows for
    codes that aren't in the Studio queue yet, and only once they're actually ready to shoot. A code with no
    photo yet is simply skipped and picked up automatically on a later tick once a photo appears."""
    if get_setting(s, "pipeline_mode", "v1") != "v2" or not get_setting(s, "v2_autoqueue", True):
        return 0
    items = get_setting(s, "shoot_list", []) or []
    if not items:
        return 0
    known = {(p.data or {}).get("lib_key") for p in s.scalars(select(Product)) if (p.data or {}).get("v2")}
    top = max([(p.data or {}).get("priority", 0) for p in s.scalars(select(Product)) if (p.data or {}).get("v2")], default=0)
    added, n = 0, 0
    for it in items:
        key = it.get("key")
        if not key or key in known:
            continue
        from .library import pick_primary
        pick_primary(s, key)                             # re-derive the primary photo fresh: a product can genuinely
        # have several usable photos in the Library with none of them currently flagged primary (the flag can lag
        # behind research that just finished), and this was the exact cause of "has photos in the Library, shows no
        # photo in Studio" - trusting a possibly-stale flag here instead of recomputing it.
        img = s.scalar(select(LibImage).where(LibImage.key == key, LibImage.primary == True))  # noqa: E712
        q = (img.meta or {}) if img else {}
        ready = bool(img and img.cut_status != "skip" and (not q.get("flags") or q.get("override")))
        if not ready:
            continue                                     # not yet - picked up automatically once it has a photo
        n += 1
        load_items(s, [{**it, "priority": top + n}])
        known.add(key)
        added += 1
    if added:
        log(s, "v2", f"Auto-queued {added} newly-ready code(s) from the shoot sheet")
        s.commit()
    return added


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
        from .library import refs_for, pick_primary
        if key:
            pick_primary(s, key)                          # re-derive fresh, same reasoning as auto_sync_queue() above:
            # a product can have usable photos in the Library with none currently flagged primary, which otherwise
            # pushes it to Studio as "no photo" even though the Library clearly shows photos for it.
        img = s.scalar(select(LibImage).where(LibImage.key == key, LibImage.primary == True)) if key else None  # noqa: E712
        q = (img.meta or {}) if img else {}
        ready = bool(img and img.cut_status != "skip" and (not q.get("flags") or q.get("override")))
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
                  "paused": False, "refs": refs_for(s, key) if (ready and key) else []}
        if not key:
            p.stage, p.error = "error", f"{it['code']} is not in the master sheet"
            missing += 1
        elif not ready:
            p.stage, p.error = "needs_photo", ("Photo " + " and ".join((img.meta or {}).get("flags", [])) + ": add a better one or press 'Use anyway' in the Library"
                                               if img and (img.meta or {}).get("flags") else "No photo in the Library for this code")
            waiting += 1
        else:
            p.raw_path, p.stage, p.error = img.path, ("queue" if p.stage in ("needs_photo", "error", "queue", "") else p.stage), ""
    s.commit()
    return {"added": added, "updated": updated, "waiting": waiting, "missing": missing, "total": len(items)}


# ------------------------------------------------------------------ queue exact codes you type in
def queue_codes(s: Session, codes: list[str]) -> dict:
    """Queue ONLY the codes typed in, instead of the whole shoot sheet. A typed code already on the shoot
    sheet keeps its shots/note from there; a code not on the sheet is still queued (all 4 shots, no note) as
    long as it resolves to a product in the Library. Newly typed codes jump to the top of the queue."""
    from .library import _index, resolve
    codes = [c.strip() for c in (codes or []) if c and c.strip()]
    if not codes:
        raise ValueError("Type at least one product code.")
    sheet_by_key = {}
    for it in (get_setting(s, "shoot_list", []) or []):
        if it.get("key"):
            sheet_by_key[it["key"]] = it
    idx = _index(s)
    items, not_found = [], []
    for n, code in enumerate(codes):
        lp, how = resolve(code, idx)
        if not lp:
            not_found.append(code)
            continue
        sh = sheet_by_key.get(lp.key, {})
        items.append({"key": lp.key, "code": sh.get("code", code), "shots": sh.get("shots", []),
                      "note": sh.get("note", ""), "priority": n - len(codes)})   # negative: floats to the top
    if not items:
        raise ValueError(f"None of those codes were found in the master sheet or Library: {', '.join(not_found)}")
    r = load_items(s, items)
    r["not_found"] = not_found
    log(s, "v2", f"Queued {len(items)} typed code(s): {r['added']} new, {r['updated']} updated"
                 + (f"; not found: {', '.join(not_found)}" if not_found else ""))
    s.commit()
    return r


def clear_stuck(s: Session) -> int:
    """Remove v2 queue rows stuck as 'no photo' or 'error' (e.g. after loading the whole shoot sheet).
    Never touches anything mid-generation, under review, or already saved."""
    rows = [p for p in s.scalars(select(Product).where(Product.stage.in_(["error", "needs_photo"])))
            if (p.data or {}).get("v2")]
    n = len(rows)
    for p in rows:
        s.delete(p)
    s.commit()
    return n


def retire_scene(s: Session, scene_id: int):
    """Kept for the old scene pool's rows still sitting in the database; nothing writes new ones any more."""
    a = s.get(SceneAsset, scene_id)
    if a:
        a.retired = True
        s.commit()
