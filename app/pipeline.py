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

SHOT_NAMES = {1: "hero", 2: "detail", 3: "use_case", 4: "lifestyle"}      # 4 shots: arrangement dropped for accuracy
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
    pick = hero_m if kind in ("hero", "detail") else other_m
    # the model boxes in Admin -> API & models are what "pro" and "fast" mean (blank = built-in default)
    chosen = {"pro": get_setting(s, "model_hero_detail", ""), "fast": get_setting(s, "model_other", "")}.get(pick) or ""
    return config.model_name(chosen.strip() or config.mode_model(pick)), size


def shot_card(s: Session, p: Product, kind: str) -> dict:
    """The brand's settings for this shot (method, pose, scene, props, extra prompt), if the brand has any."""
    row = library(s, "brands").get(p.brand_id) or {}
    return ((row.get("shots") or {}).get(kind) or {}) if (row.get("shots") or {}).get(kind, {}).get("mode") == "custom" else {}


def _render_accurate(s, p, sh, spec, prov, model, size, refs, style, angle_raw, facts, correction, fallback_raw):
    """One shot with the accuracy engine. Returns (final jpeg, how)."""
    from . import engine
    card = shot_card(s, p, sh.kind)
    method, src_rel = engine.plan(sh.kind, angle_raw, card)
    src = storage.read(src_rel) if src_rel else fallback_raw
    how = {"method": method}
    if method == "crop":
        out, why = engine.detail_crop(src)
        if out:
            sh.model = "real photo (crop)"
            return storage.to_output(out), {"method": "crop", "exact": True}
        method, how = "edit", {"method": "edit", "note": why}
    if method in ("edit", "edit_zoom"):
        layout = {"edit_zoom": "lifestyle"}.get(method, sh.kind)
        placed = engine.place(src, layout)
        if placed["ok"]:
            import io as _io
            buf = _io.BytesIO()
            placed["canvas"].save(buf, "PNG")
            spec = {**spec, "fill": {**spec.get("fill", {}), "memory_text": facts}}
            instr = engine.edit_prompt(spec, sh.kind, card) + (f"\n\nCORRECTION: {correction.split('Fix:')[-1].strip()}" if "Fix:" in correction else "")
            gen = prov.edit(buf.getvalue(), instr, model, size, refs=refs[:2], style=style)
            used = getattr(prov, "last_model", None) or model
            sh.model, sh.prompt = used, instr
            _charge(s, p, "image", used, config.image_cost(used, size), sh)
            out, info = engine.restore(gen, placed)
            return storage.to_output(out), {"method": method, "exact": info["restored"], "drift": info["drift"],
                                            "upscaled": placed["upscaled"], **({"note": how["note"]} if how.get("note") else {})}
        how = {"method": "fresh", "note": placed["reason"]}
    prompt = spec["prompt"] + (("\n\n" + correction.split("[CORRECTION", 1)[-1]) if correction else "")
    cl = [f"{lbl}: {card[k]}" for k, lbl in (("scene", "Scene"), ("background", "Background / surface"), ("props", "Props"),
                                              ("pose", "Hands / people / pose"), ("extra", "Extra")) if card.get(k)]
    if cl:
        prompt += "\n\n[BRAND SHOT CARD: follow this over the generic scene above]\n" + "\n".join(cl)
    img = storage.to_output(prov.generate(prompt, src, model, size, refs=refs, style=style))
    used = getattr(prov, "last_model", None) or model
    sh.model = used
    _charge(s, p, "image", used, config.image_cost(used, size), sh)
    return img, {**how, "method": "fresh"}


def style_text(s: Session, p: Product, prov) -> str:
    """The golden shots described in words once (cached). Pictures of other products are never shown to the image
    model, so it cannot copy them into the scene."""
    from .dataset import is_unbranded
    from .db import LibraryRow
    keys = []
    if p.brand_id and p.brand_id != "_default" and not is_unbranded(p):
        keys.append(("brands", p.brand_id))
    keys += [("categories", p.category_id), ("brands", "_default")]
    for table, key in keys:
        row = s.scalar(select(LibraryRow).where(LibraryRow.table == table, LibraryRow.key == key))
        gold = list(((row.data or {}).get("golden") or [])) if row is not None else []
        if not gold:
            continue
        sig = "|".join(gold)
        if (row.data or {}).get("golden_sig") == sig and (row.data or {}).get("golden_style"):
            return row.data["golden_style"]
        imgs = []
        for rel in gold[:3]:
            try:
                imgs.append(storage.read(rel))
            except Exception:
                pass
        if not imgs or not hasattr(prov, "describe_style"):
            return ""
        try:
            txt = prov.describe_style(imgs)
            _charge(s, p, "vision", config.model_name(config.MODEL_VISION), config.COST_VISION)
        except Exception as e:
            print(f"[pipeline] style description failed: {e}")
            return ""
        row.data = {**(row.data or {}), "golden_style": txt, "golden_sig": sig}
        s.commit()
        return txt
    return ""


