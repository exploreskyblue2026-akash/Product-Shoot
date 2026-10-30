"""Shopify queue.

Three lists (when a listing sheet is loaded):
  1. Ready and in the listing sheet      -> push these
  2. Ready but not in the listing sheet  -> shots approved, but the website team has not asked for them
  3. In the listing sheet but not ready  -> still to shoot / approve

Push = for every field, fill it only when the store's value is empty (or overwrite when ticked), add the approved
shots (never twice), add the product to EXISTING collections. Price and inventory are never touched.
A preview shows exactly what will change before anything is written.
"""
import io
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import storage
from .db import LibProduct, Product, get_setting, log, set_setting

SHOT_LABEL = {"hero": "Hero", "detail": "Detail", "use_case": "In use", "lifestyle": "Lifestyle"}
FIELDS = [("title", "SEO title"), ("descriptionHtml", "Description"), ("category", "Category"),
          ("productType", "Product type"), ("vendor", "Vendor"), ("tags", "Tags"), ("collections", "Collections"),
          ("templateSuffix", "Theme template"), ("sku", "SKU"), ("barcode", "Barcode"), ("media", "Images")]


def _n(x) -> str:
    from .library import norm
    s = str(x).strip() if isinstance(x, str) else x
    if isinstance(s, str) and re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return norm(s)


def _split(v) -> list[str]:
    return [x.strip() for x in re.split(r"[|,;]", str(v or "")) if x.strip()]


