"""Shopify queue: approved products wait here; push adds their final shots to the matching store product."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import storage
from .db import LibProduct, Product, log

SHOT_LABEL = {"hero": "Hero", "detail": "Detail", "use_case": "In use", "lifestyle": "Lifestyle"}


def info_for(s: Session, p: Product) -> dict:
    """Store-facing details from the master sheet (title, SKU, barcode, price, brand, type)."""
    d = p.data or {}
    lp = s.scalar(select(LibProduct).where(LibProduct.key == d.get("lib_key"))) if d.get("lib_key") else None
    ld = (lp.data or {}) if lp else {}
    titles = ld.get("titles") or {}
    prices = ld.get("prices") or {}
    title = (titles.get("seo optimised title") or titles.get("seo optimized title") or (lp.name if lp else "") or p.name or p.code)
    price = next((prices[k] for k in ("new sp", "selling cost", "price", "mrp", "new mrp", "maximum-retail-price")
                  if prices.get(k) not in (None, "", "Not Found")), "")
    skus = list(dict.fromkeys([x for x in (ld.get("skus") or []) + [lp.product_id if lp else "", p.code] if x]))
    return {"title": str(title)[:250], "vendor": d.get("brand_label") or (lp.brand if lp else p.brand_id),
            "type": (lp.category if lp else "") or p.category_id, "sku": skus[-1] if skus else p.code, "skus": skus,
            "barcode": (lp.ean if lp else ""), "price": price, "tags": [t for t in [(d.get("brand_label") or "").lower()] if t]}


def images_for(p: Product) -> list[tuple[str, bytes, str]]:
    """Approved shots in website order (hero first), with alt text."""
    out = []
    for sh in sorted(p.shots, key=lambda x: x.no):
        if sh.image_path and sh.status == "approved":
            alt = f"{p.name or p.code} | {SHOT_LABEL.get(sh.kind, sh.kind)}"
            out.append((f"{storage.safe(p.code)}_{sh.no}-{sh.kind}.jpg", storage.read(sh.image_path), alt[:500]))
    return out


def push_one(s: Session, p: Product) -> dict:
    from .connectors import shopify
    imgs = images_for(p)
    if not imgs:
        raise RuntimeError("No approved shots to push")
    info = info_for(s, p)
    r = shopify.push(info, imgs)
    p.shopify_id = r["product_id"]
    p.data = {**(p.data or {}), "shopify": {"status": "pushed", "how": r["how"], "added": r["added"],
                                            "skipped": r["skipped"], "product_id": r["product_id"],
                                            "at": datetime.now(timezone.utc).isoformat(timespec="minutes")}}
    log(s, "shopify", f"{p.code}: {r['added']} image(s) added to Shopify ({r['how']})")
    s.commit()
    return r


def queue(s: Session) -> list[dict]:
    rows = []
    for p in s.scalars(select(Product).where(Product.stage == "saved").order_by(Product.updated.desc())):
        sh = (p.data or {}).get("shopify") or {"status": "ready"}
        info = info_for(s, p)
        hero = next((x for x in sorted(p.shots, key=lambda x: x.no) if x.image_path and x.status == "approved"), None)
        rows.append({"id": p.id, "code": p.code, "name": p.name, "brand": info["vendor"], "barcode": info["barcode"],
                     "sku": info["sku"], "price": info["price"], "images": len([x for x in p.shots if x.image_path and x.status == "approved"]),
                     "hero": f"/files/{hero.image_path}" if hero else "", **{f"shopify_{k}": v for k, v in sh.items()}})
    return rows
