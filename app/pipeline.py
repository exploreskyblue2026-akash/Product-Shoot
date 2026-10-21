"""Autopilot pipeline: queue → analyse → 5 prompts → generate → QC (+retries) → approve → save (Drive/Shopify)."""
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from . import config
from .connectors import storage
from .dataset import build_prompts, library, live_rules, split
from .db import SessionLocal, Product, Shot, Source, Usage, get_setting, log, now
from .providers import get_provider

SHOT_NAMES = {1: "hero", 2: "use_case", 3: "detail", 4: "arrangement", 5: "lifestyle"}
_lock = threading.Lock()


def month_spend(s: Session) -> float:
    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return float(s.scalar(select(func.coalesce(func.sum(Usage.cost), 0.0)).where(Usage.ts >= start)) or 0.0)


def _charge(s: Session, p: Product, kind: str, model: str, cost: float, shot: Shot | None = None):
    s.add(Usage(product_id=p.id, kind=kind, model=model, cost=cost))
    p.cost = (p.cost or 0) + cost
    if shot is not None:
        shot.cost = (shot.cost or 0) + cost


def _cost_for(model: str) -> float:
    return config.COST_IMAGE_PRO if "pro" in model else config.COST_IMAGE_FAST


def analyse(s: Session, p: Product, prov, raw: bytes):
    rv = live_rules(s)
    cats = library(s, "categories")
    props = library(s, "props")
    ctx = {
        "schema": rv.data["vision_schema"]["schema"], "rules": rv.data["vision_schema"]["selection_rules"],
        "brands": sorted(library(s, "brands")), "categories": sorted(cats), "moods": sorted(library(s, "moods")),
        "props": {c: [k for k, v in props.items() if c in split(v["categories"])] for c in cats},
        "product": {"code": p.code, "name": p.name, "brand_id": p.brand_id, "category_id": p.category_id, **(p.data or {})},
    }
    a = prov.analyse(raw, ctx)
    _charge(s, p, "vision", config.MODEL_VISION, config.COST_VISION)
    p.analysis = a
    if not p.brand_id and a.get("brand_id") and a["brand_id"] != "_default":
        p.brand_id = a["brand_id"]
    if not p.category_id and a.get("category_id") in cats:
        p.category_id = a["category_id"]
    if not p.category_id:
        p.category_id = next(iter(cats))
    s.commit()


def process(pid: int, only: list[int] | None = None):
    """Generate (or regenerate) shots for one product. Commits after every step so the UI can follow along."""
    prov = get_provider()
    with SessionLocal() as s:
        p = s.get(Product, pid)
        raw = storage.read(p.raw_path)
        p.stage, p.error = "gen", ""
        s.commit()
        if not p.analysis:
            analyse(s, p, prov, raw)
        rv = live_rules(s)
        shots = {sh.no: sh for sh in p.shots}
        for no, kind in SHOT_NAMES.items():
            if no not in shots:
                shots[no] = Shot(no=no, kind=kind)
                p.shots.append(shots[no])
        s.commit()
        notes = {sh.no: sh.note for sh in shots.values() if sh.note}
        prompts = {x["no"]: x for x in build_prompts(s, p, p.analysis or {}, rv, notes)}
        retries = int(get_setting(s, "retries", 2))
        todo = only or [no for no, sh in shots.items() if sh.status in ("wait", "gen", "fail") or not sh.image_path]
        for no in sorted(todo):
            sh = shots[no]
            spec = prompts[no]
            model = get_setting(s, spec["model_setting"], config.MODEL_IMAGE_FAST)
            sh.status, sh.prompt, sh.model, sh.rule_version, sh.qc = "gen", spec["prompt"], model, rv.version, {}
            s.commit()
            prompt = spec["prompt"]
            result = None
            for attempt in range(retries + 1):
                sh.attempts = (sh.attempts or 0) + 1
                img = storage.to_output(prov.generate(prompt, raw, model))
                _charge(s, p, "image", model, _cost_for(model), sh)
                rel = storage.save(f"shots/{storage.safe(p.code)}/{no}-{sh.kind}-a{sh.attempts}.jpg", img)
                result = prov.qc(raw, img, {"kind": sh.kind, "no": no}, rv.data)
                _charge(s, p, "qc", config.MODEL_VISION, config.COST_VISION, sh)
                sh.image_path, sh.qc = rel, result
                s.commit()
                if all(result["checks"].values()):
                    break
                failed = [f"{k}: {result['reasons'].get(k, 'failed')}" for k, v in result["checks"].items() if not v]
                prompt = spec["prompt"] + "\n\n[CORRECTION — previous attempt failed QC]\nFix: " + "; ".join(failed)
            sh.status = "pass" if all(result["checks"].values()) else "fail"
            sh.note = ""
            s.commit()
        s.refresh(p)
        all_pass = len(p.shots) == 5 and all(sh.status in ("pass", "approved") for sh in p.shots)
        if all_pass and get_setting(s, "auto_approve", True) and get_setting(s, "autopilot", False):
            approve(s, p, "autopilot")
        else:
            p.stage = "review"
            log(s, "autopilot" if get_setting(s, "autopilot", False) else "system", f"{p.code} ready for review")
        p.data = {k: v for k, v in (p.data or {}).items() if k != "run_now"}
        s.commit()


