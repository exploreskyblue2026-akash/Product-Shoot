"""Autopilot pipeline, one path: Library photos → one analysis (memory, checklist gate, triage, 4 plans) →
reuse / edit / generate each slot → QC → approve → save (Drive/Shopify)."""
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from . import config
from .connectors import storage
from .dataset import live_rules
from .db import SessionLocal, Product, Shot, Source, Usage, get_setting, log, now
from .providers import get_provider

SHOT_NAMES = {1: "hero", 2: "lifestyle", 3: "use_case", 4: "detail"}      # listing order: hero, lifestyle, use case, detail/feature
_lock = threading.Lock()
STOP = threading.Event()          # set by the Stop button: finish nothing new, stop between attempts


def month_spend(s: Session) -> float:
    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return float(s.scalar(select(func.coalesce(func.sum(Usage.cost), 0.0)).where(Usage.ts >= start)) or 0.0)


def _charge(s: Session, p: Product, kind: str, model: str, cost: float, shot: Shot | None = None):
    s.add(Usage(product_id=p.id, kind=kind, model=model, cost=cost))
    p.cost = (p.cost or 0) + cost
    if shot is not None:
        shot.cost = (shot.cost or 0) + cost


CRITICAL = ("shape_match", "label_text", "no_artifacts")


def _passed(result: dict, mode: str) -> bool:
    checks = result.get("checks", {})
    keys = CRITICAL if mode == "light" else checks.keys()
    return all(checks.get(k, True) for k in keys)


def day_spend(s: Session) -> float:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return float(s.scalar(select(func.coalesce(func.sum(Usage.cost), 0.0)).where(Usage.ts >= start)) or 0.0)


def _model_for(s: Session, kind: str) -> tuple[str, str]:
    mode = get_setting(s, "cost_mode", "economy")
    hero_m, other_m, size = config.COST_MODES.get(mode, config.COST_MODES["economy"])
    own = (get_setting(s, f"model_{kind}", "") or "").strip()      # Admin: a model chosen for this one shot
    if own.startswith("openrouter:") and not get_setting(s, "use_openrouter", False):
        own = ""                                                   # OpenRouter is switched off: Gemini only
    if own:
        return (own if own.startswith("openrouter:") else config.model_name(own)), size
    pick = hero_m if kind in ("hero", "detail") else other_m
    # the model boxes in Admin -> API & models are what "pro" and "fast" mean (blank = built-in default)
    chosen = {"pro": get_setting(s, "model_hero_detail", ""), "fast": get_setting(s, "model_other", "")}.get(pick) or ""
    return config.model_name(chosen.strip() or config.mode_model(pick)), size