LOCK = ("\n\n[PRODUCT LOCK - overrides everything above]\nThe product must be identical to the real photos: same shape, "
        "proportions, colours, pattern and its placement, logo, every printed word, number and order of items. Never "
        "redesign, simplify, recolour or re-letter it, and never show a side that the photos do not show. "
        "\n[REALISM]\nIt must look like a real photo taken in our own studio: real materials, real scale, natural "
        "shadows. Props are plain, unbranded, real everyday objects: no logos, no brand names, no other products of "
        "any brand, no invented packaging. No text, captions or graphics that are not printed on the product.")
ANCHOR = ("\n\n[CONSISTENCY WITH THE HERO]\nThe second image is our approved hero shot of this exact product. The "
          "product in your picture must be indistinguishable from it: same design, same pattern placement, same "
          "colours, same text. Keep the same face of the product towards the camera and the same orientation; you "
          "may move the camera higher, closer or farther, but do not rotate the product to reveal unseen sides.")


def style_reference(s: Session, p: Product) -> bytes | None:
    """One approved 'golden' shot: the brand's, or for unbranded / no-profile products the category's."""
    from .dataset import is_unbranded
    rows = []
    if p.brand_id and p.brand_id != "_default" and not is_unbranded(p):
        rows.append(library(s, "brands").get(p.brand_id) or {})
    rows.append(library(s, "categories").get(p.category_id) or {})
    rows.append(library(s, "brands").get("_default") or {})
    for row in rows:
        for rel in row.get("golden") or []:
            try:
                return storage.read(rel)
            except Exception:
                continue
    return None


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
    _charge(s, p, "vision", config.model_name(config.MODEL_VISION), config.COST_VISION)
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
        if (p.data or {}).get("v2") and (p.data or {}).get("engine") == "composite":
            from . import v2
            stop_reason = v2.process(s, p, prov, only, _charge)
            _finish(s, p, stop_reason)
            return
        if not p.analysis:
            try:
                analyse(s, p, prov, raw)
            except Exception as e:                       # carry on with dataset defaults
                print(f"[pipeline] analysis failed for {p.code}: {e}")
                p.analysis = {"_error": str(e)[:300]}
                if not p.category_id:
                    from .dataset import library as _lib
                    p.category_id = next(iter(_lib(s, "categories")))
                s.commit()
        rv = live_rules(s)
        shots = {sh.no: sh for sh in p.shots}
        for no, kind in SHOT_NAMES.items():
            if no not in shots:
                shots[no] = Shot(no=no, kind=kind)
                p.shots.append(shots[no])
        s.commit()
        notes = {sh.no: sh.note for sh in shots.values() if sh.note}
        prompts = {x["no"]: x for x in build_prompts(s, p, p.analysis or {}, rv, notes)}
        retries = int(get_setting(s, "retries", 0))
        qc_mode = get_setting(s, "qc_mode", "light")
        cap = float(get_setting(s, "max_cost_product", 0.30))
        hero_gate = bool(get_setting(s, "hero_gate", True))
        defaults = [int(x) for x in (get_setting(s, "default_shots", [1]) or [1]) if int(x) in SHOT_NAMES]
        if (p.data or {}).get("v2"):                    # shoot sheet: only the shots it asks for
            from .v2 import requested_shots
            defaults = requested_shots(s, p)
        style = None                                      # golden shots go in as words, never as pictures
        style_note = style_text(s, p, prov)
        accurate = bool((p.data or {}).get("lib_key")) and get_setting(s, "engine_mode", "accurate") == "accurate"
        angle_raw = {}
        if (p.data or {}).get("lib_key"):
            from . import library as lib
            key = p.data["lib_key"]
            lp = s.scalar(select(lib.LibProduct).where(lib.LibProduct.key == key))
            if lp is not None and not ((lp.data or {}).get("memory") or {}).get("facts"):
                try:                                       # study once (≈ ₹0.3), reused for every future shoot
                    lib.study_product(s, key, prov)
                    _charge(s, p, "memory", config.model_name(config.MODEL_VISION), config.COST_VISION * 2)
                except Exception as e:
                    print(f"[pipeline] product study failed for {p.code}: {e}")
            if lp is not None:
                p.data = {**(p.data or {}), "memory": (lp.data or {}).get("memory") or {}}
            s.commit()
            for r in lib.refs_with_angles(s, key, 4):
                angle_raw.setdefault(r["angle"] or "unknown", r["path"])
            prompts = {x["no"]: x for x in build_prompts(s, p, p.analysis or {}, rv, notes)}   # now with product facts
        refs = []
        for rel in ((p.data or {}).get("refs") or [])[1:]:   # extra angles of the same product from the Library
            try:
                refs.append(storage.read(rel))
            except Exception:
                pass
        todo = only or [no for no in defaults if shots[no].status in ("wait", "gen", "fail") or not shots[no].image_path]
        stop_reason = ""
        for no in sorted(todo):
            sh = shots[no]
            spec = prompts[no]
            model, size = _model_for(s, sh.kind)
            sh.status, sh.prompt, sh.model, sh.rule_version, sh.qc = "gen", spec["prompt"], model, rv.version, {}
            s.commit()
            prompt = spec["prompt"]
            shot_raw = raw
            if angle_raw:
                want = ("front", "unknown", "angle", "side")       # every shot starts from the same main photo as the hero
                rel = next((angle_raw[a] for a in want if a in angle_raw), None)
                if rel:
                    try:
                        shot_raw = storage.read(rel)
                    except Exception:
                        shot_raw = raw
            facts = ""
            mem = (p.data or {}).get("memory") or {}
            if mem.get("facts"):
                from .dataset import memory_block
                facts = memory_block(mem).split("\n", 1)[-1].replace("\n", "; ")
            hero_ref = None
            h = shots.get(1)
            if no != 1 and h is not None and h.image_path and h.status in ("pass", "approved", "unchecked"):
                try:
                    hero_ref = storage.read(h.image_path)
                except Exception:
                    hero_ref = None
            card = shot_card(s, p, sh.kind)
            cl = [f"{lbl}: {card[k]}" for k, lbl in (("scene", "Scene"), ("background", "Background / surface"),
                  ("props", "Props (plain, unbranded, real objects)"), ("pose", "Hands / people / pose"),
                  ("extra", "Extra")) if card.get(k)]
            ang = {"flat": "Camera: top-down flat lay, the product lying on its back with the same face up as in the photos.",
                   "photo": "Use the angle of the real 45-degree or side photo of this product."}.get(card.get("angle"), "")
            used_photo = False
            if card.get("angle") == "photo":
                rel2 = next((angle_raw[a] for a in ("angle", "side") if a in angle_raw), None)
                if rel2:
                    try:
                        shot_raw = storage.read(rel2)
                        ang += " The first image shows that angle."
                        used_photo = True
                    except Exception:
                        pass
            if ang == "" or (card.get("angle") == "photo" and not any(a in angle_raw for a in ("angle", "side"))):
                ang = "" if no != 1 else "Camera: the same angle as the main real photo."
            if cl or ang:
                spec = {**spec, "prompt": spec["prompt"] + "\n\n[BRAND SHOT CARD - follow this over the generic scene "
                        "above]\n" + "\n".join(cl + ([ang] if ang else []))}
            spec = {**spec, "prompt": spec["prompt"] + (f"\n\n[BRAND LOOK - light, colours and mood only, copy no "
                                                         f"objects]\n{style_note}" if style_note else "")
                    + LOCK + ((ANCHOR.replace("Keep the same face of the product towards the camera and the same orientation; you may move the camera higher, closer or farther, but do not rotate the product to reveal unseen sides.", "Show the product at the angle of the FIRST image (a real photo of that side).") if used_photo else ANCHOR) if hero_ref else "")}
            prompt = spec["prompt"]
            call_refs = ([hero_ref] if hero_ref else []) + refs[: (1 if hero_ref else 2)]
            result = None
            for attempt in range(retries + 1):
                if STOP.is_set():
                    stop_reason = "Stopped by you"
                    break
                est = config.image_cost(model, size) + config.COST_VISION
                if p.cost + est > cap:
                    stop_reason = f"Stopped at the ${cap:.2f} per-product cost limit"
                    break
                sh.attempts = (sh.attempts or 0) + 1
                how = {"method": "fresh"}
                if accurate:
                    img, how = _render_accurate(s, p, sh, spec, prov, model, size, refs, style, angle_raw, facts,
                                                prompt if attempt else "", shot_raw)
                else:
                    img = storage.to_output(prov.generate(prompt, shot_raw, model, size, refs=call_refs, style=None))
                    used = getattr(prov, "last_model", None) or model     # may differ after a fallback
                    sh.model = used
                    _charge(s, p, "image", used, config.image_cost(used, size), sh)
                rel = storage.save(f"shots/{storage.safe(p.code)}/{no}-{sh.kind}-a{sh.attempts}.jpg", img)
                if how.get("exact"):                     # real product pixels: shape and text cannot have drifted
                    result = {"checks": {"product_exact": True}, "reasons": {}, "how": how}
                else:
                    try:
                        result = prov.qc(hero_ref or shot_raw, img, {"kind": sh.kind, "no": no, "facts": facts,
                                         "vs_hero": bool(hero_ref)}, rv.data)
                        _charge(s, p, "qc", config.model_name(config.MODEL_VISION), config.COST_VISION, sh)
                    except Exception as e:               # keep the image; a person checks it instead
                        print(f"[pipeline] QC failed for {p.code} shot {no}: {e}")
                        result = {"checks": {}, "reasons": {"qc": f"Automatic QC unavailable: {str(e)[:200]}"},
                                  "unchecked": True}
                    result["how"] = how
                sh.image_path, sh.qc = rel, result
                s.commit()
                if result.get("unchecked") or _passed(result, qc_mode):
                    break
                failed = [f"{k}: {result['reasons'].get(k, 'failed')}" for k, v in result["checks"].items()
                          if not v and (qc_mode != "light" or k in CRITICAL)]
                if (p.data or {}).get("lib_key"):
                    from .library import learn_from_fail
                    learn_from_fail(s, p.data["lib_key"], [f"{sh.kind}: {r}" for r in failed])
                prompt = spec["prompt"] + "\n\n[CORRECTION — previous attempt failed QC]\nFix: " + "; ".join(failed)
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


