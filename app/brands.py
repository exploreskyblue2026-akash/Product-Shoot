"""Brand workspace: one page per brand with its rulebook, golden shots, logo and its own ordered product queue.

Brand queue  = the products of this brand you want shot next, in order (drag & drop on the brand page).
Brand order  = which brand the studio works on first (e.g. Apli #1, Milan #2). Sending a brand queue to the studio
               gives its products priority = brand rank × 1000 + position, so the studio follows both orders.
"""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import LibImage, LibProduct, LibraryRow, Product, log


def _n(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def brand_rows(s: Session) -> dict[str, LibraryRow]:
    return {r.key: r for r in s.scalars(select(LibraryRow).where(LibraryRow.table == "brands"))}


def brand_key_for(lp: LibProduct, rows: dict) -> str:
    """Which brand profile a master-sheet product belongs to: tab name / brand name / detect words."""
    label, name = _n(lp.brand), (lp.name or "").lower()
    for key, r in rows.items():
        if key.startswith("_"):
            continue
        d = r.data or {}
        if label and label in (_n(d.get("brand_name")), _n(key)):
            return key
    for key, r in rows.items():
        if key.startswith("_"):
            continue
        words = [w.strip() for w in str((r.data or {}).get("detect_keywords", "")).split("|") if w.strip()]
        if any(w and (w in (lp.brand or "").lower() or re.search(rf"\b{re.escape(w)}\b", name)) for w in words):
            return key
    return "_default"


def products_of(s: Session, key: str) -> list[LibProduct]:
    rows = brand_rows(s)
    return [p for p in s.scalars(select(LibProduct)) if brand_key_for(p, rows) == key]


def photo_status(s: Session, lib_key: str) -> dict:
    imgs = [i for i in s.scalars(select(LibImage).where(LibImage.key == lib_key, LibImage.dup_of.is_(None)))]
    prim = next((i for i in imgs if i.primary), None)
    if not imgs:
        st = "needs_photo"
    elif prim and (prim.meta or {}).get("flags") and not (prim.meta or {}).get("override"):
        st = "low_quality"
    elif prim and prim.cut_status == "skip":
        st = "skipped"
    else:
        st = "ready"
    return {"photos": len(imgs), "status": st, "thumb": f"/files/{prim.path}" if prim else "",
            "flags": (prim.meta or {}).get("flags", []) if prim else []}


def studio_stage(s: Session, lib_key: str) -> str:
    for p in s.scalars(select(Product)):
        if (p.data or {}).get("lib_key") == lib_key and p.stage != "rejected":
            return p.stage
    return ""


def summary(s: Session) -> list[dict]:
    rows = brand_rows(s)
    counts: dict[str, int] = {}
    for p in s.scalars(select(LibProduct)):
        k = brand_key_for(p, rows)
        counts[k] = counts.get(k, 0) + 1
    out = []
    for key, r in rows.items():
        d = r.data or {}
        out.append({"key": key, "name": "SkyBlue house style" if key == "_default" else d.get("brand_name") or key,
                    "logo": f"/files/{d['logo']}" if d.get("logo") else "", "origin": d.get("origin", ""),
                    "website": d.get("website", ""), "estimate": d.get("products_estimate", ""),
                    "rank": d.get("rank"), "in_master": counts.get(key, 0), "queue": len(d.get("queue") or []),
                    "palette": d.get("palette_hex", ""), "status": d.get("status", "")})
    out.sort(key=lambda b: (b["key"] != "_default", b["rank"] is None, b["rank"] or 0, b["name"].lower()))
    return out


def detail(s: Session, key: str, search: str = "", limit: int = 200) -> dict:
    rows = brand_rows(s)
    r = rows.get(key)
    if not r:
        raise KeyError(key)
    d = dict(r.data or {})
    queue = d.get("queue") or []
    lib = {p.key: p for p in s.scalars(select(LibProduct).where(LibProduct.key.in_(queue)))} if queue else {}
    q_items = []
    for k in queue:
        p = lib.get(k)
        if p:
            q_items.append({"key": k, "code": p.product_id or k, "name": p.name, **photo_status(s, k),
                            "memory": bool(((p.data or {}).get("memory") or {}).get("facts")),
                            "approved": bool(((p.data or {}).get("memory") or {}).get("approved")),
                            "studio": studio_stage(s, k)})
    avail = []
    if key != "_default":
        term = _n(search)
        for p in products_of(s, key):
            if p.key in queue:
                continue
            if term and term not in _n(p.product_id) + _n(p.name) + _n(p.item_code) + _n(p.ean):
                continue
            avail.append({"key": p.key, "code": p.product_id or p.key, "name": p.name, "category": p.category,
                          **photo_status(s, p.key)})
        avail.sort(key=lambda x: (x["status"] != "ready", x["status"] != "low_quality", x["photos"] == 0))   # with photos first
        avail = avail[:limit]
    golden = [f"/files/{x}" for x in d.get("golden") or []]
    return {"key": key, "profile": {k: v for k, v in d.items() if k not in ("queue", "golden")}, "golden": golden,
            "logo": f"/files/{d['logo']}" if d.get("logo") else "", "queue": q_items, "available": avail,
            "in_master": len(products_of(s, key)) if key != "_default" else 0}


def set_queue(s: Session, key: str, keys: list[str]):
    r = brand_rows(s)[key]
    r.data = {**(r.data or {}), "queue": list(dict.fromkeys(k for k in keys if k))}


def add_codes(s: Session, key: str, codes: list[str]) -> dict:
    """Add products by code (product id, item code or EAN) to the end of the brand queue."""
    from .library import _index, resolve
    idx = _index(s)
    r = brand_rows(s)[key]
    queue = list((r.data or {}).get("queue") or [])
    added, unknown = [], []
    for c in codes:
        p, _ = resolve(c, idx)
        if p and p.key not in queue:
            queue.append(p.key)
            added.append(p.product_id or p.key)
        elif not p:
            unknown.append(c)
    r.data = {**(r.data or {}), "queue": queue}
    return {"added": added, "unknown": unknown}


def set_rank(s: Session, order: list[str]):
    """Brand order for the studio: first key = #1."""
    rows = brand_rows(s)
    for n, k in enumerate(order, 1):
        if k in rows:
            rows[k].data = {**(rows[k].data or {}), "rank": n}
    for k, r in rows.items():
        if k not in order and (r.data or {}).get("rank") is not None:
            r.data = {**r.data, "rank": None}


def queued_keys(s: Session) -> set[str]:
    """Every product sitting in some brand queue (the Library scan processes these too)."""
    out = set()
    for r in brand_rows(s).values():
        out |= set((r.data or {}).get("queue") or [])
    return out


def send_to_studio(s: Session, keys: list[str] | None = None) -> dict:
    """Load brand queues into the studio queue, in brand order then queue order. keys=None: every ranked brand."""
    from .v2 import load_items
    rows = brand_rows(s)
    chosen = keys or [k for k, r in rows.items() if (r.data or {}).get("rank") is not None]
    chosen.sort(key=lambda k: ((rows[k].data or {}).get("rank") or 999))
    items = []
    for k in chosen:
        d = rows[k].data or {}
        rank = d.get("rank") or 999
        for pos, lib_key in enumerate(d.get("queue") or [], 1):
            items.append({"key": lib_key, "code": lib_key, "priority": rank * 1000 + pos, "shots": [],
                          "note": "", "brand_key": k})
    r = load_items(s, items)
    log(s, "brands", f"Sent {len(items)} products from {', '.join(chosen)} to the studio queue")
    s.commit()
    return r
