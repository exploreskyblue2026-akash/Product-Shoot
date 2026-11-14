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
        from .library import refs_for
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


def retire_scene(s: Session, scene_id: int):
    """Kept for the old scene pool's rows still sitting in the database; nothing writes new ones any more."""
    a = s.get(SceneAsset, scene_id)
    if a:
        a.retired = True
        s.commit()