def _finish(s: Session, p: Product, stop_reason: str):
    p.error = stop_reason
    s.commit()
    s.refresh(p)
    made = [sh for sh in p.shots if sh.image_path]
    all_pass = bool(made) and all(sh.status in ("pass", "approved") for sh in made)
    if (p.data or {}).get("v2"):
        from .v2 import requested_shots
        want = requested_shots(s, p)
        all_pass = all_pass and all(any(sh.no == n and sh.image_path for sh in p.shots) for n in want)
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
    if get_setting(s, "publish_shopify", False):          # optional: push straight away instead of via the Shopify queue
        try:
            from . import shopq
            shopq.push_one(s, p)
        except Exception as e:
            errors.append(f"Shopify push failed: {e}")
    p.error = " · ".join(errors)[:900]
    log(s, who, f"Approved {p.code}: {len(finals)} shots saved" + (f" ({p.error})" if errors else ""))
    s.commit()


def rescan_watched(s: Session):
    if get_setting(s, "pipeline_mode", "v1") == "v2":      # v2 works only from the shoot sheet: no random inputs
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
    v2mode = get_setting(s, "pipeline_mode", "v1") == "v2"
    over_day = day_spend(s) >= float(get_setting(s, "daily_budget", 3.0))
    if over_day or (get_setting(s, "budget_action", "pause") == "pause" and month_spend(s) >= float(get_setting(s, "budget_month", 150))):
        from .db import set_setting
        set_setting(s, "autopilot", False)
        log(s, "system", "Autopilot paused: " + ("daily" if over_day else "monthly") + " budget reached")
        s.commit()
        return None
    cands = [p for p in s.scalars(select(Product).where(Product.stage.in_(["gen", "queue"])))
             if bool((p.data or {}).get("v2")) == v2mode and not (p.data or {}).get("paused")]
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
        time.sleep(0.2 if worked else config.WORKER_INTERVAL)


def start_background():
    threading.Thread(target=run_forever, daemon=True, name="autopilot").start()
