"""Library queue: the first queue. A list of product codes (any brand, mixed, no brand needed) is prepared one by one:

  1 identify  - exact code / item code / EAN in the master sheet (the sheet knows the brand);
                not in the sheet -> searched in every brand's website index; created only if the official page shows it
  2 photos    - official website FIRST (verified page), plus every raw photo already filed under that code
                (Drive, uploads); all of them are clustered under the one product code
  3 analysis  - best model: photos check + product understanding + shot plans (set, props, light)
  4 ready     - then sent to the Studio queue automatically (setting) or with one click

Each step's result is stored on the item so the Library shows exactly where every code stands.
"""
import re
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import LibImage, LibProduct, get_setting, log, set_setting


def _say(msg: str):
    """Live progress on the Library page ('Preparing 3/10 · 2033 …')."""
    try:
        from .main import _lib_job
        _lib_job["msg"] = "Preparing the Library queue · " + msg
    except Exception:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="minutes")


def add_codes(s: Session, text: str, replace: bool = False) -> dict:
    """Paste or sheet: one code per line (or comma separated). Appends to the Library queue."""
    from .library import _index, resolve
    codes = [c.strip() for c in re.split(r"[\n,;\t]+", text or "") if c.strip()]
    items = [] if replace else list(get_setting(s, "shoot_list", []) or [])
    have = {i["code"] for i in items}
    idx, added = _index(s), 0
    base = max([i.get("priority", 0) for i in items] + [1000])
    for n, c in enumerate(codes):
        if c in have:
            continue
        p, how = resolve(c, idx)
        items.append({"code": c, "key": p.key if p else "", "match": how or "not in master sheet", "shots": [],
                      "priority": base + n + 1, "note": "", "prep": {"state": "waiting"}})
        have.add(c)
        added += 1
    set_setting(s, "shoot_list", items)
    log(s, "library", f"Library queue: {added} code(s) added")
    s.commit()
    return {"added": added, "total": len(items)}


def _brands_with_site(s: Session) -> list[tuple[str, dict]]:
    from .brands import brand_rows
    return [(k, r.data or {}) for k, r in brand_rows(s).items() if not k.startswith("_") and (r.data or {}).get("website")]


def _find_on_websites(s: Session, code: str, deadline: float) -> tuple[str, dict] | None:
    """A code that is not in the master sheet: look for it in every brand's website index (reading more pages if the
    index is not complete yet). Returns (brand_key, verified page facts)."""
    from .connectors import website
    from .library import web_index
    digits = re.sub(r"\D", "", code)
    ean = digits if len(digits) in (12, 13, 14) else ""
    for bkey, b in _brands_with_site(s):
        while True:
            idx = get_setting(s, f"webindex:{bkey}", {}) or {}
            url = website.lookup(idx, ean, [code])
            if url:
                try:
                    return bkey, website.fetch_product(url, ean, [code])
                except Exception:
                    break
            urls = idx.get("urls") or []
            if (urls and idx.get("done", 0) >= len(urls)) or time.time() > deadline:
                break
            before = (len(urls), idx.get("done", 0))
            try:
                r = web_index(s, bkey, budget=150)
            except Exception:
                break
            if (r["pages"], r["indexed"]) == before or not r["pages"]:
                break                                   # site unreadable or nothing new: stop, don't loop
    return None


def _create_from_web(s: Session, code: str, bkey: str, facts: dict, b: dict) -> LibProduct:
    from .library import norm
    key = norm(code)
    lp = s.scalar(select(LibProduct).where(LibProduct.key == key))
    if lp is None:
        lp = LibProduct(key=key[:80], product_id=code[:80], name=(facts.get("title") or code)[:300], brand=(b.get("brand_name") or bkey)[:80],
                        category="", ean=(facts.get("eans") or [""])[0] if len(facts.get("eans") or []) == 1 else "",
                        data={"created_from": "official website", "web": {k: facts.get(k) for k in
                              ("url", "title", "description", "dimensions", "verified_by")}})
        s.add(lp)
        s.commit()                                      # keep it even if a later step fails
    return lp