# ---------------------------------------------------------------- listing sheet
def load_listing(s: Session, data: bytes, filename: str) -> dict:
    """Columns (any order, case-insensitive): a code column (Product ID / Item code / EAN / Barcode / SKU / Code) and
    optional Category, Collections, Tags, Template, Title, Description."""
    import openpyxl
    rows = []
    if filename.lower().endswith(".csv"):
        import csv
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig", "replace"))))
    else:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    if not rows:
        raise ValueError("The listing sheet is empty")
    head = [str(x or "").strip().lower() for x in rows[0]]

    def col(*names):
        return next((i for i, h in enumerate(head) for n in names if h == n), None)
    ci = col("product id", "item code", "code", "sku", "ean", "eancode", "barcode", "product code")
    if ci is None:
        raise ValueError("No code column found (Product ID, Item code, EAN, Barcode, SKU or Code)")
    opt = {k: col(*names) for k, names in {"category": ("category", "shopify category"), "collections": ("collections", "collection"),
                                           "tags": ("tags",), "template": ("template", "theme template"),
                                           "title": ("title", "seo title"), "description": ("description",)}.items()}
    items = {}
    for r in rows[1:]:
        code = r[ci] if ci < len(r) else None
        if code in (None, ""):
            continue
        items[_n(code)] = {"code": str(code).strip().removesuffix(".0"),
                           **{k: str(r[i]).strip() for k, i in opt.items() if i is not None and i < len(r) and r[i] not in (None, "")}}
    set_setting(s, "shopify_listing", {"file": filename, "at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                                       "items": items})
    log(s, "shopify", f"Listing sheet {filename}: {len(items)} products")
    s.commit()
    return {"products": len(items)}


def _listing(s: Session) -> dict:
    return (get_setting(s, "shopify_listing", {}) or {}).get("items") or {}


def _lp(s: Session, key: str):
    return s.scalar(select(LibProduct).where(LibProduct.key == key)) if key else None


def _codes(lp, p) -> set:
    ld = (lp.data or {}) if lp else {}
    vals = [p.code if p else "", lp.key if lp else "", lp.product_id if lp else "", lp.item_code if lp else "",
            lp.ean if lp else ""] + list(ld.get("skus") or [])
    return {_n(v) for v in vals if v}


def _sheet_row(listing: dict, codes: set) -> dict | None:
    return next((listing[c] for c in codes if c in listing), None)


# ---------------------------------------------------------------- what we would write
def info_for(s: Session, p: Product) -> dict:
    d = p.data or {}
    lp = _lp(s, d.get("lib_key"))
    ld = (lp.data or {}) if lp else {}
    titles = ld.get("titles") or {}
    row = _sheet_row(_listing(s), _codes(lp, p)) or {}
    title = (row.get("title") or titles.get("seo optimised title") or titles.get("seo optimized title")
             or (lp.name if lp else "") or p.name or p.code)
    skus = list(dict.fromkeys([x for x in (ld.get("skus") or []) + [lp.product_id if lp else "", p.code] if x]))
    brand = d.get("brand_label") or (lp.brand if lp else p.brand_id) or ""
    tags = list(dict.fromkeys([t for t in [brand.lower()] + _split(row.get("tags")) if t]))
    return {"title": str(title)[:250], "vendor": brand.title() if brand.isupper() else brand,
            "type": ((lp.category if lp else "") or p.category_id or "").title(), "sku": skus[-1] if skus else p.code,
            "skus": skus, "barcode": str(lp.ean if lp else "").removesuffix(".0"), "tags": tags,
            "category_name": row.get("category") or (lp.category if lp else "") or "",
            "collections": _split(row.get("collections")), "in_sheet": bool(row),
            "template": row.get("template") or get_setting(s, "shopify_template", "") or "",
            "description_sheet": row.get("description") or ""}


def description_for(s: Session, p: Product, prov=None) -> str:
    """Sheet description if given, else an AI description written once from the product memory (cached)."""
    info = info_for(s, p)
    if info["description_sheet"]:
        return info["description_sheet"]
    lp = _lp(s, (p.data or {}).get("lib_key"))
    if lp is not None and (lp.data or {}).get("ai_description"):
        return lp.data["ai_description"]
    if prov is None:
        from .providers import get_provider
        prov = get_provider()
    mem = ((lp.data or {}).get("memory") or {}).get("facts") if lp else {}
    html = prov.describe_product({"title": info["title"], "brand": info["vendor"], "type": info["type"],
                                  "facts": mem or {}, "name": p.name})
    if lp is not None:
        lp.data = {**(lp.data or {}), "ai_description": html}
        s.commit()
    return html


def images_for(p: Product) -> list[tuple[str, bytes, str]]:
    out = []
    for sh in sorted(p.shots, key=lambda x: x.no):
        if sh.image_path and sh.status == "approved":
            alt = f"{p.name or p.code} | {SHOT_LABEL.get(sh.kind, sh.kind)}"
            out.append((f"{storage.safe(p.code)}_{sh.no}-{sh.kind}.jpg", storage.read(sh.image_path), alt[:500]))
    return out


def _empty(v) -> bool:
    return v in (None, "", [], {}) or (isinstance(v, str) and not re.sub(r"<[^>]+>|\s", "", v))


def plan(s: Session, p: Product, overwrite: bool = False, prov=None) -> dict:
    """Field-by-field plan against the live store product. Nothing is written."""
    from .connectors import shopify
    info = info_for(s, p)
    found, how = shopify.find_product(info["barcode"], info["skus"])
    cur = shopify.get_product(found["id"]) if found else {}
    variant = {}
    if cur:
        vs = cur.get("variants", {}).get("nodes", [])
        variant = next((v for v in vs if v["id"] == found.get("_variant")), vs[0] if vs else {})
    cat = shopify.find_category(info["category_name"]) if info["category_name"] else None
    cols, missing_cols = shopify.find_collections(info["collections"]) if info["collections"] else ([], [])
    want = {"title": info["title"], "descriptionHtml": description_for(s, p, prov), "category": cat,
            "productType": info["type"], "vendor": info["vendor"], "tags": info["tags"], "collections": cols,
            "templateSuffix": info["template"], "sku": info["sku"], "barcode": info["barcode"]}
    have = {"title": cur.get("title"), "descriptionHtml": cur.get("descriptionHtml"), "category": cur.get("category"),
            "productType": cur.get("productType"), "vendor": cur.get("vendor"), "tags": cur.get("tags") or [],
            "collections": (cur.get("collections") or {}).get("nodes", []), "templateSuffix": cur.get("templateSuffix"),
            "sku": variant.get("sku"), "barcode": variant.get("barcode")}
    rows, changes, add_cols = [], {}, []
    for f, label in FIELDS:
        if f == "media":
            imgs = [sh for sh in p.shots if sh.image_path and sh.status == "approved"]
            on = {m.get("alt") for m in (cur.get("media") or {}).get("nodes", [])}
            new = [sh for sh in imgs if f"{p.name or p.code} | {SHOT_LABEL.get(sh.kind, sh.kind)}"[:500] not in on]
            rows.append({"field": label, "current": f"{len(on)} image(s)", "proposed": f"+{len(new)} approved shot(s)",
                         "action": "add" if new else "same"})
            continue
        w, h = want[f], have[f]
        show = lambda v: (v.get("fullName") if isinstance(v, dict) else ", ".join(x["title"] if isinstance(x, dict) else x for x in v)  # noqa: E731
                          if isinstance(v, list) else re.sub(r"<[^>]+>", " ", str(v or ""))[:300])
        if f == "tags":
            extra = [t for t in w if t.lower() not in {x.lower() for x in h}]
            act = "add" if extra else "same"
            if extra:
                changes["tags"] = list(h) + extra               # adding never removes a tag
            rows.append({"field": label, "current": show(h), "proposed": show(extra) or show(w), "action": act})
            continue
        if f == "collections":
            have_ids = {c["id"] for c in h}
            add_cols = [c["id"] for c in w if c["id"] not in have_ids]
            note = f" · not in store, skipped: {', '.join(missing_cols)}" if missing_cols else ""
            rows.append({"field": label, "current": show(h), "proposed": (show([c for c in w if c["id"] in add_cols]) or "—") + note,
                         "action": "add" if add_cols else ("skip" if missing_cols else "same")})
            continue
        if _empty(w):
            rows.append({"field": label, "current": show(h), "proposed": "—", "action": "none"})
            continue
        same = (h or {}).get("id") == w.get("id") if f == "category" else str(h or "").strip() == str(w).strip()
        if same:
            act = "same"
        elif _empty(h):
            act = "fill"
        else:
            act = "overwrite" if overwrite and f not in ("sku", "barcode") else "keep"
        if act in ("fill", "overwrite"):
            changes[f] = w["id"] if f == "category" else w
        rows.append({"field": label, "current": show(h) or "(empty)", "proposed": show(w), "action": act})
    rows.append({"field": "Price / inventory", "current": "—", "proposed": "never changed by the tool", "action": "none"})
    return {"found": bool(cur), "how": how or "no match: a new DRAFT product will be created", "product_id": cur.get("id", ""),
            "variant_id": variant.get("id", ""), "rows": rows, "changes": changes, "add_collections": add_cols,
            "info": {**info, "description": want["descriptionHtml"], "category_id": cat["id"] if cat else ""}}


def push_one(s: Session, p: Product, overwrite: bool = False) -> dict:
    from .connectors import shopify
    imgs = images_for(p)
    if not imgs:
        raise RuntimeError("No approved shots to push")
    pl = plan(s, p, overwrite)
    if pl["found"]:
        pid = pl["product_id"]
        shopify.apply(pid, pl["variant_id"], pl["changes"], pl["add_collections"])
        existing = {m.get("alt") for m in (shopify.get_product(pid).get("media") or {}).get("nodes", [])}
        how = pl["how"]
    else:
        pid = shopify.create_draft(pl["info"])
        if pl["add_collections"]:
            shopify.apply(pid, "", {}, pl["add_collections"])
        existing, how = set(), "new draft product"
    added, skipped = shopify.add_media(pid, imgs, existing)
    filled = [r["field"] for r in pl["rows"] if r["action"] in ("fill", "overwrite", "add") and r["field"] != "Images"]
    p.shopify_id = pid
    p.data = {**(p.data or {}), "shopify": {"status": "pushed", "how": how, "added": added, "skipped": skipped,
                                            "fields": filled, "product_id": pid,
                                            "at": datetime.now(timezone.utc).isoformat(timespec="minutes")}}
    log(s, "shopify", f"{p.code}: {added} image(s), fields {', '.join(filled) or 'none'} ({how})")
    s.commit()
    return {"product_id": pid, "how": how, "added": added, "skipped": skipped, "fields": filled}


# ---------------------------------------------------------------- the three lists
def queue(s: Session) -> dict:
    listing = _listing(s)
    meta = get_setting(s, "shopify_listing", {}) or {}
    ready_in, ready_out, seen = [], [], set()
    for p in s.scalars(select(Product).where(Product.stage == "saved").order_by(Product.updated.desc())):
        lp = _lp(s, (p.data or {}).get("lib_key"))
        codes = _codes(lp, p)
        sh = (p.data or {}).get("shopify") or {"status": "ready"}
        info = info_for(s, p)
        hero = next((x for x in sorted(p.shots, key=lambda x: x.no) if x.image_path and x.status == "approved"), None)
        row = {"id": p.id, "code": p.code, "name": p.name, "brand": info["vendor"], "barcode": info["barcode"],
               "sku": info["sku"], "images": len([x for x in p.shots if x.image_path and x.status == "approved"]),
               "hero": f"/files/{hero.image_path}" if hero else "", **{f"shopify_{k}": v for k, v in sh.items()}}
        hit = next((c for c in codes if c in listing), None)
        if hit:
            seen.add(hit)
        (ready_in if (hit or not listing) else ready_out).append(row)
    not_ready = []
    if listing:
        from .library import _index
        idx = _index(s)
        stages = {}
        for p in s.scalars(select(Product)):
            k = (p.data or {}).get("lib_key")
            if k:
                stages[k] = p.stage
        for c, item in listing.items():
            if c in seen:
                continue
            lp = idx.get(c)
            if lp is None:
                not_ready.append({"code": item["code"], "name": "", "why": "not in the master sheet"})
                continue
            if _codes(lp, None) & seen:
                continue
            st = stages.get(lp.key, "")
            why = {"": "not shot yet", "queue": "in the studio queue", "needs_photo": "waiting for a photo",
                   "gen": "generating", "review": "waiting for your approval", "error": "stopped with an error",
                   "rejected": "rejected"}.get(st, st)
            not_ready.append({"code": lp.product_id or item["code"], "name": lp.name, "why": why, "key": lp.key})
    return {"listing": {"file": meta.get("file", ""), "at": meta.get("at", ""), "count": len(listing)},
            "ready_in_sheet": ready_in, "ready_not_in_sheet": ready_out, "in_sheet_not_ready": not_ready,
            "template": get_setting(s, "shopify_template", "") or ""}