def approve(s: Session, p: Product, who: str):
    """Approve every shot, write the final files, copy to Drive and Shopify when configured."""
    naming = get_setting(s, "file_naming", "{code}_{colour}_{no}-{shot}.jpg")
    finals = []
    for sh in p.shots:
        if not sh.image_path:
            continue
        sh.status = "approved"
        name = naming.format(code=p.code, colour=storage.safe(p.colourway or "standard"), no=sh.no, shot=sh.kind)
        data = storage.read(sh.image_path)
        storage.save(f"final/{storage.safe(p.code)}/{name}", data)
        finals.append((name, data, f"{p.name} — {sh.kind.replace('_', ' ')}"))
    p.stage = "saved"
    errors = []
    folder = config.OUTPUT_DRIVE_FOLDER_ID or (get_setting(s, "output_drive_folder", "") or "")
    if folder:
        try:
            from .connectors import google
            sub = google.ensure_subfolder(google.folder_id(folder), p.code)
            for name, data, _ in finals:
                google.upload(sub, name, data)
        except Exception as e:
            errors.append(f"Drive upload failed: {e}")
    if get_setting(s, "publish_shopify", False):
        try:
            from .connectors import shopify
            p.shopify_id = shopify.publish(p, finals)
        except Exception as e:
            errors.append(f"Shopify publish failed: {e}")
    p.error = " · ".join(errors)[:900]
    log(s, who, f"Approved {p.code}: {len(finals)} shots saved" + (f" ({p.error})" if errors else ""))
    s.commit()


def rescan_watched(s: Session):
    from .ingest import scan
    due = now() - timedelta(minutes=config.WATCH_INTERVAL_MIN)
    for src in s.scalars(select(Source).where(Source.watch == True)):  # noqa: E712
        last = src.last_scan.replace(tzinfo=timezone.utc) if src.last_scan and src.last_scan.tzinfo is None else src.last_scan
        if not last or last < due:
            scan(s, src)


def next_job(s: Session) -> int | None:
    """Manual 'run now' jobs first; otherwise the oldest queued product while autopilot is on."""
    for p in s.scalars(select(Product).where(Product.stage.in_(["queue", "gen"])).order_by(Product.id)):
        if (p.data or {}).get("run_now"):
            return p.id
    if not get_setting(s, "autopilot", False):
        return None
    if get_setting(s, "budget_action", "pause") == "pause" and month_spend(s) >= float(get_setting(s, "budget_month", 150)):
        from .db import set_setting
        set_setting(s, "autopilot", False)
        log(s, "system", "Autopilot paused: monthly budget reached")
        s.commit()
        return None
    p = s.scalars(select(Product).where(Product.stage.in_(["gen", "queue"])).order_by(Product.stage.asc(), Product.id)).first()  # "gen" (resume) before "queue"
    return p.id if p else None


def tick() -> bool:
    """One worker step. Returns True when it did work."""
    if not _lock.acquire(blocking=False):
        return False
    try:
        with SessionLocal() as s:
            rescan_watched(s)
            pid = next_job(s)
            only = None
            if pid:
                only = (s.get(Product, pid).data or {}).get("only_shots")
        if not pid:
            return False
        try:
            process(pid, only)
        except Exception as e:
            traceback.print_exc()
            with SessionLocal() as s:
                p = s.get(Product, pid)
                p.stage, p.error = "error", str(e)[:900]
                p.data = {k: v for k, v in (p.data or {}).items() if k not in ("run_now", "only_shots")}
                for sh in p.shots:
                    if sh.status == "gen":
                        sh.status = "wait"
                log(s, "system", f"{p.code} stopped: {p.error[:160]}")
                s.commit()
        return True
    finally:
        _lock.release()


def run_forever():
    while True:
        try:
            worked = tick()
        except Exception:
            traceback.print_exc()
            worked = False
        time.sleep(0.2 if worked else config.WORKER_INTERVAL)


def start_background():
    threading.Thread(target=run_forever, daemon=True, name="autopilot").start()