def _prep_one(s: Session, it: dict, n_it: int, total: int, deadline: float, use_web: bool) -> dict:
    """One code: identify -> photos (store, then brand site) -> analysis. Works on a copy of the item and returns it."""
    from .library import _brand_of, web_fetch, _index, resolve
    from . import planner
    from .providers import get_provider
    it = dict(it)
    prep = dict(it.get("prep") or {})
    prep["at"] = _now()
    _say(f"{n_it}/{total} · {it['code']}")
    # 1 identify
    if not it.get("key"):
        p, how = resolve(it["code"], _index(s))
        if p:
            it["key"], it["match"] = p.key, how
        elif use_web and not re.fullmatch(r"\d{1,7}", it["code"].strip().removesuffix(".0")):
            # a short number is a SkyBlue item code: it only exists in the master sheet, never on brand websites
            hit = _find_on_websites(s, it["code"], min(deadline, time.time() + 180))
            if hit:
                bkey, facts = hit
                b = dict(_brands_with_site(s)).get(bkey, {})
                lp = _create_from_web(s, it["code"], bkey, facts, b)
                it["key"], it["match"] = lp.key, f"official website ({bkey})"
        if not it.get("key"):
            short = re.fullmatch(r"\d{1,7}", it["code"].strip().removesuffix(".0"))
            prep.update(state="not_found", why=("SkyBlue item code not in the master sheet: check the sheet's Item Code column"
                                                if short else "Not in the master sheet and not found on any brand website"))
            it["prep"] = prep
            return it
    lp = s.scalar(select(LibProduct).where(LibProduct.key == it["key"]))
    bkey, b = _brand_of(s, lp)
    prep["brand"] = b.get("brand_name") or lp.brand or bkey
    _say(f"{n_it}/{total} · {it['code']} ({prep['brand']}): photos, then analysis")
    # 2 photos: 1 our own SkyBlue store, 2 the brand's official website, plus every raw photo of this code
    has_store = s.scalar(select(LibImage.id).where(LibImage.key == it["key"], LibImage.source == "store")) is not None
    from .connectors import shopify as _shop
    exported = bool(((lp.data or {}).get("store_images")))
    if not has_store and get_setting(s, "store_first", True) and (exported or (get_setting(s, "use_shopify", True) and _shop.configured())):
        from .library import store_fetch
        s.commit()
        try:
            r = store_fetch(s, it["key"])
            prep["store"] = f"found ({r['matched_by']}) · {r['added']} photo(s)"
            has_store = r["added"] > 0 or has_store
        except ValueError as e:
            prep["store"] = str(e)[:120]
        except Exception as e:
            s.rollback()
            prep["store"] = f"store check failed: {str(e)[:100]}"
    elif not has_store and not exported and not _shop.configured():
        prep["store"] = "store not connected"
    elif has_store:
        prep.setdefault("store", "found earlier")
    has_web = s.scalar(select(LibImage.id).where(LibImage.key == it["key"], LibImage.source == "website")) is not None
    if has_store and not has_web:
        prep["web"] = "not needed: photos from the SkyBlue store"
    elif use_web and b.get("website") and not has_web:
        s.commit()
        try:
            r = web_fetch(s, it["key"])
            prep["web"] = f"found · {r['added']} photo(s) · verified by {r['verified_by']}"
        except Exception as e:
            s.rollback()
            prep["web"] = f"not found: {str(e)[:120]}"
    elif not b.get("website"):
        prep["web"] = "no website set for this brand"
    else:
        prep.setdefault("web", "found earlier" if has_web else "website switched off")
    photos = list(s.scalars(select(LibImage).where(LibImage.key == it["key"], LibImage.dup_of.is_(None),
                                                   LibImage.cut_status != "skip")))
    photos = [i for i in photos if not (i.meta or {}).get("discarded")]
    prep["photos"] = len(photos)
    prep["website_photos"] = sum(1 for i in photos if i.source == "website")
    prep["store_photos"] = sum(1 for i in photos if i.source == "store")
    if not photos:
        prep.update(state="needs_photo", why="No photo on the website or in Raw input for this code")
        it["prep"] = prep
        return it
    # 3 analysis (best model)
    if get_setting(s, "planner_on", True):
        rec = (lp.data or {}).get("plan")
        if not rec or rec.get("stale") or rec.get("status") != "ok":
            try:
                rec = planner.make_plan(s, it["key"], get_provider())
                from .db import Usage
                from . import config
                s.add(Usage(kind="plan", model=rec["model"], cost=config.COST_PLANNER))
                s.commit()
            except Exception as e:
                s.rollback()
                prep.update(state="error", why=f"Analysis failed: {str(e)[:160]}")
                it["prep"] = prep
                return it
        if rec.get("status") != "ok":
            prep.update(state="needs_photo", why="Every photo was discarded by the analysis")
            it["prep"] = prep
            return it
        if planner.needs_review(s, rec):
            prep.update(state="review", why=planner.review_reason(rec))
            it["prep"] = prep
            return it
    prep.update(state="ready", why="")
    it["prep"] = prep
    return it