def _sheet(images: list) -> bytes:
    """Reference photos side by side in one image, so QC compares against every view at once."""
    import io as _io
    from PIL import Image as _I
    ims = []
    for b in images:
        if not b:
            continue
        try:
            im = _I.open(_io.BytesIO(b)).convert("RGB")
            im.thumbnail((900, 900))
            ims.append(im)
        except Exception:
            pass
    if len(ims) <= 1:
        return images[0] if images else b""
    h = max(i.height for i in ims)
    out = _I.new("RGB", (sum(i.width for i in ims) + 20 * (len(ims) - 1), h), (255, 255, 255))
    x = 0
    for i in ims:
        out.paste(i, (x, (h - i.height) // 2))
        x += i.width + 20
    buf = _io.BytesIO()
    out.save(buf, "JPEG", quality=90)
    return buf.getvalue()


REVIEWER = "\n\nREVIEWER ADJUSTMENT (highest priority, never change the product): "


def requested_shots(s: Session, p: Product) -> list[int]:
    """The shots this product should get: the ones its shoot-sheet row asks for, else the default in Admin."""
    want = (p.data or {}).get("shots") or []
    if want:
        return sorted({int(x) for x in want if int(x) in SHOT_NAMES})
    return [int(x) for x in (get_setting(s, "default_shots", [1]) or [1]) if int(x) in SHOT_NAMES]


def process(pid: int, only: list[int] | None = None):
    """Generate (or regenerate) shots for one product. There is ONE path, the same for every product however it was
    added:  Library photos of its code -> one analysis (product memory, checklist gate, photo triage, four shot
    plans) -> each slot reused / lightly edited / generated from that plan -> QC.
    Commits after every step so the UI can follow along."""
    from . import library as lib, planner
    from .dataset import memory_block
    prov = get_provider()
    with SessionLocal() as s:
        p = s.get(Product, pid)
        p.stage, p.error = "gen", ""
        s.commit()
        # 1 · every product is a Library product: its photos are filed under its code
        key = lib.adopt_product(s, p)
        lp = s.scalar(select(lib.LibProduct).where(lib.LibProduct.key == key))
        # 2 · one analysis per product (re-used until its photos change or you ask for a new one)
        rec = (lp.data or {}).get("plan")
        if not rec or rec.get("stale") or rec.get("status") != "ok":
            rec = planner.make_plan(s, key, prov, charge=lambda used: _charge(s, p, "plan", used, config.COST_PLANNER))
        if rec.get("status") != "ok":
            p.stage, p.error = "error", "Every photo of this product was discarded by the analysis. Check the Library."
            s.commit()
            return
        # 3 · the checklist gate: all A checks and enough B checks, otherwise a person reviews the plan first
        if planner.needs_review(s, rec):
            p.stage = "plan"
            p.error = "Shot plan needs your approval in the Library: " + planner.review_reason(rec)
            p.data = {k: v for k, v in (p.data or {}).items() if k not in ("run_now", "only_shots")}
            s.commit()
            return
        s.refresh(lp)
        plan = planner.for_product(rec["json"], lp)
        static, look = planner.static_rules(s), planner.house_look(s)
        mem = (lp.data or {}).get("memory") or {}
        p.data = {**(p.data or {}), "memory": mem}
        rv = live_rules(s)                                # the automatic check's rules
        shots = {sh.no: sh for sh in p.shots}
        for no, kind in SHOT_NAMES.items():
            if no not in shots:
                shots[no] = Shot(no=no, kind=kind)
                p.shots.append(shots[no])
        s.commit()
        retries = int(get_setting(s, "retries", 0))
        qc_mode = get_setting(s, "qc_mode", "light")
        cap = float(get_setting(s, "max_cost_product", 0.30))
        hero_gate = bool(get_setting(s, "hero_gate", True))
        # only verified (not discarded) photos of this exact code: our store first, then the brand site, then the rest
        rank = {"front": 0, "unknown": 1, "": 1, "angle": 2, "side": 3}     # whole-product views first
        src_of = {i.path: i.source for i in s.scalars(select(lib.LibImage).where(
            lib.LibImage.key == key, lib.LibImage.source.in_(("store", "website"))))}
        kept = [r["path"] for r in sorted(lib.refs_with_angles(s, key, 6),
                                          key=lambda r: ({"store": 0, "website": 1}.get(src_of.get(r["path"]), 2),
                                                         rank.get(r["angle"] or "", 5)))]
        if not kept:
            p.stage, p.error = "needs_photo", "No usable photo in the Library for this code"
            s.commit()
            return
        ids = rec.get("photos") or []

        def _rel(ix):                                     # a photo the analysis pointed at (by its index)
            try:
                img = s.get(lib.LibImage, ids[int(ix)])
            except (ValueError, TypeError, IndexError):
                return None
            return img.path if img is not None and img.path in kept else None
        best = _rel(plan.get("best_reference"))
        if best:
            kept = [best] + [k for k in kept if k != best]
        plan_refs = {k: _rel(((plan.get("shots") or {}).get(k) or {}).get("reference_photo")) for k in SHOT_NAMES.values()}
        approved = ""
        if mem.get("approved") and mem.get("facts"):
            approved = memory_block(mem).replace("[PRODUCT FACTS: reproduce exactly, these override anything else]",
                                                 "[PRODUCT FACTS approved by a person: these override anything else]")
        facts = planner.qc_facts(plan)
        if mem.get("facts"):
            facts += "; " + memory_block(mem).split("\n", 1)[-1].replace("\n", "; ")
        todo = only or [no for no in requested_shots(s, p)
                        if shots[no].status in ("wait", "gen", "fail") or not shots[no].image_path]
        stop_reason = ""
        for no in sorted(todo):
            sh = shots[no]
            model, size = _model_for(s, sh.kind)
            note = (sh.note or "").strip()
            override = (((p.data or {}).get("prompt_override") or {}).get(str(no)) or "").strip()
            sh.status, sh.model, sh.rule_version, sh.qc = "gen", model, rv.version, {}
            s.commit()
            # a supplied photo that already fits this slot is used as it is, or after a small edit (never the hero)
            if sh.kind != "hero" and not override and get_setting(s, "reuse_inputs", True):
                if _reuse_input(s, p, sh, rec, prov, model, size, rv, qc_mode):
                    continue
            hero_ref = None
            h = shots.get(1)
            if no != 1 and get_setting(s, "hero_review", True) and h is not None and h.image_path and h.status != "approved":
                sh.status = "wait"
                s.commit()
                stop_reason = "Approve the hero first: shots 2-4 copy it, so a wrong hero would spoil all of them"
                break
            if no != 1 and h is not None and h.image_path and h.status in ("pass", "approved", "unchecked"):
                try:
                    hero_ref = storage.read(h.image_path)
                except Exception:
                    hero_ref = None
            main_rel = plan_refs.get(sh.kind) or kept[0]
            shot_raw = storage.read(main_rel)
            extra_rels, refs = [k for k in kept if k != main_rel], []
            for rel in extra_rels:
                try:
                    refs.append(storage.read(rel))
                except Exception:
                    pass
                if len(refs) >= 2:
                    break
            sent_rels = [main_rel] + extra_rels[:len(refs)]
            # the prompt: written from the analysed plan, the fixed slot rules and the house look. Nothing else.
            base_prompt = override or planner.render(plan, sh.kind, static, anchored=bool(hero_ref),
                                                     approved_facts=approved, look=look)
            if note:
                base_prompt += REVIEWER + note
            prompt = base_prompt
            call_refs = ([hero_ref] if hero_ref else []) + refs[:2]
            sh.prompt = prompt
            sent = {"photos": sent_rels, "hero": bool(hero_ref), "override": bool(override), "plan": True}
            result = None
            escalate = (get_setting(s, "escalate_model", "") or "").strip()
            if escalate.startswith("openrouter:") and not get_setting(s, "use_openrouter", False):
                escalate = ""
            escalated = False
            max_tries = max(1, int(get_setting(s, "max_attempts_shot", 2) or 2))   # hard cap per shot per run
            for attempt in range(min(max_tries, retries + 1 + (1 if escalate and escalate != model else 0))):
                if STOP.is_set():
                    stop_reason = "Stopped by you"
                    break
                est = config.image_cost(model, size) + config.COST_VISION
                if p.cost + est > cap:
                    stop_reason = f"Stopped at the ${cap:.2f} per-product cost limit"
                    break
                sh.attempts = (sh.attempts or 0) + 1
                try:
                    img = storage.to_output(prov.generate(prompt, shot_raw, model, size, refs=call_refs, style=None))
                except Exception as e:                    # model unavailable / refused: use the upgrade model once
                    if not escalate or escalated or escalate == model:
                        raise
                    print(f"[pipeline] {model} failed for {p.code} shot {no}, using {escalate}: {str(e)[:160]}")
                    model, escalated = escalate, True
                    img = storage.to_output(prov.generate(prompt, shot_raw, model, size, refs=call_refs, style=None))
                used = getattr(prov, "last_model", None) or model     # may differ after a fallback
                sh.model = used
                _charge(s, p, "image", used, config.image_cost(used, size), sh)
                rel = storage.save(f"shots/{storage.safe(p.code)}/{no}-{sh.kind}-a{sh.attempts}.jpg", img)
                try:
                    qref = _sheet([hero_ref, shot_raw] if hero_ref else [shot_raw] + refs[:2])
                    result = prov.qc(qref, img, {"kind": sh.kind, "no": no, "facts": facts, "sheet": True,
                                                 "vs_hero": bool(hero_ref)}, rv.data)
                    _charge(s, p, "qc", config.model_name(config.MODEL_VISION), config.COST_VISION, sh)
                except Exception as e:                   # keep the image; a person checks it instead
                    print(f"[pipeline] QC failed for {p.code} shot {no}: {e}")
                    result = {"checks": {}, "reasons": {"qc": f"Automatic QC unavailable: {str(e)[:200]}"},
                              "unchecked": True}
                result["how"] = {"method": "fresh"}
                result["sent"] = {**sent, "model": sh.model or model, "size": size}
                sh.image_path, sh.qc = rel, result
                s.commit()
                if result.get("unchecked") or _passed(result, qc_mode):
                    break
                failed = [f"{k}: {result['reasons'].get(k, 'failed')}" for k, v in result["checks"].items()
                          if not v and (qc_mode != "light" or k in CRITICAL)]
                lib.learn_from_fail(s, key, [f"{sh.kind}: {r}" for r in failed])
                prompt = base_prompt + "\n\n[CORRECTION — previous attempt failed QC]\nFix: " + "; ".join(failed)
                if escalate and not escalated and escalate != model:      # cheap model failed: one try with the best
                    model, escalated = escalate, True
                    sh.model = model
                elif attempt >= retries:
                    break
            if result is None:
                sh.status = "wait"
            elif result.get("unchecked"):
                sh.status = "unchecked"
            else:
                sh.status = "pass" if _passed(result, qc_mode) else "fail"
            sh.note = ""
            s.commit()
            if stop_reason:
                break
            if STOP.is_set():
                stop_reason = "Stopped by you"
                break
            if hero_gate and no == 1 and sh.status == "fail" and not only:
                stop_reason = "Hero failed QC, other shots not generated (saves credits)"
                break
        _finish(s, p, stop_reason)


def _reuse_input(s: Session, p: Product, sh: Shot, rec: dict, prov, model: str, size: str, rv, qc_mode: str) -> bool:
    """Input-image triage: a supplied photo that already fits this slot is used as-is (crop / resize to 1200x1500, no
    credits) or after a small preserving edit (cheap). Returns True when the slot is filled this way."""
    from . import planner
    from .library import LibImage
    pick = planner.reuse_for(rec, sh.kind)
    if not pick:
        return False
    img = s.get(LibImage, pick["image_id"])
    if img is None or min(img.w or 0, img.h or 0) < int(get_setting(s, "reuse_min_px", 1000)):
        return False
    try:
        data = storage.read(img.path)
    except Exception:
        return False
    sh.attempts = (sh.attempts or 0) + 1
    if pick["verdict"] == "edit" and pick["edits"]:
        edit_model = (get_setting(s, "model_edit", "") or "").strip() or model
        prompt = planner.EDIT_PROMPT.format(edits="\n".join(f"- {e}" for e in pick["edits"]))
        try:
            out = storage.to_output(prov.generate(prompt, data, edit_model, size, refs=[], style=None))
        except Exception as e:
            print(f"[pipeline] light edit failed for {p.code} {sh.kind}: {e}")
            return False
        used = getattr(prov, "last_model", None) or edit_model
        _charge(s, p, "image", used, config.image_cost(used, size), sh)
        how = {"method": "light_edit", "source": img.path, "edits": pick["edits"]}
        try:
            result = prov.qc(_sheet([data]), out, {"kind": sh.kind, "no": sh.no, "facts": planner.qc_facts(rec.get("json") or {}),
                                                   "sheet": True, "vs_hero": False}, rv.data)
            _charge(s, p, "qc", config.model_name(config.MODEL_VISION), config.COST_VISION, sh)
            status = "pass" if _passed(result, qc_mode) else "fail"
        except Exception as e:
            result, status = {"checks": {}, "reasons": {"qc": f"Automatic QC unavailable: {str(e)[:200]}"}, "unchecked": True}, "unchecked"
        sh.prompt, sh.model = prompt, used
    else:
        out = storage.to_output(data)                       # 4:5, 1200x1500, compressed: no credits
        how = {"method": "reused", "source": img.path, "exact": True}
        result, status = {"checks": {"product_exact": True}, "reasons": {}}, "pass"
        sh.prompt, sh.model = f"Input photo used as-is ({pick['why']})", "none"
    result["how"] = how
    result["sent"] = {"photos": [img.path], "reused": pick["verdict"], "why": pick["why"]}
    sh.image_path = storage.save(f"shots/{storage.safe(p.code)}/{sh.no}-{sh.kind}-a{sh.attempts}.jpg", out)
    sh.qc, sh.status, sh.note = result, status, ""
    s.commit()
    log(s, "pipeline", f"{p.code} {sh.kind}: {'input photo used as-is' if how['method'] == 'reused' else 'input photo with a small edit'}")
    return True


def _finish(s: Session, p: Product, stop_reason: str):
    p.error = stop_reason
    s.commit()
    s.refresh(p)
    made = [sh for sh in p.shots if sh.image_path]
    all_pass = bool(made) and all(sh.status in ("pass", "approved") for sh in made)
    all_pass = all_pass and all(any(sh.no == n and sh.image_path for sh in p.shots) for n in requested_shots(s, p))
    if all_pass and not p.error and get_setting(s, "auto_approve", True) and get_setting(s, "autopilot", False):
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
    p.data = {**(p.data or {}), "shopify": {**((p.data or {}).get("shopify") or {}), "status": "ready"}}
    if get_setting(s, "publish_shopify", False) and get_setting(s, "use_shopify", True):   # optional: push straight away instead of via the Shopify queue
        try:
            from . import shopq
            shopq.push_one(s, p)
        except Exception as e:
            errors.append(f"Shopify push failed: {e}")
    p.error = " · ".join(errors)[:900]
    log(s, who, f"Approved {p.code}: {len(finals)} shots saved" + (f" ({p.error})" if errors else ""))
    s.commit()


def rescan_watched(s: Session):
    if not get_setting(s, "watch_sources", False):         # folders are not watched unless switched on
        return
    from .ingest import scan
    due = now() - timedelta(minutes=config.WATCH_INTERVAL_MIN)
    for src in s.scalars(select(Source).where(Source.watch == True)):  # noqa: E712
        last = src.last_scan.replace(tzinfo=timezone.utc) if src.last_scan and src.last_scan.tzinfo is None else src.last_scan
        if not last or last < due:
            scan(s, src)


def next_job(s: Session) -> int | None:
    if STOP.is_set():
        return None
    """Manual 'run now' jobs first; otherwise the oldest queued product while autopilot is on."""
    for p in s.scalars(select(Product).where(Product.stage.in_(["queue", "gen"])).order_by(Product.id)):
        if (p.data or {}).get("run_now"):
            if day_spend(s) >= float(get_setting(s, "daily_budget", 3.0)):
                p.data, p.stage = {k: v for k, v in p.data.items() if k not in ("run_now", "only_shots")}, "error"
                p.error = "Daily budget reached. Raise it in Admin → Autopilot rules or wait until tomorrow."
                s.commit()
                return None
            return p.id
    if not get_setting(s, "autopilot", False):
        return None
    over_day = day_spend(s) >= float(get_setting(s, "daily_budget", 3.0))
    if over_day or (get_setting(s, "budget_action", "pause") == "pause" and month_spend(s) >= float(get_setting(s, "budget_month", 150))):
        from .db import set_setting
        set_setting(s, "autopilot", False)
        log(s, "system", "Autopilot paused: " + ("daily" if over_day else "monthly") + " budget reached")
        s.commit()
        return None
    cands = [p for p in s.scalars(select(Product).where(Product.stage.in_(["gen", "queue"])))
             if (p.data or {}).get("v2") and not (p.data or {}).get("paused")]
    cands.sort(key=lambda p: (p.stage != "gen", (p.data or {}).get("priority", 10_000), p.id))   # resume first, then priority
    return cands[0].id if cands else None


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
        if worked:
            config.touch()                              # still generating: stay active
            time.sleep(0.2)
        elif config.idle():
            time.sleep(60)                              # sleeps here; the database is not touched below
            while config.idle():
                time.sleep(30)
        else:
            time.sleep(config.WORKER_INTERVAL)


def start_background():
    threading.Thread(target=run_forever, daemon=True, name="autopilot").start()