def prepare(s: Session, minutes: int = 25, stop=None) -> str:
    """Run every waiting code through identify -> photos (store first) -> analysis -> ready (-> studio).
    Several codes are prepared at the same time (setting prep_workers, default 3); each worker has its own database
    session, and only this function writes the queue, so nothing is lost."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .db import SessionLocal
    deadline = time.time() + minutes * 60
    items = list(get_setting(s, "shoot_list", []) or [])
    use_web = get_setting(s, "use_websites", True)
    auto_send = get_setting(s, "auto_send_studio", False)
    workers = max(1, min(6, int(get_setting(s, "prep_workers", 3) or 3)))
    todo = [(n, it) for n, it in enumerate(items, 1)
            if (it.get("prep") or {}).get("state") not in ("ready", "sent", "not_found")]
    total, done = len(items), 0

    def run(n, it):
        if time.time() > deadline or (stop and stop()):
            return n, None
        with SessionLocal() as ws:
            try:
                return n, _prep_one(ws, it, n, total, deadline, use_web)
            except Exception as e:
                ws.rollback()
                return n, {**it, "prep": {**(it.get("prep") or {}), "state": "error", "why": str(e)[:160], "at": _now()}}

    if workers == 1 or len(todo) <= 1:
        results = (run(n, it) for n, it in todo)
    else:
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="prep")
        results = (f.result() for f in as_completed([pool.submit(run, n, it) for n, it in todo]))
    for n, new in results:
        if new is None:
            continue
        cur = list(get_setting(s, "shoot_list", []) or [])          # re-read: codes may have been added meanwhile
        for i, x in enumerate(cur):
            if x["code"] == new["code"]:
                cur[i] = {**x, **{k: new[k] for k in ("key", "match", "prep") if k in new}}
        items = cur
        if (new.get("prep") or {}).get("state") == "ready":
            done += 1
            if auto_send:
                send(s, [new["key"]], items)
        set_setting(s, "shoot_list", items)
        s.commit()
    if workers > 1 and len(todo) > 1:
        pool.shutdown(wait=False)
    items = list(get_setting(s, "shoot_list", []) or [])
    log(s, "library", f"Library queue prepared: {done} ready")
    s.commit()
    states = {}
    for it in items:
        st = (it.get("prep") or {}).get("state", "waiting")
        states[st] = states.get(st, 0) + 1
    return ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in states.items())


def send(s: Session, keys: list[str] | None = None, items: list | None = None) -> dict:
    """Ready codes -> Studio queue (in Library-queue order)."""
    from .v2 import load_items
    own = items is None
    items = items if items is not None else list(get_setting(s, "shoot_list", []) or [])
    pick = [it for it in items if it.get("key") and (it.get("prep") or {}).get("state") == "ready"
            and (keys is None or it["key"] in keys)]
    if not pick:
        return {"sent": 0}
    r = load_items(s, [{"key": it["key"], "code": it["code"], "shots": it.get("shots") or [], "note": it.get("note", ""),
                        "priority": it.get("priority", 0)} for it in pick])
    for it in pick:
        it["prep"] = {**(it.get("prep") or {}), "state": "sent", "sent_at": _now()}
    if own:
        set_setting(s, "shoot_list", items)
    s.commit()
    return {"sent": len(pick), **({k: v for k, v in r.items() if isinstance(v, int)} if isinstance(r, dict) else {})}


def reset_item(s: Session, code: str):
    items = list(get_setting(s, "shoot_list", []) or [])
    for it in items:
        if it["code"] == code:
            it["prep"] = {"state": "waiting"}
    set_setting(s, "shoot_list", items)
    s.commit()
